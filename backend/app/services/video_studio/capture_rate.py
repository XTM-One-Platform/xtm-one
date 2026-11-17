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

"""How fast a recording captured what its page painted: measured before step 1, logged, judged at its save.

Chromium's screencast hands over a frame each time the page painted and the
frame before was taken, so its rate falls exactly when a page animates (a
graph laid out again, a panel sliding in) on a worker that cannot encode its
frames fast enough. What this module adds around the recorder (``capture``):

* **Before step 1** (:func:`plan`): the worker's capture rate is measured on a
  :data:`PREFLIGHT_SECONDS` warm-up - a bare Chromium painting a full frame on
  every tick at the take's frame size (:func:`warm_up`). Under the take's
  ``fps`` (:data:`SUSTAIN_SHARE` of it), the take is recorded at pixel ratio
  :data:`LOWERED_RATIO` when that sustains it, and says so. Otherwise
  (:func:`choose`) the largest window that sustains the full ``fps`` is looked
  for in the rates measured on this worker shape (:func:`worker_shape`, a
  table each warm-up adds to for :data:`TABLE_SECONDS`), measured where the
  table does not say (:func:`sustained_size`, :data:`MAX_PROBES` warm-ups at
  most); the take then records at that size (``fps_fallback: size``), at fps
  30 at its own size when that sustains it (``fps``), or is refused naming
  both (:func:`decide`).
* **Beside other jobs** (``recording_lane``): the warm-up runs in the
  take's slot of the recording lane, at the priority its capture records at,
  and keeps what ran beside it on its worker (``preflight.beside``). Short of
  its fps beside scene previews, renders or other recordings, the take waits
  in the lane (``CapturePlan.waits``) - no lower ratio, no smaller window, no
  refusal: those are for what the worker itself sustains. Only a rate measured
  on an idle worker stands in its worker shape's table.
* **While it records** (:func:`gl_backend`): the recording Chromium
  draws WebGL and its canvases on the worker's GPU where Chromium finds one
  (:data:`GPU_GL_ARGS`, kept only where its GPU feature status says WebGL is
  drawn there), on SwiftShader otherwise (:data:`SOFTWARE_GL_ARGS`), so a
  WebGL or canvas graph renders on a worker without a GPU too; the take keeps
  which (``gl``) and the renderer (:data:`RENDERER_JS`).
* **Once recorded** (:func:`measure`, on the frames the take keeps - its
  scrolls drawn again at its rate by ``scroll_pacing``): the frames kept each
  second (``rates``), each step's rate while the page painted
  (``capture_fps`` on its event), and the stretches where it animated under
  :data:`LOW_FPS` for more than :data:`LOW_SECONDS` (:func:`low_spans`) -
  ``low capture rate at step 4 (12 fps for 2.3 s)`` in the take's answer and a
  ``take_capture_rate`` finding where a video shows it. A page animates where
  it painted continuously (no gap past :data:`RUN_GAP`) and consecutive frames
  differ on more than :data:`MOVING_SHARE` of the frame: a keystroke or a caret
  repaints, it does not move. A scroll's stretch is ``scroll_pacing``'s, a CSS
  transition's ``transition_pacing``'s: what neither paces - a canvas
  redrawn on every animation frame, a graph laid out again - is this check's.
  Each canvas a step showed is measured on its own pixels too
  (:func:`canvas_motion`): its transitions while the pointer rests, their rate
  at their step in the action log, one under :data:`LOW_FPS` said like a
  stretch of the whole frame, however short past :data:`CANVAS_SECONDS`.
* **Slowed down** (:func:`sparse_spans`): the stretches painted under the take's
  rate keep their painted instants, so a recording scene played under speed 1
  blends the painted frames on either side (``compositor_page``) instead of
  holding each one longer.
* **Stepping** (:func:`stepping`): the motion no pacer draws again - a
  canvas transition, a stretch the page animated on its own - plays the frames
  captured at their instants, each held to the next
  (``encoder.frames_to_mezzanine``): a frame of the take's rate with none
  captured in it is synthesised from the keyframe before. Where more than
  :data:`STEPPING_SHARE` of a stretch's frames are, its motion steps on screen:
  ``stepping motion at step 1 (canvas#graph moved 1.5 s: 33 of its 46 frames at
  30 fps synthesised from keyframes)`` in the take's notes and a
  ``stepping_motion`` finding where a video shows it. On SwiftShader and on a
  GPU, a canvas transition the screencast fell short on is captured alone first
  (:attr:`GlBackend.alone`, ``canvas_alone``): the frames composited from it
  are captured ones. On GPU GL under ``fps_fallback: refuse``, a stretch still
  read as steps once composited (:func:`refusal`) refuses the take with the
  rate it was painted at instead of keeping motion synthesised from keyframes.
* **Capture faults** (:func:`capture_fault`): a stretch read as motion
  on a step that holds the page (:data:`HOLD_STEPS`), the frames about it
  alike (SSIM :data:`FAULT_ALIKE_SSIM`), is frames drawn at another scale, not
  a motion of the page: kept as ``off_scale`` - ``off-scale frames at step 24
  (pause, 103.0-103.4 s, 0.4 s, 2 frame(s))`` in the take's notes and an
  ``off_scale_frames`` finding where a video shows it, the take to record
  again -, never as stepping motion. The recorder drops the frames the browser
  painted at another size or scale before (``capture._Screencast``) and says
  their runs (:func:`dropped_runs`, ``off_scale_dropped``).
"""

from __future__ import annotations

import asyncio
import bisect
import contextlib
import itertools
import logging
import math
import os
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from statistics import median
from typing import Any

from app.core.config import settings
from app.core.utils import format_exception_message
from app.services.video_studio.scroll_pacing import (
    PROBE_TAIL,
    canvas_limited,
    held_back_over,
    kept_every_drawn,
    mark_probes,
    mark_quota,
    mark_unclassified,
    page_drawn_rate,
    page_frame_times,
    page_limited_fps,
    page_limited_kept,
    page_rate,
    probe_kept,
    refused_cause,
)

logger = logging.getLogger(__name__)

#: Frames a second under which a take's animation steps on screen, and the seconds it must stay there to be said.
LOW_FPS = 20.0
LOW_SECONDS = 1.0
#: Seconds between two frames past which the page stopped painting: a still page sends no frame.
RUN_GAP = 0.5
#: The intervals averaged around each one for the rate it was painted at.
RATE_WINDOW = 6
#: The share of a frame two consecutive frames must differ on for the page to move there, and the grey levels a pixel
#: changes by to count: a keystroke or a caret repaints a few pixels, a graph laid out again moves.
MOVING_SHARE = 0.005
MOVING_LEVELS = 12
#: The scale frames are compared at (a JPEG decodes that small directly).
_COMPARED_AT = 4
#: A canvas transition (:func:`canvas_motion`): at least this many frames moving the canvas, over this many
#: seconds at least; the most a step's event keeps; the scale its frames are read at (every frame of a step showing a
#: canvas is).
CANVAS_FRAMES = 5
CANVAS_SECONDS = 0.4
MOST_CANVAS_RUNS = 6
_CANVAS_COMPARED_AT = 8
#: How long past the cursor's last move what a canvas draws is still the pointer's (a hover's repaint), and the gap
#: between two cursor samples past which they are two moves (:func:`pointer_spans`).
POINTER_TAIL = 0.15
_POINTER_GAP = 0.25
#: A stretch of motion no pacer draws again steps on screen where more than this share of its frames at the take's
#: rate were synthesised from keyframes (:func:`stepping`).
STEPPING_SHARE = 0.5
#: A stretch of stepping motion the audience still reads as motion (:func:`stepping_grade`): a transition no
#: longer than this many seconds of the take, painted at this many frames a second of output at least, no keyframe
#: held longer than this on screen - advice, never a finding the render waits for.
STEPPING_BRIEF_SECONDS = 1.25
STEPPING_ADVISORY_FPS = 10.0
STEPPING_HOLD_SECONDS = 0.25
#: The frames composited from a canvas captured alone are named from this (``canvas_alone.composite``).
COMPOSITED_PREFIX = "a"
#: The steps that hold the page or rest the pointer on it: a stretch the whole frame moved over there, between
#: two frames alike - :data:`FAULT_ALIKE_SSIM` of structural similarity at least (``frame_qc.ssim``) -, is a capture
#: fault (:func:`capture_fault`); the most frame names and runs a take keeps of them.
HOLD_STEPS = frozenset({"wait", "pause", "highlight", "hover"})
#: The steps that only let the page it opened on load (:func:`loading`): no pointer on it, nothing acted on.
LOAD_STEPS = frozenset({"wait", "pause"})
FAULT_ALIKE_SSIM = 0.98
MOST_FAULT_FRAMES = 12
MOST_FAULTS = 40
#: A frame between two alike is off-scale where it differs from them on this share of the frame at least
#: (:func:`moved_share`): a whole page drawn at another scale, never a hover card or a caret.
FAULT_MOVED_SHARE = 0.25
#: The most frames of a stretch a capture fault reads (:func:`capture_fault`).
_FAULT_READ_MOST = 240
#: A stretch painted under this share of the take's fps, at least :data:`SPARSE_SECONDS` long, keeps its painted
#: instants for a slowed scene to blend (:func:`sparse_spans`); the most instants kept in all.
SPARSE_SHARE = 0.9
SPARSE_SECONDS = 0.25
MAX_SPARSE_TIMES = 6000
#: The share of a take's fps the warm-up must reach for the take to be recorded at its size.
SUSTAIN_SHARE = 0.85
#: Seconds the warm-up counts frames for, after the seconds it lets the page start painting.
PREFLIGHT_SECONDS = 2.0
PREFLIGHT_SETTLE_SECONDS = 0.5
#: The JPEG quality the warm-up's screencast asks for: the take's own (``capture._SCREENCAST_QUALITY``).
WARM_UP_QUALITY = 95
#: The pixel ratio a take the worker cannot capture at its own is recorded at, when that sustains it.
LOWERED_RATIO = 1.5
#: The pixel ratios a recording takes, largest first: where no window at a refused take's sustains it, a lower one.
RATIOS = (2.0, 1.5, 1.0)
#: The seconds a rate measured at a size stands in this worker shape's table (:func:`choose`).
TABLE_SECONDS = 900.0
#: The most warm-ups a refused take's search for the size that sustains its fps runs (:func:`sustained_size`), and
#: how close to the smallest size that did not it stops: the size named is within that share of the largest.
MAX_PROBES = 5
SEARCH_TOLERANCE = 0.05
#: The rate a recording falls back to (``fps_fallback: fps``).
FALLBACK_FPS = 30
#: Launch flags of the recording Chromium drawing on the CPU: a canvas or WebGL graph drawn by SwiftShader through
#: ANGLE, the blocklist that would turn it off ignored.
SOFTWARE_GL_ARGS = ("--use-gl=angle", "--use-angle=swiftshader-webgl", "--ignore-gpu-blocklist")
#: Launch flags of the recording Chromium drawing on the worker's GPU: ANGLE on its Vulkan driver, with no
#: window surface (headless). Kept only where Chromium then draws WebGL on a GPU (:func:`gl_backend`): on a worker
#: without one it draws on SwiftShader under them.
GPU_GL_ARGS = ("--use-angle=vulkan", "--enable-features=Vulkan", "--disable-vulkan-surface", "--ignore-gpu-blocklist")
#: The second launch a recording lane (``VIDEO_STUDIO_RECORDING_GL=gpu``) tries when ANGLE on Vulkan did not
#: reach the GPU: ANGLE on the driver's EGL (an NVIDIA container runtime with the ``graphics`` capability mounts
#: libEGL_nvidia).
GPU_EGL_ARGS = ("--use-gl=angle", "--use-angle=gl-egl", "--ignore-gpu-blocklist")
#: The GLVND EGL vendor file of the NVIDIA driver (Dockerfile.worker writes it).
NVIDIA_EGL_VENDOR_FILE = "/usr/share/glvnd/egl_vendor.d/10_nvidia.json"
#: The environment of a Chromium launched under :data:`GPU_EGL_ARGS`: GLVND loads the NVIDIA vendor alone, on
#: the surfaceless platform. Left to itself GLVND tries every vendor file and Mesa (``50_mesa.json``) answers when the
#: NVIDIA one cannot open its device: ANGLE then draws on llvmpipe and reads ``enabled``. With the vendor named, the
#: EGL launch draws on the NVIDIA driver or fails, never on a CPU rasterizer it takes for a GPU.
GPU_EGL_ENV = {"__EGL_VENDOR_LIBRARY_FILENAMES": NVIDIA_EGL_VENDOR_FILE, "EGL_PLATFORM": "surfaceless"}
#: ``VIDEO_STUDIO_RECORDING_GL`` values: the GPU where found, SwiftShader always, the GPU or no recording.
GL_MODES = ("auto", "software", "gpu")
#: What a WebGL renderer names when it rasterizes on the CPU: Chromium's SwiftShader, Mesa's llvmpipe and
#: softpipe (GL), lavapipe (Vulkan). Chromium reads ``enabled`` for WebGL on any of the Mesa ones, so its feature status
#: alone takes a CPU rasterizer for a GPU.
SOFTWARE_RENDERERS = ("swiftshader", "llvmpipe", "softpipe", "lavapipe")
#: How Chromium's GPU feature status (``SystemInfo.getInfo``) names a feature drawn on a GPU: ``enabled``,
#: ``enabled_on``, ``enabled_readback``... SwiftShader's reads ``unavailable_software``.
ON_GPU_STATUS = "enabled"
#: What renders WebGL in the page: the renderer its debug extension names, ``none`` without WebGL.
RENDERER_JS = """() => {
  try {
    const canvas = document.createElement('canvas');
    const gl = canvas.getContext('webgl2') || canvas.getContext('webgl');
    if (!gl) return 'none';
    const info = gl.getExtension('WEBGL_debug_renderer_info');
    return String(info ? gl.getParameter(info.UNMASKED_RENDERER_WEBGL) : gl.getParameter(gl.RENDERER));
  } catch (e) { return 'none'; }
}"""
#: The warm-up page: a canvas the size of the window painting the whole of it on every tick.
_WARM_UP_PAGE = """<!doctype html><html><body style="margin:0;background:#0b0f1a;overflow:hidden">
<canvas id="c"></canvas><script>
const c = document.getElementById('c'), g = c.getContext('2d');
c.width = innerWidth * devicePixelRatio; c.height = innerHeight * devicePixelRatio;
c.style.width = innerWidth + 'px'; c.style.height = innerHeight + 'px';
let n = 0;
(function paint() {
  n += 1;
  g.fillStyle = '#0b0f1a'; g.fillRect(0, 0, c.width, c.height);
  for (let i = 0; i < 360; i++) {
    g.fillStyle = `hsl(${(i * 37 + n * 5) % 360} 55% ${35 + (i % 5) * 8}%)`;
    g.fillRect((i * 97 + n * 7) % c.width, (i * 53 + n * 3) % c.height, 160, 64);
  }
  g.fillStyle = '#f4f6fa'; g.font = `${18 * devicePixelRatio}px sans-serif`;
  for (let y = 40; y < c.height; y += 48 * devicePixelRatio) g.fillText('Capture warm-up ' + n, (n * 9) % 300, y);
  requestAnimationFrame(paint);
})();
</script></body></html>"""


# ── Measured once recorded ────────────────────────────────────────────────


def per_second(times: list[float], duration: float) -> list[int]:
    """The frames kept in each second of a take of *duration* seconds, *times* their instants."""
    counts = [0] * max(1, math.ceil(duration))
    for t in times:
        counts[min(max(int(t), 0), len(counts) - 1)] += 1
    return counts


def runs(times: list[float], *, gap: float = RUN_GAP) -> list[tuple[int, int]]:
    """``[start, end)`` index ranges of the frames the page painted without stopping: no two further apart than *gap*."""
    found, start = [], 0
    for k in range(1, len(times) + 1):
        if k == len(times) or times[k] - times[k - 1] > gap:
            if k - start >= 2:
                found.append((start, k))
            start = k
    return found


def slow_stretches(times: list[float], *, floor: float, least: float, gap: float = RUN_GAP) -> list[tuple[int, int]]:
    """``[first, last]`` index pairs of the frames bounding each stretch painted without stopping at under *floor*
    frames a second (each interval's rate averaged over :data:`RATE_WINDOW` around it) for *least* seconds at least."""
    found: list[tuple[int, int]] = []
    half = RATE_WINDOW // 2
    for start, end in runs(times, gap=gap):
        gaps = [b - a for a, b in itertools.pairwise(times[start:end])]
        slow = []
        for i in range(len(gaps)):
            window = gaps[max(0, i - half) : i + half + 1]
            total = sum(window)
            slow.append(total > 0 and len(window) / total < floor)
        i = 0
        while i < len(gaps):
            if not slow[i]:
                i += 1
                continue
            j = i
            while j < len(gaps) and slow[j]:
                j += 1
            if times[start + j] - times[start + i] >= least:
                found.append((start + i, start + j))
            i = j
    return found


def _grey(path: Path, *, at: int = _COMPARED_AT) -> Any:
    import numpy as np
    from PIL import Image

    with Image.open(path) as image:
        size = (max(1, image.width // at), max(1, image.height // at))
        image.draft("L", size)
        grey = image.convert("L")
        if grey.size != size:
            grey = grey.resize(size)
        return np.asarray(grey, dtype=np.int16)


def moved_share(a: Any, b: Any) -> float:
    """The share of two grey frames that differs by more than :data:`MOVING_LEVELS`."""
    import numpy as np

    if a.shape != b.shape:
        return 1.0
    return float(np.count_nonzero(np.abs(a - b) > MOVING_LEVELS)) / max(1, a.size)


def moving(frames: list[tuple[float, str]], first: int, last: int, workdir: str) -> bool:
    """Whether the page moves between frames *first* and *last* (indices of *frames*): consecutive frames differing
    on :data:`MOVING_SHARE` of the frame or more, by the median; a frame that cannot be read counts as moving."""
    shares, previous = [], None
    for _t, name in frames[first : last + 1]:
        try:
            grey = _grey(Path(workdir) / name)
        except Exception:  # a frame not read: judged on its rate alone
            return True
        if previous is not None:
            shares.append(moved_share(previous, grey))
        previous = grey
    return bool(shares) and median(shares) >= MOVING_SHARE


def paced_spans(events: list[dict], *, transitions: bool = True) -> list[tuple[float, float]]:
    """The spans a step scrolled over or - with *transitions* - a transition of its page ran over (``scroll_pacing`` /
    ``transition_pacing`` pace them, ``take_frame_rate`` judges them)."""
    spans = []
    for event in events:
        for window in [*(event.get("scrolls") or []), *((event.get("motion") or []) if transitions else [])]:
            span = window.get("t") if isinstance(window, dict) else window
            if isinstance(span, list | tuple) and len(span) >= 2:
                spans.append((float(span[0]), float(span[1])))
        scrolled = event.get("scrolled")
        if isinstance(scrolled, list) and len(scrolled) == 2:
            spans.append((float(scrolled[0]), float(scrolled[1])))
    return spans


#: The steps that play nothing (``spec`` names a point of the script): never the step a take was playing, so never
#: the one a motion, a low rate or a stretch of stepping motion is named by (#9744: a canvas's late redraw after a
#: wheel named by the marker logged after it).
UNPLAYED_STEPS = frozenset({"marker"})


def step_at(events: list[dict], t: float) -> dict | None:
    """The step a take was playing at *t* seconds: the last one started by then, a marker (:data:`UNPLAYED_STEPS`)
    passed - it plays nothing, the step before it still plays."""
    found = None
    for event in events:
        if str(event.get("action") or "") in UNPLAYED_STEPS:
            continue
        if float(event.get("t0") or 0.0) <= t + 1e-6:
            found = event
    return found


def loading(events: list[dict], t: float) -> bool:
    """Whether a take was still letting the page it opened on load at *t* seconds: the step it was playing then only
    waits (:data:`LOAD_STEPS`) and every step before it did too - nothing hovered, clicked, wheeled or typed yet, so
    what moves there is the page loading (a graph's layout physics), never a motion the script made. Before the
    first step started, the page is loading too."""
    if not events:
        return False
    playing = step_at(events, t)
    if playing is None:
        return True
    for event in events:
        if str(event.get("action") or "") in UNPLAYED_STEPS:
            continue
        if str(event.get("action") or "") not in LOAD_STEPS:
            return False
        if event is playing:
            return True
    return False


@dataclass(frozen=True)
class LowSpan:
    """A stretch a take animated under :data:`LOW_FPS` (:func:`low_spans`)."""

    start: float
    end: float
    fps: float
    step: int | None
    label: str | None = None
    #: The canvas it was measured on (:func:`canvas_motion`); ``None``: the whole frame.
    canvas: str | None = None
    #: Drawn on SwiftShader (the take's ``capture.gl``).
    software: bool = False
    #: Why a canvas transition stayed on the screencast instead of its copies alone (:data:`NO_COPY`, or the ``why``
    #: of its ``canvas_alone`` run); ``None`` where the take copied no canvas alone.
    alone: str | None = None

    @property
    def seconds(self) -> float:
        return self.end - self.start

    def words(self) -> str:
        """``low capture rate at step 4 (12 fps for 2.3 s)``; on a canvas ``... (canvas#graph moved at 9 fps for
        0.8 s)``; drawn on SwiftShader ``... for 0.8 s, software GL)``; a canvas left on the screencast ``... for
        0.8 s; on the screencast, not captured alone: <why>)``."""
        at = f"at step {self.step}" if self.step is not None else f"at {self.start:.1f} s"
        rate = f"{self.canvas} moved at {self.fps:.0f} fps" if self.canvas else f"{self.fps:.0f} fps"
        why = f"; on the screencast, not captured alone: {self.alone}" if self.alone else ""
        return f"low capture rate {at} ({rate} for {self.seconds:.1f} s{', software GL' if self.software else ''}{why})"

    def as_dict(self) -> dict:
        return {
            "t": [round(self.start, 3), round(self.end, 3)],
            "fps": round(self.fps, 1),
            "step": self.step,
            **({"label": self.label} if self.label else {}),
            **({"canvas": self.canvas} if self.canvas else {}),
            **({"gl": "software"} if self.software else {}),
            **({"alone": self.alone} if self.alone else {}),
        }


def low_spans(frames: list[tuple[float, str]], events: list[dict], workdir: str) -> list[LowSpan]:
    """Each stretch of *frames* (``(t, file name)``) where the page animated under :data:`LOW_FPS` for more than
    :data:`LOW_SECONDS`, outside the steps' scrolls and transitions; named by the step it falls in."""
    times = [t for t, _name in frames]
    paced = paced_spans(events)
    found = []
    for first, last in slow_stretches(times, floor=LOW_FPS, least=LOW_SECONDS):
        start, end = times[first], times[last]
        if any(a < end and b > start for a, b in paced) or not moving(frames, first, last, workdir):
            continue
        event = step_at(events, start)
        found.append(
            LowSpan(
                start,
                end,
                (last - first) / (end - start),
                event.get("step") if event else None,
                (event or {}).get("label"),
            )
        )
    return found


@dataclass(frozen=True)
class CanvasRun:
    """One transition of a canvas (:func:`canvas_motion`): the frames that moved it, one after the other, while
    the pointer rested - its rate the canvas's own, whatever else the page painted meanwhile."""

    canvas: str
    start: float
    end: float
    frames: int
    step: int | None
    label: str | None = None
    #: Most of its frames composited from the canvas captured alone (``canvas_alone``).
    alone: bool = False
    #: The longest two of its frames are apart, in seconds: the longest the screen holds one of its keyframes.
    hold: float = 0.0
    #: Not captured alone on a take that copies its canvases alone: why it stayed on the screencast
    #: (:func:`screencast_why`).
    screencast: str | None = None

    @property
    def fps(self) -> float:
        return (self.frames - 1) / max(self.end - self.start, 1e-6)

    def as_dict(self) -> dict:
        return {
            "canvas": self.canvas,
            "t": [round(self.start, 3), round(self.end, 3)],
            "fps": round(self.fps, 1),
            "frames": self.frames,
            **({"alone": True} if self.alone else {}),
            **({"screencast": self.screencast} if self.screencast and not self.alone else {}),
        }

    def low(self) -> LowSpan:
        return LowSpan(
            self.start,
            self.end,
            self.fps,
            self.step,
            self.label,
            canvas=self.canvas,
            alone=None if self.alone else self.screencast,
        )


#: Why a canvas transition stayed on the screencast when the take copied canvases alone but made no run of copies over
#: it (its copies too few, too far apart, or none: the canvas was not drawn by the calls the tap marks).
NO_COPY = "no run of its copies while it moved"


def screencast_why(run: CanvasRun, recorded: dict | None) -> str | None:
    """Why *run*, a canvas transition not captured alone, stayed on the screencast: the ``why`` of the take's
    ``canvas_alone`` run over it, :data:`NO_COPY` when none covers it; ``None`` on a take that copied no canvas
    alone (recorded before it, or its copies off: the take's ``alone.off`` says why once)."""
    if run.alone:
        return None
    alone = (recorded or {}).get("alone")
    if not isinstance(alone, dict) or alone.get("off"):
        return None
    for entry in alone.get("runs") or []:
        if not isinstance(entry, dict) or not entry.get("why"):
            continue
        try:
            start, end = (float(v) for v in entry["t"])
        except (KeyError, TypeError, ValueError):
            continue
        if start < run.end and end > run.start:
            return str(entry["why"])
    return NO_COPY


def pointer_spans(cursor: list | None) -> list[tuple[float, float]]:
    """The spans the cursor moved over (``(t, x, y)`` samples), each to :data:`POINTER_TAIL` past its last move:
    what a canvas draws then is the pointer's (a hover, a drag), not a transition of its own."""
    spans: list[tuple[float, float]] = []
    for before, after in itertools.pairwise(cursor or []):
        t0, t1 = float(before[0]), float(after[0])
        if (
            t1 - t0 > _POINTER_GAP
            or math.hypot(float(after[1]) - float(before[1]), float(after[2]) - float(before[2])) < 0.5
        ):
            continue
        if spans and t0 <= spans[-1][1]:
            spans[-1] = (spans[-1][0], max(spans[-1][1], t1 + POINTER_TAIL))
        else:
            spans.append((t0, t1 + POINTER_TAIL))
    return spans


def canvas_motion(
    frames: list[tuple[float, str]],
    events: list[dict],
    *,
    workdir: str,
    viewport_width: float,
    cursor: list | None = None,
) -> list[CanvasRun]:
    """Each transition of a canvas the take showed, on the *frames* its browser painted: on the frames of each
    step, the canvases it ended on (``canvases`` on its event, in CSS px of a *viewport_width* wide window) cropped
    from each frame, a frame moving a canvas where :data:`MOVING_SHARE` of its pixels changed from the frame before. A
    transition is at least :data:`CANVAS_FRAMES` frames moving it over :data:`CANVAS_SECONDS`, none further apart
    than :data:`RUN_GAP`, named by the step it started in. The frames of the steps' scrolls (a scroll moves a canvas,
    a wheel zooms it: both drawn again at the take's rate) and those the cursor moved over (:func:`pointer_spans`)
    belong to no transition. One whose moving frames were mostly composited from the canvas captured alone
    (:data:`COMPOSITED_PREFIX`) says so (``alone``)."""
    from bisect import bisect_right

    from app.services.video_studio.page_settle import canvas_key

    if not frames or viewport_width <= 0:
        return []
    # The canvases a frame shows are those of the step playing then: a marker plays nothing (#9744).
    events = [event for event in events if str(event.get("action") or "") not in UNPLAYED_STEPS]
    starts = [float(event.get("t0") or 0.0) for event in events]
    skipped = [*paced_spans(events, transitions=False), *pointer_spans(cursor)]
    last: dict[str, Any] = {}
    moving_at: dict[str, list[float]] = {}
    composited: dict[str, int] = {}
    names: dict[str, str] = {}
    found: list[CanvasRun] = []

    def close(key: str) -> None:
        times = moving_at.pop(key, None)
        alone = composited.pop(key, 0) * 2 > len(times or [])
        if times and len(times) >= CANVAS_FRAMES and times[-1] - times[0] >= CANVAS_SECONDS:
            event = step_at(events, times[0]) or {}
            hold = max((b - a for a, b in itertools.pairwise(times)), default=0.0)
            found.append(
                CanvasRun(
                    names[key], times[0], times[-1], len(times), event.get("step"), event.get("label"), alone, hold
                )
            )

    for t, name in frames:
        index = bisect_right(starts, t + 1e-6) - 1
        shown = (events[index].get("canvases") or []) if index >= 0 else []
        if not shown or any(a <= t <= b for a, b in skipped):
            for key in list(moving_at):
                close(key)
            last.clear()
            continue
        try:
            grey = _grey(Path(workdir) / name, at=_CANVAS_COMPARED_AT)
        except Exception as exc:  # a frame not read: the next one is compared with the last read
            logger.debug("Frame %s not read for its canvases: %s", name, format_exception_message(exc))
            continue
        scale = grey.shape[1] / viewport_width
        seen: set[str] = set()
        for canvas, box in (
            (entry[0], entry[1]) for entry in shown if isinstance(entry, list | tuple) and len(entry) == 2
        ):
            x, y, width, height = (float(v) for v in box)
            crop = grey[
                max(0, round(y * scale)) : round((y + height) * scale),
                max(0, round(x * scale)) : round((x + width) * scale),
            ]
            if crop.size == 0:
                continue
            key = canvas_key(str(canvas), (x, y, width, height))
            seen.add(key)
            names[key] = str(canvas)
            before, last[key] = last.get(key), crop
            if before is None or before.shape != crop.shape or moved_share(before, crop) < MOVING_SHARE:
                continue
            times = moving_at.get(key)
            if times and t - times[-1] > RUN_GAP:
                close(key)
            moving_at.setdefault(key, []).append(t)
            if name.startswith(COMPOSITED_PREFIX):
                composited[key] = composited.get(key, 0) + 1
        for key in [key for key in moving_at if key not in seen]:
            close(key)
        for key in [key for key in last if key not in seen]:
            del last[key]
    for key in list(moving_at):
        close(key)
    return sorted(found, key=lambda run: run.start)


def canvas_words(motion: list | None) -> str:
    """``canvas#graph moved 1.4 s at 9.8 fps``: each canvas transition a step's event keeps (``canvas_motion``);
    ``... at 29.6 fps, captured alone`` where its frames were composited from the canvas captured alone, ``... at
    18.0 fps, on the screencast (copied no faster than painted)`` where the take copied canvases alone and this one
    stayed on the screencast (``screencast``): each transition says which path captured it and at what rate."""
    said = []
    for run in motion or []:
        try:
            start, end = (float(v) for v in run["t"])
            if run.get("alone"):
                alone = ", captured alone"
            elif run.get("screencast"):
                alone = f", on the screencast ({run['screencast']})"
            else:
                alone = ""
            said.append(f"{run['canvas']} moved {end - start:.1f} s at {float(run['fps']):.1f} fps{alone}")
        except (KeyError, TypeError, ValueError):
            continue
    return "; ".join(said)


def frames_at(start: float, end: float, fps: int) -> int:
    """The frames of a take at *fps* over ``[start, end]`` seconds: its instants ``n / fps`` there, one at least."""
    return max(1, math.floor(end * fps + 1e-6) - math.ceil(start * fps - 1e-6) + 1)


@dataclass(frozen=True)
class Stepping:
    """A stretch of a take's motion no pacer drew again whose frames at its rate were mostly synthesised from its
    keyframes (:func:`stepping`): each held from the one captured before, a step at a time on screen."""

    start: float
    end: float
    #: Its frames at the take's rate, and those of them with no frame captured in them.
    frames: int
    synthesised: int
    fps: int
    step: int | None
    label: str | None = None
    #: The canvas whose transition it is (:func:`canvas_motion`); ``None``: the whole frame moved.
    canvas: str | None = None
    #: The longest one of its keyframes is held on screen, in seconds of the take; ``None``: not measured.
    hold: float | None = None

    @property
    def share(self) -> float:
        return self.synthesised / max(1, self.frames)

    def words(self) -> str:
        """``stepping motion at step 1 (canvas#graph moved 1.5 s: 33 of its 46 frames at 30 fps synthesised from
        keyframes)``."""
        at = f"at step {self.step}" if self.step is not None else f"at {self.start:.1f} s"
        what = f"{self.canvas} moved" if self.canvas else "the page moved"
        return (
            f"stepping motion {at} ({what} {self.end - self.start:.1f} s: {self.synthesised} of its {self.frames} "
            f"frames at {self.fps} fps synthesised from keyframes)"
        )

    def as_dict(self) -> dict:
        return {
            "t": [round(self.start, 3), round(self.end, 3)],
            "frames": self.frames,
            "synthesised": self.synthesised,
            "share": round(self.share, 3),
            "fps": self.fps,
            "step": self.step,
            **({"label": self.label} if self.label else {}),
            **({"canvas": self.canvas} if self.canvas else {}),
            **({"hold": round(self.hold, 3)} if self.hold is not None else {}),
        }


def stepping(
    frames: list[tuple[float, str]], events: list[dict], runs: list[CanvasRun], *, fps: int, workdir: str
) -> list[Stepping]:
    """Each stretch of a take's motion no pacer drew again whose frames at *fps* were more than
    :data:`STEPPING_SHARE` synthesised from keyframes: each canvas transition (*runs*, :func:`canvas_motion`), its
    frames those that moved it; each stretch of the whole frame (*frames*) moving outside the steps' scrolls and
    transitions, painted under the rate that would leave no more than that share synthesised for
    :data:`CANVAS_SECONDS` at least, and over no canvas transition - judged on its canvas's own pixels, whatever its
    rate. Named by the step it started in."""
    found = []
    for run in runs:
        total = frames_at(run.start, run.end, fps)
        if (held := max(0, total - run.frames)) > STEPPING_SHARE * total:
            found.append(Stepping(run.start, run.end, total, held, fps, run.step, run.label, run.canvas, run.hold))
    times = [t for t, _name in frames]
    judged = [*paced_spans(events), *((run.start, run.end) for run in runs)]
    for first, last in slow_stretches(times, floor=(1 - STEPPING_SHARE) * fps, least=CANVAS_SECONDS):
        start, end = times[first], times[last]
        if any(a < end and b > start for a, b in judged):
            continue
        total = frames_at(start, end, fps)
        held = max(0, total - (last - first + 1))
        if held <= STEPPING_SHARE * total or not moving(frames, first, last, workdir):
            continue
        event = step_at(events, start) or {}
        hold = max((b - a for a, b in itertools.pairwise(times[first : last + 1])), default=0.0)
        found.append(Stepping(start, end, total, held, fps, event.get("step"), event.get("label"), hold=hold))
    return sorted(found, key=lambda stretch: stretch.start)


@dataclass(frozen=True)
class SteppingGrade:
    """How a stretch of stepping motion reads where a scene plays it (:func:`stepping_grade`): over how many
    seconds of output, at how many painted frames a second of output, its longest keyframe held how long on screen,
    and whether the audience reads it as motion (``advisory``) or as steps (``dense``)."""

    seconds: float
    painted_fps: float
    hold: float
    severity: str
    #: The page settling on a step that holds it (``settling``): widgets still drawing, never a motion followed.
    settling: bool = False

    def words(self) -> str:
        """``painted at 13 fps over 1.0 s on screen, a keyframe held 0.08 s at most``."""
        return (
            f"painted at {self.painted_fps:.0f} fps over {self.seconds:.1f} s on screen, a keyframe held "
            f"{self.hold:.2f} s at most"
        )


def stepping_grade(stretch: dict, rate: float = 1.0) -> SteppingGrade | None:
    """How a stretch of a take's ``stepping`` (:func:`stepping`) reads in a scene playing the take at *rate* seconds of
    the take a second of output there: ``advisory`` - a transition of :data:`STEPPING_BRIEF_SECONDS` of the
    take at most, painted at :data:`STEPPING_ADVISORY_FPS` a second of output at least, no keyframe held past
    :data:`STEPPING_HOLD_SECONDS` of output - or ``dense``. A stretch kept before its ``hold`` was measured is held
    for its mean painted interval. ``None`` for a stretch that cannot be read. A stretch of the page settling on a
    step that holds it (``settling``: a ``wait`` whose charts, skeletons or counters were still drawing) is
    ``advisory`` whatever its rate: the audience follows no motion there, and a render never waits for it."""
    try:
        start, end = (float(v) for v in stretch["t"])
        frames, synthesised, fps = int(stretch["frames"]), int(stretch["synthesised"]), int(stretch["fps"])
        hold = float(stretch["hold"]) if stretch.get("hold") is not None else None
    except (KeyError, TypeError, ValueError):
        return None
    rate = rate if rate > 0 else 1.0
    source = max(end - start, 1.0 / max(fps, 1))
    painted = max(1, frames - synthesised) / source
    if hold is None:
        hold = 1.0 / painted
    seconds, painted_out, hold_out = source / rate, painted * rate, hold / rate
    if stretch.get("settling"):
        return SteppingGrade(seconds, painted_out, hold_out, "advisory", settling=True)
    reads = (
        source <= STEPPING_BRIEF_SECONDS and painted_out >= STEPPING_ADVISORY_FPS and hold_out <= STEPPING_HOLD_SECONDS
    )
    return SteppingGrade(seconds, painted_out, hold_out, "advisory" if reads else "dense")


def software_gl_notice(fps: float | int | None, capture: dict | None) -> str:
    """What a 60 fps recording says on a worker drawing its canvases on software GL: its canvas transitions
    will step, and fps 30 is the fallback. Empty at 30 fps, or where its canvases were not drawn on software GL
    (*capture*'s ``gl``; a take recorded before it was kept says nothing)."""
    if float(fps or 0) < 50 or ((capture or {}).get("gl") or {}).get("gpu") is not False:
        return ""
    return (
        "This deployment records canvases on software GL: 60 fps canvas transitions will step (a save says each one "
        "as stepping motion). Record at fps 30 instead - or with fps_fallback: fps - unless the page draws no canvas; "
        "a recording lane on GPU workers (VIDEO_STUDIO_RECORDING_GL) records them at 60 fps."
    )


def besides(spans: list | None, stretches: list | None) -> list:
    """The stretches of *spans* (``{t: [start, end], ...}``) none of *stretches* overlaps: a stretch said stepping
    is never said low as well."""
    covered = []
    for stretch in stretches or []:
        with contextlib.suppress(KeyError, TypeError, ValueError):
            covered.append((float(stretch["t"][0]), float(stretch["t"][1])))
    kept = []
    for span in spans or []:
        try:
            start, end = (float(v) for v in span["t"])
        except (KeyError, TypeError, ValueError):
            continue
        if not any(a < end and b > start for a, b in covered):
            kept.append(span)
    return kept


def stepping_words(capture: dict | None) -> list[str]:
    """``stepping motion at step 1 (...)`` for each stretch a take's *capture* found mostly synthesised; a
    canvas captured alone that was not composited there says why (``alone``)."""
    said = []
    for stretch in (capture or {}).get("stepping") or []:
        if not isinstance(stretch, dict):
            continue
        try:
            start, end = (float(v) for v in stretch["t"])
            step = stretch.get("step")
            found = Stepping(
                start,
                end,
                int(stretch["frames"]),
                int(stretch["synthesised"]),
                int(stretch["fps"]),
                int(step) if step is not None else None,
                canvas=str(stretch["canvas"]) if stretch.get("canvas") else None,
            )
        except (KeyError, TypeError, ValueError):
            continue
        words = found.words()
        if stretch.get("alone"):
            words = f"{words[:-1]}; not captured alone: {stretch['alone']})"
        said.append(words)
    return said


def sparse_spans(frames: list[tuple[float, str]], fps: int, workdir: str) -> list[list]:
    """``[[start, end, [instants]], ...]`` of each stretch painted under :data:`SPARSE_SHARE` of *fps* while the page
    moved, in seconds of the take's encoded video (from its first frame): what a slowed scene blends between."""
    if not frames:
        return []
    times = [t for t, _name in frames]
    origin = times[0]
    found, kept = [], 0
    for first, last in slow_stretches(times, floor=SPARSE_SHARE * fps, least=SPARSE_SECONDS):
        if kept + last - first + 1 > MAX_SPARSE_TIMES or not moving(frames, first, last, workdir):
            continue
        instants = [round(t - origin, 4) for t in times[first : last + 1]]
        found.append([instants[0], instants[-1], instants])
        kept += len(instants)
    return found


def step_rates(events: list[dict], times: list[float], *, least: float = SPARSE_SECONDS) -> None:
    """Each step's rate while its page painted (``capture_fps`` on its event, in place): its frames in the runs
    painted without stopping over its span - to the next step's start - per second of them; none under *least*."""
    spans = runs(times)
    starts = [float(event.get("t0") or 0.0) for event in events]
    for index, event in enumerate(events):
        begin = starts[index]
        until = starts[index + 1] if index + 1 < len(events) else math.inf
        frames = seconds = 0.0
        for start, end in spans:
            inside = [t for t in times[start:end] if begin <= t < until]
            if len(inside) >= 2:
                frames += len(inside) - 1
                seconds += inside[-1] - inside[0]
        if seconds >= least:
            event["capture_fps"] = round(frames / seconds, 1)


def measure(
    frames: list[tuple[float, str]],
    events: list[dict],
    *,
    duration: float,
    fps: int,
    workdir: str,
    recorded: dict | None = None,
    viewport_width: float = 0,
    cursor: list | None = None,
    painted: list[tuple[float, str]] | None = None,
) -> dict:
    """What a take's capture is kept as (``VideoTake.capture``), from the frames it keeps (its scrolls paced): its
    frames each second, its stretches under :data:`LOW_FPS`, its sparse stretches; each step's rate set on its event.
    *recorded* is what the recorder said of it (its backend, its pixel ratio, its warm-up, its GL).

    Given its window's *viewport_width* and its *cursor* path, each canvas transition (:func:`canvas_motion`,
    on the frames its browser *painted*, *frames* when not given) is kept on the event of the step it started in
    (``canvas_motion``), and one under :data:`LOW_FPS` is a stretch under the floor, unless the whole frame's already
    is there. Each stretch says it was drawn on SwiftShader.

    The stretches of its motion mostly synthesised from keyframes (:func:`stepping`) are kept (``stepping``),
    their step's event saying the largest share (``synthesised``), and a canvas captured alone that was not composited
    over one (its ``alone`` run's ``why``, ``canvas_alone.composite``) says why there."""
    times = [t for t, _name in frames]
    step_rates(events, times)
    low = low_spans(frames, events, workdir)
    shown = painted if painted is not None else frames
    runs = canvas_motion(shown, events, workdir=workdir, viewport_width=viewport_width, cursor=cursor)
    runs = [replace(run, screencast=screencast_why(run, recorded)) for run in runs]
    for run in runs:
        event = next((e for e in events if run.step is not None and e.get("step") == run.step), None)
        if event is not None and len(event.setdefault("canvas_motion", [])) < MOST_CANVAS_RUNS:
            event["canvas_motion"].append(run.as_dict())
        if run.fps < LOW_FPS and not any(span.start < run.end and span.end > run.start for span in low):
            low.append(run.low())
    software = ((recorded or {}).get("gl") or {}).get("gpu") is False
    alone = ((recorded or {}).get("alone") or {}).get("runs") or []
    # What tells a page drawing slowly on its own from a capture short of it (#10970): the frames its renderer produced
    # and the seconds its worker's quota held it back, span by span.
    page = page_frame_times(events)
    held_back = ((recorded or {}).get("alone") or {}).get("held_back")
    kept_stepping = []
    faults: list[CaptureFault] = []
    for stretch in stepping(frames, events, runs, fps=fps, workdir=workdir):
        # Frames drawn at another scale while the page held still: a capture fault, never stepping motion.
        if (fault := capture_fault(stretch, frames, events, workdir)) is not None:
            faults.append(fault)
            continue
        event = next((e for e in events if stretch.step is not None and e.get("step") == stretch.step), None)
        kept = stretch.as_dict()
        # The whole page changing on a step that holds it: its widgets still drawing, never stepping motion. A canvas
        # moving while the take still holds the page it opened on (a graph's load physics under its first wait):
        # nothing was acted on yet, the page is loading, never a scripted motion to refuse. The whole page moving before
        # the first step started (the page it opened on still drawing) is the page loading too (#9266).
        holding = str((step_at(events, stretch.start) or {}).get("action") or "") in HOLD_STEPS
        if loading(events, stretch.start) or (stretch.canvas is None and holding):
            kept["settling"] = True
        elif event is not None:
            event["synthesised"] = max(float(event.get("synthesised") or 0.0), round(stretch.share, 2))
        why = next((run["why"] for run in alone if run.get("why") and _overlaps(run.get("t"), stretch)), None)
        if why:
            kept["alone"] = why
        if software:
            kept["gl"] = "software"
        _mark_page_limited(kept, stretch, alone, page, held_back)
        # Unclassified by any count of the page's frames, drawn under the recorder's own probing (#11063).
        mark_probes([kept], events, held_back)
        # Neither: starved or page-limited by the quota's read, never unmeasured (#11097).
        mark_unclassified([kept], held_back)
        kept_stepping.append(kept)
    # Each paced transition the page itself drew under the rate told the quota's hold-back over it (#10970).
    mark_quota(events, held_back, canvas_runs=alone, fps=fps)
    low = [span for span in low if not any(f.start < span.end and f.end > span.start for f in faults)]
    return {
        **(recorded or {}),
        "fps": fps,
        "rates": per_second(times, duration),
        "low": [replace(span, software=software).as_dict() for span in sorted(low, key=lambda span: span.start)],
        "stepping": kept_stepping,
        "off_scale": [fault.as_dict() for fault in faults[:MOST_FAULTS]],
        "sparse": sparse_spans(frames, fps, workdir),
    }


def _mark_page_limited(kept: dict, stretch: Stepping, alone: list, page: list[float] | None, held_back: Any) -> None:
    """*kept* (a stretch of :func:`stepping`) told it is page-limited where the page itself drew it under the take's
    rate and every frame it drew was kept (``page_limited``: the page's own rate), and the seconds its worker's quota
    held it back there (``quota_held_back``), #10970. A canvas's own rate is its copies' count of the frames the page
    drew it in (``alone.runs[].drawn``), copied at it; the whole page's, its renderer's frames (``page_frames``)."""
    seconds = max(stretch.end - stretch.start, 1e-6)
    kept_rate = max(0, stretch.frames - stretch.synthesised) / seconds
    drawn: float | None = None
    page_drew: float | None = None
    if stretch.canvas is not None:
        # The copy run overlapping the stretch most decides, one that starts before it and ends after it included, the
        # page drawing the canvas at the rate or under it (#11032 retest: step 11 inside a run of 49 counted copies was
        # said unmeasured): every frame kept is the page's own frames, fewer a capture short of it, never unmeasured.
        best: tuple[float, dict] | None = None
        for run in alone:
            if not isinstance(run, dict) or not _overlaps(run.get("t"), stretch):
                continue
            if not isinstance(run.get("drawn"), int | float) or not isinstance(run.get("fps"), int | float):
                continue
            overlap = min(float(run["t"][1]), stretch.end) - max(float(run["t"][0]), stretch.start)
            if best is None or overlap > best[0]:
                best = (overlap, run)
        if best is not None:
            run = best[1]
            need = page_drawn_rate(float(run["drawn"]), stretch.fps)
            if kept_every_drawn(float(run["fps"]), need, seconds):
                drawn = need
            else:
                page_drew = round(float(run["drawn"]), 2)
    else:
        drawn = page_limited_fps(page, stretch.start, stretch.end, painted=kept_rate, fps=stretch.fps)
        # The page's frame loop runs at the display's rate whatever its canvas draws at: a screencast stretch over a
        # canvas copied alone follows its copy's classification (#10970 retest, steps 1 and 16 of take 92c8c265).
        if drawn is None:
            drawn, page_drew = canvas_limited(alone, stretch.start, stretch.end, painted=kept_rate, fps=stretch.fps)
    if drawn is None and page_drew is None:
        return
    if drawn is not None:
        kept["page_limited"] = drawn
    else:
        kept["page_drew"] = page_drew
    if (held := held_back_over(held_back, stretch.start, stretch.end)) is not None:
        kept["quota_held_back"] = held


def page_limited_notes(capture: dict | None, events: list[dict] | None) -> list[str]:
    """What a take's notes say of each span kept at the page's own rate (#10970): ``page-limited at step 16
    (34.98-35.17 s): the page drew 8 fps, every frame kept and held, none synthesised``; a span the page drew under
    the rate while the worker's quota held it back is said starved, one whose capture kept fewer frames than the page
    drew said capture short, one overlapping a window the capture was starved in said starved too, and one whose quota
    hold-back was not read said page-limited, quota unread: each cause apart (``scroll_pacing.refused_cause``)."""
    said = []
    mark_starved_spans(capture, events)
    spans = [*((capture or {}).get("stepping") or [])]
    spans += [
        {**entry, "step": event.get("step")}
        for event in events or []
        if isinstance(event, dict)
        for entry in [*(event.get("scrolls") or []), *(event.get("motion") or [])]
        if isinstance(entry, dict) and _classified(entry)
    ]
    for span in spans:
        if not isinstance(span, dict) or not _classified(span):
            continue
        try:
            start, end = float(span["t"][0]), float(span["t"][1])
        except (KeyError, TypeError, ValueError, IndexError):
            continue
        at = f"step {span['step']}" if span.get("step") is not None else f"{start:.1f} s"
        if page_limited_kept(span):
            said.append(
                f"page-limited at {at} ({start:.2f}-{end:.2f} s): the page drew {float(page_rate(span) or 0.0):g} fps, "
                "every frame kept and held, none synthesised"
            )
        else:
            # Each cause under its own label (#11021 retest): starved, capture short, page-limited with the quota
            # unread or the frames between paced; ``unmeasured`` is left to a span whose page frames were not counted.
            drew = next(
                (span.get(key) for key in ("page_limited", "page_drew", "painted") if span.get(key) is not None), None
            )
            if drew is None and isinstance(span.get("frames"), int):
                # A stretch drawn under the recorder's probing: the frames it kept that were not synthesised.
                drew = round(max(0, span["frames"] - int(span.get("synthesised") or 0)) / max(end - start, 1e-6), 1)
            label, _, why = refused_cause(span).partition(": ")
            said.append(f"{label} at {at} ({start:.2f}-{end:.2f} s): the page drew {float(drew or 0.0):g} fps; {why}")
    return said[:MOST_CANVAS_RUNS]


def _classified(span: dict) -> bool:
    """Whether a span is said in the notes: the page's frames counted over it, or drawn under the recorder's probing."""
    return (
        span.get("page_limited") is not None
        or span.get("page_drew") is not None
        or bool(span.get("recorder_probe"))
        # Told by the quota's read alone (#11097): starved, or page-limited at the screencast's rate.
        or span.get("screencast_rate") is not None
        or (isinstance(span.get("quota_held_back"), int | float) and float(span["quota_held_back"]) > 0)
    )


#: Characters a take's error keeps (the job's column).
ERROR_CHARS = 2000


def error_lines(lines: list[str], limit: int = ERROR_CHARS) -> str:
    """*lines* joined one a line within *limit* characters, whole: a line that would pass it is left out with the rest,
    never cut in a number (#11063: "the page drew 8." for 8.8 fps read as a sentence's end). A first line longer than
    *limit* alone is cut at its last word."""
    kept: list[str] = []
    size = 0
    for line in lines:
        if not line:
            continue
        grown = size + len(line) + (1 if kept else 0)
        if grown > limit:
            if not kept:
                kept.append(line[:limit].rsplit(" ", 1)[0])
            break
        kept.append(line)
        size = grown
    return "\n".join(kept)


def mark_starved_spans(capture: dict[str, Any] | None, events: list[dict[str, Any]] | None) -> None:
    """Each stretch of *capture* and each scroll and transition of *events* overlapping a window its capture line says
    starved (``insert_rate.starved``, :func:`idle_words`) told it (``starved_window``), in place: such a span is refused
    and said starved, never page-limited nor unmeasured (#11021 retest: step 16 inside "starved at 32.67-36.79 s")."""
    from app.services.video_studio.scroll_pacing import mark_starved

    measured = (capture or {}).get("insert_rate")
    if not isinstance(measured, dict) or not measured.get("painting") or not measured.get("starved"):
        return
    starved = measured["starved"]
    mark_starved((capture or {}).get("stepping"), starved)
    for event in events or []:
        if isinstance(event, dict):
            mark_starved([*(event.get("scrolls") or []), *(event.get("motion") or [])], starved)


def _overlaps(span: Any, stretch: Stepping) -> bool:
    try:
        return float(span[0]) < stretch.end and float(span[1]) > stretch.start
    except (TypeError, ValueError, IndexError):
        return False


@dataclass(frozen=True)
class CaptureFault:
    """Frames a take keeps drawn at another scale than its own, on a step holding the page
    (:func:`capture_fault`): a capture fault to record again, never a motion of the page."""

    start: float
    end: float
    step: int | None
    action: str | None
    #: The take's frames drawn off-scale (their file names), the first :data:`MOST_FAULT_FRAMES`, and how many.
    frames: tuple[str, ...]
    count: int
    label: str | None = None

    def words(self) -> str:
        """``off-scale frames at step 24 (pause, 103.0-103.4 s, 0.4 s, 2 frame(s))``."""
        at = f"at step {self.step}" if self.step is not None else f"at {self.start:.1f} s"
        how = f"{self.action}, " if self.action else ""
        return (
            f"off-scale frames {at} ({how}{self.start:.1f}-{self.end:.1f} s, {self.end - self.start:.1f} s, "
            f"{self.count} frame(s))"
        )

    def as_dict(self) -> dict:
        return {
            "t": [round(self.start, 3), round(self.end, 3)],
            "step": self.step,
            "action": self.action,
            "frames": list(self.frames),
            "count": self.count,
            **({"label": self.label} if self.label else {}),
        }

    @classmethod
    def from_dict(cls, raw: Any) -> CaptureFault | None:
        try:
            start, end = (float(v) for v in raw["t"])
            step = raw.get("step")
            return cls(
                start,
                end,
                int(step) if step is not None else None,
                str(raw["action"]) if raw.get("action") else None,
                tuple(str(name) for name in raw.get("frames") or []),
                int(raw.get("count") or len(raw.get("frames") or [])),
                str(raw["label"]) if raw.get("label") else None,
            )
        except (KeyError, TypeError, ValueError, AttributeError):
            return None


def _alike(a: Path, b: Path) -> bool:
    """Whether two frames are the same picture: one size, :data:`FAULT_ALIKE_SSIM` of structure at least."""
    from PIL import Image

    from app.services.video_studio.frame_qc import ssim

    with Image.open(a) as first, Image.open(b) as second:
        if first.size != second.size:
            return False
        return ssim(first, second) >= FAULT_ALIKE_SSIM


def capture_fault(
    stretch: Stepping, frames: list[tuple[float, str]], events: list[dict], workdir: str
) -> CaptureFault | None:
    """*stretch* (:func:`stepping`) as a capture fault: the whole frame read as moving on a step holding the
    page (:data:`HOLD_STEPS`), between two frames alike - its bounding frames, else the ones about them -, frames in
    between differing from them on :data:`FAULT_MOVED_SHARE` of the frame at least. ``None``: a motion of the page."""
    if stretch.canvas is not None:
        return None
    event = step_at(events, stretch.start) or {}
    action = str(event.get("action") or "")
    if action not in HOLD_STEPS:
        return None
    times = [t for t, _name in frames]
    first = bisect.bisect_left(times, stretch.start - 1e-6)
    last = bisect.bisect_right(times, stretch.end + 1e-6) - 1
    folder = Path(workdir)
    try:
        for a, b in ((first, last), (first - 1, last + 1)):
            if a < 0 or b >= len(frames) or b - a < 2 or not _alike(folder / frames[a][1], folder / frames[b][1]):
                continue
            base = _grey(folder / frames[a][1])
            off = [
                k
                for k in range(a + 1, min(b, a + 1 + _FAULT_READ_MOST))
                if moved_share(base, _grey(folder / frames[k][1])) >= FAULT_MOVED_SHARE
            ]
            if off:
                step = event.get("step")
                return CaptureFault(
                    times[off[0]],
                    times[off[-1] + 1],
                    int(step) if step is not None else None,
                    action,
                    tuple(frames[k][1] for k in off[:MOST_FAULT_FRAMES]),
                    len(off),
                    event.get("label"),
                )
    except Exception as exc:  # a frame not read: the stretch stays what it was found to be
        logger.debug("Capture fault not judged: %s", format_exception_message(exc))
    return None


#: A flash in a hold (:func:`held_flashes`, issue for static_flicker in a pause window): a frame that leaves the
#: picture it held on :data:`FLASH_SHARE` of the frame and comes back to it within :data:`FLASH_SECONDS`.
FLASH_SHARE = 0.03
FLASH_SECONDS = 0.5
#: The most a frame may differ from the one held before the flash (share of the frame) to be back at it, or, on a page
#: still settling under the hold (a graph's physics, a caret), at most :data:`FLASH_BACK_RATIO` of what the flash
#: differed by: the page came back to the state it held, not to the very same pixels (static_flicker after #9818).
FLASH_BACK_SHARE = 0.01
FLASH_BACK_RATIO = 0.34
#: The steps a flash is looked for in: a wait, a pause and a hover or highlight resting the pointer on the page
#: (:data:`HOLD_STEPS`), outside every move of the pointer (:func:`pointer_spans`) - a hovered node's dim, its
#: tooltip and the cursor hold there too, so an alone canvas copy drawn a moment late flashes in them.
FLASH_STEPS = HOLD_STEPS
#: The most frames read for flashes, and the most flashes a take keeps.
_FLASH_READ_MOST = 4000
MOST_FLASHES = 40
#: The stage that drew a frame, by the first letter of its name in the take's workdir.
FRAME_STAGES = {
    "f": "screencast",
    COMPOSITED_PREFIX: "canvas composite",
    "c": "canvas copy",
    "p": "scroll pacing",
    "z": "canvas zoom pacing",
    "m": "transition pacing",
}


def frame_stage(name: str) -> str:
    """The stage that drew the take frame *name* (:data:`FRAME_STAGES`)."""
    return FRAME_STAGES.get(name[:1], "screencast")


def held_flashes(
    frames: list[tuple[float, str]],
    events: list[dict[str, Any]],
    workdir: str,
    cursor: list | None = None,
) -> tuple[list[tuple[float, str]], list[dict[str, Any]]]:
    """*frames* (``(seconds, file name in workdir)``, sorted) with every flash of a hold dropped, and what was dropped.

    In a stretch where the step holds the page (:data:`FLASH_STEPS`: a pause, a wait, a marker's pause after it, a
    hover or a highlight resting the pointer), outside every scroll and every move of the *cursor*, nothing acts on
    the page: a run of frames that leaves the picture held before it on
    :data:`FLASH_SHARE` of the frame and comes back to it within :data:`FLASH_SECONDS` is a frame of another state
    spliced in (a resynthesised or composited frame, an off-scale paint), drawn as the page's static chrome at
    another place and back, or a hover's dim and its tooltip shown and gone (``static_flicker``). Back means within
    :data:`FLASH_BACK_SHARE` of the frame held, or within :data:`FLASH_BACK_RATIO` of what the flash differed by when
    the page still settles under the hold. The frame held before it holds instead. Each run dropped is said
    ``{t, frames, step, stages, source}``: its span, how many frames, the step, the stages that drew them
    (:func:`frame_stage`) and the take seconds of the frames dropped, so a retest can tell which stage drew them."""
    if len(frames) < 3:
        return list(frames), []
    scrolls = paced_spans(events, transitions=False) + pointer_spans(cursor)
    folder = Path(workdir)
    greys: dict[int, Any] = {}
    reads = 0

    def grey(index: int) -> Any:
        nonlocal reads
        if index not in greys:
            reads += 1
            greys[index] = _grey(folder / frames[index][1])
        return greys[index]

    def holding(index: int) -> dict[str, Any] | None:
        t = frames[index][0]
        if any(a <= t <= b for a, b in scrolls):
            return None
        event = step_at(events, t)
        if event is None or str(event.get("action") or "") not in FLASH_STEPS:
            return None
        return event

    dropped: set[int] = set()
    found: list[dict[str, Any]] = []
    i = 1
    try:
        while i < len(frames) - 1 and reads < _FLASH_READ_MOST:
            anchor = i - 1
            event = holding(anchor)
            if event is None or holding(i) is not event or moved_share(grey(anchor), grey(i)) < FLASH_SHARE:
                i += 1
                continue
            back = None
            away = moved_share(grey(anchor), grey(i))
            j = i + 1
            while j < len(frames) and frames[j][0] - frames[i][0] <= FLASH_SECONDS and holding(j) is event:
                share = moved_share(grey(anchor), grey(j))
                if share <= max(FLASH_BACK_SHARE, FLASH_BACK_RATIO * away):
                    back = j
                    break
                if share < FLASH_SHARE:
                    break
                away = min(away, share)
                j += 1
            if back is None:
                i += 1
                continue
            run = range(i, back)
            dropped.update(run)
            if len(found) < MOST_FLASHES:
                step = event.get("step")
                found.append(
                    {
                        "t": [round(frames[i][0], 3), round(frames[back][0], 3)],
                        "frames": len(run),
                        "step": int(step) if step is not None else None,
                        "action": str(event.get("action") or ""),
                        "stages": sorted({frame_stage(frames[k][1]) for k in run}),
                        "source": [round(frames[k][0], 3) for k in run][:MOST_FAULT_FRAMES],
                    }
                )
            i = back + 1
    except Exception as exc:  # a frame not read: the take keeps its frames as they are
        logger.debug("Flashes of a hold not judged: %s", format_exception_message(exc))
        return list(frames), []
    kept = [frame for k, frame in enumerate(frames) if k not in dropped]
    kept, probes = _locate_probes(kept, events)
    found = [*found, *probes]
    kept, approaches = _approach_flashes(kept, events, folder, MOST_FLASHES - len(found))
    found = [*found, *approaches]
    kept, flickers = _held_flickers(kept, events, folder, scrolls, MOST_FLASHES - len(found))
    found = [*found, *flickers]
    kept, paced_back = _paced_returns(kept, events, folder, pointer_spans(cursor), MOST_FLASHES - len(found))
    found = [*found, *paced_back]
    kept, returns = _pointer_returns(kept, events, folder, cursor, MOST_FLASHES - len(found))
    found = [*found, *returns]
    kept, replayed = _gate_flickers(kept, events, folder, MOST_FLASHES - len(found))
    found = sorted([*found, *replayed], key=lambda f: f["t"][0])
    if not found:
        return list(frames), []
    return kept, found


#: The spans a pointer step logs of the recorder's own probing, each with the seconds its frames stay the probe's past
#: it and the test :func:`held_flashes` says its frames under: the node's highlight read through the page's list
#: (``probed``), and the places a node's card was looked for under a still cursor before the one naming it
#: (``hover_searched``: an edge's card, the node's hover lit and unlit as the cursor's rest crosses its edge).
_PROBE_SPANS = (("probed", PROBE_TAIL, "locate probe"), ("hover_searched", 0.0, "hover search"))


def _locate_probes(
    frames: list[tuple[float, str]], events: list[dict[str, Any]]
) -> tuple[list[tuple[float, str]], list[dict[str, Any]]]:
    """*frames* with every frame painted while the recorder probed a node's highlight, or looked for its card at a
    place other than the one naming it, dropped, and the runs said as :func:`held_flashes` says them (test ``locate
    probe``, ``hover search``).

    A node OCR cannot read is located by its highlight (``node_locate._highlight``): the recorder focuses the page's
    list and moves its active entry onto a neighbour, the node, the other neighbour, the canvas lit for each - the
    recorder's own probing, never the film's, an A/B/A/B of the graph (full, dimmed, node lit, dimmed) under a still
    cursor. The pointer step logs its span (``probed``, take seconds); the frame painted before it holds over it and
    :data:`PROBE_TAIL` after, while the page repaints with its focus given back. A node's first place whose card names
    something else (an edge's) is the recorder's search too: the cursor rests there, its hover nudges crossing the
    node's edge light the node's hover for a few frames and unlight it (#9590, an A/B/A of the graph before the real
    hover), until the cursor moves on to the place naming it - the step logs that span (``hover_searched``)."""
    spans: list[tuple[float, float, dict[str, Any], str]] = []
    for event in events:
        for key, tail, test in _PROBE_SPANS:
            probed = event.get(key)
            if isinstance(probed, list | tuple) and len(probed) == 2:
                with contextlib.suppress(TypeError, ValueError):
                    spans.append((float(probed[0]), float(probed[1]) + tail, event, test))
    if not spans or len(frames) < 2:
        return frames, []
    kept: list[tuple[float, str]] = []
    found: list[dict[str, Any]] = []
    for t, name in frames:
        inside = next((span for span in spans if span[0] <= t <= span[1]), None)
        if inside is None or not kept:
            # The first frame of a take is never dropped: there is nothing before it to hold.
            kept.append((t, name))
            continue
        start, end, event, test = inside
        last = found[-1] if found and found[-1]["_span"] == (start, end) else None
        if last is None:
            held = step_at(events, t) or {}
            step = event.get("step")
            last = {
                "t": [round(start, 3), round(end, 3)],
                "frames": 0,
                "step": int(step) if step is not None else None,
                "action": str(held.get("action") or ""),
                "stages": [],
                "source": [],
                "test": test,
                "_span": (start, end),
            }
            found.append(last)
        last["frames"] += 1
        last["stages"] = sorted({*last["stages"], frame_stage(name)})
        if len(last["source"]) < MOST_FAULT_FRAMES:
            last["source"].append(round(t, 3))
    for run in found:
        del run["_span"]
    return kept, found[:MOST_FLASHES]


def _approach_flashes(
    frames: list[tuple[float, str]], events: list[dict[str, Any]], folder: Path, most: int
) -> tuple[list[tuple[float, str]], list[dict[str, Any]]]:
    """*frames* with every state a located node's approach showed and took back dropped, the frame before held, and the
    runs said as :func:`held_flashes` says them (test ``node approach``).

    A button on a node located on a canvas (``node_locate``) is preceded by the recorder's approach: the cursor glides
    onto the node's rim and rests there, its hover nudges crossing the node's edges, the card read before the button.
    The page lights an edge, then another, then none (dimmed, an edge lit, undimmed) before the node's own hover: an
    A/B/A of the graph a film never shows (#9590, a node located by its highlight after a long pause, 44.77-45.03 s
    before the real hover at 45.05 s, ``capture.held_flashes`` empty). A step at a point (``at``, a wheel on a graph
    canvas among them) is approached the same way, its glide crossing nodes the canvas dims and undims before the wheel
    (test ``point approach``). The pointer step logs its approach
    (``approached``, take seconds, from the node's location to the button). Inside it, a run of frames leaving the frame
    before on :data:`FLASH_SHARE` of the picture and back to it within :data:`FLASH_SECONDS` is dropped; a state that
    stays (the node's own hover, the button's answer) is kept, and nothing outside the span is touched."""
    spans: list[tuple[float, float, dict[str, Any]]] = []
    for event in events:
        approached = event.get("approached")
        if isinstance(approached, list | tuple) and len(approached) == 2:
            with contextlib.suppress(TypeError, ValueError):
                spans.append((float(approached[0]), float(approached[1]), event))
    if not spans or len(frames) < 3 or most <= 0:
        return frames, []

    def span_of(t: float) -> tuple[float, float, dict[str, Any]] | None:
        return next((span for span in spans if span[0] <= t <= span[1]), None)

    greys: dict[int, Any] = {}

    def grey(index: int) -> Any:
        if index not in greys:
            greys[index] = _grey(folder / frames[index][1])
        return greys[index]

    dropped: set[int] = set()
    found: list[dict[str, Any]] = []
    i = 1
    try:
        while i < len(frames) - 1 and len(greys) < _FLASH_READ_MOST:
            span = span_of(frames[i][0])
            anchor = i - 1
            if span is None or span_of(frames[anchor][0]) is not span:
                i += 1
                continue
            away = moved_share(grey(anchor), grey(i))
            if away < FLASH_SHARE:
                i += 1
                continue
            back = None
            j = i + 1
            while j < len(frames) and frames[j][0] - frames[i][0] <= FLASH_SECONDS and span_of(frames[j][0]) is span:
                share = moved_share(grey(anchor), grey(j))
                if share <= max(FLASH_BACK_SHARE, FLASH_BACK_RATIO * away):
                    back = j
                    break
                if share < FLASH_SHARE:
                    break
                away = min(away, share)
                j += 1
            if back is None:
                i += 1
                continue
            run = range(i, back)
            dropped.update(run)
            if len(found) < most:
                step = span[2].get("step")
                found.append(
                    {
                        "t": [round(frames[i][0], 3), round(frames[back][0], 3)],
                        "frames": len(run),
                        "step": int(step) if step is not None else None,
                        "action": str(span[2].get("action") or ""),
                        "stages": sorted({frame_stage(frames[k][1]) for k in run}),
                        "source": [round(frames[k][0], 3) for k in run][:MOST_FAULT_FRAMES],
                        "test": "point approach" if span[2].get("approach") == "point" else "node approach",
                    }
                )
            i = back + 1
    except Exception as exc:  # a frame not read: the take keeps its frames as they are
        logger.debug("Approach flashes not judged: %s", format_exception_message(exc))
        return frames, []
    if not dropped:
        return frames, []
    return [frame for k, frame in enumerate(frames) if k not in dropped], found


#: The width frames are compared at for a flicker of the page's chrome (:func:`_held_flickers`): the width the render's
#: ``static_flicker`` watch reads (``frame_freeze.ANALYSIS_WIDTH``), so its tiles cover the same part of the page.
_FLICKER_WIDTH = 192
#: The longest a picture of a hold may stay away and still flicker on a render (:func:`_held_flickers`): the render's
#: ``static_flicker`` watch reads a flip of ``static_chrome.MAX_AWAY`` frames at ``frame_freeze.ANALYSIS_FPS``, and a
#: recording scene plays a pause compressed (``spec.IdleCompression.speed``, 6 by default), so a picture away for this
#: many take seconds is away for three frames of the render at most (static_flicker after #9888: a 13 s pause).
FLICKER_AWAY_SECONDS = 0.6
#: The steps whose frames are read for an earlier picture coming back (:func:`_paced_returns`): a wheel's zoom drawn
#: again from keyframes painted a few a second, where a keyframe of an earlier state held between two later ones
#: alternates on the render (static_flicker after #9888, a wheel painted at 7.4 fps).
FLICKER_PACED_STEPS = frozenset({"wheel"})


def _flicker_grey(path: Path) -> Any:
    import numpy as np
    from PIL import Image

    with Image.open(path) as image:
        height = max(1, round(image.height * _FLICKER_WIDTH / max(1, image.width)))
        image.draft("L", (_FLICKER_WIDTH, height))
        grey = image.convert("L")
        if grey.size != (_FLICKER_WIDTH, height):
            grey = grey.resize((_FLICKER_WIDTH, height), Image.Resampling.BOX)
        return np.asarray(grey, dtype=np.float32)


def _held_flickers(
    frames: list[tuple[float, str]],
    events: list[dict[str, Any]],
    folder: Path,
    scrolls: list[tuple[float, float]],
    most: int,
) -> tuple[list[tuple[float, str]], list[dict[str, Any]]]:
    """*frames* with every run that flickers the page's chrome in a hold dropped, and the runs said as
    :func:`held_flashes` says them.

    The test of the render's ``static_flicker`` watch (``static_chrome.returns_after``), on the take: tiles of the
    frame that leave the picture held and come back to it within :data:`FLICKER_AWAY_SECONDS` - what a compressed pause
    shows in one to ``static_chrome.MAX_AWAY`` frames of the render. It reads what :data:`FLASH_SHARE` misses - two
    moments of a hover alternating (A/B/A/B), each change a tooltip, a cursor's hover or a node's dim on a small part of
    the frame - so a take whose holds would flicker on its preview holds one moment instead and lists it."""
    from app.services.video_studio import static_chrome

    if len(frames) < 3:
        return frames, []
    greys: dict[int, Any] = {}

    def grey(index: int) -> Any:
        if index not in greys:
            greys[index] = _flicker_grey(folder / frames[index][1])
        return greys[index]

    def holding(index: int) -> dict[str, Any] | None:
        t = frames[index][0]
        if any(a <= t <= b for a, b in scrolls):
            return None
        event = step_at(events, t)
        if event is None or str(event.get("action") or "") not in FLASH_STEPS:
            return None
        return event

    dropped: set[int] = set()
    found: list[dict[str, Any]] = []
    i = 1
    try:
        while i < len(frames) - 1 and len(greys) < _FLASH_READ_MOST:
            event = holding(i - 1)
            if event is None or holding(i) is not event:
                i += 1
                continue
            end = i + 1
            while end < len(frames) and holding(end) is event and frames[end][0] - frames[i][0] <= FLICKER_AWAY_SECONDS:
                end += 1
            back = None
            if end - i >= 2:
                found_at = static_chrome.returns_after(grey(i - 1), [grey(k) for k in range(i, end)])
                back = i + found_at if found_at is not None else None
            if back is None:
                i += 1
                continue
            run = range(i, back)
            dropped.update(run)
            step = event.get("step")
            last = found[-1] if found else None
            if last is not None and last["step"] == step and frames[i][0] - last["t"][1] <= FLASH_SECONDS:
                # One flicker of A/B/A/B said once: its whole span, every frame dropped in it.
                last["t"][1] = round(frames[back][0], 3)
                last["frames"] += len(run)
                last["stages"] = sorted({*last["stages"], *(frame_stage(frames[k][1]) for k in run)})
                last["source"] = [*last["source"], *(round(frames[k][0], 3) for k in run)][:MOST_FAULT_FRAMES]
            elif len(found) < most:
                found.append(
                    {
                        "t": [round(frames[i][0], 3), round(frames[back][0], 3)],
                        "frames": len(run),
                        "step": int(step) if step is not None else None,
                        "action": str(event.get("action") or ""),
                        "stages": sorted({frame_stage(frames[k][1]) for k in run}),
                        "source": [round(frames[k][0], 3) for k in run][:MOST_FAULT_FRAMES],
                        "test": "chrome flicker",
                    }
                )
            i = back + 1
    except Exception as exc:  # a frame not read: the take keeps the frames this pass had
        logger.debug("Flickers of a hold not judged: %s", format_exception_message(exc))
        return frames, []
    return [frame for k, frame in enumerate(frames) if k not in dropped], found


#: The longest a stale frame of a moving pointer may come back after (:func:`_pointer_returns`): the span the render's
#: ``static_flicker`` watch reads a flip over, ``static_chrome.MAX_AWAY`` frames away at its 30 fps plus the return.
RETURN_SECONDS = 0.14
#: The most frames between the picture and its stale return, and the least the pointer moves (px) between them.
_RETURN_AWAY_MOST = 8
_RETURN_POINTER_PX = 2.0


#: The speeds a take is replayed at for the render's ``static_flicker`` watch (:func:`_gate_flickers`): as recorded,
#: and as a recording scene plays a compressed pause (``spec.IdleCompression.speed``, 6 by default).
GATE_SPEEDS = (1.0, 6.0)
#: The finest step between two sampling phases (take seconds, :func:`gate_phases`): a frame shown twice by a scene only
#: when its trim falls in a window narrower than this is read once at every phase.
GATE_PHASE_RESOLUTION = 0.002
#: The most sampling phases the take is replayed at as recorded (:func:`gate_phases`): a scene starts reading the take
#: where its trim says, so the take frame a render frame shows is shifted by a fraction of a sample step; one phase
#: reads every other frame of a 60 fps take, and an alternation on the frames it skipped passed the take and failed
#: the preview (#9590, retest of #9950: a pause into a node click located by its highlight, trimmed at 30.88 s). A
#: frame painted off the take's grid (a page-limited take, fps_fallback fps) is read at its own start too: one sample
#: step over :data:`GATE_PHASE_RESOLUTION` phases at most.
GATE_PHASES_MOST = math.ceil(1.0 / 30.0 / GATE_PHASE_RESOLUTION - 1e-9)
#: The rate the render's watch reads frames at (``frame_freeze.ANALYSIS_FPS``).
GATE_FPS = 30.0
#: The seconds a frame time may sit past a sample and still be the frame it shows: a take keeps its frame times to
#: the microsecond (``round(t, 6)``).
GATE_TIME_SLACK = 1e-6
#: The most replays of a take: a run dropped changes what the next replay samples.
GATE_PASSES = 3


def _gate_flickers(
    frames: list[tuple[float, str]],
    events: list[dict[str, Any]],
    folder: Path,
    most: int,
) -> tuple[list[tuple[float, str]], list[dict[str, Any]]]:
    """*frames* with every run the render's ``static_flicker`` watch would still find dropped, the picture before it
    held, and the runs said as :func:`held_flashes` says them (test ``gate replay``).

    The passes before read a flicker by the step it falls in and by take frames; the render reads the frames a
    scene shows - the take sampled at :data:`GATE_FPS` of output, a compressed pause six take frames a frame or more
    apart - so a flicker across two steps (a pause into a click), or spread over more take frames than a pass reads,
    passed the take and failed the preview (#9590, retest of #9918). This pass replays the take at each of
    :data:`GATE_SPEEDS` through the watch itself (``static_chrome.ChromeWatch`` and the run rule of
    ``static_chrome.findings``) over EVERY frame, and drops the take frames from the first sample away to the sample it
    came back on: the gate and the take's list of dropped frames then agree, and what stays alternating is named with
    its time. The render's watch reads a hover under a moving pointer and a wheel's zoom like any other frame, so the
    replay does too (#9590, retest of #9942: both windows were left out, the preview flickered there and
    ``held_flashes`` stayed empty). A scroll, a zoom or a transition that moves on never comes back to the picture it
    left: the watch finds nothing there and nothing is dropped."""
    from app.services.video_studio import static_chrome

    if len(frames) < 3:
        return frames, []
    greys: dict[str, Any] = {}

    def grey(name: str) -> Any:
        if name not in greys:
            greys[name] = _flicker_grey(folder / name)
        return greys[name]

    kept = list(frames)
    found: list[dict[str, Any]] = []
    try:
        for _pass in range(GATE_PASSES):
            dropped_any = False
            replays = [(rate, phase) for rate in GATE_SPEEDS for phase in gate_phases([t for t, _n in kept], rate)]
            for speed, phase in replays:
                runs = _gate_runs(kept, speed, grey, static_chrome, phase)
                if not runs:
                    continue
                drop: set[int] = set()
                for first, back in runs:
                    span = range(first, back)
                    if not span:
                        continue
                    drop.update(span)
                    event = step_at(events, kept[first][0]) or {}
                    step = event.get("step")
                    if len(found) < most:
                        found.append(
                            {
                                "t": [round(kept[first][0], 3), round(kept[back][0], 3)],
                                "frames": len(span),
                                "step": int(step) if step is not None else None,
                                "action": str(event.get("action") or ""),
                                "stages": sorted({frame_stage(kept[k][1]) for k in span}),
                                "source": [round(kept[k][0], 3) for k in span][:MOST_FAULT_FRAMES],
                                "test": "gate replay",
                                "speed": speed,
                            }
                        )
                if drop:
                    kept = [frame for k, frame in enumerate(kept) if k not in drop]
                    dropped_any = True
                if len(greys) >= _FLASH_READ_MOST:
                    break
            if not dropped_any or len(greys) >= _FLASH_READ_MOST:
                break
    except Exception as exc:  # a frame not read: the take keeps the frames this pass had
        logger.debug("Flickers of the render's watch not replayed: %s", format_exception_message(exc))
        return frames, []
    return kept, found


def gate_phases(times: list[float], speed: float, rate: float = GATE_FPS) -> list[float]:
    """The offsets (take seconds, from 0 to one sample step) a replay at *speed* starts sampling *times* at: as recorded,
    one per place a take frame starts at within a sample step (rounded up to :data:`GATE_PHASE_RESOLUTION`), so every
    frame of the take is the first a render frame shows in one replay at least, whatever the scene's trim - the replay
    that shows a frame the most times a scene can. A frame painted off the grid of a page-limited take is held 1 to 2
    sample steps: read from the take's first frame it shows once, from the trim of the scene that previews it twice,
    a blink (#9590, retest ab13b8a2 of #11237: the dim of a wheel zoom's approach, 24.63 s of take 96bd2971). A
    compressed replay keeps the one phase: read a dozen take frames apart, a change that goes on (a wheel's zoom, a
    page settling) would read as a return."""
    step = speed / rate
    if len(times) < 2 or speed > 1.0 or step <= 0:
        return [0.0]
    origin = times[0]
    places = {0}
    for t in times[1:]:
        offset = (t - origin) % step
        if offset < GATE_TIME_SLACK or step - offset < GATE_TIME_SLACK:
            # A frame on the grid, its time rounded to the microsecond.
            continue
        places.add(min(GATE_PHASES_MOST - 1, math.ceil(offset / GATE_PHASE_RESOLUTION - 1e-6)))
    return [round(place * GATE_PHASE_RESOLUTION, 6) for place in sorted(places)]


def gate_samples(times: list[float], speed: float, rate: float = GATE_FPS, phase: float = 0.0) -> list[int]:
    """The index in *times* (take seconds, sorted) of the frame a scene playing the take at *speed* shows on each of its
    frames at *rate*: the last frame painted by then, the first read *phase* seconds after the take's first frame."""
    if not times:
        return []
    out: list[int] = []
    step = speed / rate
    # Each sample time from its count, never summed: a sum drifts under a frame time kept to the microsecond and
    # skipped a frame on the grid now and then, which hid a two-frame blink the render reads (retest ab13b8a2).
    count = 0
    t = times[0] + phase
    while t <= times[-1] + GATE_TIME_SLACK:
        out.append(max(0, bisect.bisect_right(times, t + GATE_TIME_SLACK) - 1))
        count += 1
        t = times[0] + phase + count * step
    return out


def _gate_runs(
    frames: list[tuple[float, str]],
    speed: float,
    grey: Any,
    static_chrome: Any,
    phase: float = 0.0,
) -> list[tuple[int, int]]:
    """The runs of *frames* the render's watch finds played at *speed* from *phase*: ``(first take index away, take
    index back)`` for each, the take frames between them the ones to drop."""
    times = [t for t, _name in frames]
    samples = gate_samples(times, speed, phase=phase)
    # The longer excursions read too: a blink whose away frames differ among themselves is never hidden by the shorter
    # flip between them (#9590, retest 4c4194f6: a layout click's canvas dimmed, then dimmed with a node faded).
    watch = static_chrome.ChromeWatch(every_away=True)
    recording = {"type": "recording", "id": "take"}
    for count, index in enumerate(samples):
        watch.feed(grey(frames[index][1]), count, recording)
    if not watch.hits:
        return []
    # The run rule of ``static_chrome.findings`` (``static_chrome.is_run``): three flicker frames, or one blink over a
    # still page (a node's hover dim drawn and gone before the real hover, #9590 retest of #9960).
    runs: list[list[list[float]]] = []
    for hit in sorted(watch.hits, key=lambda h: h[0]):
        if runs and int(hit[0]) - int(runs[-1][-1][0]) <= static_chrome.RUN_GAP:
            runs[-1].append(hit)
        else:
            runs.append([hit])
    out: list[tuple[int, int]] = []
    for run in runs:
        if not static_chrome.is_run(run):
            continue
        counts = sorted({int(hit[0]) for hit in run})
        first = samples[counts[0]]
        back = samples[counts[-1] + 1] if counts[-1] + 1 < len(samples) else len(frames) - 1
        # The take frames between the last sample away and the sample back that are already back stay: a replay that
        # shows every frame of its grid would otherwise drop a frame of the page it came back to.
        while back - 1 > first and static_chrome.same_picture(grey(frames[back - 1][1]), grey(frames[back][1])):
            back -= 1
        if back > first:
            out.append((first, back))
    return out


def _pointer_at(cursor: list | None, t: float) -> tuple[float, float] | None:
    """Where the pointer was at *t* (its last ``(t, x, y)`` sample at or before it), ``None`` before the first."""
    where = None
    for sample in cursor or []:
        if float(sample[0]) > t:
            break
        where = (float(sample[1]), float(sample[2]))
    return where


def _pointer_returns(
    frames: list[tuple[float, str]],
    events: list[dict[str, Any]],
    folder: Path,
    cursor: list | None,
    most: int,
) -> tuple[list[tuple[float, str]], list[dict[str, Any]]]:
    """*frames* with every stale frame of a moving pointer dropped, and the runs said as :func:`held_flashes` says them.

    While the pointer moves the page answers it (a hover's dim, a tooltip), so the holds leave those stretches alone.
    Two moments of it interleaved (A/B/A/B: an older frame composited after a newer one) still flicker on the
    render: the tiles that changed come back (``static_chrome.flipped_tiles``) within :data:`RETURN_SECONDS`, and the
    WHOLE frame is the older picture again (``static_chrome.same_picture``, the cursor drawn in it included) although the
    pointer moved on by :data:`_RETURN_POINTER_PX` at least, and the very next frame is the newer moment again. A page
    that answers a moving pointer never draws the very picture of a moment the pointer left between two frames of the
    newer one: that frame is the stale one, dropped, the newer moment held, so the sequence the render reads is
    monotonic. A hover entered and left (A/B/A, the pointer crossing a node) stays as it is. A stationary pointer (a
    click's pressed state and back) is never touched; nor are the paced scrolls and transitions, drawn by the recorder
    itself."""
    from app.services.video_studio import static_chrome

    if len(frames) < 3 or not cursor:
        return frames, []
    paced = paced_spans(events)
    greys: dict[int, Any] = {}

    def grey(index: int) -> Any:
        if index not in greys:
            greys[index] = _flicker_grey(folder / frames[index][1])
        return greys[index]

    def free(index: int) -> bool:
        t = frames[index][0]
        return not any(a <= t <= b for a, b in paced)

    def moved(a: int, b: int) -> bool:
        before, after = _pointer_at(cursor, frames[a][0]), _pointer_at(cursor, frames[b][0])
        if before is None or after is None:
            return False
        return math.hypot(after[0] - before[0], after[1] - before[1]) >= _RETURN_POINTER_PX

    kept: list[int] = []
    found: list[dict[str, Any]] = []
    try:
        for k in range(len(frames)):
            if len(greys) >= _FLASH_READ_MOST or not free(k) or len(kept) < 2:
                kept.append(k)
                continue
            stale_of = None
            for away in range(1, min(_RETURN_AWAY_MOST, len(kept) - 1) + 1):
                ref = kept[-(away + 1)]
                if frames[k][0] - frames[ref][0] > RETURN_SECONDS or not free(ref):
                    break
                between = kept[-away:]
                if not moved(ref, k):
                    continue
                window = [grey(ref), *(grey(m) for m in between), grey(k)]
                if static_chrome.flipped_tiles(window) is None or not static_chrome.same_picture(window[0], window[-1]):
                    continue
                # A/B/A/B, not a hover in and out (A/B/A): the very next frame is the newer moment again.
                if k + 1 < len(frames) and free(k + 1) and static_chrome.same_picture(grey(kept[-1]), grey(k + 1)):
                    stale_of = ref
                break
            if stale_of is None:
                kept.append(k)
                continue
            event = step_at(events, frames[k][0]) or {}
            step = event.get("step")
            last = found[-1] if found else None
            if last is not None and last["step"] == step and frames[k][0] - last["t"][1] <= FLASH_SECONDS:
                # One interleave said once: its whole span, every stale frame dropped in it.
                last["t"][1] = round(frames[k][0], 3)
                last["frames"] += 1
                last["stages"] = sorted({*last["stages"], frame_stage(frames[k][1])})
                last["source"] = [*last["source"], round(frames[k][0], 3)][:MOST_FAULT_FRAMES]
            elif len(found) < most:
                found.append(
                    {
                        "t": [round(frames[stale_of][0], 3), round(frames[k][0], 3)],
                        "frames": 1,
                        "step": int(step) if step is not None else None,
                        "action": str(event.get("action") or ""),
                        "stages": [frame_stage(frames[k][1])],
                        "source": [round(frames[k][0], 3)],
                        "test": "pointer interleave",
                    }
                )
    except Exception as exc:  # a frame not read: the take keeps the frames this pass had
        logger.debug("Stale frames of a moving pointer not judged: %s", format_exception_message(exc))
        return frames, []
    return [frames[k] for k in kept], found


def _paced_returns(
    frames: list[tuple[float, str]],
    events: list[dict[str, Any]],
    folder: Path,
    moves: list[tuple[float, float]],
    most: int,
) -> tuple[list[tuple[float, str]], list[dict[str, Any]]]:
    """*frames* with every earlier picture that comes back inside a wheel's zoom dropped, and the runs said as
    :func:`held_flashes` says them (test ``paced return``).

    A canvas a wheel zooms (:data:`FLICKER_PACED_STEPS`) is drawn again from the keyframes its page painted; one of an
    earlier state held between two later ones (an un-zoomed, un-hovered keyframe among zoomed ones) is the very
    picture of a frame kept before within :data:`FLICKER_AWAY_SECONDS` (``static_chrome.same_picture``), unlike the
    frame just before it, and the next frame is that later picture again (A/B/A/B): the ``static_flicker`` the render
    reads. A zoom that goes on never draws an earlier picture again, so it is kept whole; a move of the pointer
    (*moves*) is the page's answer to it and left to :func:`_pointer_returns`."""
    from app.services.video_studio import static_chrome

    if len(frames) < 3:
        return frames, []
    greys: dict[int, Any] = {}

    def grey(index: int) -> Any:
        if index not in greys:
            greys[index] = _flicker_grey(folder / frames[index][1])
        return greys[index]

    def wheel(index: int) -> dict[str, Any] | None:
        t = frames[index][0]
        if any(a <= t <= b for a, b in moves):
            return None
        event = step_at(events, t)
        return event if event is not None and str(event.get("action") or "") in FLICKER_PACED_STEPS else None

    kept: list[int] = []
    found: list[dict[str, Any]] = []
    try:
        for k in range(len(frames)):
            event = wheel(k)
            if event is None or len(greys) >= _FLASH_READ_MOST or len(kept) < 2 or k + 1 >= len(frames):
                kept.append(k)
                continue
            newer = kept[-1]
            stale_of = None
            if not static_chrome.same_picture(grey(newer), grey(k)) and static_chrome.same_picture(
                grey(newer), grey(k + 1)
            ):
                for ref in reversed(kept[-(_RETURN_AWAY_MOST + 1) : -1]):
                    if frames[k][0] - frames[ref][0] > FLICKER_AWAY_SECONDS or wheel(ref) is not event:
                        break
                    if static_chrome.same_picture(grey(ref), grey(k)):
                        stale_of = ref
                        break
            if stale_of is None:
                kept.append(k)
                continue
            step = event.get("step")
            last = found[-1] if found else None
            if last is not None and last["step"] == step and frames[k][0] - last["t"][1] <= FLASH_SECONDS:
                # One alternation said once: its whole span, every earlier picture dropped in it.
                last["t"][1] = round(frames[k][0], 3)
                last["frames"] += 1
                last["stages"] = sorted({*last["stages"], frame_stage(frames[k][1])})
                last["source"] = [*last["source"], round(frames[k][0], 3)][:MOST_FAULT_FRAMES]
            elif len(found) < most:
                found.append(
                    {
                        "t": [round(frames[stale_of][0], 3), round(frames[k][0], 3)],
                        "frames": 1,
                        "step": int(step) if step is not None else None,
                        "action": str(event.get("action") or ""),
                        "stages": [frame_stage(frames[k][1])],
                        "source": [round(frames[k][0], 3)],
                        "test": "paced return",
                    }
                )
    except Exception as exc:  # a frame not read: the take keeps the frames this pass had
        logger.debug("Earlier pictures of a wheel's zoom not judged: %s", format_exception_message(exc))
        return frames, []
    return [frames[k] for k in kept], found


def flash_words(flashes: list[dict[str, Any]] | None) -> str:
    """What a take's notes say of the flashes it dropped in its holds (:func:`held_flashes`), none said too."""
    if not flashes:
        return "Held flashes: none (no frame of another state found in a pause, a wait or a hover)."
    said = "; ".join(
        f"{len(f.get('source') or [])} at {float(f['t'][0]):.2f} s (step {f.get('step')}, "
        f"{', '.join([*(f.get('stages') or []), *([str(f['test'])] if f.get('test') else [])])})"
        for f in flashes[:5]
    )
    return (
        f"{sum(int(f.get('frames') or 0) for f in flashes)} frame(s) of another state spliced into a pause were "
        f"dropped, the frame before held: {said}."
    )


def fault_words(capture: dict | None) -> list[str]:
    """``off-scale frames at step 24 (...)`` for each capture fault a take's *capture* kept."""
    found = (CaptureFault.from_dict(raw) for raw in (capture or {}).get("off_scale") or [])
    return [fault.words() for fault in found if fault is not None]


def dropped_runs(drops: list[tuple[float, str]], events: list[dict]) -> list[dict]:
    """The runs of the frames the recorder dropped as painted at another size or scale
    (``capture._Screencast``), *drops* ``(seconds of the take, why)``: ``{t, frames, step, why}`` each, none further
    apart than :data:`RUN_GAP`, named by the step playing then, :data:`MOST_FAULTS` at most."""
    found: list[dict] = []
    for t, why in sorted(drops):
        if found and t - found[-1]["t"][1] <= RUN_GAP:
            found[-1]["t"][1] = round(t, 3)
            found[-1]["frames"] += 1
            continue
        event = step_at(events, t) or {}
        found.append({"t": [round(t, 3), round(t, 3)], "frames": 1, "step": event.get("step"), "why": why})
    return found[:MOST_FAULTS]


def dropped_words(runs: list[dict] | None) -> str:
    """What a take's notes say of the frames its recorder dropped: every run, step, time and length, in one
    line; empty when none was."""
    said = []
    for run in runs or []:
        try:
            start, end = (float(v) for v in run["t"])
        except (KeyError, TypeError, ValueError):
            continue
        at = f"step {run['step']}" if run.get("step") is not None else f"{start:.1f} s"
        said.append(f"{at} ({start:.1f}-{end:.1f} s, {int(run.get('frames') or 1)} frame(s): {run.get('why')})")
    if not said:
        return ""
    return (
        "Frames the browser painted at another size or scale were dropped, the frame before held in their place: "
        + "; ".join(said)
        + "."
    )


def low_words(capture: dict | None) -> list[str]:
    """``low capture rate at step 4 (12 fps for 2.3 s)`` for each stretch a take's *capture* found under the floor."""
    said = []
    for span in (capture or {}).get("low") or []:
        try:
            start, end = (float(v) for v in span["t"])
            step = span.get("step")
            low = LowSpan(
                start,
                end,
                float(span["fps"]),
                int(step) if step is not None else None,
                canvas=str(span["canvas"]) if span.get("canvas") else None,
                software=span.get("gl") == "software",
                alone=str(span["alone"]) if span.get("alone") else None,
            )
            said.append(low.words())
        except (KeyError, TypeError, ValueError):
            continue
    return said


def settling_words(capture: dict | None) -> list[str]:
    """``settling (advisory) at step 4 (the page drew on 2.0 s on a step that holds it)`` for each stretch of a take's
    *capture* the page spent settling on a step that holds it (``settling``): widgets still drawing, no motion
    the audience follows - never a stretch to record again, never a render refused."""
    said = []
    for stretch in (capture or {}).get("stepping") or []:
        if not isinstance(stretch, dict) or not stretch.get("settling"):
            continue
        try:
            start, end = (float(v) for v in stretch["t"])
        except (KeyError, TypeError, ValueError):
            continue
        step = stretch.get("step")
        at = f"at step {step}" if step is not None else f"at {start:.1f} s"
        said.append(f"settling (advisory) {at} (the page drew on {end - start:.1f} s on a step that holds it)")
    return said


def motion_notes(capture: dict | None, *, settling: bool = True) -> list[str]:
    """What a take's notes say of its motion that will step: each stretch mostly synthesised from keyframes,
    then each one captured under :data:`LOW_FPS` none of those covers; with what drew its canvases on
    SwiftShader (:func:`software_words`) when it said any. A stretch of the page settling on a step that holds it
    is said as settling (advisory), never as motion that will step, and left out with *settling* false (what a
    repair leaves to record again)."""
    stretches = (capture or {}).get("stepping") or []
    moving = [s for s in stretches if not (isinstance(s, dict) and s.get("settling"))]
    stepped = stepping_words({"stepping": moving})
    lows = low_words({"low": besides((capture or {}).get("low"), stretches)})
    notes = [
        *(f"{said[:1].upper()}{said[1:]}: its motion will step." for said in stepped),
        *(f"{said[:1].upper()}{said[1:]}: its animation will step." for said in lows),
    ]
    if notes and (software := software_words(capture)):
        notes.append(software)
    # Frames drawn at another scale on a step holding the page: every run in one line, a take to record again.
    if faults := fault_words(capture):
        notes.append(
            "Capture fault, not a motion of the page - "
            + "; ".join(faults)
            + ": the frames about each run are alike, the page did not move. Record the take again."
        )
    if settling:
        notes += [f"{said[:1].upper()}{said[1:]}: nothing to record again." for said in settling_words(capture)]
    return notes


def refusal(capture: dict | None, fallback: str | None, events: list[dict] | None = None) -> str:
    """Why a take recorded on GPU GL under ``fps_fallback: refuse`` is refused once measured: each stretch of its
    motion still mostly synthesised from keyframes (``stepping``) that reads as steps (:func:`stepping_grade`
    ``dense``) - the page settling on a step that holds it left out -, with the rate it was painted at; and each
    paced transition of its *events* (as the take keeps them, paced) over 30% drawn from keyframes or held then cut
    (``scroll_pacing.synthesised_transitions``): the transitions the save check would find ``synthesised_transition``
    are never kept ready (#10869 retest). Empty where the take keeps: another ``fps_fallback``, a take not drawn on
    GPU GL (software GL keeps saying its stepping as notes), or no such stretch. A span the page itself drew under the
    rate, every frame it drew kept and held, the worker's quota holding nothing back over it, is page-limited: kept at
    the page's own frames, said in the notes (:func:`page_limited_notes`), never refused (#10970)."""
    if fallback != "refuse" or ((capture or {}).get("gl") or {}).get("gpu") is not True:
        return ""
    # Each span's words end inside the parenthesis of its rates: its cause closes it, as before #11097.
    said = [f"{what}; {cause})" if cause else f"{what})" for what, cause in refused_spans(capture, fallback, events)]
    if not said:
        return ""
    rate = float((capture or {}).get("fps") or 0)
    fps = next((s.get("fps") for s in (capture or {}).get("stepping") or [] if isinstance(s, dict)), None) or (
        int(rate) if rate else None
    )
    return (
        f"Refused after recording: this GPU worker did not capture the take's motion at {fps} fps - "
        + "; ".join(said[:MOST_CANVAS_RUNS])
        + ". fps_fallback: refuse keeps no motion synthesised from keyframes; record again with fps_fallback: fps"
        + " (a page drawn on the GPU paints at its own rate whatever the window)."
    )


def starved_only(capture: dict | None, fallback: str | None, events: list[dict] | None = None) -> bool:
    """Whether every span :func:`refusal` refuses a take for is starved (the worker's quota or a starved window, never
    the page): such a take is recorded once more before it is refused (#11097, ``runners``)."""
    spans = refused_spans(capture, fallback, events)
    return bool(spans) and all(cause.startswith("starved") for _what, cause in spans)


def refused_spans(
    capture: dict | None, fallback: str | None, events: list[dict] | None = None
) -> list[tuple[str, str]]:
    """The spans :func:`refusal` refuses a take for, each ``(what, cause)``: *what* the span and its rates, *cause*
    :func:`scroll_pacing.refused_cause`. Empty where the take keeps."""
    if fallback != "refuse" or ((capture or {}).get("gl") or {}).get("gpu") is not True:
        return []
    from app.services.video_studio.scroll_pacing import synthesised_transitions, synthesised_words

    mark_starved_spans(capture, events)
    said: list[tuple[str, str]] = []
    # No refusal on a GPU lane advises a smaller window: a canvas drawn on the GPU paints at its own rate whatever the
    # window (#10897), a page transition on a GPU worker is the page's own work, and a stretch read as the page on a
    # wheel or click step was still the canvas (#10943, #10950: both takes advised it on a canvas zoom).
    for stretch in (capture or {}).get("stepping") or []:
        # Drawn by the page under the rate, every frame kept, its quota holding nothing back: page-limited, kept at the
        # page's own frames (Sam's decision on #10970). A span the quota starved is still refused.
        if (
            not isinstance(stretch, dict)
            or stretch.get("settling")
            or page_limited_kept(stretch)
            or probe_kept(stretch)
        ):
            continue
        grade = stepping_grade(stretch)
        # A canvas transition (a wheel zoom, a layout change) mostly synthesised is refused whatever its grade: a brief
        # one read as advisory still played redrawn from keyframes on a GPU lane that did not capture it (#10867).
        if grade is None or (grade.severity != "dense" and not stretch.get("canvas")):
            continue
        step = stretch.get("step")
        at = f"step {step}" if step is not None else f"{float(stretch['t'][0]):.1f} s"
        what = stretch.get("canvas") or "the page"
        said.append(
            (
                f"{what} at {at} painted at {grade.painted_fps:.1f} fps over {grade.seconds:.1f} s "
                f"({stretch['synthesised']} of its {stretch['frames']} frames at {stretch['fps']} fps synthesised",
                refused_cause(stretch),
            )
        )
    rate = float((capture or {}).get("fps") or 0)
    for item in synthesised_transitions(events, fps=rate)[:MOST_CANVAS_RUNS]:
        words = synthesised_words([{**item, "cause": ""}])
        # The words end on the closing parenthesis of their rates: the cause goes inside it, as a stretch's does.
        said.append((words[:-1] if words.endswith(")") else words, str(item.get("cause") or "")))
    return said


def rates_words(capture: dict | None) -> str:
    """``capture: 31 30 30 12 9 28 ... frames a second`` - what a take's answer says of its capture, each second."""
    rates = (capture or {}).get("rates") or []
    if not rates:
        return ""
    shown = " ".join(str(int(n)) for n in rates[:120])
    more = f" (+{len(rates) - 120} s)" if len(rates) > 120 else ""
    return f"frames kept each second: {shown}{more}{idle_words(capture)}"


def idle_words(capture: dict | None) -> str:
    """`` (idle 24 s: the page rested, frames held; starved at 12-13.4 s (6 fps))`` - what a take's capture line says
    of the seconds its page rested and those it asked to move but were not delivered (``insert_rate`` measured on the
    page's own paint requests, ``insert_floor.classify``, #10101); empty for a take measured before."""
    measured = (capture or {}).get("insert_rate")
    if not isinstance(measured, dict) or not measured.get("painting"):
        return ""
    parts = []
    idle = float(measured.get("idle") or 0.0)
    if idle >= 1.0:
        parts.append(f"idle {idle:.0f} s: the page rested, frames held")
    starved = [s for s in measured.get("starved") or [] if isinstance(s, list | tuple) and len(s) == 3]
    if starved:
        parts.append(
            "starved at " + ", ".join(f"{float(a):g}-{float(b):g} s ({float(f):g} fps)" for a, b, f in starved)
        )
    return f" ({'; '.join(parts)})" if parts else ""


# ── What a recording draws its canvases on ────────────────────────


@dataclass(frozen=True)
class GlBackend:
    """What this worker's recordings draw WebGL and their canvases on (:func:`gl_backend`): its launch flags, whether
    they draw on a GPU, and why not when they do not."""

    args: tuple[str, ...]
    gpu: bool
    why: str | None = None
    #: A recording lane (``VIDEO_STUDIO_RECORDING_GL=gpu``) whose Chromium did not draw on a GPU: it records nothing.
    refused: bool = False
    #: The WebGL renderer its probe read (:data:`RENDERER_JS`), when it read one.
    renderer: str | None = None

    @property
    def alone(self) -> bool:
        """Whether its recordings capture each canvas alone too (``canvas_alone``): on software GL, where the
        screencast of the whole page falls short of a canvas's transition first, and on GPU GL, where a
        re-layout after a click was still screencast at 11 to 20 fps: the copies are made at each animation frame of
        the take's rate and composited only where the screencast fell short and they match what it painted."""
        return True

    @property
    def alone_ratio(self) -> float:
        """The pixel ratio its canvases are copied at (``canvas_alone``): ``canvas_alone.ALONE_RATIO`` on software GL,
        ``canvas_alone.GPU_ALONE_RATIO`` on a GPU, where a copy at the take's own ratio held the encoders back."""
        from app.services.video_studio.canvas_alone import ALONE_RATIO, GPU_ALONE_RATIO

        return GPU_ALONE_RATIO if self.gpu else ALONE_RATIO

    @property
    def alone_quality(self) -> float:
        """The JPEG quality its canvas copies are encoded at (``canvas_alone``), higher on a GPU."""
        from app.services.video_studio.canvas_alone import ALONE_QUALITY, GPU_ALONE_QUALITY

        return GPU_ALONE_QUALITY if self.gpu else ALONE_QUALITY

    @property
    def alone_encoders(self) -> int:
        """The threads its pages encode their canvas copies on (``canvas_alone``): more on a GPU, where one thread
        encoding copies at the take's own ratio fell behind a re-layout, as many as the worker's CPUs leave beside
        the page and the screencast (``canvas_alone.gpu_encoders``)."""
        from app.services.video_studio.canvas_alone import ENCODERS, gpu_encoders

        return gpu_encoders() if self.gpu else ENCODERS

    def words(self) -> str:
        """``GPU GL``, ``software GL (Chromium found no GPU to draw WebGL on: webgl unavailable_software)``."""
        return "GPU GL" if self.gpu else f"software GL ({self.why})" if self.why else "software GL"

    def as_dict(self) -> dict:
        return {"gpu": self.gpu, **({"why": self.why} if self.why else {})}


#: The backend this process decided (:func:`gl_backend`): one at most, decided at its first recording.
_BACKEND: list[GlBackend] = []
#: A WebGL context made in a page, so its browser's GPU process is up before its feature status is read.
_WEBGL_JS = (
    "() => { const c = document.createElement('canvas'); return !!(c.getContext('webgl2') || c.getContext('webgl')); }"
)


def on_gpu(status: dict | None) -> bool:
    """Whether a browser's GPU feature status (:func:`gl_status`) says it draws WebGL on a GPU."""
    return str((status or {}).get("webgl") or "").startswith(ON_GPU_STATUS)


def software_renderer(renderer: str | None) -> str | None:
    """The CPU rasterizer a WebGL renderer names (:data:`SOFTWARE_RENDERERS`), ``None`` for a hardware one or
    none read."""
    named = str(renderer or "").lower()
    return next((found for found in SOFTWARE_RENDERERS if found in named), None)


def drawn_on_gpu(status: dict | None) -> bool:
    """Whether a probe (:func:`_probe`) drew WebGL on a GPU: its feature status says so and its renderer names no CPU
    rasterizer (an llvmpipe ANGLE reads ``enabled`` too)."""
    return on_gpu(status) and software_renderer((status or {}).get("renderer")) is None


def _read_words(status: dict) -> str:
    """What one probe read, for a refusal: ``webgl enabled, renderer ANGLE (Mesa, llvmpipe ...), a CPU rasterizer
    (llvmpipe)``."""
    renderer = status.get("renderer")
    software = software_renderer(renderer)
    said = f"webgl {status.get('webgl')}, renderer {renderer or 'unknown'}"
    return f"{said}, a CPU rasterizer ({software})" if software and on_gpu(status) else said


async def gl_status(browser: Any) -> dict:
    """What *browser* draws WebGL and its 2D canvases on, as Chromium's GPU feature status says (``SystemInfo``):
    ``{"webgl": ..., "canvas_2d": ...}``, :data:`ON_GPU_STATUS` on a GPU, ``unavailable_software`` on SwiftShader."""
    session = await browser.new_browser_cdp_session()
    try:
        info = await session.send("SystemInfo.getInfo")
    finally:
        with contextlib.suppress(Exception):  # the browser may be closing
            await session.detach()
    gpu = (info or {}).get("gpu") or {}
    status = gpu.get("featureStatus") or {}
    said = {"webgl": str(status.get("webgl") or "unknown"), "canvas_2d": str(status.get("2d_canvas") or "unknown")}
    devices = gpu_devices(gpu)
    if devices:
        said["devices"] = devices
    return said


def gpu_devices(gpu: dict) -> str:
    """The GPUs Chromium's GPU process saw (``SystemInfo.getInfo`` ``gpu.devices``), for a refusal:
    ``0x10de:0x1eb8 NVIDIA ... (driver NVIDIA 550.163.01)``, comma-separated; empty when none was listed. A lane whose
    Chromium lists no NVIDIA device (vendor ``0x10de``) never reached the card, whatever its GL loaders hold."""
    said: list[str] = []
    for device in gpu.get("devices") or []:
        if not isinstance(device, dict):
            continue
        vendor, ident = int(device.get("vendorId") or 0), int(device.get("deviceId") or 0)
        words = f"0x{vendor:04x}:0x{ident:04x}"
        named = str(device.get("deviceString") or "").strip()
        driver = " ".join(str(device.get(key) or "").strip() for key in ("driverVendor", "driverVersion")).strip()
        said.append(f"{words} {named}".strip() + (f" (driver {driver})" if driver else ""))
    return ", ".join(said)[:300]


def launch_env(args: tuple[str, ...]) -> dict[str, str] | None:
    """The environment a Chromium launched under *args* is given: the worker's own with :data:`GPU_EGL_ENV`
    under :data:`GPU_EGL_ARGS`, ``None`` (the worker's, unchanged) under any other launch flags."""
    return {**os.environ, **GPU_EGL_ENV} if tuple(args) == GPU_EGL_ARGS else None


async def _probe(args: tuple[str, ...]) -> dict:
    """:func:`gl_status` of a Chromium launched under *args* (with :func:`launch_env`), once a page of it made a WebGL
    context, with the WebGL renderer that page read (``renderer``, :data:`RENDERER_JS`)."""
    from playwright.async_api import async_playwright

    from app.services.browser_profiles.browser import PROFILE_CHROMIUM_ARGS
    from app.services.headless_render import RENDER_CHROMIUM_ARGS, RENDER_SERVICE_WORKERS

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True, args=[*RENDER_CHROMIUM_ARGS, *PROFILE_CHROMIUM_ARGS, *args], env=launch_env(args)
        )
        try:
            context = await browser.new_context(service_workers=RENDER_SERVICE_WORKERS)
            page = await context.new_page()
            await page.evaluate(_WEBGL_JS)
            status = await gl_status(browser)
            with contextlib.suppress(Exception):  # the status has the last word, the renderer is said when read
                status["renderer"] = str(await page.evaluate(RENDERER_JS))[:200]
            return status
        finally:
            await browser.close()


def gl_mode() -> str:
    """``VIDEO_STUDIO_RECORDING_GL`` read as one of :data:`GL_MODES`; anything else reads ``auto``."""
    mode = str(settings.video_studio_recording_gl or "auto").strip().lower()
    return mode if mode in GL_MODES else "auto"


def take_concurrency() -> int:
    """Recordings this worker runs at a time (``VIDEO_STUDIO_TAKE_CONCURRENCY``, 0 when it records none): one on a
    recording lane (``VIDEO_STUDIO_RECORDING_GL=gpu``), whatever the setting says. Two takes on one GPU worker held
    its quota back 22 to 35 s a take while the page, the screencast and the copies' encoders shared its CPUs, and
    their canvas transitions fell to 9 to 18 fps ("copied no faster than painted", #9266)."""
    wanted = max(0, int(settings.video_studio_take_concurrency))
    return min(wanted, 1) if gl_mode() == "gpu" else wanted


def recording_refused() -> str | None:
    """Why this worker claims no recording on its GL, ``None`` when it may: a recording lane
    (``VIDEO_STUDIO_RECORDING_GL=gpu``) records only once its probe found the GPU (:func:`gl_backend`), never on
    SwiftShader. Any other mode records as before."""
    if gl_mode() != "gpu":
        return None
    if not _BACKEND:
        return "its GPU is not probed yet"
    found = _BACKEND[0]
    return (found.why or "its Chromium draws WebGL off the GPU") if found.refused else None


def lane_gl() -> dict | None:
    """What a recording lane (``VIDEO_STUDIO_RECORDING_GL=gpu``) says of its GL in its presence record:
    ``{"gpu": bool, "refused": str | None, "renderer": str | None}``, ``refused`` the reason it claims no recording;
    ``None`` in any other mode."""
    if gl_mode() != "gpu":
        return None
    found = _BACKEND[0] if _BACKEND else None
    return {
        "gpu": bool(found and found.gpu),
        "refused": recording_refused(),
        "renderer": found.renderer if found else None,
    }


def worker_record() -> dict | None:
    """This worker's recording class and GPU model (``worker_class``): ``{"class": "l4", "gpu": "L4"}``,
    ``{"class": "cpu", "gpu": None}`` on software GL; ``None`` while its GL is not decided yet (``auto`` before its first
    recording) or its recording lane refuses recordings."""
    from app.services.video_studio import worker_class

    if gl_mode() == "software":
        return {"class": worker_class.CPU, "gpu": None}
    found = _BACKEND[0] if _BACKEND else None
    if found is None or found.refused:
        return None
    return {
        "class": worker_class.class_of(gpu=found.gpu, renderer=found.renderer),
        "gpu": worker_class.model_of(found.renderer) if found.gpu else None,
    }


#: The NVIDIA user-space libraries a recording lane's Chromium needs from the container runtime: the EGL
#: vendor GLVND dispatches to (ANGLE on EGL) and the library the Vulkan ICD names (ANGLE on Vulkan).
NVIDIA_GL_LIBRARIES = ("libEGL_nvidia.so.0", "libGLX_nvidia.so.0")
#: Where the EGL vendor files (GLVND) and the Vulkan ICD files are looked for.
GL_VENDOR_DIRS = (
    "/usr/share/glvnd/egl_vendor.d",
    "/etc/glvnd/egl_vendor.d",
    "/usr/share/vulkan/icd.d",
    "/etc/vulkan/icd.d",
)
#: The seconds a lane's library load check (:func:`_load_library`) may take before it is said to have hung.
LOAD_CHECK_SECONDS = 10.0
#: Loads one shared library by name in a child process and prints ``ok`` or the dynamic loader's error, which names
#: the missing file (a dependency the runtime did not mount). A child, so nothing NVIDIA is loaded in the worker.
_LOAD_PY = (
    "import ctypes, sys\ntry:\n    ctypes.CDLL(sys.argv[1])\n    print('ok')\nexcept OSError as exc:\n    print(exc)\n"
)


async def _load_library(name: str) -> str:
    """``loads`` when the dynamic loader opens *name* in a child process, else what the loader said."""
    return await _child(_LOAD_PY, name, ok="loads")


async def _child(code: str, *args: str, env: dict[str, str] | None = None, ok: str | None) -> str:
    """The last line a Python child running *code* printed (*ok* when it printed ``ok``), bounded to 300
    characters, or why it did not answer. A child, so nothing NVIDIA is loaded in the worker."""
    import sys

    try:
        child = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            code,
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=env,
        )
    except OSError as exc:
        return f"not checked ({format_exception_message(exc)[:80]})"
    try:
        out, _ = await asyncio.wait_for(child.communicate(), timeout=LOAD_CHECK_SECONDS)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            child.kill()
        return f"hung over {LOAD_CHECK_SECONDS:g} s"
    said = out.decode(errors="replace").strip()
    if ok is not None and said == "ok":
        return ok
    return (said.splitlines()[-1] if said else f"exit {child.returncode}")[:300]


def _listed(directory: str, prefix: str = "") -> list[str]:
    """The file names in *directory* starting with *prefix*, sorted; empty when it cannot be read."""
    try:
        return sorted(entry.name for entry in Path(directory).iterdir() if entry.name.startswith(prefix))
    except OSError:
        return []


async def container_gl_evidence() -> str:
    """What a refused recording lane's container holds of the NVIDIA driver, for its refusal: the device nodes,
    the runtime's environment, whether each of :data:`NVIDIA_GL_LIBRARIES` loads (or the loader's error naming the
    missing file) and the EGL vendor and Vulkan ICD files. Read once, when the lane refuses; never raises."""
    import os

    parts = [f"/dev: {', '.join(_listed('/dev', 'nvidia')) or 'no nvidia device'}"]
    for name in ("NVIDIA_DRIVER_CAPABILITIES", "NVIDIA_VISIBLE_DEVICES"):
        parts.append(f"{name}={os.environ.get(name, '(unset)')}")
    for library in NVIDIA_GL_LIBRARIES:
        try:
            parts.append(f"{library}: {await _load_library(library)}")
        except Exception as exc:  # evidence only: a check that fails says so
            parts.append(f"{library}: not checked ({format_exception_message(exc)[:80]})")
    for directory in GL_VENDOR_DIRS:
        found = _listed(directory)
        if found:
            parts.append(f"{directory}: {', '.join(found)}")
    try:
        parts.append(f"EGL under the lane's environment: {await _egl_check()}")
    except Exception as exc:  # evidence only: a check that fails says so
        parts.append(f"EGL under the lane's environment: not checked ({format_exception_message(exc)[:80]})")
    return "; ".join(parts)


#: Asks libEGL, in a child process, what ANGLE on EGL asks it: the client extensions it needs (device
#: enumeration, the device and surfaceless platforms), how many EGL devices the vendor enumerates, then
#: ``eglInitialize`` on the first device, the surfaceless and the default display, each with its vendor or its EGL
#: error. Prints one line; run under :func:`launch_env` of :data:`GPU_EGL_ARGS`, so the vendor GLVND picks is the
#: one Chromium's GPU process gets.
_EGL_PY = r"""import ctypes
try:
    egl = ctypes.CDLL("libEGL.so.1")
except OSError as exc:
    print("libEGL.so.1: %s" % exc)
    raise SystemExit
vp, i32 = ctypes.c_void_p, ctypes.c_int32
egl.eglQueryString.restype, egl.eglQueryString.argtypes = ctypes.c_char_p, [vp, i32]
egl.eglGetError.restype = i32
egl.eglGetProcAddress.restype, egl.eglGetProcAddress.argtypes = vp, [ctypes.c_char_p]
egl.eglInitialize.restype, egl.eglInitialize.argtypes = ctypes.c_uint, [vp, ctypes.POINTER(i32), ctypes.POINTER(i32)]
egl.eglTerminate.argtypes = [vp]
out = []
ext = (egl.eglQueryString(None, 0x3055) or b"").decode(errors="replace").split()
wanted = ("EGL_EXT_device_enumeration", "EGL_EXT_platform_device", "EGL_MESA_platform_surfaceless")
out.append("client extensions " + " ".join(n[4:] + ("+" if n in ext else "-") for n in wanted))
def fn(name, res, args):
    addr = egl.eglGetProcAddress(name)
    return ctypes.CFUNCTYPE(res, *args)(addr) if addr else None
get_display = fn(b"eglGetPlatformDisplayEXT", vp, [ctypes.c_uint, vp, vp])
query_devices = fn(b"eglQueryDevicesEXT", ctypes.c_uint, [i32, ctypes.POINTER(vp), ctypes.POINTER(i32)])
device = None
if query_devices:
    found, count = (vp * 8)(), i32(0)
    if query_devices(8, found, ctypes.byref(count)):
        out.append("devices %d" % count.value)
        device = found[0] if count.value else None
    else:
        out.append("eglQueryDevicesEXT error 0x%x" % egl.eglGetError())
else:
    out.append("no eglQueryDevicesEXT")
def init(named, display):
    if not display:
        out.append("%s: no display (0x%x)" % (named, egl.eglGetError()))
        return
    major, minor = i32(0), i32(0)
    if egl.eglInitialize(display, ctypes.byref(major), ctypes.byref(minor)):
        vendor = (egl.eglQueryString(display, 0x3053) or b"?").decode(errors="replace")
        out.append("%s: EGL %d.%d %s" % (named, major.value, minor.value, vendor))
        egl.eglTerminate(display)
    else:
        out.append("%s: eglInitialize error 0x%x" % (named, egl.eglGetError()))
if get_display and device:
    init("device", get_display(0x313F, device, None))
if get_display:
    init("surfaceless", get_display(0x31DD, None, None))
egl.eglGetDisplay.restype, egl.eglGetDisplay.argtypes = vp, [vp]
init("default", egl.eglGetDisplay(None))
print(", ".join(out))
"""


async def _egl_check() -> str:
    """What libEGL answers under the lane's EGL environment (:data:`_EGL_PY`), or why it could not be asked."""
    return await _child(_EGL_PY, env=launch_env(GPU_EGL_ARGS), ok=None)


async def _gpu_lane() -> GlBackend:
    """The backend of a recording lane (``VIDEO_STUDIO_RECORDING_GL=gpu``): ANGLE on Vulkan, then on EGL, the
    first whose Chromium draws WebGL on a GPU; refused (it records nothing) when neither does, saying what each read
    and what its container holds of the NVIDIA driver (:func:`container_gl_evidence`)."""
    read: list[str] = []
    for args, named in ((GPU_GL_ARGS, "ANGLE on Vulkan"), (GPU_EGL_ARGS, "ANGLE on EGL")):
        try:
            status = await _probe(args)
        except Exception as exc:  # the next launch is tried
            read.append(f"{named}: not probed ({format_exception_message(exc)[:120]})")
            continue
        renderer = status.get("renderer")
        if drawn_on_gpu(status):
            return GlBackend(args, gpu=True, renderer=renderer)
        read.append(f"{named}: {_read_words(status)}, GPU seen by Chromium: {status.get('devices') or 'none'}")
    try:
        held = await container_gl_evidence()
    except Exception as exc:  # evidence only: the refusal stands without it
        held = f"not read ({format_exception_message(exc)[:80]})"
    return GlBackend(
        SOFTWARE_GL_ARGS,
        gpu=False,
        why=(
            f"VIDEO_STUDIO_RECORDING_GL=gpu and Chromium found no GPU to draw WebGL on ({'; '.join(read)}). "
            f"In the container: {held}"
        ),
        refused=True,
    )


async def gl_backend() -> GlBackend:
    """What this worker's recordings draw WebGL and their canvases on, decided once per process: its GPU where
    ``VIDEO_STUDIO_RECORDING_GL`` is ``auto`` and a Chromium launched under :data:`GPU_GL_ARGS` draws WebGL on one
    (:func:`gl_status`), SwiftShader (:data:`SOFTWARE_GL_ARGS`) otherwise, saying why. A probe that cannot run is not
    kept: the next recording asks again."""
    if _BACKEND:
        return _BACKEND[0]
    mode = gl_mode()
    if mode == "software":
        found = GlBackend(SOFTWARE_GL_ARGS, gpu=False, why="VIDEO_STUDIO_RECORDING_GL=software")
    elif mode == "gpu":
        # A recording lane: the GPU or nothing, decided once (a lane without its GPU stays refused until restarted).
        found = await _gpu_lane()
        if found.refused:
            logger.error("Video Studio recording lane refuses recordings: %s", found.why)
    else:
        try:
            status = await _probe(GPU_GL_ARGS)
        except Exception as exc:  # recorded on SwiftShader, as before a GPU was looked for
            said = format_exception_message(exc)
            logger.info("GPU GL not probed for the recordings: %s", said)
            return GlBackend(SOFTWARE_GL_ARGS, gpu=False, why=f"the GPU could not be probed: {said[:160]}")
        found = (
            GlBackend(GPU_GL_ARGS, gpu=True, renderer=status.get("renderer"))
            if drawn_on_gpu(status)
            else GlBackend(
                SOFTWARE_GL_ARGS,
                gpu=False,
                why=f"Chromium found no GPU to draw WebGL on: {_read_words(status)}",
            )
        )
    _BACKEND[:] = [found]
    logger.info(
        "Video Studio recordings draw their canvases with %s%s%s",
        found.words(),
        f" (WebGL renderer: {found.renderer})" if found.renderer else "",
        f"; a canvas transition the screencast falls short on is captured on the canvas alone at pixel ratio "
        f"{found.alone_ratio:g}"
        if found.alone
        else "",
    )
    return found


def refusal_gl_words(backend: GlBackend) -> str:
    """What a refused take says of the GL its warm-up drew on: the backend, the WebGL renderer its probe read
    and ``VIDEO_STUDIO_RECORDING_GL`` - on a recording lane, whether it measured the GPU or SwiftShader."""
    said = f"{backend.words()}, WebGL renderer {backend.renderer}" if backend.renderer else backend.words()
    mode = "" if "VIDEO_STUDIO_RECORDING_GL" in said else f" (VIDEO_STUDIO_RECORDING_GL={gl_mode()})"
    return f" Its warm-up drew on {said}{mode}."


def refusal_probe_words(measured: dict | None) -> str:
    """What a refused take says of its warm-ups: the screencast format and quality they asked for, then every
    size measured beyond the take's own (``preflight.probes``, painted pixels) with its rate - what a capture-rate fix
    reads without the worker's log."""
    probes = [p for p in (measured or {}).get("probes") or [] if isinstance(p, list | tuple) and len(p) == 3]
    listed = (
        "; smaller sizes measured: " + ", ".join(f"{w}x{h} at {fps:g} fps" for w, h, fps in probes) if probes else ""
    )
    return f" Its warm-ups captured JPEG screencast frames at quality {WARM_UP_QUALITY}{listed}."


def drawn_with(backend: GlBackend, status: dict | None) -> dict:
    """What a take keeps of the GL it was drawn with (``capture.gl``): *backend*'s choice, and the feature status its
    own browser said once recorded (*status*; ``None`` when it could not say), which has the last word on the GPU."""
    kept = backend.as_dict()
    if status:
        kept.update(status, gpu=on_gpu(status))
        if backend.gpu and not kept["gpu"]:
            kept["why"] = f"its browser drew WebGL off the GPU it was launched for: webgl {status['webgl']}"
    return kept


def software_words(capture: dict | None) -> str:
    """What a take drawn on SwiftShader says of it (*capture*'s ``gl``); empty for one drawn on a GPU, or not said."""
    gl = (capture or {}).get("gl") or {}
    if gl.get("gpu") is not False:
        return ""
    why = f" ({gl['why']})" if gl.get("why") else ""
    # What its canvas-only path did (``canvas_alone``): a take recorded before it says nothing of it.
    alone = (capture or {}).get("alone")
    if isinstance(alone, dict) and alone.get("off"):
        copied = f", its canvases not captured alone ({alone['off']})"
    elif isinstance(alone, dict):
        copied = (
            ", a canvas captured alone at reduced quality only where the screencast fell short and the copy matched it"
        )
    else:
        copied = ""
    return (
        f"This worker drew its canvases on software GL{why}{copied}: a worker whose Chromium draws on a GPU captures "
        "them with their page at the take's rate (the deployment's to add: VIDEO_STUDIO_RECORDING_GL, a recording "
        "lane on GPU workers)."
    )


# ── Measured before step 1 ────────────────────────────────────────────────


async def warm_up(
    browser: Any, viewport: tuple[int, int], ratio: float, *, seconds: float = PREFLIGHT_SECONDS
) -> float:
    """Frames a second *browser*'s screencast hands over of a page painting its whole frame on every tick, laid out in
    *viewport* at *ratio*: the most a take of that size captures on this worker now."""
    from app.services.headless_render import RENDER_SERVICE_WORKERS

    context = await browser.new_context(
        viewport={"width": viewport[0], "height": viewport[1]},
        device_scale_factor=ratio,
        service_workers=RENDER_SERVICE_WORKERS,
    )
    try:
        page = await context.new_page()
        await page.set_content(_WARM_UP_PAGE)
        cdp = await context.new_cdp_session(page)
        loop = asyncio.get_running_loop()
        stamps: list[float] = []

        async def ack(session_id: Any) -> None:
            with contextlib.suppress(Exception):  # the warm-up may be closing
                await cdp.send("Page.screencastFrameAck", {"sessionId": session_id})

        def on_frame(event: dict) -> None:
            stamps.append(time.monotonic())
            loop.create_task(ack(event.get("sessionId")))

        cdp.on("Page.screencastFrame", on_frame)
        await cdp.send(
            "Page.startScreencast",
            {
                "format": "jpeg",
                "quality": WARM_UP_QUALITY,
                "maxWidth": round(viewport[0] * ratio),
                "maxHeight": round(viewport[1] * ratio),
                "everyNthFrame": 1,
            },
        )
        await asyncio.sleep(PREFLIGHT_SETTLE_SECONDS)
        counted = len(stamps)
        await asyncio.sleep(seconds)
        rate = (len(stamps) - counted) / seconds
        with contextlib.suppress(Exception):
            await cdp.send("Page.stopScreencast")
        return rate
    finally:
        with contextlib.suppress(Exception):
            await context.close()


#: Frames a second this worker captures of a window (CSS pixels) at a pixel ratio (:func:`warm_up` under the take's
#: browser zoom, ``plan``).
Measure = Callable[[tuple[int, int], float], Awaitable[float]]
#: What runs on the take's worker beside it now (``recording_lane.Slot.beside``): a ``recording_lane.Load``, ``None``
#: when it could not be read.
Beside = Callable[[], Awaitable[Any]]


# ── The rates measured on this worker shape ───────────────────────

#: Frames a second measured at a painted size, by worker shape (:func:`worker_shape`): ``{shape: {(width, height):
#: (fps, when)}}``, *when* on the monotonic clock.
_TABLE: dict[tuple[int, int], dict[tuple[int, int], tuple[float, float]]] = {}


def worker_shape() -> tuple[int, int]:
    """The worker shape a capture rate holds for: its CPUs and its memory ceiling in MiB to the nearest 256
    (``0``: none), as ``resources`` reads them."""
    from app.services.video_studio import resources

    return resources.cpu_budget(), round((resources.memory_limit() or 0) / (1 << 20) / 256) * 256


def shape_words(shape: tuple[int, int]) -> str:
    """``4 CPUs, 7.5 GiB``."""
    cpus, memory = shape
    return f"{cpus} CPU{'s' if cpus != 1 else ''}" + (f", {memory / 1024:.1f} GiB" if memory else "")


def remember(shape: tuple[int, int], painted: tuple[int, int], fps: float) -> None:
    """Keep *fps* as what this worker *shape* captures of frames *painted* pixels large."""
    _TABLE.setdefault(shape, {})[(int(painted[0]), int(painted[1]))] = (float(fps), time.monotonic())


def measured_table(shape: tuple[int, int]) -> dict[tuple[int, int], float]:
    """The rates *shape*'s table holds by painted size, measured in the last :data:`TABLE_SECONDS` (older ones
    dropped): a busy hour's measures never stand for the next."""
    now = time.monotonic()
    fresh = {size: entry for size, entry in _TABLE.get(shape, {}).items() if now - entry[1] <= TABLE_SECONDS}
    _TABLE[shape] = fresh
    return {size: fps for size, (fps, _at) in fresh.items()}


def narrowest(window: tuple[int, int]) -> int:
    """The narrowest window of *window*'s shape a recording takes: its width and its height within a viewport's."""
    from app.services.video_studio.spec import MIN_VIEWPORT_HEIGHT, MIN_VIEWPORT_WIDTH

    return max(MIN_VIEWPORT_WIDTH, math.ceil(MIN_VIEWPORT_HEIGHT * window[0] / window[1]))


def sustained_window(viewport: tuple[int, int], ratio: float, measured: float, wanted: float) -> tuple[int, int] | None:
    """The largest window of *viewport*'s shape whose frames at *ratio* the worker would capture at *wanted* frames a
    second, having captured *viewport*'s at *measured* (a frame's cost goes with its pixels); ``None`` under the
    narrowest a recording takes (:func:`narrowest`). The first guess of :func:`sustained_size`, which measures it."""
    if measured <= 0:
        return None
    scale = math.sqrt(measured / wanted)
    width = math.floor(viewport[0] * scale / 10) * 10
    if width < narrowest(viewport):
        return None
    return width, round(width * viewport[1] / viewport[0])


@dataclass(frozen=True)
class Sustained:
    """The largest window found sustaining a take's fps (:func:`sustained_size`), its pixel ratio and the rate measured
    at it - or at a larger frame that sustained it."""

    window: tuple[int, int]
    ratio: float
    fps: float

    @property
    def painted(self) -> tuple[int, int]:
        return round(self.window[0] * self.ratio), round(self.window[1] * self.ratio)

    def words(self) -> str:
        """``a 1460x821 window at pixel ratio 1.5 (2190x1232 painted, 60 fps measured)``."""
        width, height = self.painted
        return (
            f"a {self.window[0]}x{self.window[1]} window at pixel ratio {self.ratio:g} ({width}x{height} painted, "
            f"{self.fps:.0f} fps measured)"
        )


def _of_shape(width: int, window: tuple[int, int]) -> tuple[int, int]:
    return width, round(width * window[1] / window[0])


def _as_wide(painted: tuple[int, int], window: tuple[int, int], ratio: float) -> float:
    """The width of a window of *window*'s shape painting as many pixels as *painted* at *ratio*."""
    return math.sqrt(painted[0] * painted[1] * window[0] / window[1]) / ratio


async def sustained_size(
    window: tuple[int, int],
    ratio: float,
    wanted: float,
    measure: Measure,
    known: dict[tuple[int, int], float],
    *,
    probes: int = MAX_PROBES,
) -> Sustained | None:
    """The largest window of *window*'s shape - at pixel ratio *ratio*, else at the first lower one of :data:`RATIOS`
    where one does - this worker captures at *wanted* frames a second at least: bounded by the rates *known*
    by painted size (this worker shape's table, the take's own warm-ups; a frame's cost goes with its pixels), measured
    by *measure* in between until the window named is within :data:`SEARCH_TOLERANCE` of the narrowest one that fell
    short, *probes* warm-ups at most. ``None`` when none a recording takes does (:func:`narrowest`)."""
    low = narrowest(window)
    left = probes
    for at in (value for value in RATIOS if value <= ratio + 1e-9):
        good: tuple[int, float] | None = None
        bad: tuple[int, float] | None = None
        for painted, fps in known.items():
            width = _as_wide(painted, window, at)
            if fps >= wanted:
                held = min(window[0], math.floor(width))
                if held >= low and (good is None or held > good[0]):
                    good = (held, fps)
            elif width <= window[0] + 1 and (bad is None or math.ceil(width) < bad[0]):
                bad = (math.ceil(width), fps)
        if good is not None and bad is not None and good[0] >= bad[0]:
            good = None  # two measures disagree: measured again in between
        while left > 0:
            top = bad[0] if bad is not None else window[0] + 1
            if good is not None and (good[0] >= window[0] or top - good[0] <= SEARCH_TOLERANCE * top):
                break
            if good is not None:
                width = (good[0] + top) // 2 // 10 * 10
            elif bad is not None:
                guess = sustained_window(_of_shape(bad[0], window), at, bad[1], wanted)
                width = guess[0] if guess else low
            else:
                width = window[0]
            width = max(low, min(width, top - 1))
            if width >= top or (good is not None and width <= good[0]):
                break
            fps = await measure(_of_shape(width, window), at)
            left -= 1
            if fps >= wanted:
                good = (width, fps)
            else:
                bad = (width, fps)
                if width <= low:
                    break
        if good is not None:
            return Sustained(_of_shape(good[0], window), at, good[1])
    return None


@dataclass(frozen=True)
class CapturePlan:
    """What the warm-up decided (:func:`plan`): the pixel ratio the take is recorded at - with ``fps_fallback`` its
    window or its rate too -, what it says, or why it is refused; what was measured is kept with the take
    (:meth:`record`)."""

    #: The ratio the page is laid out at (the window's pixel ratio times its browser zoom, ``spec.zoomed_window``).
    ratio: float
    #: The window's pixel ratio the take records at (``TakeScript.pixel_ratio``, :data:`LOWERED_RATIO`, or the one a
    #: ``size`` fallback measured).
    pixel_ratio: float
    asked: float
    measured: dict
    note: str | None = None
    refused: str | None = None
    #: A ``size`` fallback's window, and a ``fps`` fallback's rate; ``None``: the take's own.
    window: tuple[int, int] | None = None
    fps: int | None = None
    #: What the first line of its action log says of a fallback taken.
    fallback: str | None = None
    #: What ran beside the warm-up on its worker (``recording_lane.Load``); ``None`` when it was not read.
    beside: Any = None
    #: Short of its fps beside other jobs: nothing recorded, it waits in the recording lane.
    waits: bool = False

    def record(self) -> dict:
        kept = {"pixel_ratio": {"asked": self.asked, "recorded": self.pixel_ratio}, "preflight": self.measured}
        if self.fallback:
            kept["fallback"] = self.fallback
        return kept


@contextlib.asynccontextmanager
async def warm_ups(script: Any) -> AsyncIterator[Measure]:
    """What measures this worker's capture rate for a take of *script* (:data:`Measure`): a bare Chromium of the
    recording's launch flags, a warm-up (:func:`warm_up`) per window at the take's browser zoom; closed after.

    It runs in the worker's process, at the priority the take's capture records at: a scene preview's process
    runs ``cpu_share.PREVIEW_NICE`` steps below it, and yields its CPU to the warm-up as it does to the take."""
    from playwright.async_api import async_playwright

    from app.services.browser_profiles.browser import PROFILE_CHROMIUM_ARGS
    from app.services.headless_render import RENDER_CHROMIUM_ARGS
    from app.services.video_studio.spec import zoomed_window

    gl = await gl_backend()
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True, args=[*RENDER_CHROMIUM_ARGS, *PROFILE_CHROMIUM_ARGS, *gl.args], env=launch_env(gl.args)
        )
        try:

            async def measure(window: tuple[int, int], at: float) -> float:
                css, device = zoomed_window(window, at, script.browser_zoom or 100)
                return await warm_up(browser, css, device)

            yield measure
        finally:
            await browser.close()


async def plan(
    script: Any, viewport: tuple[int, int], ratio: float, *, beside: Beside | None = None, may_wait: bool = False
) -> CapturePlan:
    """Measure the worker's capture rate at the take's frame size before step 1 (:func:`warm_ups`) and decide its pixel
    ratio - or its window or rate, or that it waits for the jobs *beside* it (:func:`choose`). A warm-up that cannot
    run records the take as asked."""
    asked = float(script.pixel_ratio)
    try:
        async with warm_ups(script) as measure:
            planned = await choose(script, viewport, ratio, measure, beside=beside, may_wait=may_wait)
        if planned.refused:
            # What the warm-ups measured and the GL they drew on: a recording lane's refusal says whether it
            # measured the GPU, and the capture each size gave.
            said = refusal_probe_words(planned.measured) + (refusal_gl_words(_BACKEND[0]) if _BACKEND else "")
            planned = replace(planned, refused=planned.refused + said)
        return planned
    except Exception as exc:  # no warm-up: recorded as asked, its rate judged once recorded
        logger.info("Capture warm-up not run: %s", format_exception_message(exc))
        return CapturePlan(ratio, asked, asked, {"error": format_exception_message(exc)[:200]})


@dataclass
class _Measured:
    """The warm-ups of one pre-flight, kept in its worker shape's table once they stand for it (:meth:`keep`)."""

    measure: Measure
    shape: tuple[int, int]
    probes: list[list] = field(default_factory=list)

    async def __call__(self, window: tuple[int, int], at: float) -> float:
        fps = await self.measure(window, at)
        painted = (round(window[0] * at), round(window[1] * at))
        self.probes.append([*painted, round(fps, 1)])
        return fps

    def known(self) -> dict[tuple[int, int], float]:
        """The rates its search starts from (:func:`sustained_size`): its worker shape's table, its own warm-ups over it."""
        found = measured_table(self.shape)
        found.update({(width, height): fps for width, height, fps in self.probes})
        return found

    def keep(self, load: Any) -> None:
        """Its warm-ups in its worker shape's table - those of a worker that ran nothing else (*load*), or whose
        load was not read: a rate measured beside other jobs is theirs, never the worker shape's."""
        if load is not None and not load.idle:
            return
        for width, height, fps in self.probes:
            remember(self.shape, (width, height), fps)


async def choose(
    script: Any,
    viewport: tuple[int, int],
    ratio: float,
    measure: Measure,
    *,
    shape: tuple[int, int] | None = None,
    beside: Beside | None = None,
    may_wait: bool = False,
) -> CapturePlan:
    """:func:`decide` on the rates *measure* reads: the take's own size, :data:`LOWERED_RATIO` when it falls
    short; refused, the largest window this worker *shape* sustains the take's ``fps`` at (:func:`sustained_size`, on
    its table and as many more warm-ups as it takes).

    *beside* reads what runs on the worker beside the take, before and after its own size's warm-up: short of
    its fps beside other jobs, the take that *may_wait* waits in the recording lane (``CapturePlan.waits``) - its
    conditions are the load's, not the worker's -, nothing more measured."""
    shape = shape or worker_shape()
    measured = _Measured(measure, shape)
    asked, wanted = float(script.pixel_ratio), float(script.fps)
    window = (script.viewport.width, script.viewport.height)
    before = await beside() if beside is not None else None
    rate = await measured(window, asked)
    load = _seen(before, await beside()) if beside is not None else None
    if rate < SUSTAIN_SHARE * wanted and may_wait and load is not None and not load.idle:
        return CapturePlan(ratio, asked, asked, _preflight(viewport, ratio, rate, load=load), beside=load, waits=True)
    lower = None
    if rate < SUSTAIN_SHARE * wanted and asked > LOWERED_RATIO:
        lower = await measured(window, LOWERED_RATIO)
    planned = decide(script, viewport, ratio, rate, lower, shape=shape, load=load)
    if planned.refused is None:
        measured.keep(load)
        return planned
    at = LOWERED_RATIO if lower is not None else asked
    unmeasured = None
    try:
        sustained = await sustained_size(window, at, wanted, measured, measured.known())
    except Exception as exc:  # the take's own size is judged all the same: a search that cannot run names none
        logger.info("Capture warm-up of the smaller sizes not run: %s", format_exception_message(exc))
        sustained, unmeasured = None, format_exception_message(exc)[:200]
    measured.keep(load)
    searched = measured.probes[2 if lower is not None else 1 :]
    return decide(
        script,
        viewport,
        ratio,
        rate,
        lower,
        shape=shape,
        sustained=sustained,
        probes=searched,
        unmeasured=unmeasured,
        load=load,
    )


def _seen(before: Any, after: Any) -> Any:
    """What ran beside a warm-up, read before and after it (``recording_lane.seen``)."""
    from app.services.video_studio.recording_lane import seen

    return seen(before, after)


def _preflight(
    viewport: tuple[int, int],
    ratio: float,
    rate: float,
    lower: float | None = None,
    probes: list | None = None,
    load: Any = None,
) -> dict:
    """What a take keeps of its warm-ups (``preflight``): the rate at its frame size, at :data:`LOWERED_RATIO`, the
    sizes searched, and what ran beside them on its worker."""
    measured: dict = {"fps": round(rate, 1), "size": [round(viewport[0] * ratio), round(viewport[1] * ratio)]}
    if lower is not None:
        measured["lowered"] = {"fps": round(lower, 1), "pixel_ratio": LOWERED_RATIO}
    if probes:
        measured["probes"] = probes
    if load is not None:
        measured["beside"] = load.as_dict()
    return measured


def decide(
    script: Any,
    viewport: tuple[int, int],
    ratio: float,
    rate: float,
    lower: float | None,
    *,
    shape: tuple[int, int] | None = None,
    sustained: Sustained | None = None,
    probes: list[list] | None = None,
    unmeasured: str | None = None,
    load: Any = None,
) -> CapturePlan:
    """The pixel ratio a take of *script* records at, its page laid out in *viewport* at *ratio*, the worker having
    captured its frames at *rate* frames a second - and at *lower* at :data:`LOWERED_RATIO` when that was measured:
    its own when the worker sustains its ``fps``, :data:`LOWERED_RATIO` when that does (said). Otherwise its
    ``fps_fallback``: the *sustained* window (``size``), :data:`FALLBACK_FPS` at its own size when *rate* holds it
    (``fps``), or a refusal naming both - *sustained* the largest window found on this worker *shape*, *unmeasured*
    why the smaller sizes could not be measured. The refusal says what ran beside the warm-up on its worker (*load*):
    nothing - the worker itself falls short -, or the jobs whose load it measured, the recording lane offered."""
    from app.services.video_studio.spec import zoomed_window

    asked = float(script.pixel_ratio)
    wanted = float(script.fps)
    window = (script.viewport.width, script.viewport.height)
    zoom = ratio / asked
    measured = _preflight(viewport, ratio, rate, lower, probes, load)
    painted = f"{measured['size'][0]}x{measured['size'][1]}"
    if rate >= SUSTAIN_SHARE * wanted:
        return CapturePlan(ratio, asked, asked, measured, beside=load)
    if lower is not None and lower >= SUSTAIN_SHARE * wanted:
        size = f"{round(window[0] * LOWERED_RATIO)}x{round(window[1] * LOWERED_RATIO)}"
        return CapturePlan(
            LOWERED_RATIO * zoom,
            LOWERED_RATIO,
            asked,
            measured,
            note=(
                f"This worker captured {painted} frames at {rate:.0f} fps before step 1, under the take's {wanted:g} fps: "
                f"recorded at pixel ratio {LOWERED_RATIO:g} ({size}, {lower:.0f} fps measured). Sharpness on a 4K screen "
                "insert is the save's to say (insert_resolution)."
            ),
            beside=load,
        )
    short = f"this worker captures {painted} frames at {rate:.0f} fps" + (
        f" ({lower:.0f} fps at pixel ratio {LOWERED_RATIO:g})" if lower is not None else ""
    )
    # A rate fell back to is named only where the take's size holds all of it: a size named always works.
    slower = wanted > FALLBACK_FPS and rate >= FALLBACK_FPS
    fallback = script.fps_fallback
    if fallback == "size" and sustained is not None:
        _css, device = zoomed_window(sustained.window, sustained.ratio, script.browser_zoom or 100)
        said = f"recorded at {sustained.words()}, the largest this worker sustains {wanted:g} fps at"
        return CapturePlan(
            device,
            sustained.ratio,
            asked,
            measured,
            note=f"Before step 1 {short}, under the take's {wanted:g} fps: {said} (fps_fallback size).",
            window=sustained.window,
            fallback=f"{said} (fps_fallback size: {painted} captured at {rate:.0f} fps)",
            beside=load,
        )
    if fallback == "fps" and slower:
        said = f"recorded at fps {FALLBACK_FPS} at its own size ({rate:.0f} fps measured there)"
        return CapturePlan(
            ratio,
            asked,
            asked,
            measured,
            note=f"Before step 1 {short}, under the take's {wanted:g} fps: {said} (fps_fallback fps).",
            fps=FALLBACK_FPS,
            fallback=f"{said} (fps_fallback fps: under {wanted:g} fps)",
            beside=load,
        )
    worker = shape_words(shape or worker_shape())
    if sustained is not None:
        named = f"the largest size measured sustaining {wanted:g} fps on this worker ({worker}) is {sustained.words()}"
    elif unmeasured:
        named = f"the smaller sizes could not be measured on this worker ({worker}): {unmeasured}"
    else:
        named = f"no window a recording takes sustains {wanted:g} fps on this worker ({worker}), even at pixel ratio 1"
    alternatives = []
    if sustained is not None:
        alternatives.append("'size'")
    if slower:
        named += f"; or fps {FALLBACK_FPS} at the requested size ({rate:.0f} fps measured there)"
        alternatives.append("'fps'")
    elif wanted > FALLBACK_FPS:
        named += f"; fps {FALLBACK_FPS} at the requested size would not hold either ({rate:.0f} fps measured there)"
    taken = f" fps_fallback {' or '.join(alternatives)} records on it without asking." if alternatives else ""
    if load is None:
        why = " The worker may be busy: recording again later may sustain it."
    elif load.idle:
        why = " No other Video Studio job ran on this worker while it measured."
    else:
        why = (
            f" It measured beside other jobs ({load.busy_words()}): record it again to queue it in the recording lane, "
            f"where it starts by itself once their load allows {wanted:g} fps."
        )
    return CapturePlan(
        ratio,
        asked,
        asked,
        measured,
        refused=(
            f"Refused before recording: {short}, under the take's {wanted:g} fps, so its animations would step; {named}."
            f"{taken}{why}"
        ),
        beside=load,
    )
