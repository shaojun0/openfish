"""Small builders that keep per-view OpenAPI metadata readable.

Without these, every `@api_operation` would repeat the same nesting of
``{"description": ..., "content": {"application/json": {"schema": {"$ref": ...}}}}``.
"""

from __future__ import annotations

from typing import Any

ERROR_MODEL = "ErrorResponse"

_ERROR_DESCRIPTIONS = {
    "400": "Malformed request",
    "401": "Authentication required, or the credential was rejected",
    "403": "Authenticated, but the role lacks the required permission",
    "404": "Not found",
    "409": "Conflict — the resource already exists",
    "413": "Payload too large",
    "503": "A required subsystem is unavailable",
}


def ref(model: str) -> dict[str, str]:
    """A JSON Schema reference to a component model."""
    return {"$ref": f"#/components/schemas/{model}"}


def array_of(model: str) -> dict[str, Any]:
    """A JSON Schema array whose items are a component model."""
    return {"type": "array", "items": ref(model)}


def _schema(model: Any) -> Any:
    return ref(model) if isinstance(model, str) else model


def json_body(model: Any = None, example: Any = None) -> dict[str, Any]:
    """A `content` mapping holding a single `application/json` entry.

    *model* may be a component name, a raw JSON Schema dict, or ``None``.
    """
    media: dict[str, Any] = {}
    schema = _schema(model)
    if schema is not None:
        media["schema"] = schema
    if example is not None:
        media["example"] = example
    return {"application/json": media}


def ok(description: str = "Success", model: Any = None) -> dict[str, Any]:
    """A successful JSON response."""
    return {"description": description, "content": json_body(model)}


def binary(description: str = "File contents") -> dict[str, Any]:
    """A raw file download."""
    return {"description": description, "content": {"application/octet-stream": {}}}


def html(description: str = "HTML document") -> dict[str, Any]:
    return {"description": description, "content": {"text/html": {}}}


def errors(*codes: str) -> dict[str, Any]:
    """Standard JSON error responses for the given status codes."""
    return {
        code: ok(_ERROR_DESCRIPTIONS.get(code, "Error"), ERROR_MODEL) for code in codes
    }
