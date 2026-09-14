"""Health check."""

from flask import Blueprint, current_app, jsonify

from config import settings
from openapi import api_operation, errors, ok

health_bp = Blueprint("health", __name__)


@health_bp.route("/health")
@api_operation(
    summary="Liveness probe",
    description=(
        "Reports the server name, the number of indexed packages and the package "
        "directory. Needs no credentials and does no work beyond reading an "
        "in-memory index, so it doubles as an orchestrator health check."
    ),
    tags=["Session"],
    security=[],
    responses={"200": ok("The server is up", "HealthInfo"), **errors("500")},
)
def health():
    packages = current_app.extensions["pypi_index"].get_snapshot()
    return jsonify({
        "status": "ok",
        "server": settings.server.server_name,
        "package_count": len(packages),
        "packages_dir": settings.storage.packages_dir,
    })
