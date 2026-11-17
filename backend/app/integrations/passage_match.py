# SPDX-License-Identifier: Apache-2.0

"""Finding a quoted passage in a document's text, and what to quote instead.

A review tool anchors a comment on a passage the agent quotes from the text
it read (a Google Doc, a Word document). The quote must match exactly: a
comment placed on a guess lands on the wrong sentence. When it does not,
:func:`near_matches` finds the passages that match once case, typographic
quotes and dashes, and spacing are ignored - the usual ways a quote drifts -
so the refusal can name the exact text to quote. Shared by the Google Docs
tools and the Word comments; never re-spell the matching.
"""

from __future__ import annotations

import re

_FOLD = {
    "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'", "\u2032": "'",
    "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u2033": '"',
    "\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-", "\u2014": "-", "\u2212": "-",
}  # fmt: skip


def folded(text: str) -> tuple[str, list[int]]:
    """*text* without case, typographic quotes and dashes, or runs of spaces,
    with the offset in *text* of every character kept."""
    chars: list[str] = []
    origin: list[int] = []
    in_space = False
    for index, char in enumerate(text):
        char = _FOLD.get(char, char)
        if char.isspace():
            if in_space:
                continue
            char, in_space = " ", True
        else:
            in_space = False
        for fold in char.casefold():
            chars.append(fold)
            origin.append(index)
    return "".join(chars), origin


def near_matches(texts: list[str], needle: str, limit: int = 3) -> list[str]:
    """The exact text of the passages *needle* matches once case, typographic
    quotes and dashes, and spacing are ignored: what to quote instead."""
    wanted = folded(needle)[0].strip()
    found: list[str] = []
    if not wanted:
        return found
    for text in texts:
        fold, origin = folded(text)
        start = fold.find(wanted)
        while start != -1 and len(found) < limit:
            end = start + len(wanted)
            exact = text[origin[start] : origin[end - 1] + 1]
            if exact not in found:
                found.append(exact)
            start = fold.find(wanted, end)
    return found


def exact_matches(texts: list[str], needle: str) -> list[tuple[int, int, int]]:
    """Every exact, non-overlapping occurrence of *needle* in *texts*, in
    order: ``(text index, start, end)``."""
    if not needle:
        return []
    pattern = re.compile(re.escape(needle))
    return [
        (index, match.start(), match.end())
        for index, text in enumerate(texts)
        for match in pattern.finditer(text)
    ]


def passage_context(text: str, start: int, end: int, width: int = 60) -> str:
    """``...before >>passage<< after...``, whitespace collapsed."""

    def clean(part: str) -> str:
        return re.sub(r"\s+", " ", part)

    before = clean(text[max(0, start - width) : start])
    after = clean(text[end : end + width])
    return f"...{before}>>{clean(text[start:end])}<<{after}..."
