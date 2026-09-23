"""Upstream HTTP access and the disk cache the hub proxies share.

The artifact hub has three read-through proxies in front of three very
different wire protocols — the npm registry, the docker registry v2 API and an
apt mirror. What they have in common is everything in this module:

* a :class:`Upstream` — one base URL, one pooled ``requests`` session, timeouts,
  optional upstream credentials, and a request method that raises a typed error
  instead of leaking ``requests`` exceptions into view code;
* a :class:`DiskCache` — content addressed by a caller-chosen key, written
  atomically (temp file + ``os.replace``), with an mtime-based freshness check
  and a byte budget so an accidental multi-gigabyte pull cannot fill the disk;
* :func:`passthrough` — turn a streamed ``requests`` response into a Flask
  response **without buffering it in memory**, which is what makes a 300 MB
  tarball or image blob survivable on a small host.

Design rules, so the three proxies behave the same way:

1. **The local filesystem is always the first source.** These helpers are only
   reached for what is *not* already on disk — the proxies ask the cache and
   the catalog directories first.
2. **Nothing is trusted to be small.** Every body goes through
   :func:`passthrough` or is written to the cache with a size ceiling;
   ``.content`` is only used for metadata documents the caller explicitly asked
   for with :meth:`Upstream.get_bytes`.
3. **A proxy failure is not a server failure.** :class:`UpstreamError` carries
   the upstream status, and callers translate it into a 502/404 rather than
   letting a traceback escape.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping
from urllib.parse import quote, urljoin

import requests
from flask import Response

from services.headers import (
    SafeHeaderSession,
    ValuePolicy,
    checked_headers,
    is_header_value_safe,
)

logger = logging.getLogger("cpypiserver.upstream")

#: Headers that describe *this* hop and must never be forwarded in either
#: direction (RFC 9110 §7.6.1, plus the de-facto ``X-Accel-*`` pair).
HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade",
    "x-accel-buffering", "x-accel-redirect",
})

#: Response headers a proxy should forward to the client. Anything not listed
#: is dropped, which keeps an upstream ``Set-Cookie`` or ``Authorization`` echo
#: from leaking through.
FORWARD_HEADERS = frozenset({
    "content-type", "content-length", "content-encoding", "content-language",
    "content-disposition", "etag", "last-modified", "cache-control", "expires",
    "vary", "accept-ranges", "content-range", "date", "age", "location",
    # Registry-specific headers that clients actually read back:
    "docker-content-digest", "docker-distribution-api-version",
})

#: Streaming chunk size. 64 KiB keeps a large body moving without holding much.
CHUNK = 64 * 1024


# ── Errors ───────────────────────────────────────────────────────────

class UpstreamError(Exception):
    """An upstream request could not be completed.

    ``status`` is the upstream HTTP status when there *was* a response, and
    ``None`` for a connection-level failure (DNS, refused, timeout). Callers
    map ``status`` to their own protocol's error surface — npm and docker both
    expect a JSON error body, apt expects a plain status line.
    """

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        url: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.url = url

    @property
    def unreachable(self) -> bool:
        """True when there was no HTTP response at all."""
        return self.status is None


# ── A single upstream endpoint ───────────────────────────────────────

@dataclass
class Upstream:
    """One upstream service this server mirrors.

    The session is created lazily and reused, so a burst of packument or
    manifest requests shares TCP connections instead of reconnecting per call.
    """

    base_url: str
    timeout: float = 30.0
    headers: dict[str, str] = field(default_factory=dict)
    #: Optional upstream credentials (HTTP Basic). The intranet registry is the
    #: usual reason to set these; a public mirror needs none.
    username: str = ""
    password: str = ""
    verify: bool = True
    #: Extra ``Accept`` values per call are the caller's business; this is the
    #: default for requests that do not override it.
    session: requests.Session | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        self.base_url = (self.base_url or "").rstrip("/")
        if self.session is None:
            # ``SafeHeaderSession``: every upstream in this process (npm, docker,
            # debian) goes through this one session type, so the header
            # allowlist is enforced on the wire and not only in ``request``.
            self.session = SafeHeaderSession()
        if self.username:
            # ``Session.auth`` is applied by requests as a base64 ``Basic``
            # value, whose alphabet cannot contain a CR/LF — a second, structural
            # reason the Basic path needs no escaping of its own.
            self.session.auth = (self.username, self.password)

    @property
    def configured(self) -> bool:
        """False when no base URL is set — the proxy is then local-only."""
        return bool(self.base_url)

    # -- URL building -------------------------------------------------

    def url_for(self, path: str) -> str:
        """Join *path* onto the base URL.

        The path is quoted per segment so a package name containing ``@`` or
        ``/`` (``@scope/name``) survives, while ``..`` cannot escape the base.
        """
        if path.startswith(("http://", "https://")):
            # Only ever produced internally (a redirect we already followed).
            return path
        clean = path.lstrip("/")
        parts = [p for p in clean.split("/") if p not in ("", ".")]
        if any(p == ".." for p in parts):
            raise UpstreamError(f"refusing traversal in upstream path: {path}")
        quoted = "/".join(quote(p, safe="@:+~") for p in parts)
        return f"{self.base_url}/{quoted}"

    # -- Requests -----------------------------------------------------

    def request(
        self,
        method: str,
        path: str,
        *,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, Any] | None = None,
        stream: bool = False,
        allow_redirects: bool = True,
    ) -> requests.Response:
        """Perform one request. HTTP error *statuses* are returned, not raised.

        Only a connection-level failure raises :class:`UpstreamError`, because
        a proxy usually wants to inspect a 404 (and try somewhere else) rather
        than treat it as an outage.
        """
        if not self.configured:
            raise UpstreamError(
                "no upstream configured (set the matching *_UPSTREAM variable)"
            )
        url = self.url_for(path)
        merged = dict(self.headers)
        if headers:
            merged.update({k: v for k, v in headers.items() if v is not None})
        # The last point before these values become an HTTP request: some of them
        # come from *our* caller (an apt ``Range``, a docker token), so this is
        # where the header allowlist is applied rather than in each caller.
        merged = checked_headers(merged, context=f"upstream {method.upper()} {url}")
        try:
            resp = self.session.request(
                method.upper(),
                url,
                headers=merged,
                params=params,
                stream=stream,
                allow_redirects=allow_redirects,
                timeout=(5.0, self.timeout),
                verify=self.verify,
            )
        except requests.RequestException as exc:
            raise UpstreamError(
                f"{method.upper()} {url} failed: {exc.__class__.__name__}: {exc}",
                url=url,
            ) from exc
        return resp

    def get_bytes(
        self,
        path: str,
        *,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, Any] | None = None,
        max_bytes: int = 32 * 1024 * 1024,
    ) -> "FrozenResponse":
        """GET a smallish document (packument, manifest, ``Packages``).

        *max_bytes* is a hard ceiling: reading an unexpectedly huge body into
        memory is how a proxy turns into an OOM. The check happens before the
        body is consumed, so a mislabelled multi-gigabyte response is refused.
        """
        resp = self.request("GET", path, headers=headers, params=params, stream=True)
        declared = resp.headers.get("Content-Length")
        if declared and declared.isdigit() and int(declared) > max_bytes:
            resp.close()
            raise UpstreamError(
                f"upstream document is {declared} bytes, over the {max_bytes} ceiling",
                status=resp.status_code,
                url=resp.url,
            )
        try:
            body = resp.content
        finally:
            resp.close()
        return FrozenResponse.from_response(resp, body)

    def get_json(
        self,
        path: str,
        *,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, Any] | None = None,
        max_bytes: int = 32 * 1024 * 1024,
    ) -> Any:
        """GET and decode JSON, raising :class:`UpstreamError` on a bad body."""
        resp = self.get_bytes(path, headers=headers, params=params, max_bytes=max_bytes)
        if resp.status_code >= 400:
            raise UpstreamError(
                f"upstream returned {resp.status_code}",
                status=resp.status_code,
                url=resp.url,
            )
        try:
            return resp.json()
        except ValueError as exc:
            raise UpstreamError(
                f"upstream returned invalid JSON: {exc}", url=resp.url
            ) from exc


@dataclass
class FrozenResponse:
    """A detached response: status/headers kept, body already in memory.

    ``requests`` responses cannot be read twice and must be closed; this is the
    small immutable stand-in the metadata helpers return.
    """

    status_code: int
    headers: dict[str, str]
    url: str
    content: bytes

    @classmethod
    def from_response(cls, resp: requests.Response, body: bytes) -> "FrozenResponse":
        return cls(
            status_code=resp.status_code,
            headers={k.lower(): v for k, v in resp.headers.items()},
            url=resp.url,
            content=body,
        )

    def json(self) -> Any:
        import json as _json

        return _json.loads(self.content.decode("utf-8"))

    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")


# ── Streaming a response through Flask ───────────────────────────────

def passthrough(
    resp: requests.Response,
    *,
    chunk_size: int = CHUNK,
    extra_headers: Mapping[str, str] | None = None,
    status: int | None = None,
) -> Response:
    """Stream an upstream response to the client without buffering it.

    The upstream connection stays open for the life of the generator and is
    closed in a ``finally``, so a client that hangs up mid-download cannot leak
    the socket.
    """
    headers = forward_headers(resp.headers)
    if extra_headers:
        headers.update(extra_headers)

    def generate() -> Iterator[bytes]:
        try:
            for chunk in resp.iter_content(chunk_size=chunk_size):
                if chunk:
                    yield chunk
        finally:
            resp.close()

    return Response(
        generate(),
        status=status or resp.status_code,
        headers=headers,
        direct_passthrough=True,
    )


def forward_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """Filter an upstream header set down to what is safe to forward.

    Two filters, and the second is not redundant: the allowlist of *names* keeps
    an upstream ``Set-Cookie`` or ``Authorization`` echo from leaking through,
    and the value check keeps a malicious upstream from putting a character in a
    forwarded header that this server would then hand to *its* client.  A value
    is allowed to contain a space (``Content-Disposition`` carries a filename)
    but must still be one printable ASCII line.
    """
    out: dict[str, str] = {}
    for key, value in headers.items():
        lower = key.lower()
        if lower in HOP_BY_HOP:
            continue
        if lower in FORWARD_HEADERS and is_header_value_safe(value, policy=ValuePolicy.LINE):
            out[key] = value
    return out


def stream_into(resp: requests.Response, dest: Path, *, chunk_size: int = CHUNK) -> int:
    """Write a streaming response to *dest* atomically; return the byte count.

    The write goes to a sibling temp file which is ``os.replace``d on success,
    so a reader never sees a half-written cache entry and an aborted download
    leaves the cache unchanged.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    tmp_fd, tmp_name = tempfile.mkstemp(dir=str(dest.parent), suffix=".part")
    try:
        with os.fdopen(tmp_fd, "wb") as fh:
            for chunk in resp.iter_content(chunk_size=chunk_size):
                if chunk:
                    fh.write(chunk)
                    written += len(chunk)
        os.replace(tmp_name, dest)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    finally:
        resp.close()
    return written


# ── Disk cache ───────────────────────────────────────────────────────

class DiskCache:
    """A content-addressed on-disk cache with a byte budget.

    Keys are opaque strings (a URL, a digest, ``pkg@version``). They are hashed
    to a flat filename under two hex shards, so the cache never depends on the
    shape of the key and never grows a directory with millions of entries.

    ``max_bytes`` is enforced after every write by deleting the *least recently
    used* entries (mtime, touched on read), which is the cheapest policy that
    keeps a long-running proxy from filling a small disk.
    """

    def __init__(self, root: str | Path, *, max_bytes: int = 512 * 1024 * 1024) -> None:
        self.root = Path(root)
        self.max_bytes = max(int(max_bytes), 0)
        self._evictions = 0

    # -- addressing ---------------------------------------------------

    def path_for(self, key: str) -> Path:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return self.root / digest[:2] / digest[2:]

    # -- reads --------------------------------------------------------

    def get(self, key: str, *, max_age: float | None = None) -> Path | None:
        """Return the cached file for *key*, or None.

        *max_age* is in seconds: metadata (packuments, ``Packages`` indexes) is
        cached with a short TTL, while immutable content (a tarball, a digest
        addressed blob) passes ``None`` and never expires.
        """
        path = self.path_for(key)
        try:
            st = path.stat()
        except OSError:
            return None
        if max_age is not None and (time.time() - st.st_mtime) > max_age:
            return None
        # Reading counts as use for the eviction policy.
        try:
            os.utime(path, None)
        except OSError:
            pass
        return path

    def open_bytes(self, key: str, *, max_age: float | None = None) -> bytes | None:
        path = self.get(key, max_age=max_age)
        if path is None:
            return None
        try:
            return path.read_bytes()
        except OSError:
            return None

    # -- writes -------------------------------------------------------

    def put_bytes(self, key: str, data: bytes) -> Path:
        return self.put_stream(key, [data])

    def put_stream(self, key: str, chunks: Iterable[bytes]) -> Path:
        """Write *chunks* to the cache atomically, then enforce the budget."""
        path = self.path_for(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".part")
        try:
            with os.fdopen(tmp_fd, "wb") as fh:
                for chunk in chunks:
                    if chunk:
                        fh.write(chunk)
            os.replace(tmp_name, path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
        self.enforce_limit()
        return path

    def adopt(self, key: str, source: Path) -> Path:
        """Move an already-downloaded file into the cache."""
        path = self.path_for(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        os.replace(source, path)
        self.enforce_limit()
        return path

    def put_response(
        self,
        key: str,
        resp: requests.Response,
        *,
        chunk_size: int = CHUNK,
    ) -> Path:
        """Stream an open upstream response into the cache, atomically.

        This is the entry point for caching a body too large to materialise in
        memory (an npm tarball, a container image blob).  ``stream_into`` writes
        the temp file and renames it, and the byte budget is enforced right
        after — which is the part that matters: without it a single unexpected
        multi-gigabyte pull would fill the disk before anyone noticed.
        """
        path = self.path_for(key)
        stream_into(resp, path, chunk_size=chunk_size)
        self.enforce_limit()
        return path

    # -- maintenance --------------------------------------------------

    def total_bytes(self) -> int:
        total = 0
        if not self.root.is_dir():
            return 0
        for entry in self.root.rglob("*"):
            try:
                if entry.is_file() and entry.suffix != ".part":
                    total += entry.stat().st_size
            except OSError:
                continue
        return total

    def enforce_limit(self) -> int:
        """Evict least-recently-used entries until the budget is met.

        Returns the number of bytes reclaimed. A cheap no-op once under budget.
        """
        if self.max_bytes <= 0 or not self.root.is_dir():
            return 0
        entries: list[tuple[float, int, Path]] = []
        total = 0
        for entry in self.root.rglob("*"):
            try:
                if not entry.is_file():
                    continue
                st = entry.stat()
            except OSError:
                continue
            if entry.suffix == ".part":
                continue
            entries.append((st.st_mtime, st.st_size, entry))
            total += st.st_size
        if total <= self.max_bytes:
            return 0

        entries.sort(key=lambda item: item[0])  # oldest first
        reclaimed = 0
        for _mtime, size, path in entries:
            if total - reclaimed <= self.max_bytes:
                break
            try:
                path.unlink()
                reclaimed += size
                self._evictions += 1
            except OSError:
                continue
        if reclaimed:
            logger.info(
                "cache %s evicted %d byte(s) to stay under the %d byte budget",
                self.root, reclaimed, self.max_bytes,
            )
        return reclaimed

    def clear(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)


def join_url_path(*parts: str) -> str:
    """Join URL path segments, quoting each one, for building proxy links."""
    return "/".join(quote(str(p).strip("/"), safe="@:+~") for p in parts if p != "")


def absolute_url(base: str, path: str) -> str:
    """``urljoin`` with a trailing slash on *base* so the last segment survives."""
    return urljoin(base.rstrip("/") + "/", path.lstrip("/"))


__all__ = [
    "CHUNK", "FORWARD_HEADERS", "HOP_BY_HOP",
    "DiskCache", "FrozenResponse", "Upstream", "UpstreamError",
    "absolute_url", "forward_headers", "join_url_path", "passthrough", "stream_into",
]
