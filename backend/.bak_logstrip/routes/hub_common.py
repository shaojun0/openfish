"""Helpers shared by the artifact-hub blueprints.

The hub grew from one blueprint into one blueprint *per ecosystem* — ``tools``
and ``models`` stay in :mod:`routes.hub`, while npm, docker and debian moved to
their own modules so a protocol implementation (npm registry, docker registry,
apt) lives next to the catalog page it belongs to.

What is left here is the handful of things all of them do identically:
content negotiation for the server-rendered index pages, building a
browser-facing SPA link that honours the global route prefix, and the size gate a
bound request body has to pass *before* the binder reads it.
"""

from __future__ import annotations

from functools import wraps

from flask import request

from config import settings
from errors import BadRequestError


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


def body_ceiling(max_bytes: int, message: str):
    """Refuse an oversized request body *before* the binder reads it.

    A route that caps its body (the model-route writes, the repository
    import/sync/runner writes) has to place this decorator **between** the guard
    and ``@validate_request()``: once the binder has run, an attacker has already
    made the server parse — and pydantic already copy — a body the route exists
    to refuse, and a ceiling applied inside the view would be a report rather
    than a gate.  It reads only ``Content-Length`` and never the body, so it is
    not a request reader; it is a size gate.

    A factory rather than a decorator because the two ceilings differ in size,
    and the message is the route's own (``400``, not the ``413`` Flask answers
    for ``MAX_CONTENT_LENGTH``).
    """

    def decorator(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            length = request.content_length
            if length and length > max_bytes:
                raise BadRequestError(message)
            return view(*args, **kwargs)

        return wrapper

    return decorator


__all__ = ["body_ceiling", "wants_json", "spa_url"]
