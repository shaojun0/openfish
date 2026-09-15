"""Serve the built Vue SPA shell.

Route ownership
---------------
The SPA owns every URL a human opens in a browser.  Machine-facing endpoints
(``/simple/``, ``/packages/<file>``, ``/python-builds/``, ``/api/v1/*``,
``/health``) are registered on other blueprints and deliberately stay out of
this module — ``pip`` and ``uv`` parse those responses directly and never
execute JavaScript, so they must keep working without the SPA.

The shell is a single static ``index.html``; all routing after that happens
client-side in ``frontend/src/router/index.ts``.

Asset serving
-------------
This module is also the *only* thing that serves files off disk to a browser.
``app.py`` disables Flask's built-in ``static`` handler, so the SPA bundle
(``static/dist/*``) is served here by one explicit route instead of the whole
``static/`` tree.  Two things live under ``static/`` that must never be public
and used to be: the Jinja templates in ``static/<ecosystem>/`` (read by
``services/templates.py``) and any TLS material an operator drops into
``static/certs/``.  Keep it that way — ``scripts/check_auth_guards.py`` now
fails the build if a blanket static handler comes back.

Whole-blueprint guard
---------------------
``spa_bp`` carries ``require_permission(APP_READ)`` (set in
``routes/__init__.py``), so neither the shell nor the bundle is reachable
without a session.  That is why this module must not assume a public request
anywhere: the only way into the console is a signed-in caller who holds
``app:read``.
"""

from __future__ import annotations

from pathlib import Path

from flask import Blueprint, abort, current_app, send_from_directory

from config import settings

spa_bp = Blueprint("spa", __name__)

#: Paths that must never fall through to the SPA shell, even when no route
#: matched.  Guards the catch-all against swallowing a typo'd API call.
_RESERVED_PREFIXES = (
    "api/",
    "simple/",
    "packages/",
    "python-builds/",
    "tools/",
    "npm/",
    "docker/",
    "debian/",
    "docs/",
    "static/",
    "certs/",
    "auth/",
    "health",
    "favicon.ico",
    # API discovery surface — explicit routes exist, but keep the catch-all
    # from ever swallowing a typo'd path in these namespaces.
    ".well-known/",
    "openapi.json",
    "llms.txt",
)


def _dist_dir() -> Path:
    """Where the built SPA lives.

    Defaults to ``<backend>/static/dist``; ``FRONTEND_DIST_DIR`` points it
    somewhere else (``../frontend/dist`` for a local build).  In the split
    Docker deployment the frontend container serves the SPA and never reaches
    this code path.
    """
    configured = settings.server.frontend_dist_dir
    if configured:
        return Path(configured).expanduser().resolve()
    return Path(current_app.root_path) / "static" / "dist"


def _shell():
    """Return the SPA entry point, or a build hint when it is missing."""
    dist = _dist_dir()
    if not (dist / "index.html").is_file():
        return (
            "<h1>Frontend bundle not found</h1>"
            "<p>This process is not serving the SPA. In the split deployment "
            "the <code>frontend</code> container serves it; for a local build:</p>"
            "<pre><code>cd frontend\nnpm install\nnpm run build</code></pre>"
            "<p>Then either run the frontend container, or set "
            "<code>FRONTEND_DIST_DIR=../frontend/dist</code> and start Flask "
            "from <code>backend/</code>.</p>",
            503,
        )
    return send_from_directory(dist, "index.html")


@spa_bp.route("/")
def index():
    """Root — also the OAuth callback's default landing page."""
    return _shell()


@spa_bp.route("/static/dist/<path:filename>")
def dist_asset(filename: str):
    """The compiled Vue bundle — ``index.html`` and ``assets/*``.

    Narrow on purpose: one directory, not the whole ``static/`` tree.  It sits
    behind the blueprint's ``app:read`` guard like the rest of the console, so
    the bundle is only ever fetched by a caller that can load the app anyway.
    """
    return send_from_directory(_dist_dir(), filename)


@spa_bp.route("/packages")
@spa_bp.route("/api-keys")
@spa_bp.route("/admin")
@spa_bp.route("/tools")
@spa_bp.route("/npm")
@spa_bp.route("/docker")
@spa_bp.route("/debian")
@spa_bp.route("/models")
@spa_bp.route("/documentation/<ecosystem>")
def page(ecosystem: str | None = None):
    return _shell()


@spa_bp.route("/<path:path>")
def fallback(path: str):
    """History-mode fallback — a deep link such as ``/admin`` reaches the SPA.

    Flask still prefers the more specific machine-facing rules, so this only
    ever handles paths no real endpoint claimed.
    """
    if path.startswith(_RESERVED_PREFIXES):
        abort(404)
    return _shell()
