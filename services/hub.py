"""Artifact-hub catalogs — tools, npm and model routes.

Every catalog in this module follows the same rule: **the filesystem (or a
small JSON file) is the source of truth**.  There is no database, no upload
endpoint and no admin UI to keep in sync, so adding an artifact is a file copy
and the next request sees it.

Layout
------
``tools/``::

    tools/
      catalog.json            optional display overlay (names, descriptions)
      dev/                    a category (directory name = category key)
        fmt.sh
        lint.py
      ops/
        check-health.sh

``npm/``::

    npm/
      catalog.json            optional explicit list
      lodash-4.17.21.tgz      scanned as name + version

``config/model_routes.json``::

    {"routes": [{"name": ..., "provider": ..., "base_url": ...}]}
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

log = logging.getLogger("cpypiserver.hub")

#: Files that are catalog metadata rather than catalog entries.
_OVERLAY_FILENAME = "catalog.json"

#: Documentation next to the artifacts is not itself an artifact.
_DOC_PREFIXES = ("readme", "license", "changelog")

#: Files larger than this are listed without a SHA-256 — hashing multi-gigabyte
#: installers on every page load is not worth it.
_HASH_LIMIT_BYTES = 64 * 1024 * 1024

#: ``(absolute path, mtime, size) -> sha256``.  Bounded crudely: cleared when it
#: grows past this many entries, which is more than any scaffold will hold.
_HASH_CACHE_MAX = 512
_sha256_cache: dict[tuple[str, float, int], str] = {}


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


def _sha256(path: Path, stat: Any) -> str | None:
    """SHA-256 of *path*, cached on (path, mtime, size)."""
    if stat.st_size > _HASH_LIMIT_BYTES:
        return None
    key = (str(path), stat.st_mtime, stat.st_size)
    cached = _sha256_cache.get(key)
    if cached is not None:
        return cached
    digest = hashlib.sha256()
    try:
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                digest.update(chunk)
    except OSError as exc:  # pragma: no cover - races with a deletion
        log.debug("cannot hash %s: %s", path, exc)
        return None
    value = digest.hexdigest()
    if len(_sha256_cache) >= _HASH_CACHE_MAX:
        _sha256_cache.clear()
    _sha256_cache[key] = value
    return value


def _load_overlay(root: Path) -> dict[str, Any]:
    """Read ``catalog.json`` when present; never let a bad file 500 the page."""
    overlay = root / _OVERLAY_FILENAME
    if not overlay.is_file():
        return {}
    try:
        data = json.loads(overlay.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("ignoring unreadable %s: %s", overlay, exc)
        return {}
    return data if isinstance(data, dict) else {}


def _visible(path: Path) -> bool:
    """Skip dotfiles, the overlay and the README/licence that documents a tree."""
    if any(part.startswith(".") for part in path.parts):
        return False
    if path.name == _OVERLAY_FILENAME:
        return False
    return not path.name.lower().startswith(_DOC_PREFIXES)


# ── Tools catalog ────────────────────────────────────────────────────

def _tool_entry(
    root: Path,
    file_path: Path,
    *,
    url_prefix: str,
    meta: dict[str, Any],
) -> dict[str, Any]:
    stat = file_path.stat()
    rel = file_path.relative_to(root).as_posix()
    return {
        "name": meta.get("name") or file_path.name,
        "filename": file_path.name,
        "relative_path": rel,
        # Quote each segment but keep the separators so nested tools still work.
        "download_url": f"{url_prefix.rstrip('/')}/{quote(rel)}",
        "size": stat.st_size,
        "size_human": human_size(stat.st_size),
        "sha256": _sha256(file_path, stat),
        "modified": _iso(stat.st_mtime),
        "description": meta.get("description"),
        "tags": list(meta.get("tags") or []),
    }


def scan_tools(root: str, *, url_prefix: str = "/tools") -> dict[str, Any]:
    """Group every file under *root* into its category directory.

    Files sitting directly in *root* land in a synthetic ``root`` category the
    UI labels "uncategorized"; a category with no files is reported too, so an
    empty scaffold category is visible instead of silently missing.
    """
    base = Path(root)
    if not base.is_dir():
        return {
            "root": str(base),
            "exists": False,
            "url_prefix": url_prefix,
            "categories": [],
            "tool_count": 0,
        }

    overlay = _load_overlay(base)
    cat_meta: dict[str, Any] = overlay.get("categories") or {}
    tool_meta: dict[str, Any] = overlay.get("tools") or {}

    categories: list[dict[str, Any]] = []

    # ── Root-level files → the synthetic "root" category ────────────
    root_files = sorted(p for p in base.iterdir() if p.is_file() and _visible(p))
    if root_files:
        categories.append({
            "key": "root",
            "name": None,
            "description": None,
            "icon": (cat_meta.get("root") or {}).get("icon"),
            "tools": [
                _tool_entry(base, f, url_prefix=url_prefix, meta=tool_meta.get(f.name) or {})
                for f in root_files
            ],
        })

    # ── One category per immediate sub-directory ────────────────────
    for directory in sorted((p for p in base.iterdir() if p.is_dir()), key=lambda p: p.name):
        if not _visible(directory):
            continue
        meta = cat_meta.get(directory.name) or {}
        files = sorted(
            (p for p in directory.rglob("*") if p.is_file() and _visible(p)),
            key=lambda p: p.relative_to(directory).as_posix(),
        )
        categories.append({
            "key": directory.name,
            "name": meta.get("name") or directory.name,
            "description": meta.get("description"),
            "icon": meta.get("icon"),
            "tools": [
                _tool_entry(
                    base,
                    f,
                    url_prefix=url_prefix,
                    meta=tool_meta.get(f.relative_to(base).as_posix()) or {},
                )
                for f in files
            ],
        })

    return {
        "root": str(base),
        "exists": True,
        "url_prefix": url_prefix,
        "categories": categories,
        "tool_count": sum(len(c["tools"]) for c in categories),
    }


# ── npm catalog ──────────────────────────────────────────────────────

def _npm_tarball_entry(path: Path, *, url_prefix: str, meta: dict[str, Any]) -> dict[str, Any]:
    stat = path.stat()
    stem = path.name[:-4] if path.name.endswith(".tgz") else path.stem
    name, _, version = stem.rpartition("-")
    if not name:  # no dash — treat the whole stem as the name
        name, version = stem, ""
    return {
        "name": meta.get("name") or name,
        "version": meta.get("version") or version or "latest",
        "filename": path.name,
        "size": stat.st_size,
        "size_human": human_size(stat.st_size),
        "modified": _iso(stat.st_mtime),
        "download_url": f"{url_prefix.rstrip('/')}/{quote(path.name)}",
        "description": meta.get("description"),
        "tags": list(meta.get("tags") or []),
    }


def scan_npm(root: str, *, upstream: str = "", url_prefix: str = "/npm/files") -> dict[str, Any]:
    """Local npm catalog — explicit entries from ``catalog.json`` plus ``*.tgz``.

    This is a *scaffold*: the server does not implement the npm registry
    protocol yet, so the only thing that can be served is a tarball placed in
    this directory.  ``upstream`` is advertised so the UI can print the
    ``npm config set registry`` line a real proxy will eventually satisfy.
    """
    base = Path(root)
    if not base.is_dir():
        return {
            "root": str(base),
            "exists": False,
            "upstream": upstream,
            "packages": [],
            "package_count": 0,
        }

    overlay = _load_overlay(base)
    listed: dict[str, Any] = {}
    for item in overlay.get("packages") or []:
        if isinstance(item, dict) and item.get("name"):
            listed[str(item["name"])] = item

    packages: list[dict[str, Any]] = []
    seen: set[str] = set()

    for tarball in sorted(base.rglob("*.tgz")):
        if not _visible(tarball):
            continue
        meta = listed.get(tarball.name) or {}
        entry = _npm_tarball_entry(tarball, url_prefix=url_prefix, meta=meta)
        entry["description"] = entry.get("description") or (listed.get(entry["name"]) or {}).get("description")
        packages.append(entry)
        seen.add(entry["name"])

    # Explicit entries with no tarball on disk are still shown, flagged so the
    # UI can mark them "metadata only" instead of pretending they are served.
    for name, item in listed.items():
        if name in seen:
            continue
        packages.append({
            "name": item.get("name") or name,
            "version": item.get("version") or "latest",
            "filename": item.get("filename"),
            "size": None,
            "size_human": "",
            "modified": None,
            "download_url": None,
            "description": item.get("description"),
            "tags": list(item.get("tags") or []),
        })

    return {
        "root": str(base),
        "exists": True,
        "upstream": upstream,
        "packages": packages,
        "package_count": len(packages),
    }


def npm_all_index(catalog: dict[str, Any], *, now: float | None = None) -> dict[str, Any]:
    """Shape a :func:`scan_npm` payload like npm's legacy ``GET /-/all`` document.

    ``/-/all`` used to be *the* way to enumerate a registry: a single JSON object
    keyed by package name, each value carrying ``dist-tags`` and ``versions``.
    npm shut its own copy down in 2017 in favour of ``/-/v1/search`` and the
    replication feed, but practically every private registry (Verdaccio, cnpm,
    …) still answers it, so it is the closest thing npm has to a static index.

    We emit that shape, simplified: one ``latest`` dist-tag per package and a
    ``dist.tarball`` only for entries that actually have a file on disk.
    """
    updated = int(time.time() if now is None else now)
    document: dict[str, Any] = {"_updated": updated}
    for item in catalog.get("packages", []):
        name = item["name"]
        version = item.get("version") or "latest"
        entry: dict[str, Any] = {
            "name": name,
            "description": item.get("description") or "",
            "dist-tags": {"latest": version},
            "versions": {},
        }
        if item.get("modified"):
            entry["time"] = {"modified": item["modified"]}
        if item.get("download_url"):
            entry["versions"][version] = {
                "name": name,
                "version": version,
                "dist": {"tarball": item["download_url"]},
            }
        document[name] = entry
    return document


# ── Model routes ─────────────────────────────────────────────────────

def load_model_routes(path: str) -> dict[str, Any]:
    """Read the model-routing description; a missing file is not an error."""
    file_path = Path(path)
    if not file_path.is_file():
        return {
            "source": str(file_path),
            "exists": False,
            "error": None,
            "routes": [],
        }

    try:
        data = json.loads(file_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("cannot read model routes from %s: %s", file_path, exc)
        return {
            "source": str(file_path),
            "exists": True,
            "error": str(exc),
            "routes": [],
        }

    raw = data.get("routes") if isinstance(data, dict) else data
    routes: list[dict[str, Any]] = []
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            routes.append({
                "name": item.get("name") or item.get("model") or "unnamed",
                "provider": item.get("provider") or "openai-compatible",
                "base_url": item.get("base_url") or item.get("baseUrl") or "",
                "model": item.get("model") or item.get("name") or "",
                "aliases": list(item.get("aliases") or []),
                "path": item.get("path") or "/v1/chat/completions",
                "enabled": item.get("enabled", True) is not False,
                "description": item.get("description"),
                "tags": list(item.get("tags") or []),
            })

    return {
        "source": str(file_path),
        "exists": True,
        "error": None,
        "version": data.get("version") if isinstance(data, dict) else None,
        "routes": routes,
    }


__all__ = ["human_size", "scan_tools", "scan_npm", "npm_all_index", "load_model_routes"]
