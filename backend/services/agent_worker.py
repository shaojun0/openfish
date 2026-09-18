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
or the work directory.  The Forgejo token likewise lives only inside
:class:`services.repo_import.ForgejoClient` (env → ``ImportConfig``); this module
never reads it.  That is the seam a separate git-credential refactor can replace
without touching the runner.

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
    mask_secrets,
    resolve_model_env,
)
from services.review_policy import (
    SOURCE_FILE,
    builtin_default,
    parse_document,
    parse_yaml,
)
from services.sandbox_env import sandbox_env

logger = logging.getLogger("cpypiserver.agent_worker")

#: The headless command a deployment configures to ask a model for findings.
#: Empty (the default) makes every review task fail loudly — never "done".
ENV_REVIEW_COMMAND = "AGENT_REVIEW_COMMAND"

#: The narrow Forgejo credential the runner uses for git push and the PR API.
#: Deliberately **not** ``FORGEJO_ADMIN_TOKEN``: the runner containers execute
#: repository-supplied code, so the admin token stays in the backend container.
#: Empty means the runner can review but cannot publish, and says so loudly.
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


def _load_task(claimed: ClaimedTask, sessions: Callable[[], SASession]) -> tuple[TaskRequest, str]:
    """Turn a claimed queue row into the runner's :class:`TaskRequest`.

    ``slug`` is the Forgejo name when the repository was mirrored (open PRs go
    there), otherwise the platform slug.
    """
    if claimed.kind not in SUPPORTED_KINDS:
        raise AgentRunnerError(
            f"任务类型 {claimed.kind!r} 没有沙箱 runner 处理器（只支持 "
            f"{', '.join(SUPPORTED_KINDS)}）；不要把它当作已完成"
        )
    payload = claimed.payload or {}
    session = sessions()
    try:
        repo = session.get(Repo, int(claimed.repo_id))
        if repo is None:
            raise AgentRunnerError(f"仓库 {claimed.repo_id} 不存在，无法执行任务 {claimed.id}")
        slug = str(repo.forgejo_repo or repo.slug)
        commit_sha = str(payload.get("commit_sha") or "").strip()
        if not commit_sha:
            commit_sha = _latest_commit_sha(session, int(repo.id))
        base_branch = str(repo.default_branch or "main")
        repo_url = _repo_url(slug)
    finally:
        session.close()

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
    return task, slug


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
        try:
            proc = subprocess.run(
                argv,
                cwd=str(cwd),
                capture_output=True,
                text=True,
                env=child,
                timeout=budget,
                check=False,
            )
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
    """The runner's narrow Forgejo credential, or ``""`` when unconfigured."""
    return (os.environ.get(ENV_RUNNER_TOKEN) or "").strip()


def build_open_pr_fn(
    forgejo_repo: str,
    *,
    client: Any | None = None,
) -> Callable[..., str]:
    """A ``open_pr_fn`` bound to one repository.

    Without an injected *client* this builds a :class:`ForgejoClient` whose token
    is ``FORGEJO_RUNNER_TOKEN`` — **never** the platform's
    ``FORGEJO_ADMIN_TOKEN``.  The runner executes repository-supplied code, so it
    gets the narrow, revocable credential and not the instance-wide admin one;
    an unset runner token means PR creation fails loudly instead of silently
    borrowing admin authority.

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
            token = runner_token()
            if token:
                config = replace(config, admin_token=token)
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
        task, slug = _load_task(claimed, sessions)
        run_id = _open_review_run(claimed, task, sessions)
        logger.info(
            "agent task %s: repo=%s kind=%s sha=%s run=%s",
            claimed.id, slug, task.kind, task.commit_sha[:12] or "<default-branch>", run_id,
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
            open_pr_fn=build_open_pr_fn(slug, client=forgejo_client),
            # The single narrow credential for both the clone/push and the PR
            # API; the compose env hands it in and nothing else.
            git_token=runner_token(),
        )
        runner = AgentRunner(
            adapter=adapter,
            sink=sink,
            model_env=model_env,
            policy_parser=policy_parser,
            # Fencing: refuse to push/open a PR if this worker lost the lease
            # while the task ran (a second replica may own it now).
            publish_guard=_make_publish_guard(claimed, sessions),
        )
        outcome = runner.run(task)
        _finish_review_run(run_id, outcome, sessions)
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
