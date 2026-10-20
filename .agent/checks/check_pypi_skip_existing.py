#!/usr/bin/env python
"""Gate: a republished distribution is a no-op, not a conflict (twine >= 6.2.0).

twine 6.2.0 removed ``--skip-existing`` for every index that is not PyPI or
TestPyPI, and it rejects the flag in the *client*, before a request is sent::

    UnsupportedConfiguration: The configured repository '…' does not have
    support for the following features: --skip-existing

A self-hosted index therefore cannot intercept the check; it has to offer the
equivalent itself.  The server does that with ``STORAGE__ON_DUPLICATE=skip``, one
of the three modes of the duplicate policy (``conflict`` = refuse, ``overwrite`` =
replace, ``skip`` = successful no-op).

What this gate proves, through the real HTTP upload endpoint and the same
multipart form twine sends:

1. **The default ``conflict`` mode is unchanged.**  A duplicate is still ``400``
   + "already exists" for twine (the text twine <= 6.1 matched to implement
   ``--skip-existing`` against a self-hosted index) and ``409`` for every other
   client.
2. **``skip`` makes a republish idempotent.**  The duplicate is ``200``, the
   stored file and its ``.sha256`` sidecar are untouched, the file is not
   rewritten, and no temp file is left behind.
3. **``overwrite`` still replaces.**  Changed bytes under the same filename
   replace the stored file and refresh its digest.
4. **The single mode replaces the two old booleans.**  ``OVERWRITE=true`` folds
   into ``overwrite``, ``SKIP_EXISTING=true`` into ``skip``, and a mode plus a
   legacy switch that disagree is refused at start-up instead of guessed.

The form deliberately carries the fields twine 6.2.0 sends and **no**
``md5_digest`` — 6.2.0 removed it, so this also proves the endpoint never
depended on it.

Run from the repository root::

    python .agent/checks/check_pypi_skip_existing.py
    # dev box without a `python` on PATH:
    backend/.venv/bin/python .agent/checks/check_pypi_skip_existing.py
"""

from __future__ import annotations

import base64
import hashlib
import io
import os
import sys
import tempfile
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = REPO_ROOT / "backend"
sys.path.insert(0, str(BACKEND_DIR))
os.chdir(BACKEND_DIR)

# The app reads its configuration once, when `config` is imported, so every
# throwaway root has to be in the environment first.  The first-run catalog seed
# and the package watcher stay out of the way.  GuardDog is off because this gate
# exercises the upload route's conflict/skip decision, not the malware scanner —
# and its YARA rules would make the run depend on an offline cache being warm.
_TMP = Path(tempfile.mkdtemp(prefix="check-pypi-skip-existing-"))
_PACKAGES = _TMP / "packages"
_PACKAGES.mkdir(parents=True)
os.environ.update({
    "AUTH_ENABLED": "true",
    "AUTH_USERNAME": "check-admin",
    "AUTH_ASSERT": "check-secret",
    "SECRET_KEY": "check-pypi-skip-existing",
    "SEED_CATALOGS": "0",
    "WATCH_PACKAGES": "false",
    "GUARDDOG_ENABLED": "false",
    "CLAMAV_HOST": "",
    "LOG_LEVEL": "WARNING",
    "PACKAGES_DIR": str(_PACKAGES),
    "API_KEYS_FILE": str(_TMP / "cpypiserver.db"),
    "STATS_SNAPSHOT_FILE": str(_TMP / "stats_snapshot.json"),
    "PYTHON_BUILDS_DIR": str(_TMP / "python-builds"),
    "NODE_BUILDS_DIR": str(_TMP / "node-builds"),
    "TOOLS_DIR": str(_TMP / "tools"),
    "DOCS_DIR": str(_TMP / "docs"),
    "NPM_DIR": str(_TMP / "npm"),
    "NPM_CACHE_DIR": str(_TMP / "cache" / "npm"),
    "DOCKER_DIR": str(_TMP / "docker-images"),
    "DOCKER_CACHE_DIR": str(_TMP / "cache" / "docker"),
    "DEBIAN_DIR": str(_TMP / "debian"),
    "DEBIAN_CACHE_DIR": str(_TMP / "cache" / "debian"),
})
os.environ.pop("DATABASE_URL", None)

from pydantic import ValidationError  # noqa: E402

from app import app  # noqa: E402
from config import settings  # noqa: E402
from config.storage import StorageConfig  # noqa: E402
from services.digest import compute_sha256  # noqa: E402

AUTH = {
    "Authorization": "Basic " + base64.b64encode(b"check-admin:check-secret").decode("ascii"),
}
#: What twine 6.2.0+ actually sends.  The endpoint keys its conflict answer off
#: the product token, so the version suffix is deliberately not relied on.
TWINE_UA = "twine/6.2.0 CPython/3.12.0 Linux/6.8 openfish-check"
OTHER_UA = "pip/26.0"

FILENAME = "demo_pkg-1.0.0-py3-none-any.whl"
FAILURES: list[str] = []


def check(condition: bool, message: str) -> None:
    if condition:
        return
    FAILURES.append(message)
    print(f"FAIL: {message}")


def _wheel_bytes(*, comment: str) -> bytes:
    """A minimal valid wheel: METADATA, WHEEL and a `RECORD` covering itself."""
    dist_info = "demo_pkg-1.0.0.dist-info"
    files = {
        "demo_pkg/__init__.py": b"",
        f"{dist_info}/METADATA": (
            "Metadata-Version: 2.1\n"
            "Name: demo-pkg\n"
            "Version: 1.0.0\n"
            f"Summary: {comment}\n"
        ).encode(),
        f"{dist_info}/WHEEL": (
            "Wheel-Version: 1.0\n"
            "Generator: check_pypi_skip_existing\n"
            "Root-Is-Purelib: true\n"
            "Tag: py3-none-any\n"
        ).encode(),
    }
    record = []
    for path, data in files.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=")
        record.append(f"{path},sha256={digest.decode('ascii')},{len(data)}")
    record.append(f"{dist_info}/RECORD,,")
    files[f"{dist_info}/RECORD"] = ("\n".join(record) + "\n").encode()

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path, data in files.items():
            archive.writestr(path, data)
    return buffer.getvalue()


def _upload(client, content: bytes, *, user_agent: str = TWINE_UA):
    """POST the multipart body `twine upload` sends (6.2.0: no md5_digest)."""
    form = {
        ":action": "file_upload",
        "protocol_version": "1",
        "name": "demo-pkg",
        "version": "1.0.0",
        "filetype": "bdist_wheel",
        "pyversion": "py3",
        "sha256_digest": hashlib.sha256(content).hexdigest(),
        "blake2_256_digest": hashlib.blake2b(content, digest_size=32).hexdigest(),
        "content": (io.BytesIO(content), FILENAME),
    }
    headers = dict(AUTH)
    headers["User-Agent"] = user_agent
    return client.post("/", data=form, headers=headers)


def _digest_of(path: Path) -> str:
    return path.read_text().strip().rsplit(":", 1)[-1]


def _check_duplicate_modes() -> None:
    """One mode replaces the two booleans, and the old env names still fold."""
    check(settings.storage.on_duplicate == "conflict",
          f"the throwaway app starts in the default conflict mode (got {settings.storage.on_duplicate!r})")
    check(not settings.storage.allows_overwrite and not settings.storage.skips_existing,
          "the default mode allows neither overwrite nor skip")

    legacy_overwrite = StorageConfig(_env_file=None, overwrite=True)
    check(legacy_overwrite.on_duplicate == "overwrite",
          "deprecated OVERWRITE=true folds into on_duplicate=overwrite")
    legacy_skip = StorageConfig(_env_file=None, skip_existing=True)
    check(legacy_skip.on_duplicate == "skip",
          "deprecated SKIP_EXISTING=true folds into on_duplicate=skip")
    legacy_false = StorageConfig(_env_file=None, overwrite=False)
    check(legacy_false.on_duplicate == "conflict",
          "deprecated OVERWRITE=false is the old default and stays conflict")
    try:
        StorageConfig(_env_file=None, on_duplicate="skip", overwrite=True)
    except ValidationError:
        pass
    else:
        check(False, "a non-conflict mode plus a legacy switch is refused, not guessed")


def main() -> int:
    _check_duplicate_modes()
    client = app.test_client()
    first = _wheel_bytes(comment="first build")
    stored = _PACKAGES / FILENAME
    sidecar = _PACKAGES / (FILENAME + ".sha256")

    # ── A fresh upload stores the file ──────────────────────────────
    response = _upload(client, first)
    check(response.status_code == 200,
          f"first upload answers 200 (got {response.status_code}: {response.data[:200]!r})")
    check(stored.is_file(), "the first upload stores the file")
    check(compute_sha256(stored) == hashlib.sha256(first).hexdigest(),
          "the stored file is byte-identical to the upload")
    check(sidecar.is_file(), "the first upload writes the .sha256 sidecar")
    check(_digest_of(sidecar) == hashlib.sha256(first).hexdigest(),
          "the sidecar carries the SHA-256 of the uploaded bytes")
    inode = stored.stat().st_ino
    mtime_ns = stored.stat().st_mtime_ns

    # ── Default `conflict` mode: the contract twine <= 6.1 and other clients rely on ──
    duplicate = _upload(client, first)
    check(duplicate.status_code == 400,
          f"a twine duplicate in conflict mode answers 400 (got {duplicate.status_code})")
    check(b"already exist" in duplicate.data.lower(),
          "the 400 body carries the 'already exists' text twine <= 6.1 matches")
    other = _upload(client, first, user_agent=OTHER_UA)
    check(other.status_code == 409,
          f"a non-twine duplicate answers 409 (got {other.status_code})")
    check(stored.stat().st_ino == inode and stored.stat().st_mtime_ns == mtime_ns,
          "a refused duplicate does not rewrite the stored file")

    # ── `skip` mode: a republish is a 200 no-op ─────────────────────
    settings.storage.on_duplicate = "skip"
    skipped = _upload(client, first)
    check(skipped.status_code == 200,
          f"a twine duplicate in skip mode answers 200 (got {skipped.status_code}: {skipped.data[:200]!r})")
    check(compute_sha256(stored) == hashlib.sha256(first).hexdigest(),
          "the skipped upload leaves the stored bytes untouched")
    check(stored.stat().st_ino == inode and stored.stat().st_mtime_ns == mtime_ns,
          "the skipped upload does not rewrite the stored file")
    check(_digest_of(sidecar) == hashlib.sha256(first).hexdigest(),
          "the skipped upload leaves the .sha256 sidecar untouched")
    leftovers = sorted(path.name for path in _PACKAGES.iterdir())
    check(leftovers == sorted([FILENAME, FILENAME + ".sha256"]),
          f"the skipped upload leaves no temp file behind (found {leftovers})")

    # ── `overwrite` mode: changed bytes replace the stored file ─────
    changed = _wheel_bytes(comment="rebuilt after the first publish")
    check(changed != first, "the second build differs from the first")
    settings.storage.on_duplicate = "overwrite"
    replaced = _upload(client, changed)
    check(replaced.status_code == 200,
          f"overwrite answers 200 (got {replaced.status_code}: {replaced.data[:200]!r})")
    check(compute_sha256(stored) == hashlib.sha256(changed).hexdigest(),
          "overwrite replaces the stored bytes")
    check(_digest_of(sidecar) == hashlib.sha256(changed).hexdigest(),
          "overwrite refreshes the .sha256 sidecar")

    settings.storage.on_duplicate = "conflict"

    if FAILURES:
        print(f"\n{len(FAILURES)} failure(s)")
        return 1
    print(
        "PASS: ON_DUPLICATE=skip makes a twine >= 6.2.0 republish a 200 no-op; "
        "conflict keeps the 400/409 contract and overwrite replaces"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
