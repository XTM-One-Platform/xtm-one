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

"""Move one slide to another layout, deterministically.

A layout is not a string to flip. Every layout has its own shape - an agenda
is 2-10 items, a roadmap 2-4 phases, a content slide 1+ blocks, a title slide
a headline with a speaker line - so "make this content slide a section
divider" is a CONVERSION: what the target has room for is carried over, what
it needs and the source does not spell out is derived from what the source
does say (a title from the first heading, agenda items from the bullets of a
list, the default title the layout carries), and what the target cannot hold
is dropped and SAID. The result is re-validated through the target layout's
own Pydantic model (:func:`presentation_spec.slide_model`), so a converted
slide is a slide ``validate_spec`` accepts, or the move is refused.

This is code, not prompt: the editor panel's layout picker calls
``POST /presentations/{id}/slide-layout`` with the slide as it holds it and
lands the converted slide in its draft; the save is the existing PATCH. No
model is asked and nothing is invented - a slide whose content cannot meet the
target's minimum (a title slide moved to an agenda has no items to list) is
refused with the reason, never padded with placeholder text, because a
placeholder on a slide the person did not write is worse than a refusal they
can act on.

The retired v1 layouts are neither a source nor a destination here: a
slide the panel still holds in a v1 shape (a draft opened before the deck was
re-read) is first upgraded by ``app.services.presentation_legacy`` to the v2
slide that says the same thing, and THAT slide is what is moved - so this
module converts between the eight layouts of the catalog and nothing else.

A ``dashboard`` is figures: its tiles are metrics and its panels hold
a chart, a table or a metric. Moved to ``content`` it becomes a metrics grid
over its panels' blocks (a panel title becomes a heading); built FROM another
slide it takes the metrics of the body as tiles and the charts and tables as
panels, and is refused when the body has no figure to show - a dashboard with
no chart is not a dashboard, and no placeholder figure is invented.

The account (``kept`` / ``derived`` / ``dropped``) is written for a person:
the panel shows it in a toast so they know what to look at on the slide.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from app.services.presentation_legacy import is_retired_layout, is_retired_slide, upgrade_retired_slide
from app.services.presentation_markup import fit_markup, fit_text
from app.services.presentation_spec import (
    AGENDA_GRID_COLUMNS,
    AGENDA_GRID_MAX_ITEMS,
    AGENDA_GRID_MIN_ITEMS,
    FILL_BLOCK_TYPES,
    MAX_AGENDA_ITEMS,
    MAX_BLOCKS_PER_SLIDE,
    MAX_CARD_ITEMS,
    MAX_COVER_SPEAKERS,
    MAX_DASHBOARD_PANELS,
    MAX_DASHBOARD_TILES,
    MAX_GRID_ITEMS,
    MAX_KEY_VALUE_ITEMS,
    MAX_LABEL_CHARS,
    MAX_LIST_ITEMS,
    MAX_MARKUP_CHARS,
    MAX_ROADMAP_PHASES,
    MAX_TAKEAWAYS,
    MAX_TEXT_CHARS,
    MAX_TITLE_CHARS,
    MIN_DASHBOARD_TILES,
    PANEL_BLOCK_TYPES,
    SLIDE_LAYOUTS,
    MetricItem,
    auto_agenda_columns,
    auto_agenda_style,
    fold_title_speaker,
    quote_credit,
    slide_model,
)

__all__ = [
    "LayoutSwitch",
    "LayoutSwitchError",
    "layout_label",
    "switch_slide_layout",
]

#: How the account names a layout - the words the editor panel uses.
_LAYOUT_LABELS: dict[str, str] = {
    "title": "title",
    "section": "section",
    "agenda": "agenda",
    "content": "content",
    "roadmap": "roadmap",
    "takeaways": "takeaways",
    "dashboard": "dashboard",
    "closing": "closing",
}


def _model_default_titles() -> dict[str, str]:
    """The title a layout carries when the source names none.

    Read off the models themselves (``AgendaSlide.title = "Agenda"``...) so
    the "was this title the person's or the layout's?" check can never drift
    from what the slide actually showed; plus one for the roadmap, whose title
    is required and has no default.
    """
    defaults: dict[str, str] = {}
    for layout in SLIDE_LAYOUTS:
        model = slide_model(layout)
        title_field = model.model_fields.get("title") if model is not None else None
        default = title_field.default if title_field is not None else None
        if isinstance(default, str) and default:
            defaults[layout] = default
    defaults.setdefault("roadmap", "Roadmap")
    return defaults


_DEFAULT_TITLES: dict[str, str] = _model_default_titles()

#: A derived title is a short line: a paragraph is not made a title, the move
#: is refused instead.
_MAX_DERIVED_TITLE_CHARS = MAX_LABEL_CHARS

#: Blocks that carry no words to list (they are pictures - a chart is a figure,
#: its numbers are not bullets): dropped, with a note, when the target lists
#: items instead of drawing blocks.
_VISUAL_BLOCKS: frozenset[str] = frozenset(
    {"image", "video", "logo_grid", "logo_wall", "diagram", "architecture", "org_chart", "chart", "spacer", "divider"}
)
#: What a dashboard PANEL holds; a metric found in a body is a tile first.
_PANEL_FIGURES: frozenset[str] = frozenset(PANEL_BLOCK_TYPES) - {"metric", "metrics_grid"}

#: A contact kind, as a spec-sheet label.
_CONTACT_LABELS: dict[str, str] = {
    "email": "Email",
    "web": "Web",
    "linkedin": "LinkedIn",
    "x": "X",
    "github": "GitHub",
    "phone": "Phone",
}

#: A marker already in a string that is about to be wrapped in ``**``.
_STRAY_MARKER_RE = re.compile(r"\*\*|`|\[\[|\]\]")


class LayoutSwitchError(ValueError):
    """The slide cannot become the target layout; the message says why, for a person."""


def layout_label(layout: str) -> str:
    """``takeaways`` -> ``takeaways``: the layout as the account names it (words, not an id)."""
    return _LAYOUT_LABELS.get(layout, layout.replace("_", " "))


def _a_slide(layout: str) -> str:
    """``an agenda slide`` / ``a content slide``: the layout with its article."""
    label = layout_label(layout)
    article = "an" if label[:1] in "aeiou" else "a"
    return f"{article} {label} slide"


@dataclass(frozen=True, slots=True)
class LayoutSwitch:
    """The converted slide plus the honest account of the move.

    ``slide`` validates under the target layout and keeps the source's ``id``,
    ``notes`` and background image. ``kept`` names what was carried over as it
    was, ``derived`` what the target needed and the conversion built from the
    source, ``dropped`` what the target has no room for - short sentences a
    toast can show.
    """

    slide: dict[str, Any]
    kept: tuple[str, ...] = ()
    derived: tuple[str, ...] = ()
    dropped: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "slide": self.slide,
            "kept": list(self.kept),
            "derived": list(self.derived),
            "dropped": list(self.dropped),
        }


# -- Text helpers -------------------------------------------------------------
#
# Fitting a string into a capped markup field (``fit_markup`` / ``fit_text`` /
# ``cut_text``) lives in ``presentation_markup`` - shared with the v1 upgrade.


def _join(parts: Sequence[str | None], separator: str = " - ") -> str | None:
    kept = [part for part in parts if part]
    return separator.join(kept) if kept else None


def _count(n: int, noun: str) -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def _profile_pictures_note(pictures: Sequence[str], target: str) -> str:
    """``The profiles block's 2 portraits and 1 logo - a takeaways slide lists words``."""
    portraits = sum(1 for role in pictures if role == "portrait")
    logos = len(pictures) - portraits
    parts = [_count(portraits, "portrait")] if portraits else []
    if logos:
        parts.append(_count(logos, "line logo"))
    return f"The profiles block's {' and '.join(parts)} - {_a_slide(target)} lists words"


def _quote_pictures_note(pictures: Sequence[str], quotes: int, target: str) -> str:
    """``The quote's portrait and logo`` or, over several quotes, ``The quotes' 2 portraits and 1 logo``."""
    if quotes == 1:
        return f"The quote's {' and '.join(pictures)} - {_a_slide(target)} lists words"
    portraits = sum(1 for role in pictures if role == "portrait")
    parts = [_count(n, noun) for n, noun in ((portraits, "portrait"), (len(pictures) - portraits, "logo")) if n]
    return f"The quotes' {' and '.join(parts)} - {_a_slide(target)} lists words"


def _ordinal(n: int) -> str:
    return {2: "second", 4: "fourth", 6: "sixth", 10: "tenth", 12: "twelfth"}.get(n, f"{n}th")


def _str(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _list(value: object) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple)) else []


def _dict(value: object) -> dict[str, Any] | None:
    return dict(value) if isinstance(value, Mapping) else None


def _dicts(value: object) -> list[dict[str, Any]]:
    return [entry for entry in (_dict(item) for item in _list(value)) if entry]


def _speaker_photos(source: _Source) -> int:
    """How many of the source's speakers wear a portrait."""
    return sum(1 for speaker in source.speakers if _str(speaker.get("photo_file_id")))


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _agenda_style_override(data: Mapping[str, Any], count: int) -> str | None:
    """The agenda ``style`` that changed the look, or ``None``.

    The look follows ``columns`` (``auto_agenda_style``: the grid at three,
    the chips otherwise) unless ``style`` says otherwise, so the choice a
    move away from the agenda layout has to report is the override - ``list``
    on the 3-column grid as much as ``grid`` on one or two columns. A stored
    ``style: "grid"`` on a 3-column agenda restates the default and is nothing
    to report, exactly like a stored ``flow: "row"``.
    """
    style = _str(data.get("style"))
    if style is None:
        return None
    columns = _int(data.get("columns")) or auto_agenda_columns(count)
    return None if style == auto_agenda_style(columns) else style


#: How the account names an agenda look it drops (an override, see above).
_AGENDA_STYLE_LOOKS: Mapping[str, str] = {
    "grid": "Grid style (N. | TITLE cells)",
    "list": "List style (numbered chips instead of the 3-column grid)",
}


# -- The source, read into one intermediate shape -----------------------------


@dataclass(slots=True)
class _Point:
    """One thing a slide LISTS - an agenda item, a takeaway, a phase, a bullet."""

    title: str
    description: str | None = None
    icon: str | None = None
    number: int | None = None

    def as_item(self) -> dict[str, Any]:
        """An ``AgendaItem`` / ``NumberedCard`` (the two share their fields)."""
        item: dict[str, Any] = {"title": self.title}
        if self.description:
            item["description"] = self.description
        if self.icon:
            item["icon"] = self.icon
        if self.number is not None:
            item["number"] = self.number
        return item

    def as_phase(self) -> dict[str, Any]:
        phase: dict[str, Any] = {"title": self.title}
        if self.description:
            phase["subtitle"] = self.description
        if self.icon:
            phase["icon"] = self.icon
        return phase

    def as_list_item(self) -> dict[str, Any]:
        text = fit_markup(_join([self.title, self.description]), MAX_TEXT_CHARS) or self.title
        item: dict[str, Any] = {"text": text}
        if self.icon:
            item["icon"] = self.icon
        return item


@dataclass(slots=True)
class _Source:
    """Everything the conversion may read off a slide, whatever its layout."""

    layout: str
    title: str | None = None
    #: The layout's default title the slide SHOWED because nobody titled it
    #: ("Agenda") - not the person's words, so a target's own default wins,
    #: but better than refusing a target that needs a title.
    displayed_default_title: str | None = None
    subtitle: str | None = None
    #: What the slide lists (items, phases, bullets...), in reading order.
    points: list[_Point] = field(default_factory=list)
    #: Where the points came from, for the account ("the 4 agenda items").
    points_origin: str = "the slide"
    #: The body as content blocks - a content slide's own.
    blocks: list[dict[str, Any]] = field(default_factory=list)
    #: A content slide's own 2-3 columns, when the body is laid out that way.
    columns: list[list[dict[str, Any]]] | None = None
    #: A roadmap's own phases (richer than points: items, goal, tone).
    phases: list[dict[str, Any]] = field(default_factory=list)
    #: A dashboard's own KPI tiles and panels - and, read off any
    #: body, the metric tiles and the chart / table figures a dashboard could
    #: be built from.
    tiles: list[dict[str, Any]] = field(default_factory=list)
    panels: list[dict[str, Any]] = field(default_factory=list)
    #: A chart's own title a heading above it displaced on the way to a panel:
    #: ``(the chart's title, the heading's)``, said in the account.
    dropped_chart_titles: list[tuple[str, str]] = field(default_factory=list)
    #: The title slide's speakers, the closing slide's one speaker as a list of one.
    speakers: list[dict[str, Any]] = field(default_factory=list)
    contacts: list[dict[str, Any]] = field(default_factory=list)
    tagline: str | None = None
    event_label: str | None = None
    footer: str | None = None
    icon_strip: list[str] = field(default_factory=list)
    number: int | None = None
    label: str | None = None
    icon: str | None = None
    footer_block: dict[str, Any] | None = None
    hide_footer: bool = False
    item_columns: int | None = None
    current: int | None = None
    #: An agenda's own grid options - nothing else draws them, so a
    #: move away from the agenda layout says they are gone. ``agenda_style`` is
    #: the explicit ``style`` only when it CHANGED the look (see
    #: ``_agenda_style_override``); a value that restates the auto rule is a
    #: default said out loud, like ``flow: "row"``, and is not carried here.
    agenda_style: str | None = None
    agenda_flow: str | None = None
    agenda_packed: bool = False
    #: A content slide whose run SPREADS over the body: ``distribute:
    #: true`` on the slide, or ``fill: true`` on its only block - a lone block
    #: has no sibling to spread from, so its own ``fill`` is what spreads its
    #: rows over the same height (and what the agenda -> content move sets).
    #: An agenda spreads its rows by default and a takeaways slide shares the
    #: height among its cards, so the look survives a move to either; a packed
    #: content run moved to an agenda packs it too (``distribute: false``).
    content_spread: bool = False
    #: Blocks that carry no words (pictures), by type - dropped when listing.
    visual_blocks: list[str] = field(default_factory=list)
    #: The pictures INSIDE a ``profiles`` block: a person's portrait
    #: (``photo_file_id``) and a line's logo (``logo_file_id``), by role. A
    #: target that lists words keeps the person's words and loses these, so
    #: they are dropped and SAID like a visual block - the block itself
    #: carries words and never lands in ``visual_blocks``.
    profile_pictures: list[str] = field(default_factory=list)
    #: The same for a ``quote``: the portrait and the company logo
    #: beside its credit, by role, and how many quotes carried them.
    quote_pictures: list[str] = field(default_factory=list)
    pictured_quotes: int = 0
    #: The logos of ``timeline`` steps: the step's words are listed, its logo
    #: has no home in a target that lists words.
    timeline_logos: int = 0
    #: The metrics read as dashboard tiles whose look a tile cannot keep: a
    #: ``plain`` or centred ``metric`` / ``metrics_grid`` - a
    #: dashboard draws its tiles as cards, so the move says the look is gone.
    restyled_tiles: int = 0


def _normalize(slide: Mapping[str, Any]) -> dict[str, Any]:
    """The slide as its own layout model stores it; the raw dict when it does not validate.

    A stored slide validates by construction. The panel's unsaved draft may
    not (a title blanked mid-edit): the conversion then reads the raw fields
    defensively rather than refusing a move over a field the target may not
    even have.
    """
    layout = slide.get("layout")
    model = slide_model(layout) if isinstance(layout, str) else None
    if model is not None:
        try:
            return model.model_validate(dict(slide)).model_dump(mode="json", exclude_none=True)
        except ValidationError:
            pass
    return {key: value for key, value in dict(slide).items() if value is not None}


def _point_from(item: object, *, title_key: str, description_key: str | None = None) -> _Point | None:
    if isinstance(item, str):
        title = fit_markup(item, MAX_TEXT_CHARS)
        return _Point(title=title) if title else None
    data = _dict(item)
    if data is None:
        return None
    title = fit_markup(data.get(title_key), MAX_TEXT_CHARS)
    if not title:
        return None
    description = fit_markup(data.get(description_key), MAX_TEXT_CHARS) if description_key else None
    return _Point(
        title=title,
        description=description,
        icon=_str(data.get("icon")),
        number=_int(data.get("number")),
    )


def _points(items: object, *, title_key: str, description_key: str | None = None) -> list[_Point]:
    return [
        point
        for point in (_point_from(item, title_key=title_key, description_key=description_key) for item in _list(items))
        if point is not None
    ]


def _phase_point(phase: Mapping[str, Any]) -> _Point | None:
    title = fit_markup(phase.get("title"), MAX_TEXT_CHARS)
    if not title:
        return None
    description = fit_markup(phase.get("subtitle"), MAX_TEXT_CHARS)
    if not description:
        items = [fit_markup(item, MAX_TEXT_CHARS) for item in _list(phase.get("items"))]
        description = fit_markup(", ".join(item for item in items if item), MAX_TEXT_CHARS)
    if not description:
        goal = _dict(phase.get("goal")) or {}
        description = fit_markup(_join([_str(goal.get("label")), _str(goal.get("value"))], ": "), MAX_TEXT_CHARS)
    return _Point(title=title, description=description, icon=_str(phase.get("icon")))


def _metric_point(metric: Mapping[str, Any]) -> _Point | None:
    label = fit_markup(metric.get("label"), MAX_TEXT_CHARS)
    value = fit_text(metric.get("value"), MAX_TEXT_CHARS)
    if not label and not value:
        return None
    if not label:
        return _Point(title=fit_markup(value, MAX_TEXT_CHARS) or "")
    description = fit_markup(_join([value, fit_text(metric.get("delta"), 50)], " "), MAX_TEXT_CHARS)
    return _Point(title=label, description=description, icon=_str(metric.get("icon")))


def _points_from_blocks(blocks: Sequence[Mapping[str, Any]], source: _Source) -> list[_Point]:
    """What a body of blocks LISTS, in reading order.

    Every block that carries words yields points; a heading followed by a
    paragraph yields one point (the paragraph as its description); pictures
    yield none and are counted on the source so the account can name them.
    """
    points: list[_Point] = []
    index = 0
    while index < len(blocks):
        block = blocks[index]
        kind = block.get("type")
        index += 1
        if kind == "heading":
            title = fit_markup(block.get("content"), MAX_TEXT_CHARS)
            description = None
            if index < len(blocks) and blocks[index].get("type") == "text":
                description = fit_markup(blocks[index].get("content"), MAX_TEXT_CHARS)
                index += 1
            if title:
                points.append(_Point(title=title, description=description, icon=_str(block.get("icon"))))
        elif kind == "text":
            title = fit_markup(block.get("content"), MAX_TEXT_CHARS)
            if title:
                points.append(_Point(title=title))
        elif kind == "quote":
            title = fit_markup(block.get("content"), MAX_TEXT_CHARS)
            if title:
                points.append(_Point(title=title, description=fit_markup(quote_credit(block), MAX_TEXT_CHARS)))
            # The figures under a customer quote are metrics a list carries;
            # its portrait and logo are files it cannot - counted on
            # the source so the account names them as dropped.
            points.extend(p for p in (_metric_point(item) for item in _dicts(block.get("figures"))) if p is not None)
            roles = [role for role, key in (("portrait", "photo_file_id"), ("logo", "logo_file_id")) if _str(block.get(key))]
            source.quote_pictures.extend(roles)
            source.pictured_quotes += bool(roles)
        elif kind in ("list", "badges"):
            points.extend(_points(block.get("items"), title_key="text"))
        elif kind in ("numbered_cards", "feature_list"):
            points.extend(_points(block.get("items"), title_key="title", description_key="description"))
        elif kind == "timeline":
            for item in _dicts(block.get("items")):
                source.timeline_logos += bool(_str(item.get("logo_file_id")))
                point = _point_from(item, title_key="title", description_key="description")
                if point is not None:
                    point.description = point.description or fit_markup(item.get("label"), MAX_TEXT_CHARS)
                    points.append(point)
        elif kind == "process_flow":
            points.extend(_points(block.get("steps"), title_key="title", description_key="description"))
        elif kind == "phase_card":
            point = _phase_point(block)
            if point is not None:
                points.append(point)
        elif kind == "key_value":
            points.extend(_points(block.get("items"), title_key="label", description_key="value"))
        elif kind == "pricing_tiers":
            for tier in _dicts(block.get("tiers")):
                point = _point_from(tier, title_key="name", description_key="description")
                if point is not None:
                    price = _join([fit_text(tier.get("price"), 40), _str(tier.get("period"))], " ")
                    point.description = point.description or fit_markup(price, MAX_TEXT_CHARS)
                    points.append(point)
        elif kind == "personas":
            points.extend(_points(block.get("items"), title_key="name", description_key="tagline"))
        elif kind == "profiles":
            # A person is one point: the name, then the title and the facts.
            # The portrait and the line logos are files a takeaway or
            # an agenda cannot carry: counted on the source (``profile_pictures``)
            # so the account names them as dropped, never lost in silence.
            for person in _dicts(block.get("items")):
                lines = _dicts(person.get("lines"))
                if _str(person.get("photo_file_id")):
                    source.profile_pictures.append("portrait")
                source.profile_pictures.extend("logo" for line in lines if _str(line.get("logo_file_id")))
                point = _point_from(person, title_key="name", description_key="title")
                if point is not None:
                    facts = [fit_markup(line.get("text"), MAX_TEXT_CHARS) for line in lines]
                    detail = " - ".join(fact for fact in facts if fact)
                    point.description = fit_markup(_join([point.description, detail], " - "), MAX_TEXT_CHARS)
                    points.append(point)
        elif kind == "section_header":
            title = fit_markup(block.get("title"), MAX_TEXT_CHARS)
            if title:
                points.append(_Point(title=title, icon=_str(block.get("icon"))))
        elif kind == "banner":
            title = fit_markup(block.get("title"), MAX_TEXT_CHARS)
            if title:
                subtitle = fit_markup(block.get("subtitle"), MAX_TEXT_CHARS)
                points.append(_Point(title=title, description=subtitle, icon=_str(block.get("icon"))))
        elif kind == "metric":
            point = _metric_point(block)
            if point is not None:
                points.append(point)
        elif kind == "metrics_grid":
            points.extend(p for p in (_metric_point(item) for item in _dicts(block.get("items"))) if p is not None)
        elif kind == "table":
            for row in _list(block.get("rows")):
                cells = [fit_markup(cell.get("text"), MAX_TEXT_CHARS) for cell in _dicts(row)]
                words = [cell for cell in cells if cell]
                if words:
                    points.append(_Point(title=words[0], description=fit_markup(" - ".join(words[1:]), MAX_TEXT_CHARS)))
        elif kind == "callout":
            title = fit_markup(block.get("title"), MAX_TEXT_CHARS)
            content = fit_markup(block.get("content"), MAX_TEXT_CHARS)
            if title or content:
                points.append(
                    _Point(
                        title=title or content or "",
                        description=content if title else None,
                        icon=_str(block.get("icon")),
                    )
                )
            points.extend(_points_from_blocks(_dicts(block.get("children")), source))
        elif kind == "challenge_answer":
            # The two cards are two points, each led by its eyebrow:
            # the challenge keeps the block's icon, the answer its check.
            for key, label_key, icon in (
                ("challenge", "challenge_label", _str(block.get("icon"))),
                ("answer", "answer_label", "circle-check"),
            ):
                text = fit_markup(block.get(key), MAX_TEXT_CHARS)
                if text:
                    label = fit_markup(block.get(label_key), MAX_TEXT_CHARS)
                    points.append(_Point(title=label or text, description=text if label else None, icon=icon))
        elif kind == "comparison":
            for side_key in ("left", "right"):
                side = _dict(block.get(side_key)) or {}
                label = fit_markup(side.get("label"), MAX_TEXT_CHARS)
                if label:
                    points.append(_Point(title=label, icon=_str(side.get("icon"))))
                points.extend(_points_from_blocks(_dicts(side.get("blocks")), source))
        elif kind == "columns":
            for column in _list(block.get("columns")):
                points.extend(_points_from_blocks(_dicts(column), source))
        elif isinstance(kind, str) and kind in _VISUAL_BLOCKS:
            source.visual_blocks.append(kind)
    return points


#: What a dashboard tile holds (``MetricItem``).
_TILE_FIELDS = frozenset(MetricItem.model_fields)


def _restyled(block: Mapping[str, Any]) -> bool:
    """Whether a ``metric`` / ``metrics_grid`` wears a look a dashboard tile cannot keep."""
    return block.get("variant") == "plain" or block.get("align") == "center"


def _figures_from_blocks(blocks: Sequence[Mapping[str, Any]], source: _Source) -> None:
    """What a body of blocks could put on a DASHBOARD: its metrics as
    tiles, its charts and tables as panels - a heading right above a figure
    becomes that panel's title, and so does a chart's own ``title``
    when no heading names it. A panel refuses a chart title of its own, so
    the chart's is always taken off the block: moved, or dropped (and said)
    when the heading's wins. Walks into columns, callouts and comparisons."""
    pending_title: str | None = None
    for block in blocks:
        kind = block.get("type")
        if kind in ("heading", "section_header"):
            pending_title = fit_markup(block.get("content" if kind == "heading" else "title"), MAX_LABEL_CHARS)
            continue
        if kind == "metric":
            # A tile is a `MetricItem`: the block's own look fields have no place on it.
            source.tiles.append({key: value for key, value in block.items() if key in _TILE_FIELDS})
            source.restyled_tiles += _restyled(block)
        elif kind == "metrics_grid":
            items = _dicts(block.get("items"))
            source.tiles.extend(items)
            source.restyled_tiles += len(items) if _restyled(block) else 0
        elif isinstance(kind, str) and kind in _PANEL_FIGURES:
            figure = dict(block)
            own_title = fit_markup(figure.pop("title", None), MAX_LABEL_CHARS) if kind == "chart" else None
            panel: dict[str, Any] = {"block": figure}
            if pending_title:
                panel["title"] = pending_title
                if own_title:
                    source.dropped_chart_titles.append((own_title, pending_title))
            elif own_title:
                # Moved, not copied: the panel is the one place the title is
                # drawn, and a switch back draws it as a heading over the chart.
                panel["title"] = own_title
            source.panels.append(panel)
        elif kind == "callout":
            _figures_from_blocks(_dicts(block.get("children")), source)
        elif kind == "comparison":
            for side_key in ("left", "right"):
                side = _dict(block.get(side_key)) or {}
                _figures_from_blocks(_dicts(side.get("blocks")), source)
        elif kind == "columns":
            for column in _list(block.get("columns")):
                _figures_from_blocks(_dicts(column), source)
        pending_title = None


def _read_source(data: Mapping[str, Any]) -> _Source:
    """Read a (normalised) slide of any layout into the one shape every target is built from."""
    layout = str(data.get("layout"))
    source = _Source(layout=layout)
    source.title = fit_markup(data.get("title"), MAX_TITLE_CHARS)
    source.subtitle = fit_markup(data.get("subtitle"), MAX_TEXT_CHARS)
    source.footer_block = _dict(data.get("footer_block"))
    source.hide_footer = data.get("hide_footer") is True

    if layout == "title":
        source.footer = fit_markup(data.get("footer"), MAX_TEXT_CHARS)
        source.event_label = fit_markup(data.get("event_label"), MAX_LABEL_CHARS)
        source.speakers = _dicts(fold_title_speaker(data).get("speakers"))
        source.icon_strip = [icon for icon in _list(data.get("icon_strip")) if isinstance(icon, str)]
    elif layout == "section":
        source.number = _int(data.get("number"))
        source.label = fit_markup(data.get("label"), MAX_LABEL_CHARS)
        source.icon = _str(data.get("icon"))
    elif layout in ("agenda", "takeaways"):
        source.points = _points(data.get("items"), title_key="title", description_key="description")
        noun = "agenda item" if layout == "agenda" else "takeaway"
        source.points_origin = f"the {_count(len(source.points), noun)}"
        source.item_columns = _int(data.get("columns"))
        source.current = _int(data.get("current"))
        if layout == "agenda":
            source.agenda_style = _agenda_style_override(data, len(source.points))
            source.agenda_flow = _str(data.get("flow"))
            source.agenda_packed = data.get("distribute") is False
    elif layout == "roadmap":
        source.phases = _dicts(data.get("phases"))
        source.points = [p for p in (_phase_point(phase) for phase in source.phases) if p is not None]
        source.points_origin = f"the {_count(len(source.phases), 'phase')}"
    elif layout == "content":
        blocks = _dicts(data.get("blocks"))
        columns = [column for column in (_dicts(column) for column in _list(data.get("columns"))) if column]
        if columns and not blocks:
            source.columns = columns
            blocks = [block for column in columns for block in column]
        source.blocks = blocks
        source.points = _points_from_blocks(blocks, source)
        source.points_origin = f"the {_count(len(blocks), 'block')}"
        source.content_spread = data.get("distribute") is True or _lone_block_fills(blocks, columns)
        _figures_from_blocks(blocks, source)
    elif layout == "dashboard":
        source.tiles = _dicts(data.get("tiles"))
        source.panels = [panel for panel in _dicts(data.get("panels")) if _dict(panel.get("block"))]
        # As a body of blocks: the tile row first, then every panel's figure
        # under its title - what a content slide shows of it.
        blocks = [{"type": "metrics_grid", "items": source.tiles[:MAX_GRID_ITEMS]}] if source.tiles else []
        for panel in source.panels:
            title = fit_markup(panel.get("title"), MAX_LABEL_CHARS)
            if title:
                blocks.append({"type": "heading", "content": title, "level": 3})
            blocks.append(dict(panel["block"]))
        source.blocks = blocks
        source.points = _points_from_blocks(blocks, source)
        source.points_origin = f"the {_count(len(source.tiles), 'tile')} and {_count(len(source.panels), 'panel')}"
    elif layout == "closing":
        speaker = _dict(data.get("speaker"))
        source.speakers = [speaker] if speaker else []
        source.contacts = _dicts(data.get("contacts"))
        source.tagline = fit_markup(data.get("tagline"), MAX_TEXT_CHARS)
    return source


# -- Building the target ------------------------------------------------------


class _Account:
    """Collects the kept / derived / dropped sentences while a target is built."""

    def __init__(self) -> None:
        self.kept: list[str] = []
        self.derived: list[str] = []
        self.dropped: list[str] = []
        #: Source fields a target consumed (kept or derived), by name - what:
        #: meth:`leftovers` must not report as dropped.
        self.handled: set[str] = set()

    def keep(self, what: str, *fields: str) -> None:
        if what not in self.kept:
            self.kept.append(what)
        self.handled.update(fields)

    def derive(self, what: str, *fields: str) -> None:
        self.derived.append(what)
        self.handled.update(fields)

    def drop(self, what: str) -> None:
        if what not in self.dropped:
            self.dropped.append(what)

    def leftovers(self, source: _Source, target: str) -> None:
        """Say what the source had that the target neither kept nor derived from."""
        body_noun = (
            "phase"
            if source.layout == "roadmap"
            else "block"
            if source.layout == "content"
            else "panel"
            if source.layout == "dashboard"
            else "item"
        )
        body_count = (
            len(source.phases)
            if source.layout == "roadmap"
            else len(source.blocks)
            if body_noun == "block"
            else len(source.panels)
            if body_noun == "panel"
            else len(source.points)
        )
        checks: tuple[tuple[str, bool, str], ...] = (
            (
                "body",
                body_count > 0,
                f"{_count(body_count, body_noun).capitalize()} - {_a_slide(target)} has no body",
            ),
            ("event_label", bool(source.event_label), "Event label"),
            ("footer", bool(source.footer), "Footer line"),
            ("icon_strip", bool(source.icon_strip), f"Icon strip ({_count(len(source.icon_strip), 'icon')})"),
            (
                "speakers",
                bool(source.speakers),
                "Speaker" if len(source.speakers) == 1 else _count(len(source.speakers), "speaker").capitalize(),
            ),
            (
                "speaker_photos",
                _speaker_photos(source) > 0,
                "Speaker photo" if _speaker_photos(source) == 1 else f"{_speaker_photos(source)} speaker photos",
            ),
            (
                # A closing slide names ONE speaker: the lead is kept, the others said.
                "co_speakers",
                len(source.speakers) > 1 and target == "closing",
                f"{_count(len(source.speakers) - 1, 'more speaker').capitalize()} - a closing slide names one",
            ),
            ("contacts", bool(source.contacts), _count(len(source.contacts), "contact line").capitalize()),
            ("tagline", bool(source.tagline), "Tagline"),
            ("number", source.number is not None, f"Section number {source.number}"),
            ("label", bool(source.label), f"Section label '{source.label}'"),
            ("icon", bool(source.icon), "Section icon"),
            ("current", source.current is not None, "Highlighted agenda item"),
            (
                # Only an override reaches here (``_agenda_style_override``):
                # the grid on one or two columns, the chips on the 3-column grid.
                "style",
                source.agenda_style is not None,
                _AGENDA_STYLE_LOOKS.get(source.agenda_style or "", f"Agenda style {source.agenda_style!r}")
                + " - only an agenda slide draws it",
            ),
            ("flow", source.agenda_flow == "column", "Column-first fill order - only an agenda slide has it"),
            (
                # A content slide packs its blocks by default, so a packed agenda
                # moved there is kept (see the content target), not dropped.
                "distribute",
                source.agenda_packed,
                "Rows packed to the top - only agenda and content slides choose that",
            ),
            (
                # An agenda spreads its rows by default, so the choice survives a
                # move there (see the agenda target); no other layout has a run.
                "distribute",
                source.content_spread,
                "Blocks spread over the body - only content and agenda slides choose that",
            ),
            ("footer_block", source.footer_block is not None, "Footer block"),
            (
                # When the body found a home the tiles went with it (a metrics
                # grid, or their words listed): said only when nothing did.
                "tiles",
                bool(source.tiles) and source.layout == "dashboard" and "body" not in self.handled,
                f"{_count(len(source.tiles), 'KPI tile')} - {_a_slide(target)} has no tile row",
            ),
            (
                "hide_footer",
                source.hide_footer,
                "Hidden footer - only content, roadmap, takeaways and dashboard slides can hide it",
            ),
            (
                # Said only when the body itself found a home in a target that
                # LISTS its words: when the whole body is dropped, the body
                # note above already counts these blocks, and a content or
                # dashboard target carries a figure as a figure.
                "visual_blocks",
                bool(source.visual_blocks) and "body" in self.handled and target not in ("content", "dashboard"),
                f"{_count(len(source.visual_blocks), 'visual block').capitalize()} "
                f"({', '.join(sorted(set(source.visual_blocks)))}) - {_a_slide(target)} lists words",
            ),
            (
                # Same rule for the pictures inside a profiles block:
                # the person's words were listed, the portrait and the logos
                # have no home in a target that lists words. A content target
                # keeps the block whole; a dashboard target drops the body and
                # says so in the body note.
                "profile_pictures",
                bool(source.profile_pictures) and "body" in self.handled and target not in ("content", "dashboard"),
                _profile_pictures_note(source.profile_pictures, target),
            ),
            (
                "quote_pictures",
                bool(source.quote_pictures) and "body" in self.handled and target not in ("content", "dashboard"),
                _quote_pictures_note(source.quote_pictures, source.pictured_quotes, target),
            ),
            (
                "timeline_logos",
                bool(source.timeline_logos) and "body" in self.handled and target not in ("content", "dashboard"),
                f"The timeline's {_count(source.timeline_logos, 'step logo')} - {_a_slide(target)} lists words",
            ),
        )
        for name, present, note in checks:
            if present and name not in self.handled:
                self.drop(note)


def _derived_title(source: _Source, target: str, account: _Account) -> str | None:
    """The title the target shows: the source's own, else the first heading of the body, else the layout's default, else a short first item."""
    if source.title:
        account.keep("Title", "title")
        return source.title
    for block in source.blocks:
        kind = block.get("type")
        key = "content" if kind == "heading" else "title" if kind in ("section_header", "banner") else None
        if key is None:
            continue
        title = fit_markup(block.get(key), MAX_TITLE_CHARS)
        if title:
            account.derive(f"Title from the first {str(kind).replace('_', ' ')} block")
            _unlist_title(source, title)
            return title
    default = _DEFAULT_TITLES.get(target)
    if default:
        account.derive(f"Title '{default}' (the layout's default - rename it if you like)")
        return default
    if source.points and len(source.points[0].title) <= _MAX_DERIVED_TITLE_CHARS:
        account.derive("Title from the first item")
        return source.points[0].title
    if source.displayed_default_title:
        account.derive(f"Title '{source.displayed_default_title}' as the slide showed it - rename it if you like")
        return source.displayed_default_title
    return None


def _unlist_title(source: _Source, title: str) -> None:
    """A heading read as the title is not ALSO the first item.

    The block that gave the title yielded a point too (``_points_from_blocks``
    reads every heading), so the target would list its own title as its first
    entry. That point goes; the paragraph under the heading - its description
    - stays listed on its own, so no words are lost.
    """
    for index, point in enumerate(source.points):
        if point.title == title:
            if point.description:
                source.points[index] = _Point(title=point.description, icon=point.icon)
            else:
                del source.points[index]
            return


def _require_title(source: _Source, target: str, account: _Account) -> str:
    title = _derived_title(source, target, account)
    if not title:
        raise LayoutSwitchError(
            f"This {layout_label(source.layout)} slide has no title and no short line a title could be read "
            f"from; {_a_slide(target)} needs one. Give the slide a title first."
        )
    return title


def _base(data: Mapping[str, Any], target: str) -> dict[str, Any]:
    """What every layout shares: identity, speaker notes, background."""
    payload: dict[str, Any] = {"layout": target}
    for key in ("id", "notes", "background_image_file_id"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            payload[key] = value
    return payload


def _carry_subtitle(source: _Source, payload: dict[str, Any], account: _Account, *, limit: int) -> None:
    subtitle = fit_markup(source.subtitle, limit)
    if subtitle:
        payload["subtitle"] = subtitle
        account.keep("Subtitle", "subtitle")


def _carry_footer_block(source: _Source, payload: dict[str, Any], account: _Account) -> None:
    if source.footer_block:
        payload["footer_block"] = source.footer_block
        account.keep("Footer block", "footer_block")


def _carry_hide_footer(source: _Source, payload: dict[str, Any], account: _Account) -> None:
    if source.hide_footer:
        payload["hide_footer"] = True
        account.keep("Hidden footer", "hide_footer")


def _lone_block_fills(blocks: Sequence[Mapping[str, Any]], columns: Sequence[Sequence[Any]]) -> bool:
    """Whether a content body is ONE block that takes the body's height (``fill: true``).

    ``distribute`` spreads blocks from each other, so on a lone block it draws
    nothing; the block's own ``fill`` is what spreads its rows over the run's
    height there. Read as the slide's spread so the look is what travels.
    """
    return not columns and len(blocks) == 1 and blocks[0].get("fill") is True


def _spread_lone_block(blocks: list[dict[str, Any]]) -> bool:
    """Set ``fill`` on a body that is one grid block, so its rows spread over the run.

    The agenda and takeaways moves build exactly one grid block (numbered
    cards or a list): a spread agenda or a takeaways slide whose cards share
    the height would otherwise pack that block under the title, and setting
    ``distribute`` on the slide would spread nothing - there is no second
    block to spread from. Returns whether the fill was set.
    """
    if len(blocks) != 1 or blocks[0].get("type") not in FILL_BLOCK_TYPES:
        return False
    blocks[0]["fill"] = True
    return True


def _items(source: _Source, target: str, account: _Account, *, minimum: int, maximum: int, noun: str) -> list[_Point]:
    """The points the target lists, or a refusal when the source has too few."""
    points = source.points
    if len(points) < minimum:
        what = "nothing to list" if not points else f"only {_count(len(points), noun)} to list"
        raise LayoutSwitchError(
            f"This {layout_label(source.layout)} slide has {what}; {_a_slide(target)} needs at "
            f"least {minimum} {noun}s. Add the content with your assistant first, or pick another layout."
        )
    if len(points) > maximum:
        account.drop(
            f"{_count(len(points) - maximum, noun).capitalize()} beyond the {_ordinal(maximum)} "
            f"- {_a_slide(target)} has at most {maximum}"
        )
        points = points[:maximum]
    same_shape = source.layout in ("agenda", "takeaways") and target in ("agenda", "takeaways")
    if same_shape:
        account.keep(_count(len(points), noun).capitalize(), "body")
    else:
        account.derive(f"{_count(len(points), noun).capitalize()} from {source.points_origin}", "body")
    return points


def _body_blocks(source: _Source, account: _Account) -> tuple[list[dict[str, Any]], list[list[dict[str, Any]]] | None]:
    """A content slide's body from any source: its own blocks, or blocks built from what it holds."""
    if source.layout == "content":
        if source.columns:
            account.keep(_count(len(source.columns), "column").capitalize(), "body")
        elif source.blocks:
            account.keep(_count(len(source.blocks), "block").capitalize(), "body")
        return source.blocks, source.columns
    if source.layout == "dashboard":
        if not source.blocks:
            return [], None
        parts = []
        if source.tiles:
            parts.append(f"a metrics grid from the {_count(len(source.tiles), 'tile')}")
        if source.panels:
            parts.append(f"the {_count(len(source.panels), 'panel')}' figures under their titles")
        account.derive(f"Blocks: {' and '.join(parts)}", "body", "tiles")
        return source.blocks, None
    if source.layout in ("agenda", "takeaways"):
        points = source.points
        if not points:
            return [], None
        if len(points) <= MAX_CARD_ITEMS:
            account.derive(f"A numbered cards block from {source.points_origin}", "body")
            return [{"type": "numbered_cards", "items": [p.as_item() for p in points]}], None
        account.derive(f"A list block from {source.points_origin} (too many for cards)", "body")
        return [{"type": "list", "items": [p.as_list_item() for p in points[:MAX_LIST_ITEMS]]}], None
    if source.layout == "roadmap":
        if not source.phases:
            return [], None
        cards: list[dict[str, Any]] = []
        for phase in source.phases[:MAX_ROADMAP_PHASES]:
            card = {
                key: value
                for key, value in phase.items()
                if key in ("icon", "title", "subtitle", "items", "goal", "tone")
            }
            card["type"] = "phase_card"
            cards.append(card)
        account.derive(f"A phase card per phase, side by side, from {source.points_origin}", "body")
        # A columns BLOCK, not the slide's own columns: those stop at three
        # and a roadmap has up to four phases.
        return (cards if len(cards) < 2 else [{"type": "columns", "columns": [[card] for card in cards]}]), None
    blocks: list[dict[str, Any]] = []
    for speaker in source.speakers:
        name = fit_markup(speaker.get("name"), MAX_LABEL_CHARS)
        if name:
            strong = f"**{name}**" if _STRAY_MARKER_RE.search(name) is None and "_" not in name else name
            line = fit_markup(_join([strong, fit_markup(speaker.get("title"), MAX_TEXT_CHARS)]), MAX_MARKUP_CHARS)
            if line:
                blocks.append({"type": "text", "content": line})
                account.derive(
                    "A text block from the speaker line"
                    if len(source.speakers) == 1
                    else "A text block from each speaker line",
                    "speakers",
                    "co_speakers",
                )
    if source.event_label:
        blocks.append({"type": "badges", "items": [{"text": source.event_label}]})
        account.derive("A badge from the event label", "event_label")
    if source.footer:
        blocks.append({"type": "text", "content": source.footer, "size": "sm"})
        account.derive("A text block from the footer line", "footer")
    if source.contacts:
        items: list[dict[str, Any]] = []
        for contact in source.contacts[:MAX_KEY_VALUE_ITEMS]:
            value = fit_markup(contact.get("value"), MAX_TEXT_CHARS)
            if not value:
                continue
            kind = str(contact.get("kind") or "")
            label = (
                fit_markup(contact.get("label"), MAX_LABEL_CHARS)
                or _CONTACT_LABELS.get(kind)
                or kind.capitalize()
                or "Contact"
            )
            items.append({"label": label, "value": value})
        if items:
            blocks.append({"type": "key_value", "items": items})
            account.derive(f"A spec sheet from the {_count(len(items), 'contact line')}", "contacts")
    if source.tagline:
        blocks.append({"type": "quote", "content": source.tagline})
        account.derive("A quote block from the tagline", "tagline")
    if source.label:
        blocks.append({"type": "badges", "items": [{"text": source.label}]})
        account.derive("A badge from the section label", "label")
    return blocks, None


#: Blocks a dashboard reads something from: consumed, not reported as dropped.
_DASHBOARD_READS: frozenset[str] = frozenset(PANEL_BLOCK_TYPES) | {
    "heading",
    "section_header",
    "columns",
    "callout",
    "comparison",
}


def _dashboard_figures(
    source: _Source, target: str, account: _Account
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The KPI tiles and panels a dashboard shows, read off the source, or a refusal.

    A dashboard is figures: it needs at least one chart or table for a panel
    and two metrics for its tile row. Neither is invented - a slide without
    them is refused with what to add first.
    """
    label = layout_label(source.layout)
    if not source.panels:
        raise LayoutSwitchError(
            f"This {label} slide has no chart or table to show on a panel; {_a_slide(target)} needs at least "
            "one. Add a chart with your assistant first, or pick another layout."
        )
    if len(source.tiles) < MIN_DASHBOARD_TILES:
        what = "no metric" if not source.tiles else f"only {_count(len(source.tiles), 'metric')}"
        raise LayoutSwitchError(
            f"This {label} slide has {what} for the KPI row; {_a_slide(target)} needs at least "
            f"{MIN_DASHBOARD_TILES} metric tiles over its panels. Add the figures with your assistant first, "
            "or pick another layout."
        )
    tiles, panels = list(source.tiles), list(source.panels)
    if len(tiles) > MAX_DASHBOARD_TILES:
        account.drop(
            f"{_count(len(tiles) - MAX_DASHBOARD_TILES, 'metric').capitalize()} beyond the "
            f"{_ordinal(MAX_DASHBOARD_TILES)} - {_a_slide(target)} has at most {MAX_DASHBOARD_TILES} tiles"
        )
        tiles = tiles[:MAX_DASHBOARD_TILES]
    if len(panels) > MAX_DASHBOARD_PANELS:
        account.drop(
            f"{_count(len(panels) - MAX_DASHBOARD_PANELS, 'figure').capitalize()} beyond the "
            f"{_ordinal(MAX_DASHBOARD_PANELS)} - {_a_slide(target)} has at most {MAX_DASHBOARD_PANELS} panels"
        )
        panels = panels[:MAX_DASHBOARD_PANELS]
    account.derive(f"{_count(len(tiles), 'KPI tile')} from the body's metrics", "body", "tiles")
    if source.restyled_tiles:
        account.drop(
            f"The plain or centred look of {_count(source.restyled_tiles, 'metric')} - "
            f"{_a_slide(target)} draws its tiles as cards"
        )
    account.derive(f"{_count(len(panels), 'panel').capitalize()} from the body's charts and tables")
    for own_title, heading in source.dropped_chart_titles:
        account.drop(
            f'The chart title "{own_title}" - its panel is titled by the heading above the chart, "{heading}"; '
            "a panel carries one title"
        )
    others = sorted({str(block.get("type")) for block in source.blocks if block.get("type") not in _DASHBOARD_READS})
    if others:
        account.drop(f"The other blocks ({', '.join(others)}) - {_a_slide(target)} shows figures only")
    return tiles, panels


def _build(source: _Source, data: Mapping[str, Any], target: str) -> tuple[dict[str, Any], _Account]:
    account = _Account()
    payload = _base(data, target)

    if target == "title":
        payload["title"] = _require_title(source, target, account)
        _carry_subtitle(source, payload, account, limit=MAX_TITLE_CHARS)
        if source.speakers:
            payload["speakers"] = source.speakers[:MAX_COVER_SPEAKERS]
            account.keep("Speaker" if len(source.speakers) == 1 else "Speakers", "speakers", "speaker_photos")
        if source.tagline:
            payload["footer"] = source.tagline
            account.derive("Footer line from the tagline", "tagline")

    elif target == "section":
        payload["title"] = _require_title(source, target, account)
        _carry_subtitle(source, payload, account, limit=MAX_TEXT_CHARS)
        if source.event_label:
            payload["label"] = source.event_label
            account.derive("Section label from the event label", "event_label")

    elif target == "closing":
        payload["title"] = _require_title(source, target, account)
        _carry_subtitle(source, payload, account, limit=MAX_TEXT_CHARS)
        if source.speakers:
            # The closing slide's speaker has no portrait: the lead, words only.
            payload["speaker"] = {key: value for key, value in source.speakers[0].items() if key != "photo_file_id"}
            account.keep("Speaker", "speakers")
        if source.footer:
            payload["tagline"] = source.footer
            account.derive("Tagline from the footer line", "footer")

    elif target in ("agenda", "takeaways"):
        maximum = MAX_AGENDA_ITEMS if target == "agenda" else MAX_TAKEAWAYS
        noun = "item" if target == "agenda" else "takeaway"
        payload["title"] = _require_title(source, target, account)
        _carry_subtitle(source, payload, account, limit=MAX_TEXT_CHARS)
        points = _items(source, target, account, minimum=2, maximum=maximum, noun=noun)
        payload["items"] = [point.as_item() for point in points]
        columns = source.item_columns
        if (
            target == "agenda"
            and columns == AGENDA_GRID_COLUMNS
            and not AGENDA_GRID_MIN_ITEMS <= len(points) <= AGENDA_GRID_MAX_ITEMS
        ):
            # Three agenda columns is the grid, which wants two to
            # three rows: the items decide, the validator would refuse the rest.
            account.drop(
                f"{AGENDA_GRID_COLUMNS} columns - an agenda grid needs {AGENDA_GRID_MIN_ITEMS} to "
                f"{AGENDA_GRID_MAX_ITEMS} items and this slide lists {len(points)}"
            )
        elif columns in (1, 2, 3):
            payload["columns"] = columns
        if source.layout == "content":
            if source.content_spread:
                # The agenda spreads its rows by default and the takeaways
                # cards share the height: the content slide's spread run is
                # the look it gets, nothing to set.
                account.keep("Blocks spread over the body", "distribute")
            elif target == "agenda":
                # A content run packs under the title unless told otherwise;
                # an agenda spreads unless told otherwise. The switch travels
                # so the rows land where the blocks were - not spread out
                # silently because the target's default happens to differ.
                payload["distribute"] = False
                account.keep("Blocks packed to the top", "distribute")
        _carry_footer_block(source, payload, account)
        if target == "takeaways":
            _carry_hide_footer(source, payload, account)

    elif target == "roadmap":
        payload["title"] = _require_title(source, target, account)
        _carry_subtitle(source, payload, account, limit=MAX_TEXT_CHARS)
        points = _items(source, target, account, minimum=2, maximum=MAX_ROADMAP_PHASES, noun="phase")
        payload["phases"] = [point.as_phase() for point in points]
        _carry_footer_block(source, payload, account)
        _carry_hide_footer(source, payload, account)

    elif target == "content":
        if source.title:
            payload["title"] = source.title
            account.keep("Title", "title")
        elif source.displayed_default_title:
            payload["title"] = source.displayed_default_title
            account.derive(f"Title '{source.displayed_default_title}' as the slide showed it - rename it if you like")
        _carry_subtitle(source, payload, account, limit=MAX_TEXT_CHARS)
        blocks, columns = _body_blocks(source, account)
        if not blocks and not columns and "subtitle" in payload:
            # Nothing for the body: the subtitle becomes it, so the slide
            # shows the same words - not a placeholder nobody wrote.
            blocks = [{"type": "text", "content": payload.pop("subtitle"), "size": "lg"}]
            account.kept.remove("Subtitle")
            account.derive("Body text from the subtitle", "body")
        if not blocks and not columns:
            raise LayoutSwitchError(
                f"This {layout_label(source.layout)} slide has nothing for a body - a content slide needs at "
                "least one block. Add the content with your assistant first, or pick another layout."
            )
        if columns:
            payload["columns"] = columns
        else:
            if len(blocks) > MAX_BLOCKS_PER_SLIDE:
                account.drop(
                    f"{_count(len(blocks) - MAX_BLOCKS_PER_SLIDE, 'block').capitalize()} beyond the "
                    f"{_ordinal(MAX_BLOCKS_PER_SLIDE)} - a content slide has at most {MAX_BLOCKS_PER_SLIDE}"
                )
                blocks = blocks[:MAX_BLOCKS_PER_SLIDE]
            payload["blocks"] = blocks
        account.handled.add("body")
        if source.agenda_packed:
            # A content slide packs its blocks by default (`distribute` is
            # false): the agenda's packed rows keep their look, nothing to set.
            account.keep("Rows packed to the top", "distribute")
        elif source.layout in ("agenda", "takeaways") and not columns and _spread_lone_block(blocks):
            # A spread agenda (its default) or a takeaways slide (its cards
            # always share the height) moved to the one grid block the body
            # becomes: the block's `fill` spreads its rows over the run, the
            # look the slide had. `distribute` on the slide would spread
            # nothing - one block has no sibling to spread from - and is not
            # set; a lone `fill` block reads back as the slide's spread.
            account.keep("Rows spread over the body (the block's fill)", "distribute")
        _carry_footer_block(source, payload, account)
        _carry_hide_footer(source, payload, account)

    elif target == "dashboard":
        payload["title"] = _require_title(source, target, account)
        _carry_subtitle(source, payload, account, limit=MAX_TEXT_CHARS)
        payload["tiles"], payload["panels"] = _dashboard_figures(source, target, account)
        _carry_footer_block(source, payload, account)
        _carry_hide_footer(source, payload, account)

    else:  # pragma: no cover - switch_slide_layout only passes current layouts
        raise LayoutSwitchError(f"Unknown layout {target!r}.")

    account.leftovers(source, target)
    return payload, account


# -- Entry point --------------------------------------------------------------


def switch_slide_layout(slide: Mapping[str, Any], target: str) -> LayoutSwitch:
    """Convert *slide* to *target*, or raise :class:`LayoutSwitchError` saying why not.

    *slide* is one ``PresentationSpec`` slide object, stored or drafted;
    *target* a layout id of the catalog. The returned slide validates under
    the target layout's model, keeps the source's ``id`` / ``notes`` /
    background image, and comes with the account of the move. Moving a slide
    to the layout it already has returns it normalised, with an empty account.

    A slide still wearing a retired v1 layout - the panel's draft was
    opened before the deck was re-read - is upgraded to its v2 equivalent
    first and the account says so; a retired layout is never a target. The
    v1 ``dashboard`` shape is told apart from the current ``dashboard`` layout
    by what it holds (``presentation_legacy.is_retired_slide``), never by id.
    """
    if not isinstance(slide, Mapping):
        raise LayoutSwitchError("The slide must be an object.")
    if target not in SLIDE_LAYOUTS:
        if is_retired_layout(target):
            raise LayoutSwitchError(
                f"'{target}' is a retired v1 layout: nothing is moved to it any more, and a stored slide "
                f"wearing it is upgraded to the current catalog. A slide can be moved to: {', '.join(SLIDE_LAYOUTS)}."
            )
        raise LayoutSwitchError(f"Unknown layout {target!r}. A slide can be moved to: {', '.join(SLIDE_LAYOUTS)}.")
    source_layout = slide.get("layout")
    upgrade_note: tuple[str, ...] = ()
    upgrade_dropped: tuple[str, ...] = ()
    if is_retired_slide(slide):
        was = f"'{source_layout}' layout" if is_retired_layout(source_layout) else f"v1 '{source_layout}' shape"
        slide, flattened = upgrade_retired_slide(slide)
        upgrade_note = (f"Upgraded from the retired {was} to {_a_slide(slide['layout'])} first",)
        if flattened:
            upgrade_dropped = ("The old rich text's formatting: its words are kept as plain text blocks",)
        source_layout = slide["layout"]
    if not isinstance(source_layout, str) or source_layout not in SLIDE_LAYOUTS:
        raise LayoutSwitchError(f"The slide's layout {source_layout!r} is not one the platform knows.")

    model = slide_model(target)
    assert model is not None  # noqa: S101 - every layout of the catalog has a model (pinned in presentation_spec)
    data = _normalize(slide)
    if source_layout == target:
        return LayoutSwitch(slide=data, derived=upgrade_note, dropped=upgrade_dropped)
    displayed_default = None
    if not _str(slide.get("title")) and data.get("title") and data["title"] == _DEFAULT_TITLES.get(source_layout):
        # "Agenda" on an agenda slide nobody titled is the model's default,
        # not the person's words: the target's own default applies instead
        # of carrying a heading that names the layout the slide is leaving.
        displayed_default = data.pop("title")

    source = _read_source(data)
    source.displayed_default_title = displayed_default
    payload, account = _build(source, data, target)
    try:
        converted = model.model_validate(payload).model_dump(mode="json", exclude_none=True)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in error.get('loc', ())) or 'slide'}: {error.get('msg', '')}"
            for error in exc.errors()[:3]
        )
        raise LayoutSwitchError(
            f"The slide could not be rebuilt as {_a_slide(target)} from what it holds "
            f"({problems}). Ask your assistant to move it instead."
        ) from exc
    return LayoutSwitch(
        slide=converted,
        kept=tuple(account.kept),
        derived=(*upgrade_note, *account.derived),
        dropped=(*upgrade_dropped, *account.dropped),
    )
