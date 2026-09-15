"""Auth package — authentication + authorization.

The modules are the API; import from the one that defines what you need::

    from auth.decorators import require_auth, require_permission, require_admin
    from auth.permissions import PACKAGE_WRITE
    from auth.api_keys import ApiKeyManager
    from auth.oauth import get_authorize_url, exchange_code
    from auth.guards import AUTH_METHODS

Authorization itself lives in :class:`services.authz.AuthzService` — there is
no role -> permission mapping in this package by design.
"""
