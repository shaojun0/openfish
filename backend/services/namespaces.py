"""The catalog namespaces — one registration per catalog, one place to add one.

The artifact hub serves five catalogs, and before this module every fact about
one of them was spelled out somewhere else:

* the deployment directory came from a field on :class:`config.hub.HubConfig`;
* the overlay's top-level JSON key and the default ``catalog.json`` path lived in
  :mod:`services.mirror_catalog`;
* "does this catalog own objects, which table backs its rows, which seed file
  ships its defaults" lived in :mod:`services.catalog_seed`, with a ``docs``
  special case for each.

That is four answers to the same question — *what is this namespace?* — spread
over three modules, and adding a sixth catalog meant finding all of them.  This
module is the one answer.  A namespace is registered as a :class:`Namespace`
record in :data:`NAMESPACES`; every consumer reads that record instead of its own
copy of the knowledge, so adding one is appending one entry (and, if it ships
defaults, one ``.sql`` file under ``config/seed/``).

The name is deliberately the *storage* namespace (``docker-images``, not
``docker``): it is the ``catalog_entries.namespace`` / S3 prefix / ``objects/``
sub-directory, so a typo cannot create a namespace nobody reads.

What is deliberately *not* here: per-namespace behaviour.  The registry says
which root a catalog lives under, whether its rows own bytes, which table backs
them and which JSON key its overlay uses; it does not know how to scan a
directory or import a tree, because that is the namespace's own module
(:mod:`services.tool_catalog`, :mod:`services.mirror_catalog`, …).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from config import settings
from models.catalog import TOOLS_NAMESPACE

# ── Namespace names ──────────────────────────────────────────────────
# The string that appears in ``catalog_entries.namespace``, in the seed's SQL
# and on the CLI.  Spelled once.

#: Documentation: ``documents`` rows plus an object per revision and asset.
DOCS = "docs"

#: The tools catalog: ``catalog_entries`` rows that own an object each.
TOOLS = TOOLS_NAMESPACE

#: The npm mirror: tarballs stay in the directory, rows describe them.
NPM = "npm"

#: The flat Debian mirror.
DEBIAN = "debian"

#: The ``docker save`` image-tarball mirror.  Suffixed so it never collides with
#: the repository's ``docker/`` infrastructure directory.
DOCKER_IMAGES = "docker-images"

# ── Which table backs a namespace's rows ─────────────────────────────
# ``documents`` is a table of its own (one row per document, keyed by ecosystem
# and slug); every other namespace shares ``catalog_entries``, scoped by the
# ``namespace`` column.  The distinction is what a seed or an emptiness check
# has to know.

#: Rows live in ``documents``.
DOCUMENT_ROWS = "documents"

#: Rows live in ``catalog_entries``, filtered by ``namespace``.
ENTRY_ROWS = "catalog_entries"

# ── Shared facts about the catalogs ──────────────────────────────────

#: The overlaid metadata file, beside a catalog's files.  The *import/export*
#: format, never a second source of truth; see :mod:`services.mirror_catalog`.
OVERLAY_FILENAME = "catalog.json"

#: Where the shipped seeds live, relative to the backend package root.
SEED_DIR = Path(__file__).resolve().parent.parent / "config" / "seed"


@dataclass(frozen=True, slots=True)
class Namespace:
    """One catalog, described once.

    ``root`` is the deployment directory the catalog's files (and, for a
    locally-stored catalog, its ``objects/`` sub-directory) live under.  It is
    the *default* path: an object backend can ignore it entirely, which is why
    :func:`services.objectstore.catalog_store` still takes it as a parameter.
    """

    name: str
    #: The catalog's directory in this deployment (``TOOLS_DIR``, ``NPM_DIR``…).
    root: str
    #: ``True`` when a row owns bytes behind the storage port (docs, tools);
    #: ``False`` for a mirror, whose file *is* the artifact and stays put.
    owns_objects: bool
    #: :data:`DOCUMENT_ROWS` or :data:`ENTRY_ROWS` — which table holds its rows.
    rows: str
    #: The top-level JSON key of the overlay file (``packages`` / ``artifacts``);
    #: ``None`` when the namespace has no single-key overlay (docs, tools).
    overlay_key: str | None = None
    #: The file name of the overlay inside :attr:`root`; ``None`` for a catalog
    #: whose metadata is not an overlay file at all (docs).
    overlay_file: str | None = None
    #: The seed file under :data:`SEED_DIR` a fresh database installs, if any.
    seed_file: str | None = None

    @property
    def overlay_path(self) -> Path | None:
        """The default import/export file, or ``None`` when there is not one."""
        if self.overlay_file is None:
            return None
        return Path(self.root) / self.overlay_file

    @property
    def seed_path(self) -> Path | None:
        """The shipped ``.sql`` for this namespace, or ``None`` when it ships none."""
        if self.seed_file is None:
            return None
        return SEED_DIR / self.seed_file


#: Every catalog, in the order the CLI lists them and a fresh database seeds
#: them.  Appending an entry here is what "adding a namespace" means.
NAMESPACES: dict[str, Namespace] = {
    DOCS: Namespace(
        name=DOCS,
        root=settings.hub.docs_dir,
        owns_objects=True,
        rows=DOCUMENT_ROWS,
        seed_file="docs.seed.sql",
    ),
    TOOLS: Namespace(
        name=TOOLS,
        root=settings.hub.tools_dir,
        owns_objects=True,
        rows=ENTRY_ROWS,
        overlay_key=None,
        overlay_file=OVERLAY_FILENAME,
        seed_file="tools.seed.sql",
    ),
    NPM: Namespace(
        name=NPM,
        root=settings.hub.npm_dir,
        owns_objects=False,
        rows=ENTRY_ROWS,
        overlay_key="packages",
        overlay_file=OVERLAY_FILENAME,
        seed_file="npm.seed.sql",
    ),
    DEBIAN: Namespace(
        name=DEBIAN,
        root=settings.hub.debian_dir,
        owns_objects=False,
        rows=ENTRY_ROWS,
        overlay_key="artifacts",
        overlay_file=OVERLAY_FILENAME,
        seed_file="debian.seed.sql",
    ),
    DOCKER_IMAGES: Namespace(
        name=DOCKER_IMAGES,
        root=settings.hub.docker_dir,
        owns_objects=False,
        rows=ENTRY_ROWS,
        overlay_key="artifacts",
        overlay_file=OVERLAY_FILENAME,
        seed_file="docker-images.seed.sql",
    ),
}

#: The namespaces whose metadata an operator imports/exports as ``catalog.json``
#: — every namespace with a single-key overlay, in registration order.
MIRRORS: tuple[str, ...] = tuple(
    entry.name for entry in NAMESPACES.values() if entry.overlay_key is not None
)

#: The namespaces a fresh database is initialized with, in installation order.
SEEDED: tuple[str, ...] = tuple(
    entry.name for entry in NAMESPACES.values() if entry.seed_file is not None
)


def get(namespace: str) -> Namespace | None:
    """The registration for *namespace*, or ``None`` when there is not one."""
    return NAMESPACES.get(namespace)


def resolve(namespace: str) -> Namespace:
    """The registration for *namespace*; refuse an unregistered one.

    A typo (``docker`` for ``docker-images``) would otherwise create a namespace
    nobody reads: rows appear, the page stays empty and nothing says why.  The
    message names the registered set so the fix is obvious.
    """
    entry = NAMESPACES.get(namespace)
    if entry is None:
        raise ValueError(
            f"unknown catalog namespace {namespace!r} "
            f"(expected one of: {', '.join(NAMESPACES)})"
        )
    return entry


def root_for(namespace: str) -> str:
    """The deployment directory one namespace's catalog lives under."""
    return resolve(namespace).root


def overlay_path(namespace: str) -> Path | None:
    """The default overlay file of *namespace*, or ``None`` when it has none."""
    return resolve(namespace).overlay_path


__all__ = [
    "DEBIAN",
    "DOCS",
    "DOCKER_IMAGES",
    "DOCUMENT_ROWS",
    "ENTRY_ROWS",
    "MIRRORS",
    "NAMESPACES",
    "NPM",
    "OVERLAY_FILENAME",
    "Namespace",
    "SEEDED",
    "SEED_DIR",
    "TOOLS",
    "get",
    "overlay_path",
    "resolve",
    "root_for",
]
