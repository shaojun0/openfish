#!/usr/bin/env python
"""Gate: the git credential exchange works with no Forgejo, no Flask, no network.

Run from the backend directory (``backend/``)::

    python scripts/check_git_identity.py

This is the self-check for ``docs/agent-hub/integration/S6.md`` and
``DEVELOPMENT.md`` §13.1: ``git-receive-pack`` is authenticated by **Forgejo**,
so the platform must broker a *Forgejo* token per user instead of handing out an
openfish API key.  What this gate proves, against a throwaway SQLite file and a
fake Forgejo client:

1. **username derivation** is deterministic, collision-free, and restricted to
   ``[a-z0-9-]`` within 39 characters;
2. **``ensure`` is idempotent** — two calls create one Forgejo account and one
   row, and an account that already exists remotely is adopted, not duplicated;
3. **concurrency** — two threads doing a first ``ensure`` at the same time leave
   exactly one row and one remote account;
4. **token reuse and rotation** — an unexpired token is reused without touching
   Forgejo; an expired one mints a new token and revokes the old one;
5. **plaintext never lands** — the stored ciphertext is not the token (though it
   decrypts back to it), no log line contains the token, and ``to_dict()`` does
   not expose it;
6. **scopes** — a user ticket is ``read:repository`` or ``write:repository`` and
   never carries any ``admin`` scope;
7. **revoke** really calls Forgejo (token, and optionally the account) and the
   row is marked revoked, then revivable;
8. **a missing ``GIT_IDENTITY_KEY``** is a clear configuration error, not a bare
   ``TypeError`` and not a plaintext fallback.

Everything external is injected; the gate never opens a socket.
"""

from __future__ import annotations

import logging
import re
import sys
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.orm import scoped_session, sessionmaker  # noqa: E402

from models.agent_hub import GitIdentity  # noqa: E402
from models.agent_hub_migrate import ensure_schema  # noqa: E402
from models.base import Base  # noqa: E402
from models.user import User  # noqa: E402
from services.git_identity import (  # noqa: E402
    FORGEJO_TOKEN_NAME,
    GIT_IDENTITY_KEY_ENV,
    SCOPE_REPOSITORY_READ,
    SCOPE_REPOSITORY_WRITE,
    ForgejoIdentityClient,
    GitIdentityConfigError,
    GitIdentityService,
    TokenCipher,
    derive_username,
    synthetic_email,
)
from services.repo_import import ImportConfig  # noqa: E402

#: Counters for the summary line at the end.
CHECKS = 0
FAILURES: list[str] = []

#: The Fernet material the gate uses.  Any non-empty secret works — that is the
#: point of deriving the key (see ``TokenCipher``); a literal here is a test
#: fixture, never a deployment value.
TEST_KEY = "gate-only-git-identity-key"


def check(label: str, condition: bool, detail: str = "") -> bool:
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
    print(f"── {title} " + "─" * max(0, 60 - len(title)))


# ── Fakes and harness ────────────────────────────────────────────────

class FakeForgejo:
    """In-memory stand-in for :class:`ForgejoIdentityClient`.

    Mirrors the real client's contract (``ensure_user`` / ``create_token`` /
    ``delete_token`` / ``delete_user``) and records every mutation so the gate
    can assert *what the platform asked Forgejo to do*.  All mutation happens
    under one lock, exactly like the real server's uniqueness constraints.
    """

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.users: dict[str, dict[str, Any]] = {}
        self.tokens: dict[str, dict[str, str]] = {}
        self.create_user_calls = 0
        self.created_accounts: list[str] = []
        self.token_calls = 0
        self.requested_scopes: list[tuple[str, ...]] = []
        self.deleted_tokens: list[tuple[str, str]] = []
        self.deleted_users: list[str] = []

    @property
    def configured(self) -> bool:  # parity with the real client
        return True

    def ensure_user(
        self, username: str, *, email: str, display_name: str = "", password: str | None = None,
    ) -> dict[str, Any]:
        with self.lock:
            if username in self.users:
                return self.users[username]
            self.create_user_calls += 1
            user = {
                "id": len(self.users) + 1,
                "username": username,
                "email": email,
                "full_name": display_name,
            }
            self.users[username] = user
            self.created_accounts.append(username)
            return user

    def find_user(self, username: str) -> dict[str, Any] | None:
        with self.lock:
            return self.users.get(username)

    def create_token(
        self, username: str, *, name: str, scopes: tuple[str, ...] | list[str],
    ) -> dict[str, Any]:
        with self.lock:
            self.token_calls += 1
            self.requested_scopes.append(tuple(scopes))
            token = f"fake-forgejo-token-{username}-{self.token_calls:04d}"
            self.tokens.setdefault(username, {})[name] = token
            return {"id": self.token_calls, "name": name, "token": token}

    def delete_token(self, username: str, *, name: str) -> bool:
        with self.lock:
            self.deleted_tokens.append((username, name))
            return self.tokens.get(username, {}).pop(name, None) is not None

    def delete_user(self, username: str) -> bool:
        with self.lock:
            self.deleted_users.append(username)
            existed = self.users.pop(username, None) is not None
            self.tokens.pop(username, None)
            return existed


class Clock:
    """A settable UTC clock so expiry is deterministic, not a sleep."""

    def __init__(self) -> None:
        self.value = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, **kwargs: int) -> None:
        self.value = self.value + timedelta(**kwargs)


class Harness:
    """A throwaway SQLite database plus a scoped session and a fake Forgejo."""

    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="git-identity-gate-")
        self.engine = create_engine(
            f"sqlite:///{Path(self._tmp.name) / 'gate.db'}",
            connect_args={"check_same_thread": False, "timeout": 30},
        )
        # ``users`` first (git_identities cascades from it), then the agent-hub
        # schema the way a booting deployment builds it.
        Base.metadata.create_all(self.engine, tables=[User.__table__])
        ensure_schema(self.engine)
        self.Session = scoped_session(sessionmaker(bind=self.engine, expire_on_commit=False))
        self.client = FakeForgejo()
        self.clock = Clock()

    def user(self, external_id: str) -> int:
        session = self.Session()
        row = User(provider="local", external_id=external_id, display_name=external_id)
        session.add(row)
        session.commit()
        return int(row.id)

    def service(self, *, client: Any | None = None, env: dict[str, str] | None = None):
        return GitIdentityService(
            self.Session,
            client=client if client is not None else self.client,
            cipher=TokenCipher(TEST_KEY),
            env=env if env is not None else {GIT_IDENTITY_KEY_ENV: TEST_KEY},
            now=self.clock,
        )

    def rows(self) -> list[dict[str, Any]]:
        with self.engine.connect() as conn:
            result = conn.execute(text(
                "SELECT user_id, forgejo_username, external_id, token_ciphertext "
                "FROM git_identities ORDER BY user_id"
            ))
            return [dict(row._mapping) for row in result]

    def close(self) -> None:
        self.Session.remove()
        self.engine.dispose()
        self._tmp.cleanup()


# ── Scenarios ────────────────────────────────────────────────────────

def scenario_username() -> None:
    section("username derivation (frozen contract)")
    first = derive_username(7, "zhangsan")
    again = derive_username(7, "zhangsan")
    check("same (user_id, external_id) is deterministic", first == again, f"{first} != {again}")
    check("different users do not collide", derive_username(7, "zhangsan") != derive_username(8, "zhangsan"))
    check("different identities do not collide", derive_username(7, "a") != derive_username(7, "b"))
    check("charset is [a-z0-9-]", re.fullmatch(r"[a-z0-9-]+", first) is not None, first)
    check("length <= 39", len(first) <= 39, str(len(first)))
    check("a large user id stays within the limit",
          len(derive_username(2**40, "x" * 512)) <= 39, derive_username(2**40, "x"))
    check("the prefix is the documented one", first.startswith("of-7-"), first)


def scenario_schema() -> None:
    section("ensure_schema covers git_identities (idempotent)")
    with tempfile.TemporaryDirectory(prefix="git-identity-schema-") as tmp:
        engine = create_engine(f"sqlite:///{Path(tmp) / 'schema.db'}")
        Base.metadata.create_all(engine, tables=[User.__table__])
        report = ensure_schema(engine)
        with engine.connect() as conn:
            names = {row[0] for row in conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='table'")
            )}
        check("a fresh ensure_schema creates git_identities",
              GitIdentity.__tablename__ in names
              and GitIdentity.__tablename__ in report["created_tables"],
              str(report))
        second = ensure_schema(engine)
        check("a second ensure_schema changes nothing",
              not any(second.values()), str(second))
        engine.dispose()


def scenario_ensure_idempotent(harness: Harness) -> None:
    section("ensure is idempotent and adopts an existing Forgejo account")
    user_id = harness.user("idempotent")
    service = harness.service()

    row = service.ensure(user_id, "idempotent", display_name="张三", email="z@example.invalid")
    expected = derive_username(user_id, "idempotent")
    check("the row carries the derived username", row.forgejo_username == expected, row.forgejo_username)
    check("exactly one Forgejo account was created", len(harness.client.created_accounts) == 1)
    check("the creator call count is one", harness.client.create_user_calls == 1)

    again = service.ensure(user_id, "idempotent")
    check("a second ensure returns the same row", again.id == row.id)
    check("a second ensure does not call Forgejo again", harness.client.create_user_calls == 1)
    check("a second ensure writes no second row", len(harness.rows()) == 1, str(harness.rows()))

    # An account that already exists remotely (e.g. a crash between the remote
    # create and the local commit) is adopted, not duplicated.
    adopted_id = harness.user("adopted")
    adopted_name = derive_username(adopted_id, "adopted")
    harness.client.ensure_user(adopted_name, email=synthetic_email(adopted_name))
    adopted = service.ensure(adopted_id, "adopted")
    check("an existing remote account is adopted",
          adopted.forgejo_username == adopted_name and harness.client.create_user_calls == 2,
          str(harness.client.create_user_calls))


def scenario_concurrency(harness: Harness) -> None:
    section("two concurrent first-ensures create one row and one remote account")
    user_id = harness.user("racer")
    before_accounts = len(harness.client.created_accounts)
    before_rows = len(harness.rows())
    barrier = threading.Barrier(2)
    results: list[Any] = []
    errors: list[BaseException] = []

    def worker() -> None:
        service = harness.service()
        try:
            barrier.wait(timeout=10)
            results.append(service.ensure(user_id, "racer", display_name="Racer"))
        except BaseException as exc:  # noqa: BLE001 - a raised worker is a failure
            errors.append(exc)
        finally:
            harness.Session.remove()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    rows = harness.rows()
    check("no worker raised", not errors, " / ".join(repr(e) for e in errors))
    check("both workers returned a mapping", len(results) == 2, str(len(results)))
    check("exactly one row exists", len(rows) == before_rows + 1, str(len(rows)))
    check("exactly one new Forgejo account exists",
          len(harness.client.created_accounts) == before_accounts + 1,
          str(harness.client.created_accounts))
    check("both workers share the same forgejo username",
          len({r.forgejo_username for r in results}) == 1,
          str([r.forgejo_username for r in results]))


def scenario_token_lifecycle(harness: Harness) -> None:
    section("token reuse, rotation, scopes and plaintext discipline")
    user_id = harness.user("tokens")
    service = harness.service()
    logs = _capture_logs()

    read_only = service.token_for(user_id, "tokens", read_only=True)
    first_calls = harness.client.token_calls
    check("a read-only ticket asks for read:repository",
          harness.client.requested_scopes[-1] == (SCOPE_REPOSITORY_READ,),
          str(harness.client.requested_scopes[-1]))
    check("the ticket reports read_only", read_only.read_only is True)
    check("the ticket expires in the configured window",
          _parse(read_only.expires_at) == harness.clock.value + timedelta(days=7),
          str(read_only.expires_at))

    harness.clock.advance(days=1)
    reused = service.token_for(user_id, "tokens", read_only=True)
    check("an unexpired token is reused", reused.token == read_only.token)
    check("reuse does not call Forgejo again", harness.client.token_calls == first_calls,
          str(harness.client.token_calls))

    write_ticket = service.token_for(user_id, "tokens", read_only=False)
    check("a scope change rotates instead of reusing a read-only token",
          write_ticket.token != read_only.token)
    check("a writable ticket asks for write:repository",
          harness.client.requested_scopes[-1] == (SCOPE_REPOSITORY_WRITE,),
          str(harness.client.requested_scopes[-1]))
    check("rotating revokes the previous token first",
          (write_ticket.username, FORGEJO_TOKEN_NAME) in harness.client.deleted_tokens,
          str(harness.client.deleted_tokens))

    harness.clock.advance(days=8)
    expired = service.token_for(user_id, "tokens", read_only=False)
    check("an expired token is replaced", expired.token != write_ticket.token)
    check("exactly one remote token per user remains",
          len(harness.client.tokens.get(expired.username, {})) == 1,
          str(harness.client.tokens.get(expired.username)))

    scopes = {scope for scopes in harness.client.requested_scopes for scope in scopes}
    check("no user ticket ever carries an admin scope",
          all("admin" not in scope for scope in scopes), str(sorted(scopes)))

    rows = [row for row in harness.rows() if row["user_id"] == user_id]
    ciphertext = str(rows[0]["token_ciphertext"])
    check("the raw row does not contain the plaintext token", expired.token not in ciphertext)
    check("the stored value decrypts back to the token",
          TokenCipher(TEST_KEY).decrypt(ciphertext) != ""
          and expired.token in TokenCipher(TEST_KEY).decrypt(ciphertext))
    check("to_dict() never exposes the token",
          "token_ciphertext" not in service.find(user_id).to_dict()
          and "token" not in {k for k in service.find(user_id).to_dict()})

    logged = "\n".join(record.getMessage() for record in logs)
    check("no log line contains the plaintext token",
          expired.token not in logged and read_only.token not in logged and write_ticket.token not in logged)


def scenario_revoke(harness: Harness) -> None:
    section("revoke really calls Forgejo and is reversible")
    user_id = harness.user("revoked")
    service = harness.service()
    ticket = service.token_for(user_id, "revoked", read_only=False)
    username = ticket.username

    check("revoking a known user reports True", service.revoke(user_id) is True)
    check("the remote token was deleted",
          (username, FORGEJO_TOKEN_NAME) in harness.client.deleted_tokens,
          str(harness.client.deleted_tokens))
    row = service.find(user_id)
    check("the row is marked revoked", row.revoked_at is not None)
    check("the ciphertext is cleared", row.token_ciphertext is None)
    check("no remote token remains", not harness.client.tokens.get(username))

    revived = service.ensure(user_id, "revoked")
    check("ensure revives a revoked mapping", revived.revoked_at is None)

    doomed_id = harness.user("doomed")
    doomed = service.token_for(doomed_id, "doomed", read_only=False)
    service.revoke(doomed_id, delete_forgejo_user=True)
    check("delete_forgejo_user deletes the account",
          doomed.username in harness.client.deleted_users,
          str(harness.client.deleted_users))
    check("revoking an unknown user reports False", service.revoke(987654321) is False)


def scenario_rotate_expired() -> None:
    section("rotate_expired re-mints only what is due")
    # Its own harness: expiry is driven by a clock, and a shared one would let
    # earlier scenarios' identities leak into this batch.
    harness = Harness()
    try:
        fresh_id = harness.user("rotate-fresh")
        stale_id = harness.user("rotate-stale")
        service = harness.service()
        service.token_for(fresh_id, "rotate-fresh", read_only=False)   # expires at t0+7
        stale = service.token_for(stale_id, "rotate-stale", read_only=True)  # t0+7
        harness.clock.advance(days=8)                                  # both are now due
        service.token_for(fresh_id, "rotate-fresh", read_only=False)   # refreshed to t0+15

        before = harness.client.token_calls
        rotated = service.rotate_expired(limit=10)
        names = {credential.username for credential in rotated}
        check("only the expired identity was rotated", names == {stale.username}, str(sorted(names)))
        check("rotation minted exactly one new token",
              harness.client.token_calls == before + 1, str(harness.client.token_calls - before))
        check("the rotated ticket kept its read-only scope",
              harness.client.requested_scopes[-1] == (SCOPE_REPOSITORY_READ,),
              str(harness.client.requested_scopes[-1]))
    finally:
        harness.close()


def scenario_missing_key(harness: Harness) -> None:
    section("a missing GIT_IDENTITY_KEY is a clear configuration error")
    try:
        TokenCipher(env={})
    except GitIdentityConfigError as exc:
        message = str(exc)
        check("TokenCipher raises GitIdentityConfigError",
              "GIT_IDENTITY_KEY" in message, message)
    except BaseException as exc:  # noqa: BLE001 - anything else is the failure
        check("TokenCipher raises GitIdentityConfigError", False, f"{type(exc).__name__}: {exc}")
    else:
        check("TokenCipher raises GitIdentityConfigError", False, "nothing raised")

    user_id = harness.user("no-key")
    # Give the account a real identity first (with the key), so the revoke below
    # has something to cut off; then prove the *same* operation works with the
    # key absent, because revoke deletes by the deterministic username.
    harness.service().token_for(user_id, "no-key", read_only=False)
    service = GitIdentityService(
        harness.Session, client=harness.client, env={}, now=harness.clock,
    )
    try:
        service.token_for(user_id, "no-key", read_only=False)
    except GitIdentityConfigError as exc:
        check("token_for surfaces the missing key as a config error",
              "GIT_IDENTITY_KEY" in str(exc), str(exc))
        check("the error is not a bare TypeError", not isinstance(exc, TypeError))
    except BaseException as exc:  # noqa: BLE001 - anything else is the failure
        check("token_for surfaces the missing key as a config error", False,
              f"{type(exc).__name__}: {exc}")
    else:
        check("token_for surfaces the missing key as a config error", False, "nothing raised")

    # ``revoke`` must still work without the key: it deletes by username.
    check("revoke does not need the encryption key", service.revoke(user_id) is True)

    # A *misconfigured* client (no FORGEJO_ADMIN_TOKEN) must not block the local
    # half of a revocation either: the row still has to be marked revoked.
    offline_id = harness.user("offline-revoke")
    harness.service().token_for(offline_id, "offline-revoke", read_only=False)
    unconfigured = ForgejoIdentityClient(
        config=ImportConfig(base_url="http://forgejo.invalid:3000", admin_token=""),
    )
    offline_service = GitIdentityService(
        harness.Session, client=unconfigured, env={}, now=harness.clock,
    )
    check("an unconfigured Forgejo client cannot stop a revoke",
          offline_service.revoke(offline_id) is True
          and harness.service().find(offline_id).revoked_at is not None)


# ── Helpers ──────────────────────────────────────────────────────────

class _ListHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:  # pragma: no cover - list append
        self.records.append(record)


def _capture_logs() -> list[logging.LogRecord]:
    """Attach a capturing handler to the service logger for the rest of the run."""
    handler = _ListHandler()
    logger = logging.getLogger("cpypiserver.git_identity")
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    return handler.records


def _parse(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


# ── main ─────────────────────────────────────────────────────────────

def main() -> int:
    print("── Agent Hub · git identity exchange (S6) offline gate " + "─" * 8)
    print(f"   repo root: {REPO_ROOT}")
    harness: Harness | None = None
    try:
        scenario_username()
        scenario_schema()
        harness = Harness()
        for scenario in (
            lambda: scenario_ensure_idempotent(harness),
            lambda: scenario_concurrency(harness),
            lambda: scenario_token_lifecycle(harness),
            lambda: scenario_revoke(harness),
            scenario_rotate_expired,
            lambda: scenario_missing_key(harness),
        ):
            try:
                scenario()
            except Exception as exc:  # noqa: BLE001 - a crashed scenario is a failure
                import traceback

                FAILURES.append(f"scenario raised {type(exc).__name__}: {exc}")
                print(f"   ❌ scenario raised {type(exc).__name__}: {exc}")
                traceback.print_exc()
    finally:
        if harness is not None:
            harness.close()

    print()
    if FAILURES:
        print(f"❌ {len(FAILURES)}/{CHECKS} check(s) failed")
        for failure in FAILURES:
            print(f"   - {failure}")
        return 1
    print(f"✅ all {CHECKS} checks passed — derivation, idempotency, concurrency, "
          "rotation, scopes, no-plaintext, revoke, config error")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
