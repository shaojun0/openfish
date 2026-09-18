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
from services.digest import sha256_text
from services.repo_import import ImportConfig, read_policy_auto_review
from services.review_policy import (
    CURATOR_AUTO,
    CURATOR_BOOTSTRAP,
    CURATOR_MODES,
    DEFAULT_CURATOR_MIN_INTERVAL_SECONDS,
)

logger = logging.getLogger("cpypiserver.routes.repo_webhook")

repo_webhook_bp = Blueprint("repo_webhook", __name__)

#: Header Forgejo signs with (``X-Forgejo-Signature``; ``X-Gitea-Signature`` is
#: accepted too because the same code signs both, and a fork renamed the
#: header without changing the digest).
SIGNATURE_HEADERS = ("X-Forgejo-Signature", "X-Gitea-Signature", "X-Hub-Signature-256")

#: Event header.  The payload's ``action`` field distinguishes e.g.
#: ``issues`` opened from ``issues`` labeled.
EVENT_HEADERS = ("X-Forgejo-Event", "X-Gitea-Event", "X-GitHub-Event")

#: Delivery header.  This is **not** covered by the HMAC, so it is never trusted
#: for a decision: :func:`delivery_id` reads it for triage/logging only, and the
#: replay key is derived from the signed body instead (a header an attacker can
#: strip or rewrite cannot be an idempotency key).
DELIVERY_HEADERS = ("X-Forgejo-Delivery", "X-Gitea-Delivery", "X-GitHub-Delivery")

#: Label that turns an issue into a fix task.
AGENT_LABEL = "agent"

#: Queue priorities.  ``AgentQueue.lease`` orders by ``priority DESC``, so a
#: *larger* number is more urgent.  A merge-triggered doc write-back is no longer
#: queued (it runs inline in the view), so ``PRIORITY_DOC`` survives only as the
#: floor that keeps the ordering assertions honest; an ``agent``-labelled issue
#: is a person waiting, so it outranks the automatic review.
PRIORITY_DOC = 3
#: A curator proposal is background housekeeping: below a review, above nothing.
PRIORITY_CURATOR = 4
PRIORITY_REVIEW = 5
PRIORITY_FIX = 8

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
    candidate = candidate.lower()
    # A signature is hex, so anything non-ASCII is already a mismatch.  Rejecting
    # it here keeps the promise above: ``hmac.compare_digest`` on two *str* raises
    # ``TypeError`` when either side is non-ASCII, which used to escape as a 500
    # for a 64-character header full of non-ASCII.  Comparing encoded bytes is
    # also the only form that is constant-time for every input.
    if not candidate.isascii():
        return False
    # compare_digest, not ==: a byte-by-byte comparison leaks the length of the
    # matching prefix, which is enough to forge a digest one nibble at a time.
    return hmac.compare_digest(expected.encode("ascii"), candidate.encode("ascii"))


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
    #: Producer idempotency token, checked by ``AgentQueue.enqueue`` against the
    #: active tasks of the same ``(repo_id, kind)``.  Empty means no dedup.
    dedup_key: str = ""

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
    dedup_key: str = "",
) -> QueuedAction:
    """Queue one agent task, degrading to a logged no-op when the queue is absent.

    The call shape is S0's::

        queue.enqueue(repo_id, kind=..., payload=dict, priority=int, dedup_key=...)

    A ``0`` return is the queue's "suppressed" sentinel (an active duplicate, or
    the repository at its in-flight ceiling): the action is reported as *not*
    delivered, with the reason, so a storm is visible instead of looking queued.
    """
    action = QueuedAction(kind=kind, repo_id=repo_id, priority=priority,
                          payload=payload, dedup_key=dedup_key)
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
        action.task = sender(
            repo_id, kind=kind, payload=payload, priority=priority,
            dedup_key=dedup_key,
        )
        if action.task:
            action.delivered = True
        else:
            action.reason = "重复投递或该仓库在途任务已达上限，已抑制（未写入队列）"
            logger.info("webhook suppressed %s for repo %s: %s",
                        kind, repo_id, action.reason)
    except Exception as exc:  # noqa: BLE001 - a queue fault must not 500 a webhook
        action.reason = f"{type(exc).__name__}: {exc}"
        logger.exception("webhook could not queue %s for repo %s", kind, repo_id)
    return action


# ── Policy ───────────────────────────────────────────────────────────

def _repo_curator_policy(repo: Any) -> tuple[str, int]:
    """``(mode, min_interval_seconds)`` for the curator trigger, from *repo*.

    Same source as :func:`_repo_policy_reader`: the import pipeline reads the
    repository's ``.agent/review-policy.yml`` once and caches the trigger fields
    on the row.  The webhook must never read the API host's own checkout — that
    is a different repository's policy, and it made a repository's
    ``curator: off`` unreachable.  ``None`` is "not read yet" and yields the
    documented default (``bootstrap``, one hour).
    """
    mode = str(getattr(repo, "curator", None) or CURATOR_BOOTSTRAP)
    if mode not in CURATOR_MODES:
        mode = CURATOR_BOOTSTRAP
    interval = getattr(repo, "curator_min_interval_seconds", None)
    return mode, int(interval if interval is not None else DEFAULT_CURATOR_MIN_INTERVAL_SECONDS)


def curator_enqueue_allowed(
    session: Any,
    repo_id: int,
    *,
    mode: str = CURATOR_BOOTSTRAP,
    min_interval_seconds: int = 0,
) -> tuple[bool, str]:
    """The DB-backed dedup/rate-limit verdict for a curator proposal.

    Fails *closed*: if the gate cannot be evaluated we do not enqueue, because
    the failure mode of "too many proposal PRs" is worse than "a proposal was
    skipped and a later push will ask again".
    """
    try:
        from services.check_store import curator_should_enqueue

        return curator_should_enqueue(
            session, repo_id, mode=mode, min_interval_seconds=min_interval_seconds
        )
    except Exception as exc:  # noqa: BLE001 - never 500 a webhook
        logger.warning("curator enqueue gate unavailable for repo %s: %s", repo_id, exc)
        return False, f"curator gate unavailable: {exc}"


def plan_actions(
    event: WebhookEvent,
    *,
    repo_id: int,
    auto_review: bool | None = None,
    policy_reader: Callable[[], bool] | None = None,
    curator_mode: str | None = None,
    curator_allowed: bool = True,
) -> list[QueuedAction]:
    """Decide what *event* should queue.  Pure: no I/O, no database.

    That purity is what lets the offline gate assert the §5.4 table exactly —
    a push to the default branch with ``auto_review`` on queues a review, a push
    to a side branch queues nothing, an ``agent``-labelled issue queues a fix,
    and anything else queues nothing.  ``auto_review`` (when given) overrides
    the policy lookup, which is how the gate drives both branches of that
    decision with no files on disk.

    ``curator_mode`` (``DESIGN-ai-checks.md`` §B) additionally queues a
    ``checks`` proposal on a default-branch push when it is ``bootstrap`` or
    ``auto`` **and** ``curator_allowed`` is true.  The mode is independent of
    ``auto_review``: a repository that does not want automatic reviews may still
    want its check suite bootstrapped.  ``curator_allowed`` carries the
    enqueue-side dedup/rate-limit verdict, which needs the database and so is
    computed by the caller and passed in — keeping this function pure.
    """
    actions: list[QueuedAction] = []

    if event.kind == "push":
        if not event.is_default_branch:
            return actions
        # A branch deletion carries no tip to review: ``after`` is all zeroes and
        # the commit list is empty.  Queueing here would fail the task later.
        if event.raw.get("deleted"):
            return actions
        head_sha = _head_sha(event)
        if not head_sha:
            logger.info(
                "push on %s carried no usable commit sha; nothing queued", event.branch,
            )
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
        if allowed:
            actions.append(QueuedAction(
                kind="review",
                repo_id=repo_id,
                priority=PRIORITY_REVIEW,
                payload={
                    "commit_sha": head_sha,
                    "branch": event.branch,
                    "trigger": "push",
                    "sender": event.sender,
                },
                reason="push to default branch with auto_review",
                dedup_key=head_sha,
            ))
        if curator_mode in (CURATOR_BOOTSTRAP, CURATOR_AUTO) and curator_allowed:
            actions.append(QueuedAction(
                kind="checks",
                repo_id=repo_id,
                priority=PRIORITY_CURATOR,
                payload={
                    "commit_sha": head_sha,
                    "branch": event.branch,
                    "trigger": "push",
                    "suite_scope": curator_mode,
                    "sender": event.sender,
                },
                reason=f"curator={curator_mode} on push to default branch",
                dedup_key=f"checks:{curator_mode}:{head_sha}",
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
            dedup_key=f"issue:{event.issue_number}" if event.issue_number else "",
        ))
        return actions

    if event.kind == "pull_request" and event.merged:
        # The doc write-back is *synchronous*: the view calls
        # :func:`backfill_pr_url` against the findings table directly (see the
        # route below).  Queueing a ``backfill`` task here was pure waste — no
        # handler implements that kind, so every merged PR produced a task that
        # burned three attempts and landed in ``dead``.  The event is still
        # ``handled`` (the write-back ran); nothing is queued.
        logger.info(
            "merged pull request %s: doc write-back runs inline, nothing queued",
            event.pr_number,
        )
    return actions


def _repo_policy_reader(repo: Any) -> Callable[[], bool]:
    """``auto_review`` for *repo*, from the value the import pipeline cached.

    The API host has no checkout, so ``read_policy_auto_review()`` with no
    ``repo_root`` could only ever answer ``True``.  The import/sync pipeline reads
    ``.agent/review-policy.yml`` from the Forgejo contents API and stores the
    result on the row (``repos.auto_review``); ``None`` means "never read", which
    keeps the documented default of reviewing every default-branch push.
    """
    def read() -> bool:
        value = getattr(repo, "auto_review", None)
        return True if value is None else bool(value)

    return read


def _head_sha(event: WebhookEvent) -> str:
    """The pushed tip: ``after`` first, then the payload's commit list.

    ``commits`` is truncated by the forge and empty for a deletion; ``after`` is
    the field that always names the new tip.
    """
    after = str(event.raw.get("after") or "").strip()
    if after and set(after) != {"0"}:
        return after
    for commit in reversed(event.commits):
        sha = commit.get("id") or commit.get("sha")
        if sha:
            return str(sha)
    return ""


# ── Repo resolution ──────────────────────────────────────────────────

def resolve_repo(session: Any, event: WebhookEvent) -> Any | None:
    """Find the one ``repos`` row a webhook is about, or ``None``.

    ``forgejo_repo`` (the Forgejo-side full name) is preferred, ``slug`` is the
    fallback — for a repository created by ``POST /api/v1/repos`` the two differ
    (Forgejo gets ``owner__name``), and resolving either way keeps the webhook
    useful while the naming policy is still being tuned.

    An ambiguous match is refused, not guessed: writing findings or queueing a
    task against an arbitrarily-chosen row is worse than dropping the event, and
    the error log names the conflict so it can be fixed.  ``None`` makes the
    route answer "unknown repository" (200, no side effects).
    """
    from models.agent_hub import Repo

    candidates = [value for value in (event.forgejo_repo, event.repo_slug) if value]
    if not candidates:
        return None
    found: dict[int, Any] = {}
    for column in ("forgejo_repo", "slug"):
        attr = getattr(Repo, column, None)
        if attr is None:
            continue
        for value in candidates:
            for row in session.query(Repo).filter(attr == value).limit(2).all():
                found[row.id] = row
    if not found:
        return None
    if len(found) > 1:
        logger.error(
            "webhook 的仓库 %r 同时匹配 %d 个 repos 行（%s）；拒绝猜测，事件未处理，"
            "请先修复命名冲突",
            candidates, len(found),
            ", ".join(str(getattr(row, "slug", row.id)) for row in found.values()),
        )
        return None
    return next(iter(found.values()))


def backfill_pr_url(session: Any, repo_id: int, number: int, url: str) -> int:
    """Record ``url`` on the findings the task that opened this PR produced.

    S3 owns the finding state machine; this is the one write §5.4 asks the
    webhook to make.  It is defensive on purpose: a schema S3 has not finished
    shipping must not turn a webhook into a 500.

    The schema has no direct PR↔finding column, so the link is reconstructed:
    ``agent_tasks.pr_url`` names the task that opened the PR,
    ``review_runs.agent_task_id`` names the runs that task produced, and a
    finding belongs to the PR when one of those runs saw it.  A merged PR this
    platform did not open has no such task, and then **nothing** is written —
    the repository-wide write over ``pr_url IS NULL`` used to let a single
    payload stamp every unrelated finding, with no way back.
    """
    if not number or not url:
        return 0
    try:
        from models.agent_hub import AgentTask, Finding, ReviewRun  # noqa: PLC0415
    except ImportError:
        logger.info("agent-hub tables not present yet; skipping pr_url backfill")
        return 0
    if not hasattr(Finding, "pr_url"):
        return 0
    updated = 0
    try:
        from sqlalchemy import or_  # noqa: PLC0415 - one query, one dialect

        task = (
            session.query(AgentTask)
            .filter(AgentTask.repo_id == repo_id, AgentTask.pr_url == url)
            .first()
        )
        if task is None:
            logger.info("no agent task records PR %s for repo %s; nothing linked",
                        url, repo_id)
            return 0
        run_ids = [
            row[0]
            for row in session.query(ReviewRun.id)
            .filter(ReviewRun.agent_task_id == task.id)
            .all()
        ]
        if not run_ids:
            return 0
        rows = (
            session.query(Finding)
            .filter(
                Finding.repo_id == repo_id,
                Finding.pr_url.is_(None),
                or_(
                    Finding.first_seen_run_id.in_(run_ids),
                    Finding.last_seen_run_id.in_(run_ids),
                ),
            )
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


def _duplicate_response(event: WebhookEvent):
    """The ``200`` body for a delivery whose authenticated key is already known.

    A duplicate is not an error: the forge resends after a 2xx it did not see,
    and a ``4xx`` would make it retry forever.
    """
    return jsonify({
        "ok": True,
        "event": event.kind,
        "action": event.action,
        "repo": event.forgejo_repo or event.repo_slug,
        "slug": None,
        "handled": False,
        "queued": [],
        "findings_updated": 0,
        "duplicate": True,
        "note": "重复投递：该请求体已处理过，未重复入队",
    })


# ── Delivery idempotency ─────────────────────────────────────────────
# The HMAC proves *who* signed a payload, not that it is new: Forgejo retries a
# delivery it did not get a 2xx for, and anyone holding the shared secret can
# replay a captured body.  The queue's own ``dedup_key`` only collapses a *still
# active* task, so a replay after the first task finished would pay for the
# review again.  Replay detection therefore lives here, keyed on the one piece
# of the request the signature actually covers — the raw body.

def delivery_id() -> str:
    """The forge's delivery header, or ``""`` when it sent none.

    **Unauthenticated hint only.**  ``X-Forgejo-Delivery`` and its siblings sit
    outside the HMAC, so a replay can drop or rewrite them at will; this value
    is never used to decide whether a delivery is new (see
    :func:`delivery_key`), only logged for triage.
    """
    for name in DELIVERY_HEADERS:
        value = (request.headers.get(name) or "").strip()
        if value:
            return value[:128]
    return ""


def delivery_key(body: bytes) -> str:
    """The authenticated idempotency key for one webhook body.

    A SHA-256 of the raw request body — the exact bytes the HMAC is computed
    over — so a captured body presents the same key however the delivery
    headers are edited.  The event header is deliberately *not* mixed in: it is
    unauthenticated too, and folding it into the key would hand back the same
    replay bypass.  The body already carries the event identity (a push and an
    issues payload cannot be byte-identical and still mean different events).

    The body is forge-published JSON, so decoding is exact; a body that does not
    decode is rejected with ``400`` before this is reached.
    :func:`services.digest.sha256_text` is this project's one SHA-256
    implementation.
    """
    return sha256_text(body.decode("utf-8"))


def delivery_seen(session: Any, key: str) -> bool:
    """Whether *key* (an authenticated body digest) was already accepted.

    This is the cheap pre-check; :func:`claim_delivery` is the authoritative one
    and closes the check-then-act race.  Fail-open on a database that predates
    the ``webhook_deliveries`` table (or any lookup error): idempotency is
    defence in depth, and it must never turn a webhook into a 500 and make
    Forgejo retry forever.  The rollback matters — without it a failed lookup
    leaves the session unusable and every later query 500s on exactly that
    legacy database.
    """
    if not key:
        return False
    try:
        from models.agent_hub import WebhookDelivery

        return (
            session.query(WebhookDelivery).filter_by(delivery_key=key).first()
            is not None
        )
    except Exception as exc:  # noqa: BLE001 - never 500 a webhook
        session.rollback()
        logger.warning("could not check webhook delivery key %s: %s", key[:16], exc)
        return False


def claim_delivery(
    session: Any,
    key: str,
    *,
    repo_id: int | None,
    event: str,
) -> bool:
    """Reserve *key* as accepted, before any side effect; ``False`` = duplicate.

    The row is inserted and **committed first**, and the unique constraint on the
    key is what makes concurrent duplicates safe: two in-flight copies of the
    same signed body both reach this insert, the database lets exactly one
    commit, and the loser sees ``IntegrityError`` and is answered as a replay
    without queueing anything.  The old check-then-remember-afterwards pair could
    not offer that — both requests passed the check before either recorded.

    Crash semantics are deliberately **at-most-once**: the delivery is considered
    processed the moment the row commits, so a crash between the commit and the
    side effects means Forgejo's retry is answered as a duplicate and those side
    effects do not run.  That is the right trade here because the side effects
    are not transactional with this table (``AgentQueue`` writes through its own
    engine) and a duplicate *paid* review / fix PR is the damage this guard
    exists to prevent.  A queue fault is already logged and answered ``200`` by
    :func:`enqueue_task`, so a retry never re-ran it anyway.

    Fail-open for a database that predates the table or an unreachable database:
    anything other than an integrity violation is logged and treated as "not
    seen", so a legacy deployment keeps answering ``200`` instead of ``500``.  A
    genuine integrity violation is *not* fail-open — the request proceeds to no
    side effects.
    """
    if not key:
        return True
    from sqlalchemy.exc import IntegrityError  # noqa: PLC0415 - one dialect seam

    try:
        from models.agent_hub import WebhookDelivery

        session.add(WebhookDelivery(delivery_key=key, repo_id=repo_id, event=event))
        session.commit()
        return True
    except IntegrityError:
        # The unique key lost the race: a duplicate, handled deliberately rather
        # than swallowed.  This is the *expected* path for a replay.
        session.rollback()
        logger.info(
            "webhook delivery key %s already recorded; ignoring replay", key[:16],
        )
        return False
    except Exception as exc:  # noqa: BLE001 - never 500 a webhook
        session.rollback()
        logger.warning("could not record webhook delivery key %s: %s", key[:16], exc)
        return True


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
        "`fix` task; a **merged** `pull_request` event records the PR URL against "
        "the findings that have none yet. Unknown events and events for a "
        "repository this platform has not imported answer `200` with "
        "`handled: false`, so Forgejo does not retry them forever."
    ),
    tags=["Repositories"],
    # Anonymous on purpose: the HMAC is the credential.  `security=[]` is what
    # tells scripts/check_auth_guards.py this is a decision, not an omission.
    security=[],
    request_body={"required": True, "content": json_body()},
    responses={
        "200": ok("The event was accepted", _WEBHOOK_SCHEMA),
        **errors("400", "401", "500"),
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
    key = delivery_key(body)
    hint = delivery_id()
    session = _session()
    if delivery_seen(session, key):
        # The HMAC proves who signed it, not that it is new: a replay (or a
        # Forgejo retry after a 2xx we failed to return) must not enqueue a
        # second review.  The key is the signed body's digest, so stripping the
        # (unsigned) delivery header does not make the replay look new.  Answer
        # 200 so the forge stops retrying either way.
        logger.info(
            "webhook delivery body %s already accepted (header hint %r); ignoring replay",
            key[:16], hint,
        )
        return _duplicate_response(event)
    repo = resolve_repo(session, event)
    if repo is None:
        # 200, not 404: an unimported repository is a permanent condition, and a
        # 4xx makes Forgejo retry the delivery forever (the same reason unknown
        # events answer 200).
        logger.warning("webhook for unknown repository %r (%s)",
                       event.forgejo_repo or event.repo_slug, event.kind)
        return jsonify({
            "ok": True,
            "event": event.kind,
            "action": event.action,
            "repo": event.forgejo_repo or event.repo_slug,
            "slug": None,
            "handled": False,
            "queued": [],
            "findings_updated": 0,
            "note": (
                f"平台没有镜像仓库 {event.forgejo_repo or event.repo_slug!r}；"
                "先用 POST /api/v1/repos/import 导入（返回 200 以免 Forgejo 反复重试）"
            ),
        })

    # Claim the delivery *before* the side effects.  The check above can race
    # (two in-flight copies both pass it); this insert's unique constraint is
    # what actually serialises them.  If it loses, answer as a duplicate instead
    # of queueing a second paid task.
    if not claim_delivery(session, key, repo_id=repo.id, event=event.kind):
        logger.info(
            "webhook delivery body %s claimed concurrently (header hint %r); ignoring replay",
            key[:16], hint,
        )
        return _duplicate_response(event)

    curator_mode, curator_interval = _repo_curator_policy(repo)
    if event.kind == "push" and event.is_default_branch:
        curator_allowed, curator_reason = curator_enqueue_allowed(
            session, repo.id, mode=curator_mode, min_interval_seconds=curator_interval,
        )
    else:
        curator_allowed, curator_reason = True, ""

    actions = plan_actions(
        event,
        repo_id=repo.id,
        policy_reader=_repo_policy_reader(repo),
        curator_mode=curator_mode,
        curator_allowed=curator_allowed,
    )
    if not curator_allowed and curator_reason:
        logger.info(
            "webhook curator proposal suppressed for %s: %s", repo.slug, curator_reason,
        )
    queued: list[QueuedAction] = []
    for action in actions:
        queued.append(enqueue_task(
            kind=action.kind,
            repo_id=action.repo_id,
            payload=action.payload,
            priority=action.priority,
            dedup_key=action.dedup_key,
        ))

    updated = 0
    if event.kind == "pull_request" and event.merged:
        # Only a merged PR is evidence of a fix; an opened or closed-unmerged PR
        # must not be stamped onto the repository's findings.
        updated = backfill_pr_url(
            session, repo.id, event.pr_number or 0, event.pr_url,
        )

    # The delivery was claimed before these side effects (see claim_delivery):
    # a crash here leaves it recorded, so a Forgejo retry is a no-op rather than
    # a second paid task.  That at-most-once choice is documented there.

    handled = bool(queued) or (event.kind == "pull_request" and event.merged)
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
    "DELIVERY_HEADERS",
    "EVENT_HEADERS",
    "PRIORITY_CURATOR",
    "SIGNATURE_HEADERS",
    "QueuedAction",
    "WebhookEvent",
    "backfill_pr_url",
    "claim_delivery",
    "compute_signature",
    "curator_enqueue_allowed",
    "delivery_id",
    "delivery_key",
    "delivery_seen",
    "enqueue_task",
    "parse_event",
    "plan_actions",
    "repo_webhook_bp",
    "resolve_repo",
    "signature_header",
    "verify_signature",
]
