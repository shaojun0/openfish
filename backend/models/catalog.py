"""Artifact-catalog tables — the *index* of a catalog that lives in the store.

A catalog used to *be* a directory tree: the tools page scanned ``TOOLS_DIR`` on
every request, the category was the first path segment, the display name came
from ``catalog_entries`` rows (the interface is ``services.mirror_catalog``), and the URL
was the file's path.  The tree was the truth, which meant listing was an
``os.walk`` with a SHA-256 of every small file, a bucket had to be walked
recursively, and the overlay was one more file to keep on a writable disk even
when the payload had moved to object storage.

So the index moves here and the bytes stay in the store:

``catalog_categories``
    One row per category — the display metadata a catalog used to keep in
    ``catalog.json`` under ``categories`` (name, description, icon).  A category
    with no entries is a real row, because the page shows it (the old scanner
    reported an empty directory for the same reason).
``catalog_entries``
    One row per artifact: its **public path** (``ops/check.sh`` — the URL tail
    and the operator's mental address), the filename, the display name and
    description and tags the overlay used to carry, the size, the digest, the
    media type, and the opaque storage key.

Two things this buys, both of which the file-based version could not have:

* **Listing is a query.**  No directory walk, no re-hashing on every page load,
  and the same code path on a filesystem and on a bucket — which is why the S3
  backend no longer has to synthesise directories out of common key prefixes.
* **The name is data, not a path.**  ``path`` is what the URL shows and what
  ``tools import/export`` round-trips; ``storage_key`` is a bare ``uuid4`` with
  no extension and no meaning (see :mod:`models.docs`).  Renaming a category, or
  moving a catalog to a bucket, changes rows — never object keys.

``namespace`` scopes both tables (``tools`` today).  It exists because the key
*is* the namespace-independent part of the design and because a second
file-backed catalog (``docker-images``, ``debian``) can adopt the same tables
without a migration; only ``tools`` is wired to them so far.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Mapping

from sqlalchemy import (
    Column,
    DateTime,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped

from .base import Base, utcnow
from .docs import KEY_MAX

#: Longest public path below a catalog root (``<category>/<sub>/<file>``).
PATH_MAX = 512

#: Longest category slug (one path segment).
CATEGORY_MAX = 128

#: Longest display name or filename.
NAME_MAX = 160

#: Longest description.
DESCRIPTION_MAX = 512

#: Longest media type.
CONTENT_TYPE_MAX = 255

#: The catalog this project wires to these tables today.
TOOLS_NAMESPACE = "tools"

#: ``catalog_entries.filename`` sentinel: this row names no file.
#:
#: A mirror row may register only metadata (npm calls those "metadata only"): the
#: file would be the artifact and it is not here.  The empty string is the
#: sentinel rather than ``NULL`` because the column is ``NOT NULL`` in every
#: deployment and SQLite cannot drop ``NOT NULL`` — switching to ``NULL`` would
#: cost an existing database a table rebuild to say the same thing.
NO_FILE = ""

#: ``catalog_entries.storage_key`` sentinel: this row owns no bytes.
#:
#: A mirror's file *is* the artifact and stays where the operator put it, so the
#: row only describes it.  Empty rather than ``NULL`` for the same reason as
#: :data:`NO_FILE`: the column is ``NOT NULL`` in every deployment and SQLite
#: cannot drop it.
NO_STORAGE_KEY = ""


class CatalogCategory(Base):
    """One category of one catalog, with the display metadata it carries."""

    __tablename__ = "catalog_categories"
    __table_args__ = (
        UniqueConstraint("namespace", "slug", name="uq_catalog_categories_slug"),
    )

    id: Mapped[int] = Column(Integer, primary_key=True, autoincrement=True)
    namespace: Mapped[str] = Column(String(32), nullable=False, index=True)
    #: One path segment; ``root`` is the synthetic category of files that sit
    #: directly in the catalog root (the UI's "uncategorized").
    slug: Mapped[str] = Column(String(CATEGORY_MAX), nullable=False)
    #: ``NULL`` for the synthetic root category, which the page labels itself.
    name: Mapped[str | None] = Column(String(NAME_MAX), nullable=True)
    description: Mapped[str | None] = Column(String(DESCRIPTION_MAX), nullable=True)
    icon: Mapped[str | None] = Column(String(64), nullable=True)
    created_at: Mapped[datetime] = Column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    modified_at: Mapped[datetime] = Column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )


class CatalogEntry(Base):
    """One artifact of one catalog: its address, its metadata and its object."""

    __tablename__ = "catalog_entries"
    __table_args__ = (
        UniqueConstraint("namespace", "path", name="uq_catalog_entries_path"),
    )

    id: Mapped[int] = Column(Integer, primary_key=True, autoincrement=True)
    namespace: Mapped[str] = Column(String(32), nullable=False, index=True)
    #: Catalog-relative path (``ops/check.sh``) — the URL tail, and the only
    #: address this row has.  Never a storage key.
    path: Mapped[str] = Column(String(PATH_MAX), nullable=False)
    #: Last path segment; stored rather than split on every render, because it
    #: is what the page and the download show.  :data:`NO_FILE` on a mirror row
    #: that registers metadata without naming a file — see that constant for why
    #: the sentinel is the empty string and not ``NULL``.
    filename: Mapped[str] = Column(String(NAME_MAX), nullable=False, default=NO_FILE)
    #: Overlay version / architecture / kind of a mirror row (``1.0.0`` /
    #: ``arm64`` / ``deb``).  Unused by the tools catalog, where the filename
    #: carries everything the listing shows.
    version: Mapped[str | None] = Column(String(64), nullable=True)
    arch: Mapped[str | None] = Column(String(32), nullable=True)
    kind: Mapped[str | None] = Column(String(32), nullable=True)
    #: Overlay display name; ``NULL`` means "show the filename".
    display_name: Mapped[str | None] = Column(String(NAME_MAX), nullable=True)
    description: Mapped[str | None] = Column(String(DESCRIPTION_MAX), nullable=True)
    #: JSON array of tags — TEXT rather than SQLAlchemy's JSON type so the same
    #: DDL works on SQLite and PostgreSQL (the trade ``models.model_route``
    #: makes for ``aliases``).
    tags: Mapped[str] = Column(Text, nullable=False, default="[]")
    #: Media type recorded on the object store and served with the download.
    content_type: Mapped[str] = Column(
        String(CONTENT_TYPE_MAX), nullable=False, default="application/octet-stream"
    )
    size: Mapped[int] = Column(Integer, nullable=False, default=0)
    sha256: Mapped[str | None] = Column(String(64), nullable=True)
    #: The opaque object key — a bare uuid4.
    #: The object this row owns.  :data:`NO_STORAGE_KEY` for a mirror row: npm /
    #: debian / docker-images artifacts stay where the operator put them and the
    #: scanner reads the directory, so a mirror row only *describes* a file and
    #: never names an object.  See that constant for why it is not ``NULL``.
    storage_key: Mapped[str] = Column(
        String(KEY_MAX), nullable=False, default=NO_STORAGE_KEY
    )
    created_at: Mapped[datetime] = Column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    #: The artifact's own timestamp: an import carries the source file's mtime,
    #: an upload records when it landed, and the page shows it as ``modified``.
    modified_at: Mapped[datetime] = Column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    created_by: Mapped[str | None] = Column(String(256), nullable=True)
    modified_by: Mapped[str | None] = Column(String(256), nullable=True)

    # ── JSON accessors ───────────────────────────────────────────────

    def tag_list(self) -> list[str]:
        """The tags as a list; a corrupt value degrades to ``[]``."""
        try:
            parsed = json.loads(self.tags or "[]")
        except ValueError:
            return []
        return [str(item) for item in parsed] if isinstance(parsed, list) else []

    def set_tags(self, values: Any) -> None:
        """Store *values* as a JSON array of strings."""
        items = list(values or [])
        self.tags = json.dumps([str(item) for item in items], ensure_ascii=False)


class CatalogSeedState(Base):
    """One row per catalog that has had its first-run defaults installed.

    The seed itself is a checked-in ``.sql`` (see ``config/seed/``) plus the
    objects it names; this table is what makes installing it a **one-shot**
    action.  Without it, "the catalog is empty, so seed it" would resurrect the
    defaults every time an administrator deleted them all — the one way a boot
    step like this can destroy an operator's decision rather than a blank page.
    """

    __tablename__ = "catalog_seed_state"

    namespace: Mapped[str] = Column(String(32), primary_key=True)
    seeded_at: Mapped[datetime] = Column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )


def category_of(path: str) -> str:
    """The category a catalog path belongs to (``root`` when it has none)."""
    head, sep, _ = path.partition("/")
    return head if sep else "root"


def overlay_metadata(entry: Mapping[str, Any]) -> dict[str, Any]:
    """The overlay fields of one ``catalog.json`` tools entry, normalised.

    The file's shape is what operators already have on disk, so it stays the
    import format; this is the one place that knows how its keys map onto the
    columns.
    """
    return {
        "display_name": (str(entry.get("name")).strip() if entry.get("name") else None),
        "description": (str(entry.get("description")).strip() if entry.get("description") else None),
        "tags": [str(item) for item in (entry.get("tags") or [])],
    }


__all__ = [
    "CATEGORY_MAX",
    "CONTENT_TYPE_MAX",
    "DESCRIPTION_MAX",
    "NAME_MAX",
    "NO_FILE",
    "NO_STORAGE_KEY",
    "PATH_MAX",
    "TOOLS_NAMESPACE",
    "CatalogCategory",
    "CatalogEntry",
    "CatalogSeedState",
    "category_of",
    "overlay_metadata",
]
