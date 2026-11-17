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

"""The beats of a video's music: where ``theme.motion.beat_sync`` lands the cuts and the builds.

A built-in track (``score.py``) keeps its style's tempo from the video's first
frame, so its beats are a grid known before any sound exists. An AI track or an
uploaded sound is listened to once: the onsets of its sound (the spectral flux
of a short-time spectrum), its tempo (the lag between 70 and 180 BPM at which
the onsets repeat best, weighted towards a moderate tempo) and the phase of the
grid that lands on most onsets. A track whose onsets repeat at no tempo clearly
has no beats: nothing is synced, and the save's quality check says so before
the render does.

Every grid is in seconds of the video, past the music's ``start``, and stops
where a track that plays once ends (a loop's crossfade is not on its beat).
The analysis of a file is kept beside the render cache (``render_cache``) by
the file's id - a file's bytes never change under its id - so the preview and
every render read the same beats.
"""

from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass
from typing import Any

from app.core.utils import format_exception_message

logger = logging.getLogger(__name__)

MIN_BPM = 70.0
MAX_BPM = 180.0
#: The tempo a listener hears when a track could be counted at half or twice it.
PREFERRED_BPM = 115.0
#: The analysis: samples a second, the spectrum's window and its hop.
SAMPLE_RATE = 11025
WINDOW = 1024
HOP = 256
#: The most seconds of a track listened to: its tempo is read from them, then carried on.
LISTEN_SECONDS = 240.0
#: How clearly the onsets repeat at the tempo found (its autocorrelation against their mean) for the track to have beats.
MIN_CLARITY = 1.6
#: Seconds from a sound's start to where the spectral flux shows it (the analysis window it takes to come in), measured.
ONSET_LATENCY = (WINDOW - HOP / 4) / SAMPLE_RATE
#: A beat's reach (seconds either side), and how many times more onset strength than an even spread must land there.
NEAR_BEAT = 0.05
MIN_LANDING = 1.8
#: Bumped when the analysis changes: a kept analysis of another version is made again.
ANALYSIS_VERSION = 1
#: The longest a cut is moved to land on a beat, seconds: a slow tempo moves it to the next beat no further.
MAX_SHIFT = 1.0


@dataclass(frozen=True)
class BeatGrid:
    """Beats every ``period`` seconds from ``phase``, up to ``until`` (video seconds)."""

    period: float
    phase: float
    until: float = math.inf

    @property
    def bpm(self) -> float:
        return 60.0 / self.period

    def next(self, t: float) -> float | None:
        """The first beat at or after *t*; ``None`` past the grid's end."""
        k = math.ceil((t - self.phase) / self.period - 1e-6)
        beat = self.phase + max(k, 0) * self.period
        return beat if beat <= self.until else None

    def within(self, start: float, end: float) -> list[float]:
        """The beats in ``[start, end]``."""
        out: list[float] = []
        beat = self.next(start)
        while beat is not None and beat <= end + 1e-9:
            out.append(round(beat, 4))
            beat = beat + self.period if beat + self.period <= self.until else None
        return out


def score_grid(track_id: str) -> BeatGrid | None:
    """The beats of a built-in track: its style's tempo, from the video's first frame."""
    from app.services.video_studio.score import get_style

    style = get_style(track_id)
    return BeatGrid(60.0 / style.bpm, 0.0) if style is not None else None


def detect(samples: Any, rate: int = SAMPLE_RATE) -> tuple[float, float, float] | None:
    """``(bpm, first beat seconds, clarity)`` of mono *samples*; ``None`` when they repeat at no tempo clearly."""
    import numpy as np

    signal = np.asarray(samples, dtype=np.float32)
    if signal.size < WINDOW * 8:
        return None
    frames = 1 + (signal.size - WINDOW) // HOP
    index = np.arange(WINDOW)[None, :] + HOP * np.arange(frames)[:, None]
    spectrum = np.abs(np.fft.rfft(signal[index] * np.hanning(WINDOW).astype(np.float32), axis=1))
    loud = np.log1p(10.0 * spectrum)
    flux = np.maximum(loud[1:] - loud[:-1], 0.0).sum(axis=1)
    if not flux.any():
        return None
    fps = rate / HOP
    # The onsets above their local mean (half a second), so a crescendo is not a beat.
    width = max(1, int(fps * 0.5))
    local = np.convolve(flux, np.ones(width) / width, mode="same")
    onset = np.maximum(flux - local, 0.0)
    onset = onset / (onset.max() or 1.0)
    lags = np.arange(int(fps * 60.0 / MAX_BPM), int(fps * 60.0 / MIN_BPM) + 1)
    centred = onset - onset.mean()
    scores = np.array([float(np.dot(centred[:-lag], centred[lag:])) / (onset.size - lag) for lag in lags])
    if not scores.size or scores.max() <= 0:
        return None
    # A log-normal prior around a moderate tempo: half and twice a tempo repeat as well, the listener hears this one.
    bpms = 60.0 * fps / lags
    weighted = scores * np.exp(-0.5 * (np.log2(bpms / PREFERRED_BPM) / 0.9) ** 2)
    best = int(np.argmax(weighted))
    lag = float(lags[best])
    if 0 < best < len(scores) - 1:
        a, b, c = scores[best - 1], scores[best], scores[best + 1]
        denominator = a - 2 * b + c
        if denominator < 0:
            lag += 0.5 * (a - c) / denominator
    clarity = float(scores[best] / (np.abs(scores).mean() or 1e-9))
    if clarity < MIN_CLARITY:
        return None
    # The period refined to the one the onsets line up on best over the whole track (the lag is a frame's width
    # coarse), then the phase: the circular mean of the onsets' times against it, each weighted by its strength.
    times = np.arange(onset.size) / fps + ONSET_LATENCY
    candidates = (lag / fps) * np.linspace(0.985, 1.015, 121)
    resultants = np.abs(np.exp(2j * np.pi * times[None, :] / candidates[:, None]) @ onset)
    period = float(candidates[int(np.argmax(resultants))])
    angle = float(np.angle(np.sum(onset * np.exp(2j * np.pi * times / period))))
    first = (angle / (2 * np.pi)) % 1.0 * period
    # How much of the onsets' strength lands near the grid, against the share an even spread would put there.
    near = np.abs(((times - first) / period + 0.5) % 1.0 - 0.5) * period <= NEAR_BEAT
    landing = float(onset[near].sum() / (onset.sum() or 1e-9)) / min(1.0, 2 * NEAR_BEAT / period)
    if landing < MIN_LANDING:
        return None
    return 60.0 / period, first, clarity


async def analyse_file(path: str) -> dict | None:
    """The beat analysis of the sound at *path*: ``{bpm, first, seconds}``, or ``None`` when it has no clear beat."""
    import numpy as np

    from app.services.video_studio import encoder

    data = await encoder.decode_mono(path, rate=SAMPLE_RATE)
    samples = np.frombuffer(data, dtype="<f4")
    seconds = samples.size / SAMPLE_RATE
    found = await asyncio.to_thread(detect, samples[: int(LISTEN_SECONDS * SAMPLE_RATE)])
    if found is None:
        return {"bpm": None, "first": None, "seconds": round(seconds, 3)}
    bpm, first, clarity = found
    return {"bpm": round(bpm, 3), "first": round(first, 4), "seconds": round(seconds, 3), "clarity": round(clarity, 2)}


async def file_analysis(file_id: str, *, path: str | None = None) -> dict | None:
    """The beat analysis of file *file_id*, kept by its id: read from the cache, else from *path* or the file.

    ``None`` when the file cannot be read or analysed (nothing is synced).
    """
    from app.services.video_studio import assets, render_cache

    key = f"{file_id}-v{ANALYSIS_VERSION}"
    kept = await render_cache.get_json("beats", key)
    if isinstance(kept, dict):
        return kept
    import tempfile
    from pathlib import Path

    try:
        if path is None:
            with tempfile.TemporaryDirectory(prefix="xtm-beats-") as work:
                local = str(Path(work) / "music")
                if await assets.download_file(file_id, local) is None:
                    return None
                found = await analyse_file(local)
        else:
            found = await analyse_file(path)
    except Exception as exc:  # a sound that cannot be listened to syncs nothing
        logger.info("Music %s not analysed for its beats: %s", file_id, format_exception_message(exc))
        return None
    if found is not None:
        await render_cache.put_json("beats", key, found)
    return found


def file_grid(analysis: dict | None, *, start: float) -> BeatGrid | None:
    """The beats of an analysed track in video seconds: past its ``start``, while it plays once (a loop is off-beat)."""
    if not analysis or not analysis.get("bpm"):
        return None
    period = 60.0 / float(analysis["bpm"])
    first = float(analysis["first"]) - start
    first = first % period if first < 0 else first
    playable = float(analysis.get("seconds") or 0.0) - start
    return BeatGrid(period, round(first, 4), max(playable, 0.0))


class SyncsNothing(str):
    """A note of :func:`video_grid` saying the video asks for beats it cannot have at all: no music, or music
    with no steady tempo - the save's ``beat_sync_unsynced`` finding, naming its two fixes, where the other notes (an AI
    track not made yet) stay notes."""


async def video_grid(spec: Any, *, db: Any = None, music_path: str | None = None, file_id: object = None) -> tuple[BeatGrid | None, str | None]:
    """The beats a video lands its cuts and builds on, and the note when it asks for beats it cannot have
    (:class:`SyncsNothing` when it can have none).

    ``(None, None)`` when the video does not ask (``beat_sync: off``) or has no music.
    """
    motion = spec.theme.motion
    if motion is None or motion.beat_sync == "off":
        return None, None
    music = spec.music
    source = music.source if music is not None else None
    if source is None:
        return None, SyncsNothing(
            f"beat_sync: {motion.beat_sync} asks for the music's beats and the video has no music: nothing is synced - "
            "give it music with a steady beat (every built-in track has one), or set theme.motion.beat_sync: off."
        )
    kind, ref = source
    if kind == "track":
        return score_grid(ref), None
    if file_id is None and kind == "file":
        file_id = ref
    if file_id is None and kind == "music" and db is not None:
        from app.services.video_studio.music import ready_file

        try:
            file_id = await ready_file(db, ref)
        except Exception:  # an AI track not made yet: its beats come with it
            return None, "beat_sync: the AI track is not made yet, its beats are read once it is."
    if file_id is None:
        return None, None
    analysis = await file_analysis(str(file_id), path=music_path)
    grid = file_grid(analysis, start=float(music.start))
    if grid is None:
        return None, SyncsNothing(
            f"beat_sync: {motion.beat_sync}: the music has no clear beat (its onsets repeat at no steady tempo), nothing "
            "is synced - play a track with a steady beat (every built-in track has one), or set theme.motion.beat_sync: "
            "off."
        )
    return grid, None
