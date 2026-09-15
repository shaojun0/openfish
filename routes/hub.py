"""Tools and model-routing routes.

The artifact hub is split one blueprint per ecosystem: npm, docker and debian
live in :mod:`routes.npm`, :mod:`routes.docker` and :mod:`routes.debian`, and
this module keeps the two remaining halves.

* **Tools** — a directory tree on disk, served as a browsable index plus direct
  downloads. It is the closest analogue of ``/simple/`` for binaries and
  scripts, and the remote install scripts (nap, uv, …) link into it.
* **Model routing** — a small JSON document (``MODELS_FILE``) that a downstream
  intranet DSH reads. This server publishes the table and lets an administrator
  edit it in the browser (``model:write``); it does not proxy inference. The
  document itself is owned by :mod:`services.model_routes`.

Endpoints
---------
``GET /tools/``                  tools index — HTML, or JSON with `?format=json`
``GET /tools/<path:filepath>``   download one tool
``GET /api/v1/tools``            tool catalog for the SPA
``GET /api/v1/models``           model-routing table
``POST /api/v1/models``          add a route — ``model:write``
``PUT /api/v1/models/<name>``    edit a route — ``model:write``
``DELETE /api/v1/models/<name>`` remove a route — ``model:write``
``POST /api/v1/models/probe``    probe an unsaved draft — ``model:write``
``POST /api/v1/models/<name>/check`` re-probe a saved route — ``model:write``

Each index mirrors ``/simple/``: a server-rendered page a script (or a human)
can read without the SPA. Templates live under ``static/<ecosystem>/``.

⚠ Decorator order is load-bearing (see ``routes/python_build.py``): the
``@hub_bp.route`` decorator must be the topmost line, or the guard is applied
after registration and never runs. ``scripts/check_auth_guards.py`` enforces
this.
"""

from __future__ import annotations

from flask import (
    Blueprint, abort, jsonify, render_template_string, request,
    send_from_directory, url_for,
)

from auth.decorators import require_permission
from auth.permissions import MODEL_READ, MODEL_WRITE, TOOL_DOWNLOAD, TOOL_READ
from config import settings
from errors import BadRequestError, PypiError
from openapi import api_operation, binary, errors, ok
from routes.hub_common import spa_url, wants_json
from services import hub, model_routes, templates

hub_bp = Blueprint("hub", __name__)

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
        "model": {"type": "string"},
        "aliases": {"type": "array", "items": {"type": "string"}},
        "path": {"type": "string"},
        "enabled": {"type": "boolean"},
        "description": {"type": ["string", "null"]},
        "tags": {"type": "array", "items": {"type": "string"}},
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
        "default_paths": {
            "type": "object",
            "description": "Provider -> endpoint path used when a route omits one",
        },
        "routes": {"type": "array", "items": _MODEL_ROUTE_SCHEMA},
    },
}

_ROUTE_WRITE_BODY = {
    "required": True,
    "content": {
        "application/json": {
            "schema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Required, unique"},
                    "provider": {"type": "string", "enum": list(model_routes.PROVIDERS)},
                    "base_url": {"type": "string", "description": "Required http(s) URL"},
                    "api_key": {
                        "type": ["string", "null"],
                        "description": (
                            "Omitted or null keeps the stored key (the API never "
                            "returns it), empty string clears it."
                        ),
                    },
                    "model": {"type": "string"},
                    "aliases": {"type": "array", "items": {"type": "string"}},
                    "path": {"type": "string"},
                    "enabled": {"type": "boolean"},
                    "description": {"type": "string", "description": "Required"},
                },
                "required": ["name", "base_url", "description"],
            }
        }
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
    return render_template_string(
        templates.tools_index(),
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


@hub_bp.route("/api/v1/models")
@require_permission(MODEL_READ)
@api_operation(
    summary="Model routing table",
    description=(
        "The model endpoints a downstream DSH deployment may be pointed at, "
        "read from `MODELS_FILE`. A missing file yields `exists: false` and an "
        "empty list rather than an error, so the panel renders on a fresh "
        "install.\n\n"
        "The raw API key of a route is **never** returned; `has_api_key` and "
        "`api_key_hint` say whether one is stored. Each route may carry the "
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
    return jsonify(
        model_routes.load(
            settings.hub.models_file,
            health_path=settings.hub.model_health_file,
        )
    )


#: Ceiling on a route-body from the SPA.  The fields are short descriptions,
#: not documents; a larger body is a mistake or an attack.
_MODEL_ROUTE_MAX_BYTES = 64 * 1024


def _route_payload() -> dict:
    """The JSON object body of a model-route write request."""
    if request.content_length and request.content_length > _MODEL_ROUTE_MAX_BYTES:
        raise BadRequestError("请求体过大")
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        raise BadRequestError("请求体必须是 JSON 对象")
    return payload


def _conflict(exc: model_routes.DuplicateRouteError) -> PypiError:
    return PypiError(f"模型路由 {exc} 已存在", status_code=409)


def _write_failed(exc: OSError) -> PypiError:
    """Surface *why* the document could not be written instead of a bare 500."""
    return PypiError(
        f"无法写入模型路由文件：{exc.strerror or exc}（MODELS_FILE 及其目录需可写，"
        "容器单文件挂载不能使用 :ro）",
        status_code=500,
    )


def _not_found(name: str):
    abort(404, description=f"模型路由 {name!r} 不存在")


def _probe_timeout() -> float:
    return settings.hub.model_probe_timeout


def _remember(route: dict, *, previous_name: str | None = None) -> dict:
    """Probe a freshly written route and remember the answer."""
    if previous_name and previous_name != route["name"]:
        model_routes.forget_health(settings.hub.model_health_file, previous_name)
    health = model_routes.probe(route, timeout=_probe_timeout())
    model_routes.record_health(settings.hub.model_health_file, route["name"], health)
    return health


@hub_bp.route("/api/v1/models", methods=["POST"])
@require_permission(MODEL_WRITE)
@api_operation(
    summary="Add a model route",
    description=(
        "Validates one route, appends it to `MODELS_FILE` and immediately "
        "probes its URL for reachability. `name` and `description` are "
        "mandatory; `api_key` may be empty. The response carries the stored "
        "route (without the key) plus the probe result. Requires `model:write`, "
        "which by default only the built-in admin role holds."
    ),
    tags=["Hub"],
    request_body=_ROUTE_WRITE_BODY,
    responses={
        "201": ok("The created route and its first probe", _ROUTE_RESULT_SCHEMA),
        **errors("400", "401", "403", "409", "500"),
    },
)
def create_model_route():
    payload = _route_payload()
    try:
        route = model_routes.create(settings.hub.models_file, payload)
    except model_routes.DuplicateRouteError as exc:
        raise _conflict(exc) from exc
    except OSError as exc:
        raise _write_failed(exc) from exc
    except ValueError as exc:
        raise BadRequestError(str(exc)) from exc
    health = _remember(route)
    return jsonify({
        "route": model_routes.public_route(route, health=health),
        "health": health,
    }), 201


@hub_bp.route("/api/v1/models/<name>", methods=["PUT"])
@require_permission(MODEL_WRITE)
@api_operation(
    summary="Edit a model route",
    description=(
        "Replaces one route in `MODELS_FILE` and re-probes it. The route is "
        "addressed by its current name, so a body with a different `name` "
        "renames it. An omitted or null `api_key` keeps the stored key; an "
        "empty string clears it. Requires `model:write`."
    ),
    tags=["Hub"],
    parameters=[_ROUTE_PARAM],
    request_body=_ROUTE_WRITE_BODY,
    responses={
        "200": ok("The saved route and its probe", _ROUTE_RESULT_SCHEMA),
        **errors("400", "401", "403", "404", "409", "500"),
    },
)
def update_model_route(name: str):
    payload = _route_payload()
    try:
        route = model_routes.update(settings.hub.models_file, name, payload)
    except model_routes.RouteNotFoundError:
        return _not_found(name)
    except model_routes.DuplicateRouteError as exc:
        raise _conflict(exc) from exc
    except OSError as exc:
        raise _write_failed(exc) from exc
    except ValueError as exc:
        raise BadRequestError(str(exc)) from exc
    health = _remember(route, previous_name=name)
    return jsonify({
        "route": model_routes.public_route(route, health=health),
        "health": health,
    })


@hub_bp.route("/api/v1/models/<name>", methods=["DELETE"])
@require_permission(MODEL_WRITE)
@api_operation(
    summary="Remove a model route",
    description=(
        "Deletes one route from `MODELS_FILE`, along with its remembered "
        "connectivity result. Requires `model:write`."
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
        removed = model_routes.delete(settings.hub.models_file, name)
    except model_routes.RouteNotFoundError:
        return _not_found(name)
    except OSError as exc:
        raise _write_failed(exc) from exc
    except ValueError as exc:
        raise BadRequestError(str(exc)) from exc
    model_routes.forget_health(settings.hub.model_health_file, name)
    return jsonify(model_routes.public_route(removed))


@hub_bp.route("/api/v1/models/probe", methods=["POST"])
@require_permission(MODEL_WRITE)
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
        "content": {
            "application/json": {
                "schema": {
                    "type": "object",
                    "properties": {
                        "provider": {"type": "string", "enum": list(model_routes.PROVIDERS)},
                        "base_url": {"type": "string"},
                        "api_key": {"type": ["string", "null"]},
                        "path": {"type": "string"},
                    },
                    "required": ["base_url"],
                }
            }
        },
    },
    responses={
        "200": ok("The probe result", _HEALTH_SCHEMA),
        **errors("400", "401", "403", "500"),
    },
)
def probe_model_route():
    payload = _route_payload()
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
            settings.hub.models_file,
            name,
            health_path=settings.hub.model_health_file,
            timeout=_probe_timeout(),
        )
    except model_routes.RouteNotFoundError:
        return _not_found(name)
    except OSError as exc:
        raise _write_failed(exc) from exc
    except ValueError as exc:
        raise BadRequestError(str(exc)) from exc
    return jsonify(health)


__all__ = ["hub_bp"]
