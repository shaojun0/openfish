#!/usr/bin/env python
"""Gate: ``AUTH_ENABLED=false`` really does fall through to the `anonymous` role.

Run from the repository root::

    python scripts/check_auth_disabled.py

``AUTH_ENABLED`` is documented as the master switch, and the built-in
``anonymous`` role is documented as "applies to requests that never
authenticated (only reachable with ``AUTH_ENABLED=false``)".  For a while both
statements were false: the guards never read the setting, so an operator who
flipped it to open the mirror to an intranet got **401 on every endpoint**
instead — while ``/api/v1/session`` cheerfully advertised the anonymous
permissions.  This gate pins the corrected behaviour down.

It boots a throwaway instance (fresh database, nothing shared with the real
deployment) with the switch off and asserts:

* an anonymous caller reaches what the ``anonymous`` role grants
  (``package:read``, ``build:read``) and is *forbidden*, not *unauthorized*,
  for what it does not grant;
* a caller that still presents a valid credential is identified as usual, so an
  administrator can keep using the dashboard while the switch is off.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
os.chdir(REPO_ROOT)

# Point at a throwaway database and turn the master switch OFF *before* the app
# is imported — settings are read at import time.
_TMP_DB = Path(tempfile.mkdtemp(prefix="cpypi-authoff-")) / "authoff.db"
os.environ["API_KEYS_FILE"] = str(_TMP_DB)
os.environ["AUTH_ENABLED"] = "false"
os.environ["AUTH_USERNAME"] = "root"
os.environ["AUTH_ASSERT"] = "authoff-gate-secret"
os.environ["OAUTH2_INTROSPECT_URL"] = ""
os.environ["OAUTH2_AUTHORIZE_URL"] = ""
os.environ["ADMIN_USERS"] = '["root"]'

from app import app  # noqa: E402 - imported late so the env above applies

ROOT_AUTH = ("root", "authoff-gate-secret")

failures: list[str] = []


def check(ok: bool, label: str) -> None:
    print(("   ✅ " if ok else "   ✗ ") + label)
    if not ok:
        failures.append(label)


def main() -> int:
    anon = app.test_client()

    print("── Master switch off ───────────────────────────────────────────")
    session = anon.get("/api/v1/session").get_json()
    check(session["auth_enabled"] is False, "session reports auth_enabled=false")
    check(session["authenticated"] is False, "an anonymous caller stays anonymous")
    check(
        {"package:read", "build:read"} <= set(session["permissions"]),
        f"session advertises the anonymous role's grants "
        f"(got {session['permissions']})",
    )

    print()
    print("── Anonymously reachable == what `anonymous` holds ─────────────")
    check(
        anon.get("/simple/").status_code not in (401, 403),
        f"package:read is honoured anonymously, not 401 "
        f"(got {anon.get('/simple/').status_code})",
    )
    check(
        anon.get("/python-builds/").status_code not in (401, 403),
        f"build:read is honoured anonymously, not 401 "
        f"(got {anon.get('/python-builds/').status_code})",
    )

    print()
    print("── Everything else is 403 (forbidden), never 401 ───────────────")
    for path in ("/api/v1/admin/roles", "/npm/", "/docker/", "/debian/"):
        status = anon.get(path).status_code
        check(status == 403, f"GET {path} -> 403 (got {status})")

    print()
    print("── Credentials stay optional, not ignored ──────────────────────")
    check(
        anon.get("/api/v1/admin/roles", auth=ROOT_AUTH).status_code == 200,
        "a valid Basic credential is still identified while the switch is off",
    )

    print()
    if failures:
        print(f"❌ auth-disabled check FAILED — {len(failures)} problem(s)")
        return 1
    print("✅ auth-disabled check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
