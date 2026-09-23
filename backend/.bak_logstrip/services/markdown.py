"""Markdown → HTML through ``markdown-it-py``.

The hub publishes ecosystem documentation as plain ``.md`` files.  This module
is the only thing that turns one into HTML, and it delegates the parsing to a
real CommonMark implementation rather than re-implementing one: the previous
hand-written renderer had to grow its own emphasis pass, table parser, list
indenter and placeholder stack, and every one of those was a place to be subtly
wrong.  ``markdown-it-py`` is the CommonMark reference behaviour, already
battle-tested, and one dependency replaces ~400 lines of bespoke parser.

Three policy decisions are ours, not the library's:

* **Raw HTML never becomes markup.**  The parser runs with ``html=False``, so
  ``<script>`` in a document is escaped to text.  Safety is structural — there
  is no sanitizer pass and no allow-list to get wrong.
* **Only ``http``, ``https`` and ``mailto`` links survive.**  ``javascript:``,
  ``data:``, ``vbscript:`` and ``file:`` URLs are refused *before* a tag is
  built, so a dangerous link stays visible as literal text.  A rejected link is
  not an error: the author sees their source back and can fix it.
* **Document-relative ``assets/…`` URLs are rewritten** to the absolute
  ``asset_base`` of the document being rendered, so the raw Markdown stays
  portable while the HTML points at the one place the asset is actually served
  from.

The renderer is intentionally *pure*: ``render(text)`` in, an HTML fragment out.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from markdown_it import MarkdownIt
from markdown_it.rules_core import StateCore

#: Link schemes a document may use.  Anything else is dropped.
_SAFE_SCHEMES = frozenset({"http", "https", "mailto"})

#: ``assets/…`` spellings that mean "next to this document".
_ASSET_PREFIXES = ("assets/", "./assets/")

#: markdown-it's own strikethrough rule emits ``<s>``; the rest of the UI
#: expects ``<del>``.
_STRIKE_TAGS = frozenset({"s"})


class _Renderer(MarkdownIt):
    """CommonMark with this project's link policy."""

    def validateLink(self, url: str) -> bool:  # noqa: N802 - markdown-it's spelling
        return safe_url(url) is not None


def safe_url(url: str) -> str | None:
    """Return *url* when its scheme is safe, otherwise ``None``.

    Relative URLs (``/simple/``, ``#anchor``, ``../up``) have no scheme and are
    always allowed.  A scheme outside :data:`_SAFE_SCHEMES` is rejected, and the
    inline pass leaves the original text untouched when that happens.
    """
    candidate = (url or "").strip()
    if not candidate:
        return None
    if urlsplit(candidate).scheme.lower() not in ("", *_SAFE_SCHEMES):
        return None
    return candidate


def _resolve_asset(url: str, asset_base: str | None) -> str:
    """Prefix a document-relative ``assets/…`` URL with its absolute base.

    Any other URL — absolute, scheme-carrying or pointing elsewhere — is left
    exactly as authored.
    """
    if not asset_base:
        return url
    for prefix in _ASSET_PREFIXES:
        if url.startswith(prefix):
            return asset_base.rstrip("/") + "/" + url[len(prefix):]
    return url


def _prepare(state: StateCore) -> None:
    """Core pass: normalise strike tags and resolve asset URLs."""
    asset_base: str | None = state.env.get("asset_base")
    for token in state.tokens:
        if token.type != "inline":
            continue
        for child in token.children or []:
            if child.type == "image":
                _rewrite_attr(child, "src", asset_base)
            elif child.type == "link_open":
                _rewrite_attr(child, "href", asset_base)
            if child.tag in _STRIKE_TAGS:
                child.tag = "del"


def _rewrite_attr(token: Any, name: str, asset_base: str | None) -> None:
    url = token.attrGet(name)
    if url is not None:
        token.attrSet(name, _resolve_asset(url, asset_base))


_MARKDOWN = _Renderer("commonmark", {"html": False})
_MARKDOWN.enable(["table", "strikethrough"])
_MARKDOWN.core.ruler.push("cpypiserver_assets", _prepare)


def render(text: str, asset_base: str | None = None) -> str:
    """Render a Markdown document to a safe HTML fragment.

    *asset_base* is the absolute URL prefix a document's relative ``assets/…``
    references are resolved against (see :func:`_resolve_asset`).  Passing
    ``None`` leaves every URL exactly as authored.
    """
    if not text:
        return ""
    normalized = text.replace("\r\n", "\n").replace("\r", "").replace("\x00", "")
    return _MARKDOWN.render(normalized, {"asset_base": asset_base})


__all__ = ["render", "safe_url"]
