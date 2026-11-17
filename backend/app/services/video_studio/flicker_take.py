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

"""A scene preview's ``static_flicker`` read on the take frames the whole render's watch reads (issue #10054).

A scene preview is drawn at 30 fps from its own first frame; the whole render at the video's own rate, 60 fps at high
and ultra, and its frozen-picture watch reads that MP4 at ``frame_freeze.analysis_rate`` (30 for 60) in the phase of the
document's first frame. On a recording scene whose take plays compressed (``spec.IdleCompression``) or starts between
two of the preview's frames, the two read other take frames: a hover dim drawn and undrawn (A/B/A) on the frames the
render reads is a blocking ``static_flicker`` the green preview stepped over.

This check reads, for each ``recording`` scene of the preview, the take frames the render's watch would read
(one render frame in ``step``, counted from the document's first, each mapped to its take frame as
``double_take.shown_frames`` does), decodes them from the take's file and feeds them, in that order, to the render's own
watch (:class:`static_chrome.ChromeWatch`, its run rule :func:`static_chrome.is_run`, same thresholds). A run it keeps is
a ``static_flicker`` finding timed in the preview's MP4, with its take frame, said as read on the render's frames.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from typing import Any

from app.services.video_studio import static_chrome
from app.services.video_studio.double_take import map_time, read_take_frames
from app.services.video_studio.render_checks import FAIL, STATIC_FLICKER, Finding, clock


def watch_step(render_fps: float) -> int:
    """The render frames between two the render's watch reads: ``Shown.step`` at ``frame_freeze.analysis_rate``."""
    from app.services.video_studio.frame_freeze import analysis_rate

    return max(1, round(render_fps / analysis_rate(render_fps)))


def watched_frames(scene: dict, render_fps: float, step: int) -> list[tuple[int, int]]:
    """``(render frame, take frame)`` for each frame of *scene* the render's watch reads at *render_fps*: every
    *step*-th render frame from the document's first, each the take frame it shows (``double_take.shown_frames``)."""
    start, end = float(scene.get("start") or 0.0), float(scene.get("end") or 0.0)
    src_fps = max(float(scene.get("src_fps") or 0.0), 1.0)
    last_take = max(int(scene.get("src_frames") or 1) - 1, 0)
    time_map = scene.get("time_map") or []
    first = math.ceil(start * render_fps - 1e-9)
    first += (-first) % step
    out: list[tuple[int, int]] = []
    for frame in range(first, math.ceil(end * render_fps - 1e-9), step):
        take_t = map_time(time_map, max(0.0, frame / render_fps - start))
        out.append((frame, min(max(round(take_t * src_fps), 0), last_take)))
    return out


def watch_runs(order: list[tuple[int, int]], pictures: dict[int, Any], step: int) -> list[list[list[float]]]:
    """The runs of :attr:`static_chrome.ChromeWatch.hits` the render's watch keeps (``static_chrome.findings``' rule)
    over *order* (``watched_frames``), each hit's frame the watch count ``render frame // step``."""
    watch = static_chrome.ChromeWatch()
    scene = {"type": "recording", "id": "take"}
    for frame, take_frame in order:
        picture = pictures.get(take_frame)
        # A take frame not decoded: never compared across it.
        watch.feed(picture, frame // step, scene if picture is not None else {"type": "none"})
    runs: list[list[list[float]]] = []
    for hit in sorted(watch.hits, key=lambda h: h[0]):
        if runs and hit[0] - runs[-1][-1][0] <= static_chrome.RUN_GAP:
            runs[-1].append(hit)
        else:
            runs.append([hit])
    return [run for run in runs if static_chrome.is_run(run)]


async def findings(
    document: dict,
    *,
    sources: dict,
    render_fps: float,
    offset: float = 0.0,
    span: tuple[float, float] | None = None,
    on_frame: Callable[[], None] | None = None,
) -> tuple[list[Finding], dict]:
    """The ``static_flicker`` findings the render at *render_fps* would raise on the *document*'s recording scenes
    inside *span* ``(start, seconds)`` (a scene preview's window), timed in the preview's MP4 (*offset*: where it
    starts in *document*), and a summary of what was read. *sources*: the compositor's ``take/<scene id>`` ->
    ``FrameSource``."""
    from app.services.video_studio.double_frames import _excused

    step = watch_step(render_fps)
    rate = render_fps / step
    found: list[Finding] = []
    read = 0
    scenes_read: list[str] = []
    window = (span[0], span[0] + span[1]) if span else None
    for scene in document.get("scenes") or []:
        if scene.get("type") not in static_chrome.READ_TYPES:
            continue
        scene_id = str(scene.get("id") or "")
        source = sources.get(f"take/{scene_id}")
        if source is None:
            continue
        order = watched_frames(scene, render_fps, step)
        if window is not None:
            order = [pair for pair in order if window[0] <= pair[0] / render_fps < window[1]]
        if len(order) < 3:
            continue
        size = (int(scene.get("src_w") or 16), int(scene.get("src_h") or 9))
        pictures = await read_take_frames(
            source.path, {take for _f, take in order}, src_fps=float(source.fps or 1.0), size=size
        )
        read += len(pictures)
        scenes_read.append(scene_id)
        if on_frame is not None:
            on_frame()
        take_of = {frame // step: take for frame, take in order}
        for run in watch_runs(order, pictures, step):
            first, last = int(run[0][0]), int(run[-1][0])
            start, end = first / rate - offset, (last + 1) / rate - offset
            if _excused(document, start + offset, end + offset):
                continue
            strongest = max(run, key=lambda h: h[1])
            count = int(strongest[0])
            at = count / rate - offset
            box = [min(h[2] for h in run), min(h[3] for h in run), max(h[4] for h in run), max(h[5] for h in run)]
            frames = len({int(h[0]) for h in run})
            take_frame = take_of.get(count)
            found.append(
                Finding(
                    STATIC_FLICKER,
                    start,
                    end,
                    scene_id,
                    f"{frames} frames of a recording, as the render at {render_fps:g} fps shows and watches them, draw "
                    f"the page's static chrome at another place and back ({clock(start)} to {clock(end)}, box {box}), "
                    f"take frame {take_frame} at {clock(at)} the worst (read on the take; this preview, drawn from "
                    f"other frames, may step over it): {static_chrome.REMEDY}",
                    FAIL,
                    {
                        "frames": frames,
                        "frame": count * step,
                        "frame_at": round(at, 3),
                        "take_frame": take_frame,
                        "box": box,
                        "tiles": int(strongest[1]),
                        "scene_type": "recording",
                        "render_fps": render_fps,
                    },
                    code="static_flicker",
                    params={"frames": frames, "start": clock(start), "end": clock(end)},
                )
            )
    summary = {"render_fps": render_fps, "take_frames": read, "scenes": scenes_read, "static_flickers": len(found)}
    return found, summary
