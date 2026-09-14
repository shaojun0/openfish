"""RBAC tables — roles, permission points, and the two link tables.

This is the classic five-table RBAC shape:

    users ──< user_roles >── roles ──< role_permissions >── permissions

Split of authority (this is the important part):

* ``permissions``  — permission *points*.  Rows are seeded from the codes the
  route guards actually check, so the code declares "these exist"; the database
  owns their display name, grouping and description.
* ``roles``        — fully database-owned.  There is deliberately **no**
  role -> permission mapping anywhere in Python.
* ``user_roles`` /
  ``role_permissions`` — the actual grants.  Editing these is a database write,
  never a code change.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped

from .base import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


class Role(Base):
    __tablename__ = "roles"

    id: Mapped[int] = Column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = Column(String(64), nullable=False, unique=True, index=True)
    name: Mapped[str] = Column(String(128), nullable=False)
    description: Mapped[str | None] = Column(Text, nullable=True)

    # Built-in roles (admin / authenticated / anonymous) cannot be deleted.
    is_builtin: Mapped[bool] = Column(Boolean, nullable=False, default=False)

    # Granted to requests that never authenticated (only reachable when
    # AUTH_ENABLED=false).  Replaces the old hard-coded "anonymous" role.
    is_anonymous_default: Mapped[bool] = Column(Boolean, nullable=False, default=False)

    # Granted automatically to every user the moment their account is created.
    # This is how "everyone who logs in may publish" is expressed without
    # hard-coding the word "authenticated" in Python.
    auto_grant: Mapped[bool] = Column(Boolean, nullable=False, default=False)

    created_at: Mapped[datetime] = Column(DateTime(timezone=True), nullable=False, default=utcnow)

    def to_dict(self, *, permission_codes: list[str] | None = None,
                user_count: int | None = None) -> dict:
        d = {
            "id": self.id,
            "code": self.code,
            "name": self.name,
            "description": self.description,
            "is_builtin": bool(self.is_builtin),
            "is_anonymous_default": bool(self.is_anonymous_default),
            "auto_grant": bool(self.auto_grant),
            "created_at": iso(self.created_at),
        }
        if permission_codes is not None:
            d["permissions"] = permission_codes
        if user_count is not None:
            d["user_count"] = user_count
        return d

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Role {self.code}>"


class Permission(Base):
    """A permission *point* — e.g. ``package:write``.

    Rows are upserted at startup from the codes the guards declare.  An admin
    may rename / re-describe / re-group a row; they may also add one, which
    simply stays inert until some route checks that code.
    """

    __tablename__ = "permissions"

    id: Mapped[int] = Column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = Column(String(64), nullable=False, unique=True, index=True)
    name: Mapped[str] = Column(String(128), nullable=False)
    module: Mapped[str | None] = Column(String(32), nullable=True, index=True)
    description: Mapped[str | None] = Column(Text, nullable=True)
    created_at: Mapped[datetime] = Column(DateTime(timezone=True), nullable=False, default=utcnow)

    def to_dict(self, *, role_count: int | None = None) -> dict:
        d = {
            "id": self.id,
            "code": self.code,
            "name": self.name,
            "module": self.module,
            "description": self.description,
        }
        if role_count is not None:
            d["role_count"] = role_count
        return d

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Permission {self.code}>"


class UserRole(Base):
    """Grant of a role to a user.  Composite primary key — no surrogate id."""

    __tablename__ = "user_roles"

    user_id: Mapped[int] = Column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    role_id: Mapped[int] = Column(
        Integer, ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True
    )
    granted_by: Mapped[int | None] = Column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    granted_at: Mapped[datetime] = Column(DateTime(timezone=True), nullable=False, default=utcnow)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<UserRole user={self.user_id} role={self.role_id}>"


class RolePermission(Base):
    """Grant of a permission point to a role."""

    __tablename__ = "role_permissions"

    role_id: Mapped[int] = Column(
        Integer, ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True
    )
    permission_id: Mapped[int] = Column(
        Integer, ForeignKey("permissions.id", ondelete="CASCADE"), primary_key=True
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<RolePermission role={self.role_id} perm={self.permission_id}>"


__all__ = ["Role", "Permission", "UserRole", "RolePermission", "utcnow", "iso"]
