"""Auth package — authentication + authorization.

Public API surface::

    from auth.decorators import require_auth, require_permission, require_admin
    from auth.permissions import PACKAGE_WRITE
    from auth.api_keys import ApiKeyManager
    from auth.oauth import get_authorize_url, exchange_code
    from auth.guards import AUTH_METHODS

Authorization itself lives in :class:`services.authz.AuthzService` — there is
no role -> permission mapping in this package by design.
"""

from auth.decorators import (
    can,
    current_display_name,
    current_principal,
    current_sub,
    current_user_id,
    require_admin,
    require_auth,
    require_package_read,
    require_package_write,
    require_permission,
)
from auth import permissions
from auth.api_keys import ApiKeyManager
from auth.oauth import exchange_code, get_authorize_url, identity_from_info
from auth.guards import AUTH_METHODS

__all__ = [
    # guards / decorators
    "require_auth",
    "require_permission",
    "require_admin",
    "require_package_read",
    "require_package_write",
    "current_principal",
    "current_user_id",
    "current_sub",
    "current_display_name",
    "can",
    # permission points
    "permissions",
    # services
    "ApiKeyManager",
    "get_authorize_url",
    "exchange_code",
    "identity_from_info",
    "AUTH_METHODS",
]
