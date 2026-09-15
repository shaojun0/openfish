"""OAuth2 helpers — introspection, authorize URL, code exchange.

Identity extraction lives in :func:`identity_from_info` and is deliberately
picky, because this is where the previous implementation went wrong: it used
the *display name* as the account key, so renaming a person in the corporate
directory silently orphaned their roles.
"""

from __future__ import annotations

import logging
import urllib.parse
from urllib.parse import urlencode

import requests
from requests.auth import HTTPBasicAuth

from config import settings

logger = logging.getLogger("cpypiserver.oauth")


def _verify() -> str | bool:
    """TLS verification argument for outbound OAuth2 calls.

    Returns a CA bundle path when one is configured, otherwise ``True`` (the
    system trust store).  It never returns ``False``: disabling verification
    on an introspection call means anyone able to intercept the connection can
    fabricate an identity and log in as anybody.
    """
    bundle = (settings.auth.oauth2_ca_bundle or "").strip()
    return bundle or True


def identity_from_info(info: dict) -> tuple[str, str, str | None]:
    """Derive ``(external_id, display_name, email)`` from an introspection body.

    ``external_id`` is the stable account key and is what roles attach to.  The
    corporate login (the local part of the e-mail address) is preferred because
    it survives a rename; ``stuffName`` is only ever the display name.
    """
    email = (info.get("email") or "").strip()
    stuff_name = (info.get("stuffName") or "").strip()
    sub = (info.get("sub") or "").strip()

    if "@" in email:
        external_id = email.split("@", 1)[0].strip()
    else:
        external_id = email or sub or stuff_name

    display_name = stuff_name or external_id
    return external_id, display_name, (email or None)


def introspect_token(token: str) -> dict | None:
    """Validate an OAuth2 access token against the introspection endpoint."""
    url = settings.auth.oauth2_introspect_url
    if not url:
        return None
    try:
        params = {"productId": settings.auth.oauth2_product_id}
        headers = {"authorization": f"Bearer {token}"}
        resp = requests.get(
            f"{url}?{urllib.parse.urlencode(params)}",
            headers=headers, verify=_verify(), timeout=(5, 10),
        )
        resp.raise_for_status()
        return resp.json()
    except Exception:
        logger.warning("Token introspection failed", exc_info=True)
        return None


def get_authorize_url() -> str:
    """Build the OAuth2 authorization URL for a browser redirect.

    Returns an empty string when OAuth2 is not configured, so callers can fall
    back to a plain 401 instead of redirecting to a meaningless ``?`` URL.
    """
    base = (settings.auth.oauth2_authorize_url or "").strip()
    if not base:
        return ""
    params: dict = {
        "client_id": settings.auth.oauth2_client_id,
        "response_type": "code",
    }
    if settings.auth.is_4a and settings.auth.oauth2_auth_preference:
        params["auth-preference"] = settings.auth.oauth2_auth_preference
    return f"{base}?{urlencode(params)}"


def exchange_code(code: str) -> dict | None:
    token_url = (settings.auth.oauth2_token_url or "").strip()
    if not token_url:
        return None

    try:
        payload = f"grant_type=authorization_code&code={code}"
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        resp = requests.post(
            token_url, data=payload, headers=headers,
            auth=HTTPBasicAuth(
                settings.auth.oauth2_client_id, settings.auth.oauth2_client_secret
            ),
            verify=_verify(),
        )
        if resp.status_code == 200:
            return resp.json()
        logger.warning("Code exchange failed: HTTP %s", resp.status_code)
    except Exception as exc:
        logger.warning("Code exchange failed: %s", exc)
        raise
    return None
