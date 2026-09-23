"""Blueprint registration — URL prefixes and auth policies.

Called once from app.py after all extensions are inited.

Layout
------
``/api/v1/*``         JSON contract consumed by the Vue SPA and by API-key clients
``/simple/*``         PEP 503 / PEP 691 — consumed by pip, uv, twine
``/python-builds/*``  uv CPython mirror
``/node-builds/*``    nvm/fnm/node-gyp Node.js mirror
``/tools/*``          tool index + downloads (``/api/v1/tools`` for JSON)
``/npm/*``            npm index, ``/-/all``, ``/-/ping`` + tarball downloads
``/docker/*``         docker index, ``/v2/_catalog`` + image tarball downloads
``/debian/*``         debian index, flat ``Packages`` + .deb downloads
``/docs/<eco>/*``     per-ecosystem Markdown documentation — every URL needs ``doc:read``
``/documentation/<eco>`` the SPA page that browses it (behind ``app:read``, one namespace over)
``/certs/ca_chain.pem`` the private CA chain, so clients can trust the mirror
``/static/dist/*``    the compiled Vue bundle — served only with ``app:read``
``/openapi.json``     OpenAPI 3.1 description; ``/docs`` and ``/llms.txt`` alongside
``/auth/*``           OAuth2 login flow
``/*``                the SPA shell — needs ``app:read`` (see ``routes/spa.py``)

Authorization policy, in one line: **anonymous is docs and nothing else.**  The
``anonymous`` role holds ``doc:read`` alone, and the console is gated by
``app:read`` rather than by ``require_auth`` precisely so that turning
``AUTH_ENABLED`` off cannot open the UI — see the guard block in
:func:`register_all`.
"""

from __future__ import annotations

from config import settings
from auth.decorators import require_auth, require_permission
from auth.permissions import (
    ADMIN_ROLES,
    ADMIN_VIEW,
    AGENT_RUN,
    APP_READ,
    FINDING_READ,
    REPO_READ,
)


def register_all(app):
    """Register all blueprints with auth guards."""
    from routes.health import health_bp
    from routes.auth_routes import auth_router
    from routes.pypi import pypi_bp
    from routes.api_keys import api_keys_bp
    from routes.admin import admin_bp
    from routes.python_build import python_build_bp
    from routes.node_build import node_build_bp
    from routes.session import session_bp
    from routes.discovery import discovery_bp
    from routes.spa import spa_bp
    from routes.access import access_bp
    from routes.hub import hub_bp
    from routes.npm import npm_bp
    from routes.docker import docker_bp
    from routes.debian import debian_bp
    from routes.docs import docs_bp
    from routes.certs import certs_bp
    from routes.device import device_bp
    from routes.repos import repo_bp
    from routes.repo_context import repo_context_bp
    from routes.findings import findings_bp
    from routes.agent_tasks import agent_tasks_bp
    from routes.repo_webhook import repo_webhook_bp

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
    # The browser application is gated by its own point, not by `require_auth`.
    # That distinction matters: with `AUTH_ENABLED=false` no credential is
    # demanded, so `require_auth` would pass and the UI would open to an
    # anonymous visitor.  `app:read` — which the `anonymous` role does not hold,
    # and which every signed-in user gets — is what actually closes it.
    #
    # It gates the shell and its bundle only; every byte behind it is still
    # checked by that ecosystem's own point.  A browser that hits `/` without a
    # session gets a 401, which the error handler turns into the OAuth redirect
    # (or a Basic challenge) — see `extensions/error_handlers.py`.
    spa_bp.before_request(require_permission(APP_READ))
    # ── Agent Hub (S1–S4 routes) ────────────────────────────────────
    # Same convention as everywhere else: the blueprint-wide guard is the
    # *floor*, and the finer points are supplied per view (importing and
    # deciding carry `repo:write` / `finding:decide` / `policy:write` /
    # `repo:push` / `agent:admin` on their own routes).  A missing floor would
    # let a view without a decorator answer anonymously, which is exactly what
    # must never happen.
    repo_bp.before_request(require_permission(REPO_READ))
    repo_context_bp.before_request(require_permission(REPO_READ))
    findings_bp.before_request(require_permission(FINDING_READ))
    agent_tasks_bp.before_request(require_permission(AGENT_RUN))
    # `repo_webhook_bp` deliberately gets **no** guard here: Forgejo calls it
    # with no session, and its authentication is the shared-secret HMAC in
    # §5.4.  The anonymity is deliberate, never an oversight, and the reason
    # stays recorded here.
    # `python_build_bp` and `node_build_bp` intentionally have no
    # blueprint-wide guard: their `/*-builds/health` endpoints are public
    # mirror probes.  The other routes there carry `@require_permission`
    # decorators, and one written above its `@route` decorator would silently
    # stop running — that pattern is forbidden.

    # ── Machine-facing endpoints ────────────────────────────────────
    app.register_blueprint(health_bp)
    # `pypi_bp` is a flask-openapi3 APIBlueprint — its simple-index views bind
    # `?format=` through the library — so it goes through `register_api` (the
    # APIBlueprint-aware entry point) rather than `register_blueprint`.
    app.register_api(pypi_bp, url_prefix=prefix)
    app.register_blueprint(python_build_bp, url_prefix=prefix)
    app.register_blueprint(node_build_bp, url_prefix=prefix)
    app.register_blueprint(auth_router, url_prefix=prefix)

    # ── API discovery: /openapi.json, /docs, /llms.txt, /.well-known/... ──
    # Anonymous by design: these publish the contract, never registry data.
    app.register_blueprint(discovery_bp, url_prefix=prefix)

    # ── JSON API consumed by the SPA ────────────────────────────────
    app.register_blueprint(session_bp, url_prefix=api)
    # `api_keys_bp` and `access_bp` bind request bodies as view parameters
    # (`body: CreateKeyRequest`, `body: CreateRoleRequest`, …), so they go
    # through `register_api` like `pypi_bp`.  Their read-only routes stay plain
    # `@…_bp.route` views and are registered unchanged.
    app.register_api(api_keys_bp, url_prefix=api)
    app.register_blueprint(admin_bp, url_prefix=api + "/admin")
    app.register_api(access_bp, url_prefix=api + "/admin")

    # ── Artifact hub: one blueprint per ecosystem ───────────────────
    # Each carries both the JSON catalog under `/api/v1/*` and the
    # machine-facing protocol routes of its ecosystem, so all four are
    # registered at the bare prefix and declare their full paths internally.
    # Every view keeps its own `@require_permission` guard — there is no
    # blueprint-wide guard, so no guard may be dropped or written above its
    # `@route` decorator.
    app.register_api(hub_bp, url_prefix=prefix)
    # `npm_bp` is a flask-openapi3 APIBlueprint too: `PUT /npm/<package>` binds
    # `npm publish`'s JSON body as a view parameter, which only the per-verb
    # decorators install, so it goes through `register_api` like `pypi_bp`. Every
    # other npm route stays a plain `@npm_bp.route` and is registered unchanged.
    app.register_api(npm_bp, url_prefix=prefix)
    # `docker_bp` and `debian_bp` are flask-openapi3 APIBlueprints as well: the
    # docker `tags/list` query, the docker artifact upload, the offline snapshot
    # query and the offline bundle import bind their input as view parameters,
    # which only the per-verb decorators install, so both go through
    # `register_api`. Every other route on them stays a plain `@…_bp.route` and
    # is registered unchanged.
    app.register_api(docker_bp, url_prefix=prefix)
    app.register_api(debian_bp, url_prefix=prefix)
    # `docs_bp` binds request input on four of its views (the two Markdown
    # writes, the asset upload and the raw-download `?download=` flag), so it
    # goes through `register_api` as well; every other docs route stays a plain
    # `@docs_bp.route` and is registered unchanged.
    app.register_api(docs_bp, url_prefix=prefix)

    # ── Private CA chain. Anonymous on purpose: a client must be able to
    #    fetch the CA before it can trust the mirror.  Serves exactly one
    #    configured file, replacing the old `static/certs/` static directory.
    app.register_blueprint(certs_bp, url_prefix=prefix)

    # ── Device authorization (the DSH plugin's key hand-off).  Registered at
    #    the bare prefix because it deliberately straddles both surfaces: the
    #    JSON machine half lives under /api/v1/device/* and is anonymous by
    #    design (the caller has no credential yet), while the HTML approval
    #    page at /device is reached through the normal browser login flow.
    app.register_blueprint(device_bp, url_prefix=prefix)

    # ── Agent Hub: repositories, findings and the agent runtime ─────────
    # Registered at the bare prefix like the artifact-hub blueprints, so each
    # view declares its full path internally (/api/v1/repos…,
    # /api/v1/findings…, /api/v1/agent/tasks…, /api/v1/repos/<slug>/context…).
    # `repo_bp` binds request input on seven of its views (the two list queries
    # and the five JSON bodies), which only the per-verb decorators install, so
    # it goes through `register_api` like `pypi_bp`.  Every other view on it
    # stays a plain `@repo_bp.route` and is registered unchanged.
    app.register_api(repo_bp, url_prefix=prefix)
    # The other three Agent-Hub blueprints bind request input too — the context
    # search's query string, the findings board's filters, the decide/policy
    # bodies and the agent task list/create pair (each module carries a "Request
    # binding" note naming its views) — so they go through `register_api` as
    # well.  Their remaining views stay plain `@…_bp.route` and are registered
    # unchanged; `repo_webhook_bp` binds nothing at all, because its HMAC is
    # verified over the raw bytes before anything is parsed.
    app.register_api(repo_context_bp, url_prefix=prefix)
    app.register_api(findings_bp, url_prefix=prefix)
    app.register_api(agent_tasks_bp, url_prefix=prefix)
    app.register_blueprint(repo_webhook_bp, url_prefix=prefix)

    # ── SPA shell. Machine routes above win by rule specificity, so this
    #    only ever handles browser-facing URLs.
    app.register_blueprint(spa_bp, url_prefix=prefix)
