#!/usr/bin/env python
"""Gate: the RBAC tables must actually decide access.

Run from the repository root::

    python scripts/check_rbac.py

A permission table that nothing consults is worse than no permission table: it
looks like security.  This gate boots the real app against a throwaway database
and drives the whole model end to end:

* a fresh deployment bootstraps exactly one superuser, and only once — a second
  start with ``ADMIN_USERS`` still set must not promote anybody;
* becoming an administrator is a database write, not a restart: granting a role
  takes effect on the very next request (cache invalidation);
* every permission point is *enforced*: a role without ``package:write`` gets
  403 from the upload endpoint even though it is authenticated;
* ``is_superuser`` bypasses the tables, and survives a broken/emptied grant;
* the two escalation guards hold — a non-superuser cannot set the superuser
  flag, and the last superuser cannot be demoted;
* built-in roles cannot be deleted, and a mistyped permission code shows up as
  an orphan rather than silently denying everybody.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
os.chdir(REPO_ROOT)

# Point the app at a throwaway database *before* importing it.
_TMP_DB = Path(tempfile.mkdtemp(prefix="cpypi-rbac-")) / "rbac.db"
os.environ["API_KEYS_FILE"] = str(_TMP_DB)
os.environ["AUTH_USERNAME"] = "root"
os.environ["AUTH_ASSERT"] = "rbac-gate-secret"
os.environ["OAUTH2_INTROSPECT_URL"] = ""
os.environ["OAUTH2_AUTHORIZE_URL"] = ""
# List-valued settings must be JSON — a bare `root` raises at import time.
os.environ["ADMIN_USERS"] = '["root"]'

from app import app  # noqa: E402
from auth.permissions import PACKAGE_READ, PACKAGE_WRITE  # noqa: E402
from services.authz import bootstrap  # noqa: E402

ROOT_AUTH = ("root", "rbac-gate-secret")

failures: list[str] = []


def check(ok: bool, label: str) -> None:
    print(("   ✅ " if ok else "   ✗ ") + label)
    if not ok:
        failures.append(label)


def client_as(user_id: int):
    """A test client whose session cookie identifies *user_id*."""
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["uid"] = user_id
    return c


def main() -> int:
    authz = app.extensions["authz"]
    root = app.test_client()

    # ── 1. Cold start ────────────────────────────────────────────────
    print("── Cold start ──────────────────────────────────────────────────")
    check(authz.count_superusers() == 1,
          f"exactly one superuser bootstrapped from ADMIN_USERS "
          f"(got {authz.count_superusers()})")

    r = root.get("/api/v1/session", auth=ROOT_AUTH).get_json()
    check(r["is_superuser"] is True, "the bootstrapped identity survives login")
    check(r["user"] == "root", "session reports the stable identity, not a display name")

    # Re-running bootstrap must be inert.
    before = authz.count_superusers()
    bootstrap(authz, admin_users=["someone-else"])
    check(authz.count_superusers() == before,
          "ADMIN_USERS is a one-shot seed — re-running it promotes nobody")

    # ── 2. Authorization is data, not code ───────────────────────────
    print()
    print("── Roles are database rows ─────────────────────────────────────")
    alice = authz.provision_user("oauth2", "alice", display_name="Alice")
    check(alice is not None, "an account can be provisioned before first login")

    alice_client = client_as(alice.id)
    anon = app.test_client()

    check(anon.get("/simple/").status_code == 401,
          "anonymous is refused the package index")
    check(alice_client.get("/simple/").status_code == 200,
          "auto-granted `authenticated` role reaches the package index")
    check(alice_client.get("/api/v1/admin/roles").status_code == 403,
          "a normal account is refused the access-control API")

    # Narrow the account to a read-only role — no restart, no code change.
    role = authz.create_role("reader-only", "只读")
    authz.set_role_permissions(role["id"], [PACKAGE_READ])
    authz.grant_role(alice.id, "reader-only")
    authz.revoke_role(alice.id, "authenticated")

    check(alice_client.get("/simple/").status_code == 200,
          "revoking `authenticated` and granting `reader-only` keeps read access")
    upload = alice_client.post("/", auth=None)
    check(upload.status_code == 403,
          f"package:write is enforced — upload refused with 403 (got {upload.status_code})")

    authz.set_role_permissions(role["id"], [PACKAGE_READ, PACKAGE_WRITE])
    upload = alice_client.post("/", auth=None)
    check(upload.status_code == 400,
          f"granting package:write takes effect on the next request, no restart "
          f"(400 = passed the guard, rejected the empty body; got {upload.status_code})")

    # ── 3. The superuser escape hatch ────────────────────────────────
    print()
    print("── Superuser bypass and escalation guards ──────────────────────")
    check(alice_client.get("/api/v1/admin/roles").status_code == 403,
          "adding a permission did not make the account an administrator")

    escalate = alice_client.put(f"/api/v1/admin/users/{alice.id}/superuser",
                                json={"superuser": True})
    check(escalate.status_code in (401, 403),
          f"a non-superuser cannot set the superuser flag (got {escalate.status_code})")

    accounts = root.get("/api/v1/admin/users", auth=ROOT_AUTH).get_json()
    root_id = next(u["id"] for u in accounts if u["external_id"] == "root")

    demote_last = root.put(f"/api/v1/admin/users/{root_id}/superuser",
                           auth=ROOT_AUTH, json={"superuser": False})
    check(demote_last.status_code == 400,
          f"the last superuser cannot be demoted (got {demote_last.status_code})")

    promoted = root.put(f"/api/v1/admin/users/{alice.id}/superuser",
                        auth=ROOT_AUTH, json={"superuser": True})
    check(promoted.status_code == 200, "a superuser may promote somebody else")
    check(client_as(alice.id).get("/api/v1/admin/roles").status_code == 200,
          "is_superuser bypasses the permission tables entirely")

    demoted = root.put(f"/api/v1/admin/users/{root_id}/superuser",
                       auth=ROOT_AUTH, json={"superuser": False})
    check(demoted.status_code == 200,
          "with two superusers the second may be demoted")
    check(root.get("/api/v1/admin/roles", auth=ROOT_AUTH).status_code == 403,
          "once demoted, the account falls back to its roles")

    # ── 4. Role hygiene ──────────────────────────────────────────────
    print()
    print("── Role hygiene ────────────────────────────────────────────────")
    admin_role = next(r for r in authz.list_roles() if r["code"] == "admin")
    check(authz.list_roles() and admin_role["is_builtin"],
          "the built-in admin role exists and is flagged built-in")

    deleted = client_as(alice.id).delete(f"/api/v1/admin/roles/{admin_role['id']}")
    check(deleted.status_code == 400,
          f"built-in roles cannot be deleted (got {deleted.status_code})")

    # A permission code nothing references is a likely typo.
    check(authz.orphan_permissions() == [],
          f"no permission point is held by zero roles "
          f"(got {authz.orphan_permissions()})")

    # Every point the server ships must be reachable by at least one role.
    declared = {p["code"] for p in authz.list_permissions()}
    expected = {PACKAGE_READ, PACKAGE_WRITE,
                "build:read", "build:download", "build:sha256",
                "key:list", "key:create", "key:delete", "key:stats",
                "admin:view", "admin:refresh", "admin:roles"}
    check(expected <= declared,
          f"every built-in permission point is registered "
          f"(missing: {sorted(expected - declared)})")

    print()
    if failures:
        print(f"❌ rbac check FAILED — {len(failures)} problem(s)")
        for item in failures:
            print("   " + item)
        return 1
    print("✅ rbac check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
