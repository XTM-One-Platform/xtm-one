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

"""What a hearing says of the lexicon's terms: the transcript check made lexicon-aware, and the brand check.

A narration is heard back by a speech-to-text (``voice_check`` for each
segment, ``audio_qc`` for the voice of the final render). Before its word
error rate is computed, both sides are tokenised alike (:func:`hear`):

* the narration as written, each term the lexicon's voice reads
  (``lexicon.Lexicon.find``) standing as one token of its own;
* the transcript, where any reading the lexicon accepts for a term the
  narration writes stands as that same token (:func:`reading`): its written
  form, case, punctuation and spaces aside (``Open CTI``, ``open-CTI``; the
  capitals of an acronym kept), its ``say`` form (the
  separators free: ``see tem``, ``seetem``; only when it sounds like the
  entry's IPA), its letters one by one with or without separators or spelled
  by their names (``X T M``, ``ex tee em``), or
  any span of words that sounds like its IPA (``phonetics.reading_cost``,
  Within a weighted distance - a near vowel, a lost ``d`` after ``n``,
  never a tense vowel for a lax one, never a whole phoneme - and at most one
  phoneme off on its consonants and the vowels its words start with:
  ``Phil Igram`` is not ``Filigran``).

A number is read as its words on both sides: written in digits by the
speech-to-text (``XTM 1`` is ``XTM One``) or by the entry, in its written or
its ``say`` form (``Windows 11`` heard as ``Windows eleven``). A term
heard otherwise stays as the words heard and counts
against the WER. The **brand check** is the same alignment read at each term:
the transcript must hold the term's token where the narration has it, and the
voice must say it fluently - a silence of :data:`MAX_TERM_PAUSE` or more inside
the span of its reading (the audio's own pauses, :func:`pauses_in`) fails it,
whatever its words: "Phil ... Egran", "Open ... C-T-I", letters spelled one
at a time. A term missing, heard wrong or broken by a pause fails its
segment whatever the WER (:class:`TermHearing`, ``brand term mispronounced:
<term> heard as '<words>' at m:ss.s``). A term accepted in other words than
the entry writes - neither its written form nor its ``say`` form, case,
punctuation and spaces aside (:func:`plain`) - is said too
(:attr:`TermHearing.heard_otherwise`): the render warns, so a reviewer
reads ``heard 'Philigran'`` without the audio.

A term read as words that its written form, read at sight, spells letter by
letter (``CTEM``, read ``see-tem``: :func:`spelled_syllables`) is held to
the voice's own syllables, never to the transcript alone: a speech-to-text
writes ``CTEM`` for ``see tem`` and for ``C-T-E-M`` alike. The syllable
nuclei of the audio heard (:func:`nuclei_in`: the peaks of its loudness in
the band a vowel is loudest in) are counted inside the span of its reading;
past half-way from the syllables its reading says to those its letters say,
it was read letter by letter, and fails (``CTEM read letter by letter at
0:41.2``) whatever its words.

A **proper noun** no entry stands for (:func:`name_flags`: a capital first,
where no sentence starts with it) has no reading the platform knows:
the voice guesses it and the speech-to-text spells what it heard
(``Mosconi``, ``Moscow knee`` for ``Moscone``). It is matched as the lexicon
matches a term's reading (:func:`says_name`: its written form, or the same
consonants), so it is no word error - and, having no entry, never a brand
check either. Every other word is held to its spelling.
"""

from __future__ import annotations

import functools
import itertools
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from app.services.video_studio import phonetics
from app.services.video_studio.lexicon import CASE_INSENSITIVE, Entry, Lexicon, Segment, named

#: What a term stands as once read: the token both sides share.
TERM = "\x00term:"
#: A silence this long inside a term breaks it (seconds). Measured on spoken takes: every reading heard as broken
#: ("Phil ... Egran", letters spelled one at a time, "Open ... C-T-I") held one of 0.15 s or more, every fluent one
#: 0.13 s at most; silences under ``speech_timing.MIN_PAUSE`` (0.12 s, a stop's closure) are no pause at all.
MAX_TERM_PAUSE = 0.15
#: A pause is inside a term when it starts and ends this far within its span: never the silence around it.
_PAUSE_MARGIN = 0.03
#: The words a term reading may span beyond its ``say`` parts (letters spelled one per word).
_EXTRA_SPAN = 2
_MAX_SPAN = 8
#: The words shown for a term heard wrong.
_HEARD_SHOWN = 6
#: A word as the transcript check reads it: digits (with their separators) or letters.
_PIECE = re.compile(r"\d+(?:[.,]\d+)*|[^\W\d_]+")
_APOSTROPHES = re.compile(r"['\u2019\u02bc]")


@dataclass(frozen=True)
class Piece:
    """A word of a text or a transcript as written, case kept, with when it is said (``0, 0`` for a text)."""

    text: str
    start: float = 0.0
    end: float = 0.0


def _fold(text: str | None) -> str:
    folded = unicodedata.normalize("NFKC", text or "").replace("%", " percent ").replace("&", " and ")
    return _APOSTROPHES.sub(" ", folded)


def pieces_of(words: Sequence[Any] | str | None) -> list[Piece]:
    """The pieces of a text, or of transcribed words (``text`` / ``start`` / ``end``, or tuples), each with its time."""
    if words is None:
        return []
    if isinstance(words, str):
        items: list[tuple[str, float, float]] = [(words, 0.0, 0.0)]
    else:
        items = [
            (w[0], float(w[1]), float(w[2])) if isinstance(w, tuple) else (str(w.text), float(w.start), float(w.end))
            for w in words
        ]
    out: list[Piece] = []
    for text, start, end in items:
        out.extend(Piece(m.group(0).replace(",", ""), start, end) for m in _PIECE.finditer(_fold(text)))
    return out


# ── Proper nouns ──────────────────────────────────────────────────

#: What ends a sentence: the word after it starts one, and a capital there says nothing of a name.
_SENTENCE_END = re.compile(r"[.!?\u2026\n]")
#: A proper noun's sound is held to its consonants (:func:`says_name`): with fewer, it must keep all of them.
NAME_KEY_MIN = 3


def name_flags(text: str, *, sentence_start: bool = True) -> list[bool]:
    """For each piece of *text* (:func:`pieces_of`), whether it is a proper noun no lexicon entry stands for.

    A word of two letters or more with a capital first is one where no
    sentence starts with it. A sentence's first word is one when the word
    after it is one too (``Moscone Center``), or when it is written like a
    coined name or an acronym (``SecOps``, ``SOAR``: ``lexicon.named``).
    *sentence_start*: *text* starts a sentence (``False``: it follows a
    lexicon term in the middle of one).
    """
    folded = _fold(text)
    words: list[str] = []
    starts: list[bool] = []
    cursor, starting = 0, sentence_start
    for match in _PIECE.finditer(folded):
        starting = starting or bool(_SENTENCE_END.search(folded[cursor : match.start()]))
        words.append(match.group(0))
        starts.append(starting)
        starting, cursor = False, match.end()
    capital = [len(word) >= 2 and word.isalpha() and word[0].isupper() for word in words]
    return [
        capital[i]
        and (not starts[i] or named(word) or (i + 1 < len(words) and capital[i + 1] and not starts[i + 1]))
        for i, word in enumerate(words)
    ]


def says_name(written: str, heard: Sequence[str]) -> bool:
    """Whether the *heard* words say the proper noun *written*, as the lexicon accepts a term's reading.

    Its written form, case, punctuation and spaces aside (``Mos cone``), or
    the same sound as English reads both: the same consonants and the vowels
    words start with (``phonetics.key``), at most ``MAX_KEY_EDITS`` apart
    (none under :data:`NAME_KEY_MIN` of them), starting alike - so a word
    before the name is never taken for its first part. The vowels inside are
    free: a name's spelling does not say them, and the speech-to-text spells
    what it heard (``Mosconi``, ``Moscow knee``).
    """
    if not heard:
        return False
    if plain(" ".join(heard)) == plain(written):
        return True
    mine = phonetics.key(phonetics.word_readings(written))
    theirs = phonetics.key(part for word in heard for part in phonetics.word_readings(word))
    if not mine or not theirs or mine[0] != theirs[0]:
        return False
    allowed = phonetics.MAX_KEY_EDITS if len(mine) >= NAME_KEY_MIN else 0
    return phonetics.key_edits(mine, theirs) <= allowed


@dataclass(frozen=True)
class NameHearing:
    """A proper noun of a narration heard by its sound in other words than written."""

    name: str
    heard: str

    def as_dict(self) -> dict:
        return {"name": self.name, "heard": self.heard[:120]}


# ── The readings of a term ────────────────────────────────────────────────


def _written_parts(entry: Entry) -> list[str]:
    return [part for piece in pieces_of(entry.text) for part in phonetics.split_camel(piece.text)]


def _spelled_runs(span: Sequence[Piece]) -> list[str]:
    """The letters *span* writes one per word, run by run (``open C T I``: ``CTI``)."""
    runs = itertools.groupby(span, key=lambda piece: len(piece.text) == 1 and piece.text.isalpha())
    return [letters for single, group in runs if single and len(letters := "".join(p.text for p in group)) >= 2]


def _written(entry: Entry, span: Sequence[Piece]) -> bool:
    """*span* is the written form, case, punctuation and spaces aside: ``Open CTI`` and ``Fili gran`` are.

    The capitals of an acronym stay capitals (unless any case goes): a
    speech-to-text writes ``open aev`` for a word it heard, not three letters.
    And letters written one per word were heard spelled: they are the written
    form only where the entry reads them so (``X T M`` is ``XTM``, ``C T E M``
    is not ``CTEM``, read ``see-tem``).
    """
    want = [word for part in _written_parts(entry) for word in phonetics.number_words(part)]
    written, heard = "".join(want), "".join(piece.text for piece in span)
    if written.casefold() != heard.casefold():
        return False
    spelled = {segment.text.casefold() for segment in entry.segments if segment.letters}
    if any(run.casefold() not in spelled for run in _spelled_runs(span)):
        return False
    acronyms = [(sum(map(len, want[:i])), word) for i, word in enumerate(want) if len(word) >= 2 and word.isupper()]
    if entry.match == CASE_INSENSITIVE or not acronyms:
        return True
    return len(written) == len(heard) and all(heard[at : at + len(word)].isupper() for at, word in acronyms)


def _letter(piece: Piece, letter: str) -> bool:
    """*piece* says *letter*: the letter itself, in any case, or a word that sounds like its name (``tee``)."""
    if piece.text.casefold() == letter.casefold():
        return True
    if len(piece.text) < 2 or piece.text.isupper():
        return False
    name = phonetics.LETTER_PHONEMES.get(letter.upper(), ())
    return phonetics.distance(name, phonetics.word_phonemes(piece.text)) <= phonetics.LETTER_TOLERANCE


@functools.lru_cache(maxsize=1024)
def _said_words(segment: Segment) -> tuple[str, ...]:
    """The words of a word run of a ``say`` form as a hearing holds them: case-folded, a number as its words."""
    return tuple(word.casefold() for piece in pieces_of(segment.text) for word in phonetics.number_words(piece.text))


def _said(entry: Entry, span: Sequence[Piece]) -> bool:
    """*span* is the ``say`` form: its words with any separators, its letters one per word or glued in capitals."""
    segments = entry.segments
    memo: dict[tuple[int, int], bool] = {}

    def go(s: int, p: int) -> bool:
        if (s, p) in memo:
            return memo[(s, p)]
        if s == len(segments):
            result = p == len(span)
        elif segments[s].letters:
            letters = segments[s].text
            result = (p < len(span) and span[p].text == letters and go(s + 1, p + 1)) or (
                p + len(letters) <= len(span)
                and all(_letter(span[p + k], letters[k]) for k in range(len(letters)))
                and go(s + 1, p + len(letters))
            )
        else:
            result, joined, key = False, "", "".join(_said_words(segments[s]))
            for q in range(p, len(span)):
                joined += span[q].text.casefold()
                if not key.startswith(joined):
                    break
                if joined == key and go(s + 1, q + 1):
                    result = True
                    break
        memo[(s, p)] = result
        return result

    return bool(segments) and go(0, 0)


def _as_said(span: Sequence[Piece]) -> list[Piece]:
    """*span* with each number in digits written as its words (``1`` -> ``one``), as the voice said it."""
    return [replace(piece, text=word) for piece in span for word in phonetics.number_words(piece.text)]


def plain(text: str) -> str:
    """*text* case, punctuation and spaces aside, a number as its words: ``Open C-T-I`` and ``OpenCTI`` alike."""
    return "".join(piece.text for piece in _as_said(pieces_of(text))).casefold()


def reading(entry: Entry, span: Sequence[Piece]) -> str | None:
    """Which accepted reading of *entry* *span* is (``written``, ``say``, ``letters``), ``None`` when none exactly.

    The ``say`` form counts only when it sounds like the entry's IPA
    (``Entry.say_agrees``): a voice saying a wrong ``say`` faithfully is
    still heard wrong.
    """
    said = _as_said(span)
    if _written(entry, said):
        return "written"
    if entry.say_agrees and _said(entry, said):
        return "letters" if entry.spells_letters and len(said) > 1 else "say"
    return None


def phonetic_cost(entry: Entry, span: Sequence[Piece]) -> float | None:
    """How far *span* sounds from *entry*'s reading when it says it (``phonetics.reading_cost``); ``None`` when not."""
    heard = [part for piece in _as_said(span) for part in phonetics.word_readings(piece.text)]
    return phonetics.reading_cost(entry.words, heard)


def _span_limit(entry: Entry) -> int:
    parts = sum(len(s.text) if s.letters else len(_said_words(s)) for s in entry.segments)
    return min(_MAX_SPAN, max(parts, len(entry.text.split())) + _EXTRA_SPAN)


def spelled_syllables(entry: Entry) -> int | None:
    """How many syllables *entry*'s written form says read at sight, when that is more than its reading says: its
    capitals read one by one (``phonetics.word_readings``: ``CTEM`` says 4 where ``see-tem`` says 2). ``None`` for an
    entry read letter by letter, or whose written form says no more syllables than its reading: nothing to tell apart."""
    if entry.spells_letters:
        return None
    at_sight = phonetics.syllables(p for word in entry.text.split() for part in phonetics.word_readings(word) for p in part)
    return at_sight if at_sight > phonetics.syllables(entry.phonemes) else None


# ── Both sides tokenised alike ────────────────────────────────────────────


@dataclass(frozen=True)
class Token:
    """A token of the alignment: its normalised word (or a term's :data:`TERM` token) and where it comes from."""

    norm: str
    start: float = 0.0
    end: float = 0.0
    #: The words it stands for, as written or heard.
    raw: str = ""
    #: The term it is; for a heard one, which reading said it.
    entry: Entry | None = None
    reading: str | None = None
    #: The narration it belongs to (the reference side).
    owner: int = 0
    #: A proper noun of the narration no lexicon entry stands for (:func:`name_flags`).
    name: bool = False


def _term(entry: Entry) -> str:
    return TERM + entry.key


def _normalised(pieces: Sequence[Piece], owner: int = 0, names: Sequence[bool] = ()) -> list[Token]:
    """Plain words normalised as the transcript check reads them (``voice_check``: numbers, letters run together).

    *names*: whether each piece is a proper noun; a token of one piece that is keeps it.
    """
    from app.services.video_studio.voice_check import _letters, _numbers

    items = [(piece.text.casefold(), float(i), float(i)) for i, piece in enumerate(pieces)]
    out = []
    for norm, first, last in _letters(_numbers(items)):
        a, b = int(first), int(last)
        raw = " ".join(piece.text for piece in pieces[a : b + 1])
        name = a == b and a < len(names) and names[a]
        out.append(Token(norm, pieces[a].start, pieces[b].end, raw, owner=owner, name=name))
    return out


def reference_tokens(texts: Sequence[str], lexicon: Lexicon) -> list[Token]:
    """The narrations as written, each term the voice reads as its own token, each token tagged with its narration.

    A proper noun no entry stands for is a plain token marked :attr:`Token.name`.
    """
    voice = lexicon.voice()
    out: list[Token] = []

    def plain_words(text: str, owner: int, *, sentence_start: bool) -> list[Token]:
        return _normalised(pieces_of(text), owner, name_flags(text, sentence_start=sentence_start))

    for owner, text in enumerate(texts):
        cursor = 0
        for occurrence in voice.find(text):
            out.extend(plain_words(text[cursor : occurrence.start], owner, sentence_start=cursor == 0))
            out.append(Token(_term(occurrence.entry), raw=occurrence.written, entry=occurrence.entry, owner=owner))
            cursor = occurrence.end
        out.extend(plain_words(text[cursor:], owner, sentence_start=cursor == 0))
    return out


def heard_tokens(words: Sequence[Any] | str, entries: Sequence[Entry]) -> list[Token]:
    """The transcript, each accepted reading of one of *entries* as that term's token (the narration's terms only)."""
    pieces = pieces_of(words)
    ordered = sorted(entries, key=lambda e: (-len(e.key), e.key))
    out: list[Token] = []
    plain: list[Piece] = []
    p = 0
    while p < len(pieces):
        found: tuple[Entry, int, str] | None = None
        for entry in ordered:
            for size in range(1, min(_span_limit(entry), len(pieces) - p) + 1):
                said = reading(entry, pieces[p : p + size])
                if said is not None:
                    found = (entry, size, said)
                    break
            if found is not None:
                break
        if found is None:
            best: tuple[float, int, int] | None = None
            for index, entry in enumerate(ordered):
                for size in range(1, min(_span_limit(entry), len(pieces) - p) + 1):
                    cost = phonetic_cost(entry, pieces[p : p + size])
                    if cost is None:
                        continue
                    score = (cost / max(len(entry.phonemes), 1), size, index)
                    if best is None or score < best:
                        best = score
            if best is not None:
                found = (ordered[best[2]], best[1], "phonetic")
        if found is None:
            plain.append(pieces[p])
            p += 1
            continue
        out.extend(_normalised(plain))
        plain = []
        entry, size, said = found
        span = pieces[p : p + size]
        out.append(Token(_term(entry), span[0].start, span[-1].end, " ".join(s.text for s in span), entry, said))
        p += size
    out.extend(_normalised(plain))
    return out


# ── The brand check ───────────────────────────────────────────────────────


def clock(seconds: float | None) -> str:
    """``0:03.2``: a time as the brand check says it, to the tenth."""
    minutes, rest = divmod(max(0.0, float(seconds or 0.0)), 60.0)
    return f"{int(minutes)}:{rest:04.1f}"


#: How a reviewer is told which reading accepted a term heard otherwise than written (:meth:`TermHearing.notice`).
_READING_WORDS = {"say": "its respelling", "letters": "its letters", "phonetic": "a reading that sounds like its entry"}


@dataclass(frozen=True)
class TermHearing:
    """One term of a narration as it was heard: where, what, and whether the lexicon accepts it."""

    term: str
    passed: bool
    #: The words heard where the narration has it (empty: nothing was heard there).
    heard: str
    #: Seconds into the audio heard; ``None`` when no time is known.
    at: float | None
    #: The narration it is written in (its index among those heard together).
    owner: int = 0
    #: The reading that said it, when its words were right (``written``, ``say``, ``letters``, ``phonetic``).
    reading: str | None = None
    #: The longest silence inside it, in seconds, when that broke it (:data:`MAX_TERM_PAUSE` or more).
    pause: float | None = None
    #: Its words are neither its written form nor its ``say`` form, case, punctuation and spaces aside (:func:`plain`).
    otherwise: bool = False
    #: The syllables the voice said inside it and those its reading says, when that read it letter by letter
    #: (:func:`spelled_syllables`).
    syllables: int | None = None
    reading_syllables: int | None = None

    @property
    def heard_otherwise(self) -> bool:
        """Accepted in other words than the entry writes: a reading that sounds like it, its letters by name."""
        return self.passed and self.otherwise

    def notice(self, *, offset: float = 0.0) -> str:
        """What a reviewer is told of a term :attr:`heard_otherwise`: the words heard, and which reading accepted them."""
        where = f" at {clock(self.at + offset)}" if self.at is not None else ""
        how = _READING_WORDS.get(self.reading or "", self.reading or "")
        return f"brand term {self.term} heard as '{self.heard[:80]}'{where}, not as written: accepted as {how} - listen to it"

    def message(self, *, offset: float = 0.0) -> str:
        where = f" at {clock(self.at + offset)}" if self.at is not None else ""
        if not self.heard:
            return f"brand term mispronounced: {self.term} not heard{where}"
        if self.pause is not None:
            return (
                f"brand term mispronounced: {self.term} spelled with pauses, a {self.pause:.2f} s silence inside "
                f"('{self.heard[:80]}'){where}"
            )
        if self.syllables is not None:
            return (
                f"brand term mispronounced: {self.term} read letter by letter{where} ({self.syllables} syllables heard "
                f"where its reading says {self.reading_syllables})"
            )
        return f"brand term mispronounced: {self.term} heard as '{self.heard[:80]}'{where}"

    def as_dict(self) -> dict:
        out: dict[str, Any] = {"term": self.term, "passed": self.passed, "heard": self.heard[:120]}
        if self.at is not None:
            out["at"] = round(self.at, 3)
        if self.reading:
            out["reading"] = self.reading
        if self.pause is not None:
            out["pause"] = round(self.pause, 3)
        if self.syllables is not None:
            out["syllables"] = {"heard": self.syllables, "reading": self.reading_syllables}
        if self.heard_otherwise:
            out["heard_otherwise"] = True
        return out


def pauses_in(samples: Any, rate: int) -> list[tuple[float, float]]:
    """The silences of *samples* (mono) a voice holds, ``[(start, end)]`` in seconds: the gaps between its voiced runs."""
    from app.services.video_studio.speech_timing import voiced_runs

    return [(before[1], after[0]) for before, after in itertools.pairwise(voiced_runs(samples, rate))]


def longest_pause(start: float, end: float, pauses: Sequence[tuple[float, float]]) -> float:
    """The longest of *pauses* inside ``[start, end]`` (never one at its edges), in seconds to the millisecond; ``0`` with none."""
    inside = [e - s for s, e in pauses if s >= start + _PAUSE_MARGIN and e <= end - _PAUSE_MARGIN]
    return round(max(inside, default=0.0), 3)


#: The band a syllable's vowel is loudest in (Hz): voicing and the first formant, clear of a fricative's hiss and a
#: stop's burst, which would peak on their own.
NUCLEUS_BAND = (150.0, 1000.0)
#: A syllable peaks no more than this under the voice's loud level (dB) ...
NUCLEUS_FLOOR_DB = 25.0
#: ... this far over the dip parting it from the syllable before (dB) ...
NUCLEUS_DIP_DB = 3.0
#: ... and at least this long after it (seconds): two peaks closer are one vowel's.
NUCLEUS_GAP = 0.08
#: The loudness is smoothed over this long (seconds) before its peaks are read: a voice's pitch never makes one.
_NUCLEUS_SMOOTH = 0.05


def nuclei_in(samples: Any, rate: int) -> list[float]:
    """When each syllable of *samples* (mono) peaks, in seconds: the peaks of its loudness in :data:`NUCLEUS_BAND`, every
    ``speech_timing.FRAME_SECONDS``, smoothed over :data:`_NUCLEUS_SMOOTH`, within :data:`NUCLEUS_FLOOR_DB` of its loud
    level, each standing :data:`NUCLEUS_DIP_DB` over the dip before it and :data:`NUCLEUS_GAP` after the peak before
    (else the louder of the two is kept). A syllable has one vowel: the syllables a reading said, on the words' clock."""
    import numpy as np
    from scipy import signal

    from app.services.video_studio.speech_timing import FRAME_SECONDS

    data = np.asarray(samples, dtype=np.float64)
    hop = max(1, round(rate * FRAME_SECONDS))
    count = len(data) // hop
    low, high = NUCLEUS_BAND[0], min(NUCLEUS_BAND[1], rate * 0.45)
    if count < 3 or high <= low:
        return []
    sections = signal.butter(2, [low, high], btype="bandpass", fs=rate, output="sos")
    if len(data) > 3 * (2 * len(sections) + 1):
        data = signal.sosfiltfilt(sections, data)
    power = np.mean(data[: count * hop].reshape(count, hop) ** 2, axis=1)
    width = max(1, round(_NUCLEUS_SMOOTH / FRAME_SECONDS))
    level = 10.0 * np.log10(np.convolve(power, np.ones(width) / width, mode="same") + 1e-12)
    floor = float(np.percentile(level, 95)) - NUCLEUS_FLOOR_DB
    gap = max(1, round(NUCLEUS_GAP / FRAME_SECONDS))
    kept: list[int] = []
    for index in range(1, count - 1):
        if not (level[index] > floor and level[index] >= level[index - 1] and level[index] > level[index + 1]):
            continue
        if kept:
            last = kept[-1]
            dip = float(level[last : index + 1].min())
            if index - last < gap or min(level[last], level[index]) - dip < NUCLEUS_DIP_DB:
                if level[index] > level[last]:
                    kept[-1] = index
                continue
        kept.append(index)
    return [round((index + 0.5) * FRAME_SECONDS, 3) for index in kept]


@dataclass
class Hearing:
    """A transcript held against narrations, both tokenised alike: the alignment and each term as heard."""

    reference: list[Token]
    heard: list[Token]
    alignment: Any
    terms: list[TermHearing] = field(default_factory=list)
    #: The proper nouns heard by their sound in other words than written.
    names: list[NameHearing] = field(default_factory=list)

    @property
    def wer(self) -> float:
        return round(self.alignment.wer, 4)

    @property
    def failed_terms(self) -> list[TermHearing]:
        return [t for t in self.terms if not t.passed]


def _span(to_ref: Sequence[int | None], index: int) -> tuple[int, int, int]:
    """``(low, high, before)``: the transcript tokens heard where reference token *index* stands, bounded by its
    neighbours (none when ``low > high``), and the last one heard for an earlier token (``-1``: none)."""
    mapped = [j for j, ref in enumerate(to_ref) if ref == index]
    before = max((j for j, ref in enumerate(to_ref) if ref is not None and ref < index), default=-1)
    after = min((j for j, ref in enumerate(to_ref) if ref is not None and ref > index), default=len(to_ref))
    if mapped:
        low, high = min(mapped), max(mapped)
        while low - 1 > before and to_ref[low - 1] is None:
            low -= 1
        while high + 1 < after and to_ref[high + 1] is None:
            high += 1
    else:
        low, high = before + 1, after - 1
    return low, high, before


def _around(hearing_tokens: list[Token], to_ref: Sequence[int | None], index: int) -> tuple[str, float | None]:
    """The words heard where reference token *index* stands (bounded by its neighbours), and when."""
    low, high, before = _span(to_ref, index)
    if low > high:
        at = hearing_tokens[before].end if before >= 0 else None
        return "", at
    high = min(high, low + _HEARD_SHOWN - 1)
    return " ".join(t.raw for t in hearing_tokens[low : high + 1]), hearing_tokens[low].start


# ── The word-level diff ───────────────────────────────────────────────────

#: The longest words a word heard otherwise keeps of what was written and of what was heard instead.
_DIFF_KEPT = 60


@dataclass(frozen=True)
class WordHeard:
    """A word of a narration as the transcript check heard it: one entry of the word error rate's diff.

    *written*: as the narration writes it (a lexicon term, a number: one word); empty for words the voice added that
    the narration does not say. *heard*: heard as written. *said*: the words heard in its place when it was not
    (empty: nothing was).
    """

    written: str
    heard: bool
    said: str = ""

    def text(self) -> str:
        """``'Their' heard as 'There'``, ``'cite' not heard``, ``'now' added``: a word heard otherwise, said."""
        if not self.written:
            return f"'{self.said[:_DIFF_KEPT]}' added"
        if not self.said:
            return f"'{self.written[:_DIFF_KEPT]}' not heard"
        return f"'{self.written[:_DIFF_KEPT]}' heard as '{self.said[:_DIFF_KEPT]}'"

    def as_dict(self) -> dict:
        out: dict[str, Any] = {"word": self.written[:_DIFF_KEPT], "heard": self.heard}
        if not self.heard:
            out["said"] = self.said[:_DIFF_KEPT]
        return out

    @classmethod
    def of(cls, raw: object) -> WordHeard | None:
        """The word :meth:`as_dict` kept; ``None`` for one kept in another shape."""
        if not isinstance(raw, dict) or not isinstance(raw.get("word"), str):
            return None
        return cls(raw["word"], bool(raw.get("heard")), str(raw.get("said") or ""))


def word_diff(hearing: Hearing) -> tuple[WordHeard, ...]:
    """Each word of the narration as *hearing*'s alignment heard it, in order, and the words the voice added where it
    added them: the diff behind its word error rate (a lexicon term, a number, a proper noun heard by its sound: one
    word each)."""
    alignment = hearing.alignment
    to_ref = alignment.hypothesis_to_reference
    words: list[WordHeard] = []
    covered: set[int] = set()
    for index, token in enumerate(hearing.reference):
        if alignment.reference_heard[index]:
            words.append(WordHeard(token.raw, True))
            continue
        low, high, _before = _span(to_ref, index)
        covered.update(range(low, high + 1))
        shown = hearing.heard[low : min(high, low + _HEARD_SHOWN - 1) + 1] if low <= high else []
        words.append(WordHeard(token.raw, False, " ".join(t.raw for t in shown)))
    # A word heard where the narration says nothing, next to words heard right: added before the next word written.
    added: dict[int, list[str]] = {}
    last = -1
    for j, ref in enumerate(to_ref):
        if ref is not None:
            last = max(last, ref)
        elif j not in covered:
            added.setdefault(last + 1, []).append(hearing.heard[j].raw)
    out: list[WordHeard] = []
    for index in range(len(words) + 1):
        if index in added:
            out.append(WordHeard("", False, " ".join(added[index][:_HEARD_SHOWN])))
        if index < len(words):
            out.append(words[index])
    return tuple(out)


def diff_line(words: Sequence[WordHeard]) -> str:
    """The narration word by word, each word heard otherwise in brackets: ``['Their' heard as 'There'] platform``."""
    return " ".join(word.written if word.heard else f"[{word.text()}]" for word in words)


def otherwise_line(words: Sequence[WordHeard], *, limit: int = 6) -> str:
    """The first *limit* words heard otherwise, said (``'Their' heard as 'There', 'cite' not heard``), and how many
    more; empty when every word was heard as written."""
    wrong = [word for word in words if not word.heard]
    shown = ", ".join(word.text() for word in wrong[:limit])
    return shown + (f" and {len(wrong) - limit} more" if len(wrong) > limit else "")


def _name_readings(reference: Sequence[Token], heard: Sequence[Token]) -> dict[int, Any]:
    """``{reference index: says(first, count)}`` for each proper noun of *reference*: whether those heard words say it."""
    spans: dict[tuple[int, int], list[str] | None] = {}

    def words(first: int, count: int) -> list[str] | None:
        if (first, count) not in spans:
            run = heard[first : first + count]
            spans[(first, count)] = (
                None if len(run) < count or any(t.entry is not None for t in run) else [w for t in run for w in t.raw.split()]
            )
        return spans[(first, count)]

    def reader(written: str):
        def says(first: int, count: int) -> bool:
            run = words(first, count)
            return run is not None and says_name(written, run)

        return says

    return {index: reader(token.raw) for index, token in enumerate(reference) if token.name}


def _term_heard(
    entry: Entry,
    token: Token,
    pauses: Sequence[tuple[float, float]] | None,
    *,
    timed: bool = True,
    owner: int = 0,
    nuclei: Sequence[float] | None = None,
) -> TermHearing:
    """*entry* heard as the reading *token*: passed, unless the audio holds a pause of :data:`MAX_TERM_PAUSE` inside it,
    or - a term read as words its written form spells (:func:`spelled_syllables`) - more of the syllable *nuclei* inside
    it than half-way from the syllables its reading says to those its letters say."""
    passed, pause, syllables, said = True, None, None, None
    if timed and pauses is not None:
        longest = longest_pause(token.start, token.end, pauses)
        if longest >= MAX_TERM_PAUSE:
            passed, pause = False, longest
    spelled = spelled_syllables(entry) if passed and timed and nuclei is not None else None
    if spelled is not None:
        heard = sum(1 for at in nuclei or () if token.start <= at <= token.end)
        said = phonetics.syllables(entry.phonemes)
        if 2 * heard > said + spelled:
            passed, syllables = False, heard
    otherwise = bool(token.raw) and plain(token.raw) not in (plain(entry.text), plain(entry.say))
    at = token.start if timed else None
    return TermHearing(
        entry.text,
        passed,
        token.raw,
        at,
        owner=owner,
        reading=token.reading,
        pause=pause,
        otherwise=otherwise,
        syllables=syllables,
        reading_syllables=said if syllables is not None else None,
    )


def found_reading(
    entry: Entry,
    words: Sequence[Any],
    near: float,
    pauses: Sequence[tuple[float, float]] | None = None,
    nuclei: Sequence[float] | None = None,
) -> tuple[TermHearing, float] | None:
    """The reading of *entry* the timed *words* (in order) say nearest *near* seconds, held to *pauses* and *nuclei* as
    :func:`hear` holds a term, and when that reading ends; ``None`` when they say none."""
    found = [token for token in heard_tokens(words, [entry]) if token.entry is not None]
    if not found:
        return None
    token = min(found, key=lambda token: abs(token.start - near))
    return _term_heard(entry, token, pauses, nuclei=nuclei), token.end


def term_offsets(text: str, lexicon: Lexicon | None) -> list[int]:
    """Where each lexicon term of *text* starts (characters), in the order :func:`hear` reads its terms."""
    return [occurrence.start for occurrence in (lexicon or Lexicon()).voice().find(text)]


def hear(
    texts: Sequence[str] | str,
    words: Sequence[Any] | str,
    lexicon: Lexicon | None,
    *,
    timed: bool = True,
    pauses: Sequence[tuple[float, float]] | None = None,
    nuclei: Sequence[float] | None = None,
) -> Hearing:
    """*words* (a transcript, its words timed) against the narration *texts*, the lexicon's terms read as one token each.

    *timed* ``False``: the words carry no times (a transcript's text alone),
    and a term heard wrong is said without one. *pauses*: the silences of the
    audio heard (:func:`pauses_in`, on the words' clock); a term whose reading
    holds one of :data:`MAX_TERM_PAUSE` or more fails. ``None``: fluency is
    not checked. *nuclei*: the syllables of the audio heard (:func:`nuclei_in`,
    on the words' clock); a term read as words holding more than half-way to
    the syllables its letters say fails, read letter by letter
    (:func:`_term_heard`). ``None``: its syllables are not counted. A proper
    noun no entry stands for is heard when the words
    aligned there say it (:func:`says_name`): a spelling the
    speech-to-text chose for a name is no word error.
    """
    from app.services.video_studio.voice_check import align

    texts = [texts] if isinstance(texts, str) else list(texts)
    lexicon = lexicon or Lexicon()
    reference = reference_tokens(texts, lexicon)
    present = list({t.entry.key: t.entry for t in reference if t.entry is not None}.values())
    heard = heard_tokens(words, present)
    alignment = align([t.norm for t in reference], [t.norm for t in heard], names=_name_readings(reference, heard))
    hearing = Hearing(reference, heard, alignment)
    hearing.names = [
        NameHearing(reference[index].raw, said)
        for index, first, count in alignment.named
        if plain(said := " ".join(t.raw for t in heard[first : first + count])) != plain(reference[index].raw)
    ]
    for index, token in enumerate(reference):
        if token.entry is None:
            continue
        if alignment.reference_heard[index]:
            j = next(j for j, ref in enumerate(alignment.hypothesis_to_reference) if ref == index)
            hearing.terms.append(_term_heard(token.entry, heard[j], pauses, timed=timed, owner=token.owner, nuclei=nuclei))
            continue
        said, at = _around(heard, alignment.hypothesis_to_reference, index)
        otherwise = bool(said) and plain(said) not in (plain(token.entry.text), plain(token.entry.say))
        hearing.terms.append(
            TermHearing(token.entry.text, False, said, at if timed else None, owner=token.owner, otherwise=otherwise)
        )
    return hearing
