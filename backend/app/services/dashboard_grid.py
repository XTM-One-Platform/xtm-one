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

"""Where the widgets of a custom dashboard sit: a 12-column grid.

Each widget has a ``layout`` ``{x, y, w, h}``: ``x`` and ``w`` in columns,
``y`` and ``h`` in rows of OpenCTI's grid, which a custom dashboard uses as its
own (``dev-docs/dashboard-studio-next.md`` D8: a 62 px row and a 20 px gap, so
82 px from one row to the next), so an OpenCTI widget sits where it sat in
OpenCTI. A widget
written without one (an older dashboard, an agent that gave only a
``width``) is placed by :func:`fill_layouts`: below the placed widgets, in
order, as wide as its ``width`` and as tall as its kind needs. Below the
grid's breakpoint every renderer stacks the widgets in reading order
(:func:`reading_order`).

OpenAEV uses the same 12 columns with shorter rows; a layout is converted by
the row pitch of each platform (:func:`rows_from_platform`,
:func:`rows_to_platform`), OpenCTI's unchanged.

Dashboards, versions and widget packs stored before D8 hold rows of 36 px
(:data:`LEGACY_ROW_PITCH`); they are read in today's rows when they are loaded
(:func:`legacy_rows`, the models' ``load`` listeners), never rewritten in bulk.
"""

from __future__ import annotations

GRID_COLUMNS = 12
#: The tallest a widget may be, in rows (the frontend's ``MAX_WIDGET_HEIGHT``).
MAX_ROWS = 40

#: Pixels from one grid row to the next: the row height plus the gap. XTM One's grid is OpenCTI's.
ROW_PITCH = {"xtm": 82, "opencti": 82, "openaev": 70}
#: The row pitch of the grid custom dashboards had before D8 (20 px rows, 16 px gaps).
LEGACY_ROW_PITCH = 36
#: Marks a stored form whose rows are today's (``dashboard_native``, widget packs).
GRID_UNIT = "opencti"

_WIDTH_COLUMNS = {"full": 12, "half": 6, "third": 4}

#: The rows a widget of a kind takes when nothing says otherwise.
_KIND_ROWS = {
    "score": 3,
    "stats": 2,
    "stages": 2,
    "decision": 3,
    "feed": 4,
    "callout": 2,
    "ranked_list": 3,
    "steps": 3,
    "checklist": 3,
    "progress": 2,
    "prompts": 2,
    "text": 2,
}
#: The rows of a platform widget by how it is drawn; a type drawn as a link is short.
_VIEW_ROWS = {"number": 3, "list": 4, "table": 4}
_CHART_ROWS = 4
_LINK_ROWS = 2
#: The old ``size`` of a platform widget, as rows.
_SIZE_ROWS = {"s": 3, "m": 4, "l": 5}


def width_columns(width: object) -> int:
    """The columns of a ``width`` (``full``, ``half``, ``third``)."""
    return _WIDTH_COLUMNS.get(width, GRID_COLUMNS) if isinstance(width, str) else GRID_COLUMNS


def width_of(columns: int) -> str:
    """The ``width`` closest to *columns*: what a reader without the grid shows."""
    if columns <= 4:
        return "third"
    if columns <= 6:
        return "half"
    return "full"


def default_rows(widget: dict) -> int:
    """The rows *widget* takes when it has no layout."""
    kind = widget.get("kind")
    if kind == "platform":
        view = widget.get("view")
        if view is None:
            return _LINK_ROWS
        if view in _VIEW_ROWS:
            return _VIEW_ROWS[view]
        return _SIZE_ROWS.get(widget.get("size"), _CHART_ROWS)
    return _KIND_ROWS.get(kind, 3) if isinstance(kind, str) else 3


def _valid(layout: object) -> bool:
    if not isinstance(layout, dict):
        return False
    values = [layout.get(key) for key in ("x", "y", "w", "h")]
    if any(isinstance(v, bool) or not isinstance(v, int) for v in values):
        return False
    x, y, w, h = values
    return x >= 0 and y >= 0 and 1 <= w <= GRID_COLUMNS and x + w <= GRID_COLUMNS and 1 <= h <= MAX_ROWS


def _flow(sizes: list[tuple[int, int]], top: int) -> list[dict]:
    """Place widgets of *sizes* (``(w, h)``) in rows from row *top*, left to right, in order."""
    placed: list[dict] = []
    x, y, row = 0, top, 0
    for w, h in sizes:
        if x + w > GRID_COLUMNS:
            x, y, row = 0, y + row, 0
        placed.append({"x": x, "y": y, "w": w, "h": h})
        x += w
        row = max(row, h)
    return placed


def _size(widget: dict) -> tuple[int, int]:
    layout = widget.get("layout")
    if _valid(layout):
        return layout["w"], layout["h"]
    return width_columns(widget.get("width")), default_rows(widget)


def bottom(widgets: list[dict]) -> int:
    """The first row below every placed widget."""
    return max((w["layout"]["y"] + w["layout"]["h"] for w in widgets if _valid(w.get("layout"))), default=0)


def fill_layouts(widgets: list[dict]) -> list[dict]:
    """Give every widget a layout: the placed ones keep theirs, the others go below them, in order."""
    missing = [w for w in widgets if not _valid(w.get("layout"))]
    if not missing:
        return widgets
    for widget, layout in zip(missing, _flow([_size(w) for w in missing], bottom(widgets)), strict=True):
        widget["layout"] = layout
    return widgets


def reflow(widgets: list[dict]) -> list[dict]:
    """Place every widget again in rows, in list order, each keeping its size."""
    for widget, layout in zip(widgets, _flow([_size(w) for w in widgets], 0), strict=True):
        widget["layout"] = layout
    return widgets


def reading_order(widgets: list[dict]) -> list[dict]:
    """*widgets* top to bottom, left to right; unplaced ones last, as given."""

    def key(pair: tuple[int, dict]) -> tuple[int, int, int, int]:
        index, widget = pair
        layout = widget.get("layout")
        return (0, layout["y"], layout["x"], index) if _valid(layout) else (1, 0, 0, index)

    return [w for _, w in sorted(enumerate(widgets), key=key)]


# ── The platforms' grids ─────────────────────────────────────────────────────


def _int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return round(value)


def legacy_rows(y: int, h: int) -> tuple[int, int]:
    """A place stored in rows of 36 px (``y``, ``h``) in today's rows.

    The top and the bottom are converted, not the height: two widgets that
    touched still touch, and none overlaps the next.
    """
    top = round(y * LEGACY_ROW_PITCH / ROW_PITCH["xtm"])
    bottom_row = round((y + h) * LEGACY_ROW_PITCH / ROW_PITCH["xtm"])
    return top, min(max(bottom_row - top, 1), MAX_ROWS)


def legacy_layout(layout: object) -> object:
    """A stored ``{x, y, w, h}`` in rows of 36 px, in today's rows; anything else as it is."""
    if not isinstance(layout, dict) or not all(isinstance(layout.get(k), int) and not isinstance(layout.get(k), bool) for k in ("y", "h")):
        return layout
    y, h = legacy_rows(layout["y"], layout["h"])
    return {**layout, "y": y, "h": h}


def rows_from_platform(rows: int, platform: str) -> int:
    """*rows* of *platform*'s grid as XTM One rows."""
    return max(1, round(rows * ROW_PITCH[platform] / ROW_PITCH["xtm"]))


def rows_to_platform(rows: int, platform: str) -> int:
    """XTM One *rows* as rows of *platform*'s grid."""
    return max(1, round(rows * ROW_PITCH["xtm"] / ROW_PITCH[platform]))


def layout_from_platform(x: object, y: object, w: object, h: object, platform: str) -> dict | None:
    """A platform widget's ``x, y, w, h`` as an XTM One layout, ``None`` when it does not say."""
    values = [_int(v) for v in (x, y, w, h)]
    if any(v is None for v in values):
        return None
    px, py, pw, ph = values  # type: ignore[misc]
    pw = min(max(pw, 1), GRID_COLUMNS)
    px = min(max(px, 0), GRID_COLUMNS - pw)
    return {
        "x": px,
        "y": rows_from_platform(max(py, 0), platform) if py > 0 else 0,
        "w": pw,
        "h": min(rows_from_platform(max(ph, 1), platform), MAX_ROWS),
    }


def layout_to_platform(layout: dict, platform: str) -> dict:
    """An XTM One layout as ``{x, y, w, h}`` of *platform*'s grid."""
    return {
        "x": layout["x"],
        "y": rows_to_platform(layout["y"], platform) if layout["y"] > 0 else 0,
        "w": layout["w"],
        "h": rows_to_platform(layout["h"], platform),
    }


__all__ = [
    "GRID_COLUMNS",
    "GRID_UNIT",
    "LEGACY_ROW_PITCH",
    "MAX_ROWS",
    "ROW_PITCH",
    "bottom",
    "default_rows",
    "fill_layouts",
    "layout_from_platform",
    "layout_to_platform",
    "legacy_layout",
    "legacy_rows",
    "reading_order",
    "reflow",
    "rows_from_platform",
    "rows_to_platform",
    "width_columns",
    "width_of",
]
