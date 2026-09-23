"""apt mirror proxy — the read-through ``dists/`` and ``pool/`` half of Debian.

The Debian ecosystem has two very different kinds of object and they must be
handled differently, which is the whole reason this service exists:

* **Metadata** (``Release``, ``InRelease``, ``Packages`` and its ``.gz`` / ``.xz``
  variants, ``by-hash/...``) is small, is fetched by every apt client, and is
  rewritten on the mirror's own schedule.  It is *cached on disk* with a TTL so
  the second client in an intranet never touches the upstream mirror, and it is
  served byte-for-byte — apt verifies the signature and the hashes in
  ``Release``, so a document that was transparently decompressed or re-encoded
  on the way through is a hard failure, not a cosmetic one.
* **Package files** (``pool/.../*.deb``) are large and immutable.  They are
  streamed straight through, never cached, with ``Range`` forwarded upstream so
  a resumed ``apt install`` works.

Both trees are *local-first*: a regular file under ``DEBIAN_DIR/dists`` or
``DEBIAN_DIR/pool`` is served directly (a synced archive tree makes the intranet
work offline), and only a local miss falls through to the upstream logic above.

The shared machinery lives in :mod:`services.upstream` (``Upstream``,
``DiskCache``, ``passthrough``); what is added here is the apt-specific policy:
which URL is the effective upstream, what a safe mirror-relative path is, how a
cached document keeps its upstream headers, and how an upstream failure is
turned into a JSON ``502`` instead of a traceback.

Cached documents are stored as a tiny self-describing envelope::

    b"DFAPT1\\n" + <4-byte big-endian header length> + <JSON header> + <body>

Data and the ``Content-Type`` / ``Content-Encoding`` it arrived with stay
together, one file per document, so the shared LRU eviction treats a document
as a single entry.
"""

from __future__ import annotations

import json
import struct
from pathlib import Path
from typing import Iterator

from flask import Response, jsonify, request, send_from_directory
from requests import RequestException
from urllib3.exceptions import HTTPError as Urllib3HTTPError

from config import settings
from services.headers import ValuePolicy, is_header_value_safe
from services.upstream import (
    CHUNK,
    DiskCache,
    Upstream,
    UpstreamError,
    forward_headers,
    passthrough,
)


#: Envelope magic and the 4-byte big-endian length prefix of its JSON header.
_MAGIC = b"DFAPT1\n"
_U32 = struct.Struct(">I")

#: Upstream documents larger than this are streamed to the client uncached.
#: apt mirrors also carry ``Contents-*`` files that can be a hundred megabytes;
#: buffering or caching one is not worth it, and the LRU could evict the entry
#: it just wrote on a small budget anyway.
_MAX_CACHED_BYTES = 64 * 1024 * 1024

#: Response headers worth keeping in a cached envelope.  ``content-length`` is
#: deliberately absent: it is recomputed from the cached body, which is what
#: keeps a wrong ``Content-Length`` from surviving a stale entry.
_CACHEABLE_HEADERS = frozenset({
    "content-type", "content-encoding", "content-language", "content-disposition",
    "etag", "last-modified", "cache-control", "expires", "vary",
    "accept-ranges", "content-range", "location",
})

#: One ``Upstream``/``DiskCache`` per distinct configuration, built lazily so a
#: test (or a reload) that changes ``settings.hub`` gets the new configuration
#: without restarting the process.
_upstreams: dict[tuple[str, float], Upstream] = {}
_caches: dict[tuple[str, int], DiskCache] = {}


# ── Configuration ────────────────────────────────────────────────────

def effective_upstream() -> str:
    """The apt mirror actually proxied, or ``""`` for local-only mode.

    ``DEBIAN_UPSTREAM`` wins; when it is empty, ``DEBIAN_MIRROR`` is used if it
    looks like a URL.  That fallback is what lets a deployment advertise one
    mirror in the UI and have the proxy start using it without a second setting.
    """
    upstream = (settings.hub.debian_upstream or "").strip()
    if upstream:
        return upstream.rstrip("/")
    mirror = (settings.hub.debian_mirror or "").strip()
    if mirror.startswith(("http://", "https://")):
        return mirror.rstrip("/")
    return ""


def configured() -> bool:
    """True when ``dists/`` and ``pool/`` can be proxied."""
    return bool(effective_upstream())


def upstream() -> Upstream:
    """The configured :class:`Upstream`, reusing its connection pool."""
    base = effective_upstream()
    key = (base, float(settings.hub.debian_timeout))
    instance = _upstreams.get(key)
    if instance is None:
        instance = Upstream(
            base,
            timeout=float(settings.hub.debian_timeout),
            # apt compares bytes; ask the mirror for an unencoded body so a CDN
            # cannot gzip a ``Release`` behind our back.
            headers={"Accept-Encoding": "identity"},
        )
        _upstreams[key] = instance
    return instance


def cache() -> DiskCache:
    """The metadata :class:`DiskCache`, honouring the configured byte budget."""
    root = str(settings.hub.debian_cache_dir)
    max_bytes = max(int(settings.hub.debian_cache_max_mb), 0) * 1024 * 1024
    key = (root, max_bytes)
    instance = _caches.get(key)
    if instance is None:
        instance = DiskCache(root, max_bytes=max_bytes)
        _caches[key] = instance
    return instance


# ── Path safety ──────────────────────────────────────────────────────

class PathError(ValueError):
    """A mirror-relative path that is unsafe or empty."""


def safe_mirror_path(path: str) -> str:
    """Normalise a mirror-relative path, refusing anything that could escape.

    The value comes from a ``<path:...>`` converter, so it may contain slashes;
    ``..``, backslashes and NUL are rejected *before* the value is used as a
    cache key or handed to :meth:`Upstream.url_for`, which refuses traversal on
    its own but would otherwise raise later than we want to answer.
    """
    raw = (path or "").strip()
    if not raw:
        raise PathError("empty upstream path")
    if ".." in raw or "\\" in raw or "\x00" in raw:
        raise PathError(f"refusing unsafe upstream path: {path}")
    if raw.startswith("/"):
        raise PathError(f"refusing absolute upstream path: {path}")
    segments = [segment for segment in raw.split("/") if segment not in ("", ".")]
    if not segments:
        raise PathError(f"empty upstream path: {path}")
    return "/".join(segments)


def local_mirror_file(kind: str, safe: str) -> Path | None:
    """Return ``<DEBIAN_DIR>/<kind>/<safe>`` when it is a local regular file.

    ``kind`` is ``"dists"`` or ``"pool"``; ``safe`` must already be normalised by
    :func:`safe_mirror_path`.  Returns ``None`` when the local tree has no such
    file, so a caller can fall back to the upstream mirror — which is exactly
    what the offline relay needs when it walks a synced ``dists/`` tree before
    reaching for the network.  The resolved path is checked against the resolved
    root so a symlink inside the tree cannot point outside it.
    """
    root = Path(settings.hub.debian_dir) / kind
    try:
        resolved = (root / safe).resolve()
        resolved.relative_to(root.resolve())
    except (OSError, RuntimeError, ValueError) as exc:
        raise PathError(f"refusing local mirror path outside {kind}/: {safe}") from exc
    return resolved if resolved.is_file() else None


def _local_mirror_file(kind: str, safe: str) -> Response | None:
    """Serve ``<DEBIAN_DIR>/<kind>/<safe>`` from the local mirror tree.

    Returns ``None`` when the local tree has no such regular file, so the caller
    can fall back to the upstream mirror.  ``send_from_directory(...,
    conditional=True)`` supplies the ``Range`` / ``HEAD`` / ``Content-Length`` /
    ``Last-Modified`` handling.
    """
    resolved = local_mirror_file(kind, safe)
    if resolved is None:
        return None
    return send_from_directory(Path(settings.hub.debian_dir) / kind, safe, conditional=True)


# ── Cache envelope ───────────────────────────────────────────────────

def _encode_envelope(header: dict[str, str], status: int = 200) -> bytes:
    kept = {
        key.lower(): value
        for key, value in header.items()
        if key.lower() in _CACHEABLE_HEADERS
    }
    payload = json.dumps(
        {"status": status, "headers": kept}, separators=(",", ":")
    ).encode("utf-8")
    return _MAGIC + _U32.pack(len(payload)) + payload


def _decode_envelope(path: Path) -> tuple[int, dict[str, str], int]:
    """Return ``(status, headers, body_offset)`` for a cached document."""
    with path.open("rb") as handle:
        prefix = handle.read(len(_MAGIC) + _U32.size)
        if len(prefix) != len(_MAGIC) + _U32.size or not prefix.startswith(_MAGIC):
            raise ValueError("bad cache envelope")
        length = _U32.unpack(prefix[len(_MAGIC):])[0]
        payload = handle.read(length)
        if len(payload) != length:
            raise ValueError("truncated cache envelope")
    data = json.loads(payload.decode("utf-8"))
    # The envelope is a local file, but the values in it are *upstream* headers
    # that were stored verbatim, and they are about to be replayed to a client:
    # only the ones that are still one printable line are kept, so a tampered or
    # legacy envelope cannot inject a header into the response.
    stored = {str(key): str(value) for key, value in (data.get("headers") or {}).items()}
    headers = {
        key: value
        for key, value in stored.items()
        if is_header_value_safe(value, policy=ValuePolicy.LINE)
    }
    return int(data.get("status", 200)), headers, len(_MAGIC) + _U32.size + length


def _stream_file(path: Path, offset: int) -> Iterator[bytes]:
    with path.open("rb") as handle:
        handle.seek(offset)
        while True:
            chunk = handle.read(CHUNK)
            if not chunk:
                break
            yield chunk


def _cached_response(path: Path, status: int, headers: dict[str, str], offset: int) -> Response:
    """Serve a cached document, preserving its stored upstream headers."""
    length = max(path.stat().st_size - offset, 0)
    headers = dict(headers)
    headers["Content-Length"] = str(length)
    headers.setdefault("Accept-Ranges", "bytes")
    return Response(
        _stream_file(path, offset),
        status=status,
        headers=headers,
        direct_passthrough=True,
    )


def _raw_passthrough(resp) -> Response:
    """Stream a response without buffering and *without* decoding its encoding.

    ``Response.iter_content`` transparently undoes ``Content-Encoding``; that
    is wrong for an apt mirror, where a ``.gz`` body is the artifact and the
    header must describe the bytes actually sent.
    """
    def body() -> Iterator[bytes]:
        try:
            for chunk in resp.raw.stream(CHUNK, decode_content=False):
                if chunk:
                    yield chunk
        finally:
            resp.close()

    return Response(
        body(),
        status=resp.status_code,
        headers=forward_headers(resp.headers),
        direct_passthrough=True,
    )


def _error(status: int, message: str) -> Response:
    response = jsonify({"error": message})
    response.status_code = status
    return response


def _local_only() -> Response:
    return _error(
        404,
        "no upstream apt mirror is configured (DEBIAN_UPSTREAM or a URL "
        "DEBIAN_MIRROR); only the local flat repository at /debian/Packages "
        "and /debian/files/ is available",
    )


# ── dists/: cached metadata ──────────────────────────────────────────

def dists_response(path: str) -> Response:
    """Serve apt metadata locally, else proxy it and cache it for the TTL."""
    try:
        safe = safe_mirror_path(path)
    except PathError as exc:
        return _error(400, str(exc))

    try:
        local = _local_mirror_file("dists", safe)
    except PathError as exc:
        return _error(400, str(exc))
    if local is not None:
        return local

    if not configured():
        return _local_only()

    target = f"dists/{safe}"
    store = cache()
    key = f"apt:{effective_upstream()}|{target}"

    cached = store.get(key, max_age=int(settings.hub.debian_metadata_ttl))
    if cached is not None:
        try:
            status, headers, offset = _decode_envelope(cached)
        except (OSError, ValueError) as exc:
            pass
        else:
            return _cached_response(cached, status, headers, offset)

    return _fetch_metadata(store, key, target)


def _fetch_metadata(store: DiskCache, key: str, target: str) -> Response:
    try:
        resp = upstream().request("GET", target, stream=True)
    except UpstreamError as exc:
        return _error(502, "apt upstream unreachable")

    status = resp.status_code
    if status >= 400:
        resp.close()
        mapped = status if status < 500 else 502
        return _error(mapped, "upstream apt mirror returned an unexpected response")
    if status != 200:
        resp.close()
        return _error(502, "unexpected upstream status")

    declared = resp.headers.get("Content-Length")
    declared_bytes = int(declared) if declared and declared.isdigit() else None
    budget = store.max_bytes
    too_big = declared_bytes is not None and (
        declared_bytes > _MAX_CACHED_BYTES
        or (budget > 0 and declared_bytes > budget)
    )
    if too_big:
        return _raw_passthrough(resp)

    envelope = _encode_envelope(forward_headers(resp.headers))

    def chunks() -> Iterator[bytes]:
        yield envelope
        for chunk in resp.raw.stream(CHUNK, decode_content=False):
            if chunk:
                yield chunk

    try:
        written = store.put_stream(key, chunks())
    except (RequestException, Urllib3HTTPError, OSError) as exc:
        resp.close()
        return _error(502, "apt upstream failed while reading")
    finally:
        resp.close()

    try:
        status, headers, offset = _decode_envelope(written)
    except (OSError, ValueError) as exc:
        return _error(502, "apt metadata exceeded the cache budget")

    return _cached_response(written, status, headers, offset)


# ── pool/: streamed packages ─────────────────────────────────────────

def pool_response(path: str, method: str = "GET") -> Response:
    """Serve a package locally, else proxy it and forward ``Range`` upstream."""
    try:
        safe = safe_mirror_path(path)
    except PathError as exc:
        return _error(400, str(exc))

    try:
        local = _local_mirror_file("pool", safe)
    except PathError as exc:
        return _error(400, str(exc))
    if local is not None:
        return local

    if not configured():
        return _local_only()

    target = f"pool/{safe}"
    upstream_headers: dict[str, str] = {}
    byte_range = request.headers.get("Range")
    if byte_range:
        upstream_headers["Range"] = byte_range

    client = upstream()
    try:
        if method.upper() == "HEAD":
            resp = client.request("HEAD", target, headers=upstream_headers)
            status = resp.status_code
            forwarded = forward_headers(resp.headers) if status < 400 else {}
            resp.close()
            if status >= 400:
                mapped = status if status < 500 else 502
                return _error(mapped, "upstream apt mirror returned an unexpected response")
            # No body argument: a plain ``Response`` would rewrite
            # ``Content-Length`` to 0, and apt's HEAD probe wants the real size.
            return Response(status=status, headers=forwarded)
        resp = client.request("GET", target, headers=upstream_headers, stream=True)
    except UpstreamError as exc:
        return _error(502, "apt upstream unreachable")

    if resp.status_code >= 400:
        status = resp.status_code
        resp.close()
        mapped = status if status < 500 else 502
        return _error(mapped, "upstream apt mirror returned an unexpected response")

    return passthrough(resp)


__all__ = [
    "PathError",
    "cache",
    "configured",
    "dists_response",
    "effective_upstream",
    "local_mirror_file",
    "pool_response",
    "safe_mirror_path",
    "upstream",
]
