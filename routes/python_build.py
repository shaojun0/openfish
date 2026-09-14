"""Python-build-standalone routes — CPython mirror for uv."""

import logging
from pathlib import Path

from flask import (
    Blueprint, current_app, jsonify, render_template_string,
    send_from_directory, url_for,
)

from config import settings
from auth.decorators import require_auth
from services import templates

logger = logging.getLogger("cpypiserver.python_build")
python_build_bp = Blueprint("python_build", __name__)


def _index():
    return current_app.extensions.get("python_build_index")


@require_auth()
@python_build_bp.route("/python-builds/")
def discovery():
    idx = _index()
    if idx is None:
        return "<h1>Python builds not configured</h1>", 404
    snapshot = idx.get_snapshot()
    s = idx.stats()
    base_url = url_for("python_build.discovery", _external=True).rstrip("/")
    sorted_releases = sorted(snapshot.items(), key=lambda kv: kv[0], reverse=True)
    return render_template_string(
        templates.build_discovery(),
        server_name=settings.server.server_name,
        base_url=base_url,
        releases=len(snapshot),
        file_count=s["files"],
        releases_dict=dict(sorted_releases),
    )


@require_auth()
@python_build_bp.route("/python-builds/<release_tag>/")
def release_page(release_tag: str):
    idx = _index()
    if idx is None:
        return "<h1>Python builds not configured</h1>", 404
    files = idx.get_files_for_release(release_tag)
    if files is None:
        return f"<h1>Release '{release_tag}' not found</h1>", 404
    return render_template_string(
        templates.build_release(),
        server_name=settings.server.server_name,
        release_tag=release_tag,
        file_count=len(files),
        files=files,
    )


@require_auth()
@python_build_bp.route("/python-builds/<release_tag>/<filename>")
def download(release_tag: str, filename: str):
    idx = _index()
    if idx is None:
        return jsonify({"error": "Python builds not configured"}), 404
    f = idx.get_file(release_tag, filename)
    if f is None:
        return jsonify({"error": f"Build '{filename}' not found in {release_tag}"}), 404
    builds_dir = Path(settings.storage.python_builds_dir) / release_tag
    resp = send_from_directory(str(builds_dir), filename)
    resp.headers.pop("Content-Encoding", None)
    resp.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
    return resp


@python_build_bp.route("/python-builds/health")
def health():
    idx = _index()
    if idx is None:
        return jsonify({"status": "disabled", "reason": "index not initialized"}), 200
    s = idx.stats()
    return jsonify({
        "status": "ok", "builds_dir": settings.storage.python_builds_dir,
        **s, "total_size_human": f"{s['total_size'] / 1024 / 1024 / 1024:.1f} GB",
    })


@require_auth()
@python_build_bp.route("/python-builds/<release_tag>/<filename>/sha256")
def sha256(release_tag: str, filename: str):
    idx = _index()
    if idx is None:
        return jsonify({"error": "Python builds not configured"}), 404
    f = idx.get_file(release_tag, filename)
    if f is None:
        return jsonify({"error": f"Build '{filename}' not found"}), 404
    digest = idx.get_sha256(f)
    return jsonify({"filename": f.filename, "release_tag": f.release_tag, "version": f.version, "sha256": digest, "size": f.size})
