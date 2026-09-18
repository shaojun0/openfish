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

from services.agent_runner import (  # noqa: E402
    STEPS,
    AgentRunner,
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

_SECRET = "sk-live-DEADBEEF-0123456789"

#: Expected task failures are logged deliberately; keep them out of the gate's
#: stdout so the ✅/❌ report stays readable.
logging.getLogger("cpypiserver.agent_runner").addHandler(logging.NullHandler())

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
    ) -> None:
        self.calls: list[str] = []
        self.findings = list(findings or [])
        self.gates = list(
            gates
            or [GateResult(gate="check_lint", passed=True, exit_code=0, duration_ms=4)]
        )
        self.emitted: dict[str, Any] | None = None
        self.pushed: list[str] = []

    def set_model_env(self, env: Mapping[str, str]) -> None:
        self.calls.append("set_model_env")

    def prepare(self, workdir: Path) -> None:
        Path(workdir).mkdir(parents=True, exist_ok=True)

    def clone(self, workdir: Path, *, repo_url: str, commit_sha: str) -> None:
        self.calls.append("clone")

    def read_context(self, workdir: Path) -> ReadContext:
        self.calls.append("read")
        return ReadContext(agents_md="# AGENTS", policy_text=None)

    def run_gates(self, workdir: Path, *, timeout: float) -> GateSummary:
        self.calls.append("gates")
        passed = sum(1 for item in self.gates if item.passed)
        return GateSummary(
            gates=self.gates,
            total=len(self.gates),
            passed=passed,
            failed=len(self.gates) - passed,
        )

    def review(self, workdir: Path, *, policy: Any, commit_sha: str, context: ReadContext):
        self.calls.append("review")
        return self.findings

    def search(self, workdir: Path, *, finding: Mapping[str, Any], commit_sha: str, context: ReadContext):
        self.calls.append("search")
        return finding

    def emit(self, workdir: Path, *, payload: Mapping[str, Any]) -> str:
        self.calls.append("emit")
        self.emitted = dict(payload)
        return str(Path(workdir) / "result.json")

    def push(self, workdir: Path, *, branch: str, commit_sha: str) -> None:
        self.calls.append("push")
        self.pushed.append(branch)

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

def main() -> int:
    check_protocol_order()
    check_push_guard()
    check_gates()
    check_result_schema()
    check_workdir_retention()
    check_secret_masking()

    if _problems:
        print(f"\n❌ {len(_problems)} check(s) failed")
        for label in _problems:
            print("   - " + label)
        return 1
    print("\n✅ agent runtime honours §9.2/§9.3/§9.4/§9.5 (offline, fake adapters)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
