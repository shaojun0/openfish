"""Build the OpenAPI 3.1 document from the live Flask application.

The document is generated from ``app.url_map`` plus the ``@api_operation``
metadata attached to each view, which makes one direction of drift impossible:
the spec can never advertise an endpoint the server does not serve.  The other
direction — a served endpoint missing from the spec — is reported by
:func:`undocumented_endpoints`, which ``scripts/check_openapi.py`` turns into a
failing check.
"""

from __future__ import annotations

import re
from importlib import metadata as importlib_metadata
from typing import Any, Iterable

from flask import Flask
from pydantic import BaseModel

import schemas as schemas_module
from openapi.registry import operation_of

#: URL prefixes that form the machine contract.  Everything else — the SPA,
#: `/static`, the OAuth redirect endpoints — is deliberately out of scope.
DOCUMENTED_PREFIXES: tuple[str, ...] = (
    "/api/",
    "/simple/",
    "/packages/",
    "/python-builds/",
    "/tools/",
    "/npm/",
    "/health",
)

#: Flask converters that can appear in a rule but are always plain strings on
#: the wire.
_CONVERTER_RE = re.compile(r"<(?:(\w+):)?(\w+)>")

TAGS: list[dict[str, str]] = [
    {"name": "Session", "description": "Who am I, and what may I do?"},
    {"name": "Packages", "description": "Browse and download Python packages."},
    {"name": "API keys", "description": "Issue and revoke the tokens clients authenticate with."},
    {"name": "Administration", "description": "Server-wide aggregates. Requires the admin role."},
    {"name": "Python builds", "description": "Prebuilt CPython mirror consumed by `uv python install`."},
    {"name": "Hub", "description": "Intranet artifact hub — tools catalog, npm scaffold and model routing."},
    {"name": "Upload", "description": "Publish packages, as `twine` does."},
]

SECURITY_SCHEMES: dict[str, dict[str, Any]] = {
    "bearerApiKey": {
        "type": "http",
        "scheme": "bearer",
        "description": (
            "API key issued at `/api-keys`. Send it as "
            "`Authorization: Bearer cpypi_<48 hex chars>`. This is the "
            "recommended scheme for programmatic and agent access.\n\n"
            "A key inherits the role of the user it was issued to. A key created "
            "by an administrator therefore also carries `admin:view` and "
            "`admin:refresh`; check `GET /api/v1/session` to see what a given key "
            "may actually do."
        ),
    },
    "basicAuth": {
        "type": "http",
        "scheme": "basic",
        "description": (
            "HTTP Basic. For `pip`, `uv` and `twine` use the fixed username "
            "`__token__` with the API key as the password. A deployment may "
            "additionally configure a human username/password."
        ),
    },
    "sessionCookie": {
        "type": "apiKey",
        "in": "cookie",
        "name": "session",
        "description": "Browser session established by the OAuth2 login flow.",
    },
}

#: Applied to every operation that does not override `security`.
DEFAULT_SECURITY: list[dict[str, list[str]]] = [
    {"bearerApiKey": []},
    {"basicAuth": []},
    {"sessionCookie": []},
]


def _package_version() -> str:
    try:
        return importlib_metadata.version("cpypiserver")
    except importlib_metadata.PackageNotFoundError:
        return "0.0.0"


def flask_path_to_openapi(rule: str) -> str:
    """``/api/v1/keys/<key_id>`` becomes ``/api/v1/keys/{key_id}``."""
    return _CONVERTER_RE.sub(lambda m: "{" + m.group(2) + "}", rule)


def _path_parameters(rule: str) -> list[dict[str, Any]]:
    return [
        {
            "name": m.group(2),
            "in": "path",
            "required": True,
            "schema": {"type": "string"},
        }
        for m in _CONVERTER_RE.finditer(rule)
    ]


def _operation_id(endpoint: str, method: str) -> str:
    return f"{endpoint.replace('.', '_')}_{method.lower()}"


def _operation(rule, method: str, meta: dict[str, Any]) -> dict[str, Any]:
    op: dict[str, Any] = {
        "summary": meta["summary"],
        "operationId": meta.get("operationId") or _operation_id(rule.endpoint, method),
        "responses": meta["responses"],
    }
    if meta.get("description"):
        op["description"] = meta["description"]
    if meta.get("tags"):
        op["tags"] = meta["tags"]

    parameters = list(meta.get("parameters") or [])
    known = {p["name"] for p in parameters}
    parameters.extend(p for p in _path_parameters(str(rule)) if p["name"] not in known)
    if parameters:
        op["parameters"] = parameters

    if meta.get("requestBody"):
        op["requestBody"] = meta["requestBody"]

    # `None` means "inherit the server default"; `[]` explicitly marks the
    # operation as callable without credentials.
    security = meta.get("security")
    op["security"] = DEFAULT_SECURITY if security is None else security
    return op


def _model_registry() -> dict[str, type[BaseModel]]:
    """Every pydantic model defined in `schemas`, by class name."""
    found: dict[str, type[BaseModel]] = {}
    for name in dir(schemas_module):
        obj = getattr(schemas_module, name)
        if isinstance(obj, type) and issubclass(obj, BaseModel) and obj is not BaseModel:
            found[name] = obj
    return found


def _refs_in(node: Any) -> set[str]:
    """Collect every `#/components/schemas/X` reference in a nested structure."""
    names: set[str] = set()

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            ref = value.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
                names.add(ref.rsplit("/", 1)[-1])
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(node)
    return names


def _build_schemas(paths: dict[str, Any]) -> dict[str, Any]:
    """Emit component schemas for every model the paths reference."""
    models = _model_registry()
    pending = list(_refs_in(paths))
    schemas: dict[str, Any] = {}

    while pending:
        name = pending.pop()
        if name in schemas:
            continue
        model = models.get(name)
        if model is None:
            raise KeyError(
                f"OpenAPI document references schema '{name}', "
                f"but schemas.{name} is not a pydantic model"
            )
        document = model.model_json_schema(
            ref_template="#/components/schemas/{model}", by_alias=True
        )
        # pydantic emits nested models flat in `$defs`; hoist them into the
        # component section where the refs already point.
        for def_name, def_schema in document.pop("$defs", {}).items():
            if def_name not in schemas:
                schemas[def_name] = def_schema
                pending.append(def_name)
        schemas[name] = document

    return schemas


def _iter_documented(app: Flask) -> Iterable[tuple[Any, str, dict[str, Any]]]:
    for rule in app.url_map.iter_rules():
        if not str(rule).startswith(DOCUMENTED_PREFIXES):
            continue
        view = app.view_functions.get(rule.endpoint)
        meta = operation_of(view) if view is not None else None
        if meta is None:
            continue
        for method in sorted((rule.methods or set()) - {"HEAD", "OPTIONS"}):
            yield rule, method, meta


def undocumented_endpoints(app: Flask) -> list[str]:
    """Served machine endpoints that carry no `@api_operation` metadata.

    An empty list means the spec covers the whole contract.  Used by
    ``scripts/check_openapi.py`` so that adding a route without documenting it
    fails the check rather than silently vanishing from `/openapi.json`.
    """
    missing: list[str] = []
    for rule in app.url_map.iter_rules():
        if rule.endpoint == "static" or not str(rule).startswith(DOCUMENTED_PREFIXES):
            continue
        view = app.view_functions.get(rule.endpoint)
        if operation_of(view) is None:
            methods = ",".join(sorted((rule.methods or set()) - {"HEAD", "OPTIONS"}))
            missing.append(f"{methods} {rule}")
    return sorted(missing)


def build_spec(app: Flask, base_url: str = "/") -> dict[str, Any]:
    """Assemble the OpenAPI 3.1 document for *app*.

    Args:
        app: The Flask application to describe.
        base_url: Absolute server URL, normally derived from the incoming
            request so the document stays correct behind a reverse proxy.
    """
    paths: dict[str, Any] = {}
    for rule, method, meta in _iter_documented(app):
        path = flask_path_to_openapi(str(rule))
        paths.setdefault(path, {})[method.lower()] = _operation(rule, method, meta)

    schemas = _build_schemas(paths)

    # Fail loudly rather than emitting a document with dangling references.
    unresolved = sorted(_refs_in(paths) - set(schemas))
    if unresolved:
        raise KeyError(f"unresolved OpenAPI schema references: {unresolved}")

    from config import settings

    return {
        "openapi": "3.1.0",
        "info": {
            "title": f"{settings.server.server_name} API",
            "version": _package_version(),
            "summary": "Self-hosted Python package registry",
            "description": (
                "JSON API for a self-hosted Python package registry, plus the "
                "machine-facing endpoints that `pip`, `uv` and `twine` consume "
                "directly.\n\n"
                "**Authentication.** Create an API key on the `/api-keys` page, "
                "then send it as `Authorization: Bearer <key>`. Package managers "
                "use the same key over HTTP Basic with the username `__token__`.\n\n"
                "Start at `GET /api/v1/session` to discover the calling "
                "identity's role and permissions."
            ),
            "license": {"name": "Proprietary"},
        },
        "servers": [{"url": base_url or "/"}],
        "tags": TAGS,
        "paths": paths,
        "components": {
            "securitySchemes": SECURITY_SCHEMES,
            "schemas": schemas,
        },
        "security": DEFAULT_SECURITY,
    }
