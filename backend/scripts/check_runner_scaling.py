#!/usr/bin/env python
"""Gate: the runner pool scales horizontally on the database, with no broker.

Run from the backend directory (`backend/`)::

    python scripts/check_runner_scaling.py

DEVELOPMENT.md §3.1 rejects Redis/Celery: the deployment is one SQLite file or
one PostgreSQL database, and a queue whose state lives *outside* that database
could disagree with it.  Horizontal scaling therefore has to come from the
schema and the claim query, not from a new service.  This gate pins the three
properties that makes ``--scale runner=N`` true, all offline:

1. **The compose service is scalable.**  ``runner`` carries no fixed
   ``container_name`` (which is what makes ``--scale`` fail), still keeps its
   per-replica limits, and still shares one database with the backend.
2. **A worker identity is unique per process.**  ``worker_id()`` folds host,
   pid and a random suffix together, so N replicas cannot claim as one another.
3. **The claim path is backend-correct.**  PostgreSQL locks the candidate row
   with ``FOR UPDATE SKIP LOCKED``; SQLite takes ``BEGIN IMMEDIATE`` before the
   claim's SELECT.  Both are asserted by *running* the real methods against a
   fake engine/session and compiling the statement each produced.

It also checks that ``workdir_for()`` is per task (so N replicas sharing the
``/work`` bind mount cannot land in one directory) and that neither the queue
nor the dependency manifest has grown a broker.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from sqlalchemy.dialects import postgresql, sqlite  # noqa: E402

from services.agent_queue import AgentQueue, worker_id  # noqa: E402
from services.agent_runner import workdir_for  # noqa: E402

#: ``<repo>/docker/docker-compose.yml`` — this file lives in ``backend/scripts``.
COMPOSE_FILE = REPO_ROOT.parent / "docker" / "docker-compose.yml"

_problems: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    if ok:
        print(f"✅ {label}")
    else:
        _problems.append(label)
        print(f"❌ {label}" + (f" — {detail}" if detail else ""))


# ── Fakes: run the real claim methods without a database ─────────────

class _FakeDialect:
    def __init__(self, name: str) -> None:
        self.name = name


class _FakeEngine:
    """Just enough engine for ``AgentQueue``: a dialect name, nothing to open."""

    def __init__(self, name: str) -> None:
        self.dialect = _FakeDialect(name)


class _NoRow:
    def first(self) -> Any:
        return None


class _DriverRecorder:
    """Records raw DBAPI statements — where ``BEGIN IMMEDIATE`` is issued."""

    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, statement: str, *args: Any, **kwargs: Any) -> None:
        self.statements.append(str(statement))


class _CaptureSession:
    """A session shim that captures the statement the claim built.

    ``AgentQueue.claim`` only needs ``scalars(...).first()``, ``commit`` and
    ``rollback``; ``_serialise_writes`` additionally walks
    ``connection().connection.driver_connection``.  Both are provided so the
    production code path — not a reimplementation — is what gets asserted.
    """

    def __init__(self, captured: list[Any], driver: _DriverRecorder | None = None) -> None:
        self._captured = captured
        self._driver = driver

    def __enter__(self) -> "_CaptureSession":
        return self

    def __exit__(self, *exc: Any) -> bool:
        return False

    def scalars(self, statement: Any) -> _NoRow:
        self._captured.append(statement)
        return _NoRow()

    def connection(self) -> Any:
        return SimpleNamespace(
            connection=SimpleNamespace(driver_connection=self._driver)
        )

    def rollback(self) -> None:
        pass

    def commit(self) -> None:
        pass


def _capture_claim(dialect: str) -> tuple[list[Any], list[str]]:
    captured: list[Any] = []
    driver = _DriverRecorder()
    queue = AgentQueue(_FakeEngine(dialect))
    # Swap only the session factory: ``claim``/``_serialise_writes`` themselves
    # are the production code under test.
    queue.session = lambda: _CaptureSession(captured, driver)  # type: ignore[method-assign]
    claimed = queue.claim(worker="gate-scaling")
    assert claimed is None, f"a fake session must report no task, got {claimed!r}"
    return captured, driver.statements


# ── 1. compose: the runner service can be scaled ─────────────────────

def check_compose() -> None:
    print("\n── compose：runner 可水平扩展 ──────────────────────────────")
    try:
        import yaml
    except ImportError:  # pragma: no cover - pyyaml ships with the project
        check("pyyaml 可用来解析 compose", False, "pyyaml 未安装")
        return

    if not COMPOSE_FILE.is_file():
        check("找到 docker/docker-compose.yml", False, str(COMPOSE_FILE))
        return
    document = yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8")) or {}
    services = document.get("services") or {}
    runner = services.get("runner") or {}

    check("compose 里有 runner 服务", bool(runner), str(sorted(services)))
    check(
        "runner 没有固定 container_name（否则 --scale 会失败）",
        "container_name" not in runner,
        f"container_name={runner.get('container_name')!r}",
    )
    check(
        "runner 仍然只在 profile runner 下启动",
        "runner" in (runner.get("profiles") or []),
        repr(runner.get("profiles")),
    )
    check(
        "其它服务仍保留固定 container_name（多副本不适用于它们）",
        services.get("forgejo", {}).get("container_name") == "openfish-forgejo",
        repr(services.get("forgejo", {}).get("container_name")),
    )
    check(
        "每个副本的资源限制仍在",
        float(runner.get("cpus") or 0) > 0
        and bool(runner.get("mem_limit"))
        and bool(runner.get("pids_limit"))
        and runner.get("read_only") is True
        and runner.get("cap_drop") == ["ALL"]
        and any("no-new-privileges" in str(opt) for opt in runner.get("security_opt") or []),
        repr({k: runner.get(k) for k in
              ("cpus", "mem_limit", "pids_limit", "read_only", "cap_drop", "security_opt")}),
    )
    command = runner.get("command") or []
    check(
        "runner 命令跑的是队列 worker",
        isinstance(command, list)
        and "services.agent_queue" in command
        and "worker" in command
        and "--loop" in command,
        repr(command),
    )
    check(
        "所有副本共用同一个 /work 与同一个数据库",
        (runner.get("environment") or {}).get("AGENT_WORK_ROOT") == "/work"
        and any("agent-work:/work" in str(volume) for volume in runner.get("volumes") or []),
        repr(runner.get("volumes")),
    )


# ── 2. worker identity is unique per process ─────────────────────────

def check_worker_identity() -> None:
    print("\n── worker 身份：每进程唯一 ────────────────────────────────")
    mine = [worker_id() for _ in range(64)]
    check("同一进程内每次调用都不同", len(set(mine)) == len(mine),
          f"unique={len(set(mine))}/{len(mine)}")

    code = "from services.agent_queue import worker_id; print(worker_id())"
    outputs: list[str] = []
    for _ in range(3):
        proc = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(REPO_ROOT), capture_output=True, text=True, check=False,
        )
        outputs.append((proc.stdout or "").strip())
    check("子进程都能打印 worker id", all(outputs) and proc.returncode == 0,
          f"outputs={outputs!r} stderr={proc.stderr[-120:]!r}")
    check("跨进程（N 副本）身份彼此不同且不同于父进程",
          len(set(outputs)) == len(outputs) and not (set(outputs) & set(mine)),
          repr(outputs))


# ── 3. the claim path is correct on both backends ────────────────────

def check_claim_path() -> None:
    print("\n── claim 路径：PG / SQLite 各自正确 ───────────────────────")
    pg_captured, _ = _capture_claim("postgresql")
    check("PostgreSQL claim 真的构造了 SELECT", len(pg_captured) == 1,
          f"captured={len(pg_captured)}")
    if pg_captured:
        pg_sql = str(pg_captured[0].compile(dialect=postgresql.dialect()))
        check("PostgreSQL claim 使用 FOR UPDATE SKIP LOCKED",
              "FOR UPDATE SKIP LOCKED" in pg_sql, pg_sql)
        check("PostgreSQL claim 按 priority DESC, created_at ASC 取一条",
              "ORDER BY agent_tasks.priority DESC" in pg_sql
              and "agent_tasks.created_at ASC" in pg_sql
              and "LIMIT" in pg_sql,
              pg_sql)
    else:
        check("PostgreSQL claim 使用 FOR UPDATE SKIP LOCKED", False, "no statement")

    sqlite_captured, sqlite_driver = _capture_claim("sqlite")
    check("SQLite claim 先取写锁：执行了 BEGIN IMMEDIATE",
          any("BEGIN IMMEDIATE" in statement for statement in sqlite_driver),
          repr(sqlite_driver))
    if sqlite_captured:
        sqlite_sql = str(sqlite_captured[0].compile(dialect=sqlite.dialect()))
        check("SQLite claim 不带 FOR UPDATE（方言不支持）",
              "FOR UPDATE" not in sqlite_sql, sqlite_sql)
    else:
        check("SQLite claim 不带 FOR UPDATE（方言不支持）", False, "no statement")

    check("claim 的写锁只在 SQLite 分支加（PG 由 SKIP LOCKED 保证）",
          not _capture_claim("postgresql")[1],
          repr(_capture_claim("postgresql")[1]))


# ── 4. per-task work directories, and no broker dependency ───────────

def check_isolation_and_deps() -> None:
    print("\n── 每任务工作目录 / 无 broker ─────────────────────────────")
    first, second = workdir_for("/work", "101"), workdir_for("/work", "102")
    check("不同 task_id 映射到不同目录", first != second, f"{first} vs {second}")
    check("同一 task_id 映射稳定", workdir_for("/work", "101") == first)
    retried = workdir_for("/work", "101", 1)
    check("重试使用独立工作目录（不删前一次 checkout）",
          retried != first and "attempt1" in retried.name, str(retried))
    try:
        workdir_for("/work", "../escape")
        check("task_id 不能逃出 /work", False, "竟然通过了")
    except Exception:
        check("task_id 不能逃出 /work", True)

    manifest = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8").lower()
    check("pyproject 没有 redis", "redis" not in manifest)
    check("pyproject 没有 celery", "celery" not in manifest)
    queue_source = (REPO_ROOT / "services" / "agent_queue.py").read_text(encoding="utf-8")
    lowered = queue_source.lower()
    check("queue 代码不 import redis/celery",
          "import redis" not in lowered and "import celery" not in lowered)
    check("worker 对 dead 任务发出可发现告警",
          "死信队列" in queue_source and "status=dead" in queue_source)


# ── main ─────────────────────────────────────────────────────────────

def main() -> int:
    check_compose()
    check_worker_identity()
    check_claim_path()
    check_isolation_and_deps()

    if _problems:
        print(f"\n❌ {len(_problems)} check(s) failed")
        for label in _problems:
            print("   - " + label)
        return 1
    print("\n✅ runner pool scales on the DB-backed queue (no Redis, no broker)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
