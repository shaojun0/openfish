"""DB index/cache for the AI-maintained check suite (DESIGN-ai-checks.md §C).

**The repository tree is the source of truth.**  Everything in this module is
derived from it: a frozen suite snapshot and its provenance, the falsifiability
verdict for each check, and the per-check run history that makes a flaky or
never-failing check detectable.  Wiping these tables and re-cloning a repo
reconstructs every row that matters, which is why the runner still resolves the
suite from the tree and only *records* it here.

This replaces the earlier JSONL-only history: a shared file is wrong under
several worker replicas (concurrent appends, no query), and a table is what the
curator's dedup/rate-limit query needs anyway.  :class:`~services.check_validation.CheckHistoryStore`
stays as the in-memory test double; :class:`DbCheckHistoryStore` is the
production store.  There is no JSONL store left to compete with it.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterable
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session as SASession

from models.agent_hub import (
    AgentTask,
    CheckRun as CheckRunRow,
    CheckSuiteSnapshot,
    CheckValidation as CheckValidationRow,
)
from models.base import utcnow
from services.check_validation import (
    CheckHistorySummary,
    CheckRunRecord,
    CheckValidation,
    summarize_history,
)
from services.gates import CheckSuite, suite_fingerprint

logger = logging.getLogger("cpypiserver.check_store")

#: Session factory: a callable returning a fresh SQLAlchemy session.
SessionFactory = Callable[[], SASession]

#: Task statuses that count as "a proposal may still land".
OPEN_TASK_STATUSES: tuple[str, ...] = ("queued", "leased", "running")


# ── Suite snapshots + provenance ─────────────────────────────────────

def record_suite_snapshot(
    session: SASession,
    *,
    repo_id: int,
    kind: str,
    suite: CheckSuite,
    base_sha: str = "",
    author: str = "ai",
    model: str = "",
    task_id: int | None = None,
    status: str = "active",
) -> CheckSuiteSnapshot:
    """Insert or refresh the snapshot for ``(repo_id, kind, suite_hash)``.

    Idempotent by identity: re-recording the same suite updates provenance and
    status instead of growing a duplicate row, so "the active snapshot" is a
    plain lookup.
    """
    suite_hash = suite_fingerprint(suite)
    row = _snapshot_row(session, repo_id=repo_id, kind=kind, suite_hash=suite_hash)
    payload = json.dumps(
        [check.model_dump() for check in suite.checks], ensure_ascii=True
    )
    if row is None:
        row = CheckSuiteSnapshot(
            repo_id=int(repo_id),
            kind=str(kind),
            source=str(suite.source),
            suite_hash=suite_hash,
            checks=payload,
            base_sha=str(base_sha or ""),
            author=str(author or "ai"),
            model=str(model or ""),
            task_id=task_id,
            status=str(status),
        )
        session.add(row)
        logger.info(
            "recorded %s suite snapshot for repo %s (hash=%s, checks=%d)",
            kind, repo_id, suite_hash[:12], len(suite.checks),
        )
    else:
        row.source = str(suite.source)
        row.checks = payload
        row.base_sha = str(base_sha or "")
        row.author = str(author or row.author)
        row.model = str(model or row.model)
        row.task_id = task_id if task_id is not None else row.task_id
        row.status = str(status)
    session.flush()
    return row


def _snapshot_row(
    session: SASession,
    *,
    repo_id: int,
    kind: str,
    suite_hash: str,
) -> CheckSuiteSnapshot | None:
    return session.execute(
        select(CheckSuiteSnapshot)
        .where(
            CheckSuiteSnapshot.repo_id == int(repo_id),
            CheckSuiteSnapshot.kind == str(kind),
            CheckSuiteSnapshot.suite_hash == str(suite_hash),
        )
        .limit(1)
    ).scalars().first()


def active_suite(
    session: SASession,
    repo_id: int,
    *,
    kind: str = "ai",
) -> CheckSuiteSnapshot | None:
    """The most recently activated snapshot for a repo/kind, or ``None``."""
    return session.execute(
        select(CheckSuiteSnapshot)
        .where(
            CheckSuiteSnapshot.repo_id == int(repo_id),
            CheckSuiteSnapshot.kind == str(kind),
            CheckSuiteSnapshot.status == "active",
        )
        .order_by(CheckSuiteSnapshot.updated_at.desc(), CheckSuiteSnapshot.id.desc())
        .limit(1)
    ).scalars().first()


def suite_snapshot_payload(row: CheckSuiteSnapshot | None) -> dict[str, Any] | None:
    return row.to_dict() if row is not None else None


# ── Validation + run history ─────────────────────────────────────────

def record_validation(
    session: SASession,
    *,
    repo_id: int,
    result: CheckValidation,
    base_sha: str = "",
) -> CheckValidationRow:
    """Persist one falsifiability verdict."""
    row = CheckValidationRow(
        repo_id=int(repo_id),
        check_id=str(result.check_id),
        base_sha=str(base_sha or ""),
        detection_rate=float(result.detection_rate),
        faults_seeded=int(result.faults_attempted),
        validated=bool(result.validated),
        status=str(result.status),
        reason=str(result.reason)[:2000],
    )
    session.add(row)
    session.flush()
    return row


def record_runs(
    session: SASession,
    *,
    repo_id: int,
    records: Iterable[CheckRunRecord],
) -> int:
    """Append run-history rows; returns how many were written."""
    count = 0
    for record in records:
        session.add(CheckRunRow(
            repo_id=int(repo_id),
            check_id=str(record.check_id),
            suite_hash=str(record.suite_hash or ""),
            revision=str(record.commit_sha or ""),
            state=str(record.state),
            exit_code=record.exit_code,
            duration_ms=int(record.duration_ms or 0),
            weakened=bool(record.weakened),
        ))
        count += 1
    session.flush()
    return count


def _row_to_record(row: CheckRunRow, *, validation: str = "unvalidated") -> CheckRunRecord:
    return CheckRunRecord(
        check_id=str(row.check_id),
        state=str(row.state),
        suite_hash=str(row.suite_hash or ""),
        commit_sha=str(row.revision or ""),
        exit_code=row.exit_code,
        duration_ms=int(row.duration_ms or 0),
        at=row.created_at.isoformat() if row.created_at else "",
        validation=validation,
        weakened=bool(row.weakened),
    )


def history_records(
    session: SASession,
    repo_id: int,
    *,
    check_id: str | None = None,
) -> list[CheckRunRecord]:
    """Every recorded run for a repo (optionally one check), oldest first."""
    query = (
        select(CheckRunRow)
        .where(CheckRunRow.repo_id == int(repo_id))
        .order_by(CheckRunRow.id.asc())
    )
    if check_id is not None:
        query = query.where(CheckRunRow.check_id == str(check_id))
    rows = session.execute(query).scalars().all()
    validations = {
        str(row.check_id): str(row.status)
        for row in session.execute(
            select(CheckValidationRow)
            .where(CheckValidationRow.repo_id == int(repo_id))
            .order_by(CheckValidationRow.id.asc())
        ).scalars().all()
    }
    return [_row_to_record(row, validation=validations.get(str(row.check_id), "unvalidated"))
            for row in rows]


def history_summaries(
    session: SASession,
    repo_id: int,
) -> list[CheckHistorySummary]:
    return summarize_history(history_records(session, repo_id))


class DbCheckHistoryStore:
    """The production history store: :mod:`models.agent_hub` ``check_runs``.

    Same three-method surface as the in-memory double, so a caller can be handed
    either.  Each call opens and closes its own session — the store is shared
    across requests and must not hold one open.
    """

    def __init__(self, sessions: SessionFactory, repo_id: int) -> None:
        self._sessions = sessions
        self._repo_id = int(repo_id)

    def append(self, records: Iterable[CheckRunRecord]) -> None:
        rows = list(records)
        if not rows:
            return
        session = self._sessions()
        try:
            record_runs(session, repo_id=self._repo_id, records=rows)
            session.commit()
        finally:
            session.close()

    def records(self, *, check_id: str | None = None) -> list[CheckRunRecord]:
        session = self._sessions()
        try:
            return history_records(session, self._repo_id, check_id=check_id)
        finally:
            session.close()

    def summaries(self) -> list[CheckHistorySummary]:
        session = self._sessions()
        try:
            return history_summaries(session, self._repo_id)
        finally:
            session.close()


# ── Curator enqueue gate (dedup + rate limit) ────────────────────────

def curator_should_enqueue(
    session: SASession,
    repo_id: int,
    *,
    mode: str = "bootstrap",
    min_interval_seconds: int = 0,
) -> tuple[bool, str]:
    """Whether a new ``checks`` proposal may be queued for *repo_id*.

    Three guards, all cheap SQL:

    * **no open proposal** — a ``checks`` task already ``queued``/``leased``/
      ``running`` means a proposal is in flight; a second one would be a
      duplicate;
    * **bootstrap is once-only** — with ``mode="bootstrap"`` an existing AI
      snapshot means the repository has already been bootstrapped, so no further
      proposal is queued (this is what keeps ``bootstrap`` from becoming PR
      spam); ``mode="auto"`` skips that guard and relies on the cooldown;
    * **cooling-off** — no ``checks`` task created within
      ``min_interval_seconds``, so a burst of pushes cannot open a stack of PRs.

    The suite-hash dedup the design also asks for is enforced one level up, by
    :func:`proposal_in_flight_for_hash`, because it is the *resolved* hash that
    is only known once a run has cloned the repo.
    """
    open_task = session.execute(
        select(AgentTask)
        .where(
            AgentTask.repo_id == int(repo_id),
            AgentTask.kind == "checks",
            AgentTask.status.in_(OPEN_TASK_STATUSES),
        )
        .limit(1)
    ).scalars().first()
    if open_task is not None:
        return False, f"已有进行中的 checks 任务 #{open_task.id}（状态 {open_task.status}）"

    if str(mode) == "bootstrap" and has_ai_snapshot(session, repo_id):
        return False, "bootstrap 只提案一次：该仓库已有 AI 套件快照"

    if int(min_interval_seconds) > 0:
        cutoff = utcnow() - timedelta(seconds=int(min_interval_seconds))
        recent = session.execute(
            select(AgentTask)
            .where(
                AgentTask.repo_id == int(repo_id),
                AgentTask.kind == "checks",
                AgentTask.created_at >= cutoff,
            )
            .order_by(AgentTask.created_at.desc())
            .limit(1)
        ).scalars().first()
        if recent is not None:
            return False, f"冷却期内：{min_interval_seconds}s 内已有 checks 任务 #{recent.id}"
    return True, ""


def has_ai_snapshot(session: SASession, repo_id: int) -> bool:
    """Whether any AI suite snapshot has ever been recorded for *repo_id*."""
    row = session.execute(
        select(CheckSuiteSnapshot.id)
        .where(
            CheckSuiteSnapshot.repo_id == int(repo_id),
            CheckSuiteSnapshot.kind == "ai",
        )
        .limit(1)
    ).first()
    return row is not None


def proposal_in_flight_for_hash(
    session: SASession,
    repo_id: int,
    *,
    suite_hash: str,
) -> bool:
    """Whether an open ``checks`` task already targets *suite_hash*.

    The hash travels in the task payload, so this is a payload scan over the
    (tiny) set of open checks tasks for one repo — no JSON index needed.
    """
    if not suite_hash:
        return False
    rows = session.execute(
        select(AgentTask)
        .where(
            AgentTask.repo_id == int(repo_id),
            AgentTask.kind == "checks",
            AgentTask.status.in_(OPEN_TASK_STATUSES),
        )
    ).scalars().all()
    for row in rows:
        payload = row.payload_dict()
        if str(payload.get("suite_hash") or "") == str(suite_hash):
            return True
    return False


def record_proposal_snapshot(
    session: SASession,
    *,
    repo_id: int,
    suite: CheckSuite,
    base_sha: str,
    model: str,
    task_id: int | None,
    status: str = "proposed",
) -> CheckSuiteSnapshot:
    """A curator proposal is recorded as ``proposed`` until a human merges it."""
    return record_suite_snapshot(
        session,
        repo_id=repo_id,
        kind="ai",
        suite=suite,
        base_sha=base_sha,
        author="ai",
        model=model,
        task_id=task_id,
        status=status,
    )


__all__ = [
    "OPEN_TASK_STATUSES",
    "DbCheckHistoryStore",
    "SessionFactory",
    "active_suite",
    "curator_should_enqueue",
    "has_ai_snapshot",
    "history_records",
    "history_summaries",
    "proposal_in_flight_for_hash",
    "record_proposal_snapshot",
    "record_runs",
    "record_suite_snapshot",
    "record_validation",
    "suite_snapshot_payload",
]
