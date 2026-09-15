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
"""

from __future__ import annotations

import logging

from flask import Blueprint, jsonify, request

from auth.decorators import current_principal
from errors import BadRequestError, ForbiddenError
from openapi import api_operation, array_of, errors, json_body, ok
from services.authz import AuthzService

access_bp = Blueprint("access", __name__)
_log = logging.getLogger("cpypiserver.access")

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


def _body() -> dict:
    return request.get_json(silent=True) or {}


def _actor() -> str:
    """Who is making this change, for the audit trail in the log."""
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


@access_bp.route("/roles", methods=["POST"])
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
def create_role():
    data = _body()
    try:
        role = _authz().create_role(
            code=data.get("code", ""),
            name=data.get("name") or data.get("code", ""),
            description=data.get("description"),
        )
    except ValueError as exc:
        raise BadRequestError(str(exc)) from exc
    _log.info("access: %s created role %r", _actor(), role["code"])
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
    _log.info("access: %s deleted role id=%s", _actor(), role_id)
    return jsonify({"deleted": role_id})


@access_bp.route("/roles/<int:role_id>/permissions", methods=["PUT"])
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
def set_role_permissions(role_id: int):
    codes = _body().get("permissions")
    if not isinstance(codes, list) or not all(isinstance(c, str) for c in codes):
        raise BadRequestError("`permissions` must be a list of permission codes")
    authz = _authz()
    try:
        authz.set_role_permissions(role_id, codes)
    except ValueError as exc:
        raise BadRequestError(str(exc)) from exc
    role = next((r for r in authz.list_roles() if r["id"] == role_id), None)
    if role is None:
        raise BadRequestError("role not found")
    _log.info("access: %s set role %r permissions to %s",
              _actor(), role["code"], sorted(role["permissions"]))
    return jsonify(role)


# ══════════════════════════════════════════════════════════════════════
#  Accounts
# ══════════════════════════════════════════════════════════════════════

@access_bp.route("/users")
@api_operation(
    summary="List accounts",
    description=(
        "Local accounts and the roles they hold. An account is created "
        "automatically the first time somebody authenticates, so it can be "
        "granted a role before that person ever logs in — which is what makes "
        "`python cli.py create-admin <id>` work."
    ),
    tags=["Access control"],
    responses={"200": ok("Accounts", array_of("UserInfo")), **_errors()},
)
def list_users():
    limit = request.args.get("limit", default=200, type=int)
    offset = request.args.get("offset", default=0, type=int)
    return jsonify(_authz().list_users(limit=max(1, min(limit, 1000)), offset=max(0, offset)))


@access_bp.route("/users/<int:user_id>/roles", methods=["POST"])
@api_operation(
    summary="Grant a role",
    description="Idempotent: granting a role the account already holds returns `granted: false`.",
    tags=["Access control"],
    request_body={"required": True, "content": json_body("GrantRoleRequest")},
    responses={"200": ok("Grant applied"), **_errors("400", "404")},
)
def grant_role(user_id: int):
    code = (_body().get("role") or "").strip()
    if not code:
        raise BadRequestError("`role` is required")
    try:
        granted = _authz().grant_role(user_id, code)
    except ValueError as exc:
        raise BadRequestError(str(exc)) from exc
    if granted:
        _log.info("access: %s granted role %r to user id=%s", _actor(), code, user_id)
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
        _log.info("access: %s revoked role %r from user id=%s",
                  _actor(), role_code, user_id)
    return jsonify({"user_id": user_id, "role": role_code, "revoked": revoked})


@access_bp.route("/users/<int:user_id>/superuser", methods=["PUT"])
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
def set_superuser(user_id: int):
    principal = current_principal() or {}
    if not principal.get("is_superuser"):
        raise ForbiddenError(
            message="Only a superuser may grant or revoke the superuser flag"
        )

    value = _body().get("superuser")
    if not isinstance(value, bool):
        raise BadRequestError("`superuser` must be a boolean")

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
    _log.warning("access: %s set is_superuser=%s on user id=%s",
                 _actor(), value, user_id)
    return jsonify({"user_id": user_id, "is_superuser": value})
