"""Health check."""

from flask import Blueprint, current_app, jsonify

from config import settings

health_bp = Blueprint("health", __name__)


@health_bp.route("/health")
def health():
    packages = current_app.extensions["pypi_index"].get_snapshot()
    return jsonify({
        "status": "ok",
        "server": settings.server.server_name,
        "package_count": len(packages),
        "packages_dir": settings.storage.packages_dir,
    })
