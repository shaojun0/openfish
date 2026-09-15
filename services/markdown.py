"""Dependency-free, HTML-escaping Markdown renderer.

The hub publishes ecosystem documentation as plain ``.md`` files.  This module
is the only thing that turns one into HTML, and it deliberately implements a
*small* subset rather than adding a CommonMark parser to a project whose whole
point is a tiny dependency footprint:

==== =====================================================================
``#``..``######``  ATX headings
`` ``` ``/``~~~`` fenced code blocks, with an optional language
``>``   blockquotes
lists   unordered (``-``/``*``/``+``) and ordered (``1.``), one nesting level
tables  GitHub-style pipe tables with alignment
``---``, ``***`` horizontal rules
inline  code spans, bold, italic, strikethrough, links, images, autolinks
==== =====================================================================

**Safety is structural, not filter-based.**  Every character of the source is
HTML-escaped *before* any markup is generated, so raw HTML in a document can
never become live markup — there is no allow-list to get wrong.  A handful of
characters (``<``, ``>``, ``&``) survive as entities inside code spans and
links; the only thing built from them is markup this module emits itself.
Link targets are additionally checked for a dangerous scheme, so ``javascript:``
and ``data:`` URLs are dropped rather than rendered.

The renderer is intentionally *pure*: ``render(text)`` in, an HTML fragment out.
"""

from __future__ import annotations

import html
import re

# ── Block patterns ───────────────────────────────────────────────────

_FENCE_RE = re.compile(r"^\s*(```|~~~)\s*([A-Za-z0-9_+.#-]*)\s*$")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_HR_RE = re.compile(r"^\s*([-*_])(?:\s*\1){2,}\s*$")
_QUOTE_RE = re.compile(r"^\s*>\s?(.*)$")
_LIST_RE = re.compile(r"^(\s*)([-*+]|\d{1,9}\.)\s+(.*)$")
_TABLE_DELIM_RE = re.compile(r"^\s*\|?\s*:?-+:?\s*(?:\|\s*:?-+:?\s*)+\|?\s*$")

# ── Inline patterns ──────────────────────────────────────────────────

_CODE_SPAN_RE = re.compile(r"`([^`]+)`")
_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(\s*([^\s)]+)(?:\s+\"([^\"]*)\")?\s*\)")
_LINK_RE = re.compile(r"\[([^\]]+)\]\(\s*([^\s)]+)(?:\s+\"([^\"]*)\")?\s*\)")
_AUTOLINK_RE = re.compile(r"&lt;((?:https?://|mailto:)[^\s]*?)&gt;")
_BOLD_RE = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*|__(?=\S)(.+?)(?<=\S)__")
_ITALIC_RE = re.compile(r"\*(?=\S)([^*]+?)(?<=\S)\*|_(?=\S)([^_]+?)(?<=\S)_")
_STRIKE_RE = re.compile(r"~~(?=\S)(.+?)(?<=\S)~~")
_HARD_BREAK_RE = re.compile(r"(?: {2,}|\\)\n")

_SCHEME_RE = re.compile(r"^([A-Za-z][A-Za-z0-9+.\-]*):")
_SAFE_SCHEMES = frozenset({"http", "https", "mailto"})

#: Placeholder fence for spans that must not be re-processed (code, links).
_TOKEN = "\x00{}\x00"


def safe_url(url: str) -> str | None:
    """Return *url* when its scheme is safe, otherwise ``None``.

    Relative URLs (``/simple/``, ``#anchor``, ``../up``) have no scheme and are
    always allowed.  A scheme outside :data:`_SAFE_SCHEMES` — ``javascript:``,
    ``data:``, ``vbscript:``, ``file:`` … — is rejected, and the inline pass
    leaves the original text untouched when that happens.
    """
    candidate = url.strip()
    if not candidate:
        return None
    match = _SCHEME_RE.match(candidate)
    if match and match.group(1).lower() not in _SAFE_SCHEMES:
        return None
    return candidate


def _escape(text: str) -> str:
    return html.escape(text, quote=False)


def _attr(value: str) -> str:
    """Escape a value taken from already-HTML-escaped text for a quoted attribute.

    ``&``/``<``/``>`` are entities by this point, so only the quote character
    still needs neutralising — re-escaping would turn ``&amp;`` into
    ``&amp;amp;`` and corrupt every URL that carries a query string.
    """
    return value.replace('"', "&quot;")


def _inline(text: str) -> str:
    """Escape *text* and render the inline markup inside it."""
    return _inline_escaped(_escape(text))


def _inline_escaped(text: str) -> str:
    """Render inline markup inside already-escaped *text*.

    Code spans and links are replaced with placeholder tokens first, so the
    emphasis passes can never reach inside them; the tokens are restored at the
    end.
    """
    tokens: list[str] = []

    def stash(fragment: str) -> str:
        tokens.append(fragment)
        return _TOKEN.format(len(tokens) - 1)

    # 1. Code spans: their content is literal, so stash them untouched.
    text = _CODE_SPAN_RE.sub(lambda m: stash(f"<code>{m.group(1).strip()}</code>"), text)

    # 2. Images before links: `![alt](url)` also matches the link pattern.
    def image(match: re.Match[str]) -> str:
        url = safe_url(match.group(2))
        if url is None:
            return match.group(0)
        alt = _attr(match.group(1))
        title = f' title="{_attr(match.group(3))}"' if match.group(3) else ""
        return stash(f'<img src="{_attr(url)}" alt="{alt}"{title}>')

    text = _IMAGE_RE.sub(image, text)

    def link(match: re.Match[str]) -> str:
        url = safe_url(match.group(2))
        if url is None:
            return match.group(0)
        # The label may itself carry inline markup; the recursion is bounded by
        # the bracket syntax and the text is already escaped.
        label = _inline_escaped(match.group(1))
        title = f' title="{_attr(match.group(3))}"' if match.group(3) else ""
        return stash(f'<a href="{_attr(url)}"{title}>{label}</a>')

    text = _LINK_RE.sub(link, text)

    # 3. Autolinks: `<https://…>` survived escaping as `&lt;https://…&gt;`.
    text = _AUTOLINK_RE.sub(
        lambda m: stash(f'<a href="{_attr(m.group(1))}">{m.group(1)}</a>'),
        text,
    )

    # 4. Emphasis. Bold first, then italic, then strikethrough.
    text = _BOLD_RE.sub(
        lambda m: stash(f"<strong>{m.group(1) or m.group(2)}</strong>"), text
    )
    text = _ITALIC_RE.sub(
        lambda m: stash(f"<em>{m.group(1) or m.group(2)}</em>"), text
    )
    text = _STRIKE_RE.sub(lambda m: stash(f"<del>{m.group(1)}</del>"), text)

    # 5. Restore the stashed spans.
    def restore(match: re.Match[str]) -> str:
        return tokens[int(match.group(1))]

    text = re.sub(r"\x00(\d+)\x00", restore, text)
    return text


def _unwrap_single_paragraph(fragment: str) -> str:
    """``<p>x</p>`` → ``x`` so a tight list item is not padded with a paragraph."""
    if fragment.startswith("<p>") and fragment.endswith("</p>") and fragment.count("<p>") == 1:
        return fragment[3:-4]
    return fragment


def _is_table_start(lines: list[str], index: int) -> bool:
    if index + 1 >= len(lines) or "|" not in lines[index]:
        return False
    return bool(_TABLE_DELIM_RE.match(lines[index + 1]))


def _split_row(row: str) -> list[str]:
    row = row.strip()
    if row.startswith("|"):
        row = row[1:]
    if row.endswith("|"):
        row = row[:-1]
    return [cell.strip() for cell in row.split("|")]


def _alignments(delimiter: str) -> list[str]:
    styles: list[str] = []
    for cell in _split_row(delimiter):
        left, right = cell.startswith(":"), cell.endswith(":")
        if left and right:
            styles.append("center")
        elif right:
            styles.append("right")
        elif left:
            styles.append("left")
        else:
            styles.append("")
    return styles


def _render_table(lines: list[str], index: int) -> tuple[str, int]:
    header = _split_row(lines[index])
    styles = _alignments(lines[index + 1])
    index += 2
    body: list[list[str]] = []
    while index < len(lines) and lines[index].strip() and "|" in lines[index]:
        body.append(_split_row(lines[index]))
        index += 1

    def cell(tag: str, content: str, column: int) -> str:
        style = styles[column] if column < len(styles) else ""
        attr = f' style="text-align:{style}"' if style else ""
        return f"<{tag}{attr}>{_inline(content)}</{tag}>"

    out = ["<table>", "<thead>", "<tr>"]
    out.extend(cell("th", value, i) for i, value in enumerate(header))
    out += ["</tr>", "</thead>", "<tbody>"]
    for row in body:
        out.append("<tr>")
        out.extend(cell("td", value, i) for i, value in enumerate(row))
        out.append("</tr>")
    out += ["</tbody>", "</table>"]
    return "\n".join(out), index


def _render_list(lines: list[str], index: int) -> tuple[str, int]:
    match = _LIST_RE.match(lines[index])
    assert match is not None  # caller checked
    base_indent = len(match.group(1))
    ordered = match.group(2)[0].isdigit()
    tag = "ol" if ordered else "ul"

    items: list[str] = []
    while index < len(lines):
        head = _LIST_RE.match(lines[index])
        if (
            head is None
            or len(head.group(1)) != base_indent
            or head.group(2)[0].isdigit() != ordered
        ):
            break

        content = [head.group(3)]
        index += 1
        while index < len(lines):
            current = lines[index]
            if not current.strip():
                # A blank line belongs to the item only when more-indented
                # content continues after it.
                probe = index + 1
                while probe < len(lines) and not lines[probe].strip():
                    probe += 1
                if probe < len(lines) and (
                    len(lines[probe]) - len(lines[probe].lstrip()) > base_indent
                ):
                    content.append("")
                    index += 1
                else:
                    break
            elif len(current) - len(current.lstrip()) > base_indent:
                content.append(current)
                index += 1
            else:
                break

        dedented: list[str] = []
        for line in content:
            if not line.strip():
                dedented.append("")
            else:
                strip = min(len(line) - len(line.lstrip()), base_indent + 2)
                dedented.append(line[strip:])
        rendered = _render_blocks(dedented).strip()
        items.append(f"<li>{_unwrap_single_paragraph(rendered)}</li>")

    return f"<{tag}>\n" + "\n".join(items) + f"\n</{tag}>", index


def _is_block_start(line: str) -> bool:
    return bool(
        _FENCE_RE.match(line)
        or _HEADING_RE.match(line)
        or _HR_RE.match(line)
        or _QUOTE_RE.match(line)
        or _LIST_RE.match(line)
    )


def _render_blocks(lines: list[str]) -> str:
    out: list[str] = []
    index = 0
    total = len(lines)

    while index < total:
        line = lines[index]
        if not line.strip():
            index += 1
            continue

        fence = _FENCE_RE.match(line)
        if fence:
            language = fence.group(2)
            index += 1
            body: list[str] = []
            while index < total and not _FENCE_RE.match(lines[index]):
                body.append(lines[index])
                index += 1
            index += 1  # consume the closing fence (or run off the end)
            attr = f' class="language-{language}"' if language else ""
            code = _escape(chr(10).join(body))
            out.append(f"<pre><code{attr}>{code}</code></pre>")
            continue

        heading = _HEADING_RE.match(line)
        if heading:
            level = min(len(heading.group(1)), 6)
            out.append(f"<h{level}>{_inline(heading.group(2).strip())}</h{level}>")
            index += 1
            continue

        if _HR_RE.match(line):
            out.append("<hr>")
            index += 1
            continue

        if _QUOTE_RE.match(line):
            quoted: list[str] = []
            while index < total:
                match = _QUOTE_RE.match(lines[index])
                if match:
                    quoted.append(match.group(1))
                    index += 1
                elif not lines[index].strip() and index + 1 < total and _QUOTE_RE.match(lines[index + 1]):
                    quoted.append("")
                    index += 1
                else:
                    break
            inner = _render_blocks(quoted).strip()
            out.append(f"<blockquote>\n{inner}\n</blockquote>")
            continue

        if _is_table_start(lines, index):
            table, index = _render_table(lines, index)
            out.append(table)
            continue

        if _LIST_RE.match(line):
            listing, index = _render_list(lines, index)
            out.append(listing)
            continue

        paragraph = [line]
        index += 1
        while index < total and lines[index].strip() and not _is_block_start(lines[index]):
            paragraph.append(lines[index])
            index += 1
        joined = _HARD_BREAK_RE.sub("<br>\n", "\n".join(paragraph))
        out.append(f"<p>{_inline(joined)}</p>")

    return "\n".join(out)


def render(text: str) -> str:
    """Render a Markdown document to a safe HTML fragment."""
    if not text:
        return ""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    return _render_blocks(normalized.split("\n"))


__all__ = ["render", "safe_url"]
