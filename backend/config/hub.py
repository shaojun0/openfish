"""Artifact-hub configuration — tools, npm, docker, debian and model routing.

The server started life as a Python-only package index.  It is now growing into
an intranet artifact hub with a handful of sibling catalogs, each backed by a
directory on disk (or a small JSON file) rather than a database:

* ``tools_dir``    — a directory tree whose sub-directories are *categories*
  and whose files are the downloadable tools themselves.
* ``docs_dir``     — one sub-directory per ecosystem holding that ecosystem's
  Markdown documentation. Read-only over HTTP except for the admin-only
  upload/delete endpoints.
* ``npm_dir``      — the local npm cache/registry directory.  A read-through
  proxy in front of ``npm_upstream`` serves the registry protocol, so a client
  pointed at ``npm config set registry`` can install anything the upstream
  mirror has, with the local tarballs as the first source.
* ``docker_dir``   — ``docker save`` image tarballs plus the compose/Dockerfile
  snippets an offline host needs.
* ``debian_dir``   — local ``.deb`` files plus the ``sources.list`` snippet for
  the intranet mirror.
* ``models_file``  — the JSON description of the model routes a downstream
  DSH deployment may point at.

Everything is deliberately file-based and configuration-driven: adding a tool
is ``cp``-ing a file into ``tools/<category>/``, and adding a model route is
editing one JSON file.  No schema migration, no restart.
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from config.paths import backend_path, catalog_path


class HubConfig(BaseSettings):
    # Flat .env names (TOOLS_DIR, NPM_DIR, MODELS_FILE, ...) work as well as
    # the nested HUB__* form — see ServerConfig.model_config.
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    tools_dir: str = Field(
        default=catalog_path("tools"),
        description=(
            "Root of the tools catalog. Each immediate sub-directory is a "
            "category; files below a category are downloadable tools."
        ),
    )
    docs_dir: str = Field(
        default=catalog_path("docs"),
        description=(
            "Root of the per-ecosystem Markdown documentation. Each immediate "
            "sub-directory is one ecosystem (python/, npm/, docker/, debian/, "
            "tools/, models/) and holds that ecosystem's document folder "
            "projects; a project is <id>/document.md plus its own meta.json and "
            "assets/. The directory is the catalog: an administrator creates "
            "and edits documents in the browser, everyone else reads and "
            "downloads them."
        ),
    )
    npm_dir: str = Field(
        default=catalog_path("npm"),
        description=(
            "Directory holding the local npm catalog — tarballs, an optional "
            "catalog.json overlay, and the publish.json dist-tag sidecar that "
            "`npm publish` (PUT /npm/<package>, npm:publish) maintains. Must be "
            "writable for the publish endpoint to work."
        ),
    )
    npm_upstream: str = Field(
        default="https://registry.npmmirror.com",
        description=(
            "Upstream npm registry this server mirrors. Advertised to clients as "
            "the `npm config set registry` target and, when npm_proxy_enabled is "
            "true, used as the read-through source for packages that are not "
            "already cached locally."
        ),
    )
    npm_proxy_enabled: bool = Field(
        default=True,
        description=(
            "Serve the npm registry protocol: packuments, version manifests, "
            "tarballs and `/-/v1/search`. When false the registry still answers "
            "for locally cached packages, but unknown packages return 404 "
            "instead of being fetched from npm_upstream."
        ),
    )
    npm_upstream_token: str = Field(
        default="",
        description=(
            "Optional bearer token sent to the upstream npm registry, for a "
            "private mirror that requires authentication. Empty means anonymous."
        ),
    )
    npm_timeout: float = Field(
        default=30.0,
        ge=1.0,
        description="Upstream npm registry read timeout in seconds",
    )
    npm_cache_dir: str = Field(
        default=backend_path("data", "cache", "npm"),
        description=(
            "Disk cache for packuments and tarballs fetched from npm_upstream. "
            "Tarballs are content-addressed and survive restarts; packuments are "
            "revalidated, see services/npm_registry.py."
        ),
    )
    npm_cache_max_mb: int = Field(
        default=512,
        ge=0,
        description=(
            "Byte budget for npm_cache_dir, in MiB. Least-recently-used entries "
            "are evicted past it; 0 disables the ceiling (not recommended on a "
            "small disk)."
        ),
    )
    docker_dir: str = Field(
        default=catalog_path("docker-images"),
        description=(
            "Directory holding `docker save` tarballs and compose/Dockerfile "
            "snippets. Named with a suffix so it never collides with the "
            "repository's docker/ infrastructure directory."
        ),
    )
    docker_registry: str = Field(
        default="",
        description=(
            "Optional intranet registry this server fronts (e.g. "
            "http://registry.intra:5000). Advertised in the UI as the value for "
            "`docker login` and as the default docker_upstream."
        ),
    )
    docker_upstream: str = Field(
        default="",
        description=(
            "Registry v2 endpoint this server proxies pulls from, e.g. "
            "https://registry-1.docker.io for Docker Hub or "
            "http://registry.intra:5000 for an intranet registry. Empty means "
            "local-only: manifests and blobs must already be in docker_cache_dir."
        ),
    )
    docker_upstream_username: str = Field(
        default="",
        description="HTTP Basic username for docker_upstream (a private registry)",
    )
    docker_upstream_password: str = Field(
        default="",
        description="HTTP Basic password for docker_upstream",
    )
    docker_default_namespace: str = Field(
        default="library",
        description=(
            "Namespace assumed for a single-segment image name, the way "
            "`docker pull nginx` means `library/nginx` on Docker Hub. Set to an "
            "empty string on a registry that does not namespace official images."
        ),
    )
    docker_timeout: float = Field(
        default=60.0,
        ge=1.0,
        description="Upstream registry read timeout in seconds (blobs can be slow)",
    )
    docker_cache_dir: str = Field(
        default=backend_path("data", "cache", "docker"),
        description=(
            "Disk cache for manifests and blobs fetched from docker_upstream. "
            "Blobs are digest-addressed and therefore immutable."
        ),
    )
    docker_cache_max_mb: int = Field(
        default=1024,
        ge=0,
        description=(
            "Byte budget for docker_cache_dir, in MiB. Image layers are large; "
            "raise it if this host is expected to seed pulls for others, lower it "
            "on a small disk. 0 disables the ceiling."
        ),
    )
    debian_dir: str = Field(
        default=catalog_path("debian"),
        description="Directory holding local .deb files and apt config snippets",
    )
    debian_mirror: str = Field(
        default="",
        description=(
            "Optional intranet apt mirror advertised in the UI (e.g. "
            "http://mirror.intra/debian). Also used as the default "
            "debian_upstream when that is left empty."
        ),
    )
    debian_upstream: str = Field(
        default="",
        description=(
            "apt mirror this server proxies `dists/` and `pool/` from, e.g. "
            "http://deb.debian.org/debian. Empty means local-only: only the flat "
            "`/debian/Packages` index built from debian_dir is served."
        ),
    )
    debian_timeout: float = Field(
        default=60.0,
        ge=1.0,
        description="Upstream apt mirror read timeout in seconds",
    )
    debian_cache_dir: str = Field(
        default=backend_path("data", "cache", "debian"),
        description=(
            "Disk cache for proxied apt metadata (Release, Packages and their "
            "compressed variants). Package files are streamed, not cached."
        ),
    )
    debian_cache_max_mb: int = Field(
        default=256,
        ge=0,
        description="Byte budget for debian_cache_dir, in MiB. 0 disables the ceiling.",
    )
    debian_metadata_ttl: int = Field(
        default=300,
        ge=0,
        description=(
            "How long (seconds) a proxied apt metadata document is trusted before "
            "it is re-fetched. `Release`/`Packages` change on the mirror's own "
            "schedule, so a few minutes of staleness is normal."
        ),
    )
    models_file: str = Field(
        default=backend_path("config", "model_routes.json"),
        description=(
            "JSON file describing the model routes for downstream DSH. Read by "
            "everyone holding `model:read`; written by administrators holding "
            "`model:write` through the routing panel, so the file (and the "
            "directory holding it) must be writable by the server process."
        ),
    )
    model_health_file: str = Field(
        default=backend_path("data", "model_health.json"),
        description=(
            "Where the result of the last connectivity probe of each model "
            "route is remembered, keyed by route name. Kept out of models_file "
            "so the document downstream DSH reads stays a pure route table."
        ),
    )
    model_probe_timeout: float = Field(
        default=5.0,
        ge=0.5,
        description=(
            "Timeout in seconds for one model-route connectivity probe. The "
            "probe only checks that the URL answers; it never sends an "
            "inference request body."
        ),
    )
    device_codes_file: str = Field(
        default=backend_path("data", "device_codes.json"),
        description=(
            "Where pending device-authorization requests (the flow the DSH "
            "enterprise-intranet plugin uses to obtain an API key without a "
            "copy-and-paste) are kept between the plugin's `code` request and "
            "its first successful `token` poll. Device codes are stored "
            "SHA-256-hashed; the file must be writable by the server process "
            "and should not be backed up."
        ),
    )
    device_code_ttl: int = Field(
        default=900,
        ge=60,
        description=(
            "Seconds a pending device-authorization request stays valid before "
            "the plugin must start over."
        ),
    )


__all__ = ["HubConfig"]
