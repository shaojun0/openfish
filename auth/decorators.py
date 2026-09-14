"""Authentication and authorization decorators.

Two guards are provided::

    require_auth()                        # authenticated, any permissions
    require_permission("package:write")   # authenticated AND holds the point

Both work in two positions:

1. **Blueprint-wide**, as a ``before_request`` hook — the robust choice::

       bp.before_request(require_permission(PACKAGE_WRITE))

2. **Per route**, as a decorator — written *below* ``@bp.route``::

       @bp.route("/", methods=["POST"])
       @require_permission(PACKAGE_WRITE)
       def upload(): ...

   ⚠ Order is not optional.  Decorators apply bottom-up, so the permission
   check must be the **first** one applied and ``@bp.route`` the last::

       @bp.route(...)          # applied last  ✅ registers the guarded wrapper
       @require_permission(X)  # applied first ✅
       def view(): ...

   Written the other way round, ``bp.route`` registers the *undecorated*
   function and the check silently never runs.  That is exactly the bug this
   rewrite fixes in ``routes/python_build.py`` — prefer ``before_request`` for
   blueprint-wide policy so the ordering trap cannot come back.
"""

from __future__ import annotations

from functools import wraps
from typing import Optional

from flask import current_app, g

from auth.permissions import ADMIN_VIEW, PACKAGE_READ, PACKAGE_WRITE, declare
from auth.guards import AUTH_METHODS
from errors import ForbiddenError, UnauthorizedError


# ── Principal accessors ──────────────────────────────────────────────

def current_principal() -> Optional[dict]:
    """The ``g.auth_user`` dict, or None when unauthenticated."""
    return getattr(g, "auth_user", None)


def current_user_id() -> Optional[int]:
    principal = current_principal()
    return principal.get("user_id") if principal else None


def current_sub() -> Optional[str]:
    """Stable identity of the caller (never a display name)."""
    principal = current_principal()
    return principal.get("sub") if principal else None


def current_display_name() -> str:
    principal = current_principal()
    if not principal:
        return "anonymous"
    return principal.get("display_name") or principal.get("sub") or "unknown"


def can(perm: str) -> bool:
    """Ad-hoc permission check for templates / view bodies."""
    authz = current_app.extensions.get("authz")
    if authz is None:
        return False
    return authz.has_permission(current_principal(), perm)


# ── Internals ────────────────────────────────────────────────────────

def _authenticate(allowed: list[str]) -> bool:
    """Try each permitted method in order; the first success wins.

    Short-circuits when an earlier guard already authenticated this request.
    A blueprint-wide ``require_auth()`` plus a per-route ``require_permission()``
    would otherwise authenticate twice — and for a bearer token that means a
    second introspection round trip to the identity provider.
    """
    if getattr(g, "auth_user", None) is not None:
        return True
    for method in allowed:
        fn = AUTH_METHODS.get(method)
        if fn and fn():
            return True
    return False


def _unauthorized(allowed: list[str]) -> UnauthorizedError:
    # Carry a Basic challenge unconditionally.  Package managers and container
    # clients only send credentials after a 401 tells them which scheme to use;
    # npm and docker both answer a bare 401 by giving up.  The browser-facing
    # redirect for the SPA is chosen by the error handler, which reads this
    # challenge only when it is not answering a machine client.
    return UnauthorizedError(
        message=f"Authentication required — allowed: {', '.join(allowed)}",
        www_authenticate='Basic realm="cpypiserver"',
    )


# ── Guards ───────────────────────────────────────────────────────────

def require_auth(*, methods: list[str] | None = None):
    """Authenticate the request without requiring any specific permission."""
    allowed = list(methods or AUTH_METHODS)

    def _check() -> None:
        if not _authenticate(allowed):
            raise _unauthorized(allowed)

    def guard(*args, **kwargs):
        if len(args) == 1 and callable(args[0]) and not kwargs:
            f = args[0]

            @wraps(f)
            def wrapper(*w_args, **w_kwargs):
                _check()
                return f(*w_args, **w_kwargs)

            return wrapper
        _check()
        return None

    return guard


def require_permission(perm: str, *, methods: list[str] | None = None):
    """Authenticate the request *and* require the permission point *perm*."""
    declare(perm)
    allowed = list(methods or AUTH_METHODS)

    def _check() -> None:
        if not _authenticate(allowed):
            raise _unauthorized(allowed)

        authz = current_app.extensions.get("authz")
        if authz is None:
            raise ForbiddenError(message="Authorization service unavailable")

        principal = current_principal()
        if not authz.has_permission(principal, perm):
            who = (principal or {}).get("sub") or "anonymous"
            raise ForbiddenError(
                message=f"Permission '{perm}' required (account: {who})"
            )

    def guard(*args, **kwargs):
        if len(args) == 1 and callable(args[0]) and not kwargs:
            f = args[0]

            @wraps(f)
            def wrapper(*w_args, **w_kwargs):
                _check()
                return f(*w_args, **w_kwargs)

            return wrapper
        _check()
        return None

    return guard


# ── Convenience aliases ──────────────────────────────────────────────

def require_admin(f=None):
    """``@require_admin`` / ``require_admin()`` — requires ``admin:view``."""
    guard = require_permission(ADMIN_VIEW)
    return guard(f) if f is not None else guard


def require_package_read(f=None):
    guard = require_permission(PACKAGE_READ)
    return guard(f) if f is not None else guard


def require_package_write(f=None):
    guard = require_permission(PACKAGE_WRITE)
    return guard(f) if f is not None else guard


__all__ = [
    "require_auth", "require_permission",
    "require_admin", "require_package_read", "require_package_write",
    "current_principal", "current_user_id", "current_sub",
    "current_display_name", "can",
]
