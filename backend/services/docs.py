"""Per-ecosystem Markdown documentation stored as **folder projects**.

Every ecosystem in the sidebar owns a *documentation leaf*, and one document is
a lightweight project directory rather than a loose file::

    docs/
      python/
        getting-started/          # one document = one folder
          document.md             # the Markdown source
          meta.json               # {title, created, modified}
          assets/                 # images and other files the document uses
            architecture.png
        install-notes/
          document.md
      npm/
        publishing/
          document.md

A folder is the unit of the catalog, so a document can carry its own images
without every ecosystem sharing one flat pile of files.  The filesystem stays
the source of truth (the directory *is* the catalog), which means a change is
visible on the next request with no restart and no database.

Write access is deliberately narrow.  Reading and downloading require
``doc:read`` (held by the built-in ``authenticated`` role); creating, editing,
deleting a document or uploading one of its assets requires ``doc:upload``
(held only by the built-in ``admin`` role).  There is no way to write outside
the ecosystem directory: :func:`normalize_doc_id` and
:func:`normalize_asset_name` pin every name to a single path segment, and the
``resolve_*`` helpers re-check the result against the root.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

log = logging.getLogger("cpypiserver.docs")

#: The ecosystems that own a documentation leaf, in sidebar order.
ECOSYSTEMS: tuple[str, ...] = ("python", "npm", "docker", "debian", "tools", "models")

#: Inside a document folder.
DOC_FILENAME = "document.md"
META_FILENAME = "meta.json"
ASSETS_DIRNAME = "assets"

#: Markdown documents are small; a couple of megabytes is already an enormous
#: handbook chapter.  Assets (screenshots, diagrams) get a larger, separate
#: budget.
MAX_DOC_BYTES = 2 * 1024 * 1024
MAX_ASSET_BYTES = 16 * 1024 * 1024

MAX_DOC_ID_LENGTH = 96
MAX_TITLE_LENGTH = 160
MAX_ASSET_NAME_LENGTH = 160

_FORBIDDEN_CHARS = set('/\\:*?"<>|')
_DASH_RUN_RE = re.compile(r"-{2,}")
_IMAGE_EXTS = frozenset({
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg",
    ".bmp", ".avif", ".ico", ".tif", ".tiff",
})

#: Names a document folder may not use, because they are the project's own
#: structure (or the preview endpoint's path segment).
_RESERVED_IDS = frozenset({DOC_FILENAME, META_FILENAME, ASSETS_DIRNAME, "preview"})


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
    """Derive a stable, URL-safe folder id from a title.

    ASCII is lower-cased and whitespace becomes ``-``; a CJK title is kept as
    is (a Chinese folder name is perfectly valid and far friendlier than a
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
    """Validate a document folder id.

    Accepts exactly one path segment: no separators, no leading dot, no control
    characters, and never the names the project uses for its own files.
    """
    candidate = (value or "").strip()
    if not candidate:
        raise ValueError("文档标识为空")
    if len(candidate) > MAX_DOC_ID_LENGTH:
        raise ValueError(f"文档标识过长（最多 {MAX_DOC_ID_LENGTH} 个字符）")
    if candidate.startswith("."):
        raise ValueError("文档标识不能以 '.' 开头")
    if any(ch in _FORBIDDEN_CHARS or ord(ch) < 32 for ch in candidate):
        raise ValueError("文档标识不能包含路径分隔符或控制字符")
    if candidate in _RESERVED_IDS:
        raise ValueError(f"文档标识 {candidate!r} 是保留名")
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


# ── Path resolution ──────────────────────────────────────────────────

def _base(root: str) -> Path:
    return Path(root)


def resolve_eco_dir(root: str, ecosystem: str) -> Path:
    """Return the directory holding *ecosystem*'s document projects.

    Raises ``KeyError`` for an unknown ecosystem so a typo in a URL becomes a
    404 rather than an arbitrary directory on disk.
    """
    if ecosystem not in ECOSYSTEMS:
        raise KeyError(ecosystem)
    return _base(root) / ecosystem


def _contained(path: Path, base: Path) -> Path:
    if not path.resolve().is_relative_to(base.resolve()):
        raise ValueError("文档路径越界")
    return path


def resolve_doc_dir(root: str, ecosystem: str, doc_id: str) -> Path:
    """Return one document's project directory (which may not exist yet)."""
    eco_dir = resolve_eco_dir(root, ecosystem)
    return _contained(eco_dir / normalize_doc_id(doc_id), eco_dir)


def resolve_doc_file(root: str, ecosystem: str, doc_id: str) -> Path:
    """Return the Markdown source of one document.

    Raises ``KeyError`` (unknown ecosystem), ``ValueError`` (bad id) or
    ``FileNotFoundError`` (no such document).
    """
    path = resolve_doc_dir(root, ecosystem, doc_id) / DOC_FILENAME
    if not path.is_file():
        raise FileNotFoundError(doc_id)
    return path


def resolve_asset(root: str, ecosystem: str, doc_id: str, name: str) -> Path:
    """Return the on-disk path of one asset of one document."""
    assets_dir = resolve_doc_dir(root, ecosystem, doc_id) / ASSETS_DIRNAME
    path = _contained(assets_dir / normalize_asset_name(name), assets_dir)
    if not path.is_file():
        raise FileNotFoundError(name)
    return path


# ── Small helpers ────────────────────────────────────────────────────

def human_size(num: float) -> str:
    """Format a byte count the way the rest of the UI does."""
    step = 1024.0
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num) < step or unit == "TB":
            if unit == "B":
                return f"{int(num)} {unit}"
            return f"{num:.1f} {unit}"
        num /= step
    return f"{num:.1f} TB"


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _atomic_write(path: Path, data: bytes) -> None:
    """Write *data* to *path* through a temp file + ``os.replace``.

    A concurrent reader therefore sees either the old file or the new one,
    never a half-written one.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:  # pragma: no cover - best effort cleanup
            pass
        raise


def heading_in_text(text: str) -> str:
    """The first ``#`` heading in a Markdown string, or ``""``."""
    for line in (text or "").splitlines()[:200]:  # only the head matters
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip().rstrip("#").strip()
    return ""


def first_heading(path: Path) -> str:
    """The document's first ``#`` heading, or an empty string."""
    try:
        return heading_in_text(path.read_text(encoding="utf-8", errors="replace"))
    except OSError as exc:  # pragma: no cover - races with a deletion
        log.debug("cannot read heading of %s: %s", path, exc)
        return ""


def _read_meta(doc_dir: Path) -> dict[str, Any]:
    try:
        raw = json.loads((doc_dir / META_FILENAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _write_meta(
    doc_dir: Path,
    *,
    title: str,
    created: str | None = None,
) -> dict[str, Any]:
    now = datetime.now(tz=timezone.utc).isoformat()
    payload = {"title": title, "created": created or now, "modified": now}
    _atomic_write(
        doc_dir / META_FILENAME,
        (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
    )
    return payload


def document_title(doc_dir: Path, doc_id: str) -> str:
    """The list title: the stored title, else the first heading, else the id."""
    title = str(_read_meta(doc_dir).get("title") or "").strip()
    if title:
        return title
    return first_heading(doc_dir / DOC_FILENAME) or doc_id


def is_image_name(name: str) -> bool:
    return Path(name).suffix.lower() in _IMAGE_EXTS


# ── Assets ───────────────────────────────────────────────────────────

def _asset_entry(
    path: Path, *, ecosystem: str, doc_id: str, url_prefix: str
) -> dict[str, Any]:
    stat = path.stat()
    base = url_prefix.rstrip("/")
    return {
        "name": path.name,
        "size": stat.st_size,
        "size_human": human_size(stat.st_size),
        "modified": _iso(stat.st_mtime),
        "is_image": is_image_name(path.name),
        "url": f"{base}/{quote(ecosystem)}/{quote(doc_id)}/{ASSETS_DIRNAME}/{quote(path.name)}",
    }


def _assets_in(
    assets_dir: Path, *, ecosystem: str, doc_id: str, url_prefix: str
) -> list[dict[str, Any]]:
    if not assets_dir.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for path in sorted(assets_dir.iterdir(), key=lambda p: p.name.lower()):
        if not path.is_file() or path.name.startswith("."):
            continue
        try:
            normalize_asset_name(path.name)
        except ValueError:
            log.debug("skipping unservable asset %r", path.name)
            continue
        out.append(
            _asset_entry(path, ecosystem=ecosystem, doc_id=doc_id, url_prefix=url_prefix)
        )
    return out


def list_assets(
    root: str, ecosystem: str, doc_id: str, *, url_prefix: str = "/docs"
) -> list[dict[str, Any]]:
    """Every asset of one document, alphabetically."""
    assets_dir = resolve_doc_dir(root, ecosystem, doc_id) / ASSETS_DIRNAME
    return _assets_in(
        assets_dir, ecosystem=ecosystem, doc_id=doc_id, url_prefix=url_prefix
    )


def save_asset(
    root: str, ecosystem: str, doc_id: str, name: str, data: bytes
) -> dict[str, Any]:
    """Store one asset inside a document's own ``assets/`` directory."""
    if not isinstance(data, (bytes, bytearray)):
        raise ValueError("资源内容必须是二进制数据")
    if len(data) > MAX_ASSET_BYTES:
        raise ValueError(f"资源超过 {MAX_ASSET_BYTES // (1024 * 1024)} MiB 上限")

    doc_dir = resolve_doc_dir(root, ecosystem, doc_id)
    if not (doc_dir / DOC_FILENAME).is_file():
        raise FileNotFoundError(doc_id)

    filename = normalize_asset_name(name)
    assets_dir = doc_dir / ASSETS_DIRNAME
    target = _contained(assets_dir / filename, assets_dir)
    _atomic_write(target, bytes(data))
    log.info("asset %s/%s/%s saved (%d bytes)", ecosystem, doc_id, filename, len(data))
    return _asset_entry(
        target, ecosystem=ecosystem, doc_id=doc_id, url_prefix="/docs"
    )


def delete_asset(root: str, ecosystem: str, doc_id: str, name: str) -> dict[str, Any]:
    """Remove one asset from a document project."""
    path = resolve_asset(root, ecosystem, doc_id, name)
    entry = _asset_entry(
        path, ecosystem=ecosystem, doc_id=doc_id, url_prefix="/docs"
    )
    path.unlink()
    log.info("asset %s/%s/%s deleted", ecosystem, doc_id, path.name)
    return entry


# ── Catalog ──────────────────────────────────────────────────────────

def _doc_entry(
    doc_dir: Path,
    doc_id: str,
    *,
    ecosystem: str,
    url_prefix: str,
    api_prefix: str,
) -> dict[str, Any]:
    meta = _read_meta(doc_dir)
    md_path = doc_dir / DOC_FILENAME
    stat = md_path.stat() if md_path.is_file() else None
    base = url_prefix.rstrip("/")
    api = api_prefix.rstrip("/")
    raw = f"{base}/{quote(ecosystem)}/{quote(doc_id)}"
    assets = _assets_in(
        doc_dir / ASSETS_DIRNAME,
        ecosystem=ecosystem,
        doc_id=doc_id,
        url_prefix=url_prefix,
    )
    return {
        "id": doc_id,
        "title": document_title(doc_dir, doc_id),
        "filename": DOC_FILENAME,
        "size": stat.st_size if stat else 0,
        "size_human": human_size(stat.st_size if stat else 0),
        "modified": _iso(stat.st_mtime) if stat else None,
        "created": meta.get("created"),
        "asset_count": len(assets),
        # Read/download the raw Markdown exactly as authored.
        "download_url": f"{raw}?download=1",
        "raw_url": raw,
        "assets_url": f"{api}/docs/{quote(ecosystem)}/{quote(doc_id)}/assets",
    }


def scan(
    root: str,
    ecosystem: str,
    *,
    url_prefix: str = "/docs",
    api_prefix: str = "/api/v1",
) -> dict[str, Any]:
    """Catalog one ecosystem's document projects (alphabetical by id)."""
    base = resolve_eco_dir(root, ecosystem)
    documents: list[dict[str, Any]] = []
    if base.is_dir():
        for path in sorted(base.iterdir(), key=lambda p: p.name.lower()):
            if not path.is_dir() or path.name.startswith("."):
                continue
            if not (path / DOC_FILENAME).is_file():
                continue
            try:
                doc_id = normalize_doc_id(path.name)
            except ValueError:
                log.debug("skipping unservable document folder %r", path.name)
                continue
            documents.append(
                _doc_entry(
                    path,
                    doc_id,
                    ecosystem=ecosystem,
                    url_prefix=url_prefix,
                    api_prefix=api_prefix,
                )
            )
    return {
        "ecosystem": ecosystem,
        "root": str(base),
        "exists": base.is_dir(),
        "url_prefix": url_prefix,
        "doc_count": len(documents),
        "documents": documents,
    }


def scan_all(
    root: str, *, url_prefix: str = "/docs", api_prefix: str = "/api/v1"
) -> dict[str, Any]:
    """Every ecosystem's document count — the landing data for the SPA."""
    ecosystems: list[dict[str, Any]] = []
    for key in ECOSYSTEMS:
        payload = scan(root, key, url_prefix=url_prefix, api_prefix=api_prefix)
        ecosystems.append({
            "key": key,
            "exists": payload["exists"],
            "doc_count": payload["doc_count"],
        })
    return {
        "root": str(_base(root)),
        "url_prefix": url_prefix,
        "ecosystems": ecosystems,
    }


def read(
    root: str,
    ecosystem: str,
    doc_id: str,
    *,
    api_prefix: str = "/api/v1",
) -> dict[str, Any]:
    """Read one document's source, metadata and asset list."""
    path = resolve_doc_file(root, ecosystem, doc_id)
    entry = _doc_entry(
        path.parent,
        normalize_doc_id(doc_id),
        ecosystem=ecosystem,
        url_prefix="/docs",
        api_prefix=api_prefix,
    )
    entry["content"] = path.read_text(encoding="utf-8")
    entry["assets"] = _assets_in(
        path.parent / ASSETS_DIRNAME,
        ecosystem=ecosystem,
        doc_id=doc_id,
        url_prefix="/docs",
    )
    return entry


# ── Write path ───────────────────────────────────────────────────────

def save_document(
    root: str,
    ecosystem: str,
    *,
    title: str,
    content: str = "",
    doc_id: str | None = None,
) -> tuple[dict[str, Any], bool]:
    """Create or replace a document, returning ``(entry, replaced)``.

    The folder id is derived from the title unless one is supplied.  Saving a
    document whose title/id already exists **replaces** its content — that is
    the documented way to change a document, and it never creates a duplicate.
    An empty *content* is a valid, empty document.
    """
    if not isinstance(content, str):
        raise ValueError("文档内容必须是文本")
    encoded = content.encode("utf-8")
    if len(encoded) > MAX_DOC_BYTES:
        raise ValueError(f"文档超过 {MAX_DOC_BYTES // (1024 * 1024)} MiB 上限")

    clean_title = normalize_title(title)
    chosen_id = normalize_doc_id(doc_id) if doc_id else slugify(clean_title)
    doc_dir = resolve_doc_dir(root, ecosystem, chosen_id)
    existed = (doc_dir / DOC_FILENAME).is_file()
    meta = _read_meta(doc_dir)

    doc_dir.mkdir(parents=True, exist_ok=True)
    _atomic_write(doc_dir / DOC_FILENAME, encoded)
    _write_meta(doc_dir, title=clean_title, created=meta.get("created"))

    log.info(
        "document %s/%s %s (%d bytes)",
        ecosystem, chosen_id, "replaced" if existed else "created", len(encoded),
    )
    entry = _doc_entry(
        doc_dir,
        chosen_id,
        ecosystem=ecosystem,
        url_prefix="/docs",
        api_prefix="/api/v1",
    )
    return entry, existed


def save_content(root: str, ecosystem: str, doc_id: str, content: str) -> dict[str, Any]:
    """Rewrite one existing document's Markdown source.

    If the new source opens with a ``#`` heading it becomes the document's
    title (so the list follows the document); otherwise the stored title is
    kept, which is what lets an empty document still have a name.
    """
    if not isinstance(content, str):
        raise ValueError("文档内容必须是文本")
    encoded = content.encode("utf-8")
    if len(encoded) > MAX_DOC_BYTES:
        raise ValueError(f"文档超过 {MAX_DOC_BYTES // (1024 * 1024)} MiB 上限")

    doc_dir = resolve_doc_dir(root, ecosystem, doc_id)
    if not (doc_dir / DOC_FILENAME).is_file():
        raise FileNotFoundError(doc_id)

    _atomic_write(doc_dir / DOC_FILENAME, encoded)
    meta = _read_meta(doc_dir)
    heading = first_heading(doc_dir / DOC_FILENAME)
    title = heading or str(meta.get("title") or "").strip() or normalize_doc_id(doc_id)
    _write_meta(doc_dir, title=title, created=meta.get("created"))

    log.info("document %s/%s saved (%d bytes)", ecosystem, doc_id, len(encoded))
    return _doc_entry(
        doc_dir,
        normalize_doc_id(doc_id),
        ecosystem=ecosystem,
        url_prefix="/docs",
        api_prefix="/api/v1",
    )


def delete(root: str, ecosystem: str, doc_id: str) -> dict[str, Any]:
    """Remove one document project (source, metadata and assets)."""
    doc_dir = resolve_doc_dir(root, ecosystem, doc_id)
    if not doc_dir.is_dir():
        raise FileNotFoundError(doc_id)
    entry = _doc_entry(
        doc_dir,
        normalize_doc_id(doc_id),
        ecosystem=ecosystem,
        url_prefix="/docs",
        api_prefix="/api/v1",
    )
    shutil.rmtree(doc_dir)
    log.info("document %s/%s deleted", ecosystem, doc_id)
    return entry


__all__ = [
    "ECOSYSTEMS",
    "DOC_FILENAME",
    "META_FILENAME",
    "ASSETS_DIRNAME",
    "MAX_DOC_BYTES",
    "MAX_ASSET_BYTES",
    "normalize_title",
    "normalize_doc_id",
    "normalize_asset_name",
    "slugify",
    "resolve_eco_dir",
    "resolve_doc_dir",
    "resolve_doc_file",
    "resolve_asset",
    "document_title",
    "first_heading",
    "heading_in_text",
    "is_image_name",
    "scan",
    "scan_all",
    "read",
    "save_document",
    "save_content",
    "delete",
    "list_assets",
    "save_asset",
    "delete_asset",
    "human_size",
]
