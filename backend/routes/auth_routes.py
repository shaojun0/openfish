"""OAuth2 authentication routes.

After a successful callback the user lands back where they were *on the device
authorization page*: an unauthenticated visitor to ``/device`` is sent here with
the pending ``?user_code=…`` (see ``routes/device.py``), and that code is kept in
the session so it survives the round trip through the identity provider.

No redirect target is ever assembled from a request argument.  ``?next=`` only
selects a landing key from a closed set and ``url_for`` builds the URL from
constants, so the open-redirect question is settled by construction rather than
by a same-origin check a reader has to trust.  The console keeps its own deep
link client-side; a ``?next=`` naming anything but ``/device`` lands on the
index.

When no identity provider is configured the same URL falls back to an HTTP
Basic challenge, so a Basic-only deployment has a working sign-in path too.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

from flask import Blueprint, jsonify, redirect, request, session, url_for

from auth.oauth import get_authorize_url, exchange_code
from config import settings
from routes.session import identify

auth_router = Blueprint("auth", __name__)

#: The only non-SPA landing a login round trip may return to.  It carries a
#: ``user_code`` that has to survive the provider hop; :func:`_landing`
#: re-attaches it with ``url_for``, so the browser never names the URL.
_DEVICE_LANDING = "device.authorize"


def _remember_landing(target: str | None) -> None:
    """Reduce ``?next=`` to a landing key plus device-flow data.

    The request supplies a *key*, never a URL: only a path of ``/device``
    selects anything other than the SPA index, and the only value carried over
    is the ``user_code``, which is re-encoded as a query argument of a fixed
    route.  That is the whole open-redirect defence — nothing here rests on a
    reviewer trusting a same-origin check.
    """
    parts = urlsplit(target or "")
    device = parts.path.rstrip("/") == "/device"
    session["login_landing"] = _DEVICE_LANDING if device else ""
    session["login_user_code"] = (
        (parse_qs(parts.query).get("user_code") or [""])[0] if device else ""
    )


def _landing() -> str:
    """Build the post-login ``Location`` from stored keys and constants."""
    landing = session.pop("login_landing", "")
    user_code = session.pop("login_user_code", "")
    if landing != _DEVICE_LANDING:
        return url_for("spa.index")
    if user_code:
        return url_for(_DEVICE_LANDING, user_code=user_code)
    return url_for(_DEVICE_LANDING)


@auth_router.route("/auth/login")
def auth_login():
    _remember_landing(request.args.get("next"))

    # OAuth2 is the preferred flow when an authorization endpoint is set.
    if settings.auth.oauth2_authorize_url.strip():
        return redirect(get_authorize_url())

    # No identity provider.  If the browser has already answered a Basic
    # challenge, it resent the credentials with this request — finish the
    # round trip by sending the user where they wanted to go.
    if identify()[0] is not None:
        return redirect(_landing())

    if settings.auth.basic_username and settings.auth.basic_password:
        resp = jsonify({"error": "Authentication required"})
        resp.status_code = 401
        resp.headers["WWW-Authenticate"] = 'Basic realm="cpypiserver"'
        return resp

    return jsonify({"error": "No authentication method is configured"}), 503


@auth_router.route("/auth")
def auth_callback():
    code = request.args.get("code")
    if not code:
        return "<h1>Bad request</h1><p>Missing authorization code.</p>", 400

    token_data = exchange_code(code)
    if not token_data:
        return "<h1>Login failed</h1><p>Failed to exchange authorization code.</p>", 400

    session["auth_token"] = token_data["access_token"]
    session["auth_user"] = token_data.get("sub", "")

    return redirect(_landing())


@auth_router.route("/auth/logout")
def auth_logout():
    session.clear()
    return redirect(url_for("spa.index"))
