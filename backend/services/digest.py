"""SHA-256 digests of artifacts — one cache, one hashing loop.

Three callers need the digest of a file that may be very large:

* the package index, which publishes ``#sha256=`` fragments in the PEP 503
  index — those must survive a restart, so a cold start does not re-read every
  wheel on the disk;
* the CPython and Node build mirrors, which publish a checksum beside every
  archive;
* the artifact catalogs (tools / npm / docker / debian), which show a digest
  when the file is small enough for that to be worth doing at all.

They used to implement that separately, with different caches keyed on different
things and different side effects.  This is the single implementation, with two
levels that share one hashing loop:

* an in-memory cache keyed on a **stamp** — whatever identifies the content
  version of the bytes.  For a path that is ``(resolved path, mtime_ns, size)``,
  so an edited file misses automatically and an untouched one is never read
  twice; a store passes ``(namespace, key, modified, size)``, the same idea for
  a medium that has no path.  One cache, because "the digest of this version of
  these bytes" is one question.
* an optional ``<file>.sha256`` sidecar for the package tree, which is what
  makes a cold start cheap.  A sidecar records the stat it was computed from
  (``<mtime_ns>:<size>:<digest>``), so it too goes stale by itself rather than
  trusting a digest for a file that has since been replaced.
"""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Hashable, Iterable
from pathlib import Path
from typing import IO

#: Read size for the streaming hash: large enough that a multi-GB archive on an
#: SSD is not dominated by syscalls.
_BLOCKSIZE = 8 << 20

#: A digest is 64 lowercase hex characters.
_HEXLEN = 64
_HEXDIGITS = frozenset("0123456789abcdef")

#: ``stamp -> digest``.  Bounded at :data:`_CACHE_MAX` entries (oldest out) so a
#: long-lived process that sees many edits cannot grow it without limit.
_CACHE_MAX = 8192
_cache: dict[Hashable, str] = {}
_lock = threading.Lock()


def _remember(key: Hashable, digest: str) -> None:
    """Cache one digest, evicting the oldest entries past the cap."""
    with _lock:
        _cache[key] = digest
        while len(_cache) > _CACHE_MAX:
            _cache.pop(next(iter(_cache)))


def _sidecar_path(path: Path) -> Path:
    return path.with_suffix(path.suffix + ".sha256")


def _stamp(st) -> str:
    """The stat a sidecar is only valid for."""
    return f"{st.st_mtime_ns}:{st.st_size}"


def _read_sidecar(sidecar: Path) -> tuple[str | None, str] | None:
    """Read a sidecar as ``(stamp, digest)``; ``None`` when unusable.

    A *stamp* of ``None`` means a legacy sidecar that holds the bare digest — it
    is adopted (and upgraded in place) rather than discarded, so moving to the
    stamped format does not re-hash a tree of multi-gigabyte wheels.
    """
    try:
        text = sidecar.read_text().strip()
    except OSError:
        return None
    if len(text) == _HEXLEN and all(c in _HEXDIGITS for c in text):
        return None, text
    stamp, _, digest = text.partition(":")
    if len(digest) == _HEXLEN and all(c in _HEXDIGITS for c in digest):
        return stamp, digest
    return None


def _write_sidecar(sidecar: Path, stamp: str, digest: str) -> None:
    """Best-effort sidecar write — a full disk must not fail an upload."""
    try:
        sidecar.write_text(f"{stamp}:{digest}")
    except OSError:
        pass


# ── Byte and text digestion ──────────────────────────────────────────
# The only place in this project that turns bytes into a SHA-256 hex digest.
# `fingerprint()` (Agent Hub finding identity) and the policy hash both need a
# digest *of a string*; before this existed each caller called `hashlib` itself,
# which is the "one concern, one implementation" rule's canonical violation.

def _hexdigest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str, *, encoding: str = "utf-8") -> str:
    """SHA-256 of *text*, as 64 lowercase hex characters.

    Not memoised: the callers hash short strings they have just built (a finding
    fingerprint, a policy document), so a cache would cost more than it saves.
    """
    return _hexdigest(text.encode(encoding))


def sha256_of(chunks: Iterable[bytes], *, stamp: Hashable | None = None) -> str:
    """SHA-256 of a byte stream, memoised on *stamp*.

    The one hashing loop.  :func:`sha256_or_none` feeds it a file's blocks and
    :func:`sha256_of_reader` feeds it a store object's, so the algorithm and its
    cache exist once for both media.  *stamp* must change whenever the bytes do
    — a path passes ``(resolved path, mtime_ns, size)``, a store passes
    ``(namespace, key, modified, size)`` — because it *is* the cache key: a
    caller with no version identity to offer passes ``None`` and gets a correct
    digest with no memo.
    """
    if stamp is not None:
        with _lock:
            cached = _cache.get(stamp)
        if cached is not None:
            return cached

    digester = hashlib.sha256()
    for block in chunks:
        digester.update(block)
    digest = digester.hexdigest()

    if stamp is not None:
        _remember(stamp, digest)
    return digest


def sha256_of_reader(handle: IO[bytes], *, stamp: Hashable | None = None) -> str:
    """SHA-256 of an open binary stream, memoised on *stamp*."""
    return sha256_of(iter(lambda: handle.read(_BLOCKSIZE), b""), stamp=stamp)


def compute_sha256(file_path: str | Path, *, sidecar: bool = True) -> str:
    """SHA-256 of *file_path*, memoised on its stat.

    With *sidecar*, a ``<file>.sha256`` next to the file is consulted first and
    written back after a miss, so a restart does not re-hash the tree.  Raises
    :class:`OSError` when the file cannot be read, which is what the package
    index expects: a file it indexed a moment ago should exist.
    """
    digest = sha256_or_none(file_path, sidecar=sidecar)
    if digest is None:
        raise OSError(f"cannot read {file_path} to hash it")
    return digest


def sha256_or_none(
    file_path: str | Path,
    *,
    max_bytes: int | None = None,
    sidecar: bool = False,
) -> str | None:
    """SHA-256 of *file_path*, or ``None`` when it is unreadable or too large.

    *max_bytes* is the "not worth hashing" ceiling used by the artifact
    catalogs: a multi-gigabyte installer is still listed, just without a digest.
    """
    path = Path(file_path)
    try:
        st = path.stat()
    except OSError:
        return None
    if max_bytes is not None and st.st_size > max_bytes:
        return None

    key = (str(path.resolve()), st.st_mtime_ns, st.st_size)
    with _lock:
        cached = _cache.get(key)
    if cached is not None:
        return cached

    sidecar_file = _sidecar_path(path) if sidecar else None
    if sidecar_file is not None:
        stored = _read_sidecar(sidecar_file)
        if stored is not None:
            stamp, digest = stored
            if stamp == _stamp(st):
                _remember(key, digest)
                return digest
            if stamp is None:
                # Legacy sidecar: the digest is still the one for this file
                # (nothing modified it, or the watchdog would have dropped the
                # sidecar), so adopt it and record the stamp for next time.
                _remember(key, digest)
                _write_sidecar(sidecar_file, _stamp(st), digest)
                return digest

    try:
        with open(path, "rb") as fh:
            digest = sha256_of_reader(fh, stamp=key)
    except OSError:
        return None

    if sidecar_file is not None:
        _write_sidecar(sidecar_file, _stamp(st), digest)
    return digest


def invalidate_digest_cache(file_path: str | Path) -> None:
    """Forget every cached digest for *file_path*, and drop its sidecar."""
    resolved = str(Path(file_path).resolve())
    with _lock:
        for key in [k for k in _cache if k[0] == resolved]:
            del _cache[key]
    try:
        _sidecar_path(Path(file_path)).unlink(missing_ok=True)
    except OSError:
        pass


def store_digest(file_path: str | Path, digest: str) -> None:
    """Record a digest that is already known (e.g. computed while uploading)."""
    path = Path(file_path)
    try:
        st = path.stat()
    except OSError:
        return
    _remember((str(path.resolve()), st.st_mtime_ns, st.st_size), digest)
    _write_sidecar(_sidecar_path(path), _stamp(st), digest)


__all__ = [
    "compute_sha256",
    "invalidate_digest_cache",
    "sha256_of",
    "sha256_of_reader",
    "sha256_or_none",
    "sha256_text",
    "store_digest",
]
