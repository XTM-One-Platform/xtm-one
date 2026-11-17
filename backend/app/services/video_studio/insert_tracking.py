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

"""Screen inserts that follow the camera: a quad tracked through its scene's footage.

A ``screen_insert`` maps a picture or a video onto a surface of its scene's
footage given as a ``quad`` - fixed, so on footage whose camera moves the
insert slides off its monitor. With ``track: true`` the quad is followed
instead:

* **The quad** is the surface on the footage frame the insert comes in on.
* **The surface and its bezel** are what is followed: the quad and a
  ring :data:`BEZEL_OUTSIDE` past its edges - a building's face and its
  frame, a monitor and its bezel - read by a ``tracking.PlanarTracker`` on
  the footage decoded at most :data:`TRACK_SIDE` long, one homography per
  frame until the insert goes out; what moves on its own on the surface (the
  model's drawing on a screen) agrees with no homography and is dropped as an
  outlier. Every insert on the same footage is followed in one decode of it
  (:func:`track_all`).
* **The quads** of every frame - the four corners moved by its homography,
  averaged over :data:`SMOOTH_WINDOW` frames: the surface's perspective, never
  an axis-aligned rectangle - are what the render's driver draws the insert
  into (``screen_insert.compose``) for the footage frame the page shows; the
  page places every frame on one box covering them all.

**The bezel read.** On every frame the bezel is read near where it
was read on the frame before (``surface_refine.read_near``: the read the
save and the static alignment gate make, on the luma or the key's matte),
seeded by its own reading - never by the tracker's quad but on a frame it
found nothing - and kept when it lies within :data:`BEZEL_READ_MAX_SHARE`
of the frame's width of that seed. A frame the tracker is not sure of - too
few points agree, fewer than :data:`MIN_CONFIDENCE` of those followed into
it agree on the plane, the quad it gives folds or changes size past
:data:`MAX_AREA_CHANGE` or stands past :data:`BEZEL_READ_MAX_SHARE` off the
bezel read there, or the surface had too little detail to follow at all -
takes that bezel instead, and the tracker starts again from it. A wall shot
whose screen draws on its own and whose wall carries little detail is a
static-looking surface its tracker may doubt, whose bezel is clean on every
frame. :attr:`InsertTrack.bezel_read` names those frames
(:meth:`InsertTrack.said`: "N frames by the tracker, M by the bezel read"),
and :meth:`InsertTrack.spans` the runs of frames each source placed, which the
save and the preview say (:meth:`InsertTrack.said_spans`).
The first frame's quad is the one given - the bezel the save measured there
(``insert_bezels``).

**The bezel read is fused into the track.** Where the bezel read takes over
from the tracker (or gives it back), the two quads differ by up to
:data:`BEZEL_READ_MAX_SHARE` - both on the surface, within the alignment gate,
but a one-frame step the jitter gate fails - and each read restarts the
tracker from its own quad, read noise included. The track is split in two
(:func:`blended`): the surface's motion, the tracker's own frame-to-frame
steps integrated (taken between them across the frames a switch or the bezel
read placed), and the quad's offset from that motion, low-passed over
:data:`BLEND_FRAMES` frames. The motion is kept as the footage shows it (a real
jump the tracker follows still jumps); the offset never steps, so a smoothly
drifting surface gives a smooth quad, each frame an average of the sources
around it moved by the motion between them - within the alignment gate.

A surface the bezel read loses too fails the render (and the scene preview)
with :class:`InsertTrackLostError`: ``surface lost at t=...`` in its scene's
time, the footage frame named, why the tracker and the bezel read let it go,
and that frame - the last quad found drawn on it - attached
(:attr:`InsertTrackLostError.frame_png`). Never an insert drawn off its surface.
Before the insert comes in - its keyed surface followed back only to
keep it covered - a loss holds the quad of the frame after it back to the
first frame it is held on (:attr:`InsertTrack.held_before`), said in the
render's notes: the residual-key and presence gates read what those frames
show, and the render no longer fails for a surface nobody sees an insert on.

**A stabilised AI shot** is warped to its first frame and cropped
(``stabilise``): followed through those frames, the quad would jitter with
the warp's residue and the crop cuts the screen at the frame's edge. Its
inserts are followed through the un-stabilised take instead
(:func:`track_stabilised`): each quad is taken into the raw frame through the
stabilisation's own map of that frame, followed there, and each quad
followed is brought back through the map of its frame - where the
stabilised take draws the screen. A quad that leaves the frame the
stabilised take keeps fails as "screen cropped" (:class:`ScreenCroppedError`):
an insert is never drawn into a screen the crop cut.

**A wall of panes** (``panes``) is followed once, as the one quad its
overlay gives; each pane's quads are the wall's split (:func:`pane_track`).
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.services.video_studio.surface_refine import ALIGNMENT_MAX_SHARE

logger = logging.getLogger(__name__)

#: The longest side the footage is followed at.
TRACK_SIDE = 960
#: What is followed around the surface: this share of the quad's size past its edges.
BEZEL_OUTSIDE = 0.25
#: Fewer points than this agreeing on a frame's homography: the surface is lost.
MIN_INLIERS = 10
#: Fewer than this share of the points followed into a frame agreeing on its homography: the track's confidence is gone.
MIN_CONFIDENCE = 0.4
#: The bezel read is kept within this share of the frame's width of where it read the bezel on the frame before, and a
#: sure tracker's quad this far off it at a corner yields to it: the alignment gate's own threshold.
BEZEL_READ_MAX_SHARE = ALIGNMENT_MAX_SHARE
#: Why a surface's tracker does not follow it at all: too few points to follow.
TOO_LITTLE_DETAIL = "the surface and its bezel have too little detail to follow"
#: Moved when a track is followed differently, so a track kept by an earlier render is followed again (1: the bezel read
#: on the frames the tracker is not sure of, its confidence the share of the points followed into the frame; 2: a switch
#: between the tracker and the bezel read blended over :data:`BLEND_FRAMES` frames; 3: the bezel read fused into the
#: track, its offset from the tracker's motion low-passed).
TRACK_VERSION = 3
#: The span (frames) the offset between the sources is low-passed over (:func:`blended`, a Gaussian of half that
#: sigma): a step of :data:`BEZEL_READ_MAX_SHARE` (0.5%) moves no frame by more than about 0.05%, and its second
#: difference - what the jitter gate reads - stays under 0.01%, against the gate's 0.1%.
BLEND_FRAMES = 8
#: The spans a save or preview note names at most (:meth:`InsertTrack.said_spans`); the rest are counted.
SAID_SPANS = 12
#: A tracked quad whose area is outside this factor of the one given (either way), or not convex: lost.
MAX_AREA_CHANGE = 2.0
#: Frames averaged around each one when the tracked corners are smoothed.
SMOOTH_WINDOW = 3
#: How often (frames) the caller hears how far the track is.
PROGRESS_EVERY = 30
#: How far (a share of the frame) a quad followed on a stabilised shot may stand past its edge before it is cropped.
CROP_TOLERANCE = 0.002
_CORNERS = ("top left", "top right", "bottom right", "bottom left")


class InsertTrackLostError(RuntimeError):
    """A tracked insert's surface was lost: ``str()`` says which insert, when in its scene and at which footage frame.

    *frame_png* is that footage frame with the last quad found drawn on it (the finding's frame); *scene_id* and
    *scene_seconds* place it in the video.
    """

    def __init__(
        self,
        message: str,
        *,
        frame: int,
        seconds: float,
        scene_id: str | None = None,
        scene_seconds: float = 0.0,
        frame_png: bytes | None = None,
    ) -> None:
        super().__init__(message)
        self.frame = frame
        self.seconds = seconds
        self.scene_id = scene_id
        self.scene_seconds = scene_seconds
        self.frame_png = frame_png


class ScreenCroppedError(InsertTrackLostError):
    """A tracked insert on a stabilised shot leaves the frame the stabilisation kept: "screen cropped"."""


@dataclass(frozen=True)
class InsertTrack:
    """A tracked insert's quad in each footage frame from *first*, 0-1 across and down the footage."""

    first: int
    quads: tuple[tuple[tuple[float, float], ...], ...]
    #: The box every quad lies in (0-1, ``x0, y0, x1, y1``): where the page places each drawn frame.
    box: tuple[float, float, float, float]
    #: The footage frames whose quad is the bezel read's: the tracker was not sure of them.
    bezel_read: tuple[int, ...] = ()
    #: The first frames, before it comes in, held on the quad of the frame after them: its surface lost there,
    #: followed back.
    held_before: int = 0

    @property
    def last(self) -> int:
        return self.first + len(self.quads) - 1

    def said(self) -> str:
        """Who placed its frames (:func:`placed_by`), and those held before it comes in."""
        said = placed_by(len(self.quads) - len(self.bezel_read) - self.held_before, len(self.bezel_read))
        return f"{said}, {self.held_before} held before it comes in" if self.held_before else said

    def spans(self) -> list[tuple[str, int, int]]:
        """The runs of footage frames each source placed, in order: ``(source, first frame, last frame)``, the source
        ``"held"`` (before it comes in, :attr:`held_before`), ``"tracker"`` or ``"bezel read"``."""
        by_bezel = set(self.bezel_read)
        runs: list[tuple[str, int, int]] = []
        for offset in range(len(self.quads)):
            frame = self.first + offset
            source = "held" if offset < self.held_before else "bezel read" if frame in by_bezel else "tracker"
            if runs and runs[-1][0] == source and runs[-1][2] == frame - 1:
                runs[-1] = (source, runs[-1][1], frame)
            else:
                runs.append((source, frame, frame))
        return runs

    def said_spans(self) -> str:
        """Its :meth:`spans` as a note says them: ``"frames 0-40 by the tracker, 41-60 by the bezel read"``, at most
        :data:`SAID_SPANS` of them, the rest counted; the switches between them blended (:func:`blended`)."""
        runs = self.spans()
        said = [
            f"{first}-{last} {'held' if source == 'held' else 'by the ' + source}"
            if last > first
            else f"{first} {'held' if source == 'held' else 'by the ' + source}"
            for source, first, last in runs[:SAID_SPANS]
        ]
        more = f", and {len(runs) - SAID_SPANS} more spans" if len(runs) > SAID_SPANS else ""
        switches = sum(1 for a, b in itertools.pairwise(runs) if "held" not in (a[0], b[0]))
        blend = f"; {switches} source switches blended over {BLEND_FRAMES} frames" if switches else ""
        return f"frames {', '.join(said)}{more}{blend}"

    def near_switch(self, frame: int | None, reach: int = BLEND_FRAMES) -> bool:
        """Whether footage frame *frame* lies within *reach* frames of a switch between the tracker and the bezel read
        (:meth:`spans`, the frames held before it comes in left out): where a quad step comes from the track, not the
        footage (``insert_checks._jitter_action``)."""
        if frame is None or not self.bezel_read:
            return False
        runs = [run for run in self.spans() if run[0] != "held"]
        return any(abs(int(frame) - after[1]) <= reach for after in runs[1:])

    def quad_at(self, frame: int | None) -> tuple[tuple[float, float], ...]:
        """The quad on footage frame *frame* (clamped to the frames tracked; ``None``: the first)."""
        if frame is None:
            return self.quads[0]
        return self.quads[min(max(frame - self.first, 0), len(self.quads) - 1)]

    def box_quad(self) -> list[list[float]]:
        """:attr:`box` as the four corners the page's ``insertBox`` reads."""
        x0, y0, x1, y1 = self.box
        return [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]


def placed_by(tracker: int, bezel: int) -> str:
    """Who placed a track's frames, as the save says it: ``"N frames by the tracker, M by the bezel read"``."""
    return f"{tracker} frames by the tracker, {bezel} by the bezel read"


def blended(corners: Sequence[Any], by_bezel: Sequence[bool], frames: int = BLEND_FRAMES) -> list[Any]:
    """*corners* (one quad per frame, pixels) with the bezel read (*by_bezel*: whether it placed that frame) fused into
    the track, so a smooth footage gives a smooth quad across every switch of source.

    The track is split in two. Its motion: the tracker's own step between two frames it placed in a row, taken
    between them - per corner, linearly - on every other frame, whose step mixes the motion with the gap between the
    sources and the read's noise (a single frame of the tracker between two reads too: whether it was followed from
    the read or not); integrated from the first frame; no such step at all, the track's mean motion. Its offset:
    each quad less that motion, a constant while one source follows the surface, a step at each switch. The offset
    is low-passed (a Gaussian of sigma *frames* / 2) and added back to the motion: the motion the tracker saw is
    kept whole (a real jump it follows still jumps), the switches and the reads' noise are spread over the frames
    around them, never a step, and every frame is an average of the sources near it. A track placed by one source
    throughout is returned unchanged.
    """
    import numpy as np
    from scipy.ndimage import gaussian_filter1d

    quads = [np.asarray(quad, dtype=float) for quad in corners]
    count = len(quads)
    sources = [bool(by_bezel[n]) for n in range(count)]
    if count < 3 or frames < 1 or all(sources) or not any(sources):
        return quads
    track = np.stack(quads)
    steps = np.zeros_like(track)
    steps[1:] = track[1:] - track[:-1]
    # The steps the tracker measured: from a frame it placed onto the next one it placed.
    known = np.array([n > 0 and not sources[n] and not sources[n - 1] for n in range(count)])
    unknown = ~known
    unknown[0] = False
    if known.any():
        at, flat = np.arange(count), steps.reshape(count, -1)
        for column in range(flat.shape[1]):
            flat[unknown, column] = np.interp(at[unknown], at[known], flat[known, column])
        steps = flat.reshape(track.shape)
    else:
        steps[1:] = (track[-1] - track[0]) / (count - 1)
    motion = np.cumsum(steps, axis=0)
    offset = gaussian_filter1d(track - motion, sigma=frames / 2, axis=0, mode="nearest")
    return list(motion + offset)


def _area(quad: Any) -> float:
    import numpy as np

    points = np.asarray(quad, dtype=float)
    x, y = points[:, 0], points[:, 1]
    return float(0.5 * (np.dot(x, np.roll(y, -1)) - np.dot(np.roll(x, -1), y)))


@dataclass(frozen=True)
class Asked:
    """One insert to follow: its *quad* (0-1) on footage frame *first*, followed to frame *last*; *what* names it.

    *scene_id* and *time_map* (its scene's, scene seconds to footage seconds) say when in the video a loss is. *since*
    is the frame it is followed back to from *first*: a keyed insert held on its surface before it comes in.
    *key* is its chroma key (``screen_insert.KeySpec``): the bezel read reads the key's matte, as the save does.
    """

    quad: Sequence[Sequence[float]]
    first: int
    last: int
    what: str
    scene_id: str | None = None
    time_map: tuple[tuple[float, float], ...] = ()
    since: int | None = None
    key: Any = None

    @property
    def back_to(self) -> int:
        """The first footage frame it is followed on: *since*, never past *first*."""
        return min(self.since, self.first) if self.since is not None else self.first


def asked_of(
    payload: dict, footage: dict, fps: float, quad: Sequence[Sequence[float]], what: str, *, scene_id: str | None, key: Any = None
) -> Asked:
    """What the tracked insert the plan places as *payload* on *footage* (its planned scene, or the scene's backdrop)
    asks to follow at the footage's *fps*: from the frame it comes in on, where *quad* is given, over every frame its
    layer is drawn on (a keyed insert's whole scene, followed back from there) - the render's and the save's
    one reading."""
    from app.services.video_studio import timeline

    time_map = [tuple(point) for point in footage.get("time_map") or []]
    first = round(timeline.map_forward(time_map, float(payload["t0"])) * fps)
    drawn_from, drawn_to = timeline.insert_drawn(payload)
    since = min(first, round(timeline.map_forward(time_map, drawn_from) * fps))
    last = max(first, round(timeline.map_forward(time_map, max(float(payload["t1"]), drawn_to)) * fps))
    return Asked(quad, first, last, what, scene_id, tuple(time_map), since, key)


#: The ``render_cache`` kind the quads a render followed are kept under for the editor's preview.
PREVIEW_TRACKS = "preview-tracks"


def preview_track_digest(footage_file: object, scene_id: object, payload: dict[str, Any], time_map: Any) -> str:
    """The key a tracked insert's quads are kept under for the editor's preview: what the render followed them from.

    The footage's file, the scene, the insert (its key, its quad, when it is drawn) and the scene's time map: what
    the render and the preview's plan both carry (``timeline``), so the preview finds the quads the last render of
    the same scene followed, and a changed quad or timing finds none (the preview keeps its badge).
    """
    import hashlib
    import json

    from app.services.video_studio import timeline

    drawn = timeline.insert_drawn(payload)
    said = {
        "version": TRACK_VERSION,
        "footage": str(footage_file),
        "scene": str(scene_id),
        "insert": str(payload["src"]),
        "quad": [[round(float(x), 5), round(float(y), 5)] for x, y in payload["quad"]],
        "t": [round(float(payload["t0"]), 4), round(float(payload["t1"]), 4), *(round(float(v), 4) for v in drawn)],
        "time_map": [[round(float(a), 4), round(float(b), 4)] for a, b in (time_map or [])],
    }
    return hashlib.sha256(json.dumps(said, sort_keys=True).encode("utf-8")).hexdigest()


def preview_track_payload(track: InsertTrack, fps: float) -> dict[str, Any]:
    """*track* as the preview's page reads it (``compositor_page.previewInsert``): its quads, 0-1 of the footage, from
    footage frame ``from`` at the footage's ``fps``."""
    return {
        "from": int(track.first),
        "fps": float(fps),
        "quads": [[[round(float(x), 5), round(float(y), 5)] for x, y in quad] for quad in track.quads],
    }


def preview_track_read(kept: object) -> dict[str, Any] | None:
    """A kept preview track (:func:`preview_track_payload`) checked, or ``None`` for one the page cannot draw."""
    if not isinstance(kept, dict):
        return None
    try:
        quads = [[[float(x), float(y)] for x, y in quad] for quad in kept["quads"]]
        if not quads or any(len(quad) != 4 for quad in quads):
            return None
        return {"from": int(kept["from"]), "fps": float(kept["fps"]), "quads": quads}
    except (KeyError, TypeError, ValueError):
        return None


def _evidence(rgb: Any, quad: Any) -> bytes:
    """*rgb* (a footage frame) with *quad* (its pixels) outlined: the frame a lost surface is shown by."""
    import io

    import numpy as np
    from PIL import Image, ImageDraw

    picture = Image.fromarray(np.ascontiguousarray(rgb), "RGB")
    ImageDraw.Draw(picture).line([tuple(map(float, p)) for p in [*quad, quad[0]]], fill=(255, 40, 40), width=3)
    out = io.BytesIO()
    picture.save(out, format="JPEG", quality=88)
    return out.getvalue()


@dataclass
class _Follower:
    """One way an insert is followed (forward, or back to its ``since``): its planar tracker and the quad on the
    tracker's reference frame (pixels) - no tracker: too little detail to follow -, and its bezel read."""

    tracker: Any
    quad: Any
    #: Where the bezel read found the bezel last (pixels): the next frame's read is seeded there - by its own reading,
    #: never by a tracker's quad but on a frame it found no bezel.
    bezel: Any = None


class _Behind:
    """The footage frames before an insert's first that it is followed back over, as decoded - in a file of
    their own, never in memory: a scene's seconds before its insert comes in are hundreds of frames, each
    about 1.5 MB at :data:`TRACK_SIDE`. A frame given back frees its slot for the next."""

    def __init__(self, size: int) -> None:
        import tempfile

        self._file = tempfile.TemporaryFile()  # noqa: SIM115 - closed by :meth:`close`
        self._size = int(size)
        self._slots: dict[int, int] = {}
        self._free: list[int] = []
        self._next = 0

    def __contains__(self, frame: int) -> bool:
        return frame in self._slots

    def __iter__(self):
        return iter(list(self._slots))

    def __len__(self) -> int:
        return len(self._slots)

    def put(self, frame: int, raw: bytes) -> None:
        if frame not in self._slots:
            if self._free:
                self._slots[frame] = self._free.pop()
            else:
                self._slots[frame], self._next = self._next, self._next + 1
        self._file.seek(self._slots[frame] * self._size)
        self._file.write(raw)

    def get(self, frame: int) -> bytes:
        self._file.seek(self._slots[frame] * self._size)
        return self._file.read(self._size)

    def pop(self, frame: int) -> None:
        self._free.append(self._slots.pop(frame))

    def close(self) -> None:
        self._file.close()


@dataclass
class _Following:
    asked: Asked
    given: Any
    expected: float
    #: Its key as its first frame shows it (``screen_insert.KeySpec.sampled``): what the bezel read keys.
    key: Any = None
    follower: _Follower | None = None
    corners: list[Any] = field(default_factory=list)
    #: The footage frames the bezel read placed.
    by_bezel: list[int] = field(default_factory=list)
    #: The first frames held on the quad of the frame after them: its surface lost there, before it comes in.
    held_before: int = 0


def _bezel_read(rgb: Any, seed: Any, key: Any) -> tuple[Any | None, str]:
    """The bezel *rgb* (a footage frame at the tracking size) shows near *seed* - where it was read on the frame before,
    its pixels - read as the save and the static gate read it (``surface_refine.read_near``, on the luma or
    *key*'s matte): ``(its corners, "")`` within :data:`BEZEL_READ_MAX_SHARE` of the frame's width of the seed, else
    ``(None, why not)``."""
    import numpy as np

    from app.services.video_studio import surface_refine

    found = surface_refine.read_near(
        rgb,
        seed,
        key.key() if key is not None else None,
        key.matte_tolerance() if key is not None else None,
        feather=key.feather if key is not None else 1.5,
    )
    if not found.found or found.seed_error is None:
        return None, "the bezel read finds no bezel where it read one before"
    share = found.seed_error / max(rgb.shape[1], 1)
    if share > BEZEL_READ_MAX_SHARE:
        return None, (
            f"the bezel read finds one {share:.2%} of the frame's width from where it read one before "
            f"(over {BEZEL_READ_MAX_SHARE:.1%})"
        )
    return np.asarray(found.quad, dtype=float), ""


async def track(
    video_path: str,
    quad: Sequence[Sequence[float]],
    *,
    first: int,
    last: int,
    what: str,
    progress: Callable[[int, int], Awaitable[None]] | None = None,
) -> InsertTrack:
    """*quad* (0-1, on footage frame *first*) followed through *video_path* to frame *last*; or :class:`InsertTrackLostError`.

    *what* names the insert in the failure ("Scene demo: its screen insert 0");
    *progress* hears ``(frames followed, frames to follow)`` every
    :data:`PROGRESS_EVERY` frames (a render's heartbeat).
    """
    [followed] = await track_all(video_path, [Asked(quad, first, last, what)], progress=progress)
    return followed


async def track_all(
    video_path: str,
    asked: Sequence[Asked],
    *,
    progress: Callable[[int, int], Awaitable[None]] | None = None,
    on_frame: Callable[[], None] | None = None,
) -> list[InsertTrack]:
    """Every insert *asked* followed through *video_path* in one decode of it, in order; or :class:`InsertTrackLostError`.

    Each is followed from its own first frame to its last, as :func:`track`
    follows one, and back to its ``since``: the frames before its first
    are kept as decoded, at the tracking size, in a file (:class:`_Behind`),
    and followed in reverse from the quad given on its first. A frame
    the tracker is not sure of keeps the bezel read there, and the tracker
    starts again from it (the module's docstring). A surface lost before
    the insert comes in - only followed to keep its keyed surface covered -
    holds the quad of the frame after it back to its ``since``
    (:attr:`InsertTrack.held_before`): the render says so, and the
    gates read what those frames show; lost once it is in, it fails.
    *progress* hears the frames followed of them all. *on_frame* is called
    after each frame decoded: a render renews its lease from it.
    """
    import numpy as np

    from app.services.video_studio import encoder, screens, timeline
    from app.services.video_studio.tracking import PlanarTracker, Region, convex, grown, luma, project

    if not asked:
        return []
    info = await encoder.probe(video_path)
    width, height = int(getattr(info, "width", 0) or 0), int(getattr(info, "height", 0) or 0)
    fps = float(getattr(info, "fps", None) or 24.0)

    def lost(item: Asked, frame: int, why: str, shown: Any = None) -> InsertTrackLostError:
        at = timeline.map_inverse([tuple(p) for p in item.time_map], frame / fps) if item.time_map else frame / fps
        # Before it comes in, it is followed only to keep its keyed surface covered: where it comes in decides.
        remedy = (
            "start the insert with its scene (start 0) and give its quad on the scene's first footage frame"
            if frame < item.first
            else "cut the insert short (duration) before that moment"
        )
        return InsertTrackLostError(
            f"{item.what}: surface lost at t={at:.2f} s of its scene (footage frame {frame}, {frame / fps:.2f} s): "
            f"{why} - give the quad on calmer footage, {remedy}, or drop track for a static quad.",
            frame=frame,
            seconds=frame / fps,
            scene_id=item.scene_id,
            scene_seconds=at,
            frame_png=shown,
        )

    if width < 16 or height < 16:
        raise InsertTrackLostError(
            f"{asked[0].what} cannot follow its surface: its footage's size could not be read.",
            frame=asked[0].first,
            seconds=asked[0].first / fps,
        )
    scale = min(1.0, TRACK_SIDE / max(width, height))
    size = (max(16, round(width * scale / 2) * 2), max(16, round(height * scale / 2) * 2))
    following = []
    for item in asked:
        given = np.asarray(item.quad, dtype=float) * np.array(size, dtype=float)
        following.append(_Following(item, given, abs(_area(given))))
    start, end = min(item.back_to for item in asked), max(item.last for item in asked)
    total = sum(item.last - item.back_to for item in asked)
    index = -1
    #: The frames before an insert's first that it is followed back over, as decoded, in a file.
    behind = _Behind(size[0] * size[1] * 3)

    def followed() -> int:
        return sum(max(len(one.corners) - 1, 0) for one in following)

    def opened(grey: Any, quad: Any) -> Any:
        """A tracker of the surface *quad* (pixels) shows on *grey* and its bezel; ``None`` on too little detail."""
        tracker = PlanarTracker(grey, Region(outer=grown(quad, BEZEL_OUTSIDE)), max_points=240, min_distance=5, min_inliers=MIN_INLIERS)
        return tracker if tracker.features >= MIN_INLIERS else None

    async def follower_of(grey: Any, quad: Any) -> _Follower:
        """*quad* (pixels, on *grey*) followed from there: its tracker, and its bezel read seeded on it."""
        corners = np.asarray(quad, dtype=float)
        return _Follower(await asyncio.to_thread(opened, grey, corners), corners, corners.copy())

    async def placed(one: _Following, follower: _Follower, grey: Any, rgb: Any, frame: int, last: Any) -> tuple[Any, bool]:
        """The quad of *one* on footage frame *frame* (*grey*, *rgb*) and whether the bezel read placed it: the
        tracker's when it is sure of it, else the bezel the read finds near where it found it on the frame before, the
        tracker started again from it; neither, :class:`InsertTrackLostError` against *last* (the quad of the frame
        before). The read runs on every frame, so its seed is its own reading of the frame before."""
        read, missed = await asyncio.to_thread(_bezel_read, rgb, follower.bezel, one.key)
        if read is not None:
            follower.bezel = read
        why = TOO_LITTLE_DETAIL
        if follower.tracker is not None:
            step = await asyncio.to_thread(follower.tracker.step, grey)
            confidence = step.inliers / max(step.followed, 1)
            if not step.found:
                why = f"only {step.inliers} points of the surface and its bezel still agree"
            elif confidence < MIN_CONFIDENCE:
                why = f"the track's confidence fell to {confidence:.0%} (under {MIN_CONFIDENCE:.0%})"
            else:
                moved = project(step.homography, follower.quad)
                ratio = abs(_area(moved)) / max(one.expected, 1e-6)
                # Its corner farthest off the bezel read there: the alignment gate judges one side as it judges the mean.
                off = float(np.linalg.norm(moved - read, axis=1).max()) / size[0] if read is not None else 0.0
                if not convex(moved) or not 1 / MAX_AREA_CHANGE <= ratio <= MAX_AREA_CHANGE:
                    why = "the tracked surface folds or changes size"
                elif off > BEZEL_READ_MAX_SHARE:
                    # Sure of a quad the bezel this frame shows denies: the alignment gate would fail it on that bezel.
                    why = f"the tracked quad stands {off:.2%} of the frame's width off the bezel the frame shows"
                else:
                    if read is None:
                        # The bezel read lost it on its own (a surface moving faster than its band): the tracker carries it.
                        follower.bezel = moved
                    return moved, False
        if read is None:
            raise lost(one.asked, frame, f"{why}, and {missed}", _evidence(rgb, last))
        follower.tracker, follower.quad = await asyncio.to_thread(opened, grey, read), read
        return read, True

    frames = encoder.iter_rgb_frames(video_path, width=size[0], height=size[1], resize=True)
    with contextlib.closing(behind):
        async with contextlib.aclosing(frames):
            async for raw in frames:
                index += 1
                if on_frame is not None:
                    on_frame()
                if index < start:
                    if progress is not None and index % (PROGRESS_EVERY * 10) == 0:
                        await progress(0, total)
                    continue
                if index > end:
                    break
                if any(one.asked.back_to <= index < one.asked.first for one in following):
                    behind.put(index, raw)
                now = [one for one in following if one.asked.first <= index <= one.asked.last]
                if not now:
                    continue
                if progress is not None and index > start and (index - start) % PROGRESS_EVERY == 0:
                    await progress(followed(), total)
                rgb = np.frombuffer(raw, dtype=np.uint8).reshape(size[1], size[0], 3)
                grey = luma(rgb)
                for one in now:
                    if one.follower is None:
                        if one.asked.key is not None:
                            one.key = await asyncio.to_thread(one.asked.key.sampled, rgb, one.given)
                        one.follower = await follower_of(grey, one.given)
                        # Back from the frame its quad is given on, to the first frame it is held on.
                        back = []
                        if one.asked.back_to < index:
                            follower = await follower_of(grey, one.given)
                            for frame in range(index - 1, one.asked.back_to - 1, -1):
                                earlier = np.frombuffer(behind.get(frame), dtype=np.uint8).reshape(size[1], size[0], 3)
                                last = back[-1] if back else one.given
                                try:
                                    quad, read = await placed(one, follower, luma(earlier), earlier, frame, last)
                                except InsertTrackLostError as exc:
                                    # Before it comes in: held on the quad after, back to its since, said.
                                    one.held_before = frame - one.asked.back_to + 1
                                    back.extend(np.array(last, dtype=float) for _ in range(one.held_before))
                                    logger.info("%s: held before it comes in, %d frames: %s", one.asked.what, one.held_before, exc)
                                    break
                                back.append(quad)
                                if read:
                                    one.by_bezel.append(frame)
                        one.corners.extend([*reversed(back), one.given.copy()])
                        waiting = [other.asked for other in following if other.follower is None]
                        for frame in [frame for frame in behind if not any(item.back_to <= frame < item.first for item in waiting)]:
                            behind.pop(frame)
                        continue
                    quad, read = await placed(one, one.follower, grey, rgb, index, one.corners[-1])
                    one.corners.append(quad)
                    if read:
                        one.by_bezel.append(index)
    tracks = []
    for one in following:
        if not one.corners:
            raise lost(one.asked, one.asked.first, "the footage has no such frame")
        # Each frame's source: a switch between the tracker and the bezel read is blended, never a one-frame step.
        by_bezel = set(one.by_bezel)
        sources = [one.asked.back_to + offset in by_bezel for offset in range(len(one.corners))]
        steady = blended(one.corners, sources)
        smoothed = screens.smooth(steady, [True] * len(steady), window=SMOOTH_WINDOW)
        quads = [[(float(x) / size[0], float(y) / size[1]) for x, y in q] for q in smoothed]
        tracks.append(_boxed(one.asked.back_to, quads, sorted(one.by_bezel), one.held_before))
    return tracks


def _boxed(
    first: int, quads: Sequence[Sequence[tuple[float, float]]], bezel_read: Sequence[int] = (), held_before: int = 0
) -> InsertTrack:
    """An :class:`InsertTrack` of *quads* (0-1) from footage frame *first*, rounded, with the box they all lie in; the
    frames *bezel_read* placed, and the *held_before* first ones held."""
    rounded = tuple(tuple((round(float(x), 6), round(float(y), 6)) for x, y in quad) for quad in quads)
    xs = [x for quad in rounded for x, _y in quad]
    ys = [y for quad in rounded for _x, y in quad]
    return InsertTrack(
        first, rounded, (min(xs), min(ys), max(xs), max(ys)), tuple(int(frame) for frame in bezel_read), int(held_before)
    )


def _outside(quad: Any) -> list[tuple[int, float, float]]:
    """The corners of *quad* (0-1) standing past the frame by more than :data:`CROP_TOLERANCE`: ``(index, x, y)``."""
    low, high = -CROP_TOLERANCE, 1.0 + CROP_TOLERANCE
    return [(n, float(x), float(y)) for n, (x, y) in enumerate(quad) if not (low <= x <= high and low <= y <= high)]


def pane_track(track: InsertTrack, rows: int, cols: int, gutter: float, pane: int) -> InsertTrack:
    """Pane *pane* of a wall followed as *track*: each frame's quad split as ``spec.pane_quads`` splits it."""
    from app.services.video_studio.spec import pane_quads

    return _boxed(
        track.first,
        [pane_quads([tuple(map(float, p)) for p in quad], rows, cols, gutter)[pane] for quad in track.quads],
        track.bezel_read,
        track.held_before,
    )


async def track_stabilised(
    raw_path: str,
    maps: Sequence[Sequence[float]],
    asked: Sequence[Asked],
    *,
    fps: float,
    kept_share: float | None = None,
    progress: Callable[[int, int], Awaitable[None]] | None = None,
    on_frame: Callable[[], None] | None = None,
) -> list[InsertTrack]:
    """Every insert *asked* on a stabilised take followed through its un-stabilised take *raw_path*.

    *asked* quads are on the stabilised take (0-1) and *maps* each frame's
    map from it to the raw take (``stabilise.MAPS``); the quads followed are
    on the stabilised take, where it draws the surface. One standing past
    its frame raises :class:`ScreenCroppedError`, naming the frame, its
    second (*fps*) and what to do: the crop cut the screen (*kept_share*:
    the share of the frame the stabilisation kept), or the camera carries
    it out of the frame.
    """
    import dataclasses

    import numpy as np

    from app.services.video_studio import stabilise, timeline
    from app.services.video_studio.tracking import project

    matrices = [stabilise.map_matrix(list(coefficients)) for coefficients in maps]

    def matrix(frame: int) -> Any:
        return matrices[min(max(frame, 0), len(matrices) - 1)]

    raw = [
        dataclasses.replace(item, quad=project(matrix(item.first), np.asarray(item.quad, dtype=float)).tolist())
        for item in asked
    ]
    followed = await track_all(raw_path, raw, progress=progress, on_frame=on_frame)
    tracks = []
    for item, track in zip(asked, followed, strict=True):
        back = []
        for offset, quad in enumerate(track.quads):
            frame = track.first + offset
            steady = project(np.linalg.inv(matrix(frame)), np.asarray(quad, dtype=float))
            if outside := _outside(steady):
                corner, x, y = outside[0]
                kept = f" (it keeps {kept_share:.0%} of the frame the model made)" if kept_share else ""
                if _outside(quad):
                    remedy = "cut the insert short (duration) before that moment, or give the quad on calmer footage"
                    why = "the shot's camera carries it out of the frame"
                else:
                    remedy = (
                        "generate the shot again with stabilise: false (track: true then follows its camera), or give "
                        "the quad where the stabilised shot shows the whole screen"
                    )
                    why = f"the stabilised shot's crop cuts into it{kept}"
                seconds = frame / max(fps, 1.0)
                raise ScreenCroppedError(
                    f"{item.what}: screen cropped at frame {frame} of its footage ({seconds:.2f} s): its "
                    f"{_CORNERS[corner]} corner ({x:.3f}, {y:.3f}) is outside the frame - {why}; {remedy}.",
                    frame=frame,
                    seconds=seconds,
                    scene_id=item.scene_id,
                    scene_seconds=timeline.map_inverse(list(item.time_map), seconds) if item.time_map else seconds,
                )
            back.append([(float(px), float(py)) for px, py in steady])
        tracks.append(_boxed(track.first, back, track.bezel_read, track.held_before))
    return tracks
