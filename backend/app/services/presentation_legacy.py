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

"""Upgrade of the retired v1 slide layouts to the current catalog.

The v1 layouts - ``two_columns``, ``comparison``, the v1 ``dashboard``,
``generic`` - were kept next to the block-based v2 "so every stored deck keeps
validating". That kept four Pydantic models, four React components, a raw
HTML field (``rich_text``) with its own sanitizer and a "(v1)" label on every
surface that lists layouts, and it left the platform with two answers to
"which layouts exist": the brand deck advertised eleven, the editor's picker
offered seven. The promise to stored decks is kept here instead, by
CONVERSION: a v1 slide is rewritten as the v2 slide that says the same thing,
deterministically, so nothing downstream ever meets a v1 slide again.

Where it runs
-------------
* On every READ of a stored spec: ``Presentation.spec`` and
  ``PresentationVersion.spec`` are :class:`~app.models.presentation.SpecJSON`
  columns whose ``process_result_value`` calls :func:`upgrade_spec`, so the
  viewer, the studio prompt, the outline in a tool result, the exporter, a
  package and a version preview all see v2 - without touching one of the ~25
  places that read the column.
* On every WRITE: :func:`app.services.presentation_spec.validate_spec` runs
  :func:`upgrade_spec` before validating, so a stored deck re-saved through any
  path (theme change, patch, restore, import, duplicate) is written back as
  v2, an agent that still emits a v1 layout from an old instruction gets the
  upgraded slide, and a v1-era package still imports.

There is deliberately NO data migration: the rules forbid an unbounded
in-transaction backfill, the upgrader is needed at the boundaries anyway (a
package written before the retirement, an agent instruction that names a v1
layout), and a stored row is rewritten on its next intentional save - the same
shape as :data:`~app.services.presentation_spec.LEGACY_THEME_ALIASES`, which
reads the v1 ``filigran`` theme as ``dark`` on every read and write.

What the upgrade keeps and what it drops
----------------------------------------
Every word travels; the ``id``, the speaker ``notes`` and the background image
travel unchanged (a comment anchored to the slide, a link the agent pasted and
the React key all keep working). The ONE thing that does not survive is the
formatting of a ``rich_text`` fragment: v2 has no HTML field, so its words
become plain ``text`` blocks (one per paragraph group, never cut mid-fragment
when the words fit in several blocks) and the report says so. A slide that
carried nothing but a title (a v1 ``generic`` used as a divider) becomes a
``section`` slide; one that carried nothing at all - v1 allowed it - becomes a
``section`` titled with the deck's title, which is the one compatibility
fallback and the only place the upgrade writes a word the author did not.

The mapping, per retired layout:

* ``two_columns`` -> ``content`` with two ``columns`` (per side: ``heading`` ->
  a level-3 ``heading`` block, ``bullets`` -> a ``list``, ``rich_text`` ->
  ``text`` blocks, ``image_file_id`` -> an ``image``); one empty side -> a
  single-flow body.
* ``comparison`` -> ``content`` with a ``comparison`` block when both sides
  carry a label and points, ``section_header`` + ``list`` blocks otherwise;
  the ``verdict`` -> a closing ``text`` block.
* v1 ``dashboard`` -> ``content`` with a ``metrics_grid`` (``value`` /
  ``label`` / ``delta`` / ``trend``) plus the ``rich_text`` words. The id is
  ALIVE again (a KPI tile row over a grid of native chart / table /
  metric panels), so this one is recognised by shape - ``metrics`` and
  ``rich_text`` under the id, no ``tiles`` and no ``panels`` - not by id
  (:func:`is_retired_slide`). A v2 dashboard is not touched here.
* ``generic`` -> ``content`` with ``list`` / ``text`` / ``image`` blocks.

The module is pure (no I/O, no database) and imports the spec module for its
size caps only; the spec module imports it lazily from ``validate_spec``, and
the ORM type decorator lazily from its read hook, so neither creates a cycle.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any

from app.services.presentation_markup import fit_markup, fit_text
from app.services.presentation_spec import (
    MAX_BLOCKS_PER_COLUMN,
    MAX_BLOCKS_PER_SLIDE,
    MAX_GRID_ITEMS,
    MAX_LABEL_CHARS,
    MAX_LIST_ITEMS,
    MAX_MARKUP_CHARS,
    MAX_TEXT_CHARS,
    MAX_TITLE_CHARS,
    fold_title_speaker,
)

__all__ = [
    "RETIRED_LAYOUTS",
    "V1_DASHBOARD_LAYOUT",
    "UpgradeReport",
    "fold_title_speakers",
    "is_retired_layout",
    "is_retired_slide",
    "retired_layout_note",
    "upgrade_report",
    "upgrade_retired_slide",
    "upgrade_spec",
]

#: The v1 layout IDS that no longer exist. Never a model, never a component,
#: never advertised: a slide wearing one is upgraded at the read and write
#: boundaries.
RETIRED_LAYOUTS: tuple[str, ...] = ("two_columns", "comparison", "generic")

#: The v1 ``dashboard`` shares its id with the v2 ``dashboard`` layout
#: (a KPI tile row over a grid of chart / table / metric panels), so the
#: id alone does not say which one a slide is - its SHAPE does. The v1 slide
#: carried ``metrics`` and ``rich_text``; the v2 one carries ``tiles`` and
#: ``panels``. A dashboard slide with neither v2 field is the retired shape
#: and is upgraded like the retired ids (:func:`is_retired_slide`). The rule
#: reads the v2 fields rather than the v1 ones so a stored v1 slide that was a
#: title alone (v1 allowed it) is still recognised and never fails a re-save
#: on fields nobody wrote.
V1_DASHBOARD_LAYOUT = "dashboard"
_V2_DASHBOARD_FIELDS = frozenset({"tiles", "panels"})

#: The title of the section slide an EMPTY v1 slide becomes when the deck has
#: no title to lend it either. The only invented word in the whole upgrade.
FALLBACK_SECTION_TITLE = "Section"

#: The v2 size cap of a KPI tile's value / delta (``MetricItem``).
_MAX_METRIC_VALUE_CHARS = 40
_MAX_METRIC_DELTA_CHARS = 50

_HORIZONTAL_SPACE_RE = re.compile(r"[ \t\r\f\v]+")

#: HTML elements that start a new line of words when the fragment is flattened.
_HTML_BLOCK_TAGS = frozenset(
    {
        "p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6",
        "tr", "table", "blockquote", "pre", "section", "article", "header", "footer",
    }
)
#: HTML elements whose content is code or an embed, never the author's words.
_HTML_SKIPPED_TAGS = frozenset({"script", "style", "template", "iframe", "object", "noscript", "svg", "math"})

#: Fields every layout shares (``_SlideBase``), carried over as they are.
_SHARED_FIELDS: tuple[str, ...] = ("id", "notes", "background_image_file_id")


def is_retired_layout(layout: object) -> bool:
    """Is *layout* an id the catalog no longer has?

    An ID check only: ``dashboard`` is a current layout, so it answers ``False``
    here whatever the slide holds - the layout switch relies on that to accept
    it as a destination. Ask :func:`is_retired_slide` about a SLIDE.
    """
    return isinstance(layout, str) and layout in RETIRED_LAYOUTS


def is_retired_slide(slide: Mapping[str, Any]) -> bool:
    """Is *slide* a v1 slide the upgrader rewrites - a retired id, or the v1
    ``dashboard`` shape (no ``tiles``, no ``panels``)?"""
    layout = slide.get("layout")
    if is_retired_layout(layout):
        return True
    return layout == V1_DASHBOARD_LAYOUT and not (_V2_DASHBOARD_FIELDS & set(slide))


@dataclass(frozen=True, slots=True)
class UpgradeReport:
    """What :func:`upgrade_spec` did to a deck, for the tool result.

    ``upgraded`` lists ``(position, v1 layout, v2 layout)`` per rewritten slide
    (1-based, the way the deck numbers them); ``flattened`` counts the slides
    whose ``rich_text`` formatting was dropped for its words.
    """

    upgraded: tuple[tuple[int, str, str], ...] = ()
    flattened: int = 0

    def __bool__(self) -> bool:
        return bool(self.upgraded)

    def note(self) -> str | None:
        """One sentence for the agent (and the person behind it), or ``None``."""
        if not self.upgraded:
            return None
        layouts = sorted({source for _pos, source, _target in self.upgraded})
        positions = ", ".join(str(pos) for pos, _s, _t in self.upgraded)
        count = len(self.upgraded)
        line = (
            f"Upgraded {count} slide{'s' if count != 1 else ''} (at position{'s' if count != 1 else ''} "
            f"{positions}) from the retired v1 layout{'s' if len(layouts) != 1 else ''} "
            f"{', '.join(layouts)} to the current catalog - author with the current layouts "
            f"only; the retired ones are converted on every save."
        )
        if self.flattened:
            line += (
                f" The formatting of {self.flattened} rich_text fragment"
                f"{'s' if self.flattened != 1 else ''} was dropped: the words are kept as plain "
                "text blocks; re-apply emphasis with inline markup where it matters."
            )
        return line


# ── Reading v1 values ────────────────────────────────────────────────────────


def _str(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _list(value: object) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple)) else []


def _dict(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


class _WordsOfHTML(HTMLParser):
    """The words of an HTML fragment, one line per block-level element.

    A real parser rather than a tag-stripping regex, on purpose: ``html.parser``
    reads an attribute value as an attribute value, so a tag nested inside one
    (``<p srcdoc="<script>x</script>">y</p>``) never reaches the words, and the
    content of ``script`` / ``style`` / an embed is code, not copy - dropped
    whole. Entities are decoded into the words (``&amp;`` -> ``&``).
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skipping = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _HTML_SKIPPED_TAGS:
            self._skipping += 1
        elif tag in _HTML_BLOCK_TAGS:
            self.parts.append("\n")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _HTML_BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _HTML_SKIPPED_TAGS:
            self._skipping = max(0, self._skipping - 1)
        elif tag in _HTML_BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skipping:
            self.parts.append(data)


def _html_to_text(value: object) -> str | None:
    """The words of a ``rich_text`` fragment, formatting gone, one line per block."""
    if not isinstance(value, str) or not value.strip():
        return None
    parser = _WordsOfHTML()
    parser.feed(value)
    parser.close()
    text = _HORIZONTAL_SPACE_RE.sub(" ", "".join(parser.parts))
    lines = [line.strip() for line in text.split("\n")]
    return "\n".join(line for line in lines if line) or None


def _list_items(bullets: object) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for bullet in _list(bullets):
        text = fit_markup(bullet, MAX_TEXT_CHARS)
        if text:
            items.append({"text": text})
    return items[:MAX_LIST_ITEMS]


@dataclass(slots=True)
class _Body:
    """The blocks a v1 body becomes, plus what was lost on the way."""

    blocks: list[dict[str, Any]] = field(default_factory=list)
    flattened: bool = False

    def add(self, block: dict[str, Any] | None) -> None:
        if block is not None:
            self.blocks.append(block)

    def add_list(self, bullets: object) -> None:
        items = _list_items(bullets)
        if items:
            self.blocks.append({"type": "list", "items": items})

    def add_rich_text(self, value: object, *, room: int) -> None:
        """``rich_text`` as plain ``text`` blocks, one per paragraph group.

        The v1 field held up to 4000 characters, a v2 ``text`` block 1500: the
        words are packed line by line into as many blocks as fit in *room*, so
        a long fragment loses its formatting but not its last paragraphs. Only
        a single line longer than a whole block is cut.
        """
        words = _html_to_text(value)
        if not words:
            return
        chunks: list[str] = []
        current = ""
        for line in words.split("\n"):
            candidate = f"{current}\n{line}" if current else line
            if current and len(candidate) > MAX_MARKUP_CHARS:
                chunks.append(current)
                current = line
            else:
                current = candidate
        if current:
            chunks.append(current)
        added = False
        for chunk in chunks[: max(room, 1)]:
            content = fit_markup(chunk, MAX_MARKUP_CHARS)
            if content:
                self.blocks.append({"type": "text", "content": content})
                added = True
        if added:
            self.flattened = True

    def add_image(self, file_id: object) -> None:
        image = _str(file_id)
        if image:
            self.blocks.append({"type": "image", "file_id": image})


def _column_body(side: Mapping[str, Any]) -> _Body:
    """One side of a ``two_columns`` slide as leaf blocks."""
    body = _Body()
    heading = fit_markup(side.get("heading"), MAX_TEXT_CHARS)
    if heading:
        body.add({"type": "heading", "content": heading, "level": 3})
    body.add_list(side.get("bullets"))
    image = _str(side.get("image_file_id"))
    # The heading, the list and the image leave the rest of the column to the words.
    body.add_rich_text(side.get("rich_text"), room=MAX_BLOCKS_PER_COLUMN - len(body.blocks) - (1 if image else 0))
    body.add_image(image)
    return body


# ── Building the v2 slide ────────────────────────────────────────────────────


def _two_columns(slide: Mapping[str, Any], out: dict[str, Any]) -> bool:
    left = _column_body(_dict(slide.get("left")))
    right = _column_body(_dict(slide.get("right")))
    columns = [body.blocks for body in (left, right) if body.blocks]
    flattened = left.flattened or right.flattened
    if len(columns) == 2:
        out["columns"] = columns
    elif columns:
        # One empty side: the words flow in a single column instead.
        out["blocks"] = columns[0][:MAX_BLOCKS_PER_SLIDE]
    return flattened


def _comparison(slide: Mapping[str, Any], out: dict[str, Any]) -> bool:
    sides: list[tuple[str | None, list[dict[str, Any]]]] = []
    for side_key in ("left", "right"):
        side = _dict(slide.get(side_key))
        sides.append((fit_markup(side.get("label"), MAX_TEXT_CHARS), _list_items(side.get("points"))))
    blocks: list[dict[str, Any]] = []
    if all(label and items for label, items in sides):
        blocks.append(
            {
                "type": "comparison",
                "left": {"label": sides[0][0], "blocks": [{"type": "list", "items": sides[0][1]}]},
                "right": {"label": sides[1][0], "blocks": [{"type": "list", "items": sides[1][1]}]},
            }
        )
    else:
        for label, items in sides:
            if label:
                blocks.append({"type": "section_header", "title": label})
            if items:
                blocks.append({"type": "list", "items": items})
    verdict = fit_markup(slide.get("verdict"), MAX_MARKUP_CHARS)
    if verdict:
        blocks.append({"type": "text", "content": verdict})
    if blocks:
        out["blocks"] = blocks[:MAX_BLOCKS_PER_SLIDE]
    return False


def _dashboard(slide: Mapping[str, Any], out: dict[str, Any]) -> bool:
    tiles: list[dict[str, Any]] = []
    for metric in (_dict(item) for item in _list(slide.get("metrics"))):
        value = fit_text(metric.get("value"), _MAX_METRIC_VALUE_CHARS)
        label = fit_markup(metric.get("label"), MAX_LABEL_CHARS)
        if not value or not label:
            continue
        tile: dict[str, Any] = {"value": value, "label": label}
        delta = fit_text(metric.get("delta"), _MAX_METRIC_DELTA_CHARS)
        if delta:
            tile["delta"] = delta
        if metric.get("trend") in ("up", "down", "flat"):
            tile["trend"] = metric["trend"]
        tiles.append(tile)
    body = _Body()
    if tiles:
        body.add({"type": "metrics_grid", "items": tiles[:MAX_GRID_ITEMS]})
    body.add_rich_text(slide.get("rich_text"), room=MAX_BLOCKS_PER_SLIDE - len(body.blocks))
    if body.blocks:
        out["blocks"] = body.blocks[:MAX_BLOCKS_PER_SLIDE]
    return body.flattened


def _generic(slide: Mapping[str, Any], out: dict[str, Any]) -> bool:
    body = _Body()
    body.add_list(slide.get("bullets"))
    image = _str(slide.get("image_file_id"))
    body.add_rich_text(slide.get("rich_text"), room=MAX_BLOCKS_PER_SLIDE - len(body.blocks) - (1 if image else 0))
    body.add_image(image)
    if body.blocks:
        out["blocks"] = body.blocks[:MAX_BLOCKS_PER_SLIDE]
    return body.flattened


_BUILDERS = {
    "two_columns": _two_columns,
    "comparison": _comparison,
    "dashboard": _dashboard,
    "generic": _generic,
}


def upgrade_retired_slide(slide: Mapping[str, Any], *, deck_title: str | None = None) -> tuple[dict[str, Any], bool]:
    """Rewrite one v1 slide as the v2 slide that says the same thing.

    Returns ``(slide, flattened)``: the upgraded slide - a ``content`` slide,
    or a ``section`` slide when the body is empty - and whether a
    ``rich_text`` fragment lost its formatting on the way. A slide of a
    current layout (a v2 ``dashboard`` included) is returned as a plain copy,
    untouched, so callers can run this over every slide without checking first.
    """
    if not is_retired_slide(slide):
        return dict(slide), False
    layout = slide.get("layout")

    out: dict[str, Any] = {"layout": "content"}
    for key in _SHARED_FIELDS:
        value = slide.get(key)
        if value is not None and value != "":
            out[key] = value
    title = fit_markup(slide.get("title"), MAX_TITLE_CHARS)
    if title:
        out["title"] = title
    flattened = _BUILDERS[str(layout)](slide, out)

    if "blocks" not in out and "columns" not in out:
        # Nothing to draw: the slide was a divider (a title alone) or empty. A
        # content slide needs a body, a section slide needs a title.
        out["layout"] = "section"
        out["title"] = title or fit_markup(deck_title, MAX_TITLE_CHARS) or FALLBACK_SECTION_TITLE
    return out, flattened


def upgrade_spec(spec: Any) -> Any:
    """Upgrade every retired slide of *spec*; return *spec* itself when there is none.

    Copy-on-write: a deck with no v1 slide - every deck written after the
    retirement - comes back as the SAME object, so the read hook costs one scan
    of the layouts and no allocation on the common path. Anything that is not
    a deck-shaped mapping is returned as is: this runs on a column read, and a
    read must never raise.
    """
    if not isinstance(spec, Mapping):
        return spec
    slides = spec.get("slides")
    if not isinstance(slides, list) or not any(
        isinstance(slide, Mapping) and is_retired_slide(slide) for slide in slides
    ):
        return spec
    deck_title = spec.get("title") if isinstance(spec.get("title"), str) else None
    upgraded = dict(spec)
    upgraded["slides"] = [
        upgrade_retired_slide(slide, deck_title=deck_title)[0] if isinstance(slide, Mapping) else slide
        for slide in slides
    ]
    return upgraded


def _logo_grid_has_labels(block: Mapping[str, Any]) -> bool:
    return any(isinstance(item, Mapping) and _str(item.get("label")) for item in _list(block.get("items")))


def _keep_logo_labels(node: Any) -> tuple[Any, bool]:
    if isinstance(node, Mapping):
        out: dict[str, Any] | None = None
        if node.get("type") == "logo_grid" and "show_labels" not in node and _logo_grid_has_labels(node):
            out = dict(node)
            out["show_labels"] = True
        for key, value in node.items():
            new_value, changed = _keep_logo_labels(value)
            if changed:
                out = dict(node) if out is None else out
                out[key] = new_value
        return (out, True) if out is not None else (node, False)
    if isinstance(node, list):
        items: list[Any] | None = None
        for index, value in enumerate(node):
            new_value, changed = _keep_logo_labels(value)
            if changed:
                items = list(node) if items is None else items
                items[index] = new_value
        return (items, True) if items is not None else (node, False)
    return node, False


def keep_stored_logo_labels(spec: Any) -> Any:
    """A STORED ``logo_grid`` whose items carry labels keeps drawing them.

    Labels are drawn only with ``show_labels`` (default false, so
    a name an agent writes as the logo's alternative text never grows a row
    by accident). A grid stored before the field has no ``show_labels`` key
    at all - every save since writes it - and it drew every label it had, so
    reading one reads the switch as on. READ paths only (the ``spec`` column
    hook, a package written before the field): on the write path an absent key
    is an agent's new block and means the default. Copy-on-write, never raises.
    """
    fixed, changed = _keep_logo_labels(spec)
    return fixed if changed else spec


def fold_title_speakers(spec: Any) -> Any:
    """A STORED title slide's one-speaker form read as its ``speakers`` list.

    The write side folds through the model (``TitleSlide``,
    ``fold_title_speaker``); this is the read side, for the decks stored
    before ``speakers`` existed - the editor, the agent and the exporter all
    meet one shape. Copy-on-write, never raises.
    """
    if not isinstance(spec, Mapping) or not isinstance(spec.get("slides"), list):
        return spec
    slides = spec["slides"]
    folded = [fold_title_speaker(slide) for slide in slides]
    if all(new is old for new, old in zip(folded, slides, strict=True)):
        return spec
    out = dict(spec)
    out["slides"] = folded
    return out


def upgrade_report(spec: Any) -> UpgradeReport:
    """What :func:`upgrade_spec` would do to *spec*, for the tool result."""
    if not isinstance(spec, Mapping) or not isinstance(spec.get("slides"), list):
        return UpgradeReport()
    deck_title = spec.get("title") if isinstance(spec.get("title"), str) else None
    upgraded: list[tuple[int, str, str]] = []
    flattened = 0
    for position, slide in enumerate(spec["slides"], start=1):
        if not isinstance(slide, Mapping) or not is_retired_slide(slide):
            continue
        new_slide, lost = upgrade_retired_slide(slide, deck_title=deck_title)
        upgraded.append((position, str(slide["layout"]), str(new_slide["layout"])))
        flattened += int(lost)
    return UpgradeReport(upgraded=tuple(upgraded), flattened=flattened)


def retired_layout_note(spec: Any) -> str | None:
    """The :meth:`UpgradeReport.note` for *spec*, or ``None`` when nothing was retired."""
    return upgrade_report(spec).note()
