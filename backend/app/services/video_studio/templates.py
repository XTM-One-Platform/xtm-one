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

"""Video templates: one brand's look and rules, applied to every video that wears it.

A video template is a template container of kind ``video``, the entity decks
and reports are dressed by, so a brand is declared once and the Templates
library lists, shares, duplicates and publishes it like any other:

* ``theme_config`` - the palette and the fonts, the deck keys
  (``presentations.THEME_CONFIG_KEYS``): a brand's video and its decks read
  the same colours, ``chart_1..6`` included (the series a ``mosaic`` backdrop
  draws its tiles in);
* ``extra_metadata.supported_schemes`` / ``theme_variants`` - the colour
  schemes, read with the deck resolver (``presentations.resolve_template_theme``);
* the ``logo`` / ``logo_light`` / ``logo_mark`` assets - the marks the title
  cards, the outro and the watermark draw; their captions say what each one
  spells (:attr:`VideoTemplate.marks`), so a card never writes again the
  brand name its logo already shows (:func:`brand_repeats`);
* ``extra_metadata.video`` - the RULES, :class:`VideoTemplateStyle`:

  - ``purposes``: what the template is made for (:data:`TEMPLATE_PURPOSES`),
    so the "New video" dialog and the agent pick one per kind of video;
  - ``defaults``: the top of a spec the template presets (the scheme, the
    backdrop, the watermark, the transition, the captions, the voice-over);
  - ``scene_defaults`` / ``overlay_defaults``: per scene and overlay type
    (the browser window of a recording, the cursor, the camera, the title
    card style, the lower third style, an image overlay's width and
    opacity); a scene type's defaults may also add the brand's marks as
    image overlays by their role (``{type: image, asset: logo_mark}``,
    never a file), drawn before the scene's own overlays unless the
    scene draws that mark itself;
  - ``intro`` / ``outro``: the title card and the closing card the template
    adds to a video that does not open or close on one;
  - ``brand_name``: the name the template's logo spells out, when it is not
    the template's own name;
  - ``locked``: the paths whose template value always wins - the author's is
    replaced and the save says so - plus ``theme.palette``, which keeps the
    template's colours and logo over any the spec names;
  - ``aspect_ratios``: the formats the brand ships, the first the default;
  - ``shots``: the art direction of every AI shot made for a video wearing it
    (:class:`ShotDirection`) - the look the video model is asked for
    and what no shot shows. A shot records it when it is asked
    (:func:`shot_direction`), the worker applies it (:func:`direct_shot`) and a
    save names the shots made under another template's (:func:`shot_notes`).

A default fills only what the author left unset: a project stores the spec AS
WRITTEN (``spec.authored_spec``) and the effective spec is computed from it and
the template on every save and every render (:func:`apply_template`), so a
brand update reaches the next render of every video wearing it, and "free
style" stays one field away: ``freestyle: true`` applies no template at all,
not even the platform default (:data:`DEFAULT_VIDEO_TEMPLATE_SLUGS`). A
presentation template may dress a video too, for its palette and logo only.
"""

from __future__ import annotations

import copy
import logging
import re
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal

from pydantic import Field, ValidationError, model_validator

from app.services.video_studio.spec import (
    BRAND_ASSETS,
    MAX_OVERLAYS_PER_SCENE,
    SpecProblem,
    VideoSpecError,
    _problems_from,
    _Strict,
    brand_marks,
    card_draws_logo,
    validate_video_spec,
)

logger = logging.getLogger(__name__)

#: ``extra_metadata`` key of a video template's rules.
VIDEO_STYLE_KEY = "video"

#: The built-in video template a spec that names none wears: the first one
#: this platform ships (the Filigran one in Copilot mode, XTM One elsewhere).
DEFAULT_VIDEO_TEMPLATE_SLUGS: tuple[str, ...] = ("filigran-video", "xtm-one-video")

#: The lock that keeps the template's colours and logo over the spec's own.
PALETTE_LOCK = "theme.palette"
_PALETTE_OVERRIDES = ("accent", "accent_2", "background_color", "text_color", "logo_file_id")
#: The fields of a locked object its lock leaves to each scene unless the locked value names them: a recording
#: drawn edge to edge (``frame.fill``) for a UI that must be read, which a template turns off by locking ``fill: false``.
LOCK_OPEN_FIELDS: dict[str, frozenset[str]] = {"scenes.recording.frame": frozenset({"fill"})}

AspectRatio = Literal["16:9", "9:16", "1:1", "4:5"]
SceneType = Literal["title", "statement", "recording", "slide", "figure", "clip", "image", "outro", "motion"]
OverlayType = Literal["lower_third", "callout", "badge", "caption", "spotlight", "image"]
ASPECT_RATIOS: tuple[str, ...] = ("16:9", "9:16", "1:1", "4:5")
SCENE_TYPES: tuple[str, ...] = ("title", "statement", "recording", "slide", "figure", "clip", "image", "outro", "motion")
#: What a template is made for: the "New video" dialog suggests one per kind of video.
TEMPLATE_PURPOSES: tuple[str, ...] = ("product_demo", "brand_film", "event", "launch", "explainer", "social")
TemplatePurpose = Literal["product_demo", "brand_film", "event", "launch", "explainer", "social"]

#: The top of a spec a template may preset, and which of their fields (None:
#: any). The palette and fonts are its ``theme_config``; the format its
#: ``aspect_ratios``.
_DEFAULT_KEYS: dict[str, frozenset[str] | None] = {
    # How the video moves (``pace.with_defaults``): its presets fill what the spec leaves unset before the
    # template's own do.
    "pace": None,
    "format": frozenset({"fps"}),
    # How the brand moves: its personality and its signature, beat sync and motion blur.
    "theme": frozenset({"mode", "background", "watermark", "motion"}),
    "default_transition": None,
    "voiceover": None,
    "captions": None,
    "fade_in": None,
    "fade_out": None,
    # A brand's sound: a built-in track and how it sits under the voice.
    "music": frozenset({"track", "volume", "ducking", "duck_db", "fade_in", "fade_out"}),
}
#: The fields of a video's music that name what plays: the author's, when set, win over the template's track.
_MUSIC_SOURCES = ("track", "music_id", "file_id", "prompt")
#: What a scene IS rather than how it looks: never a template's to preset.
_SCENE_CONTENT_KEYS = frozenset(
    {
        "id",
        "type",
        "take_id",
        "presentation_id",
        "slide",
        "slide_id",
        "block",
        "block_spec",
        "clip_id",
        "file_id",
        "title",
        "subtitle",
        "narration",
        "overlays",
        "trim_start",
        "trim_end",
        "highlights",
        "lines",
        "secondary",
        "chips",
        "figure",
        "backdrop",
        # A motion scene's content: what it draws, its texts.
        "composition",
        "elements",
        "items",
        "label",
    }
)
_OVERLAY_DEFAULT_KEYS = frozenset({"style", "position", "duration", "width", "opacity"})
#: The scene-level overlays a template may add: its brand marks by role, never a file.
_BRAND_OVERLAY_KEYS = frozenset({"type", "asset", "position", "width", "opacity", "start", "duration", "text"})
_BOOKEND_KEYS = {
    "title": frozenset(
        {"title", "subtitle", "kicker", "duration", "style", "align", "show_logo", "narration", "transition"}
    ),
    "outro": frozenset(
        {"title", "subtitle", "kicker", "cta", "url", "duration", "style", "show_logo", "narration", "transition"}
    ),
}
_PROBE_ID = "00000000-0000-0000-0000-000000000000"
_PROBE_SCENES: dict[str, dict[str, Any]] = {
    "title": {"type": "title", "title": "Probe"},
    "statement": {"type": "statement", "lines": ["Probe"]},
    "recording": {"type": "recording", "take_id": _PROBE_ID},
    "slide": {"type": "slide", "presentation_id": _PROBE_ID},
    "figure": {"type": "figure", "presentation_id": _PROBE_ID},
    "clip": {"type": "clip", "clip_id": _PROBE_ID},
    "image": {"type": "image", "file_id": _PROBE_ID},
    "outro": {"type": "outro", "title": "Probe"},
    "motion": {"type": "motion", "composition": "kinetic_title", "title": "Probe"},
}


# ── Merging and paths ─────────────────────────────────────────────────────


def deep_merge(base: Any, over: Any) -> Any:
    """*over* laid on *base*: objects merge key by key, anything else is replaced."""
    if isinstance(base, dict) and isinstance(over, dict):
        merged = copy.deepcopy(base)
        for key, value in over.items():
            merged[key] = deep_merge(merged[key], value) if key in merged else copy.deepcopy(value)
        return merged
    return copy.deepcopy(over)


def _lookup(data: Any, parts: list[str]) -> tuple[bool, Any]:
    for part in parts:
        if not isinstance(data, dict) or part not in data:
            return False, None
        data = data[part]
    return True, data


def _assign(data: dict, parts: list[str], value: Any) -> None:
    for part in parts[:-1]:
        child = data.get(part)
        if not isinstance(child, dict):
            child = {}
            data[part] = child
        data = child
    data[parts[-1]] = copy.deepcopy(value)


def _shown(value: Any) -> str:
    if isinstance(value, dict):
        return "its " + ", ".join(sorted(value)) + " settings"
    text = value if isinstance(value, str) else repr(value)
    return text if len(text) <= 60 else text[:57] + "..."


# ── The rules ─────────────────────────────────────────────────────────────


class Bookend(_Strict):
    """The title card (``intro``) or closing card (``outro``) a template adds."""

    #: Added when the video does not already open (close) on a scene of that
    #: type; ``false`` keeps the card as the defaults of one the author writes.
    required: bool = True
    #: The card's fields; ``{title}`` in a text is the video's title.
    scene: dict[str, Any] = Field(default_factory=dict)


#: The most terms a template's art direction avoids.
MAX_AVOIDED_TERMS = 16
ShotTerm = Annotated[str, Field(min_length=2, max_length=60)]


class ShotDirection(_Strict):
    """``shots``: how every AI shot made for a video wearing the template looks.

    Applied by the platform, never left to the prompt the author writes
    (:func:`direct_shot`): the style joins the prompt of a shot the model draws
    from text, the avoided terms the negative prompt of a model that takes one;
    the frames of a made shot are read for them and a shot showing one says so.
    It steers a shot, it never refuses one.
    """

    #: The look, sent to the video model as written: light, lens, materials, the palette in words, the motion.
    style: str | None = Field(None, max_length=600)
    #: What no shot shows, one term each.
    avoid: list[ShotTerm] = Field(default_factory=list, max_length=MAX_AVOIDED_TERMS)


class VideoTemplateStyle(_Strict):
    """``extra_metadata.video``: what a video template presets, adds and locks."""

    aspect_ratios: list[AspectRatio] = Field(default_factory=lambda: list(ASPECT_RATIOS), min_length=1, max_length=4)
    #: What the template is made for (:data:`TEMPLATE_PURPOSES`); a template made for several names them all.
    purposes: list[TemplatePurpose] = Field(default_factory=list, max_length=6)
    defaults: dict[str, Any] = Field(default_factory=dict)
    scene_defaults: dict[SceneType, dict[str, Any]] = Field(default_factory=dict)
    overlay_defaults: dict[OverlayType, dict[str, Any]] = Field(default_factory=dict)
    intro: Bookend | None = None
    outro: Bookend | None = None
    #: The name the template's logo spells out (its wordmark) when the template's own name is another one.
    brand_name: str | None = Field(None, min_length=1, max_length=60)
    locked: list[str] = Field(default_factory=list, max_length=40)
    shots: ShotDirection | None = None

    @model_validator(mode="after")
    def _coherent(self) -> VideoTemplateStyle:
        problems = _style_problems(self)
        if problems:
            raise ValueError("; ".join(problems))
        return self

    def lock_value(self, path: str) -> tuple[bool, Any]:
        """The value a lock keeps, found in the defaults it locks."""
        parts = path.split(".")
        if parts[0] == "scenes" and len(parts) >= 3:
            return _lookup(self.scene_defaults.get(parts[1]) or {}, parts[2:])
        if parts[0] == "overlays" and len(parts) >= 3:
            return _lookup(self.overlay_defaults.get(parts[1]) or {}, parts[2:])
        return _lookup(self.defaults, parts)


def _bookend_scene(style: VideoTemplateStyle, kind: str, bookend: Bookend, title: str) -> dict:
    scene = deep_merge(style.scene_defaults.get(kind) or {}, bookend.scene)
    scene["type"] = kind
    for key in ("title", "subtitle", "kicker", "cta", "url", "narration"):
        if isinstance(scene.get(key), str):
            scene[key] = scene[key].replace("{title}", title)
    scene.setdefault("title", title)
    return scene


#: Which card each ``template_scenes`` entry sets: the opening title card, the closing outro.
TEMPLATE_CARDS = {"intro": "title", "outro": "outro"}


def _card_settings(value: Any) -> dict:
    """What ``template_scenes.<card>`` sets on a card the template adds: its duration, its kicker, the
    opening card's alignment.

    A kicker set to ``null`` is kept as such: the card then has none, not even the template's.
    """
    if not isinstance(value, dict):
        return {}
    settings = {key: value[key] for key in ("duration", "align") if value.get(key) is not None}
    if "kicker" in value:
        settings["kicker"] = value["kicker"]
    return settings


def _sets_kicker(cards: dict, scene: dict) -> bool:
    """Whether the spec sets the kicker of *scene*, a card the template adds (``template_scenes.<card>.kicker``)."""
    setting = cards.get(card_name(scene.get("type")))
    return isinstance(setting, dict) and "kicker" in setting


def card_name(scene_type: str | None) -> str:
    """The ``template_scenes`` entry of a card the template adds, by the card's type."""
    return next((name for name, kind in TEMPLATE_CARDS.items() if kind == scene_type), "intro")


def _brand_overlays(value: Any) -> bool:
    """Whether *value* is a list of image overlays naming a brand mark by its role, and nothing more."""
    return isinstance(value, list) and all(
        isinstance(item, dict)
        and item.get("type") == "image"
        and item.get("asset") in BRAND_ASSETS
        and set(item) <= _BRAND_OVERLAY_KEYS
        for item in value
    )


def _with_brand_overlays(scene: dict, brand: list[dict]) -> None:
    """*scene*'s overlays with the template's brand marks before its own, unless it draws that mark itself."""
    own = list(scene.get("overlays") or [])
    drawn = {o.get("asset") for o in own if isinstance(o, dict) and o.get("type") == "image" and o.get("asset")}
    added = [copy.deepcopy(o) for o in brand if o.get("asset") not in drawn]
    if added:
        scene["overlays"] = [*added, *own][:MAX_OVERLAYS_PER_SCENE]


def _style_problems(style: VideoTemplateStyle) -> list[str]:
    problems: list[str] = []
    for key, value in style.defaults.items():
        if key not in _DEFAULT_KEYS:
            problems.append(
                f"defaults.{key} is not something a template presets (use one of {', '.join(_DEFAULT_KEYS)})"
            )
            continue
        allowed = _DEFAULT_KEYS[key]
        if allowed is not None:
            if not isinstance(value, dict):
                problems.append(f"defaults.{key} must be an object")
                continue
            problems.extend(
                f"defaults.{key}.{sub} is not something a template presets (use one of {', '.join(sorted(allowed))})"
                for sub in value
                if sub not in allowed
            )
            if key == "music" and not value.get("track"):
                problems.append("defaults.music names its built-in track (track): the sound every video starts with")
    for kind, values in style.scene_defaults.items():
        problems.extend(
            f"scene_defaults.{kind}.{name} is what a scene shows, not how it looks: the author writes it"
            for name in values
            if name in _SCENE_CONTENT_KEYS and name != "overlays"
        )
        if "overlays" in values and not _brand_overlays(values["overlays"]):
            problems.append(
                f"scene_defaults.{kind}.overlays: a template adds image overlays of its brand marks only - "
                "{type: image, asset: logo | logo_light | logo_mark, position?, width?, opacity?, start?, duration?}"
            )
        camera = values.get("camera")
        if isinstance(camera, dict) and "keyframes" in camera:
            problems.append(f"scene_defaults.{kind}.camera.keyframes belong to one recording, not to a template")
    for kind, values in style.overlay_defaults.items():
        problems.extend(
            f"overlay_defaults.{kind}.{name}: a template presets {', '.join(sorted(_OVERLAY_DEFAULT_KEYS))} only"
            for name in values
            if name not in _OVERLAY_DEFAULT_KEYS
        )
    for name, bookend, kind in (("intro", style.intro, "title"), ("outro", style.outro, "outro")):
        if bookend is not None:
            problems.extend(
                f"{name}.scene.{key} is not a field of the {kind} card"
                for key in bookend.scene
                if key not in _BOOKEND_KEYS[kind]
            )
    for path in style.locked:
        if path == PALETTE_LOCK:
            continue
        if path.startswith("format."):
            problems.append(f"locked {path}: the formats a template allows are its aspect_ratios")
            continue
        found, _ = style.lock_value(path)
        if not found:
            problems.append(f"locked {path} names no value: set it in the defaults it locks first")
    if problems:
        return problems
    # Every preset must be something a spec accepts: probe one scene of each
    # type (and one overlay of each preset type) with the defaults applied.
    probe_scenes = [deep_merge(style.scene_defaults.get(kind) or {}, _PROBE_SCENES[kind]) for kind in SCENE_TYPES]
    probe_scenes[0]["overlays"] = [
        *(probe_scenes[0].get("overlays") or []),
        *(
            {**values, "type": kind, **({"asset": "logo_mark"} if kind == "image" else {"text": "Probe"})}
            for kind, values in style.overlay_defaults.items()
        ),
    ]
    for name, bookend, kind in (("intro", style.intro, "title"), ("outro", style.outro, "outro")):
        if bookend is not None:
            scene = _bookend_scene(style, kind, bookend, "Probe")
            scene["id"] = f"probe-{name}"
            probe_scenes.append(scene)
    probe = deep_merge(style.defaults, {"title": "Probe", "scenes": probe_scenes})
    try:
        validate_video_spec(probe)
    except VideoSpecError as exc:
        problems.extend(f"the presets do not make a valid video: {p.path}: {p.message}" for p in exc.problems[:8])
    return problems


def validate_style(raw: object) -> tuple[dict | None, list[str]]:
    """``(canonical rules, problems)`` of an ``extra_metadata.video`` value; ``None`` drops it."""
    if raw is None or raw == {}:
        return None, []
    try:
        style = VideoTemplateStyle.model_validate(raw)
    except ValidationError as exc:
        return None, [
            f"extra_metadata.{VIDEO_STYLE_KEY}: {p.path + ': ' if p.path else ''}{p.message}"
            for p in _problems_from(exc, raw)
        ]
    return style.model_dump(mode="json", exclude_none=True), []


def parse_style(raw: object) -> VideoTemplateStyle | None:
    """The stored rules as a model; ``None`` when there are none or they no longer validate."""
    if not raw:
        return None
    try:
        return VideoTemplateStyle.model_validate(raw)
    except ValidationError:
        logger.warning("A stored video template style no longer validates; the template applies its palette only")
        return None


# ── A template as a video reads it ────────────────────────────────────────


@dataclass(frozen=True)
class BrandMark:
    """A picture the template draws as one of its brand marks (``logo`` / ``logo_light`` / ``logo_mark``)."""

    file_id: str
    #: The role the asset carries: a ``logo_mark`` is the symbol without the wordmark, it spells no name.
    role: str
    #: Its caption (the asset's description): what the picture shows.
    caption: str = ""


@dataclass
class VideoTemplate:
    id: str
    name: str
    kind: str
    description: str | None = None
    style: VideoTemplateStyle | None = None
    supported_schemes: list[str] = field(default_factory=list)
    guidance: str | None = None
    is_builtin: bool = False
    #: The brand marks it draws, by the role they fill (:func:`load_video_template`); empty: none known.
    marks: dict[str, BrandMark] = field(default_factory=dict)

    @classmethod
    def from_container(cls, container: Any) -> VideoTemplate:
        from app.services.template_container_seed import BUILTIN_TAG
        from app.services.template_containers import VIDEO_KIND, container_supported_schemes

        metadata = container.extra_metadata if isinstance(container.extra_metadata, dict) else {}
        kind = str(container.kind or "").strip().lower()
        guidance = metadata.get("guidance")
        return cls(
            id=str(container.id),
            name=container.name,
            kind=kind,
            description=container.description,
            style=parse_style(metadata.get(VIDEO_STYLE_KEY)) if kind == VIDEO_KIND else None,
            supported_schemes=container_supported_schemes(container),
            guidance=guidance if isinstance(guidance, str) else None,
            is_builtin=BUILTIN_TAG in (container.tags or []),
        )


@dataclass
class AppliedTemplate:
    #: The effective spec, every default filled: what a render draws.
    spec: dict
    #: One sentence per thing the template changed against the author's spec.
    notes: list[str]
    #: Ids of the scenes the template added (its intro / outro).
    added_scenes: list[str]
    template: VideoTemplate | None


def _unique_id(wanted: str, used: set[str]) -> str:
    candidate, suffix = wanted, 2
    while candidate in used:
        candidate = f"{wanted}-{suffix}"
        suffix += 1
    used.add(candidate)
    return candidate


# ── The brand a card spells ───────────────────────────────────────

#: A word of a card's text: letters and digits, kept whole across the dots, slashes and hyphens of an
#: address, so ``filigran.io/thread`` is one word and never writes the brand ``THREAD``.
_WORD = re.compile(r"\w+(?:[./@:-]\w+)*")


def _words(text: object) -> list[str]:
    return [word.casefold() for word in _WORD.findall(str(text or ""))]


def names_in(text: object, names: Iterable[str]) -> list[str]:
    """The *names* *text* writes as whole words, whatever the case: ``THREAD 2026`` writes ``THREAD``."""
    words = _words(text)
    found = []
    for name in names:
        wanted = _words(name)
        if wanted and any(words[i : i + len(wanted)] == wanted for i in range(len(words) - len(wanted) + 1)):
            found.append(name)
    return found


def brand_names(template: VideoTemplate | None) -> list[str]:
    """The names a template's logo may spell: its ``brand_name``, then its own name."""
    if template is None:
        return []
    declared = template.style.brand_name if template.style is not None else None
    names: list[str] = []
    for name in (declared, template.name):
        if name and name.casefold() not in {known.casefold() for known in names}:
            names.append(name)
    return names


@dataclass(frozen=True)
class BrandRepeat:
    """A brand name a card writes while one of its marks already spells it."""

    name: str
    #: What spells it: ``show_logo`` (the card's logo) or ``overlays.<i>`` (an image overlay of a logo).
    mark: str
    #: The field of the scene that writes it again (``kicker``, ``title``, ``lines.1``, ``overlays.2.text``).
    field: str
    text: str


#: The words of a card a viewer reads, by scene type.
_CARD_TEXT: dict[str, tuple[str, ...]] = {
    "title": ("kicker", "title", "subtitle"),
    "outro": ("kicker", "title", "subtitle", "cta", "url"),
    "statement": ("kicker", "figure", "lines", "secondary", "chips"),
}
#: The overlays whose words are drawn (an image's and a screen insert's text is their label).
_READ_OVERLAYS = frozenset({"lower_third", "callout", "badge", "caption"})


def _card_texts(scene: dict) -> list[tuple[str, str]]:
    """``(field, text)`` of every word *scene* draws."""
    found: list[tuple[str, str]] = []
    for key in _CARD_TEXT.get(str(scene.get("type")), ()):
        value = scene.get(key)
        if isinstance(value, list):
            found.extend((f"{key}.{index}", str(item)) for index, item in enumerate(value) if item)
        elif value:
            found.append((key, str(value)))
    for index, overlay in enumerate(scene.get("overlays") or []):
        if isinstance(overlay, dict) and overlay.get("type") in _READ_OVERLAYS:
            found.extend(
                (f"overlays.{index}.{key}", str(overlay[key])) for key in ("text", "subtext") if overlay.get(key)
            )
    return found


def _card_marks(scene: dict, template: VideoTemplate, theme: dict) -> list[tuple[str, list[str]]]:
    """``(what draws it, the brand names it spells)`` of every brand mark *scene* draws that spells one."""
    names = brand_names(template)
    by_file = {mark.file_id: mark for mark in template.marks.values()}
    own_id = theme.get("logo_file_id")
    # The spec's own logo is the template's mark when it names one, else a picture with no caption known.
    own = (by_file.get(own_id) or BrandMark(own_id, "logo")) if own_id else None
    drawn = brand_marks(template.marks, light=theme.get("mode") == "light", own_logo=own)

    def spelled(mark: BrandMark | None, alt: object = None) -> list[str]:
        if mark is None or mark.role == "logo_mark":
            return []
        return [name for name in names if names_in(mark.caption, [name]) or names_in(alt, [name])]

    # A statement's logo is not drawn while the watermark is on: it spells nothing then.
    shows_logo = card_draws_logo(scene.get("type"), scene.get("show_logo"), theme.get("watermark"))
    found = [("show_logo", spelled(drawn["logo"]))] if shows_logo else []
    for index, overlay in enumerate(scene.get("overlays") or []):
        if isinstance(overlay, dict) and overlay.get("type") == "image":
            mark = drawn.get(overlay["asset"]) if overlay.get("asset") else by_file.get(overlay.get("file_id"))
            found.append((f"overlays.{index}", spelled(mark, overlay.get("text"))))
    return [(what, names_spelled) for what, names_spelled in found if names_spelled]


def brand_repeats(scene: dict, template: VideoTemplate | None, theme: dict) -> list[BrandRepeat]:
    """Where *scene* (an effective spec's) writes a brand name one of its marks already spells.

    A mark spells the template's brand names (:func:`brand_names`) its
    caption or its alt text writes; the symbol alone (a ``logo_mark``
    asset) spells none, and neither does a logo the template does not
    caption. *theme* is the effective spec's: its colour scheme picks the
    logo variant, its own logo replaces the template's.
    """
    if template is None or not template.marks or scene.get("type") not in _CARD_TEXT:
        return []
    found: list[BrandRepeat] = []
    for what, spelled in _card_marks(scene, template, theme):
        for key, text in _card_texts(scene):
            found.extend(BrandRepeat(name, what, key, text) for name in names_in(text, spelled))
    return found


def _drop_repeated_kickers(effective: dict, template: VideoTemplate, added: list[str], cards: dict) -> None:
    """Leave the template's kicker off a card it adds whose logo already spells it; a kicker the spec sets stays."""
    theme = effective.get("theme") or {}
    for scene in effective.get("scenes") or []:
        if scene.get("id") not in added or not scene.get("kicker") or _sets_kicker(cards, scene):
            continue
        if any(repeat.field == "kicker" for repeat in brand_repeats(scene, template, theme)):
            scene.pop("kicker")


def apply_template(authored: dict, template: VideoTemplate | None) -> AppliedTemplate:
    """The effective spec of *authored* dressed by *template*, or :class:`VideoSpecError`."""
    from app.services.video_studio import pace

    notes: list[str] = []
    added: list[str] = []
    style = template.style if template is not None else None
    # The video's pace presets what the spec leaves unset before the template's defaults do.
    spec = pace.with_defaults(copy.deepcopy(authored), authored.get("pace") or (style.defaults.get("pace") if style else None))
    if template is not None and style is not None:
        name = template.name
        for key, value in style.defaults.items():
            if key == "music" and isinstance(spec.get("music"), dict):
                authored_music = spec["music"]
                if authored_music.get("enabled") is False:
                    continue
                if any(authored_music.get(field) for field in _MUSIC_SOURCES):
                    value = {k: v for k, v in value.items() if k != "track"}
            spec[key] = deep_merge(value, spec[key]) if key in spec else copy.deepcopy(value)

        scenes: list[dict] = spec.setdefault("scenes", [])
        used = {str(scene.get("id")) for scene in scenes if scene.get("id")}
        title = str(spec.get("title") or "")
        # What the author sets on the cards the template adds: those cards are not in the spec.
        cards = spec.get("template_scenes") if isinstance(spec.get("template_scenes"), dict) else {}
        if style.intro is not None and style.intro.required and (not scenes or scenes[0].get("type") != "title"):
            scene = {**_bookend_scene(style, "title", style.intro, title), **_card_settings(cards.get("intro"))}
            scene["id"] = _unique_id("intro", used)
            scenes.insert(0, scene)
            added.append(scene["id"])
        if style.outro is not None and style.outro.required and (not scenes or scenes[-1].get("type") != "outro"):
            scene = {**_bookend_scene(style, "outro", style.outro, title), **_card_settings(cards.get("outro"))}
            scene["id"] = _unique_id("outro", used)
            scenes.append(scene)
            added.append(scene["id"])
        for index, scene in enumerate(scenes):
            if scene.get("id") in added:
                continue
            kind = scene.get("type")
            if kind == "title" and style.intro is not None and index == 0:
                scene.update(deep_merge(_bookend_defaults(style.intro), scene))
            elif kind == "outro" and style.outro is not None and index == len(scenes) - 1:
                scene.update(deep_merge(_bookend_defaults(style.outro), scene))
            looks = dict(style.scene_defaults.get(kind) or {})
            brand = looks.pop("overlays", None) or []
            scene.update(deep_merge(looks, scene))
            # The template's brand marks join the scene's own overlays rather than replace them.
            _with_brand_overlays(scene, brand)
            for overlay in scene.get("overlays") or []:
                overlay.update(deep_merge(style.overlay_defaults.get(overlay.get("type")) or {}, overlay))

        for path in style.locked:
            notes.extend(_enforce_lock(spec, authored, style, path, name))

        ratio = (spec.get("format") or {}).get("aspect_ratio")
        if ratio is None:
            _assign(spec, ["format", "aspect_ratio"], style.aspect_ratios[0])
        elif ratio not in style.aspect_ratios:
            _assign(spec, ["format", "aspect_ratio"], style.aspect_ratios[0])
            notes.append(
                f"The {name} template ships {', '.join(style.aspect_ratios)}: rendered {style.aspect_ratios[0]}, not {ratio}."
            )
    if template is not None and template.supported_schemes:
        mode = (spec.get("theme") or {}).get("mode", "dark")
        if mode not in template.supported_schemes:
            _assign(spec, ["theme", "mode"], template.supported_schemes[0])
            notes.append(
                f"The {template.name} template is {' and '.join(template.supported_schemes)} only: "
                f"rendered {template.supported_schemes[0]}, not {mode}."
            )
    effective = validate_video_spec(spec)
    if template is not None and added:
        cards = authored.get("template_scenes") if isinstance(authored.get("template_scenes"), dict) else {}
        _drop_repeated_kickers(effective, template, added, cards)
    return AppliedTemplate(effective, notes, added, template)


def _bookend_defaults(bookend: Bookend) -> dict:
    """What an author's own opening (closing) card takes from the template's: its look, never its words."""
    return {key: value for key, value in bookend.scene.items() if key in ("style", "align", "show_logo", "duration")}


def template_watermark(template: VideoTemplate | None) -> str | None:
    """The corner *template* draws its mark in on every video that leaves ``theme.watermark`` unset; ``None`` for none."""
    style = template.style if template is not None else None
    corner = ((style.defaults.get("theme") or {}).get("watermark") if style is not None else None) or "none"
    return None if corner == "none" else str(corner)


def watermark_note(authored: dict, effective: dict, template: VideoTemplate | None) -> str | None:
    """What a save says of a video drawing no watermark while its template draws one; ``None`` otherwise.

    The template's corner is a default: it fills ``theme.watermark`` only
    where the spec leaves it unset (:func:`apply_template`), so a spec
    writing ``none`` keeps the video bare - said, with the way back to the
    template's corner, rather than left for the render to show.
    """
    corner = template_watermark(template)
    if corner is None or ((effective.get("theme") or {}).get("watermark") or "none") != "none":
        return None
    written = "watermark" in (authored.get("theme") or {})
    cause = "the spec sets theme.watermark: none" if written else "theme.watermark resolves to none"
    return (
        f"No watermark: {cause}, while the {template.name} template draws its mark {corner.replace('_', ' ')} on "
        "its videos - leave theme.watermark out to take the template's corner, or keep none on purpose."
    )


def _enforce_lock(spec: dict, authored: dict, style: VideoTemplateStyle, path: str, name: str) -> list[str]:
    if path == PALETTE_LOCK:
        theme = spec.get("theme") or {}
        dropped = [key for key in _PALETTE_OVERRIDES if key in theme]
        for key in dropped:
            theme.pop(key)
        if not dropped:
            return []
        return [f"The {name} template sets the colours and the logo: theme.{', theme.'.join(dropped)} not kept."]
    found, value = style.lock_value(path)
    if not found:
        return []
    parts = path.split(".")
    if parts[0] in ("scenes", "overlays") and len(parts) >= 3:
        kind, inner = parts[1], parts[2:]
        changed = False
        # Left to each scene: the open fields this lock's value does not name.
        open_fields = [key for key in LOCK_OPEN_FIELDS.get(path, ()) if isinstance(value, dict) and key not in value]
        authored_scenes = {s.get("id"): s for s in authored.get("scenes") or [] if isinstance(s, dict)}
        for scene in spec.get("scenes") or []:
            targets: list[tuple[dict, dict | None]] = []
            source = authored_scenes.get(scene.get("id"))
            if parts[0] == "scenes" and scene.get("type") == kind:
                targets.append((scene, source))
            elif parts[0] == "overlays":
                source_overlays = (source or {}).get("overlays") or []
                for position, overlay in enumerate(scene.get("overlays") or []):
                    if overlay.get("type") == kind:
                        targets.append(
                            (overlay, source_overlays[position] if position < len(source_overlays) else None)
                        )
            for target, original in targets:
                had, previous = _lookup(original or {}, inner)
                if open_fields and isinstance(previous, dict):
                    previous = {key: v for key, v in previous.items() if key not in open_fields}
                    had = had and bool(previous)
                if had and previous != value:
                    changed = True
                _found, current = _lookup(target, inner)
                kept = {key: current[key] for key in open_fields if isinstance(current, dict) and key in current}
                _assign(target, inner, {**value, **kept} if kept else value)
        if changed:
            return [f"{path} is set by the {name} template ({_shown(value)}): the spec's value was not kept."]
        return []
    had, previous = _lookup(authored, parts)
    _assign(spec, parts, value)
    if had and previous != value:
        return [f"{path} is set by the {name} template ({_shown(value)}): the spec's {_shown(previous)} was not kept."]
    return []


# ── The art direction of AI shots ─────────────────────────────────


def shot_direction(template: VideoTemplate | None) -> dict | None:
    """What a shot made for a video wearing *template* records: the template and its art direction (``None``: free style).

    A template without one is recorded too, with no style and nothing
    avoided, so the shot generated again reads the same template.
    """
    if template is None:
        return None
    shots = template.style.shots if template.style is not None else None
    return {
        "template_id": template.id,
        "template": template.name,
        "style": shots.style if shots else None,
        "avoid": list(shots.avoid) if shots else [],
    }


def merge_negative(negative_prompt: str | None, avoid: Any) -> str | None:
    """*negative_prompt* followed by every avoided term it does not already name."""
    text = (negative_prompt or "").strip()
    known = text.casefold()
    missing: list[str] = []
    for term in avoid if isinstance(avoid, list) else []:
        term = str(term).strip() if isinstance(term, str) else ""
        if term and term.casefold() not in known:
            missing.append(term)
            known += f"\n{term.casefold()}"
    if not missing:
        return text or None
    head = text.rstrip(" ,;.")
    return ", ".join([head, *missing] if head else missing)


def direct_shot(
    prompt: str,
    negative_prompt: str | None,
    direction: Any,
    *,
    start_frame: bool,
    negative_field: bool,
    limit: int,
) -> tuple[str, str | None]:
    """The prompt and negative prompt a video model is sent for a shot made under *direction* (:func:`shot_direction`).

    The style joins a shot the model draws from text; a shot that starts from
    a picture keeps that picture's look. The avoided terms join the negative
    prompt of a model that takes one (:func:`merge_negative`) and are never
    written into a prompt: a model renders what its prompt names, so a model
    with no negative prompt (*negative_field* false) is steered by the style
    alone, and the author's negative prompt reaches it as before. The prompt
    stays within *limit*.
    """
    direction = direction if isinstance(direction, dict) else {}
    style = "" if start_frame else str(direction.get("style") or "").strip()
    negative = merge_negative(negative_prompt, direction.get("avoid")) if negative_field else (negative_prompt or None)
    tail = f"\n\n{style}" if style else ""
    return prompt.strip()[: max(0, limit - len(tail))] + tail, negative


@dataclass(frozen=True)
class ShotUse:
    """An AI shot a video plays, made under a template's art direction: its scene and that template."""

    scene_id: str
    clip_id: str
    #: The id and name of the template the shot was made for.
    directed_by: str
    directed_by_name: str


def shot_notes(template: VideoTemplate | None, uses: list[ShotUse], *, video_id: str) -> list[str]:
    """What a save says of the AI shots a video wearing *template* plays under another template's direction.

    Advice, never a gate: a shot made for another template keeps playing, and
    is named with the call that makes it again in place under this one should
    its look clash. A shot made before shots had a direction, in free style or
    from a picture (*uses* holds none of them) is the author's choice and is
    not named. Nothing for a template without a direction.
    """
    if template is None or template.style is None or template.style.shots is None:
        return []
    stray = [use for use in uses if use.directed_by != template.id]
    if not stray:
        return []
    listed = ", ".join(f"{use.scene_id} (`{use.clip_id}`, {use.directed_by_name})" for use in stray[:6])
    more = f" and {len(stray) - 6} more" if len(stray) > 6 else ""
    return [
        f"AI shots made under another template's art direction play in {listed}{more}. When one clashes with the "
        f"{template.name} look, generate it again in place: generate_video_clip(clip_id=..., video_id='{video_id}')."
    ]


# ── Finding the template of a video ───────────────────────────────────────


async def load_usable_template(db: Any, template_id: object, user: Any) -> Any | None:
    """The template container *user* may dress a video with (three tiers; an admin, any), or ``None``."""
    from sqlalchemy import select

    from app.api.v1.helpers import get_user_group_ids, user_visible_filter
    from app.core.utils import safe_uuid
    from app.models.template_container import TemplateContainer

    tid = template_id if isinstance(template_id, uuid.UUID) else safe_uuid(str(template_id or "").strip())
    if tid is None:
        return None
    groups = await get_user_group_ids(db, user.id)
    result = await db.execute(
        select(TemplateContainer).where(
            TemplateContainer.id == tid,
            user_visible_filter(TemplateContainer, user.id, groups, is_admin=user.role == "admin"),
        )
    )
    return result.scalar_one_or_none()


async def load_video_template(db: Any, container: Any | None) -> VideoTemplate | None:
    """*container* as a video reads it, with the brand marks it draws and their captions (:attr:`VideoTemplate.marks`).

    A mark is the file the palette names for its role (``logo_file_id`` /
    ``logo_light_file_id`` / ``mark_file_id``), else the container's oldest
    image asset of that role: the deck rule (``presentations._fill_role_logos``).
    """
    if container is None:
        return None
    from sqlalchemy import or_, select

    from app.models.file_attachment import FileAttachment
    from app.models.template_container import TEMPLATE_ASSET_SOURCE
    from app.services.presentations import THEME_FILE_ID_KEYS, THEME_FILE_ID_ROLES, sanitize_theme_config

    template = VideoTemplate.from_container(container)
    named = {
        key: str(uuid.UUID(file_id))
        for key, file_id in sanitize_theme_config(container.theme_config, keys=THEME_FILE_ID_KEYS).items()
    }
    rows = (
        await db.execute(
            select(FileAttachment.id, FileAttachment.asset_role, FileAttachment.description)
            .where(
                FileAttachment.template_container_id == container.id,
                FileAttachment.source == TEMPLATE_ASSET_SOURCE,
                FileAttachment.content_type.like("image/%"),
                or_(
                    FileAttachment.asset_role.in_(BRAND_ASSETS),
                    FileAttachment.id.in_([uuid.UUID(file_id) for file_id in named.values()]),
                ),
            )
            .order_by(FileAttachment.created_at.asc())
        )
    ).all()
    assets = [BrandMark(str(asset_id), str(role or ""), str(description or "")) for asset_id, role, description in rows]
    for key, role in THEME_FILE_ID_ROLES.items():
        wanted = named.get(key)
        mark = next((a for a in assets if (a.file_id == wanted if wanted else a.role == role)), None)
        if mark is not None:
            template.marks[role] = mark
    return template


async def default_template(db: Any) -> Any | None:
    """The platform's default video template (:data:`DEFAULT_VIDEO_TEMPLATE_SLUGS`), or ``None``."""
    from sqlalchemy import select

    from app.models.template_container import TemplateContainer
    from app.services.template_container_seed import builtin_container_filter
    from app.services.template_containers import VIDEO_KIND

    for slug in DEFAULT_VIDEO_TEMPLATE_SLUGS:
        result = await db.execute(select(TemplateContainer).where(builtin_container_filter(slug)).limit(1))
        row = result.scalar_one_or_none()
        if row is not None and str(row.kind or "").lower() == VIDEO_KIND:
            return row
    return None


def keep_look(authored: dict, stored: dict | None) -> dict:
    """*authored*, still in free style when it rewrites a free-style video and names no look of its own.

    A spec naming neither ``template_id`` nor ``freestyle`` keeps the look of
    the video it replaces (*stored*, its spec as written): a template is kept
    by :func:`resolve_for_spec`, free style here, so a whole new spec never
    dresses a free-style video in the platform default.
    """
    if not (stored or {}).get("freestyle") or "freestyle" in authored or authored.get("template_id"):
        return authored
    return {**authored, "freestyle": True}


async def resolve_for_spec(db: Any, user: Any, authored: dict, *, current_id: uuid.UUID | None) -> Any | None:
    """The template container a saved spec wears, or :class:`VideoSpecError`.

    ``freestyle`` wears none; a ``template_id`` must name a video or a
    presentation template the author may use; otherwise the project keeps the
    template it wears (when still usable, free style: :func:`keep_look`), and a
    new one gets the default.
    """
    from app.services.template_containers import PRESENTATION_KIND, VIDEO_KIND

    if authored.get("freestyle"):
        return None
    requested = authored.get("template_id")
    if requested:
        row = await load_usable_template(db, requested, user)
        if row is None:
            raise VideoSpecError(
                [
                    SpecProblem(
                        "template_id",
                        "no template you may use has this id (video_studio_capabilities lists the video templates)",
                    )
                ]
            )
        if str(row.kind or "").lower() not in (VIDEO_KIND, PRESENTATION_KIND):
            raise VideoSpecError(
                [SpecProblem("template_id", f"{row.name} is a {row.kind} template: a video wears a video template")]
            )
        return row
    if current_id is not None:
        row = await load_usable_template(db, current_id, user)
        if row is not None:
            return row
    return await default_template(db)


async def list_video_templates(db: Any, user: Any) -> list[Any]:
    """The video templates *user* may pick (the picker's three tiers), the default first."""
    from sqlalchemy import func, select

    from app.api.v1.helpers import get_user_group_ids, user_visible_filter
    from app.models.template_container import TemplateContainer
    from app.services.template_containers import VIDEO_KIND

    groups = await get_user_group_ids(db, user.id)
    result = await db.execute(
        select(TemplateContainer)
        .where(
            func.lower(TemplateContainer.kind) == VIDEO_KIND,
            user_visible_filter(TemplateContainer, user.id, groups, is_admin=False),
        )
        .order_by(TemplateContainer.name)
        .limit(200)
    )
    rows = list(result.scalars().all())
    default = await default_template(db)
    if default is not None:
        rows.sort(key=lambda row: row.id != default.id)
    return rows


def template_summary(container: Any, *, is_default: bool = False) -> dict:
    """A video template as the picker and the agent read it: what it presets, adds and locks."""
    template = VideoTemplate.from_container(container)
    style = template.style
    palette = container.theme_config if isinstance(container.theme_config, dict) else {}
    swatch_keys = ("background", "surface", "foreground", "accent", "gradient_from", "gradient_to")
    return {
        "id": template.id,
        "name": template.name,
        "description": template.description,
        "kind": template.kind,
        "is_default": is_default,
        "is_builtin": template.is_builtin,
        "supported_schemes": template.supported_schemes,
        "aspect_ratios": list(style.aspect_ratios) if style else list(ASPECT_RATIOS),
        "purposes": list(style.purposes) if style else [],
        "locked": list(style.locked) if style else [],
        "intro": bool(style and style.intro and style.intro.required),
        "outro": bool(style and style.outro and style.outro.required),
        "background": ((style.defaults.get("theme") or {}).get("background") if style else None),
        # How its videos move: the personality it presets, its whole motion for a tool reading it.
        "motion": ((style.defaults.get("theme") or {}).get("motion") if style else None),
        "watermark": template_watermark(template),
        "shots": style.shots.model_dump(mode="json", exclude_none=True) if style and style.shots else None,
        "guidance": template.guidance,
        "swatches": [palette[key] for key in swatch_keys if isinstance(palette.get(key), str)],
        "series": [palette[f"chart_{i}"] for i in range(1, 7) if isinstance(palette.get(f"chart_{i}"), str)],
        "heading_font": palette.get("heading_font") if isinstance(palette.get("heading_font"), str) else None,
    }
