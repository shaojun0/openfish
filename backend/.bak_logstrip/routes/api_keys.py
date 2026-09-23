"""API key management — CRUD for Bearer tokens.

The HTML dashboard that used to live here is now part of the Vue SPA
(``frontend/src/views/ApiKeysView.vue``).  This module only serves JSON, under
``/api/v1``.

``POST /keys`` binds its body as a view parameter (``body: CreateKeyRequest``)
rather than reading ``request.get_json`` by hand: the model is the one the
OpenAPI document has always advertised for this operation, and ``@validate_request()``
— kept *below* the blueprint's ``require_auth`` and the view's
``@require_permission``, so an unauthorized caller is answered ``401``/``403``
and never by the binder — is what actually enforces it.
"""

from __future__ import annotations

from flask import current_app, jsonify
from flask_openapi3 import APIBlueprint, validate_request

from auth.decorators import current_sub, current_user_id, require_permission
from auth.permissions import KEY_CREATE, KEY_DELETE, KEY_LIST, KEY_STATS
from errors import BadRequestError
from openapi import api_operation, array_of, errors, json_body, ok
from schemas import CreateKeyRequest

api_keys_bp = APIBlueprint("api_keys", __name__)


@api_keys_bp.route("/keys", methods=["GET"])
@require_permission(KEY_LIST)
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
    mgr = current_app.extensions["api_key_manager"]
    keys = mgr.list_keys(user_id=current_user_id(), created_by=current_sub())
    for k in keys:
        k["stats_detail"] = mgr.get_key_stats(k["id"])
    return jsonify(keys)


@api_keys_bp.post("/keys")
@require_permission(KEY_CREATE)
@validate_request()
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
def create_key(body: CreateKeyRequest):
    # `body` is bound and validated by flask-openapi3 from `CreateKeyRequest`, so
    # neither the missing-field case nor the type of `expires_in_days` is this
    # view's business any more.  Only the whitespace rule is left: `"   "`
    # satisfies `min_length=1` and would otherwise mint an unnamed key.
    name = body.name.strip()
    if not name:
        raise BadRequestError("name is required")

    expires = body.expires_in_days
    if expires is not None and expires <= 0:
        expires = None

    mgr = current_app.extensions["api_key_manager"]
    result = mgr.create_key(
        name=name,
        created_by=current_sub() or "unknown",
        expires_in_days=expires,
        user_id=current_user_id(),
    )
    return jsonify(result), 201


@api_keys_bp.route("/keys/<key_id>", methods=["DELETE"])
@require_permission(KEY_DELETE)
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
    mgr = current_app.extensions["api_key_manager"]
    if not mgr.delete_key(key_id):
        return jsonify({"error": "key not found"}), 404
    return jsonify({"deleted": key_id})


@api_keys_bp.route("/keys/<key_id>/stats", methods=["GET"])
@require_permission(KEY_STATS)
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
    mgr = current_app.extensions["api_key_manager"]
    return jsonify(mgr.get_key_stats(key_id))

