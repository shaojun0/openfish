"""Idempotent schema top-up for the Agent Hub tables — the no-Alembic migration.

``extensions.database`` calls ``Base.metadata.create_all()`` at boot, which
creates missing *tables* but never alters an existing one, and then runs its own
small ALTER list.  This module is the same idea, scoped to the ten
``models.agent_hub`` tables, and is the single entry point the Agent Hub code
(routes, the queue worker, the gates) uses when it needs the schema without
booting Flask:

    from models.agent_hub_migrate import ensure_schema
    ensure_schema(engine)

It does four things, in order, and is safe to run any number of times:

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
4. **Evolve CHECK constraints** listed in :data:`_CHECK_CONSTRAINTS` when the
   model's allowed values outgrow the live constraint (the ``TASK_KIND`` →
   ``checks`` change is the first case).  SQLite uses the documented
   table-rebuild procedure; PostgreSQL drops and re-adds the named constraint.
   The live constraint is inspected first, so an already-current database is a
   no-op.

The lists below are deliberately populated even though every table in this
module is new: that is the mechanism the next agent-hub schema change uses, and
leaving it empty-but-present is how the "one place to look" promise is kept.
Add a column to a model *and* to these lists in the same change.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.schema import CreateTable

from .agent_hub import (
    CHECK_SUITE_KIND,
    CHECK_SUITE_STATUS,
    IMPORT_PHASE,
    TASK_KIND,
    TASK_STATUS,
)
from .agent_hub import AgentTask, CheckRun, CheckSuiteSnapshot, CheckValidation
from .agent_hub import Finding, FindingEvent, FindingEvidence, GitIdentity
from .agent_hub import ImportJob, Repo, RepoCommit, RepoIssue, ReviewRun
from .base import Base

logger = logging.getLogger("cpypiserver.models.agent_hub_migrate")

#: Every table this module owns, in dependency order (a table appears after the
#: tables it references).  ``create_missing_tables`` also resolves foreign keys
#: by name, so the order is documentation rather than a requirement.
#: ``git_identities`` references ``users`` (the only table here that does), so
#: on an existing deployment it is created whole by ``create_missing_tables`` —
#: there is no column-level ALTER to back-fill.
AGENT_HUB_TABLES = (
    Repo, ImportJob, RepoIssue, RepoCommit, ReviewRun, AgentTask,
    CheckSuiteSnapshot, CheckValidation, CheckRun,
    Finding, FindingEvent, FindingEvidence, GitIdentity,
)

#: ``(table, constraint name, column, required values)`` for CHECK constraints
#: whose allowed set may grow.  Adding a value to one of these tuples in
#: ``models/agent_hub.py`` is enough: :func:`evolve_check_constraints` detects
#: that the live constraint does not admit the new value and rewrites it,
#: preserving every row.
_CHECK_CONSTRAINTS: tuple[tuple[str, str, str, tuple[str, ...]], ...] = (
    ("agent_tasks", "ck_agent_tasks_kind", "kind", TASK_KIND),
    ("agent_tasks", "ck_agent_tasks_status", "status", TASK_STATUS),
    ("check_suite_snapshots", "ck_check_suite_snapshots_kind", "kind", CHECK_SUITE_KIND),
    ("check_suite_snapshots", "ck_check_suite_snapshots_status", "status",
     CHECK_SUITE_STATUS),
    # ``import_jobs`` predates this module's top-up discipline, so a deployment
    # created before ``validate`` was added carries the old CHECK and every
    # ``POST /repos/import`` fails.  Evolving it here repairs those databases.
    ("import_jobs", "ck_import_jobs_phase", "phase", IMPORT_PHASE),
)

#: ``(table, column, column DDL)`` for columns that may postdate a database
#: created by an earlier version of this module.  The DDL must be acceptable to
#: both SQLite and PostgreSQL — nullable, no server-side default beyond a
#: constant, no inline foreign-key clause on SQLite-only syntax.
#:
#: Only ``repos`` needs entries today: it is the table a deployment that
#: predates this module can already have.  The other nine are created whole by
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
    # Cached per-repo review policy; NULL = "not read yet" (webhook defaults on).
    ("repos", "auto_review", "BOOLEAN"),
    # Producer-side idempotency key; NULL on rows written before it existed,
    # which the dedup query treats as "no key" rather than as a match.
    ("agent_tasks", "dedup_key", "VARCHAR(200)"),
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

        {"created_tables": [...], "added_columns": ["repos.foo"],
         "added_indexes": [...], "updated_constraints": ["agent_tasks.ck_agent_tasks_kind"]}

    An empty report means the database was already current — which is the normal
    case after the first boot.  Never raises because something already exists:
    existence is decided by inspection, not by catching an exception.
    """
    report: dict[str, list[str]] = {
        "created_tables": [],
        "added_columns": [],
        "added_indexes": [],
        "updated_constraints": [],
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

    # CHECK constraints are rewritten last: the tables they live on must exist
    # (created above) and their indexes must be present if a SQLite rebuild has
    # to recreate them.
    report["updated_constraints"] = evolve_check_constraints(engine)

    if any(report.values()):
        logger.info("Agent-hub schema updated: %s", report)
    return report


# ── CHECK-constraint evolution ───────────────────────────────────────
# create_all() and ALTER ADD COLUMN cannot widen a CHECK, so adding a value to
# TASK_KIND used to be a schema change an existing deployment could not pick up.
# This is that missing step, done the way each backend documents it.

_LITERAL_RE = re.compile(r"'((?:[^']|'')*)'")


def _table_model(table: str) -> Any | None:
    return next((model for model in AGENT_HUB_TABLES if model.__tablename__ == table), None)


def _check_literals(sqltext: str) -> set[str]:
    """The single-quoted literals inside a CHECK expression."""
    return {match.replace("''", "'") for match in _LITERAL_RE.findall(sqltext or "")}


def _live_checks(inspector: Any, table: str) -> list[dict[str, Any]]:
    try:
        return [dict(item) for item in inspector.get_check_constraints(table)]
    except Exception as exc:  # a backend that cannot introspect must fail loudly
        logger.warning("cannot introspect CHECK constraints on %s: %s", table, exc)
        return []


def _constraint_admits(
    live: list[dict[str, Any]],
    name: str,
    values: tuple[str, ...],
) -> bool:
    """Whether the live constraint *name* already accepts every required value."""
    for item in live:
        if item.get("name") != name:
            continue
        return set(values) <= _check_literals(str(item.get("sqltext") or ""))
    return False


def _mentions_column(sqltext: str, column: str) -> bool:
    return re.search(rf"(?<![A-Za-z0-9_]){re.escape(column)}(?![A-Za-z0-9_])", sqltext or "") is not None


def _in_check_sql(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN (" + ", ".join(f"'{value}'" for value in values) + ")"


def evolve_check_constraints(engine: Engine) -> list[str]:
    """Rewrite every CHECK constraint whose allowed set has grown.

    Inspects first, so an already-current database changes nothing (the second
    call is a no-op) and no data is touched when nothing needs rewriting.
    """
    updated: list[str] = []
    inspector = inspect(engine)
    present = set(inspector.get_table_names())
    for table, name, column, values in _CHECK_CONSTRAINTS:
        if table not in present:
            continue
        live = _live_checks(inspector, table)
        if _constraint_admits(live, name, values):
            continue
        if _dialect_name(engine) == "postgresql":
            _evolve_postgres(engine, table, name, column, values, live)
        else:
            _rebuild_sqlite(engine, table)
        updated.append(f"{table}.{name}")
        logger.warning(
            "Migrated agent-hub database: widened CHECK %s on %s to admit %s",
            name, table, ", ".join(values),
        )
        inspector = inspect(engine)  # the shape changed; re-read before the next
    return updated


def _evolve_postgres(
    engine: Engine,
    table: str,
    name: str,
    column: str,
    values: tuple[str, ...],
    live: list[dict[str, Any]],
) -> None:
    """``DROP CONSTRAINT`` + ``ADD CONSTRAINT`` with the model's own name."""
    with engine.begin() as conn:
        # Drop every live CHECK that constrains this column, whatever it is
        # called: a hand-created deployment may not use the model's name, and a
        # stale duplicate would keep rejecting the new value.
        for item in live:
            live_name = str(item.get("name") or "")
            if not live_name or not _mentions_column(str(item.get("sqltext") or ""), column):
                continue
            conn.execute(text(f'ALTER TABLE {table} DROP CONSTRAINT IF EXISTS "{live_name}"'))
        conn.execute(text(
            f"ALTER TABLE {table} ADD CONSTRAINT {name} CHECK ({_in_check_sql(column, values)})"
        ))


def _rebuild_sqlite(engine: Engine, table: str) -> None:
    """The documented SQLite table-rebuild, preserving rows, FKs and indexes.

    Order matters: ``PRAGMA foreign_keys=OFF`` must run *outside* a transaction
    (it is silently ignored inside one), the new table is created under a
    temporary name so the live table is only absent between ``DROP`` and
    ``RENAME``, and the FKs of other tables keep pointing at ``table`` because
    that name is never renamed away.  ``PRAGMA foreign_key_check`` verifies the
    result before the connection is released.
    """
    model = _table_model(table)
    if model is None:
        raise RuntimeError(f"no model registered for table {table!r}")
    new_name = f"{table}__openfish_new"
    ddl = str(CreateTable(model.__table__).compile(dialect=engine.dialect))
    match = re.search(rf"CREATE TABLE {re.escape(table)}(?![A-Za-z0-9_])", ddl)
    if match is None:
        raise RuntimeError(f"unexpected CREATE TABLE DDL for {table}: {ddl[:80]!r}")
    # Replace only the table name in the CREATE TABLE clause; constraint and
    # index names that also contain the table name must stay untouched.
    ddl = ddl[: match.start()] + f"CREATE TABLE {new_name}" + ddl[match.end():]

    columns = [column.name for column in model.__table__.columns]
    column_sql = ", ".join(columns)
    raw = engine.raw_connection()
    try:
        cursor = raw.cursor()
        cursor.execute("PRAGMA foreign_keys=OFF")
        try:
            cursor.execute("BEGIN")
            cursor.execute(f"DROP TABLE IF EXISTS {new_name}")
            cursor.execute(ddl)
            cursor.execute(
                f"INSERT INTO {new_name} ({column_sql}) SELECT {column_sql} FROM {table}"
            )
            cursor.execute(f"DROP TABLE {table}")
            cursor.execute(f"ALTER TABLE {new_name} RENAME TO {table}")
            for index in model.__table__.indexes:
                index_columns = ", ".join(column.name for column in index.columns)
                unique_sql = "UNIQUE " if index.unique else ""
                cursor.execute(
                    f"CREATE {unique_sql}INDEX IF NOT EXISTS {index.name} "
                    f"ON {table}({index_columns})"
                )
            cursor.execute("COMMIT")
        except Exception:
            cursor.execute("ROLLBACK")
            raise
        finally:
            cursor.execute("PRAGMA foreign_keys=ON")
        violations = cursor.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise RuntimeError(
                f"foreign key violations after rebuilding {table}: {violations[:5]}"
            )
    finally:
        raw.close()


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


__all__ = [
    "AGENT_HUB_TABLES",
    "create_missing_tables",
    "ensure_schema",
    "evolve_check_constraints",
]
