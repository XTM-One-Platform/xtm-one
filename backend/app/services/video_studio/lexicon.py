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

"""The pronunciation lexicon of the voice-over.

A speech model guesses how to read a brand name or an acronym, and guesses
differently from one voice to the next. The lexicon says it once:

* **Entries** (:class:`Entry`): ``text`` as written in a narration, ``say``
  (how it is read: letters read one by one are capitals joined by hyphens,
  ``X-T-M``), an optional ``ipa``, ``match`` (``exact``: the whole token, case
  and all, never joined to another word; ``word``: the whole word, case and
  all, however it is joined; ``case_insensitive``: the whole word in any case)
  and ``apply_to`` (``voice``, the default, ``captions`` or ``both``: the
  captions keep the written form unless the entry says otherwise).
* **Two sources, merged per video** (:func:`merge`): the platform's lexicon
  (``lexicon_store``: edited by an administrator, versioned, seeded from
  ``data/pronunciation_lexicon.json``) and the video's own
  ``voiceover.pronunciations``. A video entry overrides the platform's for the
  same written form. Nothing else adds a term: the pipeline never guesses one.
* **Said through the strongest mechanism the speech model offers**
  (:func:`render`): an SSML ``<phoneme alphabet="ipa">`` (``<sub alias>`` for
  an entry with no IPA) when the model reads SSML, the IPA in its delivery
  instruction when it takes one (``ipa_instruction``: the written form
  kept in the text), the provider's own lexicon
  when it keeps one (``provider_lexicon``: its rules, :func:`provider_rules`,
  registered once by ``voice.py``), else the narration rewritten with each
  term's reading (``text_rewrite``, :func:`rewrite_form`), tokenised so a word
  that only contains a term is never touched. An IPA no mechanism could send
  is said by its ``say`` respelling, and the render says so
  (:meth:`Rendition.notes`). A segment that failed its check
  is spoken again with the text rewrite (``FALLBACK``); a term it heard wrong
  twice is then said in another context (``REFRAMED``): its respelling
  run by run, alone in its clause (:func:`reframe_form`).
* **Letters are one token.** An entry read letter by letter is never
  sent as separated letters: its IPA runs each run of letters together into
  one phonetic word, stress marks included (:attr:`Entry.speech_ipa`:
  ``XTM`` -> ``ˌɛksˌtiːˈɛm``, ``XTM One`` -> ``ˌɛksˌtiːˈɛm ˈwʌn``, built from
  the letters' names when the entry gives no IPA), and every mechanism that
  sends text sends the letters glued into the words its written form has
  (``OpenCTI``, ``XTM One``), never with hyphens, spaces or punctuation
  between them: a voice reads them as one word, with no pause inside.

The check of what was heard is ``pronunciation_check``; the readings a hearing
may take come from :attr:`Entry.segments` and :attr:`Entry.phonemes`.
"""

from __future__ import annotations

import functools
import hashlib
import itertools
import json
import re
import unicodedata
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape as _xml_escape

EXACT, WORD, CASE_INSENSITIVE = "exact", "word", "case_insensitive"
MATCHES = (EXACT, WORD, CASE_INSENSITIVE)
VOICE, CAPTIONS, BOTH = "voice", "captions", "both"
APPLIES = (VOICE, CAPTIONS, BOTH)

SSML_PHONEME, IPA_INSTRUCTION = "ssml_phoneme", "ipa_instruction"
SSML_SUB, PROVIDER_LEXICON, TEXT_REWRITE = "ssml_sub", "provider_lexicon", "text_rewrite"
#: A term the voice said wrong twice, sent in another context (:func:`reframe_form`): its respelling, alone in
#: its clause.
TEXT_REFRAME = "text_reframe"
#: The mechanisms, the strongest first.
MECHANISMS = (SSML_PHONEME, IPA_INSTRUCTION, SSML_SUB, PROVIDER_LEXICON, TEXT_REWRITE, TEXT_REFRAME)
#: The mechanisms that send a term's IPA to the speech model.
IPA_MECHANISMS = frozenset({SSML_PHONEME, IPA_INSTRUCTION})
#: How hard a narration is spoken: the strongest mechanism, the text rewrite (a retry), or the text rewrite with each
#: term heard wrong twice in another context (the retry after two).
STRONGEST, FALLBACK, REFRAMED = "strongest", "fallback", "reframed"
#: Moved whenever how a term is sent changes (:func:`render`, :func:`rewrite_form`, :func:`reframe_form`): a narration
#: placed with a fallback take under another version is heard again (``voice_check.pipeline_version``).
RULES_VERSION = 1

#: Each letter's name in IPA (General American), as a letter read one by one is said.
LETTER_IPA: dict[str, str] = {
    "A": "eɪ",
    "B": "biː",
    "C": "siː",
    "D": "diː",
    "E": "iː",
    "F": "ɛf",
    "G": "dʒiː",
    "H": "eɪtʃ",
    "I": "aɪ",
    "J": "dʒeɪ",
    "K": "keɪ",
    "L": "ɛl",
    "M": "ɛm",
    "N": "ɛn",
    "O": "oʊ",
    "P": "piː",
    "Q": "kjuː",
    "R": "ɑːr",
    "S": "ɛs",
    "T": "tiː",
    "U": "juː",
    "V": "viː",
    "W": "dʌbəljuː",
    "X": "ɛks",
    "Y": "waɪ",
    "Z": "ziː",
}
#: The stress marks of an IPA word: a run of letters carries the main one on its last letter, a secondary on the others.
_PRIMARY, _SECONDARY = "\u02c8", "\u02cc"
_STRESS = _PRIMARY + _SECONDARY

DATA_DIR = Path(__file__).with_name("data")
SEED_PATH = DATA_DIR / "pronunciation_lexicon.json"
SCHEMA_PATH = DATA_DIR / "pronunciation_lexicon.schema.json"
#: The most entries a lexicon holds (the platform's; a video's are capped by its spec).
MAX_ENTRIES = 500

#: What joins two parts of a ``say`` form (``see-tem``, ``X-T-M``) without making them two words.
_SAY_PART = re.compile(r"[^\s\-\u2010\u2011\u2012\u2013\u2014.,;:]+")
#: Characters that glue a token to its neighbour: an ``exact`` entry is never matched glued to another word.
_GLUE = r"\w\-'\u2019\u2010\u2011"


def _clean_ipa(ipa: object) -> str | None:
    text = unicodedata.normalize("NFC", str(ipa or "")).strip().strip("/[]").strip()
    return text or None


def _is_letter(part: str) -> bool:
    return len(part) == 1 and part.isalpha() and part.isupper()


@dataclass(frozen=True)
class Segment:
    """A run of a ``say`` form: letters read one by one (``CTI``), or words read as one (``Open``, ``see-tem``)."""

    #: As the ``say`` form writes it (``Open``, ``see-tem``); the letters alone for a letter run (``CTI``).
    text: str
    letters: bool

    @property
    def key(self) -> str:
        """What a hearing is held to: the letters, or the words case-folded with nothing between them."""
        return self.text if self.letters else "".join(_SAY_PART.findall(self.text)).casefold()


def say_segments(say: str) -> tuple[Segment, ...]:
    """*say* cut into letter runs (two capitals or more, one by one) and the words between them."""
    say = unicodedata.normalize("NFKC", say or "")
    parts = list(_SAY_PART.finditer(say))
    segments: list[Segment] = []
    i = 0
    while i < len(parts):
        j = i
        while j < len(parts) and _is_letter(parts[j].group(0)):
            j += 1
        if j - i >= 2:
            segments.append(Segment("".join(p.group(0) for p in parts[i:j]), True))
            i = j
            continue
        k = i + 1
        while k < len(parts) and not (
            _is_letter(parts[k].group(0)) and k + 1 < len(parts) and _is_letter(parts[k + 1].group(0))
        ):
            k += 1
        segments.append(Segment(say[parts[i].start() : parts[k - 1].end()], False))
        i = k
    return tuple(segments)


@dataclass(frozen=True)
class Entry:
    """How one term is said: its written form, its reading, how it is found and what reads it."""

    text: str
    say: str
    ipa: str | None = None
    match: str = WORD
    apply_to: str = VOICE

    @property
    def voice(self) -> bool:
        return self.apply_to in (VOICE, BOTH)

    @property
    def captions(self) -> bool:
        return self.apply_to in (CAPTIONS, BOTH)

    @property
    def key(self) -> str:
        """The written form an override is decided on: its words, single-spaced."""
        return " ".join(self.text.split())

    @functools.cached_property
    def segments(self) -> tuple[Segment, ...]:
        return say_segments(self.say)

    @property
    def spells_letters(self) -> bool:
        """Whether part of it is read letter by letter (``X-T-M``, ``Open C-T-I``)."""
        return any(segment.letters for segment in self.segments)

    @functools.cached_property
    def speech_ipa(self) -> str | None:
        """The IPA a speech model is sent: the entry's own, each run of its letters one phonetic word.

        A run of letters its IPA writes as separate words (``ɛks tiː ɛm``) is
        sent as one (``ˌɛksˌtiːˈɛm``, :func:`letters_ipa`), the rest of its
        IPA as given. An entry read letter by letter with no IPA - or one
        whose IPA does not follow its ``say`` form run by run - is sent its
        letters' names and its words' English reading (``phonetics``). An
        entry with no letters is sent its IPA as given (``None`` without).
        """
        if not self.spells_letters:
            return self.ipa
        aligned = _aligned_ipa(self.segments, self.ipa.split()) if self.ipa else None
        return aligned or " ".join(_segment_ipa(segment) for segment in self.segments)

    @functools.cached_property
    def phonemes(self) -> tuple[str, ...]:
        """Its reading as phonemes: its IPA, else its ``say`` form read as English (``phonetics``)."""
        from app.services.video_studio import phonetics

        if self.ipa:
            parsed = phonetics.parse_ipa(self.ipa)
            if parsed:
                return parsed
        return phonetics.say_phonemes(self.segments)

    @functools.cached_property
    def words(self) -> tuple[tuple[str, ...], ...]:
        """Its reading word by word: its IPA's words, else its ``say`` form's (``phonetics.key`` reads them)."""
        from app.services.video_studio import phonetics

        if self.ipa:
            parsed = phonetics.parse_ipa_words(self.ipa)
            if parsed:
                return parsed
        return phonetics.say_words(self.segments)

    @functools.cached_property
    def say_agrees(self) -> bool:
        """Whether its ``say`` form sounds like its IPA (always, with no IPA): the IPA is the reading when both are given.

        A ``say`` that does not is still what a model without SSML is sent,
        but a hearing of it is not accepted: the check holds the voice to the
        IPA, by the rule a hearing is held to (``phonetics.reading_cost``).
        """
        from app.services.video_studio import phonetics

        if not self.ipa:
            return True
        return phonetics.reading_cost(self.words, phonetics.say_words(self.segments)) is not None

    def as_dict(self) -> dict:
        out: dict[str, Any] = {"text": self.text, "say": self.say, "match": self.match, "apply_to": self.apply_to}
        if self.ipa:
            out["ipa"] = self.ipa
        return out


def letters_ipa(letters: str) -> str:
    """*letters* read one by one as ONE phonetic word: ``XTM`` -> ``ˌɛksˌtiːˈɛm``, the main stress on the last."""
    names = [LETTER_IPA[letter.upper()] for letter in letters if letter.upper() in LETTER_IPA]
    return "".join((_PRIMARY if index == len(names) - 1 else _SECONDARY) + name for index, name in enumerate(names))


def _segment_ipa(segment: Segment) -> str:
    """A ``say`` run's IPA from its own reading: its letters as one word, its words as English reads them (``phonetics``)."""
    from app.services.video_studio import phonetics

    if segment.letters:
        return letters_ipa(segment.text)
    words = phonetics.say_words((segment,))
    return " ".join("".join(word) for word in words) or segment.text


def _aligned_ipa(segments: Sequence[Segment], given: list[str]) -> str | None:
    """*given* (an entry's IPA, word by word) with each letter run of *segments* joined into one word; ``None`` unaligned.

    A letter run takes the IPA words that sound like its letters' names
    (``phonetics.LETTER_PHONEMES``), one compact word or one word a letter;
    a word run takes as many IPA words as it has words. Words left over, or
    a run that sounds otherwise: not aligned.
    """
    from app.services.video_studio import phonetics

    out: list[str] = []
    at = 0
    for segment in segments:
        if not segment.letters:
            count = len(segment.text.split())
            if at + count > len(given):
                return None
            out.extend(given[at : at + count])
            at += count
            continue
        target = tuple(p for letter in segment.text for p in phonetics.LETTER_PHONEMES.get(letter.upper(), ()))
        sounds: tuple[str, ...] = ()
        taken = 0
        while at + taken < len(given) and len(sounds) < len(target):
            sounds += phonetics.parse_ipa(given[at + taken])
            taken += 1
        if sounds != target:
            return None
        out.append(letters_ipa(segment.text))
        at += taken
    return " ".join(out) if at == len(given) else None


def entry_of(item: Any) -> Entry:
    """An :class:`Entry` from an entry, a mapping or an object with the same fields (``spec.Pronunciation``)."""
    if isinstance(item, Entry):
        return item

    def read(name: str) -> Any:
        return item.get(name) if isinstance(item, dict) else getattr(item, name, None)

    match = str(read("match") or WORD)
    apply_to = str(read("apply_to") or VOICE)
    return Entry(
        text=" ".join(str(read("text") or "").split()),
        say=str(read("say") or "").strip(),
        ipa=_clean_ipa(read("ipa")),
        match=match if match in MATCHES else WORD,
        apply_to=apply_to if apply_to in APPLIES else VOICE,
    )


@dataclass(frozen=True)
class Occurrence:
    """Where a narration writes a term: its characters and the entry it is said by."""

    start: int
    end: int
    entry: Entry
    written: str


def same_form(a: Entry, b: Entry) -> bool:
    if a.key == b.key:
        return True
    return CASE_INSENSITIVE in (a.match, b.match) and a.key.casefold() == b.key.casefold()


def _pattern(entry: Entry) -> re.Pattern[str]:
    body = r"\s+".join(re.escape(word) for word in entry.text.split())
    if entry.match == EXACT:
        return re.compile(rf"(?<![{_GLUE}]){body}(?![{_GLUE}])")
    return re.compile(rf"(?<!\w){body}(?!\w)", re.IGNORECASE if entry.match == CASE_INSENSITIVE else 0)


@dataclass(frozen=True)
class Lexicon:
    """The entries a video is said with: its own first, then the platform's it does not override."""

    entries: tuple[Entry, ...] = ()
    #: The version of the platform lexicon merged in; ``None`` when none was.
    version: int | None = None

    def __bool__(self) -> bool:
        return bool(self.entries)

    def voice(self) -> Lexicon:
        """The entries the voice reads (``apply_to`` voice or both)."""
        return Lexicon(tuple(e for e in self.entries if e.voice), self.version)

    def captions(self) -> Lexicon:
        """The entries the captions show as said (``apply_to`` captions or both)."""
        return Lexicon(tuple(e for e in self.entries if e.captions), self.version)

    @functools.cached_property
    def _patterns(self) -> tuple[re.Pattern[str], ...]:
        return tuple(_pattern(entry) for entry in self.entries)

    def find(self, text: str | None) -> list[Occurrence]:
        """Every term *text* writes, as whole words per each entry's ``match``: the longest wins where two overlap."""
        if not text or not self.entries:
            return []
        found: list[tuple[int, int, int]] = []
        for index, pattern in enumerate(self._patterns):
            found.extend((m.start(), m.end(), index) for m in pattern.finditer(text) if m.end() > m.start())
        kept: list[tuple[int, int, int]] = []
        for start, end, index in sorted(found, key=lambda f: (-(f[1] - f[0]), f[0], f[2])):
            if all(end <= s or start >= e for s, e, _i in kept):
                kept.append((start, end, index))
        return [Occurrence(s, e, self.entries[i], text[s:e]) for s, e, i in sorted(kept)]

    def present(self, text: str | None) -> list[Entry]:
        """The entries *text* writes, once each, in the order it first writes them."""
        seen: dict[str, Entry] = {}
        for occurrence in self.find(text):
            seen.setdefault(occurrence.entry.key, occurrence.entry)
        return list(seen.values())

    def rewrite(self, text: str | None) -> str | None:
        """*text* with each term the voice reads replaced by its reading (:func:`rewrite_form`)."""
        if not text:
            return text
        return _replace(text, self.voice().find(text), lambda o: rewrite_form(o.entry))

    def caption_text(self, text: str | None) -> str | None:
        """*text* as the captions show it: the written form, but for the entries applied to the captions too."""
        if not text:
            return text
        return _replace(text, self.captions().find(text), lambda o: o.entry.say)

    def digest(self) -> str:
        """What the entries say, as one digest: a cache key changes with any of them."""
        body = json.dumps([e.as_dict() for e in self.entries], sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(body.encode("utf-8")).hexdigest()[:24]


def _replace(text: str, found: Sequence[Occurrence], form) -> str:
    out, cursor = [], 0
    for occurrence in found:
        out.append(text[cursor : occurrence.start])
        out.append(form(occurrence))
        cursor = occurrence.end
    out.append(text[cursor:])
    return "".join(out)


def merge(platform: Iterable[Any] = (), video: Iterable[Any] = (), *, version: int | None = None) -> Lexicon:
    """The lexicon of a video: its own entries, then each platform entry it does not override (same written form)."""
    own = [entry for entry in (entry_of(item) for item in video) if entry.text and entry.say]
    kept = [
        entry
        for entry in (entry_of(item) for item in platform)
        if entry.text and entry.say and not any(same_form(entry, mine) for mine in own)
    ]
    return Lexicon(tuple(own + kept), version)


# ── Saying it ─────────────────────────────────────────────────────────────


def rewrite_form(entry: Entry) -> str:
    """How a model that reads no SSML is sent *entry*: its ``say`` form, each run of letters one token.

    A syllabic reading (``see-tem``, ``Fil-i-gran``) goes as given, never
    spelled out: the parts are what tell the voice how to say it. Letters
    (``X-T-M``) go glued into the words its written form has (``Open C-T-I``
    -> ``OpenCTI``, ``X-T-M One`` -> ``XTM One``), what a voice says as
    one word - never letters with hyphens, spaces or punctuation between
    them, which a voice reads with a pause after each.
    """
    if not entry.spells_letters:
        return entry.say.strip()
    parts = [segment.text for segment in entry.segments]
    return " ".join(parts) if len(entry.text.split()) > 1 else "".join(parts)


def reframe_form(entry: Entry) -> str:
    """How a term the voice said wrong twice is sent once more: its ``say`` form as the lexicon spells it, run
    by run - each run its own word, the letters of a run glued (``Open A-E-V`` -> ``Open AEV``, never ``OpenAEV``,
    which the voice already said wrong, nor ``A-E-V``, a pause after each letter)."""
    return " ".join(segment.text for segment in entry.segments) if entry.spells_letters else entry.say.strip()


def _set_apart(said: str, rest: str) -> tuple[str, str]:
    """*said* (the narration sent up to a term) and the term's own comma after it: a comma closes the word before
    it and opens the word after it, where nothing - punctuation, the narration's start or end - parts them already."""
    kept = said.rstrip()
    if kept and kept[-1].isalnum():
        said = kept + "," + said[len(kept) :]
    after = rest.lstrip()
    return said, "," if after and after[0].isalnum() else ""


def _reframed(text: str, found: Sequence[Occurrence], reframe: Collection[str]) -> Rendition:
    """*text* with every term rewritten, each one in *reframe* by :func:`reframe_form`, alone in its clause."""
    said, cursor, terms = "", 0, []
    for occurrence in found:
        entry = occurrence.entry
        said += text[cursor : occurrence.start]
        if entry.key in reframe:
            said, comma = _set_apart(said, text[occurrence.end :])
            said += reframe_form(entry) + comma
            terms.append((entry.text, TEXT_REFRAME))
        else:
            said += rewrite_form(entry)
            terms.append((entry.text, TEXT_REWRITE))
        cursor = occurrence.end
    return Rendition(said + text[cursor:], terms=tuple(terms))


@dataclass(frozen=True)
class SsmlDialect:
    """How a speech model reads SSML: a ``<speak>`` document with its text escaped, or tags inside plain text."""

    name: str
    root: bool
    escape_text: bool

    def text(self, value: str) -> str:
        return _xml_escape(value) if self.escape_text else value

    def phoneme(self, ipa: str, written: str) -> str:
        return f'<phoneme alphabet="ipa" ph="{_xml_escape(ipa, {chr(34): "&quot;"})}">{self.text(written)}</phoneme>'

    def sub(self, alias: str, written: str) -> str:
        return f'<sub alias="{_xml_escape(alias, {chr(34): "&quot;"})}">{self.text(written)}</sub>'

    def document(self, body: str) -> str:
        return f"<speak>{body}</speak>" if self.root else body


#: The SSML a provider declares its models read (``speech.PronunciationSupport.ssml``).
SSML_DIALECTS = {
    "w3c": SsmlDialect("w3c", root=True, escape_text=True),
    "inline": SsmlDialect("inline", root=False, escape_text=False),
}


@dataclass(frozen=True)
class Rendition:
    """A narration as it is sent to a speech model: the text, whether it is SSML, how each term is said."""

    text: str
    ssml: bool = False
    #: ``(written form, mechanism)`` of each term, in the narration's order.
    terms: tuple[tuple[str, str], ...] = ()
    #: The rules of the provider's lexicon the request refers to (``provider_lexicon``).
    rules: tuple[Any, ...] = ()
    #: ``(written form, IPA)`` of each term its delivery instruction says (``ipa_instruction``), once each.
    ipa: tuple[tuple[str, str], ...] = ()
    #: ``(written form, IPA, say)`` of each term with an IPA said by its respelling: the model takes no IPA.
    respelled: tuple[tuple[str, str, str], ...] = ()

    @property
    def mechanism(self) -> str | None:
        """The mechanisms used, the strongest first (``ssml_phoneme+text_rewrite``); ``None`` with no term."""
        used = sorted({m for _t, m in self.terms}, key=MECHANISMS.index)
        return "+".join(used) or None

    def key(self) -> str:
        """What decides the audio beside the text: the mechanisms, SSML or not, the lexicon's rules, the IPA instructed."""
        if not self.terms:
            return ""
        rules = hashlib.sha256(repr(self.rules).encode("utf-8")).hexdigest()[:16] if self.rules else ""
        key = f"lexicon:{self.mechanism}:{int(self.ssml)}:{rules}"
        if self.ipa:
            key += ":ipa:" + hashlib.sha256(repr(self.ipa).encode("utf-8")).hexdigest()[:16]
        return key

    def notes(self, model: str) -> list[str]:
        """What a render says of each IPA *model* could not take: the term was said by its respelling."""
        return [
            f'"{written}" has an IPA (/{ipa}/) that {model} cannot take (no SSML phoneme, no pronunciation '
            f'instruction): said by its respelling "{say}".'
            for written, ipa, say in self.respelled
        ]


def provider_rules(lexicon: Lexicon) -> tuple[Any, ...]:
    """The rules a provider's own lexicon holds for *lexicon*: each term the voice reads, aliased to its reading."""
    from app.llm.speech import LexiconRule

    return tuple(
        LexiconRule(entry.text, rewrite_form(entry), case_sensitive=entry.match != CASE_INSENSITIVE)
        for entry in lexicon.voice().entries
    )


def render(
    text: str, lexicon: Lexicon, support: Any = None, *, level: str = STRONGEST, reframe: Collection[str] = ()
) -> Rendition:
    """*text* as sent to a speech model offering *support* (``speech.PronunciationSupport``).

    At the ``STRONGEST`` level each term is said through the strongest
    mechanism the model offers: an SSML phoneme for an entry with an IPA, else
    its IPA in the model's delivery instruction (the written form kept in the
    text), an SSML ``<sub>`` for one without, the provider's lexicon, else the
    text rewrite; an IPA none of them sends is said by its respelling and
    recorded (``Rendition.respelled``). The IPA sent is the entry's
    :attr:`Entry.speech_ipa`: a run of letters is one phonetic word,
    and an entry read letter by letter always has one. At the ``FALLBACK``
    level every term is rewritten (:func:`rewrite_form`). At the ``REFRAMED``
    level too, but each term *reframe* names (its written form: one the voice
    said wrong twice) by its respelling run by run
    (:func:`reframe_form`), alone in its clause: a comma before and after it
    where nothing parts it from its neighbours already (``text_reframe``).
    """
    found = lexicon.voice().find(text)
    if not found:
        return Rendition(text)
    if level == REFRAMED:
        return _reframed(text, found, reframe)
    strongest = level == STRONGEST and support is not None
    dialect = SSML_DIALECTS.get(getattr(support, "ssml", "") or "") if strongest else None
    chosen: list[str] = []
    for occurrence in found:
        entry = occurrence.entry
        if dialect is not None and getattr(support, "phoneme", False) and entry.speech_ipa:
            chosen.append(SSML_PHONEME)
        elif strongest and getattr(support, "instruction", False) and entry.speech_ipa:
            chosen.append(IPA_INSTRUCTION)
        elif dialect is not None and getattr(support, "sub", False):
            chosen.append(SSML_SUB)
        elif strongest and getattr(support, "lexicon", False):
            chosen.append(PROVIDER_LEXICON)
        else:
            chosen.append(TEXT_REWRITE)
    if not {SSML_PHONEME, SSML_SUB} & set(chosen):
        dialect = None
    escape = dialect.text if dialect is not None else (lambda value: value)
    out, cursor = [], 0
    for occurrence, mechanism in zip(found, chosen, strict=True):
        out.append(escape(text[cursor : occurrence.start]))
        entry = occurrence.entry
        if mechanism == SSML_PHONEME and dialect is not None:
            out.append(dialect.phoneme(entry.speech_ipa or "", occurrence.written))
        elif mechanism == SSML_SUB and dialect is not None:
            out.append(dialect.sub(rewrite_form(entry), occurrence.written))
        elif mechanism in (PROVIDER_LEXICON, IPA_INSTRUCTION):
            out.append(escape(occurrence.written))
        else:
            out.append(escape(rewrite_form(entry)))
        cursor = occurrence.end
    out.append(escape(text[cursor:]))
    body = "".join(out)
    pairs = list(zip(found, chosen, strict=True))
    instructed = {o.entry.key: (o.entry.text, o.entry.speech_ipa or "") for o, m in pairs if m == IPA_INSTRUCTION}
    respelled = {
        o.entry.key: (o.entry.text, o.entry.ipa or "", rewrite_form(o.entry))
        for o, m in pairs
        if o.entry.ipa and m not in IPA_MECHANISMS and level == STRONGEST
    }
    return Rendition(
        dialect.document(body) if dialect is not None else body,
        ssml=dialect is not None,
        terms=tuple((o.entry.text, m) for o, m in pairs),
        rules=provider_rules(lexicon) if PROVIDER_LEXICON in chosen else (),
        ipa=tuple(instructed.values()),
        respelled=tuple(respelled.values()),
    )


# ── Authoring ─────────────────────────────────────────────────────────────

#: A word written like a coined name or an acronym: capitals after its first letter (``SecOps``, ``SOAR``).
_NAMED = re.compile(r"(?<![\w\-])[^\W\d_]{2,}\d*(?![\w\-])")
#: Shorter, a word in capitals is a common abbreviation the voices know.
_ACRONYM_MIN = 3
_SPELL_GLUE = r"[\s.\-\u2010\u2011]+"


def named(word: str) -> bool:
    """Whether *word* is written like a coined name or an acronym (capitals after its first letter; in capitals, three letters or more)."""
    letters = "".join(ch for ch in word if ch.isalpha())
    if letters.isupper():
        return len(letters) >= _ACRONYM_MIN
    return any(a.islower() and b.isupper() for a, b in itertools.pairwise(letters))


def _only_cases(entry: Entry) -> bool:
    """Whether *entry* changes nothing but the case of what it writes: its ``say`` is its written words, as words.

    ``eXtended`` read ``extended``: written in any case, the voice says it as the entry does.
    """

    def words(text: str) -> list[str]:
        return [part.casefold() for part in _SAY_PART.findall(text)]

    return not entry.spells_letters and entry.say_agrees and words(entry.say) == words(entry.text)


def pronunciation_warnings(text: str | None, lexicon: Lexicon, brand: Lexicon | None = None) -> list[str]:
    """What a narration writes that the voice may not say as its lexicon does, one sentence each.

    * a term written in another case than its entry (``word`` / ``exact``): the entry is never applied - unless the
      entry only changes the case (:func:`_only_cases`), which the voice says the same either way;
    * the letters of a term the lexicon reads as a word, written out one by one;
    * a term the *brand* lexicon reads (:func:`brand_lexicon`) that *lexicon* has no entry for: ``CTEM has no
      pronunciation; the voice may spell it`` where its capitals read at sight say more syllables than the brand's
      reading (``pronunciation_check.spelled_syllables``), with the brand's entry to add;
    * a word written like a coined name or an acronym no entry covers: the voice guesses.

    Each sentence names the narration: the title or the lines on screen may write the term as the entry does.
    """
    if not text:
        return []
    voice = lexicon.voice()
    covered = [(o.start, o.end) for o in voice.find(text)]
    out: list[str] = []
    seen: set[str] = set()
    for entry in voice.entries:
        if entry.match != CASE_INSENSITIVE:
            loose = re.compile(rf"(?<!\w){r'\s+'.join(re.escape(w) for w in entry.text.split())}(?!\w)", re.IGNORECASE)
            for match in loose.finditer(text):
                if any(s <= match.start() < e for s, e in covered) or match.group(0) in seen:
                    continue
                seen.add(match.group(0))
                covered.append((match.start(), match.end()))
                if _only_cases(entry):
                    continue
                out.append(
                    f'its narration writes "{match.group(0)}" where the pronunciation lexicon has "{entry.text}": the '
                    f'voice is only told how to say "{entry.text}" - write "{entry.text}" in the narration'
                )
        letters = entry.text.replace(" ", "")
        if not entry.spells_letters and letters.isalpha() and letters.isupper() and len(letters) >= 2:
            spelled = re.compile(rf"(?<!\w){_SPELL_GLUE.join(letters)}\.?(?!\w)", re.IGNORECASE)
            for match in spelled.finditer(text):
                covered.append((match.start(), match.end()))
                out.append(
                    f'its narration spells "{match.group(0)}" letter by letter, but the pronunciation lexicon reads '
                    f'{entry.text} as "{entry.say}": write "{entry.text}" in the narration'
                )
    out.extend(_brand_unsaid(text, brand, covered))
    unknown: list[str] = []
    for match in _NAMED.finditer(text):
        word = match.group(0)
        if named(word) and word not in unknown and not any(s <= match.start() < e for s, e in covered):
            unknown.append(word)
    if unknown:
        listed = ", ".join(f'"{w}"' for w in unknown[:6]) + (f" and {len(unknown) - 6} more" if len(unknown) > 6 else "")
        out.append(
            f"its narration says {listed}, which no pronunciation lexicon entry (the platform's or the video's) covers: "
            "the voice guesses how to say it - add a reading to voiceover.pronunciations (preview_voiceover tells how a "
            "voice says it), or ask an administrator to add it to the platform lexicon"
        )
    return out


def _brand_unsaid(text: str, brand: Lexicon | None, covered: list[tuple[int, int]]) -> list[str]:
    """One sentence for each term of *text* the *brand* lexicon reads that nothing *covered* (the video's lexicon, a
    warning already said) stands for, once each, its span added to *covered*: the voice is told nothing of it."""
    from app.services.video_studio.pronunciation_check import spelled_syllables

    out: list[str] = []
    said: set[str] = set()
    for occurrence in (brand.voice().find(text) if brand else []):
        if any(s < occurrence.end and occurrence.start < e for s, e in covered):
            continue
        covered.append((occurrence.start, occurrence.end))
        entry = occurrence.entry
        if entry.key in said:
            continue
        said.add(entry.key)
        guess = "the voice may spell it" if spelled_syllables(entry) is not None else "the voice guesses how to say it"
        out.append(
            f"{occurrence.written} has no pronunciation; {guess} - the brand reads it \"{entry.say}\": add "
            f"{json.dumps({key: value for key, value in entry.as_dict().items() if key in ('text', 'say', 'ipa')}, ensure_ascii=False)} "
            "to voiceover.pronunciations, or ask an administrator to put it back in the platform lexicon"
        )
    return out


@functools.cache
def brand_lexicon() -> Lexicon:
    """The readings the brand owner confirmed (the platform's data file, :func:`load_seed`), read once: what a save holds a
    video's lexicon to (:func:`pronunciation_warnings`), whatever the platform lexicon's administrators made of it."""
    return Lexicon(tuple(load_seed()))


# ── The seed ──────────────────────────────────────────────────────────────


def load_seed(path: Path = SEED_PATH, schema_path: Path = SCHEMA_PATH) -> list[Entry]:
    """The readings the platform lexicon starts with, validated against their schema."""
    import jsonschema

    data = json.loads(path.read_text(encoding="utf-8"))
    jsonschema.validate(data, json.loads(schema_path.read_text(encoding="utf-8")))
    return [entry_of(item) for item in data["entries"]]
