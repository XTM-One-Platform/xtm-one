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

"""A canvas captured alone where a recording's screencast falls short of it, on software GL and on a GPU.

On a worker without a GPU the recording Chromium draws WebGL and its canvases
on SwiftShader (``capture_rate.gl_backend``), and its screencast hands over a
frame of the whole page only as fast as the worker composites, encodes and
sends one: a graph laid out again was captured at 5 to 16 fps and each frame
held to the next at the take's 30 or 60 (``encoder.frames_to_mezzanine``),
stepping. On a GPU worker the screencast still fell short of a 0.8 s
re-layout after a click (11 to 20 fps): its frames are encoded on the
CPU whatever draws them. So a take captures each canvas alone too
(``capture_rate.GlBackend.alone``), on software GL at :data:`ALONE_RATIO`
and :data:`ALONE_QUALITY`, on GPU GL at :data:`GPU_ALONE_RATIO` and
:data:`GPU_ALONE_QUALITY` (``GlBackend.alone_ratio``):

* **While it records** (:func:`tap_script`, :class:`Tap`). Every document marks
  each canvas its 2D and WebGL drawing calls draw on and, once the callbacks of
  an animation frame have all run - the page's and its own counted, so a WebGL
  canvas is read before its buffer is presented -, copies each canvas drawn,
  with the canvases stacked in its box, over the colour behind them, at pixel
  ratio :data:`ALONE_RATIO`, at most once a frame of the take's rate. A worker
  thread encodes the copy as JPEG at :data:`ALONE_QUALITY` and drops one alike
  to the copy before, so the page's thread pays the copy alone; a canvas copied
  alike :data:`IDLE_AFTER` times running is copied every :data:`IDLE_MS` until
  it changes or the page is acted on. Each copy keeps its instant (the
  screencast's clock), its canvas's visible box and the boxes of what the page
  draws over it (read on a grid, as ``capture._CANVAS_AT_JS`` reads a canvas
  under the wheel), and makes the page paint on for a moment
  (``capture._MOTION_WATCH_JS``), so the screencast hands over the state a
  transition left. The recorder drains the copies every :data:`DRAIN_SECONDS`.
  A page that cannot run the encoder (a ``worker-src`` policy) copies nothing
  and the take says why (``alone.off``).
* **Once recorded** (:func:`composite`, before the take is paced). A canvas's
  copies one after the other - none further apart than ``capture_rate.RUN_GAP``,
  in one box, outside the steps' scrolls (``scroll_pacing`` draws those again)
  - make a run when there are ``capture_rate.CANVAS_FRAMES`` of them over
  ``capture_rate.CANVAS_SECONDS``. A run the screencast moved at
  ``scroll_pacing.PAINTED_ENOUGH`` of the take's rate keeps its painted frames,
  and so does one its copies were no faster than; only the frames painted that
  moved its canvas count (:func:`moved_rate`, ``moved`` on the run), not those
  handed over with the canvas as the frame before had it. Any other is checked against
  what the browser painted with the canvas at rest about it (:func:`check`):
  its last copy against the first frame painted once it stood still, the copy
  it stood at before against the last frame painted before it moved; outside
  what the page draws over it, softened so the copy's own pixel ratio is no
  difference, more than :data:`MISMATCH_SHARE` of the canvas differing by
  :data:`MATCH_LEVELS` grey levels - a stacked layer it did not
  draw, a filter on it, a buffer read once presented - keeps the painted frames
  (``why``). A run that matches is composited: at each of its copies' instants
  the last frame painted by then, each canvas then moving drawn into its box
  from its latest copy but where the page draws over it; the frames painted
  over the run's span stand behind those. What the page holds still over the canvas while it moves and the copies
  miss (a toolbar, a floating button off the ``over`` grid, :func:`static_layers`) stays the frame's painted behind,
  so it is drawn at one place on every frame (#9590); a run whose copies miss more than :data:`STATIC_MOST` of the
  box that way keeps its painted frames (:data:`UNALIGNED`). The take keeps each run (``alone.runs``:
  its canvas, span, rates, whether it was composited and why not) and is paced,
  measured and encoded from the frames composited: ``capture_rate`` measures a
  canvas transition on them (``alone`` on it) and judges its motion stepping on
  them (``stepping_motion``).

**Limits.** A canvas drawn after an animation frame's callbacks (a resize
observer) is copied at the next frame - a WebGL one from its presented buffer,
which the check refuses. What the page draws over a canvas without taking
pointer events (a label layer) is not on the grid: the copy covers it, and the
check refuses the run where that differs. A canvas the page transforms
(``transform``) or draws in a worker (``transferControlToOffscreen``) is not
copied. The colour behind a transparent canvas is its nearest background
colour: an image or a gradient there differs, refused the same way.
"""

from __future__ import annotations

import asyncio
import base64
import bisect
import contextlib
import json
import logging
import math
import time
from collections import OrderedDict
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.core.utils import format_exception_message
from app.services.video_studio.capture_rate import (
    CANVAS_FRAMES,
    CANVAS_SECONDS,
    COMPOSITED_PREFIX,
    RUN_GAP,
    paced_spans,
    step_at,
)

logger = logging.getLogger(__name__)

#: The pixel ratio a canvas is copied at (CSS px times this, at most the take's own) and the JPEG quality of a copy:
#: the reduced quality a transition plays at where the screencast fell short.
ALONE_RATIO = 1.0
ALONE_QUALITY = 0.8
#: The same on a GPU worker: the copy plays a transition at the quality of the frames about it, at pixel ratio 1
#: (the composite scales it to the frame). At the take's own ratio (1.5, 2880x1620 copies) the page paid 21.9 ms
#: a copy and the encoders 165 ms, so three encoders kept 13 copies a second and 14 copies were drawn smaller
#: anyway (#8457, take 7179792a): the readback and the JPEG encode of a copy grow with its pixels, and a copy at
#: ratio 1 has 44% of them. At this ratio a copy is never drawn smaller (:data:`LOWER_AT` lowers to
#: :data:`ALONE_RATIO`), so every copy of a run has one size.
GPU_ALONE_RATIO = 1.0
GPU_ALONE_QUALITY = 0.9
#: The encoder threads a page encodes its copies on, each at most :data:`IN_FLIGHT` copies behind: one on software GL,
#: whose threads draw the page too; :data:`GPU_ENCODERS` on a GPU, where one thread encoding a copy at the take's own
#: ratio fell behind a re-layout ("copied no faster than painted"). A copy goes to the next thread in turn, so a
#: canvas moving keeps every thread busy; one standing still reads alike on each and is copied every
#: :data:`IDLE_MS` only.
ENCODERS = 1
GPU_ENCODERS = 3
#: The CPUs of a GPU worker left to the page's own thread and the screencast's encode: its encoder threads are at
#: most its CPUs less these (``GlBackend.alone_encoders``), one at least. Three encoders on a 4-CPU worker kept its
#: container busy 2.5 CPUs on average while its quota held it back 19.0 s over a 16 s take, the encodes coming in
#: bursts (98 ms a copy, about 19 a second) beside the page that painted at 13 to 16 fps (#8457, take 20d277ef).
#: Two spared (two encoders) did not help: held back 23.3 s, busy 2.6 CPUs, the page still at 12 to 13 fps and 6 copies
#: skipped, every encoder behind (take 07a5505d): one is spared, three encoders on 4 CPUs again.
GPU_SPARE_CPUS = 1
IN_FLIGHT = 4
#: The copies a canvas may be ahead of its rate (:data:`_TAP_JS`'s budget): it is copied at each frame the page draws
#: while its budget, refilled at the take's rate, holds one, so a page drawing near the take's rate with jitter is
#: copied at about that rate, never above it on average (#10961: fixed 1/fps slots copied a 33 fps page at 22 fps).
#: A canvas starts with one copy in hand, so a run is copied at most this many copies above the rate. Two ahead
#: still copied a GPU canvas drawn at 33 to 45 fps at 22 (#10970, take 001ef823, the worker held back 6.3 s by its
#: quota): a page whose thread its container's quota stops draws in bursts at the screen's rate between gaps of 100
#: to 200 ms, and a budget full after 67 ms lets the rest of the gap's refill go. Six (200 ms) still copied a wheel
#: zoom at 24 fps of 31 drawn (#10970, take e48150a0, held back 3.8 s): the quota's gaps run to 300 to 500 ms there,
#: and fifteen (500 ms) keeps their refill for the burst. A canvas the page has not drawn for :data:`COPY_FRESH_MS`
#: starts again with one copy in hand, so a transition after a still canvas is not copied above the rate.
COPY_BUDGET = 15.0
COPY_FRESH_MS = 1000
#: The budget's refill above the take's rate on a GPU's encoders (:func:`copy_rate`): refilled at the take's rate
#: exactly, a canvas drawn at 34 to 35 fps on L4 was still copied at 25 (#10970, retest of #11296, take 51415c30, no
#: copy skipped, 480 drawn and 470 encoded), every frame drawn while the budget held less than one copy lost. The
#: composite keeps the copy current at each frame of the take, so a copy above the rate costs encoder time only; the
#: GPU's encoders spent 29 ms a copy there, three of them about a hundred copies a second. Software GL keeps the rate.
COPY_HEADROOM = 1.25


def copy_rate(fps: int, encoders: int) -> float:
    """The copies a second a canvas's budget refills at (``_TAP_JS``'s ``RATE``): the take's *fps*, times
    :data:`COPY_HEADROOM` on several *encoders* (a GPU's)."""
    return float(fps) * (COPY_HEADROOM if max(1, int(encoders)) > ENCODERS else 1.0)


def flight_depth(encoders: int) -> int:
    """The copies a page may have in flight on its *encoders* before a copy due is skipped (``busy``):
    :data:`IN_FLIGHT` per encoder and, on several encoders (a GPU's), never fewer than a full :data:`COPY_BUDGET`
    plus the copy drawn now; one encoder (software GL, whose threads draw the page too) keeps :data:`IN_FLIGHT`. Three
    encoders held 12, below the budget of 15: a page drawing at the screen's rate after a quota gap spent its refill
    into a queue full at the twelfth copy and the rest of the burst was skipped, so a GPU wheel zoom was copied at 25
    fps of 32 drawn with 51 copies skipped, every encoder behind (#10970, retest a10a3f48, take a103c4df). The
    encoders keep up on average there (the steady copies were not behind), so a deeper queue absorbs the burst
    instead of dropping it."""
    encoders = max(1, int(encoders))
    depth = IN_FLIGHT * encoders
    return depth if encoders <= ENCODERS else max(depth, math.ceil(COPY_BUDGET) + 1)


def gpu_encoders(cpus: int | None = None) -> int:
    """The encoder threads a GPU page encodes its copies on: :data:`GPU_ENCODERS`, at most the worker's CPUs
    (``resources.cpu_budget``, its container's quota included) less :data:`GPU_SPARE_CPUS`, one at least."""
    if cpus is None:
        from app.services.video_studio import resources

        cpus = resources.cpu_budget()
    return max(1, min(GPU_ENCODERS, int(cpus) - GPU_SPARE_CPUS))


#: Once half the copies the encoders may hold are in flight, a page copying above :data:`ALONE_RATIO` (a GPU's)
#: draws its next copies at :data:`ALONE_RATIO` and :data:`ALONE_QUALITY`: a smaller copy encodes about twice as
#: fast, so a transition stays copied at the take's rate instead of skipped ("every encoder behind"); the composite
#: scales every copy to the frame, so a run may mix both sizes.
LOWER_AT = 0.5
#: A canvas copied alike this many times running is copied every this many ms only, until it changes or the page is
#: acted on (a pointer pressed, a key, the wheel): a page redrawing a still canvas on every frame costs little.
IDLE_AFTER = 3
IDLE_MS = 250
#: Seconds between two drains of the page's copies, and the most it keeps between two (8 s at 30 fps).
DRAIN_SECONDS = 0.25
KEPT_IN_PAGE = 240
#: A copy is checked against what the browser painted at this scale of its frames (as ``capture_rate`` compares
#: them), both averaged over this many pixels square there - labels and outlines drawn at the copy's pixel ratio
#: differ from the frame's on their edges, measured 2.8% of a 300-node graph unaveraged, 0.3% averaged -, a pixel
#: differing by more than this many grey levels, more than this share of the canvas differing.
COMPARED_AT = 4
SOFTENED = 3
MATCH_LEVELS = 32
MISMATCH_SHARE = 0.02
#: The frame painted once a canvas stood still stands for its state when painted at least this long after its last
#: copy, at most this long after.
REST_AFTER = 0.05
REST_SECONDS = 1.0
#: The frames painted at rest after a run its last copy is compared with, the best kept: the first frame after a
#: run may still be on its way to the screen when the page's thread is held back (#9266).
REST_FRAMES = 6
#: What a page's copying costs (``tap.cost``, :meth:`Tap.stop`'s ``cost``): its thread's ms copying, the copies drawn,
#: the copies encoded (not alike to the one before), the encoders' ms on them, their bytes, and the part of those ms
#: the encoders spent comparing a copy with the one before (its signature, read back from the GPU), and the part of the
#: page's ms spent finding what it draws over its canvases (``over``, a grid of hit tests every ``OVER_MS``) and taking
#: the snapshots of its layers (``createImageBitmap``); the layers a copy holds and those of them the encoder kept
#: from a copy before (``reused``: not drawn on since, not snapshotted again); the ms the page's own animation frame
#: callbacks ran (``drawing``) over the frames it drew a canvas in (``frames``): a page that spends most of a frame
#: drawing paints slowly whatever the recorder costs it; the snapshots taken of WebGL layers (``gl``): a WebGL layer
#: cleared every frame is drawn on every frame, so it is snapshotted on every copy (take b02099f7 kept none); the ms
#: the page's thread was held by long tasks (``blocked``, each over 50 ms, the browser's long task timing) and how
#: many (``long``): a page drawing its frames in 1.3 ms still painted at 12 to 13 fps (take 07a5505d), so what else
#: holds its thread is said; the part of those ms in tasks that drained the copies (``draining``: a drain called
#: within the task, its copies serialised there): the page's thread was held 3.3 s by 42 long tasks while the worker's
#: quota held it back 22.8 s (take aebc40c2), this tells the drain's hand-over from a thread stretched by the quota.
COST_KEYS = (
    "page",
    "copied",
    "encoded",
    "encoding",
    "bytes",
    "signing",
    "scanning",
    "snapshotting",
    "layers",
    "reused",
    "drawing",
    "frames",
    "gl",
    "blocked",
    "long",
    "draining",
    "spent",
    "idled",
    "alike",
)
#: The most layer snapshots an encoder keeps for the copies after, the least used closed first: one canvas of three
#: stacked layers (a graph's main, lasso and relation canvases), 19 MB each at 2880x1620.
KEPT_LAYERS = 3
#: The fewest seconds a take records for the worker's CPU over them to be said (``alone.worker``).
WORKER_MIN_SECONDS = 1.0
#: The most runs a take keeps (``alone.runs``).
MOST_RUNS = 40
#: The most skipped copies a take keeps the instant of (``Tap.skips``); past it they are counted, not placed. Two
#: skips of one canvas this close are one window (``alone.runs[].skipped_windows``), at most :data:`MOST_SKIP_WINDOWS`
#: kept a run; the most steps said outside every run (``alone.skipped_outside_runs``).
MOST_SKIPS = 600
SKIP_GAP = 0.2
MOST_SKIP_WINDOWS = 20
#: The most spans a take keeps of its worker held back by its CPU quota (``alone.held_back``), and the least held
#: back between two drains for the span to be kept: what tells a span the page drew slowly on its own from one its
#: container starved (#10970).
MOST_HELD_BACK = 200
HELD_BACK_LEAST = 0.01
#: The copies are written as ``c000001.jpg``: a canvas's pixels, never a frame of the take.
COPY_PREFIX = "c"
#: A pixel of a canvas's box that differs by at most this many grey levels over the frames painted about a run stands
#: still there (JPEG noise is a few levels); the layers its copies miss there are grown by this many device px, so
#: their anti-aliased edge is the painted one too (:func:`static_layers`, #9590).
STATIC_LEVELS = 12
STATIC_GROW = 2
#: The most of a canvas's box the page may hold still over it that its copies miss: past it the copies do not line up
#: with what the browser painted (a canvas copied at another place or scale), and the run keeps its painted frames.
STATIC_MOST = 0.35
#: Why a run keeps its painted frames.
PAINTED_AT_RATE = "painted at its rate"
UNALIGNED = "its copies do not line up with what the browser painted over its span"
NO_FASTER = "copied no faster than painted"
UNCHECKED = "no frame painted with the canvas at rest about it to check its copies against"

#: The encoder's thread: a copy composed from its layers (each an ``ImageBitmap`` already cropped and sized to the
#: copy, over the colour behind them), shrunk to a small canvas for its signature, dropped when alike to the one
#: before of its canvas (``same``), else encoded as JPEG and answered as a data URL with the ms spent and the ms of
#: them spent on the signature; ``null`` when it cannot be read (a canvas tainted by another origin's picture). The
#: copy is shrunk on a canvas the GPU draws (``shrinking``) and only the 64x36 result is read back (``signing``):
#: drawing a GPU copy straight onto a canvas kept for reading read the whole copy back first, then ``convertToBlob``
#: read it back again (#8457: 218 ms an encoder per copy at 2880x1620 on a GPU worker). The layers are composed here,
#: not on the page's thread: there a copy cost the page 19.2 ms, more than a frame, while its canvas painted at 12 to
#: 16 fps under the recorder (#8457, take 2d77a1e4). They arrive at their canvas's own pixels and are scaled here:
#: snapshots resized on the page still cost it 20.0 ms a copy (take bdaf7b20). Each layer arrives as
#: ``[at, bitmap, mark]``: *at* names the layer of its canvas, *mark* how many times it was drawn on and its crop; a
#: layer not drawn on since this encoder last got it comes without its bitmap and the one kept is drawn again (the
#: page's snapshots cost it 11.5 of its 16.8 ms a copy, take 4b30650d). A layer kept with another mark (copies
#: answered out of turn) or not kept answers ``[id, null, 0, 0, true]``: the page sends every layer again.
_ENCODER_JS = """
const signs = new Map(), kept = new Map();
const KEPT = __KEPT_LAYERS__;
const small = new OffscreenCanvas(64, 36), shrinking = small.getContext('2d');
const sign = new OffscreenCanvas(64, 36), signing = sign.getContext('2d', { willReadFrequently: true });
let picture = null, drawing = null;
const closeAll = (layers) => { for (const layer of layers) { try { layer.close(); } catch (closed) { /* closed */ } } };
const forget = () => { closeAll([...kept.values()].map((k) => k.bitmap)); kept.clear(); };
onmessage = (e) => {
  const [id, key, parts, width, height, behind, quality] = e.data;
  const began = performance.now();
  const layers = [];
  let lost = false;
  for (const [at, bitmap, mark] of parts) {
    if (bitmap) {
      const before = kept.get(at);
      if (before) closeAll([before.bitmap]);
      kept.delete(at);
      kept.set(at, { bitmap, mark });
      layers.push(bitmap);
    } else {
      const before = kept.get(at);
      if (!before || before.mark !== mark) { lost = true; continue; }
      kept.delete(at);
      kept.set(at, before);
      layers.push(before.bitmap);
    }
  }
  while (kept.size > KEPT) {
    const [oldest, k] = kept.entries().next().value;
    closeAll([k.bitmap]);
    kept.delete(oldest);
  }
  if (lost) { postMessage([id, null, 0, 0, true]); return; }
  try {
    if (!picture || picture.width !== width || picture.height !== height) {
      picture = new OffscreenCanvas(width, height);
      drawing = picture.getContext('2d');
    }
    drawing.fillStyle = behind;
    drawing.fillRect(0, 0, width, height);
    for (const layer of layers) drawing.drawImage(layer, 0, 0, width, height);
    shrinking.drawImage(picture, 0, 0, 64, 36);
    signing.drawImage(small, 0, 0);
    const now = signing.getImageData(0, 0, 64, 36).data, before = signs.get(key);
    const signed = performance.now() - began;
    let same = !!before;
    for (let i = 0; same && i < now.length; i++) if (Math.abs(now[i] - before[i]) > 1) same = false;
    if (same) { postMessage([id, 'same']); return; }
    signs.set(key, now);
    picture.convertToBlob({ type: 'image/jpeg', quality }).then(
      (blob) => postMessage([id, new FileReaderSync().readAsDataURL(blob), performance.now() - began, signed]),
      () => postMessage([id, null]),
    );
  } catch (err) {
    forget();
    postMessage([id, null, 0, 0, true]);
  }
};
"""

#: Before any page script: marks each canvas drawn on, copies it once an animation frame's callbacks have all
#: run, at most once a frame of the take's rate, the encoding left to :data:`_ENCODER_JS`. ``drain()`` answers
#: ``{frames: [[t, key, name, clip, over, base64], ...], off}``: *t* in ms since the epoch (the screencast's clock),
#: *key* the canvas's box, *name* how the page names it, *clip* its visible box and *over* the boxes of what the page
#: draws over it in CSS px of the viewport; *off* why nothing is copied. ``stop()`` copies nothing more.
_TAP_JS = """(() => {
  if (window.__xtmAlone || window.top !== window) return;
  const FPS = __FPS__, RATIO = __RATIO__, QUALITY = __QUALITY__, SIDE = __SIDE__, MOST = __MOST__, KEPT = __KEPT__;
  const IDLE_AFTER = __IDLE_AFTER__, IDLE_MS = __IDLE_MS__, ENCODERS = __ENCODERS__, FLIGHT = __FLIGHT__;
  const LOW_RATIO = __LOW_RATIO__, LOW_QUALITY = __LOW_QUALITY__, LOWER = Math.ceil(FLIGHT * __LOWER_AT__);
  // What the page draws over a canvas is found again every OVER_MS, every OVER_MOVING_MS while its copies change: a
  // grid of hit tests cost the page 14.0 of its 43.2 ms a copy on a graph moving (#8457, take b02099f7).
  const OVER_MS = 500, OVER_MOVING_MS = 2000, KEPT_LAYERS = __KEPT_LAYERS__, DRAINS_KEPT = 64;
  // A canvas is copied at most FPS times a second on average, paced by a budget of copies refilled at FPS a second and
  // holding at most BUDGET: a copy at each frame the page draws while one is left. A grid of fixed 1/FPS slots dropped
  // the second of two frames drawn within one slot and had none in the next: a GPU canvas painting at 33 fps was
  // copied 22 times a second (#10961, take e5a71484). Its depth covers the gaps of a page drawing in bursts, held back
  // by its container's quota (#10970); a canvas starts with one copy in hand, and again once not drawn for FRESH_MS.
  const BUDGET = __COPY_BUDGET__;
  const RATE = __COPY_RATE__;
  const FRESH_MS = __COPY_FRESH_MS__;
  const drains = [];
  const clock = () => performance.timeOrigin + performance.now();
  const name = __NAME__;
  const tap = window.__xtmAlone = {
    out: [], off: null, busy: 0, lowered: 0,
    // Each copy skipped, every encoder behind: ``[t, key]``, handed over at the next drain (``Tap.skips``), so a
    // take tells the transition or the step its skips fell in, not only their count (#10970).
    skips: [],
    // What copying costs (``Tap.cost``): the ms the page's thread spent copying, the copies drawn, the copies
    // encoded, their ms on the encoders and their bytes.
    cost: {
      page: 0, copied: 0, encoded: 0, encoding: 0, bytes: 0, signing: 0, scanning: 0, snapshotting: 0,
      layers: 0, reused: 0, drawing: 0, frames: 0, gl: 0,
      blocked: 0, long: 0, draining: 0,
      spent: 0, idled: 0, alike: 0,
    },
    drain() {
      // The task that drains also hands the copies over (their base64 serialised on this thread): its moment is kept.
      drains.push(performance.now());
      if (drains.length > DRAINS_KEPT) drains.shift();
      const frames = tap.out; tap.out = [];
      const skips = tap.skips; tap.skips = [];
      return { frames, off: tap.off, busy: tap.busy, lowered: tap.lowered, cost: tap.cost, skips };
    },
    stop() { tap.off = tap.off || 'stopped'; },
    hold(on) { held = !!on; dirty.clear(); },
  };
  const encoders = [];
  try {
    const source = URL.createObjectURL(new Blob([__ENCODER__], { type: 'text/javascript' }));
    for (let i = 0; i < ENCODERS; i++) encoders.push(new Worker(source));
  } catch (e) {
    if (!encoders.length) {
      tap.off = 'the page refused its encoder: ' + String((e && e.message) || e).slice(0, 160);
      return;
    }
  }
  // The page's thread held by long tasks (over 50 ms each), whoever ran them: the page's, the recorder's; those that
  // drained the copies (a drain called within them) are counted apart (``draining``).
  try {
    new PerformanceObserver((list) => {
      for (const entry of list.getEntries()) {
        tap.cost.blocked += entry.duration; tap.cost.long++;
        const end = entry.startTime + entry.duration;
        if (drains.some((at) => at >= entry.startTime && at <= end)) tap.cost.draining += entry.duration;
      }
    }).observe({ type: 'longtask' });
  } catch (e) { /* no long task timing in this browser */ }
  for (const encoder of encoders) {
    encoder.onerror = (e) => { tap.off = 'its encoder stopped: ' + String(e.message || 'an error').slice(0, 160); };
  }
  const dirty = new Set(), states = new Map(), pending = new Map();
  // What each encoder keeps of each layer (its number and its mark: times drawn on and crop), as last sent to it.
  const sent = encoders.map(() => new Map());
  let serial = 0, flight = 0, acted = 0, held = false, layerSerial = 0;
  for (const type of ['pointerdown', 'keydown', 'wheel']) {
    addEventListener(type, () => { acted = clock(); }, { capture: true, passive: true });
  }
  // The end of an animation frame: every callback of it run, the page's and the one a canvas drawn outside a
  // frame asks for, so the copy reads what the frame drew before it is presented.
  const raf = window.requestAnimationFrame.bind(window), caf = window.cancelAnimationFrame.bind(window);
  let due = new Set(), running = null, woken = false;
  window.requestAnimationFrame = function (callback) {
    let id = 0;
    id = raf((at) => {
      if (!running || !running.has(id)) { running = due; due = new Set(); }
      running.delete(id);
      const began = performance.now();
      try {
        return callback(at);
      } finally {
        tap.cost.drawing += performance.now() - began;
        if (running && running.size === 0) {
          running = null; woken = false; tap.cost.frames++;
          const now = performance.now();
          copyDrawn();
          tap.cost.page += performance.now() - now;
        }
      }
    });
    due.add(id);
    return id;
  };
  window.cancelAnimationFrame = function (id) {
    due.delete(id);
    if (running) running.delete(id);
    return caf(id);
  };
  const wake = () => {
    if (!woken && !running && !due.size) { woken = true; window.requestAnimationFrame(() => {}); }
  };
  const mark = (canvas) => {
    if (!(canvas instanceof HTMLCanvasElement)) return;
    // Every draw counted, held or not: a layer drawn on since an encoder got it is snapshotted again.
    canvas.__xtmDrawn = (canvas.__xtmDrawn || 0) + 1;
    canvas.__xtmInk = true;
    if (!tap.off && !held) { dirty.add(canvas); wake(); }
  };
  // A 2D layer cleared while it holds nothing drawn since it was last cleared whole stays as it was: not marked, so
  // the encoder draws the snapshot it keeps (a graph clears its blank lasso and relation layers every frame: take
  // 840ddc8f kept 0% of its layers and spent 13.3 of its 19.2 ms a copy snapshotting them). A clear covering the
  // whole layer, through the transform it is drawn with, leaves it blank.
  const covers = (g, args) => {
    const canvas = g.canvas, [x, y, w, h] = args.map(Number);
    const t = g.getTransform ? g.getTransform() : null;
    if (!t || t.b || t.c || ![x, y, w, h].every(Number.isFinite)) return false;
    const xs = [t.a * x + t.e, t.a * (x + w) + t.e], ys = [t.d * y + t.f, t.d * (y + h) + t.f];
    return Math.min(...xs) <= 0 && Math.min(...ys) <= 0
      && Math.max(...xs) >= canvas.width && Math.max(...ys) >= canvas.height;
  };
  const cleared = (g, whole) => {
    const canvas = g.canvas;
    if (!(canvas instanceof HTMLCanvasElement) || !canvas.__xtmInk) return;
    mark(canvas);
    if (whole) canvas.__xtmInk = false;
  };
  const hook = (proto, names) => {
    if (!proto) return;
    const twoD = window.CanvasRenderingContext2D && proto === CanvasRenderingContext2D.prototype;
    for (const n of names) {
      const f = proto[n];
      if (typeof f !== 'function' || f.__xtmAlone) continue;
      const marking = twoD && (n === 'clearRect' || n === 'reset')
        ? function (...args) { cleared(this, n === 'reset' || covers(this, args)); return f.apply(this, args); }
        : function (...args) { mark(this.canvas); return f.apply(this, args); };
      marking.__xtmAlone = true;
      proto[n] = marking;
    }
  };
  hook(window.CanvasRenderingContext2D && CanvasRenderingContext2D.prototype,
    ['fill', 'stroke', 'fillRect', 'strokeRect', 'clearRect', 'fillText', 'strokeText', 'drawImage', 'putImageData', 'reset']);
  for (const kind of [window.WebGLRenderingContext, window.WebGL2RenderingContext]) {
    hook(kind && kind.prototype,
      ['clear', 'drawArrays', 'drawElements', 'drawArraysInstanced', 'drawElementsInstanced', 'drawRangeElements']);
  }
  const shown = (el) => !el.checkVisibility || el.checkVisibility({ checkOpacity: true, checkVisibilityCSS: true });
  const keyOf = (r) => [r.left, r.top, r.width, r.height].map(Math.round).join(',');
  const round = (v) => Math.round(v * 10) / 10;
  // What the canvas shows: its box within the viewport and within each ancestor that clips what overflows it.
  const clipped = (el, r) => {
    let left = Math.max(0, r.left), top = Math.max(0, r.top);
    let right = Math.min(innerWidth, r.right), bottom = Math.min(innerHeight, r.bottom);
    for (let node = el.parentElement; node && node !== document.body && node !== document.documentElement; node = node.parentElement) {
      const cs = getComputedStyle(node);
      if (cs.overflowX === 'visible' && cs.overflowY === 'visible') continue;
      const b = node.getBoundingClientRect();
      left = Math.max(left, b.left); top = Math.max(top, b.top); right = Math.min(right, b.right); bottom = Math.min(bottom, b.bottom);
    }
    return [left, top, right - left, bottom - top];
  };
  const behind = (el) => {
    for (let node = el.parentElement; node; node = node.parentElement) {
      const c = getComputedStyle(node).backgroundColor;
      if (c && c !== 'transparent' && !/^rgba\\(.*,\\s*0\\)$/.test(c)) return c;
    }
    return '#ffffff';
  };
  // What the page draws over the canvas: an element above it on a grid across what it shows, shown, smaller than half
  // of it (a layer as large as the canvas is part of its drawing).
  const over = (layers, clip) => {
    const [left, top, width, height] = clip, seen = new Set(), found = [];
    for (let i = 0; i < 24; i++) {
      for (let j = 0; j < 14; j++) {
        for (const el of document.elementsFromPoint(left + (i + 0.5) * width / 24, top + (j + 0.5) * height / 14)) {
          if (layers.includes(el)) break;
          if (seen.has(el) || layers.some((layer) => el.contains(layer))) continue;
          seen.add(el);
          if (typeof el.id === 'string' && el.id.startsWith('__xtm')) continue;
          const b = el.getBoundingClientRect(), cs = getComputedStyle(el);
          if (cs.visibility === 'hidden' || Number(cs.opacity) === 0) continue;
          if (b.width * b.height > 0.5 * width * height) continue;
          if (found.length < 40) found.push([b.left, b.top, b.width, b.height].map(round));
        }
      }
    }
    return found;
  };
  const copyDrawn = () => {
    if (!dirty.size || tap.off || held) return;
    const t = clock();
    const surfaces = new Map();
    for (const canvas of dirty) {
      if (!canvas.isConnected || !shown(canvas)) continue;
      const r = canvas.getBoundingClientRect();
      if (r.width < SIDE || r.height < SIDE || getComputedStyle(canvas).transform !== 'none') continue;
      const key = keyOf(r);
      if (!surfaces.has(key)) surfaces.set(key, { key, r, drawn: [] });
      surfaces.get(key).drawn.push(canvas);
    }
    dirty.clear();
    const canvases = [...document.querySelectorAll('canvas')];
    const largest = [...surfaces.values()].sort((a, b) => b.r.width * b.r.height - a.r.width * a.r.height).slice(0, MOST);
    for (const surface of largest) {
      const { key, r } = surface;
      const st = states.get(key) || { budget: 1, filled: t, idle: 0, last: 0, overAt: -Infinity, over: [], copy: null, frames: 0 };
      states.set(key, st);
      // The frames the page drew this canvas in, copied or not (a layer drawn on since this was last counted): a
      // copy carries the count, so a run says the rate its canvas was drawn at beside the rate it was copied at.
      // A wheel zoom painted at 34 to 42 fps by the screencast was copied at 22 (#10970, take 185d7597):
      // this tells a copy loop falling short from a page drawing its canvas no faster.
      let fresh = false;
      for (const el of surface.drawn) {
        if (el.__xtmFramed !== el.__xtmDrawn) { el.__xtmFramed = el.__xtmDrawn; fresh = true; }
      }
      if (fresh) st.frames++;
      st.budget = t - st.filled > FRESH_MS ? 1 : Math.min(BUDGET, st.budget + (t - st.filled) * RATE / 1000);
      st.filled = t;
      const spent = st.budget < 1;
      const layers = canvases.filter((el) => shown(el) && keyOf(el.getBoundingClientRect()) === key);
      // A 2D canvas keeps what it drew: one not copied now is copied at the next frame. A WebGL one's buffer is
      // presented: a frame not copied is gone.
      const keeps = layers.every((el) => !el.__xtmContext || el.__xtmContext === '2d');
      const idle = st.idle >= IDLE_AFTER && t - st.last < IDLE_MS && acted <= st.last;
      // A copy due while every encoder is FLIGHT behind is skipped: counted, the take says it (``capture_words``).
      if (!spent && !idle && flight >= FLIGHT) {
        tap.busy++;
        tap.skips.push([t, key]);
        if (tap.skips.length > KEPT) tap.skips.shift();
      }
      // Why a canvas drawn was not copied (#10970: copied at 25 fps of 35 drawn, no copy skipped as busy): its budget
      // spent (over the take's rate) or its copies alike (still); said on the capture line beside the copies drawn.
      if (fresh && spent) tap.cost.spent++;
      else if (fresh && idle) tap.cost.idled++;
      if (spent || idle || flight >= FLIGHT) {
        if (keeps) { for (const el of surface.drawn) dirty.add(el); wake(); }
        continue;
      }
      const clip = clipped(layers[layers.length - 1] || surface.drawn[0], r);
      if (clip[2] < SIDE || clip[3] < SIDE) continue;
      st.budget -= 1; st.last = t;
      if (st.idle >= IDLE_AFTER || t - st.overAt > (st.idle ? OVER_MS : OVER_MOVING_MS)) {
        const scanned = performance.now();
        st.over = over(layers, clip); st.overAt = t;
        tap.cost.scanning += performance.now() - scanned;
      }
      // The encoders half behind: this copy is drawn smaller and encoded at the lower quality, to keep up.
      const lower = RATIO > LOW_RATIO && flight >= LOWER;
      if (lower) tap.lowered++;
      const ratio = Math.min(devicePixelRatio, lower ? LOW_RATIO : RATIO);
      const w = Math.max(1, Math.round(clip[2] * ratio)), h = Math.max(1, Math.round(clip[3] * ratio));
      // The page's thread only snapshots each layer, cropped at its own pixels (a snapshot is taken when asked, so a
      // WebGL buffer is read before it is presented); the encoder scales and composes them over the colour behind.
      // No resize is asked of the snapshot: resizing a GPU canvas's snapshot scales it in software, reading the
      // whole buffer back on the page's thread first.
      if (!st.behind || st.idle >= IDLE_AFTER) st.behind = behind(layers[0] || surface.drawn[0]);
      try {
        const id = ++serial, at = id % encoders.length, keeps = sent[at];
        const parts = [], snapshots = [], snapped = performance.now();
        for (const layer of layers) {
          if (!layer.width || !layer.height) continue;
          const sx = layer.width / r.width, sy = layer.height / r.height;
          const box = [Math.round((clip[0] - r.left) * sx), Math.round((clip[1] - r.top) * sy),
            Math.max(1, Math.round(clip[2] * sx)), Math.max(1, Math.round(clip[3] * sy))];
          if (!layer.__xtmLayer) layer.__xtmLayer = ++layerSerial;
          const layerMark = (layer.__xtmDrawn || 0) + ':' + layer.width + 'x' + layer.height + ':' + box.join(',');
          tap.cost.layers++;
          // A layer this encoder already holds as it is now: not snapshotted again, the encoder draws the one it kept.
          // Kept in the order the encoder keeps them (least used first), as many: what it closes, the page forgets.
          const holds = keeps.get(layer.__xtmLayer) === layerMark;
          keeps.delete(layer.__xtmLayer);
          keeps.set(layer.__xtmLayer, layerMark);
          while (keeps.size > KEPT_LAYERS) keeps.delete(keeps.keys().next().value);
          if (holds) {
            tap.cost.reused++;
            parts.push([layer.__xtmLayer, null, layerMark]);
            continue;
          }
          parts.push([layer.__xtmLayer, null, layerMark]);
          if (layer.__xtmContext && layer.__xtmContext !== '2d') tap.cost.gl++;
          snapshots.push([parts.length - 1, createImageBitmap(layer, ...box)]);
        }
        tap.cost.snapshotting += performance.now() - snapped;
        const quality = lower ? Math.min(QUALITY, LOW_QUALITY) : QUALITY, fill = st.behind;
        pending.set(id, [t, key, name(layers[layers.length - 1] || surface.drawn[0]), clip.map(round), st.over, st.frames]);
        flight++; tap.cost.copied++;
        Promise.all(snapshots.map(([, snapshot]) => snapshot)).then(
          (bitmaps) => {
            snapshots.forEach(([index], i) => { parts[index][1] = bitmaps[i]; });
            encoders[at].postMessage([id, key, parts, w, h, fill, quality], bitmaps);
          },
          () => answered({ data: [id, null, 0, 0, true] }),
        );
      } catch (e) { /* a canvas that cannot be read is not copied */ }
    }
  };
  const answered = (e) => {
    const [id, data, spent, signed, resend] = e.data;
    // The encoder lost a layer it was to draw again (or a snapshot was refused): every layer is sent to it anew.
    if (resend) sent[id % encoders.length].clear();
    const meta = pending.get(id);
    pending.delete(id);
    flight = Math.max(0, flight - 1);
    if (!meta) return;
    const st = states.get(meta[1]);
    if (data === 'same') { tap.cost.alike++; if (st) st.idle++; return; }
    if (st) st.idle = 0;
    if (typeof data !== 'string' || !data.startsWith('data:image/jpeg;base64,')) return;
    tap.cost.encoded++; tap.cost.encoding += Number(spent) || 0; tap.cost.signing += Number(signed) || 0;
    tap.cost.bytes += Math.round((data.length - 23) * 3 / 4);
    tap.out.push([...meta, data.slice(23)]);
    if (tap.out.length > KEPT) tap.out.shift();
    // The page paints on a moment once its canvas changed: the screencast hands over what it left.
    if (window.__xtmMotion && window.__xtmMotion.paint) window.__xtmMotion.paint(0);
  };
  for (const encoder of encoders) encoder.onmessage = answered;
})()"""

_DRAIN_JS = "() => window.__xtmAlone ? window.__xtmAlone.drain() : null"
_STOP_JS = "() => { if (window.__xtmAlone) window.__xtmAlone.stop(); }"
_HOLD_JS = "(on) => { if (window.__xtmAlone) window.__xtmAlone.hold(on); }"


@contextlib.asynccontextmanager
async def held(page: Any):
    """No canvas copied while the block runs: a step's scroll, whose copies no run keeps (:func:`alone_runs`) -
    ``scroll_pacing`` draws it again -, its page's frames left to it. A page that copies nothing holds nothing."""
    await _hold(page, True)
    try:
        yield
    finally:
        await _hold(page, False)


async def _hold(page: Any, on: bool) -> None:
    with contextlib.suppress(Exception):  # navigating: the next document copies from its start
        await page.evaluate(_HOLD_JS, on)


def tap_script(
    fps: int,
    name_js: str,
    *,
    ratio: float = ALONE_RATIO,
    quality: float = ALONE_QUALITY,
    encoders: int = ENCODERS,
) -> str:
    """The script every document of a take runs first (:data:`_TAP_JS`): its canvases copied at most *fps* times a
    second at pixel ratio *ratio* (at most the page's own) and JPEG *quality* on *encoders* threads, named by
    *name_js* (``capture._NAME_JS``, as the take's log names elements)."""
    from app.services.video_studio.page_settle import MIN_CANVAS_SIDE, MOST_CANVASES

    values = {
        "__FPS__": str(int(fps)),
        "__RATIO__": repr(float(ratio)),
        "__QUALITY__": repr(float(quality)),
        "__SIDE__": str(MIN_CANVAS_SIDE),
        "__MOST__": str(MOST_CANVASES),
        "__KEPT__": str(KEPT_IN_PAGE),
        "__IDLE_AFTER__": str(IDLE_AFTER),
        "__IDLE_MS__": str(IDLE_MS),
        "__ENCODERS__": str(max(1, int(encoders))),
        "__FLIGHT__": str(flight_depth(encoders)),
        "__LOW_RATIO__": repr(float(min(ratio, ALONE_RATIO))),
        "__LOW_QUALITY__": repr(float(min(quality, ALONE_QUALITY))),
        "__LOWER_AT__": repr(float(LOWER_AT)),
        "__ENCODER__": json.dumps(_ENCODER_JS),
        "__KEPT_LAYERS__": str(KEPT_LAYERS),
        "__COPY_BUDGET__": repr(float(COPY_BUDGET)),
        "__COPY_RATE__": repr(copy_rate(fps, encoders)),
        "__COPY_FRESH_MS__": str(int(COPY_FRESH_MS)),
        "__NAME__": name_js,
    }
    script = _TAP_JS
    for marker, value in values.items():
        script = script.replace(marker, value)
    return script


@dataclass(frozen=True)
class AloneFrame:
    """One copy of a canvas: its instant (seconds of the take), the box it was copied in (``key``), how the page
    names it, the part of it shown and the boxes of what the page draws over it (CSS px of the viewport), its file."""

    t: float
    surface: str
    canvas: str
    clip: tuple[float, float, float, float]
    over: tuple[tuple[float, float, float, float], ...]
    file: str
    #: The frames the page had drawn its canvas in when it copied it (``tap_script``'s count), ``None`` for a copy
    #: kept before it was counted: two copies of a run tell the rate its canvas was drawn at between them.
    drawn: int | None = None


def _write_copies(workdir: str, copies: list[tuple[str, str]]) -> None:
    for name, data in copies:
        (Path(workdir) / name).write_bytes(base64.b64decode(data))


def _box(raw: Any) -> tuple[float, float, float, float] | None:
    if isinstance(raw, list | tuple) and len(raw) == 4:
        with contextlib.suppress(TypeError, ValueError):
            x, y, w, h = (float(v) for v in raw)
            return x, y, w, h
    return None


#: The clock a tap times the worker's CPU on (a test replaces it, never the event loop's).
_clock = time.monotonic


def _cpu_stat() -> Any:
    """The container's CPU now (``resources.cpu_stat``), ``None`` where it cannot be read."""
    from app.services.video_studio import resources

    try:
        return resources.cpu_stat()
    except Exception:  # an unreadable cgroup says nothing
        return None


class Tap:
    """What a take's page copied of its canvases (:func:`tap_script`), drained every :data:`DRAIN_SECONDS`
    while it records: each copy written into *workdir* (``c000001.jpg``), kept as an :class:`AloneFrame` timed from
    *origin* (seconds since the epoch, the screencast's start), *max_copies* at most."""

    def __init__(
        self,
        page: Any,
        workdir: str,
        *,
        origin: float,
        max_copies: int,
        ratio: float = ALONE_RATIO,
        quality: float = ALONE_QUALITY,
    ) -> None:
        self.page, self.workdir, self.origin, self.max_copies = page, workdir, origin, max_copies
        #: The pixel ratio and JPEG quality its page copies at (:func:`tap_script`), as the take keeps them.
        self.ratio, self.quality = ratio, quality
        self.frames: list[AloneFrame] = []
        #: Why the page copied nothing (its encoder refused), as it said.
        self.off: str | None = None
        #: The copies due the page skipped, every encoder behind (:data:`IN_FLIGHT`).
        self.busy = 0
        #: Each of them as ``(seconds of the take, canvas box)``, :data:`MOST_SKIPS` at most: what
        #: :func:`composite` attributes to a transition or a step (``skipped``, ``skipped_outside_runs``).
        self.skips: list[tuple[float, str]] = []
        #: The copies the page drew at the lower ratio, its encoders half behind (:data:`LOWER_AT`).
        self.lowered = 0
        #: What copying cost (:data:`COST_KEYS`): the page's thread ms, the copies drawn and encoded, the encoders' ms
        #: and the copies' bytes, summed over every document of the take.
        self.cost: dict[str, float] = dict.fromkeys(COST_KEYS, 0.0)
        self._cost_done: dict[str, float] = dict.fromkeys(COST_KEYS, 0.0)
        self._cost_now: dict[str, float] = dict.fromkeys(COST_KEYS, 0.0)
        self._stopped = asyncio.Event()
        self._task: asyncio.Task | None = None
        #: The container's CPU when it started copying (``resources.cpu_stat``) and the monotonic clock then: what
        #: :meth:`stop` says the worker's CPUs were busy with while the take recorded (``worker``).
        self._cpu_began: tuple[float, Any] | None = None
        #: ``[start, end, seconds]``: each stretch between two drains its worker's quota held it back over, in seconds
        #: of the take; ``None`` where the container's CPU cannot be read (#10970).
        self.held_back: list[list[float]] | None = None
        self._held_last: tuple[float, float] | None = None

    def start(self) -> None:
        self._cpu_began = (_clock(), _cpu_stat())
        if self._cpu_began[1] is not None:
            self.held_back = []
            self._held_last = (time.time() - self.origin, float(self._cpu_began[1].throttled_seconds))
        self._task = asyncio.create_task(self._run())

    def sample_held_back(self) -> None:
        """The quota's hold-back since the last sample, kept as a span of the take where it held the worker back."""
        if self._held_last is None or self.held_back is None:
            return
        stat = _cpu_stat()
        if stat is None:
            return
        now, throttled = time.time() - self.origin, float(stat.throttled_seconds)
        before_t, before = self._held_last
        self._held_last = (now, throttled)
        held = throttled - before
        if held < HELD_BACK_LEAST or now <= before_t:
            return
        last = self.held_back[-1] if self.held_back else None
        if last is not None and before_t - last[1] <= 1e-3:
            last[1], last[2] = round(now, 3), round(last[2] + held, 3)
        elif len(self.held_back) < MOST_HELD_BACK:
            self.held_back.append([round(max(0.0, before_t), 3), round(now, 3), round(held, 3)])

    async def stop(self) -> dict:
        """Stop draining, once the last copies are in, and the page copying; what the take keeps of it (``alone``)."""
        self._stopped.set()
        if self._task is not None:
            await self._task
        await self.drain()
        with contextlib.suppress(Exception):  # the page may be gone
            await self.page.evaluate(_STOP_JS)
        kept: dict = {"ratio": self.ratio, "quality": self.quality, "copies": len(self.frames)}
        if self.off and self.off != "stopped":
            kept["off"] = self.off
        # A page that navigated restarts its count: the skips handed over say the copies of every document.
        self.busy = max(self.busy, len(self.skips))
        if self.busy:
            kept["busy"] = self.busy
        if self.skips:
            kept["skips"] = [[t, key] for t, key in self.skips]
        if self.lowered:
            kept["lowered"] = self.lowered
        if self.cost.get("copied"):
            kept["cost"] = {k: round(v, 1) for k, v in self.cost.items()}
        if worker := self._worker_cpu():
            kept["worker"] = worker
        self.sample_held_back()
        if self.held_back is not None:
            kept["held_back"] = self.held_back
        return kept

    def _worker_cpu(self) -> dict | None:
        """``{cpus, of, throttled}``: the CPUs the worker's container kept busy on average while the take recorded
        (the page, its encoders, the screencast's encode and anything beside them), of the CPUs it may use, and the
        seconds its quota held it back; ``None`` where the container's CPU cannot be read. A take copying on a GPU
        worker said copies cost its encoders 98 ms each, about 19 a second, while its canvas painted at 13 to 16 fps
        (#8457, take 32482533): this tells a worker short of CPU from a page that paints slowly on its own."""
        if self._cpu_began is None:
            return None
        began, before = self._cpu_began
        after, seconds = _cpu_stat(), _clock() - began
        if before is None or after is None or seconds < WORKER_MIN_SECONDS:
            return None
        if before.usage_seconds is None or after.usage_seconds is None:
            return None
        from app.services.video_studio import resources

        return {
            "cpus": round(max(0.0, after.usage_seconds - before.usage_seconds) / seconds, 1),
            "of": resources.cpu_budget(),
            "throttled": round(max(0.0, after.throttled_seconds - before.throttled_seconds), 1),
        }

    def _count_cost(self, raw: Any) -> None:
        """The page's running cost (``tap.cost``) folded in: a count lower than the last read is a new document."""
        if not isinstance(raw, dict):
            return
        now = {k: float(raw[k]) for k in COST_KEYS if isinstance(raw.get(k), int | float) and raw[k] >= 0}
        if now.get("copied", 0.0) < self._cost_now.get("copied", 0.0):
            self._cost_done = {k: self._cost_done[k] + self._cost_now[k] for k in COST_KEYS}
        self._cost_now = {k: now.get(k, 0.0) for k in COST_KEYS}
        self.cost = {k: self._cost_done[k] + self._cost_now[k] for k in COST_KEYS}

    async def _run(self) -> None:
        while not self._stopped.is_set():
            await self.drain()
            self.sample_held_back()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stopped.wait(), DRAIN_SECONDS)

    async def drain(self) -> None:
        """The copies the page made since the last drain, written and kept; a page navigating keeps its own."""
        try:
            raw = await self.page.evaluate(_DRAIN_JS)
        except Exception as exc:  # navigating: what the page copied goes with it
            logger.debug("Canvas copies not drained: %s", format_exception_message(exc))
            return
        if not isinstance(raw, dict):
            return
        if raw.get("off") and self.off is None:
            self.off = str(raw["off"])[:200]
        if isinstance(raw.get("busy"), int | float):
            self.busy = max(self.busy, int(raw["busy"]))
        for skip in raw.get("skips") or []:
            if len(self.skips) >= MOST_SKIPS:
                break
            if isinstance(skip, list) and len(skip) == 2 and isinstance(skip[0], int | float):
                at = float(skip[0]) / 1000 - self.origin
                if at >= 0:
                    self.skips.append((round(at, 4), str(skip[1])))
        if isinstance(raw.get("lowered"), int | float):
            self.lowered = max(self.lowered, int(raw["lowered"]))
        self._count_cost(raw.get("cost"))
        copied: list[AloneFrame] = []
        written: list[tuple[str, str]] = []
        for entry in raw.get("frames") or []:
            # ``[t, key, name, clip, over, frames, data]``; a page whose tap predates the frames count sends six.
            if (
                len(self.frames) + len(copied) >= self.max_copies
                or not isinstance(entry, list)
                or len(entry) not in (6, 7)
            ):
                continue
            at, key, canvas, clip, over, *rest = entry
            frames_drawn, data = (rest[0], rest[1]) if len(rest) == 2 else (None, rest[0])
            box = _box(clip)
            if box is None or not isinstance(data, str):
                continue
            t = float(at) / 1000 - self.origin
            if t < 0:
                continue
            name = f"{COPY_PREFIX}{len(self.frames) + len(copied):06d}.jpg"
            boxes = tuple(b for b in (_box(raw_box) for raw_box in over or []) if b is not None)
            drawn = int(frames_drawn) if isinstance(frames_drawn, int | float) and frames_drawn >= 0 else None
            copied.append(AloneFrame(round(t, 4), str(key), str(canvas or "canvas")[:120], box, boxes, name, drawn))
            written.append((name, data))
        if not written:
            return
        try:
            await asyncio.to_thread(_write_copies, self.workdir, written)
        except Exception as exc:  # copies not written are not kept: the take keeps its painted frames there
            logger.warning("Canvas copies not written: %s", format_exception_message(exc))
            return
        self.frames.extend(copied)


# ── Once recorded ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class AloneRun:
    """A canvas's copies one after the other (:func:`alone_runs`): one transition of it, copied alone."""

    copies: tuple[AloneFrame, ...]

    @property
    def start(self) -> float:
        return self.copies[0].t

    @property
    def end(self) -> float:
        return self.copies[-1].t

    @property
    def fps(self) -> float:
        return (len(self.copies) - 1) / max(self.end - self.start, 1e-6)

    @property
    def drawn_fps(self) -> float | None:
        """The frames a second the page drew its canvas in over the run, copied or not; ``None`` where its copies
        predate the count. A run copied below it lost frames to the copy loop; one copied at it, to the page."""
        first, last = self.copies[0].drawn, self.copies[-1].drawn
        if first is None or last is None or last < first:
            return None
        return (last - first) / max(self.end - self.start, 1e-6)

    @property
    def clip(self) -> tuple[float, float, float, float]:
        return self.copies[0].clip

    def latest(self, t: float) -> AloneFrame:
        """Its copy at *t*: the last one copied by then."""
        index = bisect.bisect_right([copy.t for copy in self.copies], t + 1e-6) - 1
        return self.copies[max(index, 0)]


def _same_clip(a: tuple[float, ...], b: tuple[float, ...]) -> bool:
    return all(abs(x - y) <= 1.0 for x, y in zip(a, b, strict=True))


def canvas_wheel(scroll: Any) -> bool:
    """Whether a step's logged scroll is a wheel over a canvas (``capture._scroll``: its ``surface`` a canvas)."""
    return (
        isinstance(scroll, dict)
        and isinstance(scroll.get("surface"), dict)
        and scroll["surface"].get("kind") == "canvas"
    )


def wheel_free(events: list[dict]) -> list[dict]:
    """*events* without their wheels over a canvas: the scrolls whose copies no run keeps."""
    return [
        {**event, "scrolls": [s for s in event.get("scrolls") or [] if not canvas_wheel(s)]}
        if event.get("scrolls")
        else event
        for event in events
    ]


def alone_runs(alone: Sequence[AloneFrame], events: list[dict]) -> list[AloneRun]:
    """The runs of each canvas's copies: one after the other, none further apart than ``RUN_GAP``, in one box,
    ``CANVAS_FRAMES`` over ``CANVAS_SECONDS`` at least; a copy made while a step scrolled the window or a container
    belongs to none. A wheel over a canvas (its scroll logged with a canvas ``surface``) zooms that canvas: its copies
    make a run, the zoom captured alone at the rate the page drew it."""
    scrolls = paced_spans(wheel_free(events), transitions=False)
    by_surface: dict[str, list[AloneFrame]] = {}
    for copy in sorted(alone, key=lambda c: c.t):
        by_surface.setdefault(copy.surface, []).append(copy)
    found: list[AloneRun] = []

    def close(copies: list[AloneFrame]) -> None:
        if len(copies) >= CANVAS_FRAMES and copies[-1].t - copies[0].t >= CANVAS_SECONDS:
            found.append(AloneRun(tuple(copies)))

    for copies in by_surface.values():
        current: list[AloneFrame] = []
        for copy in copies:
            if any(a <= copy.t <= b for a, b in scrolls):
                close(current)
                current = []
                continue
            if current and (copy.t - current[-1].t > RUN_GAP or not _same_clip(copy.clip, current[-1].clip)):
                close(current)
                current = []
            current.append(copy)
        close(current)
    return sorted(found, key=lambda run: run.start)


def _span(start: float, end: float, scale: float, origin: int, limit: int) -> tuple[int, int]:
    return min(max(round(start * scale) - origin, 0), limit), min(max(round(end * scale) - origin, 0), limit)


def _grey(path: Path, at: int) -> Any:
    import numpy as np
    from PIL import Image

    with Image.open(path) as image:
        size = (max(1, image.width // at), max(1, image.height // at))
        image.draft("L", size)
        grey = image.convert("L")
        if grey.size != size:
            grey = grey.resize(size)
        return np.asarray(grey, dtype=np.int16)


def _softened(grey: Any) -> Any:
    """*grey* averaged over :data:`SOFTENED` px about each pixel: text and hairlines drawn at another pixel ratio are
    alike there, a shape the copy misses is not."""
    import numpy as np

    pad = SOFTENED // 2
    padded = np.pad(grey.astype(np.float32), pad, mode="edge")
    rows, cols = grey.shape
    return sum(padded[i : i + rows, j : j + cols] for i in range(SOFTENED) for j in range(SOFTENED)) / (
        SOFTENED * SOFTENED
    )


def mismatch(copy: AloneFrame, frame: str, *, workdir: str, ratio: float) -> float | None:
    """The share of *copy*'s canvas that differs from the frame painted *frame* (a file of *workdir*, *ratio* device
    px a CSS px) by more than :data:`MATCH_LEVELS` grey levels, both read at :data:`COMPARED_AT` of its size and
    softened (:func:`_softened`: the copy is drawn at :data:`ALONE_RATIO`), outside what the page draws over it;
    ``None`` where too little of it shows to tell."""
    import numpy as np
    from PIL import Image

    painted = _grey(Path(workdir) / frame, COMPARED_AT)
    scale = ratio / COMPARED_AT
    x, y, w, h = copy.clip
    left, right = _span(x, x + w, scale, 0, painted.shape[1])
    top, bottom = _span(y, y + h, scale, 0, painted.shape[0])
    if right - left < 4 or bottom - top < 4:
        return None
    crop = painted[top:bottom, left:right]
    with Image.open(Path(workdir) / copy.file) as image:
        mine = np.asarray(
            image.convert("L").resize((right - left, bottom - top), Image.Resampling.BILINEAR), dtype=np.int16
        )
    kept = np.ones(crop.shape, dtype=bool)
    # One pixel of the compared scale around what is drawn over it: its edge is read on both.
    grow = 1 / scale
    for ox, oy, ow, oh in copy.over:
        x0, x1 = _span(ox - grow, ox + ow + grow, scale, left, crop.shape[1])
        y0, y1 = _span(oy - grow, oy + oh + grow, scale, top, crop.shape[0])
        kept[y0:y1, x0:x1] = False
    counted = int(kept.sum())
    if not counted:
        return None
    return float(np.count_nonzero((np.abs(_softened(crop) - _softened(mine)) > MATCH_LEVELS) & kept)) / counted


def check(
    run: AloneRun,
    copies: Sequence[AloneFrame],
    frames: Sequence[tuple[float, str]],
    *,
    workdir: str,
    ratio: float,
    checked: list[dict] | None = None,
) -> str | None:
    """Why *run*'s copies are not composited, checked against what the browser painted with its canvas at rest about
    it (*frames*, *copies* all of its canvas's): its last copy against the first frame painted at least
    :data:`REST_AFTER` after it, within :data:`REST_SECONDS` and before its canvas changed again; the copy it stood
    at before against the last frame painted after that copy, within :data:`REST_SECONDS` of it, and before the run.
    ``None``: they match.

    *checked*, when given, receives one entry per pair compared (``side`` after | before, the copy's and the frame's
    instants, their ``lag`` in seconds and the ``share`` that differs, ``None`` where too little showed): a take's
    run then says which side refused it and how far apart the two were, what a mismatch is read from (#9266: 14% of
    the canvas on a layout click, the side and the lag never said)."""
    times = [t for t, _name in frames]
    pairs: list[tuple[str, AloneFrame, int]] = []
    after = bisect.bisect_left(times, run.end + REST_AFTER)
    later = next((copy.t for copy in copies if copy.t > run.end + 1e-6), math.inf)
    # Every frame painted at rest after the run, within REST_SECONDS and before its canvas changed again, may stand
    # for it: the best of them is compared. The first one alone may be handed over before the page presented the
    # canvas's last state, the page's thread held back by the worker's quota (#9266, a layout click refused at 14%
    # with the worker held back 4.1 s): one frame at rest that matches shows the copies draw what the browser paints.
    rest = [
        index
        for index in range(after, min(len(times), after + REST_FRAMES))
        if times[index] - run.end <= REST_SECONDS and times[index] < later
    ]
    if not rest and after < len(times) and times[after] < later:
        # A zoom that settled paints nothing more: the screencast hands over its next frame only once the page changes
        # elsewhere, past REST_SECONDS (#10943, a V100S take refused "no frame painted with the canvas at
        # rest"). Its canvas copied nothing since, so that frame still shows it at rest.
        rest = [after]
    if rest:
        pairs.append(("after", run.copies[-1], rest[0]))
    before = [copy for copy in copies if copy.t < run.start - 1e-6]
    if before and _same_clip(before[-1].clip, run.clip):
        index = bisect.bisect_right(times, run.start) - 1
        # Within REST_SECONDS of the copy, as on the after side: a frame painted later may show what the page drew
        # since without a copy (a wheel's redraw, #9744: compared 7.84 s after it and refused at 6%), no refusal of
        # the copies; the after side then decides alone.
        if index >= 0 and before[-1].t + REST_AFTER <= times[index] <= before[-1].t + REST_SECONDS:
            pairs.append(("before", before[-1], index))
    if not pairs:
        return UNCHECKED
    why: str | None = None
    for side, copy, index in pairs:
        share = mismatch(copy, frames[index][1], workdir=workdir, ratio=ratio)
        if side == "after" and share is not None and share > MISMATCH_SHARE:
            for other in rest[1:]:
                found = mismatch(copy, frames[other][1], workdir=workdir, ratio=ratio)
                if found is not None and found < share:
                    share, index = found, other
                if share <= MISMATCH_SHARE:
                    break
        lag = times[index] - copy.t
        if checked is not None:
            checked.append(
                {
                    "side": side,
                    "copy": round(copy.t, 3),
                    "frame": round(times[index], 3),
                    "lag": round(lag, 3),
                    "share": None if share is None else round(share, 4),
                }
            )
        if why is not None:
            continue
        if share is None:
            why = UNCHECKED
        elif share > MISMATCH_SHARE:
            said = (
                "its last copy against the frame painted"
                if side == "after"
                else "the copy it stood at before against the frame painted"
            )
            why = f"its copies differ from what the browser painted ({share:.0%} of the canvas, {said} {lag:.2f} s after it)"
    return why


def _rgb(path: Path) -> Any:
    import numpy as np
    from PIL import Image

    with Image.open(path) as image:
        return np.array(image.convert("RGB"))


def _region(clip: tuple[float, float, float, float], ratio: float, shape: tuple[int, ...]) -> tuple[int, int, int, int]:
    """*clip* (CSS px) as ``(left, right, top, bottom)`` device px of a frame of *shape*."""
    x, y, w, h = clip
    left, right = _span(x, x + w, ratio, 0, shape[1])
    top, bottom = _span(y, y + h, ratio, 0, shape[0])
    return left, right, top, bottom


def _scaled_copy(copy: AloneFrame, size: tuple[int, int], *, workdir: str, scaled: OrderedDict) -> Any:
    import numpy as np
    from PIL import Image

    found = scaled.get((copy.file, size))
    if found is None:
        with Image.open(Path(workdir) / copy.file) as image:
            found = np.asarray(image.convert("RGB").resize(size, Image.Resampling.BILINEAR))
        scaled[(copy.file, size)] = found
        while len(scaled) > 8:
            scaled.popitem(last=False)
    else:
        scaled.move_to_end((copy.file, size))
    return found


def _drawn(copy: AloneFrame, *, ratio: float, left: int, top: int, size: tuple[int, int]) -> Any:
    """Where *copy* is drawn in its box (``size`` device px from ``left``, ``top``): not where the page draws over it."""
    import numpy as np

    drawn = np.ones((size[1], size[0]), dtype=bool)
    for ox, oy, ow, oh in copy.over:
        x0, x1 = _span(ox, ox + ow, ratio, left, size[0])
        y0, y1 = _span(oy, oy + oh, ratio, top, size[1])
        drawn[y0:y1, x0:x1] = False
    return drawn


def _paste(
    picture: Any, copy: AloneFrame, *, ratio: float, workdir: str, scaled: OrderedDict, static: Any = None
) -> None:
    """*copy* drawn into its box of the frame *picture* (device px), but where the page draws over it and where
    *static* (a mask of its box, :func:`static_layers`) keeps the page as painted."""
    left, right, top, bottom = _region(copy.clip, ratio, picture.shape)
    if right - left < 2 or bottom - top < 2:
        return
    size = (right - left, bottom - top)
    found = _scaled_copy(copy, size, workdir=workdir, scaled=scaled)
    drawn = _drawn(copy, ratio=ratio, left=left, top=top, size=size)
    if static is not None and static.shape == drawn.shape:
        drawn &= ~static
    region = picture[top:bottom, left:right]
    region[drawn] = found[drawn]


def _luma(picture: Any) -> Any:
    import numpy as np

    return (picture[..., 0].astype(np.int16) * 3 + picture[..., 1].astype(np.int16) * 4 + picture[..., 2]) // 8


def static_layers(
    run: AloneRun,
    frames: Sequence[tuple[float, str]],
    *,
    workdir: str,
    ratio: float,
    scaled: OrderedDict,
) -> tuple[Any, float] | None:
    """What the page draws over *run*'s canvas that stands still while the canvas moves under it and that its copies
    miss (a toolbar or a floating button the grid of ``over`` boxes did not catch, #9590): a mask of the canvas's box
    (device px) and its share of the box, ``None`` where fewer than two frames were painted about the run to tell.

    Read on the frames painted from the last one before the run to the first one after it: a pixel that differs by at
    most :data:`STATIC_LEVELS` grey levels over all of them stands still there; one of those where the run's copy at
    each frame's instant differs from the frame by more than :data:`MATCH_LEVELS` on at least half of them is a layer
    the copy misses, grown by :data:`STATIC_GROW` px. Composited, those pixels stay the frame's painted behind: the
    chrome is drawn at one place on every frame, never once from the page and once from the copy's box."""
    import numpy as np

    times = [t for t, _name in frames]
    first = max(bisect.bisect_right(times, run.start + 1e-6) - 1, 0)
    last = min(bisect.bisect_left(times, run.end - 1e-6) + 1, len(frames))
    picked = list(range(first, last))
    if len(picked) < 2:
        return None
    low = high = None
    misses = None
    compared = 0
    box: tuple[int, int, int, int] | None = None
    for index in picked:
        t, name = frames[index]
        try:
            picture = _rgb(Path(workdir) / name)
        except Exception as exc:  # a frame not read tells nothing
            logger.debug("Frame %s not read for its static layers: %s", name, format_exception_message(exc))
            continue
        if box is None:
            box = _region(run.clip, ratio, picture.shape)
        left, right, top, bottom = box
        if right - left < 2 or bottom - top < 2:
            return None
        crop = picture[top:bottom, left:right]
        if crop.shape[0] != bottom - top or crop.shape[1] != right - left:
            continue
        grey = _luma(crop)
        low = grey if low is None else np.minimum(low, grey)
        high = grey if high is None else np.maximum(high, grey)
        copy = run.latest(min(max(t, run.start), run.end))
        size = (right - left, bottom - top)
        mine = _luma(_scaled_copy(copy, size, workdir=workdir, scaled=scaled))
        differs = np.abs(mine - grey) > MATCH_LEVELS
        misses = differs.astype(np.int16) if misses is None else misses + differs
        compared += 1
    if compared < 2 or low is None or high is None or misses is None:
        return None
    static = ((high - low) <= STATIC_LEVELS) & (misses * 2 >= compared)
    if STATIC_GROW and static.any():
        grown = static.copy()
        rows, cols = static.shape
        for dy in range(-STATIC_GROW, STATIC_GROW + 1):
            for dx in range(-STATIC_GROW, STATIC_GROW + 1):
                ys, ye = max(dy, 0), rows + min(dy, 0)
                xs, xe = max(dx, 0), cols + min(dx, 0)
                grown[ys:ye, xs:xe] |= static[ys - dy : ye - dy, xs - dx : xe - dx]
        static = grown
    return static, float(static.mean())


@dataclass
class Composited:
    """What :func:`composite` made of a take: its frames (those painted, a composited run's in place of those it
    painted over its span), the window's scroll offset each composited frame stands at (its painted frame's), its runs."""

    frames: list[tuple[float, str]]
    painted: dict[str, float] = field(default_factory=dict)
    runs: list[dict] = field(default_factory=list)
    #: The copies skipped, every encoder behind, outside every run, per step (:func:`skipped_outside`):
    #: ``[{step, skipped, t: [first, last]}]`` (``alone.skipped_outside_runs``).
    skipped_outside: list[dict] = field(default_factory=list)


def kept_skips(alone: dict | None) -> list[tuple[float, str]]:
    """The skipped copies a take's ``alone`` keeps (``Tap.stop``'s ``skips``) as ``(seconds, canvas box)``; none for
    a take recorded before they were kept."""
    found: list[tuple[float, str]] = []
    for skip in (alone or {}).get("skips") or []:
        if isinstance(skip, list | tuple) and len(skip) == 2 and isinstance(skip[0], int | float):
            found.append((float(skip[0]), str(skip[1])))
    return found


def copies_skipped(count: int) -> str:
    """*count* skipped copies in words: "1 copy skipped", "4 copies skipped"."""
    return f"{count} {'copy' if count == 1 else 'copies'} skipped"


def _skip_windows(instants: Sequence[float]) -> list[list[float]]:
    """*instants* (sorted) as ``[[t0, t1], ...]``: two skips :data:`SKIP_GAP` apart at most are one window."""
    windows: list[list[float]] = []
    for t in instants:
        if windows and t - windows[-1][1] <= SKIP_GAP:
            windows[-1][1] = round(t, 3)
        else:
            windows.append([round(t, 3), round(t, 3)])
    return windows[:MOST_SKIP_WINDOWS]


def skips_by_run(
    runs: Sequence[AloneRun], skips: Sequence[tuple[float, str]]
) -> tuple[dict[int, list[float]], list[float]]:
    """The skipped copies *skips* (``(seconds, canvas box)``, ``Tap.skips``) of each of *runs* (by index) and those of
    none: a skip of a run's canvas from :data:`RUN_GAP` before its first copy to :data:`RUN_GAP` after its last (the
    gap that would have kept it one run, had the copy been made) is the run's, the nearest run's where two meet. A
    burst's tail of skips after its last copy so stays the burst's (#10970)."""
    inside: dict[int, list[float]] = {}
    outside: list[float] = []
    for t, surface in sorted(skips):
        best: tuple[float, int] | None = None
        for index, run in enumerate(runs):
            if run.copies[0].surface != surface or not run.start - RUN_GAP <= t <= run.end + RUN_GAP:
                continue
            distance = max(run.start - t, t - run.end, 0.0)
            if best is None or distance < best[0]:
                best = (distance, index)
        if best is None:
            outside.append(t)
        else:
            inside.setdefault(best[1], []).append(t)
    return inside, outside


def skipped_outside(instants: Sequence[float], events: list[dict]) -> list[dict]:
    """The skips of no run (*instants*, sorted) per step the take was playing then (``capture_rate.step_at``):
    ``[{step, skipped, t: [first, last]}]`` in the order the steps played, a skip before any step under ``None``."""
    by_step: dict[Any, dict] = {}
    for t in instants:
        step = (step_at(events, t) or {}).get("step")
        entry = by_step.setdefault(step, {"step": step, "skipped": 0, "t": [round(t, 3), round(t, 3)]})
        entry["skipped"] += 1
        entry["t"][1] = round(t, 3)
    return sorted(by_step.values(), key=lambda entry: entry["t"][0])[:MOST_RUNS]


#: The scale a run's canvas is read at in the frames painted over it to tell those that moved it, as
#: ``capture_rate.canvas_motion`` reads a canvas transition (its ``_CANVAS_COMPARED_AT``).
MOVED_AT = 8


def moved_rate(run: AloneRun, frames: Sequence[tuple[float, str]], *, workdir: str, ratio: float, greys: dict) -> float:
    """The rate the screencast moved *run*'s canvas at: the frames painted over its span whose canvas box differs from
    the frame painted before by ``capture_rate.MOVING_SHARE`` (as ``capture_rate.canvas_motion`` counts a transition's
    frames), a second. A screencast handing over a frame at the take's rate while the canvas changed on one of two
    counted as painted at its rate (#9266, investigate step 6: canvas moved at 14.1 fps, kept "painted at its rate");
    *greys* keeps the frames read, by name."""
    from app.services.video_studio.capture_rate import MOVING_SHARE, moved_share

    times = [t for t, _name in frames]
    first = max(bisect.bisect_left(times, run.start - 1e-6) - 1, 0)
    last = bisect.bisect_right(times, run.end + 1e-6)
    scale = ratio / MOVED_AT
    x, y, w, h = run.clip
    moved, before = 0, None
    for index in range(first, last):
        name = frames[index][1]
        grey = greys.get(name)
        if grey is None:
            try:
                grey = greys[name] = _grey(Path(workdir) / name, MOVED_AT)
            except Exception as exc:  # a frame not read: the next one is compared with the last read
                logger.debug("Frame %s not read for its canvas: %s", name, format_exception_message(exc))
                continue
        left, right = _span(x, x + w, scale, 0, grey.shape[1])
        top, bottom = _span(y, y + h, scale, 0, grey.shape[0])
        crop = grey[top:bottom, left:right]
        if (
            before is not None
            and crop.size
            and times[index] >= run.start - 1e-6
            and moved_share(before, crop) >= MOVING_SHARE
        ):
            moved += 1
        before = crop
    return moved / max(run.end - run.start, 1e-6)


def composite(
    painted: Sequence[tuple[float, str]],
    alone: Sequence[AloneFrame],
    events: list[dict],
    *,
    fps: int,
    pixel_ratio: float,
    workdir: str,
    offsets: dict[str, float] | None = None,
    skips: Sequence[tuple[float, str]] = (),
) -> Composited:
    """The frames *painted* by a take's browser (``(seconds, file name in workdir)``), each run of its canvases'
    copies *alone* the screencast fell short on composited in (see the module's docstring), at *fps*. *pixel_ratio*
    is the frames' device px a CSS px; *offsets* the window's scroll offset each painted frame stood at; *skips* the
    copies the page skipped, every encoder behind (``Tap.skips``), said on the run or the step they fell in."""
    from app.services.video_studio.scroll_pacing import PAINTED_ENOUGH, FrameWriter

    frames = sorted(painted)
    if not alone or not frames:
        return Composited(frames, skipped_outside=skipped_outside(sorted(t for t, _key in skips), events))
    times = [t for t, _name in frames]
    by_surface: dict[str, list[AloneFrame]] = {}
    for copy in sorted(alone, key=lambda c: c.t):
        by_surface.setdefault(copy.surface, []).append(copy)
    chosen: list[AloneRun] = []
    kept: list[dict] = []
    greys: dict[str, Any] = {}
    scaled: OrderedDict = OrderedDict()
    statics: dict[int, Any] = {}
    runs = alone_runs(alone, events)
    skipped_in, skipped_out = skips_by_run(runs, skips)
    for serial_run, run in enumerate(runs):
        seconds = max(run.end - run.start, 1e-6)
        inside = bisect.bisect_right(times, run.end) - bisect.bisect_left(times, run.start)
        rate = inside / seconds
        # The screencast's frames a second over the run, and those of them that moved its canvas: a frame handed over
        # with the canvas as it was is no frame of the transition.
        moved = min(rate, moved_rate(run, frames, workdir=workdir, ratio=pixel_ratio, greys=greys))
        event = step_at(events, run.start) or {}
        entry: dict[str, Any] = {
            "canvas": run.copies[-1].canvas,
            "t": [round(run.start, 3), round(run.end, 3)],
            "step": event.get("step"),
            "copies": len(run.copies),
            "fps": round(run.fps, 1),
            "painted": round(rate, 1),
            "moved": round(moved, 1),
            "composited": False,
        }
        if (drawn_fps := run.drawn_fps) is not None:
            entry["drawn"] = round(drawn_fps, 1)
        # Said on every run, 0 and [] when none fell in it, so the record keeps one shape (#10970, retest c3a8b4a6).
        run_skips = skipped_in.get(serial_run) or []
        entry["skipped"] = len(run_skips)
        entry["skipped_windows"] = _skip_windows(run_skips)
        if moved >= PAINTED_ENOUGH * fps:
            entry["why"] = PAINTED_AT_RATE
        elif run.fps <= moved:
            entry["why"] = NO_FASTER
        elif why := check(
            run,
            by_surface[run.copies[0].surface],
            frames,
            workdir=workdir,
            ratio=pixel_ratio,
            checked=entry.setdefault("checked", []),
        ):
            entry["why"] = why
        else:
            # What the page holds still over the canvas while it moves (a toolbar, a floating button) stays the
            # frame's painted behind on every composited frame, never drawn once from the page and once from the copy
            # (#9590); a run whose copies miss too much of the box is no copy of what the browser painted.
            found = static_layers(run, frames, workdir=workdir, ratio=pixel_ratio, scaled=scaled)
            share = found[1] if found is not None else 0.0
            if share > STATIC_MOST:
                entry["why"] = UNALIGNED
                entry["static"] = round(share, 4)
            else:
                entry["composited"] = True
                if found is not None and share > 0:
                    statics[id(run)] = found[0]
                    entry["static"] = round(share, 4)
                chosen.append(run)
        if len(kept) < MOST_RUNS:
            kept.append(entry)
        else:
            # A run past those kept says its skips with the step they fell in: the counts still sum to the total.
            skipped_out.extend(skipped_in.get(serial_run, []))
    outside = skipped_outside(sorted(skipped_out), events)
    if not chosen:
        return Composited(frames, runs=kept, skipped_outside=outside)
    drawn: list[tuple[float, str]] = []
    standing: dict[str, float] = {}
    background: tuple[int, Any] | None = None
    instants = sorted({copy.t for run in chosen for copy in run.copies})
    with ThreadPoolExecutor(max_workers=4) as saver:
        writer = FrameWriter(Path(workdir), saver)
        for serial, t in enumerate(instants):
            index = bisect.bisect_right(times, t + 1e-6) - 1
            if index < 0:
                continue
            if background is None or background[0] != index:
                background = (index, _rgb(Path(workdir) / frames[index][1]))
            picture = background[1].copy()
            for run in chosen:
                if run.start - 1e-6 <= t <= run.end + 1e-6:
                    _paste(
                        picture,
                        run.latest(t),
                        ratio=pixel_ratio,
                        workdir=workdir,
                        scaled=scaled,
                        static=statics.get(id(run)),
                    )
            name = f"{COMPOSITED_PREFIX}{serial:06d}.jpg"
            writer.write(picture, name)
            drawn.append((round(t, 6), name))
            if offsets and frames[index][1] in offsets:
                standing[name] = offsets[frames[index][1]]
        writer.flush()
    inside_runs = [(run.start, run.end) for run in chosen]
    out = [frame for frame in frames if not any(a - 1e-6 <= frame[0] <= b + 1e-6 for a, b in inside_runs)]
    return Composited(sorted(out + drawn), standing, kept, outside)


def notes(runs: Sequence[dict] | None, gl: str = "software GL") -> list[str]:
    """What a take's notes say of the canvas transitions it composited from their copies: where, the rate the
    screencast painted them at and the rate they play at; *gl* what its canvases were drawn with (``GPU GL`` on a
    recording lane)."""
    said, kept = [], []
    for run in runs or []:
        at = f"step {run['step']}" if run.get("step") is not None else f"{float(run['t'][0]):.1f} s"
        line = (
            f"{run['canvas']} at {at} ({float(run['t'][1]) - float(run['t'][0]):.1f} s, painted at {float(run['painted']):.0f} "
            f"fps, copied at {float(run['fps']):.0f} fps"
        )
        # The rate the page drew the canvas at, where the take counted it: copied below it, the copy loop fell short.
        if run.get("drawn") is not None:
            line += f" of {float(run['drawn']):.0f} drawn"
        # The copies of it skipped while every encoder was behind (``composite``'s ``skipped``): none, nothing said.
        if run.get("skipped"):
            line += f", {copies_skipped(int(run['skipped']))} (every encoder behind)"
        if run.get("composited"):
            said.append(line + ")")
        elif gl != "software GL":
            # On a GPU every canvas transition copied alone gets its rates said, kept as painted too: a wheel zoom left
            # to the screencast was named nowhere (#10955, step 22 of take 032b3ba3).
            kept.append(line + (f", {run['why']})" if run.get("why") else ")"))
    out = []
    if said:
        out.append(
            f"Captured alone where the screencast fell short ({gl}): "
            + "; ".join(said)
            + (". Those transitions play at reduced quality while they move." if gl == "software GL" else ".")
        )
    if kept:
        out.append(f"Copied alone and kept as painted ({gl}): " + "; ".join(kept) + ".")
    return out


def capture_words(capture: dict | None) -> str:
    """What a take's capture line says of its canvases captured alone: ``canvas transitions captured alone: 1
    composited, 1 kept as painted (painted at its rate)``, the copies the page skipped while every encoder was behind
    (``Tap.busy``), why nothing was copied, or nothing for a take recorded before its canvases were copied alone."""
    alone = (capture or {}).get("alone")
    if not isinstance(alone, dict):
        return ""
    if alone.get("off"):
        return f"canvases not captured alone ({alone['off']})"
    runs = [run for run in alone.get("runs") or [] if isinstance(run, dict)]
    if not runs:
        return "canvases captured alone where the screencast falls short: no transition needed it"
    composited = sum(1 for run in runs if run.get("composited"))
    whys = sorted({str(run.get("why")) for run in runs if run.get("why")})
    left = len(runs) - composited
    said = f"canvas transitions captured alone: {composited} composited"
    if left:
        said += f", {left} kept as painted ({'; '.join(whys)})"
    if alone.get("busy"):
        said += f"; {copies_skipped(int(alone['busy']))}, every encoder behind"
        if where := skipped_words(alone):
            said += f" ({where})"
    if alone.get("lowered"):
        said += f"; {int(alone['lowered'])} copies drawn smaller to keep up"
    if cost := cost_words(alone.get("cost")):
        said += f"; {cost}"
    if worker := worker_words(alone.get("worker")):
        said += f"; {worker}"
    return said


def skipped_words(alone: dict) -> str:
    """Where a take's skipped copies (``alone.busy``) fell: ``4 in transitions, 2 outside (step 3: 2)``, the rest not
    placed (past :data:`MOST_SKIPS`); empty for a take not composited with them (recorded before, or failed), which
    keeps no ``skipped_outside_runs``."""
    runs = [run for run in alone.get("runs") or [] if isinstance(run, dict)]
    outside = [entry for entry in alone.get("skipped_outside_runs") or [] if isinstance(entry, dict)]
    if "skipped_outside_runs" not in alone:
        return ""
    inside = sum(int(run.get("skipped") or 0) for run in runs)
    out = sum(int(entry.get("skipped") or 0) for entry in outside)
    parts = [f"{inside} in transitions", f"{out} outside"]
    if outside:
        steps = ", ".join(
            f"{'step ' + str(entry['step']) if entry.get('step') is not None else 'before any step'}: {int(entry['skipped'])}"
            for entry in outside[:6]
        )
        parts[1] += f" ({steps}{', ...' if len(outside) > 6 else ''})"
    if (unplaced := int(alone.get("busy") or 0) - inside - out) > 0:
        parts.append(f"{unplaced} not placed")
    return ", ".join(parts)


def worker_words(worker: Any) -> str:
    """What the worker's CPUs did while the take recorded (``Tap._worker_cpu``): ``the worker busy 3.6 of its 4
    CPUs, 1.2 s held back by its quota``. A worker near all its CPUs is what holds the copies and the page's paint
    back; one far from them is not. Empty for a take recorded before it was kept."""
    if not isinstance(worker, dict) or not isinstance(worker.get("cpus"), int | float):
        return ""
    of = int(worker.get("of") or 0)
    said = f"the worker busy {float(worker['cpus']):.1f}"
    said += f" of its {of} CPU{'s' if of != 1 else ''}" if of else " CPUs"
    if float(worker.get("throttled") or 0) > 0:
        said += f", {float(worker['throttled']):.1f} s held back by its quota"
    return said


def cost_words(cost: Any) -> str:
    """What copying cost a take (``Tap.cost``), per copy: ``copies cost the page 2.1 ms (1.2 ms finding what is drawn
    over), the encoders 38 ms (4 ms comparing), 210 KB each (412 drawn, 380 encoded)``: where the page's thread, the
    encoders' comparison or their encoding is what holds the copy rate back. An encoder's ms run from a copy's
    arrival to its answer, other copies encoding meanwhile. Empty for a take that drew no copy or recorded before the
    cost was kept; no part said for a take recorded before it was kept."""
    if not isinstance(cost, dict) or not cost.get("copied"):
        return ""
    copied, encoded = float(cost["copied"]), float(cost.get("encoded") or 0)
    said = f"copies cost the page {float(cost.get('page') or 0) / copied:.1f} ms"
    parts: list[str] = []
    if cost.get("snapshotting"):
        parts.append(f"{float(cost['snapshotting']) / copied:.1f} ms snapshotting")
    if cost.get("scanning"):
        parts.append(f"{float(cost['scanning']) / copied:.1f} ms finding what is drawn over")
    if parts:
        said += f" ({', '.join(parts)})"
    # The layers a copy snapshots or reuses: a graph of three stacked canvases copied as one surface holds three; one
    # held where three are drawn means its overlays sit on boxes of their own (take 12d1da1e said neither share).
    if cost.get("layers"):
        said += f", {float(cost['layers']) / copied:.1f} layers a copy"
    if cost.get("layers") and cost.get("reused"):
        said += f", {float(cost['reused']) / float(cost['layers']):.0%} of its layers kept from a copy before"
    if cost.get("gl") and float(cost.get("layers") or 0) > float(cost.get("reused") or 0):
        snapped = float(cost["layers"]) - float(cost.get("reused") or 0)
        said += f", {min(1.0, float(cost['gl']) / snapped):.0%} of its snapshots WebGL layers"
    if cost.get("frames"):
        said += f", its own frames {float(cost.get('drawing') or 0) / float(cost['frames']):.1f} ms drawing"
    if cost.get("long"):
        long, held = int(cost["long"]), float(cost.get("blocked") or 0) / 1000
        said += f", its thread held {held:.1f} s by {long} long task{'s' if long != 1 else ''}"
        # A take whose tap counted the drains says what they held, none included: a take recorded before keeps no
        # ``draining`` and says nothing of it, so a stored take tells which tap recorded it (take d6c76ac8 could not).
        if "draining" in cost:
            draining = float(cost.get("draining") or 0) / 1000
            said += f" ({draining:.1f} s of them" if draining else " (none of them"
            said += " handing copies over)"
    if encoded:
        said += f", the encoders {float(cost.get('encoding') or 0) / encoded:.0f} ms"
        if cost.get("signing"):
            said += f" ({float(cost['signing']) / encoded:.0f} ms comparing)"
        said += f", {float(cost.get('bytes') or 0) / encoded / 1024:.0f} KB each"
    # The frames drawn not copied and why (#10970): over the take's rate (its budget spent), or alike (still); the
    # copies the encoders found alike to the one before. Said only where the tap counted them.
    whys = [
        f"{int(cost[key])} {words}"
        for key, words in (("spent", "frames over the rate"), ("idled", "frames still"), ("alike", "alike"))
        if cost.get(key)
    ]
    tail = f"; not copied or kept: {', '.join(whys)}" if whys else ""
    return f"{said} ({int(copied)} drawn, {int(encoded)} encoded{tail})"


def take_capture_words(capture: dict | None) -> str:
    """What drew a take's canvases and what its canvases captured alone did, said on its capture line, a ready take's
    after its rates and a failed take's alone: ``WebGL drawn by ...; canvases drawn with GPU GL; canvas transitions
    captured alone: ...``. Empty for a take that kept none of it."""
    capture = capture or {}
    said: list[str] = []
    if capture.get("backend"):
        said.append(f"WebGL drawn by {capture['backend']}")
    # Whether its canvases were drawn on a GPU (``capture_rate.drawn_with``).
    drawn = capture.get("gl") or {}
    if drawn.get("gpu") is not None:
        gl = "GPU GL" if drawn["gpu"] else "software GL" + (f" ({drawn['why']})" if drawn.get("why") else "")
        said.append(f"canvases drawn with {gl}")
    if alone := capture_words(capture):
        said.append(alone)
    return "; ".join(said)
