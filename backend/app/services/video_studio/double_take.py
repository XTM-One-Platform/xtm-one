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

"""A scene preview's ``double_image`` read at the render's frame rate on a take's synthesised spans (issue #9474).

A scene preview is drawn at 30 fps; the whole render at the video's own rate, 60 fps at high and ultra. A take whose
motion was mostly synthesised from keyframes (``capture_rate.stepping``) holds frames that are a mix of two painted
instants; at 30 fps the preview steps over some of them and reads green, while the 60 fps render shows every one and
its ``double_image`` watch (``double_frames``) flags them. A green preview then said nothing of the render.

This check closes that gap without drawing the scene again: for each ``recording`` scene of the preview, over each
synthesised stretch of its take the scene plays (its stepping motion, :func:`stepping_spans`, and its page transitions
drawn again from two keyframes, :func:`paced_spans`: a dialog opening over a graph, rows replacing their skeleton),
it lists the take frames the render shows at *render_fps*, in the order it shows them (``compositor_page.takeTime``:
one take frame per output frame, at the frame's centre), decodes
those frames from the take's file and judges each against the two around it with the render's own reading
(:class:`double_frames.DoubleWatch`, same thresholds). A double take frame is a ``double_image`` finding timed in
the preview's MP4, said as read at the render's rate. Its cost is the synthesised spans alone, at the watch's
small grey size.
"""

from __future__ import annotations

import itertools
import logging
import math
from collections.abc import Callable
from typing import Any

import numpy as np

from app.services.video_studio.double_frames import READ_TYPES, REMEDY, DoubleWatch, _excused
from app.services.video_studio.render_checks import DOUBLE_IMAGE, WARN, Finding, clock

logger = logging.getLogger(__name__)

#: The grey width a take frame is read at: the frozen-picture watch's (``frame_freeze.ANALYSIS_WIDTH``).
READ_WIDTH = 192
#: The take frames before and after a synthesised stretch read with it: the frames around its first and last.
MARGIN_FRAMES = 1


def map_time(points: list, x: float) -> float:
    """``compositor_page.mapTime``: the source second at scene second *x* of a time map ``[[x, y], ...]``."""
    if not points:
        return x
    if x <= points[0][0]:
        return float(points[0][1])
    last = points[-1]
    if x >= last[0]:
        return float(last[1])
    for (a0, a1), (b0, b1) in itertools.pairwise(points):
        if a0 <= x < b0:
            return float(b1) if b0 == a0 else float(a1) + (float(b1) - float(a1)) * (x - a0) / (b0 - a0)
    return float(last[1])


def _span(entry: Any) -> tuple[float, float] | None:
    t = entry.get("t") if isinstance(entry, dict) else None
    if isinstance(t, list | tuple) and len(t) == 2 and float(t[1]) > float(t[0]):
        return float(t[0]), float(t[1])
    return None


def stepping_spans(take: Any) -> list[tuple[float, float]]:
    """The take's stretches mostly synthesised from keyframes (``TakeMedia.stepping``), in seconds of the take."""
    return [span for stretch in getattr(take, "stepping", None) or [] if (span := _span(stretch)) is not None]


def paced_spans(take: Any) -> list[tuple[float, float]]:
    """The take's page transitions drawn again from two keyframes (``transition_pacing``: a step's ``motion`` window
    kept ``paced``), in seconds of the take. Each of their frames is a blend of the keyframe before and the one after -
    a dialog fading in over a graph, loaded rows over their skeleton - so a take frame of theirs the preview's rate
    steps over can be a double image the render at its own rate shows (issue #9474, retest of #9528)."""
    spans = []
    for event in getattr(take, "events", None) or []:
        windows = event.get("motion") if isinstance(event, dict) else None
        for window in windows if isinstance(windows, list) else []:
            if isinstance(window, dict) and window.get("paced") and (span := _span(window)) is not None:
                spans.append(span)
    return spans


def synthesised_spans(take: Any) -> list[tuple[float, float]]:
    """Every stretch of the take whose frames were synthesised rather than captured: its stepping motion and its paced
    page transitions, in take order."""
    return sorted({*stepping_spans(take), *paced_spans(take)})


def shown_frames(scene: dict, render_fps: float, spans: list[tuple[float, float]]) -> list[tuple[int, int]]:
    """``(render frame, take frame)`` for each frame the render at *render_fps* draws of *scene* whose take frame
    falls in one of *spans* (take seconds, widened by :data:`MARGIN_FRAMES`); render frames counted from the
    document's start."""
    start, end = float(scene.get("start") or 0.0), float(scene.get("end") or 0.0)
    src_fps = max(float(scene.get("src_fps") or 0.0), 1.0)
    last_take = max(int(scene.get("src_frames") or 1) - 1, 0)
    time_map = scene.get("time_map") or []
    margin = MARGIN_FRAMES / src_fps
    shown: list[tuple[int, int]] = []
    for frame in range(math.ceil(start * render_fps - 1e-9), math.ceil(end * render_fps - 1e-9)):
        take_t = map_time(time_map, max(0.0, frame / render_fps - start))
        if any(a - margin <= take_t <= b + margin for a, b in spans):
            shown.append((frame, min(max(round(take_t * src_fps), 0), last_take)))
    return shown


def runs_of(shown: list[tuple[int, int]]) -> list[list[tuple[int, int]]]:
    """*shown* split where the render frames stop following each other: each run is read on its own."""
    runs: list[list[tuple[int, int]]] = []
    for pair in shown:
        if runs and pair[0] == runs[-1][-1][0] + 1:
            runs[-1].append(pair)
        else:
            runs.append([pair])
    return runs


def judge(run: list[tuple[int, int]], pictures: dict[int, Any]) -> list[tuple[int, int, float, float]]:
    """``(render frame, take frame, blend, share)`` of each double take frame of *run*, its take frames read in the
    order the render shows them, each change of take frame once (a take frame held over two render frames is one
    picture, never compared with itself)."""
    order: list[tuple[int, int]] = []
    for frame, take_frame in run:
        if not order or order[-1][1] != take_frame:
            order.append((frame, take_frame))
    # A synthesised span may blend content drawn on one side only (a panel's text over its spinner, #10100): judged too.
    watch = DoubleWatch(appearing=True)
    scene = {"type": "recording", "id": "take"}
    for count, (_frame, take_frame) in enumerate(order):
        picture = pictures.get(take_frame)
        if picture is None:
            watch.feed(np.zeros((1, 1), dtype=np.float32), count, {"type": "none"})
            continue
        watch.feed(picture, count, scene)
    return [(order[int(h[0])][0], order[int(h[0])][1], float(h[1]), float(h[2])) for h in watch.hits]


async def read_take_frames(path: str, frames: set[int], *, src_fps: float, size: tuple[int, int]) -> dict[int, Any]:
    """The grey take frames *frames* of the take at *path*, read at :data:`READ_WIDTH` from the first to the last."""
    from app.services.video_studio import encoder

    if not frames:
        return {}
    width = READ_WIDTH
    height = max(2, round(width * size[1] / max(size[0], 1) / 2) * 2)
    first, last = min(frames), max(frames)
    start = max(0.0, (first - 0.25) / src_fps) if first else 0.0
    out: dict[int, Any] = {}
    index = first
    stream = encoder.iter_gray_frames(path, width=width, height=height, fps=src_fps, start=start)
    try:
        async for raw in stream:
            if index in frames:
                out[index] = np.frombuffer(raw, dtype=np.uint8).reshape(height, width).astype(np.float32)
            index += 1
            if index > last:
                break
    finally:
        await stream.aclose()
    return out


async def findings(
    document: dict,
    *,
    sources: dict,
    takes: dict,
    render_fps: float,
    offset: float = 0.0,
    span: tuple[float, float] | None = None,
    on_frame: Callable[[], None] | None = None,
) -> tuple[list[Finding], dict]:
    """The ``double_image`` findings the render at *render_fps* would raise on the synthesised take frames of the
    *document*'s recording scenes inside *span* ``(start, seconds)`` (a scene preview's window), timed in the
    preview's MP4 (*offset*: where it starts in *document*), and a summary of what was read.

    *sources*: the compositor's ``take/<scene id>`` -> ``FrameSource``; *takes*: ``scene id`` -> its ``TakeMedia``.
    """
    found: list[Finding] = []
    read = 0
    scenes_read: list[str] = []
    window = (span[0], span[0] + span[1]) if span else None
    for scene in document.get("scenes") or []:
        if scene.get("type") not in READ_TYPES:
            continue
        start, end = float(scene.get("start") or 0.0), float(scene.get("end") or 0.0)
        if window is not None and (end <= window[0] or start >= window[1]):
            continue
        scene_id = str(scene.get("id") or "")
        source = sources.get(f"take/{scene_id}")
        take = takes.get(scene_id)
        spans = synthesised_spans(take) if take is not None else []
        if source is None or not spans:
            continue
        shown = shown_frames(scene, render_fps, spans)
        if window is not None:
            shown = [pair for pair in shown if window[0] <= pair[0] / render_fps < window[1]]
        if not shown:
            continue
        size = (int(scene.get("src_w") or 16), int(scene.get("src_h") or 9))
        pictures = await read_take_frames(
            source.path, {take_frame for _f, take_frame in shown}, src_fps=float(source.fps or 1.0), size=size
        )
        read += len(pictures)
        scenes_read.append(scene_id)
        if on_frame is not None:
            on_frame()
        for run in runs_of(shown):
            for frame, take_frame, blend, share in judge(run, pictures):
                at = frame / render_fps - offset
                if _excused(document, at + offset, at + offset + 1 / render_fps):
                    continue
                found.append(
                    Finding(
                        DOUBLE_IMAGE,
                        at,
                        at + 1 / render_fps,
                        scene_id,
                        f"take frame {take_frame} of a recording, shown at {clock(at)} by the render at "
                        f"{render_fps:g} fps, is a {round(100 * blend)}% mix of the take frames around it (read on its "
                        f"synthesised stretch; this preview, drawn at a lower rate, may step over it): {REMEDY}",
                        WARN,
                        {
                            "frames": 1,
                            "frame": frame,
                            "frame_at": round(at, 3),
                            "take_frame": take_frame,
                            "blend": round(blend, 3),
                            "share": round(share, 3),
                            "scene_type": "recording",
                            "render_fps": render_fps,
                        },
                        code="double_image",
                        params={"frames": 1, "start": clock(at), "end": clock(at + 1 / render_fps)},
                    )
                )
    summary = {"render_fps": render_fps, "take_frames": read, "scenes": scenes_read, "double_images": len(found)}
    return found, summary
