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
import stat
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Literal, Mapping, Protocol, Sequence

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from config import settings
from config.agent import (
    DEFAULT_MAX_FINDINGS,
    DEFAULT_RETENTION_SECONDS,
    DEFAULT_WORK_ROOT,
)
from services.check_curator import CuratorReport, proposal_body, proposal_title
from services.check_suite import (
    CHECK_DIR_RELPATH,
    FrozenSuite,
    curator_guard_violations,
    fix_guard_violations,
    freeze_suite,
    resolve_ai_suite,
    resolve_suite,
    resolve_suites,
    suite_protected_paths,
)
from services.check_trust import (
    Assurance,
    assess_assurance,
    render_two_suites,
)
from services.digest import sha256_text
from services.fileio import write_json
from services.format import utc_now_iso
from services.gates import (
    SOURCE_AGENT_CHECKS,
    STATUS_UNVERIFIED,
    CheckSuite,
    GateResult,
    GateSummary,
    render_summary,
    run_suite as run_check_suite,
    suite_fingerprint,
    suite_from_scripts,
    unverified_summary,
)
from services.git_auth import credential_args, git_env
from services.model_routes import mask_api_key, resolve
from services.sandbox_env import sandbox_env
from services.sandbox_identity import (
    SANDBOX_HOME_DIRNAME,
    SandboxIdentityError,
    describe_identity,
    prepare_untrusted_workdir,
)

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

#: Values ``pr_policy`` accepts.  Deliberately duplicated (not imported) from
#: ``services.review_policy``: the runner must stay importable in the offline
#: gate without a policy file, and these are the runner's own contract.
PR_POLICY_ON_GREEN = "on_green"
PR_POLICY_ALWAYS = "always"
PR_POLICY_NEVER = "never"
PR_POLICIES: tuple[str, ...] = (PR_POLICY_ON_GREEN, PR_POLICY_ALWAYS, PR_POLICY_NEVER)
DEFAULT_PR_POLICY = PR_POLICY_ON_GREEN

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
            f"禁止推送保护分支 {plain}（I4：只能推 {AGENT_BRANCH_PREFIX}* 分支并开 PR）"
        )
    if not name.startswith(AGENT_BRANCH_PREFIX):
        raise ProtectedBranchError(
            f"只允许推送 {AGENT_BRANCH_PREFIX}* 前缀分支，得到 {name}（I4）"
        )
    if any(ch in _FORBIDDEN_BRANCH_CHARS or ord(ch) < 0x20 for ch in name):
        raise ProtectedBranchError(f"分支名含非法字符：{name}")
    if (
        ".." in name
        or "@{" in name
        or "//" in name
        or name.endswith("/")
        or name.endswith(".lock")
    ):
        raise ProtectedBranchError(f"分支名不是合法的 git ref：{name}")
    if not _BRANCH_RE.match(name):
        raise ProtectedBranchError(f"分支名不符合 {AGENT_BRANCH_PREFIX}<name> 形式：{name}")
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
    #: Provenance of the *frozen* verification suite (P1.1/P1.3).  Recorded so a
    #: reader can tell which suite judged the change and whether it was resolved
    #: at all — an unverified run must never look like a passed one.  These
    #: describe the **repository (user)** suite; the AI suite is reported
    #: separately in ``ai_suite_*`` and ``ai_gates`` so the two are never merged
    #: into one anonymous green list.
    suite_hash: str = ""
    suite_source: str = ""
    suite_state: str = ""
    ai_suite_hash: str = ""
    ai_suite_source: str = ""
    ai_suite_state: str = ""
    #: The trust-ladder rung and its PR label (L0–L3).  L3 is off by default.
    assurance_level: str = ""
    assurance_label: str = ""


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
    #: ``passed`` / ``failed`` / ``unverified``.  The result document is the PR
    #: description's source, so the third state has to survive serialization.
    status: str = "passed"
    exit_code: int | None = None
    stdout_tail: str = ""
    duration_ms: int = 0


class Result(BaseModel):
    """The §9.5 document — the runner's only output contract."""

    model_config = ConfigDict(strict=True)

    run: RunMeta
    findings: list[Finding] = Field(default_factory=list)
    gates: list[GateEntry]
    #: The AI-maintained suite's results, kept separate from ``gates`` (the
    #: repository/user authority) on purpose — see ``services.check_trust``.
    ai_gates: list[GateEntry] = Field(default_factory=list)
    #: Present only for a ``checks`` task: what the curator proposed and what the
    #: falsifiability validator proved (``services.check_curator``).
    curator: dict[str, Any] | None = None


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
        status=result.status,
        exit_code=int(result.exit_code),
        stdout_tail=result.stdout_tail,
        duration_ms=int(result.duration_ms),
    )


# ── Work-directory isolation and 24h retention (§9.2) ────────────────

def workdir_for(root: str | Path, task_id: str, attempt: int = 0) -> Path:
    """``<root>/<task_id>``, refusing anything that could escape the root.

    *attempt* > 0 appends ``-attempt<n>`` so a reclaimed-and-retried task cannot
    delete the checkout a still-running first attempt is using (the lease is
    advisory once it expires; see ``AgentRunner.run``'s publish guard).
    """
    component = str(task_id).strip()
    if (
        not component
        or component in (".", "..")
        or "/" in component
        or "\\" in component
        or any(ord(ch) < 0x20 for ch in component)
    ):
        raise AgentRunnerError(f"非法任务 id：{task_id}")
    if int(attempt) > 0:
        component = f"{component}-attempt{int(attempt)}"
    return Path(root) / component


def checkout_root(workdir: str | Path) -> Path:
    """Where the repository actually is inside a work directory.

    The adapter clones into ``<workdir>/repo``; a fake adapter (and some
    deployments) put the tree directly in ``<workdir>``.  One implementation so
    the runner and the adapter cannot disagree about which directory is the
    checkout — that disagreement is exactly how the old gate resolution ended up
    running the runner image's own ``backend/scripts`` against a foreign repo.
    """
    nested = Path(workdir) / "repo"
    return nested if nested.is_dir() else Path(workdir)


def _no_follow_lstat(path: Path) -> os.stat_result | None:
    """``os.lstat`` *path*, or ``None`` when it is a symlink or unreadable.

    The checkout is group-writable by the sandbox uid, so repository code can
    replace any entry with a symlink; a worker ``chmod`` that followed one would
    hand the sandbox write access to a worker-owned file outside the checkout
    (the invariant stated in :mod:`services.sandbox_identity`).  ``lstat`` never
    follows the final component, so a link is skipped — not followed and not a
    failure — and an unreadable entry is a debug log, never a raised error.
    Linux ``os.chmod`` has no ``follow_symlinks`` support (it raises
    ``NotImplementedError``), so this ``lstat`` is the guard, exactly as in
    :func:`services.sandbox_identity._relax`.
    """
    try:
        st = os.lstat(path)
    except OSError as exc:
        logger.debug("cannot lstat %s: %s", path, exc)
        return None
    if stat.S_ISLNK(st.st_mode):
        logger.debug("skipping symlink %s: refusing to chmod through it", path)
        return None
    return st


def mark_finished(workdir: str | Path) -> Path:
    """Drop the ``.done`` marker that makes a work directory sweepable.

    Opened with ``O_NOFOLLOW``: the work directory is group-writable by the
    sandbox uid, so a check could plant ``.done`` as a symlink and turn this
    worker-owned write into a clobber of the link's target.  A planted symlink
    fails with ``ELOOP``/``ENOTDIR``, which the caller already logs and swallows.
    """
    marker = Path(workdir) / FINISHED_MARKER
    marker.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(
        marker, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o644,
    )
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(utc_now_iso())
    return marker


def _epoch(now: float | int | None) -> float:
    if now is None:
        return time.time()
    if isinstance(now, (int, float)):
        return float(now)
    timestamp = getattr(now, "timestamp", None)
    if callable(timestamp):
        return float(timestamp())
    raise AgentRunnerError(f"无法理解的时间参数：{now}")


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


# ── Configuration (owned by config/agent.py) ─────────────────────────

def configured_work_root() -> Path:
    """The configured ``AGENT_WORK_ROOT`` — where each task gets its checkout."""
    return Path(settings.agent.work_root)


def configured_retention_seconds() -> int:
    """How long a finished work directory is kept before :func:`sweep_workdirs`."""
    return int(settings.agent.work_retention_seconds)


def configured_gate_timeout() -> float:
    """The per-gate wall-clock budget, in seconds."""
    return float(settings.agent.gate_timeout)


def configured_max_findings() -> int:
    """The deployment-wide ceiling on findings per run."""
    return int(settings.agent.max_findings)


# ── Result-gated PR policy (§9.3 step 6) ─────────────────────────────

def normalize_pr_policy(value: Any) -> str | None:
    """Map *value* onto a known policy name; ``None`` when unset or unknown."""
    name = str(value or "").strip().lower()
    if not name:
        return None
    if name not in PR_POLICIES:
        logger.warning(
            "pr_policy %r 不是 %s 之一，忽略并使用默认值 %s",
            value, "/".join(PR_POLICIES), DEFAULT_PR_POLICY,
        )
        return None
    return name


def configured_pr_policy() -> str | None:
    """The deployment-wide ``AGENT_PR_POLICY``, when it names a known policy.

    ``None`` (the default) means the setting is unset — the per-repository
    ``.agent/review-policy.yml`` then decides, which is what a mixed deployment
    wants.  An unrecognised value is logged and treated as unset by
    :func:`normalize_pr_policy`, never guessed at.
    """
    return normalize_pr_policy(settings.agent.pr_policy)


def configured_auto_fix() -> bool | None:
    """``AGENT_AUTO_FIX`` as an optional boolean; ``None`` when unset."""
    return settings.agent.auto_fix


def _failed_gate_names(gates: Sequence[GateEntry]) -> list[str]:
    """The gates that did not pass — the concrete reason a PR is withheld.

    ``unverified`` entries are deliberately excluded: "nothing checked this" is
    not a red check and is handled by its own branch, because the two need
    different messages in the PR/log.
    """
    return [
        entry.gate for entry in gates
        if not entry.passed and str(getattr(entry, "status", "") or "") != STATUS_UNVERIFIED
    ]



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
        raise AgentRunnerError(f"模型路由表里找不到 {route}")
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
    session: Any,
    *,
    route: str | None = None,
) -> dict[str, str]:
    """Read the route table through ``model_routes.resolve`` and build the env.

    *session* is a SQLAlchemy session (or the app's ``scoped_session``) — the
    route table is the ``model_routes`` table, so a caller that has no database
    has no route table either and should pass a pre-resolved ``model_env`` to
    :class:`AgentRunner` instead of calling this.
    """
    table = resolve(session)
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
    #: Result-gated PR policy from the repository's policy file.  The runner
    #: treats an unknown value as the default rather than trusting it.
    pr_policy: str = DEFAULT_PR_POLICY
    #: §6.4: may a *review* task escalate into a fix+PR?  Default false.
    auto_fix: bool = False
    #: §7.2 extension: inline verification commands from ``checks:``.  Passed to
    #: ``services.check_suite.resolve_suite`` as the second resolution provider.
    checks: tuple[Mapping[str, Any], ...] = ()
    #: DESIGN-ai-checks.md §B: curator trigger mode (``off|bootstrap|auto``) and
    #: its cooling-off window.  Carried on the view so the runner can gate a
    #: ``checks`` task on the repository's own policy.
    curator: str = "bootstrap"
    curator_min_interval_seconds: int = 3600


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
    #: Retry counter from the queue row (0 on the first attempt).  It only
    #: isolates the work directory and the published branch, so a retry never
    #: collides with a previous attempt's checkout or ref.
    attempt: int = 0


@dataclass(frozen=True)
class RunOutcome:
    """What a run did, in a shape a worker can persist or log."""

    task_id: str
    status: str
    steps: tuple[str, ...]
    run: dict[str, Any] | None = None
    findings: tuple[dict[str, Any], ...] = ()
    gates: tuple[dict[str, Any], ...] = ()
    #: The AI-maintained suite's results, separate from ``gates`` (authority).
    ai_gates: tuple[dict[str, Any], ...] = ()
    #: A ``checks`` task's curator report (proposal + falsifiability evidence).
    curator: dict[str, Any] | None = None
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

    ``manage_status=False`` is the mode the queue worker uses: the runner still
    validates and hands over findings, but ``services.agent_queue.Worker`` owns
    the ``running → done|failed`` transitions.  That keeps retry/back-off and
    dead-lettering authoritative — a runner that wrote ``failed`` itself would
    bypass them.
    """

    def __init__(
        self,
        *,
        ingest: Callable[[str, Result], Any] | None = None,
        session: Any | None = None,
        manage_status: bool = True,
    ) -> None:
        self._ingest = ingest
        self._session = session
        self._manage_status = bool(manage_status)

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

        if not self._manage_status:
            return
        session = self._db()
        task = self._task(task_id)
        if task is None:
            return
        task.status = "running"
        task.started_at = task.started_at or utcnow()
        session.commit()

    def mark_failed(self, task_id: str, reason: str) -> None:
        from models.base import utcnow

        logger.warning("agent task %s failed: %s", task_id, reason)
        if not self._manage_status:
            return
        session = self._db()
        task = self._task(task_id)
        if task is None:
            return
        task.status = "failed"
        task.finished_at = utcnow()
        session.commit()

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

        if not self._manage_status:
            # Status, result_ref and the PR link all belong to the queue Worker
            # here: this sink's session may not even be bound in a Flask-free
            # worker process (see ``agent_worker._record_pr_link``).
            return
        session = self._db()
        task = self._task(task_id)
        if task is None:
            return
        task.status = "done"
        task.result_ref = result_ref
        if pr_url:
            task.pr_url = pr_url
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

    def run_gates(
        self,
        workdir: Path,
        *,
        timeout: float,
        suite: CheckSuite | None = None,
    ) -> GateSummary:
        """Run the **repository (user)** suite inside the checkout.

        *suite* is the **frozen** descriptor the runner resolved from the base
        snapshot.  An adapter must run exactly that descriptor — re-resolving
        from the (possibly already modified) working tree is how a fix would get
        to grade its own homework.
        """

    def run_ai_gates(
        self,
        workdir: Path,
        *,
        timeout: float,
        suite: CheckSuite | None = None,
    ) -> GateSummary:
        """Run the **AI-maintained** suite, reported separately from ``gates``.

        Optional for compatibility: an adapter without it contributes an
        explicit ``unverified`` AI block rather than silently pretending the AI
        suite passed.
        """

    def curate(self, workdir: Path, *, timeout: float) -> Mapping[str, Any]:
        """Validate a ``checks`` proposal and return the curator report.

        Optional as well: a ``checks`` run without it still commits/pushes the
        proposal, but nothing has been proven, so every proposed check stays
        ``unvalidated`` (the safe direction).
        """

    def changed_paths(self, workdir: Path) -> Sequence[str]:
        """Repository-relative paths this run has modified so far.

        The path guard needs the fix's edit set *before* it is committed; an
        adapter that cannot enumerate it fails the run closed rather than
        guessing that nothing protected was touched.
        """

    def review(
        self,
        workdir: Path,
        *,
        policy: PolicyView,
        commit_sha: str,
        context: ReadContext,
        kind: str = "review",
    ) -> Sequence[Mapping[str, Any]]:
        """Produce raw finding mappings for the policy.

        *kind* is the task's role (``review`` / ``fix`` / ``checks``) and is how
        the headless command learns whether it is expected to edit files (a
        fixer) or must leave the tree alone (a reviewer) — see
        ``OPENFISH_TASK_KIND`` in :mod:`services.agent_worker`.
        """

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

    def push(
        self,
        workdir: Path,
        *,
        branch: str,
        commit_sha: str,
        repo_url: str,
    ) -> None:
        """Push ``HEAD`` to *branch*, authenticating only against *repo_url*.

        The destination is an explicit argument rather than the checkout's
        ``origin``: untrusted code runs in the checkout before this step and can
        rewrite ``remote.origin.url``.  The runner has already checked I4.
        """

    def commit(
        self,
        workdir: Path,
        *,
        branch: str,
        commit_sha: str,
        message: str,
    ) -> bool:
        """Stage and commit the review's edits; false when there is nothing to push."""

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
        git_token: str | None = None,
        git_host: str | None = None,
    ) -> None:
        self._scripts_dir = Path(scripts_dir) if scripts_dir is not None else None
        self._gate_executor = gate_executor
        self._review_fn = review_fn
        self._search_fn = search_fn
        self._open_pr_fn = open_pr_fn
        self._git_binary = git_binary
        self._model_env: dict[str, str] = {}
        #: The narrow, revocable credential git uses for clone/push.  Passed in
        #: explicitly (``FORGEJO_RUNNER_TOKEN``) rather than read from the
        #: ambient environment, so it can never silently be the admin token.
        self._git_token = (git_token or "").strip()
        #: The only host that credential may be presented to (see git_auth).
        self._git_host = (git_host or "").strip()
        #: ``.git/config`` digest recorded right after clone; a push refuses to
        #: run when the checkout has since rewritten it (``url.*.insteadOf``,
        #: a planted ``credential.helper``, ``http.extraHeader`` …).
        self._config_digest: str | None = None

    # -- credentials ---------------------------------------------------

    def secrets(self) -> tuple[str, ...]:
        """The credentials this adapter holds, so the runner can redact them."""
        return (self._git_token,) if self._git_token else ()

    def set_model_env(self, env: Mapping[str, str]) -> None:
        self._model_env = {str(k): str(v) for k, v in (env or {}).items()}

    def _child_env(self) -> dict[str, str]:
        """The environment a ``git`` subprocess runs under.

        Git needs an allowlist of plumbing (PATH/HOME/locale), not the worker's
        environment: the platform's signing key, Forgejo admin token and
        git-identity key must never reach a child process that runs inside a
        checkout.  The model credential is deliberately absent too — it belongs
        to the review command (see ``agent_worker.build_review_fn``), and git has
        no use for it.  The one authority git does receive is its own scoped
        token, under ``OPENFISH_GIT_TOKEN``, pinned to its own host.
        """
        extra = {"GIT_TERMINAL_PROMPT": "0"}
        extra.update(git_env(self._git_token, self._git_host))
        return sandbox_env(extra=extra)

    def _run(self, argv: Sequence[str], *, cwd: Path | None = None) -> str:
        import subprocess  # local: keeps the module import graph small

        parts = list(argv)
        if self._git_token and parts and parts[0] == self._git_binary:
            # ``-c credential.helper=…`` carries no secret; the token is in the
            # environment (``_child_env``), never in argv.
            parts[1:1] = credential_args()
        proc = subprocess.run(
            parts,
            cwd=str(cwd) if cwd is not None else None,
            capture_output=True,
            text=True,
            env=self._child_env(),
            check=False,
        )
        if proc.returncode != 0:
            detail = mask_secrets(
                (proc.stderr or proc.stdout or "").strip()[:300],
                (*self._model_env.values(), self._git_token),
            )
            raise AgentRunnerError(
                f"命令失败（{argv[0]}，exit {proc.returncode}）：{detail}"
            )
        return proc.stdout or ""

    # -- the six steps -------------------------------------------------

    def prepare(self, workdir: Path) -> None:
        root = Path(workdir)
        root.mkdir(parents=True, exist_ok=True)
        # A reclaimed task (or an operator retry) reuses ``/work/<task_id>``: the
        # task id is stable per row, so the same directory comes back.  The
        # partial checkout a dead attempt left behind would make ``git clone``
        # refuse the target ("destination path already exists"), so clear it.
        # Safe because the queue lease guarantees no other replica is inside this
        # task directory; only ``repo/`` is removed, so the previous attempt's
        # ``result.json`` and logs survive for triage.
        checkout = root / "repo"
        if checkout.exists():
            shutil.rmtree(checkout, ignore_errors=True)

    def clone(self, workdir: Path, *, repo_url: str, commit_sha: str) -> None:
        target = Path(workdir) / "repo"
        # Blobless and tagless: the review needs the tree at one commit, not the
        # whole history, and a targeted SHA fetch is the shallow part.
        self._run([
            self._git_binary, "clone", "--quiet", "--no-checkout",
            "--filter=blob:none", "--no-tags", repo_url, str(target),
        ])
        if not str(commit_sha or "").strip():
            # No target sha (a fix task whose payload names no commit): the tip
            # of the default branch is what a human would review.  FETCH_HEAD is
            # that tip after a shallow fetch, so no extra API round-trip.
            self._run([
                self._git_binary, "-C", str(target), "fetch", "--quiet", "--depth", "1", "origin",
            ])
            self._run([
                self._git_binary, "-C", str(target),
                "checkout", "--quiet", "--detach", "FETCH_HEAD",
            ])
        else:
            try:
                self._run([
                    self._git_binary, "-C", str(target), "fetch", "--quiet",
                    "--depth", "1", "origin", commit_sha,
                ])
                self._run([
                    self._git_binary, "-C", str(target),
                    "checkout", "--quiet", "--detach", "FETCH_HEAD",
                ])
            except AgentRunnerError:
                # A forge that refuses to serve an arbitrary SHA (no
                # ``uploadpack.allowReachableSHA1InWant``) still serves the default
                # branch tip, which is the commit a push webhook reviews.
                self._run([
                    self._git_binary, "-C", str(target),
                    "fetch", "--quiet", "--depth", "1", "origin",
                ])
                self._run([
                    self._git_binary, "-C", str(target),
                    "checkout", "--quiet", "--detach", commit_sha,
                ])
        # The pristine config is the reference ``push`` checks against: the steps
        # in between run repository-supplied code inside this checkout.
        self._config_digest = self._config_hash(target)

    def read_context(self, workdir: Path) -> ReadContext:
        root = checkout_root(workdir)
        policy_file = root / POLICY_RELPATH
        policy_text = _read_optional(policy_file)
        return ReadContext(
            agents_md=_read_optional(root / AGENTS_FILENAME),
            policy_text=policy_text,
            policy_path=str(policy_file) if policy_text is not None else None,
        )

    def run_gates(
        self,
        workdir: Path,
        *,
        timeout: float,
        suite: CheckSuite | None = None,
    ) -> GateSummary:
        """Run the frozen *suite*, or resolve one **from the checkout**.

        The fallback is the whole point of P1.0: when no suite is handed in, the
        resolution starts at the cloned repository (``.agent/checks/`` → policy
        ``checks:`` → manifests → ``unverified``) and can only ever return
        ``unverified``.  It must never reach for this module's own
        ``REPO_ROOT/scripts`` — that is the runner *image's* openfish gates, and
        running them against a foreign repo with ``cwd=<checkout>`` is a false
        green that ``pr_policy=on_green`` would happily push.
        """
        root = checkout_root(workdir)
        if suite is None:
            suite = self._resolved_default_suite(root)
        logger.info(
            "运行校验套件：source=%s checks=%d sha=%s",
            suite.source, len(suite.checks), suite_fingerprint(suite)[:12],
        )
        return run_check_suite(
            suite,
            root=root,
            executor=self._gate_executor,
            timeout=timeout,
        )

    def _resolved_default_suite(self, root: Path) -> CheckSuite:
        """The suite for *root*, without the runner's own scripts unless asked.

        An explicit ``scripts_dir`` constructor argument is an operator override
        (openfish checking itself) and is honoured; nothing else may point the
        resolution at :data:`services.gates.REPO_ROOT`.
        """
        if self._scripts_dir is not None:
            return suite_from_scripts(self._scripts_dir, root=root)
        return resolve_suite(root)

    def run_ai_gates(
        self,
        workdir: Path,
        *,
        timeout: float,
        suite: CheckSuite | None = None,
    ) -> GateSummary:
        """Run the AI-maintained suite (``.agent/checks/**``), separately.

        Never falls back to the repository's own scripts: the AI suite is its
        own namespace, and an empty one is an explicit ``unverified`` block
        rather than a green one.
        """
        root = checkout_root(workdir)
        if suite is None:
            suite = resolve_ai_suite(root)
        return run_check_suite(
            suite,
            root=root,
            executor=self._gate_executor,
            timeout=timeout,
        )

    def curate(self, workdir: Path, *, timeout: float) -> Mapping[str, Any]:
        """Validate the suite a ``checks`` proposal just wrote (see check_curator).

        Optional protocol method: the runner calls it only for a ``checks`` task
        and degrades to "no report" when an adapter does not implement it.
        """
        from services.check_curator import curate_workspace

        report = curate_workspace(
            checkout_root(workdir),
            executor=self._gate_executor,
            timeout=timeout,
        )
        return report.as_payload()

    def changed_paths(self, workdir: Path) -> Sequence[str]:
        """``git status --porcelain`` names, relative to the checkout.

        Transient build artifacts (bytecode and tool caches) are filtered out:
        running a check leaves a ``__pycache__`` behind, and reporting that as
        "the agent edited this" would make the path guard fire on a check that
        simply executed.  Real source and suite edits are never transient.

        ``.git/config`` came from the checkout, which untrusted code has been
        writing since the clone; ``git status`` executes a ``core.fsmonitor``
        program named there, and that child would inherit the worker's
        ``OPENFISH_GIT_TOKEN``.  The post-clone digest is therefore re-asserted
        first, and the repository-supplied hook is disabled explicitly.
        """
        root = checkout_root(workdir)
        self._assert_config_untouched(root)
        output = self._run([
            self._git_binary, "-C", str(root),
            "-c", "core.fsmonitor=false",
            "status", "--porcelain", "--untracked-files=all",
        ])
        return [
            name for name in (line[3:].strip() for line in output.splitlines())
            if name and not _is_transient_path(name)
        ]

    def protect_suite(self, workdir: Path, *, readonly: bool) -> None:
        """Best-effort ``chmod`` of ``.agent/checks/**`` (defence in depth).

        The authoritative guard is :func:`services.check_suite.fix_guard_violations`
        applied to :meth:`changed_paths`, which works even when the sandbox runs
        as root; this just makes an opportunistic write fail early.

        Symlinks are never followed.  The checkout is group-writable by the
        sandbox uid, so repository code can replace any entry with a link —
        ``.agent/checks`` itself included, which is why that directory is
        ``os.lstat``-ed and refused when it is a link — and a worker ``chmod``
        following one would hand the sandbox write access to a worker-owned file
        outside the checkout (the invariant stated in
        :mod:`services.sandbox_identity`).  ``os.walk(..., followlinks=False)``
        mirrors :func:`services.sandbox_identity._relax_tree`: a symlinked
        directory is listed but never descended into, and
        :func:`_no_follow_lstat` also skips it as a chmod target.
        """
        directory = checkout_root(workdir) / CHECK_DIR_RELPATH
        directory_stat = _no_follow_lstat(directory)
        if directory_stat is None or not stat.S_ISDIR(directory_stat.st_mode):
            return
        mode = 0o555 if readonly else 0o755
        file_mode = 0o444 if readonly else 0o644
        # ``topdown=False`` yields children before their directory, matching the
        # deepest-first order of the old ``sorted(rglob("*"), reverse=True)``.
        for dirpath, dirnames, filenames in os.walk(
            directory, topdown=False, followlinks=False,
            onerror=lambda exc: logger.debug("cannot walk %s: %s", directory, exc),
        ):
            parent = Path(dirpath)
            for name in (*dirnames, *filenames):
                path = parent / name
                entry = _no_follow_lstat(path)
                if entry is None:
                    continue
                target_mode = mode if stat.S_ISDIR(entry.st_mode) else file_mode
                try:
                    os.chmod(path, target_mode)
                except OSError as exc:
                    logger.debug("cannot chmod %s: %s", path, exc)
        try:
            os.chmod(directory, mode)
        except OSError as exc:
            logger.debug("cannot chmod %s: %s", directory, exc)

    def protect_paths(
        self,
        workdir: Path,
        paths: Sequence[str],
        *,
        readonly: bool,
    ) -> None:
        """Best-effort ``chmod`` of specific repo-relative files.

        The authoritative guard is :func:`services.check_suite.fix_guard_violations`
        the resolved suite; this locks the suite's *base-revision* entry
        scripts and declaration files for the duration of a fix, so an
        opportunistic write fails early instead of relying on the post-hoc guard.

        Symlinks are never followed.  *paths* is repository-controlled, so each
        ``root / relative`` is re-anchored under its resolved parent — a link or
        ``..`` in any directory component therefore cannot escape the checkout —
        and its final component is ``lstat``-ed: a repository-planted link is
        skipped rather than chmod'ed, so the worker never hands the sandbox write
        access to a file outside the checkout (the invariant stated in
        :mod:`services.sandbox_identity`).
        """
        root = checkout_root(workdir)
        root_real = Path(os.path.realpath(root))
        file_mode = 0o444 if readonly else 0o644
        for relative in paths:
            if not relative or Path(relative).is_absolute():
                logger.debug("skipping non-relative protected path %r", relative)
                continue
            path = root_real / relative
            # Resolve the *parent* only: the final component must stay
            # un-followed for ``_no_follow_lstat``.
            parent_real = Path(os.path.realpath(path.parent))
            if parent_real != root_real and root_real not in parent_real.parents:
                logger.debug("skipping %s: parent resolves outside %s", path, root_real)
                continue
            path = parent_real / path.name
            entry = _no_follow_lstat(path)
            if entry is None or not stat.S_ISREG(entry.st_mode):
                continue
            try:
                os.chmod(path, file_mode)
            except OSError as exc:
                logger.debug("cannot chmod %s: %s", path, exc)

    def review(
        self,
        workdir: Path,
        *,
        policy: PolicyView,
        commit_sha: str,
        context: ReadContext,
        kind: str = "review",
    ) -> Sequence[Mapping[str, Any]]:
        if self._review_fn is None:
            raise AgentRunnerError("未配置 review 执行器（review_fn）")
        return self._review_fn(
            workdir, policy=policy, commit_sha=commit_sha, context=context, kind=kind
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

    def commit(
        self,
        workdir: Path,
        *,
        branch: str,
        commit_sha: str,
        message: str,
    ) -> bool:
        """Stage the review's edits, commit them, and refuse an empty branch.

        Without this step ``push`` would push the detached base checkout and open
        a zero-diff pull request.  A reviewer that commits on its own is still
        accepted: what matters is that ``HEAD`` moved away from the commit the
        task started on.  Comparing ``HEAD`` before and after also covers a task
        with **no** target sha (clone followed the default branch tip), where
        comparing against ``commit_sha`` would never detect an empty diff.

        This is the first worker-run git command after untrusted code, and
        ``git add`` executes any clean filter ``.git/config`` names while ``git
        status`` executes ``core.fsmonitor`` — both would run with the worker's
        ``OPENFISH_GIT_TOKEN``.  The config is re-asserted against the post-clone
        digest and the fsmonitor hook disabled before either runs.
        """
        root = checkout_root(workdir)
        self._assert_config_untouched(root)
        before = self._run([self._git_binary, "-C", str(root), "rev-parse", "HEAD"]).strip()
        status = self._run([
            self._git_binary, "-C", str(root),
            "-c", "core.fsmonitor=false",
            "status", "--porcelain",
        ])
        if status.strip():
            self._run([
                self._git_binary, "-C", str(root),
                "-c", "core.fsmonitor=false",
                "add", "-A",
            ])
            # ``--no-verify`` plus an empty ``core.hooksPath``: the checkout was
            # produced by a command that could leave a ``.git/hooks/pre-commit``
            # behind (``git status`` never reports ``.git/**``), and a hook would
            # run *after* the path guard and could rewrite what gets committed.
            self._run([
                self._git_binary, "-C", str(root),
                *self._no_hooks_args(),
                "-c", "user.name=openfish-agent",
                "-c", "user.email=agent@openfish.invalid",
                "commit", "--quiet", "--no-verify", "-m", message,
            ])
        head = self._run([self._git_binary, "-C", str(root), "rev-parse", "HEAD"]).strip()
        if not head or head == before:
            raise AgentRunnerError(
                f"fix 模式没有产生可推送的提交（HEAD 仍是 {before[:12] or '起始提交'}，"
                f"分支 {branch}）"
            )
        return True

    @staticmethod
    def _no_hooks_args() -> list[str]:
        """``-c core.hooksPath=…`` so a hook planted in the checkout never runs.

        Git discovers hooks only from the working tree's ``.git`` directory, so
        pointing ``core.hooksPath`` at an empty directory neutralises both
        ``.git/hooks`` and any ``core.hooksPath`` an earlier step wrote into
        ``.git/config``.  Every command that could run a repository hook (commit
        *and* push) must carry this.

        The directory is created **fresh with an unpredictable name** on every
        call, under the system temp dir rather than the work tree: the checkout
        and its parent are writable by the code that ran before this step, so a
        directory inside them could be deleted and replaced with a symlink to
        ``.git/hooks`` — which would put the hooks straight back.  ``/tmp`` is
        sticky, so the untrusted uid cannot remove or replace it.
        """
        hooks_dir = Path(tempfile.mkdtemp(prefix="openfish-hooks-"))
        return ["-c", f"core.hooksPath={hooks_dir}"]

    @staticmethod
    def _config_hash(root: Path) -> str:
        """Digest of the checkout's ``.git/config`` (``""`` when unreadable)."""
        try:
            text = (Path(root) / ".git" / "config").read_text(
                encoding="utf-8", errors="replace"
            )
        except OSError:
            return ""
        return sha256_text(text)

    def _assert_config_untouched(self, root: Path) -> None:
        """Refuse any post-untrusted git command when ``.git/config`` was rewritten.

        That file is read from *inside* the checkout, which repository-supplied
        code runs in before this step.  A rewrite could redirect the push
        (``url.*.insteadOf``), add an ``http.extraHeader``, plant a
        ``credential.helper`` git would hand the token to on ``store``, or name a
        ``core.fsmonitor`` / ``filter.<n>.clean`` program for the worker's own
        ``git status`` / ``git add`` to execute with the runner token.  Rather
        than enumerate the dangerous keys, any change from the post-clone digest
        is a violation.
        """
        current = self._config_hash(root)
        if self._config_digest is None or current != self._config_digest:
            raise AgentRunnerError(
                "checkout 的 .git/config 在 clone 之后被改写，拒绝继续执行 git"
                "（可能是 url.*.insteadOf / credential.helper / http.extraHeader / "
                "core.fsmonitor / filter.*.clean 注入）"
            )

    def _remote_head(self, root: Path, repo_url: str, branch: str) -> str:
        """The remote SHA at ``refs/heads/<branch>`` on *repo_url*, or ``""``."""
        output = self._run([
            self._git_binary, "-C", str(root),
            "ls-remote", "--heads", repo_url, f"refs/heads/{branch}",
        ])
        parts = output.split()
        return parts[0] if parts else ""

    def push(
        self,
        workdir: Path,
        *,
        branch: str,
        commit_sha: str,
        repo_url: str,
    ) -> None:
        assert_pushable(branch)
        root = checkout_root(workdir)
        # The destination is the task's repository, never the checkout's mutable
        # ``origin``: untrusted code ran here between clone and push.
        target = str(repo_url or "").strip()
        if not target:
            raise AgentRunnerError("push 缺少仓库地址（repo_url）")
        self._assert_config_untouched(root)
        head = self._run([self._git_binary, "-C", str(root), "rev-parse", "HEAD"]).strip()
        remote = self._remote_head(root, target, branch)
        if remote and head and remote == head:
            # The branch is already exactly what this run produced: a retry (or a
            # second replica) reached the publish step twice.  Pushing again would
            # be a no-op and a different SHA would be rejected as non-fast-forward,
            # so reuse it instead of turning a repeat into a hard failure.
            logger.info("分支 %s 已存在且指向本次提交 %s，跳过重复 push", branch, head[:12])
            return
        if remote:
            raise AgentRunnerError(
                f"远端分支 {branch} 已存在（{remote[:12]}）且不是本次提交 {head[:12]}；"
                "拒绝覆盖，请人工确认上一次尝试留下的分支/PR"
            )
        # Fully-qualified destination: the checkout is detached, and from a
        # detached HEAD git refuses to DWIM ``HEAD:agent/<name>`` into
        # ``refs/heads/agent/<name>`` ("您提供的目标不是一个完整的引用名称").
        self._run([
            self._git_binary, "-C", str(root),
            *self._no_hooks_args(),
            "push", "--quiet", target, f"HEAD:refs/heads/{branch}",
        ])

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
        model_session: Any | None = None,
        model_route: str | None = None,
        model_env: Mapping[str, str] | None = None,
        gate_timeout: float | None = None,
        retention_seconds: int | None = None,
        max_findings: int | None = None,
        policy_parser: Callable[[str | None], PolicyView] | None = None,
        pr_policy: str | None = None,
        auto_fix: bool | None = None,
        publish_guard: Callable[[], None] | None = None,
    ) -> None:
        self._adapter: RunnerAdapter = adapter or SubprocessRunnerAdapter()
        self._sink: TaskSink = sink or NullSink()
        #: Called immediately before a push and again before opening a PR.  The
        #: worker injects a lease-liveness check here; without it a task whose
        #: lease was reclaimed while it ran would still publish.
        self._publish_guard = publish_guard
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
        # Precedence for both knobs: explicit argument > ``AGENT_*`` env >
        # ``.agent/review-policy.yml`` > built-in default.  The policy file is
        # per repository and therefore only known at ``run`` time, so the first
        # two levels are resolved here and the rest in ``run``.
        self._pr_policy_override = (
            normalize_pr_policy(pr_policy)
            if pr_policy is not None
            else configured_pr_policy()
        )
        self._auto_fix_override = (
            bool(auto_fix) if auto_fix is not None else configured_auto_fix()
        )
        self._model_env: dict[str, str] = self._resolve_model_env(
            model_session, model_route=model_route, fallback=model_env
        )
        # The model credential plus whatever the adapter holds (its git token):
        # the token rides in the git subprocess environment, so it must be in the
        # same redaction set as the model key — logs, result.json and errors.
        self._secrets: tuple[str, ...] = secret_values(self._model_env) + self._adapter_secrets()

    def _adapter_secrets(self) -> tuple[str, ...]:
        """Credentials the adapter holds, when it exposes them."""
        getter = getattr(self._adapter, "secrets", None)
        if not callable(getter):
            return ()
        return tuple(str(item) for item in getter() if item)

    @staticmethod
    def _resolve_model_env(
        model_session: Any | None,
        *,
        model_route: str | None,
        fallback: Mapping[str, str] | None,
    ) -> dict[str, str]:
        if fallback is not None:
            return {str(key): str(value) for key, value in fallback.items()}
        if model_session is None:
            # No session, no route table: a caller that wants a model env from
            # the database passes one (or hands over ``model_env`` directly).
            return {}
        try:
            return resolve_model_env(model_session, route=model_route)
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
        # A retry gets its own directory: if a reclaimed lease let a second
        # replica start the same task, ``prepare()`` must not delete the first
        # attempt's checkout from under it.
        workdir = workdir_for(self._work_root, task.task_id, task.attempt)
        redaction = SecretRedactingFilter(self._secrets)
        logger.addFilter(redaction)
        adapter = self._adapter
        adapter.set_model_env(self._model_env)
        self._log_model_env()
        started_at = utc_now_iso()
        pr_url: str | None = None
        result_ref: str | None = None
        frozen: FrozenSuite | None = None
        suite_state = STATUS_UNVERIFIED
        try:
            self._sink.mark_running(task.task_id)
            adapter.prepare(workdir)

            steps.append("clone")
            adapter.clone(workdir, repo_url=task.repo_url, commit_sha=task.commit_sha)

            # ── Hand the checkout to the sandbox uid (privilege separation) ──
            # Everything *after* this point that runs repository code — the
            # gates (``services.gates``) and the headless review command
            # (``agent_worker.build_review_fn``) — is dropped to
            # ``AGENT_SANDBOX_UID``/``AGENT_SANDBOX_GID``.  The work tree must
            # therefore be group-writable *without* changing its owner: git
            # stays the worker's (no dubious-ownership), while the sandbox uid
            # can still write the build products a check leaves behind.
            # ``prepare_untrusted_workdir`` is a no-op without those env vars.
            # git itself (clone/fetch/commit/push) deliberately never drops: it
            # carries the credential and is the trusted worker's own process.
            try:
                prepare_untrusted_workdir(workdir)
            except SandboxIdentityError as exc:
                # Fail the task loudly: a half-configured sandbox must not
                # silently degrade into "review code as the worker".
                raise AgentRunnerError(
                    f"无法为任务 {task.task_id} 准备工作目录的沙箱身份：{exc}"
                ) from exc
            logger.info(
                "agent 任务 %s：%s", task.task_id, describe_identity()
            )

            steps.append("read")
            context = adapter.read_context(workdir)
            policy = load_policy(context.policy_text, parser=self._policy_parser)

            # ── Freeze BOTH suites from the BASE snapshot (P1.3) ────────
            # Resolution happens here, before the review step gets a chance to
            # edit anything, and each descriptor is reused for the post-fix
            # re-run.  The repo tree is the source of truth; ``FrozenSuite``
            # pins which revision it was read from.  The two suites are resolved
            # independently and never merged: ``pairs.user`` is authority,
            # ``pairs.ai`` is advisory (see services.check_trust).
            checkout = checkout_root(workdir)
            pairs = resolve_suites(checkout, policy_checks_field=policy.checks)
            frozen_user = freeze_suite(pairs.user, base_sha=task.commit_sha)
            frozen_ai = freeze_suite(pairs.ai, base_sha=task.commit_sha)
            frozen = frozen_user
            logger.info(
                "agent 任务 %s 校验套件：user=%s/%d checks hash=%s；ai=%s/%d checks hash=%s",
                task.task_id, frozen_user.source, len(frozen_user.suite.checks),
                frozen_user.fingerprint[:12], frozen_ai.source,
                len(frozen_ai.suite.checks), frozen_ai.fingerprint[:12],
            )
            if task.kind == "fix":
                # `readonly chmod` is defence in depth; the authoritative guard
                # is the changed-path check before commit.  Besides the AI suite
                # directory, lock the *base-revision* entry scripts and
                # declaration files the resolved user suite runs: a fix that
                # rewrites its own judge fails at the write, not only at commit.
                _protect_suite(adapter, workdir, readonly=True)
                _protect_paths(
                    adapter, workdir,
                    suite_protected_paths(frozen_user.suite, root=checkout),
                    readonly=True,
                )

            steps.append("gates")
            summary = adapter.run_gates(
                workdir, timeout=self._gate_timeout, suite=frozen_user.suite
            )
            gates = [gate_entry(item) for item in _gate_items(summary)]
            suite_state = _summary_state(summary)
            ai_summary = _run_ai_gates(adapter, workdir, self._gate_timeout, frozen_ai.suite)
            ai_gates = [gate_entry(item) for item in _gate_items(ai_summary)]
            ai_suite_state = _summary_state(ai_summary)

            steps.append("review")
            raw = list(
                adapter.review(
                    workdir, policy=policy, commit_sha=task.commit_sha,
                    context=context, kind=task.kind,
                )
            )[: self._effective_max_findings(policy)]

            # A curator proposal is proven (or not) by the *validator*, never by
            # the model's claim: this runs before the commit and rewrites the
            # manifest's ``validated`` flags from the falsifiability evidence.
            curator_report = _curate(adapter, workdir, self._gate_timeout) if task.kind == "checks" else None
            if curator_report is not None:
                logger.info(
                    "agent 任务 %s curator 提案：%s checks，%s validated，gating=%s",
                    task.task_id, curator_report.get("total"),
                    curator_report.get("validated"), curator_report.get("gating"),
                )

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
                        "suite_hash": frozen_user.fingerprint,
                        "suite_source": frozen_user.source,
                        "suite_state": suite_state,
                        "ai_suite_hash": frozen_ai.fingerprint,
                        "ai_suite_source": frozen_ai.source,
                        "ai_suite_state": ai_suite_state,
                    },
                    "findings": [dict(item) for item in enriched],
                    "gates": [item.model_dump() for item in gates],
                    "ai_gates": [item.model_dump() for item in ai_gates],
                    "curator": curator_report,
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
            pr_policy = self._effective_pr_policy(policy)
            auto_fix = self._effective_auto_fix(policy)
            failed_gates = _failed_gate_names(gates)
            # §6.4: a push-triggered *review* may only become a fix+PR when the
            # repository explicitly opted in (`auto_fix: true`) **and** there is
            # something concrete to fix (a failed gate, which is in the
            # "allowed to auto-fix" column).  Default false, like per-rule
            # autofix.
            escalate = task.kind == "review" and auto_fix and bool(failed_gates)
            writes_source = task.kind in ("fix", "checks") or escalate
            # Trust ladder (governance): the repository suite is authority; the
            # AI suite is advisory.  L3 is only reachable when a policy turns it
            # on (default off) — and no rung ever authorises a merge.
            assurance = assess_assurance(user_summary=summary, ai_summary=ai_summary)
            body_user_summary = summary
            body_ai_summary = ai_summary
            document["run"]["assurance_level"] = assurance.level
            document["run"]["assurance_label"] = assurance.label
            result = validate_result(document)
            if writes_source:
                # I4: refuse anything but agent/* before any push is attempted.
                branch = assert_pushable(
                    task.branch
                    or f"{AGENT_BRANCH_PREFIX}{'checks' if task.kind == 'checks' else 'fix'}"
                       f"-{task.task_id}"
                )
                # ── Path guard: the hard invariant, in code ──────────────
                # A run that produces a fix may not have touched the frozen
                # suite that judges it; a curator may not have touched source.
                # This runs *before* commit, so a violation fails the task
                # loudly and never reaches a branch or a PR.  The resolved user
                # suite is passed in so its own entrypoints (and the files that
                # declare how it runs) are protected too — freezing the
                # descriptor without freezing the scripts it executes was the
                # hole that let a fix rewrite its own judge.
                _guard_task_paths(
                    adapter, task, workdir, escalate=escalate, suite=frozen_user.suite,
                )
                if pr_policy == PR_POLICY_NEVER:
                    logger.info(
                        "agent 任务 %s：pr_policy=never，只产出 finding，不 commit/push/开 PR",
                        task.task_id,
                    )
                else:
                    # §9.3 step 6: the review's edits become a commit; an empty
                    # one is a failed task, never an empty pull request.
                    adapter.commit(
                        workdir,
                        branch=branch,
                        commit_sha=task.commit_sha,
                        message=_commit_message(result),
                    )
                    if task.kind == "checks":
                        # A curator proposal is *not* a fix: it is the check suite
                        # itself, and its checks are unvalidated by definition
                        # until the validator runs on them.  Gating it on the
                        # frozen base suite's on_green would make every first
                        # proposal impossible — so it opens a labelled PR for
                        # human review and can never merge.
                        logger.info(
                            "agent 任务 %s：curator 提案 PR 不按 on_green 门控"
                            "（新 check 在 validated 之前本来就不能门控）；仍需人工 review",
                            task.task_id,
                        )
                    if pr_policy == PR_POLICY_ON_GREEN and task.kind != "checks":
                        # Re-run the **frozen** suites after the fix: step 3's
                        # summary describes the base commit, and re-resolving
                        # from the modified tree would let the fix grade its own
                        # homework.  Pasting a green base summary into the PR
                        # would claim a state the fix has not earned.
                        fresh = adapter.run_gates(
                            workdir, timeout=self._gate_timeout, suite=frozen_user.suite
                        )
                        gates = [gate_entry(item) for item in _gate_items(fresh)]
                        failed_gates = _failed_gate_names(gates)
                        fresh_ai = _run_ai_gates(
                            adapter, workdir, self._gate_timeout, frozen_ai.suite
                        )
                        ai_gates = [gate_entry(item) for item in _gate_items(fresh_ai)]
                        assurance = assess_assurance(
                            user_summary=fresh, ai_summary=fresh_ai
                        )
                        body_user_summary = fresh
                        body_ai_summary = fresh_ai
                        document["gates"] = [item.model_dump() for item in gates]
                        document["ai_gates"] = [item.model_dump() for item in ai_gates]
                        document["run"]["suite_state"] = _summary_state(fresh)
                        document["run"]["ai_suite_state"] = _summary_state(fresh_ai)
                        document["run"]["assurance_level"] = assurance.level
                        document["run"]["assurance_label"] = assurance.label
                        result = validate_result(document)
                        if not assurance.may_open_pr:
                            # L0 (nothing verified) and a failed repository suite
                            # both land here; the AI suite may never override a
                            # repository-suite failure.
                            reason = (
                                f"pr_policy=on_green 但保证等级 {assurance.level} "
                                f"不允许自动 push/开 PR（{assurance.reason}）"
                                + (
                                    "；失败 gate：" + ", ".join(failed_gates)
                                    if failed_gates else ""
                                )
                                + (
                                    "；校验 unverified（没有可门控的检查）"
                                    if assurance.level == "L0" else ""
                                )
                                + "；已 commit 但按策略不 push、不开 PR"
                            )
                            logger.error("agent 任务 %s：%s", task.task_id, reason)
                            self._sink.mark_failed(task.task_id, reason)
                            return RunOutcome(
                                task_id=task.task_id,
                                status="failed",
                                steps=tuple(steps),
                                run=document["run"],
                                findings=tuple(document["findings"]),
                                gates=tuple(document["gates"]),
                                error=reason,
                            )
                    # Durable record **before** the first external side effect:
                    # a push/PR is not idempotent, so a crash after it would
                    # leave a published change the platform never recorded and a
                    # retry unable to reproduce.  Findings are the review's real
                    # output and must survive even if publishing fails.
                    result_ref = adapter.emit(workdir, payload=document)
                    self._sink.record_findings(task.task_id, result)
                    self._publish(branch=branch)
                    adapter.push(
                        workdir,
                        branch=branch,
                        commit_sha=task.commit_sha,
                        repo_url=task.repo_url,
                    )
                    self._publish(branch=branch)
                    if task.kind == "checks" and curator_report is not None:
                        # A curator proposal is labelled, so a reviewer (or an
                        # audit) can find it without reading the diff.
                        pr_title = proposal_title(
                            count=int(curator_report.get("total") or 0),
                            validated=int(curator_report.get("validated") or 0),
                        )
                        pr_body = proposal_body(
                            CuratorReport.from_payload(curator_report),
                            base_sha=task.commit_sha,
                        )
                    else:
                        pr_title = _pr_title(result)
                        pr_body = self._pr_body(
                            result,
                            assurance=assurance,
                            user_summary=body_user_summary,
                            ai_summary=body_ai_summary,
                            user_suite=frozen_user.suite,
                            ai_suite=frozen_ai.suite,
                        )
                    pr_url = adapter.open_pr(
                        workdir,
                        branch=branch,
                        base=task.base_branch,
                        title=pr_title,
                        body=pr_body,
                    )

            if result_ref is None:
                # No publish path ran (pr_policy=never, or a review that did not
                # escalate): still record the review before marking the task done.
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
                ai_gates=tuple(document.get("ai_gates") or ()),
                curator=document.get("curator"),
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
            if frozen is not None and task.kind in ("fix", "checks"):
                _protect_suite(adapter, workdir, readonly=False)
                _protect_paths(
                    adapter, workdir,
                    suite_protected_paths(frozen.suite, root=checkout_root(workdir)),
                    readonly=False,
                )
            try:
                mark_finished(workdir)
            except OSError as exc:
                logger.warning("cannot mark work directory %s finished: %s", workdir, exc)
            logger.removeFilter(redaction)

    def _publish(self, *, branch: str) -> None:
        """Refuse to publish when the injected guard says this run lost its lease.

        The queue's lease is the only mutual exclusion, and it is advisory: the
        heartbeat only logs when it is lost.  Two replicas can therefore be
        inside the same task.  The worker injects a DB liveness check here, so
        the stale replica fails at the last moment instead of pushing a branch or
        opening a PR that belongs to the current owner.
        """
        if self._publish_guard is None:
            return
        try:
            self._publish_guard()
        except AgentRunnerError:
            raise
        except Exception as exc:  # noqa: BLE001 - a broken guard must fail closed
            raise AgentRunnerError(
                f"发布前无法确认任务租约仍然有效（{type(exc).__name__}: {exc}）；"
                f"拒绝 push {branch} / 开 PR"
            ) from exc

    def _effective_max_findings(self, policy: PolicyView) -> int:
        budget = policy.max_findings_per_run or self._max_findings
        return max(1, min(int(budget), self._max_findings))

    def _effective_pr_policy(self, policy: PolicyView) -> str:
        """Explicit arg > ``AGENT_PR_POLICY`` > policy file > ``on_green``."""
        if self._pr_policy_override is not None:
            return self._pr_policy_override
        return normalize_pr_policy(policy.pr_policy) or DEFAULT_PR_POLICY

    def _effective_auto_fix(self, policy: PolicyView) -> bool:
        if self._auto_fix_override is not None:
            return bool(self._auto_fix_override)
        return bool(policy.auto_fix)

    @property
    def pr_policy(self) -> str | None:
        """The constructor/env override, or ``None`` when the policy file decides."""
        return self._pr_policy_override

    @property
    def auto_fix(self) -> bool | None:
        """The constructor/env override, or ``None`` when the policy file decides."""
        return self._auto_fix_override


    def _log_model_env(self) -> None:
        if not self._model_env:
            logger.info("agent 运行时未配置模型路由")
            return
        logger.info("agent 运行时模型配置（已掩码）：%s", mask_env(self._model_env))

    def _pr_body(
        self,
        result: Result,
        *,
        assurance: Assurance | None = None,
        user_summary: GateSummary | None = None,
        ai_summary: GateSummary | None = None,
        user_suite: CheckSuite | None = None,
        ai_suite: CheckSuite | None = None,
    ) -> str:
        lines = ["## 智能体发现", ""]
        if not result.findings:
            lines.append("本次 review 未产出 finding。")
        for finding in result.findings:
            lines.append(
                f"- [{finding.level}/{finding.severity}] "
                f"`{finding.file_path}` — {finding.title}（`{finding.rule_id}`）"
            )
        lines.append("")
        if assurance is not None:
            # Two distinct, labelled blocks — never one anonymous green list —
            # plus the explicit "a human must merge this" notice.
            lines.append(render_two_suites(
                user_summary,
                ai_summary,
                assurance,
                user_suite=user_suite,
                ai_suite=ai_suite,
            ))
        else:
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


def _summary_state(summary: Any) -> str:
    """The suite-level state of a summary; a duck-typed one defaults to passed.

    A fake adapter in the offline gate returns a :class:`GateSummary` with an
    explicit state; the fallback keeps an older duck-typed object working
    without silently *improving* its verdict (``passed`` is the value such an
    object already implied).
    """
    state = str(getattr(summary, "state", "") or "")
    return state or "passed"


def _run_ai_gates(
    adapter: RunnerAdapter,
    workdir: Path,
    timeout: float,
    suite: CheckSuite,
) -> GateSummary:
    """Run the AI suite when the adapter supports it; else an honest unverified.

    An adapter that predates the two-suite model must not make the AI block look
    green by omission — so the fallback is an explicit ``unverified`` summary.
    """
    method = getattr(adapter, "run_ai_gates", None)
    if method is None:
        return unverified_summary(
            "adapter 未实现 run_ai_gates：AI 校验块按 unverified 处理（不参与门控）",
            source=SOURCE_AGENT_CHECKS,
        )
    return method(workdir, timeout=timeout, suite=suite)


def _curate(
    adapter: RunnerAdapter,
    workdir: Path,
    timeout: float,
) -> Mapping[str, Any] | None:
    """Validate a curator proposal when the adapter can; else no report.

    No report means every proposed check stays ``unvalidated`` in the tree — the
    safe direction — so a missing hook can never promote a check to gating.
    """
    method = getattr(adapter, "curate", None)
    if method is None:
        logger.info("adapter 未实现 curate：提案不会被证伪验证，全部保持 unvalidated")
        return None
    report = method(workdir, timeout=timeout)
    return dict(report) if report is not None else None


#: Path fragments that a check's own execution leaves behind.  They are not
#: edits the agent made and must not trip the path guard.  ``SANDBOX_HOME_DIRNAME``
#: is a stale copy from a deployment where the sandbox ``HOME`` lived inside the
#: checkout; the current implementation puts it outside the work tree entirely.
_TRANSIENT_MARKERS: tuple[str, ...] = (
    "__pycache__/", ".pytest_cache/", ".mypy_cache/", ".ruff_cache/",
    f"{SANDBOX_HOME_DIRNAME}/",
)


def _is_transient_path(path: str) -> bool:
    normalized = str(path or "").replace("\\", "/")
    if normalized.endswith((".pyc", ".pyo")):
        return True
    return any(marker in normalized for marker in _TRANSIENT_MARKERS)


def _protect_suite(adapter: RunnerAdapter, workdir: Path, *, readonly: bool) -> None:
    """Best-effort OS-level protection; the path guard is authoritative.

    Optional on purpose: an adapter that cannot chmod still gets the real
    enforcement, and a failure here must never fail an otherwise valid task.
    The adapter implementation is expected to skip symlinks rather than follow
    them (see :meth:`SubprocessRunnerAdapter.protect_suite`): the checkout is
    sandbox-writable, so a followed link would chmod outside it.
    """
    protect = getattr(adapter, "protect_suite", None)
    if protect is None:
        return
    try:
        protect(workdir, readonly=readonly)
    except OSError as exc:
        logger.debug("cannot change suite permissions under %s: %s", workdir, exc)


def _protect_paths(
    adapter: RunnerAdapter,
    workdir: Path,
    paths: Sequence[str],
    *,
    readonly: bool,
) -> None:
    """Best-effort lock/unlock of specific repo-relative files (defence in depth).

    Complements :func:`_protect_suite`: the AI suite directory is protected
    there, the *user* suite's base-revision entry scripts and declaration files
    are protected here.  The adapter implementation is expected to skip symlinks
    rather than follow them (see
    :meth:`SubprocessRunnerAdapter.protect_paths`): the paths are
    repository-controlled, so a followed link would chmod outside the checkout.
    """
    locked = tuple(paths)
    protect = getattr(adapter, "protect_paths", None)
    if protect is None or not locked:
        return
    try:
        protect(workdir, locked, readonly=readonly)
    except OSError as exc:
        logger.debug("cannot change file permissions under %s: %s", workdir, exc)


def _adapter_changed_paths(adapter: RunnerAdapter, workdir: Path) -> list[str]:
    """The run's edits, or a loud failure when the adapter cannot report them.

    Fail closed: an adapter that cannot enumerate what it changed cannot prove
    it left the frozen suite alone, and "cannot prove" is not "may push".
    """
    method = getattr(adapter, "changed_paths", None)
    if method is None:
        raise AgentRunnerError(
            "adapter 没有实现 changed_paths()，无法证明本次 run 没有改动被冻结的校验套件；"
            "按失败处理（硬不变量：同一 run 不得既修复又削弱判它的检查）"
        )
    return [str(path) for path in method(workdir)]


def _guard_task_paths(
    adapter: RunnerAdapter,
    task: TaskRequest,
    workdir: Path,
    *,
    escalate: bool = False,
    suite: CheckSuite | None = None,
) -> None:
    """Refuse a run whose edits cross its role boundary (the hard invariant).

    * a ``fix`` (or an escalated review→fix) may not write ``.agent/checks/**``,
      the resolved user suite's entrypoints, or the files that declare it;
    * a ``checks`` curator may write only ``.agent/checks/**`` (plus test files).
    """
    changed = _adapter_changed_paths(adapter, workdir)
    if task.kind == "checks":
        violations = curator_guard_violations(changed)
        if violations:
            raise AgentRunnerError(
                "checks（curator）任务写出了 .agent/checks/** 与测试文件之外的内容（"
                + ", ".join(violations)
                + "）：curator 只改校验套件，改源码是 fix 任务的职责"
            )
        return
    if task.kind == "fix" or escalate:
        violations = fix_guard_violations(changed, suite=suite)
        if violations:
            raise AgentRunnerError(
                "fix 任务改动了被冻结的校验套件或它的执行入口（"
                + ", ".join(violations)
                + "）：硬不变量禁止同一 run 既产出修复又削弱判它的检查"
            )


def _pr_title(result: Result) -> str:
    count = len(result.findings)
    if count == 1:
        return f"fix: {result.findings[0].title}"
    return f"fix: 智能体修复 {count} 项发现"


def _commit_message(result: Result) -> str:
    """One-line subject plus the rule id, per the commit contract in AGENTS.md."""
    if len(result.findings) == 1:
        finding = result.findings[0]
        return f"fix: {finding.title}\n\nrule: {finding.rule_id}"
    return f"fix: 智能体修复 {len(result.findings)} 项发现"


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
    "DEFAULT_PR_POLICY",
    "DEFAULT_RETENTION_SECONDS",
    "DEFAULT_WORK_ROOT",
    "FINISHED_MARKER",
    "MODEL_ENV_PREFIX",
    "POLICY_RELPATH",
    "PROTECTED_BRANCHES",
    "PR_POLICIES",
    "PR_POLICY_ALWAYS",
    "PR_POLICY_NEVER",
    "PR_POLICY_ON_GREEN",
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
    "checkout_root",
    "configured_auto_fix",
    "configured_gate_timeout",
    "configured_max_findings",
    "configured_pr_policy",
    "configured_retention_seconds",
    "configured_work_root",
    "gate_entry",
    "load_policy",
    "mark_finished",
    "mask_env",
    "mask_secrets",
    "normalize_pr_policy",
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
