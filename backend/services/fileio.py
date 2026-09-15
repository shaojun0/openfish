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
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

logger = logging.getLogger("cpypiserver.fileio")


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
            logger.debug("cannot rename onto %s (%s); rewriting in place", path, exc)
            with open(path, "wb") as fh:
                fh.write(data)
    finally:
        # A successful replace consumed the temp file; the in-place fallback
        # left it behind.  Either way, cleaning up is best-effort.
        try:
            os.unlink(tmp_name)
        except OSError:
            pass


def write_json(path: Path, payload: Any) -> None:
    """Serialise *payload* as formatted UTF-8 JSON, atomically."""
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    atomic_write_bytes(path, text.encode("utf-8"))


def read_json(path: Path, default: Any = None) -> Any:
    """Parse *path* as JSON, returning *default* when it is missing or invalid.

    A catalog or state file is operator-owned: a malformed one must degrade to
    the default rather than 500 the page that reads it.  A missing file is not
    worth a log line — it is the normal state of an optional overlay — but a
    file that exists and cannot be parsed is.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return default
    except OSError as exc:
        logger.warning("cannot read %s: %s", path, exc)
        return default
    try:
        return json.loads(text)
    except ValueError as exc:
        logger.warning("ignoring malformed %s: %s", path, exc)
        return default


__all__ = ["atomic_write_bytes", "read_json", "write_json"]
