"""Generic watchdog-backed in-memory index base class.

Subclasses implement ``_full_scan()``, ``_add_or_update()``, ``_remove()``.

The digest helpers that used to live here now have exactly one home, in
:mod:`services.digest`; they are re-exported so the index modules keep a single
import site for everything they need.
"""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from pathlib import Path

from services.digest import compute_sha256, invalidate_digest_cache, store_digest


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

    # ── Observer (shared across subclasses) ────────────────────────

    def _start_observer(self) -> None:
        try:
            from watchdog.observers import Observer
            from watchdog.events import FileSystemEventHandler
        except ImportError:
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


__all__ = [
    "WatchdogIndex",
    "compute_sha256",
    "invalidate_digest_cache",
    "store_digest",
]
