"""Log-injection defence — escape control characters before they reach a handler.

A log line is a record with a structure — a timestamp, a level, a logger name
and a message — and that structure is held together by newlines.  Any untrusted
value interpolated with ``%s`` can therefore forge a second, entirely fabricated
record: a document id containing ``"\\n2026-01-01 [cpypiserver] CRITICAL: ..."``
is indistinguishable from a real line in ``journalctl`` or a log shipper.  With
``%r`` the value is escaped by ``repr``, but not every call site can use it (the
documentid, a role code and a filesystem path are all logged as plain text on
purpose).

The fix is one choke point rather than an audit of several hundred ``logger.*``
calls: :class:`SanitizingFilter` rewrites the record's message and its string
arguments, and :func:`install` attaches it to the root logger *and* to every
handler already on it.  A handler filter sees records propagated from child
loggers too, which a filter on the child would not; and because the rewrite
happens on ``record.msg``/``record.args`` before formatting, ``%d``/``%r``
placeholders keep working — only ``str`` values are touched.

``\\n``, ``\\r`` and ``\\t`` become their two-character escapes, every other
control character becomes ``\\xNN``.  The result stays on one line and stays
greppable, and an operator reading the log sees exactly what was submitted.
"""

from __future__ import annotations

import logging

#: ``ord(control character) -> replacement``.  Built once; ``str.translate`` is
#: the C-level loop, so a filter on every record costs nothing measurable.
_TRANSLATION = {
    **{code: f"\\x{code:02x}" for code in range(32)},
    127: "\\x7f",
    ord("\n"): "\\n",
    ord("\r"): "\\r",
    ord("\t"): "\\t",
}

#: Marks a record the filter has already rewritten, so a logger and a handler
#: both carrying the filter cannot escape the message twice.
_MARKER = "_cpypiserver_log_sanitized"


def scrub(value: str) -> str:
    """Escape the control characters in *value*; other strings pass through."""
    return value.translate(_TRANSLATION)


def _scrub_arg(value: object) -> object:
    if isinstance(value, str):
        return scrub(value)
    return value


class SanitizingFilter(logging.Filter):
    """Escape control characters in every message this logger emits."""

    def filter(self, record: logging.LogRecord) -> bool:
        if getattr(record, _MARKER, False):
            return True
        record.msg = _scrub_arg(record.msg)
        if isinstance(record.args, dict):
            record.args = {key: _scrub_arg(value) for key, value in record.args.items()}
        elif isinstance(record.args, tuple):
            record.args = tuple(_scrub_arg(value) for value in record.args)
        setattr(record, _MARKER, True)
        return True


def install(logger: logging.Logger | None = None) -> None:
    """Attach :class:`SanitizingFilter` to *logger* (root by default) and handlers.

    Idempotent: ``app.py`` calls it once at import, and a test that re-imports
    the module must not stack a second filter.  ``logging.basicConfig`` puts the
    handler on the root logger, so that is where the filter has to live for
    records from ``cpypiserver.*`` children to be cleaned.
    """
    target = logger if logger is not None else logging.getLogger()
    if not any(isinstance(existing, SanitizingFilter) for existing in target.filters):
        target.addFilter(SanitizingFilter())
    for handler in target.handlers:
        if not any(isinstance(existing, SanitizingFilter) for existing in handler.filters):
            handler.addFilter(SanitizingFilter())


__all__ = ["SanitizingFilter", "install", "scrub"]
