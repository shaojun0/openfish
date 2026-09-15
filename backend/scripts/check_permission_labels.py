#!/usr/bin/env python
"""Gate: a guarded route must carry the *semantically correct* permission point.

Run from the backend directory (`backend/`)::

    python scripts/check_permission_labels.py

Why this exists
---------------
``scripts/check_auth_guards.py`` proves an endpoint *refuses anonymous access*.
That is not enough.  A route guarded by the wrong point still answers 401 to an
anonymous caller, so the guard check stays green while the label lies.  Two real
examples this gate was written for:

* ``/docker/v2/<name>/blobs/<digest>`` streams the image layer bytes that
  ``docker pull`` downloads, but was guarded by ``docker:read`` ("浏览 Docker
  目录" — browse the catalog).  A browse-only role could pull full images and
  ``docker:download`` never applied.
* ``/debian/pool/<path>`` streams ``.deb`` package bytes, guarded by
  ``debian:read`` for the same reason.

So this gate pins down the *intended* route -> permission mapping for every
per-route guard in ``routes/`` and fails when the two drift apart.  The table
below **is** the contract; adding a route means deciding its point here.

Checks
------
1. **Explicit table** — every guarded route must use exactly the constant
   listed in ``EXPECTED``.  A route missing from the table, or an entry that no
   longer matches a real route, is a failure (so renames cannot slip through).
2. **Method vs class** — a mutating route may never use a ``:read`` point, and
   a GET/HEAD-only route may never use a ``:write``/``:upload`` point.
3. **Built-in sanity** — every point seeded to ``authenticated`` must exist in
   ``auth.permissions.BUILTIN``.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

ROUTES_DIR = REPO_ROOT / "routes"

#: Blueprints guarded once in ``routes/__init__.py`` instead of per route.
#: ``AUTH_ONLY`` means "authenticated, no specific point" (the per-route
#: decorator then supplies the point).
BLUEPRINT_GUARDS: dict[str, str] = {
    "admin": "ADMIN_VIEW",
    "access": "ADMIN_ROLES",
    "api_keys": "AUTH_ONLY",
    "pypi": "AUTH_ONLY",
    # The browser app: gated by its own point so it stays closed to `anonymous`
    # even while `AUTH_ENABLED=false`.
    "spa": "APP_READ",
}

#: ``(module stem, view function) -> permission constant`` for every route that
#: carries its own ``@require_permission``.  Read it as the answer to "what must
#: a caller hold to reach this route?".
EXPECTED: dict[tuple[str, str], str] = {
    # ── PyPI: read deliberately includes downloading (see the point's own
    #    description: "浏览 PEP 503 索引并下载包文件"). ──────────────────
    ("pypi", "simple_index"): "PACKAGE_READ",
    ("pypi", "package_page"): "PACKAGE_READ",
    ("pypi", "serve_package"): "PACKAGE_READ",
    ("pypi", "serve_package_from_simple"): "PACKAGE_READ",
    ("pypi", "upload"): "PACKAGE_WRITE",
    # ── python-build-standalone ──────────────────────────────────────────
    ("python_build", "discovery"): "BUILD_READ",
    ("python_build", "release_page"): "BUILD_READ",
    ("python_build", "download"): "BUILD_DOWNLOAD",
    ("python_build", "sha256"): "BUILD_SHA256",
    ("python_build", "catalog"): "BUILD_READ",
    # ── nodejs.org/dist ──────────────────────────────────────────────────
    ("node_build", "discovery"): "NODE_BUILD_READ",
    ("node_build", "index_json"): "NODE_BUILD_READ",
    ("node_build", "index_tab"): "NODE_BUILD_READ",
    ("node_build", "release_page"): "NODE_BUILD_READ",
    ("node_build", "shasums"): "NODE_BUILD_DOWNLOAD",
    ("node_build", "download"): "NODE_BUILD_DOWNLOAD",
    ("node_build", "sha256"): "NODE_BUILD_SHA256",
    ("node_build", "catalog"): "NODE_BUILD_READ",
    # ── tools catalog ────────────────────────────────────────────────────
    ("hub", "tools_index"): "TOOL_READ",
    ("hub", "download_tool"): "TOOL_DOWNLOAD",
    ("hub", "tools_catalog"): "TOOL_READ",
    # ── model routing ────────────────────────────────────────────────────
    ("hub", "model_routes_index"): "MODEL_READ",
    # The resolved view is the *machine* half of the routing table: it carries
    # each route's real upstream api_key, which the console view must never
    # return. The dedicated point keeps "browse the table" and "hand a client
    # the keys" separately revocable.
    ("hub", "model_routes_resolved"): "MODEL_RESOLVE",
    ("hub", "create_model_route"): "MODEL_WRITE",
    ("hub", "update_model_route"): "MODEL_WRITE",
    ("hub", "delete_model_route"): "MODEL_WRITE",
    ("hub", "probe_model_route"): "MODEL_WRITE",
    ("hub", "check_model_route"): "MODEL_WRITE",
    # ── npm: metadata is read, tarball bytes are download ────────────────
    ("npm", "npm_index"): "NPM_READ",
    ("npm", "npm_all"): "NPM_READ",
    ("npm", "npm_ping"): "NPM_READ",
    ("npm", "npm_search"): "NPM_READ",
    ("npm", "npm_packument"): "NPM_READ",
    ("npm", "npm_version"): "NPM_READ",
    ("npm", "npm_tarball"): "NPM_DOWNLOAD",
    ("npm", "download_npm_file"): "NPM_DOWNLOAD",
    ("npm", "npm_catalog"): "NPM_READ",
    # ── docker: manifests/catalog are read, blobs are download ───────────
    ("docker", "docker_index"): "DOCKER_READ",
    ("docker", "docker_v2_probe"): "DOCKER_READ",
    ("docker", "docker_catalog"): "DOCKER_READ",
    ("docker", "docker_tags"): "DOCKER_READ",
    ("docker", "docker_manifest"): "DOCKER_READ",
    ("docker", "docker_blob"): "DOCKER_DOWNLOAD",
    ("docker", "download_docker_file"): "DOCKER_DOWNLOAD",
    ("docker", "docker_catalog_api"): "DOCKER_READ",
    # ── debian: dists/ metadata is read, pool/ packages are download ─────
    ("debian", "debian_index"): "DEBIAN_READ",
    ("debian", "debian_packages"): "DEBIAN_READ",
    ("debian", "download_debian_file"): "DEBIAN_DOWNLOAD",
    ("debian", "debian_dists"): "DEBIAN_READ",
    ("debian", "debian_pool"): "DEBIAN_DOWNLOAD",
    ("debian", "debian_catalog_api"): "DEBIAN_READ",
    # ── per-ecosystem Markdown docs ──────────────────────────────────────
    ("docs", "docs_overview"): "DOC_READ",
    ("docs", "docs_catalog"): "DOC_READ",
    ("docs", "docs_create"): "DOC_UPLOAD",
    ("docs", "docs_document"): "DOC_READ",
    ("docs", "docs_update"): "DOC_UPLOAD",
    ("docs", "docs_delete"): "DOC_UPLOAD",
    ("docs", "docs_preview"): "DOC_UPLOAD",
    ("docs", "docs_assets"): "DOC_READ",
    ("docs", "docs_asset_upload"): "DOC_UPLOAD",
    ("docs", "docs_asset_delete"): "DOC_UPLOAD",
    ("docs", "docs_index_redirect"): "DOC_READ",
    ("docs", "docs_index"): "DOC_READ",
    ("docs", "docs_raw"): "DOC_READ",
    ("docs", "docs_asset_raw"): "DOC_READ",
    # ── API keys (also behind the api_keys blueprint's require_auth) ─────
    ("api_keys", "list_keys"): "KEY_LIST",
    ("api_keys", "create_key"): "KEY_CREATE",
    ("api_keys", "delete_key"): "KEY_DELETE",
    ("api_keys", "key_stats"): "KEY_STATS",
    # ── administration ───────────────────────────────────────────────────
    ("admin", "stats"): "ADMIN_VIEW",
    ("admin", "refresh_stats"): "ADMIN_REFRESH",
    # ── SPA JSON ─────────────────────────────────────────────────────────
    ("session", "packages"): "PACKAGE_READ",
    # ── Device authorization ─────────────────────────────────────────────
    # Approving mints an API key, so it needs the same point as creating one
    # from the console. `/device` itself carries no point: it bounces an
    # unauthenticated visitor through the login flow, and the approval it
    # renders lands on this guarded POST.
    ("device", "approve_device"): "KEY_CREATE",
}

MUTATING = frozenset({"POST", "PUT", "PATCH", "DELETE"})
READING = frozenset({"GET", "HEAD"})


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


def _guard_constant(node: ast.expr) -> str | None:
    """Constant checked by a ``require_*`` decorator, if it names one."""
    name = _decorator_name(node)
    if name == "require_admin":
        return "ADMIN_VIEW"
    if name.split(".")[-1] != "require_permission":
        return None
    if isinstance(node, ast.Call) and node.args:
        return ast.unparse(node.args[0]).strip()
    return None


def _route_methods(node: ast.expr) -> set[str]:
    if not isinstance(node, ast.Call):
        return set()
    for kw in node.keywords:
        if kw.arg == "methods":
            try:
                return {str(m).upper() for m in ast.literal_eval(kw.value)}
            except (ValueError, SyntaxError):
                return set()
    return set()


def collect() -> dict[tuple[str, str], dict]:
    """Every routed view, with its decorator guard and HTTP methods."""
    found: dict[tuple[str, str], dict] = {}
    for path in sorted(ROUTES_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            rule: str | None = None
            methods: set[str] = set()
            guard: str | None = None
            for dec in node.decorator_list:
                name = _decorator_name(dec)
                if name.endswith(".route"):
                    for arg in dec.args:
                        try:
                            value = ast.literal_eval(arg)
                        except (ValueError, SyntaxError):
                            value = None
                        if isinstance(value, str):
                            rule = value
                    methods |= _route_methods(dec)
                else:
                    guard = guard or _guard_constant(dec)
            if rule is None:
                continue
            found[(path.stem, node.name)] = {
                "rule": rule,
                "methods": methods or {"GET"},
                "guard": guard,
                "blueprint": BLUEPRINT_GUARDS.get(path.stem),
                "file": str(path.relative_to(REPO_ROOT)),
                "lineno": node.lineno,
            }
    return found


def check_table(found: dict[tuple[str, str], dict]) -> list[str]:
    """Every decorated route must match EXPECTED, and vice versa."""
    problems: list[str] = []

    for key, info in sorted(found.items()):
        if info["guard"] is None:
            continue  # public, or guarded by its blueprint — see other checks
        expected = EXPECTED.get(key)
        if expected is None:
            problems.append(
                f"{info['file']}:{info['lineno']} {key[1]}() — guarded by "
                f"{info['guard']} but missing from the EXPECTED table. "
                f"Add it so the point is a deliberate, reviewed choice."
            )
        elif info["guard"] != expected:
            problems.append(
                f"{info['file']}:{info['lineno']} {key[1]}() — guarded by "
                f"{info['guard']}, expected {expected}."
            )

    for key, expected in sorted(EXPECTED.items()):
        if key not in found:
            problems.append(
                f"EXPECTED lists {key[0]}.{key[1]} -> {expected}, but no such "
                f"guarded route exists (renamed or removed?)."
            )
    return problems


def check_method_class(found: dict[tuple[str, str], dict]) -> list[str]:
    """A mutation may not use a read point; a pure read may not use a write point."""
    problems: list[str] = []
    for key, info in sorted(found.items()):
        guard = info["guard"] or info["blueprint"]
        if not guard or guard == "AUTH_ONLY":
            continue
        methods = info["methods"]
        where = f"{info['file']}:{info['lineno']} {key[1]}()"
        if methods & MUTATING and guard.endswith("_READ"):
            problems.append(f"{where} — mutating {sorted(methods)} uses read point {guard}.")
        if methods <= READING and guard.endswith(("_WRITE", "_UPLOAD")):
            problems.append(f"{where} — read-only {sorted(methods)} uses write point {guard}.")
    return problems


def check_seed_points() -> list[str]:
    """Every point seeded to `authenticated` must be a declared built-in."""
    import auth.permissions as P
    from services.authz import _AUTHENTICATED_SEED

    builtin = set(P.BUILTIN)
    unknown = sorted(code for code in _AUTHENTICATED_SEED if code not in builtin)
    return [
        f"services.authz._AUTHENTICATED_SEED grants {code!r}, which is not in "
        f"auth.permissions.BUILTIN."
        for code in unknown
    ]


def main() -> int:
    found = collect()
    failures: list[str] = []

    for title, problems in (
        ("route -> permission table", check_table(found)),
        ("method vs permission class", check_method_class(found)),
        ("seeded points are built-in", check_seed_points()),
    ):
        print(f"── {title} ──")
        if problems:
            failures.extend(problems)
            for problem in problems:
                print(f"   ✗ {problem}")
        else:
            print("   ✅ ok")
        print()

    guarded = sum(1 for i in found.values() if i["guard"])
    print(
        f"{guarded} per-route guarded view(s) checked against "
        f"{len(EXPECTED)} expected mapping(s)."
    )
    if failures:
        print(f"❌ permission label check FAILED — {len(failures)} problem(s)")
        return 1
    print("✅ permission label check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
