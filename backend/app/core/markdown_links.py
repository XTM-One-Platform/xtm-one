# SPDX-License-Identifier: Apache-2.0

"""A markdown link whose label is somebody else's text.

A conversation title, a document name: text a person typed, rendered as the
label of a ``[label](url)`` link in an answer the chat displays as markdown.
Unescaped, a title such as ``x](https://evil.example) [y`` forges a second
link, a newline ends the paragraph, a backtick opens code and ``<`` reaches
the HTML layer. The URL here is always one the platform built, so only the
label is user text; it is escaped for every one of those destinations.
"""

from __future__ import annotations

import re

#: Every character markdown or its HTML layer gives a meaning to inside a link
#: label, escaped with a backslash.
_MARKDOWN_SPECIALS = re.compile(r"([\\`*_{}\[\]()<>#+!|~])")
_CONTROL = re.compile(r"[\x00-\x1f\x7f  ]+")


def markdown_link_label(text: str | None, *, fallback: str, max_chars: int = 120) -> str:
    """*text* made safe as a markdown link label: one line, bounded, every
    markdown or HTML special character escaped; *fallback* when it is blank."""
    cleaned = _CONTROL.sub(" ", text or "").strip()
    cleaned = " ".join(cleaned.split())[:max_chars].rstrip()
    return _MARKDOWN_SPECIALS.sub(r"\\\1", cleaned or fallback)


def markdown_link(label: str | None, url: str, *, fallback: str) -> str:
    """``[label](url)`` with *label* escaped. *url* must be one the platform
    built (no user text in it)."""
    return f"[{markdown_link_label(label, fallback=fallback)}]({url})"
