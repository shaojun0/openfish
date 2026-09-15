"""Application configuration — nested domain models.

Usage::

    from config import settings
    print(settings.server.port)
    print(settings.auth.basic_username)
    print(settings.storage.packages_dir)
    print(settings.security.clamav_host)

Environment variables use ``__`` as delimiter::

    SERVER__HOST=0.0.0.0
    AUTH__BASIC_USERNAME=admin
    STORAGE__PACKAGES_DIR=/data/packages
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict

from config.server import ServerConfig
from config.storage import StorageConfig
from config.auth import AuthConfig
from config.security import SecurityConfig
from config.hub import HubConfig


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        extra="ignore",
    )

    server: ServerConfig = ServerConfig()
    storage: StorageConfig = StorageConfig()
    auth: AuthConfig = AuthConfig()
    security: SecurityConfig = SecurityConfig()
    hub: HubConfig = HubConfig()

    # ── Convenience aliases (backward-compat with Flask config) ─────
    @property
    def debug(self) -> bool:
        return self.server.debug

    @property
    def secret_key(self) -> str:
        return self.server.secret_key

    @property
    def max_content_length(self) -> int:
        return self.storage.max_content_length

    @property
    def packages_dir(self) -> str:
        return self.storage.packages_dir


settings = Settings()
