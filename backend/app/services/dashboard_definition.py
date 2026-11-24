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

"""The definition of a custom dashboard (version 2): data sources, visualizations and layout kept apart.

A dashboard is stored as one JSON definition::

    {version: 2, title, description,
     dataSources:    {id: {type, name, options}},           # where data comes from
     visualizations: {id: {type, title, description,       # what draws it
                           dataSources: {primary, annotation}, options, context,
                           eventHandlers, containerOptions}},
     inputs:         {id: {type, title, token, defaultValue, options, dataSources}},
     layout:         {tabs: {items: [{layoutId, label}], options},
                      layoutDefinitions: {id: {type: grid|absolute, options,
                                               structure: [{item, type, position}]}},
                      globalInputs: [input ids]},
     defaults, expressions, applicationProperties}

One data source may feed several visualizations; a visualization sits in a
tab's layout (a 12-column grid, ``dashboard_grid``, or an absolute canvas in
pixels, which also holds shapes, lines, icons, images and markdown:
``dashboard_canvas``). Everything is bounded and unknown keys are refused, like the widget
spec this replaces as the stored form.

Dashboards written before (``{title, subtitle, widgets}``, version 1) and the
widgets an agent or a pack still writes are read through :func:`upgrade`: a
content widget becomes a ``xtm.<kind>`` visualization holding its fields as
options, a platform widget a visualization of its view reading a ``platform``
data source of its own, and the widgets' grid places the first tab's layout.
:func:`widget_view` goes the other way, for what still speaks widgets (the
OpenCTI / OpenAEV converters, packs, the studio's inspector).
"""

from __future__ import annotations

import copy
import json
import logging
import re
import uuid
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from app.services.custom_dashboard_spec import (
    DASHBOARD_WIDGET_KINDS,
    MAX_SHORT_TEXT_CHARS,
    MAX_TITLE_CHARS,
    DashboardSpecError,
    validate_spec,
    validate_widget,
)
from app.services.dashboard_canvas import (
    CANVAS_TYPES,
    CanvasError,
    LinePosition,
    check_structure,
    detach_lines,
    validate_canvas_options,
    validate_element_options,
)
from app.services.dashboard_grid import GRID_COLUMNS, MAX_ROWS, fill_layouts, reflow, width_columns, width_of
from app.services.dashboard_pipeline import MAX_STEPS, Step
from app.services.dashboard_visualization_options import validate_options

logger = logging.getLogger(__name__)

DEFINITION_VERSION = 2

MAX_DATA_SOURCES = 40
MAX_VISUALIZATIONS = 60
MAX_INPUTS = 20
MAX_TABS = 10
MAX_STRUCTURE = 120
MAX_EVENT_HANDLERS = 10
#: Bytes of JSON one free-form object (options, context, defaults...) may hold.
MAX_OBJECT_BYTES = 16_384
#: Bytes of the whole definition.
MAX_DEFINITION_BYTES = 512_000
#: The largest absolute canvas, in pixels.
MAX_CANVAS = 10_000
#: Bytes of the SVG an ``xtm.choropleth.svg`` visualization draws its regions on (``options.svg``).
MAX_CHOROPLETH_SVG_BYTES = 200_000

#: An element id: a visualization, data source, input or layout.
ELEMENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,59}$")
#: A token name (``$name$``).
TOKEN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]{0,59}$")

#: Visualizations of XTM One content: the widget kinds, their fields as options.
CONTENT_TYPES: tuple[str, ...] = tuple(f"xtm.{kind}" for kind in DASHBOARD_WIDGET_KINDS if kind != "platform")
#: Visualizations drawing a data source.
DATA_TYPES: tuple[str, ...] = (
    "xtm.singlevalue",
    "xtm.list",
    "xtm.distribution",
    "xtm.column",
    "xtm.bar",
    "xtm.line",
    "xtm.area",
    "xtm.pie",
    "xtm.table",
    "xtm.scatter",
    "xtm.bubble",
    "xtm.punchcard",
    "xtm.singlevalueicon",
    "xtm.singlevalueradial",
    "xtm.fillergauge",
    "xtm.markergauge",
    "xtm.platformlink",
    "xtm.answer",  # an answer in words (the ``text`` shape): an agent's, an assignment's, a variable's
    # Maps, graphs and flows (``geo``, ``graph``, ``events``, ``tree`` data, or a table's columns).
    "xtm.map",
    "xtm.choropleth.svg",
    "xtm.linkgraph",
    "xtm.networkgraph",
    "xtm.sankey",
    "xtm.parallelcoordinates",
    "xtm.timeline",
    "xtm.treemap",
    # A distribution or a series drawn as a radar, a polar area, a word cloud or a heatmap.
    "xtm.radar",
    "xtm.polararea",
    "xtm.wordcloud",
    "xtm.heatmap",
    # Validation outcomes (``outcomes`` data).
    "xtm.outcomematrix",
    "xtm.pathgraph",
    "xtm.exposureconsole",
    "xtm.scoregauge",
    "xtm.ringgauge",
    "xtm.domaincards",
)
#: Canvas elements (``dashboard_canvas``): shapes, icons, images and markdown, on an absolute layout only.
VISUALIZATION_TYPES: tuple[str, ...] = CONTENT_TYPES + DATA_TYPES + CANVAS_TYPES
INPUT_TYPES: tuple[str, ...] = (
    "input.timerange",
    "input.dropdown",
    "input.multiselect",
    "input.text",
    "input.number",
    "input.button",
    "input.entity",
)

#: How a platform widget's view is drawn as a visualization, and back.
VIEW_TO_TYPE: dict[str | None, str] = {
    "number": "xtm.singlevalue",
    "list": "xtm.list",
    "distribution": "xtm.distribution",
    "bar": "xtm.column",
    "hbar": "xtm.bar",
    "line": "xtm.line",
    "area": "xtm.area",
    "donut": "xtm.pie",
    "table": "xtm.table",
    "scatter": "xtm.scatter",
    "bubble": "xtm.bubble",
    "punchcard": "xtm.punchcard",
    "icon": "xtm.singlevalueicon",
    "radial": "xtm.singlevalueradial",
    "filler": "xtm.fillergauge",
    "marker": "xtm.markergauge",
    "map": "xtm.map",
    "choroplethsvg": "xtm.choropleth.svg",
    "linkgraph": "xtm.linkgraph",
    "networkgraph": "xtm.networkgraph",
    "sankey": "xtm.sankey",
    "parallel": "xtm.parallelcoordinates",
    "timeline": "xtm.timeline",
    "treemap": "xtm.treemap",
    "radar": "xtm.radar",
    "polar": "xtm.polararea",
    "wordcloud": "xtm.wordcloud",
    "heatmap": "xtm.heatmap",
    "outcomematrix": "xtm.outcomematrix",
    "pathgraph": "xtm.pathgraph",
    "exposureconsole": "xtm.exposureconsole",
    "scoregauge": "xtm.scoregauge",
    "ringgauge": "xtm.ringgauge",
    "domaincards": "xtm.domaincards",
    None: "xtm.platformlink",
}
TYPE_TO_VIEW: dict[str, str | None] = {value: key for key, value in VIEW_TO_TYPE.items()}

#: The validation views (``outcomes`` data).
OUTCOME_TYPES: tuple[str, ...] = tuple(VIEW_TO_TYPE[view] for view in ("outcomematrix", "pathgraph", "exposureconsole", "scoregauge", "ringgauge", "domaincards"))

#: Validation views XTM One drew before it reproduced its modules' own, and the view each became
#: (a stored definition is upgraded on read, :func:`upgrade`). A platform widget takes the view its
#: module draws it with when that is one of the replacements (an attack path drawn as a grid
#: becomes a path graph).
LEGACY_TYPES: dict[str, str] = {
    "xtm.validationgates": "xtm.exposureconsole",
    "xtm.exposuregauge": "xtm.scoregauge",
    "xtm.resiliencerings": "xtm.ringgauge",
    "xtm.outcomecards": "xtm.domaincards",
    "xtm.outcomegrid": "xtm.outcomematrix",
}
#: The same views as a version 1 platform widget names them.
LEGACY_VIEWS: dict[str, str] = {
    "gates": "xtm.validationgates",
    "exposure": "xtm.exposuregauge",
    "resilience": "xtm.resiliencerings",
    "outcomecards": "xtm.outcomecards",
    "outcomegrid": "xtm.outcomegrid",
}

FIRST_LAYOUT = "layout_1"


class DefinitionError(DashboardSpecError):
    """A definition or an edit of one that failed validation; the message says where."""


def _bounded_object(value: object, where: str) -> object:
    if not isinstance(value, dict):
        raise ValueError(f"{where} must be a JSON object")
    if len(json.dumps(value, separators=(",", ":"), default=str)) > MAX_OBJECT_BYTES:
        raise ValueError(f"{where} holds more than {MAX_OBJECT_BYTES} bytes")
    return value


def _check_id(value: str) -> str:
    if not ELEMENT_ID_RE.match(value):
        raise ValueError("an id is 1-60 letters, digits, '_', '-' or '.', starting with a letter or digit")
    return value


ElementId = Annotated[str, Field(min_length=1, max_length=60)]
FreeObject = Annotated[dict[str, Any], Field(default_factory=dict)]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, populate_by_name=True)


# ── Data sources ─────────────────────────────────────────────────────────────


#: The closed shapes a data source answers in (``dashboard_live``).
Shape = Literal["number", "series", "distribution", "rows", "text", "geo", "graph", "events", "tree", "outcomes"]

#: How often a source is read again: ``30s`` to ``24h``.
REFRESH_RE = re.compile(r"^(\d{1,5})([smh])$")
MIN_REFRESH_SECONDS = 30
MAX_REFRESH_SECONDS = 86_400
#: How many derived sources a chain may stack (a derived of a derived of ...).
MAX_CHAIN_DEPTH = 4
#: An answer's path to the part a source draws: ``a.b.0.c``.
PATH_RE = re.compile(r"^[A-Za-z0-9_-]{1,60}(?:\.[A-Za-z0-9_-]{1,60}){0,9}$")


def refresh_seconds(value: str | None) -> int | None:
    """``30s`` / ``5m`` / ``1h`` in seconds, ``None`` for no refresh."""
    if value is None:
        return None
    match = REFRESH_RE.match(value)
    if not match:
        return None
    return int(match.group(1)) * {"s": 1, "m": 60, "h": 3600}[match.group(2)]


class _Refreshed(_Model):
    """Read again every ``refresh`` (``delay``: after the last answer; ``interval``: on a fixed beat)."""

    refresh: Annotated[str | None, Field(default=None, max_length=8)]
    refresh_type: Annotated[Literal["delay", "interval"], Field(default="delay", alias="refreshType")]

    @field_validator("refresh")
    @classmethod
    def _refresh(cls, value: str | None) -> str | None:
        if value is None:
            return value
        seconds = refresh_seconds(value)
        if seconds is None or not MIN_REFRESH_SECONDS <= seconds <= MAX_REFRESH_SECONDS:
            raise ValueError("refresh is a duration from 30s to 24h, like '30s', '5m' or '1h'")
        return value


class PlatformSourceOptions(_Refreshed):
    """An OpenCTI / OpenAEV widget or an OpenCRQ metric, read live with the viewer's access."""

    source: Literal["opencti", "openaev", "opencrq"]
    integration_id: uuid.UUID
    native: dict
    #: The period read: a range key (``7d``...), fixed dates (``2026-01-01/2026-03-31``, an OpenCTI dashboard's
    #: own), or the token a time range input sets (``$time$``).
    range: Annotated[str | None, Field(default=None, max_length=110)]

    @field_validator("range")
    @classmethod
    def _range(cls, value: str | None) -> str | None:
        return check_range(value)

    @model_validator(mode="after")
    def _check_native(self) -> PlatformSourceOptions:
        from app.services.dashboard_platform import NativeError, check_native

        try:
            self.native = check_native(self.source, self.native).native
        except NativeError as exc:
            raise ValueError(str(exc)) from None
        return self


class StaticSourceOptions(_Model):
    """Data written in the definition (sample or test data): one of the closed shapes."""

    shape: Shape
    data: dict

    @model_validator(mode="after")
    def _closed(self) -> StaticSourceOptions:
        from app.services.dashboard_live import LiveError, closed_static_data

        try:
            self.data = closed_static_data(self.shape, self.data)
        except (TypeError, ValueError, KeyError, LiveError) as exc:
            raise ValueError(f"data does not fit the '{self.shape}' shape: {exc}") from None
        return self


class _AnswerOptions(_Refreshed):
    """An answer in words read as one shape: the text itself, or JSON holding the shape (at ``path``)."""

    shape: Shape = "text"
    path: Annotated[str | None, Field(default=None, max_length=600)]

    @field_validator("path")
    @classmethod
    def _path(cls, value: str | None) -> str | None:
        if value is not None and not PATH_RE.match(value):
            raise ValueError("path is up to 10 keys or indexes joined by '.', like 'response.findings'")
        return value


class AssignmentSourceOptions(_AnswerOptions):
    """The answer of an assignment's last completed run the viewer may see."""

    assignment_id: uuid.UUID


class AskSourceOptions(_AnswerOptions):
    """A question asked to an agent when the viewer asks for it (never on page load)."""

    agent_id: uuid.UUID
    question: Annotated[str, Field(min_length=1, max_length=2_000)]


class VariableSourceOptions(_Refreshed):
    """The value of a variable the viewer may read; a secret one is never drawn."""

    variable_id: uuid.UUID
    shape: Literal["text", "number"] = "text"


class DerivedSourceOptions(_Refreshed):
    """Another source's result through closed steps (a chain of steps, ``dashboard_pipeline``)."""

    extends: ElementId
    steps: Annotated[list[Step], Field(default_factory=list, max_length=MAX_STEPS)]


class PlatformDataSource(_Model):
    type: Literal["platform"]
    name: Annotated[str | None, Field(default=None, max_length=80)]
    options: PlatformSourceOptions


class StaticDataSource(_Model):
    type: Literal["static"]
    name: Annotated[str | None, Field(default=None, max_length=80)]
    options: StaticSourceOptions


class AssignmentDataSource(_Model):
    type: Literal["assignment"]
    name: Annotated[str | None, Field(default=None, max_length=80)]
    options: AssignmentSourceOptions


class AskDataSource(_Model):
    type: Literal["ask"]
    name: Annotated[str | None, Field(default=None, max_length=80)]
    options: AskSourceOptions


class VariableDataSource(_Model):
    type: Literal["variable"]
    name: Annotated[str | None, Field(default=None, max_length=80)]
    options: VariableSourceOptions


class DerivedDataSource(_Model):
    type: Literal["derived"]
    name: Annotated[str | None, Field(default=None, max_length=80)]
    options: DerivedSourceOptions


DataSource = Annotated[
    PlatformDataSource | StaticDataSource | AssignmentDataSource | AskDataSource | VariableDataSource | DerivedDataSource,
    Field(discriminator="type"),
]
#: The sources that read something of the platform's with the viewer's access (checked against the writer too).
REFERENCE_SOURCE_TYPES = ("platform", "assignment", "ask", "variable")


# ── Visualizations and inputs ────────────────────────────────────────────────


#: Where a clicked value is read from: the clicked name (a series, a slice, a column), its value, a row's field.
CLICK_KEY_RE = re.compile(r"^(name|value|x|y|row\.[A-Za-z0-9_.-]{1,100}\.value)$")


class TokenFromClick(_Model):
    token: Annotated[str, Field(min_length=1, max_length=60)]
    #: What the click gives (``name``, ``value``, ``x``, ``y``, ``row.<field>.value``), or ``value`` written here.
    key: Annotated[str | None, Field(default=None, max_length=110)]
    value: Annotated[str | None, Field(default=None, max_length=500)]

    @model_validator(mode="after")
    def _one(self) -> TokenFromClick:
        if not TOKEN_RE.match(self.token):
            raise ValueError("a token is 1-60 letters, digits, '_' or '.', starting with a letter or '_'")
        if (self.key is None) == (self.value is None):
            raise ValueError("a token takes what the click gives ('key') or a written 'value', one of the two")
        if self.key is not None and not CLICK_KEY_RE.match(self.key):
            raise ValueError("key is name, value, x, y or row.FIELD.value")
        return self


class TokenName(_Model):
    token: Annotated[str, Field(min_length=1, max_length=60)]


class _SetTokens(_Model):
    tokens: Annotated[list[TokenFromClick], Field(default_factory=list, max_length=10)]


class _UnsetTokens(_Model):
    tokens: Annotated[list[TokenName], Field(default_factory=list, max_length=10)]


class _LinkToUrl(_Model):
    url: Annotated[str, Field(min_length=1, max_length=1000)]
    new_tab: Annotated[bool, Field(default=False, alias="newTab")]

    @field_validator("url")
    @classmethod
    def _safe(cls, url: str) -> str:
        return LinkAction._safe(url)  # the same rule as an input's link


class _TokenValue(_Model):
    token: Annotated[str, Field(min_length=1, max_length=60, pattern=r"^[A-Za-z_][A-Za-z0-9_.]{0,59}$")]
    #: Written, with the page's tokens and the click's (``$click.value$``) put in.
    value: Annotated[str, Field(max_length=500)]


class _LinkToDashboard(_Model):
    dashboard_id: Annotated[uuid.UUID, Field(alias="dashboardId")]
    #: Tokens the other dashboard opens with (``?form.<token>=``).
    tokens: Annotated[list[_TokenValue], Field(default_factory=list, max_length=10)]
    new_tab: Annotated[bool, Field(default=False, alias="newTab")]


class _NoOptions(_Model):
    pass


class _SwitchTab(_Model):
    layout_id: Annotated[ElementId, Field(alias="layoutId")]


class _SetTimeRange(_Model):
    #: A brush over a series sets ``<token>.earliest`` and ``<token>.latest`` (its first and last x).
    token: Annotated[str, Field(min_length=1, max_length=50, pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,49}$")]


_HANDLER_OPTIONS: dict[str, type[_Model]] = {
    "drilldown.setToken": _SetTokens,
    "drilldown.unsetToken": _UnsetTokens,
    "drilldown.linkToUrl": _LinkToUrl,
    "drilldown.linkToDashboard": _LinkToDashboard,
    "drilldown.openInPlatform": _NoOptions,
    "drilldown.switchTab": _SwitchTab,
    "drilldown.setTimeRange": _SetTimeRange,
}


class EventHandler(_Model):
    """What a click (or a brush) on a visualization does; a visualization runs its handlers in order."""

    type: Literal[tuple(_HANDLER_OPTIONS)]  # type: ignore[valid-type]
    options: FreeObject

    @model_validator(mode="after")
    def _typed(self) -> EventHandler:
        self.options = _HANDLER_OPTIONS[self.type].model_validate(self.options).model_dump(by_alias=True, exclude_none=True, mode="json")
        return self


class Visibility(_Model):
    """When a visualization shows: every show condition true, no hide condition true, and data unless asked."""

    show_conditions: Annotated[list[ElementId], Field(default_factory=list, max_length=10, alias="showConditions")]
    hide_conditions: Annotated[list[ElementId], Field(default_factory=list, max_length=10, alias="hideConditions")]
    hide_when_no_data: Annotated[bool, Field(default=False, strict=True, alias="hideWhenNoData")]


class ContainerOptions(_Model):
    visibility: Visibility = Field(default_factory=Visibility)


def _choropleth_svg(value: object) -> str:
    """The SVG a choropleth draws on, through the template assets' sanitizer: nothing runs, nothing reaches out."""
    from app.services.svg_sanitizer import SvgSanitizeError, sanitize_svg

    if not isinstance(value, str) or not value.strip():
        raise ValueError("an xtm.choropleth.svg visualization needs options.svg: the SVG whose element ids are its regions")
    raw = value.encode("utf-8")
    if len(raw) > MAX_CHOROPLETH_SVG_BYTES:
        raise ValueError(f"options.svg holds more than {MAX_CHOROPLETH_SVG_BYTES} bytes")
    try:
        clean = sanitize_svg(raw)
    except SvgSanitizeError as exc:
        raise ValueError(f"options.svg is not a usable SVG: {exc}") from None
    return clean.decode("utf-8")


class Visualization(_Model):
    type: Literal[VISUALIZATION_TYPES]  # type: ignore[valid-type]
    title: Annotated[str | None, Field(default=None, max_length=MAX_TITLE_CHARS)]
    description: Annotated[str | None, Field(default=None, max_length=MAX_SHORT_TEXT_CHARS)]
    data_sources: Annotated[dict[Literal["primary", "annotation"], ElementId], Field(default_factory=dict, alias="dataSources")]
    options: FreeObject
    context: FreeObject
    event_handlers: Annotated[list[EventHandler], Field(default_factory=list, max_length=MAX_EVENT_HANDLERS, alias="eventHandlers")]
    container_options: Annotated[ContainerOptions, Field(default_factory=ContainerOptions, alias="containerOptions")]
    #: Panel chrome: a bar while the data is read again, the time it was read.
    show_progress_bar: Annotated[bool | None, Field(default=None, alias="showProgressBar")]
    show_last_updated: Annotated[bool | None, Field(default=None, alias="showLastUpdated")]

    @field_validator("options", "context")
    @classmethod
    def _bounded(cls, value: dict, info: Any) -> dict:
        if info.field_name == "options" and info.data.get("type") == "xtm.choropleth.svg" and isinstance(value, dict):
            # The SVG has a room of its own; the other options keep the common bound.
            _bounded_object({k: v for k, v in value.items() if k != "svg"}, "options")
            return value
        return _bounded_object(value, info.field_name)  # type: ignore[return-value]

    @model_validator(mode="after")
    def _by_type(self) -> Visualization:
        if self.type == "xtm.choropleth.svg":
            self.options = {**self.options, "svg": _choropleth_svg(self.options.get("svg"))}
        if self.type in CONTENT_TYPES:
            kind = self.type.removeprefix("xtm.")
            widget = validate_widget({**self.options, "kind": kind}, where="options")
            self.options = {k: v for k, v in widget.items() if k not in ("kind", "width", "layout", "id", "title")}
            if self.data_sources:
                raise ValueError(f"a {self.type} visualization draws its own content and reads no data source")
        elif self.type in CANVAS_TYPES:
            try:
                self.options = validate_element_options(self.type, self.options)
            except CanvasError as exc:
                raise ValueError(f"options: {exc}") from None
            if self.data_sources:
                raise ValueError(f"a {self.type} element reads no data source")
        else:
            if "primary" not in self.data_sources:
                raise ValueError(f"a {self.type} visualization needs dataSources.primary (the data source it draws)")
            self.options = validate_options(self.type, self.options)
        return self


class InputItem(_Model):
    label: Annotated[str, Field(min_length=1, max_length=120)]
    value: Annotated[str, Field(max_length=500)]


class ChoiceOptions(_Model):
    """A dropdown's or a multiselect's choices: written here, and/or read from its data source."""

    items: Annotated[list[InputItem], Field(default_factory=list, max_length=200)]
    #: Of a data source's rows (or a distribution's ``label``): the field giving the value, and the one shown.
    value_field: Annotated[str | None, Field(default=None, max_length=100, alias="valueField")]
    label_field: Annotated[str | None, Field(default=None, max_length=100, alias="labelField")]


class TimerangeOptions(_Model):
    #: The periods offered (the live ranges), all by default.
    items: Annotated[list[str], Field(default_factory=list, max_length=10)]

    @field_validator("items")
    @classmethod
    def _ranges(cls, items: list[str]) -> list[str]:
        from app.services.dashboard_live import RANGES

        unknown = [item for item in items if item not in RANGES]
        if unknown:
            raise ValueError(f"items are periods among {', '.join(RANGES)}, not {unknown[0][:20]!r}")
        return items


class TextOptions(_Model):
    placeholder: Annotated[str | None, Field(default=None, max_length=120)]


class NumberOptions(_Model):
    min: float | None = None
    max: float | None = None
    step: Annotated[float | None, Field(default=None, gt=0)]


class SetTokenAction(_Model):
    type: Literal["setToken"]
    token: Annotated[str, Field(min_length=1, max_length=60)]
    value: Annotated[str, Field(max_length=500)]


class UnsetTokenAction(_Model):
    type: Literal["unsetToken"]
    token: Annotated[str, Field(min_length=1, max_length=60)]


class LinkAction(_Model):
    type: Literal["link"]
    #: A page of the web app (``/...``) or an http(s) address; tokens are put in URL-encoded.
    url: Annotated[str, Field(min_length=1, max_length=1000)]
    new_tab: Annotated[bool, Field(default=False, alias="newTab")]

    @field_validator("url")
    @classmethod
    def _safe(cls, url: str) -> str:
        if (url.startswith("/") and not url.startswith("//")) or re.match(r"^https?://[^\s]+$", url, re.IGNORECASE):
            return url
        raise ValueError("url is a page of the web app ('/...') or an http(s) address")


class SwitchTabAction(_Model):
    type: Literal["switchTab"]
    layout_id: Annotated[ElementId, Field(alias="layoutId")]


ButtonAction = Annotated[SetTokenAction | UnsetTokenAction | LinkAction | SwitchTabAction, Field(discriminator="type")]


class ButtonOptions(_Model):
    label: Annotated[str, Field(min_length=1, max_length=60)]
    actions: Annotated[list[ButtonAction], Field(min_length=1, max_length=10)]


class EntityOptions(_Model):
    """An entity of a platform, picked by the viewer among those of ``entityTypes`` (searched with their access):
    the token holds its id. An OpenCTI custom view's "current entity" is one."""

    source: Literal["opencti", "openaev", "opencrq"]
    #: The integration searched; a pack's input has none until it is inserted.
    integration_id: uuid.UUID | None = None
    entity_types: Annotated[
        list[Annotated[str, Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_.-]+$")]],
        Field(min_length=1, max_length=10, alias="entityTypes"),
    ]
    placeholder: Annotated[str | None, Field(default=None, max_length=120)]
    #: ``False``: while it has no value, a filter on its token is left out instead of the source waiting - an
    #: OpenAEV dashboard's simulation or scenario parameter, which reads every one until one is picked. Unset:
    #: required.
    required: bool | None = None


def optional_tokens(definition: dict) -> frozenset[str]:
    """The tokens of the entity inputs that are not required (``EntityOptions.required``)."""
    names = set()
    for entry in (definition.get("inputs") or {}).values():
        options = entry.get("options") if isinstance(entry, dict) and isinstance(entry.get("options"), dict) else {}
        if entry.get("type") == "input.entity" and options.get("required") is False and isinstance(entry.get("token"), str):
            names.add(entry["token"])
    return frozenset(names)


_INPUT_OPTIONS: dict[str, type[_Model]] = {
    "input.timerange": TimerangeOptions,
    "input.dropdown": ChoiceOptions,
    "input.multiselect": ChoiceOptions,
    "input.text": TextOptions,
    "input.number": NumberOptions,
    "input.button": ButtonOptions,
    "input.entity": EntityOptions,
}


class Input(_Model):
    type: Literal[INPUT_TYPES]  # type: ignore[valid-type]
    title: Annotated[str | None, Field(default=None, max_length=MAX_TITLE_CHARS)]
    token: Annotated[str | None, Field(default=None, max_length=60)]
    default_value: Annotated[Any, Field(default=None, alias="defaultValue")]
    options: FreeObject
    data_sources: Annotated[dict[Literal["primary"], ElementId], Field(default_factory=dict, alias="dataSources")]

    @field_validator("token")
    @classmethod
    def _token(cls, value: str | None) -> str | None:
        if value is not None and not TOKEN_RE.match(value):
            raise ValueError("a token is 1-60 letters, digits, '_' or '.', starting with a letter or '_'")
        return value

    @field_validator("options")
    @classmethod
    def _bounded(cls, value: dict) -> dict:
        return _bounded_object(value, "options")  # type: ignore[return-value]

    @field_validator("default_value")
    @classmethod
    def _small(cls, value: Any) -> Any:
        if len(json.dumps(value, default=str)) > 2_000:
            raise ValueError("defaultValue holds more than 2000 bytes")
        return value

    @model_validator(mode="after")
    def _by_type(self) -> Input:
        from app.services.dashboard_live import RANGES

        model = _INPUT_OPTIONS[self.type]
        self.options = model.model_validate(self.options).model_dump(mode="json", by_alias=True, exclude_none=True)
        if self.data_sources and self.type not in ("input.dropdown", "input.multiselect"):
            raise ValueError(f"an {self.type} reads no data source")
        value = self.default_value
        if self.type == "input.button":
            if self.token is not None or value is not None:
                raise ValueError("a button sets tokens through its actions: it has no token and no defaultValue")
            return self
        if self.token is None:
            raise ValueError(f"an {self.type} sets a token: name it ('token')")
        if value is None:
            return self
        if self.type == "input.timerange" and value not in RANGES:
            raise ValueError(f"defaultValue is a period among {', '.join(RANGES)}")
        if self.type == "input.multiselect" and not (isinstance(value, list) and all(isinstance(v, str) for v in value)):
            raise ValueError("a multiselect's defaultValue is a list of values")
        if self.type == "input.number" and (isinstance(value, bool) or not isinstance(value, int | float)):
            raise ValueError("a number input's defaultValue is a number")
        if self.type in ("input.dropdown", "input.text", "input.entity") and not isinstance(value, str):
            raise ValueError(f"an {self.type}'s defaultValue is text")
        return self


# ── Layout ───────────────────────────────────────────────────────────────────


class Position(_Model):
    x: Annotated[int, Field(ge=0, le=MAX_CANVAS, strict=True)]
    y: Annotated[int, Field(ge=0, le=MAX_CANVAS * 4, strict=True)]
    w: Annotated[int, Field(ge=1, le=MAX_CANVAS, strict=True)]
    h: Annotated[int, Field(ge=1, le=MAX_CANVAS, strict=True)]


class StructureItem(_Model):
    item: ElementId
    #: ``line``: an ``xtm.shape.line`` of a canvas, its ends as ``{from, to}``.
    type: Literal["block", "input", "line"] = "block"
    position: Position | LinePosition

    @model_validator(mode="after")
    def _shape(self) -> StructureItem:
        if (self.type == "line") != isinstance(self.position, LinePosition):
            raise ValueError("a line is placed by {from, to}, a block or an input by {x, y, w, h}")
        return self


class LayoutDefinition(_Model):
    type: Literal["grid", "absolute"] = "grid"
    options: FreeObject
    structure: Annotated[list[StructureItem], Field(default_factory=list, max_length=MAX_STRUCTURE)]

    @field_validator("options")
    @classmethod
    def _bounded(cls, value: dict) -> dict:
        return _bounded_object(value, "options")  # type: ignore[return-value]

    @model_validator(mode="after")
    def _fits(self) -> LayoutDefinition:
        if self.type == "absolute":
            try:
                self.options = validate_canvas_options(self.options)
            except CanvasError as exc:
                raise ValueError(f"options: {exc}") from None
        for index, entry in enumerate(self.structure):
            p = entry.position
            if self.type == "grid" and entry.type == "line":
                raise ValueError(f"structure.{index}: a line sits on an absolute layout, not a grid")
            if isinstance(p, LinePosition):
                continue
            if self.type == "grid" and (p.x + p.w > GRID_COLUMNS or p.h > MAX_ROWS):
                raise ValueError(
                    f"structure.{index}: on a grid, x + w must be at most {GRID_COLUMNS} and h at most {MAX_ROWS}"
                )
        return self


class TabItem(_Model):
    layout_id: Annotated[ElementId, Field(alias="layoutId")]
    label: Annotated[str, Field(min_length=1, max_length=60)]


class TabOptions(_Model):
    show_tab_bar: Annotated[bool, Field(default=True, alias="showTabBar")]
    bar_position: Annotated[Literal["top", "left"], Field(default="top", alias="barPosition")]


class Tabs(_Model):
    items: Annotated[list[TabItem], Field(min_length=1, max_length=MAX_TABS)]
    options: TabOptions = Field(default_factory=TabOptions)


class LayoutOptions(_Model):
    #: Inputs change their tokens only when the Submit button is pressed (and once when the page opens).
    submit_button: Annotated[bool, Field(default=False, alias="submitButton")]
    submit_on_dashboard_load: Annotated[bool, Field(default=True, alias="submitOnDashboardLoad")]


class Layout(_Model):
    options: LayoutOptions = Field(default_factory=LayoutOptions)
    tabs: Tabs
    layout_definitions: Annotated[
        dict[ElementId, LayoutDefinition], Field(min_length=1, max_length=MAX_TABS, alias="layoutDefinitions")
    ]
    global_inputs: Annotated[list[ElementId], Field(default_factory=list, max_length=MAX_INPUTS, alias="globalInputs")]


class ApplicationProperties(_Model):
    hide_edit: Annotated[bool, Field(default=False, alias="hideEdit")]
    hide_export: Annotated[bool, Field(default=False, alias="hideExport")]
    hide_open_in_platform: Annotated[bool, Field(default=False, alias="hideOpenInPlatform")]
    collapse_navigation: Annotated[bool, Field(default=False, alias="collapseNavigation")]


class Definition(_Model):
    version: Literal[2] = 2
    title: Annotated[str, Field(min_length=1, max_length=MAX_TITLE_CHARS)]
    description: Annotated[str | None, Field(default=None, max_length=MAX_SHORT_TEXT_CHARS)]
    #: The dashboard's own theme; none follows the viewer's.
    theme: Literal["light", "dark"] | None = None
    data_sources: Annotated[dict[ElementId, DataSource], Field(default_factory=dict, max_length=MAX_DATA_SOURCES, alias="dataSources")]
    visualizations: Annotated[dict[ElementId, Visualization], Field(default_factory=dict, max_length=MAX_VISUALIZATIONS)]
    inputs: Annotated[dict[ElementId, Input], Field(default_factory=dict, max_length=MAX_INPUTS)]
    layout: Layout
    defaults: FreeObject
    expressions: FreeObject
    application_properties: Annotated[ApplicationProperties, Field(default_factory=ApplicationProperties, alias="applicationProperties")]

    @field_validator("defaults", "expressions")
    @classmethod
    def _bounded(cls, value: dict, info: Any) -> dict:
        return _bounded_object(value, info.field_name)  # type: ignore[return-value]

    @model_validator(mode="after")
    def _tokens(self) -> Definition:
        from app.services.dashboard_tokens import NAME_RE

        conditions = self.expressions.get("conditions")
        if conditions is not None:
            if not isinstance(conditions, dict) or len(conditions) > 40:
                raise ValueError("expressions.conditions maps at most 40 ids to {name, value}")
            for cid, condition in conditions.items():
                _check_id(cid)
                if not isinstance(condition, dict) or set(condition) - {"name", "value"}:
                    raise ValueError(f"expressions.conditions.{cid} is {{name, value}}")
                if not isinstance(condition.get("value"), str) or not 0 < len(condition["value"]) <= 300:
                    raise ValueError(f"expressions.conditions.{cid}.value is an expression of 1-300 characters")
                if condition.get("name") is not None and (not isinstance(condition["name"], str) or len(condition["name"]) > 80):
                    raise ValueError(f"expressions.conditions.{cid}.name is text of at most 80 characters")
        computed = self.expressions.get("tokens")
        if computed is not None:
            if not isinstance(computed, dict) or len(computed) > MAX_INPUTS:
                raise ValueError(f"expressions.tokens maps at most {MAX_INPUTS} token names to expressions")
            for name, expression in computed.items():
                if not NAME_RE.match(name) or ":" in name:
                    raise ValueError(f"expressions.tokens.{name[:60]}: not a token name")
                if not isinstance(expression, str) or not 0 < len(expression) <= 300:
                    raise ValueError(f"expressions.tokens.{name}: an expression of 1-300 characters")
        defaults = (self.defaults.get("tokens") or {}).get("default") if isinstance(self.defaults.get("tokens"), dict) else None
        if self.defaults.get("tokens") is not None and not isinstance(defaults, dict):
            raise ValueError("defaults.tokens is {default: {name: {value}}}")
        for name, entry in (defaults or {}).items():
            if not NAME_RE.match(name) or ":" in name or not isinstance(entry, dict) or set(entry) - {"value"}:
                raise ValueError(f"defaults.tokens.default.{name[:60]} is {{value}} under a token name")
            value = entry.get("value")
            if not (isinstance(value, str) or (isinstance(value, list) and all(isinstance(v, str) for v in value))):
                raise ValueError(f"defaults.tokens.default.{name}.value is text or a list of texts")
        return self

    @model_validator(mode="after")
    def _references(self) -> Definition:
        for group in (self.data_sources, self.visualizations, self.inputs, self.layout.layout_definitions):
            for key in group:
                _check_id(key)
        shared = set(self.visualizations) & set(self.inputs)
        if shared:
            raise ValueError(f"visualizations and inputs share the id(s) {', '.join(sorted(shared))}")
        for vid, viz in self.visualizations.items():
            for role, sid in viz.data_sources.items():
                if sid not in self.data_sources:
                    raise ValueError(f"visualizations.{vid}.dataSources.{role} names no data source '{sid}'")
        for sid, source in self.data_sources.items():
            if isinstance(source, DerivedDataSource):
                seen = [sid]
                current: Any = source
                while isinstance(current, DerivedDataSource):
                    parent = current.options.extends
                    if parent not in self.data_sources:
                        raise ValueError(f"dataSources.{sid}.options.extends names no data source '{parent}'")
                    if parent in seen:
                        raise ValueError(f"dataSources.{sid}: its chain comes back to '{parent}'")
                    seen.append(parent)
                    if len(seen) > MAX_CHAIN_DEPTH + 1:
                        raise ValueError(f"dataSources.{sid}: a chain stacks at most {MAX_CHAIN_DEPTH} derived sources")
                    current = self.data_sources[parent]
        for iid, item in self.inputs.items():
            sid = item.data_sources.get("primary")
            if sid is not None and sid not in self.data_sources:
                raise ValueError(f"inputs.{iid}.dataSources.primary names no data source '{sid}'")
        layouts = self.layout.layout_definitions
        tabbed = [tab.layout_id for tab in self.layout.tabs.items]
        for lid in tabbed:
            if lid not in layouts:
                raise ValueError(f"layout.tabs names no layout '{lid}'")
        if len(set(tabbed)) != len(tabbed):
            raise ValueError("layout.tabs names a layout twice")
        for lid in layouts:
            if lid not in tabbed:
                raise ValueError(f"layout.layoutDefinitions.{lid} is in no tab")
        for lid, definition in layouts.items():
            seen: set[str] = set()
            for index, entry in enumerate(definition.structure):
                pool = self.inputs if entry.type == "input" else self.visualizations
                if entry.item not in pool:
                    raise ValueError(f"layout.layoutDefinitions.{lid}.structure.{index} names no {entry.type} '{entry.item}'")
                if entry.item in seen:
                    raise ValueError(f"layout.layoutDefinitions.{lid} places '{entry.item}' twice")
                seen.add(entry.item)
            check_structure(lid, definition, self.visualizations)
        for iid in self.layout.global_inputs:
            if iid not in self.inputs:
                raise ValueError(f"layout.globalInputs names no input '{iid}'")
        conditions = self.expressions.get("conditions") or {}
        for vid, viz in self.visualizations.items():
            visibility = viz.container_options.visibility
            for cid in [*visibility.show_conditions, *visibility.hide_conditions]:
                if cid not in conditions:
                    raise ValueError(f"visualizations.{vid}.containerOptions.visibility names no condition '{cid}'")
            for index, handler in enumerate(viz.event_handlers):
                if handler.type == "drilldown.switchTab" and handler.options.get("layoutId") not in layouts:
                    raise ValueError(f"visualizations.{vid}.eventHandlers.{index} names no layout '{handler.options.get('layoutId')}'")
        for iid, item in self.inputs.items():
            for index, action in enumerate((item.options or {}).get("actions") or []):
                if action.get("type") == "switchTab" and action.get("layoutId") not in layouts:
                    raise ValueError(f"inputs.{iid}.options.actions.{index} names no layout '{action.get('layoutId')}'")
        return self


# ── Validation entry points ──────────────────────────────────────────────────


def _errors(exc: ValidationError) -> str:
    lines = []
    for err in exc.errors()[:15]:
        loc = ".".join(str(part) for part in err["loc"] if not str(part).startswith(("function-", "tagged-union")))
        # A cross-check names its own field: Pydantic's "Value error, " prefix only gets in the way.
        msg = str(err.get("msg", "invalid value")).removeprefix("Value error, ")
        lines.append(f"  - {loc}: {msg}" if loc else f"  - {msg}")
    return "\n".join(lines)


def validate_definition(payload: object) -> dict:
    """Validate a whole definition and return its normalized JSON-safe dict (camelCase keys)."""
    if not isinstance(payload, dict):
        raise DefinitionError("A dashboard definition must be a JSON object.")
    if len(json.dumps(payload, separators=(",", ":"), default=str)) > MAX_DEFINITION_BYTES:
        raise DefinitionError(f"A dashboard definition holds at most {MAX_DEFINITION_BYTES} bytes.")
    try:
        definition = Definition.model_validate(payload)
    except ValidationError as exc:
        raise DefinitionError(
            f"The dashboard definition is invalid and was NOT saved. Fix these field(s):\n{_errors(exc)}"
        ) from None
    return definition.model_dump(mode="json", by_alias=True, exclude_none=True)


def is_definition(spec: object) -> bool:
    return isinstance(spec, dict) and spec.get("version") == DEFINITION_VERSION


def check_range(value: str | None) -> str | None:
    """A platform source's ``range`` as stored: a range key, fixed dates or a token; refused otherwise."""
    from app.services.dashboard_live import RANGES, is_range
    from app.services.dashboard_tokens import PLACEHOLDER_RE

    if value is None or is_range(value) or PLACEHOLDER_RE.fullmatch(value):
        return value
    raise ValueError(f"range is one of {', '.join(RANGES)}, fixed dates ('2026-01-01/2026-03-31') or a token ('$time$')")


# ── Widgets <-> definition ───────────────────────────────────────────────────


def _widget_parts(widget: dict) -> tuple[dict, dict | None]:
    """A validated widget as ``(visualization, data source or None)``."""
    kind = widget["kind"]
    viz: dict = {}
    if widget.get("title"):
        viz["title"] = widget["title"]
    if kind == "platform":
        viz["type"] = VIEW_TO_TYPE.get(widget.get("view"), "xtm.platformlink")
        source = {
            "type": "platform",
            "options": {
                "source": widget["source"],
                "integration_id": str(widget["integration_id"]),
                "native": widget["native"],
            },
        }
        if widget.get("range"):
            source["options"]["range"] = widget["range"]
        if widget.get("link"):
            viz["options"] = {"link": widget["link"]}
        return viz, source
    viz["type"] = f"xtm.{kind}"
    viz["options"] = {k: v for k, v in widget.items() if k not in ("kind", "id", "title", "width", "layout", "size")}
    return viz, None


def _structure(widgets: list[dict]) -> list[dict]:
    return [{"item": w["id"], "type": "block", "position": dict(w["layout"])} for w in widgets]


def _replacement(viz_type: str, source: object) -> str:
    """The view a legacy validation view became: its module's own for a platform widget, when the
    module draws it with a validation view, else the legacy view's replacement."""
    if isinstance(source, dict) and source.get("type") == "platform":
        from app.services.dashboard_platform import NativeError, check_native

        options = source.get("options") if isinstance(source.get("options"), dict) else {}
        try:
            preferred = VIEW_TO_TYPE.get(check_native(str(options.get("source")), options.get("native")).preferred_view)
        except (NativeError, TypeError, ValueError, KeyError, AttributeError):
            preferred = None
        if preferred in OUTCOME_TYPES:
            return preferred
    return LEGACY_TYPES[viz_type]


def _upgrade_types(definition: dict) -> dict:
    """*definition* with every legacy validation view (:data:`LEGACY_TYPES`) drawn by its replacement."""
    visualizations = definition.get("visualizations")
    sources = definition.get("dataSources") if isinstance(definition.get("dataSources"), dict) else {}
    for viz in visualizations.values() if isinstance(visualizations, dict) else []:
        if isinstance(viz, dict) and viz.get("type") in LEGACY_TYPES:
            primary = (viz.get("dataSources") or {}).get("primary") if isinstance(viz.get("dataSources"), dict) else None
            viz["type"] = _replacement(viz["type"], sources.get(primary) if isinstance(primary, str) else None)
    defaults = (definition.get("defaults") or {}).get("visualizations") if isinstance(definition.get("defaults"), dict) else None
    if isinstance(defaults, dict):
        for legacy, replacement in LEGACY_TYPES.items():
            if legacy in defaults:
                moved = defaults.pop(legacy)
                defaults.setdefault(replacement, moved)
    return definition


def upgrade(spec: object) -> dict:
    """A stored dashboard as the working definition: a version 2 one as it is, a manifest (version 3,
    ``dashboard_native``) read back, a version 1 one converted.

    A legacy validation view becomes its replacement (:data:`LEGACY_TYPES`). Never
    validates: what was stored is read; a write validates what it writes.
    """
    from app.services.dashboard_native import is_native

    if is_native(spec):
        from app.services.dashboard_native import NativeFormatError, from_native

        try:
            return _upgrade_types(from_native(spec))  # type: ignore[arg-type]
        except NativeFormatError as exc:
            # Only XTM One writes a stored manifest; one it cannot read is shown empty, never a crash of the page.
            logger.error("A stored dashboard manifest could not be read: %s", exc)
            return {**upgrade({}), "title": str((spec.get("x_xtm_one") or {}).get("title") or "Dashboard")}  # type: ignore[union-attr]
    if is_definition(spec):
        return _upgrade_types(copy.deepcopy(spec))  # type: ignore[arg-type]
    spec = spec if isinstance(spec, dict) else {}
    widgets = fill_layouts([dict(w) for w in spec.get("widgets") or [] if isinstance(w, dict) and w.get("id")])
    for widget in widgets:
        if widget.get("view") in LEGACY_VIEWS:
            widget["view"] = TYPE_TO_VIEW[_replacement(LEGACY_VIEWS[widget["view"]], {"type": "platform", "options": widget})]
    data_sources: dict = {}
    visualizations: dict = {}
    for widget in widgets:
        try:
            viz, source = _widget_parts(widget)
        except (KeyError, TypeError):
            continue
        if source is not None:
            sid = f"ds_{widget['id']}"
            data_sources[sid] = source
            viz["dataSources"] = {"primary": sid}
        visualizations[widget["id"]] = viz
    definition: dict = {
        "version": DEFINITION_VERSION,
        "title": spec.get("title") or "Dashboard",
        "dataSources": data_sources,
        "visualizations": visualizations,
        "inputs": {},
        "layout": {
            "tabs": {"items": [{"layoutId": FIRST_LAYOUT, "label": "Overview"}], "options": {"showTabBar": False}},
            "layoutDefinitions": {
                FIRST_LAYOUT: {"type": "grid", "structure": _structure([w for w in widgets if w["id"] in visualizations])}
            },
            "globalInputs": [],
        },
    }
    if spec.get("subtitle"):
        definition["description"] = spec["subtitle"]
    return definition


def from_widgets(spec_payload: object) -> dict:
    """A version 1 payload (``{title, subtitle?, widgets}``) validated and turned into a definition."""
    return validate_definition(upgrade(validate_spec(spec_payload)))


def _placement(definition: dict) -> dict[str, dict]:
    """Each placed visualization's position and the layout holding it (the first one placing it)."""
    placed: dict[str, dict] = {}
    for lid, layout in (definition.get("layout") or {}).get("layoutDefinitions", {}).items():
        for entry in layout.get("structure") or []:
            if entry.get("type", "block") == "block" and entry.get("item") not in placed:
                placed[entry["item"]] = {"layout": lid, "type": layout.get("type", "grid"), **entry["position"]}
    return placed


def widget_view(definition: dict, *, layout_id: str | None = None) -> list[dict]:
    """The visualizations as widgets (``kind``, fields, ``layout``), in reading order.

    Every visualization that has a widget form (content, or a platform source);
    with *layout_id*, only those placed in that layout. Positions are a grid
    layout's; a visualization of an absolute layout is placed in rows.
    """
    placed = _placement(definition)
    sources = definition.get("dataSources") or {}
    widgets: list[dict] = []
    for vid, viz in (definition.get("visualizations") or {}).items():
        where = placed.get(vid)
        if layout_id is not None and (where is None or where["layout"] != layout_id):
            continue
        vtype = viz.get("type", "")
        widget: dict
        if vtype in CONTENT_TYPES:
            widget = {"kind": vtype.removeprefix("xtm."), **(viz.get("options") or {})}
        else:
            source = sources.get((viz.get("dataSources") or {}).get("primary") or "")
            if not isinstance(source, dict) or source.get("type") != "platform":
                continue
            options = source.get("options") or {}
            widget = {
                "kind": "platform",
                "source": options.get("source"),
                "integration_id": options.get("integration_id"),
                "native": options.get("native"),
            }
            if options.get("range"):
                widget["range"] = options["range"]
            view = TYPE_TO_VIEW.get(vtype)
            if view is not None:
                widget["view"] = view
            link = (viz.get("options") or {}).get("link")
            if link:
                widget["link"] = link
        widget["id"] = vid
        if viz.get("title"):
            widget["title"] = viz["title"]
        if where is not None and where["type"] == "grid":
            widget["layout"] = {k: where[k] for k in ("x", "y", "w", "h")}
            widget["width"] = width_of(where["w"])
        widgets.append(widget)
    fill_layouts(widgets)
    return sorted(widgets, key=lambda w: (w["layout"]["y"], w["layout"]["x"]))


#: The shape of an answer in words (an agent's, an assignment's, a variable's).
_ANSWER_SHAPES = ("text",)


def visualization_catalog(shape: str | None = None) -> list[dict]:
    """The data visualizations, each with the data shapes it draws and its options (read from their models):
    what an agent reads to pick one. Narrowed to those drawing *shape* when given."""
    from app.services.dashboard_platform import SHAPE_VIEWS
    from app.services.dashboard_visualization_options import option_names

    catalog = []
    for viz_type in DATA_TYPES:
        view = TYPE_TO_VIEW.get(viz_type)
        if viz_type == "xtm.platformlink":
            continue
        shapes = list(_ANSWER_SHAPES) if viz_type == "xtm.answer" else [s for s, views in SHAPE_VIEWS.items() if view in views]
        if shape is not None and shape not in shapes:
            continue
        catalog.append({"type": viz_type, "shapes": shapes, "options": option_names(viz_type)})
    return catalog


def summary_widgets(spec: object) -> list[dict]:
    """The widget view of a stored dashboard of either version."""
    return widget_view(upgrade(spec))


def visualization_count(definition: dict) -> int:
    """The visualizations a dashboard shows; a canvas's shapes, icons, images and markdown are no widgets."""
    return sum(
        1 for viz in (definition.get("visualizations") or {}).values() if not (isinstance(viz, dict) and viz.get("type") in CANVAS_TYPES)
    )


def platform_sources(definition: dict) -> dict[str, dict]:
    """The platform data sources of a definition, by id."""
    return {
        sid: source
        for sid, source in (definition.get("dataSources") or {}).items()
        if isinstance(source, dict) and source.get("type") == "platform"
    }


# ── Edits ────────────────────────────────────────────────────────────────────


class Operation(_Model):
    """One step of an edit of a definition.

    Widget steps (what agents and the studio's inspector send): ``upsert`` a
    widget, ``remove`` one, ``move`` one after another, ``layout`` (place
    several, in *layoutId*'s layout: ``{id, x, y, w, h}``, a line ``{id, from, to}``;
    a visualization or an input placed in no layout yet is added to this one,
    so a ``set visualizations.<id>`` is placed without rewriting the whole layout).
    Element steps (the source editor, the configuration panel, the canvas):
    ``set`` a whole element at *path* (``visualizations.<id>``,
    ``dataSources.<id>``, ``inputs.<id>``, ``layout``, ``defaults``,
    ``expressions``, ``applicationProperties``, ``title``, ``description``,
    ``theme``), ``delete`` one. An element leaving a canvas lets go of the
    lines held to it: their ends stay where they were.
    """

    op: Literal["upsert", "remove", "move", "layout", "set", "delete"]
    widget: dict | None = None
    id: Annotated[str | None, Field(default=None, max_length=60)]
    after: Annotated[str | None, Field(default=None, max_length=60)]
    items: Annotated[list[dict] | None, Field(default=None, max_length=MAX_STRUCTURE)]
    layout_id: Annotated[str | None, Field(default=None, max_length=60, alias="layoutId")]
    path: Annotated[str | None, Field(default=None, max_length=200)]
    value: Any = None


_SET_PATH_RE = re.compile(
    r"^(?:(?P<group>visualizations|dataSources|inputs)\.(?P<id>[A-Za-z0-9][A-Za-z0-9_.-]{0,59})"
    r"|(?P<whole>layout|defaults|expressions|applicationProperties|title|description|theme))$"
)


def _layout(definition: dict, layout_id: str | None, where: str) -> dict:
    layouts = definition["layout"]["layoutDefinitions"]
    lid = layout_id or definition["layout"]["tabs"]["items"][0]["layoutId"]
    if lid not in layouts:
        raise DefinitionError(f"{where}: no layout '{lid}' (layouts: {', '.join(layouts)}).")
    return layouts[lid]


def _entry(layout: dict, item: str) -> dict | None:
    return next((e for e in layout.get("structure") or [] if e.get("item") == item), None)


def _place_unplaced(definition: dict, layout: dict, eid: str) -> dict | None:
    """A visualization or an input placed in no layout yet, added to *layout* (its place is the item's); else ``None``."""
    if any(_entry(other, eid) for other in definition["layout"]["layoutDefinitions"].values()):
        return None
    viz = definition["visualizations"].get(eid)
    if viz is not None:
        kind = "line" if viz.get("type") == "xtm.shape.line" else "block"
    elif eid in (definition.get("inputs") or {}):
        kind = "input"
    else:
        return None
    entry = {"item": eid, "type": kind, "position": {}}
    layout.setdefault("structure", []).append(entry)
    return entry


def _grid_widgets(definition: dict, layout: dict) -> list[dict]:
    """A grid layout's blocks as ``{id, layout, width}`` stubs, in reading order (for placement)."""
    stubs = []
    for entry in layout.get("structure") or []:
        if entry.get("type", "block") != "block":
            continue
        p = entry["position"]
        stubs.append({"id": entry["item"], "layout": dict(p), "width": width_of(p["w"])})
    return sorted(stubs, key=lambda s: (s["layout"]["y"], s["layout"]["x"]))


def _place(definition: dict, layout: dict, vid: str, widget: dict, previous: dict | None, *, after: str | None) -> bool:
    """Put *vid* in *layout*: where it was (resized to a new width), where it says, or below; ``True`` when the grid must re-flow."""
    entry = _entry(layout, vid)
    given = widget.get("layout") if isinstance(widget.get("layout"), dict) else None
    if given is not None:
        position = dict(given)
    elif entry is not None:
        position = dict(entry["position"])
        width = widget.get("width")
        if width is not None and layout.get("type", "grid") == "grid" and width != width_of(position["w"]):
            position["w"] = width_columns(width)
            position["x"] = min(position["x"], GRID_COLUMNS - position["w"])
    elif layout.get("type") == "absolute":
        # Below the canvas's lowest block, in pixels: a grid cell means nothing here.
        bottom = max(
            (e["position"]["y"] + e["position"]["h"] for e in layout.get("structure") or [] if e.get("type", "block") == "block"),
            default=0,
        )
        position = {"x": 24, "y": min(bottom + 24, MAX_CANVAS * 4 - 320), "w": 480, "h": 320}
    else:
        stubs = _grid_widgets(definition, layout)
        stub = {"id": vid, "width": widget.get("width", "full"), "kind": widget.get("kind"), "view": widget.get("view")}
        fill_layouts([*stubs, stub])
        position = stub["layout"]
    if entry is None:
        layout.setdefault("structure", []).append({"item": vid, "type": "block", "position": position})
    else:
        entry["position"] = position
    return after is not None and given is None


def _reflow_after(layout: dict, vid: str, after: str, where: str) -> None:
    """Re-flow a grid layout with *vid* right after *after* (``"start"`` for the top)."""
    stubs = [s for s in _grid_widgets({}, layout) if s["id"] != vid]
    moved = next((s for s in _grid_widgets({}, layout) if s["id"] == vid), None)
    if moved is None:
        return
    if after == "start":
        stubs.insert(0, moved)
    else:
        index = next((i for i, s in enumerate(stubs) if s["id"] == after), None)
        if index is None:
            raise DefinitionError(f"{where}.after names no widget '{after}'.")
        stubs.insert(index + 1, moved)
    reflow(stubs)
    by_id = {s["id"]: s["layout"] for s in stubs}
    for entry in layout.get("structure") or []:
        if entry["item"] in by_id:
            entry["position"] = by_id[entry["item"]]


def _drop_orphan_source(definition: dict, sid: str | None, vid: str) -> None:
    """Remove the data source a widget brought along when nothing reads it any more."""
    if not sid or sid != f"ds_{vid}":
        return
    readers = [
        v for v in definition["visualizations"].values() if sid in (v.get("dataSources") or {}).values()
    ] + [i for i in definition["inputs"].values() if sid in (i.get("dataSources") or {}).values()]
    if not readers:
        definition["dataSources"].pop(sid, None)


def _upsert_widget(definition: dict, op: Operation, where: str) -> str:
    if op.widget is None:
        raise DefinitionError(f"{where}: an upsert needs 'widget'.")
    raw = dict(op.widget)
    wid = raw.get("id")
    if wid is None:
        base, n = str(raw.get("kind") or "widget"), 2
        wid = base
        while wid in definition["visualizations"] or wid in definition["inputs"]:
            wid, n = f"{base}-{n}", n + 1
        raw["id"] = wid
    widget = validate_widget(raw, where=f"{where}.widget")
    widget["width"] = raw.get("width")  # what the edit named, not the default
    viz, source = _widget_parts({**widget, "width": widget["width"] or "full"})
    previous = definition["visualizations"].get(wid)
    if previous is not None:
        # The widget form carries no interactions, visibility or description: kept.
        for key in ("description", "context", "eventHandlers", "containerOptions"):
            if key in previous:
                viz[key] = previous[key]
    old_sid = (previous or {}).get("dataSources", {}).get("primary") if previous else None
    if source is not None:
        sid = old_sid if old_sid and definition["dataSources"].get(old_sid, {}).get("type") == "platform" else f"ds_{wid}"
        definition["dataSources"][sid] = source
        viz["dataSources"] = {"primary": sid}
    definition["visualizations"][wid] = viz
    if source is None:
        _drop_orphan_source(definition, old_sid, wid)
    layout = _layout(definition, op.layout_id, where)
    if _place(definition, layout, wid, {**raw, "kind": widget["kind"], "view": widget.get("view")}, previous, after=op.after):
        _reflow_after(layout, wid, op.after or "start", where)
    return wid


def apply_definition_operations(
    definition: dict,
    operations: object,
    *,
    title: str | None = None,
    subtitle: str | None = None,
) -> dict:
    """Apply an edit to a definition and return the new validated one; nothing changes unless all of it applies."""
    if not isinstance(operations, list):
        raise DefinitionError("operations must be an array of {op, ...} objects.")
    if len(operations) > 40:
        raise DefinitionError("An edit holds at most 40 operations.")
    working = upgrade(definition)
    working.setdefault("dataSources", {})
    working.setdefault("visualizations", {})
    working.setdefault("inputs", {})
    for index, raw in enumerate(operations):
        where = f"operations.{index}"
        try:
            op = Operation.model_validate(raw)
        except ValidationError as exc:
            raise DefinitionError(f"{where} is invalid:\n{_errors(exc)}") from None
        if op.op == "upsert":
            _upsert_widget(working, op, where)
            continue
        if op.op == "set" or op.op == "delete":
            match = _SET_PATH_RE.match(op.path or "")
            if not match:
                raise DefinitionError(
                    f"{where}: path must be visualizations.ID, dataSources.ID, inputs.ID, layout, defaults, "
                    "expressions, applicationProperties, title, description or theme."
                )
            if match.group("group"):
                group, eid = match.group("group"), match.group("id")
                if op.op == "set":
                    if not isinstance(op.value, dict):
                        raise DefinitionError(f"{where}: value must be the whole element (a JSON object).")
                    working[group][eid] = op.value
                else:
                    if eid not in working[group]:
                        raise DefinitionError(f"{where}: no {group} element '{eid}'.")
                    working[group].pop(eid)
                    if group in ("visualizations", "inputs"):
                        for layout in working["layout"]["layoutDefinitions"].values():
                            detach_lines(layout, eid)
                            layout["structure"] = [e for e in layout.get("structure") or [] if e.get("item") != eid]
                        working["layout"]["globalInputs"] = [
                            i for i in working["layout"].get("globalInputs") or [] if i != eid
                        ]
            else:
                whole = match.group("whole")
                if op.op == "delete":
                    if whole in ("layout", "title"):
                        raise DefinitionError(f"{where}: {whole} cannot be deleted.")
                    working.pop(whole, None)
                else:
                    working[whole] = op.value
            continue
        if op.op == "layout":
            if not op.items:
                raise DefinitionError(f"{where}: a layout needs 'items' ([{{id, x, y, w, h}}]).")
            layout = _layout(working, op.layout_id, where)
            for item in op.items:
                if not isinstance(item, dict) or "id" not in item:
                    raise DefinitionError(f"{where}: each item names its 'id'.")
                entry = _entry(layout, str(item["id"])) or _place_unplaced(working, layout, str(item["id"]))
                if entry is None:
                    known = ", ".join(e["item"] for e in layout.get("structure") or []) or "none"
                    raise DefinitionError(f"{where}: no widget has the id '{item['id']}' (ids: {known}).")
                if entry.get("type") == "line":
                    entry["position"] = {k: item.get(k) for k in ("from", "to")}
                else:
                    entry["position"] = {k: item.get(k) for k in ("x", "y", "w", "h")}
            continue
        if not op.id:
            raise DefinitionError(f"{where}: a {op.op} needs the widget's 'id'.")
        if op.id not in working["visualizations"]:
            known = ", ".join(working["visualizations"]) or "none"
            raise DefinitionError(f"{where}: no widget has the id '{op.id}' (ids: {known}).")
        if op.op == "remove":
            sid = (working["visualizations"][op.id].get("dataSources") or {}).get("primary")
            working["visualizations"].pop(op.id)
            for layout in working["layout"]["layoutDefinitions"].values():
                detach_lines(layout, op.id)
                layout["structure"] = [e for e in layout.get("structure") or [] if e.get("item") != op.id]
            _drop_orphan_source(working, sid, op.id)
            continue
        if op.after is None:
            raise DefinitionError(f"{where}: a move needs 'after' (a widget id, or \"start\").")
        if op.after == op.id:
            raise DefinitionError(f"{where}: a widget cannot move after itself.")
        _reflow_after(_layout(working, op.layout_id, where), op.id, op.after, where)
    if title is not None:
        working["title"] = title
    if subtitle is not None:
        if subtitle:
            working["description"] = subtitle
        else:
            working.pop("description", None)
    if not working["visualizations"] and any(
        isinstance(raw, dict)
        and (raw.get("op") == "remove" or (raw.get("op") == "delete" and str(raw.get("path", "")).startswith("visualizations.")))
        for raw in operations
    ):
        raise DefinitionError("A dashboard keeps at least one widget.")
    return validate_definition(working)


__all__ = [
    "CONTENT_TYPES",
    "DATA_TYPES",
    "DEFINITION_VERSION",
    "INPUT_TYPES",
    "VIEW_TO_TYPE",
    "VISUALIZATION_TYPES",
    "DefinitionError",
    "apply_definition_operations",
    "from_widgets",
    "is_definition",
    "platform_sources",
    "summary_widgets",
    "upgrade",
    "validate_definition",
    "visualization_count",
    "widget_view",
]
