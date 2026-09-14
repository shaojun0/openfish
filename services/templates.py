"""Centralised template loading — lru-cached file reads."""

from functools import lru_cache


@lru_cache(maxsize=8)
def _load(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def admin() -> str:
    return _load("static/admin.html")


def api_keys() -> str:
    return _load("static/api_keys.html")


def root() -> str:
    return _load("static/root.html")


def pypi_simple_index() -> str:
    return _load("static/pypi_template/simple_index.html")


def pypi_simple_package() -> str:
    return _load("static/pypi_template/simple_package.html")


def build_discovery() -> str:
    return _load("static/python_build_template/discovery.html")


def build_release() -> str:
    return _load("static/python_build_template/release.html")
