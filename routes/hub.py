"""Artifact-hub routes — tools catalog, npm scaffold and model routing.

The hub is the second half of the sidebar: besides Python packages, an intranet
deployment needs a place to hand out tools and to describe the model endpoints
a downstream DSH should talk to.  Everything is file-backed (see
:mod:`services.hub`), so there is no write API here — dropping a file into the
configured directory is the upload.

Endpoints
---------
Machine-facing (parseable, no JavaScript)
``GET /tools/``                tools index — HTML, or JSON with `?format=json`
``GET /tools/<path:filepath>`` download one tool
``GET /npm/``                  npm catalog index — HTML or the `/-/all` JSON
``GET /npm/-/all``             npm legacy full-index JSON
``GET /npm/-/ping``            npm health convention, returns `{}`
``GET /npm/files/<filename>``  download one local tarball

JSON API for the SPA
``GET /api/v1/tools``          tool catalog
``GET /api/v1/npm``            npm catalog
``GET /api/v1/models``         model-routing table

The `/tools/` and `/npm/` indexes are mirror images of `/simple/`: a
server-rendered page a script (or a human) can read without the SPA.  Their
templates live under ``static/tools/`` and ``static/npm/`` respectively.

⚠ Decorator order is load-bearing (see ``routes/python_build.py``): the
``@*.route`` decorator must be the topmost line, or the guard is applied after
registration and never runs.  ``scripts/check_auth_guards.py`` enforces this.
"""

from __future__ import annotations

from flask import (
    Blueprint, current_app, jsonify, render_template_string, request, send_from_directory, url_for,
)

from auth.decorators import require_permission
from auth.permissions import MODEL_READ, NPM_READ, TOOL_DOWNLOAD, TOOL_READ
from config import settings
from openapi import api_operation, binary, errors, json_body, ok
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

_NPM_SCHEMA = {
    "type": "object",
    "properties": {
        "root": {"type": "string"},
        "exists": {"type": "boolean"},
        "upstream": {"type": "string"},
        "package_count": {"type": "integer"},
        "packages": {"type": "array", "items": {"type": "object"}},
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

_NPM_ALL_SCHEMA = {
    "type": "object",
    "additionalProperties": {"type": "object"},
    "description": "Keyed by package name, npm's legacy `/-/all` shape.",
}


# ── Helpers ──────────────────────────────────────────────────────────

def _wants_json() -> bool:
    """Content negotiation for the index pages, mirroring `/simple/`."""
    return (
        request.args.get("format") == "json"
        or "application/json" in request.headers.get("Accept", "")
    )


def _tools_payload() -> dict:
    prefix = settings.server.route_prefix.rstrip("/") + "/tools"
    return hub.scan_tools(settings.hub.tools_dir, url_prefix=prefix)


def _npm_payload() -> dict:
    prefix = settings.server.route_prefix.rstrip("/") + "/npm/files"
    return hub.scan_npm(
        settings.hub.npm_dir,
        upstream=settings.hub.npm_upstream,
        url_prefix=prefix,
    )


def _spa_url(path: str) -> str:
    """Absolute-ish URL of an SPA page, honouring the global route prefix."""
    return settings.server.route_prefix.rstrip("/") + path


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
    if _wants_json():
        return jsonify(payload)
    return render_template_string(
        templates.tools_index(),
        server_name=settings.server.server_name,
        base_url=url_for("hub.tools_index", _external=True),
        spa_url=_spa_url("/tools"),
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
    current_app.logger.info("tool download: %s", filepath)
    return send_from_directory(root, filepath, as_attachment=True)


# ── npm: static index + legacy conventions + download ────────────────

@hub_bp.route("/npm/")
@require_permission(NPM_READ)
@api_operation(
    summary="npm catalog index (scaffold)",
    description=(
        "The local npm directory as a browsable index. HTML by default; the "
        "JSON form (`?format=json` or `Accept: application/json`) returns the "
        "same legacy `/-/all` document as `GET /npm/-/all`.\n\n"
        "**The registry protocol is not implemented yet** — packuments and "
        "version manifests are still missing, so `npm install` cannot work yet."
    ),
    tags=["Hub"],
    responses={
        "200": {
            "description": "npm index as HTML or JSON",
            "content": {
                "text/html": {},
                "application/json": {"schema": _NPM_ALL_SCHEMA},
            },
        },
        **errors("401", "403", "500"),
    },
)
def npm_index():
    catalog = _npm_payload()
    document = hub.npm_all_index(catalog)
    if _wants_json():
        return jsonify(document)
    return render_template_string(
        templates.npm_index(),
        server_name=settings.server.server_name,
        base_url=url_for("hub.npm_index", _external=True),
        spa_url=_spa_url("/npm"),
        upstream=catalog["upstream"],
        packages=catalog["packages"],
        package_count=catalog["package_count"],
        all_url=url_for("hub.npm_all"),
        ping_url=url_for("hub.npm_ping"),
        updated=document["_updated"],
    )


@hub_bp.route("/npm/-/all")
@require_permission(NPM_READ)
@api_operation(
    summary="npm full index (legacy /-/all)",
    description=(
        "Every locally known package as one JSON object, in the shape npm's "
        "shut-down `GET /-/all` endpoint used: keyed by package name, each value "
        "carrying `dist-tags` and the versions that actually have a tarball on "
        "disk. It is the closest thing npm has to a static index — the modern "
        "replacements are `/-/v1/search` and the replication feed."
    ),
    tags=["Hub"],
    responses={
        "200": ok("Full index, keyed by package name", _NPM_ALL_SCHEMA),
        **errors("401", "403", "500"),
    },
)
def npm_all():
    return jsonify(hub.npm_all_index(_npm_payload()))


@hub_bp.route("/npm/-/ping")
@require_permission(NPM_READ)
@api_operation(
    summary="npm ping",
    description=(
        "The npm registry health convention: an empty JSON object. `npm ping` "
        "calls this before anything else, so a registry that answers it looks "
        "alive to the client."
    ),
    tags=["Hub"],
    responses={"200": {"description": "Always `{}`", "content": json_body()},
               **errors("401", "403")},
)
def npm_ping():
    return jsonify({})


@hub_bp.route("/npm/files/<path:filename>")
@require_permission(NPM_READ)
@api_operation(
    summary="Download a local npm tarball",
    description=(
        "Streams one `*.tgz` out of `NPM_DIR`. This is a stop-gap so a tarball "
        "placed on disk is fetchable before the npm registry protocol lands."
    ),
    tags=["Hub"],
    responses={
        "200": binary("The requested tarball"),
        **errors("401", "403", "404", "500"),
    },
)
def download_npm_file(filename: str):
    return send_from_directory(settings.hub.npm_dir, filename, as_attachment=True)


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


@hub_bp.route("/api/v1/npm")
@require_permission(NPM_READ)
@api_operation(
    summary="npm catalog (scaffold)",
    description=(
        "Local npm packages known to this server. **The npm registry protocol "
        "is not implemented yet** — this endpoint only describes tarballs and "
        "`catalog.json` entries sitting in `NPM_DIR`, plus the upstream registry "
        "the UI advertises. Entries with a null `download_url` are metadata "
        "only and cannot be fetched."
    ),
    tags=["Hub"],
    responses={
        "200": ok("Local npm catalog", _NPM_SCHEMA),
        **errors("401", "403", "500"),
    },
)
def npm_catalog():
    return jsonify(_npm_payload())


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
