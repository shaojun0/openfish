#!/usr/bin/env python
"""Gate: the AI-maintained check suite cannot grade its own homework.

Run from the backend directory (`backend/`)::

    python scripts/check_agent_checks.py

This is the gate for the *hard invariant* of the AI checks feature:

    A check suite that gates a fix must be read-only with respect to that fix.
    One run may never both produce a fix and weaken the checks that judge it.

It proves, entirely offline (no git, no docker, no model, no network):

* **P1.0** — a checkout with no resolvable suite yields the explicit
  ``unverified`` state and the runner **never** falls back to this image's own
  ``backend/scripts/check_*.py``; ``pr_policy=on_green`` then refuses to push.
* **P1.1** — ``GateResult``/``GateSummary`` carry three outcomes and an empty or
  unvalidated suite is never ``ok``.
* **P1.2** — the resolution order (``.agent/checks/`` → policy ``checks:`` →
  manifest discovery → ``unverified``) and the pure discovery providers.
* **P1.3** — the suite is frozen from the base snapshot and a fix that writes
  ``.agent/checks/**`` fails loudly before any commit or push; a curator may
  write only the suite (plus test files).
* **P1.4** — ``OPENFISH_TASK_KIND`` reaches the headless command.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping, Sequence

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
os.chdir(REPO_ROOT)

from sqlalchemy import inspect, text  # noqa: E402
from sqlalchemy.schema import CreateTable  # noqa: E402

from models.agent_hub import (  # noqa: E402
    TASK_KIND,
    AgentTask,
    CheckRun,
    CheckSuiteSnapshot,
    CheckValidation as CheckValidationRow,
    Repo,
)
from models.agent_hub_migrate import ensure_schema  # noqa: E402
from routes.repo_webhook import parse_event, plan_actions  # noqa: E402
from services import agent_worker, check_store  # noqa: E402
from services.agent_queue import AgentQueue, build_engine  # noqa: E402
from services.agent_runner import (  # noqa: E402
    AgentRunner,
    ReadContext,
    TaskRequest,
)
from services.agent_surface import (  # noqa: E402
    AGENT_PATH_MODULES,
    AgentSurfaceError,
    RestrictedForgejoClient,
    forbidden_client_methods,
    scan_forbidden_api,
)
from services.check_curator import (  # noqa: E402
    PROPOSAL_MARKER,
    apply_validations,
)
from services.check_discovery import (  # noqa: E402
    cargo_checks,
    discover_checks,
    go_checks,
    makefile_checks,
    python_checks,
)
from services.check_suite import (  # noqa: E402
    CHECK_DIR_RELPATH,
    curator_guard_violations,
    fix_guard_violations,
    freeze_suite,
    is_test_path,
    policy_checks,
    resolve_suite,
    resolve_suites,
    suite_protected_paths,
)
from services.check_trust import (  # noqa: E402
    ASSURANCE_L0,
    ASSURANCE_L1,
    ASSURANCE_L2,
    ASSURANCE_L3,
    AI_TITLE,
    DEFAULT_L3_CRITERIA,
    HUMAN_REVIEW_NOTICE,
    MAY_AUTO_MERGE,
    REPOSITORY_TITLE,
    L3Criteria,
    assess_assurance,
    check_l3_eligible,
    governance_findings,
    render_two_suites,
)
from services.check_validation import (  # noqa: E402
    CheckHistoryStore,
    CheckRunRecord,
    CheckValidation,
    default_seeders,
    summarize_history,
    validate_check,
)
from services.gates import (  # noqa: E402
    SOURCE_AGENT_CHECKS,
    SOURCE_DISCOVERY,
    SOURCE_POLICY,
    SOURCE_SCRIPTS,
    SOURCE_UNVERIFIED,
    STATUS_FAILED,
    STATUS_PASSED,
    STATUS_UNVERIFIED,
    VALIDATION_UNVALIDATED,
    VALIDATION_VALIDATED,
    CheckCommand,
    CheckSuite,
    GateResult,
    GateSummary,
    render_summary,
    run_suite,
    suite_fingerprint,
    unverified_summary,
)
from services.review_policy import (  # noqa: E402
    PolicyValidationError,
    parse_document,
    policy_hash,
)

logging.getLogger("cpypiserver.agent_runner").addHandler(logging.NullHandler())
logging.getLogger("cpypiserver.check_suite").addHandler(logging.NullHandler())

_problems: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    if ok:
        print(f"✅ {label}")
    else:
        _problems.append(label)
        print(f"❌ {label}" + (f" — {detail}" if detail else ""))


# ── Fakes ────────────────────────────────────────────────────────────

class _FakeAdapter:
    """A recording adapter whose gate verdict we control exactly."""

    def __init__(
        self,
        *,
        gates: Sequence[GateResult] | None = None,
        summary: GateSummary | None = None,
        changed: bool = True,
        changed_paths: Sequence[str] | None = None,
    ) -> None:
        self.gates = list(
            gates or [GateResult(gate="ai:unit", passed=True, exit_code=0, duration_ms=3)]
        )
        self.summary = summary
        self.changed = changed
        self.changed_paths_list = list(changed_paths or [])
        self.calls: list[str] = []
        self.pushed: list[str] = []
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
        return ReadContext(agents_md="# AGENTS")

    def run_gates(self, workdir: Path, *, timeout: float, suite: Any | None = None) -> GateSummary:
        self.calls.append("gates")
        self.suites.append(suite)
        if self.summary is not None:
            return self.summary
        passed = sum(1 for item in self.gates if item.passed)
        return GateSummary(
            gates=self.gates, total=len(self.gates), passed=passed,
            failed=len(self.gates) - passed,
        )

    def changed_paths(self, workdir: Path) -> Sequence[str]:
        return list(self.changed_paths_list)

    def review(self, workdir: Path, *, policy: Any, commit_sha: str, context: ReadContext,
               kind: str = "review"):
        self.calls.append("review")
        self.review_kinds.append(kind)
        return []

    def search(self, workdir: Path, *, finding: Mapping[str, Any], commit_sha: str, context: ReadContext):
        return finding

    def emit(self, workdir: Path, *, payload: Mapping[str, Any]) -> str:
        self.calls.append("emit")
        return str(Path(workdir) / "result.json")

    def commit(self, workdir: Path, *, branch: str, commit_sha: str, message: str) -> bool:
        self.calls.append("commit")
        if not self.changed:
            raise RuntimeError("no changes")
        return True

    def push(self, workdir: Path, *, branch: str, commit_sha: str) -> None:
        self.calls.append("push")
        self.pushed.append(branch)

    def open_pr(self, workdir: Path, *, branch: str, base: str, title: str, body: str) -> str:
        self.calls.append("open_pr")
        return "http://forgejo.local/o/r/pulls/1"


def _run(adapter: _FakeAdapter, root: str, task: TaskRequest, **kwargs: Any) -> Any:
    return AgentRunner(adapter=adapter, work_root=root, **kwargs).run(task)


# ── P1.0: no resolvable suite → unverified, and never the image's gates ──

def check_false_green() -> None:
    print("\nP1.0 空套件必须是 unverified（修掉假绿）")
    from services.agent_runner import SubprocessRunnerAdapter

    with tempfile.TemporaryDirectory(prefix="ai-checks-") as tmp:
        workdir = Path(tmp) / "1"
        checkout = workdir / "repo"
        checkout.mkdir(parents=True)
        (checkout / "README.md").write_text("# foreign repo, no suite\n", encoding="utf-8")

        # The real adapter, real discovery, real resolution — no fakes.
        adapter = SubprocessRunnerAdapter()
        summary = adapter.run_gates(workdir, timeout=30.0)
        names = [item.gate for item in summary.gates]
        check(
            "外来仓库无套件 → state=unverified（不是绿）",
            summary.state == STATUS_UNVERIFIED and not summary.ok and not summary.verified,
            f"state={summary.state} ok={summary.ok}",
        )
        check(
            "绝不回退到 runner 镜像自己的 check_*.py",
            "check_lint" not in names and "check_agent_runtime" not in names,
            f"gates={names}",
        )
        check("unverified 汇总带上原因", bool(summary.reason), summary.reason)
        check(
            "venv 里确实存在镜像自己的 gates（否则上面的断言无意义）",
            (REPO_ROOT / "scripts" / "check_lint.py").is_file(),
        )

        # openfish itself keeps working: its own checkout scripts resolve.
        native = Path(tmp) / "openfish"
        (native / "backend" / "scripts").mkdir(parents=True)
        (native / "backend" / "scripts" / "check_lint.py").write_text("raise SystemExit(0)\n")
        suite = resolve_suite(native)
        check(
            "自带 backend/scripts/check_*.py 的仓库仍解析为 scripts 套件",
            suite.source == SOURCE_SCRIPTS and [c.id for c in suite.checks] == ["check_lint"],
            f"source={suite.source} checks={[c.id for c in suite.checks]}",
        )
        check(
            "套件命令相对 checkout，不含绝对路径",
            suite.checks[0].argv == ["python", "backend/scripts/check_lint.py"],
            repr(suite.checks[0].argv),
        )

    # on_green must not push when the suite is unverified.
    with tempfile.TemporaryDirectory(prefix="ai-checks-") as tmp:
        adapter = _FakeAdapter(summary=unverified_summary("没有可解析的校验套件"))
        outcome = _run(
            adapter, tmp,
            TaskRequest(task_id="2", repo_url="u", commit_sha="a" * 40,
                        kind="fix", branch="agent/fix-2"),
        )
        check(
            "on_green + unverified → 不 push、不开 PR、任务 failed",
            outcome.status == "failed" and not adapter.pushed
            and "open_pr" not in adapter.calls,
            f"status={outcome.status} calls={adapter.calls}",
        )
        check(
            "拒绝原因点明 unverified",
            "unverified" in (outcome.error or ""), str(outcome.error),
        )
        check(
            "unverified 的 gate 条目进入 result 文档且带状态",
            all(g.get("status") == STATUS_UNVERIFIED for g in outcome.gates)
            and bool(outcome.gates),
            repr(outcome.gates),
        )


# ── P1.1: three outcomes, and only a real pass is ok ─────────────────

def check_tri_state() -> None:
    print("\nP1.1 三态：passed / failed / unverified")
    empty = run_suite(CheckSuite(checks=[], source=SOURCE_UNVERIFIED, reason="nothing"))
    check(
        "空套件 run_suite → unverified 且 ok=False",
        empty.state == STATUS_UNVERIFIED and not empty.ok,
        repr(empty.model_dump()),
    )
    only_unvalidated = run_suite(
        CheckSuite(
            checks=[CheckCommand(id="ai:x", argv=["true"], validation=VALIDATION_UNVALIDATED)],
            source=SOURCE_AGENT_CHECKS,
        ),
        root=REPO_ROOT,
    )
    check(
        "只有 unvalidated 检查 → unverified（不能门控）",
        only_unvalidated.state == STATUS_UNVERIFIED and not only_unvalidated.ok,
        repr(only_unvalidated.model_dump()),
    )
    validated = run_suite(
        CheckSuite(
            checks=[
                CheckCommand(id="user:ok", argv=[sys.executable, "-c", "print(1)"]),
                CheckCommand(id="ai:x", argv=[sys.executable, "-c", "print(2)"],
                             validation=VALIDATION_UNVALIDATED),
            ],
            source=SOURCE_DISCOVERY,
        ),
        root=REPO_ROOT,
    )
    check(
        "有 validated 且通过 → passed（unvalidated 只报告）",
        validated.state == STATUS_PASSED and validated.ok
        and validated.unverified == 1 and validated.failed == 0,
        repr(validated.model_dump()),
    )
    red = run_suite(
        CheckSuite(
            checks=[CheckCommand(id="user:bad", argv=[sys.executable, "-c", "raise SystemExit(3)"])],
            source=SOURCE_DISCOVERY,
        ),
        root=REPO_ROOT,
    )
    check(
        "validated 失败 → failed",
        red.state == STATUS_FAILED and not red.ok and red.failed == 1,
        repr(red.model_dump()),
    )
    rendered = render_summary(empty)
    check("render_summary 明确写出 unverified", "unverified" in rendered, rendered)
    check(
        "GateResult 的 passed/status 不会互相矛盾",
        GateResult(gate="g", passed=False, exit_code=1).status == STATUS_FAILED
        and GateResult(gate="g", passed=True, exit_code=0).status == STATUS_PASSED
        and not GateResult(gate="g", passed=True, exit_code=0,
                           status=STATUS_UNVERIFIED).passed,
    )


# ── P1.2: resolution order + pure discovery ──────────────────────────

def check_resolution_order() -> None:
    print("\nP1.2 解析顺序与零配置发现")
    with tempfile.TemporaryDirectory(prefix="ai-checks-") as tmp:
        root = Path(tmp)
        check("全空 → unverified", resolve_suite(root).source == SOURCE_UNVERIFIED)

        # manifest discovery
        (root / "package.json").write_text(json.dumps({"scripts": {"test": "jest", "lint": "eslint ."}}))
        discovered = resolve_suite(root)
        check(
            "package.json → npm test / npm run lint",
            discovered.source == SOURCE_DISCOVERY
            and [(c.id, c.argv) for c in discovered.checks]
            == [("npm:test", ["npm", "run", "test"]),
                ("npm:lint", ["npm", "run", "lint"])],
            repr([(c.id, c.argv) for c in discovered.checks]),
        )

        # policy checks beat discovery
        pol = resolve_suite(root, policy_checks_field=[{"id": "p", "command": "make verify"}])
        check(
            "policy checks: 优先于自动发现",
            pol.source == SOURCE_POLICY and [c.id for c in pol.checks] == ["p"],
            f"source={pol.source}",
        )

        # .agent/checks beats everything
        (root / CHECK_DIR_RELPATH).mkdir(parents=True)
        (root / CHECK_DIR_RELPATH / "checks.yml").write_text(
            "version: 1\nchecks:\n  - id: unit\n    command: pytest -q\n"
        )
        agent = resolve_suite(root, policy_checks_field=[{"id": "p", "command": "make verify"}])
        check(
            ".agent/checks/ 优先于 policy 与发现",
            agent.source == SOURCE_AGENT_CHECKS and [c.id for c in agent.checks] == ["unit"],
            f"source={agent.source}",
        )
        check(
            "manifest 里的检查默认 unvalidated（未证伪不得门控）",
            not agent.resolved and agent.checks[0].validation == VALIDATION_UNVALIDATED,
            repr([c.validation for c in agent.checks]),
        )
        (root / CHECK_DIR_RELPATH / "checks.yml").write_text(
            "version: 1\nchecks:\n  - id: unit\n    command: pytest -q\n    validated: true\n"
        )
        check(
            "显式 validated: true 才可门控",
            resolve_suite(root).resolved,
        )

    # Providers are pure and offline: build the manifests, never execute.
    with tempfile.TemporaryDirectory(prefix="ai-checks-") as tmp:
        root = Path(tmp)
        (root / "pyproject.toml").write_text(
            "[tool.ruff]\nline-length = 100\n[tool.mypy]\nstrict = true\n"
        )
        (root / "tests").mkdir()
        (root / "tests" / "test_x.py").write_text("def test_x():\n    assert True\n")
        checks = python_checks(root)
        check(
            "python 项目 → pytest + ruff + mypy（声明才加）",
            [c.id for c in checks] == ["pytest", "ruff", "mypy"],
            repr([c.id for c in checks]),
        )
        (root / "go.mod").write_text("module x\n")
        check(
            "go.mod → build/vet/test 三条独立检查",
            [c.id for c in go_checks(root)] == ["go:build", "go:vet", "go:test"],
        )
        (root / "Cargo.toml").write_text("[package]\nname='x'\n")
        check("Cargo.toml → cargo check/test",
              [c.id for c in cargo_checks(root)] == ["cargo:check", "cargo:test"])
        (root / "Makefile").write_text("test:\n\tpytest\nlint:\n\truff .\ndeploy:\n\techo no\n")
        check("Makefile → 只取 test/check/lint",
              [c.id for c in makefile_checks(root)] == ["make:test", "make:lint"])

    # The providers are injectable, so a caller can supply its own policy.
    with tempfile.TemporaryDirectory(prefix="ai-checks-") as tmp:
        root = Path(tmp)
        (root / "package.json").write_text(json.dumps({"scripts": {"test": "jest"}}))
        injected = discover_checks(root, providers=())
        check("注入空 provider 列表 → 无发现", not injected.checks)


# ── P1.3: freeze + path guard ─────────────────────────────────────────

def check_freeze_and_guard() -> None:
    print("\nP1.3 冻结 + 路径守卫")
    suite = CheckSuite(
        checks=[
            CheckCommand(id="a", argv=["true"]),
            CheckCommand(id="b", argv=["false"]),
        ],
        source=SOURCE_AGENT_CHECKS,
    )
    frozen = freeze_suite(suite, base_sha="c" * 40)
    reordered = CheckSuite(
        checks=[CheckCommand(id="b", argv=["false"]), CheckCommand(id="a", argv=["true"])],
        source=SOURCE_AGENT_CHECKS,
    )
    check("同一套件指纹稳定", freeze_suite(suite).fingerprint == frozen.fingerprint)
    check("顺序不同即不同套件", suite_fingerprint(reordered) != frozen.fingerprint)

    violations = fix_guard_violations([
        "backend/services/foo.py", ".agent/checks/checks.yml",
        "./.agent/checks/test_x.py", f"{CHECK_DIR_RELPATH}",
    ])
    check(
        "fix 守卫拦住 .agent/checks/** 的一切写法",
        violations == [".agent/checks/checks.yml", ".agent/checks/test_x.py",
                       CHECK_DIR_RELPATH],
        repr(violations),
    )
    check(
        "curator 只能写套件与测试文件",
        curator_guard_violations([
            ".agent/checks/checks.yml", "tests/test_a.py", "api.test.ts",
            "backend/services/foo.py",
        ]) == ["backend/services/foo.py"],
    )
    check(
        "测试文件识别",
        is_test_path("tests/x.py") and is_test_path("backend/tests/test_y.py")
        and is_test_path("a/b.spec.ts") and not is_test_path("backend/services/x.py"),
    )
    check(
        "fix 同时被禁止改 policy 声明与用户测试路径（用户套件是权威）",
        fix_guard_violations([
            ".agent/review-policy.yml", "tests/test_x.py", "backend/tests/y.py",
        ]) == [".agent/review-policy.yml", "tests/test_x.py", "backend/tests/y.py"],
    )

    # P0: freezing the descriptor is not enough — the fix must not rewrite the
    # scripts that descriptor *executes*, nor the files declaring how they run.
    entry_suite = CheckSuite(
        checks=[
            CheckCommand(id="lint", argv=["python", "backend/scripts/check_lint.py"]),
            CheckCommand(id="test", argv=["make", "test"]),
        ],
        source=SOURCE_AGENT_CHECKS,
    )
    entry_hits = fix_guard_violations(
        ["backend/scripts/check_lint.py", "Makefile", "package.json",
         "pyproject.toml", "pytest.ini", "conftest.py", "backend/services/foo.py"],
        suite=entry_suite,
    )
    check(
        "fix 不能改被判套件实际执行的入口脚本",
        "backend/scripts/check_lint.py" in entry_hits,
        repr(entry_hits),
    )
    check(
        "fix 不能改声明套件如何运行的文件",
        {"Makefile", "package.json", "pyproject.toml", "pytest.ini", "conftest.py"}
        <= set(entry_hits),
        repr(entry_hits),
    )
    check(
        "fix 仍可改普通源码",
        "backend/services/foo.py" not in entry_hits,
        repr(entry_hits),
    )

    # "Judge with the base revision": the runner locks the suite's own entry
    # scripts and declaration files read-only for the duration of a fix, so an
    # opportunistic write fails at the syscall, not only at the later guard.
    with tempfile.TemporaryDirectory(prefix="ai-checks-") as tmp:
        root = Path(tmp)
        (root / "backend" / "scripts").mkdir(parents=True)
        (root / "backend" / "scripts" / "check_lint.py").write_text("print(1)\n")
        (root / "Makefile").write_text("test:\n\ttrue\n")
        (root / "pyproject.toml").write_text("[project]\nname='x'\n")
        locked = suite_protected_paths(entry_suite, root=root)
        check(
            "fix 期间锁定被判套件的入口脚本与声明文件",
            {"backend/scripts/check_lint.py", "Makefile", "pyproject.toml"} <= set(locked),
            repr(locked),
        )
        check(
            "只锁定真实存在的文件（不存在的不进锁清单）",
            "package.json" not in locked,
            repr(locked),
        )

    # A fix that edits the suite fails before commit/push.
    with tempfile.TemporaryDirectory(prefix="ai-checks-") as tmp:
        adapter = _FakeAdapter(changed_paths=[".agent/checks/checks.yml"])
        outcome = _run(
            adapter, tmp,
            TaskRequest(task_id="3", repo_url="u", commit_sha="b" * 40,
                        kind="fix", branch="agent/fix-3"),
        )
        check(
            "fix 改动套件 → 任务 failed，且没有 commit / push / PR",
            outcome.status == "failed" and "commit" not in adapter.calls
            and not adapter.pushed and "open_pr" not in adapter.calls,
            f"status={outcome.status} calls={adapter.calls}",
        )
        check("拒绝原因点明硬不变量",
              "硬不变量" in (outcome.error or ""), str(outcome.error))

    # A clean fix still flows through, re-running the FROZEN suite.
    with tempfile.TemporaryDirectory(prefix="ai-checks-") as tmp:
        adapter = _FakeAdapter(changed_paths=["backend/services/foo.py"])
        outcome = _run(
            adapter, tmp,
            TaskRequest(task_id="4", repo_url="u", commit_sha="c" * 40,
                        kind="fix", branch="agent/fix-4"),
        )
        check(
            "干净 fix → commit + push + PR",
            outcome.status == "done" and adapter.pushed == ["agent/fix-4"]
            and "open_pr" in adapter.calls,
            f"status={outcome.status} calls={adapter.calls}",
        )
        check(
            "事后重跑用的是同一份冻结套件（不是重新解析）",
            len(adapter.suites) == 2 and adapter.suites[0] is adapter.suites[1],
            f"suites={adapter.suites}",
        )
        check("review 拿到 kind=fix", adapter.review_kinds == ["fix"],
              repr(adapter.review_kinds))

    # The same guard applies to an escalated review→fix (auto_fix).
    with tempfile.TemporaryDirectory(prefix="ai-checks-") as tmp:
        adapter = _FakeAdapter(
            gates=[GateResult(gate="ai:x", passed=False, exit_code=1)],
            changed_paths=[".agent/checks/x.py"],
        )
        outcome = _run(
            adapter, tmp,
            TaskRequest(task_id="5", repo_url="u", commit_sha="d" * 40,
                        kind="review", branch="agent/fix-5"),
            auto_fix=True,
        )
        check(
            "升级出来的 fix 同样受套件守卫约束",
            outcome.status == "failed" and "commit" not in adapter.calls,
            f"status={outcome.status} calls={adapter.calls}",
        )


# ── P1.4: the mode signal reaches the command ─────────────────────────

def check_task_kind_env() -> None:
    print("\nP1.4 OPENFISH_TASK_KIND 传给 headless 命令")
    with tempfile.TemporaryDirectory(prefix="ai-checks-") as tmp:
        out = Path(tmp) / "env.json"
        script = Path(tmp) / "dump.py"
        script.write_text(
            "import json, os, pathlib, sys\n"
            "pathlib.Path(sys.argv[1]).write_text(json.dumps({\n"
            "    'kind': os.environ.get('OPENFISH_TASK_KIND'),\n"
            "    'sha': os.environ.get('OPENFISH_COMMIT_SHA'),\n"
            "}))\n",
            encoding="utf-8",
        )
        review_fn = agent_worker.build_review_fn(f"{sys.executable} {script} {out}")
        review_fn(Path(tmp), policy=None, commit_sha="e" * 40, context=None, kind="fix")
        payload = json.loads(out.read_text(encoding="utf-8"))
        check(
            "kind=fix 写进子进程环境，sha 仍在",
            payload.get("kind") == "fix" and payload.get("sha") == "e" * 40,
            json.dumps(payload),
        )


# ── policy checks: schema + hash sensitivity ─────────────────────────

def check_policy_checks_field() -> None:
    print("\n§7.2 checks: 字段")
    document = parse_document(
        {"version": 1, "checks": [{"id": "unit", "command": "pytest -q", "timeout": 30}]}
    )
    check(
        "policy 解析 checks:",
        len(document.checks) == 1 and document.checks[0].id == "unit"
        and document.checks[0].timeout == 30,
        repr(document.checks),
    )
    with_checks = policy_hash(document)
    without = policy_hash(parse_document({"version": 1}))
    check("有/无 checks 的 policy_hash 不同", with_checks != without)
    check(
        "无 checks 的 policy_hash 与新增字段前一致（向后兼容）",
        without == policy_hash(parse_document({"version": 1, "rules": []})),
    )
    try:
        parse_document({"version": 1, "checks": [{"id": "a", "command": "x"},
                                                 {"id": "a", "command": "y"}]})
        check("重复 check id 被拒", False, "竟然通过了")
    except PolicyValidationError:
        check("重复 check id 被拒", True)
    check(
        "policy checks 标记为 validated（人写的，可门控）",
        policy_checks([{"id": "unit", "command": "pytest -q"}]).resolved,
    )


# ── No-merge red line: the agent's capability surface ────────────────

def check_no_merge_surface() -> None:
    print("\n红线：agent 永远不能合并 / 批准 / 改分支保护")
    check("MAY_AUTO_MERGE 常量恒为 False", MAY_AUTO_MERGE is False)
    findings = scan_forbidden_api()
    check(
        "agent 路径源码里没有 merge/approve/protection 操作",
        findings == [],
        repr(findings),
    )
    check(
        "审计确实覆盖了 agent 路径模块",
        "services/agent_worker.py" in AGENT_PATH_MODULES
        and "services/repo_import.py" in AGENT_PATH_MODULES,
        repr(AGENT_PATH_MODULES),
    )

    class MergeableClient:
        """A client with far too many capabilities, to prove the proxy blocks."""

        def create_pull_request(self, repo: str, *, head: str, base: str,
                                title: str, body: str = "") -> str:
            return "http://forgejo/o/r/pulls/1"

        def merge_pull_request(self, repo: str, index: int) -> str:
            return "merged"

        def approve_pull_request(self, repo: str, index: int) -> None:
            return None

        def update_branch_protection(self, repo: str, payload: dict) -> None:
            return None

    proxy = RestrictedForgejoClient(MergeableClient())
    check(
        "白名单只放行 create_pull_request",
        proxy.create_pull_request("o/r", head="agent/x", base="main", title="t").endswith("/1"),
    )
    blocked = []
    for name in ("merge_pull_request", "approve_pull_request", "update_branch_protection"):
        try:
            getattr(proxy, name)
            blocked.append(name)
        except AgentSurfaceError:
            pass
    check("merge/approve/protection 全部被代理拒绝", blocked == [], repr(blocked))

    hits = forbidden_client_methods(MergeableClient())
    check(
        "静态审计能认出危险方法（证明扫描非空转）",
        {"merge_pull_request", "approve_pull_request", "update_branch_protection"} <= set(hits),
        repr(hits),
    )
    real = forbidden_client_methods(__import__("services.repo_import", fromlist=["ForgejoClient"]).ForgejoClient)
    check("真实 ForgejoClient 不含任何危险方法", real == [], repr(real))

    # build_open_pr_fn must actually route through the proxy.
    wrapped = agent_worker.build_open_pr_fn("o/r", client=MergeableClient())
    url = wrapped(Path("/tmp"), branch="agent/x", base="main", title="t", body="b")
    check("build_open_pr_fn 仍能开 PR（经代理）", url.endswith("/1"), url)


# ── Trust ladder (L0–L3), no human approval, never auto-merge ────────

def check_trust_ladder() -> None:
    print("\n信任阶梯 L0–L3（机械获得；永不自动合并）")
    unverified = unverified_summary("nothing")
    green = GateSummary(
        gates=[GateResult(gate="user:t", passed=True, exit_code=0)],
        total=1, passed=1, failed=0,
    )
    red = GateSummary(
        gates=[GateResult(gate="user:t", passed=False, exit_code=1)],
        total=1, passed=0, failed=1,
    )
    ai_green = GateSummary(
        gates=[GateResult(gate="ai:t", passed=True, exit_code=0)],
        total=1, passed=1, failed=0, suite_source=SOURCE_AGENT_CHECKS,
    )

    l0 = assess_assurance(user_summary=unverified, ai_summary=unverified)
    check("L0：两套都 unverified → 不许开 PR",
          l0.level == ASSURANCE_L0 and not l0.may_open_pr and not l0.may_auto_merge)

    l1 = assess_assurance(user_summary=unverified, ai_summary=ai_green)
    check("L1：只有 AI 校验通过 → 可开带标签的 PR，绝不自动合并",
          l1.level == ASSURANCE_L1 and l1.may_open_pr and not l1.may_auto_merge
          and l1.label == "ai-checks/L1")

    l2 = assess_assurance(user_summary=green, ai_summary=red)
    check("L2：仓库校验通过 → 以仓库校验为准，AI 失败不能把它翻红也不能被掩盖",
          l2.level == ASSURANCE_L2 and l2.may_open_pr and "AI" in l2.reason)

    masked = assess_assurance(user_summary=red, ai_summary=ai_green)
    check("L2：仓库校验失败 → AI 全绿也不能开 PR（AI 不能覆盖用户失败）",
          masked.level == ASSURANCE_L2 and not masked.may_open_pr)

    check("L3 默认关闭（criteria.enabled=False）", DEFAULT_L3_CRITERIA.enabled is False)
    forced = assess_assurance(
        user_summary=unverified, ai_summary=ai_green, l3_eligible=True,
        criteria=DEFAULT_L3_CRITERIA,
    )
    check("L3 未启用时即使 eligible 也只到 L1", forced.level == ASSURANCE_L1)
    promoted = assess_assurance(
        user_summary=unverified, ai_summary=ai_green, l3_eligible=True,
        criteria=L3Criteria(enabled=True),
    )
    check("L3 显式开启且达标 → L3，但依然 may_auto_merge=False",
          promoted.level == ASSURANCE_L3 and not promoted.may_auto_merge)

    # L3 eligibility is objective and needs history + validation.
    record = CheckRunRecord(check_id="ai:t", state=STATUS_PASSED, validation=VALIDATION_VALIDATED)
    history = summarize_history([record] * 25)

    weak = CheckValidation(check_id="ai:t", status=VALIDATION_UNVALIDATED, faults_detected=0)
    strong = CheckValidation(
        check_id="ai:t", status=VALIDATION_VALIDATED,
        baseline_passed=True, faults_attempted=1, faults_detected=1,
    )
    check("L3 未开启时 check_l3_eligible 恒 False",
          check_l3_eligible([strong], history, criteria=DEFAULT_L3_CRITERIA) is False)
    check("L3 开启后：未证伪的检查不达标",
          check_l3_eligible([weak], history, criteria=L3Criteria(enabled=True)) is False)
    check("L3 开启后：证伪 + 足够运行次数 + 无 flake 才达标",
          check_l3_eligible([strong], history, criteria=L3Criteria(enabled=True)) is True)
    weakened_history = summarize_history([
        CheckRunRecord(check_id="ai:t", state=STATUS_PASSED,
                       validation=VALIDATION_VALIDATED, weakened=True)
    ] * 25)
    check("L3 开启后：有弱化记录的检查不达标（require_never_weakened）",
          check_l3_eligible([strong], weakened_history,
                            criteria=L3Criteria(enabled=True)) is False)

    body = render_two_suites(green, ai_green, l2)
    check("PR 正文分两块报告（不合并成一份匿名绿）",
          REPOSITORY_TITLE in body and AI_TITLE in body, body[:200])
    check("PR 正文写明不会自动合并、需人工 review",
          HUMAN_REVIEW_NOTICE.splitlines()[-1][:20] in body and "never" in body)


# ── Two-suite resolution + governance findings ───────────────────────

def check_two_suites_and_findings() -> None:
    print("\n两套解耦 + 治理 finding")
    with tempfile.TemporaryDirectory(prefix="ai-checks-") as tmp:
        root = Path(tmp)
        (root / "package.json").write_text(json.dumps({"scripts": {"test": "jest"}}))
        (root / CHECK_DIR_RELPATH).mkdir(parents=True)
        (root / CHECK_DIR_RELPATH / "checks.yml").write_text(
            "version: 1\nchecks:\n  - id: ai-unit\n    command: pytest -q\n    validated: true\n"
        )
        pair = resolve_suites(root)
        check(
            "用户套件与 AI 套件独立解析（用户=仓库自带，AI=.agent/checks/）",
            [c.id for c in pair.user.checks] == ["npm:test"]
            and [c.id for c in pair.ai.checks] == ["ai-unit"],
            f"user={[c.id for c in pair.user.checks]} ai={[c.id for c in pair.ai.checks]}",
        )

    findings = governance_findings(user_suite=None, ai_suite=None)
    check("无任何校验 → checks.no-automated-verification finding",
          [f["rule_id"] for f in findings] == ["checks.no-automated-verification"])
    findings = governance_findings(
        user_suite=CheckSuite(checks=[CheckCommand(id="user:t", argv=["true"])]),
        ai_suite=CheckSuite(checks=[CheckCommand(id="ai:t", argv=["true"],
                                                 validation=VALIDATION_UNVALIDATED)]),
        history=summarize_history([
            CheckRunRecord(check_id="ai:t", state=STATUS_PASSED, validation=VALIDATION_VALIDATED)
        ]),
    )
    rules = {f["rule_id"] for f in findings}
    check("从未失败 + 套件漂移都进 ledger",
          {"checks.never-failed", "checks.ai-suite-diverges"} <= rules, repr(rules))
    check("finding 形状符合 §9.5（level=debt, autofix=False）",
          all(f["level"] == "debt" and f["autofix"] is False for f in findings))


# ── P2.2 falsifiability validator + history ──────────────────────────

def check_validator() -> None:
    print("\nP2.2 证伪验证器 + 运行历史")
    check("默认 seeder 至少覆盖 python", len(default_seeders()) >= 1)
    with tempfile.TemporaryDirectory(prefix="ai-checks-") as tmp:
        root = Path(tmp)
        (root / "src").mkdir()
        (root / "src" / "calc.py").write_text(
            "def gt(a, b):\n    return a > b\n", encoding="utf-8"
        )
        (root / "verify.py").write_text(
            "import sys\n"
            "from src.calc import gt\n"
            "sys.exit(0 if gt(2, 1) and not gt(1, 2) else 1)\n",
            encoding="utf-8",
        )
        good = CheckCommand(id="user:calc", argv=[sys.executable, "verify.py"])
        validation = validate_check(good, repo_root=root)
        check(
            "能发现回归的检查 → validated（当前通过 + 变异后失败）",
            validation.validated and validation.faults_detected == 1
            and validation.baseline_passed,
            repr(validation.as_payload()),
        )

        always_green = CheckCommand(id="ai:noop", argv=[sys.executable, "-c", "print('ok')"])
        noop = validate_check(always_green, repo_root=root)
        check(
            "永远为真的检查 → unvalidated（不得门控）",
            not noop.validated and noop.baseline_passed,
            repr(noop.as_payload()),
        )
        broken = CheckCommand(id="user:broken", argv=[sys.executable, "-c", "raise SystemExit(2)"])
        failed_baseline = validate_check(broken, repo_root=root)
        check("基线就失败的检查 → unvalidated",
              not failed_baseline.validated and not failed_baseline.baseline_passed)

    store = CheckHistoryStore()
    store.append([
        CheckRunRecord(check_id="a", state=STATUS_PASSED, validation=VALIDATION_VALIDATED),
        CheckRunRecord(check_id="a", state=STATUS_PASSED, validation=VALIDATION_VALIDATED),
        CheckRunRecord(check_id="b", state=STATUS_FAILED, validation=VALIDATION_VALIDATED),
        CheckRunRecord(check_id="b", state=STATUS_PASSED, validation=VALIDATION_VALIDATED),
    ])
    summaries = {s.check_id: s for s in store.summaries()}
    check("历史能识别『从未失败』", summaries["a"].never_failed and not summaries["b"].never_failed)
    check("历史能算 flake 比例", abs(summaries["b"].flake_rate - 0.5) < 1e-9)

    # A curator cannot promote its own check by claiming it: the validator's
    # verdict overwrites the manifest, including a model-written ``true``.
    with tempfile.TemporaryDirectory(prefix="ai-manifest-") as tmp:
        manifest = Path(tmp) / "checks.yml"
        manifest.write_text(
            "version: 1\nchecks:\n  - id: claimed\n    command: 'true'\n"
            "    validated: true\n",
            encoding="utf-8",
        )
        changed = apply_validations(manifest, [CheckValidation(
            check_id="claimed", status=VALIDATION_UNVALIDATED, baseline_passed=True,
            reason="no seed could make it fail",
        )])
        rewritten = manifest.read_text(encoding="utf-8")
        check("curator 不能自称 validated（证伪结果覆盖 manifest）",
              changed == 1 and "validated: false" in rewritten, rewritten)


# ── Curator role: only the AI suite, its own PR ──────────────────────

def check_curator_role() -> None:
    print("\nchecks（curator）角色：只写 AI 套件，独立 PR")
    with tempfile.TemporaryDirectory(prefix="ai-checks-") as tmp:
        adapter = _FakeAdapter(changed_paths=["backend/services/foo.py"])
        outcome = _run(
            adapter, tmp,
            TaskRequest(task_id="6", repo_url="u", commit_sha="f" * 40, kind="checks"),
        )
        check(
            "curator 改源码 → 任务 failed，无 commit/push/PR",
            outcome.status == "failed" and "commit" not in adapter.calls
            and not adapter.pushed,
            f"status={outcome.status} calls={adapter.calls}",
        )
        check("拒绝原因点明 curator 只改套件",
              "curator" in (outcome.error or ""), str(outcome.error))

    with tempfile.TemporaryDirectory(prefix="ai-checks-") as tmp:
        adapter = _FakeAdapter(
            summary=unverified_summary("base 还没有可门控校验"),
            changed_paths=[".agent/checks/checks.yml", "tests/test_calc.py"],
        )
        outcome = _run(
            adapter, tmp,
            TaskRequest(task_id="7", repo_url="u", commit_sha="a" * 40, kind="checks"),
        )
        check(
            "curator 提案不被 on_green 卡死（新 check 本来就还没 validated）",
            outcome.status == "done" and adapter.pushed == ["agent/checks-7"]
            and "open_pr" in adapter.calls,
            f"status={outcome.status} calls={adapter.calls}",
        )
        check(
            "curator 只能开 agent/* 分支且绝不自动合并",
            adapter.pushed == ["agent/checks-7"],
        )


# ── A: checks task kind is enqueueable, migration is safe ────────────

def check_checks_task_kind_migration() -> None:
    print("\nA：checks 任务类型可入队（旧库 CHECK 迁移）")
    check("TASK_KIND 含 checks", "checks" in TASK_KIND, str(TASK_KIND))
    with tempfile.TemporaryDirectory(prefix="ai-migrate-") as tmp:
        db = str(Path(tmp) / "legacy.db")
        engine = build_engine(f"sqlite:///{db}")
        legacy_ddl = str(CreateTable(AgentTask.__table__).compile(dialect=engine.dialect))
        check("模型 DDL 已包含 checks", "'checks'" in legacy_ddl)
        legacy_ddl = legacy_ddl.replace(
            "'review', 'fix', 'checks', 'import', 'backfill'",
            "'review', 'fix', 'import', 'backfill'",
        )
        connection = sqlite3.connect(db)
        connection.executescript(
            "CREATE TABLE repos (id INTEGER NOT NULL PRIMARY KEY, slug VARCHAR(256) NOT NULL);"
        )
        connection.commit()
        connection.execute(legacy_ddl)
        connection.executescript(
            """
            INSERT INTO repos (id, slug) VALUES (1, 'legacy/repo');
            INSERT INTO agent_tasks
                (id, repo_id, kind, status, payload, leased_by, attempts, max_attempts,
                 priority, created_at)
            VALUES (7, 1, 'fix', 'done', '{"commit_sha":"abc"}', '', 2, 3, 3,
                    '2026-01-01 00:00:00');
            """
        )
        connection.commit()
        # Prove the "legacy" fixture really is legacy: it must reject `checks`.
        legacy_rejected = False
        try:
            connection.execute(
                "INSERT INTO agent_tasks (repo_id, kind, status, payload, leased_by,"
                " attempts, max_attempts, priority, created_at)"
                " VALUES (1,'checks','queued','{}','',0,3,5,'2026-01-02 00:00:00')"
            )
        except sqlite3.IntegrityError:
            legacy_rejected = True
        connection.close()
        check("旧库确实拒绝 checks（fixture 有效）", legacy_rejected)

        first = ensure_schema(engine)
        check(
            "ensure_schema 拓宽了 ck_agent_tasks_kind",
            "agent_tasks.ck_agent_tasks_kind" in first["updated_constraints"],
            repr(first),
        )
        with engine.begin() as conn:
            rows = conn.execute(text(
                "SELECT id, repo_id, kind, status, payload, attempts, priority"
                " FROM agent_tasks ORDER BY id"
            )).fetchall()
            preserved = [tuple(row) for row in rows]
            conn.execute(text(
                "INSERT INTO agent_tasks (repo_id, kind, status, payload, leased_by,"
                " attempts, max_attempts, priority, created_at)"
                " VALUES (1,'checks','queued','{}','',0,3,5,'2026-01-02 00:00:00')"
            ))
        check(
            "迁移保留原行（id / 字段不变）",
            preserved == [(7, 1, "fix", "done", '{"commit_sha":"abc"}', 2, 3)],
            repr(preserved),
        )
        check("迁移后 checks 可插入", True)
        with engine.begin() as conn:
            bogus_rejected = False
            try:
                conn.execute(text(
                    "INSERT INTO agent_tasks (repo_id, kind, status, payload, leased_by,"
                    " attempts, max_attempts, priority, created_at)"
                    " VALUES (1,'bogus','queued','{}','',0,3,5,'2026-01-02 00:00:00')"
                ))
            except Exception:
                bogus_rejected = True
        check("迁移后非法 kind 仍被拒", bogus_rejected)
        with engine.connect() as conn:
            check(
                "迁移后外键/索引完好",
                conn.exec_driver_sql("PRAGMA foreign_key_check").fetchall() == []
                and "ix_agent_tasks_status_priority_created"
                in {item["name"] for item in inspect(engine).get_indexes("agent_tasks")},
            )
        review_fks = inspect(engine).get_foreign_keys("review_runs")
        check(
            "review_runs 的 agent_task_id 外键仍指向 agent_tasks",
            any(fk.get("referred_table") == "agent_tasks" for fk in review_fks),
            repr(review_fks),
        )
        second = ensure_schema(engine)
        check(
            "第二次 ensure_schema 完全 no-op",
            second == {"created_tables": [], "added_columns": [], "added_indexes": [],
                       "updated_constraints": []},
            repr(second),
        )
        engine.dispose()


# ── B: curator trigger, dedup and rate limit ─────────────────────────

def _push_event(branch: str = "main"):
    payload = {
        "ref": f"refs/heads/{branch}",
        "after": "a" * 40,
        "commits": [{"id": "a" * 40}],
        "repository": {"full_name": "openfish/candidate", "default_branch": "main"},
        "sender": {"login": "gate"},
    }
    return parse_event("push", payload)


def check_curator_trigger() -> None:
    print("\nB：curator 触发 + 去重 + 限流")
    from services.review_policy import CURATOR_AUTO, CURATOR_BOOTSTRAP, CURATOR_OFF

    actions = plan_actions(
        _push_event(), repo_id=1, auto_review=True,
        curator_mode=CURATOR_BOOTSTRAP, curator_allowed=True,
    )
    check("bootstrap：默认分支 push → review + checks",
          [a.kind for a in actions] == ["review", "checks"],
          repr([a.kind for a in actions]))
    check("checks 提案带 bootstrap scope",
          "bootstrap" in json.dumps(actions[-1].payload), json.dumps(actions[-1].payload))
    auto = plan_actions(_push_event(), repo_id=1, auto_review=True,
                        curator_mode=CURATOR_AUTO, curator_allowed=True)
    check("auto：同样入队 checks", [a.kind for a in auto] == ["review", "checks"])
    off = plan_actions(_push_event(), repo_id=1, auto_review=True,
                       curator_mode=CURATOR_OFF, curator_allowed=True)
    check("off：不入队 checks", [a.kind for a in off] == ["review"])
    blocked = plan_actions(_push_event(), repo_id=1, auto_review=True,
                           curator_mode=CURATOR_BOOTSTRAP, curator_allowed=False)
    check("curator_allowed=False：不入队 checks", [a.kind for a in blocked] == ["review"])
    side = plan_actions(_push_event("feature"), repo_id=1, auto_review=True,
                        curator_mode=CURATOR_BOOTSTRAP, curator_allowed=True)
    check("非默认分支：什么都不入队", side == [])

    with tempfile.TemporaryDirectory(prefix="ai-trigger-") as tmp:
        engine = build_engine(f"sqlite:///{Path(tmp) / 'hub.db'}")
        ensure_schema(engine)
        queue = AgentQueue(engine)
        session = queue.session()
        repo = Repo(slug="openfish/candidate", kind="workspace")
        session.add(repo)
        session.commit()
        repo_id = int(repo.id)
        allowed, reason = check_store.curator_should_enqueue(session, repo_id)
        check("无任务时允许入队", allowed, reason)
        task_id = queue.enqueue(repo_id, kind="checks", payload={"suite_scope": "bootstrap"})
        session.expire_all()
        allowed, reason = check_store.curator_should_enqueue(session, repo_id)
        check("已有排队中的提案 → 拒绝第二个（去重）", not allowed, reason)
        check("拒绝原因点名进行中的任务", "checks" in reason, reason)
        session.close()

        # Finish the task, then the cooling-off window applies.
        from models.base import utcnow as _utcnow

        with engine.begin() as conn:
            conn.execute(text(
                "UPDATE agent_tasks SET status='done', created_at=:now WHERE id=:id"
            ), {"now": _utcnow(), "id": task_id})
        session = queue.session()
        allowed, reason = check_store.curator_should_enqueue(
            session, repo_id, min_interval_seconds=3600
        )
        check("冷却期内（rate limit）拒绝", not allowed, reason)
        allowed, _ = check_store.curator_should_enqueue(session, repo_id, min_interval_seconds=0)
        check("无冷却时允许", allowed)

        # Suite-hash level dedup (the hash is only known after a clone).
        with engine.begin() as conn:
            conn.execute(text(
                "UPDATE agent_tasks SET status='queued', payload=:payload WHERE id=:id"
            ), {"payload": json.dumps({"suite_hash": "H"}), "id": task_id})
        session.expire_all()
        check(
            "同一 suite hash 的在飞提案被识别",
            check_store.proposal_in_flight_for_hash(session, repo_id, suite_hash="H"),
        )
        check(
            "不同 suite hash 不误伤",
            not check_store.proposal_in_flight_for_hash(session, repo_id, suite_hash="OTHER"),
        )
        with engine.begin() as conn:
            conn.execute(text("UPDATE agent_tasks SET status='done' WHERE id=:id"),
                         {"id": task_id})

        # bootstrap is once-only; auto keeps re-proposing (subject to cooldown).
        check_store.record_suite_snapshot(
            session, repo_id=repo_id, kind="ai",
            suite=CheckSuite(checks=[CheckCommand(id="ai:x", argv=["true"])],
                             source=SOURCE_AGENT_CHECKS),
            base_sha="a" * 40, author="ai", status="proposed",
        )
        session.commit()
        allowed, reason = check_store.curator_should_enqueue(
            session, repo_id, mode="bootstrap", min_interval_seconds=0
        )
        check("bootstrap 只提案一次（已有 AI 快照 → 拒绝）", not allowed, reason)
        check("拒绝原因说明 bootstrap 一次性", "bootstrap" in reason, reason)
        allowed, reason = check_store.curator_should_enqueue(
            session, repo_id, mode="auto", min_interval_seconds=0
        )
        check("auto 模式不受『已提案』限制（仍受冷却约束）", allowed, reason)
        session.close()
        engine.dispose()


# ── C: DB provenance / validation / run history ──────────────────────

def check_check_store() -> None:
    print("\nC：DB provenance / 验证 / 运行历史")
    from services.gates import SOURCE_AGENT_CHECKS, VALIDATION_VALIDATED

    with tempfile.TemporaryDirectory(prefix="ai-store-") as tmp:
        engine = build_engine(f"sqlite:///{Path(tmp) / 'hub.db'}")
        ensure_schema(engine)
        queue = AgentQueue(engine)
        session = queue.session()
        repo = Repo(slug="openfish/store", kind="workspace")
        session.add(repo)
        session.commit()
        repo_id = int(repo.id)
        session.close()

        suite = CheckSuite(
            checks=[CheckCommand(id="ai:calc", argv=["true"], validation=VALIDATION_VALIDATED)],
            source=SOURCE_AGENT_CHECKS,
        )
        session = queue.session()
        row = check_store.record_suite_snapshot(
            session, repo_id=repo_id, kind="ai", suite=suite, base_sha="a" * 40,
            author="ai", model="test-model", task_id=1, status="proposed",
        )
        first_id = int(row.id)
        again = check_store.record_suite_snapshot(
            session, repo_id=repo_id, kind="ai", suite=suite, base_sha="a" * 40,
            author="ai", model="test-model", task_id=1, status="proposed",
        )
        session.commit()
        check("快照按 (repo,kind,hash) 幂等", int(again.id) == first_id)
        check("proposed 提案不是 active 快照",
              check_store.active_suite(session, repo_id, kind="ai") is None)

        check_store.record_validation(
            session, repo_id=repo_id,
            result=CheckValidation(
                check_id="ai:calc", status="validated", baseline_passed=True,
                faults_attempted=2, faults_detected=1, detected_by=("seed",),
                reason="proven",
            ),
            base_sha="a" * 40,
        )
        check_store.record_runs(session, repo_id=repo_id, records=[
            CheckRunRecord(check_id="ai:calc", state=STATUS_PASSED,
                           suite_hash="h", commit_sha="a" * 40,
                           validation=VALIDATION_VALIDATED),
            CheckRunRecord(check_id="ai:calc", state=STATUS_FAILED,
                           suite_hash="h", commit_sha="b" * 40,
                           validation=VALIDATION_VALIDATED),
        ])
        session.commit()
        session.close()

        store = check_store.DbCheckHistoryStore(queue.session, repo_id)
        store.append([CheckRunRecord(check_id="ai:calc", state=STATUS_PASSED,
                                     validation=VALIDATION_VALIDATED)])
        summaries = {item.check_id: item for item in store.summaries()}
        check("DB 历史可累加并算 flake",
              summaries["ai:calc"].runs == 3
              and abs(summaries["ai:calc"].flake_rate - 1 / 3) < 1e-9,
              repr(summaries["ai:calc"].as_payload()))
        check("DB 记录的 validation 状态可读回",
              summaries["ai:calc"].validation == VALIDATION_VALIDATED)
        session = queue.session()
        validations = session.query(CheckValidationRow).filter_by(repo_id=repo_id).all()
        check("check_validations 落库且 validated=True",
              len(validations) == 1 and bool(validations[0].validated))
        session.close()
        engine.dispose()


# ── D: the curator loop, end to end, offline ─────────────────────────

class _FakeForgejo:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def create_pull_request(self, repo: str, *, head: str, base: str,
                            title: str, body: str = "") -> str:
        self.calls.append({"repo": repo, "head": head, "base": base,
                           "title": title, "body": body})
        return "http://forgejo.local/openfish/candidate/pulls/1"


def check_curator_loop() -> None:
    print("\nD：curator 提案闭环（假模型命令 + 假 Forgejo，真 git）")
    git = shutil.which("git")
    if git is None:
        check("需要 git 才能离线验证闭环", False, "PATH 上没有 git")
        return

    with tempfile.TemporaryDirectory(prefix="ai-loop-") as tmp:
        base = Path(tmp)
        git_root = base / "git"
        seed = base / "seed"
        bare = git_root / "openfish" / "candidate.git"
        bare.parent.mkdir(parents=True)
        seed.mkdir()

        (seed / "src").mkdir()
        (seed / "src" / "calc.py").write_text(
            "def gt(a, b):\n    return a > b\n", encoding="utf-8"
        )
        (seed / "verify.py").write_text(
            "import sys\n"
            "from src.calc import gt\n"
            "sys.exit(0 if gt(2, 1) and not gt(1, 2) else 1)\n",
            encoding="utf-8",
        )

        def run_git(*args: str, cwd: Path) -> str:
            proc = subprocess.run([git, *args], cwd=str(cwd), capture_output=True,
                                  text=True, check=False)
            if proc.returncode != 0:
                raise RuntimeError(f"git {args} failed: {proc.stderr.strip()[:200]}")
            return proc.stdout.strip()

        run_git("init", "-q", cwd=seed)
        run_git("config", "user.email", "gate@openfish.invalid", cwd=seed)
        run_git("config", "user.name", "gate", cwd=seed)
        run_git("add", "-A", cwd=seed)
        run_git("commit", "-qm", "seed", cwd=seed)
        sha = run_git("rev-parse", "HEAD", cwd=seed)
        run_git("init", "-q", "--bare", str(bare), cwd=base)
        run_git("push", "-q", str(bare), "HEAD:refs/heads/main", cwd=seed)

        review_script = base / "review.py"
        review_script.write_text(
            "import json, os, pathlib, sys\n"
            "kind = os.environ.get('OPENFISH_TASK_KIND')\n"
            "repo = pathlib.Path(os.environ['OPENFISH_REPO_DIR'])\n"
            "if kind == 'checks':\n"
            "    d = repo / '.agent' / 'checks'\n"
            "    d.mkdir(parents=True, exist_ok=True)\n"
            "    cmd = json.dumps(sys.executable + ' verify.py')\n"
            "    (d / 'checks.yml').write_text(\n"
            "        'version: 1\\nchecks:\\n  - id: calc\\n'\n"
            "        f'    command: {cmd}\\n'\n"
            "        '    validated: false\\n')\n"
            "print('[]')\n",
            encoding="utf-8",
        )

        previous_env = {
            name: os.environ.get(name)
            for name in ("FORGEJO_GIT_BASE_URL", "FORGEJO_BASE_URL", "AGENT_WORK_ROOT")
        }
        os.environ["FORGEJO_GIT_BASE_URL"] = f"file://{git_root}"
        os.environ["FORGEJO_BASE_URL"] = f"file://{git_root}"
        os.environ["AGENT_WORK_ROOT"] = str(base / "work")
        try:
            engine = build_engine(f"sqlite:///{base / 'hub.db'}")
            ensure_schema(engine)
            queue = AgentQueue(engine)
            session = queue.session()
            repo = Repo(slug="candidate", kind="workspace",
                        forgejo_repo="openfish/candidate", default_branch="main")
            session.add(repo)
            session.commit()
            repo_id = int(repo.id)
            session.close()

            fake = _FakeForgejo()
            handler = agent_worker.build_handler(
                engine=engine,
                review_command=f"{sys.executable} {review_script}",
                forgejo_client=fake,
            )
            task_id = queue.enqueue(
                repo_id, kind="checks",
                payload={"commit_sha": sha, "suite_scope": "bootstrap"},
            )
            claimed = queue.claim(worker="gate-worker")
            check("checks 任务可以 enqueue + lease",
                  claimed is not None and claimed.kind == "checks", repr(claimed))
            error = ""
            result_ref = ""
            try:
                result_ref = handler(claimed)
            except Exception as exc:  # noqa: BLE001 - reported as a failed assertion
                error = f"{type(exc).__name__}: {exc}"
            check("curator 任务成功结束", bool(result_ref), error)

            check("开了一个 PR（经受限客户端）", len(fake.calls) == 1, repr(fake.calls))
            call = fake.calls[0] if fake.calls else {}
            check("PR 标题带提案标记",
                  PROPOSAL_MARKER in str(call.get("title")), str(call.get("title")))
            check("PR 正文带提案标记与人工合并声明",
                  PROPOSAL_MARKER in str(call.get("body"))
                  and "不会自动合并" in str(call.get("body")))
            # Retries must not collide with a previous attempt's branch, so the
            # name carries the attempt number (0 → a1).
            expected_branch = f"agent/checks-{task_id}-a{claimed.attempts + 1}"
            check("PR head 是 agent/checks-<task>-a<attempt>",
                  call.get("head") == expected_branch, str(call.get("head")))
            check("PR base 是默认分支", call.get("base") == "main", str(call.get("base")))

            branch_ref = run_git("rev-parse", expected_branch, cwd=bare)
            check("提案分支真的推到了 origin", bool(branch_ref), branch_ref)
            manifest = run_git(
                "show", f"{expected_branch}:.agent/checks/checks.yml", cwd=bare,
            )
            check("证伪验证器把 validated 改写为 true（不是模型自称）",
                  "validated: true" in manifest, manifest)

            session = queue.session()
            snapshot = session.query(CheckSuiteSnapshot).filter_by(repo_id=repo_id).all()
            validations = session.query(CheckValidationRow).filter_by(repo_id=repo_id).all()
            runs = session.query(CheckRun).filter_by(repo_id=repo_id).all()
            session.close()
            check("provenance：AI 快照落库（author=ai, status=proposed）",
                  len(snapshot) == 1 and snapshot[0].author == "ai"
                  and snapshot[0].status == "proposed" and bool(snapshot[0].base_sha),
                  repr([row.to_dict() for row in snapshot]))
            check("validation：证伪结果落库且 validated=True",
                  len(validations) == 1 and bool(validations[0].validated),
                  repr([row.to_dict() for row in validations]))
            check("run history：运行历史落库", len(runs) >= 1,
                  repr([row.to_dict() for row in runs]))
            engine.dispose()
        finally:
            for name, value in previous_env.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value


def main() -> int:
    check_false_green()
    check_tri_state()
    check_resolution_order()
    check_freeze_and_guard()
    check_task_kind_env()
    check_policy_checks_field()
    check_no_merge_surface()
    check_trust_ladder()
    check_two_suites_and_findings()
    check_validator()
    check_curator_role()
    check_checks_task_kind_migration()
    check_curator_trigger()
    check_check_store()
    check_curator_loop()

    if _problems:
        print(f"\n❌ {len(_problems)} check(s) failed")
        for label in _problems:
            print("   - " + label)
        return 1
    print("\n✅ AI checks invariant holds: frozen suite, path guard, unverified is not green")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
