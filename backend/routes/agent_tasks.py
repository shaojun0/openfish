"""Agent task routes — enqueue, observe, retry and cancel a runtime task.

The JSON face of the agent runtime (§5.3).  It binds HTTP to the queue S0 owns
(``services/agent_queue.py``, table ``agent_tasks`` per §4.2) and holds no queue
logic of its own: per the layering rule a route is "JSON 契约 + 权限点绑定".
The one read it does itself is the ``repo`` filter/join, which the queue's
``enqueue``/``retry``/``cancel``/``get`` surface does not cover.

Endpoints
---------
``GET    /api/v1/agent/tasks``            list, ``?repo=&status=`` — ``agent:run``
``POST   /api/v1/agent/tasks``            enqueue ``{repo, kind, payload}`` — ``agent:run``
``POST   /api/v1/agent/tasks/<id>/retry`` requeue a failed/dead task — ``agent:admin``
``POST   /api/v1/agent/tasks/<id>/cancel`` stop a queued/leased/running task — ``agent:admin``
``GET    /api/v1/agent/tasks/<id>/log``   result + log references — ``agent:admin``

Paths are declared in full because the blueprint is registered at the bare
prefix (the hub/repos/findings convention).  The two permission points
(``agent:run`` / ``agent:admin``, §5.1) are imported from ``auth.permissions``
so the catalogue gate resolves them; ``anonymous`` holds neither.

Request binding
---------------
``list_agent_tasks`` binds its ``?repo=&status=&page=&page_size=`` query and
``create_agent_task`` its JSON body as view parameters instead of reading
``request`` by hand, which is what ``@validate_request()`` — placed *below* the
guard, so an unauthorized request is answered ``401``/``403`` and never by the
binder — installs.  Both models carry the raw values (a query string, a value of
any JSON type) because the decisions stay here: ``_int_arg`` clamps the paging,
``_coerce_int`` applies the queue's defaults, and the ``repo``/``kind``/``payload``
checks keep answering with the prose 400/404 this route has always sent.  The
retry, cancel and log views read no input beyond their path, so they stay plain
``@agent_tasks_bp.route`` views.
"""

from __future__ import annotations

import logging
from typing import Any

from flask import current_app, jsonify
from flask_openapi3 import APIBlueprint, validate_request
from sqlalchemy import func, select

from auth.decorators import current_principal, require_permission
from auth.permissions import AGENT_ADMIN, AGENT_RUN
from errors import BadRequestError, PypiError
from models.agent_hub import TASK_KIND, TASK_STATUS, AgentTask, Repo, ReviewRun
from openapi import api_operation, array_of, errors, json_body, ok, ref
from schemas import AgentTaskCreateRequest, AgentTaskListQuery
from services.agent_queue import AgentQueue

# `doc_ui=False` for the same reason `app.py` passes it: the request-binding half
# of flask-openapi3 is all this module uses, and the library's own document is
# never served — `/openapi.json` is built from `@api_operation` in `openapi/`.
agent_tasks_bp = APIBlueprint("agent_tasks", __name__, doc_ui=False)
logger = logging.getLogger("cpypiserver.agent_tasks")

#: Statuses every route here can return on top of the endpoint-specific ones.
_PROTECTED = ("401", "403", "404", "500", "503")

#: Page-size bounds for the list endpoint.
_MAX_PAGE_SIZE = 100


# ── Inline OpenAPI schemas ───────────────────────────────────────────
# The single-task document lives in `schemas.AgentTask`, the shared response
# registry, so the pydantic model and the published contract are one definition;
# the list wrapper is inline, exactly as `_REPO_LIST_SCHEMA` is in
# `routes/repos.py`.

_TASK_LIST_SCHEMA = {
    "type": "object",
    "properties": {
        "items": array_of("AgentTask"),
        "total": {"type": "integer"},
        "page": {"type": "integer"},
        "page_size": {"type": "integer"},
    },
}

_TASK_REQUEST_SCHEMA = {
    "type": "object",
    "required": ["repo", "kind"],
    "properties": {
        "repo": {"type": "string", "description": "仓库 slug（`<owner>/<name>`）"},
        "kind": {"type": "string", "enum": list(TASK_KIND)},
        "payload": {
            "type": "object",
            "description": "目标 sha / issue 号 / finding ids；默认为空对象",
        },
        "priority": {"type": "integer", "description": "越大越先领取，默认 0"},
        "max_attempts": {"type": "integer", "minimum": 1, "description": "默认 3"},
    },
}

_LOG_SCHEMA = {
    "type": "object",
    "properties": {
        "task_id": {"type": "integer"},
        "status": {"type": "string"},
        "result_ref": {
            "type": ["string", "null"],
            "description": "result.json 在工作目录 / 产物存储中的引用",
        },
        "log_ref": {"type": ["string", "null"], "description": "review_run.log_ref"},
    },
}


# ── Helpers ──────────────────────────────────────────────────────────

def _errors(*extra: str) -> dict:
    """Standard error responses plus *extra*, in a stable (sorted) order."""
    return errors(*sorted({*_PROTECTED, *extra}))


def _actor() -> str:
    """Who asked for this, for the queue's audit trail."""
    return (current_principal() or {}).get("sub") or "unknown"


def _queue() -> AgentQueue:
    """The process-wide queue, built on first use and cached on the app."""
    queue = current_app.extensions.get("agent_queue")
    if queue is None:
        engine = current_app.extensions.get("db_engine")
        if engine is None:
            raise PypiError("数据库引擎不可用", status_code=503)
        queue = AgentQueue(engine)
        current_app.extensions["agent_queue"] = queue
    return queue


def _repo_slug(session, repo_id: int) -> str | None:
    slug = session.execute(select(Repo.slug).where(Repo.id == repo_id)).scalar_one_or_none()
    return str(slug) if slug is not None else None


def _runner_id(queue: AgentQueue, task_id: int) -> int | None:
    """The task's ``repo_runners.id``; ``None`` for a legacy row.

    ``AgentQueue.get`` returns ``AgentTask.to_dict()``, the queue's own
    serializer, which does not carry the runner column; the route reads the one
    attribute it needs instead of widening that contract.
    """
    with queue.session() as session:
        value = session.execute(
            select(AgentTask.runner_id).where(AgentTask.id == task_id)
        ).scalar_one_or_none()
    return int(value) if value is not None else None


def _task_view(queue: AgentQueue, task_id: int, *, slug: str | None = None) -> dict:
    """One task as the API returns it, with the repo slug joined in."""
    task = queue.get(task_id)
    if task is None:
        raise PypiError("任务不存在", status_code=404)
    if slug is None:
        with queue.session() as session:
            slug = _repo_slug(session, int(task["repo_id"]))
    task["repo"] = slug
    task["runner_id"] = _runner_id(queue, task_id)
    return task


def _int_arg(raw: str | None, name: str, default: int) -> int:
    """A bound ``?page=``/``?page_size=`` value, read as this route always has.

    The raw string comes from the bound query model, but the *decision* stays
    here: an absent value means the default, and anything ``int()`` cannot read —
    an empty value included — is this route's own prose ``400``.  (``routes/
    repos.py`` reads an empty paging value as the default; this route never
    has.)  The clamps around the call are unchanged, so ``?page_size=5000``
    still answers ``100`` instead of the binder's ``400``.
    """
    if raw is None:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise BadRequestError(f"{name} 必须是整数") from None


# ── Endpoints ────────────────────────────────────────────────────────

@agent_tasks_bp.get("/api/v1/agent/tasks")
@require_permission(AGENT_RUN)
@validate_request()
@api_operation(
    summary="List agent tasks",
    description=(
        "Task history, newest first, filtered by `repo` (slug) and/or `status` "
        "(`" + " | ".join(TASK_STATUS) + "`).  Any signed-in caller with "
        "`agent:run` may list; managing an individual task needs `agent:admin`."
    ),
    tags=["Agent runtime"],
    parameters=[
        {"name": "repo", "in": "query", "schema": {"type": "string"}},
        {
            "name": "status",
            "in": "query",
            "schema": {"type": "string", "enum": list(TASK_STATUS)},
        },
        {"name": "page", "in": "query", "schema": {"type": "integer", "minimum": 1}},
        {
            "name": "page_size",
            "in": "query",
            "schema": {"type": "integer", "minimum": 1, "maximum": _MAX_PAGE_SIZE},
        },
    ],
    responses={"200": ok("A page of agent tasks", _TASK_LIST_SCHEMA), **_errors("400")},
)
def list_agent_tasks(query: AgentTaskListQuery):
    # `query` is the bound filter/paging string — see `schemas.AgentTaskListQuery`:
    # the values stay raw so `_int_arg` keeps clamping the paging and answering a
    # malformed value with this route's own prose 400.
    repo = (query.repo or "").strip() or None
    status = (query.status or "").strip() or None
    if status and status not in TASK_STATUS:
        raise BadRequestError(f"未知状态，可选：{' / '.join(TASK_STATUS)}")
    page = max(1, _int_arg(query.page, "page", 1))
    page_size = max(1, min(_int_arg(query.page_size, "page_size", 20), _MAX_PAGE_SIZE))

    queue = _queue()
    base = select(AgentTask, Repo.slug).join(Repo, AgentTask.repo_id == Repo.id)
    counting = select(func.count(AgentTask.id)).join(Repo, AgentTask.repo_id == Repo.id)
    if repo:
        base = base.where(Repo.slug == repo)
        counting = counting.where(Repo.slug == repo)
    if status:
        base = base.where(AgentTask.status == status)
        counting = counting.where(AgentTask.status == status)

    with queue.session() as session:
        total = int(session.execute(counting).scalar_one() or 0)
        rows = session.execute(
            base.order_by(AgentTask.created_at.desc(), AgentTask.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).all()

    items = []
    for task, slug in rows:
        item = task.to_dict(include_payload=False)
        item["repo"] = slug
        item["runner_id"] = getattr(task, "runner_id", None)
        items.append(item)
    return jsonify({"items": items, "total": total, "page": page, "page_size": page_size})


@agent_tasks_bp.post("/api/v1/agent/tasks")
@require_permission(AGENT_RUN)
@validate_request()
@api_operation(
    summary="Enqueue an agent task",
    description=(
        "Queues one `review`/`fix`/`import`/`backfill` task for a repo.  The "
        "runtime leases it, clones a read-only copy, runs the gates, reviews and "
        "emits a result document; a `fix` run may additionally push an "
        "`agent/*` branch and open a PR (never a protected branch — I4)."
    ),
    tags=["Agent runtime"],
    request_body={"required": True, "content": json_body(_TASK_REQUEST_SCHEMA)},
    responses={"201": ok("Task enqueued", ref("AgentTask")), **_errors("400", "409")},
)
def create_agent_task(body: AgentTaskCreateRequest):
    # `body` is the bound JSON body — see `schemas.AgentTaskCreateRequest`: the
    # three value fields are deliberately untyped, so every check below keeps
    # answering with the prose 400/404 this route has always sent.
    repo_slug = str(body.repo or "").strip()
    if not repo_slug:
        raise BadRequestError("缺少 repo")
    kind = str(body.kind or "").strip()
    if kind not in TASK_KIND:
        raise BadRequestError(f"未知任务类型，可选：{' / '.join(TASK_KIND)}")
    task_payload = body.payload or {}
    if not isinstance(task_payload, dict):
        raise BadRequestError("payload 必须是 JSON 对象")
    priority = _coerce_int(body.priority, "priority", 0)
    max_attempts = _coerce_int(body.max_attempts, "max_attempts", 3)
    if max_attempts < 1:
        raise BadRequestError("max_attempts 至少为 1")

    queue = _queue()
    with queue.session() as session:
        repo = session.execute(select(Repo).where(Repo.slug == repo_slug)).scalar_one_or_none()
    if repo is None:
        raise PypiError("仓库不存在", status_code=404)

    task_id = queue.enqueue(
        int(repo.id), kind=kind, payload=task_payload, priority=priority, max_attempts=max_attempts
    )
    if not task_id:
        # AgentQueue.enqueue returns 0 when the producer-side admission control
        # suppressed the row (repo at AGENT_MAX_IN_FLIGHT_PER_REPO).  A 201 with a
        # task id of 0 would be a lie.
        raise PypiError(
            "该仓库的在途任务已达上限（AGENT_MAX_IN_FLIGHT_PER_REPO），"
            "本次未入队；等当前任务结束后重试",
            status_code=409,
        )
    logger.info("agent task %s enqueued: repo=%s kind=%s actor=%s", task_id, repo_slug, kind, _actor())
    return jsonify(_task_view(queue, task_id, slug=repo_slug)), 201


@agent_tasks_bp.route("/api/v1/agent/tasks/<int:task_id>/retry", methods=["POST"])
@require_permission(AGENT_ADMIN)
@api_operation(
    summary="Retry an agent task",
    description=(
        "Puts a `failed`/`dead` task back on the queue (`queued`, attempts "
        "reset).  A `leased`/`running` task is refused — cancel it first.  "
        "Administrative (`agent:admin`)."
    ),
    tags=["Agent runtime"],
    responses={"200": ok("Task requeued", ref("AgentTask")), **_errors("409")},
)
def retry_agent_task(task_id: int):
    queue = _queue()
    if not queue.retry(task_id, reason=f"manual retry by {_actor()}"):
        raise PypiError("任务不存在或当前状态不可重试", status_code=409)
    logger.info("agent task %s retried by %s", task_id, _actor())
    return jsonify(_task_view(queue, task_id))


@agent_tasks_bp.route("/api/v1/agent/tasks/<int:task_id>/cancel", methods=["POST"])
@require_permission(AGENT_ADMIN)
@api_operation(
    summary="Cancel an agent task",
    description=(
        "Parks a `queued`/`leased`/`running` task as `dead` so no worker picks "
        "it up and a running worker's next heartbeat is rejected — the "
        "cooperative cancellation signal.  Administrative (`agent:admin`)."
    ),
    tags=["Agent runtime"],
    responses={"200": ok("Task cancelled", ref("AgentTask")), **_errors("409")},
)
def cancel_agent_task(task_id: int):
    queue = _queue()
    if not queue.cancel(task_id, reason=f"cancelled by {_actor()}"):
        raise PypiError("任务不存在或已结束", status_code=409)
    logger.info("agent task %s cancelled by %s", task_id, _actor())
    return jsonify(_task_view(queue, task_id))


@agent_tasks_bp.route("/api/v1/agent/tasks/<int:task_id>/log")
@require_permission(AGENT_ADMIN)
@api_operation(
    summary="Agent task log reference",
    description=(
        "Where a task's output lives: the `result.json` reference the runner "
        "wrote and the `review_run.log_ref` it produced.  The bytes are not "
        "streamed here — the reference may name a file inside the sandbox work "
        "directory, which is retained for 24h (§9.2).  Administrative."
    ),
    tags=["Agent runtime"],
    responses={"200": ok("Result and log references", _LOG_SCHEMA), **_errors()},
)
def agent_task_log(task_id: int):
    queue = _queue()
    task = queue.get(task_id)
    if task is None:
        raise PypiError("任务不存在", status_code=404)
    with queue.session() as session:
        run = session.execute(
            select(ReviewRun)
            .where(ReviewRun.agent_task_id == task_id)
            .order_by(ReviewRun.id.desc())
            .limit(1)
        ).scalars().first()
    return jsonify(
        {
            "task_id": task_id,
            "status": task["status"],
            "result_ref": task["result_ref"],
            "log_ref": run.log_ref if run is not None else None,
        }
    )


def _coerce_int(value: Any, name: str, default: int) -> int:
    """One int-ish body field, read the way this module has always read it.

    ``priority``/``max_attempts`` stay untyped in the bound model (see
    ``schemas.AgentTaskCreateRequest``) because ``int()`` *is* the tolerance: an
    absent or null value is the queue's default, and anything it cannot read is
    the prose ``400`` below rather than the binder's.
    """
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        raise BadRequestError(f"{name} 必须是整数") from None


__all__ = ["agent_tasks_bp"]
