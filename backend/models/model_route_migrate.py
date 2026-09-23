"""Idempotent schema top-up for the ``model_routes`` table — no Alembic here either.

``extensions.database`` calls ``Base.metadata.create_all()`` at boot, which
creates missing *tables* but never alters an existing one.  This module is the
same idea as :mod:`models.agent_hub_migrate`, scoped to the one table that
module does not own.

It exists because ``api_key_env`` was **retired**: a route used to be able to
name an environment variable its key was read from, and that column is ``NOT
NULL`` with no server default, so a database created before the change rejects
every insert the application now makes (the ORM no longer supplies a value).
Dropping it is therefore not cosmetic — it is what keeps an upgraded deployment
writable.

* **PostgreSQL** — ``ALTER TABLE … DROP COLUMN IF EXISTS``, the cheap path.
* **SQLite** — a table rebuild, because SQLite cannot drop a column in place.
  :func:`models.table_rebuild.rebuild_table` is the project's one implementation
  of the documented procedure.

The retired column held an environment-variable *name*, never a secret, so
nothing is lost when the values go — but an operator who was relying on it must
now store the key itself (``cli.py model-route set-key``), which is the whole
point of the change.

:func:`ensure_schema` also *reports* rows whose ``api_key`` is still plaintext.
It deliberately does **not** re-seal them: sealing needs the master key and
writes a secret, so it is an explicit operator action (``cli.py model-route
seal``), not a side effect of booting.  Counting them needs no key, which is why
the visibility can live here while the rewrite does not.
"""

from __future__ import annotations

import logging

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

from config.keys import KeysConfig

from .model_route import API_KEY_PREFIX, ModelRoute
from .table_rebuild import rebuild_table

logger = logging.getLogger("cpypiserver.models.model_route_migrate")

#: The column this module retires.  Kept as a constant so the docstring, the
#: DDL and the log line cannot drift apart.
RETIRED_COLUMN = "api_key_env"

#: The table this module owns.
TABLE = ModelRoute.__tablename__

#: The variable an operator is told to set, derived from the config model that
#: owns it so the name in the log cannot drift from the name that is read.
MODEL_ROUTE_KEY_ENV = KeysConfig.env_name("model_route_key")


def plaintext_key_count(engine: Engine) -> int:
    """How many routes still hold an un-sealed key.

    A pure string test against :data:`models.model_route.API_KEY_PREFIX`, so it
    answers without the master key — which is what lets boot report "3 routes
    still store a plaintext key" on a deployment that has not configured one yet.
    """
    inspector = inspect(engine)
    if TABLE not in set(inspector.get_table_names()):
        return 0
    columns = {column["name"] for column in inspector.get_columns(TABLE)}
    if "api_key" not in columns:
        return 0
    with engine.connect() as conn:
        row = conn.execute(
            text(
                f"SELECT COUNT(*) FROM {TABLE} "
                f"WHERE COALESCE(api_key, '') <> '' AND api_key NOT LIKE :prefix"
            ),
            {"prefix": f"{API_KEY_PREFIX}%"},
        ).fetchone()
    return int(row[0]) if row else 0


def retire_api_key_env(engine: Engine) -> bool:
    """Drop the retired column; ``True`` when this call changed the schema.

    Inspects first, so a fresh database (``create_all`` never created the
    column) and every subsequent boot are no-ops.
    """
    inspector = inspect(engine)
    if TABLE not in set(inspector.get_table_names()):
        return False
    if RETIRED_COLUMN not in {column["name"] for column in inspector.get_columns(TABLE)}:
        return False

    if engine.dialect.name == "postgresql":
        with engine.begin() as conn:
            conn.execute(text(f"ALTER TABLE {TABLE} DROP COLUMN IF EXISTS {RETIRED_COLUMN}"))
    else:
        rebuild_table(engine, TABLE)
    logger.warning(
        "Migrated the model-route table: dropped %s.%s (a route's key is now "
        "sealed into api_key; set it with `cli.py model-route set-key`)",
        TABLE,
        RETIRED_COLUMN,
    )
    return True


def ensure_schema(engine: Engine) -> dict[str, object]:
    """Bring *engine*'s ``model_routes`` schema up to date; idempotent.

    Returns what this call found and changed::

        {"dropped_columns": ["model_routes.api_key_env"], "plaintext_keys": 2}

    ``plaintext_keys`` is a standing count, not a change — it is reported on
    every call so a boot can warn about it.  Never raises because something is
    already absent: existence is decided by inspection.
    """
    report: dict[str, object] = {"dropped_columns": [], "plaintext_keys": 0}
    if retire_api_key_env(engine):
        report["dropped_columns"] = [f"{TABLE}.{RETIRED_COLUMN}"]
    report["plaintext_keys"] = plaintext_key_count(engine)
    if report["plaintext_keys"]:
        logger.warning(
            "%s model route(s) still store their upstream key in plaintext; "
            "set %s and run `cli.py model-route seal` to re-seal them",
            report["plaintext_keys"],
            MODEL_ROUTE_KEY_ENV,
        )
    return report


__all__ = [
    "MODEL_ROUTE_KEY_ENV",
    "RETIRED_COLUMN",
    "TABLE",
    "ensure_schema",
    "plaintext_key_count",
    "retire_api_key_env",
]
