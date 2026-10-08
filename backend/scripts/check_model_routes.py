#!/usr/bin/env python
"""Gate: a route's upstream key is sealed at rest, and never silently degraded.

Run from the backend directory (`backend/`)::

    python scripts/check_model_routes.py

``model_routes.api_key`` used to hold a live upstream credential in a plain
column, or an environment-variable *name* that made one row's behaviour depend
on the deploying process.  Both are gone: the key is sealed with Fernet under
``MODEL_ROUTE_KEY`` and stored as an ``enc:v1:`` envelope.  This gate pins that
contract, and every way it is allowed to fail:

1. a sealed key round-trips — what the API publishes for ``/models/resolved`` is
   the plaintext, and what the *database* holds is not;
2. sealing **fails closed** when no master key is configured: the save is refused
   rather than storing plaintext, and a route with no key can still be saved;
3. a row written before sealing existed is still served, and reports
   ``api_key_source: "plaintext"`` so the operator knows to seal it;
4. an envelope sealed under a *different* key reports ``api_key_source:
   "unreadable"`` and yields no key — never a partial or guessed one;
5. ``migrate_plaintext_keys`` re-seals exactly the legacy rows, is idempotent,
   and leaves already-sealed rows byte-identical (no pointless re-encryption);
6. the retired ``api_key_env`` column is gone from the schema, and an existing
   database that still has it is migrated without losing rows.

It talks to a throwaway SQLite database and **never boots Flask**, so it is also
the one model-route gate that runs on a Windows developer box — where
``libmagic`` is absent and every app-booting gate dies at import.
"""

from __future__ import annotations

import os
import secrets
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
os.chdir(REPO_ROOT)

#: The master key the fixture rows are sealed under.  Literal on purpose: it is
#: a fixture, never a deployment value.  Set before ``config`` is imported —
#: ``config.settings`` reads the environment once and nothing re-reads it.
TEST_MASTER_KEY = "gate-only-model-route-master-key"
os.environ["MODEL_ROUTE_KEY"] = TEST_MASTER_KEY

import sqlite3  # noqa: E402

from sqlalchemy import create_engine, inspect, text  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from config import settings  # noqa: E402
from config.keys import KeysConfig  # noqa: E402
from models.base import Base  # noqa: E402
from models.model_route import API_KEY_PREFIX, ModelRoute  # noqa: E402
from models.model_route_migrate import (  # noqa: E402
    ensure_schema,
    plaintext_key_count,
    retire_api_key_env,
)
from services import model_routes  # noqa: E402
from services.sealing import SealingKeyMissing, SecretSealer  # noqa: E402

#: Generated so no literal credential sits in the tree; a scanner cannot tell a
#: fixture from a real key.
UPSTREAM_KEY = "sk-" + secrets.token_urlsafe(24)
OTHER_KEY = "sk-" + secrets.token_urlsafe(24)
FOREIGN_MASTER_KEY = "some-other-deployments-master-key"

CHECKS = 0
FAILURES: list[str] = []


def check(condition: bool, label: str, detail: str = "") -> bool:
    global CHECKS
    CHECKS += 1
    if condition:
        print(f"   ✅ {label}")
        return True
    message = f"{label}{(' — ' + detail) if detail else ''}"
    FAILURES.append(message)
    print(f"   ❌ {message}")
    return False


def section(title: str) -> None:
    print()
    print(f"── {title} " + "─" * max(0, 62 - len(title)))


def _fresh_engine(name: str):
    """A throwaway SQLite engine with the application schema on it."""
    path = Path(tempfile.mkdtemp(prefix="cpypi-model-routes-")) / f"{name}.db"
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    return engine


def _open(engine):
    """A session bound to *engine*.

    Deliberately not ``extensions.database.Session``: that is a module-level
    scoped factory, and ``configure()`` cannot re-point a scoped session that
    already exists — so a gate that walked several throwaway databases would
    silently keep reading the first one.  Every ``services.model_routes`` entry
    point takes the session as an argument, so each section simply gets its own.
    """
    return sessionmaker(bind=engine, expire_on_commit=False)()


def _seed_legacy_row(engine, name: str, api_key: str) -> None:
    """A row as a pre-sealing deployment stored it: bare plaintext key."""
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO model_routes "
                "(name, provider, kind, base_url, path, model, api_key, aliases, "
                " enabled, description, created_at, updated_at) "
                "VALUES (:name, 'openai', 'chat', 'https://api.example.invalid', "
                "'/v1/chat/completions', :model, :api_key, '[]', 1, "
                "'legacy fixture', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ),
            {"name": name, "model": f"{name}-model", "api_key": api_key},
        )


def _payload(name: str, **overrides) -> dict:
    payload = {
        "name": name,
        "provider": "openai",
        "base_url": "https://api.example.invalid",
        "description": "gate fixture",
        "aliases": [],
        **overrides,
    }
    return payload


def main() -> int:  # noqa: C901 - a gate is a linear list of assertions
    print(f"model-route key sealing — master key from {KeysConfig.env_name('model_route_key')}")

    section("1. a sealed key round-trips, and the row is not plaintext")
    check(
        set(model_routes.KEY_SOURCES) == {"stored", "plaintext", "unreadable", "none"},
        "the published api_key_source vocabulary is exactly what the docs promise",
    )
    engine = _fresh_engine("roundtrip")
    session = _open(engine)
    entry = model_routes.create(session, _payload("sealed", api_key=UPSTREAM_KEY))
    stored = str(entry["api_key"])
    check(
        stored.startswith(API_KEY_PREFIX),
        f"the stored value is an envelope ({stored[:12]}…)",
        )
    check(UPSTREAM_KEY not in stored, "the plaintext key does not appear in the row")
    with engine.connect() as conn:
        raw = conn.execute(text("SELECT api_key FROM model_routes")).scalar()
    check(UPSTREAM_KEY not in str(raw), "nor in the column as the database holds it")
    resolved = model_routes.resolve(session)["routes"]
    check(
        [r["api_key"] for r in resolved] == [UPSTREAM_KEY],
        "resolve() hands the client the decrypted key",
    )
    check(
        resolved[0]["api_key_source"] == "stored",
        "and reports api_key_source=stored",
    )
    public = model_routes.load(session)["routes"][0]
    check(public["api_key"] is None, "the browsing view still nulls api_key")
    check(
        public["api_key_hint"] == "••••" + UPSTREAM_KEY[-4:],
        "and publishes only the last-four hint",
    )
    session.close()

    section("2. sealing fails closed, and an empty key still saves")
    session = _open(engine)
    original = settings.keys.model_route_key
    settings.keys.model_route_key = ""
    try:
        try:
            model_routes.create(session, _payload("no-master-key", api_key=UPSTREAM_KEY))
            check(False, "saving a key without MODEL_ROUTE_KEY is refused")
        except SealingKeyMissing as exc:
            check(True, "saving a key without MODEL_ROUTE_KEY raises SealingKeyMissing")
            check(
                KeysConfig.env_name("model_route_key") in str(exc),
                "and the message names the variable to set",
            )
        check(
            model_routes.seal_api_key("") == "",
            "but a route with no key needs no master key",
        )
        keyless = model_routes.create(session, _payload("keyless"))
        check(keyless["api_key"] == "", "so a keyless route is stored with an empty key")
        check(
            model_routes.effective_api_key(keyless)[1] == "none",
            "and reports api_key_source=none",
        )
    finally:
        settings.keys.model_route_key = original
        session.close()

    section("3. a row written before sealing existed is served, and flagged")
    engine = _fresh_engine("legacy")
    _seed_legacy_row(engine, "legacy", OTHER_KEY)
    session = _open(engine)
    loaded = model_routes.load(session)["routes"]
    check(
        loaded[0]["api_key_source"] == "plaintext",
        "a bare key reports api_key_source=plaintext",
    )
    check(loaded[0]["has_api_key"] is True, "and still counts as configured")
    check(
        model_routes.effective_api_key(model_routes.raw_route(session, "legacy"))[0] == OTHER_KEY,
        "and the probe path still authenticates with it",
    )
    session.close()

    section("4. an envelope this deployment cannot open is not guessed at")
    engine = _fresh_engine("foreign")
    foreign = SecretSealer(FOREIGN_MASTER_KEY, prefix=API_KEY_PREFIX)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO model_routes "
                "(name, provider, kind, base_url, path, model, api_key, aliases, "
                " enabled, description, created_at, updated_at) "
                "VALUES ('foreign', 'openai', 'chat', 'https://api.example.invalid', "
                "'/v1/chat/completions', 'foreign-model', :api_key, '[]', 1, "
                "'foreign fixture', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ),
            {"api_key": foreign.seal(OTHER_KEY)},
        )
    session = _open(engine)
    row = model_routes.raw_route(session, "foreign")
    key, source = model_routes.effective_api_key(row)
    check(key == "", "a foreign envelope yields no key")
    check(source == "unreadable", "and reports api_key_source=unreadable")
    public = model_routes.load(session)["routes"][0]
    check(public["has_api_key"] is False, "so the console does not claim it is configured")
    session.close()

    section("5. re-sealing is complete, idempotent and non-churning")
    engine = _fresh_engine("migrate")
    _seed_legacy_row(engine, "legacy-a", UPSTREAM_KEY)
    _seed_legacy_row(engine, "legacy-b", OTHER_KEY)
    session = _open(engine)
    sealed_row = model_routes.create(session, _payload("already-sealed", api_key=OTHER_KEY))
    before = str(sealed_row["api_key"])
    check(plaintext_key_count(engine) == 2, "two rows are still plaintext before sealing")
    resealed = model_routes.migrate_plaintext_keys(session)
    check(sorted(resealed) == ["legacy-a", "legacy-b"], "both legacy rows were re-sealed")
    check(plaintext_key_count(engine) == 0, "no plaintext key remains")
    check(model_routes.migrate_plaintext_keys(session) == [], "a second run is a no-op")
    after = str(model_routes.raw_route(session, "already-sealed")["api_key"])
    check(after == before, "an already-sealed row is not re-encrypted")
    resolved = {r["name"]: r["api_key"] for r in model_routes.resolve(session)["routes"]}
    check(
        resolved == {"legacy-a": UPSTREAM_KEY, "legacy-b": OTHER_KEY, "already-sealed": OTHER_KEY},
        "every key still decrypts to what was stored",
    )
    session.close()

    section("6. the retired api_key_env column is gone, and is migrated away")
    check(
        "api_key_env" not in {c.name for c in ModelRoute.__table__.columns},
        "the model no longer declares api_key_env",
    )
    fresh = _fresh_engine("fresh")
    check(
        "api_key_env" not in {c["name"] for c in inspect(fresh).get_columns("model_routes")},
        "and a fresh database is created without it",
    )
    check(retire_api_key_env(fresh) is False, "so the migration is a no-op on a fresh database")

    # An *upgraded* deployment: the old column exists, NOT NULL and with no
    # server default — which is exactly what would reject every later insert.
    upgraded_path = Path(tempfile.mkdtemp(prefix="cpypi-model-routes-")) / "upgraded.db"
    con = sqlite3.connect(upgraded_path)
    con.executescript(
        """
        CREATE TABLE model_routes (
            id INTEGER NOT NULL PRIMARY KEY,
            name VARCHAR(128) NOT NULL,
            provider VARCHAR(32) NOT NULL,
            kind VARCHAR(32) NOT NULL,
            base_url VARCHAR(1024) NOT NULL,
            path VARCHAR(256) NOT NULL,
            model VARCHAR(256) NOT NULL,
            api_key VARCHAR(1024) NOT NULL,
            api_key_env VARCHAR(128) NOT NULL,
            aliases TEXT NOT NULL,
            enabled BOOLEAN NOT NULL,
            description VARCHAR(500) NOT NULL,
            health TEXT,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL
        );
        """
    )
    con.execute(
        "INSERT INTO model_routes (id, name, provider, kind, base_url, path, model,"
        " api_key, api_key_env, aliases, enabled, description, created_at, updated_at)"
        " VALUES (1, 'upgraded', 'openai', 'chat', 'https://api.example.invalid',"
        " '/v1/chat/completions', 'm', :key, 'SOME_OLD_VAR', '[]', 1, 'd',"
        " CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
        {"key": UPSTREAM_KEY},
    )
    con.commit()
    con.close()

    upgraded = create_engine(f"sqlite:///{upgraded_path}")
    report = ensure_schema(upgraded)
    check(
        report["dropped_columns"] == ["model_routes.api_key_env"],
        "an upgraded database reports the column it retired",
    )
    columns = {c["name"] for c in inspect(upgraded).get_columns("model_routes")}
    check("api_key_env" not in columns, "and the column is gone from the table")
    with upgraded.connect() as conn:
        kept = conn.execute(text("SELECT name, api_key FROM model_routes")).fetchall()
    check(
        [tuple(r) for r in kept] == [("upgraded", UPSTREAM_KEY)],
        "the existing row survived the rebuild, key and all",
    )
    check(
        report["plaintext_keys"] == 1,
        "and its pre-sealing key is reported as needing `model-route seal`",
    )
    # The point of the migration: the table must accept the inserts the
    # application now makes, which omit the retired NOT NULL column entirely.
    check(
        ensure_schema(upgraded)["dropped_columns"] == [],
        "re-running the migration changes nothing",
    )

    print()
    if FAILURES:
        print(f"❌ {len(FAILURES)} of {CHECKS} checks failed")
        for item in FAILURES:
            print(f"   · {item}")
        return 1
    print(f"✅ {CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
