"""NodeBuildIndex — watchdog-maintained index of a nodejs.org/dist mirror.

The Node counterpart of :mod:`index.python_build`.  Where that one mirrors
``python-build-standalone`` for ``uv python install``, this one mirrors the
``nodejs.org/dist`` tree that ``nvm``, ``fnm`` and ``node-gyp`` consume through
``NODEJS_ORG_MIRROR``::

    node-builds/
      index.json                 optional — authentic nodejs.org metadata
      v20.11.0/
        node-v20.11.0-linux-x64.tar.xz
        node-v20.11.0-darwin-arm64.tar.gz
        SHASUMS256.txt           optional — regenerated from the files when absent
      v22.14.0/
        ...

The release directory is the source of truth, exactly as in the Python build
index: dropping an archive into ``node-builds/vX.Y.Z/`` is the whole publish
step, and the next request sees it.  ``index.json`` is read as an *overlay*
only — it may add the ``lts``/``date``/``npm`` fields a filename cannot carry,
but it can never make a release appear that is not on disk, so `fnm`/`nvm`
never advertise a version this server would then 404.
"""

from __future__ import annotations

import os
import re
from dataclasses import asdict
from pathlib import Path
from typing import Any

from index.base import WatchdogIndex, compute_sha256, invalidate_digest_cache
from schemas import NodeBuildFile


#: ``node-v20.11.0-linux-x64.tar.xz`` and friends.  The version's optional
#: prerelease suffix is restricted to the names Node actually publishes
#: (``-rc.1``, ``-nightly…``) *and* to characters that cannot contain a dash:
#: an unrestricted tail backtracks into ``-linux-x64`` and leaves the platform
#: wrong (``v21.0.0-rc.1-linux-x64`` parses as version ``21.0.0-rc.1-linux``,
#: platform ``x64``).
#:
#: The platform/arch group is optional because nodejs.org also publishes two
#: platform-less artifacts per release: the source tarball
#: (``node-v20.11.0.tar.xz``) and the universal macOS installer
#: (``node-v20.11.0.pkg``).  ``arch`` accepts a dash-separated *tail* because
#: Alpine's build is ``linux-x64-musl`` — a genuine target, not a variant to
#: drop.
_NODE_PATTERN = re.compile(
    r"^node-v"
    r"(?P<version>\d+\.\d+\.\d+(?:-(?:rc|beta|alpha|nightly)[0-9A-Za-z.]*)?)"
    r"(?:-(?P<platform>[a-z0-9]+)"
    r"(?:-(?P<arch>[a-z0-9_]+(?:-[a-z0-9_]+)*))?)?"
    r"\.(?P<ext>tar\.gz|tar\.xz|tar\.bz2|zip|7z|msi|pkg|tar)$"
)

#: A release directory: ``v20.11.0``, ``20.11.0`` or a prerelease of either.
_RELEASE_DIR_PATTERN = re.compile(r"^v?\d+\.\d+\.\d+(?:-[0-9A-Za-z.\-]+)?$")

#: Suffix used by node's own ``index.json`` for a platform/arch/format triple.
_EXT_TOKEN = {
    "tar.gz": "tar",
    "tar.xz": "tar",
    "tar.bz2": "tar",
    "zip": "zip",
    "7z": "7z",
    "msi": "msi",
    "pkg": "pkg",
}

_SEMVER_PATTERN = re.compile(
    r"^v?(?P<major>\d+)\.(?P<minor>\d+)(?:\.(?P<patch>\d+))?"
    r"(?:-(?P<pre>[0-9A-Za-z.\-]+))?$"
)


def _version_key(tag: str) -> tuple:
    """Sort key that orders ``v20.9.0`` before ``v20.10.0`` and finals over rc."""
    match = _SEMVER_PATTERN.match(tag)
    if match is None:
        return (0, 0, 0, 0, tag)
    pre = match.group("pre") or ""
    return (
        int(match.group("major")),
        int(match.group("minor")),
        int(match.group("patch") or 0),
        1 if not pre else 0,   # a final release outranks its own prereleases
        pre,
    )


def _parse_node_filename(filename: str) -> NodeBuildFile | None:
    match = _NODE_PATTERN.match(filename)
    if match is None:
        return None
    extension = match.group("ext")
    # No platform segment: the source tarball or the universal macOS installer.
    # Name the pseudo-platform after what it is, so the UI and `file_token`
    # never have to special-case an empty string.
    platform = match.group("platform") or (
        "src" if extension in ("tar.gz", "tar.xz", "tar.bz2", "tar") else extension
    )
    return NodeBuildFile(
        filename=filename,
        path="",
        release_tag="",
        version=match.group("version"),
        platform=platform,
        arch=match.group("arch") or "",
        extension=extension,
    )


def _entry(full_path: Path, release_tag: str) -> NodeBuildFile | None:
    bf = _parse_node_filename(full_path.name)
    if bf is None:
        return None
    bf.release_tag = release_tag
    bf.path = str(full_path.absolute())
    bf.size = full_path.stat().st_size
    return bf


def file_token(bf: NodeBuildFile) -> str:
    """The token node's ``index.json`` uses for this artifact.

    ``linux-x64``, ``linux-x64-musl``, ``osx-arm64-tar``, ``win-x64-zip``,
    ``headers``, ``src`` — enough for a client filtering releases by platform
    to recognise one.
    """
    if bf.platform in ("headers", "src", "pkg"):
        return bf.platform
    platform = "osx" if bf.platform == "darwin" else bf.platform
    token = f"{platform}-{bf.arch}" if bf.arch else platform
    # Node only spells the archive format out on the platforms that ship more
    # than one; a Linux tarball is just `linux-x64`.
    if bf.platform in ("darwin", "win") and bf.extension in _EXT_TOKEN:
        token = f"{token}-{_EXT_TOKEN[bf.extension]}"
    return token


class NodeBuildIndex(WatchdogIndex):
    """Thread-safe in-memory index of a nodejs.org/dist-shaped mirror."""

    def __init__(self, builds_dir: str) -> None:
        super().__init__(builds_dir, recursive=True)
        self._by_release: dict[str, list[NodeBuildFile]] = {}
        self._meta: dict[str, dict[str, Any]] = {}

    # ── Public API ──────────────────────────────────────────────────

    def get_snapshot(self) -> dict[str, list[NodeBuildFile]]:
        with self._lock:
            return {k: list(v) for k, v in self._by_release.items()}

    def get_releases(self) -> list[str]:
        """Release tags, newest first."""
        with self._lock:
            return sorted(self._by_release.keys(), key=_version_key, reverse=True)

    def get_files_for_release(self, release_tag: str) -> list[NodeBuildFile] | None:
        with self._lock:
            files = self._by_release.get(release_tag)
            return list(files) if files else None

    def get_file(self, release_tag: str, filename: str) -> NodeBuildFile | None:
        with self._lock:
            for f in self._by_release.get(release_tag, []):
                if f.filename == filename:
                    return f
        return None

    def get_sha256(self, bf: NodeBuildFile) -> str:
        if bf.sha256_digest is not None:
            return bf.sha256_digest
        bf.sha256_digest = compute_sha256(bf.path)
        return bf.sha256_digest

    def resolve_alias(self, tag: str) -> str | None:
        """``latest`` / ``latest-v20.x`` → a concrete release tag, or None.

        ``nvm`` resolves ``nvm install node`` through ``latest/SHASUMS256.txt``
        and ``nvm install 20`` through ``latest-v20.x/…``; both are aliases this
        mirror has to answer.
        """
        alias = re.match(r"^latest(?:-v(?P<major>\d+)\.x)?$", tag)
        if alias is None:
            return None
        wanted_major = int(alias.group("major")) if alias.group("major") else None
        for release in self.get_releases():
            if wanted_major is None:
                return release
            match = _SEMVER_PATTERN.match(release)
            if match is not None and int(match.group("major")) == wanted_major:
                return release
        return None

    def stats(self) -> dict:
        with self._lock:
            files = sum(len(v) for v in self._by_release.values())
            total_size = sum(
                f.size for flist in self._by_release.values() for f in flist
            )
            platforms: set[str] = set()
            versions: set[str] = set()
            for flist in self._by_release.values():
                for f in flist:
                    platforms.add(f.platform)
                    versions.add(f.version)
            return {
                "releases": len(self._by_release),
                "files": files,
                "total_size": total_size,
                "platforms": sorted(platforms),
                "versions": sorted(versions, key=_version_key),
            }

    def shasums(self, release_tag: str) -> str | None:
        """``SHASUMS256.txt`` for one release, in node's two-space format.

        Returns ``None`` for an unknown release.  Every listed archive is hashed
        on demand (and cached through the shared digest cache), so the first
        call on a cold mirror is I/O-bound — the authentic file, when it was
        mirrored alongside the archives, is served without this cost.
        """
        files = self.get_files_for_release(release_tag)
        if files is None:
            return None
        lines = [
            f"{self.get_sha256(f)}  {f.filename}"
            for f in sorted(files, key=lambda f: f.filename)
        ]
        return "\n".join(lines) + "\n"

    def index_document(self) -> list[dict[str, Any]]:
        """The mirror as node's ``index.json``: one object per local release.

        Metadata the filename cannot express (``lts``, ``date``, ``npm``, …) is
        merged from an authentic ``index.json`` at the mirror root when one was
        synced; without it those fields are simply absent rather than invented.
        """
        snapshot = self.get_snapshot()
        with self._lock:
            meta = {k: dict(v) for k, v in self._meta.items()}
        document: list[dict[str, Any]] = []
        for release_tag in sorted(snapshot, key=_version_key, reverse=True):
            files = snapshot[release_tag]
            entry: dict[str, Any] = {"version": release_tag, "files": []}
            seen: list[str] = []
            for bf in sorted(files, key=lambda f: f.filename):
                token = file_token(bf)
                if token not in seen:
                    seen.append(token)
            entry["files"] = seen
            overlay = meta.get(release_tag)
            if overlay:
                for field in (
                    "date", "lts", "security", "npm", "v8", "uv",
                    "zlib", "openssl", "modules",
                ):
                    if field in overlay:
                        entry[field] = overlay[field]
            entry.setdefault("lts", False)
            entry.setdefault("security", False)
            document.append(entry)
        return document

    def index_tab(self) -> str:
        """The mirror as node's tab-separated ``index.tab``.

        ``nvm`` prefers this document and falls back to ``index.json``; keeping
        the same 11 columns means a client can parse it with the same awk
        program it uses against nodejs.org.
        """
        header = [
            "version", "date", "files", "npm", "v8", "uv", "zlib",
            "openssl", "modules", "lts", "security",
        ]
        rows = ["\t".join(header)]
        for entry in self.index_document():
            lts = entry.get("lts")
            rows.append("\t".join([
                str(entry.get("version", "")),
                str(entry.get("date", "")),
                ",".join(entry.get("files", [])),
                str(entry.get("npm", "")),
                str(entry.get("v8", "")),
                str(entry.get("uv", "")),
                str(entry.get("zlib", "")),
                str(entry.get("openssl", "")),
                str(entry.get("modules", "")),
                "-" if lts in (False, None) else str(lts),
                "-" if not entry.get("security") else "true",
            ]))
        return "\n".join(rows) + "\n"

    # ── Full scan ───────────────────────────────────────────────────

    def _full_scan(self) -> None:
        by_release: dict[str, list[NodeBuildFile]] = {}
        builds_path = Path(self._dir)
        if not builds_path.is_dir():
            with self._lock:
                self._by_release = by_release
                self._meta = {}
            return
        for release_dir in builds_path.iterdir():
            if not release_dir.is_dir() or not _RELEASE_DIR_PATTERN.match(release_dir.name):
                continue
            release_tag = release_dir.name
            entries: list[NodeBuildFile] = []
            for f in release_dir.iterdir():
                if not f.is_file() or not f.name.startswith("node-v"):
                    continue
                bf = _entry(f, release_tag)
                if bf is not None:
                    entries.append(bf)
            if entries:
                entries.sort(key=lambda bf: bf.filename)
                by_release[release_tag] = entries
        with self._lock:
            self._by_release = by_release
            self._meta = self._load_index_overlay(builds_path)

    @staticmethod
    def _load_index_overlay(base: Path) -> dict[str, dict[str, Any]]:
        """Read an authentic ``index.json`` as per-release metadata; never fail."""
        import json

        overlay_file = base / "index.json"
        if not overlay_file.is_file():
            return {}
        try:
            data = json.loads(overlay_file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return {}
        if not isinstance(data, list):
            return {}
        meta: dict[str, dict[str, Any]] = {}
        for item in data:
            if isinstance(item, dict) and item.get("version"):
                meta[str(item["version"])] = item
        return meta

    # ── Incremental ─────────────────────────────────────────────────

    def _add_or_update(self, abs_path: str) -> None:
        full = Path(abs_path)
        if not full.is_file() or not full.name.startswith("node-v"):
            return
        release_tag = full.parent.name
        if not _RELEASE_DIR_PATTERN.match(release_tag):
            return
        bf = _entry(full, release_tag)
        if bf is None:
            return
        invalidate_digest_cache(abs_path)
        with self._lock:
            files = self._by_release.setdefault(release_tag, [])
            files[:] = [f for f in files if f.filename != bf.filename]
            files.append(bf)
            files.sort(key=lambda f: f.filename)

    def _remove(self, abs_path: str) -> None:
        filename = os.path.basename(abs_path)
        release_tag = os.path.basename(os.path.dirname(abs_path))
        invalidate_digest_cache(abs_path)
        with self._lock:
            files = self._by_release.get(release_tag)
            if files is None:
                return
            files[:] = [f for f in files if f.filename != filename]
            if not files:
                del self._by_release[release_tag]

    # ── Serialisation helper shared with the routes ─────────────────

    @staticmethod
    def as_dict(bf: NodeBuildFile) -> dict[str, Any]:
        """Plain-dict view of one artifact, without the on-disk path."""
        data = asdict(bf)
        data.pop("path", None)
        return data


__all__ = ["NodeBuildIndex", "file_token"]
