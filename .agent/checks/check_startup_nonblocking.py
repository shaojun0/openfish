#!/usr/bin/env python
"""Gate: the first admin-stats computation must not run on the import path.

``StatsRefreshExtension.init_app`` runs while ``app.py`` is being imported, i.e.
before gunicorn binds the port.  It used to call ``services.stats.compute``
synchronously, and that walk does ``stat`` on every package file in
``PACKAGES_DIR`` (the index scan stores ``size=0``, so the first computation
fills them in).  On a large catalog — or on network/cold storage — the first
statistics pass therefore blocked the HTTP listener for many minutes.

The container then exceeded its healthcheck grace, and because the edge nginx
``depends_on: backend: condition: service_healthy``, ``docker compose up -d``
aborted with ``dependency failed to start: container openfish-backend is
unhealthy`` while nginx stayed in ``Created``.  That is the "backend Error and
nginx is down too" cold-start symptom in docker/README.md §5.

This gate imports ``app`` with ``services.stats.compute`` replaced by a function
that sleeps.  A synchronous first refresh shows up directly as a slow import;
the fixed version schedules it on the same background thread that handles later
refreshes, so the import returns immediately and ``/health`` answers.

Run from the repository root::

    python .agent/checks/check_startup_nonblocking.py
    # dev box without a `python` on PATH:
    backend/.venv/bin/python .agent/checks/check_startup_nonblocking.py
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

#: How long the patched ``stats.compute`` sleeps.  With the defect the import
#: takes at least this long and fails the assertion below; with the fix it is
#: still sleeping on a daemon thread when the child exits.
SLOW_COMPUTE_SECONDS = 20.0

#: The import budget.  A healthy import is ~2 s here; anything that still has to
#: wait for a statistics pass cannot fit.
MAX_IMPORT_SECONDS = 10.0
MAX_HEALTH_SECONDS = 5.0

CHILD = f"""
import sys, time
sys.path.insert(0, ".")
import services.stats as stats_module

def _slow_compute(*args, **kwargs):
    time.sleep({SLOW_COMPUTE_SECONDS!r})
    return {{}}

# Patched *before* ``app`` imports: both the extension
# (``from services.stats import compute as compute_stats``) and routes/admin.py
# resolve the name at import time, so the sleeper is what they get.
stats_module.compute = _slow_compute

t0 = time.time()
from app import app  # noqa: E402
import_seconds = time.time() - t0
assert import_seconds < {MAX_IMPORT_SECONDS!r}, (
    f"import app took {{import_seconds:.1f}}s with a slow stats.compute; "
    "the first refresh is back on the import path, so a cold start with a "
    "large PACKAGES_DIR blocks gunicorn past its healthcheck grace and nginx "
    "never starts"
)

client = app.test_client()
t1 = time.time()
resp = client.get("/health")
health_seconds = time.time() - t1
assert resp.status_code == 200, resp.status_code
assert health_seconds < {MAX_HEALTH_SECONDS!r}, f"/health took {{health_seconds:.1f}}s"
print(f"import={{import_seconds:.2f}}s health={{health_seconds:.2f}}s")
"""


def _state_env(root: Path) -> dict[str, str]:
    """Keep the child's writes inside *root* instead of the checkout."""
    state, data = root / "state", root / "data"
    return {
        "AUTH_ENABLED": "true",
        "SEED_CATALOGS": "0",
        "SECRET_KEY": "check-startup-nonblocking",
        "API_KEYS_FILE": str(data / "cpypiserver.db"),
        "DEVICE_CODES_FILE": str(data / "device_codes.json"),
        "PACKAGES_DIR": str(state / "packages"),
        "PYTHON_BUILDS_DIR": str(state / "python-builds"),
        "NODE_BUILDS_DIR": str(state / "node-builds"),
        "TOOLS_DIR": str(state / "tools"),
        "NPM_DIR": str(state / "npm"),
        "DOCKER_DIR": str(state / "docker-images"),
        "DEBIAN_DIR": str(state / "debian"),
        "DOCS_DIR": str(state / "docs"),
        "NPM_CACHE_DIR": str(state / "cache" / "npm"),
        "DOCKER_CACHE_DIR": str(state / "cache" / "docker"),
        "DEBIAN_CACHE_DIR": str(state / "cache" / "debian"),
    }


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="openfish-startup-") as tmp:
        env = dict(os.environ)
        env.update(_state_env(Path(tmp)))
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        proc = subprocess.run(
            [sys.executable, "-c", CHILD],
            cwd=str(BACKEND_DIR),
            env=env,
            capture_output=True,
            text=True,
            timeout=TIMEOUT_S,
        )

    output = (proc.stdout + proc.stderr).strip()
    if proc.returncode != 0:
        print(output)
        print(f"FAIL: startup is blocked by the first stats computation ({output[-300:]})")
        return 1

    print(output)
    print("startup non-blocking OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
