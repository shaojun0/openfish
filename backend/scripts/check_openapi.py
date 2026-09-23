#!/usr/bin/env python
"""Gate: `/openapi.json` must describe exactly what the server actually serves.

Run from the backend directory (`backend/`)::

    python scripts/check_openapi.py

Exits non-zero when:

* a machine-facing endpoint carries no ``@api_operation`` metadata, so it would
  silently vanish from the description;
* a ``$ref`` points at a schema that does not exist;
* two operations share an ``operationId``, which breaks generated clients;
* the document fails OpenAPI 3.1 structural validation — this last step runs
  only when ``openapi-spec-validator`` is installed.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app import app  # noqa: E402
from openapi import build_spec, undocumented_endpoints  # noqa: E402
from openapi.spec import SECURITY_SCHEMES  # noqa: E402

failures: list[str] = []


def fail(message: str) -> None:
    failures.append(message)


def main() -> int:
    # ── 1. Coverage: no served endpoint may be undocumented ──────────
    missing = undocumented_endpoints(app)
    if missing:
        fail("machine endpoints missing @api_operation metadata:")
        for item in missing:
            failures.append(f"    {item}")
    else:
        print("✅ every machine endpoint carries @api_operation metadata")

    spec = build_spec(app, base_url="https://registry.example.invalid")

    # ── 2. Envelope ──────────────────────────────────────────────────
    if spec.get("openapi") != "3.1.0":
        fail(f"expected openapi 3.1.0, got {spec.get('openapi')}")
    if not spec.get("paths"):
        fail("document has no paths")
    if not spec.get("info", {}).get("title"):
        fail("info.title is missing")

    # ── 3. Every $ref resolves ───────────────────────────────────────
    schemas = set(spec.get("components", {}).get("schemas", {}))
    refs: set[str] = set()

    def walk(node) -> None:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str):
                refs.add(ref)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(spec)
    dangling = sorted(r for r in refs if r.rsplit("/", 1)[-1] not in schemas)
    if dangling:
        fail(f"dangling $ref: {dangling}")
    else:
        print(f"✅ all {len(refs)} $ref(s) resolve against {len(schemas)} component schemas")

    # ── 4. Operation hygiene ─────────────────────────────────────────
    operation_ids: dict[str, str] = {}
    operation_count = 0
    for path, entry in spec["paths"].items():
        for method, operation in entry.items():
            if method not in {"get", "post", "put", "patch", "delete"}:
                continue
            operation_count += 1
            where = f"{method.upper()} {path}"

            op_id = operation.get("operationId")
            if not op_id:
                fail(f"{where} has no operationId")
            elif op_id in operation_ids:
                fail(f"duplicate operationId '{op_id}' ({operation_ids[op_id]} and {where})")
            else:
                operation_ids[op_id] = where

            if not operation.get("summary"):
                fail(f"{where} has no summary")
            if not operation.get("responses"):
                fail(f"{where} declares no responses")
            if "security" not in operation:
                fail(f"{where} declares no security (use security=[] for public operations)")

            for requirement in operation.get("security") or []:
                for scheme in requirement:
                    if scheme not in SECURITY_SCHEMES:
                        fail(f"{where} references unknown security scheme '{scheme}'")

            # Path template variables must be declared as parameters.
            declared = {p["name"] for p in operation.get("parameters", []) if p.get("in") == "path"}
            for chunk in path.split("{")[1:]:
                name = chunk.split("}")[0]
                if name not in declared:
                    fail(f"{where} does not declare path parameter '{name}'")

    print(f"✅ {operation_count} operations, {len(operation_ids)} unique operationIds")

    # ── 5. Optional: full structural validation ──────────────────────
    try:
        from openapi_spec_validator import validate  # type: ignore
    except ImportError:
        print("ℹ️  openapi-spec-validator not installed — skipping structural validation")
    else:
        try:
            validate(spec)
        except Exception as exc:  # noqa: BLE001 - the validator raises many types
            fail(f"OpenAPI 3.1 validation failed: {exc}")
        else:
            print("✅ document passes openapi-spec-validator (3.1)")

    # ── Report ───────────────────────────────────────────────────────
    print()
    if failures:
        print("❌ openapi check FAILED")
        for message in failures:
            print("   " + message)
        return 1

    rendered = json.dumps(spec)
    print(f"✅ openapi check passed — {len(spec['paths'])} paths, {len(rendered):,} bytes of JSON")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
