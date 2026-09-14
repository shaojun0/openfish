"""API key management — CRUD for Bearer tokens.

The HTML dashboard that used to live here is now part of the Vue SPA
(``frontend/src/views/ApiKeysView.vue``).  This module only serves JSON, under
``/api/v1``.
"""

from flask import Blueprint, current_app, g, jsonify, request, session

from errors import UnauthorizedError

api_keys_bp = Blueprint("api_keys", __name__)


@api_keys_bp.route("/keys", methods=["GET"])
def list_keys():
    _require_auth()
    mgr = current_app.extensions["api_key_manager"]
    keys = mgr.list_keys(created_by=_current_user())
    for k in keys:
        k["stats_detail"] = mgr.get_key_stats(k["id"])
    return jsonify(keys)


@api_keys_bp.route("/keys", methods=["POST"])
def create_key():
    _require_auth()
    data = request.get_json(silent=True) or {}
    name = str(data.get("name", "")).strip()
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


@api_keys_bp.route("/keys/<key_id>", methods=["DELETE"])
def delete_key(key_id: str):
    _require_auth()
    mgr = current_app.extensions["api_key_manager"]
    if not mgr.delete_key(key_id):
        return jsonify({"error": "key not found"}), 404
    return jsonify({"deleted": key_id})


@api_keys_bp.route("/keys/<key_id>/stats", methods=["GET"])
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


def _require_auth() -> None:
    """Defence in depth — the blueprint also installs a before_request guard."""
    if _current_user() is None:
        raise UnauthorizedError()
