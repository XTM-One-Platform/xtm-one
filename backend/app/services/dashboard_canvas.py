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

"""What an absolute layout of a dashboard (a canvas in pixels) holds besides visualizations.

A tab whose layout is ``absolute`` is a canvas in pixels::

    layoutDefinitions.<id>: {type: "absolute",
                             options: {width, height, display, backgroundColor,
                                       backgroundImage: {fileId, sizeType, opacity}},
                             structure: [{item, type: block, position: {x, y, w, h}},
                                         {item, type: line,  position: {from, to}}]}

and may hold, besides the visualizations of data and content, elements that
draw no data (visualizations too, placed like them, refused on a grid):

* ``xtm.shape.rectangle`` / ``xtm.shape.ellipse``: fill, stroke, dash, corner radius;
* ``xtm.shape.line``: placed by a ``line`` entry whose ends are a point
  (``{x, y}``) or a connection to a block of the same layout (``{item, port}``,
  a port being the middle of a side), with an arrow at either end;
* ``xtm.icon``: a Lucide icon of the catalog decks use (``presentation_icons``)
  or an SVG uploaded to the dashboard (``fileId``), cleaned by ``svg_sanitizer``
  on the way in;
* ``xtm.image``: an image uploaded to the dashboard, with its alternative text;
* ``xtm.markdown``: text with its colour, size, family and alignment.

A ``fileId`` names a file uploaded to THIS dashboard (``POST
/custom-dashboards/{id}/assets``); the route serving it checks that, never this
module. Colours are hex (``#rgb``, ``#rrggbb``, ``#rrggbbaa``): what a person
picked, not a theme token. Every option is bounded and unknown keys are refused.
"""

from __future__ import annotations

import re
import uuid
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

#: The largest absolute canvas, in pixels (``dashboard_definition.MAX_CANVAS``).
MAX_CANVAS = 10_000
DEFAULT_CANVAS_WIDTH = 1440
DEFAULT_CANVAS_HEIGHT = 960
MAX_MARKDOWN_CHARS = 5_000
MAX_ALT_CHARS = 300

SHAPE_TYPES: tuple[str, ...] = ("xtm.shape.rectangle", "xtm.shape.ellipse", "xtm.shape.line")
#: The elements only a canvas holds: they draw no data source.
CANVAS_TYPES: tuple[str, ...] = (*SHAPE_TYPES, "xtm.icon", "xtm.image", "xtm.markdown")
LINE_TYPE = "xtm.shape.line"
PORTS: tuple[str, ...] = ("n", "e", "s", "w")
DISPLAY_MODES: tuple[str, ...] = ("auto", "actual-size", "fit-to-width")

HEX_COLOR_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")


class CanvasError(ValueError):
    """An option of a canvas or of one of its elements that failed validation."""


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, populate_by_name=True)


def _color(value: str | None) -> str | None:
    if value is not None and not HEX_COLOR_RE.match(value):
        raise ValueError("a colour is #rgb, #rrggbb or #rrggbbaa")
    return value


def _file_id(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        return str(uuid.UUID(value))
    except ValueError:
        raise ValueError("fileId is the id of a file uploaded to the dashboard") from None


Color = Annotated[str | None, Field(default=None, max_length=9)]
Opacity = Annotated[float, Field(default=1, ge=0, le=1)]
FileId = Annotated[str | None, Field(default=None, max_length=36, alias="fileId")]


# ── The canvas ───────────────────────────────────────────────────────────────


class BackgroundImage(_Model):
    file_id: Annotated[str, Field(min_length=1, max_length=36, alias="fileId")]
    #: ``auto``: its own size, top left; ``contain`` / ``cover`` the canvas.
    size_type: Annotated[Literal["auto", "contain", "cover"], Field(default="cover", alias="sizeType")]
    opacity: Opacity

    _file = field_validator("file_id")(_file_id)


class CanvasOptions(_Model):
    """An absolute layout's options: its size, how it fits the screen, its background."""

    width: Annotated[int, Field(default=DEFAULT_CANVAS_WIDTH, ge=100, le=MAX_CANVAS, strict=True)]
    height: Annotated[int, Field(default=DEFAULT_CANVAS_HEIGHT, ge=100, le=MAX_CANVAS, strict=True)]
    #: ``auto``: scaled down to the screen, never up; ``actual-size``: its pixels,
    #: scrolled; ``fit-to-width``: scaled to the screen's width, up or down.
    display: Literal[DISPLAY_MODES] = "auto"  # type: ignore[valid-type]
    background_color: Annotated[Color, Field(alias="backgroundColor")]
    background_image: Annotated[BackgroundImage | None, Field(default=None, alias="backgroundImage")]

    _color = field_validator("background_color")(_color)


# ── Line placement ───────────────────────────────────────────────────────────


class LinePoint(_Model):
    x: Annotated[int, Field(ge=0, le=MAX_CANVAS, strict=True)]
    y: Annotated[int, Field(ge=0, le=MAX_CANVAS, strict=True)]


class LineConnection(_Model):
    """An end held to the middle of one side of a block of the same layout."""

    item: Annotated[str, Field(min_length=1, max_length=60)]
    port: Literal[PORTS]  # type: ignore[valid-type]


LineEnd = LinePoint | LineConnection


class LinePosition(_Model):
    from_: Annotated[LineEnd, Field(alias="from")]
    to: LineEnd


# ── Elements ─────────────────────────────────────────────────────────────────

DashStyle = Annotated[Literal["solid", "dashed", "dotted"], Field(default="solid", alias="strokeDashStyle")]


class _Stroke(_Model):
    stroke_color: Annotated[Color, Field(alias="strokeColor")]
    stroke_width: Annotated[int, Field(default=1, ge=0, le=50, strict=True, alias="strokeWidth")]
    stroke_opacity: Annotated[float, Field(default=1, ge=0, le=1, alias="strokeOpacity")]
    stroke_dash_style: DashStyle

    _stroke = field_validator("stroke_color")(_color)


class EllipseOptions(_Stroke):
    fill_color: Annotated[Color, Field(alias="fillColor")]
    fill_opacity: Annotated[float, Field(default=1, ge=0, le=1, alias="fillOpacity")]

    _fill = field_validator("fill_color")(_color)


class RectangleOptions(EllipseOptions):
    #: The corner radius, in pixels.
    rx: Annotated[int, Field(default=0, ge=0, le=1_000, strict=True)]


class LineOptions(_Stroke):
    from_arrow: Annotated[bool, Field(default=False, alias="fromArrow")]
    to_arrow: Annotated[bool, Field(default=False, alias="toArrow")]


class IconOptions(_Model):
    """A Lucide icon by its id, or an SVG uploaded to the dashboard: exactly one."""

    icon: Annotated[str | None, Field(default=None, max_length=80)]
    file_id: FileId
    color: Color
    opacity: Opacity
    alt: Annotated[str | None, Field(default=None, max_length=MAX_ALT_CHARS)]

    _file = field_validator("file_id")(_file_id)
    _color = field_validator("color")(_color)

    @field_validator("icon")
    @classmethod
    def _known(cls, value: str | None) -> str | None:
        if value is None:
            return None
        from app.services.presentation_icons import normalize_icon_name, suggest_icon_names

        name = normalize_icon_name(value)
        if name is None:
            close = suggest_icon_names(value)
            hint = f" (close: {', '.join(close)})" if close else ""
            raise ValueError(f"no Lucide icon is named '{value[:80]}'{hint}")
        return name


class ImageOptions(_Model):
    file_id: FileId
    alt: Annotated[str | None, Field(default=None, max_length=MAX_ALT_CHARS)]
    #: ``contain`` keeps it whole, ``cover`` fills the box, ``fill`` stretches it.
    fit: Literal["contain", "cover", "fill"] = "contain"
    opacity: Opacity

    _file = field_validator("file_id")(_file_id)


class MarkdownOptions(_Model):
    markdown: Annotated[str, Field(default="", max_length=MAX_MARKDOWN_CHARS)]
    font_color: Annotated[Color, Field(alias="fontColor")]
    background_color: Annotated[Color, Field(alias="backgroundColor")]
    font_size: Annotated[Literal["small", "default", "large", "extraLarge"], Field(default="default", alias="fontSize")]
    font_family: Annotated[Literal["sans", "serif", "mono"], Field(default="sans", alias="fontFamily")]
    text_align: Annotated[Literal["left", "center", "right"], Field(default="left", alias="textAlign")]

    _colors = field_validator("font_color", "background_color")(_color)


_OPTIONS: dict[str, type[_Model]] = {
    "xtm.shape.rectangle": RectangleOptions,
    "xtm.shape.ellipse": EllipseOptions,
    "xtm.shape.line": LineOptions,
    "xtm.icon": IconOptions,
    "xtm.image": ImageOptions,
    "xtm.markdown": MarkdownOptions,
}


def _message(exc: ValidationError) -> str:
    parts = []
    for err in exc.errors()[:5]:
        loc = ".".join(str(part) for part in err["loc"])
        msg = str(err.get("msg", "invalid value")).removeprefix("Value error, ")
        parts.append(f"{loc}: {msg}" if loc else msg)
    return "; ".join(parts)


def validate_element_options(vtype: str, options: dict) -> dict:
    """The options of a canvas element of *vtype*, normalized (camelCase keys, defaults left out)."""
    try:
        model = _OPTIONS[vtype].model_validate(options)
    except ValidationError as exc:
        raise CanvasError(_message(exc)) from None
    if isinstance(model, IconOptions) and (model.icon is None) == (model.file_id is None):
        raise CanvasError("an icon names either 'icon' (a Lucide icon) or 'fileId' (an uploaded SVG)")
    return model.model_dump(mode="json", by_alias=True, exclude_none=True, exclude_defaults=True)


def validate_canvas_options(options: dict) -> dict:
    """An absolute layout's options, normalized (size always written, defaults otherwise left out)."""
    try:
        model = CanvasOptions.model_validate(options)
    except ValidationError as exc:
        raise CanvasError(_message(exc)) from None
    dumped = model.model_dump(mode="json", by_alias=True, exclude_none=True, exclude_defaults=True)
    return {"width": model.width, "height": model.height, **dumped}


def port_point(position: dict, port: str) -> dict:
    """The middle of one side of a block placed at *position* (``{x, y, w, h}``)."""
    x, y, w, h = position["x"], position["y"], position["w"], position["h"]
    point = {"n": (x + w // 2, y), "e": (x + w, y + h // 2), "s": (x + w // 2, y + h), "w": (x, y + h // 2)}[port]
    return {"x": point[0], "y": point[1]}


def detach_lines(layout: dict, item: str) -> None:
    """Turn every line end held to *item* into the point it was at: *item* is leaving *layout*."""
    blocks = {e.get("item"): e.get("position") for e in layout.get("structure") or [] if e.get("type", "block") == "block"}
    position = blocks.get(item)
    for entry in layout.get("structure") or []:
        if entry.get("type") != "line":
            continue
        ends = entry.get("position") or {}
        for key in ("from", "to"):
            end = ends.get(key)
            if isinstance(end, dict) and end.get("item") == item:
                ends[key] = port_point(position, end.get("port", "e")) if isinstance(position, dict) else {"x": 0, "y": 0}


def file_ids(definition: dict) -> set[str]:
    """Every file a definition draws: canvas backgrounds, images and uploaded icons."""
    found: set[str] = set()
    for layout in ((definition.get("layout") or {}).get("layoutDefinitions") or {}).values():
        image = ((layout or {}).get("options") or {}).get("backgroundImage")
        if isinstance(image, dict) and isinstance(image.get("fileId"), str):
            found.add(image["fileId"])
    for viz in (definition.get("visualizations") or {}).values():
        if isinstance(viz, dict) and viz.get("type") in ("xtm.icon", "xtm.image"):
            fid = (viz.get("options") or {}).get("fileId")
            if isinstance(fid, str):
                found.add(fid)
    return found


def check_structure(lid: str, layout: Any, visualizations: dict) -> None:
    """The cross-checks a canvas needs: elements only on a canvas, lines placed as lines, held to blocks of it."""
    blocks = {e.item for e in layout.structure if e.type == "block"}
    for index, entry in enumerate(layout.structure):
        where = f"layout.layoutDefinitions.{lid}.structure.{index}"
        vtype = getattr(visualizations.get(entry.item), "type", None)
        if entry.type == "input":
            continue
        if layout.type == "grid" and vtype in CANVAS_TYPES:
            raise ValueError(f"{where}: a {vtype} element sits on an absolute layout, not a grid")
        if (entry.type == "line") != (vtype == LINE_TYPE):
            raise ValueError(f"{where}: an {LINE_TYPE} visualization is placed by a 'line' entry, and only it")
        if entry.type != "line":
            continue
        for key in ("from_", "to"):
            end = getattr(entry.position, key)
            if isinstance(end, LineConnection) and (end.item not in blocks or end.item == entry.item):
                raise ValueError(f"{where}.position.{key.rstrip('_')}: '{end.item}' is no block of this layout")


__all__ = [
    "CANVAS_TYPES",
    "DISPLAY_MODES",
    "LINE_TYPE",
    "CanvasError",
    "LinePosition",
    "check_structure",
    "detach_lines",
    "file_ids",
    "port_point",
    "validate_canvas_options",
    "validate_element_options",
]
