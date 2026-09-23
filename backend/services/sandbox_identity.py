"""The uid/gid untrusted repository code is dropped to inside the runner.

The runner container used to hold a single uid: the worker that owns the git
credential also executed the repository's own ``check_*.py`` (through
``services.gates``) and the headless review command (through
``services.agent_worker.build_review_fn``).  Untrusted code could therefore read
``/proc/<worker_pid>/environ`` and walk off with ``FORGEJO_RUNNER_TOKEN`` — the
allowlist in :mod:`services.sandbox_env` never entered into it, because the child
was already inside the trust boundary.

This module is the one place that answers "who may the untrusted child be?" and
"what may it touch?", so the gate executor and the review runner cannot answer
differently:

* **Drop, don't hide.**  The child is spawned with ``user``/``group`` set to the
  configured sandbox identity (``10002:10000`` in the runner image).  The worker
  keeps its own uid and credential; git therefore stays the worker's uid because
  *it* carries the token.
* **The env pair is the switch.**  ``AGENT_SANDBOX_UID`` and
  ``AGENT_SANDBOX_GID`` must be set *together*.  Neither set is dev/test mode
  and every helper is a no-op, so offline gates and unit tests are unchanged.
  One set, or an unparseable one, raises :class:`SandboxIdentityError` — never a
  silent "run as the trusted uid".
* **The work tree stays the worker's.**  The worker still owns every entry (git
  must not report dubious ownership), so :func:`prepare_untrusted_workdir`
  changes only the *group* (chgrp to the sandbox gid, never chown), adds the
  group bits the sandbox needs, and sets the setgid bit on directories so
  entries the worker creates later inherit the shared group.
* **Nothing the sandbox can replace is ever chmod'ed through.**  A work
  directory is group-writable by design, so its owner could replace any entry
  with a symlink; a worker following that link with ``chmod`` would hand the
  sandbox write access to a worker-owned file.  The sandbox ``HOME`` is
  therefore a worker-owned ``tempfile.mkdtemp`` directory **outside the
  checkout**, made accessible with ``os.fchown``/``os.fchmod`` on a
  ``O_NOFOLLOW`` file descriptor — never by path.  A symlinked work directory is
  refused outright.
* **Fail closed without the capability.**  If an identity is configured but the
  process cannot ``setuid``/``setgid`` (CapEff lacks ``CAP_SETUID`` /
  ``CAP_SETGID`` and the euid is not 0), :func:`untrusted_popen_kwargs` raises
  rather than quietly spawning beside the worker's credential.
"""

from __future__ import annotations

import os
import stat
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from config import settings
from config.agent import AgentConfig


# ── Configuration ────────────────────────────────────────────────────
# The names are derived from :mod:`config.agent`, which owns these variables, so
# a message or a log line cannot name one the settings no longer read.

#: Environment variable naming the uid untrusted subprocesses run as.
SANDBOX_UID_ENV = AgentConfig.env_name("sandbox_uid")

#: Environment variable naming the gid untrusted subprocesses run as.  Also the
#: sandbox uid's primary group, so a group-writable work tree is enough for it.
SANDBOX_GID_ENV = AgentConfig.env_name("sandbox_gid")

#: Legacy name of the sandbox ``HOME`` directory.  It used to live *inside* the
#: checkout, which both polluted ``git status`` and let a repository replace it
#: with a symlink; :data:`SANDBOX_HOME_PREFIX` replaced it.  The name is kept so
#: the path guard can still ignore a stale copy left by an older deployment.
SANDBOX_HOME_DIRNAME = ".sandbox-home"

#: Prefix of the worker-owned sandbox ``HOME``.  ``tempfile.mkdtemp`` fills in a
#: random suffix under the system temp directory, so the path is created
#: exclusively, owned by the worker, and (on a sticky ``/tmp``) cannot be
#: replaced by the untrusted uid.
SANDBOX_HOME_PREFIX = "openfish-sandbox-home-"

#: The root every work directory lives under.  Parent directories from a work
#: tree up to (and including) this root are relaxed so the sandbox uid can
#: traverse in; the walk never goes above it.
WORK_ROOT_ENV = AgentConfig.env_name("work_root")

#: Linux capability bit numbers (``linux/capability.h``): ``CAP_SETGID`` = 6,
#: ``CAP_SETUID`` = 7.  Dropping the child from the worker's uid to the sandbox
#: uid in-child needs only these two, which is exactly what the runner service
#: adds back (and which are only effective for a root worker process).
CAP_SETGID_BIT = 6
CAP_SETUID_BIT = 7

#: The sandbox home directory: group-writable, setgid, world-visible.
SANDBOX_HOME_MODE = 0o2775

#: ``g+rw`` for files, plus ``g+x`` for directories and already-executable files
#: (the ``X`` in ``g+rwX``), and ``g+rwx`` for directories.
_GROUP_RW = 0o060
_GROUP_X = 0o010
_GROUP_RWX = 0o070
#: ``X`` (as in ``g+rwX``): only files that already carry an execute bit.
_ANY_EXECUTE = 0o111
#: Directory setgid bit: new entries inherit the directory's group.
_SETGID = 0o2000

#: The sandbox ``HOME`` already created for each identity.  Creating it once and
#: reusing it means no spawn ever chmods a path the sandbox uid could have
#: replaced since the last one.
_HOME_CACHE: dict[tuple[int, int], Path] = {}


class SandboxIdentityError(RuntimeError):
    """The sandbox identity is misconfigured or cannot be applied.

    Raised for a partial/unparseable env pair and for a configured identity this
    process cannot drop to.  Never swallowed: the alternative is running
    untrusted code with the trusted worker's authority.
    """


@dataclass(frozen=True)
class SandboxIdentity:
    """The uid/gid untrusted subprocesses are dropped to."""

    uid: int
    gid: int


# ── Identity resolution ──────────────────────────────────────────────

def _configured_env() -> dict[str, str]:
    """The deployed sandbox knobs, in the string form the parsers below read.

    :mod:`config.agent` is the only reader of the environment; this module never
    touches ``os.environ``, so a value cannot be swapped under a running worker.
    Only the three names this module asks about are projected, which keeps the
    settings-to-parser bridge small enough to read at a glance.
    """
    return {
        SANDBOX_UID_ENV: _as_text(settings.agent.sandbox_uid),
        SANDBOX_GID_ENV: _as_text(settings.agent.sandbox_gid),
        WORK_ROOT_ENV: str(settings.agent.work_root or ""),
    }


def _as_text(value: int | None) -> str:
    """``""`` for an unset (``None``) knob, else its decimal text."""
    return "" if value is None else str(value)


def _origin(env: Mapping[str, str] | None) -> Mapping[str, str]:
    """The mapping to read; an explicit empty mapping is not replaced.

    ``None`` means "the deployment's configuration"; ``{}`` keeps meaning "this
    caller has nothing configured", which is how the offline gates exercise the
    unconfigured and half-configured paths without touching the environment.
    """
    return _configured_env() if env is None else env


def _raw(name: str, env: Mapping[str, str] | None) -> str:
    return str(_origin(env).get(name) or "").strip()


def configured_identity(
    *, env: Mapping[str, str] | None = None,
) -> SandboxIdentity | None:
    """The configured sandbox identity, or ``None`` when none is configured.

    Both :data:`SANDBOX_UID_ENV` and :data:`SANDBOX_GID_ENV` set and parseable as
    positive integers is the only configured state.  A partial or unparseable
    setting raises :class:`SandboxIdentityError` (fail closed): guessing the
    missing half would spawn untrusted code as the trusted worker.

    *env* is for callers that have their own mapping — the offline gate, which
    must be able to present a half-configured pair without changing what the
    rest of the process is configured with.  ``None`` reads the deployment's
    settings, which are resolved once at start-up (see
    :class:`config.base.EnvSettings`).
    """
    uid_raw = _raw(SANDBOX_UID_ENV, env)
    gid_raw = _raw(SANDBOX_GID_ENV, env)
    if not uid_raw and not gid_raw:
        return None
    try:
        uid = int(uid_raw, 10)
        gid = int(gid_raw, 10)
    except ValueError as exc:
        raise SandboxIdentityError(
            f"{SANDBOX_UID_ENV}/{SANDBOX_GID_ENV} 必须同时设置为正整数"
            f"（uid={uid_raw or '<unset>'} gid={gid_raw or '<unset>'}）；"
            "沙箱身份不完整或无法解析时拒绝运行不可信代码，绝不静默回退到 worker uid"
        ) from exc
    if uid <= 0 or gid <= 0:
        raise SandboxIdentityError(
            f"{SANDBOX_UID_ENV}/{SANDBOX_GID_ENV} 必须同时设置为正整数"
            f"（uid={uid_raw or '<unset>'} gid={gid_raw or '<unset>'}）；"
            "沙箱身份不完整或无法解析时拒绝运行不可信代码，绝不静默回退到 worker uid"
        )
    return SandboxIdentity(uid=uid, gid=gid)


# ── Capability check ─────────────────────────────────────────────────

def _cap_effective() -> int | None:
    """Effective capabilities from ``/proc/self/status``, or ``None``.

    ``None`` means "cannot tell" (non-Linux, unreadable ``/proc``), which
    :func:`privilege_drop_capable` falls back on to the ``euid == 0`` answer.
    """
    try:
        status = Path("/proc/self/status").read_text(encoding="ascii")
    except OSError:
        return None
    for line in status.splitlines():
        if line.startswith("CapEff:"):
            try:
                return int(line.split(":", 1)[1].strip(), 16)
            except (IndexError, ValueError):
                return None
    return None


def privilege_drop_capable(*, env: Mapping[str, str] | None = None) -> bool:
    """Whether this process can lower a child's uid/gid to the sandbox identity.

    Root always may.  Otherwise ``CapEff`` must carry ``CAP_SETUID`` (bit 7) and
    ``CAP_SETGID`` (bit 6); on a non-Linux host or an unreadable ``/proc`` the
    answer falls back to ``os.geteuid() == 0``.  *env* is accepted for symmetry
    with the other helpers and never influences the answer.
    """
    del env
    if os.geteuid() == 0:
        return True
    caps = _cap_effective()
    if caps is None:
        return False
    return bool(caps & (1 << CAP_SETUID_BIT)) and bool(caps & (1 << CAP_SETGID_BIT))


# ── The spawn contract ───────────────────────────────────────────────

def untrusted_popen_kwargs(
    *, env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Extra ``subprocess.Popen`` keyword arguments for an untrusted child.

    ``{}`` when no identity is configured (dev/tests: no drop).  Otherwise the
    child starts as the sandbox uid/gid, roots its supplementary groups at that
    gid and gets a fresh ``umask``.  A configured identity this process cannot
    switch to raises :class:`SandboxIdentityError` naming the missing
    capabilities and the two env vars — never a silent spawn as the worker.
    """
    identity = configured_identity(env=env)
    if identity is None:
        return {}
    if not privilege_drop_capable(env=env):
        raise SandboxIdentityError(
            f"已配置 {SANDBOX_UID_ENV}={identity.uid} / {SANDBOX_GID_ENV}="
            f"{identity.gid}，但当前进程缺少 CAP_SETUID/CAP_SETGID（euid="
            f"{os.geteuid()}，非 root 且 CapEff 不含所需位）；拒绝以 worker uid "
            "运行不可信代码——请给 runner 容器加 `cap_add: [SETUID, SETGID]`，"
            "或清空这两个环境变量以显式关闭沙箱"
        )
    return {
        "user": identity.uid,
        "group": identity.gid,
        "extra_groups": [identity.gid],
        # Group-writable umask, matching the shared setgid group: directories the
        # sandbox creates stay removable by the group (``0755`` would leave the
        # worker unable to clean a reclaimed checkout).
        "umask": 0o002,
    }


def _open_real_dir(path: Path) -> int:
    """Open *path* as a directory, refusing to follow a final symlink."""
    return os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)


def _sandbox_home(identity: SandboxIdentity) -> Path | None:
    """The worker-owned ``HOME`` for *identity*, created once (``None`` on error).

    Created with ``tempfile.mkdtemp`` under the system temp directory, so it is
    outside every checkout and outside anything the sandbox uid can replace, then
    chgrp/chmod'ed through the ``O_NOFOLLOW`` descriptor.  A failure is a
    best-effort ``None``: the child then keeps the allowlisted ``HOME`` rather
    than aborting a gate before it can report its own failure.
    """
    key = (identity.uid, identity.gid)
    cached = _HOME_CACHE.get(key)
    if cached is not None:
        try:
            cached_stat = os.lstat(cached)
        except OSError:
            cached_stat = None
        if cached_stat is not None and stat.S_ISDIR(cached_stat.st_mode):
            return cached
        _HOME_CACHE.pop(key, None)
    base = Path(tempfile.gettempdir())
    try:
        raw = Path(tempfile.mkdtemp(prefix=SANDBOX_HOME_PREFIX, dir=str(base)))
        fd = _open_real_dir(raw)
    except OSError as exc:
        return None
    try:
        home_stat = os.fstat(fd)
        if not stat.S_ISDIR(home_stat.st_mode) or home_stat.st_uid != os.geteuid():
            return None
        try:
            os.fchown(fd, -1, identity.gid)
        except OSError as exc:
            pass
        os.fchmod(fd, SANDBOX_HOME_MODE)
    except OSError as exc:
        pass
    finally:
        os.close(fd)
    _HOME_CACHE[key] = raw
    return raw


def sandbox_env_overrides(
    *, env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Environment additions for an untrusted child, ``{}`` when unconfigured.

    The one override today is ``HOME``: the worker's home belongs to the trusted
    uid and is typically mode ``0700``, so it is unusable to the sandbox uid.
    The child gets a dedicated worker-owned directory **outside the checkout**
    instead — a repository that can write its own work tree could otherwise
    replace an in-tree ``HOME`` with a symlink and turn the worker's next chmod
    into write access to a worker-owned file.  Callers merge the result into the
    child environment **after** :func:`services.sandbox_env.sandbox_env`.
    """
    identity = configured_identity(env=env)
    if identity is None:
        return {}
    home = _sandbox_home(identity)
    if home is None:
        return {}
    return {"HOME": str(home)}


# ── The work-tree contract ───────────────────────────────────────────

def _relax(path: Path, gid: int) -> None:
    """Best-effort group-share one entry: chgrp, ``g+rwX``, setgid on directories.

    The owner is never changed (``chown`` is never called): git must keep seeing
    the worker as the owner of the checkout.  Symlinks are only ``lchown``-ed, so
    a link is never followed outside the tree.  Every ``OSError`` is a debug log,
    not a failure — one entry the worker cannot re-mode must not abort the task.
    """
    try:
        os.chown(path, -1, gid, follow_symlinks=False)
    except (OSError, NotImplementedError) as exc:
        pass
    try:
        st = os.lstat(path)
    except OSError as exc:
        return
    if stat.S_ISLNK(st.st_mode):
        return
    mode = stat.S_IMODE(st.st_mode)
    directory = stat.S_ISDIR(st.st_mode)
    mode |= _GROUP_RWX if directory else _GROUP_RW
    if directory or (mode & _ANY_EXECUTE):
        mode |= _GROUP_X
    if directory:
        mode |= _SETGID
    try:
        os.chmod(path, mode)
    except OSError as exc:
        pass


def _walk_error(exc: OSError) -> None:
    pass


def _require_real_directory(path: Path) -> None:
    """Fail closed when *path* is not a real directory.

    ``os.walk(root, followlinks=False)`` still follows a symlinked *root*: the
    untrusted uid can create entries in a group-writable work root, so
    ``<work_root>/<task_id>`` could be a symlink to ``/app`` and the next relax
    pass would chgrp/chmod that target.  ``lstat`` (never ``stat``) is what
    distinguishes the link from the directory it points at.
    """
    try:
        st = os.lstat(path)
    except OSError as exc:
        raise SandboxIdentityError(
            f"工作目录 {path} 不可用：{exc}；拒绝在未知状态上放开沙箱权限"
        ) from exc
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise SandboxIdentityError(
            f"工作目录 {path} 不是真实目录（lstat 结果为符号链接或特殊文件）；"
            "拒绝顺着符号链接 chgrp/chmod"
        )


def _relax_tree(root: Path, gid: int) -> None:
    """Relax *root* and everything below it without following symlinked dirs."""
    _require_real_directory(root)
    _relax(root, gid)
    for dirpath, dirnames, filenames in os.walk(root, onerror=_walk_error, followlinks=False):
        directory = Path(dirpath)
        for name in (*dirnames, *filenames):
            _relax(directory / name, gid)


def _ancestor_dirs(path: Path, work_root: Path | None) -> list[Path]:
    """Parents of *path* up to and including *work_root* (never above it)."""
    if work_root is None:
        return []
    try:
        here = path.resolve()
        root = work_root.resolve()
    except OSError:
        return []
    if root not in here.parents:
        return []
    # ``path`` itself was already confirmed to be a real directory by
    # ``_relax_tree``; ``resolve()`` keeps the returned parents real, and the
    # ``root not in here.parents`` guard stops the walk above the work root.
    ancestors: list[Path] = []
    for parent in here.parents:
        ancestors.append(parent)
        if parent == root:
            break
    return ancestors


def prepare_untrusted_workdir(
    path: str | Path, *, env: Mapping[str, str] | None = None,
) -> None:
    """Make *path* (and its parents up to the work root) sandbox-usable.

    No-op without a configured identity.  Otherwise, per entry, best-effort:
    ``chgrp`` to the sandbox gid without changing the owner, add ``g+rw`` to
    files and ``g+rwx`` plus the setgid bit to directories, and relax the
    ancestors from *path* up to ``AGENT_WORK_ROOT`` so the sandbox uid can
    traverse in.  A *path* that is not a real directory (a symlink the untrusted
    uid planted under a writable work root) is refused with
    :class:`SandboxIdentityError` instead of followed.  Idempotent, and
    otherwise never raises for a permission hiccup.
    """
    identity = configured_identity(env=env)
    if identity is None:
        return
    work_root_raw = _raw(WORK_ROOT_ENV, env)
    work_root = Path(work_root_raw) if work_root_raw else None
    root = Path(path)
    _relax_tree(root, identity.gid)
    for ancestor in _ancestor_dirs(root, work_root):
        _relax(ancestor, identity.gid)


# ── Diagnostics ──────────────────────────────────────────────────────

def describe_identity(*, env: Mapping[str, str] | None = None) -> str:
    """One log-safe line describing the drop that will (not) happen."""
    identity = configured_identity(env=env)
    if identity is None:
        return "no sandbox uid configured (dev mode: untrusted code runs as the worker)"
    capable = "privilege drop capable" if privilege_drop_capable(env=env) else "privilege drop unavailable"
    return f"sandbox uid={identity.uid} gid={identity.gid} ({capable})"


__all__ = [
    "CAP_SETGID_BIT",
    "CAP_SETUID_BIT",
    "SANDBOX_GID_ENV",
    "SANDBOX_HOME_DIRNAME",
    "SANDBOX_HOME_MODE",
    "SANDBOX_HOME_PREFIX",
    "SANDBOX_UID_ENV",
    "WORK_ROOT_ENV",
    "SandboxIdentity",
    "SandboxIdentityError",
    "configured_identity",
    "describe_identity",
    "prepare_untrusted_workdir",
    "privilege_drop_capable",
    "sandbox_env_overrides",
    "untrusted_popen_kwargs",
]
