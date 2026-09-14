"""Authentication decorators — permission-based.

Every decorator follows the same contract:
  1. Try allowed auth methods in order.
  2. On success → resolve the user's role.
  3. Check the required permission against the role.

Usage::

    @require_permission(Permission.PACKAGE_READ)
    def download(...): ...

    @require_permission(Permission.ADMIN_REFRESH)
    def refresh_stats(...): ...

Blueprints use before_request hooks:

    bp.before_request(require_auth())
    bp.before_request(require_permission(Permission.PACKAGE_WRITE))
"""

from functools import wraps

from flask import g

from auth.permissions import Permission, has_permission
from auth.guards import AUTH_METHODS
from errors import UnauthorizedError, ForbiddenError


def _get_role() -> str:
    """Extract the role from the current auth context. Defaults to 'anonymous'."""
    user = getattr(g, "auth_user", None)
    if user is None:
        return "anonymous"
    if isinstance(user, dict):
        return user.get("role", "anonymous")
    return "anonymous"


def require_auth(*, methods: list[str] | None = None):
    """Auth guard — authenticate the request, but don't check specific permissions.

    Acceptable as both a before_request hook and a per-route decorator.

    Args:
        methods: Auth methods to try.  ``None`` = all in order.
    """
    allowed = methods or list(AUTH_METHODS)

    def _authenticate():
        for method in allowed:
            fn = AUTH_METHODS.get(method)
            if fn and fn():
                return None  # authenticated
        raise UnauthorizedError(
            message=f"Authentication required — allowed: {', '.join(allowed)}"
        )

    def guard(*args, **kwargs):
        if len(args) == 1 and callable(args[0]):
            # Per-route decorator
            f = args[0]

            @wraps(f)
            def wrapper(*w_args, **w_kwargs):
                _authenticate()
                return f(*w_args, **w_kwargs)

            return wrapper
        # before_request
        return _authenticate()

    return guard


def require_permission(perm: Permission, *, methods: list[str] | None = None):
    """Decorator / before_request guard: require a specific permission.

    The user must BOTH authenticate AND possess *perm*.

    Usage::

        @require_permission(Permission.PACKAGE_WRITE)
        def upload():
            ...

        admin_bp.before_request(require_permission(Permission.ADMIN_VIEW))
    """
    allowed = methods or list(AUTH_METHODS)

    def _check():
        # 1. Authenticate
        for method in allowed:
            fn = AUTH_METHODS.get(method)
            if fn and fn():
                break
        else:
            raise UnauthorizedError(
                message=f"Authentication required — allowed: {', '.join(allowed)}"
            )

        # 2. Authorize
        role = _get_role()
        if not has_permission(role, perm):
            raise ForbiddenError(
                message=f"Permission '{perm.value}' required (role: {role})"
            )

    def guard(*args, **kwargs):
        if len(args) == 1 and callable(args[0]):
            f = args[0]

            @wraps(f)
            def wrapper(*w_args, **w_kwargs):
                _check()
                return f(*w_args, **w_kwargs)

            return wrapper
        return _check()

    return guard


# ── Convenience aliases ──────────────────────────────────────────────

def require_admin(f=None):
    """Shortcut for @require_admin on admin-only routes.

    Can be used as ``@require_admin`` or ``require_admin()``.
    """
    guard = require_permission(Permission.ADMIN_VIEW)
    if f is not None:
        return guard(f)
    return guard


def require_package_read(f=None):
    """Shortcut: require PACKAGE_READ permission."""
    guard = require_permission(Permission.PACKAGE_READ)
    if f is not None:
        return guard(f)
    return guard


def require_package_write(f=None):
    """Shortcut: require PACKAGE_WRITE permission."""
    guard = require_permission(Permission.PACKAGE_WRITE)
    if f is not None:
        return guard(f)
    return guard
