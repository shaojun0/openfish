"""Access control API — roles, permission points and accounts.

Backs the SPA's access panel.  Everything here writes to the five RBAC tables
(``users`` / ``roles`` / ``permissions`` / ``user_roles`` / ``role_permissions``)
through :class:`services.authz.AuthzService`.

Every route on this blueprint sits behind ``admin:roles`` (attached as a
blueprint-wide ``before_request`` guard in ``routes/__init__.py``), which is a
narrower grant than ``admin:view``: being able to read the dashboard does not
mean being able to edit who may do what.

One escalation guard is enforced in code rather than by a permission, because
no permission level should be able to hand out the bypass:

* only an existing **superuser** may set the ``is_superuser`` flag, and never
  in a way that would leave the server with zero superusers.

Every mutating route binds its body as a view parameter (``body: …Request``)
instead of reading ``request.get_json`` by hand — the models are the ones the
OpenAPI document has always advertised for these operations.  ``@validate_request()``
sits *below* the blueprint-wide ``admin:roles`` guard and the per-view
``require_permission`` where present, so an unauthorized caller gets
``401``/``403`` and is never answered by the binder.
"""

from __future__ import annotations


from flask import jsonify
from flask_openapi3 import APIBlueprint, validate_request

from auth.decorators import current_principal
from errors import BadRequestError, ForbiddenError
from openapi import api_operation, array_of, errors, json_body, ok
from schemas import (
    CreateRoleRequest,
    GrantRoleRequest,
    SetRolePermissionsRequest,
    SetSuperuserRequest,
    UserListQuery,
)
from services.authz import AuthzService

access_bp = APIBlueprint("access", __name__)

#: Statuses every route here can return, on top of the specific ones.
_PROTECTED = ("401", "403", "500")


def _errors(*extra: str) -> dict:
    """Standard error responses plus *extra*, in a stable (sorted) order.

    Sorted on purpose: the OpenAPI document is generated from this dict, and an
    unstable key order would make the rendered spec differ between runs.
    """
    return errors(*sorted({*_PROTECTED, *extra}))


def _authz() -> AuthzService:
    from flask import current_app

    authz = current_app.extensions.get("authz")
    if authz is None:
        raise ForbiddenError(message="Authorization service unavailable")
    return authz


def _actor() -> str:
    """Who is making this change, for the audit trail in the logger."""
    return (current_principal() or {}).get("sub") or "unknown"


# ══════════════════════════════════════════════════════════════════════
#  Permission points
# ══════════════════════════════════════════════════════════════════════

@access_bp.route("/permissions")
@api_operation(
    summary="List permission points",
    description=(
        "Every permission point the server knows about, with the number of roles "
        "holding it. A point with `role_count: 0` cannot be reached by any role — "
        "usually a typo in a route guard, or a grant somebody removed.\n\n"
        "Three drift flags ride along, because `role_count` alone cannot see them: "
        "`stale` (the row survives a rename but no guard declares it any more), "
        "`expected_for_authenticated` vs `held_by_authenticated`, and their "
        "combination `authenticated_pending` — a point ordinary users are supposed "
        "to have but the `authenticated` role was never migrated to, which is "
        "invisible in `role_count` because the `admin` role is topped up "
        "automatically."
    ),
    tags=["Access control"],
    responses={"200": ok("Permission points", array_of("PermissionInfo")), **_errors()},
)
def list_permissions():
    return jsonify(_authz().list_permissions())


# ══════════════════════════════════════════════════════════════════════
#  Roles
# ══════════════════════════════════════════════════════════════════════

@access_bp.route("/roles")
@api_operation(
    summary="List roles",
    description=(
        "Every role with the permission codes it holds and how many accounts "
        "carry it. Built-in roles (`admin`, `authenticated`, `anonymous`) cannot "
        "be deleted; `auto_grant` roles are handed to every new account, and "
        "`is_anonymous_default` roles apply to requests that never authenticated."
    ),
    tags=["Access control"],
    responses={"200": ok("Roles", array_of("RoleInfo")), **_errors()},
)
def list_roles():
    return jsonify(_authz().list_roles())


@access_bp.post("/roles")
@validate_request()
@api_operation(
    summary="Create a role",
    description=(
        "Creates an empty role. Grant it permission points with "
        "`PUT /roles/{role_id}/permissions` and hand it to accounts with "
        "`POST /users/{user_id}/roles`."
    ),
    tags=["Access control"],
    request_body={"required": True, "content": json_body("CreateRoleRequest")},
    responses={"201": ok("Role created", "RoleInfo"), **_errors("400", "409")},
)
def create_role(body: CreateRoleRequest):
    try:
        role = _authz().create_role(
            code=body.code,
            name=body.name or body.code,
            description=body.description,
        )
    except ValueError as exc:
        raise BadRequestError(str(exc)) from exc
    return jsonify(role), 201


@access_bp.route("/roles/<int:role_id>", methods=["DELETE"])
@api_operation(
    summary="Delete a role",
    description=(
        "Deletes a role and every grant that referenced it. Built-in roles are "
        "refused with HTTP 400 — rename or re-scope them instead."
    ),
    tags=["Access control"],
    responses={"200": ok("Role deleted", "DeleteResult"), **_errors("400", "404")},
)
def delete_role(role_id: int):
    try:
        _authz().delete_role(role_id)
    except ValueError as exc:
        raise BadRequestError(str(exc)) from exc
    return jsonify({"deleted": role_id})


@access_bp.put("/roles/<int:role_id>/permissions")
@validate_request()
@api_operation(
    summary="Replace a role's permissions",
    description=(
        "Sets the role's permission points wholesale. Send the complete list — "
        "anything omitted is revoked. Codes that do not exist yet are created as "
        "new points, which lets an administrator define a permission before the "
        "route that checks it ships."
    ),
    tags=["Access control"],
    request_body={"required": True, "content": json_body("SetRolePermissionsRequest")},
    responses={"200": ok("Role updated", "RoleInfo"), **_errors("400", "404")},
)
def set_role_permissions(role_id: int, body: SetRolePermissionsRequest):
    # `permissions` is `list[str]` in the model, which is exactly what the view
    # used to re-check by hand.
    authz = _authz()
    try:
        authz.set_role_permissions(role_id, body.permissions)
    except ValueError as exc:
        raise BadRequestError(str(exc)) from exc
    role = next((r for r in authz.list_roles() if r["id"] == role_id), None)
    if role is None:
        raise BadRequestError("role not found")
    return jsonify(role)


# ══════════════════════════════════════════════════════════════════════
#  Accounts
# ══════════════════════════════════════════════════════════════════════

@access_bp.get("/users")
@validate_request()
@api_operation(
    summary="List accounts",
    description=(
        "Local accounts and the roles they hold. An account is created "
        "automatically the first time somebody authenticates, so it can be "
        "granted a role before that person ever logs in — which is what makes "
        "`python cli.py create-admin <id>` work."
    ),
    tags=["Access control"],
    parameters=[
        {
            "name": "limit",
            "in": "query",
            "required": False,
            "description": "Accounts to return, clamped to 1..1000 (default 200).",
            "schema": {"type": "integer", "default": 200},
        },
        {
            "name": "offset",
            "in": "query",
            "required": False,
            "description": "Accounts to skip; negatives are clamped to 0.",
            "schema": {"type": "integer", "default": 0},
        },
    ],
    responses={"200": ok("Accounts", array_of("UserInfo")), **_errors()},
)
def list_users(query: UserListQuery):
    # The bounds stay in the view rather than on the model: a `?limit=5000` is
    # clamped, not a 400 — the same choice `/-/v1/search` makes.
    limit = 200 if query.limit is None else query.limit
    offset = 0 if query.offset is None else query.offset
    return jsonify(_authz().list_users(limit=max(1, min(limit, 1000)), offset=max(0, offset)))


@access_bp.post("/users/<int:user_id>/roles")
@validate_request()
@api_operation(
    summary="Grant a role",
    description="Idempotent: granting a role the account already holds returns `granted: false`.",
    tags=["Access control"],
    request_body={"required": True, "content": json_body("GrantRoleRequest")},
    responses={"200": ok("Grant applied"), **_errors("400", "404")},
)
def grant_role(user_id: int, body: GrantRoleRequest):
    code = body.role.strip()
    if not code:
        raise BadRequestError("`role` is required")
    try:
        granted = _authz().grant_role(user_id, code)
    except ValueError as exc:
        raise BadRequestError(str(exc)) from exc
    if granted:
        pass
    return jsonify({"user_id": user_id, "role": code, "granted": granted})


@access_bp.route("/users/<int:user_id>/roles/<role_code>", methods=["DELETE"])
@api_operation(
    summary="Revoke a role",
    description="Removes one role grant. Returns `revoked: false` when it was not held.",
    tags=["Access control"],
    responses={"200": ok("Grant removed"), **_errors("404")},
)
def revoke_role(user_id: int, role_code: str):
    revoked = _authz().revoke_role(user_id, role_code)
    if revoked:
        pass
    return jsonify({"user_id": user_id, "role": role_code, "revoked": revoked})


@access_bp.put("/users/<int:user_id>/superuser")
@validate_request()
@api_operation(
    summary="Set the superuser flag",
    description=(
        "Superusers bypass every permission check, so this one operation is not "
        "available to `admin:roles` alone — the caller must already be a "
        "superuser. The last remaining superuser cannot be demoted; the server "
        "would otherwise be impossible to administer."
    ),
    tags=["Access control"],
    request_body={"required": True, "content": json_body("SetSuperuserRequest")},
    responses={"200": ok("Flag updated"), **_errors("400", "404")},
)
def set_superuser(user_id: int, body: SetSuperuserRequest):
    principal = current_principal() or {}
    if not principal.get("is_superuser"):
        raise ForbiddenError(
            message="Only a superuser may grant or revoke the superuser flag"
        )

    value = body.superuser
    authz = _authz()
    if not value and authz.count_superusers() <= 1:
        raise BadRequestError(
            "refusing to demote the last superuser — the server would have no "
            "administrator left. Promote somebody else first."
        )

    try:
        authz.set_superuser(user_id, value)
    except ValueError as exc:
        raise BadRequestError(str(exc)) from exc
    return jsonify({"user_id": user_id, "is_superuser": value})
