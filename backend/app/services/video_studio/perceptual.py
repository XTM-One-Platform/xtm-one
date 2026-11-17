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

"""Perceptual quality gates: what a viewer sees as poor at once while every other gate is green.

Three measures, each pure and cheap, judged by :mod:`quality` (issue #10036):

- **A synthetic voice** (:func:`timbre`, :func:`synthetic_voice`): a narration's spectral flatness and the share of
  its energy above :data:`ARTIFACT_HZ` - the metallic, buzzy texture a time-stretched or degraded take carries -
  compared with the same voice at speed 1 when a reference is known, else with fixed ceilings. And a narration said
  under :data:`MIN_STRETCH_SPEED` on a speech model that slows by time-stretching (:func:`stretched_speed`).
- **Texture noise** (:func:`texture_noise`): an AI shot frame whose large areas carry spatially uncorrelated noise or
  over-sharpening (a crunchy carpet, fur bleeding onto subjects), read on the least structured pixels of each tile so
  natural texture (stone, straw, cobbles) is not counted; judged against the shot's start picture when it has one
  (noise the take adds), else against the rest of the frame, or an absolute floor when the region covers most of the
  frame (issue #10083). The region is named as a box of the frame (0-1).
- **Capture-rate floor** (:func:`under_capture_floor`): a recording that painted under :data:`MIN_INSERT_PAINTED_FPS`
  frames a second is refused as a screen insert source.

A finding of these gates blocks a render of a video whose template is made for a brand film or a product demo
(:func:`blocks`), and is advice on any other.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

#: The template purposes (``templates.TEMPLATE_PURPOSES``) on which a perceptual finding blocks a render.
BLOCKING_PURPOSES = frozenset({"brand_film", "product_demo"})

# ── Voice ─────────────────────────────────────────────────────────────────

#: Speech models that slow a narration by time-stretching the waveform (no prosody re-synthesis): under
#: :data:`MIN_STRETCH_SPEED` they sound artificial.
TIME_STRETCHING_MODELS = frozenset({"tts-1", "tts-1-hd"})
#: Under this speed a time-stretching model's narration is said to sound artificial (advisory).
MIN_STRETCH_SPEED = 0.95
#: Above this frequency, energy in a narration is artifact rather than voice (sibilants peak lower).
ARTIFACT_HZ = 8000.0
#: The frame the spectrum is read on, in samples, and the energy floor (relative) under which a frame is silence.
_FRAME = 2048
_SILENCE = 1e-4
#: A narration flatter than this, or with more than this share of its energy above :data:`ARTIFACT_HZ`, sounds
#: synthetic with no reference to compare with.
MAX_FLATNESS = 0.32
MAX_ARTIFACT_SHARE = 0.06
#: Against the same voice at speed 1: this much flatter, or this many times its artifact share, sounds synthetic.
MAX_FLATNESS_RISE = 0.08
MAX_ARTIFACT_RATIO = 2.5


@dataclass(frozen=True)
class Timbre:
    """A narration's timbre: its mean spectral flatness (0 tonal - 1 white noise) over its voiced frames, and the
    share of its energy above :data:`ARTIFACT_HZ`."""

    flatness: float
    artifact_share: float

    def as_dict(self) -> dict[str, Any]:
        return {"flatness": round(self.flatness, 4), "artifact_share": round(self.artifact_share, 4)}

    @classmethod
    def from_dict(cls, data: Any) -> Timbre | None:
        if not isinstance(data, dict) or data.get("flatness") is None or data.get("artifact_share") is None:
            return None
        return cls(float(data["flatness"]), float(data["artifact_share"]))


def timbre(samples: Any, rate: int) -> Timbre | None:
    """The :class:`Timbre` of mono *samples* (floats, -1..1) at *rate* Hz; ``None`` when nothing is voiced or the
    rate cannot carry :data:`ARTIFACT_HZ`."""
    import numpy as np

    data = np.asarray(samples, dtype=np.float64).reshape(-1)
    if rate <= 2 * ARTIFACT_HZ or data.size < _FRAME:
        return None
    count = data.size // _FRAME
    frames = data[: count * _FRAME].reshape(count, _FRAME) * np.hanning(_FRAME)
    power = np.abs(np.fft.rfft(frames, axis=1)) ** 2
    energy = power.sum(axis=1)
    # Silence (digital or under the floor relative to the loudest frame) is never voiced.
    voiced = (energy > energy.max() * _SILENCE) & (energy > _FRAME * _SILENCE)
    if not voiced.any():
        return None
    power = power[voiced] + 1e-12
    flatness = np.exp(np.log(power).mean(axis=1)) / power.mean(axis=1)
    freqs = np.fft.rfftfreq(_FRAME, 1.0 / rate)
    high = power[:, freqs >= ARTIFACT_HZ].sum() / power.sum()
    return Timbre(float(flatness.mean()), float(high))


def synthetic_voice(measured: Timbre, reference: Timbre | None = None) -> str | None:
    """Why *measured* sounds synthetic - against *reference* (the same voice at speed 1) when known, else the
    fixed ceilings -; ``None`` when it does not."""
    if reference is not None:
        rise = measured.flatness - reference.flatness
        ratio = measured.artifact_share / max(reference.artifact_share, 1e-6)
        if rise > MAX_FLATNESS_RISE:
            return f"spectral flatness {measured.flatness:.2f}, {rise:+.2f} over the same voice at speed 1"
        if ratio > MAX_ARTIFACT_RATIO and measured.artifact_share > MAX_ARTIFACT_SHARE / 2:
            return (
                f"{measured.artifact_share:.1%} of its energy above {ARTIFACT_HZ / 1000:g} kHz, {ratio:.1f}x the same "
                "voice at speed 1 (metallic artifacts)"
            )
        return None
    if measured.flatness > MAX_FLATNESS:
        return f"spectral flatness {measured.flatness:.2f} over {MAX_FLATNESS:.2f}"
    if measured.artifact_share > MAX_ARTIFACT_SHARE:
        return (
            f"{measured.artifact_share:.1%} of its energy above {ARTIFACT_HZ / 1000:g} kHz, over "
            f"{MAX_ARTIFACT_SHARE:.0%} (metallic artifacts)"
        )
    return None


def stretched_speed(model: str | None, speed: float) -> str | None:
    """The advisory of a narration said at *speed* on a speech *model* that time-stretches; ``None`` otherwise."""
    if not model or model.strip().lower() not in TIME_STRETCHING_MODELS or speed >= MIN_STRETCH_SPEED:
        return None
    return (
        f"its narration is said at speed {speed:g} on {model}, which slows a voice by time-stretching it: under "
        f"{MIN_STRETCH_SPEED:g} it sounds artificial - keep the speed at 1 and give the scene more time, or pick a "
        "speech model that speaks slower natively"
    )


# ── Picture ───────────────────────────────────────────────────────────────

#: The grid a frame is read on and the grey side it is read at.
_GRID = 8
_SIDE = 1024
#: The noise residual: per pixel, the smallest absolute second difference over the four directions (horizontal,
#: vertical, both diagonals). An edge, a stone joint or a straw strand is smooth along at least one direction and
#: answers near 0 there; uncorrelated noise answers in every direction.
_DIRECTIONS = (
    ((0.0, 0.0, 0.0), (1.0, -2.0, 1.0), (0.0, 0.0, 0.0)),
    ((0.0, 1.0, 0.0), (0.0, -2.0, 0.0), (0.0, 1.0, 0.0)),
    ((1.0, 0.0, 0.0), (0.0, -2.0, 0.0), (0.0, 0.0, 1.0)),
    ((0.0, 0.0, 1.0), (0.0, -2.0, 0.0), (1.0, 0.0, 0.0)),
)
#: The residual is averaged over patches of this side, and a tile's noise is this quantile of its patches: noise sits
#: in every patch of a tile, texture in some of them only, so the quietest patches read the noise alone.
_PATCH = 8
_PATCH_QUANTILE = 0.1
#: The region must cover at least this share of the frame to be judged: a large area a viewer reads.
MIN_FLAT_SHARE = 0.15
#: Without a start picture, a tile is in the region when its noise (grey levels 0-255) reaches this: clean footage of
#: busy natural texture (straw, stone, cobbles) stays under it (11 at most on the calibration barn), a crunchy
#: over-sharpened carpet does not (18 and over).
MIN_NOISE_LEVEL = 14.0
#: And the region carries this many times the noise of the rest of the frame.
MAX_TEXTURE_RATIO = 2.2
#: A region over this share of the frame has no meaningful rest to compare with: it is judged against
#: :data:`ABSOLUTE_NOISE_FLOOR` alone.
WHOLE_FRAME_SHARE = 0.6
#: The noise a clean frame never carries over most of its area (calibrated as :data:`MIN_NOISE_LEVEL`).
ABSOLUTE_NOISE_FLOOR = 14.0
#: With a start picture: a tile is in the region when the take carries this many times the start picture's noise
#: there, and at least :data:`MIN_ADDED_NOISE` - noise the take adds, never what the start picture already shows.
MAX_ADDED_RATIO = 2.5
MIN_ADDED_NOISE = 4.0
#: The start picture's noise is read as at least this (a perfectly clean start would make any take infinitely noisier).
_START_NOISE_FLOOR = 1.0


@dataclass(frozen=True)
class TextureNoise:
    """A frame region carrying noise or over-sharpening: its box (0-1 ``x0, y0, x1, y1``), its share of the frame,
    the ratio, its noise level (sigma, grey levels) and what it was judged against: ``start`` (the take over its start
    picture), ``frame`` (the rest of the frame) or ``floor`` (:data:`ABSOLUTE_NOISE_FLOOR`, the region covering most
    of the frame)."""

    box: tuple[float, float, float, float]
    share: float
    ratio: float
    level: float
    against: str = "frame"

    def as_dict(self) -> dict[str, Any]:
        return {
            "box": [round(v, 3) for v in self.box],
            "share": round(self.share, 3),
            "ratio": round(self.ratio, 2),
            "level": round(self.level, 2),
            "against": self.against,
        }

    @classmethod
    def from_dict(cls, data: Any) -> TextureNoise | None:
        try:
            x0, y0, x1, y1 = (float(v) for v in data["box"])
            against = str(data.get("against") or "frame")
            return cls((x0, y0, x1, y1), float(data["share"]), float(data["ratio"]), float(data["level"]), against)
        except (AttributeError, KeyError, TypeError, ValueError):
            return None

    def region(self) -> str:
        """``the lower half (x 0.00-1.00, y 0.50-1.00)``: where the noise sits, in words a director reads."""
        x0, y0, x1, y1 = self.box
        cy, cx = (y0 + y1) / 2, (x0 + x1) / 2
        vertical = "upper" if cy < 0.4 else "lower" if cy > 0.6 else "middle"
        horizontal = "left" if cx < 0.4 else "right" if cx > 0.6 else "centre"
        return f"the {vertical} {horizontal} of the frame (x {x0:.2f}-{x1:.2f}, y {y0:.2f}-{y1:.2f})"

    def measure(self) -> str:
        """How far over the line it is, against what it was judged on."""
        if self.against == "start":
            return f"{self.ratio:.1f}x the noise of its start picture there (sigma {self.level:.1f})"
        if self.against == "floor":
            return f"noise sigma {self.level:.1f}, over the {ABSOLUTE_NOISE_FLOOR:g} a clean frame never reaches"
        return f"{self.ratio:.1f}x the noise of the rest of the frame (sigma {self.level:.1f})"


def noise_map(picture: Any, size: tuple[int, int] | None = None) -> Any:
    """The ``_GRID x _GRID`` noise of *picture* (a PIL image; resized to *size* when given): per tile, the
    spatially uncorrelated high-frequency residual (:data:`_DIRECTIONS`) of its quietest patches
    (:data:`_PATCH_QUANTILE`), in grey levels. ``None`` when the picture is too small to read."""
    import numpy as np
    from scipy import ndimage

    grey = picture.convert("L")
    if size is not None:
        grey = grey.resize(size)
    else:
        grey.thumbnail((_SIDE, _SIDE))
    g = np.asarray(grey, dtype=np.float64)
    h, w = g.shape
    th, tw = h // _GRID, w // _GRID
    if th < 2 * _PATCH or tw < 2 * _PATCH:
        return None
    residual = np.min(
        [np.abs(ndimage.convolve(g, np.asarray(kernel), mode="reflect")) for kernel in _DIRECTIONS], axis=0
    )
    noise = np.zeros((_GRID, _GRID))
    ph, pw = th // _PATCH, tw // _PATCH
    for i in range(_GRID):
        for j in range(_GRID):
            tile = residual[i * th : i * th + ph * _PATCH, j * tw : j * tw + pw * _PATCH]
            patches = tile.reshape(ph, _PATCH, pw, _PATCH).mean(axis=(1, 3))
            noise[i, j] = float(np.quantile(patches, _PATCH_QUANTILE))
    return noise


def _box(mask: Any) -> tuple[float, float, float, float]:
    import numpy as np

    rows, cols = np.nonzero(mask)
    return (
        float(cols.min() / _GRID),
        float(rows.min() / _GRID),
        float((cols.max() + 1) / _GRID),
        float((rows.max() + 1) / _GRID),
    )


def texture_noise(picture: Any, start: Any = None) -> TextureNoise | None:
    """The :class:`TextureNoise` of *picture* (a PIL image), against its *start* picture when the shot has one;
    ``None`` when it is clean.

    With *start*: only the noise the take adds over its start picture counts (:data:`MAX_ADDED_RATIO`). Without: the
    tiles over :data:`MIN_NOISE_LEVEL` against the rest of the frame (:data:`MAX_TEXTURE_RATIO`), or against
    :data:`ABSOLUTE_NOISE_FLOOR` alone when they cover more than :data:`WHOLE_FRAME_SHARE` of it."""
    import numpy as np

    take = noise_map(picture)
    if take is None:
        return None
    if start is not None:
        grey = picture.convert("L")
        grey.thumbnail((_SIDE, _SIDE))
        before = noise_map(start, grey.size)
        if before is not None:
            ratio_map = take / np.maximum(before, _START_NOISE_FLOOR)
            added = (ratio_map >= MAX_ADDED_RATIO) & (take >= MIN_ADDED_NOISE)
            share = float(added.mean())
            if share < MIN_FLAT_SHARE:
                return None
            return TextureNoise(_box(added), share, float(ratio_map[added].mean()), float(take[added].mean()), "start")
    noisy = take >= MIN_NOISE_LEVEL
    share = float(noisy.mean())
    if share < MIN_FLAT_SHARE:
        return None
    level = float(take[noisy].mean())
    if share > WHOLE_FRAME_SHARE:
        if level < ABSOLUTE_NOISE_FLOOR:
            return None
        return TextureNoise(_box(noisy), share, level / ABSOLUTE_NOISE_FLOOR, level, "floor")
    ratio = level / max(float(take[~noisy].mean()), _START_NOISE_FLOOR)
    if ratio < MAX_TEXTURE_RATIO:
        return None
    return TextureNoise(_box(noisy), share, ratio, level, "frame")


#: Where a shot's texture reading is kept, by its frames' files (``render_cache``).
TEXTURE_CACHE_KIND = "perceptual-texture"
TEXTURE_VERSION = 2


async def shot_texture(
    frame_file_ids: dict[str, str], workdir: str, start_file_id: str | None = None
) -> tuple[str, TextureNoise] | None:
    """``(frame file id, its texture noise)`` of the noisiest of an AI shot's kept frames (``params name -> file
    id``), each judged against the shot's start picture (*start_file_id*, ``clips.start_frame_file_id``) when it has
    one, read once per take (``render_cache``); ``None`` when every frame is clean or none can be read (logged)."""
    import hashlib
    import json
    import logging
    from pathlib import Path

    from app.services.video_studio import assets, frame_qc, render_cache

    ids = [str(frame_file_ids[name]) for name, _at in frame_qc.FRAMES if frame_file_ids.get(name)]
    if not ids:
        return None
    digest = hashlib.sha256(
        json.dumps({"v": TEXTURE_VERSION, "frames": ids, "start": start_file_id}).encode()
    ).hexdigest()
    kept = await render_cache.get_json(TEXTURE_CACHE_KIND, digest)
    if isinstance(kept, dict):
        if not kept.get("frame_file_id"):
            return None
        noise = TextureNoise.from_dict(kept.get("noise"))
        return (str(kept["frame_file_id"]), noise) if noise is not None else None
    worst: tuple[str, TextureNoise] | None = None
    try:
        start = None
        if start_file_id:
            start_path = Path(workdir) / f"texture-{digest[:12]}-start"
            if await assets.download_file(start_file_id, str(start_path)) is not None:
                start = frame_qc.open_picture(start_path.read_bytes())
        for index, file_id in enumerate(ids):
            path = Path(workdir) / f"texture-{digest[:12]}-{index}"
            if await assets.download_file(file_id, str(path)) is None:
                return None
            found = texture_noise(frame_qc.open_picture(path.read_bytes()), start)
            if found is not None and (worst is None or found.ratio > worst[1].ratio):
                worst = (file_id, found)
    except Exception as exc:  # advisory reading: a shot is never judged on a reading that failed
        logging.getLogger(__name__).info("Texture noise of a shot not measured: %s", exc)
        return None
    await render_cache.put_json(
        TEXTURE_CACHE_KIND, digest, {"frame_file_id": worst[0], "noise": worst[1].as_dict()} if worst else {}
    )
    return worst


# ── Screen inserts ────────────────────────────────────────────────────────

#: A recording painted under this many frames a second is refused as a screen insert source.
MIN_INSERT_PAINTED_FPS = 15.0


def painted_fps(captured_frames: int | None, duration: float | None) -> float | None:
    """The frames a recording's browser painted a second (``TakeMedia.captured_frames`` over its length)."""
    if not captured_frames or not duration or duration <= 0:
        return None
    return float(captured_frames) / float(duration)


#: Where a take's capture keeps the rate it painted at while its page changed (``capture.insert_rate``,
#: ``insert_floor.motion_rate``, #10087): ``{whole, motion, seconds, windows}``.
INSERT_RATE_KEY = "insert_rate"


def still(measured: Any) -> bool:
    """Whether a take measured over its motion windows (*measured*, :data:`INSERT_RATE_KEY`) never moved: a still page
    is fully captured, a held frame being exact."""
    return isinstance(measured, dict) and measured.get("whole") is not None and measured.get("motion") is None


def insert_rate(captured_frames: int | None, duration: float | None, measured: Any = None) -> float | None:
    """The rate the insert capture floor judges: the rate a take painted at while its page changed when it was measured
    so (*measured*, :data:`INSERT_RATE_KEY`; ``None`` for a still take, :func:`still`), else - a take recorded before
    the measure - its frames over its whole length (:func:`painted_fps`)."""
    if isinstance(measured, dict) and measured.get("whole") is not None:
        motion = measured.get("motion")
        return float(motion) if motion is not None else None
    return painted_fps(captured_frames, duration)


def under_capture_floor(captured_frames: int | None, duration: float | None, measured: Any = None) -> float | None:
    """The rate of a recording under :data:`MIN_INSERT_PAINTED_FPS` (:func:`insert_rate`: over the seconds its page
    changed when *measured*), ``None`` when it clears it, held still or is unknown."""
    rate = insert_rate(captured_frames, duration, measured)
    return rate if rate is not None and rate < MIN_INSERT_PAINTED_FPS else None


# ── Severity ──────────────────────────────────────────────────────────────


def blocks(purposes: Iterable[str] | None) -> bool:
    """Whether a perceptual finding blocks a render of a video whose template is made for *purposes*."""
    return bool(BLOCKING_PURPOSES.intersection(purposes or ()))


def purposes_of(template: Any) -> tuple[str, ...]:
    """The purposes of the template a video wears (``templates.VideoTemplateStyle.purposes``); ``()`` for none."""
    style = getattr(template, "style", None)
    return tuple(getattr(style, "purposes", None) or ())
