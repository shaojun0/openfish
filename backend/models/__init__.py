"""SQLAlchemy models for cpypiserver.

Importing this package registers every table on ``Base.metadata`` — keep it
that way, because ``create_all`` only creates what has been imported.

``agent_hub`` is the ninth-through-seventeenth table group (repos, imported
collaboration history, findings, review runs and the agent task queue); its
constants and ``to_dict()`` serializers are re-exported here so callers can say
``from models import FINDING_STATUS`` instead of reaching into the module.
"""

from __future__ import annotations

from .base import Base
from .api_key import ApiKey, ApiKeyStats
from .agent_hub import (
    EVIDENCE_RELATION,
    FINDING_LEVEL,
    FINDING_SEVERITY,
    FINDING_STATUS,
    IMPORT_MODE,
    IMPORT_PHASE,
    ISSUE_STATE,
    REPO_KIND,
    REPO_SOURCE,
    REPO_SYNC_STATE,
    REVIEW_RUN_STATUS,
    TASK_KIND,
    TASK_STATUS,
    AgentTask,
    Finding,
    FindingEvent,
    FindingEvidence,
    ImportJob,
    Repo,
    RepoCommit,
    RepoIssue,
    ReviewRun,
    context_key,
    fingerprint,
)
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
    # ── Agent Hub tables ─────────────────────────────────────────────
    "AgentTask",
    "Finding",
    "FindingEvent",
    "FindingEvidence",
    "ImportJob",
    "Repo",
    "RepoCommit",
    "RepoIssue",
    "ReviewRun",
    # Agent Hub vocabularies and the fingerprint helper (§4.3 / §4.4)
    "EVIDENCE_RELATION",
    "FINDING_LEVEL",
    "FINDING_SEVERITY",
    "FINDING_STATUS",
    "IMPORT_MODE",
    "IMPORT_PHASE",
    "ISSUE_STATE",
    "REPO_KIND",
    "REPO_SOURCE",
    "REPO_SYNC_STATE",
    "REVIEW_RUN_STATUS",
    "TASK_KIND",
    "TASK_STATUS",
    "context_key",
    "fingerprint",
]
