"""Idempotent schema top-up for the ``model_routes`` table — no Alembic here either.

``extensions.database`` calls ``Base.metadata.create_all()`` at boot, which
creates missing *tables* but never alters an existing one.  This module is the
same idea as :mod:`models.agent_hub_migrate`, scoped to the one table that
module does not own.

Two changes need it:

``api_key_env`` was **retired** — a route used to be able to name an environment
variable its key was read from, and that column is ``NOT NULL`` with no server
default, so a database created before the change rejects every insert the
application now makes (the ORM no longer supplies a value).  Dropping it is
therefore not cosmetic — it is what keeps an upgraded deployment writable.

* **PostgreSQL** — ``ALTER TABLE … DROP COLUMN IF EXISTS``, the cheap path.
* **SQLite** — a table rebuild, because SQLite cannot drop a column in place.
  :func:`models.table_rebuild.rebuild_table` is the project's one implementation
  of the documented procedure.

The retired column held an environment-variable *name*, never a secret, so
nothing is lost when the values go — but an operator who was relying on it must
now store the key itself (``cli.py model-route set-key``), which is the whole
point of the change.

The ``mineru`` provider was **retired** for a different reason: it is not a wire
format of its own, since a MinerU endpoint answers the OpenAI format.  A database
written before that decision holds rows the schema now refuses
(``ck_model_routes_provider`` is generated from
:data:`models.model_route.PROVIDERS`), so :func:`retire_mineru_provider` rewrites
them to ``openai`` — keeping their ``base_url``, ``model``, sealed key and
``kind``, because the route is still a working endpoint — and then replaces the
constraint that admitted the retired spelling.  Only a ``path`` still pointing at
the retired protocol's own default is cleared, so the OpenAI default applies
instead of a dead endpoint being probed.

:func:`ensure_schema` also *reports* rows whose ``api_key`` is still plaintext.
It deliberately does **not** re-seal them: sealing needs the master key and
writes a secret, so it is an explicit operator action (``cli.py model-route
seal``), not a side effect of booting.  Counting them needs no key, which is why
the visibility can live here while the rewrite does not.
"""

from __future__ import annotations


from sqlalchemy import CheckConstraint, inspect, text
from sqlalchemy.engine import Engine

from config.keys import KeysConfig

from .base import in_check
from .model_route import API_KEY_PREFIX, PROVIDERS, ModelRoute
from .table_rebuild import columns_present, rebuild_table


#: The column this module retires.  Kept as a constant so the docstring, the
#: DDL and the log line cannot drift apart.
RETIRED_COLUMN = "api_key_env"

#: The table this module owns.
TABLE = ModelRoute.__tablename__

#: The provider value this module retires, and the one it is rewritten to.  A
#: MinerU endpoint speaks the OpenAI format, so the row survives the change.
RETIRED_PROVIDER = "mineru"
PROVIDER_REPLACEMENT = "openai"

#: The endpoint path the retired provider defaulted to.  A route that still names
#: it is pointing at the old protocol's own shape, so the path is cleared and the
#: replacement protocol's default is resolved on read instead.
RETIRED_PROVIDER_PATH = "/file_parse"

#: The variable an operator is told to set, derived from the config model that
#: owns it so the name in the log cannot drift from the name that is read.
MODEL_ROUTE_KEY_ENV = KeysConfig.env_name("model_route_key")


def provider_check_name() -> str:
    """The name of the CHECK constraint listing the wire formats.

    Read from the model rather than spelled out, for the same reason the
    constraint's body is generated from :data:`models.model_route.PROVIDERS`: a
    rename in one place must not leave a PostgreSQL deployment enforcing the old
    vocabulary.
    """
    for constraint in ModelRoute.__table__.constraints:
        if isinstance(constraint, CheckConstraint) and "provider" in str(constraint.sqltext):
            return str(constraint.name)
    raise RuntimeError(f"{TABLE} declares no provider CHECK constraint")


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
    return True


def _provider_check_is_stale(engine: Engine) -> bool:
    """Whether the table's CHECK constraints still admit a retired provider.

    Dialect-independent on purpose: SQLite and PostgreSQL both reflect a named
    CHECK with its body as text, and the body is generated from the same
    :data:`models.model_route.PROVIDERS` tuple, so searching that text for the
    retired value is the one test that works on both — and the one that stays
    correct when a *different* provider is retired later.
    """
    inspector = inspect(engine)
    if TABLE not in set(inspector.get_table_names()):
        return False
    for constraint in inspector.get_check_constraints(TABLE):
        body = str(constraint.get("sqltext") or "").lower()
        if RETIRED_PROVIDER in body:
            return True
    return False


def retire_mineru_provider(engine: Engine) -> dict[str, object]:
    """Rewrite ``mineru`` routes to ``openai`` and refresh the provider CHECK.

    Returns ``{"rewritten": [names…], "constraint": "rebuilt" | "recreated" |
    None}``.  Idempotent: a table with no such row and a current constraint is
    left untouched.

    The rows are rewritten **before** the constraint tightens — otherwise the
    rebuild or the ``ALTER`` would fail on the very rows it is meant to migrate.
    Nothing but the provider is changed, because the endpoint itself still works:
    base URL, model, sealed key, aliases, ``kind`` (a MinerU route is ``ocr``) and
    enabled all carry over.  Only a ``path`` still naming the retired protocol's
    own default is cleared, since that endpoint does not exist on an OpenAI-format
    server; an administrator's own path is left exactly as written.
    """
    inspector = inspect(engine)
    if TABLE not in set(inspector.get_table_names()):
        return {"rewritten": [], "constraint": None}

    with engine.begin() as conn:
        names = [
            str(row[0])
            for row in conn.execute(
                text(f"SELECT name FROM {TABLE} WHERE LOWER(provider) = :retired"),
                {"retired": RETIRED_PROVIDER},
            ).fetchall()
        ]
        if names:
            conn.execute(
                text(
                    f"UPDATE {TABLE} SET path = '' "
                    f"WHERE LOWER(provider) = :retired AND path = :path"
                ),
                {"retired": RETIRED_PROVIDER, "path": RETIRED_PROVIDER_PATH},
            )
            conn.execute(
                text(f"UPDATE {TABLE} SET provider = :replacement WHERE LOWER(provider) = :retired"),
                {"replacement": PROVIDER_REPLACEMENT, "retired": RETIRED_PROVIDER},
            )

    changed: str | None = None
    if _provider_check_is_stale(engine):
        constraint = provider_check_name()
        if engine.dialect.name == "postgresql":
            with engine.begin() as conn:
                conn.execute(text(f"ALTER TABLE {TABLE} DROP CONSTRAINT IF EXISTS {constraint}"))
                conn.execute(
                    text(
                        f"ALTER TABLE {TABLE} ADD CONSTRAINT {constraint} "
                        f"CHECK ({in_check('provider', PROVIDERS)})"
                    )
                )
            changed = "recreated"
        else:
            # SQLite cannot alter a CHECK in place; the rebuild applies the model's
            # current definition (and, in passing, any other pending column change).
            rebuild_table(engine, TABLE)
            changed = "rebuilt"

    if names:
        pass
    return {"rewritten": names, "constraint": changed}


def ensure_schema(engine: Engine) -> dict[str, object]:
    """Bring *engine*'s ``model_routes`` schema up to date; idempotent.

    Returns what this call found and changed::

        {
            "rewritten_routes": ["mineru-ocr"],
            "dropped_columns": ["model_routes.api_key_env"],
            "plaintext_keys": 2,
        }

    ``plaintext_keys`` is a standing count, not a change — it is reported on
    every call so a boot can warn about it.  ``dropped_columns`` is measured
    rather than taken from the step that did it, because on SQLite the provider
    rebuild and the column drop are the *same* rebuild: whichever runs second
    finds its work already done, and the report must still name what changed.
    Never raises because something is already absent: existence is decided by
    inspection.
    """
    columns_before = columns_present(engine, TABLE)
    report: dict[str, object] = {
        "rewritten_routes": [],
        "dropped_columns": [],
        "plaintext_keys": 0,
    }
    retired = retire_mineru_provider(engine)
    report["rewritten_routes"] = retired["rewritten"]
    dropped_by_its_own_step = retire_api_key_env(engine)
    report["dropped_columns"] = [
        f"{TABLE}.{name}" for name in sorted(columns_before - columns_present(engine, TABLE))
    ]
    if report["dropped_columns"] and not dropped_by_its_own_step:
        pass
    report["plaintext_keys"] = plaintext_key_count(engine)
    if report["plaintext_keys"]:
        pass
    return report


__all__ = [
    "MODEL_ROUTE_KEY_ENV",
    "PROVIDER_REPLACEMENT",
    "RETIRED_COLUMN",
    "RETIRED_PROVIDER",
    "RETIRED_PROVIDER_PATH",
    "TABLE",
    "ensure_schema",
    "plaintext_key_count",
    "provider_check_name",
    "retire_api_key_env",
    "retire_mineru_provider",
]
