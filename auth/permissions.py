"""Permission model — fine-grained access control.

Usage::

    from auth.permissions import Permission
    from auth.decorators import require_permission

    @require_permission(Permission.PACKAGE_WRITE)
    def upload():
        ...

Extend by adding new Permission values + updating ROLES mapping.
Routes never need to change when permissions evolve.
"""

from enum import Enum


class Permission(str, Enum):
    """Granular permissions for cpypiserver.

    Each route is protected by one or more permissions.
    Roles aggregate permissions; the decorator checks membership.

    Adding a new Permission:
        1. Add the value here.
        2. Add it to the relevant role(s) in ROLES.
    """

    # Package operations
    PACKAGE_READ = "package:read"
    PACKAGE_WRITE = "package:write"

    # Build operations
    BUILD_READ = "build:read"
    BUILD_DOWNLOAD = "build:download"
    BUILD_SHA256 = "build:sha256"

    # API key management
    KEY_LIST = "key:list"
    KEY_CREATE = "key:create"
    KEY_DELETE = "key:delete"
    KEY_STATS = "key:stats"

    # Admin
    ADMIN_VIEW = "admin:view"
    ADMIN_REFRESH = "admin:refresh"


# ── Role → permission set mapping ────────────────────────────────────

ROLES: dict[str, set[Permission]] = {
    "anonymous": {
        Permission.PACKAGE_READ,
        Permission.BUILD_READ,
    },
    "authenticated": {
        # Inherits anonymous + adds write
        Permission.PACKAGE_READ,
        Permission.PACKAGE_WRITE,
        Permission.BUILD_READ,
        Permission.BUILD_DOWNLOAD,
        Permission.BUILD_SHA256,
        Permission.KEY_LIST,
        Permission.KEY_CREATE,
        Permission.KEY_DELETE,
        Permission.KEY_STATS,
    },
    "admin": {
        # All permissions
        Permission.PACKAGE_READ,
        Permission.PACKAGE_WRITE,
        Permission.BUILD_READ,
        Permission.BUILD_DOWNLOAD,
        Permission.BUILD_SHA256,
        Permission.KEY_LIST,
        Permission.KEY_CREATE,
        Permission.KEY_DELETE,
        Permission.KEY_STATS,
        Permission.ADMIN_VIEW,
        Permission.ADMIN_REFRESH,
    },
}


def get_permissions(role: str) -> set[Permission]:
    """Return the permission set for a named role, or empty set."""
    return ROLES.get(role, set())


def has_permission(role: str, perm: Permission) -> bool:
    """Check whether *role* possesses *perm*."""
    return perm in get_permissions(role)


def role_for(identifier: str) -> str:
    """Map an authenticated user identifier to a role.

    The configured HTTP Basic username and anyone listed in ``ADMIN_USERS`` are
    administrators; every other authenticated identifier is a regular member.

    This is the single definition of that rule. Session, OAuth2 and API-key
    authentication all resolve roles through it, so a key cannot end up with a
    different role than the user it was issued to.
    """
    from config import settings

    if not identifier:
        return "authenticated"
    if settings.auth.basic_username and identifier == settings.auth.basic_username:
        return "admin"
    if identifier in settings.server.admin_users:
        return "admin"
    return "authenticated"
