"""Per-ecosystem Markdown documentation.

Every ecosystem in the sidebar owns a **documentation leaf**, and that leaf's
documents live in their own sub-directory of ``DOCS_DIR`` — nothing sits in the
top-level directory itself::

    docs/
      python/   getting-started.md
      npm/      publishing.md
      docker/   offline-images.md
      debian/   apt-sources.md
      tools/    authoring-tools.md
      models/   dsh-routing.md

The directory *is* the catalog, the same way ``tools/`` and ``npm/`` work: the
filesystem is the source of truth, so the next request sees a change with no
restart and no database.  The one deliberate difference is the write path — a
document is published **by uploading a ``.md`` file**, which only the holders of
``doc:upload`` (the built-in admin role) may do.  Everyone else reads and
downloads; there is no in-browser editor, so a document's history is exactly the
sequence of uploaded files.

Filenames are validated rather than trusted (see :func:`normalize_name`): a
document is always a direct child of its ecosystem directory, never a path.
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

log = logging.getLogger("cpypiserver.docs")

#: The ecosystems that own a documentation leaf, in sidebar order.
ECOSYSTEMS: tuple[str, ...] = ("python", "npm", "docker", "debian", "tools", "models")

#: Markdown documents are small; a couple of megabytes is already an enormous
#: handbook chapter, and refusing more protects the disk from an accidental
#: multi-gigabyte upload.
MAX_DOC_BYTES = 2 * 1024 * 1024

#: Filenames must be a single path segment ending in ``.md``.
MAX_NAME_LENGTH = 128
_FORBIDDEN_CHARS = set('/\\:*?"<>|')


def normalize_name(name: str) -> str:
    """Validate a caller-supplied Markdown filename.

    Accepts a single path segment (no separators), rejects dotfiles and control
    characters, and requires the ``.md`` extension.  Chinese filenames are fine
    — the URL builder quotes them — but ``../`` traversal never is.
    """
    candidate = (name or "").strip()
    if not candidate:
        raise ValueError("文档名为空")
    if len(candidate) > MAX_NAME_LENGTH:
        raise ValueError(f"文档名过长（最多 {MAX_NAME_LENGTH} 个字符）")
    if candidate.startswith("."):
        raise ValueError("文档名不能以 '.' 开头")
    if any(ch in _FORBIDDEN_CHARS or ord(ch) < 32 for ch in candidate):
        raise ValueError("文档名不能包含路径分隔符或控制字符")
    if candidate.lower().endswith(".md") is False:
        raise ValueError("文档必须是 Markdown 文件（.md 结尾）")
    return candidate


def _base(root: str) -> Path:
    return Path(root)


def resolve_dir(root: str, ecosystem: str) -> Path:
    """Return the directory holding *ecosystem*'s documents.

    Raises ``KeyError`` for an unknown ecosystem so a typo in a URL becomes a
    404 rather than an arbitrary directory on disk.
    """
    if ecosystem not in ECOSYSTEMS:
        raise KeyError(ecosystem)
    return _base(root) / ecosystem


def resolve_file(root: str, ecosystem: str, name: str) -> Path:
    """Return the on-disk path of one document.

    Raises ``KeyError`` (unknown ecosystem), ``ValueError`` (bad filename) or
    ``FileNotFoundError`` (no such document).
    """
    base = resolve_dir(root, ecosystem)
    filename = normalize_name(name)
    path = base / filename
    # `normalize_name` already pins the filename to one segment; the resolve
    # check is belt-and-braces against a future change to that function.
    if not path.resolve().is_relative_to(base.resolve()):
        raise ValueError("文档路径越界")
    if not path.is_file():
        raise FileNotFoundError(filename)
    return path


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


def title_of(path: Path) -> str:
    """The document's first ``#`` heading, or its filename without ``.md``.

    A heading is the author's chosen title, which is friendlier in a list than
    ``getting-started.md``.  A file that cannot be read — or holds no heading —
    falls back to the stem rather than failing the whole listing.
    """
    fallback = path.stem
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for _ in range(200):  # only the head of the file matters
                line = fh.readline()
                if not line:
                    break
                stripped = line.strip()
                if stripped.startswith("# "):
                    title = stripped[2:].strip().rstrip("#").strip()
                    return title or fallback
    except OSError as exc:  # pragma: no cover - races with a deletion
        log.debug("cannot read title of %s: %s", path, exc)
    return fallback


def _entry(path: Path, *, ecosystem: str, url_prefix: str) -> dict[str, Any]:
    stat = path.stat()
    base = url_prefix.rstrip("/")
    raw = f"{base}/{quote(ecosystem)}/{quote(path.name)}"
    return {
        "name": path.name,
        "title": title_of(path),
        "filename": path.name,
        "size": stat.st_size,
        "size_human": human_size(stat.st_size),
        "modified": _iso(stat.st_mtime),
        # Read/download the raw Markdown exactly as uploaded.
        "download_url": f"{raw}?download=1",
        "raw_url": raw,
    }


def scan(root: str, ecosystem: str, *, url_prefix: str = "/docs") -> dict[str, Any]:
    """Catalog one ecosystem's documents (alphabetical, Markdown only)."""
    base = resolve_dir(root, ecosystem)
    documents: list[dict[str, Any]] = []
    if base.is_dir():
        for path in sorted(base.iterdir(), key=lambda p: p.name.lower()):
            if not path.is_file() or path.suffix.lower() != ".md":
                continue
            try:
                normalize_name(path.name)
            except ValueError:
                log.debug("skipping unservable document name %r", path.name)
                continue
            documents.append(_entry(path, ecosystem=ecosystem, url_prefix=url_prefix))
    return {
        "ecosystem": ecosystem,
        "root": str(base),
        "exists": base.is_dir(),
        "url_prefix": url_prefix,
        "doc_count": len(documents),
        "documents": documents,
    }


def scan_all(root: str, *, url_prefix: str = "/docs") -> dict[str, Any]:
    """Every ecosystem's document count — the landing data for the SPA."""
    ecosystems: list[dict[str, Any]] = []
    for key in ECOSYSTEMS:
        payload = scan(root, key, url_prefix=url_prefix)
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


def read(root: str, ecosystem: str, name: str) -> dict[str, Any]:
    """Read one document as UTF-8 text plus its catalog metadata."""
    path = resolve_file(root, ecosystem, name)
    content = path.read_text(encoding="utf-8")
    entry = _entry(path, ecosystem=ecosystem, url_prefix="/docs")
    entry["content"] = content
    return entry


def save(root: str, ecosystem: str, name: str, content: str) -> dict[str, Any]:
    """Create or replace one document, atomically.

    The write goes to a temp file in the ecosystem directory and is then
    ``os.replace``-d over the target, so a concurrent reader sees either the old
    document or the new one — never a half-written file.  Uploading the same
    filename again is the documented way to change a document's content.
    """
    if not isinstance(content, str):
        raise ValueError("文档内容必须是文本")
    encoded = content.encode("utf-8")
    if len(encoded) > MAX_DOC_BYTES:
        raise ValueError(f"文档超过 {MAX_DOC_BYTES // (1024 * 1024)} MiB 上限")

    base = resolve_dir(root, ecosystem)
    filename = normalize_name(name)
    base.mkdir(parents=True, exist_ok=True)
    target = base / filename

    fd, tmp_name = tempfile.mkstemp(prefix=".upload-", suffix=".md", dir=base)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(encoded)
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:  # pragma: no cover - best effort cleanup
            pass
        raise

    log.info("document %s/%s saved (%d bytes)", ecosystem, filename, len(encoded))
    return _entry(target, ecosystem=ecosystem, url_prefix="/docs")


def delete(root: str, ecosystem: str, name: str) -> dict[str, Any]:
    """Remove one document; returns the entry that was removed."""
    path = resolve_file(root, ecosystem, name)
    entry = _entry(path, ecosystem=ecosystem, url_prefix="/docs")
    path.unlink()
    log.info("document %s/%s deleted", ecosystem, path.name)
    return entry


__all__ = [
    "ECOSYSTEMS",
    "MAX_DOC_BYTES",
    "normalize_name",
    "resolve_dir",
    "resolve_file",
    "title_of",
    "scan",
    "scan_all",
    "read",
    "save",
    "delete",
    "human_size",
]
