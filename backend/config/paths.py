"""Filesystem anchors — the two roots this repository distinguishes.

The project tree has two levels that mean different things::

    <project>/                artifact-hub catalogs: tools/, npm/, node-builds/,
                              docker-images/, debian/, docs/ — operator data,
                              bind-mounted into the container at run time
    <project>/backend/        this application: code, Jinja templates, local
                              state (data/, packages/, certs/)

Path defaults are resolved from ``__file__`` rather than from the process
working directory.  That keeps them correct no matter where the server was
started from — which matters because the backend is now a subdirectory of the
repository rather than its root.

An explicitly configured value (``PACKAGES_DIR``, ``TOOLS_DIR``, …) always wins:
environment variables override these defaults, and docker-compose sets every
one of them to an absolute ``/app/...`` path.
"""

from pathlib import Path

#: ``<project>/backend`` — the application package root.
BACKEND_ROOT: Path = Path(__file__).resolve().parent.parent

#: ``<project>`` — one level up; holds the operator-managed artifact catalogs.
PROJECT_ROOT: Path = BACKEND_ROOT.parent


def backend_path(*parts: str) -> str:
    """Absolute path inside the backend tree (code, templates, local state)."""
    return str(BACKEND_ROOT.joinpath(*parts))


def project_path(*parts: str) -> str:
    """Absolute path inside the project tree (artifact-hub catalogs)."""
    return str(PROJECT_ROOT.joinpath(*parts))


__all__ = ["BACKEND_ROOT", "PROJECT_ROOT", "backend_path", "project_path"]
