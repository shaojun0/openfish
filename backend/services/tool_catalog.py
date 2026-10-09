"""The tools catalog — rows in the database, bytes in the object store.

The tools page used to scan ``TOOLS_DIR`` on every request: the directory was the
catalog, the category was its first path segment, and the display name came from
an optional ``catalog.json`` beside the files.  This module owns the catalog now,
so the two are separated on purpose:

* **the row is the artifact** — its public path (``ops/check.sh``, which is the
  URL tail and what ``cli.py tools export`` writes back), its filename, the
  display name/description/tags the overlay used to hold, its size, digest and
  media type.  :mod:`models.catalog` explains why.
* **the object key is opaque** — a bare ``uuid4``, decided here and read back
  from the row for ever after.  Nothing derives a name from it and nothing
  derives it from a name, which is what lets the medium change without renaming
  anything.

Consequences worth stating, because they are behaviour changes rather than
implementation details:

* **Listing is a query.**  No directory walk, no hashing the tree on every page
  load, and the same code path on a filesystem and on a bucket — so the S3
  backend no longer synthesises directories out of key prefixes.
* **A file in the store that is not in the table is not in the catalog.**  The
  drop-in workflow becomes ``cp`` + ``cli.py tools import`` (or the upload
  endpoint), exactly like documentation; a stray object is invisible to the page
  and to the download route, which is also what makes the catalog's contents
  auditable.
* **Display metadata is rows**, not a file: ``catalog.json`` is now the *import
  format* and what ``export`` writes back, never a second source of truth.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, IO
from urllib.parse import quote

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from models.catalog import (
    TOOLS_NAMESPACE,
    CatalogCategory,
    CatalogEntry,
    category_of,
    overlay_metadata,
)
from models.docs import new_key
from services import namespaces, objectstore
from services.digest import sha256_of
from services.fileio import read_json, write_json
from services.format import human_size
from services.objectstore import ObjectInfo
from services.paths import contained


#: The synthetic category of files that sit directly in the catalog root.
ROOT_CATEGORY = "root"

#: Files that document a tree rather than being artifacts in it — kept in step
#: with the scanner's historical rule (``services.hub._visible``).
DOC_PREFIXES = ("readme", "license", "changelog")

#: The store's own home inside a catalog root (see
#: :data:`services.objectstore.OBJECTS_DIRNAME`).  Reserved: an import that
#: walked into it would catalogue the objects it had just written.
RESERVED_DIRS = (objectstore.OBJECTS_DIRNAME,)

#: The overlay file, imported and exported but never the source of truth.
OVERLAY_FILENAME = "catalog.json"

# ── Queries ──────────────────────────────────────────────────────────

def _entries(session: Session, namespace: str = TOOLS_NAMESPACE) -> list[CatalogEntry]:
    return list(
        session.scalars(
            select(CatalogEntry)
            .where(CatalogEntry.namespace == namespace)
            .order_by(func.lower(CatalogEntry.path))
        )
    )


def _categories(session: Session, namespace: str = TOOLS_NAMESPACE) -> list[CatalogCategory]:
    return list(
        session.scalars(
            select(CatalogCategory).where(CatalogCategory.namespace == namespace)
        )
    )


def find(
    session: Session,
    path: str,
    *,
    namespace: str = TOOLS_NAMESPACE,
) -> CatalogEntry | None:
    """The row for one public path, or ``None``."""
    return session.scalars(
        select(CatalogEntry).where(
            CatalogEntry.namespace == namespace,
            CatalogEntry.path == path.strip("/"),
        )
    ).first()


def require(
    session: Session,
    path: str,
    *,
    namespace: str = TOOLS_NAMESPACE,
) -> CatalogEntry:
    """The row for one public path, or ``FileNotFoundError`` (the routes' 404)."""
    row = find(session, path, namespace=namespace)
    if row is None:
        raise FileNotFoundError(path)
    return row


# ── Payload shapes ───────────────────────────────────────────────────

def _iso(value: datetime | None) -> str | None:
    """An ISO-8601 **UTC** string (SQLite hands a naive value back; see docs)."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def entry_payload(row: CatalogEntry, *, url_prefix: str) -> dict[str, Any]:
    """One tool, in the shape the page, the API and the templates know."""
    return {
        "name": row.display_name or row.filename,
        "filename": row.filename,
        "relative_path": row.path,
        # Quote each segment but keep the separators so nested tools still work.
        "download_url": f"{url_prefix.rstrip('/')}/{quote(row.path)}",
        "size": row.size,
        "size_human": human_size(row.size),
        "sha256": row.sha256,
        "modified": _iso(row.modified_at),
        "description": row.description,
        "tags": row.tag_list(),
    }


def scan(
    session: Session,
    *,
    url_prefix: str = "/tools",
    namespace: str = TOOLS_NAMESPACE,
) -> dict[str, Any]:
    """The whole catalog: categories (including empty ones) and their tools.

    The payload is exactly what the directory scanner returned — the SPA, the
    server-rendered index and the upload response all still read it — with one
    difference underneath: it comes from two queries instead of a walk.
    """
    store = objectstore.catalog_store(namespace, _root_for(namespace))
    entries = _entries(session, namespace)
    rows = {row.slug: row for row in _categories(session, namespace)}

    grouped: dict[str, list[CatalogEntry]] = {}
    for entry in entries:
        grouped.setdefault(category_of(entry.path), []).append(entry)

    # Every described category is shown, even with nothing in it; so is every
    # category an entry implies (import creates both, but a hand-written row
    # should not be able to hide the files under it).
    slugs = sorted(set(grouped) | set(rows), key=lambda slug: (slug != ROOT_CATEGORY, slug))

    categories: list[dict[str, Any]] = []
    for slug in slugs:
        row = rows.get(slug)
        tools = sorted(
            grouped.get(slug, []),
            key=lambda item: item.path[len(slug) + 1:] if slug != ROOT_CATEGORY else item.path,
        )
        if slug == ROOT_CATEGORY:
            # The synthetic category keeps the page's own label, exactly as the
            # scanner did; only its icon is describable.
            categories.append({
                "slug": ROOT_CATEGORY,
                "name": None,
                "description": None,
                "icon": row.icon if row else None,
                "tools": [entry_payload(item, url_prefix=url_prefix) for item in tools],
            })
            continue
        categories.append({
            "slug": slug,
            "name": (row.name if row and row.name else slug),
            "description": row.description if row else None,
            "icon": row.icon if row else None,
            "tools": [entry_payload(item, url_prefix=url_prefix) for item in tools],
        })

    return {
        "root": store.location,
        "exists": bool(entries) or store.exists(""),
        "url_prefix": url_prefix,
        "categories": categories,
        "tool_count": sum(len(category["tools"]) for category in categories),
    }


def _root_for(namespace: str) -> str:
    """The local root a namespace maps to when the backend is a directory."""
    return namespaces.resolve(namespace).root


# ── Write path ───────────────────────────────────────────────────────

def save_entry(
    session: Session,
    *,
    path: str,
    filename: str,
    content_type: str,
    size: int,
    sha256: str | None,
    storage_key: str,
    modified_at: datetime | None = None,
    display_name: str | None = None,
    description: str | None = None,
    tags: Any = (),
    actor: str | None = None,
    namespace: str = TOOLS_NAMESPACE,
) -> CatalogEntry:
    """Insert or update one catalog entry, committing the transaction.

    The object has already been written by the caller (through
    :mod:`services.hub_upload`); this records it.  An update **replaces** the
    row's key, and the superseded object is left for the caller to remove — the
    same "rows first, objects second" order the documentation delete uses.
    """
    path = path.strip("/")
    row = find(session, path, namespace=namespace)
    if row is None:
        row = CatalogEntry(
            namespace=namespace,
            path=path,
            filename=filename,
            storage_key=storage_key,
            created_by=actor,
        )
        session.add(row)
    row.filename = filename
    row.display_name = display_name
    row.description = description
    row.set_tags(tags)
    row.content_type = content_type
    row.size = size
    row.sha256 = sha256
    row.storage_key = storage_key
    row.modified_at = modified_at or datetime.now(tz=timezone.utc)
    row.modified_by = actor
    session.commit()
    return row


def upsert_category(
    session: Session,
    slug: str,
    *,
    name: str | None = None,
    description: str | None = None,
    icon: str | None = None,
    namespace: str = TOOLS_NAMESPACE,
) -> CatalogCategory:
    """Record a category and its display metadata."""
    row = session.scalars(
        select(CatalogCategory).where(
            CatalogCategory.namespace == namespace,
            CatalogCategory.slug == slug,
        )
    ).first()
    if row is None:
        row = CatalogCategory(namespace=namespace, slug=slug)
        session.add(row)
    row.name = name
    row.description = description
    row.icon = icon
    row.modified_at = datetime.now(tz=timezone.utc)
    return row


def delete_entry(
    session: Session,
    path: str,
    *,
    namespace: str = TOOLS_NAMESPACE,
) -> dict[str, Any]:
    """Remove one artifact: its row, then its object."""
    row = require(session, path, namespace=namespace)
    payload = entry_payload(row, url_prefix="/tools")
    key = row.storage_key
    session.delete(row)
    session.commit()
    try:
        objectstore.catalog_store(namespace, _root_for(namespace)).delete(key)
    except OSError:  # pragma: no cover - a store that cannot delete
        pass
    return payload


# ── Import / export: the folder tree as a bridge, not a store ────────

@dataclass(slots=True)
class ToolImportReport:
    """What ``cli.py tools import`` did (or would do, with ``dry_run``)."""

    created: int = 0
    updated: int = 0
    unchanged: int = 0
    skipped: int = 0
    categories: int = 0
    removed: int = 0
    errors: list[str] = field(default_factory=list)

    def lines(self) -> list[str]:
        out = [
            f"新增条目 {self.created} 个",
            f"更新条目 {self.updated} 个",
            f"内容未变（跳过） {self.unchanged} 个",
            f"分类 {self.categories} 个",
        ]
        if self.removed:
            out.append(f"清理已从目录消失的条目 {self.removed} 个")
        if self.errors:
            out.append(f"跳过 {len(self.errors)} 项：")
            out.extend(f"  - {item}" for item in self.errors)
        return out


def _overlay(source: Path) -> dict[str, Any]:
    data = read_json(source / OVERLAY_FILENAME, default={})
    return data if isinstance(data, dict) else {}


def _visible(relative: Path) -> bool:
    """The scanner's historic rule, plus the store's own reserved directory.

    No dotfile, no overlay, no README/licence, and nothing below
    ``objects/`` — that last one is what keeps ``import`` from cataloguing the
    objects it wrote a moment ago.
    """
    if any(part.startswith(".") or part in RESERVED_DIRS for part in relative.parts):
        return False
    name = relative.name
    if name == OVERLAY_FILENAME:
        return False
    return not name.lower().startswith(DOC_PREFIXES)


def import_tree(
    session: Session,
    source: str | Path,
    *,
    actor: str | None = None,
    dry_run: bool = False,
    prune: bool = False,
    namespace: str = TOOLS_NAMESPACE,
) -> ToolImportReport:
    """Read a ``TOOLS_DIR``-shaped tree (and its ``catalog.json``) into the tables.

    Idempotent: an entry whose size and digest are already stored is counted as
    ``unchanged`` and keeps its object, so re-running the import after adding one
    file costs one write, not a rewrite of the catalog.  With *prune*, entries
    that no longer exist in the tree are removed — that is the only way to delete
    through the bridge, and it is off by default because "delete what I did not
    see" is exactly the guess a migration must not make on its own.
    """
    report = ToolImportReport()
    root = Path(source)
    if not root.is_dir():
        raise ValueError(f"目录不存在：{root}")

    overlay = _overlay(root)
    cat_meta: dict[str, Any] = overlay.get("categories") or {}
    tool_meta: dict[str, Any] = overlay.get("tools") or {}
    store = objectstore.catalog_store(namespace, _root_for(namespace))

    # Categories and entries are tracked apart: a category slug is one path
    # segment, so asking `category_of` about it always answers "root" — which is
    # how an empty root category used to appear on every catalog.
    seen_entries: set[str] = set()
    # Every immediate sub-directory is a category, empty or not — the scanner
    # reported an empty directory too, so an empty category stays visible.
    for directory in sorted((p for p in root.iterdir() if p.is_dir()), key=lambda p: p.name):
        if not _visible(Path(directory.name)):
            continue
        meta = cat_meta.get(directory.name) or {}
        if not dry_run:
            upsert_category(
                session,
                directory.name,
                name=meta.get("name"),
                description=meta.get("description"),
                icon=meta.get("icon"),
                namespace=namespace,
            )
        report.categories += 1

    for candidate in sorted(root.rglob("*"), key=lambda p: p.as_posix()):
        if not candidate.is_file():
            continue
        relative = candidate.relative_to(root)
        if not _visible(relative):
            continue
        path = relative.as_posix()
        seen_entries.add(path)
        try:
            _import_entry(
                session, store, root, candidate, path, tool_meta, report,
                actor=actor, dry_run=dry_run, namespace=namespace,
            )
        except (OSError, ValueError) as exc:
            session.rollback()
            report.errors.append(f"{path}: {exc}")

    # The synthetic root category exists only when something actually sits in
    # the root; an empty one would put a permanent "uncategorized" heading on a
    # catalog that has no uncategorized artifacts.
    if any("/" not in path for path in seen_entries) and not dry_run:
        upsert_category(session, ROOT_CATEGORY, namespace=namespace)
    if not dry_run:
        session.commit()

    if prune:
        for row in _entries(session, namespace):
            if row.path in seen_entries:
                continue
            report.removed += 1
            if not dry_run:
                delete_entry(session, row.path, namespace=namespace)

    return report


def _import_entry(
    session: Session,
    store: objectstore.ObjectStore,
    root: Path,
    candidate: Path,
    path: str,
    tool_meta: dict[str, Any],
    report: ToolImportReport,
    *,
    actor: str | None,
    dry_run: bool,
    namespace: str,
) -> None:
    data = candidate.read_bytes()
    digest = sha256_of([data])
    meta = overlay_metadata(
        tool_meta.get(path) or tool_meta.get(candidate.name) or {}
    )
    content_type = _guess_type(candidate.name)
    modified = datetime.fromtimestamp(candidate.stat().st_mtime, tz=timezone.utc)

    row = find(session, path, namespace=namespace)
    unchanged = (
        row is not None
        and row.sha256 == digest
        and row.size == len(data)
        and row.filename == candidate.name
        and row.display_name == meta["display_name"]
        and row.description == meta["description"]
        and row.tag_list() == list(meta["tags"])
    )
    if unchanged:
        report.unchanged += 1
        return
    if dry_run:
        if row is None:
            report.created += 1
        else:
            report.updated += 1
        return

    key = new_key()
    store.put(key, (data,), content_type=content_type)
    superseded = row.storage_key if row is not None else None
    save_entry(
        session,
        path=path,
        filename=candidate.name,
        content_type=content_type,
        size=len(data),
        sha256=digest,
        storage_key=key,
        modified_at=modified,
        display_name=meta["display_name"],
        description=meta["description"],
        tags=meta["tags"],
        actor=actor,
        namespace=namespace,
    )
    if superseded and superseded != key:
        try:
            store.delete(superseded)
        except OSError:  # pragma: no cover
            pass
    if row is None:
        report.created += 1
    else:
        report.updated += 1


def export_tree(
    session: Session,
    target: str | Path,
    *,
    namespace: str = TOOLS_NAMESPACE,
) -> int:
    """Write the catalog back out as a folder tree plus its ``catalog.json``.

    The portability half of :func:`import_tree`: what an operator diffs, backups
    and carries into a disconnected deployment.  The target is a directory the
    caller names — deliberately not the store root, so an export can never
    collide with the objects the database owns.
    """
    root = Path(target)
    store = objectstore.catalog_store(namespace, _root_for(namespace))
    entries = _entries(session, namespace)
    categories = _categories(session, namespace)

    overlay_categories: dict[str, Any] = {}
    for row in categories:
        if row.slug == ROOT_CATEGORY or not (row.name or row.description or row.icon):
            continue
        item: dict[str, Any] = {}
        if row.name:
            item["name"] = row.name
        if row.description:
            item["description"] = row.description
        if row.icon:
            item["icon"] = row.icon
        overlay_categories[row.slug] = item

    overlay_tools: dict[str, Any] = {}
    for row in entries:
        item = {}
        if row.display_name:
            item["name"] = row.display_name
        if row.description:
            item["description"] = row.description
        if row.tag_list():
            item["tags"] = row.tag_list()
        if item:
            overlay_tools[row.path] = item

    for row in entries:
        # `contained` proves the row's public path stays under the export target:
        # the path is data, and an imported or hand-edited row is not trusted.
        destination = contained(root, *row.path.split("/"))
        with store.open(row.storage_key) as handle:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(handle.read())

    if overlay_categories or overlay_tools:
        write_json(root / OVERLAY_FILENAME, {
            "categories": overlay_categories,
            "tools": overlay_tools,
        })
    return len(entries)


def _guess_type(name: str) -> str:
    import mimetypes

    return mimetypes.guess_type(name)[0] or "application/octet-stream"


def open_entry(
    session: Session,
    path: str,
    *,
    namespace: str = TOOLS_NAMESPACE,
) -> tuple[ObjectInfo, IO[bytes], CatalogEntry]:
    """One artifact ready to stream: ``(object info, stream, row)``."""
    row = require(session, path, namespace=namespace)
    store = objectstore.catalog_store(namespace, _root_for(namespace))
    info = store.stat(row.storage_key)
    if info is None:
        raise FileNotFoundError(row.storage_key)
    return info, store.open(row.storage_key), row


__all__ = [
    "OVERLAY_FILENAME",
    "ROOT_CATEGORY",
    "TOOLS_NAMESPACE",
    "ToolImportReport",
    "delete_entry",
    "entry_payload",
    "export_tree",
    "find",
    "import_tree",
    "open_entry",
    "require",
    "save_entry",
    "scan",
    "upsert_category",
]
