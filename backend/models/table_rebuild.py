"""Rebuilding a SQLite table — the one implementation of the documented procedure.

``create_all()`` creates missing *tables* and ``ALTER TABLE … ADD COLUMN`` adds
columns, but SQLite cannot drop a column, widen a ``NOT NULL``, or alter a CHECK
constraint in place.  The documented answer is to build the new table, copy the
rows, drop the old one and rename — and this project needs it in two places
(retiring ``model_routes.api_key_env``, and evolving an Agent Hub CHECK
constraint), so it lives here rather than as a private copy per migrator.

Order is what makes this correct, and every step below is load-bearing:

* ``PRAGMA foreign_keys=OFF`` must run *outside* a transaction — it is silently
  ignored inside one, and with FKs on the ``DROP TABLE`` would cascade into
  other tables' rows;
* the new table is created under a temporary name, so the live table is absent
  only between ``DROP`` and ``RENAME`` — and because the table's own name is
  never renamed away, other tables' foreign keys keep pointing at it;
* ``PRAGMA foreign_key_check`` verifies the result before the connection is
  released, so a mistake raises here rather than surfacing as orphaned rows.

The new shape comes from ``Base.metadata`` — the declaring model — so a
migration cannot drift from the schema the application writes.
"""

from __future__ import annotations

import logging
import re

from sqlalchemy import inspect
from sqlalchemy.engine import Engine
from sqlalchemy.schema import CreateTable

from .base import Base

logger = logging.getLogger("cpypiserver.models.table_rebuild")

#: Suffix for the table that exists only mid-rebuild.
NEW_TABLE_SUFFIX = "__openfish_new"


def table_present(engine: Engine, table: str) -> bool:
    """Whether *table* exists — the guard every migration step starts with."""
    return table in set(inspect(engine).get_table_names())


def columns_present(engine: Engine, table: str) -> set[str]:
    """The column names *table* currently has (empty when it does not exist)."""
    inspector = inspect(engine)
    if table not in set(inspector.get_table_names()):
        return set()
    return {column["name"] for column in inspector.get_columns(table)}


def rebuild_table(engine: Engine, table: str) -> None:
    """Rebuild *table* to match its model's current definition, preserving rows.

    Columns present in the database but no longer declared on the model are
    **dropped**, and their values with them — the caller is responsible for
    having decided that is intended.
    """
    model_table = Base.metadata.tables.get(table)
    if model_table is None:
        raise RuntimeError(f"no model registered for table {table}")
    new_name = f"{table}{NEW_TABLE_SUFFIX}"
    ddl = str(CreateTable(model_table).compile(dialect=engine.dialect))
    match = re.search(rf"CREATE TABLE {re.escape(table)}(?![A-Za-z0-9_])", ddl)
    if match is None:
        raise RuntimeError(f"unexpected CREATE TABLE DDL for {table}: {ddl[:80]}")
    # Replace only the table name in the CREATE TABLE clause; constraint and
    # index names that also contain the table name must stay untouched.
    ddl = ddl[: match.start()] + f"CREATE TABLE {new_name}" + ddl[match.end():]

    columns = [column.name for column in model_table.columns]
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
            for index in model_table.indexes:
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


__all__ = [
    "NEW_TABLE_SUFFIX",
    "columns_present",
    "rebuild_table",
    "table_present",
]
