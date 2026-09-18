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
under the app, under the CLI, and under ``scripts/check_agent_hub.py``::

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

The worker's *work* is injected (``handler``): the platform ships a placeholder
that retires the task, and S4 replaces it with the sandbox runner.  The queue
itself never imports the runner, which is what keeps this module importable
without Flask or a Docker daemon.
"""

from __future__ import annotations

import argparse
import json
import logging
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

from sqlalchemy import create_engine, event, func, select, update
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session

from models.agent_hub import TASK_KIND, TASK_STATUS, AgentTask

logger = logging.getLogger("cpypiserver.agent_queue")

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

#: The fallback database when neither ``--db`` nor ``DATABASE_URL`` is set —
#: the same file ``config`` defaults to, so `python -m services.agent_queue`
#: outside Docker hits the deployment's own database.
DEFAULT_SQLITE_PATH = "data/cpypiserver.db"


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

    def to_dict(self) -> dict[str, Any]:
        """JSON-ready view (payload already parsed) for the worker and the API."""
        return {
            "id": self.id,
            "repo_id": self.repo_id,
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

    Deliberately narrower than ``extensions.database.resolve_database_url``: it
    resolves the two inputs the CLI has and stops, because importing
    ``config.settings`` here would make the queue unusable without the app's
    environment (and would drag pydantic-settings into the gate).  The rules are
    the same: a bare path is SQLite, ``postgres://`` is upgraded to the psycopg
    driver this project ships, anything with an explicit driver is left alone.
    """
    raw = (database if database is not None else os.environ.get("DATABASE_URL", "")) or ""
    value = raw.strip()
    if not value:
        return f"sqlite:///{DEFAULT_SQLITE_PATH}"
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
    ) -> int:
        """Append a task and return its id (the API hands this back to the SPA)."""
        if kind not in TASK_KIND:
            raise ValueError(f"unknown task kind {kind!r}; expected one of {TASK_KIND}")
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        with self.session() as session:
            task = AgentTask(
                repo_id=repo_id,
                kind=kind,
                status="queued",
                payload=_json(payload),
                priority=int(priority),
                max_attempts=int(max_attempts),
                attempts=0,
                leased_by="",
                scheduled_at=_as_utc(scheduled_at) if scheduled_at else _now(),
            )
            session.add(task)
            session.commit()
            task_id = int(task.id)
        logger.info("Enqueued %s task %d for repo %d", kind, task_id, repo_id)
        return task_id

    # ── consumer ─────────────────────────────────────────────────────

    def claim(self, *, worker: str, lease_seconds: float | None = None) -> ClaimedTask | None:
        """Atomically move the best queued task to ``leased`` for *worker*.

        "Best" is ``priority DESC, created_at ASC`` among the rows whose
        ``scheduled_at`` has arrived — served by
        ``index(status, priority, created_at)``.  Returns ``None`` when there is
        nothing to do, which is the worker's signal to sleep, not an error.

        Callers should prefer :meth:`lease`, which also starts the heartbeat.
        """
        lease_for = float(lease_seconds if lease_seconds is not None else self.lease_seconds)
        now = _now()
        expires = now + timedelta(seconds=lease_for)

        with self.session() as session:
            self._serialise_writes(session)
            if self.engine.dialect.name == "postgresql":
                statement = (
                    select(AgentTask)
                    .where(
                        AgentTask.status == "queued",
                        (AgentTask.scheduled_at.is_(None)) | (AgentTask.scheduled_at <= now),
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
        logger.info("Leased task %d (%s) to %s until %s",
                    claimed.id, claimed.kind, worker, expires.isoformat())
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
                    # Either the lease was lost (reclaimed) or the row is gone.
                    logger.warning("Heartbeat lost for task %d (%s) — worker %s "
                                   "must assume it no longer owns it",
                                   claimed.id, claimed.kind, worker)
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
                logger.warning("Task %d outcome ignored: lease held by %s is gone",
                               lease.task_id, lease.worker)
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
            logger.error("Task %d is dead after %d attempt(s): %s",
                         lease.task_id, attempts, error)
        else:
            logger.warning("Task %d failed (attempt %d) — retry scheduled: %s",
                           lease.task_id, attempts, error)
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
            logger.warning("Task %d outcome %r ignored: lease held by %s is gone",
                           lease.task_id, status, lease.worker)
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
                logger.error("Reclaimed task %d as dead after %d attempt(s)",
                             outcome.task_id, outcome.attempts)
            else:
                logger.warning("Reclaimed expired task %d (was %s, attempt %d) — requeued",
                               outcome.task_id, outcome.previous_status, outcome.attempts)
        if not outcomes:
            logger.debug("Reclaim sweep found no expired leases")
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
        """Counts by status, plus how many are claimable and how many expired."""
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
    """Default handler: retire the task without doing any work.

    Exists so ``--once``/``--loop`` are useful before S4 lands the sandbox
    runner, and so the queue gate can exercise the *worker* loop rather than
    only the queue methods.  It logs loudly: a deployment that is retiring tasks
    instantly is a deployment whose runner was never wired up.
    """
    logger.warning(
        "Retiring %s task %d with the placeholder handler — no sandbox runner is "
        "configured (payload=%s)", task.kind, task.id, task.payload)
    return f"placeholder:{task.id}"


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
        self.handler = handler or placeholder_handler
        self.name = name or worker_id()
        self.poll_interval = float(poll_interval)

    def run_once(self) -> bool:
        """One cycle.  Returns True when a task was claimed and settled."""
        self.queue.reclaim_expired()
        with self.queue.lease(worker=self.name) as claimed:
            if claimed is None:
                logger.debug("Worker %s found nothing to do", self.name)
                return False
            self.queue.mark_running(claimed.lease)
            try:
                result_ref = self.handler(claimed)
            except Exception as exc:  # noqa: BLE001 - one task must not kill the worker
                logger.exception("Task %d handler raised", claimed.id)
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
        logger.info("Worker %s started (poll=%ss, lease=%ss, heartbeat=%ss)",
                    self.name, self.poll_interval,
                    self.queue.lease_seconds, self.queue.heartbeat_seconds)
        try:
            while stop_after is None or settled < stop_after:
                if self.run_once():
                    settled += 1
                    continue  # there may be more work — do not sleep on it
                time.sleep(self.poll_interval)
        except KeyboardInterrupt:  # pragma: no cover - interactive only
            logger.info("Worker %s interrupted after %d task(s)", self.name, settled)
        logger.info("Worker %s stopped after %d task(s)", self.name, settled)
        return settled


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
        worker = Worker(queue, name=args.worker, poll_interval=args.poll_interval)
        did_work = worker.run_once()
        if not did_work:
            print("no task available")
        engine.dispose()
        return 0

    worker = Worker(queue, name=args.worker, poll_interval=args.poll_interval)
    try:
        settled = worker.run_loop(stop_after=args.max_tasks or None)
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        print("\ninterrupted")
        settled = 0
    finally:
        engine.dispose()
    print(f"{worker.name}: settled {settled} task(s)")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    """``status`` — the queue's shape, for an operator or a health check."""
    engine = build_engine(args.db, echo=args.echo)
    queue = AgentQueue(engine)
    queue.ensure_schema()
    stats = queue.stats()
    print(f"database : {_safe_url(resolve_database_url(args.db))}")
    for status in TASK_STATUS:
        print(f"  {status:<8} {stats.by_status.get(status, 0)}")
    print(f"  ready    {stats.ready}    expired leases {stats.expired}")
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
            f"the SQLite file {DEFAULT_SQLITE_PATH} when that is unset."
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
        print(f"error: {exc}")
        return 2
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
