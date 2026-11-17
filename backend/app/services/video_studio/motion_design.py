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

"""How a video moves: the brand's motion tokens, the built-in compositions and what a motion scene draws.

Three things live here, and only here:

* **The tokens** (:func:`resolve_motion`). A video's ``theme.motion`` names a
  personality; it resolves into the numbers every choreography of the
  compositor page is drawn with - durations, easing curves, staggers, the
  distance and blur a text rises from, how a card title splits, what a card's
  logo, kicker and call to action do. ``precise`` is the platform's own motion,
  the values the page drew before tokens existed, so a video without
  ``theme.motion`` draws the same frames. The tokens travel in the document
  (``theme.motion``): the page never decides a duration of its own.
* **The compositions** (:func:`compose`). A motion scene's ``composition`` is
  laid out here into elements, from its texts, the frame's shape and the
  tokens, so a ``kinetic_title`` moves like the brand whatever template the
  video wears. The page draws elements and knows no composition.
* **The elements as the page draws them** (:func:`element_payload`): a role's
  type, a picture's key, the presets' durations and easings, all resolved.

A Lottie file a motion scene plays is checked here too (:func:`check_lottie`):
the page plays it with ``lottie_light``, which evaluates no expression, and
loads nothing the file names outside itself.
"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from app.services.video_studio.spec import MotionElement, MotionScene, MotionStyle

# ── The tokens ────────────────────────────────────────────────────────────

PERSONALITIES: tuple[str, ...] = ("precise", "calm", "energetic", "cinematic", "playful")

#: Each personality's tokens, before ``speed``. ``time`` scales every offset the
#: page counts in seconds; the durations and staggers below are already in
#: seconds. ``precise`` holds the numbers the page drew with before motion tokens existed.
_PERSONALITY: dict[str, dict[str, Any]] = {
    "precise": {
        "time": 1.0,
        "ease": {"enter": "out", "exit": "in", "move": "in_out", "emphasis": "out_back"},
        "title": {
            "split": "words",
            "style": "rise",
            "duration": 0.7,
            "distance": 46,
            "blur": 14,
            "rotate": 2,
            "scale": 1.0,
            "stagger": 0.065,
        },
        "block": {"duration": 0.7, "distance": 40, "blur": 12, "step": 0.55, "word": 0.07, "drift": 0.02},
        "card": {"logo": "rise", "kicker": "track", "rule": "draw", "cta": "pop", "accent": "none", "drift": 0.025},
        "overlay": {"duration": 0.38},
    },
    "calm": {
        "time": 1.3,
        "ease": {"enter": "out_quint", "exit": "in_out", "move": "in_out", "emphasis": "out_quint"},
        "title": {
            "split": "lines",
            "style": "mask",
            "duration": 1.1,
            "distance": 24,
            "blur": 6,
            "rotate": 0,
            "scale": 1.0,
            "stagger": 0.16,
        },
        "block": {"duration": 1.0, "distance": 22, "blur": 8, "step": 0.7, "word": 0.09, "drift": 0.012},
        "card": {"logo": "fade", "kicker": "fade", "rule": "draw", "cta": "rise", "accent": "none", "drift": 0.015},
        "overlay": {"duration": 0.5},
    },
    "energetic": {
        "time": 0.75,
        "ease": {"enter": "snap", "exit": "in_out_expo", "move": "in_out_expo", "emphasis": "spring(220, 14)"},
        "title": {
            "split": "letters",
            "style": "pop",
            "duration": 0.6,
            "distance": 70,
            "blur": 0,
            "rotate": 0,
            "scale": 0.55,
            "stagger": 0.026,
        },
        "block": {"duration": 0.5, "distance": 56, "blur": 0, "step": 0.4, "word": 0.05, "drift": 0.024},
        "card": {"logo": "scale", "kicker": "track", "rule": "draw", "cta": "pop", "accent": "sweep", "drift": 0.03},
        "overlay": {"duration": 0.3},
    },
    "cinematic": {
        "time": 1.25,
        "ease": {"enter": "out_expo", "exit": "in_out_expo", "move": "in_out_expo", "emphasis": "out_quint"},
        "title": {
            "split": "letters",
            "style": "blur",
            "duration": 1.3,
            "distance": 0,
            "blur": 28,
            "rotate": 0,
            "scale": 1.08,
            "stagger": 0.038,
        },
        "block": {"duration": 1.1, "distance": 16, "blur": 20, "step": 0.75, "word": 0.08, "drift": 0.032},
        "card": {"logo": "mask", "kicker": "track", "rule": "draw", "cta": "rise", "accent": "sweep", "drift": 0.04},
        "overlay": {"duration": 0.5},
    },
    "playful": {
        "time": 0.9,
        "ease": {"enter": "spring_bouncy", "exit": "in_out", "move": "in_out", "emphasis": "spring_bouncy"},
        "title": {
            "split": "letters",
            "style": "pop",
            "duration": 0.75,
            "distance": 40,
            "blur": 0,
            "rotate": 8,
            "scale": 0.4,
            "stagger": 0.034,
        },
        "block": {"duration": 0.6, "distance": 44, "blur": 0, "step": 0.45, "word": 0.06, "drift": 0.024},
        "card": {"logo": "scale", "kicker": "rise", "rule": "draw", "cta": "pop", "accent": "none", "drift": 0.03},
        "overlay": {"duration": 0.34},
    },
}
#: Seconds keys of the token groups ``speed`` divides.
_TIMED = {"title": ("duration", "stagger"), "block": ("duration", "step", "word"), "overlay": ("duration",)}


def motion_style(value: MotionStyle | dict | None) -> MotionStyle:
    """*value* (a spec's ``theme.motion``, as a model or a dict) as a :class:`MotionStyle`, ``precise`` when absent."""
    if isinstance(value, MotionStyle):
        return value
    if isinstance(value, dict):
        return MotionStyle.model_validate(value)
    return MotionStyle()


def resolve_motion(value: MotionStyle | dict | None) -> dict:
    """The tokens the compositor page draws a video with: its personality's, ``speed`` and ``title`` applied."""
    style = motion_style(value)
    tokens = copy.deepcopy(_PERSONALITY[style.personality])
    speed = float(style.speed or 1.0)
    tokens["time"] = round(tokens["time"] / speed, 4)
    for group, keys in _TIMED.items():
        for key in keys:
            tokens[group][key] = round(tokens[group][key] / speed, 4)
    if style.title is not None:
        tokens["title"]["split"] = style.title
    if style.logo is not None:
        tokens["card"]["logo"] = style.logo
    if style.accent is not None:
        tokens["card"]["accent"] = style.accent
    tokens["personality"] = style.personality
    tokens["speed"] = speed
    tokens["beat_sync"] = style.beat_sync
    tokens["motion_blur"] = style.motion_blur
    return tokens


#: The sub-frames each frame averages under motion blur, spread over a 180-degree shutter (half the frame's time),
#: and the qualities it is drawn at: a draft or a standard render is for judging the edit, not the finish.
BLUR_SAMPLES = 3
BLUR_SHUTTER = 0.5
BLUR_QUALITIES = frozenset({"high", "ultra"})


def blur_samples(value: Any, quality: str | None) -> int:
    """How many sub-frames each frame of a render at *quality* draws: :data:`BLUR_SAMPLES` with motion blur, else 1.

    *value* is a spec's ``theme.motion`` (a :class:`MotionStyle` or its dict) or a document's resolved tokens.
    """
    if quality not in BLUR_QUALITIES or value is None:
        return 1
    blur = value.motion_blur if isinstance(value, MotionStyle) else value.get("motion_blur") if isinstance(value, dict) else None
    return BLUR_SAMPLES if blur == "on" else 1


def blur_offsets(samples: int) -> list[float]:
    """Where each sub-frame of a frame is drawn, in frames from it: spread evenly over the shutter, centred on it."""
    if samples <= 1:
        return [0.0]
    return [round(BLUR_SHUTTER * ((index + 0.5) / samples - 0.5), 6) for index in range(samples)]


# ── Elements as the page draws them ───────────────────────────────────────

#: The type of each text role: size (design px), weight, font, colour, and the
#: kicker's tracking and capitals.
_ROLES: dict[str, dict[str, Any]] = {
    "headline": {"size": 128, "weight": 800, "font": "heading", "color": "foreground", "tracking": -0.035},
    "title": {"size": 84, "weight": 800, "font": "heading", "color": "foreground", "tracking": -0.03},
    "body": {"size": 36, "weight": 400, "font": "body", "color": "muted", "tracking": 0.0},
    "kicker": {"size": 24, "weight": 700, "font": "heading", "color": "accent", "tracking": 0.22, "uppercase": True},
    "label": {"size": 30, "weight": 600, "font": "heading", "color": "foreground", "tracking": 0.0},
    "figure": {"size": 180, "weight": 800, "font": "heading", "color": "gradient", "tracking": -0.04},
}
_ANCHOR_POINTS = {
    "center": (0.5, 0.5),
    "top_left": (0.0, 0.0),
    "top": (0.5, 0.0),
    "top_right": (1.0, 0.0),
    "left": (0.0, 0.5),
    "right": (1.0, 0.5),
    "bottom_left": (0.0, 1.0),
    "bottom": (0.5, 1.0),
    "bottom_right": (1.0, 1.0),
}
#: Seconds a counted text counts for when no ``progress`` keyframe drives it, before the tokens' time.
COUNT_SECONDS = 1.4


def lottie_key(file_id: str) -> str:
    """Where a Lottie file is served from to the page."""
    return f"lottie/{file_id}"


def picture_key(element: MotionElement) -> str | None:
    """Where an image element's picture is served from: the cards' ``logo``, else an image overlay's key.

    The logo is the one the title cards draw (served as ``logo``); a file or
    another mark is an image overlay's picture (``overlay/<file or mark>``,
    ``timeline.overlay_image_key``), fetched and sized like one.
    """
    if element.kind != "image":
        return None
    if element.file_id:
        return f"overlay/{element.file_id}"
    if element.asset == "logo":
        return "logo"
    return f"overlay/{element.asset}" if element.asset else None


def _enter_payload(enter: Any, tokens: dict, *, split: bool) -> dict | None:
    if enter is None:
        return None
    preset = enter.preset
    emphasis = preset in ("pop", "scale")
    duration = enter.duration if enter.duration is not None else (
        tokens["title"]["duration"] if split else tokens["block"]["duration"]
    )
    return {
        "preset": preset,
        "at": round(float(enter.at), 4),
        "duration": round(float(duration), 4),
        "ease": enter.ease or ("emphasis" if emphasis else "enter"),
        "stagger": round(float(enter.stagger if enter.stagger is not None else tokens["title"]["stagger"]), 4),
        "order": enter.order,
    }


def _exit_payload(exit_: Any, tokens: dict, length: float) -> dict | None:
    if exit_ is None:
        return None
    duration = exit_.duration if exit_.duration is not None else tokens["block"]["duration"] * 0.6
    at = exit_.at if exit_.at is not None else max(0.0, length - duration)
    return {"preset": exit_.preset, "at": round(float(at), 4), "duration": round(float(duration), 4), "ease": exit_.ease or "exit"}


def element_payload(element: MotionElement, tokens: dict, *, length: float) -> dict:
    """*element* as the compositor page draws it: its role's type, its picture's key, its presets' timings resolved."""
    anchor = _ANCHOR_POINTS[element.anchor]
    out: dict[str, Any] = {
        "id": element.id,
        "kind": element.kind,
        "x": element.x,
        "y": element.y,
        "anchor": list(anchor),
        "w": element.width,
        "h": element.height,
        "below": element.below,
        "gap": element.gap,
        "z": element.z,
        "start": element.start,
        "end": element.end,
        "opacity": element.opacity,
        "scale": element.scale,
        "rotate": element.rotate,
        "blur": element.blur,
        "keys": [key.model_dump(mode="json", exclude_none=True) for key in element.keys],
    }
    split = bool(element.kind == "text" and element.split and element.split != "none")
    out["enter"] = _enter_payload(element.enter, tokens, split=split)
    out["exit"] = _exit_payload(element.exit, tokens, length)
    if element.loop is not None:
        loop = element.loop.model_dump(mode="json")
        if loop.get("at") is None:
            entered = out["enter"]["at"] + out["enter"]["duration"] if out["enter"] else element.start
            loop["at"] = round(entered, 4)
        out["loop"] = loop
    if element.path is not None:
        out["path"] = element.path.model_dump(mode="json")
        out["path"]["ease"] = out["path"].get("ease") or "move"
    if element.mask is not None:
        out["mask"] = element.mask.model_dump(mode="json")
    if element.kind == "text":
        role = _ROLES[element.role or "body"]
        out["text"] = {
            "text": element.text,
            "size": element.size or role["size"],
            "weight": element.weight or role["weight"],
            "font": element.font or role["font"],
            "color": element.color or role["color"],
            "align": element.align or ("left" if anchor[0] == 0.0 else "right" if anchor[0] == 1.0 else "center"),
            "split": element.split or "none",
            "tracking": element.tracking if element.tracking is not None else role["tracking"],
            "uppercase": element.uppercase if element.uppercase is not None else bool(role.get("uppercase")),
            "max_w": element.max_width,
            "marker": element.marker or "none",
            "count": bool(element.count),
            "count_seconds": round(COUNT_SECONDS * tokens["time"], 4),
        }
    elif element.kind == "shape":
        out["shape"] = {
            "shape": element.shape or "rect",
            "fill": element.fill or ("none" if element.shape in ("line", "ring") else "accent"),
            "stroke": element.stroke or ("accent" if element.shape in ("line", "ring") else "none"),
            "stroke_w": element.stroke_width if element.stroke_width is not None else 4.0,
            "radius": element.radius or 0.0,
            "sides": element.sides or (5 if element.shape == "star" else 6),
        }
    elif element.kind == "path":
        out["path_d"] = {
            "d": element.d,
            "view": list(element.view_box) if element.view_box else [1000.0, 1000.0],
            "morph_to": element.morph_to,
            "fill": element.fill or "none",
            "stroke": element.stroke or ("accent" if not element.fill or element.fill == "none" else "none"),
            "stroke_w": element.stroke_width if element.stroke_width is not None else 4.0,
        }
    elif element.kind == "image":
        out["image"] = {"src": picture_key(element), "fit": element.fit or "contain", "brand": element.asset}
    elif element.kind == "lottie":
        out["lottie"] = {
            "src": lottie_key(str(element.file_id)),
            "speed": element.speed or 1.0,
            "loop": True if element.loop_playback is None else element.loop_playback,
            "fit": element.fit or "contain",
        }
    elif element.kind == "particles":
        out["particles"] = {
            "count": element.particle_count,
            "seed": element.seed if element.seed is not None else 7,
            "shape": element.particle or "dot",
            "size": element.size or 3.0,
            "drift": element.drift if element.drift is not None else 0.3,
            "twinkle": element.twinkle if element.twinkle is not None else 0.4,
            "connect": element.connect or 0.0,
            "color": element.color or "accent",
        }
    return out


def reveal_end(elements: Sequence[dict]) -> float:
    """Seconds into the scene when its last element is in (the quality check measures what is left to read it)."""
    last = 0.0
    for element in elements:
        enter = element.get("enter")
        if not enter:
            last = max(last, float(element.get("start") or 0.0))
            continue
        parts = _parts_of(element)
        last = max(last, enter["at"] + enter["duration"] + max(parts - 1, 0) * enter.get("stagger", 0.0))
    return round(last, 4)


def _parts_of(element: dict) -> int:
    """How many parts a split text comes in as (an estimate the page refines: its lines are laid out there)."""
    text = element.get("text") or {}
    split = text.get("split")
    words = [w for w in str(text.get("text") or "").replace("*", "").split() if w]
    if split == "letters":
        return max(1, sum(len(w) for w in words))
    if split == "words":
        return max(1, len(words))
    if split == "lines":
        return max(1, str(text.get("text") or "").count("\n") + 1)
    return 1


def snap_to_beats(elements: list[dict], beats: Sequence[float], *, tolerance: float) -> int:
    """Move each element's enter onto the nearest beat within *tolerance* seconds (``beat_sync: all``); how many moved."""
    if not beats:
        return 0
    moved = 0
    for element in elements:
        enter = element.get("enter")
        if not enter:
            continue
        beat = min(beats, key=lambda b: abs(b - enter["at"]))
        if abs(beat - enter["at"]) <= tolerance and beat >= 0.0:
            if beat != enter["at"]:
                moved += 1
            enter["at"] = round(beat, 4)
    return moved


# ── The compositions ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class Frame:
    """The design canvas a composition is laid out on."""

    width: int
    height: int

    @property
    def portrait(self) -> bool:
        return self.height > self.width

    @property
    def type_scale(self) -> float:
        """How much smaller display type is set on a frame narrower than 16:9: a line still holds a few words."""
        return min(1.0, (self.width / self.height) / (16 / 9)) ** 0.35

    @property
    def small_scale(self) -> float:
        """The same for body type, which shrinks less: it stays readable on a phone."""
        return self.type_scale**0.5

    def square(self, share_of_short: float) -> tuple[float, float]:
        """``(width, height)`` shares of a square *share_of_short* of the frame's short side."""
        side = min(self.width, self.height) * share_of_short
        return side / self.width, side / self.height


def _model(data: dict) -> MotionElement:
    return MotionElement.model_validate(data)


def _enter(preset: str, at: float, **extra: Any) -> dict:
    return {"preset": preset, "at": round(max(0.0, at), 4), **extra}


def _title_enter(tokens: dict, at: float) -> dict:
    """How a composition's title comes in: the tokens' title style, as a preset."""
    style = tokens["title"]["style"]
    preset = {"rise": "rise", "pop": "pop", "blur": "blur", "mask": "mask_up"}.get(style, "rise")
    return _enter(preset, at, duration=tokens["title"]["duration"], stagger=tokens["title"]["stagger"])


def _title_parts(tokens: dict, text: str) -> int:
    split = tokens["title"]["split"]
    words = [w for w in text.replace("*", "").split() if w]
    if split == "letters":
        return max(1, sum(len(w) for w in words))
    if split == "words":
        return max(1, len(words))
    return max(1, text.count("\n") + 1)


def _title_end(tokens: dict, at: float, text: str) -> float:
    return at + tokens["title"]["duration"] + (_title_parts(tokens, text) - 1) * tokens["title"]["stagger"]


def _logo_enter(tokens: dict, at: float) -> dict:
    mode = tokens["card"]["logo"]
    preset = {"rise": "rise", "fade": "fade", "scale": "pop", "mask": "mask_right"}.get(mode, "rise")
    return _enter(preset, at, duration=0.6 * tokens["time"] if mode != "mask" else 0.9 * tokens["time"])


def _kicker_enter(tokens: dict, at: float) -> dict:
    mode = tokens["card"]["kicker"]
    preset = {"track": "track", "fade": "fade", "rise": "rise"}.get(mode, "track")
    return _enter(preset, at, duration=0.55 * tokens["time"])


def _glow(frame: Frame, *, x: float = 0.5, y: float = 0.5, size: float = 0.9, color: str = "accent") -> dict:
    w, h = frame.square(size)
    return {
        "id": "glow",
        "kind": "shape",
        "shape": "ellipse",
        "fill": color,
        "x": x,
        "y": y,
        "width": w,
        "height": h,
        "opacity": 0.16,
        "blur": 90.0,
        "enter": _enter("fade", 0.0, duration=1.2),
        "loop": {"preset": "breathe", "period": 6.0, "amount": 0.35},
    }


def _camera(tokens: dict, length: float) -> dict | None:
    """The slow push every composition moves with: deeper for a cinematic brand, turning slightly in 3D."""
    drift = float(tokens["card"]["drift"])
    if drift <= 0.0:
        return None
    keys = [{"t": 0.0, "zoom": 1.0}, {"t": round(max(length, 0.1), 4), "zoom": round(1.0 + drift * 1.6, 4), "ease": "linear"}]
    if tokens["personality"] == "cinematic":
        keys[0].update({"rotate_y": -3.0, "rotate_x": 1.5})
        keys[1].update({"rotate_y": 2.0, "rotate_x": -0.5})
    return {"perspective": 1600.0, "keys": keys}


def _texts_block(
    scene: MotionScene, tokens: dict, frame: Frame, *, x: float, anchor: str, top: float, title_size: float
) -> tuple[list[dict], float]:
    """Kicker, title and subtitle stacked from *top*, each under the one before; the elements and when the title is in."""
    t = tokens["time"]
    out: list[dict] = []
    above: str | None = None
    if scene.kicker:
        out.append(
            {"id": "kicker", "kind": "text", "role": "kicker", "text": scene.kicker, "x": x, "y": top, "anchor": anchor,
             "size": round(24 * frame.small_scale, 1), "enter": _kicker_enter(tokens, 0.12 * t)}
        )
        above = "kicker"
    title_at = 0.22 * t
    title_end = title_at
    if scene.title:
        out.append(
            {"id": "title", "kind": "text", "role": "headline", "text": scene.title, "x": x, "y": top, "anchor": anchor,
             "size": round(title_size * frame.type_scale, 1), "max_width": 0.84, "split": tokens["title"]["split"],
             "enter": _title_enter(tokens, title_at), **({"below": above, "gap": 0.025} if above else {})}
        )
        title_end = _title_end(tokens, title_at, scene.title)
        above = "title"
    if scene.subtitle:
        out.append(
            {"id": "subtitle", "kind": "text", "role": "body", "text": scene.subtitle, "x": x, "y": top, "anchor": anchor,
             "size": round(34 * frame.small_scale, 1), "max_width": 0.7, "enter": _enter("rise", title_end + 0.15 * t),
             **({"below": above, "gap": 0.035} if above else {})}
        )
        above = "subtitle"
    return out, title_end


def _kinetic_title(scene: MotionScene, tokens: dict, frame: Frame, theme: dict, length: float) -> list[dict]:
    t = tokens["time"]
    center = scene.align != "left"
    x, anchor = (0.5, "top") if center else (0.09, "top_left")
    out = [_glow(frame, x=0.5 if center else 0.3)]
    if scene.show_logo and theme.get("logo"):
        w, h = frame.square(0.06)
        out.append({"id": "logo", "kind": "image", "asset": "logo", "x": x, "y": 0.16, "anchor": anchor, "height": h,
                    "width": min(0.5, w * 6), "enter": _logo_enter(tokens, 0.05 * t)})
    texts, title_end = _texts_block(scene, tokens, frame, x=x, anchor=anchor, top=0.32 if not scene.kicker else 0.28,
                                    title_size=132)
    out.extend(texts)
    if tokens["card"]["rule"] == "draw" and not center:
        out.append({"id": "rule", "kind": "shape", "shape": "line", "stroke": "gradient", "stroke_width": 4.0,
                    "x": 0.09, "y": 0.8, "anchor": "top_left", "width": 0.12, "height": 0.004,
                    "below": texts[-1]["id"], "gap": 0.05,
                    "enter": _enter("draw", title_end, duration=0.8 * t, ease="move")})
    if tokens["card"]["accent"] == "sweep" and scene.title:
        title = next(element for element in out if element["id"] == "title")
        title["loop"] = {"preset": "shimmer", "period": 3.6, "amount": 0.6, "at": round(title_end + 0.2 * t, 4)}
    return out


def _logo_reveal(scene: MotionScene, tokens: dict, frame: Frame, theme: dict, length: float) -> list[dict]:
    t = tokens["time"]
    ring_w, ring_h = frame.square(0.62)
    out = [
        _glow(frame, size=0.8),
        {"id": "ring", "kind": "shape", "shape": "ring", "stroke": "gradient", "stroke_width": 3.0, "x": 0.5, "y": 0.46,
         "width": ring_w, "height": ring_h, "enter": _enter("draw", 0.1 * t, duration=1.1 * t, ease="move"),
         "keys": [{"t": round(1.15 * t, 4), "scale": 1.0, "opacity": 1.0},
                  {"t": round(2.0 * t, 4), "scale": 1.35, "opacity": 0.0, "ease": "out"}]},
    ]
    has_logo = bool(theme.get("logo"))
    tagline = scene.title if has_logo else scene.subtitle
    if has_logo:
        w, h = frame.square(0.16)
        out.append({"id": "logo", "kind": "image", "asset": "logo", "x": 0.5, "y": 0.46, "width": min(0.6, w * 4.5),
                    "height": h, "enter": _logo_enter(tokens, 0.45 * t),
                    "loop": {"preset": "shimmer", "period": 3.4, "amount": 0.7, "at": round(1.5 * t, 4)}})
        tagline_at = 1.05 * t
    else:
        wordmark = scene.title or ""
        if wordmark:
            out.append({"id": "wordmark", "kind": "text", "role": "headline", "text": wordmark, "x": 0.5, "y": 0.46,
                        "size": round(120 * frame.type_scale, 1), "max_width": 0.8, "split": "letters",
                        "enter": _title_enter(tokens, 0.45 * t)})
        tagline_at = _title_end(tokens, 0.45 * t, wordmark or " ") + 0.1 * t
    if tagline:
        out.append({"id": "tagline", "kind": "text", "role": "label", "text": tagline, "x": 0.5, "y": 0.74, "anchor": "top",
                    "size": round(40 * frame.small_scale, 1), "max_width": 0.8, "split": tokens["title"]["split"],
                    "enter": _enter("rise", tagline_at, duration=tokens["title"]["duration"],
                                    stagger=round(tokens["title"]["stagger"] * 0.7, 4))})
    if scene.kicker:
        out.append({"id": "kicker", "kind": "text", "role": "kicker", "text": scene.kicker, "x": 0.5, "y": 0.16,
                    "anchor": "top", "enter": _kicker_enter(tokens, 0.2 * t)})
    return out


#: A ring drawn from its top, clockwise, in a 100 x 100 box.
_RING_D = "M50 4 A46 46 0 1 1 49.99 4"


def _stat_counter(scene: MotionScene, tokens: dict, frame: Frame, theme: dict, length: float) -> list[dict]:
    t = tokens["time"]
    w, h = frame.square(0.5)
    cy = 0.47
    out = [
        _glow(frame, y=cy, size=0.7),
        {"id": "track", "kind": "shape", "shape": "ring", "stroke": "muted", "stroke_width": 10.0, "x": 0.5, "y": cy,
         "width": w, "height": h, "opacity": 0.22, "enter": _enter("fade", 0.0, duration=0.5 * t)},
        {"id": "arc", "kind": "path", "d": _RING_D, "view_box": [100.0, 100.0], "stroke": "gradient", "stroke_width": 10.0,
         "x": 0.5, "y": cy, "width": w, "height": h,
         "keys": [{"t": round(0.3 * t, 4), "draw": 0.0}, {"t": round(1.9 * t, 4), "draw": 1.0, "ease": "out_expo"}]},
        {"id": "figure", "kind": "text", "role": "figure", "text": scene.figure, "x": 0.5, "y": cy, "count": True,
         "size": round(150 * frame.type_scale, 1), "max_width": round(w * 0.86, 4),
         "enter": _enter("pop" if tokens["title"]["style"] == "pop" else "rise", 0.3 * t, duration=0.7 * t)},
    ]
    if scene.kicker:
        out.append({"id": "kicker", "kind": "text", "role": "kicker", "text": scene.kicker, "x": 0.5,
                    "y": round(max(0.04, cy - h / 2 - 0.05), 4), "anchor": "bottom", "enter": _kicker_enter(tokens, 0.1 * t)})
    if scene.label:
        out.append({"id": "label", "kind": "text", "role": "label", "text": scene.label, "x": 0.5,
                    "y": round(cy + h / 2 + 0.05, 4), "anchor": "top", "size": round(40 * frame.small_scale, 1),
                    "max_width": 0.8, "color": "muted", "enter": _enter("rise", 1.3 * t)})
    return out


def _list_build(scene: MotionScene, tokens: dict, frame: Frame, theme: dict, length: float) -> list[dict]:
    t = tokens["time"]
    x = 0.1 if not frame.portrait else 0.08
    out: list[dict] = []
    above: str | None = None
    if scene.kicker:
        out.append({"id": "kicker", "kind": "text", "role": "kicker", "text": scene.kicker, "x": x, "y": 0.16,
                    "anchor": "top_left", "enter": _kicker_enter(tokens, 0.1 * t)})
        above = "kicker"
    if scene.title:
        out.append({"id": "title", "kind": "text", "role": "title", "text": scene.title, "x": x, "y": 0.16,
                    "anchor": "top_left", "size": round(72 * frame.type_scale, 1), "max_width": 0.8,
                    "split": tokens["title"]["split"], "enter": _title_enter(tokens, 0.2 * t),
                    **({"below": above, "gap": 0.02} if above else {})})
        above = "title"
    start = (_title_end(tokens, 0.2 * t, scene.title) if scene.title else 0.3 * t) + 0.1 * t
    gap = tokens["block"]["step"] * 0.8
    preset = "slide_right" if tokens["personality"] in ("energetic", "playful") else "rise"
    for index, item in enumerate(scene.items):
        element = {"id": f"item-{index + 1}", "kind": "text", "role": "label", "text": item, "x": x, "y": 0.4,
                   "anchor": "top_left", "size": round(42 * frame.small_scale, 1), "max_width": 0.8, "marker": "dot",
                   "enter": _enter(preset, start + index * gap)}
        if above:
            element.update({"below": above, "gap": 0.06 if index == 0 else 0.03})
        out.append(element)
        above = element["id"]
    return out


def _split_statement(scene: MotionScene, tokens: dict, frame: Frame, theme: dict, length: float) -> list[dict]:
    t = tokens["time"]
    count = len(scene.lines)
    size = 104 * frame.type_scale
    line_h = size * 1.25 / frame.height
    top = 0.5 - line_h * count / 2
    out = [_glow(frame, size=0.75)]
    step = tokens["block"]["step"]
    above: str | None = None
    for index, line in enumerate(scene.lines):
        element = {"id": f"line-{index + 1}", "kind": "text", "role": "headline", "text": line, "x": 0.5,
                   "y": round(top, 4), "anchor": "top", "size": round(size, 1), "max_width": 0.88,
                   "enter": _enter("mask_up" if index % 2 == 0 else "mask_down", 0.2 * t + index * step,
                                   duration=tokens["block"]["duration"])}
        if above:
            element.update({"below": above, "gap": 0.012})
        out.append(element)
        above = element["id"]
    if scene.subtitle:
        out.append({"id": "subtitle", "kind": "text", "role": "body", "text": scene.subtitle, "x": 0.5, "y": 0.8,
                    "anchor": "top", "max_width": 0.7, "below": above, "gap": 0.045,
                    "enter": _enter("fade", 0.2 * t + count * step + 0.2 * t)})
    return out


#: Two quotation marks in a 100 x 80 box.
_QUOTE_D = (
    "M0 80 L0 45 C0 20 12 4 36 0 L40 12 C26 16 20 26 20 40 L40 40 L40 80 Z "
    "M56 80 L56 45 C56 20 68 4 92 0 L96 12 C82 16 76 26 76 40 L96 40 L96 80 Z"
)


def _quote(scene: MotionScene, tokens: dict, frame: Frame, theme: dict, length: float) -> list[dict]:
    t = tokens["time"]
    center = scene.align != "left"
    x, anchor = (0.5, "top") if center else (0.11, "top_left")
    mark_w, mark_h = frame.square(0.1)
    at = 0.3 * t
    words = len([w for w in (scene.title or "").split() if w])
    end = at + tokens["title"]["duration"] + max(words - 1, 0) * tokens["block"]["word"]
    out = [
        {"id": "mark", "kind": "path", "d": _QUOTE_D, "view_box": [100.0, 80.0], "fill": "gradient", "stroke": "none",
         "x": x, "y": 0.18, "anchor": anchor, "width": mark_w, "height": round(mark_h * 0.8, 4),
         "enter": _enter("pop", 0.05 * t, duration=0.6 * t)},
        {"id": "quote", "kind": "text", "role": "title", "text": scene.title, "x": x, "y": 0.32, "anchor": anchor,
         "size": round(62 * frame.type_scale, 1), "weight": 600, "max_width": 0.78, "split": "words", "below": "mark",
         "gap": 0.04, "enter": _enter("rise", at, duration=tokens["title"]["duration"], stagger=tokens["block"]["word"])},
    ]
    if scene.subtitle:
        out.append({"id": "rule", "kind": "shape", "shape": "line", "stroke": "accent", "stroke_width": 3.0, "x": x,
                    "y": 0.8, "anchor": anchor, "width": 0.04, "height": 0.003, "below": "quote", "gap": 0.05,
                    "enter": _enter("draw", end, duration=0.5 * t, ease="move")})
        out.append({"id": "author", "kind": "text", "role": "label", "text": scene.subtitle, "x": x, "y": 0.83,
                    "anchor": anchor, "color": "muted", "max_width": 0.7, "below": "rule", "gap": 0.02,
                    "enter": _enter("rise", end + 0.2 * t)})
    return out


def _constellation(scene: MotionScene, tokens: dict, frame: Frame, theme: dict, length: float) -> list[dict]:
    t = tokens["time"]
    out = [
        {"id": "field", "kind": "particles", "number": 70 if frame.portrait else 96, "seed": 11, "connect": 0.12,
         "drift": 0.35, "twinkle": 0.5, "size": 3.0, "color": "accent", "x": 0.5, "y": 0.5, "width": 1.0, "height": 1.0,
         "enter": _enter("fade", 0.0, duration=1.4 * t)},
        _glow(frame, size=0.7),
    ]
    center = scene.align != "left"
    x, anchor = (0.5, "top") if center else (0.09, "top_left")
    texts, _end = _texts_block(scene, tokens, frame, x=x, anchor=anchor, top=0.36 if not scene.kicker else 0.32,
                               title_size=120)
    out.extend(texts)
    return out


_COMPOSERS = {
    "kinetic_title": _kinetic_title,
    "logo_reveal": _logo_reveal,
    "stat_counter": _stat_counter,
    "list_build": _list_build,
    "split_statement": _split_statement,
    "quote": _quote,
    "constellation": _constellation,
}


def compose(scene: MotionScene, tokens: dict, *, frame: Frame, theme: dict, length: float) -> dict:
    """What a motion scene draws, for the page: its composition laid out, then its own elements over it.

    ``{"elements", "camera", "brands", "reveal_end"}`` - ``brands`` when an
    element draws a brand mark (the watermark leaves the stage to it, as to a
    card's logo).
    """
    laid: list[MotionElement] = []
    if scene.composition is not None:
        laid = [_model(data) for data in _COMPOSERS[scene.composition](scene, tokens, frame, theme, length)]
    elements = [element_payload(element, tokens, length=length) for element in [*laid, *scene.elements]]
    if scene.camera is not None:
        camera = {
            "perspective": scene.camera.perspective,
            "keys": [key.model_dump(mode="json", exclude_none=True) for key in scene.camera.keys],
        }
    else:
        camera = _camera(tokens, length) if scene.composition is not None else None
    if camera is not None:
        for key in camera["keys"]:
            key.setdefault("ease", "move")
    brands = any(element["kind"] == "image" and (element.get("image") or {}).get("brand") for element in elements)
    return {"elements": elements, "camera": camera, "brands": brands, "reveal_end": reveal_end(elements)}


def drawn_texts(scene: MotionScene) -> list[str]:
    """The texts a motion scene writes, for its name in the editor and the chapters."""
    texts = [scene.title, *scene.lines, scene.figure, *scene.items]
    texts.extend(element.text for element in scene.elements if element.kind == "text")
    return [text for text in texts if text]


def overlay_pictures(scenes: Iterable[Any]) -> list[MotionElement]:
    """The image elements of the motion scenes of *scenes* served as an image overlay's picture (not the cards' logo)."""
    return [
        element
        for scene in scenes
        if getattr(scene, "type", None) == "motion"
        for element in scene.elements
        if element.kind == "image" and (picture_key(element) or "").startswith("overlay/")
    ]


def lottie_files(scenes: Iterable[Any]) -> dict[str, str]:
    """``key -> file id`` of every Lottie file the motion scenes of *scenes* play."""
    return {
        lottie_key(str(element.file_id)): str(element.file_id)
        for scene in scenes
        if getattr(scene, "type", None) == "motion"
        for element in scene.elements
        if element.kind == "lottie" and element.file_id
    }


# ── Lottie files ──────────────────────────────────────────────────────────

#: The largest Lottie file a motion scene plays, and the most layers.
MAX_LOTTIE_BYTES = 2 * 1024 * 1024
MAX_LOTTIE_LAYERS = 400
_DATA_URI = re.compile(r"^data:image/(png|jpeg|jpg|webp|gif|svg\+xml);base64,", re.IGNORECASE)


@dataclass(frozen=True)
class LottieCheck:
    """What :func:`check_lottie` found: a refusal (``problem``) or the file's facts and what it will not play."""

    problem: str | None = None
    width: int = 0
    height: int = 0
    seconds: float = 0.0
    notes: tuple[str, ...] = ()


def _has_expressions(node: Any) -> bool:
    if isinstance(node, dict):
        if isinstance(node.get("x"), str) and ("k" in node or "a" in node):
            return True
        return any(_has_expressions(value) for value in node.values())
    if isinstance(node, list):
        return any(_has_expressions(item) for item in node)
    return False


def check_lottie(data: bytes) -> LottieCheck:
    """Whether *data* is a Lottie animation a motion scene may play: refused when it loads anything from outside itself.

    The page plays it with ``lottie_light``: no expression is evaluated (a file
    that has some plays without them, and says so), and nothing the file names
    is fetched - its pictures are embedded (``data:`` URIs) and its fonts are
    the page's or drawn as glyphs, or the file is refused.
    """
    if len(data) > MAX_LOTTIE_BYTES:
        return LottieCheck(f"the Lottie file is {len(data) / 1048576:.1f} MB; a motion scene plays {MAX_LOTTIE_BYTES // 1048576} MB at most")
    try:
        doc = json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError):
        return LottieCheck("the file is not a Lottie animation (Lottie JSON, exported from After Effects with Bodymovin)")
    if not isinstance(doc, dict) or not isinstance(doc.get("layers"), list):
        return LottieCheck("the file is not a Lottie animation: it has no layers")
    try:
        width, height = int(doc.get("w") or 0), int(doc.get("h") or 0)
        rate = float(doc.get("fr") or 0)
        first, last = float(doc.get("ip") or 0), float(doc.get("op") or 0)
    except (TypeError, ValueError):
        return LottieCheck("the Lottie file's size, frame rate or frames are not numbers")
    if width <= 0 or height <= 0 or rate <= 0 or last <= first:
        return LottieCheck("the Lottie file has no size, frame rate or frames")
    if len(doc["layers"]) > MAX_LOTTIE_LAYERS:
        return LottieCheck(f"the Lottie file has {len(doc['layers'])} layers; a motion scene plays {MAX_LOTTIE_LAYERS} at most")
    for asset in doc.get("assets") or []:
        if not isinstance(asset, dict) or "p" not in asset or "layers" in asset:
            continue
        path = str(asset.get("p") or "")
        if not (asset.get("e") == 1 or _DATA_URI.match(path)) or str(asset.get("u") or "").strip():
            return LottieCheck(
                "the Lottie file loads a picture from outside itself; export it with its images embedded (Bodymovin: "
                "'Include in json')"
            )
    for font in ((doc.get("fonts") or {}).get("list") or []) if isinstance(doc.get("fonts"), dict) else []:
        if isinstance(font, dict) and (str(font.get("fPath") or "").strip() or font.get("origin") in (1, 2, 3)):
            return LottieCheck(
                "the Lottie file loads a font from outside itself; export its text as glyphs (Bodymovin: 'Glyphs') or "
                "convert it to shapes"
            )
    notes = ()
    if _has_expressions(doc["layers"]):
        notes = ("its expressions are not played: the animation plays its keyframes alone",)
    return LottieCheck(None, width, height, round((last - first) / rate, 4), notes)


#: Checked Lottie files by id: a file's bytes never change under its id. Bounded, oldest out.
_CHECKED: dict[str, LottieCheck] = {}
_CHECKED_MAX = 256


async def read_lottie(db: Any, file_id: object) -> LottieCheck | None:
    """The check of the Lottie file *file_id* (:func:`check_lottie`); ``None`` when there is no such file.

    Read once per process and file: the editor's preview plans every few
    seconds, the save and the render read it again only when it is new.
    """
    import uuid

    from app.models.file_attachment import FileAttachment
    from app.services.file_storage import file_storage

    key = str(file_id)
    if key in _CHECKED:
        return _CHECKED[key]
    row = await db.get(FileAttachment, uuid.UUID(key))
    if row is None:
        return None
    if (row.size or 0) > MAX_LOTTIE_BYTES:
        found = LottieCheck(f"the Lottie file is {row.size / 1048576:.1f} MB; a motion scene plays {MAX_LOTTIE_BYTES // 1048576} MB at most")
    else:
        try:
            found = check_lottie(await file_storage.download(row.s3_object_name))
        except Exception:  # unreadable now: checked again on the next read
            return LottieCheck("the Lottie file could not be read")
    if len(_CHECKED) >= _CHECKED_MAX:
        _CHECKED.pop(next(iter(_CHECKED)))
    _CHECKED[key] = found
    return found


