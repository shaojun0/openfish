"""Documentation tables — identity, revisions and assets for ``/docs``.

A documentation document used to *be* a directory: ``DOCS_DIR/<ecosystem>/<id>/
document.md`` plus a ``meta.json`` and an ``assets/`` folder, with the folder
name as the only identity it had.  That made three things impossible and one
thing dangerous:

* **Impossible** — knowing who changed what, undoing a bad edit, and detecting
  two editors who saved from the same starting point (the second save silently
  won).  A folder has no revision to compare against.
* **Dangerous** — the folder lived in the operator's work tree, so a browser
  save dirtied ``git status`` and a checkout could take the only copy.

So the metadata moves here and the bytes move behind
:mod:`services.objectstore`:

``documents``
    One row per document.  ``ecosystem`` + ``slug`` is the URL address
    (``/docs/python/getting-started``) and stays human-readable; ``id`` is the
    stable identity the object keys and the revision history hang off, so
    **renaming a document no longer moves its history**.  ``revision`` is the
    optimistic-concurrency token: a save that names the revision it started from
    is refused with a ``409`` when someone else got there first.  ``size`` and
    ``sha256`` duplicate the current revision's row on purpose — the catalog
    lists hundreds of documents and a digest to display, and a join per row for
    two integers is not worth the round trip.
``document_revisions``
    Every revision of every document, oldest first, each pointing at its own
    immutable object.  The body is written once per save and never overwritten,
    so "what did this look like before?" is a row, not a backup.  Nothing prunes
    them: documentation is capped at a couple of megabytes per revision, and an
    offline deployment has no retention policy to inherit.
``document_assets``
    The images and attachments a document owns, keyed by the name its Markdown
    references (``assets/<name>``).  ``content_type`` is cached so serving an
    asset does not re-guess it, and ``sha256`` is what the integrity gate
    compares against the object.

Object keys are **opaque**: a bare ``uuid4``, no extension, no path.  A key is
an identifier for bytes, never a description of them — the row that owns it
carries the media type, the display name and the public path (``documents.slug``,
``document_assets.name``), and the URL surface (``/docs/<ecosystem>/<slug>``)
never mentions a key at all.  One row, one object, one uuid; the catalog's shape
lives in these tables rather than in the naming scheme, which is also why the
medium can change without renaming anything.

An operator may still keep a hand-written folder tree — that is what
``cli.py docs import`` reads and ``docs export`` writes — but it is a bridge, not
the store.
"""

from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped

from .base import Base, utcnow

#: Longest document slug (one URL path segment).  The validation lives in
#: :mod:`services.docs`; these are the schema's copy of the same numbers, and
#: that module imports them rather than declaring a second set.
SLUG_MAX = 96

#: Longest human-facing title.
TITLE_MAX = 160

#: Longest asset filename.
ASSET_NAME_MAX = 160

#: Length of the opaque object key: a uuid4 in canonical form.
KEY_MAX = 64

#: The media type of every document body.  Documentation is Markdown by
#: definition, so this is a constant — and it is written to the object store on
#: every save, so the bucket says what it holds.
DOC_CONTENT_TYPE = "text/markdown"


class Document(Base):
    """One documentation document, addressed by ``ecosystem`` + ``slug``."""

    __tablename__ = "documents"
    __table_args__ = (
        UniqueConstraint("ecosystem", "slug", name="uq_documents_ecosystem_slug"),
    )

    id: Mapped[int] = Column(Integer, primary_key=True, autoincrement=True)
    #: Sidebar group: python / npm / docker / debian / tools / models.
    ecosystem: Mapped[str] = Column(String(32), nullable=False, index=True)
    #: The URL segment, and the human-readable folder name on export.
    slug: Mapped[str] = Column(String(SLUG_MAX), nullable=False)
    title: Mapped[str] = Column(String(TITLE_MAX), nullable=False)
    #: Optimistic-concurrency token, starting at 1 for a document's first save.
    revision: Mapped[int] = Column(Integer, nullable=False, default=1)
    #: Object key of the *current* revision's Markdown — a bare uuid4.
    storage_key: Mapped[str] = Column(String(KEY_MAX), nullable=False)
    size: Mapped[int] = Column(Integer, nullable=False, default=0)
    sha256: Mapped[str | None] = Column(String(64), nullable=True)
    created_at: Mapped[datetime] = Column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    modified_at: Mapped[datetime] = Column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    #: ``auth.decorators.current_sub()`` when a request made the change, NULL for
    #: a CLI import.  A stable subject, never a display name.
    created_by: Mapped[str | None] = Column(String(256), nullable=True)
    modified_by: Mapped[str | None] = Column(String(256), nullable=True)

# ── Object keys ──────────────────────────────────────────────────────

def new_key() -> str:
    """A fresh opaque object key.

    One key per stored object — a revision body, an asset, a tool — generated
    where the row is created and read back from that row for ever after.  It is
    the whole naming scheme: no extension to go stale, no path to reorganise, and
    no way for code to mistake it for a description of the bytes.
    """
    return str(uuid4())


class DocumentRevision(Base):
    """One saved revision of one document — immutable once written."""

    __tablename__ = "document_revisions"
    __table_args__ = (
        UniqueConstraint("document_id", "revision", name="uq_document_revisions_number"),
    )

    id: Mapped[int] = Column(Integer, primary_key=True, autoincrement=True)
    document_id: Mapped[int] = Column(
        Integer,
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    revision: Mapped[int] = Column(Integer, nullable=False)
    #: Where this revision's Markdown lives; never rewritten after the insert.
    storage_key: Mapped[str] = Column(String(KEY_MAX), nullable=False)
    size: Mapped[int] = Column(Integer, nullable=False)
    sha256: Mapped[str | None] = Column(String(64), nullable=True)
    #: Written to the object store with the bytes, so the bucket can say what an
    #: extension-less key holds.
    content_type: Mapped[str] = Column(
        String(255), nullable=False, default=DOC_CONTENT_TYPE
    )
    #: The title as it was at this revision, so history shows what the document
    #: was called then rather than what it is called now.
    title: Mapped[str] = Column(String(TITLE_MAX), nullable=False)
    created_at: Mapped[datetime] = Column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    created_by: Mapped[str | None] = Column(String(256), nullable=True)


class DocumentAsset(Base):
    """One image or attachment belonging to a document.

    Keyed by ``(document_id, name)``: the name is what the Markdown references
    and what the URL carries, so re-uploading a file under the same name
    replaces it — the behaviour the editor's asset panel has always had.
    """

    __tablename__ = "document_assets"
    __table_args__ = (
        UniqueConstraint("document_id", "name", name="uq_document_assets_name"),
    )

    id: Mapped[int] = Column(Integer, primary_key=True, autoincrement=True)
    document_id: Mapped[int] = Column(
        Integer,
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = Column(String(ASSET_NAME_MAX), nullable=False)
    storage_key: Mapped[str] = Column(String(KEY_MAX), nullable=False)
    size: Mapped[int] = Column(Integer, nullable=False)
    sha256: Mapped[str | None] = Column(String(64), nullable=True)
    #: Guessed once at upload (``mimetypes``) and served back verbatim.
    content_type: Mapped[str] = Column(String(255), nullable=False, default="")
    created_at: Mapped[datetime] = Column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    created_by: Mapped[str | None] = Column(String(256), nullable=True)


#: The migration-state column a document carries while an import is partial —
#: deliberately not a column: an import writes one document per transaction (see
#: ``services.docs.import_tree``), so a half-imported document is not a state the
#: database can be in.  ``(ecosystem, slug)`` is the natural key an import
#: upserts on, which is what makes re-running it idempotent.


__all__ = [
    "ASSET_NAME_MAX",
    "DOC_CONTENT_TYPE",
    "Document",
    "DocumentAsset",
    "DocumentRevision",
    "KEY_MAX",
    "SLUG_MAX",
    "TITLE_MAX",
    "new_key",
]
