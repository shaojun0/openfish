#!/usr/bin/env python
"""Gate: a 401 to the SPA must not carry a Basic challenge.

The defect this locks down
--------------------------
``_unauthorized()`` used to attach ``WWW-Authenticate: Basic`` to **every**
401.  Package managers need that challenge — pip/npm/docker/twine only offer a
credential after a 401 names the scheme — but the SPA does not: Chromium answers
a Basic challenge with its native credential dialog, and while the challenge is
outstanding the XHR can sit without ever firing ``loadend``.  The SPA's 401
interceptor therefore never runs ``redirectToLogin()``, and a session that has
expired shows the browser's Basic prompt instead of the configured OAuth2/4A
round trip.  On an intranet this presents as "only the crude Basic login
window, no redirect to 4A".

The fix marks every SPA request with ``X-OpenFish-Spa: 1`` and leaves the
challenge off for exactly those requests.  This check drives the real app:

* a plain JSON request (machine client) to a guarded endpoint still gets
  ``401`` + ``WWW-Authenticate: Basic``;
* the same request with the SPA header gets ``401`` without the challenge, so
  the browser keeps the response and the SPA's interceptor can navigate;
* a guarded API that lives on an ``_API_BLUEPRINTS`` blueprint behaves the
  same way;
* ``/api/v1/session`` still answers 200 anonymously, and the login/callback
  redirect paths (``/auth/login`` and a browser navigation to a guarded route)
  still point at the identity provider — the challenge change must not disturb
  them;
* the frontend still declares the same header name, so the two ends cannot
  drift apart silently.

Run from the repository root::

    python .agent/checks/check_spa_401_challenge.py
    # dev box without a `python` on PATH:
    backend/.venv/bin/python .agent/checks/check_spa_401_challenge.py
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

#: The marker the SPA sends; both ends must agree on it.
SPA_HEADER = "X-OpenFish-Spa"
AUTHORIZE_URL = "https://4a.example/oauth/authorize"

CHILD = r"""
import sys
sys.path.insert(0, ".")

from app import app

client = app.test_client()
SPA = {"X-OpenFish-Spa": "1"}
JSON = {"Accept": "application/json"}
BROWSER = {"Accept": "text/html,application/xhtml+xml"}
AUTHORIZE = "https://4a.example/oauth/authorize"


def check(label, response, *, status, challenge=None):
    got = response.headers.get("WWW-Authenticate")
    assert response.status_code == status, (
        f"{label}: expected {status}, got {response.status_code} "
        f"(WWW-Authenticate={got!r}, body={response.get_data(as_text=True)[:160]!r})"
    )
    if challenge is None:
        assert got is None, (
            f"{label}: SPA requests must not be challenged with Basic, got {got!r}"
        )
    else:
        assert got == challenge, f"{label}: expected challenge {challenge!r}, got {got!r}"


def location(label, response, prefix):
    assert response.status_code == 302, (
        f"{label}: expected the OAuth2 redirect, got {response.status_code} "
        f"(Location={response.headers.get('Location')!r})"
    )
    loc = response.headers.get("Location") or ""
    assert loc.startswith(prefix), f"{label}: expected {prefix!r}, got {loc!r}"


# 1. Machine client / non-SPA caller keeps the Basic challenge it needs.
check(
    "machine client, stats/overview",
    client.get("/api/v1/stats/overview", headers=JSON),
    status=401,
    challenge='Basic realm="cpypiserver"',
)
check(
    "machine client, api/v1/packages",
    client.get("/api/v1/packages", headers=JSON),
    status=401,
    challenge='Basic realm="cpypiserver"',
)

# 2. The SPA gets the same 401 without the challenge, so the browser does not
#    open its native credential dialog and the axios interceptor can redirect.
check(
    "SPA, stats/overview",
    client.get("/api/v1/stats/overview", headers={**JSON, **SPA}),
    status=401,
)
check(
    "SPA, api/v1/packages",
    client.get("/api/v1/packages", headers={**JSON, **SPA}),
    status=401,
)

# 3. The unguarded session probe and the identity-provider redirects are
#    unchanged by this fix.
check(
    "SPA, /api/v1/session",
    client.get("/api/v1/session", headers=SPA),
    status=200,
)
location("machine client, /auth/login", client.get("/auth/login", headers=BROWSER), AUTHORIZE)
location(
    "browser navigation to a guarded route",
    client.get("/api/v1/stats/overview", headers=BROWSER),
    AUTHORIZE,
)

print("PASS: SPA 401s are challenge-free; machine clients and the OAuth2 redirect still work")
"""


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="openfish-spa-401-") as tmp:
        base = Path(tmp)
        for sub in ("packages", "pybuilds", "nodebuilds"):
            (base / sub).mkdir()

        env = dict(os.environ)
        env.update({
            "PACKAGES_DIR": str(base / "packages"),
            "PYTHON_BUILDS_DIR": str(base / "pybuilds"),
            "NODE_BUILDS_DIR": str(base / "nodebuilds"),
            "API_KEYS_FILE": str(base / "cpypiserver.db"),
            "STATS_SNAPSHOT_FILE": str(base / "stats_snapshot.json"),
            "WATCH_PACKAGES": "false",
            "SEED_CATALOGS": "false",
            "LOG_LEVEL": "WARNING",
            "STATS_CACHE_SECONDS": "7200",
            # A realistic OAuth2 deployment: the authorize URL exists (so
            # /auth/login has somewhere to redirect), Basic is also configured
            # (so the old code really would have challenged every 401), and the
            # token/introspection URLs are never called by these anonymous
            # requests.
            "OAUTH2_AUTHORIZE_URL": AUTHORIZE_URL,
            "OAUTH2_TOKEN_URL": "https://4a.example/oauth/token",
            "OAUTH2_INTROSPECT_URL": "https://4a.example/oauth/introspect",
            "OAUTH2_CLIENT_ID": "cpypiserver",
            "OAUTH2_CLIENT_SECRET": "secret",
            "AUTH_USERNAME": "admin",
            "AUTH_ASSERT": "password",
        })

        result = subprocess.run(
            [sys.executable, "-c", CHILD],
            cwd=str(BACKEND_DIR),
            env=env,
            capture_output=True,
            text=True,
            timeout=TIMEOUT_S,
        )
        if result.returncode != 0:
            sys.stderr.write(f"\nFAILED\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}\n")
            raise SystemExit(1)
        for line in result.stdout.splitlines():
            if "PASS" in line:
                print(line)

    # The marker is a contract between the two ends; a rename on either side
    # silently restores the native dialog, so check the frontend source too.
    client_ts = REPO_ROOT / "frontend/src/api/client.ts"
    source = client_ts.read_text(encoding="utf-8")
    assert SPA_HEADER in source, (
        f"frontend/src/api/client.ts no longer sends {SPA_HEADER}; the backend "
        "will challenge the SPA with Basic again"
    )
    print(f"PASS: frontend still sends {SPA_HEADER}")


if __name__ == "__main__":
    main()
