"""The environment untrusted code is allowed to inherit.

Two things the sandbox executes are *not* platform code: the repository's own
checks (``services.gates`` resolves ``check_*.py`` / ``npm test`` / ``make test``
from the checkout) and the headless review command driven by a model that reads
untrusted issue text.  Both used to be spawned with ``dict(os.environ)`` — which
in the runner container is ``x-backend-env`` from ``docker/docker-compose.yml``:
``SECRET_KEY``, ``FORGEJO_ADMIN_TOKEN``, ``GIT_IDENTITY_KEY``, the OAuth client
secret and every upstream password.  A cloned repository could therefore print
or POST the platform's master credentials without ever involving the model.

This module is the one place that decides what crosses that boundary, so the
answer cannot drift between the gate executor and the review runner.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

#: Names that carry no authority: process plumbing, locale, and the toolchain
#: knobs a foreign build legitimately reads.  Deliberately a **allowlist** — a
#: deny-list of "secret-looking" names would silently pass the next credential
#: a deployment invents.
ALLOWED_NAMES: frozenset[str] = frozenset({
    # process / shell plumbing
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "TERM", "TZ",
    "TMPDIR", "TEMP", "TMP", "HOSTNAME", "PWD",
    # locale / presentation
    "LANG", "LANGUAGE", "LC_ALL", "TERM_PROGRAM",
    "NO_COLOR", "FORCE_COLOR", "CLICOLOR", "CI",
    # Python
    "PYTHONPATH", "PYTHONHOME", "PYTHONUNBUFFERED", "PYTHONDONTWRITEBYTECODE",
    "PYTHONIOENCODING", "PYTHONUTF8", "PYTHONHASHSEED",
    "VIRTUAL_ENV", "PIP_INDEX_URL", "PIP_EXTRA_INDEX_URL", "PIP_TRUSTED_HOST",
    "PIP_DISABLE_PIP_VERSION_CHECK",
    # Node / npm
    "NODE_PATH", "NODE_OPTIONS",
    # JVM / Go / Rust / C toolchain
    "JAVA_HOME", "MAVEN_OPTS", "GRADLE_USER_HOME",
    "GOROOT", "GOPATH", "GOFLAGS", "GOPROXY",
    "CARGO_HOME", "RUSTUP_HOME",
    "CC", "CXX", "CFLAGS", "CXXFLAGS", "LDFLAGS",
    "MAKE", "MAKEFLAGS", "MAKELEVEL",
    # TLS trust material (a CA bundle is public, not a credential)
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
    "GIT_SSL_CAINFO",
    # git client behaviour (never a token: the credential helper reads its own env)
    "GIT_TERMINAL_PROMPT", "GIT_CONFIG_NOSYSTEM", "GIT_SSH_COMMAND",
})

#: Prefixes forwarded wholesale: tool-specific tuning that carries no authority.
ALLOWED_PREFIXES: tuple[str, ...] = (
    "LC_", "XDG_", "npm_config_", "NPM_CONFIG_", "UV_",
)

#: Names the platform must never hand to untrusted code, kept here so a reader
#: can see the boundary stated positively as well.  This is documentation: none
#: of them may appear in an allowlist that a gate executor would pass.
PLATFORM_SECRETS: tuple[str, ...] = (
    "SECRET_KEY",
    "FORGEJO_ADMIN_TOKEN",
    "FORGEJO_RUNNER_TOKEN",
    "FORGEJO_WEBHOOK_SECRET",
    "GIT_IDENTITY_KEY",
    "RUNNER_CREDENTIAL_KEY",
    "DATABASE_URL",
    "OAUTH2_CLIENT_SECRET",
    "OAUTH2_CA_BUNDLE",
    "NPM_UPSTREAM_TOKEN",
    "DOCKER_UPSTREAM_PASSWORD",
    "IMPORT_SOURCE_TOKEN",
)


def allowed(name: str) -> bool:
    """Whether *name* may cross the untrusted boundary."""
    return name in ALLOWED_NAMES or name.startswith(ALLOWED_PREFIXES)


def sandbox_env(
    *,
    source: Mapping[str, str] | None = None,
    extra: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """The environment for a subprocess that runs untrusted code.

    *source* defaults to ``os.environ``; *extra* is merged **after** filtering
    and is the caller's explicit grant (a model key for the review command, a
    scoped git token for one clone).  Callers that need the worker's full
    environment (the project's own in-repo gate runs) must opt out in
    :class:`services.gates.SubprocessGateExecutor` instead of widening this.
    """
    origin = os.environ if source is None else source
    child = {str(name): str(value) for name, value in origin.items() if allowed(str(name))}
    if extra:
        child.update({str(name): str(value) for name, value in extra.items()})
    return child


__all__ = [
    "ALLOWED_NAMES",
    "ALLOWED_PREFIXES",
    "PLATFORM_SECRETS",
    "allowed",
    "sandbox_env",
]
