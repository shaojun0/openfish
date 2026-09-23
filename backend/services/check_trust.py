"""The two-suite assurance model: user checks are authority, AI checks advise.

Governance decision (see ``docs/agent-hub/DESIGN-ai-checks.md``): there are
**two independent, decoupled suites**, and no per-change human approval flow.

* the **repository (user) suite** — the repo's own native tests/CI, or commands
  a human declared in ``.agent/review-policy.yml`` ``checks:``.  It is the
  authority.  The AI never modifies it, and an AI result may never mask or
  override one of its failures.
* the **AI-maintained suite** — ``.agent/checks/**``, proposed and evolved by
  the curator with no human approval.  It is **advisory**: it adds signal, and
  it ratchets (an active check may only be weakened by a separate, visible
  curator run with recorded provenance).

Trust is **mechanically earned**, not human-granted, on a four-rung ladder:

====  ==========================================  ============================
rung  condition                                   what it may do
====  ==========================================  ============================
L0    no validated checks                          ``unverified``; report only
L1    validated AI checks                          open a **labelled** PR
L2    a repository suite exists                    gate on the repository suite
L3    L1 + objective promotion criteria met        gate on that AI check too
====  ==========================================  ============================

**No rung ever authorises a merge.**  :data:`MAY_AUTO_MERGE` is a module
constant that is ``False`` and :attr:`Assurance.may_auto_merge` is hard-coded to
it, so no configuration, policy value or test can turn an automated merge on
from here.  L3 grants *gate authority and PR labelling only*; it is **off by
default** and additionally must be switched on by policy.

Everything in this module is pure: it takes already-computed
:class:`~services.gates.GateSummary` objects and history summaries and decides.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from services.check_validation import CheckHistorySummary, CheckValidation
from services.gates import (
    STATUS_PASSED,
    STATUS_UNVERIFIED,
    VALIDATION_VALIDATED,
    CheckSuite,
    GateSummary,
    render_summary,
)


#: The assurance ladder, weakest first.
ASSURANCE_L0 = "L0"
ASSURANCE_L1 = "L1"
ASSURANCE_L2 = "L2"
ASSURANCE_L3 = "L3"
ASSURANCE_LEVELS: tuple[str, ...] = (ASSURANCE_L0, ASSURANCE_L1, ASSURANCE_L2, ASSURANCE_L3)

#: **The red line, as a constant.**  No code path in this project may set this
#: to true; a test asserts it and the agent's Forgejo surface (see
#: ``services.agent_surface``) has no merge capability to call even if it did.
MAY_AUTO_MERGE = False

#: PR label prefix; the rung is appended so a reviewer sees the assurance level
#: without reading the body.
LABEL_PREFIX = "ai-checks"

#: Human-readable rung descriptions.
LEVEL_DESCRIPTIONS: dict[str, str] = {
    ASSURANCE_L0: "无可门控校验（unverified）——只报告，不 push、不开 PR",
    ASSURANCE_L1: "仅由 AI 维护的校验背书——可开带标签的 PR，绝不自动合并",
    ASSURANCE_L2: "由仓库（用户）校验背书——AI 校验只作补充信号",
    ASSURANCE_L3: "AI 校验按客观标准晋升为权威（默认关闭，策略显式开启）",
}


@dataclass(frozen=True)
class L3Criteria:
    """Objective, auditable criteria for promoting an AI check to authoritative.

    This is the *automatic* replacement for a human approval flow.  Every field
    is a property of the check's own recorded history, not a judgement call.

    **Default off.**  :data:`DEFAULT_L3_CRITERIA` has ``enabled=False``, so L3 is
    unreachable until a deployment explicitly flips it in policy — and even
    then it only grants gate authority, never merge.
    """

    enabled: bool = False
    min_detection_rate: float = 1.0
    min_runs: int = 20
    max_flake_rate: float = 0.0
    require_never_weakened: bool = True


#: The default, deliberately OFF (see :class:`L3Criteria`).
DEFAULT_L3_CRITERIA = L3Criteria()


@dataclass(frozen=True)
class Assurance:
    """The run's assurance verdict: which suite verified it, and what it allows."""

    level: str
    may_open_pr: bool
    may_auto_merge: bool
    label: str
    description: str
    reason: str
    user_state: str
    ai_state: str
    verified_by: tuple[str, ...] = ()

    def as_payload(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "may_open_pr": self.may_open_pr,
            "may_auto_merge": self.may_auto_merge,
            "label": self.label,
            "description": self.description,
            "reason": self.reason,
            "user_state": self.user_state,
            "ai_state": self.ai_state,
            "verified_by": list(self.verified_by),
        }


def _state_of(summary: GateSummary | None) -> str:
    if summary is None:
        return STATUS_UNVERIFIED
    return str(getattr(summary, "state", "") or STATUS_UNVERIFIED)


def check_l3_eligible(
    validations: Iterable[CheckValidation],
    history: Iterable[CheckHistorySummary],
    *,
    criteria: L3Criteria = DEFAULT_L3_CRITERIA,
) -> bool:
    """Whether *every* candidate AI check meets the objective promotion criteria.

    Returns ``False`` immediately when ``criteria.enabled`` is false (the
    default), so the ladder stops at L1/L2 unless a policy explicitly opts in.
    """
    if not criteria.enabled:
        return False
    validation_by_id = {item.check_id: item for item in validations}
    history_by_id = {item.check_id: item for item in history}
    if not validation_by_id:
        return False
    for check_id, validation in validation_by_id.items():
        if not validation.validated:
            return False
        if validation.detection_rate < criteria.min_detection_rate:
            return False
        record = history_by_id.get(check_id)
        if record is None or record.runs < criteria.min_runs:
            return False
        if record.flake_rate > criteria.max_flake_rate:
            return False
        if criteria.require_never_weakened and record.weakened:
            return False
    return True


def assess_assurance(
    *,
    user_summary: GateSummary | None,
    ai_summary: GateSummary | None,
    l3_eligible: bool = False,
    criteria: L3Criteria = DEFAULT_L3_CRITERIA,
) -> Assurance:
    """Decide the assurance rung and what it authorises.

    * a repository suite that ran (state != ``unverified``) decides the PR;
    * otherwise validated AI checks decide it, labelled;
    * otherwise the run is L0 and must not open a PR.

    An AI failure never turns a passing repository suite into a failure and
    never turns a failing repository suite into a pass: the two states are
    evaluated independently and the repository suite wins.
    """
    user_state = _state_of(user_summary)
    ai_state = _state_of(ai_summary)
    if user_state != STATUS_UNVERIFIED:
        may_open = user_state == STATUS_PASSED
        return Assurance(
            level=ASSURANCE_L2,
            may_open_pr=may_open,
            may_auto_merge=MAY_AUTO_MERGE,
            label=label_for(ASSURANCE_L2),
            description=LEVEL_DESCRIPTIONS[ASSURANCE_L2],
            reason=(
                f"仓库校验 {user_state}（权威）；AI 校验 {ai_state}"
                + ("（只作补充信号）" if ai_state != STATUS_UNVERIFIED else "（无）")
            ),
            user_state=user_state,
            ai_state=ai_state,
            verified_by=("repository",),
        )
    if ai_state != STATUS_UNVERIFIED:
        level = ASSURANCE_L3 if (l3_eligible and criteria.enabled) else ASSURANCE_L1
        may_open = ai_state == STATUS_PASSED
        return Assurance(
            level=level,
            may_open_pr=may_open,
            may_auto_merge=MAY_AUTO_MERGE,
            label=label_for(level),
            description=LEVEL_DESCRIPTIONS[level],
            reason=(
                f"没有仓库校验，仅 AI 维护的校验 {ai_state}"
                + ("（L3 已按客观标准晋升，仍不自动合并）"
                   if level == ASSURANCE_L3 else "（advisory，绝不自动合并）")
            ),
            user_state=user_state,
            ai_state=ai_state,
            verified_by=("ai",),
        )
    return Assurance(
        level=ASSURANCE_L0,
        may_open_pr=False,
        may_auto_merge=MAY_AUTO_MERGE,
        label=label_for(ASSURANCE_L0),
        description=LEVEL_DESCRIPTIONS[ASSURANCE_L0],
        reason="没有可门控的校验（仓库校验与 AI 校验都 unverified）",
        user_state=user_state,
        ai_state=ai_state,
        verified_by=(),
    )


def label_for(level: str) -> str:
    """The PR label for an assurance rung."""
    return f"{LABEL_PREFIX}/{level}"


# ── Two-suite reporting ──────────────────────────────────────────────

REPOSITORY_TITLE = "Repository checks (authority)"
AI_TITLE = "AI-maintained checks (advisory)"

#: The sentence every AI-produced PR must carry.
HUMAN_REVIEW_NOTICE = (
    "> **不会自动合并。** 本 PR 由 agent 产出，必须经人工 review 后由人合并；"
    "agent 没有合并、批准或改分支保护的能力。"
)


def render_two_suites(
    user_summary: GateSummary | None,
    ai_summary: GateSummary | None,
    assurance: Assurance,
    *,
    user_suite: CheckSuite | None = None,
    ai_suite: CheckSuite | None = None,
) -> str:
    """The PR body's verification block: two distinct, labelled tables.

    The two suites are deliberately **not** merged into one anonymous green
    list: a reader has to be able to tell which checks are the repository's own
    authority and which the AI maintains.
    """
    lines = [
        "## 校验与保证等级",
        "",
        f"**Assurance: `{assurance.level}`** — {assurance.description}",
        "",
        assurance.reason,
        "",
        render_summary(_materialize(user_summary), title=_title(REPOSITORY_TITLE, user_suite)),
        "",
        render_summary(_materialize(ai_summary), title=_title(AI_TITLE, ai_suite)),
        "",
        f"**Label:** `{assurance.label}` · **auto-merge:** "
        f"`{'yes' if assurance.may_auto_merge else 'never'}`",
        "",
        HUMAN_REVIEW_NOTICE,
    ]
    return "\n".join(lines)


def _title(base: str, suite: CheckSuite | None) -> str:
    if suite is None or not suite.checks:
        return base
    return f"{base} — source: {suite.source}, {len(suite.checks)} check(s)"


def _materialize(summary: GateSummary | None) -> GateSummary:
    """A renderable summary; a missing suite is an explicit unverified row."""
    if summary is not None:
        return summary
    from services.gates import unverified_summary

    return unverified_summary("本次 run 没有这一套校验")


# ── Governance findings (existing ledger) ────────────────────────────

RULE_NO_VERIFICATION = "checks.no-automated-verification"
RULE_NEVER_FAILED = "checks.never-failed"
RULE_AI_SUITE_DIVERGES = "checks.ai-suite-diverges"


def _finding(
    rule_id: str,
    *,
    title: str,
    detail: str,
    file_path: str,
    symbol: str,
    severity: str = "medium",
) -> dict[str, Any]:
    return {
        "rule_id": rule_id,
        "level": "debt",
        "severity": severity,
        "file_path": file_path,
        "symbol": symbol,
        "title": title,
        "detail": detail,
        "context_key": "",
        "evidence": [],
        "autofix": False,
    }


def governance_findings(
    *,
    user_suite: CheckSuite | None,
    ai_suite: CheckSuite | None,
    history: Sequence[CheckHistorySummary] = (),
) -> list[dict[str, Any]]:
    """Findings for the existing ledger (§6): verification gaps, visible.

    * ``checks.no-automated-verification`` — neither suite can gate;
    * ``checks.never-failed`` — a check with no observed failure is not evidence;
    * ``checks.ai-suite-diverges`` — the AI suite and the repository suite no
      longer cover the same checks (drift is expected, but it must be visible).
    """
    findings: list[dict[str, Any]] = []
    user_checks = {check.id: check for check in (user_suite.checks if user_suite else [])}
    ai_checks = {check.id: check for check in (ai_suite.checks if ai_suite else [])}

    if not user_checks and not ai_checks:
        findings.append(_finding(
            RULE_NO_VERIFICATION,
            title="这个仓库没有任何自动化校验",
            detail="没有仓库自带校验，也没有 .agent/checks/ 套件；agent 无法自证改动安全。",
            file_path=".agent/review-policy.yml",
            symbol="checks",
            severity="high",
        ))
    for summary in history:
        if summary.never_failed and summary.validation == VALIDATION_VALIDATED:
            findings.append(_finding(
                RULE_NEVER_FAILED,
                title=f"检查 {summary.check_id} 从未失败过",
                detail=(
                    f"{summary.check_id} 记录了 {summary.runs} 次运行、0 次失败；"
                    "从未失败过的检查无法证明它能发现回归。"
                ),
                file_path=".agent/checks/checks.yml",
                symbol=summary.check_id,
            ))
    if user_checks and ai_checks:
        only_ai = sorted(set(ai_checks) - set(user_checks))
        only_user = sorted(set(user_checks) - set(ai_checks))
        if only_ai or only_user:
            findings.append(_finding(
                RULE_AI_SUITE_DIVERGES,
                title="AI 套件与仓库套件覆盖不一致",
                detail=(
                    "AI 独有：" + (", ".join(only_ai) or "无")
                    + "；仓库独有：" + (", ".join(only_user) or "无")
                ),
                file_path=".agent/checks/checks.yml",
                symbol="suite",
            ))
    return findings


__all__ = [
    "ASSURANCE_L0",
    "ASSURANCE_L1",
    "ASSURANCE_L2",
    "ASSURANCE_L3",
    "ASSURANCE_LEVELS",
    "AI_TITLE",
    "DEFAULT_L3_CRITERIA",
    "HUMAN_REVIEW_NOTICE",
    "LABEL_PREFIX",
    "LEVEL_DESCRIPTIONS",
    "MAY_AUTO_MERGE",
    "REPOSITORY_TITLE",
    "RULE_AI_SUITE_DIVERGES",
    "RULE_NEVER_FAILED",
    "RULE_NO_VERIFICATION",
    "Assurance",
    "L3Criteria",
    "assess_assurance",
    "check_l3_eligible",
    "governance_findings",
    "label_for",
    "render_two_suites",
]
