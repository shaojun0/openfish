#!/usr/bin/env python
"""Gate: the findings state machine, dedup and policy layer behave as specified.

Run from the backend directory (`backend/`)::

    python scripts/check_findings.py

No Flask, no server, no git: a throwaway SQLite database is built from the
really-declared tables and every rule of §4.4 / §6 / §7 is driven directly
through `services.findings` and `services.review_policy`.  That is the point of
the gate — these are the invariants a review can silently break, and none of
them needs a request to observe:

* **I1** — `fingerprint` is stable, and nothing that moves between runs (line
  number, commit sha, wall clock, summary text) may reach it.
* **Dedup** — the same batch ingested twice adds no rows; it moves
  `seen_count` / `last_seen_run_id` and keeps the status machine continuous.
* **§6.2** — exactly three activation conditions: code drift (symbol range
  intersection, not "the file was touched"), debt expiry, escalation.
* **I2** — `wontfix` / `acknowledge` without `owner` or without `due` are
  refused.
* **I3** — a `blocking` finding accepts neither, and `fixed` is the way out.
* **§6.3** — three false positives for one rule produce exactly **one**
  `meta.rule-quality` finding, however often the watch runs.
* **§7** — the built-in default loads without a file, a missing or expired
  `exceptions[*].due` is a validation error, and `policy_hash` changes when (and
  only when) the policy's meaning changes.

A fake ORM is installed only when `models/agent_hub.py` (slice S0) is not
importable yet, so this gate is runnable before and after that slice lands.
"""

from __future__ import annotations

import builtins
import importlib
import shutil
import subprocess
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from sqlalchemy import (  # noqa: E402
    Date, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint,
    create_engine,
)
from sqlalchemy.orm import Mapped, mapped_column, sessionmaker  # noqa: E402

failures: list[str] = []
checks = 0


def fail(message: str) -> None:
    failures.append(message)
    print(f"   ✗ {message}")


def expect(condition: bool, message: str) -> None:
    """One assertion; never raises, so the gate reports every problem at once."""
    global checks
    checks += 1
    if not condition:
        fail(message)


def expect_raises(exception: type[BaseException], fn, message: str) -> BaseException | None:
    """Assert that *fn* raises *exception*; returns the instance when it does."""
    global checks
    checks += 1
    try:
        fn()
    except exception as exc:  # noqa: BLE001 - the point is to inspect it
        return exc
    except Exception as exc:  # noqa: BLE001
        fail(f"{message} — raised {type(exc).__name__}({exc}) instead")
        return None
    fail(f"{message} — nothing was raised")
    return None


# ── The tables ───────────────────────────────────────────────────────
# `models/agent_hub.py` belongs to slice S0.  When it is present the gate runs
# against the real tables; until then a stand-in with exactly the columns §4.2
# documents is installed under the same module name, so the service code path is
# identical either way.

def _install_models():
    try:
        import models.agent_hub as real  # type: ignore
    except ImportError:
        pass
    else:
        print("ℹ️  using models/agent_hub.py (slice S0) for the real tables")
        return real

    print("ℹ️  models/agent_hub.py absent — building the §4.2 tables as a stand-in")

    from models.base import Base

    class Repo(Base):
        __tablename__ = "repos"
        __table_args__ = (UniqueConstraint("slug", name="uq_repos_slug"),)

        id: Mapped[int] = mapped_column(Integer, primary_key=True)
        slug: Mapped[str] = mapped_column(String(255))
        default_branch: Mapped[str] = mapped_column(String(128), default="main")

    class Finding(Base):
        __tablename__ = "findings"
        __table_args__ = (UniqueConstraint("repo_id", "fingerprint", name="uq_findings_repo_fp"),)

        id: Mapped[int] = mapped_column(Integer, primary_key=True)
        repo_id: Mapped[int] = mapped_column(Integer, ForeignKey("repos.id"))
        fingerprint: Mapped[str] = mapped_column(String(64))
        rule_id: Mapped[str] = mapped_column(String(255))
        level: Mapped[str] = mapped_column(String(32), default="debt")
        severity: Mapped[str] = mapped_column(String(32), default="medium")
        status: Mapped[str] = mapped_column(String(32), default="open")
        file_path: Mapped[str] = mapped_column(String(512))
        symbol: Mapped[str] = mapped_column(String(255))
        line_hint: Mapped[int | None] = mapped_column(Integer, nullable=True)
        title: Mapped[str] = mapped_column(String(512), default="")
        detail: Mapped[str] = mapped_column(Text, default="")
        first_seen_run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
        last_seen_run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
        seen_count: Mapped[int] = mapped_column(Integer, default=1)
        owner: Mapped[str | None] = mapped_column(String(255), nullable=True)
        due: Mapped[date | None] = mapped_column(Date, nullable=True)
        decided_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
        decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
        pr_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
        stop_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
        created_at: Mapped[datetime] = mapped_column(
            DateTime, default=lambda: datetime.now(timezone.utc)
        )
        updated_at: Mapped[datetime] = mapped_column(
            DateTime, default=lambda: datetime.now(timezone.utc)
        )

    class FindingEvent(Base):
        __tablename__ = "finding_events"

        id: Mapped[int] = mapped_column(Integer, primary_key=True)
        finding_id: Mapped[int] = mapped_column(Integer, ForeignKey("findings.id"))
        at: Mapped[datetime] = mapped_column(
            DateTime, default=lambda: datetime.now(timezone.utc)
        )
        actor: Mapped[str] = mapped_column(String(255), default="")
        from_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
        to_status: Mapped[str] = mapped_column(String(32), default="open")
        reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
        run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    class ReviewRun(Base):
        __tablename__ = "review_runs"

        id: Mapped[int] = mapped_column(Integer, primary_key=True)
        repo_id: Mapped[int] = mapped_column(Integer, ForeignKey("repos.id"))
        agent_task_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
        commit_sha: Mapped[str] = mapped_column(String(64), default="")
        policy_hash: Mapped[str] = mapped_column(String(64), default="")
        findings_new: Mapped[int] = mapped_column(Integer, default=0)
        findings_matched: Mapped[int] = mapped_column(Integer, default=0)
        gates_total: Mapped[int] = mapped_column(Integer, default=0)
        gates_passed: Mapped[int] = mapped_column(Integer, default=0)
        gates_failed: Mapped[int] = mapped_column(Integer, default=0)
        status: Mapped[str] = mapped_column(String(32), default="running")
        log_ref: Mapped[str | None] = mapped_column(String(512), nullable=True)
        started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
        finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    class FindingEvidence(Base):
        __tablename__ = "finding_evidence"

        id: Mapped[int] = mapped_column(Integer, primary_key=True)
        finding_id: Mapped[int] = mapped_column(Integer, ForeignKey("findings.id"))
        repo_issue_id: Mapped[int] = mapped_column(Integer)
        relation: Mapped[str] = mapped_column(String(32), default="mentions")

    import types as _types

    module = _types.ModuleType("models.agent_hub")
    for name, value in (
        ("Repo", Repo),
        ("Finding", Finding),
        ("FindingEvent", FindingEvent),
        ("ReviewRun", ReviewRun),
        ("FindingEvidence", FindingEvidence),
        ("FINDING_STATUS", ("open", "acknowledged", "wontfix", "fixed", "stale")),
        ("FINDING_LEVEL", ("blocking", "debt")),
    ):
        setattr(module, name, value)
    sys.modules["models.agent_hub"] = module
    return module


MODELS = _install_models()

from services import findings  # noqa: E402
from services import review_policy  # noqa: E402
from services.findings import (  # noqa: E402
    DriftContext, ReactivationContext, SymbolRange, ranges_intersect,
    symbol_ranges_from_diff,
)


# ── Fixtures ─────────────────────────────────────────────────────────

def new_session():
    """A fresh in-memory database with every declared table, and one repo."""
    engine = create_engine("sqlite://")
    from models.base import Base

    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    session.add(MODELS.Repo(id=1, slug="acme/widgets"))
    session.add(MODELS.Repo(id=2, slug="acme/gadgets"))
    session.flush()
    return session


def make_run(session, number: int, repo_id: int = 1) -> int:
    """A real `review_runs` row for *number*.

    `ingest` refuses a dangling run id on purpose (I5: every finding must be
    traceable to the run that produced it), so the gate has to create the run
    the runner would have created.
    """
    existing = session.get(MODELS.ReviewRun, number)
    if existing is not None:
        return number
    session.add(
        MODELS.ReviewRun(
            id=number,
            repo_id=repo_id,
            commit_sha=f"{number:040x}"[-40:],
            policy_hash="0" * 64,
            status="running",
        )
    )
    session.flush()
    return number


def entry(rule_id="backend.logger-name", file_path="backend/services/foo.py",
          symbol="Foo.bar", **overrides):
    """One §9.5 findings entry, with the fields under test overridable."""
    payload = {
        "rule_id": rule_id,
        "level": "debt",
        "severity": "medium",
        "file_path": file_path,
        "symbol": symbol,
        "line_hint": 42,
        "title": "logger name does not follow the convention",
        "detail": "module-level logger must be cpypiserver.<domain>",
    }
    payload.update(overrides)
    return payload


def ack(session, finding_id, *, actor="dev", owner="team-x", days=30):
    """Drive a finding into `acknowledged` with a future deadline."""
    return findings.decide(
        finding_id,
        "acknowledge",
        actor,
        owner=owner,
        due=date.today() + timedelta(days=days),
        session=session,
    )


def rule_of(session, finding_id):
    return findings.get_finding(session, finding_id)


# ── Checks ───────────────────────────────────────────────────────────

def check_fingerprint() -> None:
    print("── fingerprint §4.4 / I1 ───────────────────────────────────────")
    baseline = findings.fingerprint("backend.logger-name", "backend/services/foo.py", "Foo.bar")
    expect(
        baseline == findings.fingerprint("backend.logger-name", "backend/services/foo.py", "Foo.bar"),
        "the same inputs must produce the same fingerprint",
    )
    expect(
        baseline != findings.fingerprint("backend.logger-name", "backend/services/foo.py", "Foo.baz"),
        "a different symbol must produce a different fingerprint",
    )
    expect(
        baseline != findings.fingerprint("backend.logger-name", "backend/services/bar.py", "Foo.bar"),
        "a different file must produce a different fingerprint",
    )
    expect(
        baseline != findings.fingerprint("backend.other-rule", "backend/services/foo.py", "Foo.bar"),
        "a different rule must produce a different fingerprint",
    )
    expect(
        baseline != findings.fingerprint(
            "backend.logger-name", "backend/services/foo.py", "Foo.bar", "second-occurrence"
        ),
        "a rule-supplied context_key must change the fingerprint",
    )
    expect(len(baseline) == 64 and all(c in "0123456789abcdef" for c in baseline),
           "the fingerprint must be 64 lowercase hex characters")

    # The canonical string is what gets hashed: anything that moves between runs
    # must be absent from it.  This is the I1 regression guard.
    canonical = findings.fingerprint_input(
        "backend.logger-name", "backend/services/foo.py", "Foo.bar"
    )
    expect(canonical == "backend.logger-name\x00backend/services/foo.py\x00Foo.bar\x00",
           f"the hashed string must be the §4.4 tuple, got {canonical!r}")
    for forbidden, label in (
        ("line", "a line number"),
        ("sha", "a commit sha"),
        ("date", "a timestamp"),
        ("42", "the line hint value"),
    ):
        expect(forbidden not in canonical,
               f"{label} must not reach the fingerprint input ({forbidden!r} found)")

    # The digest must be over exactly the §4.4 string, computed through the
    # project's single SHA-256 implementation.  models/agent_hub.py states the
    # same formula for S0's callers, so both are pinned to the same bytes: an
    # edit to either copy that changes the value fails here rather than
    # silently splitting finding identity in two.
    from services.digest import compute_sha256

    with tempfile.NamedTemporaryFile("w+b") as handle:
        handle.write(canonical.encode("utf-8"))
        handle.flush()
        expected = compute_sha256(handle.name, sidecar=False)
    expect(baseline == expected,
           "fingerprint must be the sha256 of the §4.4 canonical string")
    s0_value = MODELS.fingerprint(
        "backend.logger-name", "backend/services/foo.py", "Foo.bar"
    )
    expect(str(s0_value) == baseline,
           f"models.agent_hub.fingerprint and services.findings.fingerprint must "
           f"agree byte-for-byte (S0={s0_value}, S3={baseline})")
    expect("line_hint" not in canonical, "line_hint must never reach the fingerprint")

    # Changing volatile metadata must not move the fingerprint.  A run that
    # re-reports the same problem on another line/sha/date is the same finding.
    volatile_a = entry(line_hint=42)
    volatile_b = entry(line_hint=9001, detail="re-worded by the model")
    volatile_b["commit_sha"] = "deadbeef" * 8
    volatile_b["seen_at"] = "2030-01-01T00:00:00Z"
    fp_a = findings.fingerprint(volatile_a["rule_id"], volatile_a["file_path"], volatile_a["symbol"])
    fp_b = findings.fingerprint(volatile_b["rule_id"], volatile_b["file_path"], volatile_b["symbol"])
    expect(fp_a == fp_b,
           "line_hint / commit sha / timestamp / detail must not change the fingerprint")


def check_dedup() -> None:
    print("── dedup across runs (G4) ──────────────────────────────────────")
    session = new_session()
    batch = [entry(symbol="Foo.bar"), entry(symbol="Foo.baz", rule_id="docs.missing-route-doc")]

    make_run(session, 101, repo_id=1)
    first = findings.ingest(1, 101, batch, session=session)
    expect(first["new"] == 2 and first["matched"] == 0,
           f"first ingest must open 2 findings, got {first}")
    rows = session.query(MODELS.Finding).all()
    expect(len(rows) == 2, f"first ingest must write 2 rows, found {len(rows)}")
    expect(all(row.status == "open" for row in rows), "new findings must open")
    expect(all(row.seen_count == 1 for row in rows), "a new finding starts at seen_count=1")

    # An acknowledged finding must survive a re-report: same identity, advanced
    # observation, status untouched (§6.2 has no condition that fires here).
    target = rows[0]
    ack(session, target.id)
    before = target.seen_count

    # A drifted file that changed somewhere else in the file must not reactivate.
    other_region = DriftContext(
        symbol=SymbolRange(path="backend/services/foo.py", symbol="Foo.bar", start=10, end=20),
        changed=((100, 140),),
        detail="changed elsewhere in the file",
    )
    make_run(session, 102, repo_id=1)
    second = findings.ingest(1, 102, batch, session=session, drift=other_region)
    expect(second["new"] == 0 and second["matched"] == 2,
           f"second ingest must match both findings, got {second}")
    expect(session.query(MODELS.Finding).count() == 2,
           "dedup must not add a row on the second run")
    expect(target.seen_count == before + 1,
           f"seen_count must advance ({before} -> {target.seen_count})")
    expect(target.last_seen_run_id == 102,
           f"last_seen_run_id must follow the newest run, got {target.last_seen_run_id}")
    expect(target.first_seen_run_id == 101,
           f"first_seen_run_id must stay pinned, got {target.first_seen_run_id}")
    expect(target.status == "acknowledged",
           f"a deferral must survive a non-activating re-report, got {target.status}")

    make_run(session, 103, repo_id=1)
    third = findings.ingest(1, 103, batch, session=session)
    expect(third["new"] == 0 and third["matched"] == 2,
           f"a third identical run must still only match, got {third}")
    expect(session.query(MODELS.Finding).count() == 2,
           "three runs of one batch must leave exactly two findings")
    expect(target.seen_count == before + 2, "seen_count must count every re-observation")

    events = findings.events_for(session, target.id)
    expect(len(events) == 2 and events[0]["from_status"] is None and events[0]["to_status"] == "open",
           f"open-then-acknowledge must leave exactly 2 events, got {events}")

    # The run's own counters must add up — they are what the PR description
    # quotes — and a finding may not reference a run that does not exist (I5).
    second_run = session.get(MODELS.ReviewRun, 102)
    expect(int(second_run.findings_new or 0) == 0 and int(second_run.findings_matched or 0) == 2,
           f"run 102 must record 0 new / 2 matched, got "
           f"{second_run.findings_new}/{second_run.findings_matched}")
    first_run = session.get(MODELS.ReviewRun, 101)
    expect(int(first_run.findings_new or 0) == 2,
           f"run 101 must record 2 new, got {first_run.findings_new}")
    expect_raises(
        findings.FindingsError,
        lambda: findings.ingest(1, 9999, batch, session=session),
        "ingesting against a review_run that does not exist must be refused",
    )


def check_activation_drift() -> None:
    print("── §6.2 condition 1: code drift ───────────────────────────────")
    expect(ranges_intersect((10, 20), (15, 25)), "overlapping ranges must intersect")
    expect(ranges_intersect((10, 20), (20, 30)), "touching ranges must intersect")
    expect(not ranges_intersect((10, 20), (21, 30)), "adjacent-but-disjoint ranges must not intersect")
    expect(not ranges_intersect((10, 20), (1, 9)), "ranges before the symbol must not intersect")

    diff = (
        "diff --git a/backend/services/foo.py b/backend/services/foo.py\n"
        "--- a/backend/services/foo.py\n"
        "+++ b/backend/services/foo.py\n"
        "@@ -40,0 +41,3 @@ def bar():\n"
        "+one\n+two\n+three\n"
        "@@ -120 +123 @@ class Other:\n"
        "-old\n+new\n"
        "diff --git a/backend/services/gone.py b/backend/services/gone.py\n"
        "--- a/backend/services/gone.py\n"
        "+++ /dev/null\n"
        "@@ -1,5 +0,0 @@\n"
        "-a\n-b\n-c\n-d\n-e\n"
    )
    ranges = symbol_ranges_from_diff(diff)
    expect(ranges.get("backend/services/foo.py") == ((41, 43), (123, 123)),
           f"parsed changed ranges are wrong: {ranges}")
    expect("backend/services/gone.py" not in ranges,
           "a deleted file has no new-side lines and must not count as drift")

    session = new_session()
    make_run(session, 1, repo_id=1)
    findings.ingest(1, 1, [entry(symbol="Foo.bar")], session=session)
    row = session.query(MODELS.Finding).one()
    ack(session, row.id)

    # Inside the symbol's own region → drift.
    anchor = SymbolRange(path=row.file_path, symbol=row.symbol, start=38, end=60)
    inside = DriftContext(symbol=anchor, changed=ranges["backend/services/foo.py"])
    expect(findings.should_reactivate(row, ReactivationContext(drift=inside)) == findings.REASON_DRIFT,
           "a change inside the symbol's range must reactivate on code_drift")

    # Same file, different function → NOT drift.  This is the degradation the
    # spec calls out: "the file was touched" is not the condition.
    elsewhere = DriftContext(symbol=anchor, changed=((123, 123),))
    expect(findings.should_reactivate(row, ReactivationContext(drift=elsewhere)) is None,
           "a change elsewhere in the same file must not reactivate")

    # An unanchorable symbol must not silently become "the whole file".
    unanchored = DriftContext(symbol=None, changed=((123, 123),), detail="unanchorable")
    expect(findings.should_reactivate(row, ReactivationContext(drift=unanchored)) is None,
           "an unanchored symbol must not be treated as drifting")

    # …and the same judgement through the real ingest path.
    make_run(session, 2, repo_id=1)
    findings.ingest(1, 2, [entry(symbol="Foo.bar")], session=session, drift=elsewhere)
    expect(row.status == "acknowledged",
           "ingest with a foreign-range diff must leave the finding acknowledged")
    make_run(session, 3, repo_id=1)
    findings.ingest(1, 3, [entry(symbol="Foo.bar")], session=session, drift=inside)
    expect(row.status == "open",
           "ingest with an intersecting diff must reactivate to open")
    expect(row.owner is None and row.due is None,
           "a reactivated finding must drop the stale owner/due")
    reactivation = [e for e in findings.events_for(session, row.id)
                    if e["to_status"] == "open" and e["from_status"] == "acknowledged"]
    expect(len(reactivation) == 1 and reactivation[0]["reason"] == findings.REASON_DRIFT,
           f"reactivation must be logged with its reason, got {reactivation}")


def check_activation_due() -> None:
    print("── §6.2 condition 2: debt expiry ──────────────────────────────")
    session = new_session()
    make_run(session, 1, repo_id=1)
    findings.ingest(1, 1, [entry(symbol="Foo.due")], session=session)
    row = session.query(MODELS.Finding).one()
    ack(session, row.id, days=10)
    today = date.today()

    expect(findings.should_reactivate(row, ReactivationContext(today=today)) is None,
           "a future due date must not reactivate")
    expect(findings.should_reactivate(row, ReactivationContext(today=row.due + timedelta(days=1)))
           == findings.REASON_DUE,
           "a due date in the past must reactivate on due")
    expect(findings.should_reactivate(row, ReactivationContext(today=row.due)) == findings.REASON_DUE,
           "the deadline itself (due <= today) must reactivate")

    # Driving the clock forward through ingest, as the nightly sweep would.
    future = (row.due + timedelta(days=1))
    make_run(session, 2, repo_id=1)
    findings.ingest(1, 2, [entry(symbol="Foo.due")], session=session, today=future)
    expect(row.status == "open",
           "an expired deferral must be reactivated by the next run")

    # §12.1 anti-spiral: a human "stop reactivating" override silences the
    # deadline, and only an escalation may still fire.
    make_run(session, 3, repo_id=2)
    findings.ingest(2, 3, [entry(symbol="Foo.due")], session=session)
    other = session.query(MODELS.Finding).filter(MODELS.Finding.repo_id == 2).one()
    ack(session, other.id, days=10)
    stopped = ReactivationContext(today=other.due + timedelta(days=5), stop_reason="accepted-risk")
    expect(findings.should_reactivate(other, stopped) is None,
           "a stop_reason must silence the deadline condition")
    escalating = ReactivationContext(
        today=other.due + timedelta(days=5), stop_reason="accepted-risk",
        rule_open_count=5, rule_threshold=3,
    )
    expect(findings.should_reactivate(other, escalating) == findings.REASON_ESCALATION,
           "an escalation must still fire through a stop_reason")


def check_activation_escalation() -> None:
    print("── §6.2 condition 3: escalation ───────────────────────────────")
    session = new_session()
    policy = _policy(rule_count_threshold=2)
    batch = [
        entry(symbol="Foo.one"),
        entry(symbol="Foo.two"),
        entry(symbol="Foo.three"),
    ]
    make_run(session, 1, repo_id=2)
    findings.ingest(2, 1, batch, session=session)
    rows = session.query(MODELS.Finding).order_by(MODELS.Finding.id).all()
    for row in rows:
        ack(session, row.id, days=30)

    # §6.2 condition 3 is a statement about the rule, not about one row: the
    # run that pushes the count over the threshold may be reporting a symbol
    # nobody has deferred.  So the *next* run's escalation sweep must open
    # every deferred finding of the rule at once.
    make_run(session, 2, repo_id=2)
    findings.ingest(2, 2, [entry(symbol="Foo.four")], session=session,
                    run_context={"policy": policy})
    expect(
        [row.status for row in rows] == ["open", "open", "open"],
        f"exceeding rule_count_threshold must reopen every deferral, got "
        f"{[row.status for row in rows]}",
    )
    reasons = [e["reason"] for e in findings.events_for(session, rows[0].id)]
    expect(findings.REASON_ESCALATION in reasons,
           f"the escalation reason must be logged, got {reasons}")
    expect(all(row.owner is None and row.due is None for row in rows),
           "an escalated finding must drop the stale owner/due")

    # Without a policy threshold there is no escalation, so the deferrals hold.
    session2 = new_session()
    make_run(session2, 1, repo_id=2)
    findings.ingest(2, 1, batch, session=session2)
    rows2 = session2.query(MODELS.Finding).order_by(MODELS.Finding.id).all()
    for row in rows2:
        ack(session2, row.id, days=30)
    make_run(session2, 2, repo_id=2)
    findings.ingest(2, 2, [entry(symbol="Foo.four")], session=session2)
    expect(all(row.status == "acknowledged" for row in rows2),
           "with no threshold in scope, defers must not be escalated")

    # A re-judged-higher severity is the other half of condition 3.
    escalated_severity = ReactivationContext(previous_severity="medium", current_severity="critical")
    expect(findings.should_reactivate(rows2[0], escalated_severity) == findings.REASON_ESCALATION,
           "a higher severity must reactivate")
    lowered = ReactivationContext(previous_severity="critical", current_severity="low")
    expect(findings.should_reactivate(rows2[0], lowered) is None,
           "a lower severity must not reactivate")


def check_invariant_i2() -> None:
    print("── I2: wontfix/acknowledge need owner + due ────────────────────")
    session = new_session()
    make_run(session, 1, repo_id=1)
    findings.ingest(1, 1, [entry(symbol="Foo.i2a"), entry(symbol="Foo.i2b"), entry(symbol="Foo.i2c")],
                    session=session)
    rows = session.query(MODELS.Finding).order_by(MODELS.Finding.id).all()

    expect_raises(
        findings.DecisionInvalidError,
        lambda: findings.decide(rows[0].id, "wontfix", "dev", due=date.today() + timedelta(days=1),
                                session=session),
        "wontfix without owner must be refused",
    )
    expect_raises(
        findings.DecisionInvalidError,
        lambda: findings.decide(rows[0].id, "wontfix", "dev", owner="team-x", session=session),
        "wontfix without due must be refused",
    )
    expect_raises(
        findings.DecisionInvalidError,
        lambda: findings.decide(rows[1].id, "acknowledge", "dev", owner="team-x", session=session),
        "acknowledge without due must be refused",
    )
    expect_raises(
        findings.DecisionInvalidError,
        lambda: findings.decide(rows[2].id, "acknowledge", "dev", due=date.today(),
                                session=session),
        "acknowledge without owner must be refused",
    )
    expect(rows[0].status == "open" and rows[1].status == "open" and rows[2].status == "open",
           "a refused decision must not change any status")
    expect(
        all(e["from_status"] is None for e in findings.events_for(session, rows[0].id)),
        "a refused decision must not write a FindingEvent",
    )

    ack(session, rows[1].id)
    expect(rows[1].status == "acknowledged" and rows[1].owner == "team-x" and rows[1].due is not None,
           "a complete acknowledge must be accepted and record owner+due")
    expect_raises(
        findings.DecisionInvalidError,
        lambda: findings.decide(rows[2].id, "explode", "dev", session=session),
        "an unknown action must be refused",
    )


def check_invariant_i3() -> None:
    print("── I3: a blocking finding may not be deferred ──────────────────")
    session = new_session()
    make_run(session, 1, repo_id=1)
    findings.ingest(1, 1, [
        entry(symbol="Foo.blk1", level="blocking"),
        entry(symbol="Foo.blk2", level="blocking"),
        entry(symbol="Foo.blk3", level="blocking"),
    ], session=session)
    rows = session.query(MODELS.Finding).order_by(MODELS.Finding.id).all()
    due = date.today() + timedelta(days=30)

    expect_raises(
        findings.DecisionInvalidError,
        lambda: findings.decide(rows[0].id, "wontfix", "dev", owner="team-x", due=due,
                                session=session),
        "blocking + wontfix must be refused even with owner and due",
    )
    expect_raises(
        findings.DecisionInvalidError,
        lambda: findings.decide(rows[1].id, "acknowledge", "dev", owner="team-x", due=due,
                                session=session),
        "blocking + acknowledge must be refused even with owner and due",
    )
    findings.decide(rows[2].id, "fixed", "dev", session=session)
    expect(rows[2].status == "fixed", "blocking + fixed must be accepted")

    # §6.3: a confirmed false positive is the other way out of blocking, and it
    # needs a second person.
    make_run(session, 2, repo_id=1)
    findings.ingest(1, 2, [entry(symbol="Foo.blk4", level="blocking")], session=session)
    fourth = session.query(MODELS.Finding).order_by(MODELS.Finding.id.desc()).first()
    expect_raises(
        findings.DecisionInvalidError,
        lambda: findings.decide(fourth.id, "fixed", "dev", reason="false_positive", session=session),
        "an unconfirmed false positive must be refused",
    )
    expect_raises(
        findings.DecisionInvalidError,
        lambda: findings.decide(fourth.id, "fixed", "dev", reason="false_positive",
                                confirmed_by="dev", session=session),
        "the confirmation must come from someone else",
    )
    findings.decide(fourth.id, "fixed", "dev", reason="false_positive",
                    confirmed_by="reviewer-2", session=session)
    expect(fourth.status == "fixed",
           "a second-person-confirmed false positive must fix a blocking finding")


def check_rule_governance() -> None:
    print("── §6.3 rule governance (one meta finding) ─────────────────────")
    session = new_session()
    threshold = 3
    policy = _policy(rule_noise_threshold=threshold)
    entries = [entry(rule_id="docs.missing-route-doc", symbol=f"Route.handler{i}") for i in range(threshold)]
    make_run(session, 1, repo_id=1)
    findings.ingest(1, 1, entries, session=session)
    rows = session.query(MODELS.Finding).order_by(MODELS.Finding.id).all()

    before = findings.rule_quality_watch(1, session=session, policy=policy)
    expect(before["created"] == [] and before["noisy"] == [],
           f"noisy rules must not be flagged before the threshold, got {before}")

    for index, row in enumerate(rows):
        findings.decide(row.id, "fixed", f"dev-{index}", reason="false_positive",
                        confirmed_by="reviewer-2", session=session)

    first = findings.rule_quality_watch(1, session=session, policy=policy)
    expect(first["created"] and first["noisy"] == ["docs.missing-route-doc"],
           f"the noisy rule must produce a governance finding, got {first}")
    meta = (
        session.query(MODELS.Finding)
        .filter(MODELS.Finding.rule_id == findings.RULE_QUALITY_RULE_ID)
        .all()
    )
    expect(len(meta) == 1, f"exactly one meta.rule-quality finding must exist, found {len(meta)}")
    expect(meta[0].fingerprint == findings.fingerprint(
        findings.RULE_QUALITY_RULE_ID, findings.POLICY_ENTRY_PATH,
        findings.RULE_QUALITY_SYMBOL, "docs.missing-route-doc",
    ), "the governance finding must key on the offending rule via context_key")
    expect(meta[0].file_path == ".agent/review-policy.yml",
           f"it must point at the policy entry, got {meta[0].file_path}")
    expect("docs.missing-route-doc" in meta[0].detail,
           "its detail must name the rule that needs fixing")

    second = findings.rule_quality_watch(1, session=session, policy=policy)
    third = findings.rule_quality_watch(1, session=session, policy=policy)
    still = (
        session.query(MODELS.Finding)
        .filter(MODELS.Finding.rule_id == findings.RULE_QUALITY_RULE_ID)
        .all()
    )
    expect(len(still) == 1,
           f"repeated watches must be idempotent, found {len(still)} governance findings")
    expect(second["created"] == [] and third["created"] == [] and second["updated"],
           f"later watches must update, not create: {second} {third}")

    # A different repository must get its own governance finding, not a lookup
    # hit from the first one.
    make_run(session, 2, repo_id=2)
    findings.ingest(2, 2, [entry(rule_id="docs.route-table-drift", symbol="Docs.page")], session=session)
    other = (
        session.query(MODELS.Finding)
        .filter(MODELS.Finding.repo_id == 2, MODELS.Finding.rule_id == "docs.route-table-drift")
        .one()
    )
    findings.decide(other.id, "fixed", "dev", reason="false_positive", confirmed_by="reviewer-2",
                    session=session)
    isolated = findings.rule_quality_watch(2, session=session, policy=policy)
    expect(isolated["noisy"] == [], "one false positive must not flag a rule")
    expect(
        session.query(MODELS.Finding)
        .filter(MODELS.Finding.repo_id == 2, MODELS.Finding.rule_id == findings.RULE_QUALITY_RULE_ID)
        .count() == 0,
        "a governance finding must be scoped to its repository",
    )


def check_autofix() -> None:
    print("── §6.4 autofix boundary ──────────────────────────────────────")
    policy = _policy(rules=[
        {"id": "backend.logger-name", "level": "debt", "autofix": True},
        {"id": "backend.service-split", "level": "debt", "autofix": False},
    ])
    expect(findings.autofix_allowed("backend.logger-name", policy) is True,
           "a rule that opted in must be auto-fixable")
    expect(findings.autofix_allowed("backend.service-split", policy) is False,
           "a rule that opted out must not be auto-fixable")
    expect(findings.autofix_allowed("docs.missing-route-doc", policy) is False,
           "an unknown rule must default to not auto-fixable")
    expect(findings.autofix_allowed("backend.logger-name", None) is False,
           "no policy at all must mean not auto-fixable")
    builtin = review_policy.builtin_default()
    expect(all(builtin.rule(rule_id)["autofix"] is False for rule_id in review_policy.BUILTIN_RULES),
           "every rule in the built-in default must have autofix false")
    expect(all(builtin.rule(rule_id)["level"] == "blocking"
               for rule_id in review_policy.BUILTIN_RULES),
           "every rule in the built-in default must be blocking")


def check_policy() -> None:
    print("── §7 review policy ───────────────────────────────────────────")
    # 1. A missing file yields the read-only built-in default, and says so.
    missing = Path(tempfile.mkdtemp(prefix="cpypi-policy-")) / "nope"
    loaded = review_policy.load(missing)
    expect(loaded.policy_source == review_policy.SOURCE_BUILTIN,
           "an absent policy file must report policy_source=builtin-default")
    expect(loaded.readonly is True, "the built-in default must be read-only")
    expect(loaded.policy_hash and len(loaded.policy_hash) == 64,
           "the built-in default must still carry a policy_hash")
    expect(review_policy.builtin_default().policy_source == "builtin-default",
           "the builtin_default() marker must be exactly 'builtin-default'")

    root = Path(tempfile.mkdtemp(prefix="cpypi-policy-"))
    policy_dir = root / ".agent"
    policy_dir.mkdir(parents=True)

    # 2. A good document round-trips, with a stable hash.
    text = (
        "version: 1\n"
        "defaults:\n"
        "  auto_review: true\n"
        "  max_findings_per_run: 50\n"
        "rules:\n"
        "  - id: backend.no-typing-optional\n"
        "    level: blocking\n"
        "    autofix: true\n"
        "  - id: docs.missing-route-doc\n"
        "    level: debt\n"
        "    default_due_days: 30\n"
        "    autofix: false\n"
        "exceptions:\n"
        "  - rule: backend.openapi-schema-order\n"
        "    paths: [\"backend/routes/pypi.py\"]\n"
        "    reason: PEP 563 + flask-pydantic\n"
        "    decided_by: shaojun0\n"
        f"    due: {(date.today() + timedelta(days=200)).isoformat()}\n"
        "    status: wontfix\n"
        "escalation:\n"
        "  rule_noise_threshold: 3\n"
        "  rule_count_threshold: 20\n"
    )
    (policy_dir / "review-policy.yml").write_text(text, encoding="utf-8")
    good = review_policy.load(root)
    expect(good.policy_source == review_policy.SOURCE_FILE,
           "an existing file must report policy_source=file")
    expect(good.policy_hash != loaded.policy_hash,
           "a real policy must hash differently from the built-in default")
    expect(good.exception_for("backend.openapi-schema-order", "backend/routes/pypi.py") is not None,
           "an exception must cover the path it names")
    expect(good.exception_for("backend.openapi-schema-order", "backend/routes/other.py") is None,
           "an exception must not cover a path it does not name")
    expect(good.effective_level("backend.no-typing-optional") == "blocking",
           "a blocking rule must stay blocking without an exception")
    expect(good.effective_level("docs.missing-route-doc") == "debt",
           "a debt rule must be debt")
    # §7.3: an exception on an unknown rule is a warning, not a failure.
    expect(any("backend.openapi-schema-order" in warning for warning in good.warnings),
           f"an exception naming an unknown rule must warn, got {good.warnings}")

    # 3. The hash is over meaning: formatting must not move it, content must.
    #    A comment, a re-ordered key and a flow-style list are all "the same
    #    policy" — the first run after such a commit is still comparable.
    reformatted = (
        "# Reviewed by the platform team; see §7 of the Agent Hub spec.\n"
        "version: 1\n"
        "defaults:\n"
        "  max_findings_per_run: 50\n"
        "  auto_review: true\n"
        "rules:\n"
        "  - {id: backend.no-typing-optional, level: blocking, autofix: true}\n"
        "  - {id: docs.missing-route-doc, level: debt, default_due_days: 30, autofix: false}\n"
        "exceptions:\n"
        "  - rule: backend.openapi-schema-order\n"
        "    paths: [\"backend/routes/pypi.py\"]\n"
        "    reason: PEP 563 + flask-pydantic\n"
        "    decided_by: shaojun0\n"
        f"    due: {(date.today() + timedelta(days=200)).isoformat()}\n"
        "    status: wontfix\n"
        "escalation:\n"
        "  rule_count_threshold: 20\n"
        "  rule_noise_threshold: 3\n"
    )
    (policy_dir / "review-policy.yml").write_text(reformatted, encoding="utf-8")
    same_meaning = review_policy.load(root)
    expect(same_meaning.policy_hash == good.policy_hash,
           "comments, key order and formatting must not change the policy hash")
    expect(review_policy.hash_changed(good.policy_hash, same_meaning.policy_hash) is False,
           "hash_changed must be false for a formatting-only edit")

    (policy_dir / "review-policy.yml").write_text(
        text.replace("default_due_days: 30", "default_due_days: 7"), encoding="utf-8"
    )
    changed = review_policy.load(root)
    expect(changed.policy_hash != good.policy_hash,
           "editing a rule must change the policy hash")
    expect(review_policy.hash_changed(good.policy_hash, changed.policy_hash) is True,
           "hash_changed must be true after a semantic edit (§7.1)")
    expect(review_policy.hash_changed(None, changed.policy_hash) is False,
           "the first run has nothing to compare against, so nothing changed")
    expect(review_policy.hash_changed(good.policy_hash, None) is False,
           "a missing current hash must not claim a change")

    # 4. §7.3 constraints.
    expect_raises(
        review_policy.PolicyValidationError,
        lambda: review_policy.parse_yaml("- rule: x\n  due: 2026-01-01\n") and
        review_policy.parse_document({"exceptions": [{"rule": "x", "status": "wontfix"}]}),
        "an exception without due must be rejected",
    )
    expect_raises(
        review_policy.PolicyValidationError,
        lambda: review_policy.parse_document({
            "exceptions": [{
                "rule": "x",
                "due": (date.today() - timedelta(days=1)).isoformat(),
                "status": "wontfix",
            }]
        }),
        "an expired exception due must be rejected",
    )
    expect_raises(
        review_policy.PolicyValidationError,
        lambda: review_policy.parse_document({"exceptions": [{"rule": "x", "due": "not-a-date"}]}),
        "a malformed due must be rejected",
    )
    expect_raises(
        review_policy.PolicyValidationError,
        lambda: review_policy.parse_document({"rules": [{"id": "a"}, {"id": "a"}]}),
        "a duplicated rule id must be rejected",
    )
    expect_raises(
        review_policy.PolicyValidationError,
        lambda: review_policy.parse_document({"rules": [{"id": "a", "level": "medium"}]}),
        "an unknown level must be rejected",
    )
    accepted = review_policy.parse_document({
        "rules": [{"id": "a", "level": "debt"}],
        "exceptions": [{
            "rule": "a",
            "due": (date.today() + timedelta(days=1)).isoformat(),
            "status": "wontfix",
        }],
    })
    expect(accepted.warnings == [], f"a fully-known exception must not warn: {accepted.warnings}")


def check_dependency_fallback() -> None:
    print("── pyyaml is optional (graceful degradation) ──────────────────")
    # Reading a *file* needs pyyaml; the built-in default does not.  An install
    # without the package must fail with our own error, and unrelated slices
    # (contract checks, the routes) must keep importing.
    expect_raises(
        review_policy.PolicyDependencyError,
        _yaml_missing_probe,
        "a missing pyyaml must raise PolicyDependencyError, not ImportError",
    )
    expect(review_policy.builtin_default().policy_source == "builtin-default",
           "the built-in default must still load without pyyaml")


def _yaml_missing_probe() -> None:
    """Simulate an install without pyyaml and assert the failure is ours.

    ``services.review_policy`` loads the package through
    ``importlib.import_module``, so both that and ``__import__`` are blocked.
    A snapshot/restore of ``sys.modules`` and the two import hooks keeps the
    probe from leaking into the checks that follow.
    """
    snapshot = dict(sys.modules)
    real_import = builtins.__import__
    real_import_module = importlib.import_module

    def _blocked_import(name, *args, **kwargs):
        if name == "yaml" or name.startswith("yaml."):
            raise ImportError("No module named 'yaml'")
        return real_import(name, *args, **kwargs)

    def _blocked_module(name, *args, **kwargs):
        if name == "yaml" or name.startswith("yaml."):
            raise ImportError("No module named 'yaml'")
        return real_import_module(name, *args, **kwargs)

    builtins.__import__ = _blocked_import
    importlib.import_module = _blocked_module
    sys.modules.pop("yaml", None)
    try:
        review_policy.parse_yaml("version: 1\n")
    finally:
        builtins.__import__ = real_import
        importlib.import_module = real_import_module
        sys.modules.clear()
        sys.modules.update(snapshot)


def check_symbol_range() -> None:
    print("── symbol anchoring (pure) ────────────────────────────────────")
    source = (
        "import os\n"
        "\n"
        "logger = os\n"
        "\n"
        "class Foo:\n"
        "    def bar(self):\n"
        "        return 1\n"
        "\n"
        "    def baz(self):\n"
        "        return 2\n"
        "\n"
        "def top():\n"
        "    return 3\n"
    )
    bar = findings.symbol_range(source, path="backend/services/foo.py", symbol="Foo.bar")
    expect(bar is not None and (bar.start, bar.end) == (6, 7),
           f"Foo.bar must resolve to lines 6-7, got {bar}")
    baz = findings.symbol_range(source, path="backend/services/foo.py", symbol="Foo.baz")
    expect(baz is not None and (baz.start, baz.end) == (9, 10),
           f"Foo.baz must resolve to lines 9-10, got {baz}")
    top = findings.symbol_range(source, path="backend/services/foo.py", symbol="top")
    expect(top is not None and (top.start, top.end) == (12, 13),
           f"a module-level function must resolve, got {top}")
    expect(findings.symbol_range(source, path="x.py", symbol="nope") is None,
           "an unknown symbol must not be anchored")
    expect(findings.symbol_range(source, path="x.py", symbol="") is None,
           "an empty symbol must not be anchored")
    # A non-Python file falls back to the textual def/class anchor.
    text = "first line\ndef thing():\n    pass\n\nclass Other:\n    pass\n"
    found = findings.symbol_range(text, path="Makefile", symbol="thing")
    expect(found is not None and found.start == 2,
           f"a textual fallback anchor must find 'thing', got {found}")


def check_git_adapter() -> None:
    print("── git drift adapter (read-only, real git) ────────────────────")
    if shutil.which("git") is None:
        print("ℹ️  git is not on PATH — adapter check skipped")
        return

    scratch = Path(tempfile.mkdtemp(prefix="cpypi-drift-"))
    path = scratch / "backend" / "services"
    path.mkdir(parents=True)
    target = path / "foo.py"
    original = (
        "def bar():\n"
        "    return 1\n"
        "\n"
        "\n"
        "def untouched():\n"
        "    return 2\n"
    )
    target.write_text(original, encoding="utf-8")

    def git(*args: str) -> str:
        proc = subprocess.run(
            ["git", "-C", str(scratch), *args],
            capture_output=True, text=True, check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr}")
        return proc.stdout

    try:
        git("init", "-q")
        git("config", "user.email", "gate@example.invalid")
        git("config", "user.name", "gate")
        git("add", "-A")
        git("commit", "-q", "-m", "base")
        base = git("rev-parse", "HEAD").strip()

        # 1. Only a comment above the function changes: not the symbol's region.
        target.write_text("# a new comment\n" + original, encoding="utf-8")
        git("add", "-A")
        git("commit", "-q", "-m", "comment above")
        head_comment = git("rev-parse", "HEAD").strip()
        adapter = findings.GitDriftAdapter(scratch)
        above = adapter.drift_context(
            file_path="backend/services/foo.py", symbol="bar",
            base=base, head=head_comment,
        )
        expect(above.symbol is not None, "the adapter must anchor a real symbol")
        expect(not above.symbol.overlaps(above.changed),
               f"a comment above the function is not drift: {above.detail}")

        # 2. The function's own body changes: drift.
        target.write_text(original.replace("return 1", "return 42"), encoding="utf-8")
        git("add", "-A")
        git("commit", "-q", "-m", "body change")
        head_body = git("rev-parse", "HEAD").strip()
        inside = adapter.drift_context(
            file_path="backend/services/foo.py", symbol="bar",
            base=base, head=head_body,
        )
        expect(inside.symbol is not None and inside.symbol.overlaps(inside.changed),
               f"a change inside the symbol's body is drift: {inside.detail}")
        # …and the same diff is not drift for a *different* symbol in the file.
        other = adapter.drift_context(
            file_path="backend/services/foo.py", symbol="untouched",
            base=base, head=head_body,
        )
        expect(not other.symbol.overlaps(other.changed),
               "a change in one function is not drift for another function")

        # 3. The adapter never writes: the work tree is untouched by the reads.
        expect(target.read_text(encoding="utf-8").startswith("def bar():"),
               "the adapter must not rewrite the file it reads")
        expect(git("status", "--porcelain") == "",
               "the adapter must leave the working tree clean")
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _policy(*, rules=None, rule_noise_threshold=3, rule_count_threshold=20):
    """A policy document built in memory, for the activation/governance checks."""
    payload = {
        "version": 1,
        "rules": rules if rules is not None else [
            {"id": "backend.logger-name", "level": "debt", "autofix": False},
        ],
        "escalation": {
            "rule_noise_threshold": rule_noise_threshold,
            "rule_count_threshold": rule_count_threshold,
        },
    }
    return review_policy.parse_document(payload)


# ── Report ───────────────────────────────────────────────────────────

def main() -> int:
    print(f"📋 findings gate — {checks if checks else 0} checks so far")
    check_fingerprint()
    check_dedup()
    check_activation_drift()
    check_activation_due()
    check_activation_escalation()
    check_invariant_i2()
    check_invariant_i3()
    check_rule_governance()
    check_autofix()
    check_policy()
    check_dependency_fallback()
    check_symbol_range()
    check_git_adapter()

    print()
    if failures:
        print(f"❌ findings check FAILED — {len(failures)} of {checks} check(s)")
        for message in failures:
            print(f"   • {message}")
        return 1
    print(f"✅ findings check passed — {checks} checks over §4.4 / §6 / §7")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
