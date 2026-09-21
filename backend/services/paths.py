"""Request-path safety — one wrapper around Werkzeug's two path primitives.

Every place the server turns an *external* name into a path under a root used to
re-derive that safety itself: ``services/validation.py`` called
``secure_filename`` and then hand-rolled a ``resolve()``/``relative_to()``
check, ``services/docs.py`` had its own ``_contained``, and the upload route
joined the result straight onto ``PACKAGES_DIR``.  Three implementations of one
concern is exactly what ARCHITECTURE.md forbids, and two of them were subtly
different.

This module is the single implementation.  It does not re-invent either
primitive — both come from Werkzeug, which is already a dependency:

* :func:`safe_name` wraps :func:`werkzeug.utils.secure_filename`, which strips
  directory components, NUL bytes and control characters, and transliterates
  non-ASCII.  It raises ``ValueError`` where the library returns ``""``, so a
  caller gets a rejected upload rather than a write to the root itself.
* :func:`contained` wraps :func:`werkzeug.security.safe_join`, which returns
  ``None`` — never a path outside the root — when a component is absolute or
  contains ``..``.  Getting ``None`` back is the *only* failure mode, so the
  ``None`` check below cannot be forgotten the way a hand-written prefix
  comparison can.

:func:`single_segment` is the stricter third case: a name that is already
trusted to be one component (a document id, an asset name, a tool filename) is
proved to be one here rather than reduced to one, so a caller that expected
``my doc`` does not silently end up with ``my_doc``.
"""

from __future__ import annotations

from pathlib import Path

from werkzeug.security import safe_join
from werkzeug.utils import secure_filename

#: Bytes a POSIX filesystem refuses in a single name.  Checked instead of
#: letting ``open`` raise ``EINVAL``/``ENAMETOOLONG`` deep inside a write.
MAX_NAME_BYTES = 255

_PATH_SEPARATORS = ("/", "\\", "\x00")


def safe_name(filename: str) -> str:
    """Reduce *filename* to a safe basename with :func:`secure_filename`.

    Raises :class:`ValueError` when nothing usable is left, which is the
    difference from the library's ``""``: an empty basename would otherwise
    publish the file as the directory itself.
    """
    name = secure_filename(filename or "")
    if not name:
        raise ValueError("invalid filename")
    if len(name.encode("utf-8")) > MAX_NAME_BYTES:
        raise ValueError("filename is too long")
    return name


def contained(root: str | Path, *parts: str) -> Path:
    """Join *parts* under *root*, refusing anything that escapes it.

    Delegates the comparison to :func:`werkzeug.security.safe_join`, so a
    component that is absolute (``/etc/passwd``), climbs out (``../x``) or
    hides an alternative separator (``a\\..\\b`` on a platform where ``\\``
    separates) yields ``ValueError`` here instead of a path the caller then
    opens.
    """
    joined = safe_join(str(root), *parts)
    if joined is None:
        raise ValueError(f"path escapes {root!r}")
    return Path(joined)


def single_segment(value: str, *, what: str = "name") -> str:
    """Prove *value* is exactly one path segment and return it unchanged.

    Unlike :func:`safe_name` this does not repair the value, so a document id
    that slugifies to something else is a rejected request rather than a silent
    rename.  Raises :class:`ValueError` naming *what* so callers can keep their
    own user-facing wording.
    """
    if not value:
        raise ValueError(f"{what} is empty")
    if len(value.encode("utf-8")) > MAX_NAME_BYTES:
        raise ValueError(f"{what} is too long")
    if value in {".", ".."} or value.startswith("."):
        raise ValueError(f"{what} must not start with a dot")
    if any(sep in value for sep in _PATH_SEPARATORS):
        raise ValueError(f"{what} must not contain a path separator")
    if Path(value).name != value:
        raise ValueError(f"{what} must be a single path segment")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError(f"{what} must not contain control characters")
    return value


__all__ = ["MAX_NAME_BYTES", "contained", "safe_name", "single_segment"]
