"""Stats refresh extension — background daemon thread for admin stats."""

from __future__ import annotations

import logging
import threading

from extensions import Extension
from services.stats import compute as compute_stats

logger = logging.getLogger("cpypiserver.stats_refresh")


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
                logger.info("Computing admin stats…")
                with app.app_context():
                    pkg_index = app.extensions.get("pypi_index")
                    key_mgr = app.extensions.get("api_key_manager")
                    data = compute_stats(pkg_index, key_mgr)
                cache.set("admin_stats", data, timeout=0)
                elapsed = time.time() - start
                logger.info(
                    "Stats refreshed (%d packages, %.1fs, next in %ds)",
                    data["overview"]["package_count"], elapsed, interval,
                )
            except Exception:
                logger.exception("Stats refresh failed")

        def _schedule():
            t = threading.Timer(interval, _tick)
            t.daemon = True
            t.start()

        def _tick():
            _refresh()
            _schedule()

        _refresh()
        _schedule()
