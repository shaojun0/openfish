"""The one place that builds git credentials for a platform-owned clone/push.

Both the read-only mirror (``services.repo_import.GitCommitReader``) and the
agent runner (``services.agent_runner.SubprocessRunnerAdapter``) authenticate to
Forgejo over HTTP Basic, and neither may put the token in the URL, in argv, in a
log line or in ``.git/config``.  The mechanism is therefore shared here: the
token travels in an environment variable, and an inline ``credential.helper``
(there is no secret in the snippet itself) prints it when git asks.

The helper is deliberately narrow:

* it answers only the ``get`` operation — git hands the plaintext credential to
  every configured helper on ``store``/``erase``, so a helper that answers those
  is a credential sink;
* it answers only for the one host the caller pinned (``GIT_HOST_ENV``), so a
  checkout that rewrites its remote cannot make git hand the token elsewhere;
* :func:`credential_args` resets the helper list first, so a helper planted in
  the checkout's ``.git/config`` is never consulted at all.
"""

from __future__ import annotations

from urllib.parse import urlsplit

#: Environment variable the inline helper reads the token from.
GIT_TOKEN_ENV = "OPENFISH_GIT_TOKEN"

#: Environment variable naming the only host the helper will answer for.
GIT_HOST_ENV = "OPENFISH_GIT_HOST"

#: ``git -c`` snippet: the username is a fixed placeholder (Forgejo keys on the
#: token) and the password is read from the environment at request time.  The
#: token itself is never interpolated into this string.  ``host`` is read from
#: the request git pipes in; a default port is stripped so ``host:443`` and
#: ``host`` compare equal.
GIT_CREDENTIAL_HELPER = (
    '!f() { [ "$1" = get ] || exit 0; host=;'
    " while IFS='=' read -r key value; do"
    ' [ -z "$key" ] && break;'
    ' [ "$key" = host ] && host=$value;'
    " done;"
    " host=${host%:443}; host=${host%:80};"
    f' [ -n "$host" ] && [ "$host" = "${{{GIT_HOST_ENV}}}" ] || exit 0;'
    " echo username=x-access-token;"
    f' echo "password=${{{GIT_TOKEN_ENV}}}";'
    " }; f"
)


def git_host_of(url: str) -> str:
    """The ``host`` git presents for *url*, with a default port stripped.

    Matches the normalization the helper applies, so ``https://h:443/x`` and
    ``https://h/x`` both pin ``h``.
    """
    try:
        netloc = urlsplit(str(url or "")).netloc
    except ValueError:
        return ""
    host = netloc.rsplit("@", 1)[-1].lower()
    for suffix in (":443", ":80"):
        if host.endswith(suffix):
            host = host[: -len(suffix)]
    return host


def credential_args() -> list[str]:
    """``-c credential.helper=…`` for git, carrying no secret itself.

    The empty value first *resets* the helper list: git calls every configured
    helper with the plaintext credential on ``store``/``erase``, so without the
    reset a helper planted in the checkout's ``.git/config`` would be handed the
    token after a successful push.
    """
    return [
        "-c", "credential.helper=",
        "-c", f"credential.helper={GIT_CREDENTIAL_HELPER}",
    ]


def git_env(token: str, host: str = "") -> dict[str, str]:
    """The environment that carries *token* to :func:`credential_args`.

    *host* is the only host the helper answers for.  Without it the helper
    answers nothing — fail closed — rather than handing the token to whatever
    remote a modified checkout names.
    """
    env: dict[str, str] = {}
    if token:
        env[GIT_TOKEN_ENV] = token
    if host:
        env[GIT_HOST_ENV] = host
    return env


__all__ = [
    "GIT_CREDENTIAL_HELPER",
    "GIT_HOST_ENV",
    "GIT_TOKEN_ENV",
    "credential_args",
    "git_env",
    "git_host_of",
]
