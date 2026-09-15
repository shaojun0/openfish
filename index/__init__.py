"""Index package — watchdog-maintained in-memory indexes."""

from index.base import WatchdogIndex, compute_sha256
from index.packages import PackageIndex, normalize_package_name, PackageFile
from index.python_build import PythonBuildIndex
from index.node_build import NodeBuildIndex


def register_all(app):
    """Create and start all watchdog indexes, attach to app.extensions."""
    from config import settings
    import atexit

    pkg_index = PackageIndex(settings.storage.packages_dir)
    pkg_index.start()
    app.extensions["pypi_index"] = pkg_index
    atexit.register(pkg_index.stop)

    build_index = PythonBuildIndex(settings.storage.python_builds_dir)
    build_index.start()
    app.extensions["python_build_index"] = build_index
    atexit.register(build_index.stop)

    node_index = NodeBuildIndex(settings.storage.node_builds_dir)
    node_index.start()
    app.extensions["node_build_index"] = node_index
    atexit.register(node_index.stop)


__all__ = [
    "WatchdogIndex", "compute_sha256",
    "PackageIndex", "normalize_package_name", "PackageFile",
    "PythonBuildIndex", "NodeBuildIndex", "register_all",
]
