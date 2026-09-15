"""Package storage and index configuration."""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from config.paths import backend_path, catalog_path


class StorageConfig(BaseSettings):
    # See ServerConfig.model_config — flat .env names are supported.
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    packages_dir: str = Field(
        default=backend_path("packages"),
        description="Path to the packages directory",
    )
    overwrite: bool = Field(
        default=False,
        description="Allow overwriting already-uploaded packages",
    )
    max_content_length: int = Field(
        default=100 * 1024 * 1024,
        ge=1,
        description="Maximum upload body size in bytes",
    )
    allow_extensions: list[str] = Field(
        default=["whl", "zip", "tar.gz", "tar"],
        description="Allowed file extensions",
    )
    watch_packages: bool = Field(
        default=True,
        description="Use watchdog to maintain in-memory index",
    )
    python_builds_dir: str = Field(
        default=catalog_path("python-build-standalone"),
        description="Path to python-build-standalone releases directory",
    )
    node_builds_dir: str = Field(
        default=catalog_path("node-builds"),
        description=(
            "Path to a nodejs.org/dist-shaped mirror of prebuilt Node.js "
            "archives (release directories named vX.Y.Z), consumed by nvm, fnm "
            "and node-gyp through NODEJS_ORG_MIRROR"
        ),
    )
    database_url: str | None = Field(
        default=None,
        description=(
            "SQLAlchemy database URL for API keys, users, RBAC and statistics. "
            "Empty (the default) keeps the historical single-file SQLite "
            "database at API_KEYS_FILE. Set it to a PostgreSQL URL — e.g. "
            "postgresql+psycopg://user:pass@db:5432/openfish — to run the whole "
            "authorization/API-key/statistics layer on PostgreSQL instead. "
            "A bare postgresql:// URL is upgraded to the psycopg (v3) driver."
        ),
    )
    api_keys_file: str = Field(
        default=backend_path("data", "cpypiserver.db"),
        description=(
            "SQLite database path for API keys and stats. Used only when "
            "DATABASE_URL is empty; it is the file that is migrated in place by "
            "the lightweight ALTERs in extensions/database.py."
        ),
    )
