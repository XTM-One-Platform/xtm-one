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

"""The compositor page: one HTML document that draws any frame of a timeline.

Loaded in headless Chromium by ``compositor.py``, which hands it the timeline
(``timeline.plan``'s document) through ``window.__xtm.load`` and asks for each
frame with ``window.__xtm.render(index)``. Everything on screen is a pure
function of the frame's time - no CSS animation, no timer, no network: the
frames of a recording or a clip, the slides and images are fetched from the
``https://studio.render/`` origin the driver answers itself, the fonts are
inlined. So frame N always looks the same, whichever page draws it, which is
what lets a render be split into chunks drawn in parallel.

What it draws: a branded animated background; recordings in a browser window
(URL bar, rounded corners, shadow) zoomed by the virtual camera as a whole,
with a drawn cursor, click ripples, spotlights on highlighted elements and
callouts anchored to them; kinetic title and outro cards; statements in big
type revealed line by line (accent words, a counted figure, chips) over the
brand backdrop or a dimmed video or image; stills with a Ken
Burns motion and, for an animated still, its effects drawn on the real image
without any model (panels lifted in parallax, a light sweep, a pulse on a
region); lower thirds, badges, captions and pictures (a logo, a mark:
image overlays); transitions between scenes;
burned-in subtitles; a watermark; fades from and to black; motion scenes:
text split into letters, words or lines, shapes and paths drawn
on and morphed, pictures, Lottie animations and particles, each moved by its
keyframes and presets under a 3D camera. Every choreography reads the
brand's motion tokens (``theme.motion``, ``motion_design.resolve_motion``).

All user text is set with ``textContent``, never parsed as HTML.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.services.video_studio.figure_fit import page_stands
from app.services.video_studio.motion_design import resolve_motion
from app.services.video_studio.screens import key_matte_constants
from app.services.video_studio.spec import ACCENT_PATTERN, INSERT_FADE_SECONDS
from app.services.video_studio.timeline import HELD_END_SLACK, STILL_HOST_ZOOM

ORIGIN = "https://studio.render"

#: The Lottie player a motion scene's Lottie elements are seeked with: lottie-web's ``lottie_light`` build
#: (MIT, ``vendor/lottie-web-LICENSE.md``), its SVG renderer alone - it evaluates no expression and loads nothing
#: the page does not hand it. Inlined before the page's script, so the page loads nothing from the network.
LOTTIE_VERSION = "5.13.0"
LOTTIE_JS = (Path(__file__).with_name("vendor") / "lottie_light.min.js").read_text(encoding="utf-8")

#: The subtitles as the page sets them (``#caption`` in :data:`PAGE_CSS`, ``layCaptions`` in
#: :data:`PAGE_JS`): their font size and line height, at most two lines, each style's vertical
#: padding, how far from the frame's edge they stand, and the gap a scene keeps from them (``reserveOf``).
CAPTION_FONT_PX = 34
CAPTION_LINE_HEIGHT = 1.42
CAPTION_MAX_LINES = 2
CAPTION_PADDING_PX = {"pill": 12, "bar": 36, "outline": 0}
CAPTION_EDGE_OFFSET = {"top": 0.06, "bottom": 0.065}
CAPTION_GAP = 0.025


def caption_extent(height: int, *, style: str, position: str) -> float:
    """How far from its edge of the frame the tallest subtitle the page draws reaches, in pixels: its pill's
    top for subtitles at the bottom - two lines at the full font size, the style's padding, the edge's offset."""
    edge = "top" if position == "top" else "bottom"
    box = CAPTION_MAX_LINES * CAPTION_FONT_PX * CAPTION_LINE_HEIGHT + CAPTION_PADDING_PX.get(style, CAPTION_PADDING_PX["pill"])
    return round(height * CAPTION_EDGE_OFFSET[edge] + box, 1)


def caption_reserve(height: int, *, style: str, position: str) -> dict[str, float]:
    """The band a picture drawn for the whole frame keeps clear of subtitles: ``{"top": px, "bottom": px}``.

    The page measures each cue and keeps what it draws clear of the tallest
    shown during a scene (``reserveOf``); a picture drawn before the page runs
    cannot be measured, so it keeps clear of the tallest the page draws
    (:func:`caption_extent`) and a gap.
    """
    edge = "top" if position == "top" else "bottom"
    band = round(caption_extent(height, style=style, position=position) + height * CAPTION_GAP, 1)
    return {"top": band if edge == "top" else 0.0, "bottom": band if edge == "bottom" else 0.0}

PAGE_CSS = r"""
* { box-sizing: border-box; margin: 0; padding: 0; }
html, body { width: 100%; height: 100%; overflow: hidden; background: #000; }
body { -webkit-font-smoothing: antialiased; text-rendering: geometricPrecision; }
#stage { position: absolute; left: 0; top: 0; overflow: hidden; background: var(--bg); }
.layer { position: absolute; inset: 0; overflow: hidden; display: none; will-change: transform, opacity; }
.bg { position: absolute; inset: -10%; background: var(--bg); }
.blob { position: absolute; border-radius: 50%; will-change: transform; }
.grid { position: absolute; inset: 0; opacity: .55;
  background-image: linear-gradient(var(--line) 1px, transparent 1px), linear-gradient(90deg, var(--line) 1px, transparent 1px);
  background-size: 64px 64px; mask-image: radial-gradient(ellipse at 50% 45%, #000 20%, transparent 75%); }
.cam { position: absolute; left: 0; top: 0; transform-origin: 0 0; will-change: transform; }
.win { position: absolute; overflow: hidden; background: var(--surface); }
.win.shadow { box-shadow: 0 50px 120px rgba(0,0,0,.55), 0 18px 40px rgba(0,0,0,.35); }
.win .border { position: absolute; inset: 0; border-radius: inherit; border: 1px solid rgba(255,255,255,.10); pointer-events: none; }
.bar { position: absolute; left: 0; right: 0; top: 0; display: flex; align-items: center; gap: 14px; padding: 0 18px;
  background: var(--chrome); border-bottom: 1px solid rgba(255,255,255,.06); }
.dots { display: flex; gap: 8px; }
.dots i { width: 12px; height: 12px; border-radius: 50%; display: block; }
.url { flex: 1; display: flex; align-items: center; gap: 8px; margin: 0 auto; max-width: 62%; height: 28px; padding: 0 14px;
  border-radius: 8px; background: var(--chrome-field); color: var(--muted); font: 500 15px var(--body-font); white-space: nowrap; overflow: hidden; }
.url span { overflow: hidden; text-overflow: ellipsis; }
.url b { color: var(--fg); font-weight: 500; }
/* No fill of its own under the footage: an edge the camera puts between two pixels is blended with
   what lies under it, so a fill unlike the window's surface shows as a seam that flickers frame to frame
   while the camera zooms. The preview's video element fills the viewport as the render's frame image does. */
.vp { position: absolute; left: 0; overflow: hidden; }
.vp img, .vp video { position: absolute; left: 0; top: 0; width: 100%; height: 100%; object-fit: fill; image-rendering: auto; }
.fx { position: absolute; inset: 0; pointer-events: none; }
.cursor { position: absolute; left: 0; top: 0; transform-origin: 0 0; filter: drop-shadow(0 3px 6px rgba(0,0,0,.45)); }
.ripple { position: absolute; border-radius: 50%; border: 3px solid var(--accent); background: color-mix(in srgb, var(--accent) 22%, transparent); }
.spot { position: absolute; border-radius: 14px; border: 2px solid color-mix(in srgb, var(--accent) 80%, white); }
.callout { position: absolute; max-width: 560px; padding: 18px 22px; border-radius: 16px; color: var(--fg);
  background: color-mix(in srgb, var(--surface) 94%, transparent); border: 1px solid color-mix(in srgb, var(--accent) 45%, transparent);
  box-shadow: 0 24px 60px rgba(0,0,0,.45); font: 600 26px/1.25 var(--heading-font); }
.callout.accent { background: var(--accent); color: var(--on-accent); border-color: transparent; }
.callout.light { background: #ffffff; color: #0b1020; border-color: rgba(0,0,0,.08); }
.callout.dark { background: #0b0f19; color: #f4f6fb; }
.callout small { display: block; margin-top: 6px; font: 400 19px/1.35 var(--body-font); opacity: .78; }
.callout .tip { position: absolute; width: 18px; height: 18px; transform: rotate(45deg); background: inherit; border: inherit; }
.lower { position: absolute; left: 5%; bottom: 11%; display: flex; gap: 18px; align-items: stretch; padding: 18px 30px 18px 18px;
  border-radius: 18px; background: color-mix(in srgb, var(--surface) 90%, transparent); border: 1px solid rgba(255,255,255,.08);
  box-shadow: 0 24px 60px rgba(0,0,0,.45); }
.lower .bar2 { width: 6px; border-radius: 3px; background: linear-gradient(var(--grad-from), var(--grad-to)); }
.lower h3 { font: 700 36px/1.1 var(--heading-font); color: var(--fg); }
.lower p { margin-top: 6px; font: 400 23px/1.3 var(--body-font); color: var(--muted); }
.badge { position: absolute; display: flex; align-items: center; gap: 10px; padding: 10px 18px; border-radius: 999px;
  background: color-mix(in srgb, var(--accent) 18%, var(--surface)); border: 1px solid color-mix(in srgb, var(--accent) 60%, transparent);
  color: var(--fg); font: 700 20px var(--heading-font); letter-spacing: .06em; text-transform: uppercase; }
.badge i { width: 10px; height: 10px; border-radius: 50%; background: var(--accent); box-shadow: 0 0 16px var(--accent); }
.bigcap { position: absolute; left: 8%; right: 8%; top: 64%; text-align: center; color: var(--fg);
  font: 800 64px/1.08 var(--heading-font); letter-spacing: -.02em; text-shadow: 0 10px 40px rgba(0,0,0,.6); }
.ovimg { position: absolute; object-fit: contain; object-position: center; }
.inshost { position: absolute; left: 0; top: 0; pointer-events: none; transform-origin: 50% 50%; }
.ins, .insglow { position: absolute; pointer-events: none; }
.insglow { mix-blend-mode: screen; }
.insprev { position: absolute; left: 0; top: 0; overflow: hidden; transform-origin: 0 0; background: transparent; }
.insprev img, .insprev video { position: absolute; left: 0; top: 0; width: 100%; height: 100%; }
.card { position: absolute; display: flex; flex-direction: column; justify-content: center; }
.card .logo { height: 50px; width: auto; max-width: 46%; margin-bottom: 42px; object-fit: contain; object-position: left center; align-self: flex-start; }
.card.center .logo { align-self: center; object-position: center; }
.kicker { font: 700 22px var(--heading-font); letter-spacing: .22em; text-transform: uppercase; color: var(--accent); margin-bottom: 26px; }
.title { font-family: var(--heading-font); font-weight: 800; letter-spacing: -.035em; line-height: 1.02; color: var(--fg); text-wrap: balance; }
.title .w { display: inline-block; white-space: pre; will-change: transform, opacity, filter; }
/* A gradient through the glyphs is painted inside its element's box only: an inline box, as tall as the
   font's ascenders and descenders - never the inline-block word or the block figure, as tall as their line - widened
   for the ink past them (a ring over a capital, the hook of a j, the tail of an f) by a padding its margins take back.
   None below: the descenders stand inside the box, and a bottom padding counts in the height fitText reads. */
.grad { display: inline; padding: .15em .15em 0; margin: 0 -.15em;
  background: linear-gradient(100deg, var(--grad-from), var(--grad-to)); -webkit-background-clip: text; background-clip: text; color: transparent; }
.subtitle { margin-top: 30px; font: 400 32px/1.35 var(--body-font); color: var(--muted); max-width: 1100px; text-wrap: pretty; }
.rule { height: 4px; border-radius: 2px; margin-top: 34px; background: linear-gradient(90deg, var(--grad-from), var(--grad-to)); transform-origin: 0 50%; }
.tile { position: absolute; will-change: transform, opacity;
  background-image: linear-gradient(135deg, rgba(255,255,255,.10), rgba(255,255,255,0) 45%, rgba(0,0,0,.12)); }
.cta { display: inline-flex; align-self: flex-start; margin-top: 44px; padding: 20px 38px; border-radius: 16px; background: var(--accent);
  color: var(--on-accent); font: 700 30px var(--heading-font); box-shadow: 0 18px 50px color-mix(in srgb, var(--accent) 45%, transparent); }
.card.center .cta { align-self: center; }
.ctaurl { margin-top: 22px; font: 500 24px var(--body-font); color: var(--muted); letter-spacing: .02em; }
.card.center { align-items: center; text-align: center; }
.still { position: absolute; overflow: hidden; }
.ph { position: absolute; inset: 0; display: flex; align-items: center; justify-content: center; padding: 0 8%; background: var(--surface); color: var(--muted); font: 600 40px/1.3 var(--body-font); text-align: center; }
.ph.failed { color: #ffb3a7; box-shadow: inset 0 0 0 4px rgba(255, 99, 82, .55); }
/* One composited layer for a statement's whole text, never one per line or word:
   over a moving backdrop, the software compositor of a render pays for every layer on every frame. */
.stmt { will-change: transform; }
.stmt .lines { display: flex; flex-direction: column; gap: .1em; font-family: var(--heading-font); font-weight: 700; letter-spacing: -.025em; line-height: 1.1; color: var(--fg); }
.stmt .sline { text-wrap: balance; }
.stmt .w { display: inline-block; white-space: pre; }
.stmt .w.hl { background: none; color: var(--accent); }
.stmt .figure { margin-bottom: 22px; font-family: var(--heading-font); font-weight: 800; letter-spacing: -.04em; line-height: 1; font-variant-numeric: tabular-nums; }
.stmt .ssub { margin-top: 26px; font: 400 34px/1.35 var(--body-font); color: var(--muted); max-width: 1300px; text-wrap: pretty; }
.stmt .ssub + .ssub { margin-top: 8px; }
.chips { display: flex; flex-wrap: wrap; gap: 14px; margin-top: 44px; }
.card.center .chips { justify-content: center; }
.chip { padding: 11px 24px; border-radius: 999px; border: 1.5px solid color-mix(in srgb, var(--accent) 70%, transparent);
  background: color-mix(in srgb, var(--accent) 12%, transparent); color: var(--fg); font: 600 22px var(--heading-font); letter-spacing: .08em; text-transform: uppercase; }
.backdrop { position: absolute; inset: 0; overflow: hidden; }
.backdrop img, .backdrop video { position: absolute; left: 0; top: 0; width: 100%; height: 100%; object-fit: cover; transform-origin: 50% 50%; }
.dim { position: absolute; inset: 0; }
.still img, .still video { position: absolute; left: 0; top: 0; width: 100%; height: 100%; transform-origin: 50% 50%; will-change: transform; }
.still.card2 { border-radius: 22px; box-shadow: 0 40px 100px rgba(0,0,0,.5); }
/* A shot's footage and what is mapped onto it: drifts in while the shot holds its last frame. */
.shot { position: absolute; inset: 0; transform-origin: 50% 50%; }
.fxstage { position: absolute; inset: 0; transform-origin: 50% 50%; will-change: transform; }
.fxstage .socket { position: absolute; background: #000; opacity: 0; border-radius: 8px; }
.fxstage .lift { position: absolute; overflow: hidden; border-radius: 8px; will-change: transform; }
.fxstage .lift img { will-change: auto; }
.fxstage .sweep { position: absolute; inset: 0; overflow: hidden; }
.fxstage .sweep i { position: absolute; left: 0; top: -60%; height: 220%; opacity: 0; mix-blend-mode: screen; }
.fxstage .pulse { position: absolute; opacity: 0; border-radius: 12px; mix-blend-mode: screen;
  background: color-mix(in srgb, var(--accent) 20%, transparent);
  box-shadow: 0 0 0 3px var(--accent), 0 0 42px 8px color-mix(in srgb, var(--accent) 55%, transparent); }
.fxstage .pulse.round { border-radius: 50%; }
/* A title or a motion text cut into letters: each letter moves, its word stays whole on its line. Letters are
   never their own composited layer: a title has dozens, and the software compositor pays for every layer. */
.ww { display: inline-block; white-space: pre; }
.w.l { display: inline-block; white-space: pre; will-change: auto; }
/* A word risen into view from under its baseline (the `mask` style): its window keeps the descenders. */
.mk { display: inline-block; overflow: hidden; vertical-align: top; padding: 0 .05em .18em; margin: 0 -.05em -.18em; }
.mk > .wi { display: inline-block; }
.mview { position: absolute; inset: 0; perspective-origin: 50% 50%; }
.mworld { position: absolute; left: 0; top: 0; transform-origin: 50% 50%; transform-style: preserve-3d; }
.mel { position: absolute; left: 0; top: 0; transform-origin: 50% 50%; }
.mel.masked { overflow: hidden; padding-bottom: .12em; }
.mtext .mtx { line-height: 1.12; }
.mtext .w { display: inline-block; white-space: pre; }
.mtext .hl { color: var(--accent); }
.mtext .mtt { display: inline; }
.mkr { display: inline-block; vertical-align: middle; margin: 0 .55em .08em 0; }
.mkr.dot { width: .42em; height: .42em; border-radius: 50%; background: var(--accent);
  box-shadow: 0 0 .6em color-mix(in srgb, var(--accent) 60%, transparent); }
.mkr.dash { width: .7em; height: .12em; border-radius: .06em; background: var(--accent); }
.mkr.check { width: .8em; height: .8em; }
.mimg img { width: 100%; height: 100%; display: block; }
.mshine { position: absolute; inset: 0; pointer-events: none; mix-blend-mode: screen;
  -webkit-mask-size: contain; mask-size: contain; -webkit-mask-repeat: no-repeat; mask-repeat: no-repeat; -webkit-mask-position: center; mask-position: center; }
.mlot svg { display: block; }
#caption { position: absolute; left: 50%; display: none; transform: translateX(-50%); max-width: 82%; text-align: center; }
#caption span { display: inline; font: 600 34px/1.42 var(--body-font); color: #fff; -webkit-box-decoration-break: clone; box-decoration-break: clone; }
#caption.cap-pill span { padding: 6px 18px; border-radius: 12px; background: rgba(6,8,14,.78); }
#caption.cap-bar { left: 0; right: 0; transform: none; max-width: none; padding: 18px 9%; background: rgba(6,8,14,.72); }
#caption.cap-outline span { text-shadow: 0 0 3px #000, 0 0 3px #000, 0 2px 8px #000; font-weight: 700; }
#watermark { position: absolute; height: 44px; width: auto; opacity: .88; display: none; }
#fade { position: absolute; inset: 0; background: #000; opacity: 0; pointer-events: none; }
#flash { position: absolute; inset: 0; background: #fff; opacity: 0; pointer-events: none; }
"""

PAGE_JS = r"""
(() => {
const ORIGIN = 'https://studio.render';
// ``media`` is set only by the editor's live preview (``doc.preview_media``):
// the recordings and clips then play from video elements; a render
// leaves it null and draws the frames the driver serves.
const S = { doc: null, W: 1920, H: 1080, fps: 60, T: 0, scenes: [], built: new Map(), loads: [], media: null, playing: false, muted: false, sources: [],
  capInfo: [], capEdge: 'bottom', capOffset: 0, wm: null,
  // The preview's media across documents ("The preview's media" below).
  ids: {}, held: new Map(), pool: new Map(), drawnFor: new Map(), shownImg: {}, loadedUrls: new Set(), retired: [], drop: [],
  ahead: false, onLost: null, job: null };
const clamp = (v, a, b) => Math.min(b, Math.max(a, v));
const lerp = (a, b, t) => a + (b - a) * t;
const inOut = t => t < .5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2;
const out3 = t => 1 - Math.pow(1 - t, 3);
// A camera move: smootherstep, its peak speed 1.875x its mean (3x for inOut), still at both ends.
const glide = t => t * t * t * (t * (6 * t - 15) + 10);
const back = t => { const c1 = 1.70158, c3 = c1 + 1; return 1 + c3 * Math.pow(t - 1, 3) + c1 * Math.pow(t - 1, 2); };
const prog = (t, s, d) => d <= 0 ? (t >= s ? 1 : 0) : clamp((t - s) / d, 0, 1);
// spec.INSERT_FADE_SECONDS and timeline.HELD_END_SLACK.
const INSERT_FADE = __INSERT_FADE__, HELD_END_SLACK = __HELD_END_SLACK__;
// timeline.STILL_HOST_ZOOM: a picture's screen inserts drawn at the size its motion shows it at closest.
const STILL_HOST_ZOOM = __STILL_HOST_ZOOM__;
// screens.key_matte's constants: the preview tests a keyed insert's key with the render's own arithmetic (keyMatte).
const KEY_MATTE = __KEY_MATTE__;
// ── Motion: the brand's tokens and its easings ───────────────────
// Every duration, easing, stagger and distance a choreography draws with is
// a token of the document (`theme.motion`, `motion_design.resolve_motion`);
// the page decides none of its own. `precise` holds the numbers it drew with
// before tokens existed. An easing is a name, `cubic-bezier(x1, y1, x2, y2)`
// or `spring(stiffness, damping)` - a damped spring solved in closed form and
// fitted to the move's duration, so it is a pure function of time too.
const DEFAULT_MOTION = __DEFAULT_MOTION__;
const FIGURE_STANDS = __FIGURE_STANDS__;
const MO = () => (S.doc && S.doc.theme && S.doc.theme.motion) || DEFAULT_MOTION;

function cubicBezier(x1, y1, x2, y2) {
  const cx = 3 * x1, bx = 3 * (x2 - x1) - cx, ax = 1 - cx - bx;
  const cy = 3 * y1, by = 3 * (y2 - y1) - cy, ay = 1 - cy - by;
  const sx = (u) => ((ax * u + bx) * u + cx) * u, sy = (u) => ((ay * u + by) * u + cy) * u;
  const dx = (u) => (3 * ax * u + 2 * bx) * u + cx;
  return (x) => {
    if (x <= 0) return 0;
    if (x >= 1) return 1;
    let u = x;
    for (let i = 0; i < 8; i++) {
      const err = sx(u) - x, d = dx(u);
      if (Math.abs(err) < 1e-6) return sy(u);
      if (Math.abs(d) < 1e-6) break;
      u -= err / d;
    }
    let lo = 0, hi = 1; u = x;
    for (let i = 0; i < 40; i++) { const v = sx(u); if (Math.abs(v - x) < 1e-6) break; if (v < x) lo = u; else hi = u; u = (lo + hi) / 2; }
    return sy(u);
  };
}

// A spring from 0 to 1 (unit mass), its settling time mapped onto the move: overshoot and bounce follow its damping.
function springEase(stiffness, damping) {
  const w0 = Math.sqrt(stiffness), zeta = damping / (2 * w0);
  let at;
  if (zeta < 1) {
    const wd = w0 * Math.sqrt(1 - zeta * zeta);
    at = (s) => 1 - Math.exp(-zeta * w0 * s) * (Math.cos(wd * s) + (zeta * w0 / wd) * Math.sin(wd * s));
  } else {
    at = (s) => 1 - Math.exp(-w0 * s) * (1 + w0 * s);
  }
  // Settled when it stays within 0.1% of its target.
  let settle = 0.05;
  for (let s = 0.05; s < 20; s += 0.01) {
    let still = true;
    for (let k = 0; k < 12 && still; k++) if (Math.abs(at(s + k * 0.02) - 1) > 0.001) still = false;
    if (still) { settle = s; break; }
    settle = s;
  }
  return (p) => (p <= 0 ? 0 : p >= 1 ? 1 : at(p * settle));
}

const EASES = new Map(Object.entries({
  linear: (t) => t,
  in: (t) => t * t * t,
  out: out3,
  in_out: inOut,
  out_back: back,
  out_expo: (t) => (t >= 1 ? 1 : 1 - Math.pow(2, -10 * t)),
  in_out_expo: (t) => (t <= 0 ? 0 : t >= 1 ? 1 : t < 0.5 ? Math.pow(2, 20 * t - 10) / 2 : (2 - Math.pow(2, -20 * t + 10)) / 2),
  out_quint: (t) => 1 - Math.pow(1 - t, 5),
  snap: cubicBezier(0.3, 1.25, 0.5, 1),
  spring: springEase(170, 20),
  spring_soft: springEase(120, 22),
  spring_bouncy: springEase(260, 12),
}));
const BRAND_EASES = new Set(['enter', 'exit', 'move', 'emphasis']);

// The function of an easing named as the spec and the tokens write it; the brand's own names read the tokens.
function easeOf(name, depth) {
  const key = String(name || 'out');
  if (BRAND_EASES.has(key)) return (depth || 0) > 2 ? out3 : easeOf((MO().ease || {})[key] || 'out', (depth || 0) + 1);
  const known = EASES.get(key);
  if (known) return known;
  let made = null;
  const b = /^cubic-bezier\(([^,]+),([^,]+),([^,]+),([^)]+)\)$/.exec(key.replace(/\s+/g, ''));
  if (b) made = cubicBezier(...b.slice(1).map(Number));
  const s = /^spring\(([^,]+),([^)]+)\)$/.exec(key.replace(/\s+/g, ''));
  if (s) made = springEase(Number(s[1]), Number(s[2]));
  made = made || out3;
  EASES.set(key, made);
  return made;
}

const el = (tag, cls, parent) => { const e = document.createElement(tag); if (cls) e.className = cls; if (parent) parent.appendChild(e); return e; };
const SVG_NS = 'http://www.w3.org/2000/svg';
const svgEl = (tag, attrs, parent) => {
  const e = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs || {})) e.setAttribute(k, String(v));
  if (parent) parent.appendChild(e);
  return e;
};

function mapTime(points, x) {
  if (!points || !points.length) return x;
  if (x <= points[0][0]) return points[0][1];
  const last = points[points.length - 1];
  if (x >= last[0]) return last[1];
  let lo = 0, hi = points.length - 1;
  while (hi - lo > 1) { const mid = (lo + hi) >> 1; if (points[mid][0] <= x) lo = mid; else hi = mid; }
  const a = points[lo], b = points[hi];
  return b[0] === a[0] ? b[1] : a[1] + (b[1] - a[1]) * (x - a[0]) / (b[0] - a[0]);
}

function camera(frames, t) {
  if (!frames || !frames.length) return [0.5, 0.5, 1];
  if (t <= frames[0][0]) return frames[0].slice(1);
  const last = frames[frames.length - 1];
  if (t >= last[0]) return last.slice(1);
  let i = 0; while (i < frames.length - 1 && frames[i + 1][0] <= t) i++;
  const a = frames[i], b = frames[i + 1];
  const p = glide((t - a[0]) / Math.max(b[0] - a[0], 1e-6));
  // b[4]: how far the move pulls back half-way (log of zoom), so a long pan shows where it goes.
  const z = Math.exp(lerp(Math.log(a[3]), Math.log(b[3]), p) - (b[4] || 0) * Math.sin(Math.PI * p));
  return [lerp(a[1], b[1], p), lerp(a[2], b[2], p), z];
}

function cursorAt(samples, t) {
  if (!samples || !samples.length) return null;
  const at = (x) => {
    if (x <= samples[0][0]) return [samples[0][1], samples[0][2]];
    const last = samples[samples.length - 1];
    if (x >= last[0]) return [last[1], last[2]];
    let lo = 0, hi = samples.length - 1;
    while (hi - lo > 1) { const mid = (lo + hi) >> 1; if (samples[mid][0] <= x) lo = mid; else hi = mid; }
    const a = samples[lo], b = samples[hi], p = (x - a[0]) / Math.max(b[0] - a[0], 1e-6);
    return [lerp(a[1], b[1], p), lerp(a[2], b[2], p)];
  };
  // A light box filter: the recorded path at 60 Hz, smoothed like a hand.
  let x = 0, y = 0; const taps = [-0.05, -0.025, 0, 0.025, 0.05];
  for (const d of taps) { const p = at(t + d); x += p[0]; y += p[1]; }
  return [x / taps.length, y / taps.length];
}

function setImg(img, url) {
  if (!url || img.dataset.src === url) return;
  img.dataset.src = url;
  img.src = url;
  S.loads.push(img.decode().catch(() => {}));
}

// Where an asset (logo, mark, image) is read: the driver's origin, or the preview's media URL.
function assetUrl(key) {
  return S.media ? heldUrl(key) : `${ORIGIN}/${key}`;
}

// ── The preview's media ──────────────────────────────────────────
// The editor plans its preview again every few seconds while a shot is being
// made, and after each save: every plan is a new document, its URLs minted
// again. What the stage shows must come through that untouched, so in the
// preview (never in a render, which draws the frames the driver serves):
// - a key plays the URL first minted for its file while that URL answers,
//   for HOLD_MS at most (`heldUrl`): a newer token of the same file
//   (`preview_media_ids`) loads nothing again;
// - the videos outlive a document (`S.pool`, by key and file): the layer of
//   the next document takes the element as it is, loaded, at its time;
// - a new file under a key (a new take), a URL that stopped answering or one
//   held too long is swapped in behind what is on screen: the element that
//   last drew the key, else the shot's poster, stands in front until the new
//   one draws its frame (`cover`); a picture likewise (`setAsset`);
// - the old layers stay on screen until the new document's frame is drawn,
//   in the same task (`loadDoc`): the stage is never painted half built;
// - the scenes about to play are built ahead, their videos at their first
//   frame (`prepareAhead`);
// - a URL that stopped answering is reported (`onSourceLost`), so the editor
//   plans again and the next document brings a fresh one.
const HOLD_MS = 20 * 60 * 1000;
const AHEAD_S = 6;
const READY_EVENTS = ['loadeddata', 'seeked', 'canplay', 'playing'];

const mediaId = (key) => key + '\n' + (S.ids[key] || S.media[key]);

function heldUrl(key) {
  const url = S.media[key];
  if (!url) return null;
  const id = mediaId(key);
  const held = S.held.get(id);
  if (held && (held.url === url || (!held.lost && Date.now() - held.since < HOLD_MS))) return held.url;
  S.held.set(id, { url, since: Date.now(), lost: false });
  return url;
}

function lost(url) {
  let known = false;
  for (const held of S.held.values()) if (held.url === url && !held.lost) { held.lost = true; known = true; }
  if (known && S.onLost) { try { S.onLost(); } catch (e) { /* the host's own failure */ } }
}

// The new document's media: what it no longer plays leaves the pool, released once the swap is drawn.
function keepMedia(doc, media) {
  S.ids = doc.preview_media_ids || {};
  const wanted = new Set(Object.keys(media).map(key => key + '\n' + (S.ids[key] || media[key])));
  for (const [id, entry] of S.pool) if (!wanted.has(id)) { S.pool.delete(id); S.drop.push(entry); }
  for (const id of [...S.held.keys()]) if (!wanted.has(id)) S.held.delete(id);
  for (const key of [...S.drawnFor.keys()]) if (!(key in media)) S.drawnFor.delete(key);
  // The quality check plays no media: nothing to load ahead.
  S.ahead = Object.keys(media).length > 0;
}

function poolVideo(key) {
  const url = heldUrl(key);
  const id = mediaId(key);
  const known = S.pool.get(id);
  if (known && known.url === url) return known;
  const video = document.createElement('video');
  video.muted = true; video.playsInline = true; video.preload = 'auto';
  const entry = { id, url, video, drawn: false, standing: false };
  // What stands in for the key's next new element, whichever plan built this one.
  video.addEventListener('loadeddata', () => { entry.drawn = true; if (S.pool.get(id) === entry) S.drawnFor.set(key, entry); });
  video.addEventListener('error', () => lost(url));
  video.src = url;
  if (known) S.drop.push(known);
  S.pool.set(id, entry);
  return entry;
}

function release(entry) {
  const video = entry.video;
  video.pause(); video.removeAttribute('src'); video.load(); video.remove();
  entry.drawn = false;
}

// An asset into an <img>: the driver's frame in a render; in the preview, the
// URL its key holds - and while that one is not loaded yet, the picture the
// key showed before, if loaded (a fresh <img> draws nothing while it loads).
function setAsset(img, key) {
  const url = assetUrl(key);
  if (!url) return false;
  if (!S.media) { setImg(img, url); return true; }
  const was = S.shownImg[key];
  if (was && was !== url && S.loadedUrls.has(was) && !S.loadedUrls.has(url)) {
    setImg(img, was);
    const next = new Image();
    next.src = url;
    next.decode().then(() => {
      S.loadedUrls.add(url); S.shownImg[key] = url;
      if (img.dataset.src === was) setImg(img, url);
    }, () => lost(url));
    return true;
  }
  img.addEventListener('load', () => { S.loadedUrls.add(url); S.shownImg[key] = url; }, { once: true });
  img.addEventListener('error', () => lost(url), { once: true });
  setImg(img, url);
  return true;
}

// The local slope of a time map: how fast the source plays at output time x.
function mapRate(points, x) {
  if (!points || points.length < 2) return 1;
  let i = 0; while (i < points.length - 2 && points[i + 1][0] <= x) i++;
  const a = points[i], b = points[i + 1];
  return b[0] === a[0] ? 0 : (b[1] - a[1]) / (b[0] - a[0]);
}

// Footage shorter than its scene holds its last frame once the planner has slowed it as far as it may:
// from then on the picture drifts in until the scene ends, eased in from rest, so a held frame
// never reads as frozen. HELD_DRIFT a second of hold, HELD_DRIFT_MAX in all, spread over the whole hold:
// the pace of an image's slow push in (`motionTransform`), never a zoom that reframes the shot. Eased in
// over HELD_EASE, short enough that a soft picture moves within the render's frozen-picture check's half
// second (`frame_freeze.FROZEN_SECONDS`).
const HELD_DRIFT = 0.02, HELD_DRIFT_MAX = 0.08, HELD_EASE = 0.3;

// `[from, until]`: where a time map holds its last frame, or null. The last `still` seconds (the scene's
// own `hold_end`, a still beat its author asked for) do not drift: the picture keeps the scale it reached.
function heldSpan(points, still) {
  if (!points || points.length < 2) return null;
  const last = points[points.length - 1];
  let i = points.length - 1;
  while (i > 0 && points[i - 1][1] === last[1]) i--;
  const until = last[0] - (still || 0);
  return i < points.length - 1 && until > points[i][0] ? [points[i][0], until] : null;
}

// The scale of the held picture at time t: 1 until it holds, then growing to the end of the hold.
function heldDrift(span, t) {
  if (!span || !(t > span[0])) return 1;
  const hold = span[1] - span[0], d = Math.min(t, span[1]) - span[0];
  const ease = Math.min(HELD_EASE, hold / 2);
  const run = d < ease ? d * d / (2 * ease) : d - ease / 2;
  return 1 + Math.min(HELD_DRIFT_MAX, HELD_DRIFT * hold) * run / (hold - ease / 2);
}

function driftShot(node, span, t) {
  const s = heldDrift(span, t);
  node.style.transform = s === 1 ? '' : `scale(${s})`;
}

function seeked(video) {
  return new Promise(resolve => {
    const done = () => { video.removeEventListener('seeked', done); resolve(); };
    video.addEventListener('seeked', done);
    setTimeout(done, 700);
  });
}

// What a recording or a clip is drawn from: one frame image per output frame
// (a render), or a video element kept at the source time (the preview). Paused, the
// preview seeks exactly; playing, it lets the video run at the time map's
// rate and corrects only a drift the eye would see.
function placeholder(parent, text) {
  const label = el('div', 'ph', parent);
  label.textContent = text || '';
  return label;
}

// A gain envelope, `[[seconds, gain], ...]` linear between its points, at the video's time `t`.
function envelopeAt(points, t) {
  if (!points || !points.length) return 1;
  if (t <= points[0][0]) return points[0][1];
  for (let i = 1; i < points.length; i++) {
    const [t1, g1] = points[i];
    if (t <= t1) {
      const [t0, g0] = points[i - 1];
      return t1 > t0 ? g0 + (g1 - g0) * (t - t0) / (t1 - t0) : g1;
    }
  }
  return points[points.length - 1][1];
}

// Where a scene's footage holds its last frame and drifts in: its `extend_mode`
// (`timeline.extension`) is `zoom`, the default, which the planner leaves unmarked; a loop or a hold does not drift.
function driftSpan(sc) {
  return sc.extend ? null : heldSpan(sc.time_map, sc.hold_end);
}

// A looping end (`extend_mode: loop`): [the source time under, the source time over, the over layer's
// opacity] at t. It loops until `to`, the author's still beat (`hold_end`), then keeps that frame.
function loopTimes(ext, sc, t) {
  t = Math.min(t, ext.to ?? t);
  const step = Math.max(ext.period - ext.fade, 1e-3), b0 = ext.from - ext.fade;
  const k = Math.max(0, Math.floor((t - b0) / step));
  const local = t - (b0 + k * step);
  const over = ext.src_from + local * ext.speed;
  const under = k === 0 ? mapTime(sc.time_map, t) : ext.src_from + (local + step) * ext.speed;
  return [under, over, ext.fade > 0 ? clamp(local / ext.fade, 0, 1) : 1];
}

function frameSource(parent, sc) {
  // Still being recorded or generated, or failed: the preview says so where it will play.
  if (S.media && !S.media[sc.src]) {
    const label = placeholder(parent, sc.preview_label);
    if ((sc.preview_state || {}).state === 'failed') label.classList.add('failed');
    return { node: label, show() {}, prime() {} };
  }
  if (S.media) return previewSource(parent, sc);
  const img = el('img', '', parent);
  // Every output frame shows exactly one take frame, never two cross-dissolved: a blend of two painted
  // instants draws every line twice wherever the page moved between them, and a page swap as a ghost. A stretch the
  // recording painted under its rate, played slowed, shows the painted instant nearest the source time.
  const url = (index) => `${ORIGIN}/${sc.src}/${clamp(index, 0, Math.max(sc.src_frames - 1, 0))}.jpg`;
  return {
    node: img,
    show(t, rate) {
      const painted = sc.painted && rate > 0 && rate < 0.999 ? nearestPainted(sc.painted, t) : null;
      if (painted !== null) {
        setImg(img, url(Math.ceil(painted * sc.src_fps - 1e-6)));
        return;
      }
      setImg(img, url(Math.round(t * sc.src_fps)));
    },
    prime() {},
  };
}

// The scene time a recording's take frame is picked at for a frame drawn at scene time `t`: its output frame's centre
// (`S.frame`), so every sub-frame of a motion-blur shutter shows the same take frame; `t` itself at the centre.
function takeTime(sc, t) {
  return Math.max(0, S.frame / S.fps - sc.start);
}

// The painted instant nearest the source time `t` within a sparse stretch of a recording
// (`capture_rate.sparse_spans`; one picture, never a blend of two); null outside one.
function nearestPainted(spans, t) {
  for (const [a, b, times] of spans) {
    if (t < a || t >= b || !times.length) continue;
    let i = 0, j = times.length - 1;
    while (j - i > 1) { const m = (i + j) >> 1; if (times[m] <= t) i = m; else j = m; }
    return Math.abs(times[j] - t) < Math.abs(t - times[i]) ? times[j] : times[i];
  }
  return null;
}

// A video at the source time `t`: playing at the time map's rate, a drift the eye would see corrected;
// paused, seeked exactly (the frame drawn waits for the seek of the one it shows).
function drive(source, video, t, rate, audible, shown) {
  if (S.playing && rate > 0) {
    const r = clamp(rate, 0.0625, 16);
    if (Math.abs(video.playbackRate - r) > 0.01) video.playbackRate = r;
    video.muted = S.muted || !audible;
    video.volume = clamp(source.volume * envelopeAt(source.duck, S.T), 0, 1);
    if (Math.abs(video.currentTime - t) > 0.3) video.currentTime = t;
    // A browser that refuses sound without a gesture still plays it silent.
    if (video.paused) video.play().catch(() => { video.muted = true; video.play().catch(() => {}); });
  } else {
    if (!video.paused) video.pause();
    if (Math.abs(video.currentTime - t) > 0.02) { video.currentTime = t; if (shown) S.loads.push(seeked(video)); }
  }
}

const videosOf = (source) => [source.video, ...source.covers.filter(c => c.video).map(c => c.video)];

// A recording or a clip in the preview: its pooled video, and in front of it,
// until it has drawn a frame, the element that last drew this key (kept in
// step, silent) or the shot's poster.
function previewSource(parent, sc) {
  const key = sc.src;
  const entry = poolVideo(key);
  const video = entry.video;
  parent.appendChild(video);
  const heard = sc.preview_audio || {};
  const first = mapTime(sc.time_map, 0);
  const source = { video, audio: !!sc.preview_audio, volume: heard.volume ?? 1, duck: heard.duck || null, active: false, covers: [],
    // Built ahead: at its first frame, decoded before it comes on screen.
    prime() { if (!source.active && video.paused && Math.abs(video.currentTime - first) > 0.02) video.currentTime = first; } };
  const before = S.drawnFor.get(key);
  if (!entry.drawn) cover(source, parent, before && before !== entry && before.drawn ? before : null, key);
  S.sources.push(source);
  return {
    show(t, rate) {
      source.active = true;
      drive(source, video, t, rate, source.audio, !source.covers.length);
      for (const c of source.covers) if (c.video) drive(source, c.video, t, rate, false, true);
    },
    prime: source.prime,
  };
}

// The second layer of a looping end in the preview: its own silent element over the pooled one,
// which keeps playing the layer under it (a render draws two frames the driver serves instead).
function loopLayer(parent, sc) {
  const url = heldUrl(sc.src);
  if (!url) return { node: el('div', '', parent), show() {}, prime() {} };
  const video = el('video', '', parent);
  video.muted = true; video.playsInline = true; video.preload = 'auto'; video.src = url;
  const source = { video, volume: 0, duck: null };
  return { node: video, show(t, rate) { drive(source, video, t, rate, false, true); }, prime() {} };
}

function cover(source, parent, before, key) {
  const covers = source.covers;
  if (before) {
    before.standing = true;
    parent.appendChild(before.video);
    covers.push({ entry: before, video: before.video });
  } else {
    const img = el('img', 'poster', parent);
    if (setAsset(img, 'poster/' + key.slice(key.indexOf('/') + 1))) {
      img.style.objectFit = getComputedStyle(source.video).objectFit;
      covers.push({ img });
    } else img.remove();
  }
  if (!covers.length) return;
  const video = source.video;
  const done = () => {
    if (video.readyState < 2 || video.seeking) return;
    for (const c of covers) {
      if (c.img) { c.img.remove(); continue; }
      c.entry.standing = false;
      if (S.pool.get(c.entry.id) !== c.entry) release(c.entry);
      else c.video.remove();
    }
    covers.length = 0;
    for (const name of READY_EVENTS) video.removeEventListener(name, done);
  };
  for (const name of READY_EVENTS) video.addEventListener(name, done);
}

// Screen inserts: a picture or a video mapped onto a surface of the scene's footage, its
// quad's corners 0-1 across and down that footage. A render draws each frame the driver warped at
// the host's size in device pixels (screen_insert.py), placed on the quad's bounding box with the
// driver's own arithmetic (insertBox); the editor's preview plays the insert's build, drawn ahead by the
// same driver code (previewBuild), else maps its source with a CSS perspective (matrix3d), a video
// playing at the scene's time. A tracked insert names the footage frame shown
// (&f=): the driver draws it into that frame's quad, every frame on one box (ins.box). A matted one
// (ins.matte) names it too - its key or its occlusion reads that frame - and the output frame
// it is drawn for (&n=), which its audit is timed by. A pushed one (ins.zoom) names its own
// second (&o=, ms after it came in): the driver cuts its source to the window the push frames then. A
// keyed one is held on its surface over its scene (ins.held): drawn on every frame, how much of
// it shows named (&v=, permille), the driver painting the rest of the surface in the bezel's colour.
function insertBox(quad, w, h) {
  const xs = quad.map(p => p[0]), ys = quad.map(p => p[1]);
  const x0 = Math.max(0, Math.floor(Math.min(...xs) * w)), x1 = Math.min(w, Math.ceil(Math.max(...xs) * w));
  const y0 = Math.max(0, Math.floor(Math.min(...ys) * h)), y1 = Math.min(h, Math.ceil(Math.max(...ys) * h));
  return x1 > x0 && y1 > y0 ? [x0, y0, x1, y1] : null;
}

// The CSS matrix3d drawing a w x h box (transform-origin 0 0) onto the quad `p` (pixels).
function quadMatrix(p, w, h) {
  const [[x0, y0], [x1, y1], [x2, y2], [x3, y3]] = p;
  const dx1 = x1 - x2, dx2 = x3 - x2, dy1 = y1 - y2, dy2 = y3 - y2;
  const sx = x0 - x1 + x2 - x3, sy = y0 - y1 + y2 - y3;
  let g = 0, k = 0;
  if (Math.abs(sx) > 1e-9 || Math.abs(sy) > 1e-9) {
    const den = dx1 * dy2 - dx2 * dy1;
    g = (sx * dy2 - dx2 * sy) / den; k = (dx1 * sy - sx * dy1) / den;
  }
  const a = (x1 - x0 + g * x1) / w, b = (x3 - x0 + k * x3) / h, d = (y1 - y0 + g * y1) / w, e = (y3 - y0 + k * y3) / h;
  return `matrix3d(${a},${d},0,${g / w},${b},${e},0,${k / h},0,0,1,0,${x0},${y0},0,1)`;
}

// The rect a picture of srcW x srcH covers in a w x h box (object-fit: cover): where its footage really is.
function coverRect(w, h, srcW, srcH) {
  const s = Math.max(w / Math.max(srcW, 1), h / Math.max(srcH, 1));
  return [(w - srcW * s) / 2, (h - srcH * s) / 2, srcW * s, srcH * s];
}

function insertHost(parent, rect) {
  const host = el('div', 'inshost', parent);
  Object.assign(host.style, { left: rect[0] + 'px', top: rect[1] + 'px', width: rect[2] + 'px', height: rect[3] + 'px' });
  return host;
}

// The preview's insert drawn ahead by the render's own compositor (insert_builds): the whole
// footage frame, transparent but for the insert - keyed, tracked, its source moving - played over the
// footage in step with the scene. Until it is drawn, previewInsert maps the source onto the quad.
function previewBuild(host, ins) {
  const key = 'build/' + ins.src;
  if (!ins.build || !S.media[key]) return null;
  const box = el('div', 'insprev', host);
  Object.assign(box.style, { left: '0', top: '0', width: '100%', height: '100%', transform: 'none' });
  const video = poolVideo(key).video;
  box.appendChild(video);
  video.style.objectFit = 'fill';
  const source = { video, audio: false, volume: 0, duck: null, active: false, covers: [] };
  S.sources.push(source);
  const last = Math.max((ins.build.duration || 0) - 0.01, 0);
  return { ins, node: box, glow: null, show(t) { source.active = true; drive(source, video, clamp(t - ins.build.t0, 0, last), 1, false, true); } };
}

// A screen insert's camera push s seconds after it came in, as screen_insert.Zoom draws it: the scale,
// eased from 1, and the window of the source it frames - centred on its focus, kept inside the source.
function zoomTransform(z, s) {
  const span = z.end - z.start, x = span <= 0 ? 1 : clamp((s - z.start) / span, 0, 1);
  const k = 1 + (z.scale - 1) * x * x * (3 - 2 * x);
  if (k <= 1.001) return 'none';
  const shown = 1 / k, cx = clamp(z.focus[0], shown / 2, 1 - shown / 2), cy = clamp(z.focus[1], shown / 2, 1 - shown / 2);
  return `scale(${k}) translate(${-100 * (cx - shown / 2)}%, ${-100 * (cy - shown / 2)}%)`;
}

// The quad a tracked insert's stand-in is drawn on at footage second `at`: the one the last render followed on that
// footage frame (ins.preview_track, insert_builds.attach_stand_ins), clamped to the frames it followed; its planned
// quad without one, or where no footage second is known (a picture).
function previewQuad(ins, at) {
  const tr = ins.preview_track;
  if (!tr || at === undefined || at === null) return ins.quad;
  return tr.quads[clamp(Math.round(at * tr.fps) - tr.from, 0, tr.quads.length - 1)];
}

// screens.key_matte on an RGBA pixel buffer: the share of the key in each pixel (1 on the surface, 0 on what stands
// in front), its constants the render's (KEY_MATTE).
function keyMatte(data, key) {
  const K = KEY_MATTE, Y = K.to_ycc, pad = K.luma_pad;
  const ycc = (r, g, b) => Y.map(row => row[0] * r + row[1] * g + row[2] * b);
  const [ky, kcb, kcr] = ycc(...key.rgb);
  const ref = [kcb / (ky + pad), kcr / (ky + pad)], mag = Math.max(Math.hypot(ref[0], ref[1]), 1e-6);
  const dir = [ref[0] / mag, ref[1] / mag], scale = key.tolerance ? key.tolerance / K.key_in : 1;
  const out = new Float32Array(data.length / 4);
  for (let i = 0; i < out.length; i++) {
    const [y, cb, cr] = ycc(data[4 * i], data[4 * i + 1], data[4 * i + 2]);
    if (y < K.min_luma * 0.5) { out[i] = 0; continue; }
    const c0 = cb / (y + pad), c1 = cr / (y + pad), on = c0 * dir[0] + c1 * dir[1];
    const across = Math.hypot(c0 - on * dir[0], c1 - on * dir[1]) / mag;
    const share = clamp((on / mag - K.low) / (K.high - K.low), 0, 1);
    const pure = clamp((K.across_out * scale - across) / ((K.across_out - K.across_in) * scale), 0, 1);
    out[i] = share * pure;
  }
  return out;
}

// What stands in front of a keyed insert's surface, kept in front in the preview: over the insert, a canvas of the
// footage frame shown, opaque where the key's matte says it is not the surface (keyMatte), read over the quad's
// bounding box at most KEY_MASK_WIDTH pixels across. Footage the page may not read (another origin) draws none.
const KEY_MASK_WIDTH = 480;
function keyMask(host, ins, w, h) {
  const footage = host.closest('.shot, .still, .backdrop');
  const canvas = el('canvas', 'insmask', host);
  Object.assign(canvas.style, { position: 'absolute', pointerEvents: 'none' });
  const ctx = canvas.getContext('2d', { willReadFrequently: true });
  let broken = false;
  const draw = (quad) => {
    const src = footage && footage.querySelector('video, img');
    if (broken || !ctx || !src) return;
    const b = insertBox(quad, w, h);
    if (!b) return;
    const k = Math.min(1, KEY_MASK_WIDTH / Math.max(b[2] - b[0], 1));
    const cw = Math.max(1, Math.round((b[2] - b[0]) * k)), ch = Math.max(1, Math.round((b[3] - b[1]) * k));
    if (canvas.width !== cw || canvas.height !== ch) { canvas.width = cw; canvas.height = ch; }
    Object.assign(canvas.style, { left: b[0] + 'px', top: b[1] + 'px', width: (b[2] - b[0]) + 'px', height: (b[3] - b[1]) + 'px' });
    const sw = src.videoWidth || src.naturalWidth, sh = src.videoHeight || src.naturalHeight;
    if (!sw || !sh) return;
    try {
      ctx.clearRect(0, 0, cw, ch);
      ctx.drawImage(src, b[0] * sw / w, b[1] * sh / h, (b[2] - b[0]) * sw / w, (b[3] - b[1]) * sh / h, 0, 0, cw, ch);
      const pixels = ctx.getImageData(0, 0, cw, ch);
      const matte = keyMatte(pixels.data, ins.preview_key);
      for (let i = 0; i < matte.length; i++) pixels.data[4 * i + 3] = Math.round(255 * (1 - matte[i]));
      ctx.putImageData(pixels, 0, 0);
    } catch (e) { broken = true; ctx.clearRect(0, 0, cw, ch); }
  };
  draw.node = canvas;
  return draw;
}

function previewInsert(host, ins, w, h) {
  if (!S.media[ins.src]) return null;
  const p = ins.quad.map(q => [q[0] * w, q[1] * h]);
  const bw = (Math.hypot(p[1][0] - p[0][0], p[1][1] - p[0][1]) + Math.hypot(p[2][0] - p[3][0], p[2][1] - p[3][1])) / 2;
  const bh = (Math.hypot(p[3][0] - p[0][0], p[3][1] - p[0][1]) + Math.hypot(p[2][0] - p[1][0], p[2][1] - p[1][1])) / 2;
  const box = el('div', 'insprev', host);
  Object.assign(box.style, { width: bw + 'px', height: bh + 'px', transform: quadMatrix(p, bw, bh) });
  let media, show = null;
  if (ins.kind === 'video') {
    media = poolVideo(ins.src).video;
    box.appendChild(media);
    // Its source plays at the scene's time, as the render's does, while its build is drawn.
    const source = { video: media, audio: false, volume: 0, duck: null, active: false, covers: [] };
    S.sources.push(source);
    show = (t) => { source.active = true; drive(source, media, mapTime(ins.time_map, t), mapRate(ins.time_map, t), false, true); };
  } else {
    media = el('img', '', box);
    setAsset(media, ins.src);
  }
  media.style.objectFit = ins.fit === 'contain' ? 'contain' : 'cover';
  if (ins.zoom) {
    media.style.transformOrigin = '0 0';
    const play = show;
    show = (t, at) => { if (play) play(t, at); media.style.transform = zoomTransform(ins.zoom, t - ins.t0); };
  }
  // Tracked: the box drawn on the quad the render followed on the footage frame shown. Keyed: what stands in front
  // of the surface painted back over it.
  const mask = ins.preview_key ? keyMask(host, ins, w, h) : null;
  if (ins.preview_track || mask) {
    const play = show;
    show = (t, at) => {
      if (play) play(t, at);
      const quad = previewQuad(ins, at);
      if (ins.preview_track) box.style.transform = quadMatrix(quad.map(q => [q[0] * w, q[1] * h]), bw, bh);
      if (mask) mask(quad);
    };
  }
  return { ins, node: box, glow: null, mask: mask ? mask.node : null, show };
}

// How much of an insert shows t seconds into its scene (timeline.insert_visibility): coming in after a t0 past the
// scene's start, going before its t1 - never, for one that runs to its scene's end (to_end).
const insertShown = (ins, t) => t >= ins.t0 && (ins.to_end || t < ins.t1)
  ? Math.min(ins.t0 > 0 ? prog(t, ins.t0, INSERT_FADE) : 1, ins.to_end ? 1 : 1 - prog(t, ins.t1 - INSERT_FADE, INSERT_FADE)) : 0;

// The second past which no frame may paint a scene under an insert that runs to its end
// (timeline.painted_end): a frame painted there is kept for the driver (overruns), which fails the render.
const paintedEnd = (ins) => (ins.held ? ins.held[1] : ins.t1);
function noteOverrun(sc, ins, t, length) {
  const end = paintedEnd(ins);
  if (!ins.to_end || t < end + HELD_END_SLACK) return;
  (S.overruns = S.overruns || []).push({ src: ins.src, scene: sc.id, frame: S.frame, t: Math.round(t * 1e4) / 1e4,
    length: Math.round(length * 1e4) / 1e4, painted_end: end });
}

// A keyed insert held on its surface (timeline.insert_held): within its held span, the one held to the scene's
// end on its last frame too.
const heldAt = (ins, t, length) => !!ins.held && t >= ins.held[0]
  && (ins.held[1] >= length - HELD_END_SLACK || t < ins.held[1]);

function buildInserts(host, sc, w, h, zoom) {
  const items = [];
  for (const ins of sc.inserts || []) {
    if (S.media) {
      const item = previewBuild(host, ins) || previewInsert(host, ins, w, h);
      if (item) items.push(item);
      continue;
    }
    // Drawn at the size the host is seen at its closest (a recording's camera zooms in).
    const W = Math.max(1, Math.round(w * window.devicePixelRatio * zoom)), H = Math.max(1, Math.round(h * window.devicePixelRatio * zoom));
    const box = insertBox(ins.box || ins.quad, W, H);
    if (!box) continue;
    const glow = ins.glow > 0 ? el('img', 'insglow', host) : null;
    if (glow) Object.assign(glow.style, { left: '0', top: '0', width: '100%', height: '100%', objectFit: 'fill' });
    const node = el('img', 'ins', host);
    Object.assign(node.style, { objectFit: 'fill', left: (100 * box[0] / W) + '%', top: (100 * box[1] / H) + '%',
      width: (100 * (box[2] - box[0]) / W) + '%', height: (100 * (box[3] - box[1]) / H) + '%' });
    // Named, so the driver can read where the frame places it (insertRects); `data-src` is setImg's.
    node.dataset.insert = ins.src;
    items.push({ ins, node, glow, W, H });
  }
  // `at`: the second of the scene's footage shown at t, which a tracked insert's quad follows.
  const length = sc.end - sc.start;
  return (t, at) => {
    const held = S.media ? items.map(() => false) : items.map(it => heldAt(it.ins, t, length));
    items.forEach((it, k) => {
      const ins = it.ins;
      if (!S.media) noteOverrun(sc, ins, t, length);
      const a = insertShown(ins, t);
      // Held with nothing held beneath it on its surface (timeline.insert_layers): the layer drawn whole on the scene's
      // own footage, how much of the insert shows composed by the driver over the bezel's colour (&v=) - never the
      // insert faded on its own over the key, never left out. Over a layer held beneath it, it fades as any insert.
      const base = held[k] && !items.some((other, j) => j < k && held[j] && other.ins.surface === ins.surface);
      const opacity = base ? 1 : a;
      it.node.style.opacity = String(opacity);
      if (it.mask) it.mask.style.opacity = String(opacity);
      if (it.glow) it.glow.style.opacity = String(a * ins.glow);
      if (S.media && a && it.show) it.show(t, at);
      if (!opacity || S.media) return;
      // A video insert's frame is picked at its output frame's centre (`takeTime`), as a recording's take frame is:
      // every motion-blur sub-frame shows the same insert frame, never two averaged into one double image where the
      // insert's video changes frame inside the shutter. Its fade and push stay at the sub-frame's own time.
      const index = ins.kind === 'video'
        ? clamp(Math.round(mapTime(ins.time_map, takeTime(sc, t)) * ins.src_fps), 0, Math.max(ins.src_frames - 1, 0)) : 0;
      const tr = ins.track, mt = ins.matte;
      const f = tr ? clamp(Math.round((at || 0) * tr.fps), tr.from, tr.to)
        : mt && mt.fps ? clamp(Math.round((at || 0) * mt.fps), 0, Math.max((mt.frames || 1) - 1, 0)) : null;
      // A pushed insert names its own second, held at the push's ends: a still before and after it is asked once.
      const z = ins.zoom ? Math.round(1000 * clamp(t - ins.t0, ins.zoom.start, ins.zoom.end)) : null;
      const size = `w=${it.W}&h=${it.H}` + (f !== null ? `&f=${f}` : '') + (mt ? `&n=${S.frame}` : '') + (z !== null ? `&o=${z}` : '')
        + (base ? `&v=${Math.round(a * 1000)}` : '');
      setImg(it.node, `${ORIGIN}/${ins.src}/${index}.png?${size}`);
      if (it.glow) setImg(it.glow, `${ORIGIN}/${ins.src}/${index}.glow.png?${size}`);
    });
  };
}

// The words of a text as spans to animate. `*words*` are drawn in the accent
// (`mark`): an asterisk opens an accent before a non-space and closes one
// after a non-space, so `5* rated, 4* loved` stays text. A line break the
// author typed breaks the line. Without an accent, a title's last word takes
// the gradient (`hl`).
const ACCENT = /__ACCENT_PATTERN__/;
const ACCENT_SPLIT = new RegExp('(' + ACCENT.source + ')');
const ACCENT_WHOLE = new RegExp('^' + ACCENT.source + '$');

function textWords(container, text, highlightLast, mark) {
  container.textContent = '';
  const source = String(text || '');
  const marked = ACCENT.test(source);
  const segments = marked
    ? source.split(ACCENT_SPLIT).filter(s => s.length).map(s => (ACCENT_WHOLE.test(s) ? [s.slice(1, -1), true] : [s, false]))
    : [[source, false]];
  const real = source.split(/\s+/).filter(w => w.trim()).length;
  const nodes = [];
  let n = 0;
  for (const [segment, accent] of segments) {
    for (const w of segment.split(/(\s+)/).filter(x => x.length)) {
      const breaks = w.trim() ? 0 : (w.match(/\n/g) || []).length;
      if (breaks) {
        for (let i = 0; i < breaks; i++) el('br', '', container);
        continue;
      }
      const span = el('span', 'w', container);
      span.textContent = w;
      if (!w.trim()) continue;
      n++;
      if (marked ? accent : highlightLast && n === real && real > 1) span.classList.add(marked ? (mark || 'hl') : 'hl');
      nodes.push(span);
    }
  }
  return nodes;
}

// The text of *node* drawn in the brand gradient, by an inline span (`.grad`); returns the span.
function gradientText(node, text) {
  node.textContent = '';
  const span = el('span', 'grad', node);
  span.textContent = text;
  return span;
}

// A statement's figure (`6,000+`): counted up from zero when it holds a whole number.
function parseFigure(text) {
  const m = /^(\D*?)(\d[\d,]*)(\D*)$/.exec(String(text || '').trim());
  if (!m) return null;
  const target = parseInt(m[2].replace(/,/g, ''), 10);
  return Number.isFinite(target) ? { prefix: m[1], target, grouped: m[2].includes(','), suffix: m[3] } : null;
}

function formatFigure(f, value) {
  const n = Math.round(value);
  return f.prefix + (f.grouped ? n.toLocaleString('en-US') : String(n)) + f.suffix;
}

function fitText(node, maxWidth, maxHeight, start, min) {
  let size = start;
  node.style.fontSize = size + 'px';
  while (size > min && (node.scrollWidth > maxWidth + 1 || node.scrollHeight > maxHeight + 1)) {
    size -= 4; node.style.fontSize = size + 'px';
  }
  return size;
}

// ── Layout: nothing a frame shows covers anything else ───────────────────
// The subtitles keep a band at their edge that a card's text, a lower third,
// a recording's window and a picture stay out of; a card's WHOLE text (logo,
// kicker, title, figure, lines, chips, call to action) is scaled to fit the
// room left; a badge or a callout takes the spot asked when it is free, else
// the first free one. Measured, so it holds whatever the agent wrote.

const rectOf = (node) => { const r = node.getBoundingClientRect(); return { x: r.left, y: r.top, w: r.width, h: r.height }; };
const meets = (a, b, gap) => a.x < b.x + b.w + gap && b.x < a.x + a.w + gap && a.y < b.y + b.h + gap && b.y < a.y + a.h + gap;
const together = (a, b) => a.t0 < b.t1 && b.t0 < a.t1;

// What a scene keeps clear at the subtitles' edge: their tallest cue shown
// during it, plus a gap (px from that edge; 0 when nothing is said). Burned
// in or not: a player draws the WebVTT track in that same band.
function reserveOf(sc) {
  const room = { top: 0, bottom: 0 };
  const c = S.doc.captions;
  if (!c || !(c.cues || []).length) return room;
  let h = 0;
  c.cues.forEach((q, i) => { if (q[1] > sc.start && q[0] < sc.end) h = Math.max(h, (S.capInfo[i] || {}).h || 0); });
  if (h > 0) room[S.capEdge] = S.capOffset + h + S.H * 0.025;
  return room;
}

function captionBand(room) {
  const h = room[S.capEdge];
  if (!h) return null;
  return { x: 0, y: S.capEdge === 'top' ? 0 : S.H - h, w: S.W, h, t0: -Infinity, t1: Infinity };
}

// The glyphs of a block, line by line - its text nodes only: a range over the
// block itself would also return its child blocks' full-width boxes.
function glyphRects(node) {
  const out = [];
  const walk = document.createTreeWalker(node, NodeFilter.SHOW_TEXT);
  const range = document.createRange();
  for (let text = walk.nextNode(); text; text = walk.nextNode()) {
    if (!text.textContent.trim()) continue;
    range.selectNodeContents(text);
    for (const r of range.getClientRects()) if (r.width > 1 && r.height > 1) out.push(r);
  }
  return out;
}

// What a card's blocks draw: their glyphs, or their box for a picture, a pill
// or a bar. One rectangle per line, so the room beside a short line is free.
function inkRects(card) {
  const out = [];
  const add = (r) => { if (r.width >= 1 && r.height >= 1) out.push({ x: r.left, y: r.top, w: r.width, h: r.height }); };
  for (const child of card.children) {
    if (child.classList.contains('chips')) { for (const chip of child.children) add(chip.getBoundingClientRect()); continue; }
    if (child.tagName === 'IMG' || !child.textContent.trim() || child.classList.contains('cta')) { add(child.getBoundingClientRect()); continue; }
    glyphRects(child).forEach(add);
  }
  return out;
}

const overlapArea = (a, b) => Math.max(0, Math.min(a.x + a.w, b.x + b.w) - Math.max(a.x, b.x)) * Math.max(0, Math.min(a.y + a.h, b.y + b.h) - Math.max(a.y, b.y));

// Lower thirds and big captions stand above the subtitles; two shown at once
// stand one above the other. Returns where they went.
function placeBands(items, room) {
  const placed = [];
  for (const it of items) {
    const o = it.o;
    if (o.type !== 'lower_third' && o.type !== 'caption') continue;
    const w = it.node.offsetWidth, h = it.node.offsetHeight;
    const floor = S.H - Math.max(o.type === 'lower_third' ? S.H * 0.11 : S.H * 0.08, room.bottom);
    const r = { x: o.type === 'lower_third' ? S.W * 0.05 : S.W * 0.08, y: o.type === 'lower_third' ? floor - h : Math.min(S.H * 0.64, floor - h), w, h, t0: o.t0, t1: o.t1 };
    for (let moved = true; moved;) {
      moved = false;
      for (const p of placed) if (together(p, r) && meets(p, r, 16)) { r.y = p.y - 16 - h; moved = true; }
    }
    it.node.style.bottom = 'auto'; it.node.style.top = r.y + 'px';
    it.placed = true;
    placed.push(r);
  }
  return placed;
}

const SPOTS = ['top_right', 'top_left', 'bottom_right', 'bottom_left', 'top', 'bottom', 'right', 'left'];

function spotRect(pos, w, h, room) {
  const W = S.W, H = S.H, m = 0.06;
  const top = Math.max(H * m, room.top), bottom = Math.max(H * 0.14, room.bottom);
  let x = (W - w) / 2, y = (top + H - bottom - h) / 2;
  if (pos.includes('left')) x = W * m;
  if (pos.includes('right')) x = W - w - W * m;
  if (pos.startsWith('top')) y = top;
  if (pos.startsWith('bottom')) y = H - h - bottom;
  return { x, y, w, h };
}

// Badges, pictures and unanchored callouts: the spot asked, or the first one
// free of what the scene shows (*taken*) and of the overlays shown with them -
// or, when none is, the one that covers least.
function placeSpots(items, room, taken, placed) {
  for (const it of items) {
    const o = it.o;
    if (!(o.type === 'badge' || o.type === 'image' || (o.type === 'callout' && !o.box))) continue;
    const w = it.node.offsetWidth, h = it.node.offsetHeight;
    const asked = !o.position || o.position === 'auto' ? 'top_right' : o.position;
    const busy = [...taken, ...placed.filter(p => together(p, o))];
    const free = (r) => !busy.some(q => meets(r, q, 16));
    const covered = (r) => busy.reduce((sum, q) => sum + overlapArea(r, q), 0);
    let r = spotRect(asked, w, h, room);
    if (!free(r)) {
      const spots = [asked, ...SPOTS.filter(p => p !== asked)].map(p => spotRect(p, w, h, room));
      r = spots.find(free) || spots.reduce((best, s) => (covered(s) < covered(best) ? s : best));
    }
    it.node.style.left = r.x + 'px'; it.node.style.top = r.y + 'px';
    it.placed = true;
    placed.push({ ...r, t0: o.t0, t1: o.t1 });
  }
}

// A card's box (its base margin, or more where the subtitles or its lower
// thirds take the frame's edge) and the scale that fits its whole text in it.
function frameText(card, sc, overlays, base, center) {
  const room = reserveOf(sc);
  overlays.room = room;
  const bands = placeBands(overlays, room);
  const top = Math.max(base, room.top);
  let bottom = Math.max(base, room.bottom);
  for (const r of bands) bottom = Math.max(bottom, S.H - r.y + S.H * 0.025);
  card.style.top = top + 'px'; card.style.bottom = bottom + 'px';
  let need = 0, wide = 0;
  for (const child of card.children) {
    const cs = getComputedStyle(child);
    need += child.offsetHeight + parseFloat(cs.marginTop) + parseFloat(cs.marginBottom);
    wide = Math.max(wide, child.scrollWidth);
  }
  // Too tall for the room, or a word too long for the width (a URL, a product code): scaled to fit both.
  const fit = Math.min(1, (S.H - top - bottom) / Math.max(need, 1), card.clientWidth / Math.max(wide, 1));
  card.style.transformOrigin = center ? '50% 50%' : '0% 50%';
  card.style.transform = `scale(${fit})`;
  placeSpots(overlays, room, [...inkRects(card), captionBand(room), S.wm].filter(Boolean), bands);
  card.style.transform = '';
  return fit;
}

// Overlays over a recording or a picture: above the subtitles, clear of the watermark.
function placeOver(overlays, sc) {
  const room = reserveOf(sc);
  overlays.room = room;
  placeSpots(overlays, room, [captionBand(room), S.wm].filter(Boolean), placeBands(overlays, room));
  return room;
}

// mulberry32, seeded per scene: the same tiles on every render of a video.
function seeded(seed) {
  let s = (Math.floor(seed * 1000) ^ 0x9e3779b9) >>> 0;
  return () => {
    s = (s + 0x6d2b79f5) | 0;
    let t = s;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

// Square tiles in the template's series colours, grown from the corners the
// content leaves free (the deck's mosaic covers, in motion): `start` keeps the
// left column of a left-aligned card, `center` the middle of a centred one,
// `frame` lets the tiles run behind a recording's window.
function buildMosaic(bg, seed, compose) {
  const series = (S.doc.theme.series || []).length ? S.doc.theme.series : [S.doc.theme.accent, S.doc.theme.accent_2];
  const tile = Math.round(Math.min(S.W, S.H) / 18);
  const cols = Math.ceil(S.W / tile), rows = Math.ceil(S.H / tile);
  const next = seeded(seed + 7.31);
  const diamond = (x, y, rx, ry, peak) => (c, r) => {
    const d = Math.abs(c + 0.5 - x) / rx + Math.abs(r + 0.5 - y) / ry;
    return d >= 1 ? 0 : peak * Math.pow(1 - d, 0.55);
  };
  const shapes = compose === 'center'
    ? [diamond(cols, 0, cols * 0.3, rows * 0.36, 0.92), diamond(0, rows, cols * 0.3, rows * 0.36, 0.9),
       diamond(0, 0, cols * 0.16, rows * 0.22, 0.7), diamond(cols, rows, cols * 0.16, rows * 0.22, 0.7)]
    : compose === 'start'
      ? [diamond(cols, 0, cols * 0.46, rows * 0.62, 0.94), diamond(cols, rows, cols * 0.2, rows * 0.3, 0.78)]
      : [diamond(cols, 0, cols * 0.34, rows * 0.5, 0.92), diamond(0, rows, cols * 0.3, rows * 0.42, 0.88)];
  const keep = (c, r) => {
    if (compose === 'start') return c < cols * 0.6 && r > rows * 0.14 && r < rows * 0.88;
    if (compose === 'center') return c > cols * 0.2 && c < cols * 0.8 && r > rows * 0.16 && r < rows * 0.84;
    return false;
  };
  const slots = new Int16Array(cols * rows).fill(-1);
  const free = (c, r, w, h) => {
    if (c + w > cols || r + h > rows) return false;
    for (let y = r; y < r + h; y++) for (let x = c; x < c + w; x++) if (slots[y * cols + x] >= 0 || keep(x, y)) return false;
    return true;
  };
  const tiles = [];
  for (let r = 0; r < rows; r++) {
    for (let c = 0; c < cols; c++) {
      if (slots[r * cols + c] >= 0 || keep(c, r)) continue;
      let p = Math.max(...shapes.map(f => f(c, r)));
      if (r === 0) p = Math.max(p, 0.45 * Math.max(0, 1 - (cols - c - 0.5) / (cols * 0.5)));
      if (p <= 0 || next() >= Math.min(p, 0.95)) continue;
      let w = 1, h = 1;
      const roll = next();
      if (p > 0.7 && roll < 0.06 && free(c, r, 3, 3)) { w = 3; h = 3; }
      else if (p > 0.45 && roll < 0.22 && free(c, r, 2, 2)) { w = 2; h = 2; }
      else if (roll > 0.92 && free(c, r, 2, 1)) { w = 2; }
      const left = c > 0 ? slots[r * cols + c - 1] : -1, above = r > 0 ? slots[(r - 1) * cols + c] : -1;
      let slot = Math.floor(next() * series.length);
      for (let k = 0; k < 4 && (slot === left || slot === above); k++) slot = Math.floor(next() * series.length);
      for (let y = r; y < r + h; y++) for (let x = c; x < c + w; x++) slots[y * cols + x] = slot;
      const node = el('div', 'tile', bg);
      Object.assign(node.style, { left: c * tile + 'px', top: r * tile + 'px', width: w * tile + 'px', height: h * tile + 'px',
        backgroundColor: series[slot] });
      const reach = Math.hypot((cols - c) / cols, r / rows);
      tiles.push({ node, delay: 0.05 + 0.55 * Math.min(reach, 1.3) + 0.12 * next(), phase: next() * 6.283 });
    }
  }
  return (t) => {
    for (const q of tiles) {
      const e = out3(prog(t, q.delay, 0.5));
      q.node.style.opacity = String(e * (0.9 + 0.1 * Math.sin(t * 0.9 + q.phase)));
      q.node.style.transform = `scale(${0.6 + 0.4 * e})`;
    }
  };
}

function buildBackground(parent, style, seed, compose) {
  const bg = el('div', 'bg', parent);
  if (style === 'mosaic') {
    // The tiles are laid on the canvas itself, not on the drifting oversize backdrop.
    bg.style.inset = '0';
    return buildMosaic(bg, seed, compose || 'frame');
  }
  const blobs = [];
  if (style !== 'solid') {
    const colors = ['var(--accent)', 'var(--accent2)', 'var(--accent)'];
    const sizes = [0.95, 0.8, 0.6];
    for (let i = 0; i < (style === 'gradient' ? 2 : 3); i++) {
      const b = el('div', 'blob', bg);
      const size = sizes[i] * Math.max(S.W, S.H);
      b.style.width = size + 'px'; b.style.height = size + 'px';
      b.style.background = `radial-gradient(circle at 50% 50%, color-mix(in srgb, ${colors[i]} ${style === 'mesh' ? 40 : 30}%, transparent) 0%, transparent 62%)`;
      blobs.push({ el: b, size, phase: seed * 1.7 + i * 2.1, base: [[0.12, 0.05], [0.55, 0.45], [0.2, 0.6]][i] });
    }
  }
  if (style === 'grid' || style === 'aurora') el('div', 'grid', bg);
  return (t) => {
    for (const b of blobs) {
      const x = (b.base[0] + 0.06 * Math.sin(t * 0.21 + b.phase)) * S.W - b.size / 2 + S.W * 0.1;
      const y = (b.base[1] + 0.05 * Math.cos(t * 0.17 + b.phase)) * S.H - b.size / 2 + S.H * 0.1;
      b.el.style.transform = `translate(${x}px, ${y}px)`;
    }
  };
}

const CURSOR_SVG = '<svg width="30" height="34" viewBox="0 0 30 34" xmlns="http://www.w3.org/2000/svg"><path d="M3 2 L3 27 L9.5 21 L14 31.5 L18.5 29.5 L14.2 19.5 L23 19.5 Z" fill="#ffffff" stroke="#0b0f19" stroke-width="2.2" stroke-linejoin="round"/></svg>';
const DOT_SVG = '<svg width="30" height="30" viewBox="0 0 30 30" xmlns="http://www.w3.org/2000/svg"><circle cx="15" cy="15" r="9" fill="#ffffff" stroke="#0b0f19" stroke-width="2.4"/></svg>';

// How dark a recording's highlight makes the rest of its picture, at most: light enough that the frame's mean
// brightness never dips past the brightness-dip gate (``brightness_dip.DIP_RATIO``), even on a full-bleed recording.
const HIGHLIGHT_DIM = 0.12;

function anchorBox(layout, cam, box) {
  const [x, y, w, h] = box;
  const p = layout.toCanvas(x, y, cam), q = layout.toCanvas(x + w, y + h, cam);
  return [p[0], p[1], q[0] - p[0], q[1] - p[1]];
}

function buildOverlays(parent, overlays) {
  const items = [];
  for (const o of overlays || []) {
    let node;
    if (o.type === 'lower_third') {
      node = el('div', 'lower', parent);
      el('div', 'bar2', node);
      const text = el('div', '', node);
      el('h3', '', text).textContent = o.text;
      if (o.subtext) el('p', '', text).textContent = o.subtext;
    } else if (o.type === 'badge') {
      node = el('div', 'badge', parent); el('i', '', node);
      const s = el('span', '', node); s.textContent = o.text;
    } else if (o.type === 'caption') {
      node = el('div', 'bigcap', parent); node.textContent = o.text;
    } else if (o.type === 'image') {
      // A picture, sized by the planner (design px) so it is placed before it has loaded.
      node = el('img', 'ovimg', parent);
      node.alt = o.text || '';
      node.dataset.opacity = String(o.opacity ?? 1);
      if (o.brand) node.dataset.brand = 'overlay:' + o.brand;
      node.style.width = o.w + 'px'; node.style.height = o.h + 'px';
      // No URL only in the quality check, which loads no media: the box still stands, to be measured.
      setAsset(node, o.src);
    } else if (o.type === 'spotlight') {
      node = el('div', 'spot', parent);
    } else {
      node = el('div', 'callout ' + (o.style === 'glass' ? '' : o.style), parent);
      const t = el('div', '', node); t.textContent = o.text;
      if (o.subtext) { const sm = el('small', '', node); sm.textContent = o.subtext; }
      if (o.box) { node.tip = el('div', 'tip', node); }
    }
    node.style.opacity = '0';
    items.push({ o, node, placed: false });
  }
  return items;
}

function updateOverlays(items, t, layout, cam) {
  const room = items.room || { top: 0, bottom: 0 };
  const E = easeOf('enter'), P = easeOf('emphasis'), inSeconds = MO().overlay.duration;
  const drawn = [];
  for (const it of items) {
    const o = it.o;
    const shown = t >= o.t0 && t < o.t1;
    if (!shown) { it.node.style.opacity = '0'; continue; }
    const pin = prog(t, o.t0, inSeconds), pout = 1 - prog(t, o.t1 - 0.28, 0.28);
    const a = Math.min(E(pin), pout);
    it.node.style.opacity = String(o.type === 'image' ? a * (o.opacity ?? 1) : a);
    if (o.type === 'image') { it.node.style.transform = `scale(${0.96 + 0.04 * E(pin)})`; continue; }
    if (o.type === 'spotlight' && o.box && layout) {
      const [x, y, w, h] = anchorBox(layout, cam, o.box);
      const pad = 12;
      it.node.style.left = (x - pad) + 'px'; it.node.style.top = (y - pad) + 'px';
      it.node.style.width = (w + 2 * pad) + 'px'; it.node.style.height = (h + 2 * pad) + 'px';
      it.node.style.boxShadow = `0 0 0 6000px rgba(2,5,12,${0.6 * a})`;
      continue;
    }
    if (o.type === 'callout' && o.box && layout) {
      const [x, y, w, h] = anchorBox(layout, cam, o.box);
      const r = it.node.getBoundingClientRect();
      const gap = 22;
      // Above its element, below it when there is no room; never into the subtitles.
      const ceil = Math.max(S.H * 0.04, room.top), floor = S.H - Math.max(S.H * 0.03, room.bottom);
      const above = y - r.height - gap, under = Math.min(y + h + gap, floor - r.height);
      let top = above, below = false;
      if (top < ceil) { top = Math.max(ceil, under); below = true; }
      let left = clamp(x + w / 2 - r.width / 2, S.W * 0.03, S.W * 0.97 - r.width);
      // Another callout shown now already there: the element's other side, else clear of it.
      const at = (y2) => ({ x: left, y: y2, w: r.width, h: r.height });
      let beside = true;
      if (drawn.some(d => meets(at(top), d, 12))) {
        const other = below ? above : under;
        const clear = (y2) => y2 >= ceil && y2 + r.height <= floor && !meets(at(y2), { x, y, w, h }, 4) && !drawn.some(d => meets(at(y2), d, 12));
        if (clear(other)) { top = other; below = !below; }
        else {
          const highest = Math.min(...drawn.map(d => d.y)), lowest = Math.max(...drawn.map(d => d.y + d.h));
          top = highest - 12 - r.height >= ceil ? highest - 12 - r.height : Math.min(lowest + 12, floor - r.height);
          beside = false;
        }
      }
      drawn.push(at(top));
      const lift = (1 - P(pin)) * 14 * (below ? -1 : 1);
      it.node.style.left = left + 'px'; it.node.style.top = (top + lift) + 'px';
      it.node.style.transform = `scale(${0.94 + 0.06 * E(pin)})`;
      if (it.node.tip) it.node.tip.style.display = beside ? 'block' : 'none';
      if (it.node.tip && beside) {
        const tipX = clamp(x + w / 2 - left - 9, 18, r.width - 36);
        it.node.tip.style.left = tipX + 'px';
        it.node.tip.style.top = below ? '-9px' : (r.height - 9) + 'px';
        it.node.tip.style.borderTop = below ? 'inherit' : 'none';
      }
      continue;
    }
    if (o.type === 'lower_third') it.node.style.transform = `translateX(${(1 - E(pin)) * -60}px)`;
    else if (o.type === 'caption') it.node.style.transform = `translateY(${(1 - E(pin)) * 30}px) scale(${0.97 + 0.03 * E(pin)})`;
    else it.node.style.transform = `translateY(${(1 - P(pin)) * 12}px)`;
  }
}

// Where a framed recording's window stands: as large as the margin leaves it, centred. The subtitles may run over
// at most WINDOW_UNDER_SUBTITLES of the page at the window's edge (the pill's own gap dropped), never more; a
// watermark across the window's columns is kept off it. Returns [x, y, w, h, page w, page h].
const WINDOW_UNDER_SUBTITLES = 0.1;
function windowBox(sc, frame, room, barH, ar) {
  const W = S.W, H = S.H, k = WINDOW_UNDER_SUBTITLES, gap = H * 0.025;
  const pad = frame.padding * W;
  // The band's inner edge: the pill's top (subtitles at the bottom) or its bottom (at the top).
  const pillTop = room.bottom ? H - room.bottom + gap : Infinity;
  const pillBottom = room.top ? room.top - gap : -Infinity;
  const fit = (top, bottom) => {
    let vh = Math.min(bottom - top - barH, (W - 2 * pad) / ar);
    if (pillTop < Infinity) vh = Math.min(vh, (pillTop - top - barH) / (1 - k));
    if (pillBottom > -Infinity) vh = Math.min(vh, (bottom - pillBottom) / (1 - k));
    vh = Math.max(vh, 1);
    const vw = vh * ar, h = vh + barH, x = (W - vw) / 2;
    let y = (top + bottom - h) / 2;
    if (pillTop < Infinity) y = Math.min(y, pillTop - barH - (1 - k) * vh);
    if (pillBottom > -Infinity) y = Math.max(y, pillBottom - barH - k * vh);
    y = Math.min(Math.max(y, top), bottom - h);
    return [x, y, vw, h, vw, vh];
  };
  let box = fit(pad, H - pad);
  const wm = S.wm && !noWatermark(sc) ? S.wm : null;
  if (wm && wm.x < box[0] + box[2] && box[0] < wm.x + wm.w) {
    const clear = H * 0.01;
    box = wm.y < H / 2 ? fit(Math.max(pad, wm.y + wm.h + clear), H - pad) : fit(pad, Math.min(H - pad, wm.y - clear));
  }
  return box;
}

function buildRecording(layer, sc) {
  const update = buildBackground(layer, S.doc.theme.background_style, sc.start);
  const frame = sc.frame || { style: 'window', url_bar: true, padding: 0.02, radius: 18, shadow: true };
  const W = S.W, H = S.H;
  const ar = sc.css_w / sc.css_h;
  let winX, winY, winW, winH, barH, vpW, vpH;
  const room = reserveOf(sc);
  const cam = el('div', 'cam', layer);
  const win = el('div', 'win' + (frame.shadow && frame.style === 'window' ? ' shadow' : ''), cam);
  if (frame.fill) {
    // Edge to edge: the page covers the whole canvas on every frame, the captions drawn over it. Never fitted
    // inside the captions' band: that left the film's backdrop under the page (fill_uncovered).
    barH = 0;
    const scale = Math.max(W / sc.css_w, H / sc.css_h);
    vpW = sc.css_w * scale; vpH = sc.css_h * scale; winW = vpW; winH = vpH; winX = (W - vpW) / 2; winY = (H - vpH) / 2;
  } else if (frame.style === 'window') {
    barH = frame.url_bar ? 52 : 0;
    [winX, winY, winW, winH, vpW, vpH] = windowBox(sc, frame, room, barH, ar);
    win.style.borderRadius = frame.radius + 'px';
  } else {
    barH = 0;
    const scale = Math.max(W / sc.css_w, H / sc.css_h);
    vpW = sc.css_w * scale; vpH = sc.css_h * scale; winW = vpW; winH = vpH; winX = (W - vpW) / 2; winY = (H - vpH) / 2;
  }
  Object.assign(win.style, { left: winX + 'px', top: winY + 'px', width: winW + 'px', height: winH + 'px' });
  let urlText = null;
  if (barH) {
    const bar = el('div', 'bar', win); bar.style.height = barH + 'px';
    const dots = el('div', 'dots', bar);
    for (const c of ['#ff5f57', '#febc2e', '#28c840']) { const d = el('i', '', dots); d.style.background = c; }
    const url = el('div', 'url', bar);
    url.insertAdjacentHTML('afterbegin', '<svg width="13" height="15" viewBox="0 0 13 15"><path d="M3 6V4.5a3.5 3.5 0 0 1 7 0V6" fill="none" stroke="currentColor" stroke-width="1.6"/><rect x="1" y="6" width="11" height="8" rx="2" fill="currentColor"/></svg>');
    urlText = el('span', '', url);
    el('div', '', bar).style.width = '52px';
  }
  const vp = el('div', 'vp', win);
  Object.assign(vp.style, { top: barH + 'px', width: vpW + 'px', height: vpH + 'px' });
  const frames = frameSource(vp, sc);
  const closest = Math.max(1, ...(sc.camera || []).map(k => k[3] || 1));
  const inserts = buildInserts(vp, sc, vpW, vpH, closest);
  el('div', 'border', win);
  const fx = el('div', 'fx', layer);
  const highlightNodes = [];
  const ripples = [0, 1, 2].map(() => { const r = el('div', 'ripple', fx); r.style.opacity = '0'; return r; });
  const cursorNode = el('div', 'cursor', fx);
  cursorNode.innerHTML = sc.cursor.style === 'arrow' ? CURSOR_SVG : DOT_SVG;
  const overlays = buildOverlays(fx, sc.overlays);
  placeOver(overlays, sc);
  const layout = {
    toCanvas(u, v, c) {
      const X = winX + u * vpW, Y = winY + barH + v * vpH;
      return [c.tx + c.z * X, c.ty + c.z * Y];
    },
  };
  let lastUrl = null;
  const held = driftSpan(sc);
  return (t) => {
    update(t);
    // A render's motion-blur sub-frame shows the take frame of its output frame's centre: the shutter averages what
    // moves over the take (the camera, the cursor), never two take frames into one double image where the take
    // changes frame inside the shutter (a stepping capture), which a preview, drawn without blur, never shows.
    const ft = S.media ? t : takeTime(sc, t);
    frames.show(mapTime(sc.time_map, ft), mapRate(sc.time_map, ft));
    // A tracked or matted insert follows the take frame shown (`ft`), never a footage second between two of them.
    inserts(t, mapTime(sc.time_map, ft));
    // The camera zooms the whole shot toward its focus point, and in a little further while the last frame holds.
    const [cx, cy, zoom] = camera(sc.camera, t);
    const z = zoom * heldDrift(held, t);
    const fxp = winX + cx * vpW, fyp = winY + barH + cy * vpH;
    const k = clamp((z - 1) / 0.5, 0, 1);
    let tx = lerp(fxp, W / 2, k) - z * fxp, ty = lerp(fyp, H / 2, k) - z * fyp;
    if (z * winW >= W) tx = clamp(tx, W - z * (winX + winW), -z * winX);
    if (z * winH >= H) ty = clamp(ty, H - z * (winY + winH), -z * winY);
    cam.style.transform = `translate(${tx}px, ${ty}px) scale(${z})`;
    const c = { tx, ty, z };
    if (urlText) {
      let current = '';
      for (const u of sc.urls || []) if (u[0] <= t) current = u[1];
      if (current !== lastUrl) {
        lastUrl = current;
        urlText.textContent = '';
        try {
          const parsed = new URL(current);
          const host = el('b', '', urlText); host.textContent = parsed.host;
          const rest = parsed.pathname + parsed.search;
          if (rest && rest !== '/') urlText.appendChild(document.createTextNode(rest));
        } catch (e) { urlText.textContent = current; }
      }
    }
    // Spotlights on highlighted elements, drawn inside the recording's own picture (the viewport, which the camera
    // moves and clips): the dim around the element never reaches the theme background, the overlays, the subtitles or
    // the watermark (brightness dip), and stays light (HIGHLIGHT_DIM), the outline and its glow carrying the emphasis.
    const active = (sc.highlights || []).filter(h => t >= h[0] && t < h[1]);
    while (highlightNodes.length < active.length) highlightNodes.push(el('div', 'spot', vp));
    highlightNodes.forEach((node, i) => {
      const h = active[i];
      if (!h) { node.style.display = 'none'; return; }
      node.style.display = 'block';
      const a = Math.min(out3(prog(t, h[0], 0.3)), 1 - prog(t, h[1] - 0.3, 0.3));
      const [u, v, bw, bh] = h.slice(2);
      const pad = 14 / Math.max(z, 1e-3);
      Object.assign(node.style, { left: (u * vpW - pad) + 'px', top: (v * vpH - pad) + 'px', width: (bw * vpW + 2 * pad) + 'px', height: (bh * vpH + 2 * pad) + 'px', opacity: String(a) });
      node.style.boxShadow = `0 0 0 6000px rgba(2,5,12,${HIGHLIGHT_DIM * a}), 0 0 32px color-mix(in srgb, var(--accent) 50%, transparent)`;
    });
    // The cursor and its clicks.
    const cur = sc.cursor;
    const pos = cur.visible ? cursorAt(cur.samples, t) : null;
    if (!pos) { cursorNode.style.display = 'none'; }
    else {
      cursorNode.style.display = 'block';
      const [px, py] = layout.toCanvas(pos[0], pos[1], c);
      let press = 1;
      for (const k2 of cur.clicks || []) { const d = t - k2[0]; if (d >= 0 && d < 0.22) press = Math.min(press, 0.84 + 0.16 * (d / 0.22)); }
      const s = cur.size * (1 + (z - 1) * 0.35) * press;
      const ox = cur.style === 'arrow' ? -3 * s : -15 * s, oy = cur.style === 'arrow' ? -2 * s : -15 * s;
      cursorNode.style.transform = `translate(${px + ox}px, ${py + oy}px) scale(${s})`;
    }
    const live = (cur.click_effect === 'none' ? [] : (cur.clicks || [])).filter(k2 => t >= k2[0] && t < k2[0] + 0.62).slice(-3);
    ripples.forEach((r, i) => {
      const k2 = live[i];
      if (!k2) { r.style.opacity = '0'; return; }
      const p = (t - k2[0]) / 0.62;
      const [px, py] = layout.toCanvas(k2[1], k2[2], c);
      const radius = (cur.click_effect === 'pulse' ? 26 : 10 + 44 * out3(p)) * (1 + (z - 1) * 0.4);
      Object.assign(r.style, { left: (px - radius) + 'px', top: (py - radius) + 'px', width: 2 * radius + 'px', height: 2 * radius + 'px',
        opacity: String(0.7 * (1 - p)) });
    });
    updateOverlays(overlays, t, layout, c);
  };
}

function buildCard(layer, sc) {
  const center = sc.type === 'outro' || sc.align !== 'left';
  const update = buildBackground(layer, sc.style === 'minimal' ? 'solid' : S.doc.theme.background_style, sc.start,
    center ? 'center' : 'start');
  const card = el('div', 'card' + (center ? ' center' : ''), layer);
  const marginX = S.W * (center ? 0.1 : 0.09);
  Object.assign(card.style, { left: marginX + 'px', right: marginX + 'px' });
  if (sc.style === 'spotlight') {
    const glow = el('div', 'blob', layer);
    const size = Math.min(S.W, S.H) * 1.1;
    Object.assign(glow.style, { width: size + 'px', height: size + 'px', left: (S.W - size) / 2 + 'px', top: (S.H - size) / 2 + 'px',
      background: 'radial-gradient(circle, color-mix(in srgb, var(--accent) 26%, transparent) 0%, transparent 60%)' });
    layer.insertBefore(glow, card);
  }
  if (sc.style === 'split') {
    const panel = el('div', '', layer);
    Object.assign(panel.style, { position: 'absolute', left: 0, top: 0, bottom: 0, width: '0.9%', background: 'linear-gradient(var(--grad-from), var(--grad-to))' });
  }
  let logo = null;
  if (sc.show_logo && S.doc.theme.logo) { logo = el('img', 'logo', card); logo.dataset.brand = 'logo'; setAsset(logo, 'logo'); }
  let kicker = null;
  if (sc.kicker) { kicker = el('div', 'kicker', card); kicker.textContent = sc.kicker; }
  const mo = MO(), T = mo.time, ti = mo.title, cm = mo.card;
  const E = easeOf('enter'), P = easeOf('emphasis');
  const title = el('div', 'title', card);
  const words = textWords(title, sc.title, true);
  for (const w of words) if (w.classList.contains('hl')) gradientText(w, w.textContent);
  if (ti.split === 'letters') splitLetters(words);
  if (ti.style === 'mask') maskWords(words);
  const maxW = S.W - 2 * marginX;
  fitText(title, maxW, S.H * 0.42, sc.type === 'outro' ? 104 : 120, 48);
  let sub = null;
  if (sc.subtitle) { sub = el('div', 'subtitle', card); sub.textContent = sc.subtitle; }
  const rule = sc.type === 'title' && !center ? el('div', 'rule', card) : null;
  if (rule) rule.style.width = '220px';
  let cta = null, url = null;
  if (sc.cta) { cta = el('div', 'cta', card); cta.textContent = sc.cta; }
  if (sc.url) { url = el('div', 'ctaurl', card); url.textContent = sc.url; }
  const overlays = buildOverlays(layer, sc.overlays);
  const fit = frameText(card, sc, overlays, S.H * 0.12, center);
  // The parts the title comes in by - its words, letters or lines (laid out by now) - one after the other.
  const parts = titleParts(title, words, ti.split);
  const after = 0.3 * T + parts.length * ti.stagger;
  const logoSeconds = (cm.logo === 'mask' ? 0.9 : 0.6) * T;
  const grads = [...title.querySelectorAll('.grad')];
  const draw = (t) => {
    update(t);
    const life = clamp(t / Math.max(sc.end - sc.start, 0.1), 0, 1);
    card.style.transform = `scale(${fit * (1 + cm.drift * life)})`;
    if (logo) drawLogo(logo, cm.logo, prog(t, 0.05 * T, logoSeconds), E, P);
    if (kicker) {
      const p = E(prog(t, 0.12 * T, 0.55 * T));
      kicker.style.opacity = String(p);
      if (cm.kicker !== 'fade') kicker.style.transform = `translateY(${(1 - p) * 18}px)`;
      if (cm.kicker === 'track') kicker.style.letterSpacing = (0.32 - 0.1 * p) + 'em';
    }
    parts.forEach((nodes, i) => drawTitlePart(nodes, prog(t, 0.22 * T + i * ti.stagger, ti.duration), ti, E, P));
    if (sub) { const p = E(prog(t, after + 0.15 * T, 0.6 * T)); sub.style.opacity = String(p); sub.style.transform = `translateY(${(1 - p) * 20}px)`; }
    if (rule) rule.style.transform = `scaleX(${E(prog(t, after, 0.8 * T))})`;
    if (cta) {
      const p = prog(t, after + 0.35 * T, 0.6 * T); cta.style.opacity = String(E(p));
      const pulse = 1 + 0.02 * Math.sin(Math.max(0, t - after - 1 * T) * 3.2);
      cta.style.transform = cm.cta === 'rise' ? `translateY(${(1 - E(p)) * 24}px)` : `translateY(${(1 - P(p)) * 24}px) scale(${pulse})`;
    }
    if (url) { const p = E(prog(t, after + 0.6 * T, 0.5 * T)); url.style.opacity = String(p); }
    if (cm.accent === 'sweep') for (const g of grads) sweepGradient(g, t - after - 0.2 * T);
    updateOverlays(overlays, t, null, null);
  };
  // When the last block is fully in: the check measures what is left to read it.
  draw.info = {
    fit,
    reveal_end: Math.max(logo ? 0.05 * T + logoSeconds : 0, kicker ? 0.67 * T : 0,
      0.22 * T + ti.duration + Math.max(parts.length - 1, 0) * ti.stagger,
      sub ? after + 0.75 * T : 0, rule ? after + 0.8 * T : 0, cta ? after + 0.95 * T : 0, url ? after + 1.1 * T : 0),
  };
  return draw;
}

// A title's words cut into letters: each letter its own part, a word kept whole on its line; an accent word,
// drawn in the brand gradient, stays one part - a gradient through the word, not one per letter.
function splitLetters(words) {
  for (const w of words) {
    if (w.querySelector('.grad')) continue;
    const text = w.textContent;
    w.textContent = '';
    w.classList.remove('w');
    w.classList.add('ww');
    const pieces = typeof Intl !== 'undefined' && Intl.Segmenter
      ? [...new Intl.Segmenter(undefined, { granularity: 'grapheme' }).segment(text)].map(s => s.segment)
      : [...text];
    for (const ch of pieces) { const l = el('span', 'w l', w); l.textContent = ch; }
  }
}

// Each word drawn through its own window (the `mask` style): it rises into view from under its baseline.
function maskWords(words) {
  for (const w of words) {
    for (const node of w.classList.contains('ww') ? [...w.children] : [w]) {
      const inner = el('span', 'wi');
      while (node.firstChild) inner.appendChild(node.firstChild);
      node.appendChild(inner);
      node.classList.add('mk');
    }
  }
}

// The nodes each part of a title moves: a word, a letter, or the words of one line (never the spaces between words).
function titleParts(title, words, split) {
  const units = words.flatMap(w => (w.classList.contains('ww') ? [...w.children] : [w]));
  if (split !== 'lines') return units.map(n => [n]);
  const lines = [];
  let top = null;
  for (const n of units) {
    const y = Math.round(n.offsetTop);
    if (top === null || Math.abs(y - top) > 4) { lines.push([]); top = y; }
    lines[lines.length - 1].push(n);
  }
  return lines;
}

// One part of a title at its progress *p*, drawn in the title style of the brand.
function drawTitlePart(nodes, p, ti, E, P) {
  const style = ti.style;
  const e = (style === 'pop' ? P : E)(p);
  for (const w of nodes) {
    if (style === 'mask') {
      w.style.opacity = String(Math.min(1, p * 4));
      if (w.firstChild && w.firstChild.style) w.firstChild.style.transform = `translateY(${(1 - e) * 112}%)`;
      continue;
    }
    w.style.opacity = String(style === 'pop' ? Math.min(1, p * 3) : e);
    const dy = (style === 'pop' ? 0.4 : 1) * (1 - e) * ti.distance;
    let transform = `translateY(${dy}px) rotate(${(1 - e) * ti.rotate}deg)`;
    if (ti.scale !== 1) transform += ` scale(${lerp(ti.scale, 1, e)})`;
    w.style.transform = transform;
    w.style.filter = p < 1 && ti.blur > 0 ? `blur(${(1 - e) * ti.blur}px)` : 'none';
  }
}

// A card's logo coming in: rising, fading, scaled up on a spring, or uncovered from its left edge.
function drawLogo(logo, mode, p, E, P) {
  if (mode === 'fade') { logo.style.opacity = String(E(p)); return; }
  if (mode === 'scale') { logo.style.opacity = String(Math.min(1, p * 3)); logo.style.transform = `scale(${lerp(0.6, 1, P(p))})`; return; }
  if (mode === 'mask') {
    const e = E(p);
    logo.style.opacity = String(Math.min(1, p * 3));
    logo.style.clipPath = `inset(-10% ${(1 - e) * 100}% -10% -10%)`;
    logo.style.transform = `translateX(${(1 - e) * -12}px)`;
    return;
  }
  const e = E(p);
  logo.style.opacity = String(e);
  logo.style.transform = `translateY(${(1 - e) * 14}px)`;
}

// A light crossing a gradient word, *u* seconds after it starts, every 3.6 s.
function sweepGradient(node, u) {
  const period = 3.6, cross = 1.1;
  const local = u < 0 ? -1 : u % period;
  if (local < 0 || local > cross) {
    node.style.backgroundImage = '';
    return;
  }
  const x = lerp(-30, 130, inOut(local / cross));
  node.style.backgroundImage = `linear-gradient(100deg, transparent ${x - 14}%, rgba(255,255,255,.7) ${x}%, transparent ${x + 14}%), `
    + 'linear-gradient(100deg, var(--grad-from), var(--grad-to))';
}

// What plays behind a statement - a video held on its last frame, or a moving
// image - darkened most where the words are; nothing to show yet (a shot being
// made, failed): the brand backdrop instead.
function buildBackdrop(layer, sc, compose) {
  const bd = sc.backdrop;
  const url = bd ? assetUrl(bd.src) : null;
  if (!bd || (S.media && !url)) return buildBackground(layer, S.doc.theme.background_style, sc.start, compose);
  const box = el('div', 'backdrop', layer);
  let show;
  // A screen insert sits on the footage where object-fit puts it, and moves with an image's motion.
  const footage = bd.kind === 'video' ? [bd.src_w, bd.src_h] : [bd.image_w, bd.image_h];
  const moving = el('div', 'inshost', box);
  Object.assign(moving.style, { width: '100%', height: '100%' });
  const rect = coverRect(S.W, S.H, footage[0] || S.W, footage[1] || S.H);
  const inserts = buildInserts(insertHost(moving, rect), sc, rect[2], rect[3], bd.kind === 'video' ? 1 : STILL_HOST_ZOOM);
  if (bd.kind === 'video') {
    const shot = el('div', 'shot', box);
    const frames = frameSource(shot, bd);
    shot.appendChild(moving);
    const held = heldSpan(bd.time_map);
    show = (t) => {
      frames.show(mapTime(bd.time_map, t), mapRate(bd.time_map, t));
      inserts(t, mapTime(bd.time_map, t));
      driftShot(shot, held, t);
    };
  } else {
    const img = el('img', '', box);
    box.appendChild(moving);
    setAsset(img, bd.src);
    show = (t) => {
      const motion = motionTransform(bd.motion, clamp(t / Math.max(sc.end - sc.start, 0.1), 0, 1));
      img.style.transform = motion;
      moving.style.transform = motion;
      inserts(t);
    };
  }
  const dim = el('div', 'dim', layer);
  dim.style.opacity = String(Math.min(1, bd.dim * 1.3));
  dim.style.background = `radial-gradient(ellipse 75% 70% at ${compose === 'start' ? '32%' : '50%'} 50%, var(--bg) 0%, color-mix(in srgb, var(--bg) 50%, transparent) 100%)`;
  return show;
}

function buildStatement(layer, sc) {
  const center = sc.align !== 'left';
  const update = buildBackdrop(layer, sc, center ? 'center' : 'start');
  const card = el('div', 'card stmt' + (center ? ' center' : ''), layer);
  const marginX = S.W * 0.09;
  Object.assign(card.style, { left: marginX + 'px', right: marginX + 'px' });
  const maxW = S.W - 2 * marginX;
  let logo = null;
  if (sc.show_logo && S.doc.theme.logo) { logo = el('img', 'logo', card); logo.dataset.brand = 'logo'; setAsset(logo, 'logo'); }
  let kicker = null;
  if (sc.kicker) { kicker = el('div', 'kicker', card); kicker.textContent = sc.kicker; }
  let figure = null, figureText = null;
  const fig = parseFigure(sc.figure);
  if (sc.figure) {
    figure = el('div', 'figure', card);
    figureText = gradientText(figure, fig ? formatFigure(fig, fig.target) : sc.figure);
    fitText(figure, maxW, S.H * 0.22, 180, 64);
  }
  const block = el('div', 'lines', card);
  const lines = (sc.lines || []).map(text => {
    const line = el('div', 'sline', block);
    return { line, words: textWords(line, text, false, 'hl') };
  });
  fitText(block, maxW, S.H * (figure ? 0.34 : 0.5), 104, 40);
  const subs = (sc.secondary || []).map(text => { const s = el('div', 'ssub', card); textWords(s, text, false, 'hl'); return s; });
  const chips = [];
  if ((sc.chips || []).length) {
    const row = el('div', 'chips', card);
    for (const label of sc.chips) { const chip = el('span', 'chip', row); chip.textContent = label; chips.push(chip); }
  }
  const overlays = buildOverlays(layer, sc.overlays);
  const fit = frameText(card, sc, overlays, S.H * 0.1, center);
  // When each block comes in: the kicker, the figure counting up, the lines
  // (or their words), the smaller lines, then the chips - in the brand's tempo,
  // each line on a beat of the music when the video asks it (`beat_sync: all`).
  const mo = MO(), T = mo.time, bl = mo.block;
  const E = easeOf('enter'), P = easeOf('emphasis');
  const words = lines.reduce((n, line) => n + line.words.length, 0);
  const step = sc.reveal === 'together' ? 0 : bl.step;
  const plan = {};
  let at = 0.12 * T;
  if (kicker) { plan.kicker = at; at += 0.2 * T; }
  if (figure) { plan.figure = at; at += 0.45 * T; }
  plan.lines = at;
  const lineAt = lines.map((_, i) => onBeat(sc, plan.lines + i * step));
  const linesSpan = sc.beats && sc.beats.length ? Math.max(lineAt[lineAt.length - 1] - plan.lines, 0) : Math.max(lines.length - 1, 0) * step;
  at += sc.reveal === 'words' ? bl.word * words + 0.3 * T : linesSpan + 0.5 * T;
  plan.subs = at;
  at += subs.length ? subs.length * 0.25 * T + 0.2 * T : 0;
  plan.chips = at;
  const rise = (node, t, from) => {
    const p = prog(t, from, bl.duration), e = E(p);
    node.style.opacity = String(e);
    node.style.transform = `translateY(${(1 - e) * bl.distance}px)`;
    node.style.filter = p < 1 && bl.blur > 0 ? `blur(${(1 - e) * bl.blur}px)` : 'none';
  };
  const logoSeconds = (mo.card.logo === 'mask' ? 0.9 : 0.6) * T;
  const draw = (t) => {
    update(t);
    card.style.transform = `scale(${fit * (1 + bl.drift * clamp(t / Math.max(sc.end - sc.start, 0.1), 0, 1))})`;
    if (logo) drawLogo(logo, mo.card.logo, prog(t, 0.05 * T, logoSeconds), E, P);
    if (kicker) {
      const p = E(prog(t, plan.kicker, 0.55 * T));
      kicker.style.opacity = String(p); kicker.style.transform = `translateY(${(1 - p) * 18}px)`;
    }
    if (figure) {
      rise(figure, t, plan.figure);
      if (fig) figureText.textContent = formatFigure(fig, fig.target * E(prog(t, plan.figure, 1.2 * T)));
    }
    if (sc.reveal === 'words') {
      let k = 0;
      for (const line of lines) for (const w of line.words) rise(w, t, plan.lines + bl.word * k++);
    } else {
      lines.forEach((line, i) => rise(line.line, t, lineAt[i]));
    }
    subs.forEach((s, i) => {
      const p = E(prog(t, plan.subs + i * 0.25 * T, 0.6 * T));
      s.style.opacity = String(p); s.style.transform = `translateY(${(1 - p) * 18}px)`;
    });
    chips.forEach((chip, i) => {
      const p = prog(t, plan.chips + i * 0.12 * T, 0.5 * T);
      chip.style.opacity = String(E(p)); chip.style.transform = `translateY(${(1 - P(p)) * 16}px) scale(${0.92 + 0.08 * E(p)})`;
    });
    updateOverlays(overlays, t, null, null);
  };
  draw.info = {
    fit,
    reveal_end: Math.max(logo ? 0.05 * T + logoSeconds : 0, kicker ? plan.kicker + 0.55 * T : 0, figure ? plan.figure + 1.2 * T : 0,
      lines.length ? plan.lines + bl.duration + (sc.reveal === 'words' ? bl.word * Math.max(words - 1, 0) : linesSpan) : 0,
      subs.length ? plan.subs + (subs.length - 1) * 0.25 * T + 0.6 * T : 0,
      chips.length ? plan.chips + (chips.length - 1) * 0.12 * T + 0.5 * T : 0),
  };
  return draw;
}

// The beat of the music a moment of a scene lands on (`beat_sync: all`): the nearest one, never earlier than
// the scene's start; the moment itself when the music has no beats or the video does not ask.
function onBeat(sc, at) {
  const beats = sc.beats;
  if (!beats || !beats.length) return at;
  let best = at, gap = Infinity;
  for (const b of beats) { const d = Math.abs(b - at); if (d < gap && b >= 0) { gap = d; best = b; } }
  return gap <= (sc.beat_tolerance || 0.15) ? best : at;
}

function motionTransform(motion, life) {
  const e = inOut(life);
  switch (motion) {
    case 'zoom_in': return `scale(${1 + 0.14 * e})`;
    case 'zoom_out': return `scale(${1.14 - 0.14 * e})`;
    case 'pan_left': return `scale(1.1) translateX(${3.5 - 7 * e}%)`;
    case 'pan_right': return `scale(1.1) translateX(${-3.5 + 7 * e}%)`;
    case 'none': return 'none';
    default: return `scale(${1.02 + 0.07 * e}) translate(${-1.2 * e}%, ${-0.8 * e}%)`;
  }
}

// How far a parallax layer of an animated still moves beyond the image, with the scene's own
// camera motion: more the nearer it is (depth), outward on a zoom, along a pan, a slow float
// when the image holds still.
function parallaxShift(motion, life, t, depth, cx, cy, W, H) {
  const e = inOut(life);
  switch (motion) {
    case 'pan_left': return [-0.04 * W * depth * (2 * e - 1), 0];
    case 'pan_right': return [0.04 * W * depth * (2 * e - 1), 0];
    case 'zoom_in': return [(cx - 0.5) * W * 0.1 * depth * e, (cy - 0.5) * H * 0.1 * depth * e];
    case 'zoom_out': return [(cx - 0.5) * W * 0.1 * depth * (1 - e), (cy - 0.5) * H * 0.1 * depth * (1 - e)];
    case 'none': return [0, -0.006 * H * depth * Math.sin(2 * Math.PI * t / 4)];
    default: return [-0.03 * W * depth * e, -0.02 * H * depth * e];
  }
}

// Where a light sweep is at time t, 0-1 across the image, or -1 between two sweeps.
function sweepProgress(fx, t) {
  if (t < fx.at) return -1;
  const local = fx.repeat ? (t - fx.at) % fx.repeat : t - fx.at;
  return local <= fx.duration ? local / fx.duration : -1;
}

// The effects of an animated still, drawn on the real image without any model: inside the stage
// the camera moves, so a region given in shares of the image stays on it.
function buildEffects(stage, sc, url, W, H) {
  const parts = [];
  const place = (node, [x, y, w, h]) => Object.assign(node.style, {
    left: x * 100 + '%', top: y * 100 + '%', width: w * 100 + '%', height: h * 100 + '%',
  });
  for (const fx of sc.effects) {
    if (fx.type === 'parallax') {
      fx.layers.forEach((layer, i) => {
        const [x, y, w, h] = layer.box;
        const socket = el('div', 'socket', stage);
        place(socket, layer.box);
        const lift = el('div', 'lift', stage);
        place(lift, layer.box);
        const copy = el('img', '', lift);
        Object.assign(copy.style, { width: 100 / w + '%', height: 100 / h + '%', left: -x / w * 100 + '%', top: -y / h * 100 + '%' });
        setImg(copy, url);
        const start = fx.at + i * 0.15;
        parts.push((t, life) => {
          const up = out3(prog(t, start, 0.8));
          const [dx, dy] = parallaxShift(sc.motion, life, t, layer.depth, x + w / 2, y + h / 2, W, H);
          lift.style.transform = `translate(${dx * up}px, ${dy * up - 6 * layer.depth * up}px) scale(${1 + 0.05 * layer.depth * up})`;
          lift.style.boxShadow = `0 ${Math.round(24 * layer.depth * up)}px ${Math.round(60 * layer.depth * up)}px rgba(0,0,0,${(0.5 * up).toFixed(3)})`;
          socket.style.opacity = (0.35 * up).toFixed(3);
        });
      });
    } else if (fx.type === 'light_sweep') {
      const band = el('i', '', el('div', 'sweep', stage));
      band.style.width = fx.width * 100 + '%';
      band.style.background = `linear-gradient(90deg, transparent, rgba(255,255,255,${fx.strength}), transparent)`;
      parts.push((t) => {
        const p = sweepProgress(fx, t);
        band.style.opacity = p < 0 ? '0' : '1';
        if (p >= 0) band.style.transform = `translateX(${lerp(-(fx.width + 0.3) * W, 1.15 * W, inOut(p))}px) rotate(${fx.angle}deg)`;
      });
    } else if (fx.type === 'pulse') {
      const ring = el('div', fx.shape === 'ellipse' ? 'pulse round' : 'pulse', stage);
      place(ring, fx.box);
      parts.push((t) => {
        const local = t - fx.at;
        const active = local >= 0 && (fx.count == null || local < fx.count * fx.period);
        const wave = active ? 0.5 - 0.5 * Math.cos(2 * Math.PI * local / fx.period) : 0;
        ring.style.opacity = (fx.strength * wave).toFixed(3);
        ring.style.transform = `scale(${1 + 0.04 * wave})`;
      });
    }
  }
  return (t, life) => { for (const part of parts) part(t, life); };
}

function buildStill(layer, sc, isClip) {
  const update = buildBackground(layer, S.doc.theme.background_style, sc.start);
  const box = el('div', 'still', layer);
  const iw = isClip ? sc.src_w : sc.image_w, ih = isClip ? sc.src_h : sc.image_h;
  const ar = iw / Math.max(ih, 1), car = S.W / S.H;
  // A framed picture stands above the subtitles; a full-bleed one is footage they play over.
  const room = reserveOf(sc);
  // A slide is a page whose text runs to its edges: of the frame's own shape, it is full bleed until the
  // subtitles are said over it, then scaled to stand above their band. A figure is drawn clear of
  // the band in its own frame (``figures.caption_safe_area``).
  const page = !!sc.framed && (room.top > 0 || room.bottom > 0);
  const edge = page ? S.H * 0.03 : S.H * 0.09;
  const padTop = Math.max(edge, room.top), padBottom = Math.max(edge, room.bottom);
  let w, h;
  let cover = !page && (sc.fit === 'cover' || Math.abs(ar - car) / car < 0.03);
  if (cover) { if (ar > car) { h = S.H; w = h * ar; } else { w = S.W; h = w / ar; } }
  else { const maxW = page ? S.W : S.W * 0.86, maxH = S.H - padTop - padBottom; w = Math.min(maxW, maxH * ar); h = w / ar; box.classList.add('card2'); }
  // The preview does not know an image's size before it loads: the box takes
  // the safe area and the picture fits itself in it.
  if (S.media && sc.preview_unsized) {
    cover = sc.fit === 'cover';
    w = cover ? S.W : S.W * 0.86; h = cover ? S.H : S.H - padTop - padBottom;
    box.classList.toggle('card2', !cover);
  }
  const top = cover ? (S.H - h) / 2 : padTop + (S.H - padTop - padBottom - h) / 2;
  Object.assign(box.style, { left: (S.W - w) / 2 + 'px', top: top + 'px', width: w + 'px', height: h + 'px' });
  const shot = isClip ? el('div', 'shot', box) : null;
  const frames = isClip ? frameSource(shot, sc) : null;
  // A looping end: the same footage again on a layer over it, silent, so the clip's sound plays once.
  const ext = isClip ? sc.extend || null : null;
  const looped = ext && ext.mode === 'loop' ? (S.media ? loopLayer(shot, sc) : frameSource(shot, sc)) : null;
  if (looped) looped.node.style.opacity = '0';
  const held = isClip ? driftSpan(sc) : null;
  // An animated still's image and its effects share one stage, which the camera moves.
  const stage = !isClip && sc.effects && sc.effects.length ? el('div', 'fxstage', box) : null;
  const img = isClip ? null : el('img', '', stage || box);
  // A screen insert on a still sits on the picture and moves with its motion, as on a statement's backdrop - once the
  // picture is framed at its own size (a preview that could not read it draws none).
  const moving = img && (sc.inserts || []).length && !(S.media && sc.preview_unsized) ? el('div', 'inshost', stage || box) : null;
  if (moving) Object.assign(moving.style, { width: '100%', height: '100%' });
  const inserts = isClip ? buildInserts(shot, sc, w, h, 1) : moving ? buildInserts(moving, sc, w, h, STILL_HOST_ZOOM) : null;
  let effects = null;
  if (img && S.media && sc.preview_unsized) img.style.objectFit = sc.fit === 'cover' ? 'cover' : 'contain';
  if (img) {
    const url = assetUrl(sc.src);
    if (url) {
      setAsset(img, sc.src);
      if (stage) effects = buildEffects(stage, sc, url, w, h);
    }
    // A deck slide renders at render time: the preview names it.
    else if (S.media) { img.remove(); placeholder(box, sc.preview_label); }
  }
  const overlays = buildOverlays(layer, sc.overlays);
  placeOver(overlays, sc);
  // A figure stood in space: leaning back and turning towards the camera as it builds, or turning slowly
  // from one side to the other. Its own frames stay flat; the picture is what turns. Its poses are the ones the
  // figure's fit is judged at (`figure_fit.STANDS`).
  const stood = sc.figure && sc.perspective && FIGURE_STANDS.poses[sc.perspective] ? FIGURE_STANDS.poses[sc.perspective] : null;
  if (stood) {
    layer.style.perspective = Math.round(Math.max(S.W, S.H) * FIGURE_STANDS.perspective) + 'px';
    box.style.transformOrigin = '50% 50%';
  }
  return (t) => {
    update(t);
    const length = Math.max(sc.end - sc.start, 0.1);
    if (stood) {
      const e = easeOf('move')(clamp(t / length, 0, 1));
      box.style.transform = `scale(${stood.scale}) rotateX(${lerp(stood.from[0], stood.to[0], e)}deg) `
        + `rotateY(${lerp(stood.from[1], stood.to[1], e)}deg)`;
    }
    if (isClip) {
      // The footage second on screen, which a tracked insert follows: in a loop, the layer the crossfade shows most.
      let at = mapTime(sc.time_map, t);
      if (looped && t >= ext.from - ext.fade) {
        const [under, over, alpha] = loopTimes(ext, sc, t);
        frames.show(under, ext.speed);
        looped.show(over, ext.speed);
        looped.node.style.opacity = String(alpha);
        at = alpha < 0.5 ? under : over;
      } else {
        frames.show(at, mapRate(sc.time_map, t));
        if (looped) looped.node.style.opacity = '0';
      }
      inserts(t, at);
      driftShot(shot, held, t);
    } else if (img) {
      const life = clamp(t / length, 0, 1);
      const motion = motionTransform(sc.motion, life);
      (stage || img).style.transform = motion;
      if (moving && !stage) moving.style.transform = motion;
      if (effects) effects(t, life);
      if (inserts) inserts(t);
    }
    updateOverlays(overlays, t, null, null);
  };
}

// ── Motion scenes ────────────────────────────────────────────────
// A motion scene draws elements (`motion_design.compose`): text, shapes,
// paths, pictures, Lottie animations, particles. Each frame every element is
// drawn from its keyframes, then its enter / exit / loop presets (drawn with
// the brand's tokens), its motion path and its mask, seen through the scene's
// 3D camera: a pure function of time like every other scene. Lottie and the
// particles are runtimes the element seeks to the frame's time, never a clock.

const COLOR_VARS = {
  foreground: 'var(--fg)', muted: 'var(--muted)', accent: 'var(--accent)', accent_2: 'var(--accent2)',
  surface: 'var(--surface)', background: 'var(--bg)', on_accent: 'var(--on-accent)', white: '#ffffff', black: '#000000',
};
let gradientIds = 0;
let measureSvg = null;

// A channel's value at *t* from the keyframes that set it, eased into each by the later one's easing.
function keyed(keys, name, t, fallback) {
  let a = null, b = null;
  for (const k of keys || []) {
    if (k[name] === undefined || k[name] === null) continue;
    if (k.t <= t) a = k;
    else { b = k; break; }
  }
  if (!a && !b) return fallback;
  if (!a) return b[name];
  if (!b) return a[name];
  return lerp(a[name], b[name], easeOf(b.ease || 'move')((t - a.t) / Math.max(b.t - a.t, 1e-6)));
}

// The delay of each part of a split text: in order, from the end, from the centre out, or shuffled by its seed.
function partDelays(n, order, stagger, seed) {
  const idx = [...Array(n).keys()];
  if (order === 'end') return idx.map(i => (n - 1 - i) * stagger);
  if (order === 'center') return idx.map(i => Math.abs(i - (n - 1) / 2) * stagger);
  if (order === 'random') {
    const r = seeded(seed);
    const rank = new Array(n);
    idx.map(i => [r(), i]).sort((a, b) => a[0] - b[0]).forEach(([, i], k) => { rank[i] = k; });
    return idx.map(i => rank[i] * stagger);
  }
  return idx.map(i => i * stagger);
}

const freshState = (e) => ({
  x: e.x, y: e.y, scale: e.scale, rotate: e.rotate, rx: 0, ry: 0, z: e.z, opacity: e.opacity, blur: e.blur,
  reveal: 1, draw: 1, morph: 0, progress: null, dx: 0, dy: 0, track: 0, shift: 0, clip: null,
});

// What an enter preset does to an element (or a part of a text) at progress *p* (eased: *k*); *D* the rise distance.
function presetIn(v, preset, p, k, D) {
  switch (preset) {
    case 'fade': v.opacity *= k; break;
    case 'rise': v.opacity *= k; v.dy += (1 - k) * D; break;
    case 'drop': v.opacity *= k; v.dy -= (1 - k) * D; break;
    case 'slide_left': v.opacity *= k; v.dx += (1 - k) * D * 2; break;
    case 'slide_right': v.opacity *= k; v.dx -= (1 - k) * D * 2; break;
    case 'scale': v.opacity *= Math.min(1, p * 3); v.scale *= lerp(0.6, 1, k); break;
    case 'pop': v.opacity *= Math.min(1, p * 4); v.scale *= lerp(0.3, 1, k); break;
    case 'blur': v.opacity *= k; v.blur += (1 - k) * Math.max(MO().title.blur, 18); v.scale *= lerp(1.06, 1, k); break;
    case 'track': v.opacity *= k; v.track += (1 - k) * 0.3; break;
    case 'flip': v.opacity *= Math.min(1, p * 3); v.rx += (1 - k) * -90; break;
    case 'mask_up': v.opacity *= Math.min(1, p * 4); v.shift = Math.max(v.shift, 1 - k); break;
    case 'mask_down': v.opacity *= Math.min(1, p * 4); v.shift = Math.min(v.shift, -(1 - k)); break;
    case 'mask_left': v.clip = `inset(-8% -8% -8% ${(1 - k) * 100}%)`; break;
    case 'mask_right': v.clip = `inset(-8% ${(1 - k) * 100}% -8% -8%)`; break;
    case 'iris': v.clip = `circle(${k * 75}% at 50% 50%)`; break;
    case 'draw': v.draw = Math.min(v.draw, k); break;
    case 'typewriter': v.opacity *= p > 0 ? 1 : 0; break;
    default: break;
  }
}

// What an exit preset does at progress *p* (eased: *k*): the enter's move, the other way.
function presetOut(v, preset, p, k, D) {
  switch (preset) {
    case 'fade': v.opacity *= 1 - k; break;
    case 'rise': v.opacity *= 1 - k; v.dy -= k * D; break;
    case 'drop': v.opacity *= 1 - k; v.dy += k * D; break;
    case 'slide_left': v.opacity *= 1 - k; v.dx -= k * D * 2; break;
    case 'slide_right': v.opacity *= 1 - k; v.dx += k * D * 2; break;
    case 'scale': v.opacity *= 1 - k; v.scale *= lerp(1, 0.6, k); break;
    case 'blur': v.opacity *= 1 - k; v.blur += k * 20; break;
    case 'mask_up': v.shift = -k; break;
    case 'mask_down': v.shift = k; break;
    case 'mask_left': v.clip = `inset(-8% ${k * 100}% -8% -8%)`; break;
    case 'mask_right': v.clip = `inset(-8% -8% -8% ${k * 100}%)`; break;
    case 'iris': v.clip = `circle(${(1 - k) * 75}% at 50% 50%)`; break;
    default: break;
  }
}

// What a loop preset does *u* seconds into it.
function presetLoop(v, loop, u) {
  const ph = 2 * Math.PI * u / loop.period, a = loop.amount, wave = 0.5 - 0.5 * Math.cos(ph);
  switch (loop.preset) {
    case 'float': v.dy += Math.sin(ph) * 14 * a; break;
    case 'pulse': v.scale *= 1 + 0.06 * a * wave; break;
    case 'breathe': v.opacity *= 1 - 0.35 * a * wave; v.scale *= 1 + 0.04 * a * wave; break;
    case 'spin': v.rotate += 360 * u / loop.period; break;
    case 'sway': v.rotate += Math.sin(ph) * 6 * a; break;
    default: break;
  }
}

// The mask a `reveal` keyframe opens: a rectangle from one side (soft-edged with a feather), or a circle.
function revealMask(node, mask, r) {
  if (!mask) return;
  if (mask.shape === 'circle') { node.style.clipPath = `circle(${r * 75}% at 50% 50%)`; return; }
  const cut = (1 - r) * 100, f = (mask.feather || 0) * 100;
  if (f > 0 && mask.side !== 'center') {
    const dir = { left: 'to right', right: 'to left', top: 'to bottom', bottom: 'to top' }[mask.side] || 'to right';
    const edge = r * (100 + f);
    const gradient = `linear-gradient(${dir}, #000 ${edge - f}%, transparent ${edge}%)`;
    node.style.maskImage = gradient; node.style.webkitMaskImage = gradient;
    return;
  }
  node.style.clipPath = {
    left: `inset(0 ${cut}% 0 0)`, right: `inset(0 0 0 ${cut}%)`, top: `inset(0 0 ${cut}% 0)`,
    bottom: `inset(${cut}% 0 0 0)`, center: `inset(${cut / 2}% ${cut / 2}%)`,
  }[mask.side] || `inset(0 ${cut}% 0 0)`;
}

function measureLayer() {
  if (!measureSvg) {
    measureSvg = svgEl('svg', { width: 0, height: 0, style: 'position:absolute;left:-9999px;top:-9999px' }, document.body);
  }
  return measureSvg;
}

// *n* points along path data *d*, evenly spaced: what a morph interpolates.
function samplePath(d, n) {
  const path = svgEl('path', { d }, measureLayer());
  const length = path.getTotalLength();
  const points = [];
  for (let i = 0; i < n; i++) { const q = path.getPointAtLength(length * i / (n - 1)); points.push([q.x, q.y]); }
  path.remove();
  return points;
}

const paint = (role, gid) => (role === 'gradient' ? `url(#${gid})` : role === 'none' || !role ? 'none' : COLOR_VARS[role] || role);

function gradientDef(svg) {
  const gid = 'mg' + (++gradientIds);
  const g = svgEl('linearGradient', { id: gid, x1: 0, y1: 0, x2: 1, y2: 0 }, svgEl('defs', {}, svg));
  svgEl('stop', { offset: 0, style: 'stop-color: var(--grad-from)' }, g);
  svgEl('stop', { offset: 1, style: 'stop-color: var(--grad-to)' }, g);
  return gid;
}

// Each kind's node, sized; *it* gets what its frames draw.
function buildText(it, node) {
  const e = it.e, tx = e.text;
  node.classList.add('mtext');
  node.dataset.group = 'motion:' + e.id;
  const box = el('div', 'mtx', node);
  Object.assign(box.style, {
    fontFamily: tx.font === 'body' ? 'var(--body-font)' : 'var(--heading-font)', fontWeight: String(tx.weight),
    fontSize: tx.size + 'px', letterSpacing: tx.tracking + 'em', textAlign: tx.align,
    textTransform: tx.uppercase ? 'uppercase' : 'none', color: tx.color === 'gradient' ? 'var(--fg)' : COLOR_VARS[tx.color] || 'var(--fg)',
  });
  if (tx.max_w) box.style.maxWidth = tx.max_w * S.W + 'px';
  else box.style.whiteSpace = 'pre';
  if (tx.marker && tx.marker !== 'none') {
    const mark = el('span', 'mkr ' + tx.marker, box);
    if (tx.marker === 'check') {
      const tick = svgEl('svg', { viewBox: '0 0 18 18', width: '100%', height: '100%' }, mark);
      svgEl('path', { d: 'M2 9 L7 14 L16 3', fill: 'none', stroke: 'var(--accent)', 'stroke-width': 3, 'stroke-linecap': 'round',
        'stroke-linejoin': 'round' }, tick);
    }
    it.marker = mark;
  }
  const span = el('span', 'mtt', box);
  it.textSpan = span;
  if (tx.count) {
    const word = el('span', 'w', span);
    word.textContent = tx.text;
    it.counted = parseFigure(tx.text.replace(/\*/g, '')) || null;
    it.countSpan = word;
    it.parts = [[word]];
  } else {
    const words = textWords(span, tx.text, false, 'hl');
    if (tx.color === 'gradient') for (const w of words) gradientText(w, w.textContent);
    const enter = e.enter || {};
    const split = enter.preset === 'typewriter' ? 'letters' : tx.split;
    if (split === 'letters') splitLetters(words);
    if (split !== 'none' && (enter.preset === 'mask_up' || enter.preset === 'mask_down')) maskWords(words);
    it.split = split;
    it.words = words;
  }
  if (tx.max_w) fitText(box, tx.max_w * S.W + 1, Infinity, tx.size, Math.max(12, tx.size * 0.5));
  if (!tx.count && it.split === 'none' && e.enter && (e.enter.preset === 'mask_up' || e.enter.preset === 'mask_down')) node.classList.add('masked');
}

function buildShape(it, node) {
  const e = it.e, sh = e.shape, w = it.boxW, h = it.boxH;
  // A soft glow (an ellipse blurred wide) is a radial gradient: a blur filter that large costs every frame.
  if (sh.shape === 'ellipse' && e.blur >= 24 && sh.fill !== 'none') {
    const color = sh.fill === 'gradient' ? 'var(--grad-from)' : COLOR_VARS[sh.fill] || 'var(--accent)';
    Object.assign(node.style, { width: w + 'px', height: h + 'px', borderRadius: '50%',
      background: `radial-gradient(closest-side, ${color} 0%, color-mix(in srgb, ${color} 45%, transparent) 45%, transparent 100%)` });
    it.glow = true;
    return;
  }
  const svg = svgEl('svg', { width: w, height: h, viewBox: `0 0 ${w} ${h}`, overflow: 'visible' }, node);
  const gid = sh.fill === 'gradient' || sh.stroke === 'gradient' ? gradientDef(svg) : null;
  const sw = sh.stroke !== 'none' ? sh.stroke_w : 0, i = sw / 2;
  let geo;
  if (sh.shape === 'rect') geo = svgEl('rect', { x: i, y: i, width: Math.max(w - sw, 0), height: Math.max(h - sw, 0), rx: sh.radius }, svg);
  else if (sh.shape === 'ellipse' || sh.shape === 'ring') geo = svgEl('ellipse', { cx: w / 2, cy: h / 2, rx: Math.max(w / 2 - i, 0), ry: Math.max(h / 2 - i, 0) }, svg);
  else if (sh.shape === 'line') geo = w >= h ? svgEl('line', { x1: 0, y1: h / 2, x2: w, y2: h / 2 }, svg) : svgEl('line', { x1: w / 2, y1: 0, x2: w / 2, y2: h }, svg);
  else {
    const n = sh.sides, pts = [];
    const star = sh.shape === 'star';
    for (let k = 0; k < (star ? n * 2 : n); k++) {
      const a = -Math.PI / 2 + k * Math.PI * 2 / (star ? n * 2 : n), r = star && k % 2 ? 0.45 : 1;
      pts.push(`${w / 2 + Math.cos(a) * (w / 2 - i) * r},${h / 2 + Math.sin(a) * (h / 2 - i) * r}`);
    }
    geo = svgEl('polygon', { points: pts.join(' ') }, svg);
  }
  geo.setAttribute('fill', sh.shape === 'ring' || sh.shape === 'line' ? 'none' : paint(sh.fill, gid));
  geo.setAttribute('stroke', paint(sh.stroke, gid));
  geo.setAttribute('stroke-width', String(sw));
  geo.setAttribute('stroke-linecap', 'round');
  it.geo = geo;
}

function buildPath(it, node) {
  const e = it.e, pd = e.path_d, w = it.boxW, h = it.boxH;
  const svg = svgEl('svg', { width: w, height: h, viewBox: `0 0 ${pd.view[0]} ${pd.view[1]}`, preserveAspectRatio: 'xMidYMid meet', overflow: 'visible' }, node);
  const gid = pd.fill === 'gradient' || pd.stroke === 'gradient' ? gradientDef(svg) : null;
  // The stroke is given in design pixels: in the path's own units, as the box scales them.
  const unit = Math.min(w / pd.view[0], h / pd.view[1]) || 1;
  const path = svgEl('path', { d: pd.d, fill: paint(pd.fill, gid), stroke: paint(pd.stroke, gid), 'stroke-width': pd.stroke_w / unit,
    'stroke-linecap': 'round', 'stroke-linejoin': 'round' }, svg);
  it.geo = path;
  if (pd.morph_to) {
    it.morph = { from: samplePath(pd.d, 96), to: samplePath(pd.morph_to, 96), closed: /z\s*$/i.test(pd.d) && /z\s*$/i.test(pd.morph_to) };
  }
}

function buildPicture(it, node) {
  const e = it.e, im = e.image;
  node.classList.add('mimg');
  node.dataset.group = 'motion:' + e.id;
  Object.assign(node.style, { width: it.boxW + 'px', height: it.boxH + 'px' });
  const img = el('img', '', node);
  img.alt = '';
  img.style.objectFit = im.fit;
  if (im.brand) img.dataset.brand = im.brand === 'logo' ? 'logo' : 'overlay:' + im.brand;
  setAsset(img, im.src);
  if (e.loop && e.loop.preset === 'shimmer') {
    const shine = el('div', 'mshine', node);
    const url = assetUrl(im.src);
    if (url) for (const key of ['maskImage', 'webkitMaskImage']) shine.style[key] = `url("${url}")`;
    it.shine = shine;
  }
}

function buildLottie(it, node) {
  const e = it.e, lo = e.lottie;
  const box = el('div', 'mlot', node);
  Object.assign(box.style, { width: it.boxW + 'px', height: it.boxH + 'px' });
  const url = assetUrl(lo.src);
  if (!url || typeof window.lottie === 'undefined') return;
  S.loads.push(fetch(url).then(r => r.json()).then(data => {
    it.anim = window.lottie.loadAnimation({
      container: box, renderer: 'svg', loop: false, autoplay: false, animationData: data,
      rendererSettings: { preserveAspectRatio: lo.fit === 'cover' ? 'xMidYMid slice' : 'xMidYMid meet', progressiveLoad: false },
    });
    it.lottieRate = Number(data.fr) || 30;
    it.lottieFrames = Math.max(1, Number(data.op) - Number(data.ip));
    // Loaded after its first frame was asked: seeked to it before that frame is captured (the page waits for it).
    seekLottie(it);
  }).catch(() => { it.anim = null; }));
}

// A Lottie element at the time its frame was last asked: its progress keyframes', else its own clock from its start.
function seekLottie(it) {
  if (!it.anim || !it.want) return;
  const e = it.e, frames = it.lottieFrames, from = e.enter ? e.enter.at : e.start;
  const { t, progress } = it.want;
  let f = progress != null ? progress * (frames - 0.001) : (t - from) * it.lottieRate * e.lottie.speed;
  if (progress == null) f = e.lottie.loop ? ((f % frames) + frames) % frames : clamp(f, 0, frames - 0.001);
  it.anim.goToAndStop(Math.max(0, f), true);
}

function buildParticles(it, node) {
  const e = it.e, pa = e.particles, w = it.boxW, h = it.boxH;
  const dpr = window.devicePixelRatio || 1;
  const canvas = el('canvas', '', node);
  canvas.width = Math.round(w * dpr); canvas.height = Math.round(h * dpr);
  Object.assign(canvas.style, { width: w + 'px', height: h + 'px', display: 'block' });
  const r = seeded(pa.seed + 0.5);
  it.points = Array.from({ length: pa.count }, () => ({ x: r(), y: r(), a: r() * Math.PI * 2, s: 0.4 + r() * 0.6, ph: r() * Math.PI * 2, k: 0.6 + r() * 0.8 }));
  it.ctx = canvas.getContext('2d');
  it.dpr = dpr;
  const css = getComputedStyle(document.documentElement);
  const color = pa.color === 'gradient' ? css.getPropertyValue('--grad-from') : css.getPropertyValue(
    { foreground: '--fg', muted: '--muted', accent: '--accent', accent_2: '--accent2', surface: '--surface', background: '--bg', on_accent: '--on-accent' }[pa.color] || '--accent');
  it.color = (color || '').trim() || (pa.color === 'white' ? '#ffffff' : pa.color === 'black' ? '#000000' : '#0fbcff');
}

// The particles at *t*: drifting, wrapping at the edges, twinkling, the near ones joined by a line.
function drawParticles(it, t) {
  const pa = it.e.particles, ctx = it.ctx, w = it.boxW, h = it.boxH;
  ctx.setTransform(it.dpr, 0, 0, it.dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  const wrap = (v) => ((v % 1) + 1) % 1;
  const at = it.points.map(p => [wrap(p.x + Math.cos(p.a) * pa.drift * 0.02 * p.s * t) * w, wrap(p.y + Math.sin(p.a) * pa.drift * 0.02 * p.s * t) * h]);
  ctx.strokeStyle = it.color; ctx.fillStyle = it.color;
  if (pa.connect > 0) {
    const reach = pa.connect * S.W, reach2 = reach * reach;
    ctx.lineWidth = 1;
    for (let i = 0; i < at.length; i++) {
      for (let j = i + 1; j < at.length; j++) {
        const dx = at[i][0] - at[j][0], dy = at[i][1] - at[j][1], d2 = dx * dx + dy * dy;
        if (d2 >= reach2) continue;
        ctx.globalAlpha = 0.45 * (1 - Math.sqrt(d2) / reach);
        ctx.beginPath(); ctx.moveTo(at[i][0], at[i][1]); ctx.lineTo(at[j][0], at[j][1]); ctx.stroke();
      }
    }
  }
  it.points.forEach((p, i) => {
    const [x, y] = at[i], s = pa.size * p.k;
    ctx.globalAlpha = 1 - pa.twinkle * (0.5 + 0.5 * Math.sin(t * 1.7 * p.s + p.ph));
    ctx.beginPath();
    if (pa.shape === 'square') ctx.fillRect(x - s, y - s, 2 * s, 2 * s);
    else if (pa.shape === 'plus') { ctx.lineWidth = Math.max(1, s / 2); ctx.moveTo(x - s * 1.5, y); ctx.lineTo(x + s * 1.5, y); ctx.moveTo(x, y - s * 1.5); ctx.lineTo(x, y + s * 1.5); ctx.stroke(); }
    else if (pa.shape === 'ring') { ctx.lineWidth = Math.max(1, s / 3); ctx.arc(x, y, s * 1.4, 0, Math.PI * 2); ctx.stroke(); }
    else { ctx.arc(x, y, s, 0, Math.PI * 2); ctx.fill(); }
  });
  ctx.globalAlpha = 1;
}

// A light crossing a text or a picture, *u* seconds into its loop.
function shimmerAt(it, u) {
  const loop = it.e.loop, cross = Math.min(1.2, loop.period * 0.4);
  const local = u < 0 ? -1 : u % loop.period;
  const on = local >= 0 && local <= cross;
  const x = on ? lerp(-30, 130, inOut(local / cross)) : 0;
  const strength = (0.45 + 0.5 * loop.amount).toFixed(3);
  if (it.shine) {
    it.shine.style.background = on ? `linear-gradient(100deg, transparent ${x - 14}%, rgba(255,255,255,${strength}) ${x}%, transparent ${x + 14}%)` : 'none';
    return;
  }
  if (!it.textSpan) return;
  for (const g of it.textSpan.querySelectorAll('.grad')) sweepGradient(g, on ? local : -1);
  const color = COLOR_VARS[it.e.text.color] || 'var(--fg)';
  const span = it.textSpan;
  if (!on || it.e.text.color === 'gradient') {
    span.style.backgroundImage = ''; span.style.color = ''; span.style.webkitBackgroundClip = ''; span.style.backgroundClip = '';
    return;
  }
  span.style.color = 'transparent';
  span.style.webkitBackgroundClip = 'text'; span.style.backgroundClip = 'text';
  span.style.backgroundImage = `linear-gradient(100deg, ${color} 0%, ${color} ${x - 14}%, rgba(255,255,255,${strength}) ${x}%, ${color} ${x + 14}%, ${color} 100%)`;
}

// Where a motion path puts an element at progress *q*: in shares of the frame, and its heading.
function pathPoint(it, q) {
  if (!it.motionPath) {
    const path = svgEl('path', { d: it.e.path.d }, measureLayer());
    it.motionPath = { path, length: path.getTotalLength() };
  }
  const { path, length } = it.motionPath;
  const a = path.getPointAtLength(length * clamp(q, 0, 1)), b = path.getPointAtLength(Math.min(length, length * clamp(q, 0, 1) + 1));
  return [a.x / 1000, a.y / 1000, Math.atan2(b.y - a.y, b.x - a.x) * 180 / Math.PI];
}

function buildElement(world, e) {
  const node = el('div', 'mel', world);
  const it = { e, node, boxW: (e.w || 0) * S.W, boxH: (e.h || 0) * S.H };
  // A box with one side given is square; one with neither takes a fifth of the frame's short side.
  if (!it.boxW && it.boxH) it.boxW = it.boxH;
  if (!it.boxH && it.boxW) it.boxH = it.boxW;
  if (!it.boxW && e.kind !== 'text') it.boxW = it.boxH = Math.min(S.W, S.H) * 0.2;
  if (e.kind === 'text') buildText(it, node);
  else if (e.kind === 'shape') buildShape(it, node);
  else if (e.kind === 'path') buildPath(it, node);
  else if (e.kind === 'image') buildPicture(it, node);
  else if (e.kind === 'lottie') buildLottie(it, node);
  else if (e.kind === 'particles') buildParticles(it, node);
  return it;
}

// The parts a split text comes in by, once it is laid out: its letters, words or lines.
function motionParts(it) {
  if (it.parts) return it.parts;
  if (!it.words) return [];
  const split = it.split;
  if (split === 'none') return [];
  return titleParts(it.textSpan, it.words, split);
}

function drawElement(it, t, inCamera, D) {
  const e = it.e, node = it.node;
  if (t < e.start || (e.end != null && t >= e.end)) { node.style.display = 'none'; return; }
  node.style.display = '';
  const v = freshState(e);
  for (const [key, name] of [['x', 'x'], ['y', 'y'], ['scale', 'scale'], ['rotate', 'rotate'], ['rotate_x', 'rx'], ['rotate_y', 'ry'],
    ['z', 'z'], ['opacity', 'opacity'], ['blur', 'blur'], ['reveal', 'reveal'], ['draw', 'draw'], ['morph', 'morph'], ['progress', 'progress']]) {
    v[name] = keyed(e.keys, key, t, v[name]);
  }
  if (e.path) {
    const [px, py, heading] = pathPoint(it, easeOf(e.path.ease)(prog(t, e.path.start, e.path.end - e.path.start)));
    v.x = px; v.y = py;
    if (e.path.orient) v.rotate += heading;
  }
  const enter = e.enter;
  const parts = it.partsLaid;
  if (enter && enter.preset !== 'none' && !(parts && parts.length > 1)) {
    const p = prog(t, enter.at, enter.duration);
    presetIn(v, enter.preset, p, easeOf(enter.ease)(p), D);
  }
  if (e.exit && e.exit.preset !== 'none') {
    const p = prog(t, e.exit.at, e.exit.duration);
    presetOut(v, e.exit.preset, p, easeOf(e.exit.ease)(p), D);
    node.dataset.leaving = p > 0 ? '1' : '';
  }
  if (e.loop && e.loop.preset !== 'shimmer' && t >= e.loop.at) presetLoop(v, e.loop, t - e.loop.at);
  // Placed by its anchor - under the element it stands below when it is laid out so - then moved.
  const left = v.x * S.W - e.anchor[0] * it.w + v.dx;
  const top = (it.belowTop != null && !it.keyedY ? it.belowTop : v.y * S.H - e.anchor[1] * it.h) + v.dy;
  const deep = inCamera || v.z || v.rx || v.ry;
  let transform = deep ? `translate3d(${left}px, ${top}px, ${v.z}px)` : `translate(${left}px, ${top}px)`;
  if (!inCamera && (v.rx || v.ry)) transform = 'perspective(1000px) ' + transform;
  if (v.rx) transform += ` rotateX(${v.rx}deg)`;
  if (v.ry) transform += ` rotateY(${v.ry}deg)`;
  if (v.rotate) transform += ` rotate(${v.rotate}deg)`;
  if (v.scale !== 1) transform += ` scale(${v.scale})`;
  node.style.transform = transform;
  node.style.opacity = String(clamp(v.opacity, 0, 1));
  node.style.filter = !it.glow && v.blur > 0.05 ? `blur(${v.blur}px)` : 'none';
  if (v.clip) node.style.clipPath = v.clip;
  else if (e.mask) revealMask(node, e.mask, clamp(v.reveal, 0, 1));
  else node.style.clipPath = '';
  if (it.inner) it.inner.style.transform = v.shift ? `translateY(${v.shift * 108}%)` : '';
  if (v.track && it.textSpan) it.textSpan.parentElement.style.letterSpacing = (e.text.tracking + v.track) + 'em';
  else if (it.textSpan && enter && enter.preset === 'track') it.textSpan.parentElement.style.letterSpacing = e.text.tracking + 'em';
  // Each part of a split text, on its own delay.
  if (parts && parts.length > 1 && enter) {
    const delays = it.delays;
    const ease = easeOf(enter.ease);
    parts.forEach((nodes, i) => {
      const p = prog(t, enter.at + delays[i], enter.preset === 'typewriter' ? 0 : enter.duration);
      const pv = freshState({ x: 0, y: 0, scale: 1, rotate: 0, z: 0, opacity: 1, blur: 0 });
      presetIn(pv, enter.preset, p, ease(p), D);
      for (const n of nodes) {
        if (pv.shift && n.firstChild && n.firstChild.style) n.firstChild.style.transform = `translateY(${pv.shift * 112}%)`;
        else if (n.firstChild && n.firstChild.style && n.classList.contains('mk')) n.firstChild.style.transform = '';
        n.style.opacity = String(clamp(pv.opacity, 0, 1));
        let tr = '';
        if (pv.dx || pv.dy) tr += `translate(${pv.dx}px, ${pv.dy}px)`;
        if (pv.rx) tr += ` perspective(600px) rotateX(${pv.rx}deg)`;
        if (pv.scale !== 1) tr += ` scale(${pv.scale})`;
        n.style.transform = tr;
        n.style.filter = pv.blur > 0.05 ? `blur(${pv.blur}px)` : 'none';
      }
    });
  }
  if (it.marker) {
    const at = enter ? enter.at : e.start;
    const p = prog(t, at, 0.45 * MO().time), k = easeOf('emphasis')(p);
    it.marker.style.opacity = String(Math.min(1, p * 3));
    it.marker.style.transform = `scale(${lerp(0.2, 1, k)})`;
  }
  if (it.countSpan && it.counted) {
    const from = enter ? enter.at : e.start;
    const share = v.progress != null ? v.progress : easeOf('enter')(prog(t, from, e.text.count_seconds));
    it.countSpan.textContent = formatFigure(it.counted, it.counted.target * share);
  }
  if (it.geo) {
    // Drawn on (a draw keyframe or preset) on every frame alike, whatever frames the page drew before.
    if (it.drawable === undefined) {
      it.drawable = (e.keys || []).some(k => k.draw !== undefined && k.draw !== null)
        || (e.enter && e.enter.preset === 'draw') || false;
    }
    if (it.drawable) {
      if (!it.length) it.length = it.geo.getTotalLength ? it.geo.getTotalLength() : 0;
      if (it.length) {
        it.geo.style.strokeDasharray = String(it.length);
        it.geo.style.strokeDashoffset = String(it.length * (1 - clamp(v.draw, 0, 1)));
      }
      if (it.geo.getAttribute('stroke') === 'none') it.geo.style.opacity = String(clamp(v.draw, 0, 1));
    }
    if (it.morph) {
      const m = clamp(v.morph, 0, 1);
      if (m <= 0) it.geo.setAttribute('d', e.path_d.d);
      else if (m >= 1) it.geo.setAttribute('d', e.path_d.morph_to);
      else {
        const pts = it.morph.from.map((a, i) => { const b = it.morph.to[i]; return `${lerp(a[0], b[0], m).toFixed(2)} ${lerp(a[1], b[1], m).toFixed(2)}`; });
        it.geo.setAttribute('d', 'M' + pts.join(' L') + (it.morph.closed ? ' Z' : ''));
      }
    }
  }
  if (e.kind === 'lottie') {
    it.want = { t, progress: v.progress };
    seekLottie(it);
  }
  if (it.ctx) drawParticles(it, t);
  if (e.loop && e.loop.preset === 'shimmer') shimmerAt(it, t - e.loop.at);
}

function cameraAt(cam, t) {
  const keys = cam.keys || [];
  return {
    x: keyed(keys, 'x', t, 0.5), y: keyed(keys, 'y', t, 0.5), zoom: keyed(keys, 'zoom', t, 1),
    rx: keyed(keys, 'rotate_x', t, 0), ry: keyed(keys, 'rotate_y', t, 0), r: keyed(keys, 'rotate', t, 0),
  };
}

function buildMotion(layer, sc) {
  const center = sc.align !== 'left';
  const update = buildBackground(layer, sc.background === 'solid' ? 'solid' : S.doc.theme.background_style, sc.start,
    center ? 'center' : 'start');
  const view = el('div', 'mview', layer);
  const world = el('div', 'mworld', view);
  Object.assign(world.style, { width: S.W + 'px', height: S.H + 'px' });
  if (sc.camera) view.style.perspective = sc.camera.perspective + 'px';
  const items = (sc.elements || []).map(e => buildElement(world, e));
  // Laid out at rest, in order: an element standing below another is placed under its box as drawn.
  const byId = new Map();
  for (const it of items) {
    const e = it.e;
    it.w = it.boxW || it.node.offsetWidth;
    it.h = it.boxH || it.node.offsetHeight;
    it.keyedY = (e.keys || []).some(k => k.y !== undefined && k.y !== null) || !!e.path;
    const ref = e.below ? byId.get(e.below) : null;
    if (ref) it.belowTop = ref.top0 + ref.h + e.gap * S.H;
    it.top0 = it.belowTop != null ? it.belowTop : e.y * S.H - e.anchor[1] * it.h;
    byId.set(e.id, it);
    if (it.node.classList.contains('masked')) it.inner = it.node.firstChild;
    it.partsLaid = motionParts(it);
    const enter = e.enter;
    if (enter && it.partsLaid.length > 1) {
      const stagger = enter.preset === 'typewriter' ? enter.duration / it.partsLaid.length : enter.stagger;
      it.delays = partDelays(it.partsLaid.length, enter.order, stagger, (e.id || '').length + 3.7);
    }
  }
  const overlays = buildOverlays(layer, sc.overlays);
  placeOver(overlays, sc);
  const D = MO().block.distance;
  const draw = (t) => {
    update(t);
    if (sc.camera) {
      const c = cameraAt(sc.camera, t);
      world.style.transform = `translate(${(0.5 - c.x) * S.W * c.zoom}px, ${(0.5 - c.y) * S.H * c.zoom}px) scale(${c.zoom})`
        + ` rotateX(${c.rx}deg) rotateY(${c.ry}deg) rotate(${c.r}deg)`;
    }
    for (const it of items) drawElement(it, t, !!sc.camera, D);
    updateOverlays(overlays, t, null, null);
  };
  let end = 0;
  for (const it of items) {
    const enter = it.e.enter;
    if (!enter) { end = Math.max(end, it.e.start); continue; }
    const spread = it.delays ? Math.max(...it.delays) : 0;
    end = Math.max(end, enter.at + (enter.preset === 'typewriter' ? 0 : enter.duration) + spread);
  }
  draw.info = { fit: 1, reveal_end: Math.max(end, 0) };
  return draw;
}

function build(sc, index) {
  const stage = document.getElementById('stage');
  const layer = el('div', 'layer', stage);
  // In the scenes' order whatever order they are built in (a preview seeks back): a scene comes in over the one before.
  let next = null;
  for (const [i, b] of S.built) if (i > index && (!next || i < next[0])) next = [i, b];
  if (next) stage.insertBefore(layer, next[1].layer);
  // Laid out while it is built: text fitting and overlay placement measure it.
  layer.style.display = 'block';
  const from = S.sources.length;
  let update;
  if (sc.type === 'recording') update = buildRecording(layer, sc);
  else if (sc.type === 'clip') update = buildStill(layer, sc, true);
  else if (sc.type === 'slide' || sc.type === 'image') update = buildStill(layer, sc, false);
  else if (sc.type === 'statement') update = buildStatement(layer, sc);
  else if (sc.type === 'motion') update = buildMotion(layer, sc);
  else update = buildCard(layer, sc);
  const sources = S.sources.slice(from);
  return { layer, update, info: update.info || { fit: 1, reveal_end: 0 }, sources };
}

// The scenes starting within AHEAD_S of the last one on screen, built hidden: their media load and their videos
// wait on their first frame, so a scene never comes on screen before it can draw.
function prepareAhead(T, active) {
  const last = active.length ? active[active.length - 1] : -1;
  for (let i = last + 1; i < S.scenes.length && S.scenes[i].start - T <= AHEAD_S; i++) {
    let b = S.built.get(i);
    if (!b) {
      b = build(S.scenes[i], i);
      b.layer.style.display = 'none';
      S.built.set(i, b);
    }
    for (const source of b.sources) source.prime();
  }
}

// How a scene comes in over the one before. Each move is scaled to the
// frame, eased, and softened where a hard edge would read as a slideshow: the
// dissolve settles the incoming picture, the push goes through the frame with
// a touch of blur, the wipe has a feathered edge, the slide covers the scene
// it leaves (which drifts back, dimmed), the whip is a fast blurred pan and the
// flash an exposure burst.
function applyTransition(prev, next, sc, t) {
  const tr = sc.transition_in || { type: 'cut', duration: 0 };
  const p = tr.duration > 0 ? clamp((t - sc.start) / tr.duration, 0, 1) : 1;
  const e = easeOf('move')(p);
  const reset = (l) => {
    l.style.opacity = '1'; l.style.transform = 'none'; l.style.filter = 'none';
    l.style.clipPath = 'none'; l.style.maskImage = 'none'; l.style.webkitMaskImage = 'none';
  };
  reset(next.layer); if (prev) reset(prev.layer);
  next.wipe = null;
  if (!prev || p >= 1) return;
  const P = prev.layer.style, N = next.layer.style;
  const peak = Math.sin(Math.PI * p);
  switch (tr.type) {
    case 'fade_black':
      P.opacity = String(1 - out3(clamp(p * 2, 0, 1)));
      N.opacity = String(out3(clamp(p * 2 - 1, 0, 1)));
      break;
    case 'slide_left':
    case 'slide_up': {
      const axis = tr.type === 'slide_left' ? 'X' : 'Y', span = tr.type === 'slide_left' ? S.W : S.H;
      P.transform = `translate${axis}(${-e * span * 0.3}px) scale(${1 - 0.04 * e})`;
      P.filter = `brightness(${1 - 0.45 * e})`;
      N.transform = `translate${axis}(${(1 - e) * span}px)`;
      break;
    }
    case 'zoom':
      P.transform = `scale(${1 + 0.35 * e})`; P.filter = `blur(${e * 10}px)`; P.opacity = String(1 - out3(p));
      N.transform = `scale(${1.18 - 0.18 * e})`; N.filter = `blur(${(1 - e) * 10}px)`; N.opacity = String(out3(p));
      break;
    case 'blur':
      P.filter = `blur(${e * 14}px)`; P.opacity = String(1 - e);
      N.filter = `blur(${(1 - e) * 14}px)`; N.opacity = String(e); N.transform = `scale(${1.04 - 0.04 * e})`;
      break;
    case 'wipe': {
      const edge = e * 116 - 8;
      const mask = `linear-gradient(90deg, #000 ${edge - 8}%, transparent ${edge + 8}%)`;
      N.maskImage = mask; N.webkitMaskImage = mask;
      // Where it covers the scene it leaves, for the brand marks the audit counts.
      next.wipe = edge;
      break;
    }
    case 'whip':
      P.transform = `translateX(${-e * S.W * 0.55}px)`; P.filter = `blur(${peak * 26}px)`;
      N.transform = `translateX(${(1 - e) * S.W * 0.55}px)`; N.filter = `blur(${peak * 26}px)`;
      N.opacity = String(clamp((p - 0.3) / 0.25, 0, 1));
      break;
    case 'flash': {
      // An exposure burst: the white of #flash peaks as the scenes swap, so it hides the change.
      S.flash = Math.max(S.flash || 0, 0.92 * peak * peak);
      P.filter = `brightness(${1 + 1.5 * out3(clamp(p * 2, 0, 1))})`;
      N.filter = `brightness(${1 + 1.5 * (1 - out3(clamp(p * 2 - 1, 0, 1)))})`;
      const swap = clamp((p - 0.44) / 0.12, 0, 1);
      P.opacity = String(1 - swap); N.opacity = String(swap);
      break;
    }
    default:
      N.opacity = String(e); N.transform = `scale(${1.03 - 0.03 * e})`;
  }
}

// The watermark leaves the stage to the cards that carry the brand - title
// and closing cards, statements showing the logo - and comes back with the
// content, after its first beat and once the card has left (its transition
// over): an opening is a card, not a corner logo, and the brand is
// never on the frame twice.
const brandsItself = (sc) => sc.type === 'title' || sc.type === 'outro' || (sc.type === 'statement' && !!sc.show_logo)
  || (sc.type === 'motion' && !!sc.brands);
// A scene whose spec turns the watermark off (`watermark: false`) leaves the corner as a card does: its
// footage may show the brand's logo itself, which the corner mark would duplicate (`duplicate_logo`).
const noWatermark = (sc) => brandsItself(sc) || sc.watermark === false;

function watermarkAlpha(T) {
  let i = 0;
  S.scenes.forEach((sc, k) => { if (sc.start <= T) i = k; });
  const sc = S.scenes[i];
  if (!sc) return 1;
  const before = i > 0 && !noWatermark(S.scenes[i - 1]);
  const t = T - sc.start;
  const left = Math.max(0.6, (sc.transition_in || {}).duration || 0);
  if (!noWatermark(sc)) return before ? 1 : out3(prog(t, left, 0.8));
  return before ? 1 - prog(t, 0, 0.35) : 0;
}

function updateCaption(T) {
  const cap = document.getElementById('caption');
  const c = S.doc.captions;
  if (!c || !c.enabled || !c.cues.length) { cap.style.display = 'none'; return; }
  const index = c.cues.findIndex(q => T >= q[0] && T < q[1]);
  if (index < 0) { cap.style.display = 'none'; return; }
  const cue = c.cues[index];
  cap.style.display = 'block';
  if (cap.dataset.text !== cue[2]) {
    cap.dataset.text = cue[2]; cap.firstChild.textContent = cue[2];
    cap.firstChild.style.fontSize = S.capInfo[index] ? S.capInfo[index].font + 'px' : '';
  }
  const a = Math.min(prog(T, cue[0], 0.12), 1 - prog(T, cue[1] - 0.1, 0.1));
  cap.style.opacity = String(Math.max(a, 0));
}

// The subtitles' place, kept clear of a watermark at their edge, and each
// cue's size (shrunk down to 26 px until it holds on two lines) and height:
// what every scene they play over keeps clear (`reserveOf`).
function layCaptions(cap, wm) {
  const c = S.doc.captions;
  S.capInfo = [];
  S.capEdge = c && c.position === 'top' ? 'top' : 'bottom';
  S.capOffset = S.H * (S.capEdge === 'top' ? 0.06 : 0.065);
  cap.style.maxWidth = '';
  if (!c || !(c.cues || []).length) return;
  const bar = cap.classList.contains('cap-bar');
  const sameEdge = c.enabled && S.wm && (S.capEdge === 'top') === (S.wm.y < S.H / 2);
  if (sameEdge && !bar) {
    const free = S.wm.x > S.W / 2 ? S.wm.x - S.W / 2 : S.W / 2 - (S.wm.x + S.wm.w);
    cap.style.maxWidth = Math.min(S.W * 0.82, 2 * (free - S.W * 0.02)) + 'px';
  }
  const span = cap.firstChild;
  cap.style.display = 'block'; cap.style.opacity = '0';
  const lines = () => new Set([...span.getClientRects()].map(r => Math.round(r.top))).size;
  for (const q of c.cues) {
    span.textContent = q[2];
    let size = 34;
    span.style.fontSize = size + 'px';
    while (lines() > 2 && size > 26) { size -= 2; span.style.fontSize = size + 'px'; }
    let y0 = Infinity, y1 = -Infinity;
    for (const r of [cap.getBoundingClientRect(), ...span.getClientRects()]) { y0 = Math.min(y0, r.top); y1 = Math.max(y1, r.bottom); }
    S.capInfo.push({ font: size, lines: lines(), h: y1 - y0 });
  }
  span.textContent = ''; span.style.fontSize = ''; cap.dataset.text = ''; cap.style.display = 'none';
  // A full-width bar would run over a watermark at its edge: the mark stands above it.
  if (sameEdge && bar) {
    const lift = S.capOffset + Math.max(...S.capInfo.map(i => i.h)) + S.H * 0.02;
    if (S.capEdge === 'top') wm.style.top = lift + 'px'; else wm.style.bottom = lift + 'px';
    S.wm = rectOf(wm);
  }
}

// ── The check: what the audience would see go wrong, measured ────────────
// `audit()` plays every scene at the moments that matter - its text all in,
// each overlay shown, each subtitle shown - and reports what covers what,
// what runs past the frame and what is still coming in when the scene ends.
// ``quality.py`` turns it into findings the agent fixes before rendering.

const BLOCKS = [['logo', 'logo'], ['kicker', 'kicker'], ['title', 'title'], ['subtitle', 'subtitle'], ['figure', 'figure'],
  ['sline', 'line'], ['ssub', 'smaller line'], ['chip', 'chip'], ['cta', 'call to action'], ['ctaurl', 'URL'],
  ['lower', 'lower third'], ['badge', 'badge'], ['bigcap', 'caption overlay'], ['callout', 'callout'],
  ['ovimg', 'image overlay'], ['mtext', 'motion text'], ['mimg', 'motion picture']];
const BOXED = new Set(['chip', 'cta', 'lower', 'badge', 'callout', 'ovimg', 'mimg']);
const OVERLAY_SELECTOR = '.lower, .badge, .bigcap, .callout, .ovimg';

function shownAlpha(node) {
  let a = 1;
  for (let n = node; n && n !== document.body; n = n.parentElement) {
    const cs = getComputedStyle(n);
    if (cs.display === 'none' || cs.visibility === 'hidden') return 0;
    a *= parseFloat(cs.opacity);
  }
  return a;
}

function inkOf(node, boxed) {
  if (boxed || node.tagName === 'IMG') { const r = node.getBoundingClientRect(); return r.width > 1 && r.height > 1 ? [r] : []; }
  return glyphRects(node);
}

const excerpt = (text) => { const s = String(text || '').replace(/\s+/g, ' ').trim(); return s.length > 60 ? s.slice(0, 57) + '...' : s; };

// What is drawn now: the scene's blocks (they flow together, so they never
// cover each other), each overlay, the subtitles and the watermark. Subtitles
// left to the player (not burned in) are the band it draws them in.
function onScreen(layer, sc, T) {
  const items = [];
  const overlays = [...layer.querySelectorAll(OVERLAY_SELECTOR)];
  for (const [cls, name] of BLOCKS) {
    for (const node of layer.querySelectorAll('.' + cls)) {
      const owner = node.closest(OVERLAY_SELECTOR);
      if (owner && owner !== node) continue;
      // A title's or a line's words come in one by one: seen once one is, in once all are.
      // A picture drawn faint on purpose (its opacity) still covers what is under it.
      const base = shownAlpha(node) / (node.dataset.opacity ? Math.max(parseFloat(node.dataset.opacity), 0.05) : 1);
      const words = [...node.querySelectorAll('.w')].map(w => parseFloat(getComputedStyle(w).opacity));
      // A motion scene's elements are placed one by one: each is a group of its own, so two that meet are said.
      items.push({ name, node, alpha: base * (words.length ? Math.max(...words) : 1), least: base * (words.length ? Math.min(...words) : 1),
        rects: inkOf(node, BOXED.has(cls)), text: excerpt(node.textContent || node.alt),
        group: owner ? 'overlay' + overlays.indexOf(owner) : node.dataset.group || 'scene', leaving: !!node.dataset.leaving });
    }
  }
  const cap = document.getElementById('caption');
  if (cap.style.display === 'block') {
    // A bar runs edge to edge on purpose: it covers what is under it, and touches the frame.
    const bar = cap.classList.contains('cap-bar');
    const rects = bar ? [cap.getBoundingClientRect()] : [...cap.firstChild.getClientRects()];
    const alpha = shownAlpha(cap);
    items.push({ name: 'subtitles', node: cap, alpha, least: alpha, rects, text: excerpt(cap.dataset.text), group: 'subtitles', edge: bar });
  } else if (S.doc.captions && !S.doc.captions.enabled) {
    const index = (S.doc.captions.cues || []).findIndex(q => T >= q[0] && T < q[1]);
    if (index >= 0 && S.capInfo[index]) {
      const h = S.capOffset + S.capInfo[index].h, y = S.capEdge === 'top' ? 0 : S.H - h;
      items.push({ name: 'subtitles', node: cap, alpha: 1, least: 1, rects: [{ left: 0, right: S.W, top: y, bottom: y + h, width: S.W, height: h }],
        text: excerpt(S.doc.captions.cues[index][2]), group: 'subtitles', edge: true });
    }
  }
  const wm = document.getElementById('watermark');
  if (wm.style.display === 'block' && wm.naturalWidth) {
    const alpha = shownAlpha(wm);
    items.push({ name: 'watermark', node: wm, alpha, least: alpha, rects: [wm.getBoundingClientRect()], text: '', group: 'watermark', edge: true });
  }
  return items.filter(it => it.rects.length);
}

// The share of *rect* - a mark's box, its centre when it has no size (a picture not loaded) - that the layer of a
// scene (*built*) spans: where its box (moved by its transition) and a wipe's edge put it.
function spanned(built, rect) {
  const box = built.layer.getBoundingClientRect();
  const right = Math.min(box.right, built.wipe != null ? S.W * built.wipe / 100 : Infinity);
  if (rect.width < 1 || rect.height < 1) {
    const x = rect.left + rect.width / 2, y = rect.top + rect.height / 2;
    return x >= box.left && x <= right && y >= box.top && y <= box.bottom ? 1 : 0;
  }
  const w = Math.max(0, Math.min(rect.right, right) - Math.max(rect.left, box.left));
  const h = Math.max(0, Math.min(rect.bottom, box.bottom) - Math.max(rect.top, box.top));
  return (w * h) / (rect.width * rect.height);
}

// The share of *rect* that a scene coming in (*over*, built) covers: what its layer spans, at its opacity.
function coverage(over, rect) {
  return spanned(over, rect) * parseFloat(getComputedStyle(over.layer).opacity);
}

// The brand marks drawn on the frame: the watermark, a card's logo, an image overlay of a brand mark.
// Drawn at half strength or more - a picture drawn faint on purpose counted at full strength, as it covers -
// less what the scenes coming in over its layer cover of it.
function brandMarks() {
  const marks = [];
  const active = S.active || [];
  active.forEach((i, k) => {
    const built = S.built.get(i);
    if (!built || built.layer.style.display !== 'block') return;
    for (const node of built.layer.querySelectorAll('[data-brand]')) {
      const rect = node.getBoundingClientRect();
      let left = 1;
      for (const j of active.slice(k + 1)) {
        const over = S.built.get(j);
        if (over) left *= 1 - coverage(over, rect);
      }
      const faint = node.dataset.opacity ? Math.max(parseFloat(node.dataset.opacity), 0.05) : 1;
      if ((shownAlpha(node) / faint) * left >= 0.5) marks.push({ mark: node.dataset.brand, scene: S.scenes[i].id });
    }
  });
  const wm = document.getElementById('watermark');
  if (wm.style.display === 'block' && shownAlpha(wm) >= 0.5) marks.push({ mark: 'watermark', scene: null });
  return marks;
}

function measure(items, findings) {
  const seen = items.filter(it => it.alpha >= 0.5);
  for (let i = 0; i < seen.length; i++) {
    for (let j = i + 1; j < seen.length; j++) {
      const a = seen[i], b = seen[j];
      if (a.group === b.group) continue;
      let area = 0;
      for (const p of a.rects) for (const q of b.rects) {
        const w = Math.min(p.right, q.right) - Math.max(p.left, q.left), h = Math.min(p.bottom, q.bottom) - Math.max(p.top, q.top);
        if (w > 3 && h > 3) area += w * h;
      }
      if (area > 0) findings.push({ kind: 'overlap', block: a.name, text: a.text, other: b.name, other_text: b.text, amount: Math.round(area) });
    }
  }
  // Text cut by the frame, or touching it: closer to an edge than a safe margin.
  const safe = Math.min(S.W, S.H) * 0.015;
  for (const it of seen) {
    if (it.edge) continue;
    let worst = null;
    for (const r of it.rects) {
      for (const [side, past] of [['left', -r.left], ['right', r.right - S.W], ['top', -r.top], ['bottom', r.bottom - S.H]]) {
        if (past + safe > 2 && (!worst || past > worst.amount)) worst = { side, amount: Math.round(past) };
      }
    }
    if (worst) findings.push({ kind: 'outside', block: it.name, text: it.text, ...worst });
  }
}

function keyOf(f) { return [f.kind, f.block, f.text, f.other || '', f.other_text || ''].join('|'); }

// The editor's preview loads a document on every edit and every plan: the
// layers of the one before stay on screen, their media pooled, until the
// frame under the playhead is drawn from the new one, in one task.
async function loadDoc(doc) {
  await document.fonts.ready;
  const media = doc.preview_media || null;
  if (media) keepMedia(doc, media);
  for (const b of S.built.values()) {
    if (media) S.retired.push(b.layer);
    else b.layer.remove();
  }
  S.built = new Map(); S.sources = []; S.loads = []; S.overruns = [];
  S.media = media;
  S.doc = doc; S.W = doc.width; S.H = doc.height; S.fps = doc.fps;
  const root = document.documentElement.style;
  const th = doc.theme;
  for (const [k, v] of Object.entries({ '--bg': th.background, '--surface': th.surface, '--fg': th.foreground, '--muted': th.muted,
    '--accent': th.accent, '--accent2': th.accent_2, '--line': th.line, '--on-accent': th.on_accent, '--chrome': th.chrome,
    '--chrome-field': th.chrome_field, '--heading-font': th.heading_font, '--body-font': th.body_font,
    '--grad-from': th.gradient_from || th.accent, '--grad-to': th.gradient_to || th.accent_2 })) root.setProperty(k, v);
  const stage = document.getElementById('stage');
  stage.style.width = S.W + 'px'; stage.style.height = S.H + 'px';
  const cap = document.getElementById('caption');
  // Prefixed: the browser window's URL bar is `.bar` too.
  cap.className = 'cap-' + (doc.captions.style || 'pill');
  cap.style.top = ''; cap.style.bottom = ''; cap.dataset.text = '';
  if (doc.captions.position === 'top') cap.style.top = (S.H * 0.06) + 'px'; else cap.style.bottom = (S.H * 0.065) + 'px';
  const wm = document.getElementById('watermark');
  Object.assign(wm.style, { display: 'none', top: '', bottom: '', left: '', right: '' });
  const wmKey = th.mark ? 'mark' : 'logo';
  // Standing with no picture where none is served - the quality check, which loads no media - so the brand marks
  // it counts include the watermark.
  if (doc.watermark && doc.watermark !== 'none' && (th.mark || th.logo)) {
    wm.style.display = 'block'; setAsset(wm, wmKey);
    const m = Math.round(S.H * 0.04);
    if (doc.watermark.includes('top')) wm.style.top = m + 'px'; else wm.style.bottom = m + 'px';
    if (doc.watermark.includes('left')) wm.style.left = m + 'px'; else wm.style.right = m + 'px';
  }
  S.scenes = doc.scenes;
  await Promise.all(S.loads); S.loads = [];
  S.wm = wm.style.display === 'block' && wm.naturalWidth ? { ...rectOf(wm), t0: -Infinity, t1: Infinity } : null;
  layCaptions(cap, wm);
  // The frame on screen, drawn from the new document before the browser paints again (the subtitles were just
  // hidden to be measured); its media's waits are left to the frames that follow.
  if (S.retired.length) void renderFrame(clamp(Math.round(S.T * S.fps), 0, Math.max((doc.frames || 1) - 1, 0)));
  return true;
}

async function renderFrame(index) {
  const T = index / S.fps;
  S.T = T;
  // A sub-frame of motion blur is drawn at its own time, its screen inserts audited as its frame's.
  S.frame = Math.round(index);
  const doc = S.doc;
  const active = [];
  S.scenes.forEach((sc, i) => { if (T >= sc.start && (T < sc.end || i === S.scenes.length - 1)) active.push(i); });
  S.active = active;
  for (const [i, b] of S.built) {
    if (active.includes(i)) continue;
    // A render draws forward: a scene that has ended is dropped with its last frame,
    // so the page holds what is on screen, never the whole video (built again if asked).
    if (!S.media && S.scenes[i].end <= T) { b.layer.remove(); S.built.delete(i); }
    else b.layer.style.display = 'none';
  }
  for (const s of S.sources) s.active = false;
  let prev = null;
  S.flash = 0;
  for (const i of active) {
    const sc = S.scenes[i];
    if (!S.built.has(i)) S.built.set(i, build(sc, i));
    const b = S.built.get(i);
    b.layer.style.display = 'block';
    b.update(Math.max(0, T - sc.start));
    applyTransition(prev, b, sc, T);
    prev = b;
  }
  document.getElementById('flash').style.opacity = String(S.flash);
  updateCaption(T);
  const wm = document.getElementById('watermark');
  if (wm.style.display === 'block') wm.style.opacity = String(0.88 * watermarkAlpha(T));
  const fade = document.getElementById('fade');
  const fin = doc.fade_in > 0 ? 1 - prog(T, 0, doc.fade_in) : 0;
  const fout = doc.fade_out > 0 ? prog(T, doc.duration - doc.fade_out, doc.fade_out) : 0;
  fade.style.opacity = String(Math.max(fin, fout));
  if (S.media) {
    if (S.ahead) prepareAhead(T, active);
    // The new document's frame is drawn: the layers of the one before go, with the media it alone played.
    for (const layer of S.retired) layer.remove();
    S.retired = [];
    for (const entry of S.drop) if (!entry.standing) release(entry);
    S.drop = [];
  }
  // A scene off screen is silent: its video stops where it is.
  for (const s of S.sources) if (!s.active) for (const video of videosOf(s)) if (!video.paused) video.pause();
  const pending = S.loads; S.loads = [];
  // Playing, the preview never waits on a load: the clock runs, and what loads shows as soon as it can.
  if (S.media && S.playing) return true;
  await Promise.all(pending);
  return true;
}

window.__xtm = {
  load(doc) {
    const run = (S.job || Promise.resolve()).then(() => loadDoc(doc));
    S.job = run.catch(() => {});
    return run;
  },
  // A frame asked while a document loads is drawn from it, once it is.
  async render(index) {
    if (S.job) await S.job;
    return renderFrame(index);
  },
  // The preview's transport: playing lets the videos run, paused seeks them frame-exact.
  setPlaying(playing) {
    S.playing = !!playing;
    if (!S.playing) for (const s of S.sources) for (const video of videosOf(s)) video.pause();
  },
  setMuted(muted) {
    S.muted = !!muted;
    for (const s of S.sources) s.video.muted = S.muted || !s.audio;
  },
  // Called when a media URL of the preview stops answering (an expired token, a replaced file): plan again.
  onSourceLost(listener) {
    S.onLost = typeof listener === 'function' ? listener : null;
  },
  // Where the frame on screen places each screen insert it shows: `{src: [x, y, w, h, shown, over?]}` in CSS
  // pixels of the stage, for the driver to cut the insert region out of the frame it captured, at the render's
  // resolution - how much of it the frame shows: its opacity, less what the scenes coming in over it cover of
  // it - and `over`, the `[x, y, w, h]` boxes of what its own scene draws over it (`onScreen`: its text, its
  // chips, its overlays, the subtitles burned in, the watermark), which the presence gate leaves out of its quad.
  insertRects() {
    const stage = document.getElementById('stage').getBoundingClientRect();
    const cap = document.getElementById('caption');
    const rects = {};
    const active = S.active || [];
    active.forEach((i, k) => {
      const built = S.built.get(i);
      if (!built || built.layer.style.display !== 'block') return;
      let ink = null;
      for (const node of built.layer.querySelectorAll('img.ins[data-insert]')) {
        if (node.style.opacity === '0') continue;
        const r = node.getBoundingClientRect();
        if (r.width < 1 || r.height < 1) continue;
        // A wipe's mask is no opacity: where it has not reached the insert yet, the scene left shows there.
        let shown = shownAlpha(node) * (built.wipe != null ? spanned(built, r) : 1);
        for (const j of active.slice(k + 1)) {
          const over = S.built.get(j);
          if (over && over.layer.style.display === 'block') shown *= 1 - coverage(over, r);
        }
        // Subtitles the player draws (not burned in) are no part of the frame.
        ink = ink || onScreen(built.layer, S.scenes[i], S.T)
          .filter(it => it.alpha > 0 && (it.node !== cap || cap.style.display === 'block')).flatMap(it => it.rects);
        const px = (v) => Math.round(v * 10) / 10;
        const over = ink.filter(o => o.right > r.left && o.left < r.right && o.bottom > r.top && o.top < r.bottom)
          .map(o => [px(o.left - stage.left), px(o.top - stage.top), px(o.width), px(o.height)]);
        rects[node.dataset.insert] = [r.left - stage.left, r.top - stage.top, r.width, r.height, Math.round(shown * 1000) / 1000,
          ...(over.length ? [over] : [])];
      }
    });
    return rects;
  },
  // The frames drawn since the last call that painted a scene at or past the painted end of an insert that runs to its
  // end (noteOverrun): `[{src, scene, frame, t, length, painted_end}]`, handed on and forgotten.
  overruns() {
    const out = S.overruns || [];
    S.overruns = [];
    return out;
  },
  // The preview stand-in's arithmetic, read by its tests: the key's matte (keyMatte) and the tracked quad (previewQuad).
  standIn: { keyMatte, previewQuad },
  // Frame *index* with the scene *id* alone on the stage: the other scenes hidden, its transition undone,
  // nothing drawn over it - its own finished frame as this page composes it, which the render's guard reads - and
  // where it places each of its screen inserts (insertRects). The next frame drawn puts everything back.
  async alone(index, id) {
    await this.render(index);
    const k = S.scenes.findIndex(sc => sc.id === id);
    for (const i of S.active || []) {
      const built = S.built.get(i);
      if (!built) continue;
      if (i !== k) { built.layer.style.display = 'none'; continue; }
      const l = built.layer.style;
      l.opacity = '1'; l.transform = 'none'; l.filter = 'none'; l.clipPath = 'none'; l.maskImage = 'none'; l.webkitMaskImage = 'none';
      built.wipe = null;
    }
    for (const name of ['flash', 'fade', 'watermark']) document.getElementById(name).style.opacity = '0';
    document.getElementById('caption').style.display = 'none';
    return this.insertRects();
  },
  // Frame *index* with the scene *id* painted black, or white: its layer kept where the transition draws it -
  // its opacity, its transform, its mask - and everything else as the frame draws it. The black frame is what the
  // frame shows of all but that scene; the white one less the black one, how much of it the frame shows at each pixel:
  // what the presence gate unblends a transition's frame with. The next frame drawn puts everything back.
  async tone(index, id, white) {
    await this.render(index);
    const built = S.built.get(S.scenes.findIndex(sc => sc.id === id));
    if (!built || built.layer.style.display !== 'block') return false;
    const own = built.layer.style.filter;
    built.layer.style.filter = `${own && own !== 'none' ? own + ' ' : ''}brightness(0)${white ? ' invert(1)' : ''}`;
    return true;
  },
  // The brand marks frame *index* draws: what the audit counts, read frame by frame.
  async marks(index) {
    await this.render(index);
    return brandMarks();
  },
  async audit() {
    const scenes = [];
    const cues = (S.doc.captions || {}).cues || [];
    for (let i = 0; i < S.scenes.length; i++) {
      const sc = S.scenes[i], next = S.scenes[i + 1];
      const from = sc.start + ((sc.transition_in || {}).duration || 0);
      const until = next ? Math.min(sc.end, next.start) : sc.end;
      const settled = Math.max(from, until - 0.05);
      // Each overlay and subtitle just in, and once more late: the scene's text keeps coming in meanwhile.
      const moments = [settled];
      for (const o of sc.overlays || []) {
        moments.push(sc.start + Math.min(o.t0 + 0.5, (o.t0 + o.t1) / 2), sc.start + Math.max(o.t0 + 0.5, o.t1 - 0.3));
      }
      for (const q of cues) {
        const a = Math.max(q[0], from), b = Math.min(q[1], until);
        if (b - a > 0.05) moments.push(a + Math.min(0.25, (b - a) / 2), Math.max(a + 0.12, b - 0.15));
      }
      const last = Math.round(settled * S.fps);
      const times = [last, ...[...new Set(moments.map(t => Math.round(t * S.fps)))].filter(f => f !== last).sort((a, b) => a - b).slice(0, 23)];
      // The brand drawn twice: counted on every frame played here, and over the scene's opening, where the
      // watermark and a card's logo hand over to each other - through its transition in and just after it.
      const tr = (sc.transition_in || {}).duration || 0;
      const opening = [0.15, 0.35, 0.7, 1.0, 1.4, tr / 2, tr + 0.3, tr + 0.8]
        .map(dt => Math.round(Math.min(sc.start + dt, settled) * S.fps));
      const findings = [], keys = new Set(), doubled = new Map();
      for (const frame of [...times, ...new Set(opening.filter(f => !times.includes(f)))]) {
        await this.render(frame);
        const marks = brandMarks();
        if (marks.length > 1) {
          const key = marks.map(m => m.mark + '@' + (m.scene || '')).sort().join('|');
          if (!doubled.has(key) || frame < doubled.get(key).frame) doubled.set(key, { frame, marks });
        }
        if (!times.includes(frame)) continue;
        const built = S.built.get(i);
        if (!built || built.layer.style.display !== 'block') continue;
        const found = [];
        const items = onScreen(built.layer, sc, frame / S.fps);
        measure(items, found);
        if (frame === last) {
          // An element leaving on purpose (its exit) is not still coming in.
          for (const it of items) {
            if ((it.group === 'scene' || it.group.startsWith('motion:')) && !it.leaving && it.least < 0.95) {
              found.push({ kind: 'rushed', block: it.name, text: it.text });
            }
          }
        }
        for (const f of found) {
          const key = keyOf(f);
          if (keys.has(key)) continue;
          keys.add(key);
          findings.push({ ...f, at: Math.round((frame / S.fps - sc.start) * 10) / 10 });
        }
      }
      for (const { frame, marks } of doubled.values()) {
        findings.push({ kind: 'brand_twice', marks, at: Math.round((frame / S.fps - sc.start) * 10) / 10,
          video_at: Math.round(frame / S.fps * 100) / 100 });
      }
      const info = (S.built.get(i) || {}).info || { fit: 1, reveal_end: 0 };
      scenes.push({ index: i, id: sc.id, type: sc.type, start: sc.start, end: sc.end, shown_from: from, shown_until: until,
        fit: Math.round(info.fit * 1000) / 1000, reveal_end: Math.round(info.reveal_end * 100) / 100, findings });
    }
    return { width: S.W, height: S.H, scenes, captions: S.capInfo.map((c, k) => ({ ...c, index: k })) };
  },
};
})();
""".replace("__ACCENT_PATTERN__", ACCENT_PATTERN).replace("__DEFAULT_MOTION__", json.dumps(resolve_motion(None))).replace(
    "__FIGURE_STANDS__", json.dumps(page_stands())
).replace("__INSERT_FADE__", repr(float(INSERT_FADE_SECONDS))).replace("__HELD_END_SLACK__", repr(float(HELD_END_SLACK))).replace(
    "__STILL_HOST_ZOOM__", repr(float(STILL_HOST_ZOOM))
)
# screens.key_matte's constants, which the preview's key test (keyMatte) reads.
PAGE_JS = PAGE_JS.replace("__KEY_MATTE__", json.dumps(key_matte_constants()))


def page_html(font_css: str) -> str:
    """The compositor document; its script installs ``window.__xtm``."""
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<style>{font_css}</style><style>{PAGE_CSS}</style></head>"
        "<body><div id='stage'></div><div id='flash'></div>"
        "<div id='caption'><span></span></div><img id='watermark' alt='' data-brand='watermark'><div id='fade'></div>"
        f"<script>{LOTTIE_JS}</script><script>{PAGE_JS}</script></body></html>"
    )
