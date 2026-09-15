"""Per-ecosystem Markdown documentation — read, download, admin upload.

Each ecosystem group in the sidebar has its own **documentation leaf**
(``/docs/<ecosystem>``) whose documents live in ``DOCS_DIR/<ecosystem>/``.  This
blueprint is the whole surface of that feature:

============================  ==========================================
``GET  /api/v1/docs``         every ecosystem with its document count
``GET  /api/v1/docs/<eco>``   one ecosystem's catalog
``GET  /api/v1/docs/<eco>/<name>``  one document (source + rendered HTML)
``POST /api/v1/docs/<eco>``   upload/replace a ``.md`` file — ``doc:upload``
``DELETE /api/v1/docs/<eco>/<name>``  remove a document — ``doc:upload``
``GET  /docs/<eco>/``         server-rendered index (HTML, or JSON)
``GET  /docs/<eco>/<name>``   raw Markdown download — ``doc:read``
============================  ==========================================

**The split is deliberate.**  Reading and downloading require ``doc:read``,
which the built-in ``authenticated`` role holds, so every signed-in user can
read the handbook.  Changing a document requires ``doc:upload``, which only the
built-in ``admin`` role holds: an administrator publishes by *uploading a
Markdown file*, and nobody edits content in the browser.

⚠ Decorator order is load-bearing (see ``routes/python_build.py``): the
``@docs_bp.route`` decorator must be the topmost line, or the guard is applied
after registration and never runs. ``scripts/check_auth_guards.py`` enforces it.
"""

from __future__ import annotations

from flask import (
    Blueprint, abort, jsonify, render_template_string, request,
    send_from_directory, url_for,
)

from auth.decorators import require_permission
from auth.permissions import DOC_READ, DOC_UPLOAD
from config import settings
from errors import BadRequestError
from openapi import api_operation, errors, ok
from routes.hub_common import spa_url, wants_json
from services import docs, markdown, templates

docs_bp = Blueprint("docs", __name__)

# ── Inline response shapes ───────────────────────────────────────────
# The leaf payloads are schemaless enough that a JSON object with `type` set
# describes them without a pydantic model per catalog (see routes/hub.py).

_DOCUMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "title": {"type": "string"},
        "filename": {"type": "string"},
        "size": {"type": "integer"},
        "size_human": {"type": "string"},
        "modified": {"type": ["string", "null"]},
        "download_url": {"type": "string"},
        "raw_url": {"type": "string"},
    },
}

_DOC_DETAIL_SCHEMA = {
    "type": "object",
    "properties": {
        **_DOCUMENT_SCHEMA["properties"],
        "content": {"type": "string"},
        "html": {"type": "string"},
    },
}

_CATALOG_SCHEMA = {
    "type": "object",
    "properties": {
        "ecosystem": {"type": "string"},
        "root": {"type": "string"},
        "exists": {"type": "boolean"},
        "url_prefix": {"type": "string"},
        "doc_count": {"type": "integer"},
        "documents": {"type": "array", "items": _DOCUMENT_SCHEMA},
    },
}

_OVERVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "root": {"type": "string"},
        "url_prefix": {"type": "string"},
        "ecosystems": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "key": {"type": "string"},
                    "exists": {"type": "boolean"},
                    "doc_count": {"type": "integer"},
                },
            },
        },
    },
}

_ECOSYSTEM_PARAM = {
    "name": "ecosystem",
    "in": "path",
    "required": True,
    "description": "Ecosystem key: " + " / ".join(f"`{key}`" for key in docs.ECOSYSTEMS),
    "schema": {"type": "string", "enum": list(docs.ECOSYSTEMS)},
}

_NAME_PARAM = {
    "name": "name",
    "in": "path",
    "required": True,
    "description": "Markdown filename (`*.md`), a single path segment.",
    "schema": {"type": "string"},
}


# ── Helpers ──────────────────────────────────────────────────────────

def _require_ecosystem(ecosystem: str) -> None:
    """404 an unknown ecosystem before it becomes a directory lookup."""
    if ecosystem not in docs.ECOSYSTEMS:
        abort(404, description=f"Unknown ecosystem '{ecosystem}'")


def _catalog(ecosystem: str) -> dict:
    return docs.scan(
        settings.hub.docs_dir, ecosystem, url_prefix=spa_url("/docs")
    )


def _read(ecosystem: str, name: str) -> dict:
    """Read one document and attach its rendered HTML."""
    entry = docs.read(settings.hub.docs_dir, ecosystem, name)
    entry["html"] = markdown.render(entry["content"])
    return entry


def _uploaded_filename() -> str:
    """The target filename from a multipart upload.

    An explicit ``name`` form field is taken verbatim (and validated by
    :func:`services.docs.normalize_name`, so a traversal is rejected).  The
    uploaded file's own name has its directory components stripped first, since
    some browsers still send a ``C:\\fakepath\\doc.md`` path; the result is then
    validated the same way.
    """
    explicit = (request.form.get("name") or "").strip()
    if explicit:
        return explicit
    upload = request.files.get("file")
    client_name = upload.filename if upload else ""
    return (client_name or "").replace("\\", "/").rsplit("/", 1)[-1]


# ── JSON API ─────────────────────────────────────────────────────────

@docs_bp.route("/api/v1/docs")
@require_permission(DOC_READ)
@api_operation(
    summary="Documentation overview",
    description=(
        "Every ecosystem that owns a documentation leaf, with the number of "
        "Markdown documents currently published for it. An ecosystem whose "
        "directory does not exist yet is reported with `exists: false` rather "
        "than omitted, so the SPA can render an empty state."
    ),
    tags=["Docs"],
    responses={
        "200": ok("Per-ecosystem document counts", _OVERVIEW_SCHEMA),
        **errors("401", "403", "500"),
    },
)
def docs_overview():
    return jsonify(
        docs.scan_all(settings.hub.docs_dir, url_prefix=spa_url("/docs"))
    )


@docs_bp.route("/api/v1/docs/<ecosystem>")
@require_permission(DOC_READ)
@api_operation(
    summary="Ecosystem documentation catalog",
    description=(
        "The Markdown documents published for one ecosystem, newest metadata "
        "included. `download_url` fetches the raw `.md` file; the SPA renders "
        "the document itself from `GET /api/v1/docs/<ecosystem>/<name>`."
    ),
    tags=["Docs"],
    parameters=[_ECOSYSTEM_PARAM],
    responses={
        "200": ok("The ecosystem's documents", _CATALOG_SCHEMA),
        **errors("401", "403", "404", "500"),
    },
)
def docs_catalog(ecosystem: str):
    _require_ecosystem(ecosystem)
    return jsonify(_catalog(ecosystem))


@docs_bp.route("/api/v1/docs/<ecosystem>/<name>")
@require_permission(DOC_READ)
@api_operation(
    summary="One documentation document",
    description=(
        "One Markdown document, returned in two forms: `content` is the raw "
        "source exactly as uploaded, and `html` is the server-rendered, "
        "HTML-escaped body the SPA displays. Raw HTML inside a document is "
        "never emitted as markup."
    ),
    tags=["Docs"],
    parameters=[_ECOSYSTEM_PARAM, _NAME_PARAM],
    responses={
        "200": ok("The document source and rendered HTML", _DOC_DETAIL_SCHEMA),
        **errors("400", "401", "403", "404", "500"),
    },
)
def docs_document(ecosystem: str, name: str):
    _require_ecosystem(ecosystem)
    try:
        entry = _read(ecosystem, name)
    except ValueError as exc:
        raise BadRequestError(str(exc)) from exc
    except FileNotFoundError:
        abort(404, description=f"Document '{name}' not found")
    return jsonify(entry)


@docs_bp.route("/api/v1/docs/<ecosystem>", methods=["POST"])
@require_permission(DOC_UPLOAD)
@api_operation(
    summary="Upload (or replace) a documentation document",
    description=(
        "Publishes one Markdown file. The body is `multipart/form-data` with a "
        "`file` part (and an optional `name` field that overrides the uploaded "
        "filename). Uploading an existing filename **replaces** that document — "
        "this is the only way document content ever changes, and it requires "
        "`doc:upload`, which by default only the built-in admin role holds."
    ),
    tags=["Docs"],
    parameters=[_ECOSYSTEM_PARAM],
    request_body={
        "required": True,
        "content": {
            "multipart/form-data": {
                "schema": {
                    "type": "object",
                    "properties": {
                        "file": {"type": "string", "format": "binary"},
                        "name": {"type": "string"},
                    },
                    "required": ["file"],
                }
            }
        },
    },
    responses={
        "201": ok("The stored document", _DOCUMENT_SCHEMA),
        **errors("400", "401", "403", "404", "413", "500"),
    },
)
def docs_upload(ecosystem: str):
    _require_ecosystem(ecosystem)
    upload = request.files.get("file")
    if upload is None or not upload.filename:
        raise BadRequestError("multipart/form-data 需要一个 file 字段")

    raw = upload.read(docs.MAX_DOC_BYTES + 1)
    if len(raw) > docs.MAX_DOC_BYTES:
        abort(
            413,
            description=f"Document exceeds the {docs.MAX_DOC_BYTES // (1024 * 1024)} MiB limit",
        )
    try:
        # `utf-8-sig` transparently drops a BOM an editor may have written.
        content = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise BadRequestError("文档必须是 UTF-8 编码的 Markdown 文本") from exc

    try:
        entry = docs.save(
            settings.hub.docs_dir, ecosystem, _uploaded_filename(), content
        )
    except ValueError as exc:
        raise BadRequestError(str(exc)) from exc
    return jsonify(entry), 201


@docs_bp.route("/api/v1/docs/<ecosystem>/<name>", methods=["DELETE"])
@require_permission(DOC_UPLOAD)
@api_operation(
    summary="Delete a documentation document",
    description=(
        "Removes one Markdown document. Like the upload endpoint this requires "
        "`doc:upload`; reading access is not enough."
    ),
    tags=["Docs"],
    parameters=[_ECOSYSTEM_PARAM, _NAME_PARAM],
    responses={
        "200": ok("The removed document", _DOCUMENT_SCHEMA),
        **errors("400", "401", "403", "404", "500"),
    },
)
def docs_delete(ecosystem: str, name: str):
    _require_ecosystem(ecosystem)
    try:
        entry = docs.delete(settings.hub.docs_dir, ecosystem, name)
    except ValueError as exc:
        raise BadRequestError(str(exc)) from exc
    except FileNotFoundError:
        abort(404, description=f"Document '{name}' not found")
    return jsonify(entry)


# ── Machine-facing index + raw download ──────────────────────────────

@docs_bp.route("/docs/<ecosystem>/")
@require_permission(DOC_READ)
@api_operation(
    summary="Ecosystem documentation index",
    description=(
        "A server-rendered index of one ecosystem's documents — the docs "
        "counterpart of `/tools/`. Returns HTML to a browser and JSON when "
        "`?format=json` or `Accept: application/json` is used; the JSON is the "
        "same document `GET /api/v1/docs/<ecosystem>` returns."
    ),
    tags=["Docs"],
    parameters=[_ECOSYSTEM_PARAM],
    responses={
        "200": {
            "description": "Document index as HTML or JSON",
            "content": {
                "text/html": {},
                "application/json": {"schema": _CATALOG_SCHEMA},
            },
        },
        **errors("401", "403", "404", "500"),
    },
)
def docs_index(ecosystem: str):
    _require_ecosystem(ecosystem)
    payload = _catalog(ecosystem)
    if wants_json():
        return jsonify(payload)
    return render_template_string(
        templates.docs_index(),
        server_name=settings.server.server_name,
        base_url=url_for("docs.docs_index", ecosystem=ecosystem, _external=True),
        spa_url=spa_url(f"/docs/{ecosystem}"),
        ecosystem=ecosystem,
        documents=payload["documents"],
        doc_count=payload["doc_count"],
    )


@docs_bp.route("/docs/<ecosystem>/<name>")
@require_permission(DOC_READ)
@api_operation(
    summary="Download a documentation document",
    description=(
        "Streams one document's raw Markdown from `DOCS_DIR/<ecosystem>/`. Add "
        "`?download=1` (the `download_url` the catalog hands out) to receive it "
        "as an attachment; without it the file is served inline as "
        "`text/markdown`. Traversal outside the ecosystem directory is refused."
    ),
    tags=["Docs"],
    parameters=[_ECOSYSTEM_PARAM, _NAME_PARAM],
    responses={
        "200": {
            "description": "Raw Markdown document",
            "content": {"text/markdown": {}},
        },
        **errors("400", "401", "403", "404", "500"),
    },
)
def docs_raw(ecosystem: str, name: str):
    _require_ecosystem(ecosystem)
    try:
        docs.resolve_file(settings.hub.docs_dir, ecosystem, name)
    except ValueError as exc:
        raise BadRequestError(str(exc)) from exc
    except FileNotFoundError:
        abort(404, description=f"Document '{name}' not found")
    return send_from_directory(
        docs.resolve_dir(settings.hub.docs_dir, ecosystem),
        name,
        mimetype="text/markdown",
        as_attachment=bool(request.args.get("download")),
        download_name=name,
    )


__all__ = ["docs_bp"]
