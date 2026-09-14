"""Server identity, network, and admin configuration."""

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class ServerConfig(BaseSettings):
    # Read the project-level .env directly so flat names such as HOST / PORT /
    # SECRET_KEY work for local development as well as inside containers.
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    host: str = Field(default="0.0.0.0", description="Bind address")
    port: int = Field(default=9090, ge=1, le=65535, description="Bind port")
    debug: bool = Field(default=False, description="Flask debug mode")
    secret_key: str = Field(
        default="change-me-in-production",
        description="Flask secret key for session signing",
    )
    server_name: str = Field(default="pypiserver", description="Branding name")
    log_level: str = Field(
        default="INFO",
        description="Logging level (DEBUG, INFO, WARNING, ERROR)",
    )
    route_prefix: str = Field(
        default="",
        description="Global URL prefix for all routes",
    )
    admin_users: list[str] = Field(
        default=[],
        description="Whitelist of admin user identifiers",
    )
    stats_cache_seconds: int = Field(
        default=7200,
        ge=60,
        description="Admin stats background refresh interval in seconds",
    )
