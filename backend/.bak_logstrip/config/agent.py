"""Agent runtime configuration — every ``AGENT_*`` deployment knob.

The agent runtime (the queue's producer-side throttle, the runner's work
directory and budgets, and the sandbox uid the untrusted children are dropped
to) is configured by one flat family of environment variables.  They used to be
read, re-parsed and defaulted in four different modules — ``agent_runner`` had
its own ``_env_int``/``_env_float``, ``agent_queue`` its own ``int(...) or 0``,
``agent_worker`` read ``AGENT_REVIEW_*`` inline, and ``sandbox_identity`` and
``agent_runner`` each declared their own constant for the *same*
``AGENT_WORK_ROOT``.  A value could therefore disagree with itself depending on
which module asked.

This module is now the single reader.  Services ask :data:`config.settings` and
never touch ``os.environ``::

    settings.agent.work_root            # AGENT_WORK_ROOT
    settings.agent.gate_timeout         # AGENT_GATE_TIMEOUT

Values are read once, when the process builds its settings.  Nothing re-reads
the environment mid-run: a deployment knob that a running worker can be steered
into after start-up is an injection surface, not a feature.  A test (or an
operator) that wants a second configuration sets the field on the settings
object — the same thing the environment would have done, but explicit and
in-process.

The one thing deliberately **not** here is the vocabulary of ``pr_policy``:
:mod:`services.review_policy` owns those three names and validates them (a
per-repository policy file and the deployment-wide setting must be read the same
way), so this field stays a plain string and the normalisation stays there.
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import SettingsConfigDict

from config.base import EnvSettings

#: Prefix in front of every flat name in this group.
ENV_PREFIX = "AGENT_"

#: Defaults, kept as module constants so the services that also expose them as
#: library-level defaults (``agent_runner.DEFAULT_WORK_ROOT`` and friends) can
#: re-export one declaration instead of holding a second copy of the number.
#: ``DEFAULT_GATE_TIMEOUT`` is deliberately **not** here: ``services.gates`` owns
#: it as the executor's own default (what ``run_suite()`` does with a caller who
#: passes no timeout), and importing it back would make ``config`` depend on a
#: service.  The field default below must stay equal to it.
DEFAULT_WORK_ROOT = "/work"
DEFAULT_RETENTION_SECONDS = 24 * 60 * 60
DEFAULT_MAX_FINDINGS = 50
DEFAULT_REVIEW_TIMEOUT = 900.0


class AgentConfig(EnvSettings):
    """The ``AGENT_*`` group: one agent worker's process- and task-level knobs."""

    model_config = SettingsConfigDict(env_prefix=ENV_PREFIX)

    # ── Work tree ───────────────────────────────────────────────────
    work_root: str = Field(
        default=DEFAULT_WORK_ROOT,
        description=(
            "Root under which every task gets its own checkout. The runner "
            "creates <work_root>/<task_id> (or, for a per-repository runner, "
            "the runner's own workspace below it) and the sandbox uid must be "
            "able to traverse every level from here down, which is why "
            "services/sandbox_identity.py relaxes the ancestors up to exactly "
            "this directory and never above it."
        ),
    )
    work_retention_seconds: int = Field(
        default=DEFAULT_RETENTION_SECONDS,
        ge=0,
        description=(
            "How long a finished task's work directory is kept before the sweep "
            "removes it. Only a directory carrying the finished marker is ever "
            "swept, so an in-flight task cannot be collected."
        ),
    )

    # ── Budgets ─────────────────────────────────────────────────────
    gate_timeout: float = Field(
        default=120.0,
        gt=0,
        description=(
            "Wall-clock budget, in seconds, for one verification gate. Must "
            "stay equal to services.gates.DEFAULT_GATE_TIMEOUT, which remains "
            "the executor's own default for a caller that passes none."
        ),
    )
    max_findings: int = Field(
        default=DEFAULT_MAX_FINDINGS,
        ge=0,
        description=(
            "Ceiling on the findings one run may report. A per-repository "
            ".agent/review-policy.yml may lower it; this is the deployment-wide "
            "bound that keeps one repository from flooding the review surface."
        ),
    )
    review_command: str = Field(
        default="",
        description=(
            "The headless command that performs one review, run inside the "
            "checkout. It must print a JSON array of findings on stdout. Empty "
            "means there is no reviewer: a review task fails loudly rather than "
            "being marked done, so an unconfigured deployment cannot look green. "
            "The command is parsed with shlex and never through a shell."
        ),
    )
    review_timeout: float = Field(
        default=DEFAULT_REVIEW_TIMEOUT,
        gt=0,
        description="Wall-clock budget, in seconds, for one review command.",
    )

    # ── Admission control ───────────────────────────────────────────
    max_in_flight_per_repo: int = Field(
        default=0,
        ge=0,
        description=(
            "Producer-side ceiling on the queued/leased/running tasks one "
            "repository may hold, so a webhook storm cannot fill the single "
            "global queue. 0 means unlimited (the library default); a "
            "deployment sets a small number. A repository whose runner row sets "
            "max_concurrency > 0 overrides this value, and an explicit "
            "max_in_flight_per_repo argument overrides both."
        ),
    )

    # ── Result-gated publication ────────────────────────────────────
    pr_policy: str = Field(
        default="",
        description=(
            "Deployment-wide override for the result-gated PR decision "
            "(on_green / always / never). Empty, the default, lets each "
            "repository's .agent/review-policy.yml decide — which is what a "
            "mixed deployment wants. An unknown value is logged and ignored by "
            "services.agent_runner.normalize_pr_policy, never guessed at."
        ),
    )
    auto_fix: bool | None = Field(
        default=None,
        description=(
            "Whether a review with failing gates may be escalated to a fix "
            "plus PR. Unset (the default) lets each repository's policy decide; "
            "set it only to override every repository at once, which AGENTS.md "
            "§6.4 treats as an operator decision rather than a default."
        ),
    )

    # ── Untrusted-child isolation ───────────────────────────────────
    sandbox_uid: int | None = Field(
        default=None,
        ge=1,
        description=(
            "uid the untrusted children (the repository's own check_*.py and "
            "the review command) are dropped to. Both this and sandbox_gid must "
            "be set for the drop to happen; setting only one is refused rather "
            "than guessed, because a half-configured drop would run untrusted "
            "code as the trusted worker."
        ),
    )
    sandbox_gid: int | None = Field(
        default=None,
        ge=1,
        description=(
            "gid the untrusted children are dropped to, and the group the work "
            "tree is shared with. See sandbox_uid."
        ),
    )


__all__ = [
    "DEFAULT_MAX_FINDINGS",
    "DEFAULT_RETENTION_SECONDS",
    "DEFAULT_REVIEW_TIMEOUT",
    "DEFAULT_WORK_ROOT",
    "ENV_PREFIX",
    "AgentConfig",
]
