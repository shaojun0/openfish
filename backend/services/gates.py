"""Gate normalisation — one shape for every verification command.

The backend already has a pile of offline regression gates.  They were written
one at a time and each prints its own flavour of pass/fail, which is fine for a
human running one command but useless to an agent that has to *prove* it did no
harm before opening a pull request (§9.4).  This module gives them one shape::

    run_gates()  ->  GateSummary(gates=[GateResult, ...], total, passed, failed)
    run_suite()  ->  GateSummary(...)  # any ordered check descriptor

Design rules:

* **Discovery, not a hand-kept list.**  A gate is any ``check_*.py`` beside this
  repository's other gates (the same convention ``scripts/check_lint.py`` uses
  for its lint targets).  Adding a gate is adding a file — there is no registry
  to forget to update.  ``check_*.py`` is now *one provider* of a
  :class:`CheckSuite`; a suite can equally be a list of shell-free commands
  resolved from ``.agent/checks/`` or from a repository's manifests.
* **Three outcomes, never two.**  A verification suite is ``passed``,
  ``failed`` or — when nothing could be resolved — ``unverified``.  The third
  state is what stops "we ran the image's own gates against a foreign repo" from
  looking green (see :func:`run_suite`); ``unverified`` never authorises a push
  and, in particular, is never the same value as "passed".
* **Every gate gets its own budget.**  A hung gate must fail, not wedge the run;
  the default is :data:`DEFAULT_GATE_TIMEOUT` seconds per gate.
* **Output is tailed, never trusted to be small.**  A gate that dumps a
  megabyte of traceback is truncated to :data:`STDOUT_TAIL_LIMIT` bytes so the
  result document (and the log) cannot explode.
* **One entry point for the agent and for CI.**  ``render_summary`` is what the
  PR description pastes and ``main`` is what CI runs, so "green locally, red in
  CI" cannot come from two different gate implementations (§10).
* **Everything is injectable.**  :func:`run_gates` / :func:`run_suite` take the
  command list and the executor, so the offline gate
  (:mod:`scripts.check_agent_runtime`) runs the whole thing with a fake executor
  and never spawns a process.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

from pydantic import BaseModel, ConfigDict, Field, model_validator

from services.digest import sha256_text
from services.sandbox_env import sandbox_env
from services.sandbox_identity import (
    SandboxIdentityError,
    sandbox_env_overrides,
    untrusted_popen_kwargs,
)

logger = logging.getLogger("cpypiserver.gates")

#: ``<project>/backend`` — resolved from this file, exactly like
#: ``scripts/check_lint.py`` does, so discovery works from any cwd.
REPO_ROOT = Path(__file__).resolve().parent.parent

#: The directory the gate scripts live in, relative to :data:`REPO_ROOT`.
SCRIPTS_DIRNAME = "scripts"

#: What counts as a gate script.  The same glob the project already uses for
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

#: Interpreter a *suite descriptor* names for Python checks.  Deliberately the
#: bare name rather than ``sys.executable``: the descriptor's fingerprint must
#: not change with the machine that resolved it, and the sandbox image always
#: has ``python`` on ``PATH``.  (``SubprocessGateExecutor`` still defaults to
#: ``sys.executable`` for the legacy direct-script path.)
SUITE_PYTHON = "python"

#: How much of a gate's combined stdout+stderr is kept (bytes of UTF-8 text).
STDOUT_TAIL_LIMIT = 4096

#: Prefix written in front of a tailed body so a reader can tell it was cut.
TRUNCATION_MARKER = "…[output truncated]\n"

#: Exit code a shell reports for a command killed by ``timeout(1)``; reused for
#: a gate this module kills so a timeout is distinguishable from a real failure.
TIMEOUT_EXIT_CODE = 124

# ── The three outcomes ───────────────────────────────────────────────
# One vocabulary shared by a single check and by the whole suite.  ``unverified``
# is deliberately not a synonym for either "passed" or "failed": it means *no
# check judged this change*, which is the one state that must never unlock a PR.

STATUS_PASSED = "passed"
STATUS_FAILED = "failed"
STATUS_UNVERIFIED = "unverified"
GATE_STATUSES: tuple[str, ...] = (STATUS_PASSED, STATUS_FAILED, STATUS_UNVERIFIED)

# ── Where a resolved suite came from ─────────────────────────────────
# Recorded on the suite and in the result document so a reader can tell an
# agent-authored suite from the repository's own long-standing gates.

SOURCE_AGENT_CHECKS = "agent-checks"   # .agent/checks/ — versioned, approved
SOURCE_POLICY = "policy"               # .agent/review-policy.yml ``checks:``
SOURCE_DISCOVERY = "discovery"         # manifest auto-discovery (zero config)
SOURCE_SCRIPTS = "scripts"             # <repo>/backend/scripts/check_*.py
SOURCE_EXPLICIT = "explicit"           # the caller handed us the commands
SOURCE_UNVERIFIED = "unverified"       # nothing resolvable — cannot gate
SUITE_SOURCES: tuple[str, ...] = (
    SOURCE_AGENT_CHECKS,
    SOURCE_POLICY,
    SOURCE_DISCOVERY,
    SOURCE_SCRIPTS,
    SOURCE_EXPLICIT,
    SOURCE_UNVERIFIED,
)

# ── Falsifiability before a check may gate (hard invariant) ──────────
# A check that an agent just wrote is *unvalidated* until a validator proves it
# passes on the current revision and fails on at least one known-bad revision.
# Unvalidated checks may report; they must never be the reason a PR opens.

VALIDATION_VALIDATED = "validated"
VALIDATION_UNVALIDATED = "unvalidated"
VALIDATIONS: tuple[str, ...] = (VALIDATION_VALIDATED, VALIDATION_UNVALIDATED)


class GateResult(BaseModel):
    """One gate's normalised outcome.

    ``status`` is the tri-state (``passed`` / ``failed`` / ``unverified``);
    ``passed`` is kept for the §9.4 contract and for callers that only ask the
    boolean question.  A ``failed`` result carries ``passed=False``; an
    ``unverified`` one also carries ``passed=False`` but must not be silently
    read as a failure — use ``status``.

    The last fields are extra information the runner records but that a reader
    of the summary does not need; ``gate``/``passed``/``exit_code``/
    ``stdout_tail``/``duration_ms`` are the §9.4 contract.
    """

    model_config = ConfigDict(strict=True)

    gate: str
    passed: bool
    status: str = STATUS_PASSED
    exit_code: int
    stdout_tail: str = ""
    duration_ms: int = 0
    timed_out: bool = False

    @model_validator(mode="after")
    def _sync_status(self) -> "GateResult":
        """Keep ``status`` and ``passed`` from disagreeing.

        A caller that only knows ``passed=False`` (every pre-existing caller)
        gets ``status="failed"``; a caller that marks a check ``unverified``
        gets ``passed=False`` without the check counting as a real failure.
        """
        if self.status not in GATE_STATUSES:
            raise ValueError(f"status={self.status!r} 不是 {GATE_STATUSES} 之一")
        if self.status == STATUS_UNVERIFIED:
            self.passed = False
        elif not self.passed:
            self.status = STATUS_FAILED
        return self


class GateSummary(BaseModel):
    """Every gate's result plus the counts a PR description quotes.

    ``state`` is the suite-level outcome.  An **empty, unresolved** suite is
    ``unverified`` — that is the false-green this module exists to prevent — and
    :attr:`ok` is only true for an actual ``passed`` suite.
    """

    model_config = ConfigDict(strict=True)

    gates: list[GateResult] = Field(default_factory=list)
    total: int = 0
    passed: int = 0
    failed: int = 0
    unverified: int = 0
    duration_ms: int = 0
    state: str = ""
    reason: str = ""
    suite_source: str = ""
    suite_hash: str = ""

    @model_validator(mode="after")
    def _sync_state(self) -> "GateSummary":
        if self.state and self.state not in GATE_STATUSES:
            raise ValueError(f"state={self.state!r} 不是 {GATE_STATUSES} 之一")
        if not self.state:
            # A hand-built summary (tests, an injected fake adapter) declares no
            # state: derive it from the counts it did provide.  An empty one is
            # ``passed`` only because :func:`run_suite` — the one place that can
            # resolve a real suite — never returns an empty summary without
            # explicitly marking it ``unverified``.
            if self.failed:
                self.state = STATUS_FAILED
            elif self.unverified and not self.passed:
                self.state = STATUS_UNVERIFIED
            else:
                self.state = STATUS_PASSED
        return self

    @property
    def ok(self) -> bool:
        """True only for a resolved suite that passed every gating check."""
        return self.state == STATUS_PASSED and self.failed == 0

    @property
    def verified(self) -> bool:
        """False when no check judged the change — never treat that as green."""
        return self.state != STATUS_UNVERIFIED


# ── The suite descriptor ─────────────────────────────────────────────

class CheckCommand(BaseModel):
    """One verification command in a suite.

    * ``id`` is stable across runs and is what a finding or a PR body can name;
    * ``argv`` is an argument vector, never a shell string, so a command that
      came out of a manifest cannot become a shell injection;
    * ``cwd`` is relative to the repository root (an absolute path would make
      the suite hash depend on where it was checked out);
    * ``timeout`` overrides :data:`DEFAULT_GATE_TIMEOUT` for this check;
    * ``validation`` is the hard invariant: only a ``validated`` check may gate
      a PR, an ``unvalidated`` one reports but never unlocks a push.
    """

    model_config = ConfigDict(strict=True)

    id: str
    argv: list[str]
    cwd: str = "."
    timeout: float | None = None
    validation: str = VALIDATION_VALIDATED
    source: str = SOURCE_EXPLICIT

    @model_validator(mode="after")
    def _check_command(self) -> "CheckCommand":
        if not self.id.strip():
            raise ValueError("check id 不能为空")
        if not self.argv or not str(self.argv[0]).strip():
            raise ValueError(f"check {self.id!r} 的 argv 为空")
        if self.validation not in VALIDATIONS:
            raise ValueError(
                f"check {self.id!r} validation={self.validation!r} "
                f"不是 {VALIDATIONS} 之一"
            )
        return self

    @property
    def gating(self) -> bool:
        """Whether this check may block (and therefore unlock) a PR."""
        return self.validation == VALIDATION_VALIDATED


class CheckSuite(BaseModel):
    """An ordered list of checks plus where it came from.

    Ordered on purpose: a suite is executed top to bottom and its
    :func:`suite_fingerprint` digests that order, so "the same checks in a
    different order" is a different, reviewable suite.
    """

    model_config = ConfigDict(strict=True)

    checks: list[CheckCommand] = Field(default_factory=list)
    source: str = SOURCE_UNVERIFIED
    reason: str = ""

    @model_validator(mode="after")
    def _check_source(self) -> "CheckSuite":
        if self.source not in SUITE_SOURCES:
            raise ValueError(f"suite source={self.source!r} 不是 {SUITE_SOURCES} 之一")
        ids: set[str] = set()
        for check in self.checks:
            if check.id in ids:
                raise ValueError(f"suite 里出现重复的 check id：{check.id!r}")
            ids.add(check.id)
        return self

    @property
    def gating(self) -> list[CheckCommand]:
        """The checks allowed to gate — the ones a validator has confirmed."""
        return [check for check in self.checks if check.gating]

    @property
    def reporting(self) -> list[CheckCommand]:
        """The checks that may report but never gate (``unvalidated``)."""
        return [check for check in self.checks if not check.gating]

    @property
    def resolved(self) -> bool:
        """True when at least one check may judge a change."""
        return bool(self.gating)


def suite_fingerprint(suite: CheckSuite) -> str:
    """Stable hash of a suite's meaning — order, commands, validation.

    ``reason`` is excluded: it describes *this read* (for instance which
    provider matched), not the suite, exactly like ``policy_hash`` excludes a
    policy's warnings.  Absolute paths are excluded too because ``cwd`` is
    relative to the repository root.
    """
    normalized = [
        {
            "id": check.id,
            "argv": list(check.argv),
            "cwd": check.cwd,
            "timeout": check.timeout,
            "validation": check.validation,
        }
        for check in suite.checks
    ]
    payload = f"{suite.source}:{normalized!r}"
    return sha256_text(payload)


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


class CommandExecutor(Protocol):
    """Run one suite command under a timeout (the generalised executor)."""

    def run_command(
        self,
        command: CheckCommand,
        *,
        root: Path,
        timeout: float,
    ) -> ExecOutcome:
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


#: Environment policies for :class:`SubprocessGateExecutor`.  ``sandbox`` is the
#: default because the executor's normal caller is the agent runtime, which runs
#: a *cloned repository's own* checks; ``inherit`` is the explicit opt-in for the
#: project's own trusted gates (``run_gates`` / ``make gates``).
ENV_SANDBOX = "sandbox"
ENV_INHERIT = "inherit"


def _terminate_process_group(proc: subprocess.Popen) -> None:
    """Kill *proc* **and every process it forked**, not just the direct child.

    ``subprocess.run(timeout=…)`` only kills the child it started, so a check
    that daemonised a grandchild leaves it running for the container's lifetime —
    still holding CPU, sockets and its view of the checkout.  Every check is
    started in its own session (``start_new_session=True``) precisely so the
    whole group can be signalled here.
    """
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        proc.kill()


class SubprocessGateExecutor:
    """Default executor: run a gate with the current interpreter.

    ``python`` is injectable so a test (or a deployment with a specific
    interpreter) can name it; ``env`` and ``cwd`` describe *where* the legacy
    script executor runs — the sandbox adapter points them at the cloned
    checkout.  :meth:`run_command` ignores ``cwd`` and resolves every command's
    own ``cwd`` against the suite root instead, which is what keeps a suite
    portable between checkouts.

    ``env_mode`` decides what the child inherits: ``sandbox`` (the default) hands
    it the allowlist from :mod:`services.sandbox_env`, so repository-supplied
    checks never see the worker's platform credentials; ``inherit`` is for the
    project's own ``check_*.py``, which legitimately need the CI environment.
    """

    def __init__(
        self,
        *,
        python: str | None = None,
        cwd: str | Path | None = None,
        env: Mapping[str, str] | None = None,
        env_mode: str = ENV_SANDBOX,
    ) -> None:
        if env_mode not in (ENV_SANDBOX, ENV_INHERIT):
            raise ValueError(
                f"env_mode must be {ENV_SANDBOX!r} or {ENV_INHERIT!r}, got {env_mode!r}"
            )
        self._python = python or sys.executable
        self._cwd = Path(cwd) if cwd is not None else REPO_ROOT
        self._env = dict(env) if env is not None else None
        self._env_mode = env_mode

    def _child_env(self) -> dict[str, str]:
        """The environment a check runs under.

        In the default ``sandbox`` mode this is an allowlist, not
        ``dict(os.environ)``: a check comes from the repository under review,
        while the worker's environment holds the platform signing key, the
        Forgejo admin token and the git-identity key.

        ``PYTHONDONTWRITEBYTECODE`` is set unconditionally: a check runs inside
        the reviewed checkout, and a stray ``__pycache__`` would show up as an
        edit the agent made — which the path guard would then (correctly, but
        uselessly) flag.  Gates must leave no trace in the tree they judge.
        """
        child = dict(os.environ) if self._env_mode == ENV_INHERIT else sandbox_env()
        child["PYTHONDONTWRITEBYTECODE"] = "1"
        if self._env:
            child.update(self._env)
        return child

    def _capture(
        self,
        argv: Sequence[str],
        *,
        cwd: str,
        timeout: float,
    ) -> tuple[int, str, bool]:
        """Run *argv* in its own process group; return ``(exit, output, timed_out)``.

        A check is **untrusted code**: it comes from the repository under review.
        When the deployment configures a sandbox identity
        (``AGENT_SANDBOX_UID`` / ``AGENT_SANDBOX_GID``) the child is dropped to
        that uid/gid, so it can no longer read the worker's
        ``/proc/<pid>/environ`` and walk off with ``FORGEJO_RUNNER_TOKEN``.  The
        ``HOME`` override is merged **after** :func:`sandbox_env`, which is the
        allowlist that has already dropped the platform credentials; it points at
        a worker-owned directory outside the checkout, never at anything inside
        the repository's own (writable) work tree.  Without the two env vars this
        is exactly the spawn it always was (dev / this gate).
        """
        child = self._child_env()
        child.update(sandbox_env_overrides())
        proc = subprocess.Popen(
            list(argv),
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=child,
            start_new_session=True,
            # ``{}`` in dev mode; a configured-but-unusable identity raises
            # ``SandboxIdentityError`` and the gate fails loudly instead of
            # quietly running repository code as the trusted worker.
            **untrusted_popen_kwargs(),
        )
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _terminate_process_group(proc)
            out, err = proc.communicate()
            return TIMEOUT_EXIT_CODE, _text(out) + _text(err), True
        return int(proc.returncode), _text(out) + _text(err), False

    def run(self, script: Path, *, timeout: float) -> ExecOutcome:
        started = time.monotonic()
        try:
            code, output, timed_out = self._capture(
                [self._python, str(script)], cwd=str(self._cwd), timeout=timeout,
            )
        except OSError as exc:
            return ExecOutcome(
                127, f"[error] cannot execute {script}: {exc}\n", False,
                _elapsed_ms(started),
            )
        except SandboxIdentityError as exc:
            # A configured-but-unusable sandbox identity is a failed gate, not a
            # reason to fall back to the trusted uid: repository code must never
            # run beside the worker's credential just because the drop failed.
            return ExecOutcome(
                127, f"[error] sandbox identity unusable: {exc}\n", False,
                _elapsed_ms(started),
            )
        if timed_out:
            output += f"\n[timeout] gate exceeded {timeout:g}s and was killed\n"
        return ExecOutcome(code, output, timed_out, _elapsed_ms(started))

    def run_command(
        self,
        command: CheckCommand,
        *,
        root: Path,
        timeout: float,
    ) -> ExecOutcome:
        """Run one :class:`CheckCommand` with its own ``cwd`` under *root*."""
        started = time.monotonic()
        cwd = Path(root) / command.cwd if command.cwd else Path(root)
        argv = [str(part) for part in command.argv]
        try:
            code, output, timed_out = self._capture(argv, cwd=str(cwd), timeout=timeout)
        except OSError as exc:
            return ExecOutcome(
                127,
                f"[error] cannot execute check {command.id} ({argv[0]}): {exc}\n",
                False,
                _elapsed_ms(started),
            )
        except SandboxIdentityError as exc:
            # Same fail-closed rule as :meth:`run`: the check is repository code,
            # so a broken drop is a red gate and never a spawn as the worker.
            return ExecOutcome(
                127,
                f"[error] sandbox identity unusable for check {command.id}: {exc}\n",
                False,
                _elapsed_ms(started),
            )
        if timed_out:
            output += f"\n[timeout] check {command.id} exceeded {timeout:g}s and was killed\n"
        return ExecOutcome(code, output, timed_out, _elapsed_ms(started))


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


def suite_from_scripts(
    scripts_dir: str | Path,
    *,
    root: str | Path | None = None,
    excludes: Sequence[str] = (),
) -> CheckSuite:
    """The ``backend/scripts/check_*.py`` provider: one check per gate file.

    The command is ``<python> <relative script>`` with ``cwd`` at the repository
    root, so the descriptor is independent of where the repo was checked out.
    These are the repository's own long-standing gates (not a suite an agent
    wrote in this run), which is why they are ``validated`` and may gate.
    """
    base = scripts_root(scripts_dir)
    repo = Path(root) if root is not None else base.parent.parent
    commands: list[CheckCommand] = []
    for path in discover_gates(scripts_dir, exclude=excludes):
        commands.append(
            CheckCommand(
                id=path.stem,
                argv=[SUITE_PYTHON, _relative(path, repo)],
                cwd=".",
                validation=VALIDATION_VALIDATED,
                source=SOURCE_SCRIPTS,
            )
        )
    return CheckSuite(checks=commands, source=SOURCE_SCRIPTS)


def _relative(path: Path, root: Path) -> str:
    """*path* relative to *root* when possible, else the absolute path."""
    try:
        return path.resolve().relative_to(Path(root).resolve()).as_posix()
    except (OSError, ValueError):
        return str(path)


def unverified_summary(reason: str, *, source: str = SOURCE_UNVERIFIED) -> GateSummary:
    """The explicit "nothing judged this" summary — never green.

    A synthetic gate entry carries the reason so a PR body, a log line and the
    result document all show *why* the suite could not verify anything instead
    of an empty (and therefore accidentally green) table.
    """
    entry = GateResult(
        gate=f"suite:{source}",
        passed=False,
        status=STATUS_UNVERIFIED,
        exit_code=-1,
        stdout_tail=reason,
    )
    return GateSummary(
        gates=[entry],
        total=1,
        passed=0,
        failed=0,
        unverified=1,
        state=STATUS_UNVERIFIED,
        reason=reason,
        suite_source=source,
    )


def tail(text: str, *, limit: int = STDOUT_TAIL_LIMIT) -> str:
    """Keep the last *limit* characters, marking where the rest went."""
    body = text or ""
    if len(body) <= limit:
        return body
    return TRUNCATION_MARKER + body[-limit:]


def summarize(results: Sequence[GateResult]) -> GateSummary:
    """Count a list of normalised results into a :class:`GateSummary`."""
    items = list(results)
    passed = sum(1 for item in items if item.status == STATUS_PASSED)
    failed = sum(1 for item in items if item.status == STATUS_FAILED)
    unverified = sum(1 for item in items if item.status == STATUS_UNVERIFIED)
    if failed:
        state = STATUS_FAILED
    elif passed:
        state = STATUS_PASSED
    else:
        state = STATUS_UNVERIFIED
    return GateSummary(
        gates=items,
        total=len(items),
        passed=passed,
        failed=failed,
        unverified=unverified,
        duration_ms=sum(item.duration_ms for item in items),
        state=state,
    )


def run_suite(
    suite: CheckSuite,
    *,
    root: str | Path | None = None,
    executor: CommandExecutor | None = None,
    timeout: float = DEFAULT_GATE_TIMEOUT,
) -> GateSummary:
    """Execute an ordered :class:`CheckSuite` and normalise every result.

    The invariant lives here: a suite with **no gating check** — an empty one,
    or one made only of ``unvalidated`` checks — returns
    :data:`STATUS_UNVERIFIED`.  It is never a pass, so a caller that gates a PR
    on "green" cannot be fooled by an empty or agent-authored-but-unproven
    suite.  ``unvalidated`` checks still *run* and their output is reported.
    """
    checks = list(suite.checks)
    if not checks:
        return unverified_summary(
            suite.reason or "仓库里没有可解析的校验套件（.agent/checks/、policy checks:、"
                            "manifest 自动发现都没有命中）",
            source=suite.source or SOURCE_UNVERIFIED,
        )
    runner = executor or SubprocessGateExecutor()
    base = Path(root) if root is not None else REPO_ROOT
    results: list[GateResult] = []
    for command in checks:
        budget = float(command.timeout or timeout)
        try:
            outcome = runner.run_command(command, root=base, timeout=budget)
        except Exception as exc:  # one check must not abort the whole suite
            logger.exception("check %s crashed its executor", command.id)
            outcome = ExecOutcome(1, f"[error] executor crashed: {exc}\n", False, 0)
        if not command.gating:
            # Report-only: even a red result is informational, not a failure.
            status = STATUS_UNVERIFIED
        elif outcome.exit_code == 0 and not outcome.timed_out:
            status = STATUS_PASSED
        else:
            status = STATUS_FAILED
        results.append(
            GateResult(
                gate=command.id,
                passed=outcome.exit_code == 0 and not outcome.timed_out,
                status=status,
                exit_code=int(outcome.exit_code),
                stdout_tail=tail(outcome.output),
                duration_ms=int(outcome.duration_ms),
                timed_out=bool(outcome.timed_out),
            )
        )
        logger.info(
            "check %s %s (exit %s, %dms)",
            command.id,
            results[-1].status,
            outcome.exit_code,
            outcome.duration_ms,
        )
    summary = summarize(results)
    summary.suite_source = suite.source
    summary.suite_hash = suite_fingerprint(suite)
    if not suite.resolved:
        summary.state = STATUS_UNVERIFIED
        summary.reason = (
            suite.reason
            or "套件里的检查都还是 unvalidated（未经证伪验证），只能报告、不能门控"
        )
    return summary


def run_gates(
    paths: Sequence[Path] | None = None,
    *,
    scripts_dir: str | Path | None = None,
    executor: GateExecutor | None = None,
    timeout: float = DEFAULT_GATE_TIMEOUT,
    include_live: bool = False,
) -> GateSummary:
    """Discovery-driven wrapper around the legacy ``check_*.py`` provider.

    Kept because ``services.gates.main`` (CI) and the offline gate both call it;
    a caller that has a resolved :class:`CheckSuite` should call
    :func:`run_suite` directly.  A discovery that finds nothing is
    ``unverified``, not an empty green table.
    """
    targets = (
        [Path(path) for path in paths]
        if paths is not None
        else discover_gates(scripts_dir, include_live=include_live)
    )
    if not targets:
        return unverified_summary(
            f"在 {scripts_root(scripts_dir)} 里没有发现任何 check_*.py"
        )
    # These are the *project's own* gates, not a foreign checkout's: they are
    # trusted code and need the caller's full environment (CI variables, DB URL,
    # mirrors).  Only this discovery path opts out of the sandbox allowlist.
    runner = executor or SubprocessGateExecutor(env_mode=ENV_INHERIT)
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
            results[-1].status,
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


def _status_of(row: Mapping[str, Any]) -> str:
    status = str(row.get("status") or "")
    if status in GATE_STATUSES:
        return status
    return STATUS_PASSED if row.get("passed") else STATUS_FAILED


def render_summary(
    summary: GateSummary | Sequence[GateResult] | Sequence[Mapping[str, Any]],
    *,
    title: str = "Gates",
) -> str:
    """A Markdown table the PR description (or a log) can paste verbatim.

    Accepts a :class:`GateSummary`, a list of :class:`GateResult`, or a list of
    plain mappings, so both the runner and the route layer can render whatever
    they hold without converting first.  ``unverified`` gets its own marker:
    rendering it as a failure would hide that the real problem is "nothing
    checked this", not "a check went red".  *title* lets the two-suite report
    label each block without a second table renderer.
    """
    reason = ""
    if isinstance(summary, GateSummary):
        rows = _rows(summary.gates)
        total, passed, failed = summary.total, summary.passed, summary.failed
        unverified = summary.unverified
        state = summary.state
        reason = summary.reason
    else:
        rows = _rows(summary)
        total = len(rows)
        statuses = [_status_of(row) for row in rows]
        passed = statuses.count(STATUS_PASSED)
        failed = statuses.count(STATUS_FAILED)
        unverified = statuses.count(STATUS_UNVERIFIED)
        state = STATUS_FAILED if failed else (STATUS_PASSED if passed else STATUS_UNVERIFIED)

    marks = {
        STATUS_PASSED: "✅ pass",
        STATUS_FAILED: "❌ fail",
        STATUS_UNVERIFIED: "⚠️ unverified",
    }
    lines = [
        f"### {title}",
        "",
        "| gate | result | exit | time |",
        "| --- | --- | --- | --- |",
    ]
    for row in rows:
        mark = marks[_status_of(row)]
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
    if state == STATUS_UNVERIFIED:
        lines.append(
            f"**{title}: unverified — {passed}/{total} passed, {failed} failed, "
            f"{unverified} unverified；没有可门控的校验，禁止据此自动 push/开 PR**"
        )
        if reason:
            lines.append(f"\n> {reason}")
    elif failed:
        lines.append(f"**{title}: {passed}/{total} passed — {failed} failed**")
    else:
        lines.append(f"**{title}: {passed}/{total} passed — all green**")
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
    "GATE_STATUSES",
    "LIVE_GATES",
    "REPO_ROOT",
    "SOURCE_AGENT_CHECKS",
    "SOURCE_DISCOVERY",
    "SOURCE_EXPLICIT",
    "SOURCE_POLICY",
    "SOURCE_SCRIPTS",
    "SOURCE_UNVERIFIED",
    "STATUS_FAILED",
    "STATUS_PASSED",
    "STATUS_UNVERIFIED",
    "STDOUT_TAIL_LIMIT",
    "SUITE_SOURCES",
    "TIMEOUT_EXIT_CODE",
    "TRUNCATION_MARKER",
    "VALIDATION_UNVALIDATED",
    "VALIDATION_VALIDATED",
    "VALIDATIONS",
    "CheckCommand",
    "CheckSuite",
    "CommandExecutor",
    "ExecOutcome",
    "GateExecutor",
    "GateResult",
    "GateSummary",
    "SubprocessGateExecutor",
    "discover_gates",
    "main",
    "render_summary",
    "run_gates",
    "run_suite",
    "scripts_root",
    "suite_fingerprint",
    "suite_from_scripts",
    "summarize",
    "tail",
    "unverified_summary",
]
