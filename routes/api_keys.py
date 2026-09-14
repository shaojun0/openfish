"""API key management — CRUD for Bearer tokens.

The HTML dashboard that used to live here is now part of the Vue SPA
(``frontend/src/views/ApiKeysView.vue``).  This module only serves JSON, under
``/api/v1``.
"""

from flask import Blueprint, current_app, g, jsonify, request, session

from errors import UnauthorizedError
from openapi import api_operation, array_of, errors, json_body, ok

api_keys_bp = Blueprint("api_keys", __name__)


@api_keys_bp.route("/keys", methods=["GET"])
@api_operation(
    summary="List API keys",
    description=(
        "Keys created by the calling subject, newest first, each with its usage "
        "breakdown inlined as `stats_detail`. The raw secret is never included — "
        "only a short `prefix` for identification."
    ),
    tags=["API keys"],
    responses={
        "200": ok("API keys owned by the caller", array_of("ApiKey")),
        **errors("401", "403", "500"),
    },
)
def list_keys():
    _require_auth()
    mgr = current_app.extensions["api_key_manager"]
    keys = mgr.list_keys(created_by=_current_user())
    for k in keys:
        k["stats_detail"] = mgr.get_key_stats(k["id"])
    return jsonify(keys)


@api_keys_bp.route("/keys", methods=["POST"])
@api_operation(
    summary="Create an API key",
    description=(
        "Mints a new key. **The `key` field in the response is the only time the "
        "secret is ever transmitted** — the server stores just its SHA-256, so "
        "capture it immediately.\n\n"
        "Use the key as `Authorization: Bearer <key>`, or as the password of an "
        "HTTP Basic request with the username `__token__` for pip/twine."
    ),
    tags=["API keys"],
    request_body={"required": True, "content": json_body("CreateKeyRequest")},
    responses={
        "201": ok("Key created — copy the `key` field now", "CreatedApiKey"),
        **errors("400", "401", "403", "500"),
    },
)
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
@api_operation(
    summary="Revoke an API key",
    description=(
        "Permanently deletes the key. Any client still using it loses access "
        "immediately; this cannot be undone."
    ),
    tags=["API keys"],
    responses={
        "200": ok("Key revoked", "DeleteResult"),
        **errors("401", "403", "404", "500"),
    },
)
def delete_key(key_id: str):
    _require_auth()
    mgr = current_app.extensions["api_key_manager"]
    if not mgr.delete_key(key_id):
        return jsonify({"error": "key not found"}), 404
    return jsonify({"deleted": key_id})


@api_keys_bp.route("/keys/<key_id>/stats", methods=["GET"])
@api_operation(
    summary="Per-key usage breakdown",
    description=(
        "Download and upload counters for one key, totalled and split by package."
    ),
    tags=["API keys"],
    responses={
        "200": ok("Usage for this key", "KeyStats"),
        **errors("401", "403", "500"),
    },
)
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
