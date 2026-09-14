"""SQLAlchemy models for API keys and usage statistics."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, relationship

from .base import Base


class ApiKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[str] = Column(String(32), primary_key=True)                     # e.g. k_abc123def456
    name: Mapped[str] = Column(String(128), nullable=False, index=True)        # human-readable label
    prefix: Mapped[str] = Column(String(16), nullable=False)                   # first 12 chars of raw key + "…"
    hash: Mapped[str] = Column(String(64), nullable=False, unique=True, index=True)  # SHA256 of raw key
    created_by: Mapped[str] = Column(String(256), nullable=False, index=True)  # OAuth2 sub / username
    created_at: Mapped[str] = Column(String(32), nullable=False)               # ISO 8601 UTC

    # ── Expiration ──────────────────────────────────────────────────
    # NULL → permanent (never expires); otherwise a UTC ISO timestamp
    expires_at: Mapped[str | None] = Column(String(32), nullable=True, default=None)

    # ── Activity tracking ───────────────────────────────────────────
    last_used: Mapped[str | None] = Column(String(32), nullable=True)          # ISO 8601 UTC

    # ── Relationship to stats ───────────────────────────────────────
    stats: Mapped[list[ApiKeyStats]] = relationship(
        "ApiKeyStats",
        back_populates="api_key",
        cascade="all, delete-orphan",
        lazy="selectin",
    )

    def is_expired(self) -> bool:
        """Check whether this key has expired at the current UTC time."""
        if self.expires_at is None:
            return False
        try:
            expires = datetime.fromisoformat(self.expires_at)
            return datetime.now(timezone.utc) > expires
        except ValueError:
            return False

    def to_dict(self, *, include_hash: bool = False) -> dict:
        """Serialize to dict, optionally including the hash."""
        expired = self.is_expired()
        d = {
            "id": self.id,
            "name": self.name,
            "prefix": self.prefix,
            "created_by": self.created_by,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "is_permanent": self.expires_at is None,
            "is_expired": expired,
            "last_used": self.last_used,
            "download_count": self.get_download_count(),
            "upload_count": self.get_upload_count(),
        }
        if include_hash:
            d["hash"] = self.hash
        return d

    def get_download_count(self) -> int:
        """Sum of 'download' event_type counts from stats."""
        return sum(s.count for s in self.stats if s.event_type == "download")

    def get_upload_count(self) -> int:
        """Sum of 'upload' event_type counts from stats."""
        return sum(s.count for s in self.stats if s.event_type == "upload")


class ApiKeyStats(Base):
    """Per-package usage statistics for an API key.

    One row per (key_id, package_name, event_type) combination.
    Counts are incremented atomically via SQL UPDATE.
    """

    __tablename__ = "api_key_stats"
    __table_args__ = (
        Index("ix_stats_key_pkg_event", "key_id", "package_name", "event_type", unique=True),
    )

    id: Mapped[int] = Column(Integer, primary_key=True, autoincrement=True)
    key_id: Mapped[str] = Column(
        String(32), ForeignKey("api_keys.id", ondelete="CASCADE"), nullable=False, index=True
    )
    package_name: Mapped[str] = Column(String(256), nullable=False, index=True)
    event_type: Mapped[str] = Column(String(16), nullable=False)  # "download" | "upload"
    count: Mapped[int] = Column(Integer, nullable=False, default=0)

    api_key: Mapped[ApiKey] = relationship("ApiKey", back_populates="stats")
