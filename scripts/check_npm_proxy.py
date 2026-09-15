#!/usr/bin/env python
"""Gate: the npm registry protocol must actually work against the real routes.

Run from the repository root, with **no network** required::

    python scripts/check_npm_proxy.py

The server now claims to speak the npm registry protocol, not just to publish a
static catalog.  A claim like that is only worth anything if a client can read
it, so this gate drives the real Flask app through ``app.test_client()`` with
HTTP Basic credentials and checks the wire shapes npm depends on:

* a packument, full and abbreviated, with every version carrying the mandatory
  dependency maps, ``bin`` and a rewritten absolute ``dist.tarball``;
* a version manifest, by exact version and by dist-tag;
* a tarball whose bytes are byte-for-byte the file on disk;
* ``/-/v1/search`` with npm's exact envelope and case-insensitive filtering;
* scoped packages (``@scope/name``) resolving to the packument, not to a
  ``<package>/<version>`` manifest — the route-ambiguity regression;
* an unknown package as a JSON ``404``, never a ``500``;
* an anonymous request as ``401``;
* the read-through proxy: a package and a tarball fetched from a *fake* upstream
  running in a background thread, then served from cache with no second hit.

The local fixtures are tiny ``.tgz`` files built here with ``tarfile``, so the
whole gate runs offline and leaves nothing behind in the repository.
"""

from __future__ import annotations

import base64
import collections
import hashlib
import io
import json
import os
import shutil
import sys
import tarfile
import tempfile
import threading
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
os.chdir(REPO_ROOT)

# ── A throwaway identity/database, set before the app is imported ────
_TMP = Path(tempfile.mkdtemp(prefix="openfish-npm-gate-"))
_NPM_DIR = _TMP / "npm"
_CACHE_DIR = _TMP / "cache"
_NPM_DIR.mkdir(parents=True, exist_ok=True)

os.environ["API_KEYS_FILE"] = str(_TMP / "gate.db")
os.environ["AUTH_USERNAME"] = "dev"
os.environ["AUTH_ASSERT"] = "devpass"
os.environ["ADMIN_USERS"] = '["dev"]'
os.environ["OAUTH2_INTROSPECT_URL"] = ""
os.environ["OAUTH2_AUTHORIZE_URL"] = ""
os.environ["NPM_DIR"] = str(_NPM_DIR)
os.environ["NPM_CACHE_DIR"] = str(_CACHE_DIR)
os.environ["NPM_PROXY_ENABLED"] = "false"

from werkzeug.serving import make_server  # noqa: E402

from app import app  # noqa: E402
from config import settings  # noqa: E402

AUTH = {"Authorization": "Basic " + base64.b64encode(b"dev:devpass").decode("ascii")}

failures: list[str] = []
checks = 0


def check(condition: bool, label: str) -> None:
    global checks
    checks += 1
    print(("   ✅ " if condition else "   ✗ ") + label)
    if not condition:
        failures.append(label)


def section(title: str) -> None:
    print(f"\n── {title} " + "─" * max(0, 60 - len(title)))


def get(client, path: str, *, auth: bool = True, headers: dict | None = None):
    merged = dict(headers or {})
    if auth:
        merged.update(AUTH)
    return client.get(path, headers=merged)


def build_tarball_bytes(name: str, version: str, description: str) -> bytes:
    """A minimal but valid npm tarball: ``package/package.json`` + an index."""
    manifest = {
        "name": name,
        "version": version,
        "description": description,
        "keywords": ["openfish", "fixture"],
        "dependencies": {"left-pad": "^1.3.0"},
        "optionalDependencies": {"fsevents": "^2.0.0"},
        "peerDependencies": {"react": ">=16"},
        "bin": {name.split("/")[-1]: "index.js"},
    }
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for member, payload in (
            ("package/package.json", json.dumps(manifest).encode("utf-8")),
            ("package/index.js", b"module.exports = 42;\n"),
        ):
            info = tarfile.TarInfo(member)
            info.size = len(payload)
            info.mtime = 0
            archive.addfile(info, io.BytesIO(payload))
    return buffer.getvalue()


def write_tarball(directory: Path, filename: str, data: bytes) -> Path:
    path = directory / filename
    path.write_bytes(data)
    return path


# ── Local fixtures ───────────────────────────────────────────────────
DEMO_NAME, DEMO_VERSION = "openfish-demo", "1.2.3"
DEMO_NEWER = "1.3.0"
SCOPED_NAME, SCOPED_VERSION = "@openfish/scoped", "0.5.0"
#: A package the mirror has *partially* synced: only ``1.0.0`` is on disk while
#: the upstream also publishes ``2.0.0``.  The proxy must merge the two, or a
#: dependency on ``^2.0.0`` would be unresolvable (the express/accepts ETARGET).
MERGE_NAME, MERGE_LOCAL, MERGE_UPSTREAM = "openfish-partial", "1.0.0", "2.0.0"

DEMO_123 = build_tarball_bytes(DEMO_NAME, DEMO_VERSION, "Openfish protocol fixture")
DEMO_130 = build_tarball_bytes(DEMO_NAME, DEMO_NEWER, "Openfish protocol fixture")
SCOPED = build_tarball_bytes(SCOPED_NAME, SCOPED_VERSION, "Scoped protocol fixture")
MERGE_BYTES = build_tarball_bytes(MERGE_NAME, MERGE_LOCAL, "Partial-sync fixture")

write_tarball(_NPM_DIR, f"{DEMO_NAME}-{DEMO_VERSION}.tgz", DEMO_123)
write_tarball(_NPM_DIR, f"{DEMO_NAME}-{DEMO_NEWER}.tgz", DEMO_130)
# npm's scoped tarball naming drops the scope from the file name.
write_tarball(_NPM_DIR, f"scoped-{SCOPED_VERSION}.tgz", SCOPED)
write_tarball(_NPM_DIR, f"{MERGE_NAME}-{MERGE_LOCAL}.tgz", MERGE_BYTES)
# A catalog.json entry with no tarball on disk: metadata only, but still listed.
META_NAME, META_VERSION = "openfish-meta", "0.1.0"
(_NPM_DIR / "catalog.json").write_text(json.dumps({
    "packages": [{
        "name": META_NAME,
        "version": META_VERSION,
        "description": "catalog metadata fixture",
        "tags": ["meta"],
    }],
}), encoding="utf-8")

settings.hub.npm_dir = str(_NPM_DIR)
settings.hub.npm_cache_dir = str(_CACHE_DIR)
settings.hub.npm_proxy_enabled = False


def main() -> int:
    client = app.test_client()

    section("Local packument (full)")
    response = get(client, f"/npm/{DEMO_NAME}")
    check(response.status_code == 200, f"GET /npm/{DEMO_NAME} -> 200")
    document = response.get_json() or {}
    check(document.get("name") == DEMO_NAME, "packument carries its name")
    check(document.get("dist-tags") == {"latest": DEMO_NEWER},
          f"dist-tags.latest is the highest version ({DEMO_NEWER})")
    check(set(document.get("versions") or {}) == {DEMO_VERSION, DEMO_NEWER},
          "every version is present")
    check(bool(document.get("modified")), "packument carries `modified`")

    manifest = (document.get("versions") or {}).get(DEMO_VERSION) or {}
    for key in ("name", "version", "dependencies", "optionalDependencies",
                "peerDependencies", "bin", "dist"):
        check(key in manifest, f"version manifest carries `{key}`")
    check(manifest.get("dependencies") == {"left-pad": "^1.3.0"},
          "version manifest carries real dependencies")
    dist = manifest.get("dist") or {}
    tarball_url = dist.get("tarball") or ""
    check(tarball_url.startswith("http://") and
          tarball_url.endswith(f"/npm/{DEMO_NAME}/-/{DEMO_NAME}-{DEMO_VERSION}.tgz"),
          f"dist.tarball rewritten to this server: {tarball_url}")
    check(dist.get("shasum") == hashlib.sha1(DEMO_123).hexdigest(),
          "dist.shasum is the tarball's sha1")
    check(dist.get("integrity", "").startswith("sha512-"), "dist.integrity is SRI")

    section("Abbreviated packument")
    abbreviated = get(client, f"/npm/{DEMO_NAME}", headers={
        "Accept": "application/vnd.npm.install-v1+json",
    })
    check(abbreviated.status_code == 200, "abbreviated request -> 200")
    abbr = abbreviated.get_json() or {}
    check(set(abbr) == {"name", "dist-tags", "versions", "modified"},
          "abbreviated document keeps only name/dist-tags/versions/modified")
    abbr_manifest = (abbr.get("versions") or {}).get(DEMO_VERSION) or {}
    for key in ("name", "version", "dependencies", "optionalDependencies",
                "peerDependencies", "bin", "dist"):
        check(key in abbr_manifest, f"abbreviated manifest carries `{key}`")
    check("description" not in abbr_manifest,
          "abbreviated manifest drops the full-only `description`")

    section("Version manifest")
    one = get(client, f"/npm/{DEMO_NAME}/{DEMO_VERSION}")
    check(one.status_code == 200, f"GET /npm/{DEMO_NAME}/{DEMO_VERSION} -> 200")
    check((one.get_json() or {}).get("version") == DEMO_VERSION, "exact version resolves")
    tagged = get(client, f"/npm/{DEMO_NAME}/latest")
    check(tagged.status_code == 200 and
          (tagged.get_json() or {}).get("version") == DEMO_NEWER,
          "dist-tag `latest` resolves to the tagged version")
    missing = get(client, f"/npm/{DEMO_NAME}/99.99.99")
    check(missing.status_code == 404, "unknown version -> 404")

    section("Scoped route ambiguity")
    scoped_doc = get(client, f"/npm/{SCOPED_NAME}")
    check(scoped_doc.status_code == 200, f"GET /npm/{SCOPED_NAME} -> 200")
    check((scoped_doc.get_json() or {}).get("name") == SCOPED_NAME,
          "scoped path resolves to the *packument*, not a <pkg>/<version> manifest")
    scoped_manifest = get(client, f"/npm/{SCOPED_NAME}/{SCOPED_VERSION}")
    check(scoped_manifest.status_code == 200 and
          (scoped_manifest.get_json() or {}).get("version") == SCOPED_VERSION,
          "scoped path with a version resolves to the manifest")
    scoped_dist = ((scoped_doc.get_json() or {}).get("versions") or {}).get(
        SCOPED_VERSION, {}).get("dist", {})
    check(scoped_dist.get("tarball", "").endswith(
        f"/npm/{SCOPED_NAME}/-/{SCOPED_NAME.split('/')[-1]}-{SCOPED_VERSION}.tgz"),
        "scoped dist.tarball keeps the scope in the URL")
    # The file is named `scoped-0.5.0.tgz` (npm drops the scope from the
    # basename). Parsing that filename must not invent a package called
    # `scoped` whose manifest has no tarball — the react/@floating-ui/react
    # collision that made `npm install react` fail with an invalid manifest.
    basename_pkg = get(client, f"/npm/{SCOPED_NAME.split('/')[-1]}")
    check(basename_pkg.status_code == 404,
          "a scoped tarball's scope-less basename is not invented as a package")

    section("Tarballs")
    downloaded = get(client, f"/npm/{DEMO_NAME}/-/{DEMO_NAME}-{DEMO_VERSION}.tgz")
    check(downloaded.status_code == 200, "local tarball -> 200")
    check(downloaded.data == DEMO_123, "tarball bytes are identical to the file on disk")
    check(downloaded.headers.get("Content-Type") == "application/octet-stream",
          "tarball Content-Type is application/octet-stream")
    scoped_download = get(
        client,
        f"/npm/{SCOPED_NAME}/-/{SCOPED_NAME.split('/')[-1]}-{SCOPED_VERSION}.tgz",
    )
    check(scoped_download.status_code == 200 and scoped_download.data == SCOPED,
          "scoped tarball streams the right bytes")
    # The scoped file is `scoped-0.5.0.tgz`. An unscoped package called `scoped`
    # does not exist, so asking for that basename as `scoped` must not hand back
    # another package's bytes — it is a miss, not a silent EINTEGRITY later.
    colliding = get(client, f"/npm/scoped/-/{SCOPED_NAME.split('/')[-1]}-{SCOPED_VERSION}.tgz")
    check(colliding.status_code == 404,
          "a tarball basename is not served for a package that does not own it")

    section("Local tree changes are noticed")
    # Adding a file must invalidate the cached index; the cheap directory-mtime
    # signature has to see it just as the old per-file stat walk did.
    LATE_NAME, LATE_VERSION = "openfish-late", "9.9.9"
    write_tarball(_NPM_DIR, f"{LATE_NAME}-{LATE_VERSION}.tgz",
                  build_tarball_bytes(LATE_NAME, LATE_VERSION, "Late fixture"))
    late_doc = get(client, f"/npm/{LATE_NAME}")
    check(late_doc.status_code == 200 and LATE_VERSION in
          ((late_doc.get_json() or {}).get("versions") or {}),
          "a tarball added after the first index build is picked up")

    section("Search (/-/v1/search)")
    found = get(client, "/npm/-/v1/search?text=demo")
    check(found.status_code == 200, "GET /-/v1/search -> 200")
    payload = found.get_json() or {}
    check({"objects", "total", "time"} <= set(payload), "envelope has objects/total/time")
    objects = payload.get("objects") or []
    check(len(objects) == 1 and objects[0]["package"]["name"] == DEMO_NAME,
          "text=demo matches only the demo package")
    first = objects[0] if objects else {}
    package = first.get("package") or {}
    check({"name", "version", "description", "date", "links", "publisher",
           "maintainers", "keywords"} <= set(package), "package object is complete")
    check("npm" in (package.get("links") or {}), "package.links.npm is present")
    score = first.get("score") or {}
    check({"final", "detail"} <= set(score) and
          {"quality", "popularity", "maintenance"} <= set(score.get("detail") or {}),
          "score carries final + detail{quality,popularity,maintenance}")
    check("searchScore" in first, "object carries searchScore")
    check(payload.get("total") == 1, "total counts the merged matches")

    insensitive = get(client, "/npm/-/v1/search?text=DEMO")
    check(len((insensitive.get_json() or {}).get("objects") or []) == 1,
          "search is case-insensitive")
    by_description = get(client, "/npm/-/v1/search?text=protocol%20fixture")
    check(any(o["package"]["name"] == SCOPED_NAME
              for o in (by_description.get_json() or {}).get("objects") or []),
          "search matches the description")
    no_match = get(client, "/npm/-/v1/search?text=zzz-no-such-package")
    check((no_match.get_json() or {}).get("objects") == [], "no match -> empty objects")
    empty = get(client, "/npm/-/v1/search?text=")
    check(empty.status_code == 200 and (empty.get_json() or {}).get("total") == 0,
          "empty text -> empty result set, not an error")
    clamped = get(client, "/npm/-/v1/search?text=demo&size=9999&from=-5")
    check(clamped.status_code == 200, "out-of-range size/from clamp instead of erroring")

    section("Errors and auth")
    unknown = get(client, "/npm/definitely-not-a-real-package-xyz")
    check(unknown.status_code == 404, "unknown package -> 404")
    check(isinstance((unknown.get_json() or {}).get("error"), str),
          "404 body is JSON with an `error` string")
    scoped_unknown = get(client, "/npm/@nope/missing")
    check(scoped_unknown.status_code == 404, "unknown scoped package -> 404")

    anonymous = app.test_client()
    for path in (f"/npm/{DEMO_NAME}", "/npm/-/v1/search?text=demo",
                 f"/npm/{DEMO_NAME}/-/{DEMO_NAME}-{DEMO_VERSION}.tgz"):
        response = anonymous.get(path)
        check(response.status_code == 401, f"anonymous GET {path} -> 401")

    section("Existing catalog routes still work")
    check(get(client, "/npm/-/ping").status_code == 200, "/npm/-/ping -> 200")
    check(get(client, "/npm/-/all").status_code == 200, "/npm/-/all -> 200")
    check(get(client, "/api/v1/npm").status_code == 200, "/api/v1/npm -> 200")
    index = get(client, "/npm/", headers={"Accept": "application/json"})
    check(index.status_code == 200 and isinstance(index.get_json(), dict),
          "/npm/ with Accept: application/json -> 200 JSON")
    legacy = get(client, f"/npm/files/{DEMO_NAME}-{DEMO_VERSION}.tgz")
    check(legacy.status_code == 200 and legacy.data == DEMO_123,
          "/npm/files/<filename> still serves the tarball")
    meta = get(client, f"/npm/{META_NAME}")
    check(meta.status_code == 200 and META_VERSION in
          ((meta.get_json() or {}).get("versions") or {}),
          "a catalog.json metadata-only entry is still listed")

    section("Read-through proxy against a fake upstream")
    _run_proxy_checks()

    print()
    if failures:
        print(f"❌ npm proxy gate FAILED — {len(failures)} of {checks} checks failed")
        for message in failures:
            print("   " + message)
        return 1
    print(f"✅ npm proxy gate passed — {checks} checks")
    return 0


# ── Fake upstream + proxy checks ─────────────────────────────────────

PROXY_NAME, PROXY_VERSION = "proxy-demo", "2.0.1"
PROXY_TARBALL_PATH = f"/{PROXY_NAME}/-/{PROXY_NAME}-{PROXY_VERSION}.tgz"
PROXY_BYTES = build_tarball_bytes(PROXY_NAME, PROXY_VERSION, "Proxy fixture")


class FakeUpstream:
    """Minimal registry: one packument and its tarball, counting every hit."""

    def __init__(self) -> None:
        self.hits: collections.Counter[str] = collections.Counter()
        self.lock = threading.Lock()

    def __call__(self, environ, start_response):
        path = environ.get("PATH_INFO", "")
        with self.lock:
            self.hits[path] += 1
        if path == f"/{PROXY_NAME}":
            body = json.dumps({
                "name": PROXY_NAME,
                "dist-tags": {"latest": PROXY_VERSION},
                "versions": {
                    PROXY_VERSION: {
                        "name": PROXY_NAME,
                        "version": PROXY_VERSION,
                        "dependencies": {"left-pad": "^1.3.0"},
                        "dist": {
                            "tarball": f"https://upstream.invalid{PROXY_TARBALL_PATH}",
                            "shasum": "0" * 40,
                            "integrity": "sha512-AAAA",
                        },
                    },
                },
                "modified": "2024-01-01T00:00:00.000Z",
                "time": {"modified": "2024-01-01T00:00:00.000Z"},
            }).encode("utf-8")
            start_response("200 OK", [
                ("Content-Type", "application/json"),
                ("Content-Length", str(len(body))),
            ])
            return [body]
        if path == f"/{MERGE_NAME}":
            # The upstream knows two versions of a package the mirror has only
            # partially synced; 2.0.0 exists *only* upstream.
            body = json.dumps({
                "name": MERGE_NAME,
                "dist-tags": {"latest": MERGE_UPSTREAM},
                "versions": {
                    MERGE_LOCAL: {
                        "name": MERGE_NAME,
                        "version": MERGE_LOCAL,
                        "dist": {
                            "tarball": f"https://upstream.invalid/{MERGE_NAME}/-/{MERGE_NAME}-{MERGE_LOCAL}.tgz",
                            "shasum": "1" * 40,
                            "integrity": "sha512-BBBB",
                        },
                    },
                    MERGE_UPSTREAM: {
                        "name": MERGE_NAME,
                        "version": MERGE_UPSTREAM,
                        "dependencies": {"accepts": "^2.0.0"},
                        "dist": {
                            "tarball": f"https://upstream.invalid/{MERGE_NAME}/-/{MERGE_NAME}-{MERGE_UPSTREAM}.tgz",
                            "shasum": "2" * 40,
                            "integrity": "sha512-CCCC",
                        },
                    },
                },
                "modified": "2024-01-01T00:00:00.000Z",
                "time": {"modified": "2024-01-01T00:00:00.000Z"},
            }).encode("utf-8")
            start_response("200 OK", [
                ("Content-Type", "application/json"),
                ("Content-Length", str(len(body))),
            ])
            return [body]
        if path == PROXY_TARBALL_PATH:
            start_response("200 OK", [
                ("Content-Type", "application/octet-stream"),
                ("Content-Length", str(len(PROXY_BYTES))),
            ])
            return [PROXY_BYTES]
        start_response("404 Not Found", [("Content-Type", "application/json")])
        return [b'{"error": "not found"}']


def _run_proxy_checks() -> None:
    upstream = FakeUpstream()
    server = make_server("127.0.0.1", 0, upstream)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_port
    try:
        settings.hub.npm_proxy_enabled = True
        settings.hub.npm_upstream = f"http://127.0.0.1:{port}"
        settings.hub.npm_cache_dir = str(_CACHE_DIR)
        client = app.test_client()

        first = get(client, f"/npm/{PROXY_NAME}")
        check(first.status_code == 200, "unknown package is fetched from the fake upstream")
        document = first.get_json() or {}
        check(document.get("name") == PROXY_NAME, "proxied packument is returned")
        proxied_manifest = (document.get("versions") or {}).get(PROXY_VERSION) or {}
        check((proxied_manifest.get("dist") or {}).get("tarball", "").endswith(
            f"/npm/{PROXY_NAME}/-/{PROXY_NAME}-{PROXY_VERSION}.tgz"),
            "proxied dist.tarball is rewritten to this server")
        with upstream.lock:
            first_hits = upstream.hits[f"/{PROXY_NAME}"]
        check(first_hits == 1, "fake upstream saw the packument request once")

        second = get(client, f"/npm/{PROXY_NAME}")
        check(second.status_code == 200, "second packument request -> 200")
        with upstream.lock:
            after_second = upstream.hits[f"/{PROXY_NAME}"]
        check(after_second == 1, "second packument request was served from cache (no hit)")

        tarball = get(client, f"/npm/{PROXY_NAME}/-/{PROXY_NAME}-{PROXY_VERSION}.tgz")
        check(tarball.status_code == 200, "proxied tarball -> 200")
        check(tarball.data == PROXY_BYTES, "proxied tarball bytes match the upstream")
        with upstream.lock:
            tar_hits = upstream.hits[PROXY_TARBALL_PATH]
        check(tar_hits == 1, "fake upstream saw the tarball request once")

        cached = get(client, f"/npm/{PROXY_NAME}/-/{PROXY_NAME}-{PROXY_VERSION}.tgz")
        check(cached.status_code == 200 and cached.data == PROXY_BYTES,
              "second tarball request -> 200 with identical bytes")
        with upstream.lock:
            tar_after = upstream.hits[PROXY_TARBALL_PATH]
        check(tar_after == 1, "second tarball request was served from disk cache (no hit)")
        cached_bytes = [p.read_bytes() for p in _CACHE_DIR.rglob("*") if p.is_file()]
        check(PROXY_BYTES in cached_bytes,
              "the proxied tarball actually landed in the disk cache")

        missing = get(client, "/npm/no-such-proxied-pkg")
        check(missing.status_code == 404, "upstream 404 propagates as 404, not 500")

        section("Partially synced package merges the upstream versions")
        merged = get(client, f"/npm/{MERGE_NAME}")
        check(merged.status_code == 200, f"GET /npm/{MERGE_NAME} -> 200")
        merged_doc = merged.get_json() or {}
        merged_versions = merged_doc.get("versions") or {}
        check(MERGE_LOCAL in merged_versions,
              "the locally synced version is still present")
        check(MERGE_UPSTREAM in merged_versions,
              "an upstream-only version is *not* hidden by the local mirror")
        local_dist = (merged_versions.get(MERGE_LOCAL) or {}).get("dist") or {}
        check(local_dist.get("tarball", "").endswith(
            f"/npm/{MERGE_NAME}/-/{MERGE_NAME}-{MERGE_LOCAL}.tgz"),
            "the synced version's dist.tarball points at this server")
        check((merged_doc.get("dist-tags") or {}).get("latest") == MERGE_UPSTREAM,
              "upstream dist-tags survive the merge")
        local_tar = get(client, f"/npm/{MERGE_NAME}/-/{MERGE_NAME}-{MERGE_LOCAL}.tgz")
        check(local_tar.status_code == 200 and local_tar.data == MERGE_BYTES,
              "the synced version still streams the local bytes")
    finally:
        server.shutdown()
        thread.join(timeout=5)

    section("Upstream outage degrades, never 500")
    original_upstream = settings.hub.npm_upstream
    settings.hub.npm_upstream = "http://127.0.0.1:1"  # nothing listens here
    try:
        outage_client = app.test_client()
        degraded = get(outage_client, "/npm/-/v1/search?text=demo")
        check(degraded.status_code == 200 and any(
            item["package"]["name"] == DEMO_NAME
            for item in (degraded.get_json() or {}).get("objects") or []
        ), "dead upstream: search degrades to local-only results (200)")

        packument = get(outage_client, "/npm/some-package-from-a-dead-upstream")
        check(packument.status_code != 500 and packument.is_json,
              "dead upstream: packument miss is a JSON non-500")

        local_only = get(outage_client, f"/npm/{MERGE_NAME}")
        check(local_only.status_code == 200 and MERGE_LOCAL in
              ((local_only.get_json() or {}).get("versions") or {}),
              "dead upstream: a locally mirrored package still resolves (200)")

        tarball = get(outage_client, "/npm/some-package/-/some-package-1.0.0.tgz")
        check(tarball.status_code != 500 and tarball.is_json,
              "dead upstream: tarball miss is a JSON non-500")
    finally:
        settings.hub.npm_upstream = original_upstream
        settings.hub.npm_proxy_enabled = False


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)
