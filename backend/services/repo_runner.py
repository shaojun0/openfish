"""Logical per-repo runners — one credential, workspace and policy per repo.

The platform runs **one shared process pool** of agent runners (no docker
socket, no per-repo daemon; see ``services/agent_queue.py`` for why).  What this
module adds is the per-repository *configuration* a pooled worker resolves when
it picks up a task: which credential to clone/push with, where under
``AGENT_WORK_ROOT`` the checkout may live, which egress policy applies, and how
many tasks may be in flight for that repository.

The storage row is :class:`models.agent_hub.RepoRunner`, one per repository.
This module is the only writer of its credential fields:

* ``shared`` — the deployment-wide ``FORGEJO_RUNNER_TOKEN`` (the historical
  behaviour, and the default).
* ``repo`` — a repository-scoped token sealed with
  :class:`services.git_identity.TokenCipher` (Fernet; key from
  ``GIT_IDENTITY_KEY``).  **No plaintext token may ever reach the database or a
  log.**  A missing key is a configuration error: :meth:`RepoRunnerService.
  set_credential` stores nothing and :meth:`RepoRunnerService.credential`
  refuses to fall back to the shared token.

Everything is injectable — the session factory, the environment mapping and the
cipher — so the service runs under Flask, in a worker and in an offline gate
without a database server, and so a test can prove that the ciphertext on disk
never contains the plaintext.  The module imports no Flask.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from models.agent_hub import Repo, RepoRunner
from models.base import utcnow
from services.agent_runner import mask_secrets
from services.git_identity import GitIdentityConfigError, TokenCipher

logger = logging.getLogger("cpypiserver.repo_runner")

# ── Public vocabulary ────────────────────────────────────────────────
# Frozen names the rest of the platform imports; the CHECK constraints on
# ``repo_runners`` mirror these tuples, which is why a typo is an IntegrityError
# rather than a row only one query happens to match.

RUNNER_CREDENTIAL_SHARED = "shared"
RUNNER_CREDENTIAL_REPO = "repo"
RUNNER_CREDENTIAL_KINDS: tuple[str, ...] = (RUNNER_CREDENTIAL_SHARED, RUNNER_CREDENTIAL_REPO)

RUNNER_EGRESS_INHERIT = "inherit"
RUNNER_EGRESS_INTERNAL = "internal"
RUNNER_EGRESS_ALLOWLIST = "allowlist"
RUNNER_EGRESS_POLICIES: tuple[str, ...] = (
    RUNNER_EGRESS_INHERIT,
    RUNNER_EGRESS_INTERNAL,
    RUNNER_EGRESS_ALLOWLIST,
)

#: The deployment-wide fallback token.  This module owns the canonical reader
#: (:func:`shared_runner_token`); ``services/agent_worker.py`` keeps its own
#: thin wrapper for backwards compatibility.
SHARED_TOKEN_ENV = "FORGEJO_RUNNER_TOKEN"

#: Root directory name under ``AGENT_WORK_ROOT`` for per-runner workspaces.
DEFAULT_WORKSPACE_SUBDIR = "runners"

#: Username paired with a token in a git credential.  Forgejo accepts an access
#: token as the password with any username; the conventional one is
#: ``x-access-token``.
_CREDENTIAL_USERNAME = "x-access-token"

#: One path segment of a workspace subdir.  A segment may not be ``.``/``..``
#: (checked separately, because both match this pattern) and no character
#: outside this set is ever accepted.
_SEGMENT_RE = re.compile(r"^[A-Za-z0-9._-]+$")


# ── Errors ───────────────────────────────────────────────────────────

class RepoRunnerError(Exception):
    """A per-repo runner configuration or credential problem.

    Callers map this onto a typed refusal: invalid input, a missing encryption
    key, or an unreadable ciphertext.  It is never a signal to silently fall
    back to a different credential.
    """


# ── Workspace path safety ────────────────────────────────────────────

def safe_workspace_subdir(value: str | None, *, runner_id: int | None = None) -> str:
    """Validate/normalize a per-runner relative workspace path.

    Rejects absolute paths, ``""``/``.``/``..`` segments, backslashes, control
    characters and any segment that is not ``[A-Za-z0-9._-]``; an empty input
    means "the default", returned as ``runners/<runner_id>`` (or just
    ``runners`` when no id is known yet).  Raises :class:`RepoRunnerError` on a
    violation — a path that could escape ``AGENT_WORK_ROOT`` is never silently
    rewritten into a safe-looking one.
    """
    raw = "" if value is None else str(value).strip()
    if not raw:
        if runner_id is None:
            return DEFAULT_WORKSPACE_SUBDIR
        return f"{DEFAULT_WORKSPACE_SUBDIR}/{int(runner_id)}"

    if raw.startswith("/") or raw.startswith("\\"):
        raise RepoRunnerError(f"workspace_subdir 不能是绝对路径：{value!r}")
    if "\\" in raw:
        raise RepoRunnerError(f"workspace_subdir 不能包含反斜杠：{value!r}")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in raw):
        raise RepoRunnerError(f"workspace_subdir 不能包含控制字符：{value!r}")

    for segment in raw.split("/"):
        if segment in ("", ".", ".."):
            raise RepoRunnerError(f"workspace_subdir 含非法路径段：{value!r}")
        if not _SEGMENT_RE.match(segment):
            raise RepoRunnerError(f"workspace_subdir 含非法字符：{value!r}")
    return raw


# ── Shared-token reader ──────────────────────────────────────────────

def shared_runner_token(*, env: Mapping[str, str] | None = None) -> str:
    """The deployment-wide fallback token from ``FORGEJO_RUNNER_TOKEN``.

    ``""`` when the variable is unset or empty — never ``None`` — so a caller
    can branch on truthiness without a second None check.  The value is stripped
    the same way ``services.agent_worker.runner_token`` does.
    """
    return ((env or os.environ).get(SHARED_TOKEN_ENV) or "").strip()


# ── Credential value ─────────────────────────────────────────────────

@dataclass(frozen=True)
class RunnerCredential:
    """One ready-to-use credential for a repository's task.

    ``source`` is ``"repo"`` when this repository has its own sealed token and
    ``"shared"`` when it fell back to ``FORGEJO_RUNNER_TOKEN``.  ``token`` is
    the only place a plaintext token exists; it lives exactly as long as this
    value does and is never logged.
    """

    username: str
    # ``repr=False``: the plaintext token must never be rendered by ``repr()``,
    # ``%r`` or an f-string — here or on anything that embeds this value.
    token: str = field(repr=False)
    source: str
    expires_at: datetime | None = None


# ── Service ──────────────────────────────────────────────────────────

class RepoRunnerService:
    """Resolve and persist the logical runner of one repository.

    ``sessions`` is a session *factory* (``Session`` from
    ``extensions.database``, a ``sessionmaker`` or a lambda), so the service
    owns its transaction boundary and needs no Flask app.  ``env`` and
    ``cipher`` are injectable for the offline gate.
    """

    def __init__(
        self,
        sessions: Callable[[], Session],
        *,
        env: Mapping[str, str] | None = None,
        cipher: TokenCipher | None = None,
    ) -> None:
        self.sessions = sessions
        self._env = env
        self._cipher = cipher

    # -- lookups ------------------------------------------------------

    def get(self, repo_id: int) -> RepoRunner | None:
        """The runner row for *repo_id*, or ``None`` — never creates one."""
        session = self._session()
        try:
            return self._find(session, _repo_id(repo_id))
        finally:
            session.close()

    def list(self) -> list[RepoRunner]:
        """Every configured runner, ordered by ``repo_id``."""
        session = self._session()
        try:
            return (
                session.query(RepoRunner)
                .order_by(RepoRunner.repo_id.asc())
                .all()
            )
        finally:
            session.close()

    def document(self, repo_id: int, *, name: str = "") -> dict:
        """The runner as a JSON-ready document; **never creates the row**.

        A repository that has never been configured has no row and is described
        with the platform defaults (``id`` is ``None`` until a mutating call or
        :meth:`workspace_root` materialises it).  Materialising on a read would
        let a ``GET`` insert a row — and race ``enqueue``'s runner stamping,
        changing which runner a new task binds to.
        """
        repo_id = _repo_id(repo_id)
        session = self._session()
        try:
            row = self._find(session, repo_id)
            if row is not None:
                return row.to_dict()
        finally:
            session.close()
        return self._default_row(repo_id, name=name).to_dict()

    def ensure(self, repo_id: int, *, name: str = "") -> RepoRunner:
        """Return the repo's runner, creating it with defaults when absent.

        Idempotent: one runner per repository (``repo_id`` is unique), and an
        existing row is returned untouched — settings are only ever changed
        through :meth:`update`.  The name defaults to the repo slug, falling
        back to ``repo-<id>``.  The unique constraint is the concurrency
        backstop: the loser of a race re-reads the winner's row.
        """
        repo_id = _repo_id(repo_id)
        session = self._session()
        try:
            row = self._find(session, repo_id)
            if row is not None:
                return row
            return self._create(session, repo_id, name=name)
        finally:
            session.close()

    # -- mutation -----------------------------------------------------

    def update(
        self,
        repo_id: int,
        *,
        enabled: bool | None = None,
        max_concurrency: int | None = None,
        egress_policy: str | None = None,
        egress_allowlist: str | None = None,
        workspace_subdir: str | None = None,
    ) -> RepoRunner:
        """Validate then persist a settings change.

        ``max_concurrency`` must be ``>= 0`` (0 = inherit), ``egress_policy``
        must be one of :data:`RUNNER_EGRESS_POLICIES`, and ``workspace_subdir``
        goes through :func:`safe_workspace_subdir`.  A subdir that resolves to
        another runner's workspace (including its ``runners/<id>`` default) is
        rejected too: two repositories must never share one work root.  Every
        invalid value raises :class:`RepoRunnerError` before anything is
        persisted.  ``None`` means "leave unchanged"; pass
        ``egress_allowlist=""`` to clear the allowlist.
        """
        repo_id = _repo_id(repo_id)

        # Validate up front so an invalid request cannot leave a half-applied
        # change (or, on the first call, a freshly created default row).
        concurrency: int | None = None
        if max_concurrency is not None:
            try:
                concurrency = int(max_concurrency)
            except (TypeError, ValueError) as exc:
                raise RepoRunnerError(f"max_concurrency 必须是整数：{max_concurrency!r}") from exc
            if concurrency < 0:
                raise RepoRunnerError(f"max_concurrency 不能为负数：{concurrency}")

        policy: str | None = None
        if egress_policy is not None:
            policy = str(egress_policy).strip()
            if policy not in RUNNER_EGRESS_POLICIES:
                raise RepoRunnerError(
                    f"未知的 egress_policy：{egress_policy!r}（可选 {RUNNER_EGRESS_POLICIES}）"
                )

        subdir: str | None = None
        if workspace_subdir is not None and str(workspace_subdir).strip():
            subdir = safe_workspace_subdir(workspace_subdir)

        allowlist: str | None = None
        if egress_allowlist is not None:
            allowlist = _normalize_allowlist(egress_allowlist)

        session = self._session()
        try:
            row = self._find(session, repo_id)
            if row is None:
                row = self._create(session, repo_id, name="")
            if enabled is not None:
                row.enabled = bool(enabled)
            if concurrency is not None:
                row.max_concurrency = concurrency
            if policy is not None:
                row.egress_policy = policy
            if egress_allowlist is not None:
                row.egress_allowlist = allowlist
            if workspace_subdir is not None:
                effective = subdir or safe_workspace_subdir("", runner_id=row.id)
                conflict = self._workspace_conflict(session, repo_id, effective)
                if conflict is not None:
                    raise RepoRunnerError(
                        f"workspace_subdir {effective!r} 与仓库 {conflict.repo_id} "
                        "的 runner 工作区相同：两个仓库不能共享同一个工作区根"
                    )
                row.workspace_subdir = effective
            return self._persist(session, row)
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    # -- credentials --------------------------------------------------

    def set_credential(
        self,
        repo_id: int,
        *,
        token: str,
        username: str = "",
        expires_at: datetime | None = None,
    ) -> RepoRunner:
        """Seal *token* for this repository and mark it ``credential_kind='repo'``.

        The cipher is required **before** any database write, so a missing
        ``GIT_IDENTITY_KEY`` stores nothing and leaves the row on the shared
        credential (never a plaintext fallback).  An empty token is an error.
        """
        repo_id = _repo_id(repo_id)
        plaintext = (token or "").strip()
        if not plaintext:
            raise RepoRunnerError("runner 凭据不能为空")
        cipher = self._require_cipher()

        session = self._session()
        try:
            row = self._find(session, repo_id)
            if row is None:
                row = self._create(session, repo_id, name="")
            try:
                sealed = cipher.encrypt(plaintext)
            except Exception as exc:
                # Never log either side of the plaintext/ciphertext pair.
                logger.warning(
                    "runner credential encryption failed for repo %s: %s",
                    repo_id, mask_secrets(str(exc), [plaintext]),
                )
                raise RepoRunnerError("runner 凭据加密失败，未写入任何内容") from exc
            row.credential_kind = RUNNER_CREDENTIAL_REPO
            row.credential_username = (username or "").strip() or None
            row.credential_ciphertext = sealed
            row.credential_expires_at = expires_at
            row.credential_rotated_at = utcnow()
            result = self._persist(session, row)
            logger.info("stored a repo-scoped runner credential for repo %s", repo_id)
            return result
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def clear_credential(self, repo_id: int) -> RepoRunner:
        """Drop the repo-scoped token and fall back to the shared credential."""
        repo_id = _repo_id(repo_id)
        session = self._session()
        try:
            row = self._find(session, repo_id)
            if row is None:
                row = self._create(session, repo_id, name="")
            row.credential_kind = RUNNER_CREDENTIAL_SHARED
            row.credential_ciphertext = None
            row.credential_username = None
            row.credential_expires_at = None
            return self._persist(session, row)
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def credential(self, repo_id: int) -> RunnerCredential | None:
        """The credential a task for this repository must use.

        ``credential_kind='repo'`` always resolves the sealed token — a missing
        or empty ciphertext is an error, never a shared fallback.  Otherwise the
        shared ``FORGEJO_RUNNER_TOKEN`` is returned as ``source='shared'``.
        ``None`` means neither exists.

        A decrypt failure or an expired ``credential_expires_at`` raises
        :class:`RepoRunnerError` — **fail closed**.  Falling back to the shared
        token on a corrupt repo credential would silently authenticate as the
        wrong principal; presenting an expired token keeps a possibly-revoked
        credential in use.
        """
        repo_id = _repo_id(repo_id)
        session = self._session()
        try:
            row = self._find(session, repo_id)
            if row is not None and row.credential_kind == RUNNER_CREDENTIAL_REPO:
                self._reject_expired(row)
                return RunnerCredential(
                    username=row.credential_username or _CREDENTIAL_USERNAME,
                    token=self._decrypt(row),
                    source=RUNNER_CREDENTIAL_REPO,
                    expires_at=row.credential_expires_at,
                )
        finally:
            session.close()

        token = shared_runner_token(env=self._env)
        if not token:
            return None
        return RunnerCredential(
            username=_CREDENTIAL_USERNAME,
            token=token,
            source=RUNNER_CREDENTIAL_SHARED,
            expires_at=None,
        )

    # -- workspace / activity -----------------------------------------

    def workspace_root(self, repo_id: int, base: str | Path) -> Path:
        """``base / safe_workspace_subdir(runner.workspace_subdir, runner_id)``.

        Creates the runner row on first use (via :meth:`ensure`), because the
        default path is id-derived and therefore meaningless without the row.
        The joined path is resolved and must stay under *base*, so a symlink in
        a pre-existing component cannot redirect the checkout outside
        ``AGENT_WORK_ROOT``; the directory is then created ``0700`` before it is
        returned.
        """
        row = self.ensure(repo_id)
        subdir = safe_workspace_subdir(row.workspace_subdir, runner_id=row.id)
        base_path = Path(base)
        root = base_path / subdir
        resolved = root.resolve()
        if not resolved.is_relative_to(base_path.resolve()):
            raise RepoRunnerError(
                f"workspace_subdir 解析后越出工作根 {base_path}：{root}"
            )
        resolved.mkdir(parents=True, exist_ok=True, mode=0o700)
        return root

    def record_task(self, repo_id: int) -> None:
        """Stamp ``last_task_at`` — the per-repo "recently active" signal."""
        repo_id = _repo_id(repo_id)
        session = self._session()
        try:
            row = self._find(session, repo_id)
            if row is None:
                row = self._create(session, repo_id, name="")
            row.last_task_at = utcnow()
            self._persist(session, row)
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    # -- internals ----------------------------------------------------

    def _session(self) -> Session:
        return self.sessions()

    @staticmethod
    def _find(session: Session, repo_id: int) -> RepoRunner | None:
        return (
            session.query(RepoRunner)
            .filter(RepoRunner.repo_id == repo_id)
            .one_or_none()
        )

    @staticmethod
    def _effective_subdir(row: RepoRunner) -> str:
        """The path *row* actually resolves to, default included."""
        return safe_workspace_subdir(row.workspace_subdir, runner_id=row.id)

    @classmethod
    def _workspace_conflict(
        cls, session: Session, repo_id: int, candidate: str
    ) -> RepoRunner | None:
        """Another runner whose effective workspace equals *candidate*."""
        others = session.query(RepoRunner).filter(RepoRunner.repo_id != repo_id).all()
        for other in others:
            if cls._effective_subdir(other) == candidate:
                return other
        return None

    @staticmethod
    def _reject_expired(row: RepoRunner) -> None:
        """Raise when the repo credential's recorded expiry has passed."""
        expires = row.credential_expires_at
        if expires is None:
            return
        if expires.tzinfo is None:  # SQLite hands back naive datetimes
            expires = expires.replace(tzinfo=timezone.utc)
        if expires <= utcnow():
            raise RepoRunnerError(
                f"仓库 {row.repo_id} 的 runner 凭据已于 {expires.isoformat()} 过期，"
                "拒绝使用（过期即 fail-closed）"
            )

    @staticmethod
    def _default_row(repo_id: int, *, name: str) -> RepoRunner:
        """A transient row carrying the documented defaults (never persisted)."""
        resolved = (name or "").strip() or f"repo-{repo_id}"
        return RepoRunner(
            repo_id=repo_id,
            name=resolved[:128],
            enabled=True,
            max_concurrency=0,
            workspace_subdir="",
            egress_policy=RUNNER_EGRESS_INHERIT,
            egress_allowlist=None,
            credential_kind=RUNNER_CREDENTIAL_SHARED,
            credential_username=None,
            credential_ciphertext=None,
            credential_expires_at=None,
            credential_rotated_at=None,
            last_task_at=None,
        )

    @staticmethod
    def _repo_slug(session: Session, repo_id: int) -> str:
        slug = session.query(Repo.slug).filter(Repo.id == repo_id).scalar()
        return str(slug or "").strip()

    def _create(self, session: Session, repo_id: int, *, name: str) -> RepoRunner:
        """Insert the default row; idempotent and race-tolerant.

        The default workspace is ``runners/<id>``, so the id is flushed before
        the collision check: another runner may already resolve to that exact
        path (an explicit ``workspace_subdir`` that names this id).
        """
        resolved = (name or "").strip() or self._repo_slug(session, repo_id)
        if not resolved:
            resolved = f"repo-{repo_id}"
        row = RepoRunner(repo_id=repo_id, name=resolved[:128])
        session.add(row)
        try:
            session.flush()  # assigns row.id without committing
            effective = safe_workspace_subdir("", runner_id=row.id)
            conflict = self._workspace_conflict(session, repo_id, effective)
            if conflict is not None:
                session.rollback()
                raise RepoRunnerError(
                    f"默认工作区 {effective!r} 已被仓库 {conflict.repo_id} 的 runner "
                    "占用：两个仓库不能共享同一个工作区根"
                )
            session.commit()
        except IntegrityError:
            session.rollback()
            existing = self._find(session, repo_id)
            if existing is None:
                raise
            return existing
        session.refresh(row)
        return row

    def _require_cipher(self) -> TokenCipher:
        """The cipher, translating a missing key into a domain error."""
        if self._cipher is None:
            try:
                self._cipher = TokenCipher(env=self._env)
            except GitIdentityConfigError as exc:
                raise RepoRunnerError(
                    "GIT_IDENTITY_KEY 未配置：runner 凭据必须加密存储，不会以明文降级"
                ) from exc
        return self._cipher

    def _decrypt(self, row: RepoRunner) -> str:
        cipher = self._require_cipher()
        ciphertext = row.credential_ciphertext or ""
        try:
            token = cipher.decrypt(ciphertext).strip()
        except Exception as exc:
            logger.warning(
                "runner credential for repo %s is unreadable; refusing the shared fallback: %s",
                row.repo_id, mask_secrets(str(exc), [ciphertext]),
            )
            raise RepoRunnerError(
                f"仓库 {row.repo_id} 的 runner 凭据无法解密，拒绝回退到共享 token"
            ) from exc
        if not token:
            raise RepoRunnerError(
                f"仓库 {row.repo_id} 的 runner 凭据解密为空，拒绝回退到共享 token"
            )
        return token

    @staticmethod
    def _persist(session: Session, row: RepoRunner) -> RepoRunner:
        """Commit and re-load the row so it is safe to read after ``close``.

        ``session.refresh`` matters because a caller may inject a session factory
        with the default ``expire_on_commit=True``; without it, reading a column
        on the detached row would raise ``DetachedInstanceError``.
        """
        session.commit()
        session.refresh(row)
        return row


# ── Helpers ──────────────────────────────────────────────────────────

def _repo_id(repo_id: int) -> int:
    try:
        return int(repo_id)
    except (TypeError, ValueError) as exc:
        raise RepoRunnerError(f"repo_id 必须是整数：{repo_id!r}") from exc


def _normalize_allowlist(value: str) -> str | None:
    """Comma-separated hosts → a stable, de-duplicated list (``None`` if empty)."""
    hosts: list[str] = []
    for raw in str(value).replace(";", ",").split(","):
        host = raw.strip()
        if host and host not in hosts:
            hosts.append(host)
    return ",".join(hosts) if hosts else None


__all__ = [
    "DEFAULT_WORKSPACE_SUBDIR",
    "RUNNER_CREDENTIAL_KINDS",
    "RUNNER_CREDENTIAL_REPO",
    "RUNNER_CREDENTIAL_SHARED",
    "RUNNER_EGRESS_ALLOWLIST",
    "RUNNER_EGRESS_INHERIT",
    "RUNNER_EGRESS_INTERNAL",
    "RUNNER_EGRESS_POLICIES",
    "SHARED_TOKEN_ENV",
    "RepoRunnerError",
    "RepoRunnerService",
    "RunnerCredential",
    "safe_workspace_subdir",
    "shared_runner_token",
]
