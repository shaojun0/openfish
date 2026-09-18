"""Curator post-processing: prove the proposal before it may gate (§D).

The ``checks`` task asks the model (through the same ``AGENT_REVIEW_COMMAND``
seam) to write a suite under ``.agent/checks/**``.  The model's word is not
evidence, so this module runs the **falsifiability validator** over every check
the model proposed and then rewrites the manifest so that ``validated`` reflects
what was *proven*, not what the model claimed:

* a check that passes the current revision and fails a seeded known-bad one is
  marked ``validated: true`` and may gate;
* a check that cannot be proven is written back as ``validated: false`` (even if
  the model had written ``true``) and may only report.

Two things this deliberately does **not** do: it never touches the user's suite,
and it never runs in the same task as a fix — the guard in
``services.agent_runner`` and the fact that the frozen base suite (not the
proposal) is what gates the curator's own run keep the two apart.

The report is JSON-shaped so the worker can persist it as provenance without
re-deriving anything.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from services.check_suite import (
    CHECK_DIR_RELPATH,
    CHECK_MANIFEST_NAMES,
    CheckManifest,
    resolve_ai_suite,
)
from services.check_validation import (
    CheckValidation,
    Seeder,
    validate_check,
)
from services.gates import (
    DEFAULT_GATE_TIMEOUT,
    CommandExecutor,
    suite_fingerprint,
)
from services.review_policy import dump_yaml, parse_yaml

logger = logging.getLogger("cpypiserver.check_curator")

#: Marker every curator proposal PR carries, in the title and the body, so the
#: proposal is auditable without reading the diff.
PROPOSAL_MARKER = "[openfish-checks-proposal]"

#: A proposal is a proposal, not a research project: cap how many checks one run
#: will validate, so a runaway model cannot turn a task into an hour of seeding.
MAX_CHECKS_PER_PROPOSAL = 20


@dataclass(frozen=True)
class CuratorReport:
    """What the curator actually proposed and what could be proven."""

    suite_hash: str
    source: str
    checks: tuple[dict[str, object], ...] = ()
    validations: tuple[CheckValidation, ...] = ()
    manifest_path: str | None = None
    notes: tuple[str, ...] = field(default=())

    @property
    def total(self) -> int:
        return len(self.checks)

    @property
    def validated(self) -> int:
        return sum(1 for item in self.validations if item.validated)

    @property
    def gating(self) -> bool:
        """True when at least one proposed check earned the right to gate."""
        return self.validated > 0

    def as_payload(self) -> dict[str, object]:
        return {
            "suite_hash": self.suite_hash,
            "source": self.source,
            "manifest_path": self.manifest_path,
            "total": self.total,
            "validated": self.validated,
            "gating": self.gating,
            "checks": [dict(item) for item in self.checks],
            "validations": [item.as_payload() for item in self.validations],
            "notes": list(self.notes),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "CuratorReport":
        """Rebuild a report from its :meth:`as_payload` form.

        The runner carries the report through the adapter protocol as JSON, so
        the PR-body renderer needs a way back to the object without a second
        shape.
        """
        validations = tuple(
            CheckValidation(
                check_id=str(item.get("check_id") or ""),
                status=str(item.get("status") or "unvalidated"),
                baseline_passed=bool(item.get("baseline_passed")),
                faults_attempted=int(item.get("faults_attempted") or 0),
                faults_detected=int(item.get("faults_detected") or 0),
                detected_by=tuple(str(name) for name in (item.get("detected_by") or ())),
                reason=str(item.get("reason") or ""),
            )
            for item in (payload.get("validations") or ())
            if isinstance(item, Mapping)
        )
        manifest = payload.get("manifest_path")
        return cls(
            suite_hash=str(payload.get("suite_hash") or ""),
            source=str(payload.get("source") or ""),
            checks=tuple(
                dict(item) for item in (payload.get("checks") or ())
                if isinstance(item, Mapping)
            ),
            validations=validations,
            manifest_path=str(manifest) if manifest else None,
            notes=tuple(str(note) for note in (payload.get("notes") or ())),
        )


def _manifest_path(repo_root: Path) -> Path | None:
    directory = repo_root / CHECK_DIR_RELPATH
    return next(
        (directory / name for name in CHECK_MANIFEST_NAMES if (directory / name).is_file()),
        None,
    )


def apply_validations(manifest_path: str | Path, validations: Iterable[CheckValidation]) -> int:
    """Write the *proven* validation status back into a suite manifest.

    Returns how many entries changed.  A check the validator could not prove is
    forced to ``validated: false`` — a curator run can therefore never promote
    its own check by claiming it, only by demonstrating it.
    """
    path = Path(manifest_path)
    try:
        raw = parse_yaml(path.read_text(encoding="utf-8"))
        manifest = CheckManifest.model_validate(dict(raw))
    except Exception as exc:  # a broken manifest is reported, not silently fixed
        logger.warning("cannot rewrite %s: %s", path, exc)
        return 0
    proven = {item.check_id: bool(item.validated) for item in validations}
    changed = 0
    for entry in manifest.checks:
        wanted = bool(proven.get(entry.id, False))
        if entry.validated != wanted:
            entry.validated = wanted
            changed += 1
    if changed:
        payload = manifest.model_dump(mode="json")
        path.write_text(dump_yaml(payload), encoding="utf-8")
        logger.info("rewrote %s: %d check(s) validation status updated", path, changed)
    return changed


def curate_workspace(
    repo_root: str | Path,
    *,
    executor: CommandExecutor | None = None,
    timeout: float = DEFAULT_GATE_TIMEOUT,
    seeders: Sequence[Seeder] | None = None,
    max_checks: int = MAX_CHECKS_PER_PROPOSAL,
    copier: Callable[[Path, Path], None] | None = None,
) -> CuratorReport:
    """Validate the suite the model just wrote, and record what it earned.

    Runs after the model has edited ``.agent/checks/**`` and before the commit:
    ``apply_validations`` is what makes the committed manifest carry proven
    status, so a later fix resolves a suite whose gating checks really can fail.
    """
    root = Path(repo_root)
    suite = resolve_ai_suite(root)
    notes: list[str] = []
    if not suite.checks:
        notes.append(
            f"提案里没有可解析的检查（模型可能没有写 {CHECK_DIR_RELPATH}/）；"
            "没有可验证的对象"
        )
    validations: list[CheckValidation] = []
    for check in suite.checks[: max(0, int(max_checks))]:
        validations.append(validate_check(
            check,
            repo_root=root,
            executor=executor,
            timeout=timeout,
            seeders=seeders,
            copier=copier,
        ))
    if len(suite.checks) > max_checks:
        notes.append(f"只验证了前 {max_checks} 个检查（共 {len(suite.checks)} 个）")

    manifest = _manifest_path(root)
    if manifest is not None and validations:
        changed = apply_validations(manifest, validations)
        if changed:
            notes.append(f"manifest 里 {changed} 个检查的 validated 状态被证伪结果改写")
        # Re-resolve so the report's descriptor reflects the rewritten flags.
        suite = resolve_ai_suite(root)
    elif manifest is None and validations:
        notes.append(
            "没有 checks.yml manifest：验证结果只进数据库，检查在树里仍是 "
            "unvalidated（不会门控），直到策展人补一个 manifest"
        )

    report = CuratorReport(
        suite_hash=suite_fingerprint(suite),
        source=suite.source,
        checks=tuple(check.model_dump() for check in suite.checks),
        validations=tuple(validations),
        manifest_path=str(manifest) if manifest is not None else None,
        notes=tuple(notes),
    )
    logger.info(
        "curator proposal: %d check(s), %d validated, gating=%s",
        report.total, report.validated, report.gating,
    )
    return report


def proposal_title(*, count: int, validated: int) -> str:
    """The title of a proposal PR, marker included."""
    return (
        f"{PROPOSAL_MARKER} AI 校验套件提案（{count} 个检查，{validated} 个已验证）"
    )


def proposal_body(report: CuratorReport, *, base_sha: str = "") -> str:
    """The proposal PR's body: what was proposed, what was proven, what is next."""
    lines = [
        f"## {PROPOSAL_MARKER}",
        "",
        "这是 **AI 策展人**（`checks` 任务）对 AI 校验套件 `.agent/checks/**` 的提案，"
        "与任何 fix PR 分离，需人工 review 后由人合并。",
        "",
        f"- 基础修订：`{base_sha or '（默认分支）'}`",
        f"- 套件指纹：`{report.suite_hash[:16]}`",
        f"- 检查数：{report.total}（其中 **{report.validated}** 个通过证伪验证，可门控）",
        f"- manifest：`{report.manifest_path or '（无）'}`",
        "",
        "### 检查与证伪结果",
        "",
        "| check | 结果 | 说明 |",
        "| --- | --- | --- |",
    ]
    for item in report.validations:
        mark = "✅ validated" if item.validated else "⚠️ unvalidated"
        lines.append(f"| `{item.check_id}` | {mark} | {item.reason} |")
    if report.notes:
        lines.extend(["", "### 备注", ""])
        lines.extend(f"- {note}" for note in report.notes)
    lines.extend([
        "",
        "> 未通过证伪验证的检查只能报告、不能门控。该 PR **不会自动合并**。",
    ])
    return "\n".join(lines)


__all__ = [
    "MAX_CHECKS_PER_PROPOSAL",
    "PROPOSAL_MARKER",
    "CuratorReport",
    "apply_validations",
    "curate_workspace",
    "proposal_body",
    "proposal_title",
]
