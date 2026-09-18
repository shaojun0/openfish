#!/usr/bin/env python
"""Gate: untrusted code in the runner is dropped to a dedicated uid.

Run from the backend directory (``backend/``)::

    python scripts/check_sandbox_uid.py

The runner container holds two authorities at once: the trusted worker (which
owns the git credential) and untrusted repository code — the repo's own
``check_*.py`` driven by ``services.gates`` and the headless review command
driven by ``services.agent_worker.build_review_fn``.  Untrusted code used to
execute as the same uid, so a check could read ``/proc/<worker_pid>/environ`` and
walk off with ``FORGEJO_RUNNER_TOKEN``; the ``services.sandbox_env`` allowlist
never entered into it, because the child was already inside the trust boundary.

``services/sandbox_identity.py`` is the one place that answers "who may the
untrusted child be?".  This gate pins both halves of the fix:

* **behaviour** — the pure helpers are exercised with explicit env mappings and
  a *fake* capability probe, so no real privilege drop, container or network is
  involved; the sandbox ``HOME`` must be a worker-owned directory **outside** the
  checkout, and a symlinked work directory must fail closed rather than be
  relaxed through; the suite-protection ``chmod`` (``protect_suite`` /
  ``protect_paths``) runs as the *trusted* worker after untrusted code has had a
  writable checkout, so it too must skip symlinks instead of following them into
  a worker-owned file outside the checkout;
* **wiring** — the gate executor and the review runner must merge
  ``untrusted_popen_kwargs()`` and ``sandbox_env_overrides()``, the worker's git
  subprocesses must re-assert the post-clone ``.git/config`` (a repo-config
  ``core.fsmonitor`` / clean filter runs with the runner token otherwise),
  ``AgentRunner.run`` must prepare the work tree before the gates/review run, and
  the runner image plus compose anchor must create and configure the sandbox
  identity.

It also pins the second part of the slice: per-repo runner credentials are
sealed and opened with the dedicated ``RUNNER_CREDENTIAL_KEY``, which is
deliberately separate from the user-identity master key ``GIT_IDENTITY_KEY``.

Deployment check the offline gate cannot make: the worker must run as **root**
for Docker to grant it CapsEff ``CAP_SETUID``/``CAP_SETGID``; a non-root ``USER``
leaves those caps bounding-only and every task fails closed.  The gate asserts
the Dockerfile keeps ``USER root``; the real-container acceptance step is
documented in ``docker/runner/README.md`` §9.
"""

from __future__ import annotations

import ast
import os
import stat
import sys
import tempfile
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from services import sandbox_identity  # noqa: E402
from services.agent_runner import SubprocessRunnerAdapter  # noqa: E402
from services.sandbox_env import ALLOWED_NAMES, PLATFORM_SECRETS  # noqa: E402
from services.sandbox_identity import (  # noqa: E402
    SANDBOX_GID_ENV,
    SANDBOX_UID_ENV,
    SandboxIdentity,
    SandboxIdentityError,
    configured_identity,
    describe_identity,
    prepare_untrusted_workdir,
    privilege_drop_capable,
    sandbox_env_overrides,
    untrusted_popen_kwargs,
)

REPO_DIR = REPO_ROOT.parent
DOCKER_DIR = REPO_DIR / "docker"

#: The uid/gid the runner image must create and compose must declare.  Pinned
#: here once so a drift in either file is caught instead of silently failing
#: closed at runtime.
EXPECTED_SANDBOX_UID = 10002
EXPECTED_SANDBOX_GID = 10000

FAILURES: list[str] = []
CHECKS = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global CHECKS
    CHECKS += 1
    if condition:
        print(f"   ✅ {label}")
        return
    message = f"{label}{(' — ' + detail) if detail else ''}"
    FAILURES.append(message)
    print(f"   ❌ {message}")


def section(title: str) -> None:
    print()
    print(f"── {title} " + "─" * max(0, 60 - len(title)))


# ── YAML / Dockerfile helpers ────────────────────────────────────────

def _yaml_anchor(text: str, header: str) -> str:
    """The lines of a top-level YAML anchor block, without *header* itself."""
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if header in line), None)
    if start is None:
        return ""
    body: list[str] = []
    for line in lines[start + 1:]:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            body.append(line)
            continue
        if len(line) - len(line.lstrip()) == 0:
            break
        body.append(line)
    return "\n".join(body)


def _yaml_service(text: str, name: str) -> str:
    """The lines of a top-level ``  <name>:`` service block."""
    header = f"  {name}:"
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if line.rstrip() == header), None)
    if start is None:
        return ""
    body: list[str] = []
    for line in lines[start + 1:]:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            body.append(line)
            continue
        if len(line) - len(line.lstrip()) <= 2:
            break
        body.append(line)
    return "\n".join(body)


def _yaml_list(block: str, key: str) -> list[str]:
    """The ``- item`` entries of a list key inside a service block."""
    lines = block.splitlines()
    start = next((i for i, line in enumerate(lines) if line.strip() == f"{key}:"), None)
    if start is None:
        return []
    key_indent = len(lines[start]) - len(lines[start].lstrip())
    items: list[str] = []
    for line in lines[start + 1:]:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if len(line) - len(line.lstrip()) <= key_indent:
            break
        if stripped.startswith("- "):
            items.append(stripped[2:].strip())
    return items


def _yaml_scalar(block: str, key: str) -> str:
    """The scalar value of ``key: value`` in a YAML block, unquoted (or ``""``)."""
    for line in block.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or ":" not in stripped:
            continue
        name, _, value = stripped.partition(":")
        if name.strip() != key:
            continue
        value = value.split("#", 1)[0].strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        return value
    return ""


# ── source helpers ───────────────────────────────────────────────────

def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _function_source(path: Path, name: str, *, class_name: str | None = None) -> str:
    """The source segment of ``name`` (optionally a method of *class_name*)."""
    source = _source(path)
    tree = ast.parse(source)
    root: ast.AST = tree
    if class_name is not None:
        found = next(
            (node for node in ast.walk(tree)
             if isinstance(node, ast.ClassDef) and node.name == class_name),
            None,
        )
        if found is None:
            return ""
        root = found
    for node in ast.walk(root):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(source, node) or ""
    return ""


def _references(source: str, name: str) -> int:
    """How many AST nodes read/name *name* (docstrings do not count)."""
    return sum(
        1
        for node in ast.walk(ast.parse(source))
        if (isinstance(node, ast.Constant) and node.value == name)
        or (isinstance(node, ast.Name) and node.id == name)
        or (isinstance(node, ast.Attribute) and node.attr == name)
    )


# ── 1. identity resolution ───────────────────────────────────────────

def check_identity_resolution() -> None:
    section("1 · identity: the env pair is the switch")
    check("no env pair → no configured identity",
          configured_identity(env={}) is None)
    for label, env in (
        ("only the uid", {SANDBOX_UID_ENV: "10002"}),
        ("only the gid", {SANDBOX_GID_ENV: "10000"}),
        ("a non-numeric uid", {SANDBOX_UID_ENV: "sandbox", SANDBOX_GID_ENV: "10000"}),
        ("a non-numeric gid", {SANDBOX_UID_ENV: "10002", SANDBOX_GID_ENV: "sandbox"}),
        ("a non-positive uid", {SANDBOX_UID_ENV: "0", SANDBOX_GID_ENV: "10000"}),
    ):
        try:
            configured_identity(env=env)
        except SandboxIdentityError:
            check(f"{label} raises SandboxIdentityError", True)
        else:
            check(f"{label} raises SandboxIdentityError", False,
                  "a partial/unparseable identity was accepted")
    identity = configured_identity(env={SANDBOX_UID_ENV: "10002", SANDBOX_GID_ENV: "10000"})
    check("both set → the uid/gid pair",
          identity == SandboxIdentity(uid=10002, gid=10000), repr(identity))
    check("privilege_drop_capable answers with a bool",
          isinstance(privilege_drop_capable(env={}), bool))


# ── 2. spawn contract ────────────────────────────────────────────────

def check_spawn_contract() -> None:
    section("2 · spawn: no drop in dev, a hard drop when configured")
    with mock.patch.object(sandbox_identity, "privilege_drop_capable", return_value=False):
        check("unconfigured → no Popen kwargs (even without capabilities)",
              untrusted_popen_kwargs(env={}) == {})

    both = {SANDBOX_UID_ENV: "10002", SANDBOX_GID_ENV: "10000"}
    with mock.patch.object(sandbox_identity, "privilege_drop_capable", return_value=True):
        kwargs = untrusted_popen_kwargs(env=both)
    check("configured + capable → user/group/extra_groups/group umask",
          kwargs == {"user": 10002, "group": 10000,
                     "extra_groups": [10000], "umask": 0o002},
          repr(kwargs))

    with mock.patch.object(sandbox_identity, "privilege_drop_capable", return_value=False):
        try:
            untrusted_popen_kwargs(env=both)
        except SandboxIdentityError as exc:
            message = str(exc)
            check("configured + incapable → fail closed", True)
            check("the refusal names CAP_SETUID/CAP_SETGID and both env vars",
                  "CAP_SETUID" in message and "CAP_SETGID" in message
                  and SANDBOX_UID_ENV in message and SANDBOX_GID_ENV in message,
                  message)
        else:
            check("configured + incapable → fail closed", False,
                  "untrusted_popen_kwargs returned a spawn as the worker uid")

    # HOME is the only env override.  It must live *outside* the checkout, in a
    # worker-owned directory the untrusted uid cannot replace: an in-tree HOME
    # let a repository symlink it and turn the worker's next chmod into write
    # access to a worker-owned file.
    with tempfile.TemporaryDirectory(prefix="openfish-sandbox-home-") as tmp:
        checkout = Path(tmp) / "repo"
        checkout.mkdir()
        check("unconfigured → no env overrides", sandbox_env_overrides(env={}) == {})
        overrides = sandbox_env_overrides(env=both)
        raw_home = overrides.get("HOME")
        home = Path(raw_home) if raw_home else None
        outside = (
            home is not None
            and home.is_absolute()
            and checkout.resolve() != home.resolve()
            and checkout.resolve() not in home.resolve().parents
        )
        check("configured → HOME is outside the checkout", outside, repr(overrides))
        real_dir = home is not None and home.is_dir() and not home.is_symlink()
        check("the sandbox home exists as a real directory", real_dir,
              repr(raw_home))
        if home is not None and real_dir:
            mode = stat.S_IMODE(home.stat().st_mode)
            check("the sandbox home is group-writable and setgid",
                  mode & 0o2070 == 0o2070, oct(mode))
            check("the sandbox home is owned by the worker uid",
                  home.stat().st_uid == os.geteuid(), repr(raw_home))
        else:
            check("the sandbox home is group-writable and setgid", False, "no home")
            check("the sandbox home is owned by the worker uid", False, "no home")
        check("repeated calls reuse one HOME (never re-chmod a path)",
              sandbox_env_overrides(env=both).get("HOME") == raw_home,
              repr(raw_home))


# ── 2b. symlink refusal ──────────────────────────────────────────────

def check_symlink_refusal() -> None:
    section("2b · symlinks: a replaceable work dir is refused, not followed")
    both = {SANDBOX_UID_ENV: "10002", SANDBOX_GID_ENV: "10000"}
    with tempfile.TemporaryDirectory(prefix="openfish-sandbox-link-") as tmp:
        target = Path(tmp) / "target"
        target.mkdir()
        planted = target / "planted.txt"
        planted.write_text("x\n", encoding="utf-8")
        target.chmod(0o700)
        planted.chmod(0o600)
        link = Path(tmp) / "work"
        link.symlink_to(target, target_is_directory=True)
        try:
            prepare_untrusted_workdir(link, env=both)
        except SandboxIdentityError:
            check("a symlinked work directory fails closed", True)
        else:
            check("a symlinked work directory fails closed", False,
                  "the relax pass followed the symlink to its target")
        check("the symlink target was not chgrp/chmod'ed",
              stat.S_IMODE(target.stat().st_mode) == 0o700
              and stat.S_IMODE(planted.stat().st_mode) == 0o600,
              f"{oct(stat.S_IMODE(target.stat().st_mode))}/"
              f"{oct(stat.S_IMODE(planted.stat().st_mode))}")
        # Unconfigured is still a no-op, never a refusal.
        prepare_untrusted_workdir(link, env={})


# ── 2c. no worker chmod follows a repository symlink ─────────────────

def check_protect_no_follow() -> None:
    """The suite-protection chmod must never follow a repository symlink.

    ``SubprocessRunnerAdapter.protect_suite`` / ``protect_paths`` run as the
    **trusted** worker, twice: once read-only before the gates, and once
    widening (``readonly=False``) in ``AgentRunner.run``'s ``finally`` — *after*
    untrusted repository code has had a writable checkout.  A ``chmod`` that
    followed a link there made the worker hand the sandbox access to any
    worker-owned file it can reach (``.agent/checks -> /app/data`` turned the
    queue database from ``0600`` into ``0644``), which undoes the uid separation
    this whole slice exists for.  This scenario pins both the behaviour and the
    source, because "no chmod follows a link" is exactly the kind of guard a
    later refactor deletes while the existing gates stay green.
    """
    section("2c · suite protection: no worker chmod follows a repo symlink")
    adapter = SubprocessRunnerAdapter()
    with tempfile.TemporaryDirectory(prefix="openfish-protect-link-") as tmp:
        base = Path(tmp)
        workdir = base / "work"
        checkout = workdir / "repo"
        (checkout / ".agent").mkdir(parents=True)
        outside = base / "outside"
        outside.mkdir()
        worker_file = outside / "cpypiserver.db"
        worker_file.write_text("secret\n", encoding="utf-8")
        worker_file.chmod(0o600)
        # A repository can commit this link (or plant it while it runs as the
        # sandbox uid): `.agent/checks` itself is the symlink.
        (checkout / ".agent" / "checks").symlink_to(outside, target_is_directory=True)

        adapter.protect_suite(workdir, readonly=False)   # the widening pass
        check("a symlinked .agent/checks is never followed",
              stat.S_IMODE(worker_file.stat().st_mode) == 0o600,
              oct(stat.S_IMODE(worker_file.stat().st_mode)))

        # A real suite must still be protected: the guard must skip symlinks
        # without disabling the protection it exists for.
        (checkout / ".agent" / "checks").unlink()
        real_suite = checkout / ".agent" / "checks"
        real_suite.mkdir()
        real_script = real_suite / "check_x.py"
        real_script.write_text("#\n", encoding="utf-8")
        adapter.protect_suite(workdir, readonly=True)
        check("a real suite directory is still locked read-only",
              stat.S_IMODE(real_suite.stat().st_mode) == 0o555
              and stat.S_IMODE(real_script.stat().st_mode) == 0o444,
              f"{oct(stat.S_IMODE(real_suite.stat().st_mode))}/"
              f"{oct(stat.S_IMODE(real_script.stat().st_mode))}")
        adapter.protect_suite(workdir, readonly=False)
        check("the widening pass still restores a real suite",
              stat.S_IMODE(real_suite.stat().st_mode) == 0o755
              and stat.S_IMODE(real_script.stat().st_mode) == 0o644,
              f"{oct(stat.S_IMODE(real_suite.stat().st_mode))}/"
              f"{oct(stat.S_IMODE(real_script.stat().st_mode))}")

        # protect_paths takes repository-controlled relative paths.
        planted = checkout / "planted.py"
        planted.symlink_to(worker_file)
        secret = outside / "protected.py"
        secret.write_text("#\n", encoding="utf-8")
        secret.chmod(0o600)
        (checkout / "real.py").write_text("#\n", encoding="utf-8")
        adapter.protect_paths(workdir, ["planted.py"], readonly=True)
        check("protect_paths skips a symlinked final component",
              stat.S_IMODE(worker_file.stat().st_mode) == 0o600,
              oct(stat.S_IMODE(worker_file.stat().st_mode)))
        adapter.protect_paths(workdir, ["../protected.py", "real.py"], readonly=True)
        check("protect_paths refuses a path that escapes the checkout",
              stat.S_IMODE(secret.stat().st_mode) == 0o600,
              oct(stat.S_IMODE(secret.stat().st_mode)))
        check("protect_paths still locks a real repo file",
              stat.S_IMODE((checkout / "real.py").stat().st_mode) == 0o444,
              oct(stat.S_IMODE((checkout / "real.py").stat().st_mode)))

    runner = _source(REPO_ROOT / "services" / "agent_runner.py")
    suite_body = _function_source(
        REPO_ROOT / "services" / "agent_runner.py", "protect_suite",
        class_name="SubprocessRunnerAdapter",
    )
    paths_body = _function_source(
        REPO_ROOT / "services" / "agent_runner.py", "protect_paths",
        class_name="SubprocessRunnerAdapter",
    )
    check("protect_suite walks without following symlinked directories",
          "followlinks=False" in suite_body, suite_body[:120])
    check("protect_suite never chmods through rglob",
          ".rglob(" not in suite_body, suite_body[:120])
    check("protect_paths re-anchors the parent and lstats the final component",
          "realpath" in paths_body and "_no_follow_lstat" in paths_body,
          paths_body[:120])
    check("protect_paths never chmods through is_file/is_dir",
          ".is_file()" not in paths_body and ".is_dir()" not in paths_body,
          paths_body[:120])
    check("the no-follow guard uses lstat", "os.lstat" in runner)


# ── 3. work-tree contract ────────────────────────────────────────────

def check_work_tree() -> None:
    section("3 · work tree: group-shared, setgid, owner untouched")
    both = {SANDBOX_UID_ENV: "10002", SANDBOX_GID_ENV: "10000"}
    with tempfile.TemporaryDirectory(prefix="openfish-sandbox-tree-") as tmp:
        root = Path(tmp) / "tree"
        sub = root / "sub"
        sub.mkdir(parents=True)
        plain = sub / "plain.txt"
        plain.write_text("x\n", encoding="utf-8")
        tool = sub / "tool.sh"
        tool.write_text("#!/bin/sh\n", encoding="utf-8")
        for path in (root, sub):
            path.chmod(0o700)
        plain.chmod(0o600)
        tool.chmod(0o700)
        owner = root.stat().st_uid

        prepare_untrusted_workdir(root, env={})
        check("unconfigured → a no-op (modes untouched)",
              stat.S_IMODE(root.stat().st_mode) == 0o700
              and stat.S_IMODE(sub.stat().st_mode) == 0o700)

        prepare_untrusted_workdir(root, env=both)
        root_mode = stat.S_IMODE(root.stat().st_mode)
        sub_mode = stat.S_IMODE(sub.stat().st_mode)
        plain_mode = stat.S_IMODE(plain.stat().st_mode)
        tool_mode = stat.S_IMODE(tool.stat().st_mode)
        check("directories gain g+rwx and the setgid bit",
              root_mode & 0o2070 == 0o2070 and sub_mode & 0o2070 == 0o2070,
              f"{oct(root_mode)}/{oct(sub_mode)}")
        check("files gain g+rw", plain_mode & 0o060 == 0o060, oct(plain_mode))
        check("an already-executable file gains g+x", tool_mode & 0o010 == 0o010,
              oct(tool_mode))
        check("the owner is never changed (git stays unambiguous)",
              root.stat().st_uid == owner
              and sub.stat().st_uid == owner
              and plain.stat().st_uid == owner)

        prepare_untrusted_workdir(root, env=both)
        check("a second pass is idempotent",
              stat.S_IMODE(sub.stat().st_mode) == sub_mode
              and stat.S_IMODE(plain.stat().st_mode) == plain_mode)

    check("describe_identity reports dev mode",
          "no sandbox uid configured" in describe_identity(env={}))
    described = describe_identity(env=both)
    check("describe_identity names the configured uid/gid",
          "10002" in described and "10000" in described, described)


# ── 4. source wiring ─────────────────────────────────────────────────

def check_source_wiring() -> None:
    section("4 · wiring: only untrusted children drop")
    capture = _function_source(
        REPO_ROOT / "services" / "gates.py", "_capture",
        class_name="SubprocessGateExecutor",
    )
    check("the gate executor drops untrusted checks",
          "subprocess.Popen(" in capture and "**untrusted_popen_kwargs()" in capture,
          "SubprocessGateExecutor._capture does not merge the drop kwargs")
    check("the gate executor merges HOME after the allowlist",
          "child = self._child_env()" in capture
          and capture.find("child = self._child_env()")
          < capture.find("sandbox_env_overrides("),
          "sandbox_env_overrides() is not merged after _child_env()")

    review = _function_source(REPO_ROOT / "services" / "agent_worker.py", "build_review_fn")
    check("the review command drops to the sandbox uid",
          "subprocess.run(" in review and "**untrusted_popen_kwargs()" in review,
          "build_review_fn.review_fn never merges the drop kwargs")
    check("the review env override is merged after the allowlist",
          "sandbox_env(extra=" in review
          and review.find("sandbox_env(extra=") < review.find("sandbox_env_overrides("),
          "sandbox_env_overrides() is not merged after sandbox_env(extra=…)")

    git_run = _function_source(
        REPO_ROOT / "services" / "agent_runner.py", "_run",
        class_name="SubprocessRunnerAdapter",
    )
    check("the git subprocess keeps the worker uid (it owns the credential)",
          bool(git_run) and "untrusted_popen_kwargs" not in git_run,
          "SubprocessRunnerAdapter._run drops privileged git to the sandbox uid")

    run = _function_source(
        REPO_ROOT / "services" / "agent_runner.py", "run", class_name="AgentRunner",
    )
    prepared = run.find("prepare_untrusted_workdir(")
    check("AgentRunner.run prepares the work tree for the sandbox uid",
          prepared >= 0 and "describe_identity(" in run)
    check("the work tree is prepared before the gates run",
          prepared >= 0 and 0 <= prepared < run.find("run_gates("))
    check("the work tree is prepared before the review runs",
          prepared >= 0 and 0 <= prepared < run.find("adapter.review("))

    repo_runner = _source(REPO_ROOT / "services" / "repo_runner.py")
    check("repo_runner reads the dedicated RUNNER_CREDENTIAL_KEY",
          _references(repo_runner, "RUNNER_CREDENTIAL_KEY") > 0)
    check("repo_runner never reads the user-identity master key",
          _references(repo_runner, "GIT_IDENTITY_KEY") == 0,
          "repo_runner references GIT_IDENTITY_KEY")
    check("the runner cipher is built from the explicit key, not the env fallback",
          "TokenCipher(key=" in repo_runner and "TokenCipher(env=" not in repo_runner,
          "the runner credential cipher can fall back to GIT_IDENTITY_KEY")

    check("RUNNER_CREDENTIAL_KEY is a platform secret",
          "RUNNER_CREDENTIAL_KEY" in PLATFORM_SECRETS)
    check("RUNNER_CREDENTIAL_KEY is never in the untrusted allowlist",
          "RUNNER_CREDENTIAL_KEY" not in ALLOWED_NAMES)

    # The worker's own git commands run *after* untrusted code, inside a checkout
    # that code can rewrite; ``git status`` / ``git add`` execute repo-config
    # programs (core.fsmonitor, filter.<n>.clean) with the runner token in the
    # environment unless the config is re-asserted first.
    runner_source = _source(REPO_ROOT / "services" / "agent_runner.py")
    check("every post-untrusted git command re-asserts the post-clone config",
          runner_source.count("self._assert_config_untouched(root)") >= 3,
          "changed_paths/commit/push must all re-assert .git/config")
    check("git status disables a repo-config fsmonitor program",
          '"core.fsmonitor=false"' in runner_source)
    check("the hooks-off directory is outside the writable work tree",
          "dir=root.parent" not in runner_source)
    check("a stale in-tree sandbox HOME is a transient path",
          "SANDBOX_HOME_DIRNAME" in runner_source)


# ── 5. runner image / compose ────────────────────────────────────────

def check_runner_image() -> None:
    section("5 · runner image: sandbox user exists, worker keeps the caps")
    dockerfile_path = DOCKER_DIR / "runner" / "Dockerfile"
    check("docker/runner/Dockerfile exists", dockerfile_path.is_file())
    if not dockerfile_path.is_file():
        return
    dockerfile = _source(dockerfile_path)
    check("the image creates the shared sandbox group",
          "groupadd" in dockerfile and f"--gid {EXPECTED_SANDBOX_GID}" in dockerfile
          and "openfish" in dockerfile)
    check(f"the image creates the sandbox user (uid {EXPECTED_SANDBOX_UID})",
          f"--uid {EXPECTED_SANDBOX_UID}" in dockerfile and "useradd" in dockerfile)
    check("the sandbox user joins the shared gid",
          f"--gid {EXPECTED_SANDBOX_GID}" in dockerfile and "sandbox" in dockerfile)
    check("the worker is a member of the shared group (so chgrp to it works)",
          "usermod -aG openfish root" in dockerfile,
          "a root worker outside gid 10000 cannot chgrp the shared work tree")
    # Docker only grants effective capabilities to a root process: with a
    # non-root image USER the cap_add reaches the bounding set alone (CapEff=0),
    # so the drop fails closed and *every* task's gates go red.
    check("the worker runs as root so CAP_SETUID/CAP_SETGID stay effective",
          "USER root" in dockerfile and "USER runner" not in dockerfile,
          "a non-root USER makes compose's cap_add inert")
    check("no other USER directive can de-privilege the worker",
          all(not line.strip().startswith("USER ") or line.strip() == "USER root"
              for line in dockerfile.splitlines()))


def check_compose_runner() -> None:
    section("6 · compose: the sandbox identity and the runner key")
    compose_path = DOCKER_DIR / "docker-compose.yml"
    check("docker-compose.yml exists", compose_path.is_file())
    if not compose_path.is_file():
        return
    text = _source(compose_path)
    runner_env = _yaml_anchor(text, "x-runner-env: &runner-env")
    check("x-runner-env anchor exists", bool(runner_env))
    effective = "\n".join(
        line for line in runner_env.splitlines() if not line.strip().startswith("#")
    )
    check(f"runner env carries {SANDBOX_UID_ENV}", f"{SANDBOX_UID_ENV}:" in effective)
    check(f"runner env carries {SANDBOX_GID_ENV}", f"{SANDBOX_GID_ENV}:" in effective)
    check("runner env carries RUNNER_CREDENTIAL_KEY",
          "RUNNER_CREDENTIAL_KEY:" in effective)
    check("runner env does NOT carry the identity master key",
          "GIT_IDENTITY_KEY" not in effective,
          "GIT_IDENTITY_KEY must stay out of the runner container")
    # Presence is not enough: a typo'd uid the image never creates passes a
    # key-existence check and fails closed at runtime.
    check(f"runner env sets {SANDBOX_UID_ENV}={EXPECTED_SANDBOX_UID}",
          _yaml_scalar(runner_env, SANDBOX_UID_ENV) == str(EXPECTED_SANDBOX_UID),
          repr(_yaml_scalar(runner_env, SANDBOX_UID_ENV)))
    check(f"runner env sets {SANDBOX_GID_ENV}={EXPECTED_SANDBOX_GID}",
          _yaml_scalar(runner_env, SANDBOX_GID_ENV) == str(EXPECTED_SANDBOX_GID),
          repr(_yaml_scalar(runner_env, SANDBOX_GID_ENV)))

    # Both sides of the credential must be wired: the backend seals, the runner
    # opens.  The runner anchor alone cannot prove the seal side exists.
    backend_env = _yaml_anchor(text, "x-backend-env: &backend-env")
    check("x-backend-env anchor exists", bool(backend_env))
    check("the backend (seal side) carries RUNNER_CREDENTIAL_KEY",
          _yaml_scalar(backend_env, "RUNNER_CREDENTIAL_KEY") != "",
          "set_credential() would always refuse")

    env_example = DOCKER_DIR / ".env.example"
    example = _source(env_example) if env_example.is_file() else ""
    check("the env template assigns RUNNER_CREDENTIAL_KEY",
          "RUNNER_CREDENTIAL_KEY=" in example)
    check("the env template assigns GIT_IDENTITY_KEY",
          "GIT_IDENTITY_KEY=" in example)

    runner_service = _yaml_service(text, "runner")
    cap_drop = _yaml_list(runner_service, "cap_drop")
    cap_add = _yaml_list(runner_service, "cap_add")
    check("the runner service drops ALL capabilities", cap_drop == ["ALL"], repr(cap_drop))
    check("the runner adds back only the drop and DAC_OVERRIDE caps",
          set(cap_add) == {"SETUID", "SETGID", "DAC_OVERRIDE"}, repr(cap_add))
    check("the runner service uses the sandbox env anchor",
          "<<: *runner-env" in runner_service)


# ── main ─────────────────────────────────────────────────────────────

def main() -> int:
    print("── sandbox uid privilege-separation gate " + "─" * 17)
    print(f"   repo root: {REPO_ROOT}")
    for scenario in (
        check_identity_resolution,
        check_spawn_contract,
        check_symlink_refusal,
        check_protect_no_follow,
        check_work_tree,
        check_source_wiring,
        check_runner_image,
        check_compose_runner,
    ):
        try:
            scenario()
        except Exception as exc:  # noqa: BLE001 - a crashed scenario is a failure
            import traceback

            FAILURES.append(f"{scenario.__name__} raised {type(exc).__name__}: {exc}")
            print(f"   ❌ {scenario.__name__} raised {type(exc).__name__}: {exc}")
            traceback.print_exc()

    print()
    if FAILURES:
        print(f"❌ {len(FAILURES)}/{CHECKS} check(s) failed")
        for failure in FAILURES:
            print(f"   - {failure}")
        return 1
    print(f"✅ all {CHECKS} checks passed — sandbox identity resolves and fails closed, "
          "the spawn merges the drop and an out-of-tree HOME override, untrusted "
          "children (not git) drop, the work tree is group-shared without chown and "
          "refuses symlinked roots, post-untrusted git re-asserts .git/config, and the "
          "runner image (USER root) plus compose carry the identity and "
          "RUNNER_CREDENTIAL_KEY without GIT_IDENTITY_KEY")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
