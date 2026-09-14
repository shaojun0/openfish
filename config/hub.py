"""Artifact-hub configuration — tools, npm and model routing.

The server started life as a Python-only package index.  It is now growing into
an intranet artifact hub with a handful of sibling catalogs, each backed by a
directory on disk (or a small JSON file) rather than a database:

* ``tools_dir``    — a directory tree whose sub-directories are *categories*
  and whose files are the downloadable tools themselves.
* ``npm_dir``      — an optional local npm cache/registry directory.  The
  reverse proxy is not wired yet; the catalog is scanned so the UI can show
  what a future ``npm config set registry`` would serve.
* ``models_file``  — the JSON description of the model routes a downstream
  DSH deployment may point at.

Everything is deliberately file-based and configuration-driven: adding a tool
is ``cp``-ing a file into ``tools/<category>/``, and adding a model route is
editing one JSON file.  No schema migration, no restart.
"""

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class HubConfig(BaseSettings):
    # Flat .env names (TOOLS_DIR, NPM_DIR, MODELS_FILE, ...) work as well as
    # the nested HUB__* form — see ServerConfig.model_config.
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    tools_dir: str = Field(
        default="tools",
        description=(
            "Root of the tools catalog. Each immediate sub-directory is a "
            "category; files below a category are downloadable tools."
        ),
    )
    npm_dir: str = Field(
        default="npm",
        description="Directory holding the local npm catalog (tarballs and/or catalog.json)",
    )
    npm_upstream: str = Field(
        default="https://registry.npmmirror.com",
        description=(
            "Upstream npm registry this server is meant to mirror. Published to "
            "the UI as the value for `npm config set registry` once the proxy "
            "lands; it is not proxied yet."
        ),
    )
    models_file: str = Field(
        default="config/model_routes.json",
        description="JSON file describing the model routes for downstream DSH",
    )


__all__ = ["HubConfig"]
