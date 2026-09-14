"""OAuth2 authentication routes."""

from flask import Blueprint, redirect, request, session, url_for

from auth.oauth import get_authorize_url, exchange_code

auth_router = Blueprint("auth", __name__)


@auth_router.route("/auth/login")
def auth_login():
    return redirect(get_authorize_url())


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
    return redirect(url_for("api_keys.dashboard"))


@auth_router.route("/auth/logout")
def auth_logout():
    session.clear()
    return redirect(url_for("api_keys.dashboard"))
