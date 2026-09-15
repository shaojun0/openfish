"""Centralised template loading — lru-cached file reads.

Only *machine-facing* templates live here.  Human-facing pages are part of the
Vue SPA under ``frontend/`` and never touch this module.

Layout
------
Templates are grouped **by ecosystem**, mirroring the sidebar and the URL
namespaces, so a new index element goes next to the siblings it belongs to::

    static/python/    PEP 503 index + python-build-standalone listings
    static/node/      the nodejs.org/dist build mirror listings
    static/tools/     the tools directory index
    static/npm/       the npm catalog index
    static/docker/    the docker artifact index
    static/debian/    the debian package index

``services/hub.py`` produces the data; the Jinja templates here only render it.
"""

from functools import lru_cache

#: ``static/<ecosystem>/<file>`` — one place to change if the tree moves.
_PYTHON = "static/python"
_NODE = "static/node"
_TOOLS = "static/tools"
_NPM = "static/npm"
_DOCKER = "static/docker"
_DEBIAN = "static/debian"


@lru_cache(maxsize=16)
def _load(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


# ── Python ───────────────────────────────────────────────────────────

def pypi_simple_index() -> str:
    return _load(f"{_PYTHON}/simple_index.html")


def pypi_simple_package() -> str:
    return _load(f"{_PYTHON}/simple_package.html")


def build_discovery() -> str:
    return _load(f"{_PYTHON}/build_discovery.html")


def build_release() -> str:
    return _load(f"{_PYTHON}/build_release.html")


# ── Node.js ──────────────────────────────────────────────────────────

def node_build_discovery() -> str:
    return _load(f"{_NODE}/build_discovery.html")


def node_build_release() -> str:
    return _load(f"{_NODE}/build_release.html")


# ── Tools / npm ──────────────────────────────────────────────────────

def tools_index() -> str:
    return _load(f"{_TOOLS}/index.html")


def npm_index() -> str:
    return _load(f"{_NPM}/index.html")


def docker_index() -> str:
    return _load(f"{_DOCKER}/index.html")


def debian_index() -> str:
    return _load(f"{_DEBIAN}/index.html")
