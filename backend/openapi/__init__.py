"""Machine-readable API description.

`registry` holds the declarative metadata attached to each view; `spec` turns
the live Flask url_map plus that metadata into an OpenAPI 3.1 document;
`render` turns the same document into plain HTML and Markdown for humans and
language models.
"""

from openapi.registry import api_operation, operation_of
from openapi.responses import array_of, binary, errors, html, json_body, ok, ref
from openapi.spec import build_spec, undocumented_endpoints

__all__ = [
    "api_operation",
    "operation_of",
    "build_spec",
    "undocumented_endpoints",
    "array_of",
    "binary",
    "errors",
    "html",
    "json_body",
    "ok",
    "ref",
]
