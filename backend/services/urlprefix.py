"""The client-visible URL prefix — one answer for both deployment styles.

A deployment can put this server under a path (``/pypi``) in two ways, and the
URL builders used to answer that question in two different ways, which is how
the ``/api/v1/*`` catalogs lost the prefix behind the shipped nginx:

* **the edge nginx strips it** (the shipped topology, and what a first-level
  ``location /pypi`` needs): it forwards the stripped path plus
  ``X-Forwarded-Prefix: /pypi``.  ``ProxyFix`` turns that header into
  ``SCRIPT_NAME``, so the prefix lives in ``request.script_root`` and the Flask
  routes themselves stay at the root.
* **the app itself is mounted under it** (``ROUTE_PREFIX``): Flask registers
  every route, and therefore every ``url_for``, with the prefix already in the
  URL map.  The request context does not carry it, so this module falls back to
  the configured value.

:func:`external_prefix` and :func:`external_base_url` are the only two places
allowed to answer "what prefix does the client see?".  Read them from the
definition (``from services.urlprefix import external_prefix``), never by
re-deriving ``script_root`` at a call site.
"""

from __future__ import annotations

from flask import request

from config import settings


def external_prefix() -> str:
    """The client-visible path prefix, without a trailing slash.

    ``""`` when the app is served at the root.  ``SCRIPT_NAME`` (set by
    ``ProxyFix`` from ``X-Forwarded-Prefix``) wins over ``ROUTE_PREFIX``: with
    the shipped nginx the former is the request-scoped truth, while the latter
    is only used when no proxy header describes this request.
    """
    return (request.script_root or settings.server.route_prefix).rstrip("/")


def external_base_url() -> str:
    """The absolute client-visible base URL, without a trailing slash.

    ``request.url_root`` already carries ``SCRIPT_NAME``; ``ROUTE_PREFIX`` is
    added only when nothing set it (the "app mounted under the prefix" style),
    so the two styles never double the prefix.
    """
    base = request.url_root.rstrip("/")
    configured = settings.server.route_prefix.rstrip("/")
    if configured and not request.script_root:
        base += configured
    return base


__all__ = ["external_base_url", "external_prefix"]
