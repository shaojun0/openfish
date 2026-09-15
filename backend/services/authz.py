"""Authorization service — the one place that answers "may this principal?".

Everything about *who may do what* passes through :class:`AuthzService`:

    principal (dict from ``g.auth_user``, or None for anonymous)
        -> roles        (user_roles)
        -> permissions  (role_permissions -> permissions)

Design notes
------------
* The service owns a tiny versioned cache.  Any mutation bumps the version and
  drops the cache, so a freshly granted role takes effect on the next request
  without a restart or a TTL wait.  No external cache dependency, which also
  lets the CLI use this class without a Flask app.
* ``is_superuser`` short-circuits **before** any table lookup.  If the role
  tables are emptied or corrupted, a superuser can still get in and repair
  them.  This is the whole point of the column.
* Seeding is idempotent and additive: existing rows are never overwritten, so
  an administrator's edits survive every restart.  The one exception is the
  built-in ``admin`` role, which is documented to always hold every point.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Iterable

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import scoped_session

from auth import permissions as P
from models.base import utcnow
from models.rbac import (
    Permission,
    Role,
    RolePermission,
    SeedMigration,
    UserRole,
)
from models.user import User

logger = logging.getLogger("cpypiserver.authz")

#: Permission points the ``anonymous`` role starts with.  Anonymous access is
#: reachable only when ``AUTH_ENABLED=false`` (or, in a deployment that turns
#: the switch off, by any caller that presents no credential).
#:
#: Deliberately minimal: **documentation only.**  An anonymous caller may read
#: the ecosystem handbook on ``/docs/*`` and nothing else — no package index, no
#: mirror, no catalogue, and not even the SPA shell, which is guarded by
#: ``require_auth`` rather than by a point so that flipping the master switch
#: cannot accidentally open the application.  Add a point here only if the
#: deployment is genuinely meant to serve it to unauthenticated strangers.
_ANONYMOUS_SEED = (P.DOC_READ,)

#: Permission points the ``authenticated`` role starts with.  This is *seed
#: data for one built-in role row*, not a role -> permission mapping in code:
#: once created, the row is the authority and admins may edit it freely.
#:
#: Rule of thumb, and the reason this tuple is exhaustive rather than
#: hand-picked: **every read/download point for a mirror ecosystem belongs
#: here; only write/admin points stay with the ``admin`` role.**  A point that
#: gates a mirror a signed-in developer is expected to consume (``uv``, ``nvm``,
#: ``npm``, ``docker``, ``apt``, the tools catalog) must be seeded, or the
#: feature is silently admin-only.  ``nodebuild:*`` was missed for exactly that
#: reason — see ``scripts/check_permission_catalog.py``, which now refuses a new
#: built-in point that nobody has classified.
_AUTHENTICATED_SEED = (
    P.PACKAGE_READ, P.PACKAGE_WRITE,
    P.BUILD_READ, P.BUILD_DOWNLOAD, P.BUILD_SHA256,
    P.NODE_BUILD_READ, P.NODE_BUILD_DOWNLOAD, P.NODE_BUILD_SHA256,
    P.TOOL_READ, P.TOOL_DOWNLOAD, P.NPM_READ, P.NPM_DOWNLOAD, P.MODEL_READ,
    P.MODEL_RESOLVE,
    P.DOCKER_READ, P.DOCKER_DOWNLOAD, P.DEBIAN_READ, P.DEBIAN_DOWNLOAD,
    P.DOC_READ,
    P.APP_READ,
    P.KEY_LIST, P.KEY_CREATE, P.KEY_DELETE, P.KEY_STATS,
)

#: One-time seed top-ups, applied to built-in roles that already exist.
#:
#: ``sync_builtin_roles`` seeds a role only when its row is created, so a point
#: added to a seed tuple *after* that never reaches a running deployment.  Each
#: entry below is the delta that closes that gap for one release; it is applied
#: exactly once (recorded in ``seed_migrations``), and it only ever *adds* the
#: points it names, so it can never resurrect an unrelated grant an
#: administrator removed.  Keep the ids stable — they are the primary key.
_SEED_TOPUPS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    # (id, built-in role code, points that release added to the seed)
    (
        "2026-09-npm-download",
        P.AUTHENTICATED_ROLE,
        (P.NPM_DOWNLOAD,),
    ),
    (
        "2026-09-nodebuild",
        P.AUTHENTICATED_ROLE,
        (
            P.NODE_BUILD_READ,
            P.NODE_BUILD_DOWNLOAD,
            P.NODE_BUILD_SHA256,
        ),
    ),
    # `app:read` gates the SPA shell, so an existing deployment that does not
    # receive it loses the whole web console — this entry is what keeps an
    # upgrade from locking everybody out of the UI.
    (
        "2026-09-app-read",
        P.AUTHENTICATED_ROLE,
        (P.APP_READ,),
    ),
    # The DSH enterprise-intranet plugin resolves the model routing table (with
    # each route's upstream key) as an ordinary signed-in user; without this the
    # feature would be silently admin-only on an upgraded deployment.
    (
        "2026-09-model-resolve",
        P.AUTHENTICATED_ROLE,
        (P.MODEL_RESOLVE,),
    ),
)


class AuthzService:
    """Role-based access control over the five RBAC tables."""

    #: How long a resolved grant set stays valid.  Within one process a write
    #: bumps the version and invalidates instantly, so this TTL only matters
    #: when several workers serve the same database: worker B cannot see
    #: worker A's version counter, and would otherwise serve a stale grant set
    #: indefinitely.  30s bounds that staleness without a query per request.
    CACHE_TTL_SECONDS = 30.0

    #: How often ``last_login_at`` is allowed to cost a write.  This runs on
    #: the request path for HTTP Basic and session cookies; committing every
    #: time would take a write lock (SQLite) or a round trip (PostgreSQL) for
    #: each `pip` download.
    LOGIN_TOUCH_INTERVAL_SECONDS = 300.0

    def __init__(self, session_factory: scoped_session) -> None:
        self._session_factory = session_factory
        self._version = 0
        self._grant_cache: dict[int, tuple[int, float, frozenset[str]]] = {}
        self._anon_cache: tuple[int, float, frozenset[str]] | None = None

    @property
    def _s(self):
        return self._session_factory()

    # ── Cache ────────────────────────────────────────────────────────

    def invalidate(self) -> None:
        """Drop every cached grant.  Call after any write."""
        self._version += 1
        self._grant_cache.clear()
        self._anon_cache = None

    @classmethod
    def _fresh(cls, entry: tuple[int, float, frozenset[str]] | None,
               version: int) -> frozenset[str] | None:
        """Return the cached grants, or None when the entry must be recomputed."""
        if entry is None:
            return None
        entry_version, stamped_at, grants = entry
        if entry_version != version:
            return None
        if (time.monotonic() - stamped_at) > cls.CACHE_TTL_SECONDS:
            return None
        return grants

    # ══════════════════════════════════════════════════════════════════
    #  Seeding / bootstrap
    # ══════════════════════════════════════════════════════════════════

    def sync_permissions(self) -> dict[str, int]:
        """Upsert the permission points the code declares.

        Only *inserts*: an existing row's name/description/module are left
        alone so admin edits survive.  A row whose module is NULL gets it
        filled in, since that is a value only the code can know.

        Rows are never deleted — a point that disappears from the code may
        still be referenced by a role, and silently dropping it would be a
        surprise.  Instead both directions of drift are reported:
        :meth:`orphan_permissions` (declared but held by no role) and
        :meth:`stale_permissions` (held in the database but no longer declared
        by any guard).
        """
        catalog = P.seed_catalog()
        session = self._s
        created = 0
        try:
            existing = {p.code: p for p in session.query(Permission).all()}
            for code, (name, description, module) in catalog.items():
                row = existing.get(code)
                if row is None:
                    session.add(Permission(
                        code=code, name=name, module=module, description=description,
                    ))
                    created += 1
                elif row.module is None:
                    row.module = module
            session.commit()
            return {"created": created, "total": len(catalog)}
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def sync_builtin_roles(self) -> None:
        """Make sure the three built-in roles exist and are shaped correctly."""
        session = self._s
        try:
            perms = {p.code: p for p in session.query(Permission).all()}
            for code, (name, desc, is_builtin, anon_default, auto_grant) in P.BUILTIN_ROLES.items():
                role = session.query(Role).filter(Role.code == code).first()
                if role is None:
                    role = Role(
                        code=code, name=name, description=desc,
                        is_builtin=is_builtin,
                        is_anonymous_default=anon_default,
                        auto_grant=auto_grant,
                    )
                    session.add(role)
                    session.flush()
                    seed = {
                        P.ANONYMOUS_ROLE: _ANONYMOUS_SEED,
                        P.AUTHENTICATED_ROLE: _AUTHENTICATED_SEED,
                        P.ADMIN_ROLE: tuple(perms),
                    }[code]
                    self._set_role_permissions(session, role, seed, perms)
                    logger.info("Created built-in role %r with %d permission(s)", code, len(seed))
                else:
                    # Keep the flags authoritative; they drive anonymous and
                    # auto-grant behaviour and must not drift.
                    role.is_builtin = True
                    role.is_anonymous_default = anon_default
                    role.auto_grant = auto_grant

            # The admin role is documented as "holds everything": top it up
            # whenever a new permission point appears so new features work
            # without a manual grant.  Deliberately never trimmed.
            admin = session.query(Role).filter(Role.code == P.ADMIN_ROLE).first()
            if admin is not None:
                self._set_role_permissions(session, admin, tuple(perms), perms, additive=True)

            # Bring an already-existing role up to date with points a later
            # release added to its seed, exactly once each.
            self._apply_seed_topups(session, perms)

            # Whatever is still missing after the top-ups is a genuine gap (an
            # administrator revoked it, or the seed changed without a top-up).
            # The admin role is topped up automatically, which means such a
            # point looks "held" and never shows up as an orphan — say it out
            # loud instead of letting signed-in users silently get 403.
            self._warn_missing_authenticated_seed(session)

            session.commit()
            self.invalidate()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    @staticmethod
    def _apply_seed_topups(session, perms: dict[str, Permission]) -> list[str]:
        """Apply each ``_SEED_TOPUPS`` entry at most once.  Returns applied ids.

        Additive by construction: an entry names the exact points one release
        added to a seed, so applying it cannot touch any other grant — an
        administrator's deliberate revocation of some *other* point survives.
        The ``seed_migrations`` row is written in the same transaction, so a
        crash halfway through replays cleanly instead of double-granting.
        """
        applied: list[str] = []
        for migration_id, role_code, codes in _SEED_TOPUPS:
            if session.get(SeedMigration, migration_id) is not None:
                continue
            role = session.query(Role).filter(Role.code == role_code).first()
            if role is None:
                # The role will be created (and freshly seeded) later in a
                # future boot; do not consume the migration.
                continue
            wanted = {perms[c].id for c in codes if c in perms}
            current = {
                rp.permission_id for rp in
                session.query(RolePermission)
                .filter(RolePermission.role_id == role.id).all()
            }
            added = wanted - current
            for pid in added:
                session.add(RolePermission(role_id=role.id, permission_id=pid))
            session.add(SeedMigration(code=migration_id))
            applied.append(migration_id)
            logger.info(
                "Seed migration %r: granted %d point(s) to role %r (%s)",
                migration_id, len(added), role_code,
                ", ".join(sorted(codes)) if codes else "—",
            )
        return applied

    @staticmethod
    def _warn_missing_authenticated_seed(session) -> None:
        """Log seeded points the ``authenticated`` role does not actually hold."""
        if session.query(Role).filter(Role.code == P.AUTHENTICATED_ROLE).first() is None:
            return
        held = AuthzService._role_permission_codes(session, P.AUTHENTICATED_ROLE)
        missing = sorted(set(_AUTHENTICATED_SEED) - held)
        if missing:
            logger.warning(
                "The %r role does not hold seeded point(s): %s — grant them on "
                "/access (or with AuthzService.set_role_permissions); until then "
                "signed-in users get 403 for those features.",
                P.AUTHENTICATED_ROLE, ", ".join(missing),
            )

    def bootstrap_superusers(self, identifiers: Iterable[str]) -> list[str]:
        """Promote configured admins **only when no superuser exists yet**.

        This makes ``ADMIN_USERS`` a one-shot cold-start seed rather than a
        permanent back door: whoever can set an environment variable would
        otherwise be able to grant themselves the server.  After the first
        superuser exists the setting is inert.
        """
        wanted = [i for i in (identifiers or []) if i and str(i).strip()]
        if not wanted:
            return []

        session = self._s
        try:
            has_super = (
                session.query(User).filter(User.is_superuser.is_(True)).first() is not None
            )
            if has_super:
                return []

            promoted: list[str] = []
            for ident in wanted:
                ident = str(ident).strip()
                user = session.query(User).filter(User.external_id == ident).first()
                if user is None:
                    user = User(
                        provider="bootstrap", external_id=ident,
                        display_name=ident, is_active=True, is_superuser=True,
                    )
                    session.add(user)
                else:
                    user.is_superuser = True
                promoted.append(ident)
            session.commit()
            self.invalidate()
            logger.warning(
                "Bootstrapped %d superuser(s) from ADMIN_USERS: %s — "
                "this setting is now inert; manage admins with the CLI instead.",
                len(promoted), ", ".join(promoted),
            )
            return promoted
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    # ══════════════════════════════════════════════════════════════════
    #  Users
    # ══════════════════════════════════════════════════════════════════

    def provision_user(
        self,
        provider: str,
        external_id: str,
        display_name: str | None = None,
        email: str | None = None,
    ) -> User | None:
        """Find or create the shadow account for an external identity.

        Called on every successful authentication (just-in-time provisioning),
        so a user's roles can exist before they ever log in — which is what
        makes ``cli.py create-admin`` work for someone who has never
        connected.  Freshly created accounts also receive every
        ``auto_grant`` role.
        """
        external_id = (external_id or "").strip()
        if not external_id:
            return None
        provider = (provider or "local").strip() or "local"

        session = self._s
        try:
            user = (
                session.query(User)
                .filter(User.external_id == external_id)
                .first()
            )
            if user is None:
                user = User(
                    provider=provider, external_id=external_id,
                    display_name=display_name or external_id, email=email,
                )
                session.add(user)
                try:
                    session.flush()
                except IntegrityError:
                    # Lost a race with a concurrent first login — re-read.
                    session.rollback()
                    user = (
                        session.query(User)
                        .filter(User.external_id == external_id)
                        .first()
                    )
                    if user is None:
                        raise
                    return user
                self._apply_auto_grant(session, user)
                logger.info("Provisioned user %s:%s (id=%s)", provider, external_id, user.id)
            else:
                if display_name and user.display_name != display_name:
                    user.display_name = display_name
                if email and not user.email:
                    user.email = email

            if self._should_touch_login(user.last_login_at):
                user.last_login_at = utcnow()

            session.commit()
            self.invalidate()
            session.expunge(user)
            return user
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    @classmethod
    def _should_touch_login(cls, last_login_at: datetime | None) -> bool:
        """True when ``last_login_at`` is stale enough to be worth a write.

        The two backends disagree about timezones: SQLite has no timezone type,
        so a value read back is naive, while PostgreSQL returns an aware value
        for the same ``DateTime(timezone=True)`` column.  Normalise before
        subtracting rather than trusting either.
        """
        if last_login_at is None:
            return True
        if last_login_at.tzinfo is None:
            last_login_at = last_login_at.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - last_login_at).total_seconds()
        return age > cls.LOGIN_TOUCH_INTERVAL_SECONDS

    def get_user(self, user_id: int) -> User | None:
        session = self._s
        try:
            user = session.get(User, user_id)
            if user is not None:
                session.expunge(user)
            return user
        finally:
            session.close()

    def find_user(self, external_id: str, provider: str | None = None) -> User | None:
        session = self._s
        try:
            q = session.query(User).filter(User.external_id == external_id)
            if provider:
                q = q.filter(User.provider == provider)
            user = q.first()
            if user is not None:
                session.expunge(user)
            return user
        finally:
            session.close()

    def list_users(self, *, limit: int = 200, offset: int = 0) -> list[dict]:
        session = self._s
        try:
            users = (
                session.query(User)
                .order_by(User.id)
                .offset(offset).limit(limit).all()
            )
            out = []
            for u in users:
                codes = [
                    code for (code,) in session.query(Role.code)
                    .join(UserRole, UserRole.role_id == Role.id)
                    .filter(UserRole.user_id == u.id).all()
                ]
                out.append(u.to_dict() | {"roles": sorted(codes)})
            return out
        finally:
            session.close()

    # ══════════════════════════════════════════════════════════════════
    #  Authorization — the hot path
    # ══════════════════════════════════════════════════════════════════

    def _grants_for_user_id(self, user_id: int) -> frozenset[str]:
        cached = self._fresh(self._grant_cache.get(user_id), self._version)
        if cached is not None:
            return cached

        session = self._s
        try:
            rows = (
                session.query(Permission.code)
                .join(RolePermission, RolePermission.permission_id == Permission.id)
                .join(Role, Role.id == RolePermission.role_id)
                .join(UserRole, UserRole.role_id == Role.id)
                .filter(UserRole.user_id == user_id)
                .distinct()
                .all()
            )
            grants = frozenset(code for (code,) in rows)
        finally:
            session.close()

        self._grant_cache[user_id] = (self._version, time.monotonic(), grants)
        return grants

    def anonymous_grants(self) -> frozenset[str]:
        """Permissions of every role flagged ``is_anonymous_default``."""
        cached = self._fresh(self._anon_cache, self._version)
        if cached is not None:
            return cached

        session = self._s
        try:
            rows = (
                session.query(Permission.code)
                .join(RolePermission, RolePermission.permission_id == Permission.id)
                .join(Role, Role.id == RolePermission.role_id)
                .filter(Role.is_anonymous_default.is_(True))
                .distinct()
                .all()
            )
            grants = frozenset(code for (code,) in rows)
        finally:
            session.close()

        self._anon_cache = (self._version, time.monotonic(), grants)
        return grants

    def has_permission(self, principal: dict | None, code: str) -> bool:
        """The check every guard ultimately makes.

        *principal* is the ``g.auth_user`` dict (or None when unauthenticated).
        """
        if principal is None:
            return code in self.anonymous_grants()
        if principal.get("is_superuser"):
            return True
        user_id = principal.get("user_id")
        if user_id is None:
            return code in self.anonymous_grants()
        return code in self._grants_for_user_id(user_id)

    def permission_codes(self, principal: dict | None) -> frozenset[str]:
        """Effective permissions — used to render the UI, not to enforce."""
        if principal is None:
            return self.anonymous_grants()
        if principal.get("is_superuser"):
            session = self._s
            try:
                return frozenset(
                    code for (code,) in session.query(Permission.code).all()
                )
            finally:
                session.close()
        user_id = principal.get("user_id")
        if user_id is None:
            return self.anonymous_grants()
        return self._grants_for_user_id(user_id)

    def role_codes(self, user_id: int) -> list[str]:
        session = self._s
        try:
            rows = (
                session.query(Role.code)
                .join(UserRole, UserRole.role_id == Role.id)
                .filter(UserRole.user_id == user_id)
                .order_by(Role.code)
                .all()
            )
            return [code for (code,) in rows]
        finally:
            session.close()

    # ══════════════════════════════════════════════════════════════════
    #  Roles
    # ══════════════════════════════════════════════════════════════════

    def list_roles(self) -> list[dict]:
        session = self._s
        try:
            roles = session.query(Role).order_by(Role.id).all()
            out = []
            for role in roles:
                codes = [
                    code for (code,) in
                    session.query(Permission.code)
                    .join(RolePermission, RolePermission.permission_id == Permission.id)
                    .filter(RolePermission.role_id == role.id)
                    .order_by(Permission.code).all()
                ]
                count = (
                    session.query(UserRole).filter(UserRole.role_id == role.id).count()
                )
                out.append(role.to_dict(permission_codes=codes, user_count=count))
            return out
        finally:
            session.close()

    def get_role(self, code: str | None = None, role_id: int | None = None) -> Role | None:
        session = self._s
        try:
            q = session.query(Role)
            role = q.filter(Role.id == role_id).first() if role_id is not None \
                else q.filter(Role.code == code).first()
            if role is not None:
                session.expunge(role)
            return role
        finally:
            session.close()

    def create_role(self, code: str, name: str, description: str | None = None) -> dict:
        code = (code or "").strip()
        if not code:
            raise ValueError("role code is required")
        if not code.replace("-", "").replace("_", "").isalnum():
            raise ValueError("role code may only contain letters, digits, '-' and '_'")
        session = self._s
        try:
            if session.query(Role).filter(Role.code == code).first() is not None:
                raise ValueError(f"role {code!r} already exists")
            role = Role(code=code, name=(name or code).strip(), description=description)
            session.add(role)
            session.commit()
            self.invalidate()
            return role.to_dict(permission_codes=[])
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def delete_role(self, role_id: int) -> None:
        session = self._s
        try:
            role = session.get(Role, role_id)
            if role is None:
                raise ValueError("role not found")
            if role.is_builtin:
                raise ValueError(f"role {role.code!r} is built-in and cannot be deleted")
            session.delete(role)
            session.commit()
            self.invalidate()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def set_role_permissions(self, role_id: int, codes: Iterable[str]) -> list[str]:
        session = self._s
        try:
            role = session.get(Role, role_id)
            if role is None:
                raise ValueError("role not found")
            perms = {p.code: p for p in session.query(Permission).all()}
            unknown = [c for c in codes if c not in perms]
            # Unknown codes are created on the fly: an admin may legitimately
            # define a point before the code that checks it ships.
            for code in unknown:
                perm = Permission(
                    code=code, name=code, module=P.module_of(code),
                    description="由管理后台创建；尚无代码检查该权限点",
                )
                session.add(perm)
                session.flush()
                perms[code] = perm
            self._set_role_permissions(session, role, tuple(codes), perms, replace=True)
            session.commit()
            self.invalidate()
            return sorted(set(codes))
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    @staticmethod
    def _set_role_permissions(session, role: Role, codes: Iterable[str],
                              perms: dict[str, Permission], *,
                              replace: bool = False, additive: bool = False) -> int:
        """Rewrite (or top up) a role's permission rows.  Returns rows changed."""
        wanted = {perms[c].id for c in codes if c in perms}

        current = {
            rp.permission_id for rp in
            session.query(RolePermission).filter(RolePermission.role_id == role.id).all()
        }
        if replace:
            for pid in current - wanted:
                session.query(RolePermission).filter(
                    RolePermission.role_id == role.id,
                    RolePermission.permission_id == pid,
                ).delete()
        add = wanted - current
        for pid in add:
            session.add(RolePermission(role_id=role.id, permission_id=pid))
        return len(add) + (len(current - wanted) if replace else 0)

    # ══════════════════════════════════════════════════════════════════
    #  Grants
    # ══════════════════════════════════════════════════════════════════

    def grant_role(self, user_id: int, role_code: str, granted_by: int | None = None) -> bool:
        """Grant *role_code* to *user_id*.  Returns False if already held."""
        session = self._s
        try:
            role = session.query(Role).filter(Role.code == role_code).first()
            if role is None:
                raise ValueError(f"role {role_code!r} not found")
            if session.get(User, user_id) is None:
                raise ValueError(f"user {user_id} not found")
            exists = session.query(UserRole).filter(
                UserRole.user_id == user_id, UserRole.role_id == role.id
            ).first()
            if exists is not None:
                return False
            session.add(UserRole(user_id=user_id, role_id=role.id, granted_by=granted_by))
            session.commit()
            self.invalidate()
            return True
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def revoke_role(self, user_id: int, role_code: str) -> bool:
        session = self._s
        try:
            role = session.query(Role).filter(Role.code == role_code).first()
            if role is None:
                return False
            deleted = session.query(UserRole).filter(
                UserRole.user_id == user_id, UserRole.role_id == role.id
            ).delete()
            session.commit()
            self.invalidate()
            return bool(deleted)
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def set_superuser(self, user_id: int, value: bool = True) -> bool:
        session = self._s
        try:
            user = session.get(User, user_id)
            if user is None:
                raise ValueError(f"user {user_id} not found")
            user.is_superuser = bool(value)
            session.commit()
            self.invalidate()
            return True
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def count_superusers(self) -> int:
        session = self._s
        try:
            return session.query(User).filter(User.is_superuser.is_(True)).count()
        finally:
            session.close()

    @staticmethod
    def _apply_auto_grant(session, user: User) -> None:
        """Give a brand-new user every role flagged ``auto_grant``."""
        roles = session.query(Role).filter(Role.auto_grant.is_(True)).all()
        for role in roles:
            exists = session.query(UserRole).filter(
                UserRole.user_id == user.id, UserRole.role_id == role.id
            ).first()
            if exists is None:
                session.add(UserRole(user_id=user.id, role_id=role.id))

    # ══════════════════════════════════════════════════════════════════
    #  Permissions (read-only views for the UI / self-check)
    # ══════════════════════════════════════════════════════════════════

    def list_permissions(self) -> list[dict]:
        """The catalogue, each row annotated with the drift signals the UI shows.

        Two flags are computed per row:

        * ``stale`` — the row exists in the database but no guard declares it
          any more (a rename or a removed feature left it behind).  It stays
          grantable and no route will ever check it.
        * ``authenticated_pending`` — the code seeds the point to the
          ``authenticated`` role, but that row does not hold it yet.  That is
          exactly what an upgraded deployment looks like after a new point
          ships, and it is otherwise invisible because the ``admin`` role is
          topped up automatically and hides the gap.
        """
        catalog = set(P.seed_catalog())
        expected = set(_AUTHENTICATED_SEED)
        session = self._s
        try:
            perms = session.query(Permission).order_by(Permission.module, Permission.code).all()
            auth_held = self._role_permission_codes(session, P.AUTHENTICATED_ROLE)
            out = []
            for perm in perms:
                count = session.query(RolePermission).filter(
                    RolePermission.permission_id == perm.id
                ).count()
                row = perm.to_dict(role_count=count)
                row["stale"] = perm.code not in catalog
                row["expected_for_authenticated"] = perm.code in expected
                row["held_by_authenticated"] = perm.code in auth_held
                row["authenticated_pending"] = (
                    perm.code in expected and perm.code not in auth_held
                )
                out.append(row)
            return out
        finally:
            session.close()

    def orphan_permissions(self) -> list[str]:
        """Points the code declares that no role holds.

        A non-empty result is the signal that a permission code was mistyped
        (the typo becomes an orphan) or that a role assignment is missing.
        Logged as a warning at startup; also surfaced at ``/admin/api/permissions``.
        """
        catalog = set(P.seed_catalog())
        session = self._s
        try:
            held = {
                code for (code,) in
                session.query(Permission.code)
                .join(RolePermission, RolePermission.permission_id == Permission.id)
                .distinct().all()
            }
        finally:
            session.close()
        return sorted(catalog - held)

    def stale_permissions(self) -> list[str]:
        """Catalogue rows no guard declares any more.

        The mirror image of :meth:`orphan_permissions`.  ``sync_permissions``
        never deletes, so a point that was renamed or whose feature was removed
        stays in the catalogue: an administrator still sees it on ``/access``,
        can still grant it, and no route will ever check it.  Logged as a
        warning at startup.
        """
        catalog = set(P.seed_catalog())
        session = self._s
        try:
            codes = {code for (code,) in session.query(Permission.code).all()}
        finally:
            session.close()
        return sorted(codes - catalog)

    @staticmethod
    def _role_permission_codes(session, role_code: str) -> set[str]:
        """Permission codes a built-in role holds.  Empty set when it is absent."""
        role = session.query(Role).filter(Role.code == role_code).first()
        if role is None:
            return set()
        return {
            code for (code,) in
            session.query(Permission.code)
            .join(RolePermission, RolePermission.permission_id == Permission.id)
            .filter(RolePermission.role_id == role.id)
            .all()
        }


def bootstrap(authz: AuthzService, admin_users: Iterable[str] = ()) -> dict:
    """Idempotent startup seeding.

    Call this *after* the blueprints have been imported, because route guards
    are what declare permission points.  Returns a small summary suitable for
    logging.
    """
    perms = authz.sync_permissions()
    authz.sync_builtin_roles()
    promoted = authz.bootstrap_superusers(admin_users)

    orphans = authz.orphan_permissions()
    if orphans:
        # Usually a typo in a @require_permission(...) code: the typo becomes a
        # permission point no role holds, so the route silently denies everyone
        # except superusers.  Loud on purpose.
        logger.warning(
            "Permission points held by no role: %s — "
            "check for a mistyped @require_permission code",
            ", ".join(orphans),
        )

    stale = authz.stale_permissions()
    if stale:
        # Drift in the other direction: a row survived a rename or a removed
        # feature.  It is still offered on /access and can still be granted,
        # but no guard checks it, so granting it changes nothing.
        logger.warning(
            "Permission points no guard declares any more: %s — rename/remove "
            "them, or drop the stale `permissions` rows; granting one has no effect",
            ", ".join(stale),
        )

    return {
        "permissions": perms,
        "superusers": promoted,
        "orphans": orphans,
        "stale": stale,
    }


__all__ = ["AuthzService", "bootstrap"]
