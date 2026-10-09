"""Helpers shared by the artifact-hub blueprints.

The hub grew from one blueprint into one blueprint *per ecosystem* — ``tools``
and ``models`` stay in :mod:`routes.hub`, while npm, docker and debian moved to
their own modules so a protocol implementation (npm registry, docker registry,
apt) lives next to the catalog page it belongs to.

What is left here is the handful of things all of them do identically:
content negotiation for the server-rendered index pages, building a
browser-facing SPA link that honours the global route prefix, and the two
primitives a catalog download needs — the size gate a bound body has to pass
*before* the binder reads it, and the streamed response that carries the HTTP
semantics a file download implies.
"""

from __future__ import annotations

from functools import wraps

from typing import IO

from flask import Response, request, send_file

from config import settings
from errors import BadRequestError
from services.objectstore import ObjectInfo, etag_of


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


def stream_object(
    info: ObjectInfo,
    stream: IO[bytes],
    *,
    mimetype: str | None = None,
    as_attachment: bool = False,
    download_name: str | None = None,
) -> Response:
    """Stream one stored object with the HTTP semantics a download implies.

    ``send_file`` cannot size a stream opened by a *store* — it reads
    ``Content-Length``, ``Last-Modified``, ``ETag`` and ``Range`` off a path —
    so handing it ``store.open(key)`` silently drops all four, and those are
    exactly the headers a client resuming a multi-gigabyte artifact depends on.
    The store's :class:`~services.objectstore.ObjectInfo` already knows the size
    and the modification time, so this builds the response explicitly and lets
    Werkzeug's own ``make_conditional`` do the
    ``If-None-Match``/``If-Modified-Since``/``Range`` work.

    ``send_file`` still provides what it is good at without a path: media-type
    guessing from the download name and RFC 5987 encoding of a non-ASCII
    filename.  The download name falls back to the key's last segment, which is
    also what stops the response from leaking the absolute path a local store
    opened the stream from.
    """
    response = send_file(
        stream,
        mimetype=mimetype,
        as_attachment=as_attachment,
        download_name=download_name or info.key.rsplit("/", 1)[-1],
        # Both computed here, from what the store knows — see above.
        conditional=False,
        etag=False,
    )
    response.content_length = info.size
    if info.modified is not None:
        response.last_modified = info.modified
        response.set_etag(etag_of(info))
    # `complete_length` is not optional: Werkzeug only completes a Range
    # request when it is told the full length, even though `content_length` is
    # already set here.  Without it a `Range` request silently answers 200 with
    # the whole body instead of 206.
    response.make_conditional(request, accept_ranges=True, complete_length=info.size)
    return response


__all__ = ["body_ceiling", "stream_object", "wants_json", "spa_url"]
