"""Node.js build mirror routes — a ``nodejs.org/dist`` clone for nvm/fnm.

The Node counterpart of :mod:`routes.python_build`.  Where that module serves
``python-build-standalone`` for ``uv python install``, this one serves the
``nodejs.org/dist`` shape that ``nvm``, ``fnm`` and ``node-gyp`` expect when
their mirror is pointed here::

    GET /node-builds/                              release listing (HTML or JSON)
    GET /node-builds/index.json                    version list for `nvm ls-remote`/fnm
    GET /node-builds/index.tab                     same list, node's tab-separated form
    GET /node-builds/<tag>/                         one release
    GET /node-builds/<tag>/SHASUMS256.txt           checksums (mirrored or generated)
    GET /node-builds/<tag>/<filename>               download one archive
    GET /node-builds/<tag>/<filename>/sha256        checksum of one archive
    GET /node-builds/health                         mirror status (public probe)
    GET /api/v1/node-builds                         catalog document for the SPA

``<tag>`` may also be the alias ``latest`` or ``latest-v20.x``, which is how
``nvm install node`` and ``nvm install 20`` resolve a version before they know
its number.

Client setup::

    export NVM_NODEJS_ORG_MIRROR=http://<openfish>/node-builds
    nvm install 20

⚠ Decorator order is load-bearing (see ``routes/python_build.py``): the
``@node_build_bp.route`` decorator must be the topmost line, or the guard is
applied after registration and never runs.  ``scripts/check_auth_guards.py``
enforces this.
"""

from __future__ import annotations

import logging
from pathlib import Path

from flask import (
    Blueprint, Response, current_app, jsonify, render_template_string,
    send_from_directory, url_for,
)

from auth.decorators import require_permission
from auth.permissions import (
    NODE_BUILD_DOWNLOAD, NODE_BUILD_READ, NODE_BUILD_SHA256,
)
from config import settings
from openapi import api_operation, binary, errors, html, ok
from routes.hub_common import wants_json
from services import build_mirror, templates

logger = logging.getLogger("cpypiserver.node_build")
node_build_bp = Blueprint("node_build", __name__)

#: Auxiliary files node publishes next to the archives.  The index tracks only
#: archives, so these are served straight from the release directory when a
#: full ``nodejs.org/dist`` was mirrored; ``send_from_directory`` makes the
#: path traversal-safe and this allowlist keeps it to files we mean to expose.
_AUX_FILES = frozenset({"SHASUMS256.txt", "SHASUMS256.txt.asc", "SHASUMS256.txt.sig"})

_RELEASE_TAG_PARAM = {
    "name": "release_tag",
    "in": "path",
    "required": True,
    "description": "A release tag (`v20.11.0`), or the alias `latest` / `latest-v20.x`.",
    "schema": {"type": "string"},
}


def _index():
    return current_app.extensions.get("node_build_index")


def _builds_dir() -> Path:
    return Path(settings.storage.node_builds_dir)


def _resolve(idx, tag: str) -> str | None:
    """Map a requested tag (possibly ``latest``/``latest-v20.x``) to a release."""
    if idx.get_files_for_release(tag) is not None:
        return tag
    return idx.resolve_alias(tag)


def _not_found(what: str):
    return jsonify({"error": what}), 404


def _payload(idx) -> dict:
    prefix = settings.server.route_prefix.rstrip("/") + "/node-builds"
    return build_mirror.node_catalog(
        idx,
        url_prefix=prefix,
        mirror_url=url_for("node_build.discovery", _external=True),
        index_url=url_for("node_build.index_json", _external=True),
        exists=_builds_dir().is_dir(),
    )


# ── Discovery: release listing + machine-facing indexes ──────────────

@node_build_bp.route("/node-builds/")
@require_permission(NODE_BUILD_READ)
@api_operation(
    summary="Available Node.js builds",
    description=(
        "Release tags of the ``nodejs.org/dist`` mirror, as an HTML page of "
        "links — the layout `nvm`, `fnm` and `node-gyp` expect when "
        "`NVM_NODEJS_ORG_MIRROR` points here.\n\n"
        "Pass `?format=json` (or `Accept: application/json`) for the same "
        "catalog `GET /api/v1/node-builds` returns. A wire protocol for Node "
        "tooling, not a browsing page."
    ),
    tags=["Node builds"],
    responses={
        "200": {
            "description": "Release listing as HTML or JSON",
            "content": {
                "text/html": {},
                "application/json": {"schema": build_mirror.CATALOG_SCHEMA},
            },
        },
        **errors("401", "403", "500"),
    },
)
def discovery():
    idx = _index()
    if idx is None:
        return "<h1>Node builds not configured</h1>", 404
    if wants_json():
        return jsonify(_payload(idx))
    snapshot = idx.get_snapshot()
    s = idx.stats()
    base_url = url_for("node_build.discovery", _external=True).rstrip("/")
    # `get_releases()` is newest-first; preserve that order in the rendered
    # document so the page reads top-down from the most recent release.
    releases_dict = {tag: snapshot[tag] for tag in idx.get_releases() if tag in snapshot}
    return render_template_string(
        templates.node_build_discovery(),
        server_name=settings.server.server_name,
        base_url=base_url,
        releases=len(snapshot),
        file_count=s["files"],
        releases_dict=releases_dict,
    )


@node_build_bp.route("/node-builds/index.json")
@require_permission(NODE_BUILD_READ)
@api_operation(
    summary="Node version index (index.json)",
    description=(
        "Every mirrored release in node's own `index.json` shape: "
        "`version`, `files` and — when an authentic `index.json` was synced "
        "alongside the archives — `date`, `lts`, `npm` and the rest.\n\n"
        "Only releases present on disk are listed, so `nvm ls-remote` can never "
        "offer a version this mirror would then 404."
    ),
    tags=["Node builds"],
    responses={
        "200": {
            "description": "Version list, node's index.json shape",
            "content": {"application/json": {"schema": {"type": "array", "items": {"type": "object"}}}},
        },
        **errors("401", "403", "500"),
    },
)
def index_json():
    idx = _index()
    if idx is None:
        return jsonify([])
    return jsonify(idx.index_document())


@node_build_bp.route("/node-builds/index.tab")
@require_permission(NODE_BUILD_READ)
@api_operation(
    summary="Node version index (index.tab)",
    description=(
        "The same version list as `index.json`, in node's tab-separated form. "
        "`nvm` prefers this document when it exists and falls back to "
        "`index.json` when it does not."
    ),
    tags=["Node builds"],
    responses={
        "200": {
            "description": "Version list, node's index.tab shape",
            "content": {"text/plain": {}},
        },
        **errors("401", "403", "500"),
    },
)
def index_tab():
    idx = _index()
    if idx is None:
        return Response("", mimetype="text/plain")
    return Response(idx.index_tab(), mimetype="text/plain")


@node_build_bp.route("/node-builds/health")
@api_operation(
    summary="Node mirror status",
    description=(
        "Counts and sizes of the mirror. Reports `status: disabled` rather than "
        "failing when no builds directory is configured, and needs no credentials."
    ),
    tags=["Node builds"],
    security=[],
    responses={"200": ok("Mirror status", "BuildMirrorHealth"), **errors("500")},
)
def health():
    idx = _index()
    if idx is None:
        return jsonify({"status": "disabled", "reason": "index not initialized"}), 200
    s = idx.stats()
    return jsonify({
        "status": "ok", "builds_dir": settings.storage.node_builds_dir,
        **s, "total_size_human": f"{s['total_size'] / 1024 / 1024 / 1024:.1f} GB",
    })


# ── One release: page, checksums, downloads ──────────────────────────

@node_build_bp.route("/node-builds/<release_tag>/")
@require_permission(NODE_BUILD_READ)
@api_operation(
    summary="Builds within one release",
    description=(
        "Every archive published for a release tag, as an HTML page of links. "
        "`latest` and `latest-v20.x` resolve to a concrete release."
    ),
    tags=["Node builds"],
    parameters=[_RELEASE_TAG_PARAM],
    responses={"200": html("Artifact listing"), **errors("401", "404", "500")},
)
def release_page(release_tag: str):
    idx = _index()
    if idx is None:
        return "<h1>Node builds not configured</h1>", 404
    resolved = _resolve(idx, release_tag)
    if resolved is None:
        return f"<h1>Release '{release_tag}' not found</h1>", 404
    files = idx.get_files_for_release(resolved)
    if files is None:
        return f"<h1>Release '{release_tag}' not found</h1>", 404
    return render_template_string(
        templates.node_build_release(),
        server_name=settings.server.server_name,
        release_tag=resolved,
        file_count=len(files),
        files=files,
    )


@node_build_bp.route("/node-builds/<release_tag>/SHASUMS256.txt")
@require_permission(NODE_BUILD_DOWNLOAD)
@api_operation(
    summary="Checksums for one release",
    description=(
        "The `SHASUMS256.txt` a client verifies a download against. An "
        "authentic file mirrored next to the archives is served as-is; when it "
        "is absent the document is generated from the archives on disk (hashed "
        "on first access and cached thereafter)."
    ),
    tags=["Node builds"],
    parameters=[_RELEASE_TAG_PARAM],
    responses={
        "200": {"description": "SHASUMS256.txt", "content": {"text/plain": {}}},
        **errors("401", "404", "500"),
    },
)
def shasums(release_tag: str):
    idx = _index()
    if idx is None:
        return _not_found("Node builds not configured")
    resolved = _resolve(idx, release_tag)
    if resolved is None:
        return _not_found(f"Release '{release_tag}' not found")
    on_disk = _builds_dir() / resolved / "SHASUMS256.txt"
    if on_disk.is_file():
        return send_from_directory(str(on_disk.parent), on_disk.name, mimetype="text/plain")
    text = idx.shasums(resolved)
    if text is None:
        return _not_found(f"Release '{release_tag}' not found")
    return Response(text, mimetype="text/plain")


@node_build_bp.route("/node-builds/<release_tag>/<filename>")
@require_permission(NODE_BUILD_DOWNLOAD)
@api_operation(
    summary="Download a Node.js build",
    description=(
        "Streams one prebuilt Node.js archive. `latest` / `latest-v20.x` are "
        "resolved first, so a client that only knows the alias can fetch from "
        "the concrete release directory it points at."
    ),
    tags=["Node builds"],
    parameters=[
        _RELEASE_TAG_PARAM,
        {
            "name": "filename",
            "in": "path",
            "required": True,
            "description": "Archive basename, e.g. `node-v20.11.0-linux-x64.tar.xz`.",
            "schema": {"type": "string"},
        },
    ],
    responses={
        "200": binary("The requested build archive"),
        **errors("401", "404", "500"),
    },
)
def download(release_tag: str, filename: str):
    idx = _index()
    if idx is None:
        return _not_found("Node builds not configured")
    resolved = _resolve(idx, release_tag)
    if resolved is None:
        return _not_found(f"Release '{release_tag}' not found")
    release_dir = _builds_dir() / resolved
    if idx.get_file(resolved, filename) is None and filename not in _AUX_FILES:
        return _not_found(f"Build '{filename}' not found in {release_tag}")
    resp = send_from_directory(str(release_dir), filename)
    # The archives are already compressed; letting Flask re-encode them wastes
    # CPU and breaks the byte count a checksum verifies.
    resp.headers.pop("Content-Encoding", None)
    resp.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    return resp


@node_build_bp.route("/node-builds/<release_tag>/<filename>/sha256")
@require_permission(NODE_BUILD_SHA256)
@api_operation(
    summary="Checksum of one build",
    description="SHA-256 of a build archive, for verifying a download.",
    tags=["Node builds"],
    parameters=[
        _RELEASE_TAG_PARAM,
        {
            "name": "filename",
            "in": "path",
            "required": True,
            "description": "Archive basename.",
            "schema": {"type": "string"},
        },
    ],
    responses={
        "200": ok("Checksum", "BuildChecksum"),
        **errors("401", "404", "500"),
    },
)
def sha256(release_tag: str, filename: str):
    idx = _index()
    if idx is None:
        return _not_found("Node builds not configured")
    resolved = _resolve(idx, release_tag)
    if resolved is None:
        return _not_found(f"Release '{release_tag}' not found")
    f = idx.get_file(resolved, filename)
    if f is None:
        return _not_found(f"Build '{filename}' not found")
    return jsonify({
        "filename": f.filename, "release_tag": f.release_tag,
        "version": f.version, "sha256": idx.get_sha256(f), "size": f.size,
    })


# ── JSON API for the SPA ─────────────────────────────────────────────

@node_build_bp.route("/api/v1/node-builds")
@require_permission(NODE_BUILD_READ)
@api_operation(
    summary="Node build catalog",
    description=(
        "Every mirrored Node.js release and archive, shaped for the SPA's "
        "`/npm` page: the same document as `GET /node-builds/?format=json`. "
        "`mirror_url` and `env_var` are what a client should copy to install "
        "from this server."
    ),
    tags=["Node builds"],
    responses={
        "200": ok("Node build catalog", build_mirror.CATALOG_SCHEMA),
        **errors("401", "403", "500"),
    },
)
def catalog():
    idx = _index()
    if idx is None:
        return jsonify({
            "kind": "node", "root": settings.storage.node_builds_dir,
            "exists": False, "url_prefix": "", "mirror_url": "", "index_url": "",
            "env_var": "NVM_NODEJS_ORG_MIRROR", "client": "nvm / fnm",
            "releases": [], "release_count": 0, "file_count": 0,
            "total_size": 0, "total_size_human": "0 B",
        })
    return jsonify(_payload(idx))


__all__ = ["node_build_bp"]
