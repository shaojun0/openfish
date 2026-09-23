"""SQLAlchemy declarative base, plus the helpers every table shares.

Models inherit from :class:`Base`; the functions are here rather than in a
model module because several tables need them, and a per-module copy is how the
two used to drift apart.
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


def in_check(column: str, values: tuple[str, ...]) -> str:
    """SQL for ``column IN ('a', 'b', …)`` — the body of a CHECK constraint.

    A vocabulary a table refuses to store is generated from the same tuple the
    services validate against, so the two can never disagree about what the
    column accepts.
    """
    joined = ", ".join(f"'{value}'" for value in values)
    return f"{column} IN ({joined})"


class Base(DeclarativeBase):
    pass


__all__ = ["Base", "in_check", "iso", "utcnow"]
