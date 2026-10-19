#!/usr/bin/env python
"""Gate: the statistics endpoints must never compute file sizes on the request path.

The defect this locks down
--------------------------
``GET /api/v1/packages`` used to compute the whole statistics dictionary per
request, including ``stat``-ing every package file (the index stores
``size=0``).  On the 1.5 TB / 160k-file intranet deployment that took longer than
the 30 s reverse proxy in front of it: the SPA got a ``504`` shaped like a
success, rendered an empty table, and the overview page said the registry had no
packages.  The overview page called that same endpoint for three counters, and
non-admins cannot read ``/admin/stats``.

The fixed contract, asserted here in a child process against a real Flask app:

1. **Cold cache**: ``/api/v1/packages`` and ``/api/v1/stats/overview`` answer
   from the in-memory index with ``sizes_ready: false``, and they do **not**
   call ``Path.stat`` at all (the test patches it to raise).
2. **Warm/cached**: with the computed dictionary in the cache, both endpoints
   answer **without calling ``services.stats.compute``** (patched to raise).
3. **Persisted snapshot**: a second boot with an empty ``PACKAGES_DIR`` serves
   the previous numbers from ``stats_snapshot.json`` — again without computing.

Run from the repository root::

    python .agent/checks/check_stats_request_path.py
    # dev box without a `python` on PATH:
    backend/.venv/bin/python .agent/checks/check_stats_request_path.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = REPO_ROOT / "backend"
TIMEOUT_S = 120

_COMMON_CHILD = r"""
import os
import sys
import threading

# The extension schedules its first refresh on a `threading.Timer` at import
# time; a no-op Timer keeps the check deterministic (no concurrent warm-up
# racing the assertions).  The snapshot restore under test does not use it.
class _NoTimer:
    def __init__(self, *args, **kwargs):
        self.daemon = False
        self.name = ""

    def start(self):
        pass


threading.Timer = _NoTimer

client = None
cache = None

def _client(app):
    return app.test_client()

AUTH = {"Authorization": "Basic YWRtaW46c2VjcmV0"}
"""

_COLD_AND_CACHED = _COMMON_CHILD + r"""
import services.stats as stats_module
from pathlib import Path

from app import app

cache = app.extensions["caching"]
assert cache.get(stats_module.STATS_CACHE_KEY) is None, "test starts with a cold cache"
client = _client(app)

# ── 1. cold cache: counts only, no filesystem stat on the request path ──
real_stat = Path.stat

def _no_stat(self, *args, **kwargs):
    raise AssertionError(f"Path.stat called on the request path: {self}")

Path.stat = _no_stat
try:
    response = client.get("/api/v1/packages", headers=AUTH)
    assert response.status_code == 200, response.data[:400]
    packages = response.get_json()
    assert [p["name"] for p in packages] == ["demo-pkg-1", "demo-pkg-2", "demo-pkg-3"], packages
    assert all(p["total_size"] == 0 for p in packages), packages

    response = client.get("/api/v1/stats/overview", headers=AUTH)
    assert response.status_code == 200, response.data[:400]
    overview = response.get_json()
    assert overview["package_count"] == 3, overview
    assert overview["file_count"] == 3, overview
    assert overview["sizes_ready"] is False, overview
finally:
    Path.stat = real_stat

# ── 2. cached dictionary: served verbatim, compute() never called ──────
data = stats_module.compute(
    app.extensions["pypi_index"], app.extensions["api_key_manager"]
)
cache.set(stats_module.STATS_CACHE_KEY, data, timeout=0)
assert stats_module.save_snapshot(data), "snapshot must be writable"

def _boom(*args, **kwargs):
    raise AssertionError("services.stats.compute called on the request path")

stats_module.compute = _boom
try:
    response = client.get("/api/v1/stats/overview", headers=AUTH)
    assert response.status_code == 200, response.data[:400]
    overview = response.get_json()
    assert overview["sizes_ready"] is True, overview
    assert overview["total_storage"] == 6000, overview

    response = client.get("/api/v1/packages", headers=AUTH)
    assert response.status_code == 200, response.data[:400]
    packages = response.get_json()
    assert sorted(p["total_size"] for p in packages) == [1000, 2000, 3000], packages

finally:
    pass

print("child1: cold-cache fallback and cached reads OK")
"""

_SNAPSHOT_RESTORE = _COMMON_CHILD + r"""
import services.stats as stats_module

def _boom(*args, **kwargs):
    raise AssertionError("a boot with a persisted snapshot must not recompute statistics")

stats_module.compute = _boom

from app import app

cache = app.extensions["caching"]
assert cache.get(stats_module.STATS_CACHE_KEY) is not None, "snapshot was not restored"
client = _client(app)

response = client.get("/api/v1/stats/overview", headers=AUTH)
assert response.status_code == 200, response.data[:400]
overview = response.get_json()
assert overview["package_count"] == 3, overview
assert overview["file_count"] == 3, overview
assert overview["total_storage"] == 6000, overview
assert overview["sizes_ready"] is True, overview

response = client.get("/api/v1/packages", headers=AUTH)
assert response.status_code == 200, response.data[:400]
assert sorted(p["total_size"] for p in response.get_json()) == [1000, 2000, 3000]

print("child2: persisted snapshot served after restart OK")
"""


def _run_child(name: str, code: str, env: dict[str, str]) -> None:
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(BACKEND_DIR),
        env=env,
        capture_output=True,
        text=True,
        timeout=TIMEOUT_S,
    )
    if result.returncode != 0:
        sys.stderr.write(f"\n[{name}] FAILED\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}\n")
        raise SystemExit(1)
    for line in result.stdout.splitlines():
        if "OK" in line:
            print(line)


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="openfish-stats-check-") as tmp:
        base = Path(tmp)
        packages = base / "packages"
        packages.mkdir()
        for index, name in enumerate(("demo_pkg_1", "demo_pkg_2", "demo_pkg_3"), start=1):
            target = packages / f"{name}-1.0.0-py3-none-any.whl"
            target.write_bytes(b"x" * (1000 * index))
        for sub in ("pybuilds", "nodebuilds", "empty-packages"):
            (base / sub).mkdir()

        env = dict(os.environ)
        env.update({
            "PACKAGES_DIR": str(packages),
            "PYTHON_BUILDS_DIR": str(base / "pybuilds"),
            "NODE_BUILDS_DIR": str(base / "nodebuilds"),
            "API_KEYS_FILE": str(base / "cpypiserver.db"),
            "STATS_SNAPSHOT_FILE": str(base / "stats_snapshot.json"),
            "AUTH_USERNAME": "admin",
            "AUTH_ASSERT": "secret",
            "WATCH_PACKAGES": "false",
            "SEED_CATALOGS": "false",
            "LOG_LEVEL": "WARNING",
            "STATS_CACHE_SECONDS": "7200",
        })

        _run_child("cold-and-cached", _COLD_AND_CACHED, env)
        assert (base / "stats_snapshot.json").is_file(), "snapshot file was not written"

        # Second boot: empty catalog, same snapshot.  The restored numbers must
        # be served even though the index now holds nothing.
        env["PACKAGES_DIR"] = str(base / "empty-packages")
        _run_child("snapshot-restore", _SNAPSHOT_RESTORE, env)

    print("PASS: statistics endpoints are cache-first; snapshot restores after restart")


if __name__ == "__main__":
    main()
