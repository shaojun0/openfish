"""Idempotent schema top-up for the Agent Hub tables — the no-Alembic migration.

``extensions.database`` calls ``Base.metadata.create_all()`` at boot, which
creates missing *tables* but never alters an existing one, and then runs its own
small ALTER list.  This module is the same idea, scoped to the nine
``models.agent_hub`` tables, and is the single entry point the Agent Hub code
(routes, the queue worker, the gates) uses when it needs the schema without
booting Flask:

    from models.agent_hub_migrate import ensure_schema
    ensure_schema(engine)

It does three things, in order, and is safe to run any number of times:

1. **Create missing tables.**  Only the Agent Hub tables are created, and only
   the absent ones, so calling this against an existing deployment cannot touch
   anything that was not already there.
2. **Add missing columns** listed in :data:`_COLUMNS`.  Introspection goes
   through SQLAlchemy's Inspector, so the same code path works on SQLite and
   PostgreSQL.  Every added column must be nullable with a constant default —
   SQLite cannot add a ``NOT NULL`` column to a non-empty table, and the
   historical fix for that is a nullable column plus a data backfill.
3. **Create missing indexes** listed in :data:`_INDEXES`.  These use the same
   names as the constraints declared in ``models/agent_hub.py``, so a fresh
   ``create_all`` and an ALTER-migrated database converge on one shape instead
   of growing two equivalent indexes.

The lists below are deliberately populated even though every table in this
module is new: that is the mechanism the next agent-hub schema change uses, and
leaving it empty-but-present is how the "one place to look" promise is kept.
Add a column to a model *and* to these lists in the same change.
"""

from __future__ import annotations

import logging

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

from .agent_hub import AgentTask, Finding, FindingEvent, FindingEvidence, ImportJob
from .agent_hub import Repo, RepoCommit, RepoIssue, ReviewRun
from .base import Base

logger = logging.getLogger("cpypiserver.models.agent_hub_migrate")

#: Every table this module owns, in dependency order (a table appears after the
#: tables it references).  ``create_missing_tables`` also resolves foreign keys
#: by name, so the order is documentation rather than a requirement.
AGENT_HUB_TABLES = (
    Repo, ImportJob, RepoIssue, RepoCommit, ReviewRun, AgentTask,
    Finding, FindingEvent, FindingEvidence,
)

#: ``(table, column, column DDL)`` for columns that may postdate a database
#: created by an earlier version of this module.  The DDL must be acceptable to
#: both SQLite and PostgreSQL — nullable, no server-side default beyond a
#: constant, no inline foreign-key clause on SQLite-only syntax.
#:
#: Only ``repos`` needs entries today: it is the table a deployment that
#: predates this module can already have.  The other eight are created whole by
#: :func:`create_missing_tables`, so a column added to one of *them* later is
#: added here as well — that is the maintenance rule.
_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("repos", "source", "VARCHAR(16)"),
    ("repos", "source_url", "VARCHAR(512)"),
    ("repos", "default_branch", "VARCHAR(128)"),
    ("repos", "forgejo_repo", "VARCHAR(256)"),
    ("repos", "kind", "VARCHAR(16)"),
    ("repos", "sync_state", "VARCHAR(16)"),
    ("repos", "synced_at", "TIMESTAMP"),
    ("repos", "issue_count", "INTEGER"),
    ("repos", "commit_count", "INTEGER"),
    ("repos", "created_at", "TIMESTAMP"),
    ("repos", "updated_at", "TIMESTAMP"),
)

#: ``(index name, table, column definitions, unique?)``.  ``IF NOT EXISTS`` is
#: supported by SQLite (>= 3.8) and PostgreSQL (>= 9.5), so no existence probe
#: is needed; every name matches the constraint SQLAlchemy declares in
#: ``models/agent_hub.py``, so a fresh database and a migrated one converge on
#: the same index instead of growing two equivalents.  ``TEXT`` is rendered
#: per-dialect by :func:`_column_ddl` when needed, but indexes only need the
#: column list, so these entries are dialect-free.
_INDEXES: tuple[tuple[str, str, str, bool], ...] = (
    ("ix_repos_slug", "repos", "slug", True),
    ("ix_repos_sync_state", "repos", "sync_state", False),
)


def _dialect_name(engine: Engine) -> str:
    """``"sqlite"`` or ``"postgresql"`` — the two backends this ships against."""
    return engine.dialect.name


def _column_ddl(engine: Engine, sql_type: str, *, nullable: bool = True) -> str:
    """A portable column definition for ``ALTER TABLE … ADD COLUMN``.

    The text type differs (PostgreSQL ``TEXT``, SQLite ``VARCHAR``) and is cast
    through each dialect's own generic type, so the same manifest entry produces
    valid DDL on both.  Only ever used for *nullable* columns — see the module
    docstring.
    """
    type_sql = sql_type
    if sql_type.upper() == "TEXT" and _dialect_name(engine) != "postgresql":
        type_sql = "VARCHAR"
    suffix = "" if nullable else " NOT NULL"
    return f"{type_sql}{suffix}"


def ensure_schema(engine: Engine, *, create: bool = True) -> dict[str, list[str]]:
    """Bring *engine*'s Agent Hub schema up to date; idempotent.

    Returns a report of what this call actually changed::

        {"created_tables": [...], "added_columns": ["repos.foo"], "added_indexes": [...]}

    An empty report means the database was already current — which is the normal
    case after the first boot.  Never raises because something already exists:
    existence is decided by inspection, not by catching an exception.
    """
    report: dict[str, list[str]] = {
        "created_tables": [],
        "added_columns": [],
        "added_indexes": [],
    }

    if create:
        report["created_tables"] = create_missing_tables(engine)

    inspector = inspect(engine)
    present = set(inspector.get_table_names())

    with engine.begin() as conn:
        for table, column, sql_type in _COLUMNS:
            if table not in present:
                continue  # no table to alter — nothing to back-fill either
            if column in {c["name"] for c in inspector.get_columns(table)}:
                continue
            ddl = _column_ddl(engine, sql_type)
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
            report["added_columns"].append(f"{table}.{column}")
            logger.warning("Migrated agent-hub database: added %s.%s", table, column)

        for name, table, columns, unique in _INDEXES:
            if table not in present:
                continue
            # `CREATE INDEX IF NOT EXISTS` succeeds whether or not the index was
            # there, so the report is driven by inspection — "did this call
            # change anything?" has to be answerable, because "create_all plus
            # this helper" is the whole migration story.
            if name in {index["name"] for index in inspector.get_indexes(table)}:
                continue
            unique_sql = "UNIQUE " if unique else ""
            conn.execute(text(
                f"CREATE {unique_sql}INDEX IF NOT EXISTS {name} ON {table}({columns})"
            ))
            report["added_indexes"].append(name)
            logger.warning("Migrated agent-hub database: created index %s", name)

    if any(report.values()):
        logger.info("Agent-hub schema updated: %s", report)
    return report


def create_missing_tables(engine: Engine) -> list[str]:
    """``create_all`` for the Agent Hub tables only; returns the names created.

    Restricted on purpose: a caller that needs the queue schema must not be able
    to create — or worse, alter — tables belonging to another module.
    """
    inspector = inspect(engine)
    present = set(inspector.get_table_names())
    missing = [model for model in AGENT_HUB_TABLES if model.__tablename__ not in present]
    if not missing:
        return []

    # create_all itself skips existing tables, so passing the full list would be
    # safe too; passing the missing ones keeps the intent explicit and makes the
    # returned report exact.  It resolves inter-table foreign keys by name, so
    # ordering within the list does not matter.
    Base.metadata.create_all(engine, tables=[model.__table__ for model in missing])
    logger.info("Agent-hub schema created: %s", [model.__tablename__ for model in missing])
    return [model.__tablename__ for model in missing]


__all__ = ["AGENT_HUB_TABLES", "create_missing_tables", "ensure_schema"]
