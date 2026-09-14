"""Blueprint registration — URL prefixes and auth policies.

Called once from app.py after all extensions are inited.

Layout
------
``/api/v1/*``         JSON contract consumed by the Vue SPA and by API-key clients
``/simple/*``         PEP 503 / PEP 691 — consumed by pip, uv, twine
``/python-builds/*``  uv CPython mirror
``/auth/*``           OAuth2 login flow
``/*``                the SPA shell (see ``routes/spa.py``)
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
    from routes.session import session_bp
    from routes.spa import spa_bp

    prefix = settings.server.route_prefix
    api = prefix + "/api/v1"

    # ── Auth guards (MUST be set BEFORE register_blueprint) ─────────
    # `basic` is included so the SPA keeps working in a deployment that
    # authenticates with HTTP Basic instead of OAuth2.
    api_keys_bp.before_request(
        require_auth(methods=["session", "basic", "api_key", "api_key_basic", "bearer"])
    )
    admin_bp.before_request(require_permission(Permission.ADMIN_VIEW))
    pypi_bp.before_request(require_auth())

    # ── Machine-facing endpoints ────────────────────────────────────
    app.register_blueprint(health_bp)
    app.register_blueprint(pypi_bp, url_prefix=prefix)
    app.register_blueprint(python_build_bp, url_prefix=prefix)
    app.register_blueprint(auth_router, url_prefix=prefix)

    # ── JSON API consumed by the SPA ────────────────────────────────
    app.register_blueprint(session_bp, url_prefix=api)
    app.register_blueprint(api_keys_bp, url_prefix=api)
    app.register_blueprint(admin_bp, url_prefix=api + "/admin")

    # ── SPA shell. Machine routes above win by rule specificity, so this
    #    only ever handles browser-facing URLs.
    app.register_blueprint(spa_bp, url_prefix=prefix)
