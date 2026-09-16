#!/usr/bin/env python
"""Gate: the Debian offline relay works end to end with no network.

Run from the backend directory (`backend/`)::

    python scripts/check_debian_offline.py

The script starts a fake apt mirror in a background thread, points the
application at it, seeds a throwaway *intranet* repository with two packages,
and drives the real Flask app through ``app.test_client()`` with HTTP Basic
credentials.  Only ``127.0.0.1`` is contacted, so the check is safe on an
air-gapped build host.

What is asserted

* ``GET /debian/offline`` reports the relay configuration;
* ``GET /debian/offline/snapshot`` walks the upstream ``Packages`` index and
  returns a self-describing text snapshot whose ``X-Openfish-Sha256`` matches
  its body;
* ``POST /debian/offline/plan`` diffs that snapshot against the intranet repo:
  missing packages are ``install``/``missing``, a locally-newer package is
  skipped, a satisfied dependency is not pulled in, and an unsatisfiable
  dependency is reported in the header instead of dropped;
* ``POST /debian/offline/bundle`` re-resolves the plan, downloads from the fake
  upstream, and packs a gzip tarball whose manifest lists every file; a package
  whose declared size does not match the bytes is skipped, not bundled;
* a plan whose declared size is over ``DEBIAN_OFFLINE_MAX_MB`` is refused with
  ``400`` before anything is downloaded;
* feeding the wrong artifact to a step is a ``400``, and a tampered snapshot is
  accepted but reported as ``snapshot_integrity: mismatch``;
* ``POST /debian/offline/import`` refuses a corrupted archive and writes
  *nothing*; a good archive is imported into ``DEBIAN_DIR`` (pool layout + flat
  copy), the flat ``/debian/Packages`` index lists the new packages, and a
  second import is a no-op;
* anonymous access is a ``401``;
* the pure helpers — Debian version comparison, ``.deb`` name parsing and
  dependency-clause parsing — behave as documented.

Any failure exits non-zero with a clear message.
"""

from __future__ import annotations

import base64
import hashlib
import io
import os
import shutil
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
os.chdir(REPO_ROOT)

_TMP = Path(tempfile.mkdtemp(prefix="debian-offline-check-"))
os.environ["SECRET_KEY"] = os.environ.get("SECRET_KEY") or "debian-offline-check"
os.environ["AUTH_USERNAME"] = "dev"
os.environ["AUTH_ASSERT"] = "devpass"
os.environ["ADMIN_USERS"] = '["dev"]'
os.environ["API_KEYS_FILE"] = str(_TMP / "keys.db")
os.environ["DEBIAN_DIR"] = str(_TMP / "intranet")
os.environ["DEBIAN_UPSTREAM"] = ""
os.environ["DEBIAN_MIRROR"] = ""
os.environ["DEBIAN_SUITES"] = "bookworm"
os.environ["DEBIAN_COMPONENTS"] = "main"
os.environ["DEBIAN_ARCHES"] = "amd64"
os.environ["DEBIAN_CACHE_DIR"] = str(_TMP / "cache")
os.environ["DEBIAN_METADATA_TTL"] = "300"
os.environ["DEBIAN_OFFLINE_DIR"] = str(_TMP / "offline")

from app import app  # noqa: E402
from config import settings  # noqa: E402
from services import debian_offline as relay  # noqa: E402

# ── The fake package universe ────────────────────────────────────────
#
# Each entry is (name, version, depends, declared_size_override).  The blobs are
# synthesized, so the manifest's SHA256 is the real digest of the fake bytes and
# verification is exercised rather than stubbed.

_BIG = b"\0" * (1536 * 1024)


def _blob(name: str) -> bytes:
    if name == "big":
        return _BIG
    return (name * 40).encode("utf-8") + b"\n"


_UNIVERSE = (
    ("curl", "8.5.0-2", "libc6 (>= 2.34), libfoo (>= 1.0)", None),
    ("libfoo", "1.2-1", "libbar (>= 2.0)", None),
    ("libbar", "2.1-1", "", None),
    ("libc6", "2.36-9", "", None),
    ("oldpkg", "1.0-1", "", None),
    ("ghosty", "1.0-1", "ghostlib (>= 1)", None),
    ("big", "1.0-1", "", None),
    # Declared one byte larger than the bytes served: the bundle step must
    # notice and skip it rather than ship a file that fails its own manifest.
    ("wrongsize", "1.0-1", "", 1),
)

_BLOBS: dict[str, bytes] = {}
_FILENAMES: dict[str, str] = {}
for _name, _version, _depends, _ in _UNIVERSE:
    _file = f"pool/main/{_name[0]}/{_name}/{_name}_{_version}_amd64.deb"
    _FILENAMES[_name] = _file
    _BLOBS[_file] = _blob(_name)


def _packages_document() -> bytes:
    stanzas: list[bytes] = []
    for name, version, depends, size_override in _UNIVERSE:
        filename = _FILENAMES[name]
        blob = _BLOBS[filename]
        size = len(blob) + (size_override or 0)
        lines = [
            f"Package: {name}".encode(),
            f"Version: {version}".encode(),
            b"Architecture: amd64",
            f"Filename: {filename}".encode(),
            f"Size: {size}".encode(),
            f"SHA256: {hashlib.sha256(blob).hexdigest()}".encode(),
        ]
        if depends:
            lines.append(f"Depends: {depends}".encode())
        lines.append(f"Description: {name} test package".encode())
        stanzas.append(b"\n".join(lines))
    return b"\n\n".join(stanzas) + b"\n"


PACKAGES = _packages_document()
PACKAGES_PATH = "/dists/bookworm/main/binary-amd64/Packages"


class _MirrorHandler(BaseHTTPRequestHandler):
    """A minimal read-only apt mirror: one Packages index and its pool files."""

    protocol_version = "HTTP/1.1"

    def log_message(self, *args, **kwargs) -> None:  # pragma: no cover
        pass

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        path = self.path.split("?", 1)[0]
        if path == PACKAGES_PATH:
            self._send(200, PACKAGES, "text/plain; charset=utf-8")
            return
        blob = _BLOBS.get(path.lstrip("/"))
        if blob is None:
            self._send(404, b"no such object\n", "text/plain; charset=utf-8")
            return
        self._send(200, blob, "application/vnd.debian.binary-package")

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


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


def _text(response) -> str:
    return response.data.decode("utf-8", errors="replace")


def _document(text: str) -> relay.Document:
    return relay.decode_document(text)


def _rows(document: relay.Document) -> dict[str, dict[str, str]]:
    return {row.get("name", ""): row for row in document.rows}


def _post_file(client, path: str, field: str, filename: str, data: bytes, **extra):
    payload = {field: (io.BytesIO(data), filename)}
    payload.update(extra)
    return client.post(
        path, data=payload, headers=dict(AUTH), content_type="multipart/form-data"
    )


def _plant(repo: Path, filename: str, content: bytes = b"seed\n") -> None:
    repo.mkdir(parents=True, exist_ok=True)
    (repo / filename).write_bytes(content)


def _deb_count(repo: Path) -> int:
    return len(list(repo.rglob("*.deb"))) if repo.is_dir() else 0


# ── Pure helpers ─────────────────────────────────────────────────────

def check_helpers() -> None:
    print("── pure helpers ────────────────────────────────────────────────")
    pairs = [
        ("1.0~rc1", "1.0", -1),
        ("1.0", "1.0~rc1", 1),
        ("2:1.0", "1.0", 1),
        ("1.0-2", "1.0-10", -1),
        ("1.0", "1.0", 0),
        ("1.0+b1", "1.0", 1),
        ("1.0-1", "1.0-1~bpo12+1", 1),
    ]
    for left, right, expected in pairs:
        actual = relay.compare_versions(left, right)
        check(
            f"version {left!r} vs {right!r} → {expected}",
            actual == expected,
            f"got {actual}",
        )

    parsed = relay.parse_deb_filename("vim_2%3a9.0.1-1_amd64.deb")
    check(
        "epoch in a pool filename is percent-decoded",
        parsed is not None and parsed.version == "2:9.0.1-1" and parsed.arch == "amd64",
        repr(parsed),
    )
    check(
        "a non-.deb name is refused",
        relay.parse_deb_filename("Packages") is None,
    )

    groups = relay.parse_dependency_groups("a (>= 1) | b, c:any, d (<< 2)")
    check(
        "dependency groups split alternatives from conjunctions",
        [[alt.name for alt in group] for group in groups] == [["a", "b"], ["c"], ["d"]],
        repr(groups),
    )
    check(
        "a version constraint is parsed",
        groups[0][0].op == ">=" and groups[0][0].version == "1",
        repr(groups[0][0]),
    )
    check(
        "satisfies honours the operator",
        relay.satisfies("2.1", ">=", "2.0")
        and not relay.satisfies("1.9", ">=", "2.0")
        and relay.satisfies("1.0~rc1", "<<", "1.0"),
    )

    text = relay.encode_document(
        relay.PLAN_MAGIC,
        title="t",
        header=[("file", "x.txt")],
        columns=("name", "version"),
        rows=[{"name": "curl", "version": "1.0"}],
    )
    document = relay.decode_document(text, expected_magic=relay.PLAN_MAGIC)
    check(
        "a document round-trips and self-verifies",
        document.integrity_ok
        and document.rows == [{"name": "curl", "version": "1.0"}]
        and document.get("file") == "x.txt",
        repr(document.rows),
    )
    truncated = text.replace("curl", "cur", 1)
    check(
        "an edited body fails the self-check",
        not relay.decode_document(truncated).integrity_ok,
    )

    universe = relay.Universe.build(
        [
            relay.Package(
                "postfix", "3.7", "amd64",
                provides="mail-transport-agent (= 1.0)",
            )
        ]
    )
    check(
        "a versioned Provides registers the bare virtual name",
        "mail-transport-agent" in universe.providers
        and "(=" not in universe.providers
        and "1.0)" not in universe.providers,
        repr(sorted(universe.providers)),
    )


# ── The HTTP protocol ────────────────────────────────────────────────

def check_protocol(client) -> None:
    repo = Path(settings.hub.debian_dir)
    _plant(repo, "libc6_2.36-9_amd64.deb")
    _plant(repo, "oldpkg_2.0-1_amd64.deb")
    baseline = _deb_count(repo)

    print()
    print("── relay status ────────────────────────────────────────────────")
    response = client.get("/debian/offline", headers=dict(AUTH))
    payload = response.get_json() or {}
    check(
        "status reports the relay configuration",
        response.status_code == 200
        and payload.get("suites") == ["bookworm"]
        and payload.get("configured") is True,
        f"status={response.status_code} body={payload}",
    )
    check(
        "anonymous access is refused",
        client.get("/debian/offline").status_code == 401,
    )

    print()
    print("── snapshot ────────────────────────────────────────────────────")
    response = client.get("/debian/offline/snapshot", headers=dict(AUTH))
    snapshot = _text(response)
    check(
        "snapshot walks the upstream Packages index",
        response.status_code == 200
        and snapshot.startswith(relay.SNAPSHOT_MAGIC)
        and "curl" in snapshot
        and "big" in snapshot,
        f"status={response.status_code} bytes={len(snapshot)}",
    )
    check(
        "snapshot carries a matching digest header",
        response.headers.get("X-Openfish-Sha256")
        == hashlib.sha256(response.data).hexdigest(),
        f"header={response.headers.get('X-Openfish-Sha256')!r}",
    )
    snapshot_doc = _document(snapshot)
    check(
        "snapshot self-verifies and lists every package",
        snapshot_doc.integrity_ok and len(snapshot_doc.rows) == len(_UNIVERSE),
        f"integrity={snapshot_doc.integrity_ok} rows={len(snapshot_doc.rows)}",
    )

    print()
    print("── plan (the intranet-side diff) ───────────────────────────────")
    response = _post_file(
        client, "/debian/offline/plan", "snapshot", "snapshot.txt", response.data
    )
    plan = _text(response)
    plan_doc = _document(plan)
    rows = _rows(plan_doc)
    check(
        "plan is emitted for the uploaded snapshot",
        response.status_code == 200 and plan.startswith(relay.PLAN_MAGIC),
        f"status={response.status_code} body={plan[:120]}",
    )
    check(
        "a missing package is an install",
        rows.get("curl", {}).get("action") == "install"
        and rows.get("curl", {}).get("source") == "missing",
        repr(rows.get("curl")),
    )
    check(
        "a locally-current package is skipped",
        "libc6" not in rows,
        repr(sorted(rows)),
    )
    check(
        "a locally-newer package is not downgraded",
        "oldpkg" not in rows,
        repr(sorted(rows)),
    )
    check(
        "a locally-current dependency is not pulled in",
        "libc6" not in rows,
    )
    check(
        "an unsatisfiable dependency is reported, not dropped",
        "ghostlib" in plan_doc.get("unresolved_1", ""),
        f"unresolved={plan_doc.get('unresolved')!r}",
    )
    check(
        "the header records the source snapshot digest",
        plan_doc.get("snapshot_sha256")
        == hashlib.sha256(snapshot.encode("utf-8")).hexdigest(),
    )

    # A targeted plan exercises the dependency closure on its own: only `curl`
    # is named, so `libfoo`/`libbar` can only arrive as `dependency` rows.
    response = _post_file(
        client, "/debian/offline/plan", "snapshot", "snapshot.txt",
        snapshot.encode("utf-8"), only="curl",
    )
    targeted = _rows(_document(_text(response)))
    check(
        "a targeted plan pulls the closure in as dependencies",
        response.status_code == 200
        and set(targeted) == {"curl", "libfoo", "libbar"}
        and targeted.get("libfoo", {}).get("source") == "dependency"
        and targeted.get("libbar", {}).get("source") == "dependency",
        f"status={response.status_code} rows={targeted}",
    )

    print()
    print("── bundle (the internet-side pack) ─────────────────────────────")
    response = _post_file(
        client, "/debian/offline/bundle", "plan", "plan.txt", plan.encode("utf-8")
    )
    bundle_info = response.get_json() or {}
    check(
        "bundle build answers with the archive metadata",
        response.status_code == 201
        and bundle_info.get("packages") == 5
        and bundle_info.get("skipped") == 1,
        f"status={response.status_code} body={bundle_info}",
    )
    skipped_names = {item.get("name") for item in bundle_info.get("skipped_packages", [])}
    check(
        "a package whose bytes do not match its declared size is skipped",
        skipped_names == {"wrongsize"},
        repr(skipped_names),
    )
    bundle_url = bundle_info.get("download_url", "")
    response = client.get(bundle_url, headers=dict(AUTH))
    bundle_bytes = response.data
    check(
        "the built bundle downloads and matches its digest",
        response.status_code == 200
        and hashlib.sha256(bundle_bytes).hexdigest() == bundle_info.get("sha256"),
        f"status={response.status_code} bytes={len(bundle_bytes)}",
    )
    check(
        "the bundle is a gzip archive",
        bundle_bytes[:2] == b"\x1f\x8b",
        repr(bundle_bytes[:4]),
    )

    print()
    print("── refusal paths ───────────────────────────────────────────────")
    response = _post_file(
        client, "/debian/offline/bundle", "plan", "plan.txt", snapshot.encode("utf-8")
    )
    check(
        "the wrong artifact for a step is a 400",
        response.status_code == 400,
        f"status={response.status_code} body={_text(response)[:120]}",
    )
    trailer = f"sha256\t{'0' * 64}"
    lines = snapshot.rstrip("\n").splitlines()
    lines[-1] = trailer
    tampered = "\n".join(lines) + "\n"
    response = _post_file(
        client, "/debian/offline/plan", "snapshot", "snapshot.txt", tampered.encode("utf-8")
    )
    check(
        "a tampered snapshot is accepted but flagged",
        response.status_code == 200
        and _document(_text(response)).get("snapshot_integrity") == "mismatch",
        f"status={response.status_code} header="
        f"{_document(_text(response)).get('snapshot_integrity')!r}",
    )
    response = client.post(
        "/debian/offline/plan", headers=dict(AUTH), data=b""
    )
    check(
        "an empty plan request is a 400",
        response.status_code == 400,
        f"status={response.status_code}",
    )

    previous_max = settings.hub.debian_offline_max_mb
    settings.hub.debian_offline_max_mb = 1
    try:
        response = _post_file(
            client, "/debian/offline/bundle", "plan", "plan.txt", plan.encode("utf-8")
        )
        check(
            "a plan over DEBIAN_OFFLINE_MAX_MB is refused before download",
            response.status_code == 400
            and "DEBIAN_OFFLINE_MAX_MB" in _text(response),
            f"status={response.status_code} body={_text(response)[:140]}",
        )
    finally:
        settings.hub.debian_offline_max_mb = previous_max

    print()
    print("── import (the intranet-side unpack) ───────────────────────────")
    corrupted = bytearray(bundle_bytes)
    corrupted[len(corrupted) // 2] ^= 0xFF
    response = _post_file(
        client, "/debian/offline/import", "bundle", "bundle.tar.gz", bytes(corrupted)
    )
    check(
        "a corrupted bundle is refused",
        response.status_code == 400,
        f"status={response.status_code} body={_text(response)[:140]}",
    )
    check(
        "a refused import writes nothing",
        _deb_count(repo) == baseline,
        f"count={_deb_count(repo)} baseline={baseline}",
    )

    response = _post_file(
        client, "/debian/offline/import", "bundle", "bundle.tar.gz", bundle_bytes
    )
    report = response.get_json() or {}
    check(
        "a good bundle is imported",
        response.status_code == 200
        and report.get("imported") == 5
        and report.get("failed") == 0,
        f"status={response.status_code} body={report}",
    )
    check(
        "imported packages land in the pool layout and at the repo root",
        (repo / _FILENAMES["curl"]).is_file()
        and (repo / "curl_8.5.0-2_amd64.deb").is_file(),
        repr(sorted(p.name for p in repo.iterdir())[:6]),
    )
    response = client.get("/debian/Packages", headers=dict(AUTH))
    flat = _text(response)
    check(
        "the flat apt index lists the imported packages",
        response.status_code == 200
        and "Package: curl" in flat
        and "Filename: files/curl_8.5.0-2_amd64.deb" in flat,
        f"status={response.status_code} bytes={len(flat)}",
    )
    response = client.get("/api/v1/debian", headers=dict(AUTH))
    check(
        "the debian catalog counts the imported packages",
        response.status_code == 200
        and response.get_json().get("artifact_count") == baseline + 5,
        f"count={response.get_json().get('artifact_count')} expected={baseline + 5}",
    )
    response = _post_file(
        client, "/debian/offline/import", "bundle", "bundle.tar.gz", bundle_bytes
    )
    report = response.get_json() or {}
    check(
        "re-importing the same bundle is a no-op",
        response.status_code == 200
        and report.get("imported") == 0
        and report.get("skipped") == 5,
        f"body={report}",
    )


def main() -> int:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _MirrorHandler)
    mirror = f"http://127.0.0.1:{server.server_address[1]}"
    threading.Thread(target=server.serve_forever, daemon=True).start()

    client = app.test_client()
    settings.hub.debian_upstream = mirror
    settings.hub.debian_mirror = ""

    try:
        check_helpers()
        check_protocol(client)
    finally:
        server.shutdown()
        server.server_close()
        shutil.rmtree(_TMP, ignore_errors=True)

    print()
    if FAILURES:
        print(f"❌ debian offline check FAILED — {len(FAILURES)} of {CHECKS} check(s)")
        for message in FAILURES:
            print("   " + message)
        return 1

    print(f"✅ debian offline check passed — {CHECKS} check(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
