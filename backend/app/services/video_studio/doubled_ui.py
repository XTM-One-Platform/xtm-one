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

"""Two framings of one page drawn at once: a recording's page doubled.

A recording scene's camera that still moves when the crossfade into the next scene starts blends its moving
framing with the next scene's framing of the same page: the title bar, the tabs and the toolbar show twice,
offset, for the length of the dissolve. The planner keeps the automatic camera still before the transition out
(``timeline.camera_settled_before``); this module is the check that the plan draws no such frame, whatever made
its camera move there (an automatic camera, keyframes written by hand).

It draws the plan, never the media: a synthetic page carrying one sharp title bar (:func:`chrome_page`) through
each scene's camera as the compositor applies it - one geometric transform of one picture per frame
(:func:`view_at`: ``compositor_page.camera``; :func:`placed`: the recording scene's ``cam`` transform) - blended
over the crossfade the way the page blends the two scenes, sampled at :data:`SAMPLE_FPS`. A frame whose title bar
edge is split in two (:func:`ghost_edges`: one edge drawn as a staircase of two, the step between them a blend of
the two sides) is a page drawn twice. A clean geometric zoom moves the edge and keeps it whole.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from itertools import pairwise

import numpy as np

#: The synthetic page drawn through the plan's cameras: small enough to sample every frame of a transition.
WIDTH, HEIGHT = 320, 180
#: The page's title bar (rows) and its levels: a dark bar over a light page.
BAR_ROWS = 30
BAR_LEVEL, PAGE_LEVEL = 40.0, 240.0
#: How many frames a second of a transition are drawn and read.
SAMPLE_FPS = 30
#: An edge is a run of rows whose level changes by more than this share of the frame's range, row to row.
EDGE_SHARE = 0.08
#: A step between two edges of the same sign is a blend of its two sides when it sits this far between them (the
#: share of one framing in the blend): under the first or past the last, one framing reads, the other is a faint
#: trace.
GHOST_WEIGHTS = (0.15, 0.85)
#: Two edges further apart than this share of the frame's height are two things of the page, never one drawn twice.
GHOST_MAX_GAP = 0.2
#: The transitions that blend two pictures at once: a camera moving under one draws its page twice.
BLENDING = frozenset({"crossfade"})


def _glide(p: float) -> float:
    return p * p * p * (p * (6 * p - 15) + 10)


def view_at(frames: list, t: float) -> tuple[float, float, float]:
    """The camera's view ``(cx, cy, zoom)`` at *t* seconds into its scene, as ``compositor_page.camera`` draws it:
    eased between its keyframes ``[t, cx, cy, zoom, arc?]``, the zoom moving in log space."""
    if not frames:
        return 0.5, 0.5, 1.0
    if t <= frames[0][0]:
        return float(frames[0][1]), float(frames[0][2]), float(frames[0][3])
    if t >= frames[-1][0]:
        return float(frames[-1][1]), float(frames[-1][2]), float(frames[-1][3])
    i = max(k for k in range(len(frames)) if frames[k][0] <= t)
    a, b = frames[i], frames[i + 1]
    p = _glide((t - a[0]) / max(b[0] - a[0], 1e-6))
    arc = float(b[4]) if len(b) > 4 else 0.0
    zoom = math.exp(math.log(a[3]) + (math.log(b[3]) - math.log(a[3])) * p - arc * math.sin(math.pi * p))
    return a[1] + (b[1] - a[1]) * p, a[2] + (b[2] - a[2]) * p, zoom


def placed(view: tuple[float, float, float], width: int = WIDTH, height: int = HEIGHT) -> tuple[float, float, float]:
    """The recording scene's ``cam`` transform ``(tx, ty, zoom)`` for *view*, its footage filling the frame: the focus
    drawn toward the middle as the zoom grows, the page never pulled off an edge."""
    cx, cy, zoom = view
    fx, fy = cx * width, cy * height
    k = min(max((zoom - 1) / 0.5, 0.0), 1.0)
    tx = fx + (width / 2 - fx) * k - zoom * fx
    ty = fy + (height / 2 - fy) * k - zoom * fy
    if zoom * width >= width:
        tx = min(max(tx, width - zoom * width), 0.0)
    if zoom * height >= height:
        ty = min(max(ty, height - zoom * height), 0.0)
    return tx, ty, zoom


def chrome_page(width: int = WIDTH, height: int = HEIGHT) -> np.ndarray:
    """The synthetic page: a sharp dark title bar over a light page."""
    page = np.full((height, width), PAGE_LEVEL)
    page[:BAR_ROWS] = BAR_LEVEL
    return page


def drawn(page: np.ndarray, view: tuple[float, float, float]) -> np.ndarray:
    """*page* through the camera at *view*: one geometric transform of one picture, sampled bilinearly."""
    height, width = page.shape
    tx, ty, zoom = placed(view, width, height)
    ys, xs = np.mgrid[0:height, 0:width].astype(float)
    sx, sy = np.clip((xs - tx) / zoom, 0, width - 1), np.clip((ys - ty) / zoom, 0, height - 1)
    x0, y0 = np.floor(sx).astype(int), np.floor(sy).astype(int)
    x1, y1 = np.minimum(x0 + 1, width - 1), np.minimum(y0 + 1, height - 1)
    fx, fy = sx - x0, sy - y0
    top = page[y0, x0] * (1 - fx) + page[y0, x1] * fx
    bottom = page[y1, x0] * (1 - fx) + page[y1, x1] * fx
    return top * (1 - fy) + bottom * fy


@dataclass(frozen=True)
class Ghost:
    """One edge of a frame drawn twice: its two copies' rows, and the share in the blend of the framing whose copy of
    the edge comes first (the level of the rows between them, from one side to the other)."""

    first: int
    second: int
    weight: float


def _edges(profile: np.ndarray, threshold: float) -> list[tuple[int, int, int]]:
    """``(first row, last row, sign)`` of each run of rows whose level changes past *threshold* row to row, one way."""
    steps = np.diff(profile)
    runs: list[tuple[int, int, int]] = []
    for row, step in enumerate(steps):
        sign = 1 if step > threshold else -1 if step < -threshold else 0
        if not sign:
            continue
        if runs and runs[-1][1] == row - 1 and runs[-1][2] == sign:
            runs[-1] = (runs[-1][0], row, sign)
        else:
            runs.append((row, row, sign))
    return runs


def ghost_edges(frame: np.ndarray) -> list[Ghost]:
    """The edges of *frame* drawn twice: two edges of the same sign, close, the rows between them flat at a level that
    blends the two sides (:data:`GHOST_WEIGHTS`). Read on the rows' mean over the left third of the frame, where a
    page's title bar and side panel sit."""
    height, width = frame.shape[:2]
    profile = np.asarray(frame, dtype=float)[:, : max(1, width // 3)].mean(axis=1)
    span = float(profile.max() - profile.min())
    if span <= 1e-6:
        return []
    runs = _edges(profile, EDGE_SHARE * span)
    ghosts = []
    for (a0, a1, sign), (b0, b1, other) in pairwise(runs):
        if sign != other or b0 - a1 > GHOST_MAX_GAP * height:
            continue
        before, between, after = profile[a0], profile[a1 + 1 : b0 + 1], profile[b1 + 1]
        if after == before or float(np.ptp(between)) > EDGE_SHARE * span:
            continue
        weight = float((between.mean() - before) / (after - before))
        if GHOST_WEIGHTS[0] <= weight <= GHOST_WEIGHTS[1]:
            ghosts.append(Ghost(a1 + 1, b1 + 1, round(weight, 3)))
    return ghosts


def _frames(start: float, end: float) -> list[float]:
    return [n / SAMPLE_FPS for n in range(math.ceil(start * SAMPLE_FPS), math.floor(end * SAMPLE_FPS) + 1)]


def doubled_under_transition(first: dict, second: dict) -> list[float]:
    """The seconds into planned scene *first* of each frame of its crossfade into *second* (the planned scene after
    it, showing the same page) that draws the page twice: the two scenes' cameras drawn on :func:`chrome_page` and
    blended as the transition blends them. ``[]`` for a transition that blends nothing, or two still framings."""
    transition = second.get("transition_in") or {}
    if transition.get("type") not in BLENDING:
        return []
    start, fade_from = float(first.get("start") or 0.0), float(second.get("start") or 0.0)
    fade_until = min(float(first.get("end") or 0.0), fade_from + float(transition.get("duration") or 0.0))
    if fade_until <= fade_from:
        return []
    page = chrome_page()
    found = []
    for at in _frames(fade_from, fade_until):
        out = drawn(page, view_at(first.get("camera") or [], at - start))
        into = drawn(page, view_at(second.get("camera") or [], at - fade_from))
        p = (at - fade_from) / (fade_until - fade_from)
        if ghost_edges(out * (1 - p) + into * p):
            found.append(round(at - start, 3))
    return found
