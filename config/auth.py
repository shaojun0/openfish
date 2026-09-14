"""Authentication — HTTP Basic, OAuth2, and API key settings."""

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class AuthConfig(BaseSettings):
    # See ServerConfig.model_config — flat .env names are supported, including
    # the backward-compatible aliases AUTH_USERNAME / AUTH_ASSERT.
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── Toggle ──────────────────────────────────────────────────────
    auth_enabled: bool = Field(default=True, description="Enable authentication")
    is_4a: bool = Field(default=False, description="Enable 4A authentication mode")

    # ── HTTP Basic ──────────────────────────────────────────────────
    # Secrets are never committed: provide them via environment variables
    # (AUTH_USERNAME / AUTH_ASSERT) or a local, git-ignored .env file.
    basic_username: str = Field(
        default="",
        alias="auth_username",  # backward-compat env var
        description="HTTP Basic Auth username (env: AUTH_USERNAME)",
    )
    basic_password: str = Field(
        default="",
        alias="auth_assert",  # backward-compat env var
        description="HTTP Basic Auth password (env: AUTH_ASSERT)",
    )

    # ── OAuth2 ──────────────────────────────────────────────────────
    # Leave the URLs empty to disable the OAuth2 code paths entirely.
    oauth2_introspect_url: str = Field(
        default="",
        description="Token introspection endpoint (RFC 7662), e.g. https://auth.example.com/oauth/introspect",
    )
    oauth2_client_id: str = Field(
        default="",
        description="Client ID registered with the OAuth2 provider (env: OAUTH2_CLIENT_ID)",
    )
    oauth2_product_id: int = Field(
        default=0,
        description="Product ID registered with the OAuth2 provider (env: OAUTH2_PRODUCT_ID)",
    )
    oauth2_client_secret: str = Field(
        default="",
        description="Client secret for the OAuth2 provider (env: OAUTH2_CLIENT_SECRET)",
    )
    oauth2_issuer: str = Field(
        default="",
        description="Expected issuer for JWT validation (optional)",
    )
    oauth2_token_url: str = Field(
        default="",
        description="Token endpoint, e.g. https://auth.example.com/oauth/token",
    )
    oauth2_authorize_url: str = Field(
        default="",
        description="OAuth2 authorization endpoint, e.g. https://auth.example.com/oauth/authorize",
    )
    oauth2_auth_preference: str = Field(
        default="",
        description="Optional auth-preference query parameter for 4A-style flows",
    )
    oauth2_scope_read: str = Field(
        default="package:read",
        description="OAuth2 scope for read operations",
    )
    oauth2_scope_write: str = Field(
        default="package:write",
        description="OAuth2 scope for write operations",
    )
