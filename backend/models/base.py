"""SQLAlchemy declarative base, plus the timestamp helpers every table shares.

Models inherit from :class:`Base`; the two functions are here rather than in a
model module because both ``users`` and the RBAC tables need them, and a
per-module copy is how the two used to drift apart.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import DeclarativeBase


def utcnow() -> datetime:
    """Timezone-aware "now" — the default for every timestamp column."""
    return datetime.now(timezone.utc)


def iso(dt: datetime | None) -> str | None:
    """Serialize a datetime to an ISO-8601 string (or ``None``)."""
    return dt.isoformat() if dt is not None else None


class Base(DeclarativeBase):
    pass


__all__ = ["Base", "iso", "utcnow"]
