"""Blueprint registration — URL prefixes and auth policies.

Called once from app.py after all extensions are inited.

Layout
------
``/api/v1/*``         JSON contract consumed by the Vue SPA and by API-key clients
``/simple/*``         PEP 503 / PEP 691 — consumed by pip, uv, twine
``/python-builds/*``  uv CPython mirror
``/tools/*``          tool index + downloads (``/api/v1/tools`` for JSON)
``/npm/*``            npm index, ``/-/all``, ``/-/ping`` + tarball downloads
``/docker/*``         docker index, ``/v2/_catalog`` + image tarball downloads
``/debian/*``         debian index, flat ``Packages`` + .deb downloads
``/openapi.json``     OpenAPI 3.1 description; ``/docs`` and ``/llms.txt`` alongside
``/auth/*``           OAuth2 login flow
``/*``                the SPA shell (see ``routes/spa.py``)
"""

from config import settings
from auth.decorators import require_auth, require_permission
from auth.permissions import ADMIN_ROLES, ADMIN_VIEW


def register_all(app):
    """Register all blueprints with auth guards."""
    from routes.health import health_bp
    from routes.auth_routes import auth_router
    from routes.pypi import pypi_bp
    from routes.api_keys import api_keys_bp
    from routes.admin import admin_bp
    from routes.python_build import python_build_bp
    from routes.session import session_bp
    from routes.discovery import discovery_bp
    from routes.spa import spa_bp
    from routes.access import access_bp
    from routes.hub import hub_bp
    from routes.npm import npm_bp
    from routes.docker import docker_bp
    from routes.debian import debian_bp

    prefix = settings.server.route_prefix
    api = prefix + "/api/v1"

    # ── Auth guards (MUST be set BEFORE register_blueprint) ─────────
    # `basic` is included so the SPA keeps working in a deployment that
    # authenticates with HTTP Basic instead of OAuth2.
    api_keys_bp.before_request(
        require_auth(methods=["session", "basic", "api_key", "api_key_basic", "bearer"])
    )
    admin_bp.before_request(require_permission(ADMIN_VIEW))
    # Access control is a strictly narrower grant than "view the dashboard":
    # holding admin:view does not let you edit roles.
    access_bp.before_request(require_permission(ADMIN_ROLES))
    pypi_bp.before_request(require_auth())
    # `python_build_bp` intentionally has no blueprint-wide guard: its
    # `/python-builds/health` endpoint is a public mirror probe.  The other
    # routes there carry `@require_auth()` decorators, and
    # `scripts/check_auth_guards.py` fails the build if one of them is ever
    # written above its `@route` decorator and silently stops running.

    # ── Machine-facing endpoints ────────────────────────────────────
    app.register_blueprint(health_bp)
    app.register_blueprint(pypi_bp, url_prefix=prefix)
    app.register_blueprint(python_build_bp, url_prefix=prefix)
    app.register_blueprint(auth_router, url_prefix=prefix)

    # ── API discovery: /openapi.json, /docs, /llms.txt, /.well-known/... ──
    # Anonymous by design: these publish the contract, never registry data.
    app.register_blueprint(discovery_bp, url_prefix=prefix)

    # ── JSON API consumed by the SPA ────────────────────────────────
    app.register_blueprint(session_bp, url_prefix=api)
    app.register_blueprint(api_keys_bp, url_prefix=api)
    app.register_blueprint(admin_bp, url_prefix=api + "/admin")
    app.register_blueprint(access_bp, url_prefix=api + "/admin")

    # ── Artifact hub: one blueprint per ecosystem ───────────────────
    # Each carries both the JSON catalog under `/api/v1/*` and the
    # machine-facing protocol routes of its ecosystem, so all four are
    # registered at the bare prefix and declare their full paths internally.
    # Every view keeps its own `@require_permission` guard — there is no
    # blueprint-wide guard, and `scripts/check_auth_guards.py` fails the build
    # if one is ever dropped or written above its `@route` decorator.
    app.register_blueprint(hub_bp, url_prefix=prefix)
    app.register_blueprint(npm_bp, url_prefix=prefix)
    app.register_blueprint(docker_bp, url_prefix=prefix)
    app.register_blueprint(debian_bp, url_prefix=prefix)

    # ── SPA shell. Machine routes above win by rule specificity, so this
    #    only ever handles browser-facing URLs.
    app.register_blueprint(spa_bp, url_prefix=prefix)
