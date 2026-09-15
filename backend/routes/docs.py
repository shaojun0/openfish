"""Per-ecosystem Markdown documentation — folder projects, assets, editing.

Each ecosystem group in the sidebar has its own **documentation leaf**
(``/documentation/<ecosystem>`` — the SPA page) whose documents live as folder
projects under ``DOCS_DIR/<ecosystem>/<id>/`` — each with its own
``document.md``, a ``meta.json`` title record and an ``assets/`` directory for
the images the document uses.  This blueprint is the whole surface of that
feature:

=========================================  ==========================
``GET    /api/v1/docs``                    every ecosystem + doc count
``GET    /api/v1/docs/<eco>``              one ecosystem's catalog
``POST   /api/v1/docs/<eco>``              create/replace a document — ``doc:upload``
``GET    /api/v1/docs/<eco>/<id>``         one document (source + HTML + assets)
``PUT    /api/v1/docs/<eco>/<id>``         save in-browser edits — ``doc:upload``
``DELETE /api/v1/docs/<eco>/<id>``         delete a document — ``doc:upload``
``POST   /api/v1/docs/<eco>/<id>/preview`` render unsaved source — ``doc:upload``
``GET    /api/v1/docs/<eco>/<id>/assets``  list a document's assets
``POST   /api/v1/docs/<eco>/<id>/assets``  upload an asset — ``doc:upload``
``DELETE /api/v1/docs/<eco>/<id>/assets/<name>``  delete an asset — ``doc:upload``
``GET    /docs/<eco>``                     permanent redirect to ``/docs/<eco>/``
``GET    /docs/<eco>/``                    server-rendered index (HTML or JSON)
``GET    /docs/<eco>/<id>``                raw Markdown download
``GET    /docs/<eco>/<id>/assets/<name>``  one asset (image inline, rest as attachment)
=========================================  ==========================

**The URL split is deliberate, and so is the boundary.**  Every ``/docs/*`` URL
requires ``doc:read`` — there is no path under it that a trailing slash can
flip to a different permission, because the human page lives one namespace over
at ``/documentation/<eco>`` (served by the SPA shell).  ``/docs/<eco>`` exists
only to redirect to the canonical slashed form, so old bookmarks keep working.

**Reading and downloading** require ``doc:read``, which the built-in
``authenticated`` role holds, so every signed-in user can read the handbook.
Changing anything requires ``doc:upload``, which only the built-in ``admin``
role holds.

⚠ Decorator order is load-bearing (see ``routes/python_build.py``): the
``@docs_bp.route`` decorator must be the topmost line, or the guard is applied
after registration and never runs. ``scripts/check_auth_guards.py`` enforces it.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

from flask import (
    Blueprint, abort, jsonify, redirect, render_template, request,
    send_from_directory, url_for,
)

from auth.decorators import require_permission
from auth.permissions import DOC_READ, DOC_UPLOAD
from config import settings
from errors import BadRequestError
from openapi import api_operation, errors, ok
from routes.hub_common import spa_url, wants_json
from services import docs, markdown

docs_bp = Blueprint("docs", __name__)

# ── Inline response shapes ───────────────────────────────────────────
# The leaf payloads are schemaless enough that a JSON object with `type` set
# describes them without a pydantic model per catalog (see routes/hub.py).

_ASSET_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "size": {"type": "integer"},
        "size_human": {"type": "string"},
        "modified": {"type": ["string", "null"]},
        "is_image": {"type": "boolean"},
        "url": {"type": "string"},
    },
}

_DOCUMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "id": {"type": "string"},
        "title": {"type": "string"},
        "filename": {"type": "string"},
        "size": {"type": "integer"},
        "size_human": {"type": "string"},
        "modified": {"type": ["string", "null"]},
        "created": {"type": ["string", "null"]},
        "asset_count": {"type": "integer"},
        "download_url": {"type": "string"},
        "raw_url": {"type": "string"},
        "assets_url": {"type": "string"},
    },
}

_DOC_DETAIL_SCHEMA = {
    "type": "object",
    "properties": {
        **_DOCUMENT_SCHEMA["properties"],
        "content": {"type": "string"},
        "html": {"type": "string"},
        "assets": {"type": "array", "items": _ASSET_SCHEMA},
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

_DOC_PARAM = {
    "name": "doc_id",
    "in": "path",
    "required": True,
    "description": "Document folder id (one path segment).",
    "schema": {"type": "string"},
}

_ASSET_NAME_PARAM = {
    "name": "name",
    "in": "path",
    "required": True,
    "description": "Asset filename inside the document's `assets/` directory.",
    "schema": {"type": "string"},
}


# ── Helpers ──────────────────────────────────────────────────────────

def _require_ecosystem(ecosystem: str) -> None:
    """404 an unknown ecosystem before it becomes a directory lookup."""
    if ecosystem not in docs.ECOSYSTEMS:
        abort(404, description=f"Unknown ecosystem '{ecosystem}'")


def _asset_base(ecosystem: str, doc_id: str) -> str:
    """Absolute URL prefix a document's ``assets/…`` references resolve to."""
    return f"{spa_url('/docs')}/{quote(ecosystem)}/{quote(doc_id)}/assets"


def _catalog(ecosystem: str) -> dict:
    return docs.scan(
        settings.hub.docs_dir,
        ecosystem,
        url_prefix=spa_url("/docs"),
        api_prefix=spa_url("/api/v1"),
    )


def _read(ecosystem: str, doc_id: str) -> dict:
    """Read one document and attach its rendered HTML."""
    entry = docs.read(
        settings.hub.docs_dir, ecosystem, doc_id, api_prefix=spa_url("/api/v1")
    )
    entry["html"] = markdown.render(
        entry["content"], asset_base=_asset_base(ecosystem, doc_id)
    )
    return entry


def _decode_markdown(upload) -> str:
    """Read a ``.md`` file part as UTF-8 text, enforcing the size ceiling."""
    raw = upload.read(docs.MAX_DOC_BYTES + 1)
    if len(raw) > docs.MAX_DOC_BYTES:
        abort(
            413,
            description=f"Document exceeds the {docs.MAX_DOC_BYTES // (1024 * 1024)} MiB limit",
        )
    try:
        # `utf-8-sig` transparently drops a BOM an editor may have written.
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise BadRequestError("文档必须是 UTF-8 编码的 Markdown 文本") from exc


def _json_content() -> str:
    """The ``content`` string of a JSON request body."""
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict) or not isinstance(payload.get("content"), str):
        raise BadRequestError("请求体需要一个字符串字段 content")
    return payload["content"]


# ── JSON API: catalog ────────────────────────────────────────────────

@docs_bp.route("/api/v1/docs")
@require_permission(DOC_READ)
@api_operation(
    summary="Documentation overview",
    description=(
        "Every ecosystem that owns a documentation leaf, with the number of "
        "document projects currently published for it. An ecosystem whose "
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
        docs.scan_all(
            settings.hub.docs_dir,
            url_prefix=spa_url("/docs"),
            api_prefix=spa_url("/api/v1"),
        )
    )


@docs_bp.route("/api/v1/docs/<ecosystem>")
@require_permission(DOC_READ)
@api_operation(
    summary="Ecosystem documentation catalog",
    description=(
        "The document projects published for one ecosystem. Each entry is a "
        "folder with its own `document.md` and assets; `download_url` fetches "
        "the raw Markdown, and the SPA renders the document itself from "
        "`GET /api/v1/docs/<ecosystem>/<doc_id>`."
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


@docs_bp.route("/api/v1/docs/<ecosystem>", methods=["POST"])
@require_permission(DOC_UPLOAD)
@api_operation(
    summary="Create (or replace) a documentation document",
    description=(
        "Creates a document project from a title, optionally seeded with an "
        "uploaded `.md` file. A create whose derived folder id already exists "
        "**replaces** that document's content instead of duplicating it; the "
        "response's `replaced` flag says which happened. The body is "
        "`multipart/form-data` with a `title` field and an optional `file` "
        "part; without a file the document starts empty. Requires `doc:upload`, "
        "which by default only the built-in admin role holds."
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
                        "title": {"type": "string"},
                        "file": {"type": "string", "format": "binary"},
                    },
                    "required": ["title"],
                }
            }
        },
    },
    responses={
        "201": ok(
            "The stored document and whether it replaced an existing one",
            {
                "type": "object",
                "properties": {
                    "document": _DOCUMENT_SCHEMA,
                    "replaced": {"type": "boolean"},
                },
            },
        ),
        **errors("400", "401", "403", "404", "413", "500"),
    },
)
def docs_create(ecosystem: str):
    _require_ecosystem(ecosystem)
    title = (request.form.get("title") or "").strip()
    content = ""
    upload = request.files.get("file")
    if upload is not None and upload.filename:
        content = _decode_markdown(upload)
        if not title:
            # Some browsers still send a `C:\fakepath\doc.md` path.
            fallback = Path(upload.filename.replace("\\", "/")).stem
            title = docs.heading_in_text(content) or fallback
    if not title:
        raise BadRequestError("请填写文档标题，或上传一个 .md 文件")

    try:
        entry, replaced = docs.save_document(
            settings.hub.docs_dir, ecosystem, title=title, content=content
        )
    except ValueError as exc:
        raise BadRequestError(str(exc)) from exc
    return jsonify({"document": entry, "replaced": replaced}), 201


@docs_bp.route("/api/v1/docs/<ecosystem>/<doc_id>")
@require_permission(DOC_READ)
@api_operation(
    summary="One documentation document",
    description=(
        "One document project: `content` is the raw Markdown source, `html` is "
        "the server-rendered, HTML-escaped body (with relative `assets/…` "
        "image URLs made absolute), and `assets` lists the files the document "
        "can reference. Raw HTML inside a document is never emitted as markup."
    ),
    tags=["Docs"],
    parameters=[_ECOSYSTEM_PARAM, _DOC_PARAM],
    responses={
        "200": ok("The document source, rendered HTML and assets", _DOC_DETAIL_SCHEMA),
        **errors("400", "401", "403", "404", "500"),
    },
)
def docs_document(ecosystem: str, doc_id: str):
    _require_ecosystem(ecosystem)
    try:
        entry = _read(ecosystem, doc_id)
    except ValueError as exc:
        raise BadRequestError(str(exc)) from exc
    except FileNotFoundError:
        abort(404, description=f"Document '{doc_id}' not found")
    return jsonify(entry)


@docs_bp.route("/api/v1/docs/<ecosystem>/<doc_id>", methods=["PUT"])
@require_permission(DOC_UPLOAD)
@api_operation(
    summary="Save a documentation document's source",
    description=(
        "Replaces one document's Markdown with the body of a JSON request "
        "(`{\"content\": \"…\"}`). This is the in-browser editor's write path; "
        "if the new source opens with a `#` heading it also becomes the "
        "document's title. Requires `doc:upload`."
    ),
    tags=["Docs"],
    parameters=[_ECOSYSTEM_PARAM, _DOC_PARAM],
    request_body={
        "required": True,
        "content": {
            "application/json": {
                "schema": {
                    "type": "object",
                    "properties": {"content": {"type": "string"}},
                    "required": ["content"],
                }
            }
        },
    },
    responses={
        "200": ok("The stored document", _DOC_DETAIL_SCHEMA),
        **errors("400", "401", "403", "404", "413", "500"),
    },
)
def docs_update(ecosystem: str, doc_id: str):
    _require_ecosystem(ecosystem)
    content = _json_content()
    try:
        docs.save_content(settings.hub.docs_dir, ecosystem, doc_id, content)
    except ValueError as exc:
        raise BadRequestError(str(exc)) from exc
    except FileNotFoundError:
        abort(404, description=f"Document '{doc_id}' not found")
    return jsonify(_read(ecosystem, doc_id))


@docs_bp.route("/api/v1/docs/<ecosystem>/<doc_id>", methods=["DELETE"])
@require_permission(DOC_UPLOAD)
@api_operation(
    summary="Delete a documentation document",
    description=(
        "Removes one document project — its Markdown, metadata and every "
        "uploaded asset. Like the write endpoints this requires `doc:upload`; "
        "reading access is not enough."
    ),
    tags=["Docs"],
    parameters=[_ECOSYSTEM_PARAM, _DOC_PARAM],
    responses={
        "200": ok("The removed document", _DOCUMENT_SCHEMA),
        **errors("400", "401", "403", "404", "500"),
    },
)
def docs_delete(ecosystem: str, doc_id: str):
    _require_ecosystem(ecosystem)
    try:
        entry = docs.delete(settings.hub.docs_dir, ecosystem, doc_id)
    except ValueError as exc:
        raise BadRequestError(str(exc)) from exc
    except FileNotFoundError:
        abort(404, description=f"Document '{doc_id}' not found")
    return jsonify(entry)


@docs_bp.route("/api/v1/docs/<ecosystem>/<doc_id>/preview", methods=["POST"])
@require_permission(DOC_UPLOAD)
@api_operation(
    summary="Render unsaved documentation source",
    description=(
        "Renders a JSON body's `content` to escaped HTML without touching "
        "disk — the editor's live preview. Relative `assets/…` references are "
        "resolved against the document being edited, so an image inserted from "
        "the editor's asset panel previews exactly as it will be served."
    ),
    tags=["Docs"],
    parameters=[_ECOSYSTEM_PARAM, _DOC_PARAM],
    request_body={
        "required": True,
        "content": {
            "application/json": {
                "schema": {
                    "type": "object",
                    "properties": {"content": {"type": "string"}},
                    "required": ["content"],
                }
            }
        },
    },
    responses={
        "200": ok(
            "The rendered HTML",
            {
                "type": "object",
                "properties": {"html": {"type": "string"}},
            },
        ),
        **errors("400", "401", "403", "404", "500"),
    },
)
def docs_preview(ecosystem: str, doc_id: str):
    _require_ecosystem(ecosystem)
    try:
        docs.normalize_doc_id(doc_id)
    except ValueError as exc:
        raise BadRequestError(str(exc)) from exc
    content = _json_content()
    return jsonify({
        "html": markdown.render(content, asset_base=_asset_base(ecosystem, doc_id))
    })


# ── JSON API: assets ─────────────────────────────────────────────────

@docs_bp.route("/api/v1/docs/<ecosystem>/<doc_id>/assets")
@require_permission(DOC_READ)
@api_operation(
    summary="List a document's assets",
    description=(
        "Every file in the document project's own `assets/` directory — the "
        "images and attachments its Markdown can reference as `assets/<name>`."
    ),
    tags=["Docs"],
    parameters=[_ECOSYSTEM_PARAM, _DOC_PARAM],
    responses={
        "200": ok(
            "The document's assets",
            {
                "type": "object",
                "properties": {"assets": {"type": "array", "items": _ASSET_SCHEMA}},
            },
        ),
        **errors("400", "401", "403", "404", "500"),
    },
)
def docs_assets(ecosystem: str, doc_id: str):
    _require_ecosystem(ecosystem)
    try:
        docs.resolve_doc_file(settings.hub.docs_dir, ecosystem, doc_id)
    except ValueError as exc:
        raise BadRequestError(str(exc)) from exc
    except FileNotFoundError:
        abort(404, description=f"Document '{doc_id}' not found")
    return jsonify({
        "assets": docs.list_assets(
            settings.hub.docs_dir, ecosystem, doc_id, url_prefix=spa_url("/docs")
        )
    })


@docs_bp.route("/api/v1/docs/<ecosystem>/<doc_id>/assets", methods=["POST"])
@require_permission(DOC_UPLOAD)
@api_operation(
    summary="Upload an asset into a document",
    description=(
        "Stores one `multipart/form-data` `file` part inside the document's "
        "own `assets/` directory. This is how an image belongs to a document "
        "instead of every ecosystem sharing one flat folder. Requires "
        "`doc:upload`."
    ),
    tags=["Docs"],
    parameters=[_ECOSYSTEM_PARAM, _DOC_PARAM],
    request_body={
        "required": True,
        "content": {
            "multipart/form-data": {
                "schema": {
                    "type": "object",
                    "properties": {"file": {"type": "string", "format": "binary"}},
                    "required": ["file"],
                }
            }
        },
    },
    responses={
        "201": ok("The stored asset", _ASSET_SCHEMA),
        **errors("400", "401", "403", "404", "413", "500"),
    },
)
def docs_asset_upload(ecosystem: str, doc_id: str):
    _require_ecosystem(ecosystem)
    upload = request.files.get("file")
    if upload is None or not upload.filename:
        raise BadRequestError("multipart/form-data 需要一个 file 字段")

    data = upload.read(docs.MAX_ASSET_BYTES + 1)
    if len(data) > docs.MAX_ASSET_BYTES:
        abort(
            413,
            description=(
                f"Asset exceeds the {docs.MAX_ASSET_BYTES // (1024 * 1024)} MiB limit"
            ),
        )
    try:
        entry = docs.save_asset(
            settings.hub.docs_dir, ecosystem, doc_id, upload.filename, data
        )
    except ValueError as exc:
        raise BadRequestError(str(exc)) from exc
    except FileNotFoundError:
        abort(404, description=f"Document '{doc_id}' not found")
    return jsonify(entry), 201


@docs_bp.route("/api/v1/docs/<ecosystem>/<doc_id>/assets/<name>", methods=["DELETE"])
@require_permission(DOC_UPLOAD)
@api_operation(
    summary="Delete a document asset",
    description="Removes one file from the document's `assets/` directory.",
    tags=["Docs"],
    parameters=[_ECOSYSTEM_PARAM, _DOC_PARAM, _ASSET_NAME_PARAM],
    responses={
        "200": ok("The removed asset", _ASSET_SCHEMA),
        **errors("400", "401", "403", "404", "500"),
    },
)
def docs_asset_delete(ecosystem: str, doc_id: str, name: str):
    _require_ecosystem(ecosystem)
    try:
        entry = docs.delete_asset(settings.hub.docs_dir, ecosystem, doc_id, name)
    except ValueError as exc:
        raise BadRequestError(str(exc)) from exc
    except FileNotFoundError:
        abort(404, description=f"Asset '{name}' not found")
    return jsonify(entry)


# ── Machine-facing index, raw download and assets ────────────────────

@docs_bp.route("/docs/<ecosystem>")
@require_permission(DOC_READ)
@api_operation(
    summary="Canonical documentation index URL",
    description=(
        "Permanent redirect (308) to `/docs/<ecosystem>/`, so the docs "
        "namespace has exactly one URL per resource. Before this route existed "
        "the SPA shell claimed the unslashed form, which meant `/docs/<eco>` "
        "and `/docs/<eco>/` answered with different content **and different "
        "permissions** — a trailing slash silently changed who could read it."
    ),
    tags=["Docs"],
    parameters=[_ECOSYSTEM_PARAM],
    responses={
        "308": {"description": "Redirect to `/docs/<ecosystem>/`"},
        **errors("401", "403", "404"),
    },
)
def docs_index_redirect(ecosystem: str):
    _require_ecosystem(ecosystem)
    return redirect(url_for("docs.docs_index", ecosystem=ecosystem), code=308)


@docs_bp.route("/docs/<ecosystem>/")
@require_permission(DOC_READ)
@api_operation(
    summary="Ecosystem documentation index",
    description=(
        "A server-rendered index of one ecosystem's document projects — the "
        "docs counterpart of `/tools/`. Returns HTML to a browser and JSON "
        "when `?format=json` or `Accept: application/json` is used; the JSON "
        "is the same document `GET /api/v1/docs/<ecosystem>` returns."
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
    return render_template(
        "docs/index.html",
        server_name=settings.server.server_name,
        base_url=url_for("docs.docs_index", ecosystem=ecosystem, _external=True),
        spa_url=spa_url(f"/documentation/{ecosystem}"),
        ecosystem=ecosystem,
        documents=payload["documents"],
        doc_count=payload["doc_count"],
    )


@docs_bp.route("/docs/<ecosystem>/<doc_id>")
@require_permission(DOC_READ)
@api_operation(
    summary="Download a documentation document",
    description=(
        "Streams one document's raw Markdown from "
        "`DOCS_DIR/<ecosystem>/<doc_id>/document.md`. Add `?download=1` (the "
        "`download_url` the catalog hands out) to receive it as an attachment; "
        "without it the file is served inline as `text/markdown`. Traversal "
        "outside the ecosystem directory is refused."
    ),
    tags=["Docs"],
    parameters=[_ECOSYSTEM_PARAM, _DOC_PARAM],
    responses={
        "200": {
            "description": "Raw Markdown document",
            "content": {"text/markdown": {}},
        },
        **errors("400", "401", "403", "404", "500"),
    },
)
def docs_raw(ecosystem: str, doc_id: str):
    _require_ecosystem(ecosystem)
    try:
        docs.resolve_doc_file(settings.hub.docs_dir, ecosystem, doc_id)
    except ValueError as exc:
        raise BadRequestError(str(exc)) from exc
    except FileNotFoundError:
        abort(404, description=f"Document '{doc_id}' not found")
    return send_from_directory(
        docs.resolve_doc_dir(settings.hub.docs_dir, ecosystem, doc_id),
        docs.DOC_FILENAME,
        mimetype="text/markdown",
        as_attachment=bool(request.args.get("download")),
        download_name=f"{doc_id}.md",
    )


@docs_bp.route("/docs/<ecosystem>/<doc_id>/assets/<name>")
@require_permission(DOC_READ)
@api_operation(
    summary="Serve a documentation asset",
    description=(
        "Streams one file from a document's `assets/` directory. Images are "
        "served inline so Markdown can reference them; every other type is "
        "served as an attachment. Traversal outside the assets directory is "
        "refused."
    ),
    tags=["Docs"],
    parameters=[_ECOSYSTEM_PARAM, _DOC_PARAM, _ASSET_NAME_PARAM],
    responses={
        "200": {
            "description": "The asset bytes",
            "content": {"application/octet-stream": {}},
        },
        **errors("400", "401", "403", "404", "500"),
    },
)
def docs_asset_raw(ecosystem: str, doc_id: str, name: str):
    _require_ecosystem(ecosystem)
    try:
        docs.resolve_asset(settings.hub.docs_dir, ecosystem, doc_id, name)
    except ValueError as exc:
        raise BadRequestError(str(exc)) from exc
    except FileNotFoundError:
        abort(404, description=f"Asset '{name}' not found")
    assets_dir = docs.resolve_doc_dir(settings.hub.docs_dir, ecosystem, doc_id) / docs.ASSETS_DIRNAME
    inline = docs.is_image_name(name)
    return send_from_directory(
        assets_dir,
        name,
        as_attachment=not inline,
        download_name=None if inline else name,
    )


__all__ = ["docs_bp"]
