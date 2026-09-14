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
"""

from __future__ import annotations

from pathlib import Path

from flask import Blueprint, abort, current_app, send_from_directory

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
    "static/",
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
    return Path(current_app.root_path) / "static" / "dist"


def _shell():
    """Return the SPA entry point, or a build hint when it is missing."""
    dist = _dist_dir()
    if not (dist / "index.html").is_file():
        return (
            "<h1>Frontend bundle not found</h1>"
            "<p>The Vue frontend has not been built yet. Build it with:</p>"
            "<pre><code>cd frontend\nnpm install\nnpm run build</code></pre>"
            "<p>The Docker image performs this step automatically.</p>",
            503,
        )
    return send_from_directory(dist, "index.html")


@spa_bp.route("/")
def index():
    """Root — also the OAuth callback's default landing page."""
    return _shell()


@spa_bp.route("/packages")
@spa_bp.route("/api-keys")
@spa_bp.route("/admin")
@spa_bp.route("/tools")
@spa_bp.route("/npm")
@spa_bp.route("/docker")
@spa_bp.route("/debian")
@spa_bp.route("/models")
def page():
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
