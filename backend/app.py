"""cpypiserver — lightweight Python package server built with Flask.

Entry point.  All infrastructure is wired via extensions (plugin architecture),
so this file stays short regardless of how many features are added.
"""

from __future__ import annotations

import logging
import os
import sys

from flask import Response, jsonify
from flask_openapi3 import OpenAPI
from pydantic import ValidationError
from werkzeug.middleware.proxy_fix import ProxyFix

from config import settings
from extensions import ExtensionRegistry
from extensions.database import DatabaseExtension
from extensions.cache import CacheExtension
from extensions.index_ext import IndexExtension
from extensions.error_handlers import ErrorHandlersExtension
from extensions.stats_refresh import StatsRefreshExtension
from services import logsafe


# ── Request-binding error envelope ──────────────────────────────────
# flask-openapi3 hands ``validation_error_callback`` the raw pydantic
# ``ValidationError`` and passes the return value straight to ``abort()``, so
# the callback must return a ready ``Response`` — returning a
# ``(body, status)`` tuple raises ``LookupError`` at abort time.
#
# The shape is the one the previous request-binding layer produced, kept
# deliberately: an invalid bound request answers ``400`` with a nested
# ``{"validation_error": {"<location>": [...]}}`` body rather than the library
# default (``422`` with a bare list).
#
# The location key comes from the model that failed, because that is all the
# exception carries: pydantic reports *field* locations and never which part of
# the request the model was binding, and field names collide across locations
# (``format`` is a query field on one route, ``content`` a form field on another).
# A model bound to a route but absent from this table still answers ``400``; it is
# filed under the generic ``body_params``, so a forgotten entry degrades the key
# rather than the status.
_BOUND_MODEL_LOCATION: dict[str, str] = {
    "FormatQuery": "query_params",
    "SimpleProjectPath": "path_params",
    "PyPIUploadForm": "form_params",
    "NpmPublishDocument": "body_params",
    "NpmSearchQuery": "query_params",
    "CreateKeyRequest": "body_params",
    "CreateRoleRequest": "body_params",
    "SetRolePermissionsRequest": "body_params",
    "GrantRoleRequest": "body_params",
    "SetSuperuserRequest": "body_params",
    "UserListQuery": "query_params",
    "ToolUploadForm": "form_params",
    "ModelRouteRequest": "body_params",
    "ModelRouteProbeRequest": "body_params",
    "DockerTagsQuery": "query_params",
    "DockerUploadForm": "form_params",
    "DebianSnapshotQuery": "query_params",
    "DebianBundleUploadForm": "form_params",
    "DocsContentRequest": "body_params",
    "DocsAssetUploadForm": "form_params",
    "DocsDownloadQuery": "query_params",
    "RepoListQuery": "query_params",
    "RepoIssueListQuery": "query_params",
    "RepoCreateRequest": "body_params",
    "RepoImportRequest": "body_params",
    "RepoSyncRequest": "body_params",
    "RunnerPatchRequest": "body_params",
    "RunnerCredentialRequest": "body_params",
    "RepoContextSearchQuery": "query_params",
    "FindingListQuery": "query_params",
    "FindingDecisionRequest": "body_params",
    "ReviewPolicyRequest": "body_params",
    "AgentTaskListQuery": "query_params",
    "AgentTaskCreateRequest": "body_params",
}


def _validation_error_response(exc: ValidationError) -> Response:
    location = _BOUND_MODEL_LOCATION.get(exc.title or "", "body_params")
    resp = jsonify({"validation_error": {location: exc.errors()}})
    resp.status_code = 400
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


# The machine-facing Jinja templates live under ``static/<ecosystem>/`` — the
# name is historical.  Pointing Flask's own loader at that folder is what lets
# every route call ``render_template("python/simple_index.html", …)`` instead of
# reading the file by hand and handing the text to ``render_template_string``.
#
# No built-in static handler.  Flask's default serves *everything* under
# ``static/`` anonymously, which would publish those Jinja templates and
# anything an operator drops into ``static/certs/``.  The only asset a browser
# needs before it has a session is the compiled Vue bundle, and ``routes/spa.py``
# serves that one directory explicitly.  This also means an unguarded endpoint
# can no longer hide behind the ``static`` endpoint.
# `OpenAPI` is a `Flask` subclass: every existing blueprint keeps working, and
# the routes that need request binding live on an `APIBlueprint` (see
# `routes/pypi.py` and `register_api` in `routes/__init__.py`).
#
# `doc_ui=False` deliberately imports only the request-binding half of
# flask-openapi3.  Its bundled documentation surface (`/openapi/`,
# `/openapi/openapi.json`, `/openapi/static/…`) would register three more
# anonymous endpoints and would publish a *second* OpenAPI document next to the
# repository's own, which stays the single source of truth
# (`backend/openapi/`, served at `/openapi.json`).
app = OpenAPI(
    __name__,
    static_folder=None,
    template_folder="static",
    doc_ui=False,
    validation_error_callback=_validation_error_response,
)
app.config.from_mapping(settings.model_dump())
app.secret_key = settings.server.secret_key
app.config["MAX_CONTENT_LENGTH"] = settings.storage.max_content_length
os.makedirs(settings.storage.packages_dir, exist_ok=True)

# ── Logging ─────────────────────────────────────────────────────────
# Installed *before* the extensions, because `ErrorHandlersExtension` redacts
# every 5xx body and logs the original message instead: that log line is where
# an operator sees the driver error, the upstream URL or the path a client must
# no longer be shown, so it has to be configured by the time the first response
# can be refused.  `LOG_LEVEL` was declared in `config/server.py` without being
# wired to anything; this is the one place a handler is attached, so the level is
# set here.  `logsafe` adds the single log-injection filter (newlines can forge a
# record) to the root logger here rather than at each call site.
logging.basicConfig(
    level=getattr(logging, settings.server.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stderr,
)
logsafe.install()

# ── Trust reverse-proxy headers (X-Forwarded-Proto, etc.) ──────────
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

# ── Plug in extensions (topological order auto-resolved) ────────────
registry = ExtensionRegistry([
    ErrorHandlersExtension(),
    DatabaseExtension(),
    CacheExtension(),
    IndexExtension(),
    StatsRefreshExtension(),
])
registry.init_all(app)

# ── Register routes ─────────────────────────────────────────────────
from routes import register_all
register_all(app)

# ── Seed authorization ──────────────────────────────────────────────
# Must run after the blueprints are imported: the guards in those modules are
# what declare the permission points, so the catalog is incomplete until now.
from services.authz import bootstrap as bootstrap_authz

# Cold-start superusers.  Both sources are configuration, so neither can be
# used to grant a role at runtime — they only seed the very first administrator
# (see AuthzService.bootstrap_superusers).  Prefer `python cli.py create-admin`
# for everything afterwards.
_bootstrap_ids = list(settings.server.admin_users)
if settings.auth.basic_username:
    _bootstrap_ids.append(settings.auth.basic_username)

_authz_summary = bootstrap_authz(app.extensions["authz"], _bootstrap_ids)
logging.getLogger("cpypiserver").info(
    "Authorization ready: %d permission point(s), %d role(s), %d superuser(s) total",
    _authz_summary["permissions"]["total"],
    len(app.extensions["authz"].list_roles()),
    app.extensions["authz"].count_superusers(),
)

# ── Run ─────────────────────────────────────────────────────────────

def main() -> None:
    """Console-script entry point (see `[project.scripts]` in pyproject.toml)."""
    app.run(
        host=settings.server.host,
        port=settings.server.port,
        debug=settings.server.debug,
        use_reloader=False,
    )


if __name__ == "__main__":
    main()
