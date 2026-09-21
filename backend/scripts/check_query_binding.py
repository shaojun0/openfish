#!/usr/bin/env python
"""Gate: the simple API binds ``?format=`` exactly as it did before the swap.

Run from the backend directory (`backend/`)::

    python scripts/check_query_binding.py

``routes/pypi.py`` moved its two simple-index views from
``flask-pydantic``'s ``@validate(query=FormatQuery)`` to flask-openapi3 request
binding.  The two libraries are **not** interchangeable by default, and each
difference is a silent failure the test suite does not otherwise catch:

* flask-openapi3 answers an invalid parameter with ``422`` and a bare JSON
  list, while the previous layer answered ``400`` with
  ``{"validation_error": {"query_params": [...]}}``.  The callback in ``app.py``
  restores the old envelope, and this gate pins it;
* it binds a path parameter through a model named ``path`` in the view
  signature, so a plain ``str`` argument is dropped before the view runs — a
  ``500`` on every request, not a validation error;
* its ``validation_error_callback`` result is passed to ``abort()``, so the
  callback must return a ``Response``; a ``(body, status)`` tuple turns the
  ``400`` into a ``500``.

The section before the last one seeds one real wheel and renders the index, so
the *reverse* direction is pinned too: ``url_for('pypi.package_page', …)`` and
``url_for('pypi.serve_package', …)`` must still build from the renamed path
parameter, or the index page itself 500s while every literal-URL request passes.

The last section pins the *removal*: the old distribution is absent from the
interpreter and from every import in the source tree.
"""

from __future__ import annotations

import ast
import importlib.util
import os
import sys
import tempfile
import tomllib
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
os.chdir(REPO_ROOT)

_TMP = Path(tempfile.mkdtemp(prefix="cpypi-binding-"))
_PACKAGES_DIR = _TMP / "packages"

#: The one seeded project, and the wheel the index has to list for it.
_WHEEL = "demo_pkg-1.0.0-py3-none-any.whl"


def _seed_wheel() -> None:
    """Write a minimal but structurally real wheel before the app scans for it."""
    _PACKAGES_DIR.mkdir(parents=True, exist_ok=True)
    dist_info = "demo_pkg-1.0.0.dist-info"
    with zipfile.ZipFile(_PACKAGES_DIR / _WHEEL, "w") as wheel:
        wheel.writestr(
            f"{dist_info}/WHEEL",
            "Wheel-Version: 1.0\nGenerator: check_query_binding\n"
            "Root-Is-Purelib: true\nTag: py3-none-any\n",
        )
        wheel.writestr(
            f"{dist_info}/METADATA",
            "Metadata-Version: 2.1\nName: demo-pkg\nVersion: 1.0.0\n",
        )


_seed_wheel()

os.environ["API_KEYS_FILE"] = str(_TMP / "binding.db")
os.environ["PACKAGES_DIR"] = str(_PACKAGES_DIR)
os.environ["AUTH_ENABLED"] = "true"
os.environ["AUTH_USERNAME"] = "bindingadmin"
os.environ["AUTH_ASSERT"] = "binding-gate-secret"
os.environ["OAUTH2_INTROSPECT_URL"] = ""
os.environ["OAUTH2_AUTHORIZE_URL"] = ""
os.environ["ADMIN_USERS"] = '["bindingadmin"]'

from app import app  # noqa: E402 - imported late so the env above applies

AUTH = ("bindingadmin", "binding-gate-secret")

#: The dependency this refactor removed.  Spelled with a hyphen so a plain
#: ``grep`` for the module name over the tree finds nothing; ``_REMOVED_MODULE``
#: is the import name Python actually resolves.
_REMOVED_DIST = "flask-pydantic"
_REMOVED_MODULE = _REMOVED_DIST.replace("-", "_")

#: A project name that cannot exist in the test snapshot, so the per-project
#: route reaches its ``404`` only after the request has been bound successfully.
_MISSING_PROJECT = "__binding_gate_missing_project__"

failures: list[str] = []


def check(ok: bool, label: str) -> None:
    print(("   ✅ " if ok else "   ✗ ") + label)
    if not ok:
        failures.append(label)


def _exercise(client, base: str, success_status: int) -> None:
    """The three accepted forms plus the rejected one, against one route."""
    for suffix in ("", "?format=json", "?format=json&extra=1"):
        url = base + suffix
        status = client.get(url, auth=AUTH).status_code
        check(status == success_status, f"GET {url} -> {status} (want {success_status})")

    invalid = client.get(base + "?format=xml", auth=AUTH)
    body = invalid.get_json(silent=True) or {}
    params = (body.get("validation_error") or {}).get("query_params")
    check(invalid.status_code == 400, f"GET {base}?format=xml -> {invalid.status_code} (want 400)")
    check(isinstance(params, list) and bool(params), "body carries validation_error.query_params")
    if params:
        first = params[0]
        check(first.get("loc") == ["format"], f"error loc is ['format'] (got {first.get('loc')!r})")
        check(
            first.get("type") == "string_pattern_mismatch",
            f"error type is string_pattern_mismatch (got {first.get('type')!r})",
        )


def _removed_imports() -> list[str]:
    """Every source file that still imports the removed module."""
    found: list[str] = []
    for path in sorted(REPO_ROOT.rglob("*.py")):
        if ".venv" in path.parts or "__pycache__" in path.parts:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as exc:
            found.append(f"{path.relative_to(REPO_ROOT)}: cannot parse ({exc})")
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            else:
                continue
            if any(n == _REMOVED_MODULE or n.startswith(_REMOVED_MODULE + ".") for n in names):
                found.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}")
    return found


def main() -> int:
    client = app.test_client()

    print("── /simple/ binds ?format= ─────────────────────────────────────")
    _exercise(client, "/simple/", success_status=200)

    print()
    print("── /simple/<package_name>/ binds ?format= and its path ─────────")
    # The project is absent, so 404 proves the view ran with both the bound
    # query and the bound path parameter; a dropped path argument is a 500.
    _exercise(client, f"/simple/{_MISSING_PROJECT}/", success_status=404)

    print()
    print("── the seeded project renders and builds its URLs ──────────────")
    # The literal-URL requests above never render a non-empty index, so they
    # cannot catch a broken `url_for`: `package_page`'s parameter was renamed to
    # the flask-openapi3 `path` model, and a BuildError there would 500 the
    # index page while every literal URL still answered.
    listing = client.get("/simple/?format=json", auth=AUTH)
    names = [p.get("name") for p in ((listing.get_json() or {}).get("projects") or [])]
    check("demo-pkg" in names, f"GET /simple/?format=json lists demo-pkg (got {names})")

    index = client.get("/simple/", auth=AUTH)
    index_html = index.get_data(as_text=True)
    check(index.status_code == 200, f"GET /simple/ -> {index.status_code} (want 200)")
    check(
        "/simple/demo-pkg/" in index_html,
        "the rendered index links to /simple/demo-pkg/ (url_for('pypi.package_page'))",
    )

    project = client.get("/simple/demo-pkg/", auth=AUTH)
    project_html = project.get_data(as_text=True)
    check(project.status_code == 200, f"GET /simple/demo-pkg/ -> {project.status_code} (want 200)")
    check(_WHEEL in project_html, f"the project page lists {_WHEEL}")
    check(
        f"/packages/{_WHEEL}" in project_html,
        "the project page links to the file (url_for('pypi.serve_package'))",
    )

    print()
    print("── the removed dependency is gone ──────────────────────────────")
    # `find_spec` is environment state, not source state: a stale venv makes
    # this red on correct code, so the failure names the fix.
    check(importlib.util.find_spec(_REMOVED_MODULE) is None,
          f"{_REMOVED_MODULE} is not importable — run `uv sync` if it lingers in this venv")
    leftover = _removed_imports()
    check(not leftover, f"no source file imports {_REMOVED_MODULE} (found: {leftover})")
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    declared = tomllib.loads(pyproject).get("project", {}).get("dependencies", [])
    check(
        not any(str(dep).lower().startswith(_REMOVED_DIST) for dep in declared),
        f"pyproject.toml no longer declares {_REMOVED_DIST}",
    )
    check(
        any(str(dep).lower().startswith("flask-openapi3") for dep in declared),
        "pyproject.toml declares flask-openapi3",
    )

    print()
    if failures:
        print(f"❌ query-binding check FAILED — {len(failures)} problem(s)")
        return 1
    print("✅ query-binding check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
