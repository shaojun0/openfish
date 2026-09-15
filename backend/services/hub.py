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

The model-route document is the one catalog an administrator edits in the
browser rather than on disk; :mod:`services.model_routes` owns it.
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
#: grows past this many entries, which is more than any catalog directory holds.
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
    empty category is visible instead of silently missing.
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

    This is the *catalog* view: what is physically present in ``NPM_DIR``.  The
    registry protocol itself — packuments, manifests, tarballs, search — is
    served by :mod:`routes.npm` on top of :mod:`services.npm_registry`, which
    consults this directory first and falls back to ``upstream``.  ``upstream``
    is carried here so the catalog page can print the
    ``npm config set registry`` line that actually works.
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


# ── Docker / Debian flat catalogs ────────────────────────────────────

def _flat_entry(
    base: Path,
    path: Path,
    *,
    url_prefix: str,
    info: dict[str, Any],
    meta: dict[str, Any],
) -> dict[str, Any]:
    """One on-disk artifact, with the parsed filename metadata and overlay."""
    stat = path.stat()
    rel = path.relative_to(base).as_posix()
    return {
        "name": meta.get("name") or info.get("name") or path.name,
        "version": meta.get("version") or info.get("version") or "",
        "arch": meta.get("arch") or info.get("arch") or "",
        "kind": meta.get("kind") or info.get("kind") or "file",
        "filename": path.name,
        "size": stat.st_size,
        "size_human": human_size(stat.st_size),
        "sha256": _sha256(path, stat),
        "modified": _iso(stat.st_mtime),
        "download_url": f"{url_prefix.rstrip('/')}/{quote(rel)}",
        "description": meta.get("description"),
        "tags": list(meta.get("tags") or []),
    }


def _flat_metadata_entry(item: dict[str, Any]) -> dict[str, Any]:
    """A catalog.json entry with no file behind it (nothing to download)."""
    return {
        "name": item.get("name") or item.get("filename") or "unnamed",
        "version": item.get("version") or "",
        "arch": item.get("arch") or "",
        "kind": item.get("kind") or "file",
        "filename": item.get("filename"),
        "size": None,
        "size_human": "",
        "sha256": None,
        "modified": None,
        "download_url": None,
        "description": item.get("description"),
        "tags": list(item.get("tags") or []),
    }


def scan_flat(
    root: str,
    *,
    url_prefix: str,
    parse: Any,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Catalog a flat directory of artifacts (docker images, .deb files, …).

    ``parse(filename)`` derives the display metadata from the filename; an
    optional ``catalog.json`` under *root* overrides it per entry:

    .. code-block:: json

        {"artifacts": [
          {"filename": "nginx-1.25.3.tar", "name": "nginx", "tags": ["web"]},
          {"name": "redis", "version": "7.2.4"}
        ]}

    An entry without a ``filename`` (or whose file is missing) is reported with
    ``download_url: null`` — the UI marks those *metadata only* rather than
    pretending they are served.
    """
    base = Path(root)
    result: dict[str, Any] = {
        "root": str(base),
        "exists": base.is_dir(),
        "url_prefix": url_prefix,
        "artifacts": [],
        "artifact_count": 0,
    }
    result.update(extra or {})
    if not base.is_dir():
        return result

    overlay = _load_overlay(base)
    by_filename: dict[str, dict[str, Any]] = {}
    metadata_only: list[dict[str, Any]] = []
    for item in overlay.get("artifacts") or []:
        if not isinstance(item, dict):
            continue
        filename = item.get("filename")
        if filename:
            by_filename[str(filename)] = item
        else:
            metadata_only.append(item)

    artifacts: list[dict[str, Any]] = []
    matched: set[str] = set()
    for path in sorted(p for p in base.iterdir() if p.is_file() and _visible(p)):
        meta = by_filename.get(path.name) or {}
        artifacts.append(_flat_entry(
            base, path, url_prefix=url_prefix, info=parse(path.name), meta=meta,
        ))
        matched.add(path.name)

    # Entries that name a file which is not on disk, then pure metadata entries.
    for filename, item in by_filename.items():
        if filename not in matched:
            artifacts.append(_flat_metadata_entry(item))
    for item in metadata_only:
        artifacts.append(_flat_metadata_entry(item))

    result["artifacts"] = artifacts
    result["artifact_count"] = len(artifacts)
    return result


def _parse_docker_filename(filename: str) -> dict[str, Any]:
    """``nginx-1.25.3.tar`` → image ``nginx:1.25.3``; describe the rest as files."""
    lower = filename.lower()
    for ext in (".tar.gz", ".tgz", ".tar"):
        if lower.endswith(ext):
            stem = filename[: -len(ext)]
            name, _, tag = stem.rpartition("-")
            if not name:
                name, tag = stem, ""
            return {"name": name, "version": tag, "kind": "image"}
    if lower.endswith((".yml", ".yaml")):
        return {"name": filename, "version": "", "kind": "compose"}
    if lower.startswith("dockerfile"):
        return {"name": filename, "version": "", "kind": "dockerfile"}
    return {"name": filename, "version": "", "kind": "file"}


def _parse_debian_filename(filename: str) -> dict[str, Any]:
    """``curl_8.5.0_arm64.deb`` → package ``curl`` 8.5.0 (arm64)."""
    if filename.lower().endswith(".deb"):
        parts = filename[:-4].split("_")
        if len(parts) >= 3:
            return {
                "name": parts[0],
                "version": "_".join(parts[1:-1]),
                "arch": parts[-1],
                "kind": "deb",
            }
        if len(parts) == 2:
            return {"name": parts[0], "version": parts[1], "arch": "", "kind": "deb"}
        return {"name": filename[:-4], "version": "", "arch": "", "kind": "deb"}
    if filename.lower().endswith((".list", ".sources", ".example", ".gpg", ".key")):
        return {"name": filename, "version": "", "kind": "config"}
    return {"name": filename, "version": "", "kind": "file"}


def scan_docker(root: str, *, url_prefix: str, registry: str = "") -> dict[str, Any]:
    """Docker catalog — image tarballs plus compose/Dockerfile snippets."""
    return scan_flat(
        root,
        url_prefix=url_prefix,
        parse=_parse_docker_filename,
        extra={"registry": registry},
    )


def scan_debian(root: str, *, url_prefix: str, mirror: str = "") -> dict[str, Any]:
    """Debian catalog — local ``.deb`` files plus apt config snippets."""
    return scan_flat(
        root,
        url_prefix=url_prefix,
        parse=_parse_debian_filename,
        extra={"mirror": mirror},
    )


def docker_registry_catalog(catalog: dict[str, Any]) -> dict[str, Any]:
    """Shape a :func:`scan_docker` payload like the Registry v2 ``/v2/_catalog``.

    The OCI distribution spec defines ``GET /v2/_catalog`` → ``{"repositories":
    [...]}``; it is the only enumeration endpoint the Docker registry protocol
    has, which makes it the closest analogue of npm's ``/-/all``.
    """
    repositories = sorted({
        item["name"] for item in catalog.get("artifacts", [])
        if item.get("kind") == "image"
    })
    return {"repositories": repositories}


def debian_packages_index(catalog: dict[str, Any]) -> str:
    """Render a flat apt ``Packages`` index from the ``.deb`` entries.

    This is Debian's static index element: a flat repository is exactly a
    directory of ``.deb`` files next to a ``Packages`` file that lists them.
    Only entries with a file on disk are emitted — apt would fail on a
    ``Filename:`` that does not resolve.
    """
    stanzas: list[str] = []
    for item in catalog.get("artifacts", []):
        if item.get("kind") != "deb" or not item.get("download_url"):
            continue
        lines = [
            f"Package: {item['name']}",
            f"Version: {item.get('version') or '0'}",
            f"Architecture: {item.get('arch') or 'all'}",
            # Relative to the repo base (`/debian/`), which is where apt resolves
            # it from — the files are served under `/debian/files/`.
            f"Filename: files/{item['filename']}",
            f"Size: {item.get('size') or 0}",
        ]
        if item.get("sha256"):
            lines.append(f"SHA256: {item['sha256']}")
        lines.append(f"Description: {item.get('description') or item['name']}")
        stanzas.append("\n".join(lines))
    return "\n\n".join(stanzas) + ("\n" if stanzas else "")


# ── Model routes ─────────────────────────────────────────────────────
# The route table is no longer read-only: an administrator edits it through the
# routing panel, so its whole lifecycle — read, validate, write and probe —
# lives in :mod:`services.model_routes` rather than being split across two
# modules.  This module keeps only the artifact catalogs.

__all__ = [
    "human_size",
    "scan_tools",
    "scan_npm",
    "npm_all_index",
    "scan_docker",
    "scan_debian",
    "docker_registry_catalog",
    "debian_packages_index",
]
