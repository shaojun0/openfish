"""Debian routes — the apt repository surface plus the local package catalog.

apt talks HTTP like pip does: it fetches an index (``Packages``, with a
``Release``/``InRelease`` describing it) and then the ``.deb`` files the index
points at. Both halves are served here:

* **A flat, locally-built repository** — ``/debian/Packages`` is rendered from
  the ``.deb`` files that actually exist in ``DEBIAN_DIR``, and the files come
  from ``/debian/files/``. ``deb [trusted=yes] <base>/debian/ ./`` is all a
  client needs.
* **A mirror proxy** — when ``DEBIAN_UPSTREAM`` is configured, ``dists/`` and
  ``pool/`` paths are streamed from it (metadata cached), so an intranet host
  can point a normal ``deb http://<mirror> bookworm main`` line at this server.

Wire protocol
-------------
``GET /debian/Packages``              flat apt index built from local .deb files
``GET /debian/files/<filename>``      one local .deb / apt snippet
``GET /debian/dists/<path>``          Release / InRelease / Packages (proxied, cached)
``GET|HEAD /debian/pool/<path>``      .deb from the upstream mirror (proxied, Range)

Catalog
-------
``GET /debian/``                      browsable index (HTML or JSON)
``GET /api/v1/debian``                catalog document for the SPA

⚠ Decorator order is load-bearing: ``@debian_bp.route`` must be the topmost
line, or the guard is applied after registration and never runs.
``scripts/check_auth_guards.py`` enforces this.
"""

from __future__ import annotations

from flask import (
    Blueprint, Response, jsonify, render_template_string, request,
    send_from_directory, url_for,
)

from auth.decorators import require_permission
from auth.permissions import DEBIAN_DOWNLOAD, DEBIAN_READ
from config import settings
from openapi import api_operation, binary, errors, ok
from routes.hub_common import spa_url, wants_json
from services import debian_apt, hub, templates

debian_bp = Blueprint("debian", __name__)

_DEBIAN_SCHEMA = {
    "type": "object",
    "properties": {
        "root": {"type": "string"},
        "exists": {"type": "boolean"},
        "mirror": {"type": "string"},
        "artifact_count": {"type": "integer"},
        "artifacts": {"type": "array", "items": {"type": "object"}},
    },
}


def _debian_payload() -> dict:
    prefix = settings.server.route_prefix.rstrip("/") + "/debian/files"
    return hub.scan_debian(
        settings.hub.debian_dir,
        url_prefix=prefix,
        mirror=settings.hub.debian_mirror,
    )


# ── Catalog: browsable index + flat Packages + download ──────────────

@debian_bp.route("/debian/")
@require_permission(DEBIAN_READ)
@api_operation(
    summary="Debian catalog index",
    description=(
        "The local debian directory as a browsable index: `.deb` files plus the "
        "apt `sources.list` snippet for the intranet mirror. HTML by default, "
        "the `/api/v1/debian` document with `?format=json`.\n\n"
        "`apt` itself reads the flat `Packages` index at `/debian/Packages`, or "
        "the proxied `dists/` and `pool/` paths when an upstream mirror is "
        "configured."
    ),
    tags=["Debian"],
    responses={
        "200": {
            "description": "Debian catalog as HTML or JSON",
            "content": {
                "text/html": {},
                "application/json": {"schema": _DEBIAN_SCHEMA},
            },
        },
        **errors("401", "403", "500"),
    },
)
def debian_index():
    catalog = _debian_payload()
    if wants_json():
        return jsonify(catalog)
    return render_template_string(
        templates.debian_index(),
        server_name=settings.server.server_name,
        base_url=url_for("debian.debian_index", _external=True),
        spa_url=spa_url("/debian"),
        mirror=catalog["mirror"],
        artifacts=catalog["artifacts"],
        artifact_count=catalog["artifact_count"],
        packages_url=url_for("debian.debian_packages"),
    )


@debian_bp.route("/debian/Packages")
@require_permission(DEBIAN_READ)
@api_operation(
    summary="Flat apt Packages index",
    description=(
        "A flat apt repository index rendered from the local `.deb` files — the "
        "static index element of the Debian ecosystem. Only entries that "
        "actually exist on disk are listed, because apt fails on a `Filename:` "
        "that does not resolve."
    ),
    tags=["Debian"],
    responses={
        "200": {"description": "apt Packages stanzas", "content": {"text/plain": {}}},
        **errors("401", "403", "500"),
    },
)
def debian_packages():
    return Response(
        hub.debian_packages_index(_debian_payload()), mimetype="text/plain"
    )


@debian_bp.route("/debian/files/<path:filename>")
@require_permission(DEBIAN_DOWNLOAD)
@api_operation(
    summary="Download a debian artifact",
    description=(
        "Streams one `.deb` or apt config snippet out of `DEBIAN_DIR`. "
        "Install a package with `apt install ./<file>.deb`."
    ),
    tags=["Debian"],
    responses={
        "200": binary("The requested .deb or config file"),
        **errors("401", "403", "404", "500"),
    },
)
def download_debian_file(filename: str):
    return send_from_directory(settings.hub.debian_dir, filename, as_attachment=True)


# ── Mirror proxy: dists/ (cached metadata) + pool/ (streamed packages) ──

@debian_bp.route("/debian/dists/<path:path>", methods=["GET"])
@require_permission(DEBIAN_READ)
@api_operation(
    summary="Proxy apt metadata from the upstream mirror",
    description=(
        "Read-through mirror of the apt metadata tree: `Release`, `InRelease`, "
        "`Release.gpg`, `<component>/binary-<arch>/Packages` and its `.gz`/`.xz` "
        "variants, and `by-hash/...` paths.\n\n"
        "Documents are cached on disk for `DEBIAN_METADATA_TTL` seconds and "
        "served byte-for-byte with the upstream `Content-Type` and "
        "`Content-Encoding` intact — apt verifies the signatures and hashes in "
        "`Release`, so a re-encoded body would fail verification. A mirror "
        "`404`/`403` is forwarded as a JSON error with the upstream status; an "
        "unreachable mirror is a `502`.\n\n"
        "With no upstream configured the response is a `404` explaining that "
        "only the local flat repository is available."
    ),
    tags=["Debian"],
    responses={
        "200": binary("The upstream metadata document, unchanged"),
        **errors("400", "401", "403", "404", "502"),
    },
)
def debian_dists(path: str):
    return debian_apt.dists_response(path)


@debian_bp.route("/debian/pool/<path:path>", methods=["GET", "HEAD"])
@require_permission(DEBIAN_READ)
@api_operation(
    summary="Proxy an apt package file from the upstream mirror",
    description=(
        "Streams one `pool/.../*.deb` straight from the upstream mirror without "
        "caching it. The client's `Range` header is forwarded, so a `206` with "
        "`Content-Range` / `Accept-Ranges` / `Content-Length` comes back and a "
        "resumed `apt install` works. `HEAD` performs an upstream `HEAD` and "
        "never downloads a body.\n\n"
        "A mirror `404`/`403` is forwarded as a JSON error with the upstream "
        "status; an unreachable mirror is a `502`. With no upstream configured "
        "the response is a `404`."
    ),
    tags=["Debian"],
    responses={
        "200": binary("The requested package file"),
        "206": {"description": "The requested byte range", "content": {"application/octet-stream": {}}},
        **errors("400", "401", "403", "404", "502"),
    },
)
def debian_pool(path: str):
    return debian_apt.pool_response(path, request.method)


@debian_bp.route("/api/v1/debian")
@require_permission(DEBIAN_READ)
@api_operation(
    summary="Debian catalog",
    description=(
        "Local `.deb` packages (filename parsed as `<pkg>_<version>_<arch>.deb`) "
        "and apt config snippets, plus the optional intranet mirror the UI "
        "advertises. Entries with a null `download_url` are metadata only."
    ),
    tags=["Debian"],
    responses={
        "200": ok("Local debian catalog", _DEBIAN_SCHEMA),
        **errors("401", "403", "500"),
    },
)
def debian_catalog_api():
    return jsonify(_debian_payload())


__all__ = ["debian_bp"]
