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

"""One scripted recording of the backend browser.

A take is a :class:`~app.services.video_studio.spec.TakeScript` played in the
recording browser (``services/browser_profiles/browser.py``) while Chromium's screencast hands over every
painted frame at the device pixel ratio. What makes a take produce a great
video later, rather than a screen grab:

* **The action log.** Every step records when the cursor started moving
  (``t0``), the instant of the action (``ta``), when it ended (``t1``), the
  CSS box of its element and the point acted on, the scroll that brought the
  element into view (``scrolled``), and the URL before and after it
  (``url_before``, ``url``: a step that opened another page). The production
  zooms, draws the cursor and anchors callouts from it.
* **Human pacing.** The cursor travels along a gentle arc with an
  ease-in-out (its path is recorded), typing has a natural cadence, scrolling
  is wheel-driven on a 60 Hz clock over the step's ``seconds``
  (:func:`scroll_seconds`), so the page scrolls at a steady pace whatever each
  turn of the wheel costs, and any scroll container under the cursor follows -
  nothing teleports. Over a canvas (a graph the wheel zooms) the wheel
  turns on the take's own clock (``TakeScript.fps``), each turn painted before
  the next, and the scroll's log keeps the canvas, each turn's delta and the
  transform its 2D context draws under.
* **A canvas acted on** (``pointer``). A pointer step may act on a point
  of the page or a node of a graph canvas (found by its label, with no hook in
  the application: ``node_locate``, how logged as ``located``)
  rather than an element: the cursor glides there and rests on it, moving, for
  ``hover_ms`` before the button - a canvas reads its hover from the pointer's
  moves -; a ``right_click`` logs the menu it opened, a ``drag`` moves in
  :data:`MIN_DRAG_MOVES` steps at least, a ``wheel`` turns on the target, keys
  held through any of them (``modifiers``). The target's box is logged as an
  element's is, so the camera frames a node clicked.
* **Holds that watch their page** (``page_settle``). A ``wait``, a
  ``pause`` and a ``highlight`` read the page while they hold it
  (:func:`watch_page`: its progress indicators, its DOM's last change, its
  canvases' pixels on the screencast's last frame) and log the spans it showed
  loading; a ``wait`` after a click that navigated or switched a tab (or with
  ``until_settled``) runs on past its seconds until the page settled, at most
  its ``timeout`` more, and logs when it did.
* **Clean frames.** No browser chrome, no scrollbars, sRGB colour; the
  ``redact`` selectors are blurred and the ``hide`` ones removed in the page
  itself, so a secret never reaches a frame.
* **The session, checked before step 1**. A profile's saved session is
  loaded and probed (``signin.check_authenticated``: the start URL renders the
  application, not its sign-in). When it does not, a profile that can sign in
  by itself - credentials (a TOTP secret for an MFA), or a saved identity
  provider session to replay - is signed in again and checked once more;
  otherwise the take stops with "profile not authenticated: landed on <url>"
  and no video is made. The refreshed session is handed back, with its probe,
  to be saved.
* **A clean page state** (:func:`reset_page`). A saved session carries
  what the takes before left in the page - a graph's layout in its origin's
  storage, read back into its address. Once the session is checked, the
  take's ``reset_state`` opens ``start_url`` exactly as given with its
  origin's localStorage and sessionStorage cleared at the document's start,
  but the profile's ``keep_storage_keys``; cookies are never touched. What it
  cleared and the URL it opened on are the first line of the action log
  (``capture.opened``); a page the reset signed out stops the take
  (:class:`ResetSignedOutError`) without marking the session expired. A
  ``goto`` may reset its page too.
* **The first frame signed in**. The take's start page, opened in
  that session, may paint the application's sign-in form while it reads the
  session; the screencast starts only once the page shows signed in
  (``signin.wait_signed_in``), so the first frame is the application and the
  action log counts from there. A page that never does within the sign-in
  timeout stops the take like the pre-flight.
* **A browser zoom the page's layout holds**. A zoom lays the page out
  in the window over the zoom; before the screencast starts, the start page is
  read there (:func:`zoom_fit`): a page wider than that - the document or its
  main container overflowing more than unzoomed - refuses the take before step
  1, naming the width it lays out at, by how much it overflows and the largest
  zoom that keeps its layout.
* **How far down its layout holds**. Once recorded - the screencast
  stopped, so no frame shows it - the page the take ends on is narrowed step
  by step and its layout compared with the recorded one (:func:`probe_layout`):
  the narrowest width that keeps it is kept with the take, so a narrower
  viewport is advised only where the page keeps its desktop layout. A page
  that overflows below its minimum width says that width exactly.

The frames arrive at the browser's own pace (a frame only when the page
painted: about 2 a second at 3200x1800); ``encoder.frames_to_mezzanine`` turns
them into a constant-rate mezzanine, each frame held until the next one was
painted. So while a step scrolls, where its scroller stands is logged at 60 Hz
(:meth:`_Recorder.scroll_log`) - the window, or the scroll container
under the cursor or holding the element brought into view, named as the page
names it - and the scroll is drawn again at the take's rate
(``TakeScript.fps``) from the frames painted (``scroll_pacing.pace``) before it
is encoded. Each frame keeps the window's scroll offset it painted (the
screencast's ``scrollOffsetY``, :attr:`CaptureResult.painted`): a
window's scroll is paced on what its keyframes painted rather than on offsets
read off their pixels; a canvas's zoom on how its keyframes' pixels moved, as
far between them as its log says. A step that changes the page
(:data:`MOTION_ACTIONS`) keeps the windows its CSS transitions and
animations move over (``motion``, :data:`_MOTION_WATCH_JS`), drawn again at the
take's rate the same way (``transition_pacing``). On software GL and on a GPU
(``capture_rate.GlBackend.alone``), every document copies its canvases alone as
they change (``canvas_alone.tap_script``), drained while the take records
(:attr:`CaptureResult.alone`): a canvas transition the screencast fell short
on is composited from them before the take is paced
(``canvas_alone.composite``). A step that misses its
element says, beside the closest names, what the page was filtered by
(:func:`page_filters`: the text its fields held and its address's parameters)
and the page state it missed it in (``take_state``: the page's URL,
title and candidates, and where an earlier take of the profile found it); the
contact sheet keeps the frame where it failed (:func:`sheet_frames`,
:func:`failure_sheet_frames`).
"""

from __future__ import annotations

import asyncio
import base64
import collections
import contextlib
import itertools
import json
import logging
import math
import os
import re
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from app.core.utils import format_exception_message
from app.services.browser_profiles.browser import (
    ProfileBrowser,
    StorageReset,
    TargetPolicy,
    open_cleared,
    origin_of,
    profile_browser,
    seed_storage_script,
)
from app.services.browser_profiles.profiles import ProfileSnapshot
from app.services.video_studio import canvas_alone, page_settle, pointer, take_state
from app.services.video_studio.spec import (
    LOCATOR_ACTIONS,
    MIN_VIEWPORT_WIDTH,
    POINTER_ACTIONS,
    TYPING_DELAY_MS,
    NodeTarget,
    PageReset,
    TakeScript,
    TakeStep,
    Viewport,
    page_reset,
)
from app.services.video_studio.transition_pacing import MOST_SECONDS, PAINT_AFTER

logger = logging.getLogger(__name__)

#: Seconds a cursor travel takes, by pace: (base, per 1000 CSS px, max).
_PACE = {"fast": (0.18, 0.22, 0.55), "natural": (0.3, 0.38, 0.95), "slow": (0.45, 0.55, 1.4)}
_CURSOR_STEP_SECONDS = 1 / 60
#: The buttons a node step reads the page's hover card for before pressing (``pointer.hover_check``).
_NODE_BUTTONS = frozenset({"click", "double_click", "right_click", "drag"})
#: The steps after which the places a node was confirmed at are forgotten: what pans, zooms or re-lays the view. A
#: place kept across another step is still checked by the card under the cursor before any button.
_VIEW_MOVERS = frozenset({"goto", "drag", "wheel", "scroll", "select"})
#: Seconds a ``drag`` holds the button while the cursor moves when its step names none, the fewest moves it
#: makes on the way (each a ``pointermove``: a lasso or a node follows them smoothly), and the pause after the press.
DRAG_SECONDS = 0.8
MIN_DRAG_MOVES = 24
_DRAG_PRESS_SECONDS = 0.06
_SCREENCAST_QUALITY = 95
#: CSS pixels a second the wheel scrolls at by pace, and the shortest and longest a scroll step takes unless
#: its ``seconds`` say otherwise. The wheel turns once a frame of the take (:func:`wheel_deltas`).
_SCROLL_SPEED = {"fast": 2400.0, "natural": 1500.0, "slow": 900.0}
_SCROLL_SECONDS = (0.35, 4.0)
#: How long a canvas's zoom is read on once the wheel stopped: at least the first, at most the second, until
#: its 2D transform holds - a canvas may ease on after the wheel.
_CANVAS_SETTLE_SECONDS = (0.3, 1.0)
#: CSS px a box drawn over a canvas may move by between the wheel's start and its settle and still be the same box.
_SAME_OVER_PX = 2.0
#: How the page names an element in a take's log: its tag, id, first two classes and role -
#: ``main#content.panel[region]``.
_NAME_JS = """(el) => {
    let said = el.tagName.toLowerCase();
    if (el.id) said += '#' + el.id;
    const classes = typeof el.className === 'string' ? el.className.trim().split(/\\s+/).filter(Boolean).slice(0, 2) : [];
    if (classes.length) said += '.' + classes.join('.');
    const role = el.getAttribute('role');
    if (role) said += '[' + role + ']';
    return said.slice(0, 120);
  }"""
#: Watches what scrolls on the page: the window, and every scroll container the recorder marks (the one under
#: the cursor, those holding an element brought into view) or that scrolls. ``read()`` answers ``[t, scrollY,
#: scrollX, [[id, top, left, [x, y, w, h]], ...]]``: *t* in milliseconds since the epoch, the clock the screencast's
#: frames are stamped on, and each container's client box in CSS pixels of the viewport; ``names()`` how the page
#: names each. The window is the document's own scroller: a body laid out as a scroll container is one.
_SCROLL_WATCH_JS = (
    """(() => {
  if (window.__xtmScroll) return;
  const page = (el) => !el || el === document || el === document.scrollingElement;
  const name = __NAME__;
  const watch = window.__xtmScroll = {
    n: 0, els: [],
    add(el) {
      if (page(el) || !(el instanceof Element)) return;
      if (!el.__xtmScrollId) el.__xtmScrollId = ++watch.n;
      if (!watch.els.includes(el)) watch.els.push(el);
    },
    holding(el) {
      for (let node = el; node && !page(node); node = node.parentElement) {
        const cs = getComputedStyle(node);
        if (/(auto|scroll|overlay)/.test(cs.overflowY) && node.scrollHeight > node.clientHeight + 1) watch.add(node);
      }
    },
    at(x, y) { watch.holding(document.elementFromPoint(x, y)); },
    names() { return Object.fromEntries(watch.els.map((el) => [el.__xtmScrollId, name(el)])); },
    read() {
      watch.els = watch.els.filter((el) => el.isConnected);
      return [performance.timeOrigin + performance.now(), window.scrollY, window.scrollX, watch.els.map((el) => {
        const r = el.getBoundingClientRect();
        return [el.__xtmScrollId, el.scrollTop, el.scrollLeft, [r.left + el.clientLeft, r.top + el.clientTop, el.clientWidth, el.clientHeight]];
      })];
    },
  };
  document.addEventListener('scroll', (e) => watch.add(e.target), { capture: true, passive: true });
})()"""
).replace("__NAME__", _NAME_JS)
#: The page's ``position: fixed`` and ``position: sticky`` elements in view: ``[[x, y, w, h, position], ...]``
#: in CSS px of the viewport: every element of the document walked, shown and in view, however thin, as the slice of it
#: that paints. Its box is cut to the viewport (and, for a sticky one, to every ancestor clipping its overflow), then
#: hit-tested on up to :data:`_FIXED_HIT_ROWS` rows of :data:`_FIXED_HIT_POINTS` points: a row paints when the topmost
#: element painting at half or more of its points is it or one of its own, and the box recorded runs from the first row
#: that paints to the last, so a box an ancestor clips away or another layer covers vouches for nothing. A point's stack
#: is read with ``pointer-events`` lifted to ``auto`` on every element by a constructed stylesheet for the synchronous
#: probe (no DOM mutation, no frame painted in between): an overlay that lets clicks through is seen, and so is an
#: element of that kind. Walking the stack from the top, a layer that paints nothing itself (a transparent wrapper, a
#: portal) is passed; the first that paints (a background, an image, a canvas) is the point's owner. A sticky or fixed
#: band the scroll pacing finds is held only on one of them, so the list is all or nothing: ``null`` (not read: the bands
#: stay unchecked, as on a take recorded before) past :data:`_FIXED_WALK_LIMIT` elements or :data:`_FIXED_BOX_LIMIT`
#: boxes, or when the probe cannot be made (no constructed stylesheets, a ``pointer-events: none !important`` inline).
_FIXED_WALK_LIMIT = 20000
_FIXED_BOX_LIMIT = 40
_FIXED_HIT_POINTS = 9
_FIXED_HIT_ROWS = 5
_FIXED_BOXES_JS = (
    """() => {
  if (!document.body) return null;
  if (!('adoptedStyleSheets' in document) || typeof CSSStyleSheet !== 'function') return null;
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_ELEMENT), found = [];
  let walked = 0;
  for (let el = walker.currentNode; el; el = walker.nextNode()) {
    if (++walked > __WALK__) return null;
    const cs = getComputedStyle(el);
    if (cs.position !== 'fixed' && cs.position !== 'sticky') continue;
    if (cs.display === 'none' || cs.visibility === 'hidden' || Number(cs.opacity) === 0) continue;
    found.push(el);
  }
  const replaced = new Set(['IMG', 'CANVAS', 'VIDEO', 'IFRAME', 'svg', 'EMBED', 'OBJECT', 'INPUT', 'TEXTAREA', 'SELECT']);
  const paints = (node) => {
    if (replaced.has(node.tagName)) return true;
    const s = getComputedStyle(node);
    if (s.backgroundImage !== 'none') return true;
    // Alpha of the computed colour: rgb(r, g, b) is opaque (rgb(0, 0, 0) is black), rgba(..., a) carries it.
    const parts = s.backgroundColor.match(/[\\d.]+/g) || [];
    return s.backgroundColor !== 'transparent' && (parts.length < 4 || Number(parts[3]) > 0);
  };
  const owns = (el, x, y) => {
    for (const node of document.elementsFromPoint(x, y)) {
      // The element itself vouches only where it paints; a transparent one (a portal root) lets the stack below show.
      if (node === el) { if (paints(el)) return true; continue; }
      if (el.contains(node)) return true;
      if (node.contains(el)) return false;
      if (paints(node)) return false;
    }
    return false;
  };
  const probe = (el, x0, x1, y0, y1) => {
    const rows = Math.min(__ROWS__, Math.max(1, Math.floor((y1 - y0) / 8)));
    let first = -1, last = -1;
    for (let r = 0; r < rows; r++) {
      const y = y0 + (r + 0.5) * (y1 - y0) / rows;
      let hits = 0;
      for (let i = 0; i < __POINTS__; i++) if (owns(el, x0 + (i + 0.5) * (x1 - x0) / __POINTS__, y)) hits++;
      if (2 * hits >= __POINTS__) { if (first < 0) first = r; last = r; }
    }
    return first < 0 ? null : [y0 + first * (y1 - y0) / rows, y0 + (last + 1) * (y1 - y0) / rows];
  };
  const boxes = [], before = document.adoptedStyleSheets, lift = new CSSStyleSheet();
  lift.replaceSync('* { pointer-events: auto !important; }');
  document.adoptedStyleSheets = [...before, lift];
  try {
    for (const el of found) {
      if (getComputedStyle(el).pointerEvents === 'none') return null;
      const cs = getComputedStyle(el), position = cs.position, b = el.getBoundingClientRect();
      let x0 = Math.max(0, b.left), x1 = Math.min(innerWidth, b.right), y0 = Math.max(0, b.top), y1 = Math.min(innerHeight, b.bottom);
      if (position === 'sticky') {
        for (let up = el.parentElement; up && up !== document.documentElement; up = up.parentElement) {
          const us = getComputedStyle(up);
          if (us.overflowX === 'visible' && us.overflowY === 'visible') continue;
          const c = up.getBoundingClientRect();
          x0 = Math.max(x0, c.left); x1 = Math.min(x1, c.right); y0 = Math.max(y0, c.top); y1 = Math.min(y1, c.bottom);
        }
      }
      if (x1 - x0 <= 0 || y1 - y0 <= 0) continue;
      const painted = probe(el, x0, x1, y0, y1);
      if (!painted) continue;
      if (boxes.length >= __BOXES__) return null;
      boxes.push([x0, painted[0], x1 - x0, painted[1] - painted[0], position]);
    }
  } finally {
    document.adoptedStyleSheets = before;
  }
  return boxes;
}"""
    .replace("__WALK__", str(_FIXED_WALK_LIMIT))
    .replace("__BOXES__", str(_FIXED_BOX_LIMIT))
    .replace("__POINTS__", str(_FIXED_HIT_POINTS))
    .replace("__ROWS__", str(_FIXED_HIT_ROWS))
)
#: Where the window stands and the furthest it scrolls down (CSS px): what a ``scroll`` step to ``top`` or ``bottom``
#: moves it between, a frame at a time.
_WINDOW_SCROLL_RANGE_JS = (
    "() => { const s = document.scrollingElement || document.documentElement;"
    " return [window.scrollY, Math.max(0, s.scrollHeight - window.innerHeight)]; }"
)
#: Before any page script: each canvas remembers the kind of context it was given (``2d``, ``webgl2``...), so
#: the recorder reads a 2D canvas's transform without ever giving a canvas a context it did not ask for.
_CANVAS_CONTEXT_JS = """(() => {
  const proto = HTMLCanvasElement.prototype, get = proto.getContext;
  if (get.__xtmContext) return;
  const remembering = function (type, ...rest) {
    const context = get.call(this, type, ...rest);
    if (context && !this.__xtmContext) Object.defineProperty(this, '__xtmContext', { value: String(type), configurable: true });
    return context;
  };
  remembering.__xtmContext = true;
  proto.getContext = remembering;
})()"""
#: Where a wheel at ``[x, y, dy]`` (CSS px, *dy* its direction) turns: ``null`` when the point's scroll chain
#: (the element under it and its ancestors, then the document) can move that way; else the visible centre ``[x, y]`` of
#: the largest scroll container of the page that can, and how the page names it - an app shell whose content scrolls
#: in an inner container under a fixed top bar or beside a side menu loses every turn over the bar or the menu.
#: ``{"none": true}``: nothing on the page can move that way.
_WHEEL_TARGET_JS = (
    """([x, y, dy]) => {
  const name = __NAME__;
  const can = (el) => {
    const cs = getComputedStyle(el);
    if (!/(auto|scroll|overlay)/.test(cs.overflowY) || el.scrollHeight <= el.clientHeight + 1) return false;
    return dy > 0 ? el.scrollTop + el.clientHeight < el.scrollHeight - 1 : el.scrollTop > 0;
  };
  const doc = document.scrollingElement || document.documentElement;
  const pageCan = doc.scrollHeight > window.innerHeight + 1 && (dy > 0 ? window.scrollY + window.innerHeight < doc.scrollHeight - 1 : window.scrollY > 0);
  for (let node = document.elementFromPoint(x, y); node && node !== doc && node !== document.body; node = node.parentElement) {
    if (can(node)) return null;
  }
  if (pageCan) return null;
  let best = null, area = 0;
  for (const el of document.querySelectorAll('*')) {
    if (el === doc || el === document.body || !can(el)) continue;
    const r = el.getBoundingClientRect();
    const w = Math.min(r.right, window.innerWidth) - Math.max(r.left, 0);
    const h = Math.min(r.bottom, window.innerHeight) - Math.max(r.top, 0);
    if (w <= 0 || h <= 0 || w * h <= area) continue;
    const cx = Math.max(r.left, 0) + w / 2, cy = Math.max(r.top, 0) + h / 2;
    const hit = document.elementFromPoint(cx, cy);
    if (!hit || !el.contains(hit)) continue;
    best = [cx, cy, name(el)];
    area = w * h;
  }
  return best ? { point: [best[0], best[1]], element: best[2] } : { none: true };
}"""
).replace("__NAME__", _NAME_JS)
#: The canvas a wheel at ``[x, y]`` (CSS px) lands on: the topmost one there, its context and client box, kept
#: as ``window.__xtmSurface`` with the 2D one under it whose transform :data:`_CANVAS_TICK_JS` reads; ``null``: none.
#: ``over``: the boxes of what the page draws over it (a toolbar, a legend), read on a grid across it - an element
#: above it there, shown, smaller than half of it (a layer as large as the canvas is part of its drawing).
_CANVAS_AT_JS = """([x, y]) => {
  const canvases = document.elementsFromPoint(x, y).filter((el) => el instanceof HTMLCanvasElement);
  window.__xtmSurface = null;
  if (!canvases.length) return null;
  const canvas = canvases[0];
  const drawn = canvases.find((el) => el.__xtmContext) || null;
  window.__xtmSurface = { flat: canvases.find((el) => el.__xtmContext === '2d') || null };
  const r = canvas.getBoundingClientRect();
  const left = Math.max(r.left, 0), top = Math.max(r.top, 0);
  const right = Math.min(r.right, innerWidth), bottom = Math.min(r.bottom, innerHeight);
  const seen = new Set(), over = [];
  // A box drawn over the canvas that takes no pointer (a graph's link tooltip is pointer-events: none) is skipped by
  // elementsFromPoint: every element but the canvases (stacked layers keep their own order) takes the pointer while
  // the boxes are read, in this one task, so nothing paints.
  const every = document.createElement('style');
  every.textContent = ':not(canvas) { pointer-events: auto !important; }';
  document.documentElement.appendChild(every);
  try {
    for (let i = 0; i < 24; i++) {
      for (let j = 0; j < 14; j++) {
        const px = left + (i + 0.5) * (right - left) / 24, py = top + (j + 0.5) * (bottom - top) / 14;
        for (const el of document.elementsFromPoint(px, py)) {
          if (el === canvas) break;
          if (seen.has(el) || el.contains(canvas)) continue;
          seen.add(el);
          const b = el.getBoundingClientRect(), cs = getComputedStyle(el);
          if (cs.visibility === 'hidden' || Number(cs.opacity) === 0) continue;
          if (b.width * b.height > 0.5 * (right - left) * (bottom - top)) continue;
          if (over.length < 40) over.push([b.left, b.top, b.width, b.height]);
        }
      }
    }
  } finally {
    every.remove();
  }
  return { context: drawn ? drawn.__xtmContext : null, box: [r.left, r.top, r.width, r.height], over };
}"""
#: Once the page has painted the frame a turn of the wheel drew: ``[t, [a, b, c, d, e, f] | null]``, *t* in
#: milliseconds since the epoch (the screencast's clock) and the 2D canvas's transform, read once that frame's rendering
#: is done; a page that paints nothing answers after 250 ms.
_CANVAS_TICK_JS = """() => new Promise((resolve) => {
  const read = () => {
    const flat = window.__xtmSurface && window.__xtmSurface.flat;
    let m = null;
    if (flat) {
      try { const t = flat.getContext('2d').getTransform(); m = [t.a, t.b, t.c, t.d, t.e, t.f]; } catch (e) { m = null; }
    }
    resolve([performance.timeOrigin + performance.now(), m]);
  };
  const own = () => setTimeout(read, 0);
  own.__xtmOwn = true;
  requestAnimationFrame(own);
  setTimeout(read, 250);
})"""
#: The paints of a wheel's turns over a GPU canvas read at once (``_Recorder._scrolled``): the turns are never held
#: by a read, and a page that stopped painting answers each read within 250 ms.
_GPU_TICKS_IN_FLIGHT = 4
#: The wheel turns over a GPU canvas sent ahead of the browser's answer to the first (``_Recorder._scrolled``): each
#: still goes out on its own tick, never a burst queued on the page.
_GPU_WHEELS_IN_FLIGHT = 6
#: The wheel's turns over a GPU canvas a frame of the take, each that share of the frame's distance. The page redraws
#: its canvas only when a turn lands, and the compositor presents about 60 frames a second: one turn a frame of a
#: 30 fps take, delivered with any jitter, put two turns in one presented frame and none in the next, and the zoom
#: painted at 20 to 26 fps beside 50 at rest (#10955). Two a frame keep a turn in every presented frame; the take is
#: still copied once a frame of its rate.
_GPU_TURNS_PER_FRAME = 2


def gpu_ticks(read: Sequence[Any]) -> list[list]:
    """The ticks of a wheel's turns read beside each other on a GPU (``_Recorder._painted_tick``), in the order they
    were painted; a read that failed (navigating) is left out."""
    return sorted((tick for tick in read if isinstance(tick, list) and tick), key=lambda tick: float(tick[0]))


#: The most spans a step keeps of what its page asked to move over (``painting``, #10101).
MOST_PAINTING_SPANS = 400
#: The most frame times a step keeps of those its page's renderer produced (``page_frames``, #10970).
MOST_PAGE_FRAMES = 4000
#: The steps whose page changes are paced at the take's rate (``transition_pacing``): what a person acts on.
MOTION_ACTIONS = frozenset({"hover", "click", "double_click", "type", "select", "wait_for", "press"})
#: Notes, from the document's start, each element a transition or an animation runs on and each one a step changes.
#: ``take(since)`` answers ``[[start, end, element], ...]`` in milliseconds since the epoch (the scroll
#: watch's clock): from each note, as long as the element moves - its computed transition (of the property that
#: runs) or animation, delay and iteration count with it, its subtree read for a change - at most ``MOST`` ms. The
#: screencast hands over a frame only when the page paints: until ``AFTER`` ms past a motion's end, a dot of one pixel
#: at 1% (``#__xtm_paint``) is repainted every ``EVERY`` ms, so the page as the change left it is painted soon after.
_MOTION_WATCH_JS = (
    (
        """(() => {
  if (window.__xtmMotion || window.top !== window) return;
  const MOST = __MOST__, AFTER = __AFTER__, EVERY = 200, KEPT = 2000, READ = 2000, KEPT_OUT = 200;
  const PAINT_GAP = 100, PAINTING_KEPT = 4000;
  const clock = () => performance.timeOrigin + performance.now();
  let until = 0, ticking = false;
  // When the page itself asked to move (#10101): each animation frame it requested, each transition or animation
  // that ran, merged into [start, end] spans (ms since the epoch). A second it asked for none is idle, not starved.
  const painting = [];
  const painted = (start, end) => {
    const last = painting[painting.length - 1];
    if (last && start - last[1] <= PAINT_GAP) { last[1] = Math.max(last[1], end); return; }
    painting.push([start, end]);
    if (painting.length > PAINTING_KEPT) painting.shift();
  };
  const requestFrame = window.requestAnimationFrame;
  // The frames the page's renderer produced while it moved (#10970): an animation frame loop of its own, run while the
  // page asks to move and past each motion, timed each frame (ms since the epoch). A span the page itself drew under
  // the take's rate is told by it from one the screencast fell short on (``capture_rate.mark_page_limited``).
  const FRAMES_KEPT = 4000, FRAMES_AFTER = 300;
  const frames = [];
  let framesUntil = 0, framing = false;
  const ownFrame = Object.assign(() => {
    if (performance.now() > framesUntil) { framing = false; return; }
    frames.push(clock());
    if (frames.length > FRAMES_KEPT) frames.shift();
    requestFrame.call(window, ownFrame);
  }, { __xtmOwn: true });
  const frameUntil = (ms) => {
    if (typeof requestFrame !== 'function') return;
    framesUntil = Math.max(framesUntil, performance.now() + ms);
    if (!framing) { framing = true; requestFrame.call(window, ownFrame); }
  };
  if (typeof requestFrame === 'function') {
    window.requestAnimationFrame = function (callback) {
      if (typeof callback === 'function' && !callback.__xtmOwn) { const t = clock(); painted(t, t); frameUntil(FRAMES_AFTER); }
      return requestFrame.call(window, callback);
    };
  }
  const tick = () => {
    if (performance.now() > until) { ticking = false; return; }
    let dot = document.getElementById('__xtm_paint');
    if (!dot) {
      dot = document.createElement('div');
      dot.id = '__xtm_paint';
      dot.style.cssText = 'position:fixed;left:0;top:0;width:1px;height:1px;pointer-events:none;z-index:2147483647;background:#000;opacity:0.01';
      document.documentElement.appendChild(dot);
    }
    dot.style.opacity = dot.style.opacity === '0.01' ? '0.011' : '0.01';
    setTimeout(tick, EVERY);
  };
  const paintUntil = (ms) => {
    until = Math.max(until, performance.now() + ms + AFTER);
    frameUntil(ms + AFTER);
    if (!ticking) { ticking = true; setTimeout(tick, EVERY); }
  };
  const name = __NAME__;
  const ms = (list) => String(list || '0s').split(',').map((value) => {
    const v = value.trim(), n = parseFloat(v);
    return Number.isFinite(n) ? (v.endsWith('ms') ? n : n * 1000) : 0;
  });
  const nth = (list, i) => list[i % list.length] || 0;
  const transition = (cs, property) => {
    const durations = ms(cs.transitionDuration), delays = ms(cs.transitionDelay);
    return cs.transitionProperty.split(',').reduce((most, p, i) => {
      p = p.trim();
      return !property || p === property || p === 'all' ? Math.max(most, nth(delays, i) + nth(durations, i)) : most;
    }, 0);
  };
  const animation = (cs, wanted) => {
    if (!cs.animationName || cs.animationName === 'none') return 0;
    const durations = ms(cs.animationDuration), delays = ms(cs.animationDelay);
    const counts = cs.animationIterationCount.split(',').map((v) => parseFloat(v));
    return cs.animationName.split(',').reduce((most, n, i) => {
      if (wanted && n.trim() !== wanted) return most;
      const count = nth(counts, i);
      return Math.max(most, nth(delays, i) + nth(durations, i) * (Number.isFinite(count) && count > 0 ? count : 1));
    }, 0);
  };
  const lasting = (el) => { const cs = getComputedStyle(el); return Math.max(transition(cs), animation(cs)); };
  const motion = window.__xtmMotion = {
    // A canvas copied alone that changed (``canvas_alone``) paints the page on the same way.
    paint: (ms) => paintUntil(ms),
    seen: [],
    note(t, el, known) {
      if (!(el instanceof Element) || el.id === '__xtm_paint') return;
      motion.seen.push([t, el, known]);
      if (motion.seen.length > KEPT) motion.seen.shift();
      if (known) { paintUntil(Math.min(known, MOST)); painted(t, t + Math.min(known, MOST)); }
    },
    // The spans the page asked to move over since it was last read, then forgotten.
    painting() { return painting.splice(0, painting.length); },
    // The times of the frames the page's renderer produced since it was last read, then forgotten.
    frames() { return frames.splice(0, frames.length); },
    take(since) {
      const out = [], examined = new Set();
      let read = 0;
      for (const [t, el, known] of motion.seen) {
        if (t < since || !el.isConnected || out.length >= KEPT_OUT) continue;
        let longest = known === null ? 0 : known, mover = el;
        if (known === null) {
          // A changed element is read once a step, its subtree with it: a script changing it again moves nothing more.
          if (examined.has(el)) continue;
          examined.add(el);
          const walker = document.createTreeWalker(el, NodeFilter.SHOW_ELEMENT);
          for (let node = el; node && read < READ; node = walker.nextNode(), read++) {
            const d = lasting(node);
            if (d > longest) { longest = d; mover = node; }
          }
        }
        if (longest > 0) out.push([t, t + Math.min(longest, MOST), name(mover)]);
      }
      motion.seen = [];
      return out;
    },
  };
  document.addEventListener('transitionrun', (e) => {
    if (e.target instanceof Element) motion.note(clock(), e.target, transition(getComputedStyle(e.target), e.propertyName));
  }, { capture: true, passive: true });
  document.addEventListener('animationstart', (e) => {
    if (e.target instanceof Element) motion.note(clock(), e.target, animation(getComputedStyle(e.target), e.animationName));
  }, { capture: true, passive: true });
  new MutationObserver((records) => {
    const t = clock();
    for (const r of records) {
      if (r.type === 'childList') r.addedNodes.forEach((node) => motion.note(t, node, null));
      else motion.note(t, r.target, null);
    }
  }).observe(document, { subtree: true, childList: true, attributes: true });
})()"""
    )
    .replace("__NAME__", _NAME_JS)
    .replace("__MOST__", str(round(MOST_SECONDS * 1000)))
    .replace("__AFTER__", str(round(PAINT_AFTER * 1000)))
)
#: Watches, from the document's start, whether the page still loads (``page_settle``): when its DOM last changed
#: (the recorder's own ``__xtm*`` nodes aside) and, on ``read()``, ``{t, changed, busy, canvases, looping}`` - *t* and
#: *changed* in ms since the epoch (the screencast's clock), *busy* the progress indicators shown (``role=progressbar`` -
#: MUI's LinearProgress and CircularProgress render it -, a ``progress``, ``aria-busy=true``; one with a value only while
#: that value moves), *canvases* ``[name, [x, y, w, h]]`` of the largest canvases shown, in CSS px of the viewport, and
#: *looping* the same of the elements shown running an animation that never ends (a spinner or a skeleton drawn
#: by CSS changes no DOM; ``page_settle`` reads whether it moves on the frame).
_SETTLE_WATCH_JS = (
    """(() => {
  if (window.__xtmSettle || window.top !== window) return;
  const clock = () => performance.timeOrigin + performance.now();
  const name = __NAME__;
  const ours = (node) => {
    for (let n = node; n; n = n.parentNode) if (typeof n.id === 'string' && n.id.startsWith('__xtm')) return true;
    return false;
  };
  const mine = (r) => r.type === 'childList'
    ? r.addedNodes.length + r.removedNodes.length > 0 && [...r.addedNodes, ...r.removedNodes].every(ours)
    : ours(r.target);
  const watch = window.__xtmSettle = { changed: clock(), values: new WeakMap() };
  new MutationObserver((records) => {
    if (records.some((r) => !mine(r))) watch.changed = clock();
  }).observe(document, { subtree: true, childList: true, attributes: true, characterData: true });
  const shown = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width < 1 || r.height < 1 || r.right <= 0 || r.bottom <= 0 || r.left >= innerWidth || r.top >= innerHeight) return null;
    if (el.checkVisibility && !el.checkVisibility({ checkOpacity: true, checkVisibilityCSS: true })) return null;
    return r;
  };
  watch.read = () => {
    const busy = [];
    for (const el of document.querySelectorAll('[role=progressbar], progress, [aria-busy=true]')) {
      if (busy.length >= 6) break;
      if (!shown(el)) continue;
      let value = el.getAttribute('aria-valuenow');
      if (value === null && el instanceof HTMLProgressElement && el.hasAttribute('value')) value = String(el.value);
      if (value !== null && el.getAttribute('aria-busy') !== 'true') {
        const before = watch.values.get(el);
        watch.values.set(el, value);
        if (before === undefined || before === value) continue;
      }
      busy.push(name(el));
    }
    const canvases = [];
    for (const el of document.querySelectorAll('canvas')) {
      const r = shown(el);
      if (!r) continue;
      const left = Math.max(0, r.left), top = Math.max(0, r.top);
      const width = Math.min(innerWidth, r.right) - left, height = Math.min(innerHeight, r.bottom) - top;
      if (width >= __SIDE__ && height >= __SIDE__) canvases.push([name(el), [left, top, width, height]]);
    }
    canvases.sort((a, b) => b[1][2] * b[1][3] - a[1][2] * a[1][3]);
    const looping = [];
    for (const a of document.getAnimations ? document.getAnimations() : []) {
      if (looping.length >= __LOOPING__) break;
      const effect = a.effect;
      if (a.playState !== 'running' || !effect || !effect.getComputedTiming || effect.getComputedTiming().iterations !== Infinity) continue;
      const el = effect.target;
      if (!(el instanceof Element) || ours(el)) continue;
      const r = shown(el);
      if (!r) continue;
      const left = Math.max(0, r.left), top = Math.max(0, r.top);
      const width = Math.min(innerWidth, r.right) - left, height = Math.min(innerHeight, r.bottom) - top;
      const key = name(el);
      if (width >= 2 && height >= 2 && !looping.some((entry) => entry[0] === key)) looping.push([key, [left, top, width, height]]);
    }
    return { t: clock(), changed: watch.changed, busy, canvases: canvases.slice(0, __MOST__), looping };
  };
})()"""
).replace("__NAME__", _NAME_JS).replace("__SIDE__", str(page_settle.MIN_CANVAS_SIDE)).replace("__MOST__", str(page_settle.MOST_CANVASES)).replace(
    "__LOOPING__", str(page_settle.MOST_LOOPING)
)
_SETTLE_READ_JS = "() => window.__xtmSettle ? window.__xtmSettle.read() : null"
#: Whether a clicked element is a tab, or inside one: a ``wait`` after it holds until its page settled.
_TAB_JS = "(el) => !!el.closest('[role=tab]')"
#: What a page shows it is filtered by: the text its fields hold - a search box, an autocomplete - and the
#: parameters of its address. A list page that keeps its search term across navigations lists fewer rows than a later
#: step expects.
_PAGE_FILTERS_JS = """() => {
  const skipped = new Set(['password', 'hidden', 'checkbox', 'radio', 'submit', 'button', 'reset', 'file', 'image', 'range', 'color']);
  const fields = [];
  for (const el of document.querySelectorAll('input, textarea, [role=searchbox]')) {
    if (fields.length >= 6) break;
    if (el instanceof HTMLInputElement && skipped.has(el.type)) continue;
    const value = String('value' in el ? el.value : el.textContent || '').trim();
    const box = el.getBoundingClientRect();
    if (!value || !box.width || !box.height) continue;
    const label = el.getAttribute('aria-label') || el.getAttribute('placeholder')
      || (el.labels && el.labels[0] ? el.labels[0].innerText : '') || el.getAttribute('name') || '';
    fields.push([label.trim().slice(0, 60), value.slice(0, 80)]);
  }
  const query = [...new URLSearchParams(location.search)].slice(0, 8).map(([k, v]) => [k.slice(0, 40), v.slice(0, 120)]);
  return { fields, query };
}"""


class TakeError(RuntimeError):
    """A recording that could not be made; ``str()`` says which step and why."""


class CredentialRefusedError(TakeError):
    """A ``type`` step that would type into a password field: refused before a key is typed."""

    def __init__(self, step: int) -> None:
        from app.services.video_studio.take_preflight import refused_password

        self.step = step
        super().__init__(refused_password(step))


class SrOnlyTargetError(RuntimeError):
    """A step on an element drawn for a screen reader only: fails its step as a miss does, page state said."""


class ProfileNotAuthenticatedError(TakeError):
    """The profile's session does not open the application and could not be renewed: no take is recorded."""

    def __init__(self, probe: Any, detail: str | None = None) -> None:
        from app.services.browser_profiles.signin import session_error_text

        self.probe = probe
        super().__init__(session_error_text(probe, detail))


class ResetSignedOutError(TakeError):
    """The page state reset before step 1 signed the application out: its session passed the pre-flight, so
    the profile's saved session is left as it is and no take is recorded."""

    def __init__(self, probe: Any, cleared: dict) -> None:
        self.probe = probe
        super().__init__(
            f"The page state reset signed the application out: start_url rendered it signed in before the reset, and "
            f"landed on {probe.url or 'nothing'} ({probe.reason}) after it, which {take_state.cleared_words(cleared)}. "
            "The profile's saved session is untouched. Name the storage keys the application keeps its sign-in in as "
            "the profile's keep_storage_keys (save_browser_profile, login_selectors), or record with reset_state "
            "{local_storage: false, session_storage: false}."
        )


class StepFailedError(TakeError):
    """A step that failed the take: when, in seconds of the take, and - once the screencast stopped - the
    frames painted up to there and the steps logged, which the contact sheet of the failed take ends on."""

    def __init__(self, message: str, *, step: int, at: float, label: str) -> None:
        super().__init__(message)
        self.step, self.at, self.label = step, at, label
        self.frames: list[tuple[float, str]] = []
        self.events: list[dict] = []
        #: What the take opened on (``take_state.opened_words``).
        self.opened: dict | None = None
        #: Pictures the step left beside its frames (the canvas a node was looked for on), ``(file name, what
        #: it shows)`` in the take's directory: the contact sheet ends on them.
        self.attached: list[tuple[str, str]] = []
        #: What the take's capture kept up to the failure (``alone``, ``gl``, ``backend``): the capture line a failed
        #: take says too (``canvas_alone.take_capture_words``).
        self.capture: dict = {}


class WorkerBusyError(RuntimeError):
    """A take short of its fps beside other jobs on its worker (``capture_rate.CapturePlan.waits``): nothing
    recorded, nothing refused - it waits in the recording lane (``recording_lane``)."""

    def __init__(self, plan: Any, wanted: float, *, relaunch: bool = False) -> None:
        self.plan, self.wanted, self.relaunch = plan, wanted, relaunch
        if relaunch:
            # A take relaunched after a worker went with it, short of its fps on its new worker (#9812).
            super().__init__(
                f"Short of its {wanted:g} fps on the worker it was relaunched on: waiting for one that sustains it"
            )
            return
        super().__init__(f"Short of its {wanted:g} fps beside other jobs: {plan.beside.busy_words()}")


@dataclass
class CaptureResult:
    workdir: str
    frames: list[tuple[float, str]]
    duration: float
    events: list[dict]
    cursor: list[list[float]]
    viewport: tuple[int, int]
    pixel_ratio: float
    frame_size: tuple[int, int] | None
    storage_state: dict | None
    signed_in: bool | None
    notes: list[str] = field(default_factory=list)
    #: The probe of :attr:`storage_state` (a ``signin.ProbeResult``), so the saved session's status is honest.
    probe: Any = None
    #: How far down the page keeps the layout it was recorded with (:func:`probe_layout`); ``None``: not probed.
    layout: dict | None = None
    #: The step the take's time ran out at: it and the steps after it were not recorded; ``None``: all were.
    truncated_at_step: int | None = None
    #: The window's scroll offset each frame painted, by file name (CSS pixels, the screencast's ``scrollOffsetY``):
    #: what ``scroll_pacing`` paces a window's scroll on.
    painted: dict[str, float] = field(default_factory=dict)
    #: What the recorder says of its capture (``capture_rate``): the GL backend, the pixel ratio asked and
    #: recorded at, the warm-up measured before step 1; ``capture_rate.measure`` adds its rates once paced. Its
    #: ``opened`` (``take_state.opened_words``): the URL it opened on and what its page state reset cleared.
    capture: dict = field(default_factory=dict)
    #: The script as recorded: a ``fps_fallback`` recorded it at another window, pixel ratio or rate.
    script: TakeScript | None = None
    #: Its canvases copied alone as they changed, drawn on software GL (``canvas_alone.Tap``): what
    #: ``canvas_alone.composite`` composites where the screencast fell short.
    alone: list[canvas_alone.AloneFrame] = field(default_factory=list)


#: The screencast frame's metadata that says the geometry it was painted at: a frame whose geometry differs
#: from the take's is painted for something else (a screenshot clipped to a canvas) and never kept.
_FRAME_GEOMETRY = ("deviceWidth", "deviceHeight", "pageScaleFactor", "offsetTop")


def _geometry(metadata: dict) -> tuple:
    """The geometry a screencast frame says it was painted at (:data:`_FRAME_GEOMETRY`), rounded."""
    found = []
    for key in _FRAME_GEOMETRY:
        value = metadata.get(key)
        found.append(round(float(value), 3) if isinstance(value, int | float) else None)
    return tuple(found)


class _Screencast:
    """Chromium's screencast of the page, written to disk as JPEG frames.

    Only frames of the take's geometry are kept: Chromium also hands over, while a screenshot clipped to a
    canvas is taken (``node_locate``), a frame of that clip's size, which the encoder would stretch to the take's - the
    page drawn at another scale for as long as nothing else paints. Once stopped, a frame whose metadata says another
    geometry than most of the take's frames, or whose JPEG is not the size most of them are, is dropped - a hold
    never reads its canvases on one meanwhile. Each one dropped is kept in :attr:`dropped` (its instant, why)."""

    def __init__(self, cdp: Any, workdir: str, *, max_frames: int) -> None:
        self._cdp = cdp
        self._dir = workdir
        self._max = max_frames
        self.t_start = 0.0
        self.frames: list[tuple[float, str]] = []
        #: The window's scroll offset each frame painted (CSS pixels), by file name.
        self.offsets: dict[str, float] = {}
        self.frame_size: tuple[int, int] | None = None
        #: The frame last painted (its instant, its JPEG in base64): what a hold reads its canvases on.
        self.latest: tuple[float, str] | None = None
        #: The frames dropped as painted at another geometry: ``(seconds of the take, why)``.
        self.dropped: list[tuple[float, str]] = []
        #: The geometry the first frame said it was painted at; each frame's, and its JPEG's size, by file name.
        self._geometry: tuple | None = None
        self._geometries: dict[str, tuple] = {}
        self._sizes: dict[str, tuple[int, int]] = {}
        self._writes: set[asyncio.Future] = set()
        self._loop = asyncio.get_running_loop()
        self._stopped = False

    def _on_frame(self, event: dict) -> None:
        session_id = event.get("sessionId")
        self._loop.create_task(self._ack(session_id))
        if len(self.frames) >= self._max:
            return
        metadata = event.get("metadata") or {}
        stamp = float(metadata.get("timestamp") or time.time())
        t = max(0.0, stamp - self.t_start)
        name = f"f{len(self.frames):06d}.jpg"
        self.frames.append((t, name))
        geometry = _geometry(metadata)
        self._geometries[name] = geometry
        if self._geometry is None:
            self._geometry = geometry
        if isinstance(metadata.get("scrollOffsetY"), int | float):
            self.offsets[name] = float(metadata["scrollOffsetY"])
        data = event.get("data") or ""
        if geometry == self._geometry:
            self.latest = (t, data)
        future = self._loop.run_in_executor(None, self._write, name, data)
        self._writes.add(future)
        future.add_done_callback(self._writes.discard)

    def _write(self, name: str, data: str) -> None:
        from PIL import Image

        raw = base64.b64decode(data)
        with open(os.path.join(self._dir, name), "wb") as handle:
            handle.write(raw)
        try:
            # The JPEG's header alone: what :meth:`stop` keeps a frame by.
            with Image.open(os.path.join(self._dir, name)) as image:
                self._sizes[name] = image.size
        except Exception as exc:  # a frame whose size is unread is kept as painted
            logger.debug("Screencast frame size unread: %s", format_exception_message(exc))

    async def _ack(self, session_id: Any) -> None:
        with contextlib.suppress(Exception):  # the page may be navigating
            await self._cdp.send("Page.screencastFrameAck", {"sessionId": session_id})

    async def frame(self) -> tuple[float, bytes, float] | None:
        """The frame last painted: its instant, its JPEG and how long after the one before it; ``None``: none yet."""
        if self.latest is None:
            return None
        t, data = self.latest
        gap = self.frames[-1][0] - self.frames[-2][0] if len(self.frames) > 1 else 0.0
        return t, base64.b64decode(data), gap

    async def start(self, width: int, height: int) -> None:
        self._cdp.on("Page.screencastFrame", self._on_frame)
        self.t_start = time.time()
        await self._cdp.send(
            "Page.startScreencast",
            {
                "format": "jpeg",
                "quality": _SCREENCAST_QUALITY,
                "maxWidth": width,
                "maxHeight": height,
                "everyNthFrame": 1,
            },
        )

    async def stop(self) -> None:
        if not self._stopped:
            self._stopped = True
            with contextlib.suppress(Exception):  # the page may already be gone
                await self._cdp.send("Page.stopScreencast")
        if self._writes:
            await asyncio.gather(*list(self._writes), return_exceptions=True)
        self.frames.sort()
        self._keep_take_size()

    def _keep_take_size(self) -> None:
        """Drop the frames painted at another geometry or another JPEG size than most of the take's, and say
        the take's size."""
        geometries = collections.Counter(self._geometries.values())
        sizes = collections.Counter(self._sizes.values())
        geometry = max(geometries, key=lambda found: (geometries[found], str(found))) if geometries else None
        size = max(sizes, key=lambda found: (sizes[found], found)) if sizes else None
        if size is not None:
            self.frame_size = size
        kept: list[tuple[float, str]] = []
        for t, name in self.frames:
            why = _geometry_change(self._geometries.get(name), geometry) or _size_change(self._sizes.get(name), size)
            if why:
                self.dropped.append((t, why))
                self.offsets.pop(name, None)
                continue
            kept.append((t, name))
        self.frames = kept
        self.dropped.sort()


def _geometry_change(found: tuple | None, take: tuple | None) -> str:
    """How a frame's geometry differs from the take's (``pageScaleFactor 1.4 for 1``); empty: it does not."""
    if found is None or take is None or found == take:
        return ""
    said = ", ".join(
        f"{key} {now:g} for {was:g}"
        for key, now, was in zip(_FRAME_GEOMETRY, found, take, strict=True)
        if now is not None and was is not None and now != was
    )
    return said or "another geometry"


def _size_change(found: tuple[int, int] | None, take: tuple[int, int] | None) -> str:
    """How a frame's JPEG size differs from the take's (``3018x1276 px for 3200x1800``); empty: it does not."""
    if found is None or take is None or found == take:
        return ""
    return f"{found[0]}x{found[1]} px for {take[0]}x{take[1]}"


#: The controls a visually hidden element may be part of: a click that lands inside the one it sits in - an
#: icon button around its hidden name, a toggle's label around its hidden input - reaches what the step names.
_SR_ONLY_CONTROLS = (
    "a[href], button, label, summary, [role=button], [role=link], [role=tab], [role=menuitem], [role=checkbox], "
    "[role=switch], [role=radio], [role=option]"
)
#: Whether a step's element is drawn for a screen reader only: itself or an ancestor clipped to nothing (the
#: ``sr-only`` recipe: 1 px, clip rect(0 0 0 0) or clip-path inset(50%), overflow hidden), and a click at its point
#: lands outside it, its labels and the control it sits in; then the role it has, its text before the first comma (a
#: graph's entry reads "type title, N relationships": the label a node step names), the label of its listbox,
#: what the click lands on and whether the page draws a canvas. ``null`` for an element a person sees, or one a click
#: at its point reaches all the same (a radio button's input clipped under the label drawn as its button).
_SR_ONLY_JS = """(el) => {
  const clipped = (node) => {
    const cs = getComputedStyle(node);
    const r = node.getBoundingClientRect();
    const tiny = r.width <= 1 && r.height <= 1 && cs.overflow === 'hidden';
    const clip = /rect\\(0(px)?,? 0(px)?,? 0(px)?,? 0(px)?\\)/.test(cs.clip || '');
    const path = /inset\\(\\s*(50|100)%/.test(cs.clipPath || '');
    return tiny || clip || path;
  };
  const named = (node) => node.getAttribute('role') || node.tagName.toLowerCase();
  let node = el;
  let listbox = null;
  for (let depth = 0; node && node !== document.body && depth < 12; depth += 1) {
    if (!listbox && node.getAttribute && node.getAttribute('role') === 'listbox') listbox = node;
    if (clipped(node)) {
      const r = el.getBoundingClientRect();
      const root = el.getRootNode();
      const hit = (root.elementFromPoint ? root : document).elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
      const owners = [el, ...(el.labels || []), el.closest(__CONTROLS__)].filter(Boolean);
      if (hit && owners.some((owner) => owner.contains(hit))) return null;
      const text = (el.getAttribute('aria-label') || el.textContent || '').replace(/\\s+/g, ' ').trim();
      return { role: named(el), name: text.split(',')[0].trim().slice(0, 200) || null,
        listbox: (listbox && (listbox.getAttribute('aria-label') || '')) || null,
        lands: hit ? named(hit) : null,
        canvas: !!document.querySelector('canvas') };
    }
    node = node.parentElement;
  }
  return null;
}""".replace("__CONTROLS__", json.dumps(_SR_ONLY_CONTROLS))
#: The characters of a hidden list's label, and of a role, a refusal quotes: a step's message keeps its first 300, the
#: advice after them included.
SR_ONLY_LABEL_CHARS = 40
SR_ONLY_ROLE_CHARS = 20
#: The characters of the node label a refusal names: a longer one is cut, which a node step still reads as the
#: beginning of its entry (``node_locate.match_label``).
SR_ONLY_NAME_CHARS = 30


def sr_only_words(hidden: dict) -> str:
    """Why a step on a visually hidden element is refused, and what to act on instead (one line): on a page
    drawing a canvas, the node step reading that entry."""
    role = str(hidden.get("role") or "element")[:SR_ONLY_ROLE_CHARS]
    listbox = str(hidden.get("listbox") or "")[:SR_ONLY_LABEL_CHARS]
    where = f" of the list {listbox!r}" if listbox else ""
    lands = f"on the {str(hidden['lands'])[:SR_ONLY_ROLE_CHARS]}" if hidden.get("lands") else "outside the window"
    said = f"the {role}{where} is drawn for a screen reader only: a click at its point lands {lands}, not on it"
    if hidden.get("canvas"):
        name = " ".join(str(hidden.get("name") or "").split())[:SR_ONLY_NAME_CHARS].strip()
        node = f"node {{label: {name!r}}}" if name else "node {label}"
        said += f"; if it mirrors the canvas, act on {node} or a point (at {{x, y, in: 'canvas'}})"
    return said


def _locator(page: Any, step: TakeStep) -> Any:
    if step.role:
        locator = page.get_by_role(step.role, name=step.name) if step.name else page.get_by_role(step.role)
    elif step.selector:
        locator = page.locator(step.selector)
    else:
        locator = page.get_by_text(step.text or "", exact=False)
    return locator.nth(step.nth)


#: The roles an element a ``role`` + ``name`` step names may have besides that role, tried after it: a menu
#: entry that toggles is a ``menuitemcheckbox``, a switch is a checkbox drawn as one, a picker's entry an ``option`` or
#: a ``treeitem`` - the same control to the person watching.
ROLE_FAMILIES = {
    "menuitem": ("menuitemcheckbox", "menuitemradio"),
    "menuitemcheckbox": ("menuitem", "menuitemradio"),
    "menuitemradio": ("menuitem", "menuitemcheckbox"),
    "checkbox": ("switch",),
    "switch": ("checkbox",),
    "option": ("treeitem",),
    "treeitem": ("option",),
}
#: Roles tried for another only by its exact name, and only when one element has it: a ``button`` step may click the
#: menu entry a menu draws as one, never one of several entries sharing a word with it.
UNIQUE_ROLE_FAMILIES = {"button": ("menuitem",)}
#: Seconds between two reads of the page while a step's role or its family is looked for.
_ROLE_POLL_SECONDS = 0.15


def role_candidates(role: str) -> list[tuple[str, bool]]:
    """The roles a step naming *role* matches, in the order tried: ``(role, by its exact and unique name only)``."""
    return [
        (role, False),
        *((other, False) for other in ROLE_FAMILIES.get(role, ())),
        *((other, True) for other in UNIQUE_ROLE_FAMILIES.get(role, ())),
    ]


def _ease(t: float) -> float:
    return 4 * t * t * t if t < 0.5 else 1 - (-2 * t + 2) ** 3 / 2


def drives_pointer(step: TakeStep) -> bool:
    """Whether a pointer step plays as one (``_Recorder._pointer_step``): on a point or a node, a right-click, a
    drag or a wheel, or with keys held or a rest asked for; a plain click, double-click or hover on an element plays as
    it always did."""
    return step.on_canvas or step.action in ("right_click", "drag", "wheel") or bool(step.modifiers) or step.hover_ms is not None


def scroll_seconds(pixels: float, pace: str) -> float:
    """How long a scroll by *pixels* takes at *pace* when its step says nothing."""
    low, high = _SCROLL_SECONDS
    return min(high, max(low, abs(pixels) / _SCROLL_SPEED.get(pace, _SCROLL_SPEED["natural"])))


def wheel_deltas(pixels: int, seconds: float, fps: int) -> list[int]:
    """The turns of a scroll by *pixels* over *seconds* on a take recorded at *fps*: one a frame, each the same
    distance to the pixel - 1200 px over 2 s is 120 turns of 10 px at 60 fps, 60 of 20 px at 30 -, so the page moves
    a near-constant distance between two painted frames instead of a few large steps and holds."""
    count = max(1, round(seconds * fps))
    return [round(pixels * (i + 1) / count) - round(pixels * i / count) for i in range(count)]


#: The frame a page last painted, for its canvases' pixels: its instant, its JPEG and how long after the one
#: before it; ``None`` when none is in hand.
FrameSource = Callable[[], Awaitable[tuple[float, bytes, float] | None]]


async def _read_settle(page: Any, *, origin: float, at: float) -> page_settle.PageRead:
    """One read of *page*'s settle watch, installed first in a document that has none."""
    try:
        raw = await page.evaluate(_SETTLE_READ_JS)
        if raw is None:
            await page.evaluate(_SETTLE_WATCH_JS)
            raw = await page.evaluate(_SETTLE_READ_JS)
    except Exception as exc:  # navigating: read as a page changing
        logger.debug("Settle watch not read: %s", format_exception_message(exc))
        raw = None
    return page_settle.page_read(raw, origin=origin, at=at)


async def watch_page(
    page: Any,
    watch: page_settle.PageWatch,
    *,
    origin: float,
    viewport_width: int,
    frame: FrameSource | None,
    seconds: float,
    extra: float = 0.0,
) -> tuple[float | None, float]:
    """Watch *page* (``page_settle``) every ``SAMPLE_SECONDS`` for *seconds* and, given *extra*, past them until
    it settled, at most *extra* more; its canvases are read on *frame*. ``(when it settled, when the watch ended)`` in
    seconds since *origin* (seconds since the epoch); settled ``None``: not, or not asked."""

    def now() -> float:
        return time.time() - origin

    began = time.monotonic()
    end, limit = began + seconds, began + seconds + extra
    # The frame last decoded and the boxes read on it: a page that painted nothing since is not decoded again.
    decoded: tuple[float, tuple, dict] | None = None
    while True:
        read = await _read_settle(page, origin=origin, at=now())
        crops, painted, gap = None, None, 0.0
        boxes = read.boxes()
        if boxes and frame is not None and (latest := await frame()) is not None:
            painted, jpeg, gap = latest
            if decoded is not None and decoded[:2] == (painted, boxes):
                crops = decoded[2]
            else:
                try:
                    crops = await asyncio.to_thread(page_settle.canvas_crops, jpeg, viewport_width, boxes)
                    decoded = (painted, boxes, crops)
                except Exception as exc:  # a frame that will not decode: its canvases go unread this time
                    logger.debug("Frame not read for the page's canvases: %s", format_exception_message(exc))
        watch.observe(read, crops, frame_t=painted, gap=gap)
        at = time.monotonic()
        settled = watch.settled_at(now()) if extra else None
        if at >= end and (not extra or settled is not None or at >= limit):
            ended = now()
            watch.close(ended)
            return settled, ended
        await asyncio.sleep(max(0.0, min(page_settle.SAMPLE_SECONDS, (end if at < end else limit) - at)))


class _Recorder:
    def __init__(
        self,
        rb: ProfileBrowser,
        script: TakeScript,
        *,
        viewport: tuple[int, int],
        max_seconds: float,
        keep: tuple[str, ...] = (),
    ) -> None:
        self.rb = rb
        self.page = rb.page
        self.script = script
        #: The storage keys a ``goto``'s page state reset keeps: the profile's session's.
        self.keep = keep
        self.pace = _PACE[script.pace]
        self.events: list[dict] = []
        self.cursor: list[list[float]] = []
        self.viewport = viewport
        vw, vh = viewport
        self.x, self.y = vw * 0.6, vh * 0.62
        self.deadline = 0.0
        self.max_seconds = max_seconds
        self.t_start = 0.0
        #: The page's URL when the current step started.
        self.step_url: str | None = None
        #: The scrolls the current step made, logged (:meth:`scroll_log`): its event takes them.
        self.scrolls: list[dict] = []
        #: The role the current step's element matched, when it is one of its role's family (:func:`role_candidates`).
        self.matched_role: str | None = None
        #: The screencast: the frame a hold reads its page's canvases on.
        self.screen: _Screencast | None = None
        #: Whether the page's canvases draw on the worker's GPU (``capture_rate.GlBackend.gpu``): a wheel over a canvas
        #: keeps its copies only there (#10897); on software GL the zoom is paced from the page's frames.
        self.gpu = False
        #: Where the page's card or menu named a node under the cursor, by label: ``(point, step)``. A later step on
        #: the same node tries that place first (``pointer.remembered``); forgotten on a step of :data:`_VIEW_MOVERS`
        #: or a page changed.
        self.confirmed: dict[str, tuple[tuple[float, float], int]] = {}

    def now(self) -> float:
        return time.time() - self.t_start

    async def _hold(self, step: TakeStep, seconds: float, began: float) -> dict:
        """Hold the page *seconds* from *began*, watching whether it still loads (``page_settle``): a ``wait``
        held until its page settled (``page_settle.until_settled``) runs on past them until it did, at most its
        ``timeout`` more. Its event's ``t1``, the spans it showed loading and, held until settled, when it settled."""
        settle = page_settle.until_settled(step, self.events)
        extra = (step.timeout if step.timeout is not None else page_settle.TIMEOUT_SECONDS) if settle else 0.0
        watch = page_settle.PageWatch(began=began)
        settled, ended = await watch_page(
            self.page,
            watch,
            origin=self.t_start,
            viewport_width=self.viewport[0],
            frame=self.screen.frame if self.screen is not None else None,
            seconds=seconds,
            extra=extra,
        )
        return {"t1": ended, **watch.logged(until_settled=settle, settled=settled, seconds=seconds, ended=ended)}

    async def log_canvases(self, index: int) -> None:
        """The canvases step *index* ended on, on its event: ``canvases``, ``[name, [x, y, w, h]]`` in CSS px
        of the viewport, the largest few - what ``capture_rate.canvas_motion`` reads on the frames of its span."""
        if not self.events or self.events[-1].get("step") != index:
            return
        read = await _read_settle(self.page, origin=self.t_start, at=self.now())
        if read.canvases:
            self.events[-1]["canvases"] = [[name, [round(v, 1) for v in box]] for name, box in read.canvases]

    @contextlib.asynccontextmanager
    async def scroll_log(self, *, at: tuple[float, float] | None = None, locator: Any = None):
        """Log where the page's scrollers stand at ``scroll_pacing.SCROLL_SAMPLE_HZ`` while the block runs:
        the window and the scroll containers under the cursor at *at* or holding *locator*'s element - each named as
        the page names it -, from before it moves to after it settles: what ``scroll_pacing.pace`` draws the
        scroll again at its rate from. It yields what the block adds to the log (the canvas under the wheel; the
        wheel's turns)."""
        from app.services.video_studio.scroll_pacing import SCROLL_SAMPLE_HZ

        page = self.page
        samples: list[list] = []
        added: dict[str, Any] = {}
        try:
            await page.evaluate(_SCROLL_WATCH_JS)
            if at is not None:
                await page.evaluate("([x, y]) => window.__xtmScroll.at(x, y)", [at[0], at[1]])
            if locator is not None:
                await locator.evaluate("(el) => window.__xtmScroll.holding(el)")
        except Exception as exc:  # a page that will not run it scrolls as before, its frames as painted
            logger.debug("Scroll watch not installed: %s", format_exception_message(exc))
            yield added
            return

        async def sample() -> bool:
            try:
                read = await page.evaluate("() => window.__xtmScroll.read()")
            except Exception:  # navigating: the log stops where it is
                return False
            if isinstance(read, list) and len(read) >= 4:
                samples.append([round(float(read[0]) / 1000 - self.t_start, 4), *read[1:]])
            return True

        stop = asyncio.Event()

        async def run() -> None:
            while not stop.is_set():
                began = time.monotonic()
                if not await sample():
                    return
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), max(0.0, 1 / SCROLL_SAMPLE_HZ - (time.monotonic() - began)))

        async def fixed_boxes() -> list[list[Any]] | None:
            try:
                read = await page.evaluate(_FIXED_BOXES_JS)
            except Exception:  # navigating: the bands go unchecked
                return None
            if not isinstance(read, list):
                return None
            return [[round(float(v), 1) for v in box[:4]] + [str(box[4])] for box in read if isinstance(box, list) and len(box) == 5]

        started = self.now()
        await sample()
        # The page's own fixed and sticky elements, before it moves and once it settled: the only places a band
        # the pacing finds is held.
        before = await fixed_boxes()
        task = asyncio.create_task(run())
        try:
            yield added
        finally:
            stop.set()
            await task
            await sample()
            logged: dict[str, Any] = {"t": [round(started, 4), round(self.now(), 4)], "samples": samples, **added}
            after = await fixed_boxes()
            # Only a whole read on both sides vouches for a band; one side unknown leaves the bands unchecked.
            if before is not None and after is not None:
                logged["fixed"] = before + after
            # How the page names each container it watched: the log says which one scrolled.
            with contextlib.suppress(Exception):  # navigated away: its containers go unnamed
                if names := await page.evaluate("() => window.__xtmScroll ? window.__xtmScroll.names() : {}"):
                    logged["names"] = names
            self.scrolls.append(logged)

    async def locate(self, step: TakeStep) -> tuple[Any, float]:
        """The element *step* acts on and the seconds left of its wait.

        A ``role`` + ``name`` step whose role has a family (:func:`role_candidates`)
        is looked for under its own role first, then under each of its family's,
        until one has the element; the role it matched is logged
        (:attr:`matched_role`). Any other step, and one found under no role,
        waits on its own locator as before.
        """
        wait = step.wait_seconds
        candidates = role_candidates(step.role) if step.role and step.name else []
        if len(candidates) < 2:
            return _locator(self.page, step), wait
        deadline = time.monotonic() + wait
        while True:
            for role, unique in candidates:
                found = self.page.get_by_role(role, name=step.name, exact=True if unique else None)
                count = await found.count()
                if count > step.nth and (not unique or count == 1):
                    if role != step.role:
                        self.matched_role = role
                    return found.nth(step.nth), max(0.5, deadline - time.monotonic())
            if time.monotonic() >= deadline:
                return _locator(self.page, step), 0.5
            await asyncio.sleep(_ROLE_POLL_SECONDS)

    async def move_to(self, x: float, y: float) -> None:
        dx, dy = x - self.x, y - self.y
        distance = math.hypot(dx, dy)
        if distance < 2:
            return
        base, per_k, cap = self.pace
        seconds = min(cap, base + per_k * distance / 1000)
        steps = max(6, int(seconds / _CURSOR_STEP_SECONDS))
        # A gentle arc: the control point is pushed perpendicular to the path.
        bend = min(0.12 * distance, 90.0)
        nx, ny = (-dy / distance, dx / distance) if distance else (0.0, 0.0)
        cx, cy = self.x + dx / 2 + nx * bend, self.y + dy / 2 + ny * bend
        sx, sy = self.x, self.y
        started = time.time()
        for i in range(1, steps + 1):
            t = _ease(i / steps)
            px = (1 - t) ** 2 * sx + 2 * (1 - t) * t * cx + t * t * x
            py = (1 - t) ** 2 * sy + 2 * (1 - t) * t * cy + t * t * y
            await self.page.mouse.move(px, py)
            self.x, self.y = px, py
            self.cursor.append([round(self.now(), 4), round(px, 1), round(py, 1)])
            target = started + seconds * i / steps
            delay = target - time.time()
            if delay > 0:
                await asyncio.sleep(delay)

    async def _box(self, locator: Any, timeout: float) -> tuple[dict, list[float] | None]:
        """The element's settled box, and ``[start, end]`` of the scroll that brought it into view (else ``None``)."""
        await locator.wait_for(state="visible", timeout=timeout * 1000)
        scrolled = await self._scroll_into_view(locator)
        box = None
        for _ in range(6):
            first = await locator.bounding_box()
            await asyncio.sleep(0.08)
            second = await locator.bounding_box()
            if first and second and abs(first["x"] - second["x"]) < 1 and abs(first["y"] - second["y"]) < 1:
                box = second
                break
        box = box or await locator.bounding_box()
        if not box:
            raise TakeError("the element has no size on the page")
        if hidden := await _evaluate(locator, _SR_ONLY_JS, None):
            # An entry only a screen reader reaches: a graph canvas mirrors every node in a visually hidden
            # listbox, and a click at its 1 px option's point lands off the canvas, so the menu it should open never
            # does.
            raise SrOnlyTargetError(sr_only_words(hidden))
        return {k: round(float(box[k]), 1) for k in ("x", "y", "width", "height")}, scrolled

    async def _scroll_into_view(self, locator: Any) -> list[float] | None:
        inside = await locator.evaluate(
            "(el) => { const r = el.getBoundingClientRect();"
            " return r.top >= 0 && r.bottom <= window.innerHeight && r.left >= 0 && r.right <= window.innerWidth; }"
        )
        if inside:
            return None
        started = self.now()
        async with self.scroll_log(locator=locator):
            await locator.evaluate("(el) => el.scrollIntoView({behavior: 'smooth', block: 'center', inline: 'nearest'})")
            await self._wait_scroll_end()
        return [round(started, 4), round(self.now(), 4)]

    async def _wait_scroll_end(self) -> None:
        last = None
        for _ in range(40):
            position = await self.page.evaluate(
                "() => { const s = document.scrollingElement || document.documentElement;"
                " let inner = 0; for (const el of document.querySelectorAll('*')) { if (el.scrollTop) inner += el.scrollTop; if (inner > 1e9) break; }"
                " return [window.scrollX, window.scrollY, s.scrollTop, inner]; }"
            )
            if position == last:
                return
            last = position
            await asyncio.sleep(0.08)

    async def settle(self) -> None:
        with contextlib.suppress(Exception):  # read the page as it is
            await self.page.wait_for_load_state("domcontentloaded", timeout=10_000)
        with contextlib.suppress(Exception):  # long-polling apps never go idle
            await self.page.wait_for_load_state("networkidle", timeout=1500)
        if self.script.settle_ms:
            await asyncio.sleep(self.script.settle_ms / 1000)

    def _event(self, index: int, step: TakeStep, **values: Any) -> dict:
        event = {
            "step": index,
            "action": step.action,
            "label": step_label(step),
            "ok": True,
            "url": self.page.url,
        }
        if self.step_url:
            event["url_before"] = self.step_url
        if step.zoom is not None:
            event["zoom"] = step.zoom
        if self.scrolls:
            event["scrolls"], self.scrolls = self.scrolls, []
        if self.matched_role:
            event["matched_role"] = self.matched_role
        event.update(values)
        if isinstance(event.get("label"), str):
            event["label"] = event["label"][:200]
        return event

    async def _motion_clock(self) -> float | None:
        """The page's clock (ms since the epoch) once its motion watch is installed, ``None`` when it cannot run."""
        try:
            await self.page.evaluate(_MOTION_WATCH_JS)
            return float(await self.page.evaluate("() => performance.timeOrigin + performance.now()"))
        except Exception as exc:  # a page that will not run it keeps its frames as painted
            logger.debug("Motion watch not installed: %s", format_exception_message(exc))
            return None

    async def motion_since(self, since: float) -> list[list]:
        """What moved on the page since *since* (its clock, ms) as a step changed it (:data:`_MOTION_WATCH_JS`):
        ``[start, end, element]`` in seconds of the take - what ``transition_pacing`` draws again at the take's rate."""
        try:
            read = await self.page.evaluate("(since) => window.__xtmMotion ? window.__xtmMotion.take(since) : []", since)
        except Exception as exc:  # navigating: what it noted goes with the page
            logger.debug("Motion not read: %s", format_exception_message(exc))
            return []
        return [
            [
                round(float(start) / 1000 - self.t_start, 4),
                round(float(end) / 1000 - self.t_start, 4),
                str(name or "")[:120],
            ]
            for start, end, name in (entry for entry in read or [] if isinstance(entry, list) and len(entry) == 3)
        ]

    async def painting(self) -> list[list[float]] | None:
        """The spans the page asked to move over since the last read (:data:`_MOTION_WATCH_JS`: the animation frames
        it requested, the transitions and animations that ran), ``[start, end]`` in seconds of the take - what the insert
        capture floor tells a starved capture from an idle page by (``insert_floor``, #10101). ``None``: not read."""
        try:
            read = await self.page.evaluate(
                "() => window.__xtmMotion && window.__xtmMotion.painting ? window.__xtmMotion.painting() : null"
            )
        except Exception as exc:  # navigating: what it noted goes with the page
            logger.debug("Painting not read: %s", format_exception_message(exc))
            return None
        if not isinstance(read, list):
            return None
        spans = [
            [round(float(span[0]) / 1000 - self.t_start, 3), round(float(span[1]) / 1000 - self.t_start, 3)]
            for span in read
            if isinstance(span, list) and len(span) == 2
        ]
        return spans[-MOST_PAINTING_SPANS:]

    async def page_frames(self) -> list[float] | None:
        """The times the page's renderer produced a frame at since the last read, in seconds of the take
        (:data:`_MOTION_WATCH_JS`: an animation frame loop of its own while the page moves): the rate the page itself
        drew a span at, told from the rate the screencast painted it at (``capture_rate.mark_page_limited``, #10970).
        ``None``: not read."""
        try:
            read = await self.page.evaluate(
                "() => window.__xtmMotion && window.__xtmMotion.frames ? window.__xtmMotion.frames() : null"
            )
        except Exception as exc:  # navigating: what it counted goes with the page
            logger.debug("Page frames not read: %s", format_exception_message(exc))
            return None
        if not isinstance(read, list):
            return None
        times = [round(float(t) / 1000 - self.t_start, 4) for t in read if isinstance(t, int | float)]
        return times[-MOST_PAGE_FRAMES:]

    async def run_step(self, index: int, step: TakeStep) -> None:
        """Play *step*; one that changes the page (:data:`MOTION_ACTIONS`) keeps the windows its transitions and
        animations move over (``motion``). Every step keeps the spans the page asked to move over (``painting``,
        :meth:`painting`). Its ``url`` is where the page is once it played (:func:`current_url`)."""
        since = await self._motion_clock() if step.action in MOTION_ACTIONS else None
        await self._play(index, step)
        if self.events and self.events[-1].get("step") == index:
            self.events[-1]["url"] = await current_url(self.page)
        if (
            since is not None
            and self.events
            and self.events[-1].get("step") == index
            and (moved := await self.motion_since(since))
        ):
            self.events[-1]["motion"] = moved
        if self.events and self.events[-1].get("step") == index and (spans := await self.painting()) is not None:
            self.events[-1]["painting"] = spans
        if self.events and self.events[-1].get("step") == index and (drawn := await self.page_frames()) is not None:
            self.events[-1]["page_frames"] = drawn

    async def _play(self, index: int, step: TakeStep) -> None:
        page = self.page
        action = step.action
        t0 = self.now()
        url_before, self.step_url = self.step_url, await current_url(page)
        self.matched_role = None
        if action in _VIEW_MOVERS or (url_before is not None and url_before != self.step_url):
            self.confirmed.clear()
        if action == "goto":
            # A goto opens its URL as given; with reset_state its page starts from a clean state.
            reset = page_reset(step.reset_state)
            logged = {}
            if reset is None:
                await self.rb.goto(step.url or "")
            else:
                logged["reset"] = await open_cleared(self.rb, step.url or "", storage_reset(reset, self.keep))
            await self.settle()
            self.events.append(self._event(index, step, t0=t0, ta=t0, t1=self.now(), url=page.url, **logged))
            return
        if action in ("wait", "pause"):
            held = await self._hold(step, float(step.seconds or 0), t0)
            self.events.append(self._event(index, step, t0=t0, ta=t0, **held))
            return
        if action == "marker":
            self.events.append(self._event(index, step, t0=t0, ta=t0, t1=t0))
            return
        if action == "press":
            await page.keyboard.press(step.key or "Enter")
            ta = self.now()
            await self.settle()
            self.events.append(self._event(index, step, t0=t0, ta=ta, t1=self.now()))
            return
        if action == "scroll" and not (step.selector or step.role or step.text):
            await self._scroll(step)
            self.events.append(self._event(index, step, t0=t0, ta=t0, t1=self.now()))
            return
        if action == "type" and not (step.selector or step.role):
            if await _evaluate(page, _FOCUSED_HIDDEN_INPUT_JS, False):
                raise CredentialRefusedError(index)
            await page.keyboard.type(step.text or "", delay=step.delay_ms or TYPING_DELAY_MS[self.script.pace])
            if step.submit:
                await page.keyboard.press("Enter")
            t1 = self.now()
            await self.settle()
            self.events.append(self._event(index, step, t0=t0, ta=t0, t1=t1))
            return
        if action in POINTER_ACTIONS and drives_pointer(step):
            await self._pointer_step(index, step)
            return

        locator, wait = await self.locate(step)
        if action == "wait_for":
            await locator.wait_for(state="visible", timeout=wait * 1000)
            box = await locator.bounding_box()
            self.events.append(self._event(index, step, t0=t0, ta=self.now(), t1=self.now(), box=_box_payload(box)))
            return
        box, scrolled = await self._box(locator, wait)
        if action == "type" and await _is_password_field(locator):
            raise CredentialRefusedError(index)
        moved = {"scrolled": scrolled} if scrolled else {}
        point_x = box["x"] + box["width"] / 2
        point_y = box["y"] + box["height"] / 2
        if action == "scroll":
            self.events.append(self._event(index, step, t0=t0, ta=self.now(), t1=self.now(), box=_box_payload(box)))
            return
        if action == "highlight":
            shown = self.now()
            held = await self._hold(step, step.seconds if step.seconds is not None else 2.0, shown)
            self.events.append(self._event(index, step, t0=t0, ta=shown, box=_box_payload(box), **moved, **held))
            return
        if action == "type":
            # Aim at the start of a wide field, where a person clicks to type.
            point_x = box["x"] + min(box["width"] / 2, 24.0 + box["width"] * 0.1)
        move_start = self.now()
        await self.move_to(point_x, point_y)
        if action == "hover":
            await asyncio.sleep(0.25)
            ta = self.now()
            await self.settle()
            self.events.append(
                self._event(
                    index,
                    step,
                    t0=move_start,
                    ta=ta,
                    t1=self.now(),
                    box=_box_payload(box),
                    point={"x": point_x, "y": point_y},
                    **moved,
                )
            )
            return
        if action in ("click", "double_click"):
            # A tab switched: the wait after it holds until its page settled.
            tab = {"tab": True} if await _evaluate(locator, _TAB_JS, False) else {}
            ta = self.now()
            await page.mouse.click(point_x, point_y, click_count=2 if action == "double_click" else 1, delay=70)
            t1 = self.now()
            await self.settle()
            self.events.append(
                self._event(
                    index,
                    step,
                    t0=move_start,
                    ta=ta,
                    t1=t1,
                    box=_box_payload(box),
                    point={"x": point_x, "y": point_y},
                    **tab,
                    **moved,
                )
            )
            return
        if action == "select" and not await locator.evaluate("(el) => el.tagName === 'SELECT'"):
            # An ARIA combobox, or a button opening a list: opened, its option picked by name.
            await self._pick_option(index, step, locator, box, (point_x, point_y), move_start, moved)
            return
        if action == "select":
            ta = self.now()
            await page.mouse.click(point_x, point_y, delay=60)
            await locator.select_option(step.value)
            t1 = self.now()
            await self.settle()
            self.events.append(
                self._event(
                    index,
                    step,
                    t0=move_start,
                    ta=ta,
                    t1=t1,
                    box=_box_payload(box),
                    point={"x": point_x, "y": point_y},
                    **moved,
                )
            )
            return
        if action == "type":
            ta = self.now()
            await page.mouse.click(point_x, point_y, delay=50)
            if step.clear:
                await locator.fill("")
            secret = await _is_password_field(locator)
            await asyncio.sleep(0.15)
            await page.keyboard.type(step.text or "", delay=step.delay_ms or TYPING_DELAY_MS[self.script.pace])
            if step.submit:
                await asyncio.sleep(0.2)
                await page.keyboard.press("Enter")
            t1 = self.now()
            await self.settle()
            self.events.append(
                self._event(
                    index,
                    step,
                    t0=move_start,
                    ta=ta,
                    t1=t1,
                    box=_box_payload(box),
                    point={"x": point_x, "y": point_y},
                    **({} if secret else {"text": (step.text or "")[:200]}),
                    **moved,
                )
            )
            return
        raise TakeError(f"unknown action {action}")

    async def _pick_option(
        self,
        index: int,
        step: TakeStep,
        control: Any,
        box: dict,
        point: tuple[float, float],
        move_start: float,
        moved: dict,
    ) -> None:
        """Open *control* and click the option named ``step.value`` in the list it opens.

        The option is found by its accessible name (:func:`find_choice`):
        exact first, then case-insensitive. An editable combobox whose list
        does not show it has the value typed first, as a person narrows a
        list; one that still shows nothing is opened with ArrowDown. The
        three tries share one deadline, the step's own wait at most
        (``wait_seconds``): an optional step whose list never shows
        gives up as soon as it would. A list with no such option is closed
        (Escape) and the step fails naming the options it offered; a list
        that cannot even be searched fails the step, never the take.
        """
        page = self.page
        wanted = (step.value or "").strip()
        await page.mouse.click(*point, delay=60)
        await self.settle()
        deadline = time.monotonic() + min(OPTION_WAIT_SECONDS, step.wait_seconds)

        async def search() -> Any | None:
            try:
                return await find_choice(page, control, wanted, wait=max(0.0, deadline - time.monotonic()))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                raise TakeError(
                    f"the list the {step.role or 'element'} opened could not be searched: {format_exception_message(exc)}"
                ) from None

        option = await search()
        if option is None and await _evaluate(control, _EDITABLE_JS, False):
            await page.keyboard.press("ControlOrMeta+A")
            await page.keyboard.type(wanted, delay=step.delay_ms or TYPING_DELAY_MS[self.script.pace])
            await self.settle()
            option = await search()
        if option is None:
            await page.keyboard.press("ArrowDown")
            option = await search()
        if option is None:
            offered = [entry for entry in await page_names(page) if entry["role"] in CHOICE_ROLES]
            await page.keyboard.press("Escape")
            near = closest_names(wanted, offered) or offered[:6]
            listed = f"; it offered {names_line(near)}" if near else "; it showed no option"
            raise TakeError(f"no option named {wanted!r} in the list the {step.role or 'element'} opened{listed}")
        option_box, _scrolled = await self._box(option, OPTION_WAIT_SECONDS)
        # Read before the click: the list usually closes, and the option goes with it.
        name = await _evaluate(option, "(el) => (el.innerText || el.textContent || '').trim()", "") or wanted
        target = (option_box["x"] + option_box["width"] / 2, option_box["y"] + option_box["height"] / 2)
        await self.move_to(*target)
        ta = self.now()
        await page.mouse.click(*target, delay=60)
        t1 = self.now()
        await self.settle()
        self.events.append(
            self._event(
                index,
                step,
                t0=move_start,
                ta=ta,
                t1=t1,
                # What the step acts on is the control and its choice: the camera frames both.
                box=_box_payload(_union(box, option_box)),
                control_box=_box_payload(box),
                option={"name": name[:200], "box": _box_payload(option_box)},
                point={"x": target[0], "y": target[1]},
                **moved,
            )
        )

    async def _target(self, step: TakeStep) -> pointer.Target:
        """Where a pointer step acts: its node, its point, else its element once shown, scrolled into view."""
        if step.node is not None:
            return await pointer.locate_node(self.page, step.node, viewport=self.viewport, wait=step.wait_seconds)
        if step.at is not None:
            return await pointer.locate_point(self.page, step.at, viewport=self.viewport, wait=step.wait_seconds)
        locator, wait = await self.locate(step)
        box, scrolled = await self._box(locator, wait)
        return pointer.Target((box["x"] + box["width"] / 2, box["y"] + box["height"] / 2), box, {}, scrolled)

    async def _released(self, step: TakeStep) -> pointer.Target:
        """Where a ``drag`` releases: its ``to``, a node or a point - read once the cursor rests on where it
        presses, before its keys are held (a node read by its highlight presses keys of its own)."""
        if isinstance(step.to, NodeTarget):
            return await pointer.locate_node(self.page, step.to.node, viewport=self.viewport, wait=step.wait_seconds)
        return await pointer.locate_point(self.page, step.to, viewport=self.viewport, wait=step.wait_seconds)

    @contextlib.asynccontextmanager
    async def _held(self, keys: list[str]):
        """*keys* held down while the block runs, released whatever happens."""
        pressed: list[str] = []
        try:
            for key in keys:
                await self.page.keyboard.down(key)
                pressed.append(key)
            yield
        finally:
            for key in reversed(pressed):
                with contextlib.suppress(Exception):  # the page may be navigating
                    await self.page.keyboard.up(key)

    async def _hover(self, ms: int) -> None:
        """Rest *ms* milliseconds on the cursor's point, moving over it by under a pixel: each move a
        ``pointermove`` a canvas reads its hover from, never seen on a frame."""
        if ms <= 0:
            return
        x, y = self.x, self.y
        end = time.monotonic() + ms / 1000
        offsets = itertools.cycle((pointer.HOVER_NUDGE, 0.0))
        while time.monotonic() < end:
            await self.page.mouse.move(x + next(offsets), y)
            await asyncio.sleep(min(pointer.HOVER_MOVE_SECONDS, max(0.0, end - time.monotonic())))
        await self.page.mouse.move(x, y)

    async def _drag(self, end: tuple[float, float], seconds: float) -> int:
        """Press, move the cursor to *end* over *seconds* in :data:`MIN_DRAG_MOVES` moves at least, release;
        the moves made."""
        page = self.page
        sx, sy = self.x, self.y
        await page.mouse.down()
        await asyncio.sleep(_DRAG_PRESS_SECONDS)
        moves = max(MIN_DRAG_MOVES, math.ceil(seconds / _CURSOR_STEP_SECONDS))
        started = time.monotonic()
        for i in range(1, moves + 1):
            t = _ease(i / moves)
            px, py = sx + (end[0] - sx) * t, sy + (end[1] - sy) * t
            await page.mouse.move(px, py)
            self.x, self.y = px, py
            self.cursor.append([round(self.now(), 4), round(px, 1), round(py, 1)])
            delay = started + seconds * i / moves - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
        await page.mouse.up()
        return moves

    async def _pointer_step(self, index: int, step: TakeStep) -> None:
        """A pointer step on a node, a point or an element (``pointer``): the cursor glides to its target and
        rests on it, then the button - right for a menu, which is read -, a drag to its release or the wheel, its keys
        held meanwhile. A ``wheel`` without a target turns where the cursor stands."""
        page, action = self.page, step.action
        targeted = step.on_canvas or bool(step.selector or step.role or step.text)
        looked_from = self.now()
        target = await self._target(step) if targeted else None
        label = step.node.label if step.node is not None and target is not None else None
        if label is not None and label in self.confirmed:
            place, at_step = self.confirmed[label]
            target = pointer.remembered(target, place, at_step)
        move_start = self.now()
        hovered: list[dict] = []
        searched: list[float] | None = None
        if target is not None:
            hover_ms = step.hover_ms if step.hover_ms is not None else pointer.HOVER_MS if step.on_canvas else 0
            checks = target.located is not None and bool(target.names) and action in _NODE_BUTTONS
            before = await pointer.read_shown(page) if checks else []
            await self.move_to(*target.point)
            rested = self.now()
            await self._hover(hover_ms)
            if checks:
                hovered, target = await self._hover_to_node(target, before, hover_ms)
                searched = self._searched(hovered, rested, hover_ms)
                missed = pointer.hover_missed(hovered, target)
                if missed is not None:
                    # No place the node was tried at shows its card: the button is not pressed on something else.
                    # Where its seconds went, said: locating the node, then trying its places.
                    spent = {
                        "locate_seconds": round(move_start - looked_from, 2),
                        "hover_seconds": round(self.now() - move_start, 2),
                    }
                    raise pointer.TargetNotFoundError(
                        f"{missed} (node located in {spent['locate_seconds']:.1f} s, its places tried in "
                        f"{spent['hover_seconds']:.1f} s)",
                        picture=await self._marked(target),
                        logged={**self._missed_logged(target, hovered), **spent},
                    )
                if label is not None and action != "drag" and any(entry.get("node") for entry in hovered):
                    self.confirmed[label] = (target.point, index)
        point = (self.x, self.y)
        box = target.box if target is not None else None
        end = await self._released(step) if action == "drag" else None
        ta = self.now()
        logged: dict[str, Any] = {}
        async with self._held(list(step.modifiers or [])):
            if action in ("click", "double_click", "right_click"):
                await page.mouse.click(
                    *point,
                    button="right" if action == "right_click" else "left",
                    click_count=2 if action == "double_click" else 1,
                    delay=70,
                )
            elif end is not None:
                moves = await self._drag(end.point, float(step.seconds) if step.seconds is not None else DRAG_SECONDS)
                logged = {"to_point": {"x": end.point[0], "y": end.point[1]}, "to_box": _box_payload(end.box), "moves": moves}
                if end.logged:
                    logged["to"] = end.logged
                if end.located:
                    logged["to_located"] = end.located
                # What the step acts on is where it presses and where it releases: the camera frames both.
                box = _union(box, end.box) if box else end.box
            elif action == "wheel":
                await self._scroll(step)
        t1 = self.now()
        wrong_menu = None
        if action == "right_click":
            logged["menu"] = await pointer.read_menu(page, step.timeout if step.timeout is not None else pointer.MENU_WAIT_SECONDS)
            if target is not None and target.located is not None:
                # The menu a node's right-click opened is headed with the node: else the point was not on it.
                checked, wrong_menu = pointer.node_menu_check(logged["menu"], target, point)
                if checked is not None:
                    logged["menu_check"] = checked
                # A menu naming something else, or none at all: the next places the node may stand at are tried, the wrong
                # menu closed first, before the step fails - an edge's label a layered layout highlights with its node.
                tried: list[dict] = []
                for other in target.others[: pointer.MENU_RETRIES] if wrong_menu is not None else ():
                    with contextlib.suppress(Exception):  # a menu that closes on its own needs no Escape
                        await page.keyboard.press("Escape")
                    await self.move_to(*other)
                    await self._hover(step.hover_ms if step.hover_ms is not None else pointer.HOVER_MS)
                    point = (self.x, self.y)
                    await page.mouse.click(*point, button="right", delay=70)
                    menu = await pointer.read_menu(page, step.timeout if step.timeout is not None else pointer.MENU_WAIT_SECONDS)
                    checked, wrong_menu = pointer.node_menu_check(menu, target, point)
                    tried.append({"x": round(other[0], 1), "y": round(other[1], 1), "title": (checked or {}).get("title")})
                    logged["menu"] = menu
                    if checked is not None:
                        logged["menu_check"] = checked
                    if wrong_menu is None:
                        break
                if tried:
                    logged["menu_retried"] = tried
                    t1 = self.now()
        if wrong_menu is None:
            await self.settle()
        if target is not None and target.logged:
            logged["target"] = target.logged
        if target is not None and target.located:
            logged["located"] = target.located
            if probed := self._probed(target.located):
                logged["probed"] = probed
        if searched is not None:
            logged["hover_searched"] = searched
        if target is not None and target.located and action in _NODE_BUTTONS:
            # Where the step's seconds went before its button: the node read (OCR shots, highlight probe:
            # ``located_in``), then the cursor's glide onto the node's rim and its rests there, whose nudges cross the
            # node's edges, the page lighting one, another, then none before the node's own hover (#9590). A state shown
            # and gone over the whole of it (``approached``) is the recorder's, never the film's
            # (``capture_rate.held_flashes``, test ``node approach``).
            logged["located_in"] = [round(looked_from, 4), round(move_start, 4)]
            logged["approached"] = [round(looked_from, 4), round(ta, 4)]
        elif target is not None and step.at is not None:
            # A step at a point (``at``, in the window or ``in`` an element), a wheel on a graph canvas among them: the
            # cursor glides to the point and rests there before the button or the wheel, crossing nodes and edges the
            # canvas lights and dims on the way (#10071, a wheel at a point after a pause: the graph dimmed 3 frames and
            # undimmed before the wheel). A state shown and gone over it is the recorder's (test ``point approach``).
            logged["approached"] = [round(looked_from, 4), round(ta, 4)]
            logged["approach"] = "point"
        if hovered:
            logged["hover_check"] = hovered
        if target is not None and target.scrolled:
            logged["scrolled"] = target.scrolled
        if step.modifiers:
            logged["modifiers"] = list(step.modifiers)
        logged.update(box=_box_payload(box), point={"x": round(point[0], 1), "y": round(point[1], 1)})
        if label is not None and action == "right_click" and wrong_menu is None and (logged.get("menu_check") or {}).get("node"):
            self.confirmed[label] = (point, index)
        if wrong_menu is not None and target is not None:
            # The take's failure logs the step with what it read (``record_take``).
            raise pointer.TargetNotFoundError(wrong_menu, picture=await self._marked(target), logged=logged)
        self.events.append(self._event(index, step, t0=move_start, ta=ta, t1=t1, **logged))

    def _searched(self, hovered: list[dict], rested: float, hover_ms: int) -> list[float] | None:
        """The take seconds the cursor looked for a node's card at places whose card named something else (an edge's),
        from its rest on the first of them to its arrival on the place naming the node; ``None`` when the first place
        named it or none did. The cursor's hover nudges there cross the node's edges, the page lighting the node's
        hover and unlighting it under a cursor that seems still: the recorder's search, never the film's - the take
        holds a frame of before over it (``capture_rate.held_flashes``, test ``hover search``, #9590)."""
        if len(hovered) < 2 or hovered[0].get("node") or not hovered[-1].get("node"):
            return None
        arrived = self.now() - hover_ms / 1000
        return [round(rested, 4), round(arrived, 4)] if arrived > rested else None

    def _probed(self, located: dict) -> list[float] | None:
        """The take seconds a node's highlight was probed over (``node_locate._highlight``: the page's list focused and
        its active entry moved, the canvas lit for each), ``None`` for a node located otherwise: what the take holds a
        frame of before over (``capture_rate.held_flashes``, test ``locate probe``)."""
        highlight = located.get("highlight") if isinstance(located.get("highlight"), dict) else {}
        at = highlight.pop("probed_at", None)
        if not isinstance(at, list) or len(at) != 2:
            return None
        start, end = float(at[0]) - self.t_start, float(at[1]) - self.t_start
        return [round(start, 4), round(end, 4)] if end > start else None

    @staticmethod
    async def _marked(target: pointer.Target) -> bytes | None:
        """The canvas shot *target* was read on, its point marked: what a failed node step's contact sheet keeps."""
        from app.services.video_studio import node_locate

        picture = target.picture
        if picture is not None and target.picture_box is not None:
            picture = await asyncio.to_thread(node_locate.marked, picture, target.picture_box, target.point)
        return picture

    @classmethod
    def _missed_logged(cls, target: pointer.Target, hovered: list[dict]) -> dict:
        """What the take's failure logs of a node step stopped before its button: where and how the node was found
        and what the page's card said at each place tried."""
        logged: dict[str, Any] = {"hover_check": hovered, "point": cls._xy(target.point)}
        if target.logged:
            logged["target"] = target.logged
        if target.located:
            logged["located"] = target.located
        return logged

    @staticmethod
    def _xy(point: tuple[float, float]) -> dict:
        return {"x": round(point[0], 1), "y": round(point[1], 1)}

    async def _hover_to_node(
        self, target: pointer.Target, before: list, hover_ms: int
    ) -> tuple[list[dict], pointer.Target]:
        """The cursor resting on *target*'s point: the card the page shows there read (``pointer.hover_check``);
        one naming something else (a relationship's type) sends the cursor to the node's next places
        (:attr:`Target.others`) until one names the node, before any button. ``(what each place's card said, the
        target with the point it rests on first)``; no card at all checks nothing."""
        tried: list[dict] = []
        point = target.point
        read = pointer.hover_check(before, await pointer.read_shown(self.page), point, target.names)
        if read is None or read["node"]:
            return ([{**self._xy(point), "title": read["title"], "node": True}] if read else []), target
        tried.append({**self._xy(point), "title": read["title"], "node": False})
        stale = read["key"]
        remaining = list(target.others)
        # The places are tried for :data:`pointer.HOVER_BUDGET_SECONDS` at most: each is a glide and a rest.
        until = time.monotonic() + pointer.HOVER_BUDGET_SECONDS
        for other in target.others[: pointer.HOVER_RETRIES]:
            if time.monotonic() >= until:
                break
            remaining.remove(other)
            await self.move_to(*other)
            await self._hover(hover_ms)
            read = pointer.hover_check(before, await pointer.read_shown(self.page), other, target.names, stale)
            named = read is not None and read["node"]
            tried.append({**self._xy(other), "title": read["title"] if read else None, "node": named})
            if named:
                # The place the card names the node at comes first; the menu check tries the others after it.
                return tried, replace(target, point=other, others=(point, *remaining))
            if read is not None:
                stale = read["key"]
        # No place's card named the node: a card read somewhere fails the step before the button
        # (``pointer.hover_missed``) where the cursor stands; none at all leaves it to the menu check, the cursor back
        # on the first place.
        if not any(entry.get("title") for entry in tried):
            await self.move_to(*point)
            await self._hover(hover_ms)
        return tried, target

    async def _scroll(self, step: TakeStep) -> None:
        surface = await self._canvas_at(self.x, self.y) if step.to is None and step.by else None
        moved = await self._wheel_target(step.by or 0) if surface is None and step.to is None and step.by else None
        # A wheel over a canvas drawn on the GPU zooms it: its copies stay on, so the zoom is captured alone at the rate
        # the page draws it (``canvas_alone.alone_runs`` keeps them). A scroll of the window or a container, and any
        # wheel on software GL (its copies no faster than its paints), holds them: its frames paced again from the
        # page's (``scroll_pacing``).
        holding = canvas_alone.held(self.page) if surface is None or not self.gpu else contextlib.nullcontext()
        async with self.scroll_log(at=(self.x, self.y)) as logged, holding:
            logged["surface"] = surface
            if moved:
                logged["wheel_moved"] = moved
            await self._scrolled(step, surface, logged)

    async def _wheel_target(self, by: int) -> dict | None:
        """Where the wheel of a step scrolling *by* pixels turns: over the cursor when what is under it, or the
        window, can move that way; else the cursor glides first to the visible centre of the page's largest scroll
        container that can - an app shell scrolling an inner container under a fixed bar - and the move is answered
        (``{"to": [x, y], "element": name}``) for the take's log. ``None``: the cursor stays."""
        try:
            found = await self.page.evaluate(_WHEEL_TARGET_JS, [self.x, self.y, 1 if by > 0 else -1])
        except Exception as exc:  # navigating: the wheel turns where the cursor stands
            logger.debug("Wheel target not read: %s", format_exception_message(exc))
            return None
        point = found.get("point") if isinstance(found, dict) else None
        if not isinstance(point, list) or len(point) != 2:
            return None
        x, y = float(point[0]), float(point[1])
        await self.move_to(x, y)
        return {"to": [round(x, 1), round(y, 1)], "element": str(found.get("element") or "")[:120] or None}

    async def _canvas_at(self, x: float, y: float) -> dict | None:
        """The canvas a wheel at *x*, *y* lands on: its context, its client box, the boxes of what the page draws
        over it (``over``) and the point, with the log of the wheel's turns over it to come (``ticks``); ``None`` when
        there is none (or the page will not say)."""
        try:
            found = await self.page.evaluate(_CANVAS_AT_JS, [x, y])
        except Exception as exc:  # navigating: the wheel turns as over any page
            logger.debug("Canvas under the cursor not read: %s", format_exception_message(exc))
            return None
        box = found.get("box") if isinstance(found, dict) else None
        if not isinstance(box, list) or len(box) != 4:
            return None
        over = [rect for rect in found.get("over") or [] if isinstance(rect, list) and len(rect) == 4]
        return {
            "kind": "canvas",
            "context": found.get("context"),
            "box": [round(float(v), 1) for v in box],
            "over": [[round(float(v), 1) for v in rect] for rect in over],
            "at": [round(x, 1), round(y, 1)],
            "ticks": [],
        }

    async def _painted_tick(self, sent: int) -> list:
        """Once the frame the wheel's last turn drew is painted: ``[t, delta sent, transform | None]``, *t* in
        seconds of the take, the transform the canvas's 2D context draws under."""
        try:
            read = await self.page.evaluate(_CANVAS_TICK_JS)
            t = float(read[0]) / 1000 - self.t_start
            transform = [round(float(v), 5) for v in read[1]] if isinstance(read[1], list) and len(read[1]) == 6 else None
        except Exception:  # navigating: the turn is logged on the recorder's clock
            t, transform = self.now(), None
        return [round(t, 4), sent, transform]

    async def _scrolled(self, step: TakeStep, surface: dict | None = None, logged: dict | None = None) -> None:
        page = self.page
        start = 0.0
        if step.to in ("top", "bottom"):
            # The window to its top or its bottom, moved a frame at a time like the wheel below - never the browser's
            # own smooth scroll, which runs at its own pace and paints in a few large steps.
            start, end = await page.evaluate(_WINDOW_SCROLL_RANGE_JS)
            delta_total = round((0.0 if step.to == "top" else float(end)) - float(start))
        else:
            delta_total = step.by or 0
        if delta_total:
            # The wheel under the cursor turned once a frame of the take over the step's seconds (:func:`wheel_deltas`):
            # scroll containers follow, and the page moves the same distance between two painted frames
            # whatever each turn costs - a turn the clock falls behind on is caught up on the next. Over a canvas
            # each turn is painted before the next and logged with what the canvas drew: a burst of turns is
            # never queued on a canvas repainting slower than they come.
            seconds = float(step.seconds) if step.seconds is not None else scroll_seconds(delta_total, self.script.pace)
            gpu_canvas = step.to is None and surface is not None and self.gpu
            rate = int(self.script.fps) * (_GPU_TURNS_PER_FRAME if gpu_canvas else 1)
            plan = list(itertools.accumulate(wheel_deltas(delta_total, seconds, rate)))
            tick = 1 / rate
            if surface is not None:
                surface["ticks"].append(await self._painted_tick(0))
            # When the wheel turned and how far it had: the motion a compositor shows, however late the page
            # paints it. Each turn is logged on the take's frame clock - the frame it was due on, never the instant a
            # busy host sent it -, so a take is paced on an exact 1/fps a frame whatever the host's load: a turn sent
            # late, or caught up with the next, moves the log no differently from one sent on time.
            turns: list[list[float]] = [[0.0, 0]]
            if logged is not None:
                logged["wheel"] = {"t0": round(self.now(), 4), "pixels": delta_total, "turns": turns}
            # On a GPU the canvas repaints faster than the take's rate: awaiting each turn's painted frame before the
            # next (a frame, then a CDP round trip) let one turn through every other frame, and the page drew the zoom
            # at 10 to 15 fps whatever the window (#10943). There each turn's paint is read beside the next turns,
            # at most :data:`_GPU_TICKS_IN_FLIGHT` read at once; on software GL each is awaited, as before.
            reading: list[asyncio.Task] = []
            # And each turn is sent without awaiting the one before: the browser answers a wheel only once the page's
            # main thread ran its wheel handler, behind the paint and the copies, so awaiting each let a turn through
            # every other frame and the zoom painted at 20 to 24 fps beside 54 at rest (#10950). At most
            # :data:`_GPU_WHEELS_IN_FLIGHT` are sent ahead, in order; on software GL each is awaited, as before.
            wheeling: list[asyncio.Task] = []
            started, sent = time.monotonic(), 0
            while True:
                elapsed = time.monotonic() - started
                due = min(len(plan), math.floor(elapsed / tick) + 1)
                turn = plan[due - 1] - sent
                if turn:
                    if gpu_canvas:
                        wheeling = [task for task in wheeling if not task.done()]
                        if len(wheeling) >= _GPU_WHEELS_IN_FLIGHT:
                            await asyncio.wait(wheeling[:1])
                        wheeling.append(asyncio.ensure_future(page.mouse.wheel(0, turn)))
                        await asyncio.sleep(0)
                    elif step.to is None:
                        await page.mouse.wheel(0, turn)
                    else:
                        await page.evaluate("(top) => window.scrollTo({top, behavior: 'instant'})", start + sent + turn)
                    sent += turn
                    turns.append([round((due - 1) * tick, 4), sent])
                    if surface is not None and self.gpu:
                        # A paint read never holds the next turn: with :data:`_GPU_TICKS_IN_FLIGHT` reads unanswered
                        # this turn's paint is not read (the next one's is), so the turns keep their ticks (#10955).
                        if sum(1 for task in reading if not task.done()) < _GPU_TICKS_IN_FLIGHT:
                            reading.append(asyncio.ensure_future(self._painted_tick(sent)))
                    elif surface is not None:
                        surface["ticks"].append(await self._painted_tick(sent))
                if due >= len(plan):
                    break
                await asyncio.sleep(max(0.0, started + due * tick - time.monotonic()))
            if wheeling:
                await asyncio.gather(*wheeling, return_exceptions=True)
            if reading and surface is not None:
                surface["ticks"].extend(gpu_ticks(await asyncio.gather(*reading, return_exceptions=True)))
            if surface is not None:
                await self._canvas_settled(surface, sent)
                await self._canvas_appeared(surface)
        await self._wait_scroll_end()
        await asyncio.sleep(0.25)

    async def _canvas_appeared(self, surface: dict) -> None:
        """The boxes the page draws over the canvas once its zoom settled that were not there as the wheel began
        (``surface["appeared"]``, CSS px): a tooltip the cursor opened over a link the zoom brought under it. Its
        pixels tell two keyframes apart where no zoom does; pacing leaves them out of the registration and keeps them
        as painted.

        Also ``surface["stayed"]``: the indices of ``over`` the page still draws at the same place once the zoom
        settled - a toolbar, a Legend button the page pins over the canvas. The zoom never carries one: its box
        may be mostly see-through, the canvas moving under it, but what the page draws there stood still."""
        found = await self._canvas_at(*surface["at"])
        if found is None:
            return
        before = surface.get("over") or []

        def same(rect: list, old: list) -> bool:
            return all(abs(p - q) <= _SAME_OVER_PX for p, q in zip(rect, old, strict=True))

        appeared = [rect for rect in found["over"] if not any(same(rect, old) for old in before)]
        if appeared:
            surface["appeared"] = appeared
        stayed = [index for index, old in enumerate(before) if any(same(rect, old) for rect in found["over"])]
        if stayed:
            surface["stayed"] = stayed

    async def _canvas_settled(self, surface: dict, sent: int) -> None:
        """The canvas's zoom read on, a painted frame at a time, once the wheel stopped: for at least the first
        of :data:`_CANVAS_SETTLE_SECONDS`, then until its 2D transform holds, the second at most."""
        least, most = _CANVAS_SETTLE_SECONDS
        began, ticks = time.monotonic(), surface["ticks"]
        while time.monotonic() - began < most:
            ticks.append(await self._painted_tick(sent))
            if time.monotonic() - began >= least and (ticks[-1][2] is None or ticks[-1][2] == ticks[-2][2]):
                return


#: The roles of a list's options: what a ``select`` on a combobox picks.
CHOICE_ROLES = ("option", "menuitemradio", "menuitem", "treeitem", "gridcell")
#: What holds a choice found outside the list the control names: ``option`` lives in a
#: (portalled) listbox anywhere, but a ``menuitem``, a ``treeitem`` or a ``gridcell`` alone is as likely a
#: navigation entry or a table cell named like the option, so it is taken only inside a popup.
_POPUP_CONTAINER = ":is([role=listbox], [role=menu], [role=tree], [role=grid])"
_ANYWHERE_ROLES = ("option",)
#: How long a ``select`` waits for the list a combobox opened to show the option.
OPTION_WAIT_SECONDS = 3.0
#: The most matches of one name looked at for a visible one.
_CHOICES_READ = 10
_EDITABLE_JS = (
    "(el) => el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.isContentEditable"
    " || !!el.querySelector('input:not([type=hidden]), textarea, [contenteditable=true]')"
)
_POPUP_JS = (
    "(el) => (el.getAttribute('aria-controls') || el.getAttribute('aria-owns') || '').split(/\\s+/).filter(Boolean)"
)


async def _evaluate(locator: Any, script: str, default: Any) -> Any:
    """*script* on *locator*'s element, or *default* when it cannot be read (re-rendered, detached)."""
    try:
        return await locator.evaluate(script)
    except Exception:  # a control the page replaced on opening: read as nothing
        return default


async def find_choice(page: Any, control: Any, name: str, *, wait: float = OPTION_WAIT_SECONDS) -> Any | None:
    """The visible option named *name* in the list *control* opened, or ``None`` once *wait* seconds passed.

    Read by accessible name, as a ``role`` + ``name`` step is: an exact name
    first, then a name containing it in any case; in the list the control
    names (``aria-controls`` / ``aria-owns``) first, then anywhere on the page
    - a list portalled to the end of the document is the common case -, where
    only an ``option`` is taken anywhere and the other roles only inside a
    popup (:data:`_POPUP_CONTAINER`). An id is quoted as JSON, a valid
    CSS string whatever it holds (a backslash, a quote).
    """
    if not name:
        return None
    ids = await _evaluate(control, _POPUP_JS, [])
    owned = [
        (page.locator(f"[id={json.dumps(value, ensure_ascii=False)}]"), CHOICE_ROLES)
        for value in ids
        if isinstance(value, str)
    ]
    scopes = [
        *owned,
        (page, _ANYWHERE_ROLES),
        (page.locator(_POPUP_CONTAINER), tuple(role for role in CHOICE_ROLES if role not in _ANYWHERE_ROLES)),
    ]
    deadline = time.monotonic() + wait
    while True:
        for exact in (True, False):
            for scope, roles in scopes:
                for role in roles:
                    candidates = scope.get_by_role(role, name=name, exact=exact)
                    for i in range(min(await candidates.count(), _CHOICES_READ)):
                        if await candidates.nth(i).is_visible():
                            return candidates.nth(i)
        if time.monotonic() >= deadline:
            return None
        await asyncio.sleep(0.15)


def _union(a: dict, b: dict) -> dict:
    """The box holding two ``{x, y, width, height}`` boxes."""
    left, top = min(a["x"], b["x"]), min(a["y"], b["y"])
    right = max(a["x"] + a["width"], b["x"] + b["width"])
    bottom = max(a["y"] + a["height"], b["y"] + b["height"])
    return {"x": left, "y": top, "width": right - left, "height": bottom - top}


def _box_payload(box: dict | None) -> dict | None:
    if not box:
        return None
    width = box.get("width", box.get("w"))
    height = box.get("height", box.get("h"))
    return {
        "x": round(float(box["x"]), 1),
        "y": round(float(box["y"]), 1),
        "w": round(float(width), 1),
        "h": round(float(height), 1),
    }


def _page_css(script: TakeScript) -> str:
    rules = []
    if script.redact:
        rules.append(f"{', '.join(script.redact)} {{ filter: blur(10px) saturate(0.6) !important; }}")
    if script.hide:
        rules.append(f"{', '.join(script.hide)} {{ display: none !important; visibility: hidden !important; }}")
    return "\n".join(rules)


def _init_script(css: str) -> str:
    return (
        "(() => { const css = " + json.dumps(css) + ";"
        " const apply = () => { if (document.getElementById('__xtm_video_studio')) return;"
        " const style = document.createElement('style'); style.id = '__xtm_video_studio'; style.textContent = css;"
        " (document.head || document.documentElement).appendChild(style); };"
        " if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', apply); else apply(); })();"
    )


def seeded_keep(keep: tuple[str, ...], seeded: dict[str, str]) -> tuple[str, ...]:
    """The storage keys a take's page state resets keep: the profile's session's, then the keys the take seeds
    (``TakeScript.local_storage``), so a reset clears what the takes before left and never what this one asked."""
    return tuple(dict.fromkeys((*keep, *seeded)))


def storage_reset(reset: PageReset, keep: tuple[str, ...]) -> StorageReset:
    """What a page state reset clears of the page's origin: never a cookie, never a key of *keep*."""
    return StorageReset(reset.local_storage, reset.session_storage, reset.indexed_db, tuple(keep))


async def reset_page(rb: ProfileBrowser, url: str, reset: PageReset, keep: tuple[str, ...]) -> dict:
    """Open *url* in a clean page state (``browser.open_cleared``): its origin's storage cleared at the new
    document's start but the session's *keep* keys, at *url* exactly as given (``url_query``) - never where the page
    left its address -, else opened there and reloaded once at the address it landed on. ``{reset, cleared}``, what
    the action log's first line says (``take_state.opened_words``)."""
    if not reset.url_query:
        await rb.goto(url)
    cleared = await open_cleared(rb, url, storage_reset(reset, keep), reload=not reset.url_query)
    return {"reset": reset.model_dump(), "cleared": cleared}


def _cleared_any(cleared: dict) -> bool:
    return bool(cleared.get("unread") or any(cleared.get(key) for key in ("local_storage", "session_storage", "indexed_db")))


@dataclass
class Preflight:
    """What a take records at once its capture rate was measured (:func:`preflight`): its script - another window,
    pixel ratio or rate a ``fps_fallback`` took -, its CSS viewport and pixel ratio, its notes and its ``capture``."""

    script: TakeScript
    viewport: tuple[int, int]
    ratio: float
    notes: list[str]
    captured: dict


async def preflight(script: TakeScript, *, progress: Any = None, lane: Any = None) -> Preflight:
    """Measure the worker's capture rate before step 1 (``capture_rate.plan``) and what the take records at.

    The take is recorded at a lower pixel ratio that sustains its fps - or, by
    its ``fps_fallback``, at the largest window or at fps 30 -, or
    refused (``TakeError``). In the recording lane's slot *lane*
    (``recording_lane.Slot``) the warm-up measures one take of its worker at a
    time and reads what runs beside it: short of its fps beside other jobs, it
    raises :class:`WorkerBusyError` - nothing recorded, the take waits in the
    lane -, and a take that waited says so in its notes and its action log
    (``capture.waited``), or in its refusal.
    """
    from datetime import UTC, datetime

    from app.services.video_studio import capture_rate
    from app.services.video_studio.spec import zoomed_window

    viewport, ratio = zoomed_window(
        (script.viewport.width, script.viewport.height), float(script.pixel_ratio), script.browser_zoom or 100
    )
    if progress:
        await progress(4, "Measuring the worker's capture rate")
    async with lane.measuring() if lane is not None else contextlib.nullcontext():
        planned = await capture_rate.plan(
            script, viewport, ratio, beside=lane.beside if lane is not None else None, may_wait=bool(lane and lane.may_wait)
        )
    if planned.waits:
        raise WorkerBusyError(planned, float(script.fps))
    waited = lane.waited if lane is not None else None
    now = datetime.now(UTC)
    if planned.refused and lane is not None and getattr(lane, "relaunched", False) and lane.may_wait:
        # Relaunched after a worker went with it (a rollout, a drain): a worker just booted often falls short of the
        # take's fps for its first minutes. It waits in the lane for one that sustains it, up to
        # ``recording_lane.RELAUNCH_MAX_WAIT``, and is refused as a first launch only past it (#9812).
        raise WorkerBusyError(planned, float(script.fps), relaunch=True)
    if planned.refused:
        raise TakeError(planned.refused + (waited.refused_words(now) if waited is not None else ""))
    notes = [planned.note] if planned.note else []
    captured = planned.record()
    if waited is not None:
        # Recorded once its worker's load allowed its fps: its action log's first line says the wait.
        captured["waited"] = waited.waited_words(now, planned.measured.get("fps"))
        notes.append(f"{captured['waited'][:1].upper()}{captured['waited'][1:]}.")
    ratio = planned.ratio
    if planned.window is not None:
        # Recorded at the largest window the worker sustains the take's fps at (fps_fallback size).
        pixel_ratio = int(planned.pixel_ratio) if float(planned.pixel_ratio).is_integer() else planned.pixel_ratio
        script = script.model_copy(
            update={"viewport": Viewport(width=planned.window[0], height=planned.window[1]), "pixel_ratio": pixel_ratio}
        )
        viewport, ratio = zoomed_window(planned.window, float(planned.pixel_ratio), script.browser_zoom or 100)
    if planned.fps is not None:
        script = script.model_copy(update={"fps": planned.fps})
    return Preflight(script, viewport, ratio, notes, captured)


async def _drawn(rb: Any, gl: Any) -> dict:
    """What a take keeps of what drew its canvases (``backend``, ``gl``; *gl* a ``capture_rate.GlBackend``), read once
    it recorded or failed: a WebGL context made before would start the GPU process the page's canvases then draw on."""
    from app.services.video_studio import capture_rate

    kept: dict = {}
    try:
        kept["backend"] = str(await rb.page.evaluate(capture_rate.RENDERER_JS))[:200]
    except Exception as exc:  # the backend is said when the page can say it
        logger.debug("WebGL renderer not read: %s", format_exception_message(exc))
    # What its browser drew WebGL and its canvases on, as Chromium says it.
    status = None
    try:
        status = await capture_rate.gl_status(rb.browser)
    except Exception as exc:  # said from the backend chosen
        logger.debug("GPU feature status not read: %s", format_exception_message(exc))
    kept["gl"] = capture_rate.drawn_with(gl, status)
    return kept


async def record_take(
    script: TakeScript,
    *,
    workdir: str,
    profile: ProfileSnapshot | None,
    start_url: str,
    policy: TargetPolicy,
    max_seconds: float,
    progress: Any = None,
    warm_up: bool = False,
    earlier: dict[str, take_state.EarlierFind] | None = None,
    lane: Any = None,
) -> CaptureResult:
    """Play *script* and capture it into *workdir*; the frames, action log and cursor path.

    Its ``browser_zoom`` lays the page out in a CSS viewport that many times
    smaller at that many times the pixel ratio (``spec.zoomed_window``): the
    frames keep the window's size, and the take records the viewport and the
    ratio the page was laid out at, which every reader of its events and
    cursor (CSS pixels) maps onto its frames. *warm_up* measures the worker's
    capture rate first (:func:`preflight`, in the recording lane's slot *lane*);
    the worker's takes do, a direct call records as asked. Its ``reset_state``
    opens ``start_url`` in a clean page state once the session is
    checked (:func:`reset_page`). A step that misses its element says the page
    it missed it on, and where one of *earlier*
    (``take_state.load_earlier_finds``) found it.
    """
    from app.services.browser_profiles import signin
    from app.services.video_studio import capture_rate
    from app.services.video_studio.spec import zoomed_window

    viewport, ratio = zoomed_window(
        (script.viewport.width, script.viewport.height), float(script.pixel_ratio), script.browser_zoom or 100
    )
    options = profile.options if profile else {}
    notes: list[str] = []
    signed_in: bool | None = None
    captured: dict = {}
    if warm_up:
        planned = await preflight(script, progress=progress, lane=lane)
        script, viewport, ratio = planned.script, planned.viewport, planned.ratio
        notes.extend(planned.notes)
        captured = planned.captured
    # Its WebGL and canvases drawn on the worker's GPU where Chromium finds one, on SwiftShader otherwise.
    gl = await capture_rate.gl_backend()
    if gl.refused:  # a recording lane without its GPU records nothing in software
        raise TakeError(f"Refused before recording: this worker's recording lane has no GPU - {gl.why}.")
    # The build this worker runs, said on its action log's first line (#11407): a retest reads which build recorded it.
    from app.services.video_studio.take_state import BUILD_KEY, build_record

    captured[BUILD_KEY] = build_record()
    # The worker class and GPU model it records on, said before step 1 (its stage) and on its action log's first line.
    if (worker := capture_rate.worker_record()) is not None:
        captured["worker"] = worker
        if progress:
            from app.services.video_studio.worker_class import words as worker_words

            await progress(5, f"Recording on {worker_words(worker)}")
    async with profile_browser(
        policy=policy,
        viewport=viewport,
        pixel_ratio=ratio,
        storage_state=profile.storage_state if profile else None,
        color_scheme=script.color_scheme or options.get("color_scheme"),
        locale=options.get("locale") or "en-US",
        timezone=options.get("timezone") or "UTC",
        extra_args=gl.args,
        env=capture_rate.launch_env(gl.args),
    ) as rb:
        css = _page_css(script)
        if css:
            await rb.context.add_init_script(_init_script(css))
        await rb.context.add_init_script(_CANVAS_CONTEXT_JS)
        # Every document the take opens notes what moves from its start and when its DOM last changed:
        # a step that navigates included.
        await rb.context.add_init_script(_MOTION_WATCH_JS)
        await rb.context.add_init_script(_SETTLE_WATCH_JS)
        if gl.alone:
            # On the CPU and on a GPU: every document copies its canvases as they change, for the take to composite
            # where the screencast falls short of a transition (a re-layout after a click).
            await rb.context.add_init_script(
                canvas_alone.tap_script(
                    script.fps, _NAME_JS, ratio=gl.alone_ratio, quality=gl.alone_quality, encoders=gl.alone_encoders
                )
            )
        keep = seeded_keep(profile.keep_storage_keys if profile is not None else (), script.local_storage)
        if script.local_storage and origin_of(start_url):
            # The application's own preference (its theme) as asked, on every document of the start origin, before
            # its scripts read it; registered before any page state reset, which keeps these keys.
            await rb.context.add_init_script(seed_storage_script(origin_of(start_url), script.local_storage))
        recorder = _Recorder(rb, script, viewport=viewport, max_seconds=max_seconds, keep=keep)
        recorder.gpu = bool(gl.gpu)
        cdp = await rb.context.new_cdp_session(rb.page)
        frame_w = round(viewport[0] * ratio)
        frame_h = round(viewport[1] * ratio)
        screencast = _Screencast(cdp, workdir, max_frames=int(max_seconds * 70) + 200)
        recorder.screen = screencast

        if progress:
            await progress(5, "Opening the application")
        if profile is not None and not script.record_sign_in and profile.needs_sign_in:
            # The take starts on the signed-in application, or not at all.
            async def renewing() -> None:
                if progress:
                    await progress(8, "Signing in again")

            try:
                expired = await signin.ensure_signed_in(rb, profile, on_renew=renewing)
            except signin.SessionNotSignedInError as exc:
                raise ProfileNotAuthenticatedError(exc.probe, exc.detail) from exc
            if expired is not None:
                notes.append(
                    f"The saved session did not open the application ({expired.reason}): the profile signed in "
                    "again before recording."
                )
            signed_in = True
        # What the saved session carried over from the takes before is cleared once it is checked.
        reset = page_reset(script.reset_state)
        opened: dict = {"asked": start_url, "carried": bool(profile is not None and profile.storage_state)}
        if reset is not None and opened["carried"]:
            if progress:
                await progress(9, "Opening the start page in a clean page state")
            opened.update(await reset_page(rb, start_url, reset, keep))
        else:
            await rb.goto(start_url)
        if signed_in and profile is not None:
            # The page may paint its sign-in form while it reads the session: the take
            # starts once the application shows signed in, so nothing painted before is in it.
            if progress:
                await progress(9, "Waiting for the application to open signed in")
            # A start URL on an application the profile declares is judged as that application (its own host).
            landed = await signin.wait_signed_in(rb, profile.scoped_to(start_url))
            if not landed.authenticated:
                if _cleared_any(opened.get("cleared") or {}):
                    raise ResetSignedOutError(landed, opened["cleared"])
                raise ProfileNotAuthenticatedError(landed)
        await recorder.settle()
        await asyncio.sleep(0.4)
        zoom = script.browser_zoom or 100
        if zoom > 100:
            if progress:
                await progress(9, f"Checking the page's layout at a browser zoom of {zoom}%")
            fit = await zoom_fit(rb.page, viewport, (script.viewport.width, script.viewport.height), zoom)
            if fit.refused:
                raise TakeError(f"Refused before recording: {fit.words()}.")
            if fit.read_unzoomed:
                await recorder.settle()

        opened["url"] = await current_url(rb.page)
        opened["added_query"] = take_state.added_query(start_url, opened["url"])
        captured["opened"] = opened
        await screencast.start(frame_w, frame_h)
        recorder.t_start = screencast.t_start
        # What its page copies of its canvases, drained while it records: timed on the screencast's clock.
        tap = (
            canvas_alone.Tap(
                rb.page,
                workdir,
                origin=screencast.t_start,
                max_copies=int(max_seconds * script.fps) + 200,
                ratio=gl.alone_ratio,
                quality=gl.alone_quality,
            )
            if gl.alone
            else None
        )
        if tap is not None:
            tap.start()
        recorder.cursor.append([0.0, round(recorder.x, 1), round(recorder.y, 1)])
        await rb.page.mouse.move(recorder.x, recorder.y)
        await asyncio.sleep(0.5)
        truncated_at: int | None = None
        try:
            total = len(script.steps)
            for index, step in enumerate(script.steps):
                if progress:
                    await progress(10 + int(70 * index / max(total, 1)), f"Step {index + 1}/{total}: {step.action}")
                started = recorder.now()
                if started >= max_seconds:
                    truncated_at = index
                    break
                # The cap stops the step it falls in: what was recorded before it is kept.
                bound = asyncio.timeout(max_seconds - started)
                try:
                    async with bound:
                        await recorder.run_step(index, step)
                        await recorder.log_canvases(index)
                except TakeError:
                    raise
                except Exception as exc:
                    if isinstance(exc, TimeoutError) and bound.expired():
                        truncated_at = index
                        break
                    failed_at = recorder.now()
                    message = format_exception_message(exc).splitlines()[0][:300]
                    located = step.action in LOCATOR_ACTIONS and not step.on_canvas
                    # The page state it missed its element in: its URL, title and the candidates it showed,
                    # and where an earlier take of the profile found it.
                    state = await take_state.page_state(rb.page, step) if located else None
                    found = (earlier or {}).get(take_state.step_key(step) or "") if located else None
                    missed = take_state.missed_words(state, found)
                    # A pointer step that acted before it failed (the menu a node's right-click opened was not
                    # the node's) logs what it read: the node located, the point, the menu.
                    acted = getattr(exc, "logged", None) or {}
                    if step.optional:
                        # What the page offered at that moment, closest to what the step asked for (a graph's
                        # labels for a node), and what it was filtered by: read inside the span the
                        # planner cuts.
                        near = await _near(exc, step, rb.page)
                        held = await page_filters(rb.page) if located else {}
                        gave_up = recorder.now()
                        offered = f"; closest on the page then: {names_line(near)}" if near else ""
                        if said := filters_words(held):
                            offered += f"; the page then held {said} ({FILTERED})"
                        if missed:
                            offered += f"; {missed[:1].lower()}{missed[1:]}"
                        notes.append(
                            f"Optional step {index} ({step.action}) skipped after {gave_up - started:.1f} s: {message} "
                            f"(its wait is cut from the video){offered}"
                        )
                        # The whole span it waited is logged, so the planner cuts it from the video.
                        event = recorder._event(index, step, **acted, t0=started, ta=gave_up, t1=gave_up, ok=False)
                        if near:
                            event["near"] = near
                        if said:
                            event["filters"] = held
                        if state is not None:
                            event["page"] = state.as_dict()
                        if found is not None:
                            event["found_before"] = {"take_id": found.take_id, "url": found.url}
                        recorder.events.append(event)
                        continue
                    near = await _near(exc, step, rb.page)
                    offered = f" Closest on the page then: {names_line(near)}." if near else ""
                    if said := filters_words(await page_filters(rb.page) if located else {}):
                        offered += f" The page then held {said}: {FILTERED}."
                    if missed:
                        offered += f" {missed}."
                    label = recorder._event(index, step)["label"] or ""
                    if acted:
                        recorder.events.append(
                            recorder._event(index, step, **acted, t0=started, ta=failed_at, t1=failed_at, ok=False)
                        )
                    # How long the step ran before it failed, from the end of the step before: the gap a failed take
                    # is read for, its action log ending there.
                    ran = f" (after {failed_at - started:.1f} s, at {failed_at:.1f} s of the take)"
                    failed = StepFailedError(
                        f"Step {index} ({step.action} {label}) failed{ran}: {message}{offered}",
                        step=index,
                        at=failed_at,
                        label=label,
                    )
                    if isinstance(exc, pointer.TargetNotFoundError) and exc.picture:
                        # The canvas the node was looked for on: the contact sheet ends on it.
                        name = f"step-{index:03d}-canvas.png"
                        await asyncio.to_thread((Path(workdir) / name).write_bytes, exc.picture)
                        failed.attached.append((name, f"step {index}: the canvas read for {label}"))
                    raise failed from exc
            if truncated_at is None:
                await asyncio.sleep(script.end_hold_ms / 1000)
            else:
                notes.append(truncation_note(script, truncated_at, max_seconds))
        except StepFailedError as failed:
            # The contact sheet of the failed take ends on the frame where the step failed.
            await screencast.stop()
            failed.frames, failed.events, failed.opened = list(screencast.frames), list(recorder.events), opened
            # What its canvases copied and what drew them, up to the failure: a failed take says its capture line.
            if tap is not None:
                captured["alone"], tap = await tap.stop(), None
                # Never composited, its skipped copies are placed nowhere: the count says them, not each instant.
                captured["alone"].pop("skips", None)
            failed.capture = {
                "alone": captured.get("alone"),
                BUILD_KEY: captured.get(BUILD_KEY),
                **await _drawn(rb, gl),
            }
            failed.capture = {key: value for key, value in failed.capture.items() if value}
            raise
        finally:
            await screencast.stop()
            if tap is not None:
                captured["alone"] = await tap.stop()
        duration = recorder.now()
        # The frames painted at another geometry than the take's, dropped: every run said, step, time, length.
        if screencast.dropped:
            captured["off_scale_dropped"] = capture_rate.dropped_runs(screencast.dropped, recorder.events)
            if said := capture_rate.dropped_words(captured["off_scale_dropped"]):
                notes.append(said)
        captured.update(await _drawn(rb, gl))
        storage_state = None
        after = None
        if profile is not None and (signed_in or script.record_sign_in):
            try:
                storage_state, after = await signin.save_session(rb, profile)
            except Exception as exc:
                logger.debug("Session not saved after take: %s", format_exception_message(exc))
                storage_state = None
        layout = None
        if progress:
            await progress(82, "Probing how far down the page keeps its layout")
        try:
            layout = await asyncio.wait_for(probe_layout(rb.page, viewport), timeout=LAYOUT_PROBE_SECONDS)
        except Exception as exc:  # advice only: a take is never refused for it
            logger.info("Layout of the recorded page not probed: %s", format_exception_message(exc))
    if not screencast.frames:
        raise TakeError("The browser painted no frame (the page may be blank or blocked).")
    return CaptureResult(
        workdir=workdir,
        frames=screencast.frames,
        duration=max(duration, screencast.frames[-1][0] + 0.05),
        events=recorder.events,
        cursor=recorder.cursor,
        viewport=viewport,
        pixel_ratio=ratio,
        frame_size=screencast.frame_size,
        storage_state=storage_state,
        signed_in=signed_in,
        notes=notes,
        probe=after,
        layout=layout,
        truncated_at_step=truncated_at,
        painted=screencast.offsets,
        capture=captured,
        script=script,
        alone=tap.frames if tap is not None else [],
    )


def truncation_note(script: TakeScript, step: int, max_seconds: float) -> str:
    """What a take the cap stopped at *step* says: where it stopped, what it kept and how to record the rest."""
    last = len(script.steps) - 1
    rest = f"step {step}" if step == last else f"steps {step} to {last}"
    return (
        f"The recording reached its {max_seconds:g} s limit at step {step} ({script.steps[step].action} "
        f"{step_label(script.steps[step]) or ''}): it is kept up to there, {rest} not recorded. Record {rest} as a "
        "second recording (it starts where this one ended: open that page first)."
    )


# ── How far down the page keeps its layout ────────────────────────

#: The narrowest viewport probed: the narrowest a take records.
LAYOUT_PROBE_FLOOR = MIN_VIEWPORT_WIDTH
#: The widths probed on the way down, as shares of the recorded width; then halved between the last kept and the
#: first changed, :data:`LAYOUT_BISECTIONS` times.
LAYOUT_STEPS = (0.9, 0.8, 0.7, 0.6, 0.5, 0.4)
LAYOUT_BISECTIONS = 3
#: How long the page is given to lay itself out again after each resize (a resize handler may be debounced).
LAYOUT_SETTLE_SECONDS = 0.35
#: The longest the probe takes, every width together.
LAYOUT_PROBE_SECONDS = 15.0
#: The blocks of the layout: at least this share of the viewport's width, this tall, within two screens.
_BLOCK_SHARE = 0.08
_BLOCK_HEIGHT = 12
_MAX_BLOCKS = 400
#: Side by side: these blocks (at least this share of the width) sharing half the height of the shorter.
_COLUMN_SHARE = 0.12
#: CSS pixels a page may overflow its width by and still lay out whole: the rounding of a fractional layout.
SPILL_TOLERANCE = 2
LAYOUT_VERSION = 2

#: Marks the page's layout blocks at the recorded width (``true``) or reads them again (``false``): their boxes in
#: CSS pixels (``null``: no longer shown), and how far the document and its main container overflow their width
#: (``spill``, ``main_spill``): a page whose minimum width is wider than the viewport.
_LAYOUT_JS = """(mark) => {
  const W = innerWidth, H = innerHeight;
  if (mark) {
    const found = [];
    for (const el of document.querySelectorAll('body *')) {
      const r = el.getBoundingClientRect();
      if (r.width < W * __SHARE__ || r.height < __HEIGHT__ || r.bottom <= 0 || r.top >= 2 * H) continue;
      const cs = getComputedStyle(el);
      if (cs.display === 'none' || cs.visibility === 'hidden') continue;
      found.push(el);
      if (found.length >= __MOST__) break;
    }
    window.__xtmLayoutBlocks = found;
  }
  const boxes = (window.__xtmLayoutBlocks || []).map(el => {
    if (!el.isConnected) return null;
    const r = el.getBoundingClientRect(), cs = getComputedStyle(el);
    if (r.width <= 0 || r.height <= 0 || cs.display === 'none' || cs.visibility === 'hidden') return null;
    return [r.left, r.top, r.width, r.height];
  });
  const spill = Math.max(0, document.documentElement.scrollWidth - W);
  const main = document.querySelector('main, [role="main"]');
  const mainSpill = main ? Math.max(0, main.scrollWidth - main.clientWidth) : 0;
  return {width: W, overflow: spill > __TOLERANCE__, spill, main_spill: mainSpill, boxes};
}""".replace("__SHARE__", str(_BLOCK_SHARE)).replace("__HEIGHT__", str(_BLOCK_HEIGHT)).replace("__MOST__", str(_MAX_BLOCKS)).replace(
    "__TOLERANCE__", str(SPILL_TOLERANCE)
)


def _spills(read: dict) -> tuple[float, float]:
    """How far the document and the main container of a read of :data:`_LAYOUT_JS` overflow their width, in CSS px;
    a read that says only whether the page scrolls sideways counts as one past the tolerance."""
    document = read.get("spill")
    if not isinstance(document, int | float):
        document = SPILL_TOLERANCE + 1 if read.get("overflow") else 0
    main = read.get("main_spill")
    return float(document), float(main) if isinstance(main, int | float) else 0.0


def overflowed_by(reference: dict, probed: dict) -> float:
    """How much more the page overflows in *probed* than in *reference* (two reads of :data:`_LAYOUT_JS`), its document
    or its main container, in CSS px: what a narrower viewport cut off that the reference showed."""
    (document, main), (document_before, main_before) = _spills(probed), _spills(reference)
    return max(0.0, document - document_before, main - main_before)


def min_width(reference: dict, probed: dict) -> int | None:
    """The narrowest width the page lays out whole at: *probed*'s width plus what it cut off past the
    tolerance; ``None`` when nothing overflows it."""
    extra = overflowed_by(reference, probed)
    if extra <= SPILL_TOLERANCE:
        return None
    return math.ceil(float(probed.get("width") or 0) + extra)


@dataclass(frozen=True)
class ZoomFit:
    """The start page read at a recording's browser zoom before it records (:func:`zoom_fit`)."""

    zoom: int
    #: The CSS width a zoom lays the page out at (the window over the zoom), and the window's own.
    css_width: int
    window_width: int
    #: CSS px the page overflows by at the zoom beyond what it does unzoomed; 0 when it lays out whole.
    overflow: float = 0.0
    #: The page was read unzoomed too: the viewport was resized and put back.
    read_unzoomed: bool = False

    @property
    def refused(self) -> bool:
        return self.overflow > SPILL_TOLERANCE

    @property
    def min_width(self) -> int:
        """The narrowest CSS width the page lays out whole at."""
        return math.ceil(self.css_width + self.overflow)

    def largest_zoom(self) -> int:
        """The largest zoom whose window over it holds :attr:`min_width`, within the tolerance."""
        fits = math.floor(self.window_width * 100 / max(1.0, self.min_width - SPILL_TOLERANCE))
        return max(100, min(self.zoom - 1, fits))

    def words(self) -> str:
        """``at zoom 190 the page lays out at 842 px and overflows by 158 px; the largest zoom that keeps its layout is
        160``."""
        return (
            f"at zoom {self.zoom} the page lays out at {self.css_width} px and overflows by {round(self.overflow)} px; "
            f"the largest zoom that keeps its layout is {self.largest_zoom()}"
        )


async def zoom_fit(page: Any, viewport: tuple[int, int], window: tuple[int, int], zoom: int) -> ZoomFit:
    """The start page in *page*, laid out at a browser zoom's CSS *viewport* (the *window* over *zoom*), read before the
    take records: how far its document and its main container overflow there. One that overflows is read at
    the *window* too - a page wider than any window is not the zoom's doing - and the viewport put back."""
    zoomed = await page.evaluate(_LAYOUT_JS, False)
    fit = ZoomFit(zoom, int(viewport[0]), int(window[0]))
    if max(_spills(zoomed)) <= SPILL_TOLERANCE:
        return fit
    try:
        await page.set_viewport_size({"width": window[0], "height": window[1]})
        await asyncio.sleep(LAYOUT_SETTLE_SECONDS)
        unzoomed = await page.evaluate(_LAYOUT_JS, False)
    finally:
        await page.set_viewport_size({"width": viewport[0], "height": viewport[1]})
        await asyncio.sleep(LAYOUT_SETTLE_SECONDS)
    return ZoomFit(zoom, int(viewport[0]), int(window[0]), overflow=overflowed_by(unzoomed, zoomed), read_unzoomed=True)


def _side_by_side(a: list[float], b: list[float]) -> bool:
    """Whether boxes *a* and *b* (``[x, y, w, h]``) stand side by side: apart across, sharing half the shorter's height."""
    left, right = (a, b) if a[0] <= b[0] else (b, a)
    if left[0] + left[2] > right[0] + 1.0:
        return False
    shared = min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1])
    return shared >= 0.5 * min(a[3], b[3])


def layout_change(reference: dict, probed: dict) -> str | None:
    """What changed between the page's layout at its recorded width (*reference*) and at a narrower one (*probed*),
    both read by :data:`_LAYOUT_JS`; ``None`` when it is the same layout, drawn narrower.

    The same layout keeps every block it showed, scrolls no further sideways, and keeps side by side the columns
    it had side by side - a breakpoint hides a sidebar behind a menu, stacks the cards, or the page overflows: its
    document, or its main container cutting what it holds.
    """
    width = float(reference.get("width") or 1.0)
    before, after = reference.get("boxes") or [], probed.get("boxes") or []
    (document, main), (document_before, main_before) = _spills(probed), _spills(reference)
    if document > document_before + SPILL_TOLERANCE:
        return "the page scrolls sideways"
    if main > main_before + SPILL_TOLERANCE:
        return "its main container overflows its width"
    shown = [n for n, box in enumerate(before) if box]
    lost = [n for n in shown if n >= len(after) or not after[n]]
    if any(before[n][2] >= _COLUMN_SHARE * width for n in lost) or len(lost) >= 3:
        return f"{len(lost)} of its blocks are no longer shown (a menu or a panel collapsed)"
    columns = [n for n in shown if n not in lost and before[n][2] >= _COLUMN_SHARE * width]
    stacked = sum(
        1
        for i, a in enumerate(columns)
        for b in columns[i + 1 :]
        if _side_by_side(before[a], before[b]) and not _side_by_side(after[a], after[b])
    )
    if stacked:
        return f"blocks it shows side by side stack ({stacked} pair{'s' if stacked != 1 else ''})"
    return None


async def probe_layout(page: Any, viewport: tuple[int, int]) -> dict:
    """How far down the page in *page* keeps the layout it was recorded with at *viewport*.

    Run once the take is recorded, on the page it ends on: its layout blocks
    are marked at the recorded width, the viewport is narrowed step by step
    (:data:`LAYOUT_STEPS`, then halved :data:`LAYOUT_BISECTIONS` times between
    the last width kept and the first changed) and each width is compared to
    it (:func:`layout_change`), then the viewport is put back. ``holds_down_to``
    is the narrowest width with the same layout - a narrower viewport is only
    advised down to it (``insert_legibility``) -, ``changes_at`` the widest
    one where it changed and ``why``; ``None`` when it held down to the floor.
    A page that overflows below a minimum width says it exactly
    (:func:`min_width`): the width it overflowed at plus what it cut off, read
    once more to see it holds there.
    """
    width, height = viewport

    async def read(at: int | None) -> dict:
        if at is not None:
            await page.set_viewport_size({"width": at, "height": height})
            await asyncio.sleep(LAYOUT_SETTLE_SECONDS)
        return await page.evaluate(_LAYOUT_JS, at is None)

    reference = await read(None)
    kept, changed, why = width, None, None
    try:
        for share in LAYOUT_STEPS:
            at = max(LAYOUT_PROBE_FLOOR, round(width * share))
            if at >= kept:
                continue
            seen = await read(at)
            found = layout_change(reference, seen)
            if found:
                changed, why = at, found
                least = min_width(reference, seen)
                if least is not None and at < least <= kept:
                    again = layout_change(reference, await read(least))
                    if again is None:
                        kept, changed = least, least - 1
                    else:
                        changed, why = least, again
                break
            kept = at
            if at == LAYOUT_PROBE_FLOOR:
                break
        if changed is not None:
            for _ in range(LAYOUT_BISECTIONS):
                at = (kept + changed) // 2
                if at in (kept, changed):
                    break
                found = layout_change(reference, await read(at))
                if found:
                    changed, why = at, found
                else:
                    kept = at
    finally:
        await page.set_viewport_size({"width": width, "height": height})
    return {
        "version": LAYOUT_VERSION,
        "width": width,
        "holds_down_to": kept,
        "changes_at": changed,
        "why": why,
        "blocks": sum(1 for box in reference.get("boxes") or [] if box),
    }


def size_words(css: tuple[int, int], pixel_ratio: float, script: dict | None) -> str:
    """What a take's answer says of its size: its painted size first - the size every check of a source
    quotes - then the window and pixel ratio it was recorded at, and the CSS size a browser zoom laid its page out at:
    ``3200x1801 px painted (a 1600x900 window at pixel ratio 2, its page laid out at 842x474 by a browser zoom of 190%)``."""
    from app.services.video_studio.spec import browser_zoom, painted_size, recorded_ratio

    zoom = browser_zoom(script)
    width, height = painted_size(css, pixel_ratio)
    ratio = recorded_ratio(pixel_ratio, script)
    said = f"{width}x{height} px painted (a {round(width / ratio)}x{round(height / ratio)} window at pixel ratio {round(ratio, 2):g}"
    if zoom != 100:
        said += f", its page laid out at {css[0]}x{css[1]} by a browser zoom of {zoom}%"
    return said + ")"


def layout_words(layout: dict | None) -> str:
    """What a take's answer says of its probed *layout* (:func:`probe_layout`); empty when it was not probed."""
    if not isinstance(layout, dict) or not layout.get("holds_down_to"):
        return ""
    holds = int(layout["holds_down_to"])
    if not layout.get("changes_at"):
        return (
            f"Its page keeps this layout down to {holds} px (laid out: the window over its browser zoom), the "
            "narrowest probed once it was recorded."
        )
    return (
        f"Its page keeps this layout down to {holds} px (laid out: the window over its browser zoom); at "
        f"{int(layout['changes_at'])} px {layout.get('why') or 'its layout changes'} (probed once it was recorded): a "
        "narrower window or a higher browser zoom records another layout."
    )


# ── What a page offers to click ───────────────────────────────────

#: The ARIA roles a recording step acts on.
INTERACTIVE_ROLES = frozenset(
    {
        "button",
        "link",
        "menuitem",
        "menuitemcheckbox",
        "menuitemradio",
        "tab",
        "option",
        "checkbox",
        "radio",
        "switch",
        "textbox",
        "searchbox",
        "combobox",
        "listbox",
        "treeitem",
        "slider",
        "spinbutton",
    }
)
#: The most interactive elements read from a page.
MAX_PAGE_NAMES = 150
#: How many names a skipped step is told about.
NEAREST_NAMES = 3
_ARIA_LINE = re.compile(r'^\s*-\s+(?P<role>[a-z]+)(?:\s+"(?P<name>(?:[^"\\]|\\.)*)")?')
_WORD = re.compile(r"[^\W_]+", re.UNICODE)


def interactive_names(aria: str, *, limit: int = MAX_PAGE_NAMES) -> list[dict]:
    """The interactive elements of an ARIA snapshot (Playwright's ``aria_snapshot``): role, name, selector.

    The names are the accessible names a ``role`` + ``name`` step matches, the
    selector the one to write for it; each once, in page order.
    """
    found: dict[tuple[str, str], dict] = {}
    for line in (aria or "").splitlines():
        match = _ARIA_LINE.match(line)
        if not match or match["role"] not in INTERACTIVE_ROLES:
            continue
        name = (match["name"] or "").replace('\\"', '"').strip()
        if not name or (match["role"], name) in found:
            continue
        quoted = name.replace('"', '\\"')
        found[(match["role"], name)] = {"role": match["role"], "name": name, "selector": f'role={match["role"]}[name="{quoted}"]'}
        if len(found) >= limit:
            break
    return list(found.values())


def _words(text: str) -> list[str]:
    return [w.lower() for w in _WORD.findall(text or "")]


def _same_word(asked: str, seen: str) -> bool:
    """One word read as the other: equal, or one the beginning of the other (``flow`` / ``flows``)."""
    shorter, longer = sorted((asked, seen), key=len)
    return shorter == longer or (len(shorter) >= 3 and longer.startswith(shorter))


def closest_names(asked: str, names: list[dict], *, count: int = NEAREST_NAMES) -> list[dict]:
    """The interactive *names* closest to the text a step asked for, best first.

    Ranked like a search - every word of the asked text in the name first, then
    the most words, a word matching its plural or its stem - then by how alike
    the two read; a name sharing nothing with the asked text is never offered.
    """
    import difflib

    wanted = _words(asked)
    if not wanted:
        return []
    ranked = []
    for entry in names:
        seen = _words(entry["name"])
        hits = sum(1 for word in wanted if any(_same_word(word, other) for other in seen))
        alike = difflib.SequenceMatcher(None, " ".join(wanted), " ".join(seen)).ratio()
        if hits or alike >= 0.6:
            ranked.append(((hits == len(wanted), hits, alike), entry))
    ranked.sort(key=lambda item: item[0], reverse=True)
    return [entry for _score, entry in ranked[:count]]


def names_line(entries: list[dict]) -> str:
    return ", ".join(f'"{e["name"]}" ({e["role"]})' for e in entries)


async def page_names(page: Any) -> list[dict]:
    """The interactive elements the page shows now (:func:`interactive_names`); empty when it cannot be read."""
    try:
        aria = await page.locator("body").aria_snapshot(timeout=5000)
    except Exception as exc:  # a page navigating or closing: nothing to offer
        logger.debug("ARIA snapshot not read: %s", format_exception_message(exc))
        return []
    return interactive_names(aria)


#: What a step that missed its element is told of what its page held (:func:`filters_words`).
FILTERED = "a search term or a filter the page kept narrows what it lists"


async def current_url(page: Any) -> str:
    """Where *page* is now: its own ``location.href``, which a ``history.replaceState`` changes at once -
    ``page.url`` follows it only once the browser's notice of it reached the driver -, else ``page.url`` (navigating)."""
    try:
        href = await page.evaluate("() => location.href")
    except Exception:  # navigating, or no page to ask: the address it last committed
        return page.url
    return href if isinstance(href, str) and href else page.url


async def page_filters(page: Any) -> dict:
    """What the page shows it is filtered by now (:data:`_PAGE_FILTERS_JS`): ``{fields: [[label, value], ...],
    query: [[name, value], ...]}``; empty when it cannot be read."""
    try:
        held = await page.evaluate(_PAGE_FILTERS_JS)
    except Exception as exc:  # a page navigating or closing: nothing to say
        logger.debug("Page filters not read: %s", format_exception_message(exc))
        return {}
    return held if isinstance(held, dict) else {}


def filters_words(held: dict | None) -> str:
    """``its field "Search" "malware"; its address searchTerm=malware`` - what *held* (:func:`page_filters`) says; empty
    when the page held nothing."""
    held = held or {}
    fields = [entry for entry in held.get("fields") or [] if isinstance(entry, list) and len(entry) == 2 and entry[1]]
    query = [entry for entry in held.get("query") or [] if isinstance(entry, list) and len(entry) == 2]
    parts = []
    if fields:
        parts.append(", ".join(f'its field "{label}" "{value}"' if label else f'a field "{value}"' for label, value in fields))
    if query:
        parts.append("its address " + ", ".join(f"{name}={value}" for name, value in query))
    return "; ".join(parts)


def step_label(step: TakeStep) -> str | None:
    """How a step is named in the action log the agent and the editor read.

    A ``type`` step's ``text`` is what it types - a password, a code - never
    its name: it is named by its label, its target or the field.
    """
    typed = step.action == "type"
    at = step.at
    return (
        step.label
        or step.name
        or (None if typed else step.text)
        or step.selector
        or (step.node.label if step.node is not None else None)
        or (f"({at.x:g}, {at.y:g})" + (f" in {at.within}" if at.within else "") if at is not None else None)
        or step.url
        or step.key
        or ("a field" if typed else None)
    )


#: Whether the element keys go to now hides what is typed: a ``type`` step without a locator types there.
_FOCUSED_HIDDEN_INPUT_JS = (
    "() => document.activeElement instanceof HTMLInputElement && document.activeElement.type === 'password'"
)


async def _is_password_field(locator: Any) -> bool:
    """Whether the element a ``type`` step types into hides what is typed (never fails the step)."""
    try:
        return bool(await locator.evaluate("(el) => el instanceof HTMLInputElement && el.type === 'password'"))
    except Exception:
        return False


def _asked_text(step: TakeStep) -> str:
    return step.name or step.text or step.label or step.selector or ""


async def _near(exc: BaseException, step: TakeStep, page: Any) -> list[dict]:
    """What the page offered closest to what a failed step asked for: the graph's labels nearest a node step's
    (``pointer.TargetNotFoundError``), else the interactive names nearest an element step's."""
    if isinstance(exc, pointer.TargetNotFoundError):
        return exc.near
    if step.action in LOCATOR_ACTIONS and not step.on_canvas:
        return closest_names(_asked_text(step), await page_names(page))
    return []


@dataclass
class PageSnapshot:
    """A page of the signed-in application looked at: where it landed, what it offers, a screenshot."""

    url: str
    title: str
    names: list[dict]
    png: bytes
    #: The session the profile signed in again with, and its probe, to be saved; ``None`` when it was signed in.
    storage_state: dict | None = None
    probe: Any = None
    #: Seconds the page took to settle once it opened (``page_settle``); ``None``: not within :attr:`watched`.
    settled_after: float | None = None
    watched: float = 0.0
    #: What kept it from settling, when it did not.
    unsettled: list[str] = field(default_factory=list)
    #: The page state reset it opened in, as a take's (:func:`reset_page`); ``None``: none ran.
    opened: dict | None = None

    def settle_words(self) -> str:
        return page_settle.snapshot_words(self.settled_after, self.watched, self.unsettled)


async def snapshot_page(
    profile: ProfileSnapshot,
    url: str,
    *,
    viewport: tuple[int, int] = (1600, 900),
    reset_state: bool | PageReset = True,
) -> PageSnapshot:
    """Open *url* in the profile's saved session, as a recording's pre-flight does, and look at it: nothing typed.

    The session is checked first and signed in again when the profile can do
    it alone (``signin.ensure_signed_in``), else :class:`ProfileNotAuthenticatedError`;
    then *url* opens in the clean page state a take opens in
    *(reset_state*, :func:`reset_page`), so the names it reads are those a take
    finds. The page is watched until it settled (:func:`watch_page`), at
    most ``page_settle.SNAPSHOT_SECONDS``, and how long that took once it opened
    is kept: what a wait after the step that opens it holds.
    """
    from app.services.browser_profiles import signin

    options = profile.options or {}
    async with profile_browser(
        policy=profile.policy,
        viewport=viewport,
        pixel_ratio=1,
        storage_state=profile.storage_state,
        color_scheme=options.get("color_scheme"),
        locale=options.get("locale") or "en-US",
        timezone=options.get("timezone") or "UTC",
    ) as rb:
        await rb.context.add_init_script(_SETTLE_WATCH_JS)
        renewed = None
        if profile.needs_sign_in:
            try:
                renewed = await signin.ensure_signed_in(rb, profile)
            except signin.SessionNotSignedInError as exc:
                raise ProfileNotAuthenticatedError(exc.probe, exc.detail) from exc
        opened = time.time()
        reset = page_reset(reset_state)
        state_reset = None
        if reset is not None and profile.storage_state:
            state_reset = await reset_page(rb, url, reset, profile.keep_storage_keys)
        else:
            await rb.goto(url)
        with contextlib.suppress(Exception):  # read the page as it is
            await rb.page.wait_for_load_state("domcontentloaded", timeout=10_000)
        with contextlib.suppress(Exception):  # long-polling apps never go idle
            await rb.page.wait_for_load_state("networkidle", timeout=3000)
        watch = page_settle.PageWatch(began=0.0)
        settled, watched = await watch_page(
            rb.page,
            watch,
            origin=opened,
            viewport_width=viewport[0],
            frame=await _screenshots(rb, origin=opened),
            seconds=0.0,
            extra=page_settle.SNAPSHOT_SECONDS,
        )
        names = await page_names(rb.page)
        png = await rb.page.screenshot(type="png")
        title = ""
        with contextlib.suppress(Exception):
            title = await rb.page.title()
        state, probe = None, None
        if renewed is not None:
            with contextlib.suppress(Exception):  # the look stands without the session saved
                state, probe = await signin.save_session(rb, profile)
        return PageSnapshot(
            url=await current_url(rb.page),
            title=title,
            names=names,
            png=png,
            storage_state=state,
            probe=probe,
            settled_after=settled,
            watched=watched,
            unsettled=[] if settled is not None else watch.moving(watched),
            opened=state_reset,
        )


async def _screenshots(rb: ProfileBrowser, *, origin: float) -> FrameSource | None:
    """A frame source taking a screenshot of the page each time it is asked: a look runs no screencast. CDP's
    own capture, which puts nothing in the page the settle watch would read as a change."""
    try:
        cdp = await rb.context.new_cdp_session(rb.page)
    except Exception as exc:  # the canvases go unread; the indicators and the DOM still tell
        logger.debug("No screenshot session for the page's canvases: %s", format_exception_message(exc))
        return None
    last: list[float] = []

    async def frame() -> tuple[float, bytes, float] | None:
        try:
            shot = await cdp.send("Page.captureScreenshot", {"format": "jpeg", "quality": 80})
        except Exception:  # navigating: no frame this time
            return None
        t = time.time() - origin
        gap = t - last[-1] if last else 0.0
        last[:] = [t]
        return t, base64.b64decode(shot.get("data") or ""), gap

    return frame


#: The most frames a contact sheet shows.
SHEET_MOST = 9


def contact_sheet(frames_dir: str, picks: list[tuple[float, str, str]], *, out_path: str) -> None:
    """A grid of frames (``(t, file name, label)``) the agent can look at to check the take."""
    from PIL import Image, ImageDraw, ImageFont

    if not picks:
        return
    thumbs = []
    for t, name, label in picks[:SHEET_MOST]:
        with Image.open(Path(frames_dir) / name) as image:
            thumb = image.convert("RGB")
            thumb.thumbnail((640, 360))
            canvas = Image.new("RGB", (640, 392), (14, 16, 24))
            canvas.paste(thumb, ((640 - thumb.width) // 2, 0))
            draw = ImageDraw.Draw(canvas)
            try:
                font = ImageFont.load_default(size=16)
            except TypeError:  # Pillow < 10.1
                font = ImageFont.load_default()
            draw.text((10, 366), f"{t:5.1f}s  {label}"[:70], fill=(220, 225, 235), font=font)
            thumbs.append(canvas)
    columns = 3 if len(thumbs) > 4 else 2 if len(thumbs) > 1 else 1
    rows = math.ceil(len(thumbs) / columns)
    sheet = Image.new("RGB", (columns * 640 + (columns + 1) * 8, rows * 392 + (rows + 1) * 8), (6, 7, 12))
    for i, thumb in enumerate(thumbs):
        r, c = divmod(i, columns)
        sheet.paste(thumb, (8 + c * 648, 8 + r * 400))
    sheet.save(out_path, "PNG", optimize=True)


def step_result_seconds(event: dict, duration: float) -> float:
    """When a step's result shows: half a second after it ended - the frame the contact sheet shows for it."""
    return min(float(event.get("t1") or event.get("ta") or 0.0) + 0.5, duration)


#: The most skipped steps a contact sheet shows the frame of, besides the actions it picks.
SHEET_SKIPPED = 3


def _frame_at(frames: list[tuple[float, str]], t: float) -> str:
    """The frame painted last at *t* (the first one before any)."""
    import bisect

    i = min(max(bisect.bisect_right([f[0] for f in frames], t) - 1, 0), len(frames) - 1)
    return frames[i][1]


def sheet_frames(frames: list[tuple[float, str]], events: list[dict], duration: float, count: int = 6) -> list[tuple[float, str, str]]:
    """Frames right after the main actions (else evenly spread), with what happened, and the frame where each skipped
    step gave up (:data:`SHEET_SKIPPED` at most): the page it missed its element on."""
    if not frames:
        return []
    picks: list[tuple[float, str, str]] = []
    actions = [e for e in events if e.get("action") not in ("wait", "marker") and e.get("ok", True)]
    if actions:
        step = max(1, len(actions) // count)
        for event in actions[::step][:count]:
            t = step_result_seconds(event, duration)
            picks.append((t, _frame_at(frames, t), f"step {event.get('step')} {event.get('action')}: {event.get('label') or ''}"))
    if len(picks) < 2:
        picks = [(duration * i / (count - 1), _frame_at(frames, duration * i / (count - 1)), "") for i in range(count)]
    for event in [e for e in events if not e.get("ok", True)][:SHEET_SKIPPED]:
        t = min(float(event.get("ta") or event.get("t0") or 0.0), duration)
        picks.append((t, _frame_at(frames, t), f"step {event.get('step')} skipped: {event.get('label') or ''}"))
    return sorted(picks, key=lambda pick: pick[0])


def pick_sheet_frames(result: CaptureResult, count: int = 6) -> list[tuple[float, str, str]]:
    """:func:`sheet_frames` of a recorded take."""
    return sheet_frames(result.frames, result.events, result.duration, count)


def failure_sheet_frames(failed: StepFailedError, count: int = 5) -> list[tuple[float, str, str]]:
    """The contact sheet of a take a step failed: frames after its steps up to there, then the frame where the
    step failed - the page it missed its element on -, then what the step attached (the canvas a node was
    looked for on), every one of them within the :data:`SHEET_MOST` a sheet shows."""
    if not failed.frames:
        return []
    picks = sheet_frames(failed.frames, failed.events, failed.at, count) if failed.events else []
    ending = [(failed.at, _frame_at(failed.frames, failed.at), f"step {failed.step} failed: {failed.label}")]
    ending += [(failed.at, name, said) for name, said in failed.attached]
    return [*picks[: max(0, SHEET_MOST - len(ending))], *ending]
