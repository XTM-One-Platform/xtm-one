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

"""How loud a video's music plays: measured at save and in the render check, so a quiet mix is a finding.

The mix levels every bed to the voice (``encoder.prepare_bed``, at
``REFERENCE_LUFS``), plays it at ``music.volume``, ducks it by
``music.duck_db`` where someone speaks (``encoder.duck_points``) and brings
the master to ``MASTER_LUFS``. None of that says how the track itself sounds
there: an AI track mostly silent, a long quiet intro, a volume and a ducking
that lose it under the voice. So the bed the mix takes is measured - its
momentary loudness, the BS.1770 gating blocks (``encoder.momentary_loudness``)
- and three numbers are derived from it, on the master's scale (:func:`measure`):

* **integrated**: the music at its level where no one speaks, gated as BS.1770 integrates;
* **under the voice**: the same, ducked, over the narration, and how far below the voice it is;
* **the longest quiet stretch**: where its momentary loudness stays under :data:`QUIET_LUFS`.

A bed that fades out (``music.ending`` ``fade``) decays under the video's
close: a quiet stretch that starts in its last seconds - the outro card and
the music's fade out, :func:`closing_seconds` - is the track's own
decay under the closing card, by design: a fact of the ``music`` section,
never a finding.

The master's gain is the mix's own rule applied to what is known of it: the
voice (each narration at ``REFERENCE_LUFS`` times its volume, where it is
said) and the ducked music, brought to ``MASTER_LUFS`` - a clip's own sound
left out.

Past its threshold each is a problem (:meth:`MusicLevel.problems`): a warning
of the render's audio check (``render.check_render``, on the bed it mixed),
never a failed render; an advisory of the quality check after a save
(:func:`planned_music`, :func:`measure_planned`: the bed prepared as the
render prepares it, its loudness kept by its digest in ``render_cache``),
never a refused render.
"""

from __future__ import annotations

import asyncio
import logging
import math
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from app.core.utils import format_exception_message

logger = logging.getLogger(__name__)

#: Music whose momentary loudness stays under this is not heard; :data:`QUIET_SECONDS` of it is a finding.
QUIET_LUFS = -35.0
QUIET_SECONDS = 6.0
#: The music under the voice by more than this is lost under it.
LOST_UNDER_VOICE_DB = 28.0
#: The music under the voice by less than this competes with it: the words are hard to follow.
TOO_LOUD_UNDER_VOICE_DB = 6.0
#: How long the quality check after a save waits for the bed to be prepared and measured.
SAVE_MEASURE_TIMEOUT_SECONDS = 25.0
#: The BS.1770 gating block, and how often one ends (``encoder.momentary_loudness``).
BLOCK_SECONDS = 0.4
HOP_SECONDS = 0.1
_ABSOLUTE_GATE = -70.0
_RELATIVE_GATE = 10.0
#: The ``render_cache`` kind a bed's momentary loudness is kept under, by the bed's digest.
CACHE_KIND = "loudness"
#: The most of a video's end a fading bed's decay may start in (:func:`closing_seconds`).
CLOSING_MAX_SECONDS = 8.0


def _energy(lufs: Any) -> np.ndarray:
    return np.power(10.0, (np.asarray(lufs, dtype=np.float64) + 0.691) / 10.0)


def _lufs(energy: Any) -> np.ndarray:
    return -0.691 + 10.0 * np.log10(np.maximum(np.asarray(energy, dtype=np.float64), 1e-30))


def integrated(energies: np.ndarray) -> float | None:
    """The integrated loudness of gating-block *energies*: BS.1770's absolute, then relative gate; ``None``: silence."""
    loud = energies[energies > _energy(_ABSOLUTE_GATE)]
    if not loud.size:
        return None
    kept = loud[loud > _energy(_lufs(loud.mean()) - _RELATIVE_GATE)]
    return float(_lufs(kept.mean())) if kept.size else None


def _inside(starts: np.ndarray, ends: np.ndarray, windows: Sequence[tuple[float, float]]) -> np.ndarray:
    """The share of each block ``[start, end)`` that *windows* (which never overlap) cover."""
    covered = np.zeros_like(starts)
    for a, b in windows:
        covered += np.clip(np.minimum(ends, float(b)) - np.maximum(starts, float(a)), 0.0, None)
    return np.clip(covered / (ends - starts), 0.0, 1.0)


def _ducking(
    centers: np.ndarray, *, windows: Sequence[tuple[float, float]], depth_db: float, duration: float
) -> np.ndarray:
    """The power the ducking envelope leaves of each block: ``encoder.duck_points``, where the mix bends it."""
    from app.services.video_studio.encoder import duck_points

    if depth_db <= 0 or not windows:
        return np.ones_like(centers)
    points = duck_points(windows, depth_db=depth_db, duration=duration)
    times = np.array([t for t, _gain in points], dtype=np.float64)
    gains = np.array([gain for _t, gain in points], dtype=np.float64)
    across = np.linspace(-BLOCK_SECONDS / 2, BLOCK_SECONDS / 2, 9)
    return (np.interp(centers[:, None] + across[None, :], times, gains) ** 2).mean(axis=1)


def _runs(quiet: np.ndarray) -> list[tuple[int, int]]:
    """``(first, last)`` index of every run of ``True`` in *quiet*, in order."""
    runs: list[tuple[int, int]] = []
    first = None
    for index, value in enumerate([*quiet.tolist(), False]):
        if value and first is None:
            first = index
        elif not value and first is not None:
            runs.append((first, index - 1))
            first = None
    return runs


def _longest(runs: Sequence[tuple[int, int]]) -> tuple[int, int] | None:
    """The longest of *runs* (the first of equals); ``None`` for none."""
    best: tuple[int, int] | None = None
    for run in runs:
        if best is None or run[1] - run[0] > best[1] - best[0]:
            best = run
    return best


def closing_seconds(document: dict, *, ending: str, fade_out: float) -> float:
    """How many of the video's last seconds a fading bed decays in: the outro card's length - the planned
    *document*'s last scene, when it is one - plus the music's *fade_out*, at most :data:`CLOSING_MAX_SECONDS`; 0 for
    a bed whose *ending* is not ``fade``."""
    if ending != "fade":
        return 0.0
    scenes = [scene for scene in document.get("scenes") or [] if isinstance(scene, dict)]
    last = scenes[-1] if scenes else {}
    outro = float(last.get("end") or 0.0) - float(last.get("start") or 0.0) if last.get("type") == "outro" else 0.0
    return min(CLOSING_MAX_SECONDS, max(outro, 0.0) + max(float(fade_out), 0.0))


@dataclass(frozen=True)
class MusicProblem:
    """A level of the music the audience would miss: what, where (seconds in the MP4) and what to change."""

    code: str
    at: float
    end: float | None
    message: str


@dataclass(frozen=True)
class MusicLevel:
    """How loud the music plays (:func:`measure`), in LUFS on the master's scale, its times in the MP4's seconds."""

    #: The music at its level where no one speaks; ``None``: silent.
    integrated: float | None
    #: The music ducked, over the narration; ``None`` without one.
    under_voice: float | None
    #: The voice where it is said; ``None`` without a narration.
    voice: float | None
    #: Where the first narration starts; ``None`` without one.
    narrated_from: float | None
    #: ``(from, to)`` of the longest stretch under :data:`QUIET_LUFS` but its decay; ``None`` for none.
    quiet: tuple[float, float] | None
    volume: float
    duck_db: float
    master_gain_db: float
    #: How many of the video's last seconds its fading bed decays in (:func:`closing_seconds`); 0: none.
    closing: float = 0.0
    #: ``(from, to)`` of the stretch under :data:`QUIET_LUFS` that starts there: the track's decay under the
    #: closing card, by design; ``None`` for none.
    decay: tuple[float, float] | None = None

    @property
    def quiet_seconds(self) -> float:
        return round(self.quiet[1] - self.quiet[0], 2) if self.quiet else 0.0

    @property
    def decay_seconds(self) -> float:
        return round(self.decay[1] - self.decay[0], 2) if self.decay else 0.0

    @property
    def below_voice_db(self) -> float | None:
        """How far under the voice the ducked music plays; ``None`` without a narration or a sound."""
        if self.voice is None or self.under_voice is None:
            return None
        return self.voice - self.under_voice

    def problems(self) -> list[MusicProblem]:
        """What the audience would miss: a silent or too quiet track, a long quiet stretch, music lost under the voice."""
        if self.integrated is None:
            return [
                MusicProblem(
                    "silent",
                    0.0,
                    None,
                    "the music is silent: nothing of it plays above -70 LUFS - choose another track "
                    "(video_studio_capabilities lists the built-in ones)",
                )
            ]
        found: list[MusicProblem] = []
        if self.integrated < QUIET_LUFS:
            found.append(
                MusicProblem(
                    "quiet",
                    0.0,
                    None,
                    f"the music plays at {self.integrated:.1f} LUFS integrated, under {QUIET_LUFS:g} LUFS: too quiet "
                    f"to be heard - raise music.volume (now {self.volume:g}) or choose a louder track",
                )
            )
        elif self.quiet is not None and self.quiet_seconds >= QUIET_SECONDS:
            from app.services.video_studio.render_checks import clock

            found.append(
                MusicProblem(
                    "quiet_stretch",
                    self.quiet[0],
                    self.quiet[1],
                    f"the music stays under {QUIET_LUFS:g} LUFS for {self.quiet_seconds:.1f} s "
                    f"({clock(self.quiet[0])}-{clock(self.quiet[1])}): a quiet passage of the track - start it "
                    "elsewhere (music.start) or choose another track",
                )
            )
        gap = self.below_voice_db
        if gap is not None and gap > LOST_UNDER_VOICE_DB:
            lower = f" or lower music.duck_db (now {self.duck_db:g})" if self.duck_db > 0 else ""
            found.append(
                MusicProblem(
                    "lost_under_voice",
                    self.narrated_from or 0.0,
                    None,
                    f"under the narration the music plays at {self.under_voice:.1f} LUFS, {gap:.0f} dB below the "
                    f"voice (more than {LOST_UNDER_VOICE_DB:g}): lost under it - raise music.volume (now "
                    f"{self.volume:g}){lower}",
                )
            )
        elif gap is not None and gap < TOO_LOUD_UNDER_VOICE_DB:
            deeper = (
                f" or raise music.duck_db (now {self.duck_db:g})"
                if self.duck_db > 0
                else " or turn its ducking on (music.ducking)"
            )
            found.append(
                MusicProblem(
                    "too_loud_under_voice",
                    self.narrated_from or 0.0,
                    None,
                    f"under the narration the music plays at {self.under_voice:.1f} LUFS, only {gap:.1f} dB below the "
                    f"voice (less than {TOO_LOUD_UNDER_VOICE_DB:g}): it competes with the words - lower music.volume "
                    f"(now {self.volume:g}){deeper}",
                )
            )
        return found

    def summary(self) -> dict:
        """The ``music`` section of a render's audio check (``render_checks.Checks.audio``)."""
        from app.services.video_studio.render_checks import FINDINGS, PASSED

        def one(value: float | None) -> float | None:
            return round(value, 1) if value is not None else None

        section = {
            "status": FINDINGS if self.problems() else PASSED,
            "integrated_lufs": one(self.integrated),
            "under_voice_lufs": one(self.under_voice),
            "voice_lufs": one(self.voice),
            "below_voice_db": one(self.below_voice_db),
            "quiet_seconds": self.quiet_seconds,
            "quiet_from": round(self.quiet[0], 2) if self.quiet else None,
            "quiet_to": round(self.quiet[1], 2) if self.quiet else None,
            "quiet_lufs": QUIET_LUFS,
            "volume": self.volume,
            "duck_db": self.duck_db,
            "master_gain_db": round(self.master_gain_db, 1),
        }
        if self.decay is not None:
            section.update(
                decay_seconds=self.decay_seconds,
                decay_from=round(self.decay[0], 2),
                decay_to=round(self.decay[1], 2),
                closing_seconds=round(self.closing, 2),
            )
        return section


def summary_line(section: dict) -> str:
    """The ``Music`` line of a tool answer, from a render's ``music`` section (:meth:`MusicLevel.summary`)."""
    if section.get("integrated_lufs") is None:
        return f"Music: {section.get('status')}: silent (nothing above -70 LUFS)."
    parts = [f"{section['integrated_lufs']:.1f} LUFS integrated"]
    if section.get("under_voice_lufs") is not None and section.get("below_voice_db") is not None:
        parts.append(
            f"{section['under_voice_lufs']:.1f} LUFS under the voice once ducked ({section['below_voice_db']:.0f} dB "
            "below it)"
        )
    lufs, seconds = section.get("quiet_lufs", QUIET_LUFS), section.get("quiet_seconds", 0.0)
    parts.append(f"its longest stretch under {lufs:g} LUFS lasts {seconds:.1f} s")
    line = f"Music: {section.get('status')}: " + ", ".join(parts) + "."
    if section.get("decay_seconds"):
        from app.services.video_studio.render_checks import clock

        line += (
            f" The track decays under the closing card, by design: under {lufs:g} LUFS for "
            f"{section['decay_seconds']:.1f} s ({clock(section.get('decay_from'))}-{clock(section.get('decay_to'))}), "
            f"from inside the video's last {section.get('closing_seconds', 0.0):.1f} s - its outro and the music's "
            "fade out (music.ending fade)."
        )
    return line


def measure(
    blocks: Sequence[tuple[float, float]],
    *,
    duration: float,
    volume: float,
    duck_db: float,
    duck_windows: Sequence[tuple[float, float]] = (),
    voice_windows: Sequence[tuple[float, float]] = (),
    voice_volume: float = 1.0,
    span: tuple[float, float] | None = None,
    closing: float = 0.0,
) -> MusicLevel:
    """How a bed plays in a video of *duration*, from its momentary loudness *blocks* (``[(t, LUFS)]``, at its level).

    *volume* and *duck_db* (0: no ducking) as the mix applies them, the
    envelope bent over *duck_windows*; someone speaks over *voice_windows*, at
    *voice_volume*. *span* ``(start, seconds)`` is the part of the video an
    MP4 plays (a scene preview): what it measures and when, from the MP4's
    start; the master stays the whole mix's, as ``encoder.mix_audio`` makes it.
    *closing* (:func:`closing_seconds`) is how many of the video's last seconds
    its fading bed decays in: a quiet stretch of :data:`QUIET_SECONDS` or more
    starting there is its decay (:attr:`MusicLevel.decay`), never its longest
    quiet stretch; a shorter one is counted as any other.
    """
    from app.services.video_studio.encoder import MASTER_LUFS, REFERENCE_LUFS

    ends = np.array([float(t) for t, _m in blocks], dtype=np.float64)
    levels = np.array([float(m) for _t, m in blocks], dtype=np.float64)
    kept = ends <= duration + HOP_SECONDS / 2
    ends, levels = ends[kept], levels[kept]
    starts = ends - BLOCK_SECONDS
    centers = ends - BLOCK_SECONDS / 2
    music = _energy(levels) * volume**2
    ducked = music * _ducking(centers, windows=duck_windows, depth_db=duck_db, duration=duration)
    spoken = _inside(starts, ends, voice_windows)
    voice_at = REFERENCE_LUFS + 20.0 * math.log10(voice_volume) if voice_volume > 0 else None
    voice = spoken * float(_energy(voice_at)) if voice_at is not None else np.zeros_like(ends)
    mastered = integrated(ducked + voice)
    gain_db = MASTER_LUFS - mastered if mastered is not None else 0.0
    scale = 10.0 ** (gain_db / 10.0)
    offset, until = (span[0], span[0] + span[1]) if span is not None else (0.0, duration)
    shown = (centers >= offset) & (centers <= until)
    heard = music * scale
    # Where someone speaks, the envelope all the way down: a block across a narration's edge is still ramping.
    under = shown & (spoken > 0.999)
    if not under.any():
        under = shown & (spoken >= 0.5)
    narrated = under.any() and voice_at is not None

    def played(run: tuple[int, int] | None) -> tuple[float, float] | None:
        """*run* in the MP4's seconds: from where its first block starts to where its last one ends."""
        if run is None:
            return None
        return max(float(starts[run[0]]), offset) - offset, min(float(ends[run[1]]), until) - offset

    runs = _runs((_lufs(heard) < QUIET_LUFS) & shown)
    closes_from = duration - closing if closing > 0 else None

    def decays(run: tuple[int, int]) -> bool:
        """A run long enough to be a finding that starts inside the closing window: the bed's decay."""
        start, end = played(run)
        return closes_from is not None and start + offset >= closes_from and end - start >= QUIET_SECONDS

    decaying = [run for run in runs if decays(run)]
    quiet = played(_longest([run for run in runs if run not in decaying]))
    first_said = min((float(a) for a, b in voice_windows if b > offset and a < until), default=None)
    return MusicLevel(
        integrated=integrated(heard[shown]),
        under_voice=integrated(ducked[under] * scale) if narrated else None,
        voice=voice_at + gain_db if narrated and voice_at is not None else None,
        narrated_from=max(first_said, offset) - offset if narrated and first_said is not None else None,
        quiet=quiet,
        volume=float(volume),
        duck_db=float(duck_db),
        master_gain_db=float(gain_db),
        closing=float(closing) if closes_from is not None else 0.0,
        decay=played(_longest(decaying)),
    )


# ── At save: the bed the render will prepare ─────────────────────────────


@dataclass(frozen=True)
class PlannedMusic:
    """The music of a saved video as its plan places it (:func:`planned_music`): what :func:`measure` takes."""

    bed: Any
    file_id: object | None
    volume: float
    duck_db: float
    duck_windows: tuple[tuple[float, float], ...]
    voice_windows: tuple[tuple[float, float], ...]
    voice_volume: float
    duration: float
    #: How many of its last seconds the bed decays in (:func:`closing_seconds`).
    closing: float = 0.0


async def planned_music(db: Any, spec: Any, plan: Any) -> PlannedMusic | None:
    """The music *spec* plays under *plan* (``timeline.TimelinePlan``); ``None`` when none plays or it cannot yet.

    A track still being composed, failed or gone is said by the other checks
    (``music.ready_file``), never measured.
    """
    from app.services.video_studio import music as music_tracks

    music = spec.music
    if music is None or music.volume <= 0:
        return None
    bed = music_tracks.Bed.of(music, plan.duration, plan.cues())
    if bed is None:
        return None
    try:
        file_id = await music_tracks.source_file_id(db, bed)
    except (RuntimeError, ValueError):
        return None
    voiced = None if spec.voiceover.enabled else set()
    return PlannedMusic(
        bed=bed,
        file_id=file_id,
        volume=float(music.volume),
        duck_db=float(music.duck_db) if music.ducking else 0.0,
        duck_windows=tuple(plan.duck_windows(voiced=voiced)) if music.ducking else (),
        voice_windows=tuple(plan.voice_windows(voiced=voiced)),
        voice_volume=float(spec.voiceover.volume),
        duration=float(plan.duration),
        closing=closing_seconds(plan.document, ending=music.ending, fade_out=music.fade_out),
    )


async def bed_loudness(bed: Any, file_id: object | None) -> list[tuple[float, float]] | None:
    """The momentary loudness of *bed* as the render prepares it (``music.make_bed``), kept by its digest.

    ``None`` when its file is gone.
    """
    from app.services.video_studio import assets, encoder, render_cache
    from app.services.video_studio.music import make_bed

    digest = bed.digest()
    kept = await render_cache.get_json(CACHE_KIND, digest)
    if isinstance(kept, list):
        return [(float(t), float(m)) for t, m in kept]
    with tempfile.TemporaryDirectory(prefix="xtm-music-level-") as work:
        folder = Path(work)
        source = None
        if file_id is not None:
            source = str(folder / "source")
            if await assets.download_file(file_id, source) is None:
                return None
        bed_path = str(folder / "bed.wav")
        await make_bed(bed, bed_path, source_path=source, workdir=folder)
        blocks = await encoder.momentary_loudness(bed_path)
    await render_cache.put_json(CACHE_KIND, digest, [[round(t, 3), round(m, 1)] for t, m in blocks])
    return blocks


async def measure_planned(planned: PlannedMusic, *, timeout: float = SAVE_MEASURE_TIMEOUT_SECONDS) -> MusicLevel | None:
    """How *planned* will play (:func:`measure`); ``None`` when its bed could not be prepared in *timeout* seconds."""
    try:
        blocks = await asyncio.wait_for(bed_loudness(planned.bed, planned.file_id), timeout=timeout)
    except Exception as exc:  # the render measures it again: a save is never held up by its music
        logger.info("The music of a saved video was not measured: %s", format_exception_message(exc))
        return None
    if blocks is None:
        return None
    return measure(
        blocks,
        duration=planned.duration,
        volume=planned.volume,
        duck_db=planned.duck_db,
        duck_windows=planned.duck_windows,
        voice_windows=planned.voice_windows,
        voice_volume=planned.voice_volume,
        closing=planned.closing,
    )
