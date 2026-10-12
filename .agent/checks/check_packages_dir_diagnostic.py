#!/usr/bin/env python
"""Gate: a broken `PACKAGES_DIR` mount is explained, not a bare Errno 17.

`PACKAGES_DIR` is a bind-mounted catalog root, and `docker/README.md` §4
re-points those with a symlink.  A link whose target is not there **inside the
container** — the disk was never mounted, or the link was written on the host
with an absolute target that does not survive the bind mount — used to stop the
worker with::

    FileExistsError: [Errno 17] File exists: '/app/share/python/packages_data'

a message that names neither the variable nor the cause.  `app.py` now walks
the path and says which segment is the obstacle, before gunicorn loads the app.

Three shapes have to be diagnosed, and this gate pins all three:

1. the link is the last segment of `PACKAGES_DIR`;
2. the link is an ancestor of it (the ordinary `ln -s` mistake, one level up);
3. the path exists as a regular file.

Each case runs `import app` in a subprocess with that `PACKAGES_DIR`; all three
fail before the extensions are initialised, so the gate touches no database and
no catalog.  The happy path needs no case here: every other start of the app
(and the mirror-catalog gate, which imports it) would go red on a false
positive.

Run from the repository root::

    python .agent/checks/check_packages_dir_diagnostic.py
    # dev box without a `python` on PATH:
    backend/.venv/bin/python .agent/checks/check_packages_dir_diagnostic.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = REPO_ROOT / "backend"
TIMEOUT_S = 120
TARGET = "/nonexistent-openfish-target"


def _import_app(packages_dir: str) -> tuple[int, str]:
    """Run `import app` with *packages_dir* configured; return (code, output)."""
    env = dict(os.environ)
    env["PACKAGES_DIR"] = packages_dir
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    proc = subprocess.run(
        [sys.executable, "-c", "import app"],
        cwd=str(BACKEND_DIR),
        env=env,
        capture_output=True,
        text=True,
        timeout=TIMEOUT_S,
    )
    return proc.returncode, proc.stdout + proc.stderr


def main() -> int:
    failures: list[str] = []
    with tempfile.TemporaryDirectory(prefix="openfish-packages-dir-") as tmp:
        root = Path(tmp)
        (root / "python" / "real").mkdir(parents=True)
        (root / "afile").write_text("not a directory\n")
        os.symlink(TARGET, root / "python" / "packages_data")
        os.symlink(TARGET, root / "broken-parent")

        cases = {
            "dangling last segment": root / "python" / "packages_data",
            "dangling ancestor": root / "broken-parent" / "packages",
            "regular file": root / "afile",
        }
        checked = 0
        for name, path in cases.items():
            checked += 1
            code, output = _import_app(str(path))
            last_line = output.strip().splitlines()[-1] if output.strip() else "(no output)"
            if code == 0:
                failures.append(f"{name}: the app started on a path that is not a directory")
                continue
            if "cannot use PACKAGES_DIR" not in output:
                failures.append(f"{name}: no PACKAGES_DIR diagnosis — {last_line}")
                continue
            if str(path) not in output:
                failures.append(f"{name}: the message does not name the configured path")
            if path.is_symlink() and TARGET not in output:
                failures.append(f"{name}: the message does not name the link's target")

    for line in failures:
        print(f"FAIL {line}")
    print(
        f"check_packages_dir_diagnostic: {checked} cases, {len(failures)} failure(s)",
        file=sys.stderr,
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
