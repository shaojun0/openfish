"""Display formatting — one implementation per thing that gets rendered.

The hub, the docs browser and the statistics panel all show a byte count and a
timestamp, and they used to each carry their own copy of the formatter.  They
now share these three functions, so "1.5 MB" and an ISO-8601 UTC instant are
spelled the same way everywhere.
"""

from __future__ import annotations

from datetime import datetime, timezone


def human_size(num: float) -> str:
    """A byte count with binary units, e.g. ``1536`` → ``"1.5 KB"``."""
    step = 1024.0
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num) < step or unit == "TB":
            return f"{int(num)} {unit}" if unit == "B" else f"{num:.1f} {unit}"
        num /= step
    return f"{num:.1f} TB"


def iso_from_timestamp(ts: float) -> str:
    """A filesystem timestamp as an ISO-8601 UTC string."""
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def utc_now_iso() -> str:
    """The current instant as an ISO-8601 UTC string."""
    return datetime.now(tz=timezone.utc).isoformat()


__all__ = ["human_size", "iso_from_timestamp", "utc_now_iso"]
