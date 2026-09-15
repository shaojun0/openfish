"""cpypiserver — lightweight Python package server built with Flask.

Entry point.  All infrastructure is wired via extensions (plugin architecture),
so this file stays short regardless of how many features are added.
"""

from __future__ import annotations

import os
import logging

from flask import Flask
from werkzeug.middleware.proxy_fix import ProxyFix

from config import settings
from extensions import ExtensionRegistry
from extensions.database import DatabaseExtension
from extensions.cache import CacheExtension
from extensions.index_ext import IndexExtension
from extensions.error_handlers import ErrorHandlersExtension
from extensions.stats_refresh import StatsRefreshExtension

# ── Logging ──────────────────────────────────────────────────────────
logging.basicConfig(
    level=getattr(logging, settings.server.log_level.upper(), logging.INFO),
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)

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
# can no longer hide behind the ``static`` endpoint — see
# scripts/check_auth_guards.py.
app = Flask(__name__, static_folder=None, template_folder="static")
app.config.from_mapping(settings.model_dump())
app.secret_key = settings.server.secret_key
app.config["MAX_CONTENT_LENGTH"] = settings.storage.max_content_length
os.makedirs(settings.storage.packages_dir, exist_ok=True)

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
