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

"""Grounding: the text cut into numbered segments, and quotes found back in it.

A model cannot count characters, so it never gives offsets. It quotes a mention verbatim
and names the numbered segment it read it in; :func:`find_occurrences` finds the quote
back in the source, tolerating what PDF extraction does to text (a line break or a hyphen
inside a word, doubled spaces, typographic quotes and dashes, case). A quote that cannot
be found is not grounded, and an object without a grounded mention is not emitted: the
anti-hallucination rule, enforced in code rather than asked of the model.

Segments are sentences or lines, numbered over the whole document so an id never depends
on the chunk it was shown in; a chunk is a run of consecutive segments under a size.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache

# A sentence ends at ". ", "! ", "? " or a line break; a blank line always ends one.
_SEGMENT_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(\[])|\n\s*\n|(?<=[:;])\n|\n(?=[-*••]\s)")
_MAX_SEGMENT_CHARS = 600
# A period that ends an abbreviation, not a sentence: "U.S. Treasury", "Acme Inc. said".
_ABBREVIATION = re.compile(
    r"(?:\b(?:[A-Za-z]\.){2,}|\b(?:Inc|Corp|Ltd|Co|Dept|Gov|Mr|Mrs|Ms|Dr|St|No|vs|Jan|Feb|Mar|Apr|Jun|"
    r"Jul|Aug|Sep|Sept|Oct|Nov|Dec|approx|incl|etc)\.)$"
)

_EQUIVALENT = {
    "‘": "'", "’": "'", "‚": "'", "‛": "'", "ʼ": "'", "`": "'",
    "“": '"', "”": '"', "„": '"',
    "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "−": "-",
    " ": " ", "​": "", "­": "",
}


@dataclass(frozen=True, slots=True)
class Segment:
    """A sentence or line of the source, with its absolute offsets."""

    index: int
    start: int
    end: int
    text: str


@dataclass(frozen=True, slots=True)
class Chunk:
    """Consecutive segments shown to the model together."""

    index: int
    segments: tuple[Segment, ...]

    @property
    def start(self) -> int:
        return self.segments[0].start

    @property
    def end(self) -> int:
        return self.segments[-1].end

    def numbered(self) -> str:
        """The chunk as the model reads it: one ``[S12] text`` line per segment."""
        return "\n".join(f"[S{seg.index}] {' '.join(seg.text.split())}" for seg in self.segments)


def split_segments(text: str) -> list[Segment]:
    """Cut *text* into sentence-or-line segments, none longer than about 600 chars."""
    segments: list[Segment] = []
    position = 0
    for match in [*_SEGMENT_END.finditer(text), None]:
        if match is not None and "\n" not in match.group(0) and _ABBREVIATION.search(text[position : match.start()]):
            continue  # "U.S. Treasury": the period ends an abbreviation
        end = match.start() if match else len(text)
        _append_segment(segments, text, position, end)
        if match:
            position = match.end()
    return segments


def _append_segment(segments: list[Segment], text: str, start: int, end: int) -> None:
    # Trim surrounding whitespace, keeping the offsets exact.
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    while end - start > _MAX_SEGMENT_CHARS:
        cut = text.rfind(" ", start, start + _MAX_SEGMENT_CHARS)
        cut = cut if cut > start else start + _MAX_SEGMENT_CHARS
        segments.append(Segment(len(segments), start, cut, text[start:cut]))
        start = cut
        while start < end and text[start].isspace():
            start += 1
    if end > start:
        segments.append(Segment(len(segments), start, end, text[start:end]))


def build_chunks(segments: list[Segment], max_chars: int) -> list[Chunk]:
    """Group consecutive segments into chunks of at most *max_chars* characters each
    (a single longer segment makes a chunk of its own)."""
    chunks: list[Chunk] = []
    current: list[Segment] = []
    size = 0
    for segment in segments:
        length = segment.end - segment.start + 1
        if current and size + length > max_chars:
            chunks.append(Chunk(len(chunks), tuple(current)))
            current, size = [], 0
        current.append(segment)
        size += length
    if current:
        chunks.append(Chunk(len(chunks), tuple(current)))
    return chunks


def _normalize_char(char: str) -> str:
    return _EQUIVALENT.get(char, char).casefold()


def is_case_exact(quote: str) -> bool:
    """A short all-caps name (IT, US, HR, WHO, U.S.) is matched only as written: "it" and
    "us" are words, never the sector or the country."""
    letters = [c for c in quote if c.isalpha()]
    return 0 < len(letters) <= 4 and all(c.isupper() for c in letters)


@lru_cache(maxsize=1024)
def _pattern(quote: str, case_sensitive: bool = False) -> re.Pattern[str] | None:
    """A pattern finding *quote* in raw text: any whitespace run (line breaks included)
    for a space, an optional line break or soft hyphen between two characters of a word,
    case and typographic variants folded, whole words at alphanumeric edges."""
    cleaned = "".join(_EQUIVALENT.get(c, c) for c in quote).strip()
    if not cleaned:
        return None
    parts: list[str] = []
    for token in re.findall(r"\s+|\S+", cleaned):
        if token.isspace():
            parts.append(r"\s+")
            continue
        chars = []
        for char in token:
            variants = {char, *(k for k, v in _EQUIVALENT.items() if v == char and v)}
            chars.append(
                re.escape(char) if len(variants) == 1
                else "[" + "".join(re.escape(v) for v in sorted(variants)) + "]"
            )
        parts.append(r"(?:-?\r?\n|­)?".join(chars))
    left = r"(?<![0-9A-Za-z])" if cleaned[0].isalnum() else ""
    right = r"(?![0-9A-Za-z])" if cleaned[-1].isalnum() else ""
    try:
        return re.compile(left + "".join(parts) + right, 0 if case_sensitive else re.IGNORECASE)
    except re.error:
        return None


def find_occurrences(
    text: str, quote: str, start: int = 0, end: int | None = None, *, case_sensitive: bool = False
) -> list[tuple[int, int]]:
    """Every ``(start, end)`` where *quote* occurs in ``text[start:end]``, absolute. A short
    all-caps quote is always matched case-exactly (:func:`is_case_exact`)."""
    pattern = _pattern(quote, case_sensitive or is_case_exact(quote))
    if pattern is None:
        return []
    stop = len(text) if end is None else end
    found = [(m.start(), m.end()) for m in pattern.finditer(text, start, stop)]
    if found or not quote.strip():
        return found
    return _normalized_occurrences(text, quote, start, stop, case_sensitive)


@lru_cache(maxsize=2)
def _normalized(text: str) -> tuple[str, tuple[int, ...]]:
    """*text* in Unicode compatibility form (ligatures, full-width forms, non-breaking
    spaces unfolded), with each character's offset in the original."""
    chars: list[str] = []
    origin: list[int] = []
    for index, char in enumerate(text):
        folded = unicodedata.normalize("NFKC", char)
        chars.append(folded)
        origin.extend([index] * len(folded))
    return "".join(chars), tuple(origin)


def _normalized_occurrences(
    text: str, quote: str, start: int, stop: int, case_sensitive: bool
) -> list[tuple[int, int]]:
    """The occurrences found once both sides are in compatibility form, mapped back."""
    normalized, origin = _normalized(text)
    if len(normalized) == len(text) and normalized == text and unicodedata.normalize("NFKC", quote) == quote:
        return []
    pattern = _pattern(unicodedata.normalize("NFKC", quote), case_sensitive or is_case_exact(quote))
    if pattern is None:
        return []
    found = []
    for match in pattern.finditer(normalized):
        begin, finish = origin[match.start()], origin[match.end() - 1] + 1
        if start <= begin and finish <= stop:
            found.append((begin, finish))
    return found


def ground(text: str, quote: str, window: tuple[int, int] | None) -> tuple[int, int] | None:
    """Where *quote* is: as written in *window* (the cited segment or chunk), then in any
    case there, then as written anywhere, then in any case anywhere; ``None`` when it is
    not in the text at all."""
    for bounds in ((window[0], window[1]),) if window is not None else ():
        for exact in (True, False):
            found = find_occurrences(text, quote, bounds[0], bounds[1], case_sensitive=exact)
            if found:
                return found[0]
    for exact in (True, False):
        found = find_occurrences(text, quote, case_sensitive=exact)
        if found:
            return found[0]
    return None


# Words that qualify a name without being part of it ("the APT-K-47 group", "the defense
# sector", "WalkerShell malware"): two surface forms equal once they are dropped name one
# entity of one family.
_QUALIFIERS = frozenset(
    {
        "the", "a", "an", "group", "groups", "gang", "team", "actor", "actors", "threat",
        "ransomware", "malware", "backdoor", "trojan", "implant", "family", "tool", "toolkit",
        "framework", "sector", "sectors", "industry", "industries", "campaign", "operation",
        "botnet", "variant", "advanced", "persistent",
    }
)


# A letter or digit of any script; an underscore separates words as punctuation does.
_WORD = r"[^\W_]+"


def _folded(text: str) -> str:
    """*text* without case, accents or typographic variants, in any script: ``Café`` and
    ``CAFE`` fold alike, ``日本`` or a Cyrillic name keeps its letters."""
    decomposed = unicodedata.normalize("NFKD", "".join(_normalize_char(c) for c in text))
    return "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()


def core_key(text: str) -> str:
    """:func:`surface_key` without qualifying words; ``""`` when only those remain
    (``The APT-K-47 (Advanced Persistent Threat) group`` → ``aptk47`` needs the id kept:
    a word is dropped only when it is a whole word, so ``APT-K-47`` stays)."""
    words = re.findall(_WORD + "(?:-" + _WORD + ")*", _folded(text))
    kept = [w for w in words if w not in _QUALIFIERS]
    return re.sub(r"[\W_]+", "", "".join(kept))


def surface_key(text: str) -> str:
    """The folded form two surface forms of one name share (case, accents excluded)."""
    return re.sub(r"[\W_]+", "", _folded(text))


__all__ = [
    "Chunk",
    "Segment",
    "build_chunks",
    "core_key",
    "find_occurrences",
    "ground",
    "is_case_exact",
    "split_segments",
    "surface_key",
]
