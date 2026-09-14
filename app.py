"""cpypiserver — lightweight Python package server built with Flask.

Entry point.  All infrastructure is wired via extensions (plugin architecture),
so this file stays short regardless of how many features are added.
"""

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

app = Flask(__name__)
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

# ── Run ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app.run(
        host=settings.server.host,
        port=settings.server.port,
        debug=settings.server.debug,
        use_reloader=False,
    )
