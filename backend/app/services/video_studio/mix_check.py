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

"""What a render's mixed audio sounds like, measured: loudness, true peak, music fit, narration pace, silences.

The render's other checks hear the narration's words (``audio_qc``), how loud
the bed plays against the voice (``music_level``) and watch the picture
(``frame_freeze``). None of them measures the mix a viewer hears as a whole, so
a film could pass every gate with music that stops before the outro, a bed
that loops with an audible seam, a narration read at 95 words a minute or a
held picture over silence. This module reads the encoded MP4's sound and the
narration stem, and says per scene and for the whole MP4:

* **loudness and true peak**: BS.1770 integrated loudness (LUFS) and the
  highest true peak (dBTP) of each scene and of the whole MP4, from the
  ``ebur128`` frame log of the final encode (``encoder.loudness_log``);
* **music fit**: where the bed stops being heard against the video's end, and
  where a bed shorter than the video loops - the seam found as the repeated
  segment of the bed (a cross-correlation of the bed against itself near its
  source's length), named with its time;
* **narration pace**: words a minute of each narration over its voiced
  seconds on the stem, against the speed it was asked at;
* **silences**: a silent stretch of the mix longer than :data:`SILENT_GAP_SECONDS`
  inside a narrated scene, and a still picture (held by design or frozen) over
  which nothing is heard - a dead hold.

Each is a warning (:data:`render_checks.AUDIO_MIX`, the code in its data): the
render is ready, the person hears it and decides. The numbers are the audio
check's ``mix`` section (:meth:`MixReport.summary`), the ``Mix`` line of a
render's answer (:func:`summary_line`). Nothing here changes the picture or the
mix: it only reads them. ffmpeg and numpy only, no external service.
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass, field

import numpy as np

#: A momentary loudness under this is nothing heard (the mix's floor; a music fade's tail stays above it).
SILENT_LUFS = -50.0
#: A silent stretch of a narrated scene longer than this is a gap in the film.
SILENT_GAP_SECONDS = 1.5
#: A still picture over silence at least this long is a dead hold.
DEAD_HOLD_SECONDS = 1.0
#: The music stopping more than this before the video's end leaves its end without music.
MUSIC_EARLY_SECONDS = 1.0
#: A narration slower or faster than this, in words a minute, is flagged.
SLOW_WPM, FAST_WPM = 110.0, 180.0
#: A narration of fewer words is never flagged slow: its words a minute are read off a second or two of voice, its
#: take's lead-in and tail weigh as much as its words, and no speed keeps its subtitles readable over the length that
#: pace asks (``quality.CAPTION_PACE_MIN_WORDS``, the save's subtitle check, agrees). Its row still says its pace.
PACE_MIN_WORDS = 6
#: Words a minute of a narration at speed 1.0: what the pace is read against.
REFERENCE_WPM = 150.0
#: A true peak above this, in dBTP, clips on some players (the master aims at ``encoder.MASTER_TRUE_PEAK``).
TRUE_PEAK_LIMIT = -1.0
#: The bed's repeated segment is searched this far around its repeat lag (``encoder.bed_repeat``), and confirmed above
#: this.
SEAM_SEARCH_SECONDS = 1.0
SEAM_CORRELATION = 0.8
#: The rate the bed is decoded at to find its seam, and the length of the segment compared.
SEAM_RATE = 8000
SEAM_SEGMENT_SECONDS = 2.0
#: The BS.1770 momentary block (``encoder.momentary_loudness``).
_BLOCK = 0.4

_WORD_RE = re.compile(r"[\w'’-]+", re.U)


def count_words(text: str) -> int:
    return len(_WORD_RE.findall(text or ""))


def _energy(lufs: np.ndarray) -> np.ndarray:
    return np.power(10.0, (lufs + 0.691) / 10.0)


def gated_loudness(levels: Sequence[float]) -> float | None:
    """BS.1770 integrated loudness of momentary *levels* (LUFS): absolute then relative gate; ``None``: silence."""
    values = np.asarray([float(v) for v in levels], dtype=np.float64)
    energy = _energy(values)
    loud = energy[values > -70.0]
    if not loud.size:
        return None
    kept = loud[-0.691 + 10.0 * np.log10(loud) > -0.691 + 10.0 * math.log10(float(loud.mean())) - 10.0]
    return round(float(-0.691 + 10.0 * math.log10(float(kept.mean()))), 1) if kept.size else None


@dataclass(frozen=True)
class MixProblem:
    """What a viewer would hear wrong: its code, where (seconds of the MP4), its scene, and what to change."""

    code: str
    at: float
    end: float | None
    scene_id: str | None
    message: str


@dataclass(frozen=True)
class ScenePace:
    """One narration as the stem says it: its words, its voiced seconds, and the speed it was asked at."""

    scene_id: str
    words: int
    seconds: float
    speed: float
    #: ``(from, to)`` in the video's seconds where its voice starts and stops.
    voiced: tuple[float, float] | None = None

    @property
    def wpm(self) -> float | None:
        return round(self.words * 60.0 / self.seconds, 1) if self.seconds > 0.5 and self.words else None

    @property
    def expected_wpm(self) -> float:
        return round(REFERENCE_WPM * self.speed, 1)


def narration_pace(
    samples: np.ndarray,
    rate: int,
    spans: dict[str, tuple[float, float]],
    texts: dict[str, str],
    speeds: dict[str, float],
) -> list[ScenePace]:
    """The pace of each narration of the stem *samples* (mono at *rate*): its words over the seconds from its first
    voiced run to its last inside its placed span (``stitch.Stem.spans``), the take's own padding left out."""
    from app.services.video_studio.speech_timing import voiced_runs

    out: list[ScenePace] = []
    for scene_id, (start, end) in sorted(spans.items(), key=lambda item: item[1][0]):
        words = count_words(texts.get(scene_id, ""))
        if not words:
            continue
        lo, hi = max(0, round(start * rate)), min(len(samples), round(end * rate))
        runs = voiced_runs(samples[lo:hi], rate) if hi > lo else []
        voiced = (start + runs[0][0], start + runs[-1][1]) if runs else (start, end)
        out.append(
            ScenePace(scene_id, words, round(voiced[1] - voiced[0], 2), float(speeds.get(scene_id, 1.0)), voiced)
        )
    return out


def find_loop_seam(
    samples: np.ndarray,
    rate: int,
    *,
    expected: float,
    search: float = SEAM_SEARCH_SECONDS,
    after: float = 1.5,
) -> tuple[float, float] | None:
    """Where the bed *samples* (mono at *rate*) repeats itself: ``(seam seconds, correlation)``, the lag within
    *search* of *expected* (its repeat lag, ``encoder.bed_repeat``) at which a segment of its opening, from *after*
    seconds, comes back best, or ``None`` when the bed is too short to hold the repeat. The correlation says how sure
    it is: a looped bed reads above :data:`SEAM_CORRELATION`."""
    x = np.asarray(samples, dtype=np.float64)
    seg = round(SEAM_SEGMENT_SECONDS * rate)
    # Past a fade in, which the loop's second pass does not repeat, and past the crossfade its repeat enters with.
    first = min(round(after * rate), max(0, len(x) - seg))
    low, high = max(seg, round((expected - search) * rate)), round((expected + search) * rate)
    if first + high + seg > len(x) or high <= low:
        return None
    a = x[first : first + seg]
    b = x[first + low : first + high + seg]
    a = a - a.mean()
    if float(np.dot(a, a)) < 1e-9:
        return None
    n = 1 << math.ceil(math.log2(len(b) + len(a)))
    cross = np.fft.irfft(np.fft.rfft(b, n) * np.conj(np.fft.rfft(a, n)), n)[: len(b) - len(a) + 1]
    window = np.concatenate([[0.0], np.cumsum(b * b)])
    sums = np.concatenate([[0.0], np.cumsum(b)])
    energy_b = window[len(a) :] - window[: len(b) - len(a) + 1]
    mean_b = (sums[len(a) :] - sums[: len(b) - len(a) + 1]) / len(a)
    centred = np.maximum(energy_b - len(a) * mean_b * mean_b, 1e-12)
    corr = cross / np.sqrt(float(np.dot(a, a)) * centred)
    best = int(np.argmax(corr))
    return round((low + best) / rate, 2), round(float(corr[best]), 3)


@dataclass
class MixReport:
    """What the mix measured (:func:`analyse`): times in the MP4's seconds."""

    integrated: float | None
    true_peak: float | None
    duration: float
    scenes: list[dict] = field(default_factory=list)
    music_end: float | None = None
    loop_seam: float | None = None
    problems: list[MixProblem] = field(default_factory=list)

    def summary(self) -> dict:
        from app.services.video_studio.render_checks import FINDINGS, PASSED

        out: dict = {
            "status": FINDINGS if self.problems else PASSED,
            "integrated_lufs": self.integrated,
            "true_peak_dbtp": self.true_peak,
            "duration": round(self.duration, 2),
            "scenes": self.scenes,
        }
        if self.music_end is not None:
            out["music_end"] = round(self.music_end, 2)
        if self.loop_seam is not None:
            out["loop_seam"] = round(self.loop_seam, 2)
        return out


def _silent_runs(blocks: Sequence[tuple[float, float]]) -> list[tuple[float, float]]:
    """``(from, to)`` of every stretch where nothing is heard: a run of momentary blocks under :data:`SILENT_LUFS`."""
    runs: list[list[float]] = []
    for end, lufs in blocks:
        if lufs < SILENT_LUFS:
            start = end - _BLOCK
            if runs and start <= runs[-1][1] + 1e-6:
                runs[-1][1] = end
            else:
                runs.append([max(0.0, start), end])
    return [(a, b) for a, b in runs]


def _within(values: Sequence[tuple[float, float]], start: float, end: float) -> list[float]:
    """The values of the blocks (``(end time, value)``) that lie inside ``[start, end]``."""
    return [v for t, v in values if t - _BLOCK / 2 >= start - 1e-6 and t - _BLOCK / 2 <= end + 1e-6]


def analyse(
    blocks: Sequence[tuple[float, float]],
    *,
    duration: float,
    scenes: Sequence[tuple[str, float, float]],
    peaks: Sequence[tuple[float, float]] = (),
    true_peak: float | None = None,
    paces: Sequence[ScenePace] = (),
    offset: float = 0.0,
    music_end: float | None = None,
    video_end: float | None = None,
    loop: tuple[float, float] | None = None,
    holds: Sequence[tuple[float, float, str | None]] = (),
) -> MixReport:
    """The mix of an MP4 of *duration* seconds, from its momentary loudness *blocks* and frame true *peaks*
    (``[(t, value)]``, the MP4's seconds) and its *true_peak*.

    *scenes* ``[(id, start, end)]`` and *paces* are in the video's seconds; *offset* is where the MP4 starts in it (a
    scene preview). *music_end* is where the bed stops being heard and *video_end* the video's end, in the video's
    seconds; *loop* ``(seam, correlation)`` where its bed repeats (:func:`find_loop_seam`), a seam said when the
    correlation confirms the repeat (:data:`SEAM_CORRELATION`). *holds* ``[(from, to, scene)]`` are the
    pictures held still in the MP4 (by design or frozen), in its seconds.
    """
    from app.services.video_studio.render_checks import clock

    problems: list[MixProblem] = []
    shown_end = offset + duration
    narrated = {pace.scene_id for pace in paces}
    pace_of = {pace.scene_id: pace for pace in paces}
    silent = _silent_runs(blocks)

    def scene_id_at(seconds: float) -> str | None:
        found = None
        for scene_id, start, _end in scenes:
            if start <= seconds + offset:
                found = scene_id
        return found

    rows: list[dict] = []
    for scene_id, start, end in scenes:
        a, b = max(start, offset) - offset, min(end, shown_end) - offset
        if b - a <= 0.05:
            continue
        row: dict = {"scene_id": scene_id, "from": round(a, 2), "to": round(b, 2)}
        row["lufs"] = gated_loudness(_within(blocks, a, b))
        scene_peaks = _within(peaks, a, b)
        row["true_peak_dbtp"] = round(max(scene_peaks), 1) if scene_peaks else None
        if (pace := pace_of.get(scene_id)) is not None:
            row.update(words=pace.words, voiced_seconds=pace.seconds, wpm=pace.wpm, speed=pace.speed)
            row["expected_wpm"] = pace.expected_wpm
            short = pace.words < PACE_MIN_WORDS
            if pace.wpm is not None and ((pace.wpm < SLOW_WPM and not short) or pace.wpm > FAST_WPM):
                slow = pace.wpm < SLOW_WPM
                at = max((pace.voiced or (start, end))[0], offset) - offset
                fix = (
                    "tighten the narration or raise its speed (narration_speed, voiceover.speed)"
                    if slow
                    else "shorten the narration or lower its speed (narration_speed, voiceover.speed)"
                )
                problems.append(
                    MixProblem(
                        "pace_slow" if slow else "pace_fast",
                        at,
                        None,
                        scene_id,
                        f"the narration is read at {pace.wpm:.0f} words a minute ({pace.words} words in "
                        f"{pace.seconds:.1f} s at speed {pace.speed:g}), "
                        f"{'under' if slow else 'over'} {SLOW_WPM if slow else FAST_WPM:.0f}: {fix}",
                    )
                )
        if scene_id in narrated:
            for s, e in silent:
                gap_a, gap_b = max(s, a), min(e, b)
                if gap_b - gap_a > SILENT_GAP_SECONDS:
                    problems.append(
                        MixProblem(
                            "silent_gap",
                            gap_a,
                            gap_b,
                            scene_id,
                            f"nothing is heard for {gap_b - gap_a:.1f} s ({clock(gap_a)}-{clock(gap_b)}) in a narrated "
                            "scene: shorten the scene, close the pause in its narration or give it music",
                        )
                    )
        rows.append(row)

    if true_peak is not None and true_peak > TRUE_PEAK_LIMIT:
        at = max(peaks, key=lambda p: p[1])[0] - 0.1 if peaks else 0.0
        problems.append(
            MixProblem(
                "true_peak",
                max(at, 0.0),
                None,
                scene_id_at(max(at, 0.0)),
                f"the mix peaks at {true_peak:.1f} dBTP, above {TRUE_PEAK_LIMIT:g}: it may clip on some players - "
                "lower a clip's own sound or the music's volume",
            )
        )

    shown_music_end = None
    if music_end is not None and video_end is not None:
        shown_music_end = music_end - offset
        early = video_end - music_end
        # A preview says it only when the video's end is in its window.
        if early > MUSIC_EARLY_SECONDS and video_end <= shown_end + 0.05:
            at = max(music_end, offset) - offset
            problems.append(
                MixProblem(
                    "music_ends_early",
                    at,
                    video_end - offset,
                    scene_id_at(at),
                    f"the music stops at {clock(music_end)}, {early:.1f} s before the video ends at "
                    f"{clock(video_end)}: its end plays without music - loop it (music.loop), start it earlier "
                    "(music.start) or choose a longer track",
                )
            )
    shown_seam = None
    if loop is not None and loop[1] >= SEAM_CORRELATION:
        seam, correlation = loop
        if offset - 0.05 <= seam <= shown_end + 0.05:
            shown_seam = seam - offset
            heard = f"the music repeats itself from {clock(seam)} (its opening heard again, correlation {correlation:.2f})"
            problems.append(
                MixProblem(
                    "loop_seam",
                    shown_seam,
                    None,
                    scene_id_at(shown_seam),
                    f"{heard}: a loop seam - choose a track as long as the video, or end it there (music.loop: false)",
                )
            )

    for hold_a, hold_b, scene_id in holds:
        if hold_b - hold_a < DEAD_HOLD_SECONDS:
            continue
        heard = _within(blocks, hold_a, hold_b)
        if heard and max(heard) < SILENT_LUFS:
            problems.append(
                MixProblem(
                    "dead_hold",
                    hold_a,
                    hold_b,
                    scene_id or scene_id_at(hold_a),
                    f"the picture holds still for {hold_b - hold_a:.1f} s ({clock(hold_a)}-{clock(hold_b)}) and "
                    "nothing is heard: shorten the hold, narrate over it or give it music",
                )
            )

    return MixReport(
        integrated=gated_loudness([v for _t, v in blocks]),
        true_peak=round(true_peak, 1) if true_peak is not None else None,
        duration=duration,
        scenes=rows,
        music_end=shown_music_end,
        loop_seam=shown_seam,
        problems=sorted(problems, key=lambda p: (p.at, p.code)),
    )


def music_end_of(blocks: Sequence[tuple[float, float]]) -> float | None:
    """Where a bed (its momentary loudness at its own level, the video's seconds) is last heard; ``None``: silent."""
    heard = [t for t, lufs in blocks if lufs >= SILENT_LUFS]
    return max(heard) if heard else None


def summary_line(section: dict) -> str:
    """The ``Mix`` line of a tool answer, from a render's ``mix`` section (:meth:`MixReport.summary`)."""
    from app.services.video_studio.render_checks import clock

    lufs, peak = section.get("integrated_lufs"), section.get("true_peak_dbtp")
    parts = [f"{lufs:.1f} LUFS integrated" if lufs is not None else "silent"]
    if peak is not None:
        parts.append(f"true peak {peak:.1f} dBTP")
    if section.get("music_end") is not None:
        parts.append(f"music heard to {clock(section['music_end'])} of {clock(section.get('duration'))}")
    if section.get("loop_seam") is not None:
        parts.append(f"loop seam at {clock(section['loop_seam'])}")
    paced = [
        f"`{row['scene_id']}` {row['wpm']:.0f} wpm" for row in section.get("scenes") or [] if row.get("wpm") is not None
    ]
    if paced:
        parts.append("pace " + ", ".join(paced[:8]) + (f" and {len(paced) - 8} more" if len(paced) > 8 else ""))
    return f"Mix: {section.get('status')}: " + "; ".join(parts) + "."
