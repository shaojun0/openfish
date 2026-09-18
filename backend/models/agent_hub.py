"""Agent Hub tables — repos, the imported collaboration history, findings,
review runs and the agent task queue.

Spec: ``docs/agent-hub/DEVELOPMENT.md`` §4 (data model), §4.3 (enums) and §4.4
(fingerprint).  This module is the single source of truth for both the schema
and the status vocabularies; the services and routes above it import the
constants from here rather than spelling the strings again.

Timestamp representation
------------------------
The two historical styles in this project are ``DateTime(timezone=True)`` with
:func:`models.base.utcnow` (:mod:`models.rbac`) and plain ISO-8601 ``String(32)``
(:mod:`models.api_key`).  Agent Hub uses **DateTime**, because this domain
*compares and arithmetic-s on timestamps inside SQL* rather than only rendering
them: the queue matches leases with ``lease_until < now`` and
``scheduled_at <= now``, and the findings dashboard filters ``due`` ranges.
Keeping them as strings would push that arithmetic into Python and make the
lease query non-indexable.  ``to_dict()`` still renders ISO-8601 through
:func:`models.base.iso`, so the JSON contract is unchanged either way.

Relationship to the rest of the schema
--------------------------------------
There are deliberately **no foreign keys into ``users``** for the history
tables: an agent finding is attributed to an actor string (an OAuth subject)
exactly like ``api_keys.created_by``, so a repository imported from upstream
keeps its history even when the account that decided a finding is later
removed.  The one exception is :class:`GitIdentity`, which is a *live* mapping
rather than history — it is meaningless without its account, so it carries a
real ``users.id`` foreign key with ``ON DELETE CASCADE``.  Within the module,
foreign keys are real and cascade, because those rows are ours.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped

from services.digest import sha256_text

from .base import Base, iso, utcnow

# ── Status vocabularies (DEVELOPMENT.md §4.3) ────────────────────────
# "No scattered string literals" is a hard rule: services and routes import
# these tuples and the CHECK constraints below are generated from them, so a
# typo is an IntegrityError at write time rather than a row that only one
# query accidentally matches.

FINDING_STATUS = ("open", "acknowledged", "wontfix", "fixed", "stale")
FINDING_LEVEL = ("blocking", "debt")
FINDING_SEVERITY = ("critical", "high", "medium", "low")

TASK_STATUS = ("queued", "leased", "running", "done", "failed", "dead")
TASK_KIND = ("review", "fix", "import", "backfill")

REPO_SYNC_STATE = ("pending", "cloning", "issues", "indexing", "ready", "error")
REPO_KIND = ("upstream", "workspace")
REPO_SOURCE = ("import", "local")

IMPORT_MODE = ("code", "code+issues", "issues")
IMPORT_PHASE = ("migrate", "poll", "mirror_issues", "index_commits", "done")

ISSUE_STATE = ("open", "closed")
REVIEW_RUN_STATUS = ("running", "ok", "error")
EVIDENCE_RELATION = ("mentions", "duplicate_of", "fixed_by")


def _in_check(column: str, values: tuple[str, ...]) -> str:
    """SQL for ``column IN ('a', 'b', …)`` — the body of a CHECK constraint."""
    joined = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({joined})"


# ── Fingerprint (DEVELOPMENT.md §4.4, invariant I1) ──────────────────

def context_key(*parts: str | None) -> str:
    """Build a rule's *additional disambiguator* for :func:`fingerprint`.

    §4.4 keeps ``context_key`` empty unless "same rule, same location, but
    genuinely two problems" is true — ``debt.duplicate-implementation`` is the
    canonical case, where the second implementation's path is the disambiguator.
    Rules that need one call this with the stable identifiers they have;
    ``None`` parts are dropped.  The result is a single string that is
    ``""`` for the common case.
    """
    return "\x1f".join(part for part in parts if part)


def fingerprint(
    rule_id: str,
    file_path: str,
    symbol: str,
    context: str = "",
) -> str:
    """Stable identity of a finding — the *only* basis for de-duplication.

    ``sha256(f"{rule_id}\\x00{file_path}\\x00{symbol}\\x00{context}")``, exactly
    as specified in §4.4.  Note what is **not** in the input: line numbers,
    commit shas, timestamps and text summaries.  Any of those would make the
    same problem hash differently on the next run, which is invariant I1 and the
    "报告螺旋" the whole design exists to prevent.

    ``file_path`` is stored in the repository's own relative form (no leading
    ``./``, forward slashes) — the callers normalize it; the fingerprint cannot
    tell "the same file written two ways" apart.

    The digest itself comes from :func:`services.digest.sha256_text`: this module
    defines *what* is hashed, the digest module owns *how* — one SHA-256
    implementation for the whole project.
    """
    raw = f"{rule_id}\x00{file_path}\x00{symbol}\x00{context}"
    return sha256_text(raw)


# ── repos ────────────────────────────────────────────────────────────

class Repo(Base):
    """A repository openfish knows about — mirrored upstream, or a local workspace."""

    __tablename__ = "repos"
    __table_args__ = (
        CheckConstraint(_in_check("kind", REPO_KIND), name="ck_repos_kind"),
        CheckConstraint(_in_check("source", REPO_SOURCE), name="ck_repos_source"),
        CheckConstraint(_in_check("sync_state", REPO_SYNC_STATE), name="ck_repos_sync_state"),
    )

    id: Mapped[int] = Column(Integer, primary_key=True, autoincrement=True)
    # "<owner>/<name>" — the stable handle used in URLs and on the git side.
    slug: Mapped[str] = Column(String(256), nullable=False, unique=True, index=True)
    source: Mapped[str] = Column(String(16), nullable=False, default="import")
    source_url: Mapped[str | None] = Column(String(512), nullable=True)
    default_branch: Mapped[str] = Column(String(128), nullable=False, default="main")
    # Forgejo's full name (e.g. "openfish/vllm") once the migration created it.
    forgejo_repo: Mapped[str | None] = Column(String(256), nullable=True)
    # "upstream" is a read-only mirror (context source); "workspace" is writable.
    kind: Mapped[str] = Column(String(16), nullable=False, default="upstream")
    sync_state: Mapped[str] = Column(String(16), nullable=False, default="pending", index=True)
    synced_at: Mapped[datetime | None] = Column(DateTime(timezone=True), nullable=True)
    # Materialized counts so the repository list never fans out into N+1
    # COUNT(*) queries — the import pipeline is the only writer.
    issue_count: Mapped[int] = Column(Integer, nullable=False, default=0)
    commit_count: Mapped[int] = Column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = Column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "slug": self.slug,
            "source": self.source,
            "source_url": self.source_url,
            "default_branch": self.default_branch,
            "forgejo_repo": self.forgejo_repo,
            "kind": self.kind,
            "sync_state": self.sync_state,
            "synced_at": iso(self.synced_at),
            "issue_count": self.issue_count,
            "commit_count": self.commit_count,
            "created_at": iso(self.created_at),
            "updated_at": iso(self.updated_at),
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Repo {self.slug}>"


# ── import_jobs ──────────────────────────────────────────────────────

class ImportJob(Base):
    """One resumable run of the §8.2 import pipeline.

    ``cursor`` is the resumption point (last mirrored issue number / page), so a
    worker that dies mid-mirror continues instead of re-importing.  ``partial``
    records the ``IMPORT_MAX_ISSUES`` ceiling being hit: the API must surface
    that explicitly (silent truncation of a 20k-issue upstream is forbidden).
    """

    __tablename__ = "import_jobs"
    __table_args__ = (
        CheckConstraint(_in_check("mode", IMPORT_MODE), name="ck_import_jobs_mode"),
        CheckConstraint(_in_check("phase", IMPORT_PHASE), name="ck_import_jobs_phase"),
    )

    id: Mapped[int] = Column(Integer, primary_key=True, autoincrement=True)
    repo_id: Mapped[int] = Column(
        Integer, ForeignKey("repos.id", ondelete="CASCADE"), nullable=False, index=True
    )
    mode: Mapped[str] = Column(String(16), nullable=False, default="code+issues")
    status: Mapped[str] = Column(String(16), nullable=False, default="running", index=True)
    phase: Mapped[str] = Column(String(32), nullable=False, default="migrate")
    cursor: Mapped[str | None] = Column(String(256), nullable=True)
    progress: Mapped[int] = Column(Integer, nullable=False, default=0)
    total: Mapped[int] = Column(Integer, nullable=False, default=0)
    done: Mapped[int] = Column(Integer, nullable=False, default=0)
    partial: Mapped[bool] = Column(Boolean, nullable=False, default=False)
    error: Mapped[str | None] = Column(Text, nullable=True)
    started_at: Mapped[datetime | None] = Column(
        DateTime(timezone=True), nullable=True, default=utcnow
    )
    finished_at: Mapped[datetime | None] = Column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = Column(DateTime(timezone=True), nullable=False, default=utcnow)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "repo_id": self.repo_id,
            "mode": self.mode,
            "status": self.status,
            "phase": self.phase,
            "cursor": self.cursor,
            "progress": self.progress,
            "total": self.total,
            "done": self.done,
            "partial": bool(self.partial),
            "error": self.error,
            "started_at": iso(self.started_at),
            "finished_at": iso(self.finished_at),
            "created_at": iso(self.created_at),
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<ImportJob {self.id} repo={self.repo_id} {self.phase}>"


# ── repo_issues ──────────────────────────────────────────────────────

class RepoIssue(Base):
    """A mirrored upstream issue or pull request — *evidence*, never an instruction.

    ``body`` keeps the raw Markdown; rendering happens at read time via
    ``services/markdown.py``.  Imported text is untrusted (I6, §8.4): the
    context builder must wrap it in an explicit delimiter, and nothing in it may
    be executed as an agent instruction.
    """

    __tablename__ = "repo_issues"
    __table_args__ = (
        # Named so a freshly created table and an ALTER-migrated one end up with
        # the same index name (see models/agent_hub_migrate.py).
        UniqueConstraint("repo_id", "source_id", name="uq_repo_issues_repo_source"),
        Index("ix_repo_issues_repo_state", "repo_id", "state"),
        CheckConstraint(_in_check("state", ISSUE_STATE), name="ck_repo_issues_state"),
    )

    id: Mapped[int] = Column(Integer, primary_key=True, autoincrement=True)
    repo_id: Mapped[int] = Column(
        Integer, ForeignKey("repos.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # The source system's number.  Pull requests share the namespace, which is
    # why `is_pull_request` exists rather than a negative number.
    number: Mapped[int] = Column(Integer, nullable=False)
    is_pull_request: Mapped[bool] = Column(Boolean, nullable=False, default=False, index=True)
    title: Mapped[str] = Column(Text, nullable=False)
    body: Mapped[str | None] = Column(Text, nullable=True)
    state: Mapped[str] = Column(String(16), nullable=False, default="open")
    author: Mapped[str | None] = Column(String(256), nullable=True)
    # JSON array of label names.  TEXT + json rather than a JSON column: the
    # same DDL then works on SQLite and PostgreSQL with no dialect branch.
    labels: Mapped[str] = Column(Text, nullable=False, default="[]")
    milestone: Mapped[str | None] = Column(String(256), nullable=True)
    created_at: Mapped[datetime | None] = Column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime | None] = Column(DateTime(timezone=True), nullable=True)
    closed_at: Mapped[datetime | None] = Column(DateTime(timezone=True), nullable=True)
    # Permanent link in the source system, for the agent to cite.
    url: Mapped[str | None] = Column(String(512), nullable=True)
    # The source system's own id — the idempotent upsert key for re-imports.
    source_id: Mapped[str | None] = Column(String(128), nullable=True)
    imported_at: Mapped[datetime] = Column(DateTime(timezone=True), nullable=False, default=utcnow)

    def label_list(self) -> list[str]:
        """The labels as a list; a corrupt value degrades to ``[]``."""
        try:
            parsed = json.loads(self.labels or "[]")
        except ValueError:
            return []
        return [str(item) for item in parsed] if isinstance(parsed, list) else []

    def to_dict(self, *, include_body: bool = True) -> dict:
        d = {
            "id": self.id,
            "repo_id": self.repo_id,
            "number": self.number,
            "is_pull_request": bool(self.is_pull_request),
            "title": self.title,
            "state": self.state,
            "author": self.author,
            "labels": self.label_list(),
            "milestone": self.milestone,
            "created_at": iso(self.created_at),
            "updated_at": iso(self.updated_at),
            "closed_at": iso(self.closed_at),
            "url": self.url,
            "source_id": self.source_id,
            "imported_at": iso(self.imported_at),
        }
        if include_body:
            d["body"] = self.body
        return d

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<RepoIssue repo={self.repo_id} #{self.number}>"


# ── repo_commits ─────────────────────────────────────────────────────

class RepoCommit(Base):
    """Shallow commit metadata for context retrieval (never the git objects)."""

    __tablename__ = "repo_commits"
    __table_args__ = (
        UniqueConstraint("repo_id", "sha", name="uq_repo_commits_repo_sha"),
        Index("ix_repo_commits_repo_committed", "repo_id", "committed_at"),
    )

    id: Mapped[int] = Column(Integer, primary_key=True, autoincrement=True)
    repo_id: Mapped[int] = Column(
        Integer, ForeignKey("repos.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sha: Mapped[str] = Column(String(64), nullable=False)
    author: Mapped[str | None] = Column(String(256), nullable=True)
    author_email: Mapped[str | None] = Column(String(256), nullable=True)
    message: Mapped[str | None] = Column(Text, nullable=True)
    committed_at: Mapped[datetime | None] = Column(DateTime(timezone=True), nullable=True)
    parent_sha: Mapped[str | None] = Column(String(64), nullable=True)
    imported_at: Mapped[datetime] = Column(DateTime(timezone=True), nullable=False, default=utcnow)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "repo_id": self.repo_id,
            "sha": self.sha,
            "author": self.author,
            "author_email": self.author_email,
            "message": self.message,
            "committed_at": iso(self.committed_at),
            "parent_sha": self.parent_sha,
            "imported_at": iso(self.imported_at),
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<RepoCommit repo={self.repo_id} {self.sha[:8]}>"


# ── findings ─────────────────────────────────────────────────────────

class Finding(Base):
    """A rule hit with a lifecycle — the unit the whole design revolves around.

    ``fingerprint`` is the identity (§4.4); ``seen_count`` and
    ``last_seen_run_id`` are what make a re-observation visible instead of a
    duplicate row.  Invariants I2/I3 (``wontfix`` needs owner + due, blocking
    findings may not be deferred) are enforced at the API layer of S3; the
    columns here just carry the fields they need.
    """

    __tablename__ = "findings"
    __table_args__ = (
        UniqueConstraint("repo_id", "fingerprint", name="uq_findings_repo_fingerprint"),
        Index("ix_findings_repo_status", "repo_id", "status"),
        Index("ix_findings_repo_rule", "repo_id", "rule_id"),
        Index("ix_findings_status_due", "status", "due"),
        CheckConstraint(_in_check("level", FINDING_LEVEL), name="ck_findings_level"),
        CheckConstraint(_in_check("severity", FINDING_SEVERITY), name="ck_findings_severity"),
        CheckConstraint(_in_check("status", FINDING_STATUS), name="ck_findings_status"),
    )

    id: Mapped[int] = Column(Integer, primary_key=True, autoincrement=True)
    repo_id: Mapped[int] = Column(
        Integer, ForeignKey("repos.id", ondelete="CASCADE"), nullable=False, index=True
    )
    fingerprint: Mapped[str] = Column(String(64), nullable=False)
    # Which rule fired — a policy rule id, never free text.
    rule_id: Mapped[str] = Column(String(128), nullable=False, index=True)
    level: Mapped[str] = Column(String(16), nullable=False, default="debt")
    severity: Mapped[str] = Column(String(16), nullable=False, default="medium")
    status: Mapped[str] = Column(String(16), nullable=False, default="open", index=True)
    # The symbol anchor from §4.4: a function/class/method name, or a stable
    # file-level anchor when there is none.  Never a line number.
    file_path: Mapped[str] = Column(String(512), nullable=False)
    symbol: Mapped[str] = Column(String(256), nullable=False, default="")
    # A hint for the human reading the report only — it is *not* part of the
    # identity and must never enter the fingerprint.
    line_hint: Mapped[int | None] = Column(Integer, nullable=True)
    title: Mapped[str] = Column(Text, nullable=False)
    detail: Mapped[str | None] = Column(Text, nullable=True)
    first_seen_run_id: Mapped[int | None] = Column(
        Integer, ForeignKey("review_runs.id", ondelete="SET NULL"), nullable=True
    )
    last_seen_run_id: Mapped[int | None] = Column(
        Integer, ForeignKey("review_runs.id", ondelete="SET NULL"), nullable=True
    )
    seen_count: Mapped[int] = Column(Integer, nullable=False, default=1)
    # Required (with `due`) for acknowledged / wontfix — invariant I2.
    owner: Mapped[str | None] = Column(String(256), nullable=True)
    due: Mapped[date | None] = Column(Date, nullable=True)
    decided_by: Mapped[str | None] = Column(String(256), nullable=True)
    decided_at: Mapped[datetime | None] = Column(DateTime(timezone=True), nullable=True)
    pr_url: Mapped[str | None] = Column(String(512), nullable=True)
    created_at: Mapped[datetime] = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = Column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "repo_id": self.repo_id,
            "fingerprint": self.fingerprint,
            "rule_id": self.rule_id,
            "level": self.level,
            "severity": self.severity,
            "status": self.status,
            "file_path": self.file_path,
            "symbol": self.symbol,
            "line_hint": self.line_hint,
            "title": self.title,
            "detail": self.detail,
            "first_seen_run_id": self.first_seen_run_id,
            "last_seen_run_id": self.last_seen_run_id,
            "seen_count": self.seen_count,
            "owner": self.owner,
            "due": self.due.isoformat() if self.due is not None else None,
            "decided_by": self.decided_by,
            "decided_at": iso(self.decided_at),
            "pr_url": self.pr_url,
            "created_at": iso(self.created_at),
            "updated_at": iso(self.updated_at),
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Finding {self.id} {self.rule_id} {self.status}>"


# ── finding_events ───────────────────────────────────────────────────

class FindingEvent(Base):
    """Append-only status transition history — "why is this finding like this?"."""

    __tablename__ = "finding_events"
    __table_args__ = (Index("ix_finding_events_finding_at", "finding_id", "at"),)

    id: Mapped[int] = Column(Integer, primary_key=True, autoincrement=True)
    finding_id: Mapped[int] = Column(
        Integer, ForeignKey("findings.id", ondelete="CASCADE"), nullable=False, index=True
    )
    at: Mapped[datetime] = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    actor: Mapped[str | None] = Column(String(256), nullable=True)
    from_status: Mapped[str | None] = Column(String(16), nullable=True)
    to_status: Mapped[str] = Column(String(16), nullable=False)
    reason: Mapped[str | None] = Column(Text, nullable=True)
    # The review run that observed/decided it, when there was one.
    run_id: Mapped[int | None] = Column(
        Integer, ForeignKey("review_runs.id", ondelete="SET NULL"), nullable=True
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "finding_id": self.finding_id,
            "at": iso(self.at),
            "actor": self.actor,
            "from_status": self.from_status,
            "to_status": self.to_status,
            "reason": self.reason,
            "run_id": self.run_id,
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<FindingEvent {self.id} {self.from_status}->{self.to_status}>"


# ── review_runs ──────────────────────────────────────────────────────

class ReviewRun(Base):
    """One execution of the review protocol (§9.3) over one commit.

    ``policy_hash`` is why a run is comparable to its predecessor: when the
    policy file changes, the next run's PR description must say so rather than
    pretending the two reports are the same measurement.
    """

    __tablename__ = "review_runs"
    __table_args__ = (
        Index("ix_review_runs_repo_started", "repo_id", "started_at"),
        CheckConstraint(_in_check("status", REVIEW_RUN_STATUS), name="ck_review_runs_status"),
    )

    id: Mapped[int] = Column(Integer, primary_key=True, autoincrement=True)
    repo_id: Mapped[int] = Column(
        Integer, ForeignKey("repos.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # NULL when a run was started by hand rather than by a queued task.
    agent_task_id: Mapped[int | None] = Column(
        Integer, ForeignKey("agent_tasks.id", ondelete="SET NULL"), nullable=True, index=True
    )
    commit_sha: Mapped[str] = Column(String(64), nullable=False)
    policy_hash: Mapped[str | None] = Column(String(64), nullable=True)
    findings_new: Mapped[int] = Column(Integer, nullable=False, default=0)
    findings_matched: Mapped[int] = Column(Integer, nullable=False, default=0)
    gates_total: Mapped[int] = Column(Integer, nullable=False, default=0)
    gates_passed: Mapped[int] = Column(Integer, nullable=False, default=0)
    gates_failed: Mapped[int] = Column(Integer, nullable=False, default=0)
    status: Mapped[str] = Column(String(16), nullable=False, default="running", index=True)
    log_ref: Mapped[str | None] = Column(String(512), nullable=True)
    started_at: Mapped[datetime] = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    finished_at: Mapped[datetime | None] = Column(DateTime(timezone=True), nullable=True)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "repo_id": self.repo_id,
            "agent_task_id": self.agent_task_id,
            "commit_sha": self.commit_sha,
            "policy_hash": self.policy_hash,
            "findings_new": self.findings_new,
            "findings_matched": self.findings_matched,
            "gates_total": self.gates_total,
            "gates_passed": self.gates_passed,
            "gates_failed": self.gates_failed,
            "status": self.status,
            "log_ref": self.log_ref,
            "started_at": iso(self.started_at),
            "finished_at": iso(self.finished_at),
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<ReviewRun {self.id} {self.commit_sha[:8]} {self.status}>"


# ── agent_tasks ──────────────────────────────────────────────────────

class AgentTask(Base):
    """A queued unit of agent work — the table ``services/agent_queue.py`` drives.

    The status machine is ``queued → leased → running → done|failed|dead``.
    ``index(status, priority, created_at)`` is the lease query's index: it
    filters on status and orders by the other two, so the claim is an index
    scan, not a sort of the whole table.
    """

    __tablename__ = "agent_tasks"
    __table_args__ = (
        Index("ix_agent_tasks_status_priority_created", "status", "priority", "created_at"),
        Index("ix_agent_tasks_lease_until", "lease_until"),
        CheckConstraint(_in_check("status", TASK_STATUS), name="ck_agent_tasks_status"),
        CheckConstraint(_in_check("kind", TASK_KIND), name="ck_agent_tasks_kind"),
    )

    id: Mapped[int] = Column(Integer, primary_key=True, autoincrement=True)
    repo_id: Mapped[int] = Column(
        Integer, ForeignKey("repos.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[str] = Column(String(16), nullable=False, default="review")
    status: Mapped[str] = Column(String(16), nullable=False, default="queued")
    # JSON: target sha, issue number, finding ids — whatever the runner needs.
    payload: Mapped[str] = Column(Text, nullable=False, default="{}")
    # Empty string (not NULL) for "unleased", so the reclaim comparison
    # `leased_by != :worker` is plain SQL and a cleared lease is one uniform
    # value instead of a NULL special case.
    leased_by: Mapped[str] = Column(String(128), nullable=False, default="")
    # Heartbeat deadline.  NULL means "not leased"; a worker renews this every
    # 15s and another worker reclaims the row once it is in the past.
    lease_until: Mapped[datetime | None] = Column(DateTime(timezone=True), nullable=True)
    # "Available at": set on enqueue and pushed into the future by retry
    # back-off, so a failing task does not spin.
    scheduled_at: Mapped[datetime | None] = Column(DateTime(timezone=True), nullable=True)
    attempts: Mapped[int] = Column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = Column(Integer, nullable=False, default=3)
    # Higher wins: the lease query is ORDER BY priority DESC, created_at ASC.
    priority: Mapped[int] = Column(Integer, nullable=False, default=0)
    result_ref: Mapped[str | None] = Column(String(512), nullable=True)
    # Last failure/reclaim explanation — the operator's only clue for a `dead` row
    # besides the worker log (a dead task is an ops-log event, not a finding).
    error: Mapped[str | None] = Column(Text, nullable=True)
    created_at: Mapped[datetime] = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    started_at: Mapped[datetime | None] = Column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = Column(DateTime(timezone=True), nullable=True)

    def payload_dict(self) -> dict[str, Any]:
        """The payload as a dict; a corrupt value degrades to ``{}``."""
        try:
            parsed = json.loads(self.payload or "{}")
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def to_dict(self, *, include_payload: bool = True) -> dict:
        d = {
            "id": self.id,
            "repo_id": self.repo_id,
            "kind": self.kind,
            "status": self.status,
            "leased_by": self.leased_by or None,
            "lease_until": iso(self.lease_until),
            "scheduled_at": iso(self.scheduled_at),
            "attempts": self.attempts,
            "max_attempts": self.max_attempts,
            "priority": self.priority,
            "result_ref": self.result_ref,
            "error": self.error,
            "created_at": iso(self.created_at),
            "started_at": iso(self.started_at),
            "finished_at": iso(self.finished_at),
        }
        if include_payload:
            d["payload"] = self.payload_dict()
        return d

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<AgentTask {self.id} {self.kind} {self.status}>"


# ── git_identities ───────────────────────────────────────────────────

class GitIdentity(Base):
    """The per-user bridge from an openfish account to a Forgejo credential.

    Spec: ``DEVELOPMENT.md`` §5.2 (the git credential hand-off) and §13.1 (the
    push credential chain this table closes).  Forgejo authenticates
    ``git-receive-pack`` itself, so an openfish API key can never be the push
    password; the platform keeps one *dedicated* Forgejo account per openfish
    user and stores the Forgejo access token it minted for that account.

    ``forgejo_username`` is derived deterministically from
    ``(user_id, external_id)`` by ``services.git_identity.derive_username`` and
    is effectively permanent: commit attribution and audit hang off it, so a
    rename would orphan every past commit.  Treat the derivation rule as frozen.

    ``token_ciphertext`` is a Fernet-sealed envelope holding the Forgejo token
    and the scope it was minted with — **no plaintext Forgejo token may ever
    reach this table**.  ``services.git_identity`` is the only writer and the key
    comes from the ``GIT_IDENTITY_KEY`` environment variable.
    ``token_expires_at`` is this platform's rotation deadline (Forgejo access
    tokens carry none of their own), ``rotated_at`` the last mint and
    ``revoked_at`` a deactivation that the service can revive.
    """

    __tablename__ = "git_identities"

    id: Mapped[int] = Column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = Column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    # Forgejo-side login name.  Unique because it is the remote account key.
    forgejo_username: Mapped[str] = Column(String(64), nullable=False, unique=True)
    external_id: Mapped[str] = Column(String(256), nullable=False)
    token_ciphertext: Mapped[str | None] = Column(Text, nullable=True)
    token_expires_at: Mapped[datetime | None] = Column(DateTime(timezone=True), nullable=True)
    rotated_at: Mapped[datetime | None] = Column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = Column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = Column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    def to_dict(self) -> dict:
        # The ciphertext is deliberately absent: this document is for the
        # console, and the only thing it may learn is *whether* a token exists.
        return {
            "id": self.id,
            "user_id": self.user_id,
            "forgejo_username": self.forgejo_username,
            "external_id": self.external_id,
            "has_token": bool(self.token_ciphertext),
            "token_expires_at": iso(self.token_expires_at),
            "rotated_at": iso(self.rotated_at),
            "revoked_at": iso(self.revoked_at),
            "created_at": iso(self.created_at),
            "updated_at": iso(self.updated_at),
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<GitIdentity user={self.user_id} {self.forgejo_username}>"


# ── finding_evidence ─────────────────────────────────────────────────

class FindingEvidence(Base):
    """Link from a finding to the historical issue/PR that discusses it (§4.6).

    Deterministic links (the finding's path appearing in an issue body) take
    effect immediately; an agent-suggested link lands as ``pending`` and waits
    for a human in the console.  ``confidence``/``rationale`` are what the human
    decides on.
    """

    __tablename__ = "finding_evidence"
    __table_args__ = (
        UniqueConstraint(
            "finding_id", "repo_issue_id", "relation",
            name="uq_finding_evidence_triple",
        ),
        Index("ix_finding_evidence_finding", "finding_id"),
        CheckConstraint(
            _in_check("relation", EVIDENCE_RELATION), name="ck_finding_evidence_relation"
        ),
        CheckConstraint(
            _in_check("status", ("pending", "confirmed", "rejected")),
            name="ck_finding_evidence_status",
        ),
    )

    id: Mapped[int] = Column(Integer, primary_key=True, autoincrement=True)
    finding_id: Mapped[int] = Column(
        Integer, ForeignKey("findings.id", ondelete="CASCADE"), nullable=False
    )
    repo_issue_id: Mapped[int] = Column(
        Integer, ForeignKey("repo_issues.id", ondelete="CASCADE"), nullable=False, index=True
    )
    relation: Mapped[str] = Column(String(16), nullable=False, default="mentions")
    # "deterministic" (from the matcher) | "agent" (a suggestion needing review).
    origin: Mapped[str] = Column(String(16), nullable=False, default="deterministic")
    status: Mapped[str] = Column(String(16), nullable=False, default="pending")
    confidence: Mapped[int | None] = Column(Integer, nullable=True)
    rationale: Mapped[str | None] = Column(Text, nullable=True)
    created_at: Mapped[datetime] = Column(DateTime(timezone=True), nullable=False, default=utcnow)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "finding_id": self.finding_id,
            "repo_issue_id": self.repo_issue_id,
            "relation": self.relation,
            "origin": self.origin,
            "status": self.status,
            "confidence": self.confidence,
            "rationale": self.rationale,
            "created_at": iso(self.created_at),
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<FindingEvidence finding={self.finding_id} issue={self.repo_issue_id}>"


__all__ = [
    # enums
    "EVIDENCE_RELATION",
    "FINDING_LEVEL",
    "FINDING_SEVERITY",
    "FINDING_STATUS",
    "IMPORT_MODE",
    "IMPORT_PHASE",
    "ISSUE_STATE",
    "REPO_KIND",
    "REPO_SOURCE",
    "REPO_SYNC_STATE",
    "REVIEW_RUN_STATUS",
    "TASK_KIND",
    "TASK_STATUS",
    # helpers
    "context_key",
    "fingerprint",
    # tables
    "AgentTask",
    "Finding",
    "FindingEvent",
    "FindingEvidence",
    "GitIdentity",
    "ImportJob",
    "Repo",
    "RepoCommit",
    "RepoIssue",
    "ReviewRun",
]
