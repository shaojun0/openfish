#!/usr/bin/env python
"""Gate: a logged-in browser must not introspect its OAuth2 token per request.

The defect this locks down
--------------------------
The login callback only stored the access token in the session cookie; identity
resolution was left to ``try_session``, which then introspected that token
against the identity provider **on every single request**.  On the intranet the
4A introspection endpoint answers in ~16 s, so every page load and every API call
cost 16 s — ``/api/v1/session``, then ``/api/v1/packages``, then whatever came
next — while the handler itself was fast.  That looks exactly like a slow
SQLite database or a slow endpoint, and nothing in the logs named the real
cause.

The fix persists the account id (``session["uid"]``) once the token has been
resolved, so later requests authenticate from the database.  This check proves
it with a counting fake introspection server:

* the first ``GET /api/v1/session`` with an ``auth_token`` cookie calls the
  provider once and remembers the account;
* the second request, and a permission-guarded API call after it, do **not**
  call the provider again;
* the session cookie now carries ``uid``.

Run from the repository root::

    python .agent/checks/check_session_identity_cache.py
    # dev box without a `python` on PATH:
    backend/.venv/bin/python .agent/checks/check_session_identity_cache.py
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

CHILD = r"""
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

calls = {"introspect": 0}


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler's spelling
        calls["introspect"] += 1
        body = json.dumps({
            "sub": "u-1",
            "email": "u1@corp.example",
            "stuffName": "U1",
            "active": True,
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
os.environ["OAUTH2_INTROSPECT_URL"] = (
    f"http://127.0.0.1:{server.server_address[1]}/introspect"
)

from app import app

client = app.test_client()
with client.session_transaction() as sess:
    sess["auth_token"] = "an-access-token"

response = client.get("/api/v1/session")
assert response.status_code == 200, response.data[:400]
body = response.get_json()
assert body["authenticated"] is True, body
assert calls["introspect"] == 1, f"first request must introspect exactly once, got {calls}"

with client.session_transaction() as sess:
    assert sess.get("uid") is not None, "the account id must be remembered in the session"

# Second request: no identity-provider round trip.
response = client.get("/api/v1/session")
assert response.status_code == 200, response.data[:400]
assert calls["introspect"] == 1, f"second request re-introspected: {calls}"

# A permission-guarded endpoint takes the same path and must be equally cheap.
response = client.get("/api/v1/stats/overview")
assert response.status_code == 200, response.data[:400]
assert calls["introspect"] == 1, f"API call re-introspected: {calls}"

print(f"child: introspection calls after 3 requests = {calls['introspect']} (expected 1)")
print("PASS: a logged-in session authenticates from the database, not the IdP")
"""


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="openfish-session-check-") as tmp:
        base = Path(tmp)
        packages = base / "packages"
        packages.mkdir()
        for sub in ("pybuilds", "nodebuilds"):
            (base / sub).mkdir()

        env = dict(os.environ)
        env.update({
            "PACKAGES_DIR": str(packages),
            "PYTHON_BUILDS_DIR": str(base / "pybuilds"),
            "NODE_BUILDS_DIR": str(base / "nodebuilds"),
            "API_KEYS_FILE": str(base / "cpypiserver.db"),
            "STATS_SNAPSHOT_FILE": str(base / "stats_snapshot.json"),
            "WATCH_PACKAGES": "false",
            "SEED_CATALOGS": "false",
            "LOG_LEVEL": "WARNING",
            "STATS_CACHE_SECONDS": "7200",
            # Configured by the child, once its fake server has a port.
            "OAUTH2_INTROSPECT_URL": "",
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
            if "PASS" in line or "introspection calls" in line:
                print(line)


if __name__ == "__main__":
    main()
