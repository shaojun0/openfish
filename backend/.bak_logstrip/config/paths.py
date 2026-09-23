"""Filesystem anchors — the roots this repository distinguishes.

The tree has three levels that mean different things::

    <project>/                deploy bundle: docker/ plus the two build units
    <project>/docker/         artifact-hub catalogs: tools/, npm/, node-builds/,
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

from __future__ import annotations

from pathlib import Path

#: ``<project>/backend`` — the application package root.
BACKEND_ROOT: Path = Path(__file__).resolve().parent.parent

#: ``<project>`` — one level up; holds the Compose bundle and both build units.
PROJECT_ROOT: Path = BACKEND_ROOT.parent

#: ``<project>/docker`` — the Compose bundle; the artifact catalogs live here so
#: the repository root stays a small, readable index of build units.
CATALOGS_ROOT: Path = PROJECT_ROOT / "docker"


def backend_path(*parts: str) -> str:
    """Absolute path inside the backend tree (code, templates, local state)."""
    return str(BACKEND_ROOT.joinpath(*parts))


def project_path(*parts: str) -> str:
    """Absolute path inside the project tree (the whole repository)."""
    return str(PROJECT_ROOT.joinpath(*parts))


def catalog_path(*parts: str) -> str:
    """Absolute path inside the artifact-hub catalogs (``<project>/docker/``)."""
    return str(CATALOGS_ROOT.joinpath(*parts))


__all__ = [
    "BACKEND_ROOT",
    "PROJECT_ROOT",
    "CATALOGS_ROOT",
    "backend_path",
    "project_path",
    "catalog_path",
]
