"""The one place that builds git credentials for a platform-owned clone/push.

Both the read-only mirror (``services.repo_import.GitCommitReader``) and the
agent runner (``services.agent_runner.SubprocessRunnerAdapter``) authenticate to
Forgejo over HTTP Basic, and neither may put the token in the URL, in argv, in a
log line or in ``.git/config``.  The mechanism is therefore shared here: the
token travels in an environment variable, and an inline ``credential.helper``
(there is no secret in the snippet itself) prints it when git asks.

The env name is deliberately a *separate* variable from any platform-wide key so
a caller can hand git a narrow, revocable token instead of the admin credential.
"""

from __future__ import annotations

#: Environment variable the inline helper reads the token from.
GIT_TOKEN_ENV = "OPENFISH_GIT_TOKEN"

#: ``git -c`` snippet: the username is a fixed placeholder (Forgejo keys on the
#: token) and the password is read from the environment at request time.  The
#: token itself is never interpolated into this string.
GIT_CREDENTIAL_HELPER = (
    '!f() { echo username=x-access-token; '
    f'echo "password=${{{GIT_TOKEN_ENV}}}"; }}; f'
)


def credential_args() -> list[str]:
    """``-c credential.helper=…`` for git, carrying no secret itself."""
    return ["-c", f"credential.helper={GIT_CREDENTIAL_HELPER}"]


def git_env(token: str) -> dict[str, str]:
    """The environment that carries *token* to :func:`credential_args`."""
    return {GIT_TOKEN_ENV: token} if token else {}


__all__ = ["GIT_CREDENTIAL_HELPER", "GIT_TOKEN_ENV", "credential_args", "git_env"]
