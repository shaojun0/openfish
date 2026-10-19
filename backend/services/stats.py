"""Server-wide statistics: one computation, one cached snapshot, many readers.

Why this module looks like this
-------------------------------
The expensive part of the overview numbers is that the index stores ``size=0``
for every file — the full scan never ``stat``s the catalog, which is what keeps
startup fast on a 100k-file registry — so the first real storage figure has to
come from ``stat``-ing every package file.  On a 1.5 TB / 160k-file catalog on
network storage that walk takes tens of seconds.

That walk used to run *inside* two request handlers: ``/api/v1/admin/stats`` (on
a cache miss) and — for every non-admin user — ``/api/v1/packages``, which the
overview page called just to read three counters.  A 30 s reverse proxy in front
of the deployment then answered ``504``, and the SPA rendered an empty package
list, which reads exactly like "the registry has no packages".

The contract now:

* :func:`compute` is the only expensive function, and the background
  ``StatsRefreshExtension`` is the only caller that passes ``sizes=True``.
* Request handlers are cache-first (:data:`STATS_CACHE_KEY`).  When the cache
  is genuinely empty they fall back to :func:`compute` with ``sizes=False,
  counters=False`` — pure in-memory packing of what the index already holds,
  no filesystem calls — so a request can never wait on the file sizes.
* The last successful result is persisted (:func:`save_snapshot` /
  :func:`load_snapshot`) next to the database, so a restart serves the previous
  numbers immediately instead of recomputing them behind the first page load.

The sizes are computed with a small thread pool: the bottleneck is the latency
of one ``stat`` per file, not CPU, so a serial walk over a network filesystem
spends almost all of its wall time waiting.
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from config import settings
from services.fileio import read_json, write_json
from services.format import human_size

logger = logging.getLogger("cpypiserver.stats")

#: Key of the computed dictionary in the Flask-Caching store.
STATS_CACHE_KEY = "admin_stats"

#: Bumped when the persisted shape changes in a way the reader must not guess.
SNAPSHOT_VERSION = 1

#: A `stat` costs a couple of microseconds on a local disk and a millisecond or
#: more on network storage.  The first few files are timed: below this latency
#: the walk stays serial (threads would only add GIL contention), above it the
#: remaining files are dealt into one strided group per worker, so a 160k-file
#: catalog becomes eight pool tasks instead of 160k futures.
_PARALLEL_STAT_THRESHOLD_SECONDS = 50e-6
_STAT_SAMPLE_SIZE = 32


# ── Persisted snapshot ───────────────────────────────────────────────

def snapshot_path() -> Path:
    """Where the last successful computation is kept between restarts.

    ``STATS_SNAPSHOT_FILE`` is forwarded by docker-compose as an empty string,
    which the settings layer treats as "unset", so the default below is what a
    container uses: ``/app/data/stats_snapshot.json``, inside the same bind
    mount as the SQLite database.
    """
    return Path(settings.storage.stats_snapshot_file)


def load_snapshot() -> dict | None:
    """Read the persisted statistics dictionary, or ``None`` when unusable.

    A stale file from an older release, or one truncated by a full disk, must
    degrade to "no snapshot" — the background refresh then rebuilds it — rather
    than fail the boot.
    """
    data = read_json(snapshot_path(), default=None)
    if not isinstance(data, dict):
        return None
    if data.get("snapshot_version") != SNAPSHOT_VERSION:
        return None
    if not isinstance(data.get("overview"), dict) or not isinstance(data.get("packages"), list):
        return None
    return data


def save_snapshot(data: dict) -> bool:
    """Persist the computed dictionary atomically.  Never raises."""
    try:
        write_json(snapshot_path(), dict(data, snapshot_version=SNAPSHOT_VERSION))
        return True
    except OSError as exc:
        logger.warning("cannot persist stats snapshot %s: %s", snapshot_path(), exc)
        return False


# ── Computation ──────────────────────────────────────────────────────

def compute(
    pkg_index,
    key_mgr,
    *,
    sizes: bool = True,
    counters: bool = True,
    sizing_workers: int | None = None,
) -> dict:
    """Build the aggregated-stats dictionary.

    ``sizes=False`` skips the per-file ``stat`` walk (the only filesystem I/O in
    here) and reports zero storage — the cheap fallback for request handlers
    that must answer immediately while the background refresh is still running.

    ``counters=False`` skips the database queries behind the download/upload/key
    numbers, for the same reason.
    """
    snapshot = pkg_index.get_snapshot() if pkg_index else {}
    all_files: list = []
    for files in snapshot.values():
        all_files.extend(files)

    if sizes:
        _fill_sizes(all_files, sizing_workers)

    per_package: dict = {}
    for name, files in sorted(snapshot.items()):
        total_size = sum(f.size for f in files) if sizes else 0
        per_package[name] = {"file_count": len(files), "total_size": total_size}

    if counters:
        key_stats, total_downloads, total_uploads, active_key_count = _key_stats(key_mgr)
        package_db_stats = _aggregate_package_stats(key_mgr) if key_mgr else {}
    else:
        key_stats, total_downloads, total_uploads, active_key_count = [], 0, 0, 0
        package_db_stats = {}

    merged_packages: list = []
    for name, pkg_info in per_package.items():
        db_info = package_db_stats.get(name, {})
        merged_packages.append({
            "name": name,
            "file_count": pkg_info["file_count"],
            "total_size": pkg_info["total_size"],
            "total_size_human": human_size(pkg_info["total_size"]),
            "download_count": db_info.get("downloads", 0),
            "upload_count": db_info.get("uploads", 0),
        })
    merged_packages.sort(key=lambda x: x["download_count"] + x["upload_count"], reverse=True)

    total_storage = sum(p["total_size"] for p in per_package.values())
    generated_at = _now()
    return {
        "overview": {
            "package_count": len(snapshot),
            "file_count": len(all_files),
            "total_storage": total_storage,
            "total_storage_human": human_size(total_storage),
            "total_keys": len(key_stats) if key_mgr else 0,
            "active_keys": active_key_count,
            "total_downloads": total_downloads,
            "total_uploads": total_uploads,
            "sizes_ready": sizes,
            "generated_at": generated_at,
        },
        "packages": merged_packages,
        "keys": key_stats,
        "sizes_ready": sizes,
        "generated_at": generated_at,
    }


def overview(data: dict | None) -> dict:
    """Project a computed/loaded dictionary down to the three public counters.

    The SPA overview needs package/file/storage only; download and key totals
    stay on the admin surface, so they are not echoed by the public endpoint.
    ``sizes_ready`` tells the client whether the storage figure is final or the
    background walk is still running.
    """
    payload = data if isinstance(data, dict) else {}
    source = payload.get("overview")
    if not isinstance(source, dict):
        source = {}
    return {
        "package_count": int(source.get("package_count") or 0),
        "file_count": int(source.get("file_count") or 0),
        "total_storage": int(source.get("total_storage") or 0),
        "total_storage_human": source.get("total_storage_human") or human_size(0),
        "sizes_ready": bool(payload.get("sizes_ready", False)),
        "generated_at": payload.get("generated_at"),
    }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _fill_sizes(files: list, workers: int | None) -> None:
    """Fill ``file.size`` for every entry that does not have it yet.

    A size that a previous pass already filled is reused, so a file is
    ``stat``-ed at most once per process lifetime — that is what makes the
    second and later refreshes cheap.
    """
    pending = [f for f in files if not f.size]
    if not pending:
        return
    if workers is None:
        workers = settings.server.stats_sizing_workers
    if workers > 1 and len(pending) > _STAT_SAMPLE_SIZE:
        started = time.perf_counter()
        for f in pending[:_STAT_SAMPLE_SIZE]:
            f.size = _file_size(f)
        per_file = (time.perf_counter() - started) / _STAT_SAMPLE_SIZE
        pending = pending[_STAT_SAMPLE_SIZE:]
        if per_file < _PARALLEL_STAT_THRESHOLD_SECONDS:
            workers = 1
        else:
            logger.info(
                "stats: stat latency ~%.0f µs, sizing %d files with %d workers",
                per_file * 1_000_000, len(pending) + _STAT_SAMPLE_SIZE, workers,
            )
    if workers <= 1:
        for f in pending:
            f.size = _file_size(f)
        return
    groups = [pending[index::workers] for index in range(workers)]
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="stats-size") as pool:
        for group, sizes in zip(groups, pool.map(_sizes_of_group, groups)):
            for f, size in zip(group, sizes):
                if size:
                    f.size = size


def _file_size(f) -> int:
    try:
        return Path(f.path).stat().st_size
    except OSError:
        return 0


def _sizes_of_group(files: list) -> list[int]:
    """One pool task: ``stat`` a whole slice of the catalog (see ``_fill_sizes``)."""
    return [_file_size(f) for f in files]


def _key_stats(key_mgr) -> tuple[list, int, int, int]:
    if not key_mgr:
        return [], 0, 0, 0
    keys = key_mgr.list_keys()
    total_dl = total_ul = active = 0
    result: list = []
    for k in keys:
        dl = k.get("download_count", 0)
        ul = k.get("upload_count", 0)
        total_dl += dl
        total_ul += ul
        if not k.get("is_expired"):
            active += 1
        result.append({
            "id": k["id"], "name": k["name"], "prefix": k["prefix"],
            "created_by": k["created_by"],
            "download_count": dl, "upload_count": ul,
            "last_used": k.get("last_used", ""),
            "created_at": k.get("created_at", ""),
            "expires_at": k.get("expires_at", ""),
            "is_expired": k.get("is_expired", False),
            "is_permanent": k.get("is_permanent", False),
        })
    result.sort(key=lambda x: x["download_count"] + x["upload_count"], reverse=True)
    return result, total_dl, total_ul, active


def _aggregate_package_stats(key_mgr) -> dict:
    session = key_mgr._s
    try:
        from models.api_key import ApiKeyStats
        rows = session.query(ApiKeyStats).all()
        result: dict = {}
        for row in rows:
            pkg = result.setdefault(row.package_name, {"downloads": 0, "uploads": 0})
            if row.event_type == "download":
                pkg["downloads"] += row.count
            else:
                pkg["uploads"] += row.count
        return result
    except Exception:
        return {}
    finally:
        session.close()
