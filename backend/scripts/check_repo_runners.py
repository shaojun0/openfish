#!/usr/bin/env python
"""Gate: the per-repo runner is one row, one credential, one workspace.

Run from the backend directory (``backend/``)::

    python scripts/check_repo_runners.py

``services/repo_runner.py`` is the only writer of the ``repo_runners`` row
(``models/agent_hub.py``) and the only resolver of "which credential does repo X
clone with?".  That single-answer property is what this gate pins, entirely
offline (no Flask, no network, no Forgejo, a throwaway SQLite file):

1. **schema** — ``ensure_schema`` creates ``repo_runners`` and
   ``agent_tasks.runner_id``; a second call changes nothing; the two CHECK
   constraints reject an unknown ``credential_kind`` / ``egress_policy``.
2. **one row per repo** — ``ensure()`` is idempotent and never rewrites an
   existing row; the unique constraint is the race backstop.
3. **settings** — ``update()`` validates before it persists, normalizes the
   allowlist, and an empty subdir resets to ``runners/<id>``.
4. **credential at rest** — a repo token is sealed: no plaintext in any column,
   ``credential()`` decrypts it, and ``to_dict()`` exposes only
   ``has_credential``.
5. **fail closed** — a missing ``RUNNER_CREDENTIAL_KEY`` stores nothing even
   when ``GIT_IDENTITY_KEY`` is present (there is no fallback between the two
   keys), a corrupted ciphertext makes ``credential()`` raise even when a shared
   token exists, and so do an empty/absent ciphertext on a ``repo``-kind row and
   an expired ``credential_expires_at``; it is never a silent fallback.
6. **shared fallback** — ``FORGEJO_RUNNER_TOKEN`` is returned as
   ``x-access-token``; no token means ``None`` and credentials never create a
   row.
7. **workspace** — ``safe_workspace_subdir`` rejects anything that could escape
   ``AGENT_WORK_ROOT``; ``workspace_root`` joins the safe path under the base,
   refuses a symlink that resolves outside it, and no two runners may resolve to
   one root.
8. **dispatch** — ``enqueue()`` stamps the repo's runner id, a disabled runner's
   task is not claimable (including legacy ``runner_id IS NULL`` tasks, which
   stay queued for re-enable), ``ClaimedTask.runner_id`` round-trips, and
   ``stats().ready`` agrees with ``claim()``.
9. **concurrency** — the runner's ``max_concurrency`` overrides the env ceiling
   and an explicit ``enqueue(max_in_flight_per_repo=)`` overrides the runner.
10. **secrets** — the plaintext token appears in no log record emitted while the
    credential is stored, decrypted or found corrupt.
"""

from __future__ import annotations

import json
import logging
import sys
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from sqlalchemy import inspect, text  # noqa: E402
from sqlalchemy.exc import IntegrityError  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402

from config import settings  # noqa: E402
from models.agent_hub import AgentTask, Repo, RepoRunner  # noqa: E402
from models.agent_hub_migrate import ensure_schema  # noqa: E402
from services.agent_queue import (  # noqa: E402
    ENV_MAX_IN_FLIGHT_PER_REPO,
    AgentQueue,
    build_engine,
)
from services.git_identity import TokenCipher  # noqa: E402
from services.repo_runner import (  # noqa: E402
    RUNNER_CREDENTIAL_KEY_ENV,
    RUNNER_CREDENTIAL_KINDS,
    RUNNER_CREDENTIAL_REPO,
    RUNNER_CREDENTIAL_SHARED,
    RUNNER_EGRESS_ALLOWLIST,
    RUNNER_EGRESS_POLICIES,
    SHARED_TOKEN_ENV,
    RepoRunnerError,
    RepoRunnerService,
    safe_workspace_subdir,
    shared_runner_token,
)

#: A deterministic high-entropy key for the offline gate.  A deployment reads
#: ``RUNNER_CREDENTIAL_KEY`` from the environment; the gate injects its own so no
#: host secret is required.
_TEST_KEY = "openfish-repo-runner-gate-key-not-for-deployment"

#: A mapping with neither the cipher key nor the shared token, for the paths that
#: must prove they fail closed rather than reach for the deployment's own
#: credentials.  An explicit mapping — even an empty one — means "nothing is
#: configured here"; only ``env=None`` reads the settings, so this can no longer
#: be satisfied by accident from the host environment.
_EMPTY_ENV: dict[str, str] = {}

# The gate deliberately drives failure paths (a corrupted credential, an
# in-flight ceiling); those warnings are the subject of the assertions, not
# output for a human.  A NullHandler keeps the last-resort handler from echoing
# them to stderr without suppressing the ``_LogCapture`` used in scenario 10.
logging.getLogger("cpypiserver.agent_queue").addHandler(logging.NullHandler())
logging.getLogger("cpypiserver.repo_runner").addHandler(logging.NullHandler())

#: Counters for the summary line at the end.
CHECKS = 0
FAILURES: list[str] = []


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


# ── Harness ──────────────────────────────────────────────────────────

class _Harness:
    """A throwaway SQLite database with the real Agent Hub schema."""

    def __init__(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="openfish-repo-runners-")
        self.engine = build_engine(f"sqlite:///{Path(self.tmp.name) / 'hub.db'}")
        ensure_schema(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)

    def close(self) -> None:
        self.engine.dispose()
        self.tmp.cleanup()

    def repo(self, slug: str) -> int:
        """Insert a workspace repo (the FK every runner row needs)."""
        with self.Session() as session:
            row = Repo(
                slug=slug, source="local", kind="workspace",
                default_branch="main", sync_state="ready",
            )
            session.add(row)
            session.commit()
            return int(row.id)

    def service(
        self,
        *,
        env: dict[str, str] | None = None,
        cipher: TokenCipher | None = None,
    ) -> RepoRunnerService:
        return RepoRunnerService(self.Session, env=env, cipher=cipher)


def _make_repo(engine, slug: str) -> int:
    """A repo row on a raw engine (used by the schema scenario)."""
    with Session(engine) as session:
        row = Repo(
            slug=slug, source="local", kind="workspace",
            default_branch="main", sync_state="ready",
        )
        session.add(row)
        session.commit()
        return int(row.id)


def _cipher() -> TokenCipher:
    return TokenCipher(key=_TEST_KEY)


def _future() -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=1)


def _stored_text(engine, repo_id: int) -> str:
    """Every column of the repo's ``repo_runners`` row, joined into one string."""
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT * FROM repo_runners WHERE repo_id = :repo_id"),
            {"repo_id": int(repo_id)},
        ).mappings().first()
    if row is None:
        return ""
    return " ".join("" if value is None else str(value) for value in row.values())


def _settings(row: RepoRunner) -> tuple:
    return (
        bool(row.enabled), int(row.max_concurrency), row.egress_policy,
        row.egress_allowlist, row.workspace_subdir,
    )


class _LogCapture(logging.Handler):
    """Record every formatted log line emitted while attached."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.messages: list[str] = []
        self._formatter = logging.Formatter("%(levelname)s %(name)s %(message)s")

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.messages.append(self._formatter.format(record))
        except Exception:  # noqa: BLE001 - a formatting bug must not break the gate
            self.messages.append(str(record.getMessage()))


# ── 1. schema ────────────────────────────────────────────────────────

def scenario_schema() -> None:
    section("1 · schema: tables, runner_id, CHECK vocabularies")
    with tempfile.TemporaryDirectory(prefix="openfish-runner-schema-") as tmp:
        engine = build_engine(f"sqlite:///{Path(tmp) / 'hub.db'}")
        try:
            first = ensure_schema(engine)
            check("ensure_schema creates repo_runners",
                  "repo_runners" in first["created_tables"], json.dumps(first))
            inspector = inspect(engine)
            task_columns = {column["name"] for column in inspector.get_columns("agent_tasks")}
            check("ensure_schema creates agent_tasks.runner_id",
                  "runner_id" in task_columns, sorted(task_columns))
            runner_columns = {column["name"] for column in inspector.get_columns("repo_runners")}
            check("repo_runners carries the runner columns",
                  {
                      "repo_id", "name", "enabled", "max_concurrency",
                      "workspace_subdir", "egress_policy", "egress_allowlist",
                      "credential_kind", "credential_username",
                      "credential_ciphertext", "credential_expires_at",
                      "credential_rotated_at", "last_task_at",
                  } <= runner_columns,
                  sorted(runner_columns))

            second = ensure_schema(engine)
            check("a second ensure_schema is a no-op",
                  not any(second.values()), json.dumps(second))

            check("the model vocabulary matches the documented one",
                  RUNNER_CREDENTIAL_KINDS == (RUNNER_CREDENTIAL_SHARED, RUNNER_CREDENTIAL_REPO)
                  and tuple(RUNNER_EGRESS_POLICIES) == ("inherit", "internal", "allowlist"),
                  f"{RUNNER_CREDENTIAL_KINDS}/{RUNNER_EGRESS_POLICIES}")

            ok_repo = _make_repo(engine, "schema-ok")
            with Session(engine) as session:
                session.add(RepoRunner(repo_id=ok_repo, name="ok"))
                session.commit()
                row = session.query(RepoRunner).filter_by(repo_id=ok_repo).one()
                check("a fresh row defaults to shared/inherit",
                      row.credential_kind == RUNNER_CREDENTIAL_SHARED
                      and row.egress_policy == "inherit" and bool(row.enabled)
                      and int(row.max_concurrency) == 0,
                      f"kind={row.credential_kind} egress={row.egress_policy}")

            for label, kwargs in (
                ("credential_kind", {"credential_kind": "bogus"}),
                ("egress_policy", {"egress_policy": "bogus"}),
            ):
                repo_id = _make_repo(engine, f"schema-bad-{label}")
                with Session(engine) as session:
                    session.add(RepoRunner(repo_id=repo_id, name="bad", **kwargs))
                    try:
                        session.commit()
                    except IntegrityError:
                        session.rollback()
                        check(f"the CHECK rejects {label}={kwargs[label]}", True)
                    else:
                        check(f"the CHECK rejects {label}={kwargs[label]}", False,
                              "the invalid row was accepted")
        finally:
            engine.dispose()


# ── 2. exactly one runner per repo ───────────────────────────────────

def scenario_one_row_per_repo() -> None:
    section("2 · exactly one runner per repository")
    harness = _Harness()
    try:
        repo_id = harness.repo("one-row")
        service = harness.service(env=_EMPTY_ENV)
        first = service.ensure(repo_id)
        second = service.ensure(repo_id, name="ignored")
        check("ensure() is idempotent", first.id == second.id,
              f"{first.id} vs {second.id}")
        check("ensure(name=…) never rewrites an existing row",
              second.name == first.name and second.name != "ignored",
              repr(second.name))
        check("list() returns exactly the one row",
              [row.id for row in service.list()] == [first.id],
              repr([row.id for row in service.list()]))

        with Session(harness.engine) as session:
            session.add(RepoRunner(repo_id=repo_id, name="duplicate"))
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                check("the unique constraint rejects a second row for the repo", True)
            else:
                check("the unique constraint rejects a second row for the repo", False,
                      "the duplicate was accepted")
        with Session(harness.engine) as session:
            count = session.query(RepoRunner).filter_by(repo_id=repo_id).count()
        check("exactly one row survives the rejected insert", count == 1, str(count))
    finally:
        harness.close()


# ── 3. settings validation ───────────────────────────────────────────

def scenario_settings() -> None:
    section("3 · settings: validated update, allowlist, default subdir")
    harness = _Harness()
    try:
        repo_id = harness.repo("settings")
        service = harness.service(env=_EMPTY_ENV)
        runner = service.ensure(repo_id)

        updated = service.update(
            repo_id,
            enabled=False,
            max_concurrency=2,
            egress_policy=RUNNER_EGRESS_ALLOWLIST,
            egress_allowlist=" a.example, b.example ,a.example ",
            workspace_subdir="custom/one",
        )
        check("update() persists every field",
              not bool(updated.enabled) and int(updated.max_concurrency) == 2
              and updated.egress_policy == RUNNER_EGRESS_ALLOWLIST
              and updated.workspace_subdir == "custom/one",
              f"{_settings(updated)}")
        check("update() normalizes and de-duplicates the allowlist",
              updated.egress_allowlist == "a.example,b.example",
              repr(updated.egress_allowlist))

        service.update(repo_id, enabled=True)
        check("enabled flips back", bool(service.get(repo_id).enabled))

        service.update(repo_id, workspace_subdir="")
        check("an empty subdir resets to the id-derived default",
              service.get(repo_id).workspace_subdir == f"runners/{runner.id}",
              repr(service.get(repo_id).workspace_subdir))

        before = _settings(service.get(repo_id))
        for label, kwargs in (
            ("a negative max_concurrency", {"max_concurrency": -1}),
            ("an unknown egress_policy", {"egress_policy": "bogus"}),
            ("an escaping workspace_subdir", {"workspace_subdir": "../etc"}),
        ):
            try:
                service.update(repo_id, **kwargs)
            except RepoRunnerError:
                check(f"update() refuses {label}", True)
            else:
                check(f"update() refuses {label}", False, "the invalid value was accepted")
        check("a refused update leaves the row untouched",
              _settings(service.get(repo_id)) == before, repr(_settings(service.get(repo_id))))
    finally:
        harness.close()


# ── 4. credential at rest ────────────────────────────────────────────

def scenario_credential_at_rest() -> None:
    section("4 · credential at rest: sealed, decryptable, never exposed")
    harness = _Harness()
    try:
        repo_id = harness.repo("credential")
        service = harness.service(env={SHARED_TOKEN_ENV: "shared-token"}, cipher=_cipher())
        secret = f"SECRET-{uuid.uuid4().hex}"
        service.ensure(repo_id)
        runner = service.set_credential(
            repo_id, token=secret, username="bot", expires_at=_future(),
        )

        stored = _stored_text(harness.engine, repo_id)
        check("no plaintext token anywhere in the repo_runners row",
              secret not in stored, "the plaintext was found in the row")
        check("the sealed value is not the plaintext",
              bool(runner.credential_ciphertext)
              and secret not in str(runner.credential_ciphertext)
              and len(str(runner.credential_ciphertext)) > len(secret),
              "ciphertext missing or equal to the plaintext")
        check("the row is marked credential_kind='repo'",
              runner.credential_kind == RUNNER_CREDENTIAL_REPO
              and runner.credential_rotated_at is not None,
              f"kind={runner.credential_kind}")

        credential = service.credential(repo_id)
        check("credential() decrypts the repo token",
              credential is not None
              and credential.source == RUNNER_CREDENTIAL_REPO
              and credential.token == secret
              and credential.username == "bot",
              f"source={getattr(credential, 'source', None)}")
        check("credential() carries the expiry",
              credential is not None and credential.expires_at is not None)
        check("repr() never renders the plaintext token",
              credential is not None and secret not in repr(credential),
              "the plaintext token is visible in repr()")

        payload = service.get(repo_id).to_dict()
        check("to_dict() exposes has_credential", payload["has_credential"] is True,
              json.dumps(payload))
        check("to_dict() never exposes the ciphertext or the plaintext",
              "credential_ciphertext" not in payload and "ciphertext" not in payload
              and secret not in json.dumps(payload),
              f"keys={sorted(payload)}")

        service.clear_credential(repo_id)
        fallback = service.credential(repo_id)
        check("clear_credential() drops back to the shared token",
              fallback is not None
              and fallback.source == RUNNER_CREDENTIAL_SHARED
              and fallback.token == "shared-token",
              f"source={getattr(fallback, 'source', None)}")
        check("clearing removes the ciphertext",
              service.get(repo_id).credential_ciphertext is None)

        # The dedicated RUNNER_CREDENTIAL_KEY (not GIT_IDENTITY_KEY) must seal
        # and open the row all by itself: no injected cipher here, so
        # ``_require_cipher`` builds one straight from the environment, and a
        # *different* service instance holding only that key opens it again.
        dedicated_repo = harness.repo("dedicated-key")
        dedicated_env = {
            SHARED_TOKEN_ENV: "shared-token",
            RUNNER_CREDENTIAL_KEY_ENV: _TEST_KEY,
        }
        dedicated_secret = f"SECRET-{uuid.uuid4().hex}"
        harness.service(env=dedicated_env).set_credential(
            dedicated_repo, token=dedicated_secret,
        )
        reopened = harness.service(env=dedicated_env).credential(dedicated_repo)
        check("RUNNER_CREDENTIAL_KEY alone seals and opens a repo credential",
              reopened is not None
              and reopened.source == RUNNER_CREDENTIAL_REPO
              and reopened.token == dedicated_secret,
              f"source={getattr(reopened, 'source', None)}")
        check("the dedicated-key ciphertext carries no plaintext",
              dedicated_secret not in _stored_text(harness.engine, dedicated_repo))
        wrong_key = harness.service(env={
            SHARED_TOKEN_ENV: "shared-token",
            RUNNER_CREDENTIAL_KEY_ENV: "a-different-runner-key",
        })
        try:
            wrong_key.credential(dedicated_repo)
        except RepoRunnerError:
            check("a different RUNNER_CREDENTIAL_KEY cannot open the credential", True)
        else:
            check("a different RUNNER_CREDENTIAL_KEY cannot open the credential", False,
                  "credential() returned a token sealed under another key")
    finally:
        harness.close()


# ── 5. fail closed ───────────────────────────────────────────────────

def scenario_fail_closed() -> None:
    section("5 · fail closed: missing key, corrupted ciphertext")
    harness = _Harness()
    try:
        repo_id = harness.repo("fail-closed")
        # A shared token and even the user-identity master key exist, but the
        # dedicated RUNNER_CREDENTIAL_KEY does not: the write must refuse.  The
        # runner key is deliberately separate from GIT_IDENTITY_KEY, so a runner
        # can hold the former without the latter, and there is no fallback.
        service = harness.service(env={
            SHARED_TOKEN_ENV: "shared-token",
            "GIT_IDENTITY_KEY": "identity-master-key-not-for-runner-credentials",
        })
        service.ensure(repo_id)
        secret = f"SECRET-{uuid.uuid4().hex}"
        try:
            service.set_credential(repo_id, token=secret)
        except RepoRunnerError as exc:
            check("a missing RUNNER_CREDENTIAL_KEY makes set_credential refuse", True)
            check("the refusal names the missing runner key",
                  "RUNNER_CREDENTIAL_KEY" in str(exc), str(exc))
        else:
            check("a missing RUNNER_CREDENTIAL_KEY makes set_credential refuse", False,
                  "set_credential returned a row")
        row = service.get(repo_id)
        check("nothing was stored",
              row.credential_kind == RUNNER_CREDENTIAL_SHARED
              and row.credential_ciphertext is None,
              f"kind={row.credential_kind}")
        check("no plaintext reached the row",
              secret not in _stored_text(harness.engine, repo_id))
        shared = service.credential(repo_id)
        check("the repo still resolves the shared token",
              shared is not None and shared.source == RUNNER_CREDENTIAL_SHARED
              and shared.token == "shared-token",
              f"source={getattr(shared, 'source', None)}")

        # A corrupted ciphertext must raise, never fall back to the shared token.
        other = harness.repo("corrupt")
        sealed = harness.service(env={SHARED_TOKEN_ENV: "shared-token"}, cipher=_cipher())
        sealed.set_credential(other, token=f"SECRET-{uuid.uuid4().hex}")
        with harness.engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE repo_runners SET credential_ciphertext = :value "
                    "WHERE repo_id = :repo_id"
                ),
                {"value": "not-a-valid-fernet-token", "repo_id": other},
            )
        try:
            sealed.credential(other)
        except RepoRunnerError:
            check("a corrupted ciphertext makes credential() raise", True)
        else:
            check("a corrupted ciphertext makes credential() raise", False,
                  "credential() fell back to the shared token")
        check("the corrupted row stays a repo credential",
              sealed.get(other).credential_kind == RUNNER_CREDENTIAL_REPO,
              f"kind={sealed.get(other).credential_kind}")

        # A repo-kind row whose ciphertext is NULL or "" is still a repo
        # credential: it must raise, never fall back to the shared token.
        for label, value in (("NULL", None), ("empty", "")):
            blank = harness.repo(f"blank-ciphertext-{label.lower()}")
            blank_service = harness.service(
                env={SHARED_TOKEN_ENV: "shared-token"}, cipher=_cipher(),
            )
            blank_service.ensure(blank)
            with harness.engine.begin() as conn:
                conn.execute(
                    text(
                        "UPDATE repo_runners SET credential_kind = :kind, "
                        "credential_ciphertext = :value WHERE repo_id = :repo_id"
                    ),
                    {"kind": RUNNER_CREDENTIAL_REPO, "value": value, "repo_id": blank},
                )
            try:
                blank_service.credential(blank)
            except RepoRunnerError:
                check(f"a repo credential with a {label} ciphertext raises", True)
            else:
                check(f"a repo credential with a {label} ciphertext raises", False,
                      "credential() fell back to the shared token")

        # An expired repo credential must not be presented to Forgejo.
        expiring = harness.repo("expired")
        expired_service = harness.service(
            env={SHARED_TOKEN_ENV: "shared-token"}, cipher=_cipher(),
        )
        expired_service.set_credential(
            expiring, token=f"SECRET-{uuid.uuid4().hex}",
            expires_at=datetime.now(timezone.utc) - timedelta(hours=1),
        )
        try:
            expired_service.credential(expiring)
        except RepoRunnerError as exc:
            check("an expired repo credential raises", True)
            check("the refusal names the expiry", "过期" in str(exc), str(exc))
        else:
            check("an expired repo credential raises", False,
                  "credential() returned an expired token")
    finally:
        harness.close()


# ── 6. shared fallback ───────────────────────────────────────────────

def scenario_shared_fallback() -> None:
    section("6 · shared fallback: FORGEJO_RUNNER_TOKEN as x-access-token")
    harness = _Harness()
    try:
        repo_id = harness.repo("shared")
        service = harness.service(env={SHARED_TOKEN_ENV: "  shared-token  "})
        credential = service.credential(repo_id)
        check("a repo with no runner row still resolves the shared token",
              credential is not None and credential.source == RUNNER_CREDENTIAL_SHARED,
              f"source={getattr(credential, 'source', None)}")
        check("the shared token is stripped",
              credential is not None and credential.token == "shared-token")
        check("the shared username is x-access-token",
              credential is not None and credential.username == "x-access-token",
              f"username={getattr(credential, 'username', None)}")
        check("credential() never creates a row", service.get(repo_id) is None)

        check("shared_runner_token reads and strips the env value",
              shared_runner_token(env={SHARED_TOKEN_ENV: " tok "}) == "tok")
        check("shared_runner_token is '' when unset",
              shared_runner_token(env=_EMPTY_ENV) == "")
        check("no env token → credential() is None",
              harness.service(env=_EMPTY_ENV).credential(repo_id) is None)

        runner = service.ensure(repo_id)
        check("an explicit shared row returns the same shared token",
              service.credential(repo_id).source == RUNNER_CREDENTIAL_SHARED)
        check("ensure() does not stamp a credential",
              runner.credential_kind == RUNNER_CREDENTIAL_SHARED
              and runner.credential_ciphertext is None)
    finally:
        harness.close()


# ── 7. workspace ─────────────────────────────────────────────────────

def scenario_workspace() -> None:
    section("7 · workspace: safe subdir and base join")
    for bad in (
        "../etc", "/etc", "a/../b", "..", ".", "a//b", "a\\b", "\\etc",
        "a\nb", "a\x7fb", "a\x00b",
    ):
        try:
            safe_workspace_subdir(bad)
        except RepoRunnerError:
            check(f"safe_workspace_subdir rejects {bad}", True)
        else:
            check(f"safe_workspace_subdir rejects {bad}", False, "the path was accepted")

    check("'' → runners/<id>", safe_workspace_subdir("", runner_id=7) == "runners/7",
          repr(safe_workspace_subdir("", runner_id=7)))
    check("'' with no id → runners", safe_workspace_subdir("") == "runners",
          repr(safe_workspace_subdir("")))
    check("whitespace is the default",
          safe_workspace_subdir("   ", runner_id=3) == "runners/3")
    check("a two-level relative path is allowed",
          safe_workspace_subdir("team/a-b_c.d") == "team/a-b_c.d")

    harness = _Harness()
    try:
        repo_id = harness.repo("workspace")
        service = harness.service(env=_EMPTY_ENV)
        runner = service.ensure(repo_id)
        base = Path(harness.tmp.name) / "work"
        check("workspace_root joins the default subdir under the base",
              service.workspace_root(repo_id, base) == base / f"runners/{runner.id}",
              str(service.workspace_root(repo_id, base)))
        service.update(repo_id, workspace_subdir="custom/sub")
        check("workspace_root honours a custom subdir",
              service.workspace_root(repo_id, base) == base / "custom/sub",
              str(service.workspace_root(repo_id, base)))

        # Two repositories must never resolve to one work root: neither by an
        # explicit subdir naming another's id-derived default, nor by two
        # explicit subdirs that are equal.
        other_repo = harness.repo("workspace-other")
        other = service.ensure(other_repo)
        try:
            service.update(repo_id, workspace_subdir=f"runners/{other.id}")
        except RepoRunnerError:
            check("an explicit subdir cannot take over another runner's default", True)
        else:
            check("an explicit subdir cannot take over another runner's default", False,
                  "the takeover path was accepted")
        service.update(repo_id, workspace_subdir="team/shared")
        try:
            service.update(other_repo, workspace_subdir="team/shared")
        except RepoRunnerError:
            check("two runners cannot share an explicit workspace subdir", True)
        else:
            check("two runners cannot share an explicit workspace subdir", False,
                  "both runners accepted team/shared")
        check("the refused collision left the second runner on its default",
              service.get(other_repo).workspace_subdir != "team/shared",
              repr(service.get(other_repo).workspace_subdir))

        # A symlink anywhere in the joined path cannot redirect the checkout
        # outside the work root.
        outside = Path(harness.tmp.name) / "outside"
        outside.mkdir()
        (base / "escape").symlink_to(outside, target_is_directory=True)
        service.update(repo_id, workspace_subdir="escape")
        try:
            service.workspace_root(repo_id, base)
        except RepoRunnerError:
            check("workspace_root refuses a symlink that escapes the base", True)
        else:
            check("workspace_root refuses a symlink that escapes the base", False,
                  "the symlink was followed outside AGENT_WORK_ROOT")

        # A read describes the defaults without materialising a row.
        virgin = harness.repo("workspace-virgin")
        document = service.document(virgin, name="virgin")
        check("document() describes defaults without creating a row",
              service.get(virgin) is None and document["id"] is None
              and document["enabled"] is True
              and document["credential_kind"] == RUNNER_CREDENTIAL_SHARED
              and document["workspace_subdir"] == "",
              json.dumps(document))
    finally:
        harness.close()


# ── 8. dispatch ──────────────────────────────────────────────────────

def scenario_dispatch() -> None:
    section("8 · dispatch: runner_id stamped, disabled runners skipped")
    harness = _Harness()
    try:
        queue = AgentQueue(harness.engine)
        service = harness.service(env=_EMPTY_ENV)

        # Enabled: claimable and stamped.
        repo_id = harness.repo("dispatch-enabled")
        runner = service.ensure(repo_id)
        task_id = queue.enqueue(
            repo_id, kind="review", payload={"commit_sha": "a" * 40}, dedup_key="a" * 40,
        )
        check("enqueue() returns a task id", task_id > 0, str(task_id))
        with Session(harness.engine) as session:
            stored = session.get(AgentTask, task_id)
            check("the task row carries the runner id",
                  stored.runner_id == runner.id, f"{stored.runner_id} vs {runner.id}")
        claimed = queue.claim(worker="gate-enabled")
        check("a task for an enabled runner is claimable", claimed is not None)
        check("ClaimedTask.runner_id round-trips",
              claimed is not None and claimed.runner_id == runner.id)
        check("ClaimedTask.to_dict() carries runner_id",
              claimed is not None and claimed.to_dict()["runner_id"] == runner.id)
        if claimed is not None:
            queue.succeed(claimed.lease)

        # Disabled: written but not claimable until re-enabled.
        disabled_repo = harness.repo("dispatch-disabled")
        disabled_runner = service.ensure(disabled_repo)
        service.update(disabled_repo, enabled=False)
        disabled_task = queue.enqueue(disabled_repo, kind="fix", payload={}, dedup_key="d")
        check("a task is still written for a disabled runner", disabled_task > 0,
              str(disabled_task))
        check("a task for a disabled runner is not claimable",
              queue.claim(worker="gate-disabled") is None)
        service.update(disabled_repo, enabled=True)
        reclaimed = queue.claim(worker="gate-reenabled")
        check("enabling the runner makes its task claimable",
              reclaimed is not None and reclaimed.id == disabled_task,
              repr(getattr(reclaimed, "id", None)))
        check("the re-enabled task still carries its runner id",
              reclaimed is not None and reclaimed.runner_id == disabled_runner.id)
        if reclaimed is not None:
            queue.succeed(reclaimed.lease)

        # Legacy rows (no runner row) stay claimable with runner_id NULL.
        legacy_repo = harness.repo("dispatch-legacy")
        legacy_task = queue.enqueue(legacy_repo, kind="review", payload={})
        with Session(harness.engine) as session:
            check("no runner row → runner_id stays NULL",
                  session.get(AgentTask, legacy_task).runner_id is None)
        legacy_claimed = queue.claim(worker="gate-legacy")
        check("a legacy task is still claimable",
              legacy_claimed is not None and legacy_claimed.id == legacy_task)
        check("a legacy task reports runner_id None",
              legacy_claimed is not None and legacy_claimed.runner_id is None)
        if legacy_claimed is not None:
            queue.succeed(legacy_claimed.lease)

        # A legacy task (runner_id NULL) whose repo's runner is *now* disabled is
        # skipped by claim — the decision follows the repository's current row,
        # not the task snapshot — and stays queued until re-enabled rather than
        # being failed and dead-lettered.
        legacy_disabled_repo = harness.repo("dispatch-legacy-disabled")
        queued_legacy = queue.enqueue(legacy_disabled_repo, kind="review", payload={})
        with Session(harness.engine) as session:
            check("the pre-runner task is stamped NULL",
                  session.get(AgentTask, queued_legacy).runner_id is None)
        service.ensure(legacy_disabled_repo)
        service.update(legacy_disabled_repo, enabled=False)
        check("a legacy task for a disabled runner is not claimable",
              queue.claim(worker="gate-legacy-disabled") is None)
        with Session(harness.engine) as session:
            check("the skipped task stays queued, not dead",
                  session.get(AgentTask, queued_legacy).status == "queued")
        check("stats().ready excludes the skipped task",
              queue.stats().ready == 0, str(queue.stats().ready))
        service.update(legacy_disabled_repo, enabled=True)
        check("stats().ready counts the task once re-enabled",
              queue.stats().ready == 1, str(queue.stats().ready))
        recovered = queue.claim(worker="gate-legacy-reenabled")
        check("re-enabling recovers the skipped legacy task",
              recovered is not None and recovered.id == queued_legacy,
              repr(getattr(recovered, "id", None)))
        if recovered is not None:
            queue.succeed(recovered.lease)
    finally:
        harness.close()


# ── 9. concurrency ───────────────────────────────────────────────────

def scenario_concurrency() -> None:
    section("9 · concurrency: runner ceiling and explicit argument precedence")
    harness = _Harness()
    # The deployment ceiling used to be injected through the environment.  The
    # settings object is now the only reader of the environment, resolved once at
    # start-up, so the gate sets the *field*: the same thing the variable would
    # have done, minus a runtime re-read that anything running in this process
    # could have used to re-point a worker's ceiling.  The variable name is
    # asserted below so the field and the documented knob cannot drift apart.
    check(f"the ceiling knob is still {ENV_MAX_IN_FLIGHT_PER_REPO}",
          ENV_MAX_IN_FLIGHT_PER_REPO == "AGENT_MAX_IN_FLIGHT_PER_REPO",
          ENV_MAX_IN_FLIGHT_PER_REPO)
    previous = settings.agent.max_in_flight_per_repo
    settings.agent.max_in_flight_per_repo = 1
    try:
        repo_id = harness.repo("concurrency")
        service = harness.service(env=_EMPTY_ENV)
        service.ensure(repo_id)
        queue = AgentQueue(harness.engine)

        first = queue.enqueue(repo_id, kind="review", payload={})
        second = queue.enqueue(repo_id, kind="review", payload={})
        check("the deployment ceiling suppresses the second task",
              first > 0 and second == 0, f"{first}/{second}")

        service.update(repo_id, max_concurrency=2)
        third = queue.enqueue(repo_id, kind="review", payload={})
        check("runner.max_concurrency overrides the deployment ceiling", third > 0, str(third))
        fourth = queue.enqueue(repo_id, kind="review", payload={})
        check("the runner ceiling is then enforced", fourth == 0, str(fourth))

        fifth = queue.enqueue(repo_id, kind="review", payload={},
                              max_in_flight_per_repo=5)
        check("an explicit argument overrides the runner value", fifth > 0, str(fifth))
        sixth = queue.enqueue(repo_id, kind="review", payload={},
                              max_in_flight_per_repo=3)
        check("the explicit ceiling is itself enforced", sixth == 0, str(sixth))
    finally:
        settings.agent.max_in_flight_per_repo = previous
        harness.close()


# ── 10. secrets never logged ─────────────────────────────────────────

def scenario_secrets_never_logged() -> None:
    section("10 · secrets: the plaintext token reaches no log record")
    harness = _Harness()
    capture = _LogCapture()
    module_logger = logging.getLogger("cpypiserver.repo_runner")
    previous_level = module_logger.level
    module_logger.setLevel(logging.DEBUG)
    module_logger.addHandler(capture)
    try:
        repo_id = harness.repo("secrets")
        service = harness.service(env={SHARED_TOKEN_ENV: "shared-token"}, cipher=_cipher())
        secret = f"SECRET-{uuid.uuid4().hex}"
        service.ensure(repo_id)
        service.set_credential(repo_id, token=secret)
        service.credential(repo_id)

        with harness.engine.begin() as conn:
            conn.execute(
                text(
                    "UPDATE repo_runners SET credential_ciphertext = :value "
                    "WHERE repo_id = :repo_id"
                ),
                {"value": "corrupt-ciphertext", "repo_id": repo_id},
            )
        try:
            service.credential(repo_id)
        except RepoRunnerError:
            pass

        check("logging was actually captured (the scenario is not vacuous)",
              bool(capture.messages), "no log records")
        leaked = [message for message in capture.messages if secret in message]
        check("the plaintext token appears in no log record", not leaked,
              f"{len(leaked)} record(s) leaked it")
        check("the credential path logged something to capture",
              any("credential" in message for message in capture.messages),
              "no credential log line")
    finally:
        module_logger.removeHandler(capture)
        module_logger.setLevel(previous_level)
        harness.close()


# ── main ─────────────────────────────────────────────────────────────

def main() -> int:
    print("── Agent Hub · per-repo runner offline gate " + "─" * 15)
    print(f"   repo root: {REPO_ROOT}")
    for scenario in (
        scenario_schema,
        scenario_one_row_per_repo,
        scenario_settings,
        scenario_credential_at_rest,
        scenario_fail_closed,
        scenario_shared_fallback,
        scenario_workspace,
        scenario_dispatch,
        scenario_concurrency,
        scenario_secrets_never_logged,
    ):
        try:
            scenario()
        except Exception as exc:  # noqa: BLE001 - a crashed scenario is a failure
            import traceback

            FAILURES.append(f"{scenario.__name__} raised {type(exc).__name__}: {exc}")
            print(f"   ❌ {scenario.__name__} raised {type(exc).__name__}: {exc}")
            traceback.print_exc()

    print()
    if FAILURES:
        print(f"❌ {len(FAILURES)}/{CHECKS} check(s) failed")
        for failure in FAILURES:
            print(f"   - {failure}")
        return 1
    print(f"✅ all {CHECKS} checks passed — schema, one row per repo, validated settings, "
          "sealed credential, fail-closed (corrupt/empty/expired), shared fallback, "
          "safe and unique workspace, dispatch, concurrency, no plaintext in logs")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
