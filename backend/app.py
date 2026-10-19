"""cpypiserver — lightweight Python package server built with Flask.

Entry point.  All infrastructure is wired via extensions (plugin architecture),
so this file stays short regardless of how many features are added.
"""

from __future__ import annotations

import logging
import os
import sys
import time

from flask import Response, g, jsonify, request
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

logger = logging.getLogger("cpypiserver.http")

#: Import time is invisible in the access log (the worker accepts the first
#: request only after this module finishes), so it is measured here and logged
#: once at the end.  On a 100k-file catalog the index scan is the phase that
#: matters; see `extensions/__init__.py` for the per-extension breakdown.
_startup_started = time.perf_counter()

# Anything at or above this is logged with the path, so a slow endpoint is
# one grep away instead of a Network-tab story.
SLOW_REQUEST_MS = 1000.0


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


# ── Mount roots ─────────────────────────────────────────────────────
# `PACKAGES_DIR` is a catalog root: a bind mount in compose, and docker/README
# §4 re-points those with a symlink (`ln -sfn /srv/openfish-share/npm docker/npm`).
# The bind mount carries the *directory*, never the host's path space, so a link
# inside it is resolved by the container: a host absolute target is not there.
# `os.makedirs(..., exist_ok=True)` answers that with a bare
# `FileExistsError: [Errno 17]` — the path does exist, as a link, it just is not
# a directory — which names neither the variable nor the cause.
#
# Two outcomes instead, decided by where the link points:
#   * a *relative* target that is not there yet, on the same filesystem as the
#     link, is created.  That is what `makedirs` does for a path that is simply
#     missing, and the directory stays inside the mounted tree;
#   * everything else — an absolute target, another filesystem, a regular file in
#     the way — refuses to boot with the variable, the link and its target named.
#     Creating an absolute target would put the packages in the container's
#     writable layer, where the next rebuild discards them.
def _broken_link(path: str) -> str | None:
    """The first symlink at or above *path* that does not resolve to a directory."""
    current = os.path.abspath(path)
    while current != os.path.dirname(current):  # stop at the filesystem root
        if os.path.islink(current) and not os.path.isdir(current):
            return current
        if os.path.exists(current):
            return None  # a real directory (or a file, reported separately) is above
        current = os.path.dirname(current)
    return None


def _repairable_target(link: str) -> str | None:
    """Where to create broken *link*'s target, or ``None`` when that is not safe.

    A relative target is resolved inside whatever filesystem reads the link, so
    creating it here is the same act as ``makedirs`` on a path that is missing —
    but only when it stays on the link's own filesystem (i.e. inside the same
    mount) and its parent already exists.  Only the leaf is ever created.
    """
    text = os.readlink(link)
    if os.path.isabs(text):
        return None
    target = os.path.normpath(os.path.join(os.path.dirname(link), text))
    parent = os.path.dirname(target)
    if not os.path.isdir(parent):
        return None
    if os.stat(parent).st_dev != os.stat(os.path.dirname(link)).st_dev:
        return None  # a different mount: not ours to create
    return target


def _directory_problem(path: str) -> str | None:
    """Why *path* cannot be used as a directory, or ``None`` when it can.

    Walks up from *path*: the first segment that exists but is not a directory
    is the obstacle (a dangling symlink, a symlink to a file, a regular file).
    """
    link = _broken_link(path)
    if link is not None:
        return (
            f"the symlink {link!r} points at {os.path.realpath(link)!r}, "
            "which is not a directory"
        )
    current = os.path.abspath(path)
    while current != os.path.dirname(current):
        if os.path.exists(current):
            return None if os.path.isdir(current) else f"{current!r} is not a directory"
        current = os.path.dirname(current)
    return None


def _ensure_directory(path: str, variable: str) -> None:
    """Create the configured mount root *path*, or say why it is unusable."""
    link = _broken_link(path)
    target = _repairable_target(link) if link is not None else None
    if target is not None:
        os.makedirs(target, exist_ok=True)
        # Before `logging.basicConfig`, so this goes to stderr the way the
        # RuntimeError below does; either way it lands in `docker compose logs`.
        print(
            f"openfish: {variable}: created {target!r}, "
            f"the missing target of the symlink {link!r}",
            file=sys.stderr,
        )
    problem = _directory_problem(path)
    if problem is not None:
        raise RuntimeError(
            f"cannot use {variable}={path!r}: {problem}. Create that path, or "
            "re-point the symlink at something that exists where the process runs "
            "— a link written on the host with an absolute target is not there "
            "after a bind mount; see `docker/README.md` §5."
        )
    os.makedirs(path, exist_ok=True)


# The variable name is asked of the model, never re-typed (see `config.base`).
_ensure_directory(
    settings.storage.packages_dir, settings.storage.env_name("packages_dir")
)

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


# ── Request timing ──────────────────────────────────────────────────
# ``Server-Timing: app;dur=…`` shows up in the browser's Network panel next to
# the total time.  The two differ when the delay is *not* in the handler — a
# worker still importing the app accepts the connection late — which is exactly
# the "every request took 16 s right after `docker compose up -d`" report.
@app.before_request
def _request_timing_start() -> None:
    g.openfish_request_started = time.perf_counter()


@app.after_request
def _request_timing_finish(response: Response) -> Response:
    started = getattr(g, "openfish_request_started", None)
    if started is None:
        return response
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    # `auth` is the time spent resolving the caller (OAuth2 introspection, API
    # key lookup, session cookie); it is the part that can dominate a request on
    # an intranet with a slow identity provider.  Reported separately so the
    # browser's Network panel answers "16 s — but which 16 s?".
    auth_ms = getattr(g, "openfish_auth_ms", None)
    timing = f"app;dur={elapsed_ms:.1f}"
    if auth_ms is not None:
        timing += f", auth;dur={auth_ms:.1f}"
    response.headers["Server-Timing"] = timing
    if elapsed_ms >= SLOW_REQUEST_MS:
        logger.warning(
            "slow request %s %s -> %s in %.0f ms",
            request.method, request.path, response.status_code, elapsed_ms,
        )
    return response

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
_routes_started = time.perf_counter()
from routes import register_all
register_all(app)
logger.info("routes registered in %.1f ms", (time.perf_counter() - _routes_started) * 1000.0)

# ── Seed authorization ──────────────────────────────────────────────
# Must run after the blueprints are imported: the guards in those modules are
# what declare the permission points, so the catalog is incomplete until now.
from services.authz import bootstrap as bootstrap_authz

# Cold-start superusers.  Both sources are configuration, so neither can be
# server holds no superuser of its own: the first administrator is created
# explicitly with `python cli.py create-admin <identity>`, and nothing in the
# environment can grant the server to whoever can set a variable.
# for everything afterwards.
_authz_started = time.perf_counter()
_authz_summary = bootstrap_authz(app.extensions["authz"])
logger.info(
    "authorization seeded in %.1f ms", (time.perf_counter() - _authz_started) * 1000.0
)
logger.info(
    "openfish backend ready in %.1f ms", (time.perf_counter() - _startup_started) * 1000.0
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
