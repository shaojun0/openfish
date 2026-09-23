"""Repository import — drive Forgejo's migration and mirror the result here.

Decision D1 (§2.2 of ``docs/agent-hub/DEVELOPMENT.md``) puts the git protocol
and the actual migration in Forgejo: this module never parses a git object and
never writes one.  What it owns is the *pipeline* around that migration, in the
order §8.2 fixes it::

    1 validate       parse the source URL, probe reachability + issue volume
    2 migrate        POST the Forgejo migration (issues/labels/milestones/prs)
    3 poll           watch the migration, write ImportJob.progress
    4 mirror_issues  page through Forgejo issues, upsert into repo_issues
    5 index_commits  page through commit metadata into repo_commits
    6 done           repo.sync_state = ready, materialise issue/commit counts

Three properties are non-negotiable, because a 20 000-issue mirror is the
normal case here (vllm), not the edge case:

* **Resumable.**  Every phase transition writes ``ImportJob.phase`` and
  ``ImportJob.cursor`` *before* the next phase starts, so a worker that dies
  mid-import resumes from the last committed page instead of redoing an hour of
  API traffic.  A page is processed and committed atomically; the cursor only
  moves after the commit.
* **Idempotent.**  ``repo_issues`` is keyed by the source system's id
  (``source_id``, falling back to ``number``), so running the same page twice
  updates rows instead of duplicating them.  That is what makes the resume safe
  to be slightly conservative.
* **Never silently truncated.**  ``IMPORT_MAX_ISSUES`` (default 20 000) is a
  protection, and hitting it sets ``partial`` on the job; the REST layer always
  renders that flag (see :func:`job_payload`), so an operator sees "this repo is
  a partial mirror" rather than believing 20 000 is all there is.

Everything external is injectable.  :class:`RepoImportService` takes a Forgejo
client and a commit reader; the default implementations are
:class:`ForgejoClient` (HTTP, ``requests``) and :class:`GitCommitReader` (a
shallow read-only bare copy plus the ``git`` CLI, §3.1).  Both can be
substituted, so the whole pipeline runs offline — no Forgejo, no network, no
Flask.

Configuration lives in :mod:`config.forgejo` (``FORGEJO_*`` / ``IMPORT_*`` /
``GIT_MIRROR_DIR``) and is read once, when the process builds its settings; this
module no longer looks at the environment itself, so a job cannot mirror from a
different Forgejo than the process that queued it.  The Forgejo admin token and
the webhook secret are never written to a file, a log line or a response body,
matching the "no secrets on disk" rule the model route table already follows.
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence
from urllib.parse import quote, urlparse

import requests

from config import settings
from config.forgejo import ForgejoConfig
from services.agent_runner import POLICY_RELPATH, mask_secrets
from services.git_auth import credential_args, git_env, git_host_of
from services.headers import SafeHeaderSession, checked_headers


# ── Configuration (owned by config/forgejo.py) ───────────────────────

def _cfg_default(field: str) -> Any:
    """The default :class:`ForgejoConfig` declares for *field*.

    Read off the model instead of being re-typed, so an unconfigured
    :class:`ImportConfig` means exactly what an unconfigured deployment means and
    the two cannot drift.
    """
    return ForgejoConfig.model_fields[field].default


@dataclass(frozen=True)
class ImportConfig:
    """The import pipeline's knobs, as one immutable value.

    :meth:`from_settings` is how a running deployment builds one.  The settings
    object is the only reader of the environment, so a job can no longer mirror
    from a different Forgejo than the process that queued it, and the old
    field-by-field ``number()`` parsing — which warned and silently fell back to
    a default for a value it could not use — is gone: the settings model
    validates those values once, at start-up, and refuses to start rather than
    quietly importing with a limit nobody chose.

    The class stays a plain frozen value object rather than *being* the settings
    model, because every consumer is handed one — the pipeline, the webhook route
    and the Forgejo identity exchange all take a config, and the offline gates
    inject their own.  Its field defaults are the settings' own defaults (see
    :func:`_cfg_default`), so there is one number in the tree.
    """

    base_url: str = _cfg_default("forgejo_base_url")
    git_base_url: str = _cfg_default("forgejo_git_base_url")
    admin_token: str = _cfg_default("forgejo_admin_token")
    webhook_secret: str = _cfg_default("forgejo_webhook_secret")
    owner: str = _cfg_default("forgejo_owner")
    public_base_url: str = _cfg_default("forgejo_public_base_url")
    max_issues: int = _cfg_default("import_max_issues")
    max_commits: int = _cfg_default("import_max_commits")
    max_rate: float = _cfg_default("import_max_rate")
    page_size: int = _cfg_default("import_page_size")
    poll_interval: float = _cfg_default("import_poll_interval")
    poll_attempts: int = _cfg_default("import_poll_attempts")
    http_timeout: float = _cfg_default("import_http_timeout")
    mirror_dir: str = _cfg_default("git_mirror_dir")
    #: Credential for cloning from a *remote* import source that requires
    #: authentication (see ``config.forgejo.import_source_token``).  It used to be
    #: read straight out of the environment at probe time, which is why it never
    #: appeared in this dataclass; it is a platform secret and belongs with the
    #: rest of the deployment's Forgejo configuration.
    source_token: str = _cfg_default("import_source_token")

    @classmethod
    def from_settings(cls, source: ForgejoConfig | None = None) -> "ImportConfig":
        """Build one from the deployment's Forgejo/importer settings.

        The small normalisations stay here — trailing slashes off the URLs, the
        credentials stripped of surrounding whitespace — because
        :class:`ImportConfig` is the value every consumer reads, and it has to
        mean the same thing whether it came from the settings, from a job row or
        from a gate.
        """
        config = settings.forgejo if source is None else source
        return cls(
            base_url=config.forgejo_base_url.strip().rstrip("/"),
            git_base_url=config.forgejo_git_base_url.strip().rstrip("/"),
            admin_token=config.forgejo_admin_token.strip(),
            webhook_secret=config.forgejo_webhook_secret.strip(),
            owner=config.forgejo_owner.strip(),
            public_base_url=config.forgejo_public_base_url.strip().rstrip("/"),
            max_issues=int(config.import_max_issues),
            max_commits=int(config.import_max_commits),
            max_rate=float(config.import_max_rate),
            page_size=int(config.import_page_size),
            poll_interval=float(config.import_poll_interval),
            poll_attempts=int(config.import_poll_attempts),
            http_timeout=float(config.import_http_timeout),
            mirror_dir=config.git_mirror_dir.strip(),
            source_token=config.import_source_token.strip(),
        )


#: HTTP statuses worth retrying: a 4xx is our bug, a 5xx/429 is theirs.
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})

#: Backoff schedule, mirroring ``services/upstream.py``'s retry philosophy: a
#: short, bounded retry so one flaky call does not fail a long import.
RETRY_BACKOFF_SECONDS = (0.5, 1.5, 4.0)

#: The phase order §8.2 fixes.  ``progress`` is derived from the index, so a
#: resumed job reports the same numbers a fresh one would.
JOB_PHASES: tuple[str, ...] = (
    "validate", "migrate", "poll", "mirror_issues", "index_commits", "done",
)

#: ``phase -> (floor, ceiling)`` of the 0-100 progress scale.
PHASE_BOUNDS: dict[str, tuple[int, int]] = {
    "validate": (0, 5),
    "migrate": (5, 15),
    "poll": (15, 30),
    "mirror_issues": (30, 80),
    "index_commits": (80, 95),
    "done": (95, 100),
}

TERMINAL_STATUSES = frozenset({"done", "failed", "error", "dead"})

CURSOR_VERSION = 1


# ── Errors ───────────────────────────────────────────────────────────

class RepoImportError(Exception):
    """Base class for the pipeline's domain errors."""


class SourceUrlError(RepoImportError):
    """The source URL is empty, malformed, or names no repository."""


class ForgejoError(RepoImportError):
    """A Forgejo API call failed, or answered something unusable."""


class RequiresIssuesError(RepoImportError):
    """The repo must be mirrored before issues can be read from it."""


# ── Source URL parsing ───────────────────────────────────────────────

#: Hosts whose web URL carries no extra path segment before ``owner/name``.
#: GitLab (and self-hosted clones of it) nest projects under subgroups
#: (``group/subgroup/project``), so the whole path — not just its last two
#: segments — is the repository's identity.
_KNOWN_HOSTS = {
    "github.com": "github",
    "gitee.com": "gitee",
    "gitlab.com": "gitlab",
}

_OWNER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


@dataclass(frozen=True)
class RepoSource:
    """One parsed repository URL: the full namespace plus where it came from."""

    host: str
    #: The last two path segments — what a human calls the owner and the name.
    owner: str
    name: str
    clone_url: str
    #: The **full** namespace path (``group/subgroup/project``).  Identity and
    #: every generated URL use this, so two repositories that differ only above
    #: the last segment cannot collide or be confused for each other.
    path: str = ""
    kind: str = "git"  # github | gitee | gitlab | git

    @property
    def slug(self) -> str:
        """The canonical ``repos.slug`` value — the full namespace (§4.2)."""
        return self.path or f"{self.owner}/{self.name}"

    @property
    def web_url(self) -> str:
        if not self.host:
            return self.clone_url
        return f"https://{self.host}/{self.slug}"


def _split_namespace(path: str) -> tuple[str, str, str]:
    """``(full path, owner, name)`` for a repository URL path.

    ``owner``/``name`` stay the last two segments (display, Forgejo naming), but
    the returned full path is what identifies the repository: GitLab nests
    projects under subgroups, and rebuilding a URL from only the last two
    segments would clone a different repository — or, worse, a same-named one at
    the top level.
    """
    parts = [p for p in path.split("/") if p]
    if len(parts) < 2:
        raise SourceUrlError(f"仓库地址缺少 owner/name：{path}")
    if parts[-1].endswith(".git"):
        parts[-1] = parts[-1][: -len(".git")]
    if not all(_OWNER_RE.match(segment) for segment in parts[:-1]) \
            or not _NAME_RE.match(parts[-1]):
        raise SourceUrlError(f"仓库地址中的路径段非法：{'/'.join(parts)}")
    return "/".join(parts), parts[-2], parts[-1]


def parse_source(source_url: str) -> RepoSource:
    """Parse a GitHub / Gitee / GitLab / bare https git URL.

    Accepted shapes (each optionally with a trailing ``.git`` and a ``#branch``
    fragment, which git itself allows and users paste)::

        https://github.com/vllm-project/vllm
        https://gitee.com/oschina/git-osc
        https://gitlab.com/gitlab-org/gitlab
        https://git.example.internal/team/project.git
        git@github.com:vllm-project/vllm.git          (scp-like, convenience)
        git://git.example.internal/team/project.git
        ssh://git@git.example.internal/team/project.git

    The whole path is the repository (GitLab subgroup paths included); the last
    two segments are only its owner/name label.
    """
    raw = (source_url or "").strip()
    if not raw:
        raise SourceUrlError("source_url 不能为空")
    raw = raw.split("#", 1)[0].strip()
    if not raw:
        raise SourceUrlError("source_url 不能为空")

    if "://" not in raw and "@" in raw and ":" in raw.split("@", 1)[1]:
        # scp-like: git@host:owner/name.git
        user_host, _, path = raw.partition(":")
        host = user_host.split("@", 1)[-1]
        namespace, owner, name = _split_namespace(path)
        scheme = "https"
    else:
        parsed = urlparse(raw)
        scheme = (parsed.scheme or "").lower()
        if scheme not in {"http", "https", "git", "ssh"}:
            raise SourceUrlError(f"不支持的仓库协议：{raw}（需要 https / git / ssh）")
        host = (parsed.hostname or "").lower()
        if not host:
            raise SourceUrlError(f"仓库地址缺少主机名：{raw}")
        namespace, owner, name = _split_namespace(parsed.path)

    kind = _KNOWN_HOSTS.get(host, "git")
    clone_scheme = "https" if scheme in {"http", "https", "ssh"} else scheme
    clone_url = f"{clone_scheme}://{host}/{namespace}.git"
    return RepoSource(
        host=host, owner=owner, name=name, clone_url=clone_url,
        path=namespace, kind=kind,
    )


def default_slug(source: RepoSource) -> str:
    """The ``repos.slug`` a fresh import uses."""
    return source.slug


def probe_url(source: RepoSource, *, token: str = "") -> str:
    """A cheap, read-only metadata URL for the *source* host.

    Used by phase 1 to answer "does this exist, and roughly how big is it?"
    without cloning anything.  Only the three known hosts can be probed this
    way; a bare git URL is validated by the migration itself.
    """
    if source.kind == "github":
        return f"https://api.github.com/repos/{source.slug}"
    if source.kind == "gitee":
        return f"https://gitee.com/api/v5/repos/{source.slug}"
    if source.kind == "gitlab":
        project = quote(source.slug, safe="")
        return f"https://gitlab.com/api/v4/projects/{project}"
    return ""


# ── Rate limiting (2 req/s by default, §8.2) ─────────────────────────

class RateLimiter:
    """A token-free, monotonic-clock rate limiter — one request per interval.

    Deliberately trivial: the import pipeline must not add a dependency
    (celery/redis) for what a single worker can enforce with a clock.  The
    interval comes from ``IMPORT_MAX_RATE`` (requests per second, default 2).
    """

    def __init__(self, rate: float = 2.0, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.rate = float(rate) if rate and rate > 0 else 2.0
        self._clock = clock
        self._last: float | None = None

    @property
    def interval(self) -> float:
        return 1.0 / self.rate

    def wait(self) -> float:
        """Block until the next slot is due; return the seconds slept."""
        now = self._clock()
        if self._last is not None:
            due = self._last + self.interval
            if now < due:
                time.sleep(due - now)
                now = self._clock()
        slept = 0.0 if self._last is None else max(0.0, now - self._last - self.interval)
        self._last = now
        return slept


# ── Model access ─────────────────────────────────────────────────────
# S0 owns ``models/agent_hub.py``.  The names below are the §4.2 contract; they
# are imported here so the dependency is explicit and pyflakes can see it.  A
# test that must run before S0 lands swaps the classes through
# :func:`set_models` — that seam is what keeps this module independent of the
# rest of the build.

from models.agent_hub import ImportJob, Repo, RepoCommit, RepoIssue  # noqa: E402

_MODELS: dict[str, Any] = {
    "Repo": Repo,
    "ImportJob": ImportJob,
    "RepoIssue": RepoIssue,
    "RepoCommit": RepoCommit,
}


def set_models(**models: Any) -> None:
    """Override the model classes (tests only).  Unknown keys are rejected."""
    for name, cls in models.items():
        if name not in _MODELS:
            raise KeyError(f"unknown agent-hub model {name}")
        _MODELS[name] = cls


def models() -> dict[str, Any]:
    return dict(_MODELS)


def _columns(cls: Any) -> set[str]:
    """Attribute names a mapped class actually has (SQLAlchemy 2.x)."""
    table = getattr(cls, "__table__", None)
    if table is None:
        return set()
    return {c.key for c in table.columns}


def _assign(obj: Any, values: Mapping[str, Any]) -> None:
    """Set only the columns this build of S0's model really declares.

    S0 and S1 land in parallel; the spec's table (§4.2) names columns S0 may
    have spelled slightly differently (``partial`` is the known one).  Filling
    what exists — and dropping what does not — is what lets S1 ship before S0,
    with the REST layer still reporting ``partial`` unconditionally.
    """
    available = _columns(type(obj))
    for key, value in values.items():
        if key in available:
            setattr(obj, key, value)


def _get(obj: Any, key: str, default: Any = None) -> Any:
    return getattr(obj, key, default) if hasattr(obj, key) else default


def assign_fields(obj: Any, values: Mapping[str, Any]) -> None:
    """Public spelling of :func:`_assign` — fill only existing columns.

    The REST layer creates rows too (a local workspace), and it needs the same
    tolerance for a model whose columns S0 has not finished adding.
    """
    _assign(obj, values)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _bounded(value: float, low: int = 0, high: int = 100) -> int:
    return max(low, min(high, int(value)))


def phase_progress(phase: str, *, fraction: float = 0.0) -> int:
    """Progress inside a phase, on the fixed 0-100 scale.

    Monotonic by construction: ``PHASE_BOUNDS`` only ever moves forward, and a
    completed phase reports its ceiling (not the next phase's floor), so a
    reader never sees progress go backwards when a job resumes.
    """
    low, high = PHASE_BOUNDS.get(phase, (0, 100))
    return _bounded(low + (high - low) * max(0.0, min(1.0, fraction)))


# ── Resume cursor ────────────────────────────────────────────────────

@dataclass
class Cursor:
    """The last committed position of a job (``ImportJob.cursor``)."""

    phase: str
    issue_number: int = 0
    commit_page: int = 0
    issues_seen: int = 0
    commits_seen: int = 0
    partial: bool = False
    migrate_polls: int = 0
    extras: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        payload: dict[str, Any] = {
            "v": CURSOR_VERSION,
            "phase": self.phase,
            "issue_number": self.issue_number,
            "commit_page": self.commit_page,
            "issues_seen": self.issues_seen,
            "commits_seen": self.commits_seen,
            "partial": self.partial,
            "migrate_polls": self.migrate_polls,
        }
        payload.update(self.extras)
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_json(cls, raw: str | None, *, phase: str = "validate") -> "Cursor":
        """Parse a stored cursor; anything unreadable degrades to *phase*."""
        if not raw:
            return cls(phase=phase)
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return cls(phase=phase)
        if not isinstance(data, dict) or data.get("v") != CURSOR_VERSION:
            return cls(phase=phase)
        stored = str(data.get("phase") or phase)
        if stored not in JOB_PHASES:
            stored = phase
        extras = {
            k: v for k, v in data.items()
            if k not in {
                "v", "phase", "issue_number", "commit_page",
                "issues_seen", "commits_seen", "partial", "migrate_polls",
            }
        }
        return cls(
            phase=stored,
            issue_number=int(data.get("issue_number") or 0),
            commit_page=int(data.get("commit_page") or 0),
            issues_seen=int(data.get("issues_seen") or 0),
            commits_seen=int(data.get("commits_seen") or 0),
            partial=bool(data.get("partial")),
            migrate_polls=int(data.get("migrate_polls") or 0),
            extras=extras,
        )


# ── The Forgejo HTTP client (injectable) ─────────────────────────────

@dataclass
class IssuePage:
    """One page of Forgejo issues, normalised to plain dicts."""

    items: list[dict[str, Any]] = field(default_factory=list)
    next_page: int | None = None
    next_offset: int | None = None
    total: int | None = None
    partial: bool = False


@dataclass
class CommitPage:
    """One page of commit metadata, normalised to plain dicts."""

    items: list[dict[str, Any]] = field(default_factory=list)
    next_page: int | None = None
    total: int | None = None
    partial: bool = False


class ForgejoClient:
    """Thin, retrying, rate-limited wrapper over the Forgejo HTTP API.

    The API root is ``<base_url>/api/v1`` and the admin token is carried as
    ``Authorization: token …``.  Every call raises :class:`ForgejoError` rather
    than leaking a ``requests`` exception, and 5xx/429 answers are retried with
    the same bounded backoff the artifact-hub proxies use.

    The whole class is substitutable: :class:`RepoImportService` only needs
    :meth:`trigger_migration`, :meth:`migration_state`, :meth:`stream_issues`
    and :meth:`stream_commits`, so an offline gate can pass a fake.
    """

    def __init__(
        self,
        *,
        config: ImportConfig | None = None,
        session: requests.Session | None = None,
        limiter: RateLimiter | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config or ImportConfig.from_settings()
        self.base_url = self.config.base_url
        self.timeout = self.config.http_timeout
        self.session = session or SafeHeaderSession()
        self.limiter = limiter or RateLimiter(self.config.max_rate)
        self._sleep = sleeper
        self._token = self.config.admin_token

    # -- plumbing -----------------------------------------------------

    @property
    def configured(self) -> bool:
        return bool(self.base_url)

    def _headers(self) -> dict[str, str]:
        """Request headers for one Forgejo API call.

        The token is a configured secret, so the mapping is checked against the
        header allowlist here, at the boundary, instead of in each caller.
        """
        headers = {"Accept": "application/json", "User-Agent": "openfish-agent-hub/1"}
        token = (self._token or "").strip()
        if token:
            headers["Authorization"] = f"token {token}"
        return checked_headers(headers, context="forgejo API request")

    def _api(self, path: str) -> str:
        return f"{self.base_url}/api/v1/{path.lstrip('/')}"

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Any = None,
        attempts: int = len(RETRY_BACKOFF_SECONDS) + 1,
    ) -> requests.Response:
        """One HTTP call with rate limiting and bounded retry/backoff.

        The token is never logged: only the method and the URL are.
        """
        if not self.configured:
            raise ForgejoError("FORGEJO_BASE_URL 未配置")
        url = self._api(path)
        last: Exception | None = None
        for attempt in range(1, attempts + 1):
            self.limiter.wait()
            try:
                response = self.session.request(
                    method.upper(),
                    url,
                    headers=self._headers(),
                    params=dict(params or {}),
                    json=json_body,
                    timeout=(5.0, self.timeout),
                )
            except requests.RequestException as exc:
                last = exc
            else:
                if response.status_code not in RETRY_STATUSES:
                    return response
                last = ForgejoError(
                    f"Forgejo {method} {url} → HTTP {response.status_code}"
                )
            if attempt < attempts:
                self._sleep(RETRY_BACKOFF_SECONDS[min(attempt - 1,
                                                      len(RETRY_BACKOFF_SECONDS) - 1)])
        raise ForgejoError(f"Forgejo {method} {url} 连续失败：{last}")

    def _json(self, method: str, path: str, **kwargs: Any) -> Any:
        response = self._request(method, path, **kwargs)
        if response.status_code == 404:
            raise ForgejoError(f"Forgejo 资源不存在：{method} {path}")
        if response.status_code >= 400:
            raise ForgejoError(
                f"Forgejo {method} {path} → HTTP {response.status_code}"
            )
        try:
            return response.json()
        except ValueError as exc:
            raise ForgejoError(f"Forgejo {method} {path} 返回非 JSON：{exc}") from exc

    def read_file(self, forgejo_repo: str, path: str) -> str | None:
        """The text of one file at the repository's default branch.

        ``None`` means "not there (or unreadable)", which is a normal answer: a
        repository without ``.agent/review-policy.yml`` must not be an error.
        Used to cache the per-repo review policy on the ``repos`` row, because
        the API host has no checkout to read it from.
        """
        slug = str(forgejo_repo or "").strip().strip("/")
        if not slug or "/" not in slug or not path:
            return None
        # Encode each segment, not the slashes: Forgejo's contents API routes on
        # the path (`…/contents/.agent/review-policy.yml`), so `%2F` for the
        # separator would 404.
        encoded = "/".join(quote(segment, safe="") for segment in path.split("/") if segment)
        try:
            document = self._json("GET", f"repos/{slug}/contents/{encoded}")
        except ForgejoError:
            return None
        if not isinstance(document, Mapping):
            return None
        content = document.get("content")
        if not isinstance(content, str) or not content.strip():
            return None
        try:
            # The contents API wraps base64 at 60 columns; b64decode tolerates it.
            return base64.b64decode(content).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return None

    # -- migration ----------------------------------------------------

    def trigger_migration(
        self,
        source: RepoSource,
        *,
        owner: str,
        name: str,
        include_issues: bool = True,
        include_prs: bool = True,
        include_labels: bool = True,
        include_milestones: bool = True,
        mirror: bool = True,
        auth_token: str = "",
        auth_username: str = "",
    ) -> dict[str, Any]:
        """Start a Forgejo migration and return its repo document.

        ``mirror=True`` is Forgejo's "pull mirror" flag and is what makes a
        ``kind=upstream`` repo read-only on the Forgejo side — the policy half
        of §8.1's ``mirror`` mode lives here.
        """
        body: dict[str, Any] = {
            "clone_addr": source.clone_url,
            "repo_name": name,
            "repo_owner": owner,
            "service": source.kind if source.kind != "git" else "git",
            "mirror": bool(mirror),
            "private": False,
            "description": f"openfish import of {source.slug}",
            "issues": bool(include_issues),
            "labels": bool(include_labels),
            "milestones": bool(include_milestones),
            "pull_requests": bool(include_prs),
            "wiki": False,
            "releases": False,
        }
        if auth_token:
            body["auth_token"] = auth_token
        if auth_username:
            body["auth_username"] = auth_username
        data = self._json("POST", "repos/migrate", json_body=body)
        return data if isinstance(data, dict) else {}

    def migration_state(self, forgejo_repo: str) -> dict[str, Any]:
        """The Forgejo repo document — migration completeness is read from it."""
        data = self._json("GET", f"repos/{quote(forgejo_repo, safe='/')}")
        return data if isinstance(data, dict) else {}

    @staticmethod
    def migration_finished(state: Mapping[str, Any]) -> bool:
        """Best-effort "has the migration settled?" predicate.

        Forgejo exposes no single ``migrating`` boolean on every version, so
        this reads the fields that exist and prefers the conservative answer:
        an unrecognised document is treated as *not finished* and the poll
        budget (``IMPORT_POLL_ATTEMPTS``) bounds the wait either way.
        """
        for key in ("migrating", "is_migrating"):
            if key in state:
                return not bool(state.get(key))
        status = str(state.get("migration_status") or "").lower()
        if status:
            return status in {"finished", "done", "success", "idle"}
        # Unknown shape: call it finished once the repo is readable, which it
        # must be for us to have got this document at all.
        return bool(state.get("full_name") or state.get("name"))

    # -- issues -------------------------------------------------------

    def stream_issues(
        self,
        forgejo_repo: str,
        *,
        state: str = "all",
        after_number: int = 0,
        offset: int = 0,
        per_page: int = 50,
    ) -> IssuePage:
        """One page of issues **and** pull requests, as Forgejo orders them.

        *offset* addresses the page: a resuming job must ask for the rows it has
        not stored yet, and Forgejo has no "issues after number N" parameter.
        Pairing an offset with the caller's ``after_number`` filter (which drops
        anything at or below the cursor) is what makes a resumed page exact —
        passing a *page number* here would skip everything the cursor already
        covered and then some.
        """
        size = max(1, min(per_page, 100))
        raw = self._json(
            "GET",
            f"repos/{quote(forgejo_repo, safe='/')}/issues",
            params={
                "state": state or "all",
                "type": "all",
                "sort": "created",
                "direction": "asc",
                "page": (max(0, offset) // size) + 1,
                "per_page": size,
            },
        )
        rows = raw if isinstance(raw, list) else []
        items = [self.normalize_issue(row) for row in rows if isinstance(row, dict)]
        if offset % size:
            # A partial first page (a resumed mid-page offset): drop the rows
            # already stored.  The common path is offset % size == 0.
            items = items[offset % size:]
        if after_number:
            items = [i for i in items if int(i["number"]) > after_number]
        next_offset = offset + len(items)
        more = len(rows) >= size
        return IssuePage(
            items=items,
            next_page=None,
            next_offset=next_offset if more else None,
            total=self._issue_total(forgejo_repo),
        )

    def _issue_total(self, forgejo_repo: str) -> int | None:
        """The repo's own issue/PR counters, or ``None`` when unreported."""
        try:
            state = self.migration_state(forgejo_repo)
        except ForgejoError:
            return None
        for key in ("open_issues_count", "issues_count"):
            value = state.get(key)
            if isinstance(value, int):
                return value
        return None

    @staticmethod
    def normalize_issue(row: Mapping[str, Any]) -> dict[str, Any]:
        """Flatten one Forgejo issue into the ``repo_issues`` shape.

        Handles the two live shapes (``pull_request`` object vs.
        ``pull_request`` key) and both label encodings (list of objects, list
        of strings) so the mirror is not coupled to one Forgejo version.
        """
        labels = row.get("labels") or []
        names: list[str] = []
        for label in labels:
            if isinstance(label, str):
                names.append(label)
            elif isinstance(label, Mapping) and label.get("name"):
                names.append(str(label["name"]))
        milestone = row.get("milestone")
        if isinstance(milestone, Mapping):
            milestone_name = milestone.get("title") or milestone.get("name")
        else:
            milestone_name = milestone if isinstance(milestone, str) else None
        author = row.get("user")
        if isinstance(author, Mapping):
            author_name = author.get("login") or author.get("username") or ""
        else:
            author_name = str(row.get("author") or "")
        is_pr = bool(row.get("pull_request")) or bool(row.get("is_pull_request"))
        number = int(row.get("number") or 0)
        source_id = row.get("id")
        return {
            "number": number,
            "is_pull_request": is_pr,
            "title": str(row.get("title") or ""),
            "body": str(row.get("body") or ""),
            "state": str(row.get("state") or "open"),
            "author": str(author_name),
            "labels": names,
            "milestone": str(milestone_name) if milestone_name else None,
            "created_at": row.get("created_at"),
            "updated_at": row.get("updated_at"),
            "closed_at": row.get("closed_at"),
            "url": row.get("html_url") or row.get("url"),
            "source_id": str(source_id) if source_id is not None else str(number),
        }

    # -- commits ------------------------------------------------------

    def stream_commits(
        self,
        forgejo_repo: str,
        *,
        branch: str = "main",
        page: int = 1,
        per_page: int = 50,
    ) -> CommitPage:
        """One page of commit metadata (the bounded alternative to a clone).

        Commits are immutable and only ever appended, so a *page number* is a
        stable cursor here (unlike issues, which are editable and can be
        renumbered by a migration).
        """
        size = max(1, min(per_page, 100))
        raw = self._json(
            "GET",
            f"repos/{quote(forgejo_repo, safe='/')}/commits",
            params={
                "sha": branch or "main",
                "page": max(1, page),
                "per_page": size,
                "stat": "false",
            },
        )
        rows = raw if isinstance(raw, list) else []
        items = [self.normalize_commit(row) for row in rows if isinstance(row, dict)]
        next_page = page + 1 if len(rows) >= size else None
        return CommitPage(items=items, next_page=next_page)

    @staticmethod
    def normalize_commit(row: Mapping[str, Any]) -> dict[str, Any]:
        commit = row.get("commit") if isinstance(row.get("commit"), Mapping) else {}
        author = row.get("author") if isinstance(row.get("author"), Mapping) else {}
        commit_author = commit.get("author") if isinstance(commit.get("author"), Mapping) else {}
        sha = str(row.get("sha") or commit.get("id") or (row.get("commit") if isinstance(row.get("commit"), str) else ""))
        message = str(commit.get("message") or row.get("message") or "")
        return {
            "sha": sha,
            "message": message,
            "author": str(author.get("login") or author.get("username")
                          or commit_author.get("name") or ""),
            "committed_at": (commit_author.get("date")
                             or (commit.get("committer") or {}).get("date")
                             if isinstance(commit.get("committer"), Mapping)
                             else commit_author.get("date")) or row.get("created"),
        }

    # -- probing ------------------------------------------------------

    def probe_source(self, source: RepoSource) -> dict[str, Any]:
        """Read-only reachability probe against the *source* host (§8.2 step 1).

        Anonymous for a public repository; the Forgejo admin token is not sent
        to github.com.  Returns ``{"reachable": bool, "open_issues": int|None,
        "detail": str}`` and never raises for a network failure — an
        unreachable source is a pipeline error the caller records, not a crash.
        """
        url = probe_url(source)
        if not url:
            return {"reachable": True, "open_issues": None,
                    "detail": "bare git URL — probe deferred to migration"}
        headers = {"Accept": "application/json", "User-Agent": "openfish-agent-hub/1"}
        token = self.config.source_token
        if token and source.kind != "git":
            headers["Authorization"] = f"Bearer {token}"
        headers = checked_headers(headers, context=f"source probe {url}")
        self.limiter.wait()
        try:
            response = self.session.request(
                "GET", url, headers=headers, timeout=(5.0, self.timeout)
            )
        except requests.RequestException as exc:
            return {"reachable": False, "open_issues": None, "detail": str(exc)}
        if response.status_code >= 400:
            return {"reachable": False, "open_issues": None,
                    "detail": f"HTTP {response.status_code}"}
        try:
            data = response.json()
        except ValueError:
            return {"reachable": True, "open_issues": None, "detail": "non-JSON probe"}
        open_issues = data.get("open_issues_count")
        if open_issues is None:
            open_issues = data.get("open_issues")
        return {
            "reachable": True,
            "open_issues": int(open_issues) if isinstance(open_issues, int) else None,
            "detail": str(data.get("full_name") or data.get("path_with_namespace") or ""),
        }

    # -- pull requests ------------------------------------------------

    def create_pull_request(
        self,
        forgejo_repo: str,
        *,
        head: str,
        base: str,
        title: str,
        body: str = "",
    ) -> str:
        """Open a pull request and return its web URL.

        This is the **single** seam the agent runtime opens PRs through, so the
        credential lives only in :meth:`_headers` (the admin token, from the
        environment via :class:`ImportConfig`): nothing about the token is passed
        in, returned, or logged here, which is what lets the git-credential
        refactor replace how credentials are obtained without touching callers.

        ``head`` is the source branch (``agent/*`` by the runtime's I4 guard),
        ``base`` the target branch.  Forgejo answers ``201`` with the PR object;
        a duplicate or a protected branch comes back as a typed
        :class:`ForgejoError`, never a bare ``requests`` exception.
        """
        slug = str(forgejo_repo or "").strip().strip("/")
        if not slug or "/" not in slug:
            raise ForgejoError(
                f"forgejo_repo 必须是 <owner>/<name>，得到 {forgejo_repo}"
            )
        document = self._json(
            "POST",
            f"repos/{slug}/pulls",
            json_body={
                "head": str(head),
                "base": str(base),
                "title": str(title),
                "body": str(body),
            },
        )
        if not isinstance(document, Mapping):
            raise ForgejoError("Forgejo 创建 PR 返回了非对象 JSON")
        url = str(document.get("html_url") or document.get("url") or "").strip()
        if not url:
            raise ForgejoError("Forgejo 创建 PR 未返回 URL")
        return url


# ── Commit metadata via the read-only git copy ───────────────────────
# §3.1: commits come from the Forgejo API by default, and from a shallow,
# read-only bare copy driven by the ``git`` CLI when the API cannot supply
# them.  No pygit2 / dulwich / GitPython — a subprocess and a fixed argv is the
# whole implementation, which is also why it is easy to audit.

#: ASCII unit separator: cannot appear in a commit subject or an author name,
#: so it is a safe field delimiter independent of locale.
_GIT_SEP = "\x1f"
_GIT_FORMAT = f"%H{_GIT_SEP}%an{_GIT_SEP}%aI{_GIT_SEP}%s"


class GitCommitReader:
    """Read commit metadata out of a shallow bare clone (read-only)."""

    def __init__(
        self,
        *,
        config: ImportConfig | None = None,
        git_binary: str = "git",
        runner: Callable[..., subprocess.CompletedProcess] | None = None,
    ) -> None:
        self.config = config or ImportConfig.from_settings()
        self.git = git_binary
        self._run = runner or subprocess.run

    # -- clone/fetch --------------------------------------------------

    def mirror_path(self, forgejo_repo: str) -> Path:
        # Percent-encode each path segment so the mapping is injective (``a..b``
        # and ``a_b`` are different repositories) and ``..`` can never climb out
        # of ``base``.
        segments = [
            segment for segment in forgejo_repo.split("/")
            if segment and segment not in {".", ".."}
        ]
        safe = "/".join(quote(segment, safe="") for segment in segments) or "_"
        root = self.config.mirror_dir
        base = Path(root) if root else Path("data") / "forgejo-mirror"
        return base / f"{safe}.git"

    def _git_env(self) -> dict[str, str]:
        """Environment that carries the git credential, never argv or disk.

        The token reaches git through ``OPENFISH_GIT_TOKEN`` and an inline
        ``credential.helper``, so it never appears in ``ps`` output, in an
        exception message, or in the mirror's ``.git/config`` — the three places
        a URL-embedded credential used to leak to.  No git-version floor beyond
        the credential protocol itself.
        """
        token = (self.config.admin_token or "").strip()
        base = (self.config.git_base_url or self.config.base_url).rstrip("/")
        env = {"GIT_TERMINAL_PROMPT": "0"}
        env.update(git_env(token, git_host_of(base)))
        return env

    def _git_auth_args(self) -> list[str]:
        """``-c credential.helper=…`` for a token, or nothing when unset."""
        if not (self.config.admin_token or "").strip():
            return []
        return credential_args()

    def _fetch_mirror(self, destination: Path) -> None:
        """Refresh an existing mirror, un-shallowing a legacy depth-1 copy."""
        env = self._git_env()
        try:
            self._git(
                ["fetch", "--prune", "--filter=blob:none", "--unshallow", "origin"],
                cwd=destination, extra_env=env,
            )
        except RepoImportError:
            # Already complete: ``--unshallow`` is rejected, a plain fetch works.
            self._git(
                ["fetch", "--prune", "--filter=blob:none", "origin"],
                cwd=destination, extra_env=env,
            )

    def ensure_mirror(self, forgejo_repo: str, *, refresh: bool = False) -> Path:
        """``git clone --bare --filter=blob:none`` (or ``fetch``) — never pushes.

        The clone is blobless but carries the full commit graph, because
        :meth:`stream` is asked for up to ``IMPORT_MAX_COMMITS`` commits: a
        ``--depth 1`` copy can only ever answer with one.
        """
        destination = self.mirror_path(forgejo_repo)
        destination.parent.mkdir(parents=True, exist_ok=True)
        base = (self.config.git_base_url or self.config.base_url).rstrip("/")
        clone_url = f"{base}/{forgejo_repo}.git"

        if destination.is_dir():
            # ``refresh`` must win over the readiness cache: a long-lived worker
            # that short-circuits here would never see a new commit again.
            if refresh:
                self._fetch_mirror(destination)
            return destination

        self._git(
            [
                "clone", "--bare", "--filter=blob:none",
                "--no-tags", clone_url, str(destination),
            ],
            extra_env=self._git_env(),
        )
        return destination

    def _git(
        self,
        args: Sequence[str],
        *,
        cwd: Path | None = None,
        extra_env: Mapping[str, str] | None = None,
    ) -> str:
        argv = [self.git, *self._git_auth_args(), *args]
        # The ambient environment, deliberately: this is not configuration being
        # *read*, it is the environment a git client is *given* — proxies,
        # ``GIT_SSL_CAINFO``, credential helpers and the operator's git config all
        # have to survive, and the import runs trusted platform code (unlike the
        # sandbox children, which go through services/sandbox_env.py).
        env = dict(os.environ)
        env.update(extra_env or {})
        try:
            proc = self._run(
                argv, cwd=str(cwd) if cwd else None, capture_output=True,
                text=True, timeout=600, env=env,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise RepoImportError(
                f"git 调用失败（{args[0]}）："
                f"{mask_secrets(str(exc), [self.config.admin_token])}"
            ) from exc
        if proc.returncode != 0:
            # Only ``args[0]`` is quoted: the full argv used to carry the clone
            # URL, and with it the admin token, into the error message and logs.
            detail = mask_secrets(
                (proc.stderr or "").strip()[:400], [self.config.admin_token],
            )
            raise RepoImportError(
                f"git {args[0]} 退出码 {proc.returncode}：{detail}"
            )
        return proc.stdout or ""

    def stream(
        self,
        forgejo_repo: str,
        *,
        branch: str = "main",
        limit: int = 5000,
    ) -> Iterator[dict[str, Any]]:
        """Yield up to *limit* commits, newest first, from the read-only copy."""
        destination = self.ensure_mirror(forgejo_repo)
        reference = branch if branch else "HEAD"
        log_argv = [
            "log", f"--max-count={max(1, limit)}", f"--format={_GIT_FORMAT}", reference,
        ]
        try:
            raw = self._git(log_argv, cwd=destination)
        except RepoImportError:
            # A stale ``default_branch`` must not empty the index: a bare mirror
            # always has HEAD, which is the forge's real default branch.
            if reference == "HEAD":
                raise
            raw = self._git([*log_argv[:-1], "HEAD"], cwd=destination)
        for line in raw.splitlines():
            if not line.strip():
                continue
            sha, _, rest = line.partition(_GIT_SEP)
            author, _, rest = rest.partition(_GIT_SEP)
            committed_at, _, subject = rest.partition(_GIT_SEP)
            yield {
                "sha": sha,
                "author": author,
                "committed_at": committed_at,
                "message": subject,
            }

    def close(self) -> None:
        """Drop the local copy (best effort; a failure is never fatal)."""
        if not self.config.mirror_dir:
            return
        base = Path(self.config.mirror_dir)
        if base.name.endswith("-mirror"):
            shutil.rmtree(base, ignore_errors=True)


# ── Default git branch detection ─────────────────────────────────────

DEFAULT_BRANCH = "main"


# ── The pipeline ─────────────────────────────────────────────────────

@dataclass
class ImportOutcome:
    """The result of one bounded slice of pipeline work."""

    job_id: int
    status: str            # running | done | partial | failed
    phase: str
    progress: int
    repo_slug: str
    forgejo_repo: str = ""
    issues_seen: int = 0
    commits_seen: int = 0
    partial: bool = False
    error: str | None = None
    steps: int = 0

    @property
    def finished(self) -> bool:
        return self.status != "running"

    def as_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "status": self.status,
            "phase": self.phase,
            "progress": self.progress,
            "repo": self.repo_slug,
            "forgejo_repo": self.forgejo_repo,
            "issues_seen": self.issues_seen,
            "commits_seen": self.commits_seen,
            "partial": self.partial,
            "error": self.error,
        }


class RepoImportService:
    """Drive one import job from ``pending`` to ``done`` (or ``partial``).

    The session is passed in, not imported from ``extensions.database``: the
    pipeline then runs identically in a Flask request, in a worker process and
    in the offline gate, which is what makes the resume behaviour testable
    without a server.
    """

    def __init__(
        self,
        session: Any,
        *,
        client: Any | None = None,
        commits: Any | None = None,
        config: ImportConfig | None = None,
        queue: Callable[..., Any] | None = None,
        artifact_root: str | None = None,
    ) -> None:
        self.session = session
        self.config = config or ImportConfig.from_settings()
        self.client = client if client is not None else ForgejoClient(config=self.config)
        self.commits = commits if commits is not None else GitCommitReader(config=self.config)
        self.queue = queue
        self.artifact_root = artifact_root or self.config.mirror_dir

    # ── model access ─────────────────────────────────────────────────

    @property
    def Repo(self) -> Any:
        return _MODELS["Repo"]

    @property
    def ImportJob(self) -> Any:
        return _MODELS["ImportJob"]

    @property
    def RepoIssue(self) -> Any:
        return _MODELS["RepoIssue"]

    @property
    def RepoCommit(self) -> Any:
        return _MODELS["RepoCommit"]

    def _commit(self) -> None:
        self.session.commit()

    # ── lookups ──────────────────────────────────────────────────────

    def repo_by_slug(self, slug: str) -> Any | None:
        return (
            self.session.query(self.Repo)
            .filter(self.Repo.slug == slug)
            .one_or_none()
        )

    def job(self, job_id: int) -> Any | None:
        return self.session.get(self.ImportJob, job_id)

    # ── job creation ─────────────────────────────────────────────────

    def create_job(
        self,
        source_url: str,
        *,
        mode: str = "code+issues",
        repo_id: int | None = None,
        include_prs: bool = True,
        skip_to: str | None = None,
    ) -> Any:
        """Create a queued ``ImportJob`` (and its ``Repo`` when new).

        Returns the job.  The heavy lifting is deliberately *not* here: this
        runs inside the request that answered ``POST /api/v1/repos/import`` and
        must return immediately.  The worker (or ``advance`` in the offline
        gate) drives :meth:`run`.

        *skip_to* starts a job further down the pipeline — ``POST
        /api/v1/repos/<slug>/sync`` uses ``"mirror_issues"`` to re-mirror an
        existing Forgejo repo without migrating it a second time.  It is only
        honoured for a repo that already has a ``forgejo_repo``.
        """
        source = parse_source(source_url)
        if mode not in {"code", "code+issues", "issues"}:
            raise SourceUrlError(f"不支持的导入模式：{mode}")
        if skip_to is not None and skip_to not in JOB_PHASES:
            raise SourceUrlError(f"不支持的起始阶段：{skip_to}")

        repo = self.session.get(self.Repo, repo_id) if repo_id else None
        if repo is None:
            repo = self.repo_by_slug(source.slug)
        if repo is None:
            repo = self.Repo()
            _assign(repo, {
                "slug": source.slug,
                "source": "import",
                "kind": "upstream",
                "sync_state": "pending",
                "issue_count": 0,
                "commit_count": 0,
            })
            self.session.add(repo)
            self.session.flush()
        _assign(repo, {
            "source_url": source.web_url,
            "default_branch": _get(repo, "default_branch") or DEFAULT_BRANCH,
            "sync_state": "pending",
            "updated_at": _now(),
        })

        # An incremental sync resumes *after* the migration: it needs the
        # Forgejo repo the first import created, not a second clone of the same
        # upstream.  The cursor therefore carries both the phase and the name.
        start_phase = "validate"
        extras: dict[str, Any] = {}
        if skip_to and skip_to != "validate":
            forgejo_repo = str(_get(repo, "forgejo_repo") or "")
            if forgejo_repo:
                start_phase = skip_to
                extras["forgejo_repo"] = forgejo_repo
                extras["include_issues"] = mode in {"code+issues", "issues"}
                extras["include_prs"] = bool(include_prs)

        job = self.ImportJob()
        _assign(job, {
            "repo_id": repo.id,
            "mode": mode,
            "status": "queued",
            "phase": start_phase,
            "progress": 0,
            "total": 0,
            "done": 0,
            "cursor": Cursor(phase=start_phase, extras=extras).to_json(),
            "include_prs": bool(include_prs),
            "partial": False,
            "error": None,
            "started_at": None,
            "finished_at": None,
        })
        self.session.add(job)
        self._commit()
        return job

    # ── execution ────────────────────────────────────────────────────

    def run(self, job: Any, *, max_steps: int = 1_000_000) -> ImportOutcome:
        """Advance *job* until it finishes or *max_steps* pages are consumed.

        Each step is one unit of real work (a phase transition, or one page of
        issues/commits), and every step commits, so ``max_steps=1`` is the "do
        one unit then stop" primitive the resume test drives.  *Waiting* is not
        a step: the migration poll is bounded by ``IMPORT_POLL_ATTEMPTS``
        instead, which is why a bounded-steps caller cannot starve a slow
        migration.
        """
        cursor = Cursor.from_json(_get(job, "cursor"), phase=str(_get(job, "phase") or "validate"))
        steps = 0
        _assign(job, {"status": "running"})
        if not _get(job, "started_at"):
            _assign(job, {"started_at": _now()})
        if _get(job, "partial"):
            cursor.partial = True
        self._commit()

        try:
            idles = 0
            # The poll budget bounds the wait; the error raised one poll past it
            # is the safety net for a caller that ignores the break.
            idle_budget = max(1, self.config.poll_attempts)
            while steps < max_steps:
                phase = cursor.phase
                if phase == "done":
                    return self._finish(job, cursor, steps)
                handler = getattr(self, f"_step_{phase}", None)
                if handler is None:
                    raise RepoImportError(f"未知的导入阶段：{phase}")
                moved = handler(job, cursor)
                if moved:
                    steps += 1
                    idles = 0
                else:
                    # A handler that made no forward progress is *waiting* (the
                    # migration poll).  Waiting is bounded by the poll budget,
                    # never by `max_steps`: a bounded-steps caller would
                    # otherwise finish its budget before a slow migration did.
                    idles += 1
                self._write(job, cursor)
                if cursor.phase == "done":
                    return self._finish(job, cursor, steps)
                if idles >= idle_budget:
                    break
        except RepoImportError as exc:
            return self._fail(job, cursor, exc, steps)
        except Exception as exc:  # noqa: BLE001 - a pipeline step must not 500 a worker
            return self._fail(job, cursor, exc, steps)

        return ImportOutcome(
            job_id=int(_get(job, "id") or 0),
            status="running",
            phase=cursor.phase,
            progress=int(_get(job, "progress") or 0),
            repo_slug=self._slug_of(job),
            forgejo_repo=str(_get(job, "forgejo_repo") or ""),
            issues_seen=cursor.issues_seen,
            commits_seen=cursor.commits_seen,
            partial=cursor.partial,
            steps=steps,
        )

    def run_job_id(self, job_id: int, *, max_steps: int = 1_000_000) -> ImportOutcome:
        job = self.job(job_id)
        if job is None:
            raise RepoImportError(f"ImportJob {job_id} 不存在")
        return self.run(job, max_steps=max_steps)

    def advance(self, job: Any, *, max_steps: int = 1_000_000) -> ImportOutcome:
        """Alias for :meth:`run` — the worker loop's entry point."""
        return self.run(job, max_steps=max_steps)

    # ── phases ───────────────────────────────────────────────────────

    def _step_validate(self, job: Any, cursor: Cursor) -> bool:
        repo = self.session.get(self.Repo, _get(job, "repo_id"))
        if repo is None:
            raise RepoImportError("ImportJob 指向的仓库不存在")
        source_url = _get(repo, "source_url") or _get(job, "source_url") or ""
        source = parse_source(source_url)
        probe: dict[str, Any] = {}
        try:
            probe = self.client.probe_source(source)
        except Exception as exc:  # noqa: BLE001 - a probe failure is recorded, not fatal
            probe = {"reachable": False, "open_issues": None, "detail": str(exc)}
        _assign(repo, {
            "source_url": source.web_url,
            "default_branch": _get(repo, "default_branch") or DEFAULT_BRANCH,
            "sync_state": "pending",
            "updated_at": _now(),
        })
        cursor.extras["source_url"] = source.web_url
        cursor.extras["source_kind"] = source.kind
        cursor.extras["probe_reachable"] = bool(probe.get("reachable"))
        cursor.extras["probe_detail"] = str(probe.get("detail") or "")
        cursor.phase = "migrate"
        return True

    def _step_migrate(self, job: Any, cursor: Cursor) -> bool:
        repo = self.session.get(self.Repo, _get(job, "repo_id"))
        if repo is None:
            raise RepoImportError("ImportJob 指向的仓库不存在")
        source = parse_source(_get(repo, "source_url") or cursor.extras.get("source_url", ""))
        owner = self.config.owner
        name = self._forgejo_name(source)
        mode = str(_get(job, "mode") or "code+issues")
        include_issues = mode in {"code+issues", "issues"}
        include_prs = bool(_get(job, "include_prs", True)) and include_issues
        _assign(repo, {"sync_state": "cloning", "updated_at": _now()})

        existing = _get(repo, "forgejo_repo")
        if existing:
            forgejo_repo = str(existing)
        else:
            document = self.client.trigger_migration(
                source,
                owner=owner,
                name=name,
                include_issues=include_issues,
                include_prs=include_prs,
                mirror=str(_get(repo, "kind") or "upstream") == "upstream",
            )
            forgejo_repo = str(
                document.get("full_name") or f"{owner}/{document.get('name') or name}"
            )
            _assign(repo, {"forgejo_repo": forgejo_repo, "updated_at": _now()})
        cursor.extras["forgejo_repo"] = forgejo_repo
        cursor.extras["include_issues"] = include_issues
        cursor.extras["include_prs"] = include_prs
        self._mirror_state(job, cursor, forgejo_repo=forgejo_repo)
        cursor.phase = "poll"
        return True

    def _step_poll(self, job: Any, cursor: Cursor) -> bool:
        forgejo_repo = str(cursor.extras.get("forgejo_repo") or _get(job, "forgejo_repo") or "")
        if not forgejo_repo:
            raise RepoImportError("migrate 阶段没有落 forgejo_repo")
        cursor.migrate_polls += 1
        if cursor.migrate_polls > self.config.poll_attempts:
            raise ForgejoError(
                f"Forgejo migration 未在 {self.config.poll_attempts} 次轮询内完成：{forgejo_repo}"
            )
        state = self.client.migration_state(forgejo_repo)
        finished = bool(self.client.migration_finished(state))
        if not finished:
            self._mirror_state(job, cursor, forgejo_repo=forgejo_repo)
            return False
        self._mirror_state(job, cursor, forgejo_repo=forgejo_repo)
        cursor.phase = "mirror_issues"
        return True

    def _refresh_repo_policy(self, repo: Any, forgejo_repo: str) -> None:
        """Cache the repository's review-policy trigger fields on the repo row.

        The webhook runs on the API host, which has no checkout, so it cannot
        read ``.agent/review-policy.yml`` itself.  Reading it once per job and
        caching the fields that drive enqueueing — ``auto_review``, ``curator``
        and the curator cooling-off window — is what makes a per-repo policy
        real, and it keeps every reader on one source.

        Trust boundary (I6): the file is repository **content**.  For a
        ``workspace`` — a platform-owned repository users push to — honouring it
        is the documented feature.  An ``upstream`` mirror's copy was written by
        a third party, so it must not decide what the platform does: those rows
        are left on the platform defaults.
        """
        if str(_get(repo, "kind") or "upstream") != "workspace":
            self._clear_repo_policy(repo)
            return
        from services.review_policy import (  # noqa: PLC0415 - optional layer
            SOURCE_FILE,
            builtin_default,
            parse_document,
            parse_yaml,
        )

        defaults = builtin_default().defaults
        try:
            text = self.client.read_file(forgejo_repo, str(POLICY_RELPATH).replace("\\", "/"))
        except Exception as exc:  # noqa: BLE001 - policy I/O never fails an import
            text = ""
        if text and text.strip():
            try:
                defaults = parse_document(parse_yaml(text), source=SOURCE_FILE).defaults
            except Exception as exc:  # noqa: BLE001
                pass
        values: dict[str, Any] = {
            "auto_review": bool(defaults.auto_review),
            "curator": str(defaults.curator),
            "curator_min_interval_seconds": int(defaults.curator_min_interval_seconds),
        }
        if any(_get(repo, name) != value for name, value in values.items()):
            _assign(repo, {**values, "updated_at": _now()})

    def _clear_repo_policy(self, repo: Any) -> None:
        """Drop a cached policy so the row falls back to the platform defaults.

        Whatever an imported mirror's ``.agent/review-policy.yml`` once said must
        not survive as a platform decision (I6); ``None`` is the webhook's
        documented "not read" state.
        """
        names = ("auto_review", "curator", "curator_min_interval_seconds")
        if any(_get(repo, name) is not None for name in names):
            _assign(repo, {name: None for name in names} | {"updated_at": _now()})

    def _step_mirror_issues(self, job: Any, cursor: Cursor) -> bool:
        if not bool(cursor.extras.get("include_issues", True)):
            cursor.phase = "index_commits"
            return True
        repo = self.session.get(self.Repo, _get(job, "repo_id"))
        if repo is None:
            raise RepoImportError("ImportJob 指向的仓库不存在")
        forgejo_repo = str(cursor.extras.get("forgejo_repo") or _get(repo, "forgejo_repo") or "")
        if not forgejo_repo:
            raise ForgejoError("仓库没有 Forgejo 侧全名，无法镜像 issue")
        _assign(repo, {"sync_state": "issues", "updated_at": _now()})

        after = cursor.issue_number
        # The offset is the number of *source rows* already consumed, not the
        # number stored: with `include_prs=false` a page's PRs are skipped, and
        # asking Forgejo for the next offset based on stored rows would re-read
        # them forever.
        offset = cursor.issues_seen
        page_result = self.client.stream_issues(
            forgejo_repo,
            state="all",
            after_number=after,
            offset=offset,
            per_page=self.config.page_size,
        )
        rows = list(getattr(page_result, "items", []) or [])
        included = [
            row for row in rows
            if bool(cursor.extras.get("include_prs", True)) or not row.get("is_pull_request")
        ]

        next_offset = getattr(page_result, "next_offset", None)
        short_page = next_offset is None
        stored = cursor.extras.get("issues_stored", cursor.issues_seen)

        room = max(0, self.config.max_issues - stored)
        truncated = len(included) > room
        if truncated:
            included = included[:room]
            cursor.partial = True
        # The rows are committed by ``_upsert_issues``; the cursor is advanced
        # here and written immediately after.  If the worker dies between the
        # two, the resume re-reads one committed page — an idempotent update —
        # rather than skipping one, which is the only safe direction to err in.
        written = self._upsert_issues(repo, included) if included else 0
        stored += written
        consumed = len(rows) if truncated else (next_offset or 0)
        cursor.issues_seen = max(cursor.issues_seen, consumed or len(rows))
        cursor.extras["issues_stored"] = stored
        if included:
            cursor.issue_number = max(
                cursor.issue_number, max(int(row["number"]) for row in included)
            )
        self._mirror_state(
            job, cursor, forgejo_repo=forgejo_repo,
            total=self.config.max_issues if cursor.partial else None,
            done=stored,
        )
        if truncated:
            cursor.phase = "index_commits"
        elif short_page:
            cursor.phase = "index_commits"
        return True

    def _step_index_commits(self, job: Any, cursor: Cursor) -> bool:
        repo = self.session.get(self.Repo, _get(job, "repo_id"))
        if repo is None:
            raise RepoImportError("ImportJob 指向的仓库不存在")
        _assign(repo, {"sync_state": "indexing", "updated_at": _now()})
        forgejo_repo = str(cursor.extras.get("forgejo_repo") or _get(repo, "forgejo_repo") or "")
        branch = str(_get(repo, "default_branch") or DEFAULT_BRANCH)

        # Read the review policy once per job (this step runs page by page, and a
        # fetch per page would be N redundant API calls).
        if forgejo_repo and not cursor.extras.get("policy_read"):
            self._refresh_repo_policy(repo, forgejo_repo)
            cursor.extras["policy_read"] = True

        room = max(0, self.config.max_commits - cursor.commits_seen)
        page = max(1, cursor.commit_page)
        try:
            page_result = self.client.stream_commits(
                forgejo_repo, branch=branch, page=page, per_page=self.config.page_size,
            )
        except Exception as exc:  # noqa: BLE001 - the API is optional, git is not absent
            page_result = None
        rows = list(getattr(page_result, "items", []) or []) if page_result else []
        source = "api"
        fell_back = False
        if not rows and cursor.extras.get("commit_source") != "git":
            # The API answered (or failed) with nothing.  Try the read-only
            # local copy exactly once, and remember that we did: an empty
            # repository must not loop the fallback forever.  Ask for one more
            # than there is room for, so an exact hit is how the ceiling is seen.
            source = "git"
            fell_back = True
            cursor.extras["commit_source"] = "git"
            try:
                rows = list(self.commits.stream(
                    forgejo_repo, branch=branch, limit=room + 1,
                ))
            except RepoImportError as exc:
                # Both sources failed.  Finishing here would report a repository
                # with no history as "done"; fail so the job is retried instead.
                raise RepoImportError(
                    f"commit API 与 git 回退都无法读取 {forgejo_repo}：{exc}"
                ) from exc

        truncated = len(rows) > room
        if truncated:
            rows = rows[:room]
            cursor.partial = True
        if rows:
            cursor.commits_seen += self._upsert_commits(repo, rows, source=source)
        cursor.commit_page = page + 1
        next_page = bool(getattr(page_result, "next_page", None)) if page_result else False
        # A fallback page has no pagination: whatever `git log --max-count`
        # returned is the whole answer.
        done = (not next_page) or truncated or fell_back \
            or cursor.commits_seen >= self.config.max_commits
        if done and next_page and not truncated:
            # The forge says another page exists, so the commit ceiling — not the
            # end of history — is what ended the walk.  Completing the job here
            # without ``partial`` would present a truncated history as complete.
            truncated = True
            cursor.partial = True
        if done:
            _assign(repo, {
                "commit_count": cursor.commits_seen,
                "updated_at": _now(),
            })
            cursor.phase = "done"
        self._mirror_state(job, cursor, forgejo_repo=forgejo_repo, done=cursor.commits_seen)
        return True

    # ── persistence helpers ──────────────────────────────────────────

    def _forgejo_name(self, source: RepoSource) -> str:
        """The Forgejo-side repository name: the full namespace, ``__``-joined.

        Built from ``source.slug`` (not ``owner``/``name``) so two GitLab
        subgroups that share a project name get different Forgejo repositories.
        """
        return source.slug.replace("/", "__")

    def _slug_of(self, job: Any) -> str:
        repo = self.session.get(self.Repo, _get(job, "repo_id"))
        return str(_get(repo, "slug") or "")

    def _mirror_state(
        self,
        job: Any,
        cursor: Cursor,
        *,
        forgejo_repo: str = "",
        total: int | None = None,
        done: int | None = None,
    ) -> None:
        """Write ``phase``/``progress``/``cursor`` (and counters) for *job*."""
        fraction = self._fraction(job, cursor)
        values: dict[str, Any] = {
            "phase": cursor.phase,
            "progress": phase_progress(cursor.phase, fraction=fraction),
            "cursor": cursor.to_json(),
            "partial": bool(cursor.partial),
            "status": "running",
        }
        if forgejo_repo:
            values["forgejo_repo"] = forgejo_repo
        if total is not None:
            values["total"] = total
        if done is not None:
            values["done"] = done
        _assign(job, values)
        self._commit()

    def _write(self, job: Any, cursor: Cursor) -> None:
        self._mirror_state(job, cursor)

    def _fraction(self, job: Any, cursor: Cursor) -> float:
        """How far through the *current* phase the job is (0.0–1.0)."""
        if cursor.phase == "poll":
            return min(1.0, cursor.migrate_polls / max(1, self.config.poll_attempts))
        if cursor.phase == "mirror_issues":
            total = _get(job, "total") or 0
            if total:
                return min(1.0, cursor.issues_seen / total)
            return 0.0
        if cursor.phase == "index_commits":
            total = _get(job, "total") or 0
            if total:
                return min(1.0, cursor.commits_seen / total)
            return min(1.0, cursor.commits_seen / max(1, self.config.max_commits))
        return 0.0

    def _finish(self, job: Any, cursor: Cursor, steps: int) -> ImportOutcome:
        repo = self.session.get(self.Repo, _get(job, "repo_id"))
        counts = self._counts(repo)
        stored = int(cursor.extras.get("issues_stored", cursor.issues_seen) or 0)
        cursor.phase = "done"
        _assign(repo, {
            "sync_state": "ready",
            "synced_at": _now(),
            "updated_at": _now(),
            **counts,
        })
        _assign(job, {
            "phase": "done",
            "progress": 100,
            "status": "done",
            "done": stored,
            "cursor": cursor.to_json(),
            "partial": bool(cursor.partial),
            "finished_at": _now(),
        })
        if not _columns(type(job)) & {"partial"}:
            # S0 has not shipped the column yet.  The flag is still surfaced
            # (derived) by the REST layer, and this note makes the truncation
            # visible to anyone reading the row.
            note = "partial mirror: issue ceiling reached"
            if note not in str(_get(job, "error") or ""):
                _assign(job, {"error": note})
        self._commit()
        status = "partial" if cursor.partial else "done"
        return ImportOutcome(
            job_id=int(_get(job, "id") or 0),
            status=status,
            phase="done",
            progress=100,
            repo_slug=str(_get(repo, "slug") or ""),
            forgejo_repo=str(_get(repo, "forgejo_repo") or ""),
            issues_seen=stored,
            commits_seen=cursor.commits_seen,
            partial=cursor.partial,
            error=_get(job, "error"),
            steps=steps,
        )

    def _fail(
        self,
        job: Any,
        cursor: Cursor,
        exc: BaseException,
        steps: int,
    ) -> ImportOutcome:
        message = f"{type(exc).__name__}: {exc}"
        repo = self.session.get(self.Repo, _get(job, "repo_id"))
        _assign(repo, {"sync_state": "error", "updated_at": _now()})
        _assign(job, {
            "status": "failed",
            "error": message[:2000],
            "progress": int(_get(job, "progress") or phase_progress(cursor.phase)),
            "cursor": cursor.to_json(),
            "partial": bool(cursor.partial),
            "finished_at": _now(),
        })
        try:
            self._commit()
        except Exception:  # noqa: BLE001 - never mask the original failure
            self.session.rollback()
        return ImportOutcome(
            job_id=int(_get(job, "id") or 0),
            status="failed",
            phase=cursor.phase,
            progress=int(_get(job, "progress") or 0),
            repo_slug=str(_get(repo, "slug") or ""),
            forgejo_repo=str(_get(job, "forgejo_repo") or ""),
            issues_seen=cursor.issues_seen,
            commits_seen=cursor.commits_seen,
            partial=cursor.partial,
            error=message,
            steps=steps,
        )

    # ── upserts (the idempotency contract) ───────────────────────────

    def _upsert_issues(self, repo: Any, rows: Sequence[Mapping[str, Any]]) -> int:
        """Idempotent upsert keyed by ``(repo_id, source_id)``.

        The key falls back to ``number`` when the source system gave no id, and
        an existing row is *updated* — never duplicated.  Running the same page
        twice is therefore a no-op on the second pass, which is exactly what
        the resumable cursor relies on.
        """
        written = 0
        for row in rows:
            key = str(row.get("source_id") or row.get("number") or "").strip()
            number = int(row.get("number") or 0)
            existing = None
            if key:
                existing = (
                    self.session.query(self.RepoIssue)
                    .filter(self.RepoIssue.repo_id == repo.id,
                            self.RepoIssue.source_id == key)
                    .one_or_none()
                )
            if existing is None and number:
                existing = (
                    self.session.query(self.RepoIssue)
                    .filter(self.RepoIssue.repo_id == repo.id,
                            self.RepoIssue.number == number)
                    .one_or_none()
                )
            target = existing if existing is not None else self.RepoIssue()
            if existing is None:
                _assign(target, {"repo_id": repo.id, "created_at": _now()})
            _assign(target, {
                "number": number,
                "is_pull_request": bool(row.get("is_pull_request")),
                "title": str(row.get("title") or ""),
                "body": str(row.get("body") or ""),
                "state": str(row.get("state") or "open"),
                "author": str(row.get("author") or ""),
                "labels": json.dumps(list(row.get("labels") or []), ensure_ascii=False),
                "milestone": row.get("milestone"),
                "closed_at": _parse_time(row.get("closed_at")),
                "url": row.get("url"),
                "source_id": key or str(number),
                "updated_at": _parse_time(row.get("updated_at")) or _now(),
            })
            if existing is None:
                _assign(target, {"created_at": _parse_time(row.get("created_at")) or _now(),
                                 "imported_at": _now()})
                self.session.add(target)
            written += 1
        self._commit()
        return written

    def _upsert_commits(
        self,
        repo: Any,
        rows: Sequence[Mapping[str, Any]],
        *,
        source: str,
    ) -> int:
        """Idempotent upsert keyed by ``(repo_id, sha)``.

        Only commit *metadata* is stored.  File paths belong to whichever later
        slice consumes them; no line number, timestamp or textual digest is
        part of any identity key here (§4.4 forbids that for fingerprints, and
        the same discipline keeps this table re-importable).
        """
        written = 0
        for row in rows:
            sha = str(row.get("sha") or "").strip()
            if not sha:
                continue
            existing = (
                self.session.query(self.RepoCommit)
                .filter(self.RepoCommit.repo_id == repo.id, self.RepoCommit.sha == sha)
                .one_or_none()
            )
            target = existing if existing is not None else self.RepoCommit()
            if existing is None:
                _assign(target, {"repo_id": repo.id})
            _assign(target, {
                "sha": sha,
                "message": str(row.get("message") or ""),
                "author": str(row.get("author") or ""),
                "committed_at": _parse_time(row.get("committed_at")) or _now(),
                "source": source,
                "updated_at": _now(),
            })
            if existing is None:
                _assign(target, {"created_at": _now(), "imported_at": _now()})
                self.session.add(target)
            written += 1
        self._commit()
        return written

    def _counts(self, repo: Any) -> dict[str, Any]:
        issue_count = (
            self.session.query(self.RepoIssue)
            .filter(self.RepoIssue.repo_id == repo.id)
            .count()
        )
        commit_count = (
            self.session.query(self.RepoCommit)
            .filter(self.RepoCommit.repo_id == repo.id)
            .count()
        )
        return {"issue_count": issue_count, "commit_count": commit_count}


# ── Time parsing ─────────────────────────────────────────────────────

def _parse_time(value: Any) -> datetime | None:
    """Parse the RFC 3339 timestamps Forgejo emits; ``None`` when unusable."""
    if isinstance(value, datetime):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    candidate = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


# ── Reading state back (used by the REST layer) ──────────────────────

def partial_of(job: Any) -> bool:
    """Whether *job* is a truncated mirror.

    Reads the ``partial`` column when S0 ships it and the cursor/error note
    otherwise, so the REST contract is the same either way.
    """
    if hasattr(job, "partial") and getattr(job, "partial") is not None:
        return bool(getattr(job, "partial"))
    if _get(job, "partial"):
        return True
    note = str(_get(job, "error") or "")
    if "partial mirror" in note:
        return True
    try:
        cursor = Cursor.from_json(_get(job, "cursor"))
    except Exception:  # noqa: BLE001 - a corrupt cursor must not 500 a GET
        return False
    return bool(cursor.partial)


def job_payload(job: Any, *, repo_slug: str | None = None) -> dict[str, Any]:
    """The progress document ``GET /api/v1/imports/<job_id>`` returns.

    ``partial``, ``truncated`` and ``max_issues`` are always present.  §8.2 is
    explicit that a ceiling must never be a silent truncation, so the flag is
    part of the contract rather than an optional extra.
    """
    config = ImportConfig.from_settings()
    partial = partial_of(job)
    finished = _get(job, "finished_at")
    started = _get(job, "started_at")
    return {
        "job_id": int(_get(job, "id") or 0),
        "repo_id": _get(job, "repo_id"),
        "repo": repo_slug,
        "mode": str(_get(job, "mode") or ""),
        "status": str(_get(job, "status") or "queued"),
        "phase": str(_get(job, "phase") or "validate"),
        "progress": int(_get(job, "progress") or 0),
        "total": int(_get(job, "total") or 0),
        "done": int(_get(job, "done") or 0),
        "cursor": _get(job, "cursor"),
        "error": _get(job, "error"),
        "partial": partial,
        "truncated": partial,
        "max_issues": config.max_issues,
        "max_commits": config.max_commits,
        "started_at": _iso(started),
        "finished_at": _iso(finished),
    }


def _iso(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value) if value else None


def read_policy_auto_review(repo_root: str | Path | None = None) -> bool:
    """Whether a push to the default branch should queue a review.

    Reads the cached/parsed policy when ``services.review_policy`` (S3) exists
    and falls back to ``True`` otherwise.  Kept here, next to the pipeline,
    because the webhook's decision is not a policy-*parsing* concern.
    """
    if repo_root is not None:
        candidate = Path(repo_root) / ".agent" / "review-policy.yml"
        if candidate.is_file():
            return _scan_auto_review(candidate)
    try:
        from services import review_policy  # noqa: PLC0415 - optional until S3 lands
    except ImportError:
        return True
    for name in ("auto_review_enabled", "load_auto_review", "auto_review"):
        reader = getattr(review_policy, name, None)
        if not callable(reader):
            continue
        for call in (lambda: reader(), lambda: reader(repo_root),
                     lambda: reader(repo_root=repo_root)):
            try:
                value = call()
            except TypeError:
                continue
            except Exception as exc:  # noqa: BLE001 - policy trouble never blocks a webhook
                return True
            if value is None:
                break
            if isinstance(value, Mapping):
                value = value.get("auto_review", True)
            return bool(value)
    return True


def _scan_auto_review(path: Path) -> bool:
    """A minimal ``auto_review:`` scan, used only when S3's parser is absent."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return True
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("auto_review:"):
            return stripped.split(":", 1)[1].strip().lower() not in {"false", "no", "0"}
    return True


def fingerprint_source_paths(
    forgejo_repo: str,
    *,
    config: ImportConfig | None = None,
) -> list[str]:
    """File paths touched by a repository's commits, for the S3 fingerprint.

    **S1 does not build fingerprints** (§4.4 is S3's) — this helper exists so
    the boundary is explicit: it hands over *paths only*, never a line number,
    a timestamp or a digest, because any of those in a fingerprint would break
    the cross-commit deduplication the whole design rests on.
    """
    import_config = config or ImportConfig.from_settings()
    reader = GitCommitReader(config=import_config)
    mirror = reader.ensure_mirror(forgejo_repo)
    raw = reader._git(["log", "--name-only", "--format=", "HEAD"], cwd=mirror)
    return sorted({line.strip() for line in raw.splitlines() if line.strip()})


__all__ = [
    "CURSOR_VERSION",
    "DEFAULT_BRANCH",
    "JOB_PHASES",
    "PHASE_BOUNDS",
    "RETRY_BACKOFF_SECONDS",
    "RETRY_STATUSES",
    "TERMINAL_STATUSES",
    "CommitPage",
    "Cursor",
    "ForgejoClient",
    "ForgejoError",
    "GitCommitReader",
    "ImportConfig",
    "ImportOutcome",
    "IssuePage",
    "RateLimiter",
    "RepoImportError",
    "RepoImportService",
    "RepoSource",
    "RequiresIssuesError",
    "SourceUrlError",
    "assign_fields",
    "default_slug",
    "fingerprint_source_paths",
    "job_payload",
    "models",
    "parse_source",
    "partial_of",
    "phase_progress",
    "probe_url",
    "read_policy_auto_review",
    "set_models",
]
