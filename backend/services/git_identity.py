"""Git identity exchange — mint a Forgejo credential for a platform user.

Spec: ``docs/agent-hub/DEVELOPMENT.md`` §5.2 (the credential hand-off) and
§13.1 (the open question this module closes), plus the S6 integration note.

The problem
-----------
``git-receive-pack`` is authenticated by **Forgejo**, not by this platform.
Handing a git client an openfish API key therefore closes ``clone`` (Forgejo
serves public reads anonymously) but never ``push``: Forgejo does not know this
platform's keys.  The two halves of the §5.2 contract are reconciled here —
openfish stays the identity authority, Forgejo stays the executor::

    caller ──(platform credential)──▶ GET /api/v1/repos/<slug>/git-credential
                                       ① require_permission(REPO_PUSH)   [route]
                                       ② ensure(user)  → git_identities row
                                       ③ FORGEJO_ADMIN_TOKEN → Forgejo:
                                            POST /admin/users
                                            POST /users/{u}/tokens
                                       ④ {username: of-…, password: <token>}
    caller ──▶ git push ──▶ Forgejo checks its *own* token → ok

Design rules (all three are load-bearing)
-----------------------------------------
* **Per user, not a shared robot.**  Every openfish account gets its own
  Forgejo account, so commit attribution, audit and single-person revocation
  all work.  The mapping lives in ``git_identities``.
* **The username is a frozen contract.**  :func:`derive_username` is
  ``of-<user_id>-<sha256(external_id)[:10]>``: deterministic, ``[a-z0-9-]`` and
  at most 39 characters.  Renaming a Forgejo account would orphan every commit
  authored under it, so this rule must not change once deployed.
* **No plaintext token ever touches the database or a log.**  Tokens are sealed
  with :class:`TokenCipher` (Fernet; key from ``GIT_IDENTITY_KEY``) and log
  lines are masked with :func:`services.agent_runner.mask_secrets`.  A missing
  key is a *configuration error* — a clear :class:`GitIdentityConfigError`, never
  a silent plaintext fallback.

Everything external goes through :class:`ForgejoIdentityClient`, and every knob
is injectable, so ``scripts/check_git_identity.py`` runs the whole thing against
a fake client, a throwaway SQLite file and no network.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import quote

import requests
from cryptography.fernet import Fernet
from sqlalchemy.exc import IntegrityError

from models.agent_hub import GitIdentity
from models.base import iso, utcnow
from services.agent_runner import mask_secrets
from services.digest import sha256_text
from services.repo_import import (
    RETRY_BACKOFF_SECONDS,
    RETRY_STATUSES,
    ForgejoError,
    ImportConfig,
    RateLimiter,
)

logger = logging.getLogger("cpypiserver.git_identity")


# ── Configuration constants ──────────────────────────────────────────

#: Environment variable holding the Fernet master key for token storage.
GIT_IDENTITY_KEY_ENV = "GIT_IDENTITY_KEY"

#: What an operator sees when the key is missing.  Deliberately actionable:
#: the endpoint answers 503 rather than degrading to plaintext.
GIT_IDENTITY_KEY_HELP = (
    "GIT_IDENTITY_KEY 未配置：git 身份兑换必须加密存储 Forgejo token，不会以明文降级。"
    "生成并注入 backend 容器：python -c "
    "\"import secrets; print(secrets.token_urlsafe(48))\"（见 docs/agent-hub/integration/S6.md）"
)

#: How long a minted Forgejo token is reused before it is rotated.  Forgejo
#: access tokens carry no expiry of their own, so this deadline is *ours*:
#: after it, :meth:`GitIdentityService.token_for` revokes the old token and
#: mints a new one.
DEFAULT_TOKEN_LIFETIME_DAYS = 7

#: The one token name this platform uses per Forgejo account.  Forgejo rejects a
#: duplicate name (HTTP 400), which is why rotation deletes before it creates —
#: and why a user never accumulates one token per rotation window.
FORGEJO_TOKEN_NAME = "openfish-git"

#: access-token scopes (Forgejo docs, "Access Token Scope").  ``read:``/``write:``
#: are the two rungs; ``write`` covers POST/PUT/PATCH/DELETE.  The *platform*
#: admin token needs ``write:admin`` for the ``/admin/*`` calls — that is
#: deployment configuration (S6.md), never something this module mints.
SCOPE_REPOSITORY_READ = "read:repository"
SCOPE_REPOSITORY_WRITE = "write:repository"
SCOPE_ADMIN_WRITE = "write:admin"

#: The scopes a *user* ticket is minted with, by mode.  Least privilege: a
#: read-only mirror must never receive a token that can push to any repo.
USER_SCOPES_READ_ONLY: tuple[str, ...] = (SCOPE_REPOSITORY_READ,)
USER_SCOPES_WRITE: tuple[str, ...] = (SCOPE_REPOSITORY_WRITE,)

#: Username derivation (§ identity contract, frozen once deployed).
USERNAME_PREFIX = "of-"
USERNAME_HASH_LENGTH = 10
USERNAME_MAX_LENGTH = 39
_USERNAME_RE = re.compile(r"^[a-z0-9-]+$")


# ── Errors ───────────────────────────────────────────────────────────

class GitIdentityError(Exception):
    """Base class for the identity exchange's domain errors."""


class GitIdentityConfigError(GitIdentityError):
    """The exchange is not configured (missing key / Forgejo admin token).

    The route maps this onto ``503``: the platform is up, the feature is not
    usable, and — critically — it must not fall back to storing tokens in clear.
    """


# ── Username derivation (the frozen contract) ────────────────────────

def derive_username(user_id: int, external_id: str) -> str:
    """The Forgejo login name for one openfish account — deterministic.

    ``of-<user_id>-<sha256(external_id)[:10]>``, characters ``[a-z0-9-]`` only,
    at most :data:`USERNAME_MAX_LENGTH` characters.

    Properties the gate asserts:

    * the same ``(user_id, external_id)`` always yields the same name;
    * two different users never collide, because ``user_id`` (unique on
      ``users``) is part of the name — an ``external_id`` hash collision alone
      could not merge two accounts;
    * the result is a valid Forgejo / git login (lowercase, digits, dashes).

    The digest comes from :func:`services.digest.sha256_text`, the project's one
    SHA-256 implementation (§3.2).
    """
    try:
        numeric_id = int(user_id)
    except (TypeError, ValueError) as exc:
        raise GitIdentityError(f"user_id 必须是整数：{user_id!r}") from exc
    digest = sha256_text(str(external_id or ""))
    username = f"{USERNAME_PREFIX}{numeric_id}-{digest[:USERNAME_HASH_LENGTH]}"
    if len(username) > USERNAME_MAX_LENGTH or not _USERNAME_RE.match(username):
        raise GitIdentityError(f"派生的 Forgejo 用户名不合法：{username!r}")
    return username


def synthetic_email(username: str) -> str:
    """A deterministic, reserved-TLD address for a Forgejo shadow account.

    Forgejo requires a unique e-mail per account.  ``.invalid`` is reserved by
    RFC 2606, so this can never route mail and never collides with a real one.
    """
    return f"{username}@openfish.invalid"


# ── Token encryption ─────────────────────────────────────────────────

class TokenCipher:
    """Fernet wrapper that seals a token for storage.

    ``GIT_IDENTITY_KEY`` may be any high-entropy secret (the S6.md command is
    ``secrets.token_urlsafe(48)``); a valid Fernet key is *derived* from it as
    ``urlsafe_b64encode(sha256(secret))``.  Deriving rather than demanding
    exactly 32 url-safe bytes is deliberate: ``Fernet.generate_key()`` and a
    random token both work, so an operator cannot end up with a key the code
    silently refuses.  Rotation of the key is out of scope and documented.
    """

    def __init__(self, key: str | None = None, *, env: Mapping[str, str] | None = None) -> None:
        raw = key
        if raw is None:
            raw = (env or os.environ).get(GIT_IDENTITY_KEY_ENV)
        raw = (raw or "").strip()
        if not raw:
            raise GitIdentityConfigError(GIT_IDENTITY_KEY_HELP)
        derived = base64.urlsafe_b64encode(bytes.fromhex(sha256_text(raw)))
        self._fernet = Fernet(derived)

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")

    def decrypt(self, ciphertext: str) -> str:
        return self._fernet.decrypt(ciphertext.encode("ascii")).decode("utf-8")


# ── The Forgejo admin client (injectable) ────────────────────────────

class ForgejoIdentityClient:
    """The handful of Forgejo admin calls the exchange needs.

    Same posture as :class:`services.repo_import.ForgejoClient` — one
    ``requests`` session, a shared :class:`RateLimiter`, bounded retry/backoff —
    but scoped to users and tokens so the offline gate can substitute a fake and
    never touch the network.

    * ``GET    /api/v1/users/{username}``                 find
    * ``POST   /api/v1/admin/users``                      create
    * ``POST   /api/v1/users/{username}/tokens``          mint (admin + Sudo)
    * ``DELETE /api/v1/users/{username}/tokens/{name}``   revoke
    * ``DELETE /api/v1/admin/users/{username}``           delete

    The mint/revoke calls send the admin token *and* a ``Sudo: {username}``
    header.  Forgejo's ``/users/{username}/tokens`` handler resolves the target
    from the path (``ctx.User()``), and its documented admin mechanism is the
    ``Sudo`` header; S6.md records the equivalent ``forgejo admin user
    generate-access-token`` CLI path for deployments that prefer it.

    The token value is read from either the modern ``token`` field or the older
    ``sha1`` one, because Forgejo renamed it.  No response body is ever logged.
    """

    def __init__(
        self,
        *,
        config: ImportConfig | None = None,
        session: requests.Session | None = None,
        limiter: RateLimiter | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config or ImportConfig.from_env()
        self.base_url = self.config.base_url
        self.timeout = self.config.http_timeout
        self.session = session or requests.Session()
        self.limiter = limiter or RateLimiter(self.config.max_rate)
        self._sleep = sleeper
        self._token = self.config.admin_token

    # -- plumbing -----------------------------------------------------

    @property
    def configured(self) -> bool:
        """True when both the base URL and the platform admin token are set."""
        return bool(self.base_url and self._token)

    def _headers(self, *, sudo: str | None = None) -> dict[str, str]:
        headers = {"Accept": "application/json", "User-Agent": "openfish-agent-hub/1"}
        token = (self._token or "").strip()
        if token:
            headers["Authorization"] = f"token {token}"
        if sudo:
            headers["Sudo"] = sudo
        return headers

    def _api(self, path: str) -> str:
        return f"{self.base_url}/api/v1/{path.lstrip('/')}"

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        sudo: str | None = None,
        attempts: int = len(RETRY_BACKOFF_SECONDS) + 1,
    ) -> requests.Response:
        """One HTTP call with rate limiting and bounded retry/backoff.

        Neither the admin token nor the minted token is ever logged; a warning
        that does quote an exception is passed through
        :func:`services.agent_runner.mask_secrets` first, in case a transport
        error ever embeds one (§AGENTS.md rule 6).
        """
        if not self.configured:
            raise GitIdentityConfigError(
                "Forgejo 管理 API 未配置：需要 FORGEJO_BASE_URL 与 FORGEJO_ADMIN_TOKEN"
            )
        url = self._api(path)
        last: Exception | None = None
        for attempt in range(1, attempts + 1):
            self.limiter.wait()
            try:
                response = self.session.request(
                    method.upper(),
                    url,
                    headers=self._headers(sudo=sudo),
                    json=json_body,
                    timeout=(5.0, self.timeout),
                )
            except requests.RequestException as exc:
                last = exc
                logger.warning(
                    "forgejo %s %s failed (%d/%d): %s", method, url, attempt, attempts,
                    mask_secrets(str(exc), [self._token]),
                )
            else:
                if response.status_code not in RETRY_STATUSES:
                    return response
                last = ForgejoError(f"Forgejo {method} {url} → HTTP {response.status_code}")
                logger.warning("forgejo %s %s → HTTP %d (%d/%d)",
                               method, url, response.status_code, attempt, attempts)
            if attempt < attempts:
                self._sleep(RETRY_BACKOFF_SECONDS[min(attempt - 1,
                                                      len(RETRY_BACKOFF_SECONDS) - 1)])
        raise ForgejoError(f"Forgejo {method} {url} 连续失败：{last}")

    @staticmethod
    def _json_object(response: requests.Response) -> dict[str, Any]:
        try:
            data = response.json()
        except ValueError as exc:
            raise ForgejoError(f"Forgejo 返回非 JSON（HTTP {response.status_code}）") from exc
        if not isinstance(data, dict):
            raise ForgejoError(f"Forgejo 返回的不是 JSON 对象（HTTP {response.status_code}）")
        return data

    # -- users --------------------------------------------------------

    def find_user(self, username: str) -> dict[str, Any] | None:
        response = self._request("GET", f"users/{quote(username, safe='')}")
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise ForgejoError(f"Forgejo GET users/{username} → HTTP {response.status_code}")
        return self._json_object(response)

    def ensure_user(
        self,
        username: str,
        *,
        email: str,
        display_name: str = "",
        password: str | None = None,
    ) -> dict[str, Any]:
        """Find or create the Forgejo account; idempotent and race-tolerant.

        The password is generated and **discarded**: the account exists to own
        tokens, not to be logged into, and keeping a second secret would be a
        second thing to leak.  A concurrent create answers 400/409/422 (Forgejo
        spells its uniqueness error differently across versions), which is
        treated as "someone else won the race" and re-read.
        """
        existing = self.find_user(username)
        if existing is not None:
            return existing
        body: dict[str, Any] = {
            "username": username,
            "email": email,
            "password": password or secrets.token_urlsafe(24),
            "must_change_password": False,
            "send_notify": False,
        }
        if display_name:
            body["full_name"] = display_name
        response = self._request("POST", "admin/users", json_body=body)
        if response.status_code in (400, 409, 422):
            existing = self.find_user(username)
            if existing is not None:
                return existing
            raise ForgejoError(
                f"Forgejo 创建用户 {username} 失败：HTTP {response.status_code}"
            )
        if response.status_code >= 400:
            raise ForgejoError(f"Forgejo POST admin/users → HTTP {response.status_code}")
        return self._json_object(response)

    def delete_user(self, username: str) -> bool:
        response = self._request("DELETE", f"admin/users/{quote(username, safe='')}")
        if response.status_code in (200, 204):
            return True
        if response.status_code == 404:
            return False
        raise ForgejoError(f"Forgejo DELETE admin/users/{username} → HTTP {response.status_code}")

    # -- tokens -------------------------------------------------------

    def create_token(
        self,
        username: str,
        *,
        name: str,
        scopes: Sequence[str],
    ) -> dict[str, Any]:
        response = self._request(
            "POST",
            f"users/{quote(username, safe='')}/tokens",
            json_body={"name": name, "scopes": list(scopes)},
            sudo=username,
        )
        if response.status_code >= 400:
            raise ForgejoError(
                f"Forgejo 为用户 {username} 签发 token → HTTP {response.status_code}"
            )
        data = self._json_object(response)
        token = str(data.get("token") or data.get("sha1") or "")
        if not token:
            raise ForgejoError(f"Forgejo 为用户 {username} 签发的 token 缺少 token 字段")
        return {"id": data.get("id"), "name": str(data.get("name") or name), "token": token}

    def delete_token(self, username: str, *, name: str) -> bool:
        response = self._request(
            "DELETE",
            f"users/{quote(username, safe='')}/tokens/{quote(str(name), safe='')}",
            sudo=username,
        )
        if response.status_code in (200, 204):
            return True
        if response.status_code == 404:
            return False
        raise ForgejoError(
            f"Forgejo DELETE users/{username}/tokens/{name} → HTTP {response.status_code}"
        )


# ── The service ──────────────────────────────────────────────────────

@dataclass(frozen=True)
class GitCredential:
    """One ready-to-use git credential.

    ``token`` is the only place a plaintext Forgejo token exists, and it lives
    exactly as long as this value does.  ``read_only`` records which scope the
    token was minted with, so a caller can tell a mirror ticket from a writable
    one.
    """

    username: str
    token: str
    expires_at: str | None
    read_only: bool


#: Process-local first-provision locks, keyed by ``user_id``.  The database
#: unique constraint is the real guarantee (it also covers a second process);
#: this only stops two in-flight requests from both calling Forgejo, and keeps
#: SQLite from seeing two writers on the first request.
_ENSURE_LOCKS: dict[int, threading.Lock] = {}
_ENSURE_LOCKS_GUARD = threading.Lock()


def _ensure_lock(user_id: int) -> threading.Lock:
    with _ENSURE_LOCKS_GUARD:
        lock = _ENSURE_LOCKS.get(user_id)
        if lock is None:
            lock = threading.Lock()
            _ENSURE_LOCKS[user_id] = lock
        return lock


class GitIdentityService:
    """Lazy per-user Forgejo provisioning plus token mint / reuse / revoke.

    The session is passed in, not imported from ``extensions.database``, so the
    service runs identically in a Flask request, a worker, the CLI and the
    offline gate.  The Forgejo client, the cipher, the clock and the environment
    are all injectable for the same reason.
    """

    def __init__(
        self,
        session: Any,
        *,
        client: Any | None = None,
        cipher: TokenCipher | None = None,
        env: Mapping[str, str] | None = None,
        now: Callable[[], datetime] = utcnow,
        lifetime_days: int = DEFAULT_TOKEN_LIFETIME_DAYS,
        token_name: str = FORGEJO_TOKEN_NAME,
    ) -> None:
        self.session = session
        self.client = client if client is not None else ForgejoIdentityClient()
        self._cipher = cipher
        self._env = env
        self._now = now
        self.lifetime_days = int(lifetime_days)
        self.token_name = token_name

    @property
    def cipher(self) -> TokenCipher:
        """The token cipher, built on first use.

        Lazy on purpose: ``revoke`` does not need the key (it only needs the
        deterministic username), so an operator can still cut a user off while
        ``GIT_IDENTITY_KEY`` is missing; only *minting* demands the key.
        """
        if self._cipher is None:
            self._cipher = TokenCipher(env=self._env)
        return self._cipher

    # -- lookups ------------------------------------------------------

    def find(self, user_id: int) -> GitIdentity | None:
        return (
            self.session.query(GitIdentity)
            .filter(GitIdentity.user_id == int(user_id))
            .one_or_none()
        )

    def by_username(self, forgejo_username: str) -> GitIdentity | None:
        return (
            self.session.query(GitIdentity)
            .filter(GitIdentity.forgejo_username == forgejo_username)
            .one_or_none()
        )

    # -- ensure (lazy create, idempotent, concurrency-safe) -----------

    def ensure(
        self,
        user_id: int,
        external_id: str,
        *,
        display_name: str | None = None,
        email: str | None = None,
    ) -> GitIdentity:
        """Return the mapping for *user_id*, creating it (both sides) if needed.

        Idempotent: an existing, non-revoked row is returned without touching
        Forgejo at all — which is what makes token reuse cheap.  A revoked row
        is revived (the Forgejo account is re-ensured, the row cleared).

        Concurrency: a process-local lock per user serialises the first
        provision, and the ``git_identities.user_id`` unique constraint is the
        backstop — on ``IntegrityError`` the loser re-reads the winner's row
        (the same pattern as :meth:`services.authz.AuthzService.provision_user`).
        """
        user_id = int(user_id)
        external_id = (external_id or "").strip()
        if not external_id:
            raise GitIdentityError("external_id 不能为空：Forgejo 用户名由它派生")

        row = self.find(user_id)
        if row is not None and row.revoked_at is None:
            return row

        username = derive_username(user_id, external_id)
        with _ensure_lock(user_id):
            # Re-read under the lock: a concurrent first request may have won
            # while this one waited.
            row = self.find(user_id)
            if row is not None and row.revoked_at is None:
                return row
            # Remote account first: a crash between the writes then leaves an
            # unused Forgejo account (harmless) rather than a mapping that
            # points at nothing (which would 502 every credential request).
            self.client.ensure_user(
                username,
                email=email or synthetic_email(username),
                display_name=display_name or "",
            )
            if row is None:
                row = GitIdentity(
                    user_id=user_id,
                    forgejo_username=username,
                    external_id=external_id,
                )
                self.session.add(row)
                try:
                    self.session.commit()
                except IntegrityError:
                    self.session.rollback()
                    existing = self.find(user_id)
                    if existing is None:
                        raise
                    return existing
            else:
                row.revoked_at = None
                row.external_id = external_id
                if row.forgejo_username != username:
                    row.forgejo_username = username
                self.session.commit()
            logger.info("provisioned git identity %s for user %s", username, user_id)
            return row

    # -- token reuse / mint -------------------------------------------

    def token_for(
        self,
        user_id: int,
        external_id: str | None = None,
        *,
        display_name: str | None = None,
        email: str | None = None,
        read_only: bool = False,
    ) -> GitCredential:
        """A live credential for *user_id*: reuse the token or mint a new one.

        A stored token is reused while it is unexpired **and** was minted for
        the same scope; otherwise the old one is revoked first and a fresh one
        replaces it (Forgejo refuses a duplicate token name, and revoking first
        is what stops one token per rotation window from piling up).
        """
        # Every successful path seals or unseals, so validate GIT_IDENTITY_KEY
        # *before* any remote write: a missing key must produce a clean config
        # error without leaving an orphan Forgejo token behind.
        self._require_cipher()
        row = self.find(int(user_id))
        if external_id is None and row is not None:
            external_id = row.external_id
        if external_id is None:
            raise GitIdentityError(f"用户 {user_id} 没有 git 身份，且未提供 external_id")
        row = self.ensure(user_id, external_id, display_name=display_name, email=email)

        now = self._now()
        token = self._reusable_token(row, now=now, read_only=read_only)
        if token is None:
            token = self._mint(row, read_only=read_only, now=now)
        return GitCredential(
            username=row.forgejo_username,
            token=token,
            expires_at=iso(row.token_expires_at),
            read_only=bool(read_only),
        )

    def _reusable_token(
        self, row: GitIdentity, *, now: datetime, read_only: bool,
    ) -> str | None:
        if row.revoked_at is not None or not row.token_ciphertext:
            return None
        if self._is_expired(row.token_expires_at, now):
            return None
        sealed = self._unseal(row)
        if sealed is None or bool(sealed.get("read_only")) != bool(read_only):
            return None
        token = str(sealed.get("token") or "")
        return token or None

    def _mint(self, row: GitIdentity, *, read_only: bool, now: datetime) -> str:
        scopes = USER_SCOPES_READ_ONLY if read_only else USER_SCOPES_WRITE
        had_token = bool(row.token_ciphertext)
        if had_token:
            try:
                self.client.delete_token(row.forgejo_username, name=self.token_name)
            except (ForgejoError, GitIdentityError) as exc:
                # A stale token we could not delete is a rotation delay, not a
                # reason to refuse the push; the new mint is still attempted.
                logger.warning(
                    "could not revoke the previous Forgejo token for %s: %s",
                    row.forgejo_username, exc,
                )
        minted = self.client.create_token(
            row.forgejo_username, name=self.token_name, scopes=scopes,
        )
        token = str(minted.get("token") or "")
        if not token:
            raise GitIdentityError(f"Forgejo 没有为 {row.forgejo_username} 返回 token")
        row.token_ciphertext = self._seal(token, read_only)
        row.token_expires_at = now + timedelta(days=self.lifetime_days)
        row.rotated_at = now
        row.revoked_at = None
        self.session.commit()
        logger.info(
            "minted a %s Forgejo token for %s (rotates in %d day(s))",
            "read-only" if read_only else "read/write", row.forgejo_username,
            self.lifetime_days,
        )
        return token

    # -- revoke / rotate ----------------------------------------------

    def revoke(self, user_id: int, *, delete_forgejo_user: bool = False) -> bool:
        """Cut a user's git access off; optionally remove the Forgejo account.

        The remote token is always revoked and ``revoked_at`` written.  With
        ``delete_forgejo_user=True`` the Forgejo account goes too, but the local
        row stays (the deterministic mapping is worth keeping) — a later
        :meth:`ensure` revives it under the same name.
        """
        row = self.find(int(user_id))
        if row is None:
            return False
        if row.token_ciphertext:
            try:
                self.client.delete_token(row.forgejo_username, name=self.token_name)
            except (ForgejoError, GitIdentityError) as exc:
                logger.warning(
                    "could not revoke the Forgejo token for %s: %s",
                    row.forgejo_username, exc,
                )
        if delete_forgejo_user:
            try:
                self.client.delete_user(row.forgejo_username)
            except (ForgejoError, GitIdentityError) as exc:
                logger.warning(
                    "could not delete the Forgejo account %s: %s",
                    row.forgejo_username, exc,
                )
        row.token_ciphertext = None
        row.token_expires_at = None
        row.revoked_at = self._now()
        self.session.commit()
        logger.info(
            "revoked git identity for user %s (forgejo account deleted=%s)",
            user_id, bool(delete_forgejo_user),
        )
        return True

    def rotate_expired(self, *, limit: int = 100) -> list[GitCredential]:
        """Re-mint every identity whose token has passed its deadline.

        The batch entry point a worker or ``cli.py`` calls.  Expiry is decided
        in Python rather than SQL so the SQLite/PostgreSQL timezone difference
        cannot decide whether a token is stale.
        """
        now = self._now()
        rows = (
            self.session.query(GitIdentity)
            .filter(
                GitIdentity.revoked_at.is_(None),
                GitIdentity.token_expires_at.is_not(None),
            )
            .order_by(GitIdentity.token_expires_at.asc())
            .limit(max(1, int(limit)))
            .all()
        )
        rotated: list[GitCredential] = []
        for row in rows:
            if not self._is_expired(row.token_expires_at, now):
                continue
            sealed = self._unseal(row) or {}
            try:
                rotated.append(self.token_for(
                    row.user_id, row.external_id, read_only=bool(sealed.get("read_only")),
                ))
            except (ForgejoError, GitIdentityError) as exc:
                logger.warning(
                    "could not rotate the git identity for user %s: %s", row.user_id, exc,
                )
        return rotated

    # -- sealed payload / clock helpers -------------------------------

    def _require_cipher(self) -> None:
        """Touch the cipher so a missing key fails before any remote write."""
        _ = self.cipher

    def _seal(self, token: str, read_only: bool) -> str:
        payload = json.dumps(
            {"token": token, "read_only": bool(read_only)},
            separators=(",", ":"), sort_keys=True,
        )
        return self.cipher.encrypt(payload)

    def _unseal(self, row: GitIdentity) -> dict[str, Any] | None:
        if not row.token_ciphertext:
            return None
        try:
            payload = json.loads(self.cipher.decrypt(row.token_ciphertext))
        except Exception:  # noqa: BLE001 - a corrupt/foreign ciphertext rotates
            logger.warning(
                "git identity token for user %s is unreadable; it will be rotated",
                row.user_id,
            )
            return None
        return payload if isinstance(payload, dict) else None

    @staticmethod
    def _is_expired(expires_at: datetime | None, now: datetime) -> bool:
        if expires_at is None:
            return True
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        return expires_at <= now


__all__ = [
    "DEFAULT_TOKEN_LIFETIME_DAYS",
    "FORGEJO_TOKEN_NAME",
    "GIT_IDENTITY_KEY_ENV",
    "GIT_IDENTITY_KEY_HELP",
    "SCOPE_ADMIN_WRITE",
    "SCOPE_REPOSITORY_READ",
    "SCOPE_REPOSITORY_WRITE",
    "USERNAME_MAX_LENGTH",
    "USERNAME_PREFIX",
    "USER_SCOPES_READ_ONLY",
    "USER_SCOPES_WRITE",
    "ForgejoIdentityClient",
    "GitCredential",
    "GitIdentityConfigError",
    "GitIdentityError",
    "GitIdentityService",
    "TokenCipher",
    "derive_username",
    "synthetic_email",
]
