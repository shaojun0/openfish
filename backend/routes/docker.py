"""Docker routes — the registry v2 protocol plus the offline artifact catalog.

Two things live here, and they are deliberately separate:

* **The registry protocol** under ``/docker/v2/`` — what ``docker pull`` (and
  ``skopeo``, ``containerd``, …) speaks. It serves manifests and blobs, backed
  by the local artifact directory and, when ``DOCKER_UPSTREAM`` is configured,
  by a read-through proxy in front of an upstream registry.
* **The offline catalog** under ``/docker/`` — ``docker save`` tarballs and
  compose/Dockerfile snippets an air-gapped host imports with ``docker load``.

Wire protocol
-------------
``GET  /docker/v2/``                          API version probe
``GET  /docker/v2/_catalog``                  repository list
``GET  /docker/v2/<name>/tags/list``          tags
``GET|HEAD /docker/v2/<name>/manifests/<ref>`` manifest
``GET|HEAD /docker/v2/<name>/blobs/<digest>``  blob

Catalog
-------
``GET /docker/``                              browsable index (HTML or JSON)
``GET /docker/files/<filename>``              download an image tar / snippet
``GET /api/v1/docker``                        catalog document for the SPA
``POST /api/v1/docker``                       upload an image tar / snippet — ``docker:upload``

Scope limitation
----------------
The registry paths are a **read-through proxy** in front of ``DOCKER_UPSTREAM``
(Docker Hub, or an intranet registry) with a local disk cache.  The ``docker
save`` tarballs in ``DOCKER_DIR`` remain the offline path: they contribute
repository names and tags to ``/v2/_catalog`` and ``/v2/<name>/tags/list``, but
this server does **not** unpack a saved tarball and serve its layers as registry
manifests/blobs.  With no upstream configured, a manifest or blob request for a
local-only image returns 404 saying exactly that.

⚠ Decorator order is load-bearing: ``@docker_bp.route`` must be the topmost
line, or the guard is applied after registration and never runs.
``scripts/check_auth_guards.py`` enforces this.
"""

from __future__ import annotations

import logging
from urllib.parse import quote

from flask import (
    Blueprint, Response, jsonify, render_template, request,
    send_from_directory, url_for,
)

from auth.decorators import require_permission
from auth.permissions import DOCKER_DOWNLOAD, DOCKER_READ, DOCKER_UPLOAD
from config import settings
from errors import BadRequestError, PypiError
from openapi import api_operation, binary, errors, json_body, ok
from routes.hub_common import spa_url, wants_json
from services import docker_registry as registry
from services import hub, hub_upload

logger = logging.getLogger("cpypiserver.docker")

docker_bp = Blueprint("docker", __name__)

_DOCKER_SCHEMA = {
    "type": "object",
    "properties": {
        "root": {"type": "string"},
        "exists": {"type": "boolean"},
        "registry": {"type": "string"},
        "artifact_count": {"type": "integer"},
        "artifacts": {"type": "array", "items": {"type": "object"}},
    },
}

#: One `services.hub.scan_docker` entry — the shape the catalog and the upload
#: response share. `kind` is `image`, `compose`, `dockerfile` or `file`.
_DOCKER_ARTIFACT_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "version": {"type": "string"},
        "arch": {"type": "string"},
        "kind": {"type": "string"},
        "filename": {"type": "string"},
        "size": {"type": "integer"},
        "size_human": {"type": "string"},
        "sha256": {"type": ["string", "null"]},
        "modified": {"type": ["string", "null"]},
        "download_url": {"type": "string"},
        "description": {"type": ["string", "null"]},
        "tags": {"type": "array", "items": {"type": "string"}},
    },
}

_DOCKER_CATALOG_SCHEMA = {
    "type": "object",
    "properties": {"repositories": {"type": "array", "items": {"type": "string"}}},
}

_TAGS_SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "tags": {"type": "array", "items": {"type": "string"}},
    },
}


def _docker_payload() -> dict:
    prefix = settings.server.route_prefix.rstrip("/") + "/docker/files"
    return hub.scan_docker(
        settings.hub.docker_dir,
        url_prefix=prefix,
        registry=settings.hub.docker_registry,
    )


def _docker_entry_by_filename(filename: str) -> dict | None:
    """The catalog entry for one on-disk artifact, or ``None`` if it is hidden."""
    for artifact in _docker_payload()["artifacts"]:
        if artifact["filename"] == filename:
            return artifact
    return None


def _registry_error(exc: registry.DockerRegistryError) -> Response:
    """The OCI error body a registry client expects, e.g. ``{"errors": [...]}``.

    A ``5xx`` from this server means the *upstream* failed, and its message can
    carry the upstream URL, a TLS error or a response body — internals the client
    has no business seeing.  Those are logged and replaced with the generic OCI
    ``UNAVAILABLE`` text.  A ``4xx`` is the client's own problem (an unknown
    repository, a bad reference) and keeps its specific message, which is what a
    ``docker pull`` shows the user.
    """
    if exc.status >= 500:
        logger.warning("docker upstream failure (%s): %s", exc.code, exc.message)
        message = "upstream registry unavailable"
    else:
        message = exc.message
    response = jsonify({"errors": [{"code": exc.code, "message": message}]})
    response.status_code = exc.status
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


def _name_parameter() -> dict:
    return {
        "name": "name",
        "in": "path",
        "required": True,
        "description": "Repository name, e.g. `library/nginx`.",
        "schema": {"type": "string"},
    }


# ── Catalog: browsable index + Registry v2 catalog + download ────────

@docker_bp.route("/docker/")
@require_permission(DOCKER_READ)
@api_operation(
    summary="Docker catalog index",
    description=(
        "The local docker directory as a browsable index: `docker save` image "
        "tarballs plus the compose/Dockerfile snippets an offline host needs. "
        "HTML by default, the `/api/v1/docker` document with `?format=json`.\n\n"
        "A live `docker pull` uses the registry protocol at `/docker/v2/` "
        "instead; this page is the offline-artifact view."
    ),
    tags=["Docker"],
    responses={
        "200": {
            "description": "Docker catalog as HTML or JSON",
            "content": {
                "text/html": {},
                "application/json": {"schema": _DOCKER_SCHEMA},
            },
        },
        **errors("401", "403", "500"),
    },
)
def docker_index():
    catalog = _docker_payload()
    if wants_json():
        return jsonify(catalog)
    return render_template(
        "docker/index.html",
        server_name=settings.server.server_name,
        base_url=url_for("docker.docker_index", _external=True),
        spa_url=spa_url("/docker"),
        registry=catalog["registry"],
        artifacts=catalog["artifacts"],
        artifact_count=catalog["artifact_count"],
        catalog_url=url_for("docker.docker_catalog"),
    )


# ── Registry v2 pull protocol ────────────────────────────────────────

@docker_bp.route("/docker/v2/")
@require_permission(DOCKER_READ)
@api_operation(
    summary="Registry v2 API version probe",
    description=(
        "The `GET /v2/` probe every registry client calls first. It answers "
        "`200` with an empty JSON object and "
        "`Docker-Distribution-Api-Version: registry/2.0`; anything else makes "
        "the client refuse to proceed. It deliberately does not touch the "
        "upstream, so it works even in local-only mode."
    ),
    tags=["Docker"],
    responses={
        "200": {
            "description": "The registry speaks v2",
            "content": json_body(),
        },
        **errors("401", "403"),
    },
)
def docker_v2_probe():
    response = jsonify({})
    response.headers["Docker-Distribution-Api-Version"] = "registry/2.0"
    return response


@docker_bp.route("/docker/v2/_catalog")
@require_permission(DOCKER_READ)
@api_operation(
    summary="Docker registry catalog",
    description=(
        "The repositories this registry can serve, in the OCI distribution "
        "spec's `GET /v2/_catalog` shape (`{\"repositories\": [...]}`). It is "
        "the only enumeration endpoint the registry protocol defines, which "
        "makes it this ecosystem's counterpart to npm's `/-/all`.\n\n"
        "The list is the union of the local `docker save` catalog and the "
        "repositories this proxy has cached manifests for. It cannot include "
        "everything the upstream has: no registry protocol endpoint enumerates "
        "a remote registry."
    ),
    tags=["Docker"],
    responses={
        "200": ok("Repository names", _DOCKER_CATALOG_SCHEMA),
        **errors("401", "403", "500"),
    },
)
def docker_catalog():
    document = hub.docker_registry_catalog(_docker_payload())
    repositories = set(document.get("repositories") or [])
    repositories.update(registry.cached_repositories())
    document["repositories"] = sorted(repositories)
    return jsonify(document)


@docker_bp.route("/docker/v2/<path:name>/tags/list")
@require_permission(DOCKER_READ)
@api_operation(
    summary="List repository tags",
    description=(
        "The tags of one repository as `{\"name\": ..., \"tags\": [...]}`. The "
        "list merges local `docker save` tarball tags with the upstream "
        "registry's tags; when the upstream is unreachable the local/cached "
        "list is returned instead of an error.\n\n"
        "`?n=` limits the number of tags and `?last=` resumes lexically after a "
        "tag, per the OCI pagination convention. A `Link: <...>; rel=\"next\"` "
        "header points at the next page when there is one."
    ),
    tags=["Docker"],
    parameters=[
        _name_parameter(),
        {
            "name": "n", "in": "query", "required": False,
            "description": "Maximum number of tags to return.",
            "schema": {"type": "integer", "minimum": 0},
        },
        {
            "name": "last", "in": "query", "required": False,
            "description": "Return tags lexically after this one.",
            "schema": {"type": "string"},
        },
    ],
    responses={
        "200": ok("Tags of the repository", _TAGS_SCHEMA),
        **errors("401", "403", "404", "502"),
    },
)
def docker_tags(name: str):
    try:
        tags = registry.list_tags(name)
    except registry.DockerRegistryError as exc:
        return _registry_error(exc)

    last = request.args.get("last", "")
    if last:
        tags = [tag for tag in tags if tag > last]
    limit = request.args.get("n", type=int)
    truncated = False
    if limit is not None and limit >= 0 and len(tags) > limit:
        tags = tags[:limit]
        truncated = True

    response = jsonify({"name": name, "tags": tags})
    if truncated and tags:
        response.headers["Link"] = (
            f'</docker/v2/{quote(name)}/tags/list'
            f'?n={limit}&last={quote(tags[-1])}>; rel="next"'
        )
    return response


@docker_bp.route("/docker/v2/<path:name>/manifests/<reference>", methods=["GET", "HEAD"])
@require_permission(DOCKER_READ)
@api_operation(
    summary="Get a manifest",
    description=(
        "Fetch the manifest a tag or digest names, streamed from the local "
        "cache or the upstream registry. The client's `Accept` header is "
        "forwarded, so manifest lists and image manifests negotiate the same "
        "way they would against the upstream; the upstream `Content-Type` and "
        "`Docker-Content-Digest` are echoed back.\n\n"
        "A manifest fetched by tag is cached for a short TTL; one fetched by "
        "digest is immutable. `HEAD` returns the same headers with no body."
    ),
    tags=["Docker"],
    parameters=[
        _name_parameter(),
        {
            "name": "reference",
            "in": "path",
            "required": True,
            "description": "A tag (`latest`) or a digest (`sha256:…`).",
            "schema": {"type": "string"},
        },
    ],
    responses={
        "200": binary("The manifest body"),
        **errors("401", "403", "404", "502"),
    },
)
def docker_manifest(name: str, reference: str):
    try:
        result = registry.get_manifest(
            name, reference, accept=request.headers.get("Accept"),
        )
    except registry.DockerRegistryError as exc:
        return _registry_error(exc)

    headers = {
        "Content-Type": result.content_type,
        "Docker-Content-Digest": result.digest,
    }
    # Werkzeug strips the body for a HEAD request while keeping Content-Length,
    # so the same response is correct for both methods and no bytes are sent.
    return Response(result.content, status=200, headers=headers)


@docker_bp.route("/docker/v2/<path:name>/blobs/<digest>", methods=["GET", "HEAD"])
@require_permission(DOCKER_DOWNLOAD)
@api_operation(
    summary="Get a blob",
    description=(
        "Fetch a content-addressed blob (a layer or an image config). Blobs are "
        "immutable, so a fetched blob is cached permanently under "
        "`DOCKER_CACHE_MAX_MB`; a cached blob answers `Range` requests with "
        "`206` + `Content-Range`. An uncached blob is streamed from the "
        "upstream registry while the same bytes are written to the cache, so "
        "the second request for the digest is served from disk.\n\n"
        "`HEAD` returns the upstream headers (including `Content-Length`) "
        "without downloading the blob."
    ),
    tags=["Docker"],
    parameters=[
        _name_parameter(),
        {
            "name": "digest",
            "in": "path",
            "required": True,
            "description": "Content digest of the blob, e.g. `sha256:…`.",
            "schema": {"type": "string"},
        },
    ],
    responses={
        "200": binary("The blob contents"),
        "206": binary("A byte range of the blob"),
        **errors("400", "401", "403", "404", "502"),
    },
)
def docker_blob(name: str, digest: str):
    try:
        return registry.open_blob(
            name,
            digest,
            range_header=request.headers.get("Range"),
            head=request.method == "HEAD",
        )
    except registry.DockerRegistryError as exc:
        return _registry_error(exc)


# ── Offline artifacts ────────────────────────────────────────────────

@docker_bp.route("/docker/files/<path:filename>")
@require_permission(DOCKER_DOWNLOAD)
@api_operation(
    summary="Download a docker artifact",
    description=(
        "Streams one image tarball or compose/Dockerfile out of `DOCKER_DIR`. "
        "Load an image with `docker load -i <file.tar>`."
    ),
    tags=["Docker"],
    responses={
        "200": binary("The requested image tarball or config file"),
        **errors("401", "403", "404", "500"),
    },
)
def download_docker_file(filename: str):
    return send_from_directory(settings.hub.docker_dir, filename, as_attachment=True)


@docker_bp.route("/api/v1/docker")
@require_permission(DOCKER_READ)
@api_operation(
    summary="Docker catalog",
    description=(
        "Offline docker artifacts known to this server: `docker save` image "
        "tarballs (filename parsed as `<name>-<tag>.tar`) and compose/Dockerfile "
        "snippets, plus the optional intranet registry the UI advertises. "
        "Entries with a null `download_url` are metadata only."
    ),
    tags=["Docker"],
    responses={
        "200": ok("Local docker catalog", _DOCKER_SCHEMA),
        **errors("401", "403", "500"),
    },
)
def docker_catalog_api():
    return jsonify(_docker_payload())


@docker_bp.route("/api/v1/docker", methods=["POST"])
@require_permission(DOCKER_UPLOAD)
@api_operation(
    summary="Upload a docker artifact",
    description=(
        "Stores one `multipart/form-data` `file` part in `DOCKER_DIR`, next to "
        "the artifacts `GET /api/v1/docker` lists. This is the offline path an "
        "air-gapped host consumes after `docker load -i`, and the page's own "
        "upload control.\n\n"
        "Accepted, matching what the catalog knows how to describe: a "
        "`docker save` image tarball (`.tar`, `.tar.gz`, `.tgz`), a compose file "
        "(`.yml`, `.yaml`), or a Dockerfile (`Dockerfile`, `Dockerfile.<stage>` "
        "or `*.dockerfile`). The name must be a single path segment — no `/`, no "
        "backslash, no `..`, no dotfile — and a name the scanner hides as "
        "metadata (`catalog.json`, README/LICENSE/CHANGELOG) is refused. An "
        "existing file answers `409` unless `STORAGE__OVERWRITE=true`; a body "
        "above `MAX_CONTENT_LENGTH` answers `413`. The response is the stored "
        "entry in exactly the shape `GET /api/v1/docker` reports.\n\n"
        "Requires `docker:upload`, which by default only the built-in admin role "
        "holds: pulling from the catalog and the pull-through registry is open to "
        "every signed-in user, but publishing an artifact is administrative."
    ),
    tags=["Docker"],
    request_body={
        "required": True,
        "content": {
            "multipart/form-data": {
                "schema": {
                    "type": "object",
                    "properties": {"file": {"type": "string", "format": "binary"}},
                    "required": ["file"],
                }
            }
        },
    },
    responses={
        "201": ok(
            "The stored artifact, in the catalog's entry shape",
            _DOCKER_ARTIFACT_SCHEMA,
        ),
        **errors("400", "401", "403", "409", "413", "500"),
    },
)
def upload_docker_artifact():
    upload = request.files.get("file")
    if upload is None or not upload.filename:
        raise BadRequestError("multipart/form-data 需要一个 file 字段")
    target = hub_upload.docker_target(settings.hub.docker_dir, upload.filename)
    hub_upload.save(target, upload)
    entry = _docker_entry_by_filename(target.name)
    if entry is None:
        # `docker_target` refuses every name the scanner hides; reaching here
        # means the scanner's visibility rule changed under us.
        raise PypiError(
            f"已写入 {target.name}，但 Docker 目录扫描未列出它；请检查 DOCKER_DIR 的可见性规则",
            status_code=500,
        )
    return jsonify(entry), 201


__all__ = ["docker_bp"]
