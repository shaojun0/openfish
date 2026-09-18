"""The agent runtime — one task's lifecycle, from clone to result document.

This module owns §9.3's fixed six-step protocol and nothing else::

    1 clone   只读副本 fetch 目标 sha
    2 read    AGENTS.md + .agent/review-policy.yml（没有则 builtin-default）
    3 gates   跑 backend/scripts/check_*.py（services.gates）
    4 review  按 policy 规则产出 findings
    5 search  每条 finding 先检索历史 issue，填 evidence
    6 emit    写 result.json（仅 fix 模式才推分支开 PR）

It deliberately **does not store state**: the task/run rows live in the database
(S0's ``models/agent_hub.py`` + ``services/agent_queue.py``) and are touched
through an injected :class:`TaskSink`.  It also does not deduplicate findings —
it validates the §9.5 document and hands it to the platform, which gives it to
S3's ingest.

Three invariants are enforced here in code rather than in prose:

* **I4** — :func:`assert_pushable` is the single gate every push goes through;
  only ``agent/*`` refs pass and protected branches are refused by name.
* **§8.4/I6** — imported issue text is wrapped by :func:`wrap_untrusted` before
  it reaches a model prompt, and the notice says it is evidence, not orders.
* **§9.5** — :func:`validate_result` is the only way a document becomes a
  :class:`Result`; a document that fails validation fails the *task* and writes
  no finding ("宁缺勿脏").

Everything external (the ``git`` CLI, docker, subprocesses) sits behind
:class:`RunnerAdapter`.  :class:`SubprocessRunnerAdapter` is the real
implementation and takes callables for the two steps only the platform can wire
(the model review and the Forgejo PR); the offline gate injects a recording fake
and never clones, never starts a container and never calls a model.

Model credentials are read from the existing route table via
``services.model_routes.resolve``, passed to child processes through the
environment, cleared when the task ends, and **never written to the work
directory or to a log in cleartext** — :class:`SecretRedactingFilter` and
:func:`mask_secrets` are the two safety nets.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Literal, Mapping, Protocol, Sequence

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from services.fileio import write_json
from services.format import utc_now_iso
from services.gates import (
    DEFAULT_GATE_TIMEOUT,
    GateResult,
    GateSummary,
    SubprocessGateExecutor,
    render_summary,
    run_gates as run_gate_suite,
)
from services.model_routes import mask_api_key, resolve

logger = logging.getLogger("cpypiserver.agent_runner")

# ── Protocol constants ───────────────────────────────────────────────

#: The six steps of §9.3, in the order the runner must take them.
STEPS: tuple[str, ...] = ("clone", "read", "gates", "review", "search", "emit")

#: The only branch namespace the runtime may push (I4).
AGENT_BRANCH_PREFIX = "agent/"

#: Branch names that are protected regardless of prefix.  The prefix rule
#: already refuses ``main``; the explicit set exists so the refusal message is
#: about protection rather than about a missing prefix.
PROTECTED_BRANCHES: frozenset[str] = frozenset(
    {"main", "master", "trunk", "develop", "release"}
)

#: ``agent/<name>`` where ``<name>`` starts with an alphanumeric.  Kept narrow
#: on purpose: a ref with ``..``, a leading ``-`` or a control character is how
#: a branch name becomes a command-line argument.
_BRANCH_RE = re.compile(r"^agent/[A-Za-z0-9][A-Za-z0-9._/-]*$")
_FORBIDDEN_BRANCH_CHARS = frozenset(" ~^:?*[\\\x7f")

#: Name of the document the runner writes in the work directory (§9.5).
RESULT_FILENAME = "result.json"

#: Files the ``read`` step looks for in the checkout.
AGENTS_FILENAME = "AGENTS.md"
POLICY_RELPATH = Path(".agent") / "review-policy.yml"

#: Environment variable names the platform may set; these are the S4 slice's
#: knobs and are documented for promotion into ``config/hub.py``.
ENV_WORK_ROOT = "AGENT_WORK_ROOT"
ENV_WORK_RETENTION = "AGENT_WORK_RETENTION_SECONDS"
ENV_GATE_TIMEOUT = "AGENT_GATE_TIMEOUT"
ENV_MAX_FINDINGS = "AGENT_MAX_FINDINGS"

#: Defaults mirroring §9.2 (``/work/<task_id>``, 24h) and §9.4 (120s).
DEFAULT_WORK_ROOT = "/work"
DEFAULT_RETENTION_SECONDS = 24 * 60 * 60
DEFAULT_MAX_FINDINGS = 50

#: Marker written when a task ends; ``sweep_workdirs`` only ever removes a
#: directory that carries one, so an in-flight task is never swept.
FINISHED_MARKER = ".done"

#: Prefix for the environment block handed to child processes.
MODEL_ENV_PREFIX = "OPENFISH_MODEL"

#: Substrings that mark an environment variable as a secret to mask.
SECRET_HINTS: tuple[str, ...] = ("KEY", "TOKEN", "SECRET", "PASSWORD")

#: Tag used to fence imported issue/PR text (§8.4 / I6).  The fence itself is
#: **not** reimplemented here: ``services.repo_context.wrap_untrusted`` owns it,
#: together with ``UNTRUSTED_PREAMBLE`` (the sentence that goes in the prompt).
UNTRUSTED_TAG = "untrusted-issue"


# ── Errors ───────────────────────────────────────────────────────────

class AgentRunnerError(Exception):
    """Base class for the runtime's domain errors."""


class ProtectedBranchError(AgentRunnerError):
    """A push was attempted to something other than an ``agent/*`` branch (I4)."""


class ResultValidationError(AgentRunnerError):
    """The §9.5 document did not validate; the task must fail closed."""


# ── I4: the push guard ───────────────────────────────────────────────

def assert_pushable(branch: str) -> str:
    """Return *branch* when it is a pushable ``agent/*`` ref, else raise.

    This is the **only** function allowed to decide that a branch may be
    pushed, so there is exactly one place to audit for invariant I4: the agent
    never writes ``main`` or any protected branch, and it can only ever write
    ``agent/*``.
    """
    name = str(branch or "").strip()
    if not name:
        raise ProtectedBranchError("分支名不能为空")
    plain = name[len("refs/heads/"):] if name.startswith("refs/heads/") else name
    if plain in PROTECTED_BRANCHES:
        raise ProtectedBranchError(
            f"禁止推送保护分支 {plain!r}（I4：只能推 {AGENT_BRANCH_PREFIX}* 分支并开 PR）"
        )
    if not name.startswith(AGENT_BRANCH_PREFIX):
        raise ProtectedBranchError(
            f"只允许推送 {AGENT_BRANCH_PREFIX}* 前缀分支，得到 {name!r}（I4）"
        )
    if any(ch in _FORBIDDEN_BRANCH_CHARS or ord(ch) < 0x20 for ch in name):
        raise ProtectedBranchError(f"分支名含非法字符：{name!r}")
    if (
        ".." in name
        or "@{" in name
        or "//" in name
        or name.endswith("/")
        or name.endswith(".lock")
    ):
        raise ProtectedBranchError(f"分支名不是合法的 git ref：{name!r}")
    if not _BRANCH_RE.match(name):
        raise ProtectedBranchError(f"分支名不符合 {AGENT_BRANCH_PREFIX}<name> 形式：{name!r}")
    return name


# ── §9.5 result schema ───────────────────────────────────────────────

class RunMeta(BaseModel):
    """The ``run`` object: what was reviewed and with which policy."""

    model_config = ConfigDict(strict=True)

    commit_sha: str
    policy_hash: str
    started_at: str
    finished_at: str | None = None
    task_id: str | None = None
    repo: str | None = None
    policy_source: str = "repo"
    mode: str = "review"


class Evidence(BaseModel):
    """One link from a finding to the history it was checked against (§4.6)."""

    model_config = ConfigDict(strict=True)

    kind: Literal["issue", "pr", "commit"] = "issue"
    number: int | None = None
    relation: Literal["mentions", "duplicate_of", "fixed_by"] = "mentions"
    url: str | None = None
    title: str | None = None


class Finding(BaseModel):
    """One rule hit.  ``rule_id`` is a rule, never free text (§4.2)."""

    model_config = ConfigDict(strict=True)

    rule_id: str
    level: Literal["blocking", "debt"]
    severity: Literal["critical", "high", "medium", "low"]
    file_path: str
    symbol: str
    title: str
    detail: str = ""
    line_hint: int | None = None
    context_key: str = ""
    evidence: list[Evidence] = Field(default_factory=list)
    autofix: bool = False


class GateEntry(BaseModel):
    """One gate inside a result document — the §9.5 subset of §9.4."""

    model_config = ConfigDict(strict=True)

    gate: str
    passed: bool
    exit_code: int | None = None
    stdout_tail: str = ""
    duration_ms: int = 0


class Result(BaseModel):
    """The §9.5 document — the runner's only output contract."""

    model_config = ConfigDict(strict=True)

    run: RunMeta
    findings: list[Finding] = Field(default_factory=list)
    gates: list[GateEntry]


def validate_result(payload: Any) -> Result:
    """Validate a §9.5 document, raising :class:`ResultValidationError`.

    Strict on purpose: a finding that names no ``rule_id`` cannot be
    fingerprinted, and a document with no ``gates`` cannot prove the change is
    accepted.  Both are better as a failed task than as a dirty finding.
    """
    if not isinstance(payload, Mapping):
        raise ResultValidationError("result 必须是 JSON 对象")
    try:
        return Result.model_validate(dict(payload))
    except ValidationError as exc:
        raise ResultValidationError(_describe_validation(exc)) from exc


def _describe_validation(exc: ValidationError) -> str:
    parts: list[str] = []
    for error in exc.errors()[:8]:
        location = ".".join(str(part) for part in error.get("loc", ())) or "<root>"
        parts.append(f"{location}: {error.get('msg')}")
    return "result 校验失败：" + "; ".join(parts)


def gate_entry(result: GateResult) -> GateEntry:
    """Normalise a §9.4 gate result into the §9.5 document shape."""
    return GateEntry(
        gate=result.gate,
        passed=result.passed,
        exit_code=int(result.exit_code),
        stdout_tail=result.stdout_tail,
        duration_ms=int(result.duration_ms),
    )


# ── Work-directory isolation and 24h retention (§9.2) ────────────────

def workdir_for(root: str | Path, task_id: str) -> Path:
    """``<root>/<task_id>``, refusing anything that could escape the root."""
    component = str(task_id).strip()
    if (
        not component
        or component in (".", "..")
        or "/" in component
        or "\\" in component
        or any(ord(ch) < 0x20 for ch in component)
    ):
        raise AgentRunnerError(f"非法任务 id：{task_id!r}")
    return Path(root) / component


def mark_finished(workdir: str | Path) -> Path:
    """Drop the ``.done`` marker that makes a work directory sweepable."""
    marker = Path(workdir) / FINISHED_MARKER
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(utc_now_iso(), encoding="utf-8")
    return marker


def _epoch(now: float | int | None) -> float:
    if now is None:
        return time.time()
    if isinstance(now, (int, float)):
        return float(now)
    timestamp = getattr(now, "timestamp", None)
    if callable(timestamp):
        return float(timestamp())
    raise AgentRunnerError(f"无法理解的时间参数：{now!r}")


def sweep_workdirs(
    now: float | int | None = None,
    root: str | Path | None = None,
    *,
    retention_seconds: int = DEFAULT_RETENTION_SECONDS,
) -> list[Path]:
    """Return the finished work directories past their retention window.

    Pure with respect to the filesystem: it only *reports* candidates and never
    deletes anything (call :func:`remove_workdirs` for that), which is what
    makes the 24h rule cheap to unit-test.  A directory without a ``.done``
    marker is an in-flight task and is never a candidate.
    """
    root_path = Path(root) if root is not None else configured_work_root()
    if not root_path.is_dir():
        return []
    cutoff = _epoch(now) - max(0.0, float(retention_seconds))
    expired: list[Path] = []
    for entry in sorted(root_path.iterdir()):
        if not entry.is_dir():
            continue
        try:
            finished_at = (entry / FINISHED_MARKER).stat().st_mtime
        except OSError:
            continue
        if finished_at <= cutoff:
            expired.append(entry)
    return expired


def remove_workdirs(paths: Iterable[Path]) -> int:
    """Delete the directories :func:`sweep_workdirs` returned; count successes."""
    removed = 0
    for path in paths:
        try:
            shutil.rmtree(path)
            removed += 1
        except OSError as exc:  # a locked directory is retried next sweep
            logger.warning("cannot remove work directory %s: %s", path, exc)
    return removed


# ── Configuration (promote these into config/hub.py, see S4.md) ──────

def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def configured_work_root() -> Path:
    return Path(os.environ.get(ENV_WORK_ROOT) or DEFAULT_WORK_ROOT)


def configured_retention_seconds() -> int:
    return _env_int(ENV_WORK_RETENTION, DEFAULT_RETENTION_SECONDS)


def configured_gate_timeout() -> float:
    return _env_float(ENV_GATE_TIMEOUT, DEFAULT_GATE_TIMEOUT)


def configured_max_findings() -> int:
    return _env_int(ENV_MAX_FINDINGS, DEFAULT_MAX_FINDINGS)


# ── Model credentials: resolve -> env -> subprocess, then gone ───────

def select_route(
    routes: Sequence[Mapping[str, Any]],
    *,
    route: str | None = None,
) -> Mapping[str, Any]:
    """Pick the model route a run should use: by name/model/alias, or the first."""
    enabled = [item for item in routes if item.get("enabled", True)]
    if not enabled:
        raise AgentRunnerError("模型路由表里没有启用的路由")
    if route:
        for item in enabled:
            names = {
                str(item.get("name") or ""),
                str(item.get("model") or ""),
                *[str(alias) for alias in item.get("aliases") or []],
            }
            if route in names:
                return item
        raise AgentRunnerError(f"模型路由表里找不到 {route!r}")
    return enabled[0]


def build_model_env(
    route: Mapping[str, Any],
    *,
    prefix: str = MODEL_ENV_PREFIX,
) -> dict[str, str]:
    """Turn one resolved route into the environment a child process reads.

    The generic ``OPENFISH_MODEL_*`` names are always set; the provider's own
    variable names are set too when there is a convention for them, so a stock
    OpenAI/Anthropic SDK inside the sandbox needs no glue.
    """
    provider = str(route.get("provider") or "openai")
    base_url = str(route.get("base_url") or "")
    env: dict[str, str] = {
        f"{prefix}_PROVIDER": provider,
        f"{prefix}_BASE_URL": base_url,
        f"{prefix}_PATH": str(route.get("path") or ""),
        f"{prefix}_MODEL": str(route.get("model") or ""),
    }
    api_key = str(route.get("api_key") or "")
    if api_key:
        env[f"{prefix}_API_KEY"] = api_key
    provider_names = {
        "openai": ("OPENAI_API_KEY", "OPENAI_BASE_URL"),
        "anthropic": ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL"),
    }
    if provider in provider_names:
        key_name, url_name = provider_names[provider]
        if api_key:
            env[key_name] = api_key
        env[url_name] = base_url
    return env


def resolve_model_env(
    models_file: str | Path,
    *,
    route: str | None = None,
    health_path: str | Path | None = None,
) -> dict[str, str]:
    """Read the route table through ``model_routes.resolve`` and build the env."""
    table = resolve(models_file, health_path=health_path)
    chosen = select_route(table.get("routes") or [], route=route)
    return build_model_env(chosen)


def mask_env(env: Mapping[str, str]) -> dict[str, str]:
    """A loggable copy of an environment block: secret values are masked."""
    masked: dict[str, str] = {}
    for name, value in env.items():
        if any(hint in name.upper() for hint in SECRET_HINTS) and value:
            masked[name] = mask_api_key(value) or "••••"
        else:
            masked[name] = value
    return masked


def secret_values(env: Mapping[str, str]) -> tuple[str, ...]:
    """The raw values that must never appear in a log line or a result."""
    return tuple(
        str(value)
        for name, value in env.items()
        if value and any(hint in name.upper() for hint in SECRET_HINTS)
    )


def mask_secrets(text: str, secrets: Iterable[str]) -> str:
    """Replace every occurrence of a secret in *text* with its masked form."""
    result = text or ""
    for secret in secrets:
        value = str(secret or "")
        if value:
            result = result.replace(value, mask_api_key(value) or "••••")
    return result


def _scrub_payload(value: Any, secrets: Sequence[str]) -> Any:
    """Recursively mask secrets in a JSON-shaped value before it is written."""
    if isinstance(value, str):
        return mask_secrets(value, secrets)
    if isinstance(value, Mapping):
        return {key: _scrub_payload(item, secrets) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_scrub_payload(item, secrets) for item in value]
    return value


class SecretRedactingFilter(logging.Filter):
    """Mask known secrets in every log record emitted while a run is active.

    A second line of defence: the runtime already masks explicitly, but a
    traceback or a subprocess's stderr should not be able to leak a key either.
    """

    def __init__(self, secrets: Iterable[str]) -> None:
        super().__init__()
        self._secrets = tuple(str(secret) for secret in secrets if secret)

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._secrets:
            return True
        try:
            message = record.getMessage()
        except Exception:  # a broken record must not break logging
            return True
        record.msg = mask_secrets(message, self._secrets)
        record.args = ()
        return True


# ── Policy (S3 owns parsing; the runner only needs identity) ─────────

#: sha256 of the read-only policy used when the repository has none (§7.3).
BUILTIN_POLICY_TEXT = "version: 1\nsource: builtin-default\n"


@dataclass(frozen=True)
class PolicyView:
    """What the runner needs to know about a policy: identity and budget.

    Deep policy semantics (rules, exceptions, escalation) belong to S3's
    ``services.review_policy``; the runner carries only what §9.5 puts in the
    result document and passes the view to the review step.
    """

    source: str
    hash: str
    max_findings_per_run: int = DEFAULT_MAX_FINDINGS
    raw: str | None = None
    warnings: tuple[str, ...] = ()


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def builtin_default_policy() -> PolicyView:
    """The read-only default used when ``.agent/review-policy.yml`` is absent."""
    return PolicyView(
        source="builtin-default",
        hash=_sha256_text(BUILTIN_POLICY_TEXT),
        max_findings_per_run=DEFAULT_MAX_FINDINGS,
    )


def load_policy(
    text: str | None,
    *,
    parser: Callable[[str | None], PolicyView] | None = None,
    source: str = "repo",
) -> PolicyView:
    """Build a :class:`PolicyView`; a missing file means builtin-default.

    *parser* is the S3 integration point and **owns the builtin case too**: it
    is called with ``None`` when the repository has no policy file, so S3 can
    supply its own ``builtin_default()`` hash instead of this module inventing a
    second one.  Without a parser the runner still keeps its promise that a
    policy's *identity* is always available (``policy_hash`` on every run).
    """
    if parser is not None:
        try:
            return parser(text)
        except Exception as exc:  # a broken policy must not crash the run
            logger.warning("policy parser failed; keeping hash-only view: %s", exc)
            return PolicyView(
                source=f"{source}-unparsed",
                hash=_sha256_text(text or ""),
                raw=text,
                warnings=(str(exc),),
            )
    if text is None or not text.strip():
        return builtin_default_policy()
    return PolicyView(source=source, hash=_sha256_text(text), raw=text)


@dataclass(frozen=True)
class ReadContext:
    """The two files the ``read`` step loads out of the checkout."""

    agents_md: str | None = None
    policy_text: str | None = None
    policy_path: str | None = None


def wrap_untrusted(text: str | None, *, kind: str = UNTRUSTED_TAG) -> str:
    """Fence imported text as data — delegates to S2's single implementation.

    ``services.repo_context.wrap_untrusted`` neutralises a forged closing tag and
    is what :func:`services.repo_context.search` uses to build a prompt block, so
    reusing it keeps one answer to "imported text is evidence, not orders"
    (§8.4 / I6).  Imported lazily because that module pulls in SQLAlchemy.
    """
    from services.repo_context import wrap_untrusted as fence

    return fence(text or "", tag=kind)


# ── Task and outcome value objects ───────────────────────────────────

@dataclass(frozen=True)
class TaskRequest:
    """One unit of work handed to :meth:`AgentRunner.run`."""

    task_id: str
    repo_url: str
    commit_sha: str
    kind: str = "review"
    base_branch: str = "main"
    branch: str | None = None
    repo: str | None = None
    issue_number: int | None = None
    finding_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class RunOutcome:
    """What a run did, in a shape a worker can persist or log."""

    task_id: str
    status: str
    steps: tuple[str, ...]
    run: dict[str, Any] | None = None
    findings: tuple[dict[str, Any], ...] = ()
    gates: tuple[dict[str, Any], ...] = ()
    result_ref: str | None = None
    pr_url: str | None = None
    error: str | None = None


# ── Task sink (platform-side state; the runner stores nothing) ───────

class TaskSink:
    """Where task state goes.  The default does nothing.

    S0's worker passes a DB-backed sink; the offline gate passes a recording
    one.  The runner only ever *calls* these methods, which is what keeps
    "agent_runner.py owns no state" true.
    """

    def mark_running(self, task_id: str) -> None:
        """A run for *task_id* has started."""

    def mark_failed(self, task_id: str, reason: str) -> None:
        """The task failed and wrote no finding."""

    def record_findings(self, task_id: str, result: Result) -> None:
        """Hand the validated §9.5 document to the platform (→ S3 ingest)."""

    def mark_done(
        self,
        task_id: str,
        *,
        result_ref: str,
        pr_url: str | None = None,
    ) -> None:
        """The task finished and its result document is at *result_ref*."""


class NullSink(TaskSink):
    """A sink that records nothing — for a dry run or a review-only invocation."""


class FindingsSink(TaskSink):
    """Forward validated findings to the platform's ingest; own no task state.

    This is the sink to pair with S0's :class:`services.agent_queue.Worker`: the
    worker already owns the task's ``running → done|failed`` transitions around
    the handler, so the runner must only hand over the §9.5 document (→ S3's
    ingest) and leave status alone.  A worker that drives the runner directly
    (no ``Worker``) can use :class:`DbTaskSink` instead.
    """

    def __init__(self, ingest: Callable[[str, Result], Any] | None = None) -> None:
        self._ingest = ingest

    def record_findings(self, task_id: str, result: Result) -> None:
        if self._ingest is None:
            logger.info(
                "agent task %s produced %d finding(s); ingest not wired",
                task_id,
                len(result.findings),
            )
            return
        self._ingest(task_id, result)


class DbTaskSink(TaskSink):
    """The platform sink: task state in the database, findings to S3's ingest.

    The database import is deferred so this module stays importable in an
    offline gate.  *ingest* is the S3 hand-off (``services.findings.ingest``);
    when it is not wired, the validated document is only logged — the platform
    still owns that call, and the runner must not reimplement dedup.
    """

    def __init__(
        self,
        *,
        ingest: Callable[[str, Result], Any] | None = None,
        session: Any | None = None,
    ) -> None:
        self._ingest = ingest
        self._session = session

    def _db(self) -> Any:
        if self._session is not None:
            return self._session
        from extensions.database import Session  # deferred: no DB to import offline

        return Session

    def _task(self, task_id: str) -> Any:
        from models.agent_hub import AgentTask

        session = self._db()
        try:
            return session.get(AgentTask, int(task_id))
        except (TypeError, ValueError):
            return None

    def mark_running(self, task_id: str) -> None:
        from models.base import utcnow

        session = self._db()
        task = self._task(task_id)
        if task is None:
            return
        task.status = "running"
        task.started_at = task.started_at or utcnow()
        session.commit()

    def mark_failed(self, task_id: str, reason: str) -> None:
        from models.base import utcnow

        session = self._db()
        task = self._task(task_id)
        if task is None:
            return
        task.status = "failed"
        task.finished_at = utcnow()
        session.commit()
        logger.warning("agent task %s failed: %s", task_id, reason)

    def record_findings(self, task_id: str, result: Result) -> None:
        if self._ingest is not None:
            self._ingest(task_id, result)
            return
        logger.info(
            "agent task %s produced %d finding(s); platform ingest not wired",
            task_id,
            len(result.findings),
        )

    def mark_done(
        self,
        task_id: str,
        *,
        result_ref: str,
        pr_url: str | None = None,
    ) -> None:
        from models.base import utcnow

        session = self._db()
        task = self._task(task_id)
        if task is None:
            return
        task.status = "done"
        task.result_ref = result_ref
        task.finished_at = utcnow()
        session.commit()


# ── Runner adapter: every external execution lives behind this ───────

class RunnerAdapter(Protocol):
    """The external world, injectable.

    One method per external effect the six steps need, so a fake that records
    calls can prove the order and so the offline gate never touches git, docker
    or a model.
    """

    def set_model_env(self, env: Mapping[str, str]) -> None:
        """Install (or with an empty mapping, drop) the model credential env."""

    def prepare(self, workdir: Path) -> None:
        """Create the isolated work directory."""

    def clone(self, workdir: Path, *, repo_url: str, commit_sha: str) -> None:
        """Fetch *commit_sha* from *repo_url* into *workdir* (read-only copy)."""

    def read_context(self, workdir: Path) -> ReadContext:
        """Load ``AGENTS.md`` and ``.agent/review-policy.yml`` when present."""

    def run_gates(self, workdir: Path, *, timeout: float) -> GateSummary:
        """Run the gate suite inside the checkout."""

    def review(
        self,
        workdir: Path,
        *,
        policy: PolicyView,
        commit_sha: str,
        context: ReadContext,
    ) -> Sequence[Mapping[str, Any]]:
        """Produce raw finding mappings for the policy."""

    def search(
        self,
        workdir: Path,
        *,
        finding: Mapping[str, Any],
        commit_sha: str,
        context: ReadContext,
    ) -> Mapping[str, Any]:
        """Attach historical-issue evidence to one finding (§8.3)."""

    def emit(self, workdir: Path, *, payload: Mapping[str, Any]) -> str:
        """Write the result document; return its reference."""

    def push(self, workdir: Path, *, branch: str, commit_sha: str) -> None:
        """Push ``HEAD`` to *branch*.  The runner has already checked I4."""

    def open_pr(
        self,
        workdir: Path,
        *,
        branch: str,
        base: str,
        title: str,
        body: str,
    ) -> str:
        """Open the pull request; return its URL."""


def _read_optional(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


class SubprocessRunnerAdapter:
    """The real adapter: ``git`` + subprocess, with model callables injected.

    The two steps that need the platform — the model review and the Forgejo PR
    — are constructor callables, because their implementation belongs to the
    sandbox image and to S1 respectively, not here.  A missing callable raises
    :class:`AgentRunnerError`, so a half-wired deployment fails loudly instead
    of pretending it reviewed something.
    """

    def __init__(
        self,
        *,
        scripts_dir: str | Path | None = None,
        gate_executor: Any | None = None,
        review_fn: Callable[..., Sequence[Mapping[str, Any]]] | None = None,
        search_fn: Callable[..., Mapping[str, Any]] | None = None,
        open_pr_fn: Callable[..., str] | None = None,
        git_binary: str = "git",
    ) -> None:
        self._scripts_dir = Path(scripts_dir) if scripts_dir is not None else None
        self._gate_executor = gate_executor
        self._review_fn = review_fn
        self._search_fn = search_fn
        self._open_pr_fn = open_pr_fn
        self._git_binary = git_binary
        self._model_env: dict[str, str] = {}

    # -- credentials ---------------------------------------------------

    def set_model_env(self, env: Mapping[str, str]) -> None:
        self._model_env = {str(k): str(v) for k, v in (env or {}).items()}

    def _child_env(self) -> dict[str, str]:
        env = dict(os.environ)
        env.update(self._model_env)
        env.setdefault("GIT_TERMINAL_PROMPT", "0")
        return env

    def _run(self, argv: Sequence[str], *, cwd: Path | None = None) -> str:
        import subprocess  # local: keeps the module import graph small

        proc = subprocess.run(
            list(argv),
            cwd=str(cwd) if cwd is not None else None,
            capture_output=True,
            text=True,
            env=self._child_env(),
            check=False,
        )
        if proc.returncode != 0:
            detail = mask_secrets(
                (proc.stderr or proc.stdout or "").strip()[:300],
                self._model_env.values(),
            )
            raise AgentRunnerError(
                f"命令失败（{argv[0]}，exit {proc.returncode}）：{detail}"
            )
        return proc.stdout or ""

    # -- the six steps -------------------------------------------------

    def prepare(self, workdir: Path) -> None:
        Path(workdir).mkdir(parents=True, exist_ok=True)

    def clone(self, workdir: Path, *, repo_url: str, commit_sha: str) -> None:
        target = Path(workdir) / "repo"
        self._run([self._git_binary, "clone", "--quiet", "--no-checkout", repo_url, str(target)])
        self._run([self._git_binary, "-C", str(target), "fetch", "--quiet", "--depth", "1", "origin", commit_sha])
        self._run([self._git_binary, "-C", str(target), "checkout", "--quiet", "--detach", "FETCH_HEAD"])

    def read_context(self, workdir: Path) -> ReadContext:
        root = Path(workdir) / "repo"
        if not root.is_dir():
            root = Path(workdir)
        policy_file = root / POLICY_RELPATH
        policy_text = _read_optional(policy_file)
        return ReadContext(
            agents_md=_read_optional(root / AGENTS_FILENAME),
            policy_text=policy_text,
            policy_path=str(policy_file) if policy_text is not None else None,
        )

    def _checkout_scripts(self, workdir: Path) -> Path | None:
        candidate = Path(workdir) / "repo" / "backend" / "scripts"
        return candidate if candidate.is_dir() else None

    def run_gates(self, workdir: Path, *, timeout: float) -> GateSummary:
        scripts = self._scripts_dir or self._checkout_scripts(workdir)
        executor = self._gate_executor
        if executor is None and scripts is not None:
            executor = SubprocessGateExecutor(cwd=scripts.parent.parent)
        return run_gate_suite(scripts_dir=scripts, executor=executor, timeout=timeout)

    def review(
        self,
        workdir: Path,
        *,
        policy: PolicyView,
        commit_sha: str,
        context: ReadContext,
    ) -> Sequence[Mapping[str, Any]]:
        if self._review_fn is None:
            raise AgentRunnerError("未配置 review 执行器（review_fn）")
        return self._review_fn(
            workdir, policy=policy, commit_sha=commit_sha, context=context
        )

    def search(
        self,
        workdir: Path,
        *,
        finding: Mapping[str, Any],
        commit_sha: str,
        context: ReadContext,
    ) -> Mapping[str, Any]:
        if self._search_fn is None:
            return finding
        return self._search_fn(
            workdir, finding=finding, commit_sha=commit_sha, context=context
        )

    def emit(self, workdir: Path, *, payload: Mapping[str, Any]) -> str:
        path = Path(workdir) / RESULT_FILENAME
        write_json(path, dict(payload))
        return str(path)

    def push(self, workdir: Path, *, branch: str, commit_sha: str) -> None:
        assert_pushable(branch)
        root = Path(workdir) / "repo"
        if not root.is_dir():
            root = Path(workdir)
        self._run([self._git_binary, "-C", str(root), "push", "--quiet", "origin", f"HEAD:{branch}"])

    def open_pr(
        self,
        workdir: Path,
        *,
        branch: str,
        base: str,
        title: str,
        body: str,
    ) -> str:
        if self._open_pr_fn is None:
            raise AgentRunnerError("未配置 PR 创建器（open_pr_fn，需 S1 的 Forgejo 客户端）")
        return self._open_pr_fn(
            workdir, branch=branch, base=base, title=title, body=body
        )


# ── The runner ───────────────────────────────────────────────────────

class AgentRunner:
    """Runs one task through §9.3's six steps.

    A runner is reusable across tasks: per-task state lives in locals, and the
    model credential environment is installed for the duration of a run and
    cleared in ``finally``.
    """

    def __init__(
        self,
        *,
        adapter: RunnerAdapter | None = None,
        sink: TaskSink | None = None,
        work_root: str | Path | None = None,
        models_file: str | Path | None = None,
        model_route: str | None = None,
        health_path: str | Path | None = None,
        model_env: Mapping[str, str] | None = None,
        gate_timeout: float | None = None,
        retention_seconds: int | None = None,
        max_findings: int | None = None,
        policy_parser: Callable[[str | None], PolicyView] | None = None,
    ) -> None:
        self._adapter: RunnerAdapter = adapter or SubprocessRunnerAdapter()
        self._sink: TaskSink = sink or NullSink()
        self._work_root = Path(work_root) if work_root is not None else configured_work_root()
        self._gate_timeout = (
            float(gate_timeout) if gate_timeout is not None else configured_gate_timeout()
        )
        self._retention_seconds = (
            int(retention_seconds)
            if retention_seconds is not None
            else configured_retention_seconds()
        )
        self._max_findings = (
            int(max_findings) if max_findings is not None else configured_max_findings()
        )
        self._policy_parser = policy_parser
        self._model_env: dict[str, str] = self._resolve_model_env(
            models_file, model_route=model_route, health_path=health_path, fallback=model_env
        )
        self._secrets: tuple[str, ...] = secret_values(self._model_env)

    @staticmethod
    def _resolve_model_env(
        models_file: str | Path | None,
        *,
        model_route: str | None,
        health_path: str | Path | None,
        fallback: Mapping[str, str] | None,
    ) -> dict[str, str]:
        if fallback is not None:
            return {str(key): str(value) for key, value in fallback.items()}
        if models_file is None:
            return {}
        try:
            return resolve_model_env(models_file, route=model_route, health_path=health_path)
        except Exception as exc:
            # A broken route table must not fail the task before it starts; the
            # review step will fail loudly if it actually needed a model.
            logger.warning("模型路由解析失败：%s", exc)
            return {}

    # -- accessors used by tests and the worker -------------------------

    @property
    def model_env(self) -> dict[str, str]:
        return dict(self._model_env)

    @property
    def secrets(self) -> tuple[str, ...]:
        return self._secrets

    # -- the lifecycle --------------------------------------------------

    def run(self, task: TaskRequest) -> RunOutcome:
        """Execute one task.  Never raises for a task-level failure."""
        steps: list[str] = []
        workdir = workdir_for(self._work_root, task.task_id)
        redaction = SecretRedactingFilter(self._secrets)
        logger.addFilter(redaction)
        adapter = self._adapter
        adapter.set_model_env(self._model_env)
        self._log_model_env()
        started_at = utc_now_iso()
        pr_url: str | None = None
        try:
            self._sink.mark_running(task.task_id)
            adapter.prepare(workdir)

            steps.append("clone")
            adapter.clone(workdir, repo_url=task.repo_url, commit_sha=task.commit_sha)

            steps.append("read")
            context = adapter.read_context(workdir)
            policy = load_policy(context.policy_text, parser=self._policy_parser)

            steps.append("gates")
            summary = adapter.run_gates(workdir, timeout=self._gate_timeout)
            gates = [gate_entry(item) for item in _gate_items(summary)]

            steps.append("review")
            raw = list(
                adapter.review(
                    workdir, policy=policy, commit_sha=task.commit_sha, context=context
                )
            )[: self._effective_max_findings(policy)]

            steps.append("search")
            enriched = [
                adapter.search(
                    workdir,
                    finding=finding,
                    commit_sha=task.commit_sha,
                    context=context,
                )
                for finding in raw
            ]

            steps.append("emit")
            payload = _scrub_payload(
                {
                    "run": {
                        "commit_sha": task.commit_sha,
                        "policy_hash": policy.hash,
                        "policy_source": policy.source,
                        "mode": task.kind,
                        "task_id": task.task_id,
                        "repo": task.repo,
                        "started_at": started_at,
                        "finished_at": utc_now_iso(),
                    },
                    "findings": [dict(item) for item in enriched],
                    "gates": [item.model_dump() for item in gates],
                },
                self._secrets,
            )

            try:
                result = validate_result(payload)
            except ResultValidationError as exc:
                logger.error(
                    "result 校验失败，任务 %s 标记 failed 且不写 finding：%s",
                    task.task_id,
                    exc,
                )
                self._sink.mark_failed(task.task_id, str(exc))
                return RunOutcome(
                    task_id=task.task_id,
                    status="failed",
                    steps=tuple(steps),
                    gates=tuple(item.model_dump() for item in gates),
                    error=str(exc),
                )

            document = result.model_dump()
            if task.kind == "fix":
                # I4: refuse anything but agent/* before any push is attempted.
                branch = assert_pushable(
                    task.branch or f"{AGENT_BRANCH_PREFIX}fix-{task.task_id}"
                )
                adapter.push(workdir, branch=branch, commit_sha=task.commit_sha)
                pr_url = adapter.open_pr(
                    workdir,
                    branch=branch,
                    base=task.base_branch,
                    title=_pr_title(result),
                    body=self._pr_body(result),
                )

            result_ref = adapter.emit(workdir, payload=document)
            self._sink.record_findings(task.task_id, result)
            self._sink.mark_done(task.task_id, result_ref=result_ref, pr_url=pr_url)
            return RunOutcome(
                task_id=task.task_id,
                status="done",
                steps=tuple(steps),
                run=document["run"],
                findings=tuple(document["findings"]),
                gates=tuple(document["gates"]),
                result_ref=result_ref,
                pr_url=pr_url,
            )
        except AgentRunnerError as exc:
            logger.error("agent 任务 %s 失败：%s", task.task_id, exc)
            self._sink.mark_failed(task.task_id, str(exc))
            return RunOutcome(
                task_id=task.task_id,
                status="failed",
                steps=tuple(steps),
                error=str(exc),
            )
        except Exception as exc:  # fail closed: an unexpected error is a failed task
            safe = mask_secrets(f"{exc.__class__.__name__}: {exc}", self._secrets)
            logger.exception("agent 任务 %s 异常终止", task.task_id)
            self._sink.mark_failed(task.task_id, safe)
            return RunOutcome(
                task_id=task.task_id,
                status="failed",
                steps=tuple(steps),
                error=safe,
            )
        finally:
            adapter.set_model_env({})
            try:
                mark_finished(workdir)
            except OSError as exc:
                logger.warning("cannot mark work directory %s finished: %s", workdir, exc)
            logger.removeFilter(redaction)

    def _effective_max_findings(self, policy: PolicyView) -> int:
        budget = policy.max_findings_per_run or self._max_findings
        return max(1, min(int(budget), self._max_findings))

    def _log_model_env(self) -> None:
        if not self._model_env:
            logger.info("agent 运行时未配置模型路由")
            return
        logger.info("agent 运行时模型配置（已掩码）：%s", mask_env(self._model_env))

    def _pr_body(self, result: Result) -> str:
        lines = ["## 智能体发现", ""]
        if not result.findings:
            lines.append("本次 review 未产出 finding。")
        for finding in result.findings:
            lines.append(
                f"- [{finding.level}/{finding.severity}] "
                f"`{finding.file_path}` — {finding.title}（`{finding.rule_id}`）"
            )
        lines.extend(["", render_summary([g.model_dump() for g in result.gates])])
        return "\n".join(lines)

    def sweep(self, now: float | int | None = None) -> int:
        """Apply the 24h rule to this runner's work root; return removed count."""
        return remove_workdirs(
            sweep_workdirs(now, self._work_root, retention_seconds=self._retention_seconds)
        )


def _gate_items(summary: Any) -> list[GateResult]:
    """Accept a :class:`GateSummary`, a plain sequence, or a duck-typed object."""
    if isinstance(summary, GateSummary):
        return list(summary.gates)
    items: list[GateResult] = []
    for item in summary or []:
        items.append(item if isinstance(item, GateResult) else GateResult.model_validate(dict(item)))
    return items


def _pr_title(result: Result) -> str:
    count = len(result.findings)
    if count == 1:
        return f"fix: {result.findings[0].title}"
    return f"fix: 智能体修复 {count} 项发现"


def run_task(
    task: TaskRequest,
    *,
    adapter: RunnerAdapter | None = None,
    sink: TaskSink | None = None,
    **kwargs: Any,
) -> RunOutcome:
    """One-shot convenience wrapper around :class:`AgentRunner`."""
    return AgentRunner(adapter=adapter, sink=sink, **kwargs).run(task)


__all__ = [
    "AGENT_BRANCH_PREFIX",
    "AGENTS_FILENAME",
    "BUILTIN_POLICY_TEXT",
    "DEFAULT_MAX_FINDINGS",
    "DEFAULT_RETENTION_SECONDS",
    "DEFAULT_WORK_ROOT",
    "FINISHED_MARKER",
    "MODEL_ENV_PREFIX",
    "POLICY_RELPATH",
    "PROTECTED_BRANCHES",
    "RESULT_FILENAME",
    "STEPS",
    "UNTRUSTED_TAG",
    "AgentRunner",
    "AgentRunnerError",
    "DbTaskSink",
    "Evidence",
    "Finding",
    "FindingsSink",
    "GateEntry",
    "NullSink",
    "PolicyView",
    "ProtectedBranchError",
    "ReadContext",
    "Result",
    "ResultValidationError",
    "RunMeta",
    "RunOutcome",
    "RunnerAdapter",
    "SecretRedactingFilter",
    "SubprocessRunnerAdapter",
    "TaskRequest",
    "TaskSink",
    "assert_pushable",
    "build_model_env",
    "builtin_default_policy",
    "configured_gate_timeout",
    "configured_max_findings",
    "configured_retention_seconds",
    "configured_work_root",
    "gate_entry",
    "load_policy",
    "mark_finished",
    "mask_env",
    "mask_secrets",
    "remove_workdirs",
    "resolve_model_env",
    "run_task",
    "secret_values",
    "select_route",
    "sweep_workdirs",
    "validate_result",
    "workdir_for",
    "wrap_untrusted",
]
