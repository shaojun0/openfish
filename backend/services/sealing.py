"""At-rest sealing — one Fernet derivation, one envelope, for every sealed column.

Three tables store a credential that must not be legible in a database dump, a
backup, or a replicated volume:

* ``git_identities.token_ciphertext`` — a user's Forgejo token;
* ``repo_runners.credential_ciphertext`` — a repository's runner token;
* ``model_routes.api_key`` — an upstream model endpoint's key.

Each has its own master key in :class:`config.keys.KeysConfig`; that separation
is deliberate and these must never fall back to each other.  But *"which bytes
are the key, and how is the cipher built from them?"* is one concern, so it is
answered once, here, and the three call sites differ only in which secret they
hand it.

The derivation is the part worth stating.  A master key may be any high-entropy
secret (``secrets.token_urlsafe(48)``), and a valid Fernet key is *derived* from
it as ``urlsafe_b64encode(sha256(secret))``.  Deriving rather than demanding
exactly 32 url-safe bytes is deliberate: ``Fernet.generate_key()`` and a random
token both work, so an operator cannot end up with a key the code silently
refuses.

:class:`SecretSealer` additionally tags what it writes with a **prefix**, which
is what lets a column hold a mixture during a migration: a value carrying the
prefix is an envelope of *this version*, anything else is a legacy plaintext
value that predates sealing.  Without the prefix the two are
indistinguishable — a Fernet token is only recognisable by trying to open it —
and "is this row still plaintext?" could not be answered without the key.
"""

from __future__ import annotations

import base64

from cryptography.fernet import Fernet

from services.digest import sha256_text


class SealingKeyMissing(RuntimeError):
    """A sealed column's master key is not configured.

    A *configuration* error, never a reason to degrade to plaintext: the caller
    either refuses the write or reports the row as unreadable.
    """


def derive_fernet(secret: str) -> Fernet:
    """The Fernet cipher *secret* names — see the module docstring.

    Raises :class:`SealingKeyMissing` for a blank secret rather than deriving a
    perfectly usable key from the hash of the empty string.
    """
    raw = (secret or "").strip()
    if not raw:
        raise SealingKeyMissing("no at-rest sealing key configured")
    return Fernet(base64.urlsafe_b64encode(bytes.fromhex(sha256_text(raw))))


class SecretSealer:
    """Seals one column's secrets under one master key, with a version prefix.

    Built per use rather than cached: construction is a SHA-256 of a short
    string plus a cipher object, and reading the key from ``settings`` at the
    call site is what keeps a test (or the CLI) from having to re-point a
    module-level singleton.
    """

    def __init__(self, secret: str, *, prefix: str, help_text: str = "") -> None:
        self._secret = secret or ""
        self._prefix = prefix
        self._help = help_text

    @property
    def available(self) -> bool:
        """Whether a master key is configured — the write path's gate."""
        return bool(self._secret.strip())

    @property
    def help(self) -> str:
        """What to tell an operator when :attr:`available` is false."""
        return self._help

    def _cipher(self) -> Fernet:
        try:
            return derive_fernet(self._secret)
        except SealingKeyMissing as exc:
            raise SealingKeyMissing(self._help or str(exc)) from exc

    def is_sealed(self, value: str) -> bool:
        """Whether *value* is an envelope of this version.

        A pure string test, so a caller can ask it **without** a master key —
        which is what lets the read path tell "not sealed" (legacy plaintext)
        apart from "sealed but I cannot open it" (missing/wrong key).
        """
        return str(value or "").startswith(self._prefix)

    def seal(self, value: str) -> str:
        """Wrap *value* in an envelope.  Raises :class:`SealingKeyMissing`."""
        token = self._cipher().encrypt(str(value).encode("utf-8")).decode("ascii")
        return f"{self._prefix}{token}"

    def unseal(self, value: str) -> str:
        """Open an envelope produced by :meth:`seal`.

        Raises :class:`SealingKeyMissing` when no key is configured and
        ``cryptography.fernet.InvalidToken`` when the token is corrupt or was
        sealed under a different key.  Callers that must not fail a whole table
        over one bad row catch both and report the row.
        """
        text = str(value or "")
        if not self.is_sealed(text):
            raise ValueError("value is not a sealed envelope")
        return self._cipher().decrypt(text[len(self._prefix):].encode("ascii")).decode("utf-8")


__all__ = [
    "SealingKeyMissing",
    "SecretSealer",
    "derive_fernet",
]
