"""Durable, atomic writes for the small documents the server owns.

The model route table, a document's ``meta.json``, a document body and the
device-code store are all written the same way: to a sibling temp file which is
then ``os.replace``d into position.  A concurrent reader — or the downstream
consumer of the route table — therefore sees either the old file or the new
one, never a half-written one.

There is one exception, and it is the reason this lives in one place instead of
being re-derived per module: a **single-file bind mount** (a common way to hand
a deployment its route table) is a mount point, and ``rename(2)`` onto it fails
with ``EBUSY``, or ``EXDEV`` when the temp file is on another filesystem.  There
the document is rewritten in place, which is the only operation such a mount
permits.
"""

from __future__ import annotations

import errno
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Iterable



def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write *data* to *path* atomically (see the module docstring)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        try:
            os.replace(tmp_name, path)
        except OSError as exc:
            if exc.errno not in (errno.EBUSY, errno.EXDEV):
                raise
            with open(path, "wb") as fh:
                fh.write(data)
    finally:
        # A successful replace consumed the temp file; the in-place fallback
        # left it behind.  Either way, cleaning up is best-effort.
        try:
            os.unlink(tmp_name)
        except OSError:
            pass


def atomic_write_stream(path: Path, chunks: Iterable[bytes]) -> int:
    """Stream *chunks* to *path* atomically.  Returns the number of bytes written.

    Companion to :func:`atomic_write_bytes` for a body too large to hold in
    memory: the chunks are consumed lazily and written straight to a sibling
    temp file, so an HTTP upload streams through the process instead of landing
    in it.  ``mkstemp`` creates that file 0600 and it is chmodded to 0644 just
    before the rename, so the published file is world-readable while a
    half-written one never is.

    Unlike :func:`atomic_write_bytes` there is no in-place fallback for an
    ``EBUSY``/``EXDEV`` rename failure.  That fallback exists for a *single-file
    bind mount*; this function targets a directory of artifacts, where a sibling
    rename always succeeds — and the chunks it has already consumed could not be
    replayed to rewrite a mount point anyway.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    total = 0
    fd, tmp_name = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as fh:
            for chunk in chunks:
                if not chunk:
                    continue
                fh.write(chunk)
                total += len(chunk)
        os.chmod(tmp_name, 0o644)
        os.replace(tmp_name, path)
    finally:
        # A successful replace consumed the temp file; an aborted stream left it
        # behind.  Either way, cleaning up is best-effort.
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
    return total


def write_json(path: Path, payload: Any) -> None:
    """Serialise *payload* as formatted UTF-8 JSON, atomically."""
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    atomic_write_bytes(path, text.encode("utf-8"))


def parse_json(text: str, default: Any = None) -> Any:
    """Parse *text* as JSON, returning *default* when it is malformed.

    The "a bad catalog file must not 500 the page that reads it" rule, split out
    of :func:`read_json` so the same rule applies to a document read through
    :mod:`services.objectstore` instead of a path: a malformed overlay degrades
    to the default, silently, exactly as a missing one does.
    """
    try:
        return json.loads(text)
    except ValueError:
        return default


def read_json(path: Path, default: Any = None) -> Any:
    """Parse *path* as JSON, returning *default* when it is missing or invalid.

    A catalog or state file is operator-owned: a malformed one must degrade to
    the default rather than 500 the page that reads it, and so must one that
    cannot be read at all.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return default
    return parse_json(text, default=default)


__all__ = [
    "atomic_write_bytes",
    "atomic_write_stream",
    "parse_json",
    "read_json",
    "write_json",
]
