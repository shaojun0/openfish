"""Helpers shared by the artifact-hub blueprints.

The hub grew from one blueprint into one blueprint *per ecosystem* — ``tools``
and ``models`` stay in :mod:`routes.hub`, while npm, docker and debian moved to
their own modules so a protocol implementation (npm registry, docker registry,
apt) lives next to the catalog page it belongs to.

What is left here is the handful of things all of them do identically:
content negotiation for the server-rendered index pages, and building a
browser-facing SPA link that honours the global route prefix.
"""

from __future__ import annotations

from flask import request

from config import settings


def wants_json() -> bool:
    """Content negotiation for the index pages, mirroring ``/simple/``.

    Returns True when the caller asked for JSON either explicitly
    (``?format=json``) or through the ``Accept`` header, which is what the
    static indexes and scripts use.
    """
    return (
        request.args.get("format") == "json"
        or "application/json" in request.headers.get("Accept", "")
    )


def spa_url(path: str) -> str:
    """Absolute-ish path of an SPA page, honouring the global route prefix."""
    return settings.server.route_prefix.rstrip("/") + path


__all__ = ["wants_json", "spa_url"]
