"""Findings and review policy routes — §5.3's decision surface.

``GET  /api/v1/findings``              filtered debt board (`finding:read`)
``GET  /api/v1/findings/<id>``         one finding plus its transition history
``POST /api/v1/findings/<id>/decide``  §6.1 transition (`finding:decide`)
``POST /api/v1/findings/<id>/fix``     enqueue a `fix` AgentTask (`agent:run`)
``GET  /api/v1/policies/<slug>``       the repo's policy + `policy_hash`
``PUT  /api/v1/policies/<slug>``       replace it (`policy:write`)

The module is bindings only: the state machine, fingerprinting and activation
rules live in :mod:`services.findings`, the file format in
:mod:`services.review_policy`.  What is decided here is HTTP shape — which
failure is a 404, which is the §5.3 ``409 finding_decision_invalid``, and which
permission each route demands.

The permission points come from ``auth.agent_hub_permissions`` (re-exported by
``auth.permissions``); every route carries its own ``@require_permission``
guard, so registering this blueprint cannot accidentally expose it.

Request binding
---------------
Three views take their input as view parameters instead of reading ``request``
by hand: ``list_findings`` binds its filter query (``query: FindingListQuery``)
and ``decide_finding`` / ``put_policy`` bind their JSON bodies.  On each of them
``@validate_request()`` sits *below* the guard, so an unauthorized request is
answered ``401``/``403`` and never by the binder —
``scripts/check_request_binding.py`` pins that shape and order.  The path
variables stay plain Flask arguments: ``<int:finding_id>`` is Flask's own
converter, and ``<path:slug>`` is validated by :func:`_checked_slug`, whose
message is what the console shows for a traversal attempt.
"""

from __future__ import annotations

import logging
import re

from flask_openapi3 import APIBlueprint, validate_request

from auth.decorators import current_principal, require_permission
from auth.permissions import AGENT_RUN, FINDING_DECIDE, FINDING_READ, POLICY_WRITE
from errors import BadRequestError, ForbiddenError
from openapi import api_operation, errors, json_body, ok
from schemas import FindingDecisionRequest, FindingListQuery, ReviewPolicyRequest
from services import findings as findings_service
from services import review_policy

# `doc_ui=False` for the same reason `app.py` passes it: the request-binding half
# of flask-openapi3 is all this module uses, and the library's own document is
# never served — `/openapi.json` is built from `@api_operation` in `openapi/`.
findings_bp = APIBlueprint("findings", __name__, doc_ui=False)
logger = logging.getLogger("cpypiserver.findings_routes")

#: A repository slug (``<owner>/<name>``) as it may appear in a URL.  Flask has
#: no converter for "one or more segments, but never ``..``", so the pattern is
#: enforced here: the policy route is the only catch-all in this blueprint and a
#: traversal must not reach the filesystem through it.
_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*(/[A-Za-z0-9][A-Za-z0-9._-]*)*$")

_PROTECTED = ("401", "403", "404", "500")


def _errors(*extra: str) -> dict:
    """Standard error responses plus *extra*, in a stable (sorted) order."""
    return errors(*sorted({*_PROTECTED, *extra}))


def _session():
    from extensions.database import Session

    return Session()


def _actor() -> str:
    return (current_principal() or {}).get("sub") or "unknown"


def _int_arg(raw: str | None, name: str, default: int) -> int:
    """A bound query value read the way this module has always read it.

    The raw string comes from the bound query model, but the *decision* stays
    here: an absent **or empty** value means the default, and anything that is
    not an integer is this route's own prose ``400``.  A typed model field would
    turn ``?limit=`` into the binding's error instead of the default.
    """
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise BadRequestError(f"{name} must be an integer") from exc


def _checked_slug(slug: str) -> str:
    """Reject a slug that is not ``<owner>/<name>`` before it reaches a path.

    ``<path:slug>`` exists only because a slug may hold a ``/``; everything else
    about it (empty segments, ``..``, leading ``/``) is refused here.
    """
    if not _SLUG_RE.match(slug or ""):
        raise BadRequestError(
            "invalid repository slug; expected '<owner>/<name>'"
        )
    return slug


# ── OpenAPI fragments ────────────────────────────────────────────────
# Inline *response* schemas, exactly as `routes/hub.py` does it: they describe
# shapes this slice owns and are validated here rather than in the shared
# registry.  The request models below do live in `schemas.py`, like every other
# bound view's, so one file holds the whole wire contract.

_FINDING_SCHEMA = {
    "type": "object",
    "properties": {
        "id": {"type": "integer"},
        "repo_id": {"type": "integer"},
        "fingerprint": {
            "type": "string",
            "description": "Stable identity (I1) — sha256 over rule/file/symbol/context_key.",
        },
        "rule_id": {"type": "string"},
        "level": {"type": "string", "enum": list(findings_service.FINDING_LEVEL)},
        "severity": {"type": "string", "enum": ["critical", "high", "medium", "low"]},
        "status": {"type": "string", "enum": list(findings_service.FINDING_STATUS)},
        "file_path": {"type": "string"},
        "symbol": {"type": "string"},
        "line_hint": {"type": ["integer", "null"]},
        "title": {"type": "string"},
        "detail": {"type": "string"},
        "first_seen_run_id": {"type": ["integer", "null"]},
        "last_seen_run_id": {"type": ["integer", "null"]},
        "seen_count": {"type": "integer"},
        "owner": {"type": ["string", "null"]},
        "due": {"type": ["string", "null"], "description": "ISO date; I2 requires it on wontfix/acknowledge."},
        "decided_by": {"type": ["string", "null"]},
        "decided_at": {"type": ["string", "null"]},
        "pr_url": {"type": ["string", "null"]},
        "created_at": {"type": ["string", "null"]},
        "updated_at": {"type": ["string", "null"]},
    },
}

_FINDING_EVENT_SCHEMA = {
    "type": "object",
    "properties": {
        "id": {"type": "integer"},
        "finding_id": {"type": "integer"},
        "at": {"type": ["string", "null"]},
        "actor": {"type": "string"},
        "from_status": {"type": ["string", "null"]},
        "to_status": {"type": "string"},
        "reason": {"type": ["string", "null"]},
        "run_id": {"type": ["integer", "null"]},
    },
}

_FINDING_DETAIL_SCHEMA = {
    "type": "object",
    "properties": {
        "finding": _FINDING_SCHEMA,
        "events": {"type": "array", "items": _FINDING_EVENT_SCHEMA},
    },
}

_FINDING_LIST_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {"type": "array", "items": _FINDING_SCHEMA},
        "total": {"type": "integer", "description": "Rows matching the filter, before paging"},
        "limit": {"type": "integer"},
        "offset": {"type": "integer"},
    },
}

_DECIDE_BODY = {
    "required": True,
    "content": json_body(
        {
            "type": "object",
            "required": ["action"],
            "properties": {
                "action": {
                    "type": "string",
                    "enum": list(findings_service.DECISION_ACTIONS),
                    "description": (
                        "`fix`/`fixed` = it is (or will be) repaired; "
                        "`acknowledge`/`wontfix` = deferred to `owner`+`due` "
                        "(debt only — I2/I3); `false_positive` = a confirmed "
                        "`fixed` that also feeds the §6.3 rule review."
                    ),
                },
                "owner": {"type": "string", "description": "Required for acknowledge/wontfix"},
                "due": {"type": "string", "description": "ISO date, required for acknowledge/wontfix, must be in the future"},
                "reason": {"type": "string"},
                "confirmed_by": {"type": "string", "description": "Second person confirming a false positive"},
            },
        }
    ),
}

_DECIDE_RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "finding": _FINDING_SCHEMA,
        "from_status": {"type": "string"},
        "to_status": {"type": "string"},
        "reason": {"type": ["string", "null"]},
    },
}

_FIX_RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "finding_id": {"type": "integer"},
        "task_id": {"type": ["integer", "null"]},
        "kind": {"type": "string"},
        "queued": {"type": "boolean"},
    },
}

_POLICY_SCHEMA = {
    "type": "object",
    "properties": {
        "slug": {"type": "string"},
        "version": {"type": "integer"},
        "defaults": {"type": "object"},
        "rules": {"type": "array", "items": {"type": "object"}},
        "exceptions": {"type": "array", "items": {"type": "object"}},
        "escalation": {"type": "object"},
        "policy_source": {
            "type": "string",
            "enum": [review_policy.SOURCE_FILE, review_policy.SOURCE_BUILTIN],
            "description": "`builtin-default` means no `.agent/review-policy.yml` exists",
        },
        "policy_hash": {"type": "string"},
        "warnings": {"type": "array", "items": {"type": "string"}},
        "readonly": {"type": "boolean"},
        "path": {"type": ["string", "null"]},
    },
}

_SLUG_PARAM = {
    "name": "slug",
    "in": "path",
    "required": True,
    "description": "Repository slug (`<owner>/<name>`) or id; the filename is fixed",
    "schema": {"type": "string"},
}

_ID_PARAM = {
    "name": "id",
    "in": "path",
    "required": True,
    "description": "Numeric finding id",
    "schema": {"type": "integer"},
}


def _repo_root_for(slug: str) -> str | None:
    """Filesystem root of the repository named *slug*, when it is a local one.

    Imported repositories live in Forgejo, not on this filesystem, so ``None``
    is a normal answer: the caller then gets the platform default policy rather
    than an error.
    """
    from config import settings

    local = getattr(settings.hub, "repo_root", None) or getattr(settings, "repos_root", None)
    if not local:
        return None
    return f"{str(local).rstrip('/')}/{slug}"


# ══════════════════════════════════════════════════════════════════════
#  Findings
# ══════════════════════════════════════════════════════════════════════

@findings_bp.get("/api/v1/findings")
@require_permission(FINDING_READ)
@validate_request()
@api_operation(
    summary="List findings",
    description=(
        "The debt board: every finding, newest first, with the filters the "
        "console offers. `repo` accepts a numeric id or a `<owner>/<name>` "
        "slug.\n\n"
        "`status` is one of `open`/`acknowledged`/`wontfix`/`fixed`/`stale`; "
        "`level` is `blocking` or `debt`. A finding keeps its `fingerprint` "
        "across runs (I1), so the same row moves through the state machine "
        "instead of being reported again (§12.1)."
    ),
    tags=["Findings"],
    parameters=[
        {"name": "repo", "in": "query", "schema": {"type": "string"}},
        {"name": "status", "in": "query", "schema": {"type": "string", "enum": list(findings_service.FINDING_STATUS)}},
        {"name": "level", "in": "query", "schema": {"type": "string", "enum": list(findings_service.FINDING_LEVEL)}},
        {"name": "rule", "in": "query", "schema": {"type": "string"}},
        {"name": "owner", "in": "query", "schema": {"type": "string"}},
        {"name": "limit", "in": "query", "schema": {"type": "integer", "default": 200}},
        {"name": "offset", "in": "query", "schema": {"type": "integer", "default": 0}},
    ],
    responses={"200": ok("Findings", _FINDING_LIST_SCHEMA), **_errors("400")},
)
def list_findings(query: FindingListQuery):
    # `query` is the bound filter string — see `schemas.FindingListQuery`: the
    # values stay raw so `_int_arg` keeps answering an empty `?limit=` with the
    # default and a malformed one with this route's own prose 400.
    limit = _int_arg(query.limit, "limit", 200)
    offset = _int_arg(query.offset, "offset", 0)
    payload = findings_service.list_findings(
        _session(),
        repo=query.repo or None,
        status=query.status or None,
        level=query.level or None,
        rule=query.rule or None,
        owner=query.owner or None,
        limit=limit,
        offset=offset,
    )
    return {"items": payload["items"], "total": payload["total"], "limit": limit, "offset": offset}


@findings_bp.route("/api/v1/findings/<int:finding_id>")
@require_permission(FINDING_READ)
@api_operation(
    summary="Read one finding",
    description=(
        "One finding plus its `finding_events` history — the audit trail of "
        "every §6.1 transition, who made it and why. That history is what makes "
        "a `wontfix` reviewable rather than a silent deletion (§12.1)."
    ),
    tags=["Findings"],
    parameters=[{**_ID_PARAM, "name": "finding_id"}],
    responses={"200": ok("The finding and its history", _FINDING_DETAIL_SCHEMA), **_errors()},
)
def get_finding(finding_id: int):
    session = _session()
    finding = findings_service.get_finding(session, finding_id)
    return {
        "finding": findings_service.serialize(finding),
        "events": findings_service.events_for(session, finding_id),
    }


@findings_bp.post("/api/v1/findings/<int:finding_id>/decide")
@require_permission(FINDING_DECIDE)
@validate_request()
@api_operation(
    summary="Decide a finding",
    description=(
        "Apply one transition from the §6.1 table.\n\n"
        "Hard rejections (HTTP 409 `finding_decision_invalid`):\n"
        "* **I2** — `acknowledge` and `wontfix` both require `owner` **and** "
        "`due`; an open-ended deferral is how a finding disappears forever.\n"
        "* **I3** — a `blocking` finding accepts neither. It can only be "
        "`fixed` (really repaired) or `false_positive` (a confirmed rule "
        "defect, which §6.3 then counts against the rule).\n"
        "* a `false_positive` needs `confirmed_by` from a second account.\n\n"
        "Every accepted decision writes a `FindingEvent`."
    ),
    tags=["Findings"],
    parameters=[{**_ID_PARAM, "name": "finding_id"}],
    request_body=_DECIDE_BODY,
    responses={
        "200": ok("The finding after the transition", _DECIDE_RESULT_SCHEMA),
        **_errors("400", "409"),
    },
)
def decide_finding(finding_id: int, body: FindingDecisionRequest):
    # `body` is the bound JSON body — see `schemas.FindingDecisionRequest`: every
    # field is optional and `action` is a plain string, so the §6.1 table's own
    # 409 stays the answer for a missing or unknown action, and I2/I3 stay in
    # `services.findings.decide`.
    try:
        due = findings_service.coerce_due(body.due)
    except ValueError as exc:
        raise BadRequestError(str(exc)) from exc
    try:
        result = findings_service.decide(
            finding_id,
            str(body.action or ""),
            _actor(),
            owner=body.owner,
            due=due,
            reason=body.reason,
            session=_session(),
            confirmed_by=body.confirmed_by,
        )
    except findings_service.DecisionInvalidError as exc:
        return {"error": exc.code, "message": exc.message}, 409
    except findings_service.FindingNotFoundError as exc:
        return {"error": "not_found", "message": str(exc)}, 404
    return result


@findings_bp.route("/api/v1/findings/<int:finding_id>/fix", methods=["POST"])
@require_permission(AGENT_RUN)
@api_operation(
    summary="Queue a fix for a finding",
    description=(
        "Enqueues one `fix` agent task targeting this finding and returns its "
        "id; the actual repair is a branch + PR on `agent/*`, never a push to a "
        "protected branch (I4). Whether the agent may touch the rule at all is "
        "`policy.rules[<rule_id>].autofix` (§6.4), and §6.4's categories stay "
        "report-only even when a rule asks for more."
    ),
    tags=["Findings"],
    parameters=[{**_ID_PARAM, "name": "finding_id"}],
    responses={"200": ok("The queued task", _FIX_RESULT_SCHEMA), **_errors("400", "503")},
)
def fix_finding(finding_id: int):
    session = _session()
    finding = findings_service.get_finding(session, finding_id)
    payload = {
        "finding_id": int(finding.id),
        "repo_id": int(finding.repo_id),
        "rule_id": finding.rule_id,
        "file_path": finding.file_path,
        "symbol": finding.symbol,
        "fingerprint": finding.fingerprint,
        "agent_task_id": None,
    }
    run_id = getattr(finding, "last_seen_run_id", None)
    if run_id is not None:
        payload["review_run_id"] = int(run_id)
    task_id = _enqueue_fix(finding, payload)
    return {
        "finding_id": int(finding.id),
        "task_id": task_id,
        "kind": "fix",
        "queued": task_id is not None,
    }


def _enqueue_fix(finding, payload: dict) -> int | None:
    """Queue the fix task through S0's `services.agent_queue` (§9.1).

    ``AgentQueue.enqueue(repo_id, *, kind=…, payload=dict, priority=int)`` is the
    contract; the engine comes from the application when one is running, and is
    built from the same configuration otherwise, so this works inside a request
    and from a CLI-driven run.  The import is lazy so this blueprint still loads
    on a checkout where S0's queue has not landed.
    """
    from services import agent_queue

    try:
        from flask import current_app

        engine = current_app.extensions.get("db_engine")
    except RuntimeError:  # no application context (CLI / worker)
        engine = None
    if engine is None:
        engine = agent_queue.build_engine()

    queue = agent_queue.AgentQueue(engine)
    task_id = queue.enqueue(
        int(finding.repo_id),
        kind="fix",
        payload=payload,
        priority=0,
    )
    logger.info("finding %s: queued fix task %s", finding.id, task_id)
    return int(task_id) if task_id is not None else None


# ══════════════════════════════════════════════════════════════════════
#  Review policy
# ══════════════════════════════════════════════════════════════════════

def _resolve_slug(slug: str) -> str:
    """Validate + canonicalize the ``<path:slug>`` parameter."""
    return _checked_slug(slug.strip())


def _load_policy(slug: str):
    """The policy in force for *slug*, after checking the slug itself."""
    name = _resolve_slug(slug)
    root = _repo_root_for(name)
    try:
        if root is not None:
            return review_policy.load(root)
    except review_policy.PolicyValidationError as exc:
        raise BadRequestError("the policy is invalid") from exc
    except review_policy.PolicyDependencyError as exc:
        raise ForbiddenError(message=str(exc)) from exc
    return review_policy.load()


@findings_bp.route("/api/v1/policies/<path:slug>")
@require_permission(FINDING_READ)
@api_operation(
    summary="Read the review policy",
    description=(
        "The repository's `.agent/review-policy.yml`, parsed and validated, "
        "with `policy_hash` for `review_runs` comparability (§7.1).\n\n"
        "When the file does not exist the response carries the read-only "
        "built-in default and `policy_source: \"builtin-default\"` — everything "
        "blocking, nothing auto-fixable. `warnings` lists exceptions that name a "
        "rule this policy does not define (§7.3): those are reported, not "
        "fatal."
    ),
    tags=["Findings"],
    parameters=[_SLUG_PARAM],
    responses={"200": ok("The effective policy", _POLICY_SCHEMA), **_errors("400")},
)
def get_policy(slug: str):
    return {"slug": slug, **_load_policy(slug).as_payload()}


@findings_bp.put("/api/v1/policies/<path:slug>")
@require_permission(POLICY_WRITE)
@validate_request()
@api_operation(
    summary="Replace the review policy",
    description=(
        "Validates the submitted document and returns the normalized form with "
        "its new `policy_hash`. Requires `policy:write`.\n\n"
        "§7.3 is enforced: every `exceptions[*].due` is mandatory and must be "
        "strictly in the future, and duplicate rule ids are rejected. An "
        "exception naming an unknown rule is accepted but reported in "
        "`warnings`.\n\n"
        "The built-in default is read-only — a deployment with no policy file "
        "cannot be edited into existence through this endpoint. Writing the "
        "file is the repository's job (it is reviewed like any other change), "
        "so this endpoint returns the canonical YAML to commit."
    ),
    tags=["Findings"],
    parameters=[_SLUG_PARAM],
    request_body={
        "required": True,
        "content": json_body(
            {
                "type": "object",
                "description": "The §7.2 document (any subset; omitted sections take defaults)",
                "properties": {
                    "version": {"type": "integer"},
                    "defaults": {"type": "object"},
                    "rules": {"type": "array", "items": {"type": "object"}},
                    "exceptions": {"type": "array", "items": {"type": "object"}},
                    "escalation": {"type": "object"},
                },
            }
        ),
    },
    responses={"200": ok("The stored, normalized policy", _POLICY_SCHEMA), **_errors("400", "409")},
)
def put_policy(slug: str, body: ReviewPolicyRequest):
    # `body` is the bound JSON object, dumped with `exclude_unset=True` — see
    # `schemas.ReviewPolicyRequest`: `services.review_policy` distinguishes an
    # absent section (built-in default) from an explicit `null` (a validation
    # failure), so only the keys the caller actually sent may reach it.
    payload = body.model_dump(by_alias=True, exclude_unset=True)
    if not payload:
        raise BadRequestError("a policy document is required")
    name = _resolve_slug(slug)
    try:
        document = review_policy.parse_document(
            payload, source=review_policy.SOURCE_FILE, path=_repo_root_for(name)
        )
    except review_policy.PolicyValidationError as exc:
        raise BadRequestError(exc.message) from exc
    canonical = review_policy.dump(document)
    return {
        "slug": name,
        **document.as_payload(),
        "yaml": canonical,
    }


__all__ = ["findings_bp"]
