"""Render the OpenAPI document as plain HTML and as ``llms.txt`` Markdown.

Neither renderer uses JavaScript or a CDN.  A package registry is often
deployed on an isolated network where a CDN is unreachable, and language models
read plain, semantic markup far more reliably than a rendered single-page app.
"""

from __future__ import annotations

import html
from typing import Any

_METHOD_ORDER = ("get", "post", "put", "patch", "delete")


# ── Small helpers ────────────────────────────────────────────────────

def _esc(value: Any) -> str:
    return html.escape(str(value))


def _ref_name(ref: str) -> str:
    return ref.rsplit("/", 1)[-1]


def _schema_label(schema: Any) -> str:
    if not isinstance(schema, dict):
        return ""
    if "$ref" in schema:
        return _ref_name(schema["$ref"])
    kind = schema.get("type")
    if kind == "array":
        return f"{_schema_label(schema.get('items'))}[]"
    if kind == "object" and "additionalProperties" in schema:
        return f"map[{_schema_label(schema['additionalProperties'])}]"
    return str(kind or "")


def _ordered_paths(spec: dict) -> list[tuple[str, dict]]:
    return sorted(spec.get("paths", {}).items())


def _operations(spec: dict):
    """Yield ``(path, method, operation)`` in a stable order."""
    for path, entry in _ordered_paths(spec):
        for method in _METHOD_ORDER:
            if method in entry:
                yield path, method, entry[method]


# ── HTML reference ───────────────────────────────────────────────────

def _render_parameters(operation: dict) -> str:
    parameters = operation.get("parameters") or []
    if not parameters:
        return ""
    rows = []
    for param in parameters:
        schema = param.get("schema") or {}
        rows.append(
            "<tr>"
            f"<td><code>{_esc(param.get('name'))}</code></td>"
            f"<td>{_esc(param.get('in'))}</td>"
            f"<td>{'yes' if param.get('required') else 'no'}</td>"
            f"<td>{_esc(_schema_label(schema))}</td>"
            f"<td>{_esc(param.get('description', ''))}</td>"
            "</tr>"
        )
    return (
        "<h4>Parameters</h4>"
        "<table><thead><tr><th>Name</th><th>In</th><th>Required</th>"
        "<th>Type</th><th>Description</th></tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table>"
    )


def _render_request_body(operation: dict) -> str:
    body = operation.get("requestBody")
    if not body:
        return ""
    content = body.get("content", {})
    parts = ["<h4>Request body</h4>"]
    if body.get("description"):
        parts.append(f"<p>{_esc(body['description'])}</p>")
    for media, media_doc in content.items():
        label = _schema_label(media_doc.get("schema"))
        parts.append(
            f"<p><code>{_esc(media)}</code>"
            + (f" — <code>{_esc(label)}</code>" if label else "")
            + (" <em>(required)</em>" if body.get("required") else "")
            + "</p>"
        )
    return "".join(parts)


def _render_responses(operation: dict) -> str:
    responses = operation.get("responses") or {}
    rows = []
    for status, response in responses.items():
        content = (response or {}).get("content", {})
        media = ", ".join(content.keys())
        labels = ", ".join(
            filter(None, (_schema_label(d.get("schema")) for d in content.values()))
        )
        rows.append(
            "<tr>"
            f"<td><code>{_esc(status)}</code></td>"
            f"<td>{_esc((response or {}).get('description', ''))}</td>"
            f"<td>{_esc(media)}</td>"
            f"<td>{_esc(labels)}</td>"
            "</tr>"
        )
    return (
        "<h4>Responses</h4>"
        "<table><thead><tr><th>Status</th><th>Description</th>"
        "<th>Media type</th><th>Schema</th></tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table>"
    )


def _render_security(operation: dict, schemes: dict) -> str:
    required = operation.get("security") or []
    if not required:
        return "<h4>Authentication</h4><p>None — this endpoint is public.</p>"
    names = [name for requirement in required for name in requirement]
    items = "".join(
        f"<li><code>{_esc(name)}</code> — {_esc(schemes.get(name, {}).get('description', ''))}</li>"
        for name in names
    )
    return f"<h4>Authentication</h4><p>Any one of:</p><ul>{items}</ul>"


def _render_schemas(spec: dict) -> str:
    schemas = spec.get("components", {}).get("schemas", {})
    if not schemas:
        return ""
    blocks = ["<h2 id=\"schemas\">Schemas</h2>"]
    for name in sorted(schemas):
        schema = schemas[name]
        blocks.append(f'<h3 id="schema-{_esc(name)}"><code>{_esc(name)}</code></h3>')
        if schema.get("description"):
            blocks.append(f"<p>{_esc(schema['description'])}</p>")
        properties = schema.get("properties")
        if not properties:
            kinds = schema.get("anyOf") or schema.get("oneOf")
            if kinds:
                blocks.append(
                    "<p>One of: "
                    + ", ".join(f"<code>{_esc(_schema_label(k))}</code>" for k in kinds)
                    + "</p>"
                )
            continue
        required = set(schema.get("required") or [])
        rows = []
        for prop, prop_schema in properties.items():
            rows.append(
                "<tr>"
                f"<td><code>{_esc(prop)}</code></td>"
                f"<td>{_esc(_schema_label(prop_schema))}</td>"
                f"<td>{'yes' if prop in required else 'no'}</td>"
                f"<td>{_esc(prop_schema.get('description', ''))}</td>"
                "</tr>"
            )
        blocks.append(
            "<table><thead><tr><th>Field</th><th>Type</th><th>Required</th>"
            "<th>Description</th></tr></thead><tbody>"
            + "".join(rows)
            + "</tbody></table>"
        )
    return "".join(blocks)


def render_reference_html(spec: dict) -> str:
    """A self-contained, JavaScript-free API reference."""
    info = spec.get("info", {})
    schemes = spec.get("components", {}).get("securitySchemes", {})
    base = (spec.get("servers") or [{"url": "/"}])[0].get("url", "/")

    by_tag: dict[str, list[tuple[str, str, dict]]] = {}
    for path, method, operation in _operations(spec):
        tags = operation.get("tags") or ["Other"]
        for tag in tags:
            by_tag.setdefault(tag, []).append((path, method, operation))

    parts = [
        "<!DOCTYPE html>",
        '<html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>{_esc(info.get('title', 'API'))}</title>",
        "<style>",
        "body{font:15px/1.6 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;",
        "max-width:1000px;margin:40px auto;padding:0 20px;color:#1f2328}",
        "h1{border-bottom:2px solid #d8dee4;padding-bottom:.3em}",
        "h2{margin-top:2em;border-bottom:1px solid #d8dee4;padding-bottom:.2em}",
        "h3{margin-top:1.6em}code{background:#f6f8fa;padding:.15em .4em;border-radius:4px;",
        "font-family:SFMono-Regular,Menlo,Consolas,monospace;font-size:.9em}",
        "pre{background:#f6f8fa;padding:12px;border-radius:6px;overflow-x:auto}",
        "table{border-collapse:collapse;width:100%;margin:.6em 0;font-size:.92em}",
        "th,td{border:1px solid #d8dee4;padding:6px 10px;text-align:left;vertical-align:top}",
        "th{background:#f6f8fa}",
        ".op{border:1px solid #d8dee4;border-radius:8px;padding:16px 18px;margin:18px 0}",
        ".m{display:inline-block;font-weight:700;padding:2px 8px;border-radius:4px;",
        "color:#fff;margin-right:8px;font-size:.8em;letter-spacing:.5px}",
        ".get{background:#1a7f37}.post{background:#0969da}.delete{background:#cf222e}",
        ".put{background:#9a6700}.patch{background:#8250df}",
        ".path{font-family:SFMono-Regular,Menlo,Consolas,monospace;font-weight:600}",
        ".note{color:#57606a;font-size:.9em}",
        "</style></head><body>",
        f"<h1>{_esc(info.get('title', 'API'))}</h1>",
        f"<p class=\"note\">Version {_esc(info.get('version', ''))} · Base URL "
        f"<code>{_esc(base)}</code> · "
        f'<a href="{_esc(base)}/openapi.json">OpenAPI 3.1 document</a> · '
        f'<a href="{_esc(base)}/llms.txt">llms.txt</a></p>',
    ]

    description = (info.get("description") or "").strip()
    if description:
        for paragraph in description.split("\n\n"):
            parts.append(f"<p>{_esc(paragraph)}</p>")

    # Table of contents
    parts.append("<h2>Endpoints</h2><ul>")
    for path, method, operation in _operations(spec):
        anchor = f"{method}-{path}".replace("/", "-").replace("{", "").replace("}", "")
        parts.append(
            f'<li><code>{method.upper()}</code> <a href="#{_esc(anchor)}">'
            f"<code>{_esc(path)}</code></a> — {_esc(operation.get('summary', ''))}</li>"
        )
    parts.append("</ul>")

    for tag, operations in by_tag.items():
        parts.append(f"<h2>{_esc(tag)}</h2>")
        for path, method, operation in operations:
            anchor = f"{method}-{path}".replace("/", "-").replace("{", "").replace("}", "")
            parts.append(f'<div class="op" id="{_esc(anchor)}">')
            parts.append(
                f'<h3><span class="m {_esc(method)}">{method.upper()}</span>'
                f'<span class="path">{_esc(path)}</span></h3>'
            )
            if operation.get("summary"):
                parts.append(f"<p>{_esc(operation['summary'])}</p>")
            if operation.get("description"):
                parts.append(f'<p class="note">{_esc(operation["description"])}</p>')
            parts.append(_render_security(operation, schemes))
            parts.append(_render_parameters(operation))
            parts.append(_render_request_body(operation))
            parts.append(_render_responses(operation))
            parts.append("</div>")

    parts.append(_render_schemas(spec))
    parts.append(
        '<p class="note">This page is generated from <code>/openapi.json</code>. '
        "Point any OpenAPI 3.1 tool at that URL for an interactive client.</p>"
    )
    parts.append("</body></html>")
    return "".join(parts)


# ── llms.txt ─────────────────────────────────────────────────────────

def render_llms_txt(spec: dict, base_url: str) -> str:
    """A curated Markdown index following the llms.txt v2 layout.

    H1, then a blockquote summary, then H2 sections holding Markdown link lists.
    """
    info = spec.get("info", {})
    base = (base_url or "").rstrip("/")
    server_name = info.get("title", "cpypiserver").replace(" API", "")

    lines: list[str] = [
        f"# {server_name}",
        "",
        "> Self-hosted Python package registry. Serves `pip`, `uv` and `twine`, "
        "and exposes a JSON API authenticated with API keys.",
        "",
        f"Base URL: `{base}`",
        "",
        "## Authentication",
        "",
        "Every `/api/v1` endpoint accepts an API key as a Bearer token. Create one "
        f"on the [{base}/api-keys]({base}/api-keys) page; the raw key is shown once.",
        "",
        "```bash",
        f'curl -H "Authorization: Bearer $CPYPI_API_KEY" {base}/api/v1/packages',
        "```",
        "",
        "`pip`, `uv` and `twine` use the same key over HTTP Basic with the fixed "
        "username `__token__`:",
        "",
        "```bash",
        f"pip install --index-url {base}/simple/ <package>",
        f'twine upload --repository-url {base}/ --username __token__ --password "$CPYPI_API_KEY" dist/*',
        "```",
        "",
        "Ask `GET /api/v1/session` what the calling key is allowed to do; the "
        "response lists the exact `permissions` strings.",
        "",
        "## Documentation",
        "",
        f"- [OpenAPI 3.1 document]({base}/openapi.json): machine-readable description of "
        f"every endpoint, parameter, response schema and security scheme. Prefer this "
        f"over reading HTML.",
        f"- [API reference]({base}/docs): the same content rendered as plain HTML — no "
        f"JavaScript, no CDN.",
        f"- [Plain-text mirror of this file]({base}/llms.txt)",
        "",
        "## Registry endpoints (what package managers read)",
        "",
        f"- [Simple index]({base}/simple/): PEP 503 / PEP 691. Add "
        f"`Accept: application/vnd.pypi.simple.v1+json` (or `?format=json`) for JSON.",
        f"- [Python builds]({base}/python-builds/): prebuilt CPython for `uv python install`.",
        f"- [Health]({base}/health): liveness and package count.",
        "",
        "## API endpoints",
        "",
    ]

    by_tag: dict[str, list[tuple[str, str, dict]]] = {}
    for path, method, operation in _operations(spec):
        for tag in operation.get("tags") or ["Other"]:
            by_tag.setdefault(tag, []).append((path, method, operation))

    for tag, operations in by_tag.items():
        lines.append(f"### {tag}")
        lines.append("")
        for path, method, operation in operations:
            lines.append(
                f"- `{method.upper()} {path}` — {operation.get('summary', '')}"
            )
        lines.append("")

    lines += [
        "## Optional",
        "",
        f"- [Admin statistics]({base}/api/v1/admin/stats): requires the admin role.",
        f"- [`/.well-known/api-catalog`]({base}/.well-known/api-catalog): RFC 9727 linkset "
        f"for automated API discovery.",
        "",
    ]
    return "\n".join(lines)
