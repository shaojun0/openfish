"""Index package — watchdog-maintained in-memory indexes.

:func:`register_all` is the package's only entry point: it builds the three
indexes and hangs them off ``app.extensions``.  The index classes themselves are
imported from the module that defines them (``index.packages``, …).
"""

from __future__ import annotations

import atexit

from index.node_build import NodeBuildIndex
from index.packages import PackageIndex
from index.python_build import PythonBuildIndex


def register_all(app) -> None:
    """Create and start all watchdog indexes, attach to app.extensions."""
    from config import settings

    for key, index in (
        ("pypi_index", PackageIndex(settings.storage.packages_dir)),
        ("python_build_index", PythonBuildIndex(settings.storage.python_builds_dir)),
        ("node_build_index", NodeBuildIndex(settings.storage.node_builds_dir)),
    ):
        index.start()
        app.extensions[key] = index
        atexit.register(index.stop)


__all__ = ["register_all"]
