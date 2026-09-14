"""Admin dashboard — system statistics."""

import logging

from flask import Blueprint, current_app, g, jsonify, session

from config import settings
from auth.decorators import require_admin
from services.stats import compute as compute_stats
from services import templates

admin_bp = Blueprint("admin", __name__)
_log = logging.getLogger("cpypiserver.admin")


@admin_bp.route("/")
@require_admin
def dashboard():
    session["admin_authenticated"] = True
    session["admin_user"] = _display_name()
    return (
        templates.admin()
        .replace("{{ server_name }}", settings.server.server_name)
        .replace("{{ user }}", _display_name())
    )


@admin_bp.route("/stats")
@require_admin
def stats():
    cache = current_app.extensions.get("caching")
    if cache is not None:
        data = cache.get("admin_stats")
        if data is not None:
            return jsonify(data)
    _log.warning("Stats cache miss — computing live")
    pkg_index = current_app.extensions.get("pypi_index")
    key_mgr = current_app.extensions.get("api_key_manager")
    return jsonify(compute_stats(pkg_index, key_mgr))


@admin_bp.route("/refresh-stats", methods=["POST"])
@require_admin
def refresh_stats():
    cache = current_app.extensions.get("caching")
    if cache is None:
        return jsonify({"refreshed": False, "error": "cache not available"}), 503
    pkg_index = current_app.extensions.get("pypi_index")
    key_mgr = current_app.extensions.get("api_key_manager")
    data = compute_stats(pkg_index, key_mgr)
    cache.set("admin_stats", data, timeout=0)
    return jsonify({"refreshed": True})


def _display_name() -> str:
    u = getattr(g, "auth_user", None)
    if u is None:
        return "unknown"
    return u.get("sub", str(u)) if isinstance(u, dict) else str(u)
