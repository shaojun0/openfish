#!/usr/bin/env python
"""Gate: the database backend is chosen correctly, and the chosen one works.

Run from the backend directory (`backend/`)::

    python scripts/check_database.py
    python scripts/check_database.py --url postgresql+psycopg://… --yes

The application supports two backends behind one schema — SQLite (default) and
PostgreSQL (``DATABASE_URL``) — and this gate pins down the three things that
could silently rot:

1. **Selection.** ``resolve_database_url`` maps a bare path to SQLite, upgrades
   a bare ``postgresql://`` URL to the driver we ship, and leaves an explicit
   driver alone.  A regression here would quietly point the app at the wrong
   database.
2. **In-place migration.** A database created before the ``api_keys.user_id``
   column existed must gain that column and its index on the next boot.  The
   introspection used to be SQLite-only (``PRAGMA table_info``); it now goes
   through SQLAlchemy's Inspector so it also works on PostgreSQL, and this check
   builds a legacy-shaped SQLite file to prove the generic path still migrates.
3. **A full round trip on the target.**  With no arguments the target is a
   throwaway SQLite file, so this is an offline gate like the others.  Pass
   ``--url`` (plus ``--yes``, because it writes) to run the same round trip —
   bootstrap, user, role, API key, statistics — against a real PostgreSQL
   server.

The remote form is deliberately guarded: the gate boots the real app with
``ADMIN_USERS=root`` and writes a test account, so it must never be pointed at
a production database by accident.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
os.chdir(REPO_ROOT)

_TMP_DIR = Path(tempfile.mkdtemp(prefix="cpypi-db-gate-"))
_TMP_SQLITE = _TMP_DIR / "gate.db"
_LEGACY_SQLITE = _TMP_DIR / "legacy.db"

failures: list[str] = []


def check(ok: bool, label: str) -> None:
    print(("   ✅ " if ok else "   ✗ ") + label)
    if not ok:
        failures.append(label)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--url", default=None,
        help=(
            "Database to test: a SQLAlchemy URL. Defaults to a throwaway SQLite "
            "file. A non-SQLite URL requires --yes."
        ),
    )
    parser.add_argument(
        "--yes", action="store_true",
        help="Confirm that a non-SQLite target may be written to (it will be seeded).",
    )
    return parser.parse_args()


def _configure_environment(args: argparse.Namespace) -> str:
    """Point the app at the target *before* ``config`` is imported."""
    if args.url:
        if not args.url.startswith("sqlite") and not args.yes:
            print(
                f"refusing to write to non-SQLite target {args.url!r} without --yes.\n"
                "This gate seeds a superuser and creates a test account.",
                file=sys.stderr,
            )
            raise SystemExit(2)
        os.environ["DATABASE_URL"] = args.url
        os.environ.pop("API_KEYS_FILE", None)
    else:
        os.environ.pop("DATABASE_URL", None)
        os.environ["API_KEYS_FILE"] = str(_TMP_SQLITE)

    # Deterministic, isolated app configuration — same baseline as check_rbac.
    os.environ["AUTH_USERNAME"] = "root"
    os.environ["AUTH_ASSERT"] = "database-gate-secret"
    os.environ["OAUTH2_INTROSPECT_URL"] = ""
    os.environ["OAUTH2_AUTHORIZE_URL"] = ""
    os.environ["ADMIN_USERS"] = '["root"]'
    return args.url or f"sqlite:///{_TMP_SQLITE}"


_ARGS = _parse_args()
TARGET_URL = _configure_environment(_ARGS)

from sqlalchemy import inspect  # noqa: E402

from app import app  # noqa: E402
from extensions.database import (  # noqa: E402
    describe_database,
    init_engine,
    resolve_database_url,
)
from models.user import User  # noqa: E402
from services.authz import bootstrap  # noqa: E402


def _dialect_of(url: str) -> str:
    return "sqlite" if url.startswith("sqlite") else "postgresql"


# ── 1. URL selection ─────────────────────────────────────────────────

def check_resolution() -> None:
    print("── URL selection ───────────────────────────────────────────────")
    cases = (
        # No password appears in these fixtures: only the scheme rewrite is
        # under test, and a `user:secret@` string in a source file is a
        # credential-shaped literal to every reader and scanner.
        ("postgres://u@h:5432/d", "postgresql+psycopg://u@h:5432/d"),
        ("postgresql://u@h:5432/d", "postgresql+psycopg://u@h:5432/d"),
        # An explicit driver is respected: we ship psycopg, but must not
        # silently rewrite somebody who installed psycopg2 / pg8000 on purpose.
        ("postgresql+psycopg2://u@h/d", "postgresql+psycopg2://u@h/d"),
        ("sqlite:////var/lib/openfish.db", "sqlite:////var/lib/openfish.db"),
        # Backward compatibility: cli.py --db <path> must still mean SQLite.
        ("/tmp/legacy.db", "sqlite:////tmp/legacy.db"),
    )
    for raw, expected in cases:
        check(resolve_database_url(raw) == expected,
              f"resolve({raw!r}) → {expected}")

    check(_dialect_of(resolve_database_url()) == _dialect_of(TARGET_URL),
          "the configured target resolves to the expected dialect")


# ── 2. Legacy database migration ─────────────────────────────────────

def check_light_migration() -> None:
    print()
    print("── Migration of a pre-existing database ────────────────────────")
    # The shape of api_keys before `user_id` existed.  create_all() leaves an
    # existing table alone, so only _apply_light_migrations can fix it.
    conn = sqlite3.connect(_LEGACY_SQLITE)
    conn.executescript(
        """
        CREATE TABLE api_keys (
            id VARCHAR(32) NOT NULL PRIMARY KEY,
            name VARCHAR(128) NOT NULL,
            prefix VARCHAR(16) NOT NULL,
            hash VARCHAR(64) NOT NULL,
            created_by VARCHAR(256) NOT NULL,
            created_at VARCHAR(32) NOT NULL,
            expires_at VARCHAR(32),
            last_used VARCHAR(32)
        );
        """
    )
    conn.commit()
    conn.close()

    engine = init_engine(f"sqlite:///{_LEGACY_SQLITE}")
    inspector = inspect(engine)
    columns = {c["name"] for c in inspector.get_columns("api_keys")}
    check("user_id" in columns, "a legacy api_keys table gains the user_id column")
    indexes = {i["name"] for i in inspector.get_indexes("api_keys")}
    check("ix_api_keys_user_id" in indexes, "the matching index is created too")
    check(
        {t for t in inspector.get_table_names()} >= {"users", "roles", "permissions"},
        "the new RBAC tables are created alongside the old ones",
    )
    engine.dispose()

    # A second boot must be a no-op, not a duplicate-column error.
    init_engine(f"sqlite:///{_LEGACY_SQLITE}").dispose()
    check(True, "running the migration a second time is harmless")


# ── 3. Round trip on the target backend ──────────────────────────────

def check_target_backend() -> None:
    expected = _dialect_of(TARGET_URL)
    print()
    print(f"── Round trip on {expected} ─────────────────────────────────────")

    info = app.extensions["database_info"]
    check(info["dialect"] == expected,
          f"the engine is bound to {expected} (got {info})")
    check(describe_database(app.extensions["db_engine"]) == info,
          "the /health descriptor matches the bound engine")

    health = app.test_client().get("/health").get_json()
    check(health["database"]["dialect"] == expected,
          "/health reports the active backend")

    authz = app.extensions["authz"]
    check(authz.count_superusers() == 1,
          f"exactly one superuser bootstrapped (got {authz.count_superusers()})")
    bootstrap(authz, admin_users=["root"])
    check(authz.count_superusers() == 1,
          "re-running bootstrap is inert — the seed is one-shot")

    # Users + roles: the RBAC tables on this backend.
    user = authz.provision_user("gate", "db-gate-user", display_name="DB Gate")
    check(user is not None and authz.find_user("db-gate-user") is not None,
          "an account round-trips through the users table")
    role = authz.create_role("db-gate-role", "DB Gate")
    check(authz.grant_role(user.id, "db-gate-role"),
          "granting a role writes to user_roles")
    check("db-gate-role" in authz.role_codes(user.id),
          "the grant reads back")
    check(authz.revoke_role(user.id, "db-gate-role"),
          "revoking the role deletes the user_roles row")

    # API keys + statistics: the two tables with hand-written DDL / counters.
    key_mgr = app.extensions["api_key_manager"]
    minted = key_mgr.create_key("db-gate-key", "db-gate-user", user_id=user.id)
    validated = key_mgr.validate(minted["key"])
    check(validated is not None and validated["user_id"] == user.id,
          "an API key is created, hashed and validated")
    key_mgr.record_download(minted["id"], "demo-pkg")
    key_mgr.record_download(minted["id"], "demo-pkg")
    key_mgr.record_upload(minted["id"], "demo-pkg")
    stats = key_mgr.get_key_stats(minted["id"])
    check(stats["total_downloads"] == 2 and stats["total_uploads"] == 1,
          f"usage counters increment (got {stats['total_downloads']} / "
          f"{stats['total_uploads']})")
    check(key_mgr.delete_key(minted["id"]),
          "deleting the key cascades to its statistics")

    # Leave the scratch database as we found it.
    authz.delete_role(role["id"])
    _delete_user(authz, "db-gate-user")


def _delete_user(authz, external_id: str) -> None:
    session = authz._s
    try:
        session.query(User).filter(User.external_id == external_id).delete()
        session.commit()
    finally:
        session.close()


def main() -> int:
    print(f"Target: {TARGET_URL}")
    print()
    check_resolution()
    check_light_migration()
    check_target_backend()

    print()
    if failures:
        print(f"❌ database check FAILED — {len(failures)} problem(s)")
        for item in failures:
            print("   " + item)
        return 1
    print("✅ database check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
