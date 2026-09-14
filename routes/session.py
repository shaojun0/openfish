"""Session introspection — who is the current caller, and what may they do?

The SPA needs this *before* it can render anything, so ``/api/v1/session``
answers with HTTP 200 even for an anonymous caller instead of raising 401.
That keeps the client-side 401 interceptor reserved for real API calls.
"""

from __future__ import annotations

from flask import Blueprint, g, jsonify

from auth.decorators import require_permission
from auth.guards import AUTH_METHODS
from auth.permissions import Permission, get_permissions, has_permission
from config import settings
from openapi import api_operation, array_of, errors, ok

session_bp = Blueprint("session", __name__)

# Browser sessions first — a logged-in user in the SPA is identified by cookie.
# Then programmatic credentials, matching the order used elsewhere.
_IDENTITY_ORDER = ("session", "api_key", "bearer", "basic", "api_key_basic")


def identify() -> tuple[dict | None, str | None]:
    """Try every credential in turn; return the identity or ``(None, None)``.

    Unlike :func:`auth.decorators.require_auth` this never raises.
    """
    for method in _IDENTITY_ORDER:
        fn = AUTH_METHODS.get(method)
        if fn is None or not fn():
            continue
        user = getattr(g, "auth_user", None)
        if isinstance(user, dict):
            return user, method
    return None, None


@session_bp.route("/session")
@api_operation(
    summary="Describe the calling identity",
    description=(
        "Returns the authenticated subject, its role, and the exact permission "
        "strings it holds. Always answers HTTP 200: an anonymous caller gets "
        "`authenticated: false` rather than a 401, so a client can render a "
        "signed-out state without treating it as an error.\n\n"
        "**Start here.** The `permissions` array tells you which of the other "
        "endpoints the configured credential may call."
    ),
    tags=["Session"],
    security=[],
    responses={"200": ok("The calling identity", "SessionInfo")},
)
def whoami():
    user, method = identify()
    role = user.get("role", "anonymous") if user else "anonymous"

    return jsonify(
        {
            "authenticated": user is not None,
            "auth_enabled": settings.auth.auth_enabled,
            "user": user.get("sub") if user else None,
            "role": role,
            "permissions": sorted(p.value for p in get_permissions(role)),
            "server_name": settings.server.server_name,
            "is_admin": has_permission(role, Permission.ADMIN_VIEW),
            "auth_method": method,
        }
    )


@session_bp.route("/packages")
@require_permission(Permission.PACKAGE_READ)
@api_operation(
    summary="List packages",
    description=(
        "Every package in the registry with its file count, total size and "
        "per-package download/upload counters. Sorted by activity, most active "
        "first. Use the trailing `/simple/` endpoints to enumerate the actual "
        "files of one package."
    ),
    tags=["Packages"],
    responses={
        "200": ok("Package list", array_of("PackageSummary")),
        **errors("401", "403", "500"),
    },
)
def packages():
    """Aggregated package view for the SPA's package browser.

    Reuses the same computation the admin dashboard relies on so the two
    views can never disagree on counts or sizes.
    """
    from flask import current_app

    from services.stats import compute

    pkg_index = current_app.extensions.get("pypi_index")
    key_mgr = current_app.extensions.get("api_key_manager")
    return jsonify(compute(pkg_index, key_mgr)["packages"])
