#!/usr/bin/env python
"""Gate: the S4 agent runtime works end-to-end with **no** external service.

Run from the backend directory (`backend/`)::

    python scripts/check_agent_runtime.py

The runner's whole job is to drive a sandbox: clone a repo, run gates, ask a
model, search history, emit a result and (in fix mode) push a branch.  None of
that can run in CI, so every external effect sits behind an injectable adapter
and this gate supplies recording fakes.  It proves the *orchestration* and the
four rules that are cheap to get wrong:

* **§9.3 order** — the six steps run in order, each exactly once.
* **I4** — a push to ``main``/``master`` is refused; ``agent/*`` is allowed.
* **§9.4 gates** — normalisation, per-gate timeout and output tailing.
* **§9.5 schema** — a document missing ``rule_id``/``gates`` or carrying a wrong
  type fails the *task* and writes no finding.
* **result-gated PRs** — ``pr_policy=on_green`` never pushes on a red gate,
  ``never`` never pushes, ``always`` is the documented escape hatch, and a fix
  with no commit never pushes.
* **defects A1/A2** — the production worker builds a real handler (not
  ``placeholder_handler``), an unconfigured ``AGENT_REVIEW_COMMAND`` fails the
  task, and a validated result really reaches the ``findings`` table.
* the 24h work-directory rule, and that a model key never lands in a log or a
  result document.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
os.chdir(REPO_ROOT)

from config import settings  # noqa: E402
from config.agent import AgentConfig  # noqa: E402
from models.agent_hub import Finding, Repo  # noqa: E402
from services import agent_worker  # noqa: E402
from services.agent_queue import (  # noqa: E402
    AgentQueue,
    Worker,
    build_engine,
    build_worker,
    default_handler,
    placeholder_handler,
)
from services.agent_runner import (  # noqa: E402
    STEPS,
    AgentRunner,
    AgentRunnerError,
    ProtectedBranchError,
    ReadContext,
    ResultValidationError,
    TaskRequest,
    TaskSink,
    assert_pushable,
    mark_finished,
    remove_workdirs,
    sweep_workdirs,
    validate_result,
    workdir_for,
)
from services.gates import (  # noqa: E402
    GATE_GLOB,
    STDOUT_TAIL_LIMIT,
    TIMEOUT_EXIT_CODE,
    TRUNCATION_MARKER,
    ExecOutcome,
    GateResult,
    GateSummary,
    discover_gates,
    render_summary,
    run_gates,
    scripts_root,
)
from services.repo_import import (  # noqa: E402
    ForgejoClient,
    ForgejoError,
    ImportConfig,
)
from services.review_policy import (  # noqa: E402
    PolicyValidationError,
    parse_document,
)

_SECRET = "sk-live-DEADBEEF-0123456789"

#: Expected task failures are logged deliberately; keep them out of the gate's
#: stdout so the ✅/❌ report stays readable.
logging.getLogger("cpypiserver.agent_runner").addHandler(logging.NullHandler())
logging.getLogger("cpypiserver.agent_worker").addHandler(logging.NullHandler())

_problems: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    if ok:
        print(f"✅ {label}")
    else:
        _problems.append(label)
        print(f"❌ {label}" + (f" — {detail}" if detail else ""))


# ── Fakes (no git, no docker, no model, no network) ──────────────────

class RecordingSink(TaskSink):
    def __init__(self) -> None:
        self.running: list[str] = []
        self.failed: list[tuple[str, str]] = []
        self.recorded: list[tuple[str, Any]] = []
        self.done: list[tuple[str, dict[str, Any]]] = []

    def mark_running(self, task_id: str) -> None:
        self.running.append(task_id)

    def mark_failed(self, task_id: str, reason: str) -> None:
        self.failed.append((task_id, reason))

    def record_findings(self, task_id: str, result: Any) -> None:
        self.recorded.append((task_id, result))

    def mark_done(self, task_id: str, *, result_ref: str, pr_url: str | None = None) -> None:
        self.done.append((task_id, {"result_ref": result_ref, "pr_url": pr_url}))


class FakeAdapter:
    """Records every external call so the order can be asserted."""

    def __init__(
        self,
        findings: Sequence[Mapping[str, Any]] | None = None,
        *,
        gates: Sequence[GateResult] | None = None,
        changed: bool = True,
        policy_text: str | None = None,
        changed_paths: Sequence[str] | None = None,
    ) -> None:
        self.calls: list[str] = []
        self.findings = list(findings or [])
        self.gates = list(
            gates
            or [GateResult(gate="check_lint", passed=True, exit_code=0, duration_ms=4)]
        )
        self.emitted: dict[str, Any] | None = None
        self.pushed: list[str] = []
        self.changed = changed
        self.committed: list[str] = []
        self.policy_text = policy_text
        #: What the run's edits look like to the path guard.  Empty by default:
        #: a plain fix, not one that touched the frozen suite.
        self.changed_paths_list = list(changed_paths or [])
        #: The frozen suite handed to ``run_gates`` (P1.3) and the role handed
        #: to ``review`` (P1.4), recorded so a check can assert both.
        self.suites: list[Any] = []
        self.review_kinds: list[str] = []

    def set_model_env(self, env: Mapping[str, str]) -> None:
        self.calls.append("set_model_env")

    def prepare(self, workdir: Path) -> None:
        Path(workdir).mkdir(parents=True, exist_ok=True)

    def clone(self, workdir: Path, *, repo_url: str, commit_sha: str) -> None:
        self.calls.append("clone")

    def read_context(self, workdir: Path) -> ReadContext:
        self.calls.append("read")
        return ReadContext(agents_md="# AGENTS", policy_text=self.policy_text)

    def run_gates(
        self,
        workdir: Path,
        *,
        timeout: float,
        suite: Any | None = None,
    ) -> GateSummary:
        self.calls.append("gates")
        self.suites.append(suite)
        passed = sum(1 for item in self.gates if item.passed)
        return GateSummary(
            gates=self.gates,
            total=len(self.gates),
            passed=passed,
            failed=len(self.gates) - passed,
        )

    def changed_paths(self, workdir: Path) -> Sequence[str]:
        return list(self.changed_paths_list)

    def review(self, workdir: Path, *, policy: Any, commit_sha: str, context: ReadContext,
               kind: str = "review"):
        self.calls.append("review")
        self.review_kinds.append(kind)
        return self.findings

    def search(self, workdir: Path, *, finding: Mapping[str, Any], commit_sha: str, context: ReadContext):
        self.calls.append("search")
        return finding

    def emit(self, workdir: Path, *, payload: Mapping[str, Any]) -> str:
        self.calls.append("emit")
        self.emitted = dict(payload)
        return str(Path(workdir) / "result.json")

    def push(
        self, workdir: Path, *, branch: str, commit_sha: str, repo_url: str = "",
    ) -> None:
        self.calls.append("push")
        self.pushed.append(branch)

    def commit(
        self,
        workdir: Path,
        *,
        branch: str,
        commit_sha: str,
        message: str,
    ) -> bool:
        self.calls.append("commit")
        if not self.changed:
            raise AgentRunnerError("no changes to commit")
        self.committed.append(branch)
        return True

    def open_pr(self, workdir: Path, *, branch: str, base: str, title: str, body: str) -> str:
        self.calls.append("open_pr")
        return "http://forgejo.local/o/r/pulls/1"


class FakeExecutor:
    def __init__(self, *, codes: Sequence[int] = (), output: str = "") -> None:
        self.codes = list(codes)
        self.output = output
        self.calls: list[tuple[str, float]] = []

    def run(self, script: Path, *, timeout: float) -> ExecOutcome:
        self.calls.append((script.name, timeout))
        code = self.codes.pop(0) if self.codes else 0
        return ExecOutcome(code, self.output, False, 7)


class SlowExecutor(FakeExecutor):
    """A fake that reports a timeout whenever the budget is below 1s."""

    def run(self, script: Path, *, timeout: float) -> ExecOutcome:
        self.calls.append((script.name, timeout))
        time.sleep(0.001)
        if timeout < 1.0:
            return ExecOutcome(TIMEOUT_EXIT_CODE, self.output + "[timeout]", True, int(timeout * 1000))
        return ExecOutcome(0, self.output, False, 1)


def _finding(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "rule_id": "backend.no-typing-optional",
        "level": "blocking",
        "severity": "medium",
        "file_path": "backend/services/foo.py",
        "symbol": "FooService.bar",
        "title": "标题",
        "detail": "细节",
        "line_hint": 42,
        "evidence": [],
        "autofix": False,
    }
    data.update(overrides)
    return data


# ── §9.3: the six steps, in order ────────────────────────────────────

def check_protocol_order() -> None:
    print("\n§9.3 六步流程")
    with tempfile.TemporaryDirectory(prefix="agent-runtime-") as root:
        adapter = FakeAdapter([_finding()])
        sink = RecordingSink()
        runner = AgentRunner(adapter=adapter, sink=sink, work_root=root)
        outcome = runner.run(
            TaskRequest(task_id="1", repo_url="http://forgejo/o/r.git", commit_sha="a" * 40)
        )
        check("任务成功结束", outcome.status == "done", outcome.error or "")
        check("六步按序执行", outcome.steps == STEPS, repr(outcome.steps))
        external = [call for call in adapter.calls if call != "set_model_env"]
        check(
            "每一步都真的调用了 adapter",
            external == ["clone", "read", "gates", "review", "search", "emit"],
            repr(adapter.calls),
        )
        check("完成的运行写入了 .done 标记", (Path(root) / "1" / ".done").exists())
        check("sink 收到 running/done", sink.running == ["1"] and len(sink.done) == 1)
        check("sink 收到 finding", len(sink.recorded) == 1)


# ── I4: the push guard ───────────────────────────────────────────────

def check_push_guard() -> None:
    print("\nI4 分支守卫")
    rejected = []
    for branch in ("main", "master", "refs/heads/main", "fix/thing", "", "agent/x..y"):
        try:
            assert_pushable(branch)
            rejected.append(branch)
        except ProtectedBranchError:
            pass
    check("保护分支/非 agent 前缀被拒", not rejected, f"漏网：{rejected}")
    try:
        allowed = assert_pushable("agent/fix-123")
        check("agent/fix-123 通过", allowed == "agent/fix-123")
    except ProtectedBranchError as exc:
        check("agent/fix-123 通过", False, str(exc))

    with tempfile.TemporaryDirectory(prefix="agent-runtime-") as root:
        sink = RecordingSink()
        adapter = FakeAdapter([])
        runner = AgentRunner(adapter=adapter, sink=sink, work_root=root)
        outcome = runner.run(
            TaskRequest(
                task_id="2",
                repo_url="http://forgejo/o/r.git",
                commit_sha="b" * 40,
                kind="fix",
                branch="main",
            )
        )
        check("fix 模式推 main 被拒且任务 failed", outcome.status == "failed" and not adapter.pushed)
        check("推 main 失败时无 finding 入库", sink.recorded == [])

    with tempfile.TemporaryDirectory(prefix="agent-runtime-") as root:
        sink = RecordingSink()
        adapter = FakeAdapter([])
        runner = AgentRunner(adapter=adapter, sink=sink, work_root=root)
        outcome = runner.run(
            TaskRequest(
                task_id="3",
                repo_url="http://forgejo/o/r.git",
                commit_sha="c" * 40,
                kind="fix",
                branch="agent/fix-3",
            )
        )
        check(
            "fix 模式先 commit 再 push",
            outcome.status == "done"
            and adapter.committed == ["agent/fix-3"]
            and adapter.calls.index("commit") < adapter.calls.index("push"),
            f"calls={adapter.calls}",
        )

    with tempfile.TemporaryDirectory(prefix="agent-runtime-") as root:
        sink = RecordingSink()
        adapter = FakeAdapter([], changed=False)
        runner = AgentRunner(adapter=adapter, sink=sink, work_root=root)
        outcome = runner.run(
            TaskRequest(
                task_id="4",
                repo_url="http://forgejo/o/r.git",
                commit_sha="d" * 40,
                kind="fix",
                branch="agent/fix-4",
            )
        )
        check(
            "无改动的 fix 任务失败且不 push、不开 PR",
            outcome.status == "failed"
            and not adapter.pushed
            and "open_pr" not in adapter.calls
            and sink.recorded == [],
            f"calls={adapter.calls}",
        )


#: A gate suite carrying one failure — the trigger for `on_green` refusals.
RED_GATES = [GateResult(gate="check_lint", passed=False, exit_code=1, duration_ms=3)]


def _fix_runner(adapter: "FakeAdapter", sink: RecordingSink, root: str, **kwargs: Any) -> AgentRunner:
    return AgentRunner(adapter=adapter, sink=sink, work_root=root, **kwargs)


# ── result-gated PR policy (§9.3 step 6 / C) ─────────────────────────

def check_pr_policy() -> None:
    print("\n结果门控的 PR 策略（on_green / always / never / auto_fix）")

    # on_green (the default) must refuse to push when a gate is red.
    with tempfile.TemporaryDirectory(prefix="agent-runtime-") as root:
        sink = RecordingSink()
        adapter = FakeAdapter([], gates=RED_GATES)
        outcome = _fix_runner(adapter, sink, root).run(TaskRequest(
            task_id="10", repo_url="u", commit_sha="a" * 40,
            kind="fix", branch="agent/fix-10",
        ))
        check(
            "on_green：gates 红 → 不 push、不开 PR",
            outcome.status == "failed" and not adapter.pushed and "open_pr" not in adapter.calls,
            f"status={outcome.status} calls={adapter.calls}",
        )
        check("on_green：失败原因带上具体的失败 gate",
              "check_lint" in (outcome.error or ""), str(outcome.error))

    # on_green with green gates still pushes (the happy path).
    with tempfile.TemporaryDirectory(prefix="agent-runtime-") as root:
        sink = RecordingSink()
        adapter = FakeAdapter([])
        outcome = _fix_runner(adapter, sink, root).run(TaskRequest(
            task_id="11", repo_url="u", commit_sha="b" * 40,
            kind="fix", branch="agent/fix-11",
        ))
        check(
            "on_green：gates 绿 → push + 开 PR",
            outcome.status == "done" and adapter.pushed == ["agent/fix-11"]
            and "open_pr" in adapter.calls,
            f"status={outcome.status} calls={adapter.calls}",
        )

    # never is report-only: no commit, no push, no PR — but the task still ends.
    with tempfile.TemporaryDirectory(prefix="agent-runtime-") as root:
        sink = RecordingSink()
        adapter = FakeAdapter([])
        outcome = _fix_runner(adapter, sink, root, pr_policy="never").run(TaskRequest(
            task_id="12", repo_url="u", commit_sha="c" * 40,
            kind="fix", branch="agent/fix-12",
        ))
        check(
            "never：不 commit、不 push、不开 PR，任务仍 done",
            outcome.status == "done" and not adapter.pushed
            and adapter.committed == [] and "open_pr" not in adapter.calls,
            f"status={outcome.status} calls={adapter.calls}",
        )

    # always is the documented escape hatch: push even with a red gate.
    with tempfile.TemporaryDirectory(prefix="agent-runtime-") as root:
        sink = RecordingSink()
        adapter = FakeAdapter([], gates=RED_GATES)
        outcome = _fix_runner(adapter, sink, root, pr_policy="always").run(TaskRequest(
            task_id="13", repo_url="u", commit_sha="d" * 40,
            kind="fix", branch="agent/fix-13",
        ))
        check(
            "always：gates 红也 commit + push + 开 PR（已文档化）",
            outcome.status == "done" and adapter.pushed == ["agent/fix-13"]
            and "open_pr" in adapter.calls,
            f"status={outcome.status} calls={adapter.calls}",
        )

    # §6.4: a push-triggered review only escalates when auto_fix is on.
    with tempfile.TemporaryDirectory(prefix="agent-runtime-") as root:
        sink = RecordingSink()
        adapter = FakeAdapter([], gates=RED_GATES)
        outcome = _fix_runner(adapter, sink, root).run(TaskRequest(
            task_id="14", repo_url="u", commit_sha="e" * 40, kind="review",
        ))
        check(
            "auto_fix 默认关：review 不升级为 fix（不 commit/push）",
            outcome.status == "done" and adapter.committed == [] and not adapter.pushed,
            f"status={outcome.status} calls={adapter.calls}",
        )

    with tempfile.TemporaryDirectory(prefix="agent-runtime-") as root:
        sink = RecordingSink()
        adapter = FakeAdapter([], gates=RED_GATES)
        outcome = _fix_runner(adapter, sink, root, auto_fix=True).run(TaskRequest(
            task_id="15", repo_url="u", commit_sha="f" * 40, kind="review",
            branch="agent/fix-15",
        ))
        check(
            "auto_fix 开 + gates 红：review 升级为 fix（先 commit）",
            adapter.committed == ["agent/fix-15"],
            f"status={outcome.status} calls={adapter.calls}",
        )
        check("升级后仍按 on_green 门控：gates 红则不 push",
              outcome.status == "failed" and not adapter.pushed,
              f"status={outcome.status} calls={adapter.calls}")

    with tempfile.TemporaryDirectory(prefix="agent-runtime-") as root:
        sink = RecordingSink()
        adapter = FakeAdapter([], gates=[GateResult(gate="check_lint", passed=True, exit_code=0)])
        outcome = _fix_runner(adapter, sink, root, auto_fix=True).run(TaskRequest(
            task_id="16", repo_url="u", commit_sha="1" * 40, kind="review",
            branch="agent/fix-16",
        ))
        check(
            "auto_fix 开但 gates 全绿：无可修项，review 不升级",
            outcome.status == "done" and adapter.committed == [] and not adapter.pushed,
            f"status={outcome.status} calls={adapter.calls}",
        )

    # C1: the repository's .agent/review-policy.yml also configures the knob.
    policy_parser = agent_worker.build_policy_parser()
    never_policy = "version: 1\ndefaults:\n  pr_policy: never\n  auto_fix: true\n"
    view = policy_parser(never_policy)
    check("policy 文件解析出 pr_policy/auto_fix",
          view.pr_policy == "never" and view.auto_fix is True,
          f"pr_policy={view.pr_policy} auto_fix={view.auto_fix}")

    with tempfile.TemporaryDirectory(prefix="agent-runtime-") as root:
        sink = RecordingSink()
        adapter = FakeAdapter([], policy_text=never_policy)
        outcome = AgentRunner(
            adapter=adapter, sink=sink, work_root=root, policy_parser=policy_parser,
        ).run(TaskRequest(
            task_id="17", repo_url="u", commit_sha="2" * 40,
            kind="fix", branch="agent/fix-17",
        ))
        check("policy 文件 pr_policy=never：不 commit/push/开 PR",
              outcome.status == "done" and adapter.committed == []
              and not adapter.pushed and "open_pr" not in adapter.calls,
              f"status={outcome.status} calls={adapter.calls}")

    with tempfile.TemporaryDirectory(prefix="agent-runtime-") as root:
        sink = RecordingSink()
        adapter = FakeAdapter(
            [], gates=RED_GATES, policy_text="version: 1\ndefaults:\n  auto_fix: true\n",
        )
        outcome = AgentRunner(
            adapter=adapter, sink=sink, work_root=root, policy_parser=policy_parser,
        ).run(TaskRequest(
            task_id="18", repo_url="u", commit_sha="3" * 40, kind="review",
            branch="agent/fix-18",
        ))
        check("policy 文件 auto_fix=true：review 升级为 fix",
              adapter.committed == ["agent/fix-18"],
              f"status={outcome.status} calls={adapter.calls}")

    # An unknown value in the schema is rejected, not silently defaulted.
    try:
        parse_document({"version": 1, "defaults": {"pr_policy": "sometimes"}})
        check("非法 pr_policy 被 policy schema 拒绝", False, "竟然通过了")
    except PolicyValidationError:
        check("非法 pr_policy 被 policy schema 拒绝", True)

    # The deployment-wide override wins over the file and is normalised.  It is
    # configured on the settings object rather than through ``os.environ``: the
    # settings are resolved once at start-up and are the only reader of the
    # environment, so a runtime re-read — which anything running in this process
    # could have used to re-point publication — no longer exists.  The variable
    # name is asserted first, so the field and the documented knob cannot drift.
    check("AGENT_PR_POLICY 仍然映射到 settings.agent.pr_policy",
          AgentConfig.env_name("pr_policy") == "AGENT_PR_POLICY",
          AgentConfig.env_name("pr_policy"))
    previous_policy = settings.agent.pr_policy
    settings.agent.pr_policy = "NEVER"
    try:
        with tempfile.TemporaryDirectory(prefix="agent-runtime-") as root:
            sink = RecordingSink()
            adapter = FakeAdapter([], policy_text="version: 1\ndefaults:\n  pr_policy: always\n")
            outcome = AgentRunner(
                adapter=adapter, sink=sink, work_root=root, policy_parser=policy_parser,
            ).run(TaskRequest(
                task_id="19", repo_url="u", commit_sha="4" * 40,
                kind="fix", branch="agent/fix-19",
            ))
        check("AGENT_PR_POLICY 覆盖 policy 文件（normalise 大小写）",
              outcome.status == "done" and adapter.committed == [] and not adapter.pushed,
              f"status={outcome.status} calls={adapter.calls}")
    finally:
        settings.agent.pr_policy = previous_policy


# ── production worker wiring (defects A1 / A2) ───────────────────────

class _FakeClaimed:
    """A ``ClaimedTask``-shaped row; only the fields the handler reads."""

    def __init__(self, *, kind: str, task_id: int = 1, repo_id: int = 1) -> None:
        self.id = task_id
        self.repo_id = repo_id
        self.kind = kind
        self.payload: dict[str, Any] = {}


def check_findings_ingestion() -> None:
    """Defect A2: a validated result must actually reach the findings table."""
    print("\nfindings 落库（缺陷 A2）")
    with tempfile.TemporaryDirectory(prefix="agent-ingest-") as root:
        engine = build_engine(f"sqlite:///{root}/queue.db")
        queue = AgentQueue(engine)
        queue.ensure_schema()
        try:
            session = queue.session()
            repo = Repo(slug="gate/ingest", kind="workspace")
            session.add(repo)
            session.commit()
            repo_id = int(repo.id)
            session.close()

            queued_id = queue.enqueue(repo_id, kind="review", payload={})
            claimed = _FakeClaimed(kind="review", task_id=queued_id, repo_id=repo_id)
            task = TaskRequest(
                task_id=str(queued_id), repo_url="u", commit_sha="a" * 40,
                kind="review", repo="gate/ingest",
            )
            sessions = agent_worker.sqlalchemy_session_factory(engine)
            run_id = agent_worker._open_review_run(claimed, task, sessions)

            result = validate_result({
                "run": {
                    "commit_sha": "a" * 40, "policy_hash": "h",
                    "started_at": "2026-01-01T00:00:00Z",
                },
                "findings": [_finding()],
                "gates": [{"gate": "check_lint", "passed": True, "exit_code": 0}],
            })
            agent_worker._make_ingest(repo_id, run_id, sessions)(str(queued_id), result)

            session = sessions()
            try:
                rows = session.query(Finding).filter(Finding.repo_id == repo_id).all()
            finally:
                session.close()
            check(
                "ingest 把 finding 写进 findings 表",
                len(rows) == 1 and rows[0].rule_id == "backend.no-typing-optional",
                f"rows={[(r.rule_id, r.fingerprint) for r in rows]}",
            )
        finally:
            engine.dispose()


def check_worker_wiring() -> None:
    print("\n§9.1 生产 worker 装配真实 runner（不是 placeholder）")
    with tempfile.TemporaryDirectory(prefix="agent-runtime-") as root:
        engine = build_engine(f"sqlite:///{root}/queue.db")
        queue = AgentQueue(engine)
        queue.ensure_schema()
        try:
            handler = default_handler(queue)
            check("default_handler 不是 placeholder_handler",
                  handler is not placeholder_handler)
            check("Worker 在不注入 handler 时也不用 placeholder",
                  Worker(queue).handler is not placeholder_handler)

            fake = lambda task: "fake"  # noqa: E731 - deliberately a tiny sentinel
            seen: list[Any] = []
            worker = build_worker(
                queue, handler_factory=lambda q: (seen.append(q), fake)[1]
            )
            check("build_worker 的 handler_factory 注入生效",
                  worker.handler is fake and seen == [queue])

            built = agent_worker.build_handler(engine=engine)
            check("agent_worker.build_handler 返回真实处理器",
                  callable(built) and built is not placeholder_handler)

            # A kind this sandbox runner cannot run must fail loudly, never be
            # retired as if it had run.
            try:
                built(_FakeClaimed(kind="backfill"))
                check("未支持的 kind 大声失败（不静默退休）", False, "竟然返回了")
            except AgentRunnerError:
                check("未支持的 kind 大声失败（不静默退休）", True)
        finally:
            engine.dispose()


def check_review_command() -> None:
    print("\nreview 命令：未配置必须失败，配置了才产出 findings")
    unset = agent_worker.build_review_fn("")
    try:
        unset(Path(tempfile.gettempdir()), policy=None, commit_sha="a" * 40, context=None)
        check("空 AGENT_REVIEW_COMMAND → 抛错（任务失败而非 done）", False, "竟然返回了")
    except AgentRunnerError as exc:
        check("空 AGENT_REVIEW_COMMAND → 抛错（任务失败而非 done）",
              "AGENT_REVIEW_COMMAND" in str(exc), str(exc))

    with tempfile.TemporaryDirectory(prefix="agent-review-") as root:
        script = Path(root) / "review.py"
        script.write_text(
            "import json, sys\n"
            "print(json.dumps([{'rule_id': 'backend.logger-name'}]))\n",
            encoding="utf-8",
        )
        review_fn = agent_worker.build_review_fn(f"{sys.executable} {script}")
        findings = review_fn(Path(root), policy=None, commit_sha="b" * 40, context=None)
        check("配置了命令时解析 findings JSON",
              len(findings) == 1 and findings[0]["rule_id"] == "backend.logger-name",
              repr(findings))

        failing = Path(root) / "fail.py"
        failing.write_text("import sys; sys.exit(3)\n", encoding="utf-8")
        broken = agent_worker.build_review_fn(f"{sys.executable} {failing}")
        try:
            broken(Path(root), policy=None, commit_sha="c" * 40, context=None)
            check("review 命令非零退出 → 抛错", False, "竟然返回了")
        except AgentRunnerError:
            check("review 命令非零退出 → 抛错", True)


def check_open_pr_seam() -> None:
    """B3: PR creation goes through the Forgejo client and nothing else."""
    print("\nPR 创建接缝（ForgejoClient）")

    class FakeClient:
        def __init__(self) -> None:
            self.calls: list[tuple[Any, ...]] = []

        def create_pull_request(self, repo: str, *, head: str, base: str,
                                title: str, body: str = "") -> str:
            self.calls.append((repo, head, base, title, body))
            return "http://forgejo.local/o/r/pulls/9"

    fake = FakeClient()
    open_pr = agent_worker.build_open_pr_fn("o/r", client=fake)
    url = open_pr(Path("/tmp"), branch="agent/fix-1", base="main",
                  title="t", body="b")
    check("build_open_pr_fn 调 ForgejoClient.create_pull_request",
          fake.calls == [("o/r", "agent/fix-1", "main", "t", "b")]
          and url.endswith("/pulls/9"),
          f"calls={fake.calls} url={url}")

    client = ForgejoClient(config=ImportConfig(base_url="http://forgejo:3000",
                                               admin_token="gate-token"))
    calls: list[tuple[str, str, dict[str, Any]]] = []

    def fake_json(method: str, path: str, **kwargs: Any) -> Any:
        calls.append((method, path, kwargs))
        return {"html_url": "http://forgejo/o/r/pulls/7", "number": 7}

    client._json = fake_json  # type: ignore[method-assign]
    url = client.create_pull_request("o/r", head="agent/fix-2", base="main",
                                     title="标题", body="正文")
    posted = calls[0] if calls else ("", "", {})
    check("create_pull_request POST repos/<slug>/pulls 并回 URL",
          posted[0] == "POST" and posted[1] == "repos/o/r/pulls"
          and posted[2].get("json_body", {}).get("head") == "agent/fix-2"
          and url.endswith("/pulls/7"),
          f"calls={calls} url={url}")
    try:
        client.create_pull_request("noslash", head="a", base="b", title="t")
        check("非法 forgejo_repo 被拒", False, "竟然通过了")
    except ForgejoError:
        check("非法 forgejo_repo 被拒", True)


# ── §9.4: gates normalisation ────────────────────────────────────────

def check_gates() -> None:
    print("\n§9.4 gates 归一化")
    paths = [Path("check_a.py"), Path("check_b.py"), Path("check_c.py")]

    all_green = run_gates(paths, executor=FakeExecutor(codes=[0, 0, 0]), timeout=30.0)
    check(
        "全过：total/passed/failed 正确",
        (all_green.total, all_green.passed, all_green.failed) == (3, 3, 0) and all_green.ok,
        repr(all_green.model_dump()),
    )

    partial = run_gates(paths, executor=FakeExecutor(codes=[0, 1, 0]), timeout=30.0)
    check(
        "部分失败：计数正确",
        (partial.total, partial.passed, partial.failed) == (3, 2, 1) and not partial.ok,
        repr(partial.model_dump()),
    )
    check("失败 gate 带上了 exit_code", partial.gates[1].exit_code == 1)

    timed = run_gates([paths[0]], executor=SlowExecutor(), timeout=0.05)
    check(
        "超时：视作 failed 且标记 timed_out",
        timed.failed == 1
        and timed.gates[0].timed_out
        and not timed.gates[0].passed
        and timed.gates[0].exit_code == TIMEOUT_EXIT_CODE,
        repr(timed.model_dump()),
    )
    check("超时写进汇总文本", "timeout" in render_summary(timed))

    long_output = "x" * (STDOUT_TAIL_LIMIT * 2) + "TAIL-END"
    tailed = run_gates([paths[0]], executor=FakeExecutor(output=long_output), timeout=30.0)
    body = tailed.gates[0].stdout_tail
    check(
        "stdout_tail 被截断且保留尾部",
        len(body) <= STDOUT_TAIL_LIMIT + len(TRUNCATION_MARKER)
        and body.startswith(TRUNCATION_MARKER)
        and body.endswith("TAIL-END")
        and len(body) < len(long_output),
        f"len={len(body)}",
    )

    rendered = render_summary(partial)
    check(
        "render_summary 是 Markdown 表格且带计数",
        "| gate |" in rendered and "❌" in rendered and "2/3" in rendered,
        rendered,
    )

    found = discover_gates(scripts_root())
    names = {path.name for path in found}
    check(
        "gate 发现能找到 check_*.py",
        "check_lint.py" in names and "check_agent_runtime.py" in names,
        f"glob={GATE_GLOB} found={len(found)}",
    )
    check(
        "需要活服务的 check_contract 默认排除",
        "check_contract.py" not in names,
        repr(sorted(names)),
    )


# ── §9.5: schema validation fails the task closed ────────────────────

def check_result_schema() -> None:
    print("\n§9.5 result schema")
    run = {"commit_sha": "c" * 40, "policy_hash": "h", "started_at": "2026-01-01T00:00:00Z"}
    good = {"run": run, "findings": [_finding()], "gates": []}
    try:
        validate_result(good)
        check("合法 payload 通过", True)
    except ResultValidationError as exc:
        check("合法 payload 通过", False, str(exc))

    for label, payload in (
        ("缺 rule_id", {"run": run, "findings": [_finding(rule_id=None)], "gates": []}),
        ("缺 gates", {"run": run, "findings": []}),
        ("类型错误", {"run": run, "findings": [], "gates": [{"gate": "g", "passed": "yes"}]}),
    ):
        try:
            validate_result(payload)
            check(f"{label} 被拒", False, "竟然通过了")
        except ResultValidationError:
            check(f"{label} 被拒", True)

    with tempfile.TemporaryDirectory(prefix="agent-runtime-") as root:
        adapter = FakeAdapter([{"level": "debt", "severity": "low", "file_path": "a", "symbol": "b", "title": "缺 rule_id"}])
        sink = RecordingSink()
        outcome = AgentRunner(adapter=adapter, sink=sink, work_root=root).run(
            TaskRequest(task_id="3", repo_url="u", commit_sha="d" * 40)
        )
        check("非法产出：任务标 failed", outcome.status == "failed" and len(sink.failed) == 1)
        check("非法产出：无 finding 写入", sink.recorded == [])
        check("非法产出：不写 result.json", adapter.emitted is None)


# ── §9.2: 24h work-directory retention ───────────────────────────────

def check_workdir_retention() -> None:
    print("\n§9.2 工作目录 24h 回收")
    now = time.time()
    with tempfile.TemporaryDirectory(prefix="agent-runtime-") as root:
        base = Path(root)
        for name, age in (("expired", 25 * 3600), ("fresh", 3600), ("inflight", None)):
            directory = base / name
            directory.mkdir()
            if age is not None:
                marker = mark_finished(directory)
                os.utime(marker, (now - age, now - age))
        candidates = {path.name for path in sweep_workdirs(now, base)}
        check("只回收到期的任务目录", candidates == {"expired"}, repr(candidates))
        check("未到期目录仍在", (base / "fresh").exists())
        check("未结束（无 .done）目录不回收", (base / "inflight").exists())
        removed = remove_workdirs(sweep_workdirs(now, base))
        check(
            "回收后到期目录消失、其余保留",
            removed == 1 and not (base / "expired").exists() and (base / "fresh").exists(),
        )
    try:
        workdir_for("/work", "../escape")
        check("任务 id 不能逃出工作根", False, "竟然通过了")
    except Exception:
        check("任务 id 不能逃出工作根", True)


# ── secrets never land in a log or a result ──────────────────────────

def check_secret_masking() -> None:
    print("\n密钥不落日志 / 不落结果")
    records: list[str] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record.getMessage())

    agent_logger = logging.getLogger("cpypiserver.agent_runner")
    handler = Capture(level=logging.DEBUG)
    old_level = agent_logger.level
    agent_logger.setLevel(logging.DEBUG)
    agent_logger.addHandler(handler)
    try:
        with tempfile.TemporaryDirectory(prefix="agent-runtime-") as root:
            # The fake model leaks the key into a finding detail; the runner must
            # scrub it before the document is emitted.
            adapter = FakeAdapter([_finding(detail=f"leaked {_SECRET} here")])
            sink = RecordingSink()
            runner = AgentRunner(
                adapter=adapter,
                sink=sink,
                work_root=root,
                model_env={"OPENFISH_MODEL_API_KEY": _SECRET, "OPENFISH_MODEL_BASE_URL": "http://m"},
            )
            outcome = runner.run(
                TaskRequest(task_id="4", repo_url="u", commit_sha="e" * 40)
            )
    finally:
        agent_logger.removeHandler(handler)
        agent_logger.setLevel(old_level)

    log_text = "\n".join(records)
    emitted = json.dumps(adapter.emitted or {}, ensure_ascii=False)
    found = json.dumps(outcome.findings, ensure_ascii=False)
    check("日志中出现过模型配置行", "模型配置" in log_text, log_text[:120])
    check("日志无明文 key", _SECRET not in log_text)
    check("emit 的 result.json 无明文 key", _SECRET not in emitted)
    check("产出的 findings 无明文 key", _SECRET not in found)


# ── main ─────────────────────────────────────────────────────────────

def check_publish_safety() -> None:
    """Publish order, lease fencing and retry isolation (P0-1/P0-2/P1-5)."""
    print("\n发布顺序 / 租约 fencing / 重试隔离")

    # Findings are the durable record of a review and must be written before the
    # non-idempotent push/PR, so a crash after publishing cannot lose them.
    with tempfile.TemporaryDirectory(prefix="agent-runtime-") as root:
        sink = RecordingSink()
        adapter = FakeAdapter([_finding()])
        outcome = AgentRunner(adapter=adapter, sink=sink, work_root=root).run(
            TaskRequest(task_id="30", repo_url="u", commit_sha="f" * 40,
                        kind="fix", branch="agent/fix-30")
        )
        calls = adapter.calls
        check(
            "emit 与 record_findings 发生在 push/open_pr 之前",
            outcome.status == "done"
            and "emit" in calls and "push" in calls and "open_pr" in calls
            and calls.index("emit") < calls.index("push") < calls.index("open_pr")
            and bool(sink.recorded),
            f"calls={calls}",
        )

    # A worker that lost its lease must fail before touching the remote.
    with tempfile.TemporaryDirectory(prefix="agent-runtime-") as root:
        sink = RecordingSink()
        adapter = FakeAdapter([_finding()])

        def lost_lease() -> None:
            raise AgentRunnerError("租约已被回收")

        outcome = AgentRunner(
            adapter=adapter, sink=sink, work_root=root, publish_guard=lost_lease,
        ).run(TaskRequest(task_id="31", repo_url="u", commit_sha="a" * 40,
                          kind="fix", branch="agent/fix-31"))
        check(
            "租约丢失 → 不 push、不开 PR，任务 failed",
            outcome.status == "failed" and not adapter.pushed
            and "open_pr" not in adapter.calls,
            f"status={outcome.status} calls={adapter.calls}",
        )

    # A broken guard must fail closed, not silently allow the publish.
    with tempfile.TemporaryDirectory(prefix="agent-runtime-") as root:
        adapter = FakeAdapter([_finding()])

        def broken() -> None:
            raise OSError("db down")

        outcome = AgentRunner(
            adapter=adapter, sink=RecordingSink(), work_root=root, publish_guard=broken,
        ).run(TaskRequest(task_id="32", repo_url="u", commit_sha="b" * 40,
                          kind="fix", branch="agent/fix-32"))
        check(
            "发布守卫自身报错也 fail-closed",
            outcome.status == "failed" and not adapter.pushed,
            f"status={outcome.status} calls={adapter.calls}",
        )

    # A retry must not reuse the previous attempt's directory (or branch).
    check(
        "重试使用独立的工作目录",
        workdir_for("/w", "7") != workdir_for("/w", "7", 1)
        and "attempt1" in workdir_for("/w", "7", 1).name,
    )


def main() -> int:
    check_protocol_order()
    check_push_guard()
    check_pr_policy()
    check_worker_wiring()
    check_review_command()
    check_findings_ingestion()
    check_open_pr_seam()
    check_gates()
    check_result_schema()
    check_workdir_retention()
    check_secret_masking()
    check_publish_safety()

    if _problems:
        print(f"\n❌ {len(_problems)} check(s) failed")
        for label in _problems:
            print("   - " + label)
        return 1
    print("\n✅ agent runtime honours §9.2/§9.3/§9.4/§9.5 (offline, fake adapters)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
