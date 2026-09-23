"""Python-build-standalone routes — CPython mirror for uv.

⚠ Decorator order is load-bearing.  Decorators apply bottom-up, so
``@python_build_bp.route`` must be the **topmost** one: anything above it is
applied *after* the view has already been registered and is silently dropped.

    @python_build_bp.route(...)   # applied last  → registers the guarded view
    @require_auth()               # applied in the middle
    @api_operation(...)           # applied first → @wraps propagates the metadata
    def view(): ...

An earlier revision had ``@require_auth()`` above ``@route``, which registered
the *unguarded* function and left every ``/python-builds/*`` route readable
without credentials.  ``scripts/check_auth_guards.py`` fails the build if that
pattern comes back.
"""

from __future__ import annotations

import logging
from pathlib import Path

from flask import (
    Blueprint, current_app, jsonify, render_template,
    send_from_directory, url_for,
)

from config import settings
from auth.decorators import require_permission
from auth.permissions import BUILD_DOWNLOAD, BUILD_READ, BUILD_SHA256
from openapi import api_operation, binary, errors, ok
from services import build_mirror
from services.format import human_size

logger = logging.getLogger("cpypiserver.python_build")
python_build_bp = Blueprint("python_build", __name__)


def _index():
    return current_app.extensions.get("python_build_index")


def _payload(idx) -> dict:
    """The build catalog the SPA's `/packages` page renders."""
    prefix = settings.server.route_prefix.rstrip("/") + "/python-builds"
    return build_mirror.python_catalog(
        idx,
        url_prefix=prefix,
        mirror_url=url_for("python_build.discovery", _external=True),
        index_url=url_for("python_build.discovery", _external=True),
        exists=Path(settings.storage.python_builds_dir).is_dir(),
    )


@python_build_bp.route("/python-builds/")
@require_permission(BUILD_READ)
@api_operation(
    summary="Available CPython builds",
    description=(
        "Release tags of the prebuilt CPython mirror, as an HTML page of links — "
        "the layout `uv python install` expects when "
        "`UV_PYTHON_INSTALL_MIRROR` points here.\n\n"
        "A wire protocol for `uv`, not a browsing page."
    ),
    tags=["Python builds"],
    responses={
        "200": {"description": "Release listing", "content": {"text/html": {}}},
        **errors("401", "404", "500"),
    },
)
def discovery():
    idx = _index()
    if idx is None:
        return "<h1>Python builds not configured</h1>", 404
    snapshot = idx.get_snapshot()
    s = idx.stats()
    base_url = url_for("python_build.discovery", _external=True).rstrip("/")
    sorted_releases = sorted(snapshot.items(), key=lambda kv: kv[0], reverse=True)
    return render_template(
        "python/build_discovery.html",
        server_name=settings.server.server_name,
        base_url=base_url,
        releases=len(snapshot),
        file_count=s["files"],
        releases_dict=dict(sorted_releases),
    )


@python_build_bp.route("/python-builds/<release_tag>/")
@require_permission(BUILD_READ)
@api_operation(
    summary="Builds within one release",
    description="Every artifact published for a release tag, as an HTML page of links.",
    tags=["Python builds"],
    responses={
        "200": {"description": "Artifact listing", "content": {"text/html": {}}},
        **errors("401", "404", "500"),
    },
)
def release_page(release_tag: str):
    idx = _index()
    if idx is None:
        return "<h1>Python builds not configured</h1>", 404
    files = idx.get_files_for_release(release_tag)
    if files is None:
        return "<h1>Release not found</h1>", 404
    return render_template(
        "python/build_release.html",
        server_name=settings.server.server_name,
        release_tag=release_tag,
        file_count=len(files),
        files=files,
    )


@python_build_bp.route("/python-builds/<release_tag>/<filename>")
@require_permission(BUILD_DOWNLOAD)
@api_operation(
    summary="Download a CPython build",
    description="Streams one prebuilt interpreter archive.",
    tags=["Python builds"],
    responses={
        "200": binary("The requested build archive"),
        **errors("401", "404", "500"),
    },
)
def download(release_tag: str, filename: str):
    idx = _index()
    if idx is None:
        return jsonify({"error": "Python builds not configured"}), 404
    f = idx.get_file(release_tag, filename)
    if f is None:
        return jsonify({"error": "Build not found in that release"}), 404
    builds_dir = Path(settings.storage.python_builds_dir) / release_tag
    resp = send_from_directory(str(builds_dir), filename)
    resp.headers.pop("Content-Encoding", None)
    resp.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    return resp


@python_build_bp.route("/python-builds/health")
@api_operation(
    summary="CPython mirror status",
    description=(
        "Counts and sizes of the mirror. Reports `status: disabled` rather than "
        "failing when no builds directory is configured, and needs no credentials."
    ),
    tags=["Python builds"],
    security=[],
    responses={"200": ok("Mirror status", "BuildMirrorHealth"), **errors("500")},
)
def health():
    idx = _index()
    if idx is None:
        return jsonify({"status": "disabled", "reason": "index not initialized"}), 200
    s = idx.stats()
    return jsonify({
        "status": "ok", "builds_dir": settings.storage.python_builds_dir,
        **s, "total_size_human": human_size(s["total_size"]),
    })


@python_build_bp.route("/python-builds/<release_tag>/<filename>/sha256")
@require_permission(BUILD_SHA256)
@api_operation(
    summary="Checksum of one build",
    description="SHA-256 of a build archive, for verifying a download.",
    tags=["Python builds"],
    responses={
        "200": ok("Checksum", "BuildChecksum"),
        **errors("401", "404", "500"),
    },
)
def sha256(release_tag: str, filename: str):
    idx = _index()
    if idx is None:
        return jsonify({"error": "Python builds not configured"}), 404
    f = idx.get_file(release_tag, filename)
    if f is None:
        return jsonify({"error": "Build not found"}), 404
    digest = idx.get_sha256(f)
    return jsonify({"filename": f.filename, "release_tag": f.release_tag, "version": f.version, "sha256": digest, "size": f.size})


# ── JSON API for the SPA ─────────────────────────────────────────────

@python_build_bp.route("/api/v1/python-builds")
@require_permission(BUILD_READ)
@api_operation(
    summary="CPython build catalog",
    description=(
        "Every mirrored `python-build-standalone` release and archive, shaped "
        "for the SPA's `/packages` page: the CPython counterpart of "
        "`GET /api/v1/node-builds`, and the same document the node page shows "
        "under its build tab. `mirror_url` and `env_var` are what a client "
        "should copy to install an interpreter from this server."
    ),
    tags=["Python builds"],
    responses={
        "200": ok("CPython build catalog", build_mirror.CATALOG_SCHEMA),
        **errors("401", "403", "500"),
    },
)
def catalog():
    idx = _index()
    if idx is None:
        return jsonify({
            "kind": "python", "root": settings.storage.python_builds_dir,
            "exists": False, "url_prefix": "", "mirror_url": "", "index_url": "",
            "env_var": "UV_PYTHON_INSTALL_MIRROR", "client": "uv",
            "releases": [], "release_count": 0, "file_count": 0,
            "total_size": 0, "total_size_human": "0 B",
        })
    return jsonify(_payload(idx))
