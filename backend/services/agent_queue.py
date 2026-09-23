"""The Agent Hub task queue — one table, one worker, no broker.

``agent_tasks`` is the queue (see ``models/agent_hub.py``): a worker claims a
row, holds a lease on it, renews that lease while it works, and either finishes
it or leaves it for another worker to reclaim.  The state machine is
``queued → leased → running → done | failed | dead``.

Why a table rather than Celery/Redis
------------------------------------
DEVELOPMENT.md §3.1 puts a hard ceiling on moving parts: the deployment is a
single SQLite file or a single PostgreSQL database, and a task queue whose state
lives *outside* that database could disagree with it (a task "queued" in Redis
for a repository that was just deleted).  The queue is therefore a plain table,
and the delivery guarantee is a **lease**, not an ack:

* claiming is atomic on both backends.  SQLite takes the write lock up front
  (``BEGIN IMMEDIATE``), so claim statements serialise instead of racing;
  PostgreSQL locks the candidate row with ``FOR UPDATE SKIP LOCKED``, so N
  workers each get a different task without blocking on each other.
* ``lease_until`` is a deadline, not a promise.  A worker that is killed — the
  case that matters, because it cannot clean up after itself — leaves a row
  whose lease expires; :meth:`AgentQueue.reclaim_expired` hands it back.
* a task is only ``done`` when the worker says so; anything else is retried up
  to ``max_attempts`` and then parked as ``dead`` for an operator.  A dead task
  is an ops-log event, never a finding (§9.1).

Interface
---------
Everything of substance is engine-driven and Flask-free, so the same code runs
under the app, under the CLI, and under a worker process::

    from services.agent_queue import AgentQueue, Lease, build_engine

    queue = AgentQueue(engine)
    queue.ensure_schema()
    task_id = queue.enqueue(repo_id, kind="review", payload={"sha": "…"})
    with queue.claim(worker="worker-1") as lease:      # ``with`` keeps the beat
        if lease is not None:
            queue.mark_running(lease)
            …
            queue.succeed(lease, result_ref="run:42")

CLI::

    python -m services.agent_queue worker --once
    python -m services.agent_queue worker --loop --poll-interval 2
    python -m services.agent_queue status

The worker's *work* is injected (``handler``): :func:`default_handler` builds the
real sandbox runner (``services.agent_worker``), which the CLI uses, while the
gates pass a recording fake through :func:`build_worker`.  The queue itself never
imports the runner at module scope, which is what keeps this module importable
without Flask or a Docker daemon.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, create_engine, event, func, select, update
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session, aliased

from config import settings
from config.agent import AgentConfig
from models.agent_hub import TASK_KIND, TASK_STATUS, AgentTask, RepoRunner


#: How often a worker renews ``lease_until`` while a task runs (§9.1: 15s).
DEFAULT_HEARTBEAT_SECONDS = 15.0

#: Lease length handed out by :meth:`AgentQueue.claim`.  Must be comfortably
#: larger than the heartbeat interval; a worker that misses several beats is
#: assumed dead and reclaimed.
DEFAULT_LEASE_SECONDS = 60.0

#: Base delay before a failed task is retried; doubled per attempt
#: (``base * 2**attempts``) and capped.  Prevents a broken task from spinning.
DEFAULT_BACKOFF_SECONDS = 30.0
MAX_BACKOFF_SECONDS = 900.0

#: Statuses that count as "this work is still pending".  Producer-side dedup and
#: the per-repo in-flight ceiling both look at exactly this set.
ACTIVE_TASK_STATUSES: tuple[str, ...] = ("queued", "leased", "running")

#: Statuses that occupy an *execution* slot: the queue has handed out a lease
#: (``leased``) or the worker has begun (``running``).  This is the set the
#: claim-time ceiling counts, whereas ``enqueue`` counts
#: :data:`ACTIVE_TASK_STATUSES` (``queued`` included) as a more conservative
#: producer-side throttle.  ``queued`` cannot be counted at claim time: the
#: candidate row is itself ``queued``, so a ceiling of 1 would make every task
#: unclaimable the moment two were waiting.  A row that has been leased but
#: whose worker died still occupies its slot until
#: :meth:`AgentQueue.reclaim_expired` frees it — the ceiling bounds *claimed*
#: work, which is what ``max_concurrency`` means.
RUNNING_TASK_STATUSES: tuple[str, ...] = ("leased", "running")

#: Environment knob for :meth:`AgentQueue.enqueue`'s per-repo ceiling.  ``0``
#: (the library default) means unlimited; a deployment sets a small number so one
#: noisy repository cannot fill the single global queue and starve the rest.  A
#: repository whose runner sets ``max_concurrency > 0`` overrides this value.
#: The name comes from the settings model rather than being re-typed here.
ENV_MAX_IN_FLIGHT_PER_REPO = AgentConfig.env_name("max_in_flight_per_repo")


def configured_max_in_flight_per_repo() -> int:
    """The configured per-repository in-flight ceiling (0 = no cap)."""
    return max(0, int(settings.agent.max_in_flight_per_repo))


def _runner_runnable():
    """SQL predicate: the task's repository has no *disabled* runner row.

    Correlated on ``repo_id`` (the repository's **current** configuration), not
    on the task's snapshotted ``runner_id``.  Disabling a runner is therefore
    immediate for every queued task — including legacy rows written before the
    runner row existed (``runner_id IS NULL``) — and a repo with no runner row
    at all has no disabled row, so its tasks stay claimable.  Shared by
    :meth:`AgentQueue.claim` and :meth:`AgentQueue.stats` so the two cannot drift.
    """
    return ~(
        select(RepoRunner.id)
        .where(
            RepoRunner.repo_id == AgentTask.repo_id,
            RepoRunner.enabled.is_(False),
        )
        .exists()
    )


def _runner_ceiling_available():
    """SQL predicate: the task's repository is below its runner's ceiling.

    Correlated on ``repo_id`` against the repository's runner row, exactly like
    :func:`_runner_runnable`.  A repository with no runner row contributes no
    predicate, so legacy ``runner_id IS NULL`` tasks stay claimable; a runner
    with ``max_concurrency = 0`` means *inherit* and likewise adds nothing —
    the environment ceiling ``AGENT_MAX_IN_FLIGHT_PER_REPO`` is deliberately
    **not** re-checked here.  It stays a producer-side throttle
    (:meth:`AgentQueue.enqueue`) because a hard claim-time check on a value that
    is not stored on the task would strand rows that were legitimately admitted
    earlier (before the knob was lowered, or through an explicit
    ``max_in_flight_per_repo`` override) and could stall the repository forever.

    The count is over :data:`RUNNING_TASK_STATUSES` (``leased`` / ``running``),
    i.e. tasks that already hold an execution slot; see that constant for why
    ``queued`` is excluded.  Written as ``NOT (bounded AND inflight >= ceiling)``
    so a missing runner row (``ceiling`` NULL → ``coalesce`` 0) and a
    ``max_concurrency`` of 0 both fall through to claimable.

    Atomicity: the predicate is part of the claim's own SELECT, so it is
    evaluated in the transaction that writes the lease.  What makes the bound
    hold *across replicas* is the repository lock :meth:`AgentQueue.claim` takes
    in the preceding statement: on PostgreSQL a competing claimant for the same
    repository blocks on that row lock until the winner commits, and because
    READ COMMITTED gives every statement a fresh snapshot, the loser's count
    then sees that committed lease instead of the pre-commit count.  On SQLite
    :meth:`AgentQueue._serialise_writes` already holds ``BEGIN IMMEDIATE``, so
    claim transactions are serialised outright.  The lock's placement, order
    and cost are documented on :meth:`AgentQueue.claim`.
    """
    running = aliased(AgentTask)
    inflight = (
        select(func.count())
        .select_from(running)
        .where(
            running.repo_id == AgentTask.repo_id,
            running.status.in_(RUNNING_TASK_STATUSES),
        )
        .scalar_subquery()
    )
    ceiling = (
        select(RepoRunner.max_concurrency)
        .where(RepoRunner.repo_id == AgentTask.repo_id)
        .scalar_subquery()
    )
    bounded = func.coalesce(ceiling, 0)
    return ~and_(bounded > 0, inflight >= bounded)


# ── Values exchanged with callers ────────────────────────────────────

@dataclass(frozen=True)
class Lease:
    """Proof that *worker* owns task *task_id* until :attr:`expires_at`.

    Passed to the outcome methods (:meth:`AgentQueue.succeed` and friends) so a
    stale worker cannot report on a task that was reclaimed and given away.
    """

    task_id: int
    worker: str
    expires_at: datetime
    attempts: int = 0

    def is_expired(self, *, now: datetime | None = None) -> bool:
        return _as_utc(now or _now()) >= _as_utc(self.expires_at)


@dataclass(frozen=True)
class ClaimedTask:
    """A task that was atomically moved to ``leased`` for this worker."""

    id: int
    repo_id: int
    kind: str
    payload: dict[str, Any]
    attempts: int
    max_attempts: int
    priority: int
    lease: Lease
    #: The repository's logical runner row, or ``None`` for a legacy task that
    #: was queued before runners existed (``agent_tasks.runner_id IS NULL``).
    runner_id: int | None = None

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready view (payload already parsed) for the worker and the API."""
        return {
            "id": self.id,
            "repo_id": self.repo_id,
            "runner_id": self.runner_id,
            "kind": self.kind,
            "payload": self.payload,
            "attempts": self.attempts,
            "max_attempts": self.max_attempts,
            "priority": self.priority,
            "leased_by": self.lease.worker,
            "lease_until": _as_utc(self.lease.expires_at).isoformat(),
        }


@dataclass(frozen=True)
class ReclaimOutcome:
    """What :meth:`AgentQueue.reclaim_expired` did with one abandoned lease."""

    task_id: int
    previous_status: str
    status: str          # "queued" (retry) or "dead" (out of attempts)
    attempts: int
    reason: str


@dataclass(frozen=True)
class ReclaimReport:
    """Result of one reclaim sweep — always returned, even when it did nothing."""

    reclaimed: tuple[ReclaimOutcome, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.reclaimed)

    @property
    def retried(self) -> tuple[ReclaimOutcome, ...]:
        return tuple(o for o in self.reclaimed if o.status == "queued")

    @property
    def dead(self) -> tuple[ReclaimOutcome, ...]:
        return tuple(o for o in self.reclaimed if o.status == "dead")

    def to_dict(self) -> dict[str, Any]:
        return {
            "reclaimed": [
                {
                    "task_id": o.task_id,
                    "previous_status": o.previous_status,
                    "status": o.status,
                    "attempts": o.attempts,
                    "reason": o.reason,
                }
                for o in self.reclaimed
            ]
        }


@dataclass(frozen=True)
class QueueStats:
    """``status -> count``, plus the two numbers an operator actually watches."""

    by_status: dict[str, int]
    ready: int
    expired: int

    def to_dict(self) -> dict[str, Any]:
        return {"by_status": self.by_status, "ready": self.ready, "expired": self.expired}


def _now() -> datetime:
    """Timezone-aware "now" — same definition as ``models.base.utcnow``."""
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    """Normalize a datetime read back from the database to aware UTC.

    SQLite has no timezone type: a ``DateTime(timezone=True)`` value round-trips
    as naive, while PostgreSQL returns it aware.  Every comparison in this
    module goes through here so the two backends behave identically.
    """
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _json(payload: dict[str, Any] | None) -> str:
    return json.dumps(payload or {}, sort_keys=True, separators=(",", ":"))


def worker_id() -> str:
    """A worker identity that is unique per host *and* per process."""
    host = socket.gethostname().split(".")[0][:48] or "worker"
    return f"{host}-{os.getpid()}-{uuid.uuid4().hex[:8]}"


# ── Engine helpers (small, self-contained: no config import, no Flask) ──

def resolve_database_url(database: str | None = None) -> str:
    """Turn ``--db`` / ``DATABASE_URL`` into a SQLAlchemy URL.

    Deliberately narrower than ``extensions.database.resolve_database_url``: that
    module imports the auth stack and the ORM session, so importing it here would
    make the queue CLI depend on the whole application.  The *rules* are the
    same because the *source* is the same — both read ``settings.storage``, so
    the CLI and the server can no longer disagree about which database they mean
    (the CLI used to read the raw environment while the server read ``.env``).
    A bare path is SQLite, ``postgres://`` is upgraded to the psycopg driver this
    project ships, anything with an explicit driver is left alone.
    """
    configured = database if database is not None else settings.storage.database_url
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


def build_engine(database: str | None = None, *, echo: bool = False) -> Engine:
    """An engine usable by the queue, with the SQLite pragmas the app uses.

    Sets ``journal_mode=WAL`` (readers do not block the claiming writer) and
    ``foreign_keys=ON``, matching ``extensions.database._create_sqlite_engine``
    so a task row cannot outlive its repository.
    """
    url = resolve_database_url(database)
    if url.startswith("sqlite"):
        database_path = make_url(url).database
        if database_path and database_path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(database_path)) or ".", exist_ok=True)
        engine = create_engine(url, echo=echo, connect_args={"check_same_thread": False})

        @event.listens_for(engine, "connect")
        def _set_pragma(dbapi_conn, _record):
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA journal_mode=WAL;")
            cursor.execute("PRAGMA foreign_keys=ON;")
            # Claiming is a write; a second worker must wait for the first
            # rather than failing fast with "database is locked".
            cursor.execute("PRAGMA busy_timeout=5000;")
            cursor.close()

        return engine

    return create_engine(url, echo=echo, pool_pre_ping=True)


def _safe_url(url: str) -> str:
    return make_url(url).render_as_string(hide_password=True)


# ── The queue ────────────────────────────────────────────────────────

class AgentQueue:
    """All queue transitions, against one engine.  Stateless; safe to share.

    Every method commits its own transaction.  Nothing here calls a model
    handler or touches the filesystem, so a route can enqueue inside a request
    and a worker can claim in another process with no coordination beyond the
    database.
    """

    def __init__(
        self,
        engine: Engine,
        *,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
        heartbeat_seconds: float = DEFAULT_HEARTBEAT_SECONDS,
    ) -> None:
        if heartbeat_seconds >= lease_seconds:
            raise ValueError(
                "heartbeat_seconds must be shorter than lease_seconds, or every "
                "task is reclaimed while it is still running"
            )
        self.engine = engine
        self.lease_seconds = float(lease_seconds)
        self.heartbeat_seconds = float(heartbeat_seconds)

    # ── schema ───────────────────────────────────────────────────────

    def ensure_schema(self) -> dict[str, list[str]]:
        """Create the Agent Hub tables if they are absent; idempotent."""
        from models.agent_hub_migrate import ensure_schema
        return ensure_schema(self.engine)

    def session(self) -> Session:
        """A session on this engine, ``expire_on_commit=False`` like the app's."""
        return Session(bind=self.engine, expire_on_commit=False)

    def _serialise_writes(self, session: Session) -> None:
        """On SQLite, take the write lock *before* the claim's SELECT.

        SQLAlchemy's SQLite driver opens a deferred transaction, so the claim
        would SELECT (shared lock) and only then try to upgrade to a write lock.
        Two workers doing that at the same moment is the classic ``database is
        locked`` — and worse, one of them can upgrade from a stale snapshot.
        ``BEGIN IMMEDIATE`` takes the write lock up front, so the second worker
        waits and then sees the first worker's leased row instead of a stale
        ``queued`` one.  PostgreSQL needs none of this: it gets the same
        guarantee from ``FOR UPDATE SKIP LOCKED`` (see :meth:`claim`).

        The statement goes to the raw DBAPI connection, because the SQLAlchemy
        Connection wrapper would autobegin its own transaction while emitting it
        — the session has already opened one (a no-op on SQLite) for us.

        Called only by the two write-then-read paths (claim and reclaim); the
        single-statement transitions need no extra lock.
        """
        if self.engine.dialect.name != "sqlite":
            return
        session.connection().connection.driver_connection.execute("BEGIN IMMEDIATE")

    # ── producer ─────────────────────────────────────────────────────

    def enqueue(
        self,
        repo_id: int,
        *,
        kind: str = "review",
        payload: dict[str, Any] | None = None,
        priority: int = 0,
        max_attempts: int = 3,
        scheduled_at: datetime | None = None,
        dedup_key: str | None = None,
        max_in_flight_per_repo: int | None = None,
    ) -> int:
        """Append a task and return its id (the API hands this back to the SPA).

        Returns **0** when the task was suppressed rather than written — a
        duplicate of an active ``(repo_id, kind, dedup_key)``, or the repository
        already at its in-flight ceiling.  Callers distinguish "queued (id > 0)"
        from "suppressed (0)" instead of mistaking a storm for success.

        The ceiling is, in order of precedence: an explicit
        *max_in_flight_per_repo* argument, the repository's runner
        ``max_concurrency`` when it is greater than 0, then
        ``AGENT_MAX_IN_FLIGHT_PER_REPO`` (``configured_max_in_flight_per_repo``).
        The task is bound to the repository's runner row when one exists, so a
        worker can honour that runner's configuration.

        This check is a **producer-side throttle, not the enforcement point**:
        it counts :data:`ACTIVE_TASK_STATUSES` (``queued`` included) and, being
        an ordinary non-atomic read, two racing producers can still both insert.
        The bound that execution actually respects is the claim-time guard in
        :func:`_runner_ceiling_available` (runner ``max_concurrency > 0`` only),
        evaluated inside :meth:`claim`'s transaction.  Both are deliberate:
        the throttle stops one noisy repository from filling the global queue,
        while the claim-time guard makes the runner's ceiling hold for rows that
        arrive by any other path — a retry, a reclaim, a race, or an explicit
        ``max_in_flight_per_repo`` override.  Returning ``0`` is therefore a
        statement about the throttle, never a promise about the whole queue.

        *dedup_key* is the producer's idempotency token (a push sha, an issue
        number).  The check is not a unique constraint: two workers racing can
        still both insert, which is the same at-least-once posture as the rest of
        the queue — but a retry storm from one producer collapses to one row.
        """
        if kind not in TASK_KIND:
            raise ValueError(f"unknown task kind {kind}; expected one of {TASK_KIND}")
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        key = str(dedup_key or "").strip()
        explicit_ceiling = (
            int(max_in_flight_per_repo) if max_in_flight_per_repo is not None else None
        )
        with self.session() as session:
            if key:
                duplicate = session.execute(
                    select(AgentTask.id).where(
                        AgentTask.repo_id == int(repo_id),
                        AgentTask.kind == kind,
                        AgentTask.dedup_key == key,
                        AgentTask.status.in_(ACTIVE_TASK_STATUSES),
                    ).limit(1)
                ).first()
                if duplicate is not None:
                    return 0
            # The logical runner owns this repo's settings (0 = inherit); it is
            # resolved before the ceiling check and stamped onto the task.
            runner = session.execute(
                select(RepoRunner).where(RepoRunner.repo_id == int(repo_id))
            ).scalar_one_or_none()
            if explicit_ceiling is not None:
                ceiling = explicit_ceiling
            elif runner is not None and int(runner.max_concurrency) > 0:
                ceiling = int(runner.max_concurrency)
            else:
                ceiling = configured_max_in_flight_per_repo()
            if ceiling > 0:
                active = session.execute(
                    select(func.count()).select_from(AgentTask).where(
                        AgentTask.repo_id == int(repo_id),
                        AgentTask.status.in_(ACTIVE_TASK_STATUSES),
                    )
                ).scalar() or 0
                if int(active) >= ceiling:
                    return 0
            task = AgentTask(
                repo_id=repo_id,
                runner_id=int(runner.id) if runner is not None else None,
                kind=kind,
                status="queued",
                payload=_json(payload),
                priority=int(priority),
                max_attempts=int(max_attempts),
                attempts=0,
                leased_by="",
                dedup_key=key or None,
                scheduled_at=_as_utc(scheduled_at) if scheduled_at else _now(),
            )
            session.add(task)
            session.commit()
            task_id = int(task.id)
        return task_id

    # ── consumer ─────────────────────────────────────────────────────

    def claim(self, *, worker: str, lease_seconds: float | None = None) -> ClaimedTask | None:
        """Atomically move the best queued task to ``leased`` for *worker*.

        "Best" is ``priority DESC, created_at ASC`` among the rows whose
        ``scheduled_at`` has arrived — served by
        ``index(status, priority, created_at)``.  A task whose repository's
        runner is disabled is skipped entirely (a disabled runner must not
        execute), decided from the repository's *current* row rather than the
        task's snapshot, so disabling is immediate for legacy ``runner_id IS
        NULL`` tasks too; a repository with no runner row stays claimable.
        Returns ``None`` when there is nothing to do, which is the worker's
        signal to sleep, not an error.

        The repository's runner ``max_concurrency`` is also a ceiling here, not
        only in :meth:`enqueue`: a candidate is skipped while the repository
        already holds that many tasks in :data:`RUNNING_TASK_STATUSES`
        (``leased`` / ``running``), so a retry, a reclaim, or a producer race
        cannot push a repository past the bound at execution time.  A runner
        with ``max_concurrency = 0`` means *inherit* and adds no claim-time
        predicate; ``AGENT_MAX_IN_FLIGHT_PER_REPO`` remains producer-side only.
        The full semantics live on :func:`_runner_ceiling_available`.

        Atomicity, stated plainly: the runner rows of every *bounded*
        repository are locked ``FOR UPDATE`` in a preceding statement (see the
        code below).  On PostgreSQL a competing claimant for the same repository
        blocks on that row lock until this claim commits, then re-counts in a
        fresh READ COMMITTED snapshot that includes the committed lease, so it
        cannot lease the (N+1)th slot; on SQLite ``BEGIN IMMEDIATE``
        (:meth:`_serialise_writes`) serialises the whole claim instead, and the
        extra statement degrades to an ordinary read.  The lock rows are taken
        in one global order (``repo_runners.repo_id`` ascending), so two
        claimants can never deadlock.  Cost, stated honestly: the lock spans all
        bounded runner rows, so concurrent claims for *different* bounded
        repositories serialise on this statement for the length of one claim
        transaction.  That is acceptable because a claim is a short count plus
        one UPDATE, repositories with no runner row or ``max_concurrency = 0``
        lock nothing, and the queue's contract is correctness rather than claim
        throughput.

        Callers should prefer :meth:`lease`, which also starts the heartbeat.
        """
        lease_for = float(lease_seconds if lease_seconds is not None else self.lease_seconds)
        now = _now()
        expires = now + timedelta(seconds=lease_for)
        # Portable on both backends: a NOT EXISTS against the repository's own
        # runner row, so no dialect-specific raw SQL is needed.
        runner_runnable = _runner_runnable()
        # The execution-slot ceiling, evaluated in the same statement as the
        # lease so the count and the write share one transaction.  On SQLite
        # that is airtight; on PostgreSQL the preceding repo lock (below) is
        # what makes the count see a competing claimant's committed lease.
        runner_ceiling = _runner_ceiling_available()

        with self.session() as session:
            self._serialise_writes(session)
            # Take the repository-scoped lock BEFORE the ceiling-leading claim
            # SELECT, and on both backends.  PostgreSQL needs it: the claim's
            # count would otherwise read the statement-start snapshot, letting
            # two replicas each observe the pre-commit count and lease two rows
            # for a repo at its ceiling.  Blocking here means the loser's next
            # statement (the claim SELECT) runs with a fresh snapshot and sees
            # the winner's committed lease.  Locking every bounded runner row in
            # one global order (repo_id ascending) prevents deadlock; it does
            # briefly serialise claims for different bounded repositories, which
            # is the accepted cost of an exact bound.  Repositories with no
            # runner row or ``max_concurrency = 0`` have no ceiling and are not
            # locked; SQLite renders no FOR UPDATE (unsupported) and already
            # holds the write lock from ``BEGIN IMMEDIATE``, so there it is only
            # a harmless read.
            session.execute(
                select(RepoRunner.repo_id)
                .where(RepoRunner.max_concurrency > 0)
                .order_by(RepoRunner.repo_id.asc())
                .with_for_update()
            ).all()
            if self.engine.dialect.name == "postgresql":
                statement = (
                    select(AgentTask)
                    .where(
                        AgentTask.status == "queued",
                        (AgentTask.scheduled_at.is_(None)) | (AgentTask.scheduled_at <= now),
                        runner_runnable,
                        runner_ceiling,
                    )
                    .order_by(AgentTask.priority.desc(), AgentTask.created_at.asc())
                    .limit(1)
                    .with_for_update(skip_locked=True)
                )
            else:
                statement = (
                    select(AgentTask)
                    .where(
                        AgentTask.status == "queued",
                        (AgentTask.scheduled_at.is_(None)) | (AgentTask.scheduled_at <= now),
                        runner_runnable,
                        runner_ceiling,
                    )
                    .order_by(AgentTask.priority.desc(), AgentTask.created_at.asc())
                    .limit(1)
                )
            task = session.scalars(statement).first()
            if task is None:
                session.rollback()
                return None
            task.status = "leased"
            task.leased_by = worker
            task.lease_until = expires
            task.started_at = now
            session.commit()
            claimed = ClaimedTask(
                id=int(task.id),
                repo_id=int(task.repo_id),
                runner_id=int(task.runner_id) if task.runner_id is not None else None,
                kind=task.kind,
                payload=task.payload_dict(),
                attempts=int(task.attempts),
                max_attempts=int(task.max_attempts),
                priority=int(task.priority),
                lease=Lease(
                    task_id=int(task.id), worker=worker,
                    expires_at=expires, attempts=int(task.attempts),
                ),
            )
        return claimed

    @contextmanager
    def lease(self, *, worker: str) -> Iterator[ClaimedTask | None]:
        """Claim a task and keep its lease alive for the duration of the block.

        ::

            with queue.lease(worker=name) as task:
                if task is None:
                    continue
                queue.mark_running(task.lease)
                …

        The heartbeat is a daemon thread that calls :meth:`heartbeat` every
        ``heartbeat_seconds``; it stops when the block exits, however it exits
        (exception included).  It never masks the task's own outcome: a heartbeat
        failure is logged and the next beat retries.
        """
        claimed = self.claim(worker=worker)
        if claimed is None:
            yield None
            return

        stop = threading.Event()

        def _beat() -> None:
            while not stop.wait(self.heartbeat_seconds):
                if not self.heartbeat(claimed.lease):
                    return

        beat = threading.Thread(target=_beat, name=f"agent-queue-hb-{claimed.id}", daemon=True)
        beat.start()
        try:
            yield claimed
        finally:
            stop.set()
            beat.join(timeout=1.0)

    def mark_running(self, lease: Lease) -> bool:
        """``leased → running``.  False when the lease was already lost."""
        return self._transition(
            lease, expected=("leased",), status="running", set_started=False,
        )

    def heartbeat(self, lease: Lease, *, lease_seconds: float | None = None) -> bool:
        """Push ``lease_until`` out by one lease length.  False when it was lost."""
        lease_for = float(lease_seconds if lease_seconds is not None else self.lease_seconds)
        now = _now()
        with self.session() as session:
            result = session.execute(
                update(AgentTask)
                .where(
                    AgentTask.id == lease.task_id,
                    AgentTask.leased_by == lease.worker,
                    AgentTask.status.in_(("leased", "running")),
                )
                .values(lease_until=now + timedelta(seconds=lease_for))
            )
            session.commit()
            return bool(result.rowcount)

    def succeed(self, lease: Lease, *, result_ref: str | None = None) -> bool:
        """``leased|running → done``.  Idempotent, so a late ack is harmless."""
        return self._finish(lease, status="done", result_ref=result_ref, error=None)

    def fail(self, lease: Lease, *, error: str) -> str:
        """Record a failure; return the status the task ended in.

        Under ``max_attempts`` the task goes back to ``queued`` with an
        exponential back-off in ``scheduled_at``; at or over it the task becomes
        ``dead`` and is left for an operator (§9.1).  Returns ``"queued"`` or
        ``"dead"``, or ``"lost"`` when the lease had already been reclaimed.
        """
        now = _now()
        with self.session() as session:
            task = session.execute(
                select(AgentTask).where(
                    AgentTask.id == lease.task_id,
                    AgentTask.leased_by == lease.worker,
                    AgentTask.status.in_(("leased", "running")),
                )
            ).scalar_one_or_none()
            if task is None:
                return "lost"

            attempts = int(task.attempts) + 1
            task.attempts = attempts
            task.error = error[:2000]
            task.lease_until = None
            task.leased_by = ""
            task.finished_at = now
            if attempts >= int(task.max_attempts):
                task.status = "dead"
                outcome = "dead"
            else:
                task.status = "queued"
                task.scheduled_at = now + timedelta(
                    seconds=min(MAX_BACKOFF_SECONDS, DEFAULT_BACKOFF_SECONDS * (2 ** (attempts - 1)))
                )
                outcome = "queued"
            session.commit()
        if outcome == "dead":
            pass
        else:
            pass
        return outcome

    def _finish(
        self, lease: Lease, *, status: str, result_ref: str | None, error: str | None,
    ) -> bool:
        with self.session() as session:
            result = session.execute(
                update(AgentTask)
                .where(
                    AgentTask.id == lease.task_id,
                    AgentTask.leased_by == lease.worker,
                    AgentTask.status.in_(("leased", "running")),
                )
                .values(
                    status=status,
                    result_ref=result_ref,
                    error=error,
                    leased_by="",
                    lease_until=None,
                    finished_at=_now(),
                )
            )
            session.commit()
        if not result.rowcount:
            pass
        return bool(result.rowcount)

    def _transition(
        self, lease: Lease, *, expected: tuple[str, ...], status: str, set_started: bool,
    ) -> bool:
        values: dict[str, Any] = {"status": status}
        if set_started:
            values["started_at"] = _now()
        with self.session() as session:
            result = session.execute(
                update(AgentTask)
                .where(
                    AgentTask.id == lease.task_id,
                    AgentTask.leased_by == lease.worker,
                    AgentTask.status.in_(expected),
                )
                .values(**values)
            )
            session.commit()
        return bool(result.rowcount)

    # ── recovery ─────────────────────────────────────────────────────

    def reclaim_expired(self, *, now: datetime | None = None) -> ReclaimReport:
        """Take back every lease whose deadline has passed.

        Called by the worker before each claim, and available to the admin API.
        For each abandoned row the attempt counter moves — that is what makes a
        task that kills its worker eventually die instead of looping forever:

        * ``attempts + 1 < max_attempts`` → back to ``queued`` for another go;
        * otherwise → ``dead``.

        PostgreSQL locks the candidate rows ``FOR UPDATE SKIP LOCKED``, so two
        workers sweeping at the same moment split the set instead of blocking;
        SQLite's ``BEGIN IMMEDIATE`` serialises them and the second finds
        nothing left to do.
        """
        moment = _now() if now is None else _as_utc(now)
        outcomes: list[ReclaimOutcome] = []

        with self.session() as session:
            self._serialise_writes(session)
            statement = (
                select(AgentTask)
                .where(
                    AgentTask.status.in_(("leased", "running")),
                    AgentTask.lease_until.is_not(None),
                    AgentTask.lease_until < moment,
                )
                .order_by(AgentTask.lease_until.asc())
            )
            if self.engine.dialect.name == "postgresql":
                statement = statement.with_for_update(skip_locked=True)
            for task in session.scalars(statement).all():
                previous = task.status
                attempts = int(task.attempts) + 1
                task.attempts = attempts
                task.error = (
                    f"lease expired while {previous} (held by "
                    f"{task.leased_by or 'unknown'}); reclaimed"
                )
                task.leased_by = ""
                task.lease_until = None
                task.scheduled_at = moment
                if attempts >= int(task.max_attempts):
                    task.status = "dead"
                    status = "dead"
                    task.finished_at = moment
                else:
                    task.status = "queued"
                    status = "queued"
                outcomes.append(ReclaimOutcome(
                    task_id=int(task.id), previous_status=previous,
                    status=status, attempts=attempts, reason=task.error,
                ))
            session.commit()

        for outcome in outcomes:
            if outcome.status == "dead":
                pass
            else:
                pass
        if not outcomes:
            pass
        return ReclaimReport(reclaimed=tuple(outcomes))

    # ── operator actions (POST /agent/tasks/<id>/retry|cancel) ───────

    def retry(self, task_id: int, *, reason: str = "manual retry") -> bool:
        """``failed|dead → queued``, attempts reset.  False if not retryable.

        A ``running`` or ``leased`` task is never touched here: another worker
        owns it, and stealing it is what ``cancel`` (an explicit stop) is for.
        """
        with self.session() as session:
            result = session.execute(
                update(AgentTask)
                .where(AgentTask.id == task_id, AgentTask.status.in_(("failed", "dead")))
                .values(
                    status="queued", attempts=0, leased_by="", lease_until=None,
                    scheduled_at=_now(), finished_at=None, result_ref=None,
                    error=f"requeued: {reason}",
                )
            )
            session.commit()
            return bool(result.rowcount)

    def cancel(self, task_id: int, *, reason: str = "cancelled") -> bool:
        """Stop a ``queued|leased|running`` task by parking it as ``dead``.

        The worker that held the lease will find its next heartbeat or outcome
        write rejected (rowcount 0), which is the cooperative cancellation
        signal; the sandbox killing itself is S4's job.
        """
        with self.session() as session:
            result = session.execute(
                update(AgentTask)
                .where(AgentTask.id == task_id, AgentTask.status.in_(("queued", "leased", "running")))
                .values(
                    status="dead", leased_by="", lease_until=None, finished_at=_now(),
                    error=f"cancelled: {reason}",
                )
            )
            session.commit()
            return bool(result.rowcount)

    # ── introspection ────────────────────────────────────────────────

    def get(self, task_id: int) -> dict[str, Any] | None:
        """One task as a dict (``AgentTask.to_dict()``), or ``None``."""
        with self.session() as session:
            task = session.get(AgentTask, task_id)
            return task.to_dict() if task is not None else None

    def stats(self, *, now: datetime | None = None) -> QueueStats:
        """Counts by status, plus how many are claimable and how many expired.

        ``ready`` applies the same disabled-runner predicate as :meth:`claim`,
        so a task skipped because its repository's runner is off is not counted
        as claimable.
        """
        moment = _now() if now is None else _as_utc(now)
        with self.session() as session:
            rows = session.execute(
                select(AgentTask.status, func.count()).group_by(AgentTask.status)
            ).all()
            by_status = {status: int(total) for status, total in rows}
            ready = session.scalar(
                select(func.count()).select_from(AgentTask).where(
                    AgentTask.status == "queued",
                    (AgentTask.scheduled_at.is_(None)) | (AgentTask.scheduled_at <= moment),
                    _runner_runnable(),
                )
            )
            expired = session.scalar(
                select(func.count()).select_from(AgentTask).where(
                    AgentTask.status.in_(("leased", "running")),
                    AgentTask.lease_until.is_not(None),
                    AgentTask.lease_until < moment,
                )
            )
        return QueueStats(
            by_status={status: by_status.get(status, 0) for status in TASK_STATUS},
            ready=int(ready or 0),
            expired=int(expired or 0),
        )


# ── Worker ───────────────────────────────────────────────────────────
#: A handler runs one claimed task.  Return value = ``result_ref`` (a string) or
#: ``None``; raising means "this attempt failed" and goes through the retry /
#: dead-letter path.  It receives the whole task so it can read ``payload``.

TaskHandler = Callable[[ClaimedTask], str | None]


def placeholder_handler(task: ClaimedTask) -> str | None:
    """Retire the task without doing any work — an explicit opt-in only.

    Kept for a dry run or a queue-only gate, **not** as a default: a deployment
    that retires tasks instantly is a deployment whose runner was never wired
    up, which is exactly the A1 defect :func:`default_handler` fixes.  It logs
    loudly for that reason.
    """
    return f"placeholder:{task.id}"


def default_handler(queue: "AgentQueue") -> TaskHandler:
    """The production handler: the real sandbox runner (``agent_worker``).

    Imported lazily so ``agent_queue`` stays importable without the runner's
    dependency graph (and so the queue can be exercised on its own).
    """
    from services.agent_worker import build_handler

    return build_handler(engine=queue.engine)


#: Factory seam: a gate swaps this out to substitute a fake handler for the CLI.
HandlerFactory = Callable[["AgentQueue"], TaskHandler]


class Worker:
    """The consuming half: reclaim, claim, run, heartbeat, settle.

    ``run_once`` does one full cycle and returns whether it did any work;
    ``run_loop`` repeats until ``stop_after`` tasks (or forever).  The handler is
    injected and its exceptions are *never* allowed to escape — a crashing model
    handler must fail one task, not the worker process.
    """

    def __init__(
        self,
        queue: AgentQueue,
        *,
        handler: TaskHandler | None = None,
        name: str | None = None,
        poll_interval: float = 2.0,
    ) -> None:
        self.queue = queue
        self.handler = handler if handler is not None else default_handler(queue)
        self.name = name or worker_id()
        self.poll_interval = float(poll_interval)

    def run_once(self) -> bool:
        """One cycle.  Returns True when a task was claimed and settled."""
        self.queue.reclaim_expired()
        with self.queue.lease(worker=self.name) as claimed:
            if claimed is None:
                return False
            self.queue.mark_running(claimed.lease)
            try:
                result_ref = self.handler(claimed)
            except Exception as exc:  # noqa: BLE001 - one task must not kill the worker
                self.queue.fail(claimed.lease, error=f"{type(exc).__name__}: {exc}")
                return True
            self.queue.succeed(claimed.lease, result_ref=result_ref)
            return True

    def run_loop(self, *, stop_after: int | None = None) -> int:
        """Poll until *stop_after* tasks are settled, or forever when ``None``.

        Returns the number of tasks settled.  Idle cycles sleep
        ``poll_interval`` seconds; the loop is deliberately a poll rather than a
        long-poll because the queue is a table and a worker restart must be
        cheap.
        """
        settled = 0
        # Dead-letter alerting: a task that exhausts its attempts becomes `dead`
        # and stops being retried.  Nothing else in the loop would tell an
        # operator, so re-read the count on a slow cadence and log at ERROR
        # whenever it grows (the API surface is
        # ``GET /api/v1/agent/tasks?status=dead``).
        idle_cycles = 0
        last_dead = -1
        dead_every = max(1, int(60.0 / max(0.1, self.poll_interval)))
        try:
            while stop_after is None or settled < stop_after:
                if self.run_once():
                    settled += 1
                    continue  # there may be more work — do not sleep on it
                idle_cycles += 1
                if idle_cycles % dead_every == 0:
                    try:
                        dead = int(self.queue.stats().by_status.get("dead", 0))
                    except Exception as exc:  # noqa: BLE001 - a stats blip is not fatal
                        dead = last_dead
                    if dead > last_dead:
                        pass
                    last_dead = dead
                time.sleep(self.poll_interval)
        except KeyboardInterrupt:  # pragma: no cover - interactive only
            pass
        return settled


def build_worker(
    queue: AgentQueue,
    *,
    handler: TaskHandler | None = None,
    handler_factory: HandlerFactory | None = None,
    name: str | None = None,
    poll_interval: float = 2.0,
) -> Worker:
    """Build the consumer, defaulting to the **real** sandbox runner handler.

    ``handler`` / ``handler_factory`` are the injectable seam: a gate substitutes
    a recording fake (or a factory) instead of a sandbox, which is how the
    production default is proven without a model, git or docker.
    """
    resolved = handler
    if resolved is None:
        resolved = (handler_factory or default_handler)(queue)
    return Worker(queue, handler=resolved, name=name, poll_interval=poll_interval)


# ── CLI (same shape as cli.py, a different binary) ───────────────────

def cmd_worker(args: argparse.Namespace) -> int:
    """``worker --once | --loop`` — the queue consumer."""
    engine = build_engine(args.db, echo=args.echo)
    queue = AgentQueue(
        engine,
        lease_seconds=args.lease_seconds,
        heartbeat_seconds=args.heartbeat_seconds,
    )
    queue.ensure_schema()

    if args.once:
        worker = build_worker(queue, name=args.worker, poll_interval=args.poll_interval)
        did_work = worker.run_once()
        if not did_work:
            pass
        engine.dispose()
        return 0

    worker = build_worker(queue, name=args.worker, poll_interval=args.poll_interval)
    try:
        settled = worker.run_loop(stop_after=args.max_tasks or None)
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        settled = 0
    finally:
        engine.dispose()
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    """``status`` — the queue's shape, for an operator or a health check."""
    engine = build_engine(args.db, echo=args.echo)
    queue = AgentQueue(engine)
    queue.ensure_schema()
    stats = queue.stats()
    for status in TASK_STATUS:
        pass
    engine.dispose()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m services.agent_queue",
        description="Agent Hub task queue — worker and operator commands.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  python -m services.agent_queue worker --once\n"
            "  python -m services.agent_queue worker --loop --poll-interval 2\n"
            "  python -m services.agent_queue status\n"
        ),
    )
    parser.add_argument(
        "--db", default=None,
        help=(
            "SQLite file path or SQLAlchemy URL.  Defaults to DATABASE_URL, or "
            f"the SQLite file {settings.storage.api_keys_file} when that is unset."
        ),
    )
    parser.add_argument("--echo", action="store_true", help="Log every SQL statement.")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("worker", help="Claim and run queued tasks.")
    p.add_argument("--once", action="store_true",
                   help="Run one cycle and exit (default when neither --once nor --loop).")
    p.add_argument("--loop", action="store_true",
                   help="Keep polling until interrupted or --max-tasks is reached.")
    p.add_argument("--max-tasks", type=int, default=0,
                   help="With --loop: stop after this many settled tasks (0 = forever).")
    p.add_argument("--worker", default=None, help="Worker name (default: host-pid-random).")
    p.add_argument("--poll-interval", type=float, default=2.0,
                   help="Seconds to sleep when the queue is empty (default: 2).")
    p.add_argument("--lease-seconds", type=float, default=DEFAULT_LEASE_SECONDS,
                   help=f"Lease length (default: {DEFAULT_LEASE_SECONDS:g}).")
    p.add_argument("--heartbeat-seconds", type=float, default=DEFAULT_HEARTBEAT_SECONDS,
                   help=f"Renewal interval (default: {DEFAULT_HEARTBEAT_SECONDS:g}).")
    p.set_defaults(func=cmd_worker, once=False)

    p = sub.add_parser("status", help="Show task counts by status.")
    p.set_defaults(func=cmd_status)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if getattr(args, "command", None) == "worker" and not args.loop:
        # `--once` is the documented default for the worker subcommand: running
        # `worker` with no flag should do a cycle and exit, not block forever.
        args.once = True
    try:
        return args.func(args)
    except ValueError as exc:
        return 2
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
