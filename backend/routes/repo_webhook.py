"""Forgejo webhooks — the machine-to-machine door into the Agent Hub (§5.4).

A webhook is the one route in this application that **must not** ask for a
session: it is called by the Forgejo container, which has no user, no cookie and
no API key.  Its credential is a shared secret and its proof is an HMAC over the
raw request body, so every request is rejected with ``401`` unless
``X-Forgejo-Signature`` matches ``HMAC-SHA256(secret, body)`` under a
constant-time comparison.

Events and the action each one triggers (§5.4):

===================================  =========================================
``push``                             default branch only, and only when the
                                     review policy allows: queue a ``review``
``issues``                           an issue labelled ``agent``: queue ``fix``
``pull_request``                     record the PR URL on the findings it
                                     closes; a merge also queues the doc
                                     write-back
===================================  =========================================

The secret lives in ``FORGEJO_WEBHOOK_SECRET`` and nowhere else — not in a
config file, not in a log line, not in a response.  The route is declared public
with ``security=[]`` so ``scripts/check_auth_guards.py`` knows the anonymity is
deliberate rather than an oversight; the HMAC check is the actual guard.

Everything decision-shaped is a module-level function
(:func:`verify_signature`, :func:`parse_event`, :func:`plan_actions`) so
``scripts/check_agent_repos.py`` can exercise the whole policy offline, with a
fake queue and no Flask app.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from flask import Blueprint, current_app, jsonify, request

from models.agent_hub import TASK_KIND
from openapi import api_operation, errors, json_body, ok
from services.repo_import import ImportConfig, read_policy_auto_review

logger = logging.getLogger("cpypiserver.routes.repo_webhook")

repo_webhook_bp = Blueprint("repo_webhook", __name__)

#: Header Forgejo signs with (``X-Forgejo-Signature``; ``X-Gitea-Signature`` is
#: accepted too because the same code signs both, and a fork renamed the
#: header without changing the digest).
SIGNATURE_HEADERS = ("X-Forgejo-Signature", "X-Gitea-Signature", "X-Hub-Signature-256")

#: Event header.  The payload's ``action`` field distinguishes e.g.
#: ``issues`` opened from ``issues`` labeled.
EVENT_HEADERS = ("X-Forgejo-Event", "X-Gitea-Event", "X-GitHub-Event")

#: Label that turns an issue into a fix task.
AGENT_LABEL = "agent"

#: Queue priorities.  ``AgentQueue.lease`` orders by ``priority DESC``, so a
#: *larger* number is more urgent.  A merge-triggered doc write-back is the
#: least urgent thing the webhook can ask for; an ``agent``-labelled issue is a
#: person waiting, so it outranks the automatic review.
PRIORITY_DOC = 3
PRIORITY_REVIEW = 5
PRIORITY_FIX = 8

#: The task kind a merged pull request queues.  S0's ``TASK_KIND`` is a closed
#: set (``review | fix | import | backfill``) and there is no "docs" kind: the
#: doc write-back *is* a backfill of the repository's history into ``/docs``.
DOC_TASK_KIND = "backfill"

#: Actions that make an issue worth acting on.
ISSUE_ACTIONS = frozenset({"opened", "reopened", "labeled", "created", "edited"})

#: Actions that mean a pull request landed.
MERGED_ACTIONS = frozenset({"closed", "merged", "merged_and_closed"})

#: Statuses that mean a pull request landed (``action == "closed"`` alone is
#: ambiguous, hence the extra ``merged`` flag Forgejo sends).
MERGED_STATES = frozenset({"merged", "merged_and_closed"})


# ── Signature ────────────────────────────────────────────────────────

def compute_signature(secret: str, body: bytes) -> str:
    """The hex HMAC-SHA256 of *body* under *secret*."""
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def verify_signature(
    body: bytes,
    header: str | None,
    *,
    secret: str,
) -> bool:
    """Constant-time check of ``X-Forgejo-Signature`` against *body*.

    Accepts the bare hex digest and the ``sha256=<hex>`` form other forges use.
    A missing secret, a missing header or a mismatch is ``False`` — never an
    exception, because the caller must answer an identical ``401`` for all
    three (an oracle that distinguishes them would leak whether a secret is
    configured at all).
    """
    if not secret:
        return False
    candidate = str(header or "").strip()
    if not candidate:
        return False
    if "=" in candidate:
        algorithm, _, value = candidate.partition("=")
        if algorithm.strip().lower() not in {"sha256", "sha-256"}:
            return False
        candidate = value.strip()
    if not candidate or len(candidate) != 64:
        return False
    expected = compute_signature(secret, body)
    # compare_digest, not ==: a byte-by-byte comparison leaks the length of the
    # matching prefix, which is enough to forge a digest one nibble at a time.
    return hmac.compare_digest(expected, candidate.lower())


def signature_header(headers: Mapping[str, str]) -> str | None:
    """The first signature header the request carries, if any."""
    for name in SIGNATURE_HEADERS:
        value = headers.get(name)
        if value:
            return value
    return None


# ── Event parsing ────────────────────────────────────────────────────

@dataclass
class WebhookEvent:
    """One accepted webhook, normalised across Gitea/Forgejo payload versions."""

    kind: str                       # push | issues | pull_request | other
    action: str = ""
    repo_slug: str = ""             # "<owner>/<name>" as the source names it
    forgejo_repo: str = ""          # "<owner>/<name>" as this platform names it
    default_branch: str = ""
    ref: str = ""
    branch: str = ""
    labels: tuple[str, ...] = ()
    issue_number: int | None = None
    pr_number: int | None = None
    pr_url: str = ""
    merged: bool = False
    sender: str = ""
    commits: tuple[dict[str, Any], ...] = ()
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_default_branch(self) -> bool:
        if not self.branch or not self.default_branch:
            return False
        return self.branch == self.default_branch

    @property
    def has_agent_label(self) -> bool:
        return AGENT_LABEL in {label.lower() for label in self.labels}

    def as_dict(self) -> dict[str, Any]:
        return {
            "event": self.kind,
            "action": self.action,
            "repo": self.repo_slug or self.forgejo_repo,
            "branch": self.branch,
            "default_branch": self.default_branch,
            "issue_number": self.issue_number,
            "pr_number": self.pr_number,
            "labels": list(self.labels),
            "merged": self.merged,
            "sender": self.sender,
        }


def _nested(payload: Mapping[str, Any], *path: str) -> Any:
    node: Any = payload
    for key in path:
        if not isinstance(node, Mapping):
            return None
        node = node.get(key)
    return node


def _labels_of(payload: Mapping[str, Any]) -> tuple[str, ...]:
    raw = payload.get("labels")
    names: list[str] = []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, Mapping) and item.get("name"):
                names.append(str(item["name"]))
            elif isinstance(item, str) and item:
                names.append(item)
    single = payload.get("label")
    if isinstance(single, Mapping) and single.get("name"):
        names.append(str(single["name"]))
    elif isinstance(single, str) and single:
        names.append(single)
    return tuple(dict.fromkeys(names))


def _repo_names(payload: Mapping[str, Any]) -> tuple[str, str, str]:
    """``(slug, forgejo_repo, default_branch)`` from the payload's repo object."""
    node = payload.get("repository")
    if not isinstance(node, Mapping):
        node = payload.get("repo") if isinstance(payload.get("repo"), Mapping) else {}
    slug = str(node.get("full_name") or "").strip()
    if not slug:
        owner = node.get("owner")
        owner_login = ""
        if isinstance(owner, Mapping):
            owner_login = str(owner.get("login") or owner.get("username") or "")
        elif isinstance(owner, str):
            owner_login = owner
        name = str(node.get("name") or "")
        if owner_login and name:
            slug = f"{owner_login}/{name}"
    return slug, slug, str(node.get("default_branch") or "main")


def parse_event(
    event_name: str,
    payload: Mapping[str, Any],
    *,
    body: bytes = b"",
) -> WebhookEvent:
    """Normalise one Forgejo/Gitea webhook body.

    Unknown event names come back as ``kind="other"`` rather than raising: a
    webhook that answers 500 makes Forgejo retry forever, and an unhandled event
    is not an error.
    """
    name = str(event_name or "").split(",")[0].strip().lower()
    if not isinstance(payload, Mapping):
        payload = {}
    slug, forgejo_repo, default_branch = _repo_names(payload)
    action = str(payload.get("action") or "").strip().lower()
    sender_node = payload.get("sender")
    sender = ""
    if isinstance(sender_node, Mapping):
        sender = str(sender_node.get("login") or sender_node.get("username") or "")
    elif isinstance(sender_node, str):
        sender = sender_node

    if name == "push":
        ref = str(payload.get("ref") or "")
        branch = ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else ref
        commits = payload.get("commits")
        return WebhookEvent(
            kind="push",
            action="pushed",
            repo_slug=slug,
            forgejo_repo=forgejo_repo,
            default_branch=default_branch,
            ref=ref,
            branch=branch,
            sender=sender,
            commits=tuple(c for c in commits if isinstance(c, Mapping)) if isinstance(commits, list) else (),
            raw=dict(payload),
        )

    if name in {"issues", "issue"}:
        issue = payload.get("issue") if isinstance(payload.get("issue"), Mapping) else {}
        number = issue.get("number")
        return WebhookEvent(
            kind="issues",
            action=action or "opened",
            repo_slug=slug,
            forgejo_repo=forgejo_repo,
            default_branch=default_branch,
            labels=_labels_of(issue) or _labels_of(payload),
            issue_number=int(number) if isinstance(number, int) else None,
            sender=sender,
            raw=dict(payload),
        )

    if name in {"pull_request", "pullrequest", "pull-request"}:
        pr = payload.get("pull_request") if isinstance(payload.get("pull_request"), Mapping) else {}
        number = pr.get("number") or payload.get("number")
        state = str(pr.get("state") or "").lower()
        merged = bool(pr.get("merged")) or state in MERGED_STATES
        if action in MERGED_ACTIONS and merged:
            merged = True
        return WebhookEvent(
            kind="pull_request",
            action=action or state,
            repo_slug=slug,
            forgejo_repo=forgejo_repo,
            default_branch=default_branch,
            issue_number=int(number) if isinstance(number, int) else None,
            pr_number=int(number) if isinstance(number, int) else None,
            pr_url=str(pr.get("html_url") or pr.get("url") or ""),
            merged=merged,
            labels=_labels_of(pr),
            sender=sender,
            raw=dict(payload),
        )

    return WebhookEvent(
        kind="other",
        action=action,
        repo_slug=slug,
        forgejo_repo=forgejo_repo,
        default_branch=default_branch,
        sender=sender,
        raw=dict(payload),
    )


# ── Queue integration (S0's services.agent_queue) ────────────────────

@dataclass
class QueuedAction:
    """One queued agent task, as planned or as delivered."""

    kind: str
    repo_id: int
    priority: int
    payload: dict[str, Any]
    delivered: bool = False
    task: Any = None
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "repo_id": self.repo_id,
            "priority": self.priority,
            "delivered": self.delivered,
            "reason": self.reason,
            "payload": self.payload,
        }


def _default_enqueue() -> tuple[Callable[..., Any], Any] | None:
    """Resolve a task sink from S0's queue, or ``None`` when it is unavailable.

    ``services.agent_queue`` exposes ``AgentQueue.enqueue(repo_id, *, kind,
    payload, priority, …)`` — there is no module-level function — so the
    process-wide instance is reused when the app has one (``agent_queue`` in
    ``current_app.extensions``, as ``routes/agent_tasks.py`` builds it) and
    otherwise one is built on the engine.  Returning ``(bound_method, owner)``
    keeps the owner alive for the duration of the call.

    The deferred import matters: the webhook must still answer ``200`` when the
    queue is absent (Forgejo would otherwise retry a request that can never
    succeed), logging the task it *would* have queued instead.
    """
    try:
        from services.agent_queue import AgentQueue  # noqa: PLC0415 - S0's module
    except ImportError:
        return None
    queue = None
    try:
        queue = current_app.extensions.get("agent_queue")
    except RuntimeError:  # pragma: no cover - no app context (CLI/tests)
        queue = None
    if queue is None:
        try:
            from extensions.database import Session  # noqa: PLC0415
        except ImportError:
            return None
        bind = getattr(Session, "bind", None)
        if bind is None:
            return None
        try:
            queue = AgentQueue(bind)
        except Exception as exc:  # noqa: BLE001 - an unavailable queue is not fatal
            logger.warning("could not build an AgentQueue: %s", exc)
            return None
    sender = getattr(queue, "enqueue", None)
    return (sender, queue) if callable(sender) else None


def enqueue_task(
    *,
    kind: str,
    repo_id: int,
    payload: dict[str, Any],
    priority: int,
    enqueue: Callable[..., Any] | None = None,
) -> QueuedAction:
    """Queue one agent task, degrading to a logged no-op when the queue is absent.

    The call shape is S0's::

        queue.enqueue(repo_id, kind=..., payload=dict, priority=int)
    """
    action = QueuedAction(kind=kind, repo_id=repo_id, priority=priority, payload=payload)
    if kind not in TASK_KIND:
        action.reason = f"未知任务类型 {kind!r}（TASK_KIND={TASK_KIND}）"
        logger.warning("webhook will not queue %s for repo %s: %s",
                       kind, repo_id, action.reason)
        return action
    resolved = (enqueue, None) if enqueue is not None else _default_enqueue()
    if resolved is None:
        action.reason = "services.agent_queue 不可用（S0 未交付或队列未初始化）"
        logger.warning("webhook would queue %s for repo %s: %s",
                       kind, repo_id, action.reason)
        return action
    sender, _owner = resolved
    try:
        action.task = sender(repo_id, kind=kind, payload=payload, priority=priority)
        action.delivered = True
    except Exception as exc:  # noqa: BLE001 - a queue fault must not 500 a webhook
        action.reason = f"{type(exc).__name__}: {exc}"
        logger.exception("webhook could not queue %s for repo %s", kind, repo_id)
    return action


# ── Policy ───────────────────────────────────────────────────────────

def plan_actions(
    event: WebhookEvent,
    *,
    repo_id: int,
    auto_review: bool | None = None,
    policy_reader: Callable[[], bool] | None = None,
) -> list[QueuedAction]:
    """Decide what *event* should queue.  Pure: no I/O, no database.

    That purity is what lets the offline gate assert the §5.4 table exactly —
    a push to the default branch with ``auto_review`` on queues a review, a push
    to a side branch queues nothing, an ``agent``-labelled issue queues a fix,
    and anything else queues nothing.  ``auto_review`` (when given) overrides
    the policy lookup, which is how the gate drives both branches of that
    decision with no files on disk.
    """
    actions: list[QueuedAction] = []

    if event.kind == "push":
        if not event.is_default_branch:
            return actions
        if auto_review is not None:
            allowed = bool(auto_review)
        else:
            reader = policy_reader or read_policy_auto_review
            try:
                allowed = bool(reader())
            except Exception as exc:  # noqa: BLE001 - policy trouble never blocks a push
                logger.warning("auto_review policy lookup failed: %s", exc)
                allowed = True
        if not allowed:
            return actions
        actions.append(QueuedAction(
            kind="review",
            repo_id=repo_id,
            priority=PRIORITY_REVIEW,
            payload={
                "commit_sha": _head_sha(event),
                "branch": event.branch,
                "trigger": "push",
                "sender": event.sender,
            },
            reason="push to default branch with auto_review",
        ))
        return actions

    if event.kind == "issues":
        if not event.has_agent_label:
            return actions
        if event.action in {"closed", "deleted"}:
            return actions
        actions.append(QueuedAction(
            kind="fix",
            repo_id=repo_id,
            priority=PRIORITY_FIX,
            payload={
                "issue_number": event.issue_number,
                "labels": list(event.labels),
                "trigger": "issue-label",
                "sender": event.sender,
            },
            reason=f"issue carries the {AGENT_LABEL!r} label",
        ))
        return actions

    if event.kind == "pull_request" and event.merged:
        # `TASK_KIND` is S0's closed set — `backfill` is the repository-wide
        # "write the history back" task, which is what §5.4's doc write-back is.
        actions.append(QueuedAction(
            kind=DOC_TASK_KIND,
            repo_id=repo_id,
            priority=PRIORITY_DOC,
            payload={
                "pr_number": event.pr_number,
                "pr_url": event.pr_url,
                "trigger": "pull_request-merged",
                "sender": event.sender,
            },
            reason="merged pull request triggers the doc write-back",
        ))
    return actions


def _head_sha(event: WebhookEvent) -> str:
    """The pushed tip, from the payload's commit list when it is present."""
    for commit in reversed(event.commits):
        sha = commit.get("id") or commit.get("sha")
        if sha:
            return str(sha)
    return ""


# ── Repo resolution ──────────────────────────────────────────────────

def resolve_repo(session: Any, event: WebhookEvent) -> Any | None:
    """Find the ``repos`` row a webhook is about.

    Prefers ``forgejo_repo`` (the Forgejo-side full name) and falls back to
    ``slug`` — for a repository created by ``POST /api/v1/repos`` the two differ
    (Forgejo gets ``owner__name``), and resolving either way is what keeps the
    webhook useful while the naming policy is still being tuned.
    """
    from models.agent_hub import Repo

    candidates = [value for value in (event.forgejo_repo, event.repo_slug) if value]
    if not candidates:
        return None
    for column in ("forgejo_repo", "slug"):
        if not hasattr(Repo, column):
            continue
        attr = getattr(Repo, column)
        for value in candidates:
            found = session.query(Repo).filter(attr == value).one_or_none()
            if found is not None:
                return found
    return None


def backfill_pr_url(session: Any, repo_id: int, number: int, url: str) -> int:
    """Record ``pr_url`` on the findings whose run produced this PR.

    S3 owns the finding state machine; this is the one write §5.4 asks the
    webhook to make.  It is defensive on purpose: a schema S3 has not finished
    shipping must not turn a webhook into a 500.
    """
    if not number or not url:
        return 0
    try:
        from models.agent_hub import Finding  # noqa: PLC0415 - S3's table
    except ImportError:
        logger.info("findings table not present yet; skipping pr_url backfill")
        return 0
    if not hasattr(Finding, "pr_url"):
        return 0
    updated = 0
    try:
        rows = (
            session.query(Finding)
            .filter(Finding.repo_id == repo_id, Finding.pr_url.is_(None))
            .all()
        )
        for row in rows:
            setattr(row, "pr_url", url)
            updated += 1
        if updated:
            session.commit()
    except Exception as exc:  # noqa: BLE001 - best-effort by design
        session.rollback()
        logger.warning("could not backfill pr_url for repo %s: %s", repo_id, exc)
        return 0
    return updated


# ── The route ────────────────────────────────────────────────────────

_WEBHOOK_SCHEMA = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean"},
        "event": {"type": "string"},
        "action": {"type": "string"},
        "repo": {"type": ["string", "null"]},
        "slug": {"type": ["string", "null"]},
        "handled": {"type": "boolean"},
        "queued": {"type": "array", "items": {"type": "object"}},
        "findings_updated": {"type": "integer"},
        "note": {"type": ["string", "null"]},
    },
}


def _secret() -> str:
    return ImportConfig.from_env().webhook_secret


def _session():
    from extensions.database import Session

    return Session


@repo_webhook_bp.route("/api/v1/repos/webhook", methods=["POST"])
@api_operation(
    summary="Forgejo → openfish webhook",
    description=(
        "Receives `push`, `issues` and `pull_request` events from the Forgejo "
        "container. **Anonymous by design** — it is called by a machine, not a "
        "user — and authenticated instead by "
        "`X-Forgejo-Signature: <hex HMAC-SHA256 of the raw body>` under the "
        "shared secret in `FORGEJO_WEBHOOK_SECRET`.\n\n"
        "A missing or mismatched signature is always `401`, with no detail "
        "about which of the two it was. The comparison is constant-time.\n\n"
        "A `push` to the default branch queues a `review` task when the review "
        "policy allows; an `issues` event whose labels include `agent` queues a "
        "`fix` task; a `pull_request` event records the PR URL against the "
        "findings it closes. Unknown events answer `200` with "
        "`handled: false` so Forgejo does not retry them forever."
    ),
    tags=["Repositories"],
    # Anonymous on purpose: the HMAC is the credential.  `security=[]` is what
    # tells scripts/check_auth_guards.py this is a decision, not an omission.
    security=[],
    request_body={"required": True, "content": json_body()},
    responses={
        "200": ok("The event was accepted", _WEBHOOK_SCHEMA),
        **errors("400", "401", "404", "500"),
    },
)
def forgejo_webhook():
    secret = _secret()
    body = request.get_data(cache=True, as_text=False) or b""
    header = signature_header(request.headers)
    # One 401 for every failure mode: no secret configured, no signature, bad
    # signature.  Distinguishing them would tell an attacker whether guessing is
    # even worth their time.
    if not verify_signature(body, header, secret=secret):
        logger.warning("rejected forgejo webhook: signature mismatch (secret %s)",
                       "configured" if secret else "unset")
        return jsonify({
            "error": "invalid_signature",
            "error_description": "webhook 签名校验失败",
        }), 401

    try:
        payload = json.loads(body.decode("utf-8")) if body else {}
    except (UnicodeDecodeError, ValueError) as exc:
        return jsonify({
            "error": "invalid_payload",
            "error_description": f"webhook 请求体不是合法 JSON：{exc}",
        }), 400
    if not isinstance(payload, dict):
        return jsonify({
            "error": "invalid_payload",
            "error_description": "webhook 请求体必须是 JSON 对象",
        }), 400

    event_name = next(
        (request.headers.get(name) for name in EVENT_HEADERS if request.headers.get(name)),
        "",
    )
    event = parse_event(event_name, payload, body=body)
    session = _session()
    repo = resolve_repo(session, event)
    if repo is None:
        logger.warning("webhook for unknown repository %r (%s)",
                       event.forgejo_repo or event.repo_slug, event.kind)
        return jsonify({
            "error": "repo_not_found",
            "error_description": (
                f"平台没有镜像仓库 {event.forgejo_repo or event.repo_slug!r}；"
                "先用 POST /api/v1/repos/import 导入"
            ),
            "event": event.kind,
        }), 404

    actions = plan_actions(event, repo_id=repo.id)
    queued: list[QueuedAction] = []
    for action in actions:
        queued.append(enqueue_task(
            kind=action.kind,
            repo_id=action.repo_id,
            payload=action.payload,
            priority=action.priority,
        ))

    updated = 0
    if event.kind == "pull_request":
        updated = backfill_pr_url(
            session, repo.id, event.pr_number or 0, event.pr_url,
        )

    handled = bool(queued) or event.kind == "pull_request"
    note = None
    if event.kind == "push" and not event.is_default_branch:
        note = "push 目标不是默认分支，未入队"
    elif not handled:
        note = "事件已接受，但没有匹配的动作"
    logger.info(
        "webhook %s/%s for %s → %d queued, %d finding(s) updated",
        event.kind, event.action, repo.slug, len(queued), updated,
    )
    return jsonify({
        "ok": True,
        "event": event.kind,
        "action": event.action,
        "repo": event.forgejo_repo or event.repo_slug,
        "slug": repo.slug,
        "handled": handled,
        "queued": [action.as_dict() for action in queued],
        "findings_updated": updated,
        "note": note,
        "event_summary": event.as_dict(),
    })


__all__ = [
    "AGENT_LABEL",
    "EVENT_HEADERS",
    "SIGNATURE_HEADERS",
    "QueuedAction",
    "WebhookEvent",
    "backfill_pr_url",
    "compute_signature",
    "enqueue_task",
    "parse_event",
    "plan_actions",
    "repo_webhook_bp",
    "resolve_repo",
    "signature_header",
    "verify_signature",
]
