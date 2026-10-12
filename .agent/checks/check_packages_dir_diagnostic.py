#!/usr/bin/env python
"""Gate: a broken `PACKAGES_DIR` mount is repaired or explained, never Errno 17.

`PACKAGES_DIR` is a bind-mounted catalog root, and `docker/README.md` §4
re-points those with a symlink.  The bind mount carries the *directory*, never
the host's path space, so a link inside it is resolved by the container.  Used
to stop the worker with::

    FileExistsError: [Errno 17] File exists: '/app/share/python/packages_data'

a message that names neither the variable nor the cause.  `app.py` now decides
by where the link points, and this gate pins both halves.

**Repaired** — a relative target that is missing, on the link's own filesystem:
creating it is what `makedirs` does for any other missing path, and the
directory stays inside the mounted tree, so the app boots.

**Refused** — everything else, with the variable, the link and its target named:

1. an absolute target (it names a host disk the container cannot see, so
   creating it would put the packages in the container's writable layer);
2. a relative target that climbs onto a different filesystem (not ours to
   create);
3. a regular file where the directory should be.

Case 2 needs two devices, so the gate compares `/dev/shm` with the temp
directory and skips it — saying why — when they share one.

Every case runs `import app` in a subprocess; the refused ones fail before the
extensions are initialised, and the repaired one is pointed at the temp tree, so
the gate touches no database and no catalog.

Run from the repository root::

    python .agent/checks/check_packages_dir_diagnostic.py
    # dev box without a `python` on PATH:
    backend/.venv/bin/python .agent/checks/check_packages_dir_diagnostic.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = REPO_ROOT / "backend"
TIMEOUT_S = 120
TARGET = "/nonexistent-openfish-target"
OTHER_FS = Path("/dev/shm")
OTHER_FS_PROBE = OTHER_FS / "openfish-packages-dir-probe"


def _import_app(overrides: dict[str, str]) -> tuple[int, str]:
    """Run `import app` with *overrides* in its environment; return (code, out)."""
    env = dict(os.environ)
    env.update(overrides)
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


def _state_env(root: Path) -> dict[str, str]:
    """Keep the repaired run's writes inside *root* instead of the checkout."""
    state, cache, data = root / "state", root / "cache", root / "data"
    return {
        "PYTHON_BUILDS_DIR": str(state / "python-builds"),
        "NODE_BUILDS_DIR": str(state / "node-builds"),
        "TOOLS_DIR": str(state / "tools"),
        "DOCS_DIR": str(state / "docs"),
        "NPM_DIR": str(state / "npm"),
        "DOCKER_DIR": str(state / "docker-images"),
        "DEBIAN_DIR": str(state / "debian"),
        "NPM_CACHE_DIR": str(cache / "npm"),
        "DOCKER_CACHE_DIR": str(cache / "docker"),
        "DEBIAN_CACHE_DIR": str(cache / "debian"),
        "API_KEYS_FILE": str(data / "cpypiserver.db"),
        "DEVICE_CODES_FILE": str(data / "device_codes.json"),
    }


def main() -> int:
    failures: list[str] = []
    checked = 0
    with tempfile.TemporaryDirectory(prefix="openfish-packages-dir-") as tmp:
        root = Path(tmp)
        (root / "python" / "real").mkdir(parents=True)
        (root / "afile").write_text("not a directory\n")
        os.symlink(TARGET, root / "python" / "packages_data")
        os.symlink(TARGET, root / "broken-parent")
        os.symlink("../packages", root / "python" / "relative")

        # ── refused: name the variable, the link and where it points
        refused: list[tuple[str, Path, list[str]]] = [
            ("absolute target", root / "python" / "packages_data", [TARGET]),
            ("dangling ancestor", root / "broken-parent" / "packages", [TARGET]),
            ("regular file", root / "afile", []),
        ]
        if os.stat(root).st_dev != os.stat(OTHER_FS).st_dev:
            OTHER_FS_PROBE.mkdir(exist_ok=True)
            climbing = os.path.relpath(OTHER_FS_PROBE / "packages", root / "python")
            os.symlink(climbing, root / "python" / "crossfs")
            refused.append(
                ("different filesystem", root / "python" / "crossfs", [str(OTHER_FS_PROBE / "packages")])
            )
        else:
            print(
                f"note: {OTHER_FS} shares a device with {root}; crossing-filesystem case skipped",
                file=sys.stderr,
            )

        for name, path, fragments in refused:
            checked += 1
            code, output = _import_app({"PACKAGES_DIR": str(path)})
            tail = output.strip().splitlines()[-1] if output.strip() else "(no output)"
            if code == 0:
                failures.append(f"{name}: the app started on a path that is not a directory")
                continue
            if "cannot use PACKAGES_DIR" not in output:
                failures.append(f"{name}: no PACKAGES_DIR diagnosis — {tail}")
                continue
            for fragment in [str(path), *fragments]:
                if fragment not in output:
                    failures.append(f"{name}: the message does not mention {fragment!r}")

        # ── repaired: the relative target is created and the app runs on
        checked += 1
        link = root / "python" / "relative"
        created = root / "packages"
        code, output = _import_app({"PACKAGES_DIR": str(link), **_state_env(root)})
        tail = output.strip().splitlines()[-1] if output.strip() else "(no output)"
        if "cannot use PACKAGES_DIR" in output:
            failures.append(f"relative target: still refused — {tail}")
        if not created.is_dir():
            failures.append(f"relative target: {created} was not created (exit {code}) — {tail}")

    shutil.rmtree(OTHER_FS_PROBE, ignore_errors=True)
    for line in failures:
        print(f"FAIL {line}")
    print(
        f"check_packages_dir_diagnostic: {checked} cases, {len(failures)} failure(s)",
        file=sys.stderr,
    )
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
