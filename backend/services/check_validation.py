"""Falsifiability validation and per-check run history (P2.2).

A check earns the right to gate by *demonstrating it can fail*: it must pass on
the current revision **and** fail on at least one known-bad revision.  A check
that has never been observed failing is indistinguishable from ``exit 0``, and
letting it gate is how "green" becomes meaningless.  This module is that
validator, plus the run history that makes a never-failing or flaky check
detectable.

The known-bad revisions come from a small, **pluggable fault-seeding harness**:
for a language the repository declares, :func:`default_seeders` supplies seeds
that flip a comparison, remove a guard or delete a return.  Seeding never
touches the real checkout — every seed is applied to a temporary copy
(``shutil.copytree``, injectable as *copier*), the check runs there, and the copy
is discarded.

Everything external is injectable (the executor, the seeders, the copier), so
the whole validator is exercised offline by ``scripts/check_agent_checks.py``
with no model, no network and no git.
"""

from __future__ import annotations

import logging
import re
import shutil
import tempfile
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from services.format import utc_now_iso
from services.gates import (
    DEFAULT_GATE_TIMEOUT,
    SOURCE_EXPLICIT,
    STATUS_FAILED,
    STATUS_PASSED,
    STATUS_UNVERIFIED,
    VALIDATION_UNVALIDATED,
    VALIDATION_VALIDATED,
    CheckCommand,
    CheckSuite,
    CommandExecutor,
    GateResult,
    run_suite,
)

logger = logging.getLogger("cpypiserver.check_validation")

#: Directories a seeded copy never needs and must not pay to copy.
COPY_IGNORE: tuple[str, ...] = (
    ".git", ".hg", ".svn", "__pycache__", ".venv", "venv", "node_modules",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", "dist", "build", "target",
)

#: Source trees a seeder looks in, in priority order.  Bounded on purpose: the
#: harness is a falsifiability probe, not an exhaustive mutation tester.
SOURCE_DIRS: tuple[str, ...] = ("src", "app", "lib", "backend", "pkg", "cmd", "internal")

#: Files a seeder never mutates (a fault in a test proves nothing).
_TEST_HINTS: tuple[str, ...] = ("test_", "_test.", "/tests/", "/test/", ".test.", ".spec.")

#: How many source files one seeder inspects before it stops looking.
MAX_FILES_PER_SEEDER = 40

#: How many seeds the validator tries before giving up on a check.
DEFAULT_MAX_SEEDS = 6


# ── Seeds ────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class SeedEdit:
    """One literal replacement inside the seeded copy."""

    path: str
    old: str
    new: str


@dataclass(frozen=True)
class FaultSeed:
    """A known-bad revision, described as concrete edits.

    ``detected`` is not part of the seed: whether a check catches it is exactly
    what the validator measures.
    """

    id: str
    language: str
    description: str
    edits: tuple[SeedEdit, ...] = field(default=())


Seeder = Callable[[Path], list[FaultSeed]]

_COMPARISONS: tuple[tuple[str, str], ...] = (
    ("==", "!="), ("!=", "=="), ("<=", ">"), (">=", "<"), ("<", ">="), (">", "<="),
)
_JS_COMPARISONS: tuple[tuple[str, str], ...] = (
    ("===", "!=="), ("!==", "==="), ("==", "!="), ("!=", "=="), ("<=", ">"), (">=", "<"),
)
_IF_RE = re.compile(r"^(?P<indent>[ \t]*)if\s+(?P<cond>[^:]+):(?P<tail>\s*(?:#.*)?)$")
_RETURN_RE = re.compile(r"^(?P<indent>[ \t]*)return\b(?P<rest>[^\n]*)$")
_JS_IF_RE = re.compile(r"^(?P<indent>[ \t]*)if\s*\((?P<cond>[^)]*)\)\s*\{(?P<tail>\s*)$")
_JS_RETURN_RE = re.compile(r"^(?P<indent>[ \t]*)return\b(?P<rest>[^\n]*);?\s*$")


def _is_test_path(path: str) -> bool:
    lowered = path.replace("\\", "/").lower()
    return any(hint in lowered for hint in _TEST_HINTS)


def _source_files(root: Path, suffixes: Sequence[str]) -> list[Path]:
    """Candidate source files, bounded and deterministic (sorted)."""
    found: list[Path] = []
    for directory in SOURCE_DIRS:
        base = root / directory
        if not base.is_dir():
            continue
        for suffix in suffixes:
            for path in sorted(base.rglob(f"*{suffix}")):
                rel = path.relative_to(root).as_posix()
                if _is_test_path(rel) or path in found:
                    continue
                found.append(path)
                if len(found) >= MAX_FILES_PER_SEEDER:
                    return found
    if found:
        return found
    for suffix in suffixes:
        for path in sorted(root.glob(f"*{suffix}")):
            rel = path.name
            if not _is_test_path(rel):
                found.append(path)
            if len(found) >= MAX_FILES_PER_SEEDER:
                return found
    return found


def _flip_comparison_seed(root: Path, path: Path, comparisons: Sequence[tuple[str, str]],
                          *, language: str) -> FaultSeed | None:
    """The first comparison in *path*, with its operator swapped."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    for old_op, new_op in comparisons:
        index = text.find(old_op)
        if index < 0:
            continue
        rel = path.relative_to(root).as_posix()
        context = text[max(0, index - 20): index + len(old_op) + 20]
        return FaultSeed(
            id=f"{rel}:flip-{old_op}",
            language=language,
            description=f"把 {rel} 里的 {old_op} 翻转成 {new_op}（上下文：{context.strip()}）",
            edits=(SeedEdit(rel, text[index:index + len(old_op)], new_op),),
        )
    return None


def _python_faults(root: Path) -> list[FaultSeed]:
    seeds: list[FaultSeed] = []
    for path in _source_files(root, (".py",)):
        flip = _flip_comparison_seed(root, path, _COMPARISONS, language="python")
        if flip is not None:
            seeds.append(flip)
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        rel = path.relative_to(root).as_posix()
        for line in text.splitlines():
            match = _IF_RE.match(line)
            if match is not None:
                seeds.append(FaultSeed(
                    id=f"{rel}:remove-guard",
                    language="python",
                    description=f"把 {rel} 的守卫 {line.strip()} 永假化",
                    edits=(SeedEdit(rel, line, f"{match.group('indent')}if False:  # fault seed"),),
                ))
                break
        for line in text.splitlines():
            match = _RETURN_RE.match(line)
            if match is not None:
                seeds.append(FaultSeed(
                    id=f"{rel}:delete-return",
                    language="python",
                    description=f"删掉 {rel} 的 {line.strip()}",
                    edits=(SeedEdit(rel, line, f"{match.group('indent')}pass  # fault seed"),),
                ))
                break
    return seeds


def _javascript_faults(root: Path) -> list[FaultSeed]:
    seeds: list[FaultSeed] = []
    for path in _source_files(root, (".js", ".ts", ".jsx", ".tsx")):
        flip = _flip_comparison_seed(root, path, _JS_COMPARISONS, language="javascript")
        if flip is not None:
            seeds.append(flip)
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        rel = path.relative_to(root).as_posix()
        for line in text.splitlines():
            match = _JS_IF_RE.match(line)
            if match is not None:
                seeds.append(FaultSeed(
                    id=f"{rel}:remove-guard",
                    language="javascript",
                    description=f"把 {rel} 的守卫 {line.strip()} 永假化",
                    edits=(SeedEdit(rel, line, f"{match.group('indent')}if (false) {{"),),
                ))
                break
    return seeds


def _go_faults(root: Path) -> list[FaultSeed]:
    seeds: list[FaultSeed] = []
    for path in _source_files(root, (".go",)):
        flip = _flip_comparison_seed(root, path, _COMPARISONS, language="go")
        if flip is not None:
            seeds.append(flip)
    return seeds


def default_seeders() -> tuple[Seeder, ...]:
    """The built-in seeders, one per language the harness can mutate.

    A repository that declares no language the harness understands yields no
    seeds, so every check in it stays ``unvalidated`` — the safe direction.
    """
    return (_python_faults, _javascript_faults, _go_faults)


# ── Validation ───────────────────────────────────────────────────────

@dataclass(frozen=True)
class CheckValidation:
    """The verdict for one check, plus the evidence for it."""

    check_id: str
    status: str = VALIDATION_UNVALIDATED
    baseline_passed: bool = False
    faults_attempted: int = 0
    faults_detected: int = 0
    detected_by: tuple[str, ...] = ()
    reason: str = ""

    @property
    def validated(self) -> bool:
        return self.status == VALIDATION_VALIDATED

    @property
    def detection_rate(self) -> float:
        if not self.faults_attempted:
            return 0.0
        return self.faults_detected / self.faults_attempted

    def as_payload(self) -> dict[str, object]:
        return {
            "check_id": self.check_id,
            "status": self.status,
            "baseline_passed": self.baseline_passed,
            "faults_attempted": self.faults_attempted,
            "faults_detected": self.faults_detected,
            "detection_rate": self.detection_rate,
            "detected_by": list(self.detected_by),
            "reason": self.reason,
        }


def _apply_edits(root: Path, seed: FaultSeed) -> bool:
    """Apply a seed's edits in place; false when any anchor is missing."""
    for edit in seed.edits:
        path = root / edit.path
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return False
        if edit.old not in text:
            return False
        if edit.old == edit.new:
            return False
        path.write_text(text.replace(edit.old, edit.new, 1), encoding="utf-8")
    return True


def _default_copier(source: Path, destination: Path) -> None:
    shutil.copytree(
        source, destination,
        ignore=shutil.ignore_patterns(*COPY_IGNORE),
        symlinks=True,
    )


def validate_check(
    check: CheckCommand,
    *,
    repo_root: str | Path,
    executor: CommandExecutor | None = None,
    timeout: float = DEFAULT_GATE_TIMEOUT,
    seeders: Sequence[Seeder] | None = None,
    max_seeds: int = DEFAULT_MAX_SEEDS,
    copier: Callable[[Path, Path], None] | None = None,
) -> CheckValidation:
    """Decide whether *check* may gate, by trying to make it fail.

    * passes on the current revision, **and**
    * fails on at least one seeded known-bad copy

    → ``validated``.  Anything else → ``unvalidated`` (report only).  A check
    that already fails on the baseline is not "validated" either: it cannot
    distinguish a good revision from a bad one.
    """
    root = Path(repo_root)
    # The probe must report pass/fail, not "unverified": a check that is still
    # unvalidated is exactly what we are trying to measure, and ``run_suite``
    # marks non-gating checks unverified by design.  Measuring a *copy* of the
    # descriptor as gating changes nothing about the caller's check.
    probe = check.model_copy(update={"validation": VALIDATION_VALIDATED})
    single = CheckSuite(checks=[probe], source=SOURCE_EXPLICIT)
    baseline = run_suite(single, root=root, executor=executor, timeout=timeout)
    if not baseline.gates or baseline.gates[0].status != STATUS_PASSED:
        return CheckValidation(
            check_id=check.id,
            status=VALIDATION_UNVALIDATED,
            baseline_passed=False,
            reason="当前修订上基线未通过，无法证明它能区分好坏修订",
        )

    seeds: list[FaultSeed] = []
    for seeder in seeders if seeders is not None else default_seeders():
        try:
            seeds.extend(seeder(root))
        except Exception as exc:  # a broken seeder must not fail the check
            logger.warning("fault seeder %s failed: %s", getattr(seeder, "__name__", seeder), exc)
    attempted = 0
    detected: list[str] = []
    copy_fn = copier or _default_copier
    for seed in seeds[: max(0, int(max_seeds))]:
        with tempfile.TemporaryDirectory(prefix="openfish-seed-") as tmp:
            seeded = Path(tmp) / "repo"
            try:
                copy_fn(root, seeded)
            except OSError as exc:
                logger.warning("cannot copy repo for seed %s: %s", seed.id, exc)
                continue
            if not _apply_edits(seeded, seed):
                continue
            attempted += 1
            outcome = run_suite(single, root=seeded, executor=executor, timeout=timeout)
            state = outcome.gates[0].status if outcome.gates else STATUS_UNVERIFIED
            if state == STATUS_FAILED:
                detected.append(seed.id)
                break
    if detected:
        return CheckValidation(
            check_id=check.id,
            status=VALIDATION_VALIDATED,
            baseline_passed=True,
            faults_attempted=attempted,
            faults_detected=len(detected),
            detected_by=tuple(detected),
            reason=f"当前修订通过，且在已知坏修订 {detected[0]} 上失败",
        )
    return CheckValidation(
        check_id=check.id,
        status=VALIDATION_UNVALIDATED,
        baseline_passed=True,
        faults_attempted=attempted,
        faults_detected=0,
        reason=(
            "当前修订通过，但没有任何可播种的变异让它失败"
            + ("（尝试了 %d 个）" % attempted if attempted else "（没有可用变异）")
        ),
    )


# ── Run history ──────────────────────────────────────────────────────

@dataclass(frozen=True)
class CheckRunRecord:
    """One execution of one check — the raw material for flake detection."""

    check_id: str
    state: str
    suite_hash: str = ""
    commit_sha: str = ""
    exit_code: int | None = None
    duration_ms: int = 0
    at: str = ""
    validation: str = VALIDATION_UNVALIDATED
    #: Set when this run belongs to a change that loosened the check (a retry
    #: budget bump, a narrowed assertion).  ``require_never_weakened`` in the L3
    #: criteria refuses to promote a check with any such run on record.
    weakened: bool = False

    def as_payload(self) -> dict[str, object]:
        return {
            "check_id": self.check_id,
            "state": self.state,
            "suite_hash": self.suite_hash,
            "commit_sha": self.commit_sha,
            "exit_code": self.exit_code,
            "duration_ms": self.duration_ms,
            "at": self.at,
            "validation": self.validation,
            "weakened": self.weakened,
        }


@dataclass(frozen=True)
class CheckHistorySummary:
    """Aggregate history for one check, for the trust ladder and findings."""

    check_id: str
    runs: int
    failures: int
    unverified: int
    validation: str
    #: True when any recorded run belongs to a weakening change.
    weakened: bool = False

    @property
    def flake_rate(self) -> float:
        return (self.failures / self.runs) if self.runs else 0.0

    @property
    def never_failed(self) -> bool:
        """A check that has only ever passed is not evidence it can fail."""
        return self.runs > 0 and self.failures == 0 and self.unverified == 0

    def as_payload(self) -> dict[str, object]:
        return {
            "check_id": self.check_id,
            "runs": self.runs,
            "failures": self.failures,
            "unverified": self.unverified,
            "flake_rate": self.flake_rate,
            "never_failed": self.never_failed,
            "validation": self.validation,
            "weakened": self.weakened,
        }


def record_from_gate(
    result: GateResult,
    *,
    suite_hash: str = "",
    commit_sha: str = "",
    at: str | None = None,
    validation: str = VALIDATION_UNVALIDATED,
) -> CheckRunRecord:
    """Turn one gate result into a history record."""
    return CheckRunRecord(
        check_id=result.gate,
        state=result.status,
        suite_hash=suite_hash,
        commit_sha=commit_sha,
        exit_code=int(result.exit_code),
        duration_ms=int(result.duration_ms),
        at=at or utc_now_iso(),
        validation=validation,
    )


def summarize_history(records: Iterable[CheckRunRecord]) -> list[CheckHistorySummary]:
    """Aggregate records per check id, preserving first-seen order."""
    order: list[str] = []
    buckets: dict[str, dict[str, Any]] = {}
    for record in records:
        if record.check_id not in buckets:
            order.append(record.check_id)
            buckets[record.check_id] = {
                "runs": 0, "failures": 0, "unverified": 0,
                "validation": record.validation, "weakened": False,
            }
        bucket = buckets[record.check_id]
        bucket["runs"] = int(bucket["runs"]) + 1
        if record.state == STATUS_FAILED:
            bucket["failures"] = int(bucket["failures"]) + 1
        elif record.state == STATUS_UNVERIFIED:
            bucket["unverified"] = int(bucket["unverified"]) + 1
        bucket["validation"] = record.validation
        bucket["weakened"] = bool(bucket["weakened"]) or bool(record.weakened)
    return [
        CheckHistorySummary(
            check_id=check_id,
            runs=int(buckets[check_id]["runs"]),
            failures=int(buckets[check_id]["failures"]),
            unverified=int(buckets[check_id]["unverified"]),
            validation=str(buckets[check_id]["validation"]),
            weakened=bool(buckets[check_id]["weakened"]),
        )
        for check_id in order
    ]


class CheckHistoryStore:
    """In-memory run history — the offline/test double.

    The production store is :class:`services.check_store.DbCheckHistoryStore`,
    which writes the ``check_runs`` table; this class keeps the same three
    methods (``append`` / ``records`` / ``summaries``) so the gate can exercise
    the validator, the trust ladder and the flake detector with no database.
    """

    def __init__(self, records: Iterable[CheckRunRecord] = ()) -> None:
        self._records: list[CheckRunRecord] = list(records)

    def append(self, records: Iterable[CheckRunRecord]) -> None:
        self._records.extend(records)

    def records(self, *, check_id: str | None = None) -> list[CheckRunRecord]:
        if check_id is None:
            return list(self._records)
        return [record for record in self._records if record.check_id == check_id]

    def summaries(self) -> list[CheckHistorySummary]:
        return summarize_history(self._records)


__all__ = [
    "COPY_IGNORE",
    "DEFAULT_MAX_SEEDS",
    "MAX_FILES_PER_SEEDER",
    "SOURCE_DIRS",
    "CheckHistoryStore",
    "CheckHistorySummary",
    "CheckRunRecord",
    "CheckValidation",
    "FaultSeed",
    "SeedEdit",
    "Seeder",
    "default_seeders",
    "record_from_gate",
    "summarize_history",
    "validate_check",
]
