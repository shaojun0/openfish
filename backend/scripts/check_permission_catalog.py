#!/usr/bin/env python
"""Gate: the permission catalogue, the guards and the docs must describe one set.

Run from the backend directory (`backend/`)::

    python scripts/check_permission_catalog.py

Why this exists
---------------
Three separate gates already look at permissions, and each one has a blind spot
that this gate closes:

* ``check_permission_labels.py`` pins the point each *view* is guarded by.  It
  says nothing about whether that point is a declared built-in, because
  ``auth.permissions.declare()`` lets a guard introduce a brand-new code — one
  that lands in the catalogue with its code as a placeholder name, never appears
  in ``README`` and never gets a curated label.
* ``check_rbac.py`` asserts ``orphan_permissions() == []`` — "no declared point
  is held by zero roles".  The ``admin`` role holds *everything*, so that is
  trivially true for any point that shipped, including one that was never given
  to ``authenticated``.  That is exactly how ``nodebuild:read`` /
  ``nodebuild:download`` / ``nodebuild:sha256`` were reachable by nobody but
  admins for a whole release.
* Nothing at all checked the *decision* behind a point.  Adding a new mirror
  point silently defaults it to "admin only", which is never what a mirror is.

So this gate makes three things explicit and enforces them:

1. **No unregistered points.**  Every code a guard checks must be a built-in
   declared in ``auth/permissions.py``.  A bare string or an unlisted constant
   fails here, not in production.
2. **No dead points.**  Every built-in code must be checked by at least one
   guard, so a point cannot outlive the route that used it.
3. **Every point is classified.**  The table below is the deliberate answer to
   "should an ordinary signed-in user get this?": a code is either
   ``AUTHENTICATED_SEEDED`` (seeded to the auto-granted ``authenticated`` role)
   or ``ADMIN_ONLY``.  The gate asserts the table matches
   ``services.authz._AUTHENTICATED_SEED`` exactly and covers ``BUILTIN``
   entirely, so shipping a new point forces that decision instead of defaulting
   it.  ``ANONYMOUS_SEEDED`` records the separate, much smaller answer to
   "should a caller who never authenticated get this?" and is pinned to
   ``services.authz._ANONYMOUS_SEED`` the same way.

``README``'s permission table is checked against the same set, in both
directions.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
# README.md documents the project as a whole and stays at the project root,
# one level above the backend package.
PROJECT_ROOT = REPO_ROOT.parent
sys.path.insert(0, str(REPO_ROOT))

from auth import permissions as P  # noqa: E402
from services.authz import _ANONYMOUS_SEED, _AUTHENTICATED_SEED  # noqa: E402

ROUTES_DIR = REPO_ROOT / "routes"

# ── The deliberate classification ────────────────────────────────────
# Every built-in point lives in exactly one of these two sets.
#
# AUTHENTICATED_SEEDED — handed to every account when the `authenticated` role
#   is created.  This is the "a signed-in developer is expected to consume this
#   mirror" bucket: `uv python install`, `nvm install`, `npm install`,
#   `docker pull`, `apt-get install`, the tools catalog, reading the handbook,
#   and managing your own API keys.
#
# ADMIN_ONLY — deliberately *not* seeded.  Either it is an administrative
#   capability, or it mutates shared state that ordinary users should not touch.
AUTHENTICATED_SEEDED: frozenset[str] = frozenset({
    P.PACKAGE_READ, P.PACKAGE_WRITE,
    P.BUILD_READ, P.BUILD_DOWNLOAD, P.BUILD_SHA256,
    P.NODE_BUILD_READ, P.NODE_BUILD_DOWNLOAD, P.NODE_BUILD_SHA256,
    P.TOOL_READ, P.TOOL_DOWNLOAD,
    P.NPM_READ, P.NPM_DOWNLOAD, P.NPM_PUBLISH,
    P.DOCKER_READ, P.DOCKER_DOWNLOAD,
    P.DEBIAN_READ, P.DEBIAN_DOWNLOAD,
    P.DEBIAN_OFFLINE,
    P.MODEL_READ,
    P.MODEL_RESOLVE,
    P.DOC_READ,
    P.APP_READ,
    P.KEY_LIST, P.KEY_CREATE, P.KEY_DELETE, P.KEY_STATS,
})

ADMIN_ONLY: frozenset[str] = frozenset({
    P.MODEL_WRITE,          # edits the shared routing table
    P.DOC_UPLOAD,           # edits the shared handbook
    P.TOOL_UPLOAD,          # publishes into the shared tools catalog
    P.DOCKER_UPLOAD,        # publishes into the shared docker catalog
    P.DEBIAN_UPLOAD,        # writes .deb files into the shared debian repository
    P.ADMIN_VIEW,
    P.ADMIN_REFRESH,
    P.ADMIN_ROLES,
})

#: ANONYMOUS_SEEDED — handed to requests that never authenticated, which is only
#: reachable with ``AUTH_ENABLED=false``.  This is a *third* annotation rather
#: than a third bucket of the partition above: a point may legitimately be here
#: and in AUTHENTICATED_SEEDED at once (``doc:read`` is, by design).
#:
#: The bar is deliberately high — an anonymous caller is a stranger on the
#: network, so the policy is "documentation and nothing else".  Note that the
#: application shell is *not* listed and cannot be: it is guarded by
#: ``require_auth``, not by a point, so no role edit can open the UI to
#: anonymous traffic.
ANONYMOUS_SEEDED: frozenset[str] = frozenset({
    P.DOC_READ,
})

#: ``require_*`` alias -> the constant it implies.
_ALIASES = {
    "require_admin": "ADMIN_VIEW",
    "require_package_read": "PACKAGE_READ",
    "require_package_write": "PACKAGE_WRITE",
}

CODE_RE = re.compile(r"`([a-z][a-z0-9_]*:[a-z][a-z0-9_]*)`")


def _call_name(node: ast.Call) -> str:
    target = node.func
    parts: list[str] = []
    while isinstance(target, ast.Attribute):
        parts.append(target.attr)
        target = target.value
    if isinstance(target, ast.Name):
        parts.append(target.id)
    return ".".join(reversed(parts))


def _resolve(node: ast.expr) -> tuple[str | None, str | None]:
    """``(code, problem)`` for one guard argument.

    A bare string is returned as a code *and* flagged, because it means the
    point was never declared in ``auth/permissions.py``.
    """
    if isinstance(node, ast.Constant):
        if isinstance(node.value, str):
            return node.value, f"bare string {node.value!r} (declare a constant instead)"
        return None, f"non-string argument {ast.unparse(node)!r}"
    source = ast.unparse(node).strip()
    name = source.split(".")[-1]
    value = getattr(P, name, None)
    if isinstance(value, str):
        return value, None
    return None, f"unresolvable reference {source!r}"


def collect_guard_codes() -> tuple[dict[str, set[str]], list[str]]:
    """Codes referenced by every guard, and any problems resolving them.

    Walks *every* ``require_*`` call in ``routes/`` rather than only decorators,
    so a blueprint-wide ``bp.before_request(require_permission(X))`` in
    ``routes/__init__.py`` is covered too.
    """
    codes: dict[str, set[str]] = {}
    problems: list[str] = []

    for path in sorted(ROUTES_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        rel = path.relative_to(REPO_ROOT)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = _call_name(node)
            short = name.split(".")[-1]
            if short == "require_permission":
                if not node.args:
                    problems.append(f"{rel}:{node.lineno} require_permission() with no argument")
                    continue
                code, problem = _resolve(node.args[0])
                if problem:
                    problems.append(f"{rel}:{node.lineno} {problem}")
                if code:
                    codes.setdefault(code, set()).add(f"{rel}:{node.lineno}")
            elif short in _ALIASES:
                const = _ALIASES[short]
                code = getattr(P, const, None)
                if isinstance(code, str):
                    codes.setdefault(code, set()).add(f"{rel}:{node.lineno} (@{short})")
                else:
                    problems.append(f"{rel}:{node.lineno} @{short} -> unknown {const}")
    return codes, problems


def read_readme_codes() -> tuple[set[str], list[str]]:
    """Permission codes documented in README's permission table."""
    text = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    marker = "The permission points shipped today"
    problems: list[str] = []
    start = text.find(marker)
    if start < 0:
        return set(), [f"README.md: could not find the permission table marker {marker!r}"]
    rest = text[start:]
    end = rest.find("\n### ")
    section = rest if end < 0 else rest[:end]
    codes = set(CODE_RE.findall(section))
    if not codes:
        problems.append("README.md: permission table lists no codes")
    return codes, problems


def check() -> tuple[list[str], list[str]]:
    """Return ``(failures, notes)``."""
    failures: list[str] = []
    notes: list[str] = []

    builtin = set(P.BUILTIN)
    referenced, problems = collect_guard_codes()
    failures.extend(problems)

    # 1. Every code a guard checks is a declared built-in point.
    unregistered = sorted(set(referenced) - builtin)
    for code in unregistered:
        failures.append(
            f"{code!r} is checked by a guard but is not declared in "
            f"auth/permissions.BUILTIN — add the constant, put it in BUILTIN and "
            f"__all__, and classify it below.  It currently ships with its code "
            f"as its display name and is absent from README."
        )

    # 2. Every built-in point is actually enforced by some guard.
    for code in sorted(builtin - set(referenced)):
        failures.append(
            f"{code!r} is declared in BUILTIN but no guard checks it — remove the "
            f"point, or attach the guard it was meant for."
        )

    # 3. The classification is a partition of BUILTIN.
    both = sorted(AUTHENTICATED_SEEDED & ADMIN_ONLY)
    for code in both:
        failures.append(f"{code!r} is classified as both authenticated-seeded and admin-only.")
    uncovered = sorted(builtin - AUTHENTICATED_SEEDED - ADMIN_ONLY)
    for code in uncovered:
        failures.append(
            f"{code!r} is a built-in point with no classification. Decide whether "
            f"an ordinary signed-in user should get it and add it to "
            f"AUTHENTICATED_SEEDED or ADMIN_ONLY here."
        )
    unknown = sorted((AUTHENTICATED_SEEDED | ADMIN_ONLY) - builtin)
    for code in unknown:
        failures.append(f"{code!r} is classified here but is not a BUILTIN point (typo?).")

    # 4. The classification agrees with the seed the code actually applies.
    seeded = set(_AUTHENTICATED_SEED)
    for code in sorted(seeded - AUTHENTICATED_SEEDED):
        failures.append(
            f"{code!r} is in services.authz._AUTHENTICATED_SEED but classified as "
            f"admin-only here — one of the two is wrong."
        )
    for code in sorted(AUTHENTICATED_SEEDED - seeded):
        failures.append(
            f"{code!r} is classified as authenticated-seeded here but is missing "
            f"from services.authz._AUTHENTICATED_SEED — a fresh deployment would "
            f"not grant it, which is the nodebuild:* bug."
        )

    # 5. The anonymous seed matches its declared policy.
    anonymous = set(_ANONYMOUS_SEED)
    for code in sorted(anonymous - builtin):
        failures.append(f"{code!r} is in _ANONYMOUS_SEED but is not a BUILTIN point.")
    for code in sorted(anonymous - ANONYMOUS_SEEDED):
        failures.append(
            f"{code!r} is granted to the `anonymous` role in "
            f"services.authz._ANONYMOUS_SEED but is not declared in "
            f"ANONYMOUS_SEEDED here — an anonymous caller is a stranger on the "
            f"network, so widening that set must be a reviewed decision."
        )
    for code in sorted(ANONYMOUS_SEEDED - anonymous):
        failures.append(
            f"{code!r} is declared ANONYMOUS_SEEDED here but is missing from "
            f"services.authz._ANONYMOUS_SEED, so anonymous callers do not "
            f"actually get it."
        )

    # 6. README documents exactly the built-in set.
    documented, readme_problems = read_readme_codes()
    failures.extend(readme_problems)
    for code in sorted(builtin - documented):
        failures.append(f"{code!r} is a built-in point but is missing from README's permission table.")
    for code in sorted(documented - builtin):
        failures.append(f"README's permission table documents {code!r}, which is not a built-in point.")

    notes.append(f"built-in points            : {len(builtin)}")
    notes.append(f"checked by a guard         : {len(referenced)}")
    notes.append(f"authenticated-seeded       : {len(AUTHENTICATED_SEEDED)}")
    notes.append(f"admin-only                 : {len(ADMIN_ONLY)}")
    notes.append(f"anonymous-seeded           : {sorted(ANONYMOUS_SEEDED)}")
    notes.append(f"documented in README       : {len(documented)}")
    return failures, notes


def main() -> int:
    failures, notes = check()

    print("── catalogue / guards / docs agree ─────────────────────────────")
    for line in notes:
        print(f"   {line}")
    print()
    if failures:
        for problem in failures:
            print(f"   ✗ {problem}")
        print()
        print(f"❌ permission catalog check FAILED — {len(failures)} problem(s)")
        return 1
    print("   ✅ every guard checks a declared point, every declared point is")
    print("      enforced, classified and documented, and the classification")
    print("      matches the seed the code applies")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
