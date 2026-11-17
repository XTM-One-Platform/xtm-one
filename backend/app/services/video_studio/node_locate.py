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

"""A node of a graph canvas found by its label, with no hook in the application filmed.

A graph drawn on a ``<canvas>`` keeps no element for its nodes, yet already
shows what a locator needs: the list it keeps for screen readers (a visually
hidden ``role=listbox`` or ``role=tree`` with one entry per node, its text
"type title, N relationships, ...", or ``aria-label`` s on the canvas's own
children), and each node's label painted under its circle. A ``node {label}``
step (:func:`locate`) reads them in three stages, read again before every
pointer step on a node - never kept across a pan, a zoom or a layout change -
and logs the stage that found it (:attr:`pointer.Target.located`):

1. **Identity** (:data:`_MIRROR_JS`, :func:`match_label`). The hidden entries
   of the page's lists, matched on their text before the first comma: the
   label exactly (or as its end: "Intrusion-Set Storm-1567" ends with
   "Storm-1567"), then whatever the case, then as the beginning of it or of
   one of its words (one entry only). The title the canvas paints follows
   from it. A page whose lists mirror no such label fails the step naming the
   three closest entries; a page with no such list goes on with the label as
   its title.
2. **Position** (:func:`canvas_ocr.find_title`). The canvas shot at the
   page's pixel ratio and read by OCR; the box of the title gives the node
   (:func:`node_point`): its circle stands :data:`LABEL_TO_NODE` label heights
   above the label's centre, and the label's height gives the zoom - the box
   the camera frames. A box that moved between two reads (a layout settling)
   is read again. Left out: what the page draws over the canvas, read
   at the shot - a hit test's elements and those it cannot see
   (``pointer-events: none``: a hover card repeating the node's title) -, and
   a label drawn as an edge's (``canvas_ocr.edge_label``: rotated, italic or
   along a line).
3. **Highlight** (:func:`highlight_of`). When OCR finds no box - labels
   overlapping, a dense cluster, labels hidden at a low zoom - the list is
   focused and its active entry moved with ArrowDown / ArrowUp, as a keyboard
   user does, the application highlighting the entry active. The canvas is
   shot with an entry beside the node active, the node's, then the other
   entry beside it: what changed both onto the node and off it is the
   node's highlight - not a node hovered before, nor its neighbours -, the
   node where that change is thickest (not the middle of the links and the
   label it redraws with it), or, when many of its links are lit with it,
   where they converge (:func:`_convergence`: a layered layout's links fill a
   wedge with the far layer thicker than the node), the thick places after.
   The shot of the node is taken only once the list's active entry is the node's, compared by text (positions read from
   ``aria-posinset``: a list mounting a window of its entries). What the page
   draws over the canvas at any of the shots is left out. A change of
   nothing, or of more than :data:`MOST_CHANGED` of the canvas, fails the
   step, the canvas shot attached (:attr:`pointer.TargetNotFoundError.picture`).

The stage, the point it resolved and the titles the node goes by are logged
(:attr:`pointer.Target.located`, :attr:`pointer.Target.names`): a ``right_click``
checks the menu it opened against them (``pointer.menu_check``).

A label neither mirrored in a list nor painted on the canvas cannot be
located, by design: the failure says which of the two is missing.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import math
import time
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from app.core.utils import format_exception_message
from app.services.video_studio import canvas_ocr
from app.services.video_studio.page_settle import NOISE
from app.services.video_studio.pointer import Target, TargetNotFoundError, closest_labels

logger = logging.getLogger(__name__)

#: The node's circle stands this many label heights above the centre of the label painted under it.
LABEL_TO_NODE = 1.2
#: The side of the box the camera frames around a node read by its label, in label heights: the circle, its label beside.
NODE_SIDE = 2 * LABEL_TO_NODE
#: A highlight changing more than this share of the canvas is not one node's (the layout moved, the view panned).
MOST_CHANGED = 0.10
#: Fewer changed pixels than this (at pixel ratio 1) is no highlight.
FEWEST_CHANGED = 12
#: A canvas whose two shots differ on less than this share of their pixels holds still.
STILL_SHARE = 0.001
#: A canvas whose links animate (dashes, particles) never holds :data:`STILL_SHARE` still: past
#: :data:`NEARLY_STILL_AFTER` seconds of looking, two shots differing on less than :data:`NEARLY_STILL_SHARE` of
#: their pixels hold still enough for the highlight stage, and past :data:`HIGHLIGHT_AFTER` seconds it runs whatever
#: the canvas does - not at the step's deadline (30 s of resolution on a production graph). An OCR read that
#: found nothing holds for a canvas nearly still since.
NEARLY_STILL_SHARE = 0.02
NEARLY_STILL_AFTER = 1.0
HIGHLIGHT_AFTER = 3.0
#: CSS pixels the changes of one highlight are joined across (a circle and its outline, a label redrawn under it).
_JOIN_PX = 3
#: Seconds between two reads while a node is looked for, and the CSS pixels it may move between two and be acted on.
_POLL_SECONDS = 0.12
_STILL_PX = 1.0
#: The most entries of the page's lists read, and the most canvases shot (the largest first).
MOST_ENTRIES = 2000
MOST_CANVASES = 3
#: The fewest CSS pixels a side of a canvas read has.
_SMALLEST_CANVAS = 40
#: The most key presses that bring a list's active entry to the node.
MOST_KEYS = 600
#: Seconds the canvas is given to paint its highlight once the entry is active.
_PAINT_SECONDS = 0.15
#: The characters of an entry, and of a list's label, the action log keeps.
_ENTRY_CHARS = 120
_LIST_CHARS = 60
#: The labels read as an edge's the action log names.
_LEFT_OUT_SAID = 5
#: The most boxes drawn over a canvas read, and the most elements of the page scanned for those a hit test misses.
_MOST_OVER = 40
_MOST_SCANNED = 20000

#: ``overOf(box, layers)``: the boxes (CSS px of the window) of what the page draws over a canvas shown in ``box``
#: whose layers are ``layers``: the elements a hit test finds on a grid over it, and those it cannot find - shown with
#: ``pointer-events: none`` (a hover card, a tooltip) - meeting its box; each shown, under half of it (a layer as
#: large as it is part of its drawing), a child of one already kept left in it.
_OVER_FN = """const overOf = (box, layers) => {
  const [left, top, width, height] = box, seen = new Set(), kept = new Set(), over = [];
  const keep = (el) => {
    const b = el.getBoundingClientRect(), cs = getComputedStyle(el);
    if (cs.visibility === 'hidden' || Number(cs.opacity) === 0 || b.width * b.height > 0.5 * width * height) return;
    kept.add(el);
    if (over.length < __MOST_OVER__) over.push([b.left, b.top, b.width, b.height]);
  };
  for (let i = 0; i < 24; i += 1) {
    for (let j = 0; j < 14; j += 1) {
      for (const el of document.elementsFromPoint(left + (i + 0.5) * width / 24, top + (j + 0.5) * height / 14)) {
        if (layers.includes(el)) break;
        if (seen.has(el) || layers.some((layer) => el.contains(layer))) continue;
        seen.add(el);
        keep(el);
      }
    }
  }
  const all = document.body ? document.body.getElementsByTagName('*') : [];
  for (let k = 0, n = Math.min(all.length, __MOST_SCANNED__); k < n && over.length < __MOST_OVER__; k += 1) {
    const el = all[k];
    if (seen.has(el) || layers.includes(el)) continue;
    seen.add(el);
    if (el.parentElement && kept.has(el.parentElement)) { kept.add(el); continue; }
    const b = el.getBoundingClientRect();
    if (b.width <= 1 || b.height <= 1 || b.right <= left || b.bottom <= top || b.left >= left + width || b.top >= top + height) continue;
    if (el.closest('canvas') || layers.some((layer) => el.contains(layer))) continue;
    if (getComputedStyle(el).pointerEvents === 'none') keep(el);
  }
  return over;
};""".replace("__MOST_OVER__", str(_MOST_OVER)).replace("__MOST_SCANNED__", str(_MOST_SCANNED))

#: The page's mirror of its graph and the canvases it shows (CSS px of the window). ``entries``: the visually hidden
#: (``sr-only``: 1 px with overflow hidden, ``clip: rect(0 0 0 0)``, ``clip-path: inset(50%)``) or off-screen
#: ``option`` / ``treeitem`` of each ``listbox`` / ``tree`` - ``pos`` its place in the list (``aria-posinset``, else
#: its index: a list may mount a window of its entries) -, and the labelled children of each canvas; ``canvases``:
#: those shown at least ``_SMALLEST_CANVAS`` px a side, the largest first, each with the boxes of what is drawn over
#: it (``over``, :data:`_OVER_FN`).
_MIRROR_JS = """([most, smallest, canvasesMost]) => {
  __OVER_FN__
  const clipped = (node) => {
    const cs = getComputedStyle(node), r = node.getBoundingClientRect();
    return (r.width <= 1 && r.height <= 1 && cs.overflow === 'hidden')
      || /rect\\(0(px)?,? 0(px)?,? 0(px)?,? 0(px)?\\)/.test(cs.clip || '') || /inset\\(\\s*(50|100)%/.test(cs.clipPath || '');
  };
  const hidden = (el) => {
    for (let node = el, depth = 0; node && node !== document.body && depth < 12; node = node.parentElement, depth += 1) {
      if (clipped(node)) return true;
    }
    const r = el.getBoundingClientRect();
    return r.right <= 0 || r.bottom <= 0 || r.left >= innerWidth || r.top >= innerHeight;
  };
  const said = (el) => (el.getAttribute('aria-label') || el.textContent || '').replace(/\\s+/g, ' ').trim();
  const entries = [];
  let mirrors = 0;
  [...document.querySelectorAll('[role=listbox], [role=tree]')].forEach((list, li) => {
    if (getComputedStyle(list).display === 'none') return;
    const items = [...list.querySelectorAll('[role=option], [role=treeitem]')];
    const all = items.length > 0 && hidden(list);
    let kept = 0;
    const count = Number(items.length && items[0].getAttribute('aria-setsize')) || items.length;
    items.forEach((item, index) => {
      if (entries.length >= most || !(all || hidden(item))) return;
      const text = said(item);
      if (!text) return;
      const at = Number(item.getAttribute('aria-posinset'));
      entries.push({ text: text.slice(0, 300), list: li, index, pos: Number.isInteger(at) && at >= 1 ? at - 1 : index,
        count, name: (list.getAttribute('aria-label') || '').slice(0, 120) });
      kept += 1;
    });
    if (kept) mirrors += 1;
  });
  const canvases = [...document.querySelectorAll('canvas')];
  [...document.querySelectorAll('canvas *')].forEach((child, index) => {
    const text = (child.getAttribute('aria-label') || '').replace(/\\s+/g, ' ').trim();
    if (text && entries.length < most) entries.push({ text: text.slice(0, 300), child: index, count: 1, name: '' });
  });
  const groups = new Map();
  for (const canvas of canvases) {
    const cs = getComputedStyle(canvas);
    if (cs.visibility === 'hidden' || cs.display === 'none' || Number(cs.opacity) === 0) continue;
    const r = canvas.getBoundingClientRect();
    const left = Math.max(r.left, 0), top = Math.max(r.top, 0);
    const right = Math.min(r.right, innerWidth), bottom = Math.min(r.bottom, innerHeight);
    if (right - left < smallest || bottom - top < smallest) continue;
    const key = [left, top, right, bottom].map(Math.round).join(',');
    if (!groups.has(key)) groups.set(key, { box: [left, top, right - left, bottom - top], canvas, layers: [] });
    groups.get(key).layers.push(canvas);
  }
  const shown = [...groups.values()].sort((a, b) => b.box[2] * b.box[3] - a.box[2] * a.box[3]).slice(0, canvasesMost);
  return { mirrors, entries, canvases: shown.map(({ box, layers }) => ({ box, over: overOf(box, layers) })) };
}""".replace("__OVER_FN__", _OVER_FN)
#: What is drawn over the canvas shown in ``box`` now (:data:`_OVER_FN`), read as each shot of it is taken: a hover
#: card opens and moves with the pointer and the list's active entry.
_OVER_JS = """(box) => {
  __OVER_FN__
  const layers = [...document.querySelectorAll('canvas')].filter((canvas) => {
    const r = canvas.getBoundingClientRect();
    const seen = [Math.max(r.left, 0), Math.max(r.top, 0), Math.min(r.right, innerWidth), Math.min(r.bottom, innerHeight)];
    return [box[0], box[1], box[0] + box[2], box[1] + box[3]].every((v, i) => Math.abs(seen[i] - v) <= 1);
  });
  return overOf(box, layers);
}""".replace("__OVER_FN__", _OVER_FN)

#: Focus the entry ``[list, index]`` (or the canvas child ``child``) holding ``text`` lives in, as a keyboard user does:
#: the list itself, else its entry that takes the focus; the element focused before is kept to give it back. Answers
#: ``{focused, active, count}`` (``active``: the active entry, :data:`_ACTIVE_HELPER_JS`; ``count``: the list's size,
#: ``aria-setsize`` else its entries) or ``{changed: true}`` when the entry is no longer there.
_FOCUS_JS = """([list, index, child, text]) => {
  const said = (el) => (el.getAttribute('aria-label') || el.textContent || '').replace(/\\s+/g, ' ').trim();
  const options = { preventScroll: true };
  window.__xtmNodeFocus = document.activeElement;
  if (child !== null && child !== undefined) {
    const el = [...document.querySelectorAll('canvas *')][child];
    if (!el || (el.getAttribute('aria-label') || '').replace(/\\s+/g, ' ').trim() !== text) return { changed: true };
    el.focus(options);
    const focused = document.activeElement === el;
    return { focused, active: focused ? { pos: 0, text } : { pos: -1, text: '' }, count: 1 };
  }
  const box = [...document.querySelectorAll('[role=listbox], [role=tree]')][list];
  const items = box ? [...box.querySelectorAll('[role=option], [role=treeitem]')] : [];
  if (!items[index] || said(items[index]).slice(0, 300) !== text) return { changed: true };
  box.focus(options);
  if (!box.contains(document.activeElement)) {
    const roving = items.find((el) => el.tabIndex >= 0) || items.find((el) => el.getAttribute('aria-selected') === 'true')
      || items[0];
    if (roving) roving.focus(options);
  }
  return { focused: box.contains(document.activeElement), active: window.__xtmNodeActive(box, items),
    count: Number(items[0].getAttribute('aria-setsize')) || items.length };
}"""
#: The active entry of a list (``aria-activedescendant``, else the entry focused, else the one selected): ``{pos,
#: text}``, ``pos`` its place in the list (``aria-posinset``, else its index), -1 for none. Installed with
#: :data:`_FOCUS_JS`, read by :data:`_ACTIVE_JS`.
_ACTIVE_HELPER_JS = """() => {
  window.__xtmNodeActive = (box, items) => {
    const said = (el) => (el.getAttribute('aria-label') || el.textContent || '').replace(/\\s+/g, ' ').trim();
    const id = box.getAttribute('aria-activedescendant');
    const el = (id && items.find((item) => item.id === id))
      || items.find((item) => item === document.activeElement || item.contains(document.activeElement))
      || items.find((item) => item.getAttribute('aria-selected') === 'true' || item.getAttribute('aria-current') === 'true');
    if (!el) return { pos: -1, text: '' };
    const at = Number(el.getAttribute('aria-posinset'));
    return { pos: Number.isInteger(at) && at >= 1 ? at - 1 : items.indexOf(el), text: said(el).slice(0, 300) };
  };
}"""
_ACTIVE_JS = """(list) => {
  const box = [...document.querySelectorAll('[role=listbox], [role=tree]')][list];
  if (!box || !window.__xtmNodeActive) return { pos: -1, text: '' };
  return window.__xtmNodeActive(box, [...box.querySelectorAll('[role=option], [role=treeitem]')]);
}"""
#: Give the focus back to the element that held it before :data:`_FOCUS_JS`, else leave none.
_RESTORE_JS = """() => {
  const before = window.__xtmNodeFocus;
  window.__xtmNodeFocus = null;
  if (before && before !== document.body && before.isConnected && typeof before.focus === 'function') before.focus({ preventScroll: true });
  else if (document.activeElement && typeof document.activeElement.blur === 'function') document.activeElement.blur();
}"""
#: Resolves once the page painted two frames: what a key press drew is on the canvas.
_PAINTED_JS = "() => new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(() => done(true))))"


@dataclass(frozen=True)
class Entry:
    """One entry of a list mirroring a graph for screen readers, or a labelled child of a canvas."""

    text: str
    #: The list it belongs to and its index there (``[role=listbox], [role=tree]`` and their entries, in page order).
    list: int | None = None
    index: int | None = None
    #: A canvas child's index among every canvas's descendants.
    child: int | None = None
    #: The list's size (``aria-setsize``, else its entries).
    count: int = 1
    #: The list's ``aria-label``.
    name: str = ""
    #: Its place in the list (``aria-posinset``, else :attr:`index`): the keys that make it active count from it.
    pos: int | None = None

    @property
    def head(self) -> str:
        """Its text before the first comma: "type title" of "type title, N relationships, ..."."""
        return " ".join(self.text.split(",", 1)[0].split())

    @property
    def place(self) -> int:
        return self.pos if self.pos is not None else self.index or 0


@dataclass(frozen=True)
class Identity:
    """The entry a label matched (:func:`match_label`): how, and the titles the canvas may paint for it, read in order
    (the first anywhere in a label, the others only as a whole label)."""

    entry: Entry
    #: ``exact``, ``case`` or ``prefix``.
    how: str
    titles: tuple[str, ...]
    #: How many entries matched that way (the first is taken).
    among: int = 1

    def logged(self) -> dict:
        logged = {"entry": self.entry.text[:_ENTRY_CHARS], "match": self.how}
        if self.entry.name:
            logged["list"] = self.entry.name[:_LIST_CHARS]
        if self.among > 1:
            logged["among"] = self.among
        return logged


@dataclass(frozen=True)
class Matched:
    """What :func:`match_label` read: the identity, else the entries the label begins (more than one: ambiguous)."""

    identity: Identity | None = None
    ambiguous: list[Entry] = field(default_factory=list)


def _words_from(head: str) -> list[int]:
    """Where each word of *head* starts."""
    return [i for i, ch in enumerate(head) if ch != " " and (i == 0 or head[i - 1] == " ")]


def _suffixes(title: str) -> tuple[str, ...]:
    """*title* without its first words, longest first: "Intrusion-Set Storm-1567" paints "Storm-1567"."""
    return tuple(title[i:] for i in _words_from(title)[1:])


def match_label(label: str, entries: list[Entry]) -> Matched:
    """The entry *label* names: its text before the first comma (:attr:`Entry.head`) is the label, or ends with
    it after a space (the type before the title); then the same whatever the case; then the label begins it or one of
    its words, for one entry only (several make it ambiguous)."""
    wanted = " ".join(label.split())
    if not wanted:
        return Matched()
    folded = wanted.casefold()
    for how in ("exact", "case"):
        hits: list[tuple[Entry, tuple[str, ...]]] = []
        for entry in entries:
            head = entry.head
            seen, asked = (head, wanted) if how == "exact" else (head.casefold(), folded)
            if seen == asked:
                hits.append((entry, (head, *_suffixes(head))))
            elif seen.endswith(" " + asked):
                hits.append((entry, (head[len(head) - len(wanted) :],)))
        if hits:
            entry, titles = hits[0]
            return Matched(Identity(entry, how, titles, len(hits)))
    begun: list[tuple[Entry, str]] = []
    for entry in entries:
        head = entry.head
        start = next((i for i in _words_from(head) if head[i:].casefold().startswith(folded)), None)
        if start is not None:
            begun.append((entry, head[start:]))
    if len(begun) == 1:
        entry, title = begun[0]
        return Matched(Identity(entry, "prefix", (title, *_suffixes(title))))
    return Matched(ambiguous=[entry for entry, _title in begun])


def node_point(box: tuple[float, float, float, float]) -> tuple[tuple[float, float], dict]:
    """The node above the label read in *box* (left, top, width, height, CSS px of the window): its point,
    :data:`LABEL_TO_NODE` label heights above the label's centre, and the box the camera frames - the node, a square
    of :data:`NODE_SIDE` label heights, and its label."""
    left, top, width, height = box
    x, y = left + width / 2, top + height / 2 - LABEL_TO_NODE * height
    side = NODE_SIDE * height
    x0, y0 = min(left, x - side / 2), min(top, y - side / 2)
    x1, y1 = max(left + width, x + side / 2), max(top + height, y + side / 2)
    framed = {"x": round(x0, 1), "y": round(y0, 1), "width": round(x1 - x0, 1), "height": round(y1 - y0, 1)}
    return (round(x, 1), round(y, 1)), framed


@dataclass(frozen=True)
class Highlight:
    """What a node's highlight changed on the canvas (:func:`highlight_of`), in the pixels of its shots."""

    centre: tuple[float, float]
    box: tuple[float, float, float, float]
    share: float
    #: Whether the point stands in a shape drawn like a node (:data:`NODE_ROUNDNESS`): as wide as tall once
    #: the thin links are opened away, never an edge's label box.
    node_shaped: bool = True
    #: The next places the highlight is thick at, best first (pixels of the shots): where a ``right_click`` whose
    #: menu names something else tries again (``capture``).
    others: tuple[tuple[float, float], ...] = ()
    #: About how many lit links converge on the point when it was resolved by them (:func:`_convergence`), 0 when
    #: it is the highlight's thickest place: a node whose links fill into one wedge with a layered layout's far layer.
    links: int = 0
    #: Why no point the lit links converge on was taken (:func:`_convergence`), empty when one was: the action log
    #: names it, so a node step pressed on a thick place says which guard left the links out.
    unconverged: str = ""


#: The thickest places of a highlight weighed as the node, the thinnest kept as a share of the thickest.
_MOST_PEAKS = 6
_PEAK_SHARE = 0.35
#: A place whose shape, its thin links opened away, is at least this share as wide as it is tall (or as tall as
#: wide) is drawn like a node; an edge's label box, wide and low, is not.
NODE_ROUNDNESS = 0.55


def _peaks(filled: np.ndarray, inside: np.ndarray, ratio: float) -> list[tuple[float, float, float, float]]:
    """The thickest places of *filled*: ``(x, y, radius, roundness)`` in pixels of the shots, the thickest
    first - each the middle of a ridge of *inside* (its distance transform), at least :data:`_PEAK_SHARE` of the
    thickest and two CSS px. Its roundness is how near as wide as tall the shape around it is, read on *filled*
    opened by half its radius: a link thinner than that leaves it, an edge's label box stays wide and low."""
    from scipy import ndimage

    top = float(inside.max())
    if top <= 0:
        return []
    floor = max(2.0 * ratio, _PEAK_SHARE * top)
    ridge = (inside >= floor) & (inside >= ndimage.maximum_filter(inside, size=3) - 0.5)
    groups, count = ndimage.label(ridge, structure=np.ones((3, 3), dtype=bool))
    found: list[tuple[float, float, float]] = []
    for index in range(1, count + 1):
        ys, xs = np.nonzero(groups == index)
        radius = float(inside[ys, xs].max())
        best = inside[ys, xs] >= radius - 0.5
        found.append((float(xs[best].mean()), float(ys[best].mean()), radius))
    found.sort(key=lambda peak: -peak[2])
    rows, cols = filled.shape
    out = []
    for x, y, radius in found[:_MOST_PEAKS]:
        reach = max(4, math.ceil(6 * radius))
        x0, x1 = max(0, int(x) - reach), min(cols, int(x) + reach + 1)
        y0, y1 = max(0, int(y) - reach), min(rows, int(y) + reach + 1)
        opened = _opened(filled[y0:y1, x0:x1], max(1, round(radius / 2)))
        out.append((x + 0.5, y + 0.5, radius, _roundness(opened, int(x) - x0, int(y) - y0)))
    return out


#: The fewest links a place must gather (:func:`_convergence`) to be taken for the node they converge on.
_CONVERGE_LINKS = 5
#: A run of thin link pixels is fitted as a straight line when it is at least this long (CSS px) and at most this
#: thick across (the standard deviation of its pixels across it, CSS px): two links crossing, a curve, a blot are not.
_SEGMENT_PX = 20
_SEGMENT_ACROSS = 1.5
#: The farthest (CSS px) the point the links meet may stand from what the highlight changed: the node glyph they
#: reach the rim of, unchanged by a highlight that only lights the links, is at most this wide a radius.
_GLYPH_REACH = 28


#: How many times a run that is not straight is cut again, each time twice as far from the thick parts
#: (:func:`_segments`): links that fan out of a hub touch near it and join into one comb until cut far enough.
_RECUT_ROUNDS = 4


def _segments(crop: np.ndarray, ratio: float) -> list[tuple[float, float, float, float, float]]:
    """The straight runs of thin link pixels of *crop*, ``(x, y, ux, uy, length)``: the middle, the direction and the
    length of each, in pixels of the crop. What is thick (a glyph, a label box, the wedge links fill near a hub) is left
    out first, which cuts every link into runs between the label boxes it passes. A run that is not straight (links
    with no label box, fanning out of a hub, touch near it and join) is cut again farther from the thick parts, up to
    :data:`_RECUT_ROUNDS` times, until its links come apart."""
    from scipy import ndimage

    depth = ndimage.distance_transform_edt(crop)
    thick = depth > 2.5 * ratio
    far = ndimage.distance_transform_edt(~thick) if thick.any() else None
    if thick.any():
        thick = ndimage.binary_dilation(thick, iterations=max(1, round(3 * ratio)))
    mask = crop & ~thick
    found = []
    for attempt in range(_RECUT_ROUNDS + 1):
        groups, _count = ndimage.label(mask, structure=np.ones((3, 3), dtype=bool))
        bent = np.zeros_like(mask)
        for index, where in enumerate(ndimage.find_objects(groups), start=1):
            if where is None:
                continue
            pixels = groups[where] == index
            ys, xs = np.nonzero(pixels)
            if xs.size < 0.75 * _SEGMENT_PX * ratio:
                continue
            xs = xs.astype(np.float64) + where[1].start + 0.5
            ys = ys.astype(np.float64) + where[0].start + 0.5
            cx, cy = float(xs.mean()), float(ys.mean())
            across, along, ux, uy = principal_axes(xs - cx, ys - cy)
            if along <= 0:
                continue
            if math.sqrt(max(across, 0.0)) > _SEGMENT_ACROSS * ratio:
                bent[where] |= pixels
                continue
            length = 2.0 * math.sqrt(3.0 * along)
            if length >= _SEGMENT_PX * ratio:
                found.append((cx, cy, ux, uy, length))
        if far is None or attempt == _RECUT_ROUNDS or not bent.any():
            break
        mask = bent & (far > 3.0 * ratio * 2 ** (attempt + 1))
    return found


def principal_axes(dx: np.ndarray, dy: np.ndarray) -> tuple[float, float, float, float]:
    """``(variance across, variance along, ux, uy)`` of the points ``(dx, dy)`` (offsets from their mean): the
    eigenvalues of their sample covariance (``np.cov``, N - 1), smaller first, and the unit direction of the larger.

    Closed form on three sums, never ``np.cov`` and ``np.linalg.eigh``: ``np.cov`` multiplies through BLAS, whose
    thread pool is sized to the node's cores, not the container's quota; woken on a run of many pixels during a take,
    it spends the CPU quota of a whole scheduling period in a few milliseconds and the recording's browser waits out
    the period (#11382: 1.7 s held back by a 16-CPU quota at 2.0 CPUs busy, on the node-locate step)."""
    count = int(dx.size)
    if count < 2:
        return 0.0, 0.0, 1.0, 0.0
    scale = 1.0 / (count - 1)
    a = float((dx * dx).sum()) * scale
    b = float((dx * dy).sum()) * scale
    c = float((dy * dy).sum()) * scale
    middle, half = (a + c) / 2.0, math.hypot((a - c) / 2.0, b)
    larger = middle + half
    if b != 0.0:
        ux, uy = larger - c, b
    else:
        ux, uy = (1.0, 0.0) if a >= c else (0.0, 1.0)
    norm = math.hypot(ux, uy) or 1.0
    return middle - half, larger, ux / norm, uy / norm


def _convergence(changed: np.ndarray, ratio: float) -> tuple[float, float, int] | str:
    """The point the lit links of a highlight converge on, ``(x, y, links)`` in pixels of the shots, or why there is
    none. Every relationship a node lights with it is a straight link with the node at one end: each straight run of
    thin link pixels (:func:`_segments`) is a line, and the point most lines pass through - their crossings voted on a
    grid, then refined by a least squares that weighs down the lines passing far from it - is the node, wherever the
    filled highlight is thickest and even inside a glyph the highlight leaves unchanged. ``links``: how many lines pass
    within two CSS px of it."""
    from scipy import ndimage

    ys, xs = np.nonzero(changed)
    if not xs.size:
        return "nothing changed"
    rows, cols = changed.shape
    margin = math.ceil(_GLYPH_REACH * ratio)
    x0, y0 = max(0, int(xs.min()) - margin), max(0, int(ys.min()) - margin)
    x1, y1 = min(cols, int(xs.max()) + margin + 1), min(rows, int(ys.max()) + margin + 1)
    crop = changed[y0:y1, x0:x1]
    lines = _segments(crop, ratio)
    if len(lines) < _CONVERGE_LINKS:
        return f"{len(lines)} straight link(s) lit, under {_CONVERGE_LINKS}"
    cx, cy, ux, uy, weight = (np.array(column) for column in zip(*lines, strict=True))
    nx, ny = -uy, ux
    height, width = crop.shape
    cell = max(1, round(2 * ratio))
    grid = (height // cell + 1, width // cell + 1)
    reach = math.hypot(height, width)
    steps = np.arange(-reach, reach + 1, cell / 2, dtype=np.float64)
    px = cx[:, None] + ux[:, None] * steps
    py = cy[:, None] + uy[:, None] * steps
    inside = (px >= 0) & (px < width) & (py >= 0) & (py < height)
    flat = (py // cell).astype(np.int64) * grid[1] + (px // cell).astype(np.int64)
    votes = np.zeros(grid[0] * grid[1])
    for row in range(len(lines)):
        # One vote per line and cell, whatever its length: the crossings count, not the pixels.
        np.add.at(votes, np.unique(flat[row][inside[row]]), 1.0)
    votes = ndimage.gaussian_filter(votes.reshape(grid), sigma=1.0)
    peak = np.unravel_index(int(np.argmax(votes)), grid)
    qx, qy = (peak[1] + 0.5) * cell, (peak[0] + 0.5) * cell
    scale = 2.0 * ratio
    dot = nx * cx + ny * cy
    for _ in range(30):
        off = nx * qx + ny * qy - dot
        trust = weight / (1.0 + (off / scale) ** 2)
        a, b, c = float((trust * nx * nx).sum()), float((trust * nx * ny).sum()), float((trust * ny * ny).sum())
        det = a * c - b * b
        if det <= 1e-9 * max(1.0, a * c):
            break
        rx, ry = float((trust * nx * dot).sum()), float((trust * ny * dot).sum())
        qx, qy = (c * rx - b * ry) / det, (a * ry - b * rx) / det
    links = int(np.count_nonzero(np.abs(nx * qx + ny * qy - dot) <= scale))
    if links < _CONVERGE_LINKS:
        return f"at most {links} of {len(lines)} straight links meet at one point"
    if not (0 <= qx < width and 0 <= qy < height):
        return "the links meet outside the canvas"
    if float(ndimage.distance_transform_edt(~crop)[int(qy), int(qx)]) > _GLYPH_REACH * ratio:
        return "the links meet far from what the highlight changed"
    return qx + x0, qy + y0, links


def _disc_element(radius: int) -> np.ndarray:
    ys, xs = np.mgrid[-radius : radius + 1, -radius : radius + 1]
    return xs * xs + ys * ys <= radius * radius


def _opened(mask: np.ndarray, radius: int) -> np.ndarray:
    """*mask* opened by the disc of *radius* (:func:`_disc_element`), the same pixels as
    ``ndimage.binary_opening(mask, structure=_disc_element(radius))`` (outside *mask* is background), read on two
    exact Euclidean distance transforms: a pixel stays eroded when no background pixel lies within *radius*, and the
    opening is every pixel within *radius* of an eroded one. The structuring element of a thick highlight is a disc
    tens of pixels wide; ``binary_opening`` with it took seconds and over a gigabyte per call, the transforms take
    milliseconds whatever the radius."""
    from scipy import ndimage

    reach = radius * radius
    padded = np.pad(mask.astype(bool), 1)
    eroded = np.rint(ndimage.distance_transform_edt(padded) ** 2) > reach
    if not eroded.any():
        return np.zeros(mask.shape, dtype=bool)
    opened = np.rint(ndimage.distance_transform_edt(~eroded) ** 2) <= reach
    return opened[1:-1, 1:-1]


def _run(line: np.ndarray, at: int) -> int:
    """The length of the run of ``True`` of *line* holding *at* (0 when *at* is not in one)."""
    if not 0 <= at < line.size or not line[at]:
        return 0
    start = at
    while start > 0 and line[start - 1]:
        start -= 1
    end = at
    while end + 1 < line.size and line[end + 1]:
        end += 1
    return end - start + 1


def _roundness(opened: np.ndarray, x: int, y: int) -> float:
    """How near as wide as tall *opened* is through ``(x, y)``: the shorter of its row and column runs over the longer."""
    if not (0 <= y < opened.shape[0] and 0 <= x < opened.shape[1]):
        return 0.0
    across, down = _run(opened[y], x), _run(opened[:, x], y)
    return min(across, down) / max(across, down) if across and down else 0.0


def _changed(before: np.ndarray, after: np.ndarray) -> np.ndarray:
    return np.abs(after.astype(np.int16) - before.astype(np.int16)) > NOISE


def highlight_of(
    before: np.ndarray,
    after: np.ndarray,
    *,
    ratio: float = 1.0,
    also: np.ndarray | None = None,
    masked: list[tuple[float, float, float, float]] | tuple = (),
) -> Highlight | str:
    """The highlight between grey shots of a canvas: the centroid and box of the largest region that changed
    (its parts within :data:`_JOIN_PX` CSS px joined), or why there is none - nothing changed, or more than
    :data:`MOST_CHANGED` of the canvas did.

    With *also*, *after* is the shot with the node's entry active, *before* and *also* those with an entry
    beside it active: what changed both from *before* to *after* and from *after* to *also* - the node's highlight
    going on, then off -, a neighbour's own change left out. What lies in *masked* (boxes of the shots' pixels: what
    the page draws over the canvas) never counts. The node is where what changed is thickest: the point
    farthest inside it, its holes filled - a ring drawn around a node reads as the node -, never the middle of the
    links and the label a node redraws with it, which are thin; the box is all that changed around it. Of its thickest
    places, one drawn like a node (:data:`NODE_ROUNDNESS`) comes before an edge's label box drawn thicker, a
    relationship a layered layout highlights with its node; the others are kept, best first, in
    :attr:`Highlight.others`."""
    from scipy import ndimage

    if before.shape != after.shape or (also is not None and also.shape != after.shape):
        return "the canvas changed size"
    changed = _changed(before, after)
    if also is not None:
        changed &= _changed(after, also)
    rows, cols = changed.shape
    for left, top, width, height in masked:
        x0, y0 = max(0, int(left)), max(0, int(top))
        x1, y1 = min(cols, math.ceil(left + width)), min(rows, math.ceil(top + height))
        if x1 > x0 and y1 > y0:
            changed[y0:y1, x0:x1] = False
    count = int(np.count_nonzero(changed))
    share = count / max(1, changed.size)
    fewest = FEWEST_CHANGED * ratio * ratio
    if count < fewest:
        return "nothing changed on the canvas" + (" outside what the page draws over it" if len(masked) else "")
    if share > MOST_CHANGED:
        return f"{share:.0%} of the canvas changed, more than a node's highlight"
    filled = ndimage.binary_fill_holes(changed)
    inside = ndimage.distance_transform_edt(filled)
    regions, _found = ndimage.label(ndimage.binary_dilation(filled, iterations=max(1, round(_JOIN_PX * ratio))))
    peaks = _peaks(filled, inside, ratio)
    if peaks:
        ranked = sorted(peaks, key=lambda peak: (peak[3] < NODE_ROUNDNESS, -peak[2]))
        centre, shaped = (ranked[0][0], ranked[0][1]), ranked[0][3] >= NODE_ROUNDNESS
        others = tuple((x, y) for x, y, _radius, _round in ranked[1:])
    else:
        cy, cx = np.nonzero(inside >= float(inside.max()) - 0.5)
        centre, shaped, others = (float(cx.mean()) + 0.5, float(cy.mean()) + 0.5), False, ()
    # The point many lit links converge on comes before every thick place of the filled highlight: a layered
    # layout's links, converging on their node, fill with the far layer into a wedge thicker than the node. The
    # places read before stay next, the one they ranked first at their head.
    hub = _convergence(changed, ratio)
    links, unconverged = 0, hub if isinstance(hub, str) else ""
    if not isinstance(hub, str):
        first, links = (hub[0], hub[1]), hub[2]
        kept = [place for place in [centre, *others] if math.dist(place, first) > 2.0 * ratio]
        centre, shaped, others = first, True, tuple(kept)
    at_y = min(changed.shape[0] - 1, int(centre[1]))
    at_x = min(changed.shape[1] - 1, int(centre[0]))
    ys, xs = np.nonzero(changed & (regions == regions[at_y, at_x]))
    if not xs.size:
        ys, xs = np.nonzero(changed)
    left, top = float(xs.min()), float(ys.min())
    box = (left, top, float(xs.max()) - left + 1, float(ys.max()) - top + 1)
    return Highlight(
        centre, box, round(share, 4), node_shaped=shaped, others=others, links=links, unconverged=unconverged
    )


def still(before: np.ndarray | None, after: np.ndarray, share: float = STILL_SHARE) -> bool:
    """Whether two grey shots of a canvas show it holding still: differing on less than *share* of their pixels."""
    if before is None or before.shape != after.shape:
        return False
    changed = np.abs(after.astype(np.int16) - before.astype(np.int16)) > NOISE
    return float(np.count_nonzero(changed)) / max(1, changed.size) < share


def settled_enough(before: np.ndarray | None, after: np.ndarray, looked: float) -> bool:
    """Whether the highlight stage may read the canvas after *looked* seconds of looking for a node: still, nearly
    still past :data:`NEARLY_STILL_AFTER`, or anyhow past :data:`HIGHLIGHT_AFTER`."""
    if looked >= HIGHLIGHT_AFTER or still(before, after):
        return True
    return looked >= NEARLY_STILL_AFTER and still(before, after, NEARLY_STILL_SHARE)


def _grey(picture: bytes) -> np.ndarray:
    from PIL import Image

    with Image.open(io.BytesIO(picture)) as image:
        return np.asarray(image.convert("L"), dtype=np.uint8)


@dataclass
class _Shot:
    """One canvas shot and read: its box (CSS px), what is drawn over it (CSS px, read as it was shot), the pixel ratio
    it was shot at, its picture, its grey pixels."""

    box: tuple[float, float, float, float]
    over: list[tuple[float, float, float, float]]
    picture: bytes
    ratio: float
    grey: np.ndarray
    #: What OCR found there (``None``: nothing; :attr:`unread`: why it could not read it).
    found: canvas_ocr.Found | None = None
    title: str | None = None
    unread: str | None = None
    #: The labels holding the title read as an edge's (``canvas_ocr.edge_label``), and why: ``'Paris' along a line``.
    left_out: list[str] = field(default_factory=list)
    #: The boxes (the shot's pixels) of those left out only for a line running on from both their ends: upright, the
    #: label of a node many links cross (a hub) reads so too - where its node may stand, tried with its highlight.
    crossed: list[tuple[float, float, float, float]] = field(default_factory=list)
    #: The grey pixels OCR last read (this shot's, or the one an unread shot holds the reading of): a canvas drifting
    #: slowly is read again once it moved from there, not from the shot before.
    read_grey: np.ndarray | None = field(default=None, repr=False)

    def css(self, box: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
        """A box of the shot's pixels in CSS px of the window."""
        left, top, width, height = box
        return (
            self.box[0] + left / self.ratio,
            self.box[1] + top / self.ratio,
            width / self.ratio,
            height / self.ratio,
        )

    def pixels(self, box: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
        """A box in CSS px of the window in the shot's pixels."""
        left, top, width, height = box
        return (
            (left - self.box[0]) * self.ratio,
            (top - self.box[1]) * self.ratio,
            width * self.ratio,
            height * self.ratio,
        )


async def _shoot(page: Any, box: tuple[float, float, float, float]) -> bytes:
    """The canvas in *box* (CSS px of the window) as a PNG at the page's pixel ratio. The window is shot whole and cut
    here: a screenshot clipped to the canvas made Chromium paint the screencast at the clip's size
    (deviceWidth 1794 for 1920) every time a node was read, a frame the recorder drops and holds the one before in
    place of - a micro-stall on every hover or idle over a graph."""
    whole = await page.screenshot(scale="device", type="png", animations="allow", caret="initial", timeout=5000)
    viewport = getattr(page, "viewport_size", None)
    width = float(viewport["width"]) if isinstance(viewport, dict) and viewport.get("width") else None
    if width is None:
        return whole
    return await asyncio.to_thread(_cut, whole, box, width)


def _cut(picture: bytes, box: tuple[float, float, float, float], window_width: float) -> bytes:
    """*picture* (the window shot whole, *window_width* CSS px wide) cut to *box* (CSS px of the window), as a PNG."""
    from PIL import Image

    with Image.open(io.BytesIO(picture)) as image:
        ratio = image.width / max(1.0, window_width)
        left, top, width, height = box
        x0, y0 = max(0, round(left * ratio)), max(0, round(top * ratio))
        x1 = min(image.width, max(x0 + 1, round((left + width) * ratio)))
        y1 = min(image.height, max(y0 + 1, round((top + height) * ratio)))
        out = io.BytesIO()
        image.crop((x0, y0, x1, y1)).save(out, "PNG")
    return out.getvalue()


def _covered(shot: _Shot, word: canvas_ocr.Word) -> bool:
    """Whether *word* lies under an element drawn over the canvas: a panel's title is not a node's label."""
    x, y, _w, _h = shot.css((word.left + word.width / 2, word.middle, 0, 0))
    return any(left <= x <= left + width and top <= y <= top + height for left, top, width, height in shot.over)


def _boxes(read: Any) -> list[tuple[float, float, float, float]]:
    return [
        (float(rect[0]), float(rect[1]), float(rect[2]), float(rect[3]))
        for rect in read or []
        if isinstance(rect, list) and len(rect) == 4
    ]


async def _over(page: Any, box: tuple[float, float, float, float]) -> list[tuple[float, float, float, float]]:
    """What the page draws over the canvas in *box* now (:data:`_OVER_JS`); none when the page will not say."""
    try:
        return _boxes(await page.evaluate(_OVER_JS, list(box)))
    except Exception as exc:  # navigating: the shot is read without it
        logger.debug("What is drawn over the canvas not read: %s", format_exception_message(exc))
        return []


async def _seen(page: Any, box: tuple[float, float, float, float], over: list | None = None) -> _Shot:
    """The canvas in *box* shot now, with what is drawn over it as it was shot (and *over*, read before)."""
    picture = await _shoot(page, box)
    drawn = await _over(page, box)
    grey = await asyncio.to_thread(_grey, picture)
    return _Shot(box, [*(over or []), *drawn], picture, grey.shape[1] / max(1.0, box[2]), grey)


def _normalised(text: str) -> str:
    return " ".join(text.split()).casefold()


def _edge_labels(shot: _Shot, words: list[canvas_ocr.Word], titles: tuple[str, ...]) -> list[canvas_ocr.Word]:
    """*words* without the labels holding one of *titles* that are drawn as an edge's
    (``canvas_ocr.edge_label``), each said in :attr:`_Shot.left_out`."""
    wanted = [_normalised(title) for title in titles if title.strip()]
    kept: list[canvas_ocr.Word] = []
    for run in canvas_ocr.labels(words):
        text = " ".join(word.text for word in run)
        if any(title in _normalised(text) for title in wanted):
            box = canvas_ocr.box_of(run)
            why = canvas_ocr.edge_label(shot.grey, box)
            if why is not None:
                shot.left_out.append(f"{text[:40]!r} {why}")
                if why == canvas_ocr.ALONG_A_LINE and canvas_ocr.find_title(run, titles[0]) is not None:
                    shot.crossed.append(box)
                continue
        kept.extend(run)
    return kept


async def _read_canvas(page: Any, canvas: dict, titles: tuple[str, ...], known: _Shot | None = None) -> _Shot:
    """Shoot *canvas* and look for the first of *titles* OCR reads on it (the others only as a whole label): never
    under what is drawn over it, nor in a label drawn as an edge's. A canvas still since *known* (the shot before,
    in which OCR found none of them) is not read again: what OCR said of it holds - one read, not one a poll."""
    shot = await _seen(page, tuple(float(v) for v in canvas["box"]), _boxes(canvas.get("over")))
    if (
        known is not None
        and known.found is None
        and known.box == shot.box
        and still(known.read_grey if known.read_grey is not None else known.grey, shot.grey, NEARLY_STILL_SHARE)
    ):
        shot.unread, shot.left_out, shot.crossed = known.unread, list(known.left_out), list(known.crossed)
        shot.read_grey = known.read_grey if known.read_grey is not None else known.grey
        return shot
    shot.read_grey = shot.grey
    words = await canvas_ocr.read_words(shot.picture, shot.ratio)
    if words is None:
        shot.unread = "no OCR engine on this worker" if not canvas_ocr.available() else "OCR could not read the canvas"
        return shot
    words = _edge_labels(shot, [word for word in words if not _covered(shot, word)], titles)
    for at, title in enumerate(titles):
        found = canvas_ocr.find_title(words, title, whole_only=at > 0)
        if found is not None:
            shot.found, shot.title = found, title
            break
    return shot


async def _mirror(page: Any) -> dict:
    try:
        read = await page.evaluate(_MIRROR_JS, [MOST_ENTRIES, _SMALLEST_CANVAS, MOST_CANVASES])
    except Exception as exc:  # navigating: read again until the step's wait ends
        logger.debug("Graph mirror not read: %s", format_exception_message(exc))
        return {}
    return read if isinstance(read, dict) else {}


def _entries(read: dict) -> list[Entry]:
    out = []
    for raw in read.get("entries") or []:
        if not isinstance(raw, dict) or not isinstance(raw.get("text"), str):
            continue
        out.append(
            Entry(
                raw["text"],
                list=raw.get("list") if isinstance(raw.get("list"), int) else None,
                index=raw.get("index") if isinstance(raw.get("index"), int) else None,
                child=raw.get("child") if isinstance(raw.get("child"), int) else None,
                count=int(raw.get("count") or 1),
                name=str(raw.get("name") or ""),
                pos=raw.get("pos") if isinstance(raw.get("pos"), int) else None,
            )
        )
    return out


def _inside(x: float, y: float, viewport: tuple[int, int]) -> bool:
    return 0.0 <= x <= viewport[0] and 0.0 <= y <= viewport[1]


def _css_box(box: tuple[float, float, float, float]) -> dict:
    return {"x": round(box[0], 1), "y": round(box[1], 1), "width": round(box[2], 1), "height": round(box[3], 1)}


def _list_words(entry: Entry) -> str:
    return f" {entry.name[:_LIST_CHARS]!r}" if entry.name else ""


def _active(read: Any) -> dict:
    """The active entry :data:`_ACTIVE_HELPER_JS` answered, ``{pos, text}``."""
    if not isinstance(read, dict):
        return {"pos": -1, "text": ""}
    pos = read.get("pos")
    return {"pos": pos if isinstance(pos, int) else -1, "text": str(read.get("text") or "")}


async def _move_to(page: Any, entry: Entry, target: int, active: dict) -> tuple[dict, int]:
    """Move the active entry of *entry*'s list to its place *target* with ArrowDown / ArrowUp (two tries): the entry
    active after and the keys pressed."""
    pressed = 0
    for _ in range(2):
        at = active["pos"]
        moves = target + 1 if at < 0 else target - at
        if moves == 0 or pressed + abs(moves) > MOST_KEYS:
            break
        key = "ArrowDown" if moves > 0 else "ArrowUp"
        for _ in range(abs(moves)):
            await page.keyboard.press(key)
        pressed += abs(moves)
        active = _active(await page.evaluate(_ACTIVE_JS, entry.list))
    return active, pressed


def _beside(place: int, count: int) -> tuple[int, int] | None:
    """Two other entries of a list of *count*, one each side of *place* where there are (else the two on its one side),
    the first moved to before the node's, the second after it; ``None`` in a list under three."""
    if count < 3 or not 0 <= place < count:
        return None
    if place == 0:
        return 1, 2
    if place == count - 1:
        return place - 1, place - 2
    return place - 1, place + 1


async def _painted(page: Any, box: tuple[float, float, float, float]) -> _Shot:
    """The canvas in *box* once the page painted what a key press changed."""
    await page.evaluate(_PAINTED_JS)
    await asyncio.sleep(_PAINT_SECONDS)
    return await _seen(page, box)


async def _highlight(page: Any, entry: Entry, before: _Shot) -> tuple[Highlight | str, bytes | None, dict]:
    """Stage 3: focus *entry*'s list and read the highlight its entry gives the node on the canvas: shot with an
    entry beside it active, then its own - once the list's active entry is the node's, by text -, then the other
    entry beside it; in a list of fewer, against *before* (the canvas before the list had focus). ``(highlight or why
    not, the shot with the node's entry active, what the log says of it)``. ``probed_at`` in the log: the seconds
    since the epoch the probe changed the page over (the list focused, its active entry moved, the focus given back),
    so the take holds a frame of before it (``capture_rate.held_flashes``, test ``locate probe``)."""
    logged: dict[str, Any] = {}
    began = time.time()
    try:
        await page.evaluate(_ACTIVE_HELPER_JS)
        focus = await page.evaluate(_FOCUS_JS, [entry.list, entry.index, entry.child, entry.text])
    except Exception as exc:  # navigating
        return f"the list could not be focused ({format_exception_message(exc)[:80]})", None, logged
    if not isinstance(focus, dict) or focus.get("changed"):
        return "the list changed meanwhile", None, logged
    shots: list[_Shot] = []
    try:
        if not focus.get("focused"):
            return "the list takes no keyboard focus", None, logged
        if entry.child is not None:
            node = await _painted(page, before.box)
        else:
            active, keys = _active(focus.get("active")), 0
            sides = _beside(entry.place, int(focus.get("count") or entry.count))
            if sides is not None:
                active, pressed = await _move_to(page, entry, sides[0], active)
                keys += pressed
                if active["pos"] == sides[0]:
                    shots.append(await _painted(page, before.box))
            active, pressed = await _move_to(page, entry, entry.place, active)
            keys += pressed
            logged["keys"] = keys
            if active["text"] != entry.text:
                stopped = active["text"].split(",", 1)[0][:60]
                return (
                    f"the list's active entry stopped on {stopped!r}, not on the node's ({entry.head[:60]!r})"
                    if active["pos"] >= 0
                    else "the list's keys made no entry active"
                ), None, logged
            node = await _painted(page, before.box)
            if shots and sides is not None:
                active, pressed = await _move_to(page, entry, sides[1], active)
                logged["keys"] = keys + pressed
                shots = [shots[0], node, await _painted(page, before.box)] if active["pos"] == sides[1] else []
    except Exception as exc:  # navigating, or the canvas gone meanwhile
        return (
            f"the canvas could not be read after the key presses ({format_exception_message(exc)[:80]})",
            None,
            logged,
        )
    finally:
        with contextlib.suppress(Exception):  # a page navigating gives no focus back
            await page.evaluate(_RESTORE_JS)
        logged["probed_at"] = [began, time.time()]
    read = shots if len(shots) == 3 else [before, node]
    masked = [before.pixels(rect) for rect in dict.fromkeys(rect for shot in read for rect in shot.over)]
    logged["shots"] = len(read)
    if masked:
        logged["masked"] = len(masked)
    # Off the event loop: on a 2x canvas the read takes about a second of one CPU, and run inline it held the loop
    # that receives and acknowledges the recording's screencast frames - the page painted on, the take kept none.
    if len(read) == 3:
        found = await asyncio.to_thread(
            highlight_of, read[0].grey, read[1].grey, ratio=before.ratio, also=read[2].grey, masked=masked
        )
    else:
        found = await asyncio.to_thread(highlight_of, before.grey, node.grey, ratio=before.ratio, masked=masked)
    return found, node.picture, logged


async def locate(page: Any, label: str, *, viewport: tuple[int, int], wait: float) -> Target:
    """Where the node labelled *label* stands on *page*, read in three stages (identity, OCR, highlight) until
    it is found and holds still, up to *wait* seconds; :class:`TargetNotFoundError` names the stage that missed it,
    the canvas shot attached."""
    began = time.monotonic()
    deadline = began + wait
    last: tuple[float, float] | None = None
    held: np.ndarray | None = None
    read: dict = {}
    matched = Matched()
    shot: _Shot | None = None
    while True:
        read = await _mirror(page)
        entries = _entries(read)
        canvases = [c for c in read.get("canvases") or [] if isinstance(c, dict) and isinstance(c.get("box"), list)]
        matched = match_label(label, entries) if entries else Matched()
        identity = matched.identity
        timed_out = time.monotonic() >= deadline
        if canvases and (identity is not None or not entries):
            titles = identity.titles if identity is not None else (" ".join(label.split()),)
            try:
                shot = await _read_canvas(page, canvases[0], titles, known=shot)
                for canvas in canvases[1:]:
                    if shot.found is not None:
                        break
                    other = await _read_canvas(page, canvas, titles)
                    if other.found is not None:
                        shot = other
            except Exception as exc:  # navigating, or a canvas gone meanwhile: read again
                logger.debug("Canvas not shot for node %r: %s", label, format_exception_message(exc))
                shot = None
            if shot is not None and shot.found is not None:
                point, framed = node_point(shot.css(shot.found.box))
                if (last is not None and math.dist(point, last) <= _STILL_PX) or timed_out:
                    return _by_ocr(label, identity, shot, shot.found, point, framed, viewport)
                last = point
            elif shot is not None:
                last = None
                if identity is not None and (settled_enough(held, shot.grey, time.monotonic() - began) or timed_out):
                    return await _by_highlight(page, label, identity, shot, viewport)
                held = shot.grey
        if timed_out:
            picture = shot.picture if shot is not None else None
            if picture is None and canvases:
                with contextlib.suppress(Exception):  # the failure is said without its picture
                    picture = await _shoot(page, tuple(float(v) for v in canvases[0]["box"]))
            raise _missed(label, read, matched, shot, picture)
        await asyncio.sleep(_POLL_SECONDS)


def _by_ocr(
    label: str,
    identity: Identity | None,
    shot: _Shot,
    found: canvas_ocr.Found,
    point: tuple[float, float],
    framed: dict,
    viewport: tuple[int, int],
) -> Target:
    x, y = point
    if not _inside(x, y, viewport):
        raise TargetNotFoundError(
            f"the node {label!r} stands at ({x:.0f}, {y:.0f}), outside the window: bring it into view first (fit the "
            "graph, or wheel / drag to it)",
            picture=shot.picture,
        )
    label_box = shot.css(found.box)
    located: dict[str, Any] = {
        "by": "ocr",
        "point": {"x": x, "y": y},
        "said": (
            f"label read in the list{_list_words(identity.entry)} ({identity.how})"
            if identity
            else "no list mirrors the graph"
        )
        + f", its box read on the canvas by OCR ({found.how})",
        "identity": identity.logged() if identity else None,
        "ocr": {
            "text": found.text[:_ENTRY_CHARS],
            "match": found.how,
            "box": _css_box(label_box),
            "label_px": round(label_box[3], 1),
        },
    }
    if found.count > 1:
        located["ocr"]["among"] = found.count
    if shot.left_out:
        located["ocr"]["left_out"] = shot.left_out[:_LEFT_OUT_SAID]
    return Target(
        point,
        framed,
        {"node": label},
        located=located,
        names=names_of(label, identity),
        picture=shot.picture,
        picture_box=shot.box,
    )


def names_of(label: str, identity: Identity | None) -> tuple[str, ...]:
    """The titles the node labelled *label* goes by: the label, the titles its list entry gives, the entry's head (its
    type before its title) - what an application names it by in the menu it opens on it."""
    names = [label, *(identity.titles if identity else ()), *([identity.entry.head] if identity else [])]
    return tuple(dict.fromkeys(" ".join(name.split()) for name in names if name.strip()))


def _unread(shot: _Shot) -> str:
    if shot.unread:
        return shot.unread
    if shot.left_out:
        return f"no box by OCR but in an edge's label ({', '.join(shot.left_out[:2])})"
    return "no box by OCR"


async def _by_highlight(page: Any, label: str, identity: Identity, shot: _Shot, viewport: tuple[int, int]) -> Target:
    unread = _unread(shot)
    highlight, after, logged = await _highlight(page, identity.entry, shot)
    if isinstance(highlight, str):
        raise TargetNotFoundError(
            f"node not located on the canvas (label read in the list{_list_words(identity.entry)}, {unread}, no "
            f"highlight on focus: {highlight})",
            picture=after or shot.picture,
        )
    x, y, _w, _h = shot.css((*highlight.centre, 0, 0))
    left, top, width, height = shot.css(highlight.box)
    if not _inside(x, y, viewport):
        raise TargetNotFoundError(
            f"the node {label!r} highlights at ({x:.0f}, {y:.0f}), outside the window", picture=after
        )
    x, y = round(x, 1), round(y, 1)
    # The places the node may stand at besides: above its label read upright with links crossing it (a hub's), then
    # the highlight's other thick places - each checked by what the page says under the cursor before the button.
    labelled = [node_point(shot.css(box))[0] for box in shot.crossed]
    others: list[tuple[float, float]] = []
    for cx, cy in [*labelled, *(shot.css((ox, oy, 0, 0))[:2] for ox, oy in highlight.others)]:
        place = (round(cx, 1), round(cy, 1))
        if _inside(*place, viewport) and place != (x, y) and place not in others:
            others.append(place)
    chosen = "links converge" if highlight.links else "thickest place drawn like a node" if highlight.node_shaped else "thickest place"
    if labelled and not highlight.node_shaped and others and others[0] == labelled[0]:
        # No part of the highlight is drawn like a node: its label's node is surer.
        (x, y), others = others[0], [(x, y), *others[1:]]
        chosen = "above its label crossed by links"
    shape = "" if highlight.node_shaped else ", no part of it drawn like a node"
    if highlight.links:
        shape += f", where its {highlight.links} links converge"
    located = {
        "by": "highlight",
        "point": {"x": x, "y": y},
        "said": f"label read in the list{_list_words(identity.entry)} ({identity.how}), {unread}, its highlight on focus{shape}",
        "identity": identity.logged(),
        "ocr": {"missed": unread},
        "highlight": {
            "box": _css_box((left, top, width, height)),
            "share": highlight.share,
            "node_shaped": highlight.node_shaped,
            "chosen": chosen,
            **(
                {"links": highlight.links}
                if highlight.links
                else {"unconverged": highlight.unconverged or "no convergence read"}
            ),
            **({"others": [{"x": ox, "y": oy} for ox, oy in others]} if others else {}),
            **({"labelled": len(labelled)} if labelled else {}),
            **logged,
        },
    }
    return Target(
        (x, y),
        _css_box((left, top, width, height)),
        {"node": label},
        located=located,
        names=names_of(label, identity),
        picture=after,
        picture_box=shot.box,
        others=tuple(others[:MOST_OTHERS]),
    )


#: The most other places a node located by its highlight is tried at (``capture``: checked under the cursor, then by
#: its menu).
MOST_OTHERS = 6


def marked(picture: bytes, box: tuple[float, float, float, float], point: tuple[float, float]) -> bytes:
    """*picture* (a canvas shot of *box*, CSS px of the window) with *point* (CSS px of the window) marked on it: where a
    node step acted, for a failure's contact sheet. *picture* as it was when it cannot be drawn on."""
    from PIL import Image, ImageDraw

    try:
        with Image.open(io.BytesIO(picture)) as image:
            drawn = image.convert("RGB")
    except Exception as exc:  # a picture that does not open is attached as it is
        logger.debug("Canvas shot not marked: %s", format_exception_message(exc))
        return picture
    ratio = drawn.width / max(1.0, box[2])
    x, y = (point[0] - box[0]) * ratio, (point[1] - box[1]) * ratio
    side, width = 14 * ratio, max(2, round(2 * ratio))
    draw = ImageDraw.Draw(drawn)
    for colour, grow in (((0, 0, 0), width), ((255, 0, 200), 0)):
        draw.ellipse((x - side, y - side, x + side, y + side), outline=colour, width=width + grow)
        draw.line((x - 2 * side, y, x - side / 2, y), fill=colour, width=width + grow)
        draw.line((x + side / 2, y, x + 2 * side, y), fill=colour, width=width + grow)
        draw.line((x, y - 2 * side, x, y - side / 2), fill=colour, width=width + grow)
        draw.line((x, y + side / 2, x, y + 2 * side), fill=colour, width=width + grow)
    out = io.BytesIO()
    drawn.save(out, "PNG")
    return out.getvalue()


def _missed(label: str, read: dict, matched: Matched, shot: _Shot | None, picture: bytes | None) -> TargetNotFoundError:
    """Why the node labelled *label* was not found once the step's wait ended, the canvas *picture* attached."""
    entries = _entries(read)
    if entries and matched.identity is None:
        names = sorted({entry.name for entry in entries if entry.name})
        listed = f" {names[0][:_LIST_CHARS]!r}" if len(names) == 1 else ""
        if len(matched.ambiguous) > 1:
            heads = ", ".join(repr(entry.head[:40]) for entry in matched.ambiguous[:3])
            return TargetNotFoundError(
                f"the label {label!r} begins {len(matched.ambiguous)} entries of the list{listed} mirroring the graph "
                f"({heads}): give the whole label",
                [{"role": "node", "name": entry.head} for entry in matched.ambiguous[:3]],
                picture=picture,
            )
        return TargetNotFoundError(
            f"no node labelled {label!r} on the graph: the list{listed} mirroring it for screen readers has no such entry",
            closest_labels(label, [entry.head for entry in entries]),
            picture=picture,
        )
    if not read.get("canvases"):
        return TargetNotFoundError(f"no canvas shown on the page to find the node {label!r} on")
    unread = f" ({_unread(shot)})" if shot is not None and (shot.unread or shot.left_out) else ""
    return TargetNotFoundError(
        f"no node labelled {label!r} on the graph: no list on the page mirrors its nodes for screen readers, and OCR "
        f"read no {label!r} painted on the canvas{unread}",
        picture=picture,
    )
