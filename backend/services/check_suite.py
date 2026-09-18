"""The repository-versioned verification suite: resolve, freeze, guard.

The suite that judges a fix is itself a file in the repository (``.agent/checks/``
or ``checks:`` in ``.agent/review-policy.yml``), exactly like the review policy.
That is what makes it reviewable, and it is also what makes the hard invariant
necessary:

    **A check suite that gates a fix must be read-only with respect to that
    fix.  One run may never both produce a fix and weaken the checks that judge
    it.**

This module owns the three mechanisms that enforce it:

* :func:`resolve_suite` — the fixed resolution order (``.agent/checks/`` →
  ``checks:`` in the policy → manifest auto-discovery → ``unverified``);
* :func:`freeze_suite` — a descriptor resolved from the base/approved snapshot,
  fingerprinted, and reused for the post-fix re-run instead of being re-read
  from the modified working tree;
* :func:`fix_guard_violations` / :func:`curator_guard_violations` — the path
  guards: a ``fix`` run may not touch ``.agent/checks/**`` at all, and a
  ``checks`` run may touch *only* ``.agent/checks/**`` (plus test files).

Nothing here executes a command; :mod:`services.gates` does that.  Nothing here
writes a file either, so the whole invariant is unit-testable offline.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from services.check_discovery import discover_checks
from services.gates import (
    SOURCE_AGENT_CHECKS,
    SOURCE_POLICY,
    SOURCE_UNVERIFIED,
    VALIDATION_UNVALIDATED,
    VALIDATION_VALIDATED,
    CheckCommand,
    CheckSuite,
    suite_fingerprint,
)

logger = logging.getLogger("cpypiserver.check_suite")

#: The directory, relative to a repository root, that holds the versioned suite.
CHECK_DIR_RELPATH = ".agent/checks"

#: Manifest filenames accepted inside that directory (first match wins).
CHECK_MANIFEST_NAMES: tuple[str, ...] = ("checks.yml", "checks.yaml")

#: Script convention inside that directory, mirroring ``backend/scripts``.
CHECK_SCRIPT_GLOB = "check_*.py"

#: Suite manifest schema version.
MANIFEST_VERSION = 1

#: The paths a ``fix`` run may never write:
#:
#: * ``.agent/checks/**`` — the frozen AI suite: editing it is how a fixer would
#:   make its own verdict green;
#: * ``.agent/review-policy.yml`` — the human-owned suite declaration (and the
#:   policy itself); AGENTS.md already forbids editing it to make a rule vanish;
#: * any human-owned test path (see :func:`is_test_path`) — the user's suite is
#:   authority and the AI never modifies it;
#: * every path a resolved (user) suite actually executes, plus the files that
#:   *declare* how it runs (see :data:`SUITE_DECLARATION_NAMES`).  Freezing the
#:   descriptor without freezing its entrypoints was the hole: a fix could
#:   rewrite ``backend/scripts/check_lint.py`` or ``pyproject.toml`` and the
#:   post-fix ``on_green`` re-run would execute the weakened suite.
FIX_PROTECTED_PREFIXES: tuple[str, ...] = (CHECK_DIR_RELPATH,)
FIX_PROTECTED_PATHS: tuple[str, ...] = (".agent/review-policy.yml",)

#: Basenames that declare *how* a resolved suite runs.  A fix that edits one of
#: these changes the commands the post-fix gates execute without touching
#: ``.agent/checks/**``, so they are protected wherever they appear.
SUITE_DECLARATION_NAMES: frozenset[str] = frozenset({
    "Makefile", "makefile", "GNUmakefile",
    "package.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock",
    "pyproject.toml", "setup.cfg", "setup.py", "tox.ini", "pytest.ini",
    "conftest.py", "noxfile.py",
    "Cargo.toml", "Cargo.lock", "go.mod", "go.sum",
    "pom.xml", "build.gradle", "build.gradle.kts", "settings.gradle",
    "tsconfig.json", "jest.config.js", "jest.config.ts",
    "vitest.config.js", "vitest.config.ts", "vite.config.js", "vite.config.ts",
    ".mocharc.yml", ".mocharc.json",
})

#: Suffixes that mark an argv token as a script a suite executes.
_SCRIPT_SUFFIXES: tuple[str, ...] = (
    ".py", ".sh", ".bash", ".js", ".mjs", ".cjs", ".ts", ".rb", ".pl", ".ps1",
)

#: Producers of a resolved suite, for provenance in the result document.
AUTHOR_AI = "ai"
AUTHOR_HUMAN = "human"


# ── Manifest schema (.agent/checks/checks.yml) ───────────────────────

class ManifestCheck(BaseModel):
    """One ``checks:`` entry of the suite manifest.

    ``validated`` defaults to **false**: a check an agent just wrote has not
    demonstrated that it can fail, so it may report but not gate.  The
    falsifiability validator flips it (or a human does, explicitly).
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    command: str
    cwd: str = "."
    timeout: float | None = Field(default=None, gt=0)
    validated: bool = False


class CheckManifest(BaseModel):
    """The whole ``.agent/checks/checks.yml`` document."""

    model_config = ConfigDict(extra="forbid")

    version: int = MANIFEST_VERSION
    checks: list[ManifestCheck] = Field(default_factory=list)


# ── Provenance ───────────────────────────────────────────────────────

@dataclass(frozen=True)
class SuiteProvenance:
    """Who proposed a suite, and against which revision (I5 traceability).

    Recorded beside the cached snapshot so "which model, which task, which base
    commit produced this suite" is answerable without reading git history.
    """

    author: str = AUTHOR_AI
    model: str = ""
    task_id: str = ""
    base_sha: str = ""
    suite_hash: str = ""
    source: str = ""

    def as_payload(self) -> dict[str, str]:
        return {
            "author": self.author,
            "model": self.model,
            "task_id": self.task_id,
            "base_sha": self.base_sha,
            "suite_hash": self.suite_hash,
            "source": self.source,
        }


@dataclass(frozen=True)
class FrozenSuite:
    """A suite resolved from a base snapshot, safe to reuse after a fix.

    ``base_sha`` is the revision the descriptor was read from; ``fingerprint``
    is :func:`services.gates.suite_fingerprint`, which is what the platform
    caches and what the result document records.
    """

    suite: CheckSuite
    fingerprint: str
    base_sha: str = ""
    notes: tuple[str, ...] = field(default=())

    @property
    def source(self) -> str:
        return self.suite.source

    @property
    def resolved(self) -> bool:
        return self.suite.resolved


# ── Resolution order ─────────────────────────────────────────────────

def _yaml_mapping(text: str) -> Mapping[str, Any]:
    """Parse YAML through the policy module's single loader (lazy ``pyyaml``)."""
    from services.review_policy import parse_yaml

    return parse_yaml(text)


def load_agent_checks(repo_root: str | Path) -> CheckSuite:
    """The ``.agent/checks/`` provider — the versioned, approved suite.

    A ``checks.yml`` manifest wins when present; otherwise ``check_*.py`` files
    in the directory become one check each.  Manifest checks default to
    ``unvalidated`` (an agent may have just written them); a ``validated: true``
    entry is an explicit human statement that the check's falsifiability was
    demonstrated.
    """
    root = Path(repo_root)
    directory = root / CHECK_DIR_RELPATH
    if not directory.is_dir():
        return CheckSuite(
            checks=[],
            source=SOURCE_AGENT_CHECKS,
            reason=f"没有 {CHECK_DIR_RELPATH}/ 目录",
        )

    manifest_path = next(
        (directory / name for name in CHECK_MANIFEST_NAMES if (directory / name).is_file()),
        None,
    )
    if manifest_path is not None:
        return _load_manifest(manifest_path)

    scripts = sorted(directory.glob(CHECK_SCRIPT_GLOB))
    commands = [
        CheckCommand(
            id=path.stem,
            argv=["python", f"{CHECK_DIR_RELPATH}/{path.name}"],
            cwd=".",
            validation=VALIDATION_UNVALIDATED,
            source=SOURCE_AGENT_CHECKS,
        )
        for path in scripts
        if path.is_file()
    ]
    return CheckSuite(
        checks=commands,
        source=SOURCE_AGENT_CHECKS,
        reason=f"{CHECK_DIR_RELPATH}/ 下的 {len(commands)} 个 check_*.py",
    )


def _load_manifest(path: Path) -> CheckSuite:
    """Parse one suite manifest; a broken file resolves to an empty suite.

    A malformed manifest must not be silently ignored into a *different* suite
    without a trace, so the reason carries the parse error and the resolution
    falls through to the next provider — which for a broken ``.agent/checks/``
    is deliberate: the repository still gets its manifests' checks rather than
    no verification at all.
    """
    try:
        raw = _yaml_mapping(path.read_text(encoding="utf-8"))
    except Exception as exc:  # a broken manifest is a warning, not a crash
        logger.warning("%s 解析失败：%s", path, exc)
        return CheckSuite(
            checks=[],
            source=SOURCE_AGENT_CHECKS,
            reason=f"{path.name} 解析失败：{exc}",
        )
    try:
        manifest = CheckManifest.model_validate(dict(raw))
    except Exception as exc:
        logger.warning("%s schema 不合法：%s", path, exc)
        return CheckSuite(
            checks=[],
            source=SOURCE_AGENT_CHECKS,
            reason=f"{path.name} schema 不合法：{exc}",
        )
    commands: list[CheckCommand] = []
    for entry in manifest.checks:
        argv = entry.command.split()
        if not argv:
            continue
        commands.append(
            CheckCommand(
                id=entry.id,
                argv=argv,
                cwd=entry.cwd,
                timeout=entry.timeout,
                validation=(
                    VALIDATION_VALIDATED if entry.validated else VALIDATION_UNVALIDATED
                ),
                source=SOURCE_AGENT_CHECKS,
            )
        )
    return CheckSuite(
        checks=commands,
        source=SOURCE_AGENT_CHECKS,
        reason=f"{path.name} 声明了 {len(commands)} 个检查",
    )


def policy_checks(entries: Sequence[Mapping[str, Any]]) -> CheckSuite:
    """The ``checks:`` field of ``.agent/review-policy.yml`` as a suite.

    Human-authored (the policy file is edited through PR review), so these are
    ``validated`` and may gate.  Commands only — no Python file required.
    """
    commands: list[CheckCommand] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, Mapping):
            continue
        command = str(entry.get("command") or "").strip()
        argv = command.split()
        if not argv:
            continue
        check_id = str(entry.get("id") or f"policy:{index}").strip()
        timeout = entry.get("timeout")
        try:
            budget = float(timeout) if timeout is not None else None
        except (TypeError, ValueError):
            budget = None
        commands.append(
            CheckCommand(
                id=check_id,
                argv=argv,
                cwd=str(entry.get("cwd") or "."),
                timeout=budget,
                validation=VALIDATION_VALIDATED,
                source=SOURCE_POLICY,
            )
        )
    return CheckSuite(checks=commands, source=SOURCE_POLICY)


def resolve_ai_suite(repo_root: str | Path) -> CheckSuite:
    """The AI-maintained suite: ``.agent/checks/**`` and nothing else.

    Its own namespace is what keeps it decoupled from the user's suite: the
    curator may evolve it freely, and it can never shadow a repository check.
    """
    return load_agent_checks(repo_root)


def resolve_user_suite(
    repo_root: str | Path,
    *,
    policy_checks_field: Sequence[Mapping[str, Any]] = (),
    discovery: Sequence[Any] | None = None,
) -> CheckSuite:
    """The repository (user) suite: the authority when it exists.

    1. ``checks:`` in ``.agent/review-policy.yml`` — a human declared them;
    2. the repository's own native gates / manifests — its tests and CI.

    Deliberately **excludes** ``.agent/checks/``: the AI suite is not the user's.
    """
    root = Path(repo_root)
    from_policy = policy_checks(policy_checks_field)
    if from_policy.checks:
        return from_policy
    discovered = discover_checks(root, providers=discovery)
    if discovered.checks:
        return discovered
    return CheckSuite(
        checks=[],
        source=SOURCE_UNVERIFIED,
        reason="仓库没有自带校验（policy checks: 与 manifest 自动发现都没有命中）",
    )


@dataclass(frozen=True)
class SuitePair:
    """The two decoupled suites a run evaluates, reported separately.

    ``user`` is authority; ``ai`` is advisory.  Keeping them as separate objects
    (rather than one merged list) is what makes "never merge them into one
    anonymous green list" structurally true.
    """

    user: CheckSuite
    ai: CheckSuite


def resolve_suites(
    repo_root: str | Path,
    *,
    policy_checks_field: Sequence[Mapping[str, Any]] = (),
    discovery: Sequence[Any] | None = None,
) -> SuitePair:
    """Resolve both suites independently (governance model)."""
    return SuitePair(
        user=resolve_user_suite(
            repo_root, policy_checks_field=policy_checks_field, discovery=discovery
        ),
        ai=resolve_ai_suite(repo_root),
    )


def resolve_suite(
    repo_root: str | Path,
    *,
    policy_checks_field: Sequence[Mapping[str, Any]] = (),
    discovery: Sequence[Any] | None = None,
) -> CheckSuite:
    """Resolve the single suite for *repo_root*; first match wins.

    1. ``.agent/checks/`` — the versioned (AI) suite;
    2. ``checks:`` in ``.agent/review-policy.yml`` — commands only;
    3. manifest auto-discovery (:mod:`services.check_discovery`);
    4. otherwise an explicit **unverified** suite — never the runner image's own
       gates, and never an empty pass.

    Kept for callers that want one verdict (and for the Phase-1 contract); the
    two-suite governance path uses :func:`resolve_suites`.  ``discovery`` lets a
    caller inject the provider list (or a fake) without patching the module.
    """
    root = Path(repo_root)
    agent = resolve_ai_suite(root)
    if agent.checks:
        return agent
    user = resolve_user_suite(
        root, policy_checks_field=policy_checks_field, discovery=discovery
    )
    if user.checks:
        return user
    reasons = [item for item in (agent.reason, user.reason) if item]
    return CheckSuite(
        checks=[],
        source=SOURCE_UNVERIFIED,
        reason="；".join(reasons) or "没有可解析的校验套件",
    )


def freeze_suite(suite: CheckSuite, *, base_sha: str = "") -> FrozenSuite:
    """Fingerprint a resolved suite and pin it to *base_sha*.

    Once frozen, the descriptor is what every later run in the same task uses —
    including the post-fix re-run — so a fix cannot change the checks that judge
    it by editing the working tree between the two runs.
    """
    return FrozenSuite(
        suite=suite,
        fingerprint=suite_fingerprint(suite),
        base_sha=str(base_sha or ""),
        notes=(suite.reason,) if suite.reason else (),
    )


# ── Path guards (the hard invariant, in code) ────────────────────────

def normalize_repo_path(path: str) -> str:
    """A repo-relative POSIX path: no ``./``, no leading ``/``, no backslashes."""
    text = str(path or "").strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text.lstrip("/")


def _under(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(prefix.rstrip("/") + "/")


def suite_script_paths(suite: "CheckSuite") -> set[str]:
    """Repo-relative script paths named on a resolved suite's ``argv``.

    ``["python", "backend/scripts/check_lint.py"]`` yields that script; a bare
    ``pytest`` yields nothing (its configuration is covered by
    :data:`SUITE_DECLARATION_NAMES` instead).
    """
    found: set[str] = set()
    for check in getattr(suite, "checks", ()) or ():
        for part in getattr(check, "argv", ()) or ():
            text = normalize_repo_path(str(part))
            if not text:
                continue
            name = text.rsplit("/", 1)[-1]
            if "/" in text or name.endswith(_SCRIPT_SUFFIXES):
                found.add(text.lstrip("./"))
    return found


#: Declaration files that a build/test command may legitimately rewrite (an
#: installer updates them), so they are protected by the path guard but **not**
#: chmod-locked for the duration of a run.
_LOCK_UNSAFE_DECLARATIONS: frozenset[str] = frozenset({
    "package-lock.json", "pnpm-lock.yaml", "yarn.lock", "Cargo.lock", "go.sum",
})


def suite_protected_paths(suite: "CheckSuite", *, root: str | Path | None = None) -> tuple[str, ...]:
    """Repo-relative files that decide what *suite* runs, for read-only locking.

    The path guard (:func:`fix_guard_violations` with *suite*) is the authority;
    this is the OS-level backstop the runner applies for the duration of a fix,
    so an opportunistic write to the script that judges the fix fails early.
    Only paths that exist under *root* are returned; without *root* the suite's
    own argv scripts are returned unfiltered.

    Lock files are excluded: an installer that a check invokes may need to
    rewrite them, and the path guard still refuses to *publish* such a change.
    """
    found = set(suite_script_paths(suite))
    base = Path(root) if root is not None else None
    if base is not None:
        # Top-level declarations only: `conftest.py` in a subdirectory is still
        # covered by the path guard, and a full tree walk on every fix is not
        # worth the latency.
        for name in SUITE_DECLARATION_NAMES - _LOCK_UNSAFE_DECLARATIONS:
            if (base / name).is_file():
                found.add(name)
        return tuple(sorted(path for path in found if (base / path).is_file()))
    return tuple(sorted(found))


def fix_guard_violations(
    paths: Iterable[str] = (),
    *,
    suite: "CheckSuite | None" = None,
) -> list[str]:
    """Paths a ``fix`` run wrote that it must never write.

    This is the concrete enforcement of "the same run may not both produce a fix
    and weaken the checks that judge it": whatever the fixer did to the frozen
    suite, the user's test paths, the policy file or the resolved suite's own
    entrypoints, the run fails loudly at commit time instead of pushing a green
    PR.  ``.agent/checks/**`` is the AI suite, ``.agent/review-policy.yml``
    declares the user suite, any test path is the user's own authority, and
    *suite* — when given — contributes both the scripts it executes and the
    files that declare how it runs.
    """
    executed = suite_script_paths(suite) if suite is not None else set()
    violations: list[str] = []
    for path in paths:
        normalized = normalize_repo_path(path)
        if normalized in FIX_PROTECTED_PATHS:
            violations.append(normalized)
            continue
        if any(_under(normalized, prefix) for prefix in FIX_PROTECTED_PREFIXES):
            violations.append(normalized)
            continue
        if is_test_path(normalized):
            violations.append(normalized)
            continue
        if normalized in executed:
            violations.append(normalized)
            continue
        if normalized.rsplit("/", 1)[-1] in SUITE_DECLARATION_NAMES:
            violations.append(normalized)
    return violations


#: Test-file shapes a curator may add outside ``.agent/checks/**`` (a check is
#: useless without the fixture or test it drives).
_TEST_PATTERNS: tuple[str, ...] = (
    "test_*.py",
    "*_test.py",
    "*.test.js",
    "*.test.ts",
    "*.test.tsx",
    "*.test.jsx",
    "*.spec.js",
    "*.spec.ts",
    "*.spec.tsx",
    "*.spec.jsx",
    "*_test.go",
    "*_test.rs",
)

_TEST_DIRS: tuple[str, ...] = ("tests", "test")


def is_test_path(path: str) -> bool:
    """Whether *path* looks like a test file a curator may add.

    Matches by filename shape (``test_*.py``, ``*.spec.ts``, …) **and** by any
    ``tests``/``test`` path segment, so ``backend/tests/y.py`` counts as a
    human-owned test path even though it is not at the repository root.
    """
    from fnmatch import fnmatch

    normalized = normalize_repo_path(path)
    name = normalized.rsplit("/", 1)[-1]
    if any(fnmatch(name, pattern) for pattern in _TEST_PATTERNS):
        return True
    segments = normalized.split("/")[:-1]
    return any(segment in _TEST_DIRS for segment in segments)


def curator_guard_violations(paths: Iterable[str] = ()) -> list[str]:
    """Paths a ``checks`` run wrote that are outside its remit.

    A curator may write ``.agent/checks/**`` and add test files; touching
    production source would let it "fix" the code instead of the checks, which
    is the fixer's job and a different PR.
    """
    violations: list[str] = []
    for path in paths:
        normalized = normalize_repo_path(path)
        if _under(normalized, CHECK_DIR_RELPATH) or is_test_path(normalized):
            continue
        violations.append(normalized)
    return violations


__all__ = [
    "AUTHOR_AI",
    "AUTHOR_HUMAN",
    "CHECK_DIR_RELPATH",
    "CHECK_MANIFEST_NAMES",
    "CHECK_SCRIPT_GLOB",
    "FIX_PROTECTED_PATHS",
    "FIX_PROTECTED_PREFIXES",
    "MANIFEST_VERSION",
    "CheckManifest",
    "FrozenSuite",
    "ManifestCheck",
    "SuitePair",
    "SuiteProvenance",
    "SUITE_DECLARATION_NAMES",
    "curator_guard_violations",
    "fix_guard_violations",
    "freeze_suite",
    "is_test_path",
    "load_agent_checks",
    "normalize_repo_path",
    "policy_checks",
    "suite_protected_paths",
    "suite_script_paths",
    "resolve_ai_suite",
    "resolve_suite",
    "resolve_suites",
    "resolve_user_suite",
]
