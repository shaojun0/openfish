"""File validation pipeline — MIME, archive structure, GuardDog, ClamAV.

Every step here delegates to a library that owns the concern:

* **name** — :func:`services.paths.safe_name` (Werkzeug's ``secure_filename``);
* **content type** — ``python-magic`` (libmagic);
* **wheel/sdist structure** — :mod:`packaging` parses the filename,
  :class:`wheel.wheelfile.WheelFile` proves the archive's ``RECORD`` matches its
  own bytes, and ``tarfile`` recognises a tar container;
* **malware** — GuardDog's YARA rules and risk engine
  (:class:`guarddog.PypiPackageScanner`) plus ``clamd`` when a daemon is
  configured.

What is left in this module is policy, not mechanism: the extension allow-list,
the size cap, which steps run and what happens when a scanner is unavailable.
Two behaviours are deliberate:

1. the filename is reduced to a safe basename *first*, so every later step — the
   error strings that echo a name, the temporary archive and the destination
   path — works on a value that cannot traverse or inject;
2. a scanner that cannot run **fails open with a warning** unless
   ``security.clamav_required`` / ``security.guarddog_required`` says otherwise,
   which mirrors how a deployment without ClamAV has always behaved.
"""

from __future__ import annotations

import importlib.util
import json
import os
import tarfile
import tempfile
import time
import zipfile
from collections.abc import Callable
from pathlib import Path

import magic
from packaging.utils import (
    InvalidSdistFilename,
    InvalidWheelFilename,
    parse_sdist_filename,
    parse_wheel_filename,
)
from wheel.wheelfile import WheelError, WheelFile

from config import settings
from services.paths import contained, safe_name


_MIME_MAP: dict[str, list[str]] = {
    "whl": ["application/zip", "application/x-zip-compressed", "application/x-zip", "application/octet-stream"],
    "zip": ["application/zip", "application/x-zip-compressed", "application/x-zip", "application/octet-stream"],
    "tar.gz": ["application/gzip", "application/x-gzip", "application/gzip-compressed", "application/x-tar", "application/octet-stream"],
    "tar": ["application/x-tar", "application/octet-stream"],
}

#: Wheel members every wheel must carry inside its ``.dist-info`` (PEP 427).
_WHEEL_REQUIRED_MEMBERS = ("WHEEL", "METADATA")

# ── GuardDog singleton ───────────────────────────────────────────────

_guarddog: object | None = None
_guarddog_extract: Callable[[str, str], None] | None = None
_guarddog_init_attempted: bool = False

#: GuardDog reads the "top packages" lists its typosquat heuristics compare
#: against through a cache with a 30-day expiry, and refreshes an expired copy
#: *while the module is being imported* — with ``requests.get(url)``, no
#: timeout, against github.com and hugovk.github.io.  On the networks this
#: product is deployed to that fetch can never succeed, so the bundled lists are
#: copied into a directory we own and stamped far into the future: the two
#: things GuardDog asks of the file are "it parses" and "it is not expired".
_CACHE_PIN_SECONDS = 10 * 365 * 24 * 3600


def _bundled_guarddog_resources() -> Path | None:
    """Locate GuardDog's bundled lists **without importing GuardDog**."""
    spec = importlib.util.find_spec("guarddog")
    roots = getattr(spec, "submodule_search_locations", None)
    if not roots:
        return None
    resources = Path(next(iter(roots))) / "analyzer" / "metadata" / "resources"
    return resources if resources.is_dir() else None


def _pin_guarddog_cache() -> None:
    """Copy GuardDog's top-package lists into the cache this server owns.

    A missing cache file is worse than an expired one: the Go detector raises
    ``Could not retrieve top Go packages``, which would make the import fail and
    the scan disappear.  So every ``top_*.json`` GuardDog ships is mirrored here
    and rewritten with a timestamp that never expires.  Idempotent: an already
    pinned copy is left alone.
    """
    cache_dir = Path(settings.security.guarddog_cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    # Set before the import below: this is the directory GuardDog reads, and it
    # keeps a successful refresh out of the (read-only) site-packages tree.
    os.environ["GUARDDOG_TOP_PACKAGES_CACHE_LOCATION"] = str(cache_dir)

    resources = _bundled_guarddog_resources()
    if resources is None:
        return

    pinned_until = int(time.time()) + _CACHE_PIN_SECONDS
    still_pinned = int(time.time()) + 365 * 24 * 3600
    for bundled in sorted(resources.glob("top_*.json")):
        target = cache_dir / bundled.name
        try:
            if json.loads(target.read_text(encoding="utf-8")).get("downloaded_timestamp", 0) > still_pinned:
                continue
        except (OSError, ValueError):
            pass
        try:
            payload = json.loads(bundled.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            continue
        payload["downloaded_timestamp"] = pinned_until
        target.write_text(json.dumps(payload), encoding="utf-8")


def _get_guarddog():
    """Return GuardDog's scanner, or ``None`` when scanning is unavailable.

    Everything the scan needs is imported here, together, so that a broken or
    half-installed GuardDog is one warning rather than a mid-upload import
    error on the request path.
    """
    global _guarddog, _guarddog_extract, _guarddog_init_attempted
    if _guarddog is not None or _guarddog_init_attempted:
        return _guarddog
    _guarddog_init_attempted = True
    if not settings.security.guarddog_enabled:
        return None
    try:
        _pin_guarddog_cache()
        from guarddog import PypiPackageScanner
        from guarddog.utils.archives import safe_extract

        scanner = PypiPackageScanner()
        _guarddog, _guarddog_extract = scanner, safe_extract
        return scanner
    except Exception as exc:  # noqa: BLE001 — an import that fails for any reason disables the scan
        msg = f"GuardDog unavailable ({type(exc).__name__}: {exc})"
        if settings.security.guarddog_required:
            raise RuntimeError(msg) from exc
        return None


def _reset_guarddog_for_test() -> None:
    global _guarddog, _guarddog_extract, _guarddog_init_attempted
    _guarddog = None
    _guarddog_extract = None
    _guarddog_init_attempted = False


# ── ClamAV singleton ─────────────────────────────────────────────────

_clamd: object | None = None
_clamd_init_attempted: bool = False


def _get_clamd():
    global _clamd, _clamd_init_attempted
    if _clamd is not None or _clamd_init_attempted:
        return _clamd
    _clamd_init_attempted = True
    host = settings.security.clamav_host
    if not host:
        return None
    try:
        import clamd
        cd = clamd.ClamdNetworkSocket(host=host, port=settings.security.clamav_port, timeout=settings.security.clamav_timeout)
        cd.ping()
        _clamd = cd
        return cd
    except Exception as exc:
        msg = f"ClamAV unreachable ({host}:{settings.security.clamav_port}): {exc}"
        if settings.security.clamav_required:
            raise RuntimeError(msg) from exc
        return None


def _reset_clamd_for_test() -> None:
    global _clamd, _clamd_init_attempted
    _clamd = None
    _clamd_init_attempted = False


# ── Validation steps ─────────────────────────────────────────────────

def _check_mime(content: bytes, ext: str) -> tuple[bool, str]:
    try:
        detected = magic.from_buffer(content, mime=True)
    except Exception as exc:
        return True, ""
    allowed = _MIME_MAP.get(ext)
    if allowed is None:
        return False, f"Unknown extension .{ext} (MIME: {detected})"
    if detected not in allowed:
        return False, f"MIME mismatch: '{detected}' for .{ext} (expected {allowed})"
    return True, ""


def _validate_wheel(archive: Path, filename: str) -> tuple[bool, str]:
    """Prove *filename* names a wheel and the archive agrees with its ``RECORD``.

    ``packaging`` owns the filename grammar and ``WheelFile`` owns the archive:
    it reads ``.dist-info/RECORD`` from the wheel named by the filename and
    checks every member's SHA-256 as it is read, so a wheel whose bytes were
    edited after it was built, or that carries a member the RECORD does not
    list, cannot pass.  Reading every member is what runs the check.
    """
    try:
        parse_wheel_filename(filename)
    except InvalidWheelFilename as exc:
        return False, f"{filename}: {exc}"
    try:
        with WheelFile(str(archive)) as wheel:
            for member in wheel.namelist():
                if member.endswith("/"):
                    continue
                with wheel.open(member) as handle:
                    handle.read()
            dist_info = wheel.dist_info_path
            names = set(wheel.namelist())
    except WheelError as exc:
        return False, f"{filename}: {exc}"
    except (zipfile.BadZipFile, OSError, ValueError) as exc:
        return False, f"{filename}: unreadable wheel ({exc})"
    missing = [name for name in _WHEEL_REQUIRED_MEMBERS if f"{dist_info}/{name}" not in names]
    if missing:
        return False, f"{filename}: missing {'/'.join(missing)} in {dist_info}"
    return True, ""


def _validate_sdist(archive: Path, filename: str, ext: str) -> tuple[bool, str]:
    """Prove *filename* is a PEP 625 sdist name and the archive is a real one."""
    if ext in ("tar.gz", "zip"):
        try:
            parse_sdist_filename(filename)
        except InvalidSdistFilename as exc:
            return False, f"{filename}: {exc}"
        return True, ""
    # PEP 625 defines no bare ``.tar`` sdist, so only the container is checkable.
    if not tarfile.is_tarfile(archive):
        return False, f"Not a valid tar archive: {filename}"
    return True, ""


def _scan_guarddog(archive: Path, extract_dir: Path) -> tuple[bool, str]:
    """Extract *archive* safely and run GuardDog's rules over its source.

    ``guarddog.utils.archives.safe_extract`` is GuardDog's own extraction
    primitive and the reason no archive code lives here: it enforces an
    uncompressed-size and compression-ratio budget, refuses symlinks and device
    files that point outside the target, and rejects the ZIP parser
    differentials (duplicate EOCD, central directory that hides local file
    headers) that make a scanner see a different archive than the installer
    does.  Anything it refuses is an archive we will not store.
    """
    scanner = _get_guarddog()
    if scanner is None or _guarddog_extract is None:
        return True, "GuardDog disabled"

    try:
        _guarddog_extract(str(archive), str(extract_dir))
    except Exception as exc:  # noqa: BLE001 — ValueError for every safety refusal
        return False, f"Unsafe archive: {exc}"

    try:
        result = scanner.scan_local(str(extract_dir))
    except Exception as exc:
        if settings.security.guarddog_required:
            raise RuntimeError(f"GuardDog scan failed: {exc}") from exc
        return True, "GuardDog error — passed through"

    score = result.get("risk_score") or {}
    value = float(score.get("score") or 0.0)
    label = score.get("label", "unknown")
    rules = sorted(
        {str(risk["threat_rule"]) for risk in (result.get("risks") or []) if risk.get("threat_rule")}
    )
    if value >= settings.security.guarddog_min_risk_score:
        return False, f"Malicious package indicators (risk {value:.1f}/10): {', '.join(rules[:4]) or label}"
    return True, f"GuardDog {label}"


def _scan_clamav(file_obj) -> tuple[bool, str]:
    cd = _get_clamd()
    if cd is None:
        return True, "ClamAV disabled"
    file_obj.seek(0)
    try:
        result = cd.instream(file_obj)
    except Exception as exc:
        if settings.security.clamav_required:
            raise RuntimeError(f"ClamAV scan failed: {exc}") from exc
        return True, "ClamAV error — passed through"
    finally:
        file_obj.seek(0)
    status = result.get("stream", ("ERROR",))
    if status[0] == "OK":
        return True, "ClamAV clean"
    virus = status[1] if len(status) > 1 else "Unknown"
    return False, f"Virus detected: {virus}"


# ── Public API ───────────────────────────────────────────────────────

def validate_file(file, filename: str) -> tuple[bool, str]:
    """Full upload validation pipeline. Returns ``(is_valid, safe_filename_or_error)``.

    The *filename* is reduced to a safe basename **first**, through
    :func:`services.paths.safe_name`, and every later step — the extension
    check, the archive messages and the destination path — works on that value.
    That ordering is what makes the error strings that echo a name
    (``Extension not allowed: '<name>'``) safe to return to the client, and it
    is why the destination is built with :func:`services.paths.contained`
    rather than by joining the raw input.

    The archive is materialised once, under that same reduced name, in a
    throwaway directory: the library checks read the file's *name* (a wheel's
    ``.dist-info`` directory is derived from it), so the temporary copy has to
    carry it too.
    """
    try:
        safe = safe_name(filename)
    except ValueError:
        return False, "Invalid filename"

    file.seek(0, os.SEEK_END)
    size = file.tell()
    file.seek(0)

    if size > settings.storage.max_content_length:
        limit_mb = settings.storage.max_content_length / 1024 / 1024
        return False, f"File too large (max {limit_mb:.0f} MB, got {size / 1024 / 1024:.1f} MB)"
    if size == 0:
        return False, "Empty file"

    name_lower = safe.lower()
    matched_ext = None
    for ext in sorted(settings.storage.allow_extensions, key=len, reverse=True):
        if name_lower.endswith(f".{ext}"):
            matched_ext = ext
            break
    if matched_ext is None:
        return False, f"Extension not allowed: '{safe}' (allowed: {', '.join(settings.storage.allow_extensions)})"

    content = file.read()
    file.seek(0)

    ok, err = _check_mime(content, matched_ext)
    if not ok:
        return False, err

    # ``ignore_cleanup_errors``: an extracted member that a tar marked
    # read-only must not turn a valid upload into a 500 after the scan passed.
    with tempfile.TemporaryDirectory(prefix="cpypi-validate-", ignore_cleanup_errors=True) as workdir:
        archive = Path(workdir) / safe
        archive.write_bytes(content)

        if matched_ext == "whl":
            ok, err = _validate_wheel(archive, safe)
        elif matched_ext in ("tar.gz", "tar", "zip"):
            ok, err = _validate_sdist(archive, safe, matched_ext)
        if not ok:
            return False, err

        extract_dir = Path(workdir) / "contents"
        extract_dir.mkdir()
        ok, err = _scan_guarddog(archive, extract_dir)
        if not ok:
            return False, err

    ok, err = _scan_clamav(file)
    if not ok:
        return False, err

    try:
        contained(settings.storage.packages_dir, safe)
    except ValueError:
        return False, "Invalid file path (path traversal)"

    return True, safe
