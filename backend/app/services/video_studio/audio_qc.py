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

"""What a render finally sounds like, heard back.

After the mux, the final AAC is demuxed from the MP4 and decoded; the voice
stem (``stitch.py``, kept as its own file before the mix) is heard beside it.
Three checks, in code (invariant 16), each finding with its time in the MP4:

* **The voice-over transcribed.** The stem is put through the final mix's
  encode (``encoder.encode_voice_like_mix``: AAC at the mix's rate and
  bitrate, in an MP4 container, demuxed and decoded back), so a word broken by
  the resampling, the stitching or the encoding is heard as the viewer hears
  it. Each narration is heard on its own placed span - where the stem placed
  its take (``stitch.Stem.spans``), never its scene's bounds on screen - and
  alone there, every other sound of the stem silenced: its span is
  transcribed where the stem's voice has energy (:func:`narration_windows`,
  ``speech_timing.voiced_runs``: a span, or a stretch of one, with no voice is
  never sent - a music-only moment holds nothing to hear), with word times
  (``app.llm.transcription``). What the speech-to-text writes where nobody
  speaks is dropped and counted, never graded (:func:`kept_words`): a window
  heard in another language than the voice-over's, and a segment saying what
  its model is known to write over silence or music
  (``transcription.known_hallucinations``) that the narration does not write.
  The words a narration's own windows heard, inside its span, are aligned on
  its text alone with the very normalisation of its own check
  (``voice_check.hearing_of``: the lexicon's terms as one token each,
  ``pronunciation_check.hear``): no word is ever graded against another
  narration. A scene's word error rate is at most 100 %; over
  ``voice_check.MAX_WER`` for the whole voice-over the render fails, a
  finding for each scene heard wrong - an advisory naming both transcripts
  for a narration its own check passed (:class:`OwnCheck`): that check
  decides its words. A narration ``voice_check`` already heard is read from
  its own transcript instead, its word times moved to where the stem
  places the take, as long as the encoded voice still plays that very take
  over its span (:func:`plays_the_take`: band energies, every 20 ms of its
  speech): only a narration not heard before, or one the stitching, the
  resampling or the encoding changed, is transcribed again - a clean render of
  N narrations transcribes N times, not 2N. A scene preview grades its own
  narration, never the neighbours' its transitions play
  (:func:`graded_narrations`). The summary names the speech-to-text model
  and provider.
* **The lexicon's terms.** Each narration's alignment read at each term it
  writes (:func:`pronunciation_findings`): a term heard wrong is a
  ``pronunciation`` finding at its time, one accepted in other words than its
  written form a ``pronunciation`` warning, and the ``pronunciation``
  section says per term its occurrences, how many passed and what was heard -
  and each take the render does not play that failed on it
  (:func:`with_attempts`): a take replaced by one that passed still counts.
  A term its narration's words do not say is looked for in the whole
  transcript within :data:`TERM_REACH` of its planned time first
  (:func:`heard_terms`): heard there, it passes where it was heard.
* **A sentence said twice.** A sentence of the narration (four words or more)
  heard more times than it is written (:func:`repeated_sentences`), each
  hearing placed in the scene it falls in: one a scene's own text explains at
  least as closely is that scene saying its text, and the finding names the
  scene of the extra hearing and what was heard there. One that
  two narrations write is refused at save (:func:`shared_sentences`):
  each scene preview hears its narration alone, so only the whole render would.
* **A stutter.** The same 80-200 ms of sound, three or more times in a row,
  inside a voiced region (:func:`find_stutters`): log band energies every
  5 ms compared at every lag of that range, the run kept only when the chunk
  changes within itself (a held vowel or a tone repeats without stuttering)
  and the waveform itself repeats at the exact lag. Looked for on the stem
  (the voice stuttered) and on the final mix (the mix or the mux did); one
  found only in the mix counts when the mix repeats what the voice had
  already said rather than what it says (:func:`repeats_the_voice`), so a
  music loop under a voice is never taken for one.
"""

from __future__ import annotations

import io
import logging
import math
import re
import wave
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import numpy as np

from app.core.utils import format_exception_message
from app.services.video_studio.render_checks import (
    FAIL,
    FAILED,
    PASSED,
    PRONUNCIATION,
    REPEATED_SENTENCE,
    SKIPPED,
    STUTTER,
    TRANSCRIPT,
    WARN,
    Finding,
    clock,
)

logger = logging.getLogger(__name__)

#: The rate everything is heard at: speech, not music.
ANALYSIS_RATE = 16000
FRAME_SECONDS = 0.020
HOP_SECONDS = 0.005
_BANDS = 40
_FMIN, _FMAX = 100.0, 7600.0
#: The chunk a stutter repeats, in seconds, and how many times in a row it must be heard.
STUTTER_SECONDS = (0.08, 0.20)
STUTTER_TIMES = 3
#: Two frames of band energies under this mean distance (dB) are the same sound.
SAME_DB = 2.5
#: A chunk whose band energies spread less than this over it (mean of each band's deviation, dB) is a held
#: sound - a tone, a sustained vowel - not a repeated one. Measured: a held tone 0.15, speech 3.6 to 3.9.
CHANGE_DB = 1.5
#: The share of a stutter's frames that must be loud (within :data:`QUIET_DB` of the voice's level).
LOUD_SHARE = 0.5
QUIET_DB = 32.0
#: The waveform repeats at the lag at least this much (normalised correlation) for a stutter.
WAVE_CORRELATION = 0.8
#: A stutter of the mix counts when it is nearer the voice's previous chunk than its own by this much (dB).
MIX_MARGIN_DB = 1.0
#: A sentence this long at least (words) is looked for twice; shorter ones recur naturally.
SENTENCE_WORDS = 4
#: How far a heard sentence may differ from the written one (word edit distance over its length).
SENTENCE_DISTANCE = 0.25
#: A narration's span is transcribed in windows split on silences this long, of at most this length.
SPLIT_SILENCE = 1.5
PART_SECONDS = 90.0
_PAD = 0.2
#: A lexicon term its narration's words do not say is looked for this far (seconds) either side of its planned time.
TERM_REACH = 3.0
#: The segments dropped as written where nobody speaks that the summary lists (all are counted).
_LISTED_HALLUCINATIONS = 10
#: A take heard already is the voice heard when every 20 ms of its speech is within this (dB, the bands' mean, the
#: two levels aligned) of what is heard there, at most :data:`TAKE_LAG_SECONDS` off its place (an encoder's delay).
#: Measured: the final mix's AAC encode 0.3-0.5, a 120 ms stutter 6.4, another take 8.1, a dropout 65.
SAME_TAKE_DB = 3.0
TAKE_LAG_SECONDS = 0.05
#: The bands a take sounds in at a moment: within this (dB) of its loudest band then.
TAKE_BAND_RANGE_DB = 40.0
_TAKE_SMOOTH_FRAMES = 4


# ── Hearing ───────────────────────────────────────────────────────────────


def band_levels(samples: np.ndarray, rate: int = ANALYSIS_RATE) -> np.ndarray:
    """Log energies (dB) of *samples* in :data:`_BANDS` bands, every :data:`HOP_SECONDS`: ``(frames, bands)``."""
    n, hop = round(FRAME_SECONDS * rate), round(HOP_SECONDS * rate)
    if len(samples) < n:
        return np.zeros((0, _BANDS), dtype=np.float32)
    nfft = 1 << (n - 1).bit_length()
    freqs = np.fft.rfftfreq(nfft, 1.0 / rate)
    edges = np.geomspace(_FMIN, min(_FMAX, rate / 2 - 1), _BANDS + 1)
    starts = np.clip(np.searchsorted(freqs, edges[:-1]), 0, len(freqs) - 1)
    window = np.hanning(n)
    frames = np.lib.stride_tricks.sliding_window_view(samples.astype(np.float64), n)[::hop]
    out = np.empty((len(frames), _BANDS), dtype=np.float32)
    for begin in range(0, len(frames), 4096):
        block = np.abs(np.fft.rfft(frames[begin : begin + 4096] * window, nfft)) ** 2
        out[begin : begin + len(block)] = np.add.reduceat(block, starts, axis=1)
    levels = 10.0 * np.log10(out + 1e-12)
    floor = float(levels.max()) - 80.0 if levels.size else 0.0
    return np.maximum(levels, floor).astype(np.float32)


def _loud(levels: np.ndarray) -> np.ndarray:
    total = 10.0 * np.log10(np.power(10.0, levels / 10.0).sum(axis=1) + 1e-12)
    return total > (float(np.percentile(total, 95)) - QUIET_DB if len(total) else 0.0)


def _runs(mask: np.ndarray, *, fill: int = 0) -> list[tuple[int, int]]:
    """``[start, end)`` of every run of ``True`` in *mask*, gaps of at most *fill* frames bridged."""
    runs: list[list[int]] = []
    for index in np.flatnonzero(mask):
        index = int(index)
        if runs and index - runs[-1][1] <= fill:
            runs[-1][1] = index + 1
        else:
            runs.append([index, index + 1])
    return [(a, b) for a, b in runs]


def _correlation(a: np.ndarray, b: np.ndarray) -> float:
    energy = math.sqrt(float(np.dot(a, a)) * float(np.dot(b, b)))
    return float(np.dot(a, b)) / energy if energy > 1e-12 else 0.0


@dataclass(frozen=True)
class Stutter:
    #: Seconds into the audio heard.
    start: float
    end: float
    #: The chunk repeated, in seconds, and how many times it is heard in a row.
    period: float
    times: int
    correlation: float

    def overlaps(self, other: Stutter) -> bool:
        return self.start < other.end and other.start < self.end


def _repeat_lag(x: np.ndarray, at: int, coarse: int, hop: int) -> tuple[int, float]:
    """The lag in samples near *coarse* at which the waveform from *at* repeats best twice: ``(lag, correlation)``."""
    best, best_lag = -1.0, coarse
    for lag in range(max(1, coarse - hop), coarse + hop + 1):
        if at + 3 * lag > len(x):
            break
        first, second, third = x[at : at + lag], x[at + lag : at + 2 * lag], x[at + 2 * lag : at + 3 * lag]
        value = min(_correlation(first, second), _correlation(second, third))
        if value > best:
            best, best_lag = value, lag
    return best_lag, best


def find_stutters(
    samples: np.ndarray, rate: int = ANALYSIS_RATE, *, voiced: Sequence[tuple[float, float]] | None = None,
    levels: np.ndarray | None = None,
) -> list[Stutter]:
    """Every stutter of *samples*: the same 80-200 ms chunk heard :data:`STUTTER_TIMES` or more times in a row.

    *voiced* ``[(start, end)]`` keeps those inside a voiced region; the
    whole signal is looked at otherwise.
    """
    levels = band_levels(samples, rate) if levels is None else levels
    hop_s = HOP_SECONDS
    hop = round(hop_s * rate)
    low, high = (round(seconds / hop_s) for seconds in STUTTER_SECONDS)
    if len(levels) < STUTTER_TIMES * low:
        return []
    loud = _loud(levels)
    x = samples.astype(np.float64)
    # The frames that straddle a chunk's edge see two chunks: a run of repeats falls short by about one window.
    window = round(FRAME_SECONDS / hop_s)
    found: list[Stutter] = []
    for k in range(low, high + 1):
        same = np.abs(levels[:-k] - levels[k:]).mean(axis=1) < SAME_DB
        for a, b in _runs(same, fill=2):
            if b - a < (STUTTER_TIMES - 1) * k - window:
                continue
            times = 1 + (b - a + window) // k
            if float(levels[a : a + k].std(axis=0).mean()) < CHANGE_DB or float(loud[a : a + times * k].mean()) < LOUD_SHARE:
                continue
            start, end = a * hop_s, min((a + times * k) * hop_s, len(samples) / rate)
            if voiced is not None:
                inside = sum(max(0.0, min(end, e) - max(start, s)) for s, e in voiced)
                if inside < 0.8 * (end - start):
                    continue
            lag, correlation = _repeat_lag(x, a * hop, k * hop, hop)
            if correlation < WAVE_CORRELATION:
                continue
            found.append(Stutter(round(start, 3), round(end, 3), round(lag / rate, 4), int(times), round(correlation, 3)))
    # A repeat at one lag shows at its multiples too: the shortest period over a span stands for it.
    kept: list[Stutter] = []
    for stutter in sorted(found, key=lambda s: (s.period, -s.times)):
        if not any(stutter.overlaps(other) for other in kept):
            kept.append(stutter)
    return sorted(kept, key=lambda s: s.start)


def repeats_the_voice(stutter: Stutter, mix_levels: np.ndarray, voice_levels: np.ndarray) -> bool:
    """Whether a stutter heard in the mix repeats what the voice had said (a mix or mux fault), not what it says.

    The mix's second chunk (:func:`band_levels`) is compared with the
    voice's first and with its own time in the voice: a mix repeating its
    sound is nearer the first. A music loop under the voice follows the voice
    and is not one.
    """
    k = max(1, round(stutter.period / HOP_SECONDS))
    a = round(stutter.start / HOP_SECONDS)
    if a + 2 * k > min(len(mix_levels), len(voice_levels)):
        return False
    second = mix_levels[a + k : a + 2 * k]
    to_first = float(np.abs(second - voice_levels[a : a + k]).mean())
    to_own = float(np.abs(second - voice_levels[a + k : a + 2 * k]).mean())
    return to_first + MIX_MARGIN_DB < to_own


# ── Transcribing ──────────────────────────────────────────────────────────


def narration_windows(
    narration: Narration, voiced: Sequence[tuple[float, float]], total: float
) -> list[tuple[float, float]]:
    """Where *narration* is transcribed: the voice's energy inside its placed span (*voiced*, the stem's
    ``speech_timing.voiced_runs``, seconds), split on silences of :data:`SPLIT_SILENCE`, at most :data:`PART_SECONDS`
    each, padded by :data:`_PAD` within the span's own padding. None when its span holds no voice: nothing is said
    there, and a speech-to-text sent silence or music writes what it learnt to write there."""
    parts: list[list[float]] = []
    for start, end in voiced:
        start, end = max(start, narration.start), min(end, narration.end)
        if end <= start:
            continue
        if parts and start - parts[-1][1] < SPLIT_SILENCE and end - parts[-1][0] <= PART_SECONDS:
            parts[-1][1] = end
        else:
            parts.append([start, end])
    low, high = max(0.0, narration.start - _PAD), min(total, narration.end + _PAD)
    return [(max(low, a - _PAD), min(high, b + _PAD)) for a, b in parts]


def _wav(samples: np.ndarray, rate: int) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes((np.clip(samples, -1.0, 1.0) * 32767.0).astype("<i2").tobytes())
    return buffer.getvalue()


@dataclass(frozen=True)
class HeardWord:
    text: str
    start: float
    end: float


@dataclass(frozen=True)
class HeardWindow:
    """A window of a narration's span transcribed (:func:`narration_windows`): whose, where, the language the
    speech-to-text heard it in, the model that heard it, and its segments - their text and their words, on the MP4's
    clock."""

    scene_id: str
    start: float
    end: float
    language: str | None
    segments: tuple[tuple[str, tuple[HeardWord, ...]], ...]
    model: str | None = None

    @property
    def words(self) -> list[HeardWord]:
        return [word for _text, words in self.segments for word in words]


def _segments(result: Any, words: Sequence[HeardWord], offset: float) -> tuple[tuple[str, tuple[HeardWord, ...]], ...]:
    """*words* by the segment of *result* each falls in (its middle); one segment, the whole text, without segments."""
    segments = [s for s in getattr(result, "segments", None) or () if str(s.text).strip()]
    if not segments:
        return ((str(result.text or ""), tuple(words)),)
    grouped: list[list[HeardWord]] = [[] for _ in segments]
    for word in words:
        middle = (word.start + word.end) / 2 - offset
        index = max((i for i, s in enumerate(segments) if float(s.start) <= middle), default=0)
        grouped[index].append(word)
    return tuple((str(s.text), tuple(group)) for s, group in zip(segments, grouped, strict=True))


def _alone(samples: np.ndarray, narration: Narration, window: tuple[float, float], rate: int = ANALYSIS_RATE) -> np.ndarray:
    """The *window* of *samples* silent but where *narration* plays: it hears no other narration, however close
    the stem placed it."""
    first = round(window[0] * rate)
    chunk = samples[first : round(window[1] * rate)].copy()
    chunk[: max(0, round(narration.start * rate) - first)] = 0.0
    chunk[max(0, round(narration.end * rate) - first) :] = 0.0
    return chunk


async def transcribe_window(
    transcriber: Any, samples: np.ndarray, narration: Narration, window: tuple[float, float], rate: int = ANALYSIS_RATE
) -> HeardWindow | None:
    """The *window* of *narration*'s span in *samples* (the voice heard) transcribed, *narration* alone playing;
    ``None`` for a window too short to hear."""
    start, end = window
    chunk = _alone(samples, narration, window, rate)
    if len(chunk) < rate // 10:
        return None
    result = await transcriber.transcribe(_wav(chunk, rate), filename="voice.wav", words=True)
    words = [HeardWord(w.text, start + w.start, start + w.end) for w in result.words if w.text.strip()]
    if not words and result.text.strip():
        # No word times: the words spread evenly over the window.
        text = result.text.split()
        step = (end - start) / max(len(text), 1)
        words = [HeardWord(t, start + i * step, start + (i + 1) * step) for i, t in enumerate(text)]
    language = getattr(result, "language", None) or None
    return HeardWindow(narration.scene_id, start, end, language, _segments(result, words, start), getattr(result, "model", None))


@dataclass(frozen=True)
class OwnCheck:
    """What a narration's own transcript check (``voice_check``) found of the take placed: whether it passed, what it
    heard, and the language it heard it in. The render's check holds the final voice to it."""

    passed: bool
    transcript: str = ""
    language: str | None = None


@dataclass(frozen=True)
class Hallucination:
    """What the speech-to-text wrote where no narration speaks (:func:`kept_words`): dropped and counted, never graded."""

    scene_id: str
    at: float
    end: float
    text: str
    why: str

    def as_dict(self) -> dict:
        return {"scene_id": self.scene_id, "at": round(self.at, 3), "end": round(self.end, 3), "text": self.text[:160], "why": self.why}


def voice_over_language(
    own: Mapping[str, OwnCheck], windows: Sequence[HeardWindow], narrations: Sequence[Narration]
) -> str | None:
    """The language the voice-over is heard in: the one its narrations' own checks and the render's windows heard most
    of their words in (case aside); ``None`` when no hearing named one."""
    from app.services.video_studio.voice_check import normalized_tokens

    words: Counter[str] = Counter()
    for narration in narrations:
        check = own.get(narration.scene_id)
        if check is not None and check.language:
            words[check.language.casefold()] += max(1, len(normalized_tokens(narration.text)))
    for window in windows:
        if window.language:
            words[window.language.casefold()] += len(window.words)
    best = words.most_common(1)
    return best[0][0] if best and best[0][1] > 0 else None


def _said_from(words: Sequence[HeardWord], phrases: Sequence[str]) -> int | None:
    """The first of *words* from which they say one of *phrases* (``pronunciation_check.plain`` forms), to their end."""
    from app.services.video_studio.pronunciation_check import plain

    for first in range(len(words)):
        said = ""
        for word in words[first:]:
            said += plain(word.text)
            if any(said.startswith(phrase) for phrase in phrases):
                return first
            if not any(phrase.startswith(said) for phrase in phrases):
                break
    return None


def _spoken(window: HeardWindow, narration: Narration, expected: str | None) -> tuple[list[HeardWord], list[Hallucination]]:
    """The words of *window* said, and what its speech-to-text wrote where nobody speaks: the whole window when it was
    heard in another language than *expected* (the voice-over's); else, in each segment, from the first word saying what
    its model is known to write over silence or music (``transcription.known_hallucinations``) to the segment's end -
    unless *narration* writes those words itself."""
    from app.llm.transcription import known_hallucinations
    from app.services.video_studio.pronunciation_check import plain

    if window.language and expected and window.language.casefold() != expected.casefold():
        words = window.words
        if not words:
            return [], []
        text = " ".join(word.text for word in words)
        why = f"heard in {window.language}, the voice-over in {expected}"
        return [], [Hallucination(window.scene_id, words[0].start, words[-1].end, text, why)]
    written = plain(narration.text)
    phrases = [said for said in (plain(p) for p in known_hallucinations(window.model)) if said and said not in written]
    kept: list[HeardWord] = []
    dropped: list[Hallucination] = []
    for _text, words in window.segments:
        first = _said_from(words, phrases) if phrases else None
        kept.extend(words if first is None else words[:first])
        if first is not None:
            gone = words[first:]
            text = " ".join(word.text for word in gone)
            dropped.append(Hallucination(window.scene_id, gone[0].start, gone[-1].end, text, "what its speech-to-text writes where nobody speaks"))
    return kept, dropped


@dataclass
class KeptWords:
    """The render's transcript as graded (:func:`kept_words`): each narration's words, and what was left out."""

    #: ``scene id -> the words its own windows heard inside its span``, on the MP4's clock.
    heard: dict[str, list[HeardWord]]
    hallucinated: list[Hallucination] = field(default_factory=list)
    #: Words a window heard outside its narration's span (and its padding): scored against nothing.
    outside: int = 0
    #: The voice-over's language (:func:`voice_over_language`).
    language: str | None = None


def kept_words(windows: Sequence[HeardWindow], narrations: Sequence[Narration], own: Mapping[str, OwnCheck]) -> KeptWords:
    """What the render's *windows* heard of each narration: its words inside its own placed span (padded by
    :data:`_PAD`), never a hallucination (:func:`_spoken`) - a window is held to the language its narration's own check
    heard it in, else the voice-over's (:func:`voice_over_language`)."""
    by_scene = {narration.scene_id: narration for narration in narrations}
    kept = KeptWords({}, language=voice_over_language(own, windows, narrations))
    for window in windows:
        narration = by_scene[window.scene_id]
        check = own.get(narration.scene_id)
        words, dropped = _spoken(window, narration, (check.language if check is not None else None) or kept.language)
        kept.hallucinated.extend(dropped)
        inside = [w for w in words if narration.start - _PAD <= (w.start + w.end) / 2 <= narration.end + _PAD]
        kept.outside += len(words) - len(inside)
        kept.heard.setdefault(narration.scene_id, []).extend(inside)
    return kept


@dataclass(frozen=True)
class HeardTake:
    """A narration ``voice_check`` heard already: the take placed, the words heard in it (timed in it, seconds), and
    the digest of the very sound they were heard in (``voice_check.take_digest``)."""

    path: str
    words: Sequence[tuple[str, float, float]]
    digest: str | None = None


def plays_the_take(heard: np.ndarray, take: np.ndarray, start: float, rate: int = ANALYSIS_RATE) -> bool:
    """Whether *heard* - the voice as the viewer hears it - plays *take* unchanged from *start* (seconds).

    Compared on their band energies (:func:`band_levels`) within
    :data:`TAKE_LAG_SECONDS` of its place, the levels aligned (a gain is no
    change): every 20 ms of the take's speech within :data:`SAME_TAKE_DB`. A
    word the stitching, the resampling or the encoding broke - cut, repeated,
    garbled - jumps far past it; the encode alone stays well under. A take
    the heard voice does not hold (most of it past its end) is not played.
    """
    hop = round(HOP_SECONDS * rate)
    first = round(start * rate)
    shown = min(len(take), len(heard) - first)
    if shown < max(rate // 10, int(0.8 * len(take))):
        return False
    take = take[:shown]
    reach = round(TAKE_LAG_SECONDS * rate)
    low = max(0, first - reach)
    take_levels = band_levels(take, rate)
    window_levels = band_levels(heard[low : min(len(heard), first + shown + reach)], rate)
    speech = _loud(take_levels)
    if not len(take_levels) or not speech.any():
        return False
    at = round((first - low) / hop)
    lag = round(TAKE_LAG_SECONDS / HOP_SECONDS)
    kernel = np.ones(_TAKE_SMOOTH_FRAMES) / _TAKE_SMOOTH_FRAMES
    # Only the bands the take sounds in, frame by frame: noise where it is silent breaks no word.
    present = take_levels >= take_levels.max(axis=1, keepdims=True) - TAKE_BAND_RANGE_DB
    counted = np.maximum(present.sum(axis=1), 1)
    best = math.inf
    for offset in range(max(0, at - lag), at + lag + 1):
        part = window_levels[offset : offset + len(take_levels)]
        if len(part) < len(take_levels):
            break
        delta = part - take_levels
        gain = float(np.median(delta[speech][present[speech]]))
        spread = np.where(present, np.abs(delta - gain), 0.0).sum(axis=1) / counted
        best = min(best, float(np.convolve(spread, kernel, mode="same")[speech].max()))
    return best <= SAME_TAKE_DB


def _take_samples(path: str, rate: int = ANALYSIS_RATE) -> np.ndarray:
    from app.services.video_studio.stitch import read_segment, resample

    samples, source_rate = read_segment(path)
    return resample(samples, source_rate, rate)


def graded_narrations(
    spans: Mapping[str, tuple[float, float]], texts: Mapping[str, str], *, offset: float = 0.0, scene_id: str | None = None
) -> list[Narration]:
    """The narrations a render's audio check grades, in order, on its MP4's clock: each where the stem placed its take
    (*spans*, ``stitch.Stem.spans``, less *offset*), never its scene's bounds on screen.

    A scene preview (*scene_id*) grades its own narration alone, transcribed over its span as the mix places it:
    its transitions play its neighbours' narrations, which its MP4 keeps for viewing and its check never
    grades - their own previews and the whole render do.
    """
    return sorted(
        (
            Narration(scene, texts.get(scene, ""), start - offset, end - offset)
            for scene, (start, end) in spans.items()
            if scene_id is None or scene == scene_id
        ),
        key=lambda n: n.start,
    )


def reused_words(
    known: Mapping[str, HeardTake], narrations: Sequence[Narration], heard: np.ndarray
) -> tuple[dict[str, list[HeardWord]], list[str]]:
    """The words of each narration heard already whose take *heard* still plays, on the MP4's clock; and the scenes
    whose take it does not (changed since it was heard: transcribed again).

    The take placed must still be the very sound its words were heard in (its digest), and *heard* must play it.
    """
    from app.services.video_studio.voice_check import take_digest

    reused: dict[str, list[HeardWord]] = {}
    changed: list[str] = []
    for narration in narrations:
        take = known.get(narration.scene_id)
        if take is None or not take.words:
            continue
        try:
            plays = bool(take.digest) and take_digest(take.path) == take.digest
            plays = plays and plays_the_take(heard, _take_samples(take.path), narration.start)
        except Exception as exc:  # a take that cannot be read is transcribed from the voice heard instead
            logger.info("Narration %s not compared: %s", narration.scene_id, format_exception_message(exc))
            plays = False
        if plays:
            reused[narration.scene_id] = [HeardWord(w, narration.start + a, narration.start + b) for w, a, b in take.words]
        else:
            changed.append(narration.scene_id)
    return reused, changed


@dataclass(frozen=True)
class Narration:
    """One narration as the stem holds it: its scene, its text as written, and where it is."""

    scene_id: str
    text: str
    start: float
    end: float


def _words(heard: Sequence[Any]) -> list[tuple[str, float, float]]:
    return [w if isinstance(w, tuple) else (str(w.text), float(w.start), float(w.end)) for w in heard]


_SENTENCE = re.compile(r"(?<=[.!?\u2026])\s+")


def _edit_distance(a: Sequence[str], b: Sequence[str]) -> int:
    previous = list(range(len(b) + 1))
    for i, word in enumerate(a, 1):
        current = [i] + [0] * len(b)
        for j, other in enumerate(b, 1):
            current[j] = min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (word != other))
        previous = current
    return previous[-1]


def written_sentences(text: str | None) -> list[tuple[tuple[str, ...], str]]:
    """The sentences of a narration's *text* looked for twice - :data:`SENTENCE_WORDS` words or more -, each as its
    words are compared (``voice_check.normalized_tokens``) and as it is written."""
    from app.services.video_studio.voice_check import normalized_tokens

    found = []
    for sentence in _SENTENCE.split((text or "").strip()):
        words = tuple(normalized_tokens(sentence))
        if len(words) >= SENTENCE_WORDS:
            found.append((words, sentence.strip()))
    return found


def _heard_as(window: Sequence[str], words: Sequence[str]) -> int | None:
    """The word edit distance at which the words *window* are a hearing of the sentence *words*; ``None`` when they are
    not one: most of its words among them, and no more than :data:`SENTENCE_DISTANCE` of its length apart."""
    wanted = set(words)
    if len(wanted & set(window)) < 0.6 * len(wanted):
        return None
    distance = _edit_distance(window, words)
    return distance if distance <= SENTENCE_DISTANCE * len(words) else None


@dataclass(frozen=True)
class SharedSentence:
    """A sentence two narrations or more write (:func:`shared_sentences`): as first written, and their scenes."""

    sentence: str
    scene_ids: tuple[str, ...]


def shared_sentences(narrations: Sequence[tuple[str, str]]) -> list[SharedSentence]:
    """Each sentence of *narrations* - ``(scene id, text)``, in order - another scene's narration writes too:
    the same words once normalised, or close enough that one is a hearing of the other (:func:`_heard_as`).

    A scene preview hears its narration alone, so each passes; the whole render hears that sentence more times than
    either narration writes it - a ``repeated_sentence`` (:func:`repeated_sentences`) found only after every scene was
    drawn. What a save refuses before any preview.
    """
    written = [(scene_id, words, sentence) for scene_id, text in narrations for words, sentence in written_sentences(text)]
    shared: list[SharedSentence] = []
    taken: set[int] = set()
    for index, (scene_id, words, sentence) in enumerate(written):
        if index in taken:
            continue
        scenes = [scene_id]
        for other, (other_scene, other_words, _said) in enumerate(written[index + 1 :], start=index + 1):
            if other in taken or other_scene in scenes:
                continue
            if _heard_as(other_words, words) is not None or _heard_as(words, other_words) is not None:
                scenes.append(other_scene)
                taken.add(other)
        if len(scenes) > 1:
            shared.append(SharedSentence(sentence, tuple(scenes)))
    return shared


def _narration_at(narrations: Sequence[Narration], at: float) -> Narration | None:
    """The narration whose placed span holds *at*; else the nearest one (a word heard a little past its span)."""
    if not narrations:
        return None
    for narration in narrations:
        if narration.start <= at < narration.end:
            return narration
    return min(narrations, key=lambda n: min(abs(at - n.start), abs(at - n.end)))


def _closest_in(window: Sequence[str], tokens: Sequence[str]) -> int | None:
    """The smallest word edit distance between *window* and any run of *tokens* as long as it, one word either way."""
    m = len(window)
    best: int | None = None
    for length in (m - 1, m, m + 1):
        if length <= 0:
            continue
        for j in range(0, max(1, len(tokens) - length + 1)):
            distance = _edit_distance(window, tokens[j : j + length])
            if best is None or distance < best:
                best = distance
    return best


def _says_its_own_text(window: Sequence[str], distance: int, narration: Narration) -> bool:
    """Whether *window*, heard in *narration* at *distance* from a sentence another scene writes, is that narration
    saying its own text: its own written words match the words heard at least as closely - the same subject and object
    with another verb, say - so they are its text heard right, not a repeat.
    """
    from app.services.video_studio.voice_check import normalized_tokens

    closest = _closest_in(window, normalized_tokens(narration.text or ""))
    return closest is not None and closest <= distance


def repeated_sentences(heard: Sequence[tuple[str, float, float]], narrations: Sequence[Narration]) -> list[Finding]:
    """A finding for each sentence of the narration heard more times than it is written, at its extra hearing.

    Each hearing is placed in the narration it falls in (:func:`_narration_at`). One in a scene that does not write the
    sentence and whose own text explains the words heard at least as closely (:func:`_says_its_own_text`) is that scene
    saying its own narration, never a repeat. The finding names the scene of the extra hearing and quotes what was heard
    there; a scene that does not write the sentence is said to have spoken it instead of its own text.
    """
    from collections import Counter

    written: Counter[tuple[str, ...]] = Counter()
    where: dict[tuple[str, ...], tuple[str, str]] = {}
    writers: dict[tuple[str, ...], set[str]] = {}
    for narration in narrations:
        for words, sentence in written_sentences(narration.text):
            written[words] += 1
            where.setdefault(words, (narration.scene_id, sentence))
            writers.setdefault(words, set()).add(narration.scene_id)
    # A scene writing a near-identical sentence (a hearing of it, the pair :func:`shared_sentences` refuses at save)
    # writes it too: its own text never explains a hearing of it away.
    for words, scenes_writing in writers.items():
        for narration in narrations:
            if any(
                _heard_as(own, words) is not None or _heard_as(words, own) is not None
                for own, _sentence in written_sentences(narration.text)
            ):
                scenes_writing.add(narration.scene_id)
    tokens = [word for word, _s, _e in heard]
    findings: list[Finding] = []
    for words, count in written.items():
        m = len(words)
        hits: list[tuple[int, int]] = []
        for i in range(0, max(0, len(tokens) - m + 1)):
            window = tokens[i : i + m]
            distance = _heard_as(window, words)
            if distance is None:
                continue
            home = _narration_at(narrations, heard[i][1])
            if (
                home is not None
                and home.scene_id not in writers[words]
                and _says_its_own_text(window, distance, home)
            ):
                continue
            hits.append((distance, i))
        taken: list[int] = []
        for _distance, i in sorted(hits):
            if all(abs(i - j) >= m for j in taken):
                taken.append(i)
        if len(taken) <= count:
            continue
        taken.sort()
        times = [heard[i][1] for i in taken]
        scenes = [(n.scene_id if (n := _narration_at(narrations, t)) is not None else None) for t in times]
        writer, sentence = where[words]
        extra = taken[count]
        scene_id = scenes[count] or writer
        said = " ".join(tokens[extra : extra + m])
        drifted = scene_id not in writers[words]
        if not drifted:
            where_heard = f'its extra hearing at {clock(times[count])} is in scene `{scene_id}`, heard "{said}"'
        else:
            where_heard = (
                f'its extra hearing at {clock(times[count])} is in scene `{scene_id}`, whose take said "{said}" '
                f"instead of its own text (the sentence is scene `{writer}`'s)"
            )
        findings.append(
            Finding(
                REPEATED_SENTENCE,
                times[count],
                heard[extra + m - 1][2],
                scene_id,
                f'"{sentence[:160]}" is heard {len(taken)} times (at {", ".join(clock(t) for t in times[:5])}), '
                f"written {count}: {where_heard}",
                FAIL,
                {
                    "times": [round(t, 3) for t in times],
                    "scenes": scenes,
                    "heard": said,
                    "written_in": writer,
                    # Its scene does not write the sentence: that scene's take said another's (:func:`drifted_takes`).
                    "drifted": drifted,
                },
            )
        )
    return findings


def drifted_takes(findings: Sequence[Any]) -> list[str]:
    """The scenes whose take said another scene's sentence instead of their own text: each blocking
    ``repeated_sentence`` finding heard in a scene that does not write the sentence (:func:`repeated_sentences`), in
    the order heard, each once. A sentence written once and said twice in its own scene is a real repeat, never one."""
    scenes: list[str] = []
    for finding in findings:
        data = getattr(finding, "data", None) or {}
        scene = getattr(finding, "scene_id", None)
        if (
            getattr(finding, "kind", None) == REPEATED_SENTENCE
            and getattr(finding, "severity", None) == FAIL
            and data.get("drifted")
            and scene
            and scene not in scenes
        ):
            scenes.append(scene)
    return scenes


@dataclass
class RenderHearing:
    """The voice-over heard narration by narration (:func:`hear_narrations`): each one's words aligned on its own text,
    and the lexicon's terms as heard, each owned by its narration's index."""

    hearings: list[Any]
    terms: list[Any]
    #: The terms their narration's words did not say, heard within :data:`TERM_REACH` of their planned time.
    found_nearby: int = 0


def _sorted(words: Sequence[Any]) -> list[tuple[str, float, float]]:
    return sorted(_words(words), key=lambda w: (w[1], w[2]))


def hear_narrations(
    heard: Mapping[str, Sequence[Any]],
    narrations: Sequence[Narration],
    lexicon: Any = None,
    *,
    pauses: Sequence[tuple[float, float]] | None = None,
    nuclei: Sequence[float] | None = None,
) -> RenderHearing:
    """Each narration's words (*heard*: scene id -> the words its own span heard, timed) aligned on its text alone, with
    the very normalisation of its own check (``voice_check.hearing_of``): no word is graded against another narration.

    *pauses*: the silences of the voice heard, on the words' clock: a term holding a long one fails. *nuclei*: its
    syllables (``pronunciation_check.nuclei_in``): a term read as words said letter by letter fails.
    """
    from app.services.video_studio.voice_check import hearing_of

    hearings = []
    for narration in narrations:
        words = _sorted(heard.get(narration.scene_id, ()))
        transcript = " ".join(word for word, _s, _e in words)
        hearings.append(hearing_of(narration.text, transcript, lexicon=lexicon, words=words, pauses=pauses, nuclei=nuclei))
    terms, found = heard_terms(hearings, narrations, heard, lexicon, pauses=pauses, nuclei=nuclei)
    return RenderHearing(hearings, terms, found)


def _planned_times(narration: Narration, lexicon: Any) -> list[float]:
    """When each lexicon term of *narration* is planned to be said: its word paced over the placed span."""
    from app.services.video_studio import pronunciation_check, speech_timing

    paced = speech_timing.paced_words(narration.text, narration.start, narration.end - narration.start)
    if not paced:
        return [narration.start for _ in pronunciation_check.term_offsets(narration.text, lexicon)]
    return [
        paced[min(len(speech_timing.tokens(narration.text[:offset])), len(paced) - 1)].start
        for offset in pronunciation_check.term_offsets(narration.text, lexicon)
    ]


def heard_terms(
    hearings: Sequence[Any],
    narrations: Sequence[Narration],
    heard: Mapping[str, Sequence[Any]],
    lexicon: Any = None,
    *,
    pauses: Sequence[tuple[float, float]] | None = None,
    nuclei: Sequence[float] | None = None,
) -> tuple[list[Any], int]:
    """Each lexicon term of *narrations* as heard (``pronunciation_check.TermHearing``, owned by its narration's index)
    and how many were found off their place.

    A term its narration's words do not say - missing, or heard as other words; never one broken by a pause or read
    letter by letter - is looked
    for in the whole render's transcript (*heard*) within :data:`TERM_REACH` of its planned time, among the words no
    narration heard as its own (``pronunciation_check.found_reading``): heard there, it is held to its fluency there and
    reported where it was heard, never "not heard".
    """
    from app.services.video_studio import pronunciation_check

    everything = _sorted([word for words in heard.values() for word in words])
    #: The stretches each narration heard as its own words: never a reading of another narration's term.
    taken = [
        (token.start, token.end)
        for hearing in hearings
        for j, token in enumerate(hearing.heard)
        if (ref := hearing.alignment.hypothesis_to_reference[j]) is not None and hearing.alignment.reference_heard[ref]
    ]

    def free_runs(near: float) -> list[list[tuple[str, float, float]]]:
        runs: list[list[tuple[str, float, float]]] = [[]]
        for word in everything:
            if abs(word[1] - near) <= TERM_REACH and not any(a <= word[1] and word[2] <= b for a, b in taken):
                runs[-1].append(word)
            elif runs[-1]:
                runs.append([])
        return [run for run in runs if run]

    terms: list[Any] = []
    found = 0
    for index, (narration, hearing) in enumerate(zip(narrations, hearings, strict=True)):
        entries = [token.entry for token in hearing.reference if token.entry is not None]
        for term, entry, planned in zip(hearing.terms, entries, _planned_times(narration, lexicon), strict=True):
            if not term.passed and term.pause is None and term.syllables is None:
                nearby = [
                    reading
                    for run in free_runs(planned)
                    if (reading := pronunciation_check.found_reading(entry, run, planned, pauses, nuclei)) is not None
                ]
                if nearby:
                    term, end = min(nearby, key=lambda reading: abs(float(reading[0].at or 0.0) - planned))
                    taken.append((float(term.at or 0.0), end))
                    found += 1
            terms.append(replace(term, owner=index))
    return terms, found


def transcript_findings(
    heard: Mapping[str, Sequence[Any]],
    narrations: Sequence[Narration],
    lexicon: Any = None,
    *,
    hearing: RenderHearing | None = None,
    own: Mapping[str, OwnCheck] | None = None,
) -> tuple[float, list[dict], list[Finding]]:
    """The voice-over heard against the narration of every scene: ``(WER, each scene's, findings)``.

    *heard* are each narration's words, heard on its own span with their times;
    the lexicon's terms count as one word whichever accepted reading was heard
    (:func:`hear_narrations`, or *hearing* when it was already). A scene's word
    error rate is at most 100 %, the voice-over's its scenes' errors over
    their words. Over ``MAX_WER`` a finding names each scene heard wrong, at
    its narration's time, with what was heard there - an advisory naming both
    transcripts for a narration whose own check passed (*own*): one check
    decides a narration's words, its own.
    """
    from app.services.video_studio.voice_check import MAX_WER

    hearing = hearing if hearing is not None else hear_narrations(heard, narrations, lexicon)
    own = own or {}
    scenes: list[dict] = []
    errors = words = 0
    for narration, heard_as in zip(narrations, hearing.hearings, strict=True):
        alignment = heard_as.alignment
        rate = min(1.0, alignment.wer)
        errors += min(alignment.errors, alignment.reference_words)
        words += alignment.reference_words
        scenes.append({"scene_id": narration.scene_id, "wer": round(rate, 4), "at": round(narration.start, 3), "end": round(narration.end, 3)})
    wer = round(errors / words, 4) if words else 0.0
    findings: list[Finding] = []
    if wer > MAX_WER:
        for narration, scene in zip(narrations, scenes, strict=True):
            if scene["wer"] <= MAX_WER:
                continue
            said = " ".join(word for word, _s, _e in _sorted(heard.get(narration.scene_id, ())))[:200]
            message = (
                f"{scene['wer']:.0%} of its narration's words are heard wrong (the whole voice-over {wer:.0%}, at most "
                f"{MAX_WER:.0%}): heard \"{said}\""
            )
            check = own.get(narration.scene_id)
            if check is not None and check.passed:
                transcript = check.transcript[:200]
                message = (
                    f"{message} - its own check heard \"{transcript}\" and passed it, which decides its words: an "
                    "advisory, listen to it"
                )
                data = {"wer": scene["wer"], "advisory": True, "heard": said, "own_transcript": transcript}
                findings.append(Finding(TRANSCRIPT, narration.start, narration.end, narration.scene_id, message, WARN, data))
                continue
            findings.append(Finding(TRANSCRIPT, narration.start, narration.end, narration.scene_id, message, FAIL, {"wer": scene["wer"]}))
    return wer, scenes, findings


def pronunciation_findings(hearing: Any, narrations: Sequence[Narration]) -> tuple[dict, list[Finding]]:
    """The lexicon's terms in the voice heard: the ``pronunciation`` section and a finding per term heard wrong.

    The section has, per term, how many times the narration writes it, how
    many of those were heard as the lexicon accepts, and the forms heard. A
    term accepted in other words than its written form is a warning,
    once per form heard, at its first time: the gate passes, and a reviewer
    reads ``heard 'Philigran'`` without opening the audio.
    """
    if not hearing.terms:
        return {"status": SKIPPED, "reason": "the narration says no lexicon term"}, []
    terms: dict[str, dict] = {}
    findings: list[Finding] = []
    #: ``(term, form heard) -> [first hearing, its time, its scene, times]`` of each term heard otherwise than written.
    otherwise: dict[tuple[str, str], list] = {}
    for term in hearing.terms:
        entry = terms.setdefault(term.term, {"term": term.term, "occurrences": 0, "passed": 0, "heard": []})
        entry["occurrences"] += 1
        entry["passed"] += 1 if term.passed else 0
        if term.heard and term.heard not in entry["heard"] and len(entry["heard"]) < 5:
            entry["heard"].append(term.heard[:80])
        if term.passed and not term.heard_otherwise:
            continue
        narration = narrations[term.owner] if 0 <= term.owner < len(narrations) else None
        at = term.at if term.at is not None else (narration.start if narration is not None else 0.0)
        scene = narration.scene_id if narration is not None else None
        if term.passed:
            otherwise.setdefault((term.term, " ".join(term.heard.split()).casefold()), [term, at, scene, 0])[3] += 1
            continue
        data = {"term": term.term, "heard": term.heard[:120]}
        if term.syllables is not None:
            data["syllables"] = {"heard": term.syllables, "reading": term.reading_syllables}
        findings.append(Finding(PRONUNCIATION, at, None, scene, term.message(), FAIL, data))
    for term, at, scene, times in otherwise.values():
        message = term.notice() + (f" ({times} times)" if times > 1 else "")
        data = {"term": term.term, "heard": term.heard[:120], "reading": term.reading, "times": times}
        findings.append(Finding(PRONUNCIATION, at, None, scene, message, WARN, data))
    status = FAILED if any(f.severity == FAIL for f in findings) else PASSED
    return {"status": status, "terms": list(terms.values())}, findings


#: The forms a term's failed takes keep, per scene.
_FAILED_HEARD_KEPT = 3


def with_attempts(section: dict, takes: Sequence[tuple[str, Sequence[dict]]]) -> dict:
    """The ``pronunciation`` section with every take its terms failed in, not only the takes the render plays.

    *takes* ``(scene id, terms heard wrong)``: each take of a narration that
    the final voice never hears (``voice_check.SpokenSegment.unplayed_failures``:
    replaced by the take that passed, or none placed) and the lexicon terms it
    heard wrong. A term's ``occurrences`` and ``passed`` stay the takes kept;
    its ``attempts_failed`` says, per scene, how many takes failed on it and
    what they heard - so a term a take failed on never reads as 100 % because
    the take kept passed. A term only those takes said is added, never heard
    in the takes kept.
    """
    if not takes:
        return section
    terms = [dict(term) for term in section.get("terms") or [] if isinstance(term, dict)]
    by_term = {str(term.get("term")): term for term in terms}
    for scene_id, wrong in takes:
        for hearing in {str(h.get("term") or ""): h for h in wrong if h.get("term")}.values():
            name = str(hearing["term"])
            if (entry := by_term.get(name)) is None:
                entry = by_term[name] = {"term": name, "occurrences": 0, "passed": 0, "heard": []}
                terms.append(entry)
            failed = entry.setdefault("attempts_failed", [])
            if (where := next((item for item in failed if item["scene_id"] == scene_id), None)) is None:
                where = {"scene_id": scene_id, "times": 0, "heard": []}
                failed.append(where)
            where["times"] += 1
            heard = str(hearing.get("heard") or "")[:80]
            if heard and heard not in where["heard"] and len(where["heard"]) < _FAILED_HEARD_KEPT:
                where["heard"].append(heard)
    return {**section, "terms": terms}


# ── The check ─────────────────────────────────────────────────────────────


@dataclass
class AudioCheck:
    status: str
    findings: list[Finding] = field(default_factory=list)
    summary: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    #: The lexicon's terms in the voice heard (:func:`pronunciation_findings`).
    pronunciation: dict = field(default_factory=dict)

    def as_summary(self) -> dict:
        return {"status": self.status, **self.summary}


def _from_f32(data: bytes) -> np.ndarray:
    return np.frombuffer(data[: len(data) - len(data) % 4], "<f4").astype(np.float32)


async def _encoded_voice(
    stem_path: str, workdir: str, *, bitrate: str | None, span: tuple[float, float] | None
) -> tuple[np.ndarray | None, str | None]:
    """The voice stem as the final mix encodes it, demuxed and decoded; ``(None, why)`` when it cannot be."""
    from app.services.video_studio import encoder

    encoded = str(Path(workdir) / "voice-encoded.m4a")
    try:
        await encoder.encode_voice_like_mix(stem_path, encoded, bitrate=bitrate or "256k", span=span)
        return _from_f32(await encoder.decode_mono(encoded, rate=ANALYSIS_RATE)), None
    except Exception as exc:  # heard before its encoding rather than not heard
        return None, f"The voice-over was heard before its encoding ({format_exception_message(exc)[:160]})."


async def check_audio(
    *,
    final_path: str,
    stem_path: str | None,
    narrations: Sequence[Narration],
    transcriber: Any,
    unchecked_reason: str | None,
    scene_of: Callable[[float], str | None],
    workdir: str,
    span: tuple[float, float] | None = None,
    lexicon: Any = None,
    bitrate: str | None = None,
    known: Mapping[str, HeardTake] | None = None,
    own: Mapping[str, OwnCheck] | None = None,
    heard: Callable[[int, int], Awaitable[None]] | None = None,
) -> AudioCheck:
    """The rendered sound heard back: the final AAC demuxed and decoded, the voice stem transcribed.

    *span* ``(start, seconds)`` is the part of the stem the MP4 plays (a
    scene preview); every time found is in the MP4's own seconds.
    *narrations* are placed in the MP4's seconds too - those graded
    (:func:`graded_narrations`), each transcribed on its own span where the
    stem's voice has energy, alone there (:func:`narration_windows`).
    The stem is heard through the final mix's encode at *bitrate*, and its
    *lexicon* terms checked (``pronunciation``). *known* are the narrations
    heard already (scene id -> :class:`HeardTake`): one the encoded voice
    still plays is read from its own words, never transcribed again
    (:func:`reused_words`). *own* is what each narration's own check found
    (scene id -> :class:`OwnCheck`): the language its windows are held to, and
    a scene heard wrong whose own check passed is an advisory.
    *heard* ``(narrations heard, narrations)`` is told before the first
    window is transcribed and after each narration: the render's
    progress through its check. What it raises stops the check.
    """
    from app.services.video_studio import encoder, pronunciation_check
    from app.services.video_studio.speech_timing import voiced_runs
    from app.services.video_studio.voice_check import timed_tokens

    check = AudioCheck(PASSED)
    aac = Path(workdir) / "final-audio.m4a"
    info = await encoder.demux_audio(final_path, str(aac))
    mix = _from_f32(await encoder.decode_mono(str(aac), rate=ANALYSIS_RATE))
    check.summary["final_audio"] = {"seconds": round(info.duration, 3), "has_audio": info.has_audio}
    if stem_path is None:
        check.status = SKIPPED
        check.summary["reason"] = "nothing is narrated"
        check.pronunciation = {"status": SKIPPED, "reason": "nothing is narrated"}
        return check
    voice = _from_f32(await encoder.decode_mono(stem_path, rate=ANALYSIS_RATE))
    if span is not None:
        voice = voice[round(span[0] * ANALYSIS_RATE) : round((span[0] + span[1]) * ANALYSIS_RATE)]
    voiced = voiced_runs(voice, ANALYSIS_RATE)
    voice_levels, mix_levels = band_levels(voice), band_levels(mix)
    stem_stutters = find_stutters(voice, voiced=voiced, levels=voice_levels)
    mix_stutters = [
        s
        for s in find_stutters(mix, voiced=voiced, levels=mix_levels)
        if not any(s.overlaps(v) for v in stem_stutters) and repeats_the_voice(s, mix_levels, voice_levels)
    ]
    for source, stutters in (("voice", stem_stutters), ("mix", mix_stutters)):
        for s in stutters:
            check.findings.append(
                Finding(
                    STUTTER,
                    s.start,
                    s.end,
                    scene_of(s.start),
                    f"the same {s.period * 1000:.0f} ms of sound is heard {s.times} times in a row "
                    + ("in the narration" if source == "voice" else "in the mix, not in the narration"),
                    FAIL,
                    {"source": source, "period_ms": round(s.period * 1000, 1), "times": s.times},
                )
            )
    check.summary["stutters"] = len(stem_stutters) + len(mix_stutters)
    if transcriber is None:
        check.notes.append(f"The voice-over was not transcribed: {unchecked_reason or 'no speech-to-text'}.")
        check.summary["transcribed"] = False
        check.pronunciation = {"status": SKIPPED, "reason": unchecked_reason or "no speech-to-text"}
    else:
        encoded, why = await _encoded_voice(stem_path, workdir, bitrate=bitrate, span=span)
        if why:
            check.notes.append(why)
        heard_voice = encoded if encoded is not None else voice
        own = own or {}
        reused, changed = reused_words(known or {}, narrations, heard_voice)
        settled = [n for n in narrations if n.scene_id in reused]
        pending = [n for n in narrations if n.scene_id not in reused]
        total = len(voice) / ANALYSIS_RATE
        windows = {n.scene_id: narration_windows(n, voiced, total) for n in pending}
        # A narration whose span holds no voice is missing from the render: its own check vouches for no word there.
        unvoiced = [n.scene_id for n in pending if not windows[n.scene_id]]
        told = -1

        async def tell(done: int) -> None:
            nonlocal told
            if heard is not None and done != told:
                told = done
                await heard(done, len(narrations))

        transcribed: list[HeardWindow] = []
        failed: Exception | None = None
        await tell(len(settled))
        for index, narration in enumerate(pending):
            for window in windows[narration.scene_id]:
                try:
                    got = await transcribe_window(transcriber, heard_voice, narration, window)
                except Exception as exc:  # the speech-to-text failing is not the render failing
                    failed = exc
                    break
                if got is not None:
                    transcribed.append(got)
            if failed is not None:
                break
            await tell(len(settled) + index + 1)
        if failed is not None:
            check.notes.append(f"The voice-over could not be transcribed ({format_exception_message(failed)[:200]}).")
            check.summary["transcribed"] = False
            check.pronunciation = {"status": SKIPPED, "reason": "the voice-over could not be transcribed"}
        else:
            await tell(len(narrations))
            kept = kept_words(transcribed, narrations, own)
            for scene, words in reused.items():
                kept.heard.setdefault(scene, []).extend(words)
            hearing = hear_narrations(
                kept.heard,
                narrations,
                lexicon,
                pauses=pronunciation_check.pauses_in(heard_voice, ANALYSIS_RATE),
                nuclei=pronunciation_check.nuclei_in(heard_voice, ANALYSIS_RATE),
            )
            vouched = {scene: own_check for scene, own_check in own.items() if scene not in unvoiced}
            wer, scenes, findings = transcript_findings(kept.heard, narrations, hearing=hearing, own=vouched)
            check.pronunciation, brand = pronunciation_findings(hearing, narrations)
            check.findings.extend(findings)
            check.findings.extend(brand)
            every_word = sorted((w for words in kept.heard.values() for w in words), key=lambda w: (w.start, w.end))
            check.findings.extend(repeated_sentences(timed_tokens(every_word), narrations))
            facts: dict[str, Any] = {}
            if kept.hallucinated:
                facts["hallucinated"] = {
                    "segments": len(kept.hallucinated),
                    "words": sum(len(h.text.split()) for h in kept.hallucinated),
                    "dropped": [h.as_dict() for h in kept.hallucinated[:_LISTED_HALLUCINATIONS]],
                }
            if kept.outside:
                facts["outside_narration_words"] = kept.outside
            if unvoiced:
                facts["unvoiced"] = unvoiced
            if hearing.found_nearby:
                facts["terms_found_nearby"] = hearing.found_nearby
            check.summary.update(
                {
                    "transcribed": True,
                    "heard": "encoded" if encoded is not None else "stem",
                    "wer": wer,
                    "scenes": scenes,
                    "model": getattr(transcriber, "model", None),
                    "provider": getattr(transcriber, "provider", None),
                    **({"language": kept.language} if kept.language else {}),
                    # The narrations read from their own transcript, the windows of their spans transcribed again,
                    # and the narrations heard already whose take the voice no longer plays as it was heard.
                    "reused_transcripts": len(reused),
                    "transcribed_parts": len(transcribed),
                    **({"changed_since_heard": changed} if changed else {}),
                    # What was heard and never graded: written where nobody speaks, outside every narration's span.
                    **facts,
                }
            )
    if any(f.severity == FAIL for f in check.findings):
        check.status = FAILED
    return check
