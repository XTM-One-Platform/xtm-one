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

"""The voice stem: every narration of a video on one track, sample-exact.

The mix used to place each narration itself (``adelay`` in whole
milliseconds, then ``amix``). The voice is now built here, once, as a file of
its own - the stem - which the mix takes as one input
(``encoder.build_mix_filter``) and the rendered-audio check transcribes
(``audio_qc``):

* **Resampled once, whole.** A segment at another rate than the mix's is
  resampled over its whole length by a windowed-sinc filter (:func:`resample`:
  a Kaiser-windowed sinc, polyphase) before it is placed - never a chunk at a
  time, whose every edge would ring. A segment already at the mix rate (the
  voice chain writes 48 kHz, ``encoder.condition_voice``) is not touched.
* **Placed sample-exact.** A segment starts on the sample of its scene's
  narration start (``round(start * rate)``).
* **Never overlapping.** The planner lasts a scene as long as its narration
  and starts the next narration after it (``timeline.plan``); a segment that
  would still start before the previous one ended is refused
  (:class:`StitchError`), never cut or crossfaded into it.
* **No click.** Every segment edge fades over :data:`EDGE_FADE_SECONDS`, a
  raised cosine.
* **Streamed.** The stem is written as it is built (32-bit float mono WAV),
  a segment at a time and the silence between them in blocks: memory follows
  the longest narration, never the video's length.

:func:`max_sample_jump` and :func:`repeated_span` are what a stitch is
measured with: the largest step from one sample to the next (a
discontinuity), and how much a span of audio repeats the span before it at
the lags a duplicated frame would show (:data:`DUPLICATE_LAGS`).
"""

from __future__ import annotations

import math
import struct
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

import numpy as np

#: The rate of the mix, and so of the stem (``encoder`` mixes at 48 kHz).
MIX_RATE = 48000
#: Every segment fades in and out over this long: no click at its edges.
EDGE_FADE_SECONDS = 0.010
#: The windowed-sinc resampler: the Kaiser window's beta (a stop band near -90 dB).
KAISER_BETA = 9.0
#: Lags at which a span repeating the one before it is a duplicated frame, in seconds.
DUPLICATE_LAGS = (0.05, 0.30)
#: Samples of silence written at once.
_SILENCE_BLOCK = 1 << 16
_FLOAT_TAG = 3


class StitchError(ValueError):
    """Two narrations that would overlap, or a segment that cannot be read; ``str()`` names the scenes."""


@dataclass(frozen=True, slots=True)
class Segment:
    """One narration to place: its scene, its audio and where its scene's narration starts, in seconds."""

    scene_id: str
    path: str
    start: float


@dataclass(frozen=True, slots=True)
class Placement:
    scene_id: str
    #: The stem's first and past-the-last samples of the segment.
    first: int
    end: int
    #: The rate the segment came in, and whether it was resampled to the mix's.
    source_rate: int
    resampled: bool

    def seconds(self, rate: int = MIX_RATE) -> tuple[float, float]:
        return self.first / rate, self.end / rate


@dataclass(frozen=True)
class Stem:
    path: str
    rate: int
    #: Its samples.
    length: int
    placements: tuple[Placement, ...]

    @property
    def duration(self) -> float:
        return self.length / self.rate

    def spans(self) -> dict[str, tuple[float, float]]:
        """``scene id -> (start, end)`` of each narration on the stem, in seconds."""
        return {p.scene_id: p.seconds(self.rate) for p in self.placements}


def resample(samples: np.ndarray, source_rate: int, rate: int = MIX_RATE) -> np.ndarray:
    """*samples* (a whole segment, mono) at *rate*: one polyphase pass of a Kaiser-windowed sinc."""
    from scipy.signal import resample_poly

    if source_rate == rate:
        return samples.astype(np.float32, copy=False)
    common = math.gcd(int(source_rate), int(rate))
    up, down = rate // common, source_rate // common
    return resample_poly(samples.astype(np.float64), up, down, window=("kaiser", KAISER_BETA)).astype(np.float32)


def edge_fades(samples: np.ndarray, rate: int, seconds: float = EDGE_FADE_SECONDS) -> np.ndarray:
    """*samples* faded in and out over *seconds* each (a raised cosine), never more than half of them."""
    out = samples.astype(np.float32, copy=True)
    n = min(round(seconds * rate), len(out) // 2)
    if n <= 0:
        return out
    ramp = (0.5 - 0.5 * np.cos(np.pi * (np.arange(n, dtype=np.float64) + 0.5) / n)).astype(np.float32)
    out[:n] *= ramp
    out[len(out) - n :] *= ramp[::-1]
    return out


def read_segment(path: str) -> tuple[np.ndarray, int]:
    """A narration's samples, mixed to mono float, and its rate."""
    from app.services.video_studio.speech_timing import read_wav

    try:
        return read_wav(path)
    except (OSError, ValueError) as exc:
        raise StitchError(f"{Path(path).name} cannot be read as a WAV file: {exc}") from None


class _FloatWav:
    """A 32-bit float mono WAV written forward, its sizes patched on close."""

    def __init__(self, path: str, rate: int) -> None:
        self._file = open(path, "wb")  # noqa: SIM115 - closed by close()
        self.rate = rate
        self.samples = 0
        fmt = struct.pack("<HHIIHHH", _FLOAT_TAG, 1, rate, rate * 4, 4, 32, 0)
        self._file.write(b"RIFF\x00\x00\x00\x00WAVE")
        self._file.write(b"fmt " + struct.pack("<I", len(fmt)) + fmt)
        self._fact = self._file.tell() + 8
        self._file.write(b"fact" + struct.pack("<I", 4) + b"\x00\x00\x00\x00")
        self._data = self._file.tell() + 4
        self._file.write(b"data\x00\x00\x00\x00")

    def write(self, samples: np.ndarray) -> None:
        self._file.write(np.asarray(samples, dtype="<f4").tobytes())
        self.samples += len(samples)

    def silence(self, count: int) -> None:
        block = np.zeros(min(max(count, 0), _SILENCE_BLOCK), dtype="<f4")
        while count > 0:
            step = min(count, len(block))
            self.write(block[:step])
            count -= step

    def close(self) -> None:
        size = self.samples * 4
        end = self._file.tell()
        self._file.seek(4)
        self._file.write(struct.pack("<I", end - 8))
        self._file.seek(self._fact)
        self._file.write(struct.pack("<I", self.samples))
        self._file.seek(self._data)
        self._file.write(struct.pack("<I", size))
        self._file.close()


def stitch(segments: Iterable[Segment], *, duration: float, out_path: str, rate: int = MIX_RATE) -> Stem:
    """The voice stem of a video lasting *duration* seconds: each segment on its sample, faded, never overlapping.

    Raises :class:`StitchError` when a segment would start before the
    previous one ended: the planner extends a scene to its narration, so an
    overlap is a planning defect, never something to cut or crossfade.
    """
    ordered = sorted(segments, key=lambda s: (s.start, s.scene_id))
    total = max(0, round(duration * rate))
    placements: list[Placement] = []
    out = _FloatWav(out_path, rate)
    try:
        cursor, previous = 0, None
        for segment in ordered:
            samples, source_rate = read_segment(segment.path)
            resampled = source_rate != rate
            placed = edge_fades(resample(samples, source_rate, rate), rate)
            first = max(0, round(segment.start * rate))
            if first < cursor:
                overlap = (cursor - first) / rate
                raise StitchError(
                    f"Scene {segment.scene_id}'s narration starts at {segment.start:.3f} s, {overlap * 1000:.0f} ms "
                    f"before scene {previous}'s ends: two narrations never overlap - the scene before must last "
                    "as long as its narration."
                )
            out.silence(first - cursor)
            out.write(placed)
            cursor = first + len(placed)
            placements.append(Placement(segment.scene_id, first, cursor, int(source_rate), resampled))
            previous = segment.scene_id
        out.silence(total - cursor)
    finally:
        out.close()
    return Stem(out_path, rate, max(total, cursor), tuple(placements))


# ── Measures ──────────────────────────────────────────────────────────────


def max_sample_jump(samples: np.ndarray) -> float:
    """The largest step from one sample to the next: a discontinuity shows as a step no signal of its band makes."""
    if len(samples) < 2:
        return 0.0
    return float(np.max(np.abs(np.diff(samples.astype(np.float64)))))


def repeated_span(
    samples: np.ndarray, rate: int, *, around: float | None = None, reach: float = 0.6, lags: Sequence[float] = DUPLICATE_LAGS
) -> tuple[float, float]:
    """How much a span of *samples* repeats the span just before it: ``(correlation, lag in seconds)``, the highest.

    For every lag in *lags* (seconds, searched in 5 ms steps) and every
    position within *reach* seconds of *around* (the whole signal when
    ``None``), the normalised correlation of ``x[t : t + lag]`` with
    ``x[t + lag : t + 2 lag]``: a frame written twice correlates at 1, speech
    and noise far below. Silent spans are skipped.
    """
    x = samples.astype(np.float64)
    low, high = (round(lag * rate) for lag in lags)
    step = max(1, round(0.005 * rate))
    hop = max(1, round(0.01 * rate))
    begin, end = 0, len(x)
    if around is not None:
        begin = max(0, round((around - reach) * rate))
        end = min(len(x), round((around + reach) * rate))
    # A span under -60 dB of the loudest sample is silence: nothing to repeat.
    quiet = (1e-3 * float(np.max(np.abs(x)))) ** 2 if len(x) else 0.0
    best, best_lag = 0.0, 0.0
    for lag in range(low, high + 1, step):
        for t in range(begin, max(begin, end - 2 * lag) + 1, hop):
            a, b = x[t : t + lag], x[t + lag : t + 2 * lag]
            if len(b) < lag:
                break
            ea, eb = float(np.dot(a, a)), float(np.dot(b, b))
            if ea <= quiet * lag or eb <= quiet * lag:
                continue
            value = float(np.dot(a, b)) / math.sqrt(ea * eb)
            if value > best:
                best, best_lag = value, lag / rate
    return best, best_lag
