"""Cached statistics for the SPA overview.

The overview page needs three numbers (packages / files / storage).  It used to
read ``GET /api/v1/packages`` — the full per-package list — and that handler
computed the whole aggregated statistics dictionary on every request, including
a ``stat`` of every package file.  On the 1.5 TB / 160k-file intranet deployment
that took longer than the 30 s reverse proxy in front of it, so the page showed
an empty registry.  Admins had the same numbers behind ``/admin/stats``, but a
non-admin cannot read that route.

This module is the missing read path: a small, cache-first projection of the
same dictionary the background refresh maintains.  It never computes file sizes
on the request thread — when the cache is empty (first boot of a deployment that
has no snapshot yet) it answers from the in-memory index with the storage figure
marked as not-yet-ready, and the SPA asks again until it is.
"""

from __future__ import annotations

from flask import Blueprint, current_app, jsonify

from auth.decorators import require_permission
from auth.permissions import PACKAGE_READ
from openapi import api_operation, errors, ok
from services.stats import STATS_CACHE_KEY, compute, overview

stats_bp = Blueprint("stats", __name__)


@stats_bp.route("/stats/overview")
@require_permission(PACKAGE_READ)
@api_operation(
    summary="Cached registry counters",
    description=(
        "Package, file and storage totals for the overview page, projected from "
        "the statistics snapshot the background refresh maintains. Answers from "
        "cache (memory, then the persisted snapshot) and never `stat`s package "
        "files on the request thread, so it stays fast on a 100k-file registry. "
        "`sizes_ready: false` means the storage figure is still being computed "
        "in the background — ask again shortly; the counts are already final. "
        "Download and API-key totals are deliberately not included: they are on "
        "the admin surface (`GET /api/v1/admin/stats`)."
    ),
    tags=["Statistics"],
    responses={"200": ok("Cached counters", "StatsOverview"), **errors("401", "403", "500")},
)
def stats_overview():
    cache = current_app.extensions.get("caching")
    data = cache.get(STATS_CACHE_KEY) if cache is not None else None
    if not isinstance(data, dict):
        # Cold cache: the counts come from the in-memory index (no filesystem
        # calls), so the page renders immediately; the persisted snapshot or the
        # background refresh fills the storage figure in.
        pkg_index = current_app.extensions.get("pypi_index")
        data = compute(pkg_index, None, sizes=False, counters=False)
    return jsonify(overview(data))
