"""SQLAlchemy models for cpypiserver.

Importing this package registers every table on ``Base.metadata`` — keep it
that way, because ``create_all`` only creates what has been imported.
"""

from .base import Base
from .api_key import ApiKey, ApiKeyStats
from .rbac import Permission, Role, RolePermission, UserRole
from .user import User

__all__ = [
    "Base",
    "ApiKey",
    "ApiKeyStats",
    "User",
    "Role",
    "Permission",
    "UserRole",
    "RolePermission",
]
