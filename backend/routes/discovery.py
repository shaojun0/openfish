"""API discovery surface — how a machine finds out what this server offers.

Four anonymous routes.  They publish the *contract*; they never expose registry
data, so leaving them unauthenticated is safe and is what lets an agent that has
only a base URL bootstrap itself.

    /openapi.json             OpenAPI 3.1 — the authoritative description
    /docs                     the same document as plain HTML (no JS, no CDN)
    /llms.txt                 curated Markdown for language-model agents
    /.well-known/api-catalog  RFC 9727 linkset pointing at all of the above

Every response from this server also advertises the machine-readable description
through a ``Link: rel="service-desc"`` header, so a client that lands on any URL
can find the contract without guessing a path.
"""

from __future__ import annotations

import json

from flask import Blueprint, Response, current_app, request

from openapi.render import render_llms_txt, render_reference_html
from openapi.spec import build_spec

discovery_bp = Blueprint("discovery", __name__)


def _base_url() -> str:
    """Absolute server root, honouring ROUTE_PREFIX and X-Forwarded-*."""
    return request.url_root.rstrip("/")


def _spec() -> dict:
    return build_spec(current_app, base_url=_base_url())


@discovery_bp.after_app_request
def _advertise(response: Response) -> Response:
    """Point every response at the machine-readable description."""
    root = request.script_root.rstrip("/")
    response.headers.setdefault(
        "Link",
        f'<{root}/openapi.json>; rel="service-desc"; type="application/json", '
        f'<{root}/docs>; rel="service-doc"; type="text/html", '
        f'<{root}/llms.txt>; rel="describedby"; type="text/markdown"',
    )
    return response


@discovery_bp.route("/openapi.json")
def openapi_json():
    """The OpenAPI 3.1 document itself.

    Served with the pretty-printed JSON representation because it is read by
    language models as often as by tooling.
    """
    response = Response(json.dumps(_spec(), indent=2), mimetype="application/json")
    response.headers["Cache-Control"] = "no-cache"
    return response


@discovery_bp.route("/docs")
def docs():
    """A JavaScript-free, CDN-free API reference rendered from the spec."""
    return Response(render_reference_html(_spec()), mimetype="text/html")


@discovery_bp.route("/llms.txt")
def llms_txt():
    """Curated Markdown index following the llms.txt v2 layout."""
    response = Response(
        render_llms_txt(_spec(), _base_url()), mimetype="text/markdown"
    )
    response.headers["Cache-Control"] = "no-cache"
    return response


@discovery_bp.route("/.well-known/api-catalog")
def api_catalog():
    """RFC 9727 API catalog — a linkset pointing at the description and docs."""
    root = _base_url()
    document = {
        "linkset": [
            {
                "anchor": f"{root}/openapi.json",
                "service-desc": [
                    {"href": f"{root}/openapi.json", "type": "application/json"}
                ],
                "service-doc": [
                    {"href": f"{root}/docs", "type": "text/html"},
                    {"href": f"{root}/llms.txt", "type": "text/markdown"},
                ],
                "status": [{"href": f"{root}/health", "type": "application/json"}],
            }
        ]
    }
    return Response(
        json.dumps(document, indent=2),
        content_type='application/linkset+json; profile="https://www.rfc-editor.org/info/rfc9727"',
    )
