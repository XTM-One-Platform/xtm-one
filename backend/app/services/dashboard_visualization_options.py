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

"""The options of a dashboard visualization that draws data, by type, and the dynamic options grammar.

A data visualization (``dashboard_definition.DATA_TYPES``) carries its
options under their camelCase names (``stackMode``,
``legendDisplay``, ``majorValue``, ``columnFormat``...). Each family has a
closed model here: an unknown key, an enum outside its list, a number out of
bounds or a malformed colour is refused with the field's path, like the rest of
the definition. The frontend reads the same options defensively
(``components/custom-dashboard/charts/chart-options.ts``).

An option may be an expression of the dynamic options subset
(``> primary | seriesByName('count') | lastPoint() | rangeValue(ranges)``),
evaluated by the browser on the closed data
(``charts/dynamic-options.ts``); :func:`check_expression` checks its grammar
here so a broken one is a 422 rather than a silently blank widget. Nothing
evaluates an expression on the server.

A colour is a named design-system colour (:data:`NAMED_COLORS`) or a strict
``#rgb`` / ``#rrggbb``; never a CSS expression.
"""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, ValidationError
from pydantic.alias_generators import to_camel

from app.services.custom_dashboard_spec import Action

#: An expression's grammar limits (``dynamic-options.ts``).
MAX_EXPRESSION_CHARS = 500
MAX_CALLS = 10

#: Each function of the subset with the kinds of its arguments: a quoted string,
#: a number, or a reference to an entry of the visualization's ``context``.
DOS_FUNCTIONS: dict[str, tuple[str, ...]] = {
    "seriesByName": ("string",),
    "seriesByIndex": ("number",),
    "firstPoint": (),
    "lastPoint": (),
    "delta": ("number",),
    "min": (),
    "max": (),
    "sum": (),
    "rangeValue": ("ref",),
    "matchValue": ("ref",),
    "gradient": ("ref",),
    "formatByType": ("ref",),
    "prefix": ("string",),
}

#: The colours a definition may name (``charts/colors.ts`` ``NAMED_COLORS``).
NAMED_COLORS: frozenset[str] = frozenset(
    {"accent", "brand", "ai", "success", "warning", "alert", "danger", "info", "neutral", "text", "muted"}
    | {f"chart{index}" for index in range(1, 11)}
)

_HEX_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,59}$")
_TOKEN_RE = re.compile(
    r"""\s*(?:(?P<pipe>\|)|(?P<open>\()|(?P<close>\))|(?P<comma>,)"""
    r"""|(?P<string>'(?:[^'\\]|\\.){0,200}'|"(?:[^"\\]|\\.){0,200}")"""
    r"""|(?P<number>-?\d+(?:\.\d+)?)|(?P<ident>[A-Za-z_][A-Za-z0-9_]*))"""
)


def is_expression(value: object) -> bool:
    return isinstance(value, str) and value.lstrip().startswith(">")


def _tokens(text: str) -> list[tuple[str, str]]:
    tokens: list[tuple[str, str]] = []
    index = 0
    while index < len(text):
        if not text[index:].strip():
            break
        match = _TOKEN_RE.match(text, index)
        if not match:
            raise ValueError(f"unexpected text at {index}")
        index = match.end()
        kind = match.lastgroup or ""
        tokens.append((kind, match.group(kind)))
    return tokens


def check_expression(text: str) -> str:
    """*text* if it is an expression of the subset; else a ``ValueError`` saying why."""
    if not is_expression(text):
        raise ValueError("an expression starts with '>'")
    if len(text) > MAX_EXPRESSION_CHARS:
        raise ValueError(f"an expression holds at most {MAX_EXPRESSION_CHARS} characters")
    tokens = _tokens(text.lstrip()[1:])
    if not tokens or tokens[0][0] != "ident" or not _IDENT_RE.match(tokens[0][1]):
        raise ValueError("an expression starts with a source name (primary, annotation or an option)")
    at, calls = 1, 0
    while at < len(tokens):
        if tokens[at][0] != "pipe":
            raise ValueError("steps are separated by '|'")
        at += 1
        kind, name = tokens[at] if at < len(tokens) else ("", "")
        if kind != "ident" or name not in DOS_FUNCTIONS:
            raise ValueError(f"unknown function '{name}'; use one of {', '.join(DOS_FUNCTIONS)}")
        at += 1
        if at >= len(tokens) or tokens[at][0] != "open":
            raise ValueError(f"{name} needs '('")
        at += 1
        args: list[str] = []
        while at < len(tokens) and tokens[at][0] != "close":
            arg_kind = tokens[at][0]
            if arg_kind not in ("string", "number", "ident"):
                raise ValueError(f"unexpected token in {name}()")
            args.append("ref" if arg_kind == "ident" else arg_kind)
            at += 1
            if at < len(tokens) and tokens[at][0] == "comma":
                at += 1
            elif at >= len(tokens) or tokens[at][0] != "close":
                raise ValueError(f"{name}() needs ')'")
        if at >= len(tokens):
            raise ValueError(f"{name}() needs ')'")
        at += 1
        if tuple(args) != DOS_FUNCTIONS[name]:
            raise ValueError(f"{name}() takes ({', '.join(DOS_FUNCTIONS[name])})")
        calls += 1
        if calls > MAX_CALLS:
            raise ValueError(f"an expression has at most {MAX_CALLS} steps")
    return text


def _color(value: str) -> str:
    if is_expression(value):
        return check_expression(value)
    if value in NAMED_COLORS or _HEX_RE.match(value):
        return value
    raise ValueError(f"a colour is #rgb, #rrggbb or one of {', '.join(sorted(NAMED_COLORS))} (or an expression)")


def _static_color(value: str) -> str:
    if value in NAMED_COLORS or _HEX_RE.match(value):
        return value
    raise ValueError(f"a colour is #rgb, #rrggbb or one of {', '.join(sorted(NAMED_COLORS))}")


def _icon(value: str) -> str:
    from app.services.presentation_icons import normalize_icon_name, suggest_icon_names

    name = normalize_icon_name(value)
    if name is None:
        hint = suggest_icon_names(value)
        raise ValueError(f"unknown icon '{value}'" + (f"; did you mean {', '.join(hint)}?" if hint else ""))
    return name


#: A colour, or an expression giving one (or one per point).
Color = Annotated[str, Field(max_length=MAX_EXPRESSION_CHARS), AfterValidator(_color)]
StaticColor = Annotated[str, Field(max_length=7), AfterValidator(_static_color)]
Expression = Annotated[str, Field(max_length=MAX_EXPRESSION_CHARS), AfterValidator(check_expression)]
#: One value: a number, a text, or an expression giving it.
ValueOption = float | Annotated[str, Field(max_length=MAX_EXPRESSION_CHARS), AfterValidator(lambda v: check_expression(v) if is_expression(v) else v)]
#: A list of values, or an expression giving one.
ValuesOption = Expression | Annotated[list[float | Annotated[str, Field(max_length=200)] | None], Field(max_length=1000)]
#: Colours, one per point: a list, or an expression giving one.
ColorsOption = Expression | Annotated[list[StaticColor], Field(max_length=1000)]
FieldName = Annotated[str, Field(min_length=1, max_length=120)]
Number = Annotated[float, Field(ge=-1e15, le=1e15)]
LegendDisplay = Literal["right", "left", "top", "bottom", "off"]
LegendTruncation = Literal["ellipsisEnd", "ellipsisMiddle", "ellipsisStart", "ellipsisOff"]
AxisTitle = Annotated[str, Field(max_length=80)]


class TrellisOptions(BaseModel):
    """A chart split into small multiples (``viz/trellis.ts``): one panel per value of a field."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, alias_generator=to_camel, populate_by_name=True)

    enabled: bool = True
    split_by: Annotated[str, Field(min_length=1, max_length=100)]
    columns: Annotated[int, Field(ge=1, le=6)] | Literal["auto"] = "auto"
    shared_scale: bool = True
    sort: Literal["name", "-name", "total", "-total"] = "name"


class _Options(BaseModel):
    """Every data visualization's options: their camelCase names, nothing else."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, alias_generator=to_camel, populate_by_name=True)

    #: The widget's control (an upgraded widget's ``link``).
    link: Action | None = None
    #: Small multiples (a chart split by a field).
    trellis: TrellisOptions | None = None
    #: How the frame writes the title, as a module writes its own (``capitalize``: each word's first letter up, as
    #: OpenCTI; ``caps``: small letter-spaced capitals, as OpenAEV), and the title it shows instead of the
    #: visualization's (OpenCTI's number heads its card with its type, its own title in the body).
    title_style: Literal["capitalize", "caps"] | None = None
    #: The accent its content is drawn in, the frame keeping the platform's: a module's own (``brand``, the
    #: design system's brand primary that OpenCTI and OpenAEV take theirs from).
    accent_color: StaticColor | None = None
    header_title: Annotated[str, Field(min_length=1, max_length=80)] | None = None
    #: What it says with nothing to show: ``period`` (XTM One's "No data for this period."), ``found``
    #: (OpenCTI's "No data has been found.", across the widget) or ``display`` (OpenAEV's "No data to
    #: display" under an inbox, across the widget).
    empty_message: Literal["period", "found", "display"] | None = None
    #: The frame's "Expand" always shown in its header (OpenAEV's coverage matrix), not only on hover.
    expand_shown: bool | None = None


#: The maps, graphs and flows (``charts/StudioViz.tsx``).
STUDIO_VIZ_TYPES = (
    "xtm.map",
    "xtm.choropleth.svg",
    "xtm.linkgraph",
    "xtm.networkgraph",
    "xtm.sankey",
    "xtm.parallelcoordinates",
    "xtm.timeline",
    "xtm.treemap",
)


class StudioVizOptions(_Options):
    """The options of a map, a graph or a flow (``charts/StudioViz.tsx``): read by its drawing, kept as written.

    The object is bounded like every visualization's options; an SVG choropleth's
    drawing is sanitized by the definition (``dashboard_definition._choropleth_svg``).
    """

    model_config = ConfigDict(extra="allow", str_strip_whitespace=True, alias_generator=to_camel, populate_by_name=True)


class TimelineOptions(StudioVizOptions):
    """A timeline: events in lanes on one time axis, or one card per event on both sides of a vertical line (``alternate``)."""

    timeline_style: Literal["lanes", "alternate"] | None = None


class MapOptions(StudioVizOptions):
    """A map: how its regions are coloured (by value classes, or by their ``level`` on the threat scale), its basemap,
    its markers, and the view it opens on (a Web Mercator zoom and centre; none: the whole region)."""

    region_colors: Literal["classes", "level"] | None = None
    basemap: Literal["surface", "contrast"] | None = None
    marker_color: StaticColor | None = None
    marker_size: Annotated[int, Field(ge=8, le=40)] | None = None
    marker_icon: Annotated[str, Field(min_length=1, max_length=60), AfterValidator(_icon)] | None = None
    zoom: Annotated[float, Field(ge=0, le=20)] | None = None
    center_lat: Annotated[float, Field(ge=-90, le=90)] | None = None
    center_lng: Annotated[float, Field(ge=-180, le=180)] | None = None
    #: Open on the first marker rather than the centre, when there is one.
    center_on_first_marker: bool | None = None


class _Axes(_Options):
    x_axis_title_text: AxisTitle | None = None
    y_axis_title_text: AxisTitle | None = None
    x_axis_title_visibility: Literal["show", "hide"] | None = None
    y_axis_title_visibility: Literal["show", "hide"] | None = None
    y_axis_scale: Literal["linear", "log"] | None = None
    y_axis_min: Number | None = None
    y_axis_max: Number | None = None
    x_axis_label_rotation: Literal[-90, -45, 0, 45, 90] | None = None


class _Palette(_Options):
    series_colors: Annotated[list[StaticColor], Field(max_length=20)] | None = None
    series_colors_by_field: Annotated[dict[FieldName, StaticColor], Field(max_length=50)] | None = None


class _Look(_Options):
    """How a chart looks, in words any visualization takes (``charts/look.ts``): what a module's own widget
    looks like, sent by its adapter as a presentation preset, or set by a person. All optional: unset, XTM One's look.
    """

    #: A named palette, in the theme's tint (``charts/palettes.ts``), and a colour drawn before it.
    palette: Literal["material", "materialMixed", "results"] | None = None
    palette_lead: StaticColor | None = None
    palette_no_pale: bool | None = None
    number_format: Literal["si", "siSpaced", "compact", "locale"] | None = None
    tooltip_content: Literal["labelValue", "label"] | None = None
    #: The line in the surface colour between slices, cells or bars (px).
    separator_width: Annotated[float, Field(ge=0, le=6)] | None = None
    open_links: bool | None = None
    #: What a widget draws, greyed, while it has nothing to show.
    empty_sample: Literal["status", "platforms", "ttp", "temporal", "radar"] | None = None
    fill: Literal["solid", "gradient"] | None = None
    fill_opacity_from: Annotated[float, Field(ge=0, le=1)] | None = None
    fill_opacity_to: Annotated[float, Field(ge=0, le=1)] | None = None
    bar_radius: Annotated[float, Field(ge=0, le=12)] | None = None
    #: A bar's thickness as a share of its band (%), and the gap between the bars of a group (px).
    bar_size: Annotated[float, Field(ge=10, le=100)] | None = None
    bar_gap: Annotated[float, Field(ge=0, le=12)] | None = None
    color_by_point: bool | None = None
    stack_totals: bool | None = None
    date_format: Literal["date", "longDate", "monthYear", "year"] | None = None
    x_axis_type: Literal["category", "time"] | None = None
    marker_size: Annotated[float, Field(ge=2, le=10)] | None = None
    label_max_length: Annotated[int, Field(ge=5, le=100)] | None = None
    label_cut: Literal["words", "chars"] | None = None
    #: Category labels that would overlap: ``rotate`` writes every one at -45 degrees (the default drops some).
    crowded_labels: Literal["rotate"] | None = None
    grid_lines: Literal["both", "horizontal", "vertical", "none"] | None = None
    grid_opacity: Annotated[float, Field(ge=0, le=0.5)] | None = None
    x_axis_font_size: Annotated[float, Field(ge=8, le=16)] | None = None
    y_axis_font_size: Annotated[float, Field(ge=8, le=16)] | None = None
    axis_label_color: StaticColor | None = None
    hide_axis_lines: bool | None = None
    value_tick_count: Annotated[int, Field(ge=1, le=10)] | None = None
    data_values_strong: bool | None = None
    #: A radar's radius (px), a donut's hole (% of its radius), a heatmap's colours by ranges of its values.
    radius: Annotated[float, Field(ge=40, le=400)] | None = None
    donut_hole_size: Annotated[float, Field(ge=10, le=90)] | None = None
    color_ranges: Literal["heat"] | None = None
    value_shades: bool | None = None
    #: The value axis laid out as ApexCharts lays it out (``charts/nice-scale.ts``): OpenCTI's and OpenAEV's charts.
    nice_ticks: bool | None = None
    #: The legend's marker and the colour of its labels (``text``: the text colour, not each series').
    legend_marker: Literal["circle", "square"] | None = None
    legend_label_color: StaticColor | None = None
    #: A pie at the top of its widget, as large as its width allows, its legend under it (ApexCharts').
    pie_align: Literal["center", "top"] | None = None


class XYOptions(_Axes, _Palette, _Look):
    """Area, line, column and bar charts."""

    stack_mode: Literal["auto", "stacked", "stacked100"] | None = None
    overlay_fields: FieldName | Annotated[list[FieldName], Field(max_length=10)] | None = None
    y2_fields: FieldName | Annotated[list[FieldName], Field(max_length=10)] | None = None
    show_overlay_y2_axis: bool | None = None
    y2_axis_title_text: AxisTitle | None = None
    y2_axis_scale: Literal["linear", "log"] | None = None
    y2_axis_min: Number | None = None
    y2_axis_max: Number | None = None
    data_values_display: Literal["off", "all", "minmax"] | None = None
    legend_display: LegendDisplay | None = None
    legend_truncation: LegendTruncation | None = None
    null_value_display: Literal["gaps", "zero", "connect"] | None = None
    point_colors: ColorsOption | None = None
    marker_display: Literal["off", "filled", "outlined"] | None = None
    line_width: Annotated[float, Field(ge=1, le=6)] | None = None
    area_opacity: Annotated[float, Field(ge=0, le=1)] | None = None
    annotation_color: StaticColor | None = None
    show_brush: bool | None = None
    result_limit: Annotated[int, Field(ge=10, le=1000)] | None = None


class PieOptions(_Palette, _Look):
    """Pie and donut."""

    collapse_threshold: Annotated[float, Field(ge=0, le=0.5)] | None = None
    collapse_label: Annotated[str, Field(min_length=1, max_length=40)] | None = None
    show_donut_hole: bool | None = None
    label_display: Literal["off", "values", "valuesAndPercentage", "percentage"] | None = None
    legend_display: LegendDisplay | None = None
    legend_truncation: LegendTruncation | None = None
    #: A polar area's slices' opacity.
    area_opacity: Annotated[float, Field(ge=0, le=1)] | None = None


class RadarOptions(_Palette, _Look):
    """A radar: one axis per label, one shape per series."""

    area_opacity: Annotated[float, Field(ge=0, le=1)] | None = None
    line_width: Annotated[float, Field(ge=1, le=6)] | None = None
    legend_display: LegendDisplay | None = None
    legend_truncation: LegendTruncation | None = None


class WordCloudOptions(_Palette, _Look):
    """A word cloud: each label sized by its value."""

    font_size_min: Annotated[int, Field(ge=8, le=32)] | None = None
    font_size_max: Annotated[int, Field(ge=12, le=96)] | None = None


class HeatmapOptions(_Look):
    """A heatmap: one row per series, one cell per point, coloured by ranges of its values (or from a lowest colour to a highest)."""

    color_min: StaticColor | None = None
    color_max: StaticColor | None = None
    data_values_display: Literal["off", "all"] | None = None
    legend_display: LegendDisplay | None = None


class ScatterOptions(_Axes, _Palette):
    """Scatter, bubble and punchcard."""

    x_field: FieldName | None = None
    y_field: FieldName | None = None
    size_field: FieldName | None = None
    category_field: FieldName | None = None
    legend_display: LegendDisplay | None = None
    bubble_size_min: Annotated[float, Field(ge=2, le=40)] | None = None
    bubble_size_max: Annotated[float, Field(ge=2, le=60)] | None = None


class SingleValueOptions(_Options):
    """A single value: its value, trend and sparkline, their formats and colours.

    ``valueStyle`` lays the value out: ``standard``; ``difference`` (a large figure under the change
    over its period, with its arrow); ``tile`` (an icon tile, a figure that counts up, a trend chip).
    """

    value_style: Literal["standard", "difference", "tile"] | None = None
    #: Where ``tile`` puts the change said in a sentence behind its info sign: beside the figure, or in the
    #: widget's header (OpenAEV's title row).
    value_info: Literal["body", "header"] | None = None

    major_value: ValueOption | None = None
    trend_value: ValueOption | None = None
    sparkline_values: ValuesOption | None = None
    trend_display: Literal["absolute", "percent", "off"] | None = None
    sparkline_display: Literal["below", "before", "after", "off"] | None = None
    show_sparkline_area_graph: bool | None = None
    unit: Annotated[str, Field(max_length=12)] | None = None
    unit_position: Literal["before", "after"] | None = None
    number_precision: Annotated[int, Field(ge=0, le=6)] | None = None
    should_use_thousand_separators: bool | None = None
    should_abbreviate_value: bool | None = None
    should_abbreviate_trend_value: bool | None = None
    under_label: Annotated[str, Field(max_length=80)] | None = None
    #: The label over the figure, beside its change (``difference``): OpenCTI's number names what it counts there.
    top_label: Annotated[str, Field(max_length=120)] | None = None
    major_color: Color | None = None
    trend_color: Color | None = None
    background_color: Color | None = None
    sparkline_stroke_color: Color | None = None


class SingleValueIconOptions(SingleValueOptions):
    icon: Annotated[str, Field(min_length=1, max_length=60), AfterValidator(_icon)] | None = None
    icon_color: Color | None = None
    icon_position: Literal["before", "after", "above"] | None = None
    show_value: bool | None = None


class SingleValueRadialOptions(SingleValueOptions):
    max_value: Number | None = None
    radial_stroke_color: Color | None = None
    radial_background_color: Color | None = None


class GaugeRange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    from_: Annotated[Number, Field(alias="from")]
    to: Number
    value: StaticColor | None = None


class GaugeOptions(_Options):
    """Filler and marker gauges."""

    value: ValueOption | None = None
    min_value: Number | None = None
    max_value: Number | None = None
    gauge_ranges: Annotated[list[GaugeRange], Field(max_length=10)] | None = None
    orientation: Literal["horizontal", "vertical"] | None = None
    gauge_color: Color | None = None
    label_display: Literal["number", "percentage", "off"] | None = None
    value_display: Literal["number", "percentage", "off"] | None = None
    unit: Annotated[str, Field(max_length=12)] | None = None
    number_precision: Annotated[int, Field(ge=0, le=6)] | None = None
    major_tick_interval: Annotated[float, Field(gt=0, le=1e15)] | None = None


class ListOptions(_Options):
    """A list of rows: ``compact`` (a name and the rest after it), ``table`` (a header, typed cells and an
    arrow column), ``rows`` (a leading icon, typed cells, a trailing arrow, no header) or ``cards``."""

    list_style: Literal["compact", "table", "rows", "cards"] | None = None
    row_height: Annotated[int, Field(ge=32, le=64)] | None = None
    cards_per_row: Annotated[int, Field(ge=1, le=6)] | None = None
    #: The colour of the arrow on the rows that open (muted by default).
    arrow_color: StaticColor | None = None


class DistributionOptions(_Options):
    """A distribution as a list: a bar under each label (``bars``), or a row per item with its icon and value (``rows``)."""

    list_style: Literal["bars", "rows"] | None = None


class ColumnFormat(BaseModel):
    model_config = ConfigDict(extra="forbid", alias_generator=to_camel, populate_by_name=True)

    width: Annotated[int, Field(ge=40, le=800)] | None = None
    align: Literal["left", "center", "right"] | None = None
    text_overflow: Literal["ellipsis", "break-word", "anywhere"] | None = None
    cell_type: Literal["text", "array", "sparkline"] | None = None
    data: Expression | None = None
    row_colors: ColorsOption | None = None
    row_background_colors: ColorsOption | None = None


class TableFormat(BaseModel):
    model_config = ConfigDict(extra="forbid", alias_generator=to_camel, populate_by_name=True)

    row_colors: ColorsOption | None = None
    row_background_colors: ColorsOption | None = None


class TableOptions(_Options):
    """A table: paging, header, row numbers, totals, column and row formats."""

    count: Annotated[int, Field(ge=1, le=100)] | None = None
    header_visibility: Literal["inline", "fixed", "none"] | None = None
    show_row_numbers: bool | None = None
    show_totals: bool | None = None
    column_format: Annotated[dict[FieldName, ColumnFormat], Field(max_length=20)] | None = None
    table_format: TableFormat | None = None


#: The validation views (``charts/OutcomeViews.tsx``): a technique matrix, a path graph, an exposure
#: console, a score gauge, a ring gauge and domain cards.
OUTCOME_VIZ_TYPES = (
    "xtm.outcomematrix",
    "xtm.pathgraph",
    "xtm.exposureconsole",
    "xtm.scoregauge",
    "xtm.ringgauge",
    "xtm.domaincards",
)


class OutcomeOptions(_Options):
    """A validation view: what it shows while its data has no count yet - a sample greyed out
    (``full``) or desaturated (``subtle``), or nothing (``off``, the default)."""

    sample_display: Literal["full", "subtle", "off"] | None = None


#: The options model of each data visualization type.
OPTIONS_BY_TYPE: dict[str, type[_Options]] = {
    "xtm.singlevalue": SingleValueOptions,
    "xtm.singlevalueicon": SingleValueIconOptions,
    "xtm.singlevalueradial": SingleValueRadialOptions,
    "xtm.fillergauge": GaugeOptions,
    "xtm.markergauge": GaugeOptions,
    "xtm.list": ListOptions,
    "xtm.distribution": DistributionOptions,
    "xtm.column": XYOptions,
    "xtm.bar": XYOptions,
    "xtm.line": XYOptions,
    "xtm.area": XYOptions,
    "xtm.pie": PieOptions,
    "xtm.scatter": ScatterOptions,
    "xtm.bubble": ScatterOptions,
    "xtm.punchcard": ScatterOptions,
    "xtm.table": TableOptions,
    "xtm.radar": RadarOptions,
    # A polar area is a pie whose slices share the circle and grow with their value.
    "xtm.polararea": PieOptions,
    "xtm.wordcloud": WordCloudOptions,
    "xtm.heatmap": HeatmapOptions,
    # The validation views (``outcomes`` data).
    **{viz_type: OutcomeOptions for viz_type in OUTCOME_VIZ_TYPES},
    "xtm.platformlink": _Options,
    # An answer in words (the ``text`` shape): drawn as its paragraphs, nothing to set.
    "xtm.answer": _Options,
    **{viz_type: StudioVizOptions for viz_type in STUDIO_VIZ_TYPES},
    "xtm.timeline": TimelineOptions,
    "xtm.map": MapOptions,
}


def _choices(annotation: Any) -> list | None:
    """The values a ``Literal`` annotation (or an optional one) allows; ``None`` for anything else."""
    from typing import Literal, get_args, get_origin

    if get_origin(annotation) is Literal:
        return list(get_args(annotation))
    for arg in get_args(annotation):
        found = _choices(arg)
        if found:
            return found
    return None


def option_names(viz_type: str) -> list[dict]:
    """The options *viz_type* takes, read from its model: each name as stored, and its values when they are a list."""
    model = OPTIONS_BY_TYPE.get(viz_type, _Options)
    described = []
    for name, info in model.model_fields.items():
        entry: dict = {"name": info.alias or name}
        choices = _choices(info.annotation)
        if choices:
            entry["values"] = choices
        described.append(entry)
    return described


def validate_options(viz_type: str, options: dict[str, Any]) -> dict[str, Any]:
    """A data visualization's options, checked against its type's model, as stored (camelCase).

    A ``ValueError`` names each wrong field, under ``options``.
    """
    model = OPTIONS_BY_TYPE.get(viz_type, _Options)
    try:
        checked = model.model_validate(options)
    except ValidationError as exc:
        problems = []
        for err in exc.errors()[:10]:
            where = ".".join(str(part) for part in err["loc"] if not str(part).startswith(("function-", "tagged-union")))
            message = str(err.get("msg", "invalid value")).removeprefix("Value error, ")
            problems.append(f"options.{where}: {message}" if where else f"options: {message}")
        raise ValueError("; ".join(problems)) from None
    return checked.model_dump(mode="json", by_alias=True, exclude_none=True)
