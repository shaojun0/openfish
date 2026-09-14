"""Database extension — SQLAlchemy engine, session, and ApiKeyManager."""

from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import scoped_session, sessionmaker

from config import settings
from extensions import Extension
from auth.api_keys import ApiKeyManager
from models.base import Base

Session = scoped_session(sessionmaker())


class DatabaseExtension(Extension):
    name = "database"
    dependencies: list[str] = []

    def init_app(self, app) -> None:
        db_path = settings.storage.api_keys_file
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)

        engine = create_engine(
            f"sqlite:///{db_path}",
            echo=settings.server.debug,
            connect_args={"check_same_thread": False},
        )

        @event.listens_for(engine, "connect")
        def _set_pragma(dbapi_conn, _record):
            c = dbapi_conn.cursor()
            c.execute("PRAGMA journal_mode=WAL;")
            c.execute("PRAGMA foreign_keys=ON;")
            c.close()

        Session.configure(bind=engine)
        Base.metadata.create_all(engine)

        app.extensions["api_key_manager"] = ApiKeyManager(Session)

        @app.teardown_appcontext
        def _shutdown(_exc=None):
            Session.remove()
