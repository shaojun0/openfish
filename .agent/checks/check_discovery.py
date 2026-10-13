#!/usr/bin/env python
"""Gate: manifest auto-discovery, and the implicit gates hiding inside it.

Run from the repository root::

    python .agent/checks/check_discovery.py
    # dev box without a `python` on PATH:
    backend/.venv/bin/python .agent/checks/check_discovery.py

``services/check_discovery.py`` is how a repository that never heard of openfish
still gets verified: it turns the manifests everyone already has into a check
suite.  The commands it finds are marked ``validated``, which means they may
**block** a PR — and that pairing has exactly two ways to surprise someone:

1. **A gate nobody declared.**  Adding ``check:`` to a root ``Makefile`` is
   enough to enroll ``make check`` as a gating check.  Discovery is pure (it
   never executes anything, by design), so nothing warns the author at the
   moment they write the target.
2. **A gate that silently does not run.**  Providers are tried in priority order
   and only the first match is used, so a repository with a root
   ``package.json`` never sees the ``make check`` it also declared.

Neither is hypothetical: both were observed by hand before this gate existed,
and the suite reason now names a displaced or broken provider instead of
dropping it.  What the gate pins:

* this repository's root ``Makefile`` defines none of ``test`` / ``check`` /
  ``lint``, and its suite resolves from ``.agent/checks/`` — so nothing here can
  be enrolled as a gate by accident;
* the discovered set, its order, its argv and its ``validated`` marking follow
  the manifest (``:=`` / ``?=`` assignments are not targets, and a
  non-conventional target such as ``smoke-frontend`` is inert);
* a displaced or broken provider is named in the suite reason;
* ``merge=True`` runs every matched manifest;
* a ``CheckCommand`` that does not state its ``validation`` is report-only, and
  a suite with no gating check normalises to ``unverified`` rather than green;
* ``fix_guard_violations`` refuses a ``Makefile`` — a fix may not rewrite the
  declaration that judges it.

Agent-written, and ``validated: false`` in ``checks.yml``: it reports, it does
not gate.  Exits non-zero if any property fails.
"""

from __future__ import annotations

import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path


def _repo_root() -> Path:
    """The checkout this check belongs to.

    Walk up for the ``backend/config/seed`` marker rather than counting
    ``parents[2]``: a suite artifact that breaks when it is moved is the very
    failure mode this gate is about.
    """
    here = Path(__file__).resolve()
    for parent in (here.parent, *here.parents):
        if (parent / "backend" / "config" / "seed").is_dir():
            return parent
    return here.parents[2]


REPO_ROOT = _repo_root()
BACKEND_DIR = REPO_ROOT / "backend"
sys.path.insert(0, str(BACKEND_DIR))


def _tree(root: Path, name: str, files: Mapping[str, str]) -> Path:
    """A fresh manifest tree under *root* — one per scenario, so nothing leaks."""
    branch = root / name
    branch.mkdir()
    for filename, text in files.items():
        (branch / filename).write_text(text, encoding="utf-8")
    return branch


def main() -> int:
    try:
        from services.check_discovery import (
            MAKE_TARGETS,
            discover_checks,
            makefile_checks,
        )
        from services.check_suite import (
            fix_guard_violations,
            resolve_suite,
            resolve_user_suite,
        )
        from services.gates import (
            SOURCE_AGENT_CHECKS,
            SOURCE_DISCOVERY,
            SOURCE_UNVERIFIED,
            STATUS_UNVERIFIED,
            VALIDATION_VALIDATED,
            CheckCommand,
            CheckSuite,
            ExecOutcome,
            run_suite,
        )
    except ImportError as exc:
        print(f"FAIL cannot import the gate machinery: {exc}")
        print(
            "     run it with the backend interpreter, e.g. "
            "backend/.venv/bin/python .agent/checks/check_discovery.py"
        )
        return 1

    failures: list[str] = []
    checked = 0

    def found(ok: bool, label: str, detail: str = "") -> None:
        nonlocal checked
        checked += 1
        if ok:
            print(f"   ✅ {label}")
            return
        failures.append(f"{label} — {detail}" if detail else label)
        print(f"   ✗ {label}" + (f"\n     {detail}" if detail else ""))

    class RecordingExecutor:
        """Runs nothing; records the ids it was handed and reports success."""

        def __init__(self) -> None:
            self.ran: list[str] = []

        def run_command(
            self, command: CheckCommand, *, root: Path, timeout: float
        ) -> ExecOutcome:
            self.ran.append(command.id)
            return ExecOutcome(0, "", False, 0)

    # ── This repository ──────────────────────────────────────────────
    # Root targets, not the whole file: `makefile_checks` is the real parser, so
    # this cannot drift from what discovery does.
    enrolled = makefile_checks(REPO_ROOT)
    found(
        not enrolled,
        "root Makefile defines no test/check/lint target",
        "these would be discovered as validated (gating) checks in any repository "
        "without a versioned suite: " + ", ".join(c.id for c in enrolled),
    )

    resolved = resolve_suite(REPO_ROOT)
    found(
        resolved.source == SOURCE_AGENT_CHECKS and bool(resolved.checks),
        "this repository's suite resolves from .agent/checks/",
        f"source={resolved.source} checks={[c.id for c in resolved.checks]}",
    )

    with tempfile.TemporaryDirectory(prefix="check-discovery-") as tmp:
        root = Path(tmp)

        # ── What a manifest turns into ───────────────────────────────
        conventional = _tree(
            root,
            "makefile-conventional",
            {
                "Makefile": (
                    "ROOT := .\n"
                    "PY ?= python3\n"
                    "smoke-frontend:\n"
                    "\t@echo hi\n"
                    "test:\n"
                    "\t@true\n"
                    "lint:\n"
                    "\t@true\n"
                    "check:\n"
                    "\t@true\n"
                )
            },
        )
        suite = discover_checks(conventional)
        found(
            [c.id for c in suite.checks] == [f"make:{name}" for name in MAKE_TARGETS],
            "only test/check/lint become checks, in MAKE_TARGETS order",
            f"got {[c.id for c in suite.checks]}, want "
            f"{[f'make:{name}' for name in MAKE_TARGETS]}",
        )
        found(
            all(c.argv == ["make", c.id.split(":", 1)[1]] for c in suite.checks),
            "each check is an argv, not a shell string",
            f"argv={[c.argv for c in suite.checks]}",
        )
        found(
            all(
                c.validation == VALIDATION_VALIDATED and c.gating
                for c in suite.checks
            ),
            "conventional targets are validated — they may gate, hence this gate",
            f"validation={[c.validation for c in suite.checks]}",
        )
        found(
            suite.source == SOURCE_DISCOVERY,
            "the suite records that it came from discovery",
            f"source={suite.source}",
        )
        found(
            all(c.id != "make:smoke-frontend" for c in suite.checks),
            "a non-conventional target is inert",
            f"got {[c.id for c in suite.checks]}",
        )

        # ── Priority: what wins, and what is said about the loser ────
        both = _tree(
            root,
            "package-and-makefile",
            {
                "package.json": '{"scripts": {"test": "true"}}',
                "Makefile": "check:\n\t@true\n",
            },
        )
        winner = discover_checks(both)
        found(
            [c.id for c in winner.checks] == ["npm:test"],
            "the higher-priority manifest wins",
            f"got {[c.id for c in winner.checks]}",
        )
        found(
            "makefile_checks" in winner.reason,
            "the displaced provider is named in the reason",
            f"reason={winner.reason!r}",
        )
        merged = discover_checks(both, merge=True)
        found(
            [c.id for c in merged.checks] == ["npm:test", "make:check"],
            "merge=True runs every matched manifest",
            f"got {[c.id for c in merged.checks]}",
        )

        def _boom(where: Path) -> list[CheckCommand]:
            raise RuntimeError("broken manifest")

        broken = discover_checks(
            _tree(root, "broken-provider", {"Makefile": "check:\n\t@true\n"}),
            providers=(_boom,),
        )
        found(
            "_boom" in broken.reason and not broken.checks,
            "a provider that raises is named, not swallowed",
            f"reason={broken.reason!r}",
        )

        empty = _tree(root, "empty", {})
        nothing = discover_checks(empty)
        found(
            not nothing.checks and nothing.source == SOURCE_DISCOVERY,
            "a repository with no manifest yields an empty discovered suite",
            f"checks={[c.id for c in nothing.checks]} source={nothing.source}",
        )
        found(
            resolve_user_suite(empty).source == SOURCE_UNVERIFIED,
            "and resolution calls that unverified, never an empty pass",
            f"source={resolve_user_suite(empty).source}",
        )

    # ── The invariants that make the above safe ──────────────────────
    bare = CheckCommand(id="bare", argv=["true"])
    found(
        not bare.gating and bare.validation != VALIDATION_VALIDATED,
        "a CheckCommand that omits validation is report-only",
        f"validation={bare.validation} gating={bare.gating}",
    )

    report_only = CheckSuite(
        checks=[CheckCommand(id="report-only", argv=["true"])],
        source=SOURCE_UNVERIFIED,
        reason="report-only suite",
    )
    executor = RecordingExecutor()
    summary = run_suite(report_only, root=REPO_ROOT, executor=executor)
    found(
        executor.ran == ["report-only"],
        "an unvalidated check still runs, so its output is reported",
        f"ran={executor.ran}",
    )
    found(
        summary.state == STATUS_UNVERIFIED,
        "a suite with no gating check normalises to unverified",
        f"state={summary.state}",
    )

    violations = fix_guard_violations(["Makefile"])
    found(
        violations == ["Makefile"],
        "a fix may not rewrite the Makefile that declares what judges it",
        f"violations={violations}",
    )

    print(
        f"   · discovery on this repository: "
        f"{[c.id for c in discover_checks(REPO_ROOT).checks] or '（无）'}"
    )

    for line in failures:
        print(f"FAIL {line}")
    print(
        f"check_discovery: {checked} properties, {len(failures)} failure(s)",
        file=sys.stderr,
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
