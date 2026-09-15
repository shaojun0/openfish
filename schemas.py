"""Pydantic request models, response models, and shared dataclasses.

The response models below are the single source of truth for `/openapi.json`.
They are also used to validate real responses in the end-to-end check, so a
view that changes shape fails that check instead of silently drifting away
from its published contract.
"""

from dataclasses import dataclass
from typing import Optional

from pydantic import BaseModel, Field


class FormatQuery(BaseModel):
    """Optional ``?format=json`` on the simple API."""

    format: str | None = Field(default=None, pattern=r"^(json)?$")


# ── Package file ─────────────────────────────────────────────────────

@dataclass
class PackageFile:
    filename: str
    path: str
    url: str
    package_name: str
    version: str
    packagetype: str = "sdist"
    python_version: str = "source"
    requires_python: Optional[str] = None
    size: int = 0
    upload_time: Optional[str] = None
    md5_digest: Optional[str] = None
    sha256_digest: Optional[str] = None


# ── Python-build file ────────────────────────────────────────────────

@dataclass
class PythonBuildFile:
    filename: str
    path: str
    release_tag: str       # e.g. "20260602"
    flavor: str            # "cpython"
    version: str           # e.g. "3.12.13"
    full_version: str      # e.g. "cpython-3.12.13+20260602"
    target_triple: str     # e.g. "aarch64-unknown-linux-gnu"
    variant: str           # e.g. "install_only_stripped"
    extension: str         # e.g. "tar.gz"
    size: int = 0
    sha256_digest: Optional[str] = None


# ── Node-build file ──────────────────────────────────────────────────

@dataclass
class NodeBuildFile:
    filename: str
    path: str
    release_tag: str       # e.g. "v20.11.0"
    version: str           # e.g. "20.11.0"
    platform: str          # "linux" | "darwin" | "win" | "headers" | ...
    arch: str              # "x64" | "arm64" | ... — empty for `headers`
    extension: str         # e.g. "tar.xz"
    size: int = 0
    sha256_digest: Optional[str] = None


# ── Sort helpers ─────────────────────────────────────────────────────

OS_ORDER = {
    "apple-darwin": "macOS",
    "unknown-linux-gnu": "Linux (glibc)",
    "unknown-linux-musl": "Linux (musl)",
    "pc-windows-msvc": "Windows",
}

ARCH_ORDER = {
    "x86_64": 0, "aarch64": 1, "armv7": 2,
    "i686": 3, "ppc64le": 4, "riscv64": 5, "s390x": 6,
}

VARIANT_ORDER = {
    "install_only_stripped": 0, "install_only": 1,
    "pgo+lto-full": 2, "lto-full": 3,
    "pgo-full": 4, "debug-full": 5, "noopt-full": 6,
}


# ── Response models ──────────────────────────────────────────────────
# Every shape the JSON API can return.  Kept next to the request models so the
# whole wire contract lives in one file.

class ErrorResponse(BaseModel):
    """Body of every JSON error response."""

    error: str = Field(description="Human readable failure reason")


class SessionInfo(BaseModel):
    """Result of `GET /api/v1/session`.

    Returned with HTTP 200 even for an anonymous caller, so a client can render
    a signed-out state without treating it as an error.
    """

    authenticated: bool = Field(description="Whether a usable credential was presented")
    auth_enabled: bool = Field(description="Whether this deployment enforces authentication")
    user: str | None = Field(
        description="Stable account id of the credential (never a display name), or null"
    )
    display_name: str | None = Field(
        default=None, description="Human-facing name for `user`, when known"
    )
    role: str = Field(description="Primary role code — admin | authenticated | anonymous | custom")
    roles: list[str] = Field(
        default_factory=list, description="Every role code the account holds"
    )
    permissions: list[str] = Field(
        description="Effective permission strings, resolved from the role tables"
    )
    server_name: str = Field(description="Configured branding name")
    is_admin: bool = Field(description="Shorthand for the admin:view permission")
    is_superuser: bool = Field(
        default=False,
        description="True when the account bypasses permission checks entirely",
    )
    auth_method: str | None = Field(description="Which credential was accepted")


class KeyStatRow(BaseModel):
    """One (package, event) bucket of API key usage."""

    package_name: str
    event_type: str = Field(description="download | upload")
    count: int


class KeyStats(BaseModel):
    """Per-key usage breakdown."""

    key_id: str
    total_downloads: int
    total_uploads: int
    per_package: list[KeyStatRow]


class ApiKey(BaseModel):
    """An issued API key.  The secret itself is never stored or returned here."""

    id: str = Field(description="Stable identifier, e.g. k_9f2c1a4b7d3e")
    name: str = Field(description="Human readable label")
    prefix: str = Field(description="First characters of the raw key, for identification")
    created_by: str
    created_at: str = Field(description="ISO 8601 UTC")
    expires_at: str | None = Field(description="ISO 8601 UTC, or null when permanent")
    is_permanent: bool
    is_expired: bool
    last_used: str | None = Field(description="ISO 8601 UTC, or null if never used")
    download_count: int
    upload_count: int
    stats_detail: KeyStats | None = None


class CreatedApiKey(ApiKey):
    """Response of `POST /api/v1/keys` — the only time the secret is shown."""

    key: str = Field(
        description="Raw API key. Returned exactly once; only its SHA-256 is stored.",
    )


class CreateKeyRequest(BaseModel):
    """Body of `POST /api/v1/keys`."""

    name: str = Field(
        min_length=1,
        max_length=128,
        description="Human readable label, e.g. 'ci-pipeline'",
    )
    expires_in_days: int | None = Field(
        default=None,
        description=(
            "Lifetime in days. Omit, or send null / a non-positive value, for a "
            "key that never expires."
        ),
    )


class DeleteResult(BaseModel):
    """Body of a successful delete."""

    deleted: str = Field(description="Identifier of the removed resource")


class PackageSummary(BaseModel):
    """One package in `GET /api/v1/packages`."""

    name: str = Field(description="Normalised (PEP 503) package name")
    file_count: int
    total_size: int = Field(description="Bytes")
    total_size_human: str = Field(description="Pre-formatted, e.g. '12.4 MB'")
    download_count: int
    upload_count: int


class AdminOverview(BaseModel):
    """Aggregate counters across the whole server."""

    package_count: int
    file_count: int
    total_storage: int
    total_storage_human: str
    total_keys: int
    active_keys: int
    total_downloads: int
    total_uploads: int


class AdminKeyEntry(BaseModel):
    """One API key as summarised in the admin statistics."""

    id: str
    name: str
    prefix: str
    created_by: str
    download_count: int
    upload_count: int
    last_used: str = ""
    created_at: str = ""
    expires_at: str = ""
    is_expired: bool
    is_permanent: bool


class AdminStats(BaseModel):
    """Result of `GET /api/v1/admin/stats`."""

    overview: AdminOverview
    packages: list[PackageSummary]
    keys: list[AdminKeyEntry]


class HealthInfo(BaseModel):
    """Result of `GET /health`."""

    status: str
    server: str
    package_count: int
    packages_dir: str


class BuildMirrorHealth(BaseModel):
    """Result of `GET /python-builds/health`."""

    status: str = Field(description="ok | disabled")
    reason: str | None = Field(default=None, description="Present when disabled")
    builds_dir: str | None = None
    releases: int | None = None
    files: int | None = None
    total_size: int | None = None
    total_size_human: str | None = None
    flavors: list[str] | None = None
    versions: list[str] | None = None


class BuildChecksum(BaseModel):
    """Result of `GET /python-builds/<tag>/<filename>/sha256`."""

    filename: str
    release_tag: str
    version: str
    sha256: str
    size: int


class SimpleProject(BaseModel):
    """One project in the PEP 691 flat project list."""

    name: str


class SimpleIndexJson(BaseModel):
    """PEP 691 `application/vnd.pypi.simple.v1+json` project list."""

    meta: dict
    projects: list[SimpleProject]


class SimpleFile(BaseModel):
    """One downloadable file in the PEP 691 per-project document."""

    filename: str
    url: str
    hashes: dict[str, str] = Field(description="At least sha256")
    requires_python: str = Field(alias="requires-python")
    size: int
    upload_time: str | None = Field(default=None, alias="upload-time")


class SimpleProjectJson(BaseModel):
    """PEP 691 `application/vnd.pypi.simple.v1+json` per-project document."""

    meta: dict
    name: str
    files: list[SimpleFile]


# ── Access control (roles, permission points, accounts) ───────────────
# Backed by the five RBAC tables: users / roles / permissions /
# user_roles / role_permissions.  See services/authz.py.

class RoleInfo(BaseModel):
    """One role and the permission points it currently holds."""

    id: int
    code: str = Field(description="Stable identifier, e.g. `publisher`")
    name: str = Field(description="Display name, editable by an administrator")
    description: str | None = None
    is_builtin: bool = Field(description="Built-in roles cannot be deleted")
    is_anonymous_default: bool = Field(
        description="Granted to requests that never authenticated"
    )
    auto_grant: bool = Field(
        description="Granted automatically to every account on first login"
    )
    permissions: list[str] = Field(
        default_factory=list, description="Permission codes this role holds"
    )
    user_count: int = Field(default=0, description="Accounts holding this role")


class PermissionInfo(BaseModel):
    """One permission point — a code some route guard checks."""

    id: int
    code: str = Field(description="`module:action`, e.g. `package:write`")
    name: str
    module: str | None = Field(default=None, description="UI grouping key")
    description: str | None = None
    role_count: int = Field(
        default=0,
        description="How many roles hold this point. 0 means no code path can reach "
                    "it except through a superuser — usually a typo.",
    )
    stale: bool = Field(
        default=False,
        description="The row exists but no guard declares the code any more, so "
                    "granting it changes nothing. Usually a renamed or removed point.",
    )
    expected_for_authenticated: bool = Field(
        default=False,
        description="The seed data hands this point to the auto-granted "
                    "`authenticated` role.",
    )
    held_by_authenticated: bool = Field(
        default=False,
        description="The `authenticated` role holds it right now.",
    )
    authenticated_pending: bool = Field(
        default=False,
        description="Expected for ordinary users but not granted to them yet — the "
                    "`authenticated` role needs the migration that ships with a new "
                    "point. Invisible in `role_count`, because `admin` is topped up "
                    "automatically and hides the gap.",
    )


class UserInfo(BaseModel):
    """One local account (a shadow of an externally authenticated identity)."""

    id: int
    provider: str = Field(description="How the account was first seen")
    external_id: str = Field(description="Stable identity — what roles attach to")
    display_name: str | None = None
    email: str | None = None
    is_active: bool
    is_superuser: bool = Field(description="Bypasses every permission check")
    roles: list[str] = Field(default_factory=list)
    last_login_at: str | None = None
    created_at: str | None = None


class CreateRoleRequest(BaseModel):
    """Body of `POST /api/v1/admin/roles`."""

    code: str = Field(description="Letters, digits, '-' and '_' only")
    name: str = Field(default="", description="Defaults to `code` when empty")
    description: str | None = None


class SetRolePermissionsRequest(BaseModel):
    """Body of `PUT /api/v1/admin/roles/{role_id}/permissions`.

    Replaces the role's grants wholesale.  Codes that do not exist yet are
    created as new permission points, so a permission can be defined before the
    code that checks it ships.
    """

    permissions: list[str]


class GrantRoleRequest(BaseModel):
    """Body of `POST /api/v1/admin/users/{user_id}/roles`."""

    role: str = Field(description="Role code to grant")


class SetSuperuserRequest(BaseModel):
    """Body of `PUT /api/v1/admin/users/{user_id}/superuser`."""

    superuser: bool
