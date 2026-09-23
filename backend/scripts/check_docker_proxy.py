#!/usr/bin/env python
"""Gate: the Docker Registry v2 pull flow must work end-to-end, offline.

Run from the backend directory (`backend/`)::

    python scripts/check_docker_proxy.py

The test stands up a fake registry upstream in a background thread and drives
the real Flask app through ``app.test_client()`` with HTTP Basic.  No network is
used, so this can run in CI.  It exercises the whole pull sequence:

* the ``/v2/`` version probe (200 + the version header) and the anonymous 401;
* the upstream bearer-token handshake (the fake answers 401 + a challenge first,
  then hands out a token, then accepts the bearer);
* a manifest fetched by tag and by digest, with the right bytes, content type
  and ``Docker-Content-Digest``;
* ``HEAD`` returning headers and no body;
* a blob streamed from upstream, then a second fetch served from the cache
  without touching the upstream (proven by hit counts);
* a ``Range`` request against the cached blob returning ``206`` with the right
  ``Content-Range``;
* ``tags/list`` merging the local `docker save` tarball tags with upstream tags;
* unknown repository -> 404 JSON, and local-only mode 404-ing instead of 500.

Exits non-zero with a clear message on any failure.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import socket
import sys
import tempfile
import threading
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
os.chdir(REPO_ROOT)

from werkzeug.serving import make_server  # noqa: E402

from config import settings  # noqa: E402

# ── Fixtures the fake registry serves ────────────────────────────────

BLOB = b"openfish-fake-layer-" + bytes(range(256)) * 4
LAYER_DIGEST = "sha256:" + hashlib.sha256(BLOB).hexdigest()
CONFIG_BLOB = b'{"architecture":"amd64","os":"linux"}'
CONFIG_DIGEST = "sha256:" + hashlib.sha256(CONFIG_BLOB).hexdigest()
MANIFEST = json.dumps(
    {
        "schemaVersion": 2,
        "mediaType": "application/vnd.docker.distribution.manifest.v2+json",
        "config": {
            "mediaType": "application/vnd.docker.container.image.v1+json",
            "size": len(CONFIG_BLOB),
            "digest": CONFIG_DIGEST,
        },
        "layers": [
            {
                "mediaType": "application/vnd.docker.image.rootfs.diff.tar.gzip",
                "size": len(BLOB),
                "digest": LAYER_DIGEST,
            }
        ],
    },
    separators=(",", ":"),
).encode()
MANIFEST_DIGEST = "sha256:" + hashlib.sha256(MANIFEST).hexdigest()
MANIFEST_TYPE = "application/vnd.docker.distribution.manifest.v2+json"

TOKEN = "fake-bearer-token"
UPSTREAM_USER = "upuser"
#: Both passwords are generated per run rather than written as literals.  A
#: fixed password in a test is still a credential string in the tree — a
#: scanner flags it, and a reader may reuse it — while nothing here depends on
#: the value being stable.
UPSTREAM_PASS = secrets.token_urlsafe(18)
CLIENT_USER = "gate-client"
CLIENT_PASS = secrets.token_urlsafe(18)


def _fake_range(header: str, size: int):
    if not header.startswith("bytes="):
        return None
    spec = header[len("bytes="):]
    if "," in spec or "-" not in spec:
        return None
    first, _, last = spec.partition("-")
    try:
        start = int(first) if first else 0
        end = int(last) if last else size - 1
    except ValueError:
        return None
    if start >= size or end < start:
        return None
    return start, min(end, size - 1)


def _closed_port() -> int:
    """A 127.0.0.1 port that nothing is listening on (connection refused)."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class FakeRegistry:
    """A tiny WSGI registry: challenge, token endpoint, manifest, blobs."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.hits: dict[str, int] = {}
        self.bearer_seen = False
        self.basic_seen = False
        self.port = 0

    # -- counters -----------------------------------------------------

    def _hit(self, path: str) -> None:
        with self.lock:
            self.hits[path] = self.hits.get(path, 0) + 1

    def count(self, path: str) -> int:
        with self.lock:
            return self.hits.get(path, 0)

    # -- WSGI ---------------------------------------------------------

    def __call__(self, environ, start_response):
        path = environ.get("PATH_INFO", "")
        method = environ.get("REQUEST_METHOD", "GET").upper()
        auth = environ.get("HTTP_AUTHORIZATION", "")
        self._hit(path)

        if path == "/token":
            return self._token(auth, start_response)
        if auth != f"Bearer {TOKEN}":
            return self._challenge(start_response)
        with self.lock:
            self.bearer_seen = True

        if path == "/v2/":
            return self._send(
                start_response, 200, b"{}",
                {
                    "Content-Type": "application/json",
                    "Docker-Distribution-Api-Version": "registry/2.0",
                },
                method,
            )
        if path == "/v2/library/tiny/tags/list":
            body = json.dumps({"name": "library/tiny", "tags": ["latest", "v1"]}).encode()
            return self._send(
                start_response, 200, body, {"Content-Type": "application/json"}, method,
            )
        if path in (
            "/v2/library/tiny/manifests/latest",
            f"/v2/library/tiny/manifests/{MANIFEST_DIGEST}",
        ):
            return self._send(
                start_response, 200, MANIFEST,
                {"Content-Type": MANIFEST_TYPE, "Docker-Content-Digest": MANIFEST_DIGEST},
                method,
            )
        if path.startswith("/v2/library/tiny/blobs/"):
            digest = path.rsplit("/", 1)[-1]
            data = {LAYER_DIGEST: BLOB, CONFIG_DIGEST: CONFIG_BLOB}.get(digest)
            if data is None:
                return self._send(
                    start_response, 404, b'{"errors":[{"code":"BLOB_UNKNOWN"}]}',
                    {"Content-Type": "application/json"}, method,
                )
            headers = {
                "Content-Type": "application/octet-stream",
                "Docker-Content-Digest": digest,
                "Accept-Ranges": "bytes",
            }
            range_header = environ.get("HTTP_RANGE")
            if range_header and method == "GET":
                parsed = _fake_range(range_header, len(data))
                if parsed is not None:
                    start, end = parsed
                    headers["Content-Range"] = f"bytes {start}-{end}/{len(data)}"
                    return self._send(
                        start_response, 206, data[start:end + 1], headers, method,
                    )
            return self._send(start_response, 200, data, headers, method)

        return self._send(
            start_response, 404,
            b'{"errors":[{"code":"NAME_UNKNOWN","message":"repository name not known"}]}',
            {"Content-Type": "application/json"}, method,
        )

    # -- helpers ------------------------------------------------------

    def _challenge(self, start_response):
        realm = f"http://127.0.0.1:{self.port}/token"
        headers = {
            "Content-Type": "application/json",
            "WWW-Authenticate": (
                f'Bearer realm="{realm}",service="fake-registry",'
                f'scope="repository:library/tiny:pull"'
            ),
        }
        return self._send(
            start_response, 401, b'{"errors":[{"code":"UNAUTHORIZED"}]}', headers,
        )

    def _token(self, auth, start_response):
        if auth.startswith("Basic "):
            try:
                decoded = base64.b64decode(auth[len("Basic "):]).decode("utf-8")
            except Exception:  # noqa: BLE001 - a malformed header is just a 401
                decoded = ""
            user, _, password = decoded.partition(":")
            if user == UPSTREAM_USER and password == UPSTREAM_PASS:
                with self.lock:
                    self.basic_seen = True
                body = json.dumps({"token": TOKEN, "expires_in": 300}).encode()
                return self._send(
                    start_response, 200, body, {"Content-Type": "application/json"},
                )
        return self._send(
            start_response, 401, b'{"error":"invalid_client"}',
            {"Content-Type": "application/json"},
        )

    @staticmethod
    def _send(start_response, status, body, headers, method="GET"):
        headers = dict(headers)
        headers["Content-Length"] = str(len(body))
        reason = {
            200: "OK", 206: "Partial Content", 401: "Unauthorized",
            404: "Not Found", 405: "Method Not Allowed",
        }.get(status, "OK")
        start_response(f"{status} {reason}", list(headers.items()))
        return [b""] if method == "HEAD" else [body]


# ── The gate ─────────────────────────────────────────────────────────

def main() -> int:
    failures: list[str] = []

    def check(condition: bool, message: str) -> None:
        if condition:
            print(f"   ✅ {message}")
        else:
            failures.append(message)
            print(f"   ❌ {message}")

    fake = FakeRegistry()
    # The fake registry's per-request logs are noise here; failures still show.
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    server = make_server("127.0.0.1", 0, fake)
    fake.port = server.server_port
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(f"── Fake registry upstream: http://127.0.0.1:{fake.port} ──")

    cache_dir = tempfile.TemporaryDirectory(prefix="openfish-docker-cache-")
    docker_dir = tempfile.TemporaryDirectory(prefix="openfish-docker-local-")
    # A local `docker save` tarball. Its filename parses to name=tiny tag=1.0,
    # which must show up merged into tags/list for library/tiny.
    Path(docker_dir.name, "tiny-1.0.tar").write_bytes(b"not-a-real-tar")

    try:
        # The proxy reads settings lazily, but pointing them at the fake before
        # the app is imported keeps the test honest about a fresh boot too.
        settings.auth.auth_enabled = True
        settings.auth.basic_username = CLIENT_USER
        settings.auth.basic_password = CLIENT_PASS
        settings.hub.docker_upstream = f"http://127.0.0.1:{fake.port}"
        settings.hub.docker_upstream_username = UPSTREAM_USER
        settings.hub.docker_upstream_password = UPSTREAM_PASS
        settings.hub.docker_cache_dir = cache_dir.name
        settings.hub.docker_cache_max_mb = 8
        settings.hub.docker_default_namespace = "library"
        settings.hub.docker_dir = docker_dir.name

        from app import app  # noqa: E402
        from services import docker_registry as registry  # noqa: E402

        registry.reset()
        credentials = base64.b64encode(f"{CLIENT_USER}:{CLIENT_PASS}".encode()).decode()
        auth = {"Authorization": f"Basic {credentials}"}
        client = app.test_client()

        print()
        print("── Version probe ───────────────────────────────────────────────")
        response = client.get("/docker/v2/", headers=auth)
        check(
            response.status_code == 200
            and response.headers.get("Docker-Distribution-Api-Version") == "registry/2.0"
            and response.get_json() == {},
            "GET /docker/v2/ → 200 + Docker-Distribution-Api-Version: registry/2.0",
        )
        response = client.get("/docker/v2/")
        check(
            response.status_code == 401
            and "basic" in response.headers.get("WWW-Authenticate", "").lower(),
            "anonymous GET /docker/v2/ → 401 with a Basic challenge",
        )

        print()
        print("── Manifest by tag + upstream token flow ───────────────────────")
        response = client.get(
            "/docker/v2/library/tiny/manifests/latest",
            headers={**auth, "Accept": MANIFEST_TYPE},
        )
        check(
            response.status_code == 200 and response.data == MANIFEST,
            "GET manifest by tag → 200 and the exact upstream bytes",
        )
        check(
            response.headers.get("Docker-Content-Digest") == MANIFEST_DIGEST,
            f"Docker-Content-Digest is sha256 of the body ({MANIFEST_DIGEST[:19]}…)",
        )
        check(
            response.headers.get("Content-Type", "").startswith(MANIFEST_TYPE),
            "upstream Content-Type is echoed back",
        )
        check(
            fake.count("/v2/library/tiny/manifests/latest") >= 2,
            "the 401 challenge was retried with a bearer token",
        )
        check(fake.count("/token") >= 1 and fake.basic_seen, "token endpoint got HTTP Basic client credentials")
        check(fake.bearer_seen, "upstream saw an Authorization: Bearer request")

        print()
        print("── Manifest by digest + HEAD ───────────────────────────────────")
        response = client.get(
            f"/docker/v2/library/tiny/manifests/{MANIFEST_DIGEST}",
            headers={**auth, "Accept": MANIFEST_TYPE},
        )
        check(
            response.status_code == 200 and response.data == MANIFEST
            and response.headers.get("Docker-Content-Digest") == MANIFEST_DIGEST,
            "GET manifest by digest → 200 with the requested digest",
        )
        response = client.open(
            "/docker/v2/library/tiny/manifests/latest", method="HEAD", headers=auth,
        )
        check(
            response.status_code == 200
            and response.data == b""
            and response.headers.get("Content-Length") == str(len(MANIFEST))
            and response.headers.get("Docker-Content-Digest") == MANIFEST_DIGEST,
            "HEAD manifest → headers only, correct Content-Length, no body",
        )

        print()
        print("── Blob streaming + cache ──────────────────────────────────────")
        response = client.get(
            f"/docker/v2/library/tiny/blobs/{LAYER_DIGEST}", headers=auth,
        )
        check(
            response.status_code == 200 and response.data == BLOB,
            "GET blob → 200 and bytes identical to the upstream layer",
        )
        check(
            response.headers.get("Docker-Content-Digest") == LAYER_DIGEST
            and response.headers.get("Accept-Ranges") == "bytes"
            and response.headers.get("Content-Length") == str(len(BLOB)),
            "blob carries Content-Length, Docker-Content-Digest and Accept-Ranges",
        )

        blob_path = f"/v2/library/tiny/blobs/{LAYER_DIGEST}"
        before = fake.count(blob_path)
        response = client.get(
            f"/docker/v2/library/tiny/blobs/{LAYER_DIGEST}", headers=auth,
        )
        after = fake.count(blob_path)
        check(
            response.status_code == 200 and response.data == BLOB and after == before,
            "second blob fetch is served from the local cache (no upstream hit)",
        )

        response = client.get(
            f"/docker/v2/library/tiny/blobs/{LAYER_DIGEST}",
            headers={**auth, "Range": "bytes=5-9"},
        )
        check(
            response.status_code == 206
            and response.data == BLOB[5:10]
            and response.headers.get("Content-Range") == f"bytes 5-9/{len(BLOB)}"
            and response.headers.get("Content-Length") == "5",
            "Range on the cached blob → 206 + correct Content-Range/Content-Length",
        )

        response = client.open(
            f"/docker/v2/library/tiny/blobs/{CONFIG_DIGEST}", method="HEAD", headers=auth,
        )
        check(
            response.status_code == 200
            and response.data == b""
            and response.headers.get("Content-Length") == str(len(CONFIG_BLOB))
            and response.headers.get("Docker-Content-Digest") == CONFIG_DIGEST,
            "HEAD blob → upstream headers only, no body download",
        )

        print()
        print("── Tags + catalog ──────────────────────────────────────────────")
        response = client.get("/docker/v2/library/tiny/tags/list", headers=auth)
        tags = (response.get_json() or {}).get("tags") or []
        check(
            response.status_code == 200
            and {"latest", "v1", "1.0"} <= set(tags),
            "tags/list merges upstream tags (latest, v1) with the local tar tag (1.0)",
        )
        response = client.get("/docker/v2/library/tiny/tags/list?n=2", headers=auth)
        page = (response.get_json() or {}).get("tags") or []
        check(
            response.status_code == 200 and len(page) == 2 and "Link" in response.headers,
            "tags/list ?n=2 paginates and advertises the next page",
        )
        response = client.get("/docker/v2/_catalog", headers=auth)
        check(
            response.status_code == 200
            and "library/tiny" in (response.get_json() or {}).get("repositories", []),
            "/v2/_catalog includes the repository known from the cache",
        )

        print()
        print("── Name expansion + error surface ──────────────────────────────")
        check(
            registry.expand_name("nginx", "https://registry-1.docker.io", "library")
            == "library/nginx",
            "single-segment name expands to library/nginx on Docker Hub",
        )
        check(
            registry.expand_name("nginx", "http://registry.intra:5000", "library") == "nginx",
            "an intranet upstream keeps the bare name",
        )
        check(
            registry.expand_name("library/nginx", "https://registry-1.docker.io", "library")
            == "library/nginx",
            "an already-namespaced name is left alone",
        )
        response = client.get("/docker/v2/library/nope/manifests/latest", headers=auth)
        payload = response.get_json() or {}
        check(
            response.status_code == 404
            and isinstance(payload, dict) and payload.get("errors"),
            "unknown repository → 404 JSON with an OCI error body",
        )
        response = client.get(f"/docker/v2/library/nope/blobs/{LAYER_DIGEST}", headers=auth)
        check(
            response.status_code == 404
            and (response.get_json() or {}).get("errors"),
            "cached blob under an unrelated repository → 404 (blobs are repo-scoped)",
        )
        uncached = "sha256:" + "0" * 64
        response = client.get(f"/docker/v2/library/nope/blobs/{uncached}", headers=auth)
        check(
            response.status_code == 404 and (response.get_json() or {}).get("errors"),
            "unknown blob digest → 404 JSON with an OCI error body",
        )

        print()
        print("── Local-only mode (no upstream) ───────────────────────────────")
        settings.hub.docker_upstream = ""
        response = client.get("/docker/v2/", headers=auth)
        check(
            response.status_code == 200,
            "version probe still answers 200 with no upstream configured",
        )
        response = client.get("/docker/v2/library/tiny/manifests/not-cached", headers=auth)
        payload = response.get_json() or {}
        check(
            response.status_code == 404 and payload.get("errors"),
            "uncached manifest in local-only mode → 404 JSON, not a 500",
        )
        response = client.get("/docker/v2/_catalog", headers=auth)
        check(
            response.status_code == 200
            and "library/tiny" in (response.get_json() or {}).get("repositories", []),
            "/v2/_catalog still lists local + cached repositories with no upstream",
        )

        print()
        print("── Unreachable upstream ────────────────────────────────────────")
        settings.hub.docker_upstream = f"http://127.0.0.1:{_closed_port()}"
        settings.hub.docker_timeout = 2
        registry.reset()
        response = client.get("/docker/v2/library/gone/manifests/latest", headers=auth)
        payload = response.get_json() or {}
        check(
            response.status_code == 502 and payload.get("errors"),
            "unreachable upstream manifest → 502 JSON (not a 500)",
        )
        fresh_digest = "sha256:" + "1" * 64
        response = client.get(f"/docker/v2/library/gone/blobs/{fresh_digest}", headers=auth)
        payload = response.get_json() or {}
        check(
            response.status_code == 502 and payload.get("errors"),
            "unreachable upstream blob → 502 JSON (not a 500)",
        )

        # Last, because it deliberately clears the cache directory.
        print()
        print("── Cache budget smaller than one blob ──────────────────────────")
        # Committing a blob and *then* enforcing the budget can evict the very
        # entry that was just written — which is correct, but reading its size
        # back with stat() afterwards used to turn a successful pull into a 500.
        settings.hub.docker_upstream = f"http://127.0.0.1:{fake.port}"
        settings.hub.docker_timeout = 5
        registry.reset()
        proxy = registry.get_proxy()
        proxy._cache.clear()
        proxy._cache.max_bytes = 1  # smaller than a single layer
        response = client.get(f"/docker/v2/library/tiny/blobs/{LAYER_DIGEST}", headers=auth)
        check(
            response.status_code == 200 and response.data == BLOB,
            "a blob larger than the whole cache budget still streams intact (no 500 after eviction)",
        )
        check(
            proxy._cache.get(LAYER_DIGEST) is None,
            "the oversized blob was evicted instead of being kept over budget",
        )
    finally:
        server.shutdown()
        thread.join(timeout=5)
        cache_dir.cleanup()
        docker_dir.cleanup()

    print()
    if failures:
        print(f"❌ docker proxy check FAILED — {len(failures)} problem(s)")
        for message in failures:
            print("   " + message)
        return 1
    print("✅ docker proxy check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
