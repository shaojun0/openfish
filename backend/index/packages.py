"""PackageIndex — watchdog-maintained in-memory PyPI package index."""

from __future__ import annotations

import os
import re
from pathlib import Path
from index.base import WatchdogIndex, compute_sha256, invalidate_digest_cache
from schemas import PackageFile


# ── Filename patterns ────────────────────────────────────────────────

WHEEL_PATTERN = re.compile(
    r"^(.+?)-([\w!<>.+]+?)(?:-[^-]+)?-(.+?)-(.+?)-(.+)\.whl$"
)
SDIST_PATTERN = re.compile(r"^(.+?)-([\d!<>.]+?)\.(tar\.gz|tar\.bz2|tar\.xz|zip)$")
PACKAGE_EXTENSIONS = {".whl", ".tar.gz", ".tar.bz2", ".tar.xz", ".zip", ".egg"}


def normalize_package_name(name: str) -> str:
    """PEP 503: lowercase, collapse ``[-_.]`` runs to ``-``."""
    return re.sub(r"[-_.]+", "-", name.lower())


def _parse_name(filename: str) -> str | None:
    m = WHEEL_PATTERN.match(filename)
    if m:
        return normalize_package_name(m.group(1))
    m = SDIST_PATTERN.match(filename)
    if m:
        return normalize_package_name(m.group(1))
    return None


def _extract_version(filename: str, norm_name: str) -> str:
    for pat in (WHEEL_PATTERN, SDIST_PATTERN):
        m = pat.match(filename)
        if m:
            return m.group(2)
    rest = filename[len(norm_name):].lstrip("-_. ")
    for ext in sorted(PACKAGE_EXTENSIONS, key=len, reverse=True):
        if rest.endswith(ext):
            return rest[:-len(ext)]
    return rest


def _build_package_file(full_path: Path, *, stat_size: bool = False) -> PackageFile | None:
    filename = full_path.name
    pkg_name = _parse_name(filename)
    if not pkg_name:
        return None
    version = _extract_version(filename, pkg_name)
    return PackageFile(
        filename=filename,
        path=str(full_path.absolute()),
        url=filename,
        package_name=pkg_name,
        version=version,
        packagetype="bdist_wheel" if filename.endswith(".whl") else "sdist",
        python_version="source" if not filename.endswith(".whl") else "py3",
        size=full_path.stat().st_size if stat_size else 0,
        sha256_digest=None,
    )


# ── PackageIndex ─────────────────────────────────────────────────────

class PackageIndex(WatchdogIndex):
    """Thread-safe in-memory PyPI package index."""

    def __init__(self, packages_dir: str) -> None:
        super().__init__(packages_dir, recursive=False)
        self._packages: dict[str, list[PackageFile]] = {}

    # ── Public API ──────────────────────────────────────────────────

    def get_sha256(self, pf: PackageFile) -> str:
        if pf.sha256_digest is not None:
            return pf.sha256_digest
        if pf.size == 0:
            try:
                pf.size = Path(pf.path).stat().st_size
            except OSError:
                pass
        digest = compute_sha256(pf.path)
        with self._lock:
            pf.sha256_digest = digest
        return digest

    def get_snapshot(self) -> dict[str, list[PackageFile]]:
        with self._lock:
            return {k: list(v) for k, v in self._packages.items()}

    def get_files(self, name: str) -> list[PackageFile] | None:
        norm = normalize_package_name(name)
        with self._lock:
            files = self._packages.get(norm)
            return list(files) if files else None

    # ── Full scan ───────────────────────────────────────────────────

    def _full_scan(self) -> None:
        pkgs: dict[str, list[PackageFile]] = {}
        pkg_path = Path(self._dir)
        if not pkg_path.is_dir():
            with self._lock:
                self._packages = pkgs
            return
        for entry in pkg_path.iterdir():
            if not any(entry.name.endswith(ext) for ext in PACKAGE_EXTENSIONS):
                continue
            pf = _build_package_file(entry)
            if pf is None:
                continue
            pkgs.setdefault(pf.package_name, []).append(pf)
        for files in pkgs.values():
            files.sort(key=lambda f: f.filename, reverse=True)
        with self._lock:
            self._packages = pkgs

    # ── Incremental ─────────────────────────────────────────────────

    def _add_or_update(self, abs_path: str) -> None:
        full = Path(abs_path)
        if not full.is_file():
            return
        if not any(full.name.endswith(ext) for ext in PACKAGE_EXTENSIONS):
            return
        pf = _build_package_file(full, stat_size=True)
        if pf is None:
            return
        invalidate_digest_cache(abs_path)
        with self._lock:
            files = self._packages.setdefault(pf.package_name, [])
            files[:] = [f for f in files if f.filename != pf.filename]
            files.append(pf)
            files.sort(key=lambda f: f.filename, reverse=True)

    def _remove(self, abs_path: str) -> None:
        filename = os.path.basename(abs_path)
        pkg_name = _parse_name(filename)
        if pkg_name is None:
            return
        invalidate_digest_cache(abs_path)
        with self._lock:
            files = self._packages.get(pkg_name)
            if files is None:
                return
            files[:] = [f for f in files if f.filename != filename]
            if not files:
                del self._packages[pkg_name]
