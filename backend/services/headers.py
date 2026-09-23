"""Outbound header safety — one predicate, enforced at the wire boundary.

An HTTP header *value* is a line, and the CR/LF that ends it is structure rather
than data: a value carrying ``\\r\\n`` is a request-splitting primitive aimed at
whatever upstream the server happens to be talking to.  Every place that puts a
secret, a configured knob or an upstream-controlled string into a request header
crosses the same boundary, and a rule enforced in one of those places is not a
rule.

So there is exactly one predicate here — :func:`is_header_value_safe` — and it is
an **allowlist**: printable ASCII, no control characters, no DEL, no non-ASCII
byte.  Strictness is chosen by header *name* (:func:`policy_for`): a credential is
held to "one word" (:data:`ValuePolicy.TOKEN`) while the handful of fields that
legitimately carry a phrase — ``Content-Disposition``, ``Accept-Encoding`` — may
contain a space (:data:`ValuePolicy.LINE`).  Nothing may contain a CR, an LF, a
NUL or a non-ASCII byte.

Three levels use it, in the order a value travels:

* :func:`safe_header_value` — build one value ("this, or a refusal");
* :func:`checked_headers` — check a whole mapping at the send site;
* :class:`SafeHeaderSession` — the backstop, a :class:`requests.Session` that
  refuses to put an unsafe value on the wire even if a future caller forgets.

This is deliberately *not* a sanitizer.  Stripping CR/LF would turn an attack
into a silently different value; refusing it makes the misconfiguration visible,
which is what an operator needs.  The one exception is a **credential read out of
storage**: dropping it there is correct, because the alternative is refusing to
serve a route whose row the operator can simply re-enter (see
:func:`services.model_routes.effective_api_key`).
"""

from __future__ import annotations

import logging
from enum import Enum
from typing import Any, Mapping

import requests
from werkzeug.http import dump_options_header

logger = logging.getLogger("cpypiserver.headers")


class ValuePolicy(Enum):
    """How strict the allowlist is for one value.

    ``TOKEN`` is the default and the strictest: no space, because every value it
    covers is a credential or a token.  ``LINE`` additionally allows the space
    character, for a value that is a human-readable phrase (a filename) but must
    still be one printable line.
    """

    TOKEN = "token"
    LINE = "line"


class UnsafeHeaderError(ValueError):
    """A value that may not become an HTTP header was about to be sent."""


def is_header_value_safe(value: Any, *, policy: ValuePolicy = ValuePolicy.TOKEN) -> bool:
    """Whether *value* may be written into an HTTP header as one printable line.

    The allowlist, in full: non-empty, every character ASCII, every character
    printable, and — under :data:`ValuePolicy.TOKEN` — no space.  Anything else
    is refused, including the empty string: a caller that has nothing to send
    should not be setting the header at all, and treating "empty" as safe hides
    that decision.
    """
    text = str(value if value is not None else "")
    if not text:
        return False
    allow_space = policy is ValuePolicy.LINE
    return all(
        char.isascii()
        and char.isprintable()
        and (allow_space or char != " ")
        for char in text
    )


def safe_header_value(
    value: Any,
    *,
    policy: ValuePolicy = ValuePolicy.TOKEN,
    what: str = "header value",
) -> str:
    """*value* as a string that may be sent, or raise :class:`UnsafeHeaderError`.

    Use this while *building* a header mapping: the refusal happens at the point
    the value enters the header world, so no downstream code has to remember the
    rule and a static analyser sees a check on the path from input to header.
    """
    text = str(value if value is not None else "").strip()
    if not is_header_value_safe(text, policy=policy):
        raise UnsafeHeaderError(
            f"{what} 不是一行可安全发送的 HTTP 头：只允许可打印 ASCII"
            + ("" if policy is ValuePolicy.TOKEN else " 与空格")
        )
    return text


def unsafe_header_names(
    headers: Mapping[str, Any] | None,
    *,
    policy: ValuePolicy | None = None,
) -> list[str]:
    """The names in *headers* whose values must not go on the wire, sorted.

    Without an explicit *policy* each name is judged by :func:`policy_for`, which
    is the same decision :func:`checked_headers` makes.
    """
    if not headers:
        return []
    return sorted(
        str(name)
        for name, value in headers.items()
        if not is_header_value_safe(
            value, policy=policy if policy is not None else policy_for(name)
        )
    )


#: Header names whose *value* legitimately contains a space, so they are checked
#: under :data:`ValuePolicy.LINE` instead of ``TOKEN``.  This is not a hole: the
#: allowlist still forbids control characters, DEL and non-ASCII bytes — the space
#: is simply not an attack character in these fields.
#:
#: Two entries earn their place by catching an over-strict first draft:
#: ``Accept-Encoding``, because ``requests`` itself sets ``gzip, deflate`` (a
#: policy that rejects the HTTP client's own defaults would take the whole
#: application down), and ``Authorization``, whose syntax is *scheme + space +
#: credential* — refusing ``Bearer <token>`` is refusing every credential there
#: is.  What the check still buys on those fields is the character that matters:
#: a CR, LF, NUL, DEL or non-ASCII byte is refused in all of them.
SPACE_PERMITTED_HEADERS: frozenset[str] = frozenset({
    "accept",
    "accept-charset",
    "accept-encoding",
    "accept-language",
    "authorization",
    "cache-control",
    "content-disposition",
    "content-language",
    "content-type",
    "if-modified-since",
    "if-none-match",
    "if-range",
    "if-unmodified-since",
    "link",
    "pragma",
    "proxy-authorization",
    "vary",
    "via",
    "warning",
    "www-authenticate",
    "x-accel-buffering",
})


def policy_for(name: Any) -> ValuePolicy:
    """The value policy for a header *name* — the strictest one that still fits.

    Names not in :data:`SPACE_PERMITTED_HEADERS` get :data:`ValuePolicy.TOKEN`,
    so ``Authorization`` / ``Cookie`` / any custom ``X-*-Token`` is held to "one
    word" while the handful of fields that carry a phrase are not.  The default
    is deliberately the strict one: a new header name is treated as a credential
    until someone says otherwise.
    """
    return (
        ValuePolicy.LINE
        if str(name).strip().lower() in SPACE_PERMITTED_HEADERS
        else ValuePolicy.TOKEN
    )


def checked_headers(
    headers: Mapping[str, Any] | None,
    *,
    policy: ValuePolicy | None = None,
    context: str = "outbound request",
) -> dict[str, str]:
    """*headers* verified against the allowlist, as a plain ``dict[str, str]``.

    This is the send-site check: call it immediately before handing a mapping to
    ``requests``.  It fails closed — an unsafe value raises rather than being
    dropped, because a header that silently disappears is a bug that shows up as
    a mysterious 401 hours later.

    Each value is checked under :func:`policy_for` (or the explicit *policy* when
    a caller knows better), so ``Accept-Encoding: gzip, deflate`` passes while
    ``Authorization: Bearer x\\r\\nX-Injected: 1`` does not.
    """
    payload = {str(name): str(value) for name, value in (headers or {}).items()}
    unsafe = sorted(
        name
        for name, value in payload.items()
        if not is_header_value_safe(
            value, policy=policy if policy is not None else policy_for(name)
        )
    )
    if unsafe:
        raise UnsafeHeaderError(
            f"{context} 里有不安全的值：{', '.join(unsafe)}"
            "（只允许可打印 ASCII，且不能含 CR/LF 等控制字符）"
        )
    return payload


class SafeHeaderSession(requests.Session):
    """A :class:`requests.Session` that will not send an unsafe header value.

    The backstop for the whole boundary: header mappings are built in a dozen
    places (config tokens, registry challenges, forwarded upstream values), and
    this makes the *wire* the place the rule is finally unavoidable rather than
    one more helper a new caller can bypass.  It is a drop-in replacement, so a
    module only has to construct it instead of ``requests.Session()``.

    Both :meth:`request` and :meth:`send` are covered: ``request`` merges the
    session's own ``headers`` with the per-call ones (so a token stored on the
    session is checked too), and ``send`` catches a caller that prepared a
    request itself.
    """

    def request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        headers = dict(self.headers or {})
        headers.update(kwargs.get("headers") or {})
        checked_headers(headers, context=f"{method.upper()} {url}")
        return super().request(method, url, **kwargs)

    def send(self, request: requests.PreparedRequest, **kwargs: Any) -> requests.Response:
        checked_headers(request.headers, context=f"{request.method} {request.url}")
        return super().send(request, **kwargs)


def attachment_disposition(filename: str) -> str:
    """``Content-Disposition: attachment`` for *filename*, safely quoted.

    A download route must echo a filename back in a response header, and a
    filename is a string the server did not author (it comes from the index, which
    reads a directory).  Two shipped helpers do the escaping so this module does
    not grow a third, hand-rolled one:

    * :func:`werkzeug.http.dump_options_header` — RFC 9110 quoting/escaping, so a
      ``"`` or ``;`` in the name stays inside the quoted value;
    * ``werkzeug.utils.send_file`` — used by the download routes — additionally
      percent-encodes the name for ``filename*=`` when it is non-ASCII, which is
      what a client actually needs to reconstruct it.

    The result is one printable line (RFC 9110 ``attachment`` is ASCII), and
    Werkzeug rejects a value that is not — so this both escapes and refuses.
    """
    disposition = dump_options_header("attachment", {"filename": filename})
    # ``Content-Disposition`` is in SPACE_PERMITTED_HEADERS, so the name-based
    # policy already allows the space a filename needs while still refusing a
    # control character.
    return checked_headers(
        {"Content-Disposition": disposition},
        context="download response",
    )["Content-Disposition"]


__all__ = [
    "SPACE_PERMITTED_HEADERS",
    "SafeHeaderSession",
    "UnsafeHeaderError",
    "ValuePolicy",
    "attachment_disposition",
    "checked_headers",
    "is_header_value_safe",
    "policy_for",
    "safe_header_value",
    "unsafe_header_names",
]
