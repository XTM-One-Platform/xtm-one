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

"""Every narration heard back before it is placed.

A speech model can garble a narration - a phrase said twice, a clause
dropped, a word looped - and the render used to place whatever came back.
Each segment is now spoken once (the digest cache of ``voice.py``), then
transcribed by the platform's speech-to-text (``app.llm.transcription``) and
compared to the text it was asked to say, in code:

* **Word error rate, lexicon-aware**. Both texts are reduced to the
  same words (:func:`normalized_tokens`: case, punctuation, numbers written
  either way, an acronym spelled out), and each term of the video's
  pronunciation lexicon stands as one token on both sides, whichever reading
  the lexicon accepts was heard (``pronunciation_check.hear``), and a proper
  noun no entry covers is heard when the words there sound like it
  (``Mosconi`` for ``Moscone`` is no error). Over :data:`MAX_WER` the segment
  fails.
* **The brand check.** Each lexicon term the narration writes must be heard
  as an accepted reading where the narration has it: a term missing or heard
  wrong fails the segment whatever the WER ("brand term mispronounced: ...
  heard as '...' at 0:03.2").
* **A repeated token sequence.** The same words twice in a row (one word
  three times) that the narration does not repeat itself
  (:func:`repeated_sequences`): a loop of the engine, whatever the WER says.

A segment is first spoken with the strongest mechanism its speech model
offers for the lexicon (``lexicon.render``; an IPA it cannot take is said by
its respelling, and its notes say so). One that fails is placed with
the take of the same voice that passed its check on the same words before
(kept by :func:`keep_passed_take` under ``voice.text_digest_of``,
whatever narrations surrounded it - the scene's preview, a render before a
neighbour was reworded), else spoken again once with the same voice and the
next-strongest mechanism (the text rewrite, letters glued:
``lexicon.FALLBACK``; a new take, ``voice.speak_take``: never from the cache,
kept beside the cached one, never over it). A term those two takes both
heard wrong is never sent the same way a third time
(:func:`heard_wrong_twice`): the next take says it in another context
(``lexicon.REFRAMED``: its respelling run by run, alone in its clause), and
the take after it slower (``voice.slower_speed``) with that text - with the
text rewrite when no term failed twice. Then the voice of the same speech
model nearest the video's speaks it (``voice.closest_voice``) - never
another provider's, never the platform's default voice. A take of that voice
is a fallback: another voice mid-film, which the audience hears, so it fails
in the video's voice (its record ``failed``, :func:`failed`) and comes with
its :class:`VoiceChange`: the render refuses it as it refuses a narration
that failed (``voice_fallback``), and the quality check says it too. A
segment that still fails is never placed silently - the render fails, or,
forced, places the closest take of its own voice with a finding naming the
term, what was heard and each mechanism tried (``render.py``). Each attempt
records its mechanism, its speed, the terms it set apart and the exact string
sent. The outcome is kept beside the cached WAV (a new object
under ``checks/<digest>/`` per check, the newest deciding,
:func:`load_record`): its duration, the transcript, the WER, the terms as
heard, which take was placed and the words heard in it with their times -
what the rendered-audio check (``audio_qc``) reads instead of transcribing
the narration a second time, while the voice heard still plays that take.
A narration heard once is never transcribed again, the editor's
preview plays the take that passed in its own voice (``voice.placed``), and
``render_video`` refuses a video one of whose narrations failed in its own
voice (``render_checks.gate``). But a fallback take - slower, a term set
apart, another voice (:func:`fallback_kind`) - is placed again unheard only
under the voice pipeline it was placed under (:func:`pipeline_version`:
the check's rules, the lexicon's, the ladder): under another, the ladder hears
the narration again from its first take, and it may now pass at its speed.

With no speech-to-text available to the person, the segment is placed
unchecked and the render says so: a check that cannot run is never a failure.
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from collections import Counter
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any

from app.core.utils import format_exception_message
from app.services.video_studio.phonetics import TEN_WORDS, UNIT_WORDS

logger = logging.getLogger(__name__)

#: Over this word error rate a narration failed its transcript check.
MAX_WER = 0.10
#: The longest token sequence looked for repeated, in words.
MAX_REPEAT_WORDS = 8
#: The record's format and the rules it was checked under: a record of other rules is a narration never checked.
#: 2: a pause inside a lexicon term fails it. 3: a number in a term's ``say`` form is heard as its words.
#: 4: a reading that sounds like a term is held to its consonants too, at most one phoneme off.
#: 5: a proper noun is heard by its sound, and a narration another voice said failed in the video's.
#: 6: a term read as words that its written form spells is held to the voice's syllables: read letter by letter, it fails.
RECORD_VERSION = 6
#: A narration that passed under the rules of this version or later still passes (6 refuses more: a term read letter
#: by letter passed before), so it is placed without being heard again; one heard under older rules is heard again.
PASSED_SINCE = 6
#: Moved whenever the retry ladder changes (:func:`speak_checked`: which takes are tried, in which order, how slow): with
#: the check's rules and the lexicon's, the voice pipeline a fallback take is placed under (:func:`pipeline_version`).
LADDER_VERSION = 1
#: What a fallback take is (:func:`fallback_kind`): spoken slower than the video's speed, a term set apart in its
#: clause, or by another voice than the video's.
SLOWER_TAKE, REFRAMED_TAKE, VOICE_TAKE = "slower", "reframed", "voice"
#: The rate a segment is sent to the speech-to-text at: speech, not music.
TRANSCRIBE_RATE = 16000
_RECORD_BYTES = 64 * 1024
#: The most words a take keeps with their times: a record stays well inside :data:`_RECORD_BYTES`.
MAX_KEPT_WORDS = 1000
#: The most words heard otherwise a take keeps (``Attempt.as_dict``: ``otherwise``), the first first.
MAX_OTHERWISE_KEPT = 12

PASSED, FAILED, UNCHECKED = "passed", "failed", "unchecked"


# ── Words ─────────────────────────────────────────────────────────────────

_UNITS = {word: value for value, word in enumerate(UNIT_WORDS)}
_TENS = {word: 10 * (i + 2) for i, word in enumerate(TEN_WORDS)}
_SCALES = {"hundred": 100, "thousand": 1000, "million": 10**6, "billion": 10**9}
_WORD = re.compile(r"\d+(?:[.,]\d+)*|[^\W\d_]+", re.UNICODE)


#: A word of a text with when it is said (``0, 0`` for a written text).
Timed = tuple[str, float, float]


def _pieces(text: str) -> list[str]:
    folded = unicodedata.normalize("NFKC", text or "").casefold().replace("%", " percent ").replace("&", " and ")
    folded = re.sub(r"['\u2019\u02bc]", "", folded)
    return [match.group(0).replace(",", "") for match in _WORD.finditer(folded)]


def _is_number_word(word: str, first: bool) -> bool:
    return word in _UNITS or word in _TENS or (word in _SCALES and not first)


def _numbers(items: list[Timed]) -> list[Timed]:
    """Runs of number words (``twenty three``, ``two hundred``) as their digits, so ``23`` and ``twenty-three`` agree."""
    out: list[Timed] = []
    i = 0
    while i < len(items):
        if not _is_number_word(items[i][0], True):
            out.append(items[i])
            i += 1
            continue
        total = current = 0
        j = i
        while j < len(items) and _is_number_word(items[j][0], j == i):
            word = items[j][0]
            if word in _SCALES:
                current = max(current, 1) * _SCALES[word]
                if _SCALES[word] >= 1000:
                    total, current = total + current, 0
            else:
                current += _UNITS.get(word, 0) + _TENS.get(word, 0)
            j += 1
        out.append((str(total + current), items[i][1], items[j - 1][2]))
        i = j
    return out


def _letters(items: list[Timed]) -> list[Timed]:
    """A run of single letters is an acronym spelled out: one word."""
    out: list[Timed] = []
    run: list[Timed] = []
    for item in [*items, ("", 0.0, 0.0)]:
        if len(item[0]) == 1 and item[0].isalpha():
            run.append(item)
            continue
        if len(run) >= 2:
            out.append(("".join(word for word, _s, _e in run), run[0][1], run[-1][2]))
        else:
            out.extend(run)
        run = []
        if item[0]:
            out.append(item)
    return out


def normalized_tokens(text: str | None) -> list[str]:
    """The words of *text* as they are compared: case-folded, without punctuation, numbers as digits.

    ``1,000`` and ``1000``, ``twenty-three`` and ``23``, ``it's`` and
    ``its``, ``X T M`` and ``XTM`` read the same.
    """
    return [word for word, _s, _e in _letters(_numbers([(piece, 0.0, 0.0) for piece in _pieces(text or "")]))]


def timed_tokens(words: Sequence[Any]) -> list[Timed]:
    """Transcribed words (``text``, ``start``, ``end``) normalised as :func:`normalized_tokens` does, each with its time."""
    items = [(piece, float(w.start), float(w.end)) for w in words for piece in _pieces(w.text)]
    return _letters(_numbers(items))


@dataclass(frozen=True)
class Alignment:
    """How a transcript lines up with a narration: its word error rate and each word's match."""

    errors: int
    reference_words: int
    #: For each transcript word, the narration word it stands for (``None``: inserted).
    hypothesis_to_reference: tuple[int | None, ...]
    #: For each narration word, whether it was heard as written.
    reference_heard: tuple[bool, ...]
    #: ``(narration word, first transcript word, how many)`` of each proper noun heard by its sound.
    named: tuple[tuple[int, int, int], ...] = ()

    @property
    def wer(self) -> float:
        if not self.reference_words:
            return 0.0 if not self.hypothesis_to_reference else 1.0
        return self.errors / self.reference_words


#: The most transcript words a proper noun may be heard as (``Moscow knee`` for ``Moscone``).
NAME_SPAN = 3


def align(
    reference: Sequence[str],
    hypothesis: Sequence[str],
    *,
    names: Mapping[int, Callable[[int, int], bool]] | None = None,
) -> Alignment:
    """The word alignment of *hypothesis* on *reference* that costs least (substitution, deletion, insertion: 1).

    A word written as one and heard as two (``OpenCTI`` / ``open cti``), or
    the reverse, matches at no cost. *names* ``{narration word: says(first,
    count)}``: the proper nouns of the narration (``pronunciation_check``),
    each matched at no cost by the run of up to :data:`NAME_SPAN` transcript
    words from *first* that ``says`` it.
    """
    n, m = len(reference), len(hypothesis)
    inf = n + m + 1
    cost = [[inf] * (m + 1) for _ in range(n + 1)]
    move = [[""] * (m + 1) for _ in range(n + 1)]
    cost[0][0] = 0
    for i in range(n + 1):
        says = names.get(i) if names and i < n else None
        for j in range(m + 1):
            here = cost[i][j]
            if here == inf:
                continue
            if i < n and here + 1 < cost[i + 1][j]:
                cost[i + 1][j], move[i + 1][j] = here + 1, "del"
            if j < m and here + 1 < cost[i][j + 1]:
                cost[i][j + 1], move[i][j + 1] = here + 1, "ins"
            if i < n and j < m:
                step = 0 if reference[i] == hypothesis[j] else 1
                if here + step < cost[i + 1][j + 1]:
                    cost[i + 1][j + 1], move[i + 1][j + 1] = here + step, "sub" if step else "ok"
            if i < n and j + 1 < m and reference[i] == hypothesis[j] + hypothesis[j + 1] and here < cost[i + 1][j + 2]:
                cost[i + 1][j + 2], move[i + 1][j + 2] = here, "split"
            if i + 1 < n and j < m and reference[i] + reference[i + 1] == hypothesis[j] and here < cost[i + 2][j + 1]:
                cost[i + 2][j + 1], move[i + 2][j + 1] = here, "join"
            if says is not None:
                for count in range(1, min(NAME_SPAN, m - j) + 1):
                    if here < cost[i + 1][j + count] and says(j, count):
                        cost[i + 1][j + count], move[i + 1][j + count] = here, f"name{count}"
    to_ref: list[int | None] = [None] * m
    heard = [False] * n
    named: list[tuple[int, int, int]] = []
    i, j = n, m
    while i or j:
        step = move[i][j]
        if step in ("ok", "sub"):
            i, j = i - 1, j - 1
            to_ref[j] = i
            heard[i] = step == "ok"
        elif step == "split":
            i, j = i - 1, j - 2
            to_ref[j] = to_ref[j + 1] = i
            heard[i] = True
        elif step == "join":
            i, j = i - 2, j - 1
            to_ref[j] = i
            heard[i] = heard[i + 1] = True
        elif step.startswith("name"):
            count = int(step[4:])
            i, j = i - 1, j - count
            to_ref[j : j + count] = [i] * count
            heard[i] = True
            named.append((i, j, count))
        elif step == "del":
            i -= 1
        else:
            j -= 1
    return Alignment(cost[n][m], n, tuple(to_ref), tuple(heard), tuple(reversed(named)))


@dataclass(frozen=True)
class Repeat:
    """Words said again right after themselves: where in the transcript, which, and how many times in a row."""

    index: int
    words: tuple[str, ...]
    times: int

    def text(self) -> str:
        return " ".join(self.words)


def _runs(tokens: Sequence[str], size: int) -> list[tuple[int, int]]:
    """``(start, times)`` of every run of a *size*-word sequence said again right after itself."""
    runs = []
    i = 0
    while i + 2 * size <= len(tokens):
        times = 1
        while i + (times + 1) * size <= len(tokens) and tokens[i + times * size : i + (times + 1) * size] == tokens[i : i + size]:
            times += 1
        if times > 1:
            runs.append((i, times))
            i += times * size
        else:
            i += 1
    return runs


def repeated_sequences(hypothesis: Sequence[str], reference: Sequence[str]) -> list[Repeat]:
    """The token sequences *hypothesis* repeats in a row more times than *reference* does, longest first.

    One word said twice in a row counts (``the the``); a narration that
    repeats itself on purpose (``very, very``) is not held against its voice.
    """
    found: list[Repeat] = []
    taken: set[int] = set()
    for size in range(min(MAX_REPEAT_WORDS, len(hypothesis) // 2), 0, -1):
        allowed: dict[tuple[str, ...], int] = {}
        for start, times in _runs(reference, size):
            key = tuple(reference[start : start + size])
            allowed[key] = max(allowed.get(key, 1), times)
        for start, times in _runs(hypothesis, size):
            key = tuple(hypothesis[start : start + size])
            span = set(range(start, start + times * size))
            if times > allowed.get(key, 1) and not span & taken:
                found.append(Repeat(start, key, times))
                taken |= span
    return sorted(found, key=lambda r: r.index)


@dataclass(frozen=True)
class TranscriptCheck:
    """A transcript held against the narration it should say."""

    wer: float
    transcript: str
    repeated: Repeat | None = None
    #: Each lexicon term of the narration as heard (``pronunciation_check.TermHearing``).
    terms: tuple = ()
    #: Each proper noun heard by its sound in other words than written (``pronunciation_check.NameHearing``).
    names: tuple = ()
    #: Each word of the narration as heard, the words added where they were (``pronunciation_check.WordHeard``): the
    #: diff behind :attr:`wer`; empty for a check replayed from a record.
    words: tuple = ()

    @property
    def failed_terms(self) -> list:
        return [term for term in self.terms if not term.passed]

    @property
    def passed(self) -> bool:
        return self.wer <= MAX_WER and self.repeated is None and not self.failed_terms

    @property
    def heard_share(self) -> float:
        """The share of the narration's words heard as written (``1 - wer``, never below 0): it passes from
        ``1 - MAX_WER``."""
        return max(0.0, 1.0 - self.wer)

    def otherwise(self) -> list:
        """The words heard otherwise (``pronunciation_check.WordHeard``), in the order of the narration."""
        return [word for word in self.words if not word.heard]

    def reason(self) -> str:
        from app.services.video_studio.pronunciation_check import otherwise_line

        failed = self.failed_terms
        parts = [term.message() for term in failed[:3]]
        if len(failed) > 3:
            parts.append(f"{len(failed) - 3} more lexicon terms heard wrong")
        if self.wer > MAX_WER:
            listed = otherwise_line(self.words)
            heard = f": {listed}" if listed else ""
            parts.append(f"{self.wer:.0%} of its words differ from the narration (at most {MAX_WER:.0%}){heard}")
        if self.repeated is not None:
            parts.append(f'"{self.repeated.text()}" is said {self.repeated.times} times in a row')
        return "; ".join(parts) or "passed"


def hearing_of(
    text: str,
    transcript: str,
    *,
    lexicon: Any = None,
    words: Sequence[Any] = (),
    pauses: Sequence[tuple[float, float]] | None = None,
    nuclei: Sequence[float] | None = None,
) -> Any:
    """*transcript* (its *words* timed, when it has them) aligned on the narration *text*: the one normalisation of a
    narration's transcript check (``pronunciation_check.hear``), the narration's own and the whole render's alike."""
    from app.services.video_studio import pronunciation_check

    return pronunciation_check.hear(
        text, list(words) or transcript, lexicon, timed=bool(words), pauses=pauses, nuclei=nuclei
    )


def compare(
    text: str,
    transcript: str,
    *,
    lexicon: Any = None,
    words: Sequence[Any] = (),
    pauses: Sequence[tuple[float, float]] | None = None,
    nuclei: Sequence[float] | None = None,
) -> TranscriptCheck:
    """*transcript* (its *words* timed, when it has them) against *text*, the *lexicon*'s terms read as one token each.

    *pauses*: the silences of the audio transcribed (``pronunciation_check.pauses_in``): a term holding a long one fails.
    *nuclei*: its syllables (``pronunciation_check.nuclei_in``): a term read as words said letter by letter fails.
    """
    from app.services.video_studio.pronunciation_check import word_diff

    hearing = hearing_of(text, transcript, lexicon=lexicon, words=words, pauses=pauses, nuclei=nuclei)
    repeated = repeated_sequences([t.norm for t in hearing.heard], [t.norm for t in hearing.reference])
    return TranscriptCheck(
        hearing.wer,
        transcript.strip(),
        repeated[0] if repeated else None,
        tuple(hearing.terms),
        tuple(hearing.names),
        word_diff(hearing),
    )


# ── The record kept beside the cached narration ───────────────────────────


def _record_prefix(digest: str) -> str:
    from app.services.video_studio.voice import _CACHE_PREFIX

    return f"{_CACHE_PREFIX}/checks/{digest}"


def _passed_prefix(text_digest: str) -> str:
    from app.services.video_studio.voice import _CACHE_PREFIX

    return f"{_CACHE_PREFIX}/passed/{text_digest}"


def _stands(record: object) -> bool:
    """Whether *record* was checked under rules that still decide (:data:`RECORD_VERSION`, :data:`PASSED_SINCE`)."""
    if not isinstance(record, dict):
        return False
    version = record.get("version")
    if version == RECORD_VERSION:
        return True
    return isinstance(version, int) and PASSED_SINCE <= version < RECORD_VERSION and record.get("status") == PASSED


def failed(record: dict) -> bool:
    """Whether the narration *record* checked failed in the video's own voice: heard wrong, or said by another."""
    return record.get("status") == FAILED or bool(record.get("fallback"))


def pipeline_version() -> str:
    """The voice pipeline a narration's take is checked and said again under: ``check6.lexicon1.ladder1`` -
    the check's rules (:data:`RECORD_VERSION`), how the lexicon sends a term (``lexicon.RULES_VERSION``: the rewrite,
    a term set apart) and the retry ladder (:data:`LADDER_VERSION`). Kept on every record; a render records it with
    each scene playing a fallback take (``render_checks.fallback_digest``)."""
    from app.services.video_studio import lexicon

    return f"check{RECORD_VERSION}.lexicon{lexicon.RULES_VERSION}.ladder{LADDER_VERSION}"


def fallback_kind(record: Mapping) -> str | None:
    """The fallback take *record* (a check record, a voice summary) placed: :data:`VOICE_TAKE` (another voice),
    :data:`SLOWER_TAKE` or :data:`REFRAMED_TAKE`; ``None`` for the video's own voice at its speed."""
    if record.get("fallback") or record.get("voice_change"):
        return VOICE_TAKE
    if _speed_of(record) is not None:
        return SLOWER_TAKE
    if record.get("reframed"):
        return REFRAMED_TAKE
    return None


def _speed_of(record: Mapping) -> float | None:
    """The speed *record*'s take was said at when it is not the video's (``slower``); ``None`` at its own."""
    speed = record.get("speed")
    return float(speed) if isinstance(speed, int | float) and not isinstance(speed, bool) else None


def stale_fallback(record: Mapping) -> bool:
    """Whether *record* placed a fallback take under another voice pipeline than this release's: never placed
    again unheard - a change of the check, the lexicon's rules or the ladder may let the narration pass at its speed."""
    return fallback_kind(record) is not None and record.get("pipeline") != pipeline_version()


def _checked_at(record: dict) -> str:
    return str(record.get("checked_at") or "")


async def _records(prefix: str) -> list[dict]:
    """The records kept under *prefix* that still stand, in no order; none when they cannot be listed."""
    from app.services.file_storage import FileNotFoundInStorageError, file_storage

    try:
        names = await file_storage.list_object_names(f"{prefix}/")
    except Exception as exc:  # records that cannot be listed are records never written
        logger.debug("Voice check records not listed: %s", format_exception_message(exc))
        return []
    found = []
    for name in names:
        try:
            record = json.loads((await file_storage.read_range(name, 0, _RECORD_BYTES)).decode("utf-8"))
        except FileNotFoundInStorageError:
            continue
        except Exception as exc:  # a record that cannot be read is one never written
            logger.debug("Voice check record unreadable: %s", format_exception_message(exc))
            continue
        if _stands(record):
            found.append(record)
    return found


async def _add(prefix: str, body: dict) -> None:
    from app.services.file_storage import file_storage

    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    try:
        await file_storage.upload(data, "check.json", "application/json", prefix=prefix)
    except Exception as exc:  # the next render checks it again
        logger.warning("Voice check record not written: %s", format_exception_message(exc))


async def load_record(digest: str) -> dict | None:
    """What the last transcript check of the narration spoken under *digest* found, or ``None`` (never checked).

    Records are never written over (an object key names one content): each
    check adds one under the narration's prefix, and the newest decides.
    """
    return max(await _records(_record_prefix(digest)), key=_checked_at, default=None)


async def save_record(digest: str, record: dict) -> None:
    """Add a check's outcome for the narration spoken under *digest* (a new object: the old ones stay as they are)."""
    await _add(_record_prefix(digest), {"version": RECORD_VERSION, "digest": digest, **record})


async def passed_take(text_digest: str, scene_id: str) -> dict | None:
    """The newest take of the video's voice that passed its check on the words under *text_digest*.

    One of *scene_id* first (its preview, typically), else of any scene that
    says the same words; ``None`` when none did. A fallback take kept under
    another voice pipeline is none (:func:`stale_fallback`).
    """
    found = [
        r
        for r in await _records(_passed_prefix(text_digest))
        if r.get("status") == PASSED and r.get("placed") and not stale_fallback(r)
    ]
    return max(found, key=lambda r: (r.get("scene_id") == scene_id, _checked_at(r)), default=None)


async def keep_passed_take(text_digest: str, record: dict) -> None:
    """Keep beside the words under *text_digest* that a take of the video's voice passed (``voice.text_digest_of``)."""
    await _add(_passed_prefix(text_digest), {"version": RECORD_VERSION, "said": text_digest, **record})


# ── A narration spoken and heard back ─────────────────────────────────────


#: How each attempt is said in a failure: the voice that spoke it.
_KIND_WORDS = {
    "same": "the video's voice",
    "earlier": "the video's voice, a take that passed on the same words before",
    "again": "the video's voice again",
    "reframed": "the video's voice",
    "slower": "the video's voice, slower",
    "closest": "the closest voice",
}
#: The attempts the video's own voice speaks: a take of any other is a fallback.
OWN_VOICE = frozenset({"same", "earlier", "again", "reframed", "slower"})
#: The longest string sent a record keeps.
_SENT_KEPT = 1200
#: The longest reason a voice change keeps.
_REASON_KEPT = 600


@dataclass(frozen=True)
class VoiceChange:
    """A narration placed in another voice than the video's: which voice, and why.

    The video's voice failed its transcript check on every take; the voice of
    the same speech model nearest it (``voice.closest_voice``) passed. The
    audience would hear another voice for that scene: a render refuses it as
    a narration that failed (``voice_fallback``), a forced one plays it
    with that finding, and the quality check says it too.
    """

    #: The video's voice, as its speech model names it, and the voice that spoke the take placed.
    video_voice: str
    voice: str
    #: Why the video's voice failed: its closest take's check, and each take tried.
    reason: str
    #: What makes that voice the closest (``voice.ClosestVoice.why``).
    closeness: str

    def as_dict(self) -> dict:
        return {"video_voice": self.video_voice, "voice": self.voice, "reason": self.reason, "closeness": self.closeness}

    @classmethod
    def of(cls, raw: object) -> VoiceChange | None:
        """The change a record or a summary keeps; ``None`` for none, or one written in another shape."""
        if not isinstance(raw, dict) or not raw.get("voice") or not raw.get("video_voice"):
            return None
        return cls(str(raw["video_voice"]), str(raw["voice"]), str(raw.get("reason") or ""), str(raw.get("closeness") or ""))

    def sentence(self, scene_id: str | None) -> str:
        """What the audience hears, why, and what to change: the render's finding and the quality check's say it."""
        why = f" ({self.reason})" if self.reason else ""
        closest = f"; {self.voice} is {self.closeness}" if self.closeness else ""
        preview = f"render_video_scene(scene='{scene_id}')" if scene_id else "render_video_scene"
        return (
            f"its narration is spoken by {self.voice}, not the video's voice {self.video_voice}: "
            f"{self.video_voice} failed its transcript check{why}{closest} - reword the narration or add a "
            f"pronunciation entry (voiceover.pronunciations), then preview the scene ({preview}); or make "
            f"{self.voice} the video's voice (voiceover.voice)"
        )


@dataclass
class Attempt:
    voice: str
    #: ``same`` (the video's voice), ``earlier`` (a take of it that passed on the same words), ``again`` (the
    #: same voice, spoken anew), ``reframed`` (the same, each term heard wrong twice in another context),
    #: ``slower`` (the same, slower), ``closest`` (the voice of the same speech model nearest it).
    kind: str
    path: str | None
    duration: float | None = None
    check: TranscriptCheck | None = None
    error: str | None = None
    #: Where the take is kept in the voice cache (``voice.object_name`` / ``voice.take_name``).
    object_name: str | None = None
    #: How its lexicon terms were said (``ssml_phoneme`` / ``ipa_instruction`` / ``provider_lexicon`` / ``text_rewrite``,
    #: joined with ``+`` when several), ``None`` with no term; and the exact string sent to the speech model.
    mechanism: str | None = None
    sent: str | None = None
    #: The terms as heard, from a record (a check replayed without transcribing again).
    heard_terms: list[dict] | None = None
    #: Each word heard with its time in the take (seconds), when the speech-to-text gave them: the rendered-audio
    #: check reads the take's place in the stem from them instead of transcribing it again - while the take
    #: placed is still the very sound heard (``words_of``: its bytes' digest, ``heard_digest``).
    words: tuple[Timed, ...] | None = None
    heard_digest: str | None = None
    #: The language the speech-to-text heard the take in, as it names it: what the rendered-audio check holds the
    #: final voice to (``audio_qc``); ``None`` when it was not said.
    language: str | None = None
    #: The render whose check that take passed, for a take of the same words placed again (``Speaker.origin``).
    origin: str | None = None
    #: The speed it was spoken at, when it is not the video's (``slower``).
    speed: float | None = None
    #: The terms it said in another context, heard wrong in two takes before it (``lexicon.REFRAMED``).
    reframed: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return self.check is not None and self.check.passed

    def terms(self) -> list[dict]:
        if self.check is not None and self.check.terms:
            return [term.as_dict() for term in self.check.terms]
        return list(self.heard_terms or [])

    def tried(self) -> str:
        words = _KIND_WORDS.get(self.kind, self.kind)
        if self.kind == "slower" and self.speed is not None:
            words = f"the video's voice at {self.speed:g}"
        elif self.kind == "closest":
            words += f", {self.voice}"
        if self.reframed:
            words += f", {', '.join(self.reframed)} respelled alone in its clause"
        return f"{self.mechanism or 'no lexicon term'} ({words})"

    def as_dict(self) -> dict:
        out: dict[str, Any] = {"voice": self.voice, "kind": self.kind, "duration": self.duration}
        if self.speed is not None:
            out["speed"] = self.speed
        if self.reframed:
            out["reframed"] = list(self.reframed)
        if self.mechanism:
            out["mechanism"] = self.mechanism
        if self.sent is not None:
            out["sent"] = self.sent[:_SENT_KEPT]
        if self.check is not None:
            out.update({"wer": self.check.wer, "transcript": self.check.transcript[:2000]})
            if self.check.repeated is not None:
                out["repeated"] = {"words": self.check.repeated.text(), "times": self.check.repeated.times}
            if self.check.names:
                out["names"] = [name.as_dict() for name in self.check.names]
            if otherwise := self.check.otherwise():
                out["otherwise"] = [word.as_dict() for word in otherwise[:MAX_OTHERWISE_KEPT]]
        if terms := self.terms():
            out["terms"] = terms
        if self.origin:
            out["origin"] = self.origin
        if self.error:
            out["error"] = self.error[:300]
        return out


@dataclass
class SpokenSegment:
    """One narration as the render places it, and what its check found."""

    scene_id: str
    #: The WAV placed; ``None`` when no take could be spoken at all.
    path: str | None
    duration: float | None
    status: str
    attempts: list[Attempt] = field(default_factory=list)
    #: Spoken by another voice after the video's failed: the closest of its speech model.
    fallback: bool = False
    #: Why it was not checked, or why it failed - in the video's voice, for a fallback.
    reason: str | None = None
    notes: list[str] = field(default_factory=list)
    #: The voice a fallback was spoken by, and why; set exactly when *fallback* is.
    voice_change: VoiceChange | None = None
    #: The take placed was not heard by this render: placed from the check record that heard it - that check's time
    #: (``checked_at``), or ``an earlier render`` when no record says it. ``None``: heard back here.
    replayed_from: str | None = None

    @property
    def placed(self) -> Attempt | None:
        return next((a for a in reversed(self.attempts) if a.path == self.path), None) if self.path else None

    @property
    def heard_words(self) -> tuple[Timed, ...] | None:
        """The words heard in the take placed, timed in it; ``None`` when they are not known."""
        placed = self.placed
        return placed.words if placed is not None and placed.heard_digest else None

    @property
    def heard_digest(self) -> str | None:
        """The digest of the sound those words were heard in (:func:`take_digest`): a take changed since says so."""
        placed = self.placed
        return placed.heard_digest if placed is not None else None

    @property
    def transcript(self) -> str:
        """What its check heard in the take placed; empty when it was not heard."""
        placed = self.placed
        return placed.check.transcript if placed is not None and placed.check is not None else ""

    @property
    def language(self) -> str | None:
        """The language its check heard the take placed in, when the speech-to-text said it."""
        placed = self.placed
        return placed.language if placed is not None else None

    def unplayed_failures(self, *, played: bool) -> list[list[dict]]:
        """The lexicon terms each take of it the render does not play heard wrong, one list a take: every take
        but the one placed, every take when *played* is ``False`` (none placed) - what the final voice never hears,
        and its pronunciation summary counts all the same (``audio_qc.with_attempts``)."""
        placed = self.placed if played else None
        found = []
        for attempt in self.attempts:
            wrong = [term for term in attempt.terms() if not term.get("passed")] if attempt is not placed else []
            if wrong:
                found.append(wrong)
        return found

    def summary(self) -> dict:
        placed = self.placed
        out: dict[str, Any] = {
            "scene_id": self.scene_id,
            "status": self.status,
            "duration": round(self.duration, 3) if self.duration else self.duration,
            "attempts": len(self.attempts),
            "fallback": self.fallback,
        }
        if self.voice_change is not None:
            out["voice_change"] = self.voice_change.as_dict()
        if placed is not None and placed.check is not None:
            out["wer"] = placed.check.wer
        if placed is not None:
            out["voice"] = placed.voice
            if placed.speed is not None:
                out["speed"] = placed.speed
            if placed.reframed:
                out["reframed"] = list(placed.reframed)
            if placed.mechanism:
                out["mechanism"] = placed.mechanism
            if placed.sent is not None:
                out["sent"] = placed.sent[:_SENT_KEPT]
            if terms := placed.terms():
                out["terms"] = terms
        if self.reason:
            out["reason"] = self.reason[:600]
        return out


def _transcribed_samples(path: str) -> Any:
    """The WAV at *path* as the speech-to-text hears it: mono at :data:`TRANSCRIBE_RATE`."""
    from app.services.video_studio.stitch import read_segment, resample

    samples, rate = read_segment(path)
    return resample(samples, rate, TRANSCRIBE_RATE)


async def transcribe_wav(transcriber: Any, path: str, *, words: bool = False) -> Any:
    """A WAV transcribed: sent as 16 kHz mono 16-bit PCM, small enough for any narration."""
    import numpy as np

    from app.llm.speech import pcm_to_wav

    pcm = (np.clip(_transcribed_samples(path), -1.0, 1.0) * 32767.0).astype("<i2").tobytes()
    return await transcriber.transcribe(pcm_to_wav(pcm, rate=TRANSCRIBE_RATE), filename="voice.wav", words=words)


async def hear_back(transcriber: Any, text: str, path: str, lexicon: Any = None) -> tuple[TranscriptCheck, Any]:
    """The narration *text*, said into the WAV at *path*, heard back and held to what it should say: ``(the check,
    the transcript)``. The one transcript check of a narration - each take ``render_video``'s voice check hears
    (:func:`speak_checked`) and ``preview_voiceover``'s line alike: the same speech-to-text call (:func:`transcribe_wav`,
    its words timed), the same normalisation and lexicon (:func:`compare`, *lexicon* the one the narration is said
    with), the same pauses and syllables. A line that passes one passes the other on the same take."""
    result = await transcribe_wav(transcriber, path, words=True)
    check = compare(
        text,
        result.text,
        lexicon=lexicon,
        words=getattr(result, "words", None) or (),
        pauses=pauses_of_wav(path),
        nuclei=nuclei_of_wav(path),
    )
    return check, result


def pauses_of_wav(path: str) -> list[tuple[float, float]]:
    """The silences the voice in the WAV at *path* holds, on its transcript's clock (``pronunciation_check.pauses_in``)."""
    from app.services.video_studio.pronunciation_check import pauses_in

    return pauses_in(_transcribed_samples(path), TRANSCRIBE_RATE)


def nuclei_of_wav(path: str) -> list[float]:
    """When each syllable of the voice in the WAV at *path* peaks, on its transcript's clock (``pronunciation_check.nuclei_in``)."""
    from app.services.video_studio.pronunciation_check import nuclei_in

    return nuclei_in(_transcribed_samples(path), TRANSCRIBE_RATE)


def _duration(path: str) -> float | None:
    from app.services.video_studio.stitch import read_segment

    try:
        samples, rate = read_segment(path)
    except ValueError:
        return None
    return len(samples) / rate if rate else None


@dataclass(frozen=True)
class Speaker:
    """What speaks a video's narrations and what hears them back, for one render."""

    target: Any
    voiceover: Any
    #: ``app.llm.transcription.Transcriber``; ``None`` when none may transcribe for the person.
    transcriber: Any = None
    #: Why nothing hears the narrations back.
    unchecked_reason: str | None = None
    #: The video's pronunciation lexicon (``lexicon.Lexicon``: the platform's entries under the video's own);
    #: ``None``: the video's own entries alone.
    lexicon: Any = None
    #: The render that speaks them (``render 1a2b3c4d (a preview of scene intro)``): kept with a take that passes.
    origin: str | None = None

    @property
    def words(self) -> Any:
        """The lexicon the narrations are said and heard with."""
        from app.services.video_studio import voice as voices

        return voices.lexicon_of(self.voiceover, self.lexicon)


async def _speak(
    speaker: Speaker,
    line: Any,
    voice: str | None,
    speed: float,
    out_path: str,
    *,
    fresh: bool,
    level: str,
    reframe: frozenset[str] = frozenset(),
) -> tuple[list[str], str]:
    """*line* spoken into *out_path* by *voice* at *speed*, its terms said at *level*: ``(notes, where the take is kept)``.

    *fresh* speaks a new take whatever the cache holds (``voice.speak_take``),
    kept beside the cached one; otherwise the cached take, spoken on a miss.
    *reframe*: the terms said in another context (``lexicon.REFRAMED``).
    """
    from app.services.video_studio import voice as voices

    asked = {
        "text": line.text,
        "voice": voice,
        "style": speaker.voiceover.style,
        "speed": speed,
        "out_path": out_path,
        "previous_text": line.previous_text,
        "next_text": line.next_text,
        "lexicon": speaker.words,
        "level": level,
        "reframe": reframe,
    }
    if fresh:
        _voice, notes, name = await voices.speak_take(speaker.target, **asked)
        return notes, name
    _voice, notes = await voices.synthesize(speaker.target, **asked)
    spoken_as = replace_voice(speaker.voiceover, voice, speed=speed)
    # Keyed on the speed it was said at: a line's own speed would otherwise stand for a slower take's.
    digest = voices.digest_of(
        speaker.target, replace(line, speed=speed), spoken_as, speaker.words, level=level, reframe=reframe
    )
    return notes, voices.object_name(digest)


async def _heard(speaker: Speaker, line: Any, attempt: Attempt) -> None:
    path = attempt.path or ""
    heard_digest = take_digest(path)
    attempt.check, result = await hear_back(speaker.transcriber, line.text, path, speaker.words)
    attempt.words = _kept_words(getattr(result, "words", None) or ())
    attempt.heard_digest = heard_digest if attempt.words else None
    attempt.language = getattr(result, "language", None) or None


def take_digest(path: str) -> str | None:
    """The SHA-256 of a take's bytes: what its words were heard in; ``None`` when it cannot be read."""
    import hashlib

    try:
        with open(path, "rb") as source:
            return hashlib.file_digest(source, "sha256").hexdigest()
    except OSError:
        return None


def _kept_words(words: Sequence[Any]) -> tuple[Timed, ...] | None:
    """Transcribed words (``text``, ``start``, ``end``) as a take keeps them; ``None`` for none or too many to keep."""
    kept = tuple((str(w.text), round(float(w.start), 3), round(float(w.end), 3)) for w in words if str(w.text).strip())
    return kept if kept and len(kept) <= MAX_KEPT_WORDS else None


def _record_words(record: dict) -> tuple[Timed, ...] | None:
    """The words a record kept of the take it placed; ``None`` for a record written before it kept them."""
    raw = record.get("words")
    if not isinstance(raw, list) or not raw:
        return None
    try:
        return tuple((str(text), float(start), float(end)) for text, start, end in raw)
    except (TypeError, ValueError):
        return None


def _from_record(record: dict, kind: str, path: str) -> Attempt:
    """The take *record* says passed, fetched into *path*: heard as the record heard it, never transcribed again."""
    attempt = Attempt(str(record.get("voice") or ""), kind, path, _duration(path))
    attempt.check = TranscriptCheck(float(record.get("wer") or 0.0), str(record.get("transcript") or ""))
    attempt.object_name = str(record["placed"])
    attempt.mechanism, attempt.sent = record.get("mechanism"), record.get("sent")
    attempt.heard_terms = [t for t in record.get("terms") or [] if isinstance(t, dict)]
    attempt.reframed = tuple(str(term) for term in record.get("reframed") or [] if isinstance(term, str))
    attempt.speed = _speed_of(record)
    attempt.language = str(record["language"]) if record.get("language") else None
    # Its words, only for the very sound they were heard in: the bytes fetched are the ones checked.
    if record.get("words_digest") and record.get("words_digest") == take_digest(path):
        attempt.words, attempt.heard_digest = _record_words(record), str(record["words_digest"])
    return attempt


async def _earlier_take(text_digest: str, scene_id: str, tried: Sequence[Attempt], path: str) -> Attempt | None:
    """The take of the video's voice that passed its check on the same words before, fetched into *path*.

    ``None`` when no take did, or the one that did was already tried here.
    """
    from app.services.video_studio import voice as voices

    record = await passed_take(text_digest, scene_id)
    if record is None or str(record["placed"]) in {a.object_name for a in tried}:
        return None
    if not await voices.fetch(str(record["placed"]), path):
        return None
    attempt = _from_record(record, "earlier", path)
    attempt.origin = str(record.get("origin") or "") or None
    return attempt


def _failure(attempts: Sequence[Attempt], *, own: bool = False) -> tuple[Attempt | None, str]:
    """The closest take of *attempts* and why they failed, every mechanism tried said; ``None`` when none was spoken.

    The closest: the fewest terms heard wrong, then the fewest words, then no loop. *own*: the closest of the video's
    own voice (a failed narration is never placed in another voice), every take tried said all the same.
    """
    spoken = [a for a in attempts if a.path and (not own or a.kind in OWN_VOICE)]
    if not spoken:
        errors = "; ".join(f"{a.kind}: {a.error}" for a in attempts if a.error) or "no take could be spoken"
        return None, f"it could not be spoken ({errors[:300]})"
    closest = min(
        spoken,
        key=lambda a: (
            len(a.check.failed_terms) if a.check else 99,
            a.check.wer if a.check else 1.0,
            a.check.repeated is not None if a.check else True,
        ),
    )
    reason = closest.check.reason() if closest.check else "it could not be heard back"
    if any(a.mechanism for a in attempts):
        reason += "; tried: " + ", ".join(a.tried() for a in attempts)
    return closest, reason


def own_voice_failure(spoken: SpokenSegment) -> str:
    """Why the video's own voice did not say *spoken*: what its takes were heard as, or why none was spoken."""
    return _failure([a for a in spoken.attempts if a.kind in OWN_VOICE])[1]


def heard_wrong_twice(attempts: Sequence[Attempt]) -> frozenset[str]:
    """The lexicon terms two takes of the video's own voice heard wrong: the next take says them in another
    context (``lexicon.REFRAMED``), never the way it already sent them twice."""
    counts = Counter(
        term
        for attempt in attempts
        if attempt.kind in OWN_VOICE and attempt.check is not None
        for term in {hearing.term for hearing in attempt.check.failed_terms}
    )
    return frozenset(term for term, times in counts.items() if times >= 2)


async def speak_checked(speaker: Speaker, line: Any, *, workdir: str, fresh: bool = False) -> SpokenSegment:
    """*line* spoken and heard back: the take that passed, else the closest one, with every attempt said.

    The video's voice from the cache with the strongest mechanism its model
    offers for the lexicon; a take of the same voice that passed its check on
    the same words before - the scene's preview, a render before a neighbour
    was reworded; the same voice spoken anew with the next-strongest
    mechanism (the text rewrite, letters glued); with each term those two
    takes heard wrong in another context (``lexicon.REFRAMED``:
    :func:`heard_wrong_twice`), then once more slower (``voice.slower_speed``);
    then the voice of the same speech model nearest the video's
    (``voice.closest_voice``) - never another provider's: a fallback,
    with its :class:`VoiceChange` and failed in the video's voice. A narration
    whose record says it passed in its own voice is placed from the cache
    without being transcribed again (:attr:`SpokenSegment.replayed_from`).

    *fresh* (``render_video(recheck: true)``, ``voice_recheck.recheck``): a
    measurement of the narration as the video's voice says it now - no record
    placed unheard, every take of the video's voice spoken anew past the voice
    cache and heard back, no earlier take placed again. The record it leaves
    is what the next render places.
    """
    from app.services.video_studio import lexicon as lexicons
    from app.services.video_studio import voice as voices

    lexicon = speaker.words
    voiceover = speaker.voiceover
    # The speed this line is said at: its scene's own, else the video's.
    base = voices.line_speed(line, voiceover)
    digest = voices.digest_of(speaker.target, line, voiceover, lexicon)
    said = voices.text_digest_of(speaker.target, line, voiceover, lexicon)
    record = await load_record(digest) if speaker.transcriber is not None else None
    sid = line.scene_id
    # An IPA the video's model cannot take is said by its respelling: the result says so.
    notes: list[str] = voices.rendition_of(speaker.target, line, voiceover, lexicon).notes(_model_of(speaker.target))
    if (
        not fresh
        and record
        and not failed(record)
        and record.get("status") == PASSED
        and record.get("placed")
        and not stale_fallback(record)
    ):
        # Heard back once already: the take that passed, from the cache, never transcribed again - but a fallback take
        # placed under another voice pipeline, which the ladder below hears again from its first take.
        path = f"{workdir}/voice-{sid}.wav"
        if await voices.fetch(str(record["placed"]), path):
            heard = _from_record(record, "same", path)
            if heard.speed is not None:
                notes.append(_slower_note(sid, heard.speed, base))
            if heard.reframed:
                notes.append(_reframed_note(sid, heard.reframed))
            at = _checked_at(record)
            replayed = f"{at[:16].replace('T', ' ')} UTC" if at else "an earlier render"
            return SpokenSegment(sid, path, heard.duration, PASSED, [heard], notes=notes, replayed_from=replayed)
        record = None

    # The cached take of a narration that failed in its own voice is known bad: spoken anew from the first try - and
    # every take of a fresh measurement.
    known_bad = fresh or bool(record and failed(record))
    attempts: list[Attempt] = []

    async def take(
        kind: str,
        voice_name: str | None,
        speed: float,
        *,
        fresh: bool,
        level: str,
        label: str = "",
        reframe: frozenset[str] = frozenset(),
    ) -> SpokenSegment | None:
        """One take spoken and heard back (``attempts[-1]``): a segment when it cannot be heard back, else ``None``."""
        path = f"{workdir}/voice-{sid}-{len(attempts)}.wav"
        attempt = Attempt(label or voice_name or getattr(speaker.target, "voice", None) or "its default voice", kind, path)
        attempt.speed = speed if speed != base else None
        attempt.reframed = tuple(sorted(reframe))
        attempts.append(attempt)
        try:
            rendition = voices.rendition_of(
                speaker.target, line, replace_voice(voiceover, voice_name), lexicon, level=level, reframe=reframe
            )
            attempt.mechanism, attempt.sent = rendition.mechanism, rendition.text
            spoken_notes, attempt.object_name = await _speak(
                speaker, line, voice_name, speed, path, fresh=fresh, level=level, reframe=reframe
            )
        except Exception as exc:
            attempt.path, attempt.error = None, format_exception_message(exc)
            return None
        spoken_notes = [*spoken_notes, *rendition.notes(_model_of(speaker.target))]
        notes.extend(note for note in spoken_notes if note not in notes)
        attempt.duration = _duration(path)
        if speaker.transcriber is None:
            return SpokenSegment(sid, path, attempt.duration, UNCHECKED, attempts, reason=speaker.unchecked_reason, notes=notes)
        try:
            await _heard(speaker, line, attempt)
        except Exception as exc:  # the speech-to-text failing is not the narration failing
            reason = f"speech-to-text failed ({format_exception_message(exc)[:200]})"
            return SpokenSegment(sid, path, attempt.duration, UNCHECKED, attempts, reason=reason, notes=notes)
        return None

    def ladder() -> Iterator[tuple[str, float, bool, str, frozenset[str]]]:
        """The takes of the video's voice, each decided once those before it were heard."""
        yield "same", base, known_bad, lexicons.STRONGEST, frozenset()
        yield "earlier", base, False, "", frozenset()
        yield "again", base, True, lexicons.FALLBACK, frozenset()
        if twice := heard_wrong_twice(attempts):
            yield "reframed", base, True, lexicons.REFRAMED, twice
        if (slower := voices.slower_speed(base)) is not None:
            twice = heard_wrong_twice(attempts)
            yield "slower", slower, True, lexicons.REFRAMED if twice else lexicons.FALLBACK, twice

    voice_name = voiceover.voice
    for kind, speed, anew, level, reframe in ladder():
        if kind == "earlier":
            if fresh:
                # A measurement places nothing it did not hear now.
                continue
            path = f"{workdir}/voice-{sid}-{len(attempts)}.wav"
            earlier = await _earlier_take(said, sid, attempts, path) if speaker.transcriber is not None else None
            if earlier is None:
                continue
            why = _failure(attempts)[1]
            attempts.append(earlier)
            await save_record(digest, _record(PASSED, earlier, attempts))
            where = f" in {earlier.origin}" if earlier.origin else ""
            notes.append(
                f"Scene {sid}: its narration as spoken for this render failed its transcript check ({why}); the take "
                f"of the video's voice that passed its check on the same words{where} is placed instead."
            )
            return SpokenSegment(sid, path, earlier.duration, PASSED, attempts, notes=notes)
        if (unheard := await take(kind, voice_name, speed, fresh=anew, level=level, reframe=reframe)) is not None:
            return unheard
        attempt = attempts[-1]
        if not attempt.passed:
            continue
        if kind == "slower":
            notes.append(_slower_note(sid, speed, base))
        if attempt.reframed:
            notes.append(_reframed_note(sid, attempt.reframed))
        await save_record(digest, _record(PASSED, attempt, attempts))
        passed = {**_record(PASSED, attempt, [attempt]), "scene_id": sid, "origin": speaker.origin}
        await keep_passed_take(said, passed)
        return SpokenSegment(sid, attempt.path, attempt.duration, PASSED, attempts, notes=notes)

    own, why = _failure(attempts)
    if own is None:
        return SpokenSegment(sid, None, None, FAILED, attempts, reason=why, notes=notes)
    # The video's voice failed every take: the voice of the same speech model nearest it, never another provider's.
    try:
        closest = await voices.closest_voice(speaker.target, voiceover.voice)
        unavailable = None if closest is not None else f"{_model_of(speaker.target)} lists no other voice"
    except Exception as exc:  # voices that cannot be listed leave the narration failed, as it is
        closest = None
        unavailable = f"the voices of {_model_of(speaker.target)} could not be listed ({format_exception_message(exc)[:160]})"
    if closest is not None:
        change = VoiceChange(closest.of, closest.label, why[:_REASON_KEPT], closest.why())
        label = closest.label
        if (unheard := await take("closest", closest.voice, base, fresh=fresh, level=lexicons.STRONGEST, label=label)) is not None:
            unheard.fallback, unheard.voice_change = True, change
            return unheard
        attempt = attempts[-1]
        if attempt.passed:
            # Said right by another voice: that voice's own pass, and a failure of the video's - the next
            # render tries the video's voice again, and the gate holds the video until it passes.
            mine = voices.digest_of(speaker.target, line, replace_voice(voiceover, closest.voice), lexicon)
            await save_record(mine, _record(PASSED, attempt, [attempt]))
            kept = _record(FAILED, own, attempts, fallback=attempt, voice_change=change)
            heard_as = own.check.reason() if own.check else why
            kept["reason"] = f"{heard_as}; the closest voice of its speech model ({attempt.voice}) passed in its place"
            await save_record(digest, kept)
            return SpokenSegment(
                sid, attempt.path, attempt.duration, PASSED, attempts, True, reason=why, notes=notes, voice_change=change
            )
    # Never placed in another voice: the closest take a forced render places is the video's own.
    closest_take, reason = _failure(attempts, own=True)
    closest_take = closest_take or own
    if unavailable:
        reason += f"; no other voice tried: {unavailable}"
    await save_record(digest, _record(FAILED, closest_take, attempts))
    return SpokenSegment(sid, closest_take.path, closest_take.duration, FAILED, attempts, reason=reason, notes=notes)


def _slower_note(scene_id: str, speed: float, video_speed: float) -> str:
    return (
        f"Scene {scene_id}: its narration is spoken slower ({speed:g}, the video's speed is {video_speed:g}), the "
        "lexicon written into its text: the video's voice failed its transcript check at its own speed."
    )


def _reframed_note(scene_id: str, terms: Sequence[str]) -> str:
    return (
        f"Scene {scene_id}: its narration is spoken with {', '.join(terms)} respelled alone in its clause (a comma "
        "around it): the video's voice said it wrong twice as the lexicon writes it into the text - listen to it."
    )


def _model_of(target: Any) -> str:
    return str(getattr(target, "model", None) or "the speech model")


def replace_voice(voiceover: Any, voice: str | None, *, speed: float | None = None) -> Any:
    """*voiceover* spoken with *voice* (``None``: the speech model's own default), at *speed* when given."""
    update: dict[str, Any] = {"voice": voice}
    if speed is not None:
        update["speed"] = speed
    if hasattr(voiceover, "model_copy"):
        return voiceover.model_copy(update=update)
    return replace(voiceover, **update)


def _record(
    status: str,
    placed: Attempt,
    attempts: list[Attempt],
    *,
    fallback: Attempt | None = None,
    voice_change: VoiceChange | None = None,
) -> dict:
    """A check's outcome as kept: *placed* is the take that passed (its object in the voice cache), else the closest.

    *fallback*: the take of another voice that passed after the video's voice failed - the closest of its
    speech model, *voice_change* says which and why: the narration failed in its own voice, and the record
    says which voice took over.
    """
    out = {
        "status": status,
        # The voice pipeline it was checked under: a fallback take of another one is heard again.
        "pipeline": pipeline_version(),
        "placed": placed.object_name if status == PASSED else None,
        "duration": placed.duration,
        "voice": placed.voice,
        "speed": placed.speed,
        "fallback": fallback is not None,
        "wer": placed.check.wer if placed.check else None,
        "transcript": placed.check.transcript[:2000] if placed.check else None,
        "mechanism": placed.mechanism,
        "sent": placed.sent[:_SENT_KEPT] if placed.sent is not None else None,
        "terms": placed.terms(),
        "words": [list(word) for word in placed.words] if status == PASSED and placed.words else None,
        "words_digest": placed.heard_digest if status == PASSED and placed.words else None,
        "language": placed.language,
        "reason": None if status == PASSED else (placed.check.reason() if placed.check else placed.error),
        "attempts": [a.as_dict() for a in attempts],
        "checked_at": datetime.now(UTC).isoformat(),
    }
    if fallback is not None:
        out["fallback_voice"] = fallback.voice
    if voice_change is not None:
        out["voice_change"] = voice_change.as_dict()
    if placed.reframed:
        out["reframed"] = list(placed.reframed)
    # How the placed take sounds (``perceptual.timbre``): what the quality check's synthetic_voice reads (issue
    # #10036). A take of the same voice heard at speed 1 (a ``same`` attempt when the video speaks at 1) is kept
    # beside it as the reference a slowed take is held to.
    if status == PASSED and (measured := _timbre_of(placed.path)) is not None:
        out["timbre"] = measured
        reference = next(
            (
                t
                for a in attempts
                if a is not placed
                and a.voice == placed.voice
                and a.speed == 1
                and (t := _timbre_of(a.path)) is not None
            ),
            None,
        )
        if reference is not None:
            out["timbre_reference"] = reference
    return out


def _timbre_of(path: str | None) -> dict | None:
    """The timbre of the WAV at *path* (``perceptual.Timbre.as_dict``); ``None`` when it cannot be read."""
    if not path:
        return None
    from app.services.video_studio import perceptual
    from app.services.video_studio.stitch import read_segment

    try:
        samples, rate = read_segment(path)
        measured = perceptual.timbre(samples, rate)
    except Exception as exc:  # a timbre never read is never judged
        logger.debug("Timbre of a narration not read: %s", format_exception_message(exc))
        return None
    return measured.as_dict() if measured is not None else None


async def narration_timbres(target: Any, spec: Any, lexicon: Any = None) -> dict[str, tuple[dict, dict | None]]:
    """``scene id -> (timbre, timbre at speed 1 or None)`` of each narration of *spec* whose last check kept one."""
    return {
        sid: (record["timbre"], record.get("timbre_reference"))
        for sid, record in await _narration_records(target, spec, lexicon)
        if isinstance(record.get("timbre"), dict)
    }


async def _narration_records(target: Any, spec: Any, lexicon: Any = None) -> list[tuple[str, dict]]:
    """``(scene id, record)`` of each narration of *spec* checked before, as *target* speaks it: its last check."""
    from app.services.video_studio import voice as voices

    found = []
    for line in voices.lines_of(spec):
        record = await load_record(voices.digest_of(target, line, spec.voiceover, lexicon))
        if record:
            found.append((line.scene_id, record))
    return found


async def failed_narrations(target: Any, spec: Any, lexicon: Any = None) -> list[tuple[str, dict]]:
    """``(scene id, record)`` of each narration of *spec* whose last transcript check failed in its own voice, as
    *target* speaks it: heard wrong, or said by another voice instead."""
    return [(sid, record) for sid, record in await _narration_records(target, spec, lexicon) if failed(record)]


async def voice_changes(target: Any, spec: Any, lexicon: Any = None) -> list[tuple[str, VoiceChange]]:
    """``(scene id, change)`` of each narration of *spec* whose last check placed another voice than the video's."""
    found = []
    for sid, record in await _narration_records(target, spec, lexicon):
        if (change := VoiceChange.of(record.get("voice_change"))) is not None:
            found.append((sid, change))
    return found


# ── A fallback take, and what would clear it ──────────────────────


def _listed(items: Sequence[str]) -> str:
    items = list(dict.fromkeys(items))
    return items[0] if len(items) == 1 else f"{', '.join(items[:-1])} and {items[-1]}"


def otherwise_of(attempt: Mapping, *, limit: int = 6) -> str:
    """The words a take as kept (:meth:`Attempt.as_dict`) heard otherwise, said (``'Their' heard as 'There'``); empty
    for a take that kept none (heard right, or kept before it did)."""
    from app.services.video_studio.pronunciation_check import WordHeard, otherwise_line

    kept = attempt.get("otherwise")
    words = [word for raw in kept if (word := WordHeard.of(raw)) is not None] if isinstance(kept, list) else []
    return otherwise_line(words, limit=limit)


def _take_failed(attempt: Mapping) -> bool:
    """Whether a take as kept (:meth:`Attempt.as_dict`) failed its check: a term heard wrong, too many words, a loop."""
    terms = [term for term in attempt.get("terms") or [] if isinstance(term, dict)]
    wer = attempt.get("wer")
    return (
        any(not term.get("passed") for term in terms)
        or bool(attempt.get("repeated"))
        or (isinstance(wer, int | float) and wer > MAX_WER)
        or bool(attempt.get("error"))
    )


@dataclass(frozen=True)
class FallbackTake:
    """A narration placed with a fallback take (:func:`fallback_kind`): what the take is, why the video's voice
    failed at its speed, the voice pipeline it was placed under, and the fix that would let it pass at that speed -
    what a render records of the scene (``VideoRender.drawing["fallbacks"]``), ``render_video`` offers to re-check and
    the save's quality check says as a fact."""

    scene_id: str
    kind: str
    #: The video's speed for this narration (its scene's own), and the take's when it is slower.
    video_speed: float
    speed: float | None = None
    #: The terms the take sets apart in their clause (``lexicon.REFRAMED``), and the voice of another voice's take.
    reframed: tuple[str, ...] = ()
    voice: str | None = None
    #: ``(term, what was heard)`` of each lexicon term a take at the video's speed heard wrong, the first heard first.
    terms: tuple[tuple[str, str], ...] = ()
    #: What those takes heard wrong when no term was: ``'the team' 2 times in a row``, ``14% of its words otherwise
    #: ['Their' heard as 'There']`` (the words a take kept heard otherwise, :func:`otherwise_of`).
    words: str = ""
    #: The voice pipeline it was placed under (:func:`pipeline_version`); ``None``: never recorded.
    pipeline: str | None = None

    def take(self) -> str:
        """``a 0.85 take``, ``a take with OpenAEV respelled alone in its clause``, ``a take in another voice (Anchor)``."""
        if self.kind == SLOWER_TAKE and self.speed is not None:
            return f"a {self.speed:g} take"
        if self.kind == REFRAMED_TAKE:
            return f"a take with {_listed(self.reframed or ('a term',))} respelled alone in its clause"
        return f"a take in another voice ({self.voice or 'not the video voice'})"

    def why(self) -> str:
        """What the video's voice did at its speed: the terms heard wrong, or the words."""
        at = f"at its speed {self.video_speed:g}"
        if self.terms:
            heard = ", ".join(f"{term} as '{said[:60]}'" if said else f"{term} not at all" for term, said in self.terms)
            return f"the video's voice heard {heard} {at}"
        if self.words:
            return f"the video's voice said {self.words} {at}"
        return f"the video's voice failed its transcript check {at}"

    def fix(self) -> str:
        """What would let the narration pass at the video's speed: a lexicon entry for the term, or a reworded clause."""
        if self.terms:
            names = [term for term, _said in self.terms]
            listed, them = _listed(names), "it" if len(names) == 1 else "them"
            entry = (
                f"a lexicon entry for {listed} the voice says right"
                f" {'inside its sentence' if self.kind == REFRAMED_TAKE else 'at full speed'}"
                " (voiceover.pronunciations: an ipa, or a say form spelled as it sounds)"
            )
            if self.kind == REFRAMED_TAKE:
                return f"{entry}, or a clause reworded to set {them} apart itself"
            return f"{entry}, or a reworded clause around {them}"
        return f"a reworded clause ({self.words})" if self.words else "a reworded narration"

    def fact(self) -> str:
        """``scene `s2` plays a 0.85 take: the video's voice heard OpenAEV as 'OpenA' at its speed 1 - a lexicon entry
        for OpenAEV ... would let it pass at its speed``: what the quality check and the render say of it."""
        return f"scene `{self.scene_id}` plays {self.take()}: {self.why()} - {self.fix()} would let it pass at its speed"

    def as_dict(self) -> dict:
        out: dict[str, Any] = {"kind": self.kind, "video_speed": self.video_speed, "pipeline": self.pipeline}
        if self.speed is not None:
            out["speed"] = self.speed
        if self.reframed:
            out["reframed"] = list(self.reframed)
        if self.voice:
            out["voice"] = self.voice
        if self.terms:
            out["terms"] = [{"term": term, "heard": said} for term, said in self.terms]
        if self.words:
            out["words"] = self.words
        return out

    @classmethod
    def of(cls, scene_id: str, raw: object) -> FallbackTake | None:
        """The take :meth:`as_dict` kept; ``None`` for none, or one kept in another shape."""
        if not isinstance(raw, dict) or raw.get("kind") not in (SLOWER_TAKE, REFRAMED_TAKE, VOICE_TAKE):
            return None
        try:
            video_speed = float(raw.get("video_speed") or 1.0)
        except (TypeError, ValueError):
            video_speed = 1.0
        terms = tuple(
            (str(item["term"]), str(item.get("heard") or ""))
            for item in raw.get("terms") or []
            if isinstance(item, dict) and item.get("term")
        )
        return cls(
            scene_id,
            str(raw["kind"]),
            video_speed,
            _speed_of(raw),
            tuple(str(term) for term in raw.get("reframed") or [] if isinstance(term, str)),
            str(raw["voice"]) if raw.get("voice") else None,
            terms,
            str(raw.get("words") or ""),
            str(raw["pipeline"]) if raw.get("pipeline") else None,
        )

    @classmethod
    def of_record(cls, scene_id: str, record: Mapping, video_speed: float) -> FallbackTake | None:
        """The fallback take a check *record* - or a render's voice summary (``SpokenSegment.summary``) - placed, with
        what each take of the video's voice at its speed heard wrong; ``None`` when it placed none.

        A record keeps its takes (``attempts``: a list, :meth:`Attempt.as_dict`); a summary keeps their count alone,
        so a take read from one names the kind, the speed, the terms set apart and the voice - never what the takes at
        the video's speed heard wrong (its ``why`` says the voice failed its transcript check, nothing more)."""
        kind = fallback_kind(record)
        if kind is None:
            return None
        kept = record.get("attempts")
        attempts = [a for a in kept if isinstance(a, dict)] if isinstance(kept, list) else []
        own = [a for a in attempts if a.get("kind") in OWN_VOICE and _speed_of(a) is None and _take_failed(a)]
        terms: dict[str, str] = {}
        words = ""
        for attempt in own:
            for term in attempt.get("terms") or []:
                if isinstance(term, dict) and term.get("term") and not term.get("passed"):
                    terms.setdefault(str(term["term"]), str(term.get("heard") or ""))
            repeated = attempt.get("repeated")
            if not words and isinstance(repeated, dict) and repeated.get("words"):
                words = f"'{repeated['words']}' {repeated.get('times') or 2} times in a row"
        if not terms and not words:
            measured = [a for a in own if isinstance(a.get("wer"), int | float)]
            worst = max(measured, key=lambda a: float(a["wer"]), default=None)
            if worst is not None and float(worst["wer"]) > MAX_WER:
                words = f"{float(worst['wer']):.0%} of its words otherwise"
                if listed := otherwise_of(worst):
                    words += f" [{listed}]"
        voice = (record.get("fallback_voice") or record.get("voice")) if kind == VOICE_TAKE else None
        return cls(
            scene_id,
            kind,
            float(video_speed),
            _speed_of(record),
            tuple(str(term) for term in record.get("reframed") or [] if isinstance(term, str)),
            str(voice) if voice else None,
            tuple(terms.items()),
            words,
            str(record["pipeline"]) if record.get("pipeline") else None,
        )


async def fallback_takes(target: Any, spec: Any, lexicon: Any = None) -> list[FallbackTake]:
    """Each narration of *spec* whose last check placed a fallback take of the video's own voice - slower, a term set
    apart - as *target* speaks it: what the save's quality check says as a fact, with its fix. A take of
    another voice is a ``voice_fallback`` finding of its own (:func:`voice_changes`)."""
    from app.services.video_studio import voice as voices

    found = []
    for line in voices.lines_of(spec):
        record = await load_record(voices.digest_of(target, line, spec.voiceover, lexicon))
        if not record or record.get("status") != PASSED or failed(record):
            continue
        take = FallbackTake.of_record(line.scene_id, record, voices.line_speed(line, spec.voiceover))
        if take is not None and take.kind != VOICE_TAKE:
            found.append(take)
    return found


async def place_kept(line: Any, take: Mapping, *, workdir: str, video_speed: float) -> SpokenSegment | None:
    """*line* placed with the take a render played (``VideoRender.drawing["takes"]``): what a splice keeps of
    each narration it does not re-check - fetched from the voice cache, never spoken nor heard again; heard as its
    record heard it while that record still places it. ``None`` when that take cannot be fetched."""
    from app.services.video_studio import voice as voices

    name = str(take.get("take") or "")
    path = f"{workdir}/voice-{line.scene_id}.wav"
    if not name or not await voices.fetch(name, path):
        return None
    record = await load_record(str(take["digest"])) if take.get("digest") else None
    if record and record.get("status") == PASSED and record.get("placed") == name:
        attempt = _from_record(record, "same", path)
    else:
        attempt = Attempt(str(take.get("voice") or ""), "same", path, _duration(path))
        attempt.object_name, attempt.speed = name, _speed_of(take)
        attempt.reframed = tuple(str(term) for term in take.get("reframed") or [] if isinstance(term, str))
        attempt.mechanism = str(take["mechanism"]) if take.get("mechanism") else None
    notes = []
    if attempt.speed is not None:
        notes.append(_slower_note(line.scene_id, attempt.speed, video_speed))
    if attempt.reframed:
        notes.append(_reframed_note(line.scene_id, attempt.reframed))
    return SpokenSegment(line.scene_id, path, attempt.duration, PASSED, [attempt], notes=notes)
