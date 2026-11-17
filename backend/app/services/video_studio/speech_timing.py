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

"""When each word of a narration is said: measured on its voice, else paced.

No speech model the platform speaks with returns word timestamps, so they are
read from the voice itself. The narration as the mix takes it
(``encoder.condition_voice``: its leading and trailing silence trimmed) is
reduced to its loudness every :data:`FRAME_SECONDS`; where it stays
:data:`SILENCE_DB` under the line's loud level for :data:`MIN_PAUSE` or more,
nobody speaks. The words are laid over the time someone speaks, each taking
its share of letters, and the text's clause breaks (``.``, ``,``, ``;`` ...)
are put on the pauses that best match where they fall, so a word late in a
long line is not pushed off by every pause before it.

A narration with no audio yet is paced over its planned span the same way,
with no pause to align on (:func:`paced_words`). Nothing here reads what the
words mean: a figure matches its steps' labels against them
(``figures.reveal_times``).
"""

from __future__ import annotations

import asyncio
import itertools
import re
import struct
from dataclasses import dataclass
from pathlib import Path

import numpy as np

#: The loudness is read every this many seconds.
FRAME_SECONDS = 0.01
#: Quieter than the line's loud level by this much: nobody speaks.
SILENCE_DB = 32.0
#: A silence shorter than this is inside a word or between two words said together.
MIN_PAUSE = 0.12
#: A voiced stretch shorter than this is a click, not speech.
_MIN_VOICED = 0.03
#: How a clause break and a pause are paired (:func:`_align_breaks`): a pairing costs the square of how far
#: apart they fall (shares of the line) times this; a break left without a pause, and a pause holding no
#: break (per second of it), cost these. A voice pauses at its punctuation: pairing wins unless far off.
_MISMATCH_WEIGHT = 4.0
_UNMATCHED_BREAK = 0.15
_UNMATCHED_PAUSE = 0.3

_TOKEN = re.compile(r"\d+(?:[.,:]\d+)*%?|[^\W\d_][\w'\u2019-]*|[.,;:!?\u2026\u2014\u2013]", re.UNICODE)
_BREAK = frozenset(".,;:!?\u2026\u2014\u2013")


@dataclass(frozen=True)
class SpokenWord:
    """One word of a narration and when it is said, in seconds on the scene's clock."""

    start: float
    end: float
    #: Lower-cased, as :func:`tokens` reads it.
    text: str


def tokens(text: str) -> list[str]:
    """The words of *text*, lower-cased, in order: what a label is matched on."""
    return [word for word, _breaks in _words(text)]


def _words(text: str) -> list[tuple[str, bool]]:
    """The words of *text*, lower-cased, each with whether a clause ends right after it."""
    out: list[tuple[str, bool]] = []
    for match in _TOKEN.finditer(text or ""):
        token = match.group(0)
        if token in _BREAK:
            if out:
                out[-1] = (out[-1][0], True)
            continue
        out.append((token.lower(), False))
    return out


def _weight(word: str) -> float:
    """A word's share of the time it is said: its letters, a number spelled out longer, a beat to start it."""
    digits = sum(ch.isdigit() for ch in word)
    return 1.0 + (len(word) - digits) + 2.5 * digits


def paced_words(text: str, start: float, seconds: float) -> list[SpokenWord]:
    """*text* said at an even pace from *start* for *seconds*: each word its share of letters, a beat at each break."""
    words = _words(text)
    if not words:
        return []
    weights = [_weight(word) + (2.0 if breaks else 0.0) for word, breaks in words]
    total = sum(weights)
    out: list[SpokenWord] = []
    at = 0.0
    for (word, breaks), weight in zip(words, weights, strict=True):
        spoken = weight - (2.0 if breaks else 0.0)
        out.append(SpokenWord(start + seconds * at / total, start + seconds * (at + spoken) / total, word))
        at += weight
    return out


def voiced_runs(samples: np.ndarray, rate: int) -> list[tuple[float, float]]:
    """Where someone speaks in *samples* (mono): ``[(start, end)]`` in seconds, a pause at least :data:`MIN_PAUSE` apart."""
    hop = max(1, round(rate * FRAME_SECONDS))
    count = len(samples) // hop
    if count == 0:
        return []
    frames = samples[: count * hop].astype(np.float64).reshape(count, hop)
    level = 20.0 * np.log10(np.sqrt(np.mean(frames * frames, axis=1)) + 1e-9)
    voiced = level > float(np.percentile(level, 95)) - SILENCE_DB
    runs: list[list[float]] = []
    index = 0
    while index < count:
        if not voiced[index]:
            index += 1
            continue
        end = index
        while end < count and voiced[end]:
            end += 1
        start_s, end_s = index * FRAME_SECONDS, end * FRAME_SECONDS
        if runs and start_s - runs[-1][1] < MIN_PAUSE:
            runs[-1][1] = end_s
        else:
            runs.append([start_s, end_s])
        index = end
    return [(a, b) for a, b in runs if b - a >= _MIN_VOICED]


def _align_breaks(breaks: list[float], pauses: list[tuple[float, float]]) -> dict[int, int]:
    """Which pause each clause break falls on: ``{break: pause}``, in order, both as shares of the line.

    *breaks* are where the text's clause breaks fall (shares of its letters);
    *pauses* each pause's place (share of the voiced time before it) and its
    length. A break may stay without a pause, a pause without a break (a
    breath); the alignment that costs least wins (:data:`_MISMATCH_WEIGHT`).
    """
    m, n = len(breaks), len(pauses)
    if not m or not n:
        return {}
    inf = float("inf")
    cost = [[inf] * (n + 1) for _ in range(m + 1)]
    move: list[list[str]] = [[""] * (n + 1) for _ in range(m + 1)]
    cost[0][0] = 0.0
    for i in range(m + 1):
        for j in range(n + 1):
            here = cost[i][j]
            if here == inf:
                continue
            if i < m and here + _UNMATCHED_BREAK < cost[i + 1][j]:
                cost[i + 1][j], move[i + 1][j] = here + _UNMATCHED_BREAK, "break"
            if j < n and here + _UNMATCHED_PAUSE * pauses[j][1] < cost[i][j + 1]:
                cost[i][j + 1], move[i][j + 1] = here + _UNMATCHED_PAUSE * pauses[j][1], "pause"
            if i < m and j < n:
                both = here + _MISMATCH_WEIGHT * (breaks[i] - pauses[j][0]) ** 2
                if both < cost[i + 1][j + 1]:
                    cost[i + 1][j + 1], move[i + 1][j + 1] = both, "both"
    matched: dict[int, int] = {}
    i, j = m, n
    while i or j:
        step = move[i][j]
        if step == "both":
            matched[i - 1] = j - 1
            i, j = i - 1, j - 1
        elif step == "break":
            i -= 1
        else:
            j -= 1
    return matched


def measured_words(text: str, runs: list[tuple[float, float]], start: float = 0.0) -> list[SpokenWord]:
    """*text* laid over the voiced *runs* of its audio (seconds into it), its clause breaks on the pauses; times + *start*."""
    words = _words(text)
    if not words or not runs:
        return []
    lengths = [b - a for a, b in runs]
    voiced = sum(lengths)
    before = np.concatenate([[0.0], np.cumsum(lengths)])

    def time_at(position: float) -> float:
        """The moment *position* seconds of speech have been said, a pause's end for a position on it."""
        position = min(max(position, 0.0), voiced)
        k = min(max(int(np.searchsorted(before, position, side="right")) - 1, 0), len(runs) - 1)
        return runs[k][0] + (position - before[k])

    weights = [_weight(word) for word, _breaks in words]
    total = sum(weights)
    ends = np.cumsum(weights) / total
    break_words = [i for i, (_word, breaks) in enumerate(words[:-1]) if breaks]
    pauses = [(before[k + 1] / voiced, runs[k + 1][0] - runs[k][1]) for k in range(len(runs) - 1)]
    matched = _align_breaks([float(ends[i]) for i in break_words], pauses)
    # The text's share and the voice's share that must meet: the line's ends, and each break on its pause.
    anchors = [(0.0, 0.0)]
    anchors += [(float(ends[break_words[b]]), float(before[p + 1])) for b, p in sorted(matched.items())]
    anchors.append((1.0, voiced))

    def spoken_at(share: float) -> float:
        """Seconds of speech said when *share* of the text's letters is."""
        for (s0, v0), (s1, v1) in itertools.pairwise(anchors):
            if share <= s1:
                return v0 + (v1 - v0) * ((share - s0) / (s1 - s0) if s1 > s0 else 0.0)
        return voiced

    out: list[SpokenWord] = []
    begin = 0.0
    for (word, _breaks), end in zip(words, ends, strict=True):
        out.append(SpokenWord(start + time_at(spoken_at(begin)), start + time_at(spoken_at(float(end)) - 1e-6), word))
        begin = float(end)
    return out


def read_wav(path: str | Path) -> tuple[np.ndarray, int]:
    """A WAV file's samples, mixed to mono float, and its rate: 16-bit PCM or 32-bit float."""
    data = Path(path).read_bytes()
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise ValueError("not a WAV file")
    fmt = pcm = None
    at = 12
    while at + 8 <= len(data):
        chunk, size = data[at : at + 4], int.from_bytes(data[at + 4 : at + 8], "little")
        body = data[at + 8 : at + 8 + size]
        if chunk == b"fmt ":
            fmt = body
        elif chunk == b"data":
            pcm = body
        at += 8 + size + (size & 1)
    if fmt is None or pcm is None or len(fmt) < 16:
        raise ValueError("a WAV file without its format or its samples")
    tag, channels, rate = struct.unpack("<HHI", fmt[:8])
    bits = struct.unpack("<H", fmt[14:16])[0]
    if tag == 0xFFFE and len(fmt) >= 26:
        tag = struct.unpack("<H", fmt[24:26])[0]
    if tag == 3 and bits == 32:
        samples = np.frombuffer(pcm[: len(pcm) - len(pcm) % 4], "<f4").astype(np.float32)
    elif tag == 1 and bits == 16:
        samples = np.frombuffer(pcm[: len(pcm) - len(pcm) % 2], "<i2").astype(np.float32) / 32768.0
    else:
        raise ValueError(f"a WAV file of format {tag} at {bits} bits")
    channels = max(1, channels)
    samples = samples[: len(samples) - len(samples) % channels]
    return samples.reshape(-1, channels).mean(axis=1), int(rate)


def _from_audio(path: str, text: str, start: float, seconds: float) -> list[SpokenWord]:
    try:
        samples, rate = read_wav(path)
    except (OSError, ValueError):
        return paced_words(text, start, seconds)
    return measured_words(text, voiced_runs(samples, rate), start) or paced_words(text, start, seconds)


async def word_times(text: str, *, start: float, seconds: float, audio_path: str | None = None) -> list[SpokenWord]:
    """When each word of *text* is said, from *start* for *seconds*: measured on *audio_path*, else paced."""
    if not audio_path:
        return paced_words(text, start, seconds)
    return await asyncio.to_thread(_from_audio, audio_path, text, start, seconds)
