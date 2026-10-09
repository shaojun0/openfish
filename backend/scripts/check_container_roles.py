#!/usr/bin/env python
"""Gate: one application image, plane selected by ``OPENFISH_ROLE``.

Run from the backend directory (``backend/``)::

    python scripts/check_container_roles.py

The platform used to build two images from the same ``backend/`` context —
``openfish-backend`` (Flask + gunicorn) and ``openfish-runner`` (git + the
agent-runtime queue worker) — which differed only in their apt packages and
their start command.  They are now **one** image and the plane is a runtime
choice:

* ``docker/docker-compose.yml`` builds every application-plane service from the
  single ``x-app-build`` anchor into the single ``openfish:latest`` tag;
* the two environment anchors select the plane per service
  (``x-backend-env`` → ``backend``, ``x-runner-env`` → ``runner``);
* ``backend/docker-entrypoint.sh`` is the one place that maps a role to a
  process, and it fails closed on an unknown role instead of quietly starting
  the wrong plane.

This gate is what keeps the second Dockerfile from creeping back: two images
would let the Python dependencies and the application code drift between the
API and the sandbox — a gate that passes locally and fails in the runner, or
the other way around.

It deliberately does **not** repeat the sandbox-identity assertions
(``check_sandbox_uid.py`` owns those, including the ``USER root`` the capability
drop needs) or the scale-out assertions (``check_runner_scaling.py``); it pins
only the one-image / one-dispatch invariant.
"""

from __future__ import annotations

import stat
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCKER_DIR = REPO_ROOT.parent / "docker"

#: The single application image.  Every service that builds from ``backend/``
#: must resolve to this tag and this Dockerfile.
IMAGE_TAG = "openfish:latest"
IMAGE_CONTEXT = "../backend"
IMAGE_DOCKERFILE = "Dockerfile"

#: Where the Dockerfile installs the dispatcher, and the repo source of it.
ENTRYPOINT_PATH = "/usr/local/bin/openfish-entrypoint"
ENTRYPOINT_SOURCE = REPO_ROOT / "docker-entrypoint.sh"

#: Service → the role its environment must select.
APP_SERVICES: dict[str, str] = {
    "backend": "backend",
    "runner": "runner",
    "backend-debug": "backend",
}

_problems: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    if ok:
        print(f"✅ {label}")
    else:
        _problems.append(label)
        print(f"❌ {label}" + (f" — {detail}" if detail else ""))


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def _compose() -> dict[str, Any]:
    try:
        import yaml
    except ImportError:  # pragma: no cover - pyyaml ships with the project
        check("pyyaml parses docker-compose.yml", False, "pyyaml is not installed")
        return {}
    text = _read(DOCKER_DIR / "docker-compose.yml")
    if not text:
        check("docker/docker-compose.yml exists", False, str(DOCKER_DIR))
        return {}
    document = yaml.safe_load(text)
    if not isinstance(document, dict):
        check("docker-compose.yml parses as a mapping", False)
        return {}
    return document


# ── 1. exactly one application image ─────────────────────────────────

def check_one_image(document: dict[str, Any]) -> None:
    print("\n── 1 · one application image ──────────────────────────────")
    dockerfile = REPO_ROOT / "Dockerfile"
    check("backend/Dockerfile is the application image", dockerfile.is_file())
    check("docker/runner/Dockerfile is gone",
          not (DOCKER_DIR / "runner" / "Dockerfile").exists(),
          "a second application Dockerfile reintroduces dependency drift")
    stray = sorted(str(path.relative_to(DOCKER_DIR)) for path in DOCKER_DIR.rglob("Dockerfile"))
    check("docker/ holds no Dockerfile at all", not stray, repr(stray))

    services = document.get("services") or {}
    for name in APP_SERVICES:
        service = services.get(name) or {}
        check(f"compose service {name} uses {IMAGE_TAG}",
              service.get("image") == IMAGE_TAG, repr(service.get("image")))
        build = service.get("build") or {}
        check(f"compose service {name} builds from {IMAGE_CONTEXT}",
              build.get("context") == IMAGE_CONTEXT, repr(build.get("context")))
        check(f"compose service {name} builds {IMAGE_DOCKERFILE}",
              build.get("dockerfile") == IMAGE_DOCKERFILE, repr(build.get("dockerfile")))

    # Nothing may build from backend/ under a different tag or file: that is the
    # shape a third image would take.
    for name, service in services.items():
        build = service.get("build") or {}
        if not isinstance(build, dict) or build.get("context") != IMAGE_CONTEXT:
            continue
        check(f"{name} does not fork the application image",
              service.get("image") == IMAGE_TAG and build.get("dockerfile") == IMAGE_DOCKERFILE,
              f"image={service.get('image')!r} build={build!r}")


# ── 2. the plane is chosen in the environment, per service ───────────

def check_role_selection(document: dict[str, Any]) -> None:
    print("\n── 2 · the plane is a per-service environment variable ────")
    anchors = {
        "x-backend-env": "backend",
        "x-runner-env": "runner",
    }
    services = document.get("services") or {}
    for anchor, role in anchors.items():
        block = document.get(anchor) or {}
        check(f"{anchor} sets OPENFISH_ROLE={role}",
              block.get("OPENFISH_ROLE") == role, repr(block.get("OPENFISH_ROLE")))
    for name, role in APP_SERVICES.items():
        environment = (services.get(name) or {}).get("environment") or {}
        check(f"{name} resolves to OPENFISH_ROLE={role}",
              environment.get("OPENFISH_ROLE") == role,
              repr(environment.get("OPENFISH_ROLE")))

    runner = services.get("runner") or {}
    check("runner does not declare an HTTP healthcheck",
          "healthcheck" not in runner,
          "the runner plane serves no port; the image probe is role-aware")


# ── 3. the dispatcher itself ─────────────────────────────────────────

def check_entrypoint() -> None:
    print("\n── 3 · docker-entrypoint.sh maps every role ────────────────")
    check("backend/docker-entrypoint.sh exists", ENTRYPOINT_SOURCE.is_file())
    if not ENTRYPOINT_SOURCE.is_file():
        return
    entrypoint = _read(ENTRYPOINT_SOURCE)
    mode = ENTRYPOINT_SOURCE.stat().st_mode
    check("the dispatcher is executable", bool(mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)),
          oct(stat.S_IMODE(mode)))
    check("it is a POSIX sh script", entrypoint.startswith("#!/bin/sh"))

    dockerfile = _read(REPO_ROOT / "Dockerfile")
    check("the image installs the dispatcher as its ENTRYPOINT",
          f'ENTRYPOINT ["{ENTRYPOINT_PATH}"]' in dockerfile)
    check("the image chmods the installed dispatcher",
          f"chmod 0755 {ENTRYPOINT_PATH}" in dockerfile)
    check("the image HEALTHCHECK goes through the dispatcher",
          f'"{ENTRYPOINT_PATH}", "healthcheck"' in dockerfile,
          "a bare curl HEALTHCHECK would report the runner permanently unhealthy")

    # The union of both planes' system packages must actually be installed;
    # dropping one makes a plane fail only at run time.
    for package in ("libmagic1", "curl", "git", "build-essential"):
        check(f"the image installs {package}", package in dockerfile)

    check("an unset OPENFISH_ROLE defaults to the backend plane",
          "${OPENFISH_ROLE:-backend}" in entrypoint)
    check("an explicit command still wins over the dispatch",
          'exec "$@"' in entrypoint,
          "docker run <cmd>, the debug profile and `worker --once` rely on it")

    # The dispatch block is the *last* `case "$(role)" in` — the healthcheck
    # subcommand has one of its own earlier in the file.
    dispatch = entrypoint.rsplit('case "$(role)" in', 1)[-1]
    check("the backend role starts gunicorn",
          "gunicorn" in dispatch and "app:app" in dispatch)
    check("the runner role starts the queue worker",
          "services.agent_queue" in dispatch and "worker" in dispatch and "--loop" in dispatch)
    check("an unknown role fails closed instead of starting a plane",
          "exit 64" in dispatch and "expected: backend | runner" in dispatch,
          "a typo must not silently become the backend")

    healthcheck = entrypoint.split('healthcheck" ]; then', 1)
    check("the healthcheck subcommand exists", len(healthcheck) == 2)
    if len(healthcheck) == 2:
        probe = healthcheck[1].split("\nfi\n", 1)[0]
        check("the backend probe is the HTTP /health endpoint",
              "curl" in probe and "/health" in probe)
        check("the runner probe does not require an HTTP port",
              "exit 0" in probe)


def main() -> int:
    document = _compose()
    check_one_image(document)
    check_role_selection(document)
    check_entrypoint()
    if _problems:
        print(f"\n❌ {len(_problems)} container-role problem(s)")
        return 1
    print("\n✅ one application image, plane selected by OPENFISH_ROLE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
