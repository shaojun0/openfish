"""Permission points — declared by code, stored in the database.

A permission *point* is named by the guard that checks it::

    from auth.permissions import PACKAGE_WRITE
    from auth.decorators import require_permission

    @require_permission(PACKAGE_WRITE)
    def upload(): ...

That is the only thing this file is for.  Two rules keep the design honest:

1. **No role -> permission mapping lives here.**  The previous version of this
   module carried a ``ROLES`` dict, which meant granting a role a new
   permission required editing Python and redeploying.  That mapping now lives
   in the ``role_permissions`` table and is edited from the admin dashboard or
   the CLI.  Grep this file: there is no ``ROLES`` symbol any more.

2. **The database row is authoritative for presentation.**  The constants
   below are the *initial* display name / description for the points this
   project ships with; ``AuthzService.sync_permissions()`` upserts them once.
   An administrator may rename, re-describe or re-group any of them, and may
   add brand-new points — a new point stays inert until some route checks it.

Constants are used instead of bare string literals purely so a typo is a
``NameError`` at import time rather than a silently orphaned permission.
"""

from __future__ import annotations

# ── Built-in permission points ───────────────────────────────────────
# The part before ":" becomes the UI grouping key (see module_of()).

# Package operations
PACKAGE_READ = "package:read"
PACKAGE_WRITE = "package:write"

# python-build-standalone mirror
BUILD_READ = "build:read"
BUILD_DOWNLOAD = "build:download"
BUILD_SHA256 = "build:sha256"

# nodejs.org/dist mirror (nvm / fnm / node-gyp)
NODE_BUILD_READ = "nodebuild:read"
NODE_BUILD_DOWNLOAD = "nodebuild:download"
NODE_BUILD_SHA256 = "nodebuild:sha256"

# Artifact hub — tools catalog (downloadable scripts/binaries)
TOOL_READ = "tool:read"
TOOL_DOWNLOAD = "tool:download"

# Artifact hub — npm catalog scaffold
NPM_READ = "npm:read"

# Artifact hub — docker image/compose catalog
DOCKER_READ = "docker:read"
DOCKER_DOWNLOAD = "docker:download"

# Artifact hub — debian package catalog
DEBIAN_READ = "debian:read"
DEBIAN_DOWNLOAD = "debian:download"

# Artifact hub — model routing table for downstream DSH
MODEL_READ = "model:read"

# API key self-service
KEY_LIST = "key:list"
KEY_CREATE = "key:create"
KEY_DELETE = "key:delete"
KEY_STATS = "key:stats"

# Administration
ADMIN_VIEW = "admin:view"
ADMIN_REFRESH = "admin:refresh"
ADMIN_ROLES = "admin:roles"


#: ``code -> (display name, description)`` for the built-in points.
#: Cosmetic seed data only; the database row wins once an admin edits it.
BUILTIN: dict[str, tuple[str, str]] = {
    PACKAGE_READ: ("读取包", "浏览 PEP 503 索引并下载包文件"),
    PACKAGE_WRITE: ("上传包", "通过 twine / uv 上传或覆盖包文件"),
    BUILD_READ: ("浏览 CPython 构建", "查看 python-build-standalone 发布列表"),
    BUILD_DOWNLOAD: ("下载 CPython 构建", "下载预编译的 CPython 发行版"),
    BUILD_SHA256: ("读取构建校验和", "查询构建产物的 SHA256"),
    NODE_BUILD_READ: ("浏览 Node 构建", "查看 nodejs.org/dist 预编译 Node.js 发布列表"),
    NODE_BUILD_DOWNLOAD: ("下载 Node 构建", "下载预编译的 Node.js 发行版与 SHASUMS256.txt"),
    NODE_BUILD_SHA256: ("读取 Node 构建校验和", "查询 Node.js 构建产物的 SHA256"),
    TOOL_READ: ("浏览工具目录", "查看内网工具目录的分类与文件清单"),
    TOOL_DOWNLOAD: ("下载工具", "从工具目录下载脚本或二进制文件"),
    NPM_READ: ("浏览 npm 目录", "查看本地 npm 包目录（脚手架）"),
    DOCKER_READ: ("浏览 Docker 目录", "查看离线镜像与 compose/Dockerfile 清单"),
    DOCKER_DOWNLOAD: ("下载 Docker 制品", "下载镜像 tar 与 compose/Dockerfile 文件"),
    DEBIAN_READ: ("浏览 Debian 目录", "查看本地 .deb 包与 apt 配置清单"),
    DEBIAN_DOWNLOAD: ("下载 Debian 包", "下载本地 .deb 与 apt 配置片段"),
    MODEL_READ: ("浏览模型路由", "查看供内网 DSH 使用的模型路由表"),
    KEY_LIST: ("列出 API 密钥", "查看自己名下的 API 密钥"),
    KEY_CREATE: ("创建 API 密钥", "签发新的 API 密钥"),
    KEY_DELETE: ("吊销 API 密钥", "删除 API 密钥"),
    KEY_STATS: ("查看密钥用量", "查看 API 密钥的下载/上传统计"),
    ADMIN_VIEW: ("查看管理后台", "访问系统统计仪表盘"),
    ADMIN_REFRESH: ("刷新统计缓存", "手动触发统计重算"),
    ADMIN_ROLES: ("管理角色与权限", "创建角色、调整授权、任命管理员"),
}


# ── Points discovered at import time ─────────────────────────────────
# Populated by require_permission(); lets a new guard introduce a new point
# without anyone editing this file.
_DECLARED: dict[str, tuple[str | None, str | None]] = {}


def declare(code: str, *, name: str | None = None, description: str | None = None) -> str:
    """Register *code* as a point some guard checks.  Idempotent.

    Called automatically by :func:`auth.decorators.require_permission`, so in
    practice you never call this yourself.
    """
    if not code or ":" not in code:
        raise ValueError(
            f"permission code {code!r} must be namespaced as 'module:action'"
        )
    prev = _DECLARED.get(code)
    if prev is None:
        _DECLARED[code] = (name, description)
    else:
        # Never let a later, sparser declaration clobber a richer earlier one.
        _DECLARED[code] = (prev[0] or name, prev[1] or description)
    return code


def declared() -> dict[str, tuple[str | None, str | None]]:
    """Every point seen so far, built-in and ad-hoc alike."""
    return dict(_DECLARED)


def module_of(code: str) -> str:
    """UI grouping key for a permission code — the part before the colon."""
    return code.split(":", 1)[0]


def seed_catalog() -> dict[str, tuple[str, str | None, str]]:
    """``code -> (name, description, module)`` to upsert into ``permissions``.

    Built-in points keep their curated names; anything else a guard declared
    gets its code as a placeholder name for an admin to improve.
    """
    catalog: dict[str, tuple[str, str | None, str]] = {}
    for code, (name, description) in BUILTIN.items():
        catalog[code] = (name, description, module_of(code))
    for code, (name, description) in _DECLARED.items():
        if code in catalog:
            continue
        catalog[code] = (name or code, description, module_of(code))
    return catalog


# ── Built-in role *identities* ───────────────────────────────────────
# Note what this is NOT: it is not a permission mapping.  These are just the
# role rows the server guarantees exist.  What each of them may do is decided
# entirely by ``role_permissions``.

ANONYMOUS_ROLE = "anonymous"
AUTHENTICATED_ROLE = "authenticated"
ADMIN_ROLE = "admin"

#: ``code -> (name, description, is_builtin, is_anonymous_default, auto_grant)``
BUILTIN_ROLES: dict[str, tuple[str, str, bool, bool, bool]] = {
    ANONYMOUS_ROLE: (
        "匿名访客",
        "未认证请求自动获得（仅在 AUTH_ENABLED=false 时可达）",
        True,
        True,    # is_anonymous_default
        False,
    ),
    AUTHENTICATED_ROLE: (
        "已认证用户",
        "任何账号首次登录时自动授予",
        True,
        False,
        True,    # auto_grant
    ),
    ADMIN_ROLE: (
        "系统管理员",
        "持有全部权限点的内置角色（注意：与 is_superuser 不同）",
        True,
        False,
        False,
    ),
}

__all__ = [
    "PACKAGE_READ", "PACKAGE_WRITE",
    "BUILD_READ", "BUILD_DOWNLOAD", "BUILD_SHA256",
    "NODE_BUILD_READ", "NODE_BUILD_DOWNLOAD", "NODE_BUILD_SHA256",
    "TOOL_READ", "TOOL_DOWNLOAD", "NPM_READ", "MODEL_READ",
    "DOCKER_READ", "DOCKER_DOWNLOAD", "DEBIAN_READ", "DEBIAN_DOWNLOAD",
    "KEY_LIST", "KEY_CREATE", "KEY_DELETE", "KEY_STATS",
    "ADMIN_VIEW", "ADMIN_REFRESH", "ADMIN_ROLES",
    "BUILTIN", "BUILTIN_ROLES",
    "ANONYMOUS_ROLE", "AUTHENTICATED_ROLE", "ADMIN_ROLE",
    "declare", "declared", "module_of", "seed_catalog",
]
