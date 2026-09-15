"""Database extension — SQLAlchemy engine, session, and the auth services.

Also exposes :func:`init_engine`, which the CLI reuses so `cli.py` can talk to
the same database without spinning up a Flask app, watchdog indexes or the
stats-refresh background thread.
"""

from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
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

# ``expire_on_commit=False`` matters: guards detach ORM objects (expunge) and
# then read their attributes outside the session.  With the default
# expire-on-commit those reads would raise DetachedInstanceError.
Session = scoped_session(sessionmaker(expire_on_commit=False))


def init_engine(db_path: str | None = None, *, echo: bool = False) -> Engine:
    """Create the SQLite engine, enable the pragmas we rely on, create tables.

    Safe to call repeatedly: ``create_all`` is a no-op for existing tables and
    :func:`_apply_light_migrations` is idempotent.
    """
    db_path = db_path or settings.storage.api_keys_file
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    engine = create_engine(
        f"sqlite:///{db_path}",
        echo=echo,
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def _set_pragma(dbapi_conn, _record):
        c = dbapi_conn.cursor()
        c.execute("PRAGMA journal_mode=WAL;")
        c.execute("PRAGMA foreign_keys=ON;")
        c.close()

    Base.metadata.create_all(engine)
    _apply_light_migrations(engine)
    return engine


# ── Lightweight migrations ───────────────────────────────────────────
# create_all() creates missing *tables* but never alters an existing one.  This
# deployment is a single SQLite file, so a few idempotent ALTERs are the right
# trade for keeping the "read it in an afternoon" promise.  If migrations ever
# get more involved than this, move to Alembic.

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
    """Add columns and indexes that postdate a pre-existing database file."""
    with engine.begin() as conn:
        for table, column, ddl in _MIGRATIONS:
            cols = {row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table})")}
            if not cols:
                continue  # table does not exist yet — create_all handled it
            if column not in cols:
                conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {ddl}")
                logger.warning("Migrated existing database: added %s.%s", table, column)

        for name, table, column in _INDEXES:
            conn.exec_driver_sql(
                f"CREATE INDEX IF NOT EXISTS {name} ON {table}({column})"
            )


class DatabaseExtension(Extension):
    name = "database"
    dependencies: list[str] = []

    def init_app(self, app) -> None:
        engine = init_engine(settings.storage.api_keys_file, echo=settings.server.debug)
        Session.configure(bind=engine)

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
