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

"""What a recording's pointer acts on when it is not an element: a point of the page or a node of a graph.

A graph drawn on a ``<canvas>`` (a knowledge graph, an attack path)
has no element for its nodes, links, hover cards or lasso: a step that only
finds elements by selector or role drives the toolbar around it, and the
viewer never sees the cursor touch the graph. A pointer step
(``spec.POINTER_ACTIONS``) may name instead:

* **a point** - ``at: {x, y}`` in CSS pixels of the window, or
  ``at: {x, y, in: "<selector>"}`` on that element, a value up to 1 a share of
  its box (:func:`locate_point`): a spot of a canvas that holds whatever the
  window's size;
* **a node** - ``node: {label}``, found by its label with no hook in the
  application filmed (:func:`locate_node`, ``node_locate``): the entry
  of the list the graph keeps for screen readers, its label read on the canvas
  by OCR, else the highlight it takes when that entry is made active. A miss
  names the three closest entries. A page that still exposes the old demo hook
  ``window.__xtmDemoLocate(label)`` (:data:`LOCATE_HOOK`) is read through it
  for one more release, the log saying so. The recorder never reads the
  application's internals.

Either way the target is logged with the box the camera frames
(:attr:`Target.box`): the automatic camera zooms on a node clicked as it does
on an element, and an ``at_step`` overlay points at it. The recorder
(``capture``) glides the cursor there and rests on it, moving, for
:data:`HOVER_MS` before the button (a canvas reads its hover from the pointer's
moves: a right-click on a node it has not hovered opens no menu); a right-click
then reads the menu it opened (:func:`read_menu`). On a node, the card the page
shows while the cursor rests is read first (:func:`hover_check`): one headed
with something else - a relationship's type - sends the cursor to the node's
next candidate place before any button is pressed. Then the menu is
checked (:func:`menu_check`): an application heads it with what it
opened on - the node's title, else "Graph" or a relationship's type -, so a
menu headed otherwise fails the step there, naming its title, the point and
the stage that resolved it. A node the card or the menu of an earlier step
named at a place is tried there first (:func:`remembered`) while the view
has not moved: a highlight read once a selection exists lights its links too.
"""

from __future__ import annotations

import asyncio
import difflib
import time
from dataclasses import dataclass, field, replace
from typing import Any

#: The page hook an application used to expose for the nodes of its graph canvas: read when a page still has it, for
#: one release more, then dropped.
LOCATE_HOOK = "__xtmDemoLocate"
#: What the action log says of a node read through :data:`LOCATE_HOOK`.
HOOK_SAID = "located through the application's hook"
#: Milliseconds the cursor rests on a point or a node before a pointer action, moving over it, unless the step says.
HOVER_MS = 250
#: Seconds between two moves of the cursor while it rests on its target: each is a ``pointermove`` the page reads.
HOVER_MOVE_SECONDS = 0.04
#: CSS pixels the cursor moves by while it rests on its target: under one, never seen on a frame.
HOVER_NUDGE = 0.6
#: The side of the box logged around a point, and around a node whose size the hook does not give, in CSS pixels: what
#: the automatic camera frames.
POINT_BOX = 56.0
#: Seconds between two reads of the hook while a node is looked for, and the most its centre may move between two
#: reads and be where the step acts (a graph's layout may still be settling).
_NODE_POLL_SECONDS = 0.12
_NODE_STILL = 1.0
#: How long a ``right_click`` waits for the menu it opens when its step names no ``timeout``.
MENU_WAIT_SECONDS = 2.0
#: The most other points a ``right_click`` on a node tries when its menu names something else.
MENU_RETRIES = 2
#: The most other points a button on a node tries, before pressing it, when the card the page shows under the
#: cursor names something else (:func:`hover_check`).
HOVER_RETRIES = 6
#: The most seconds those places are tried for: past them, the places left are not tried and the step fails (or presses
#: on a place whose card named the node), so a node whose every place shows another node's card fails in seconds.
HOVER_BUDGET_SECONDS = 3.0
#: The most menu entries logged, and the characters of a section title kept.
_MENU_ITEMS = 40
_MENU_TITLE_CHARS = 120
#: The most labels a hook's answer is read for.
_MAX_LABELS = 500
#: A card the page shows while the cursor rests on a node is read within this many CSS px of the cursor: farther, an
#: element shown meanwhile (a toast, a panel) is not what the cursor is on.
HOVER_NEAR = 360.0
#: The most elements :data:`_SHOWN_JS` reads.
_MOST_SHOWN = 40
#: How many labels a node step that finds none is told about.
NEAREST_LABELS = 3

_LOCATE_JS = """([hook, label]) => {
  const locate = window[hook];
  if (typeof locate !== 'function') return { hook: false };
  let found = null;
  try { found = locate(label); } catch (e) { return { hook: true, error: String((e && e.message) || e) }; }
  if (found && Number.isFinite(found.x) && Number.isFinite(found.y)) {
    const size = (v) => (Number.isFinite(v) && v > 0 ? v : null);
    const r = size(found.r);
    return { hook: true, found: { x: found.x, y: found.y, width: size(found.width) || (r && 2 * r),
      height: size(found.height) || (r && 2 * r) } };
  }
  let labels = [];
  try {
    const all = locate();
    if (Array.isArray(all)) labels = all.filter((x) => typeof x === 'string').slice(0, __MAX__);
  } catch (e) { labels = []; }
  return { hook: true, found: null, labels };
}""".replace("__MAX__", str(_MAX_LABELS))
#: The menu a right-click opened: the last ``role=menu`` shown, its entries' names, its box (CSS px) and its sections'
#: titles in order - the name of each ``role=group`` (``aria-label``, ``aria-labelledby``) and the text it draws
#: outside every entry, separator and hidden part: what an application heads a section with, the element it opened
#: on first. ``null``: none.
_MENU_JS = """(most) => {
  const shown = (el) => { const r = el.getBoundingClientRect(); const cs = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && cs.visibility !== 'hidden' && cs.display !== 'none'; };
  const menus = [...document.querySelectorAll('[role=menu]')].filter(shown);
  if (!menus.length) return null;
  const menu = menus[menus.length - 1];
  const r = menu.getBoundingClientRect();
  const entries = '[role=menuitem], [role=menuitemcheckbox], [role=menuitemradio]';
  const items = [...menu.querySelectorAll(entries)]
    .filter(shown)
    .map((el) => (el.getAttribute('aria-label') || el.innerText || el.textContent || '').trim().split('\\n')[0])
    .filter(Boolean)
    .slice(0, most);
  const flat = (text) => (text || '').replace(/\\s+/g, ' ').trim();
  const titles = [], read = [];
  for (const el of menu.querySelectorAll('*')) {
    if (el.closest(entries) || el.closest('[role=separator], [aria-hidden=true]') || read.some((r) => r.contains(el))) continue;
    if (!shown(el)) continue;
    if (el.getAttribute('role') === 'group') {
      const by = (el.getAttribute('aria-labelledby') || '').split(/\\s+/).map((id) => id && document.getElementById(id))
        .filter(Boolean);
      by.forEach((label) => read.push(label));
      const name = flat(el.getAttribute('aria-label') || by.map((label) => label.textContent).join(' '));
      if (name) titles.push(name);
      continue;
    }
    if ([...el.childNodes].some((node) => node.nodeType === 3 && flat(node.textContent))) {
      read.push(el);
      const text = flat((el.innerText || el.textContent || '').split('\\n')[0]);
      if (text) titles.push(text);
    }
  }
  return { items, box: [r.left, r.top, r.width, r.height], titles: titles.slice(0, 10) };
}"""


#: What the page shows over itself now: each outermost element placed out of the flow (``position`` absolute or fixed),
#: shown, smaller than a quarter of the window, holding no canvas and outside every menu, with text - ``[key, first
#: line of its text, box]`` (CSS px). The card an application shows for what the cursor rests on is one of them.
_SHOWN_JS = """(most) => {
  const flat = (text) => (text || '').replace(/\\s+/g, ' ').trim();
  const placed = [];
  for (const el of document.body.querySelectorAll('*')) {
    if (placed.length >= 4 * most) break;
    const cs = getComputedStyle(el);
    if (cs.position !== 'absolute' && cs.position !== 'fixed') continue;
    if (cs.visibility === 'hidden' || cs.display === 'none' || Number(cs.opacity) === 0) continue;
    const r = el.getBoundingClientRect();
    if (r.width < 8 || r.height < 8 || r.width * r.height > 0.25 * innerWidth * innerHeight) continue;
    if (el.querySelector('canvas') || el.closest('[role=menu]') || (typeof el.id === 'string' && el.id.startsWith('__xtm'))) continue;
    if (placed.some((outer) => outer.contains(el))) continue;
    const line = (el.innerText || '').split('\\n').map(flat).find(Boolean);
    if (!line) continue;
    placed.push(el);
  }
  return placed.slice(0, most).map((el) => {
    const r = el.getBoundingClientRect();
    const line = (el.innerText || '').split('\\n').map(flat).find(Boolean).slice(0, 120);
    const box = [r.left, r.top, r.width, r.height].map((v) => Math.round(v));
    return [el.tagName + '|' + line + '|' + box.join(','), line, box];
  });
}"""


class TargetNotFoundError(LookupError):
    """A point or a node a pointer step could not act on; ``near`` the labels closest to the one it asked for,
    ``picture`` the canvas as the node was looked for on it (a PNG), which the failed take's contact sheet keeps, and
    ``logged`` what the step's action log entry says when it acted before it failed (the node located, the menu
    it opened)."""

    def __init__(
        self,
        message: str,
        near: list[dict] | None = None,
        *,
        picture: bytes | None = None,
        logged: dict | None = None,
    ) -> None:
        super().__init__(message)
        self.near = near or []
        self.picture = picture
        self.logged = logged


@dataclass(frozen=True)
class Target:
    """Where a pointer step acts: the point in CSS pixels of the window, the box the camera frames, and what
    the action log says it was."""

    point: tuple[float, float]
    #: ``{x, y, width, height}`` in CSS pixels of the window.
    box: dict
    #: ``{"node": label}``, ``{"at": {x, y, in?}}``; empty for an element (its box says it).
    logged: dict = field(default_factory=dict)
    #: ``[start, end]`` of the scroll that brought an element into view, else ``None``.
    scrolled: list[float] | None = None
    #: How a node was found (``node_locate``): the stage that found it (``by``: ``ocr``, ``highlight``,
    #: ``hook``), the point it resolved, what each stage read and a line saying it; ``None`` for a point or an
    #: element.
    located: dict | None = None
    #: The titles a node goes by (``node_locate.names_of``): what the menu a right-click opens on it is headed with.
    names: tuple[str, ...] = ()
    #: The canvas shot the node was read on (a PNG) and its box (CSS px of the window): a failure attaches it, the point
    #: marked.
    picture: bytes | None = field(default=None, repr=False, compare=False)
    picture_box: tuple[float, float, float, float] | None = None
    #: The next points a node may stand at, best first (CSS px of the window): where a ``right_click`` whose
    #: menu names something else tries again before it fails (``capture``, :data:`MENU_RETRIES`).
    others: tuple[tuple[float, float], ...] = ()


def point_box(x: float, y: float, viewport: tuple[int, int], side: float = POINT_BOX) -> dict:
    """The box of *side* CSS pixels around ``(x, y)``, kept inside the *viewport*: what the camera frames for a point."""
    width, height = viewport
    side_x, side_y = min(side, float(width)), min(side, float(height))
    left = min(max(x - side_x / 2, 0.0), width - side_x)
    top = min(max(y - side_y / 2, 0.0), height - side_y)
    return {"x": round(left, 1), "y": round(top, 1), "width": round(side_x, 1), "height": round(side_y, 1)}


def placed_in(value: float, start: float, size: float) -> float:
    """A coordinate of a point given on an element whose box starts at *start* and spans *size*: a share of it up to 1,
    else CSS pixels from its start."""
    return start + (value * size if value <= 1.0 else value)


def _inside(x: float, y: float, viewport: tuple[int, int]) -> bool:
    return 0.0 <= x <= viewport[0] and 0.0 <= y <= viewport[1]


def _point_words(point: Any) -> str:
    where = f"({point.x:g}, {point.y:g})"
    return f"{where} in {point.within!r}" if point.within else where


async def locate_point(page: Any, point: Any, *, viewport: tuple[int, int], wait: float) -> Target:
    """Where *point* (``spec.StepPoint``) lands on *page*: in the window, or on the element its ``in`` names once that
    is shown (waited up to *wait* seconds)."""
    logged = {"at": {"x": point.x, "y": point.y, **({"in": point.within} if point.within else {})}}
    if point.within is None:
        x, y = float(point.x), float(point.y)
    else:
        element = page.locator(point.within).first
        try:
            await element.wait_for(state="visible", timeout=wait * 1000)
        except Exception:
            raise TargetNotFoundError(f"no element {point.within!r} shown on the page to place {_point_words(point)} on") from None
        box = await element.bounding_box()
        if not box or box["width"] <= 0 or box["height"] <= 0:
            raise TargetNotFoundError(f"the element {point.within!r} has no size on the page") from None
        x = placed_in(float(point.x), float(box["x"]), float(box["width"]))
        y = placed_in(float(point.y), float(box["y"]), float(box["height"]))
        if x > box["x"] + box["width"] + 0.5 or y > box["y"] + box["height"] + 0.5:
            raise TargetNotFoundError(
                f"{_point_words(point)} lies outside its element ({round(box['width'])}x{round(box['height'])} CSS px): "
                "give a share of it (0-1) or CSS pixels within it"
            )
    if not _inside(x, y, viewport):
        raise TargetNotFoundError(f"{_point_words(point)} lands at ({x:.0f}, {y:.0f}), outside the window")
    return Target((round(x, 1), round(y, 1)), point_box(x, y, viewport), logged)


def closest_labels(asked: str, labels: list[str], *, count: int = NEAREST_LABELS) -> list[dict]:
    """The *count* labels of a graph closest to the one a node step asked for, as ``{role: node, name}``: those sharing
    its words first (``capture.closest_names``), then the most alike - a node step always hears what the graph shows."""
    from app.services.video_studio.capture import closest_names

    entries = [{"role": "node", "name": label} for label in dict.fromkeys(labels) if label]
    found = closest_names(asked, entries, count=count)
    if len(found) < count:
        taken = {entry["name"] for entry in found}
        wanted = asked.casefold()
        rest = sorted(
            (entry for entry in entries if entry["name"] not in taken),
            key=lambda entry: difflib.SequenceMatcher(None, wanted, entry["name"].casefold()).ratio(),
            reverse=True,
        )
        found.extend(rest[: count - len(found)])
    return found


async def _read_node(page: Any, label: str) -> dict:
    try:
        read = await page.evaluate(_LOCATE_JS, [LOCATE_HOOK, label])
    except Exception as exc:  # navigating: read again until the step's wait ends
        return {"hook": None, "error": str(exc)}
    return read if isinstance(read, dict) else {"hook": None}


async def locate_node(page: Any, node: Any, *, viewport: tuple[int, int], wait: float) -> Target:
    """Where the node labelled ``node.label`` stands on *page*: by its label, in the stages of
    ``node_locate.locate`` - or, on a page that still exposes the old demo hook (:data:`LOCATE_HOOK`), through it,
    read until the node stands still between two reads (a graph's layout may still be settling), up to *wait*
    seconds. A node off screen or not found fails the step (:class:`TargetNotFoundError`), a miss naming the closest
    labels."""
    label = node.label
    read = await _read_node(page, label)
    if read.get("hook") is not True:
        from app.services.video_studio import node_locate

        return await node_locate.locate(page, label, viewport=viewport, wait=wait)
    deadline = time.monotonic() + wait
    last: dict | None = None
    while True:
        found = read.get("found") if read.get("hook") else None
        if isinstance(found, dict):
            if last is not None and abs(found["x"] - last["x"]) <= _NODE_STILL and abs(found["y"] - last["y"]) <= _NODE_STILL:
                break
            last = found
        else:
            last = None
        if time.monotonic() >= deadline:
            if isinstance(found, dict):
                break  # still moving at the end of its wait: acted on where it stands now
            if read.get("error"):
                raise TargetNotFoundError(f"the page's window.{LOCATE_HOOK} failed for {label!r}: {str(read['error'])[:200]}")
            raise TargetNotFoundError(f"no node labelled {label!r} on the graph", closest_labels(label, read.get("labels") or []))
        await asyncio.sleep(_NODE_POLL_SECONDS)
        read = await _read_node(page, label)
    x, y = float(found["x"]), float(found["y"])
    if not _inside(x, y, viewport):
        raise TargetNotFoundError(
            f"the node {label!r} stands at ({x:.0f}, {y:.0f}), outside the window: bring it into view first (fit the "
            "graph, or wheel / drag to it)"
        )
    width = float(found.get("width") or POINT_BOX)
    height = float(found.get("height") or POINT_BOX)
    box = {"x": round(x - width / 2, 1), "y": round(y - height / 2, 1), "width": round(width, 1), "height": round(height, 1)}
    x, y = round(x, 1), round(y, 1)
    return Target(
        (x, y),
        box,
        {"node": label},
        located={"by": "hook", "point": {"x": x, "y": y}, "said": HOOK_SAID},
        names=(" ".join(label.split()),),
    )


async def read_shown(page: Any) -> list[tuple[str, str, tuple[float, float, float, float]]]:
    """What the page shows over itself now (:data:`_SHOWN_JS`): ``(key, first line, box)``; none when it will not
    say (navigating)."""
    try:
        raw = await page.evaluate(_SHOWN_JS, _MOST_SHOWN)
    except Exception:  # navigating: nothing read, nothing checked
        return []
    shown = []
    for entry in raw or []:
        if isinstance(entry, list) and len(entry) == 3 and isinstance(entry[2], list) and len(entry[2]) == 4:
            left, top, width, height = (float(v) for v in entry[2])
            shown.append((str(entry[0]), str(entry[1]), (left, top, width, height)))
    return shown


def _near(box: tuple[float, float, float, float], point: tuple[float, float]) -> bool:
    left, top, width, height = box
    dx = max(left - point[0], 0.0, point[0] - (left + width))
    dy = max(top - point[1], 0.0, point[1] - (top + height))
    return (dx * dx + dy * dy) ** 0.5 <= HOVER_NEAR


def hover_check(
    before: list, after: list, point: tuple[float, float], names: tuple[str, ...], stale: str | None = None
) -> dict | None:
    """What the card the page showed while the cursor rested at *point* says of a node called by *names*: the
    elements shown *after* the rest and not *before* it (:func:`read_shown`), near the cursor (:data:`HOVER_NEAR`),
    headed by their first line - an application heads its hover card with what the cursor is on, the node's title or a
    relationship's type. ``{title, node, key}``: ``node`` whether a card names the node; ``None`` when no card showed
    (an application without one: the step goes on unchecked) or only *stale*, the card read at the place before."""
    had = {entry[0] for entry in before}
    cards = [entry for entry in after if entry[0] not in had and entry[0] != stale and _near(entry[2], point)]
    if not cards or not names:
        return None
    for key, title, _box in cards:
        if any(_titles_node(title, name) for name in names):
            return {"title": title, "node": True, "key": key}
    return {"title": cards[0][1], "node": False, "key": cards[0][0]}


async def read_menu(page: Any, wait: float) -> dict | None:
    """The menu a right-click opened (``role=menu``), read up to *wait* seconds: ``{items, box}`` in CSS pixels, or
    ``None`` when none showed."""
    deadline = time.monotonic() + wait
    while True:
        try:
            menu = await page.evaluate(_MENU_JS, _MENU_ITEMS)
        except Exception:  # navigating: no menu read
            menu = None
        if isinstance(menu, dict) and isinstance(menu.get("box"), list) and len(menu["box"]) == 4:
            left, top, width, height = (round(float(v), 1) for v in menu["box"])
            read = {"items": [str(item)[:120] for item in menu.get("items") or []], "box": {"x": left, "y": top, "w": width, "h": height}}
            titles = [str(title)[:_MENU_TITLE_CHARS] for title in menu.get("titles") or [] if str(title).strip()]
            if titles:
                read["titles"] = titles
            return read
        if time.monotonic() >= deadline:
            return None
        await asyncio.sleep(0.1)


def _flat(text: str) -> str:
    return " ".join(text.split()).casefold()


def _titles_node(title: str, name: str) -> bool:
    """Whether a menu's section *title* names the node called *name*: the same words whatever the case, one of them
    the other with a type before it ("Intrusion Set Storm-1567"), or the title cut short by an ellipsis."""
    title, name = _flat(title), _flat(name)
    if not title or not name:
        return False
    if title == name or title.endswith(" " + name) or name.endswith(" " + title):
        return True
    stem = title.removesuffix("\u2026").removesuffix("...").rstrip()
    return stem != title and len(stem) >= 3 and (name.startswith(stem) or any(part.startswith(stem) for part in name.split(" ")))


def menu_check(menu: dict | None, target: Target) -> tuple[dict | None, str | None]:
    """The check of the menu a ``right_click`` on a node opened: ``(what the action log says of it, why the
    step fails)``. Its sections' titles (:func:`read_menu`) must hold one naming the node (:attr:`Target.names`): one
    that names something else - "Graph" for the empty canvas, a relationship's type - says the point was not on the
    node, and the step fails there, naming the title it read, the point and the stage that resolved it. A menu
    without a title, or none, is not checked: ``(None, None)``."""
    titles = (menu or {}).get("titles") or []
    if not titles or not target.names:
        return None, None
    named = any(_titles_node(title, name) for title in titles for name in target.names)
    checked = {"title": titles[0], "node": named}
    if named:
        return checked, None
    located = target.located or {}
    x, y = target.point
    others = f" (then {', '.join(repr(title) for title in titles[1:3])})" if len(titles) > 1 else ""
    entries = len((menu or {}).get("items") or [])
    return checked, (
        f"the menu it opened is headed {titles[0]!r}{others}, {entries} entries, not the node {target.names[0]!r}: "
        f"the point it resolved by {located.get('by') or 'its locator'}, ({x:.0f}, {y:.0f}), is not on the node"
    )


def node_menu_check(menu: dict | None, target: Target, point: tuple[float, float]) -> tuple[dict | None, str | None]:
    """:func:`menu_check` for a ``right_click`` on a located node, where no menu at all fails the step too: the
    point (*point*, where the button was pressed) was not on the node, or the node offers none - a take never ends
    ready on a right-click that opened nothing."""
    if menu is not None or target.located is None or not target.names:
        return menu_check(menu, target)
    by = target.located.get("by") or "its locator"
    return None, (
        f"no menu opened on the node {target.names[0]!r}: the point it resolved by {by}, "
        f"({point[0]:.0f}, {point[1]:.0f}), is not on the node, or the node offers no menu there"
    )


def hover_missed(tried: list[dict], target: Target) -> str | None:
    """Why a button on a located node is not pressed: the card the page showed under the cursor, at every place the
    node was tried at (``capture._hover_to_node``), named something else (a relationship's type, another node).
    ``None`` when no card was read (an application without one) or one named the node."""
    if not tried or any(entry.get("node") for entry in tried):
        return None
    titles = [str(entry["title"]) for entry in tried if entry.get("title")]
    if not titles:
        return None
    by = (target.located or {}).get("by") or "its locator"
    read = ", ".join(dict.fromkeys(repr(title[:60]) for title in titles))
    return (
        f"the card the page shows under the cursor never names the node {target.names[0]!r} at the "
        f"{len(tried)} place(s) resolved by {by}: it read {read}; nothing was pressed"
    )


#: The stages whose point a place confirmed by an earlier step comes before: a highlight read once a selection
#: exists lights the selected nodes' links with the node, and its point may fall on one of them.
REMEMBERED_BEFORE = frozenset({"highlight"})


def remembered(target: Target, place: tuple[float, float], step: int) -> Target:
    """*target* pressed first at *place*, where the card or the menu of step *step* named the same node (the view not
    moved since: ``capture`` forgets it on a step that may move it), its resolved point kept as its next place. Only for
    a node located by a stage of :data:`REMEMBERED_BEFORE`; the hover check still reads the card there before any
    button, so a node moved meanwhile goes on to the point read now. The action log says so (``remembered``)."""
    located = target.located or {}
    if located.get("by") not in REMEMBERED_BEFORE or place == target.point:
        return target
    x, y = round(place[0], 1), round(place[1], 1)
    others = (target.point, *(other for other in target.others if other != (x, y)))
    said = str(located.get("said") or "")
    return replace(
        target,
        point=(x, y),
        others=others,
        located={
            **located,
            "point": {"x": x, "y": y},
            "remembered": {"x": x, "y": y, "step": step, "resolved": located.get("point")},
            "said": f"{said}; tried first where step {step} found the node named under the cursor",
        },
    )
