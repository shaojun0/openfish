#!/usr/bin/env python
"""Gate: a deployment mounted under a path prefix keeps it in every URL.

The server can be reached under a client-visible prefix in two ways:

* the edge nginx strips it and sends ``X-Forwarded-Prefix`` (the shipped
  topology, and what a first-level ``location /pypi`` produces);
* ``ROUTE_PREFIX`` is set and Flask mounts every route under it.

The URL builders used to read only ``settings.server.route_prefix``, so under
the first style the catalog documents (``/api/v1/{python-builds,node-builds,
npm,debian,tools,docker}``), the machine-facing index pages and the ``Link``
header emitted ``/python-builds/...`` without the prefix — a link a client then
follows to a 404.  This gate pins both styles, and pins the "one implementation"
rule: a route module may not re-derive the prefix from settings.

Run from the repository root::

    python .agent/checks/check_external_prefix.py
    # dev box without a `python` on PATH:
    backend/.venv/bin/python .agent/checks/check_external_prefix.py
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

#: A URL builder that adds the prefix by hand instead of asking
#: ``services.urlprefix`` — exactly the defect this gate exists for.
_FORBIDDEN = "settings.server.route_prefix.rstrip"

#: Style 1: the proxy strips the prefix and sends the header.  ProxyFix turns
#: that into SCRIPT_NAME, so the routes themselves stay at the root.
HEADER_CHILD = r"""
import sys
sys.path.insert(0, ".")
from app import app

client = app.test_client()
resp = client.get("/health", headers={"X-Forwarded-Prefix": "/pypi"})
assert resp.status_code == 200, resp.status_code
link = resp.headers.get("Link", "")
assert "</pypi/openapi.json>" in link, f"Link header lost the prefix: {link!r}"

resp = client.get("/openapi.json", headers={"X-Forwarded-Prefix": "/pypi"})
servers = resp.get_json()["servers"]
assert servers and servers[0]["url"].endswith("/pypi"), servers

with app.test_request_context("/", environ_overrides={"SCRIPT_NAME": "/pypi"}):
    from services.urlprefix import external_base_url, external_prefix
    from routes.hub import _tools_prefix
    from routes.hub_common import spa_url

    assert external_prefix() == "/pypi", external_prefix()
    assert external_base_url().endswith("/pypi"), external_base_url()
    assert _tools_prefix() == "/pypi/tools", _tools_prefix()
    assert spa_url("/npm") == "/pypi/npm", spa_url("/npm")

print("header style OK")
"""

#: Style 2: ROUTE_PREFIX is configured and the routes are mounted under it;
#: the request context carries no SCRIPT_NAME, so the configured value is used.
ROUTE_PREFIX_CHILD = r"""
import sys
sys.path.insert(0, ".")
from app import app

with app.test_request_context("/pypi/api/v1/tools"):
    from services.urlprefix import external_prefix
    from routes.hub import _tools_prefix

    assert external_prefix() == "/pypi", external_prefix()
    assert _tools_prefix() == "/pypi/tools", _tools_prefix()

client = app.test_client()
resp = client.get("/pypi/openapi.json")
assert resp.status_code == 200, resp.status_code
servers = resp.get_json()["servers"]
assert servers and servers[0]["url"].endswith("/pypi"), servers
print("route-prefix style OK")
"""


def _state_env(root: Path) -> dict[str, str]:
    """Keep the child's writes inside *root* instead of the checkout."""
    state, data = root / "state", root / "data"
    return {
        "AUTH_ENABLED": "true",
        "SEED_CATALOGS": "0",
        "SECRET_KEY": "check-external-prefix",
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


def _run_child(script: str, overrides: dict[str, str]) -> tuple[int, str]:
    env = dict(os.environ)
    env.update(overrides)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(BACKEND_DIR),
        env=env,
        capture_output=True,
        text=True,
        timeout=TIMEOUT_S,
    )
    return proc.returncode, proc.stdout + proc.stderr


def main() -> int:
    failures: list[str] = []
    checked = 0

    # ── one implementation ─────────────────────────────────────────
    offenders = sorted(
        path.relative_to(REPO_ROOT)
        for path in (BACKEND_DIR / "routes").rglob("*.py")
        if _FORBIDDEN in path.read_text(encoding="utf-8")
    )
    checked += 1
    if offenders:
        failures.append(
            "route modules re-derive the prefix from settings (use "
            "services.urlprefix): " + ", ".join(map(str, offenders))
        )

    with tempfile.TemporaryDirectory(prefix="openfish-prefix-") as tmp:
        base = _state_env(Path(tmp))

        # ── style 1: edge proxy strips + X-Forwarded-Prefix ────────
        checked += 1
        code, output = _run_child(HEADER_CHILD, base)
        if code != 0:
            tail = output.strip().splitlines()[-1] if output.strip() else "(no output)"
            failures.append(f"header style: {tail}")

        # ── style 2: ROUTE_PREFIX mounts the routes ────────────────
        checked += 1
        code, output = _run_child(ROUTE_PREFIX_CHILD, {**base, "ROUTE_PREFIX": "/pypi"})
        if code != 0:
            tail = output.strip().splitlines()[-1] if output.strip() else "(no output)"
            failures.append(f"route-prefix style: {tail}")

    for line in failures:
        print(f"FAIL {line}")
    print(
        f"check_external_prefix: {checked} cases, {len(failures)} failure(s)",
        file=sys.stderr,
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
