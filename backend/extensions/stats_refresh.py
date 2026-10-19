"""Stats refresh extension — background daemon thread for the cached statistics.

The extension owns the one place that pays for the expensive computation:

* it restores the persisted snapshot into the cache at boot, so the overview
  page answers from the previous numbers immediately after a restart;
* it recomputes on a timer (``STATS_CACHE_SECONDS``, default 2 h) on a daemon
  thread and writes both the cache and the snapshot;
* request handlers only ever read the cache (see ``routes/stats.py`` and
  ``routes/session.py``) — a cold cache degrades to the cheap in-memory
  fallback in ``services.stats.compute`` instead of blocking on file sizes.
"""

from __future__ import annotations

import logging
import threading
import time

from extensions import Extension
from services.stats import (
    STATS_CACHE_KEY,
    compute as compute_stats,
    load_snapshot,
    save_snapshot,
)

logger = logging.getLogger("cpypiserver.stats")


class StatsRefreshExtension(Extension):
    name = "stats_refresh"
    dependencies = ["cache", "index"]

    def init_app(self, app) -> None:
        from config import settings
        cache = app.extensions["caching"]
        interval = settings.server.stats_cache_seconds
        workers = settings.server.stats_sizing_workers

        # Serve the previous run's numbers until the new one is ready. This is
        # what keeps a restart from re-paying the whole `stat` walk in front of
        # the first page load.
        snapshot = load_snapshot()
        if snapshot is not None:
            cache.set(STATS_CACHE_KEY, snapshot, timeout=0)
            logger.info(
                "restored stats snapshot %s (generated_at=%s)",
                settings.storage.stats_snapshot_file,
                snapshot.get("generated_at"),
            )

        def _refresh() -> None:
            try:
                started = time.monotonic()
                with app.app_context():
                    pkg_index = app.extensions.get("pypi_index")
                    key_mgr = app.extensions.get("api_key_manager")
                    data = compute_stats(pkg_index, key_mgr, sizing_workers=workers)
                cache.set(STATS_CACHE_KEY, data, timeout=0)
                save_snapshot(data)
                elapsed = time.monotonic() - started
                logger.info(
                    "stats refreshed in %.1fs (%d packages, %d files, sizes_ready=%s)",
                    elapsed, data["overview"]["package_count"],
                    data["overview"]["file_count"], data["sizes_ready"],
                )
            except Exception:
                # A failed refresh must not kill the timer or the worker thread:
                # the previous cache/snapshot stays served and the next tick
                # tries again.
                logger.warning("stats refresh failed", exc_info=True)

        def _schedule(delay: float) -> None:
            t = threading.Timer(delay, _tick)
            t.daemon = True
            t.name = "openfish-stats-refresh"
            t.start()

        def _tick() -> None:
            _refresh()
            _schedule(interval)

        # The first computation is deliberately scheduled, not run inline: it
        # `stat`s every package file, and this extension is initialised while
        # ``app.py`` is still importing — i.e. before gunicorn binds the port.
        # On a cold start over a large or network-mounted ``PACKAGES_DIR`` that
        # walk outlived the healthcheck grace, so compose marked the backend
        # unhealthy and the edge nginx (``depends_on: backend:
        # condition: service_healthy``) was never started.  Request handlers now
        # answer from the snapshot/cache in the meantime.
        _schedule(0)
