"""Findings — stable identity, dedup, the state machine, and rule governance.

A *finding* is not a review comment: it is a long-lived object with an identity
that survives re-runs.  That identity is the **fingerprint** (§4.4), and it is
the only thing dedup is allowed to look at — never text similarity, never the
line number, never the commit.  Everything else in this module is the lifecycle
built on top of it:

* :func:`ingest` — fold one run's findings into the table: an existing
  fingerprint is *advanced* (``seen_count`` / ``last_seen_run_id``, and
  reactivation under §6.2), an unknown one is *opened* and its first
  ``FindingEvent`` written.
* :func:`decide` — the §6.1 migration table, with I2 (``wontfix`` needs
  ``owner`` **and** ``due``) and I3 (a ``blocking`` finding may not be
  ``wontfix``/``acknowledge``d) enforced as hard rejections.
* :func:`should_reactivate` — the *only* three activation conditions of §6.2.
  Condition 1 (code drift) is decided by intersecting the diff's changed line
  ranges with the symbol's own line range, not by "was the file touched".  The
  intersection and the diff parsing are pure functions so they can be unit
  tested without a git checkout; the git call itself is behind
  :class:`GitDriftAdapter`, which only ever runs read-only ``git`` commands.
* :func:`rule_quality_watch` — §6.3: a rule that produces
  ``rule_noise_threshold`` false positives inside 30 days earns one
  ``meta.rule-quality`` finding pointing at its own policy entry.  That is the
  designed brake for the §12.1 noise spiral.
* :func:`autofix_allowed` — §6.4, answered from policy, defaulting to
  ``False``.

The ORM classes live in ``models/agent_hub.py`` (slice S0) and are loaded
lazily through :func:`_models`, so this module imports cleanly in a checkout
where that file is not installed yet and a missing model is a clear
:class:`FindingsModelError` at call time rather than a broken import at startup.
"""

from __future__ import annotations

import ast
import importlib
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from services.digest import compute_sha256
from services.review_policy import POLICY_RELATIVE_PATH


# ── Enum surface (§4.3) ──────────────────────────────────────────────
# The canonical tuples live in models/agent_hub.py.  These are the fallbacks
# used while S0's module is not importable, and the source of the module-local
# names below; they are never written into the database as literals.

FINDING_STATUS: tuple[str, ...] = ("open", "acknowledged", "wontfix", "fixed", "stale")
FINDING_LEVEL: tuple[str, ...] = ("blocking", "debt")

STATUS_OPEN = "open"
STATUS_ACKNOWLEDGED = "acknowledged"
STATUS_WONTFIX = "wontfix"
STATUS_FIXED = "fixed"
STATUS_STALE = "stale"

LEVEL_BLOCKING = "blocking"
LEVEL_DEBT = "debt"

#: Actions accepted by :func:`decide`.  ``false_positive`` is a *reason* for a
#: ``fixed`` transition, not a status of its own.
DECISION_ACTIONS: tuple[str, ...] = ("fix", "fixed", "acknowledge", "wontfix", "false_positive")

#: The deferring actions and the status each one lands on.  ``acknowledge`` (the
#: action) and ``acknowledged`` (the status) are deliberately different words,
#: so the mapping is written out rather than inferred from spelling.
ACTION_ACKNOWLEDGE = "acknowledge"
DEFER_ACTIONS: dict[str, str] = {
    ACTION_ACKNOWLEDGE: STATUS_ACKNOWLEDGED,
    STATUS_WONTFIX: STATUS_WONTFIX,
}

#: Reasons that ride along on a :class:`FindingEvent`.
REASON_FALSE_POSITIVE = "false_positive"
REASON_ACCEPTED = "accepted"
REASON_WONTFIX = "wontfix"
REASON_DRIFT = "code_drift"
REASON_DUE = "due"
REASON_ESCALATION = "escalation"
REASON_MATCHED = "matched"

#: Severity ordering used by activation condition 3 (re-judged higher).
_SEVERITY_RANK: dict[str, int] = {
    "low": 0,
    "medium": 1,
    "high": 2,
    "critical": 3,
}

#: ``rule_id`` of the governance finding §6.3 creates.
RULE_QUALITY_RULE_ID = "meta.rule-quality"

#: Window over which false positives are counted for §6.3.
RULE_QUALITY_WINDOW_DAYS = 30

#: ``symbol`` used by the governance finding; it is what keeps the fingerprint
#: stable across calls.
RULE_QUALITY_SYMBOL = "review-policy.rules"

#: A governance finding points at the policy file, since that is the artifact a
#: human has to edit to resolve it.  Imported so the two slices cannot drift.
POLICY_ENTRY_PATH = POLICY_RELATIVE_PATH


class FindingsError(Exception):
    """Base class for this module's domain errors."""


class FindingsModelError(FindingsError):
    """``models/agent_hub.py`` is not installed — the table cannot be touched."""


class FindingNotFoundError(FindingsError):
    """No finding carries that id (or repository scope)."""

    def __init__(self, finding_id: int) -> None:
        super().__init__(f"finding {finding_id} does not exist")
        self.finding_id = finding_id


class DecisionInvalidError(FindingsError):
    """The §6.1 migration table refuses this transition (I2 / I3).

    The route maps this to ``409 finding_decision_invalid``, per §5.3.
    """

    def __init__(self, message: str, *, code: str = "finding_decision_invalid") -> None:
        super().__init__(message)
        self.message = message
        self.code = code


class PolicyUnavailableError(FindingsError):
    """The policy drive could not be read for a governance/autofix decision."""


# ── Model loading (S0 owns the tables) ───────────────────────────────

_MODELS: Any = None


def _models() -> Any:
    """The ``models.agent_hub`` module, or a precise error explaining why not."""
    global _MODELS
    if _MODELS is None:
        try:
            _MODELS = importlib.import_module("models.agent_hub")
        except ImportError as exc:  # pragma: no cover - exercised until S0 lands
            raise FindingsModelError(
                "models/agent_hub.py is not importable "
                f"({exc}); slice S3 needs the S0 tables "
                "(findings / finding_events / review_runs / finding_evidence)"
            ) from exc
    return _MODELS


# ── fingerprint (§4.4, invariant I1) ─────────────────────────────────

def fingerprint(
    rule_id: str,
    file_path: str,
    symbol: str,
    context_key: str = "",
) -> str:
    """``sha256(rule_id \\x00 file_path \\x00 symbol \\x00 context_key)``.

    Byte-for-byte the §4.4 formula.  Nothing that moves between runs may enter
    this string: no line number, no commit sha, no timestamp, no text summary.
    ``line_hint`` is deliberately absent even though callers carry it.

    The digest comes from :func:`services.digest.compute_sha256`, so the project
    keeps exactly one hashing loop.  ``models/agent_hub.fingerprint`` (slice S0)
    states the same §4.4 formula; the offline gate asserts the two agree
    byte-for-byte, so an edit to either keeps the other honest.
    """
    payload = fingerprint_input(rule_id, file_path, symbol, context_key)
    with tempfile.NamedTemporaryFile("w+b") as handle:
        handle.write(payload.encode("utf-8"))
        handle.flush()
        return compute_sha256(handle.name, sidecar=False)


def fingerprint_input(
    rule_id: str,
    file_path: str,
    symbol: str,
    context_key: str = "",
) -> str:
    """The exact canonical string :func:`fingerprint` hashes.

    Exposed for the offline gate, which asserts that changing a line number or
    a commit sha cannot possibly reach the digest; it is not a second
    implementation of anything.
    """
    return "\x00".join((str(rule_id), str(file_path), str(symbol), str(context_key)))


# ── Code drift: ranges, diff parsing, symbol anchoring ───────────────

class DriftError(FindingsError):
    """The read-only drift probe could not produce a trustworthy answer."""


Ranges = tuple[tuple[int, int], ...]

#: ``git diff -U0`` hunk header: ``@@ -1,4 +1,6 @@`` — we only need the new side.
_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")

#: A Python ``def`` / ``class`` line, used for non-Python fallbacks.
_ANYDEF_RE = re.compile(r"^\s*(?:async\s+)?(?:def|class)\s+([A-Za-z_][A-Za-z0-9_]*)")

#: File git reports for a deletion; nothing lives there any more.
_NULL_PATH = "/dev/null"


@dataclass(frozen=True)
class SymbolRange:
    """A line interval (1-based, inclusive) a symbol occupies in a file."""

    path: str
    symbol: str
    start: int
    end: int

    def overlaps(self, ranges: Ranges) -> bool:
        return any(
            ranges_intersect((self.start, self.end), item) for item in ranges
        )


@dataclass(frozen=True)
class DriftContext:
    """Everything condition 1 needs, once git has answered."""

    symbol: SymbolRange | None = None
    changed: Ranges = ()
    detail: str = ""


def ranges_intersect(left: tuple[int, int], right: tuple[int, int]) -> bool:
    """Whether two inclusive 1-based intervals share at least one line.

    Pure, and the whole of the "was *this symbol* touched" judgement: a file
    whose changed lines all sit in another function must not reactivate a
    finding, which is precisely the "文件被碰过" degradation §6.2 forbids.
    """
    a_start, a_end = (min(left[0], left[1]), max(left[0], left[1]))
    b_start, b_end = (min(right[0], right[1]), max(right[0], right[1]))
    return a_start <= b_end and b_start <= a_end


def merge_ranges(ranges: Ranges) -> Ranges:
    """Sort and coalesce intervals; the canonical form used for comparisons."""
    ordered = sorted((min(a, b), max(a, b)) for a, b in ranges)
    merged: list[tuple[int, int]] = []
    for start, end in ordered:
        if merged and start <= merged[-1][1] + 1:
            previous_start, previous_end = merged[-1]
            merged[-1] = (previous_start, max(previous_end, end))
        else:
            merged.append((start, end))
    return tuple(merged)


def symbol_ranges_from_diff(diff_text: str) -> dict[str, Ranges]:
    """``{new path: ((start, end), …)}`` from a unified diff (``-U0``).

    Pure text in, ranges out: the offline gate exercises drift with hand-written
    hunks and never shells out.  A hunk of length 0 (a pure deletion) adds no
    line to the new side and is therefore not drift by itself.
    """
    changed: dict[str, list[tuple[int, int]]] = {}
    current: str | None = None
    for line in diff_text.splitlines():
        if line.startswith("+++ "):
            path = line[4:].strip()
            if path.startswith('"') and path.endswith('"'):
                # Quoted paths appear when the name holds non-ASCII bytes.
                path = path[1:-1]
            current = None if path == _NULL_PATH else (path[2:] if path.startswith("b/") else path)
            if current is not None:
                changed.setdefault(current, [])
            continue
        match = _HUNK_RE.match(line)
        if match and current is not None:
            start = int(match.group(1))
            count = int(match.group(2) or 1)
            if count > 0:
                changed[current].append((start, start + count - 1))
    return {path: merge_ranges(tuple(items)) for path, items in changed.items() if items}


def _python_symbol_range(source: str, symbol: str) -> tuple[int, int] | None:
    """Line range of ``Foo.bar`` / ``bar`` in *source*, via ``ast``."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None

    wanted = tuple(part for part in symbol.split(".") if part)

    def walk(node: ast.AST, prefix: tuple[str, ...]) -> ast.AST | None:
        for child in ast.iter_child_nodes(node):
            if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            here = (*prefix, child.name)
            if here == wanted:
                return child
            if wanted[: len(here)] == here:
                # ``here`` is a proper prefix of the requested symbol
                # (``Foo`` for ``Foo.bar``) — look inside it.
                found = walk(child, here)
                if found is not None:
                    return found
        return None

    found = walk(tree, ())
    if found is None:
        return None
    end = getattr(found, "end_lineno", None) or getattr(found, "lineno", None)
    if end is None:
        return None
    return found.lineno, end


def _text_symbol_range(source: str, symbol: str) -> tuple[int, int] | None:
    """Last-resort anchor for non-Python files: the ``def``/``class`` named line.

    A dotted symbol matches by its last component (``Thing.run`` → ``run``);
    the range runs to the next definition at the same or a shallower indent.
    """
    wanted = symbol.rsplit(".", 1)[-1]
    lines = source.splitlines()
    start: int | None = None
    start_indent = 0
    for index, line in enumerate(lines):
        match = _ANYDEF_RE.match(line)
        if not match:
            continue
        if start is not None:
            indent = len(line) - len(line.lstrip())
            if indent <= start_indent:
                return start, index  # exclusive end → previous line
        if match.group(1) == wanted and start is None:
            start = index + 1
            start_indent = len(line) - len(line.lstrip())
    if start is not None:
        return start, len(lines)
    return None


def symbol_range(source: str, *, path: str, symbol: str) -> SymbolRange | None:
    """Locate *symbol* in *source*; ``None`` when it cannot be anchored.

    A symbol that cannot be located must **not** be treated as drifting: an
    unknown anchor is not evidence, it is a missing answer.
    """
    if not symbol.strip():
        return None
    found = _python_symbol_range(source, symbol.strip()) if path.endswith(".py") else None
    if found is None:
        found = _text_symbol_range(source, symbol.strip())
    if found is None:
        return None
    start, end = found
    return SymbolRange(path=path, symbol=symbol, start=start, end=max(start, end))


def changed_lines_from_diff(diff_text: str, file_path: str) -> Ranges:
    """Changed ranges for one path out of a whole-repo diff."""
    return symbol_ranges_from_diff(diff_text).get(file_path, ())


# ── Read-only git adapter ────────────────────────────────────────────

class GitDriftAdapter:
    """Answer "which lines changed at this symbol?" with read-only git calls.

    The default implementation shells out to the ``git`` CLI (§3.1 forbids
    pygit2/dulwich/GitPython).  It never writes: no fetch, no checkout, no
    index touch — only ``diff``, ``rev-parse`` and ``ls-tree``/``cat-file``
    reads against whatever the repository already holds.
    """

    def __init__(self, repo_path: str | Path, *, timeout: float = 30.0) -> None:
        self._path = Path(repo_path)
        self._timeout = float(timeout)

    @property
    def path(self) -> Path:
        return self._path

    def _git(self, *args: str) -> str:
        if shutil.which("git") is None:
            raise DriftError("the git CLI is not on PATH")
        try:
            proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
                ["git", "-C", str(self._path), *args],
                capture_output=True,
                text=True,
                timeout=self._timeout,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise DriftError(f"git {' '.join(args)} failed: {exc}") from exc
        if proc.returncode != 0:
            raise DriftError(
                f"git {' '.join(args)} exited {proc.returncode}: {proc.stderr.strip()[:400]}"
            )
        return proc.stdout

    def diff_text(self, base: str, head: str) -> str:
        """Unified diff ``base..head`` with no context, new-side line numbers."""
        return self._git("diff", "--no-color", "--no-ext-diff", "-U0", base, head)

    def file_at(self, rev: str, file_path: str) -> str | None:
        """Contents of *file_path* at *rev*; ``None`` when it is absent."""
        try:
            return self._git("show", f"{rev}:{file_path}")
        except DriftError:
            return None

    def changed_ranges(self, base: str, head: str) -> dict[str, Ranges]:
        return symbol_ranges_from_diff(self.diff_text(base, head))

    def drift_context(
        self,
        *,
        file_path: str,
        symbol: str,
        base: str,
        head: str,
    ) -> DriftContext:
        """Whether the symbol's own region moved between *base* and *head*."""
        changed = self.changed_ranges(base, head).get(file_path, ())
        if not changed:
            return DriftContext(changed=(), detail="no changed lines in this file")
        source = self.file_at(head, file_path) or self.file_at(base, file_path) or ""
        anchor = symbol_range(source, path=file_path, symbol=symbol)
        if anchor is None:
            return DriftContext(
                symbol=None,
                changed=changed,
                detail="symbol could not be anchored — not treated as drift",
            )
        return DriftContext(
            symbol=anchor,
            changed=changed,
            detail=(
                f"symbol {file_path}:{anchor.start}-{anchor.end} vs changed "
                f"{len(changed)} hunk(s)"
            ),
        )


# ── §6.2 activation ──────────────────────────────────────────────────

@dataclass(frozen=True)
class ReactivationContext:
    """Inputs for the three activation conditions.

    * ``today`` exists so the gate can move the clock without mocking.
    * ``drift``/``drift_adapter`` are alternatives: pass a prepared
      :class:`DriftContext` (tests, gate) or let the adapter answer.
    * ``stop_reason`` marks a human override (``stop_reactivating``) that
      silences conditions 1 and 2 — condition 3 still wins, because an
      escalation is a statement about the codebase, not about this row.
    """

    today: date | None = None
    drift: DriftContext | None = None
    drift_adapter: Any | None = None
    base: str | None = None
    head: str | None = None
    rule_open_count: int | None = None
    rule_threshold: int | None = None
    previous_severity: str | None = None
    current_severity: str | None = None
    stop_reason: str | None = None


def _severity_raised(previous: str | None, current: str | None) -> bool:
    if not previous or not current:
        return False
    return _SEVERITY_RANK.get(str(current), -1) > _SEVERITY_RANK.get(str(previous), -1)


def _drift_reason(finding: Any, ctx: ReactivationContext) -> str | None:
    """Condition 1, or ``None`` when it does not hold.

    Raises :class:`DriftError` when the probe was *asked* for and could not
    answer — silently treating an unanswerable probe as "no drift" is how a
    finding goes quiet forever.
    """
    if ctx.stop_reason:
        return None

    drift = ctx.drift
    if drift is None and ctx.drift_adapter is not None and ctx.base and ctx.head:
        drift = ctx.drift_adapter.drift_context(
            file_path=finding.file_path,
            symbol=finding.symbol,
            base=ctx.base,
            head=ctx.head,
        )
    if drift is None:
        return None

    anchor = drift.symbol
    if anchor is not None and anchor.overlaps(drift.changed):
        return REASON_DRIFT
    if anchor is None and drift.changed:
        pass
    return None


def should_reactivate(finding: Any, ctx: ReactivationContext) -> str | None:
    """The §6.2 conditions, in priority order; ``None`` means "stay quiet".

    Only three reasons exist:

    1. ``code_drift`` — the changed line ranges intersect the symbol's range.
    2. ``due`` — ``finding.due <= today``.
    3. ``escalation`` — the rule's open+acknowledged+wontfix count exceeds the
       policy threshold, or the severity was re-judged higher.
    """
    if _severity_raised(ctx.previous_severity, ctx.current_severity):
        return REASON_ESCALATION
    if (
        ctx.rule_threshold is not None
        and ctx.rule_open_count is not None
        and ctx.rule_open_count > ctx.rule_threshold
    ):
        return REASON_ESCALATION
    if _drift_reason(finding, ctx) is not None:
        return REASON_DRIFT
    if not ctx.stop_reason and getattr(finding, "due", None) is not None:
        today = ctx.today or datetime.now(timezone.utc).date()
        if finding.due <= today:
            return REASON_DUE
    return None


# ── Event log ────────────────────────────────────────────────────────

def _record_event(
    session: Any,
    finding: Any,
    *,
    actor: str,
    from_status: str | None,
    to_status: str,
    reason: str | None,
    run_id: int | None,
    at: datetime | None = None,
) -> Any:
    event = _models().FindingEvent(
        finding_id=finding.id,
        at=at or datetime.now(timezone.utc),
        actor=actor,
        from_status=from_status,
        to_status=to_status,
        reason=reason,
        run_id=run_id,
    )
    session.add(event)
    return event


# ── §6.1 decision table ──────────────────────────────────────────────

def _require_debt_fields(action: str, owner: str | None, due: date | None) -> None:
    """I2 — ``wontfix`` / ``acknowledge`` without owner+due is refused."""
    missing = []
    if not (owner or "").strip():
        missing.append("owner")
    if due is None:
        missing.append("due")
    if missing:
        raise DecisionInvalidError(
            f"action {action} requires {' and '.join(missing)} (invariant I2)",
            code="finding_decision_invalid",
        )


def decide(
    finding_id: int,
    action: str,
    actor: str,
    owner: str | None = None,
    due: date | None = None,
    reason: str | None = None,
    *,
    session: Any,
    run_id: int | None = None,
    confirmed_by: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Apply one transition from the §6.1 table, or refuse it.

    Enforced invariants:

    * **I2** — ``wontfix`` and ``acknowledge`` both need ``owner`` *and* ``due``.
    * **I3** — a ``blocking`` finding accepts neither; only ``fixed`` (a real
      fix) or ``fixed`` with ``reason="false_positive"`` (the §6.3 rule-review
      route).

    ``false_positive`` is not a status: it is the reason on a ``fixed``
    transition, and §6.3 requires a second person to confirm it.
    """
    models = _models()
    finding = session.get(models.Finding, int(finding_id))
    if finding is None:
        raise FindingNotFoundError(int(finding_id))

    action = str(action or "").strip().lower()
    if action not in DECISION_ACTIONS:
        raise DecisionInvalidError(
            f"unknown action {action}; expected one of "
            f"{', '.join(DECISION_ACTIONS)}",
            code="finding_decision_invalid",
        )
    actor = str(actor or "").strip() or "unknown"
    from_status = str(finding.status)

    if action in DEFER_ACTIONS:
        if str(finding.level) == LEVEL_BLOCKING:
            raise DecisionInvalidError(
                f"a blocking finding cannot be {action} (invariant I3); "
                "fix it, or record a confirmed false positive",
                code="finding_decision_invalid",
            )
        _require_debt_fields(action, owner, due)
        if from_status == STATUS_FIXED:
            raise DecisionInvalidError(
                "a fixed finding cannot go back to a deferred state; open it "
                "again through the review run that still matches it",
                code="finding_decision_invalid",
            )
        to_status = DEFER_ACTIONS[action]
        event_reason = reason or (
            REASON_WONTFIX if to_status == STATUS_WONTFIX else REASON_ACCEPTED
        )
    elif action in (STATUS_FIXED, "fix"):
        to_status = STATUS_FIXED
        event_reason = reason or REASON_ACCEPTED
        if event_reason == REASON_FALSE_POSITIVE:
            # §6.3: rejecting a rule hit needs a second person, distinct from
            # the one who proposed it.
            if not confirmed_by:
                raise DecisionInvalidError(
                    "a false positive needs a second confirmation "
                    "(finding:decide by someone other than the proposer)",
                    code="finding_decision_invalid",
                )
            if str(confirmed_by).strip() == actor:
                raise DecisionInvalidError(
                    "the second confirmation must come from another account",
                    code="finding_decision_invalid",
                )
    else:  # pragma: no cover - DECISION_ACTIONS screens every other spelling
        raise DecisionInvalidError(f"unknown action {action}")

    if to_status in (STATUS_ACKNOWLEDGED, STATUS_WONTFIX):
        finding.owner = (owner or "").strip()
        finding.due = due

    finding.status = to_status
    finding.decided_by = actor
    finding.decided_at = now or datetime.now(timezone.utc)
    _record_event(
        session,
        finding,
        actor=actor,
        from_status=from_status,
        to_status=to_status,
        reason=event_reason,
        run_id=run_id,
        at=now,
    )
    session.flush()
    return {
        "finding": serialize(finding),
        "from_status": from_status,
        "to_status": to_status,
        "reason": event_reason,
    }


# ── Serialization ────────────────────────────────────────────────────

def serialize(finding: Any) -> dict[str, Any]:
    """The JSON shape the SPA reads.  Stable key set, ISO timestamps."""
    return {
        "id": finding.id,
        "repo_id": finding.repo_id,
        "fingerprint": finding.fingerprint,
        "rule_id": finding.rule_id,
        "level": finding.level,
        "severity": finding.severity,
        "status": finding.status,
        "file_path": finding.file_path,
        "symbol": finding.symbol,
        "line_hint": finding.line_hint,
        "title": finding.title,
        "detail": finding.detail,
        "first_seen_run_id": finding.first_seen_run_id,
        "last_seen_run_id": finding.last_seen_run_id,
        "seen_count": finding.seen_count,
        "owner": finding.owner,
        "due": finding.due.isoformat() if finding.due else None,
        "decided_by": finding.decided_by,
        "decided_at": finding.decided_at.isoformat() if finding.decided_at else None,
        "pr_url": finding.pr_url,
        "created_at": finding.created_at.isoformat() if finding.created_at else None,
        "updated_at": finding.updated_at.isoformat() if finding.updated_at else None,
    }


def serialize_event(event: Any) -> dict[str, Any]:
    return {
        "id": event.id,
        "finding_id": event.finding_id,
        "at": event.at.isoformat() if event.at else None,
        "actor": event.actor,
        "from_status": event.from_status,
        "to_status": event.to_status,
        "reason": event.reason,
        "run_id": event.run_id,
    }


# ── Ingest (§4.4 dedup + §6.2 reactivation) ──────────────────────────

def _text(value: Any, fallback: str = "") -> str:
    if value is None:
        return fallback
    return str(value)


def coerce_due(value: Any) -> date | None:
    """Parse an ISO date (or accept a ``date``/``datetime``) for I2's ``due``."""
    if value is None or value == "":
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    try:
        return date.fromisoformat(str(value).strip()[:10])
    except ValueError as exc:
        # No ``got {value}``: this string is handed to ``BadRequestError`` and
        # therefore travels back to the caller.  Echoing the rejected input would
        # put whatever arrived on the wire (it is not necessarily a date) into the
        # response body; a fixed template says everything the client needs.
        raise ValueError("due must be an ISO date (YYYY-MM-DD)") from exc


def _normalize_level(value: Any) -> str:
    level = _text(value, LEVEL_DEBT).strip().lower()
    if level not in FINDING_LEVEL:
        raise ValueError(f"level must be one of {FINDING_LEVEL}, got {value}")
    return level


def _normalize_severity(value: Any) -> str:
    severity = _text(value, "medium").strip().lower()
    return severity if severity in _SEVERITY_RANK else "medium"


def _resolve_repo_id(session: Any, repo_id: int | str) -> int:
    """Accept a numeric id or a ``<owner>/<name>`` slug."""
    if isinstance(repo_id, int) or (isinstance(repo_id, str) and repo_id.isdigit()):
        return int(repo_id)
    models = _models()
    repo = getattr(models, "Repo", None)
    if repo is None:
        raise FindingsError(f"cannot resolve repo slug {repo_id}: models.Repo is missing")
    row = session.query(repo).filter(repo.slug == str(repo_id)).one_or_none()
    if row is None:
        raise FindingsError(f"no repo with slug {repo_id}")
    return int(row.id)


#: Columns that a runner may send that are *evidence*, never identity.
_EVIDENCE_KINDS = ("issue", "pull_request", "commit", "pr")


def _evidence_rows(entry: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    raw = entry.get("evidence") or []
    if not isinstance(raw, (list, tuple)):
        return []
    return [item for item in raw if isinstance(item, Mapping)]


def ingest(
    repo_id: int | str,
    run_id: int | None,
    findings: Iterable[Mapping[str, Any]],
    *,
    session: Any,
    drift_adapter: Any | None = None,
    base: str | None = None,
    head: str | None = None,
    drift: DriftContext | None = None,
    today: date | None = None,
    run_context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Fold one run's findings into the table; return what happened.

    Shape accepted (the §9.5 ``findings[]`` entries, already validated by S4)::

        {"rule_id": …, "level": "blocking"|"debt", "severity": …,
         "file_path": …, "symbol": …, "line_hint": 42,
         "title": …, "detail": …, "context_key": "",
         "evidence": [{"kind": "issue", "number": 1234,
                       "relation": "mentions"}]}

    Returns ``{"new": n, "matched": n, "reactivated": n, "finding_ids": […]}}``.
    An existing fingerprint is advanced — ``last_seen_run_id``, ``seen_count``,
    status kept — and reactivated only under §6.2.  A new fingerprint opens at
    ``open`` with a ``from=None`` event.
    """
    models = _models()
    numeric_repo = _resolve_repo_id(session, repo_id)
    entries = [entry for entry in findings if isinstance(entry, Mapping)]

    run = None
    if run_id is not None:
        run = session.get(models.ReviewRun, int(run_id))
        if run is None:
            # I5 lives on this: every agent-produced finding must be traceable
            # to the run that produced it.  A dangling id would silently break
            # that, so it is refused rather than written.
            raise FindingsError(
                f"review_run {run_id} does not exist; create the ReviewRun before "
                "ingesting its findings"
            )

    policy = (run_context or {}).get("policy")
    matched = 0
    created = 0
    reactivated = 0
    seen_ids: list[int] = []

    for entry in entries:
        rule_id = _text(entry.get("rule_id")).strip()
        file_path = _text(entry.get("file_path")).strip()
        symbol = _text(entry.get("symbol")).strip()
        if not rule_id or not file_path or not symbol:
            raise FindingsError(
                "a finding needs rule_id, file_path and symbol — "
                f"got rule_id={rule_id} file_path={file_path} symbol={symbol}"
            )
        context_key = _text(entry.get("context_key"))
        digest = fingerprint(rule_id, file_path, symbol, context_key)

        existing = (
            session.query(models.Finding)
            .filter(
                models.Finding.repo_id == numeric_repo,
                models.Finding.fingerprint == digest,
            )
            .one_or_none()
        )

        if existing is None:
            level = _normalize_level(entry.get("level"))
            finding = models.Finding(
                repo_id=numeric_repo,
                fingerprint=digest,
                rule_id=rule_id,
                level=level,
                severity=_normalize_severity(entry.get("severity")),
                status=STATUS_OPEN,
                file_path=file_path,
                symbol=symbol,
                line_hint=entry.get("line_hint"),
                title=_text(entry.get("title"), rule_id),
                detail=_text(entry.get("detail")),
                first_seen_run_id=run_id,
                last_seen_run_id=run_id,
                seen_count=1,
            )
            session.add(finding)
            session.flush()
            _record_event(
                session,
                finding,
                actor="agent",
                from_status=None,
                to_status=STATUS_OPEN,
                reason=REASON_MATCHED,
                run_id=run_id,
            )
            _attach_evidence(session, finding, _evidence_rows(entry))
            created += 1
            seen_ids.append(int(finding.id))
            continue

        # Dedup hit: identity is untouched (I1), the *observation* advances.
        matched += 1
        previous_status = str(existing.status)
        existing.last_seen_run_id = run_id
        existing.seen_count = int(existing.seen_count or 0) + 1
        existing.line_hint = entry.get("line_hint", existing.line_hint)
        if entry.get("title"):
            existing.title = _text(entry.get("title"))
        if entry.get("detail"):
            existing.detail = _text(entry.get("detail"))
        if previous_status == STATUS_STALE:
            # A stale finding that the rule reports again is live evidence.
            existing.status = STATUS_OPEN
            _record_event(
                session,
                existing,
                actor="agent",
                from_status=STATUS_STALE,
                to_status=STATUS_OPEN,
                reason=REASON_DRIFT,
                run_id=run_id,
            )
            reactivated += 1
        elif previous_status in (STATUS_ACKNOWLEDGED, STATUS_WONTFIX):
            context = _reactivation_context(
                session,
                existing,
                policy=policy,
                drift_adapter=drift_adapter,
                base=base,
                head=head,
                drift=drift,
                today=today,
            )
            reason = should_reactivate(existing, context)
            if reason is not None:
                existing.status = STATUS_OPEN
                existing.owner = None
                existing.due = None
                _record_event(
                    session,
                    existing,
                    actor="system",
                    from_status=previous_status,
                    to_status=STATUS_OPEN,
                    reason=reason,
                    run_id=run_id,
                )
                reactivated += 1
        seen_ids.append(int(existing.id))
        session.flush()

    escalated = _apply_escalation(
        session,
        numeric_repo,
        policy=policy,
        run_id=run_id,
        exclude=seen_ids,
    )
    reactivated += escalated

    if run is not None:
        run.findings_new = int(run.findings_new or 0) + created
        run.findings_matched = int(run.findings_matched or 0) + matched
        session.flush()

    return {
        "new": created,
        "matched": matched,
        "reactivated": reactivated,
        "finding_ids": seen_ids,
    }


def _attach_evidence(session: Any, finding: Any, rows: Sequence[Mapping[str, Any]]) -> int:
    """Persist §4.6 evidence links, skipping anything already linked.

    Only the durable ``issue``/``pull_request`` relations are stored here; a
    free-text suggestion is S4's job to validate before it reaches this point.
    """
    models = _models()
    evidence_model = getattr(models, "FindingEvidence", None)
    if evidence_model is None or not rows:
        return 0
    attached = 0
    existing_numbers = {
        (getattr(item, "repo_issue_id", None), getattr(item, "relation", None))
        for item in session.query(evidence_model).filter(
            evidence_model.finding_id == finding.id
        )
    }
    for row in rows:
        relation = _text(row.get("relation"), "mentions")
        if relation not in ("mentions", "duplicate_of", "fixed_by"):
            relation = "mentions"
        issue_id = row.get("repo_issue_id")
        if issue_id is None:
            # An issue *number* still has to be resolved to a row; S2 owns that
            # lookup, so an unresolved number is kept out of the link table
            # rather than guessed at.
            continue
        key = (issue_id, relation)
        if key in existing_numbers:
            continue
        session.add(
            evidence_model(
                finding_id=finding.id,
                repo_issue_id=issue_id,
                relation=relation,
            )
        )
        existing_numbers.add(key)
        attached += 1
    session.flush()
    return attached


def _apply_escalation(
    session: Any,
    repo_id: int,
    *,
    policy: Any | None,
    run_id: int | None,
    exclude: Sequence[int] = (),
) -> int:
    """§6.2 condition 3 as a rule-level sweep, once per run.

    Escalation is a statement about the *rule* ("this repository has more than
    N deferrals of it"), not about one row, so it cannot be tested only on the
    rows a run happened to re-report — the run that pushes the count over the
    threshold may be reporting a different symbol entirely.  After the batch is
    folded in, every still-deferred finding of a rule that is now over threshold
    is opened, with the reason logged on its own event stream.
    """
    models = _models()
    escalation = getattr(policy, "escalation", None)
    threshold = getattr(escalation, "rule_count_threshold", None)
    if threshold is None:
        return 0

    deferred = (
        session.query(models.Finding)
        .filter(
            models.Finding.repo_id == repo_id,
            models.Finding.status.in_((STATUS_ACKNOWLEDGED, STATUS_WONTFIX)),
        )
        .all()
    )
    counts: dict[str, int] = {}
    for row in deferred:
        counts[str(row.rule_id)] = counts.get(str(row.rule_id), 0) + 1

    reopened = 0
    for row in deferred:
        if int(row.id) in exclude:
            continue  # already handled (and counted) in the per-entry pass
        if counts.get(str(row.rule_id), 0) <= int(threshold):
            continue
        previous = str(row.status)
        row.status = STATUS_OPEN
        row.owner = None
        row.due = None
        _record_event(
            session,
            row,
            actor="system",
            from_status=previous,
            to_status=STATUS_OPEN,
            reason=REASON_ESCALATION,
            run_id=run_id,
        )
        reopened += 1
    session.flush()
    return reopened


def _reactivation_context(
    session: Any,
    finding: Any,
    *,
    policy: Any | None,
    drift_adapter: Any | None,
    base: str | None,
    head: str | None,
    drift: DriftContext | None = None,
    today: date | None = None,
) -> ReactivationContext:
    models = _models()
    threshold = None
    if policy is not None:
        escalation = getattr(policy, "escalation", None)
        threshold = getattr(escalation, "rule_count_threshold", None)
    open_count = (
        session.query(models.Finding)
        .filter(
            models.Finding.repo_id == finding.repo_id,
            models.Finding.rule_id == finding.rule_id,
            models.Finding.status.in_((STATUS_OPEN, STATUS_ACKNOWLEDGED, STATUS_WONTFIX)),
        )
        .count()
    )
    return ReactivationContext(
        today=today,
        drift=drift,
        drift_adapter=drift_adapter,
        base=base,
        head=head,
        rule_open_count=open_count,
        rule_threshold=threshold,
        previous_severity=None,
        current_severity=None,
        stop_reason=getattr(finding, "stop_reason", None),
    )


# ── §6.3 rule governance ─────────────────────────────────────────────

def _false_positive_counts(
    session: Any,
    repo_id: int,
    *,
    since: datetime,
) -> dict[str, int]:
    """``rule_id -> confirmed false positives`` in the window, from the events.

    The event log is the ledger: a ``false_positive`` reason is written once,
    by :func:`decide`, and never rewritten, so counting events cannot
    double-count a finding that was re-decided.
    """
    models = _models()
    rows = (
        session.query(models.Finding.rule_id, models.FindingEvent.id)
        .join(models.FindingEvent, models.FindingEvent.finding_id == models.Finding.id)
        .filter(
            models.Finding.repo_id == repo_id,
            models.FindingEvent.reason == REASON_FALSE_POSITIVE,
            models.FindingEvent.at >= since,
        )
        .all()
    )
    counts: dict[str, int] = {}
    for rule_id, _event_id in rows:
        counts[str(rule_id)] = counts.get(str(rule_id), 0) + 1
    return counts


def rule_quality_watch(
    repo_id: int | str,
    *,
    session: Any,
    policy: Any,
    actor: str = "system",
    now: datetime | None = None,
    window_days: int = RULE_QUALITY_WINDOW_DAYS,
) -> dict[str, Any]:
    """§6.3 — turn repeated false positives into one governance finding.

    ``escalation.rule_noise_threshold`` false positives for the same ``rule_id``
    inside the window earn a finding with ``rule_id = "meta.rule-quality"``
    whose ``context_key`` is the offending rule, so the fingerprint is stable
    and re-running this function updates the same row instead of stacking new
    ones.  It points at ``.agent/review-policy.yml`` because that is where the
    rule's entry — the thing that has to change — lives.
    """
    models = _models()
    numeric_repo = _resolve_repo_id(session, repo_id)
    moment = now or datetime.now(timezone.utc)
    since = moment - timedelta(days=int(window_days))

    escalation = getattr(policy, "escalation", None)
    threshold = int(
        getattr(escalation, "rule_noise_threshold", None)
        or getattr(policy, "rule_noise_threshold", 0)
        or 0
    )
    counts = _false_positive_counts(session, numeric_repo, since=since)
    noisy = sorted(
        (rule_id, count) for rule_id, count in counts.items()
        if rule_id != RULE_QUALITY_RULE_ID and threshold and count >= threshold
    )

    created: list[int] = []
    updated: list[int] = []
    for rule_id, count in noisy:
        digest = fingerprint(
            RULE_QUALITY_RULE_ID,
            POLICY_ENTRY_PATH,
            RULE_QUALITY_SYMBOL,
            context_key=rule_id,
        )
        finding = (
            session.query(models.Finding)
            .filter(
                models.Finding.repo_id == numeric_repo,
                models.Finding.fingerprint == digest,
            )
            .one_or_none()
        )
        detail = (
            f"{count} confirmed false positive(s) for rule {rule_id} in the last "
            f"{int(window_days)} days (threshold {threshold}); the rule needs fixing "
            "or demoting to debt."
        )
        if finding is None:
            finding = models.Finding(
                repo_id=numeric_repo,
                fingerprint=digest,
                rule_id=RULE_QUALITY_RULE_ID,
                level=LEVEL_DEBT,
                severity="medium",
                status=STATUS_OPEN,
                file_path=POLICY_ENTRY_PATH,
                symbol=RULE_QUALITY_SYMBOL,
                line_hint=None,
                title=f"Rule {rule_id} is noisy — review or demote it",
                detail=detail,
                first_seen_run_id=None,
                last_seen_run_id=None,
                seen_count=1,
            )
            session.add(finding)
            session.flush()
            _record_event(
                session,
                finding,
                actor=actor,
                from_status=None,
                to_status=STATUS_OPEN,
                reason=REASON_FALSE_POSITIVE,
                run_id=None,
                at=moment,
            )
            created.append(int(finding.id))
        else:
            finding.detail = detail
            finding.seen_count = int(finding.seen_count or 1) + 1
            if str(finding.status) in (STATUS_FIXED,):
                finding.status = STATUS_OPEN
                _record_event(
                    session,
                    finding,
                    actor=actor,
                    from_status=STATUS_FIXED,
                    to_status=STATUS_OPEN,
                    reason=REASON_ESCALATION,
                    run_id=None,
                    at=moment,
                )
            updated.append(int(finding.id))
        session.flush()

    return {
        "threshold": threshold,
        "window_days": int(window_days),
        "counts": counts,
        "noisy": [rule_id for rule_id, _ in noisy],
        "created": created,
        "updated": updated,
    }


# ── §6.4 autofix boundary ────────────────────────────────────────────

def autofix_allowed(rule_id: str, policy: Any) -> bool:
    """Whether policy lets the agent repair *rule_id* without a human.

    Default **false**: §6.4 puts whole categories (service splits, cache
    strategy, permission model, protocol semantics, ``model_routes``)
    permanently out of reach, and the policy file is where a rule opts in.
    """
    if policy is None:
        return False
    entry = None
    finder = getattr(policy, "rule", None)
    if callable(finder):
        entry = finder(rule_id)
    if entry is None:
        return False
    if isinstance(entry, Mapping):
        return bool(entry.get("autofix", False))
    return bool(getattr(entry, "autofix", False))


# ── Queries used by the routes ───────────────────────────────────────

def list_findings(
    session: Any,
    *,
    repo: int | str | None = None,
    status: str | None = None,
    level: str | None = None,
    rule: str | None = None,
    owner: str | None = None,
    limit: int = 200,
    offset: int = 0,
) -> dict[str, Any]:
    """Filtered finding list, newest first.  Returns ``{"items", "total"}``."""
    models = _models()
    query = session.query(models.Finding)
    if repo:
        query = query.filter(models.Finding.repo_id == _resolve_repo_id(session, repo))
    if status:
        query = query.filter(models.Finding.status == str(status))
    if level:
        query = query.filter(models.Finding.level == str(level))
    if rule:
        query = query.filter(models.Finding.rule_id == str(rule))
    if owner:
        query = query.filter(models.Finding.owner == str(owner))
    total = query.count()
    rows = (
        query.order_by(models.Finding.id.desc())
        .limit(max(1, min(int(limit), 1000)))
        .offset(max(0, int(offset)))
        .all()
    )
    return {"items": [serialize(row) for row in rows], "total": total}


def get_finding(session: Any, finding_id: int | str) -> Any:
    """One finding row, or :class:`FindingNotFoundError`."""
    models = _models()
    row = session.get(models.Finding, int(finding_id))
    if row is None:
        raise FindingNotFoundError(int(finding_id))
    return row


def events_for(session: Any, finding_id: int | str) -> list[dict[str, Any]]:
    """The state-machine history of one finding, oldest first."""
    models = _models()
    rows = (
        session.query(models.FindingEvent)
        .filter(models.FindingEvent.finding_id == int(finding_id))
        .order_by(models.FindingEvent.id.asc())
        .all()
    )
    return [serialize_event(row) for row in rows]


__all__ = [
    "DECISION_ACTIONS",
    "DriftContext",
    "DriftError",
    "DecisionInvalidError",
    "FINDING_LEVEL",
    "FINDING_STATUS",
    "FindingNotFoundError",
    "FindingsError",
    "FindingsModelError",
    "GitDriftAdapter",
    "LEVEL_BLOCKING",
    "LEVEL_DEBT",
    "POLICY_ENTRY_PATH",
    "PolicyUnavailableError",
    "REASON_ACCEPTED",
    "REASON_DRIFT",
    "REASON_DUE",
    "REASON_ESCALATION",
    "REASON_FALSE_POSITIVE",
    "REASON_MATCHED",
    "REASON_WONTFIX",
    "RULE_QUALITY_RULE_ID",
    "RULE_QUALITY_SYMBOL",
    "RULE_QUALITY_WINDOW_DAYS",
    "ReactivationContext",
    "STATUS_ACKNOWLEDGED",
    "STATUS_FIXED",
    "STATUS_OPEN",
    "STATUS_STALE",
    "STATUS_WONTFIX",
    "SymbolRange",
    "autofix_allowed",
    "changed_lines_from_diff",
    "coerce_due",
    "decide",
    "events_for",
    "fingerprint",
    "fingerprint_input",
    "get_finding",
    "ingest",
    "list_findings",
    "merge_ranges",
    "ranges_intersect",
    "rule_quality_watch",
    "serialize",
    "serialize_event",
    "should_reactivate",
    "symbol_range",
    "symbol_ranges_from_diff",
]
