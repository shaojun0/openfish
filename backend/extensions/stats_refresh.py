"""Stats refresh extension — background daemon thread for admin stats."""

from __future__ import annotations

import threading

from extensions import Extension
from services.stats import compute as compute_stats


class StatsRefreshExtension(Extension):
    name = "stats_refresh"
    dependencies = ["cache", "index"]

    def init_app(self, app) -> None:
        from config import settings
        cache = app.extensions["caching"]
        interval = settings.server.stats_cache_seconds

        def _refresh():
            import time
            try:
                start = time.time()
                with app.app_context():
                    pkg_index = app.extensions.get("pypi_index")
                    key_mgr = app.extensions.get("api_key_manager")
                    data = compute_stats(pkg_index, key_mgr)
                cache.set("admin_stats", data, timeout=0)
                elapsed = time.time() - start
            except Exception:
                pass

        def _schedule(delay: float) -> None:
            t = threading.Timer(delay, _tick)
            t.daemon = True
            t.name = "openfish-stats-refresh"
            t.start()

        def _tick():
            _refresh()
            _schedule(interval)

        # The first computation is deliberately scheduled, not run inline: it
        # ``stat``s every package file (the index scan stores ``size=0``), and
        # this extension is initialised while ``app.py`` is still importing —
        # i.e. before gunicorn binds the port.  On a cold start over a large or
        # network-mounted ``PACKAGES_DIR`` that walk outlived the healthcheck
        # grace, so compose marked the backend unhealthy and the edge nginx
        # (``depends_on: backend: condition: service_healthy``) was never
        # started.  The admin page computes on demand while the cache is still
        # empty (``routes/admin.py``), so moving the first tick off the import
        # path costs nothing.
        _schedule(0)
