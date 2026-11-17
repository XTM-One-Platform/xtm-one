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

"""What a video project and a recording script may contain - the one authority.

Two documents, both validated here and nowhere else:

* :class:`TakeScript` - a recording: where it starts, the ordered browser
  steps (``goto`` / ``click`` / ``double_click`` / ``right_click`` / ``type``
  / ``hover`` / ``drag`` / ``wheel`` / ``scroll`` / ``select`` / ``press`` /
  ``wait`` / ``wait_for`` / ``highlight`` / ``pause`` / ``marker``), the
  viewport and pixel ratio, what to blur or hide;
* :class:`VideoSpec` - the edit: the output format, the brand theme and its
  motion, an ordered list of scenes (``title`` / ``statement`` / ``recording`` /
  ``slide`` / ``figure`` / ``clip`` / ``image`` / ``outro`` / ``motion``),
  transitions, overlays, voice-over, music and captions. Scenes reference recordings, clips, deck slides and images by id;
  ``verify_references`` checks each id against what the caller may read.

Every model is ``extra = "forbid"``: an invented field is a precise,
field-addressed error (``scenes.2 (type=recording): camera.max_zom: Extra
inputs are not permitted``), so an agent repairs one scene instead of guessing.
The quality of the result comes from the renderer, not from prose: the
production decisions a model would otherwise describe (where to zoom, how fast
the cursor moves, how long a scene lasts for its narration) are computed by
``timeline.py`` from the recording's action log (invariant 16).
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_serializer, model_validator

# ── Limits ────────────────────────────────────────────────────────────────

MAX_SCENES = 60
MAX_STEPS = 200
MAX_OVERLAYS_PER_SCENE = 12
MAX_NARRATION_CHARS = 1500
MAX_SELECTORS = 30
#: ``localStorage`` keys a take seeds on its start origin (:attr:`TakeScript.local_storage`).
MAX_SEEDED_KEYS = 20
#: The largest browser zoom a recording takes, in percent: a browser's own.
MAX_BROWSER_ZOOM = 500
SCENE_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"
#: The most references a video keeps: the recurring props and characters its AI shots must match (``reference_drift``).
MAX_REFERENCES = 12
#: The most references one shot names in ``consistency``: as many as a shot takes as reference images
#: (``clips.MAX_REFERENCE_IMAGES``), so every reference a scene names can be sent with its shot.
MAX_CONSISTENCY = 3
_HEX_RE = re.compile(r"^#(?:[0-9a-fA-F]{6})$")

# ── Render quality presets ────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class RenderPreset:
    """How a render encodes: the short side, frame rate and encoder settings."""

    name: str
    short_side: int
    fps: int
    crf: int
    x264_preset: str
    #: How the compositor hands frames to the encoder.
    frame_format: Literal["jpeg", "png"]
    jpeg_quality: int
    audio_bitrate: str = "256k"


QUALITY_PRESETS: dict[str, RenderPreset] = {
    "draft": RenderPreset("draft", 720, 30, 24, "veryfast", "jpeg", 82, "160k"),
    "standard": RenderPreset("standard", 1080, 30, 19, "medium", "jpeg", 92),
    "high": RenderPreset("high", 1080, 60, 16, "slow", "jpeg", 95),
    "ultra": RenderPreset("ultra", 2160, 60, 15, "slow", "png", 100, "320k"),
}
DEFAULT_QUALITY = "high"
#: The qualities a scene preview is drawn at: a 720p draft, or 1080p where its sharpness is what is judged.
PREVIEW_QUALITIES: tuple[str, ...] = ("draft", "standard")
#: The qualities a whole video is judged for before it is rendered, smallest first: a save and a scene
#: preview say what one of them would refuse, a draft being a preview's.
RENDER_QUALITIES: tuple[str, ...] = ("standard", "high", "ultra")
#: The quality a save holds a video's recordings to before its first whole render: the largest a final is
#: rendered at. Once rendered, the quality of its newest whole render (``render_checks.target_quality``).
FINAL_QUALITY = RENDER_QUALITIES[-1]

ASPECT_RATIOS: dict[str, tuple[int, int]] = {"16:9": (16, 9), "9:16": (9, 16), "1:1": (1, 1), "4:5": (4, 5)}

#: The compositor lays every scene out on a canvas whose SHORT side is this
#: many design units; a preset scales it to its own short side.
DESIGN_SHORT_SIDE = 1080


def output_size(aspect_ratio: str, short_side: int) -> tuple[int, int]:
    """Pixel size of a render, both sides even (H.264 4:2:0 needs it)."""
    num, den = ASPECT_RATIOS.get(aspect_ratio, ASPECT_RATIOS["16:9"])
    if num >= den:
        width, height = round(short_side * num / den), short_side
    else:
        width, height = short_side, round(short_side * den / num)
    return width - width % 2, height - height % 2


def design_size(aspect_ratio: str) -> tuple[int, int]:
    return output_size(aspect_ratio, DESIGN_SHORT_SIDE)


# ── Errors ────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class SpecProblem:
    path: str
    message: str


class VideoSpecError(ValueError):
    """A spec the platform refuses. ``str()`` is the repair message an agent reads."""

    def __init__(self, problems: list[SpecProblem], *, what: str = "video") -> None:
        self.problems = problems
        lines = [f"- {p.path}: {p.message}" if p.path else f"- {p.message}" for p in problems[:25]]
        if len(problems) > 25:
            lines.append(f"- ... and {len(problems) - 25} more")
        super().__init__(f"The {what} is not valid:\n" + "\n".join(lines))


def _describe_location(loc: tuple, raw: dict | None) -> str:
    """``scenes.2 (type=recording): camera.max_zoom`` from a Pydantic error location."""
    parts: list[str] = []
    scene_note = ""
    items = list(loc)
    i = 0
    while i < len(items):
        part = items[i]
        if part in ("scenes", "steps") and i + 1 < len(items) and isinstance(items[i + 1], int):
            index = items[i + 1]
            parts.append(f"{part}.{index}")
            if part == "scenes" and raw is not None:
                scenes = raw.get("scenes")
                if isinstance(scenes, list) and 0 <= index < len(scenes) and isinstance(scenes[index], dict):
                    scene_note = f" (type={scenes[index].get('type')})"
            if part == "steps" and raw is not None:
                steps = raw.get("steps")
                if isinstance(steps, list) and 0 <= index < len(steps) and isinstance(steps[index], dict):
                    scene_note = f" (action={steps[index].get('action')})"
            i += 2
            # The discriminated union inserts the tag value: skip it.
            if i < len(items) and isinstance(items[i], str) and items[i] in _SCENE_TYPES | _STEP_ACTIONS:
                i += 1
            continue
        parts.append(str(part))
        i += 1
    if not parts:
        return ""
    head, *rest = parts
    if scene_note and head.startswith(("scenes.", "steps.")):
        return head + scene_note + (": " + ".".join(rest) if rest else "")
    return ".".join(parts)


def _problems_from(exc: ValidationError, raw: Any) -> list[SpecProblem]:
    problems: list[SpecProblem] = []
    raw_dict = raw if isinstance(raw, dict) else None
    for error in exc.errors(include_url=False):
        message = str(error.get("msg") or "invalid value")
        message = message.removeprefix("Value error, ")
        problems.append(SpecProblem(_describe_location(tuple(error.get("loc") or ()), raw_dict), message))
    return problems


# ── Shared bits ───────────────────────────────────────────────────────────


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    @model_validator(mode="before")
    @classmethod
    def _blank_optional_strings_are_absent(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        cleaned = dict(data)
        for name, field in cls.model_fields.items():
            key = field.alias or name
            value = cleaned.get(key)
            if isinstance(value, str) and not value.strip() and not field.is_required():
                cleaned.pop(key)
        return cleaned


def _uuid_text(value: str | None, label: str) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    for prefix in ("file:", "[[FILE:", "take:", "clip:", "music:"):
        if text.lower().startswith(prefix.lower()):
            text = text[len(prefix) :]
    text = text.rstrip("]").strip()
    try:
        return str(uuid.UUID(text))
    except ValueError:
        raise ValueError(f"{label} must be an id (a UUID), got {str(value)[:60]!r}") from None


def _hex_color(value: str | None) -> str | None:
    if value is None:
        return None
    if not _HEX_RE.match(value):
        raise ValueError("a colour is written #RRGGBB")
    return value.lower()


# ── Recording script ──────────────────────────────────────────────────────

_STEP_ACTIONS = frozenset(
    {
        "goto",
        "click",
        "double_click",
        "right_click",
        "type",
        "press",
        "hover",
        "drag",
        "wheel",
        "scroll",
        "select",
        "wait",
        "wait_for",
        "highlight",
        "pause",
        "marker",
    }
)
#: Steps that point at an element of the page.
LOCATOR_ACTIONS = frozenset(
    {"click", "double_click", "right_click", "drag", "type", "hover", "select", "wait_for", "highlight"}
)
#: The pointer actions: besides an element, each acts on a point of the page (``at``) or a node of a graph
#: drawn on a canvas (``node``), keys held down meanwhile (``modifiers``).
POINTER_ACTIONS = frozenset({"click", "double_click", "right_click", "hover", "drag", "wheel"})
#: The pointer actions that must name what they act on; a ``wheel`` without a target turns where the cursor stands.
_TARGETED_POINTER_ACTIONS = POINTER_ACTIONS - {"wheel"}
#: The keys a pointer action may hold down.
POINTER_MODIFIERS = ("Control", "Shift", "Alt", "Meta")
#: Seconds a step waits for its element when its script sets no ``timeout``.
STEP_TIMEOUT_SECONDS = 15.0
OPTIONAL_STEP_TIMEOUT_SECONDS = 3.0
#: Steps that wait or mark a moment and leave the page as it is (a highlight is drawn in post-production).
PASSIVE_ACTIONS = frozenset({"wait", "wait_for", "highlight", "pause", "marker"})
#: Milliseconds between two keystrokes of a ``type`` step without its own ``delay_ms``, by the script's pace.
TYPING_DELAY_MS = {"fast": 28, "natural": 55, "slow": 85}


#: The narrowest viewport a recording is made at, in CSS pixels, and the largest.
MIN_VIEWPORT_WIDTH = 640
MAX_VIEWPORT_WIDTH = 2560
MIN_VIEWPORT_HEIGHT = 480
MAX_VIEWPORT_HEIGHT = 1600


class Viewport(_Strict):
    width: int = Field(1600, ge=MIN_VIEWPORT_WIDTH, le=MAX_VIEWPORT_WIDTH)
    height: int = Field(900, ge=MIN_VIEWPORT_HEIGHT, le=MAX_VIEWPORT_HEIGHT)


class PageReset(_Strict):
    """What a reset of the page state clears (``capture.reset_page``): before a take's step 1, or as a ``goto``
    opens its page. The page's origin keeps, in its ``localStorage`` and its ``sessionStorage``, only the keys its
    profile declares its session's (``keep_storage_keys``); cookies are never cleared."""

    #: The page opened again at its URL exactly as given, never where the page left its address (the parameters an
    #: application writes into it from the state it restored).
    url_query: bool = True
    local_storage: bool = True
    session_storage: bool = True
    #: Its origin's IndexedDB databases deleted, before any of the page's scripts opens one.
    indexed_db: bool = False

    @property
    def clears(self) -> bool:
        return self.url_query or self.local_storage or self.session_storage or self.indexed_db


def page_reset(value: bool | PageReset | None) -> PageReset | None:
    """The reset a ``reset_state`` asks: ``True`` the default one, an object its parts; ``None`` for none."""
    if isinstance(value, PageReset):
        return value if value.clears else None
    return PageReset() if value else None


class StepPoint(_Strict):
    """A point of the page a pointer step acts on: CSS pixels of the window, or with ``in`` of the element that
    selector names - where a value up to 1 is a share of its box (``{x: 0.5, y: 0.5, in: "canvas"}``, its centre) and a
    larger one CSS pixels from its top left corner -, so a point of a canvas holds whatever the window's size."""

    model_config = ConfigDict(serialize_by_alias=True)

    x: float = Field(ge=0.0, le=20000.0)
    y: float = Field(ge=0.0, le=20000.0)
    within: str | None = Field(None, alias="in", max_length=500)


class StepNode(_Strict):
    """A node of a graph drawn on a canvas, found by its label with no hook in the application
    (``node_locate``): the list it keeps for screen readers, the label painted on the canvas read by OCR, the highlight
    the node takes when that list's entry is active. The recorder never reads the application's internals."""

    label: str = Field(min_length=1, max_length=200)


class NodeTarget(_Strict):
    """``{node: {label}}``: where a ``drag`` releases on a node."""

    node: StepNode


class TakeStep(_Strict):
    """One browser action of a recording."""

    action: Literal[
        "goto",
        "click",
        "double_click",
        "right_click",
        "type",
        "press",
        "hover",
        "drag",
        "wheel",
        "scroll",
        "select",
        "wait",
        "wait_for",
        "highlight",
        "pause",
        "marker",
    ]
    #: A CSS or Playwright selector (``text=``, ``role=``, ``#id``, ``.class``).
    selector: str | None = Field(None, max_length=500)
    #: ``type``: what to type. ``click`` / ``hover`` / ``highlight``: the
    #: element's visible text, when no selector is given.
    text: str | None = Field(None, max_length=2000)
    #: An ARIA role and accessible name (``button`` / ``Save``), the most
    #: robust locator when the app has one.
    role: str | None = Field(None, max_length=40)
    name: str | None = Field(None, max_length=200)
    #: Which match, when several elements match (0 = the first).
    nth: int = Field(0, ge=0, le=50)
    url: str | None = Field(None, max_length=2000)
    key: str | None = Field(None, max_length=60)
    #: ``select``: the option chosen - a native ``<select>``'s value or label, else the accessible name of
    #: the option the combobox's list shows.
    value: str | None = Field(None, max_length=500)
    #: ``wait`` / ``pause`` / ``highlight``: how long; ``scroll`` by pixels and ``wheel``: how long the wheel takes,
    #: else the recording's pace (``capture.scroll_seconds``); ``drag``: how long the button is held while
    #: the cursor moves (``capture.DRAG_SECONDS`` unset).
    seconds: float | None = Field(None, ge=0.0, le=60.0)
    #: ``scroll`` / ``wheel``: pixels the wheel turns (positive = down, which most graphs read as zooming out), or for
    #: ``scroll`` ``top`` / ``bottom``; with a locator, a ``scroll`` brings the element into view instead.
    by: int | None = Field(None, ge=-20000, le=20000)
    #: ``scroll``: ``top`` / ``bottom``. ``drag``: where the button is released - a point
    #: (:class:`StepPoint`) or a node (:class:`NodeTarget`).
    to: Literal["top", "bottom"] | StepPoint | NodeTarget | None = None
    #: A pointer action's point of the page (:class:`StepPoint`), in place of an element: a spot of a canvas.
    at: StepPoint | None = None
    #: A pointer action's node of a graph canvas (:class:`StepNode`), in place of an element.
    node: StepNode | None = None
    #: Milliseconds the cursor rests on its target before a pointer action, moving over it: unset, 250 on a
    #: point or a node (a canvas reads its hover from the pointer's moves) and none on an element.
    hover_ms: int | None = Field(None, ge=0, le=3000)
    #: Keys held down through a pointer action: ``Control`` + click adds to a graph's selection.
    modifiers: list[Literal["Control", "Shift", "Alt", "Meta"]] | None = Field(None, max_length=4)
    #: ``type``: the pause between keystrokes (natural typing is 45-90 ms).
    delay_ms: int | None = Field(None, ge=0, le=500)
    clear: bool = False
    submit: bool = False
    #: A step that may fail without failing the take (a cookie banner).
    optional: bool = False
    #: Seconds the step waits for its element; unset, :attr:`wait_seconds` decides. A ``wait`` held until its page
    #: settled: how much longer than its ``seconds`` it may wait (``page_settle.TIMEOUT_SECONDS`` unset).
    timeout: float | None = Field(None, ge=1.0, le=120.0)
    #: ``wait``: held past its ``seconds`` until the page settled - no progress indicator shown, its DOM and
    #: its canvases still (``page_settle``) -, at most its ``timeout`` more. Unset, on after a click that navigated or
    #: switched a tab (``page_settle.until_settled``).
    until_settled: bool | None = None
    #: How the step is named in the action log, the timeline and callouts.
    label: str | None = Field(None, max_length=200)
    #: ``False`` keeps the automatic camera from zooming on this step.
    zoom: bool | None = None
    #: ``goto``: the page it opens starts from a clean state (:class:`PageReset`; ``true`` the default one).
    reset_state: bool | PageReset | None = None

    @property
    def wait_seconds(self) -> float:
        """How long the step waits for its element: its ``timeout``, else less for an optional step.

        An optional step (a banner that may not show) gives up soon, so one
        that is skipped costs a few seconds of the take, which the planner
        then cuts (``timeline.build_time_map``).
        """
        if self.timeout is not None:
            return self.timeout
        return OPTIONAL_STEP_TIMEOUT_SECONDS if self.optional else STEP_TIMEOUT_SECONDS

    @property
    def on_canvas(self) -> bool:
        """Whether the step acts on a point or a node rather than an element."""
        return self.at is not None or self.node is not None

    @model_validator(mode="after")
    def _fields_of_the_action(self) -> TakeStep:
        action = self.action
        has_locator = bool(self.selector or self.role or (self.text and action != "type"))
        if action == "goto" and not self.url:
            raise ValueError("goto needs a url")
        # Without a locator, ``type`` types into the focused element.
        if action == "type" and self.text is None:
            raise ValueError("type needs the text to type")
        if self.on_canvas and action not in POINTER_ACTIONS:
            raise ValueError(f"at and node are the target of a pointer action ({', '.join(sorted(POINTER_ACTIONS))}), not of {action}")
        if action in POINTER_ACTIONS and has_locator + (self.at is not None) + (self.node is not None) > 1:
            raise ValueError(f"{action} acts on one target: an element (selector, role and name, text), at or node")
        if action in _TARGETED_POINTER_ACTIONS and not (has_locator or self.on_canvas):
            raise ValueError(f"{action} needs a selector, a role and name, the element's text, a point (at) or a node")
        if action in {"select", "wait_for", "highlight"} and not has_locator:
            raise ValueError(f"{action} needs a selector, a role and name, or the element's text")
        if (self.hover_ms is not None or self.modifiers) and action not in POINTER_ACTIONS:
            raise ValueError(f"hover_ms and modifiers go with a pointer action ({', '.join(sorted(POINTER_ACTIONS))})")
        released = isinstance(self.to, StepPoint | NodeTarget)
        if action == "drag" and not released:
            raise ValueError("drag needs where it releases: to {x, y, in?} or to {node: {label}}")
        if released and action != "drag":
            raise ValueError("to as a point or a node is where a drag releases; a scroll goes to top or bottom")
        if action == "wheel" and not self.by:
            raise ValueError("wheel needs by: the pixels the wheel turns (negative = up, which most graphs zoom in on)")
        if action == "select" and self.value is None:
            raise ValueError("select needs the value to choose")
        if action == "press" and not self.key:
            raise ValueError("press needs a key (Enter, Tab, Escape, ArrowDown, Control+K...)")
        if action in {"wait", "pause"} and self.seconds is None:
            raise ValueError(f"{action} needs seconds")
        if self.until_settled is not None and action != "wait":
            raise ValueError("until_settled goes with a wait: it holds the page until it settled")
        if self.reset_state is not None and action != "goto":
            raise ValueError("reset_state goes with a goto: the page it opens starts from a clean state")
        if action == "scroll" and self.by is None and self.to is None and not has_locator:
            raise ValueError("scroll needs by (pixels), to (top / bottom) or an element to scroll into view")
        if action == "marker" and not self.label:
            raise ValueError("marker needs a label")
        return self


class TakeScript(_Strict):
    """A recording: the browser steps and how the browser is set up."""

    #: Where the recording starts; defaults to the profile's start URL.
    start_url: str | None = Field(None, max_length=2000)
    steps: list[TakeStep] = Field(min_length=1, max_length=MAX_STEPS)
    viewport: Viewport = Field(default_factory=Viewport)
    #: Device pixels per CSS pixel of the captured frames: 1 is the smoothest
    #: capture, 2 the sharpest under zoom and on a screen insert of a 4K render
    #: (the default - a 1600 px viewport records 3200 px wide), 1.5 the
    #: balance for a video rendered at 1080p.
    pixel_ratio: Literal[1, 1.5, 2] = 2
    #: The page zoomed in, in percent, as a browser zoomed to it lays it out (:func:`zoomed_window`): its text larger
    #: for the width of the frame, the window, the frame and its pixels as they are. Unset, 100.
    browser_zoom: int | None = Field(None, ge=100, le=MAX_BROWSER_ZOOM)
    #: The OS colour preference the browser reports (``prefers-color-scheme``): a page that reads the media query
    #: follows it; an application keeping its theme in its own storage does not (:attr:`local_storage`).
    color_scheme: Literal["light", "dark"] | None = None
    #: ``localStorage`` keys of the start URL's origin, written at the start of each of its documents before its
    #: scripts run, and kept by every page state reset of the take: an application's own preference (its theme, a
    #: dismissed tour) recorded as asked, without touching the signed-in account. ``{"copilot-theme": "light"}``
    #: records the Copilot web app in its light theme.
    local_storage: dict[str, str] = Field(default_factory=dict)
    #: Elements blurred in every frame (emails, tokens, customer names).
    redact: list[str] = Field(default_factory=list, max_length=MAX_SELECTORS)
    #: Elements removed from the page (cookie banners, chat widgets).
    hide: list[str] = Field(default_factory=list, max_length=MAX_SELECTORS)
    #: Pause after each action so the page settles on screen.
    settle_ms: int = Field(600, ge=0, le=5000)
    #: Held at the end of the recording.
    end_hold_ms: int = Field(1200, ge=0, le=10000)
    #: How fast the cursor travels to its targets.
    pace: Literal["fast", "natural", "slow"] = "natural"
    #: The rate its motion plays at: a scroll painted at fewer frames a second is drawn again at it from the
    #: frames the browser painted (``scroll_pacing``). 60 for a scene rendered at 60 fps.
    fps: Literal[30, 60] = 30
    #: What a worker that cannot capture the take's size at its ``fps`` does (``capture_rate.decide``): records
    #: at the largest size it measured sustaining it (``size``), at fps 30 at this size (``fps``), or refuses (naming
    #: both).
    fps_fallback: Literal["size", "fps", "refuse"] = "refuse"
    #: Record the sign-in too (off: the take starts on the signed-in app).
    record_sign_in: bool = False
    #: The page state the take starts from (:class:`PageReset`): what its saved session carried over from the
    #: takes before - the address's parameters, the origin's storage - cleared before step 1.
    reset_state: bool | PageReset = True

    @field_validator("local_storage")
    @classmethod
    def _local_storage(cls, values: dict[str, str]) -> dict[str, str]:
        if len(values) > MAX_SEEDED_KEYS:
            raise ValueError(f"local_storage seeds at most {MAX_SEEDED_KEYS} keys")
        for key, value in values.items():
            if not key.strip() or len(key) > 200:
                raise ValueError("a local_storage key is 1 to 200 characters")
            if len(value) > 4000:
                raise ValueError(f"local_storage[{key!r}] is at most 4000 characters")
        return values

    @field_validator("redact", "hide")
    @classmethod
    def _selectors(cls, values: list[str]) -> list[str]:
        cleaned = [v.strip() for v in values if isinstance(v, str) and v.strip()]
        for value in cleaned:
            if len(value) > 500:
                raise ValueError("a selector is at most 500 characters")
        return cleaned

    @model_validator(mode="after")
    def _points_in_the_window(self) -> TakeScript:
        # A point of the page is in CSS pixels of the window the page is laid out in: its browser zoom's.
        (width, height), _ratio = zoomed_window(
            (self.viewport.width, self.viewport.height), float(self.pixel_ratio), self.browser_zoom or 100
        )
        for index, step in enumerate(self.steps):
            for name, point in (("at", step.at), ("to", step.to)):
                if isinstance(point, StepPoint) and point.within is None and (point.x > width or point.y > height):
                    raise ValueError(
                        f"steps.{index}.{name}: ({point.x:g}, {point.y:g}) lies outside the {width}x{height} window the "
                        "page is laid out in (CSS pixels of the page); give in: 'SELECTOR' for a point of an element"
                    )
        return self


def browser_zoom(script: dict | None) -> int:
    """The browser zoom a stored recording script asks, in percent: 100 when it names none."""
    return int((script or {}).get("browser_zoom") or 100)


def zoomed_window(viewport: tuple[int, int], pixel_ratio: float, zoom: int) -> tuple[tuple[int, int], float]:
    """``(CSS viewport, device pixel ratio)`` a page in a *viewport* window at *pixel_ratio* is laid out at, zoomed to
    *zoom* percent: the window over the zoom in CSS pixels, painted onto the same frame - a browser's own zoom."""
    width, height = viewport
    css = (max(1, round(width * 100 / zoom)), max(1, round(height * 100 / zoom)))
    return css, width * float(pixel_ratio) / css[0]


def painted_size(css: tuple[int, int], pixel_ratio: float) -> tuple[int, int]:
    """The size of a recording's frames, in pixels: its stored CSS viewport at the ratio its page was laid out at -
    ``3200x1801`` for a 1600x900 window at pixel ratio 2 zoomed to 190% (laid out at 842x474, ratio 3.8)."""
    return round(css[0] * float(pixel_ratio)), round(css[1] * float(pixel_ratio))


def recorded_ratio(pixel_ratio: float, script: dict | None) -> float:
    """The pixel ratio a recording was made at (``TakeScript.pixel_ratio``: device pixels per pixel of its window): its
    stored *script*'s, else read back from the ratio its page was laid out at under the script's browser zoom
    (:func:`zoomed_window`), which its CSS viewport's rounding moves by a hair."""
    asked = (script or {}).get("pixel_ratio")
    if isinstance(asked, int | float) and not isinstance(asked, bool) and asked > 0:
        return float(asked)
    return float(pixel_ratio) * 100 / browser_zoom(script)


def validate_take_script(raw: Any) -> dict:
    """The normalised recording script, or :class:`VideoSpecError`."""
    try:
        script = TakeScript.model_validate(raw)
    except ValidationError as exc:
        raise VideoSpecError(_problems_from(exc, raw), what="recording script") from None
    return stored_take_script(script)


# ── Video spec: shared scene parts ────────────────────────────────────────

_SCENE_TYPES = frozenset({"title", "statement", "recording", "slide", "figure", "clip", "image", "outro", "motion"})
TRANSITION_TYPES = (
    "auto",
    "cut",
    "crossfade",
    "fade_black",
    "slide_left",
    "slide_up",
    "zoom",
    "blur",
    "wipe",
    "whip",
    "flash",
)
MOTIONS = ("ken_burns", "zoom_in", "zoom_out", "pan_left", "pan_right", "none")
POSITIONS = (
    "auto",
    "top",
    "bottom",
    "left",
    "right",
    "center",
    "top_left",
    "top_right",
    "bottom_left",
    "bottom_right",
)


class Transition(_Strict):
    """How a scene comes in.

    ``auto`` - the default - lets the planner pick each cut from the scenes on
    either side, the way an editor would (``timeline.auto_transition``), at the
    ``pace`` of the video: ``calm`` dissolves and blurs, ``dynamic`` pushes and
    whips, ``classic`` only cuts, crossfades and fades to black - a
    brand film's grammar. A named type is used as named, for its ``duration``
    or its own length.
    """

    type: Literal[
        "auto", "cut", "crossfade", "fade_black", "slide_left", "slide_up", "zoom", "blur", "wipe", "whip", "flash"
    ] = "auto"
    #: Seconds; unset, the type's own length (``timeline.TRANSITION_SECONDS``).
    duration: float | None = Field(None, ge=0.1, le=2.0)
    #: How ``auto`` cuts: ``calm``, ``dynamic`` or ``classic``.
    pace: Literal["calm", "dynamic", "classic"] = "calm"

    @model_validator(mode="before")
    @classmethod
    def _fields_of_the_type(cls, data: Any) -> Any:
        # A one-of-like group: a field the type does not take is dropped, so
        # an edit setting `cut` over `{auto, duration}` leaves no duration behind.
        if not isinstance(data, dict):
            return data
        taken = transition_fields(str(data.get("type") or "auto"))
        return {key: value for key, value in data.items() if key not in _TYPE_SCOPED_FIELDS or key in taken}


#: What each transition type takes besides its ``type``: ``auto`` its pace and a length,
#: a named move its length, a cut nothing.
_TRANSITION_FIELDS = {"auto": frozenset({"duration", "pace"}), "cut": frozenset()}
_TYPE_SCOPED_FIELDS = frozenset({"duration", "pace"})


def transition_fields(kind: str) -> frozenset[str]:
    """The fields a transition of *kind* takes besides its type: a cut none, a named move its ``duration``."""
    return _TRANSITION_FIELDS.get(kind, frozenset({"duration"}))


#: The brand marks of a video's template an image overlay may name by role, never by file.
BRAND_ASSETS = ("logo", "logo_light", "logo_mark")


def brand_marks(marks: dict[str, Any], *, light: bool, own_logo: Any = None, stand_in: bool = True) -> dict[str, Any]:
    """What each brand mark draws, from the template's *marks* by role (a file id, a caption: any value).

    ``logo`` is the logo the cards draw: the light variant in a *light*
    video, the spec's *own_logo* when it names one; ``logo_light`` the light
    variant, else that logo; ``logo_mark`` the mark, else that logo. ``None``:
    no such mark. One rule for the render, the preview and the template's cards.
    Without *stand_in* (a screen insert's source), a role the template
    has no file for is ``None``: the logo never stands for the mark it is not.
    """
    logo = own_logo or (marks.get("logo_light") if light and marks.get("logo_light") else marks.get("logo"))
    if not stand_in:
        return {"logo": logo, "logo_light": marks.get("logo_light"), "logo_mark": marks.get("logo_mark")}
    return {"logo": logo, "logo_light": marks.get("logo_light") or logo, "logo_mark": marks.get("logo_mark") or logo}


def card_draws_logo(kind: object, show_logo: object, watermark: object) -> bool:
    """Whether a card of type *kind* draws its logo: asked (*show_logo*), and on a statement only with no watermark.

    The watermark (``theme.watermark``) draws the brand's mark over the
    content scenes, a statement among them: its logo would put the mark on the
    frame twice. A title or closing card carries the brand itself - the
    watermark steps aside for it. One rule for the planner and the brand
    checks of the template (``templates.brand_repeats``).
    """
    if not show_logo:
        return False
    return kind != "statement" or (watermark or "none") == "none"


#: An image overlay's width when it names none: a share of the frame's width.
IMAGE_OVERLAY_WIDTH = 0.12
#: The fields only an image overlay takes; a text overlay takes none of them.
_IMAGE_OVERLAY_FIELDS = frozenset({"file_id", "asset", "width", "opacity"})
#: The fields only a screen insert takes (its panes; its key; its light; its legibility;
#: its push; its edge despill).
_INSERT_OVERLAY_FIELDS = frozenset(
    {
        "source",
        "sources",
        "panes",
        "gutter",
        "quad",
        "trim_start",
        "fit",
        "glow",
        "light",
        "track",
        "key",
        "legibility",
        "zoom",
        "despill",
    }
)
#: How a screen insert is judged: ``legible`` (the default) is there to be read and its text is checked at save
#: (``insert_legibility``); ``ambient`` is scenery - a recording on one of several monitors sharing a frame -: its text
#: is measured and said, never judged.
INSERT_LEGIBILITY = ("legible", "ambient")
#: What a screen insert says of how it is judged, never of what is drawn: left out of what a scene's drawing is
#: recorded under (``render_checks.digest_scenes``), so declaring it keeps the scene's preview.
UNDRAWN_INSERT_FIELDS = frozenset({"legibility"})
#: What a scene says of how its AI shot is generated and checked, never of what is drawn: the video's
#: references it is held to. Left out of the scene's ``effective`` digest (``render_checks.digest_scenes``), so adding
#: or removing them keeps the scene's preview.
UNDRAWN_SCENE_FIELDS = frozenset({"consistency"})
#: What a scene says of its sound, never of its frames: a clip's own sound and its level. Left out of what a
#: whole render draws (``render_checks.digest_scenes``' ``drawn``), so a render in progress outlives an edit of them.
HEARD_SCENE_FIELDS = frozenset({"audio", "volume"})
#: What the voice-over says of its sound alone: its level. Its voice, speed and pronunciations time the
#: narrations and their captions, so they are part of what is drawn.
HEARD_VOICEOVER_FIELDS = frozenset({"volume"})
#: How much of the footage's light and reflections plays over a screen insert that names no ``light``.
INSERT_DEFAULT_LIGHT = 0.15
#: How long a screen insert takes to come in after a ``start`` past its scene's own and to go before its end:
#: the page's ramp, and how much of a keyed insert the frame server composes over its surface
#: (``timeline.insert_visibility``).
INSERT_FADE_SECONDS = 0.2
#: The fields an image overlay or a screen insert does not take (their ``text`` is their label).
_TEXT_OVERLAY_FIELDS = frozenset({"subtext", "style"})
_TYPED_OVERLAY_FIELDS = {"image": _IMAGE_OVERLAY_FIELDS, "screen_insert": _INSERT_OVERLAY_FIELDS}
_ALL_TYPED_OVERLAY_FIELDS = _IMAGE_OVERLAY_FIELDS | _INSERT_OVERLAY_FIELDS | _TEXT_OVERLAY_FIELDS
#: The scene types whose footage a screen insert maps onto (a statement only over a backdrop): an image scene's still
#: is footage as a statement's picture backdrop is - a room with its monitors, a wall shot locked.
SCREEN_INSERT_SCENES = ("clip", "recording", "statement", "image")
#: How far a quad's corner may stand outside its footage (0-1 is the picture): the quality check says when it does.
QUAD_MARGIN = 1.0
#: The smallest area a quad encloses, as a share of the footage: below it, nothing legible fits.
MIN_QUAD_AREA = 0.0004
#: A screen insert's panes: at most this many rows or columns, this many in all.
MAX_PANE_SIDE = 6
MAX_PANES = 16
#: The gutter between two panes when the insert names none: a share of the quad's width (columns) or height (rows).
DEFAULT_PANE_GUTTER = 0.015

QuadCoordinate = Annotated[float, Field(ge=-QUAD_MARGIN, le=1.0 + QUAD_MARGIN)]
#: A screen insert's quad left to the render: the bezel found on the insert's first footage frame.
AUTO_QUAD = "auto"


def quad_problems(quad: list[tuple[float, float]]) -> list[str]:
    """Why *quad* is not a surface a picture can be mapped onto (empty: it is).

    Its corners go top left, top right, bottom right, bottom left, so with
    ``y`` growing downwards every corner turns the same way (a convex quad,
    clockwise on screen); a bow tie or a quad given in another order would
    fold the picture over itself.
    """
    if len(quad) != 4:
        return ["a quad is four corners [x, y]: top left, top right, bottom right, bottom left"]
    turns = []
    for index in range(4):
        (ax, ay), (bx, by), (cx, cy) = quad[index], quad[(index + 1) % 4], quad[(index + 2) % 4]
        turns.append((bx - ax) * (cy - by) - (by - ay) * (cx - bx))
    if not all(turn > 0 for turn in turns):
        return [
            "the quad's corners must go top left, top right, bottom right, bottom left around a convex surface "
            "(as the surface's own corners are drawn in the footage)"
        ]
    area = 0.5 * sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(quad, quad[1:] + quad[:1], strict=True))
    if area < MIN_QUAD_AREA:
        return [f"the quad encloses {area:.5f} of the footage: too small to show anything legible"]
    return []


def pane_quads(quad: list[tuple[float, float]], rows: int, cols: int, gutter: float) -> list[list[tuple[float, float]]]:
    """The quads of a screen insert's panes, row by row: *quad* split *rows* x *cols*, *gutter* between them.

    Split on the surface, not on the footage: each pane's corners are taken
    through the projective map of the unit square onto *quad* (the one the
    page's ``quadMatrix`` builds), at the pane's place on the flat wall, so a
    wall seen at an angle keeps its panes in perspective. The gutter is a
    share of the quad's width between two columns and of its height between
    two rows; none stands outside the quad.
    """
    (x0, y0), (x1, y1), (x2, y2), (x3, y3) = quad
    dx1, dx2, sx = x1 - x2, x3 - x2, x0 - x1 + x2 - x3
    dy1, dy2, sy = y1 - y2, y3 - y2, y0 - y1 + y2 - y3
    g = h = 0.0
    if abs(sx) > 1e-12 or abs(sy) > 1e-12:
        den = dx1 * dy2 - dx2 * dy1
        g, h = (sx * dy2 - dx2 * sy) / den, (dx1 * sy - sx * dy1) / den
    a, b, d, e = x1 - x0 + g * x1, x3 - x0 + h * x3, y1 - y0 + g * y1, y3 - y0 + h * y3

    def at(u: float, v: float) -> tuple[float, float]:
        w = g * u + h * v + 1.0
        return (round((a * u + b * v + x0) / w, 6), round((d * u + e * v + y0) / w, 6))

    width, height = (1.0 - gutter * (cols - 1)) / cols, (1.0 - gutter * (rows - 1)) / rows
    panes = []
    for row in range(rows):
        for col in range(cols):
            u, v = col * (width + gutter), row * (height + gutter)
            panes.append([at(u, v), at(u + width, v), at(u + width, v + height), at(u, v + height)])
    return panes


class InsertSource(_Strict):
    """What a screen insert shows: a picture or a video file, a recording, an AI shot, or a brand mark of the template.

    ``asset`` names the template's own file by its role: ``logo`` (the
    full logo, emblem and wordmark), ``logo_light`` (its light variant) or
    ``logo_mark`` (the bare mark) - three files, never one standing for another.
    """

    file_id: str | None = None
    take_id: str | None = None
    clip_id: str | None = None
    asset: Literal["logo", "logo_light", "logo_mark"] | None = None

    @field_validator("file_id", "take_id", "clip_id")
    @classmethod
    def _ids(cls, value: str | None, info) -> str | None:
        return _uuid_text(value, info.field_name)

    @model_validator(mode="after")
    def _one_source(self) -> InsertSource:
        if sum(bool(value) for value in (self.file_id, self.take_id, self.clip_id, self.asset)) != 1:
            raise ValueError(
                f"a screen insert's source is exactly one of file_id, take_id, clip_id or asset ({' / '.join(BRAND_ASSETS)})"
            )
        return self


#: The flat colours a screen insert's key names by name; any other is given as ``#rrggbb``.
INSERT_KEY_COLORS = ("green", "blue", "magenta")


class InsertKey(_Strict):
    """A screen insert's chroma key: the colour of the surface the source replaces, how near, how soft.

    The source is drawn only on the pixels of its quad that show the key
    colour in that frame, the key's spill taken out of what stands in front:
    a cat or a hand passing over the surface stays in front of the insert.
    """

    color: str = "green"
    #: How far a pixel's chromaticity may stray from the key's and still be the surface (a share of the key's own).
    tolerance: float = Field(0.3, ge=0.05, le=1.0)
    #: The matte's soft edge, in pixels of a 1080p frame.
    feather: float = Field(1.5, ge=0.0, le=20.0)

    @field_validator("color")
    @classmethod
    def _color(cls, value: str) -> str:
        text = str(value).strip().lower()
        if text in INSERT_KEY_COLORS or _HEX_RE.match(text):
            return text
        raise ValueError(f"a key's color is {', '.join(INSERT_KEY_COLORS)} or a #rrggbb colour, got {str(value)[:20]!r}")


#: The most a screen insert's push draws its source within its quad: four times its fitted size.
MAX_INSERT_ZOOM = 4.0
_Share = Annotated[float, Field(ge=0.0, le=1.0)]


class InsertZoom(_Strict):
    """A camera push on a screen insert: its source drawn larger within its quad, panned to a region.

    From ``start`` to ``end`` - seconds after the insert comes in - the
    source eases from its fitted size to ``scale`` times it, centred on
    ``focus`` (``[x, y]``, 0-1 across and down the source, kept inside it),
    and holds there to the insert's end (``screen_insert.zoomed``). What the
    push brings to the viewer is the source's own text, drawn larger: the
    page keeps the layout it was recorded with.
    """

    start: float = Field(0.0, ge=0.0, le=60.0)
    end: float = Field(gt=0.0, le=60.0)
    scale: float = Field(gt=1.0, le=MAX_INSERT_ZOOM)
    focus: tuple[_Share, _Share] = (0.5, 0.5)

    @model_validator(mode="after")
    def _ordered(self) -> InsertZoom:
        if self.end <= self.start:
            raise ValueError("a zoom's end comes after its start: both are seconds after the insert comes in")
        return self


#: How far past the edge of what stands in front a keyed insert's edge despill reaches when it names none, in
#: pixels of a 1080p frame, the key's feather added; and the farthest it may be asked to.
INSERT_DESPILL_WIDTH = 2.0
MAX_INSERT_DESPILL_WIDTH = 16.0
#: A hue named by where it falls on the wheel (HSV degrees, each name up to its bound): a cast's ("tinted blue",
#: ``screen_insert.hue_name``) and the colours an author declares what stands in front of a keyed insert holds
#: (:class:`InsertDespill`).
HUE_NAMES = ((15.0, "red"), (45.0, "orange"), (70.0, "yellow"), (160.0, "green"), (200.0, "cyan"), (260.0, "blue"), (290.0, "purple"), (345.0, "magenta"), (360.0, "red"))
#: The most own colours a keyed insert declares.
MAX_INSERT_OWN_HUES = 8


class InsertDespill(_Strict):
    """How a keyed screen insert takes the key's spill off the edge of what stands in front.

    A subject in front of a keyed surface carries the surface's colour on its
    own edge in the footage - fur, hair, a sleeve. With ``edge`` (the
    default) the key's colour is limited out of that edge, its luminance
    kept, ``width_px`` past it (the key's feather added) and fading to
    nothing as far into the subject, at ``strength``; the edge then takes a
    little of the insert's light behind it. ``edge: false`` draws that edge
    as the footage shows it, the key's clamp alone taken out of it.
    ``own_hues`` are colours the subject itself holds - a hue name
    of :data:`HUE_NAMES` or a ``#rrggbb`` -, never spill on its edge: a pink
    muzzle in front of a green panel, the key's complement.
    """

    edge: bool = True
    width_px: float = Field(INSERT_DESPILL_WIDTH, ge=0.0, le=MAX_INSERT_DESPILL_WIDTH)
    strength: float = Field(1.0, ge=0.0, le=1.0)
    own_hues: list[str] | None = Field(None, max_length=MAX_INSERT_OWN_HUES)

    @field_validator("own_hues")
    @classmethod
    def _own_hues(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        names = list(dict.fromkeys(name for _bound, name in HUE_NAMES))
        out = []
        for given in value:
            text = str(given).strip().lower()
            if text not in names and not _HEX_RE.match(text):
                raise ValueError(f"an own hue is a colour name ({', '.join(names)}) or a #rrggbb colour, got {str(given)[:20]!r}")
            out.append(text)
        return list(dict.fromkeys(out)) or None


class Overlay(_Strict):
    """Drawn over a scene: a lower third, a callout bubble, a badge, a big caption, a spotlight - or a picture.

    An ``image`` overlay draws a picture - a logo, an emblem, a product
    mark - at a position, a width and a time, above the scene's picture and
    below the captions: a file (``file_id``) or a brand mark of the template by
    its role (``asset``). Its ``text`` is its label (what the quality check
    names it). The fields a type does not take are dropped, so switching a
    callout to a picture is one field.

    A ``screen_insert`` maps a picture or a video - a file, a
    recording, an AI shot, a brand mark of the template - onto a surface of
    the scene's own footage: the monitor, billboard or poster a video model
    drew, given as its four corners (``quad``, 0-1 across and down the
    footage, top left, top right, bottom right, bottom left). The render warps
    it frame by frame (``screen_insert.py``), so it moves with the scene's
    camera and stays legible at the render's resolution; ``glow`` blooms its
    light onto the scene, around what it draws, and ``light`` lays the
    footage's own light and reflections over it - as light, never as a colour
    cast on its blacks (``screen_insert.ambient_light``). ``track`` follows the
    quad - given on the footage frame the insert comes in on - through the
    footage, so the insert stays on a surface a moving camera carries
    (``insert_tracking``). ``key`` draws it only where the surface
    shows its key colour; without one, what enters the surface through its
    edge stays in front of it (the occlusion of ``screen_insert``). A wall of
    screens is one insert: ``panes`` (``N`` side by side, or
    ``[rows, cols]``) splits the quad into panes with a ``gutter`` between
    them (:func:`pane_quads`), each showing its own entry of ``sources``, row
    by row; tracked, the wall is followed once and its panes move with it;
    keyed, every pane is matted with the wall's key. ``legibility: ambient``
    makes it scenery, whose text the save never judges (``insert_legibility``).
    ``zoom`` pushes in on what it shows (:class:`InsertZoom`), every
    pane of a wall alike. ``despill`` says how a keyed insert takes
    the key's spill off the edge of what stands in front (:class:`InsertDespill`).
    """

    type: Literal["lower_third", "callout", "badge", "caption", "spotlight", "image", "screen_insert"] = "callout"
    text: str = Field("", max_length=200)
    subtext: str | None = Field(None, max_length=200)
    #: Seconds from the start of the scene; with ``at_step``, from that step.
    start: float = Field(0.0, ge=0.0, le=600.0)
    duration: float = Field(3.0, ge=0.3, le=60.0)
    #: Recording scenes: the step (0-based, as in the script) the overlay
    #: points at - its time and, for a callout or a spotlight, its element.
    at_step: int | None = Field(None, ge=0, le=MAX_STEPS - 1)
    position: Literal[
        "auto",
        "top",
        "bottom",
        "left",
        "right",
        "center",
        "top_left",
        "top_right",
        "bottom_left",
        "bottom_right",
    ] = "auto"
    style: Literal["accent", "glass", "dark", "light"] = "glass"
    #: ``image``: the picture drawn - a conversation file, an upload, a template asset.
    file_id: str | None = None
    #: ``image``: a brand mark of the video's template by its role, rather than a file.
    asset: Literal["logo", "logo_light", "logo_mark"] | None = None
    #: ``image``: its width - a share of the frame's width (up to 1), else design pixels of a 1080p frame.
    width: float | None = Field(None, gt=0.0, le=1920.0)
    #: ``image``: how opaque it is (1 = fully).
    opacity: float | None = Field(None, ge=0.05, le=1.0)
    #: ``screen_insert``: what is mapped onto the surface.
    source: InsertSource | None = None
    #: ``screen_insert``, a wall: its panes, ``N`` side by side or ``[rows, cols]``; each shows one of ``sources``.
    panes: int | tuple[int, int] | None = None
    #: ``screen_insert`` with ``panes``: what each pane shows, row by row and left to right.
    sources: list[InsertSource] | None = Field(None, min_length=2, max_length=MAX_PANES)
    #: ``screen_insert`` with ``panes``: the gap between two panes, a share of the quad's width (or height, between rows).
    gutter: float | None = Field(None, ge=0.0, le=0.1)
    #: ``screen_insert``: the surface's corners in the footage, 0-1: top left, top right, bottom right, bottom left -
    #: or ``auto`` on a static insert: the bezel found on its first footage frame. A static insert without one
    #: is ``auto``: the save measures that bezel and stores it here (``insert_bezels``).
    quad: Annotated[list[tuple[QuadCoordinate, QuadCoordinate]], Field(min_length=4, max_length=4)] | Literal["auto"] | None = None
    #: ``screen_insert``: seconds of a video source skipped at its start.
    trim_start: float | None = Field(None, ge=0.0, le=3600.0)
    #: ``screen_insert``: ``cover`` fills the surface and cuts the source's edges, ``contain`` keeps it whole on
    #: transparency (the surface shows around it, never a black card).
    fit: Literal["cover", "contain"] | None = None
    #: ``screen_insert``: how much light blooms from the inserted picture onto the scene (0 = none), around its
    #: silhouette (a PNG's alpha), never the quad's rectangle.
    glow: float | None = Field(None, ge=0.0, le=1.0)
    #: ``screen_insert``: how much of the footage's ambient light and reflections play over the insert: 0 draws
    #: the source as it is; unset, :data:`INSERT_DEFAULT_LIGHT`.
    light: float | None = Field(None, ge=0.0, le=1.0)
    #: ``screen_insert``: follow the quad through the scene's footage; off, it stays where it is given.
    track: bool | None = None
    #: ``screen_insert``: the chroma key of the surface; unset, the surface's look on the first frame is the
    #: reference, and what enters it through its edge stays in front.
    key: InsertKey | None = None
    #: ``screen_insert``: :data:`INSERT_LEGIBILITY`; unset, ``legible``.
    legibility: Literal["legible", "ambient"] | None = None
    #: ``screen_insert``: a camera push on what it shows (:class:`InsertZoom`); unset, drawn at its fitted size.
    zoom: InsertZoom | None = None
    #: ``screen_insert`` with a ``key``: how the key's spill is taken off the edge of what stands in front
    #: (:class:`InsertDespill`); unset, on, :data:`INSERT_DESPILL_WIDTH` px wide, at full strength.
    despill: InsertDespill | None = None

    @model_validator(mode="before")
    @classmethod
    def _fields_of_the_type(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        taken = _TYPED_OVERLAY_FIELDS.get(str(data.get("type") or ""), _TEXT_OVERLAY_FIELDS)
        dropped = _ALL_TYPED_OVERLAY_FIELDS - taken
        return {key: value for key, value in data.items() if key not in dropped}

    @field_validator("file_id")
    @classmethod
    def _file_id(cls, value: str | None) -> str | None:
        return _uuid_text(value, "file_id")

    @model_validator(mode="after")
    def _text_unless_spotlight(self) -> Overlay:
        if self.type == "image":
            if bool(self.file_id) == bool(self.asset):
                raise ValueError(
                    "an image overlay draws one picture: a file_id, or a brand mark of the template "
                    f"(asset: {' / '.join(BRAND_ASSETS)})"
                )
            return self
        if self.type == "screen_insert":
            if self.source is None and not self.sources:
                raise ValueError("a screen insert needs its source ({file_id | take_id | clip_id | asset})")
            if self.quad is None and self.track:
                raise ValueError(
                    "a tracked screen insert needs its quad, the surface's four corners (0-1: top left, top right, bottom "
                    "right, bottom left) on the footage frame it comes in on: the track follows them from there"
                )
            if self.auto_quad:
                if self.track:
                    raise ValueError(
                        "quad auto is the bezel found on the insert's first footage frame, drawn there on every frame: "
                        "a tracked insert takes its quad given on that frame"
                    )
            elif problems := quad_problems([(float(x), float(y)) for x, y in self.quad]):
                raise ValueError(problems[0])
            self._check_panes()
            if self.zoom is not None and self.zoom.end > self.duration + 1e-6:
                raise ValueError(
                    f"its zoom ends {self.zoom.end:g} s after the insert comes in, past its {self.duration:g} s: end the "
                    "push within the insert"
                )
            return self
        if self.type != "spotlight" and not self.text:
            raise ValueError(f"a {self.type} overlay needs text")
        return self

    def _check_panes(self) -> None:
        if self.panes is None:
            if self.sources:
                raise ValueError("sources are a wall's, one per pane: give panes (N, or [rows, cols]) with them")
            if self.gutter is not None:
                raise ValueError("gutter is the gap between a wall's panes: give panes with it")
            return
        if self.source is not None:
            raise ValueError("a wall of panes shows sources, one per pane: drop source (or panes)")
        rows, cols = self.grid or (1, 1)
        if not (1 <= rows <= MAX_PANE_SIDE and 1 <= cols <= MAX_PANE_SIDE and 2 <= rows * cols <= MAX_PANES):
            raise ValueError(
                f"panes is N side by side or [rows, cols]: 2 to {MAX_PANES} panes, at most {MAX_PANE_SIDE} a side"
            )
        if len(self.sources or []) != rows * cols:
            raise ValueError(
                f"a wall of {rows} x {cols} panes shows {rows * cols} sources, one per pane row by row: "
                f"{len(self.sources or [])} given"
            )

    @property
    def grid(self) -> tuple[int, int] | None:
        """``(rows, cols)`` of a screen insert's panes; ``None`` for one source on the whole quad."""
        if self.panes is None:
            return None
        return (1, int(self.panes)) if isinstance(self.panes, int) else (int(self.panes[0]), int(self.panes[1]))

    @property
    def pane_gutter(self) -> float:
        """The gap between a wall's panes: its ``gutter``, else :data:`DEFAULT_PANE_GUTTER`."""
        return DEFAULT_PANE_GUTTER if self.gutter is None else float(self.gutter)

    @property
    def auto_quad(self) -> bool:
        """A screen insert whose quad is found on its footage (:data:`AUTO_QUAD`): ``auto``, or none on a static
        insert."""
        return self.type == "screen_insert" and (self.quad == AUTO_QUAD or (self.quad is None and not self.track))

    def insert_panes(self) -> list[tuple[int | None, InsertSource, list[tuple[float, float]]]]:
        """What a screen insert draws, ``(pane, source, quad)``: each pane of a wall, or ``(None, source, quad)``.

        The one reading of a screen insert's surfaces: the planner,
        the render, the preview and the checks all go through it. A quad left
        to the render (:attr:`auto_quad`) draws nothing until the render finds
        it (``timeline.with_quads``): nothing judges a surface not found yet.
        """
        if self.type != "screen_insert" or self.quad is None or self.auto_quad:
            return []
        quad = [(float(x), float(y)) for x, y in self.quad]
        if self.grid is None:
            return [(None, self.source, quad)] if self.source is not None else []
        rows, cols = self.grid
        quads = pane_quads(quad, rows, cols, self.pane_gutter)
        return [(index, source, pane) for index, (source, pane) in enumerate(zip(self.sources or [], quads, strict=True))]

    @property
    def shown_sources(self) -> list[InsertSource]:
        """Every source a screen insert shows: its one source, or its panes' - a quad left to the render's too."""
        if self.auto_quad:
            return [self.source] if self.source is not None else list(self.sources or [])
        return [source for _pane, source, _quad in self.insert_panes()]


#: How a scene whose footage ends before its narration fills the rest: ``zoom`` plays the footage
#: slower to fill it and drifts in on whatever still holds (a Ken Burns), ``loop`` plays its last second
#: again and again, crossfaded, ``hold`` freezes its last frame.
EXTEND_MODES = ("hold", "loop", "zoom")
ExtendMode = Literal["hold", "loop", "zoom"]


class _SceneBase(_Strict):
    id: str | None = Field(None, pattern=SCENE_ID_PATTERN)
    #: Spoken over the scene by the voice-over; the scene lasts at least as long.
    narration: str | None = Field(None, max_length=MAX_NARRATION_CHARS)
    #: Seconds of the scene before the narration starts.
    narration_delay: float = Field(0.35, ge=0.0, le=10.0)
    #: How fast this scene's narration is said, in place of ``voiceover.speed``: a short action line picked up.
    narration_speed: float | None = Field(None, ge=0.7, le=1.3)
    #: How this scene comes in; the spec's ``default_transition`` otherwise.
    transition: Transition | None = None
    overlays: list[Overlay] = Field(default_factory=list, max_length=MAX_OVERLAYS_PER_SCENE)
    #: ``false`` turns the corner watermark off for this scene alone: footage that shows the brand's logo itself
    #: (a sign, a wall, a screen) keeps one logo on the frame (``duplicate_logo``). Unset, the video's
    #: ``theme.watermark`` - the template's - as before; ``true`` is the same as unset.
    watermark: bool | None = None

    @model_validator(mode="after")
    def _inserts_on_footage(self) -> _SceneBase:
        if not any(overlay.type == "screen_insert" for overlay in self.overlays):
            return self
        kind = getattr(self, "type", None)
        if kind not in SCREEN_INSERT_SCENES:
            raise ValueError(
                "a screen_insert maps onto the scene's footage: only clip, recording, image and statement (over a "
                "backdrop) scenes take one"
            )
        if kind == "statement" and getattr(self, "backdrop", None) is None:
            raise ValueError("a screen_insert maps onto the scene's footage: this statement has no backdrop")
        # Several inserts left to the footage (quad auto) each take a screen of their own: the save hands them out
        # (``insert_bezels``), refusing when the footage shows fewer screens than inserts.
        return self


class TitleScene(_SceneBase):
    type: Literal["title"]
    title: str = Field(min_length=1, max_length=140)
    subtitle: str | None = Field(None, max_length=240)
    kicker: str | None = Field(None, max_length=80)
    duration: float = Field(4.0, ge=1.0, le=30.0)
    style: Literal["aurora", "minimal", "spotlight", "split"] = "aurora"
    align: Literal["center", "left"] = "center"
    show_logo: bool = True


class IdleCompression(_Strict):
    """What happens to the moments of a recording where nothing moves (page loads, waits)."""

    mode: Literal["off", "compress", "cut"] = "compress"
    #: A pause longer than this many seconds is compressed.
    threshold: float = Field(1.2, ge=0.3, le=10.0)
    #: How fast a compressed pause plays.
    speed: float = Field(6.0, ge=1.5, le=20.0)


class CameraKeyframe(_Strict):
    #: Seconds of the recording (after trim, before speed changes).
    t: float = Field(ge=0.0, le=3600.0)
    #: Centre of the view, 0-1 across and down the page.
    x: float = Field(0.5, ge=0.0, le=1.0)
    y: float = Field(0.5, ge=0.0, le=1.0)
    zoom: float = Field(1.0, ge=1.0, le=4.0)


class Camera(_Strict):
    """The virtual camera over a recording."""

    #: ``auto`` zooms toward what is clicked or typed, ``static`` never moves,
    #: ``manual`` follows ``keyframes``.
    mode: Literal["auto", "static", "manual"] = "auto"
    max_zoom: float = Field(1.8, ge=1.0, le=3.0)
    zoom_on: list[Literal["click", "type", "select", "hover", "highlight"]] = Field(
        default_factory=lambda: ["click", "type", "select", "highlight"]
    )
    #: How the automatic camera moves (``timeline.CAMERA_PACES``): ``calm`` smooth,
    #: unhurried moves; ``dynamic`` quicker moves and holds.
    pace: Literal["calm", "dynamic"] = "calm"
    #: Seconds of a zoom in, overriding the pace's; zoom outs and pans in proportion.
    move_seconds: float | None = Field(None, ge=0.5, le=3.0)
    keyframes: list[CameraKeyframe] = Field(default_factory=list, max_length=200)

    @model_validator(mode="after")
    def _keyframes_for_manual(self) -> Camera:
        if self.mode == "manual" and not self.keyframes:
            raise ValueError("a manual camera needs keyframes")
        return self


class Cursor(_Strict):
    visible: bool = True
    size: float = Field(1.35, ge=0.6, le=3.0)
    style: Literal["arrow", "dot", "halo"] = "arrow"
    click_effect: Literal["ripple", "pulse", "none"] = "ripple"


class Frame(_Strict):
    """How a recording sits on the canvas."""

    #: ``window``: a browser window (URL bar) on the branded background;
    #: ``none``: the recording fills the canvas.
    style: Literal["window", "none"] = "window"
    url_bar: bool = True
    #: Margin around the window, as a share of the canvas's width: the window then takes all the room it leaves,
    #: its page's bottom edge allowed under the subtitles (``windowBox`` in the compositor page).
    padding: float = Field(0.02, ge=0.0, le=0.2)
    radius: int = Field(18, ge=0, le=48)
    shadow: bool = True
    #: ``true``: the recording drawn edge to edge - no margin, window bar, corner or halo - for a scene whose
    #: UI must be read; the captions keep their band. A template locking the frame leaves it to the scene unless its
    #: locked frame names it (``templates.LOCK_OPEN_FIELDS``).
    fill: bool = False


class Highlight(_Strict):
    at_step: int = Field(ge=0, le=MAX_STEPS - 1)
    duration: float = Field(2.0, ge=0.3, le=20.0)


class RecordingScene(_SceneBase):
    type: Literal["recording"]
    take_id: str
    #: Seconds of the recording kept, from and to.
    trim_start: float = Field(0.0, ge=0.0, le=3600.0)
    trim_end: float | None = Field(None, ge=0.0, le=3600.0)
    speed: float = Field(1.0, ge=0.25, le=4.0)
    idle: IdleCompression = Field(default_factory=IdleCompression)
    camera: Camera = Field(default_factory=Camera)
    cursor: Cursor = Field(default_factory=Cursor)
    frame: Frame = Field(default_factory=Frame)
    highlights: list[Highlight] = Field(default_factory=list, max_length=40)
    #: Seconds the recording's last frame stays after it ends, and longer while its narration lasts.
    hold_end: float = Field(0.0, ge=0.0, le=10.0)
    #: Milliseconds of the recording kept after its last step ends: unset, all the recorder held there
    #: (``TakeScript.end_hold_ms``); a dynamic video (``VideoSpec.pace``) keeps 300.
    end_hold_ms: int | None = Field(None, ge=0, le=10000)
    #: How that time plays: ``zoom`` gives back the compressed pauses, then drifts in; ``hold``
    #: keeps the pauses as asked and freezes the frame. A recording does not loop: its cursor and address bar
    #: would jump back with it.
    extend_mode: Literal["hold", "zoom"] = "zoom"
    #: ``auto``: a recording its narration outruns, even with its pauses given back, gets the ``speed`` that
    #: lasts as long, set by the save (``recording_fit``) and never below 0.5; ``off`` plays it at its ``speed``.
    fit: Literal["auto", "off"] = "auto"

    @field_validator("take_id")
    @classmethod
    def _take_id(cls, value: str) -> str:
        return _uuid_text(value, "take_id") or value

    @model_validator(mode="after")
    def _trim(self) -> RecordingScene:
        if self.trim_end is not None and self.trim_end <= self.trim_start:
            raise ValueError("trim_end must be after trim_start")
        return self


class SlideScene(_SceneBase):
    type: Literal["slide"]
    presentation_id: str
    #: 1-based slide number, or the slide's stable id.
    slide: int | None = Field(None, ge=1, le=500)
    slide_id: str | None = Field(None, max_length=64)
    duration: float = Field(5.0, ge=1.0, le=60.0)
    motion: Literal["ken_burns", "zoom_in", "zoom_out", "pan_left", "pan_right", "none"] = "ken_burns"
    #: ``False`` draws the slide without the deck's chrome: its footer, logo, slide number and header.
    chrome: bool = True

    @field_validator("presentation_id")
    @classmethod
    def _presentation_id(cls, value: str) -> str:
        return _uuid_text(value, "presentation_id") or value

    @model_validator(mode="after")
    def _one_slide(self) -> SlideScene:
        if self.slide is None and not self.slide_id:
            self.slide = 1
        return self


#: The deck slide a figure from a deck is taken from when its address names none.
_FIGURE_SLIDE = 1
#: Seconds a figure's step stays on screen, all in, before the next one starts (``FigureScene.dwell``).
DEFAULT_FIGURE_DWELL = 0.8


def figure_block_problems(block: object) -> list[str]:
    """Why *block* is not a block of a presentation spec, as the deck's own validator words it (empty: it is).

    The block is validated as the one block of a one-slide deck
    (``presentation_spec.validate_spec``): the deck and the figure share
    one definition of a block, its fields and its bounds.
    """
    from app.services.presentation_spec import PresentationSpecError, validate_spec

    if not isinstance(block, dict) or not isinstance(block.get("type"), str):
        return ["a figure's block_spec is a block of a presentation spec, with its `type`"]
    try:
        validate_spec({"title": "Figure", "slides": [{"layout": "content", "blocks": [block]}]})
    except PresentationSpecError as exc:
        prefix = "slides.0.blocks.0"
        return [
            f"block_spec{problem.path[len(prefix) :] if problem.path.startswith(prefix) else ''}: {problem.message}"
            for problem in exc.problems
        ] or [str(exc).splitlines()[0]]
    return []


def figure_file_ids(block: object) -> set[str]:
    """Every platform file a block names (``*_file_id`` keys, at any depth): what its figure draws."""
    found: set[str] = set()
    if isinstance(block, dict):
        for key, value in block.items():
            if key.endswith("file_id") and isinstance(value, str) and _uuid_like(value):
                found.add(value)
            else:
                found |= figure_file_ids(value)
    elif isinstance(block, list):
        for item in block:
            found |= figure_file_ids(item)
    return found


def _uuid_like(value: str) -> bool:
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


class FigureScene(_SceneBase):
    """One figure of a deck - an architecture, a process flow, a timeline, a chart, a diagram, a roadmap - animated.

    The block comes from a deck (``presentation_id``, its ``slide`` or
    ``slide_id``, and ``block``: its index among the slide's blocks; a
    roadmap slide is its phases) or is written here (``block_spec``: a block
    of the presentation spec, validated by the deck's own validator). The
    render draws it on the video template's palette (``figures.py``), built
    in step by step - nodes and phases appear in order, edges draw, a
    chart's series grow - with the camera moving between the steps. Built,
    it keeps moving (``motion: drift``): the whole figure drifts in,
    a pulse runs along each edge and the core node's glow breathes.
    """

    type: Literal["figure"]
    presentation_id: str | None = None
    #: 1-based slide number, or the slide's stable id.
    slide: int | None = Field(None, ge=1, le=500)
    slide_id: str | None = Field(None, max_length=64)
    #: The block's 0-based index among the slide's blocks (a columns slide: column by column).
    block: int = Field(0, ge=0, le=60)
    #: The block itself, as a deck writes it (``{"type": "process_flow", "steps": [...]}``).
    block_spec: dict | None = None
    duration: float = Field(6.0, ge=2.0, le=60.0)
    #: ``sequence``: one step after the other; ``together``: every step at once, staggered.
    build: Literal["sequence", "together"] = "sequence"
    #: When each step comes in: spread over the scene, or when the narration names it.
    reveal: Literal["even", "narration"] = "even"
    #: ``follow`` frames each step as it comes in and ends on the whole figure; ``still`` frames the whole figure.
    camera: Literal["follow", "still"] = "follow"
    #: Seconds a step stays on screen, all in, before the next one starts: steps the scene has no room
    #: to show one by one come in together, a few at a time (``figures.reveal_times``).
    dwell: float = Field(DEFAULT_FIGURE_DWELL, ge=0.0, le=5.0)
    #: ``drift``: once built, the figure drifts in slowly, its edges carry a pulse and its core node
    #: breathes, to the end of the scene; ``none``: it holds still once built.
    motion: Literal["drift", "none"] = "drift"
    #: How the figure stands in space: ``flat`` faces the camera, ``tilt`` leans back and turns
    #: towards it as it builds, ``orbit`` turns slowly from one side to the other.
    perspective: Literal["flat", "tilt", "orbit"] = "flat"

    @field_validator("presentation_id")
    @classmethod
    def _presentation_id(cls, value: str | None) -> str | None:
        return _uuid_text(value, "presentation_id")

    @field_validator("block_spec")
    @classmethod
    def _block_spec(cls, value: dict | None) -> dict | None:
        if value is None:
            return None
        problems = figure_block_problems(value)
        if problems:
            raise ValueError("; ".join(problems[:6]))
        return value

    @model_validator(mode="after")
    def _one_source(self) -> FigureScene:
        if bool(self.presentation_id) == bool(self.block_spec):
            raise ValueError("a figure scene needs exactly one of presentation_id (a deck's block) or block_spec")
        if self.presentation_id and self.slide is None and not self.slide_id:
            self.slide = _FIGURE_SLIDE
        return self


class ClipScene(_SceneBase):
    type: Literal["clip"]
    #: An AI-generated clip (``generate_video_clip``) or an uploaded video file.
    clip_id: str | None = None
    file_id: str | None = None
    trim_start: float = Field(0.0, ge=0.0, le=3600.0)
    trim_end: float | None = Field(None, ge=0.0, le=3600.0)
    speed: float = Field(1.0, ge=0.25, le=4.0)
    fit: Literal["cover", "contain"] = "cover"
    #: The clip's own sound (an AI shot's music and effects, a film's dialogue):
    #: ``true`` keeps it, ``false`` drops it; unset, the video's ``clip_sound``
    #: decides (:func:`clip_sound_choice`).
    audio: bool | None = None
    #: Its level against the voice (both brought to the same loudness), under
    #: which it goes down while someone speaks.
    volume: float = Field(0.8, ge=0.0, le=2.0)
    #: Seconds after the clip ends, and longer while its narration lasts, played as :data:`EXTEND_MODES` says.
    hold_end: float = Field(0.0, ge=0.0, le=10.0)
    extend_mode: ExtendMode = "zoom"
    #: ``intended``: the camera's move in the clip is the author's - a dolly, a pan asked for -, never a
    #: ``camera_moved`` finding (``quality.camera_moved``).
    motion: Literal["intended"] | None = None
    #: The video's ``references`` (their ids) its AI shot must match: the save-time quality check reads the shot's
    #: first, middle and last frames against each (``reference_drift``), and ``generate_video_clip`` sends them to
    #: the model with the shot (``consistency``). Only a clip scene playing an AI shot names any.
    consistency: list[str] = Field(default_factory=list, max_length=MAX_CONSISTENCY)

    @field_validator("clip_id")
    @classmethod
    def _clip_id(cls, value: str | None) -> str | None:
        return _uuid_text(value, "clip_id")

    @field_validator("consistency")
    @classmethod
    def _consistency(cls, value: list[str]) -> list[str]:
        ids = [str(item or "").strip() for item in value]
        if any(not re.match(SCENE_ID_PATTERN, item) for item in ids):
            raise ValueError("consistency lists the ids of the video's references (letters, digits, - and _)")
        if len(set(ids)) != len(ids):
            raise ValueError("consistency names each reference once")
        return ids

    @field_validator("file_id")
    @classmethod
    def _file_id(cls, value: str | None) -> str | None:
        return _uuid_text(value, "file_id")

    @model_validator(mode="after")
    def _one_source(self) -> ClipScene:
        if bool(self.clip_id) == bool(self.file_id):
            raise ValueError("a clip scene needs exactly one of clip_id or file_id")
        if self.trim_end is not None and self.trim_end <= self.trim_start:
            raise ValueError("trim_end must be after trim_start")
        if self.consistency and not self.clip_id:
            raise ValueError("consistency holds an AI shot to the video's references: an uploaded file is not checked")
        return self


#: The most regions of a picture an AI shot keeps as they are.
MAX_KEPT_REGIONS = 4


def kept_boxes(value: Any) -> list[list[float]]:
    """Regions kept from a start picture: ``[x, y, width, height]`` boxes in its shares, checked (``ValueError``)."""
    if not isinstance(value, list) or len(value) > MAX_KEPT_REGIONS:
        raise ValueError(f"the kept regions are a list of at most {MAX_KEPT_REGIONS} boxes [x, y, width, height]")
    boxes = []
    for box in value:
        numbers = isinstance(box, list | tuple) and all(
            isinstance(v, int | float) and not isinstance(v, bool) for v in box
        )
        if not numbers or len(box) != 4:
            raise ValueError("a kept region is [x, y, width, height] in shares of the image (0 to 1)")
        boxes.append(_image_box([float(v) for v in box]))
    return boxes


class ImageAnimation(_Strict):
    """The image brought to life by the video model: an AI shot made FROM it.

    The platform starts the shot when the video is saved and the scene plays
    it once made; until then, or when no video model can make it, the scene
    is the still with its ``motion``.
    """

    #: How it moves (camera, motion); the platform's default moves the camera
    #: slowly and keeps every detail, text and logo of the image.
    prompt: str | None = Field(None, max_length=600)
    #: Regions ``[x, y, width, height]`` (shares of the image) kept as the still has them: a banner,
    #: a sign, a caption the model would redraw. The still's pixels are drawn back there in every frame of
    #: the shot, following its camera; a changed list makes a new shot.
    keep: list[list[float]] = Field(default_factory=list, max_length=MAX_KEPT_REGIONS)
    #: The shot made from the image, filled in by the platform on save.
    clip_id: str | None = None

    @field_validator("clip_id")
    @classmethod
    def _clip_id(cls, value: str | None) -> str | None:
        return _uuid_text(value, "clip_id")

    @field_validator("keep")
    @classmethod
    def _keep(cls, value: list[list[float]]) -> list[list[float]]:
        return kept_boxes(value)


def _image_box(value: list[float]) -> list[float]:
    """A region of an image: ``[x, y, width, height]`` in shares of the image (0-1), inside it."""
    x, y, width, height = value
    if not (0 <= x < 1 and 0 <= y < 1 and 0 < width <= 1 and 0 < height <= 1) or x + width > 1.001 or y + height > 1.001:
        raise ValueError("a box is [x, y, width, height] in shares of the image (0 to 1), inside it")
    return [round(float(v), 4) for v in value]


class ParallaxLayer(_Strict):
    """A region of the image lifted off it, nearer the camera by *depth*."""

    box: list[float] = Field(min_length=4, max_length=4)
    #: 0.1 barely off the image, 1 the nearest: how much it moves with the camera and how high it lifts.
    depth: float = Field(0.5, ge=0.1, le=1.0)

    @field_validator("box")
    @classmethod
    def _box(cls, value: list[float]) -> list[float]:
        return _image_box(value)


class ParallaxEffect(_Strict):
    """Panels of a screenshot lifted off it with a shadow, moving with the camera faster than the image."""

    type: Literal["parallax"]
    layers: list[ParallaxLayer] = Field(min_length=1, max_length=4)
    #: Seconds into the scene the layers lift (one after the other).
    at: float = Field(0.3, ge=0.0, le=60.0)


class LightSweepEffect(_Strict):
    """A band of light crossing the image, as over glass."""

    type: Literal["light_sweep"]
    at: float = Field(0.6, ge=0.0, le=60.0)
    duration: float = Field(1.6, ge=0.4, le=8.0)
    #: The band's slant, degrees from vertical.
    angle: float = Field(20.0, ge=-75.0, le=75.0)
    #: The band's width, as a share of the image's.
    width: float = Field(0.18, ge=0.04, le=0.6)
    strength: float = Field(0.35, ge=0.05, le=1.0)
    #: Seconds from one sweep to the next; unset, it sweeps once.
    repeat: float | None = Field(None, ge=1.0, le=30.0)


class PulseEffect(_Strict):
    """A glow pulsing on one region - a gauge, a score, an alert - in the brand's accent."""

    type: Literal["pulse"]
    box: list[float] = Field(min_length=4, max_length=4)
    shape: Literal["rect", "ellipse"] = "rect"
    at: float = Field(0.5, ge=0.0, le=60.0)
    #: Seconds of one pulse.
    period: float = Field(1.6, ge=0.5, le=6.0)
    strength: float = Field(0.6, ge=0.1, le=1.0)
    #: How many pulses; unset, it pulses to the end of the scene.
    count: int | None = Field(None, ge=1, le=20)

    @field_validator("box")
    @classmethod
    def _box(cls, value: list[float]) -> list[float]:
        return _image_box(value)


StillEffect = Annotated[ParallaxEffect | LightSweepEffect | PulseEffect, Field(discriminator="type")]
#: The most effects one image scene carries.
MAX_STILL_EFFECTS = 6


#: The fields naming a moment of a recording a picture is taken from, dropped with the recording.
_RECORDING_FRAME_FIELDS = frozenset({"at_step", "at_seconds", "frame_of"})


class RecordingFrame(_Strict):
    """A picture taken from a recording: its ``take_id`` and the moment, ``at_step`` (the frame its contact
    sheet shows after that step) or ``at_seconds`` (its last frame without either).

    The save keeps that frame as a picture once (``take_frames.resolve``): its
    ``file_id`` is written beside the recording and :attr:`frame_of` says which
    moment it holds, so the frame is extracted again only when the recording
    or the moment changes.
    """

    take_id: str | None = None
    at_step: int | None = Field(None, ge=0, le=MAX_STEPS - 1)
    at_seconds: float | None = Field(None, ge=0.0, le=3600.0)
    #: The moment the picture ``file_id`` names was taken at (``take_frames.frame_key``), written by the save.
    frame_of: str | None = Field(None, max_length=160)

    @model_validator(mode="before")
    @classmethod
    def _moment_of_a_recording(cls, data: Any) -> Any:
        # A one-of-like group: switched to a file or a shot, the recording's moment goes with it.
        if isinstance(data, dict) and not data.get("take_id"):
            return {key: value for key, value in data.items() if key not in _RECORDING_FRAME_FIELDS}
        return data

    @field_validator("take_id")
    @classmethod
    def _take_id(cls, value: str | None) -> str | None:
        return _uuid_text(value, "take_id")

    @model_validator(mode="after")
    def _one_moment(self) -> RecordingFrame:
        if self.at_step is not None and self.at_seconds is not None:
            raise ValueError("a recording's frame is taken at_step or at_seconds, not both")
        return self


#: The sources a picture of a video may name, as a refusal lists them.
PICTURE_SOURCES = "file_id (a picture), or take_id with at_step or at_seconds (a frame of a recording)"


class ImageScene(_SceneBase, RecordingFrame):
    type: Literal["image"]
    #: The picture; written by the save for a frame of a recording (:class:`RecordingFrame`).
    file_id: str | None = None
    duration: float = Field(4.0, ge=1.0, le=60.0)
    motion: Literal["ken_burns", "zoom_in", "zoom_out", "pan_left", "pan_right", "none"] = "ken_burns"
    fit: Literal["cover", "contain"] = "contain"
    animate: ImageAnimation | None = None
    #: How the scene plays once its animation (a shot shorter than the scene) has ended (:data:`EXTEND_MODES`).
    extend_mode: ExtendMode = "zoom"
    #: The still brought to life without a model: panels lifted in parallax, a light sweep, a pulse on a region.
    effects: list[StillEffect] = Field(default_factory=list, max_length=MAX_STILL_EFFECTS)

    @field_validator("file_id")
    @classmethod
    def _file_id(cls, value: str | None) -> str | None:
        return _uuid_text(value, "file_id")

    @model_validator(mode="after")
    def _one_picture(self) -> ImageScene:
        if not (self.file_id or self.take_id):
            raise ValueError(f"an image scene shows one picture: {PICTURE_SOURCES}")
        return self

    @field_validator("animate", mode="before")
    @classmethod
    def _animate(cls, value: object) -> object:
        # `true` is the short way to ask; `false` is no animation.
        if value is True:
            return {}
        return None if value is False else value

    @model_validator(mode="after")
    def _one_way_to_move(self) -> ImageScene:
        if self.animate is not None and self.effects:
            raise ValueError(
                "an image scene moves by an AI shot (animate) or by effects drawn on the real image (effects), not both"
            )
        return self


#: An accent in a statement line (``*words*``), the one pattern the compositor draws with
#: (``compositor_page.PAGE_JS``): an asterisk opens an accent before a non-space and closes one after
#: a non-space, so ``5* rated`` stays text. An opening asterisk no other closes is drawn as typed:
#: a stored spec never stops validating for it, and a save says so (:func:`unclosed_accents`).
ACCENT_PATTERN = r"\*(?=\S)(?:[^*\n]*\S)?\*"
_ACCENT = re.compile(ACCENT_PATTERN)
_UNCLOSED_ACCENT = re.compile(r"(?:^|(?<=\s))\*(\S+)")

StatementLine = Annotated[str, Field(min_length=1, max_length=90)]
SecondaryLine = Annotated[str, Field(min_length=1, max_length=160)]
Chip = Annotated[str, Field(min_length=1, max_length=32)]


class StatementBackdrop(RecordingFrame):
    """What plays behind a statement: an AI shot, an uploaded video or an image, or a frame of a recording."""

    clip_id: str | None = None
    #: An uploaded video or image (a video plays, an image moves with the scene's ``motion``).
    file_id: str | None = None
    #: Seconds of a video skipped at its start.
    trim_start: float = Field(0.0, ge=0.0, le=3600.0)

    @field_validator("clip_id")
    @classmethod
    def _clip_id(cls, value: str | None) -> str | None:
        return _uuid_text(value, "clip_id")

    @field_validator("file_id")
    @classmethod
    def _file_id(cls, value: str | None) -> str | None:
        return _uuid_text(value, "file_id")

    @model_validator(mode="after")
    def _one_source(self) -> StatementBackdrop:
        # A frame of a recording carries the file_id the save kept it as.
        if (bool(self.clip_id) + bool(self.file_id or self.take_id)) != 1:
            raise ValueError(f"a backdrop needs exactly one source: clip_id (an AI shot), {PICTURE_SOURCES}")
        return self


class StatementScene(_SceneBase):
    """A message in big type, revealed line by line: the brand film's and the event loop's voice.

    ``*words*`` in a line or a secondary line are drawn in the accent colour.
    """

    type: Literal["statement"]
    lines: list[StatementLine] = Field(min_length=1, max_length=4)
    kicker: str | None = Field(None, max_length=60)
    #: A figure drawn above the lines (``6,000+``), counted up when it is a number.
    figure: str | None = Field(None, max_length=16)
    #: Smaller lines under the statement: a proof point, a translation.
    secondary: list[SecondaryLine] = Field(default_factory=list, max_length=3)
    #: Labels drawn as pills under it (``Open source``, ``Community led``).
    chips: list[Chip] = Field(default_factory=list, max_length=6)
    reveal: Literal["lines", "words", "together"] = "lines"
    align: Literal["center", "left"] = "center"
    duration: float = Field(4.0, ge=1.0, le=30.0)
    show_logo: bool = False
    backdrop: StatementBackdrop | None = None
    #: How much a backdrop is darkened under the text (0 = untouched).
    dim: float = Field(0.55, ge=0.0, le=0.9)
    #: How an image backdrop moves.
    motion: Literal["ken_burns", "zoom_in", "zoom_out", "pan_left", "pan_right", "none"] = "zoom_in"


class OutroScene(_SceneBase):
    type: Literal["outro"]
    title: str = Field(min_length=1, max_length=140)
    subtitle: str | None = Field(None, max_length=240)
    kicker: str | None = Field(None, max_length=80)
    #: The call to action drawn as a button (``Start your free trial``).
    cta: str | None = Field(None, max_length=60)
    #: A URL or handle shown under it (``filigran.io``).
    url: str | None = Field(None, max_length=120)
    duration: float = Field(5.0, ge=1.0, le=30.0)
    style: Literal["aurora", "minimal", "spotlight", "split"] = "aurora"
    show_logo: bool = True


# ── Motion design ─────────────────────────────────────────────────
#
# A ``motion`` scene is motion graphics: a built-in composition the planner lays
# out from the brand's motion tokens (``motion_design.compose``), elements written
# here, or both. Every element is drawn by the compositor page as a pure function
# of time (``compositor_page``: the keyframe engine, Lottie, particles), so a frame
# is the same on whichever page draws it. Nothing here is HTML: text is set as
# text, paths are checked path data, colours are the theme's roles.

MAX_MOTION_ELEMENTS = 40
MAX_MOTION_KEYS = 24
MAX_CAMERA_KEYS = 12
MAX_PARTICLES = 400
MAX_SCENE_PARTICLES = 1200
MAX_PATH_CHARS = 4000
MAX_PATH_NUMBERS = 1200
#: The seconds a motion scene's own timeline reaches (its ``duration`` upper bound).
MOTION_SCENE_SECONDS = 30.0

#: The built-in compositions: what each one needs written (a field of the scene).
MOTION_COMPOSITIONS: dict[str, str | None] = {
    "logo_reveal": None,
    "kinetic_title": "title",
    "stat_counter": "figure",
    "list_build": "items",
    "split_statement": "lines",
    "quote": "title",
    "constellation": "title",
}
MotionComposition = Literal[
    "logo_reveal", "kinetic_title", "stat_counter", "list_build", "split_statement", "quote", "constellation"
]
#: The named easings; ``enter`` / ``exit`` / ``move`` / ``emphasis`` are the brand's own (``theme.motion``).
EASE_NAMES = (
    "linear",
    "in",
    "out",
    "in_out",
    "out_back",
    "out_expo",
    "in_out_expo",
    "out_quint",
    "snap",
    "spring",
    "spring_soft",
    "spring_bouncy",
    "enter",
    "exit",
    "move",
    "emphasis",
)
_BEZIER_RE = re.compile(r"^cubic-bezier\(\s*([-+0-9.eE]+)\s*,\s*([-+0-9.eE]+)\s*,\s*([-+0-9.eE]+)\s*,\s*([-+0-9.eE]+)\s*\)$")
_SPRING_RE = re.compile(r"^spring\(\s*([0-9.eE+]+)\s*,\s*([0-9.eE+]+)\s*\)$")
_PATH_RE = re.compile(r"^[MmLlHhVvCcSsQqTtAaZz0-9eE.,+\-\s]+$")
_PATH_NUMBER_RE = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")
ENTER_PRESETS = (
    "fade",
    "rise",
    "drop",
    "slide_left",
    "slide_right",
    "scale",
    "pop",
    "blur",
    "track",
    "mask_up",
    "mask_down",
    "mask_left",
    "mask_right",
    "iris",
    "draw",
    "flip",
    "typewriter",
    "none",
)
EXIT_PRESETS = (
    "fade",
    "rise",
    "drop",
    "slide_left",
    "slide_right",
    "scale",
    "blur",
    "mask_up",
    "mask_down",
    "mask_left",
    "mask_right",
    "iris",
    "none",
)
LOOP_PRESETS = ("float", "pulse", "breathe", "spin", "sway", "shimmer")
#: The theme colours an element is painted in: the brand's roles, never a colour of its own.
COLOR_ROLES = (
    "foreground",
    "muted",
    "accent",
    "accent_2",
    "gradient",
    "surface",
    "background",
    "on_accent",
    "white",
    "black",
)
ColorRole = Literal[
    "foreground", "muted", "accent", "accent_2", "gradient", "surface", "background", "on_accent", "white", "black"
]
FillRole = Literal[
    "foreground",
    "muted",
    "accent",
    "accent_2",
    "gradient",
    "surface",
    "background",
    "on_accent",
    "white",
    "black",
    "none",
]
ANCHORS = ("center", "top_left", "top", "top_right", "left", "right", "bottom_left", "bottom", "bottom_right")
MOTION_ELEMENT_KINDS = ("text", "shape", "path", "image", "lottie", "particles")
#: The fields only one kind of element takes: an element of another kind that sets one is refused, naming it.
_KIND_FIELDS: dict[str, frozenset[str]] = {
    "text": frozenset(
        {
            "text",
            "role",
            "size",
            "weight",
            "color",
            "font",
            "align",
            "split",
            "tracking",
            "uppercase",
            "max_width",
            "count",
            "marker",
        }
    ),
    "shape": frozenset({"shape", "fill", "stroke", "stroke_width", "radius", "sides"}),
    "path": frozenset({"d", "view_box", "morph_to", "fill", "stroke", "stroke_width"}),
    "image": frozenset({"file_id", "asset", "fit"}),
    "lottie": frozenset({"file_id", "speed", "loop_playback", "fit"}),
    "particles": frozenset({"number", "seed", "particle", "size", "drift", "twinkle", "connect", "color"}),
}
DEFAULT_PARTICLES = 60
_SHARED_FIELDS = frozenset(_KIND_FIELDS["text"] | _KIND_FIELDS["shape"] | _KIND_FIELDS["path"]) | frozenset(
    _KIND_FIELDS["image"] | _KIND_FIELDS["lottie"] | _KIND_FIELDS["particles"]
)


def _ease_text(value: str | None) -> str | None:
    """An easing as the compositor reads it: a name, ``cubic-bezier(x1, y1, x2, y2)`` or ``spring(stiffness, damping)``."""
    if value is None:
        return None
    text = " ".join(str(value).split()).lower()
    if text in EASE_NAMES:
        return text
    bezier = _BEZIER_RE.match(text)
    if bezier:
        x1, y1, x2, y2 = (float(n) for n in bezier.groups())
        if not (0.0 <= x1 <= 1.0 and 0.0 <= x2 <= 1.0) or not (-2.0 <= y1 <= 3.0 and -2.0 <= y2 <= 3.0):
            raise ValueError("a cubic-bezier keeps x1 and x2 within 0-1 and y1, y2 within -2 to 3")
        return f"cubic-bezier({x1:g}, {y1:g}, {x2:g}, {y2:g})"
    spring = _SPRING_RE.match(text)
    if spring:
        stiffness, damping = (float(n) for n in spring.groups())
        if not (10.0 <= stiffness <= 1000.0 and 1.0 <= damping <= 100.0):
            raise ValueError("a spring is spring(stiffness 10-1000, damping 1-100)")
        return f"spring({stiffness:g}, {damping:g})"
    raise ValueError(
        f"an easing is one of {', '.join(EASE_NAMES)}, cubic-bezier(x1, y1, x2, y2) or spring(stiffness, damping)"
    )


def path_data(value: str | None) -> str | None:
    """SVG path data the compositor may set as a path's ``d``: path commands and numbers only, bounded."""
    if value is None:
        return None
    text = " ".join(str(value).split())
    if not text:
        return None
    if len(text) > MAX_PATH_CHARS:
        raise ValueError(f"path data holds {MAX_PATH_CHARS} characters at most")
    if not _PATH_RE.match(text) or text[0] not in "Mm":
        raise ValueError("path data is SVG path commands (M, L, H, V, C, S, Q, T, A, Z) and numbers, starting with M")
    if len(_PATH_NUMBER_RE.findall(text)) > MAX_PATH_NUMBERS:
        raise ValueError(f"path data holds {MAX_PATH_NUMBERS} numbers at most")
    return text


Ease = Annotated[str, Field(max_length=60)]


class MotionKey(_Strict):
    """One keyframe of an element: the values it reaches at ``t`` (seconds into the scene), eased in by ``ease``."""

    t: float = Field(ge=0.0, le=MOTION_SCENE_SECONDS)
    #: Where it stands, in shares of the frame (0-1; past the edges to come in from outside).
    x: float | None = Field(None, ge=-1.0, le=2.0)
    y: float | None = Field(None, ge=-1.0, le=2.0)
    scale: float | None = Field(None, ge=0.0, le=10.0)
    rotate: float | None = Field(None, ge=-3600.0, le=3600.0)
    #: Turned in 3D, in degrees, around the horizontal and the vertical axes.
    rotate_x: float | None = Field(None, ge=-180.0, le=180.0)
    rotate_y: float | None = Field(None, ge=-180.0, le=180.0)
    #: Towards the camera (positive) or away, in design pixels, under the scene's 3D camera.
    z: float | None = Field(None, ge=-3000.0, le=3000.0)
    opacity: float | None = Field(None, ge=0.0, le=1.0)
    blur: float | None = Field(None, ge=0.0, le=120.0)
    #: How much of the element its mask shows (0-1), from the side ``mask.side`` names.
    reveal: float | None = Field(None, ge=0.0, le=1.0)
    #: How much of a path or a shape's outline is drawn (0-1).
    draw: float | None = Field(None, ge=0.0, le=1.0)
    #: How far a path has turned into its ``morph_to`` (0-1).
    morph: float | None = Field(None, ge=0.0, le=1.0)
    #: A counted text's count (0-1 of its number), a Lottie's playhead (0-1 of its animation).
    progress: float | None = Field(None, ge=0.0, le=1.0)
    ease: Ease | None = None

    @field_validator("ease")
    @classmethod
    def _ease(cls, value: str | None) -> str | None:
        return _ease_text(value)


class MotionEnter(_Strict):
    """How an element comes in: a preset drawn with the brand's motion tokens, from ``at`` seconds into the scene."""

    preset: Literal[
        "fade",
        "rise",
        "drop",
        "slide_left",
        "slide_right",
        "scale",
        "pop",
        "blur",
        "track",
        "mask_up",
        "mask_down",
        "mask_left",
        "mask_right",
        "iris",
        "draw",
        "flip",
        "typewriter",
        "none",
    ] = "rise"
    at: float = Field(0.0, ge=0.0, le=MOTION_SCENE_SECONDS)
    #: Seconds; the brand's enter duration otherwise.
    duration: float | None = Field(None, ge=0.05, le=10.0)
    ease: Ease | None = None
    #: Seconds between the parts of a split text (letters, words, lines); the brand's stagger otherwise.
    stagger: float | None = Field(None, ge=0.0, le=1.0)
    #: Which part of a split text comes in first.
    order: Literal["start", "center", "end", "random"] = "start"

    @field_validator("ease")
    @classmethod
    def _ease(cls, value: str | None) -> str | None:
        return _ease_text(value)


class MotionExit(_Strict):
    """How an element leaves, ``at`` seconds into the scene (the scene's end otherwise, as its transition starts)."""

    preset: Literal[
        "fade",
        "rise",
        "drop",
        "slide_left",
        "slide_right",
        "scale",
        "blur",
        "mask_up",
        "mask_down",
        "mask_left",
        "mask_right",
        "iris",
        "none",
    ] = "fade"
    at: float | None = Field(None, ge=0.0, le=MOTION_SCENE_SECONDS)
    duration: float | None = Field(None, ge=0.05, le=10.0)
    ease: Ease | None = None

    @field_validator("ease")
    @classmethod
    def _ease(cls, value: str | None) -> str | None:
        return _ease_text(value)


class MotionLoop(_Strict):
    """What an element does once in, until it leaves: float, pulse, breathe, spin, sway, or a light shimmer across it."""

    preset: Literal["float", "pulse", "breathe", "spin", "sway", "shimmer"]
    #: Seconds a cycle lasts.
    period: float = Field(3.0, ge=0.2, le=30.0)
    #: How strong it is, 0-1.
    amount: float = Field(0.5, ge=0.0, le=1.0)
    #: Seconds into the scene it starts; once the element is in otherwise.
    at: float | None = Field(None, ge=0.0, le=MOTION_SCENE_SECONDS)


class MotionPath(_Strict):
    """The element moves along a path, from ``start`` to ``end`` seconds into the scene.

    ``d`` is SVG path data in frame units: 0-1000 across the frame and 0-1000 down it, whatever its format.
    """

    d: str
    start: float = Field(0.0, ge=0.0, le=MOTION_SCENE_SECONDS)
    end: float = Field(2.0, ge=0.0, le=MOTION_SCENE_SECONDS)
    ease: Ease | None = None
    #: Turned along the path's direction.
    orient: bool = False

    @field_validator("d")
    @classmethod
    def _d(cls, value: str) -> str:
        data = path_data(value)
        if data is None:
            raise ValueError("a motion path needs its path data (d)")
        return data

    @field_validator("ease")
    @classmethod
    def _ease(cls, value: str | None) -> str | None:
        return _ease_text(value)

    @model_validator(mode="after")
    def _forward(self) -> MotionPath:
        if self.end <= self.start:
            raise ValueError("a motion path ends after it starts")
        return self


class MotionMask(_Strict):
    """What a ``reveal`` keyframe (or a mask preset) uncovers: a rectangle from one side, or a circle from the centre."""

    shape: Literal["rect", "circle"] = "rect"
    side: Literal["left", "right", "top", "bottom", "center"] = "left"
    #: Soft edge, in shares of the element (0 = sharp).
    feather: float = Field(0.0, ge=0.0, le=0.5)


class MotionElement(_Strict):
    """One thing a motion scene draws: text, a shape, a path, a picture, a Lottie animation or particles.

    Placed at ``x`` / ``y`` (shares of the frame) by its ``anchor``; sized by
    ``width`` / ``height`` (shares of the frame's width and height) where it
    has a box. ``keys`` move it; ``enter`` / ``exit`` / ``loop`` are presets
    drawn with the brand's motion tokens, laid over the keyframes.
    """

    id: str | None = Field(None, pattern=SCENE_ID_PATTERN)
    kind: Literal["text", "shape", "path", "image", "lottie", "particles"]
    x: float = Field(0.5, ge=-1.0, le=2.0)
    y: float = Field(0.5, ge=-1.0, le=2.0)
    anchor: Literal["center", "top_left", "top", "top_right", "left", "right", "bottom_left", "bottom", "bottom_right"] = (
        "center"
    )
    width: float | None = Field(None, gt=0.0, le=3.0)
    height: float | None = Field(None, gt=0.0, le=3.0)
    #: Stands under an element listed before it (its id), ``gap`` below its bottom as the page lays it out: a title
    #: that wraps onto two lines pushes what stands under it down. ``y`` is then not read.
    below: str | None = Field(None, pattern=SCENE_ID_PATTERN)
    #: How far under ``below``, a share of the frame's height.
    gap: float = Field(0.03, ge=0.0, le=1.0)
    #: Towards the camera (positive) or away, in design pixels, under the scene's 3D camera.
    z: float = Field(0.0, ge=-3000.0, le=3000.0)
    #: Seconds into the scene it is on screen from (its enter starts there unless it says otherwise) and until.
    start: float = Field(0.0, ge=0.0, le=MOTION_SCENE_SECONDS)
    end: float | None = Field(None, ge=0.0, le=MOTION_SCENE_SECONDS)
    opacity: float = Field(1.0, ge=0.0, le=1.0)
    scale: float = Field(1.0, ge=0.0, le=10.0)
    rotate: float = Field(0.0, ge=-3600.0, le=3600.0)
    blur: float = Field(0.0, ge=0.0, le=120.0)
    enter: MotionEnter | None = None
    exit: MotionExit | None = None
    loop: MotionLoop | None = None
    keys: list[MotionKey] = Field(default_factory=list, max_length=MAX_MOTION_KEYS)
    path: MotionPath | None = None
    mask: MotionMask | None = None
    # text
    text: str | None = Field(None, min_length=1, max_length=240)
    #: The type it is set in: its size, weight and colour unless they are given.
    role: Literal["headline", "title", "body", "kicker", "label", "figure"] | None = None
    #: A text's type size, in design pixels (8-400); a particle's, 1-40.
    size: float | None = Field(None, ge=1.0, le=400.0)
    weight: int | None = Field(None, ge=100, le=900)
    color: ColorRole | None = None
    font: Literal["heading", "body"] | None = None
    align: Literal["left", "center", "right"] | None = None
    #: Each part comes in on its own, staggered: its lines, words or letters.
    split: Literal["none", "lines", "words", "letters"] | None = None
    #: Letter spacing, in em.
    tracking: float | None = Field(None, ge=-0.2, le=1.0)
    uppercase: bool | None = None
    #: The widest it may be set, a share of the frame's width: a longer text is set smaller.
    max_width: float | None = Field(None, gt=0.0, le=1.0)
    #: The number in its text counted up from zero as it comes in (``progress`` keyframes drive it otherwise).
    count: bool | None = None
    #: A mark drawn before the text, coming in with it: a list item's.
    marker: Literal["none", "dot", "check", "dash", "number"] | None = None
    # shape
    shape: Literal["rect", "ellipse", "line", "ring", "polygon", "star"] | None = None
    fill: FillRole | None = None
    stroke: FillRole | None = None
    stroke_width: float | None = Field(None, ge=0.0, le=60.0)
    radius: float | None = Field(None, ge=0.0, le=1000.0)
    sides: int | None = Field(None, ge=3, le=12)
    # path
    d: str | None = None
    #: The path's own coordinates: ``[width, height]`` drawn into the element's box.
    view_box: tuple[Annotated[float, Field(gt=0.0, le=10000.0)], Annotated[float, Field(gt=0.0, le=10000.0)]] | None = (
        None
    )
    morph_to: str | None = None
    # image / lottie
    file_id: str | None = None
    asset: Literal["logo", "logo_light", "logo_mark"] | None = None
    fit: Literal["contain", "cover"] | None = None
    speed: float | None = Field(None, ge=0.1, le=4.0)
    #: A Lottie that ends before the scene plays again from its start (``false``: it holds its last frame).
    loop_playback: bool | None = None
    # particles
    #: How many particles it draws.
    number: int | None = Field(None, ge=1, le=MAX_PARTICLES)
    seed: int | None = Field(None, ge=0, le=1_000_000)
    particle: Literal["dot", "square", "plus", "ring"] | None = None
    drift: float | None = Field(None, ge=0.0, le=3.0)
    twinkle: float | None = Field(None, ge=0.0, le=1.0)
    #: Lines drawn between particles closer than this share of the frame (0: none): a constellation.
    connect: float | None = Field(None, ge=0.0, le=0.4)

    @field_validator("d", "morph_to")
    @classmethod
    def _path(cls, value: str | None) -> str | None:
        return path_data(value)

    @field_validator("file_id")
    @classmethod
    def _file(cls, value: str | None) -> str | None:
        return _uuid_text(value, "file_id")

    @model_validator(mode="after")
    def _kind_fields(self) -> MotionElement:
        own = _KIND_FIELDS[self.kind]
        stray = sorted(name for name in self.model_fields_set if name in _SHARED_FIELDS and name not in own)
        if stray:
            verb = "is not a field" if len(stray) == 1 else "are not fields"
            raise ValueError(f"{', '.join(stray)} {verb} of a {self.kind} element")
        if self.kind == "text" and not self.text:
            raise ValueError("a text element needs its text")
        if self.size is not None and self.kind == "text" and self.size < 8.0:
            raise ValueError("a text is set 8 design pixels or larger (size)")
        if self.size is not None and self.kind == "particles" and self.size > 40.0:
            raise ValueError("a particle is 40 design pixels at most (size)")
        if self.kind == "path" and not self.d:
            raise ValueError("a path element needs its path data (d)")
        if self.kind == "image" and bool(self.file_id) == bool(self.asset):
            raise ValueError("an image element shows one picture: file_id, or asset (logo | logo_light | logo_mark)")
        if self.kind == "lottie" and not self.file_id:
            raise ValueError("a lottie element needs its file_id (a Lottie JSON file)")
        if self.morph_to and not self.d:
            raise ValueError("morph_to turns a path into another: give the path its d")
        if self.end is not None and self.end <= self.start:
            raise ValueError("an element ends after it starts")
        times = [key.t for key in self.keys]
        if times != sorted(times):
            raise ValueError("keys are listed in time order (t)")
        if self.count and self.text and not _COUNTED.search(self.text):
            raise ValueError("count counts the number in its text: the text holds no number")
        return self

    @property
    def particle_count(self) -> int:
        """How many particles a particles element draws (:data:`DEFAULT_PARTICLES` unless it says)."""
        return int(self.number or DEFAULT_PARTICLES) if self.kind == "particles" else 0


#: The number a counted text counts up: its first run of digits (``6,000+``, ``98%``, ``$1.2M``).
_COUNTED = re.compile(r"\d[\d,]*(?:\.\d+)?")


class MotionCameraKey(_Strict):
    """Where the 3D camera of a motion scene looks at ``t`` seconds into it."""

    t: float = Field(ge=0.0, le=MOTION_SCENE_SECONDS)
    #: The point it centres, in shares of the frame.
    x: float = Field(0.5, ge=-1.0, le=2.0)
    y: float = Field(0.5, ge=-1.0, le=2.0)
    zoom: float = Field(1.0, ge=0.3, le=6.0)
    rotate_x: float = Field(0.0, ge=-75.0, le=75.0)
    rotate_y: float = Field(0.0, ge=-75.0, le=75.0)
    rotate: float = Field(0.0, ge=-180.0, le=180.0)
    ease: Ease | None = None

    @field_validator("ease")
    @classmethod
    def _ease(cls, value: str | None) -> str | None:
        return _ease_text(value)


class MotionCamera(_Strict):
    """The 3D camera a motion scene's elements are seen through: their depth (``z``) and turns read in perspective."""

    #: Distance to the picture plane, in design pixels: smaller is a wider lens, a stronger perspective.
    perspective: float = Field(1400.0, ge=300.0, le=8000.0)
    keys: list[MotionCameraKey] = Field(default_factory=list, max_length=MAX_CAMERA_KEYS)

    @model_validator(mode="after")
    def _ordered(self) -> MotionCamera:
        times = [key.t for key in self.keys]
        if times != sorted(times):
            raise ValueError("camera keys are listed in time order (t)")
        return self


MotionItem = Annotated[str, Field(min_length=1, max_length=80)]


class MotionScene(_SceneBase):
    """Motion graphics on the brand: a built-in composition, elements of its own, or both.

    ``composition`` lays out a whole scene from the video's motion tokens
    (``theme.motion``) and the texts given here - ``kinetic_title`` (title),
    ``logo_reveal`` (the template's logo, title as its tagline),
    ``stat_counter`` (figure, label), ``list_build`` (items),
    ``split_statement`` (lines), ``quote`` (title as the quote, subtitle as who
    said it), ``constellation`` (title over connected particles). ``elements``
    are drawn over it, in order (the last on top).
    """

    type: Literal["motion"]
    composition: MotionComposition | None = None
    title: str | None = Field(None, min_length=1, max_length=140)
    subtitle: str | None = Field(None, max_length=240)
    kicker: str | None = Field(None, max_length=80)
    lines: list[StatementLine] = Field(default_factory=list, max_length=4)
    items: list[MotionItem] = Field(default_factory=list, max_length=6)
    #: The number a ``stat_counter`` counts up (``6,000+``, ``98%``).
    figure: str | None = Field(None, max_length=16)
    #: What the figure counts.
    label: str | None = Field(None, max_length=120)
    show_logo: bool = False
    align: Literal["center", "left"] = "center"
    #: ``theme``: the video's backdrop (``theme.background``); ``solid``: its background colour alone.
    background: Literal["theme", "solid"] = "theme"
    elements: list[MotionElement] = Field(default_factory=list, max_length=MAX_MOTION_ELEMENTS)
    camera: MotionCamera | None = None
    duration: float = Field(5.0, ge=1.0, le=MOTION_SCENE_SECONDS)

    @model_validator(mode="after")
    def _something_to_draw(self) -> MotionScene:
        if self.composition is None and not self.elements:
            raise ValueError(
                f"a motion scene draws a composition ({', '.join(MOTION_COMPOSITIONS)}), elements, or both"
            )
        needs = MOTION_COMPOSITIONS.get(self.composition or "")
        if needs and not getattr(self, needs):
            raise ValueError(f"the {self.composition} composition needs its {needs}")
        if self.composition == "split_statement" and len(self.lines) < 2:
            raise ValueError("the split_statement composition sets two lines or more against each other")
        if self.composition == "stat_counter" and self.figure and not _COUNTED.search(self.figure):
            raise ValueError("the stat_counter composition counts a number: its figure holds none")
        if sum(element.particle_count for element in self.elements) > MAX_SCENE_PARTICLES:
            raise ValueError(f"a motion scene draws {MAX_SCENE_PARTICLES} particles at most")
        used: set[str] = set()
        for index, element in enumerate(self.elements):
            if element.id is None:
                element.id = f"{element.kind}-{index + 1}"
            if element.id in used:
                raise ValueError(f"element id {element.id!r} is used twice")
            # Under a composition's own element (its title, its kicker) when the scene draws a composition.
            if element.below is not None and element.below not in used and self.composition is None:
                raise ValueError(f"element {element.id!r} stands below {element.below!r}: no element listed before it")
            used.add(element.id)
        return self


class MotionStyle(_Strict):
    """``theme.motion``: how the brand moves - a personality, resolved into the tokens every scene is drawn with.

    ``precise`` is the platform's own motion; ``calm`` slower and softer;
    ``energetic`` snappy, with springs and letters; ``cinematic`` long blurred
    reveals and light sweeps; ``playful`` bouncy springs. ``speed`` scales
    every duration (2 is twice as fast); ``title`` overrides how card titles
    split. ``beat_sync`` lands the cuts (``cuts``), and also the builds
    (``all``), on the music's beats; ``motion_blur`` blurs what moves over a
    film shutter at the high and ultra qualities.
    """

    personality: Literal["precise", "calm", "energetic", "cinematic", "playful"] = "precise"
    speed: float | None = Field(None, ge=0.5, le=2.0)
    title: Literal["lines", "words", "letters"] | None = None
    #: How a card's logo comes in, over the personality's: rising, fading, scaled up on a spring, uncovered from its left.
    logo: Literal["rise", "fade", "scale", "mask"] | None = None
    #: A light crossing the gradient words of a card once they are in (``sweep``), over the personality's.
    accent: Literal["none", "sweep"] | None = None
    beat_sync: Literal["off", "cuts", "all"] = "off"
    motion_blur: Literal["off", "on"] = "off"


Scene = Annotated[
    TitleScene
    | StatementScene
    | RecordingScene
    | SlideScene
    | FigureScene
    | ClipScene
    | ImageScene
    | OutroScene
    | MotionScene,
    Field(discriminator="type"),
]

#: The fields of one object that name the same thing: a model takes one of them
#: at most (a clip scene's or a backdrop's source, the music's source, a slide by
#: number or by id). An edit that sets one of them drops the others of its object
#: (``projects.apply_operations``), so switching a backdrop from a file to an AI
#: shot is one field rather than a refused save.
ONE_OF_FIELDS: tuple[frozenset[str], ...] = (
    frozenset({"clip_id", "file_id"}),
    frozenset({"track", "music_id", "file_id"}),
    frozenset({"slide", "slide_id"}),
    frozenset({"presentation_id", "block_spec"}),
    frozenset({"file_id", "asset"}),
    frozenset({"file_id", "take_id", "clip_id", "asset"}),
    # A recording's frame is taken at one moment.
    frozenset({"at_step", "at_seconds"}),
    # A screen insert shows one source, or a wall's.
    frozenset({"source", "sources"}),
)


class VideoFormat(_Strict):
    aspect_ratio: Literal["16:9", "9:16", "1:1", "4:5"] = "16:9"
    #: Frames per second; the quality preset's own otherwise.
    fps: Literal[24, 25, 30, 50, 60] | None = None


class Theme(_Strict):
    """The colour scheme and backdrop; the palette and logo come from the template unless set here."""

    mode: Literal["dark", "light"] = "dark"
    #: ``mosaic`` draws seeded tiles in the template's six series colours.
    background: Literal["aurora", "gradient", "mesh", "grid", "mosaic", "solid"] = "aurora"
    accent: str | None = None
    accent_2: str | None = None
    background_color: str | None = None
    text_color: str | None = None
    #: A logo drawn on title / outro scenes and as the watermark.
    logo_file_id: str | None = None
    watermark: Literal["none", "top_left", "top_right", "bottom_left", "bottom_right"] = "none"
    #: How the brand moves; the platform's own ``precise`` motion when unset.
    motion: MotionStyle | None = None

    @field_validator("accent", "accent_2", "background_color", "text_color")
    @classmethod
    def _colors(cls, value: str | None) -> str | None:
        return _hex_color(value)

    @field_validator("logo_file_id")
    @classmethod
    def _logo(cls, value: str | None) -> str | None:
        return _uuid_text(value, "logo_file_id")


class Pronunciation(_Strict):
    """How the voice says a word it would guess wrong: a product name, an acronym.

    The same shape as an entry of the platform's pronunciation lexicon
    (``lexicon.py``), which the video's own entries override for the same
    written form.
    """

    #: The word as written in the narration.
    text: str = Field(min_length=1, max_length=80)
    #: What the voice reads instead: letters one by one as capitals joined by hyphens ("Open C-T-I", "X-T-M One").
    say: str = Field(min_length=1, max_length=160)
    #: Its reading in the International Phonetic Alphabet, for the speech models that take one.
    ipa: str | None = Field(None, max_length=200)
    #: ``exact`` the whole token, case and all; ``word`` the whole word, case and all; ``case_insensitive`` any case.
    match: Literal["exact", "word", "case_insensitive"] = "word"
    #: What reads it: the voice; the captions (which keep the written form otherwise); both.
    apply_to: Literal["voice", "captions", "both"] = "voice"

    @field_validator("text", "say")
    @classmethod
    def _words(cls, value: str) -> str:
        value = " ".join(value.split())
        if not value:
            raise ValueError("must not be blank")
        return value

    @field_validator("ipa")
    @classmethod
    def _ipa(cls, value: str | None) -> str | None:
        value = (value or "").strip().strip("/[]").strip()
        return value or None


class Voiceover(_Strict):
    """The narration of every scene, spoken by a speech model.

    The platform's speech model (Settings > AI and models > Media > Speech generation)
    unless the video names its own: a configured provider (``provider_id``)
    and model, as ``video_studio_capabilities`` lists them.
    """

    enabled: bool = True
    #: A configured speech provider, as ``video_studio_capabilities`` lists it.
    provider_id: str | None = None
    #: Its speech model; the provider's own default when unset.
    model: str | None = Field(None, max_length=100)
    #: Earlier specs named a provider type ("openai", "gemini"); "auto" is
    #: the platform's choice.
    provider: str = Field("auto", max_length=40)
    #: A voice of the speech model, by id or name, as
    #: ``video_studio_capabilities`` lists them (ElevenLabs voice ids,
    #: Gemini Kore or a designed voice_..., OpenAI marin...).
    voice: str | None = Field(None, max_length=200)
    #: How the voice speaks ("Warm, confident product launch tone, energetic
    #: but never rushed"), for the models that take a delivery instruction;
    #: ``energetic`` or ``calm`` names a preset (``voice.STYLE_PRESETS``).
    style: str | None = Field(None, max_length=500)
    speed: float = Field(1.0, ge=0.7, le=1.3)
    volume: float = Field(1.0, ge=0.0, le=2.0)
    #: Words the voice says differently from how they are written; the
    #: captions keep the written words.
    pronunciations: list[Pronunciation] = Field(default_factory=list, max_length=60)

    @field_validator("provider_id", mode="before")
    @classmethod
    def _provider(cls, value: str | None) -> str | None:
        return _uuid_text(value, "provider_id")


class Music(_Strict):
    """The bed under the whole video: a built-in track, an AI track or an uploaded sound.

    One source: ``track`` (a built-in track, composed for this video: its
    length, its scene changes), ``music_id`` (an AI track, ``generate_music``)
    or ``file_id`` (an upload). ``prompt`` asks a music model for a track
    fitted to the video when it is saved; the track's id is then written as
    ``music_id``. Every bed is brought to the voice's loudness before the mix,
    so ``volume`` is its level against the voice, whatever the file.
    """

    #: ``false``: no music, even the template's.
    enabled: bool = True
    track: str | None = Field(None, max_length=60)
    music_id: str | None = None
    file_id: str | None = None
    #: What the AI track should sound like: genre, mood, instruments, tempo, how it evolves.
    prompt: str | None = Field(None, max_length=2000)
    #: The music model and its provider for *prompt*, as ``video_studio_capabilities`` lists them.
    model: str | None = Field(None, max_length=120)
    provider_id: str | None = None
    #: No vocals in the AI track (a bed under a narration).
    instrumental: bool = True
    #: Its level where no one speaks: 1 is as loud as the voice.
    volume: float = Field(0.5, ge=0.0, le=1.0)
    #: Lower it under the narration (and under the clips that keep their sound).
    ducking: bool = True
    #: How far it goes down there, in dB.
    duck_db: float = Field(10.0, ge=3.0, le=30.0)
    #: Seconds into the track the bed starts at (skips an intro).
    start: float = Field(0.0, ge=0.0, le=600.0)
    #: Repeat a track shorter than the video, crossfaded.
    loop: bool = True
    #: ``natural``: the track's own end lands on the video's end; ``fade``: it fades out there.
    ending: Literal["fade", "natural"] = "fade"
    fade_in: float = Field(1.0, ge=0.0, le=10.0)
    fade_out: float = Field(2.5, ge=0.0, le=15.0)

    @field_validator("music_id", "file_id", "provider_id")
    @classmethod
    def _ids(cls, value: str | None, info) -> str | None:
        return _uuid_text(value, info.field_name)

    @field_validator("track")
    @classmethod
    def _track(cls, value: str | None) -> str | None:
        if value is None:
            return None
        from app.services.video_studio.score import get_style, track_ids

        style = get_style(value)
        if style is None:
            raise ValueError(f"no built-in track {value!r} (one of {', '.join(track_ids())})")
        return style.id

    @model_validator(mode="after")
    def _one_source(self) -> Music:
        # None at all is valid: levels for the track a template brings
        # (``projects.save_spec`` says when nothing plays).
        sources = [name for name in ("track", "music_id", "file_id") if getattr(self, name)]
        if len(sources) > 1:
            raise ValueError(
                f"music plays one source: {' or '.join(sources)}, not both (set the one to drop to null in the same edit)"
            )
        return self

    @property
    def source(self) -> tuple[str, str] | None:
        """``(kind, id)`` of what plays: ``track`` / ``music`` / ``file``; ``None`` for none yet."""
        if not self.enabled:
            return None
        if self.track:
            return "track", self.track
        if self.music_id:
            return "music", self.music_id
        if self.file_id:
            return "file", self.file_id
        return None

    @property
    def plays(self) -> bool:
        """A bed runs under the video: a source, or a track asked for that the save composes."""
        return self.source is not None or (self.enabled and bool(self.prompt))


class Captions(_Strict):
    enabled: bool = True
    #: Drawn on the video; a WebVTT file is produced either way.
    burn_in: bool = True
    style: Literal["pill", "bar", "outline"] = "pill"
    position: Literal["bottom", "top"] = "bottom"


#: The longest video title a spec takes (the column holds more).
TITLE_MAX_LENGTH = 200


class TemplateCard(_Strict):
    """What a spec sets on a card its template adds: the card itself is not in the spec."""

    #: Its length; at least what its words take to read once they are in (the ``reading`` finding refuses a shorter
    #: one): every save's quality check states each card's minimum (``quality.card_minimums``).
    duration: float | None = Field(None, ge=1.0, le=30.0)
    #: The small line above the card's title, in place of the template's; ``null``: no kicker at all.
    kicker: str | None = Field(None, max_length=80)
    #: Where the opening card sets its title, in place of the template's; the closing card is always centred.
    align: Literal["center", "left"] | None = None

    @model_serializer(mode="wrap")
    def _keep_no_kicker(self, handler: Any) -> Any:
        # ``null`` is a choice, not an absence: it survives the dumps that drop unset and None values.
        data = handler(self)
        if isinstance(data, dict) and "kicker" in self.model_fields_set and self.kicker is None:
            data["kicker"] = None
        return data


class TemplateScenes(_Strict):
    """``template_scenes``: the opening (``intro``) and closing (``outro``) cards the video's template adds."""

    intro: TemplateCard | None = None
    outro: TemplateCard | None = None

    @model_validator(mode="after")
    def _outro_centred(self) -> TemplateScenes:
        if self.outro is not None and self.outro.align is not None:
            raise ValueError("outro.align: the closing card is always centred - align sets the opening card (intro)")
        return self


#: What a video reference is a picture of: a recurring prop or character its AI shots must keep.
REFERENCE_KINDS = ("prop", "character")


class VideoReference(_Strict):
    """A recurring prop or character of the video, as a picture: what its AI shots are held to.

    A crop of the prop from the scene that got it right (``save_video_frame``
    with a region), or the character's portrait. A clip scene names it in
    ``consistency``; the save-time quality check reads the shot's first,
    middle and last frames against it (``reference_drift``), and
    ``generate_video_clip`` sends it to the model with the shot.
    """

    id: str = Field(pattern=SCENE_ID_PATTERN)
    file_id: str
    #: What to call it in a finding and in a prompt: ``the branded rug``.
    label: str = Field(min_length=1, max_length=80)
    kind: Literal["prop", "character"] = "prop"
    #: What of it must stay the same, in a few words, for a model that takes no reference image beside a start
    #: frame: ``navy quilted rug, teal piping, white emblem`` - the prompt then says "the <label> exactly as in the
    #: start picture: <description>".
    description: str | None = Field(None, max_length=240)

    @field_validator("file_id")
    @classmethod
    def _file_id(cls, value: str) -> str:
        return _uuid_text(value, "file_id") or ""


class VideoSpec(_Strict):
    """The edit: everything a render needs, by reference."""

    title: str = Field(min_length=1, max_length=TITLE_MAX_LENGTH)
    #: The video template (or a presentation template, for its palette and
    #: logo) the video wears; omitted, the platform's default video template.
    template_id: str | None = None
    #: No template at all, not even the default: every choice is the spec's.
    freestyle: bool = False
    #: How the video moves (``pace.DYNAMIC``): ``dynamic`` presets, wherever the spec leaves them unset, quick
    #: recordings (their pauses cut, a dynamic camera, 300 ms kept after their last step, sped up to their narration
    #: by ``fit: auto``), dynamic transitions and statements revealed word by word; unset, ``calm``.
    pace: Literal["calm", "dynamic"] | None = None
    format: VideoFormat = Field(default_factory=VideoFormat)
    theme: Theme = Field(default_factory=Theme)
    scenes: list[Scene] = Field(min_length=1, max_length=MAX_SCENES)
    default_transition: Transition = Field(default_factory=Transition)
    voiceover: Voiceover = Field(default_factory=Voiceover)
    music: Music | None = None
    #: The clips' own sound where a scene does not say (its ``audio``):
    #: ``auto`` drops an AI shot's under the music (its generated music would
    #: fight the bed) and keeps an uploaded video's; ``keep`` / ``off`` keep or
    #: drop every clip's, ``off`` leaving the voice and the music alone.
    clip_sound: Literal["auto", "keep", "off"] = "auto"
    captions: Captions = Field(default_factory=Captions)
    #: Fade in from black at the start, out to black at the end.
    fade_in: bool = True
    fade_out: bool = True
    #: The cards the template adds (``templates.apply_template``), as the author sets them: their length, their kicker.
    template_scenes: TemplateScenes | None = None
    #: The recurring props and characters its AI shots must keep (:class:`VideoReference`): a clip scene names
    #: those it is held to in ``consistency``.
    references: list[VideoReference] = Field(default_factory=list, max_length=MAX_REFERENCES)

    @field_validator("template_id")
    @classmethod
    def _template_id(cls, value: str | None) -> str | None:
        return _uuid_text(value, "template_id")

    @model_validator(mode="after")
    def _template_or_freestyle(self) -> VideoSpec:
        if self.freestyle and self.template_id:
            raise ValueError("freestyle means no template: drop template_id or freestyle")
        return self

    @model_validator(mode="after")
    def _unique_scene_ids(self) -> VideoSpec:
        used: set[str] = set()
        for index, scene in enumerate(self.scenes):
            if scene.id is None:
                candidate = f"{scene.type}-{index + 1}"
                suffix = 2
                while candidate in used:
                    candidate = f"{scene.type}-{index + 1}-{suffix}"
                    suffix += 1
                scene.id = candidate
            elif scene.id in used:
                raise ValueError(f"scene id {scene.id!r} is used twice")
            used.add(scene.id)
        return self

    @model_validator(mode="after")
    def _references_named(self) -> VideoSpec:
        """Each reference once, and every ``consistency`` id a reference of the video."""
        known: set[str] = set()
        for reference in self.references:
            if reference.id in known:
                raise ValueError(f"reference id {reference.id!r} is used twice")
            known.add(reference.id)
        for index, scene in enumerate(self.scenes):
            unknown = [item for item in getattr(scene, "consistency", ()) if item not in known]
            if unknown:
                listed = ", ".join(sorted(known)) or "none"
                raise ValueError(
                    f"scenes.{index} ({scene.id}): consistency names no reference of the video: "
                    f"{', '.join(repr(item) for item in unknown)} (the video's references: {listed})"
                )
        return self


def _validated(raw: Any) -> VideoSpec:
    if isinstance(raw, str):
        import json

        try:
            raw = json.loads(raw)
        except ValueError:
            raise VideoSpecError([SpecProblem("", "the spec is not a JSON object")]) from None
    if not isinstance(raw, dict):
        raise VideoSpecError([SpecProblem("", "the spec is a JSON object with title and scenes")])
    try:
        return VideoSpec.model_validate(raw)
    except ValidationError as exc:
        raise VideoSpecError(_problems_from(exc, raw)) from None


def validate_video_spec(raw: Any) -> dict:
    """The normalised video spec (scene ids assigned, every default filled), or :class:`VideoSpecError`."""
    return _validated(raw).model_dump(mode="json", exclude_none=True)


def authored_spec(raw: Any) -> dict:
    """The spec as its author wrote it, validated: only what was set (plus the scene ids).

    What a project stores, so a template's defaults keep filling what the
    author left unset on every save and render, and a value the author did set
    is never mistaken for a default (:mod:`.templates`).
    """
    return _validated(raw).model_dump(mode="json", exclude_unset=True, exclude_none=True)


def parse_video_spec(stored: dict) -> VideoSpec:
    """A stored (already validated) spec as models; the renderer's reader."""
    return VideoSpec.model_validate(stored)


def clip_sound_choice(scene: ClipScene, spec: VideoSpec) -> tuple[bool, str]:
    """Whether *scene*'s own sound plays in *spec* (when the clip has one), and what decided it.

    ``scene``: its ``audio``; ``video``: the video's ``clip_sound``; ``music``:
    an AI shot under the video's music, dropped; ``auto``: kept. One rule for
    the render, the preview and the tool results.
    """
    if scene.audio is not None:
        return scene.audio, "scene"
    if spec.clip_sound != "auto":
        return spec.clip_sound == "keep", "video"
    if scene.clip_id and spec.music is not None and spec.music.plays:
        return False, "music"
    return True, "auto"


def parse_take_script(stored: dict) -> TakeScript:
    return TakeScript.model_validate(stored)


def stored_take_script(script: TakeScript) -> dict:
    """*script* as a recording keeps it (:func:`validate_take_script`'s form)."""
    return script.model_dump(mode="json", exclude_none=True)


def referenced_ids(spec: dict) -> dict[str, set[str]]:
    """Every id the spec names, by kind: ``take``, ``clip``, ``music``, ``file``, ``presentation``, ``template``."""
    refs: dict[str, set[str]] = {k: set() for k in ("take", "clip", "music", "file", "presentation", "template")}
    for scene in spec.get("scenes") or []:
        kind = scene.get("type")
        for overlay in scene.get("overlays") or []:
            if isinstance(overlay, dict) and overlay.get("type") == "image" and overlay.get("file_id"):
                refs["file"].add(overlay["file_id"])
            inserted = isinstance(overlay, dict) and overlay.get("type") == "screen_insert"
            shown = [overlay.get("source"), *(overlay.get("sources") or [])] if inserted else []
            for source in shown:
                if not isinstance(source, dict):
                    continue
                for key, source_kind in (("file_id", "file"), ("take_id", "take"), ("clip_id", "clip")):
                    if source.get(key):
                        refs[source_kind].add(source[key])
        if kind == "recording" and scene.get("take_id"):
            refs["take"].add(scene["take_id"])
        elif kind == "clip":
            if scene.get("clip_id"):
                refs["clip"].add(scene["clip_id"])
            if scene.get("file_id"):
                refs["file"].add(scene["file_id"])
        elif kind == "image" and scene.get("file_id"):
            refs["file"].add(scene["file_id"])
            animate = scene.get("animate")
            if isinstance(animate, dict) and animate.get("clip_id"):
                refs["clip"].add(animate["clip_id"])
        elif kind == "statement" and isinstance(scene.get("backdrop"), dict):
            backdrop = scene["backdrop"]
            if backdrop.get("clip_id"):
                refs["clip"].add(backdrop["clip_id"])
            if backdrop.get("file_id"):
                refs["file"].add(backdrop["file_id"])
        elif kind == "slide" and scene.get("presentation_id"):
            refs["presentation"].add(scene["presentation_id"])
        elif kind == "figure":
            if scene.get("presentation_id"):
                refs["presentation"].add(scene["presentation_id"])
            refs["file"] |= figure_file_ids(scene.get("block_spec"))
        elif kind == "motion":
            refs["file"] |= motion_file_ids(scene)
    theme = spec.get("theme") or {}
    if theme.get("logo_file_id"):
        refs["file"].add(theme["logo_file_id"])
    # The pictures of the video's references: checked like any file the spec names.
    refs["file"] |= {str(item["file_id"]) for item in references_of(spec).values()}
    if spec.get("template_id"):
        refs["template"].add(spec["template_id"])
    music = spec.get("music") or {}
    if music.get("file_id"):
        refs["file"].add(music["file_id"])
    if music.get("music_id"):
        refs["music"].add(music["music_id"])
    return refs


def motion_file_ids(scene: dict) -> set[str]:
    """The files a stored motion *scene* draws: its image and Lottie elements'."""
    return {
        str(element["file_id"])
        for element in scene.get("elements") or []
        if isinstance(element, dict) and element.get("kind") in ("image", "lottie") and element.get("file_id")
    }


def references_of(spec: dict) -> dict[str, dict]:
    """``reference id -> {id, file_id, label, kind, description}`` of a stored *spec*'s references."""
    return {
        str(item["id"]): item
        for item in spec.get("references") or []
        if isinstance(item, dict) and item.get("id") and item.get("file_id")
    }


def consistency_of(spec: dict) -> dict[str, list[dict]]:
    """``clip id -> the references it is held to`` across the clip scenes of a stored *spec*: a shot two scenes show
    is held to what both name, each reference once."""
    known = references_of(spec)
    held: dict[str, list[dict]] = {}
    for scene in spec.get("scenes") or []:
        if not isinstance(scene, dict) or scene.get("type") != "clip" or not scene.get("clip_id"):
            continue
        for item in scene.get("consistency") or []:
            reference = known.get(str(item))
            if reference is None:
                continue
            listed = held.setdefault(str(scene["clip_id"]), [])
            if all(kept["id"] != reference["id"] for kept in listed):
                listed.append(reference)
    return held


def insert_footage(spec: dict) -> dict[str, str]:
    """``clip id -> scene id`` of each AI shot a stored *spec* draws a screen insert on: a clip scene's shot,
    a statement's backdrop shot - the footage a ``screen_insert`` overlay of that scene lands on, the first scene
    naming it."""
    out: dict[str, str] = {}
    for scene in spec.get("scenes") or []:
        if not isinstance(scene, dict) or not any(
            isinstance(overlay, dict) and overlay.get("type") == "screen_insert" for overlay in scene.get("overlays") or []
        ):
            continue
        backdrop = scene.get("backdrop") if isinstance(scene.get("backdrop"), dict) else {}
        footage = scene.get("clip_id") if scene.get("type") == "clip" else backdrop.get("clip_id") if scene.get("type") == "statement" else None
        if footage:
            out.setdefault(str(footage), str(scene.get("id") or ""))
    return out


#: The media whose users ``video_projects.asset_refs`` indexes: every kind a spec names but its
#: decks and its template, which a purge never deletes - the files included.
INDEXED_ASSET_KINDS: tuple[str, ...] = ("take", "clip", "music", "file")
#: The rule ``video_projects.asset_refs`` was written by (``asset_refs_version``): a row written by an older one is
#: indexed again by the media retention (``retention.index_stale_refs``). 2: the files, the media the AI
#: shots it plays are made from, and what its ready renders drew.
ASSET_REFS_VERSION = 2


def asset_ref(kind: str, asset_id: object) -> str | None:
    """The key of one medium in ``video_projects.asset_refs``: ``"<kind>:<uuid>"``; ``None`` for no valid id."""
    try:
        return f"{kind}:{uuid.UUID(str(asset_id))}"
    except ValueError:
        return None


def asset_refs(spec: dict) -> list[str]:
    """The recordings, AI shots, AI tracks and files *spec* names, as ``video_projects.asset_refs`` keys (sorted).

    Every place :func:`referenced_ids` reads: a scene's own footage, its screen
    inserts' sources (a wall's panes included), a statement's backdrop, an
    image scene and its animation, an image overlay, a figure's pictures, the
    logo and the music. What the shots are made from and what the renders drew
    are added by ``retention.media_index``.
    """
    refs = referenced_ids(spec)
    keys = {asset_ref(kind, asset_id) for kind in INDEXED_ASSET_KINDS for asset_id in refs[kind]}
    return sorted(key for key in keys if key)


def unclosed_accents(spec: dict) -> list[str]:
    """One note per statement line holding an ``*`` that opens an accent no ``*`` closes.

    The compositor draws such a line as typed, the asterisk included
    (:data:`ACCENT_PATTERN`); the note names the word, so the author closes
    the pair or drops the asterisk before a render shows it.
    """
    notes: list[str] = []
    for index, scene in enumerate(spec.get("scenes") or []):
        if not isinstance(scene, dict) or scene.get("type") != "statement":
            continue
        texts = [("line", line) for line in scene.get("lines") or []]
        texts += [("secondary line", line) for line in scene.get("secondary") or []]
        for number, (what, text) in enumerate(texts, start=1):
            found = _UNCLOSED_ACCENT.search(_ACCENT.sub("", str(text)))
            if found is None:
                continue
            word = found.group(1).rstrip(".,;:!?")
            notes.append(
                f"Scene {scene.get('id') or index}: the * before \"{word}\" in {what} {number} opens no accent "
                f"(no * closes it), so it is drawn as typed. Wrap the accent words in a pair (*{word}*), or "
                "remove the *."
            )
    return notes


def spec_summary(spec: dict) -> str:
    """One line per scene, for a tool result."""
    lines = []
    for index, scene in enumerate(spec.get("scenes") or []):
        kind = scene.get("type")
        label = scene.get("title") or scene.get("take_id") or scene.get("clip_id") or scene.get("file_id") or ""
        over = ""
        if kind == "slide":
            label = f"deck {scene.get('presentation_id')} slide {scene.get('slide') or scene.get('slide_id')}"
        elif kind == "figure" and scene.get("block_spec"):
            label = f"{(scene.get('block_spec') or {}).get('type')} written in the scene"
        elif kind == "figure":
            label = (
                f"block {scene.get('block') or 0} of deck {scene.get('presentation_id')} slide "
                f"{scene.get('slide') or scene.get('slide_id') or _FIGURE_SLIDE}"
            )
        elif kind == "motion":
            texts = [scene.get("title"), *(scene.get("lines") or []), scene.get("figure")]
            shown = next((text for text in texts if text), None)
            label = " ".join(filter(None, [scene.get("composition"), f'"{shown}"' if shown else None]))
            if scene.get("elements"):
                label = f"{label} + {len(scene['elements'])} elements".strip(" +")
        elif kind == "statement":
            label = " / ".join(scene.get("lines") or [])
            backdrop = scene.get("backdrop") or {}
            if backdrop.get("clip_id"):
                over = f" over AI shot {backdrop['clip_id']}"
            elif backdrop.get("file_id"):
                over = f" over file {backdrop['file_id']}"
        narration = " + narration" if scene.get("narration") else ""
        lines.append(f"{index}. [{scene.get('id')}] {kind}: {str(label)[:80]}{over}{narration}")
    return "\n".join(lines)


_MUSIC_SOURCE_WORDS = {
    "track": "built-in track `{}`",
    "music": "AI track `{}`",
    "file": "an uploaded sound",
    "prompt": "an AI track composed on save",
}
_SOUND_REASON_WORDS = {"scene": " (its audio: {})", "video": "", "music": " (an AI shot under the music)", "auto": ""}


def sound_summary(spec: VideoSpec, silent: set[str] | frozenset[str] = frozenset()) -> str:
    """How *spec* sounds, for a tool result: what leads, the music, each clip's own sound.

    *silent*: the clip scenes whose video has no sound track (``projects.silent_clip_scenes``) - named
    as silent, never as a sound kept or dropped.
    """
    narrated = any(scene.narration for scene in spec.scenes)
    if not narrated:
        parts = ["no narration"]
    else:
        parts = ["the voice-over leads" if spec.voiceover.enabled else "the narration is captions only"]
    music = spec.music
    if music is not None and music.plays:
        kind, ref = music.source or ("prompt", "")
        level = f"music: {_MUSIC_SOURCE_WORDS[kind].format(ref)} at {music.volume:g}"
        if music.ducking and narrated and spec.voiceover.enabled:
            level += f", {music.duck_db:g} dB down under the voice"
        parts.append(level)
    else:
        parts.append("no music")
    text = "Sound: " + "; ".join(parts) + "."
    kept: list[str] = []
    dropped: list[str] = []
    mute: list[str] = []
    for scene in spec.scenes:
        if scene.type != "clip":
            continue
        if scene.id in silent:
            mute.append(str(scene.id))
            continue
        keep, reason = clip_sound_choice(scene, spec)
        said = _SOUND_REASON_WORDS[reason].format(str(keep).lower())
        if keep and scene.volume <= 0:
            dropped.append(f"{scene.id} (volume 0)")
        elif keep:
            kept.append(f"{scene.id}{said} at {scene.volume:g}")
        else:
            dropped.append(f"{scene.id}{said}")
    if kept or dropped or mute:
        groups = [f"off for {', '.join(dropped)}"] if dropped else []
        if kept:
            groups.append(f"kept for {', '.join(kept)}, levelled with the voice and lowered under it")
        if mute:
            groups.append(f"none for {', '.join(mute)} (no sound track)")
        text += (
            f" Clips' own sound (clip_sound {spec.clip_sound}): {'; '.join(groups)}."
            " A scene's audio true / false decides for that scene."
        )
    return text
