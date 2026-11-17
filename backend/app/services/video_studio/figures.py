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

"""Figure scenes: one block of a deck, drawn on the video template's palette and built in.

A figure is drawn by the render once the timeline is planned - its length
and its narration's place are what its build is timed on - and before the
frames (``render.run_render``). :func:`draw` opens the figure page
(``/videos/figures/static``: the deck's own ``StaticSlide`` holding the block,
``frontend/src/pages/VideoFigureStaticRender.tsx``) in the export's
locked-down headless browser (``presentation_export.open_render_page``) and
answers the page's two requests from this process: the one-slide deck
(:func:`figure_document`) and the pictures the block draws, each one
readable by the person the render is for - a template asset the slide places
for the other scheme drawn as its twin for the video's (``figure_twins``,
A light deck's logos on a dark video). It then plans the page's build
(``figure-build.ts``), seeks it to every frame where something moves and
screenshots it - a span where nothing moves is one frame - and the frames
become a constant-rate video (``encoder.frames_to_mezzanine``) the compositor
plays like a shot. Each frame drawn renews the render's lease. A
figure is drawn once per key - what its frames are drawn from
(:func:`render_key`) -: a render resumed in its working directory
keeps what a former attempt drew, and every render and preview of the same
key takes the frames and the video another drew (``figure_cache``: on the
worker, and stored beside the video). A scene preview draws a neighbour's
figure only for the frames its window shows (``span``).

When each step comes in is computed here (:func:`reveal_times`):
step k of N at k equal shares of the scene before its last
:data:`WHOLE_HOLD` seconds, or - ``reveal: narration`` - as the narration
says its words (``speech_timing``: measured on the voice, else paced), the
steps it does not name spread evenly between, before and after those it
does. A subtitle shows its whole phrase from the phrase's start, so a step
named in it comes in by then at the latest (:func:`shown_from`): the
picture never lags the caption that names it. A label is found however it
is written in the narration: in
any case, its punctuation, a possessive or a plural aside, a compound in
one word or two, a part of it alone ("OpenCTI" for "OpenCTI - Threat
intelligence"), a word spelled close to it - and the steps are matched in
their order, as many as the narration names. A step stays on screen its
scene's ``dwell`` once it is in; steps the scene has no room to show one by
one come in together, a few at a time, never all at the end. A figure keeps
clear of the subtitles' band (:func:`caption_safe_area`). Built, a figure
keeps moving to the end of its scene (``motion: drift``, ``figure-build.ts``),
its drift capped so the figure, drifted and stood in space, stays inside the
frame (``figure_fit``: :func:`plan_fit`), said by a save
(:func:`fit_lines`).

The editor's preview shows a figure's still - the figure built in whole -
drawn by the worker once the video is saved, or once a preview finds it missing
(``figure_stills``: never in the request, the render's memory ceiling
over it), and kept in the
object store under the digest of what it shows (:func:`preview_digest`: the
block, the palette, the scheme, the size, the band it keeps clear), so an
unchanged figure is never drawn twice and a changed one gets a new still. The
preview finds it by computing the same digest as its reader
(:func:`figure_previews`): nothing the spec says names a stored file. The
steps the page read of the figure are kept beside its still, so a save says
when each comes in (:func:`reveal_lines`) without drawing it again. Once its
still is shown, the preview plays the figure's build: drawn in the
background for the scene as the preview times it (:func:`prepare_builds`),
lighter than a render's, kept under the digest of the still and of that
timing (:func:`build_digest`).
"""

from __future__ import annotations

import asyncio
import dataclasses
import difflib
import itertools
import json
import logging
import math
import re
import uuid
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from app.core.utils import format_exception_message
from app.services.video_studio import figure_fit
from app.services.video_studio.spec import DEFAULT_FIGURE_DWELL, figure_file_ids
from app.services.video_studio.speech_timing import SpokenWord, tokens

logger = logging.getLogger(__name__)

#: The figure page's path, under the platform's frontend.
FIGURE_PAGE_PATH = "/videos/figures/static"
#: The deck format a figure is laid out in: the one blocks are designed for.
FIGURE_DECK_FORMAT = "16:9"
#: Where the first step comes in (seconds), past the scene's transition in.
FIRST_STEP_AT = 0.35
#: How long a scene ends on its whole figure: every step in, the camera back on all of it (``figure-build.ts``).
WHOLE_HOLD = 2.0
#: Between two steps of a figure built ``together``.
TOGETHER_GAP = 0.15
#: How early a step comes in before the narration says its words.
NARRATION_LEAD = 0.15
#: How long a step comes in for when the page does not say (``figure-build.ts``: ``PIECE_SECONDS``).
DEFAULT_SETTLE = 0.6
#: A label, or a part of one, looked for in the narration: no shorter than this, written as one word.
_MIN_PHRASE = 4
#: A word of a label looked for alone: no shorter than this - one written like a name (``OpenCTI``,
#: ``SIEM``, ``T1566``) no shorter than :data:`_MIN_NAME` - and in no other step's labels.
_MIN_WORD = 5
_MIN_NAME = 3
#: The most words of the narration one label is read across ("Open CTI" for "OpenCTI").
_MAX_PHRASE = 6
#: How alike a label and the narration's words must be spelled to match (``difflib``'s ratio), from this many letters.
_NEAR = 0.84
_MIN_NEAR = 6
#: How well a match names its step: the whole label, a part of it, a word written like a name, another word.
_WHOLE, _PART, _NAME, _WORD = 3.0, 2.5, 2.0, 1.0
#: A near spelling names its step a little less than the label written out.
_NEAR_WEIGHT = 0.8
#: What separates the parts of a label: "OpenCTI - Threat intelligence", "XTM One (Agentic core)", "Collect / enrich".
_PARTS = re.compile(r"\s+[-\u2013\u2014]\s+|[:|/(),;\u00b7\u2022+&]")
_LABEL_WORD = re.compile(r"[\w'\u2019-]+")
#: Capitals inside a word, a word all in capitals, or a digit: a name more than a word.
_NAME_LIKE = re.compile(r"[a-z][A-Z]|^[A-Z0-9]{2,}$|\d")


class FigureError(RuntimeError):
    """A figure that cannot be drawn; ``str()`` names why, for the person."""


class FigurePageError(FigureError):
    """The figure page failed twice on fresh pages while the figure itself was valid (its slide and block read):
    its code not loading, its drawing failing, its Chromium gone. The platform's failure, not the video's: the render
    says it as its Chromium's and runs again once (``render_retry``), never "fix what its reason names"."""


@dataclass(frozen=True)
class FigureSource:
    """What a figure scene draws: one slide holding its block, and the pictures that block may show."""

    #: The block's type, or ``roadmap`` for a roadmap slide's phases: which of its parts build in.
    kind: str
    slide: dict
    #: The deck the block comes from: its pictures are read through it.
    presentation_id: uuid.UUID | None
    files: frozenset[str]
    #: The template assets of its slide drawn as their twins for the video's scheme (``figure_twins.Twin``).
    twins: tuple[Any, ...] = ()


def _slide_of(deck: dict, scene: Any) -> tuple[int, dict]:
    slides = list(deck.get("slides") or [])
    if scene.slide_id:
        index = next((i for i, s in enumerate(slides) if s.get("id") == scene.slide_id), -1)
    else:
        index = (scene.slide or 1) - 1
    if not 0 <= index < len(slides):
        raise FigureError(f"Scene {scene.id}: the deck has no slide {scene.slide_id or scene.slide}.")
    return index, slides[index]


def _blocks_of(slide: dict) -> list[dict]:
    """The blocks of a slide, in the order a figure's ``block`` counts them."""
    if slide.get("layout") == "dashboard":
        return [panel.get("block") for panel in slide.get("panels") or [] if isinstance(panel.get("block"), dict)]
    if slide.get("columns"):
        return [block for column in slide["columns"] for block in column if isinstance(block, dict)]
    return [block for block in slide.get("blocks") or [] if isinstance(block, dict)]


def source_from_deck(scene: Any, deck: dict, presentation_id: uuid.UUID) -> FigureSource:
    """The figure a scene takes from a deck: its slide's block, or a roadmap slide's phases."""
    index, slide = _slide_of(deck, scene)
    if slide.get("layout") == "roadmap":
        roadmap = {k: v for k, v in slide.items() if k not in ("footer_block", "theme", "id")}
        return FigureSource("roadmap", roadmap, presentation_id, frozenset(figure_file_ids(slide.get("phases"))))
    blocks = _blocks_of(slide)
    if not blocks:
        raise FigureError(
            f"Scene {scene.id}: slide {index + 1} is a {slide.get('layout')} slide: a figure is a block of a "
            "content or dashboard slide, or a roadmap slide's phases."
        )
    if scene.block >= len(blocks):
        raise FigureError(
            f"Scene {scene.id}: slide {index + 1} has {len(blocks)} block(s); block {scene.block} does not exist "
            "(blocks are counted from 0)."
        )
    block = blocks[scene.block]
    return FigureSource(str(block.get("type")), _block_slide(block), presentation_id, frozenset(figure_file_ids(block)))


def source_from_spec(scene: Any) -> FigureSource:
    """The figure a scene writes itself (``block_spec``, validated with the scene)."""
    block = scene.block_spec or {}
    return FigureSource(str(block.get("type")), _block_slide(block), None, frozenset(figure_file_ids(block)))


def _block_slide(block: dict) -> dict:
    return {"layout": "content", "blocks": [block], "hide_footer": True}


def figure_document(source: FigureSource, *, scheme: str, palette: dict, asset_meta: dict | None = None) -> dict:
    """What the figure page reads (``spec.json``): a one-slide deck on *palette*, in the shape of a deck read.

    The slide goes through the deck's own validator, so the page renders the
    same normalised block a deck would; the chrome is off.
    """
    from app.services.presentation_spec import PresentationSpecError, validate_spec

    try:
        spec = validate_spec(
            {
                "title": "Figure",
                "theme": scheme,
                "aspect_ratio": FIGURE_DECK_FORMAT,
                "chrome": {"show_logo": False, "show_footer": False, "show_slide_numbers": False},
                "slides": [source.slide],
            }
        )
    except PresentationSpecError as exc:
        raise FigureError(f"The figure's block is not a block a deck can draw: {str(exc).splitlines()[0]}") from None
    themes = {scheme: dict(palette or {})}
    return {
        "kind": source.kind,
        "presentation": {
            "id": str(uuid.uuid4()),
            "title": "Figure",
            "theme": scheme,
            "aspect_ratio": FIGURE_DECK_FORMAT,
            "kind": "deck",
            "slide_count": 1,
            "revision": 1,
            "spec": spec,
            "theme_config": themes[scheme],
            "theme_configs": themes,
            "template_chrome": None,
            "asset_meta": asset_meta or {},
            "picture_labels": {},
        },
    }


# ── When each step comes in ───────────────────────────────────────────────


def _key(words: list[str]) -> str:
    """How *words* are compared: lower-cased, run together without their punctuation, a possessive or a plural.

    "XTM One", "XTM-One" and "xtm one's" share one key, "OpenCTI" and "Open
    CTI" too, "connectors" and "connector".
    """
    key = ""
    for word in words:
        word = word.lower().replace("\u2019", "'")
        key += re.sub(r"[\W_]+", "", word.removesuffix("'s"))
    for plural, single in (("ies", "y"), ("sses", "ss"), ("ches", "ch"), ("shes", "sh"), ("xes", "x")):
        if len(key) > len(plural) + 2 and key.endswith(plural):
            return key[: -len(plural)] + single
    if len(key) > 3 and key.endswith("s") and not key.endswith("ss"):
        return key[:-1]
    return key


@dataclass(frozen=True)
class _Cue:
    """What names a step in its narration: a key (:func:`_key`), how many words it was written in, how well it names it."""

    key: str
    words: int
    weight: float


def _cues(labels: list[str], shared: set[str]) -> list[_Cue]:
    """The cues of one step: each label whole, each part of it, and its words that name it alone.

    A word in *shared* (another step's labels have it too) names no step alone.
    """
    cues: dict[str, _Cue] = {}

    def add(words: list[str], weight: float, shortest: int) -> None:
        key = _key(words)
        if len(key) >= shortest and (key not in cues or cues[key].weight < weight):
            cues[key] = _Cue(key, len(words), weight)

    for label in labels:
        words = _LABEL_WORD.findall(label)
        add(words, _WHOLE, _MIN_PHRASE)
        parts = [part for part in _PARTS.split(label) if part.strip()]
        if len(parts) > 1:
            for part in parts:
                add(_LABEL_WORD.findall(part), _PART, _MIN_PHRASE)
        for word in words:
            if _key([word]) in shared:
                continue
            if _NAME_LIKE.search(word):
                add([word], _NAME, _MIN_NAME)
            else:
                add([word], _WORD, _MIN_WORD)
    return list(cues.values())


@dataclass(frozen=True)
class _Said:
    """Where the narration names a step: its first word, how many words, how well, and what it says."""

    first: int
    width: int
    weight: float
    text: str


def _mentions(
    cue: _Cue, windows: list[tuple[int, int, str]], index: dict[str, list[tuple[int, int]]], said: list[str]
) -> list[_Said]:
    """Every place the narration says *cue*: written alike (:func:`_key`), else spelled close to it."""
    found = [
        _Said(first, width, cue.weight, " ".join(said[first : first + width]))
        for first, width in index.get(cue.key, [])
        if width <= cue.words + 1
    ]
    if found or len(cue.key) < _MIN_NEAR:
        return found
    for first, width, key in windows:
        if width > cue.words + 1 or key[:1] != cue.key[:1] or abs(len(key) - len(cue.key)) > len(cue.key) * 0.2:
            continue
        if difflib.SequenceMatcher(None, key, cue.key).ratio() >= _NEAR:
            found.append(_Said(first, width, cue.weight * _NEAR_WEIGHT, " ".join(said[first : first + width])))
    return found


def _align(candidates: list[list[_Said]]) -> dict[int, _Said]:
    """The steps the narration names, each at most once and each after the one before: as many as it can, the best named, the earliest.

    One step matched on a word said late never keeps the steps after it from
    being matched before: the steps are aligned on the narration as a whole.
    """
    # (step, said, its best total with the steps before it, the entry it follows)
    entries: list[tuple[int, _Said, float, int]] = []
    for step, said in enumerate(candidates):
        for mention in sorted(said, key=lambda m: m.first):
            # Every match counts more than any match is better than another: the most steps named, then the best.
            own = 10.0 + mention.weight - mention.first * 1e-6
            best, back = own, -1
            for k, (other_step, other, total, _back) in enumerate(entries):
                if other_step < step and other.first + other.width <= mention.first and total + own > best:
                    best, back = total + own, k
            entries.append((step, mention, best, back))
    if not entries:
        return {}
    k = max(range(len(entries)), key=lambda i: entries[i][2])
    chosen: dict[int, _Said] = {}
    while k >= 0:
        step, mention, _total, k = entries[k]
        chosen[step] = mention
    return chosen


@dataclass(frozen=True)
class Reveal:
    """When one step comes in, and what the narration says there (``None``: spread between the steps it names)."""

    at: float
    said: str | None = None
    #: It comes in with the subtitle that shows its words, before the voice says them (:func:`shown_from`).
    subtitled: bool = False


#: A caption cue on a scene's clock: ``(start, end, phrase)`` (``timeline.TimelinePlan.captions``).
Caption = tuple[float, float, str]


def shown_from(words: list[SpokenWord], captions: Sequence[Caption]) -> list[float | None]:
    """When the subtitle showing each of *words* comes on screen; ``None`` for a word no subtitle shows.

    A cue shows its whole phrase from its start (``timeline.caption_cues``),
    so each word is shown from the start of the cue holding it. The cues'
    words are aligned on the narration's (``difflib``): the captions may
    write a word otherwise than the voice says it (a lexicon entry applied to
    the captions), and a word they do not write takes the cue before it.
    """
    shown = [(token, float(start)) for start, _end, text in captions for token in tokens(text)]
    out: list[float | None] = [None] * len(words)
    if not shown:
        return out
    matcher = difflib.SequenceMatcher(
        None, [word.text for word in words], [token for token, _ in shown], autojunk=False
    )
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        for i in range(i1, i2):
            j = j1 + (i - i1) * (j2 - j1) // (i2 - i1) if tag != "delete" else j1 - 1
            out[i] = shown[min(max(j, 0), len(shown) - 1)][1]
    return out


def _narrated(
    labels: list[list[str]],
    words: list[SpokenWord],
    *,
    even: list[float],
    end: float,
    need: float,
    captions: Sequence[Caption] = (),
) -> tuple[list[float], dict[int, str], set[int]]:
    """Each step as the narration names it, the others spread evenly around: ``(times, step -> what is said, steps
    brought in by their subtitle)``.

    A step named comes in :data:`NARRATION_LEAD` before its word is heard or
    its subtitle shown (*captions*, :func:`shown_from`), whichever is first.
    A step is named only while it and the steps after it still have *need*
    seconds each before *end*: a step named late never pushes the rest into
    the scene's last seconds. Named in no step: *even*.
    """
    count = len(labels)
    said = [word.text for word in words]
    shown = shown_from(words, captions)
    windows = [
        (first, width, _key(said[first : first + width]))
        for first in range(len(said))
        for width in range(1, min(_MAX_PHRASE, len(said) - first) + 1)
    ]
    index: dict[str, list[tuple[int, int]]] = {}
    for first, width, key in windows:
        index.setdefault(key, []).append((first, width))
    seen: dict[str, set[int]] = {}
    for step, step_labels in enumerate(labels):
        for label in step_labels:
            for word in _LABEL_WORD.findall(label):
                seen.setdefault(_key([word]), set()).add(step)
    shared = {key for key, steps in seen.items() if len(steps) > 1}

    def named_at(mention: _Said) -> float:
        heard, read = words[mention.first].start, shown[mention.first]
        return heard if read is None else min(heard, read)

    def at(mention: _Said) -> float:
        # Past the scene's transition in, unless it is named before.
        named = named_at(mention)
        return max(min(FIRST_STEP_AT, named), named - NARRATION_LEAD)

    candidates: list[list[_Said]] = []
    for step, step_labels in enumerate(labels):
        # Where the narration names the step, the best cue said there.
        best: dict[int, _Said] = {}
        for cue in _cues(step_labels, shared):
            for mention in _mentions(cue, windows, index, said):
                if mention.first not in best or mention.weight > best[mention.first].weight:
                    best[mention.first] = mention
        candidates.append([m for m in best.values() if at(m) <= end - (count - step) * need + 1e-6])
    chosen = _align(candidates)
    if not chosen:
        return list(even), {}, set()
    anchors = {step: at(mention) for step, mention in chosen.items()}
    # The scene's start and its end stand as two more marks: every other step is spread evenly between two marks.
    marks = sorted({0: min(FIRST_STEP_AT, *anchors.values()), **anchors, count: end}.items())
    times: list[float] = []
    for (a, at_a), (b, at_b) in itertools.pairwise(marks):
        times.extend(at_a + (step - a) * (at_b - at_a) / (b - a) for step in range(a, b))
    subtitled = {step for step, mention in chosen.items() if named_at(mention) < words[mention.first].start - 1e-6}
    return times, {step: mention.text for step, mention in chosen.items()}, subtitled


def _beats(times: list[float], settles: list[float], dwell: float, named: set[int]) -> list[float]:
    """*times* made readable: a step comes in with the one before it when it would cut that one's dwell short.

    A step named by the narration starts its own beat; the others join the
    beat they fall in, so steps the scene has no room for come in a few at a
    time, each beat staying on screen once it is in.
    """
    out: list[float] = []
    beat_at = beat_until = float("-inf")
    for step, (at, settle) in enumerate(zip(times, settles, strict=True)):
        if step not in named and at < beat_until - 1e-6:
            out.append(beat_at)
            beat_until = max(beat_until, beat_at + settle + dwell)
            continue
        beat_at, beat_until = at, at + settle + dwell
        out.append(at)
    return out


def reveals(
    labels: list[list[str]],
    *,
    length: float,
    mode: str = "even",
    together: bool = False,
    dwell: float = DEFAULT_FIGURE_DWELL,
    settle: list[float] | None = None,
    words: list[SpokenWord] | None = None,
    captions: Sequence[Caption] = (),
) -> list[Reveal]:
    """When each step comes in, seconds into a scene of *length*, and what the narration says there: one per step, in order.

    *labels* are each step's words and *settle* how long each takes to come
    in, as the page reads them. ``even``: step k of N at
    ``FIRST_STEP_AT + k * slot``, the slot an equal share of the scene before
    its last :data:`WHOLE_HOLD` seconds. ``narration`` (*words*: when each
    word of the narration is said, on the scene's clock) brings a step in as
    its words are said - or as the subtitle showing them comes on screen
    when that is first (*captions*, on the scene's clock) -, the others
    spread evenly between, before and after.
    A step stays its *dwell* on screen once in; steps with no room for it come
    in together (:func:`_beats`). ``together`` brings every step in at once,
    a beat apart.
    """
    count = len(labels)
    if count == 0:
        return []
    end = max(FIRST_STEP_AT, length - WHOLE_HOLD)
    if together:
        return [Reveal(round(min(end, FIRST_STEP_AT + i * TOGETHER_GAP), 4)) for i in range(count)]
    settles = [max(0.0, float(s)) for s in (settle or [])][:count]
    settles += [DEFAULT_SETTLE] * (count - len(settles))
    dwell = max(0.0, dwell)
    slot = (end - FIRST_STEP_AT) / count
    times: list[float] = [FIRST_STEP_AT + step * slot for step in range(count)]
    named: dict[int, str] = {}
    subtitled: set[int] = set()
    if mode == "narration" and words:
        need = min(slot, max(settles) + dwell)
        times, named, subtitled = _narrated(labels, words, even=times, end=end, need=need, captions=captions)
    beats = _beats(times, settles, dwell, set(named))
    return [Reveal(round(t, 4), named.get(step), step in subtitled) for step, t in enumerate(beats)]


def reveal_times(labels: list[list[str]], **kwargs: Any) -> list[float]:
    """When each step comes in (:func:`reveals`), seconds into its scene: one time per step, in order."""
    return [reveal.at for reveal in reveals(labels, **kwargs)]


def narrated_hold(dwell: float = DEFAULT_FIGURE_DWELL) -> float:
    """How long a figure built on its narration lasts past it (``timeline.plan``).

    Its last step named comes in, stays its *dwell*, then the whole figure
    holds: :func:`_narrated` follows a step only with that room before the
    hold, so a step the narration names last is built when it is said.
    """
    return DEFAULT_SETTLE + max(0.0, dwell) + WHOLE_HOLD


def frame_times(active: list[list[float]], length: float, fps: int) -> list[float]:
    """The frames a figure is screenshot at: every one where something moves, and the first still one after.

    A span where nothing moves is that one frame, held by the video until the
    next (``encoder.frames_to_mezzanine``).
    """
    count = max(1, math.ceil(length * fps))
    margin = 1.0 / fps
    times: list[float] = []
    moving_before = True
    for k in range(count):
        t = k / fps
        moving = any(a - margin <= t <= b + margin for a, b in active)
        if moving or moving_before:
            times.append(round(t, 6))
        moving_before = moving
    return times


# ── Drawing ───────────────────────────────────────────────────────────────


def figure_page_url() -> str:
    from app.services.presentation_export import render_base_url

    return f"{render_base_url()}{FIGURE_PAGE_PATH}"


#: A figure drawn with nothing to keep clear of.
NO_SAFE_AREA = {"top": 0.0, "bottom": 0.0}
#: The share of the frame's height a figure keeps clear at the edge its subtitles leave free.
SAFE_MARGIN = 0.04


def caption_safe_area(spec: Any, scene: Any, height: int) -> dict[str, float]:
    """What a figure scene keeps clear at the top and the bottom of its frame, in design pixels: the band it is
    centred in. None when the scene says nothing.

    The subtitles of a scene are its narration's (``timeline.caption_cues``),
    drawn burned in or by the player in the same place. A scene with subtitles
    keeps clear of the tallest pill the page draws there
    (``compositor_page.caption_extent``) and of :data:`SAFE_MARGIN` at the
    other edge: the figure is centred between the top margin and the
    caption pill's top, never in the room above a reserve with a gap and two
    lines, which left the band under it empty.
    """
    from app.services.video_studio.compositor_page import caption_extent

    if not spec.captions.enabled or not (scene.narration or "").strip():
        return dict(NO_SAFE_AREA)
    caption = caption_extent(height, style=spec.captions.style, position=spec.captions.position)
    margin = round(height * SAFE_MARGIN, 1)
    if spec.captions.position == "top":
        return {"top": caption, "bottom": margin}
    return {"top": margin, "bottom": caption}


def _reveals_of(
    described: dict,
    *,
    scene: Any,
    length: float,
    words: list[SpokenWord] | None,
    captions: Sequence[Caption] = (),
) -> list[Reveal]:
    """When each step of the figure the page *described* comes in, in *scene* of *length* (:func:`reveals`)."""
    steps = described.get("steps") or []
    return reveals(
        [list(step.get("labels") or []) for step in steps],
        length=length,
        mode=scene.reveal,
        together=scene.build == "together",
        dwell=scene.dwell,
        settle=[float(step.get("settle") or DEFAULT_SETTLE) for step in steps],
        words=words,
        captions=captions,
    )


def _camera(scene: Any) -> str:
    # Built together, every step is in within a second: there is nothing to follow.
    return "still" if scene.build == "together" else scene.camera


def _rect(value: object) -> figure_fit.Rect | None:
    """A rect the figure page described (``{x, y, w, h}``, canvas px), or ``None``."""
    if not isinstance(value, dict):
        return None
    try:
        rect = (float(value["x"]), float(value["y"]), float(value["w"]), float(value["h"]))
    except (KeyError, TypeError, ValueError):
        return None
    return rect if rect[2] > 0 and rect[3] > 0 else None


def plan_fit(
    described: dict,
    times: Sequence[float],
    *,
    scene: Any,
    kind: str,
    length: float,
    safe: dict,
    viewport: tuple[int, int],
    ease: str = figure_fit.DEFAULT_EASE,
) -> figure_fit.FigureFit | None:
    """The drift the figure the page *described* is drawn with in *scene* (``figure_fit.fit``): judged from
    once it is built, the camera on all of it, to its last frame, drifted and stood in space as it is drawn - its
    steps at *times*, the stand eased by the brand's *ease*. ``None`` when the page described no figure."""
    rect = _rect(described.get("figure"))
    if rect is None:
        return None
    steps = described.get("steps") or []
    boxes = [box for step in steps if (box := _rect(step.get("box"))) is not None]
    settles = [float(step.get("settle") or DEFAULT_SETTLE) for step in steps]
    # A chart's steps stand on one another (``FIGURE_PARTS.chart.whole``): its camera keeps the whole figure.
    follows = figure_fit.camera_follows(_camera(scene), boxes, length, WHOLE_HOLD, whole=kind == "chart")
    return figure_fit.fit(
        rect,
        viewport=viewport,
        safe=safe,
        perspective=getattr(scene, "perspective", "flat"),
        eased=ease,
        length=length,
        start=figure_fit.drift_start(times, settles, length, follows=follows, hold=WHOLE_HOLD),
        motion=scene.motion,
    )


def _plan_with(
    times: list[float], *, scene: Any, length: float, safe: dict, fit: figure_fit.FigureFit | None = None
) -> dict:
    """What the figure page plans the scene with (``figure-build.ts``): each step's time, the camera, the hold, the
    band, the motion - and the drift *fit* allows, holding still when it allows none worth seeing."""
    plan = {
        "times": times,
        "length": length,
        "camera": _camera(scene),
        "hold": WHOLE_HOLD,
        "safe": safe,
        "motion": scene.motion,
    }
    if fit is not None and scene.motion == "drift":
        plan.update({"motion": fit.motion, "drift": fit.drift})
    return plan


def _plan_of(
    described: dict,
    *,
    scene: Any,
    length: float,
    words: list[SpokenWord] | None,
    safe: dict,
    captions: Sequence[Caption] = (),
    kind: str = "",
    viewport: tuple[int, int] | None = None,
    ease: str = figure_fit.DEFAULT_EASE,
) -> dict:
    """The page's plan of *scene* (:func:`_plan_with`), its steps timed by :func:`reveals`, its drift fitted to the
    frame of *viewport*; without one, the drift its motion asks."""
    resolved = _reveals_of(described, scene=scene, length=length, words=words, captions=captions)
    times = [reveal.at for reveal in resolved]
    fit = (
        plan_fit(described, times, scene=scene, kind=kind, length=length, safe=safe, viewport=viewport, ease=ease)
        if viewport is not None
        else None
    )
    return _plan_with(times, scene=scene, length=length, safe=safe, fit=fit)


def reveal_line(scene: Any, described: dict, resolved: list[Reveal], *, length: float, paced: bool) -> str:
    """When each step of *scene*'s figure comes in, in words: what a save says, and the render logs.

    *paced*: its words were paced over the narration's planned span, not
    measured on its voice (the render measures them once it is spoken).
    """
    steps = described.get("steps") or []
    names = [((step.get("labels") or ["step"])[0] or "step")[:48] for step in steps]
    narrated = scene.reveal == "narration" and scene.build != "together"
    parts = []
    for name, reveal in zip(names, resolved, strict=False):
        shown = ", with its subtitle" if reveal.subtitled else ""
        said = f' (said "{reveal.said}"{shown})' if reveal.said else " (spread)" if narrated else ""
        parts.append(f'"{name}" {reveal.at:.2f} s{said}')
    how = "on its narration" if narrated else "together" if scene.build == "together" else "evenly"
    timed = (
        " - timed on the narration's planned pace; the render times it on the spoken voice"
        if narrated and paced
        else ""
    )
    return (
        f"Scene {scene.id}: its figure's {len(parts)} steps come in {how} over {length:.1f} s{timed}: "
        + ", ".join(parts)
        + "."
    )


async def _picture(source: FigureSource, user: Any, file_id: str) -> tuple[bytes, str] | None:
    """A picture the block draws, as the person the render is for may read it; ``None`` otherwise."""
    from app.core.database import async_session
    from app.services.file_access import load_readable_file
    from app.services.file_storage import file_storage

    if file_id not in source.files:
        return None
    try:
        async with async_session() as db:
            row = await load_readable_file(db, file_id, user, presentation_id=source.presentation_id)
        return await file_storage.download(row.s3_object_name), row.content_type or "application/octet-stream"
    except Exception:  # any refusal or a gone file: the picture is missing, the block draws without it
        return None


async def readable_pictures(source: FigureSource, user: Any) -> frozenset[str]:
    """The pictures of *source* the person may read: what a figure drawn for them shows, part of its key."""
    from app.core.database import async_session
    from app.services.file_access import load_readable_file

    if not source.files or user is None:
        return frozenset()
    found: set[str] = set()
    async with async_session() as db:
        for file_id in sorted(source.files):
            try:
                await load_readable_file(db, file_id, user, presentation_id=source.presentation_id)
            except Exception as exc:  # unreadable answers like unknown: drawn without it
                logger.debug(
                    "Figure picture %s not readable by the render's reader: %s", file_id, format_exception_message(exc)
                )
            else:
                found.add(file_id)
    return frozenset(found)


def _answerer(
    source: FigureSource, document: dict, user: Any, missed: set[str] | None = None
) -> Callable[[Any], Awaitable[bool]]:
    """What the figure page asks this process: its one-slide deck, and the pictures the person may read.

    A picture the page asked and did not get is added to *missed*.
    """
    spec_json = json.dumps(document).encode("utf-8")

    async def answer(route: Any) -> bool:
        path = route.request.url.split("?", 1)[0]
        if "/__figure__/" not in path:
            return False
        tail = path.split("/__figure__/", 1)[1]
        if tail == "spec.json":
            await route.fulfill(status=200, body=spec_json, content_type="application/json")
            return True
        if tail.startswith("asset/"):
            file_id = tail.removeprefix("asset/")
            picture = await _picture(source, user, file_id)
            if picture is None:
                if missed is not None:
                    missed.add(file_id)
                await route.fulfill(status=404, body=b"")
            else:
                await route.fulfill(status=200, body=picture[0], content_type=picture[1])
            return True
        await route.fulfill(status=404, body=b"")
        return True

    return answer


#: Written beside a figure's video once it is encoded: a resumed render plays it as it is.
_ENCODED_FILE = "encoded"
#: Moved when a figure is drawn differently for the same key, so every kept drawing is drawn again (2: framed
#: on what it paints, its drift fitted to the frame).
RENDER_VERSION = 2
#: Written beside a figure's frames: the release that last drew them. A render resumed on another build (a deploy,
#: a worker restart) says so when its figure page fails (:func:`resume_words`).
_RELEASE_FILE = "release"
#: Fresh pages a figure is drawn on before it fails: the first, and one more after a page that did not load.
FIGURE_PAGE_ATTEMPTS = 2


def resume_words(drawn_by: str | None, current: str | None) -> str:
    """What a figure page failure adds when its render resumed on another build than the one that drew its figure
    first: ``" (failed after resume across builds: drawn first on A, resumed on B)"``, else nothing."""
    if not drawn_by or not current or drawn_by == current:
        return ""
    return f" (failed after resume across builds: drawn first on {drawn_by}, resumed on {current})"


def _read_release(folder: Path) -> str | None:
    """The release that drew the frames kept in *folder* (:data:`_RELEASE_FILE`); ``None`` when none is written."""
    try:
        return (folder / _RELEASE_FILE).read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


@dataclass
class Drawing:
    """What :func:`draw` did for one figure: the key it is kept by, the frames it drew and those it took.

    What a render records of it is the reuse index the next pre-flight reads (``reuse_index``).
    """

    key: str = ""
    #: Frames screenshot by this draw.
    drawn: int = 0
    #: Frames a kept drawing served instead: taken from a former attempt or another render on this worker, or
    #: every frame of a video played as it is.
    reused: int = 0


def render_key(
    source: FigureSource,
    document: dict,
    *,
    scene: Any,
    length: float,
    words: list[SpokenWord] | None,
    safe: dict,
    viewport: tuple[int, int],
    scale: int,
    fps: int,
    readable: frozenset[str] = frozenset(),
    captions: Sequence[Caption] = (),
    ease: str = figure_fit.DEFAULT_EASE,
) -> str:
    """What a figure's frames are drawn from: the same key, the same frames (``figure_cache``, ``compositor.same_drawing``).

    Never the page's *document* itself, which gives the deck and its slide a
    new id on every read: what goes into it - the block, the scheme, the
    palette, the logos - and the pictures the person may read (*readable*,
    :func:`readable_pictures`); then how the scene builds it, for how long,
    when its narration says each word and its subtitles show them
    (*captions*), the band it keeps clear, how it stands in space and
    the brand's *ease* its stand moves with (its drift is fitted to them),
    the size and the frame rate, and the release whose figure page
    draws it.

    ``<content>-<scale>x<fps>`` (:func:`variant_key`): what it shows, then the size and frame rate it is drawn at,
    so a render at another quality finds a sharper drawing of the same content and derives its own from it
    (``figure_cache.find_sharper``).
    """
    import hashlib

    from app.core.config import settings

    shown = document.get("presentation") or {}
    drawn = {
        "version": RENDER_VERSION,
        "release": settings.app_version,
        "kind": source.kind,
        "slide": source.slide,
        "scheme": shown.get("theme"),
        "palette": shown.get("theme_config") or {},
        "asset_meta": shown.get("asset_meta") or {},
        "readable": sorted(readable),
        "build": scene.build,
        "reveal": scene.reveal,
        "camera": scene.camera,
        "dwell": round(float(scene.dwell), 4),
        "motion": scene.motion,
        "perspective": getattr(scene, "perspective", "flat"),
        "ease": ease,
        "length": round(float(length), 4),
        "words": [[word.text, round(word.start, 4), round(word.end, 4)] for word in words or []],
        "captions": [[round(start, 4), round(end, 4), text] for start, end, text in captions],
        "safe": safe,
        "viewport": list(viewport),
    }
    content = hashlib.sha256(json.dumps(drawn, sort_keys=True, default=str).encode("utf-8")).hexdigest()
    return variant_key(content, scale, fps)


def variant_key(content: str, scale: int, fps: int) -> str:
    """The key of the drawing of *content* at *scale* and *fps* (:func:`render_key`)."""
    return f"{content}-{int(scale)}x{int(fps)}"


def parse_key(key: str) -> tuple[str, int, int] | None:
    """``(content, scale, fps)`` of a :func:`render_key`; ``None`` for a key that is not one (an older release's)."""
    content, _, variant = key.rpartition("-")
    scale, _, fps = variant.partition("x")
    if not content or not scale.isdigit() or not fps.isdigit():
        return None
    return content, int(scale), int(fps)


def frames_in(times: list[float], span: tuple[float, float]) -> list[int]:
    """The indices of *times* (:func:`frame_times`) a span of the scene shows: the frame on screen at its start, up to its end."""
    start, end = span
    first = max((k for k, t in enumerate(times) if t <= start + 1e-6), default=0)
    last = max((k for k, t in enumerate(times) if t <= end + 1e-6), default=first)
    return list(range(first, max(first, last) + 1))


async def draw(
    source: FigureSource,
    document: dict,
    *,
    user: Any,
    scene: Any,
    length: float,
    words: list[SpokenWord] | None,
    fps: int,
    viewport: tuple[int, int],
    scale: int,
    workdir: Path,
    safe: dict | None = None,
    progress: Callable[[int, int], Awaitable[None]] | None = None,
    on_frame: Callable[[], None] | None = None,
    video_id: str | None = None,
    readable: frozenset[str] = frozenset(),
    span: tuple[float, float] | None = None,
    drawing: Drawing | None = None,
    captions: Sequence[Caption] = (),
    ease: str = figure_fit.DEFAULT_EASE,
) -> tuple[str, Any]:
    """Draw *scene*'s figure for *length* seconds: ``(the video's path, its timeline.ClipMedia)``, or :class:`FigureError`.

    *words*: when each word of its narration is said, on the scene's clock;
    *captions*: its subtitles' cues on the same clock, a step never coming in
    after the one that shows its words (:func:`shown_from`);
    *safe*: the band it keeps clear of its subtitles (:func:`caption_safe_area`);
    *ease*: the brand's ``move`` easing its stand in space turns with - its
    drift is fitted to the frame (:func:`plan_fit`).
    *on_frame* is called after each frame is drawn: the render process renews
    its lease from it (``lease.FrameLease``). The figure is drawn once
    per key (:func:`render_key`): a render resumed in the same
    *workdir* keeps what a former attempt drew; the video another render
    encoded whole - on this worker, or stored beside the video *video_id* - is
    played as it is, no page opened (``figure_cache``); the frames another
    render drew are taken and those drawn here offered to the next; a video
    drawn whole here is kept, unless a picture the person may read
    (*readable*) could not be read meanwhile. *span* ``(from, to)``, seconds
    into the scene, draws only the frames it shows (a neighbour in a scene
    preview's window): the video holds the nearest one elsewhere, and is never
    kept as the whole. *drawing*, when given, gets its key and the frames it
    drew and took.
    """
    from app.services.orchestration_engine import LeaseLostError
    from app.services.presentation_export import open_render_page
    from app.services.video_studio import compositor, encoder, figure_cache, timeline

    safe = safe or dict(NO_SAFE_AREA)
    key = render_key(
        source,
        document,
        scene=scene,
        length=length,
        words=words,
        safe=safe,
        viewport=viewport,
        scale=scale,
        fps=fps,
        readable=readable,
        captions=captions,
        ease=ease,
    )
    drawing = drawing if drawing is not None else Drawing()
    drawing.key = key
    shown = length if span is None else span[1] - span[0]
    kept = figure_cache.Kept(key, video_id)
    folder = (workdir / f"figure-{scene.id}").resolve()  # ffmpeg runs inside it: every path absolute
    folder.mkdir(parents=True, exist_ok=True)
    out = folder / ("figure.mp4" if span is None else "part.mp4")
    encoded_as = out.name if span is None else f"{out.name} {span[0]:.4f} {span[1]:.4f}"

    async def made(path: Path) -> tuple[str, Any]:
        info = await encoder.probe(str(path))
        clip = timeline.ClipMedia(
            duration=length,
            width=info.width,
            height=info.height,
            fps=float(fps),
            frame_count=max(1, round(length * fps)),
        )
        return str(path), clip

    from app.core.config import settings

    # Read before the folder is checked: the release is in the key, so a render resumed on another build finds the
    # folder made anew (``compositor.same_drawing``) and its figure drawn again from this build's page. What the
    # earlier attempt left - the build it drew on, whether it drew - only says why, should the page fail.
    drew_before = (folder / _RELEASE_FILE).is_file() or any(folder.glob("f*.jpg"))
    drawn_by = _read_release(folder)
    current = str(settings.app_version or "")
    marker = folder / _ENCODED_FILE
    if compositor.same_drawing(folder, key) and out.is_file() and marker.is_file() and marker.read_text() == encoded_as:
        logger.info("Scene %s: its figure resumed, its video kept", scene.id)
        drawing.reused = max(1, round(shown * fps))
        return await made(out)
    if (whole := await figure_cache.find(kept)) is not None:
        logger.info("Scene %s: its figure kept from an earlier render (%s)", scene.id, key[:12])
        drawing.reused = max(1, round(shown * fps))
        if progress is not None:
            await progress(0, 0)
        return await made(whole)
    if (sharper := await figure_cache.find_sharper(kept)) is not None:
        # Drawn sharper by a render at another quality: rescaled, never drawn again.
        logger.info("Scene %s: its figure derived from the sharper drawing %s", scene.id, sharper.parent.name[:24])
        drawing.reused = max(1, round(shown * fps))
        if progress is not None:
            await progress(0, 0)

        async def rescaling() -> None:
            if on_frame is not None:
                on_frame()

        derived = folder / "figure.mp4"
        await encoder.rescale(
            str(sharper),
            str(derived),
            width=viewport[0] * scale,
            height=viewport[1] * scale,
            fps=fps,
            on_wait=rescaling,
        )
        # The sharper drawing was kept only with every picture its person may read: so is this one.
        await figure_cache.keep(kept, derived)
        return await made(derived)
    frames: list[tuple[float, str]] = []
    missed: set[str] = set()
    taken = 0
    width, height = viewport
    failure: str | None = None
    for attempt in range(FIGURE_PAGE_ATTEMPTS):
        frames, taken = [], 0
        try:
            async with open_render_page(
                figure_page_url(),
                width=width,
                height=height,
                device_scale_factor=scale,
                signal="figure",
                error=FigureError,
                answer=_answerer(source, document, user, missed),
            ) as page:
                described = await page.evaluate("() => window.__xtmFigure.describe()")
                if current:
                    # Written once the page loaded and described the figure: a figure this release drew is valid.
                    (folder / _RELEASE_FILE).write_text(current, encoding="utf-8")
                resolved = _reveals_of(described, scene=scene, length=length, words=words, captions=captions)
                logger.info("%s", reveal_line(scene, described, resolved, length=length, paced=False))
                times = [reveal.at for reveal in resolved]
                fit = plan_fit(
                    described,
                    times,
                    scene=scene,
                    kind=source.kind,
                    length=length,
                    safe=safe,
                    viewport=viewport,
                    ease=ease,
                )
                if fit is not None:
                    logger.info("%s", fit.line(str(scene.id)))
                plan = _plan_with(times, scene=scene, length=length, safe=safe, fit=fit)
                planned = await page.evaluate("(plan) => window.__xtmFigure.plan(plan)", plan)
                wanted = frame_times(planned.get("active") or [], length, fps)
                needed = list(range(len(wanted))) if span is None else frames_in(wanted, span)
                for position, index in enumerate(needed):
                    name = f"f{index:05d}.jpg"
                    frames.append((wanted[index], name))
                    drawn = folder / name
                    if (drawn.is_file() and drawn.stat().st_size > 0) or figure_cache.take_frame(kept, name, folder):
                        taken += 1
                        continue
                    await page.evaluate("(t) => window.__xtmFigure.seek(t)", wanted[index])
                    # Renamed whole: a frame the render stopped in the middle of is never kept.
                    part = folder / f"{name}.part"
                    await page.screenshot(path=str(part), type="jpeg", quality=92)
                    part.replace(drawn)
                    figure_cache.share_frame(kept, drawn)
                    if on_frame is not None:
                        on_frame()
                    if progress is not None and position % 15 == 0:
                        await progress(position, len(needed))
            failure = None
            break
        except (LeaseLostError, compositor.RenderCancelledError):
            # The render's own ends, raised by its callbacks: never a figure that could not be drawn.
            raise
        except Exception as exc:  # the page's code not loaded, its drawing failed, no Chromium, a crashed page
            failure = str(exc) if isinstance(exc, FigureError) else format_exception_message(exc)
            if attempt + 1 < FIGURE_PAGE_ATTEMPTS:
                # Once more on a fresh page: the frames drawn whole are kept, only the rest is drawn.
                logger.warning("Scene %s: its figure page failed (%s); drawn again on a fresh page", scene.id, failure)
    if failure is not None:
        said = f"Scene {scene.id}: the figure could not be drawn: {failure}{resume_words(drawn_by, current)}"
        # A figure an earlier attempt drew, or one drawn first on another build, is valid: its page failed, not it.
        raise (FigurePageError if drew_before or resume_words(drawn_by, current) else FigureError)(said)
    drawing.drawn, drawing.reused = len(frames) - taken, taken
    if taken:
        logger.info("Scene %s: its figure resumed, %d of its %d frames kept", scene.id, taken, len(frames))
    # A part starts on the frame its span opens on: what it shows before that is never on screen.
    listed = frames if span is None else [(0.0, frames[0][1]), *frames[1:]]

    async def encoding() -> None:
        # Every frame drawn, it is encoded: one ffmpeg run that draws no frame, said while it lasts to the lease and
        # the stall window alike - at 2160p on a small worker it outlasts the lease.
        if on_frame is not None:
            on_frame()
        if progress is not None:
            await progress(len(listed), len(listed))

    await encoder.frames_to_mezzanine(
        listed,
        end_time=length,
        out_path=str(out),
        fps=fps,
        crf=16,
        preset="medium",
        workdir=str(folder),
        on_wait=encoding,
    )
    marker.write_text(encoded_as, encoding="utf-8")
    if span is None and not missed & readable:
        await figure_cache.keep(kept, out)
    return await made(out)


# ── The editor's still, drawn by the worker once the video is saved ───────

#: Where a figure's still is kept, by the digest of what it shows (:func:`preview_digest`), the steps the page
#: read of it beside it (:func:`steps_object`).
PREVIEW_PREFIX = "video-studio/figure-previews"
#: Moved when the still is drawn differently, so every still is drawn again (3: its steps kept beside it;
#: 4: framed on what it paints, which its kept steps say).
PREVIEW_VERSION = 4
#: The scene length a still is planned at: every step in, the camera back on the whole figure.
_PREVIEW_LENGTH = 6.0
#: A still asked for (``figure_stills.enqueue``) is queued once, whoever asks again, until it is drawn or this
#: long; a build a preview draws in the background (:func:`prepare_builds`) is drawn by one replica, a few at once
#: per process.
_DRAWING_KEY = "xtm:video-figure:drawing:{}"
_DRAWING_TTL = 15 * 60
_DRAWING = asyncio.Semaphore(2)
_DRAWING_TASKS: dict[str, asyncio.Task] = {}
#: Without Redis, the claims of this process.
_CLAIMED: set[str] = set()


@dataclass(frozen=True)
class FigurePreview:
    """One figure scene as its still is drawn: what it shows, at what size, the band it keeps clear, its digest."""

    scene_id: str
    source: FigureSource
    document: dict
    viewport: tuple[int, int]
    digest: str
    safe: dict = field(default_factory=lambda: dict(NO_SAFE_AREA))
    #: The scene (``spec.FigureScene``): how its build is timed, for the editor's build and a save's reveal times.
    scene: Any = None
    #: The brand's ``move`` easing its stand in space turns with (``figure_fit.move_ease``): what its drift is fitted to.
    ease: str = figure_fit.DEFAULT_EASE


def preview_digest(
    source: FigureSource,
    *,
    scheme: str,
    palette: dict,
    viewport: tuple[int, int],
    asset_meta: dict | None = None,
    safe: dict | None = None,
) -> str:
    """The digest of everything a still is drawn from - the block, the scheme, the palette, the logos, the size, the band.

    Taken over what goes INTO the figure page's document, never the document:
    the deck's validator gives its slide a new id on every read.
    """
    import hashlib

    shown = {
        "version": PREVIEW_VERSION,
        "kind": source.kind,
        "slide": source.slide,
        "scheme": scheme,
        "palette": palette or {},
        "asset_meta": asset_meta or {},
        "viewport": list(viewport),
        "safe": safe or NO_SAFE_AREA,
    }
    return hashlib.sha256(json.dumps(shown, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def preview_object(digest: str) -> str:
    return f"{PREVIEW_PREFIX}/{digest}.jpg"


def steps_object(digest: str) -> str:
    """Where the steps of the still under *digest* are kept: what the figure page described (``describe()``)."""
    return f"{PREVIEW_PREFIX}/{digest}.json"


async def _read_json(object_name: str) -> dict | None:
    """A JSON object kept in the store; ``None`` when it is missing or unreadable."""
    from app.services.file_storage import file_storage

    try:
        value = json.loads(await file_storage.download(object_name))
    except Exception:  # a miss: drawn before it was kept, or a store that cannot be read
        return None
    return value if isinstance(value, dict) else None


async def read_steps(digest: str) -> dict | None:
    """What the figure page described of the still under *digest* (its steps and their labels); ``None`` when not kept."""
    return await _read_json(steps_object(digest))


async def preview_stat(digest: str) -> dict | None:
    """The still under *digest* as the store describes it (``size``, ``last_modified``); ``None`` when not drawn."""
    from app.services.file_storage import file_storage

    try:
        return await file_storage.stat(preview_object(digest))
    except Exception:  # a miss: the preview words the scene, as before it was drawn
        return None


async def has_preview(digest: str) -> bool:
    """Whether the still under *digest* is drawn; a store that cannot be read is a miss."""
    return await preview_stat(digest) is not None


async def figure_previews(db: Any, user: Any, spec: Any, palette: dict) -> dict[str, FigurePreview]:
    """Every figure scene of *spec* (parsed, its template applied) as its still is drawn, by scene id.

    A deck's block is read as *user* may read the deck; a scene whose deck
    they cannot open, or whose address names no block, is left out (the
    save names that problem already, the preview words the scene).
    """
    from sqlalchemy import select

    from app.api.v1.helpers import get_user_group_ids, user_visible_filter
    from app.models.presentation import Presentation
    from app.services.presentations import load_logo_asset_meta
    from app.services.video_studio import figure_twins
    from app.services.video_studio.spec import design_size

    found: dict[str, FigurePreview] = {}
    figures = [scene for scene in spec.scenes if scene.type == "figure" and scene.id]
    if not figures:
        return found
    groups = await get_user_group_ids(db, user.id)
    viewport = design_size(spec.format.aspect_ratio)
    ease = figure_fit.move_ease(spec.theme.motion)
    for scene in figures:
        try:
            if scene.presentation_id:
                deck_id = uuid.UUID(scene.presentation_id)
                deck = (
                    await db.execute(
                        select(Presentation.spec).where(
                            Presentation.id == deck_id,
                            user_visible_filter(Presentation, user.id, groups, is_admin=user.role == "admin"),
                        )
                    )
                ).scalar_one_or_none()
                if deck is None:
                    continue
                source = source_from_deck(scene, dict(deck or {}), deck_id)
            else:
                source = source_from_spec(scene)
            source = await figure_twins.themed(db, source, scheme=spec.theme.mode, user=user)
            asset_meta = await load_logo_asset_meta(db, {"slides": [source.slide]})
            document = figure_document(source, scheme=spec.theme.mode, palette=palette, asset_meta=asset_meta)
        except FigureError:
            continue
        safe = caption_safe_area(spec, scene, viewport[1])
        digest = preview_digest(
            source, scheme=spec.theme.mode, palette=palette, viewport=viewport, asset_meta=asset_meta, safe=safe
        )
        found[scene.id] = FigurePreview(scene.id, source, document, viewport, digest, safe, scene, ease)
    return found


async def pictures_readable(db: Any, source: FigureSource, user: Any) -> bool:
    """Whether *user* may read every picture the figure draws: a still drawn for another person shows only then."""
    from app.services.file_access import load_readable_file

    for file_id in sorted(source.files):
        try:
            await load_readable_file(db, file_id, user, presentation_id=source.presentation_id)
        except Exception:  # unreadable answers like unknown: the preview words the scene instead
            return False
    return True


async def _draw_still(
    source: FigureSource, document: dict, *, user: Any, viewport: tuple[int, int], safe: dict | None = None
) -> tuple[bytes, dict]:
    """The still (:func:`draw_still`) and what the page described of the figure: its steps and their labels."""
    from app.services.presentation_export import open_render_page

    width, height = viewport
    try:
        async with open_render_page(
            figure_page_url(),
            width=width,
            height=height,
            signal="figure",
            error=FigureError,
            answer=_answerer(source, document, user),
        ) as page:
            described = await page.evaluate("() => window.__xtmFigure.describe()")
            labels = [list(step.get("labels") or []) for step in described.get("steps") or []]
            plan = {
                "times": reveal_times(labels, length=_PREVIEW_LENGTH, together=True),
                "length": _PREVIEW_LENGTH,
                "camera": "still",
                "hold": WHOLE_HOLD,
                "safe": safe or dict(NO_SAFE_AREA),
                # The figure at rest: no pulse caught half-way along an edge, no drift.
                "motion": "none",
            }
            await page.evaluate("(plan) => window.__xtmFigure.plan(plan)", plan)
            await page.evaluate("(t) => window.__xtmFigure.seek(t)", _PREVIEW_LENGTH)
            return await page.screenshot(type="jpeg", quality=88), described
    except FigureError:
        raise
    except Exception as exc:  # no Chromium, a crashed page
        raise FigureError(format_exception_message(exc)) from exc


async def draw_still(
    source: FigureSource, document: dict, *, user: Any, viewport: tuple[int, int], safe: dict | None = None
) -> bytes:
    """The figure built in whole - its scene's last frame: every step in, the whole figure clear of *safe* - as one JPEG.

    Raises :class:`FigureError`.
    """
    return (await _draw_still(source, document, user=user, viewport=viewport, safe=safe))[0]


async def store_still(preview: FigurePreview, user: Any) -> None:
    """Draw *preview*'s still, its pictures read as *user*, and keep it with its steps (the stills job)."""
    from app.services.file_storage import file_storage

    data, described = await _draw_still(
        preview.source, preview.document, user=user, viewport=preview.viewport, safe=preview.safe
    )
    # The steps first: a still that is kept has its steps beside it.
    await file_storage.upload_to(
        json.dumps(described).encode("utf-8"), steps_object(preview.digest), "application/json"
    )
    await file_storage.upload_to(data, preview_object(preview.digest), "image/jpeg")


async def claim_drawing(digest: str) -> bool:
    """Whether what is kept under *digest* (a still, a build) is to be queued or drawn now: nobody is on it (Redis)."""
    from app.core.redis import redis_client

    try:
        return bool(await (await redis_client()).set(_DRAWING_KEY.format(digest), "1", nx=True, ex=_DRAWING_TTL))
    except Exception:  # no shared state: this process alone keeps it to one ask
        if digest in _CLAIMED:
            return False
        _CLAIMED.add(digest)
        return True


async def being_drawn(digest: str) -> bool:
    """Whether the still under *digest* is queued or being drawn (claimed): a preview that does not ask still polls."""
    from app.core.redis import redis_client

    try:
        return bool(await (await redis_client()).exists(_DRAWING_KEY.format(digest)))
    except Exception:  # no shared state: this process's own claims
        return digest in _CLAIMED


async def release_drawing(digest: str) -> None:
    """The still under *digest* drawn, or given up: the next one to find it missing asks again."""
    from app.core.redis import redis_client

    _CLAIMED.discard(digest)
    try:
        await (await redis_client()).delete(_DRAWING_KEY.format(digest))
    except Exception as exc:
        logger.debug("Figure still claim not released: %s", format_exception_message(exc))


async def _draw_in_background(digest: str, what: str, drawing: Callable[[], Awaitable[None]]) -> None:
    try:
        async with _DRAWING:
            await drawing()
    except Exception as exc:  # the next preview tries again
        logger.info("%s not drawn: %s", what, format_exception_message(exc))
    finally:
        await release_drawing(digest)
        _DRAWING_TASKS.pop(digest, None)


async def _start_drawing(digest: str, what: str, drawing: Callable[[], Awaitable[None]]) -> None:
    """Draw what is kept under *digest* in the background, once, by one replica."""
    if digest in _DRAWING_TASKS or not await claim_drawing(digest):
        return
    _DRAWING_TASKS[digest] = asyncio.create_task(_draw_in_background(digest, what, drawing))


async def start_background_draw(digest: str, what: str, drawing: Callable[[], Awaitable[None]]) -> None:
    """Draw what a preview plays under *digest* in the background, once, by one replica (a screen insert's build).

    The claim and the drawing slots are the figures' own: one queue for everything the editor's preview has drawn.
    """
    await _start_drawing(digest, what, drawing)


def _reader(user: Any) -> Any:
    # The request's session closes before the figure is drawn: who reads the pictures is copied now.
    return SimpleNamespace(id=user.id, role=user.role)


# ── When a saved video's figures come in ──────────────────────────────────


@dataclass(frozen=True)
class FigureTiming:
    """A figure scene as a plan times it: its length, where its narration is said and its subtitles' cues (seconds into
    the scene)."""

    length: float
    said_from: float = 0.0
    said_seconds: float = 0.0
    captions: tuple[Caption, ...] = ()


def scene_captions(plan: Any, scene_id: str, start: float) -> tuple[Caption, ...]:
    """The caption cues of *scene_id*'s narration in *plan*, on the clock of its scene starting at *start*."""
    return tuple(
        (float(t0) - start, float(t1) - start, str(text)) for t0, t1, text in plan.captions.get(scene_id) or ()
    )


def figure_timing(plan: Any, scene_id: str) -> FigureTiming | None:
    """*scene_id*'s timing in *plan* (a ``timeline.TimelinePlan``); ``None`` when the plan has no such scene."""
    placed = next((scene for scene in plan.document["scenes"] if scene.get("id") == scene_id), None)
    if placed is None:
        return None
    start = float(placed["start"])
    said = plan.narration_spans.get(scene_id)
    if not said:
        return FigureTiming(float(placed["end"]) - start)
    return FigureTiming(
        float(placed["end"]) - start,
        float(said[0]) - start,
        float(said[1]) - float(said[0]),
        scene_captions(plan, scene_id, start),
    )


def paced_words(scene: Any, timing: FigureTiming) -> list[SpokenWord] | None:
    """*scene*'s narration paced over the span *timing* gives it (``speech_timing.paced_words``); ``None`` without one."""
    from app.services.video_studio.speech_timing import paced_words as pace

    if not (scene.narration or "").strip() or timing.said_seconds <= 0:
        return None
    return pace(scene.narration, timing.said_from, timing.said_seconds)


async def reveal_lines(previews: list[FigurePreview], timings: dict[str, FigureTiming]) -> list[str]:
    """When each step of each figure of a save comes in (:func:`reveal_line`): the steps read off its still.

    Timed on the save's plan, its narration paced: the render times it again
    on the spoken voice. A figure whose still is not drawn yet says nothing.
    """
    lines: list[str] = []
    for preview in previews:
        timing = timings.get(preview.scene_id)
        if preview.scene is None or timing is None:
            continue
        described = await read_steps(preview.digest)
        if not described or not described.get("steps"):
            continue
        resolved = _reveals_of(
            described,
            scene=preview.scene,
            length=timing.length,
            words=paced_words(preview.scene, timing),
            captions=timing.captions,
        )
        lines.append(reveal_line(preview.scene, described, resolved, length=timing.length, paced=True))
    return lines


#: The figure fits judged in this process (:func:`figure_fits`): ``key -> FigureFit or None``, the oldest
#: forgotten past :data:`FITS_KEPT`. The quality check runs on every save and every ``get_video``: an unchanged figure
#: is never planned again, nor its steps read from the object store.
_FITS: OrderedDict[str, figure_fit.FigureFit | None] = OrderedDict()
FITS_KEPT = 512


def forget_figure_fits() -> None:
    """Forget the figure fits judged in this process (:data:`_FITS`)."""
    _FITS.clear()


def _fit_key(preview: FigurePreview, timing: FigureTiming) -> str:
    """What a figure's fit is judged from: its still (``digest``), its timing, its brand's easing, the band it
    keeps clear and its size, its source's kind and the scene as written - its perspective, its motion, its
    narration - never anything it does not read."""
    import hashlib

    scene = preview.scene.model_dump(mode="json") if hasattr(preview.scene, "model_dump") else repr(preview.scene)
    said = [
        preview.digest,
        dataclasses.asdict(timing),
        preview.ease,
        preview.safe,
        list(preview.viewport),
        preview.source.kind,
        scene,
    ]
    return hashlib.sha256(json.dumps(said, sort_keys=True, default=str).encode("utf-8")).hexdigest()


async def figure_fits(
    previews: list[FigurePreview], timings: dict[str, FigureTiming]
) -> dict[str, figure_fit.FigureFit]:
    """``scene id -> how its figure is drawn in motion`` (:func:`plan_fit`) of each figure of a save, judged on
    what the page described of its still and timed as :func:`reveal_lines` times it; a still not drawn yet says
    nothing. Each fit is kept in the process by what it is judged from (:func:`_fit_key`): judged once while
    its figure is unchanged."""
    found: dict[str, figure_fit.FigureFit] = {}
    for preview in previews:
        timing = timings.get(preview.scene_id)
        if preview.scene is None or timing is None:
            continue
        key = _fit_key(preview, timing)
        if key in _FITS:
            _FITS.move_to_end(key)
            if (kept := _FITS[key]) is not None:
                found[preview.scene_id] = kept
            continue
        described = await read_steps(preview.digest)
        if not described or not described.get("steps"):
            # A still not drawn yet: judged again once it is.
            continue
        resolved = _reveals_of(
            described,
            scene=preview.scene,
            length=timing.length,
            words=paced_words(preview.scene, timing),
            captions=timing.captions,
        )
        fit = plan_fit(
            described,
            [reveal.at for reveal in resolved],
            scene=preview.scene,
            kind=preview.source.kind,
            length=timing.length,
            safe=preview.safe,
            viewport=preview.viewport,
            ease=preview.ease,
        )
        _FITS[key] = fit
        while len(_FITS) > FITS_KEPT:
            _FITS.popitem(last=False)
        if fit is not None:
            found[preview.scene_id] = fit
    return found


def twin_lines(previews: list[FigurePreview]) -> list[str]:
    """What a save says of the template assets each figure draws as their twins for the video's scheme
    (``figure_twins``): ``Scene s05: its figure draws 4 pictures as their twins for the dark video: opencti-logo-light.svg
    as opencti-logo.svg, ...``. A raster drawn as the vector that replaces it is said apart: ``Scene s05: its
    figure draws 1 raster as the vector it names for the dark video, sharp at any size, the deck left as it is:
    opencrq-logo-light.png as opencrq-logo.svg (through opencrq-logo-light.svg).``"""
    lines: list[str] = []
    for preview in previews:
        scheme = (preview.document.get("presentation") or {}).get("theme") or "video's"
        pairs = sorted({(twin.filename, twin.twin_filename) for twin in preview.source.twins if not twin.sharpened})
        if pairs:
            said = ", ".join(f"{picture} as {twin}" for picture, twin in pairs)
            count = f"{len(pairs)} picture{'s' if len(pairs) != 1 else ''}"
            lines.append(
                f"Scene {preview.scene_id}: its figure draws {count} as their twins for the {scheme} video: {said}."
            )
        sharpened = sorted(
            {(twin.filename, twin.twin_filename, twin.through) for twin in preview.source.twins if twin.sharpened}
        )
        if sharpened:
            said = ", ".join(
                f"{picture} as {vector}" + (f" (through {through})" if through else "")
                for picture, vector, through in sharpened
            )
            count = f"{len(sharpened)} raster{'s' if len(sharpened) != 1 else ''}"
            lines.append(
                f"Scene {preview.scene_id}: its figure draws {count} as the vector {'it names' if len(sharpened) == 1 else 'they name'} "
                f"for the {scheme} video, sharp at any size, the deck left as it is: {said}."
            )
    return lines


async def fit_lines(previews: list[FigurePreview], timings: dict[str, FigureTiming]) -> list[str]:
    """What a save says of how each figure is drawn in motion: the drift it plays - capped, or held still -
    and the frame it keeps clear at its closest (:meth:`figure_fit.FigureFit.line`)."""
    return [fit.line(scene_id) for scene_id, fit in (await figure_fits(previews, timings)).items()]


# ── The editor's build, drawn in the background for its preview ───────────

#: Where a figure's build is kept for the editor's preview, by the digest of what it plays (:func:`build_digest`):
#: the video and, beside it, its size and when the figure is whole.
BUILD_PREFIX = "video-studio/figure-builds"
#: Moved when a build is drawn differently, so every build is drawn again (2: its drift fitted to the frame).
BUILD_VERSION = 2
#: Lighter than a render's: fewer frames, half the pixels.
BUILD_FPS = 15
BUILD_SCALE = 0.5


def build_digest(preview: FigurePreview, timing: FigureTiming) -> str:
    """The digest of a figure's build: its still's (the block, the palette, the size, the band) and how the scene times it."""
    import hashlib

    scene = preview.scene
    narrated = scene.reveal == "narration" and bool((scene.narration or "").strip())
    shown = {
        "version": BUILD_VERSION,
        "still": preview.digest,
        "build": scene.build,
        "reveal": scene.reveal,
        "camera": scene.camera,
        "dwell": round(float(scene.dwell), 3),
        "motion": scene.motion,
        # What its drift is fitted to: how it stands in space and the brand's easing it turns with.
        "perspective": getattr(scene, "perspective", "flat"),
        "ease": preview.ease,
        "length": round(timing.length, 2),
        "narration": [scene.narration, round(timing.said_from, 2), round(timing.said_seconds, 2)] if narrated else None,
        "captions": [[round(start, 2), text] for start, _end, text in timing.captions] if narrated else None,
    }
    return hashlib.sha256(json.dumps(shown, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def build_object(digest: str) -> str:
    return f"{BUILD_PREFIX}/{digest}.mp4"


def build_meta_object(digest: str) -> str:
    return f"{BUILD_PREFIX}/{digest}.json"


async def read_build(digest: str) -> dict | None:
    """The build kept under *digest* - ``{duration, width, height, fps, frames, whole}`` - or ``None`` when it is not drawn."""
    meta = await _read_json(build_meta_object(digest))
    return (
        meta if meta and all(key in meta for key in ("duration", "width", "height", "fps", "frames", "whole")) else None
    )


async def _store_build(preview: FigurePreview, timing: FigureTiming, digest: str, user: Any) -> None:
    """Draw *preview*'s figure built in for its scene's *timing*, as the render draws it but lighter, and keep it."""
    import tempfile

    from app.services.file_storage import file_storage
    from app.services.presentation_export import open_render_page
    from app.services.video_studio import encoder

    width, height = preview.viewport
    scene = preview.scene
    with tempfile.TemporaryDirectory(prefix="xtm-figure-build-") as work:
        folder = Path(work).resolve()
        frames: list[tuple[float, str]] = []
        async with open_render_page(
            figure_page_url(),
            width=width,
            height=height,
            device_scale_factor=BUILD_SCALE,
            signal="figure",
            error=FigureError,
            answer=_answerer(preview.source, preview.document, user),
        ) as page:
            described = await page.evaluate("() => window.__xtmFigure.describe()")
            plan = _plan_of(
                described,
                scene=scene,
                kind=preview.source.kind,
                length=timing.length,
                words=paced_words(scene, timing),
                safe=preview.safe,
                viewport=preview.viewport,
                ease=preview.ease,
                captions=timing.captions,
            )
            planned = await page.evaluate("(plan) => window.__xtmFigure.plan(plan)", plan)
            for index, t in enumerate(frame_times(planned.get("active") or [], timing.length, BUILD_FPS)):
                await page.evaluate("(t) => window.__xtmFigure.seek(t)", t)
                name = f"f{index:05d}.jpg"
                await page.screenshot(path=str(folder / name), type="jpeg", quality=80)
                frames.append((t, name))
        out = str(folder / "build.mp4")
        await encoder.frames_to_mezzanine(
            frames, end_time=timing.length, out_path=out, fps=BUILD_FPS, crf=26, preset="veryfast", workdir=str(folder)
        )
        info = await encoder.probe(out)
        meta = {
            "duration": round(timing.length, 4),
            "width": info.width,
            "height": info.height,
            "fps": BUILD_FPS,
            "frames": max(1, round(timing.length * BUILD_FPS)),
            "whole": round(float(planned.get("whole") or timing.length), 4),
        }
        await file_storage.upload_path(out, build_object(digest), "video/mp4")
    # Its meta last: a build that says it is drawn has its video.
    await file_storage.upload_to(json.dumps(meta).encode("utf-8"), build_meta_object(digest), "application/json")


async def prepare_builds(builds: list[tuple[FigurePreview, FigureTiming, str]], user: Any) -> None:
    """Draw in the background the builds a preview found missing (``(still, timing, build digest)``): once each, by one replica.

    The editor's stage plays a figure's build where the scene plays;
    until it is drawn, the stage shows the figure built in whole (its still)
    and the preview says it is drawing, so the editor polls.
    """
    reader = _reader(user)
    for preview, timing, digest in builds:
        await _start_drawing(
            digest, f"Figure build of scene {preview.scene_id}", partial(_store_build, preview, timing, digest, reader)
        )
