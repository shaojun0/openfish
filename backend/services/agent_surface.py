"""The agent's Forgejo capability surface — a closed allowlist, no merge ever.

The agent runtime must never be able to merge, approve, or change branch
protection; that is a red line, not a convention.  Two mechanisms enforce it:

* :class:`RestrictedForgejoClient` is the **only** client object the agent path
  (``services.agent_worker.build_open_pr_fn``) ever hands to the runner.  Its
  ``__getattr__`` forwards exactly :data:`ALLOWED_CLIENT_METHODS` and raises
  :class:`AgentSurfaceError` for anything else, so even a future
  ``ForgejoClient.merge_pull_request`` is unreachable from the agent.
* :func:`scan_forbidden_api` parses the modules reachable from the agent path
  with :mod:`ast` and reports any string literal, attribute name or identifier
  that names a merge / approve / protection operation.  A gate runs it, so a
  forbidden call cannot be added quietly.

Neither mechanism is the *real* red line: the only path to the default branch is
a human merge, enforced by Forgejo branch protection (see
``docker/forgejo/README.md`` and ``docs/agent-hub/DESIGN-ai-checks.md``).  This
module makes openfish's own code incapable of trying; Forgejo makes an openfish
bug harmless.
"""

from __future__ import annotations

import ast
import logging
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

logger = logging.getLogger("cpypiserver.agent_surface")

#: The complete write surface the agent path may use.  ``create_pull_request``
#: opens an ``agent/*`` PR; a comment method is listed for the day the client
#: grows one.  Nothing here can merge, approve or edit protection.
ALLOWED_CLIENT_METHODS: tuple[str, ...] = (
    "create_pull_request",
    "comment_issue",
    "create_issue_comment",
)

#: Path/operation markers that must never appear in any **string literal** on
#: the agent path (an API path or an operation name written out).
FORBIDDEN_PATH_MARKERS: tuple[str, ...] = (
    "/merge",
    "/reviews",
    "merge_pull_request",
    "merge_branch",
    "dismiss_review",
    "branch_protection",
    "update_branch_protection",
    "delete_branch",
    "force_push",
)

#: Markers checked against **identifiers** (attribute names, function names).
#: Substring matching is safe here because these are code names, not prose:
#: a docstring saying "no merge, approve" is not a call.
FORBIDDEN_IDENTIFIER_MARKERS: tuple[str, ...] = (
    "merge_pull",
    "merge_branch",
    "approve",
    "dismiss",
    "branch_protection",
    "delete_branch",
    "force_push",
)

#: The combined marker set, for callers that audit a client's method names.
FORBIDDEN_API_MARKERS: tuple[str, ...] = FORBIDDEN_PATH_MARKERS + FORBIDDEN_IDENTIFIER_MARKERS

#: Modules on the agent path that :func:`scan_forbidden_api` audits.  Relative
#: to the backend package root.  ``agent_surface.py`` is deliberately **not**
#: here: it is the guard, and it necessarily spells the forbidden markers out,
#: so scanning it would flag the guard itself.
AGENT_PATH_MODULES: tuple[str, ...] = (
    "services/agent_runner.py",
    "services/agent_worker.py",
    "services/repo_import.py",
)


class AgentSurfaceError(Exception):
    """The agent path tried to reach a Forgejo capability it does not have."""


class RestrictedForgejoClient:
    """A proxy that forwards only the allowlisted pull-request operations.

    Deliberately not a subclass: a subclass would inherit every current and
    future ``ForgejoClient`` method, which is exactly the leak this exists to
    close.
    """

    def __init__(
        self,
        client: Any,
        *,
        allow: Sequence[str] = ALLOWED_CLIENT_METHODS,
    ) -> None:
        object.__setattr__(self, "_client", client)
        object.__setattr__(self, "_allow", frozenset(str(name) for name in allow))

    def __getattr__(self, name: str) -> Any:
        # ``_``-prefixed names are this object's own plumbing; never forward
        # them, or ``__getattr__`` would recurse through ``_client``.
        if name.startswith("_"):
            raise AttributeError(name)
        if name not in self._allow:
            raise AgentSurfaceError(
                f"agent 路径不允许调用 Forgejo 客户端方法 {name!r}"
                f"（能力白名单：{', '.join(sorted(self._allow))}）；"
                "合并/批准/改分支保护永远不在白名单里"
            )
        return getattr(self._client, name)

    def __setattr__(self, name: str, value: Any) -> None:
        raise AgentSurfaceError(f"agent 路径不允许改写客户端属性 {name!r}")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<RestrictedForgejoClient allow={sorted(self._allow)}>"


# ── Static audit ─────────────────────────────────────────────────────

def _string_literals(tree: ast.AST) -> list[str]:
    return [
        node.value for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]


def _identifier_names(tree: ast.AST) -> list[str]:
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            names.append(node.attr)
        elif isinstance(node, ast.Name):
            names.append(node.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names.append(node.name)
    return names


def scan_forbidden_api(
    modules: Iterable[str | Path] | None = None,
    *,
    root: str | Path | None = None,
) -> list[str]:
    """``["path:marker", ...]`` for every forbidden operation found.

    *root* defaults to the backend package root (the parent of this file's
    directory); *modules* defaults to :data:`AGENT_PATH_MODULES`.  A module that
    does not exist is reported (that is a stronger failure, not a pass).

    String literals are checked against :data:`FORBIDDEN_PATH_MARKERS` (an API
    path written out is a call); identifiers are checked against
    :data:`FORBIDDEN_IDENTIFIER_MARKERS` (substring matching on code names is
    safe, so a docstring sentence cannot trip it).
    """
    base = Path(root) if root is not None else Path(__file__).resolve().parent.parent
    targets = list(modules) if modules is not None else list(AGENT_PATH_MODULES)
    findings: list[str] = []
    for module in targets:
        path = Path(module)
        if not path.is_absolute():
            path = base / path
        try:
            source = path.read_text(encoding="utf-8")
        except OSError:
            findings.append(f"{module}:missing")
            continue
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError as exc:
            findings.append(f"{module}:unparseable({exc.msg})")
            continue
        literals = [value.lower() for value in _string_literals(tree)]
        for marker in FORBIDDEN_PATH_MARKERS:
            if any(marker.lower() in value for value in literals):
                findings.append(f"{module}:{marker}")
        for name in _identifier_names(tree):
            hit = _identifier_hit(name)
            if hit is not None:
                findings.append(f"{module}:{hit}()")
    return findings


def _identifier_hit(name: str) -> str | None:
    """The forbidden marker *name* names, or ``None`` for a harmless name."""
    lowered = str(name).lower()
    if lowered == "merge":
        return "merge"
    for marker in FORBIDDEN_IDENTIFIER_MARKERS:
        if marker in lowered:
            return marker
    return None


def forbidden_client_methods(client: Any) -> list[str]:
    """Public method names on *client* that the agent must never be able to call."""
    hits: list[str] = []
    for name in (item for item in dir(client) if not item.startswith("_")):
        if _identifier_hit(name) is not None:
            hits.append(name)
    return hits


def surface_report(root: str | Path | None = None) -> Mapping[str, Any]:
    """A JSON-shaped audit result a gate (or an operator) can print."""
    return {
        "allowed": list(ALLOWED_CLIENT_METHODS),
        "forbidden_markers": list(FORBIDDEN_API_MARKERS),
        "modules": list(AGENT_PATH_MODULES),
        "findings": scan_forbidden_api(root=root),
    }


__all__ = [
    "AGENT_PATH_MODULES",
    "ALLOWED_CLIENT_METHODS",
    "FORBIDDEN_API_MARKERS",
    "FORBIDDEN_IDENTIFIER_MARKERS",
    "FORBIDDEN_PATH_MARKERS",
    "AgentSurfaceError",
    "RestrictedForgejoClient",
    "forbidden_client_methods",
    "scan_forbidden_api",
    "surface_report",
]
