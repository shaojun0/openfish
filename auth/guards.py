"""Auth guards — individual `_try_*` functions.

Each function tries one authentication method.  On success it sets:
  - ``g.auth_user`` — dict with at least ``{"sub": str, "role": str}``
  - ``g.auth_method`` — string identifying the method used
"""

from __future__ import annotations

import logging
from typing import Optional

from flask import current_app, g, request, session

from config import settings
from auth.oauth import introspect_token, set_user_from_info

logger = logging.getLogger("cpypiserver.auth")


def _bearer_token() -> str:
    """Extract raw Bearer token from Authorization header."""
    h = request.headers.get("Authorization", "")
    if h.startswith("Bearer "):
        return h[7:].strip()
    return ""


def _verify_api_key(raw_key: str) -> bool:
    """Validate a raw API key (cpypi_...) against local store."""
    mgr = current_app.extensions.get("api_key_manager")
    if mgr is None:
        return False
    user = mgr.validate(raw_key)
    if user:
        g.auth_user = user
        g.auth_method = "api_key"
        return True
    return False


def try_basic() -> bool:
    """HTTP Basic Auth from config.

    Disabled unless BOTH username and password are configured, so an
    unset (empty) configuration can never be matched by ``":"``.
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
        g.auth_user = {"sub": auth.username, "role": _role_for(auth.username)}
        g.auth_method = "basic"
        return True
    return False


def try_api_key() -> bool:
    """Bearer API key from Authorization header."""
    return _verify_api_key(_bearer_token())


def try_api_key_basic() -> bool:
    """HTTP Basic with username=__token__ (twine-compatible)."""
    auth = request.authorization
    if auth is None or auth.username != "__token__":
        return False
    return _verify_api_key(auth.password)


def try_bearer_oauth2() -> bool:
    """Bearer OAuth2 access token (introspection)."""
    token = _bearer_token()
    if not token:
        return False
    if settings.auth.oauth2_introspect_url:
        info = introspect_token(token)
        if info:
            set_user_from_info(info)
            return True
    return False


def try_session() -> bool:
    """Session cookie (admin dashboard or OAuth2 login).

    Priority:
    1. Admin dashboard session
    2. OAuth2 auth_token from /auth/* login flow
    """
    if session.get("admin_authenticated"):
        g.auth_user = {
            "sub": session.get("admin_user", "admin"),
            "role": "admin",
        }
        g.auth_method = "admin_session"
        return True

    token = session.get("auth_token", "")
    if not token.strip():
        return False
    if settings.auth.oauth2_introspect_url:
        info = introspect_token(token.strip())
        if info:
            set_user_from_info(info)
            return True
    return False


# ── Role resolution ──────────────────────────────────────────────────


def _role_for(identifier: str) -> str:
    """Map a user identifier to its role."""
    from config import settings
    if settings.auth.basic_username and identifier == settings.auth.basic_username:
        return "admin"
    if identifier in settings.server.admin_users:
        return "admin"
    return "authenticated"


# ── Method registry (used by decorator) ──────────────────────────────

AUTH_METHODS: dict[str, callable] = {
    "basic": try_basic,
    "api_key_basic": try_api_key_basic,
    "api_key": try_api_key,
    "bearer": try_bearer_oauth2,
    "session": try_session,
}
