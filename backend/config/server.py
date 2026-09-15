"""Server identity, network, and admin configuration."""

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from config.paths import backend_path


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
    public_base_url: str = Field(
        default="",
        description=(
            "Absolute base URL this server is reached at from the public "
            "internet, e.g. https://47.97.243.86:9443. Used to build the "
            "absolute verification_uri the device-authorization flow hands "
            "back to a non-browser client (the DSH enterprise-intranet plugin). "
            "Empty = derive it from the incoming request's Host / "
            "X-Forwarded-* headers, which is correct behind the shipped nginx "
            "reverse proxy and needs no configuration there."
        ),
    )
    stats_cache_seconds: int = Field(
        default=7200,
        ge=60,
        description="Admin stats background refresh interval in seconds",
    )
    frontend_dist_dir: str = Field(
        default="",
        description=(
            "Directory holding the built Vue SPA (index.html plus assets/). "
            "Empty = <backend>/static/dist, which is where a single-container "
            "build drops it. In the split Docker deployment the SPA is served "
            "by the frontend container instead, so this normally stays empty; "
            "set it to ../frontend/dist to let Flask serve the SPA during local "
            "development without running the Vite dev server."
        ),
    )
    tls_ca_file: str = Field(
        default=backend_path("certs", "ca_chain.pem"),
        description=(
            "Private-CA chain published at GET /certs/ca_chain.pem so intranet "
            "clients can install it. Deliberately outside the web root: exactly "
            "this one file is served, never a directory, so a key dropped next "
            "to it is not reachable."
        ),
    )
