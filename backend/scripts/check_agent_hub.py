#!/usr/bin/env python
"""Gate: the Agent Hub foundation — schema, fingerprint, queue and migration.

Run from the backend directory (`backend/`)::

    python scripts/check_agent_hub.py

This is the S0 slice's self-check.  It boots **no Flask app and imports no
other slice**: it creates the nine Agent Hub tables on a throwaway SQLite file,
then exercises the four things every other Agent Hub slice builds on.  If this
passes, ``models/agent_hub.py`` and ``services/agent_queue.py`` are sound and a
failure elsewhere is somebody else's bug.

What it pins down
-----------------
1. **Schema.**  The nine tables of §4.2 exist after ``create_all``, and the
   constraints that carry invariants are really there — the unique
   ``(repo_id, fingerprint)`` that makes de-duplication a database guarantee
   rather than a code convention, and the
   ``index(status, priority, created_at)`` the claim query needs.
2. **Fingerprint (§4.4, invariant I1).**  Same inputs → same hash; a different
   rule, path, symbol or context → a different hash.  And decisively: changing
   ``line_hint`` does **not** change the fingerprint, because a line number in
   the identity is exactly how "同一问题每次重报" happens.
3. **Queue happy path.**  enqueue → claim(leased) → heartbeat (lease_until moves
   forward) → running → done, including the negative control that a second
   worker cannot claim the same task.
4. **Queue failure path.**  A running task whose lease expires is reclaimed, goes
   back to ``queued`` with ``attempts`` advanced, and — once attempts are
   exhausted — lands in ``dead`` instead of looping.
5. **Migration idempotence.**  An old-shaped ``repos`` table gains what it is
   missing, and running ``ensure_schema`` twice is a no-op (not a duplicate-
   column error), which is what makes it safe in the boot path.

Every check prints one line; the exit status is the gate's answer.
"""

from __future__ import annotations

import hashlib
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from sqlalchemy import inspect, text  # noqa: E402
from sqlalchemy.exc import IntegrityError  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from models.agent_hub import (  # noqa: E402
    TASK_STATUS,
    Finding,
    Repo,
    fingerprint,
)
from models.agent_hub_migrate import ensure_schema  # noqa: E402
from models.base import Base  # noqa: E402
from services.agent_queue import AgentQueue, Worker, build_engine  # noqa: E402

#: The nine tables of DEVELOPMENT.md §4.2, by their SQL names.
EXPECTED_TABLES = (
    "repos",
    "import_jobs",
    "repo_issues",
    "repo_commits",
    "findings",
    "finding_events",
    "review_runs",
    "agent_tasks",
    "finding_evidence",
)

_TMP_DIR = Path(tempfile.mkdtemp(prefix="cpypi-agent-hub-gate-"))
_DB_PATH = _TMP_DIR / "agent_hub.db"
_LEGACY_DB_PATH = _TMP_DIR / "legacy_agent_hub.db"

failures: list[str] = []


def check(ok: bool, label: str) -> None:
    print(("   ✅ " if ok else "   ✗ ") + label)
    if not ok:
        failures.append(label)


class RecordingHandler:
    """A task handler that records what it was given and can be made to fail."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.seen: list[dict] = []

    def __call__(self, task):
        self.seen.append(task.payload)
        if self.fail:
            raise RuntimeError("gate: deliberate handler failure")
        return f"gate:{task.id}"


def seed_repo(session: Session, slug: str, *, kind: str = "workspace") -> int:
    repo = Repo(
        slug=slug,
        source="local",
        default_branch="main",
        kind=kind,
        sync_state="ready",
    )
    session.add(repo)
    session.commit()
    return int(repo.id)


# ── 1. Schema ────────────────────────────────────────────────────────

def check_schema() -> None:
    print("── Schema (§4.2) ───────────────────────────────────────────────")
    engine = build_engine(f"sqlite:///{_DB_PATH}")
    Base.metadata.create_all(engine)

    inspector = inspect(engine)
    present = set(inspector.get_table_names())
    for table in EXPECTED_TABLES:
        check(table in present, f"table {table} exists")

    # The unique fingerprint constraint is the *database* half of I1: even a
    # buggy service cannot create the same finding twice.
    with Session(engine) as session:
        repo_id = seed_repo(session, "gate/one")
        common = dict(
            repo_id=repo_id, fingerprint="f" * 64, rule_id="gate.rule",
            level="debt", severity="low", status="open",
            file_path="backend/x.py", symbol="X.y", title="t",
        )
        session.add(Finding(**common))
        session.commit()
        try:
            session.add(Finding(**common))
            session.commit()
            duplicated = True
        except IntegrityError:
            duplicated = False
            session.rollback()
        check(not duplicated, "unique(repo_id, fingerprint) rejects a duplicate finding")

    task_indexes = {index["name"] for index in inspector.get_indexes("agent_tasks")}
    check(
        "ix_agent_tasks_status_priority_created" in task_indexes,
        "the claim index (status, priority, created_at) exists",
    )
    engine.dispose()


# ── 2. Fingerprint ───────────────────────────────────────────────────

def check_fingerprint() -> None:
    print()
    print("── Fingerprint (§4.4 / I1) ─────────────────────────────────────")
    base = fingerprint("backend.no-typing-optional", "backend/a.py", "A.b")
    check(len(base) == 64 and all(c in "0123456789abcdef" for c in base),
          "fingerprint is 64 lowercase hex characters")
    check(base == fingerprint("backend.no-typing-optional", "backend/a.py", "A.b"),
          "identical inputs hash identically (stable across calls)")
    check(base != fingerprint("backend.other-rule", "backend/a.py", "A.b"),
          "a different rule_id changes the fingerprint")
    check(base != fingerprint("backend.no-typing-optional", "backend/b.py", "A.b"),
          "a different file_path changes the fingerprint")
    check(base != fingerprint("backend.no-typing-optional", "backend/a.py", "A.c"),
          "a different symbol changes the fingerprint")
    check(base != fingerprint("backend.no-typing-optional", "backend/a.py", "A.b", "second"),
          "a rule-supplied context_key disambiguates")
    check(fingerprint("r", "p", "s", "") == fingerprint("r", "p", "s"),
          "the default context_key is the empty string")

    # The decisive one: line numbers, commit shas and summaries are NOT inputs.
    finding = Finding(
        repo_id=1, fingerprint=base, rule_id="backend.no-typing-optional",
        level="debt", severity="low", status="open", file_path="backend/a.py",
        symbol="A.b", line_hint=10, title="title v1",
    )
    before = finding.fingerprint
    finding.line_hint = 987
    finding.title = "title v2 — the same problem, restated"
    check(before == finding.fingerprint,
          "moving the line_hint or rewording the title leaves the identity alone")

    # §4.4 spells the exact byte layout: four NUL-separated fields.
    expected = hashlib.sha256(
        b"backend.no-typing-optional\x00backend/a.py\x00A.b\x00"
    ).hexdigest()
    check(base == expected, "the hash is sha256(rule\\x00path\\x00symbol\\x00context)")


class LeaseLike:
    """A ``Lease``-shaped impostor: a foreign worker's claim on the same id."""

    def __init__(self, task_id: int, worker: str) -> None:
        self.task_id = task_id
        self.worker = worker
        self.expires_at = datetime.now(timezone.utc)
        self.attempts = 0


# ── 3. Queue: happy path ─────────────────────────────────────────────

def check_queue_happy_path(queue: AgentQueue, repo_id: int) -> None:
    print()
    print("── Queue: enqueue → lease → heartbeat → done ───────────────────")
    task_id = queue.enqueue(repo_id, kind="review", payload={"sha": "deadbeef"})

    handler = RecordingHandler()
    worker = Worker(queue, handler=handler, name="gate-happy")
    worked = worker.run_once()
    check(worked, "a worker claims the queued task and settles it")
    check(handler.seen == [{"sha": "deadbeef"}], "the handler received the task payload")

    state = queue.get(task_id)
    check(state is not None and state["status"] == "done",
          f"the task ends as done (got {state and state['status']})")
    check(state is not None and state["result_ref"] == f"gate:{task_id}",
          "the handler's return value is stored as result_ref")
    check(state is not None and state["started_at"] is not None
          and state["finished_at"] is not None,
          "started_at and finished_at are both recorded")

    # The negative control for the claim query: a leased task is invisible to
    # another worker, which is the whole point of claiming it atomically.
    second = queue.enqueue(repo_id, kind="fix")
    claim_a = queue.claim(worker="gate-a")
    claim_b = queue.claim(worker="gate-b")
    check(claim_a is not None and claim_a.id == second, "the first worker gets the task")
    check(claim_b is None, "a second worker cannot claim a task that is already leased")

    # Heartbeat: the deadline moves forward, and only the lease holder may do it.
    before = queue.get(second)["lease_until"]
    check(queue.heartbeat(claim_a.lease), "the lease holder renews its lease")
    after = queue.get(second)["lease_until"]
    check(before is not None and after is not None and after > before,
          "the heartbeat pushes lease_until forward")
    check(queue.mark_running(claim_a.lease), "the lease holder marks the task running")
    check(not queue.heartbeat(LeaseLike(second, "gate-somebody-else")),
          "a worker that does not hold the lease cannot renew it")
    check(queue.succeed(claim_a.lease, result_ref="gate:manual"),
          "the lease holder completes the task")
    check(not queue.succeed(claim_a.lease), "a second completion attempt is a no-op")



# ── 4. Queue: failure, reclaim, retry, dead letter ───────────────────

def check_queue_failure_path(queue: AgentQueue, repo_id: int) -> None:
    print()
    print("── Queue: failure → reclaim → retry → dead letter ─────────────")
    failing = RecordingHandler(fail=True)
    task_id = queue.enqueue(repo_id, kind="review", payload={"sha": "cafe"}, max_attempts=3)

    worker = Worker(queue, handler=failing, name="gate-fail")
    worker.run_once()
    state = queue.get(task_id)
    check(state is not None and state["status"] == "queued"
          and state["attempts"] == 1,
          "a raised handler fails the task and requeues it with attempts advanced")
    check(state is not None and "deliberate handler failure" in (state["error"] or ""),
          "the failure reason is recorded for the operator")

    # Now the case heartbeats exist for: another worker claims the retried task,
    # is killed before it can report, and leaves an expired lease behind.  The
    # sweep must reclaim it *without* running the handler again.
    _force_expired(queue, task_id, worker_name="gate-ghost", attempts=1)
    report = queue.reclaim_expired()
    check(bool(report), "an expired lease is reclaimed")
    outcome = report.reclaimed[0] if report.reclaimed else None
    check(outcome is not None and outcome.status == "queued"
          and outcome.attempts == 2,
          "reclaiming under max_attempts requeues the task")
    check(queue.get(task_id)["status"] == "queued",
          "the reclaimed task is claimable again")

    # That attempt was the last one: abandoning it again must dead-letter it.
    _force_expired(queue, task_id, worker_name="gate-ghost-2", attempts=2)
    report = queue.reclaim_expired()
    state = queue.get(task_id)
    check(bool(report) and report.dead and not report.retried,
          "exhausting max_attempts reclaims the task as dead")
    check(state is not None and state["status"] == "dead",
          f"the task is dead, not looping (got {state and state['status']})")

    # A dead task is retryable by an operator; a running one is not.
    check(queue.retry(task_id, reason="gate"), "an operator can retry a dead task")
    check(queue.get(task_id)["status"] == "queued", "the retried task is queued again")
    running = queue.claim(worker="gate-final")
    check(running is not None, "the retried task can be claimed again")
    check(queue.cancel(running.id, reason="gate cancel"), "an operator can cancel a leased task")
    check(queue.get(running.id)["status"] == "dead",
          "cancelling parks the task as dead")

    stats = queue.stats()
    check(set(stats.by_status) == set(TASK_STATUS),
          "stats reports every state of the machine")
    check(stats.ready >= 0 and stats.expired == 0, "no lease is left expired")


def _force_expired(
    queue: AgentQueue, task_id: int, *, worker_name: str, attempts: int = 1,
) -> None:
    """Simulate a worker that claimed *task_id* and was then killed."""
    with Session(queue.engine) as session:
        session.execute(
            text(
                "UPDATE agent_tasks SET status='running', leased_by=:worker, "
                "attempts=:attempts, lease_until=:expired, scheduled_at=:past "
                "WHERE id=:id"
            ),
            {
                "worker": worker_name,
                "attempts": attempts,
                "expired": datetime.now(timezone.utc) - timedelta(seconds=1),
                "past": datetime.now(timezone.utc) - timedelta(seconds=1),
                "id": task_id,
            },
        )
        session.commit()


# ── 5. Migration ─────────────────────────────────────────────────────

def check_migration() -> None:
    print()
    print("── Migration (models/agent_hub_migrate.py) ─────────────────────")
    # A deployment that predates this module: `repos` exists in an old shape and
    # none of the other eight tables do.  ensure_schema has to cope with both.
    connection = sqlite3.connect(_LEGACY_DB_PATH)
    connection.executescript(
        """
        CREATE TABLE repos (
            id INTEGER NOT NULL PRIMARY KEY,
            slug VARCHAR(256) NOT NULL
        );
        """
    )
    connection.commit()
    connection.close()

    engine = build_engine(f"sqlite:///{_LEGACY_DB_PATH}")
    first = ensure_schema(engine)
    inspector = inspect(engine)
    present = set(inspector.get_table_names())
    check(set(EXPECTED_TABLES) <= present, "the missing Agent Hub tables are created")
    check("repos" not in first["created_tables"],
          "the pre-existing repos table is left alone, not recreated")
    columns = {c["name"] for c in inspector.get_columns("repos")}
    check({"kind", "sync_state", "issue_count", "created_at"} <= columns,
          "the pre-existing repos table gains its new columns")
    indexes = {i["name"] for i in inspector.get_indexes("repos")}
    check("ix_repos_slug" in indexes, "its indexes are created too")

    second = ensure_schema(engine)
    check(second == {
        "created_tables": [], "added_columns": [], "added_indexes": [],
        "updated_constraints": [],
    }, f"a second ensure_schema is a no-op (got {second})")
    engine.dispose()

    # And on a database this module created itself, twice more, for good measure.
    engine = build_engine(f"sqlite:///{_DB_PATH}")
    third = ensure_schema(engine)
    check(third["created_tables"] == [], "a fully migrated database creates nothing")
    check(ensure_schema(engine) == third, "repeated ensure_schema stays inert")
    engine.dispose()


# ── Main ─────────────────────────────────────────────────────────────

def main() -> int:
    print(f"Temporary database: {_DB_PATH}")
    print()
    check_schema()
    check_fingerprint()

    engine = build_engine(f"sqlite:///{_DB_PATH}")
    # The queue's own bootstrap is part of what is under test: it must be able to
    # create what it needs without create_all having run.
    queue = AgentQueue(engine, lease_seconds=30.0, heartbeat_seconds=1.0)
    queue.ensure_schema()
    with Session(engine) as session:
        repo_id = seed_repo(session, "gate/queue")
    check_queue_happy_path(queue, repo_id)
    check_queue_failure_path(queue, repo_id)
    engine.dispose()

    check_migration()

    print()
    if failures:
        print(f"❌ agent-hub check FAILED — {len(failures)} problem(s)")
        for item in failures:
            print("   " + item)
        return 1
    print("✅ agent-hub check passed — schema, fingerprint, queue and migration")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
