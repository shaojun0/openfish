"""First-run defaults for the artifact catalogs — a checked-in ``.sql`` plus its objects.

A fresh deployment used to get its documentation and tools for free: they were
files in the catalog directories, and the directory *was* the catalog.  Now the
catalog is a table and the bytes are behind the storage port, so "the samples are
in the repository" no longer means "the panel has something to show" — which is
exactly the gap this module closes, at the layer the user pointed at: **database
initialization**, not the container.

    backend/config/seed/docs.seed.sql     the documents and their revisions
    backend/config/seed/tools.seed.sql    the tool categories and entries
    backend/config/seed/objects/<ns>/<key>  the bytes each row references

Three properties are deliberate:

* **The roster is the namespace registry.**  Which catalogs ship a seed, where
  their directory is and which table their rows land in is answered once, in
  :mod:`services.namespaces`; this module only knows how to apply one ``.sql``.
* **The keys are literal uuid4s in the SQL.**  The seed is a data artifact, so
  what it installs is reviewable in the diff (and identical everywhere) instead
  of being generated at whichever moment a deployment first boots.
* **It runs through the same import path as everything else.**  The rows it
  writes are the rows ``cli.py docs|tools import`` would write — same columns,
  same ``NOT EXISTS`` guards — so the seed cannot drift into a second, special
  way of putting a catalog together.  The media type each object is stored with
  comes from ``config/seed/content-types.json``, not from the key: a key is an
  opaque uuid and says nothing about what it holds.
* **A mirror namespace seeds rows only.**  npm / debian / docker-images name
  files that stay in the operator's directory, so their seed carries no objects
  and their rows have no ``storage_key``.
* **It happens once.**  :class:`models.catalog.CatalogSeedState` records the
  installation, so an administrator who deletes the defaults keeps them deleted
  across restarts.  "The table is empty" alone would reinstall them on every
  boot, which is the one way a boot-time seed can override a human decision.

Nothing is logged here: this branch's convention is that the backend does not
write log lines, and the one thing an operator needs to see — that a fresh
database was initialized — is the return value, which ``cli.py catalogs seed``
prints when it is run by hand.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import func, inspect, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from models.catalog import CatalogEntry, CatalogSeedState
from services import namespaces

#: Where the seed lives, relative to the backend package root.
SEED_DIR = namespaces.SEED_DIR

#: The catalogs a fresh database is initialized with: namespace -> seed file,
#: in installation order.  Derived from the namespace registry, so registering a
#: catalog there is what puts its defaults on this list.  npm / docker-images /
#: debian are mirrors: their seed carries rows only, describing files an
#: operator supplies.
CATALOGS: tuple[tuple[str, str], ...] = tuple(
    (entry.name, entry.seed_file)
    for entry in namespaces.NAMESPACES.values()
    if entry.seed_file is not None
)


@dataclass(frozen=True, slots=True)
class SeedOutcome:
    """What one namespace's seed did (or did not do, and why)."""

    namespace: str
    action: str        # "seeded" | "already" | "not-empty" | "absent"
    objects: int = 0
    statements: int = 0


def ensure_seed(engine: Engine, *, force: bool = False) -> list[SeedOutcome]:
    """Install the first-run defaults into an empty database; idempotent.

    Called from :func:`extensions.database.init_engine`, i.e. on the same path
    that creates the tables and applies the light migrations — for the API
    process and for ``cli.py`` alike.  A namespace is seeded only when its rows
    are absent, its ``.sql`` is present and it has never been seeded; every other
    case is reported rather than acted on.

    *force* (``cli.py catalogs seed --force``) ignores the two guards and applies
    the seed anyway, which is how an administrator asks for the defaults back
    after deleting them.  It stays safe because every statement is written with
    a ``NOT EXISTS`` guard: re-applying it cannot duplicate a row.
    """
    outcomes: list[SeedOutcome] = []
    for namespace in namespaces.SEEDED:
        outcomes.append(_seed_namespace(engine, namespaces.resolve(namespace), force=force))
    return outcomes


def _seed_namespace(
    engine: Engine,
    entry: namespaces.Namespace,
    *,
    force: bool = False,
) -> SeedOutcome:
    sql_path = entry.seed_path
    if sql_path is None or not sql_path.is_file():
        return SeedOutcome(entry.name, "absent")
    if not inspect(engine).has_table("catalog_seed_state"):
        # `create_all` has not run yet — nothing can be seeded into tables that
        # do not exist, and this call will happen again on the next boot.
        return SeedOutcome(entry.name, "absent")

    statements = _statements(sql_path)
    objects = _objects_for(entry.name)

    with Session(engine) as session:
        if not force and session.get(CatalogSeedState, entry.name) is not None:
            return SeedOutcome(entry.name, "already")
        if not force and _has_rows(session, entry.name):
            # Rows without a marker: an upgraded deployment that already has a
            # catalogue.  Record the state and leave the content alone.
            _mark(session, entry.name)
            return SeedOutcome(entry.name, "not-empty")

    # Objects first: a row that points at a missing object is a broken page,
    # while an object no row names is inert (and the integrity check reports it).
    written = _install_objects(entry.name, objects)

    with engine.begin() as connection:
        for statement in statements:
            connection.exec_driver_sql(statement)
    with Session(engine) as session:
        _mark(session, entry.name)
    return SeedOutcome(entry.name, "seeded", objects=written, statements=len(statements))


def _has_rows(session: Session, namespace: str) -> bool:
    if namespaces.resolve(namespace).rows == namespaces.DOCUMENT_ROWS:
        from models.docs import Document

        return session.scalar(select(func.count()).select_from(Document)) > 0
    return (
        session.scalar(
            select(func.count())
            .select_from(CatalogEntry)
            .where(CatalogEntry.namespace == namespace)
        )
        > 0
    )


def _mark(session: Session, namespace: str) -> None:
    row = session.get(CatalogSeedState, namespace)
    if row is None:
        session.add(CatalogSeedState(namespace=namespace))
        session.commit()


def _statements(sql_path: Path) -> list[str]:
    """The executable statements of one seed file, in order.

    One statement per line, which is how ``config/seed/README.md``'s recipe
    writes them: splitting a general SQL file is a parser, and a parser here
    would be a second implementation of something the generator already knows.
    """
    out: list[str] = []
    for line in sql_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("--"):
            continue
        out.append(stripped)
    return out


def _objects_for(namespace: str) -> dict[str, tuple[Path, str]]:
    """``storage_key -> (source file, media type)`` for one namespace's objects.

    Only a catalog that owns objects has any: a mirror's seed names files an
    operator supplies, so a mirror registers ``owns_objects=False`` and this
    returns nothing for it however the directory looks.
    """
    if not namespaces.resolve(namespace).owns_objects:
        return {}
    directory = SEED_DIR / "objects" / namespace
    if not directory.is_dir():
        return {}
    types = _content_types().get(namespace, {})
    return {
        path.name: (path, types.get(path.name, "application/octet-stream"))
        for path in sorted(directory.iterdir())
        if path.is_file()
    }


def _content_types() -> dict[str, dict[str, str]]:
    """The manifest the generator writes beside the seed objects."""
    path = SEED_DIR / "content-types.json"
    if not path.is_file():
        return {}
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _install_objects(namespace: str, objects: dict[str, tuple[Path, str]]) -> int:
    """Copy the seed's bytes into the catalog's store under their literal keys."""
    if not objects:
        return 0
    from services import objectstore

    store = objectstore.catalog_store(namespace, namespaces.root_for(namespace))
    written = 0
    for key, (path, content_type) in objects.items():
        store.put(key, (path.read_bytes(),), content_type=content_type)
        written += 1
    return written


__all__ = ["CATALOGS", "SEED_DIR", "SeedOutcome", "ensure_seed"]
