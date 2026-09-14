"""Centralised template loading — lru-cached file reads.

Only *machine-facing* templates live here.  Human-facing pages are part of the
Vue SPA under ``frontend/`` and never touch this module.
"""

from functools import lru_cache


@lru_cache(maxsize=8)
def _load(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def pypi_simple_index() -> str:
    return _load("static/pypi_template/simple_index.html")


def pypi_simple_package() -> str:
    return _load("static/pypi_template/simple_package.html")


def build_discovery() -> str:
    return _load("static/python_build_template/discovery.html")


def build_release() -> str:
    return _load("static/python_build_template/release.html")
