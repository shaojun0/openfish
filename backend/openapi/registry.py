"""Declarative OpenAPI metadata, attached next to the view it describes.

`spec.build_spec` walks `app.url_map` and reads this metadata back off each view
function, so documentation lives beside the code it documents and cannot
describe a route that does not exist.

Placement matters: put `@api_operation` *below* the auth decorators, so
`functools.wraps` copies the metadata onto the final wrapper that Flask
actually registers::

    @bp.route("/keys")
    @require_auth()
    @api_operation(summary="List API keys", responses={...})
    def list_keys(): ...
"""

from __future__ import annotations

from typing import Any, Callable

_META_ATTR = "__openapi_operation__"


def api_operation(
    *,
    summary: str,
    description: str | None = None,
    tags: list[str] | None = None,
    parameters: list[dict[str, Any]] | None = None,
    request_body: dict[str, Any] | None = None,
    responses: dict[str, Any] | None = None,
    security: list[dict[str, list[str]]] | None = None,
    operation_id: str | None = None,
) -> Callable:
    """Attach OpenAPI operation metadata to a view function.

    Args:
        summary: One-line description shown in listings.
        description: Longer Markdown description.
        tags: Grouping tags; also used as section headings by the renderers.
        parameters: OpenAPI parameter objects. Path parameters discovered in the
            rule are added automatically.
        request_body: OpenAPI requestBody object.
        responses: Mapping of status code to OpenAPI response object. Defaults
            to a bare `200 OK`.
        security: Security requirement list. ``None`` (the default) means "use
            the server-wide default"; pass ``[]`` to mark the operation public.
        operation_id: Stable id for generated clients; derived from the endpoint
            and method when omitted.
    """

    def decorator(fn: Callable) -> Callable:
        setattr(
            fn,
            _META_ATTR,
            {
                "summary": summary,
                "description": description,
                "tags": tags or [],
                "parameters": parameters or [],
                "requestBody": request_body,
                "responses": responses or {"200": {"description": "Success"}},
                "security": security,
                "operationId": operation_id,
            },
        )
        return fn

    return decorator


def operation_of(fn: Any) -> dict[str, Any] | None:
    """Return the metadata attached to *fn*, or ``None``."""
    return getattr(fn, _META_ATTR, None)
