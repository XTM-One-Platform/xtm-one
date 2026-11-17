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

"""``DashboardSpec`` - the validated contract for agent-built custom dashboards.

This module is the **single source of truth** for what a custom dashboard may
contain. The agent tools (``build_dashboard``, ``edit_dashboard``), the REST
edit endpoint and the tests all validate through :func:`validate_spec` and
:func:`apply_operations`, so the tools, the API and the frontend renderer
(``frontend/src/components/custom-dashboard``) cannot drift on the accepted
shape.

Design constraints that shaped it
---------------------------------
* **An agent generates a UI, never markup.** A dashboard is a title and an
  ordered list of widgets, each declaring one ``kind`` of
  :data:`DASHBOARD_WIDGET_KINDS`. Every kind maps to one React component with a
  fixed prop set, drawn with the design system's tokens: the agent chooses
  widgets and fills values, the renderer owns the look. There is no HTML, CSS,
  colour or URL scheme an agent can author: colours are :data:`DASHBOARD_TONES`,
  links are platform paths or prompts sent to the assistant.
* **Fail loudly at write time, tolerate at read time.** Models forbid unknown
  keys, so an LLM that invents a field gets an error naming it and the allowed
  ones, which is what makes the retry productive. A stored dashboard is read
  back as is: a widget written under an older catalog is the renderer's to skip
  (it draws a placeholder), never a reason to fail the page.
* **Partial edits.** :func:`apply_operations` upserts, removes and reorders
  widgets by their stable ``id``, so refreshing one figure does not mean
  regenerating, and possibly breaking, the whole dashboard. A widget stored
  under an older catalog does not block an edit of the others.
* **Bounded.** Every collection and string has a cap, so one tool call can
  neither blow up the JSON column nor produce a dashboard nobody can read.

The inline markup of text fields is the deck grammar (``**bold**``, ``_em_``,
`` `code` ``, ``[[tone:text]]``), tokenized client-side into React nodes and
never into an HTML string (``components/presentation/markup.ts``).
"""

from __future__ import annotations

import re
import uuid
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from app.services.dashboard_grid import fill_layouts, reading_order, reflow, width_columns, width_of
from app.services.dashboard_platform import PLATFORM_VIEWS

# ── Catalog constants ────────────────────────────────────────────────────────

#: Widget catalog. Each entry has a matching React component in
#: ``frontend/src/components/custom-dashboard/widgets.tsx`` (``WIDGETS``); the
#: frontend lists the same ids in ``DASHBOARD_WIDGET_KINDS``, and
#: ``tests/test_custom_dashboards.py`` pins the two lists together.
DASHBOARD_WIDGET_KINDS: tuple[str, ...] = (
    "score",
    "stats",
    "stages",
    "decision",
    "feed",
    "callout",
    "ranked_list",
    "steps",
    "checklist",
    "progress",
    "prompts",
    "text",
    "platform",
)

#: The colours a widget may ask for, by meaning. The renderer maps each to a
#: design-system token, so a dashboard follows the theme and the product accent.
DASHBOARD_TONES: tuple[str, ...] = ("neutral", "accent", "ai", "success", "warning", "danger", "info")

#: How much of a row a widget takes when it has no ``layout`` (an older
#: dashboard, an agent that gave no position): ``dashboard_grid.fill_layouts``
#: places it below the others this wide. Below the grid breakpoint every widget
#: takes the full width, in reading order.
DASHBOARD_WIDTHS: tuple[str, ...] = ("full", "half", "third")

#: The state of a stage of a ``stages`` widget.
DASHBOARD_STAGE_STATES: tuple[str, ...] = ("done", "active", "pending", "blocked")

#: The direction of a ``score`` widget's change.
DASHBOARD_TREND_DIRECTIONS: tuple[str, ...] = ("up", "down", "flat")

MAX_WIDGETS = 24
MAX_TITLE_CHARS = 120
MAX_LABEL_CHARS = 80
MAX_SHORT_TEXT_CHARS = 200
MAX_TEXT_CHARS = 600
MAX_BODY_CHARS = 2000
MAX_PROMPT_CHARS = 500
MAX_VALUE_CHARS = 24
MAX_BADGE_CHARS = 6
MAX_SERIES_POINTS = 60
MAX_SCORE_STATS = 4
MAX_STATS = 6
MAX_STAGES = 8
MAX_TAGS = 4
MAX_ACTIONS = 2
MAX_FEED_ITEMS = 12
MAX_LIST_ITEMS = 12
MAX_STEPS = 12
MAX_CHECKLIST_ITEMS = 20
MAX_PROGRESS_LINES = 8
MAX_SUGGESTIONS = 6
MAX_OPERATIONS = 40
#: The lowest row a widget may start on (a full dashboard of the tallest widgets).
MAX_GRID_Y = MAX_WIDGETS * 40

#: A widget id: short, stable, URL- and DOM-safe. The agent picks it (``score``,
#: ``ctem_cycle``) so a later edit can address the widget; a missing one is
#: assigned by :func:`assign_widget_ids`.
WIDGET_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")

#: A link target inside the platform: a root-relative path, never a scheme, a
#: protocol-relative ``//host`` or the API. The frontend re-checks the same rule
#: (``isSafeDashboardHref``) before it navigates, whatever the stored row holds.


class _Strict(BaseModel):
    """Base for every spec model: unknown keys are an error, not a shrug."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


Title = Annotated[str, Field(min_length=1, max_length=MAX_TITLE_CHARS)]
OptionalTitle = Annotated[str | None, Field(default=None, max_length=MAX_TITLE_CHARS)]
Label = Annotated[str, Field(min_length=1, max_length=MAX_LABEL_CHARS)]
OptionalLabel = Annotated[str | None, Field(default=None, max_length=MAX_LABEL_CHARS)]
OptionalShortText = Annotated[str | None, Field(default=None, max_length=MAX_SHORT_TEXT_CHARS)]
Text = Annotated[str, Field(min_length=1, max_length=MAX_TEXT_CHARS)]
OptionalText = Annotated[str | None, Field(default=None, max_length=MAX_TEXT_CHARS)]
Value = Annotated[str, Field(min_length=1, max_length=MAX_VALUE_CHARS)]
Tone = Literal["neutral", "accent", "ai", "success", "warning", "danger", "info"]
Width = Literal["full", "half", "third"]


def check_internal_path(value: str | None) -> str | None:
    """Reject anything that is not a path of this platform's web app."""
    if value is None:
        return None
    from app.core.internal_paths import is_internal_path

    if not is_internal_path(value):
        raise ValueError(
            "href must be a path of this platform's web app starting with a single '/' "
            "(for example /agents or /presentations); web addresses, other schemes and "
            "/api paths are refused. Use 'prompt' instead to send a question to the assistant."
        )
    return value


def _number_to_text(value: object) -> object:
    """A figure the agent sent as a number is shown as written (``7``, ``9.6``)."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return f"{value:g}"
    return value


# ── Building blocks ──────────────────────────────────────────────────────────


class Action(_Strict):
    """A control of a widget: open a platform page, or ask the assistant.

    Exactly one of ``href`` and ``prompt``. Neither runs anything: a prompt is
    put in the assistant's composer for the person to send, so a dashboard an
    agent wrote can never act on the person's behalf.
    """

    label: Label
    href: Annotated[str | None, Field(default=None, max_length=301)]
    prompt: Annotated[str | None, Field(default=None, min_length=1, max_length=MAX_PROMPT_CHARS)]

    _check_href = field_validator("href")(check_internal_path)

    @model_validator(mode="after")
    def _one_target(self) -> Action:
        if (self.href is None) == (self.prompt is None):
            raise ValueError("an action needs exactly one of 'href' (a platform path) or 'prompt'")
        return self


class Badge(_Strict):
    label: Annotated[str, Field(min_length=1, max_length=24)]
    tone: Tone = "neutral"


class Trend(_Strict):
    """The change of a score: ``▼ 8 pts this week``."""

    direction: Literal["up", "down", "flat"]
    label: Label
    #: Whether the change is good news is the agent's call: a falling exposure
    #: score is good, a falling coverage is not.
    tone: Tone = "neutral"


class StatItem(_Strict):
    label: Label
    value: Value
    caption: OptionalShortText
    tone: Tone = "neutral"

    _value = field_validator("value", mode="before")(_number_to_text)


class GridLayout(_Strict):
    """Where a widget sits on the dashboard's 12-column grid (``dashboard_grid``)."""

    x: Annotated[int, Field(ge=0, le=11, strict=True)]
    y: Annotated[int, Field(ge=0, le=MAX_GRID_Y, strict=True)]
    w: Annotated[int, Field(ge=1, le=12, strict=True)]
    h: Annotated[int, Field(ge=1, le=40, strict=True)]

    @model_validator(mode="after")
    def _fits(self) -> GridLayout:
        if self.x + self.w > 12:
            raise ValueError("x + w must be at most 12 (the grid has 12 columns)")
        return self


class _WidgetBase(_Strict):
    #: Stable handle of the widget, for edits; assigned when omitted.
    id: Annotated[str | None, Field(default=None, max_length=40)]
    #: Heading shown above the widget.
    title: OptionalTitle
    width: Width = "full"
    #: Its place on the grid; a widget without one is placed below the others.
    layout: GridLayout | None = None
    #: The heading's link ("Details", "All agents").
    link: Action | None = None

    @field_validator("id")
    @classmethod
    def _check_id(cls, value: str | None) -> str | None:
        if value is not None and not WIDGET_ID_RE.match(value):
            raise ValueError("id must be 1-40 lowercase letters, digits, '_' or '-', starting with a letter or digit")
        return value


# ── Widgets ──────────────────────────────────────────────────────────────────


class ScoreWidget(_WidgetBase):
    """A headline figure with its trend line and a few sub-figures."""

    kind: Literal["score"]
    label: Label
    value: float
    max: float | None = None
    unit: Annotated[str | None, Field(default=None, max_length=12)]
    trend: Trend | None = None
    #: The values of the trend line, oldest first.
    series: Annotated[list[float] | None, Field(default=None, min_length=2, max_length=MAX_SERIES_POINTS)]
    #: What the line shows, read out to screen readers ("Score over 30 days").
    series_label: OptionalLabel
    stats: Annotated[list[StatItem] | None, Field(default=None, max_length=MAX_SCORE_STATS)]


class StatsWidget(_WidgetBase):
    """A row of figures."""

    kind: Literal["stats"]
    items: Annotated[list[StatItem], Field(min_length=1, max_length=MAX_STATS)]


class Stage(_Strict):
    label: Label
    state: Literal["done", "active", "pending", "blocked"] = "pending"
    #: "Done", "2 running", "4 open".
    status: OptionalLabel
    #: How far along the stage is, 0 to 1 (a full bar when omitted and done).
    progress: Annotated[float | None, Field(default=None, ge=0, le=1)]


class StagesWidget(_WidgetBase):
    """A cycle of stages with their progress (the CTEM cycle)."""

    kind: Literal["stages"]
    items: Annotated[list[Stage], Field(min_length=1, max_length=MAX_STAGES)]


class DecisionWidget(_WidgetBase):
    """Something waiting for the person: a proposal with its evidence."""

    kind: Literal["decision"]
    eyebrow: OptionalLabel
    #: When it was raised, as written ("2 min ago").
    time: OptionalLabel
    text: Text
    tags: Annotated[list[Label] | None, Field(default=None, max_length=MAX_TAGS)]
    #: The first is the primary button.
    actions: Annotated[list[Action], Field(min_length=1, max_length=MAX_ACTIONS)]


class FeedItem(_Strict):
    #: A short code shown in the item's tile ("DSC", "VAL").
    badge: Annotated[str | None, Field(default=None, max_length=MAX_BADGE_CHARS)]
    title: Label
    time: OptionalLabel
    text: OptionalText
    href: Annotated[str | None, Field(default=None, max_length=301)]

    _check_href = field_validator("href")(check_internal_path)


class FeedWidget(_WidgetBase):
    """A list of recent events (what each agent did)."""

    kind: Literal["feed"]
    items: Annotated[list[FeedItem], Field(min_length=1, max_length=MAX_FEED_ITEMS)]


class CalloutWidget(_WidgetBase):
    """A highlighted statement with its follow-ups."""

    kind: Literal["callout"]
    eyebrow: OptionalLabel
    text: Text
    tone: Tone = "neutral"
    actions: Annotated[list[Action] | None, Field(default=None, max_length=MAX_ACTIONS)]


class RankedItem(_Strict):
    title: Label
    meta: OptionalShortText
    badge: Badge | None = None
    href: Annotated[str | None, Field(default=None, max_length=301)]

    _check_href = field_validator("href")(check_internal_path)


class RankedListWidget(_WidgetBase):
    """An ordered list (what to fix first)."""

    kind: Literal["ranked_list"]
    items: Annotated[list[RankedItem], Field(min_length=1, max_length=MAX_LIST_ITEMS)]
    #: Number the items (1, 2, 3).
    numbered: bool = True


class Step(_Strict):
    label: Label
    detail: OptionalShortText
    tone: Tone = "neutral"


class StepsWidget(_WidgetBase):
    """A sequence of steps with their outcome (an attack path)."""

    kind: Literal["steps"]
    caption: OptionalLabel
    items: Annotated[list[Step], Field(min_length=1, max_length=MAX_STEPS)]


class ChecklistItem(_Strict):
    label: Annotated[str, Field(min_length=1, max_length=MAX_SHORT_TEXT_CHARS)]
    done: bool = False


class ChecklistWidget(_WidgetBase):
    """A read-only plan with what is done."""

    kind: Literal["checklist"]
    eyebrow: OptionalLabel
    items: Annotated[list[ChecklistItem], Field(min_length=1, max_length=MAX_CHECKLIST_ITEMS)]
    actions: Annotated[list[Action] | None, Field(default=None, max_length=MAX_ACTIONS)]


class ProgressLine(_Strict):
    label: Annotated[str, Field(min_length=1, max_length=MAX_SHORT_TEXT_CHARS)]
    tone: Tone = "neutral"


class ProgressWidget(_WidgetBase):
    """Work under way: its state, a bar and its latest lines."""

    kind: Literal["progress"]
    status: Badge | None = None
    text: OptionalText
    progress: Annotated[float | None, Field(default=None, ge=0, le=1)]
    #: "Step 7 of 11 · Lateral movement".
    progress_label: OptionalLabel
    lines: Annotated[list[ProgressLine] | None, Field(default=None, max_length=MAX_PROGRESS_LINES)]
    actions: Annotated[list[Action] | None, Field(default=None, max_length=MAX_ACTIONS)]


class PromptsWidget(_WidgetBase):
    """Questions the person can ask the assistant next."""

    kind: Literal["prompts"]
    #: The text of the composer the widget opens ("Ask anything…").
    placeholder: OptionalLabel
    suggestions: Annotated[
        list[Annotated[str, Field(min_length=1, max_length=MAX_SHORT_TEXT_CHARS)]],
        Field(min_length=1, max_length=MAX_SUGGESTIONS),
    ]


class TextWidget(_WidgetBase):
    """Paragraphs of prose (inline markup only), or Markdown (``format: markdown``: GitHub-flavoured, no HTML)."""

    kind: Literal["text"]
    body: Annotated[str, Field(min_length=1, max_length=MAX_BODY_CHARS)]
    format: Literal["paragraphs", "markdown"] | None = None


class PlatformWidget(_WidgetBase):
    """A widget of OpenCTI, OpenAEV or OpenCRQ, drawn from the platform's data.

    ``native`` is the platform's own configuration (an OpenCTI dashboard widget,
    an OpenAEV widget, an OpenCRQ metric), checked by ``dashboard_platform``;
    the data is read when the widget is drawn, through ``integration_id``, with
    the viewer's own access to the platform. ``view`` is how XTM One draws it
    (filled from the data's shape when omitted); ``None`` for a type XTM One
    does not draw (an OpenCTI heatmap), shown as a link to the platform.
    """

    kind: Literal["platform"]
    source: Literal["opencti", "openaev", "opencrq"]
    integration_id: uuid.UUID
    view: Literal[PLATFORM_VIEWS] | None = None  # type: ignore[valid-type]
    size: Literal["s", "m", "l"] = "m"
    native: dict
    #: The period it reads, when not the page's: as a platform data source's ``range`` (an imported OpenCTI
    #: dashboard's own period, fixed by whoever edits the dashboard).
    range: Annotated[str | None, Field(default=None, max_length=110)] = None

    @field_validator("range")
    @classmethod
    def _range(cls, value: str | None) -> str | None:
        from app.services.dashboard_definition import check_range

        return check_range(value)

    @model_validator(mode="after")
    def _check_native(self) -> PlatformWidget:
        from app.services.dashboard_platform import NativeError, check_native, resolve_view

        try:
            check = check_native(self.source, self.native)
            self.view = resolve_view(check, self.view)  # type: ignore[assignment]
        except NativeError as exc:
            raise ValueError(str(exc)) from None
        self.native = check.native
        if self.title is None and check.title:
            self.title = check.title[:MAX_TITLE_CHARS]
        return self


DashboardWidget = Annotated[
    Union[  # noqa: UP007 - a discriminated union reads clearer spelled out
        ScoreWidget,
        StatsWidget,
        StagesWidget,
        DecisionWidget,
        FeedWidget,
        CalloutWidget,
        RankedListWidget,
        StepsWidget,
        ChecklistWidget,
        ProgressWidget,
        PromptsWidget,
        TextWidget,
        PlatformWidget,
    ],
    Field(discriminator="kind"),
]

_WIDGET_MODELS: dict[str, type[BaseModel]] = {
    "score": ScoreWidget,
    "stats": StatsWidget,
    "stages": StagesWidget,
    "decision": DecisionWidget,
    "feed": FeedWidget,
    "callout": CalloutWidget,
    "ranked_list": RankedListWidget,
    "steps": StepsWidget,
    "checklist": ChecklistWidget,
    "progress": ProgressWidget,
    "prompts": PromptsWidget,
    "text": TextWidget,
    "platform": PlatformWidget,
}


class DashboardSpec(_Strict):
    title: Title
    #: A line under the title ("Friday 16 Oct · your morning brief").
    subtitle: OptionalShortText
    widgets: Annotated[list[DashboardWidget], Field(min_length=1, max_length=MAX_WIDGETS)]


# ── Validation entry points ──────────────────────────────────────────────────


class DashboardSpecError(ValueError):
    """A dashboard payload or edit that failed validation.

    Carries an agent-readable, field-addressed message: the tools return it
    verbatim so the model can fix the exact widget that is wrong.
    """


def _format_errors(exc: ValidationError, *, root: str = "") -> str:
    """Render Pydantic errors as short, addressable lines for an LLM.

    The resolved member of the discriminated union that Pydantic inserts in the
    path (``widgets.2.score.value``) moves to a ``(kind=score)`` note, so the
    path left is the one the agent built.
    """
    lines: list[str] = []
    errors = exc.errors()
    for err in errors:
        parts = [str(part) for part in err["loc"]]
        note = ""
        if len(parts) >= 3 and parts[0] == "widgets" and parts[2] in _WIDGET_MODELS:
            note = f" (kind={parts[2]})"
            parts = parts[:2] + parts[3:]
        loc = ".".join(p for p in (root, *parts) if p) or "(root)"
        lines.append(f"  - {loc}{note}: {err.get('msg', 'invalid value')}")
        if len(lines) >= 15:
            lines.append(f"  - … and {len(errors) - 15} more error(s)")
            break
    return "\n".join(lines)


_KINDS_HINT = f"Allowed widget kinds: {', '.join(DASHBOARD_WIDGET_KINDS)}."


def assign_widget_ids(widgets: list[dict]) -> list[dict]:
    """Give every widget a unique ``id``, keeping the ones it has.

    A missing id is the widget's kind, suffixed when taken (``score``,
    ``score-2``), so it reads well in the next edit. Refuses duplicates the
    agent wrote itself: two widgets cannot answer to one handle.
    """
    seen: set[str] = set()
    for widget in widgets:
        wid = widget.get("id")
        if wid is None:
            continue
        if wid in seen:
            raise DashboardSpecError(f"Two widgets have the id '{wid}': each widget needs its own id.")
        seen.add(wid)
    for widget in widgets:
        if widget.get("id") is not None:
            continue
        base = str(widget.get("kind") or "widget")
        candidate, n = base, 2
        while candidate in seen:
            candidate, n = f"{base}-{n}", n + 1
        widget["id"] = candidate
        seen.add(candidate)
    return widgets


def validate_spec(payload: object) -> dict:
    """Validate a whole dashboard and return its normalized JSON-safe dict.

    Raises :class:`DashboardSpecError` with a field-addressed message. The
    returned dict, every widget carrying its ``id``, is what is stored.
    """
    if not isinstance(payload, dict):
        raise DashboardSpecError("A dashboard must be a JSON object with a 'title' and a 'widgets' array.")
    try:
        spec = DashboardSpec.model_validate(payload)
    except ValidationError as exc:
        raise DashboardSpecError(
            "The dashboard is invalid and was NOT saved. Fix these field(s) and call the tool again:\n"
            f"{_format_errors(exc)}\n{_KINDS_HINT} Each kind accepts a fixed set of keys - see the tool description."
        ) from exc
    dumped = spec.model_dump(mode="json", exclude_none=True)
    dumped["widgets"] = reading_order(fill_layouts(assign_widget_ids(dumped["widgets"])))
    return dumped


def with_layouts(spec: object) -> object:
    """A stored *spec* with every widget placed on the grid, for display (an older one has no layouts)."""
    if not isinstance(spec, dict) or not isinstance(spec.get("widgets"), list):
        return spec
    widgets = [dict(w) if isinstance(w, dict) else w for w in spec["widgets"]]
    placeable = [w for w in widgets if isinstance(w, dict)]
    if all(isinstance(w.get("layout"), dict) for w in placeable):
        return spec
    fill_layouts(placeable)
    return {**spec, "widgets": widgets}


def validate_widget(payload: object, *, where: str = "widget") -> dict:
    """Validate one widget (an edit's upsert), id kept as given."""
    if not isinstance(payload, dict):
        raise DashboardSpecError(f"{where} must be a JSON object with a 'kind' ({', '.join(DASHBOARD_WIDGET_KINDS)}).")
    kind = payload.get("kind")
    model = _WIDGET_MODELS.get(kind) if isinstance(kind, str) else None
    if model is None:
        raise DashboardSpecError(f"{where}.kind must be one of: {', '.join(DASHBOARD_WIDGET_KINDS)}.")
    try:
        widget = model.model_validate(payload)
    except ValidationError as exc:
        raise DashboardSpecError(f"{where} is invalid and was NOT saved:\n{_format_errors(exc, root=where)}") from exc
    return widget.model_dump(mode="json", exclude_none=True)


def stale_widget_problem(widget: object) -> str | None:
    """Why a stored widget no longer matches the catalog, or ``None``.

    A dashboard keeps rendering such a widget as a placeholder; an edit that
    leaves it in place is refused with this reason, so the agent replaces it.
    """
    try:
        validate_widget(widget)
    except DashboardSpecError as exc:
        return str(exc)
    return None


# ── Partial edits ────────────────────────────────────────────────────────────


class LayoutItem(GridLayout):
    """A widget's new place, in a ``layout`` operation."""

    id: Annotated[str, Field(min_length=1, max_length=40)]


class Operation(_Strict):
    """One step of an edit: ``upsert`` a widget, ``remove`` one, ``move`` one, or ``layout`` (place several)."""

    op: Literal["upsert", "remove", "move", "layout"]
    #: ``upsert``: the whole widget; its ``id`` replaces the widget of that id,
    #: or adds a new one.
    widget: dict | None = None
    #: ``remove`` / ``move``: the widget's id.
    id: Annotated[str | None, Field(default=None, max_length=40)]
    #: ``upsert`` of a new widget / ``move``: place it right after this widget
    #: (``"start"`` for the top). Omitted: a new widget goes last, a moved one
    #: stays where it is.
    after: Annotated[str | None, Field(default=None, max_length=40)]
    #: ``layout``: the widgets' new places on the grid (a drag or a resize).
    items: Annotated[list[LayoutItem] | None, Field(default=None, max_length=MAX_WIDGETS)]


def _place(widgets: list[dict], widget: dict, after: str | None, *, where: str) -> None:
    if after is None:
        widgets.append(widget)
        return
    if after == "start":
        widgets.insert(0, widget)
        return
    for index, current in enumerate(widgets):
        if current.get("id") == after:
            widgets.insert(index + 1, widget)
            return
    raise DashboardSpecError(f"{where}.after names no widget '{after}'.")


def _kept_layout(previous: dict, requested: dict) -> dict | None:
    """The place a replaced widget keeps: the previous one, resized when the edit names another ``width``."""
    layout = previous.get("layout")
    if not isinstance(layout, dict):
        return None
    width = requested.get("width")
    if width is None or width == width_of(layout["w"]):
        return dict(layout)
    w = width_columns(width)
    return {**layout, "w": w, "x": min(layout["x"], 12 - w)}


def apply_operations(
    spec: dict,
    operations: object,
    *,
    title: str | None = None,
    subtitle: str | None = None,
) -> dict:
    """Apply an edit to a stored dashboard and return the new validated spec.

    The operations run in order on a copy; nothing is written unless all of them
    apply and the result validates. Widgets the edit does not touch are kept as
    stored: one written under an older catalog is refused with its reason only
    if the edit leaves it in place, so the agent replaces or removes it.
    """
    if not isinstance(operations, list):
        raise DashboardSpecError("operations must be an array of {op, ...} objects.")
    if len(operations) > MAX_OPERATIONS:
        raise DashboardSpecError(f"An edit holds at most {MAX_OPERATIONS} operations.")

    # Positions are the grid's: "after" and a move read the widgets in the
    # order a person sees them.
    widgets = reading_order(fill_layouts([dict(w) for w in (spec.get("widgets") or []) if isinstance(w, dict)]))
    touched: set[str] = set()
    reorder = False
    for index, raw in enumerate(operations):
        where = f"operations.{index}"
        try:
            op = Operation.model_validate(raw)
        except ValidationError as exc:
            raise DashboardSpecError(f"{where} is invalid:\n{_format_errors(exc, root=where)}") from exc
        if op.op == "upsert":
            if op.widget is None:
                raise DashboardSpecError(f"{where}: an upsert needs 'widget'.")
            widget = validate_widget(op.widget, where=f"{where}.widget")
            wid = widget.get("id")
            existing = next((i for i, w in enumerate(widgets) if wid and w.get("id") == wid), None)
            if existing is not None:
                if "layout" not in widget:
                    widget["layout"] = _kept_layout(widgets[existing], op.widget)
                widgets[existing] = widget
                if op.after is not None:
                    widgets.pop(existing)
                    _place(widgets, widget, op.after, where=where)
                    reorder = True
            else:
                if wid is None:
                    widget = assign_widget_ids([*widgets, widget])[-1]
                _place(widgets, widget, op.after, where=where)
                reorder = reorder or (op.after is not None and "layout" not in widget)
            touched.add(widget["id"])
            continue
        if op.op == "layout":
            if not op.items:
                raise DashboardSpecError(f"{where}: a layout needs 'items' ([{{id, x, y, w, h}}]).")
            by_id = {w.get("id"): w for w in widgets}
            for item in op.items:
                if item.id not in by_id:
                    known = ", ".join(str(w.get("id")) for w in widgets) or "none"
                    raise DashboardSpecError(f"{where}: no widget has the id '{item.id}' (ids: {known}).")
                by_id[item.id]["layout"] = item.model_dump(exclude={"id"})
            continue
        if not op.id:
            raise DashboardSpecError(f"{where}: a {op.op} needs the widget's 'id'.")
        position = next((i for i, w in enumerate(widgets) if w.get("id") == op.id), None)
        if position is None:
            known = ", ".join(str(w.get("id")) for w in widgets) or "none"
            raise DashboardSpecError(f"{where}: no widget has the id '{op.id}' (ids: {known}).")
        if op.op == "remove":
            widgets.pop(position)
            continue
        if op.after is None:
            raise DashboardSpecError(f"{where}: a move needs 'after' (a widget id, or \"start\").")
        if op.after == op.id:
            raise DashboardSpecError(f"{where}: a widget cannot move after itself.")
        moved = widgets.pop(position)
        _place(widgets, moved, op.after, where=where)
        reorder = True

    if reorder:
        # A move or a placement "after" a widget re-flows the grid in the new
        # order, every widget keeping its size.
        reflow(widgets)

    # Widgets left in place that the current catalog no longer accepts: the
    # edit cannot be saved over them silently, and dropping them would lose
    # what the person saw.
    for widget in widgets:
        if widget.get("id") in touched:
            continue
        problem = stale_widget_problem(widget)
        if problem:
            raise DashboardSpecError(
                f"Widget '{widget.get('id')}' no longer matches the widget catalog; replace it with an "
                f"upsert or remove it in the same edit.\n{problem}"
            )

    payload: dict = {
        "title": title if title is not None else spec.get("title"),
        "widgets": widgets,
    }
    sub = subtitle if subtitle is not None else spec.get("subtitle")
    if sub:
        payload["subtitle"] = sub
    return validate_spec(payload)


def widget_catalog() -> dict[str, list[str]]:
    """Map each widget kind to the field names it accepts (docs and tests)."""
    return {kind: [name for name in model.model_fields if name != "kind"] for kind, model in _WIDGET_MODELS.items()}
