"""Admin statistics — system-wide aggregates.

The HTML dashboard that used to live here is now part of the Vue SPA
(``frontend/src/views/AdminView.vue``).  This module only serves JSON, under
``/api/v1/admin``.
"""

from __future__ import annotations

import logging

from flask import Blueprint, current_app, jsonify

from auth.decorators import require_admin
from services.stats import compute as compute_stats

admin_bp = Blueprint("admin", __name__)
_log = logging.getLogger("cpypiserver.admin")


@admin_bp.route("/stats")
@require_admin
def stats():
    cache = current_app.extensions.get("caching")
    if cache is not None:
        data = cache.get("admin_stats")
        if data is not None:
            return jsonify(data)
    _log.warning("Stats cache miss — computing live")
    return jsonify(_compute())


@admin_bp.route("/refresh-stats", methods=["POST"])
@require_admin
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
