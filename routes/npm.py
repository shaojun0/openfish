"""npm routes — the registry protocol plus the browsable catalog.

npm is JSON-first: there is no HTML index in the protocol, so what this module
serves is the *registry wire protocol* a client such as ``npm`` or ``pnpm``
speaks, with a server-rendered catalog page on top for humans and scripts.

Wire protocol
-------------
``GET /npm/<package>``                 packument (full or abbreviated)
``GET /npm/<package>/<version>``       one version manifest
``GET /npm/<package>/-/<filename>``    tarball a packument points at
``GET /npm/-/v1/search``               search (the modern replacement for /-/all)
``GET /npm/-/ping``                    health probe every client calls first
``GET /npm/-/all``                     legacy full index

Catalog
-------
``GET /npm/``                          browsable index (HTML, or `/-/all` JSON)
``GET /npm/files/<filename>``          download a local tarball
``GET /api/v1/npm``                    catalog document for the SPA

Sources are layered, cheapest first: tarballs and ``catalog.json`` entries in
``NPM_DIR`` are the local truth, and when ``NPM_UPSTREAM`` is configured an
unknown package is fetched from it and cached, so a client that can reach this
server can install anything the upstream mirror has.

⚠ Decorator order is load-bearing: ``@npm_bp.route`` must be the topmost line,
or the guard is applied after registration and never runs.
``scripts/check_auth_guards.py`` enforces this.
"""

from __future__ import annotations

from flask import (
    Blueprint, jsonify, render_template_string, request, send_file,
    send_from_directory, url_for,
)
from werkzeug.routing import BaseConverter

from auth.decorators import require_permission
from auth.permissions import NPM_READ
from config import settings
from openapi import api_operation, binary, errors, json_body, ok
from routes.hub_common import spa_url, wants_json
from services import hub, templates
from services.npm_registry import (
    ABBREVIATED_ACCEPT, SEARCH_MAX_SIZE, NpmRegistry, clamp_search_size,
)


class _PackageConverter(BaseConverter):
    """One path element that is either ``name`` or ``@scope/name``.

    This is what resolves npm's scoped-route ambiguity deterministically.
    Werkzeug otherwise has both ``/npm/<package>/<version>`` and a scoped
    ``/npm/@<scope>/<name>`` matching ``/npm/@types/node``, and leaves the
    winner to rule weights. Making the *package* element itself own the
    ``@scope/name`` pair — with ``[^/]`` bounded so it can never swallow the
    following ``/<version>`` or ``/-/<filename>`` — removes the overlap
    entirely: ``/npm/@scope/name`` is a one-element packument request and
    ``/npm/@scope/name/1.0.0`` is a two-element manifest request, by grammar.
    Rejecting a leading ``-`` also keeps the legacy ``/-/all``,
    ``/-/ping`` and ``/-/v1/search`` rules unambiguous.
    """

    regex = r"@[^/@]+/[^/@]+|[^/@][^/]*"


npm_bp = Blueprint("npm", __name__)


@npm_bp.record_once
def _install_package_converter(state) -> None:
    """Register the ``pkg`` converter before any rule that uses it is added."""
    state.app.url_map.converters["pkg"] = _PackageConverter

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

_NPM_ALL_SCHEMA = {
    "type": "object",
    "additionalProperties": {"type": "object"},
    "description": "Keyed by package name, npm's legacy `/-/all` shape.",
}

_NPM_PACKUMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "dist-tags": {"type": "object", "additionalProperties": {"type": "string"}},
        "versions": {"type": "object", "additionalProperties": {"type": "object"}},
        "modified": {"type": "string"},
        "description": {"type": "string"},
        "keywords": {"type": "array", "items": {"type": "string"}},
    },
}

_NPM_MANIFEST_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "version": {"type": "string"},
        "dependencies": {"type": "object"},
        "optionalDependencies": {"type": "object"},
        "peerDependencies": {"type": "object"},
        "bin": {"type": "object"},
        "dist": {
            "type": "object",
            "properties": {
                "tarball": {"type": "string"},
                "shasum": {"type": "string"},
                "integrity": {"type": "string"},
            },
        },
    },
}

_NPM_SEARCH_SCHEMA = {
    "type": "object",
    "properties": {
        "objects": {"type": "array", "items": {"type": "object"}},
        "total": {"type": "integer"},
        "time": {"type": "string"},
    },
}

#: OpenAPI parameter objects for the protocol routes. Declared explicitly so
#: converters (``<pkg:package>``) still document a readable parameter.
_PACKAGE_PARAM = {
    "name": "package",
    "in": "path",
    "required": True,
    "description": "Package name — `left-pad`, or the scoped form `@scope/name`.",
    "schema": {"type": "string"},
}
_VERSION_PARAM = {
    "name": "version",
    "in": "path",
    "required": True,
    "description": "An exact version, or a dist-tag such as `latest`.",
    "schema": {"type": "string"},
}
_FILENAME_PARAM = {
    "name": "filename",
    "in": "path",
    "required": True,
    "description": "The tarball's basename, the last segment of `dist.tarball`.",
    "schema": {"type": "string"},
}
_SEARCH_PARAMS = [
    {
        "name": "text",
        "in": "query",
        "required": False,
        "description": "Search terms. Empty means an empty result set, not an error.",
        "schema": {"type": "string", "default": ""},
    },
    {
        "name": "size",
        "in": "query",
        "required": False,
        "description": f"Results to return, clamped to 1..{SEARCH_MAX_SIZE} (default 20).",
        "schema": {"type": "integer", "minimum": 1, "maximum": SEARCH_MAX_SIZE, "default": 20},
    },
    {
        "name": "from",
        "in": "query",
        "required": False,
        "description": "Offset into the merged result set; negatives are clamped to 0.",
        "schema": {"type": "integer", "minimum": 0, "default": 0},
    },
]

#: One registry per distinct configuration. Reusing it keeps the pooled
#: ``requests`` session across requests; the key makes a settings change (as the
#: offline gate makes) produce a fresh one.
_REGISTRY_CACHE: dict[tuple, NpmRegistry] = {}


def _registry() -> NpmRegistry:
    hub_settings = settings.hub
    key = (
        str(hub_settings.npm_dir),
        str(hub_settings.npm_upstream),
        bool(hub_settings.npm_proxy_enabled),
        str(hub_settings.npm_upstream_token),
        float(hub_settings.npm_timeout),
        str(hub_settings.npm_cache_dir),
        int(hub_settings.npm_cache_max_mb),
    )
    registry = _REGISTRY_CACHE.get(key)
    if registry is None:
        registry = NpmRegistry(
            root=hub_settings.npm_dir,
            upstream_url=hub_settings.npm_upstream,
            proxy_enabled=hub_settings.npm_proxy_enabled,
            token=hub_settings.npm_upstream_token,
            timeout=hub_settings.npm_timeout,
            cache_dir=hub_settings.npm_cache_dir,
            cache_max_bytes=hub_settings.npm_cache_max_mb * 1024 * 1024,
        )
        _REGISTRY_CACHE.clear()
        _REGISTRY_CACHE[key] = registry
    return registry


def _tarball_url(package: str, filename: str) -> str:
    """Absolute URL on THIS server for one tarball (honours the route prefix)."""
    return url_for("npm.npm_tarball", package=package, filename=filename, _external=True)


def _package_url(package: str) -> str:
    """Absolute packument URL, used as `links.npm` in search results."""
    return url_for("npm.npm_packument", package=package, _external=True)


def _lookup_error(what: str, reason: str | None):
    """404 for a genuine miss, 502 for an upstream outage — never a 500."""
    if reason in (None, "notfound", "proxy-disabled"):
        return jsonify({"error": f"{what} not found"}), 404
    return jsonify({"error": f"upstream npm registry unavailable: {reason}"}), 502


def _npm_payload() -> dict:
    prefix = settings.server.route_prefix.rstrip("/") + "/npm/files"
    return hub.scan_npm(
        settings.hub.npm_dir,
        upstream=settings.hub.npm_upstream,
        url_prefix=prefix,
    )


# ── Catalog: browsable index + legacy conventions + download ─────────

@npm_bp.route("/npm/")
@require_permission(NPM_READ)
@api_operation(
    summary="npm catalog index",
    description=(
        "The local npm directory as a browsable index. HTML by default; the "
        "JSON form (`?format=json` or `Accept: application/json`) returns the "
        "same legacy `/-/all` document as `GET /npm/-/all`.\n\n"
        "The registry protocol itself lives at `GET /npm/<package>`; this page "
        "is the human-facing view of what is cached locally."
    ),
    tags=["npm"],
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
    if wants_json():
        return jsonify(document)
    return render_template_string(
        templates.npm_index(),
        server_name=settings.server.server_name,
        base_url=url_for("npm.npm_index", _external=True),
        spa_url=spa_url("/npm"),
        upstream=catalog["upstream"],
        packages=catalog["packages"],
        package_count=catalog["package_count"],
        all_url=url_for("npm.npm_all"),
        ping_url=url_for("npm.npm_ping"),
        updated=document["_updated"],
    )


@npm_bp.route("/npm/-/all")
@require_permission(NPM_READ)
@api_operation(
    summary="npm full index (legacy /-/all)",
    description=(
        "Every locally known package as one JSON object, in the shape npm's "
        "shut-down `GET /-/all` endpoint used: keyed by package name, each value "
        "carrying `dist-tags` and the versions that actually have a tarball on "
        "disk. The modern replacements are `GET /npm/-/v1/search` and the "
        "replication feed, both of which this server also answers."
    ),
    tags=["npm"],
    responses={
        "200": ok("Full index, keyed by package name", _NPM_ALL_SCHEMA),
        **errors("401", "403", "500"),
    },
)
def npm_all():
    return jsonify(hub.npm_all_index(_npm_payload()))


@npm_bp.route("/npm/-/ping")
@require_permission(NPM_READ)
@api_operation(
    summary="npm ping",
    description=(
        "The npm registry health convention: an empty JSON object. `npm ping` "
        "calls this before anything else, so a registry that answers it looks "
        "alive to the client."
    ),
    tags=["npm"],
    responses={"200": {"description": "Always `{}`", "content": json_body()},
               **errors("401", "403")},
)
def npm_ping():
    return jsonify({})


# ── Registry protocol: packuments, manifests, tarballs, search ───────

@npm_bp.route("/npm/-/v1/search")
@require_permission(NPM_READ)
@api_operation(
    summary="npm search (/-/v1/search)",
    description=(
        "The modern npm search endpoint, in npm's exact shape: `objects` of "
        "`{package, score, searchScore}`, a `total` and an ISO-8601 `time`.\n\n"
        "Local packages (name, description and tags, case-insensitive) are "
        "merged with the upstream registry's matches, deduplicated by name with "
        "the local hit first. An unreachable upstream degrades to local-only "
        "results; an empty `text` is an empty result set, not an error."
    ),
    tags=["npm"],
    parameters=_SEARCH_PARAMS,
    responses={
        "200": ok("Search results", _NPM_SEARCH_SCHEMA),
        **errors("401", "403", "500"),
    },
)
def npm_search():
    try:
        offset = int(request.args.get("from", 0))
    except (TypeError, ValueError):
        offset = 0
    document = _registry().search(
        request.args.get("text", ""),
        size=clamp_search_size(request.args.get("size")),
        from_=offset,
        package_url=_package_url,
        publisher=settings.server.server_name,
    )
    return jsonify(document)


@npm_bp.route("/npm/<pkg:package>")
@require_permission(NPM_READ)
@api_operation(
    summary="npm packument",
    description=(
        "One package's registry document: `name`, `dist-tags`, every published "
        "`versions` entry and `modified`. A client asking for "
        "`Accept: application/vnd.npm.install-v1+json` gets npm's abbreviated "
        "install form, anything else the full form.\n\n"
        "Every `dist.tarball` is rewritten to an absolute URL on this server, so "
        "installs resolve here and never need the upstream mirror. Sources are "
        "layered: a local `*.tgz`, then `NPM_DIR/catalog.json`, then — when the "
        "proxy is enabled — the upstream registry (cached briefly). An unknown "
        "package is a JSON `404`."
    ),
    tags=["npm"],
    parameters=[_PACKAGE_PARAM],
    responses={
        "200": ok("Packument (full or abbreviated)", _NPM_PACKUMENT_SCHEMA),
        **errors("401", "403", "404", "502"),
    },
)
def npm_packument(package: str):
    abbreviated = ABBREVIATED_ACCEPT in request.headers.get("Accept", "")
    document, reason = _registry().packument(
        package, tarball_url=_tarball_url, abbreviated=abbreviated,
    )
    if document is None:
        return _lookup_error(f"package '{package}'", reason)
    return jsonify(document)


@npm_bp.route("/npm/<pkg:package>/<version>")
@require_permission(NPM_READ)
@api_operation(
    summary="npm version manifest",
    description=(
        "The `versions[<version>]` object of a package, with `name` added. "
        "`<version>` may be an exact version or a dist-tag such as `latest`. "
        "A version the packument does not list is a JSON `404`."
    ),
    tags=["npm"],
    parameters=[_PACKAGE_PARAM, _VERSION_PARAM],
    responses={
        "200": ok("One version manifest", _NPM_MANIFEST_SCHEMA),
        **errors("401", "403", "404", "502"),
    },
)
def npm_version(package: str, version: str):
    manifest = _registry().version_manifest(package, version, tarball_url=_tarball_url)
    if manifest is None:
        return _lookup_error(f"package '{package}' version '{version}'", "notfound")
    return jsonify(manifest)


@npm_bp.route("/npm/<pkg:package>/-/<path:filename>")
@require_permission(NPM_READ)
@api_operation(
    summary="npm tarball",
    description=(
        "Streams the `.tgz` a packument's `dist.tarball` points at. A local file "
        "in `NPM_DIR` is served first; otherwise the tarball is fetched from the "
        "upstream registry, written to the disk cache (inside the "
        "`npm_cache_max_mb` budget) and served from there, so the second request "
        "for the same tarball needs no network."
    ),
    tags=["npm"],
    parameters=[_PACKAGE_PARAM, _FILENAME_PARAM],
    responses={
        "200": binary("The requested npm tarball"),
        **errors("401", "403", "404", "502"),
    },
)
def npm_tarball(package: str, filename: str):
    registry = _registry()
    local = registry.local_tarball(filename)
    if local is not None:
        return send_file(local, mimetype="application/octet-stream", conditional=True)
    path, reason = registry.upstream_tarball(package, filename)
    if path is None:
        return _lookup_error(f"tarball '{filename}'", reason)
    return send_file(path, mimetype="application/octet-stream", conditional=True)


@npm_bp.route("/npm/files/<path:filename>")
@require_permission(NPM_READ)
@api_operation(
    summary="Download a local npm tarball",
    description=(
        "Streams one `*.tgz` out of `NPM_DIR`. Tarballs fetched from the "
        "upstream registry are cached and served from here as well, so the "
        "`dist.tarball` URL a packument advertises always resolves locally."
    ),
    tags=["npm"],
    responses={
        "200": binary("The requested tarball"),
        **errors("401", "403", "404", "500"),
    },
)
def download_npm_file(filename: str):
    return send_from_directory(settings.hub.npm_dir, filename, as_attachment=True)


@npm_bp.route("/api/v1/npm")
@require_permission(NPM_READ)
@api_operation(
    summary="npm catalog",
    description=(
        "Local npm packages known to this server: tarballs sitting in "
        "`NPM_DIR`, entries declared in its `catalog.json`, and packages "
        "already cached from the upstream registry. Entries with a null "
        "`download_url` are metadata only and cannot be fetched."
    ),
    tags=["npm"],
    responses={
        "200": ok("Local npm catalog", _NPM_SCHEMA),
        **errors("401", "403", "500"),
    },
)
def npm_catalog():
    return jsonify(_npm_payload())


__all__ = ["npm_bp"]
