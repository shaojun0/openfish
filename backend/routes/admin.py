"""Admin statistics — system-wide aggregates.

The HTML dashboard that used to live here is now part of the Vue SPA
(``frontend/src/views/AdminView.vue``).  This module only serves JSON, under
``/api/v1/admin``.
"""

from __future__ import annotations

import logging

from flask import Blueprint, current_app, jsonify

from auth.decorators import require_admin, require_permission
from auth.permissions import ADMIN_REFRESH
from openapi import api_operation, errors, ok
from services.stats import compute as compute_stats

admin_bp = Blueprint("admin", __name__)
logger = logging.getLogger("cpypiserver.admin")


@admin_bp.route("/stats")
@require_admin
@api_operation(
    summary="Server-wide statistics",
    description=(
        "Aggregated counters plus per-package and per-key rankings. Served from "
        "a cache that a background thread refreshes periodically, so the numbers "
        "can lag by up to one refresh interval — call `POST /refresh-stats` "
        "first when you need them to be current."
    ),
    tags=["Administration"],
    responses={"200": ok("Aggregated statistics", "AdminStats"), **errors("401", "403", "500")},
)
def stats():
    cache = current_app.extensions.get("caching")
    if cache is not None:
        data = cache.get("admin_stats")
        if data is not None:
            return jsonify(data)
    logger.warning("Stats cache miss — computing live")
    return jsonify(_compute())


@admin_bp.route("/refresh-stats", methods=["POST"])
@require_permission(ADMIN_REFRESH)
@api_operation(
    summary="Recompute statistics",
    description="Forces a synchronous recomputation and replaces the cached value.",
    tags=["Administration"],
    responses={
        "200": ok("Statistics recomputed"),
        **errors("401", "403", "503"),
    },
)
def refresh_stats():
    cache = current_app.extensions.get("caching")
    if cache is None:
        return jsonify({"refreshed": False, "error": "cache not available"}), 503
    cache.set("admin_stats", _compute(), timeout=0)
    return jsonify({"refreshed": True})


def _compute() -> dict:
    pkg_index = current_app.extensions.get("pypi_index")
    key_mgr = current_app.extensions.get("api_key_manager")
    return compute_stats(pkg_index, key_mgr)
