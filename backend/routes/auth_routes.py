"""OAuth2 authentication routes.

After a successful callback the user lands back where they were: the SPA sends
``?next=<path>`` when it redirects here, and the value is stored in the session
so it survives the round trip through the identity provider.

When no identity provider is configured the same URL falls back to an HTTP
Basic challenge, so a Basic-only deployment has a working sign-in path too.
"""

from flask import Blueprint, jsonify, redirect, request, session, url_for

from auth.oauth import get_authorize_url, exchange_code
from config import settings
from routes.session import identify

auth_router = Blueprint("auth", __name__)


def _safe_next(target: str | None) -> str | None:
    """Only allow same-origin, absolute-path redirects.

    Rejects ``//evil.example`` and ``https://evil.example`` so the ``next``
    parameter cannot be turned into an open redirect.
    """
    if not target or not target.startswith("/") or target.startswith("//"):
        return None
    return target


def _landing(target: str | None) -> str:
    return _safe_next(target) or url_for("spa.index")


@auth_router.route("/auth/login")
def auth_login():
    nxt = _safe_next(request.args.get("next"))
    if nxt:
        session["login_next"] = nxt

    # OAuth2 is the preferred flow when an authorization endpoint is set.
    if settings.auth.oauth2_authorize_url.strip():
        return redirect(get_authorize_url())

    # No identity provider.  If the browser has already answered a Basic
    # challenge, it resent the credentials with this request — finish the
    # round trip by sending the user where they wanted to go.
    if identify()[0] is not None:
        return redirect(_landing(session.pop("login_next", None)))

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

    target = _safe_next(request.args.get("next")) or session.pop("login_next", None)
    return redirect(_landing(target))


@auth_router.route("/auth/logout")
def auth_logout():
    session.clear()
    return redirect(url_for("spa.index"))
