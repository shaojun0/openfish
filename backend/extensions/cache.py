"""Cache extension — Flask-Caching SimpleCache."""

from __future__ import annotations

from extensions import Extension


class CacheExtension(Extension):
    name = "cache"
    dependencies: list[str] = []

    def init_app(self, app) -> None:
        from flask_caching import Cache
        cache = Cache(app, config={"CACHE_TYPE": "SimpleCache"})
        app.extensions["caching"] = cache
