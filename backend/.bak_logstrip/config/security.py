"""ClamAV virus scanning and GuardDog malware scanning configuration."""

from __future__ import annotations

from pydantic import Field

from config.base import EnvSettings
from config.paths import backend_path


class SecurityConfig(EnvSettings):
    """ClamAV virus scanning and GuardDog malware scanning configuration."""

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
    guarddog_enabled: bool = Field(
        default=True,
        description=(
            "Run GuardDog (YARA rules + risk engine) over every uploaded "
            "distribution. Unavailable outside Linux/macOS because its "
            "``nono-py`` dependency ships no Windows wheel."
        ),
    )
    guarddog_required: bool = Field(
        default=False,
        description="If True, reject uploads when GuardDog is unavailable",
    )
    guarddog_min_risk_score: float = Field(
        default=5.0,
        ge=0.0, le=10.0,
        description=(
            "Reject an upload whose GuardDog risk score reaches this value. "
            "5.0 is GuardDog's own 'suspicious' band: below it lie the "
            "capability matches (a package that merely opens a socket) that "
            "legitimate libraries trip all the time."
        ),
    )
    guarddog_cache_dir: str = Field(
        default=backend_path("data", "guarddog"),
        description=(
            "Writable directory holding GuardDog's pinned 'top packages' "
            "lists. GuardDog refreshes them from the internet at import time "
            "whenever the cached copy is older than 30 days; the pinned copies "
            "keep an air-gapped deployment offline."
        ),
    )
