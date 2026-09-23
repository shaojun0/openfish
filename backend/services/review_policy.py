"""Review policy — ``.agent/review-policy.yml``, parsed and validated (§7).

The policy file is the one place a repository states *how* it wants to be
reviewed: which rules block, which may carry debt, which the agent may fix by
itself, and how long an exception is allowed to live.  This module is the only
reader of that file.

Three things are deliberate:

* **The schema is pydantic** (§7.2), so a malformed file fails with a usable
  message instead of an ``AttributeError`` three layers down.
* **A missing file is not an error.**  §7.3 requires a read-only built-in
  default (every rule blocking, every ``autofix`` false) and an honest
  ``policy_source: "builtin-default"`` so a caller can tell the two apart.  That
  also means the whole platform works on a repository that has never heard of
  the policy file.
* **The hash is over normalized content**, not bytes: re-indenting the YAML or
  reordering two rules does not make reports "incomparable", editing a rule
  does.  ``review_runs.policy_hash`` stores it, and :func:`hash_changed` is the
  test the first run after a change must pass before it claims comparability
  (§7.1).

``pyyaml`` is the only third-party import and it is loaded lazily: a checkout
without it still serves built-in defaults, and anything that genuinely needs to
parse a file gets a precise :class:`PolicyDependencyError` naming the package.
"""

from __future__ import annotations

import importlib
import json
import logging
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict, Field

from config.paths import PROJECT_ROOT
from services.digest import compute_sha256

logger = logging.getLogger("cpypiserver.review_policy")

#: Where the policy file lives inside a repository (§7.1).
POLICY_RELATIVE_PATH = ".agent/review-policy.yml"

#: Default location for this deployment's own repository root.  A caller with a
#: checkout elsewhere (an imported repo, a sandbox) passes its own root.
DEFAULT_POLICY_PATH = PROJECT_ROOT / POLICY_RELATIVE_PATH

#: ``policy_source`` values a caller may see.
SOURCE_FILE = "file"
SOURCE_BUILTIN = "builtin-default"

#: Levels a rule may declare.
LEVEL_BLOCKING = "blocking"
LEVEL_DEBT = "debt"
LEVELS: tuple[str, ...] = (LEVEL_BLOCKING, LEVEL_DEBT)

#: Exception statuses that make sense: an exception either declares "later" or
#: "never, for these paths".
EXCEPTION_STATUSES: tuple[str, ...] = ("wontfix", "acknowledged")

#: §9.3 step 6 / result-gated PR creation.  ``on_green`` (the default) only
#: pushes and opens a PR when no gate failed; ``always`` pushes regardless
#: (documented escape hatch); ``never`` is report-only.
PR_POLICY_ON_GREEN = "on_green"
PR_POLICY_ALWAYS = "always"
PR_POLICY_NEVER = "never"
PR_POLICIES: tuple[str, ...] = (PR_POLICY_ON_GREEN, PR_POLICY_ALWAYS, PR_POLICY_NEVER)
DEFAULT_PR_POLICY = PR_POLICY_ON_GREEN

#: Curator (the ``checks`` task) trigger modes (DESIGN-ai-checks.md §B).
#: ``off`` never proposes; ``bootstrap`` proposes once for a repo whose suite is
#: ``unverified``; ``auto`` additionally proposes when a push adds source files
#: with no corresponding check.  Default ``bootstrap``: a repository with no
#: verification gets exactly one proposal instead of PR spam, and one that
#: already has a suite is left alone.
CURATOR_OFF = "off"
CURATOR_BOOTSTRAP = "bootstrap"
CURATOR_AUTO = "auto"
CURATOR_MODES: tuple[str, ...] = (CURATOR_OFF, CURATOR_BOOTSTRAP, CURATOR_AUTO)
DEFAULT_CURATOR = CURATOR_BOOTSTRAP

#: Cooling-off window between two curator proposals for one repository, so a
#: burst of pushes cannot open a stack of proposal PRs.
DEFAULT_CURATOR_MIN_INTERVAL_SECONDS = 3600

#: §7.3 fallback when a rule with no ``default_due_days`` needs a deadline.
DEFAULT_DUE_DAYS = 30

#: ``escalation.rule_count_threshold`` fallback (§6.2 condition 3).
DEFAULT_RULE_COUNT_THRESHOLD = 20

#: ``escalation.rule_noise_threshold`` fallback (§6.3).
DEFAULT_RULE_NOISE_THRESHOLD = 3

#: §7.3 — the read-only default carries this rule set, all ``blocking`` and all
#: ``autofix: false``.  A policy file may narrow it; without a file, nothing is
#: fixable unattended and nothing is merely debt.
BUILTIN_RULES: tuple[str, ...] = (
    "backend.no-typing-optional",
    "backend.logger-name",
    "backend.dead-import",
    "backend.duplicate-implementation",
    "docs.missing-route-doc",
    "docs.route-table-drift",
    "openapi.schema-order",
    "gates.failed",
    "meta.rule-quality",
)


class PolicyError(Exception):
    """Base class for policy problems."""


class PolicyValidationError(PolicyError):
    """The document is structurally valid but breaks a §7.3 constraint.

    The route maps this to HTTP 400.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class PolicyDependencyError(PolicyError):
    """``pyyaml`` is required to read a policy file and is not installed."""

    def __init__(self, message: str | None = None) -> None:
        super().__init__(
            message
            or "reading .agent/review-policy.yml requires the 'pyyaml' package "
               "(see docs/agent-hub/integration/S3.md); built-in defaults are "
               "still available without it"
        )


# ── Schema (§7.2) ────────────────────────────────────────────────────

class PolicyDefaults(BaseModel):
    """``defaults`` — run-level switches."""

    model_config = ConfigDict(extra="forbid")

    auto_review: bool = True
    #: §12.1 noise spiral: hard ceiling on how many findings one run may open.
    max_findings_per_run: int = Field(default=50, ge=1)
    #: §9.3 step 6 / result-gated PR creation.  ``on_green`` pushes + opens a PR
    #: only when no gate failed; ``always`` pushes regardless; ``never`` is
    #: report-only.  A wrong value is a §7.3 validation failure, not a default.
    pr_policy: str = DEFAULT_PR_POLICY
    #: §6.4: a *review* task may escalate into a fix+PR only when a human opted
    #: this repository in.  Default false, exactly like per-rule ``autofix``.
    auto_fix: bool = False
    #: DESIGN-ai-checks.md §B: may the AI curator propose/evolve the AI check
    #: suite?  ``off | bootstrap | auto``, default ``bootstrap``.  Opt-in, and
    #: deliberately not a human-approval flow: the proposal is a normal PR.
    curator: str = DEFAULT_CURATOR
    #: Cooling-off between two curator proposals for this repository.
    curator_min_interval_seconds: int = Field(
        default=DEFAULT_CURATOR_MIN_INTERVAL_SECONDS, ge=0
    )


class RulePolicy(BaseModel):
    """One entry of ``rules``: the policy for a single ``rule_id``."""

    model_config = ConfigDict(extra="forbid")

    id: str
    level: str = LEVEL_DEBT
    default_due_days: int | None = Field(default=None, ge=0)
    #: §6.4 — default false; only a rule that says so may be fixed unattended.
    autofix: bool = False


class PolicyException(BaseModel):
    """One entry of ``exceptions``: a time-boxed reprieve for a rule+path set."""

    model_config = ConfigDict(extra="forbid")

    rule: str
    paths: list[str] = Field(default_factory=list)
    reason: str = ""
    decided_by: str | None = None
    #: §7.3 — mandatory and must be in the future.  No default on purpose: an
    #: exception without a deadline is the §12.1 silent-list spiral.
    due: date
    status: str = "wontfix"

    def covers(self, rule_id: str, path: str | None = None) -> bool:
        if self.rule != rule_id:
            return False
        if not self.paths:
            return True
        if path is None:
            return False
        return any(_path_matches(pattern, path) for pattern in self.paths)


class PolicyEscalation(BaseModel):
    """``escalation`` — the two thresholds §6.2/§6.3 consult."""

    model_config = ConfigDict(extra="forbid")

    rule_noise_threshold: int = Field(default=DEFAULT_RULE_NOISE_THRESHOLD, ge=1)
    rule_count_threshold: int = Field(default=DEFAULT_RULE_COUNT_THRESHOLD, ge=1)


class PolicyCheck(BaseModel):
    """One entry of ``checks``: a verification command, no Python required.

    ``.agent/checks/`` is the versioned suite an AI curator maintains; this
    field is the escape hatch for a human who wants a command in the same file
    as the rest of the policy.  Only ``command`` is required, and it is split
    into an argv by the suite resolver — never handed to a shell.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    command: str
    cwd: str = "."
    timeout: float | None = Field(default=None, gt=0)


class PolicyDocument(BaseModel):
    """The whole file.  ``policy_source`` / ``warnings`` / ``policy_hash`` are
    platform-side annotations, not file content."""

    model_config = ConfigDict(extra="forbid")

    version: int = 1
    defaults: PolicyDefaults = Field(default_factory=PolicyDefaults)
    rules: list[RulePolicy] = Field(default_factory=list)
    exceptions: list[PolicyException] = Field(default_factory=list)
    escalation: PolicyEscalation = Field(default_factory=PolicyEscalation)
    #: §7.2 extension: inline verification commands.  Resolution order (see
    #: ``services.check_suite``) puts ``.agent/checks/`` first and this second.
    checks: list[PolicyCheck] = Field(default_factory=list)

    # ── Platform annotations ────────────────────────────────────────
    policy_source: str = SOURCE_FILE
    policy_hash: str = ""
    warnings: list[str] = Field(default_factory=list)
    path: str | None = None

    # ── Derived accessors ───────────────────────────────────────────

    @property
    def readonly(self) -> bool:
        """A built-in default may never be written back to disk."""
        return self.policy_source == SOURCE_BUILTIN

    def find_rule(self, rule_id: str) -> RulePolicy | None:
        for entry in self.rules:
            if entry.id == rule_id:
                return entry
        return None

    #: Name used by :func:`services.findings.autofix_allowed`.
    def rule(self, rule_id: str) -> dict[str, Any]:
        """Effective per-rule settings; an unknown rule is conservative.

        Without a policy entry there is no evidence a human opted the rule in,
        so ``autofix`` is false — and under the built-in default (§7.3, no file
        at all) every rule is ``blocking``, which also means I3 refuses to
        ``wontfix`` it.
        """
        entry = self.find_rule(rule_id)
        if entry is None:
            return {
                "id": rule_id,
                "level": LEVEL_BLOCKING if self.readonly else LEVEL_DEBT,
                "default_due_days": None,
                "autofix": False,
                "known": False,
            }
        return {
            "id": entry.id,
            "level": entry.level if entry.level in LEVELS else LEVEL_DEBT,
            "default_due_days": entry.default_due_days,
            "autofix": bool(entry.autofix),
            "known": True,
        }

    def effective_level(self, rule_id: str, path: str | None = None) -> str:
        """Level after applying exceptions: an exception never *strengthens*.

        A path-scoped exception that moves a rule to ``debt`` is exactly the
        mechanism §7.3 exists for — it is how ``docs.missing-route-doc`` stops
        blocking one legacy file without weakening the rule everywhere else.
        """
        entry = self.rule(rule_id)
        level = str(entry["level"])
        for exception in self.exceptions:
            if not exception.covers(rule_id, path):
                continue
            if exception.status == "acknowledged" and level == LEVEL_BLOCKING:
                continue
            level = LEVEL_DEBT
        return level

    def exception_for(self, rule_id: str, path: str | None = None) -> PolicyException | None:
        for exception in self.exceptions:
            if exception.covers(rule_id, path):
                return exception
        return None

    def as_payload(self) -> dict[str, Any]:
        """The JSON shape ``GET /api/v1/policies/<slug>`` returns."""
        return {
            "version": self.version,
            "defaults": self.defaults.model_dump(),
            "rules": [entry.model_dump() for entry in self.rules],
            "exceptions": [entry.model_dump(mode="json") for entry in self.exceptions],
            "escalation": self.escalation.model_dump(),
            "checks": [entry.model_dump() for entry in self.checks],
            "policy_source": self.policy_source,
            "policy_hash": self.policy_hash,
            "warnings": list(self.warnings),
            "path": self.path,
            "readonly": self.readonly,
        }


def _path_matches(pattern: str, path: str) -> bool:
    """Glob-ish match: ``fnmatch`` for ``*``/``?``, plus a literal prefix."""
    from fnmatch import fnmatch

    if fnmatch(path, pattern):
        return True
    return path.startswith(pattern.rstrip("/") + "/")


# ── Canonical hash ───────────────────────────────────────────────────

def canonical_json(document: PolicyDocument) -> str:
    """Normalized semantic content — the bytes :func:`policy_hash` digests.

    Only the three policy-bearing sections are included: annotations such as
    ``warnings`` or ``path`` describe *this read*, not the policy, and comments
    and indentation never enter at all.  ``checks`` is appended only when it is
    non-empty, so adding the field did not invalidate every existing
    ``policy_hash`` in ``review_runs``.
    """
    payload = {
        "version": document.version,
        "defaults": document.defaults.model_dump(mode="json"),
        "rules": [entry.model_dump(mode="json") for entry in document.rules],
        "exceptions": [entry.model_dump(mode="json") for entry in document.exceptions],
        "escalation": document.escalation.model_dump(mode="json"),
    }
    if document.checks:
        payload["checks"] = [entry.model_dump(mode="json") for entry in document.checks]
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _digest_text(text: str) -> str:
    """SHA-256 of *text*, through the project's single hashing loop."""
    with tempfile.NamedTemporaryFile("w+b") as handle:
        handle.write(text.encode("utf-8"))
        handle.flush()
        return compute_sha256(handle.name, sidecar=False)


def policy_hash(document: PolicyDocument) -> str:
    """Stable hash of a document's meaning, for ``review_runs.policy_hash``."""
    return _digest_text(canonical_json(document))


def hash_changed(previous: str | None, current: str | None) -> bool:
    """Whether two ``policy_hash`` values make two reports incomparable.

    A missing previous hash (the first ever run for a repository) is *not* a
    change — there was nothing to compare against, and §7.1 only asks the run to
    say so after an actual edit.
    """
    if not previous or not current:
        return False
    return previous != current


# ── Built-in default (§7.3) ──────────────────────────────────────────

def builtin_default(*, path: str | Path | None = None) -> PolicyDocument:
    """The read-only default: everything blocking, nothing auto-fixable.

    This is what a repository without ``.agent/review-policy.yml`` is reviewed
    under, so it has to be the *strict* end of the spectrum: no rule is demoted
    to debt merely because nobody wrote a policy, and I3 then refuses every
    ``wontfix``.
    """
    document = PolicyDocument(
        version=1,
        defaults=PolicyDefaults(),
        rules=[
            RulePolicy(id=rule_id, level=LEVEL_BLOCKING, autofix=False)
            for rule_id in BUILTIN_RULES
        ],
        exceptions=[],
        escalation=PolicyEscalation(),
        policy_source=SOURCE_BUILTIN,
        path=str(path) if path is not None else None,
    )
    document.policy_hash = policy_hash(document)
    return document


def with_hash(document: PolicyDocument) -> PolicyDocument:
    """Recompute ``policy_hash`` and the §7.3 warnings in place."""
    document.warnings = check_warnings(document)
    document.policy_hash = policy_hash(document)
    return document


# ── YAML loading ─────────────────────────────────────────────────────

def _yaml() -> Any:
    """Import ``yaml`` lazily so its absence cannot break unrelated slices."""
    try:
        return importlib.import_module("yaml")
    except ImportError as exc:  # pragma: no cover - depends on the deployment
        raise PolicyDependencyError() from exc


def parse_yaml(text: str) -> Mapping[str, Any]:
    """Parse policy YAML into a mapping (``None`` becomes ``{}``)."""
    yaml = _yaml()
    try:
        loaded = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise PolicyValidationError(f"invalid YAML: {exc}") from exc
    if loaded is None:
        return {}
    if not isinstance(loaded, Mapping):
        raise PolicyValidationError(
            f"the policy document must be a mapping, got {type(loaded).__name__}"
        )
    return loaded


def dump(document: PolicyDocument) -> str:
    """Serialize a document back to YAML for ``PUT /api/v1/policies/<slug>``."""
    payload = {
        "version": document.version,
        "defaults": document.defaults.model_dump(mode="json"),
        "rules": [entry.model_dump(mode="json") for entry in document.rules],
        "exceptions": [entry.model_dump(mode="json") for entry in document.exceptions],
        "escalation": document.escalation.model_dump(mode="json"),
    }
    if document.checks:
        payload["checks"] = [entry.model_dump(mode="json") for entry in document.checks]
    return dump_yaml(payload)


def dump_yaml(payload: Mapping[str, Any]) -> str:
    """Serialize a plain mapping to YAML through the project's single loader.

    The suite manifest (``.agent/checks/checks.yml``) is written with this, so
    there is one YAML implementation rather than a second ``import yaml`` in
    :mod:`services.check_curator`.
    """
    yaml = _yaml()
    return yaml.safe_dump(
        dict(payload), allow_unicode=True, sort_keys=False, default_flow_style=False
    )


# ── Validation (§7.3) ────────────────────────────────────────────────

def _today(today: date | None = None) -> date:
    return today or datetime.now(timezone.utc).date()


def validate_document(
    document: PolicyDocument,
    *,
    today: date | None = None,
) -> list[str]:
    """Enforce §7.3; return the non-fatal warnings.

    Fatal (raise :class:`PolicyValidationError` → HTTP 400):

    * an exception with no ``due`` — pydantic refuses to build the model at all,
      which is why the failure surfaces from :func:`parse_document`;
    * an exception whose ``due`` is not strictly in the future.

    Non-fatal (returned as warnings):

    * an exception that names a ``rule_id`` the document does not define — it
      may be about a rule another slice ships, so it is reported, not rejected.
    """
    moment = _today(today)
    if document.defaults.pr_policy not in PR_POLICIES:
        raise PolicyValidationError(
            f"defaults.pr_policy={document.defaults.pr_policy}; "
            f"expected one of {', '.join(PR_POLICIES)}"
        )
    if document.defaults.curator not in CURATOR_MODES:
        raise PolicyValidationError(
            f"defaults.curator={document.defaults.curator}; "
            f"expected one of {', '.join(CURATOR_MODES)}"
        )
    known = {entry.id for entry in document.rules}
    warnings: list[str] = []
    for index, exception in enumerate(document.exceptions):
        if exception.due <= moment:
            raise PolicyValidationError(
                f"exceptions[{index}] ({exception.rule}) has due={exception.due.isoformat()}, "
                f"which is not after today ({moment.isoformat()})"
            )
        if exception.status not in EXCEPTION_STATUSES:
            raise PolicyValidationError(
                f"exceptions[{index}] ({exception.rule}) has status={exception.status}; "
                f"expected one of {', '.join(EXCEPTION_STATUSES)}"
            )
        if exception.rule not in known:
            warnings.append(
                f"exceptions[{index}] references rule {exception.rule}, which this "
                "policy does not define (unknown rule — the exception is inert "
                "until the rule exists)"
            )
    seen: set[str] = set()
    for entry in document.rules:
        if entry.id in seen:
            raise PolicyValidationError(f"rules declares {entry.id} more than once")
        seen.add(entry.id)
        if entry.level not in LEVELS:
            raise PolicyValidationError(
                f"rules[{entry.id}].level={entry.level}; expected one of {', '.join(LEVELS)}"
            )
    check_ids: set[str] = set()
    for index, check in enumerate(document.checks):
        if not check.id.strip():
            raise PolicyValidationError(f"checks[{index}] has an empty id")
        if check.id in check_ids:
            raise PolicyValidationError(f"checks declares {check.id} more than once")
        check_ids.add(check.id)
        if not check.command.strip():
            raise PolicyValidationError(f"checks[{check.id}].command is empty")
    return warnings


def check_warnings(document: PolicyDocument, *, today: date | None = None) -> list[str]:
    """The warning list alone — used when a document is already validated."""
    return validate_document(document, today=today)


def parse_document(
    raw: Mapping[str, Any],
    *,
    source: str = SOURCE_FILE,
    path: str | Path | None = None,
    today: date | None = None,
) -> PolicyDocument:
    """Build a validated :class:`PolicyDocument` from a mapping.

    Raises :class:`PolicyValidationError` for both ``pydantic`` rejections and
    §7.3 violations, so a route only ever has one exception to map to a 400.
    """
    try:
        document = PolicyDocument.model_validate(dict(raw))
    except Exception as exc:  # pydantic.ValidationError, kept import-free
        raise PolicyValidationError(f"policy schema violation: {exc}") from exc
    document.policy_source = source
    document.path = str(path) if path is not None else None
    document.warnings = validate_document(document, today=today)
    document.policy_hash = policy_hash(document)
    return document


# ── Loading ──────────────────────────────────────────────────────────

@dataclass(frozen=True)
class PolicyLoad:
    """A load result plus the reason, so a caller can say *why* it defaulted."""

    document: PolicyDocument
    source: str
    reason: str = ""


def load_file(
    path: str | Path,
    *,
    today: date | None = None,
) -> PolicyLoad:
    """Read one policy file; a missing file yields the built-in default.

    Validation failures are **not** swallowed into a default: a repository that
    ships a broken policy must hear about it (400 on write, a warning on read),
    not be silently reviewed under different rules than it declared.
    """
    location = Path(path)
    if not location.is_file():
        return PolicyLoad(
            document=builtin_default(path=location),
            source=SOURCE_BUILTIN,
            reason=f"{location} does not exist",
        )
    text = location.read_text(encoding="utf-8")
    raw = parse_yaml(text)
    document = parse_document(raw, source=SOURCE_FILE, path=location, today=today)
    return PolicyLoad(document=document, source=SOURCE_FILE)


def load(
    repo_root: str | Path | None = None,
    *,
    today: date | None = None,
) -> PolicyDocument:
    """The policy for *repo_root* (default: this deployment's repo root).

    The returned document always carries ``policy_source``, ``policy_hash`` and
    ``warnings``; §7.3's ``builtin-default`` marker is how a caller tells an
    absent file from an empty one.
    """
    root = Path(repo_root) if repo_root is not None else PROJECT_ROOT
    return load_file(root / POLICY_RELATIVE_PATH, today=today).document


#: Explicit alias for callers that think in repositories rather than roots.
def load_for_repo(repo_root: str | Path | None = None, **kwargs: Any) -> PolicyDocument:
    return load(repo_root, **kwargs)


def source_of(document: PolicyDocument) -> str:
    """``"file"`` or ``"builtin-default"`` — the §7.3 discriminator."""
    return document.policy_source


__all__ = [
    "BUILTIN_RULES",
    "CURATOR_AUTO",
    "CURATOR_BOOTSTRAP",
    "CURATOR_MODES",
    "CURATOR_OFF",
    "DEFAULT_CURATOR",
    "DEFAULT_CURATOR_MIN_INTERVAL_SECONDS",
    "DEFAULT_DUE_DAYS",
    "DEFAULT_POLICY_PATH",
    "DEFAULT_PR_POLICY",
    "DEFAULT_RULE_COUNT_THRESHOLD",
    "DEFAULT_RULE_NOISE_THRESHOLD",
    "EXCEPTION_STATUSES",
    "LEVELS",
    "LEVEL_BLOCKING",
    "LEVEL_DEBT",
    "POLICY_RELATIVE_PATH",
    "PR_POLICIES",
    "PR_POLICY_ALWAYS",
    "PR_POLICY_NEVER",
    "PR_POLICY_ON_GREEN",
    "SOURCE_BUILTIN",
    "SOURCE_FILE",
    "PolicyCheck",
    "PolicyDefaults",
    "PolicyDependencyError",
    "PolicyDocument",
    "PolicyError",
    "PolicyEscalation",
    "PolicyException",
    "PolicyLoad",
    "PolicyValidationError",
    "RulePolicy",
    "builtin_default",
    "canonical_json",
    "check_warnings",
    "dump",
    "dump_yaml",
    "hash_changed",
    "load",
    "load_file",
    "load_for_repo",
    "parse_document",
    "parse_yaml",
    "policy_hash",
    "source_of",
    "validate_document",
    "with_hash",
]
