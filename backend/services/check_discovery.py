"""Zero-config check discovery from a repository's own manifests.

When a repository declares its test/lint/build commands in the places everyone
already uses, nobody should have to write an openfish-specific file to get
verified.  This module reads those declarations and turns them into a
:class:`~services.gates.CheckSuite` — **purely**: it never executes a command,
never writes a file and never touches the network, so it is safe to run during
resolution and trivial to test offline.

Providers, in priority order (first non-empty one wins):

1. ``backend/scripts/check_*.py`` — the repository's own long-standing gates
   (the openfish convention).  Reused verbatim from
   :func:`services.gates.suite_from_scripts`; an explicit convention beats a
   guess from a manifest.
2. ``package.json`` ``scripts`` — ``test`` / ``lint`` / ``build`` /
   ``typecheck``.
3. Python — ``pytest.ini`` or ``[tool.pytest]`` + tests, ``ruff``, ``mypy``.
4. ``go.mod`` — ``go build ./...`` / ``go vet ./...`` / ``go test ./...``.
5. ``Cargo.toml`` — ``cargo check`` / ``cargo test``.
6. ``Makefile`` — the ``test`` / ``check`` / ``lint`` targets, when defined.

A provider is a plain callable ``(repo_root) -> list[CheckCommand]``, and
:func:`discover_checks` takes the provider tuple as an argument, so a caller can
inject a different set (or a fake manifest reader) without patching the module.

The commands are marked ``validated``: they are the repository's own
pre-existing declarations, not something an agent wrote in this run, so they may
gate a PR.  A check the *curator* proposes lands in ``.agent/checks/`` and stays
``unvalidated`` until the falsifiability validator confirms it.
"""

from __future__ import annotations

import json
import logging
import re
import tomllib
from collections.abc import Callable, Sequence
from pathlib import Path

from services.gates import (
    SOURCE_DISCOVERY,
    VALIDATION_VALIDATED,
    CheckCommand,
    CheckSuite,
    suite_from_scripts,
)

logger = logging.getLogger("cpypiserver.check_discovery")

#: The interpreter every discovered Python command uses.  Deliberately the bare
#: name, not ``sys.executable``: the suite hash must not depend on which
#: interpreter happened to resolve the suite, and the runner image always has
#: ``python`` on ``PATH``.
PYTHON = "python"

#: ``package.json`` scripts worth running, in the order a failure should surface.
NPM_SCRIPTS: tuple[str, ...] = ("test", "lint", "build", "typecheck")

#: ``Makefile`` targets worth running, in order.
MAKE_TARGETS: tuple[str, ...] = ("test", "check", "lint")

#: A ``Makefile`` target line: ``name:`` at column 0 (a recipe line never is).
_MAKE_TARGET_RE = re.compile(r"^(?P<name>[A-Za-z0-9_.-]+)\s*:(?!=)")

#: Where the openfish gate convention lives inside a repository.
NATIVE_SCRIPTS_DIR = Path("backend") / "scripts"

Provider = Callable[[Path], list[CheckCommand]]


# ── Helpers ──────────────────────────────────────────────────────────

def _read_text(path: Path) -> str | None:
    """Best-effort UTF-8 read; an unreadable manifest simply matches nothing."""
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _command(
    check_id: str,
    argv: Sequence[str],
    *,
    timeout: float | None = None,
) -> CheckCommand:
    return CheckCommand(
        id=check_id,
        argv=[str(part) for part in argv],
        cwd=".",
        timeout=timeout,
        validation=VALIDATION_VALIDATED,
        source=SOURCE_DISCOVERY,
    )


def _toml_tables(text: str) -> set[str]:
    """Every dotted table name in a TOML document (``{"tool", "tool.ruff"}``).

    Falls back to a textual section scan when the document does not parse, so a
    half-written ``pyproject.toml`` still yields a best-effort answer instead of
    making discovery explode.
    """
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return {match.group(1).strip() for match in re.finditer(r"^\[([^\]]+)\]", text, re.M)}
    tables: set[str] = set()

    def walk(node: object, prefix: str) -> None:
        if not isinstance(node, dict):
            return
        for key, value in node.items():
            name = f"{prefix}.{key}" if prefix else str(key)
            tables.add(name)
            if isinstance(value, dict):
                walk(value, name)

    walk(data, "")
    return tables


# ── Providers ────────────────────────────────────────────────────────

def native_script_checks(root: Path) -> list[CheckCommand]:
    """``backend/scripts/check_*.py`` — the openfish gate convention."""
    scripts = Path(root) / NATIVE_SCRIPTS_DIR
    if not scripts.is_dir():
        return []
    return list(suite_from_scripts(scripts, root=root).checks)


def package_json_checks(root: Path) -> list[CheckCommand]:
    """``package.json`` ``scripts`` entries: test / lint / build / typecheck."""
    text = _read_text(Path(root) / "package.json")
    if not text:
        return []
    try:
        payload = json.loads(text)
    except ValueError:
        return []
    scripts = payload.get("scripts") if isinstance(payload, dict) else None
    if not isinstance(scripts, dict):
        return []
    return [
        _command(f"npm:{name}", ["npm", "run", name])
        for name in NPM_SCRIPTS
        if isinstance(scripts.get(name), str) and scripts.get(name).strip()
    ]


def python_checks(root: Path) -> list[CheckCommand]:
    """pytest / ruff / mypy — only when the repository actually declares them."""
    root = Path(root)
    pyproject = _read_text(root / "pyproject.toml") or ""
    tables = _toml_tables(pyproject) if pyproject else set()
    checks: list[CheckCommand] = []
    if _has_python_tests(root, tables):
        checks.append(_command("pytest", [PYTHON, "-m", "pytest", "-q"]))
    if "tool.ruff" in tables or (root / "ruff.toml").is_file() or (root / ".ruff.toml").is_file():
        checks.append(_command("ruff", [PYTHON, "-m", "ruff", "check", "."]))
    if "tool.mypy" in tables or (root / "mypy.ini").is_file() or (root / ".mypy.ini").is_file():
        checks.append(_command("mypy", [PYTHON, "-m", "mypy", "."]))
    return checks


def _has_python_tests(root: Path, tables: set[str]) -> bool:
    """Whether the repository looks like it has tests worth running."""
    if (root / "pytest.ini").is_file():
        return True
    if any(name.startswith("tool.pytest") for name in tables):
        return True
    if (root / "tests").is_dir():
        return True
    for pattern in ("test_*.py", "*_test.py", "*/test_*.py", "*/*_test.py", "tests/**/test_*.py"):
        if next(iter(root.glob(pattern)), None) is not None:
            return True
    return False


def go_checks(root: Path) -> list[CheckCommand]:
    """``go.mod``: build, vet and test the whole module as three checks."""
    if not (Path(root) / "go.mod").is_file():
        return []
    return [
        _command("go:build", ["go", "build", "./..."]),
        _command("go:vet", ["go", "vet", "./..."]),
        _command("go:test", ["go", "test", "./..."]),
    ]


def cargo_checks(root: Path) -> list[CheckCommand]:
    """``Cargo.toml``: ``cargo check`` then ``cargo test``."""
    if not (Path(root) / "Cargo.toml").is_file():
        return []
    return [
        _command("cargo:check", ["cargo", "check"]),
        _command("cargo:test", ["cargo", "test"]),
    ]


def makefile_checks(root: Path) -> list[CheckCommand]:
    """``Makefile``: only the conventional targets that are actually defined."""
    text = _read_text(Path(root) / "Makefile")
    if not text:
        return []
    defined = {
        match.group("name")
        for line in text.splitlines()
        for match in [_MAKE_TARGET_RE.match(line)]
        if match is not None
    }
    return [
        _command(f"make:{name}", ["make", name])
        for name in MAKE_TARGETS
        if name in defined
    ]


#: Providers in priority order; the first that yields a check wins.  Override
#: the whole tuple to inject a different discovery policy.
PROVIDERS: tuple[Provider, ...] = (
    native_script_checks,
    package_json_checks,
    python_checks,
    go_checks,
    cargo_checks,
    makefile_checks,
)


# ── Entry point ──────────────────────────────────────────────────────

def discover_checks(
    repo_root: str | Path,
    *,
    providers: Sequence[Provider] | None = None,
) -> CheckSuite:
    """The first manifest provider that recognises *repo_root*, or an empty suite.

    An empty suite is a legitimate answer — it is what resolution turns into the
    explicit ``unverified`` state.  It is never an empty *pass*.
    """
    root = Path(repo_root)
    for provider in providers if providers is not None else PROVIDERS:
        try:
            checks = list(provider(root))
        except Exception as exc:  # a broken manifest must not break resolution
            logger.warning("check discovery provider %s failed: %s", provider.__name__, exc)
            continue
        if checks:
            # The suite-level source is taken from the winning provider's checks
            # when they agree (the native ``check_*.py`` provider tags its
            # commands ``scripts``), so provenance stays accurate either way.
            sources = {check.source for check in checks}
            source = sources.pop() if len(sources) == 1 else SOURCE_DISCOVERY
            return CheckSuite(
                checks=checks,
                source=source,
                reason=f"auto-discovered by {provider.__name__}",
            )
    return CheckSuite(
        checks=[],
        source=SOURCE_DISCOVERY,
        reason="没有可识别的 manifest（package.json / pyproject.toml / go.mod / "
               "Cargo.toml / Makefile 都没有给出可用命令）",
    )


__all__ = [
    "MAKE_TARGETS",
    "NATIVE_SCRIPTS_DIR",
    "NPM_SCRIPTS",
    "PROVIDERS",
    "PYTHON",
    "Provider",
    "cargo_checks",
    "discover_checks",
    "go_checks",
    "makefile_checks",
    "native_script_checks",
    "package_json_checks",
    "python_checks",
]
