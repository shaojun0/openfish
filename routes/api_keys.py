"""API key management dashboard — CRUD for Bearer tokens."""

from flask import (
    Blueprint, current_app, g, jsonify, redirect,
    render_template_string, request, session, url_for,
)

from config import settings
from auth.oauth import get_authorize_url
from auth.permissions import Permission, has_permission
from auth.decorators import _get_role
from errors import UnauthorizedError
from services import templates

api_keys_bp = Blueprint("api_keys", __name__)


@api_keys_bp.route("/")
def dashboard():
    user = _current_user()
    if user is None:
        if settings.auth.auth_enabled:
            return redirect(get_authorize_url())
        user = "anonymous"
    mgr = current_app.extensions["api_key_manager"]
    keys = mgr.list_keys(created_by=user)
    return render_template_string(
        templates.api_keys(),
        server_name=settings.server.server_name,
        user=user,
        keys=keys,
        auth_enabled=settings.auth.auth_enabled,
        is_admin=_is_admin(),
        base_url=url_for("api_keys.dashboard", _external=True).rstrip("/"),
    )


@api_keys_bp.route("/api/keys", methods=["GET"])
def list_keys():
    _require_auth()
    mgr = current_app.extensions["api_key_manager"]
    keys = mgr.list_keys(created_by=_current_user())
    for k in keys:
        k["stats_detail"] = mgr.get_key_stats(k["id"])
    return jsonify(keys)


@api_keys_bp.route("/api/keys", methods=["POST"])
def create_key():
    _require_auth()
    data = request.get_json(silent=True) or {}
    name = data.get("name", "").strip()
    if not name:
        return jsonify({"error": "name is required"}), 400
    expires = data.get("expires_in_days")
    if expires is not None:
        try:
            expires = int(expires)
            if expires <= 0:
                expires = None
        except (ValueError, TypeError):
            expires = None
    mgr = current_app.extensions["api_key_manager"]
    result = mgr.create_key(name=name, created_by=_current_user(), expires_in_days=expires)
    return jsonify(result), 201


@api_keys_bp.route("/api/keys/<key_id>", methods=["DELETE"])
def delete_key(key_id: str):
    _require_auth()
    mgr = current_app.extensions["api_key_manager"]
    if not mgr.delete_key(key_id):
        return jsonify({"error": "key not found"}), 404
    return jsonify({"deleted": key_id})


@api_keys_bp.route("/api/keys/<key_id>/stats", methods=["GET"])
def key_stats(key_id: str):
    _require_auth()
    mgr = current_app.extensions["api_key_manager"]
    return jsonify(mgr.get_key_stats(key_id))


# ── Helpers ──────────────────────────────────────────────────────────

def _current_user() -> str | None:
    user = getattr(g, "auth_user", None)
    if user:
        return user.get("sub", str(user)) if isinstance(user, dict) else str(user)
    token = session.get("auth_token", "")
    return f"session:{token[:8]}…" if token else None


def _is_admin() -> bool:
    return has_permission(_get_role(), Permission.ADMIN_VIEW)


def _require_auth() -> None:
    if _current_user() is None:
        raise UnauthorizedError()
