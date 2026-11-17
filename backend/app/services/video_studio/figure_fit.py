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

"""A figure scene judged as it is drawn: its drift and its stand in space applied, kept inside the frame.

The figure page builds a figure in (``figure-build.ts``): the camera frames it
whole once every step is in, then - ``motion: drift`` - zooms in on it about the
middle of the room the subtitles leave. The compositor stands it in space
(``perspective: tilt | orbit``, ``compositor_page.buildStill``): scaled, turned
about the frame's middle, seen in perspective. On the last seconds of a scene
the two add up; the save measured the figure flat and still and said every text
fit while a node was past the frame's edge.

So a figure is judged here as it is drawn, in plain geometry: the framing and
the drift as the page applies them (:func:`framing`, :func:`drifted`), the
stand as the compositor's CSS transform projects it (:func:`project`, from
:data:`STANDS` - the numbers the compositor itself reads), at every pose of its
drift and at its last frame (:func:`fit`). Its drift is capped so its box keeps
:data:`FIT_MARGIN` of the frame clear on every side; a cap under
:data:`MIN_DRIFT` is not seen, and the figure holds still instead (``motion:
none`` for its drawing). The render and the editor's build plan the figure with
that drift (``figures._plan_with``), a save says it (:meth:`FigureFit.line`)
and the quality check finds a figure whose drawn box still leaves the frame
(``quality.content_findings``).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

#: What the figure page frames and moves by (``figure-build.ts``: ``FRAME_MARGIN``, ``DRIFT_ZOOM``, ``CAMERA_MOVE``,
#: ``STACKED_STEPS``): the margin around the whole figure, how far it drifts in by the scene's end (1.00 to 1.06),
#: the camera's move to a step, and the share two steps stand on one another past.
FRAME_MARGIN = 0.07
DRIFT_ZOOM = 0.06
CAMERA_MOVE = 0.6
STACKED_STEPS = 0.5
#: The share of the frame a figure's box keeps clear on every side, at every pose of its drift.
FIT_MARGIN = 0.03
#: A drift capped under this is not seen (its edges move about 13 px of a 1080p frame): the figure holds still.
MIN_DRIFT = 0.015
#: How a figure stands in space (``FigureScene.perspective``), as the compositor draws it: its turns (``rotateX``,
#: ``rotateY``, degrees) at the scene's start and at its end, eased by the brand's ``move`` token, and its scale.
STANDS: dict[str, dict] = {
    "tilt": {"from": [12.0, -18.0], "to": [4.0, -6.0], "scale": 0.93},
    "orbit": {"from": [5.0, -16.0], "to": [5.0, 16.0], "scale": 0.92},
}
#: The compositor's perspective distance, a share of the frame's longer side.
PERSPECTIVE = 1.1
#: The poses read across a drift, both ends included.
SAMPLES = 24
#: The easing a brand moves its camera with when none is known (``motion_design``'s ``precise`` personality).
DEFAULT_EASE = "in_out"

Rect = tuple[float, float, float, float]


def page_stands() -> dict:
    """What the compositor page stands a figure with (:data:`STANDS`, :data:`PERSPECTIVE`): one table for both."""
    return {"perspective": PERSPECTIVE, "poses": STANDS}


def ease(name: str, u: float) -> float:
    """The compositor's easing *name* at *u* (``compositor_page``'s ``EASES``): the monotone ones a camera moves with,
    its default (``out``) for any other."""
    u = min(1.0, max(0.0, u))
    if name == "linear":
        return u
    if name == "in":
        return u**3
    if name == "in_out":
        return 4 * u**3 if u < 0.5 else 1 - (-2 * u + 2) ** 3 / 2
    if name == "out_quint":
        return 1 - (1 - u) ** 5
    if name == "out_expo":
        return 1.0 if u >= 1 else 1 - 2 ** (-10 * u)
    if name == "in_out_expo":
        if u <= 0 or u >= 1:
            return u
        return 2 ** (20 * u - 10) / 2 if u < 0.5 else (2 - 2 ** (-20 * u + 10)) / 2
    return 1 - (1 - u) ** 3


def move_ease(motion: object) -> str:
    """The easing the brand's ``move`` token names (``motion_design.resolve_motion``) for a spec's ``theme.motion``."""
    from app.services.video_studio.motion_design import resolve_motion

    try:
        return str((resolve_motion(motion).get("ease") or {}).get("move") or DEFAULT_EASE)
    except Exception:  # an unreadable style moves like the default brand
        return DEFAULT_EASE


def stand_at(perspective: str, eased: str, t: float, length: float) -> tuple[float, float, float] | None:
    """``(rotate x, rotate y, scale)`` of a figure stood *perspective* at *t* seconds of a scene of *length*; ``None``
    for one that faces the camera."""
    stand = STANDS.get(perspective)
    if stand is None:
        return None
    e = ease(eased, t / max(length, 0.1))
    (x0, y0), (x1, y1) = stand["from"], stand["to"]
    return x0 + (x1 - x0) * e, y0 + (y1 - y0) * e, float(stand["scale"])


def project(point: tuple[float, float], pose: tuple[float, float, float], frame: tuple[float, float]) -> tuple[float, float]:
    """*point* of the frame where the compositor draws *pose* (``scale(s) rotateX(x) rotateY(y)`` about the frame's
    middle, under the layer's perspective of :data:`PERSPECTIVE` times its longer side)."""
    width, height = frame
    rx, ry, scale = pose
    distance = round(max(width, height) * PERSPECTIVE)
    x, y = point[0] - width / 2, point[1] - height / 2
    a, b = math.radians(ry), math.radians(rx)
    x1, z1 = math.cos(a) * x, -math.sin(a) * x
    y2 = math.cos(b) * y - math.sin(b) * z1
    z2 = math.sin(b) * y + math.cos(b) * z1
    k = distance / max(distance - z2, 1e-6)
    return width / 2 + scale * x1 * k, height / 2 + scale * y2 * k


def framing(rect: Rect, viewport: tuple[float, float], safe: dict) -> tuple[float, float, float]:
    """``(scale, x, y)`` the page puts *rect* (canvas px) in the middle of the room *safe* leaves with
    (``figure-build.ts``: ``framing``)."""
    width, height = viewport
    top, bottom = float(safe.get("top") or 0.0), float(safe.get("bottom") or 0.0)
    room = max(1.0, height - top - bottom)
    scale = min(width * (1 - 2 * FRAME_MARGIN) / max(rect[2], 1.0), room * (1 - 2 * FRAME_MARGIN) / max(rect[3], 1.0))
    cx, cy = rect[0] + rect[2] / 2, rect[1] + rect[3] / 2
    return scale, width / 2 - cx * scale, top + room / 2 - cy * scale


def drifted(view: tuple[float, float, float], viewport: tuple[float, float], safe: dict, zoom: float) -> tuple[float, float, float]:
    """*view* zoomed *zoom* times about the middle of the room (``figure-build.ts``: ``drifted``)."""
    if zoom == 1:
        return view
    width, height = viewport
    top, bottom = float(safe.get("top") or 0.0), float(safe.get("bottom") or 0.0)
    cx, cy = width / 2, top + max(1.0, height - top - bottom) / 2
    scale, x, y = view
    return scale * zoom, cx - (cx - x) * zoom, cy - (cy - y) * zoom


def drawn_box(
    rect: Rect,
    viewport: tuple[float, float],
    safe: dict,
    zoom: float,
    pose: tuple[float, float, float] | None,
    *,
    part: Rect | None = None,
) -> tuple[float, float, float, float]:
    """``(x0, y0, x1, y1)`` of the frame the figure *rect* covers - or *part* of it, a node, a legend line - framed
    whole, drifted *zoom* times, stood at *pose*."""
    scale, tx, ty = drifted(framing(rect, viewport, safe), viewport, safe, zoom)
    shown = part or rect
    corners = [(tx + x * scale, ty + y * scale) for x in (shown[0], shown[0] + shown[2]) for y in (shown[1], shown[1] + shown[3])]
    if pose is not None:
        corners = [project(corner, pose, viewport) for corner in corners]
    xs, ys = [c[0] for c in corners], [c[1] for c in corners]
    return min(xs), min(ys), max(xs), max(ys)


def clearance(box: tuple[float, float, float, float], viewport: tuple[float, float]) -> tuple[float, str]:
    """The least share of the frame *box* keeps clear, and on which side; negative past the frame."""
    width, height = viewport
    sides = {"left": box[0] / width, "top": box[1] / height, "right": 1 - box[2] / width, "bottom": 1 - box[3] / height}
    side = min(sides, key=sides.__getitem__)
    return sides[side], side


def _smooth(u: float) -> float:
    return u * u * (3 - 2 * u)


def drift_zoom(t: float, start: float, length: float, amount: float) -> float:
    """The drift's zoom at *t* (``figure-build.ts``: ``driftAt``): 1 until *start*, eased up to ``1 + amount``."""
    if length - start <= 1e-6 or t <= start:
        return 1.0
    return 1 + amount * _smooth(min(1.0, (t - start) / (length - start)))


def _shared(a: Rect, b: Rect) -> float:
    w = min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0])
    h = min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1])
    return w * h / max(1.0, a[2] * a[3], b[2] * b[3]) if w > 0 and h > 0 else 0.0


def camera_follows(camera: str, steps: Sequence[Rect], length: float, hold: float, *, whole: bool = False) -> bool:
    """Whether the page's camera follows the steps (``figure-build.ts``: ``cameraKeyframes``): a camera told to, two
    steps at least that do not stand on one another, and room to move between them before the scene ends whole."""
    if whole or camera == "still" or len(steps) < 2:
        return False
    if any(_shared(steps[i], steps[i - 1]) > STACKED_STEPS for i in range(1, len(steps))):
        return False
    return length - hold - CAMERA_MOVE >= CAMERA_MOVE


def drift_start(times: Sequence[float], settles: Sequence[float], length: float, *, follows: bool, hold: float) -> float:
    """When the figure starts drifting (``figure-build.ts``: ``driftFrom``): once the camera is back on the whole
    figure after following its steps (*hold* seconds before the end), else once every step has settled."""
    if follows:
        return min(length, max(0.0, length - hold))
    built = max((t + (settles[i] if i < len(settles) else 0.6) for i, t in enumerate(times)), default=0.0)
    return min(length, built)


@dataclass(frozen=True)
class FigureFit:
    """A figure as it is drawn in motion (:func:`fit`): the drift it plays and the frame it keeps clear."""

    #: The drift it is drawn with (``1 + drift`` by the scene's end), and the drift its ``motion`` asks.
    drift: float
    asked: float
    #: The least share of the frame its box keeps clear, at the pose that keeps least: the side and the second.
    margin: float
    side: str
    at: float
    perspective: str = "flat"
    #: Its drift capped under :data:`MIN_DRIFT`: drawn holding still (``motion: none``).
    still: bool = False
    #: Shares of the frame's height: from where to where the built figure spans, framed whole before it drifts,
    #: and the band it is centred in (what its ``safe`` leaves: the safe margin to the caption pill's top).
    spans: tuple[float, float] = (0.0, 1.0)
    within: tuple[float, float] = (0.0, 1.0)

    @property
    def capped(self) -> bool:
        return self.drift < self.asked - 1e-9

    @property
    def leaves(self) -> bool:
        """Its drawn box crosses the frame's edge."""
        return self.margin < 0

    @property
    def motion(self) -> str:
        """The ``motion`` the figure page draws it with."""
        return "drift" if self.drift > 0 else "none"

    def extent(self) -> str:
        """``Its figure spans 14% to 72% of the frame's height, centred between 4% and 83%.``: built and framed
        whole, before it drifts, in the band its subtitles leave."""
        top, bottom = self.spans
        return (
            f"Its figure spans {top:.0%} to {bottom:.0%} of the frame's height, centred between {self.within[0]:.0%} "
            f"and {self.within[1]:.0%}."
        )

    def line(self, scene_id: str) -> str:
        """What a save says of it: the drift it plays and the frame its box keeps clear, or why it holds still, then
        where it spans (:meth:`extent`)."""
        return f"{self._motion_line(scene_id)} {self.extent()}"

    def _motion_line(self, scene_id: str) -> str:
        stood = f"stood {self.perspective}" if self.perspective != "flat" else "facing the camera"
        closest = f"its {self.side} edge {self.margin:.1%} inside the frame at {self.at:.1f} s"
        if self.leaves:
            closest = f"its {self.side} edge {-self.margin:.1%} past the frame at {self.at:.1f} s"
        if self.still:
            return (
                f"Scene {scene_id}: its figure holds still once built (drawn with motion none): {stood}, a drift that "
                f"keeps it {FIT_MARGIN:.0%} inside the frame on every side would be under {MIN_DRIFT:.1%}, too little to "
                f"see; {closest}."
            )
        if not self.asked:
            return f"Scene {scene_id}: its figure holds still once built (motion none), {stood}: {closest}."
        if self.capped:
            return (
                f"Scene {scene_id}: its figure's drift is capped at {self.drift:.1%} (of {self.asked:.0%}) so it stays "
                f"{FIT_MARGIN:.0%} inside the frame on every side, {stood}: {closest}, as it is drawn."
            )
        return (
            f"Scene {scene_id}: its figure drifts in {self.drift:.1%}, {stood}, inside the frame at every pose of its "
            f"drift: {closest}."
        )

    def finding(self) -> str:
        """What the quality check says of a figure whose drawn box crosses the frame's edge (:attr:`leaves`)."""
        stood = f"stood {self.perspective}" if self.perspective != "flat" else "drawn"
        return (
            f"its figure, {stood} as it plays (drift {self.drift:.1%}), runs {-self.margin:.1%} of the frame past its "
            f"{self.side} edge at {self.at:.1f} s - set perspective: flat, or lay the block out narrower"
        )


def _least(
    rect: Rect,
    viewport: tuple[float, float],
    safe: dict,
    *,
    perspective: str,
    eased: str,
    length: float,
    start: float,
    amount: float,
) -> tuple[float, str, float]:
    """``(clearance, side, second)`` of the pose that keeps least of the frame clear, from *start* to the last frame."""
    worst = (math.inf, "left", length)
    for k in range(SAMPLES + 1):
        t = start + (length - start) * k / SAMPLES
        zoom = drift_zoom(t, start, length, amount)
        box = drawn_box(rect, viewport, safe, zoom, stand_at(perspective, eased, t, length))
        margin, side = clearance(box, viewport)
        if margin < worst[0]:
            worst = (margin, side, t)
    return worst


def fit(
    rect: Rect,
    *,
    viewport: tuple[float, float],
    safe: dict,
    perspective: str,
    eased: str,
    length: float,
    start: float,
    motion: str = "drift",
) -> FigureFit:
    """The drift a figure of *rect* (canvas px, what it paints) is drawn with, and the frame it keeps clear.

    From *start* - once it is built, the camera on all of it (:func:`drift_start`) - to its last frame, each pose
    is the page's framing drifted and the compositor's stand (*perspective*, its turns eased by *eased* over the
    scene's *length*). ``motion: drift`` drifts :data:`DRIFT_ZOOM`, capped so every pose keeps :data:`FIT_MARGIN`
    of the frame clear (the margin decreases as the drift grows); a cap under :data:`MIN_DRIFT` holds it still.
    """
    asked = DRIFT_ZOOM if motion == "drift" else 0.0
    shared = {"perspective": perspective, "eased": eased, "length": length, "start": start}

    def least(amount: float) -> tuple[float, str, float]:
        return _least(rect, viewport, safe, amount=amount, **shared)

    drift = asked
    if asked and least(asked)[0] < FIT_MARGIN:
        low, high = 0.0, asked
        if least(0.0)[0] < FIT_MARGIN:
            high = 0.0
        else:
            for _ in range(24):
                middle = (low + high) / 2
                low, high = (middle, high) if least(middle)[0] >= FIT_MARGIN else (low, middle)
        drift = math.floor(low * 1000) / 1000
    still = bool(asked) and drift < MIN_DRIFT
    if still:
        drift = 0.0
    margin, side, at = least(drift)
    height = float(viewport[1])
    _x0, top, _x1, bottom = drawn_box(rect, viewport, safe, 1.0, None)
    within = (float(safe.get("top") or 0.0) / height, 1.0 - float(safe.get("bottom") or 0.0) / height)
    return FigureFit(
        drift, asked, margin, side, at, perspective=perspective, still=still, spans=(top / height, bottom / height), within=within
    )


__all__ = [
    "DRIFT_ZOOM",
    "FIT_MARGIN",
    "MIN_DRIFT",
    "STANDS",
    "FigureFit",
    "camera_follows",
    "drawn_box",
    "drift_start",
    "fit",
    "move_ease",
    "page_stands",
    "project",
]
