#!/usr/bin/env python
"""Validate live responses against the published contract.

`check_openapi.py` proves the document is internally consistent and covers every
endpoint.  This script proves the *other* half: that the server actually returns
what the document promises.  It calls each documented JSON endpoint and hands
the response to the pydantic model the spec references.

Usage::

    python scripts/check_contract.py --base-url http://127.0.0.1:9090 \
        --api-key cpypi_...

Any endpoint whose declared schema the response violates is reported and the
script exits non-zero.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import requests
from pydantic import BaseModel, TypeAdapter

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import schemas as schemas_module  # noqa: E402

MODELS: dict[str, type[BaseModel]] = {
    name: obj
    for name in dir(schemas_module)
    if isinstance(obj := getattr(schemas_module, name), type)
    and issubclass(obj, BaseModel)
    and obj is not BaseModel
}

failures: list[str] = []
checks = 0


def resolve(schema: dict[str, Any]) -> type[BaseModel]:
    ref = schema["$ref"]
    name = ref.rsplit("/", 1)[-1]
    if name not in MODELS:
        raise KeyError(f"spec references unknown model {name}")
    return MODELS[name]


def validate(schema: dict[str, Any], data: Any) -> str:
    """Validate *data* against a JSON Schema fragment from the spec."""
    global checks
    if "$ref" in schema:
        model = resolve(schema)
        model.model_validate(data)
        checks += 1
        return model.__name__
    if schema.get("type") == "array":
        model = resolve(schema["items"])
        TypeAdapter(list[model]).validate_python(data)
        checks += 1
        return f"list[{model.__name__}]×{len(data)}"
    checks += 1
    return "untyped"


def json_schema_of(operation: dict[str, Any], status: str = "200") -> dict[str, Any] | None:
    responses = operation.get("responses") or {}
    response = responses.get(status)
    if not response:
        return None
    for media, document in (response.get("content") or {}).items():
        if media == "application/json" and "schema" in document:
            return document["schema"]
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--api-key", required=True)
    args = parser.parse_args()

    base = args.base_url.rstrip("/")
    auth = {"Authorization": f"Bearer {args.api_key}"}

    # ── Discovery surface must be reachable without credentials ──────
    for path, expect in [
        ("/openapi.json", "application/json"),
        ("/docs", "text/html"),
        ("/llms.txt", "text/markdown"),
        ("/.well-known/api-catalog", "application/linkset+json"),
    ]:
        response = requests.get(f"{base}{path}", timeout=20)
        if response.status_code != 200:
            failures.append(f"GET {path} (anonymous) -> {response.status_code}, expected 200")
        elif expect not in response.headers.get("Content-Type", ""):
            failures.append(
                f"GET {path} Content-Type is {response.headers.get('Content-Type')!r}, expected {expect!r}"
            )
        else:
            print(f"✅ GET {path:<26} 200  {expect}  ({len(response.content):,} bytes)")

    link = requests.get(f"{base}/health", timeout=20).headers.get("Link", "")
    if 'rel="service-desc"' in link:
        print("✅ every response advertises the spec via Link: rel=\"service-desc\"")
    else:
        failures.append(f"Link header missing service-desc relation: {link!r}")

    spec = requests.get(f"{base}/openapi.json", timeout=20).json()

    # ── Unauthenticated calls to secured operations must 401 ─────────
    for path in ("/api/v1/keys", "/api/v1/packages", "/api/v1/admin/stats"):
        response = requests.get(f"{base}{path}", timeout=20, headers={"Accept": "application/json"})
        if response.status_code != 401:
            failures.append(
                f"GET {path} without credentials -> {response.status_code}, expected 401"
            )
        elif "application/json" not in response.headers.get("Content-Type", ""):
            failures.append(
                f"GET {path} unauthenticated returned "
                f"{response.headers.get('Content-Type')!r}, expected JSON (an HTML "
                f"redirect would be followed silently by XHR)"
            )
        else:
            print(f"✅ GET {path:<26} 401 JSON when anonymous")

    # ── Validate live JSON responses against the declared schemas ────
    print()
    print("live response validation")

    # Resolve a real package name so the parameterised paths can be called.
    package_route = spec["paths"].get("/api/v1/packages", {}).get("get", {})
    schema = json_schema_of(package_route)
    packages: list[dict[str, Any]] = []
    if schema is not None:
        response = requests.get(f"{base}/api/v1/packages", headers=auth, timeout=20)
        if response.status_code == 200:
            packages = response.json()
            label = validate(schema, packages)
            print(f"  GET /api/v1/packages                    -> {label}")
        else:
            failures.append(f"GET /api/v1/packages -> {response.status_code}")

    key_id = None
    if packages:
        sample = packages[0]["name"]
        for path, operation in spec["paths"].items():
            if "{package_name}" not in path or "get" not in operation:
                continue
            concrete = path.replace("{package_name}", sample)
            schema = json_schema_of(operation)
            if schema is None:
                continue
            response = requests.get(f"{base}{concrete}", headers=auth, timeout=30)
            if response.status_code != 200:
                failures.append(f"GET {concrete} -> {response.status_code}")
                continue
            try:
                label = validate(schema, response.json())
            except Exception as exc:  # noqa: BLE001
                failures.append(f"GET {concrete} violates {operation['operationId']}: {exc}")
            else:
                print(f"  GET {concrete:<38} -> {label}")

    # A key is needed to exercise the key-scoped operations.
    response = requests.get(f"{base}/api/v1/keys", headers=auth, timeout=20)
    op = spec["paths"]["/api/v1/keys"]["get"]
    schema = json_schema_of(op)
    if response.status_code == 200 and schema is not None:
        keys = response.json()
        print(f"  GET /api/v1/keys                        -> {validate(schema, keys)}")
        if keys:
            key_id = keys[0]["id"]

    if key_id:
        concrete = f"/api/v1/keys/{key_id}/stats"
        op = spec["paths"]["/api/v1/keys/{key_id}/stats"]["get"]
        schema = json_schema_of(op)
        response = requests.get(f"{base}{concrete}", headers=auth, timeout=20)
        if response.status_code == 200 and schema is not None:
            print(f"  GET {concrete:<38} -> {validate(schema, response.json())}")

    for path in ("/api/v1/admin/stats", "/health", "/python-builds/health"):
        operation = spec["paths"].get(path, {}).get("get")
        if operation is None:
            continue
        schema = json_schema_of(operation)
        response = requests.get(f"{base}{path}", headers=auth, timeout=30)
        if response.status_code != 200:
            failures.append(f"GET {path} -> {response.status_code}")
            continue
        if schema is None:
            continue
        try:
            label = validate(schema, response.json())
        except Exception as exc:  # noqa: BLE001
            failures.append(f"GET {path} violates {operation['operationId']}: {exc}")
        else:
            print(f"  GET {path:<38} -> {label}")

    # ── llms.txt should point at things that exist ───────────────────
    body = requests.get(f"{base}/llms.txt", timeout=20).text
    for required in ("## Authentication", "openapi.json", "/simple/", "Bearer"):
        if required not in body:
            failures.append(f"llms.txt is missing {required!r}")

    print()
    if failures:
        print(f"❌ contract check FAILED ({len(failures)} problem(s))")
        for message in failures:
            print("   " + message)
        return 1

    print(f"✅ contract check passed — {checks} live response(s) validated against the spec")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
