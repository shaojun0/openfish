"""PythonBuildIndex — watchdog-maintained index of python-build-standalone releases."""

from __future__ import annotations

import os
import re
from pathlib import Path
from index.base import WatchdogIndex, compute_sha256, invalidate_digest_cache
from schemas import PythonBuildFile


_BUILD_PATTERN = re.compile(
    r"^(?P<flavor>cpython|pypy)"
    r"-(?P<version>\d+\.\d+\.\d+(?:[a-z]+\d+)?)"
    r"\+(?P<date>\d{8})"
    r"-(?P<triple>[^-]+-[^-]+-[^-]+-[^-]+)"
    r"-(?P<variant>.+)"
    r"\.(?P<ext>tar\.gz|tar\.bz2|tar\.xz|tar\.zst|zip)$"
)

_BUILD_EXTENSIONS = {".tar.gz", ".tar.bz2", ".tar.xz", ".tar.zst", ".zip"}


def _parse_build_filename(filename: str) -> PythonBuildFile | None:
    m = _BUILD_PATTERN.match(filename)
    if not m:
        return None
    return PythonBuildFile(
        filename=filename,
        path="",
        release_tag=m.group("date"),
        flavor=m.group("flavor"),
        version=m.group("version"),
        full_version=f"{m.group('flavor')}-{m.group('version')}+{m.group('date')}",
        target_triple=m.group("triple"),
        variant=m.group("variant"),
        extension=m.group("ext"),
    )


def _build_entry(full_path: Path, release_tag: str) -> PythonBuildFile | None:
    pf = _parse_build_filename(full_path.name)
    if pf is None:
        return None
    pf.path = str(full_path.absolute())
    pf.size = full_path.stat().st_size
    return pf


class PythonBuildIndex(WatchdogIndex):
    """Thread-safe in-memory index of python-build-standalone files."""

    def __init__(self, builds_dir: str) -> None:
        super().__init__(builds_dir, recursive=True)
        self._by_release: dict[str, list[PythonBuildFile]] = {}

    # ── Public API ──────────────────────────────────────────────────

    def get_snapshot(self) -> dict[str, list[PythonBuildFile]]:
        with self._lock:
            return {k: list(v) for k, v in self._by_release.items()}

    def get_releases(self) -> list[str]:
        with self._lock:
            return sorted(self._by_release.keys(), reverse=True)

    def get_files_for_release(self, release_tag: str) -> list[PythonBuildFile] | None:
        with self._lock:
            files = self._by_release.get(release_tag)
            return list(files) if files else None

    def get_file(self, release_tag: str, filename: str) -> PythonBuildFile | None:
        with self._lock:
            for f in self._by_release.get(release_tag, []):
                if f.filename == filename:
                    return f
        return None

    def get_sha256(self, bf: PythonBuildFile) -> str:
        if bf.sha256_digest is not None:
            return bf.sha256_digest
        bf.sha256_digest = compute_sha256(bf.path)
        return bf.sha256_digest

    def stats(self) -> dict:
        with self._lock:
            files = sum(len(v) for v in self._by_release.values())
            total_size = sum(
                f.size for flist in self._by_release.values() for f in flist
            )
            flavors: set[str] = set()
            versions: set[str] = set()
            for flist in self._by_release.values():
                for f in flist:
                    flavors.add(f.flavor)
                    versions.add(f.version)
            return {
                "releases": len(self._by_release),
                "files": files,
                "total_size": total_size,
                "flavors": sorted(flavors),
                "versions": sorted(versions),
            }

    # ── Full scan ───────────────────────────────────────────────────

    def _full_scan(self) -> None:
        by_release: dict[str, list[PythonBuildFile]] = {}
        builds_path = Path(self._dir)
        if not builds_path.is_dir():
            with self._lock:
                self._by_release = by_release
            return
        for release_dir in builds_path.iterdir():
            if not release_dir.is_dir() or not re.match(r"^\d{8}$", release_dir.name):
                continue
            release_tag = release_dir.name
            entries: list[PythonBuildFile] = []
            for f in release_dir.iterdir():
                if not f.is_file():
                    continue
                if not any(f.name.endswith(ext) for ext in _BUILD_EXTENSIONS):
                    continue
                pf = _build_entry(f, release_tag)
                if pf is None:
                    continue
                entries.append(pf)
            if entries:
                entries.sort(key=lambda pf: pf.filename)
                by_release[release_tag] = entries
        with self._lock:
            self._by_release = by_release

    # ── Incremental ─────────────────────────────────────────────────

    def _add_or_update(self, abs_path: str) -> None:
        full = Path(abs_path)
        if not full.is_file():
            return
        if not any(full.name.endswith(ext) for ext in _BUILD_EXTENSIONS):
            return
        release_tag = full.parent.name
        if not re.match(r"^\d{8}$", release_tag):
            return
        pf = _build_entry(full, release_tag)
        if pf is None:
            return
        invalidate_digest_cache(abs_path)
        with self._lock:
            files = self._by_release.setdefault(release_tag, [])
            files[:] = [f for f in files if f.filename != pf.filename]
            files.append(pf)
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
