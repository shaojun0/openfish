"""Agent Hub permission points — the eight codes the repo/agent routes check.

Spec: ``docs/agent-hub/DEVELOPMENT.md`` §5.1.  The points deliberately live in
their own module rather than in ``auth/permissions.py`` so the Agent Hub slice
can be reviewed (and reverted) as one unit; the integration step is one
:func:`install_into` call, documented in
``docs/agent-hub/integration/S0.md``.

Why a separate module is not a second permission system
-------------------------------------------------------
There is still exactly one catalog: ``auth.permissions.BUILTIN``, the dict
``services/authz.sync_permissions()`` upserts into the ``permissions`` table.
:data:`BUILTIN_AGENT_HUB` is seed data for that one catalog, shaped exactly like
the entries already in it — ``code -> (中文名, 中文描述)`` — and
:func:`install_into` is a plain dict merge.  Nothing here reads the database and
nothing here grants anything.

Two integration details this split requires (both in S0.md):

1. ``auth.permissions`` must re-export these eight **constants** (not only the
   strings).  A guard's argument is resolved by name against that module
   (``getattr(auth.permissions, <name>)``) and the declaration is recorded by
   its bare AST name, so ``require_permission(REPO_READ)`` only works when
   ``REPO_READ`` is importable from ``auth.permissions``.
2. The guards must actually exist before the codes are merged into
   ``BUILTIN``: every built-in point must be explicitly classified, and a
   declared point that no guard checks must fail ("no dead points").  Merge this
   module in the same change that lands ``routes/repos.py`` /
   ``routes/findings.py`` / ``routes/agent_tasks.py``, or the catalogue will
   (correctly) report eight unused points.

Default grants
--------------
``anonymous`` gets none of these — "anonymous is docs and nothing else" is the
project-wide rule, and §5.1 restates it.  Which points the ``authenticated``
role receives on a fresh deployment is decided in
``services.authz._AUTHENTICATED_SEED``; every built-in point must be explicitly
classified, and S0.md lists the intended classification
(AUTHENTICATED_SEEDED: ``repo:read`` / ``repo:push`` / ``finding:read`` /
``agent:run``; ADMIN_ONLY: the other four).
"""

from __future__ import annotations

# ── The eight points (DEVELOPMENT.md §5.1) ───────────────────────────

#: List/view repositories and their imported issues — every signed-in user.
REPO_READ = "repo:read"
#: Import a repository, trigger a sync, change its configuration — admin.
REPO_WRITE = "repo:write"
#: clone/push over the git protocol (the API side mints the credential); every
#: signed-in user.  The point is **global** — it is not checked per repository —
#: and the minted Forgejo ticket is account-wide (``read:repository`` /
#: ``write:repository``), not bound to the requested repo.  Per-repository
#: enforcement is a Forgejo-side ACL / branch-protection *deployment* concern,
#: not a platform check.  The ``agent/*``-only rule (invariant I4) constrains
#: the platform's own agent runner, not a human's ticket.
REPO_PUSH = "repo:push"
#: View findings and the debt board — every signed-in user.
FINDING_READ = "finding:read"
#: acknowledge / wontfix / reject a finding — repository owner role.
FINDING_DECIDE = "finding:decide"
#: Start a review/fix task — every signed-in user, subject to quota.
AGENT_RUN = "agent:run"
#: Manage the runtime, see every task, retry/terminate — admin.
AGENT_ADMIN = "agent:admin"
#: Write ``.agent/review-policy.yml`` — admin.
POLICY_WRITE = "policy:write"

#: Insertion order is the order the console's permission list shows.
AGENT_HUB_PERMISSIONS: tuple[str, ...] = (
    REPO_READ,
    REPO_WRITE,
    REPO_PUSH,
    FINDING_READ,
    FINDING_DECIDE,
    AGENT_RUN,
    AGENT_ADMIN,
    POLICY_WRITE,
)

#: ``code -> (显示名, 描述)``, the exact shape of ``auth.permissions.BUILTIN``.
#: Cosmetic seed data only — once an administrator edits a row in the console,
#: the database row is authoritative (see ``auth/permissions.py``'s docstring).
BUILTIN_AGENT_HUB: dict[str, tuple[str, str]] = {
    REPO_READ: ("浏览仓库", "列出/查看仓库及其镜像的 issue 与提交历史"),
    REPO_WRITE: ("管理仓库", "导入仓库、触发增量同步、修改仓库配置（仅管理员）"),
    REPO_PUSH: (
        "推送仓库",
        "通过 git 协议 clone/push：权限点为全局（所有登录用户），签发的 Forgejo "
        "token 为账号级（read:repository / write:repository），不绑定具体仓库；能否"
        "推送由 Forgejo 侧仓库 ACL 与分支保护决定，属部署时的手工步骤，平台不代管；"
        "agent/* 限制（I4）只约束平台自带的 agent runner，不约束人类用户的票据",
    ),
    FINDING_READ: ("浏览发现", "查看 finding 与债务看板"),
    FINDING_DECIDE: ("处置发现", "acknowledge / wontfix / 驳回 finding（需要 owner 与 due）"),
    AGENT_RUN: ("触发智能体任务", "触发一次 review / fix 任务（受配额约束）"),
    AGENT_ADMIN: ("管理智能体运行时", "管理运行时、查看全部任务、重试或终止任务（仅管理员）"),
    POLICY_WRITE: ("编写审查策略", "写入 .agent/review-policy.yml（仅管理员）"),
}


# ── Integration helper ───────────────────────────────────────────────

def install_into(catalog: dict) -> dict:
    """Merge the Agent Hub points into *catalog* and return it.

    *catalog* is ``auth.permissions.BUILTIN``.  The merge is in place (so the
    module-level dict the app already holds is updated, not shadowed) and
    idempotent — calling it twice changes nothing the second time.  An entry
    that already exists is left alone, so an administrator's rename in the
    database can never be fought over by this function: this is seed data, and
    the database wins.

    Typical call site, inside ``auth/permissions.py``::

        from auth.agent_hub_permissions import install_into
        install_into(BUILTIN)
    """
    for code, (name, description) in BUILTIN_AGENT_HUB.items():
        existing = catalog.get(code)
        if existing is None:
            catalog[code] = (name, description)
        else:
            # Keep the richer of the two labels; never overwrite a curated row
            # with a sparser one (same rule as declare()).
            catalog[code] = (existing[0] or name, existing[1] or description)
    return catalog


__all__ = [
    "AGENT_HUB_PERMISSIONS",
    "BUILTIN_AGENT_HUB",
    "AGENT_ADMIN",
    "AGENT_RUN",
    "FINDING_DECIDE",
    "FINDING_READ",
    "POLICY_WRITE",
    "REPO_PUSH",
    "REPO_READ",
    "REPO_WRITE",
    "install_into",
]
