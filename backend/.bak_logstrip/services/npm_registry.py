"""npm registry protocol — packuments, manifests, tarballs and search.

``routes/npm.py`` is a thin HTTP shell; the registry semantics live here. The
npm protocol is small but has three shapes that must not be mixed up:

* a **packument** — one package's ``name``, ``dist-tags`` and every published
  ``versions`` entry, answered in a *full* form (``application/json``) or the
  *abbreviated* install form (``application/vnd.npm.install-v1+json``);
* a **version manifest** — one entry of ``versions``, fetched by exact version
  or by dist-tag (``npm view pkg@latest``);
* a **tarball** — the ``.tgz`` that a manifest's ``dist.tarball`` points at.

Every ``dist.tarball`` this module emits is rewritten to an *absolute URL on
this server* (``{prefix}/npm/<package>/-/<basename>``), which is the whole point
of the proxy: a client that can reach this host never has to reach the upstream
mirror, and the second request for a tarball is served from local disk.

Sources are layered, cheapest first, but when the proxy is enabled they are
**merged** rather than taken from the first source that knows the package, because
a package's *whole* ``versions`` map is what npm resolves dependency ranges
against:

1. the upstream registry, when ``npm_proxy_enabled`` and ``npm_upstream`` are
   set — packuments cached for :data:`PACKUMENT_TTL` seconds and tarballs cached
   until the ``DiskCache`` byte budget evicts them;
2. a real ``*.tgz`` in ``NPM_DIR`` (hashes computed from the file, metadata read
   from the ``package/package.json`` inside it) — it *wins for its own version*
   (this server serves those bytes) but must not hide the upstream's other
   versions;
3. ``NPM_DIR/catalog.json`` entries (metadata-only listings), used only when
   neither of the above knows the version.

Taking the local mirror alone would be enough for a fully synced package, but a
mirror that synced only ``accepts@1.3.8`` would then answer `GET /npm/accepts`
with that single version — and every ``accepts@^2.0.0`` dependency (express 5.x,
for one) would die with ``ETARGET``.  So an upstream packument is the base and
local versions are overlaid on top of it.

A missing package is a ``None`` return, never an exception, so the route layer
can answer the clean ``404 {"error": ...}`` npm expects.  An *unreachable*
upstream degrades the same way: search falls back to local results, a packument
request becomes 404 — a proxy outage must not become a 500.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import tarfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import unquote, urlparse

from services import hub
from services.fileio import read_json
from services.format import iso_from_timestamp, utc_now_iso
from services.upstream import DiskCache, Upstream, UpstreamError

logger = logging.getLogger("cpypiserver.npm")

#: How long a packument fetched from the upstream registry is trusted.
PACKUMENT_TTL = 300.0

#: Server-owned metadata for versions accepted by ``npm publish`` — dist-tags,
#: publish times, description — written by :mod:`services.npm_publish`.
#:
#: The tarballs in the directory remain the authority on *what versions exist*
#: (and are discoverable without this file); this one only records what cannot
#: be derived from a filename, above all a dist-tag that is not ``latest``.
#: ``npm publish --tag next`` therefore keeps resolving across a restart, and a
#: missing or malformed copy degrades to ``latest = highest version on disk``.
PUBLISH_INDEX_FILENAME = "publish.json"

#: ``Accept`` value npm sends for the install (abbreviated) document.
ABBREVIATED_ACCEPT = "application/vnd.npm.install-v1+json"
#: ``Accept`` value for the full document.
FULL_ACCEPT = "application/json"

LATEST_TAG = "latest"

SEARCH_DEFAULT_SIZE = 20
SEARCH_MAX_SIZE = 250

#: Hard ceiling on an upstream packument, so a mislabelled response cannot OOM.
PACKUMENT_MAX_BYTES = 128 * 1024 * 1024

#: A callable that builds the absolute tarball URL for ``(package, filename)``.
TarballUrl = Callable[[str, str], str]
#: A callable that builds the absolute packument URL for ``package``.
PackageUrl = Callable[[str], str]

#: Version keys the abbreviated install document keeps.  The four core
#: dependency maps plus ``dist``/``bin`` are mandatory; the rest is what npm
#: actually reads while resolving a tree.
_ABBREVIATED_EXTRA_KEYS = (
    "devDependencies",
    "bundleDependencies",
    "bundledDependencies",
    "engines",
    "directories",
    "os",
    "cpu",
    "deprecated",
    "hasInstallScript",
    "_hasShrinkwrap",
)

#: Required keys on every version manifest, in both document forms.
_REQUIRED_MANIFEST_KEYS = (
    "name", "version", "dependencies", "optionalDependencies",
    "peerDependencies", "bin", "dist",
)

_VERSION_SPLIT = re.compile(r"[.\-+]")


# ── Small document helpers ───────────────────────────────────────────

def _visible(path: Path) -> bool:
    """Skip dotfiles and the human-facing README that documents the directory."""
    if any(part.startswith(".") for part in path.parts):
        return False
    if path.name == hub._OVERLAY_FILENAME:
        return False
    return not path.name.lower().startswith(("readme", "license", "changelog"))


def _version_key(version: str) -> tuple:
    """A cheap, total ordering over version strings (semver-ish, not exact)."""
    parts: list[tuple[int, int, str]] = []
    for piece in _VERSION_SPLIT.split(version):
        if piece.isdigit():
            parts.append((0, int(piece), ""))
        elif piece:
            parts.append((1, 0, piece))
    return tuple(parts) or ((1, 0, version),)


def _tarball_basename(url: Any) -> str:
    if not isinstance(url, str) or not url:
        return ""
    path = urlparse(url).path
    return unquote(path.rsplit("/", 1)[-1]) if path else ""


def _parse_filename(filename: str) -> tuple[str, str]:
    """``left-pad-1.3.0.tgz`` → ``("left-pad", "1.3.0")``."""
    stem = filename[:-4] if filename.endswith(".tgz") else Path(filename).stem
    name, _, version = stem.rpartition("-")
    if not name:  # no dash — treat the whole stem as the name
        return stem, ""
    return name, version


def _load_publish_index(root: Path) -> dict[str, Any]:
    """``name -> metadata`` recorded by ``npm publish``; ``{}`` when absent.

    A malformed file is an operator error, not a broken registry: it degrades to
    the metadata that can be derived from the tarballs alone.
    """
    data = read_json(root / PUBLISH_INDEX_FILENAME, default={})
    packages = data.get("packages") if isinstance(data, dict) else None
    return packages if isinstance(packages, dict) else {}


def _modified_of(doc: Mapping[str, Any]) -> str:
    modified = doc.get("modified")
    if isinstance(modified, str) and modified:
        return modified
    stamp = doc.get("time")
    if isinstance(stamp, dict):
        for key in ("modified", "created"):
            value = stamp.get(key)
            if isinstance(value, str) and value:
                return value
    return utc_now_iso()


def _read_tarball_manifest(path: Path) -> dict[str, Any] | None:
    """Read ``package/package.json`` out of a ``.tgz``; None when unreadable.

    A malformed tarball must not break the whole packument, so every failure is
    swallowed and the caller falls back to parsing the filename.
    """
    try:
        with tarfile.open(path, "r:gz") as archive:
            member = None
            for candidate in archive:
                if not candidate.isfile() or not candidate.name.endswith("package.json"):
                    continue
                if candidate.name == "package/package.json":
                    member = candidate
                    break
                if member is None and candidate.name.count("/") == 1:
                    member = candidate
            if member is None:
                return None
            handle = archive.extractfile(member)
            if handle is None:
                return None
            data = json.loads(handle.read().decode("utf-8"))
    except (OSError, ValueError, tarfile.TarError) as exc:
        logger.warning("ignoring unreadable npm tarball %s: %s", path, exc)
        return None
    return data if isinstance(data, dict) else None


# ── Content hashes ───────────────────────────────────────────────────

_DIGEST_CACHE_MAX = 512
_digest_cache: dict[tuple[str, float, int], tuple[str, str]] = {}


def _digests(path: Path) -> tuple[str, str]:
    """``(sha1 hex, sha512 SRI)`` for a tarball, memoised on (path, mtime, size)."""
    stat = path.stat()
    key = (str(path), stat.st_mtime, stat.st_size)
    cached = _digest_cache.get(key)
    if cached is not None:
        return cached
    sha1 = hashlib.sha1()
    sha512 = hashlib.sha512()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            sha1.update(chunk)
            sha512.update(chunk)
    value = (
        sha1.hexdigest(),
        "sha512-" + base64.b64encode(sha512.digest()).decode("ascii"),
    )
    if len(_digest_cache) >= _DIGEST_CACHE_MAX:
        _digest_cache.clear()
    _digest_cache[key] = value
    return value


# ── Manifest shaping ─────────────────────────────────────────────────

def _core_manifest(name: str, version: str, source: Mapping[str, Any]) -> dict[str, Any]:
    """The mandatory shape of one version, whatever the source looked like."""
    return {
        "name": source.get("name") or name,
        "version": source.get("version") or version,
        "dependencies": source.get("dependencies") or {},
        "optionalDependencies": source.get("optionalDependencies") or {},
        "peerDependencies": source.get("peerDependencies") or {},
        "bin": source.get("bin") or {},
    }


def _abbreviated_manifest(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Strip a version manifest down to what the install document needs."""
    out = {key: manifest[key] for key in _REQUIRED_MANIFEST_KEYS if key in manifest}
    for key in _ABBREVIATED_EXTRA_KEYS:
        if key in manifest:
            out[key] = manifest[key]
    return out


def _abbreviate(packument: Mapping[str, Any]) -> dict[str, Any]:
    versions = {
        str(version): _abbreviated_manifest(manifest)
        for version, manifest in (packument.get("versions") or {}).items()
        if isinstance(manifest, dict)
    }
    return {
        "name": packument.get("name"),
        "dist-tags": packument.get("dist-tags") or {},
        "versions": versions,
        "modified": packument.get("modified") or utc_now_iso(),
    }


def _rewrite_manifest(
    manifest: Mapping[str, Any],
    name: str,
    version: str,
    *,
    abbreviated: bool,
    tarball_url: TarballUrl,
) -> dict[str, Any]:
    """Normalise one version and point its ``dist.tarball`` at this server."""
    dist_in = manifest.get("dist")
    dist_in = dist_in if isinstance(dist_in, dict) else {}
    basename = _tarball_basename(dist_in.get("tarball"))
    if not basename:
        basename = f"{name.split('/')[-1]}-{version}.tgz"
    dist = dict(dist_in)
    dist.update({
        "tarball": tarball_url(name, basename),
        "shasum": dist_in.get("shasum") or "",
        "integrity": dist_in.get("integrity") or "",
    })

    core = _core_manifest(name, version, manifest)
    core["dist"] = dist

    if abbreviated:
        out = dict(core)
        for key in _ABBREVIATED_EXTRA_KEYS:
            if key in manifest:
                out[key] = manifest[key]
        return out

    merged = dict(manifest)
    merged.update(core)
    return merged


def _rewrite_packument(
    document: Any,
    *,
    name: str,
    abbreviated: bool,
    tarball_url: TarballUrl,
) -> dict[str, Any] | None:
    """Normalise an upstream packument (full or abbreviated) for this server."""
    if not isinstance(document, dict):
        return None
    real_name = str(document.get("name") or name)
    versions_in = document.get("versions")
    versions_in = versions_in if isinstance(versions_in, dict) else {}
    versions: dict[str, Any] = {}
    for version, manifest in versions_in.items():
        if isinstance(manifest, dict):
            versions[str(version)] = _rewrite_manifest(
                manifest, real_name, str(version),
                abbreviated=abbreviated, tarball_url=tarball_url,
            )
    if not versions:
        return None

    dist_tags = document.get("dist-tags")
    dist_tags = dict(dist_tags) if isinstance(dist_tags, dict) and dist_tags else {}
    if not dist_tags:
        dist_tags = {LATEST_TAG: max(versions, key=_version_key)}

    out: dict[str, Any] = {
        "name": real_name,
        "dist-tags": dist_tags,
        "versions": versions,
        "modified": _modified_of(document),
    }
    if not abbreviated:
        for key in (
            "description", "keywords", "license", "homepage", "repository",
            "author", "bugs", "maintainers", "readme", "time", "users",
        ):
            if key in document:
                out[key] = document[key]
    return out


def _merge_packuments(
    upstream: Mapping[str, Any], local: Mapping[str, Any],
) -> dict[str, Any]:
    """Overlay the local mirror's versions on an upstream packument.

    The upstream document supplies the complete ``versions`` map npm resolves
    dependency ranges against; a local ``*.tgz`` replaces the manifest of the
    exact version it carries (so ``dist.tarball`` points at this server) and is
    added when upstream does not know the version at all (a private package).
    Upstream owns the dist-tags — a mirror syncs a *subset* of upstream, so its
    ``latest`` is the true one — and a local-only version stays reachable by its
    exact version or a local tag.  A metadata-only local entry (``catalog.json``
    with no tarball) never clobbers an upstream version, because its
    ``dist.tarball`` is empty.
    """
    versions: dict[str, Any] = dict(upstream.get("versions") or {})
    for version, manifest in (local.get("versions") or {}).items():
        dist = manifest.get("dist") if isinstance(manifest, dict) else None
        tarball = dist.get("tarball") if isinstance(dist, dict) else None
        # A local tarball wins; a bare catalog listing only fills a gap.
        if version not in versions or tarball:
            versions[version] = manifest

    dist_tags: dict[str, Any] = {
        str(tag): version
        for tag, version in (upstream.get("dist-tags") or {}).items()
        if version in versions
    }
    for tag, version in (local.get("dist-tags") or {}).items():
        if tag not in dist_tags and version in versions:
            dist_tags[str(tag)] = version
    if "latest" not in dist_tags and versions:
        dist_tags["latest"] = max(versions, key=_version_key)

    merged: dict[str, Any] = dict(local)
    merged.update(upstream)
    merged["name"] = upstream.get("name") or local.get("name")
    merged["versions"] = versions
    merged["dist-tags"] = dist_tags

    # Keep a local-only version's timestamp even though upstream owns `time`.
    times = dict(local.get("time") or {})
    times.update(upstream.get("time") or {})
    if times:
        merged["time"] = times
    return merged


def clamp_search_size(value: Any) -> int:
    """Clamp npm's ``size`` to 1..250, defaulting to 20 when unusable."""
    try:
        size = int(value)
    except (TypeError, ValueError):
        return SEARCH_DEFAULT_SIZE
    return max(1, min(SEARCH_MAX_SIZE, size))


def _local_score(name: str, needle: str) -> float:
    lowered = name.lower()
    if lowered == needle:
        return 1.0
    if lowered.startswith(needle):
        return 0.8
    if needle in lowered:
        return 0.5
    return 0.3


# ── The registry ─────────────────────────────────────────────────────

@dataclass
class NpmRegistry:
    """One configured npm view of the world: local dir + optional upstream."""

    root: str | Path
    upstream_url: str = ""
    proxy_enabled: bool = False
    token: str = ""
    timeout: float = 30.0
    cache_dir: str | Path = "data/cache/npm"
    cache_max_bytes: int = 512 * 1024 * 1024

    _cache: DiskCache = field(init=False, repr=False)
    _client: Upstream | None = field(default=None, init=False, repr=False)
    _index_key: Any = field(default=None, init=False, repr=False)
    _index: dict[str, dict[str, Any]] | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self.root = Path(self.root)
        self._cache = DiskCache(self.cache_dir, max_bytes=self.cache_max_bytes)

    # -- plumbing -----------------------------------------------------

    @property
    def cache(self) -> DiskCache:
        return self._cache

    @property
    def proxying(self) -> bool:
        return bool(self.proxy_enabled and self.upstream_url)

    def client(self) -> Upstream | None:
        """The pooled upstream client, or None when the proxy is disabled."""
        if not self.proxying:
            return None
        if self._client is None:
            self._client = Upstream(
                self.upstream_url,
                timeout=self.timeout,
                headers={"User-Agent": "openfish-npm-proxy/1.0"},
            )
        return self._client

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    # -- local index --------------------------------------------------

    def _signature(self) -> tuple:
        """A cheap fingerprint of the local tree, from *directory* metadata.

        Adding, removing or renaming a tarball updates its directory's mtime, so
        stat-ing every directory is enough to notice a changed mirror.  Stat-ing
        every ``.tgz`` instead is what made this expensive: on a large mirror on
        slow storage the walk cost seconds, and because ``local_index`` runs on
        every packument request that latency was paid once per dependency in an
        install.  ``catalog.json`` is edited in place (which leaves the directory
        mtime alone), so that one file is still stat-ed explicitly.
        """
        if not self.root.is_dir():
            return ()
        items: list[tuple] = []
        pending = [self.root]
        while pending:
            directory = pending.pop()
            try:
                stat = directory.stat()
            except OSError:
                continue
            items.append((str(directory), stat.st_mtime, stat.st_size))
            try:
                with os.scandir(directory) as entries:
                    for entry in entries:
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                pending.append(Path(entry.path))
                        except OSError:
                            continue
            except OSError:
                continue
        for bookmark in (hub._OVERLAY_FILENAME, PUBLISH_INDEX_FILENAME):
            path = self.root / bookmark
            try:
                stat = path.stat()
            except OSError:
                continue
            items.append((str(path), stat.st_mtime, stat.st_size))
        return tuple(sorted(items))

    def local_index(self) -> dict[str, dict[str, Any]]:
        """Every locally-known package, keyed by name, cached on the tree's mtime.

        A packument request is normally followed by a burst of tarball requests,
        so reading and hashing every ``.tgz`` once per request would be the
        dominant cost; the fingerprint makes it once per filesystem change.
        """
        signature = self._signature()
        if self._index is not None and signature == self._index_key:
            return self._index

        index: dict[str, dict[str, Any]] = {}
        if self.root.is_dir():
            for path in sorted(self.root.rglob("*.tgz")):
                if not _visible(path):
                    continue
                manifest = _read_tarball_manifest(path)
                if manifest is not None:
                    name = str(manifest.get("name") or "").strip()
                    version = str(manifest.get("version") or "").strip()
                else:
                    name = version = ""
                if not name or not version:
                    fallback_name, fallback_version = _parse_filename(path.name)
                    name = name or fallback_name
                    version = version or fallback_version
                if not name or not version:
                    continue
                record = index.setdefault(name, {
                    "versions": {}, "filenames": {}, "paths": {}, "times": {},
                    "description": None, "tags": [], "modified": 0.0,
                })
                record["filenames"][version] = path.name
                record["paths"][version] = path
                try:
                    mtime = path.stat().st_mtime
                except OSError:
                    mtime = time.time()
                record["times"][version] = mtime
                record["modified"] = max(record["modified"], mtime)
                if manifest is not None:
                    source = dict(manifest)
                    source.update(_core_manifest(name, version, manifest))
                    source["dist"] = {}
                    record["versions"][version] = source
                    if source.get("description"):
                        record["description"] = source["description"]
                    if source.get("keywords"):
                        record["tags"] = list(source["keywords"])
                else:  # a valid tarball with no readable package.json
                    record["versions"][version] = {
                        **_core_manifest(name, version, {}),
                        "dist": {},
                    }

        # catalog.json entries without a tarball on disk are still listed.
        catalog = hub.scan_npm(str(self.root), url_prefix="")
        for entry in catalog.get("packages", []):
            name = entry.get("name")
            if not name or name in index:
                continue
            # `scan_npm` also lists every tarball by parsing its *filename*, and
            # a scoped tarball's filename has no scope: ``react-0.27.20.tgz``
            # really holds ``@floating-ui/react@0.27.20``. Those files are
            # already indexed above under their real name, so re-adding the
            # filename-derived name here would invent a bogus unscoped package
            # whose manifest has no ``dist.tarball``. A tarball listing always
            # carries a ``download_url``; only genuine catalog.json entries
            # (metadata only, null download_url) are added from this view.
            if entry.get("download_url") is not None:
                continue
            version = entry.get("version") or LATEST_TAG
            index[name] = {
                "versions": {version: {
                    **_core_manifest(name, version, {"description": entry.get("description")}),
                    "description": entry.get("description") or "",
                    "dist": {},
                }},
                "filenames": {},
                "times": {},
                "description": entry.get("description"),
                "tags": list(entry.get("tags") or []),
                "modified": time.time(),
            }

        for record in index.values():
            versions = list(record["versions"])
            record["latest"] = max(versions, key=_version_key) if versions else LATEST_TAG

        # Overlay what `npm publish` recorded.  Only the fields a filename
        # cannot express are taken: the dist-tags (a `--tag next` publish must
        # keep resolving), the publish times, and a description for search.
        for name, meta in _load_publish_index(self.root).items():
            record = index.get(name)
            if not isinstance(meta, dict) or record is None:
                continue
            tags = {
                str(tag): str(version)
                for tag, version in (meta.get("dist-tags") or {}).items()
                if isinstance(tag, str) and isinstance(version, str)
            }
            if tags:
                record["dist_tags"] = tags
            if meta.get("description") and not record.get("description"):
                record["description"] = meta["description"]
            for version, stamp in (meta.get("time") or {}).items():
                if version in record["versions"] and isinstance(stamp, (int, float)):
                    record["times"].setdefault(version, float(stamp))

        self._index = index
        self._index_key = signature
        return index

    def has_local(self, name: str) -> bool:
        return name in self.local_index()

    def _local_manifest(self, name: str, version: str, record: Mapping[str, Any],
                        tarball_url: TarballUrl) -> dict[str, Any]:
        manifest = dict(record["versions"][version])
        dist = dict(manifest.get("dist") or {})
        filename = (record.get("filenames") or {}).get(version)
        dist["tarball"] = tarball_url(name, filename) if filename else ""
        dist.setdefault("shasum", "")
        dist.setdefault("integrity", "")
        if not dist["shasum"] and filename:
            path = (record.get("paths") or {}).get(version) or self._path_for(filename)
            if path is not None and path.is_file():
                sha1, integrity = _digests(path)
                dist["shasum"] = sha1
                dist["integrity"] = integrity
        manifest["dist"] = dist
        return manifest

    def _path_for(self, filename: str) -> Path | None:
        if not filename or Path(filename).name != filename:
            return None
        candidate = self.root / filename
        if candidate.is_file() and _visible(candidate):
            return candidate
        if not self.root.is_dir():
            return None
        for path in self.root.rglob("*.tgz"):
            if path.name == filename and _visible(path):
                return path
        return None

    def local_packument(
        self, name: str, *, tarball_url: TarballUrl, abbreviated: bool = False,
    ) -> dict[str, Any] | None:
        record = self.local_index().get(name)
        if record is None:
            return None
        versions = {
            version: self._local_manifest(name, version, record, tarball_url)
            for version in record["versions"]
        }
        modified = record.get("modified") or time.time()
        # A tag recorded by `npm publish` wins for its own version — that is the
        # whole point of `npm publish --tag next`; `latest` falls back to the
        # highest version on disk when nobody ever tagged the package.
        dist_tags: dict[str, str] = {
            str(tag): str(version)
            for tag, version in (record.get("dist_tags") or {}).items()
            if version in versions
        }
        if LATEST_TAG not in dist_tags:
            dist_tags[LATEST_TAG] = record.get("latest") or LATEST_TAG
        document: dict[str, Any] = {
            "name": name,
            "dist-tags": dist_tags,
            "versions": versions,
            "modified": iso_from_timestamp(modified),
            "time": {version: iso_from_timestamp(ts) for version, ts in (record.get("times") or {}).items()},
        }
        document["time"]["modified"] = document["modified"]
        if record.get("description"):
            document["description"] = record["description"]
        if record.get("tags"):
            document["keywords"] = list(record["tags"])
        return _abbreviate(document) if abbreviated else document

    # -- upstream -----------------------------------------------------

    def _upstream_packument(
        self, name: str, abbreviated: bool,
    ) -> tuple[dict[str, Any] | None, str | None]:
        """``(document, error)``; a miss or an outage is not an exception."""
        client = self.client()
        if client is None:
            return None, "proxy-disabled"
        variant = "abbrev" if abbreviated else "full"
        key = f"packument:{name}:{variant}"
        cached = self.cache.open_bytes(key, max_age=PACKUMENT_TTL)
        if cached is not None:
            try:
                document = json.loads(cached)
            except ValueError:
                document = None
            if isinstance(document, dict):
                return document, None

        accept = ABBREVIATED_ACCEPT if abbreviated else FULL_ACCEPT
        headers = {"Accept": accept, **self._auth_headers()}
        try:
            response = client.get_bytes(name, headers=headers, max_bytes=PACKUMENT_MAX_BYTES)
        except UpstreamError as exc:
            logger.info("npm upstream packument %s failed: %s", name, exc)
            return None, str(exc)
        if response.status_code == 404:
            return None, "notfound"
        if response.status_code >= 400:
            return None, f"upstream-{response.status_code}"
        try:
            document = response.json()
        except ValueError:
            return None, "invalid-json"
        self.cache.put_bytes(key, response.content)
        return (document if isinstance(document, dict) else None), None

    # -- public protocol ----------------------------------------------

    def packument(
        self, name: str, *, tarball_url: TarballUrl, abbreviated: bool = False,
    ) -> tuple[dict[str, Any] | None, str | None]:
        """Resolve a packument, merging the local mirror with the upstream registry.

        Returns ``(document, error)``.  ``document`` is None on a miss; ``error``
        is then ``"notfound"`` or a short upstream-failure reason the route maps
        to a non-500 status.

        When the proxy is enabled the upstream document is the base and local
        versions are overlaid on top of it (see :func:`_merge_packuments`), so a
        partially synced mirror cannot hide the versions npm needs to resolve a
        dependency range.  A local-only answer is returned when the upstream is
        disabled, does not know the package, or is unreachable — a proxy outage
        must never turn a mirrored package into a 404.
        """
        local = self.local_packument(name, tarball_url=tarball_url, abbreviated=abbreviated)

        if not self.proxying:
            if local is not None:
                return local, None
            return None, "proxy-disabled"

        document, error = self._upstream_packument(name, abbreviated)
        if document is None:
            if local is not None:
                return local, None
            return None, error

        rewritten = _rewrite_packument(
            document, name=name, abbreviated=abbreviated, tarball_url=tarball_url,
        )
        if rewritten is None:
            if local is not None:
                return local, None
            return None, "notfound"
        if local is None:
            return rewritten, None
        return _merge_packuments(rewritten, local), None

    def version_manifest(
        self, name: str, version: str, *, tarball_url: TarballUrl,
    ) -> dict[str, Any] | None:
        document, _error = self.packument(name, tarball_url=tarball_url)
        if document is None:
            return None
        resolved = (document.get("dist-tags") or {}).get(version, version)
        versions = document.get("versions") or {}
        manifest = versions.get(resolved) or versions.get(version)
        if not isinstance(manifest, dict):
            return None
        out = dict(manifest)
        out["name"] = manifest.get("name") or document.get("name") or name
        return out

    # -- tarballs -----------------------------------------------------

    def local_tarball(self, package: str, filename: str) -> Path | None:
        """The local file for *package*'s tarball, or None when the mirror lacks it.

        The basename alone is not a safe key: npm drops the scope from a scoped
        tarball's filename, so ``react-19.3.0.tgz`` can belong to
        ``@types/react`` while the real ``react@19.3.0`` has the same basename.
        Serving the wrong bytes would fail npm's integrity check (EINTEGRITY),
        so the *package's own* index record is the authority; a filename it does
        not own is left for the upstream to answer.
        """
        record = self.local_index().get(package)
        if record is None:
            return None
        for version, name in (record.get("filenames") or {}).items():
            if name != filename:
                continue
            path = (record.get("paths") or {}).get(version)
            if path is not None and path.is_file():
                return path
            return self._path_for(name)
        return None

    def upstream_tarball(self, package: str, filename: str) -> tuple[Path | None, str | None]:
        """Fetch (once) and cache a tarball; ``(path, error)`` like packuments."""
        client = self.client()
        if client is None:
            return None, "proxy-disabled"
        key = f"tarball:{package}:{filename}"
        cached = self.cache.get(key, max_age=None)
        if cached is not None:
            return cached, None
        try:
            response = client.request("GET", f"{package}/-/{filename}", stream=True)
        except UpstreamError as exc:
            logger.info("npm upstream tarball %s/%s failed: %s", package, filename, exc)
            return None, str(exc)
        if response.status_code == 404:
            response.close()
            return None, "notfound"
        if response.status_code >= 400:
            response.close()
            return None, f"upstream-{response.status_code}"
        try:
            path = self.cache.put_response(key, response)
        except UpstreamError as exc:  # pragma: no cover - defensive
            return None, str(exc)
        return path, None

    # -- search -------------------------------------------------------

    def _local_objects(
        self, text: str, *, package_url: PackageUrl, publisher: str,
    ) -> list[dict[str, Any]]:
        needle = text.lower()
        objects: list[dict[str, Any]] = []
        for name, record in sorted(self.local_index().items()):
            haystack = " ".join([
                name,
                record.get("description") or "",
                " ".join(record.get("tags") or []),
            ]).lower()
            if needle not in haystack:
                continue
            score = _local_score(name, needle)
            version = record.get("latest") or LATEST_TAG
            objects.append({
                "package": {
                    "name": name,
                    "version": version,
                    "description": record.get("description") or "",
                    "date": iso_from_timestamp(record.get("modified") or time.time()),
                    "links": {"npm": package_url(name)},
                    "publisher": {"username": publisher},
                    "maintainers": [{"username": publisher, "email": ""}],
                    "keywords": list(record.get("tags") or []),
                },
                "score": {
                    "final": score,
                    "detail": {
                        "quality": score, "popularity": score, "maintenance": score,
                    },
                },
                "searchScore": score,
            })
        return objects

    def _upstream_objects(self, text: str, size: int) -> list[dict[str, Any]]:
        client = self.client()
        if client is None:
            return []
        try:
            document = client.get_json(
                "-/v1/search",
                params={"text": text, "size": size},
                headers={"Accept": FULL_ACCEPT, **self._auth_headers()},
            )
        except UpstreamError as exc:
            logger.info("npm upstream search failed, degrading to local results: %s", exc)
            return []
        raw = document.get("objects") if isinstance(document, dict) else None
        if not isinstance(raw, list):
            return []
        objects: list[dict[str, Any]] = []
        for item in raw:
            package = item.get("package") if isinstance(item, dict) else None
            if isinstance(package, dict) and package.get("name"):
                objects.append(item)
        return objects

    def search(
        self, text: str, *, size: int = SEARCH_DEFAULT_SIZE, from_: int = 0,
        package_url: PackageUrl, publisher: str = "openfish",
    ) -> dict[str, Any]:
        """npm's modern ``GET /-/v1/search`` document, local results first."""
        size = clamp_search_size(size)
        from_ = max(0, int(from_ or 0))
        text = (text or "").strip()
        if not text:
            return {"objects": [], "total": 0, "time": utc_now_iso()}

        merged: list[dict[str, Any]] = []
        seen: set[str] = set()
        local = self._local_objects(
            text, package_url=package_url, publisher=publisher,
        )
        upstream = self._upstream_objects(text, size) if self.proxying else []
        for item in local + upstream:
            name = item["package"]["name"]
            if name in seen:
                continue
            seen.add(name)
            merged.append(item)

        return {
            "objects": merged[from_:from_ + size],
            "total": len(merged),
            "time": utc_now_iso(),
        }


__all__ = [
    "ABBREVIATED_ACCEPT",
    "FULL_ACCEPT",
    "LATEST_TAG",
    "PACKUMENT_TTL",
    "PUBLISH_INDEX_FILENAME",
    "SEARCH_DEFAULT_SIZE",
    "SEARCH_MAX_SIZE",
    "NpmRegistry",
    "clamp_search_size",
]
