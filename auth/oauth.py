"""OAuth2 helpers — introspection, authorize URL, code exchange."""

from __future__ import annotations

import urllib.parse
from typing import Optional
from urllib.parse import urlencode

import requests
from requests.auth import HTTPBasicAuth
from flask import g

from config import settings


def set_user_from_info(info: dict) -> None:
    """Populate g.auth_user from an introspection response."""
    email = info.get("email", "")
    stuff_name = info.get("stuffName", "")
    if "@" in email:
        user_id = email.split("@")[0]
        sub = f"{stuff_name}[{user_id}]" if stuff_name else user_id
    else:
        sub = stuff_name or email

    g.auth_user = {"sub": sub, "role": _role_for(sub)}
    g.auth_method = "introspect"


def introspect_token(token: str) -> Optional[dict]:
    """Validate an OAuth2 access token against the introspection endpoint."""
    url = settings.auth.oauth2_introspect_url
    if not url:
        return None
    try:
        params = {"productId": settings.auth.oauth2_product_id}
        headers = {"authorization": f"Bearer {token}"}
        resp = requests.get(
            f"{url}?{urllib.parse.urlencode(params)}",
            headers=headers, verify=False, timeout=(5, 10),
        )
        resp.raise_for_status()
        return resp.json()
    except Exception:
        return None


def get_authorize_url() -> str:
    """Build the OAuth2 authorization URL for browser redirect."""
    params: dict = {"client_id": settings.auth.oauth2_client_id, "response_type": "code"}
    if settings.auth.is_4a and settings.auth.oauth2_auth_preference:
        params["auth-preference"] = settings.auth.oauth2_auth_preference
    return f"{settings.auth.oauth2_authorize_url.strip()}?{urlencode(params)}"


def exchange_code(code: str) -> dict | None:
    token_url = settings.auth.oauth2_token_url.strip()
    if not token_url:
        return None

    try:
        payload = f'grant_type=authorization_code&code={code}'
        headers = {'Content-Type': 'application/x-www-form-urlencoded'}
        resp = requests.post(token_url, data=payload, headers=headers,
                                 auth=HTTPBasicAuth(settings.auth.oauth2_client_id, settings.auth.oauth2_client_secret), verify=False)
        if resp.status_code == 200:
            data = resp.json()
            return data
    except Exception as exc:
        raise exc
    return None


def _role_for(identifier: str) -> str:
    """Map an introspected subject to a role (see `auth.permissions.role_for`)."""
    from auth.permissions import role_for

    return role_for(identifier)
