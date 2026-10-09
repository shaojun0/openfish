"""The mirror catalogs' metadata overlay, as rows rather than a ``catalog.json``.

npm / debian / docker-images were "the directory is the catalog, plus an optional
``catalog.json`` beside the files".  The directory *still* is the catalog — a
tarball's name is the package, a ``.deb``'s name is the package, and those files
travel between networks by rsync — but the metadata is a table now:
``catalog_entries`` rows in the namespace ``npm`` / ``debian`` /
``docker-images``, sharing the table the tools catalog uses.

A mirror row never owns bytes: the file *is* the bytes and stays where it is, so
``storage_key`` is the empty string here (unlike a tools row, where it names the
object the row owns).  Rows and files are merged by the scanner:

* a row that names a file (``filename``) overrides that file's display metadata;
* a row with no file is a **metadata-only** entry — what the UI shows as
  registered-but-not-served;
* a file with no row is still listed, described by its filename.

``catalog.json`` is the import/export format (``cli.py catalogs import|export``),
exactly like the tools overlay, and the shipped defaults install through
:mod:`services.catalog_seed` with the rest of the first-run content.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from config import settings
from models.catalog import CatalogEntry

#: The mirror namespaces whose metadata lives here, in the order the CLI lists.
MIRRORS: tuple[str, ...] = ("npm", "debian", "docker-images")

#: The overlay key each namespace's file uses.  npm's file says ``packages``;
#: the flat mirrors (debian, docker-images) say ``artifacts``.
def top_key(namespace: str) -> str:
    return "packages" if namespace == "npm" else "artifacts"


@dataclass(frozen=True, slots=True)
class OverlayReport:
    """What one import or export did."""

    namespace: str
    path: str
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    pruned: int = 0
    exported: int = 0

    def lines(self) -> list[str]:
        if self.exported:
            return [f"{self.namespace}: 导出 {self.exported} 条 -> {self.path}"]
        parts = [f"新增 {self.created}", f"更新 {self.updated}", f"未变 {self.unchanged}"]
        if self.pruned:
            parts.append(f"清理 {self.pruned}")
        return [f"{self.namespace}: " + "，".join(parts) + f"（{self.path}）"]


def check_namespace(namespace: str) -> str:
    """Refuse a namespace this module does not own.

    A typo (``docker`` for ``docker-images``) would otherwise create a namespace
    nobody reads: rows appear, the page stays empty, and nothing says why.
    """
    if namespace not in MIRRORS:
        raise ValueError(
            f"unknown mirror namespace {namespace!r} (expected one of: {', '.join(MIRRORS)})"
        )
    return namespace


def overlay(session: Session, namespace: str) -> dict:
    """The rows of *namespace* in the shape :mod:`services.hub` merges.

    The scanners take this dict instead of reading a file, so the file is no
    longer a source of truth for anything the request path serves.
    """
    check_namespace(namespace)
    items = []
    for row in entries(session, namespace):
        item = {
            "filename": row.filename,
            "name": row.display_name,
            "version": row.version,
            "description": row.description,
            "tags": row.tag_list(),
        }
        if namespace != "npm":
            item["arch"] = row.arch
            item["kind"] = row.kind
        items.append({key: value for key, value in item.items() if value not in (None, "")})
    return {top_key(namespace): items}


def entries(session: Session, namespace: str) -> list[CatalogEntry]:
    """Every overlay row of *namespace*, in the order they were written.

    ``id`` rather than ``path``: the overlay file is an ordered list an operator
    curated, and the seed / import insert in that order, so insertion order is
    the author's.  Sorting by path would quietly reshuffle the catalog page.
    """
    return list(
        session.scalars(
            select(CatalogEntry)
            .where(CatalogEntry.namespace == namespace)
            .order_by(CatalogEntry.id)
        )
    )


def default_path(namespace: str) -> Path:
    """Where the overlay file for *namespace* lives in a deployment."""
    roots = {
        "npm": settings.hub.npm_dir,
        "debian": settings.hub.debian_dir,
        "docker-images": settings.hub.docker_dir,
    }
    return Path(roots[namespace]) / "catalog.json"


def path_for(namespace: str, item: dict) -> str:
    """The row identity of one overlay item.

    ``filename`` when the entry names a file — that is what the file scan
    matches on.  Otherwise npm identifies a package by name, and a flat mirror
    by ``name`` + ``version`` (+ ``arch`` for debian, where the same version
    exists per architecture).
    """
    filename = str(item.get("filename") or "").strip()
    if filename:
        return filename
    name = str(item.get("name") or "").strip()
    if namespace == "npm":
        return name or "unnamed"
    version = str(item.get("version") or "").strip()
    arch = str(item.get("arch") or "").strip()
    if arch:
        return f"{name}_{version}_{arch}" if version else f"{name}_{arch}"
    return f"{name}:{version}" if version else (name or "unnamed")


def import_file(
    session: Session,
    namespace: str,
    path: str | Path,
    *,
    prune: bool = False,
    dry_run: bool = False,
) -> OverlayReport:
    """Read a ``catalog.json`` into rows; idempotent.

    *prune* deletes rows this file does not name — the overlay file is the whole
    truth of a mirror's metadata, so a deletion in it has to be a deletion here.
    """
    check_namespace(namespace)
    source = Path(path)
    payload = _read_overlay(source)
    items = [item for item in payload.get(top_key(namespace)) or [] if isinstance(item, dict)]

    existing = {row.path: row for row in entries(session, namespace)}
    report = {"created": 0, "updated": 0, "unchanged": 0}
    seen: set[str] = set()
    for item in items:
        key = path_for(namespace, item)
        if not key or key == "unnamed":
            continue
        seen.add(key)
        fields = _fields(namespace, item)
        row = existing.get(key)
        if row is None:
            session.add(CatalogEntry(namespace=namespace, path=key, **fields))
            report["created"] += 1
            if dry_run:
                session.rollback()
                continue
            continue
        if all(getattr(row, name) == value for name, value in fields.items()):
            report["unchanged"] += 1
            continue
        for name, value in fields.items():
            setattr(row, name, value)
        report["updated"] += 1

    pruned = 0
    if prune:
        for key, row in existing.items():
            if key in seen:
                continue
            pruned += 1
            if not dry_run:
                session.delete(row)

    if not dry_run:
        session.commit()
    return OverlayReport(
        namespace=namespace,
        path=str(source),
        created=report["created"],
        updated=report["updated"],
        unchanged=report["unchanged"],
        pruned=pruned,
    )


def export_file(session: Session, namespace: str, path: str | Path) -> OverlayReport:
    """Write the rows back out as a ``catalog.json`` (the format, not a truth)."""
    check_namespace(namespace)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    items = []
    for row in entries(session, namespace):
        item = {
            "filename": row.filename,
            "name": row.display_name,
            "version": row.version,
            "description": row.description,
            "tags": row.tag_list(),
        }
        if namespace != "npm":
            item["arch"] = row.arch
            item["kind"] = row.kind
        items.append({key: value for key, value in item.items() if value not in (None, "", [])})
    payload = {top_key(namespace): items}
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return OverlayReport(namespace=namespace, path=str(target), exported=len(items))


def _read_overlay(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _fields(namespace: str, item: dict) -> dict:
    """The mutable columns one overlay item sets."""
    fields = {
        # "" is the sentinel for "no file"; see CatalogEntry.filename.
        "filename": str(item.get("filename") or ""),
        "display_name": str(item.get("name") or "") or None,
        "description": str(item.get("description") or "") or None,
        "version": str(item.get("version") or "") or None,
        "tags": "[]",
    }
    if namespace != "npm":
        fields["arch"] = str(item.get("arch") or "") or None
        fields["kind"] = str(item.get("kind") or "") or None
    tags = item.get("tags")
    if isinstance(tags, list) and tags:
        fields["tags"] = json.dumps([str(tag) for tag in tags], ensure_ascii=False)
    return fields


__all__ = [
    "MIRRORS",
    "OverlayReport",
    "check_namespace",
    "default_path",
    "entries",
    "export_file",
    "import_file",
    "overlay",
    "path_for",
    "top_key",
]
