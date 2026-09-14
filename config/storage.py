"""Package storage and index configuration."""

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class StorageConfig(BaseSettings):
    # See ServerConfig.model_config — flat .env names are supported.
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    packages_dir: str = Field(
        default="packages",
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
        default="python-build-standalone",
        description="Path to python-build-standalone releases directory",
    )
    api_keys_file: str = Field(
        default="data/cpypiserver.db",
        description="SQLite database path for API keys and stats",
    )
