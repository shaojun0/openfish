"""Auth package — authentication + authorization.

Public API surface::

    from auth.decorators import require_auth, require_permission, require_admin
    from auth.permissions import Permission
    from auth.api_keys import ApiKeyManager
    from auth.oauth import get_authorize_url, exchange_code
    from auth.guards import AUTH_METHODS
"""

from auth.decorators import (
    require_auth,
    require_permission,
    require_admin,
    require_package_read,
    require_package_write,
)
from auth.permissions import Permission
from auth.api_keys import ApiKeyManager
from auth.oauth import get_authorize_url, exchange_code
from auth.guards import AUTH_METHODS

__all__ = [
    "require_auth",
    "require_permission",
    "require_admin",
    "require_package_read",
    "require_package_write",
    "Permission",
    "ApiKeyManager",
    "get_authorize_url",
    "exchange_code",
    "AUTH_METHODS",
]
