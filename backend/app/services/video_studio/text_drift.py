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

"""The text of an AI shot against its start picture.

A video model asked to move a diagram, a badge or a screenshot redraws its
labels, and often garbles them: "Orchestration" becomes "Orcheatration". The
shot then plays a misspelt brand on screen, and nothing said so. After a shot
made from a picture is stored, :func:`check` reads the words of the picture
and of the shot's first and last frames and fails the shot when a word of a
frame is a near miss of a word of the picture: the same word, misdrawn.

The reading is the person's own chat model, asked to transcribe each picture
verbatim, word by word (one call, the three pictures together) - each word
with how surely its letters read, the height of its letters and its box - and
to count the places drawn as text that cannot be read; the verdict is
computed here (:func:`drifted_words`, :func:`unreadable_frames`), never asked
of the model. A word the camera moved off the frame is not a drift, nor is a
word missing from a frame: only a word drawn wrong, or - small text a model
melts into marks, a screen in a photo, a badge - a frame drawing more
unreadable text than the picture it started from. A check that cannot run
(no chat model, an unreadable answer) leaves the shot made, with a note
saying the text was not checked.

**What the start picture itself holds unreadable is not compared.** A
flag on a pole, a distant banner, signage at an angle reads as garbage on the
start picture and as slightly different garbage on the take, and the take
failed for a word that was never readable. The expected words are the start
picture's words read at or above :data:`MIN_CONFIDENCE` and at least
:data:`MIN_HEIGHT_PX` tall (:func:`readable_words`); the others are recorded
as unreadable in the start picture and left out. A frame's word is compared
only when it is of readable size itself (:func:`drifts_of`): small lettering
on the take, garbled or not, is judged by the unreadable count alone, where
the start picture's own small lettering is the baseline. Lettering the take
shows that the start picture has at no confidence is reported as new text
(:func:`new_words`), never a failure - that is what drift usually looks like.
The verdict (:meth:`TextCheck.record`) says the words checked, the words left
out with their confidence, the new text, and for a failure the word, the
frame and its box, cropped by the worker (``frame_qc.TEXT_CHECK``).

A shot whose start picture carries lettering the model redrew is not failed
at once: the worker has the lettering located on the start picture
(:func:`locate_lettering`, the same reading model; the boxes are bounded and
merged here, :func:`lettering_boxes`), draws the picture's own pixels back
there in every frame, the regions left out of the camera's measure
(``kept_regions.keep``), and reads the take again - once. Only a take that
still reads wrong fails, as a text drift.

**Lettering the model invents is a finding of its own (``invented_text``).** A take of a branded booth comes
out with feature pills and card subtitles in gibberish where the start picture had none, or only small soft
text: new text, "reported, never failed" above. The reading now says of every word whether it is a known word
(``known``: a word of a language, a common name or an acronym), and a frame's word that is no known word, no
brand term of the platform's lexicon (``lexicon.brand_lexicon``), no word of the start picture at any
confidence and no number, its letters at least :data:`MIN_INVENTED_HEIGHT_PX` tall at 1080p, is *invented*
(:func:`invented_words`): recorded with its frame and box (:attr:`TextCheck.invented`), cropped by the worker,
said in the take's notes, and a finding of the video's quality check (``quality`` kind ``invented_text``),
blocking unless the shot was asked with ``text_check: false``. The frames read are the first, the middle and
the last (:data:`FRAMES`). The start picture is read the same way before a shot is paid for
(:func:`read_start_picture`, ``clips.plan_clip``): gibberish already in the plate is refused naming its box,
and the words it reads clearly make the prompt's lettering line (:func:`lettering_line`,
:data:`NEGATIVE_LETTERING`).
"""

from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass, field
from typing import Any

from app.core.utils import format_exception_message

logger = logging.getLogger(__name__)

#: Words shorter than this are not compared: a two-letter word misread is noise.
MIN_WORD_LENGTH = 4
#: How many edits, per letter of the picture's word, a frame's word may be off
#: and still read as that word misdrawn (``Orcheatration``: 1 in 13;
#: ``Snteligance``: 3 in 12).
MAX_EDIT_RATIO = 0.34
#: The frames of the shot read, by the name the model is told: first, middle and last.
FRAMES = ("first_frame", "middle_frame", "last_frame")
#: Lettering a take invents under this height, in the pixels of a 1080p frame, is the unreadable count's: pills and
#: subtitles stand taller, the texture of a crowd's badges does not.
MIN_INVENTED_HEIGHT_PX = 10
#: The frame height the invented threshold is stated at.
REFERENCE_HEIGHT_PX = 1080
#: The params key of what the start picture was read to show at plan time (``clips.plan_clip``): ``{"words": [...],
#: "invented": [...]}`` - the prompt's lettering line, and a restart's, without a second reading.
START_LETTERING_KEY = "start_lettering"
#: The negative term a shot made from a lettered picture is sent (:func:`with_negative_lettering`).
NEGATIVE_LETTERING = "invented lettering, gibberish text, new words or labels, misspelled or altered letters"
#: Digits, with the separators a figure carries: ``2026``, ``5,786``, ``3.5``, ``10%``, ``#1``.
_NUMBER = re.compile(r"^[#+-]?\d[\d.,:/%-]*$")
#: A word of the start picture read under this confidence (0-1), or whose letters stand under this many pixels
#: tall there, is unreadable in the start picture: recorded, never compared. A frame's word under the
#: height is not compared either: lettering that small is judged by the unreadable count.
MIN_CONFIDENCE = 0.6
MIN_HEIGHT_PX = 14
_WORD = re.compile(r"[^\W\d_](?:[^\W_]|['-](?=[^\W\d_]))+", re.UNICODE)


@dataclass(frozen=True)
class Word:
    """One word a picture was read to show: its text, how surely it reads, its letters' height and its box (shares).

    *confidence* is 1 and *height* / *box* ``None`` when the reading gave none (an older answer shape).
    """

    text: str
    confidence: float = 1.0
    height: float | None = None
    box: tuple[float, float, float, float] | None = None
    #: Whether the reading knows it as a word (of a language, a common name, an acronym); ``None`` when not said.
    known: bool | None = None

    def height_px(self, picture_height: int) -> float | None:
        """How tall its letters stand in a picture *picture_height* pixels high.

        ``None`` when the reading gave no height, or the picture's height is not known (0): the size is then not held
        against the word.
        """
        return None if self.height is None or picture_height <= 0 else self.height * picture_height

    def readable(self, picture_height: int) -> bool:
        """Whether the word reads in its picture: confident enough, and tall enough to be read."""
        height = self.height_px(picture_height)
        return self.confidence >= MIN_CONFIDENCE and (height is None or height >= MIN_HEIGHT_PX)

    @property
    def tall_at_1080p(self) -> bool:
        """Whether its letters stand :data:`MIN_INVENTED_HEIGHT_PX` or taller in a 1080p frame (unknown: yes)."""
        return self.height is None or self.height * REFERENCE_HEIGHT_PX >= MIN_INVENTED_HEIGHT_PX


@dataclass(frozen=True)
class Drift:
    """A word of the picture drawn wrong in a frame of the shot; *box* where the frame shows it (shares), when read."""

    expected: str
    seen: str
    frame: str
    box: tuple[float, float, float, float] | None = None


@dataclass(frozen=True)
class Excluded:
    """A word of the start picture left out of the comparison: unreadable there."""

    word: str
    confidence: float
    height_px: float | None = None


@dataclass(frozen=True)
class NewText:
    """Lettering a frame shows that the start picture has at no confidence: hallucinated text."""

    word: str
    frame: str
    box: tuple[float, float, float, float] | None = None


@dataclass(frozen=True)
class Invented:
    """A run of letters a picture shows that is no word at all: not known, no brand term, not of the start picture.

    *frame* is the frame it was read in (``start_picture`` for the plate itself, :func:`read_start_picture`);
    *box* its bounds in shares ``(x, y, width, height)``, when read; *crop_file_id* the crop the worker stored.
    """

    word: str
    frame: str
    box: tuple[float, float, float, float] | None = None
    crop_file_id: str | None = None

    def as_dict(self) -> dict:
        return {
            "word": self.word,
            "frame": self.frame,
            **({"box": [round(v, 4) for v in self.box]} if self.box else {}),
            **({"crop_file_id": self.crop_file_id} if self.crop_file_id else {}),
        }

    @classmethod
    def from_dict(cls, raw: Any) -> Invented | None:
        if not isinstance(raw, dict) or not str(raw.get("word") or "").strip():
            return None
        crop = raw.get("crop_file_id")
        return cls(str(raw["word"]), str(raw.get("frame") or ""), _word_box(raw.get("box")), str(crop) if crop else None)

    @property
    def where(self) -> str:
        """``x 0.32-0.68, y 0.20-0.55`` of its box, as the answer names it; empty without one."""
        if self.box is None:
            return ""
        x, y, w, h = self.box
        return f"x {x:.2f}-{x + w:.2f}, y {y:.2f}-{y + h:.2f}"


#: The origin of an unreadable place (:class:`Place`): the start picture's own lettering redrawn, marks drawn where
#: the start picture has no lettering, or a place the reading counted without locating it.
REDRAWN = "redrawn"
INVENTED_MARKS = "invented"
UNLOCATED = "unlocated"
#: The most places one picture's reading keeps: past it, the count stands and the rest go unlocated.
MAX_PLACES = 24
#: The margin, a share of the frame, two boxes may be apart and still be read as the same place.
PLACE_MARGIN = 0.02


@dataclass(frozen=True)
class Place:
    """A place a picture draws as text no one can read: its *box* (shares ``(x, y, width, height)``), when located.

    On a failed take (:func:`unreadable_places`): *frame* and *at* (seconds) where it was read, *take_text* the marks
    as read there, *start_text* the start picture's lettering at that place (empty for none), *origin*
    :data:`REDRAWN`, :data:`INVENTED_MARKS` or :data:`UNLOCATED`, and *crop_file_id* the crop the worker stored.
    """

    frame: str
    box: tuple[float, float, float, float] | None = None
    take_text: str = ""
    start_text: str = ""
    origin: str = UNLOCATED
    at: float | None = None
    crop_file_id: str | None = None

    def as_dict(self) -> dict:
        return {
            "frame": self.frame,
            "at": round(self.at, 2) if self.at is not None else None,
            "box": [round(v, 4) for v in self.box] if self.box else None,
            "start_text": self.start_text,
            "take_text": self.take_text,
            "origin": self.origin,
            "crop_file_id": self.crop_file_id,
        }

    @property
    def where(self) -> str:
        """``middle frame at 2.5 s, x 0.32-0.68, y 0.20-0.55``, as the reason names it."""
        said = self.frame.replace("_", " ")
        if self.at is not None:
            said += f" at {self.at:.1f} s"
        if self.box is not None:
            x, y, w, h = self.box
            said += f", x {x:.2f}-{x + w:.2f}, y {y:.2f}-{y + h:.2f}"
        return said


def _places_said(places: list[Place]) -> str:
    shown = "; ".join(
        p.where + (f' (start picture reads "{p.start_text}")' if p.start_text else "") for p in places[:4]
    )
    return shown + (f" and {len(places) - 4} more" if len(places) > 4 else "")


@dataclass
class TextCheck:
    """What the check found: the drifts, or why it could not tell."""

    checked: bool
    drifts: list[Drift] = field(default_factory=list)
    note: str | None = None
    #: What each picture was read to say (``start_picture``, then the frames): the frame QC's text match.
    texts: dict[str, str] | None = None
    #: ``(frame, places)``: text a frame draws unreadable, past what the start picture itself does.
    unreadable: list[tuple[str, int]] = field(default_factory=list)
    #: The start picture's words compared: read there above the thresholds; ``None`` for every word it reads.
    expected: list[str] | None = None
    #: The start picture's words left out as unreadable there, and the new text the frames show.
    excluded: list[Excluded] = field(default_factory=list)
    hallucinated: list[NewText] = field(default_factory=list)
    #: Lettering the frames invent (:func:`invented_words`): recorded, said, a quality finding; never a failed take.
    invented: list[Invented] = field(default_factory=list)
    #: The frames read, as files (``frame name -> path``): the worker crops a failed word out of them.
    frame_paths: dict[str, str] = field(default_factory=dict)
    #: The rectangle a screen insert covers, blanked before the reading (``x0, x1, y0, y1``); ``None`` for none.
    covered: tuple[float, float, float, float] | None = None
    #: The unreadable places counted in :attr:`unreadable`, one per place (:func:`unreadable_places`); empty for a
    #: check made before they were located.
    places: list[Place] = field(default_factory=list)

    @property
    def covered_note(self) -> str | None:
        """The note a take read with its screen blanked carries; ``None`` when nothing was covered."""
        if self.covered is None or not self.checked:
            return None
        x0, x1, y0, y1 = self.covered
        return (
            f"Text check: lettering on the screen (x {x0:.2f}-{x1:.2f}, y {y0:.2f}-{y1:.2f}) is covered by the insert "
            "and was not read - it never fails the take."
        )

    @property
    def failed(self) -> bool:
        """Whether the shot redrew the picture's text: a word misdrawn, or text turned unreadable."""
        return bool(self.drifts or self.unreadable)

    @property
    def expected_text(self) -> str:
        """The start picture's words the check compared, as text: what the frame QC's text match reads."""
        if self.expected is not None:
            return " ".join(self.expected)
        return (self.texts or {}).get("start_picture", "")

    @property
    def failure(self) -> Drift | None:
        """The word a failed take is named by: its first drift; ``None`` for a take failing on unreadable text alone."""
        return self.drifts[0] if self.drifts else None

    @property
    def reason(self) -> str:
        """The failure a drifted shot carries, the words and the unreadable places named."""
        found = []
        if self.drifts:
            shown = ", ".join(f'"{d.expected}" reads "{d.seen}" ({d.frame.replace("_", " ")})' for d in self.drifts[:6])
            found.append(shown + (f" and {len(self.drifts) - 6} more" if len(self.drifts) > 6 else ""))
        if self.unreadable:
            places = ", ".join(
                f"{count} place{'s' if count > 1 else ''} in the {frame.replace('_', ' ')}" for frame, count in self.unreadable
            )
            found.append(f"text the start picture reads drawn unreadable ({places})")
        advice = []
        redrawn = [p for p in self.places if p.origin == REDRAWN]
        invented = [p for p in self.places if p.origin == INVENTED_MARKS]
        unlocated = [p for p in self.places if p.origin == UNLOCATED]
        if redrawn:
            advice.append(
                f"{len(redrawn)} place{'s' if len(redrawn) > 1 else ''} redraw the start picture's own lettering "
                f"({_places_said(redrawn)}): keep that lettering from the start picture (keep_regions over its box) "
                "or blur it on the picture"
            )
        if invented:
            advice.append(
                f"{len(invented)} place{'s' if len(invented) > 1 else ''} are new lettering-like marks the model "
                f"invented where the start picture has none ({_places_said(invented)}): remove or simplify the "
                "object that carries them on the start picture, or name such marks in the negative prompt"
            )
        if unlocated:
            advice.append(
                f"{len(unlocated)} place{'s' if len(unlocated) > 1 else ''} the reading counted without locating"
            )
        joined = "; ".join(advice)
        detail = (
            f" {joined[:1].upper()}{joined[1:]}. Each place is in text_check.places with its box and crop."
            if joined
            else ""
        )
        return (
            f"text drift: the video model redrew the text of the start picture - {'; '.join(found)}.{detail} Generate "
            "the shot again with a calmer camera and less motion, or play the picture as a still."
        )

    @property
    def new_text_note(self) -> str | None:
        """The note a take showing new text carries; ``None`` when the frames show none."""
        if not self.hallucinated:
            return None
        shown = ", ".join(f'"{item.word}" ({item.frame.replace("_", " ")})' for item in self.hallucinated[:6])
        more = f" and {len(self.hallucinated) - 6} more" if len(self.hallucinated) > 6 else ""
        return (
            f"The take shows text the start picture does not: {shown}{more} - hallucinated lettering, reported and "
            "not failed; generate the shot again if it must not show it."
        )

    @property
    def invented_note(self) -> str | None:
        """The note a take showing invented lettering carries; ``None`` when the frames show none."""
        if not self.invented:
            return None
        shown = ", ".join(
            f'"{item.word}" ({item.frame.replace("_", " ")}{", " + item.where if item.box else ""})'
            for item in self.invented[:6]
        )
        more = f" and {len(self.invented) - 6} more" if len(self.invented) > 6 else ""
        return (
            f"The take shows invented lettering: {shown}{more} - letters that make no word, no brand term and no word "
            "of the start picture. The video's quality check reports it (invented_text) and a render is refused "
            "unless the shot is asked with text_check: false; generate the shot again, or keep that region from the "
            "start picture (keep_regions)."
        )

    def record(self) -> dict:
        """The take's text check as its frames keep it (``frame_qc.TEXT_CHECK``) and the answer serves it."""
        failure = self.failure
        return {
            "status": ("failed" if self.failed else "passed") if self.checked else "skipped",
            "words_checked": list(self.expected) if self.expected is not None else words(self.expected_text),
            "excluded": [{"word": item.word, "confidence": round(item.confidence, 2)} for item in self.excluded],
            "hallucinated": [{"word": item.word, "frame": item.frame} for item in self.hallucinated],
            "invented": [item.as_dict() for item in self.invented],
            "places": [place.as_dict() for place in self.places],
            "failure": (
                {
                    "word": failure.expected,
                    "seen": failure.seen,
                    "frame": failure.frame,
                    **({"box": [round(v, 4) for v in failure.box]} if failure.box else {}),
                }
                if failure is not None
                else None
            ),
            **({"note": self.note} if self.note else {}),
            **({"covered_by_insert": [round(v, 4) for v in self.covered]} if self.covered is not None else {}),
        }


def words(text: str) -> list[str]:
    """The words of *text* the check compares: letters first, at least :data:`MIN_WORD_LENGTH` long."""
    return [w for w in _WORD.findall(text or "") if len(w) >= MIN_WORD_LENGTH]


def readable_words(read: list[Word], picture_height: int) -> tuple[list[Word], list[Excluded]]:
    """*read* (the start picture's words) split into the words compared and the ones unreadable there.

    A word compared is one of :data:`MIN_WORD_LENGTH` letters or more, read at
    or above :data:`MIN_CONFIDENCE` and at least :data:`MIN_HEIGHT_PX` tall;
    the others are recorded with their confidence and left out.
    """
    kept: list[Word] = []
    left: list[Excluded] = []
    for word in read:
        for text in words(word.text):
            if word.readable(picture_height):
                kept.append(Word(text, word.confidence, word.height, word.box))
            else:
                left.append(Excluded(text, word.confidence, word.height_px(picture_height)))
    return kept, left


def _distance(a: str, b: str) -> int:
    """The Levenshtein distance of *a* and *b*."""
    if len(a) < len(b):
        a, b = b, a
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        current = [i]
        for j, cb in enumerate(b, start=1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (ca != cb)))
        previous = current
    return previous[-1]


def drifted_words(picture_text: str, frames: dict[str, str]) -> list[Drift]:
    """Every word of *frames* that is a near miss of a word of *picture_text*, once per pair.

    A word the picture has is fine; a frame's word that is part of a picture's
    word (cut by the frame's edge) or that contains one (a plural, a word run
    into the next) is not a drift; a word close to none of the picture's is
    something else (an added word, a sign in the scene), not a misdrawn one.
    """
    known = {w.lower() for w in words(picture_text)}
    if not known:
        return []
    found: dict[tuple[str, str], Drift] = {}
    for frame, text in frames.items():
        for seen in words(text):
            drift = _drift_of(seen, known, frame)
            if drift is not None:
                found.setdefault((drift.expected, drift.seen.lower()), drift)
    return list(found.values())


def _drift_of(seen: str, known: set[str], frame: str, box: tuple | None = None) -> Drift | None:
    """*seen* (a frame's word) as the near miss of one of *known* (the picture's words, lower case); ``None`` when it is not."""
    low = seen.lower()
    if low in known:
        return None
    best = min(known, key=lambda w: (_distance(low, w), w))
    if low in best or best in low:
        return None
    edits = _distance(low, best)
    if 0 < edits <= max(1, round(len(best) * MAX_EDIT_RATIO)):
        return Drift(best, seen, frame, box)
    return None


def drifts_of(expected: list[str], frames: dict[str, list[Word]], heights: dict[str, int]) -> list[Drift]:
    """Every word of *frames* (each picture's words) that is a near miss of one of *expected*, once per pair.

    Like :func:`drifted_words`, against the start picture's readable words
    alone; a frame's word whose letters stand under :data:`MIN_HEIGHT_PX` in
    that frame (*heights*: each frame's height in pixels) is not compared -
    lettering that small, garbled or not, is the unreadable count's.
    """
    known = {w.lower() for w in expected}
    if not known:
        return []
    found: dict[tuple[str, str], Drift] = {}
    for frame, read in frames.items():
        for word in read:
            height = word.height_px(heights.get(frame, 0))
            if height is not None and height < MIN_HEIGHT_PX:
                continue
            for seen in words(word.text):
                drift = _drift_of(seen, known, frame, word.box)
                if drift is not None:
                    found.setdefault((drift.expected, drift.seen.lower()), drift)
    return list(found.values())


def new_words(start: list[str], frames: dict[str, list[Word]], heights: dict[str, int]) -> list[NewText]:
    """Lettering the *frames* show that the *start* picture's words (at any confidence) do not hold.

    A frame's word read at or above :data:`MIN_CONFIDENCE`, at least
    :data:`MIN_HEIGHT_PX` tall, that is no word of the start picture, no near
    miss of one (:func:`_drift_of`) and no part of one: hallucinated text, reported.
    """
    known = {w.lower() for w in start}
    found: dict[tuple[str, str], NewText] = {}
    for frame, read in frames.items():
        for word in read:
            if not word.readable(heights.get(frame, 0)):
                continue
            for seen in words(word.text):
                low = seen.lower()
                if low in known or any(low in w or w in low for w in known) or _drift_of(seen, known, frame) is not None:
                    continue
                found.setdefault((low, frame), NewText(seen, frame, word.box))
    return list(found.values())


def brand_terms() -> set[str]:
    """The words of the platform's brand lexicon (``lexicon.brand_lexicon``), lower case: a brand term is never invented.

    Each entry's written form and each word of it: ``XTM One`` gives ``xtm one``, ``xtm`` and ``one``.
    """
    from app.services.video_studio.lexicon import brand_lexicon

    terms: set[str] = set()
    for entry in brand_lexicon().entries:
        terms.add(entry.text.lower())
        terms.update(part.lower() for part in re.findall(r"[^\W_]+", entry.text))
    return terms


def _plain(text: str) -> str:
    """*text* lower case, its letters and digits alone: ``Open-CTI!`` reads ``opencti``."""
    return "".join(ch for ch in text.lower() if ch.isalnum())


def invented_words(start: list[Word], frames: dict[str, list[Word]], brand: set[str]) -> list[Invented]:
    """Lettering the *frames* show that is no word at all, once per word and frame.

    A word the reading does not know (``Word.known`` false; a reading that did
    not say leaves nothing invented) that is no brand term (*brand*,
    :func:`brand_terms`), no number, no word of the *start* picture at any
    confidence - nor part of one, nor a near miss of one (:func:`_drift_of`:
    that is a drift, or a word the start picture itself holds unreadable) -,
    and whose letters stand :data:`MIN_INVENTED_HEIGHT_PX` tall at 1080p
    (``Word.tall_at_1080p``): a pill or a subtitle in gibberish, never the
    texture of a crowd's badges.
    """
    known: set[str] = set()
    for word in start:
        known.add(word.text.lower())
        known.update(part.lower() for part in _WORD.findall(word.text))
    compared = {w for w in known if len(w) >= MIN_WORD_LENGTH}
    found: dict[tuple[str, str], Invented] = {}
    for frame, read in frames.items():
        for word in read:
            if word.known is not False or not word.tall_at_1080p:
                continue
            seen = word.text.strip()
            low, plain = seen.lower(), _plain(seen)
            if not plain or _NUMBER.match(low) or low in brand or plain in brand:
                continue
            if low in known or plain in {_plain(w) for w in known}:
                continue
            if any(low in w or w in low for w in compared):
                continue
            if compared and _drift_of(seen, compared, frame) is not None:
                continue
            found.setdefault((low, frame), Invented(seen, frame, word.box))
    return list(found.values())


def unreadable_frames(unreadable: dict[str, int], picture_text: str) -> list[tuple[str, int]]:
    """``(frame, places)`` of each frame drawing more unreadable text than the start picture, when the picture reads.

    The model counts the places drawn as text that cannot be read; a frame
    showing more of them than the picture garbled its text - the miss
    of :func:`drifted_words`, which only sees a word that still reads.
    """
    if not words(picture_text):
        return []
    baseline = unreadable.get("start_picture", 0)
    return [(frame, count - baseline) for frame in FRAMES if (count := unreadable.get(frame, 0)) > baseline]


def _near(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> bool:
    """Whether boxes *a* and *b* (shares ``(x, y, width, height)``) overlap, :data:`PLACE_MARGIN` apart at most."""
    m = PLACE_MARGIN
    return a[0] - m < b[0] + b[2] and b[0] - m < a[0] + a[2] and a[1] - m < b[1] + b[3] and b[1] - m < a[1] + a[3]


def _illegible_places(reading: Reading, name: str, height: int) -> list[Place]:
    """The places picture *name* draws unreadable, located: the reading's own, then its words read under the thresholds."""
    located = list(reading.places.get(name, []))
    located += [Place(name, word.box, word.text) for word in reading.words_of(name) if not word.readable(height)]
    return located


def unreadable_places(
    reading: Reading,
    heights: dict[str, int],
    garbled: list[tuple[str, int]],
    *,
    duration: float | None = None,
) -> list[Place]:
    """One :class:`Place` for each unreadable place *garbled* counts (``(frame, places past the start picture's)``).

    The places of a frame that lie where the start picture holds no unreadable lettering of its own are taken
    first (the start picture's baseline is what the count already subtracts); each is set against the start
    picture's lettering at that place: words or unreadable marks there make it :data:`REDRAWN` (*start_text* what
    the start picture reads there), none :data:`INVENTED_MARKS`. A count the reading did not locate is completed with
    :data:`UNLOCATED` places, so there are always as many places as the reason counts. *duration* times the frames.
    """
    times = dict(zip(FRAMES, frame_times(duration), strict=True))
    if duration and duration > 0:
        times["last_frame"] = duration
    start_boxes = [(word.box, word.text) for word in reading.words_of("start_picture") if word.box is not None]
    start_places = _illegible_places(reading, "start_picture", heights.get("start_picture", 0))
    baseline = [place.box for place in start_places if place.box is not None]
    out: list[Place] = []
    for frame, count in garbled:
        located = [
            (place.box, place.take_text)
            for place in _illegible_places(reading, frame, heights.get(frame, 0))
            if place.box is not None
        ]
        located.sort(key=lambda item: any(_near(item[0], b) for b in baseline))
        for box, take_text in located[:count]:
            under = " ".join(text for start_box, text in start_boxes if _near(box, start_box))
            marked = any(_near(box, b) for b in baseline)
            origin = REDRAWN if under or marked else INVENTED_MARKS
            out.append(Place(frame, box, take_text, under, origin, times.get(frame)))
        out += [Place(frame, at=times.get(frame)) for _ in range(max(0, count - len(located)))]
    return out


def illegible_counts(reading: Reading, heights: dict[str, int]) -> dict[str, int]:
    """Per picture, the places drawn as text no one can read: the model's count, plus the words read under the thresholds.

    The start picture's own small or soft lettering (its flags, its distant
    signage) is the baseline :func:`unreadable_frames` compares each frame
    with: the same lettering melted on the take is no new failure.
    """
    out: dict[str, int] = {}
    for name in ("start_picture", *FRAMES):
        read = reading.words_of(name)
        out[name] = reading.unreadable.get(name, 0) + sum(1 for word in read if not word.readable(heights.get(name, 0)))
    return out


_INSTRUCTION = (
    "Transcribe the text drawn in each attached image. The images are, in order: {names}. For each image list every "
    'word drawn in it, in reading order, as {{"text": "...", "confidence": 0.0, "height": 0.0, "box": '
    '[x, y, width, height], "known": true}}: text copied exactly as it is drawn, letter by letter (keep misspellings '
    "and invented words as they are, never correct, complete or guess a word, and skip a word the image's edge cuts); "
    "confidence how surely its letters read, from 0 to 1 (1 for crisp, sharp lettering; under 0.5 for lettering that "
    "is small, soft, blurred, at a steep angle or partly hidden, where you would not swear to every letter); height "
    "the height of its letters as a share of the image height; box its bounds as shares of the image width and "
    "height, x and y its top left corner; known true when the text as drawn is a word of a language, a proper name, "
    "a brand or an acronym you recognise, or a number, false when its letters make no word you know (invented, "
    "scrambled or garbled lettering). List as unreadable each place drawn as text - a label, a line, a badge's "
    "words, a tag - whose letters cannot be read at all (melted, scrambled or smeared into shapes), as "
    '{{"box": [x, y, width, height], "text": "..."}}: its bounds as shares of the image width and height, and its '
    "marks as best they read (empty when none does). Answer with one JSON object only, each image's name as a key: "
    '{{"{first}": {{"words": [{{"text": "...", "confidence": 0.9, "height": 0.04, "box": [0.1, 0.2, 0.3, 0.05], '
    '"known": true}}], "unreadable": [{{"box": [0.6, 0.7, 0.1, 0.03], "text": ""}}]}}}} (an empty list when it '
    "shows none)."
)


@dataclass(frozen=True)
class Reading:
    """What each picture was read to say, and how many places of it are drawn as text no one can read.

    *words* is each picture's words with their confidence, height and box;
    a reading without them (an older answer shape) has every word
    of *texts* at confidence 1 with no size (:meth:`words_of`).
    """

    texts: dict[str, str]
    unreadable: dict[str, int]
    words: dict[str, list[Word]] = field(default_factory=dict)
    #: Each picture's unreadable places, located (:class:`Place`, *box* and *take_text*); absent from an older answer
    #: shape, which only counted them.
    places: dict[str, list[Place]] = field(default_factory=dict)

    def words_of(self, name: str) -> list[Word]:
        """The words picture *name* was read to show, sized when the reading sized them."""
        if name in self.words:
            return self.words[name]
        return [Word(text) for text in words(self.texts.get(name, ""))]


def _count(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _share(value: Any, *, default: float | None = None) -> float | None:
    if isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value):
        return max(0.0, min(1.0, float(value)))
    return default


def _word_box(raw: Any) -> tuple[float, float, float, float] | None:
    if not isinstance(raw, list | tuple) or len(raw) != 4:
        return None
    values = [_share(v) for v in raw]
    if any(v is None for v in values):
        return None
    x, y, width, height = values
    return (x, y, min(width, 1.0 - x), min(height, 1.0 - y)) if width > 0 and height > 0 else None


def _places_of(name: str, raw: Any) -> tuple[int, list[Place]]:
    """``(count, located places)`` of one picture's ``unreadable``: a list of ``{box, text}``, or a bare count."""
    if not isinstance(raw, list):
        return _count(raw), []
    places: list[Place] = []
    for item in raw[:MAX_PLACES]:
        if isinstance(item, dict):
            places.append(Place(name, _word_box(item.get("box")), str(item.get("text") or "").strip()[:80]))
        else:
            places.append(Place(name))
    return len(raw), [p for p in places if p.box is not None]


def _words_of(raw: Any) -> list[Word] | None:
    """The ``words`` list of one picture's reading, bounded; ``None`` when the answer has none (an older shape)."""
    if not isinstance(raw, list):
        return None
    out: list[Word] = []
    for item in raw:
        if isinstance(item, str):
            out.append(Word(item))
            continue
        if not isinstance(item, dict) or not str(item.get("text") or "").strip():
            continue
        box = _word_box(item.get("box"))
        height = _share(item.get("height"))
        if height is None and box is not None:
            height = box[3]
        known = item.get("known")
        out.append(
            Word(
                str(item["text"]).strip(),
                _share(item.get("confidence"), default=1.0),
                height,
                box,
                known if isinstance(known, bool) else None,
            )
        )
    return out


def _parse(content: str, names: list[str]) -> Reading | None:
    text = (content or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("\n") + 1 :] if "\n" in text else text
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    texts: dict[str, str] = {}
    unreadable: dict[str, int] = {}
    read: dict[str, list[Word]] = {}
    located: dict[str, list[Place]] = {}
    for name in names:
        value = data.get(name)
        if isinstance(value, dict):
            listed = _words_of(value.get("words"))
            if listed is not None:
                read[name] = listed
                texts[name] = " ".join(word.text for word in listed)
            else:
                texts[name] = str(value.get("text") or "")
            unreadable[name], located[name] = _places_of(name, value.get("unreadable"))
        elif isinstance(value, list):
            read[name] = _words_of(value) or []
            texts[name] = " ".join(word.text for word in read[name])
            unreadable[name] = 0
        else:
            texts[name] = str(value or "")
            unreadable[name] = 0
    return Reading(texts, unreadable, read, {name: found for name, found in located.items() if found})


async def transcribe(llm: Any, model: str | None, pictures: dict[str, tuple[bytes, str]]) -> Reading | None:
    """The text of each picture (``name -> (bytes, mime)``), read by *llm* in one call; ``None`` when unreadable."""
    from app.llm.base import ChatMessage, FileRef

    names = list(pictures)
    files = [
        FileRef.from_bytes(data, f"{name}.{mime.rsplit('/', 1)[-1]}", mime) for name, (data, mime) in pictures.items()
    ]
    prompt = _INSTRUCTION.format(names=", ".join(names), first=names[0])
    response = await llm.chat(
        [ChatMessage(role="user", content=prompt, files=files)], model=model, temperature=0.0, max_tokens=3000
    )
    return _parse(response.content, names)


def frame_times(duration: float | None) -> tuple[float | None, ...]:
    """When each of :data:`FRAMES` is cut: the first frame, the middle of *duration* (its first second when the
    duration is unknown), the last frame (``None``: the encoder's last)."""
    middle = duration / 2.0 if duration and duration > 0 else 1.0
    return (0.0, middle, None)


async def check(
    user_id: object,
    picture: tuple[bytes, str],
    video_path: str,
    workdir: str,
    *,
    duration: float | None = None,
    covered: tuple[float, float, float, float] | None = None,
) -> TextCheck:
    """Compare the text of *picture* (the shot's start frame) with the shot's first, middle and last frames.

    Read as the shot's person (the job bound them, ``bind_llm_caller``), with
    their chat model; the model is resolved on a short session of its own.
    *duration* places the middle frame (:func:`frame_times`). *covered* is the
    rectangle a screen insert draws over (``insert_keep_out.covered_box``:
    ``x0, x1, y0, y1``, shares of the frame): it is blanked on the picture and
    on every frame before they are read (:func:`blank_region`), so lettering the
    insert covers never fails the take (dev:video-text-check-under-insert).
    """
    from pathlib import Path

    from app.core.database import async_session
    from app.llm.router import get_llm_for_user
    from app.services.video_studio import encoder

    frames: dict[str, tuple[bytes, str]] = {}
    paths: dict[str, str] = {}
    for name, at in zip(FRAMES, frame_times(duration), strict=True):
        out = Path(workdir) / f"text-check-{name}.jpg"
        try:
            await encoder.extract_still(video_path, at=at, out_path=str(out), max_side=1568)
        except encoder.EncoderError as exc:
            return TextCheck(False, note=f"The text of the shot was not checked: no {name.replace('_', ' ')} ({exc}).")
        data = out.read_bytes()
        if covered is not None:
            data = blank_region(data, covered)
            out.write_bytes(data)
        frames[name] = (data, "image/jpeg")
        paths[name] = str(out)
    if covered is not None:
        blanked = blank_region(picture[0], covered)
        picture = (blanked, "image/jpeg") if blanked is not picture[0] else picture
    try:
        async with async_session() as db:
            llm, model = await get_llm_for_user(db, str(user_id))
        reading = await transcribe(llm, model, {"start_picture": picture, **frames})
    except Exception as exc:  # advisory: a shot is never lost because its text could not be read
        logger.info("Text check of a shot not run: %s", format_exception_message(exc))
        return TextCheck(False, note=f"The text of the shot was not checked: {format_exception_message(exc)[:200]}")
    if reading is None:
        return TextCheck(False, note="The text of the shot was not checked: the reading came back unusable.")
    heights = {"start_picture": picture_height(picture[0]), **{name: picture_height(data) for name, (data, _m) in frames.items()}}
    found = verdict(reading, heights, frame_paths=paths, duration=duration)
    found.covered = covered
    return found


#: The flat grey a covered rectangle is painted with before it is read: no letter, no key colour.
BLANK_RGB = (128, 128, 128)


def blank_region(data: bytes, covered: tuple[float, float, float, float]) -> bytes:
    """The picture *data* with the rectangle *covered* (``x0, x1, y0, y1``, shares of its width and height) painted
    flat grey, as a JPEG: what a screen insert draws over is never read. *data* as it was when it cannot be decoded."""
    import io

    from PIL import Image, ImageDraw

    x0, x1, y0, y1 = covered
    try:
        with Image.open(io.BytesIO(data)) as opened:
            image = opened.convert("RGB")
    except Exception:  # an unreadable picture is read as it is: the check says what it can
        return data
    width, height = image.size
    box = (round(x0 * width), round(y0 * height), round(x1 * width), round(y1 * height))
    if box[2] <= box[0] or box[3] <= box[1]:
        return data
    ImageDraw.Draw(image).rectangle(box, fill=BLANK_RGB)
    out = io.BytesIO()
    image.save(out, format="JPEG", quality=92)
    return out.getvalue()


def picture_height(data: bytes) -> int:
    """How many pixels tall the picture *data* is; 0 when it cannot be read (a word's height then goes unchecked)."""
    import io

    from PIL import Image

    try:
        with Image.open(io.BytesIO(data)) as opened:
            return int(opened.height)
    except Exception:  # a size the reading cannot give: the thresholds on height are not applied
        return 0


def verdict(
    reading: Reading,
    heights: dict[str, int],
    *,
    frame_paths: dict[str, str] | None = None,
    brand: set[str] | None = None,
    duration: float | None = None,
) -> TextCheck:
    """What *reading* says of the take, decided in code: the drifts, the unreadable text, the new text.

    *heights* is each picture's height in pixels (``start_picture`` and the
    frames); 0 leaves the words' heights unchecked. The start picture's words
    compared are the readable ones (:func:`readable_words`); the frames are
    compared against them alone (:func:`drifts_of`), their unreadable count
    against the start picture's own illegible lettering
    (:func:`illegible_counts`), their new text reported (:func:`new_words`)
    and their invented lettering recorded (:func:`invented_words`; *brand*
    the brand terms, :func:`brand_terms` when not given). Each unreadable place
    counted is located (:func:`unreadable_places`, *duration* timing the frames).
    """
    texts = reading.texts
    start_height = heights.get("start_picture", 0)
    readable, left = readable_words(reading.words_of("start_picture"), start_height)
    expected = [word.text for word in readable]
    frames = {name: reading.words_of(name) for name in FRAMES}
    drifts = drifts_of(expected, frames, heights)
    garbled = unreadable_frames(illegible_counts(reading, heights), " ".join(expected))
    new = new_words(words(texts.get("start_picture", "")), frames, heights)
    invented = invented_words(reading.words_of("start_picture"), frames, brand if brand is not None else brand_terms())
    return TextCheck(
        True,
        drifts,
        texts=texts,
        unreadable=garbled,
        expected=expected,
        excluded=left,
        hallucinated=new,
        invented=invented,
        frame_paths=dict(frame_paths or {}),
        places=unreadable_places(reading, heights, garbled, duration=duration),
    )


def invented_of(params: dict | None) -> list[Invented]:
    """The invented lettering a take's text check recorded (``frame_qc.TEXT_CHECK`` of its *params*), for the
    video's quality check; empty for a take checked before the record carried it."""
    kept = (params or {}).get("frame_qc")
    record = kept.get("text_check") if isinstance(kept, dict) else None
    raw = record.get("invented") if isinstance(record, dict) else None
    if not isinstance(raw, list):
        return []
    return [item for item in (Invented.from_dict(r) for r in raw) if item is not None]


def crop_word(frame_path: str, box: tuple[float, float, float, float]) -> bytes:
    """The crop of a word at *box* (shares) out of the frame at *frame_path*, grown by half its height, as a JPEG."""
    import io

    from PIL import Image

    with Image.open(frame_path) as opened:
        picture = opened.convert("RGB")
    x, y, width, height = box
    grow = height * 0.5
    left, top = max(0.0, x - grow) * picture.width, max(0.0, y - grow) * picture.height
    right, bottom = min(1.0, x + width + grow) * picture.width, min(1.0, y + height + grow) * picture.height
    crop = picture.crop((round(left), round(top), max(round(left) + 2, round(right)), max(round(top) + 2, round(bottom))))
    out = io.BytesIO()
    crop.save(out, format="JPEG", quality=92)
    return out.getvalue()


# ── The start picture read before a shot is paid for ─────────────────────


@dataclass(frozen=True)
class StartLettering:
    """What the start picture reads at plan time: its readable *words* (the prompt's lettering line) and the
    *invented* lettering it already carries (refused before the shot is paid for)."""

    words: list[str] = field(default_factory=list)
    invented: list[Invented] = field(default_factory=list)
    #: The text regions the take's check holds it to (:func:`text_regions`): ``{text, box, held}``, ``held``
    #: ``compared`` for a word read clearly (a near miss on the take fails it) or ``baseline`` for lettering
    #: unreadable already (the take may garble it, never add more of it).
    regions: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "words": list(self.words),
            "invented": [item.as_dict() for item in self.invented],
            **({"regions": [dict(region) for region in self.regions]} if self.regions else {}),
        }

    @classmethod
    def from_dict(cls, raw: Any) -> StartLettering | None:
        if not isinstance(raw, dict):
            return None
        words = [str(w) for w in raw.get("words") or [] if str(w).strip()]
        invented = [item for item in (Invented.from_dict(r) for r in raw.get("invented") or []) if item is not None]
        regions = [dict(r) for r in raw.get("regions") or [] if isinstance(r, dict) and r.get("box")]
        return cls(words, invented, regions)

    @property
    def refusal(self) -> str | None:
        """Why the plate is refused, naming the first box; ``None`` when it carries no invented lettering."""
        if not self.invented:
            return None
        first = self.invented[0]
        where = f" at {first.where}" if first.box else ""
        more = f" (and {len(self.invented) - 1} more)" if len(self.invented) > 1 else ""
        return (
            f'The start picture shows unreadable lettering{where}: "{first.word}"{more}. Clean the plate, or set '
            "keep_regions over that lettering so it is drawn back from the picture."
        )


def start_lettering(reading: Reading, height: int, *, brand: set[str] | None = None) -> StartLettering:
    """What the start picture of *reading* (read alone, as ``start_picture``) shows, decided in code.

    The readable words (:func:`readable_words`) make the lettering line; a
    word that is no word at all (:func:`invented_words` against an empty
    start, the picture being the start) is invented in the plate.
    """
    read = reading.words_of("start_picture")
    readable, _left = readable_words(read, height)
    invented = invented_words([], {"start_picture": read}, brand if brand is not None else brand_terms())
    gibberish = {item.word.lower() for item in invented}
    seen: list[str] = []
    for word in readable:
        if word.text not in seen and word.text.lower() not in gibberish:
            seen.append(word.text)
    return StartLettering(seen, invented, text_regions(reading, height))


def text_regions(reading: Reading, height: int) -> list[dict]:
    """The regions of the start picture of *reading* the take's text check holds it to, located: each word read
    clearly (``held: compared``) and each place of lettering unreadable already (``held: baseline``), as
    ``{text, box, held}`` with *box* in shares ``[x, y, width, height]``; a word or place read without a box is left
    out. What an author blanks, blurs or keeps (``keep_regions``) before a shot is paid for."""
    out: list[dict] = []
    for word in reading.words_of("start_picture"):
        if word.box is None:
            continue
        held = "compared" if word.readable(height) and words(word.text) else "baseline"
        out.append({"text": word.text, "box": [round(v, 4) for v in word.box], "held": held})
    for place in reading.places.get("start_picture", []):
        if place.box is not None:
            out.append({"text": place.take_text, "box": [round(v, 4) for v in place.box], "held": "baseline"})
    return out[: MAX_PLACES * 2]


async def read_start_picture(user_id: object, picture: tuple[bytes, str]) -> StartLettering | None:
    """The start picture read before a shot is paid for (``clips.plan_clip``); ``None`` when it could not be read."""
    from app.core.database import async_session
    from app.llm.router import get_llm_for_user

    try:
        async with async_session() as db:
            llm, model = await get_llm_for_user(db, str(user_id))
        reading = await transcribe(llm, model, {"start_picture": picture})
    except Exception as exc:  # advisory: a shot is never refused because its plate could not be read
        logger.info("Start picture of a shot not read: %s", format_exception_message(exc))
        return None
    if reading is None:
        return None
    return start_lettering(reading, picture_height(picture[0]))


#: The most words the prompt's lettering line names.
MAX_LETTERING_WORDS = 24


def lettering_line(words: list[str]) -> str | None:
    """The prompt line that holds every printed word of the start picture; ``None`` when it reads none."""
    kept = [w.strip() for w in words if w.strip()][:MAX_LETTERING_WORDS]
    if not kept:
        return None
    return "Every printed word stays exactly: " + ", ".join(f'"{w}"' for w in kept) + ". No other lettering appears."


def with_negative_lettering(negative: str | None) -> str:
    """The negative prompt with :data:`NEGATIVE_LETTERING` added once."""
    base = (negative or "").strip()
    if NEGATIVE_LETTERING in base:
        return base
    return f"{base.rstrip(', ')}, {NEGATIVE_LETTERING}" if base else NEGATIVE_LETTERING


# ── Lettering drawn back from the start picture ──────────────────

#: The params key of the lettering a take had drawn back from its start picture: ``{"boxes": [...], "drawn_file_id"}``.
LETTERING_KEY = "lettering_kept"
#: Around a located block of lettering, the margin drawn back with it, a share of its height: a box read tight
#: would leave the edges of the model's own letters showing.
LETTERING_MARGIN = 0.35
#: A block wider and taller than this share of the picture is the picture itself, not lettering on it.
MAX_LETTERING_SHARE = 0.6

_LOCATE_INSTRUCTION = (
    "The attached image is the start picture of a video shot. Locate every block of text drawn in it - a title, "
    "a label, a line of a badge, a sign, a caption - and answer with one JSON object only: "
    '{"lettering": [{"text": "...", "box": [x, y, width, height]}]}, each box the bounds of one block as shares '
    "of the image width and height from 0 to 1, x and y its top left corner (an empty list when it shows no text)."
)


def reads_lettering(verdict: TextCheck) -> bool:
    """Whether the start picture a check read carries lettering (words the check compares)."""
    return bool(verdict.texts and words(verdict.texts.get("start_picture", "")))


def _box(raw: Any) -> tuple[float, float, float, float] | None:
    """``(x0, y0, x1, y1)`` of one located block, inside the picture; ``None`` for a malformed one."""
    if not isinstance(raw, list | tuple) or len(raw) != 4:
        return None
    if not all(isinstance(v, int | float) and not isinstance(v, bool) for v in raw):
        return None
    x, y, width, height = (float(v) for v in raw)
    x0, y0 = max(0.0, x), max(0.0, y)
    x1, y1 = min(1.0, x + width), min(1.0, y + height)
    if x1 - x0 <= 0.0 or y1 - y0 <= 0.0:
        return None
    return x0, y0, x1, y1


def _union(a: tuple, b: tuple) -> tuple[float, float, float, float]:
    return min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])


def _area(box: tuple) -> float:
    return (box[2] - box[0]) * (box[3] - box[1])


def _overlap(a: tuple, b: tuple) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def lettering_boxes(located: Any) -> list[list[float]]:
    """The blocks a reading located, as the regions a take keeps (``kept_regions``): ``[x, y, width, height]`` shares.

    Each block is grown by :data:`LETTERING_MARGIN` of its height, clipped
    to the picture; one as large as the picture itself is left out
    (:data:`MAX_LETTERING_SHARE`). Overlapping blocks are merged, then the
    closest ones - the pair whose union adds the least area - until they are
    at most ``spec.MAX_KEPT_REGIONS``.
    """
    from app.services.video_studio.spec import MAX_KEPT_REGIONS, kept_boxes

    items = located if isinstance(located, list) else []
    boxes: list[tuple[float, float, float, float]] = []
    for item in items:
        box = _box(item.get("box") if isinstance(item, dict) else item)
        if box is None:
            continue
        x0, y0, x1, y1 = box
        if x1 - x0 > MAX_LETTERING_SHARE and y1 - y0 > MAX_LETTERING_SHARE:
            continue
        grow = (y1 - y0) * LETTERING_MARGIN
        boxes.append((max(0.0, x0 - grow), max(0.0, y0 - grow), min(1.0, x1 + grow), min(1.0, y1 + grow)))
    merged = True
    while merged:
        merged = False
        for i in range(len(boxes)):
            for j in range(i + 1, len(boxes)):
                if _overlap(boxes[i], boxes[j]):
                    boxes[i] = _union(boxes[i], boxes.pop(j))
                    merged = True
                    break
            if merged:
                break
    while len(boxes) > MAX_KEPT_REGIONS:
        pairs = [(i, j) for i in range(len(boxes)) for j in range(i + 1, len(boxes))]
        i, j = min(pairs, key=lambda p: _area(_union(boxes[p[0]], boxes[p[1]])) - _area(boxes[p[0]]) - _area(boxes[p[1]]))
        boxes[i] = _union(boxes[i], boxes.pop(j))
    boxes.sort(key=lambda b: (b[1], b[0]))
    return kept_boxes([[b[0], b[1], b[2] - b[0], b[3] - b[1]] for b in boxes])


def _located(content: str) -> list:
    text = (content or "").strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return []
    try:
        data = json.loads(text[start : end + 1])
    except ValueError:
        return []
    found = data.get("lettering") if isinstance(data, dict) else None
    return found if isinstance(found, list) else []


async def locate_lettering(user_id: object, picture: tuple[bytes, str]) -> list[list[float]]:
    """Where the start *picture* (``(bytes, mime)``) carries lettering: the regions to draw back (:func:`lettering_boxes`).

    Read as the shot's person, with their chat model, like :func:`check`;
    ``[]`` when it could not be read - the shot then fails on its text drift.
    """
    from app.core.database import async_session
    from app.llm.base import ChatMessage, FileRef
    from app.llm.router import get_llm_for_user

    data, mime = picture
    try:
        async with async_session() as db:
            llm, model = await get_llm_for_user(db, str(user_id))
        response = await llm.chat(
            [
                ChatMessage(
                    role="user",
                    content=_LOCATE_INSTRUCTION,
                    files=[FileRef.from_bytes(data, f"start_picture.{mime.rsplit('/', 1)[-1]}", mime)],
                )
            ],
            model=model,
            temperature=0.0,
            max_tokens=1500,
        )
        return lettering_boxes(_located(response.content))
    except Exception as exc:  # advisory: the shot fails on its own text drift instead
        logger.info("The lettering of a start picture was not located: %s", format_exception_message(exc))
        return []
