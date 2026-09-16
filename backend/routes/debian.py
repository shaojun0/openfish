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

Offline relay (the air-gap protocol)
------------------------------------
``GET  /debian/offline``              relay configuration + bundles already built
``GET  /debian/offline/snapshot``     internet → intranet: every available package
``POST /debian/offline/plan``         intranet: diff a snapshot, emit a pending list
``POST /debian/offline/bundle``       internet: download the plan, pack a .tar.gz
``GET  /debian/offline/bundles/<f>``  download a bundle this host built
``POST /debian/offline/import``       intranet: verify + unpack a bundle

Catalog
-------
``GET /debian/``                      browsable index (HTML or JSON)
``GET /api/v1/debian``                catalog document for the SPA

⚠ Decorator order is load-bearing: ``@debian_bp.route`` must be the topmost
line, or the guard is applied after registration and never runs.
``scripts/check_auth_guards.py`` enforces this.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from flask import (
    Blueprint, Response, jsonify, render_template, request,
    send_from_directory, url_for,
)

from auth.decorators import require_permission
from auth.permissions import (
    DEBIAN_DOWNLOAD, DEBIAN_OFFLINE, DEBIAN_READ, DEBIAN_UPLOAD,
)
from config import settings
from errors import BadRequestError
from openapi import api_operation, binary, errors, ok
from routes.hub_common import spa_url, wants_json
from services import debian_apt, debian_offline, hub

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
    return render_template(
        "debian/index.html",
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
@require_permission(DEBIAN_DOWNLOAD)
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


# ── Offline relay: snapshot → plan → bundle → import ─────────────────
#
# The four steps run on two hosts: `snapshot` and `bundle` on the internet
# deployment, `plan` and `import` on the intranet one.  Every step consumes one
# artifact and produces the next, so this is a chain of file transfers rather
# than a background job — the operator stays in control of what crosses the gap,
# and no state is hidden between the steps.
#
# `plan` and `import` are POSTs because they take an artifact in the body, but
# `plan` is otherwise a pure read (it never writes); only `import` mutates the
# repository, which is why the two carry different permission points.

_OFFLINE_STATUS_SCHEMA = {
    "type": "object",
    "properties": {
        "configured": {"type": "boolean"},
        "upstream": {"type": "string"},
        "root": {"type": "string"},
        "offline_dir": {"type": "string"},
        "suites": {"type": "array", "items": {"type": "string"}},
        "components": {"type": "array", "items": {"type": "string"}},
        "arches": {"type": "array", "items": {"type": "string"}},
        "recommends": {"type": "boolean"},
        "max_mb": {"type": "integer"},
        "bundle_count": {"type": "integer"},
        "bundles": {"type": "array", "items": {"type": "object"}},
    },
}

_OFFLINE_BUNDLE_SCHEMA = {
    "type": "object",
    "properties": {
        "filename": {"type": "string"},
        "download_url": {"type": "string"},
        "sha256": {"type": "string"},
        "size": {"type": "integer"},
        "size_human": {"type": "string"},
        "packages": {"type": "integer"},
        "skipped": {"type": "integer"},
        "total_bytes": {"type": "integer"},
        "plan_sha256": {"type": "string"},
    },
}

_OFFLINE_IMPORT_SCHEMA = {
    "type": "object",
    "properties": {
        "imported": {"type": "integer"},
        "skipped": {"type": "integer"},
        "failed": {"type": "integer"},
        "total_bytes": {"type": "integer"},
        "total_size_human": {"type": "string"},
        "manifest_sha256": {"type": "string"},
        "generated": {"type": "string"},
    },
}


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


def _tri_flag(*names: str) -> bool | None:
    """A form/query flag that distinguishes "absent" (None) from "false"."""
    for name in names:
        for source in (request.form, request.args):
            if name in source:
                return _truthy(source.get(name))
    return None


def _artifact_text(field: str, noun: str) -> str:
    """Read an uploaded text artifact from a file part, a form field or the body.

    Accepting all three is what lets the browser panel, the CLI and a bare
    ``curl --data-binary @snapshot.txt`` drive the same endpoint.
    """
    upload = request.files.get(field)
    if upload is not None and upload.filename:
        return upload.read().decode("utf-8", errors="replace")
    inline = request.form.get(field)
    if inline:
        return inline
    raw = request.get_data(as_text=True)
    if raw.strip():
        return raw
    raise BadRequestError(
        f"请求体为空：请以 multipart 的 {field} 文件、同名表单字段或原始文本提交{noun}"
    )


def _artifact_response(text: str, filename: str, sha256: str) -> Response:
    return Response(
        text,
        content_type="text/plain; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Openfish-Sha256": sha256,
        },
    )


@debian_bp.route("/debian/offline")
@require_permission(DEBIAN_OFFLINE)
@api_operation(
    summary="Debian offline relay status",
    description=(
        "How the relay is configured on this host — the effective apt upstream, "
        "the suites/components/arches a snapshot walks, the local repository "
        "root, the bundle directory and the bundles already built — so the "
        "console can show which half of the air gap this deployment is."
    ),
    tags=["Debian"],
    responses={
        "200": ok("Relay configuration and built bundles", _OFFLINE_STATUS_SCHEMA),
        **errors("401", "403", "500"),
    },
)
def debian_offline_status():
    return jsonify(debian_offline.status_payload())


@debian_bp.route("/debian/offline/snapshot")
@require_permission(DEBIAN_OFFLINE)
@api_operation(
    summary="Export the internet-side package snapshot",
    description=(
        "Builds the **snapshot** artifact: one tab-separated text file "
        "describing every package the configured suites/components/arches "
        "offer, with each package's apt `Filename:`, size, SHA-256 and "
        "dependency fields.  Indexes are read from the local mirror tree first "
        "and the upstream mirror second; a combination the mirror does not "
        "carry is listed in the header as a `missing_index_*` note instead of "
        "failing the whole snapshot.\n\n"
        "The response is `text/plain` with the file's own `Content-Disposition` "
        "and its digest in `X-Openfish-Sha256`.  Query parameters `suites`, "
        "`components` and `arches` narrow the walk; `fresh=1` bypasses the apt "
        "metadata TTL.  Carry the file to the intranet and `POST` it to "
        "`/debian/offline/plan`."
    ),
    tags=["Debian"],
    parameters=[
        {"name": "suites", "in": "query", "schema": {"type": "string"}},
        {"name": "components", "in": "query", "schema": {"type": "string"}},
        {"name": "arches", "in": "query", "schema": {"type": "string"}},
        {"name": "fresh", "in": "query", "schema": {"type": "boolean"}},
    ],
    responses={
        "200": binary("The snapshot text document"),
        **errors("400", "401", "403", "500", "502"),
    },
)
def debian_offline_snapshot():
    snapshot = debian_offline.build_snapshot(
        suites=debian_offline.split_list(request.args.get("suites")) or None,
        components=debian_offline.split_list(request.args.get("components")) or None,
        arches=debian_offline.split_list(request.args.get("arches")) or None,
        fresh=_truthy(request.args.get("fresh")),
    )
    return _artifact_response(snapshot.text, snapshot.filename, snapshot.sha256)


@debian_bp.route("/debian/offline/plan", methods=["POST"])
@require_permission(DEBIAN_OFFLINE)
@api_operation(
    summary="Diff a snapshot and emit the pending-update plan",
    description=(
        "Run on the **intranet** host.  Takes the snapshot the internet side "
        "produced (a `snapshot` file part, a `snapshot` form field, or the raw "
        "request body) and returns the **plan**: a text file listing every "
        "package this host's local `.deb` repository lacks or holds at an older "
        "version, plus the transitive dependency closure taken from the "
        "snapshot's own `Depends`/`Pre-Depends`.\n\n"
        "Form flags: `only` restricts the direct set to a space/comma list of "
        "package names (the closure still supplies their dependencies); "
        "`allow_downgrade=1` lets a locally-newer package be replaced; "
        "`verify_hashes=1` re-hashes local files to catch same-version content "
        "drift; `recommends=1`/`0` forces `Recommends` into, or out of, the "
        "closure.  Dependencies the snapshot cannot satisfy are listed in the "
        "header as `unresolved_*` rather than dropped.  Carry the returned "
        "`text/plain` file back to the internet side."
    ),
    tags=["Debian"],
    request_body={
        "required": True,
        "content": {
            "multipart/form-data": {
                "schema": {
                    "type": "object",
                    "properties": {
                        "snapshot": {"type": "string", "format": "binary"},
                        "only": {"type": "string"},
                        "allow_downgrade": {"type": "boolean"},
                        "verify_hashes": {"type": "boolean"},
                        "recommends": {"type": "boolean"},
                    },
                }
            },
            "text/plain": {"schema": {"type": "string"}},
        },
    },
    responses={
        "200": binary("The pending-update plan text document"),
        **errors("400", "401", "403", "500"),
    },
)
def debian_offline_plan():
    text = _artifact_text("snapshot", "快照文件")
    try:
        plan = debian_offline.build_plan(
            text,
            only=debian_offline.split_list(
                request.form.get("only") or request.args.get("only")
            ) or None,
            allow_downgrade=bool(_tri_flag("allow_downgrade")),
            verify_hashes=bool(_tri_flag("verify_hashes")),
            recommends=_tri_flag("recommends"),
        )
    except ValueError as exc:
        raise BadRequestError(str(exc)) from exc
    return _artifact_response(plan.text, plan.filename, plan.sha256)


@debian_bp.route("/debian/offline/bundle", methods=["POST"])
@require_permission(DEBIAN_OFFLINE)
@api_operation(
    summary="Build an offline update bundle from a plan",
    description=(
        "Run on the **internet** host.  Takes the plan the intranet produced (a "
        "`plan` file part, a `plan` form field, or the raw request body), "
        "re-resolves every requested package against this host's own apt "
        "metadata, downloads the files (local repository first, upstream "
        "second), verifies size and SHA-256, and packs them into a `.tar.gz` "
        "under `DEBIAN_OFFLINE_DIR`.\n\n"
        "A package the upstream no longer serves, or whose digest does not "
        "match, is reported in the response's `skipped_packages` instead of "
        "silently entering the archive.  A plan whose declared size exceeds "
        "`DEBIAN_OFFLINE_MAX_MB` is refused with `400` before anything is "
        "downloaded.  The response is JSON with the bundle's name, size, digest "
        "and `download_url`; fetch that URL (resumable, `debian:download`) and "
        "carry the archive to the intranet."
    ),
    tags=["Debian"],
    request_body={
        "required": True,
        "content": {
            "multipart/form-data": {
                "schema": {
                    "type": "object",
                    "properties": {"plan": {"type": "string", "format": "binary"}},
                }
            },
            "text/plain": {"schema": {"type": "string"}},
        },
    },
    responses={
        "201": ok("The built bundle", _OFFLINE_BUNDLE_SCHEMA),
        **errors("400", "401", "403", "500", "502"),
    },
)
def debian_offline_bundle():
    text = _artifact_text("plan", "待更新清单")
    try:
        bundle = debian_offline.build_bundle(text)
    except ValueError as exc:
        raise BadRequestError(str(exc)) from exc
    payload = bundle.to_dict()
    payload["download_url"] = url_for(
        "debian.debian_offline_download_bundle", filename=bundle.filename
    )
    return jsonify(payload), 201


@debian_bp.route("/debian/offline/bundles/<filename>")
@require_permission(DEBIAN_DOWNLOAD)
@api_operation(
    summary="Download a bundle this host built",
    description=(
        "Streams one `.tar.gz` out of `DEBIAN_OFFLINE_DIR`.  `send_from_directory` "
        "handles `Range`/`HEAD`, so a bundle interrupted in transit can be "
        "resumed.  The `download_url` a bundle build returns points here."
    ),
    tags=["Debian"],
    responses={
        "200": binary("The bundle archive"),
        **errors("401", "403", "404", "500"),
    },
)
def debian_offline_download_bundle(filename: str):
    return send_from_directory(
        settings.hub.debian_offline_dir, filename, as_attachment=True
    )


@debian_bp.route("/debian/offline/import", methods=["POST"])
@require_permission(DEBIAN_UPLOAD)
@api_operation(
    summary="Import an offline update bundle",
    description=(
        "Run on the **intranet** host.  Takes the bundle archive as a "
        "multipart `bundle` file part, extracts it to a staging directory, "
        "verifies every member against the manifest's size and SHA-256, and "
        "only then moves the `.deb` files into `DEBIAN_DIR` (mirror `pool/` "
        "layout, plus a hard link at the repository root so the flat "
        "`/debian/Packages` index lists them).  A bundle that fails any check "
        "writes nothing at all; a re-import of the same bundle skips files that "
        "are already present with the same digest.\n\n"
        "Requires `debian:upload`, which only the built-in admin role holds. "
        "The upload goes through Flask, so `MAX_CONTENT_LENGTH` (storage "
        "configuration) must be raised above the largest bundle a deployment "
        "expects."
    ),
    tags=["Debian"],
    request_body={
        "required": True,
        "content": {
            "multipart/form-data": {
                "schema": {
                    "type": "object",
                    "properties": {
                        "bundle": {"type": "string", "format": "binary"},
                    },
                    "required": ["bundle"],
                }
            }
        },
    },
    responses={
        "200": ok("What the import wrote, skipped and refused", _OFFLINE_IMPORT_SCHEMA),
        **errors("400", "401", "403", "413", "500"),
    },
)
def debian_offline_import():
    upload = request.files.get("bundle")
    if upload is None or not upload.filename:
        raise BadRequestError("multipart/form-data 需要一个 bundle 字段")
    workdir = tempfile.mkdtemp(prefix="openfish-upload-")
    try:
        staged = Path(workdir) / Path(upload.filename.replace("\\", "/")).name
        upload.save(staged)
        try:
            report = debian_offline.import_bundle(
                staged, root=settings.hub.debian_dir
            )
        except ValueError as exc:
            raise BadRequestError(str(exc)) from exc
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    return jsonify(report.to_dict())


__all__ = ["debian_bp"]
