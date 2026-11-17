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

"""The production planner: a validated spec plus its media, as a frame-exact timeline.

Pure functions, no I/O: ``render.py`` resolves the media (each recording's
action log and cursor path, clip and image sizes, narration audio lengths)
and hands them here; the result is the JSON document the compositor page
draws frame by frame.

What is decided here, in code, from the recording's action log - never asked
of a model (invariant 16):

* **Durations.** A scene lasts at least as long as its narration. Footage
  shorter than its narration plays its ``extend_mode``
  (:func:`extension`). ``zoom``, the default, fills the scene rather than stop
  on its last frame: a recording gives back the pauses its idle
  compression sped up, a shot plays slower (:func:`filled_length`), and only
  what is still missing holds the last frame, which the compositor keeps
  drifting in. ``loop`` plays the footage at its speed, then its last second
  again and again, crossfaded; ``hold`` freezes its last frame. Consecutive
  narrations never overlap, whatever the transitions.
* **Idle time.** The moments of a recording where nothing moves (a page
  loading, a wait) play faster or are cut, through a piecewise-linear map
  from output time to recording time.
* **The camera.** In ``auto`` mode it follows the action: it zooms toward
  each clicked, typed, selected or highlighted element shown on screen (never
  one trimmed away), sized so the element takes a comfortable share of the
  view, and re-centres on every next step - unless that step is already well
  in view - pulling back on the way when it travels far. It backs out when the
  next step is far enough away for a real wide moment, and for page loads and
  scrolls. A move never shrinks under its length (its :class:`CameraPace`):
  with no room the camera arrives a little late, and only a step it could not
  reach while it is still on screen plays wide. It does not move during the
  transition in. Every move is timed in OUTPUT seconds, so a compressed pause
  never makes the camera jump.
* **The cursor.** The recorded path, mapped to output time; clicks become
  ripples at the exact point clicked.
* **Overlays.** A callout or spotlight with ``at_step`` starts when that step
  happens on screen and points at its element.
* **Captions.** The narration, cut into short phrases timed on the voice.
* **Transitions.** ``auto`` (the default) picks each cut from the scenes on
  either side and the video's pace (:func:`auto_transition`): the opening card
  pushes into the content, words dissolve into pictures, AI shots dissolve or
  push and whip from one to the next, the closing card comes after a dip to
  black, and a shot too short to dissolve is cut.
"""

from __future__ import annotations

import bisect
import dataclasses
import itertools
import math
import re
from dataclasses import dataclass, field
from typing import Any, NamedTuple

from app.services.video_studio import motion_design
from app.services.video_studio.beats import MAX_SHIFT, BeatGrid
from app.services.video_studio.figures import narrated_hold
from app.services.video_studio.frame_owners import CUT_FRAMES, FrameOwners
from app.services.video_studio.spec import (
    BRAND_ASSETS,
    IMAGE_OVERLAY_WIDTH,
    INSERT_DEFAULT_LIGHT,
    INSERT_FADE_SECONDS,
    VideoSpec,
    brand_marks,
    card_draws_logo,
    clip_sound_choice,
    design_size,
)

# ── Tunables of the automatic camera and pacing ───────────────────────────

#: The share of the view a focused element should fill (each side); a
#: highlighted region is framed whole, so it may fill more.
FOCUS_FILL = 0.42
HIGHLIGHT_FILL = 0.75
#: Output seconds the camera stays on a step that opened another page before it
#: backs out to show that page.
PAGE_HOLD = 0.35
#: A zoom below this is not worth the motion.
MIN_USEFUL_ZOOM = 1.12
#: A move lasts its pace's length for this zoom depth, or for a pan of this many
#: view widths: a deeper zoom or a longer pan takes longer, a smaller one a little less.
REFERENCE_ZOOM = 1.8
REFERENCE_PAN = 0.5
#: A next step whose element lies inside the view, this share in from each edge,
#: and whose centre is closer than :data:`MIN_PAN` view widths is not panned to.
FOLLOW_MARGIN = 0.12
MIN_PAN = 0.15
#: A shot changes its zoom for a next step only when that step's own zoom differs
#: by more than this factor, or when its element fills more than :data:`MAX_FILL` of the view.
REZOOM = 1.3
MAX_FILL = 0.85
#: A pan longer than this (view widths) pulls back on the way, by up to half the
#: zoom's depth: the viewer sees where the camera goes.
ARC_FROM = 0.55


@dataclass(frozen=True)
class CameraPace:
    """How the automatic camera moves over a recording, in output seconds."""

    #: A zoom from wide to :data:`REFERENCE_ZOOM`, and back out.
    zoom_in: float
    zoom_out: float
    #: A pan of :data:`REFERENCE_PAN` view widths.
    pan: float
    #: The camera arrives this long before an action and stays this long after it.
    lead: float
    hold: float
    #: The camera rests this long after a move before the next one.
    settle: float
    #: The shortest wide moment between two shots: closer steps stay in one
    #: shot, the camera following them from one to the next.
    min_wide: float
    #: The shortest time in that makes a zoom worth it.
    min_in: float
    #: The scene opens wide at least this long, the page in full, before the first zoom.
    establish: float


CAMERA_PACES = {
    "calm": CameraPace(
        zoom_in=1.1, zoom_out=1.25, pan=0.9, lead=0.5, hold=1.5, settle=0.35, min_wide=1.2, min_in=1.0, establish=0.7
    ),
    "dynamic": CameraPace(
        zoom_in=0.8, zoom_out=0.9, pan=0.65, lead=0.4, hold=1.1, settle=0.25, min_wide=0.8, min_in=0.8, establish=0.4
    ),
}


def camera_pace(name: str, move_seconds: float | None = None) -> CameraPace:
    """The pace *name*, its moves scaled so a zoom in lasts *move_seconds* when given."""
    pace = CAMERA_PACES.get(name, CAMERA_PACES["calm"])
    if move_seconds is None:
        return pace
    scale = move_seconds / pace.zoom_in
    return dataclasses.replace(pace, zoom_in=move_seconds, zoom_out=pace.zoom_out * scale, pan=pace.pan * scale)


#: Active margins around an action, in recording seconds (idle compression).
ACTIVE_BEFORE = 0.3
ACTIVE_AFTER = 0.9
#: Kept at normal speed on each side of a compressed pause.
IDLE_EDGE = 0.25
#: The speed of a "cut" pause: fast enough to read as a jump cut.
CUT_SPEED = 40.0
#: Footage shorter than its scene (its narration) plays slower to fill it, down to this share of its
#: speed, rather than stop on its last frame; only what is still missing then holds that frame.
#: 0.7 keeps motion reading as natural: 24 fps footage still shows about 17 new frames a second, and
#: people and cameras look calm rather than in slow motion.
SLOWEST_FILL = 0.7
#: A compressed pause a narration needs is slowed back toward real time, never below this speed:
#: a spinner or a page load the author asked to compress still reads compressed, never in full.
RELAXED_PAUSE_SPEED = 2.0
#: Narration pacing when no audio has been generated yet (156 words/minute).
WORDS_PER_SECOND = 2.6
NARRATION_TAIL = 0.45
NARRATION_GAP = 0.25
#: A caption phrase is at most this many characters (two lines of 42).
CAPTION_MAX_CHARS = 84

#: The actions each ``camera.zoom_on`` entry frames: a right-click and a drag are clicks to the eye.
_ZOOM_ACTIONS = {
    "click": {"click", "double_click", "right_click", "drag"},
    "type": {"type"},
    "select": {"select"},
    "hover": {"hover"},
    "highlight": {"highlight"},
}
_CLICK_ACTIONS = {"click", "double_click", "right_click", "drag", "select"}
#: The actions the camera stays wide over: the page moves under them on its own (a wheel zooms its canvas).
_WIDE_ACTIONS = {"goto", "scroll", "wheel"}


# ── Media handed in by the renderer ───────────────────────────────────────


@dataclass
class TakeMedia:
    """One recording as the planner reads it (times in seconds of the recording)."""

    take_id: str
    duration: float
    css_width: int
    css_height: int
    pixel_ratio: float
    fps: int
    frame_count: int
    events: list[dict] = field(default_factory=list)
    #: ``(t, x, y)`` in CSS pixels of the viewport.
    cursor: list[tuple[float, float, float]] = field(default_factory=list)
    #: The frames the browser painted: with its scrolls' rates, its real frame rate (``scroll_pacing.take_rate``).
    captured_frames: int | None = None
    #: ``[[start, end, [instants]], ...]`` of the stretches it painted under its rate
    #: (``capture_rate.sparse_spans``), in seconds of its video: what a scene playing it slowed picks the nearest of
    #: (one picture per frame, never a blend).
    sparse: list = field(default_factory=list)
    #: ``[{t, fps, step}, ...]`` of the stretches it animated under 20 fps (``capture_rate.low_spans``).
    low: list = field(default_factory=list)
    #: ``[{t, frames, synthesised, share, fps, step}, ...]`` of the stretches of its motion mostly synthesised from
    #: keyframes (``capture_rate.stepping``).
    stepping: list = field(default_factory=list)
    #: ``[{t, step, action, frames, count}, ...]`` of its frames drawn at another scale on a step holding the page
    #: (``capture_rate.capture_fault``): capture faults, never stepping motion.
    off_scale: list = field(default_factory=list)
    #: ``{whole, motion, seconds, windows}``: the rate it painted at while its page changed (``capture.insert_rate``,
    #: ``insert_floor.motion_rate``), what the insert capture floor holds it to; ``None``: recorded before the measure.
    insert_rate: dict | None = None


@dataclass
class ClipMedia:
    duration: float
    width: int
    height: int
    fps: float
    frame_count: int
    has_audio: bool = False


@dataclass
class ImageMedia:
    width: int
    height: int


@dataclass
class VoiceMedia:
    duration: float


@dataclass
class PlanMedia:
    takes: dict[str, TakeMedia] = field(default_factory=dict)
    #: Keyed by the scene id (a clip scene reads a clip or an uploaded file).
    clips: dict[str, ClipMedia] = field(default_factory=dict)
    #: Keyed by the scene id (slide and image scenes), plus ``"logo"``.
    images: dict[str, ImageMedia] = field(default_factory=dict)
    #: Keyed by the scene id: what plays behind a statement (a video or an image).
    backdrops: dict[str, ClipMedia | ImageMedia] = field(default_factory=dict)
    #: Keyed by the scene id; absent = no audio (estimated pacing).
    voices: dict[str, VoiceMedia] = field(default_factory=dict)
    #: The pictures of image overlays, keyed by :func:`overlay_image_key`; a key absent
    #: is a picture the render could not find (the overlay is left out, with a note).
    overlays: dict[str, ImageMedia] = field(default_factory=dict)
    #: Set by a caller that gathered the overlays' pictures (a render, a preview): an estimate leaves it unset.
    overlays_gathered: bool = False
    #: The sources of screen inserts, keyed by :func:`insert_key`: a video or a picture. A render
    #: fails on a source it cannot read; a key absent from the editor's preview is a source not
    #: made yet (the insert is left out, with a note).
    inserts: dict[str, ClipMedia | ImageMedia] = field(default_factory=dict)
    #: Set by a caller that gathered the inserts' sources (a render, a preview).
    inserts_gathered: bool = False
    #: ``scene id -> its narration as the captions show it``: the written words, but for the lexicon
    #: entries applied to the captions (:func:`caption_texts`); a scene absent shows its narration as written.
    captions: dict[str, str] = field(default_factory=dict)
    #: The Lottie files motion scenes play, keyed by ``motion_design.lottie_key``: a key absent is a file
    #: refused or not found (the element is left out, with a note).
    lotties: dict[str, Any] = field(default_factory=dict)
    #: Set by a caller that read and checked the Lottie files (a render, a preview).
    lotties_gathered: bool = False
    #: The beats of the music in seconds of the video (``beats.BeatGrid``): where ``theme.motion.beat_sync``
    #: lands the cuts and the builds; ``None``, nothing is synced.
    beats: BeatGrid | None = None


def caption_texts(spec: VideoSpec, lexicon: Any) -> dict[str, str]:
    """``scene id -> caption text`` of the narrations a lexicon entry applied to the captions changes."""
    out: dict[str, str] = {}
    for scene in spec.scenes:
        shown = lexicon.caption_text(scene.narration) if scene.narration else None
        if shown and shown != scene.narration:
            out[scene.id or ""] = shown
    return out


def insert_key(scene_id: str | None, index: int, pane: int | None = None) -> str:
    """Where the frames of a scene's screen insert are served from: ``insert/<scene id>.<overlay index>``.

    A pane of a wall is ``insert/<scene id>.<overlay index>.<pane>``.
    """
    return f"insert/{scene_id}.{index}" + (f".{pane}" if pane is not None else "")


def insert_scene(key: str) -> str:
    """The scene id of an :func:`insert_key` (a scene id has no dot)."""
    return key.removeprefix("insert/").split(".", 1)[0]


def insert_label(key: str) -> str:
    """How a screen insert's failure names it: ``its screen insert 0``, or a pane of it."""
    parts = key.removeprefix("insert/").split(".")
    return f"its screen insert {parts[1]}" + (f" (pane {int(parts[2]) + 1})" if len(parts) > 2 else "")


@dataclass(frozen=True)
class InsertPart:
    """One surface a screen insert draws: its one source on its quad, or a pane of a wall."""

    kind: str
    ref: str
    overlay: Any
    quad: tuple[tuple[float, float], ...]
    #: The overlay's index among its scene's overlays, and the pane's among its wall's (``None``: no panes).
    index: int
    pane: int | None = None


def _insert_parts(scene: Any) -> list[tuple[str, InsertPart]]:
    """``(key, part)`` of every surface *scene*'s screen inserts draw, in order: a wall pane by pane."""
    parts = []
    for index, overlay in enumerate(scene.overlays):
        for pane, source, quad in overlay.insert_panes():
            kind, ref = (
                ("take", source.take_id)
                if source.take_id
                else ("clip", source.clip_id)
                if source.clip_id
                else ("asset", source.asset)
                if source.asset
                else ("file", source.file_id)
            )
            parts.append((insert_key(scene.id, index, pane), InsertPart(kind, str(ref), overlay, tuple(quad), index, pane)))
    return parts


def insert_sources(spec: VideoSpec) -> dict[str, InsertPart]:
    """``key -> InsertPart`` of every surface a screen insert draws: ``file`` / ``take`` / ``clip`` and its id,
    or ``asset`` and the brand mark's role."""
    return {key: part for scene in spec.scenes for key, part in _insert_parts(scene)}


#: Where a quad left to the render stands until it is found (:func:`with_quads`): the whole footage.
STAND_IN_QUAD: tuple[tuple[float, float], ...] = ((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0))


def auto_quads(spec: VideoSpec) -> list[tuple[str, int]]:
    """``(scene id, overlay index)`` of every screen insert whose quad the render finds (``quad: auto``)."""
    return [
        (scene.id or "", index)
        for scene in spec.scenes
        for index, overlay in enumerate(scene.overlays)
        if overlay.auto_quad
    ]


def with_quads(spec: VideoSpec, found: dict[tuple[str, int], Any], *, stand_in: bool = False) -> VideoSpec:
    """*spec* with the quads left to the render (``quad: auto``) written: *found*'s by ``(scene id, index)``.

    With *stand_in*, one not found stands on the whole footage
    (:data:`STAND_IN_QUAD`): what the render gathers and plans before it
    reads the footage (:func:`insert_sources` reads it then, the memory
    estimate counts it). Otherwise one not found stays ``auto`` and draws
    nothing.
    """
    autos = set(auto_quads(spec))
    if not autos:
        return spec
    scenes = []
    for scene in spec.scenes:
        overlays = []
        for index, overlay in enumerate(scene.overlays):
            quad = found.get((scene.id or "", index)) if (scene.id or "", index) in autos else None
            if quad is None and stand_in and (scene.id or "", index) in autos:
                quad = STAND_IN_QUAD
            overlays.append(overlay.model_copy(update={"quad": [tuple(p) for p in quad]}) if quad is not None else overlay)
        scenes.append(scene.model_copy(update={"overlays": overlays}))
    return spec.model_copy(update={"scenes": scenes})


def brand_asset_files(spec: VideoSpec, palette: dict) -> dict[str, str | None]:
    """The file of each brand mark a screen insert names by role; ``None`` where the template has none.

    Each role is its own file - ``logo`` the logo the cards draw, ``logo_light``
    its light variant, ``logo_mark`` the bare mark (``spec.brand_marks`` without
    its stand-in) - and never stands for another: an insert naming a mark the
    template lacks is left out, said, rather than drawn with the logo.
    """
    found = palette_marks(spec, palette, stand_in=False)
    return {role: str(file_id) if file_id else None for role, file_id in found.items()}


def overlay_image_key(overlay: Any) -> str | None:
    """Where an image overlay's picture is served from: ``overlay/<file id>`` or ``overlay/<brand mark>``."""
    if getattr(overlay, "type", None) != "image":
        return None
    if overlay.file_id:
        return f"overlay/{overlay.file_id}"
    return f"overlay/{overlay.asset}" if overlay.asset else None


def palette_marks(spec: VideoSpec, palette: dict, *, stand_in: bool = True) -> dict[str, str | None]:
    """The file each brand mark of *spec* draws from its template's *palette* (``spec.brand_marks``)."""
    return brand_marks(
        {
            "logo": palette.get("logo_file_id"),
            "logo_light": palette.get("logo_light_file_id"),
            "logo_mark": palette.get("mark_file_id"),
        },
        light=spec.theme.mode == "light",
        own_logo=spec.theme.logo_file_id,
        stand_in=stand_in,
    )


def overlay_file_ids(spec: VideoSpec, palette: dict) -> dict[str, str | None]:
    """``key -> file id`` of every image overlay's picture (:func:`overlay_image_key`); ``None``: no such mark.

    A brand mark is the template's by its role, as the title cards and the
    watermark read it (``spec.brand_marks``).
    """
    marks = palette_marks(spec, palette)
    found: dict[str, str | None] = {}
    for scene in spec.scenes:
        for overlay in scene.overlays:
            key = overlay_image_key(overlay)
            if key is not None:
                found[key] = overlay.file_id or (str(marks[overlay.asset]) if marks.get(overlay.asset) else None)
    # A motion scene's pictures are served and sized like an image overlay's.
    for element in motion_design.overlay_pictures(spec.scenes):
        key = motion_design.picture_key(element) or ""
        found[key] = element.file_id or (str(marks[element.asset]) if marks.get(element.asset) else None)
    return found


# ── Piecewise-linear time maps ────────────────────────────────────────────


def map_forward(points: list[tuple[float, float]], x: float) -> float:
    """``y`` at *x* on a monotonic piecewise-linear map ``[(x, y), ...]`` (clamped)."""
    if not points:
        return x
    if x <= points[0][0]:
        return points[0][1]
    if x >= points[-1][0]:
        return points[-1][1]
    xs = [p[0] for p in points]
    i = bisect.bisect_right(xs, x) - 1
    (x0, y0), (x1, y1) = points[i], points[i + 1]
    if x1 == x0:
        return y1
    return y0 + (y1 - y0) * (x - x0) / (x1 - x0)


def map_inverse(points: list[tuple[float, float]], y: float) -> float:
    """``x`` where the map reaches *y* first (the map is non-decreasing in ``y``)."""
    if not points:
        return y
    if y <= points[0][1]:
        return points[0][0]
    if y >= points[-1][1]:
        # The first point that reaches the end (a hold after it keeps y).
        for px, py in points:
            if py >= y:
                return px
        return points[-1][0]
    for (x0, y0), (x1, y1) in itertools.pairwise(points):
        if y0 <= y <= y1:
            if y1 == y0:
                return x0
            return x0 + (x1 - x0) * (y - y0) / (y1 - y0)
    return points[-1][0]


def _merge(windows: list[tuple[float, float]]) -> list[tuple[float, float]]:
    merged: list[tuple[float, float]] = []
    for start, end in sorted(windows):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _scroll_window(event: dict) -> tuple[float, float] | None:
    """``(start, end)`` of the scroll the capture made to bring a step's element into view, in recording seconds."""
    scrolled = event.get("scrolled")
    if not isinstance(scrolled, list | tuple) or len(scrolled) != 2:
        return None
    start, end = float(scrolled[0]), float(scrolled[1])
    return (start, end) if end > start else None


def _event_active_windows(events: list[dict]) -> list[tuple[float, float]]:
    """Where something happens on screen, in recording seconds (never a step that was skipped)."""
    windows: list[tuple[float, float]] = []
    for event in events:
        if not event.get("ok", True):
            continue
        action = event.get("action")
        t0 = float(event.get("t0") or 0.0)
        t1 = float(event.get("t1") or t0)
        if scrolled := _scroll_window(event):
            windows.append(scrolled)
        if action == "wait":
            continue
        if action == "pause":
            windows.append((t0, t1))
        elif action == "goto":
            # The navigation itself shows a blank or half-painted page: keep
            # its first instant and the painted end, compress the middle.
            windows.append((t0 - ACTIVE_BEFORE, t0 + 0.3))
            windows.append((t1 - 0.2, t1 + ACTIVE_AFTER))
        elif action == "wait_for":
            windows.append((t1 - 0.2, t1 + ACTIVE_AFTER))
        else:
            windows.append((t0 - ACTIVE_BEFORE, t1 + ACTIVE_AFTER))
    return _merge(windows)


def _skipped_windows(events: list[dict]) -> list[tuple[float, float]]:
    """What a skipped optional step waited before giving up, in recording seconds: footage of nothing."""
    return _merge(
        [
            (float(event.get("t0") or 0.0), float(event.get("t1") or 0.0))
            for event in events
            if not event.get("ok", True) and float(event.get("t1") or 0.0) > float(event.get("t0") or 0.0)
        ]
    )


#: ``(recording a, recording b, rate, compressed)``: a span of the kept recording and how fast it plays;
#: ``compressed`` is a pause the idle compression sped up, which a narration that needs the time takes back.
_Segment = tuple[float, float, float, bool]


def _cut_windows(segments: list[_Segment], windows: list[tuple[float, float]]) -> list[_Segment]:
    """*segments* with the parts inside *windows* played at :data:`CUT_SPEED`: a jump cut over them."""
    if not windows:
        return segments
    out: list[_Segment] = []
    for a, b, rate, compressed in segments:
        cursor = a
        for wa, wb in windows:
            lo, hi = max(wa, cursor), min(wb, b)
            if hi <= lo:
                continue
            if lo > cursor:
                out.append((cursor, lo, rate, compressed))
            out.append((lo, hi, max(rate, CUT_SPEED), False))
            cursor = hi
        if b > cursor:
            out.append((cursor, b, rate, compressed))
    return out


def _relax_pauses(segments: list[_Segment], speed: float, fill_to: float) -> list[_Segment]:
    """*segments* lasting up to *fill_to* output seconds: their compressed pauses played slower.

    Each pause gives back the same share of what slowing it to
    :data:`RELAXED_PAUSE_SPEED` would, so the actions keep their real time and
    a pause still reads compressed; what this cannot fill is left to the hold.
    """

    def floor(rate: float) -> float:
        return min(rate, RELAXED_PAUSE_SPEED)

    played = sum((b - a) / (rate * speed) for a, b, rate, _ in segments if b > a)
    room = sum((b - a) / speed * (1 / floor(rate) - 1 / rate) for a, b, rate, compressed in segments if compressed and b > a)
    if fill_to <= played + 1e-6 or room <= 1e-6:
        return segments
    share = min(1.0, (fill_to - played) / room)
    return [
        (a, b, 1 / (1 / rate + share * (1 / floor(rate) - 1 / rate)) if compressed else rate, compressed)
        for a, b, rate, compressed in segments
    ]


def build_time_map(
    *,
    duration: float,
    trim_start: float,
    trim_end: float | None,
    speed: float,
    idle_mode: str,
    idle_threshold: float,
    idle_speed: float,
    events: list[dict],
    fill_to: float = 0.0,
) -> list[tuple[float, float]]:
    """``[(output t, recording t), ...]`` from 0, for the kept part of a recording.

    Whatever the idle mode, the wait of a skipped optional step is cut: the
    browser looked for an element that never came, and nothing happened.
    *fill_to* is how long the scene needs it to last (its narration): the
    pauses ``compress`` sped up play slower first, never below
    :data:`RELAXED_PAUSE_SPEED`, so the recording is not rushed to then
    stop on its last frame. A ``cut`` pause stays a jump cut: the author asked
    for one.
    """
    start = max(0.0, min(trim_start, duration))
    end = duration if trim_end is None else max(start, min(trim_end, duration))
    if end <= start:
        return [(0.0, start), (0.0, start)]
    segments: list[_Segment] = []
    if idle_mode == "off" or not events:
        segments.append((start, end, 1.0, False))
    else:
        active = [(max(a, start), min(b, end)) for a, b in _event_active_windows(events) if b > start and a < end]
        cursor = start
        fast = CUT_SPEED if idle_mode == "cut" else idle_speed
        for a, b in [*active, (end, end)]:
            gap = a - cursor
            if gap > idle_threshold:
                edge = min(IDLE_EDGE, gap / 4)
                segments.append((cursor, cursor + edge, 1.0, False))
                segments.append((cursor + edge, a - edge, fast, idle_mode != "cut"))
                segments.append((a - edge, a, 1.0, False))
            elif gap > 0:
                segments.append((cursor, a, 1.0, False))
            if b > a:
                segments.append((a, b, 1.0, False))
            cursor = max(cursor, b)
    skipped = [(max(a, start), min(b, end)) for a, b in _skipped_windows(events) if b > start and a < end]
    segments = _relax_pauses(_cut_windows(segments, skipped), speed, fill_to)
    points: list[tuple[float, float]] = [(0.0, start)]
    out = 0.0
    for a, b, rate, _compressed in segments:
        if b <= a:
            continue
        out += (b - a) / (rate * speed)
        points.append((round(out, 6), round(b, 6)))
    return points


def with_hold(points: list[tuple[float, float]], hold: float) -> list[tuple[float, float]]:
    """The map extended by *hold* output seconds on its last recording frame."""
    if hold <= 0 or not points:
        return points
    last_out, last_src = points[-1]
    return [*points, (round(last_out + hold, 6), last_src)]


def filled_length(source: float, speed: float, need: float, *, stretch: bool = True) -> float:
    """The output seconds *source* seconds of footage at *speed* play in a scene that needs *need*.

    Its own length at its speed, or - when the scene needs longer and the
    footage may be slowed (*stretch*: never a shot whose own sound is heard,
    it would be slowed too) - up to :data:`SLOWEST_FILL` of its speed, to
    fill the scene rather than stop on its last frame.
    """
    played = max(0.1, source / speed)
    if not stretch or need <= played:
        return played
    return min(need, max(0.1, source / (speed * SLOWEST_FILL)))


#: The speeds a recording is said to play at when it is made: what each carries under a narration.
CANDIDATE_SPEEDS = (1.0, 0.75, 0.5)
#: The slowest a scene's footage plays (``spec``: ``speed`` from 0.25).
SLOWEST_SPEED = 0.25


def kept_end(take: TakeMedia, scene: Any = None) -> float | None:
    """Where a recording *scene* stops playing *take*: its ``trim_end``, and at most its ``end_hold_ms`` after
    the take's last step ended - the hold the recorder kept beyond cut; ``None``: to the take's end."""
    trim_end = getattr(scene, "trim_end", None)
    hold = getattr(scene, "end_hold_ms", None)
    ends = [float(e.get("t1") or e.get("ta") or e.get("t0") or 0.0) for e in take.events if e.get("ok", True)]
    if hold is None or not ends:
        return trim_end
    kept = max(ends) + hold / 1000
    if kept <= float(getattr(scene, "trim_start", 0.0) or 0.0):
        return trim_end
    return kept if trim_end is None else min(float(trim_end), kept)


def recording_seconds(take: TakeMedia, scene: Any = None, *, speed: float | None = None, fill_to: float = 0.0) -> float:
    """Output seconds the recording *take* plays in a recording *scene* (its trim, its pauses compressed as it says,
    the spec's defaults without one) at *speed* (the scene's own by default); *fill_to* gives compressed pauses back
    up to that length, as ``extend_mode: zoom`` does (:func:`build_time_map`)."""
    from app.services.video_studio.spec import IdleCompression

    idle = getattr(scene, "idle", None) or IdleCompression()
    points = build_time_map(
        duration=take.duration,
        trim_start=float(getattr(scene, "trim_start", 0.0) or 0.0),
        trim_end=kept_end(take, scene),
        speed=float(speed if speed is not None else getattr(scene, "speed", 1.0) or 1.0),
        idle_mode=idle.mode,
        idle_threshold=idle.threshold,
        idle_speed=idle.speed,
        events=take.events,
        fill_to=fill_to,
    )
    return float(points[-1][0])


def recording_lengths(take: TakeMedia) -> str:
    """What the recording *take* plays in a recording scene at each of :data:`CANDIDATE_SPEEDS`, and the
    narration each length carries at the reading pace: said when it is made, before any narration is written."""
    from app.services.video_studio.frame_freeze import FROZEN_SECONDS

    played = [(speed, recording_seconds(take, speed=speed)) for speed in CANDIDATE_SPEEDS]
    relaxed = recording_seconds(take, speed=1.0, fill_to=10 * take.duration + 60.0)
    lengths = ", ".join(f"{seconds:.1f} s at speed {speed:g}" for speed, seconds in played)
    words = " / ".join(str(max(0, int((seconds - 0.3) * WORDS_PER_SECOND))) for _speed, seconds in played)
    return (
        f"In a recording scene (its pauses compressed, the default) it plays {lengths}; extend_mode zoom (the default) "
        f"gives its pauses back up to {relaxed:.1f} s at speed 1. That carries about {words} words of narration at "
        f"the reading pace ({WORDS_PER_SECOND:g} a second): a longer narration has the scene drift in on its last "
        f"frame (zoom), or freeze it with extend_mode hold, refused at save past {FROZEN_SECONDS:g} s."
    )


def speed_to_fill(played_at: Any, need: float, *, current: float) -> float | None:
    """The fastest ``speed`` (two decimals, never above *current*) at which footage playing ``played_at(speed)``
    output seconds lasts *need*; ``None`` when even :data:`SLOWEST_SPEED` falls short."""
    if played_at(current) >= need:
        return current
    if played_at(SLOWEST_SPEED) + 1e-6 < need:
        return None
    low, high = SLOWEST_SPEED, current
    for _ in range(40):
        middle = (low + high) / 2
        if played_at(middle) >= need:
            low = middle
        else:
            high = middle
    return max(SLOWEST_SPEED, math.floor(low * 100) / 100)


# ── Camera ────────────────────────────────────────────────────────────────


@dataclass
class _Focus:
    """An action the camera may frame, in output seconds."""

    t_action: float
    t_end: float
    #: The element, normalised: left, top, right, bottom.
    box: tuple[float, float, float, float]
    zoom: float
    #: The step opened another page (its URL changed): the camera backs out to show it.
    changes_page: bool = False
    #: The step of the take it acts on (``TakeStep`` index), ``None`` for an event logged without one.
    step: int | None = None

    @property
    def area(self) -> float:
        """The share of the page its element covers."""
        return max(0.0, self.box[2] - self.box[0]) * max(0.0, self.box[3] - self.box[1])

    def leaves(self, hold: float) -> float:
        """When the camera may leave it: *hold* after the action, a moment only when it opened another page."""
        return self.t_end + (PAGE_HOLD if self.changes_page else hold)

    @property
    def middle(self) -> tuple[float, float]:
        return (self.box[0] + self.box[2]) / 2, (self.box[1] + self.box[3]) / 2

    def fill(self, zoom: float) -> float:
        """The share of the view its element takes at *zoom*, on its larger side."""
        return max(self.box[2] - self.box[0], self.box[3] - self.box[1]) * zoom


def _clamp_center(cx: float, cy: float, zoom: float) -> tuple[float, float]:
    half = 0.5 / max(zoom, 1.0)
    return min(max(cx, half), 1.0 - half), min(max(cy, half), 1.0 - half)


def _focus_zoom(box: dict, css_w: int, css_h: int, max_zoom: float, fill: float = FOCUS_FILL) -> float:
    bw = max(float(box.get("w") or 0.0) / max(css_w, 1), 0.01)
    bh = max(float(box.get("h") or 0.0) / max(css_h, 1), 0.01)
    zoom = min(fill / bw, fill / bh, max_zoom)
    return max(1.0, zoom)


def _zoom_seconds(seconds: float, zoom: float, start: float = 1.0) -> float:
    """A zoom from *start* to *zoom*: the pace's *seconds* from wide to :data:`REFERENCE_ZOOM`, longer the deeper."""
    depth = abs(math.log(max(zoom, 1.0)) - math.log(max(start, 1.0))) / math.log(REFERENCE_ZOOM)
    return seconds * min(max(math.sqrt(depth), 0.8), 1.4)


def _pan_seconds(seconds: float, distance: float) -> float:
    """A pan of *distance* view widths: the pace's *seconds* at :data:`REFERENCE_PAN`, longer the further."""
    return seconds * min(max(math.sqrt(distance / REFERENCE_PAN), 0.7), 1.5)


def _pan_distance(a: tuple[float, float], b: tuple[float, float], zoom: float) -> float:
    """How far the view travels from centre *a* to centre *b*, in view widths."""
    return math.hypot(b[0] - a[0], b[1] - a[1]) * zoom


def _in_view(box: tuple[float, float, float, float], center: tuple[float, float], zoom: float) -> bool:
    """Whether *box* lies inside the view at *center* and *zoom*, :data:`FOLLOW_MARGIN` in from each edge."""
    half = 0.5 / max(zoom, 1.0) * (1 - 2 * FOLLOW_MARGIN)
    return (
        center[0] - half <= box[0]
        and box[2] <= center[0] + half
        and center[1] - half <= box[1]
        and box[3] <= center[1] + half
    )


def _arc(distance: float, zoom: float) -> float:
    """How far a pan of *distance* view widths pulls back half-way, in log of zoom (0: a flat pan)."""
    if distance <= ARC_FROM:
        return 0.0
    return round(min(0.5 * math.log(max(zoom, 1.0)), 0.45 * (distance - ARC_FROM)), 4)


def _camera_targets(
    media: TakeMedia, time_map: list[tuple[float, float]], wanted: set[str], max_zoom: float
) -> tuple[list[_Focus], list[tuple[float, float]]]:
    """The actions the camera may frame, and the windows it stays wide over (page loads, scrolls), in output seconds.

    Only what the scene shows: an action trimmed away never moves the camera.
    """
    shown_from, shown_until = time_map[0][1], time_map[-1][1]
    w, h = max(media.css_width, 1), max(media.css_height, 1)
    focuses: list[_Focus] = []
    breaks: list[tuple[float, float]] = []

    def wide_over(start: float, end: float) -> None:
        if end >= shown_from and start <= shown_until:
            breaks.append((map_inverse(time_map, start), map_inverse(time_map, max(end, start))))

    url = None
    for event in media.events:
        if not event.get("ok", True):
            continue
        action = event.get("action")
        t0 = float(event.get("t0") or 0.0)
        ta = float(event.get("ta", t0))
        t1 = float(event.get("t1") or ta)
        before = event.get("url_before") or url  # a take recorded before it was logged: the previous step's
        changes_page = bool(before and event.get("url") and event["url"] != before)
        url = event.get("url") or url
        if scrolled := _scroll_window(event):
            wide_over(*scrolled)
        if action in _WIDE_ACTIONS:
            wide_over(t0, t1)
            continue
        box = event.get("box")
        forced = event.get("zoom")
        framed = bool(box) and forced is not False and (action in wanted or forced is True)
        zoom = 0.0
        if framed:
            fill = HIGHLIGHT_FILL if action == "highlight" else FOCUS_FILL
            zoom = _focus_zoom(box, media.css_width, media.css_height, max_zoom, fill)
        if not framed or zoom < MIN_USEFUL_ZOOM or not shown_from <= ta <= shown_until:
            if changes_page:
                wide_over(ta, t1)  # another page opens on a step the camera does not frame
            continue
        x, y = float(box["x"]), float(box["y"])
        left, top = min(max(x / w, 0.0), 1.0), min(max(y / h, 0.0), 1.0)
        right = min(max((x + float(box.get("w") or 0.0)) / w, left), 1.0)
        bottom = min(max((y + float(box.get("h") or 0.0)) / h, top), 1.0)
        t_action = map_inverse(time_map, ta)
        end = max(map_inverse(time_map, t1), t_action)
        step = event.get("step")
        focuses.append(
            _Focus(t_action, end, (left, top, right, bottom), zoom, changes_page, step if isinstance(step, int) else None)
        )
    focuses.sort(key=lambda f: f.t_action)
    breaks.sort()
    return focuses, breaks


@dataclass(frozen=True)
class CameraMove:
    """A shot the automatic camera moves in on: a zoom in or a re-frame on the next step, never the way back
    out, which ends the same shot."""

    #: Output seconds of the scene the move starts at.
    t: float
    #: The step of the take it frames, ``None`` for an event logged without one.
    step: int | None
    #: The share of the page the step's element covers.
    share: float


class _AutoCamera:
    """The automatic camera over the focused actions of one recording: it follows them, shot by shot.

    A keyframe is ``[t, cx, cy, zoom]``, plus the pull-back of the move that
    ends on it (:func:`_arc`) when that move travels far.
    """

    def __init__(self, pace: CameraPace, breaks: list[tuple[float, float]], out_duration: float, start_after: float):
        self.pace = pace
        self.breaks = breaks
        self.out_duration = out_duration
        self.frames: list[list[float]] = [[0.0, 0.5, 0.5, 1.0]]
        #: The earliest a zoom in may start: after the transition in and the opening wide moment, then
        #: after the last shot backed out.
        self.ready = max(start_after, pace.establish)
        #: The step the camera shows last and when its last move ends; ``None`` while it is wide.
        self.last: _Focus | None = None
        self.moved = 0.0
        #: Each shot it moved in on, in order.
        self.moves: list[CameraMove] = []

    def plan(self, focuses: list[_Focus]) -> list[list[float]]:
        for focus in focuses:
            if self.last is not None:
                page_moved = any(b0 < focus.t_action and b1 > self.last.t_end for b0, b1 in self.breaks)
                if page_moved or self.last.changes_page:
                    self._back_out(after_page_move=True)
                elif self._room_for_a_wide_moment(focus):
                    self._back_out(after_page_move=False)
                else:
                    self._follow(focus)
                    self._page_opened(focus)
                    continue
            self._go_in(focus)
            self._page_opened(focus)
        if self.last is not None:
            self._back_out(after_page_move=False)
        return self.frames

    def _page_opened(self, focus: _Focus) -> None:
        """No zoom in starts before the page *focus* opened is there, whether the camera framed it or not."""
        if focus.changes_page:
            self.ready = max(self.ready, focus.leaves(0.0))

    def _view(self) -> tuple[float, float, float]:
        cx, cy, zoom = self.frames[-1][1:4]
        return cx, cy, zoom

    def _hold_until(self, t: float) -> None:
        if t > self.frames[-1][0] + 1e-3:
            self.frames.append([round(t, 4), *self.frames[-1][1:4]])

    def _move_to(self, start: float, end: float, center: tuple[float, float], zoom: float, arc: float = 0.0) -> None:
        self._hold_until(start)
        frame = [round(end, 4), round(center[0], 5), round(center[1], 5), round(zoom, 4)]
        self.frames.append([*frame, arc] if arc else frame)

    def _room_for_a_wide_moment(self, focus: _Focus) -> bool:
        """Whether the camera can back out, stay wide :attr:`CameraPace.min_wide` and be back in for *focus*."""
        pace, zoom = self.pace, self._view()[2]
        leave = max(self.last.leaves(pace.hold), self.moved + pace.settle)
        cycle = _zoom_seconds(pace.zoom_out, zoom) + pace.min_wide + _zoom_seconds(pace.zoom_in, focus.zoom)
        return focus.t_action - pace.lead - leave >= cycle

    def _go_in(self, focus: _Focus) -> None:
        """Zoom in on *focus*, a little late when there is no room before it; wide when it would not be worth it."""
        pace = self.pace
        seconds = _zoom_seconds(pace.zoom_in, focus.zoom)
        page_loaded = (b1 for b0, b1 in self.breaks if b0 < focus.t_action)
        start = max([focus.t_action - pace.lead - seconds, self.ready, *page_loaded])
        arrival = start + seconds
        # In for long enough while the step is on screen, and back out before the page moves.
        until = focus.leaves(pace.hold)
        page_moves = [b0 for b0, _ in self.breaks if b0 > focus.t_action]
        if page_moves:
            until = min(until, page_moves[0] - _zoom_seconds(pace.zoom_out, focus.zoom))
        if min(until, self.out_duration) - arrival < pace.min_in:
            return
        self._move_to(start, arrival, _clamp_center(*focus.middle, focus.zoom), focus.zoom)
        self.moves.append(CameraMove(round(start, 4), focus.step, round(focus.area, 4)))
        self.last, self.moved = focus, arrival

    def _follow(self, focus: _Focus) -> None:
        """Re-frame on *focus* from the shot's current view, unless it is already well in view."""
        pace = self.pace
        cx, cy, zoom = self._view()
        target_zoom = zoom
        if focus.fill(zoom) > MAX_FILL or not zoom / REZOOM <= focus.zoom <= zoom * REZOOM:
            target_zoom = focus.zoom
        target = _clamp_center(*focus.middle, target_zoom)
        distance = _pan_distance((cx, cy), target, max(zoom, target_zoom))
        if target_zoom == zoom and distance < MIN_PAN and _in_view(focus.box, (cx, cy), zoom):
            self.last = focus
            return
        seconds = _pan_seconds(pace.pan, distance)
        if target_zoom != zoom:
            seconds = max(seconds, _zoom_seconds(pace.zoom_in, target_zoom, zoom))
        start = max(focus.t_action - pace.lead / 2 - seconds, self.moved + pace.settle, self.last.t_end + 0.15)
        if start + seconds > focus.leaves(pace.hold) - pace.min_in:
            # The step is gone before the camera would get there: it stays on the last one, and
            # backs out after this one when it opened another page.
            if focus.changes_page:
                self.last = focus
            return
        self._move_to(start, start + seconds, target, target_zoom, _arc(distance, min(zoom, target_zoom)))
        self.moves.append(CameraMove(round(start, 4), focus.step, round(focus.area, 4)))
        self.last, self.moved = focus, start + seconds

    def _back_out(self, *, after_page_move: bool) -> None:
        """Back out after the shot's last step, before the page moves; the scene may end on the shot instead."""
        pace, last = self.pace, self.last
        seconds = _zoom_seconds(pace.zoom_out, self._view()[2])
        leave = max(last.leaves(pace.hold), self.moved + pace.settle)
        page_moves = [b0 for b0, _ in self.breaks if b0 > last.t_action]
        if page_moves:
            leave = min(leave, page_moves[0] - seconds)
        leave = max(leave, last.t_end + 0.2, self.moved + pace.settle)
        self.last = None
        if leave + seconds > self.out_duration:
            if self.out_duration - seconds < max(last.leaves(pace.hold / 2), self.moved + pace.settle):
                self.ready = math.inf  # no room to come back out calmly: the scene ends on the shot
                return
            leave = self.out_duration - seconds
        self._move_to(leave, leave + seconds, (0.5, 0.5), 1.0)
        # After a page move, the new page shown wide is the wide moment: a beat, then back in.
        self.ready = max(self.ready, leave + seconds + (pace.settle if after_page_move else pace.min_wide))


def _manual_camera(
    keyframes: list[dict], time_map: list[tuple[float, float]], trim_start: float, speed: float, pace: CameraPace
) -> list[list[float]]:
    """Manual keyframes in output seconds.

    A move squeezed by a compressed pause keeps the length it was written
    with, up to a zoom in of *pace*: an idle compression never turns it into a jump.
    """
    frames: list[list[float]] = []
    written_before = 0.0
    for kf in sorted(keyframes, key=lambda k: k["t"]):
        written = float(kf["t"])
        t_out = map_inverse(time_map, trim_start + written)
        zoom = min(float(kf.get("zoom", 1.0)), 4.0)
        cx, cy = _clamp_center(float(kf.get("x", 0.5)), float(kf.get("y", 0.5)), zoom)
        if frames:
            last = frames[-1]
            moves = [cx, cy, zoom] != last[1:]
            length = min((written - written_before) / max(speed, 0.25), pace.zoom_in) if moves else 0.0
            t_out = max(t_out, last[0] + length)
        frames.append([round(t_out, 4), cx, cy, zoom])
        written_before = written
    return frames


def plan_camera(
    *,
    mode: str,
    max_zoom: float,
    zoom_on: list[str],
    keyframes: list[dict],
    media: TakeMedia,
    time_map: list[tuple[float, float]],
    trim_start: float,
    out_duration: float,
    pace: str = "calm",
    move_seconds: float | None = None,
    speed: float = 1.0,
    start_after: float = 0.0,
) -> list[list[float]]:
    """Camera keyframes ``[[t, cx, cy, zoom], ...]`` in output seconds of the scene.

    *pace* and *move_seconds* time the moves (:func:`camera_pace`); the
    automatic camera does not move before *start_after* (the transition into the scene).
    """
    return plan_camera_moves(
        mode=mode,
        max_zoom=max_zoom,
        zoom_on=zoom_on,
        keyframes=keyframes,
        media=media,
        time_map=time_map,
        trim_start=trim_start,
        out_duration=out_duration,
        pace=pace,
        move_seconds=move_seconds,
        speed=speed,
        start_after=start_after,
    )[0]


def plan_camera_moves(
    *,
    mode: str,
    max_zoom: float,
    zoom_on: list[str],
    keyframes: list[dict],
    media: TakeMedia,
    time_map: list[tuple[float, float]],
    trim_start: float,
    out_duration: float,
    pace: str = "calm",
    move_seconds: float | None = None,
    speed: float = 1.0,
    start_after: float = 0.0,
) -> tuple[list[list[float]], list[CameraMove]]:
    """:func:`plan_camera`'s keyframes and the shots the automatic camera moves in on; a static or a manual
    camera moves in on none."""
    base = [0.0, 0.5, 0.5, 1.0]
    if mode == "static" or max_zoom <= 1.0:
        return [base], []
    timing = camera_pace(pace, move_seconds)
    if mode == "manual":
        return _manual_camera(keyframes, time_map, trim_start, speed, timing) or [base], []
    wanted = set().union(*(_ZOOM_ACTIONS.get(name, set()) for name in zoom_on)) if zoom_on else set()
    focuses, breaks = _camera_targets(media, time_map, wanted, max_zoom)
    if not focuses:
        return [base], []
    camera = _AutoCamera(timing, breaks, out_duration, start_after)
    return camera.plan(focuses), camera.moves


def recording_camera(
    scene: Any,
    take: TakeMedia,
    time_map: list[tuple[float, float]],
    start_after: float,
    end_before: float = 0.0,
) -> tuple[list[list[float]], list[CameraMove]]:
    """The camera of a recording *scene* playing *take* on its planned *time_map* (its hold included), after a
    transition in of *start_after* seconds: what :func:`plan` draws, and the shots it moves in on.

    *end_before* is how long the transition out of the scene lasts: the automatic camera is back out before it
    starts, so the scene after never dissolves over a zoom out - two framings of one page blended, the page
    drawn twice (``camera_settled_before``)."""
    camera = scene.camera
    played = time_map[-1][0] if time_map else 0.0
    return plan_camera_moves(
        mode=camera.mode,
        max_zoom=camera.max_zoom,
        zoom_on=list(camera.zoom_on),
        keyframes=[kf.model_dump() for kf in camera.keyframes],
        media=take,
        time_map=time_map,
        trim_start=scene.trim_start,
        out_duration=camera_settled_before(played, end_before),
        pace=camera.pace,
        move_seconds=camera.move_seconds,
        speed=scene.speed,
        start_after=start_after,
    )


def camera_settled_before(length: float, end_before: float) -> float:
    """When the automatic camera of a scene *length* seconds long must be still: before the transition out of it
    (*end_before* seconds), never earlier than half the scene."""
    return max(length - max(end_before, 0.0), length / 2)


def transition_out_seconds(spec: VideoSpec, index: int, length: float) -> float:
    """How long the transition from scene *index* (*length* seconds) into the next one overlaps it, as :func:`plan`
    places it: 0 for the last scene or a cut."""
    if index + 1 >= len(spec.scenes):
        return 0.0
    scene, following = spec.scenes[index], spec.scenes[index + 1]
    # The run of picture-to-picture cuts only changes a dynamic montage of pictures, never a cut out of a recording.
    transition = _transition_of(following, spec, scene, length, 0)
    if transition["type"] == "cut":
        return 0.0
    return min(float(transition["duration"]), length * 0.45)


# ── Cursor, clicks, highlights, URLs ──────────────────────────────────────


def plan_cursor(
    media: TakeMedia, time_map: list[tuple[float, float]], out_duration: float
) -> tuple[list[list[float]], list[list[float]]]:
    """``(samples [[t, x, y], ...], clicks [[t, x, y], ...])`` normalised, in output seconds."""
    if not time_map:
        return [], []
    src_start, src_end = time_map[0][1], time_map[-1][1]
    w, h = max(media.css_width, 1), max(media.css_height, 1)
    samples: list[list[float]] = []
    last_t = -1.0
    previous: tuple[float, float, float] | None = None
    for t, x, y in media.cursor:
        if t < src_start:
            previous = (t, x, y)
            continue
        if t > src_end:
            break
        if previous is not None and not samples:
            samples.append([0.0, round(previous[1] / w, 5), round(previous[2] / h, 5)])
        t_out = map_inverse(time_map, t)
        # One sample per 1/90 s of output is more than any frame rate needs.
        if t_out - last_t < 1 / 90 and samples:
            samples[-1] = [round(t_out, 4), round(x / w, 5), round(y / h, 5)]
            continue
        samples.append([round(t_out, 4), round(x / w, 5), round(y / h, 5)])
        last_t = t_out
    if not samples and previous is not None:
        samples.append([0.0, round(previous[1] / w, 5), round(previous[2] / h, 5)])
    clicks: list[list[float]] = []
    for event in media.events:
        if event.get("action") not in _CLICK_ACTIONS or not event.get("ok", True):
            continue
        point = event.get("point")
        ta = float(event.get("ta", event.get("t0") or 0.0))
        if not point or ta < src_start or ta > src_end:
            continue
        t_out = map_inverse(time_map, ta)
        if 0 <= t_out <= out_duration:
            clicks.append([round(t_out, 4), round(float(point["x"]) / w, 5), round(float(point["y"]) / h, 5)])
    return samples, clicks


def _event_for_step(events: list[dict], step: int) -> dict | None:
    for event in events:
        if event.get("step") == step:
            return event
    return None


def _normalised_box(box: dict | None, media: TakeMedia) -> list[float] | None:
    if not box:
        return None
    w, h = max(media.css_width, 1), max(media.css_height, 1)
    return [
        round(float(box["x"]) / w, 5),
        round(float(box["y"]) / h, 5),
        round(float(box["w"]) / w, 5),
        round(float(box["h"]) / h, 5),
    ]


def plan_url_track(media: TakeMedia, time_map: list[tuple[float, float]]) -> list[list[Any]]:
    track: list[list[Any]] = []
    for event in media.events:
        url = event.get("url")
        if not url:
            continue
        t_out = max(0.0, map_inverse(time_map, float(event.get("t1") or event.get("t0") or 0.0)))
        if track and track[-1][1] == url:
            continue
        track.append([round(t_out, 4), str(url)[:300]])
    if track:
        track[0][0] = 0.0
    return track


def drawn_frame(frame: Any) -> dict:
    """How a recording scene's window is drawn: its ``frame``, or with ``fill`` edge to edge - no margin, window
    bar, corner or halo, the page scaled to cover the whole frame and the captions drawn over it
    (``compositor_page.buildRecording``, #10867)."""
    drawn = frame.model_dump()
    if drawn.get("fill"):
        drawn.update(style="window", url_bar=False, padding=0.0, radius=0, shadow=False)
    return drawn


def slowed_sparse(take: TakeMedia, time_map: list[tuple[float, float]]) -> list:
    """The stretches *take* painted under its rate (:attr:`TakeMedia.sparse`) that a scene playing it through
    *time_map* shows slowed - played under speed 1 -, where the compositor shows the painted instant nearest each
    output frame (never two blended); none when it never is."""
    if not take.sparse:
        return []
    slowed = any(
        b1 > b0 and (b1 - b0) < 0.999 * (a1 - a0) for (a0, b0), (a1, b1) in itertools.pairwise(time_map) if a1 > a0
    )
    if not slowed:
        return []
    shown_from, shown_until = time_map[0][1], time_map[-1][1]
    return [span for span in take.sparse if float(span[1]) >= shown_from and float(span[0]) <= shown_until]


# ── Narration and captions ────────────────────────────────────────────────


def narration_speed(scene: Any, spec: VideoSpec) -> float:
    """How fast *scene*'s narration is said: its own ``narration_speed``, else the voice-over's."""
    own = getattr(scene, "narration_speed", None)
    return float(own) if own is not None else float(spec.voiceover.speed)


def estimate_narration_seconds(text: str | None, speed: float = 1.0) -> float:
    words = len(re.findall(r"\w+", text or ""))
    if not words:
        return 0.0
    return words / (WORDS_PER_SECOND * max(speed, 0.5)) + 0.3


def caption_phrases(text: str) -> list[str]:
    """The narration cut into readable phrases of at most :data:`CAPTION_MAX_CHARS`."""
    clean = re.sub(r"\s+", " ", text or "").strip()
    if not clean:
        return []
    sentences = re.split(r"(?<=[.!?;:])\s+", clean)
    phrases: list[str] = []
    for sentence in sentences:
        if len(sentence) <= CAPTION_MAX_CHARS:
            phrases.append(sentence)
            continue
        current = ""
        for part in re.split(r"(?<=,)\s+|\s+(?=(?:and|but|or|so|then|with|which|while)\s)", sentence):
            for word in part.split(" "):
                candidate = f"{current} {word}".strip()
                if len(candidate) > CAPTION_MAX_CHARS and current:
                    phrases.append(current)
                    current = word
                else:
                    current = candidate
            if len(current) > CAPTION_MAX_CHARS * 0.6:
                phrases.append(current)
                current = ""
        if current:
            phrases.append(current)
    return [p for p in phrases if p]


def caption_cues(text: str, start: float, duration: float) -> list[list[Any]]:
    """``[[t0, t1, phrase], ...]`` spread over the voice, in proportion to their length."""
    phrases = caption_phrases(text)
    if not phrases or duration <= 0:
        return []
    weights = [len(p) + 8 for p in phrases]
    total = sum(weights)
    cues: list[list[Any]] = []
    t = start
    for phrase, weight in zip(phrases, weights, strict=True):
        span = duration * weight / total
        cues.append([round(t, 3), round(t + span, 3), phrase])
        t += span
    return cues


# ── Footage that ends before its scene ────────────────────────────

#: Output seconds a scene must outlast its footage by before its ``extend_mode`` plays.
EXTEND_MIN = 0.05
#: ``loop``: the source seconds looped at the footage's end, and the crossfade between two loops (output seconds).
EXTEND_LOOP_SECONDS = 1.0
EXTEND_LOOP_FADE = 0.3


def extension(
    mode: str,
    *,
    played: float,
    length: float,
    src_from: float,
    src_to: float,
    speed: float = 1.0,
    still: float = 0.0,
) -> dict | None:
    """How a scene plays from its footage's end (*played*, output seconds) to its own (*length*).

    ``None`` for ``zoom``, the default: the planner slowed the footage to
    fill the scene (:func:`filled_length`) and the page drifts in on
    whatever still holds (``heldSpan``). ``{"mode": "hold"}``: the last frame
    stays still, no drift. ``loop``: ``{from, to, period, fade, src_from,
    speed}`` - the last ``period`` output seconds of the source (from
    ``src_from``) again and again from ``from`` to ``to``, each crossfaded
    over ``fade`` into the next; the *still* seconds before the scene's end
    (its ``hold_end``, the author's still beat) keep the frame ``to`` shows.
    *src_from* / *src_to* are the source seconds the scene plays.
    """
    if mode == "zoom" or length - played < EXTEND_MIN:
        return None
    if mode == "loop":
        source = min(EXTEND_LOOP_SECONDS, max(0.0, src_to - src_from))
        period = source / max(speed, 1e-6)
        if period >= 0.3:
            fade = min(EXTEND_LOOP_FADE, period / 3, played)
            return {
                "mode": "loop",
                "from": round(played, 4),
                "to": round(max(played, length - still), 4),
                "period": round(period, 4),
                "fade": round(fade, 4),
                "src_from": round(src_to - source, 4),
                "speed": round(speed, 4),
            }
        # A clip too short to loop drifts in on its last frame, as ``zoom`` does.
        return None
    return {"mode": "hold"}


def loop_frames(document: dict) -> dict[str, tuple[int, int]]:
    """``source key -> (first, last)`` source frames each looping scene plays again (the frame server keeps them)."""
    kept: dict[str, tuple[int, int]] = {}
    for scene in document.get("scenes") or []:
        extend = scene.get("extend") or {}
        if extend.get("mode") != "loop" or not isinstance(scene.get("src"), str):
            continue
        fps = float(scene.get("src_fps") or 24.0)
        first = math.floor(float(extend["src_from"]) * fps)
        last = math.ceil((float(extend["src_from"]) + float(extend["period"]) * float(extend["speed"])) * fps)
        kept[scene["src"]] = (max(0, first), max(first, last))
    return kept


# ── The plan ──────────────────────────────────────────────────────────────

#: The shortest fade of a clip's own sound in and out, in seconds: no click on a cut.
CLIP_SOUND_FADE = 0.15


class ClipSound(NamedTuple):
    """A clip scene whose own sound plays: where, from which second of the clip, how loud."""

    start: float
    scene_id: str
    trim_start: float
    speed: float
    volume: float
    #: Output seconds of it that play.
    duration: float
    #: Over the transition into the scene, and out of it into the next.
    fade_in: float = CLIP_SOUND_FADE
    fade_out: float = 0.3


@dataclass
class TimelinePlan:
    """The compositor's document plus what the audio mix needs."""

    document: dict
    duration: float
    #: ``scene id -> absolute start of its narration``.
    narration_starts: dict[str, float]
    #: The clip scenes whose own sound plays (:func:`.spec.clip_sound_choice`).
    clip_audio: list[ClipSound]
    notes: list[str] = field(default_factory=list)
    #: ``scene id -> (start, end)`` of each narration, its measured (else
    #: paced) length: where the music bed goes down.
    narration_spans: dict[str, tuple[float, float]] = field(default_factory=dict)
    #: ``scene id -> (plays, reason)`` of every clip scene's own sound: the
    #: reason of :func:`.spec.clip_sound_choice`, or ``silent`` for a clip with
    #: no sound at all.
    clip_sound: dict[str, tuple[bool, str]] = field(default_factory=dict)
    #: ``scene id -> [[t0, t1, phrase], ...]``: the caption cues of its narration
    #: (:func:`caption_cues`, absolute), the subtitles shown while it is said.
    captions: dict[str, list[list[Any]]] = field(default_factory=dict)

    def voice_windows(self, *, voiced: set[str] | None = None) -> list[tuple[float, float]]:
        """Where someone speaks: ``[(start, end)]``, in time order.

        *voiced* keeps the narrations actually spoken (a render whose voice
        failed for a scene does not duck anything under it).
        """
        return sorted(span for sid, span in self.narration_spans.items() if voiced is None or sid in voiced)

    def duck_windows(self, *, voiced: set[str] | None = None) -> list[tuple[float, float]]:
        """Where the music goes down: someone speaks, or a clip's own sound plays."""
        spans = self.voice_windows(voiced=voiced)
        spans.extend((sound.start, sound.start + sound.duration) for sound in self.clip_audio)
        return sorted(spans)

    def cues(self):
        """The scenes a built-in score plays under: where each starts, what it is, whether it is narrated."""
        from app.services.video_studio.score import Cue

        return [
            Cue(float(scene["start"]), str(scene["type"]), (scene.get("id") or "") in self.narration_spans)
            for scene in self.document.get("scenes") or []
        ]


#: Each type's own length, in seconds, when a transition names none.
TRANSITION_SECONDS = {
    "cut": 0.0,
    "crossfade": 0.6,
    "fade_black": 0.8,
    "slide_left": 0.55,
    "slide_up": 0.55,
    "zoom": 0.6,
    "blur": 0.55,
    "wipe": 0.6,
    "whip": 0.35,
    "flash": 0.4,
}

#: What a scene shows, for the automatic transitions: a card, words, moving
#: pictures (an AI shot, a video, an image or a slide in motion) or a screen.
_SCENE_ROLE = {
    "title": "card",
    "outro": "card",
    "statement": "words",
    "motion": "words",
    "clip": "picture",
    "image": "picture",
    "slide": "picture",
    "figure": "picture",
    "recording": "screen",
}

#: ``(from, to) -> {pace: (type, seconds)}``: how an editor cuts from one kind
#: of scene to the next. Cuts into and out of cards are decided before.
_AUTO_GRAMMAR: dict[tuple[str, str], dict[str, tuple[str, float]]] = {
    ("words", "picture"): {"calm": ("blur", 0.6), "dynamic": ("zoom", 0.5)},
    ("words", "screen"): {"calm": ("blur", 0.55), "dynamic": ("zoom", 0.5)},
    ("words", "words"): {"calm": ("crossfade", 0.45), "dynamic": ("whip", 0.35)},
    ("picture", "words"): {"calm": ("blur", 0.5), "dynamic": ("flash", 0.4)},
    ("screen", "words"): {"calm": ("blur", 0.5), "dynamic": ("flash", 0.4)},
    ("picture", "picture"): {"calm": ("crossfade", 0.7), "dynamic": ("zoom", 0.5)},
    ("screen", "screen"): {"calm": ("crossfade", 0.45), "dynamic": ("slide_left", 0.45)},
    ("picture", "screen"): {"calm": ("crossfade", 0.5), "dynamic": ("zoom", 0.5)},
    ("screen", "picture"): {"calm": ("crossfade", 0.5), "dynamic": ("zoom", 0.5)},
}
#: A dynamic run of pictures alternates its push with a whip, so a montage of
#: AI shots keeps its rhythm without repeating one move.
_DYNAMIC_PICTURE_RUN = (("zoom", 0.5), ("whip", 0.35))
#: A scene shorter than this (seconds) is cut from: no time to dissolve.
_MIN_DISSOLVE_SCENE = 1.6
#: What the ``classic`` pace keeps of the grammar: a brand film's cuts.
CLASSIC_TRANSITIONS = frozenset({"cut", "crossfade", "fade_black"})


def auto_transition(previous: Any, scene: Any, previous_length: float, pace: str, run: int = 0) -> tuple[str, float]:
    """The transition an editor would cut from *previous* into *scene* with: ``(type, seconds)``.

    *run* counts the picture-to-picture cuts just before this one, so a
    dynamic montage alternates its moves. ``classic`` is the calm grammar
    with every move (a blur, a zoom) replaced by a crossfade of its length.
    """
    if pace == "classic":
        kind, seconds = auto_transition(previous, scene, previous_length, "calm", run)
        return (kind, seconds) if kind in CLASSIC_TRANSITIONS else ("crossfade", seconds)
    if previous_length < _MIN_DISSOLVE_SCENE:
        return "cut", 0.0
    before, after = _SCENE_ROLE.get(previous.type, "picture"), _SCENE_ROLE.get(scene.type, "picture")
    if scene.type == "outro":
        return ("fade_black", 0.8) if pace == "calm" else ("fade_black", 0.6)
    if previous.type == "title":
        return "zoom", 0.7
    if after == "card":
        return "fade_black", 0.6
    if pace == "dynamic" and (before, after) == ("picture", "picture"):
        return _DYNAMIC_PICTURE_RUN[run % len(_DYNAMIC_PICTURE_RUN)]
    return _AUTO_GRAMMAR.get((before, after), {}).get(pace, ("crossfade", 0.6))


def cut_on_frame(seconds: float, fps: float) -> float:
    """The second of the last output frame at or before *seconds*: where a cut joins two scenes, so that a cut always
    falls on the frame grid (a fraction of a frame at the join reached the encoded frame beside it as a mix of the two
    pictures). Never later: the scene before never plays past what it planned (its inserts, its narration), the
    remainder - under one frame - is carried by the scene coming in."""
    rate = max(float(fps), 1.0)
    return math.floor(seconds * rate + 1e-6) / rate


def _transition_of(scene: Any, spec: VideoSpec, previous: Any, previous_length: float, run: int) -> dict:
    tr = scene.transition or spec.default_transition
    if tr.type == "auto":
        if previous is None:
            return {"type": "cut", "duration": 0.0}
        kind, seconds = auto_transition(previous, scene, previous_length, tr.pace, run)
        if tr.duration is not None and kind != "cut":
            seconds = tr.duration
        return {"type": kind, "duration": float(seconds)}
    return {"type": tr.type, "duration": float(tr.duration if tr.duration is not None else TRANSITION_SECONDS[tr.type])}


def _overlay_payload(
    overlay: Any, *, start: float, end: float, box: list[float] | None, frame_width: int = 1920, picture: Any = None
) -> dict:
    payload = {
        "type": overlay.type,
        "text": overlay.text,
        "subtext": overlay.subtext,
        "t0": round(start, 4),
        "t1": round(end, 4),
        "position": overlay.position,
        "style": overlay.style,
        "box": box,
    }
    if overlay.type == "image":
        # Sized here, in design pixels, so the page places it like a badge before its picture has loaded.
        asked = overlay.width if overlay.width is not None else IMAGE_OVERLAY_WIDTH
        width = asked * frame_width if asked <= 1 else asked
        aspect = picture.height / picture.width if picture is not None and picture.width and picture.height else 1.0
        payload.update(
            {
                "src": overlay_image_key(overlay),
                "w": round(width, 1),
                "h": round(width * aspect, 1),
                "opacity": overlay.opacity if overlay.opacity is not None else 1.0,
            }
        )
        if overlay.asset in BRAND_ASSETS:
            # A brand mark of the template: the quality check counts it with the watermark and the cards' logos.
            payload["brand"] = overlay.asset
    return payload


def insert_light(overlay: Any) -> float:
    """How much of the footage's light plays over a screen insert: its ``light``, else ``INSERT_DEFAULT_LIGHT``."""
    light = getattr(overlay, "light", None)
    return float(INSERT_DEFAULT_LIGHT if light is None else light)


def insert_zoom(overlay: Any) -> dict | None:
    """A screen insert's camera push as the page and the frame server read it: ``{start, end, scale, focus}``
    in seconds after the insert comes in; ``None`` for none."""
    zoom = getattr(overlay, "zoom", None)
    if zoom is None:
        return None
    return {
        "start": round(float(zoom.start), 4),
        "end": round(float(zoom.end), 4),
        "scale": round(float(zoom.scale), 4),
        "focus": [round(float(zoom.focus[0]), 4), round(float(zoom.focus[1]), 4)],
    }


def insert_despill(overlay: Any) -> dict | None:
    """A screen insert's edge despill as the frame server and the preview's build read it: ``{edge, width_px,
    strength, own_hues?}`` - the colours its subject holds when it declares any; ``None`` when it names none
    (the compositor's default, ``screen_insert.Despill``)."""
    despill = getattr(overlay, "despill", None)
    if despill is None:
        return None
    read = {"edge": bool(despill.edge), "width_px": round(float(despill.width_px), 3), "strength": round(float(despill.strength), 3)}
    return {**read, "own_hues": list(despill.own_hues)} if despill.own_hues else read


def _insert_payload(
    part: InsertPart, key: str, source: ClipMedia | ImageMedia, *, start: float, end: float, length: float
) -> dict:
    """A screen insert as the page draws it: its surface, its fit, glow and light, its push, a
    video's time map.

    The time map is in seconds of the scene: the source plays from its
    ``trim_start`` when the insert comes in and holds its last frame. A pane
    of a wall is drawn as an insert of its own, on its own quad. One
    that runs to its scene's *length* (``to_end``) stays to the last
    frame its scene is painted on, however long that is once planned
    (:func:`run_to_end`).
    """
    overlay = part.overlay
    payload: dict[str, Any] = {
        "src": key,
        "quad": [[round(float(x), 6), round(float(y), 6)] for x, y in part.quad],
        "fit": overlay.fit or "cover",
        "glow": float(overlay.glow or 0.0),
        "light": insert_light(overlay),
        "t0": round(start, 4),
        "t1": round(end, 4),
    }
    if end >= length - HELD_END_SLACK:
        payload["to_end"] = True
    if getattr(overlay, "key", None) is not None:
        # Held on its surface over its whole scene (:func:`hold_surfaces`): its key never shows raw.
        payload["keyed"] = True
    if (zoom := insert_zoom(overlay)) is not None:
        payload["zoom"] = zoom
    if isinstance(source, ImageMedia):
        return {**payload, "kind": "image", "src_w": source.width, "src_h": source.height}
    trim = min(float(overlay.trim_start or 0.0), max(source.duration - 0.1, 0.0))
    played = max(0.1, min(end - start, source.duration - trim))
    return {
        **payload,
        "kind": "video",
        "time_map": [
            [round(start, 4), round(trim, 4)],
            [round(start + played, 4), round(trim + played, 4)],
            [round(max(end, start + played), 4), round(trim + played, 4)],
        ],
        "src_fps": source.fps,
        "src_frames": source.frame_count,
        "src_w": source.width,
        "src_h": source.height,
    }


#: A layer held to its scene's end is held on its last frame however its length was rounded: the page's.
HELD_END_SLACK = 0.001


def _quad_bounds(quad: Any) -> tuple[float, float, float, float]:
    xs, ys = [float(point[0]) for point in quad], [float(point[1]) for point in quad]
    return min(xs), min(ys), max(xs), max(ys)


def _bounds_meet(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def run_to_end(inserts: list[dict], length: float) -> None:
    """Each screen insert of a scene that runs to its end (``to_end``) shown to its scene's *length*: the
    seconds of the scene the page paints its footage for, once planned - the music's beat moving the cut, a hold, a
    zoom or a loop - however long it was when the insert was planned. ``t1`` and a video's last held frame move there;
    it never fades out (:func:`insert_visibility`)."""
    for payload in inserts:
        if not payload.get("to_end"):
            continue
        payload["t1"] = round(max(float(payload.get("t1") or 0.0), length), 4)
        time_map = payload.get("time_map")
        if time_map and float(time_map[-1][0]) < payload["t1"]:
            time_map[-1][0] = payload["t1"]


def hold_surfaces(inserts: list[dict], length: float) -> None:
    """Each keyed screen insert of a scene held on its surface: ``held``, the scene seconds its layer is drawn.

    The scene's painted *length* is the one clock: an insert that runs to its end is shown to it first
    (:func:`run_to_end`), so its ``t1`` and its ``held`` end are that same second.

    A keyed surface is never shown raw - before its insert comes in, after it goes, through its fades and through
    both of its scene's transitions: the frame server draws the layer on every frame it is held, the surface in the
    bezel's colour where the insert does not show (:func:`insert_visibility`), and a transition blends the scene's
    finished frame, never the insert alone. The keyed inserts whose quads overlap share one surface (``surface``: the
    index of its first insert in the scene) and hand it over: the first to come in holds it from the scene's start,
    every other one from its own ``t0``, each until the next one comes in or it goes, whichever is later, the last one
    to the scene's end (*length*). A wall's panes never overlap: each holds its own.
    """
    run_to_end(inserts, length)
    keyed = [index for index, payload in enumerate(inserts) if payload.get("keyed")]
    surfaces: list[list[int]] = []
    for index in keyed:
        bounds = _quad_bounds(inserts[index]["quad"])
        meeting = [s for s in surfaces if any(_bounds_meet(bounds, _quad_bounds(inserts[other]["quad"])) for other in s)]
        surfaces = [s for s in surfaces if s not in meeting] + [[index, *(other for s in meeting for other in s)]]
    for surface in surfaces:
        order = sorted(surface, key=lambda index: (float(inserts[index]["t0"]), index))
        for rank, index in enumerate(order):
            payload = inserts[index]
            begin = 0.0 if rank == 0 else float(payload["t0"])
            end = length if rank == len(order) - 1 else max(float(payload["t1"]), float(inserts[order[rank + 1]]["t0"]))
            payload["held"] = [round(begin, 4), round(end, 4)]
            payload["surface"] = min(surface)


def insert_visibility(payload: dict, t: float) -> float:
    """How much of a screen insert shows *t* seconds into its scene: none outside ``[t0, t1)``, coming in
    over :data:`INSERT_FADE_SECONDS` after a ``t0`` past the scene's start and going over as long before ``t1``. One
    that runs to its scene's end (``to_end``) never goes: whole from its fade in to the last frame its scene is
    painted on - the end fade is an insert's that ends inside its scene.

    The page's ``insertShown`` (``compositor_page.buildInserts``): an unkeyed insert is faded by it over its footage, a
    keyed one held on its surface (:func:`hold_surfaces`) is composed at it over the bezel's colour by the frame server.
    """
    t0, t1 = float(payload.get("t0") or 0.0), float(payload.get("t1") or 0.0)
    to_end = bool(payload.get("to_end"))
    if t < t0 or (t >= t1 and not to_end):
        return 0.0
    fade = INSERT_FADE_SECONDS
    rise = min(max((t - t0) / fade, 0.0), 1.0) if t0 > 0 else 1.0
    if to_end:
        return rise
    return min(rise, 1.0 - min(max((t - (t1 - fade)) / fade, 0.0), 1.0))


def painted_end(payload: dict) -> float:
    """The second of its scene past which no frame may paint the scene's footage under a screen insert that runs to the
    scene's end: its ``held`` end when held on its surface, else its ``t1`` - both the scene's painted length
    (:func:`run_to_end`). The page fails a frame painted at or past it, the frame server a layer asked on a frame its
    scene does not own (:func:`whole_frames`) - both read by ``insert_checks.overruns``."""
    held = payload.get("held")
    if isinstance(held, list | tuple) and len(held) == 2:
        return float(held[1])
    return float(payload.get("t1") or 0.0)


def whole_frames(document: dict, owners: FrameOwners | None = None) -> dict[str, tuple[int, int]]:
    """``insert key -> [first, end)`` output frames of a planned *document* where a screen insert that runs to its
    scene's end shows whole: from its fade in done (:func:`insert_visibility` at 1, on the frame's own time)
    to its scene's last encoded frame, inclusive (*owners*: ``frame_owners.FrameOwners``, the document's by
    default) - never a frame the next scene owns. The frame server fails a layer asked there partly shown, or asked
    at or past *end*."""
    owners = owners if owners is not None else FrameOwners.of(document)
    out: dict[str, tuple[int, int]] = {}
    for index, scene in enumerate(scene for scene in document.get("scenes") or [] if isinstance(scene, dict)):
        start = float(scene.get("start") or 0.0)
        frames = owners.frames_of(index)
        for payload in scene.get("inserts") or []:
            if not isinstance(payload, dict) or not payload.get("to_end") or not payload.get("src"):
                continue
            first = next((n for n in frames if insert_visibility(payload, n / owners.fps - start) >= 1.0), frames.stop)
            out[str(payload["src"])] = (first, frames.stop)
    return out


def insert_held(payload: dict, t: float, length: float) -> bool:
    """Whether a keyed screen insert is held on its surface *t* seconds into its scene of *length*: within its
    ``held`` span, the one held to the scene's end on its last frame too (:data:`HELD_END_SLACK`). The page's ``heldAt``."""
    held = payload.get("held")
    if not isinstance(held, list | tuple) or len(held) != 2:
        return False
    until = math.inf if float(held[1]) >= length - HELD_END_SLACK else float(held[1])
    return float(held[0]) <= t < until


def insert_drawn(payload: dict) -> tuple[float, float]:
    """``(from, to)`` in its scene's seconds where the page draws a screen insert's layer: a keyed insert's
    ``held`` span (:func:`hold_surfaces`), else where it shows (``t0``, ``t1``)."""
    held = payload.get("held")
    if isinstance(held, list | tuple) and len(held) == 2:
        return float(held[0]), float(held[1])
    return float(payload.get("t0") or 0.0), float(payload.get("t1") or 0.0)


def insert_layers(scene: dict, t: float) -> list[tuple[dict, float, float | None]]:
    """``(payload, opacity, visibility)`` of each screen insert of a planned *scene* *t* seconds into it, in the order the
    page stacks them: what the page's ``buildInserts`` draws.

    A keyed insert held there (:func:`insert_held`) with no other one held beneath it on its surface is drawn whole
    (opacity 1), the frame server composing how much of it shows over the bezel's colour (*visibility*,
    :func:`insert_visibility`); every other insert is drawn at that share, *visibility* ``None``: an unkeyed one over
    its footage, a keyed one over the insert held beneath it, which covers the key.
    """
    inserts = [payload for payload in scene.get("inserts") or [] if isinstance(payload, dict)]
    length = float(scene.get("end") or 0.0) - float(scene.get("start") or 0.0)
    held = [insert_held(payload, t, length) for payload in inserts]
    out: list[tuple[dict, float, float | None]] = []
    for index, payload in enumerate(inserts):
        shown = insert_visibility(payload, t)
        beneath = any(held[other] and inserts[other].get("surface") == payload.get("surface") for other in range(index))
        out.append((payload, 1.0, shown) if held[index] and not beneath else (payload, shown, None))
    return out


def _inserts_of(scene: Any, media: PlanMedia, notes: list[str]) -> list[tuple[str, InsertPart]]:
    """``(key, part)`` of the screen inserts of *scene* the planner draws, pane by pane; one whose source is missing is said.

    Planned only where their sources were gathered (a render, a preview):
    an estimate of the timeline has no use for them.
    """
    if not media.inserts_gathered:
        return []
    for index, overlay in enumerate(scene.overlays):
        if overlay.auto_quad:
            notes.append(
                f"Scene {scene.id}: {insert_label(insert_key(scene.id, index))} is drawn once a render finds its quad, "
                "the bezel on its first footage frame (quad: auto)."
            )
    kept = []
    for key, part in _insert_parts(scene):
        if key not in media.inserts:
            notes.append(f"Scene {scene.id}: {insert_label(key)} is left out, its source could not be read.")
            continue
        kept.append((key, part))
    return kept


def _overlays_of(scene: Any, media: PlanMedia, notes: list[str]) -> list[Any]:
    """The overlays of *scene* the planner draws: an image overlay whose picture was not found is left out, said.

    A screen insert is part of the scene's picture, never an overlay above it (:func:`_inserts_of`).
    """
    kept = []
    for overlay in scene.overlays:
        if overlay.type == "screen_insert":
            continue
        key = overlay_image_key(overlay)
        if key is not None and media.overlays_gathered and key not in media.overlays:
            what = f"the template's {overlay.asset}" if overlay.asset else f"file {overlay.file_id}"
            notes.append(f"Scene {scene.id}: its image overlay is left out, {what} could not be read.")
            continue
        kept.append(overlay)
    return kept


#: How much closer than its size a picture under screen inserts is seen at most - its scene's motion pushes in up to
#: 1.14x (``compositor_page.motionTransform``): its inserts are drawn at that size, a statement's backdrop and an image
#: scene's still alike.
STILL_HOST_ZOOM = 1.15


def insert_footage(scene: dict) -> dict | None:
    """The planned footage a planned *scene*'s screen inserts land on: a statement's backdrop, else the scene itself (a
    clip, a recording, an image scene's still or its animation); ``None`` for a statement with no backdrop drawn."""
    footage = scene.get("backdrop") if scene.get("type") == "statement" else scene
    return footage if isinstance(footage, dict) else None


def footage_still(footage: dict) -> bool:
    """Whether planned *footage* (:func:`insert_footage`) is a picture - a statement's picture backdrop, an image scene
    played as its still -: no frame to follow, its matte read on the picture, moving with its scene's motion."""
    return footage.get("kind") == "image" or footage.get("type") == "image"


def _backdrop_payload(scene: Any, backdrop: ClipMedia | ImageMedia | None, length: float) -> dict | None:
    """What plays behind a statement, or ``None`` (the brand backdrop): a video slowed to fill it, else held."""
    if scene.backdrop is None or backdrop is None:
        return None
    common = {"src": f"backdrop/{scene.id}", "dim": scene.dim}
    if isinstance(backdrop, ImageMedia):
        return {
            **common,
            "kind": "image",
            "motion": scene.motion,
            "image_w": backdrop.width,
            "image_h": backdrop.height,
        }
    start = min(scene.backdrop.trim_start, max(backdrop.duration - 0.1, 0.0))
    source = max(0.1, min(length, backdrop.duration - start))
    played = filled_length(source, 1.0, length)
    return {
        **common,
        "kind": "video",
        "time_map": [
            [0.0, round(start, 4)],
            [round(played, 4), round(start + source, 4)],
            [round(length, 4), round(start + source, 4)],
        ],
        "src_fps": backdrop.fps,
        "src_frames": backdrop.frame_count,
        "src_w": backdrop.width,
        "src_h": backdrop.height,
    }


def beat_extra(grid: BeatGrid | None, end: float) -> float:
    """How much longer a scene ending at *end* plays for its cut to land on the music's next beat.

    A scene only ever lengthens - its narration still fits - and by
    ``beats.MAX_SHIFT`` at most; past the grid's end nothing moves.
    """
    if grid is None:
        return 0.0
    beat = grid.next(end)
    if beat is None:
        return 0.0
    extra = beat - end
    return round(extra, 4) if 0.0 < extra <= MAX_SHIFT else 0.0


def _hold_longer(payload: dict, extra: float) -> None:
    """*payload* held *extra* seconds longer at its end: its footage's last frame (a statement's backdrop's too), its loop."""
    for holder in (payload, payload.get("backdrop") or {}):
        time_map = holder.get("time_map")
        if not time_map:
            continue
        last = time_map[-1]
        if len(time_map) >= 2 and time_map[-2][1] == last[1]:
            last[0] = round(last[0] + extra, 4)
        else:
            time_map.append([round(last[0] + extra, 4), last[1]])
    extend = payload.get("extend")
    if extend and extend.get("mode") == "loop":
        extend["to"] = round(extend["to"] + extra, 4)


def _motion_payload(
    scene: Any, spec: VideoSpec, media: PlanMedia, notes: list[str], *, theme: dict, size: tuple[int, int], length: float
) -> dict:
    """What a motion scene draws: its composition and elements laid out, each picture and Lottie it can play.

    A picture or a Lottie file the render could not read (or refused) is left
    out, said; with ``beat_sync: all`` each element comes in on the nearest beat.
    """
    tokens = theme.get("motion") or motion_design.resolve_motion(spec.theme.motion)
    drawn = motion_design.compose(scene, tokens, frame=motion_design.Frame(*size), theme=theme, length=length)
    kept = []
    for element in drawn["elements"]:
        if element["kind"] == "lottie" and media.lotties_gathered and element["lottie"]["src"] not in media.lotties:
            notes.append(f"Scene {scene.id}: its Lottie element {element['id']} is left out, its file could not be played.")
            continue
        src = (element.get("image") or {}).get("src")
        if src == "logo" and not theme.get("logo"):
            notes.append(f"Scene {scene.id}: its logo element {element['id']} is left out, the video has no logo.")
            continue
        if src and src != "logo" and media.overlays_gathered and src not in media.overlays:
            notes.append(f"Scene {scene.id}: its image element {element['id']} is left out, its picture could not be read.")
            continue
        kept.append(element)
    return {
        "elements": kept,
        "camera": drawn["camera"],
        "brands": any((element.get("image") or {}).get("brand") for element in kept),
        "reveal_end": motion_design.reveal_end(kept),
        "background": scene.background,
        "align": scene.align,
    }


def plan(spec: VideoSpec, media: PlanMedia, *, fps: int, theme: dict) -> TimelinePlan:
    """The frame-exact timeline of *spec* with its *media*."""
    width, height = design_size(spec.format.aspect_ratio)
    scenes_out: list[dict] = []
    narration_starts: dict[str, float] = {}
    narration_spans: dict[str, tuple[float, float]] = {}
    #: ``(index of the scene, its sound)``: the fade out is set once the next scene is placed.
    sounds: list[tuple[int, ClipSound]] = []
    clip_sound: dict[str, tuple[bool, str]] = {}
    cues: list[list[Any]] = []
    captions: dict[str, list[list[Any]]] = {}
    notes: list[str] = []
    cursor_t = 0.0  # end of the previous scene
    last_narration_end = 0.0
    voice_on = spec.voiceover.enabled
    # Whether the cuts (``cuts``), and also the builds (``all``), land on the music's beats.
    sync = spec.theme.motion.beat_sync if spec.theme.motion is not None and media.beats is not None else "off"

    picture_run = 0
    for index, scene in enumerate(spec.scenes):
        previous = spec.scenes[index - 1] if index > 0 else None
        previous_length = scenes_out[-1]["end"] - scenes_out[-1]["start"] if scenes_out else 0.0
        transition = _transition_of(scene, spec, previous, previous_length, picture_run)
        roles = (_SCENE_ROLE.get(previous.type) if previous else None, _SCENE_ROLE.get(scene.type))
        picture_run = picture_run + 1 if roles == ("picture", "picture") else 0
        overlap = 0.0
        if index > 0 and transition["type"] != "cut":
            overlap = transition["duration"]
            overlap = min(overlap, previous_length * 0.45)
        if index > 0 and transition["type"] == "cut":
            # A cut on a whole output frame: the scene before ends there, the remainder carried by this one.
            cursor_t = cut_on_frame(cursor_t, fps)
            scenes_out[-1]["end"] = round(cursor_t, 4)
        start = max(0.0, cursor_t - overlap)
        if index == 0:
            transition = {"type": "cut", "duration": 0.0}
        else:
            transition["duration"] = round(overlap, 4) if transition["type"] != "cut" else 0.0

        voice = media.voices.get(scene.id or "")
        narration_text = scene.narration or ""
        narration_length = 0.0
        if narration_text:
            narration_length = (
                voice.duration if voice else estimate_narration_seconds(narration_text, narration_speed(scene, spec))
            )
        narration_start = start + max(scene.narration_delay, overlap * 0.6)
        if narration_text and narration_start < last_narration_end + NARRATION_GAP:
            narration_start = last_narration_end + NARRATION_GAP
        narration_need = (narration_start - start) + narration_length + NARRATION_TAIL if narration_text else 0.0

        payload: dict[str, Any] = {"id": scene.id, "type": scene.type, "transition_in": transition}
        # The corner watermark off on this scene alone: said only when off, so every other document is as before.
        if scene.watermark is False:
            payload["watermark"] = False
        length: float

        if scene.type in ("title", "outro"):
            length = max(scene.duration, narration_need)
            payload.update(
                {
                    "title": scene.title,
                    "subtitle": scene.subtitle,
                    "style": scene.style,
                    "show_logo": scene.show_logo,
                    "kicker": scene.kicker,
                }
            )
            if scene.type == "title":
                payload["align"] = scene.align
            else:
                payload.update({"cta": scene.cta, "url": scene.url})
        elif scene.type == "statement":
            length = max(scene.duration, narration_need)
            payload.update(
                {
                    "lines": list(scene.lines),
                    "kicker": scene.kicker,
                    "figure": scene.figure,
                    "secondary": list(scene.secondary),
                    "chips": list(scene.chips),
                    "reveal": scene.reveal,
                    "align": scene.align,
                    "show_logo": card_draws_logo(scene.type, scene.show_logo, spec.theme.watermark),
                    "backdrop": _backdrop_payload(scene, media.backdrops.get(scene.id or ""), length),
                }
            )
        elif scene.type == "motion":
            length = max(scene.duration, narration_need)
            # On the beat before it is laid out: its elements leave at the end it really has.
            if sync != "off" and index < len(spec.scenes) - 1:
                length += beat_extra(media.beats, start + length)
            payload.update(_motion_payload(scene, spec, media, notes, theme=theme, size=(width, height), length=length))
        elif scene.type == "image" and (animated := media.clips.get(scene.id or "")) is not None:
            # Its animation is made: the shot plays, slowed to fill the scene, else holding its last frame.
            length = max(scene.duration, narration_need)
            source = max(0.1, min(animated.duration, length))
            played = filled_length(source, 1.0, length, stretch=scene.extend_mode == "zoom")
            payload.update(
                {
                    "type": "clip",
                    "src": f"clip/{scene.id}",
                    "fit": scene.fit,
                    "time_map": [[0.0, 0.0], [round(played, 4), round(source, 4)], [round(length, 4), round(source, 4)]],
                    "src_fps": animated.fps,
                    "src_frames": animated.frame_count,
                    "src_w": animated.width,
                    "src_h": animated.height,
                }
            )
            extend = extension(scene.extend_mode, played=played, length=length, src_from=0.0, src_to=source)
            if extend:
                payload["extend"] = extend
        elif scene.type == "figure":
            # Drawn as a shot or planned as a still, it stays a figure: one that rests between its steps by design.
            payload["figure"] = True
            if scene.perspective != "flat":
                # Stood in space by the compositor: the figure's own frames stay flat.
                payload["perspective"] = scene.perspective
            length = max(scene.duration, narration_need)
            if scene.reveal == "narration" and narration_text:
                # Its steps come in as they are said: one named last still has room to be followed.
                length = max(length, narration_start - start + narration_length + narrated_hold(scene.dwell))
            drawn = media.clips.get(scene.id or "")
            if drawn is not None:
                # Drawn for this very length - by the render (``figures.draw``), or for the editor's preview
                # (``figures.prepare_builds``): played like a shot.
                played = max(0.1, min(drawn.duration, length))
                payload.update(
                    {
                        "type": "clip",
                        "src": f"clip/{scene.id}",
                        "fit": "contain",
                        "time_map": [[0.0, 0.0], [round(played, 4), round(played, 4)], [round(length, 4), round(played, 4)]],
                        "src_fps": drawn.fps,
                        "src_frames": drawn.frame_count,
                        "src_w": drawn.width,
                        "src_h": drawn.height,
                    }
                )
            else:
                # Not drawn yet (the editor's preview before its build, the render's first plan): a still the page words.
                payload.update(
                    {
                        "type": "image",
                        "src": f"image/{scene.id}",
                        "motion": "none",
                        "fit": "contain",
                        "image_w": width,
                        "image_h": height,
                    }
                )
        elif scene.type in ("slide", "image"):
            length = max(scene.duration, narration_need)
            image = media.images.get(scene.id or "")
            payload.update(
                {
                    "src": f"image/{scene.id}",
                    "motion": scene.motion,
                    "fit": "contain" if scene.type == "slide" else scene.fit,
                    "image_w": image.width if image else width,
                    "image_h": image.height if image else height,
                }
            )
            if scene.type == "slide":
                payload["framed"] = True
            if scene.type == "image" and scene.effects:
                # Drawn on the real image by the compositor, a pure function of time like the rest.
                payload["effects"] = [effect.model_dump(mode="json") for effect in scene.effects]
        elif scene.type == "clip":
            clip = media.clips.get(scene.id or "")
            clip_duration = clip.duration if clip else 4.0
            trim_end = clip_duration if scene.trim_end is None else min(scene.trim_end, clip_duration)
            trim_start = min(scene.trim_start, max(trim_end - 0.1, 0.0))
            keep, reason = clip_sound_choice(scene, spec)
            if clip is not None and not clip.has_audio:
                keep, reason = False, "silent"
            heard = keep and clip is not None and scene.volume > 0
            # Only ``zoom`` fills the scene by slowing the shot: a loop or a hold plays it at its own speed.
            played = filled_length(
                trim_end - trim_start,
                scene.speed,
                narration_need - scene.hold_end,
                stretch=not heard and scene.extend_mode == "zoom",
            )
            hold = scene.hold_end + max(0.0, narration_need - (played + scene.hold_end))
            length = played + hold
            payload.update(
                {
                    "src": f"clip/{scene.id}",
                    "fit": scene.fit,
                    "time_map": [[0.0, trim_start], [round(played, 4), trim_end], [round(length, 4), trim_end]],
                    "src_fps": clip.fps if clip else 24,
                    "src_frames": clip.frame_count if clip else 1,
                    "src_w": clip.width if clip else width,
                    "src_h": clip.height if clip else height,
                }
            )
            if scene.hold_end:
                # The still beat its author asked for ends the scene: the compositor's drift stops before it.
                payload["hold_end"] = round(scene.hold_end, 4)
            extend = extension(
                scene.extend_mode,
                played=played,
                length=length,
                src_from=trim_start,
                src_to=trim_end,
                speed=scene.speed,
                still=scene.hold_end,
            )
            if extend:
                payload["extend"] = extend
            clip_sound[scene.id or ""] = (keep and scene.volume > 0, reason)
            if heard:
                fade_in = max(CLIP_SOUND_FADE, transition["duration"])
                sound = ClipSound(start, scene.id or "", trim_start, scene.speed, scene.volume, played)
                sounds.append((index, sound._replace(fade_in=min(fade_in, played / 2))))
        else:  # recording
            take = media.takes.get(scene.take_id)
            if take is None:
                raise ValueError(f"recording {scene.take_id} of scene {scene.id} is not available")
            time_map = build_time_map(
                duration=take.duration,
                trim_start=scene.trim_start,
                trim_end=kept_end(take, scene),
                speed=scene.speed,
                idle_mode=scene.idle.mode,
                idle_threshold=scene.idle.threshold,
                idle_speed=scene.idle.speed,
                events=take.events,
                # Only ``zoom`` gives the compressed pauses back to fill the scene; ``hold`` keeps them as asked.
                fill_to=narration_need - scene.hold_end if scene.extend_mode == "zoom" else 0.0,
            )
            played = time_map[-1][0]
            hold = scene.hold_end + max(0.0, narration_need - (played + scene.hold_end))
            time_map = with_hold(time_map, hold)
            length = time_map[-1][0]
            camera, _moves = recording_camera(
                scene, take, time_map, transition["duration"], transition_out_seconds(spec, index, length)
            )
            samples, clicks = plan_cursor(take, time_map, length)
            highlights: list[list[float]] = []
            for event in take.events:
                if event.get("action") == "highlight" and event.get("box") and event.get("ok", True):
                    t0 = map_inverse(time_map, float(event.get("ta", event.get("t0") or 0.0)))
                    t1 = map_inverse(time_map, float(event.get("t1") or 0.0))
                    box = _normalised_box(event.get("box"), take)
                    if box and t1 > t0:
                        highlights.append([round(t0, 4), round(t1, 4), *box])
            for highlight in scene.highlights:
                event = _event_for_step(take.events, highlight.at_step)
                box = _normalised_box(event.get("box") if event else None, take)
                if event is None or box is None:
                    notes.append(f"Scene {scene.id}: step {highlight.at_step} has no element to highlight.")
                    continue
                t0 = map_inverse(time_map, float(event.get("ta", event.get("t0") or 0.0)))
                highlights.append([round(t0, 4), round(t0 + highlight.duration, 4), *box])
            payload.update(
                {
                    "src": f"take/{scene.id}",
                    "src_fps": take.fps,
                    "src_frames": take.frame_count,
                    "src_w": round(take.css_width * take.pixel_ratio),
                    "src_h": round(take.css_height * take.pixel_ratio),
                    "css_w": take.css_width,
                    "css_h": take.css_height,
                    "time_map": [[round(a, 4), round(b, 4)] for a, b in time_map],
                    "camera": camera,
                    "cursor": {
                        "visible": scene.cursor.visible,
                        "size": scene.cursor.size,
                        "style": scene.cursor.style,
                        "click_effect": scene.cursor.click_effect,
                        "samples": samples,
                        "clicks": clicks,
                    },
                    "highlights": highlights,
                    "urls": plan_url_track(take, time_map),
                    "frame": drawn_frame(scene.frame),
                }
            )
            if painted := slowed_sparse(take, time_map):
                payload["painted"] = painted
            if scene.hold_end:
                payload["hold_end"] = round(scene.hold_end, 4)
            # A recording never loops (its cursor and address bar would jump back): it fills and drifts, or holds.
            extend = extension(scene.extend_mode, played=played, length=length, src_from=0.0, src_to=0.0)
            if extend:
                payload["extend"] = extend
            scene_overlays = []
            for overlay in _overlays_of(scene, media, notes):
                box = None
                local = overlay.start
                if overlay.at_step is not None:
                    event = _event_for_step(take.events, overlay.at_step)
                    if event is None:
                        notes.append(f"Scene {scene.id}: overlay at step {overlay.at_step} has no such step.")
                        continue
                    local = max(0.0, map_inverse(time_map, float(event.get("ta", event.get("t0") or 0.0))) - 0.15)
                    local += overlay.start
                    # A picture is placed like a badge: the step gives it its time, never its element.
                    box = _normalised_box(event.get("box"), take) if overlay.type != "image" else None
                t0 = min(local, max(length - 0.3, 0.0))
                scene_overlays.append(
                    _overlay_payload(
                        overlay,
                        start=t0,
                        end=min(t0 + overlay.duration, length),
                        box=box,
                        frame_width=width,
                        picture=media.overlays.get(overlay_image_key(overlay) or ""),
                    )
                )
            payload["overlays"] = scene_overlays
            inserts = []
            for key, part in _inserts_of(scene, media, notes):
                overlay = part.overlay
                local = overlay.start
                if overlay.at_step is not None:
                    event = _event_for_step(take.events, overlay.at_step)
                    if event is None:
                        if part.pane in (None, 0):
                            notes.append(f"Scene {scene.id}: screen insert at step {overlay.at_step} has no such step.")
                        continue
                    local += max(0.0, map_inverse(time_map, float(event.get("ta", event.get("t0") or 0.0))) - 0.15)
                t0 = min(local, max(length - 0.3, 0.0))
                inserts.append(
                    _insert_payload(
                        part, key, media.inserts[key], start=t0, end=min(t0 + overlay.duration, length), length=length
                    )
                )
            if inserts:
                payload["inserts"] = inserts

        if scene.type != "recording":
            inserts = [
                _insert_payload(
                    part,
                    key,
                    media.inserts[key],
                    start=min(part.overlay.start, max(length - 0.3, 0.0)),
                    end=min(part.overlay.start + part.overlay.duration, length),
                    length=length,
                )
                for key, part in _inserts_of(scene, media, notes)
                if part.overlay.at_step is None
            ]
            # A statement without its backdrop drawn (being made, failed) has no footage to map onto.
            if inserts and (scene.type != "statement" or payload.get("backdrop")):
                payload["inserts"] = inserts
            payload["overlays"] = [
                _overlay_payload(
                    overlay,
                    start=min(overlay.start, max(length - 0.3, 0.0)),
                    end=min(overlay.start + overlay.duration, length),
                    box=None,
                    frame_width=width,
                    picture=media.overlays.get(overlay_image_key(overlay) or ""),
                )
                for overlay in _overlays_of(scene, media, notes)
                if overlay.at_step is None
            ]

        # The cut into the next scene on the music's beat: this scene plays a little longer. A figure is drawn
        # for its very length; the last scene ends with the video.
        if sync != "off" and index < len(spec.scenes) - 1 and scene.type != "figure":
            extra = beat_extra(media.beats, start + length)
            if extra:
                length += extra
                _hold_longer(payload, extra)
        if sync == "all" and media.beats is not None and payload["type"] in ("statement", "motion"):
            # Its lines and its builds on the beats it plays over, in seconds of the scene: each on the nearest one.
            payload["beats"] = [round(b - start, 4) for b in media.beats.within(start, start + length)]
            payload["beat_tolerance"] = round(media.beats.period / 2 + 1e-3, 4)
            if payload["type"] == "motion":
                motion_design.snap_to_beats(payload["elements"], payload["beats"], tolerance=payload["beat_tolerance"])
        # Once the scene has its last length: its keyed inserts held to its end, both transitions included.
        hold_surfaces(payload.get("inserts") or [], length)
        end = start + length
        payload["start"] = round(start, 4)
        payload["end"] = round(end, 4)
        scenes_out.append(payload)
        cursor_t = end
        if narration_text:
            narration_starts[scene.id or ""] = round(narration_start, 4)
            narration_spans[scene.id or ""] = (
                round(narration_start, 4),
                round(narration_start + narration_length, 4),
            )
            last_narration_end = narration_start + narration_length
            if spec.captions.enabled:
                shown = media.captions.get(scene.id or "", narration_text)
                captions[scene.id or ""] = caption_cues(shown, narration_start, narration_length)
                cues.extend(captions[scene.id or ""])
            if voice_on and voice is None:
                notes.append(f"Scene {scene.id}: narration shown as captions only (no voice generated).")

    duration = round(cursor_t, 4)
    clip_audio: list[ClipSound] = []
    for index, sound in sounds:
        # Out with the picture: over the transition into the next scene when it starts before the sound ends.
        following = scenes_out[index + 1]["start"] if index + 1 < len(scenes_out) else duration
        fade_out = max(0.3, sound.start + sound.duration - following)
        clip_audio.append(sound._replace(fade_out=round(min(fade_out, sound.duration / 2), 4)))
    document = {
        "version": 1,
        "width": width,
        "height": height,
        "fps": fps,
        "duration": duration,
        "frames": max(1, math.ceil(duration * fps)),
        "fade_in": 0.6 if spec.fade_in else 0.0,
        "fade_out": 0.9 if spec.fade_out else 0.0,
        "theme": theme,
        "captions": {
            "enabled": spec.captions.enabled and spec.captions.burn_in,
            "style": spec.captions.style,
            "position": spec.captions.position,
            "cues": cues,
        },
        "watermark": spec.theme.watermark,
        "scenes": scenes_out,
    }
    return TimelinePlan(
        document=document,
        duration=duration,
        narration_starts=narration_starts,
        clip_audio=clip_audio,
        notes=notes,
        narration_spans=narration_spans,
        clip_sound=clip_sound,
        captions=captions,
    )


def estimate_duration(
    spec: VideoSpec, takes: dict[str, TakeMedia], clips: dict[str, ClipMedia] | None = None
) -> float:
    """The planned length before any audio exists (narration paced at 156 words per minute).

    *clips* are the clip scenes' videos as far as they are known (by scene id):
    a clip scene lasts as long as its video, so one missing is planned at the
    planner's 4 s.
    """
    return estimate_plan(spec, takes, clips).duration


def preview_window(count: int, index: int) -> tuple[int, int]:
    """``[first, end)`` of the scenes a preview of the scene at *index* plans: the one before, it, the one after."""
    return max(0, index - 1), min(count, index + 2)


def owned_span(document: dict, index: int) -> tuple[float, float]:
    """``(from, to)`` in the video where the scene at *index* of a planned *document* is on screen alone.

    After its transition in has ended, before the next scene's begins (to the end for the last scene), out of the
    video's fade from and to black: no frame of it is a blend with a neighbour. A scene too short to own any frame
    gives the instant its transition in ends.
    """
    scenes = document["scenes"]
    scene = scenes[index]
    owned_from = float(scene["start"]) + float((scene.get("transition_in") or {}).get("duration") or 0.0)
    owned_to = float(scenes[index + 1]["start"]) if index + 1 < len(scenes) else float(document["duration"])
    owned_from = max(owned_from, float(document.get("fade_in") or 0.0))
    owned_to = min(owned_to, float(document["duration"]) - float(document.get("fade_out") or 0.0))
    return owned_from, max(owned_from, owned_to)


def transition_windows(document: dict) -> list[tuple[float, float, int]]:
    """``(from, to, index of the scene coming in)`` in the video of every join between two scenes of a planned
    *document*: a transition's frames, from the next scene's start to the end of the one it leaves - the two
    blended -, a cut's :data:`CUT_FRAMES` frames each side of it."""
    fps = max(float(document.get("fps") or 30.0), 1.0)
    scenes = list(document.get("scenes") or [])
    out = []
    for index in range(1, len(scenes)):
        start = float(scenes[index]["start"])
        seconds = float((scenes[index].get("transition_in") or {}).get("duration") or 0.0)
        if seconds > 0:
            out.append((start, start + seconds, index))
        else:
            out.append((max(0.0, start - CUT_FRAMES / fps), start + CUT_FRAMES / fps, index))
    return out


def planned_transitions(plan: TimelinePlan) -> list[tuple[str, str, float]]:
    """``(scene id, transition, seconds)`` of every scene after the first, as *plan* cuts into it."""
    return [
        (str(scene.get("id")), str(scene["transition_in"]["type"]), float(scene["transition_in"]["duration"]))
        for scene in (plan.document.get("scenes") or [])[1:]
    ]


#: How long after a scene's transition in has ended its poster frame is taken.
POSTER_SETTLE_SECONDS = 0.5


def poster_time(document: dict, *, settle: float = POSTER_SETTLE_SECONDS) -> float:
    """When a render's poster is taken: a settled frame of its first scene that is not a title card.

    Settled: *settle* seconds after the transition into the scene ended and
    before the next scene's transition starts, out of the fades from and to
    black - never a dissolve mid-way. A scene too short for that gives the
    middle of its settled span; with no such scene, the first frame the fade
    in leaves visible.
    """
    scenes = list(document.get("scenes") or [])
    duration = float(document.get("duration") or 0.0)
    shown_from = float(document.get("fade_in") or 0.0)
    shown_until = duration - float(document.get("fade_out") or 0.0)
    for index, scene in enumerate(scenes):
        if scene.get("type") == "title":
            continue
        start = float(scene["start"]) + float((scene.get("transition_in") or {}).get("duration") or 0.0)
        end = float(scenes[index + 1]["start"]) if index + 1 < len(scenes) else float(scene["end"])
        start, end = max(start, shown_from), min(end, shown_until)
        if end <= start:
            continue
        return round(start + settle if start + settle < end else (start + end) / 2, 4)
    return round(min(shown_from, max(duration - 0.1, 0.0)), 4)


#: The longest chapter name a render keeps; a player cuts it to its width anyway.
CHAPTER_TITLE_MAX = 120


def chapters(spec: VideoSpec, document: dict, names: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """A whole render's chapters: one per scene of *spec*, where *document* planned it.

    A chapter starts where its scene takes over from the one before: halfway
    through its transition in, the first one at 0. It names the scene as the
    editor's timeline does - a card's title, else a statement's first line
    (its ``*accent*`` marks dropped), else what it shows (*names*: a
    recording, an AI shot) - and keeps the scene's type as authored (the
    planner may draw an image as a shot), which a player words when the
    scene has no name. A chapter that would not start after the one before
    (a scene too short to take over) is folded into it.
    """
    planned = list(document.get("scenes") or [])
    duration = float(document.get("duration") or 0.0)
    names = names or {}
    out: list[dict[str, Any]] = []
    for index, (scene, payload) in enumerate(zip(spec.scenes, planned, strict=False)):
        transition = float((payload.get("transition_in") or {}).get("duration") or 0.0)
        start = 0.0 if index == 0 else float(payload.get("start") or 0.0) + transition / 2
        start = round(min(max(start, 0.0), max(duration - 0.1, 0.0)), 3)
        if out and start <= out[-1]["start"]:
            continue
        title = getattr(scene, "title", None)
        if not title and scene.type == "statement":
            title = scene.lines[0].replace("*", "")
        if not title and scene.type == "motion":
            title = next(iter(motion_design.drawn_texts(scene)), "").replace("*", "")
        if not title:
            title = names.get(scene.id or "")
        title = " ".join(str(title or "").split())[:CHAPTER_TITLE_MAX] or None
        out.append({"start": start, "type": scene.type, "title": title})
    return out


def estimate_plan(
    spec: VideoSpec,
    takes: dict[str, TakeMedia],
    clips: dict[str, ClipMedia] | None = None,
    voices: dict[str, float] | None = None,
    *,
    place_inserts: bool = False,
    backdrops: dict[str, ClipMedia | ImageMedia] | None = None,
    beats: BeatGrid | None = None,
) -> TimelinePlan:
    """:func:`plan` before the render's audio exists: what :func:`estimate_duration` measures.

    *voices* are the narrations already spoken (``scene id -> seconds``): planned at that length, as the
    render places them; the others at the reading pace (:func:`estimate_narration_seconds`). *place_inserts*
    places every screen insert on a stand-in source, a quad left to the footage on :data:`STAND_IN_QUAD`:
    when each comes in and goes, in its scene's seconds, before any source is read; *backdrops* (by scene id) are
    what plays behind the statements, which an estimate otherwise leaves out. *beats* are the music's
    (``beats.video_grid``): a ``beat_sync`` video's cuts land on them, each scene a little longer, as the
    render plans them.
    """
    media = PlanMedia(takes=takes, clips=dict(clips or {}), backdrops=dict(backdrops or {}), beats=beats)
    media.voices = {scene_id: VoiceMedia(float(seconds)) for scene_id, seconds in (voices or {}).items() if seconds}
    if place_inserts:
        spec = with_quads(spec, {}, stand_in=True)
        media.inserts = {key: ImageMedia(1, 1) for key in insert_sources(spec)}
        media.inserts_gathered = True
    for scene in spec.scenes:
        if scene.type == "recording" and scene.take_id not in takes:
            media.takes[scene.take_id] = TakeMedia(
                take_id=scene.take_id,
                duration=10.0,
                css_width=1600,
                css_height=900,
                pixel_ratio=1.0,
                fps=60,
                frame_count=600,
            )
    return plan(spec, media, fps=30, theme={})


def vtt_from_cues(cues: list[list[Any]], position: str = "bottom") -> str:
    """A WebVTT document of the caption cues, drawn at *position* - the band every scene keeps clear for them."""

    def stamp(t: float) -> str:
        t = max(0.0, t)
        hours, rem = divmod(t, 3600)
        minutes, seconds = divmod(rem, 60)
        return f"{int(hours):02d}:{int(minutes):02d}:{seconds:06.3f}"

    settings = " line:0" if position == "top" else ""
    lines = ["WEBVTT", ""]
    for index, (t0, t1, text) in enumerate(cues, start=1):
        lines.append(str(index))
        lines.append(f"{stamp(float(t0))} --> {stamp(float(t1))}{settings}")
        lines.append(str(text).replace("-->", "->"))
        lines.append("")
    return "\n".join(lines)
