"""Blueprint registration — auth policies and URL prefixes.

Called once from app.py after all extensions are inited.
"""

from config import settings
from auth.decorators import require_auth, require_permission
from auth.permissions import Permission


def register_all(app):
    """Register all blueprints with auth guards."""
    from routes.health import health_bp
    from routes.auth_routes import auth_router
    from routes.pypi import pypi_bp
    from routes.api_keys import api_keys_bp
    from routes.admin import admin_bp
    from routes.python_build import python_build_bp

    prefix = settings.server.route_prefix

    # ── Auth guards (MUST be set BEFORE register_blueprint) ─────────
    api_keys_bp.before_request(require_auth(methods=["session", "api_key", "api_key_basic", "bearer"]))
    pypi_bp.before_request(require_auth())
    admin_bp.before_request(require_permission(Permission.ADMIN_VIEW))

    # ── Register ────────────────────────────────────────────────────
    app.register_blueprint(health_bp)
    app.register_blueprint(api_keys_bp, url_prefix=prefix)
    app.register_blueprint(auth_router, url_prefix=prefix)
    app.register_blueprint(pypi_bp, url_prefix=prefix)
    app.register_blueprint(python_build_bp, url_prefix=prefix)
    app.register_blueprint(admin_bp, url_prefix=prefix + "/admin")
