#!/usr/bin/env python
"""Gate: ``AUTH_ENABLED=false`` really does fall through to the `anonymous` role.

Run from the repository root::

    python scripts/check_auth_disabled.py

``AUTH_ENABLED`` is documented as the master switch, and the built-in
``anonymous`` role is documented as "applies to requests that never
authenticated (only reachable with ``AUTH_ENABLED=false``)".  For a while both
statements were false: the guards never read the setting, so an operator who
flipped it got **401 on every endpoint** instead — while ``/api/v1/session``
cheerfully advertised the anonymous permissions.  This gate pins the corrected
behaviour down.

It boots a throwaway instance (fresh database, nothing shared with the real
deployment) with the switch off and asserts:

* the ``anonymous`` role is **documentation and nothing else**: an anonymous
  caller reaches ``/docs/*`` (``doc:read``) and is *forbidden*, not
  *unauthorized*, for every other point;
* the application itself is unreachable anonymously — the SPA shell is guarded
  by ``require_auth``, not by a permission point, so flipping the master switch
  cannot open the UI;
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
        set(session["permissions"]) == {"doc:read"},
        f"the anonymous role is documentation and nothing else "
        f"(got {session['permissions']})",
    )

    print()
    print("── Documentation is reachable anonymously ──────────────────────")
    # 200 or 404 (no such document yet) both prove the guard passed; only
    # 401/403 would mean doc:read was not honoured.
    status = anon.get("/docs/python/").status_code
    check(
        status not in (401, 403),
        f"doc:read is honoured anonymously on /docs/python/ (got {status})",
    )
    status = anon.get("/api/v1/docs").status_code
    check(
        status not in (401, 403),
        f"doc:read is honoured anonymously on the docs JSON API (got {status})",
    )

    print()
    print("── Every mirror is 403 (forbidden), never 401 ──────────────────")
    for path in ("/simple/", "/python-builds/", "/node-builds/", "/npm/",
                 "/docker/", "/debian/", "/tools/", "/api/v1/admin/roles"):
        status = anon.get(path).status_code
        check(status == 403, f"GET {path} -> 403 (got {status})")

    print()
    print("── The application itself is not anonymous ─────────────────────")
    # `require_auth`, not a permission point: this must stay unreachable even
    # though an administrator can edit the `anonymous` role's grants.
    for path in ("/", "/admin", "/api-keys", "/static/dist/index.html"):
        status = anon.get(path).status_code
        check(
            status in (401, 403),
            f"GET {path} refuses an anonymous caller (got {status})",
        )

    print()
    print("── Credentials stay optional, not ignored ──────────────────────")
    check(
        anon.get("/api/v1/admin/roles", auth=ROOT_AUTH).status_code == 200,
        "a valid Basic credential is still identified while the switch is off",
    )
    # `not in (401, 403)` proves the console guard let the credential through.
    # Do not assert 200: the shell answers 503 when the Vue bundle has not been
    # built, and this gate must pass in a checkout that never ran `npm run
    # build` — it is testing authorization, not the presence of a build artifact.
    status = anon.get("/", auth=ROOT_AUTH).status_code
    check(
        status not in (401, 403),
        f"a valid Basic credential passes the console guard (got {status})",
    )

    print()
    if failures:
        print(f"❌ auth-disabled check FAILED — {len(failures)} problem(s)")
        return 1
    print("✅ auth-disabled check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
