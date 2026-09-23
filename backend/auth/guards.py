"""Auth guards — one ``try_*`` function per authentication method.

Each function tries exactly one way of proving identity.  On success it sets:

* ``g.auth_user``   — the principal dict the authorization layer consumes
* ``g.auth_method`` — which method succeeded

and, as a side effect, **provisions the account** into the ``users`` table
(just in time).  That is what lets an administrator grant a role to somebody
who has never logged in yet — which in turn is what makes
``python cli.py create-admin <id>`` work.

The principal dict looks like::

    {
      "user_id": 3,
      "sub": "zhangsan",          # stable identity — never a display name
      "display_name": "张三",
      "provider": "oauth2",
      "roles": ["admin"],
      "is_superuser": False,
      "auth_method": "introspect",
      "key_id": "k_ab12cd34ef56",  # API-key paths only
      "key_name": "ci-deploy",
    }
"""

from __future__ import annotations

import logging
from flask import current_app, g, request, session

from config import settings
from auth.oauth import identity_from_info, introspect_token
from models.user import User

logger = logging.getLogger("cpypiserver.auth")


# ── Plumbing ─────────────────────────────────────────────────────────

def _authz():
    return current_app.extensions.get("authz")


def _bearer_token() -> str:
    """Extract raw Bearer token from the Authorization header."""
    h = request.headers.get("Authorization", "")
    if h.startswith("Bearer "):
        return h[7:].strip()
    return ""


def _principal(user: User, method: str, **extra) -> dict:
    authz = _authz()
    return {
        "user_id": user.id,
        "sub": user.external_id,
        "display_name": user.label,
        "provider": user.provider,
        "roles": authz.role_codes(user.id) if authz else [],
        "is_superuser": bool(user.is_superuser),
        "auth_method": method,
        **extra,
    }


def _identify(
    provider: str,
    external_id: str,
    *,
    display_name: str | None = None,
    email: str | None = None,
    method: str,
    **extra,
) -> bool:
    """Provision (find-or-create) the account and install it as the principal."""
    authz = _authz()
    if authz is None:
        logger.error("authz service unavailable — cannot authenticate")
        return False
    user = authz.provision_user(provider, external_id, display_name, email)
    if user is None:
        return False
    if not user.is_active:
        logger.warning("Rejected login for deactivated account %r", user.external_id)
        return False
    g.auth_user = _principal(user, method, **extra)
    g.auth_method = method
    return True


def _identify_existing(user_id: int, *, method: str, **extra) -> bool:
    """Install an already-known account without touching the database."""
    authz = _authz()
    if authz is None:
        return False
    user = authz.get_user(user_id)
    if user is None or not user.is_active:
        return False
    g.auth_user = _principal(user, method, **extra)
    g.auth_method = method
    return True


# ── Authentication methods ───────────────────────────────────────────

def try_basic() -> bool:
    """HTTP Basic from configuration.

    Disabled unless BOTH username and password are configured, so an unset
    (empty) configuration can never be matched by ``":"``.
    """
    if not settings.auth.basic_username or not settings.auth.basic_password:
        return False
    auth = request.authorization
    if auth is None:
        return False
    if (
        auth.username == settings.auth.basic_username
        and auth.password == settings.auth.basic_password
    ):
        return _identify(
            "basic", auth.username,
            display_name=settings.auth.basic_username,
            method="basic",
        )
    return False


def try_api_key(raw_key: str | None = None) -> bool:
    """Bearer API key from the Authorization header."""
    return _verify_api_key(raw_key if raw_key is not None else _bearer_token())


def try_api_key_basic() -> bool:
    """HTTP Basic with username=__token__ (twine-compatible)."""
    auth = request.authorization
    if auth is None or auth.username != "__token__":
        return False
    return _verify_api_key(auth.password)


def try_bearer_oauth2() -> bool:
    """Bearer OAuth2 access token, validated by introspection."""
    token = _bearer_token()
    if not token:
        return False
    return _identify_oauth_token(token)


def try_session() -> bool:
    """Session cookie (admin dashboard or OAuth2 login flow).

    Order: a resolved account id first, then the legacy admin flag, then the
    OAuth2 access token captured by the login callback.
    """
    user_id = session.get("uid")
    if user_id:
        if _identify_existing(int(user_id), method="session"):
            return True
        # Stale session pointing at a deleted account — drop it.
        session.pop("uid", None)

    # Legacy admin session (pre-RBAC deployments stored only a display name).
    if session.get("admin_authenticated"):
        ident = (session.get("admin_user") or "").strip()
        if ident and _identify("local", ident, display_name=ident, method="admin_session"):
            return True

    token = (session.get("auth_token") or "").strip()
    if not token:
        return False
    return _identify_oauth_token(token, method="introspect")


# ── Internals ────────────────────────────────────────────────────────

def _verify_api_key(raw_key: str) -> bool:
    """Validate a raw API key (``cpypi_...``) against the local store."""
    if not raw_key:
        return False
    mgr = current_app.extensions.get("api_key_manager")
    if mgr is None:
        return False
    info = mgr.validate(raw_key)
    if not info:
        return False

    extra = {"key_id": info.get("key_id"), "key_name": info.get("key_name")}

    # Preferred: the key records which account owns it.
    if info.get("user_id"):
        return _identify_existing(int(info["user_id"]), method="api_key", **extra)

    # Legacy key written before the users table existed: its ``created_by``
    # holds whatever identity string was in use at the time.
    ident = (info.get("sub") or "").strip()
    if not ident:
        return False
    return _identify("api_key", ident, display_name=ident, method="api_key", **extra)


def _identify_oauth_token(token: str, *, method: str = "introspect") -> bool:
    if not settings.auth.oauth2_introspect_url:
        return False
    info = introspect_token(token)
    if not info:
        return False
    external_id, display_name, email = identity_from_info(info)
    if not external_id:
        logger.warning("Introspection response carried no usable identity: %r", info)
        return False
    return _identify(
        "oauth2", external_id,
        display_name=display_name, email=email, method=method,
    )


# ── Method registry (used by the decorators) ─────────────────────────

AUTH_METHODS: dict[str, callable] = {
    "basic": try_basic,
    "api_key_basic": try_api_key_basic,
    "api_key": try_api_key,
    "bearer": try_bearer_oauth2,
    "session": try_session,
}
