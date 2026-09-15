"""Generic watchdog-backed in-memory index base class.

Subclasses implement ``_full_scan()``, ``_add_or_update()``, ``_remove()``.
"""

from __future__ import annotations

import hashlib
import logging
import threading
from abc import ABC, abstractmethod
from pathlib import Path

logger = logging.getLogger("cpypiserver.index")

_DIGEST_BLOCKSIZE = 8 << 20  # 8 MB — tuned for multi-GB packages on SSD/NVMe
_DIGEST_HEXLEN = 64          # SHA256 hex digest length

# ── Two-level digest cache ──────────────────────────────────────────
# L1: in-memory dict (sub-ms reads), keyed by resolved absolute path.
# L2: <package>.sha256 sidecar files (persist across restarts, zero-lock).
_digest_cache: dict[str, str] = {}
_digest_cache_lock = threading.Lock()


def _sidecar_path(file_path: str | Path) -> Path:
    return Path(file_path).with_suffix(Path(file_path).suffix + ".sha256")


def _read_sidecar(sidecar: Path) -> str | None:
    """Best-effort read from a .sha256 sidecar file.  Returns None on any failure."""
    try:
        text = sidecar.read_text().strip()
        if len(text) == _DIGEST_HEXLEN and all(c in "0123456789abcdef" for c in text):
            return text
    except (FileNotFoundError, UnicodeError, OSError):
        pass
    return None


def _write_sidecar(sidecar: Path, digest: str) -> None:
    """Best-effort write to a .sha256 sidecar file."""
    try:
        sidecar.write_text(digest)
    except OSError:
        pass  # disk full / permission — non-fatal; correctness unaffected


def compute_sha256(file_path: str | Path) -> str:
    """Chunked SHA256 with L1 memory cache + L2 sidecar file.

    On cache miss reads ``<filename>.sha256`` sidecar; recomputes and
    writes the sidecar if missing or corrupt.  Self-healing.
    """
    key = str(Path(file_path).resolve())

    # ── L1: memory cache ───────────────────────────────────────────
    with _digest_cache_lock:
        cached = _digest_cache.get(key)
        if cached is not None:
            return cached

    # ── L2: sidecar file ───────────────────────────────────────────
    sidecar = _sidecar_path(file_path)
    digest = _read_sidecar(sidecar)
    if digest is not None:
        with _digest_cache_lock:
            _digest_cache[key] = digest
        return digest

    # ── Compute (slow path — only on first access or after corruption)
    digester = hashlib.sha256()
    with open(file_path, "rb") as f:
        for block in iter(lambda: f.read(_DIGEST_BLOCKSIZE), b""):
            digester.update(block)
    digest = digester.hexdigest()

    # ── Store in both levels ───────────────────────────────────────
    with _digest_cache_lock:
        _digest_cache[key] = digest
    _write_sidecar(sidecar, digest)
    return digest


def invalidate_digest_cache(file_path: str | Path) -> None:
    """Purge both memory cache and sidecar file for *file_path*."""
    key = str(Path(file_path).resolve())
    with _digest_cache_lock:
        _digest_cache.pop(key, None)
    try:
        _sidecar_path(file_path).unlink(missing_ok=True)
    except OSError:
        pass


def store_digest(file_path: str | Path, digest: str) -> None:
    """Pre-populate both memory cache and sidecar file (e.g. from streaming upload)."""
    key = str(Path(file_path).resolve())
    with _digest_cache_lock:
        _digest_cache[key] = digest
    _write_sidecar(_sidecar_path(file_path), digest)


class WatchdogIndex(ABC):
    """Thread-safe in-memory index maintained by watchdog.

    Subclasses:
      - ``_full_scan()``          — walk ``self._dir``, rebuild data
      - ``_add_or_update(path)``  — index a file
      - ``_remove(path)``         — un-index a file
    """

    def __init__(self, watch_dir: str, *, recursive: bool = False) -> None:
        self._dir = watch_dir
        self._recursive = recursive
        self._lock = threading.Lock()
        self._observer: object | None = None

    @property
    def root(self) -> str:
        """The directory this index watches; safe to read from a route."""
        return self._dir

    # ── Subclass contract ───────────────────────────────────────────

    @abstractmethod
    def _full_scan(self) -> None: ...

    @abstractmethod
    def _add_or_update(self, abs_path: str) -> None: ...

    @abstractmethod
    def _remove(self, abs_path: str) -> None: ...

    # ── Lifecycle ───────────────────────────────────────────────────

    def start(self) -> None:
        self._full_scan()
        from config import settings
        if settings.storage.watch_packages and Path(self._dir).is_dir():
            self._start_observer()

    def stop(self) -> None:
        obs = self._observer
        if obs is not None:
            obs.stop()          # type: ignore[union-attr]
            obs.join(timeout=3) # type: ignore[union-attr]
            logger.info("Watchdog stopped for %s", self._dir)

    # ── Observer (shared across subclasses) ────────────────────────

    def _start_observer(self) -> None:
        try:
            from watchdog.observers import Observer
            from watchdog.events import FileSystemEventHandler
        except ImportError:
            logger.warning("watchdog missing — '%s' will NOT auto-update", self._dir)
            return

        idx = self

        class _Handler(FileSystemEventHandler):
            def on_created(self, event):
                if not event.is_directory:
                    idx._add_or_update(event.src_path)

            def on_modified(self, event):
                if not event.is_directory:
                    idx._add_or_update(event.src_path)

            def on_deleted(self, event):
                if not event.is_directory:
                    idx._remove(event.src_path)

            def on_moved(self, event):
                if not event.is_directory:
                    idx._remove(event.src_path)
                    idx._add_or_update(event.dest_path)

        observer = Observer()
        observer.schedule(_Handler(), self._dir, recursive=self._recursive)
        observer.daemon = True
        observer.start()
        self._observer = observer
        logger.info("Watchdog started on '%s' (recursive=%s)", self._dir, self._recursive)
