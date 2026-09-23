#!/usr/bin/env python
"""Gate: every environment variable has exactly one owner.

Run from the backend directory (`backend/`)::

    python scripts/check_env_ownership.py

Configuration used to be read wherever it was needed: ``agent_runner`` had its
own ``_env_int``/``_env_float``, ``agent_queue`` its own ``int(...) or 0``,
``agent_worker`` read ``AGENT_REVIEW_*`` inline, ``repo_import`` carried a
``CONFIG_ITEMS`` table plus a private ``number()`` parser, and two modules each
declared their own constant for the *same* ``AGENT_WORK_ROOT``.  A value could
disagree with itself depending on which module asked, and a typo'd variable was
indistinguishable from an unset one.

Every one of those variables is now a field on :mod:`config`, read once when the
process starts.  This gate is what keeps it that way, in three parts:

1. **The deployment's variables are all owned.**  Every variable
   ``docker/docker-compose.yml`` hands the backend and the runner container must
   resolve to a settings field, or be listed below with the reason it is not
   configuration.  A variable no field reads is invisible configuration — it
   looks like a working deployment knob and does nothing.
2. **The application does not read the environment.**  A hand-written
   ``os.environ`` outside ``config/`` is the thing this refactor removed: a value
   read at call time cannot be validated at start-up, and it can be re-pointed
   while the process runs.  A short allowlist records the places where the
   *ambient* environment is legitimately the subject rather than the
   configuration.
3. **The names still map.**  Field naming is asserted structurally, so renaming a
   field cannot silently detach the documented variable from the process.

``scripts/`` is deliberately **not** scanned by part 2: a gate configures the
process it is about to import by setting the environment first, which is exactly
how a deployment configures it, and that is the one point where doing so is
correct rather than a leak.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import yaml  # noqa: E402

from config import Settings  # noqa: E402
from config.agent import AgentConfig  # noqa: E402
from config.base import EnvSettings  # noqa: E402
from config.forgejo import ForgejoConfig  # noqa: E402
from config.keys import KeysConfig  # noqa: E402

#: The running application.  ``scripts/`` is excluded on purpose — see the module
#: docstring.
APP_TARGETS: tuple[str, ...] = (
    "app.py", "cli.py", "debian_offline_cli.py", "errors.py", "schemas.py",
    "auth", "config", "extensions", "index", "models", "openapi", "routes",
    "services",
)

#: Files allowed to name ``os.environ`` by hand, each with the reason the
#: configuration layer cannot own it.  Everything here is the *ambient*
#: environment being handled as an object — filtered, forwarded to a child, or
#: read under a name the operator supplies — never a deployment knob.
ALLOWED_ENVIRON: dict[str, str] = {
    "services/sandbox_env.py": (
        "reads the ambient environment in order to filter it down to the "
        "allowlist an untrusted child may inherit; the ambient environment is "
        "the subject of the function, not a value it is configured by"
    ),
    "services/gates.py": (
        "ENV_INHERIT mode, the documented opt-out for the project's *own* "
        "check_*.py, which legitimately need the CI environment"
    ),
    "services/repo_import.py": (
        "the environment handed to the git client, so proxies, GIT_SSL_CAINFO "
        "and the operator's git config survive; this runs trusted code, unlike "
        "the sandbox children"
    ),
    "services/validation.py": (
        "writes a cache location for the third-party GuardDog library to read; "
        "it reads nothing and the value comes from settings.security"
    ),
    "cli.py": (
        "--token-env and --key-env name the variable on the command line so the "
        "secret never becomes an argv value; the name is operator input, and the "
        "value is stored, not read as configuration"
    ),
    "debian_offline_cli.py": (
        "OPENFISH_URL / OPENFISH_API_KEY address a remote server this standalone "
        "relay client talks to, from a machine that has no deployment settings"
    ),
}

#: Compose variables that are deliberately not configuration: they tune the
#: container runtime or a child toolchain rather than this process.
NOT_CONFIG: dict[str, str] = {
    "GIT_TERMINAL_PROMPT": (
        "set for the git children of the runner so a missing credential fails "
        "instead of blocking on a prompt"
    ),
    "PYTHONDONTWRITEBYTECODE": (
        "set so a repository's own check_*.py cannot litter the checkout with "
        "__pycache__ directories the runner then has to reason about"
    ),
}

#: The two compose anchors that are this application's environment.
COMPOSE_ANCHORS: tuple[str, ...] = ("x-backend-env", "x-runner-env")


def owned_names() -> set[str]:
    """Every environment variable name the settings declare.

    Derived by walking :class:`config.Settings`'s groups, so a new group is
    covered the moment it is registered — there is no second list to update.
    """
    names: set[str] = set()
    for field in Settings.model_fields.values():
        group = field.annotation
        if isinstance(group, type) and issubclass(group, EnvSettings):
            names.update(group.env_name(name) for name in group.model_fields)
    return names


def environ_sites(path: Path) -> list[int]:
    """Line numbers where *path* reads ``os.environ`` / ``os.getenv`` in code.

    Parsed rather than grepped: several modules *mention* ``os.environ`` in a
    docstring while doing the right thing, and a text search would flag them.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    lines: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == "environ":
            target = node.value
            if isinstance(target, ast.Name) and target.id == "os":
                lines.append(node.lineno)
        elif isinstance(node, ast.Call):
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr == "getenv"
                and isinstance(func.value, ast.Name)
                and func.value.id == "os"
            ):
                lines.append(node.lineno)
    return sorted(lines)


def scan_app_sources() -> tuple[list[str], list[str]]:
    """``(offending, stale_allowlist_entries)`` for the app tree."""
    offending: list[str] = []
    seen: set[str] = set()
    for target in APP_TARGETS:
        root = REPO_ROOT / target
        paths = [root] if root.is_file() else sorted(root.rglob("*.py"))
        for path in paths:
            if "__pycache__" in path.parts:
                continue
            lines = environ_sites(path)
            if not lines:
                continue
            relative = path.relative_to(REPO_ROOT).as_posix()
            seen.add(relative)
            if relative in ALLOWED_ENVIRON:
                continue
            offending.append(
                f"{relative}:{', '.join(str(line) for line in lines)}"
            )
    stale = sorted(set(ALLOWED_ENVIRON) - seen)
    return offending, stale


def compose_env(compose_path: Path) -> tuple[dict[str, object], list[str]]:
    """The two anchors' variables, and why the file could not be read."""
    if not compose_path.is_file():
        return {}, [f"{compose_path} is missing — cannot check the deployment's variables"]
    document = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        return {}, [f"{compose_path} did not parse as a mapping"]
    found: dict[str, object] = {}
    problems: list[str] = []
    for anchor in COMPOSE_ANCHORS:
        block = document.get(anchor)
        if not isinstance(block, dict) or not block:
            problems.append(f"{compose_path.name}: {anchor} is missing or empty")
            continue
        found[anchor] = block
    return found, problems


def check_deployment_variables() -> list[str]:
    """Part 1 — every variable the deployment sets is owned by a field."""
    failures: list[str] = []
    anchors, problems = compose_env(REPO_ROOT.parent / "docker" / "docker-compose.yml")
    failures.extend(problems)
    if problems:
        return failures

    owned = owned_names()
    present: set[str] = set()
    for anchor in COMPOSE_ANCHORS:
        block = anchors[anchor]
        assert isinstance(block, dict)
        for name in block:
            present.add(str(name))
            if name in owned or name in NOT_CONFIG:
                continue
            failures.append(
                f"{anchor}: {name} is set by the deployment but no config field "
                "reads it (add the field, or record it in NOT_CONFIG with a reason)"
            )
    for name in sorted(set(NOT_CONFIG) - present):
        failures.append(
            f"NOT_CONFIG lists {name}, which no anchor sets any more — drop the "
            "stale entry so the allowlist cannot become a dumping ground"
        )
    return failures


def check_app_sources() -> list[str]:
    """Part 2 — the application does not read the environment itself."""
    offending, stale = scan_app_sources()
    failures = [
        f"{site} reads os.environ directly (configuration belongs on config.*; "
        "see the module docstring for the documented exceptions)"
        for site in offending
    ]
    failures.extend(
        f"ALLOWED_ENVIRON lists {name}, which no longer reads os.environ — drop "
        "the stale entry"
        for name in stale
    )
    return failures


def check_name_mapping() -> list[str]:
    """Part 3 — the fields still resolve to the documented variable names."""
    failures: list[str] = []
    for field in AgentConfig.model_fields:
        name = AgentConfig.env_name(field)
        if not name.startswith("AGENT_"):
            failures.append(f"AgentConfig.{field} resolves to {name}, expected AGENT_*")
    for group in (ForgejoConfig, KeysConfig):
        for field in group.model_fields:
            name = group.env_name(field)
            if name != field.upper():
                failures.append(
                    f"{group.__name__}.{field} resolves to {name}, expected "
                    f"{field.upper()} (the deployment already speaks the "
                    "field-name-upper-cased form)"
                )
    if "IMPORT_SOURCE_TOKEN" not in owned_names():
        failures.append(
            "IMPORT_SOURCE_TOKEN is no longer owned by a config field — it is the "
            "one remote-source credential compose does not set, so nothing else "
            "would notice"
        )
    return failures


def main() -> int:
    failures = [
        *check_deployment_variables(),
        *check_app_sources(),
        *check_name_mapping(),
    ]
    if failures:
        print(f"❌ {len(failures)} environment-ownership problem(s)")
        for failure in failures:
            print("   " + failure)
        return 1
    print(
        f"✅ {len(owned_names())} variable(s) owned by config/, none read by hand "
        "in the application, every compose variable accounted for"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
