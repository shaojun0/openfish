"""Wire the real sandbox runner into the queue worker (defects A1 / A2).

Before this module ``services.agent_queue`` shipped :func:`placeholder_handler`,
which retired every task instantly: no repository was ever cloned, no model was
ever asked, and no finding ever reached the database.  This is the production
bridge that replaces it.  It owns exactly three things:

1. **a task-handler factory** — for each claimed task it builds a
   :class:`~services.agent_runner.DbTaskSink`, a
   :class:`~services.agent_runner.SubprocessRunnerAdapter` and an
   :class:`~services.agent_runner.AgentRunner`, then runs §9.3's six steps;
2. **the platform callables the runner only knows by protocol** — ``review_fn``
   (a configurable headless command), ``search_fn`` (§8.3 context search) and
   ``open_pr_fn`` (the Forgejo client);
3. **findings ingestion** — the validated §9.5 document is folded into the
   ``findings`` table through ``services.findings.ingest``, so a review actually
   leaves a trace (defect A2).

Secrets
-------
The review command comes from ``AGENT_REVIEW_COMMAND`` and is the *only* thing
that goes into a child process's argv; the model credential travels to the child
through the environment (``OPENFISH_MODEL_*``, resolved by
:func:`services.agent_runner.resolve_model_env`) and never into argv, a log line
or the work directory.  The per-repository git/PR credential is resolved from
:class:`services.repo_runner.RepoRunnerService` (a Fernet-sealed repo token, else
the shared ``FORGEJO_RUNNER_TOKEN``): the plaintext only ever reaches
:class:`services.repo_import.ForgejoClient` and the runner's git subprocess
environment, and the runner redacts it from logs and result.json.  This module
never logs it and never reads ``FORGEJO_ADMIN_TOKEN``.

Everything external is injectable (``session_factory``, ``forgejo_client``,
``review_command``, …), so ``scripts/check_agent_runtime.py`` proves the wiring
offline with fakes.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session as SASession

from models.agent_hub import AgentTask, Repo, RepoCommit, ReviewRun
from models.base import utcnow
from services.agent_queue import ClaimedTask, TaskHandler
from services.agent_runner import (
    AGENT_BRANCH_PREFIX,
    MODEL_ENV_PREFIX,
    AgentRunner,
    AgentRunnerError,
    DbTaskSink,
    PolicyView,
    Result,
    SubprocessRunnerAdapter,
    TaskRequest,
    configured_work_root,
    mask_secrets,
    resolve_model_env,
)
from services.git_auth import git_host_of
from services.repo_runner import (
    RepoRunnerService,
    RunnerCredential,
    require_real_directory,
    shared_runner_token,
)
from services.review_policy import (
    SOURCE_FILE,
    builtin_default,
    parse_document,
    parse_yaml,
)
from services.sandbox_env import sandbox_env
from services.sandbox_identity import (
    SandboxIdentityError,
    sandbox_env_overrides,
    untrusted_popen_kwargs,
)

logger = logging.getLogger("cpypiserver.agent_worker")

#: The headless command a deployment configures to ask a model for findings.
#: Empty (the default) makes every review task fail loudly — never "done".
ENV_REVIEW_COMMAND = "AGENT_REVIEW_COMMAND"

#: The shared fallback credential the runner uses when a repository has no
#: repo-scoped token.  Deliberately **not** ``FORGEJO_ADMIN_TOKEN``: the runner
#: containers execute repository-supplied code, so the admin token stays in the
#: backend container.  Empty means the runner can review but cannot publish, and
#: says so loudly.  Mirrors :data:`services.repo_runner.SHARED_TOKEN_ENV`, the
#: single implementation (:func:`runner_token` delegates there).
ENV_RUNNER_TOKEN = "FORGEJO_RUNNER_TOKEN"

#: Wall-clock budget for that command, so a stuck model client fails the task.
ENV_REVIEW_TIMEOUT = "AGENT_REVIEW_TIMEOUT"
DEFAULT_REVIEW_TIMEOUT = 900.0

#: How the headless command learns its **role** in the run.  ``review`` must
#: leave the tree alone, ``fix`` is expected to edit files (an escalated fix that
#: produces no edit fails at ``commit()`` with "fix 模式没有产生可推送的提交"),
#: and ``checks`` may write only ``.agent/checks/**`` plus test files.  Without
#: this signal a command cannot tell "review this" from "fix this", which is how
#: an escalated task used to fail for the wrong reason.
ENV_TASK_KIND = "OPENFISH_TASK_KIND"

#: The roles :data:`ENV_TASK_KIND` can carry; mirrors the runner's task kinds.
TASK_KINDS: tuple[str, ...] = ("review", "fix", "checks")

#: The model route table the runner reads (same default as the backend image).
ENV_MODELS_FILE = "MODELS_FILE"
DEFAULT_MODELS_FILE = "/app/config/model_routes.json"

#: Task kinds this sandbox runner implements.  Anything else (``import``,
#: ``backfill``) has no handler yet and must fail loudly rather than be retired
#: as if it had run.
#:
#: ``checks`` is the curator role (writes only ``.agent/checks/**`` + test files,
#: opens its own PR, never rides along with a fix PR).  The runner seam is ready
#: for it; enqueueing one still needs ``checks`` in ``models.agent_hub.TASK_KIND``
#: (a CHECK-constraint migration — see docs/agent-hub/DESIGN-ai-checks.md §9).
SUPPORTED_KINDS: tuple[str, ...] = ("review", "fix", "checks")


# ── Session / repo helpers ───────────────────────────────────────────

def sqlalchemy_session_factory(engine: Any) -> Callable[[], SASession]:
    """A ``Session`` factory bound to *engine*, engine-driven and Flask-free."""
    def make() -> SASession:
        return SASession(bind=engine, expire_on_commit=False)

    return make


@dataclass(frozen=True)
class _LoadedTask:
    """A claimed queue row resolved to everything one handler run needs.

    ``credential`` is ``None`` when the repository has neither a sealed
    repo-scoped token nor a shared ``FORGEJO_RUNNER_TOKEN``; the fix/push path
    then fails loudly on the git call, while a review-only task still runs.
    ``workspace_root`` is the repository's own logical runner directory, so two
    repos claimed by the same pooled worker never share a checkout root.  It is
    guaranteed to be a real directory (never a symlink) under ``AGENT_WORK_ROOT``
    and is re-checked immediately before it is handed to the runner.
    """

    task: TaskRequest
    slug: str
    credential: RunnerCredential | None
    workspace_root: Path


def _load_task(claimed: ClaimedTask, sessions: Callable[[], SASession]) -> _LoadedTask:
    """Turn a claimed queue row into the runner's task plus its repo runner.

    ``slug`` is the Forgejo name when the repository was mirrored (open PRs go
    there), otherwise the platform slug.  The repository's logical runner is
    resolved here: a disabled runner refuses the task, ``last_task_at`` is
    stamped, the (possibly shared) credential is read — ``None`` when neither a
    repo-scoped nor a shared token exists — and the workspace root is derived
    from the runner's ``workspace_subdir`` under ``AGENT_WORK_ROOT``.  That root
    is created as a chain of real directories by the service and then re-checked
    with ``lstat`` here, so a symlink planted in the group-writable work root is
    refused rather than followed into another repository's checkout.
    """
    if claimed.kind not in SUPPORTED_KINDS:
        raise AgentRunnerError(
            f"任务类型 {claimed.kind!r} 没有沙箱 runner 处理器（只支持 "
            f"{', '.join(SUPPORTED_KINDS)}）；不要把它当作已完成"
        )
    payload = claimed.payload or {}
    repo_id = int(claimed.repo_id)
    session = sessions()
    try:
        repo = session.get(Repo, repo_id)
        if repo is None:
            raise AgentRunnerError(f"仓库 {repo_id} 不存在，无法执行任务 {claimed.id}")
        slug = str(repo.forgejo_repo or repo.slug)
        commit_sha = str(payload.get("commit_sha") or "").strip()
        if not commit_sha:
            commit_sha = _latest_commit_sha(session, repo_id)
        base_branch = str(repo.default_branch or "main")
        repo_url = _repo_url(slug)
    finally:
        session.close()

    # One logical runner per repository: it owns the credential and the
    # workspace.  A disabled runner must not execute even when the task was
    # already leased (the claim query skips new ones; this is the backstop).
    runners = RepoRunnerService(sessions)
    runner = runners.get(repo_id)
    if runner is not None and not bool(runner.enabled):
        raise AgentRunnerError(
            f"仓库 {repo_id} 的 runner 已禁用（enabled=false），拒绝执行任务 {claimed.id}"
        )
    runners.record_task(repo_id)
    credential = runners.credential(repo_id)
    workspace_root = runners.workspace_root(repo_id, configured_work_root())
    # Cheap second guard at the consumer seam.  ``workspace_root`` already opened
    # every component with ``O_NOFOLLOW``, but the work root is group-writable by
    # the untrusted sandbox uid, so re-verify the returned path with ``lstat``
    # immediately before it reaches the runner: a component swapped for a symlink
    # in the meantime is refused instead of followed into another repo's root.
    require_real_directory(workspace_root, what="runner 工作区根")

    # Every write task gets its own `agent/*` branch and its own work directory,
    # both keyed by the attempt number: a queue retry must never collide with the
    # branch or the checkout a previous attempt left behind (the old fixed
    # `agent/fix-<id>` name made a retry fail non-fast-forward forever).
    attempt = max(0, int(claimed.attempts))
    if claimed.kind in ("fix", "checks"):
        label = "checks" if claimed.kind == "checks" else "fix"
        branch: str | None = f"{AGENT_BRANCH_PREFIX}{label}-{claimed.id}-a{attempt + 1}"
    else:
        branch = None

    task = TaskRequest(
        task_id=str(claimed.id),
        repo_url=repo_url,
        commit_sha=commit_sha,
        kind=str(claimed.kind),
        base_branch=base_branch,
        # A fix/checks task always works on its own agent/* branch (I4); a review
        # task never pushes, so it needs none.
        branch=branch,
        repo=slug,
        issue_number=_optional_int(payload.get("issue_number")),
        attempt=attempt,
    )
    return _LoadedTask(
        task=task, slug=slug, credential=credential, workspace_root=workspace_root,
    )


def _make_publish_guard(
    claimed: ClaimedTask,
    sessions: Callable[[], SASession],
) -> Callable[[], None]:
    """A lease-liveness check the runner calls just before push and before PR.

    The queue lease is advisory: ``agent_queue`` logs when a heartbeat is lost
    but cannot stop a running handler, so a reclaimed task can have two replicas
    inside it.  This closes the last window — the one that creates external side
    effects — by re-reading the row and refusing to publish unless *this* worker
    still holds the lease.
    """
    owner = claimed.lease.worker

    def guard() -> None:
        session = sessions()
        try:
            task = session.get(AgentTask, int(claimed.id))
            if (
                task is None
                or str(task.leased_by or "") != owner
                or str(task.status or "") not in ("leased", "running")
            ):
                raise AgentRunnerError(
                    f"任务 {claimed.id} 的租约已不在 worker {owner!r} 手上"
                    f"（leased_by={getattr(task, 'leased_by', None)!r},"
                    f" status={getattr(task, 'status', None)!r}）；"
                    "另一个副本已接管，拒绝 push/开 PR"
                )
        finally:
            session.close()

    return guard


def _latest_commit_sha(session: SASession, repo_id: int) -> str:
    """Newest imported commit for a repo, or ``""`` (adapter fetches the tip)."""
    row = session.execute(
        select(RepoCommit)
        .where(RepoCommit.repo_id == repo_id)
        .order_by(RepoCommit.committed_at.desc())
        .limit(1)
    ).scalars().first()
    return str(row.sha) if row is not None and row.sha else ""


def _repo_url(slug: str) -> str:
    """The git URL inside the compose network — **no credential in the URL**."""
    from services.repo_import import ImportConfig

    config = ImportConfig.from_env()
    base = (config.git_base_url or config.base_url).rstrip("/")
    return f"{base}/{slug}.git"


def _optional_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None and str(value).strip() else None
    except (TypeError, ValueError):
        return None


# ── review_fn: the configurable headless command ─────────────────────

def build_review_fn(
    command: str | None = None,
    *,
    model_env: Mapping[str, str] | None = None,
    timeout: float | None = None,
) -> Callable[..., Sequence[Mapping[str, Any]]]:
    """A ``review_fn`` that runs ``AGENT_REVIEW_COMMAND`` in the checkout.

    The command must print a JSON array of §9.5 findings (or an object with a
    ``findings`` key) on stdout.  It reads the model credential from the
    environment, never from argv; a missing command raises instead of returning
    an empty result, which is what keeps an unconfigured deployment from
    silently marking every task ``done``.
    """
    text = (
        command if command is not None else os.environ.get(ENV_REVIEW_COMMAND, "")
    ).strip()
    env = {str(key): str(value) for key, value in (model_env or {}).items()}
    budget = float(
        timeout if timeout is not None
        else os.environ.get(ENV_REVIEW_TIMEOUT) or DEFAULT_REVIEW_TIMEOUT
    )

    def review_fn(
        workdir: Path,
        *,
        policy: Any,
        commit_sha: str,
        context: Any,
        kind: str = "review",
    ) -> Sequence[Mapping[str, Any]]:
        if not text:
            raise AgentRunnerError(
                f"{ENV_REVIEW_COMMAND} 未配置：没有可执行的 headless review 命令，"
                "任务按失败处理（绝不静默退休）"
            )
        argv = shlex.split(text)
        if not argv:
            raise AgentRunnerError(f"{ENV_REVIEW_COMMAND} 为空命令：{command!r}")
        checkout = Path(workdir) / "repo"
        cwd = checkout if checkout.is_dir() else Path(workdir)
        # The review command is where the model credential legitimately goes —
        # and the *only* platform secret that does.  Everything else the worker
        # holds (SECRET_KEY, FORGEJO_ADMIN_TOKEN, GIT_IDENTITY_KEY, …) is dropped
        # by the allowlist, so a model steered by untrusted issue text cannot
        # read the platform's keys out of its own environment.
        child = sandbox_env(extra={
            **env,
            "GIT_TERMINAL_PROMPT": "0",
            # No secret is ever appended to argv: ``argv`` is exactly the
            # operator's command, and the credential rides in ``child``.
            "OPENFISH_WORKDIR": str(workdir),
            "OPENFISH_REPO_DIR": str(cwd),
            "OPENFISH_COMMIT_SHA": str(commit_sha),
            # P1.4: the role, so a fixer edits files and a reviewer does not.
            ENV_TASK_KIND: str(kind or "review"),
        })
        # Merged *after* the allowlist, exactly like the gate executor: the
        # sandbox child gets a worker-owned HOME outside the checkout it can
        # actually write (the worker's own is mode 0700 and belongs to the
        # trusted uid) and, when the deployment configures one, is dropped to the
        # dedicated sandbox uid/gid.  The review command is untrusted — it reads
        # issue text and repository files — so it must never share the worker's
        # uid and read its ``/proc/<pid>/environ``.
        child.update(sandbox_env_overrides())
        try:
            proc = subprocess.run(
                argv,
                cwd=str(cwd),
                capture_output=True,
                text=True,
                env=child,
                timeout=budget,
                check=False,
                # ``{}`` in dev mode; a configured-but-unusable identity raises
                # ``SandboxIdentityError`` and the task fails loudly rather than
                # running the headless command as the credentialed worker.
                **untrusted_popen_kwargs(),
            )
        except SandboxIdentityError as exc:
            raise AgentRunnerError(
                f"review 命令的沙箱身份不可用（{exc}）；任务失败，"
                "绝不以 worker uid 运行不可信代码"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise AgentRunnerError(
                f"review 命令超过 {budget:g}s 未结束，任务失败"
            ) from exc
        if proc.returncode != 0:
            detail = mask_secrets(
                (proc.stderr or proc.stdout or "").strip()[:400], tuple(env.values()),
            )
            raise AgentRunnerError(
                f"review 命令失败（exit {proc.returncode}）：{detail}"
            )
        return _parse_findings(proc.stdout)

    return review_fn


def _parse_findings(stdout: str | None) -> list[Mapping[str, Any]]:
    """Parse the command's stdout as a findings array; empty output → none.

    Anything else fails the task, so the platform's §9.5 validation sees a clean
    document rather than half-parsed junk.
    """
    text = (stdout or "").strip()
    if not text:
        return []
    try:
        payload = json.loads(text)
    except ValueError as exc:
        raise AgentRunnerError(f"review 命令输出不是 JSON：{exc}") from exc
    if isinstance(payload, Mapping):
        payload = payload.get("findings", [])
    if not isinstance(payload, list):
        raise AgentRunnerError("review 命令输出必须是 findings JSON 数组")
    return [item for item in payload if isinstance(item, Mapping)]


# ── search_fn: §8.3 context search, best-effort ──────────────────────

def build_search_fn(
    repo_id: int,
    *,
    sessions: Callable[[], SASession],
    limit: int = 5,
) -> Callable[..., Mapping[str, Any]]:
    """A ``search_fn`` that attaches historical-issue evidence to a finding.

    §8.3 wants every finding checked against the imported history before it is
    written.  When the platform's context search is unavailable (a checkout
    without the tables, a transient error) the finding is returned unchanged —
    the runner's contract is "enrich or pass through", never "fail the run".
    """
    def search_fn(
        workdir: Path,
        *,
        finding: Mapping[str, Any],
        commit_sha: str,
        context: Any,
    ) -> Mapping[str, Any]:
        del workdir, commit_sha, context
        try:
            from services.repo_context import search as context_search

            session = sessions()
            try:
                payload = context_search(
                    session,
                    repo_id=int(repo_id),
                    q=_search_query(finding),
                    kind="all",
                    limit=limit,
                )
            finally:
                session.close()
        except Exception as exc:  # noqa: BLE001 - search is best-effort
            logger.warning(
                "context search unavailable for finding %r: %s",
                finding.get("rule_id"), exc,
            )
            return finding
        return _attach_evidence(finding, payload.get("items") or [])

    return search_fn


def _search_query(finding: Mapping[str, Any]) -> str:
    """A short keyword query for a finding; keywords, never an empty string."""
    for key in ("title", "symbol", "rule_id", "file_path"):
        value = str(finding.get(key) or "").strip()
        if value:
            return value[:120]
    return ""


def _attach_evidence(
    finding: Mapping[str, Any],
    items: Sequence[Mapping[str, Any]],
) -> Mapping[str, Any]:
    """Add ``evidence`` entries (and ``#n`` references) for prior discussions."""
    enriched = dict(finding)
    evidence = [
        dict(item) for item in (enriched.get("evidence") or [])
        if isinstance(item, Mapping)
    ]
    seen = {
        (str(item.get("kind")), item.get("number"))
        for item in evidence
    }
    numbers: list[int] = []
    for item in items:
        if not isinstance(item, Mapping) or str(item.get("kind")) != "issue":
            continue
        number = item.get("number")
        if not isinstance(number, int) or ("issue", number) in seen:
            continue
        seen.add(("issue", number))
        evidence.append({"kind": "issue", "number": number, "relation": "mentions"})
        numbers.append(number)
    if not numbers:
        return finding
    enriched["evidence"] = evidence
    detail = str(enriched.get("detail") or "")
    suffix = "；历史讨论：" + " ".join(f"#{number}" for number in numbers)
    enriched["detail"] = (detail + suffix)[:4000]
    return enriched


# ── open_pr_fn: the single Forgejo credential seam ───────────────────

def runner_token() -> str:
    """The runner's narrow Forgejo credential, or ``""`` when unconfigured.

    A thin, name-compatible wrapper over
    :func:`services.repo_runner.shared_runner_token`, which owns the single
    implementation of the ``FORGEJO_RUNNER_TOKEN`` read.  Prefer the per-repo
    credential resolved by :class:`services.repo_runner.RepoRunnerService`; this
    is only the deployment-wide fallback.
    """
    return shared_runner_token()


def build_open_pr_fn(
    forgejo_repo: str,
    *,
    client: Any | None = None,
    token: str | None = None,
) -> Callable[..., str]:
    """A ``open_pr_fn`` bound to one repository.

    Without an injected *client* this builds a :class:`ForgejoClient` whose token
    is *token* — the credential the handler already resolved for this
    repository (``RepoRunnerService.credential``) — or, when *token* is ``None``,
    ``FORGEJO_RUNNER_TOKEN``.  **Never** the platform's
    ``FORGEJO_ADMIN_TOKEN``.  The runner executes repository-supplied code, so it
    gets the narrow, revocable credential and not the instance-wide admin one;
    an unset token means PR creation fails loudly instead of silently borrowing
    admin authority.

    The client is wrapped in :class:`services.agent_surface.RestrictedForgejoClient`
    **at call time**: the agent path gets exactly one capability
    (``create_pull_request``) and no merge, approve or branch-protection
    operation, no matter what methods the underlying client grows.
    """
    def open_pr_fn(
        workdir: Path,
        *,
        branch: str,
        base: str,
        title: str,
        body: str,
    ) -> str:
        del workdir
        resolved = client
        if resolved is None:
            from dataclasses import replace

            from services.repo_import import ForgejoClient, ImportConfig

            config = ImportConfig.from_env()
            if token is not None:
                # An explicit *token* is authoritative, including an explicit
                # ``""``: the handler resolved this repo's credential, and
                # re-reading the ambient env (or keeping ``ImportConfig``'s own
                # ``FORGEJO_ADMIN_TOKEN``) could silently authenticate as a
                # different principal.
                config = replace(config, admin_token=token)
            else:
                shared = runner_token()
                if shared:
                    config = replace(config, admin_token=shared)
            resolved = ForgejoClient(config=config)
        from services.agent_surface import RestrictedForgejoClient

        return RestrictedForgejoClient(resolved).create_pull_request(
            forgejo_repo, head=branch, base=base, title=title, body=body,
        )

    return open_pr_fn


# ── policy parser: pr_policy / auto_fix from the repository ──────────

def build_policy_parser() -> Callable[[str | None], PolicyView]:
    """Turn ``.agent/review-policy.yml`` into the runner's :class:`PolicyView`."""
    def parser(text: str | None) -> PolicyView:
        if text is None or not text.strip():
            document = builtin_default()
        else:
            document = parse_document(parse_yaml(text), source=SOURCE_FILE)
        return PolicyView(
            source=document.policy_source,
            hash=document.policy_hash,
            max_findings_per_run=document.defaults.max_findings_per_run,
            raw=text,
            warnings=tuple(document.warnings),
            pr_policy=document.defaults.pr_policy,
            auto_fix=document.defaults.auto_fix,
            # §7.2 ``checks:`` — the second suite-resolution provider.
            checks=tuple(entry.model_dump() for entry in document.checks),
            # DESIGN-ai-checks.md §B — the curator trigger policy.
            curator=document.defaults.curator,
            curator_min_interval_seconds=document.defaults.curator_min_interval_seconds,
        )

    return parser


# ── findings ingestion (defect A2) ───────────────────────────────────

def _make_ingest(
    repo_id: int,
    run_id: int,
    sessions: Callable[[], SASession],
) -> Callable[[str, Result], None]:
    """Fold one validated §9.5 document into the findings table."""
    def ingest(task_id: str, result: Result) -> None:
        from services.findings import ingest as findings_ingest

        session = sessions()
        try:
            summary = findings_ingest(
                repo_id,
                run_id,
                [item.model_dump() for item in result.findings],
                session=session,
            )
            run = session.get(ReviewRun, run_id)
            if run is not None:
                run.findings_new = int(summary.get("new", 0) or 0)
                run.findings_matched = int(summary.get("matched", 0) or 0)
                session.commit()
            logger.info(
                "agent task %s: findings ingested (new=%s matched=%s reactivated=%s)",
                task_id, summary.get("new"), summary.get("matched"),
                summary.get("reactivated"),
            )
        finally:
            session.close()

    return ingest


def _open_review_run(
    claimed: ClaimedTask,
    task: TaskRequest,
    sessions: Callable[[], SASession],
) -> int:
    """Create the ``review_runs`` row I5 requires *before* findings are written."""
    session = sessions()
    try:
        run = ReviewRun(
            repo_id=int(claimed.repo_id),
            agent_task_id=int(claimed.id),
            commit_sha=task.commit_sha or "",
            status="running",
        )
        session.add(run)
        session.commit()
        return int(run.id)
    finally:
        session.close()


def _record_pr_link(
    task_id: str, outcome: Any, sessions: Callable[[], SASession],
) -> None:
    """Persist the pull request a task opened — the merge webhook's finding link.

    The runner's ``TaskSink`` cannot do this in production: the worker is
    engine-driven and Flask-free, so the sink's global ``Session`` is unbound,
    while the queue Worker (not the sink) owns the task row.  Best-effort, like
    ``record_check_artifacts``: the PR is already open and the run already
    succeeded, so losing the link must not fail the task.  The cost is that a
    later merge writes no ``pr_url`` — the backfill fails closed rather than
    stamping the wrong findings.
    """
    pr_url = str(getattr(outcome, "pr_url", None) or "")
    if not pr_url:
        return
    session = sessions()
    try:
        task = session.get(AgentTask, int(task_id))
        if task is not None:
            task.pr_url = pr_url
            session.commit()
    except Exception as exc:  # noqa: BLE001 - a link is not worth a failed task
        session.rollback()
        logger.warning("could not record the PR link for task %s: %s", task_id, exc)
    finally:
        session.close()


def _finish_review_run(run_id: int, outcome: Any, sessions: Callable[[], SASession]) -> None:
    """Record the run's policy hash, gate counts and terminal status."""
    session = sessions()
    try:
        run = session.get(ReviewRun, run_id)
        if run is None:
            return
        meta = outcome.run or {}
        if meta.get("policy_hash"):
            run.policy_hash = str(meta["policy_hash"])
        gates = tuple(outcome.gates or ())
        run.gates_total = len(gates)
        run.gates_passed = sum(1 for gate in gates if gate.get("passed"))
        run.gates_failed = run.gates_total - run.gates_passed
        run.status = "ok" if outcome.status == "done" else "error"
        run.log_ref = outcome.result_ref
        run.finished_at = utcnow()
        session.commit()
    finally:
        session.close()


# ── Check-suite provenance, validation and history (DESIGN-ai-checks.md §C) ──

def record_check_artifacts(
    *,
    sessions: Callable[[], SASession],
    repo_id: int,
    task: TaskRequest,
    outcome: Any,
    model: str = "",
) -> None:
    """Fold a run's suite provenance, validation and history into the DB.

    Deliberately best-effort: this is an *index* of what the repository tree
    already says (``services.check_store`` documents that), so a failure to
    record must never turn a successful run into a failed task.  It is also the
    production reader for the "never failed / flaky" findings, so it runs for
    every kind, not just ``checks``.
    """
    from services.check_store import (
        record_proposal_snapshot,
        record_runs,
        record_validation,
    )
    from services.check_validation import CheckValidation, record_from_gate
    from services.gates import CheckCommand, CheckSuite, GateResult

    meta = outcome.run or {}
    records = []
    for gate in tuple(outcome.gates or ()):
        try:
            result = GateResult.model_validate(dict(gate))
        except Exception:  # noqa: BLE001 - a malformed gate row is skipped
            continue
        records.append(record_from_gate(
            result,
            suite_hash=str(meta.get("suite_hash") or ""),
            commit_sha=task.commit_sha,
            validation="validated",
        ))
    for gate in tuple(getattr(outcome, "ai_gates", ()) or ()):
        try:
            result = GateResult.model_validate(dict(gate))
        except Exception:  # noqa: BLE001
            continue
        records.append(record_from_gate(
            result,
            suite_hash=str(meta.get("ai_suite_hash") or ""),
            commit_sha=task.commit_sha,
            validation="unvalidated",
        ))

    report = getattr(outcome, "curator", None)
    session = sessions()
    try:
        if records:
            record_runs(session, repo_id=repo_id, records=records)
        if isinstance(report, Mapping):
            checks = []
            for item in report.get("checks") or ():
                if not isinstance(item, Mapping):
                    continue
                try:
                    checks.append(CheckCommand.model_validate(dict(item)))
                except Exception:  # noqa: BLE001
                    continue
            if checks:
                record_proposal_snapshot(
                    session,
                    repo_id=repo_id,
                    suite=CheckSuite(
                        checks=checks,
                        source=str(report.get("source") or "agent-checks"),
                        reason="curator proposal",
                    ),
                    base_sha=task.commit_sha,
                    model=str(model or ""),
                    task_id=int(task.task_id) if str(task.task_id).isdigit() else None,
                )
            for item in report.get("validations") or ():
                if not isinstance(item, Mapping):
                    continue
                record_validation(
                    session,
                    repo_id=repo_id,
                    result=CheckValidation(
                        check_id=str(item.get("check_id") or ""),
                        status=str(item.get("status") or "unvalidated"),
                        baseline_passed=bool(item.get("baseline_passed")),
                        faults_attempted=int(item.get("faults_attempted") or 0),
                        faults_detected=int(item.get("faults_detected") or 0),
                        detected_by=tuple(item.get("detected_by") or ()),
                        reason=str(item.get("reason") or ""),
                    ),
                    base_sha=task.commit_sha,
                )
        session.commit()
        logger.info(
            "agent task %s: recorded %d check run(s), curator=%s",
            task.task_id, len(records), bool(report),
        )
    except Exception as exc:  # noqa: BLE001 - an index failure is not a task failure
        session.rollback()
        logger.warning("could not record check artifacts for task %s: %s", task.task_id, exc)
    finally:
        session.close()


# ── The handler factory ──────────────────────────────────────────────

def build_handler(
    *,
    engine: Any,
    models_file: str | Path | None = None,
    model_route: str | None = None,
    review_command: str | None = None,
    session_factory: Callable[[], SASession] | None = None,
    forgejo_client: Any | None = None,
) -> TaskHandler:
    """Build the production :data:`TaskHandler` the queue worker runs.

    A handler is *stateless across tasks*: every task gets a fresh runner, sink
    and adapter, so one task's model credential env or work directory can never
    leak into the next.
    """
    sessions = session_factory or sqlalchemy_session_factory(engine)
    route_file = Path(models_file or os.environ.get(ENV_MODELS_FILE) or DEFAULT_MODELS_FILE)
    model_env = _resolve_model_env(route_file, model_route)
    review_fn = build_review_fn(review_command, model_env=model_env)
    policy_parser = build_policy_parser()

    def handle(claimed: ClaimedTask) -> str | None:
        loaded = _load_task(claimed, sessions)
        task, slug = loaded.task, loaded.slug
        # The repository's own credential, else the shared deployment token, else
        # "" — which leaves the fix/push path to fail loudly at git while a
        # review-only task, which never authenticates for a write, still runs.
        git_token = loaded.credential.token if loaded.credential is not None else ""
        run_id = _open_review_run(claimed, task, sessions)
        logger.info(
            "agent task %s: repo=%s kind=%s sha=%s run=%s runner_credential=%s",
            claimed.id, slug, task.kind, task.commit_sha[:12] or "<default-branch>", run_id,
            loaded.credential.source if loaded.credential is not None else "none",
        )
        sink = DbTaskSink(
            ingest=_make_ingest(int(claimed.repo_id), run_id, sessions),
            # The queue Worker owns running → done|failed so retry, back-off and
            # dead-lettering stay authoritative (see DbTaskSink).
            manage_status=False,
        )
        adapter = SubprocessRunnerAdapter(
            review_fn=review_fn,
            search_fn=build_search_fn(int(claimed.repo_id), sessions=sessions),
            open_pr_fn=build_open_pr_fn(slug, client=forgejo_client, token=git_token),
            # The single credential for both the clone/push and the PR API: this
            # repository's runner credential when it has one, else the shared
            # deployment token.  The host pin means the credential helper only
            # ever answers for this task's repo.
            git_token=git_token,
            git_host=git_host_of(task.repo_url),
        )
        runner = AgentRunner(
            adapter=adapter,
            sink=sink,
            model_env=model_env,
            policy_parser=policy_parser,
            # One logical runner per repo: the checkout lives under that repo's
            # own workspace root, never the shared root a pooled worker defaults
            # to.
            work_root=loaded.workspace_root,
            # Fencing: refuse to push/open a PR if this worker lost the lease
            # while the task ran (a second replica may own it now).
            publish_guard=_make_publish_guard(claimed, sessions),
        )
        outcome = runner.run(task)
        _finish_review_run(run_id, outcome, sessions)
        _record_pr_link(str(claimed.id), outcome, sessions)
        record_check_artifacts(
            sessions=sessions,
            repo_id=int(claimed.repo_id),
            task=task,
            outcome=outcome,
            model=str(model_env.get(f"{MODEL_ENV_PREFIX}_MODEL") or ""),
        )
        if outcome.status == "done":
            return outcome.result_ref or f"task:{claimed.id}"
        raise AgentRunnerError(outcome.error or f"agent 任务 {claimed.id} 失败")

    return handle


def _resolve_model_env(models_file: Path, route: str | None) -> dict[str, str]:
    """Resolve the route table; a missing/broken file is a warning, not a crash."""
    try:
        return resolve_model_env(models_file, route=route)
    except Exception as exc:  # noqa: BLE001 - review step fails loudly if it matters
        logger.warning("模型路由解析失败（%s）：%s", models_file, exc)
        return {}


__all__ = [
    "DEFAULT_MODELS_FILE",
    "DEFAULT_REVIEW_TIMEOUT",
    "ENV_MODELS_FILE",
    "ENV_REVIEW_COMMAND",
    "ENV_REVIEW_TIMEOUT",
    "ENV_RUNNER_TOKEN",
    "ENV_TASK_KIND",
    "SUPPORTED_KINDS",
    "TASK_KINDS",
    "build_handler",
    "build_open_pr_fn",
    "build_policy_parser",
    "build_review_fn",
    "build_search_fn",
    "record_check_artifacts",
    "runner_token",
    "sqlalchemy_session_factory",
]
