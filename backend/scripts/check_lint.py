#!/usr/bin/env python
"""Gate: the backend has no undefined names and no dead imports.

Run from the backend directory (`backend/`)::

    python scripts/check_lint.py

Every other gate checks *behaviour*; this one checks the code itself.  That
matters because the failure mode of a refactor is quiet: move a helper, miss one
call site, and the module still imports — the NameError only fires on the one
route that used it.  Consolidating the shared helpers into `services/format.py`
and friends did exactly that (a dropped `from urllib.parse import quote`), and
none of the behaviour gates noticed; this one does.

The check is `pyflakes` (the `dev` extra), which reports undefined names,
unused imports and unused locals without executing anything.  Two deliberate,
documented exceptions are filtered out rather than silenced in the source:

* ``extensions/database.py`` imports ``models`` purely for its side effect —
  importing the package is what registers every table on ``Base.metadata``.
* ``routes/pypi.py`` keeps ``FormatQuery`` in its signature for flask-pydantic
  even when a checker thinks the name is only used in an annotation.
"""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

#: Directories that hold the application.  ``scripts/`` is included so a gate
#: cannot rot either.
TARGETS = (
    "app.py", "cli.py", "errors.py", "schemas.py",
    "auth", "config", "extensions", "index", "models",
    "openapi", "routes", "services", "scripts",
)

#: ``<path>:<line>:<column>: message`` lines that are correct by design.
ALLOWED = (
    re.compile(r"^extensions/database\.py:\d+:\d+: 'models' imported but unused$"),
    re.compile(r"^routes/pypi\.py:\d+:\d+: 'FormatQuery' imported but unused$"),
)


def main() -> int:
    if importlib.util.find_spec("pyflakes") is None:
        print("⚠ pyflakes is not installed — skipping (pip install -e '.[dev]')")
        return 0

    proc = subprocess.run(
        [sys.executable, "-m", "pyflakes", *TARGETS],
        cwd=REPO_ROOT, capture_output=True, text=True, check=False,
    )

    problems = [
        line for line in proc.stdout.splitlines()
        if line.strip() and not any(pattern.match(line) for pattern in ALLOWED)
    ]

    if problems:
        print(f"❌ {len(problems)} lint problem(s)")
        for line in problems:
            print("   " + line)
        return 1

    print(f"✅ no undefined names or dead imports in {len(TARGETS)} target(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
