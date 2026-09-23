#!/usr/bin/env python
"""Gate: request input is *bound* as a view parameter — or declared an exception.

Run from the backend directory (`backend/`)::

    python scripts/check_request_binding.py

``routes/npm.py`` used to read ``npm publish``'s document with
``request.get_json(silent=True, force=True)``, and it was not alone: five other
routes parsed a body by hand while ``schemas.py`` already carried the model their
own ``/openapi.json`` entry advertised.  A hand-read request input is invisible to
the contract, skips validation, and each one is a different answer to "what does
this route accept".  This gate is what keeps the migration finished, in two
halves:

1. **Static.** Every function under ``routes/`` that touches ``request.args`` /
   ``request.form`` / ``request.files`` / ``request.get_json`` / ``request.json``
   / ``request.values`` / ``request.get_data`` must be listed in ``EXEMPT`` below
   with a reason.  A new hand-read fails the gate until somebody either binds it
   or writes down why it cannot be bound.  It also holds the *shape* of a bound
   view: a reserved binder parameter (``body`` / ``query`` / ``path`` / ``form``)
   typed with a ``schemas`` model must have ``@validate_request()``, the
   ``require_*`` guard must be written *above* it (so an unauthorized caller is
   answered 401/403 and never by the binder), and the view must be registered by
   a per-verb decorator on an ``APIBlueprint`` — the only form that installs the
   binding wrapper.
2. **Runtime.** One deliberately malformed request for every bound model that
   *can* be malformed — a model of plain strings (``NpmSearchQuery``'s
   ``?text=&size=&from=``, the Agent-Hub list filters) has no invalid value, by
   design — asserting the binder answers ``400`` with the error filed under the
   right location key and ``X-Content-Type-Options: nosniff``; the
   guard-before-binder order on the routes where it matters; and the two
   semantics a model can silently destroy: ``exclude_unset`` (absent means
   "keep", null means "default") and a body-size ceiling that has to run before
   the binder reads anything.

Out of scope, deliberately
--------------------------
``request.headers`` and ``request.content_length`` are *not* hand-reads to bind:
``Accept``/``Range`` are negotiated or passed through to an upstream
(`docker manifest`, `npm` abbreviated packuments), and ``Content-Length`` is what
a size gate has to read *before* the binder exists.  Likewise the browser and
protocol flows in ``EXEMPT`` below — OAuth redirects, the HTML forms, the
webhook's raw-body HMAC, the device flow's own error envelope — are not oversights
but decisions, and each one carries its reason.
"""

from __future__ import annotations

import ast
import base64
import importlib
import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
os.chdir(REPO_ROOT)

# ── A throwaway deployment, set before the app is imported ───────────
_TMP = Path(tempfile.mkdtemp(prefix="openfish-binding-gate-"))
for _name, _sub in (
    ("API_KEYS_FILE", "gate.db"),
    ("TOOLS_DIR", "tools"),
    ("DOCS_DIR", "docs"),
    ("DOCKER_DIR", "docker"),
    ("DOCKER_CACHE_DIR", "docker-cache"),
    ("DEBIAN_DIR", "debian"),
    ("DEBIAN_OFFLINE_DIR", "debian-offline"),
    ("NPM_DIR", "npm"),
    ("NPM_CACHE_DIR", "npm-cache"),
):
    _path = _TMP / _sub
    if _name.endswith("_DIR"):
        _path.mkdir(parents=True, exist_ok=True)
    os.environ[_name] = str(_path)
os.environ["AUTH_USERNAME"] = "dev"
os.environ["AUTH_ASSERT"] = "devpass"
os.environ["ADMIN_USERS"] = '["dev"]'
os.environ["OAUTH2_INTROSPECT_URL"] = ""
os.environ["OAUTH2_AUTHORIZE_URL"] = ""
os.environ["NPM_PROXY_ENABLED"] = "false"

#: Attributes that read request *input*.  `headers` and `content_length` are
#: deliberately absent — see the module docstring.
REQUEST_READERS = frozenset({
    "args", "form", "files", "values", "get_data", "get_json", "json",
})

#: The parameter names flask-openapi3 looks up in a view signature.
BINDER_PARAMS = frozenset({"body", "query", "path", "form", "header", "cookie", "raw"})

#: Decorators that register a route (flask-openapi3 spells them per verb).
ROUTE_VERBS = frozenset({"get", "post", "put", "patch", "delete"})

#: Decorators that gate a route.  A guard must be written *above*
#: `@validate_request()`: decorators apply bottom-up, so the one nearest the
#: function runs first, and a guard placed below the binder would let an
#: unauthenticated caller learn whether a body is valid.
GUARD_NAMES = frozenset({
    "require_auth", "require_permission", "require_admin",
    "require_package_read", "require_package_write",
})

#: Functions that still read request input by hand — every one a decision, never
#: an oversight.  Keyed `<module>.<function>` *of the function that touches
#: ``request``*, which is the helper itself when a view delegates (``_artifact_text``),
#: because that is where a future edit would reintroduce the hand-read.  The
#: reason is the whole point of the entry, and an entry whose function stops
#: reading the request fails the gate so the table cannot rot.
EXEMPT: dict[str, str] = {
    # ── Browser flows: the answer is an HTML page or a redirect ──────
    "auth_routes.auth_login": (
        "`?next=` is a browser redirect target, not a request field: a model "
        "would answer a malformed one with a JSON 400 instead of starting the "
        "login flow"
    ),
    "auth_routes.auth_callback": (
        "`?code=`/`?state=` come back from the OAuth provider; the flow answers "
        "with redirects and a session, so there is no JSON error envelope to file"
    ),
    "device.authorize": (
        "the device-authorization landing page renders HTML for an anonymous "
        "visitor and bounces them through login; `?user_code=` is a display value"
    ),
    "device.approve_device": (
        "the approval form posts `user_code` as an HTML form field and answers "
        "HTML; the binding envelope would replace the page it renders on error"
    ),
    "device.device_token": (
        "this route's 400 body *is* the device-flow protocol "
        "(`DeviceTokenError`: `{\"error\": …, \"error_description\": …}`, which "
        "the DSH plugin renders); binding `DeviceTokenRequest` would answer a "
        "malformed poll with the generic `validation_error` envelope instead"
    ),
    # ── Machine flows whose body is not one shape ────────────────────
    "repo_webhook.forgejo_webhook": (
        "the HMAC is verified over the *raw* bytes before anything is parsed "
        "(`request.get_data(cache=True)`); a binder consumes the stream and "
        "parses first, which would destroy the signature check"
    ),
    "debian._artifact_text": (
        "one value accepted three ways — a same-named file part, a same-named "
        "text field, or the raw body — which no single model describes; binding "
        "it would silently drop two of the three accepted forms"
    ),
    "debian._tri_flag": (
        "a tri-state flag that distinguishes *absent* from *false* and reads form "
        "**or** query depending on how the caller sent it; a model pins one "
        "location and collapses the tri-state"
    ),
    "debian.debian_offline_plan": (
        "its body goes through `_artifact_text` (see that entry), and `only` is "
        "accepted from the form **or** the query string"
    ),
    "docs.docs_create": (
        "the body is a `title` text field and an *optional* `file` part — one "
        "alone is a supported call, and a required `FileStorage` field would "
        "refuse every title-only create"
    ),
    # ── Content negotiation: the caller is a package manager ─────────
    "hub_common.wants_json": (
        "the shared `?format=json` / `Accept` negotiation every ecosystem index "
        "uses; it is a helper, not a route input, and each caller decides whether "
        "a bad value should be a 400 (pypi) or HTML (everything else)"
    ),
}

failures: list[str] = []
checks = 0

#: Filled by `check_static()`: every model a bound view names, by view.  The
#: runtime half compares it against the envelope location table in `app.py`.
_BOUND_MODELS: dict[str, list[str]] = {}


def check(ok: bool, label: str) -> None:
    global checks
    checks += 1
    print(("   ✅ " if ok else "   ✗ ") + label)
    if not ok:
        failures.append(label)


def section(title: str) -> None:
    print(f"\n── {title} " + "─" * max(0, 60 - len(title)))


# ══════════════════════════════════════════════════════════════════════
#  Static: what reads the request, and how a bound view must look
# ══════════════════════════════════════════════════════════════════════

def _decorator_name(node: ast.expr) -> str:
    target = node.func if isinstance(node, ast.Call) else node
    parts: list[str] = []
    while isinstance(target, ast.Attribute):
        parts.append(target.attr)
        target = target.value
    if isinstance(target, ast.Name):
        parts.append(target.id)
    return ".".join(reversed(parts))


def _leaf(name: str) -> str:
    return name.rsplit(".", 1)[-1]


def _annotation_name(annotation: ast.expr | None) -> str | None:
    return annotation.id if isinstance(annotation, ast.Name) else None


def _model_names() -> set[str]:
    """The pydantic model names `schemas` defines — what the binder can accept."""
    import pydantic

    schemas_module = importlib.import_module("schemas")
    return {
        name
        for name in dir(schemas_module)
        if isinstance(getattr(schemas_module, name), type)
        and issubclass(getattr(schemas_module, name), pydantic.BaseModel)
        and getattr(schemas_module, name) is not pydantic.BaseModel
    }


def _request_reads(func: ast.FunctionDef) -> list[str]:
    """The request-input attributes *func* touches, sorted and de-duplicated."""
    found: set[str] = set()
    for node in ast.walk(func):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "request"
            and node.attr in REQUEST_READERS
        ):
            found.add(node.attr)
    return sorted(found)


def scan_routes() -> tuple[dict[str, list[str]], list[str], dict[str, list[str]]]:
    """``(hand_reads, style_problems, bound_models)`` for every route module.

    A parameter only counts as a *binder* parameter when the function is a view
    (it carries a route decorator) **and** its annotation names a ``schemas``
    model.  ``path``, ``body``, ``query`` and ``form`` are ordinary words — this
    tree is full of helpers that take a ``path: str`` or a ``body: bytes`` — and
    treating those as bindings was exactly the false positive this rule removes.
    """
    hand_reads: dict[str, list[str]] = {}
    style: list[str] = []
    bound: dict[str, list[str]] = {}
    models = _model_names()

    for path in sorted((REPO_ROOT / "routes").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            key = f"{path.stem}.{node.name}"
            where = f"{path.relative_to(REPO_ROOT)}:{node.lineno}"

            reads = _request_reads(node)
            if reads:
                hand_reads[key] = reads

            names = [_decorator_name(d) for d in node.decorator_list]
            is_view = any(
                n.endswith(".route") or _leaf(n) in ROUTE_VERBS for n in names
            )
            params = {
                a.arg: _annotation_name(a.annotation)
                for a in node.args.args
                if a.arg in BINDER_PARAMS
            }
            params = {name: model for name, model in params.items() if model in models}
            if not is_view or not params:
                continue

            validate_at = [i for i, n in enumerate(names) if _leaf(n) == "validate_request"]
            guard_at = [i for i, n in enumerate(names) if _leaf(n) in GUARD_NAMES]

            for model in params.values():
                bound.setdefault(model, []).append(node.name)

            if not validate_at:
                style.append(
                    f"{where} {node.name}() — binds {sorted(params)} but has no "
                    f"`@validate_request()`; add it below the guard, or the binder "
                    f"installed by the per-verb decorator runs before the guard"
                )
            elif guard_at and min(guard_at) > min(validate_at):
                style.append(
                    f"{where} {node.name}() — the guard is written *below* "
                    f"`@validate_request()`, so the binder runs first and an "
                    f"unauthorized caller is answered 400 instead of 401/403"
                )
            if not any(_leaf(n) in ROUTE_VERBS for n in names):
                style.append(
                    f"{where} {node.name}() — a bound view must be registered by a "
                    f"per-verb decorator on an `APIBlueprint`, or the binding "
                    f"wrapper is never installed"
                )

    return hand_reads, style, bound


def check_static() -> None:
    hand_reads, style, bound = scan_routes()

    print()
    print("── every hand-read is a declared exception ─────────────────────")
    unaccounted = sorted(key for key in hand_reads if key not in EXEMPT)
    for key in unaccounted:
        print(f"   ✗ {key} reads {hand_reads[key]} by hand — bind it as a view "
              f"parameter, or add it to EXEMPT with the reason it cannot be")
    check(
        not unaccounted,
        f"no unaccounted hand-read (checked {len(hand_reads)} reader(s), "
        f"{len(EXEMPT)} exception(s))",
    )

    rotted = sorted(key for key in EXEMPT if key not in hand_reads)
    check(
        not rotted,
        f"every EXEMPT entry still reads the request (stale: {rotted})" if rotted
        else "no stale EXEMPT entry",
    )

    print()
    print("── and every bound view has the shape the binder needs ─────────")
    for problem in style:
        print(f"   ✗ {problem}")
    check(not style, f"{len(bound)} bound model(s), no shape problem" if not style
          else f"{len(style)} shape problem(s)")

    # The runtime half needs the same map: a model bound by a view is what the
    # envelope table has to carry.  Stash it for `check_envelope_table`.
    global _BOUND_MODELS
    _BOUND_MODELS = bound


# ══════════════════════════════════════════════════════════════════════
#  Runtime: the binder answers, the guard goes first, semantics survive
# ══════════════════════════════════════════════════════════════════════

#: One deliberately malformed request per bound model: `(label, method, path,
#: request kwargs, location)`.  `pypi`'s three models are already pinned by
#: `scripts/check_query_binding.py` (and their 403 ordering by
#: `scripts/check_rbac.py`), so they are not repeated here.
MALFORMED: list[tuple[str, str, str, dict, str]] = [
    ("npm publish document", "put", "/npm/binding-probe", {"json": []}, "body_params"),
    ("API key body", "post", "/api/v1/keys", {"json": {}}, "body_params"),
    ("role body", "post", "/api/v1/admin/roles", {"json": {}}, "body_params"),
    ("permission list", "put", "/api/v1/admin/roles/1/permissions",
     {"json": {"permissions": "package:read"}}, "body_params"),
    ("role grant", "post", "/api/v1/admin/users/1/roles", {"json": {}}, "body_params"),
    ("superuser flag", "put", "/api/v1/admin/users/1/superuser", {"json": {}}, "body_params"),
    ("account list query", "get", "/api/v1/admin/users?limit=abc", {}, "query_params"),
    ("tool upload", "post", "/api/v1/tools",
     {"data": {}, "content_type": "multipart/form-data"}, "form_params"),
    ("model route body", "post", "/api/v1/models", {"json": []}, "body_params"),
    ("model probe draft", "post", "/api/v1/models/probe", {"json": []}, "body_params"),
    ("docker artifact upload", "post", "/api/v1/docker",
     {"data": {}, "content_type": "multipart/form-data"}, "form_params"),
    ("debian bundle import", "post", "/debian/offline/import",
     {"data": {}, "content_type": "multipart/form-data"}, "form_params"),
    ("docs content", "put", "/api/v1/docs/python/binding-probe", {"json": {}}, "body_params"),
    ("docs asset upload", "post", "/api/v1/docs/python/binding-probe/assets",
     {"data": {}, "content_type": "multipart/form-data"}, "form_params"),
    ("repo body", "post", "/api/v1/repos", {"json": []}, "body_params"),
    ("repo import body", "post", "/api/v1/repos/import", {"json": []}, "body_params"),
    ("runner patch body", "patch", "/api/v1/repos/binding/probe/runner", {"json": []},
     "body_params"),
    ("runner credential body", "put", "/api/v1/repos/binding/probe/runner/credential",
     {"json": []}, "body_params"),
    ("agent task body", "post", "/api/v1/agent/tasks", {"json": []}, "body_params"),
    ("finding decision body", "post", "/api/v1/findings/1/decide", {"json": []},
     "body_params"),
    ("review policy body", "put", "/api/v1/policies/binding-probe", {"json": []},
     "body_params"),
]

#: Anonymous requests whose body is *also* invalid: the guard must answer first.
#: A binder that ran before `@require_permission` would answer 400 and leak
#: whether the body was well-formed to a caller with no credential at all.
GUARD_FIRST: list[tuple[str, str, str, dict]] = [
    ("PUT /npm/<pkg> (publish)", "put", "/npm/guard-probe", {"json": []}),
    ("POST /api/v1/keys", "post", "/api/v1/keys", {"json": {}}),
    ("POST /api/v1/models", "post", "/api/v1/models", {"json": []}),
    ("POST /api/v1/tools", "post", "/api/v1/tools",
     {"data": {}, "content_type": "multipart/form-data"}),
    ("POST /api/v1/repos", "post", "/api/v1/repos", {"json": []}),
]


def check_envelope_table(app_module) -> None:
    """The envelope location table must cover exactly the models views bind."""
    print()
    print("── the error envelope files each model under its location ──────")
    table = getattr(app_module, "_BOUND_MODEL_LOCATION", {})
    schemas_module = importlib.import_module("schemas")
    import pydantic

    locations = {"query_params", "path_params", "form_params", "body_params"}
    unknown = sorted(
        f"{name}={where}" for name, where in table.items() if where not in locations
    )
    check(not unknown, f"every location is a known key (bad: {unknown})" if unknown
          else f"{len(table)} model(s) filed under a known location")

    missing = sorted(set(_BOUND_MODELS) - set(table))
    check(
        not missing,
        f"every bound model is in the table (missing: {missing})" if missing
        else "every bound model is in the table",
    )

    not_a_model, unbound = [], []
    for name in sorted(table):
        model = getattr(schemas_module, name, None)
        if not (isinstance(model, type) and issubclass(model, pydantic.BaseModel)):
            not_a_model.append(name)
        elif name not in _BOUND_MODELS:
            unbound.append(name)
    check(not not_a_model, f"every table entry is a schemas model (not: {not_a_model})"
          if not_a_model else "every table entry is a schemas model")
    check(
        not unbound,
        f"every table entry is bound by a view (unbound: {unbound})" if unbound
        else "every table entry is bound by a view",
    )


def check_malformed(client, auth) -> None:
    print()
    print("── a malformed request is answered by the binder ───────────────")
    for label, method, path, kwargs, where in MALFORMED:
        response = getattr(client, method)(path, headers=auth, **kwargs)
        body = response.get_json(silent=True) or {}
        filed = (body.get("validation_error") or {}).get(where)
        ok = (
            response.status_code == 400
            and isinstance(filed, list)
            and bool(filed)
            and response.headers.get("X-Content-Type-Options") == "nosniff"
        )
        check(ok, f"{label} -> 400 {where} + nosniff (got {response.status_code}, "
                  f"keys {sorted(body)})")


def check_guard_first(app_module, auth) -> None:
    print()
    print("── the guard runs before the binder ────────────────────────────")
    anonymous = app_module.app.test_client()
    for label, method, path, kwargs in GUARD_FIRST:
        response = getattr(anonymous, method)(path, **kwargs)
        check(
            response.status_code in (401, 403),
            f"anonymous {label} with an invalid body -> 401/403 (got {response.status_code})",
        )


def check_semantics(client, auth) -> None:
    """The two things a model can silently destroy: absent≠null, and a ceiling."""
    print()
    print("── semantics a bound body must not lose ────────────────────────")

    # `services.model_routes` reads `payload.get(field, existing[field])`, so an
    # *absent* field means "keep" while an explicit null means "default".  Both
    # must survive the model dump (`exclude_unset=True`).
    created = client.post("/api/v1/models", headers=auth, json={
        "name": "binding-probe", "provider": "openai", "kind": "embedding",
        "base_url": "http://127.0.0.1:1", "description": "binding gate",
    })
    kept = client.put("/api/v1/models/binding-probe", headers=auth, json={
        "name": "binding-probe", "base_url": "http://127.0.0.1:1",
        "description": "binding gate 2",
    })
    kept_kind = ((kept.get_json(silent=True) or {}).get("route") or {}).get("kind")
    check(
        created.status_code == 201 and kept_kind == "embedding",
        f"an omitted field keeps the stored value (kind={kept_kind}, want 'embedding')",
    )
    nulled = client.put("/api/v1/models/binding-probe", headers=auth, json={
        "name": "binding-probe", "kind": None,
        "base_url": "http://127.0.0.1:1", "description": "binding gate 3",
    })
    null_kind = ((nulled.get_json(silent=True) or {}).get("route") or {}).get("kind")
    check(
        null_kind == "chat",
        f"an explicit null means the protocol default (kind={null_kind}, want 'chat')",
    )

    # The 64 KiB ceiling has to answer before the binder parses: this body is not
    # JSON at all, so a binder that ran first would file it under body_params.
    oversized = client.post("/api/v1/models", headers=auth,
                            data=b" " * (70 * 1024), content_type="text/plain")
    check(
        oversized.status_code == 400 and "过大" in str((oversized.get_json(silent=True) or {}).get("error")),
        f"an oversized model-route body is refused before parsing (got {oversized.status_code})",
    )

    # A *text* field named like the file part reaches a required FileStorage model
    # as a plain `str`; the view's `getattr(upload, "filename", None)` is what
    # turns that into its 400 instead of an AttributeError 500.
    as_text = client.post(
        "/api/v1/tools", headers=auth,
        data={"file": "not-a-part", "category": "probe"},
        content_type="multipart/form-data",
    )
    check(
        as_text.status_code == 400,
        f"a text field where the file part belongs -> 400 (got {as_text.status_code})",
    )


def main() -> int:
    check_static()

    import app as app_module

    AUTH = {"Authorization": "Basic " + base64.b64encode(b"dev:devpass").decode("ascii")}
    client = app_module.app.test_client()

    check_envelope_table(app_module)
    check_malformed(client, AUTH)
    check_guard_first(app_module, AUTH)
    check_semantics(client, AUTH)

    print()
    if failures:
        print(f"❌ request-binding check FAILED — {len(failures)} of {checks} checks failed")
        for message in failures:
            print("   " + message)
        return 1
    print(f"✅ request-binding check passed — {checks} checks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
