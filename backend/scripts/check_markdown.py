#!/usr/bin/env python
"""Gate: the dependency-free Markdown renderer stays safe and correct.

Run from the backend directory (`backend/`)::

    python scripts/check_markdown.py

``services/markdown.py`` is the only thing that turns an uploaded document into
HTML, so two properties must hold every time it changes:

1. **Composition** — the inline passes must compose.  A code span inside bold is
   still a code span.  This regressed once: ``**upload a `.md` file**`` rendered
   the code span as a bare ``0``, because the placeholder restore ran a single
   ``re.sub`` pass and never rescanned the fragment substituted for the outer
   emphasis token.
2. **Safety** — the renderer escapes the source before emitting markup, so raw
   HTML can never go live and a dangerous link scheme is dropped rather than
   rendered.

It exits non-zero on the first property that fails.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from services.markdown import render  # noqa: E402

#: (label, source, asset_base, fragments that MUST appear, fragments that must NOT)
CASES: list[tuple[str, str, str | None, list[str], list[str]]] = [
    (
        "code span inside bold survives",
        "**upload a `.md` file**",
        None,
        ["<strong>", "<code>.md</code>", "</strong>"],
        ["\x00"],
    ),
    (
        "code span inside italic survives",
        "*use `x` now*",
        None,
        ["<em>", "<code>x</code>", "</em>"],
        ["\x00"],
    ),
    (
        "code span inside strikethrough survives",
        "~~not `this`~~",
        None,
        ["<del>", "<code>this</code>", "</del>"],
        ["\x00"],
    ),
    (
        "link inside bold keeps both",
        "**[docs](https://example.com)**",
        None,
        ["<strong>", '<a href="https://example.com">docs</a>', "</strong>"],
        ["\x00"],
    ),
    (
        "plain code span is untouched",
        "`plain code`",
        None,
        ["<code>plain code</code>"],
        ["\x00"],
    ),
    (
        "raw HTML is escaped, never emitted",
        "<script>alert(1)</script>",
        None,
        ["&lt;script&gt;"],
        ["<script>"],
    ),
    (
        "javascript: link is dropped",
        "[x](javascript:alert(1))",
        None,
        [],
        ["<a", "href="],
    ),
    (
        "data: image is dropped",
        "![x](data:text/html;base64,PHNjcmlwdD4=)",
        None,
        [],
        ["<img"],
    ),
    (
        "relative assets are made absolute when a base is given",
        "![d](assets/diagram.png)",
        "/docs/python/guide/assets",
        ['src="/docs/python/guide/assets/diagram.png"'],
        [],
    ),
    (
        "relative assets stay portable without a base",
        "![d](assets/diagram.png)",
        None,
        ['src="assets/diagram.png"'],
        [],
    ),
]


def main() -> int:
    failures: list[str] = []
    for label, source, base, required, forbidden in CASES:
        html = render(source, asset_base=base)
        missing = [frag for frag in required if frag not in html]
        leaked = [frag for frag in forbidden if frag in html]
        if missing or leaked:
            failures.append(
                f"{label}: missing={missing or '—'} leaked={leaked or '—'} -> {html}"
            )
            print(f"❌ {label}")
        else:
            print(f"✅ {label}")

    if failures:
        print()
        print(f"❌ {len(failures)} markdown check(s) failed")
        return 1
    print()
    print("✅ markdown check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
