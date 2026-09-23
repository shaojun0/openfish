"""At-rest sealing keys — the Fernet secrets the platform stores data under.

All are high-entropy operator secrets (``secrets.token_urlsafe(48)``), all are
allowlist-excluded from every untrusted child, and — the part that matters —
they are **deliberately separate and never fall back to each other**:

* :attr:`KeysConfig.git_identity_key` seals the Forgejo token of a *user's* git
  identity in ``git_identities``.  It lives in the backend only; the runner
  container must never receive it, because the runner executes repository-
  supplied code.
* :attr:`KeysConfig.runner_credential_key` seals a *repository's* runner token in
  ``repo_runners.credential_ciphertext``.  Backend and runner both hold it, so
  the runner can unseal exactly the credential of the repository it is working
  on.  A missing runner key fails closed: it never silently seals or opens with
  the user-identity master key.
* :attr:`KeysConfig.model_route_key` seals an *upstream model key* in
  ``model_routes.api_key``.  Backend-only: the route table is served to a
  downstream DSH (``GET /api/v1/models/resolved``) and to the ``/models`` panel,
  and neither has any business holding the key the rows are sealed under.

Keeping them in one small group is the point — a reader can see the whole
key surface, and the group documents the rule that the two must not be conflated.
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import SettingsConfigDict

from config.base import EnvSettings


class KeysConfig(EnvSettings):
    """The platform's at-rest encryption keys."""

    model_config = SettingsConfigDict()

    git_identity_key: str = Field(
        default="",
        description=(
            "Master key for ``git_identities`` — the Forgejo token minted for a "
            "user's git identity is encrypted under this. Any high-entropy "
            "secret works; a Fernet key is derived from it, so an operator "
            "cannot end up with a value the code silently refuses. Empty means "
            "the git-credential endpoint answers 503 rather than degrading to "
            "plaintext. Backend-only: never given to the runner."
        ),
    )
    runner_credential_key: str = Field(
        default="",
        description=(
            "Dedicated key for ``repo_runners`` — a repository's runner token is "
            "sealed under this and unsealed inside the runner container. It must "
            "be the same value on both sides. Empty makes writing a per-"
            "repository credential fail closed (the shared forgejo_runner_token "
            "is never substituted for it)."
        ),
    )
    model_route_key: str = Field(
        default="",
        description=(
            "Dedicated key for ``model_routes`` — every route's upstream API key "
            "is sealed under this before it is stored, so a database dump, a "
            "backup or a replicated volume no longer carries a live credential. "
            "Any high-entropy secret works; a Fernet key is derived from it. "
            "Empty fails closed: saving a route with a key is refused (never a "
            "plaintext fallback) and rows already sealed report "
            "``api_key_source: unreadable`` rather than borrowing another key."
        ),
    )


__all__ = ["KeysConfig"]
