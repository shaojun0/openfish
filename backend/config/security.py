"""ClamAV virus scanning configuration."""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class SecurityConfig(BaseSettings):
    # See ServerConfig.model_config — flat .env names are supported.
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    clamav_host: str = Field(
        default="",
        description="ClamAV daemon host (leave empty to disable)",
    )
    clamav_port: int = Field(
        default=3310,
        ge=1, le=65535,
        description="ClamAV daemon port",
    )
    clamav_timeout: int = Field(
        default=30,
        ge=1,
        description="ClamAV scan timeout in seconds",
    )
    clamav_required: bool = Field(
        default=False,
        description="If True, reject uploads when ClamAV is unavailable",
    )
