# Copyright (c) 2025-2026 Filigran SAS
#
# This file is part of the XTM One Enterprise Edition ("EE") and is
# licensed under the XTM One Enterprise Edition License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

"""Heading sections of a markdown knowledge document.

A knowledge document's stored ``content`` is its source text. A long markdown
source (a runbook of 160,000 characters) cannot be rewritten in one model
output, so the knowledge tools address it by section instead: the outline of
its ATX headings (``#`` to ``######``), one section read verbatim, one section
replaced, or a new section inserted after another, every byte outside the
section kept.

A section runs from its heading line to the next heading of the same or a
higher level (fewer ``#``), or to the end of the source. Lines inside a fenced
code block (```` ``` ```` or ``~~~``) are never headings.

A selector names one heading:

* its exact text (``Rules registry``; a leading ``## `` is ignored, and a
  case-insensitive match is the fallback when no heading matches exactly);
* a heading path, ``Parent > Child``: the heading's text, then the texts of its
  nearest ancestors, outermost first;
* its 1-based index in the outline (``3`` or ``#3``).

A selector matching several headings, or none, raises :class:`SectionError`
with a message naming the offsets of the duplicates or the closest headings, so
the caller can correct itself in one step. This module is pure: no database, no
I/O.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass

_HEADING_RE = re.compile(r"^ {0,3}(#{1,6})[ \t]+(.+?)[ \t]*$")
_CLOSING_HASHES_RE = re.compile(r"[ \t]+#+$")
_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_INDEX_RE = re.compile(r"^#?(\d+)$")
_PATH_SEPARATOR = ">"
_CLOSEST_HINTS = 3


class SectionError(ValueError):
    """A selector that names no section, or several, or content that is not a section."""


@dataclass(frozen=True)
class Heading:
    """One ATX heading of a source and the range of its section.

    ``start`` is the offset of the heading line, ``end`` the offset where the
    section stops (the next heading of the same or a higher level, or the end
    of the source). ``ancestors`` are the texts of the enclosing headings,
    outermost first.
    """

    index: int
    level: int
    text: str
    start: int
    end: int
    ancestors: tuple[str, ...]

    @property
    def length(self) -> int:
        return self.end - self.start


def _heading_on(line: str) -> tuple[int, str] | None:
    """``(level, text)`` when *line* (without its newline) is an ATX heading."""
    match = _HEADING_RE.match(line)
    if match is None:
        return None
    text = _CLOSING_HASHES_RE.sub("", match.group(2)).strip()
    if not text or set(text) == {"#"}:
        return None
    return len(match.group(1)), text


def parse_outline(source: str) -> list[Heading]:
    """Every heading of *source* outside fenced code, in order, with its section range."""
    found: list[tuple[int, str, int]] = []
    fence: str | None = None
    offset = 0
    for raw_line in source.splitlines(keepends=True):
        line = raw_line.rstrip("\r\n")
        fence_match = _FENCE_RE.match(line)
        if fence is not None:
            if fence_match and fence_match.group(1)[0] == fence[0] and len(fence_match.group(1)) >= len(fence):
                fence = None
        elif fence_match:
            fence = fence_match.group(1)
        else:
            heading = _heading_on(line)
            if heading is not None:
                found.append((heading[0], heading[1], offset))
        offset += len(raw_line)

    headings: list[Heading] = []
    stack: list[tuple[int, str]] = []
    for position, (level, text, start) in enumerate(found):
        end = len(source)
        for next_level, _next_text, next_start in found[position + 1 :]:
            if next_level <= level:
                end = next_start
                break
        while stack and stack[-1][0] >= level:
            stack.pop()
        headings.append(
            Heading(
                index=position + 1,
                level=level,
                text=text,
                start=start,
                end=end,
                ancestors=tuple(t for _lvl, t in stack),
            )
        )
        stack.append((level, text))
    return headings


def _normalise(text: str) -> str:
    return " ".join(text.lstrip("#").split())


def _closest(headings: list[Heading], wanted: str) -> str:
    texts = list(dict.fromkeys(h.text for h in headings))
    close = difflib.get_close_matches(wanted, texts, n=_CLOSEST_HINTS, cutoff=0.0)
    if not close:
        return " The document has no heading."
    listed = "; ".join(f"'{t}'" for t in close)
    return f" Closest headings: {listed}."


def _ambiguous(selector: str, matches: list[Heading]) -> SectionError:
    listed = "; ".join(
        f"#{h.index} at offset {h.start}"
        + (f" (under '{' > '.join(h.ancestors)}')" if h.ancestors else "")
        for h in matches
    )
    return SectionError(
        f"section '{selector}' matches {len(matches)} headings: {listed}. Name one "
        "with its outline index (e.g. '#"
        f"{matches[0].index}') or a heading path 'Parent > {matches[0].text}'."
    )


def find_section(source: str, selector: object) -> Heading:
    """The one heading *selector* names in *source*; :class:`SectionError` otherwise."""
    headings = parse_outline(source)
    raw = str(selector if selector is not None else "").strip()
    if not raw:
        raise SectionError("'section' is empty: give a heading text, a heading path or an outline index.")
    if not headings:
        raise SectionError("the document has no markdown heading, so it has no section to address.")

    index_match = _INDEX_RE.match(raw)
    if index_match is not None:
        index = int(index_match.group(1))
        if 1 <= index <= len(headings):
            return headings[index - 1]
        raise SectionError(f"section index {index} is out of range: the outline has {len(headings)} headings (1 to {len(headings)}).")

    parts = [_normalise(p) for p in raw.split(_PATH_SEPARATOR)] if _PATH_SEPARATOR in raw else [_normalise(raw)]
    if any(not p for p in parts):
        raise SectionError(f"section path '{raw}' has an empty part; write it as 'Parent > Heading'.")
    wanted, parents = parts[-1], parts[:-1]

    def _matches(fold: bool) -> list[Heading]:
        def same(a: str, b: str) -> bool:
            return a.casefold() == b.casefold() if fold else a == b

        hits: list[Heading] = []
        for heading in headings:
            if not same(_normalise(heading.text), wanted):
                continue
            if parents:
                chain = [_normalise(a) for a in heading.ancestors]
                if len(chain) < len(parents) or not all(
                    same(x, y) for x, y in zip(chain[-len(parents) :], parents, strict=True)
                ):
                    continue
            hits.append(heading)
        return hits

    matches = _matches(fold=False) or _matches(fold=True)
    if len(matches) == 1:
        return matches[0]
    if matches:
        raise _ambiguous(raw, matches)
    raise SectionError(f"no heading matches section '{raw}'.{_closest(headings, wanted)}")


def _require_heading_first(content: str) -> None:
    first = next((line for line in content.splitlines() if line.strip()), "")
    if _heading_on(first.rstrip("\r\n")) is None:
        raise SectionError(
            "'content' must be a whole section starting with its markdown heading line "
            "(e.g. '## Title'), so the document keeps its structure. Nothing was changed."
        )


def _terminated(content: str, followed: bool) -> str:
    """*content* ending with a newline when more text follows it."""
    if followed and content and not content.endswith("\n"):
        return f"{content}\n"
    return content


def replace_section(source: str, selector: object, content: str) -> tuple[str, Heading, tuple[int, int]]:
    """*source* with the section *selector* names replaced by *content*.

    Returns ``(new_source, old_heading, (new_start, new_end))``. Every character
    outside the section is kept; a newline is added after *content* when the
    section was followed by more text and *content* does not end with one.
    """
    _require_heading_first(content)
    heading = find_section(source, selector)
    body = _terminated(content, heading.end < len(source))
    new_source = source[: heading.start] + body + source[heading.end :]
    return new_source, heading, (heading.start, heading.start + len(body))


def insert_after(source: str, selector: object, content: str) -> tuple[str, Heading, tuple[int, int]]:
    """*source* with *content* inserted as a new section after the one *selector* names.

    The new section goes at the end of the named section's range (after its
    subsections). Returns ``(new_source, anchor_heading, (new_start, new_end))``.
    """
    _require_heading_first(content)
    heading = find_section(source, selector)
    at = heading.end
    prefix = "\n" if at > 0 and not source[:at].endswith("\n") else ""
    body = prefix + _terminated(content, at < len(source))
    new_source = source[:at] + body + source[at:]
    start = at + len(prefix)
    return new_source, heading, (start, at + len(body))
