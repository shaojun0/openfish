#!/usr/bin/env python
"""Gate: the git boundary stays where the design put it.

Run from the backend directory (``backend/``)::

    python scripts/check_git_boundary.py

Two boundaries are easy to erode one line at a time, and both were eroded once:

1. **The sandbox boundary.** The runner executes a cloned repository's own
   ``check_*.py`` and a headless review command driven by untrusted issue text.
   Neither may inherit the worker's environment (``SECRET_KEY``,
   ``FORGEJO_ADMIN_TOKEN``, ``GIT_IDENTITY_KEY`` …), and the agent runner's
   ``git`` subprocesses must not borrow the admin credential either.  The fix is
   ``services.sandbox_env`` plus ``FORGEJO_RUNNER_TOKEN``; this gate pins it.
2. **The git ingress.** Forgejo serves every route at ``/`` — ``ROOT_URL=…/git/``
   only affects generated links — so nginx must *strip* the ``/git/`` prefix.
   Without the trailing slash on ``proxy_pass`` every clone/push is a 404.

It is offline and reads files plus imports the pure helpers; it never starts a
container.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from services.gates import ENV_INHERIT, ENV_SANDBOX, SubprocessGateExecutor  # noqa: E402
from services.git_auth import (  # noqa: E402
    GIT_CREDENTIAL_HELPER,
    GIT_HOST_ENV,
    GIT_TOKEN_ENV,
    credential_args,
    git_host_of,
)
from services.sandbox_env import (  # noqa: E402
    ALLOWED_NAMES,
    ALLOWED_PREFIXES,
    PLATFORM_SECRETS,
    sandbox_env,
)

REPO_DIR = REPO_ROOT.parent
DOCKER_DIR = REPO_DIR / "docker"

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"✅ {label}")
        return
    message = f"{label}{(' — ' + detail) if detail else ''}"
    FAILURES.append(message)
    print(f"❌ {message}")


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


def _nginx_location(text: str, path: str) -> str:
    """The body of ``location <path> { … }``, found by brace depth."""
    lines = text.splitlines()
    head = f"location {path}"
    start = next((i for i, line in enumerate(lines) if line.strip().startswith(head)), None)
    if start is None:
        return ""
    depth = 0
    body: list[str] = []
    for line in lines[start:]:
        depth += line.count("{") - line.count("}")
        if len(body) or "{" in line:
            body.append(line)
        if depth <= 0 and len(body) > 1:
            break
    return "\n".join(body)


def check_sandbox_env() -> None:
    print()
    print("── sandbox_env allowlist " + "─" * 30)
    planted = {name: f"secret-{name.lower()}" for name in PLATFORM_SECRETS}
    planted.update({"PATH": "/usr/bin", "HOME": "/home/runner", "LC_ALL": "C.UTF-8"})
    filtered = sandbox_env(source=planted)
    leaked = sorted(set(filtered) & set(PLATFORM_SECRETS))
    check("no platform secret survives the allowlist", not leaked, f"leaked={leaked}")
    check("PATH survives the allowlist", filtered.get("PATH") == "/usr/bin")
    check("LC_ prefix survives", filtered.get("LC_ALL") == "C.UTF-8")
    check(
        "no allowed prefix can smuggle a platform secret",
        not any(name.startswith(ALLOWED_PREFIXES) for name in PLATFORM_SECRETS),
    )
    check(
        "the allowlist never names a platform secret directly",
        not (set(PLATFORM_SECRETS) & ALLOWED_NAMES),
        f"overlap={sorted(set(PLATFORM_SECRETS) & ALLOWED_NAMES)}",
    )
    granted = sandbox_env(source=planted, extra={"OPENFISH_MODEL_KEY": "model-key"})
    check("an explicit extra is still granted",
          granted.get("OPENFISH_MODEL_KEY") == "model-key")


def check_executor_default() -> None:
    print()
    print("── gate executor environment policy " + "─" * 22)
    default = SubprocessGateExecutor()
    inherited = SubprocessGateExecutor(env_mode=ENV_INHERIT)
    check("the executor defaults to the sandbox policy",
          getattr(default, "_env_mode", None) == ENV_SANDBOX)
    check("inherit is an explicit opt-in",
          getattr(inherited, "_env_mode", None) == ENV_INHERIT)


def check_agent_child_envs() -> None:
    print()
    print("── agent path child environments " + "─" * 25)
    runner = (REPO_ROOT / "services" / "agent_runner.py").read_text(encoding="utf-8")
    worker = (REPO_ROOT / "services" / "agent_worker.py").read_text(encoding="utf-8")
    gates = (REPO_ROOT / "services" / "gates.py").read_text(encoding="utf-8")

    check("agent_runner builds git envs through sandbox_env", "sandbox_env(" in runner)
    check("agent_worker builds the review env through sandbox_env", "sandbox_env(" in worker)
    check("the executor builds sandbox envs through sandbox_env", "sandbox_env(" in gates)
    check("no code path reads os.environ wholesale for a child",
          "child = dict(os.environ)\n" not in gates
          and "child = dict(os.environ) if self._env_mode == ENV_INHERIT else sandbox_env()" in gates)
    check("the runner's git env never includes the model credential",
          "env.update(self._model_env)" not in runner)
    check("the runner gets a dedicated git token",
          "git_token" in runner and "credential_args" in runner)
    check("the review command authenticates with FORGEJO_RUNNER_TOKEN",
          "ENV_RUNNER_TOKEN" in worker and "runner_token" in worker)


def check_git_auth_single_source() -> None:
    print()
    print("── one git credential implementation " + "─" * 23)
    auth = (REPO_ROOT / "services" / "git_auth.py").read_text(encoding="utf-8")
    imports = (REPO_ROOT / "services" / "repo_import.py").read_text(encoding="utf-8")
    check("the mirror reuses the shared credential helper",
          "from services.git_auth import" in imports)
    check("the mirror no longer defines its own helper snippet",
          "echo username=x-access-token" not in imports)
    check("the helper source interpolates the env name, not the token",
          "{GIT_TOKEN_ENV}" in auth)
    check("the runtime snippet reads the token from the environment",
          "${" + GIT_TOKEN_ENV + "}" in GIT_CREDENTIAL_HELPER)
    check("the snippet interpolated into git argv carries no token value",
          "OPENFISH" in GIT_CREDENTIAL_HELPER)
    check("the helper answers only the `get` operation",
          '[ "$1" = get ]' in GIT_CREDENTIAL_HELPER)
    check("the helper pins the host from the environment",
          "${" + GIT_HOST_ENV + "}" in GIT_CREDENTIAL_HELPER)
    check("credential_args resets the configured helper list first",
          credential_args()[:2] == ["-c", "credential.helper="])
    check("git_host_of strips a default port but keeps a real one",
          git_host_of("https://h:443/x") == "h" and git_host_of("http://h:3000/x") == "h:3000")


def check_nginx_git_prefix() -> None:
    print()
    print("── nginx /git/ prefix " + "─" * 34)
    conf = DOCKER_DIR / "nginx" / "nginx.conf"
    check("nginx.conf exists", conf.is_file())
    if not conf.is_file():
        return
    block = _nginx_location(conf.read_text(encoding="utf-8"), "/git/")
    check("/git/ has a dedicated location block", bool(block))
    check(
        "/git/ strips the prefix (proxy_pass ends in a slash)",
        "proxy_pass http://openfish_forgejo/;" in block,
        block.replace("\n", " ")[:160],
    )
    check("the verbatim (404) proxy_pass is gone from /git/",
          "proxy_pass http://openfish_forgejo;" not in block)


def check_compose_runner_env() -> None:
    print()
    print("── compose runner environment " + "─" * 28)
    compose_path = DOCKER_DIR / "docker-compose.yml"
    check("docker-compose.yml exists", compose_path.is_file())
    if not compose_path.is_file():
        return
    text = compose_path.read_text(encoding="utf-8")
    runner_env = _yaml_anchor(text, "x-runner-env: &runner-env")
    check("x-runner-env anchor exists", bool(runner_env))
    # Comments in the anchor deliberately *name* the secrets they exclude, so the
    # scan is over the effective (non-comment) lines only.
    effective = "\n".join(
        line for line in runner_env.splitlines() if not line.strip().startswith("#")
    )
    for secret in ("SECRET_KEY", "FORGEJO_ADMIN_TOKEN", "GIT_IDENTITY_KEY",
                   "OAUTH2_CLIENT_SECRET", "DOCKER_UPSTREAM_PASSWORD"):
        check(f"runner env does not carry {secret}", secret not in effective)
    check("runner env carries the dedicated runner token",
          "FORGEJO_RUNNER_TOKEN" in effective)
    check("runner env still reaches the queue database",
          "API_KEYS_FILE" in effective or "DATABASE_URL" in effective)

    runner_service = _yaml_service(text, "runner")
    check("the runner service uses the sandbox env anchor",
          "<<: *runner-env" in runner_service,
          "runner service does not inherit *runner-env")
    check("the runner service no longer inherits x-backend-env",
          "<<: *backend-env" not in runner_service)
    check("per-repo pr_policy is not shadowed by a compose default",
          'AGENT_PR_POLICY: "${AGENT_PR_POLICY:-}"' in runner_env,
          "compose must leave AGENT_PR_POLICY empty so the policy file decides")
    check("per-repo auto_fix is not shadowed by a compose default",
          'AGENT_AUTO_FIX: "${AGENT_AUTO_FIX:-}"' in runner_env)


def check_no_admin_token_on_agent_path() -> None:
    print()
    print("── no admin authority on the agent path " + "─" * 20)
    forbidden = "FORGEJO_ADMIN_TOKEN"
    for module in ("agent_runner.py", "agent_worker.py"):
        source = (REPO_ROOT / "services" / module).read_text(encoding="utf-8")
        # AST, not substring: both modules *document* that they avoid the admin
        # token, and a docstring or comment is not a read.
        reads = [
            node
            for node in ast.walk(ast.parse(source))
            if (isinstance(node, ast.Constant) and node.value == forbidden)
            or (isinstance(node, ast.Name) and node.id == forbidden)
            or (isinstance(node, ast.Attribute) and node.attr == forbidden)
        ]
        check(f"{module} does not read FORGEJO_ADMIN_TOKEN", not reads,
              f"{len(reads)} reference(s)")


def check_publish_cannot_be_hijacked() -> None:
    print()
    print("── the checkout cannot hijack commit/push " + "─" * 11)
    runner = (REPO_ROOT / "services" / "agent_runner.py").read_text(encoding="utf-8")
    check("the fix commit passes --no-verify", '"--no-verify"' in runner)
    check("one hooks-off helper serves both commit and push",
          runner.count("*self._no_hooks_args(root)") >= 2,
          "a publish step grew without core.hooksPath")
    check("the hooks-off directory is created fresh, not at a predictable path",
          'mkdtemp(prefix="openfish-hooks-"' in runner,
          "a fixed path inside the writable checkout can be symlinked to .git/hooks")
    check("push targets the pinned repo_url, never the mutable origin",
          '"push", "--quiet", target' in runner
          and '"push", "--quiet", "origin"' not in runner)
    check("push refuses a rewritten .git/config",
          "_assert_config_untouched" in runner)
    check("the pristine config is digested right after clone",
          "self._config_digest = self._config_hash(target)" in runner)
    check("the runner redacts the adapter's git token",
          "_adapter_secrets" in runner and "def secrets" in runner)


def main() -> int:
    print("── git boundary gate " + "─" * 40)
    check_sandbox_env()
    check_executor_default()
    check_agent_child_envs()
    check_git_auth_single_source()
    check_nginx_git_prefix()
    check_compose_runner_env()
    check_no_admin_token_on_agent_path()
    check_publish_cannot_be_hijacked()

    print()
    if FAILURES:
        print(f"❌ {len(FAILURES)} git-boundary check(s) failed")
        for failure in FAILURES:
            print(f"   - {failure}")
        return 1
    print("✅ git boundary check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
