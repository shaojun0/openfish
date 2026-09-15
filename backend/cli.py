#!/usr/bin/env python
"""Administrative CLI — roles, grants and the superuser escape hatch.

Runs without a Flask app: no watchdog indexes, no stats-refresh thread, no
network listener.  It talks to the configured database directly — the SQLite
file at ``API_KEYS_FILE``, or the PostgreSQL server named by ``DATABASE_URL`` —
through the same ``AuthzService`` the server uses, so the two can never
disagree about a rule.

Typical first run on a fresh deployment::

    python cli.py create-admin zhangsan
    # then, once that person logs in, their account is already an administrator

Or inside Docker::

    docker exec openfish-backend python /app/cli.py create-admin zhangsan

``--db`` overrides the target.  It accepts either a SQLite file path (the
historical form) or any SQLAlchemy URL, including a PostgreSQL one::

    python cli.py --db data/other.db list-users
    python cli.py --db postgresql+psycopg://openfish:pass@db:5432/openfish list-users

Every command is idempotent, so running one twice is harmless.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from config import settings  # noqa: E402
from extensions.database import Session, init_engine  # noqa: E402
from services.authz import AuthzService, bootstrap  # noqa: E402


def build_authz(database: str | None) -> AuthzService:
    """Engine + session + seeded authorization tables, with no Flask app."""
    engine = init_engine(database)
    Session.configure(bind=engine)
    authz = AuthzService(Session)
    # Seed the permission catalog and the built-in roles so the commands below
    # can refer to `admin` / `authenticated` even on a brand-new database.
    bootstrap(authz, admin_users=[])
    return authz


def _fail(message: str) -> int:
    print(f"error: {message}", file=sys.stderr)
    return 1


def _describe(authz: AuthzService, user) -> str:
    roles = ", ".join(authz.role_codes(user.id)) or "—"
    flags = " [superuser]" if user.is_superuser else ""
    return f"{user.external_id} (id={user.id}, provider={user.provider}, roles={roles}){flags}"


# ══════════════════════════════════════════════════════════════════════
#  Commands
# ══════════════════════════════════════════════════════════════════════

def cmd_create_admin(args, authz: AuthzService) -> int:
    """Create the account if needed, then make it a superuser and an admin."""
    user = authz.find_user(args.identity)
    created = user is None
    if user is None:
        user = authz.provision_user(
            args.provider, args.identity,
            display_name=args.name or args.identity,
            email=args.email,
        )
    if user is None:
        return _fail(f"could not provision an account for {args.identity!r}")

    authz.set_superuser(user.id, True)
    # Also grant the built-in admin role: `is_superuser` is the bypass, but the
    # role is what the console displays, and it keeps the account usable if the
    # bypass is ever cleared.
    authz.grant_role(user.id, "admin")

    verb = "Created" if created else "Updated"
    print(f"{verb} {_describe(authz, user)}")
    print("  is_superuser = True — bypasses every permission check.")
    print("  Clear it later with: cli.py demote " + args.identity)
    return 0


def cmd_demote(args, authz: AuthzService) -> int:
    """Clear the superuser bypass.  Roles are left untouched."""
    user = authz.find_user(args.identity)
    if user is None:
        return _fail(f"no account for {args.identity!r}")
    if not user.is_superuser:
        print(f"{args.identity} is not a superuser — nothing to do.")
        return 0
    if authz.count_superusers() <= 1:
        return _fail(
            "refusing to clear the last superuser — the server would have no "
            "administrator left. Promote somebody else first."
        )
    authz.set_superuser(user.id, False)
    print(f"Cleared superuser on {_describe(authz, user)}")
    print("  Any roles it holds are unchanged; use `revoke` to remove those.")
    return 0


def cmd_grant(args, authz: AuthzService) -> int:
    user = authz.find_user(args.identity)
    if user is None:
        return _fail(f"no account for {args.identity!r}")
    try:
        granted = authz.grant_role(user.id, args.role)
    except ValueError as exc:
        return _fail(str(exc))
    state = "granted" if granted else "already held"
    print(f"{args.role}: {state} for {_describe(authz, user)}")
    return 0


def cmd_revoke(args, authz: AuthzService) -> int:
    user = authz.find_user(args.identity)
    if user is None:
        return _fail(f"no account for {args.identity!r}")
    revoked = authz.revoke_role(user.id, args.role)
    print(f"{args.role}: {'revoked' if revoked else 'not held'} for {_describe(authz, user)}")
    return 0


def cmd_show(args, authz: AuthzService) -> int:
    user = authz.find_user(args.identity)
    if user is None:
        return _fail(f"no account for {args.identity!r}")
    print(f"account      : {user.external_id} (id={user.id})")
    print(f"display name : {user.display_name or '—'}")
    print(f"provider     : {user.provider}")
    print(f"active       : {user.is_active}")
    print(f"superuser    : {user.is_superuser}")
    print(f"roles        : {', '.join(authz.role_codes(user.id)) or '—'}")
    principal = {"user_id": user.id, "is_superuser": user.is_superuser}
    print(f"permissions  : {', '.join(sorted(authz.permission_codes(principal))) or '—'}")
    if user.last_login_at:
        print(f"last login   : {user.last_login_at.isoformat()}")
    return 0


def cmd_list_admins(args, authz: AuthzService) -> int:
    admins = [u for u in authz.list_users(limit=1000) if u["is_superuser"]]
    if not admins:
        print("No superusers. Create one with: cli.py create-admin <identity>")
        return 0
    for entry in admins:
        print(f"  id={entry['id']:<4} {entry['external_id']}"
              f"  roles={','.join(entry['roles']) or '—'}"
              f"  last_login={entry['last_login_at'] or 'never'}")
    return 0


def cmd_list_users(args, authz: AuthzService) -> int:
    users = authz.list_users(limit=args.limit)
    if not users:
        print("No accounts yet — one is created the first time somebody logs in.")
        return 0
    for entry in users:
        mark = " [superuser]" if entry["is_superuser"] else ""
        print(f"  id={entry['id']:<4} {entry['external_id']:<32}"
              f" roles={','.join(entry['roles']) or '—'}{mark}")
    return 0


def cmd_list_roles(args, authz: AuthzService) -> int:
    for role in authz.list_roles():
        tags = []
        if role["is_builtin"]:
            tags.append("builtin")
        if role["auto_grant"]:
            tags.append("auto-grant")
        if role["is_anonymous_default"]:
            tags.append("anonymous-default")
        suffix = f"  ({', '.join(tags)})" if tags else ""
        print(f"  {role['code']:<16} {role['user_count']:>3} user(s), "
              f"{len(role['permissions'])} permission(s){suffix}")
        print(f"      {', '.join(role['permissions']) or '—'}")
    return 0


def cmd_list_permissions(args, authz: AuthzService) -> int:
    for perm in authz.list_permissions():
        warn = "  ⚠ held by no role" if perm["role_count"] == 0 else ""
        print(f"  {perm['code']:<24} {perm['role_count']} role(s){warn}")
    orphans = authz.orphan_permissions()
    if orphans:
        print()
        print(f"⚠ {len(orphans)} permission point(s) no role holds: "
              f"{', '.join(orphans)}")
        print("  Usually a mistyped @require_permission code — nobody but a")
        print("  superuser can reach the route that checks it.")
    return 0


# ══════════════════════════════════════════════════════════════════════

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cli.py",
        description="Administrative commands for cpypiserver.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  cli.py create-admin zhangsan\n"
            "  cli.py grant zhangsan publisher\n"
            "  cli.py show zhangsan\n"
            "  cli.py list-roles\n"
        ),
    )
    parser.add_argument(
        "--db", default=None,
        help=(
            "What to operate on: a SQLite file path, or a SQLAlchemy URL such "
            "as postgresql+psycopg://user:pass@host:5432/openfish.  Defaults to "
            "DATABASE_URL, or the SQLite file "
            f"{settings.storage.api_keys_file} when DATABASE_URL is unset."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("create-admin", aliases=["promote"],
                       help="Make an account a superuser (creates it if needed).")
    p.add_argument("identity", help="Stable account id (e.g. the corporate login).")
    p.add_argument("--provider", default="local",
                   help="Recorded provider for a newly created account (default: local).")
    p.add_argument("--name", default=None, help="Display name.")
    p.add_argument("--email", default=None, help="E-mail address.")
    p.set_defaults(func=cmd_create_admin)

    p = sub.add_parser("demote", help="Clear the superuser bypass (keeps roles).")
    p.add_argument("identity")
    p.set_defaults(func=cmd_demote)

    p = sub.add_parser("grant", help="Grant a role to an account.")
    p.add_argument("identity")
    p.add_argument("role")
    p.set_defaults(func=cmd_grant)

    p = sub.add_parser("revoke", help="Revoke a role from an account.")
    p.add_argument("identity")
    p.add_argument("role")
    p.set_defaults(func=cmd_revoke)

    p = sub.add_parser("show", help="Show one account's roles and permissions.")
    p.add_argument("identity")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("list-admins", help="List superusers.")
    p.set_defaults(func=cmd_list_admins)

    p = sub.add_parser("list-users", help="List accounts.")
    p.add_argument("--limit", type=int, default=200)
    p.set_defaults(func=cmd_list_users)

    p = sub.add_parser("list-roles", help="List roles and their permissions.")
    p.set_defaults(func=cmd_list_roles)

    p = sub.add_parser("list-permissions", help="List permission points.")
    p.set_defaults(func=cmd_list_permissions)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    authz = build_authz(args.db)
    try:
        return args.func(args, authz)
    except ValueError as exc:
        return _fail(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
