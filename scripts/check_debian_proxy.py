#!/usr/bin/env python
"""Gate: the apt mirror proxy (`dists/` + `pool/`) works without a network.

Run from the repository root::

    python scripts/check_debian_proxy.py

The script starts a fake apt mirror in a background thread, points the
application at it and drives the real Flask app through ``app.test_client()``
with HTTP Basic credentials.  Nothing but ``127.0.0.1`` is contacted, so the
check is safe on an air-gapped build host.

What is asserted:

* ``Release`` / ``Packages`` are proxied byte-for-byte;
* a ``.gz`` document keeps its exact bytes and its ``Content-Encoding`` — a
  transparently decompressed body would break apt's signature/hash checks;
* a second request within ``DEBIAN_METADATA_TTL`` is served from the disk cache
  (the fake mirror's hit counter does not move);
* a ``Range`` request on a pool file returns ``206`` with the right
  ``Content-Range`` and body; ``HEAD`` fetches headers only;
* an upstream ``404`` is forwarded; an unreachable upstream is a JSON ``502``;
* no upstream configured is a JSON ``404``;
* a synced local mirror under ``DEBIAN_DIR/dists`` and ``DEBIAN_DIR/pool`` is
  served directly, byte-for-byte and with working ``Range``/``HEAD``, even with
  no upstream configured or an unreachable one, and it wins over the upstream;
  a local miss still falls back to the upstream;
* ``..`` and a symlink escaping the local mirror are refused (``4xx``);
* anonymous access is a ``401``;
* the pre-existing flat repository routes still work.

Any failure exits non-zero with a clear message.
"""

from __future__ import annotations

import base64
import gzip
import os
import shutil
import socket
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
os.chdir(REPO_ROOT)

# The application reads its configuration from the environment at import time,
# so a deterministic testing environment has to be installed first.
_TMP = Path(tempfile.mkdtemp(prefix="debian-proxy-check-"))
os.environ["SECRET_KEY"] = os.environ.get("SECRET_KEY") or "debian-proxy-check"
os.environ["AUTH_USERNAME"] = "dev"
os.environ["AUTH_ASSERT"] = "devpass"
os.environ["ADMIN_USERS"] = '["dev"]'
os.environ["API_KEYS_FILE"] = str(_TMP / "keys.db")
os.environ["DEBIAN_DIR"] = str(REPO_ROOT / "debian")
os.environ["DEBIAN_UPSTREAM"] = ""
os.environ["DEBIAN_MIRROR"] = ""

from app import app  # noqa: E402
from config import settings  # noqa: E402
from services import debian_apt  # noqa: E402

# ── Fake upstream mirror ─────────────────────────────────────────────

RELEASE = (
    b"Origin: OpenFish test mirror\n"
    b"Label: OpenFish\n"
    b"Suite: stable\n"
    b"Codename: bookworm\n"
    b"Architectures: arm64 amd64\n"
    b"Components: main\n"
    b"Date: Mon, 01 Jan 2024 00:00:00 UTC\n"
)
PACKAGES = (
    b"Package: tiny\n"
    b"Version: 1.0\n"
    b"Architecture: arm64\n"
    b"Maintainer: Test <test@example.invalid>\n"
    b"Filename: pool/main/t/tiny/tiny_1.0_arm64.deb\n"
    b"Size: 64\n"
    b"SHA256: " + b"0" * 64 + b"\n"
    b"Description: tiny test package\n"
)
PACKAGES_GZ = gzip.compress(PACKAGES, mtime=0)
POOL_BLOB = bytes(range(64))

_MIRROR_ROUTES = {
    "/dists/bookworm/Release": (RELEASE, "text/plain; charset=utf-8", {}),
    "/dists/bookworm/main/binary-arm64/Packages": (PACKAGES, "text/plain; charset=utf-8", {}),
    # The gzip body is the artifact.  Labelling it ``Content-Encoding: gzip``
    # as well is the worst case: a proxy that lets requests decode it would send
    # the uncompressed Packages text under a gzip header.
    "/dists/bookworm/main/binary-arm64/Packages.gz": (
        PACKAGES_GZ,
        "application/x-gzip",
        {"Content-Encoding": "gzip"},
    ),
}


class _MirrorHandler(BaseHTTPRequestHandler):
    """A minimal read-only apt mirror with Range support and a hit counter."""

    protocol_version = "HTTP/1.1"
    hits: dict[str, int] = {}
    lock = threading.Lock()

    def log_message(self, *args, **kwargs) -> None:  # pragma: no cover
        pass

    # -- request dispatch ------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        self._serve(head=False)

    def do_HEAD(self) -> None:  # noqa: N802 - http.server API
        self._serve(head=True)

    def _record(self, path: str) -> None:
        with _MirrorHandler.lock:
            _MirrorHandler.hits[path] = _MirrorHandler.hits.get(path, 0) + 1

    @classmethod
    def count(cls, path: str) -> int:
        with cls.lock:
            return cls.hits.get(path, 0)

    def _serve(self, *, head: bool) -> None:
        path = self.path.split("?", 1)[0]
        self._record(path)

        if path == "/pool/main/t/tiny/tiny_1.0_arm64.deb":
            self._serve_blob(POOL_BLOB, head=head)
            return

        route = _MIRROR_ROUTES.get(path)
        if route is None:
            self._send(404, b"no such object\n", "text/plain; charset=utf-8", head=head)
            return
        body, content_type, extra = route
        self._send(200, body, content_type, head=head, extra=extra)

    def _serve_blob(self, blob: bytes, *, head: bool) -> None:
        total = len(blob)
        range_header = self.headers.get("Range")
        if range_header and range_header.startswith("bytes="):
            spec = range_header[len("bytes="):].split(",", 1)[0].strip()
            start_text, _, end_text = spec.partition("-")
            try:
                start = int(start_text) if start_text else 0
                end = int(end_text) if end_text else total - 1
            except ValueError:
                self._send(416, b"bad range\n", "text/plain", head=head)
                return
            if start >= total or end < start:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{total}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            end = min(end, total - 1)
            chunk = blob[start:end + 1]
            self.send_response(206)
            self.send_header("Content-Type", "application/vnd.debian.binary-package")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
            self.send_header("Content-Length", str(len(chunk)))
            self.end_headers()
            if not head:
                self.wfile.write(chunk)
            return

        self._send(
            200, blob, "application/vnd.debian.binary-package",
            head=head, extra={"Accept-Ranges": "bytes"},
        )

    def _send(
        self,
        status: int,
        body: bytes,
        content_type: str,
        *,
        head: bool,
        extra: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if not head:
            self.wfile.write(body)


def _free_port() -> int:
    """A port nobody is listening on (used to simulate an unreachable mirror)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


# ── Assertion harness ────────────────────────────────────────────────

FAILURES: list[str] = []
CHECKS = 0
AUTH = {"Authorization": "Basic " + base64.b64encode(b"dev:devpass").decode("ascii")}


def check(name: str, condition: bool, detail: str = "") -> None:
    global CHECKS
    CHECKS += 1
    if condition:
        print(f"✅ {name}")
        return
    message = f"❌ {name}" + (f" — {detail}" if detail else "")
    print(message)
    FAILURES.append(message)


def client_get(client, path: str, **kwargs):
    headers = dict(AUTH)
    headers.update(kwargs.pop("headers", {}) or {})
    return client.get(path, headers=headers, **kwargs)


def main() -> int:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _MirrorHandler)
    mirror = f"http://127.0.0.1:{server.server_address[1]}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    client = app.test_client()
    settings.hub.debian_upstream = mirror
    settings.hub.debian_mirror = ""
    settings.hub.debian_cache_dir = str(_TMP / "cache")
    settings.hub.debian_metadata_ttl = 300

    try:
        # ── Metadata: proxied byte-for-byte ──────────────────────────
        print("── dists/ metadata ─────────────────────────────────────────────")

        response = client_get(client, "/debian/dists/bookworm/Release")
        check(
            "Release proxied byte-identically",
            response.status_code == 200 and response.data == RELEASE,
            f"status={response.status_code} bytes={len(response.data)}",
        )
        check(
            "Release keeps its Content-Type",
            response.headers.get("Content-Type", "").startswith("text/plain"),
            f"Content-Type={response.headers.get('Content-Type')!r}",
        )

        response = client_get(client, "/debian/dists/bookworm/main/binary-arm64/Packages")
        check(
            "Packages proxied byte-identically",
            response.status_code == 200 and response.data == PACKAGES,
            f"status={response.status_code} bytes={len(response.data)}",
        )

        gz_response = client_get(
            client, "/debian/dists/bookworm/main/binary-arm64/Packages.gz"
        )
        check(
            ".gz body is the gzip bytes, not decompressed",
            gz_response.status_code == 200
            and gz_response.data == PACKAGES_GZ
            and gz_response.data[:2] == b"\x1f\x8b",
            f"status={gz_response.status_code} bytes={len(gz_response.data)} "
            f"expected={len(PACKAGES_GZ)}",
        )
        check(
            ".gz Content-Encoding / Content-Type preserved",
            gz_response.headers.get("Content-Encoding") == "gzip"
            and gz_response.headers.get("Content-Type") == "application/x-gzip",
            f"Content-Encoding={gz_response.headers.get('Content-Encoding')!r} "
            f"Content-Type={gz_response.headers.get('Content-Type')!r}",
        )

        # ── Second request inside the TTL must not hit the mirror ────
        print()
        print("── metadata cache ──────────────────────────────────────────────")
        release_path = "/dists/bookworm/Release"
        packages_path = "/dists/bookworm/main/binary-arm64/Packages"
        gz_path = "/dists/bookworm/main/binary-arm64/Packages.gz"
        before = {
            release_path: _MirrorHandler.count(release_path),
            packages_path: _MirrorHandler.count(packages_path),
            gz_path: _MirrorHandler.count(gz_path),
        }

        cached_release = client_get(client, "/debian/dists/bookworm/Release")
        cached_packages = client_get(
            client, "/debian/dists/bookworm/main/binary-arm64/Packages"
        )
        cached_gz = client_get(
            client, "/debian/dists/bookworm/main/binary-arm64/Packages.gz"
        )
        after = {
            release_path: _MirrorHandler.count(release_path),
            packages_path: _MirrorHandler.count(packages_path),
            gz_path: _MirrorHandler.count(gz_path),
        }

        check(
            "second fetch is served from cache (mirror not re-hit)",
            before == after and before[release_path] == 1,
            f"before={before} after={after}",
        )
        check(
            "cached responses are still byte-identical",
            cached_release.data == RELEASE
            and cached_packages.data == PACKAGES
            and cached_gz.data == PACKAGES_GZ,
            "cached body differs from the upstream body",
        )

        # ── pool/: streaming and Range ───────────────────────────────
        print()
        print("── pool/ streaming ─────────────────────────────────────────────")
        pool_path = "/debian/pool/main/t/tiny/tiny_1.0_arm64.deb"

        response = client_get(client, pool_path)
        check(
            "pool GET streams the whole blob",
            response.status_code == 200 and response.data == POOL_BLOB,
            f"status={response.status_code} bytes={len(response.data)}",
        )
        check(
            "pool GET advertises Accept-Ranges",
            response.headers.get("Accept-Ranges") == "bytes",
            f"Accept-Ranges={response.headers.get('Accept-Ranges')!r}",
        )

        response = client_get(client, pool_path, headers={"Range": "bytes=0-9"})
        check(
            "Range: bytes=0-9 -> 206 with 10 bytes",
            response.status_code == 206
            and response.data == POOL_BLOB[:10]
            and len(response.data) == 10,
            f"status={response.status_code} bytes={len(response.data)}",
        )
        check(
            "Range response carries Content-Range / Content-Length",
            response.headers.get("Content-Range") == f"bytes 0-9/{len(POOL_BLOB)}"
            and response.headers.get("Content-Length") == "10"
            and response.headers.get("Accept-Ranges") == "bytes",
            f"Content-Range={response.headers.get('Content-Range')!r} "
            f"Content-Length={response.headers.get('Content-Length')!r}",
        )

        response = client.open(pool_path, method="HEAD", headers=AUTH)
        check(
            "HEAD returns headers without a body",
            response.status_code == 200
            and response.data == b""
            and response.headers.get("Content-Length") == str(len(POOL_BLOB)),
            f"status={response.status_code} bytes={len(response.data)} "
            f"Content-Length={response.headers.get('Content-Length')!r}",
        )

        # ── Error handling ───────────────────────────────────────────
        print()
        print("── errors ──────────────────────────────────────────────────────")
        response = client_get(client, "/debian/dists/bookworm/nope/Release")
        check(
            "upstream 404 is forwarded as JSON",
            response.status_code == 404
            and response.is_json
            and "error" in response.get_json(),
            f"status={response.status_code} body={response.data[:80]!r}",
        )

        response = client_get(client, "/debian/pool/main/t/tiny/missing.deb")
        check(
            "upstream 404 on pool is forwarded as JSON",
            response.status_code == 404
            and response.is_json
            and "error" in response.get_json(),
            f"status={response.status_code} body={response.data[:80]!r}",
        )

        settings.hub.debian_upstream = f"http://127.0.0.1:{_free_port()}"
        response = client_get(client, "/debian/dists/bookworm/Release")
        check(
            "unreachable upstream -> 502 JSON",
            response.status_code == 502
            and response.is_json
            and "error" in response.get_json(),
            f"status={response.status_code} body={response.data[:80]!r}",
        )
        response = client_get(client, "/debian/pool/main/t/tiny/tiny_1.0_arm64.deb")
        check(
            "unreachable upstream on pool -> 502 JSON",
            response.status_code == 502 and response.is_json,
            f"status={response.status_code} body={response.data[:80]!r}",
        )

        settings.hub.debian_upstream = ""
        settings.hub.debian_mirror = ""
        response = client_get(client, "/debian/dists/bookworm/Release")
        check(
            "no upstream configured -> 404 JSON",
            response.status_code == 404
            and response.is_json
            and "local flat repository" in response.get_json().get("error", ""),
            f"status={response.status_code} body={response.data[:120]!r}",
        )
        response = client_get(client, "/debian/pool/main/t/tiny/tiny_1.0_arm64.deb")
        check(
            "no upstream configured on pool -> 404 JSON",
            response.status_code == 404 and response.is_json,
            f"status={response.status_code} body={response.data[:80]!r}",
        )

        # ── Path safety ──────────────────────────────────────────────
        print()
        print("── path safety ─────────────────────────────────────────────────")
        raised = False
        try:
            debian_apt.safe_mirror_path("../etc/passwd")
        except debian_apt.PathError:
            raised = True
        check("safe_mirror_path rejects '..'", raised)

        raised = False
        try:
            debian_apt.safe_mirror_path("dists/../../etc/passwd")
        except debian_apt.PathError:
            raised = True
        check("safe_mirror_path rejects embedded traversal", raised)

        traversal = client_get(
            client, "/debian/dists/%2e%2e/%2e%2e/etc/passwd"
        )
        check(
            "traversal URL never proxies the upstream",
            traversal.status_code in (400, 404),
            f"status={traversal.status_code}",
        )

        # ── Local mirror tree is preferred over the upstream ─────────
        print()
        print("── local dists/ + pool/ mirror ─────────────────────────────────")
        LOCAL_RELEASE = (
            b"Origin: OpenFish synced mirror\n"
            b"Label: OpenFish\n"
            b"Suite: stable\n"
            b"Codename: bookworm\n"
        )
        LOCAL_PACKAGES = (
            b"Package: local\n"
            b"Version: 2.0\n"
            b"Architecture: amd64\n"
            b"Filename: pool/main/l/local/local_2.0_amd64.deb\n"
        )
        LOCAL_DEB = bytes(range(256)) * 4
        mirror_root = _TMP / "mirror"
        (mirror_root / "dists/bookworm/main/binary-amd64").mkdir(parents=True)
        (mirror_root / "dists/bookworm/Release").write_bytes(LOCAL_RELEASE)
        (mirror_root / "dists/bookworm/main/binary-amd64/Packages").write_bytes(
            LOCAL_PACKAGES
        )
        (mirror_root / "pool/main/l/local").mkdir(parents=True)
        local_deb_path = "/debian/pool/main/l/local/local_2.0_amd64.deb"
        (mirror_root / "pool/main/l/local/local_2.0_amd64.deb").write_bytes(LOCAL_DEB)

        saved_dir = settings.hub.debian_dir
        saved_cache = settings.hub.debian_cache_dir
        saved_upstream = settings.hub.debian_upstream
        settings.hub.debian_dir = str(mirror_root)
        settings.hub.debian_cache_dir = str(_TMP / "cache-local")

        # Upstream is configured and would answer: the local copy must win and
        # the mirror must not be contacted.
        settings.hub.debian_upstream = mirror
        before = _MirrorHandler.count(release_path)
        response = client_get(client, "/debian/dists/bookworm/Release")
        after = _MirrorHandler.count(release_path)
        check(
            "local Release wins over a configured upstream",
            response.status_code == 200
            and response.data == LOCAL_RELEASE
            and after == before,
            f"status={response.status_code} bytes={len(response.data)} "
            f"mirror_before={before} mirror_after={after}",
        )

        # Upstream would fail: the local copy must still be served.
        settings.hub.debian_upstream = f"http://127.0.0.1:{_free_port()}"
        response = client_get(client, local_deb_path)
        check(
            "local pool .deb served while the upstream is unreachable",
            response.status_code == 200 and response.data == LOCAL_DEB,
            f"status={response.status_code} bytes={len(response.data)}",
        )

        # No upstream at all: the local copy must still be served.
        settings.hub.debian_upstream = ""
        response = client_get(
            client, "/debian/dists/bookworm/main/binary-amd64/Packages"
        )
        check(
            "local Packages served with no upstream configured",
            response.status_code == 200 and response.data == LOCAL_PACKAGES,
            f"status={response.status_code} bytes={len(response.data)}",
        )

        response = client_get(client, local_deb_path, headers={"Range": "bytes=10-19"})
        check(
            "local pool Range -> 206 with the right Content-Range",
            response.status_code == 206
            and response.data == LOCAL_DEB[10:20]
            and response.headers.get("Content-Range")
            == f"bytes 10-19/{len(LOCAL_DEB)}",
            f"status={response.status_code} bytes={len(response.data)} "
            f"Content-Range={response.headers.get('Content-Range')!r}",
        )

        response = client.open(local_deb_path, method="HEAD", headers=AUTH)
        check(
            "local pool HEAD returns headers without a body",
            response.status_code == 200
            and response.data == b""
            and response.headers.get("Content-Length") == str(len(LOCAL_DEB)),
            f"status={response.status_code} bytes={len(response.data)} "
            f"Content-Length={response.headers.get('Content-Length')!r}",
        )

        response = client_get(client, "/debian/dists/%2e%2e/%2e%2e/etc/passwd")
        check(
            "local traversal path is refused",
            400 <= response.status_code < 500,
            f"status={response.status_code} body={response.data[:60]!r}",
        )

        # A symlink inside the tree pointing outside it must not be served.
        secret = _TMP / "outside-secret"
        secret.write_bytes(b"TOP SECRET\n")
        (mirror_root / "dists/bookworm/leak").symlink_to(secret)
        response = client_get(client, "/debian/dists/bookworm/leak")
        check(
            "symlink escaping the local mirror is refused",
            400 <= response.status_code < 500 and b"TOP SECRET" not in response.data,
            f"status={response.status_code} body={response.data[:60]!r}",
        )

        # A local miss must still fall back to the upstream.
        settings.hub.debian_upstream = mirror
        settings.hub.debian_cache_dir = str(_TMP / "cache-fallback")
        response = client_get(
            client, "/debian/dists/bookworm/main/binary-arm64/Packages"
        )
        check(
            "local dists miss falls back to the upstream",
            response.status_code == 200 and response.data == PACKAGES,
            f"status={response.status_code} bytes={len(response.data)}",
        )
        response = client_get(
            client, "/debian/pool/main/t/tiny/tiny_1.0_arm64.deb"
        )
        check(
            "local pool miss falls back to the upstream",
            response.status_code == 200 and response.data == POOL_BLOB,
            f"status={response.status_code} bytes={len(response.data)}",
        )

        settings.hub.debian_dir = saved_dir
        settings.hub.debian_cache_dir = saved_cache
        settings.hub.debian_upstream = saved_upstream

        # ── Anonymous access ─────────────────────────────────────────
        print()
        print("── auth ────────────────────────────────────────────────────────")
        settings.hub.debian_upstream = mirror
        for path in (
            "/debian/dists/bookworm/Release",
            "/debian/pool/main/t/tiny/tiny_1.0_arm64.deb",
        ):
            response = client.get(path)
            check(
                f"anonymous {path} -> 401",
                response.status_code == 401,
                f"status={response.status_code}",
            )

        # ── The flat local repository must keep working ──────────────
        print()
        print("── local flat repository (regression) ──────────────────────────")
        response = client_get(client, "/debian/")
        check(
            "/debian/ still renders the catalog",
            response.status_code == 200 and response.data,
            f"status={response.status_code}",
        )

        response = client_get(client, "/debian/Packages")
        check(
            "/debian/Packages still serves text/plain",
            response.status_code == 200
            and "text/plain" in response.headers.get("Content-Type", ""),
            f"status={response.status_code} Content-Type="
            f"{response.headers.get('Content-Type')!r}",
        )

        local_files = sorted(
            p for p in Path(settings.hub.debian_dir).iterdir() if p.is_file()
        )
        if local_files:
            response = client_get(client, f"/debian/files/{local_files[0].name}")
            check(
                "/debian/files/<f> still downloads",
                response.status_code == 200 and response.data,
                f"status={response.status_code} file={local_files[0].name}",
            )
        else:
            print("ℹ️  no local file in DEBIAN_DIR — skipping /debian/files/ check")

        response = client_get(client, "/api/v1/debian")
        check(
            "/api/v1/debian still serves the catalog",
            response.status_code == 200 and response.is_json,
            f"status={response.status_code}",
        )

        # ── DEBIAN_MIRROR fallback ───────────────────────────────────
        print()
        print("── DEBIAN_MIRROR fallback ──────────────────────────────────────")
        settings.hub.debian_upstream = ""
        settings.hub.debian_mirror = mirror
        response = client_get(client, "/debian/dists/bookworm/Release")
        check(
            "URL-shaped DEBIAN_MIRROR is used when DEBIAN_UPSTREAM is empty",
            response.status_code == 200 and response.data == RELEASE,
            f"status={response.status_code} bytes={len(response.data)}",
        )
        settings.hub.debian_mirror = ""

    finally:
        server.shutdown()
        server.server_close()
        shutil.rmtree(_TMP, ignore_errors=True)

    print()
    if FAILURES:
        print(f"❌ debian proxy check FAILED — {len(FAILURES)} of {CHECKS} check(s)")
        for message in FAILURES:
            print("   " + message)
        return 1

    print(f"✅ debian proxy check passed — {CHECKS} check(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
