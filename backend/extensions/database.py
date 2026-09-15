"""Database extension — engine, session, and the auth services.

Engine selection
----------------
The schema in ``models/`` is portable SQLAlchemy, so exactly the same code runs
on both supported backends, chosen once by URL scheme:

* **SQLite** (the default) — one file at ``API_KEYS_FILE``, opened in WAL mode.
  This is the historical layout, and ``cli.py --db <file>`` keeps targeting it.
* **PostgreSQL** — any ``postgresql://…`` URL in ``DATABASE_URL``.  The users,
  roles, permissions, API keys and usage statistics move to the server in one
  piece; no table stays behind in the old file.

There is deliberately no per-table split between the two.  "Delete this API
key" and "delete the account that owns it" have to be one transaction; a
SQLite key store plus a PostgreSQL role store could not roll each other back.

Also exposes :func:`init_engine`, which the CLI reuses so `cli.py` can talk to
the same database without spinning up a Flask app, watchdog indexes or the
stats-refresh background thread.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from sqlalchemy import create_engine, event, inspect
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import scoped_session, sessionmaker

from config import settings
from extensions import Extension
from auth.api_keys import ApiKeyManager
from services.authz import AuthzService

# Importing the package is what registers every table on Base.metadata —
# create_all() only creates what has been imported.
import models  # noqa: F401
from models.base import Base

logger = logging.getLogger("cpypiserver.database")

#: ``expire_on_commit=False`` matters: guards detach ORM objects (expunge) and
#: then read their attributes outside the session.  With the default
#: expire-on-commit those reads would raise DetachedInstanceError.
Session = scoped_session(sessionmaker(expire_on_commit=False))

# ── Network-database tuning ──────────────────────────────────────────
# Only consulted for a non-SQLite URL.  ``wait`` is opt-in because the CLI
# should fail immediately on a typo, while the backend container should ride
# out a PostgreSQL container that is still booting.
_CONNECT_ATTEMPTS = 30
_CONNECT_RETRY_DELAY_SECONDS = 2.0
#: libpq's own per-attempt timeout, so one attempt cannot hang forever.
_CONNECT_TIMEOUT_SECONDS = 10
#: Drop pooled connections before an idle-timeout firewall or PgBouncer does.
_POOL_RECYCLE_SECONDS = 1800
#: Drivers that accept libpq's ``connect_timeout`` keyword.
_LIBPQ_DRIVERS = frozenset({
    "postgresql+psycopg", "postgresql+psycopg2", "postgresql+psycopg2cffi",
})


# ── URL resolution ───────────────────────────────────────────────────

def resolve_database_url(raw: str | None = None) -> str:
    """Turn a configured value into a SQLAlchemy URL.

    * ``None`` / empty        → SQLite at ``API_KEYS_FILE`` (the default).
    * ``sqlite:///…``         → used as is.
    * ``postgresql://…``      → rewritten to the psycopg (v3) driver we ship.
      An explicit ``+psycopg2`` / ``+pg8000`` is left alone, for an operator
      who installed a different driver on purpose.
    * a bare filesystem path  → SQLite, so ``cli.py --db data/x.db`` keeps
      working exactly as it always did.
    """
    configured = raw if raw is not None else settings.storage.database_url
    value = (configured or "").strip()
    if not value:
        return f"sqlite:///{settings.storage.api_keys_file}"
    if "://" not in value:
        return f"sqlite:///{value}"
    if value.startswith("postgres://"):
        return "postgresql+psycopg://" + value[len("postgres://"):]
    if value.startswith("postgresql://"):
        return "postgresql+psycopg://" + value[len("postgresql://"):]
    return value


def describe_database(engine: Engine) -> dict[str, str]:
    """Non-sensitive identity of the bound backend, for ``/health`` and logs.

    Deliberately omits host, user, database name and password: ``/health`` is
    anonymous, and "which engine am I on?" is the only operational question
    this needs to answer.
    """
    return {
        "dialect": engine.dialect.name,
        "driver": engine.dialect.driver,
    }


def _safe_url(url: str) -> str:
    """The URL with any password replaced — safe to log."""
    return make_url(url).render_as_string(hide_password=True)


# ── Engines ──────────────────────────────────────────────────────────

def _create_sqlite_engine(url: str, *, echo: bool) -> Engine:
    database = make_url(url).database
    if database and database != ":memory:":
        Path(database).parent.mkdir(parents=True, exist_ok=True)

    engine = create_engine(
        url,
        echo=echo,
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def _set_pragma(dbapi_conn, _record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL;")
        cursor.execute("PRAGMA foreign_keys=ON;")
        cursor.close()

    return engine


def _create_server_engine(url: str, *, echo: bool) -> Engine:
    connect_args: dict = {}
    if make_url(url).drivername in _LIBPQ_DRIVERS:
        connect_args["connect_timeout"] = _CONNECT_TIMEOUT_SECONDS
    # pool_pre_ping: a PostgreSQL connection can be killed by an idle timeout or
    # a database restart between requests; without this the next request fails
    # on a dead socket instead of transparently reconnecting.
    return create_engine(
        url,
        echo=echo,
        pool_pre_ping=True,
        pool_recycle=_POOL_RECYCLE_SECONDS,
        connect_args=connect_args,
    )


def _wait_for_database(engine: Engine) -> None:
    """Block until the server answers ``SELECT 1``, or re-raise the last error."""
    for attempt in range(1, _CONNECT_ATTEMPTS + 1):
        try:
            with engine.connect() as conn:
                conn.exec_driver_sql("SELECT 1")
            return
        except OperationalError:
            if attempt >= _CONNECT_ATTEMPTS:
                raise
            logger.warning(
                "Database not reachable yet (attempt %d/%d) — retrying in %.1fs",
                attempt, _CONNECT_ATTEMPTS, _CONNECT_RETRY_DELAY_SECONDS,
            )
            time.sleep(_CONNECT_RETRY_DELAY_SECONDS)


def init_engine(
    database_url: str | None = None,
    *,
    echo: bool = False,
    wait: bool = False,
) -> Engine:
    """Create the engine, create missing tables, run the light migrations.

    Safe to call repeatedly: ``create_all`` is a no-op for existing tables and
    :func:`_apply_light_migrations` is idempotent.  ``wait`` adds a bounded
    connection-retry loop for network databases — that is what lets the backend
    container come up against a PostgreSQL container that is still booting
    (Compose has no ordering guarantee without an explicit ``depends_on``).
    """
    url = resolve_database_url(database_url)
    is_sqlite = url.startswith("sqlite")

    if is_sqlite:
        engine = _create_sqlite_engine(url, echo=echo)
    else:
        engine = _create_server_engine(url, echo=echo)
        if wait:
            _wait_for_database(engine)

    Base.metadata.create_all(engine)
    _apply_light_migrations(engine)
    logger.info("Database ready: %s", _safe_url(url))
    return engine


# ── Lightweight migrations ───────────────────────────────────────────
# create_all() creates missing *tables* but never alters an existing one.  The
# deployments this project serves are a single SQLite file or a single
# PostgreSQL database, so a few idempotent ALTERs are the right trade for
# keeping the "read it in an afternoon" promise.  Column introspection goes
# through SQLAlchemy's Inspector rather than ``PRAGMA table_info`` so the same
# code path works on both backends.  If migrations ever get more involved than
# this, move to Alembic.

_MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    # (table, column, column DDL)
    ("api_keys", "user_id",
     "user_id INTEGER REFERENCES users(id) ON DELETE SET NULL"),
)

_INDEXES: tuple[tuple[str, str, str], ...] = (
    # (index name, table, column)
    ("ix_api_keys_user_id", "api_keys", "user_id"),
)


def _apply_light_migrations(engine: Engine) -> None:
    """Add columns and indexes that postdate a pre-existing database."""
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    with engine.begin() as conn:
        for table, column, ddl in _MIGRATIONS:
            if table not in tables:
                continue  # table does not exist yet — create_all handled it
            if column not in {c["name"] for c in inspector.get_columns(table)}:
                conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {ddl}")
                logger.warning("Migrated existing database: added %s.%s", table, column)

        for name, table, column in _INDEXES:
            if table not in tables:
                continue
            # Supported by both SQLite (>= 3.8) and PostgreSQL (>= 9.5).
            conn.exec_driver_sql(
                f"CREATE INDEX IF NOT EXISTS {name} ON {table}({column})"
            )


class DatabaseExtension(Extension):
    name = "database"
    dependencies: list[str] = []

    def init_app(self, app) -> None:
        engine = init_engine(
            settings.storage.database_url,
            echo=settings.server.debug,
            wait=True,
        )
        Session.configure(bind=engine)

        app.extensions["db_engine"] = engine
        app.extensions["database_info"] = describe_database(engine)
        app.extensions["api_key_manager"] = ApiKeyManager(Session)
        app.extensions["authz"] = AuthzService(Session)

        # NOTE: authorization seeding is deliberately *not* done here.  Route
        # guards declare permission points at import time, and blueprints are
        # imported after extensions are initialised — so the catalog is only
        # complete once register_all(app) has run.  See app.py.

        @app.teardown_appcontext
        def _shutdown(_exc=None):
            Session.remove()


def get_authz() -> AuthzService:
    """AuthzService bound to the process-wide session factory (CLI-friendly)."""
    return AuthzService(Session)
