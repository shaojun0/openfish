"""Tools and model-routing routes.

The artifact hub is split one blueprint per ecosystem: npm, docker and debian
live in :mod:`routes.npm`, :mod:`routes.docker` and :mod:`routes.debian`, and
this module keeps the two remaining halves.

* **Tools** — a directory tree on disk, served as a browsable index plus direct
  downloads. It is the closest analogue of ``/simple/`` for binaries and
  scripts, and the remote install scripts (nap, uv, …) link into it.
* **Model routing** — the ``model_routes`` table that a downstream intranet DSH
  reads. This server publishes the table and lets an administrator edit it in
  the browser (``model:write``); it does not proxy inference. The table itself
  is owned by :mod:`services.model_routes`, which also validates every write.
  Each route is classified by ``provider`` (wire format) and ``kind`` (model
  function — chat / completion / embedding / rerank / ocr / asr / tts).

Endpoints
---------
``GET /tools/``                  tools index — HTML, or JSON with `?format=json`
``GET /tools/<path:filepath>``   download one tool
``GET /api/v1/tools``            tool catalog for the SPA
``POST /api/v1/tools``           upload a tool — ``tool:upload``
``GET /api/v1/models``           model-routing table
``POST /api/v1/models``          add a route — ``model:write``
``PUT /api/v1/models/<name>``    edit a route — ``model:write``
``DELETE /api/v1/models/<name>`` remove a route — ``model:write``
``POST /api/v1/models/probe``    probe an unsaved draft — ``model:write``
``POST /api/v1/models/<name>/check`` re-probe a saved route — ``model:write``

Each index mirrors ``/simple/``: a server-rendered page a script (or a human)
can read without the SPA. Templates live under ``static/<ecosystem>/``.

⚠ Decorator order is load-bearing (see ``routes/python_build.py``): the route
decorator must be the topmost line, or the guard is applied after registration
and never runs. ``scripts/check_auth_guards.py`` enforces this.

The write routes bind their input as view parameters instead of reading
``request`` by hand: ``body: ModelRouteRequest`` on the model-route writes and
``form: ToolUploadForm`` on the tool upload. ``@validate_request()`` sits below
the guard, so an unauthorized write is answered ``401``/``403`` and never by the
binder. The model-route bodies are dumped with ``exclude_unset=True`` because
``services.model_routes`` distinguishes *absent* from *null* — an omitted
``kind``/``api_key`` keeps the stored value, an explicit null does not — and
``scripts/check_request_binding.py`` pins that.
"""

from __future__ import annotations

import logging
from pathlib import Path

from flask import (
    abort, jsonify, render_template, send_from_directory, url_for,
)
from flask_openapi3 import APIBlueprint, validate_request
from sqlalchemy.exc import SQLAlchemyError

from auth.decorators import require_permission
from auth.permissions import (
    MODEL_READ, MODEL_RESOLVE, MODEL_WRITE, TOOL_DOWNLOAD, TOOL_READ,
    TOOL_UPLOAD,
)
from config import settings
from errors import BadRequestError, PypiError
from extensions.database import Session
from openapi import api_operation, binary, errors, json_body, ok
from routes.hub_common import body_ceiling, spa_url, wants_json
from schemas import ModelRouteProbeRequest, ModelRouteRequest, ToolUploadForm
from services import hub, hub_upload, model_routes
from services.sealing import SealingKeyMissing

logger = logging.getLogger("cpypiserver.hub")

hub_bp = APIBlueprint("hub", __name__)

#: Inline response shapes.  The hub payloads are deliberately schemaless at the
#: leaves (a tool may carry arbitrary tags) so a JSON object with `type` set is
#: enough to describe them without a pydantic model per catalog.
_TOOLS_SCHEMA = {
    "type": "object",
    "properties": {
        "root": {"type": "string"},
        "exists": {"type": "boolean"},
        "url_prefix": {"type": "string"},
        "tool_count": {"type": "integer"},
        "categories": {"type": "array", "items": {"type": "object"}},
    },
}

#: One `services.hub.scan_tools` entry — the shape both the catalog and the
#: upload response use, so the SPA can render an upload without a second fetch.
_TOOL_ENTRY_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "filename": {"type": "string"},
        "relative_path": {
            "type": "string",
            "description": "Path relative to `TOOLS_DIR` — the download URL's tail",
        },
        "download_url": {"type": "string"},
        "size": {"type": "integer"},
        "size_human": {"type": "string"},
        "sha256": {"type": ["string", "null"]},
        "modified": {"type": ["string", "null"]},
        "description": {"type": ["string", "null"]},
        "tags": {"type": "array", "items": {"type": "string"}},
    },
}

_HEALTH_SCHEMA = {
    "type": "object",
    "properties": {
        "reachable": {"type": "boolean", "description": "The endpoint answered at all"},
        "ok": {"type": "boolean", "description": "It answered with a 2xx/3xx status"},
        "status": {
            "type": "string",
            "description": (
                "ok | auth | method | not_found | client_error | server_error | "
                "unreachable"
            ),
        },
        "http_status": {"type": ["integer", "null"]},
        "latency_ms": {"type": ["integer", "null"]},
        "url": {"type": "string", "description": "The exact URL that was probed"},
        "error": {"type": ["string", "null"]},
        "checked_at": {"type": ["string", "null"]},
    },
}

_MODEL_ROUTE_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "provider": {
            "type": "string",
            "description": "Wire format: " + " / ".join(model_routes.PROVIDERS),
        },
        "kind": {
            "type": "string",
            "description": (
                "Model function, orthogonal to the wire format: "
                + " / ".join(model_routes.KINDS)
                + ". Absent it defaults by provider ("
                + ", ".join(
                    f"{name} → {kind}" for name, kind in model_routes.DEFAULT_KINDS.items()
                )
                + ")."
            ),
        },
        "base_url": {"type": "string"},
        "api_key": {
            "type": ["string", "null"],
            "description": (
                "Always null in a response — the stored key is never returned. "
                "Use `has_api_key` / `api_key_hint` to see whether one is set."
            ),
        },
        "has_api_key": {"type": "boolean"},
        "api_key_hint": {
            "type": ["string", "null"],
            "description": "Non-secret hint: the last four characters of the key",
        },
        "api_key_source": {
            "type": "string",
            "description": (
                "stored | plaintext | unreadable | none — how the route is "
                "authenticated, never what with. `stored` is a key sealed in the "
                "table and opened successfully; `plaintext` is a row written "
                "before sealing existed and still awaiting `cli.py model-route "
                "seal`; `unreadable` means the envelope cannot be opened under "
                "this deployment's MODEL_ROUTE_KEY (missing or wrong key, or a "
                "corrupt row); `none` means the route needs no credential."
            ),
        },
        "model": {"type": "string"},
        "aliases": {"type": "array", "items": {"type": "string"}},
        "path": {"type": "string"},
        "enabled": {"type": "boolean"},
        "description": {"type": ["string", "null"]},
        "health": {
            "anyOf": [_HEALTH_SCHEMA, {"type": "null"}],
            "description": "The last connectivity probe, when one has run",
        },
    },
}

_MODELS_SCHEMA = {
    "type": "object",
    "properties": {
        "source": {"type": "string"},
        "exists": {"type": "boolean"},
        "error": {"type": ["string", "null"]},
        "version": {},
        "providers": {
            "type": "array",
            "items": {"type": "string"},
            "description": "The wire formats an administrator may choose",
        },
        "kinds": {
            "type": "array",
            "items": {"type": "string"},
            "description": "The model functions an administrator may choose",
        },
        "default_paths": {
            "type": "object",
            "description": "Provider -> endpoint path used when a route omits one",
        },
        "routes": {"type": "array", "items": _MODEL_ROUTE_SCHEMA},
    },
}

_MODEL_ROUTE_RESOLVED_SCHEMA = {
    "type": "object",
    "properties": {
        **_MODEL_ROUTE_SCHEMA["properties"],
        "api_key": {
            "type": "string",
            "description": (
                "The **effective** upstream key, verbatim — decrypted from the "
                "sealed column, or empty when the route needs none or its "
                "envelope cannot be opened (see `api_key_source`). Only "
                "`/api/v1/models/resolved` ever returns this."
            ),
        },
        "endpoint_url": {
            "type": "string",
            "description": "`base_url` + `path` already joined",
        },
    },
}

_MODELS_RESOLVED_SCHEMA = {
    "type": "object",
    "properties": {
        **_MODELS_SCHEMA["properties"],
        "routes": {"type": "array", "items": _MODEL_ROUTE_RESOLVED_SCHEMA},
    },
}

_ROUTE_PARAM = {
    "name": "name",
    "in": "path",
    "required": True,
    "description": "The route's current name (`probe` is reserved)",
    "schema": {"type": "string"},
}

_ROUTE_RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "route": _MODEL_ROUTE_SCHEMA,
        "health": _HEALTH_SCHEMA,
    },
}


def _tools_payload() -> dict:
    prefix = settings.server.route_prefix.rstrip("/") + "/tools"
    return hub.scan_tools(settings.hub.tools_dir, url_prefix=prefix)


def _tool_entry_by_path(relative_path: str) -> dict | None:
    """The catalog entry for one on-disk tool, or ``None`` if it is hidden."""
    for category in _tools_payload()["categories"]:
        for tool in category["tools"]:
            if tool["relative_path"] == relative_path:
                return tool
    return None


# ── Tools: static index + download ───────────────────────────────────

@hub_bp.route("/tools/")
@require_permission(TOOL_READ)
@api_operation(
    summary="Tools index",
    description=(
        "The tools directory as a server-rendered index, the tools counterpart "
        "of `/simple/`. Returns HTML to a browser and JSON when `?format=json` "
        "or `Accept: application/json` is used; the JSON is the same document "
        "`GET /api/v1/tools` returns.\n\n"
        "Each link points at `/tools/<category>/<filename>`."
    ),
    tags=["Hub"],
    responses={
        "200": {
            "description": "Tool index as HTML or JSON",
            "content": {
                "text/html": {},
                "application/json": {"schema": _TOOLS_SCHEMA},
            },
        },
        **errors("401", "403", "500"),
    },
)
def tools_index():
    payload = _tools_payload()
    if wants_json():
        return jsonify(payload)
    return render_template(
        "tools/index.html",
        server_name=settings.server.server_name,
        base_url=url_for("hub.tools_index", _external=True),
        spa_url=spa_url("/tools"),
        categories=payload["categories"],
        tool_count=payload["tool_count"],
    )


@hub_bp.route("/tools/<path:filepath>")
@require_permission(TOOL_DOWNLOAD)
@api_operation(
    summary="Download a tool",
    description=(
        "Streams one file out of `TOOLS_DIR`. The path is the tool's "
        "`relative_path` from the catalog; traversal outside the tools root is "
        "refused by `send_from_directory`."
    ),
    tags=["Hub"],
    responses={
        "200": binary("The requested tool"),
        **errors("401", "403", "404", "500"),
    },
)
def download_tool(filepath: str):
    root = settings.hub.tools_dir
    return send_from_directory(root, filepath, as_attachment=True)


# ── JSON API for the SPA ─────────────────────────────────────────────

@hub_bp.route("/api/v1/tools")
@require_permission(TOOL_READ)
@api_operation(
    summary="Tool catalog",
    description=(
        "The downloadable tools an intranet client can pull from this server, "
        "grouped into categories. Every file below `TOOLS_DIR` is listed; an "
        "immediate sub-directory is one category, and `catalog.json` may "
        "override the display names and descriptions.\n\n"
        "`download_url` is the browser-facing link — it is what a user clicks "
        "or `curl -O` fetches."
    ),
    tags=["Hub"],
    responses={
        "200": ok("Tool categories and files", _TOOLS_SCHEMA),
        **errors("401", "403", "500"),
    },
)
def tools_catalog():
    return jsonify(_tools_payload())


@hub_bp.post("/api/v1/tools")
@require_permission(TOOL_UPLOAD)
@validate_request()
@api_operation(
    summary="Upload a tool",
    description=(
        "Stores one `multipart/form-data` `file` part under `TOOLS_DIR` — the "
        "browser counterpart of copying a script into a category. The optional "
        "`category` field names one immediate sub-directory; an omitted or empty "
        "value places the file at the tools root, which the catalog shows as "
        "“uncategorized”.\n\n"
        "The filename must be a single path segment (no `/`, no backslash, no "
        "`..`, no dotfile) with an allowed script, archive, binary or config "
        "extension. The names the catalog scanner hides as metadata "
        "(`catalog.json`, README/LICENSE/CHANGELOG) are refused too, because a "
        "file it would not list could not be reported back. An existing file "
        "answers `409` unless `STORAGE__OVERWRITE=true`; a body above "
        "`MAX_CONTENT_LENGTH` answers `413`. The response is the stored entry in "
        "exactly the shape `GET /api/v1/tools` reports.\n\n"
        "Requires `tool:upload`, which by default only the built-in admin role "
        "holds: every signed-in user may browse and download the catalog, but "
        "publishing to it is an administrative act."
    ),
    tags=["Hub"],
    request_body={
        "required": True,
        "content": {
            "multipart/form-data": {
                "schema": {
                    "type": "object",
                    "properties": {
                        "category": {
                            "type": "string",
                            "description": (
                                "One immediate sub-directory of TOOLS_DIR; empty "
                                "or omitted means the tools root"
                            ),
                        },
                        "file": {"type": "string", "format": "binary"},
                    },
                    "required": ["file"],
                }
            }
        },
    },
    responses={
        "201": ok("The stored tool, in the catalog's entry shape", _TOOL_ENTRY_SCHEMA),
        **errors("400", "401", "403", "409", "413", "500"),
    },
)
def upload_tool(form: ToolUploadForm):
    # `form.file` is the `file` part, read out of `request.files` by the binder —
    # see `schemas.ToolUploadForm` for why the field must be a
    # `flask_openapi3.FileStorage` and not an optional one.  `filename` is also
    # what tells a real part from a *text* field of the same name: the binder
    # copies one of those into the model as a plain `str` (the field's schema is
    # what sends it to `request.files`, but `FileStorage`'s validator then hands
    # any value back untouched), so the `getattr` is what answers that request
    # with this route's 400 instead of an `AttributeError` 500.
    upload = form.file
    if not getattr(upload, "filename", None):
        raise BadRequestError("multipart/form-data 需要一个 file 字段")
    category = (form.category or "").strip()
    target = hub_upload.tools_target(
        settings.hub.tools_dir, upload.filename, category
    )
    hub_upload.save(target, upload)
    relative = target.relative_to(Path(settings.hub.tools_dir)).as_posix()
    entry = _tool_entry_by_path(relative)
    if entry is None:
        # `tools_target` already refuses every name the scanner hides, so this
        # only fires if that visibility rule drifts; fail loudly rather than
        # answer 201 for a tool nobody can see.
        raise PypiError(
            "已写入文件，但工具目录扫描未列出它；请检查 TOOLS_DIR 的可见性规则",
            status_code=500,
        )
    return jsonify(entry), 201


@hub_bp.route("/api/v1/models")
@require_permission(MODEL_READ)
@api_operation(
    summary="Model routing table",
    description=(
        "The model endpoints a downstream DSH deployment may be pointed at, "
        "read from the `model_routes` table. An empty table yields an empty "
        "list rather than an error, so the panel renders on a fresh install.\n\n"
        "Every route is classified on two independent axes: `provider` is the "
        "wire format (`openai` / `mineru` / `anthropic`) and `kind` is the model "
        "function (`chat` / `completion` / `embedding` / `rerank` / `ocr` / "
        "`asr` / `tts`). A route that names no `kind` falls back to its "
        "protocol default, so the table classifies correctly even for a row "
        "written before the field existed.\n\n"
        "The raw API key of a route is **never** returned; `has_api_key` and "
        "`api_key_hint` say whether one is stored, and `api_key_source` says how "
        "it is configured (`plaintext` and `unreadable` are the two states worth "
        "acting on). Each route may carry the "
        "result of the last connectivity probe under `health` (see "
        "`POST /api/v1/models/{name}/check`)."
    ),
    tags=["Hub"],
    responses={
        "200": ok("Model routes", _MODELS_SCHEMA),
        **errors("401", "403", "500"),
    },
)
def model_routes_index():
    return jsonify(model_routes.load(Session))


@hub_bp.route("/api/v1/models/resolved")
@require_permission(MODEL_RESOLVE)
@api_operation(
    summary="Model routing table with upstream keys",
    description=(
        "The same table as `GET /api/v1/models`, except each route carries its "
        "**real** `api_key` and a pre-joined `endpoint_url`. This is what the "
        "DSH `enterprise-intranet` plugin reads to register a provider and adopt "
        "the route whose aliases include `default` as the agent default model. "
        "The plugin selects by `kind` — only `chat` and `completion` routes "
        "become LLM providers, while `embedding`, `rerank`, `ocr`, `asr` and "
        "`tts` stay registry entries for other consumers.\n\n"
        "Deliberately a separate endpoint from the browsing view: the console "
        "must never round-trip a secret, while a downstream client cannot do "
        "anything with a masked one. Guarded by `model:resolve`, which the "
        "`authenticated` role holds and the `anonymous` role does not."
    ),
    tags=["Hub"],
    responses={
        "200": ok("Model routes including secrets", _MODELS_RESOLVED_SCHEMA),
        **errors("401", "403", "500"),
    },
)
def model_routes_resolved():
    return jsonify(model_routes.resolve(Session))


#: Ceiling on a route-body from the SPA.  The fields are short descriptions,
#: not documents; a larger body is a mistake or an attack.  The gate itself is
#: `routes.hub_common.body_ceiling`, shared with the repository writes, and it
#: has to sit between the guard and the binder — see its docstring.
_MODEL_ROUTE_MAX_BYTES = 64 * 1024
_MODEL_ROUTE_BODY = body_ceiling(_MODEL_ROUTE_MAX_BYTES, "请求体过大")


def _route_fields(body: ModelRouteRequest) -> dict:
    """A bound route body as the mapping ``services.model_routes`` expects.

    ``exclude_unset=True`` is load-bearing, not an optimisation: that service
    distinguishes an **absent** field from an explicit ``null`` (``payload.get``
    with a default), so an omitted ``kind``/``provider``/``api_key`` must not
    arrive as ``None``.  Dumping every field would silently turn "keep the stored
    value" into "reset to the default" on every ``PUT``.
    """
    return body.model_dump(by_alias=True, exclude_unset=True)


def _conflict(exc: model_routes.DuplicateRouteError) -> PypiError:
    return PypiError(f"模型路由 {exc} 已存在", status_code=409)


def _write_failed(exc: Exception) -> PypiError:
    """Answer a stable 500 while the database detail goes to the server log.

    The previous version interpolated ``exc.strerror`` into the response body.
    That is exactly the "system information disclosure" shape: the reason a
    write failed (a path, a mount flag, a permission bit) is also the map an
    attacker would otherwise have to guess.  The operator still gets the detail
    — from the log, where it belongs.
    """
    logger.error("cannot write the model-route table: %s", exc)
    return PypiError(
        "无法写入模型路由表（数据库不可写或连接中断）；详见服务日志",
        status_code=500,
    )


def _not_found(name: str):
    abort(404, description="模型路由不存在")


def _no_sealing_key(exc: Exception) -> PypiError:
    """Answer 500 when the deployment has no MODEL_ROUTE_KEY.

    A *server* misconfiguration, not a bad request: the operator's body was
    fine, and the honest answer is "this box cannot store a key right now"
    rather than a 400 that blames the caller — or, worse, a plaintext row.
    """
    logger.error("refusing to store a model-route API key: %s", exc)
    return PypiError(str(exc), status_code=500)


def _probe_timeout() -> float:
    return settings.hub.model_probe_timeout


def _remember(route: dict) -> dict:
    """Probe a freshly written route and remember the answer on its row."""
    health = model_routes.probe(route, timeout=_probe_timeout())
    model_routes.record_health(Session, route["name"], health)
    return health


@hub_bp.post("/api/v1/models")
@require_permission(MODEL_WRITE)
@_MODEL_ROUTE_BODY
@validate_request()
@api_operation(
    summary="Add a model route",
    description=(
        "Validates one route, inserts it into the `model_routes` table and "
        "immediately probes its URL for reachability. `name` and `description` "
        "are mandatory; `api_key` may be empty. `provider` names the wire format "
        "and `kind` the model function (defaulting by protocol when omitted). "
        "A non-empty `api_key` is sealed with `MODEL_ROUTE_KEY` before it is "
        "stored, so a deployment without that key gets a 500 rather than a "
        "plaintext row. The response carries the stored "
        "route (without the key) plus the probe result. Requires `model:write`, "
        "which by default only the built-in admin role holds."
    ),
    tags=["Hub"],
    request_body={
        "required": True,
        "description": "One model route; `services.model_routes` owns the rules",
        "content": json_body("ModelRouteRequest"),
    },
    responses={
        "201": ok("The created route and its first probe", _ROUTE_RESULT_SCHEMA),
        **errors("400", "401", "403", "409", "500"),
    },
)
def create_model_route(body: ModelRouteRequest):
    payload = _route_fields(body)
    try:
        route = model_routes.create(Session, payload)
    except model_routes.DuplicateRouteError as exc:
        raise _conflict(exc) from exc
    except SealingKeyMissing as exc:
        raise _no_sealing_key(exc) from exc
    except SQLAlchemyError as exc:
        raise _write_failed(exc) from exc
    except ValueError as exc:
        raise BadRequestError(str(exc)) from exc
    health = _remember(route)
    return jsonify({
        "route": model_routes.public_route(route, health=health),
        "health": health,
    }), 201


@hub_bp.put("/api/v1/models/<name>")
@require_permission(MODEL_WRITE)
@_MODEL_ROUTE_BODY
@validate_request()
@api_operation(
    summary="Edit a model route",
    description=(
        "Replaces one route in the `model_routes` table and re-probes it. The "
        "route is "
        "addressed by its current name, so a body with a different `name` "
        "renames it. An omitted or null `api_key` keeps the stored key; an "
        "empty string clears it; a value is sealed with `MODEL_ROUTE_KEY` before "
        "storage. An omitted `kind` keeps the stored one and an "
        "omitted `provider` keeps its default kind. Requires `model:write`."
    ),
    tags=["Hub"],
    parameters=[_ROUTE_PARAM],
    request_body={
        "required": True,
        "description": (
            "The fields to change; anything omitted keeps the stored value — "
            "the body is bound with `exclude_unset`, so *absent* really means absent"
        ),
        "content": json_body("ModelRouteRequest"),
    },
    responses={
        "200": ok("The saved route and its probe", _ROUTE_RESULT_SCHEMA),
        **errors("400", "401", "403", "404", "409", "500"),
    },
)
def update_model_route(name: str, body: ModelRouteRequest):
    payload = _route_fields(body)
    try:
        route = model_routes.update(Session, name, payload)
    except model_routes.RouteNotFoundError:
        return _not_found(name)
    except model_routes.DuplicateRouteError as exc:
        raise _conflict(exc) from exc
    except SealingKeyMissing as exc:
        raise _no_sealing_key(exc) from exc
    except SQLAlchemyError as exc:
        raise _write_failed(exc) from exc
    except ValueError as exc:
        raise BadRequestError(str(exc)) from exc
    health = _remember(route)
    return jsonify({
        "route": model_routes.public_route(route, health=health),
        "health": health,
    })


@hub_bp.route("/api/v1/models/<name>", methods=["DELETE"])
@require_permission(MODEL_WRITE)
@api_operation(
    summary="Remove a model route",
    description=(
        "Deletes one route from the `model_routes` table, along with its "
        "remembered connectivity result. Requires `model:write`."
    ),
    tags=["Hub"],
    parameters=[_ROUTE_PARAM],
    responses={
        "200": ok("The removed route", _MODEL_ROUTE_SCHEMA),
        **errors("400", "401", "403", "404", "500"),
    },
)
def delete_model_route(name: str):
    try:
        removed = model_routes.delete(Session, name)
    except model_routes.RouteNotFoundError:
        return _not_found(name)
    except SQLAlchemyError as exc:
        raise _write_failed(exc) from exc
    except ValueError as exc:
        raise BadRequestError(str(exc)) from exc
    return jsonify(model_routes.public_route(removed))


@hub_bp.post("/api/v1/models/probe")
@require_permission(MODEL_WRITE)
@_MODEL_ROUTE_BODY
@validate_request()
@api_operation(
    summary="Probe an unsaved model route",
    description=(
        "Runs the same connectivity check the create/update endpoints run, but "
        "against the request body instead of a stored route — the routing "
        "panel's “检测” button uses it while a row is still being edited. "
        "`base_url` is the only required field. Nothing is written. Requires "
        "`model:write`."
    ),
    tags=["Hub"],
    request_body={
        "required": True,
        "description": "The draft to probe — `base_url` is the only required field",
        "content": json_body("ModelRouteProbeRequest"),
    },
    responses={
        "200": ok("The probe result", _HEALTH_SCHEMA),
        **errors("400", "401", "403", "500"),
    },
)
def probe_model_route(body: ModelRouteProbeRequest):
    payload = _route_fields(body)
    try:
        health = model_routes.probe_target(payload, timeout=_probe_timeout())
    except ValueError as exc:
        raise BadRequestError(str(exc)) from exc
    return jsonify(health)


@hub_bp.route("/api/v1/models/<name>/check", methods=["POST"])
@require_permission(MODEL_WRITE)
@api_operation(
    summary="Re-probe a saved model route",
    description=(
        "Probes the stored route (including its API key) and updates the "
        "remembered health result the routing table displays. Requires "
        "`model:write`."
    ),
    tags=["Hub"],
    parameters=[_ROUTE_PARAM],
    responses={
        "200": ok("The probe result", _HEALTH_SCHEMA),
        **errors("400", "401", "403", "404", "500"),
    },
)
def check_model_route(name: str):
    try:
        health = model_routes.probe_and_record(
            Session,
            name,
            timeout=_probe_timeout(),
        )
    except model_routes.RouteNotFoundError:
        return _not_found(name)
    except SQLAlchemyError as exc:
        raise _write_failed(exc) from exc
    except ValueError as exc:
        raise BadRequestError(str(exc)) from exc
    return jsonify(health)


__all__ = ["hub_bp"]
