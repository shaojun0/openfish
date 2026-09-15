#!/usr/bin/env python
"""Gate: every protected endpoint must actually require credentials.

Run from the repository root::

    python scripts/check_auth_guards.py

This exists because of a real regression.  ``routes/python_build.py`` used to
read::

    @require_auth()                       # applied LAST -> discarded
    @python_build_bp.route("/python-builds/")   # registers the UNGUARDED view
    def discovery(): ...

Decorators apply bottom-up, so the permission check was applied *after* Flask
had already registered the bare function.  Every ``/python-builds/*`` route was
readable by anyone, and nothing failed: no exception, no log line, all 151
lines of source looked plausible.  The only way to catch it is to look.

Two independent checks run:

1. **Static** — an AST scan for a ``require_*`` decorator sitting *above* a
   ``@*.route`` decorator.  Fast, and points straight at the offending line.
2. **Runtime** — boots the app and requests every registered rule with no
   credentials.  Anything reachable that is not explicitly declared public is
   a failure.  This catches the ordering bug *and* a blueprint that simply
   forgot to attach a guard in ``routes/__init__.py``.

A route counts as deliberately public when its ``@api_operation`` metadata
declares ``security=[]``, or when its endpoint is listed in
``PUBLIC_ENDPOINTS`` below with a reason.
"""

from __future__ import annotations

import ast
import os
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
os.chdir(REPO_ROOT)

GUARD_NAMES = frozenset({
    "require_auth",
    "require_permission",
    "require_admin",
    "require_package_read",
    "require_package_write",
})

#: Endpoints that are anonymous on purpose.  Each needs a reason, because this
#: list is the only thing standing between "intentionally public" and
#: "accidentally unguarded".
PUBLIC_ENDPOINTS: dict[str, str] = {
    # The SPA shell is HTML; it holds no data.  The JSON behind it lives under
    # /api/v1 and is guarded per blueprint.
    "spa.index": "SPA shell",
    "spa.page": "SPA shell",
    "spa.fallback": "SPA shell",
    # Contract publication — describes routes, never serves registry data.
    "discovery.openapi_json": "publishes the API contract",
    "discovery.docs": "renders the API contract",
    "discovery.llms_txt": "renders the API contract",
    "discovery.api_catalog": "RFC 9727 api-catalog",
    # Login flow: reachable before you have a session, by definition.
    "auth.auth_login": "starts the OAuth2 login flow",
    "auth.auth_callback": "OAuth2 redirect target",
    "auth.auth_logout": "clears a session",
    # Liveness probes.
    "health.health": "liveness probe",
    "python_build.health": "mirror status probe",
    "node_build.health": "mirror status probe",
    # Answers "who am I?" — returns the anonymous principal when unauthenticated.
    "session.whoami": "identity probe",
}

#: Statuses that prove the guard ran.
DENIED = frozenset({401, 403})

#: `<int:role_id>` -> `1`, `<path:filename>` -> `x`, and so on.  Flask's test
#: client needs a *concrete* URL; a raw rule string containing the converter
#: syntax would fall through to the SPA catch-all and report a bogus 405.
_CONVERTER_RE = re.compile(r"<(?:([a-zA-Z_]+):)?([a-zA-Z_]+)>")


def _concrete_path(rule: str) -> str:
    """Substitute placeholder values into a rule so it can actually be routed."""
    def substitute(match: re.Match[str]) -> str:
        return "1" if match.group(1) == "int" else "x"

    return _CONVERTER_RE.sub(substitute, rule)


def _decorator_name(node: ast.expr) -> str:
    """Dotted name of a decorator expression, e.g. ``bp.route`` or ``require_auth``."""
    target = node.func if isinstance(node, ast.Call) else node
    parts: list[str] = []
    while isinstance(target, ast.Attribute):
        parts.append(target.attr)
        target = target.value
    if isinstance(target, ast.Name):
        parts.append(target.id)
    return ".".join(reversed(parts))


def check_decorator_order() -> list[str]:
    """AST scan: no ``require_*`` may sit above a ``@*.route`` decorator."""
    problems: list[str] = []

    for path in sorted((REPO_ROOT / "routes").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue

            names = [_decorator_name(d) for d in node.decorator_list]
            route_at = [i for i, n in enumerate(names)
                        if n.endswith(".route") or n == "route"]
            guard_at = [i for i, n in enumerate(names)
                        if n.split(".")[-1] in GUARD_NAMES]
            if not guard_at or not route_at:
                continue

            # Decorators apply bottom-up: the LAST source line is applied FIRST,
            # so @route must be the FIRST source line (index 0 of the list).
            topmost_route = min(route_at)
            misordered = [i for i in guard_at if i < topmost_route]
            if misordered:
                rel = path.relative_to(REPO_ROOT)
                problems.append(
                    f"{rel}:{node.lineno} {node.name}() — "
                    f"{names[misordered[0]]!r} is written above the route decorator, "
                    f"so it is applied after registration and never runs. "
                    f"Move it below @{names[topmost_route]}."
                )

    return problems


def check_endpoints_deny_anonymous() -> list[str]:
    """Runtime probe: unauthenticated access must be refused unless declared public."""
    problems: list[str] = []

    from app import app  # noqa: E402 - imported late so REPO_ROOT is on sys.path
    from openapi import operation_of  # noqa: E402

    client = app.test_client()

    for rule in sorted(app.url_map.iter_rules(), key=lambda r: str(r)):
        if rule.endpoint == "static":
            continue

        methods = sorted(m for m in rule.methods
                         if m in {"GET", "POST", "PUT", "PATCH", "DELETE"})
        if not methods:
            continue

        endpoint = rule.endpoint
        view = app.view_functions.get(endpoint)
        metadata = operation_of(view) or {}
        declared_public = metadata.get("security") == []
        if declared_public or endpoint in PUBLIC_ENDPOINTS:
            continue

        method = "GET" if "GET" in methods else methods[0]
        path = _concrete_path(str(rule))
        try:
            response = client.open(path, method=method)
            status = response.status_code
        except Exception as exc:  # noqa: BLE001 - any error means we never reached the guard
            problems.append(f"{method} {path} — raised {type(exc).__name__}: {exc}")
            continue

        if status not in DENIED:
            problems.append(
                f"{method} {path} returned {status} without credentials "
                f"(endpoint {endpoint!r}). Either attach a guard, or declare it "
                f"public with @api_operation(security=[]) / add it to PUBLIC_ENDPOINTS."
            )

    return problems


def main() -> int:
    failures: list[str] = []

    print("── Static: decorator order ─────────────────────────────────────")
    order_problems = check_decorator_order()
    if order_problems:
        failures.extend(order_problems)
        for problem in order_problems:
            print(f"   ✗ {problem}")
    else:
        print("   ✅ no require_* guard sits above a route decorator")

    print()
    print("── Runtime: anonymous reachability ─────────────────────────────")
    reach_problems = check_endpoints_deny_anonymous()
    if reach_problems:
        failures.extend(reach_problems)
        for problem in reach_problems:
            print(f"   ✗ {problem}")
    else:
        print("   ✅ every undeclared endpoint refuses anonymous access")

    print()
    if failures:
        print(f"❌ auth guard check FAILED — {len(failures)} problem(s)")
        return 1

    print("✅ auth guard check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
