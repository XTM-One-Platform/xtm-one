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

"""``render_html_to_image`` - a browser-rendered figure in one call.

An agent that authors a slide figure as HTML used to drive the browser tool to
render it: navigate to a data URL, ``evaluate`` an overflow probe, ``evaluate``
a second one, zoom, screenshot - five or six calls, and the HTML lived only in
the model's context, so a one-word edit meant rebuilding the figure from
memory. This tool renders the given HTML in the same locked-down headless
Chromium the PDF renderer uses (JavaScript off, every network request aborted
- inline ``data:`` resources and the conversation's own files stay), on a
canvas of the given size and device scale, WITH the deck fonts inlined as
``@font-face`` from the frontend bundle when the platform ships them, runs
the overflow / overlap probe over the real DOM and answers with:

* the PNG as a conversation file (attached as a vision block, once);
* the HTML SOURCE as a sibling ``<name>.html`` file, so the next change is an
  ``edit_file_content`` find / replace on the source and ONE re-render
  (``render_html_to_image(source_file_id=...)``), never a rebuild from memory;
* the probe findings - ``OUT`` (an element past the canvas), ``OVF`` (content
  clipped inside its box, a nowrap label running past it), ``OVERLAP`` (two
  text / image leaves painted over each other), ``WRAP`` (a table cell on more
  lines than its column holds) - each naming the element by tag, id and
  class, bounded to :data:`MAX_PROBE_FINDINGS`; a figure taller than the
  canvas cap is drawn to the cap and probed over its whole height;
* the visual summary line.

``file:<uuid>`` / ``[[FILE:<id>]]`` image handles inside the HTML are inlined
as ``data:`` URIs through the same helper ``generate_file`` uses for a PDF, so
a logo fetched with ``fetch_image_asset`` can be placed by id.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import functools
import re
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from app.core.utils import format_exception_message
from app.tools.base import (
    WORKSPACE_FILE_SOURCE,
    CallableToolExecutor,
    ToolDefinition,
    ToolResult,
    current_workspace,
)

from ._shared import EMBED_FILE_REF_RE, format_file_size, logger, persist_tool_artifact
from ._shared import resolve_file_ref as _resolve_file_ref

if TYPE_CHECKING:
    from playwright.async_api import Page
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.tools.base import FileContext

#: Canvas bounds. A 16:9 slide is 1920x1080; a 4x-density figure is well under
#: the ceiling, and a canvas above it is a poster, not a slide figure.
MIN_CANVAS_PX = 64
MAX_CANVAS_PX = 4096
DEFAULT_WIDTH = 1920
DEFAULT_HEIGHT = 1080
MAX_SCALE = 3
#: Ceiling on the DEVICE pixels one render may paint (``width * height *
#: scale^2``): the default canvas at the highest density. The per-dimension
#: bounds alone still admit a 4096 x 4096 canvas at 3x - 151 million device
#: pixels, PNG-encoded and persisted - which is a worker's memory, not a
#: slide figure. Checked before Chromium is launched.
MAX_DEVICE_PIXELS = DEFAULT_WIDTH * DEFAULT_HEIGHT * MAX_SCALE * MAX_SCALE
#: Ceiling on the HTML source (characters). A slide figure is a few KB; past
#: this the model is authoring a page, which is what generate_file is for.
MAX_HTML_CHARS = 200_000
#: Ceiling on the document handed to the browser AFTER the conversation's
#: image handles were inlined as ``data:`` URIs (characters). The source cap
#: above is checked on what the model wrote; a ``file:<uuid>`` handle then
#: expands to the stored bytes in base64 (a 5 MB fetch_image_asset logo is
#: ~6.7 MB of text), so the inlined document needs a bound of its own or one
#: source line could hand Chromium an unbounded page.
MAX_INLINED_HTML_CHARS = 16_000_000
#: Ceiling on ONE conversation image inlined by handle (bytes), checked off
#: the ``FileRef`` size BEFORE the bytes are downloaded and base64-encoded -
#: the document cap above is checked after, on the whole. A fetch_image_asset
#: logo is 5 MB at most; a slide figure that needs more is a poster.
MAX_INLINE_FILE_BYTES = 8 * 1024 * 1024
#: How long one render may take, all in (launch, layout, screenshot).
RENDER_TIMEOUT_SECONDS = 45
#: Probe findings reported, worst first.
MAX_PROBE_FINDINGS = 20
#: Workspace pages one ``pages`` call renders: a design round's mock set, not a site.
MAX_BATCH_PAGES = 30
#: Viewport captures tried when Chromium has no compositor frame yet.
SCREENSHOT_ATTEMPTS = 3
#: The one transient error a capture is retried on: ``Page.captureScreenshot``
#: answered before the compositor produced a frame (seen on CI runners).
_NO_FRAME_ERROR = "Unable to capture screenshot"
#: Two animation frames: the next paint has been committed when it resolves.
_TWO_FRAMES_JS = "() => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(() => r(null))))"

#: The deck fonts the frontend bundles (``@fontsource``); inlined from the
#: built frontend when it is next to the backend (the unified image), else from
#: the font directory the worker image ships (``Dockerfile.worker`` copies the
#: same ``@fontsource`` latin subsets, pinned by ``frontend/yarn.lock``, to
#: ``/app/fonts``); skipped in dev, where neither exists.
_FONT_FAMILIES: tuple[tuple[str, str], ...] = (
    ("Geologica", "geologica"),
    ("Elms Sans", "elms-sans"),
    ("IBM Plex Sans", "ibm-plex-sans"),
    ("IBM Plex Mono", "ibm-plex-mono"),
)
_FONT_WEIGHTS: tuple[int, ...] = (400, 500, 600, 700)
_FRONTEND_DIST = Path(__file__).resolve().parents[3] / "frontend_dist"
_WORKER_FONTS = Path(__file__).resolve().parents[3] / "fonts"
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")

#: The overflow / overlap probe, run over the rendered DOM. Returns a JSON
#: object ``{"out": [...], "ovf": [...], "overlap": [...], "svg": {...}}`` where
#: every entry names the element (``tag#id.class``) and the measurement that
#: failed. Pure geometry: what the audience would see cut off or painted twice.
#: The ``svg`` pass measures an inline ``<svg>`` figure with the fonts as
#: rendered: a label over a shape it is not inside (``overlap``), a label
#: running past the border of the shape it starts in (``ovf``), a label
#: crossed by an edge (``edge``, the edge sampled along its length; the label
#: an SVG ``<text>`` or an HTML text box drawn over the figure, a badge) and the
#: count of edge-edge crossings, a statistic of the figure, not a finding.
#: Shapes are tested with ``isPointInFill`` on a grid of points of the label's
#: box, so a circle or a diamond is its real outline, not its bounding box.
PROBE_JS = r"""
(bounds) => {
  // ``bounds`` is the area measured: the canvas by default, the whole
  // document when a figure is taller than the canvas cap (render_html).
  const W = (bounds && bounds.width) || window.innerWidth, H = (bounds && bounds.height) || window.innerHeight;
  const name = (el) => {
    let s = el.tagName.toLowerCase();
    if (el.id) s += '#' + el.id;
    const cls = (el.getAttribute('class') || '').trim().split(/\s+/).filter(Boolean).slice(0, 3);
    if (cls.length) s += '.' + cls.join('.');
    const text = (el.textContent || '').trim().replace(/\s+/g, ' ');
    if (text) s += ' "' + text.slice(0, 40) + (text.length > 40 ? '...' : '') + '"';
    return s;
  };
  const visible = (el) => {
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden' || parseFloat(cs.opacity) === 0) return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };
  // Bounded like the overlap list: the tool reports the first findings,
  // never a list the size of the DOM (a 200 KB document has thousands of
  // elements; the report is cut to MAX_PROBE_FINDINGS lines anyway).
  const CAP = 200;
  const all = Array.from(document.body.querySelectorAll('*')).filter(visible);
  const out = [], ovf = [], overlap = [];
  for (const el of all) {
    if (out.length >= CAP && ovf.length >= CAP) break;
    const r = el.getBoundingClientRect();
    if (out.length < CAP && (r.right > W + 0.5 || r.bottom > H + 0.5 || r.left < -0.5 || r.top < -0.5)) {
      out.push({ el: name(el), right: Math.round(r.right), bottom: Math.round(r.bottom), left: Math.round(r.left), top: Math.round(r.top), canvas: [W, H] });
    }
    const cs = getComputedStyle(el);
    const clips = cs.overflowX !== 'visible' || cs.overflowY !== 'visible' || cs.whiteSpace === 'nowrap' || cs.textOverflow === 'ellipsis';
    if (ovf.length < CAP && clips && (el.scrollWidth > el.clientWidth + 1 || el.scrollHeight > el.clientHeight + 1)) {
      ovf.push({ el: name(el), scroll: [el.scrollWidth, el.scrollHeight], box: [el.clientWidth, el.clientHeight], nowrap: cs.whiteSpace === 'nowrap' });
    }
  }
  const leaves = all.filter((el) => {
    if (el.tagName === 'IMG' || el.tagName === 'SVG' || el.tagName === 'CANVAS') return true;
    return Array.from(el.childNodes).some((n) => n.nodeType === 3 && n.textContent.trim());
  }).slice(0, 400);
  const rects = leaves.map((el) => el.getBoundingClientRect());
  for (let i = 0; i < leaves.length; i++) {
    for (let j = i + 1; j < leaves.length; j++) {
      if (leaves[i].contains(leaves[j]) || leaves[j].contains(leaves[i])) continue;
      const a = rects[i], b = rects[j];
      const w = Math.min(a.right, b.right) - Math.max(a.left, b.left);
      const h = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top);
      if (w > 2 && h > 2) overlap.push({ a: name(leaves[i]), b: name(leaves[j]), area: Math.round(w * h) });
      if (overlap.length >= 50) break;
    }
    if (overlap.length >= 50) break;
  }
  return { out, ovf, overlap, wrap: probeWrap(name, CAP), leaves: leaves.length, svg: probeSvg(name) };
}
"""

#: The WRAP pass of :data:`PROBE_JS`: a table cell whose text runs to more
#: lines than most cells of its column hold. Nothing is clipped (the row
#: grows), so the overflow pass never sees it, yet the row is taller than its
#: neighbours and the table loses its rhythm. Lines are the distinct tops of
#: the cell text's line boxes; a column of fewer than 3 cells, and a cell
#: whose whole column wraps alike (a description column), are not findings.
#: A cell spanning several columns (colspan or aria-colspan > 1: the expanded
#: row of a data table) and a cell holding more than one block-level box (a
#: paragraph and cards, not a single text run) are not compared with the
#: column: their height is their content, and the overflow pass still sees
#: them clipped. A cell's column is its visual one, the spans before it counted.
_PROBE_WRAP_JS = r"""
const probeWrap = (name, CAP) => {
  const found = [];
  const cells = document.body.querySelectorAll('td, th, [role="cell"], [role="gridcell"]');
  if (!cells.length) return found;
  const lines = (el) => {
    const range = document.createRange();
    range.selectNodeContents(el);
    const tops = [];
    for (const r of range.getClientRects()) {
      if (r.width < 1 || r.height < 1) continue;
      if (!tops.some((t) => Math.abs(t - r.top) < r.height / 2)) tops.push(r.top);
    }
    return tops.length;
  };
  const span = (el) => {
    const n = typeof el.colSpan === 'number' && el.colSpan > 0 ? el.colSpan : parseInt(el.getAttribute('aria-colspan') || '1', 10);
    return Number.isFinite(n) && n > 0 ? n : 1;
  };
  const INLINE = new Set(['inline', 'inline-block', 'inline-flex', 'inline-grid', 'contents', 'none']);
  const blocky = (el) => {
    let boxes = 0;
    for (const child of Array.from(el.querySelectorAll('*')).slice(0, 200)) {
      if (!INLINE.has(getComputedStyle(child).display) && ++boxes > 1) return true;
    }
    return false;
  };
  const columns = new Map();
  for (const el of Array.from(cells).slice(0, 5000)) {
    if (!(el.textContent || '').trim()) continue;
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden' || cs.whiteSpace === 'nowrap') continue;
    if (span(el) > 1 || blocky(el)) continue;
    const row = el.parentElement;
    const table = el.closest('table, [role="table"], [role="grid"]') || row;
    let index = 0;
    for (let prev = el.previousElementSibling; prev; prev = prev.previousElementSibling) index += span(prev);
    if (!columns.has(table)) columns.set(table, new Map());
    const byIndex = columns.get(table);
    if (!byIndex.has(index)) byIndex.set(index, []);
    byIndex.get(index).push({ el, n: lines(el) });
  }
  for (const byIndex of columns.values()) {
    for (const col of byIndex.values()) {
      if (col.length < 3) continue;
      const counts = new Map();
      for (const c of col) counts.set(c.n, (counts.get(c.n) || 0) + 1);
      let mode = 1, most = 0;
      for (const [n, k] of counts) if (k > most || (k === most && n < mode)) { mode = n; most = k; }
      for (const c of col) {
        if (c.n > Math.max(1, mode) && found.length < CAP) {
          found.push({ el: name(c.el), lines: c.n, column: mode, top: Math.round(c.el.getBoundingClientRect().top) });
        }
      }
    }
  }
  return found;
};
"""

#: The SVG pass of :data:`PROBE_JS` (a separate function so the HTML pass
#: above stays as it was). Bounded in elements and in time: a figure with
#: thousands of edges is reported as ``truncated``, never a stalled render.
_PROBE_SVG_JS = r"""
const probeSvg = (name) => {
  const res = { overlap: [], ovf: [], edge: [], ends: [], short: [], lane: [], border: [], crossings: 0, svgs: 0, labels: 0, shapes: 0, edges: 0, truncated: false };
  const deadline = performance.now() + 4000;
  const late = () => { if (performance.now() > deadline) { res.truncated = true; return true; } return false; };
  const CAP = 200;
  const HIDDEN = 'defs, clipPath, mask, marker, pattern, symbol';
  const AREAS = new Set(['rect', 'circle', 'ellipse', 'polygon']);
  const paints = (v, opacity) => !!v && v !== 'none' && v !== 'transparent' && !/^rgba\(.*,\s*0\)$/.test(v) && parseFloat(opacity) > 0;
  const shown = (el) => {
    if (el.closest(HIDDEN)) return false;
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden' || parseFloat(cs.opacity) === 0) return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 || r.height > 0;
  };
  const mapper = (el, invert) => {
    const m = el.getScreenCTM();
    if (!m) return null;
    const t = invert ? m.inverse() : m;
    return (x, y) => new DOMPoint(x, y).matrixTransform(t);
  };
  const meets = (a, b) => Math.min(a.right, b.right) - Math.max(a.left, b.left) > 2 && Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top) > 2;
  const grid = (b) => {
    const nx = Math.min(12, Math.max(3, Math.ceil((b.width - 2) / 6)));
    const ny = Math.min(5, Math.max(2, Math.ceil((b.height - 2) / 6)));
    const pts = [];
    for (let i = 0; i < nx; i++) for (let j = 0; j < ny; j++) {
      pts.push([b.left + 1 + (b.width - 2) * (i / (nx - 1)), b.top + 1 + (b.height - 2) * (j / (ny - 1))]);
    }
    return pts;
  };
  const labels = [], shapes = [], edges = [];
  for (const root of document.querySelectorAll('svg')) {
    if (root.parentElement && root.parentElement.closest('svg')) continue;
    res.svgs++;
    for (const el of root.querySelectorAll('text')) {
      if (labels.length >= 300) { res.truncated = true; break; }
      if (!(el.textContent || '').trim() || !shown(el)) continue;
      const box = el.getBoundingClientRect();
      let start = [box.left + 1, box.top + box.height / 2];
      try {
        const toScreen = mapper(el, false);
        const p = el.getStartPositionOfChar(0);
        if (toScreen) start = [toScreen(p.x, p.y).x + 1, box.top + box.height / 2];
      } catch (e) { /* no glyph position: the box's left edge */ }
      labels.push({ el, box, start });
    }
    for (const el of root.querySelectorAll('rect, circle, ellipse, polygon, polyline, path, line')) {
      if (shapes.length >= 400 && edges.length >= 300) { res.truncated = true; break; }
      if (!shown(el)) continue;
      const cs = getComputedStyle(el);
      const tag = el.tagName.toLowerCase();
      const filled = tag !== 'line' && paints(cs.fill, cs.fillOpacity);
      const stroked = paints(cs.stroke, cs.strokeOpacity) && parseFloat(cs.strokeWidth) > 0;
      if ((filled || (stroked && AREAS.has(tag))) && shapes.length < 400) {
        const local = mapper(el, true);
        if (local) shapes.push({ el, box: el.getBoundingClientRect(), local });
      } else if (stroked && !filled && edges.length < 300 && typeof el.getTotalLength === 'function') {
        const toScreen = mapper(el, false);
        if (!toScreen) continue;
        const len = el.getTotalLength();
        const n = Math.min(400, Math.max(2, Math.ceil(len / 3)));
        const pts = [];
        for (let k = 0; k <= n; k++) {
          const p = el.getPointAtLength((len * k) / n);
          const q = toScreen(p.x, p.y);
          pts.push([q.x, q.y]);
        }
        edges.push({ el, box: el.getBoundingClientRect(), pts });
      }
    }
  }
  res.labels = labels.length; res.shapes = shapes.length; res.edges = edges.length;
  // HTML text drawn over the figure (a badge, a count, a caption positioned
  // on a line): an element with its own visible text and no SVG ancestor.
  // Its box is its text's glyph boxes, or its whole border box when it paints
  // a background or a border of its own (a badge) and holds no figure; it
  // takes part in the edge test only, against the edges in page coordinates.
  const htmlLabels = [];
  if (edges.length) {
    const own = (el) => Array.from(el.childNodes).filter((n) => n.nodeType === 3 && n.textContent.trim());
    const unite = (rs) => rs.reduce((u, r) => (u ? { left: Math.min(u.left, r.left), top: Math.min(u.top, r.top), right: Math.max(u.right, r.right), bottom: Math.max(u.bottom, r.bottom) } : { left: r.left, top: r.top, right: r.right, bottom: r.bottom }), null);
    for (const el of document.body.querySelectorAll('*')) {
      if (htmlLabels.length >= 300) { res.truncated = true; break; }
      if (el.closest('svg') || el.tagName === 'SCRIPT' || el.tagName === 'STYLE') continue;
      const texts = own(el);
      if (!texts.length || !shown(el)) continue;
      const cs = getComputedStyle(el);
      const painted = (paints(cs.backgroundColor, '1') || parseFloat(cs.borderTopWidth) > 0 || parseFloat(cs.borderLeftWidth) > 0) && !el.querySelector('svg');
      let box = null;
      if (painted) {
        box = el.getBoundingClientRect();
      } else {
        const rs = [];
        for (const n of texts) {
          const range = document.createRange();
          range.selectNodeContents(n);
          for (const r of range.getClientRects()) if (r.width > 0 && r.height > 0) rs.push(r);
        }
        const u = unite(rs);
        if (u) box = { left: u.left, top: u.top, right: u.right, bottom: u.bottom, width: u.right - u.left, height: u.bottom - u.top };
      }
      if (box && box.width > 2 && box.height > 2) htmlLabels.push({ el, box });
    }
  }
  const inFill = (s, x, y) => { try { return s.el.isPointInFill(s.local(x, y)); } catch (e) { return false; } };
  for (const t of labels) {
    if (late()) break;
    const pts = grid(t.box);
    for (const s of shapes) {
      if (!meets(t.box, s.box)) continue;
      let inside = 0;
      for (const [x, y] of pts) if (inFill(s, x, y)) inside++;
      if (inside === 0 || inside === pts.length) continue;
      const share = Math.round((100 * inside) / pts.length);
      if (inFill(s, t.start[0], t.start[1])) {
        if (res.ovf.length < CAP) res.ovf.push({ text: name(t.el), shape: name(s.el), outside: 100 - share });
      } else if (res.overlap.length < CAP) {
        res.overlap.push({ text: name(t.el), shape: name(s.el), share });
      }
    }
    const b = { left: t.box.left + 1, right: t.box.right - 1, top: t.box.top + 1, bottom: t.box.bottom - 1 };
    const hits = edges.filter((e) => meets(t.box, e.box) || (e.box.width <= 2 || e.box.height <= 2))
      .filter((e) => e.pts.some(([x, y]) => x > b.left && x < b.right && y > b.top && y < b.bottom));
    if (hits.length && res.edge.length < CAP) res.edge.push({ text: name(t.el), edges: hits.length, first: name(hits[0].el) });
  }
  for (const t of htmlLabels) {
    if (late()) break;
    const b = { left: t.box.left + 1, right: t.box.right - 1, top: t.box.top + 1, bottom: t.box.bottom - 1 };
    const hits = edges.filter((e) => meets(t.box, e.box) || (e.box.width <= 2 || e.box.height <= 2))
      .filter((e) => e.pts.some(([x, y]) => x > b.left && x < b.right && y > b.top && y < b.bottom));
    if (hits.length && res.edge.length < CAP) res.edge.push({ text: name(t.el), edges: hits.length, first: name(hits[0].el) });
  }
  // Edge-edge crossings, a figure statistic: each edge cut to at most 64
  // segments; a meeting within 3 px of an edge's end (two edges sharing a
  // node) is not a crossing.
  const lines = edges.slice(0, 150).map((e) => {
    const step = Math.max(1, Math.ceil((e.pts.length - 1) / 64));
    const pts = e.pts.filter((_, i) => i % step === 0);
    if (pts[pts.length - 1] !== e.pts[e.pts.length - 1]) pts.push(e.pts[e.pts.length - 1]);
    return { pts, box: e.box, ends: [e.pts[0], e.pts[e.pts.length - 1]] };
  });
  const nearEnd = (ends, x, y) => ends.some(([ex, ey]) => Math.hypot(ex - x, ey - y) < 3);
  const loose = (a, b) => Math.min(a.right, b.right) + 1 >= Math.max(a.left, b.left) && Math.min(a.bottom, b.bottom) + 1 >= Math.max(a.top, b.top);
  outer: for (let i = 0; i < lines.length; i++) {
    for (let j = i + 1; j < lines.length; j++) {
      if (late()) break outer;
      const A = lines[i], B = lines[j];
      if (!loose(A.box, B.box)) continue;
      // A crossing on a sample point is seen by the two segments around it:
      // one crossing per 2 px of the pair.
      const seen = [];
      for (let p = 0; p + 1 < A.pts.length; p++) {
        const [x1, y1] = A.pts[p], [x2, y2] = A.pts[p + 1];
        for (let q = 0; q + 1 < B.pts.length; q++) {
          const [x3, y3] = B.pts[q], [x4, y4] = B.pts[q + 1];
          const d = (x2 - x1) * (y4 - y3) - (y2 - y1) * (x4 - x3);
          if (Math.abs(d) < 1e-9) continue;
          const u = ((x3 - x1) * (y4 - y3) - (y3 - y1) * (x4 - x3)) / d;
          const v = ((x3 - x1) * (y2 - y1) - (y3 - y1) * (x2 - x1)) / d;
          if (u < 0 || u >= 1 || v < 0 || v >= 1) continue;
          const x = x1 + u * (x2 - x1), y = y1 + u * (y2 - y1);
          if (nearEnd(A.ends, x, y) || nearEnd(B.ends, x, y)) continue;
          if (seen.some(([sx, sy]) => Math.hypot(sx - x, sy - y) < 2)) continue;
          seen.push([x, y]);
          res.crossings++;
        }
      }
    }
  }
  probeEdgeGeometry(res, name, edges.slice(0, 150), shapes, inFill, late, CAP);
  return res;
};

// Edge geometry, per edge (named with its data-edge when it has one): an end
// lying inside its target shape (the arrowhead hidden in or under the node),
// an end stopping short of the shape it points at, two edges sharing one lane
// (within 2 px for 12 px or more), an edge riding a shape's border (within
// 2 px for 12 px or more). A shape holding the edge's start is never its
// target; a shape of 12 px or less both ways (an arrowhead, a port dot) is not
// a target; a shape that holds two or more others (a hull, a background) is
// not a target either, but its border counts.
const probeEdgeGeometry = (res, name, edges, shapes, inFill, late, CAP) => {
  const NEAR = 2, RUN = 12, GAP = 16, DEEP = 3, SHORT = 4;
  const ename = (el) => {
    const key = el.getAttribute('data-edge');
    return name(el) + (key ? ' [data-edge=' + key.slice(0, 40) + ']' : '');
  };
  const sized = shapes.filter((s) => s.box.width > 12 || s.box.height > 12);
  const holds = (a, b) => a !== b && a.box.left <= b.box.left + 0.5 && a.box.top <= b.box.top + 0.5
    && a.box.right >= b.box.right - 0.5 && a.box.bottom >= b.box.bottom - 0.5;
  const targets = sized.filter((s) => sized.filter((o) => holds(s, o)).length < 2);
  const around = (b, x, y, pad) => x >= b.left - pad && x <= b.right + pad && y >= b.top - pad && y <= b.bottom + pad;
  const markerTip = (el) => {
    const ref = getComputedStyle(el).markerEnd || el.getAttribute('marker-end') || '';
    const m = /url\(\s*["']?#([^"')]+)/.exec(ref);
    const mk = m ? document.getElementById(m[1]) : null;
    if (!mk || mk.tagName.toLowerCase() !== 'marker') return 0;
    const w = parseFloat(mk.getAttribute('markerWidth') || '3');
    const refX = parseFloat(mk.getAttribute('refX') || '0');
    const vb = mk.viewBox && mk.viewBox.baseVal;
    const extent = vb && vb.width ? (vb.x + vb.width - refX) * (w / vb.width) : w - refX;
    const unit = mk.getAttribute('markerUnits') === 'userSpaceOnUse' ? 1 : parseFloat(getComputedStyle(el).strokeWidth) || 1;
    const ctm = el.getScreenCTM();
    return Math.max(0, extent * unit * (ctm ? Math.hypot(ctm.a, ctm.b) : 1));
  };
  const runs = (pts, on) => {
    let run = 0, best = 0, prev = false;
    for (let k = 0; k < pts.length; k++) {
      const now = on(pts[k][0], pts[k][1]);
      if (now && prev) run += Math.hypot(pts[k][0] - pts[k - 1][0], pts[k][1] - pts[k - 1][1]);
      else run = 0;
      prev = now;
      if (run > best) best = run;
    }
    return best;
  };
  for (const e of edges) {
    if (late()) return;
    const pts = e.pts, n = pts.length - 1;
    const [sx, sy] = pts[0], [ex, ey] = pts[n];
    const cand = targets.filter((s) => !inFill(s, sx, sy));
    let hit = null, depth = 0;
    for (const s of cand) {
      if (!around(s.box, ex, ey, 1) || !inFill(s, ex, ey)) continue;
      let k = n;
      while (k > 0 && inFill(s, pts[k - 1][0], pts[k - 1][1])) k--;
      const d = Math.hypot(ex - pts[k][0], ey - pts[k][1]);
      if (d >= DEEP && (!hit || s.box.width * s.box.height < hit.box.width * hit.box.height)) { hit = s; depth = d; }
    }
    if (hit) {
      if (res.ends.length < CAP) {
        const under = !!(hit.el.compareDocumentPosition(e.el) & Node.DOCUMENT_POSITION_PRECEDING);
        res.ends.push({ edge: ename(e.el), shape: name(hit.el), px: Math.round(depth), under });
      }
    } else {
      let k = n - 1;
      while (k > 0 && Math.hypot(ex - pts[k][0], ey - pts[k][1]) < 2) k--;
      const dx = ex - pts[k][0], dy = ey - pts[k][1], dl = Math.hypot(dx, dy);
      const tip = dl > 0 ? markerTip(e.el) : 0;
      const ahead = dl > 0 ? cand.filter((s) => around(s.box, ex, ey, GAP + tip)) : [];
      for (let g = 1; g <= GAP + tip && ahead.length; g++) {
        const x = ex + (dx / dl) * g, y = ey + (dy / dl) * g;
        const s = ahead.find((t) => inFill(t, x, y));
        if (!s) continue;
        if (g - tip >= SHORT && res.short.length < CAP) res.short.push({ edge: ename(e.el), shape: name(s.el), px: Math.round(g - tip) });
        break;
      }
    }
    for (const s of sized) {
      if (late()) return;
      if (!(s.box.left - 3 <= e.box.right && s.box.right + 3 >= e.box.left && s.box.top - 3 <= e.box.bottom && s.box.bottom + 3 >= e.box.top)) continue;
      const best = runs(pts, (x, y) => {
        if (!around(s.box, x, y, NEAR + 1)) return false;
        const c = [inFill(s, x + NEAR, y), inFill(s, x - NEAR, y), inFill(s, x, y + NEAR), inFill(s, x, y - NEAR)];
        return c.some(Boolean) && !c.every(Boolean);
      });
      if (best >= RUN && res.border.length < CAP) res.border.push({ edge: ename(e.el), shape: name(s.el), px: Math.round(best) });
    }
  }
  const seg = (x, y, [x1, y1], [x2, y2]) => {
    const vx = x2 - x1, vy = y2 - y1, l2 = vx * vx + vy * vy;
    const t = l2 ? Math.max(0, Math.min(1, ((x - x1) * vx + (y - y1) * vy) / l2)) : 0;
    return Math.hypot(x - (x1 + t * vx), y - (y1 + t * vy));
  };
  for (let i = 0; i < edges.length; i++) {
    for (let j = i + 1; j < edges.length; j++) {
      if (late()) return;
      const A = edges[i], B = edges[j];
      if (!(Math.min(A.box.right, B.box.right) + NEAR >= Math.max(A.box.left, B.box.left)
        && Math.min(A.box.bottom, B.box.bottom) + NEAR >= Math.max(A.box.top, B.box.top))) continue;
      const step = Math.max(1, Math.ceil((B.pts.length - 1) / 64));
      const bp = B.pts.filter((_, k) => k % step === 0);
      if (bp[bp.length - 1] !== B.pts[B.pts.length - 1]) bp.push(B.pts[B.pts.length - 1]);
      const best = runs(A.pts, (x, y) => {
        if (!around(B.box, x, y, NEAR)) return false;
        for (let k = 0; k + 1 < bp.length; k++) if (seg(x, y, bp[k], bp[k + 1]) <= NEAR) return true;
        return false;
      });
      if (best >= RUN && res.lane.length < CAP) res.lane.push({ a: ename(A.el), b: ename(B.el), px: Math.round(best) });
    }
  }
};
"""

# The SVG and WRAP passes are declared before the probe's body so the one
# function Playwright evaluates holds them all.
PROBE_JS = "(bounds) => {\n" + _PROBE_SVG_JS + "\n" + _PROBE_WRAP_JS + "\nreturn (" + PROBE_JS.strip() + ")(bounds);\n}"


class HtmlRenderError(ValueError):
    """The render could not run as asked; the message is the tool's answer."""


def _safe_name(name: str | None) -> str:
    candidate = _SAFE_NAME_RE.sub("-", (name or "").strip()).strip("-.")
    candidate = re.sub(r"\.(png|html?)$", "", candidate, flags=re.IGNORECASE)
    return (candidate or f"figure-{uuid.uuid4().hex[:8]}")[:120]


def _bounded(value: object, *, default: int, lo: int, hi: int, what: str) -> int:
    if value is None or value == "":
        return default
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        raise HtmlRenderError(f"{what} must be a whole number of pixels, not {value!r}") from None
    if not lo <= number <= hi:
        raise HtmlRenderError(f"{what} must be between {lo} and {hi}, not {number}")
    return number


def check_canvas_area(width: int, height: int, scale: int) -> None:
    """Refuse a canvas whose device-pixel area exceeds :data:`MAX_DEVICE_PIXELS`.

    Each bound on its own is honest (4096 px is a poster edge, 3x is a retina
    density); their product is not. The message names the area asked for and
    the ceiling so the model lowers the scale or the canvas, not both blindly.
    """
    area = width * height * scale * scale
    if area > MAX_DEVICE_PIXELS:
        raise HtmlRenderError(
            f"{width}x{height} at {scale}x is {area:,} device pixels; the ceiling is "
            f"{MAX_DEVICE_PIXELS:,} ({DEFAULT_WIDTH}x{DEFAULT_HEIGHT} at {MAX_SCALE}x). "
            "Lower the scale or the canvas."
        )


@functools.lru_cache(maxsize=8)
def font_face_css(dist: Path = _FRONTEND_DIST, fonts: Path = _WORKER_FONTS) -> str:
    """``@font-face`` rules inlining the deck fonts, or ``""``.

    Read from the built frontend's ``assets`` (the unified image), else from
    *fonts* (the worker image, which has no frontend bundle but runs every
    assignment's renders). The renderer aborts every network request, so the
    fonts travel as ``data:`` URIs. Only the latin subset at the four weights
    a figure uses; neither directory (a dev checkout) yields nothing and the
    render falls back to system fonts - said in the result. Cached per
    directory pair: the files are part of the image and never change while it
    runs, and the read (a few hundred KB, base64-encoded) would otherwise sit
    on the event loop once per call.
    """
    assets = dist / "assets"
    if not assets.is_dir():
        assets = fonts
    if not assets.is_dir():
        return ""
    rules: list[str] = []
    for family, slug in _FONT_FAMILIES:
        for weight in _FONT_WEIGHTS:
            for style in ("normal", "italic"):
                if style == "italic" and weight != 400:
                    continue
                matches = sorted(assets.glob(f"{slug}-latin-{weight}-{style}*.woff2"))
                if not matches:
                    continue
                try:
                    data = matches[0].read_bytes()
                except OSError:
                    continue
                encoded = base64.b64encode(data).decode("ascii")
                rules.append(
                    f"@font-face{{font-family:'{family}';font-style:{style};font-weight:{weight};"
                    f"font-display:block;src:url(data:font/woff2;base64,{encoded}) format('woff2');}}"
                )
    return "\n".join(rules)


def wrap_html(html: str, *, width: int, height: int, font_css: str) -> str:
    """The document handed to the browser: the agent's HTML on a fixed canvas.

    A full document (``<html>``) is kept as written and only gains the fonts
    in its ``<head>``; a fragment is wrapped in a body sized to the canvas
    with no margin, so what the agent laid out is what the PNG shows.
    """
    font_block = f"<style>{font_css}</style>" if font_css else ""
    canvas_css = (
        f"<style>html,body{{margin:0;padding:0;}}body{{width:{width}px;height:{height}px;"
        "overflow:hidden;}}</style>"
    )
    stripped = html.lstrip()
    if re.match(r"<(!doctype|html)\b", stripped, flags=re.IGNORECASE):
        head_match = re.search(r"<head\b[^>]*>", stripped, flags=re.IGNORECASE)
        if head_match:
            at = head_match.end()
            return stripped[:at] + font_block + canvas_css + stripped[at:]
        html_match = re.search(r"<html\b[^>]*>", stripped, flags=re.IGNORECASE)
        at = html_match.end() if html_match else 0
        return stripped[:at] + f"<head>{font_block}{canvas_css}</head>" + stripped[at:]
    return (
        "<!doctype html><html><head><meta charset=\"utf-8\">"
        f"{font_block}{canvas_css}</head><body>{html}</body></html>"
    )


def format_probe(findings: dict, *, width: int, height: int) -> tuple[str, int]:
    """The probe as lines the model acts on, worst first; and the count."""
    lines: list[str] = []
    measured = findings.get("measured") or {}
    height = max(height, int(measured.get("height") or 0))
    for item in findings.get("out", []):
        overs = []
        if item.get("right", 0) > width:
            overs.append(f"right={item['right']} > {width}")
        if item.get("bottom", 0) > height:
            overs.append(f"bottom={item['bottom']} > {height}")
        if item.get("left", 0) < 0:
            overs.append(f"left={item['left']} < 0")
        if item.get("top", 0) < 0:
            overs.append(f"top={item['top']} < 0")
        lines.append(f"OUT {item['el']}: {', '.join(overs) or 'past the canvas'}")
    for item in findings.get("ovf", []):
        scroll = item.get("scroll", [0, 0])
        box = item.get("box", [0, 0])
        kind = "nowrap text runs past its box" if item.get("nowrap") else "content clipped"
        lines.append(f"OVF {item['el']}: {kind} ({scroll[0]}x{scroll[1]} in {box[0]}x{box[1]})")
    for item in findings.get("wrap", []):
        lines.append(
            f"WRAP {item['el']}: text runs to {item.get('lines', 2)} lines where its column holds "
            f"{item.get('column', 1)} (top={item.get('top', 0)})"
        )
    svg = findings.get("svg") or {}
    for item in svg.get("ovf", []):
        lines.append(
            f"OVF {item['text']}: runs past the border of {item['shape']} "
            f"({item.get('outside', 0)}% of the label outside)"
        )
    for item in findings.get("overlap", []):
        lines.append(f"OVERLAP {item['a']} x {item['b']}: {item['area']} px2")
    for item in svg.get("overlap", []):
        lines.append(
            f"OVERLAP {item['text']} x {item['shape']}: label drawn over a shape it is not inside "
            f"({item.get('share', 0)}% of the label covered)"
        )
    for item in svg.get("edge", []):
        count = item.get("edges", 1)
        lines.append(
            f"EDGE-ON-TEXT {item['text']}: crossed by {count} edge{'s' if count != 1 else ''} (first {item['first']})"
        )
    for item in svg.get("ends", []):
        where = "drawn under it" if item.get("under") else "inside it"
        lines.append(
            f"EDGE-END {item['edge']}: ends {item.get('px', 0)} px inside {item['shape']}, its arrowhead {where}"
        )
    for item in svg.get("short", []):
        lines.append(f"EDGE-END {item['edge']}: stops {item.get('px', 0)} px short of {item['shape']}")
    for item in svg.get("lane", []):
        lines.append(f"EDGE-LANE {item['a']} x {item['b']}: share one lane for {item.get('px', 0)} px")
    for item in svg.get("border", []):
        lines.append(f"EDGE-BORDER {item['edge']}: runs along the border of {item['shape']} for {item.get('px', 0)} px")
    total = len(lines)
    return "\n".join(lines[:MAX_PROBE_FINDINGS]), total


def format_coverage(findings: dict, *, height: int) -> str:
    """One line when the probe measured past the canvas, or ``""``.

    A figure taller than the canvas cap is drawn to the cap only; the probe
    then measured the whole document, and the model must know the PNG shows
    part of what the findings cover.
    """
    measured = findings.get("measured") or {}
    full = int(measured.get("height") or 0)
    if full <= height:
        return ""
    return (
        f"Coverage: the figure is {full} px tall; the PNG shows its first {height} px (the canvas cap) "
        f"and the probe measured all {full} px."
    )


def format_svg_stats(findings: dict) -> str:
    """One line of figure statistics for the SVG pass, or ``""`` without SVG.

    Edge-edge crossings are a property of the layout (a dense graph has some),
    not a defect to fix one by one: they are counted here, not listed.
    """
    svg = findings.get("svg") or {}
    if not svg.get("svgs"):
        return ""
    line = (
        f"SVG: {svg.get('labels', 0)} label(s), {svg.get('shapes', 0)} shape(s), "
        f"{svg.get('edges', 0)} edge(s) measured; {svg.get('crossings', 0)} edge-edge crossing(s)."
    )
    if svg.get("truncated"):
        line += " The figure is larger than the probe's bounds: only part of it was measured."
    return line


def format_probe_report(findings: dict, *, width: int, height: int) -> str:
    """The probe verdict of one render: clean, or its findings, then the coverage and SVG lines."""
    probe_text, probe_total = format_probe(findings, width=width, height=height)
    if probe_total == 0:
        report = (
            "Probe: clean - nothing outside the canvas, nothing clipped, no overlapping text or images"
            ", no wrapped table cell, no label crossed by an edge, no edge end, lane or border defect."
        )
    else:
        more = f" (+{probe_total - MAX_PROBE_FINDINGS} more)" if probe_total > MAX_PROBE_FINDINGS else ""
        report = f"Probe: {probe_total} finding(s){more} - fix them in the source and re-render:\n{probe_text}"
    for extra in (format_coverage(findings, height=height), format_svg_stats(findings)):
        if extra:
            report = f"{report}\n{extra}"
    return report


async def _capture_viewport(page: Page) -> bytes:
    """Screenshot the viewport, retrying only while Chromium has no frame.

    ``Page.captureScreenshot`` can answer "Unable to capture screenshot"
    when the compositor has not produced a frame yet. That exact error is
    retried, at most :data:`SCREENSHOT_ATTEMPTS` times, after waiting for two
    animation frames; any other error is raised at once.
    """
    for attempt in range(1, SCREENSHOT_ATTEMPTS + 1):
        try:
            return await page.screenshot(full_page=False, type="png")
        except Exception as exc:
            if _NO_FRAME_ERROR not in str(exc) or attempt == SCREENSHOT_ATTEMPTS:
                raise
            with contextlib.suppress(Exception):
                await page.evaluate(_TWO_FRAMES_JS)
    raise AssertionError("unreachable")  # pragma: no cover


async def render_html(
    html: str, *, width: int, height: int, scale: int, font_css: str
) -> tuple[bytes, dict]:
    """Render *html* on a ``width`` x ``height`` canvas; return ``(png, probe)``.

    Locked down like the PDF renderer: JavaScript is OFF for the document
    (the probe runs through Playwright's own evaluate, which the page's
    setting does not gate) and every network request is aborted - only
    ``data:`` / ``about:`` / ``blob:`` resolve.
    """
    from playwright.async_api import Route, async_playwright

    from app.services.headless_render import RENDER_CHROMIUM_ARGS, RENDER_SERVICE_WORKERS
    from app.services.presentation_export import _render_slot

    async def _block_external(route: Route) -> None:
        if route.request.url.startswith(("data:", "about:", "blob:")):
            await route.continue_()
        else:
            await route.abort()

    document = wrap_html(html, width=width, height=height, font_css=font_css)

    async def _run() -> tuple[bytes, dict]:
        async with _render_slot(), async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True, args=list(RENDER_CHROMIUM_ARGS))
            try:
                context = await browser.new_context(
                    viewport={"width": width, "height": height},
                    device_scale_factor=scale,
                    java_script_enabled=False,
                    service_workers=RENDER_SERVICE_WORKERS,
                )
                try:
                    await context.route("**/*", _block_external)
                    page = await context.new_page()
                    # Belt over the disabled JavaScript: scripts and inline
                    # handlers are stripped before the content is set.
                    await page.set_content(_strip_scripts(document), wait_until="load")
                    # Inlined fonts decode after ``load``; the probe and the
                    # screenshot must measure the glyphs that will be drawn. A
                    # failure here is a font that never loads, not a render
                    # failure: the screenshot then shows the fallback.
                    with contextlib.suppress(Exception):
                        await page.evaluate("() => document.fonts ? document.fonts.ready : null")
                    png = await _capture_viewport(page)
                    bounds = await _whole_document_bounds(page, width=width, height=height)
                    probe = await page.evaluate(PROBE_JS, bounds)
                    probe = probe if isinstance(probe, dict) else {}
                    if bounds:
                        probe["measured"] = bounds
                    return png, probe
                finally:
                    await context.close()
            finally:
                await browser.close()

    return await asyncio.wait_for(_run(), timeout=RENDER_TIMEOUT_SECONDS)


#: Lifts the canvas clamp of :func:`wrap_html` and answers the document's
#: laid-out height, for a figure that may run past the canvas cap. Done in
#: an ``evaluate`` (Playwright's own world): ``page.add_style_tag`` waits on
#: the page's script world, which is off here, and never returns. A figure no
#: taller than ``limit`` gets its clamp back and answers 0.
_UNCLAMP_JS = r"""
(limit) => {
  const style = document.createElement('style');
  style.textContent = 'html,body{height:auto!important;min-height:0!important;overflow:visible!important;}';
  document.head.appendChild(style);
  let full = Math.max(document.documentElement.scrollHeight, document.body ? document.body.scrollHeight : 0);
  for (const el of Array.from(document.body ? document.body.querySelectorAll('*') : []).slice(0, 20000)) {
    full = Math.max(full, el.getBoundingClientRect().bottom);
  }
  full = Math.ceil(full);
  if (full <= limit + 1) { style.remove(); return 0; }
  return full;
}
"""


async def _whole_document_bounds(page: Page, *, width: int, height: int) -> dict[str, int] | None:
    """The area the probe measures when the figure is taller than the canvas cap.

    The canvas is at most :data:`MAX_CANVAS_PX` high, so a board taller than
    that (a long table mockup) cannot be drawn whole. The PNG keeps the cap -
    it is taken before this call - but the probe must still cover every
    element: the clamp is lifted and the probe runs with the document's full
    height as its bounds. Under the cap nothing changes (``None``): the
    canvas is the area, content below it is an ``OUT`` finding as before. A
    figure no taller than the canvas gets its clamp back.
    """
    if height < MAX_CANVAS_PX:
        return None
    full = await page.evaluate(_UNCLAMP_JS, height)
    if not isinstance(full, (int, float)) or full <= height + 1:
        return None
    return {"width": width, "height": int(full)}


_SCRIPT_RE = re.compile(r"<script\b[^>]*>.*?</script\s*>", re.IGNORECASE | re.DOTALL)
_EVENT_ATTR_RE = re.compile(r"\s+on[a-z]+\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)", re.IGNORECASE)


def _strip_scripts(document: str) -> str:
    """Drop ``<script>`` blocks and inline event handlers: the figure is a layout."""
    return _EVENT_ATTR_RE.sub("", _SCRIPT_RE.sub("", document))


def oversized_inline_refs(html: str, file_context: FileContext | None) -> list[str]:
    """The conversation IMAGES *html* references by handle that are over
    :data:`MAX_INLINE_FILE_BYTES`, as ``filename (size)`` strings.

    Read off the ``FileRef`` metadata, before any byte is downloaded: the
    inliner base64-encodes every referenced image whole, so the size is the
    one thing to check before it runs, not after.
    """
    if file_context is None or not html:
        return []
    seen: set[str] = set()
    over: list[str] = []
    for match in EMBED_FILE_REF_RE.finditer(html):
        fid = match.group("marker") or match.group("uuid")
        if not fid or fid in seen:
            continue
        seen.add(fid)
        ref = _resolve_file_ref(file_context, fid)
        if ref is None or not (ref.content_type or "").lower().startswith("image/"):
            continue
        if (ref.size or 0) > MAX_INLINE_FILE_BYTES:
            over.append(f"{ref.filename} ({format_file_size(ref.size)})")
    return over


async def inline_template_asset_refs(html: str, user_id: str | None) -> tuple[str, list[str]]:
    """Inline the ``file:<uuid>`` / ``[[FILE:<uuid>]]`` handles of *html* that name a
    TEMPLATE ASSET the caller can read, as ``data:`` URIs.

    A template asset (a brand logo, a background) is not a file of the
    conversation, so the conversation inliner leaves its handle as written and
    Chromium, which loads nothing, drew a blank. The grant is the one
    ``get_template`` and the file download apply: the asset's container under
    the caller's three-tier visibility (``caller_visible_filter``: own, shared
    with the caller or a group, company-managed). Images only (SVG included),
    each under :data:`MAX_INLINE_FILE_BYTES`, read off the row before any byte
    is downloaded. Returns the document and the ``filename (size)`` of every
    asset refused for its size; an id that is no visible template asset is
    left untouched.
    """
    from sqlalchemy import select

    from app.api.v1.helpers import caller_visible_filter, get_user_group_ids
    from app.core.utils import safe_uuid
    from app.models.file_attachment import FileAttachment
    from app.models.template_container import TEMPLATE_ASSET_SOURCE, TemplateContainer
    from app.services.file_storage import file_storage

    from ._shared import _own_session

    wanted: dict[uuid.UUID, str] = {}
    for match in EMBED_FILE_REF_RE.finditer(html or ""):
        fid = match.group("marker") or match.group("uuid")
        parsed = safe_uuid(fid) if fid else None
        if parsed is not None:
            wanted.setdefault(parsed, fid)
    if not wanted:
        return html, []
    user_uuid = safe_uuid(user_id) if user_id else None
    async with _own_session() as session:
        group_ids = await get_user_group_ids(session, user_uuid) if user_uuid is not None else []
        rows = (
            await session.execute(
                select(FileAttachment)
                .join(TemplateContainer, TemplateContainer.id == FileAttachment.template_container_id)
                .where(
                    FileAttachment.id.in_(list(wanted)),
                    FileAttachment.source == TEMPLATE_ASSET_SOURCE,
                    caller_visible_filter(TemplateContainer, user_uuid, group_ids),
                )
            )
        ).scalars().all()
    too_big: list[str] = []
    data_uris: dict[str, str] = {}
    for row in rows:
        mime = (row.content_type or "").split(";", 1)[0].strip().lower()
        if not mime.startswith("image/"):
            continue
        if (row.size or 0) > MAX_INLINE_FILE_BYTES:
            too_big.append(f"{row.filename} ({format_file_size(row.size)})")
            continue
        try:
            data = await file_storage.download(row.s3_object_name)
        except Exception as exc:
            logger.warning("render_html_to_image: template asset %s unreadable: %s", row.id, format_exception_message(exc))
            continue
        data_uris[str(row.id).lower()] = f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"
    if too_big or not data_uris:
        return html, too_big

    def _replace(match: re.Match) -> str:
        fid = (match.group("marker") or match.group("uuid") or "").lower()
        return data_uris.get(fid, match.group(0))

    return EMBED_FILE_REF_RE.sub(_replace, html), []


async def _png_to_workspace(path: object, png: bytes) -> str:
    """Write a render's PNG to the run's sandbox workspace at *path*; the answer's line, or "".

    The render itself stands whatever happens here: a run without a
    workspace or a path the workspace refuses is said in the line, never a
    failed call.
    """
    if not isinstance(path, str) or not path.strip():
        return ""
    workspace = current_workspace()
    if workspace is None:
        return "PNG not written to the workspace: this run has no sandbox workspace.\n"
    try:
        written = await workspace.write_file(path.strip(), png, tool="render_html_to_image")
    except ValueError as exc:
        return f"PNG not written to the workspace: {exc}\n"
    return f"PNG also written to the workspace: {written}.\n"


def build_render_html_tool(
    *,
    db: AsyncSession | None = None,
    user_id: str | None = None,
    conversation_id: str | None = None,
    assignment_run_id: str | None = None,
    executor: CallableToolExecutor | None = None,
) -> tuple[list[ToolDefinition], CallableToolExecutor]:
    """Build ``render_html_to_image`` (registered on *executor* when given)."""

    async def _render_html_to_image(
        html: str | None = None,
        width: object = None,
        height: object = None,
        scale: object = None,
        name: str | None = None,
        source_file_id: str | None = None,
        png_workspace_path: str | None = None,
        pages: object = None,
        file_context: FileContext | None = None,
    ) -> ToolResult:
        from app.services.image_summary import format_visual_summary, summarize_image

        from .media import _inline_embedded_file_refs, decode_file_text

        if db is None or not (user_id or conversation_id or assignment_run_id):
            return ToolResult(
                tool_call_id="",
                content="Error: render_html_to_image needs a conversation or run to store the files in.",
                is_error=True,
            )
        try:
            canvas_w = _bounded(width, default=DEFAULT_WIDTH, lo=MIN_CANVAS_PX, hi=MAX_CANVAS_PX, what="width")
            canvas_h = _bounded(height, default=DEFAULT_HEIGHT, lo=MIN_CANVAS_PX, hi=MAX_CANVAS_PX, what="height")
            density = _bounded(scale, default=1, lo=1, hi=MAX_SCALE, what="scale")
            check_canvas_area(canvas_w, canvas_h, density)
        except HtmlRenderError as exc:
            return ToolResult(tool_call_id="", content=f"Error: {exc}", is_error=True)
        if pages is not None:
            if html or source_file_id or png_workspace_path:
                return ToolResult(
                    tool_call_id="",
                    content="Error: pages renders workspace files on its own: leave html, source_file_id and png_workspace_path out.",
                    is_error=True,
                )
            return await _render_pages(pages, width=canvas_w, height=canvas_h, scale=density, file_context=file_context)

        source_ref = None
        from_workspace = False
        if source_file_id:
            source_ref = _resolve_file_ref(file_context, source_file_id)
            if source_ref is None or file_context is None:
                return ToolResult(
                    tool_call_id="",
                    content=f"Error: no file '{source_file_id}' is attached to this conversation.",
                    is_error=True,
                )
            try:
                raw = await file_context.read_bytes(source_ref.file_id)
            except Exception as exc:
                raw = None
                logger.warning("render_html_to_image: reading %s failed: %s", source_ref.file_id, format_exception_message(exc))
            text = decode_file_text(raw) if raw is not None else None
            if text is None:
                return ToolResult(
                    tool_call_id="",
                    content=f"Error: '{source_ref.filename}' is not a UTF-8 HTML source file.",
                    is_error=True,
                )
            html = text
            if not name:
                name = re.sub(r"\.html?$", "", source_ref.filename, flags=re.IGNORECASE)
            # A ``workspace_path`` source reaches the tool as a ref the
            # dispatcher parks in memory for this call only (no storage key):
            # it is saved below as a sibling .html like an inline ``html``,
            # so source_file_id and edit_file_content keep working after it.
            if getattr(source_ref, "source", None) == WORKSPACE_FILE_SOURCE:
                from_workspace = True
                source_ref = None
        if not isinstance(html, str) or not html.strip():
            return ToolResult(
                tool_call_id="",
                content=(
                    "Error: pass the HTML to render (html), the id of a stored .html source (source_file_id) "
                    "or a .html file of the sandbox workspace (workspace_path)."
                ),
                is_error=True,
            )
        if len(html) > MAX_HTML_CHARS:
            return ToolResult(
                tool_call_id="",
                content=f"Error: the HTML is {len(html):,} characters; the ceiling is {MAX_HTML_CHARS:,}.",
                is_error=True,
            )

        stem = _safe_name(name)
        font_css = font_face_css()
        too_big = oversized_inline_refs(html, file_context)
        if too_big:
            return ToolResult(
                tool_call_id="",
                content=(
                    f"Error: the HTML references conversation images over the {format_file_size(MAX_INLINE_FILE_BYTES)} "
                    f"per-image ceiling for inlining: {', '.join(too_big)}. Use a smaller version of the image "
                    "(fetch_image_asset stores up to 5 MB). Nothing was stored."
                ),
                is_error=True,
            )
        rendered_html = await _inline_embedded_file_refs(html, file_context)
        try:
            rendered_html, too_big = await inline_template_asset_refs(rendered_html, user_id)
        except Exception as exc:
            # A failed lookup leaves the handles as written: the figure renders
            # with a hole where the logo was, never a failed call.
            logger.warning("render_html_to_image: template assets not inlined: %s", format_exception_message(exc))
            too_big = []
        if too_big:
            return ToolResult(
                tool_call_id="",
                content=(
                    f"Error: the HTML references template assets over the {format_file_size(MAX_INLINE_FILE_BYTES)} "
                    f"per-image ceiling for inlining: {', '.join(too_big)}. Nothing was stored."
                ),
                is_error=True,
            )
        if len(rendered_html) > MAX_INLINED_HTML_CHARS:
            return ToolResult(
                tool_call_id="",
                content=(
                    f"Error: with its conversation images inlined the document is "
                    f"{len(rendered_html):,} characters; the ceiling is {MAX_INLINED_HTML_CHARS:,}. "
                    "Reference fewer or smaller images (a stored image inlines as base64, "
                    "about 4/3 of its size). Nothing was stored."
                ),
                is_error=True,
            )
        try:
            png, probe = await render_html(
                rendered_html, width=canvas_w, height=canvas_h, scale=density, font_css=font_css
            )
        except Exception as exc:
            logger.warning("render_html_to_image failed: %s", format_exception_message(exc), exc_info=True)
            return ToolResult(
                tool_call_id="",
                content=(
                    "Error: the headless browser could not render the HTML "
                    f"({format_exception_message(exc)}). Nothing was stored."
                ),
                is_error=True,
            )

        try:
            source_saved = source_ref
            if source_ref is None:
                source_saved = await persist_tool_artifact(
                    db=db,
                    user_id=user_id or "",
                    filename=f"{stem}.html",
                    raw_bytes=html.encode("utf-8"),
                    content_type="text/html",
                    conversation_id=conversation_id,
                    assignment_run_id=assignment_run_id,
                    file_context=file_context,
                    source="agent_working",
                )
            png_ref = await persist_tool_artifact(
                db=db,
                user_id=user_id or "",
                filename=f"{stem}.png",
                raw_bytes=png,
                content_type="image/png",
                conversation_id=conversation_id,
                assignment_run_id=assignment_run_id,
                file_context=file_context,
                source="agent_working",
            )
        except Exception as exc:
            logger.warning("render_html_to_image: storing failed: %s", format_exception_message(exc), exc_info=True)
            return ToolResult(
                tool_call_id="",
                content=f"Error: the figure rendered but could not be stored: {format_exception_message(exc)}",
                is_error=True,
            )

        probe_line = format_probe_report(probe, width=canvas_w, height=canvas_h)
        summary_line = format_visual_summary(await summarize_image(png))
        workspace_line = await _png_to_workspace(png_workspace_path, png)
        fonts_line = (
            "Fonts: Geologica / Elms Sans / IBM Plex Sans / IBM Plex Mono loaded from the platform bundle."
            if font_css
            else "Fonts: the platform bundle is not next to this backend, so system fonts were used."
        )
        # The file id and the dimensions sit in their OWN sentence: the loop's
        # claim strip removes the whole sentence that claims the attachment
        # once the pixels are dropped, and "look at it now" goes with it (the
        # two-statements shape) while the handle stays.
        return ToolResult(
            tool_call_id="",
            content=(
                f"Rendered {png_ref.filename} ({canvas_w}x{canvas_h} px at {density}x, "
                f"{format_file_size(len(png))}) as file_id {png_ref.file_id}. The PNG is attached to this "
                "result - look at it now and save what it shows with annotate_file, then place it by its "
                f"file_id (an image block, an upload). HTML source: {source_saved.filename} (file_id "
                f"{source_saved.file_id}) - change it with edit_file_content and re-render with "
                f"render_html_to_image(source_file_id=\"{source_saved.file_id}\"), never from memory"
                + (
                    "; the workspace file stays the master copy: change it with sandbox_edit_file and "
                    "re-render with the same workspace_path.\n"
                    if from_workspace
                    else ".\n"
                )
                + workspace_line
                + f"{probe_line}\n{fonts_line}\n{summary_line}"
            ),
            files=[png_ref],
        )

    async def _render_pages(
        pages: object, *, width: int, height: int, scale: int, file_context: FileContext | None
    ) -> ToolResult:
        """Render each workspace page of *pages* on one canvas; its file id and probe verdict, no pixels.

        A design round iterates a set of generated mock pages: what it needs per
        page is the probe, not the PNG in its context. Each PNG is stored as a
        conversation file (read one with read_file_content to look at it); a page
        that cannot be read or rendered is named and the others still render.
        """
        from .media import _inline_embedded_file_refs, decode_file_text

        if (
            not isinstance(pages, list)
            or not pages
            or not all(isinstance(page, str) and page.strip() for page in pages)
        ):
            return ToolResult(
                tool_call_id="",
                content="Error: pages takes a list of .html file paths of the sandbox workspace.",
                is_error=True,
            )
        if len(pages) > MAX_BATCH_PAGES:
            return ToolResult(
                tool_call_id="",
                content=f"Error: pages names {len(pages)} files; one call renders at most {MAX_BATCH_PAGES}. Nothing was rendered.",
                is_error=True,
            )
        workspace = current_workspace()
        if workspace is None:
            return ToolResult(
                tool_call_id="",
                content="Error: pages reads the sandbox workspace and this run has none. Nothing was rendered.",
                is_error=True,
            )
        font_css = font_face_css()
        blocks: list[str] = []
        files = []
        for index, path in enumerate(pages, 1):
            try:
                shown, filename, raw = await workspace.read_file(path, tool="render_html_to_image")
            except ValueError as exc:
                blocks.append(f"{index}. {path.strip()}: not rendered - {exc}")
                continue
            html = decode_file_text(raw)
            if html is None or not html.strip():
                blocks.append(f"{index}. {shown}: not rendered - not a UTF-8 HTML file.")
                continue
            if len(html) > MAX_HTML_CHARS:
                blocks.append(
                    f"{index}. {shown}: not rendered - {len(html):,} characters, the ceiling is {MAX_HTML_CHARS:,}."
                )
                continue
            too_big = oversized_inline_refs(html, file_context)
            document = html if too_big else await _inline_embedded_file_refs(html, file_context)
            if not too_big:
                try:
                    document, too_big = await inline_template_asset_refs(document, user_id)
                except Exception as exc:
                    logger.warning(
                        "render_html_to_image: template assets not inlined: %s", format_exception_message(exc)
                    )
            if too_big or len(document) > MAX_INLINED_HTML_CHARS:
                blocks.append(f"{index}. {shown}: not rendered - its images are over the inlining ceilings.")
                continue
            stem = _safe_name(re.sub(r"\.html?$", "", filename, flags=re.IGNORECASE))
            try:
                png, probe = await render_html(document, width=width, height=height, scale=scale, font_css=font_css)
                png_ref = await persist_tool_artifact(
                    db=db,
                    user_id=user_id or "",
                    filename=f"{stem}.png",
                    raw_bytes=png,
                    content_type="image/png",
                    conversation_id=conversation_id,
                    assignment_run_id=assignment_run_id,
                    file_context=file_context,
                    source="agent_working",
                )
            except Exception as exc:
                logger.warning("render_html_to_image page %s failed: %s", shown, format_exception_message(exc))
                blocks.append(f"{index}. {shown}: not rendered - {format_exception_message(exc)}")
                continue
            files.append(png_ref)
            blocks.append(
                f"{index}. {shown}: file_id {png_ref.file_id} ({format_file_size(len(png))}).\n"
                + format_probe_report(probe, width=width, height=height)
            )
        header = (
            f"Rendered {len(files)} of {len(pages)} workspace page(s) at {width}x{height} px, {scale}x. "
            "The PNGs are stored by file_id, not attached: read one with read_file_content to look at it.\n\n"
        )
        return ToolResult(tool_call_id="", content=header + "\n\n".join(blocks), is_error=not files)

    @functools.wraps(_render_html_to_image)
    async def _render_counted(*args: object, **kwargs: object) -> ToolResult:
        # A render that writes its PNG to the workspace is a workspace write from its start, as a
        # to_workspace_path call is: a reader of the same tool round waits for the PNG (#318 review).
        workspace = current_workspace() if kwargs.get("png_workspace_path") else None
        writing = getattr(workspace, "writing", None)
        with writing() if callable(writing) else contextlib.nullcontext():
            return await _render_html_to_image(*args, **kwargs)  # type: ignore[arg-type]

    executor = executor if executor is not None else CallableToolExecutor()
    executor.register("render_html_to_image", _render_counted)

    definition = ToolDefinition(
        name="render_html_to_image",
        description=(
            "Render HTML to a PNG figure in ONE call: a headless browser lays it out on a "
            "fixed canvas (default 1920x1080) with the deck fonts, runs the overflow / overlap "
            "probe (OUT: past the canvas; OVF: clipped or a label past its box; OVERLAP: text "
            "or images painted over each other, an SVG label on a node; WRAP: a table cell on "
            "more lines than its column; EDGE-ON-TEXT: an SVG "
            "edge crossing a label; EDGE-END/LANE/BORDER: edge geometry; a figure taller than "
            f"the {MAX_CANVAS_PX} px cap is probed over its whole height) and returns "
            "the PNG file_id, the probe findings and the visual summary. The HTML source is "
            "saved as a sibling .html file: edit it with edit_file_content and re-render with "
            "source_file_id, never from memory. A workspace mockup renders with workspace_path "
            "(read on the server); edit it with sandbox_edit_file and re-render. Scripts do not run; "
            "external URLs do not load - inline data: URIs or file:id / [[FILE:id]] handles "
            "(a logo from fetch_image_asset, a template asset id from get_template). For a team "
            "slide prefer the native profiles block."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "html": {
                    "type": "string",
                    "description": "The HTML to render (a fragment or a full document).",
                },
                "source_file_id": {
                    "type": "string",
                    "description": "The file id of a stored .html source to render instead of html (after an edit_file_content).",
                },
                "width": {
                    "type": "integer",
                    "minimum": MIN_CANVAS_PX,
                    "maximum": MAX_CANVAS_PX,
                    "description": f"Canvas width in CSS px (default {DEFAULT_WIDTH}).",
                },
                "height": {
                    "type": "integer",
                    "minimum": MIN_CANVAS_PX,
                    "maximum": MAX_CANVAS_PX,
                    "description": f"Canvas height in CSS px (default {DEFAULT_HEIGHT}).",
                },
                "scale": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_SCALE,
                    "description": "Device pixels per CSS px (default 1; 2 for a crisp slide picture).",
                },
                "name": {
                    "type": "string",
                    "description": "Base filename for the PNG and its .html source (e.g. 'founders-card').",
                },
                "png_workspace_path": {
                    "type": "string",
                    "description": "Also write the PNG to this workspace path, for code.",
                },
                "pages": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": MAX_BATCH_PAGES,
                    "description": "Instead of html: workspace .html files rendered in one call, each answering its PNG file_id and probe.",
                },
            },
        },
        source="builtin:files",
        # A run with a sandbox workspace gains ``workspace_path`` next to
        # source_file_id: the dispatcher reads the file on the server and hands
        # its bytes to the tool as source_file_id (``with_workspace_files``).
        workspace_files=("source_file_id",),
    )
    return [definition], executor
