"""Gate normalisation — one shape for every ``backend/scripts/check_*.py``.

The backend already has a pile of offline regression gates.  They were written
one at a time and each prints its own flavour of pass/fail, which is fine for a
human running one command but useless to an agent that has to *prove* it did no
harm before opening a pull request (§9.4).  This module gives them one shape::

    run_gates()  ->  GateSummary(gates=[GateResult, ...], total, passed, failed)

Design rules:

* **Discovery, not a hand-kept list.**  A gate is any ``check_*.py`` beside this
  repository's other gates (the same convention ``scripts/check_lint.py`` uses
  for its lint targets).  Adding a gate is adding a file — there is no registry
  to forget to update.
* **Every gate gets its own budget.**  A hung gate must fail, not wedge the run;
  the default is :data:`DEFAULT_GATE_TIMEOUT` seconds per gate.
* **Output is tailed, never trusted to be small.**  A gate that dumps a
  megabyte of traceback is truncated to :data:`STDOUT_TAIL_LIMIT` bytes so the
  result document (and the log) cannot explode.
* **One entry point for the agent and for CI.**  ``render_summary`` is what the
  PR description pastes and ``main`` is what CI runs, so "green locally, red in
  CI" cannot come from two different gate implementations (§10).
* **Everything is injectable.**  :func:`run_gates` takes the script list and the
  executor, so the offline gate (:mod:`scripts.check_agent_runtime`) runs the
  whole thing with a fake executor and never spawns a process.
"""

from __future__ import annotations

import logging
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger("cpypiserver.gates")

#: ``<project>/backend`` — resolved from this file, exactly like
#: ``scripts/check_lint.py`` does, so discovery works from any cwd.
REPO_ROOT = Path(__file__).resolve().parent.parent

#: The directory the gate scripts live in, relative to :data:`REPO_ROOT`.
SCRIPTS_DIRNAME = "scripts"

#: What counts as a gate.  The same glob the project already uses for
#: ``check_lint.py`` / ``check_openapi.py`` / …; a new gate is a new file.
GATE_GLOB = "check_*.py"

#: Gates that need something the offline suite does not have.  ``check_contract``
#: validates live HTTP responses against the published OpenAPI document, so it
#: takes ``--base-url`` / ``--api-key`` and is run by its own Makefile target
#: against a throwaway server.  Discovery skips it by default; pass
#: ``include_live=True`` to include it.
LIVE_GATES: tuple[str, ...] = ("check_contract.py",)

#: Per-gate wall-clock budget.  A gate that hangs is worse than a gate that
#: fails, because it consumes a runner slot forever.
DEFAULT_GATE_TIMEOUT = 120.0

#: How much of a gate's combined stdout+stderr is kept (bytes of UTF-8 text).
STDOUT_TAIL_LIMIT = 4096

#: Prefix written in front of a tailed body so a reader can tell it was cut.
TRUNCATION_MARKER = "…[output truncated]\n"

#: Exit code a shell reports for a command killed by ``timeout(1)``; reused for
#: a gate this module kills so a timeout is distinguishable from a real failure.
TIMEOUT_EXIT_CODE = 124


class GateResult(BaseModel):
    """One gate's normalised outcome.

    The last two fields are extra information the runner records but that a
    reader of the summary does not need; ``gate``/``passed``/``exit_code``/
    ``stdout_tail``/``duration_ms`` are the §9.4 contract.
    """

    model_config = ConfigDict(strict=True)

    gate: str
    passed: bool
    exit_code: int
    stdout_tail: str = ""
    duration_ms: int = 0
    timed_out: bool = False


class GateSummary(BaseModel):
    """Every gate's result plus the counts a PR description quotes."""

    model_config = ConfigDict(strict=True)

    gates: list[GateResult] = Field(default_factory=list)
    total: int = 0
    passed: int = 0
    failed: int = 0
    duration_ms: int = 0

    @property
    def ok(self) -> bool:
        """True when nothing failed — an empty suite counts as green."""
        return self.failed == 0


@dataclass(frozen=True)
class ExecOutcome:
    """Raw executor result, before normalisation into a :class:`GateResult`."""

    exit_code: int
    output: str
    timed_out: bool
    duration_ms: int


class GateExecutor(Protocol):
    """Run one gate script under a timeout.  The only external dependency."""

    def run(self, script: Path, *, timeout: float) -> ExecOutcome:
        ...


# ── Executors ────────────────────────────────────────────────────────

def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _text(value: Any) -> str:
    """Decode subprocess output that may be ``str``, ``bytes`` or ``None``."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


class SubprocessGateExecutor:
    """Default executor: run the gate with the current interpreter.

    ``python`` is injectable so a test (or a deployment with a specific
    interpreter) can name it; ``env`` and ``cwd`` describe *where* the gate
    runs — the sandbox adapter points them at the cloned checkout.
    """

    def __init__(
        self,
        *,
        python: str | None = None,
        cwd: str | Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self._python = python or sys.executable
        self._cwd = Path(cwd) if cwd is not None else REPO_ROOT
        self._env = dict(env) if env is not None else None

    def run(self, script: Path, *, timeout: float) -> ExecOutcome:
        started = time.monotonic()
        try:
            proc = subprocess.run(
                [self._python, str(script)],
                cwd=str(self._cwd),
                capture_output=True,
                text=True,
                timeout=timeout,
                env=self._env,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            output = _text(exc.stdout) + _text(exc.stderr)
            note = f"\n[timeout] gate exceeded {timeout:g}s and was killed\n"
            return ExecOutcome(TIMEOUT_EXIT_CODE, output + note, True, _elapsed_ms(started))
        except OSError as exc:
            return ExecOutcome(
                127,
                f"[error] cannot execute {script}: {exc}\n",
                False,
                _elapsed_ms(started),
            )
        output = (proc.stdout or "") + (proc.stderr or "")
        return ExecOutcome(proc.returncode, output, False, _elapsed_ms(started))


# ── Discovery and execution ──────────────────────────────────────────

def scripts_root(scripts_dir: str | Path | None = None) -> Path:
    """The directory gates are discovered in; defaults to ``backend/scripts``."""
    return Path(scripts_dir) if scripts_dir is not None else REPO_ROOT / SCRIPTS_DIRNAME


def discover_gates(
    scripts_dir: str | Path | None = None,
    *,
    exclude: Sequence[str] = (),
    include_live: bool = False,
) -> list[Path]:
    """Every offline ``check_*.py`` gate, sorted by name for a stable run order.

    *exclude* names files by basename so a caller can drop one gate (for
    instance the runtime's own offline gate) without moving it.  Gates in
    :data:`LIVE_GATES` are skipped unless *include_live* is set.
    """
    root = scripts_root(scripts_dir)
    if not root.is_dir():
        return []
    skip = {str(name) for name in exclude}
    if not include_live:
        skip.update(LIVE_GATES)
    found = [
        path for path in root.glob(GATE_GLOB)
        if path.is_file() and path.name not in skip
    ]
    return sorted(found, key=lambda path: path.name)


def tail(text: str, *, limit: int = STDOUT_TAIL_LIMIT) -> str:
    """Keep the last *limit* characters, marking where the rest went."""
    body = text or ""
    if len(body) <= limit:
        return body
    return TRUNCATION_MARKER + body[-limit:]


def summarize(results: Sequence[GateResult]) -> GateSummary:
    """Count a list of normalised results into a :class:`GateSummary`."""
    items = list(results)
    passed = sum(1 for item in items if item.passed)
    return GateSummary(
        gates=items,
        total=len(items),
        passed=passed,
        failed=len(items) - passed,
        duration_ms=sum(item.duration_ms for item in items),
    )


def run_gates(
    paths: Sequence[Path] | None = None,
    *,
    scripts_dir: str | Path | None = None,
    executor: GateExecutor | None = None,
    timeout: float = DEFAULT_GATE_TIMEOUT,
    include_live: bool = False,
) -> GateSummary:
    """Discover (or accept) the gates and normalise every result.

    Each gate is run independently: one crashing gate does not stop the rest,
    which is what makes the summary a complete picture instead of "the first
    thing that went wrong".
    """
    targets = (
        [Path(path) for path in paths]
        if paths is not None
        else discover_gates(scripts_dir, include_live=include_live)
    )
    runner = executor or SubprocessGateExecutor()
    results: list[GateResult] = []
    for script in targets:
        try:
            outcome = runner.run(script, timeout=timeout)
        except Exception as exc:  # one gate must not abort the whole suite
            logger.exception("gate %s crashed its executor", script.stem)
            outcome = ExecOutcome(1, f"[error] executor crashed: {exc}\n", False, 0)
        results.append(
            GateResult(
                gate=script.stem,
                passed=outcome.exit_code == 0 and not outcome.timed_out,
                exit_code=int(outcome.exit_code),
                stdout_tail=tail(outcome.output),
                duration_ms=int(outcome.duration_ms),
                timed_out=bool(outcome.timed_out),
            )
        )
        logger.info(
            "gate %s %s (exit %s, %dms)",
            script.stem,
            "passed" if results[-1].passed else "failed",
            outcome.exit_code,
            outcome.duration_ms,
        )
    return summarize(results)


# ── Rendering ────────────────────────────────────────────────────────

def _rows(gates: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in gates:
        if isinstance(item, Mapping):
            rows.append(dict(item))
        elif isinstance(item, BaseModel):
            rows.append(item.model_dump())
        else:
            rows.append(dict(vars(item)))
    return rows


def render_summary(
    summary: GateSummary | Sequence[GateResult] | Sequence[Mapping[str, Any]],
) -> str:
    """A Markdown table the PR description (or a log) can paste verbatim.

    Accepts a :class:`GateSummary`, a list of :class:`GateResult`, or a list of
    plain mappings, so both the runner and the route layer can render whatever
    they hold without converting first.
    """
    if isinstance(summary, GateSummary):
        rows = _rows(summary.gates)
        total, passed, failed = summary.total, summary.passed, summary.failed
    else:
        rows = _rows(summary)
        total = len(rows)
        passed = sum(1 for row in rows if row.get("passed"))
        failed = total - passed

    lines = [
        "### Gates",
        "",
        "| gate | result | exit | time |",
        "| --- | --- | --- | --- |",
    ]
    for row in rows:
        mark = "✅ pass" if row.get("passed") else "❌ fail"
        exit_code = row.get("exit_code")
        if row.get("timed_out"):
            exit_note = f"{exit_code} (timeout)"
        else:
            exit_note = str(exit_code if exit_code is not None else "—")
        lines.append(
            f"| `{row.get('gate', '?')}` | {mark} | {exit_note} | "
            f"{row.get('duration_ms', 0)} ms |"
        )
    lines.append("")
    if failed:
        lines.append(f"**Gates: {passed}/{total} passed — {failed} failed**")
    else:
        lines.append(f"**Gates: {passed}/{total} passed — all green**")
    return "\n".join(lines)


# ── CLI ──────────────────────────────────────────────────────────────

def main(argv: Sequence[str] | None = None) -> int:
    """Run every gate and print the summary; 0 only when all green.

    CI calls this — never a second, hand-rolled loop over the gate scripts — so
    the agent's self-check and the pipeline check the same thing (§10).
    """
    del argv  # no options today; the signature keeps room for them
    summary = run_gates()
    print(render_summary(summary))
    return 0 if summary.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "DEFAULT_GATE_TIMEOUT",
    "GATE_GLOB",
    "LIVE_GATES",
    "REPO_ROOT",
    "STDOUT_TAIL_LIMIT",
    "TIMEOUT_EXIT_CODE",
    "TRUNCATION_MARKER",
    "ExecOutcome",
    "GateExecutor",
    "GateResult",
    "GateSummary",
    "SubprocessGateExecutor",
    "discover_gates",
    "main",
    "render_summary",
    "run_gates",
    "scripts_root",
    "summarize",
    "tail",
]
