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
    python cli.py --db 'postgresql+psycopg://$POSTGRES_USER:$POSTGRES_PASSWORD@db:5432/openfish' list-users

Every command is idempotent, so running one twice is harmless.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

REPO_ROOT = Path(__file__).resolve().parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from config import settings  # noqa: E402
from extensions.database import Session, init_engine  # noqa: E402
from services.authz import AuthzService, bootstrap  # noqa: E402

if TYPE_CHECKING:  # noqa: E402
    from services.repo_runner import RepoRunnerService


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
        return _fail(f"could not provision an account for {args.identity}")

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
        return _fail(f"no account for {args.identity}")
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
        return _fail(f"no account for {args.identity}")
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
        return _fail(f"no account for {args.identity}")
    revoked = authz.revoke_role(user.id, args.role)
    print(f"{args.role}: {'revoked' if revoked else 'not held'} for {_describe(authz, user)}")
    return 0


def cmd_show(args, authz: AuthzService) -> int:
    user = authz.find_user(args.identity)
    if user is None:
        return _fail(f"no account for {args.identity}")
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


def _revoke_git_identity(user_id: int, *, delete_forgejo_user: bool = False) -> str:
    """Revoke the Forgejo credential minted for *user_id*.

    Returns ``"revoked"`` (the remote token is gone), ``"none"`` (this account
    never had one) or ``"failed"``.  A failure is **not** reported as success: the
    Forgejo token has no TTL, so a swallowed error would leave a deactivated
    account able to push.  The account is already deactivated either way, so the
    warning goes to stderr rather than aborting the command.

    The import is local so ``cli.py --help`` stays cheap.  Git-identity
    revocation needs no ``GIT_IDENTITY_KEY`` (it deletes by the deterministic
    username), so a missing key does not block it.
    """
    from services.git_identity import GitIdentityError, GitIdentityService

    try:
        revoked = GitIdentityService(Session).revoke(
            user_id, delete_forgejo_user=delete_forgejo_user,
        )
    except GitIdentityError as exc:
        print(f"warning: git identity not revoked: {exc}", file=sys.stderr)
        return "failed"
    return "revoked" if revoked else "none"


def cmd_disable_user(args, authz: AuthzService) -> int:
    """Deactivate an account and cut off its git credentials.

    ``is_active=False`` is the switch every login path checks; because the git
    push credential is a Forgejo token minted *on the user's behalf*, it has to
    be revoked here too — otherwise a deactivated account keeps pushing.
    """
    user = authz.find_user(args.identity)
    if user is None:
        return _fail(f"no account for {args.identity}")
    if not user.is_active:
        print(f"{args.identity} is already inactive — nothing to do.")
        return 0
    authz.set_active(user.id, False)
    git_status = _revoke_git_identity(
        user.id, delete_forgejo_user=args.delete_forgejo_user,
    )
    print(f"Deactivated {_describe(authz, user)}")
    print("  Every login path checks is_active, so the account can no longer authenticate.")
    if git_status == "revoked":
        suffix = " (Forgejo account deleted)" if args.delete_forgejo_user else ""
        print(f"  git identity: revoked{suffix}")
    elif git_status == "none":
        print("  git identity: none on record")
    else:
        print(
            "  git identity: NOT revoked — the Forgejo token is still valid; "
            "retry once Forgejo is reachable",
            file=sys.stderr,
        )
        return 1
    print("  Re-enable with: cli.py enable-user " + args.identity)
    return 0


def cmd_enable_user(args, authz: AuthzService) -> int:
    user = authz.find_user(args.identity)
    if user is None:
        return _fail(f"no account for {args.identity}")
    if user.is_active:
        print(f"{args.identity} is already active — nothing to do.")
        return 0
    authz.set_active(user.id, True)
    print(f"Reactivated {_describe(authz, user)}")
    print("  The next git-credential request re-provisions a Forgejo token.")
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


# ── Per-repo runners ─────────────────────────────────────────────────
# Operator surface for `models.agent_hub.RepoRunner`, one row per repository.
# Each command resolves the slug through the same `Session` the auth commands
# use, and none of them ever prints the sealed credential — only
# `has_credential`, which `RepoRunner.to_dict()` exposes in its place.

def _runner_service() -> RepoRunnerService:
    """A :class:`RepoRunnerService` bound to the CLI's configured session."""
    from services.repo_runner import RepoRunnerService

    return RepoRunnerService(Session)


def _repo_id_for_slug(slug: str) -> int:
    """Resolve ``<owner>/<name>`` to ``repos.id``; an unknown slug raises."""
    from models.agent_hub import Repo
    from services.repo_runner import RepoRunnerError

    session = Session()
    try:
        repo_id = session.query(Repo.id).filter(Repo.slug == slug).scalar()
    finally:
        session.close()
    if repo_id is None:
        raise RepoRunnerError(f"未知的仓库 slug：{slug}")
    return int(repo_id)


def _repo_slugs(repo_ids: list[int]) -> dict[int, str]:
    """Map ``repos.id`` → slug for the ids in *repo_ids*."""
    from models.agent_hub import Repo

    if not repo_ids:
        return {}
    session = Session()
    try:
        rows = session.query(Repo.id, Repo.slug).filter(Repo.id.in_(repo_ids)).all()
    finally:
        session.close()
    return {int(repo_id): str(slug) for repo_id, slug in rows}


def _print_runner_config(row, slug: str) -> None:
    """``key : value`` lines for one runner, never the sealed secret."""
    from services.repo_runner import safe_workspace_subdir

    data = row.to_dict()
    credential = data["credential_kind"]
    if data["credential_username"]:
        credential = f"{credential} (username={data['credential_username']})"
    concurrency = data["max_concurrency"]
    lines = [
        ("slug", slug),
        ("runner id", str(data["id"])),
        ("repo id", str(data["repo_id"])),
        ("name", data["name"] or "—"),
        ("enabled", "yes" if data["enabled"] else "no"),
        ("max concurrency", f"{concurrency} (inherit)" if not concurrency else str(concurrency)),
        (
            "workspace subdir",
            safe_workspace_subdir(data["workspace_subdir"], runner_id=data["id"]),
        ),
        ("egress policy", data["egress_policy"]),
        ("egress allowlist", data["egress_allowlist"] or "—"),
        ("credential kind", credential),
        ("has credential", "yes" if data["has_credential"] else "no"),
        ("credential expires", data["credential_expires_at"] or "never"),
        ("credential rotated", data["credential_rotated_at"] or "never"),
        ("last task", data["last_task_at"] or "never"),
        ("created", data["created_at"] or "—"),
        ("updated", data["updated_at"] or "—"),
    ]
    width = max(len(label) for label, _ in lines)
    for label, value in lines:
        print(f"{label:<{width}} : {value}")


def cmd_runner_list(args) -> int:
    """One line per runner: slug, enabled, credential source, concurrency, egress."""
    runners = _runner_service().list()
    if not runners:
        print("No repo runners configured. Create one with: cli.py runner enable <slug>")
        return 0
    slugs = _repo_slugs([row.repo_id for row in runners])
    for row in runners:
        slug = slugs.get(row.repo_id, f"repo-{row.repo_id}")
        state = "enabled" if row.enabled else "disabled"
        concurrency = row.max_concurrency or "inherit"
        egress = row.egress_policy
        if row.egress_allowlist:
            egress = f"{egress}:{row.egress_allowlist}"
        print(
            f"  {slug:<40} {state:<8} cred={row.credential_kind:<6}"
            f" concurrency={concurrency:<7} egress={egress}"
        )
    return 0


def cmd_runner_show(args) -> int:
    """Full configuration for one runner — never the secret itself."""
    repo_id = _repo_id_for_slug(args.slug)
    row = _runner_service().get(repo_id)
    if row is None:
        return _fail(
            f"no runner configured for {args.slug}; "
            f"create it with: cli.py runner enable {args.slug}"
        )
    _print_runner_config(row, args.slug)
    return 0


def _runner_set_enabled(args, enabled: bool) -> int:
    repo_id = _repo_id_for_slug(args.slug)
    service = _runner_service()
    row = service.get(repo_id)
    if row is not None and bool(row.enabled) == enabled:
        state = "enabled" if enabled else "disabled"
        print(f"Runner for {args.slug} is already {state} — nothing to do.")
        return 0
    row = service.update(repo_id, enabled=enabled)
    verb = "Enabled" if enabled else "Disabled"
    print(f"{verb} runner for {args.slug} (id={row.id}).")
    return 0


def cmd_runner_enable(args) -> int:
    return _runner_set_enabled(args, True)


def cmd_runner_disable(args) -> int:
    return _runner_set_enabled(args, False)


def cmd_runner_set(args) -> int:
    """Update limits, egress policy or workspace of one runner."""
    if (
        args.max_concurrency is None
        and args.egress_policy is None
        and args.egress_allowlist is None
        and args.workspace_subdir is None
    ):
        return _fail(
            "runner set needs at least one of --max-concurrency, --egress-policy, "
            "--egress-allowlist or --workspace-subdir"
        )
    repo_id = _repo_id_for_slug(args.slug)
    row = _runner_service().update(
        repo_id,
        max_concurrency=args.max_concurrency,
        egress_policy=args.egress_policy,
        egress_allowlist=args.egress_allowlist,
        workspace_subdir=args.workspace_subdir,
    )
    print(f"Updated runner for {args.slug} (id={row.id}).")
    _print_runner_config(row, args.slug)
    return 0


def cmd_runner_set_credential(args) -> int:
    """Seal a repo-scoped token read from the environment or a hidden prompt.

    The token is deliberately not an argv value — a flag would land in the
    shell history and in ``ps``.  ``--token-env`` names the variable to read;
    without it the operator is prompted through :func:`getpass.getpass`.
    """
    repo_id = _repo_id_for_slug(args.slug)
    if args.token_env:
        token = (os.environ.get(args.token_env) or "").strip()
        if not token:
            return _fail(
                f"environment variable {args.token_env} is unset or empty — "
                "nothing stored"
            )
    else:
        try:
            token = getpass.getpass(f"runner token for {args.slug}: ").strip()
        except (EOFError, KeyboardInterrupt):
            print(file=sys.stderr)
            return _fail("no token entered — nothing stored")
        if not token:
            return _fail("empty token — nothing stored")
    row = _runner_service().set_credential(
        repo_id, token=token, username=args.username or "",
    )
    print(
        f"Stored a repo-scoped runner credential for {args.slug} "
        f"(id={row.id}, username={row.credential_username or 'x-access-token'})."
    )
    print(f"  Drop it again with: cli.py runner clear-credential {args.slug}")
    return 0


def cmd_runner_clear_credential(args) -> int:
    """Drop the repo-scoped token and fall back to the shared runner token."""
    repo_id = _repo_id_for_slug(args.slug)
    service = _runner_service()
    row = service.get(repo_id)
    if row is None or not row.to_dict()["has_credential"]:
        print(f"No repo-scoped credential for {args.slug} — nothing to clear.")
        return 0
    row = service.clear_credential(repo_id)
    print(f"Cleared the repo-scoped credential for {args.slug} (id={row.id}).")
    print("  The runner falls back to the shared FORGEJO_RUNNER_TOKEN.")
    return 0


# ── Model routes ─────────────────────────────────────────────────────
# A route's upstream key is sealed under MODEL_ROUTE_KEY before it is stored, so
# the deployment step that injects a key cannot be "write it into the table" —
# and cannot be the environment either, since that is what made one row's
# behaviour depend on the deploying process (see models/model_route.py).  These
# commands are the operator's path: the same sealing the panel uses, from a
# shell, with the secret read from a variable or a hidden prompt rather than
# from argv.
#
# None of them ever prints a key: `list` shows the non-secret
# `api_key_source` / `api_key_hint` the API publishes, and nothing more.

def _model_route_session():
    """A session bound to the CLI's configured database."""
    return Session()


def _print_route_key(row: dict) -> None:
    """One route's key status, never its value."""
    source = str(row.get("api_key_source") or "none")
    hint = row.get("api_key_hint") or ""
    names = {
        "stored": "已加密存储",
        "plaintext": "⚠ 仍是明文（需 seal）",
        "unreadable": "⚠ 无法解密（MODEL_ROUTE_KEY 缺失或不对）",
        "none": "未配置",
    }
    detail = f"{names.get(source, source)}"
    if hint:
        detail += f" {hint}"
    print(f"  {row.get('name')}: {detail}")


def cmd_model_route_list(args) -> int:
    """List the routing table and how each route is authenticated."""
    from services import model_routes

    session = _model_route_session()
    try:
        routes = model_routes.load(session)["routes"]
    finally:
        session.close()
    if not routes:
        print("模型路由表为空。用 /models 面板或 config/model_routes.seed.sql 添加路由。")
        return 0
    print(f"{len(routes)} model route(s):")
    for row in routes:
        _print_route_key(row)
    if any(row.get("api_key_source") == "plaintext" for row in routes):
        print(f"  → 用 `model-route seal` 重新封存明文密钥（需要 {_key_env()}）。")
    return 0


def cmd_model_route_set_key(args) -> int:
    """Seal an upstream key read from the environment or a hidden prompt.

    The key is deliberately not an argv value — a flag would land in the shell
    history and in ``ps``.  ``--key-env`` names the variable to read; without it
    the operator is prompted through :func:`getpass.getpass`.
    """
    from services import model_routes
    from services.sealing import SealingKeyMissing

    if args.key_env:
        key = (os.environ.get(args.key_env) or "").strip()
        if not key:
            return _fail(
                f"environment variable {args.key_env} is unset or empty — nothing stored"
            )
    else:
        try:
            key = getpass.getpass(f"upstream api key for {args.name}: ").strip()
        except (EOFError, KeyboardInterrupt):
            print(file=sys.stderr)
            return _fail("no key entered — nothing stored")
        if not key:
            return _fail("empty key — nothing stored")
    session = _model_route_session()
    try:
        row = model_routes.update(session, args.name, {"api_key": key})
    except model_routes.RouteNotFoundError:
        return _fail(f"未知的模型路由：{args.name}")
    except SealingKeyMissing as exc:
        return _fail(str(exc))
    except ValueError as exc:
        return _fail(str(exc))
    finally:
        session.close()
    print(f"已封存 {args.name} 的上游密钥（{_key_env()}）。")
    _print_route_key(model_routes.public_route(row))
    print("  → 用 `model-route clear-key` 删除，或用 /models 面板重新检测连通性。")
    return 0


def cmd_model_route_clear_key(args) -> int:
    """Drop a route's stored key, so it authenticates with nothing."""
    from services import model_routes

    session = _model_route_session()
    try:
        row = model_routes.update(session, args.name, {"api_key": ""})
    except model_routes.RouteNotFoundError:
        return _fail(f"未知的模型路由：{args.name}")
    except ValueError as exc:
        return _fail(str(exc))
    finally:
        session.close()
    print(f"已清除 {args.name} 的上游密钥。")
    _print_route_key(model_routes.public_route(row))
    return 0


def cmd_model_route_seal(args) -> int:
    """Re-seal every route whose key predates sealing — the data migration.

    Idempotent, and the only thing that rewrites an existing key.  Boot reports
    how many rows need this (see ``models.model_route_migrate``) but never does
    it, because it needs the master key and writes a secret.
    """
    from services import model_routes
    from services.sealing import SealingKeyMissing

    session = _model_route_session()
    try:
        resealed = model_routes.migrate_plaintext_keys(session)
    except SealingKeyMissing as exc:
        return _fail(str(exc))
    finally:
        session.close()
    if not resealed:
        print("没有明文密钥需要处理。")
        return 0
    print(f"已重新封存 {len(resealed)} 条路由的明文密钥：")
    for name in resealed:
        print(f"  {name}")
    print("  → 数据库/备份现在不再包含可直接使用的上游凭证。")
    return 0


def _key_env() -> str:
    """The master-key variable name, derived from the config model that owns it."""
    from config.keys import KeysConfig

    return KeysConfig.env_name("model_route_key")


# ══════════════════════════════════════════════════════════════════════

def build_parser() -> argparse.ArgumentParser:
    from services import mirror_catalog

    parser = argparse.ArgumentParser(
        prog="cli.py",
        description="Administrative commands for cpypiserver.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  cli.py create-admin zhangsan\n"
            "  cli.py grant zhangsan publisher\n"
            "  cli.py show zhangsan\n"
            "  cli.py disable-user zhangsan\n"
            "  cli.py list-roles\n"
            "  cli.py runner list\n"
        ),
    )
    parser.add_argument(
        "--db", default=None,
        help=(
            "What to operate on: a SQLite file path, or a SQLAlchemy URL such "
            "as postgresql+psycopg://$POSTGRES_USER:$POSTGRES_PASSWORD@host:5432/openfish.  "
            "Defaults to "
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

    p = sub.add_parser(
        "disable-user",
        help="Deactivate an account and revoke its Forgejo git credential.",
    )
    p.add_argument("identity")
    p.add_argument(
        "--delete-forgejo-user", action="store_true",
        help="Also delete the account's dedicated Forgejo account.",
    )
    p.set_defaults(func=cmd_disable_user)

    p = sub.add_parser("enable-user", help="Reactivate a deactivated account.")
    p.add_argument("identity")
    p.set_defaults(func=cmd_enable_user)

    p = sub.add_parser("list-admins", help="List superusers.")
    p.set_defaults(func=cmd_list_admins)

    p = sub.add_parser("list-users", help="List accounts.")
    p.add_argument("--limit", type=int, default=200)
    p.set_defaults(func=cmd_list_users)

    p = sub.add_parser("list-roles", help="List roles and their permissions.")
    p.set_defaults(func=cmd_list_roles)

    p = sub.add_parser("list-permissions", help="List permission points.")
    p.set_defaults(func=cmd_list_permissions)

    p = sub.add_parser("runner", help="Manage the per-repository agent runners.")
    runner = p.add_subparsers(dest="runner_command", required=True)

    q = runner.add_parser("list", help="List configured repo runners.")
    q.set_defaults(func=cmd_runner_list)

    q = runner.add_parser("show", help="Show one runner's full configuration.")
    q.add_argument("slug", help="Repository slug, e.g. owner/name.")
    q.set_defaults(func=cmd_runner_show)

    q = runner.add_parser("enable", help="Enable a repo runner.")
    q.add_argument("slug")
    q.set_defaults(func=cmd_runner_enable)

    q = runner.add_parser("disable", help="Disable a repo runner.")
    q.add_argument("slug")
    q.set_defaults(func=cmd_runner_disable)

    q = runner.add_parser("set", help="Update limits, egress policy or workspace.")
    q.add_argument("slug")
    q.add_argument(
        "--max-concurrency", type=int, default=None,
        help="Max in-flight tasks for this repo; 0 = inherit the platform default.",
    )
    q.add_argument(
        "--egress-policy", default=None,
        help="inherit | internal | allowlist (validated by the service).",
    )
    q.add_argument(
        "--egress-allowlist", default=None,
        help="Comma-separated hosts; an empty string clears the allowlist.",
    )
    q.add_argument(
        "--workspace-subdir", default=None,
        help="Relative path under AGENT_WORK_ROOT; an empty string restores runners/<id>.",
    )
    q.set_defaults(func=cmd_runner_set)

    q = runner.add_parser(
        "set-credential",
        help="Seal a repo-scoped runner token (never passed on the command line).",
    )
    q.add_argument("slug")
    q.add_argument(
        "--token-env", default=None, metavar="VAR",
        help="Environment variable holding the token; omit to be prompted.",
    )
    q.add_argument("--username", default=None, help="Username paired with the token.")
    q.set_defaults(func=cmd_runner_set_credential)

    q = runner.add_parser(
        "clear-credential",
        help="Drop the repo-scoped token and fall back to the shared one.",
    )
    q.add_argument("slug")
    q.set_defaults(func=cmd_runner_clear_credential)

    p = sub.add_parser(
        "model-route",
        help="Inspect and set the sealed upstream keys of the routing table.",
    )
    routes = p.add_subparsers(dest="model_route_command", required=True)

    q = routes.add_parser(
        "list",
        help="Show every route and how it is authenticated (never the key).",
    )
    q.set_defaults(func=cmd_model_route_list)

    q = routes.add_parser(
        "set-key",
        help=f"Seal a route's upstream key (needs {_key_env()}; never passed on the command line).",
    )
    q.add_argument("name")
    q.add_argument(
        "--key-env", default=None, metavar="VAR",
        help="Environment variable holding the key; omit to be prompted.",
    )
    q.set_defaults(func=cmd_model_route_set_key)

    q = routes.add_parser("clear-key", help="Drop a route's stored upstream key.")
    q.add_argument("name")
    q.set_defaults(func=cmd_model_route_clear_key)

    q = routes.add_parser(
        "seal",
        help="Re-seal any key still stored in plaintext (idempotent).",
    )
    q.set_defaults(func=cmd_model_route_seal)

    # ── Documentation: the folder tree as a migration bridge ────────
    p = sub.add_parser(
        "docs",
        help="Import, export and inspect the documentation catalog.",
    )
    docs_cmd = p.add_subparsers(dest="docs_command", required=True)

    q = docs_cmd.add_parser(
        "import",
        help="Read a DOCS_DIR-shaped folder tree into the database (idempotent).",
    )
    q.add_argument(
        "--from", dest="source", default=None, metavar="DIR",
        help=f"Tree to read; defaults to DOCS_DIR ({settings.hub.docs_dir}).",
    )
    q.add_argument(
        "--dry-run", action="store_true",
        help="Report what would change without writing anything.",
    )
    q.set_defaults(func=cmd_docs_import)

    q = docs_cmd.add_parser(
        "export",
        help="Write every document out as <ecosystem>/<slug>/document.md.",
    )
    q.add_argument("--to", dest="target", required=True, metavar="DIR")
    q.set_defaults(func=cmd_docs_export)

    q = docs_cmd.add_parser("history", help="List a document's stored revisions.")
    q.add_argument("ecosystem")
    q.add_argument("doc_id")
    q.set_defaults(func=cmd_docs_history)

    q = docs_cmd.add_parser("show", help="Print one revision's Markdown (default: current).")
    q.add_argument("ecosystem")
    q.add_argument("doc_id")
    q.add_argument("--revision", type=int, default=None, help="Revision to print.")
    q.add_argument("--out", default=None, metavar="FILE", help="Write to FILE instead of stdout.")
    q.set_defaults(func=cmd_docs_show)

    # ── Tools catalog: the same bridge, one directory up ────────────
    p = sub.add_parser(
        "tools",
        help="Import and export the tools catalog (rows + store objects).",
    )
    tools_cmd = p.add_subparsers(dest="tools_command", required=True)

    q = tools_cmd.add_parser(
        "import",
        help="Read a TOOLS_DIR-shaped tree and its catalog.json into the catalog (idempotent).",
    )
    q.add_argument(
        "--from", dest="source", default=None, metavar="DIR",
        help=f"Tree to read; defaults to TOOLS_DIR ({settings.hub.tools_dir}).",
    )
    q.add_argument(
        "--dry-run", action="store_true",
        help="Report what would change without writing anything.",
    )
    q.add_argument(
        "--prune", action="store_true",
        help="Also remove entries that no longer exist in the tree.",
    )
    q.set_defaults(func=cmd_tools_import)

    q = tools_cmd.add_parser(
        "export",
        help="Write every entry back out to its path, plus a catalog.json overlay.",
    )
    q.add_argument("--to", dest="target", required=True, metavar="DIR")
    q.set_defaults(func=cmd_tools_export)

    # ── Catalogs: the shipped defaults a fresh database is initialized with ──
    p = sub.add_parser(
        "catalogs",
        help="Inspect or re-install the default catalogs a fresh database gets.",
    )
    catalogs_cmd = p.add_subparsers(dest="catalogs_command", required=True)

    q = catalogs_cmd.add_parser(
        "seed",
        help="Install the shipped documentation and tools defaults (idempotent).",
    )
    q.add_argument(
        "--force", action="store_true",
        help="Apply the seed even if it was applied before or the catalog has rows.",
    )
    q.set_defaults(func=cmd_catalogs_seed)

    q = catalogs_cmd.add_parser(
        "import",
        help="Read a mirror's catalog.json into the catalog_entries overlay rows.",
    )
    q.add_argument("--namespace", required=True, choices=list(mirror_catalog.MIRRORS))
    q.add_argument("--from", dest="source", metavar="FILE", default=None)
    q.add_argument("--prune", action="store_true", help="Delete rows the file does not name.")
    q.add_argument("--dry-run", action="store_true")
    q.set_defaults(func=cmd_catalogs_import)

    q = catalogs_cmd.add_parser(
        "export",
        help="Write a mirror's overlay rows back out as catalog.json.",
    )
    q.add_argument("--namespace", required=True, choices=list(mirror_catalog.MIRRORS))
    q.add_argument("--to", dest="target", metavar="FILE", default=None)
    q.set_defaults(func=cmd_catalogs_export)

    return parser



# ── Documentation catalog ───────────────────────────────────────────
#
# The docs feature lives in the database (see `models.docs`), but a folder tree
# is still the friendliest way to move a set of documents into or out of a
# disconnected deployment — and it is the layout this feature used to *be*, so
# an existing `DOCS_DIR` imports as-is.

def cmd_docs_import(args) -> int:
    """Read a folder tree into the documentation tables."""
    from services import docs

    source = args.source or settings.hub.docs_dir
    session = Session()
    try:
        report = docs.import_tree(session, source, dry_run=args.dry_run)
    finally:
        session.close()
    for line in report.lines():
        print(line)
    if args.dry_run:
        print("（--dry-run：没有写入任何东西）")
    return 0 if not report.errors else 1


def cmd_docs_export(args) -> int:
    """Write every document back out as a folder tree."""
    from services import docs

    session = Session()
    try:
        count = docs.export_tree(session, args.target)
    finally:
        session.close()
    print(f"已导出 {count} 篇文档到 {args.target}")
    return 0


def cmd_docs_history(args) -> int:
    """Show one document's stored revisions, newest first."""
    from services import docs

    session = Session()
    try:
        rows = docs.history(session, args.ecosystem, args.doc_id)
    finally:
        session.close()
    if not rows:
        print("没有可显示的修订。")
        return 0
    for row in rows:
        marker = "*" if row["current"] else " "
        print(
            f"{marker} r{row['revision']:<4} {row['created']}  {row['size_human']:>8}"
            f"  {row['created_by'] or '—'}  {row['title']}"
        )
    return 0


def cmd_catalogs_seed(args) -> int:
    """Install (or re-install) the shipped default catalogs.

    The same call runs automatically from ``init_engine`` on a fresh database;
    this command is how an operator inspects that outcome or asks for the
    defaults back after deleting them.
    """
    from services.catalog_seed import ensure_seed

    engine = init_engine(args.db_url())
    outcomes = ensure_seed(engine, force=args.force)
    names = {
        "seeded": "已写入默认内容",
        "already": "此前已初始化，跳过",
        "not-empty": "目录里已有内容，跳过并记录",
        "absent": "没有对应的种子文件",
    }
    for outcome in outcomes:
        detail = f"（{outcome.objects} 个对象 / {outcome.statements} 条语句）" if outcome.action == "seeded" else ""
        print(f"  {outcome.namespace}: {names.get(outcome.action, outcome.action)}{detail}")
    return 0


def cmd_catalogs_import(args) -> int:
    """Read a mirror's ``catalog.json`` into rows (the overlay is a table now)."""
    from services import mirror_catalog

    namespace = args.namespace
    source = Path(args.source) if args.source else mirror_catalog.default_path(namespace)
    session = Session()
    try:
        report = mirror_catalog.import_file(
            session, namespace, source, prune=args.prune, dry_run=args.dry_run
        )
    finally:
        session.close()
    for line in report.lines():
        print(line)
    if args.dry_run:
        print("（--dry-run：没有写入任何东西）")
    return 0


def cmd_catalogs_export(args) -> int:
    """Write a mirror's overlay rows back out as ``catalog.json``."""
    from services import mirror_catalog

    namespace = args.namespace
    target = Path(args.target) if args.target else mirror_catalog.default_path(namespace)
    session = Session()
    try:
        report = mirror_catalog.export_file(session, namespace, target)
    finally:
        session.close()
    for line in report.lines():
        print(line)
    return 0


def cmd_tools_import(args) -> int:
    """Read a TOOLS_DIR-shaped tree (and its catalog.json) into the catalog."""
    from services import tool_catalog

    source = args.source or settings.hub.tools_dir
    session = Session()
    try:
        report = tool_catalog.import_tree(
            session, source, dry_run=args.dry_run, prune=args.prune
        )
    finally:
        session.close()
    for line in report.lines():
        print(line)
    if args.dry_run:
        print("（--dry-run：没有写入任何东西）")
    return 0 if not report.errors else 1


def cmd_tools_export(args) -> int:
    """Write the tools catalog back out as a folder tree plus catalog.json."""
    from services import tool_catalog

    session = Session()
    try:
        count = tool_catalog.export_tree(session, args.target)
    finally:
        session.close()
    print(f"已导出 {count} 个条目到 {args.target}")
    return 0


def cmd_docs_show(args) -> int:
    """Print (or write out) one revision's Markdown."""
    from services import docs

    session = Session()
    try:
        row, revision, stream = docs.open_body(
            session, args.ecosystem, args.doc_id, revision=args.revision
        )
        body = stream.read()
        title = row.title
    finally:
        session.close()
    if args.out:
        Path(args.out).write_bytes(body)
        print(f"已写出 r{revision}（{title}）到 {args.out}")
        return 0
    sys.stdout.write(body.decode("utf-8"))
    return 0

def _run_runner(args) -> int:
    """Dispatch a ``runner`` subcommand, mapping domain errors onto exit 1."""
    from services.repo_runner import RepoRunnerError

    try:
        return args.func(args)
    except RepoRunnerError as exc:
        return _fail(str(exc))


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    authz = build_authz(args.db)
    if getattr(args, "runner_command", None):
        return _run_runner(args)
    if getattr(args, "model_route_command", None):
        # These map their own domain errors onto exit 1 (a missing sealing key,
        # an unknown route) and take no authorization service.
        return args.func(args)
    if getattr(args, "catalogs_command", None):
        # The seed runs inside `build_authz`'s `init_engine` already; this branch
        # only exists for the command that reports or forces it.
        args.db_url = lambda: args.db
        try:
            return args.func(args)
        except (OSError, ValueError) as exc:
            return _fail(str(exc))
    if getattr(args, "docs_command", None) or getattr(args, "tools_command", None):
        # Same posture: an unreadable tree or an unknown document is a message
        # on stderr and exit 1, not a traceback.
        try:
            return args.func(args)
        except (OSError, ValueError) as exc:
            return _fail(str(exc))
    try:
        return args.func(args, authz)
    except ValueError as exc:
        return _fail(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
