"""Artifact-hub routes — tools, npm, docker, debian and model routing.

The hub is the second half of the sidebar: besides Python packages, an intranet
deployment needs a place to hand out tools, npm tarballs, offline docker images
and .deb packages, and to describe the model endpoints a downstream DSH should
talk to.  Everything is file-backed (see :mod:`services.hub`), so there is no
write API here — dropping a file into the configured directory is the upload.

Endpoints
---------
Machine-facing (parseable, no JavaScript)
``GET /tools/``                  tools index — HTML, or JSON with `?format=json`
``GET /tools/<path:filepath>``   download one tool
``GET /npm/``                    npm catalog index — HTML or the `/-/all` JSON
``GET /npm/-/all``               npm legacy full-index JSON
``GET /npm/-/ping``              npm health convention, returns `{}`
``GET /npm/files/<filename>``    download one local tarball
``GET /docker/``                 docker catalog index — HTML or JSON
``GET /docker/v2/_catalog``      Registry v2 repository list
``GET /docker/files/<filename>`` download an image tar / compose file
``GET /debian/``                 debian catalog index — HTML or JSON
``GET /debian/Packages``         flat apt `Packages` index
``GET /debian/files/<filename>`` download a .deb / apt config snippet

JSON API for the SPA
``GET /api/v1/tools``            tool catalog
``GET /api/v1/npm``              npm catalog
``GET /api/v1/docker``           docker catalog
``GET /api/v1/debian``           debian catalog
``GET /api/v1/models``           model-routing table

Each ``<ecosystem>/`` index is a mirror of ``/simple/``: a server-rendered page
a script (or a human) can read without the SPA, plus the one JSON enumeration
endpoint that ecosystem actually recognises.  Templates live under
``static/<ecosystem>/``.

⚠ Decorator order is load-bearing (see ``routes/python_build.py``): the
``@*.route`` decorator must be the topmost line, or the guard is applied after
registration and never runs.  ``scripts/check_auth_guards.py`` enforces this.
"""

from __future__ import annotations

from flask import (
    Blueprint, Response, current_app, jsonify, render_template_string, request,
    send_from_directory, url_for,
)

from auth.decorators import require_permission
from auth.permissions import (
    DEBIAN_DOWNLOAD, DEBIAN_READ, DOCKER_DOWNLOAD, DOCKER_READ,
    MODEL_READ, NPM_READ, TOOL_DOWNLOAD, TOOL_READ,
)
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

_DOCKER_SCHEMA = {
    "type": "object",
    "properties": {
        "root": {"type": "string"},
        "exists": {"type": "boolean"},
        "registry": {"type": "string"},
        "artifact_count": {"type": "integer"},
        "artifacts": {"type": "array", "items": {"type": "object"}},
    },
}

_DEBIAN_SCHEMA = {
    "type": "object",
    "properties": {
        "root": {"type": "string"},
        "exists": {"type": "boolean"},
        "mirror": {"type": "string"},
        "artifact_count": {"type": "integer"},
        "artifacts": {"type": "array", "items": {"type": "object"}},
    },
}

_DOCKER_CATALOG_SCHEMA = {
    "type": "object",
    "properties": {"repositories": {"type": "array", "items": {"type": "string"}}},
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


def _docker_payload() -> dict:
    prefix = settings.server.route_prefix.rstrip("/") + "/docker/files"
    return hub.scan_docker(
        settings.hub.docker_dir,
        url_prefix=prefix,
        registry=settings.hub.docker_registry,
    )


def _debian_payload() -> dict:
    prefix = settings.server.route_prefix.rstrip("/") + "/debian/files"
    return hub.scan_debian(
        settings.hub.debian_dir,
        url_prefix=prefix,
        mirror=settings.hub.debian_mirror,
    )


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


# ── Docker: static index + Registry v2 catalog + download ────────────

@hub_bp.route("/docker/")
@require_permission(DOCKER_READ)
@api_operation(
    summary="Docker catalog index (scaffold)",
    description=(
        "The local docker directory as a browsable index: `docker save` image "
        "tarballs plus the compose/Dockerfile snippets an offline host needs. "
        "HTML by default, the `/api/v1/docker` document with `?format=json`.\n\n"
        "**No registry proxy.** A real `docker pull` needs the registry "
        "protocol; this page only hands out the tarballs (`docker load -i`)."
    ),
    tags=["Hub"],
    responses={
        "200": {
            "description": "Docker catalog as HTML or JSON",
            "content": {
                "text/html": {},
                "application/json": {"schema": _DOCKER_SCHEMA},
            },
        },
        **errors("401", "403", "500"),
    },
)
def docker_index():
    catalog = _docker_payload()
    if _wants_json():
        return jsonify(catalog)
    return render_template_string(
        templates.docker_index(),
        server_name=settings.server.server_name,
        base_url=url_for("hub.docker_index", _external=True),
        spa_url=_spa_url("/docker"),
        registry=catalog["registry"],
        artifacts=catalog["artifacts"],
        artifact_count=catalog["artifact_count"],
        catalog_url=url_for("hub.docker_catalog"),
    )


@hub_bp.route("/docker/v2/_catalog")
@require_permission(DOCKER_READ)
@api_operation(
    summary="Docker registry catalog",
    description=(
        "The repositories present locally, in the OCI distribution spec's "
        "`GET /v2/_catalog` shape (`{\"repositories\": [...]}`). It is the only "
        "enumeration endpoint the docker registry protocol defines, which makes "
        "it this ecosystem's counterpart to npm's `/-/all`."
    ),
    tags=["Hub"],
    responses={
        "200": ok("Repository names", _DOCKER_CATALOG_SCHEMA),
        **errors("401", "403", "500"),
    },
)
def docker_catalog():
    return jsonify(hub.docker_registry_catalog(_docker_payload()))


@hub_bp.route("/docker/files/<path:filename>")
@require_permission(DOCKER_DOWNLOAD)
@api_operation(
    summary="Download a docker artifact",
    description=(
        "Streams one image tarball or compose/Dockerfile out of `DOCKER_DIR`. "
        "Load an image with `docker load -i <file.tar>`."
    ),
    tags=["Hub"],
    responses={
        "200": binary("The requested image tarball or config file"),
        **errors("401", "403", "404", "500"),
    },
)
def download_docker_file(filename: str):
    return send_from_directory(settings.hub.docker_dir, filename, as_attachment=True)


# ── Debian: static index + flat Packages index + download ────────────

@hub_bp.route("/debian/")
@require_permission(DEBIAN_READ)
@api_operation(
    summary="Debian catalog index (scaffold)",
    description=(
        "The local debian directory as a browsable index: `.deb` files plus the "
        "apt `sources.list` snippet for the intranet mirror. HTML by default, "
        "the `/api/v1/debian` document with `?format=json`.\n\n"
        "**No apt proxy.** `apt` reads the flat `Packages` index at "
        "`/debian/Packages`; the `.deb` files themselves are served from "
        "`/debian/files/`."
    ),
    tags=["Hub"],
    responses={
        "200": {
            "description": "Debian catalog as HTML or JSON",
            "content": {
                "text/html": {},
                "application/json": {"schema": _DEBIAN_SCHEMA},
            },
        },
        **errors("401", "403", "500"),
    },
)
def debian_index():
    catalog = _debian_payload()
    if _wants_json():
        return jsonify(catalog)
    return render_template_string(
        templates.debian_index(),
        server_name=settings.server.server_name,
        base_url=url_for("hub.debian_index", _external=True),
        spa_url=_spa_url("/debian"),
        mirror=catalog["mirror"],
        artifacts=catalog["artifacts"],
        artifact_count=catalog["artifact_count"],
        packages_url=url_for("hub.debian_packages"),
    )


@hub_bp.route("/debian/Packages")
@require_permission(DEBIAN_READ)
@api_operation(
    summary="Flat apt Packages index",
    description=(
        "A flat apt repository index rendered from the local `.deb` files — the "
        "static index element of the Debian ecosystem. Only entries that "
        "actually exist on disk are listed, because apt fails on a `Filename:` "
        "that does not resolve."
    ),
    tags=["Hub"],
    responses={
        "200": {"description": "apt Packages stanzas", "content": {"text/plain": {}}},
        **errors("401", "403", "500"),
    },
)
def debian_packages():
    return Response(hub.debian_packages_index(_debian_payload()), mimetype="text/plain")


@hub_bp.route("/debian/files/<path:filename>")
@require_permission(DEBIAN_DOWNLOAD)
@api_operation(
    summary="Download a debian artifact",
    description=(
        "Streams one `.deb` or apt config snippet out of `DEBIAN_DIR`. "
        "Install a package with `apt install ./<file>.deb`."
    ),
    tags=["Hub"],
    responses={
        "200": binary("The requested .deb or config file"),
        **errors("401", "403", "404", "500"),
    },
)
def download_debian_file(filename: str):
    return send_from_directory(settings.hub.debian_dir, filename, as_attachment=True)


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


@hub_bp.route("/api/v1/docker")
@require_permission(DOCKER_READ)
@api_operation(
    summary="Docker catalog (scaffold)",
    description=(
        "Offline docker artifacts known to this server: `docker save` image "
        "tarballs (filename parsed as `<name>-<tag>.tar`) and compose/Dockerfile "
        "snippets, plus the optional intranet registry the UI advertises. "
        "Entries with a null `download_url` are metadata only."
    ),
    tags=["Hub"],
    responses={
        "200": ok("Local docker catalog", _DOCKER_SCHEMA),
        **errors("401", "403", "500"),
    },
)
def docker_catalog_api():
    return jsonify(_docker_payload())


@hub_bp.route("/api/v1/debian")
@require_permission(DEBIAN_READ)
@api_operation(
    summary="Debian catalog (scaffold)",
    description=(
        "Local `.deb` packages (filename parsed as `<pkg>_<version>_<arch>.deb`) "
        "and apt config snippets, plus the optional intranet mirror the UI "
        "advertises. Entries with a null `download_url` are metadata only."
    ),
    tags=["Hub"],
    responses={
        "200": ok("Local debian catalog", _DEBIAN_SCHEMA),
        **errors("401", "403", "500"),
    },
)
def debian_catalog_api():
    return jsonify(_debian_payload())


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
