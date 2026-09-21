"""File validation pipeline — MIME, executable scan, archive checks, ClamAV."""

from __future__ import annotations

import logging
import os
import struct
import tarfile
import zipfile
from io import BytesIO

import magic

from config import settings
from services.paths import contained, safe_name

logger = logging.getLogger("cpypiserver.safe")

_MIME_MAP: dict[str, list[str]] = {
    "whl": ["application/zip", "application/x-zip-compressed", "application/x-zip", "application/octet-stream"],
    "zip": ["application/zip", "application/x-zip-compressed", "application/x-zip", "application/octet-stream"],
    "tar.gz": ["application/gzip", "application/x-gzip", "application/gzip-compressed", "application/x-tar", "application/octet-stream"],
    "tar": ["application/x-tar", "application/octet-stream"],
}

_EXEC_SIGNATURES: list[tuple[bytes, str]] = [
    (b"MZ", "PE/DOS"), (b"\x7fELF", "ELF"),
    (b"\xca\xfe\xba\xbe", "Mach-O universal"),
    (b"\xce\xfa\xed\xfe", "Mach-O 32-bit"), (b"\xcf\xfa\xed\xfe", "Mach-O 64-bit"),
]
_EXEC_SCAN_BYTES = 64 * 1024
_SDIST_INDICATORS = ("setup.py", "setup.cfg", "pyproject.toml", "PKG-INFO")

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
        logger.info("ClamAV connected — %s:%d", host, settings.security.clamav_port)
        return cd
    except Exception as exc:
        msg = f"ClamAV unreachable ({host}:{settings.security.clamav_port}): {exc}"
        if settings.security.clamav_required:
            raise RuntimeError(msg) from exc
        logger.warning("%s — scans disabled", msg)
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
        logger.warning("python-magic failed: %s", exc)
        return True, ""
    allowed = _MIME_MAP.get(ext)
    if allowed is None:
        return False, f"Unknown extension .{ext} (MIME: {detected})"
    if detected not in allowed:
        return False, f"MIME mismatch: '{detected}' for .{ext} (expected {allowed})"
    return True, ""


def _check_executable(content: bytes) -> tuple[bool, str]:
    window = min(len(content), _EXEC_SCAN_BYTES)
    for sig, label in _EXEC_SIGNATURES:
        offset = 0
        while True:
            offset = content.find(sig, offset, window)
            if offset == -1:
                break
            if sig == b"MZ" and offset + 64 <= len(content):
                if _pe_offset(content[offset:offset + 128]) is None:
                    offset += 1; continue
            if sig == b"\x7fELF" and offset + 5 <= len(content):
                if content[offset + 4] not in (1, 2):
                    offset += 1; continue
            if sig[:2] in (b"\xce\xfa", b"\xcf\xfa") and offset + 8 > len(content):
                offset += 1; continue
            logger.warning("Executable sig '%s' at offset %d — rejected", label, offset)
            return False, f"Executable content detected ({label} at byte {offset})"
            offset += 1
    return True, ""


def _pe_offset(dos_header: bytes) -> int | None:
    if len(dos_header) < 64:
        return None
    try:
        off = struct.unpack_from("<I", dos_header, 0x3C)[0]
    except struct.error:
        return None
    return off if 64 <= off <= 65536 else None


def _validate_wheel(content: bytes, filename: str) -> tuple[bool, str]:
    try:
        with zipfile.ZipFile(BytesIO(content)) as zf:
            names = zf.namelist()
            if not names:
                return False, f"Empty wheel: {filename}"
            dist_infos = {n.split("/")[0] for n in names if n.split("/")[0].endswith(".dist-info")}
            if not dist_infos:
                return False, f"No .dist-info dir in {filename}"
            if len(dist_infos) > 1:
                return False, f"Multiple .dist-info dirs in {filename}"
            di = dist_infos.pop()
            if f"{di}/WHEEL" not in names or f"{di}/METADATA" not in names:
                return False, f"{filename}: missing WHEEL/METADATA in {di}"
            return True, ""
    except zipfile.BadZipFile:
        return False, f"Not a valid ZIP: {filename}"
    except Exception as exc:
        return False, f"Wheel validation failed: {exc}"


def _validate_sdist(content: bytes, filename: str, ext: str) -> tuple[bool, str]:
    try:
        if ext in ("tar.gz", "tar"):
            mode = "r:gz" if ext == "tar.gz" else "r:"
            with tarfile.open(fileobj=BytesIO(content), mode=mode) as tf:
                names = tf.getnames()
                if not names:
                    return False, f"Empty sdist: {filename}"
                if not any(n.rsplit("/", 1)[-1].endswith(".py") or n.rsplit("/", 1)[-1] in _SDIST_INDICATORS for n in names):
                    return False, f"No Python source files in {filename}"
                return True, ""
        elif ext == "zip":
            with zipfile.ZipFile(BytesIO(content)) as zf:
                names = zf.namelist()
                if not names:
                    return False, f"Empty zip sdist: {filename}"
                if not any(n.rsplit("/", 1)[-1].endswith(".py") or n.rsplit("/", 1)[-1] in _SDIST_INDICATORS for n in names):
                    return False, f"No Python source files in {filename}"
                return True, ""
    except tarfile.TarError as exc:
        return False, f"Invalid tar: {filename} ({exc})"
    except zipfile.BadZipFile:
        return False, f"Invalid ZIP sdist: {filename}"
    except Exception as exc:
        return False, f"Archive validation failed: {exc}"
    return True, ""


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
        logger.warning("ClamAV scan error: %s", exc)
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
    if not ok: return False, err
    ok, err = _check_executable(content)
    if not ok: return False, err

    if matched_ext == "whl":
        ok, err = _validate_wheel(content, safe)
    elif matched_ext in ("tar.gz", "tar", "zip"):
        ok, err = _validate_sdist(content, safe, matched_ext)
    if not ok:
        return False, err

    ok, err = _scan_clamav(file)
    if not ok:
        return False, err

    try:
        contained(settings.storage.packages_dir, safe)
    except ValueError:
        return False, "Invalid file path (path traversal)"

    logger.info("Upload validated: %s (ext=.%s size=%.1f KB)", safe, matched_ext, size / 1024)
    return True, safe
