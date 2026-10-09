"""Per-ecosystem Markdown documentation — documents, revisions and assets.

A document is a **row plus an object**, not a folder.  ``documents`` holds the
identity (``ecosystem`` + ``slug`` is the URL address, ``id`` is the stable
identity the history hangs off), ``document_revisions`` holds every version ever
saved, ``document_assets`` holds the images and attachments the Markdown
references, and the bytes live behind :mod:`services.objectstore` under
``objects/<ecosystem>/<id>/…``.  :mod:`models.docs` owns the schema and explains
why it looks like that.

What this replaced, and why
---------------------------
The catalogue used to *be* the directory tree::

    docs/python/getting-started/{document.md, meta.json, assets/…}

and the folder name was the document's only identity.  Three consequences made
that untenable: an edit left no trace and could not be undone; two editors
saving from the same page silently overwrote each other (the second save won);
and the tree sat in the operator's work tree, where a browser save dirtied
``git status`` and a checkout could take the only copy.  Now:

* **Edits are versioned.**  Every save writes a *new* immutable object and adds
  a ``document_revisions`` row; the previous body is never overwritten.
* **Saves are checked.**  :func:`save_content` accepts the ``revision`` the
  editor loaded and refuses the write with a ``409`` when someone else got there
  first, instead of dropping the other edit on the floor.
* **Metadata and body commit together.**  ``title``, ``size``, ``sha256`` and
  the ``storage_key`` are one row, so they cannot disagree with the bytes.

URLs did not change.  ``/docs/<ecosystem>/<slug>``, ``/documentation/<ecosystem>``
and the shapes of every JSON payload are exactly what they were — the identity
became internal instead of being the folder name, which is what makes renaming a
document stop being a data migration.

The operator's workflow survived too.  ``cli.py docs import`` reads the old
folder layout (``<ecosystem>/<slug>/document.md`` + ``meta.json`` + ``assets/``)
into the database, and ``cli.py docs export`` writes it back out — so a
deployment that preferred "copy a folder in" can keep doing that, and a backup
is still ordinary files.

Write access is deliberately narrow.  Reading and downloading require
``doc:read`` (held by the built-in ``authenticated`` role) and — for the
machine-facing documentation index — by the built-in ``anonymous`` role;
creating, editing, deleting a document or uploading one of its assets requires
``doc:upload``, held only by the built-in ``admin`` role.  There is no way to
write outside the catalog: :func:`normalize_doc_id`, :func:`normalize_asset_name`
and the store's own containment check each pin a name to one path segment.
"""

from __future__ import annotations

import mimetypes
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, Any
from urllib.parse import quote

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from errors import RevisionConflictError
from models.docs import (
    ASSET_NAME_MAX,
    DOC_CONTENT_TYPE,
    SLUG_MAX,
    TITLE_MAX,
    Document,
    DocumentAsset,
    DocumentRevision,
    new_key,
)
from services import objectstore
from services.digest import sha256_of, sha256_text
from services.objectstore import ObjectInfo
from services.fileio import atomic_write_bytes, read_json, write_json
from services.format import human_size


#: The ecosystems that own a documentation leaf, in sidebar order.
ECOSYSTEMS: tuple[str, ...] = ("python", "npm", "docker", "debian", "tools", "models")

#: The import/export layout — what ``cli.py docs import`` reads and ``docs
#: export`` writes.  The live payload does not use these names any more; they
#: survive because a folder tree is still the friendliest way to hand a set of
#: documents to (or take one from) an offline deployment.
DOC_FILENAME = "document.md"
META_FILENAME = "meta.json"
ASSETS_DIRNAME = "assets"

#: Markdown documents are small; a couple of megabytes is already an enormous
#: handbook chapter.  Assets (screenshots, diagrams) get a larger, separate
#: budget.
MAX_DOC_BYTES = 2 * 1024 * 1024
MAX_ASSET_BYTES = 16 * 1024 * 1024

#: Length limits.  The numbers live on the model, which is where the columns
#: that enforce them are; these names are what the routes and the validation
#: below have always called them.
MAX_DOC_ID_LENGTH = SLUG_MAX
MAX_TITLE_LENGTH = TITLE_MAX
MAX_ASSET_NAME_LENGTH = ASSET_NAME_MAX

_FORBIDDEN_CHARS = set('/\\:*?"<>|')
_DASH_RUN_RE = re.compile(r"-{2,}")
_IMAGE_EXTS = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg",
    ".bmp", ".avif", ".ico", ".tif", ".tiff",
})


# ── Name validation ──────────────────────────────────────────────────

def normalize_title(title: str) -> str:
    """Validate a human-facing document title."""
    candidate = (title or "").strip()
    if not candidate:
        raise ValueError("文档标题为空")
    if len(candidate) > MAX_TITLE_LENGTH:
        raise ValueError(f"文档标题过长（最多 {MAX_TITLE_LENGTH} 个字符）")
    if any(ord(ch) < 32 for ch in candidate):
        raise ValueError("文档标题不能包含控制字符")
    return candidate


def slugify(title: str) -> str:
    """Derive a stable, URL-safe slug from a title.

    ASCII is lower-cased and whitespace becomes ``-``; a CJK title is kept as
    is (a Chinese path segment is perfectly valid and far friendlier than a
    hash).  Anything that is neither alphanumeric nor ``-_.`` collapses to
    ``-``.
    """
    text = (title or "").strip().lower()
    text = re.sub(r"\s+", "-", text)
    text = "".join(ch if (ch.isalnum() or ch in "-_.") else "-" for ch in text)
    text = _DASH_RUN_RE.sub("-", text).strip("-._")
    if not text:
        text = "doc"
    return text[:MAX_DOC_ID_LENGTH].strip("-._") or "doc"


def normalize_doc_id(value: str) -> str:
    """Validate a document slug (one URL path segment)."""
    candidate = (value or "").strip()
    if not candidate:
        raise ValueError("文档标识为空")
    if len(candidate) > MAX_DOC_ID_LENGTH:
        raise ValueError(f"文档标识过长（最多 {MAX_DOC_ID_LENGTH} 个字符）")
    if candidate.startswith("."):
        raise ValueError("文档标识不能以 '.' 开头")
    if any(ch in _FORBIDDEN_CHARS or ord(ch) < 32 for ch in candidate):
        raise ValueError("文档标识不能包含路径分隔符或控制字符")
    return candidate


def normalize_asset_name(name: str) -> str:
    """Validate an asset filename.

    Directory components are stripped (some browsers send ``C:\\fakepath\\x``),
    then the same single-segment rules as :func:`normalize_doc_id` apply.  An
    extension is required so the served media type is predictable.
    """
    candidate = (name or "").strip().replace("\\", "/").rsplit("/", 1)[-1]
    if not candidate:
        raise ValueError("文件名为空")
    if len(candidate) > MAX_ASSET_NAME_LENGTH:
        raise ValueError(f"文件名过长（最多 {MAX_ASSET_NAME_LENGTH} 个字符）")
    if candidate.startswith("."):
        raise ValueError("文件名不能以 '.' 开头")
    if any(ch in _FORBIDDEN_CHARS or ord(ch) < 32 for ch in candidate):
        raise ValueError("文件名不能包含路径分隔符或控制字符")
    if "." not in candidate:
        raise ValueError("文件名必须带扩展名")
    return candidate


def require_ecosystem(ecosystem: str) -> str:
    """Prove *ecosystem* is one of the known leaves.

    Raises ``KeyError`` so a typo in a URL becomes a ``404`` rather than a
    document filed under an arbitrary name.
    """
    if ecosystem not in ECOSYSTEMS:
        raise KeyError(ecosystem)
    return ecosystem


# ── Small helpers ────────────────────────────────────────────────────

def heading_in_text(text: str) -> str:
    """The first ``#`` heading in a Markdown string, or ``""``."""
    for line in (text or "").splitlines()[:200]:  # only the head matters
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip().rstrip("#").strip()
    return ""


def is_image_name(name: str) -> bool:
    return Path(name).suffix.lower() in _IMAGE_EXTS


def _guess_content_type(name: str) -> str:
    """A media type for *name*, or ``""`` when the guess is unknown."""
    return mimetypes.guess_type(name)[0] or ""


def _iso(value: datetime) -> str:
    """An ISO-8601 **UTC** string for a stored timestamp.

    SQLite stores no offset and hands the value back naive, so a naive value is
    read as UTC — which is what every writer here meant — instead of being run
    through ``.timestamp()``, which would interpret it as the *host's* local time
    and shift every timestamp by the server's UTC offset.
    """
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _check_doc_bytes(encoded: bytes) -> None:
    if len(encoded) > MAX_DOC_BYTES:
        raise ValueError(f"文档超过 {MAX_DOC_BYTES // (1024 * 1024)} MiB 上限")


# ── Payload shapes ───────────────────────────────────────────────────

def _entry(
    row: Document,
    *,
    asset_count: int,
    url_prefix: str,
    api_prefix: str,
) -> dict[str, Any]:
    """One catalog entry — the shape the SPA, the HTML index and the tests know.

    ``id`` is the **slug**, because it is what every URL and every API call
    carries; the row's primary key is an implementation detail.  ``revision`` is
    the addition: the editor sends it back on the next save so a stale write is
    refused instead of winning.
    """
    base = url_prefix.rstrip("/")
    api = api_prefix.rstrip("/")
    raw = f"{base}/{quote(row.ecosystem)}/{quote(row.slug)}"
    return {
        "id": row.slug,
        "title": row.title,
        "filename": DOC_FILENAME,
        "revision": row.revision,
        "size": row.size,
        "size_human": human_size(row.size),
        "modified": _iso(row.modified_at),
        "created": _iso(row.created_at),
        "asset_count": asset_count,
        # Read/download the raw Markdown exactly as authored.
        "download_url": f"{raw}?download=1",
        "raw_url": raw,
        "assets_url": f"{api}/docs/{quote(row.ecosystem)}/{quote(row.slug)}/assets",
    }


def _asset_entry(row: DocumentAsset, *, ecosystem: str, slug: str, url_prefix: str) -> dict[str, Any]:
    base = url_prefix.rstrip("/")
    return {
        "name": row.name,
        "size": row.size,
        "size_human": human_size(row.size),
        "modified": _iso(row.created_at),
        "is_image": is_image_name(row.name),
        "url": (
            f"{base}/{quote(ecosystem)}/{quote(slug)}/{ASSETS_DIRNAME}/{quote(row.name)}"
        ),
    }


# ── Queries ──────────────────────────────────────────────────────────

def _find(session: Session, ecosystem: str, slug: str) -> Document | None:
    return session.scalars(
        select(Document).where(
            Document.ecosystem == ecosystem, Document.slug == slug
        )
    ).first()


def require_document(session: Session, ecosystem: str, doc_id: str) -> Document:
    """One document row, or ``FileNotFoundError`` — the routes' 404."""
    require_ecosystem(ecosystem)
    slug = normalize_doc_id(doc_id)
    row = _find(session, ecosystem, slug)
    if row is None:
        raise FileNotFoundError(slug)
    return row


def _assets(session: Session, document_id: int) -> list[DocumentAsset]:
    return list(
        session.scalars(
            select(DocumentAsset)
            .where(DocumentAsset.document_id == document_id)
            .order_by(func.lower(DocumentAsset.name))
        )
    )


def _asset_counts(session: Session, ecosystem: str) -> dict[int, int]:
    """``document_id -> asset count`` for one ecosystem, in a single query.

    The catalog lists every document at once; asking per document is the N+1
    this exists to avoid.
    """
    rows = session.execute(
        select(DocumentAsset.document_id, func.count())
        .join(Document, Document.id == DocumentAsset.document_id)
        .where(Document.ecosystem == ecosystem)
        .group_by(DocumentAsset.document_id)
    ).all()
    return {document_id: count for document_id, count in rows}


def _find_asset(session: Session, row: Document, name: str) -> DocumentAsset:
    asset = session.scalars(
        select(DocumentAsset).where(
            DocumentAsset.document_id == row.id,
            DocumentAsset.name == name,
        )
    ).first()
    if asset is None:
        raise FileNotFoundError(name)
    return asset


# ── Read path ────────────────────────────────────────────────────────

def scan(
    session: Session,
    ecosystem: str,
    *,
    url_prefix: str = "/docs",
    api_prefix: str = "/api/v1",
) -> dict[str, Any]:
    """Catalog one ecosystem's documents (alphabetical by slug)."""
    require_ecosystem(ecosystem)
    rows = list(
        session.scalars(
            select(Document)
            .where(Document.ecosystem == ecosystem)
            .order_by(func.lower(Document.slug))
        )
    )
    counts = _asset_counts(session, ecosystem)
    documents = [
        _entry(row, asset_count=counts.get(row.id, 0), url_prefix=url_prefix, api_prefix=api_prefix)
        for row in rows
    ]
    # `exists` used to mean "the ecosystem directory is there", which the SPA
    # reads to decide between an empty state and a missing leaf.  With documents
    # in the database it means "this leaf has anything at all": a document (from
    # the panel or an import) or a leftover import tree the operator has not
    # cleaned up yet.
    store = objectstore.docs_store()
    return {
        "ecosystem": ecosystem,
        "root": store.location,
        "exists": bool(documents) or store.exists(ecosystem),
        "url_prefix": url_prefix,
        "doc_count": len(documents),
        "documents": documents,
    }


def scan_all(
    session: Session,
    *,
    url_prefix: str = "/docs",
    api_prefix: str = "/api/v1",
) -> dict[str, Any]:
    """Every ecosystem's document count — the landing data for the SPA."""
    ecosystems: list[dict[str, Any]] = []
    for key in ECOSYSTEMS:
        payload = scan(session, key, url_prefix=url_prefix, api_prefix=api_prefix)
        ecosystems.append({
            "key": key,
            "exists": payload["exists"],
            "doc_count": payload["doc_count"],
        })
    return {
        "root": objectstore.docs_store().location,
        "url_prefix": url_prefix,
        "ecosystems": ecosystems,
    }


def read(
    session: Session,
    ecosystem: str,
    doc_id: str,
    *,
    api_prefix: str = "/api/v1",
) -> dict[str, Any]:
    """One document's source, metadata and asset list."""
    row = require_document(session, ecosystem, doc_id)
    assets = _assets(session, row.id)
    entry = _entry(
        row,
        asset_count=len(assets),
        url_prefix="/docs",
        api_prefix=api_prefix,
    )
    entry["content"] = _read_body(session, row)
    entry["assets"] = [
        _asset_entry(asset, ecosystem=ecosystem, slug=row.slug, url_prefix="/docs")
        for asset in assets
    ]
    return entry


def history(session: Session, ecosystem: str, doc_id: str) -> list[dict[str, Any]]:
    """Every stored revision, newest first."""
    row = require_document(session, ecosystem, doc_id)
    revisions = session.scalars(
        select(DocumentRevision)
        .where(DocumentRevision.document_id == row.id)
        .order_by(DocumentRevision.revision.desc())
    )
    return [
        {
            "revision": item.revision,
            "title": item.title,
            "size": item.size,
            "size_human": human_size(item.size),
            "sha256": item.sha256,
            "created": _iso(item.created_at),
            "created_by": item.created_by,
            "current": item.revision == row.revision,
        }
        for item in revisions
    ]


def open_body(
    session: Session,
    ecosystem: str,
    doc_id: str,
    *,
    revision: int | None = None,
) -> tuple[ObjectInfo, IO[bytes], str]:
    """One revision's Markdown, ready to stream: ``(info, stream, media type)``.

    The store metadata and the media type travel with the stream because the
    HTTP layer needs them — size and modification time build ``Content-Length``,
    ``Last-Modified`` and ``ETag``, and the type builds ``Content-Type`` — and
    asking the store or the row a second time would be both wasteful and a
    second chance to race a concurrent save.

    *revision* reads a stored historical version; ``None`` reads the current
    one.  A missing revision raises ``FileNotFoundError`` like a missing object
    does, because to the caller they are the same answer.
    """
    row = require_document(session, ecosystem, doc_id)
    store = objectstore.docs_store()
    chosen = None
    if revision is not None and revision != row.revision:
        chosen = session.scalars(
            select(DocumentRevision).where(
                DocumentRevision.document_id == row.id,
                DocumentRevision.revision == revision,
            )
        ).first()
        if chosen is None:
            raise FileNotFoundError(f"{row.slug}@{revision}")
    key = chosen.storage_key if chosen is not None else row.storage_key
    content_type = (
        chosen.content_type if chosen is not None else DOC_CONTENT_TYPE
    ) or DOC_CONTENT_TYPE

    info = store.stat(key)
    if info is None:
        # The row exists but its bytes do not: the state the integrity check
        # reports. A download answers 404 rather than a 500.
        raise FileNotFoundError(key)
    return info, store.open(key), content_type


def open_asset(
    session: Session,
    ecosystem: str,
    doc_id: str,
    name: str,
) -> tuple[ObjectInfo, IO[bytes], DocumentAsset]:
    """One asset's bytes: ``(object info, stream, asset row)``.

    The row comes back rather than just the stream because the caller needs the
    *name* (the URL identity) and the content type, and both live on it.
    """
    row = require_document(session, ecosystem, doc_id)
    asset = _find_asset(session, row, normalize_asset_name(name))
    store = objectstore.docs_store()
    info = store.stat(asset.storage_key)
    if info is None:
        raise FileNotFoundError(asset.storage_key)
    return info, store.open(asset.storage_key), asset


def list_assets(
    session: Session,
    ecosystem: str,
    doc_id: str,
    *,
    url_prefix: str = "/docs",
) -> list[dict[str, Any]]:
    """Every asset of one document, alphabetically by name."""
    row = require_document(session, ecosystem, doc_id)
    return [
        _asset_entry(asset, ecosystem=row.ecosystem, slug=row.slug, url_prefix=url_prefix)
        for asset in _assets(session, row.id)
    ]


def _read_body(session: Session, row: Document) -> str:
    """The stored Markdown of one document, decoded."""
    with objectstore.docs_store().open(row.storage_key) as handle:
        return handle.read().decode("utf-8")


def _document_keys(session: Session, row: Document) -> list[str]:
    """Every object key one document owns — bodies and assets alike.

    Collected from the rows, which is the whole point of the rows: nothing has
    to guess a key from a naming convention.
    """
    keys = [
        item.storage_key
        for item in session.scalars(
            select(DocumentRevision).where(DocumentRevision.document_id == row.id)
        )
    ]
    keys.extend(asset.storage_key for asset in _assets(session, row.id))
    return [key for key in keys if key]


# ── Write path ───────────────────────────────────────────────────────

def _store_revision(
    session: Session,
    row: Document,
    *,
    content: str,
    title: str,
    actor: str | None,
) -> None:
    """Write one new revision of *row* — object first, then the rows.

    The object is written before the transaction commits, which is the only
    order that cannot lose data: a crash in between leaves an unreferenced
    object (cheap, and what the integrity check reports) rather than a row
    pointing at bytes that do not exist.  If the commit itself fails, the object
    this call just wrote is removed again so the store does not accumulate
    orphans from a rejected save.
    """
    encoded = content.encode("utf-8")
    _check_doc_bytes(encoded)
    digest = sha256_text(content)

    revision = (row.revision or 0) + 1
    # The row needs its identity before the object can reference it; the object
    # key itself says nothing about the document (see `models.docs`).
    session.flush()
    key = new_key()
    store = objectstore.docs_store()
    store.put(key, (encoded,), content_type=DOC_CONTENT_TYPE)

    try:
        row.revision = revision
        row.title = title
        row.storage_key = key
        row.size = len(encoded)
        row.sha256 = digest
        row.modified_at = datetime.now(tz=timezone.utc)
        row.modified_by = actor
        session.add(
            DocumentRevision(
                document_id=row.id,
                revision=revision,
                storage_key=key,
                size=len(encoded),
                sha256=digest,
                content_type=DOC_CONTENT_TYPE,
                title=title,
                created_by=actor,
            )
        )
        session.commit()
    except Exception:
        session.rollback()
        try:
            store.delete(key)
        except OSError:  # pragma: no cover - a store that cannot delete
            pass
        raise


def save_document(
    session: Session,
    ecosystem: str,
    *,
    title: str,
    content: str = "",
    doc_id: str | None = None,
    actor: str | None = None,
) -> tuple[dict[str, Any], bool]:
    """Create a document, or replace one whose slug already exists.

    Returns ``(entry, replaced)``.  Saving under an existing title/slug
    **replaces** its content — the create route has always behaved that way, and
    it is now a new revision rather than a destructive overwrite.
    """
    require_ecosystem(ecosystem)
    if not isinstance(content, str):
        raise ValueError("文档内容必须是文本")
    encoded = content.encode("utf-8")
    _check_doc_bytes(encoded)

    clean_title = normalize_title(title)
    slug = normalize_doc_id(doc_id) if doc_id else slugify(clean_title)

    row = _find(session, ecosystem, slug)
    replaced = row is not None
    if row is None:
        row = Document(
            ecosystem=ecosystem,
            slug=slug,
            title=clean_title,
            revision=0,
            storage_key="",
            size=0,
            created_by=actor,
            modified_by=actor,
        )
        session.add(row)
    _store_revision(session, row, content=content, title=clean_title, actor=actor)

    return (
        _entry(row, asset_count=len(_assets(session, row.id)), url_prefix="/docs", api_prefix="/api/v1"),
        replaced,
    )


def save_content(
    session: Session,
    ecosystem: str,
    doc_id: str,
    content: str,
    *,
    revision: int | None = None,
    actor: str | None = None,
) -> dict[str, Any]:
    """Rewrite one document's Markdown as a new revision.

    If the new source opens with a ``#`` heading it becomes the document's
    title (so the list follows the document); otherwise the stored title is
    kept, which is what lets an empty document still have a name.

    *revision* is the optimistic-concurrency check: the editor passes the
    revision it loaded, and a save whose base is stale is refused with
    :class:`errors.RevisionConflictError` (``409``) instead of overwriting the
    other edit.  Omitting it keeps the historical last-write-wins behaviour for
    API clients that cannot carry one — the SPA always sends it.
    """
    if not isinstance(content, str):
        raise ValueError("文档内容必须是文本")
    encoded = content.encode("utf-8")
    _check_doc_bytes(encoded)

    row = require_document(session, ecosystem, doc_id)
    if revision is not None and revision != row.revision:
        raise RevisionConflictError(row.slug, expected=revision, current=row.revision)

    heading = heading_in_text(content)
    title = heading or (row.title or "").strip() or row.slug
    _store_revision(session, row, content=content, title=normalize_title(title), actor=actor)

    return _entry(
        row,
        asset_count=len(_assets(session, row.id)),
        url_prefix="/docs",
        api_prefix="/api/v1",
    )


def delete(session: Session, ecosystem: str, doc_id: str) -> dict[str, Any]:
    """Remove one document, its revisions and its assets."""
    row = require_document(session, ecosystem, doc_id)
    entry = _entry(
        row,
        asset_count=len(_assets(session, row.id)),
        url_prefix="/docs",
        api_prefix="/api/v1",
    )
    keys = _document_keys(session, row)
    # Rows first, objects second: the database is the index, so a delete that
    # commits and then fails to remove bytes is recoverable (the integrity check
    # reports the orphans) while the reverse would be a document that cannot be
    # read or deleted.
    session.delete(row)
    session.commit()
    try:
        objectstore.docs_store().delete_many(keys)
    except OSError:  # pragma: no cover - a store that cannot delete
        pass
    return entry


# ── Assets ───────────────────────────────────────────────────────────

def save_asset(
    session: Session,
    ecosystem: str,
    doc_id: str,
    name: str,
    data: bytes,
    *,
    actor: str | None = None,
) -> dict[str, Any]:
    """Store one asset inside a document's own asset set."""
    if not isinstance(data, (bytes, bytearray)):
        raise ValueError("资源内容必须是二进制数据")
    if len(data) > MAX_ASSET_BYTES:
        raise ValueError(f"资源超过 {MAX_ASSET_BYTES // (1024 * 1024)} MiB 上限")

    row = require_document(session, ecosystem, doc_id)
    filename = normalize_asset_name(name)
    digest = sha256_of([bytes(data)])
    content_type = _guess_content_type(filename)
    store = objectstore.docs_store()
    # Every upload gets its own key: re-uploading a name replaces the row, not
    # the bytes under it, so nothing can read a half-updated object.
    key = new_key()
    store.put(key, (bytes(data),), content_type=content_type)

    asset = session.scalars(
        select(DocumentAsset).where(
            DocumentAsset.document_id == row.id,
            DocumentAsset.name == filename,
        )
    ).first()
    superseded = asset.storage_key if asset is not None else None
    try:
        if asset is None:
            asset = DocumentAsset(
                document_id=row.id,
                name=filename,
                storage_key=key,
                size=len(data),
                sha256=digest,
                content_type=content_type,
                created_by=actor,
            )
            session.add(asset)
        else:
            asset.storage_key = key
            asset.size = len(data)
            asset.sha256 = digest
            asset.content_type = content_type
        session.commit()
    except Exception:
        session.rollback()
        try:
            store.delete(key)
        except OSError:  # pragma: no cover
            pass
        raise
    if superseded and superseded != key:
        # Best-effort: the row already points at the new object, so a failure
        # here leaves an orphan for the integrity check, never a broken asset.
        try:
            store.delete(superseded)
        except OSError:  # pragma: no cover
            pass

    return _asset_entry(asset, ecosystem=ecosystem, slug=row.slug, url_prefix="/docs")


def delete_asset(session: Session, ecosystem: str, doc_id: str, name: str) -> dict[str, Any]:
    """Remove one asset from a document."""
    row = require_document(session, ecosystem, doc_id)
    asset = _find_asset(session, row, normalize_asset_name(name))
    entry = _asset_entry(asset, ecosystem=ecosystem, slug=row.slug, url_prefix="/docs")
    key = asset.storage_key
    session.delete(asset)
    session.commit()
    try:
        objectstore.docs_store().delete(key)
    except OSError:  # pragma: no cover
        pass
    return entry


# ── Import / export: the folder tree as a bridge, not a store ────────

@dataclass(slots=True)
class ImportReport:
    """What ``cli.py docs import`` did (or would do, with ``dry_run``)."""

    documents: int = 0
    replaced: int = 0
    unchanged: int = 0
    assets: int = 0
    errors: list[str] = field(default_factory=list)

    def lines(self) -> list[str]:
        out = [
            f"新建文档 {self.documents} 篇",
            f"覆盖文档 {self.replaced} 篇",
            f"内容未变（跳过） {self.unchanged} 篇",
            f"导入资源 {self.assets} 个",
        ]
        if self.errors:
            out.append(f"跳过 {len(self.errors)} 项：")
            out.extend(f"  - {item}" for item in self.errors)
        return out


def _read_tree_meta(doc_dir: Path) -> dict[str, Any]:
    data = read_json(doc_dir / META_FILENAME, default={})
    return data if isinstance(data, dict) else {}


def _tree_title(doc_dir: Path, slug: str, content: str) -> str:
    meta = _read_tree_meta(doc_dir)
    title = str(meta.get("title") or "").strip()
    if title:
        return title
    return heading_in_text(content) or slug


def _tree_timestamp(meta: dict[str, Any], key: str) -> datetime | None:
    """One ``created``/``modified`` stamp out of a ``meta.json``, if usable.

    Imports carry the tree's own timestamps across, so a migration does not
    rewrite the whole catalog's history to "the afternoon the import ran" — and
    a restore of an export lands with the dates an operator would expect.
    """
    raw = str(meta.get(key) or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def import_tree(
    session: Session,
    source: str | Path,
    *,
    actor: str | None = None,
    dry_run: bool = False,
) -> ImportReport:
    """Read a folder tree into the database, one document per transaction.

    The tree is the layout :func:`export_tree` writes and the layout this
    feature used to *be*: ``<ecosystem>/<slug>/document.md`` with an optional
    ``meta.json`` and ``assets/``.  Re-running it is safe and cheap: a document
    whose content and title are already stored is counted as ``unchanged`` and
    does **not** get another revision, so an operator can re-import after adding
    one file without inflating the history.

    A failure on one document is recorded in the report and does not stop the
    rest: an import is a migration, and a single unreadable file must not leave
    a deployment half-migrated with no record of where it stopped.
    """
    report = ImportReport()
    root = Path(source)
    if not root.is_dir():
        raise ValueError(f"目录不存在：{root}")

    for ecosystem in ECOSYSTEMS:
        eco_dir = root / ecosystem
        if not eco_dir.is_dir():
            continue
        for doc_dir in sorted((p for p in eco_dir.iterdir() if p.is_dir()), key=lambda p: p.name.lower()):
            body = doc_dir / DOC_FILENAME
            if body.name == META_FILENAME or not body.is_file():
                continue
            try:
                _import_document(session, ecosystem, doc_dir, report, actor=actor, dry_run=dry_run)
            except (OSError, ValueError) as exc:
                session.rollback()
                report.errors.append(f"{ecosystem}/{doc_dir.name}: {exc}")
    return report


def _import_document(
    session: Session,
    ecosystem: str,
    doc_dir: Path,
    report: ImportReport,
    *,
    actor: str | None,
    dry_run: bool,
) -> None:
    slug = normalize_doc_id(doc_dir.name)
    raw = (doc_dir / DOC_FILENAME).read_bytes()
    _check_doc_bytes(raw)
    content = raw.decode("utf-8")
    title = normalize_title(_tree_title(doc_dir, slug, content))
    digest = sha256_text(content)

    row = _find(session, ecosystem, slug)
    unchanged = row is not None and row.sha256 == digest and row.title == title
    if unchanged:
        report.unchanged += 1
    elif dry_run:
        if row is None:
            report.documents += 1
        else:
            report.replaced += 1
    else:
        if row is None:
            row = Document(
                ecosystem=ecosystem,
                slug=slug,
                title=title,
                revision=0,
                storage_key="",
                size=0,
                created_by=actor,
                modified_by=actor,
            )
            session.add(row)
            report.documents += 1
        else:
            report.replaced += 1
        _store_revision(session, row, content=content, title=title, actor=actor)
        meta = _read_tree_meta(doc_dir)
        created = _tree_timestamp(meta, "created")
        modified = _tree_timestamp(meta, "modified")
        if created is not None or modified is not None:
            row.created_at = created or row.created_at
            row.modified_at = modified or row.modified_at
            session.commit()

    assets_dir = doc_dir / ASSETS_DIRNAME
    if not assets_dir.is_dir():
        return
    for asset_path in sorted((p for p in assets_dir.iterdir() if p.is_file()), key=lambda p: p.name.lower()):
        try:
            name = normalize_asset_name(asset_path.name)
        except ValueError as exc:
            report.errors.append(f"{ecosystem}/{slug}/{asset_path.name}: {exc}")
            continue
        if dry_run:
            report.assets += 1
            continue
        data = asset_path.read_bytes()
        if len(data) > MAX_ASSET_BYTES:
            report.errors.append(f"{ecosystem}/{slug}/{name}: 资源超过上限，已跳过")
            continue
        if row is None:  # pragma: no cover - created above unless dry_run
            continue
        save_asset(session, ecosystem, slug, name, data, actor=actor)
        report.assets += 1


def export_tree(session: Session, target: str | Path) -> int:
    """Write every document back out as a folder tree; return the count.

    This is the portability half of :func:`import_tree` — a backup an operator
    can read, diff and copy into a disconnected deployment.  The target is an
    ordinary directory the caller names; it is deliberately *not* the docs store
    root, so exporting cannot collide with the objects the database owns.
    """
    root = Path(target)
    store = objectstore.docs_store()
    count = 0
    rows = session.scalars(
        select(Document).order_by(Document.ecosystem, func.lower(Document.slug))
    )
    for row in rows:
        doc_dir = root / row.ecosystem / row.slug
        with store.open(row.storage_key) as handle:
            body = handle.read()
        atomic_write_bytes(doc_dir / DOC_FILENAME, body)
        write_json(doc_dir / META_FILENAME, {
            "title": row.title,
            "created": _iso(row.created_at),
            "modified": _iso(row.modified_at),
            "revision": row.revision,
        })
        for asset in _assets(session, row.id):
            with store.open(asset.storage_key) as handle:
                atomic_write_bytes(doc_dir / ASSETS_DIRNAME / asset.name, handle.read())
        count += 1
    return count


__all__ = [
    "ASSETS_DIRNAME",
    "DOC_FILENAME",
    "ECOSYSTEMS",
    "META_FILENAME",
    "MAX_ASSET_BYTES",
    "MAX_ASSET_NAME_LENGTH",
    "MAX_DOC_BYTES",
    "MAX_DOC_ID_LENGTH",
    "MAX_TITLE_LENGTH",
    "ImportReport",
    "delete",
    "delete_asset",
    "export_tree",
    "heading_in_text",
    "history",
    "import_tree",
    "is_image_name",
    "list_assets",
    "normalize_asset_name",
    "normalize_doc_id",
    "normalize_title",
    "open_asset",
    "open_body",
    "read",
    "require_document",
    "require_ecosystem",
    "save_asset",
    "save_content",
    "save_document",
    "scan",
    "scan_all",
    "slugify",
]
