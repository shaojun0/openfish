"""Forgejo integration configuration — API access, mirroring and the importer.

Everything the backend needs to talk to its Forgejo instance, plus the knobs of
the repository importer that drives it.  These were the last family of variables
read outside :mod:`config`: :class:`services.repo_import.ImportConfig` carried
its own ``CONFIG_ITEMS`` table and a ``from_env`` reader with a private
``number()`` parser, while ``services.repo_runner`` and ``services.agent_worker``
each spelled ``FORGEJO_RUNNER_TOKEN`` in their own constant.  Three readers of
one deployment's Forgejo settings, each parsing them slightly differently.

The group now owns the variables; :class:`services.repo_import.ImportConfig`
stays a frozen value object (the importer, the webhook and the identity exchange
all take one, and the offline gates inject their own) but is built from
:func:`ImportConfig.from_settings` instead of from the environment.

Field names are the flat variable names, lower-cased — the same convention
``ServerConfig`` uses for ``ADMIN_USERS``/``PUBLIC_BASE_URL`` — because the
deployment already speaks ``FORGEJO_BASE_URL`` and ``IMPORT_MAX_RATE`` and those
names must keep working verbatim.
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import SettingsConfigDict

from config.base import EnvSettings


class ForgejoConfig(EnvSettings):
    """Forgejo API endpoints, credentials, and importer limits."""

    model_config = SettingsConfigDict()

    # ── Instance ────────────────────────────────────────────────────
    forgejo_base_url: str = Field(
        default="http://forgejo:3000",
        description=(
            "Base URL of the Forgejo instance this server mirrors repositories "
            "into and opens pull requests against. The default is the compose "
            "service name, which is only resolvable inside the compose network."
        ),
    )
    forgejo_git_base_url: str = Field(
        default="",
        description=(
            "Base URL used for git-over-HTTP when it differs from "
            "forgejo_base_url (a deployment that publishes a different host for "
            "clone/push than for the REST API). Empty means use "
            "forgejo_base_url."
        ),
    )
    forgejo_owner: str = Field(
        default="openfish",
        description=(
            "Owner the mirrored repositories are created under inside Forgejo, "
            "when the import source carries no namespace of its own."
        ),
    )
    forgejo_public_base_url: str = Field(
        default="/git",
        description=(
            "Path prefix the SPA and the API links use to reach Forgejo through "
            "the front reverse proxy, e.g. /git. Empty means Forgejo is reached "
            "at the root."
        ),
    )

    # ── Credentials ─────────────────────────────────────────────────
    forgejo_admin_token: str = Field(
        default="",
        description=(
            "Instance-wide admin token, used by the importer and the identity "
            "exchange to create users, repositories and webhooks. It is a "
            "platform secret: it is allowlist-excluded from every untrusted "
            "child (services/sandbox_env.py) and never handed to the agent, "
            "which gets forgejo_runner_token instead."
        ),
    )
    forgejo_webhook_secret: str = Field(
        default="",
        description=(
            "Shared secret Forgejo signs its webhook deliveries with. Must equal "
            "the value configured on the Forgejo side; the webhook route refuses "
            "a delivery it cannot verify."
        ),
    )
    forgejo_runner_token: str = Field(
        default="",
        description=(
            "The narrow, separately revocable, non-admin Forgejo token the "
            "agent runtime uses for git clone/push and for opening pull "
            "requests. It is the deployment-wide fallback for a repository that "
            "has no credential of its own sealed under runner_credential_key. "
            "Empty means such a repository's fix task fails at push time instead "
            "of silently borrowing admin authority."
        ),
    )

    # ── Importer ────────────────────────────────────────────────────
    import_max_issues: int = Field(
        default=20000,
        gt=0,
        description=(
            "Ceiling on the issues one import job mirrors. Reaching it ends the "
            "job as partial rather than done, so a truncated mirror can never be "
            "mistaken for a complete one."
        ),
    )
    import_max_commits: int = Field(
        default=5000,
        gt=0,
        description="Ceiling on the commits one import job indexes. As above: reaching it is partial, not done.",
    )
    import_max_rate: float = Field(
        default=2.0,
        gt=0,
        description=(
            "Requests per second the importer and the identity exchange allow "
            "against Forgejo, shared by every call in the process. Deliberately "
            "low: the import is a background job and must not compete with the "
            "instance's own users."
        ),
    )
    import_page_size: int = Field(
        default=50,
        gt=0,
        description="Items requested per Forgejo list call while paging.",
    )
    import_poll_interval: float = Field(
        default=2.0,
        gt=0,
        description="Seconds between polls while waiting for a Forgejo migration or mirror to finish.",
    )
    import_poll_attempts: int = Field(
        default=150,
        gt=0,
        description=(
            "How many times to poll before giving up on a long-running Forgejo "
            "operation. With the default interval this bounds a wait at ~5 "
            "minutes, after which the job fails instead of hanging forever."
        ),
    )
    import_http_timeout: float = Field(
        default=30.0,
        gt=0,
        description=(
            "Read timeout, in seconds, for one Forgejo REST call. Git transfers "
            "are streamed by the git client and are not bounded by this."
        ),
    )
    import_source_token: str = Field(
        default="",
        description=(
            "Optional credential for cloning from a *remote* import source "
            "(GitHub, Gitee, GitLab) that requires authentication. Empty means "
            "anonymous clones. It is a platform secret and is excluded from "
            "every untrusted child."
        ),
    )
    git_mirror_dir: str = Field(
        default="",
        description=(
            "Directory holding the bare mirrors of imported repositories. Empty "
            "means the importer keeps no local mirror and always clones from "
            "Forgejo."
        ),
    )


__all__ = ["ForgejoConfig"]
