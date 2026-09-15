"""*** package routes — PEP 503 Simple Repository API."""

# NOTE: no `from __future__ import annotations` in this module.  PEP 563
# turns the `query: FormatQuery` annotation into the *string* "FormatQuery",
# and flask-pydantic reads `func.__annotations__["query"]` in preference to
# the explicit `query=` argument and hands it to `issubclass` — which then
# raises TypeError on every request.  This is the one module that opts out.

import hashlib
import json as _json
import logging
import re
import tempfile
from pathlib import Path

from flask import (
    Blueprint, Response, current_app, g,
    render_template, request, send_from_directory, url_for,
)
from flask_pydantic import validate

from auth.decorators import require_permission
from auth.permissions import PACKAGE_READ, PACKAGE_WRITE
from config import settings
from errors import BadRequestError, PackageNotFoundError, UploadConflictError
from openapi import api_operation, binary, errors, ref
from schemas import FormatQuery
from services.validation import validate_file
from index.packages import normalize_package_name
from index.base import store_digest

logger = logging.getLogger("cpypiserver")
pypi_bp = Blueprint("pypi", __name__)


_FORMAT_PARAM = {
    "name": "format",
    "in": "query",
    "required": False,
    "schema": {"type": "string", "enum": ["json"]},
    "description": "Set to `json` for the PEP 691 representation, equivalent to sending the vendor Accept header.",
}

_JSON_ACCEPT = "application/vnd.pypi.simple.v1+json"


@pypi_bp.route("/simple/")
@require_permission(PACKAGE_READ)
@api_operation(
    summary="Simple repository index (PEP 503 / PEP 691)",
    description=(
        "The index that `pip` and `uv` read. Returns HTML by default; send "
        f"`Accept: {_JSON_ACCEPT}` or `?format=json` for the JSON form.\n\n"
        "This is a wire protocol rather than a web page: the HTML is deliberately "
        "minimal and carries one link per project."
    ),
    tags=["Packages"],
    parameters=[_FORMAT_PARAM],
    responses={
        "200": {
            "description": "All projects, as HTML or PEP 691 JSON per content negotiation",
            "content": {
                "text/html": {},
                _JSON_ACCEPT: {"schema": ref("SimpleIndexJson")},
            },
        },
        **errors("401", "500"),
    },
)
@validate(query=FormatQuery)
def simple_index(query: FormatQuery):
    packages = current_app.extensions["pypi_index"].get_snapshot()
    names = sorted(packages)
    if _wants_json(request.headers.get("Accept", ""), query.format):
        return _json_response({"meta": {"api-version": "1.0"}, "projects": [{"name": n} for n in names]})
    return render_template("python/simple_index.html", package_names=names)


@pypi_bp.route("/simple/<package_name>/")
@require_permission(PACKAGE_READ)
@api_operation(
    summary="Files of one project",
    description=(
        "Every distribution file for a project, with hashes. Package names are "
        "normalised per PEP 503, so `Demo.Pkg`, `demo-pkg` and `demo_pkg` all "
        "resolve to the same project."
    ),
    tags=["Packages"],
    parameters=[_FORMAT_PARAM],
    responses={
        "200": {
            "description": "File list, as HTML or PEP 691 JSON per content negotiation",
            "content": {
                "text/html": {},
                _JSON_ACCEPT: {"schema": ref("SimpleProjectJson")},
            },
        },
        **errors("401", "404", "500"),
    },
)
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
    return render_template(
            "python/simple_package.html",
            package_name=normalize_package_name(package_name),
            files=files,
        )


@pypi_bp.route("/packages/<path:filename>")
@require_permission(PACKAGE_READ)
@api_operation(
    summary="Download a distribution file",
    description=(
        "Streams a file straight from disk. This is the URL that appears in the "
        "simple index, so `pip` and `uv` fetch it directly. Downloads made with "
        "an API key are attributed to that key's usage counters."
    ),
    tags=["Packages"],
    responses={
        "200": binary("The requested distribution file"),
        **errors("401", "404", "500"),
    },
)
def serve_package(filename: str):
    _record_download(filename)
    resp = send_from_directory(settings.storage.packages_dir, filename)
    resp.headers.pop("Content-Encoding", None)
    return resp


@pypi_bp.route("/simple/<package_name>/<filename>")
@require_permission(PACKAGE_READ)
@api_operation(
    summary="Download a file via its project path",
    description=(
        "Equivalent to `/packages/<filename>`, kept because some clients resolve "
        "relative to the project index URL."
    ),
    tags=["Packages"],
    responses={
        "200": binary("The requested distribution file"),
        **errors("401", "404", "500"),
    },
)
def serve_package_from_simple(package_name: str, filename: str):
    _record_download(filename)
    resp = send_from_directory(settings.storage.packages_dir, filename)
    resp.headers.pop("Content-Encoding", None)
    return resp


@pypi_bp.route("/", methods=["POST"])
@pypi_bp.route("/legacy/", methods=["POST"])
@require_permission(PACKAGE_WRITE)
@api_operation(
    summary="Upload a distribution (twine)",
    description=(
        "Publishes a distribution file. `twine upload --repository-url <base>/ "
        "--username __token__ --password <API-key> dist/*` speaks exactly this "
        "endpoint.\n\n"
        "The file is validated before it is stored: extension allow-list, MIME "
        "sniffing, executable-signature scan, archive structure (a wheel must "
        "contain `WHEEL` and `METADATA` in its `.dist-info`), and an optional "
        "ClamAV scan. SHA-256 is computed while streaming to a temporary file, so "
        "the whole body is never held in memory."
    ),
    tags=["Upload"],
    request_body={
        "required": True,
        "content": {
            "multipart/form-data": {
                "schema": {
                    "type": "object",
                    "required": ["content"],
                    "properties": {
                        "content": {
                            "type": "string",
                            "format": "binary",
                            "description": "The distribution file (.whl, .tar.gz, .zip or .tar)",
                        },
                        ":action": {"type": "string", "description": "twine sends `file_upload`"},
                        "protocol_version": {"type": "string"},
                        "name": {"type": "string", "description": "Project name"},
                        "version": {"type": "string"},
                        "filetype": {"type": "string", "description": "e.g. bdist_wheel, sdist"},
                        "pyversion": {"type": "string", "description": "e.g. py3, source"},
                    },
                }
            }
        },
    },
    responses={
        "200": {
            "description": "The file was stored",
            "content": {"text/plain": {"schema": {"type": "string"}, "example": "Success"}},
        },
        **errors("400", "401", "409", "413"),
    },
)
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
