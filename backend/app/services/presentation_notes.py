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

"""Speaker notes as an authoring concern.

A slide's ``notes`` field is what the presenter SAYS, so a deck has a speaking
length - and until now nobody could see it: the agent wrote notes blind (no
signal about which slides had none, nor how long the deck runs read aloud),
the presenter view showed them raw, and the PowerPoint export carried the
inline-markup markers into the notes pane. This module is the one place the
notes are measured and flattened, shared by the tool results, the export and
- by mirroring :data:`WORDS_PER_MINUTE` - the frontend (editor panel, speaker
view, notes handout), so a slide takes the same time to say everywhere.

* :func:`count_words` / :func:`estimate_speaking_seconds` - the speaking
  estimate at a conversational presenting pace.
* :func:`notes_coverage` / :func:`format_notes_coverage` - which content
  slides carry no notes and the deck's estimated total, appended to every
  ``generate_presentation`` / ``get_presentation`` result so the agent knows
  what is still owed without a round-trip.
* :func:`notes_plain_text` - the notes with the markup stripped and the line
  breaks kept, for the PPTX notes pane.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from app.services.presentation_markup import markup_to_plain_text_lenient
from app.tools.wait_policy import format_wait_duration

#: Conversational presenting pace. Read-aloud speech sits between 120 and 150
#: words a minute; 130 is the middle of a deliberate, room-facing delivery.
#: ``frontend/src/components/presentation/speaking-time.ts`` carries the same
#: number - the backend tripwire pins the two.
WORDS_PER_MINUTE = 130

#: Spoken pace of a script written WITHOUT spaces between words (Japanese,
#: Chinese): about 300 characters a minute. A word rule reads a whole Japanese
#: sentence as one word, so those characters are counted one by one and
#: converted to word-equivalents at ``WORDS_PER_MINUTE / CJK_CHARS_PER_MINUTE``
#: each; ``speaking-time.ts`` carries the same figure and the same character
#: class, pinned by the tripwire.
CJK_CHARS_PER_MINUTE = 300

#: Layouts whose slide is structural - a cover, a divider, the agenda, the
#: closing - where notes are welcome but their absence is not a gap. Every
#: other layout is a content slide the presenter has to talk over, and one
#: without notes is reported.
NOTES_OPTIONAL_LAYOUTS: frozenset[str] = frozenset({"title", "section", "agenda", "closing"})

_WORD_RE = re.compile(r"[^\W_]+(?:['\u2019-][^\W_]+)*", re.UNICODE)
#: Han (the unified ideographs, extension A, the compatibility block),
#: Hiragana, Katakana and half-width Katakana. Hangul is left out on purpose:
#: Korean is written with spaces and the word rule above reads it.
_CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff66-\uff9f]")


def count_words(notes: str | None) -> int:
    """Spoken word-equivalents in *notes* (markup markers do not count).

    A space-delimited word is one; a CJK character is
    ``WORDS_PER_MINUTE / CJK_CHARS_PER_MINUTE`` of one, rounded up per string,
    so a Japanese note gets a speaking time instead of reading as one word.
    """
    if not notes:
        return 0
    text = notes_plain_text(notes)
    cjk = len(_CJK_RE.findall(text))
    words = len(_WORD_RE.findall(_CJK_RE.sub(" ", text)))
    if cjk:
        words += math.ceil(cjk * WORDS_PER_MINUTE / CJK_CHARS_PER_MINUTE)
    return words


def estimate_speaking_seconds(notes: str | None) -> int:
    """Seconds it takes to say *notes* at :data:`WORDS_PER_MINUTE` (0 for no notes)."""
    words = count_words(notes)
    if not words:
        return 0
    return max(1, math.ceil(words * 60 / WORDS_PER_MINUTE))


def format_speaking_time(seconds: int) -> str:
    """``45s`` / ``2m`` / ``12m 30s`` / ``1h 5m`` - compact, for a tool result.

    The platform's one compact duration form (``format_wait_duration``,
    mirrored by ``formatDurationShort`` on the frontend), so the estimate the
    agent reads matches the one the editor panel and the speaker view show.
    """
    return format_wait_duration(max(0, int(seconds)))


def notes_plain_text(notes: str | None) -> str:
    """The notes as plain text: markup stripped, line breaks kept.

    This is what goes into the PowerPoint notes pane and what the words are
    counted on. Notes are not validated as markup at save time (a stray ``*``
    or ``<`` in something the presenter only reads must never refuse a deck),
    so the markers are stripped the lenient way the speaker view renders them
    - an unknown tone keeps its text, an unbalanced marker stays as written -
    and the presenter reads the same words on paper, in PowerPoint and on the
    second screen.
    """
    if not notes:
        return ""
    text = markup_to_plain_text_lenient(notes.replace("\r\n", "\n"))
    return "\n".join(line.rstrip() for line in text.split("\n")).strip()


@dataclass(frozen=True, slots=True)
class NotesCoverage:
    """How much of a deck the presenter can speak from.

    ``missing`` holds the 1-based numbers of the content slides (see
    :data:`NOTES_OPTIONAL_LAYOUTS`) that carry no notes; ``total_seconds`` is
    the whole deck's estimated speaking time.
    """

    slide_count: int
    content_count: int
    with_notes: int
    missing: tuple[int, ...] = field(default=())
    total_seconds: int = 0

    @property
    def complete(self) -> bool:
        return not self.missing


def _layout_of(slide: Any) -> str:
    if isinstance(slide, Mapping):
        return str(slide.get("layout") or "")
    return str(getattr(slide, "layout", "") or "")


def _notes_of(slide: Any) -> str | None:
    value = slide.get("notes") if isinstance(slide, Mapping) else getattr(slide, "notes", None)
    return value if isinstance(value, str) else None


def notes_coverage(slides: Iterable[Any]) -> NotesCoverage:
    """Measure *slides* (spec models or their ``dict`` form)."""
    slide_count = 0
    content_count = 0
    with_notes = 0
    missing: list[int] = []
    total = 0
    for index, slide in enumerate(slides, start=1):
        slide_count += 1
        notes = _notes_of(slide)
        has_notes = bool(notes and notes.strip())
        if has_notes:
            with_notes += 1
            total += estimate_speaking_seconds(notes)
        if _layout_of(slide) in NOTES_OPTIONAL_LAYOUTS:
            continue
        content_count += 1
        if not has_notes:
            missing.append(index)
    return NotesCoverage(
        slide_count=slide_count,
        content_count=content_count,
        with_notes=with_notes,
        missing=tuple(missing),
        total_seconds=total,
    )


def _slide_list(numbers: tuple[int, ...], *, limit: int = 12) -> str:
    shown = ", ".join(str(n) for n in numbers[:limit])
    if len(numbers) > limit:
        shown += f" and {len(numbers) - limit} more"
    return shown


def format_notes_coverage(coverage: NotesCoverage) -> str:
    """One ``Speaker notes:`` line for a tool result.

    Empty when the deck has no slides. Names the content slides still without
    notes (bounded), or says the deck is covered, and always gives the
    estimated speaking time so the agent can hold a deck to its slot.
    """
    if not coverage.slide_count:
        return ""
    spoken = format_speaking_time(coverage.total_seconds) if coverage.total_seconds else "0s"
    head = f"Speaker notes: {coverage.with_notes}/{coverage.slide_count} slides, about {spoken} spoken at {WORDS_PER_MINUTE} wpm."
    if coverage.complete:
        return head + " Every content slide has notes."
    noun = "slide" if len(coverage.missing) == 1 else "slides"
    missing = f"Missing on content {noun} {_slide_list(coverage.missing)}."
    if not coverage.with_notes:
        # The first deck of a conversation: say what notes ARE, and still give
        # the count, the (zero) estimate and the slide numbers so the line keeps
        # the shape of every other coverage line and the agent addresses the
        # slides by number like any other time.
        return (
            f"Speaker notes: none yet (0/{coverage.slide_count} slides, about {spoken} spoken "
            f"at {WORDS_PER_MINUTE} wpm). Every content slide should carry notes (what to SAY, "
            f"not the bullets read back) - the presenter reads them in the speaker view. {missing}"
        )
    return f"{head} {missing}"
