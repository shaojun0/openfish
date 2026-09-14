"""Admin statistics computation.

Runs in a background daemon thread (configurable interval) and on-demand.
Results stored in Flask-Caching SimpleCache.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger("cpypiserver.stats")


def compute(pkg_index, key_mgr) -> dict:
    """Build the full aggregated-stats dictionary."""
    snapshot = pkg_index.get_snapshot() if pkg_index else {}
    all_files: list = []
    for files in snapshot.values():
        all_files.extend(files)

    per_package: dict = {}
    for name, files in sorted(snapshot.items()):
        total_size = 0
        for f in files:
            if f.size == 0:
                try:
                    f.size = Path(f.path).stat().st_size
                except OSError:
                    pass
            total_size += f.size
        per_package[name] = {"file_count": len(files), "total_size": total_size}

    key_stats, total_downloads, total_uploads, active_key_count = _key_stats(key_mgr)
    package_db_stats = _aggregate_package_stats(key_mgr) if key_mgr else {}

    merged_packages: list = []
    for name, pkg_info in per_package.items():
        db_info = package_db_stats.get(name, {})
        merged_packages.append({
            "name": name,
            "file_count": pkg_info["file_count"],
            "total_size": pkg_info["total_size"],
            "total_size_human": _human_size(pkg_info["total_size"]),
            "download_count": db_info.get("downloads", 0),
            "upload_count": db_info.get("uploads", 0),
        })
    merged_packages.sort(key=lambda x: x["download_count"] + x["upload_count"], reverse=True)

    return {
        "overview": {
            "package_count": len(snapshot),
            "file_count": len(all_files),
            "total_storage": sum(p["total_size"] for p in per_package.values()),
            "total_storage_human": _human_size(
                sum(p["total_size"] for p in per_package.values())
            ),
            "total_keys": len(key_stats) if key_mgr else 0,
            "active_keys": active_key_count,
            "total_downloads": total_downloads,
            "total_uploads": total_uploads,
        },
        "packages": merged_packages,
        "keys": key_stats,
    }


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


def _human_size(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(size) < 1024:
            return f"{size:.1f} {unit}" if unit != "B" else f"{size} B"
        size /= 1024
    return f"{size:.1f} PB"
