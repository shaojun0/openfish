"""Extension plugin architecture.

Extensions are self-contained modules that hook into the Flask app lifecycle.
Each extension declares its dependencies; the registry resolves init order
via topological sort.

Usage — adding a new extension::

    1. Create ``extensions/my_feature.py`` with a class inheriting ``Extension``.
    2. Add it to the list in ``app.py``.

    class MyFeature(Extension):
        name = "my_feature"
        dependencies = ["database"]   # optional

        def init_app(self, app):
            app.extensions["my_feature"] = ...

No other file needs to change.
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from collections import deque

logger = logging.getLogger("cpypiserver.startup")


class Extension(ABC):
    """A pluggable extension with declared dependencies.

    Attributes:
        name: Unique identifier. Used as ``app.extensions[name]``.
        dependencies: Names of extensions that must be inited first.
    """

    name: str
    dependencies: list[str] = []

    @abstractmethod
    def init_app(self, app) -> None:
        """Initialise this extension. Called once during startup."""

    def teardown(self, app) -> None:
        """Optional cleanup. Called during app shutdown."""


class ExtensionRegistry:
    """Manages extension lifecycle with topological ordering."""

    def __init__(self, extensions: list[Extension]) -> None:
        self._extensions = extensions
        self._order = _topological_sort(extensions)

    def init_all(self, app) -> None:
        # Startup time is the one number an operator cannot get from the HTTP
        # logs: imports finish before gunicorn binds the port, so a slow index
        # scan shows up only as "every request took 16 s" on the first page
        # load.  Logging each extension's cost makes that visible in
        # ``docker logs openfish-backend``.
        started = time.perf_counter()
        for ext in self._order:
            ext_started = time.perf_counter()
            ext.init_app(app)
            logger.info(
                "extension %-14s ready in %6.1f ms",
                ext.name, (time.perf_counter() - ext_started) * 1000.0,
            )
        logger.info(
            "all extensions ready in %.1f ms", (time.perf_counter() - started) * 1000.0
        )

    def teardown_all(self, app) -> None:
        for ext in reversed(self._order):
            try:
                ext.teardown(app)
            except Exception:
                pass


def _topological_sort(extensions: list[Extension]) -> list[Extension]:
    """Kahn's algorithm — fail fast on cycles or missing deps."""
    by_name = {e.name: e for e in extensions}
    in_degree = {e.name: 0 for e in extensions}
    adj: dict[str, list[str]] = {e.name: [] for e in extensions}

    for e in extensions:
        for dep in e.dependencies:
            if dep not in by_name:
                raise ValueError(
                    f"Extension '{e.name}' depends on unknown '{dep}'"
                )
            adj[dep].append(e.name)
            in_degree[e.name] += 1

    queue = deque(name for name, deg in in_degree.items() if deg == 0)
    result: list[Extension] = []

    while queue:
        name = queue.popleft()
        result.append(by_name[name])
        for neighbor in adj[name]:
            in_degree[neighbor] -= 1
            if in_degree[neighbor] == 0:
                queue.append(neighbor)

    if len(result) != len(extensions):
        cycle = [n for n, d in in_degree.items() if d > 0]
        raise ValueError(f"Extension dependency cycle detected: {cycle}")

    return result
