"""Docker Registry v2 read-through proxy — upstream auth, tokens, cache, Range.

This is the engine behind the ``/docker/v2/*`` routes.  It fronts a
configurable upstream registry (``https://registry-1.docker.io`` for Docker Hub,
or an intranet registry) and keeps a local disk cache in front of it, so a
client that can reach *this* server can pull anything the upstream has while the
first host to ask pays for the bandwidth.

What lives here
---------------
* **The upstream auth handshake.**  Docker Hub answers an unauthenticated request
  with ``401`` + a ``WWW-Authenticate: Bearer realm=…,service=…,scope=…``
  challenge.  :meth:`DockerRegistryProxy._request` parses that challenge, fetches
  a token from the realm (HTTP Basic when ``DOCKER_UPSTREAM_USERNAME/PASSWORD``
  are set), caches it until just before it expires, and retries the original
  request once with ``Authorization: Bearer …``.  Upstream credentials and
  tokens never leave this module — they are never logged, and they are never
  echoed to the client.
* **Manifest caching.**  A manifest is small, so it is fetched whole, hashed for
  ``Docker-Content-Digest`` and written under the key ``<name>@<reference>``.  A
  *tag* reference is cached with a short TTL (:data:`TAG_TTL`); a *digest*
  reference names immutable bytes and never expires.
* **Blob caching + Range.**  Blobs are content-addressed, so they are cached
  under their digest permanently (subject to the byte budget).  A cached blob is
  served from disk and answers HTTP ``Range`` with ``206`` + ``Content-Range``;
  an uncached blob is streamed from upstream to the client while the same bytes
  are written to the cache, then the budget is enforced.

Scope limitation (deliberate)
-----------------------------
``DOCKER_DIR`` still holds ``docker save`` tarballs for the air-gapped path, and
those files contribute repository names and tags to ``/v2/_catalog`` and
``/v2/<name>/tags/list``.  They are **not** unpacked and served as registry
manifests/blobs: the registry protocol paths only answer from the upstream proxy
and from the cache this module maintains.  A production implementation would
need to read the ``manifest.json``/``layer.tar`` members of each saved image; the
task this module was written for explicitly leaves that out of scope.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlsplit

from flask import Response

from config import settings
from services.hub import _parse_docker_filename
from services.upstream import (
    CHUNK, DiskCache, Upstream, UpstreamError, forward_headers, passthrough,
)

log = logging.getLogger("cpypiserver.docker")

#: Manifest documents are metadata and therefore small; this is the ceiling on
#: what may be read into memory.  Docker Hub manifest lists are a few KiB.
MANIFEST_MAX_BYTES = 32 * 1024 * 1024

#: A tag is mutable, so a cached tag→manifest mapping is trusted for this long
#: before it is re-fetched.  Digest references are immutable and use no TTL.
TAG_TTL = 300.0

#: Fetch a new upstream token this many seconds before the old one expires, so a
#: request never races the expiry boundary.
TOKEN_SKEW = 60.0

#: Fallback token lifetime for a realm that does not send ``expires_in``.
DEFAULT_TOKEN_TTL = 300.0

#: Upstream tag pagination is followed for at most this many pages.
MAX_TAG_PAGES = 4

_DEFAULT_MANIFEST_TYPE = "application/vnd.docker.distribution.manifest.v2+json"

#: ``algorithm:hex`` — sha256 in practice, but the OCI grammar allows others.
_DIGEST_RE = re.compile(r"^[a-z0-9]+(?:[.+_-][a-z0-9]+)*:[0-9a-fA-F]{32,}$")

#: Hosts whose single-segment names mean ``<namespace>/<name>``.
_HUB_HOSTS = frozenset({
    "docker.io", "registry-1.docker.io", "index.docker.io", "registry.docker.io",
})

_QUOTED_CHALLENGE_RE = re.compile(r'([a-zA-Z0-9_-]+)\s*=\s*"((?:[^"\\]|\\.)*)"')


# ── Errors ───────────────────────────────────────────────────────────

class DockerRegistryError(Exception):
    """A registry-protocol failure, shaped into a JSON error body by the routes.

    ``status`` is the HTTP status the client should see and ``code`` is the
    OCI error code (``MANIFEST_UNKNOWN``, ``BLOB_UNKNOWN``, ``NAME_UNKNOWN``,
    …).  A proxy failure is translated to a 404/502 here rather than leaking a
    traceback or an upstream 500.
    """

    def __init__(self, message: str, *, status: int = 502, code: str = "UNAVAILABLE") -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code


# ── Small helpers ────────────────────────────────────────────────────

def is_digest(reference: str) -> bool:
    """True when *reference* is content-addressed (``sha256:…``) rather than a tag."""
    return bool(_DIGEST_RE.match(reference or ""))


def expand_name(name: str, upstream_url: str, default_namespace: str) -> str:
    """Resolve a client-facing repository name to the upstream one.

    ``docker pull nginx`` means ``library/nginx`` on Docker Hub, so a
    single-segment name is prefixed with *default_namespace* when the upstream is
    Docker-Hub-like.  The URL the client sees keeps its own name; only the
    upstream request uses the expanded one.  An intranet registry that does not
    namespace official images (``DOCKER_DEFAULT_NAMESPACE=``) is untouched.
    """
    name = (name or "").strip("/")
    if "/" in name or not default_namespace:
        return name
    host = (urlsplit(upstream_url).hostname or "").lower()
    if host in _HUB_HOSTS or host.endswith(".docker.io"):
        return f"{default_namespace}/{name}"
    return name


def sha256_digest(body: bytes) -> str:
    """The OCI digest string for *body*."""
    return "sha256:" + hashlib.sha256(body).hexdigest()


def _basic_auth_header(username: str, password: str) -> str:
    """``Authorization: Basic …`` for an upstream that authenticates with HTTP Basic."""
    raw = f"{username}:{password}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


def _parse_challenge(header: str) -> tuple[str, dict[str, str]]:
    """Split ``Bearer realm="…",service="…"`` into ``("Bearer", {...})``."""
    if not header:
        return "", {}
    scheme, _, rest = header.partition(" ")
    params: dict[str, str] = {}
    for match in _QUOTED_CHALLENGE_RE.finditer(rest or header):
        value = match.group(2).replace('\\"', '"').replace("\\\\", "\\")
        params[match.group(1).lower()] = value
    return scheme.strip(), params


def _link_next(link_header: str) -> str:
    """Return the ``rel="next"`` URL from a ``Link`` header, or an empty string."""
    if not link_header:
        return ""
    for part in link_header.split(","):
        segments = part.split(";")
        url = segments[0].strip()
        if url.startswith("<") and url.endswith(">"):
            url = url[1:-1]
        else:
            continue
        if any('rel="next"' in seg or "rel=next" in seg for seg in segments[1:]):
            return url
    return ""


def _parse_range(header: str, size: int):
    """Parse a single-range ``Range`` header.

    Returns ``(start, end)`` (inclusive, clamped), ``None`` when the header is
    absent/unparseable/multi-range (the caller serves the whole body), or
    ``False`` when the range is unsatisfiable (the caller answers ``416``).
    """
    if not header or not header.strip().lower().startswith("bytes="):
        return None
    spec = header.strip()[len("bytes="):].strip()
    if "," in spec or "-" not in spec:
        return None
    first, _, last = spec.partition("-")
    first, last = first.strip(), last.strip()
    try:
        if first == "":
            suffix = int(last)
            if suffix <= 0:
                return False
            if suffix >= size:
                return (0, size - 1)
            return (size - suffix, size - 1)
        start = int(first)
        end = int(last) if last else size - 1
    except ValueError:
        return None
    if start >= size or end < start:
        return False
    return (start, min(end, size - 1))


def _file_slice(path: Path, start: int, length: int) -> Iterator[bytes]:
    """Yield *length* bytes of *path* from *start*; the file opens lazily."""
    def generate() -> Iterator[bytes]:
        with open(path, "rb") as handle:
            handle.seek(start)
            remaining = length
            while remaining > 0:
                chunk = handle.read(min(CHUNK, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    return generate()


def _empty_body() -> Iterator[bytes]:
    """A streamed empty body: keeps an explicit ``Content-Length`` intact.

    A non-streamed ``Response(b"")`` makes Werkzeug recompute
    ``Content-Length: 0``, which is wrong for ``HEAD``.  An iterator is treated
    as streamed, so the header the caller set survives.
    """
    return iter(())


def _infer_manifest_type(body: bytes) -> str:
    """Read ``mediaType`` out of a manifest, falling back to the v2 default."""
    try:
        document = json.loads(body)
    except ValueError:
        return _DEFAULT_MANIFEST_TYPE
    if isinstance(document, dict):
        media_type = document.get("mediaType")
        if isinstance(media_type, str) and media_type:
            return media_type
    return _DEFAULT_MANIFEST_TYPE


def _manifest_blob_digests(body: bytes) -> list[str]:
    """The config/layer digests a manifest references.

    Used to link a repository to the blobs it legitimately contains, so a
    cached blob can be served for *that* repository without being leaked to an
    unrelated one.  A manifest list has no layers; its children are manifests,
    not blobs, and are deliberately not linked here.
    """
    try:
        document = json.loads(body)
    except ValueError:
        return []
    if not isinstance(document, dict):
        return []
    found: list[str] = []
    config = document.get("config")
    if isinstance(config, dict) and is_digest(config.get("digest", "")):
        found.append(config["digest"])
    for layer in document.get("layers") or []:
        if isinstance(layer, dict) and is_digest(layer.get("digest", "")):
            found.append(layer["digest"])
    return found


# ── Cache index: which repositories has this proxy seen? ─────────────

class _RepositoryIndex:
    """Best-effort ``repositories.json`` next to the disk cache.

    ``DiskCache`` hashes its keys, so a cached manifest cannot be turned back
    into a repository name by looking at the shard tree.  This sidecar records
    the names (and the tags/digests behind them) as manifests are cached, which
    is what lets ``/v2/_catalog`` list repositories known from the cache.
    Failures are logged and ignored: the index is an optimisation, never the
    source of truth.
    """

    FILENAME = "repositories.json"

    def __init__(self, root: str | Path) -> None:
        self.path = Path(root) / self.FILENAME
        self._lock = threading.Lock()

    def _load(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, sort_keys=True)
            os.replace(tmp_name, self.path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    def record_manifest(self, name: str, reference: str, digest: str,
                        blobs: list[str] | None = None) -> None:
        """Record a manifest for *name*, and the blobs it references."""
        with self._lock:
            data = self._load()
            entry = data.setdefault(name, {})
            if not isinstance(entry, dict):
                entry = {}
                data[name] = entry
            entry.setdefault("tags", [])
            entry.setdefault("digests", [])
            entry.setdefault("blobs", [])
            if is_digest(reference):
                if digest not in entry["digests"]:
                    entry["digests"].append(digest)
            else:
                if reference not in entry["tags"]:
                    entry["tags"].append(reference)
            for blob in blobs or []:
                if blob not in entry["blobs"]:
                    entry["blobs"].append(blob)
            try:
                self._save(data)
            except OSError as exc:
                log.warning("docker: cannot update %s: %s", self.path, exc)

    def record_blob(self, name: str, digest: str) -> None:
        """Link one repository to a blob that was fetched or served for it."""
        with self._lock:
            data = self._load()
            entry = data.setdefault(name, {})
            if not isinstance(entry, dict):
                entry = {}
                data[name] = entry
            blobs = entry.setdefault("blobs", [])
            if digest in blobs:
                return
            blobs.append(digest)
            try:
                self._save(data)
            except OSError as exc:
                log.warning("docker: cannot update %s: %s", self.path, exc)

    def has_blob(self, name: str, digest: str) -> bool:
        """True when *digest* is known to belong to repository *name*."""
        entry = self._load().get(name)
        if not isinstance(entry, dict):
            return False
        return digest in (entry.get("blobs") or [])

    def repositories(self) -> list[str]:
        return sorted(self._load())

    def tags(self, name: str) -> list[str]:
        entry = self._load().get(name)
        if not isinstance(entry, dict):
            return []
        return [tag for tag in entry.get("tags") or [] if isinstance(tag, str)]


# ── The proxy ────────────────────────────────────────────────────────

@dataclass
class ManifestResult:
    """A manifest body plus the headers the registry protocol echoes back."""

    content: bytes
    content_type: str
    digest: str
    from_cache: bool = False


@dataclass
class DockerRegistryProxy:
    """One configured upstream registry plus its disk cache.

    Built from :data:`config.settings` on demand (see :func:`get_proxy`), so a
    configuration change — in practice a test swapping the upstream or cache
    directory — takes effect on the next request without a restart.
    """

    upstream_url: str = ""
    username: str = ""
    password: str = ""
    timeout: float = 60.0
    default_namespace: str = "library"
    cache_dir: str = "data/cache/docker"
    cache_max_bytes: int = 1024 * 1024 * 1024

    def __post_init__(self) -> None:
        # Credentials are *not* given to the Upstream session: requests applies
        # ``session.auth`` to every request, which would overwrite the
        # ``Authorization: Bearer …`` header of the post-token retry with Basic.
        # `_request`/`_token` add Basic explicitly where it belongs instead.
        self._upstream = Upstream(
            self.upstream_url,
            timeout=self.timeout,
        )
        self._cache = DiskCache(self.cache_dir, max_bytes=self.cache_max_bytes)
        self._index = _RepositoryIndex(self.cache_dir)
        self._tokens: dict[tuple[str, str, str], tuple[str, float]] = {}
        self._token_lock = threading.Lock()

    # -- configuration -------------------------------------------------

    @property
    def configured(self) -> bool:
        """False when no upstream is set — the proxy is then local-only."""
        return self._upstream.configured

    def upstream_name(self, name: str) -> str:
        return expand_name(name, self.upstream_url, self.default_namespace)

    # -- upstream requests ---------------------------------------------

    def _forward(self, method: str, path: str, *, headers=None, params=None,
                 stream: bool = False):
        """One upstream request, mapping a connection failure to a 502.

        :class:`~services.upstream.UpstreamError` is raised for a connection-level
        failure (DNS, refused, timeout) and carries no HTTP status.  Letting it
        escape the view would turn an unreachable upstream into a 500, so it is
        translated here into the same OCI error surface the routes already know.
        """
        try:
            return self._upstream.request(
                method, path, headers=headers, params=params, stream=stream,
            )
        except UpstreamError as exc:
            raise DockerRegistryError(
                f"upstream registry unreachable: {exc}",
                status=502,
                code="UNAVAILABLE",
            ) from exc

    def _request(self, method: str, path: str, *, headers=None, params=None,
                 stream: bool = False):
        """One upstream request, retried once after a Bearer challenge.

        The first attempt carries HTTP Basic when credentials are configured —
        that satisfies a registry which authenticates with Basic directly, and a
        Bearer registry answers it with the ``WWW-Authenticate`` challenge.  The
        retry then overrides the header with the fetched Bearer token.

        A connection-level failure raises :class:`DockerRegistryError` (502);
        HTTP statuses (including a 401 the token flow could not satisfy) are
        returned for the caller to translate.
        """
        base_headers = dict(headers or {})
        if self.username and "Authorization" not in base_headers:
            base_headers["Authorization"] = _basic_auth_header(self.username, self.password)
        response = self._forward(
            method, path, headers=base_headers or None, params=params, stream=stream,
        )
        if response.status_code != 401:
            return response
        token = self._token(response.headers.get("WWW-Authenticate", ""))
        if not token:
            return response
        response.close()
        retry_headers = dict(base_headers)
        retry_headers["Authorization"] = f"Bearer {token}"
        return self._forward(
            method, path, headers=retry_headers, params=params, stream=stream,
        )

    def _token(self, challenge: str) -> str:
        """Fetch (and cache) a bearer token for a ``WWW-Authenticate`` challenge."""
        scheme, params = _parse_challenge(challenge)
        if scheme.lower() != "bearer":
            return ""
        realm = params.get("realm") or ""
        if not realm:
            return ""
        service = params.get("service", "")
        scope = params.get("scope", "")
        key = (realm, service, scope)

        now = time.time()
        with self._token_lock:
            cached = self._tokens.get(key)
            if cached and cached[1] > now:
                return cached[0]

        query: dict[str, str] = {"client_id": "cpypiserver"}
        if service:
            query["service"] = service
        if scope:
            query["scope"] = scope
        if self.username:
            # Ask for a refresh token too; harmless on realms that ignore it.
            query["offline_token"] = "true"

        token_headers = {"Accept": "application/json"}
        if self.username:
            token_headers["Authorization"] = _basic_auth_header(self.username, self.password)
        try:
            response = self._upstream.request(
                "GET", realm, params=query, headers=token_headers,
            )
        except UpstreamError as exc:
            log.warning("docker: token endpoint unreachable: %s", exc)
            return ""
        try:
            if response.status_code >= 400:
                log.warning("docker: token endpoint returned %s", response.status_code)
                return ""
            try:
                document = response.json()
            except ValueError:
                log.warning("docker: token endpoint returned invalid JSON")
                return ""
        finally:
            response.close()

        if not isinstance(document, dict):
            return ""
        token = document.get("token") or document.get("access_token") or ""
        if not isinstance(token, str) or not token:
            return ""
        try:
            ttl = float(document.get("expires_in"))
        except (TypeError, ValueError):
            ttl = DEFAULT_TOKEN_TTL
        expiry = now + max(ttl - TOKEN_SKEW, 30.0)
        with self._token_lock:
            self._tokens[key] = (token, expiry)
        # Deliberately never log the token itself.
        log.debug("docker: obtained upstream bearer token (service=%r)", service)
        return token

    def _get_document(self, path: str, *, accept: str | None, what: str):
        """GET a small upstream document; returns ``(closed_response, body)``."""
        headers = {"Accept": accept} if accept else None
        response = self._request("GET", path, headers=headers, stream=True)
        if response.status_code >= 400:
            status = response.status_code
            response.close()
            raise self._translate(status, what)
        declared = response.headers.get("Content-Length")
        if declared and declared.isdigit() and int(declared) > MANIFEST_MAX_BYTES:
            response.close()
            raise DockerRegistryError(
                f"upstream {what} is {declared} bytes, over the "
                f"{MANIFEST_MAX_BYTES} byte ceiling",
                status=502,
                code="UNAVAILABLE",
            )
        try:
            body = response.content
        finally:
            response.close()
        return response, body

    @staticmethod
    def _translate(status: int, what: str) -> DockerRegistryError:
        """Map an upstream failure status onto the registry error surface."""
        if status == 404:
            code = {
                "manifest": "MANIFEST_UNKNOWN",
                "blob": "BLOB_UNKNOWN",
                "tags": "NAME_UNKNOWN",
            }.get(what, "NAME_UNKNOWN")
            return DockerRegistryError(
                f"{what} not found upstream", status=404, code=code,
            )
        if status in (401, 403):
            # We authenticate *to* the upstream; a rejection there is our
            # configuration's problem, not the client's.
            return DockerRegistryError(
                "upstream registry rejected the configured credentials",
                status=502,
                code="UNAUTHORIZED",
            )
        return DockerRegistryError(
            f"upstream registry returned {status}", status=502, code="UNAVAILABLE",
        )

    # -- manifests -----------------------------------------------------

    def get_manifest(self, name: str, reference: str, *, accept: str | None = None) -> ManifestResult:
        """Fetch a manifest by tag or digest, caching it on the way through.

        A tag is cached for :data:`TAG_TTL` seconds; a digest is immutable.  The
        returned ``digest`` is always the sha256 of the bytes actually served,
        which is what the client verifies against ``Docker-Content-Digest``.
        """
        key = f"{name}@{reference}"
        digest_ref = is_digest(reference)
        cached = self._cache.get(key, max_age=None if digest_ref else TAG_TTL)
        if cached is not None:
            try:
                body = cached.read_bytes()
            except OSError:
                body = None
            if body is not None:
                meta = self._read_meta(key)
                digest = reference if digest_ref else sha256_digest(body)
                content_type = meta.get("content_type") or _infer_manifest_type(body)
                log.info("docker: manifest cache hit %s (%d bytes)", key, len(body))
                return ManifestResult(body, content_type, digest, from_cache=True)

        if not self.configured:
            raise DockerRegistryError(
                "no upstream registry configured; local `docker save` tarballs "
                "are advertised in the catalog but are not served as registry "
                "manifests (set DOCKER_UPSTREAM to proxy a registry)",
                status=404,
                code="MANIFEST_UNKNOWN",
            )

        path = f"/v2/{self.upstream_name(name)}/manifests/{reference}"
        response, body = self._get_document(path, accept=accept, what="manifest")

        computed = sha256_digest(body)
        upstream_digest = (response.headers.get("Docker-Content-Digest") or "").strip()
        if upstream_digest and upstream_digest.lower() != computed.lower():
            log.error(
                "docker: manifest digest mismatch for %s: upstream=%s computed=%s",
                key, upstream_digest, computed,
            )
        if digest_ref and computed.lower() != reference.lower():
            raise DockerRegistryError(
                "manifest bytes do not match the requested digest",
                status=502,
                code="DIGEST_INVALID",
            )
        content_type = response.headers.get("Content-Type") or _infer_manifest_type(body)
        served_digest = reference if digest_ref else computed

        self._cache.put_bytes(key, body)
        self._write_meta(key, {"content_type": content_type})
        # Linking the manifest's layers/config to the repository is what lets a
        # later blob request for this repository be answered from the shared,
        # content-addressed blob cache without leaking it to another repository.
        self._index.record_manifest(
            name, reference, served_digest, _manifest_blob_digests(body),
        )
        log.info("docker: cached manifest %s (%d bytes)", key, len(body))
        return ManifestResult(body, content_type, served_digest, from_cache=False)

    def _meta_key(self, key: str) -> str:
        return f"{key}#meta"

    def _read_meta(self, key: str) -> dict[str, Any]:
        raw = self._cache.open_bytes(self._meta_key(key), max_age=None)
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def _write_meta(self, key: str, meta: dict[str, Any]) -> None:
        self._cache.put_bytes(self._meta_key(key), json.dumps(meta).encode("utf-8"))

    # -- blobs ---------------------------------------------------------

    def open_blob(self, name: str, digest: str, *, range_header: str | None = None,
                  head: bool = False) -> Response:
        """Serve a blob: from disk when cached for this repository, else upstream.

        The disk file is content-addressed (one copy per digest), but a registry
        blob is *repository-scoped*: ``GET /v2/<name>/blobs/<digest>`` may only
        answer a blob that repository legitimately contains.  A cache hit is
        therefore gated on :class:`_RepositoryIndex` having linked *digest* to
        *name* — either because a manifest of that repository referenced it or
        because the upstream served it for that repository.  An unlinked cached
        blob is re-confirmed with the upstream (or 404s in local-only mode)
        instead of being leaked to an unrelated repository name.

        A cached blob answers ``Range`` with ``206`` + ``Content-Range``.  An
        uncached full GET is streamed to the client while the same bytes are
        written to the content-addressed cache entry, so the second request for
        the digest never touches the upstream.
        """
        if not is_digest(digest):
            raise DockerRegistryError(
                "blob reference must be a content digest", status=400,
                code="DIGEST_INVALID",
            )

        cached = self._cache.get(digest, max_age=None)
        if cached is not None and self._index.has_blob(name, digest):
            log.info("docker: blob cache hit %s for %s", digest, name)
            return _cached_blob_response(cached, digest, range_header)

        if not self.configured:
            if cached is not None:
                raise DockerRegistryError(
                    f"blob {digest} is not linked to repository {name}",
                    status=404, code="BLOB_UNKNOWN",
                )
            raise DockerRegistryError(
                "no upstream registry configured; local `docker save` tarballs "
                "are advertised in the catalog but are not served as registry "
                "blobs (set DOCKER_UPSTREAM to proxy a registry)",
                status=404,
                code="BLOB_UNKNOWN",
            )

        path = f"/v2/{self.upstream_name(name)}/blobs/{digest}"

        if head:
            return self._head_blob(path, digest)

        if range_header:
            # Serve the requested slice straight from upstream: caching a partial
            # body under a content-addressed key would poison the entry.
            response = self._request(
                "GET", path, headers={"Range": range_header}, stream=True,
            )
            if response.status_code >= 400:
                status = response.status_code
                response.close()
                raise self._translate(status, "blob")
            headers = forward_headers(response.headers)
            headers["Docker-Content-Digest"] = digest
            headers.setdefault("Accept-Ranges", "bytes")
            return passthrough(response, extra_headers=headers, status=response.status_code)

        response = self._request("GET", path, stream=True)
        if response.status_code >= 400:
            status = response.status_code
            response.close()
            raise self._translate(status, "blob")
        headers = forward_headers(response.headers)
        headers["Docker-Content-Digest"] = digest
        headers.setdefault("Accept-Ranges", "bytes")
        return self._stream_and_cache(response, name, digest, headers, response.status_code)

    def _head_blob(self, path: str, digest: str) -> Response:
        """Answer a blob ``HEAD`` from upstream headers without downloading it."""
        response = self._request("HEAD", path, stream=True)
        if response.status_code == 200:
            headers = forward_headers(response.headers)
            response.close()
        elif response.status_code in (405, 501):
            # Some registries reject HEAD; take a streamed GET and drop the body.
            response.close()
            response = self._request("GET", path, stream=True)
            if response.status_code >= 400:
                status = response.status_code
                response.close()
                raise self._translate(status, "blob")
            headers = forward_headers(response.headers)
            response.close()
        else:
            status = response.status_code
            response.close()
            raise self._translate(status, "blob")
        headers["Docker-Content-Digest"] = digest
        headers.setdefault("Accept-Ranges", "bytes")
        return Response(_empty_body(), status=200, headers=headers, direct_passthrough=True)

    def _stream_and_cache(self, response, name: str, digest: str,
                          headers: dict[str, str], status: int) -> Response:
        """Stream *response* to the client while writing it to the cache entry.

        The write goes to a sibling ``.part`` file that is ``os.replace``d only
        after the last byte, so a reader never sees a partial entry and an
        aborted download (client disconnect included) leaves the cache unchanged.
        The byte budget is enforced once the entry is committed — without that a
        single unexpected multi-gigabyte layer would fill the disk.  On success
        the repository is linked to the blob so the cache may answer it later.
        """
        dest = self._cache.path_for(digest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=str(dest.parent), suffix=".part")
        state: dict[str, Any] = {
            "handle": os.fdopen(fd, "wb"),
            "done": False,
            # Only sha256 digests can be verified cheaply and uniformly.
            "hasher": hashlib.sha256() if digest.lower().startswith("sha256:") else None,
        }

        def generate() -> Iterator[bytes]:
            try:
                for chunk in response.iter_content(chunk_size=CHUNK):
                    if not chunk:
                        continue
                    state["handle"].write(chunk)
                    if state["hasher"] is not None:
                        state["hasher"].update(chunk)
                    yield chunk
                state["handle"].close()
                state["handle"] = None
                computed = (
                    "sha256:" + state["hasher"].hexdigest()
                    if state["hasher"] is not None else digest
                )
                if state["hasher"] is not None and computed.lower() != digest.lower():
                    log.error(
                        "docker: blob digest mismatch for %s: got %s; not caching",
                        digest, computed,
                    )
                    raise _DiscardTemp()
                os.replace(tmp_name, dest)
                state["done"] = True
                self._index.record_blob(name, digest)
                try:
                    self._cache.enforce_limit()
                except OSError as exc:  # pragma: no cover - budget is best effort
                    log.warning("docker: cache eviction failed: %s", exc)
                log.info("docker: cached blob %s (%d bytes)", digest, dest.stat().st_size)
            except _DiscardTemp:
                pass
            finally:
                if state["handle"] is not None:
                    try:
                        state["handle"].close()
                    except OSError:
                        pass
                if not state["done"]:
                    try:
                        os.unlink(tmp_name)
                    except OSError:
                        pass
                response.close()

        return Response(
            generate(), status=status, headers=headers, direct_passthrough=True,
        )

    # -- tags ----------------------------------------------------------

    def list_tags(self, name: str) -> list[str]:
        """Merge local `docker save` tags, cached tags and upstream tags.

        An upstream failure degrades to the local/cached list rather than
        raising: `tags/list` should still answer while a mirror is down.
        """
        tags = set(self._local_tags(name))
        tags.update(self._index.tags(name))
        if self.configured:
            try:
                tags.update(self._upstream_tags(name))
            except DockerRegistryError as exc:
                log.warning("docker: tags/list upstream failed for %s: %s", name, exc.message)
        if not tags:
            raise DockerRegistryError(
                f"repository {name} is unknown", status=404, code="NAME_UNKNOWN",
            )
        return sorted(tags)

    def _upstream_tags(self, name: str) -> set[str]:
        tags: set[str] = set()
        path = f"/v2/{self.upstream_name(name)}/tags/list"
        for _ in range(MAX_TAG_PAGES):
            response, body = self._get_document(path, accept="application/json", what="tags")
            try:
                document = json.loads(body)
            except ValueError:
                document = None
            if isinstance(document, dict):
                for tag in document.get("tags") or []:
                    if isinstance(tag, str):
                        tags.add(tag)
            next_path = _link_next(response.headers.get("Link", ""))
            if not next_path:
                break
            path = next_path
        return tags

    def _local_tags(self, name: str) -> set[str]:
        """Tags parsed off the ``docker save`` tarball filenames in ``DOCKER_DIR``.

        A local ``nginx-1.25.3.tar`` contributes tag ``1.25.3`` to both
        ``nginx`` and a namespaced ``library/nginx`` request, because that is the
        name a client is most likely to ask for.
        """
        base = Path(settings.hub.docker_dir)
        if not base.is_dir():
            return set()
        wanted = {name, name.rsplit("/", 1)[-1]}
        tags: set[str] = set()
        try:
            entries = list(base.iterdir())
        except OSError:
            return tags
        for path in entries:
            try:
                if not path.is_file():
                    continue
            except OSError:
                continue
            info = _parse_docker_filename(path.name)
            if info.get("kind") != "image":
                continue
            if info.get("name") in wanted and info.get("version"):
                tags.add(str(info["version"]))
        return tags


class _DiscardTemp(Exception):
    """Raised internally to abort a cache write without cancelling the stream."""


def _cached_blob_response(path: Path, digest: str, range_header: str | None) -> Response:
    """Serve a cached blob, honouring a single HTTP ``Range``."""
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise DockerRegistryError(
            "cached blob disappeared", status=404, code="BLOB_UNKNOWN",
        ) from exc

    base = {
        "Content-Type": "application/octet-stream",
        "Docker-Content-Digest": digest,
        "Accept-Ranges": "bytes",
    }
    parsed = _parse_range(range_header, size) if range_header else None
    if parsed is False:
        headers = dict(base, **{"Content-Range": f"bytes */{size}"})
        return Response(_empty_body(), status=416, headers=headers, direct_passthrough=True)
    if parsed is None:
        headers = dict(base, **{"Content-Length": str(size)})
        return Response(
            _file_slice(path, 0, size), status=200, headers=headers,
            direct_passthrough=True,
        )
    start, end = parsed
    length = end - start + 1
    headers = dict(base, **{
        "Content-Length": str(length),
        "Content-Range": f"bytes {start}-{end}/{size}",
    })
    return Response(
        _file_slice(path, start, length), status=206, headers=headers,
        direct_passthrough=True,
    )


# ── Configuration-bound access ───────────────────────────────────────

_proxy: DockerRegistryProxy | None = None
_proxy_key: tuple[Any, ...] | None = None
_proxy_lock = threading.Lock()


def _config_key() -> tuple[Any, ...]:
    hub = settings.hub
    return (
        hub.docker_upstream,
        hub.docker_upstream_username,
        hub.docker_upstream_password,
        float(hub.docker_timeout),
        hub.docker_cache_dir,
        int(hub.docker_cache_max_mb),
        hub.docker_default_namespace,
    )


def get_proxy() -> DockerRegistryProxy:
    """The proxy for the current configuration, rebuilt when the config changes."""
    global _proxy, _proxy_key
    key = _config_key()
    with _proxy_lock:
        if _proxy is None or _proxy_key != key:
            hub = settings.hub
            _proxy = DockerRegistryProxy(
                upstream_url=hub.docker_upstream,
                username=hub.docker_upstream_username,
                password=hub.docker_upstream_password,
                timeout=float(hub.docker_timeout),
                default_namespace=hub.docker_default_namespace,
                cache_dir=hub.docker_cache_dir,
                cache_max_bytes=int(hub.docker_cache_max_mb) * 1024 * 1024,
            )
            _proxy_key = key
        return _proxy


def reset() -> None:
    """Drop the cached proxy (used by tests after changing settings)."""
    global _proxy, _proxy_key
    with _proxy_lock:
        _proxy = None
        _proxy_key = None


def get_manifest(name: str, reference: str, *, accept: str | None = None) -> ManifestResult:
    return get_proxy().get_manifest(name, reference, accept=accept)


def open_blob(name: str, digest: str, *, range_header: str | None = None,
              head: bool = False) -> Response:
    return get_proxy().open_blob(name, digest, range_header=range_header, head=head)


def list_tags(name: str) -> list[str]:
    return get_proxy().list_tags(name)


def cached_repositories() -> list[str]:
    """Repository names this proxy has cached manifests for."""
    return get_proxy()._index.repositories()


__all__ = [
    "DockerRegistryError",
    "DockerRegistryProxy",
    "ManifestResult",
    "MANIFEST_MAX_BYTES",
    "TAG_TTL",
    "cached_repositories",
    "expand_name",
    "get_manifest",
    "get_proxy",
    "is_digest",
    "list_tags",
    "open_blob",
    "reset",
    "sha256_digest",
]
