"""Tools and model-routing routes.

The artifact hub is split one blueprint per ecosystem: npm, docker and debian
live in :mod:`routes.npm`, :mod:`routes.docker` and :mod:`routes.debian`, and
this module keeps the two remaining halves.

* **Tools** — a directory tree on disk, served as a browsable index plus direct
  downloads. It is the closest analogue of ``/simple/`` for binaries and
  scripts, and the remote install scripts (nap, uv, …) link into it.
* **Model routing** — a small JSON document (``MODELS_FILE``) that a downstream
  intranet DSH reads. This server publishes the table; it does not proxy
  inference.

Endpoints
---------
``GET /tools/``                  tools index — HTML, or JSON with `?format=json`
``GET /tools/<path:filepath>``   download one tool
``GET /api/v1/tools``            tool catalog for the SPA
``GET /api/v1/models``           model-routing table

Each index mirrors ``/simple/``: a server-rendered page a script (or a human)
can read without the SPA. Templates live under ``static/<ecosystem>/``.

⚠ Decorator order is load-bearing (see ``routes/python_build.py``): the
``@hub_bp.route`` decorator must be the topmost line, or the guard is applied
after registration and never runs. ``scripts/check_auth_guards.py`` enforces
this.
"""

from __future__ import annotations

from flask import (
    Blueprint, jsonify, render_template_string, send_from_directory, url_for,
)

from auth.decorators import require_permission
from auth.permissions import MODEL_READ, TOOL_DOWNLOAD, TOOL_READ
from config import settings
from openapi import api_operation, binary, errors, ok
from routes.hub_common import spa_url, wants_json
from services import hub, templates

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

_MODELS_SCHEMA = {
    "type": "object",
    "properties": {
        "source": {"type": "string"},
        "exists": {"type": "boolean"},
        "error": {"type": ["string", "null"]},
        "version": {},
        "routes": {"type": "array", "items": {"type": "object"}},
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
        "install."
    ),
    tags=["Hub"],
    responses={
        "200": ok("Model routes", _MODELS_SCHEMA),
        **errors("401", "403", "500"),
    },
)
def model_routes():
    return jsonify(hub.load_model_routes(settings.hub.models_file))


__all__ = ["hub_bp"]
