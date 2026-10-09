"""The mirror catalogs' metadata overlay, as rows rather than a ``catalog.json``.

npm / debian / docker-images were "the directory is the catalog, plus an optional
``catalog.json`` beside the files".  The directory *still* is the catalog — a
tarball's name is the package, a ``.deb``'s name is the package, and those files
travel between networks by rsync — but the metadata is a table now:
``catalog_entries`` rows in the namespace ``npm`` / ``debian`` /
``docker-images``, sharing the table the tools catalog uses.

Three layers, kept apart on purpose:

* **the file is the artifact** — it stays where the operator put it, and it is
  what a client downloads.  A mirror row never owns bytes, so its
  ``storage_key`` is :data:`models.catalog.NO_STORAGE_KEY`.
* **the row is the description** — display name, version, architecture, kind,
  tags.  Losing the rows loses the metadata, never the artifact.
* **the directory root is a port** — where the rows live is
  :attr:`services.namespaces.Namespace.root`; where the *bytes* live is
  :func:`services.objectstore.catalog_store`'s answer, which can be a bucket.

Rows and files are merged by the scanner:

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
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from models.catalog import CatalogEntry, NO_FILE
from services import namespaces
from services.fileio import read_json, write_json
from services.hub import Overlay, OverlayItem

#: The mirror namespaces whose metadata lives here, in the order the CLI lists.
MIRRORS: tuple[str, ...] = namespaces.MIRRORS


def top_key(namespace: str) -> str:
    """The overlay key each namespace's file uses.  npm's file says ``packages``;
    the flat mirrors (debian, docker-images) say ``artifacts``."""
    return namespaces.overlay_key(namespace)



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


def overlay(session: Session, namespace: str) -> Overlay:
    """The rows of *namespace* in the shape :mod:`services.hub` merges.

    The scanners take this dict instead of reading a file, so the file is no
    longer a source of truth for anything the request path serves.
    """
    check_namespace(namespace)
    items: list[OverlayItem] = []
    for row in entries(session, namespace):
        item: OverlayItem = {
            "filename": row.filename,
            "name": row.display_name,
            "version": row.version,
            "description": row.description,
            "tags": row.tag_list(),
        }
        if namespace != namespaces.NPM:
            item["arch"] = row.arch
            item["kind"] = row.kind
        items.append({key: value for key, value in item.items() if value not in (None, NO_FILE)})
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
    check_namespace(namespace)
    return Path(namespaces.root_for(namespace)) / namespaces.OVERLAY_FILENAME


def path_for(namespace: str, item: OverlayItem) -> str:
    """The row identity of one overlay item.

    ``filename`` when the entry names a file — that is what the file scan
    matches on.  Otherwise npm identifies a package by name, and a flat mirror
    by ``name`` + ``version`` (+ ``arch`` for debian, where the same version
    exists per architecture).
    """
    filename = str(item.get("filename") or NO_FILE).strip()
    if filename:
        return filename
    name = str(item.get("name") or "").strip()
    if namespace == namespaces.NPM:
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
        fields = entry_fields(namespace, item)
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
    items: list[OverlayItem] = []
    for row in entries(session, namespace):
        item: OverlayItem = {
            "filename": row.filename,
            "name": row.display_name,
            "version": row.version,
            "description": row.description,
            "tags": row.tag_list(),
        }
        if namespace != namespaces.NPM:
            item["arch"] = row.arch
            item["kind"] = row.kind
        items.append({key: value for key, value in item.items() if value not in (None, NO_FILE, [])})
    payload: Overlay = {top_key(namespace): items}
    write_json(target, payload)
    return OverlayReport(namespace=namespace, path=str(target), exported=len(items))


def _read_overlay(path: Path) -> dict:
    """Read a ``catalog.json``; a missing or malformed one degrades to empty.

    Through :mod:`services.fileio` like every other JSON document this server
    owns, so "a bad file must not 500 the page that reads it" has one
    implementation rather than one per reader.
    """
    payload = read_json(path, default=None)
    return payload if isinstance(payload, dict) else {}


def entry_fields(namespace: str, item: OverlayItem) -> dict[str, Any]:
    """The mutable columns one overlay item sets.

    Public because :mod:`config.seed`'s generator builds the same columns when it
    turns an overlay file into ``.sql``: the mapping from a JSON key to a column
    must not exist twice.
    """
    fields = {
        "filename": str(item.get("filename") or NO_FILE),
        "display_name": str(item.get("name") or "") or None,
        "description": str(item.get("description") or "") or None,
        "version": str(item.get("version") or "") or None,
        "tags": "[]",
    }
    if namespace != namespaces.NPM:
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
    "entry_fields",
    "export_file",
    "import_file",
    "overlay",
    "path_for",
    "top_key",
]
