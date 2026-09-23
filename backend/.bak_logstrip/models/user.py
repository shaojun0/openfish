"""User model — the local account that authorization hangs off.

Identity comes from an external authenticator (OAuth2 / 4A / HTTP Basic), so a
row here is a *shadow account*: it holds no credentials of its own.  What it
does hold is the stable key that roles are attached to.

    provider    + external_id   ->  identity  (stable, never reused)
    display_name                    cosmetic, may change at any time

Never key anything off ``display_name``: people get renamed.

``password_hash`` is deliberately present but unused.  If this server is later
promoted from "OAuth2 client" to "identity provider", that column is where the
credential goes — keeping it now avoids a schema migration at that point.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, Integer, String
from sqlalchemy.orm import Mapped

from .base import Base, iso, utcnow


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = Column(Integer, primary_key=True, autoincrement=True)

    # ── Identity ────────────────────────────────────────────────────
    # ``external_id`` is THE identity key and is unique on its own — not
    # (provider, external_id).  Rationale: the same human may reach this
    # server through more than one door (OAuth2/4A login, the HTTP Basic
    # fallback, the ADMIN_USERS bootstrap list), and splitting them into
    # separate rows would mean roles granted through one door are invisible
    # through another.  ``provider`` records which door they were first seen
    # at; it is informational and never used for lookups.
    external_id: Mapped[str] = Column(String(256), nullable=False, unique=True, index=True)
    provider: Mapped[str] = Column(String(32), nullable=False, default="local")
    display_name: Mapped[str | None] = Column(String(128), nullable=True)
    email: Mapped[str | None] = Column(String(256), nullable=True)

    # ── State ───────────────────────────────────────────────────────
    is_active: Mapped[bool] = Column(Boolean, nullable=False, default=True)
    # Escape hatch: a superuser bypasses every permission lookup, so a broken
    # or emptied role table can never lock everyone out of the server.
    is_superuser: Mapped[bool] = Column(Boolean, nullable=False, default=False)

    # ── Reserved for identity-provider mode (unused today) ──────────
    password_hash: Mapped[str | None] = Column(String(256), nullable=True)

    # ── Timestamps ──────────────────────────────────────────────────
    last_login_at: Mapped[datetime | None] = Column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime | None] = Column(
        DateTime(timezone=True), nullable=True, default=utcnow, onupdate=utcnow
    )

    # ── Convenience ─────────────────────────────────────────────────

    @property
    def label(self) -> str:
        """Human-facing name, falling back to the external id."""
        return self.display_name or self.external_id

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "provider": self.provider,
            "external_id": self.external_id,
            "display_name": self.display_name,
            "email": self.email,
            "is_active": bool(self.is_active),
            "is_superuser": bool(self.is_superuser),
            "last_login_at": iso(self.last_login_at),
            "created_at": iso(self.created_at),
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<User {self.id} {self.provider}:{self.external_id}>"


__all__ = ["User"]
