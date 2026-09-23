#!/usr/bin/env python
"""Gate: the repository slice works end to end, with no Forgejo and no Flask.

Run from the backend directory (``backend/``)::

    python scripts/check_agent_repos.py

The point of this gate is that §5.3 / §5.4 / §8.2 are *contracts*, and every
one of them is exercised here against a substitute for the outside world:

1. **source URL parsing** — github.com, gitee.com, gitlab.com (including a
   subgroup) and a bare https git host all land on the right ``owner/name``;
2. **idempotent issue upsert** — the same page mirrored twice produces no
   duplicate rows, and a re-seen ``source_id`` updates in place;
3. **the large-repo ceiling** — hitting ``IMPORT_MAX_ISSUES`` ends the job
   ``partial`` **and** the progress document says so (§8.2 forbids a silent
   truncation), including on a model with no ``partial`` column yet;
4. **resumability** — the pipeline is driven one page at a time; after a
   checkpoint the next run continues from the stored cursor and never re-fetches
   a page it already committed;
5. **webhook HMAC** — a correct ``X-Forgejo-Signature`` passes, a tampered one
   is rejected, and §5.4's event table produces exactly the right tasks;
6. **monotonic progress** — ``ImportJob.progress`` never goes backwards across
   any commit the pipeline makes.

The Forgejo client, the commit reader and the agent queue are all injected, and
the database is a throwaway SQLite file.  Nothing here talks to the network,
starts Flask, or needs S0 to have landed before it can run.

Model note: S0 owns ``models/agent_hub.py``.  This gate builds *equivalent*
SQLAlchemy tables in memory and hands them to ``services.repo_import.set_models``
so the pipeline can run today.  The test schema is §4.2 plus the ``partial``
column §8.2 requires; a build whose ``ImportJob`` lacks ``partial`` is covered
by scenario 5, which proves the REST layer still reports truncation.
"""

from __future__ import annotations

import json
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from sqlalchemy import (  # noqa: E402
    Boolean, CheckConstraint, Column, DateTime, Integer, MetaData, String, Text,
    create_engine, event, select,
)
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker  # noqa: E402

#: ``in_check`` is the model layer's own CHECK-expression builder.  The fake
#: ``import_jobs`` below reuses it so the substitute table carries the real
#: ``ck_import_jobs_phase``; without it ``ensure_schema`` would rebuild the
#: substitute into the real DDL and the fake ORM class would no longer match.
from models.agent_hub import IMPORT_PHASE, TASK_KIND  # noqa: E402
from models.base import in_check  # noqa: E402
from routes.repo_webhook import (  # noqa: E402
    AGENT_LABEL, PRIORITY_DOC, PRIORITY_FIX, PRIORITY_REVIEW, backfill_pr_url,
    compute_signature, enqueue_task, parse_event, plan_actions, verify_signature,
)
from services import repo_import  # noqa: E402
from services.repo_import import (  # noqa: E402
    Cursor, ForgejoClient, ImportConfig, phase_progress,
)

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


# ── Throwaway tables (stand-in for S0's models.agent_hub) ────────────

def _build_models(*, with_partial: bool) -> tuple[dict[str, Any], MetaData]:
    """Model classes matching §4.2, optionally without the ``partial`` column.

    Each call builds a *fresh* metadata object, so scenarios cannot collide on
    a table name — the models are throwaway test doubles, not the real schema.
    """
    metadata = MetaData()

    # A fresh declarative base per harness.  DeclarativeBase carries the
    # metadata as a *class attribute*, so the mapping is built inside a class
    # body rather than by subclassing (which would inherit the outer scope's
    # problem: a nested class body cannot see the enclosing function's locals).
    class _Fresh(DeclarativeBase):
        pass

    _Fresh.metadata = metadata
    Base = _Fresh

    class Repo(Base):
        __tablename__ = "repos"
        id = Column(Integer, primary_key=True, autoincrement=True)
        slug = Column(String(256), unique=True, nullable=False)
        source = Column(String(32), nullable=False, default="import")
        source_url = Column(String(512))
        default_branch = Column(String(128), nullable=False, default="main")
        forgejo_repo = Column(String(256))
        kind = Column(String(32), nullable=False, default="upstream")
        sync_state = Column(String(32), nullable=False, default="pending")
        synced_at = Column(DateTime(timezone=True))
        issue_count = Column(Integer, nullable=False, default=0)
        commit_count = Column(Integer, nullable=False, default=0)
        # The webhook reads the cached per-repo review policy off these columns
        # (``None`` = "not read yet" → the documented default), so the substitute
        # table must carry them or the server-side Repo query selects a missing
        # column.
        auto_review = Column(Boolean, nullable=True)
        curator = Column(String(16), nullable=True)
        curator_min_interval_seconds = Column(Integer, nullable=True)
        created_at = Column(DateTime(timezone=True))
        updated_at = Column(DateTime(timezone=True))

    class ImportJob(Base):
        __tablename__ = "import_jobs"
        #: Same name and literals as the model's generated CHECK: this is what
        #: stops ``models.agent_hub_migrate.evolve_check_constraints`` from
        #: rebuilding the substitute table out from under the fake ORM class.
        __table_args__ = (
            CheckConstraint(in_check("phase", IMPORT_PHASE),
                            name="ck_import_jobs_phase"),
        )
        id = Column(Integer, primary_key=True, autoincrement=True)
        repo_id = Column(Integer, nullable=False)
        mode = Column(String(32), nullable=False, default="code+issues")
        status = Column(String(32), nullable=False, default="queued")
        phase = Column(String(32), nullable=False, default="validate")
        cursor = Column(Text)
        progress = Column(Integer, nullable=False, default=0)
        total = Column(Integer, nullable=False, default=0)
        done = Column(Integer, nullable=False, default=0)
        include_prs = Column(Boolean, nullable=False, default=True)
        error = Column(Text)
        started_at = Column(DateTime(timezone=True))
        finished_at = Column(DateTime(timezone=True))
        created_at = Column(DateTime(timezone=True))
        if with_partial:
            partial = Column(Boolean, nullable=False, default=False)

    class RepoIssue(Base):
        __tablename__ = "repo_issues"
        id = Column(Integer, primary_key=True, autoincrement=True)
        repo_id = Column(Integer, nullable=False)
        number = Column(Integer, nullable=False)
        is_pull_request = Column(Boolean, nullable=False, default=False)
        title = Column(Text)
        body = Column(Text)
        state = Column(String(32))
        author = Column(String(256))
        labels = Column(Text)
        milestone = Column(String(256))
        created_at = Column(DateTime(timezone=True))
        updated_at = Column(DateTime(timezone=True))
        closed_at = Column(DateTime(timezone=True))
        url = Column(String(512))
        source_id = Column(String(128))

    class RepoCommit(Base):
        __tablename__ = "repo_commits"
        id = Column(Integer, primary_key=True, autoincrement=True)
        repo_id = Column(Integer, nullable=False)
        sha = Column(String(64), nullable=False)
        message = Column(Text)
        author = Column(String(256))
        committed_at = Column(DateTime(timezone=True))
        source = Column(String(16))
        created_at = Column(DateTime(timezone=True))
        updated_at = Column(DateTime(timezone=True))

    # The webhook route imports this model itself (it is not part of the S1 model
    # injection), so the substitute only has to create the matching table.
    class WebhookDelivery(Base):
        __tablename__ = "webhook_deliveries"
        id = Column(Integer, primary_key=True, autoincrement=True)
        delivery_id = Column(String(128), unique=True, nullable=False, index=True)
        repo_id = Column(Integer)
        event = Column(String(32))
        received_at = Column(DateTime(timezone=True))

    return {
        "Repo": Repo,
        "ImportJob": ImportJob,
        "RepoIssue": RepoIssue,
        "RepoCommit": RepoCommit,
    }, metadata


class _Harness:
    """A temp SQLite database plus the injected doubles."""

    def __init__(self, *, with_partial: bool = True) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="openfish-repos-")
        self.engine = create_engine(f"sqlite:///{Path(self.tmp.name) / 'hub.db'}")
        self.models, metadata = _build_models(with_partial=with_partial)
        metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        self._previous_models = repo_import.models()
        repo_import.set_models(**self.models)
        self.progress_history: list[int] = []
        self._watch_progress()
        self.client = FakeForgejo()
        self.git = FakeGit()
        self.config = ImportConfig(
            base_url="http://forgejo.invalid:3000",
            git_base_url="http://forgejo.invalid:3000",
            admin_token="test-admin-token",
            webhook_secret="test-webhook-secret",
            owner="openfish",
            public_base_url="/git",
            max_issues=20000,
            max_commits=5000,
            max_rate=100000.0,
            page_size=50,
            poll_interval=0.0,
            poll_attempts=8,
            http_timeout=5.0,
            mirror_dir=str(Path(self.tmp.name) / "mirror"),
        )

    # -- lifecycle ----------------------------------------------------

    def close(self) -> None:
        repo_import.set_models(**self._previous_models)
        self.engine.dispose()
        self.tmp.cleanup()

    def _watch_progress(self) -> None:
        """Record ``ImportJob.progress`` on **every** commit, engine-wide.

        A per-session ``after_commit`` listener looks simpler but cannot read
        during the commit it is reacting to; attaching to the engine's
        ``commit`` event instead means one listener covers every session the
        pipeline opens, which is what makes "progress never went backwards" a
        real assertion.  ``commit`` fires *after* the transaction is durable, so
        reading the row back sees exactly what a restarted worker would.
        """
        job_cls = self.models["ImportJob"]
        history = self.progress_history

        @event.listens_for(self.engine, "commit")
        def _record(conn) -> None:
            try:
                value = conn.execute(
                    select(job_cls.progress).order_by(job_cls.id.desc()).limit(1)
                ).scalar()
            except Exception:  # noqa: BLE001 - diagnostics only
                return
            if value is not None:
                history.append(int(value))

    def session(self) -> Session:
        """A plain session; progress is watched at the engine level."""
        return self.Session()

    def service(self, *, queue: Any | None = None, config: ImportConfig | None = None):
        cfg = config or self.config
        return repo_import.RepoImportService(
            self.session(),
            client=self.client,
            commits=self.git,
            config=cfg,
            queue=queue,
        )


# ── Fakes ────────────────────────────────────────────────────────────

@dataclass
class FakeForgejo:
    """A scripted Forgejo: no sockets, deterministic pages, recorded calls."""

    issues: list[dict[str, Any]] = field(default_factory=list)
    commits: list[dict[str, Any]] = field(default_factory=list)
    migrate_statuses: list[dict[str, Any]] = field(default_factory=list)
    migrate_calls: list[dict[str, Any]] = field(default_factory=list)
    issue_pages: list[int] = field(default_factory=list)
    commit_pages: list[int] = field(default_factory=list)
    state_calls: int = 0
    probe_calls: int = 0
    page_size: int = 50

    def probe_source(self, source):  # noqa: ANN001 - mirrors the real client
        self.probe_calls += 1
        return {"reachable": True, "open_issues": len(self.issues),
                "detail": source.slug}

    def trigger_migration(self, source, *, owner, name, **kwargs):  # noqa: ANN001
        self.migrate_calls.append({"slug": source.slug, "owner": owner,
                                   "name": name, **kwargs})
        return {"full_name": f"{owner}/{name}", "name": name}

    def migration_state(self, forgejo_repo):  # noqa: ANN001
        self.state_calls += 1
        if self.migrate_statuses:
            return self.migrate_statuses.pop(0)
        return {"full_name": forgejo_repo, "migrating": False}

    @staticmethod
    def migration_finished(state):  # noqa: ANN001 - same predicate as production
        return ForgejoClient.migration_finished(state)

    def stream_issues(self, forgejo_repo, *, state="all", after_number=0,
                      offset=0, per_page=50, **kwargs):  # noqa: ANN001
        """Mirror the real client's contract: offset in, next offset out.

        ``after_number`` is applied *after* the window, exactly as the live
        client does, so a fake that silently got the offset semantics wrong
        would be caught by the "all 120 issues mirrored" check.
        """
        self.issue_pages.append(int(offset))
        window = self.issues[offset:offset + per_page]
        if after_number:
            window = [row for row in window if int(row["number"]) > after_number]
        next_offset = offset + per_page if offset + per_page < len(self.issues) else None
        return repo_import.IssuePage(items=list(window), next_page=None,
                                     next_offset=next_offset,
                                     total=len(self.issues))

    def stream_commits(self, forgejo_repo, *, branch="main", page=1, per_page=50,
                       **kwargs):  # noqa: ANN001
        self.commit_pages.append(int(page))
        offset = (int(page) - 1) * per_page
        window = self.commits[offset:offset + per_page]
        next_page = int(page) + 1 if offset + per_page < len(self.commits) else None
        return repo_import.CommitPage(items=list(window), next_page=next_page)


class FakeGit:
    """The read-only git copy, faked: the pipeline's fallback path."""

    def __init__(self) -> None:
        self.calls = 0
        self.limit = 0
        self.commits: list[dict[str, Any]] = []
        self.fail = ""

    def stream(self, forgejo_repo, *, branch="main", limit=5000):  # noqa: ANN001
        self.calls += 1
        self.limit = int(limit)
        if self.fail:
            raise repo_import.RepoImportError(self.fail)
        return iter(self.commits[: int(limit)])

    def close(self) -> None:
        return None


class FakeQueue:
    """Records the tasks the webhook would queue.

    Stands in for S0's ``AgentQueue``: the real signature is
    ``enqueue(repo_id, *, kind, payload, priority)``.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def enqueue(self, repo_id, *, kind, payload, priority, **kwargs):  # noqa: ANN001
        call = {"repo_id": repo_id, "kind": kind, "payload": payload,
                "priority": priority}
        self.calls.append(call)
        return len(self.calls)


def make_issues(count: int, *, is_pr: bool = False, start: int = 1) -> list[dict[str, Any]]:
    base = datetime(2024, 1, 1, tzinfo=timezone.utc)
    rows = []
    for offset in range(count):
        number = start + offset
        rows.append({
            "number": number,
            "is_pull_request": is_pr,
            "title": f"issue {number}",
            "body": f"body of issue {number}",
            "state": "open" if number % 2 else "closed",
            "author": "tester",
            "labels": ["bug", "agent"] if number % 3 == 0 else ["bug"],
            "milestone": "v1",
            "created_at": (base + timedelta(minutes=number)).isoformat(),
            "updated_at": (base + timedelta(minutes=number)).isoformat(),
            "closed_at": None,
            "url": f"https://github.com/example/repo/issues/{number}",
            "source_id": str(1000 + number),
        })
    return rows


def make_commits(count: int) -> list[dict[str, Any]]:
    base = datetime(2024, 1, 1, tzinfo=timezone.utc)
    return [
        {
            "sha": f"{index:040x}",
            "message": f"commit {index}",
            "author": "tester",
            "committed_at": (base + timedelta(hours=index)).isoformat(),
        }
        for index in range(count)
    ]


# ── 1. URL parsing ───────────────────────────────────────────────────

def scenario_parse() -> None:
    section("1 · source URL parsing (github / gitee / gitlab / bare https)")
    cases = [
        ("https://github.com/vllm-project/vllm", "github.com",
         "vllm-project", "vllm", "vllm-project/vllm", "github"),
        ("https://gitee.com/oschina/git-osc.git", "gitee.com",
         "oschina", "git-osc", "oschina/git-osc", "gitee"),
        ("https://gitlab.com/gitlab-org/gitlab", "gitlab.com",
         "gitlab-org", "gitlab", "gitlab-org/gitlab", "gitlab"),
        # The full GitLab subgroup path is the identity: rebuilding the URL from
        # the last two segments would clone a *different* repository.
        ("https://gitlab.com/group/subgroup/project.git#main", "gitlab.com",
         "subgroup", "project", "group/subgroup/project", "gitlab"),
        ("https://git.internal.example/team/tool.git", "git.internal.example",
         "team", "tool", "team/tool", "git"),
        ("git@github.com:vllm-project/vllm.git", "github.com",
         "vllm-project", "vllm", "vllm-project/vllm", "github"),
    ]
    for raw, host, owner, name, path, kind in cases:
        source = repo_import.parse_source(raw)
        ok = (
            source.host == host
            and source.owner == owner
            and source.name == name
            and source.kind == kind
            and source.path == path
            and source.slug == path
            and source.clone_url.endswith(f"/{path}.git")
        )
        check(f"{raw} → {source.slug}", ok, f"got {source}")

    # Two subgroups with the same project name must not share an identity.
    first = repo_import.parse_source("https://gitlab.com/group-a/sub/project")
    second = repo_import.parse_source("https://gitlab.com/group-b/sub/project")
    check("same-named projects in different subgroups stay distinct",
          first.slug != second.slug and first.clone_url != second.clone_url,
          f"{first.slug} vs {second.slug}")

    for bad in ("", "   ", "https://github.com/onlyowner", "ftp://host/a/b"):
        try:
            repo_import.parse_source(bad)
        except repo_import.SourceUrlError:
            check(f"rejects {bad}", True)
        else:
            check(f"rejects {bad}", False, "no SourceUrlError")

    source = repo_import.parse_source("https://github.com/vllm-project/vllm")
    check("probe_url targets the source host",
          repo_import.probe_url(source) == "https://api.github.com/repos/vllm-project/vllm",
          repo_import.probe_url(source))


# ── 2. pipeline: migrate → poll → mirror → index ─────────────────────

def _seed_repo(harness: _Harness, url: str, *, mode: str = "code+issues"):
    service = harness.service()
    job = service.create_job(url, mode=mode)
    repo = service.repo_by_slug("vllm-project/vllm")
    return service, job, repo


def scenario_pipeline() -> None:
    section("2 · pipeline: migrate → poll → mirror_issues → index_commits → done")
    harness = _Harness()
    try:
        harness.client.issues = make_issues(120)
        harness.client.commits = make_commits(75)
        harness.client.migrate_statuses = [
            {"full_name": "openfish/vllm-project__vllm", "migrating": True},
            {"full_name": "openfish/vllm-project__vllm", "migrating": False},
        ]
        service = harness.service()
        job = service.create_job("https://github.com/vllm-project/vllm")
        check("job is queued at validate", job.phase == "validate" and job.status == "queued")

        outcome = service.run(job, max_steps=200)
        check("job finished", outcome.finished and outcome.status == "done",
              f"status={outcome.status} phase={outcome.phase}")
        check("migration was triggered once with the §8.2 switches",
              len(harness.client.migrate_calls) == 1
              and harness.client.migrate_calls[0]["include_issues"] is True
              and harness.client.migrate_calls[0]["include_prs"] is True
              and harness.client.migrate_calls[0]["mirror"] is True,
              json.dumps(harness.client.migrate_calls, default=str))
        check("progress reached 100", outcome.progress == 100, str(outcome.progress))

        session = harness.session()
        repo = session.query(harness.models["Repo"]).one()
        issues = session.query(harness.models["RepoIssue"]).all()
        commits = session.query(harness.models["RepoCommit"]).all()
        check("all 120 issues mirrored", len(issues) == 120, f"got {len(issues)}")
        check("all 75 commits indexed", len(commits) == 75, f"got {len(commits)}")
        check("repo counts materialised",
              repo.issue_count == 120 and repo.commit_count == 75,
              f"{repo.issue_count}/{repo.commit_count}")
        check("repo is ready", repo.sync_state == "ready", repo.sync_state)
        check("repo knows its Forgejo name",
              repo.forgejo_repo == "openfish/vllm-project__vllm", str(repo.forgejo_repo))
        labelled = next((row for row in issues if "agent" in json.loads(row.labels)), None)
        check("labels survive as JSON and are not filtered",
              labelled is not None and json.loads(labelled.labels) == ["bug", "agent"],
              str(issues[0].labels))

        history = harness.progress_history
        check("progress never went backwards",
              all(b >= a for a, b in zip(history, history[1:])), str(history))
        check("progress ended at 100", history and history[-1] == 100, str(history))
        check("phase scale is monotonic by construction",
              all(phase_progress(JOB_PHASES[i]) <= phase_progress(JOB_PHASES[i + 1])
                  for i in range(len(JOB_PHASES) - 1)),
              "PHASE_BOUNDS")
    finally:
        harness.close()


JOB_PHASES = repo_import.JOB_PHASES


# ── 3. issue upsert idempotency ──────────────────────────────────────

def scenario_idempotent_upsert() -> None:
    section("3 · issue upsert is idempotent (source_id collision ⇒ update)")
    harness = _Harness()
    try:
        harness.client.issues = make_issues(30)
        service, job, repo = _seed_repo(harness, "https://github.com/vllm-project/vllm")
        service.run(job, max_steps=200)

        session = harness.session()
        type_issue = harness.models["RepoIssue"]

        def count() -> int:
            return session.query(type_issue).count()

        first = count()
        check("first pass inserted 30 rows", first == 30, str(first))

        # Re-run the same page verbatim: the cursor is reset, the data is not.
        again = service._upsert_issues(repo, make_issues(30))
        check("second pass wrote no new rows", count() == 30, f"got {count()}")
        check("second pass still reported 30 updates", again == 30, str(again))

        # Same source_id, changed fields ⇒ update in place, keyed by source_id.
        changed = make_issues(1)[0]
        changed["title"] = "renamed after the first pass"
        changed["state"] = "closed"
        changed["number"] = 999  # a *different* number, same source_id
        service._upsert_issues(repo, [changed])
        check("source_id collision updates instead of inserting", count() == 30,
              f"got {count()}")
        row = (
            session.query(type_issue)
            .filter(type_issue.source_id == changed["source_id"])
            .one()
        )
        check("the updated row carries the new title/state/number",
              row.title == "renamed after the first pass"
              and row.state == "closed"
              and row.number == 999,
              f"{row.title}/{row.state}/{row.number}")

        # A row that predates source_id is still matched by (repo_id, number).
        legacy = type_issue(repo_id=repo.id, number=42, title="legacy row",
                            source_id=None)
        session.add(legacy)
        session.commit()
        survivor = [row for row in make_issues(1, start=42)][0]
        service._upsert_issues(repo, [survivor])
        check("number fallback dedupes a row with no source_id",
              count() == 31, f"got {count()}")
    finally:
        harness.close()


# ── 4. resume from cursor ────────────────────────────────────────────

def scenario_resume() -> None:
    section("4 · resumable: a stored cursor continues, it does not restart")
    harness = _Harness()
    try:
        harness.client.issues = make_issues(150)
        harness.client.commits = make_commits(10)
        service = harness.service()
        job = service.create_job("https://github.com/vllm-project/vllm")

        # First chunk: validate → migrate → poll → one page of issues.
        outcome = service.run(job, max_steps=4)
        check("chunk 1 stopped early", not outcome.finished, outcome.status)
        pages_after_first = list(harness.client.issue_pages)
        check("chunk 1 consumed exactly one source page", pages_after_first == [0],
              str(pages_after_first))

        session = harness.session()
        stored = session.get(harness.models["ImportJob"], job.id)
        cursor = Cursor.from_json(stored.cursor)
        check("cursor was persisted", cursor.issue_number == 50,
              f"issue_number={cursor.issue_number}")
        check("the source offset was persisted", cursor.issues_seen == 50,
              f"offset={cursor.issues_seen}")
        check("progress was persisted", 30 <= stored.progress <= 80, str(stored.progress))

        # A *new* service + session, as after a worker restart.
        resumed = harness.service()
        stored = harness.session().get(harness.models["ImportJob"], job.id)
        outcome = resumed.run(stored, max_steps=200)
        check("resumed job finished", outcome.status == "done", outcome.status)
        check("resume did not re-read the first page",
              harness.client.issue_pages[0] == 0
              and 0 not in harness.client.issue_pages[1:],
              str(harness.client.issue_pages))
        check("resume continued from the stored offset",
              harness.client.issue_pages[1] == 50, str(harness.client.issue_pages))

        final = harness.session()
        check("every issue made it, exactly once",
              final.query(harness.models["RepoIssue"]).count() == 150,
              str(final.query(harness.models["RepoIssue"]).count()))
        check("migration was not repeated on resume",
              len(harness.client.migrate_calls) == 1,
              str(len(harness.client.migrate_calls)))

        # Waiting for the migration is not work: a bounded-steps caller still
        # waits, and a permanently-running migration stops at the poll budget
        # rather than spinning forever.
        waiting = _Harness()
        try:
            waiting.client.issues = make_issues(3)
            waiting.client.migrate_statuses = [
                {"full_name": "f", "migrating": True} for _ in range(50)
            ]
            service = waiting.service()
            job = service.create_job("https://github.com/vllm-project/vllm")
            first = service.run(job, max_steps=3)
            check("a bounded-steps call still waits for the poll rather than "
                  "burning its budget on the wait",
                  first.phase == "poll" and (not first.finished or first.status == "done"),
                  f"phase={first.phase} status={first.status}")
            check("the poll made at least one attempt",
                  waiting.client.state_calls >= 1, str(waiting.client.state_calls))
            check("the poll budget ends the wait instead of hanging",
                  waiting.client.state_calls <= waiting.config.poll_attempts,
                  str(waiting.client.state_calls))

            blocking = _Harness()
            try:
                blocking.client.issues = make_issues(3)
                blocking.config = blocking.config.__class__(
                    **{**blocking.config.__dict__, "poll_attempts": 2}
                )
                blocking.client.migrate_statuses = [
                    {"full_name": "f", "migrating": True} for _ in range(10)
                ]
                service = blocking.service()
                job = service.create_job("https://github.com/vllm-project/vllm")
                first = service.run(job, max_steps=10)
                check("the poll budget ends the wait instead of hanging",
                      not first.finished and blocking.client.state_calls <= 2,
                      f"{first.status}/{blocking.client.state_calls}")
                # A worker that keeps calling eventually gets a hard failure
                # rather than an unbounded wait; the reason names the budget.
                second = service.run(
                    blocking.session().get(blocking.models["ImportJob"], job.id),
                    max_steps=10,
                )
                check("a migration that never finishes fails the job with a reason",
                      second.status == "failed"
                      and "migration" in str(second.error).lower(),
                      f"{second.status}: {second.error}")
            finally:
                blocking.close()
        finally:
            waiting.close()
    finally:
        harness.close()


# ── 5. large-repo ceiling ────────────────────────────────────────────

def scenario_partial() -> None:
    section("5 · IMPORT_MAX_ISSUES ⇒ partial=true, and the API says so")
    harness = _Harness()
    try:
        harness.client.issues = make_issues(120)
        config = harness.config.__class__(**{**harness.config.__dict__, "max_issues": 50})
        service = harness.service(config=config)
        job = service.create_job("https://github.com/vllm-project/vllm")
        outcome = service.run(job, max_steps=200)

        check("job ends partial, not done", outcome.status == "partial", outcome.status)
        check("outcome carries the flag", outcome.partial is True)
        session = harness.session()
        stored = session.get(harness.models["ImportJob"], job.id)
        check("model column partial is set", bool(stored.partial) is True)
        check("only the ceiling was mirrored",
              session.query(harness.models["RepoIssue"]).count() == 50,
              str(session.query(harness.models["RepoIssue"]).count()))

        # The REST document is built from the *job row*; it must agree.
        previous = repo_import.ImportConfig.from_settings
        repo_import.ImportConfig.from_settings = classmethod(  # type: ignore[assignment]
            lambda cls, source=None: config
        )
        try:
            payload = repo_import.job_payload(stored, repo_slug="vllm-project/vllm")
        finally:
            repo_import.ImportConfig.from_settings = previous
        check("payload.partial is true", payload["partial"] is True, json.dumps(payload))
        check("payload.truncated is true", payload["truncated"] is True)
        check("payload names the ceiling", payload["max_issues"] == 50,
              str(payload["max_issues"]))
        check("partial_of() agrees", repo_import.partial_of(stored) is True)
    finally:
        harness.close()

    # The same behaviour when S0's ImportJob has no `partial` column at all.
    harness = _Harness(with_partial=False)
    try:
        harness.client.issues = make_issues(80)
        config = harness.config.__class__(**{**harness.config.__dict__, "max_issues": 20})
        service = harness.service(config=config)
        job = service.create_job("https://github.com/vllm-project/vllm")
        outcome = service.run(job, max_steps=200)
        check("partial still ends the job", outcome.status == "partial", outcome.status)
        session = harness.session()
        stored = session.get(harness.models["ImportJob"], job.id)
        check("no partial column on the model", not hasattr(stored, "partial"))
        check("partial_of() still reports truncation",
              repo_import.partial_of(stored) is True)
        previous = repo_import.ImportConfig.from_settings
        repo_import.ImportConfig.from_settings = classmethod(  # type: ignore[assignment]
            lambda cls, source=None: config
        )
        try:
            payload = repo_import.job_payload(stored)
        finally:
            repo_import.ImportConfig.from_settings = previous
        check("payload still exposes partial=true", payload["partial"] is True,
              json.dumps(payload))
        check("a human can see why in `error`",
              "partial mirror" in str(payload["error"] or ""), str(payload["error"]))
    finally:
        harness.close()


def scenario_commit_ceiling() -> None:
    """The commit ceiling must set ``partial`` on both paths.

    ``len(rows) > room`` is only true when the source handed back *more* rows
    than there was room for; the git fallback used to ask for exactly
    ``max_commits`` rows, so the comparison could never fire and a 100k-commit
    repository finished ``done`` with 5 000 commits and ``partial=false``.
    """
    section("5b · IMPORT_MAX_COMMITS ⇒ partial=true (API and git fallback)")

    # The API path: the ceiling stops the walk while another page is available.
    harness = _Harness()
    try:
        harness.client.commits = make_commits(120)
        config = harness.config.__class__(
            **{**harness.config.__dict__, "max_commits": 50})
        service = harness.service(config=config)
        job = service.create_job("https://github.com/vllm-project/vllm")
        outcome = service.run(job, max_steps=200)
        session = harness.session()
        check("an API walk cut at the ceiling ends partial",
              outcome.status == "partial", outcome.status)
        check("only the ceiling was stored",
              session.query(harness.models["RepoCommit"]).count() == 50,
              str(session.query(harness.models["RepoCommit"]).count()))
    finally:
        harness.close()

    # The git fallback: the API has nothing, so the read-only copy answers.
    harness = _Harness()
    try:
        harness.client.commits = []
        harness.git.commits = make_commits(30)
        config = harness.config.__class__(
            **{**harness.config.__dict__, "max_commits": 10})
        service = harness.service(config=config)
        job = service.create_job("https://github.com/vllm-project/vllm")
        outcome = service.run(job, max_steps=200)
        session = harness.session()
        check("a truncated git fallback ends partial",
              outcome.status == "partial", outcome.status)
        check("the fallback probes one past the ceiling",
              harness.git.limit == 11, str(harness.git.limit))
        check("the fallback stored only the ceiling",
              session.query(harness.models["RepoCommit"]).count() == 10,
              str(session.query(harness.models["RepoCommit"]).count()))
    finally:
        harness.close()

    # An exact fit is complete, not truncated.
    harness = _Harness()
    try:
        harness.client.commits = []
        harness.git.commits = make_commits(10)
        config = harness.config.__class__(
            **{**harness.config.__dict__, "max_commits": 10})
        service = harness.service(config=config)
        job = service.create_job("https://github.com/vllm-project/vllm")
        outcome = service.run(job, max_steps=200)
        check("an exact fit is done, not partial", outcome.status == "done",
              outcome.status)
    finally:
        harness.close()

    # Both sources failing must fail the job, not report an empty history.
    harness = _Harness()
    try:
        harness.client.commits = []
        harness.git.fail = "git is down"
        service = harness.service()
        job = service.create_job("https://github.com/vllm-project/vllm")
        outcome = service.run(job, max_steps=200)
        check("API and git both unavailable fails the job",
              outcome.status == "failed", outcome.status)
        check("the failure names the commit source",
              "git 回退" in str(outcome.error or ""), str(outcome.error))
    finally:
        harness.close()


# ── 6. webhook HMAC ──────────────────────────────────────────────────

SECRET = "test-webhook-secret"


def _push_payload(branch: str = "main") -> dict[str, Any]:
    return {
        "ref": f"refs/heads/{branch}",
        "repository": {
            "full_name": "openfish/vllm-project__vllm",
            "name": "vllm-project__vllm",
            "default_branch": "main",
            "owner": {"login": "openfish"},
        },
        "sender": {"login": "shaojun0"},
        "commits": [{"id": "a" * 40, "message": "fix"}],
    }


def _issue_payload(labels: list[str]) -> dict[str, Any]:
    return {
        "action": "labeled",
        "issue": {"number": 1234, "title": "please look", "labels":
                  [{"name": name} for name in labels]},
        "repository": {"full_name": "openfish/vllm-project__vllm",
                       "default_branch": "main"},
        "sender": {"login": "shaojun0"},
    }


def scenario_webhook() -> None:
    section("6 · webhook: HMAC verify + §5.4 event table")
    body = json.dumps(_push_payload()).encode("utf-8")
    good = compute_signature(SECRET, body)
    check("correct signature passes", verify_signature(body, good, secret=SECRET) is True)
    check("`sha256=` prefix is accepted",
          verify_signature(body, f"sha256={good}", secret=SECRET) is True)
    check("uppercase hex is accepted",
          verify_signature(body, good.upper(), secret=SECRET) is True)

    tampered = bytearray(body)
    tampered[-2] = ord("}") if tampered[-2] != ord("}") else ord(" ")
    check("tampered body is rejected",
          verify_signature(bytes(tampered), good, secret=SECRET) is False)
    other = compute_signature("another-secret", body)
    check("signature from another secret is rejected",
          verify_signature(body, other, secret=SECRET) is False)
    check("missing header is rejected", verify_signature(body, None, secret=SECRET) is False)
    check("missing secret is rejected",
          verify_signature(body, compute_signature("", body), secret="") is False)
    check("wrong algorithm is rejected",
          verify_signature(body, f"sha1={'a' * 40}", secret=SECRET) is False)
    check("garbage header is rejected",
          verify_signature(body, "not-a-signature", secret=SECRET) is False)

    # -- the §5.4 decision table, driven through the real parse/plan path
    push_main = parse_event("push", _push_payload("main"))
    check("push to the default branch is recognised", push_main.is_default_branch)
    actions = plan_actions(push_main, repo_id=7, auto_review=True)
    check("push→default branch queues a review",
          [a.kind for a in actions] == ["review"], str([a.kind for a in actions]))
    check("review carries the pushed sha and branch",
          actions[0].payload["commit_sha"] == "a" * 40
          and actions[0].payload["branch"] == "main",
          json.dumps(actions[0].payload))
    check("no policy ⇒ no review on a push",
          plan_actions(push_main, repo_id=7, auto_review=False) == [])
    check("push to a side branch queues nothing",
          plan_actions(parse_event("push", _push_payload("agent/x")), repo_id=7) == [])

    after_only = _push_payload()
    after_only.pop("commits")
    after_only["after"] = "e" * 40
    actions = plan_actions(parse_event("push", after_only), repo_id=7, auto_review=True)
    check("push uses `after` when the commit list is absent",
          [a.payload["commit_sha"] for a in actions] == ["e" * 40],
          str([a.payload for a in actions]))

    deleted = _push_payload()
    deleted["deleted"] = True
    deleted["after"] = "0" * 40
    deleted["commits"] = []
    check("branch deletion queues nothing",
          plan_actions(parse_event("push", deleted), repo_id=7, auto_review=True) == [])

    shapeless = _push_payload()
    shapeless["commits"] = []
    check("push without a usable sha queues nothing",
          plan_actions(parse_event("push", shapeless), repo_id=7, auto_review=True) == [])

    issue = parse_event("issues", _issue_payload([AGENT_LABEL, "bug"]))
    actions = plan_actions(issue, repo_id=7)
    check("issue labelled `agent` queues a fix",
          [a.kind for a in actions] == ["fix"], str([a.kind for a in actions]))
    check("fix payload names the issue",
          actions[0].payload["issue_number"] == 1234, json.dumps(actions[0].payload))
    check("issue without the label queues nothing",
          plan_actions(parse_event("issues", _issue_payload(["bug"])), repo_id=7) == [])

    merged = parse_event("pull_request", {
        "action": "closed",
        "pull_request": {"number": 55, "state": "closed", "merged": True,
                         "html_url": "https://git.example/openfish/x/pulls/55"},
        "repository": {"full_name": "openfish/vllm-project__vllm",
                       "default_branch": "main"},
    })
    actions = plan_actions(merged, repo_id=7)
    check("merged PR queues nothing — the write-back runs inline",
          actions == [], str([a.kind for a in actions]))
    check("the reserved doc kind is still in S0's vocabulary (documented, never queued)",
          "backfill" in TASK_KIND, str(TASK_KIND))
    unmerged = parse_event("pull_request", {
        "action": "closed",
        "pull_request": {"number": 56, "state": "closed", "merged": False},
        "repository": {"full_name": "openfish/vllm-project__vllm"},
    })
    check("unmerged PR queues nothing", plan_actions(unmerged, repo_id=7) == [])

    # The per-repo opt-out must actually reach the decision: the import pipeline
    # caches `.agent/review-policy.yml`'s auto_review on the repos row, and the
    # view feeds that through as the policy reader.
    from routes.repo_webhook import _repo_policy_reader

    class _CachedOff:
        auto_review = False

    class _NotRead:
        auto_review = None

    check("cached auto_review=False 会抑制 push review",
          plan_actions(push_main, repo_id=7,
                       policy_reader=_repo_policy_reader(_CachedOff())) == [])
    check("未读取策略时默认开启 review",
          [a.kind for a in plan_actions(
              push_main, repo_id=7, policy_reader=_repo_policy_reader(_NotRead()),
          )] == ["review"])

    # -- end-to-end through the queue shim
    queue = FakeQueue()
    for action in plan_actions(push_main, repo_id=7, auto_review=True):
        enqueue_task(kind=action.kind, repo_id=action.repo_id,
                     payload=action.payload, priority=action.priority, enqueue=queue.enqueue)
    check("enqueue shim called with S0's contract",
          queue.calls == [{
              "repo_id": 7,
              "kind": "review",
              "payload": {"commit_sha": "a" * 40, "branch": "main",
                          "trigger": "push", "sender": "shaojun0"},
              "priority": 5,
          }],
          json.dumps(queue.calls, default=str))
    check("a review outranks a doc backfill (priority DESC leases first)",
          PRIORITY_REVIEW > PRIORITY_DOC and PRIORITY_FIX > PRIORITY_DOC,
          f"{PRIORITY_REVIEW}/{PRIORITY_FIX}/{PRIORITY_DOC}")
    rejected = enqueue_task(kind="not-a-kind", repo_id=7, payload={}, priority=1,
                            enqueue=queue.enqueue)
    check("an unknown task kind is refused rather than written badly",
          rejected.delivered is False and "TASK_KIND" in rejected.reason,
          rejected.reason)
    unwired = enqueue_task(kind="review", repo_id=7, payload={}, priority=5)
    check("a missing agent_queue degrades to a logged no-op, not an exception",
          unwired.delivered is False and "agent_queue" in unwired.reason,
          unwired.reason)

    # -- pr_url backfill is a no-op without S3's findings table
    session = _Harness().session()
    try:
        check("pr_url backfill is safe before S3 lands",
              backfill_pr_url(session, 7, 55, "https://example.invalid/pr/55") == 0)
    finally:
        session.close()

    # -- the HTTP route, if Flask can import it here
    check("route declares itself public (HMAC is the guard)",
          _route_is_public(), "security=[] missing")


def scenario_repo_resolution() -> None:
    """An ambiguous webhook→repo match is refused, never guessed."""
    section("6b · webhook repo resolution: ambiguity is not a coin flip")
    from routes.repo_webhook import resolve_repo

    harness = _Harness()
    try:
        session = harness.session()
        repo_cls = harness.models["Repo"]
        event = parse_event("push", _push_payload("main"))
        unique = repo_cls(slug=event.repo_slug, forgejo_repo=event.forgejo_repo)
        session.add(unique)
        session.commit()
        resolved = resolve_repo(session, event)
        check("a unique match resolves",
              getattr(resolved, "id", None) == unique.id,
              str(getattr(resolved, "slug", None)))

        # A second row that also answers the payload (same forgejo_repo, another
        # slug) used to be resolved by an arbitrary ``first()``.
        clash = repo_cls(slug="openfish/other-name", forgejo_repo=event.forgejo_repo)
        session.add(clash)
        session.commit()
        check("two matching rows resolve to None instead of an arbitrary row",
              resolve_repo(session, event) is None)
    finally:
        harness.close()


def scenario_pr_link() -> None:
    """A merge stamps only the findings the PR's own review run produced."""
    section("6c · merged PR → pr_url only on linked findings")
    from models.agent_hub import AgentTask, Finding, Repo, ReviewRun
    from models.agent_hub_migrate import ensure_schema
    from routes.repo_webhook import backfill_pr_url

    harness = _Harness()
    try:
        ensure_schema(harness.engine)
        session = harness.session()
        repo = Repo(slug="openfish/linked", kind="workspace")
        session.add(repo)
        session.commit()

        url = "https://forgejo.example/openfish/linked/pulls/7"
        opener = AgentTask(
            repo_id=repo.id, kind="fix", status="done", payload="{}", pr_url=url,
        )
        session.add(opener)
        session.commit()
        opened_run = ReviewRun(
            repo_id=repo.id, agent_task_id=opener.id, commit_sha="a" * 40, status="ok",
        )
        session.add(opened_run)
        session.commit()

        other = AgentTask(repo_id=repo.id, kind="review", status="done", payload="{}")
        session.add(other)
        session.commit()
        other_run = ReviewRun(
            repo_id=repo.id, agent_task_id=other.id, commit_sha="b" * 40, status="ok",
        )
        session.add(other_run)
        session.commit()

        linked = Finding(
            repo_id=repo.id, fingerprint="f" * 64, rule_id="r", level="debt",
            severity="medium", file_path="a.py", symbol="s", title="linked",
            first_seen_run_id=opened_run.id, last_seen_run_id=opened_run.id,
        )
        unrelated = Finding(
            repo_id=repo.id, fingerprint="e" * 64, rule_id="r", level="debt",
            severity="medium", file_path="b.py", symbol="s", title="unrelated",
            first_seen_run_id=other_run.id, last_seen_run_id=other_run.id,
        )
        session.add_all([linked, unrelated])
        session.commit()

        updated = backfill_pr_url(session, repo.id, 7, url)
        check("only the linked finding is stamped", updated == 1, str(updated))
        session.expire_all()
        check("the linked finding carries the PR url",
              session.get(Finding, linked.id).pr_url == url)
        check("an unrelated finding is left alone",
              session.get(Finding, unrelated.id).pr_url is None, "stamped by mistake")

        check("a PR this platform did not open writes nothing",
              backfill_pr_url(
                  session, repo.id, 8, "https://forgejo.example/openfish/linked/pulls/8",
              ) == 0)

        # The producer side: the worker (not the sink) records the link, because
        # the worker process is Flask-free and its sink session is unbound.
        from services.agent_worker import _record_pr_link

        worker_opened = AgentTask(
            repo_id=repo.id, kind="fix", status="done", payload="{}",
        )
        session.add(worker_opened)
        session.commit()

        class _Outcome:
            pr_url = "https://forgejo.example/openfish/linked/pulls/9"

        _record_pr_link(str(worker_opened.id), _Outcome(), harness.Session)
        session.expire_all()
        check("the worker persists the PR link onto the task row",
              session.get(AgentTask, worker_opened.id).pr_url == _Outcome.pr_url)
    finally:
        harness.close()


def _route_is_public() -> bool:
    """Read the route's own metadata without booting the app."""
    from openapi import operation_of

    view = sys.modules["routes.repo_webhook"].forgejo_webhook
    metadata = operation_of(view) or {}
    return metadata.get("security") == [] and metadata.get("summary", "") != ""


# ── 7. live HTTP behaviour (own app, temp DB, no external service) ───

def scenario_http() -> None:
    """Boot a minimal Flask app around the two blueprints and drive them.

    This is not the full application — ``routes/__init__.py`` is a shared file
    this slice must not edit — but it is enough to prove that a tampered
    webhook signature is a 401 and that the import/issue routes are wired to
    the guards and the pipeline.
    """
    section("7 · HTTP surface (minimal app, temp database)")
    from flask import Flask

    harness = _Harness()
    try:
        harness.client.issues = make_issues(5)
        harness.client.commits = make_commits(3)
        app = Flask("check-agent-repos")
        # Let a view's exception reach this script's traceback instead of being
        # flattened into a bare 500 — the point of the gate is to see it.
        app.config["PROPAGATE_EXCEPTIONS"] = True
        from flask import Blueprint

        # Fresh blueprints at the **bare** prefix, mirroring
        # `routes/__init__.py`: each view declares its own full
        # `/api/v1/...` path, exactly as the artifact-hub blueprints do.  The
        # clone is necessary because re-registering an already-registered
        # blueprint is a Flask error; the view functions are the real ones.
        from auth.decorators import require_permission
        from routes.repo_webhook import repo_webhook_bp
        from routes.repos import repo_bp

        for source, name in ((repo_bp, "repos"), (repo_webhook_bp, "repo_webhook")):
            clone = Blueprint(name, __name__)
            for deferred in source.deferred_functions:
                clone.deferred_functions.append(deferred)
            if name == "repos":
                # The blueprint-wide floor `routes/__init__.py` installs.
                clone.before_request(require_permission("repo:read"))
            app.register_blueprint(clone, url_prefix="")
        app.extensions["db_engine"] = harness.engine

        # This app is a transport harness, not a deployment: it carries just
        # enough of the authorization extension for the route guards to run
        # (the real RBAC tables are S0's and are exercised by its own gate).
        class _Allow:
            def has_permission(self, principal, perm):  # noqa: ANN001
                return True

        app.extensions["authz"] = _Allow()

        # Credential handling is likewise faked: `AUTH_ENABLED` may be true in
        # this checkout, and the gate's subject is the webhook HMAC and the
        # route wiring, not S0's authentication methods.
        import auth.guards as guards
        from flask import g as flask_g

        def _test_session() -> bool:
            flask_g.auth_user = {"sub": "gate", "display_name": "gate",
                                 "user_id": 1, "is_superuser": True}
            return True

        guards.AUTH_METHODS["session"] = _test_session

        # The routes resolve their session through `extensions.database.Session`
        # (a session *proxy*: `_session()` then `.query()`/`.get()` on it, and
        # the module-level spelling is called as a factory in a few places).
        # A scoped session on the throwaway engine is exactly that shape.
        import extensions.database as database
        from sqlalchemy.orm import scoped_session
        previous_session = database.Session
        database.Session = scoped_session(harness.Session)  # type: ignore[assignment]
        try:
            client = app.test_client()
            service = harness.service()
            job = service.create_job("https://github.com/vllm-project/vllm")
            service.run(job, max_steps=200)

            response = client.get("/api/v1/imports/1")
            check("GET /imports/<id> answers", response.status_code == 200,
                  str(response.status_code))
            payload = response.get_json() or {}
            check("progress document has the ceiling fields",
                  {"partial", "truncated", "max_issues"} <= set(payload),
                  json.dumps(sorted(payload)))
            check("a complete import is not marked partial",
                  payload["partial"] is False, json.dumps(payload))

            response = client.get("/api/v1/repos/vllm-project/vllm")
            check("GET /repos/<owner>/<name> routes on a slashed slug",
                  response.status_code == 200, str(response.status_code))
            body = response.get_json() or {}
            check("detail carries the materialised counts",
                  body.get("issue_count") == 5 and body.get("commit_count") == 3,
                  json.dumps(body))

            response = client.get("/api/v1/repos/vllm-project/vllm/issues?state=open")
            expected_open = sum(1 for row in harness.client.issues
                                if row["state"] == "open")
            check("GET issues honours ?state",
                  response.status_code == 200
                  and response.get_json()["total"] == expected_open,
                  f"{response.status_code} total="
                  f"{(response.get_json() or {}).get('total')} expected={expected_open}")

            response = client.get("/api/v1/repos/vllm-project/vllm/issues?label=agent")
            expected_labelled = sum(1 for row in harness.client.issues
                                    if "agent" in row["labels"])
            check("GET issues filters on an exact label token",
                  response.status_code == 200
                  and response.get_json()["total"] == expected_labelled,
                  f"total={(response.get_json() or {}).get('total')} "
                  f"expected={expected_labelled}")

            response = client.get("/api/v1/repos/vllm-project/vllm/issues/1")
            check("GET one issue returns the body verbatim",
                  response.status_code == 200
                  and response.get_json()["body"] == "body of issue 1",
                  str(response.status_code))

            response = client.get("/api/v1/repos")
            check("GET /repos lists the repo", response.status_code == 200
                  and response.get_json()["total"] == 1, str(response.status_code))

            # Webhook: tampered signature is 401, correct one is handled.
            body_bytes = json.dumps(_push_payload("main")).encode("utf-8")
            headers = {
                "Content-Type": "application/json",
                "X-Forgejo-Event": "push",
                "X-Forgejo-Signature": compute_signature("wrong-secret", body_bytes),
            }
            response = client.post("/api/v1/repos/webhook", data=body_bytes, headers=headers)
            check("tampered webhook signature → 401", response.status_code == 401,
                  str(response.status_code))

            headers["X-Forgejo-Signature"] = compute_signature(
                harness.config.webhook_secret, body_bytes
            )
            import routes.repo_webhook as webhook_module
            previous_config = webhook_module.ImportConfig
            webhook_module.ImportConfig = SimpleNamespace(  # type: ignore[assignment]
                from_settings=lambda: harness.config
            )
            try:
                response = client.post("/api/v1/repos/webhook", data=body_bytes,
                                       headers=headers)
            finally:
                webhook_module.ImportConfig = previous_config
            check("valid signature is accepted", response.status_code == 200,
                  str(response.status_code))
            result = response.get_json() or {}
            check("accepted push reports the default-branch review decision",
                  result.get("handled") is True, json.dumps(result))
        finally:
            database.Session = previous_session  # type: ignore[assignment]
    finally:
        harness.close()


# ── 8. real queue delivery (S0's AgentQueue, no fake) ────────────────

def scenario_queue_delivery() -> None:
    """The webhook's task reaches S0's real queue table.

    Scenario 6 proves the *decision*; this proves the delivery: the same
    signature-verified request, a real ``AgentQueue`` bound to the harness
    engine, and an ``agent_tasks`` row that says what the push asked for.
    """
    section("8 · webhook → S0 queue: a push really writes an agent_task")
    from flask import Blueprint, Flask
    from sqlalchemy import text

    from models.agent_hub_migrate import ensure_schema
    from routes.repo_webhook import repo_webhook_bp
    from services.agent_queue import AgentQueue

    harness = _Harness()
    try:
        harness.client.issues = make_issues(3)
        harness.client.commits = make_commits(2)
        # S0's own bootstrap for the queue schema (and the rest of the hub).
        ensure_schema(harness.engine)
        queue = AgentQueue(harness.engine)

        app = Flask("check-agent-repos-queue")
        app.config["PROPAGATE_EXCEPTIONS"] = True
        clone = Blueprint("repo_webhook", __name__)
        for deferred in repo_webhook_bp.deferred_functions:
            clone.deferred_functions.append(deferred)
        app.register_blueprint(clone, url_prefix="")
        app.extensions["db_engine"] = harness.engine
        app.extensions["agent_queue"] = queue

        class _Allow:
            def has_permission(self, principal, perm):  # noqa: ANN001
                return True

        app.extensions["authz"] = _Allow()

        import auth.guards as guards
        from flask import g as flask_g

        def _test_session() -> bool:
            flask_g.auth_user = {"sub": "gate", "display_name": "gate",
                                 "user_id": 1, "is_superuser": True}
            return True

        guards.AUTH_METHODS["session"] = _test_session

        import extensions.database as database
        from sqlalchemy.orm import scoped_session
        previous_session = database.Session
        database.Session = scoped_session(harness.Session)  # type: ignore[assignment]
        previous_config = sys.modules["routes.repo_webhook"].ImportConfig
        sys.modules["routes.repo_webhook"].ImportConfig = SimpleNamespace(  # type: ignore[assignment]
            from_settings=lambda: harness.config
        )
        try:
            service = harness.service()
            job = service.create_job("https://github.com/vllm-project/vllm")
            service.run(job, max_steps=200)
            repo = harness.session().query(harness.models["Repo"]).one()

            body_bytes = json.dumps(_push_payload("main")).encode("utf-8")
            headers = {
                "Content-Type": "application/json",
                "X-Forgejo-Event": "push",
                "X-Forgejo-Delivery": "gate-delivery-1",
                "X-Forgejo-Signature": compute_signature(
                    harness.config.webhook_secret, body_bytes),
            }
            response = app.test_client().post(
                "/api/v1/repos/webhook", data=body_bytes, headers=headers
            )
            check("the signed push is accepted with the real queue wired",
                  response.status_code == 200, str(response.status_code))
            result = response.get_json() or {}
            check("the response reports the task as delivered",
                  result.get("handled") is True
                  and result.get("queued")
                  and result["queued"][0]["delivered"] is True,
                  json.dumps(result))

            # The HMAC only proves who signed it: the same delivery arriving
            # twice must not queue a second review (dedup_key alone stops at the
            # active task, so a replay after the task finishes used to re-run).
            replay = app.test_client().post(
                "/api/v1/repos/webhook", data=body_bytes, headers=headers
            )
            replay_body = replay.get_json() or {}
            check("a replayed delivery is not accepted twice",
                  replay.status_code == 200
                  and replay_body.get("duplicate") is True
                  and replay_body.get("handled") is False,
                  json.dumps(replay_body))

            with harness.engine.connect() as conn:
                rows = conn.execute(text(
                    "SELECT repo_id, kind, priority, status, payload FROM agent_tasks"
                )).fetchall()
            by_kind = {row._mapping["kind"]: dict(row._mapping) for row in rows}
            check(
                "the push queues a review and one curator (checks) proposal",
                set(by_kind) == {"review", "checks"}, str(rows),
            )
            row = by_kind.get("review", {})
            check("the task targets the right repo", row.get("repo_id") == repo.id,
                  str(row))
            check("the task carries the pushed sha",
                  "a" * 40 in str(row.get("payload")), str(row.get("payload")))
            proposal = by_kind.get("checks", {})
            check(
                "the curator proposal is a bootstrap-scoped checks task",
                proposal.get("repo_id") == repo.id
                and proposal.get("priority") == 4
                and "bootstrap" in str(proposal.get("payload")),
                str(proposal),
            )
            check("the queue can lease what the webhook wrote",
                  queue.claim(worker="gate-worker") is not None,
                  "claim returned nothing")
        finally:
            sys.modules["routes.repo_webhook"].ImportConfig = previous_config
            database.Session = previous_session  # type: ignore[assignment]
    finally:
        harness.close()


# ── Import phase vocabulary (the `validate` 500 regression) ──────────

def scenario_import_phase_vocabulary() -> None:
    """The model's CHECK must admit every phase the pipeline can persist.

    A fresh job is created in ``phase="validate"`` (``repo_import.create_job``).
    While ``IMPORT_PHASE`` omitted it, the generated ``ck_import_jobs_phase``
    rejected the insert and ``POST /repos/import`` answered 500 — and this gate
    stayed green, because every other scenario runs against a hand-built fake
    schema.  This one uses the **real** model tables, so the two vocabularies
    cannot drift apart again without turning the gate red.
    """
    section("import phase vocabulary")

    from models.agent_hub import IMPORT_PHASE, ImportJob, Repo
    from models.base import Base as ModelBase

    check("the model admits the pipeline's first phase",
          "validate" in IMPORT_PHASE, f"IMPORT_PHASE={IMPORT_PHASE}")
    check("JOB_PHASES is covered by IMPORT_PHASE",
          set(repo_import.JOB_PHASES) <= set(IMPORT_PHASE),
          f"extra={sorted(set(repo_import.JOB_PHASES) - set(IMPORT_PHASE))}")
    check("PHASE_BOUNDS covers exactly JOB_PHASES",
          set(repo_import.PHASE_BOUNDS) == set(repo_import.JOB_PHASES),
          f"bounds={sorted(repo_import.PHASE_BOUNDS)}")

    engine = create_engine("sqlite://")
    ModelBase.metadata.create_all(engine, tables=[Repo.__table__, ImportJob.__table__])
    try:
        with Session(engine) as session:
            repo = Repo(
                slug="phase-vocabulary", source="local", kind="workspace",
                default_branch="main", sync_state="ready",
            )
            session.add(repo)
            session.commit()
            for phase in repo_import.JOB_PHASES:
                session.add(ImportJob(
                    repo_id=repo.id, mode="code", status="queued", phase=phase,
                ))
                try:
                    session.commit()
                except Exception as exc:  # noqa: BLE001 - the CHECK is the assertion
                    session.rollback()
                    check(f"the real schema admits phase {phase}", False, str(exc))
                    continue
                check(f"the real schema admits phase {phase}", True)
    finally:
        engine.dispose()


# ── main ─────────────────────────────────────────────────────────────

def scenario_enqueue_admission() -> None:
    """Producer-side admission control: dedup by key + per-repo ceiling.

    A webhook storm (redelivery, force-push loop) used to create unbounded rows:
    the same commit could be queued N times and one repository could fill the
    single global queue.  ``AgentQueue.enqueue`` now suppresses an active
    duplicate (``(repo_id, kind, dedup_key)``) and any enqueue past the repo's
    in-flight ceiling, returning ``0`` so the caller reports ``delivered: false``
    instead of mistaking the storm for success.
    """
    section("9 · producer-side admission control")

    from services.agent_queue import AgentQueue
    from models.agent_hub import AgentTask, Repo as RealRepo
    from models.agent_hub_migrate import ensure_schema

    engine = create_engine("sqlite://")
    ensure_schema(engine)
    with Session(engine) as session:
        repo = RealRepo(slug="admission", source="local", kind="workspace",
                        default_branch="main", sync_state="ready")
        session.add(repo)
        session.commit()
        repo_id = int(repo.id)

    queue = AgentQueue(engine)
    sha = "a" * 40
    first = queue.enqueue(repo_id, kind="review", payload={"commit_sha": sha},
                          dedup_key=sha)
    check("首次入队成功", first > 0, str(first))
    check("同一 dedup_key 的在途任务被抑制",
          queue.enqueue(repo_id, kind="review", payload={"commit_sha": sha},
                        dedup_key=sha) == 0)

    # A *finished* task is not an active duplicate: the key may be enqueued again.
    with Session(engine) as session:
        task = session.get(AgentTask, first)
        task.status = "done"
        session.commit()
    check("任务结束后同一 key 可以再次入队",
          queue.enqueue(repo_id, kind="review", payload={"commit_sha": sha},
                        dedup_key=sha) > 0)

    # The ceiling counts every active status for the repo, across kinds.  One
    # review is already active here, so a ceiling of 1 suppresses the next.
    check("per-repo 在途上限生效",
          queue.enqueue(repo_id, kind="checks", payload={"commit_sha": "b" * 40},
                        dedup_key="b" * 40, max_in_flight_per_repo=1) == 0)
    check("不超过上限时仍可入队",
          queue.enqueue(repo_id, kind="fix", payload={"sha": "c"},
                        dedup_key="c", max_in_flight_per_repo=5) > 0)
    engine.dispose()


def scenario_policy_source() -> None:
    """The repo policy file is honored only where the platform owns the repo.

    I6: ``.agent/review-policy.yml`` is repository *content*, so an imported
    upstream mirror must not be able to change what the platform does.  A
    platform-owned ``workspace`` is the documented case where the file governs,
    and both trigger readers (auto_review, curator) must read the same cached row.
    """
    from routes.repo_webhook import _repo_curator_policy, _repo_policy_reader
    from services.review_policy import CURATOR_BOOTSTRAP, DEFAULT_CURATOR_MIN_INTERVAL_SECONDS

    class _PolicyClient:
        def read_file(self, forgejo_repo: str, path: str) -> str:
            return (
                'defaults:\n  auto_review: false\n  curator: "off"\n'
                "  curator_min_interval_seconds: 0\n"
            )

    harness = _Harness()
    try:
        service = harness.service()
        session = service.session
        repo_cls = harness.models["Repo"]
        upstream = repo_cls(
            slug="up/o", kind="upstream", auto_review=True,
            curator="auto", curator_min_interval_seconds=99,
        )
        workspace = repo_cls(slug="up/w", kind="workspace")
        session.add_all([upstream, workspace])
        session.commit()

        service.client = _PolicyClient()
        service._refresh_repo_policy(upstream, "openfish/up__o")
        service._refresh_repo_policy(workspace, "openfish/up__w")
        session.commit()

        check("an upstream mirror's policy file cannot change platform policy",
              upstream.auto_review is None and upstream.curator is None
              and upstream.curator_min_interval_seconds is None,
              f"auto_review={upstream.auto_review} curator={upstream.curator}")
        check("a workspace's own policy file is cached for the webhook",
              workspace.auto_review is False and workspace.curator == "off"
              and workspace.curator_min_interval_seconds == 0,
              f"auto_review={workspace.auto_review} curator={workspace.curator}")
        check("auto_review and curator come from the same cached row",
              _repo_policy_reader(workspace)() is False
              and _repo_curator_policy(workspace) == ("off", 0))
        check("a row that was never read falls back to the documented defaults",
              _repo_curator_policy(repo_cls(slug="never-read"))
              == (CURATOR_BOOTSTRAP, DEFAULT_CURATOR_MIN_INTERVAL_SECONDS))
    finally:
        harness.close()


def main() -> int:
    print("── Agent Hub · repository slice (S1) offline gate " + "─" * 12)
    print(f"   repo root: {REPO_ROOT}")
    for scenario in (
        scenario_parse,
        scenario_pipeline,
        scenario_idempotent_upsert,
        scenario_resume,
        scenario_partial,
        scenario_commit_ceiling,
        scenario_webhook,
        scenario_repo_resolution,
        scenario_pr_link,
        scenario_http,
        scenario_queue_delivery,
        scenario_import_phase_vocabulary,
        scenario_enqueue_admission,
        scenario_policy_source,
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
    print(f"✅ all {CHECKS} checks passed — import pipeline, idempotency, "
          "resume, partial flag, webhook HMAC")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
