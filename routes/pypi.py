"""*** package routes — PEP 503 Simple Repository API."""

import hashlib
import json as _json
import logging
import re
import tempfile
from pathlib import Path

from flask import (
    Blueprint, Response, current_app, g, jsonify,
    render_template_string, request, send_from_directory, url_for,
)
from flask_pydantic import validate

from config import settings
from errors import BadRequestError, PackageNotFoundError, UploadConflictError
from schemas import FormatQuery
from services.validation import validate_file
from index.packages import normalize_package_name
from index.base import store_digest
from services import templates

logger = logging.getLogger("cpypiserver")
pypi_bp = Blueprint("pypi", __name__)


@pypi_bp.route("/simple/")
@validate(query=FormatQuery)
def simple_index(query: FormatQuery):
    packages = current_app.extensions["pypi_index"].get_snapshot()
    names = sorted(packages)
    if _wants_json(request.headers.get("Accept", ""), query.format):
        return _json_response({"meta": {"api-version": "1.0"}, "projects": [{"name": n} for n in names]})
    return render_template_string(templates.pypi_simple_index(), package_names=names)


@pypi_bp.route("/simple/<package_name>/")
@validate(query=FormatQuery)
def package_page(query: FormatQuery, package_name: str):
    files = current_app.extensions["pypi_index"].get_files(package_name)
    if files is None:
        raise PackageNotFoundError(package_name)
    pkg_index = current_app.extensions["pypi_index"]
    accept = request.headers.get("Accept", "")
    if _wants_json(accept, query.format):
        return _json_response({
            "meta": {"api-version": "1.0"},
            "name": normalize_package_name(package_name),
            "files": [_file_json(f, pkg_index) for f in files],
        })
    for f in files:
        pkg_index.get_sha256(f)
    return render_template_string(templates.pypi_simple_package(), package_name=normalize_package_name(package_name), files=files)


@pypi_bp.route("/packages/<path:filename>")
def serve_package(filename: str):
    _record_download(filename)
    resp = send_from_directory(settings.storage.packages_dir, filename)
    resp.headers.pop("Content-Encoding", None)
    return resp


@pypi_bp.route("/simple/<package_name>/<filename>")
def serve_package_from_simple(package_name: str, filename: str):
    _record_download(filename)
    resp = send_from_directory(settings.storage.packages_dir, filename)
    resp.headers.pop("Content-Encoding", None)
    return resp


@pypi_bp.route("/", methods=["POST"])
@pypi_bp.route("/legacy/", methods=["POST"])
def upload():
    if "content" not in request.files:
        raise BadRequestError("Missing 'content' field")
    file = request.files["content"]
    if not file or not file.filename:
        raise BadRequestError("No file selected")
    is_valid, result = validate_file(file, file.filename)
    if not is_valid:
        raise BadRequestError(result)
    safe_filename = result
    dest = Path(settings.storage.packages_dir) / safe_filename

    # ── Stream to temp file while computing SHA256 ──────────────────
    _CHUNK = 8 << 20  # 8 MB
    digester = hashlib.sha256()
    with tempfile.NamedTemporaryFile(dir=settings.storage.packages_dir, delete=False) as tmp:
        tmp_path = Path(tmp.name)
        while True:
            chunk = file.stream.read(_CHUNK)
            if not chunk:
                break
            digester.update(chunk)
            tmp.write(chunk)
    try:
        if dest.exists() and not settings.storage.overwrite:
            # twine 1.7.0+ expects HTTP 400 to match pypi.org compatibility;
            # other clients (pip, setuptools) get the standard 409 Conflict.
            ua = request.headers.get("User-Agent", "")
            if "twine" in ua:
                raise BadRequestError(f"Package '{safe_filename}' already exists (set overwrite=1 to allow)")
            raise UploadConflictError(safe_filename)
        tmp_path.rename(dest)
        store_digest(dest, digester.hexdigest())
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
    _record_upload(safe_filename)
    return Response("Success", status=200)


# ── Helpers ──────────────────────────────────────────────────────────

def _file_json(f, index) -> dict:
    result: dict = {
        "filename": f.filename,
        "url": url_for("pypi.serve_package", filename=f.filename, _external=True),
        "hashes": {"sha256": index.get_sha256(f) if index else (f.sha256_digest or "")},
        "requires-python": f.requires_python or "",
        "size": f.size,
    }
    if f.upload_time:
        result["upload-time"] = f.upload_time
    return result


def _json_response(data: dict) -> Response:
    return Response(_json.dumps(data), mimetype="application/vnd.pypi.simple.v1+json")


def _wants_json(accept: str, fmt: str | None) -> bool:
    return fmt == "json" or "application/vnd.pypi.simple.v1+json" in accept


def _key_id() -> str | None:
    u = getattr(g, "auth_user", None)
    return u.get("key_id") if isinstance(u, dict) else None


def _extract_pkg(filename: str) -> str:
    m = re.match(r"^([A-Za-z0-9_.]+)-\d", filename)
    return m.group(1).replace("_", "-").lower() if m else filename.rsplit(".", 1)[0]


def _record_download(filename: str) -> None:
    kid = _key_id()
    if not kid: return
    try:
        mgr = current_app.extensions.get("api_key_manager")
        if mgr: mgr.record_download(kid, _extract_pkg(filename))
    except Exception: pass


def _record_upload(filename: str) -> None:
    kid = _key_id()
    if not kid: return
    try:
        mgr = current_app.extensions.get("api_key_manager")
        if mgr: mgr.record_upload(kid, _extract_pkg(filename))
    except Exception: pass
