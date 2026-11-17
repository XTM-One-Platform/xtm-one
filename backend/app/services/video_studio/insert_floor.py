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

"""A recording made for a screen insert, held to the insert capture floor when it is recorded and when it is placed.

A screen insert refuses a recording whose browser painted under ``perceptual.MIN_INSERT_PAINTED_FPS`` (15) frames
a second (the ``insert_capture_floor`` lint of a scene preview). On a busy CPU worker a take can paint 5 to 7 frames
a second and still come out ``ready``: the author learned it only at the preview's lint, or from a stepping screen in
the film. So (#10079):

* **Marked at the call.** ``record_browser_demo`` with ``target_render`` stores what the take is for on its row
  (``capture.for_insert``: the video, the scene, the insert, the quality, :func:`mark`).
* **Measured when it is recorded.** ``runners.run_take`` measures the painted rate of such a take (its frames over
  its length, ``perceptual.painted_fps``) before it is saved (:func:`judge`): under the floor on its first take, it
  is recorded once more in the recording lane, on the worker that claims it then (another one, or the same one with
  a lighter load), its first rate kept (``capture.floor_retry``, :func:`retry_capture`); under the floor again, it
  ends ``failed``, saying both rates and the window and pixel ratio its worker would sustain (:func:`Judged.words`).
  Over the floor on its second take, it is ready and its notes say it was recorded twice.
* **Over the seconds its page changes** (#10087, :func:`motion_rate`). Chromium's screencast hands over a frame
  only when the page paints: a still page gives 0 to 1 a second, a perfectly smooth console take 4 to 7 over its
  whole length. So the floor holds the rate a take painted at inside its motion windows - the stretches where
  consecutive painted frames differ (``capture_rate.MOVING_SHARE``, a caret or a keystroke does not), grouped while
  changes follow each other within :data:`MOTION_GAP` - kept on its capture (``capture.insert_rate``). A still page is
  fully captured (a held frame is exact), and so is a lone change (a click redrawn at once). A take recorded before
  the measure is held to its whole-take rate (``perceptual.insert_rate``).
* **Idle is not starved** (#10101, :func:`expected_spans`). A take recorded on a build that watches what its page asks
  to move (``painting`` on each step's event: the animation frames it requested, the transitions and animations that
  ran, ``capture._MOTION_WATCH_JS``) is measured only where the page asked to move and visibly did, plus the scrolls
  and the dense runs of changed frames: a hover card or a tooltip seconds apart is a held page, never a slow window.
  A stretch where the page asked to move and changed but the screencast delivered under the floor is ``starved`` and
  named by its seconds; the seconds the page rested are ``idle``. A take that rested is never advised a smaller window
  or a lower pixel ratio (:meth:`Judged.sustained_words`).
* **Said when it is placed.** A save names every screen insert that plays a take painted under the floor, with the
  ``record_browser_demo`` call that records it again (:func:`save_notes`): before any preview, not only at its lint.

A take not made for an insert is unchanged.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Any

from app.core.utils import format_exception_message
from app.services.video_studio.perceptual import INSERT_RATE_KEY, MIN_INSERT_PAINTED_FPS, insert_rate, painted_fps

logger = logging.getLogger(__name__)

#: Where a take made for a screen insert keeps what it is for (``capture.for_insert``).
MARK_KEY = "for_insert"
#: Where a take recorded again for the floor keeps its first take's rate (``capture.floor_retry``).
RETRY_KEY = "floor_retry"
#: What :func:`judge` decides: recorded once more, failed, or ready after a second take.
AGAIN = "again"
FAILED = "failed"
PASSED_AGAIN = "passed_again"
#: The pixel ratios a suggestion steps down through before it shrinks the window.
_RATIOS = (2.0, 1.5, 1.0)
#: The share of the estimated sustainable pixels a suggestion keeps: a margin over the floor.
_MARGIN = 0.85
#: The narrowest window a suggestion names.
_MIN_WIDTH = 640
#: Two changes of the page further apart than this (seconds) are two motion windows: the still page between them is
#: held exactly, never counted against the rate.
MOTION_GAP = 1.0
#: A motion window shorter than this (seconds) is a lone change, captured exactly: never judged.
MOTION_LEAST = 0.25
#: Outside what the page asked to move (#10101): changed frames this close (seconds) form a dense run, judged; changes
#: further apart are discrete changes of a resting page (a hover card, a tooltip), held exactly.
DENSE_GAP = 0.1
#: The most starved stretches a take's capture keeps and its words name.
MOST_STARVED = 8
#: The changed frames a window needs to be judged: two hover changes inside a page's idle render loop are not motion.
MIN_CHANGES = 3


def mark(*, video_id: str, scene: Any, insert: Any, quality: str) -> dict[str, Any]:
    """The ``capture`` a take recorded for screen insert *insert* of *scene* in video *video_id* starts with."""
    return {
        MARK_KEY: {
            "video_id": str(video_id),
            "scene": str(scene),
            "insert": None if insert in (None, "") else str(insert),
            "quality": str(quality),
        }
    }


def mark_of(capture: Any) -> dict[str, Any] | None:
    """What a take's *capture* says it was recorded for (:func:`mark`), with its first take's rate when it was
    recorded again (:data:`RETRY_KEY`); ``None``: a take not made for a screen insert."""
    if not isinstance(capture, dict) or not isinstance(capture.get(MARK_KEY), dict):
        return None
    kept = {MARK_KEY: dict(capture[MARK_KEY])}
    if isinstance(capture.get(RETRY_KEY), dict):
        kept[RETRY_KEY] = dict(capture[RETRY_KEY])
    return kept


def motion_windows(times: list[float], changed: list[bool], *, gap: float = MOTION_GAP) -> list[tuple[int, int]]:
    """``[first, last]`` indices of the changed frames bounding each motion window: *changed[k]* says frame *k + 1*
    differs from frame *k* (*times* their instants); changed frames no further apart than *gap* belong to one window.
    A window starts at its first changed frame, never at the still frame before it: the time the page held still
    before a scroll is never counted against the scroll's rate."""
    found: list[tuple[int, int]] = []
    for k, moved in enumerate(changed):
        if not moved:
            continue
        index = k + 1
        if found and times[index] - times[found[-1][1]] <= gap:
            found[-1] = (found[-1][0], index)
        else:
            found.append((index, index))
    return found


def rate_over(times: list[float], windows: list[tuple[int, int]], duration: float | None) -> dict[str, Any]:
    """What a take's capture keeps of its rate (``capture.insert_rate``): ``whole`` its frames over its length,
    ``motion`` its frames a second inside its motion *windows* at least :data:`MOTION_LEAST` long (``None``: a still
    page, or only lone changes), ``seconds`` their length and ``windows`` their count."""
    frames = seconds = 0.0
    kept = 0
    for first, last in windows:
        span = times[last] - times[first]
        if span < MOTION_LEAST:
            continue
        kept += 1
        frames += last - first
        seconds += span
    whole = painted_fps(len(times), duration)
    return {
        "whole": round(whole, 2) if whole is not None else 0.0,
        "motion": round(frames / seconds, 2) if seconds > 0 else None,
        "seconds": round(seconds, 2),
        "windows": kept,
    }


def _merged(spans: list[tuple[float, float]]) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for start, end in sorted(spans):
        if out and start <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], end))
        else:
            out.append((start, end))
    return out


def expected_spans(events: list[dict] | None) -> list[tuple[float, float]] | None:
    """Where a take's page asked to move (#10101): the spans each step's event keeps of the animation frames the page
    requested and the transitions and animations that ran (``painting``), with its transitions (``motion``) and its
    scrolls (``capture_rate.paced_spans``), merged. ``None``: a take recorded before the page was watched so (no event
    carries ``painting``) - its idle seconds cannot be told from starved ones."""
    from app.services.video_studio import capture_rate

    if not events or not any(isinstance(event.get("painting"), list) for event in events):
        return None
    spans: list[tuple[float, float]] = []
    for event in events:
        for span in event.get("painting") or []:
            try:
                start, end = float(span[0]), float(span[1])
            except (TypeError, ValueError, IndexError):
                continue
            # One animation frame requested alone is the frame after it: a tick of the page's own clock.
            spans.append((start, max(end, start + 1 / 60)))
    spans.extend(capture_rate.paced_spans(events))
    return _merged([(a, b) for a, b in spans if b >= a])


def classify(
    times: list[float],
    changed: list[bool],
    expected: list[tuple[float, float]],
    duration: float | None,
) -> dict[str, Any]:
    """A take's rate over the stretches its page asked to move and visibly moved (#10101), on *times* and *changed*
    (:func:`motion_windows`) and the *expected* spans (:func:`expected_spans`):

    * inside an expected span (the page requested animation frames, ran a transition, scrolled), changed frames no
      further apart than :data:`MOTION_GAP` form a window: the page asked to paint all along, so a gap there is frames
      not delivered - under the floor the window is **starved**;
    * outside them, only a dense run of changed frames (no gap past :data:`DENSE_GAP`) is judged: changes seconds
      apart (a hover card, a tooltip) are a resting page's, held exactly;
    * a window is judged from :data:`MIN_CHANGES` changed frames over :data:`MOTION_LEAST` seconds; the rest of the
      take is **idle**: no paint asked for, or nothing changed - a held frame, never a dropped one.

    ``{whole, motion, seconds, windows, starved, idle, painting: true}``: ``motion`` ``None`` when nothing was judged,
    ``starved`` ``[[start, end, fps], ...]``, ``idle`` the seconds of the take not judged."""
    inside = [any(a - 1e-6 <= times[k + 1] <= b + 1e-6 for a, b in expected) for k in range(len(changed))]
    judged: list[tuple[float, float, int]] = []
    for mask, gap in (
        ([moved and inside[k] for k, moved in enumerate(changed)], MOTION_GAP),
        ([moved and not inside[k] for k, moved in enumerate(changed)], DENSE_GAP),
    ):
        for first, last in motion_windows(times, mask, gap=gap):
            span = times[last] - times[first]
            if span >= MOTION_LEAST and sum(mask[first - 1 : last]) >= MIN_CHANGES:
                judged.append((times[first], times[last], last - first))
    frames = sum(count for _a, _b, count in judged)
    seconds = sum(b - a for a, b, _count in judged)
    whole = painted_fps(len(times), duration)
    starved = [
        [round(a, 2), round(b, 2), round(count / (b - a), 1)]
        for a, b, count in sorted(judged)
        if b > a and count / (b - a) < MIN_INSERT_PAINTED_FPS
    ]
    length = float(duration) if duration else (times[-1] if times else 0.0)
    return {
        "whole": round(whole, 2) if whole is not None else 0.0,
        "motion": round(frames / seconds, 2) if seconds > 0 else None,
        "seconds": round(seconds, 2),
        "windows": len(judged),
        "starved": starved[:MOST_STARVED],
        "idle": round(max(0.0, length - seconds), 1),
        "painting": True,
    }


def motion_rate(
    frames: list[tuple[float, str]], *, workdir: str, duration: float | None, events: list[dict] | None = None
) -> dict[str, Any]:
    """The rate a take's browser painted *frames* (``(t, name)`` in *workdir*, as painted) at while its page changed
    (:func:`rate_over`): consecutive frames differing on ``capture_rate.MOVING_SHARE`` of the frame or more are a
    change; a frame that cannot be read counts as one (never a still page it may not be). Given *events* that say where
    the page asked to move (:func:`expected_spans`), idle seconds are told from starved ones (:func:`classify`)."""
    from pathlib import Path

    from app.services.video_studio import capture_rate

    times = [float(t) for t, _name in frames]
    changed: list[bool] = []
    previous: Any = None
    for index, (_t, name) in enumerate(frames):
        try:
            grey: Any = capture_rate._grey(Path(workdir) / name)
        except Exception:  # a frame not read: a change
            grey = None
        if index > 0:
            changed.append(
                grey is None
                or previous is None
                or capture_rate.moved_share(previous, grey) >= capture_rate.MOVING_SHARE
            )
        previous = grey
    expected = expected_spans(events)
    if expected is not None and times:
        return classify(times, changed, expected, duration)
    return rate_over(times, motion_windows(times, changed), duration)


def measured_of(capture: Any) -> dict[str, Any] | None:
    """The rate a take's *capture* keeps over its motion windows (``perceptual.INSERT_RATE_KEY``), ``None`` when it was
    recorded before the measure."""
    found = capture.get(INSERT_RATE_KEY) if isinstance(capture, dict) else None
    return found if isinstance(found, dict) else None


def _fps(value: float) -> str:
    return f"{value:.1f}".rstrip("0").rstrip(".")


def _ratio(value: float) -> str:
    return f"{value:g}"


def sustained_size(viewport: tuple[int, int], pixel_ratio: float, fps: float) -> tuple[tuple[int, int], float] | None:
    """The window and pixel ratio a worker that painted *fps* at *viewport* x *pixel_ratio* would paint the floor at,
    estimated: its painted rate falls about in step with the pixels it paints. The take's own window at a lower pixel
    ratio first (its layout kept), else a smaller window at pixel ratio 1; ``None`` when nothing measured."""
    width, height = int(viewport[0] or 0), int(viewport[1] or 0)
    if fps <= 0 or width <= 0 or height <= 0 or pixel_ratio <= 0:
        return None
    painted = width * height * pixel_ratio * pixel_ratio
    allowed = painted * (fps / MIN_INSERT_PAINTED_FPS) * _MARGIN
    for ratio in _RATIOS:
        if ratio < pixel_ratio and width * height * ratio * ratio <= allowed:
            return (width, height), ratio
    scale = math.sqrt(allowed / (width * height))
    new_width = max(_MIN_WIDTH, int(width * scale) // 8 * 8)
    new_height = max(8, round(new_width * height / width) // 2 * 2)
    return (new_width, new_height), 1.0


@dataclass(frozen=True)
class Judged:
    """What :func:`judge` decided of a take made for a screen insert."""

    kind: str
    fps: float
    #: Its first take's rate, when this is its second take.
    first: float | None = None
    viewport: tuple[int, int] = (0, 0)
    pixel_ratio: float = 1.0
    #: The worker its first take was recorded on, when this is its second take and it was said.
    first_worker: str | None = None
    #: The worker this take was recorded on, when said.
    worker: str | None = None
    #: The seconds its page changed, when its rate is the one painted over them (#10087); ``None``: its whole length.
    motion_seconds: float | None = None
    #: The stretches its page asked to move and changed but were delivered under the floor (#10101), ``(start, end,
    #: fps)``, and the seconds it rested; ``None``: measured before idle was told from starved.
    starved: tuple[tuple[float, float, float], ...] | None = None
    idle: float | None = None

    def _over(self) -> str:
        if self.motion_seconds is None:
            return ""
        return f" over the {_fps(self.motion_seconds)} s its page changed"

    def starved_words(self) -> str:
        """``; starved at 12.1-13.4 s (6 fps), 20-21.2 s (9 fps); idle 24 s (held, not dropped)`` - empty when idle
        was not told from starved."""
        if self.starved is None:
            return ""
        said = ", ".join(f"{_fps(a)}-{_fps(b)} s ({_fps(fps)} fps)" for a, b, fps in self.starved)
        parts = [f"starved at {said}"] if said else []
        if self.idle:
            parts.append(f"idle {_fps(self.idle)} s (held, not dropped)")
        return "; " + "; ".join(parts) if parts else ""

    def _workers(self) -> str:
        if not self.first_worker or not self.worker:
            return ""
        if self.first_worker == self.worker:
            return f", both on worker {self.worker}"
        return f", the first on worker {self.first_worker}, this one on worker {self.worker}"

    def stage(self) -> str:
        """What its row's stage says while it waits to be recorded once more (at most 200 characters)."""
        return (
            f"Recording again: its first take painted {_fps(self.fps)} fps, under the "
            f"{_fps(MIN_INSERT_PAINTED_FPS)} a screen insert source needs"
        )[:200]

    def sustained_words(self) -> str:
        # A take measured before idle seconds were told from starved ones may have failed on a page at rest: no window
        # or pixel ratio that would soften the insert is advised for it (#10101).
        if self.starved is None and self.motion_seconds is not None:
            return "record it again on this build, which tells the seconds its page rested from frames not delivered"
        if self.starved is not None and not self.starved:
            return "record it again at its size"
        found = sustained_size(self.viewport, self.pixel_ratio, self.fps)
        if found is None:
            return "record it on a GPU worker or at a smaller window"
        (width, height), ratio = found
        return (
            f"a {width}x{height} window at pixel ratio {_ratio(ratio)} would sustain it on this worker (estimated "
            f"from {_fps(self.fps)} fps at {self.viewport[0]}x{self.viewport[1]} x{_ratio(self.pixel_ratio)}), or a "
            "GPU worker at its size"
        )

    def words(self) -> str:
        """What the take says: failed with both rates and the size to record at, or ready after a second take."""
        if self.kind == FAILED:
            twice = (
                f" (recorded twice; its first take painted {_fps(self.first)} fps{self._workers()})"
                if self.first is not None
                else ""
            )
            return (
                f"captured {_fps(self.fps)} fps{self._over()}, under the {_fps(MIN_INSERT_PAINTED_FPS)} an insert "
                f"source needs{twice}{self.starved_words()}: {self.sustained_words()}."
            )
        if self.kind == PASSED_AGAIN:
            return (
                f"Recorded twice for its screen insert: its first take painted {_fps(self.first or 0.0)} fps, under "
                f"the {_fps(MIN_INSERT_PAINTED_FPS)} an insert source needs; this one painted {_fps(self.fps)} fps"
                f"{self._workers()}."
            )
        return self.stage() + "."


def judge(
    marked: dict[str, Any] | None,
    *,
    captured_frames: int | None,
    duration: float | None,
    viewport: tuple[int, int],
    pixel_ratio: float,
    worker: str | None = None,
    measured: dict[str, Any] | None = None,
) -> Judged | None:
    """The decision on a take marked for a screen insert (*marked*, :func:`mark_of`) that painted *captured_frames*
    over *duration* seconds - over the seconds its page changed when *measured* (:func:`motion_rate`, #10087):
    :data:`AGAIN` under the floor on its first take, :data:`FAILED` under it on its second, :data:`PASSED_AGAIN` over
    it on its second; ``None``: a take not made for an insert, over the floor on its first take, held still (fully
    captured), or whose rate is unknown."""
    if marked is None:
        return None
    rate = insert_rate(captured_frames, duration, measured)
    if rate is None:
        return None
    over = float(measured["seconds"]) if isinstance(measured, dict) and measured.get("motion") is not None else None
    starved: tuple[tuple[float, float, float], ...] | None = None
    idle: float | None = None
    if isinstance(measured, dict) and measured.get("painting"):
        starved = tuple(
            (float(s[0]), float(s[1]), float(s[2]))
            for s in measured.get("starved") or []
            if isinstance(s, list | tuple) and len(s) == 3
        )
        idle = float(measured.get("idle") or 0.0)
    retry = marked.get(RETRY_KEY)
    first = float(retry["fps"]) if isinstance(retry, dict) and retry.get("fps") is not None else None
    first_worker = str(retry["worker"]) if isinstance(retry, dict) and retry.get("worker") else None
    size = (int(viewport[0] or 0), int(viewport[1] or 0))
    ratio = float(pixel_ratio or 1.0)
    said = str(worker) if worker else None
    if rate >= MIN_INSERT_PAINTED_FPS:
        if first is None:
            return None
        return Judged(PASSED_AGAIN, rate, first, size, ratio, first_worker, said, over, starved, idle)
    return Judged(
        FAILED if first is not None else AGAIN, rate, first, size, ratio, first_worker, said, over, starved, idle
    )


def retry_capture(marked: dict[str, Any], judged: Judged) -> dict[str, Any]:
    """The ``capture`` a take recorded once more starts from: what it is for and its first take's rate - its first
    take's keyframes and resume point dropped, so it records again from its script."""
    retry: dict[str, Any] = {"fps": round(judged.fps, 2)}
    if judged.worker:
        retry["worker"] = judged.worker
    return {MARK_KEY: dict(marked[MARK_KEY]), RETRY_KEY: retry}


def kept(marked: dict[str, Any] | None) -> dict[str, Any]:
    """What a saved or failed take keeps of its mark beside its capture (``{}`` for a take not made for an insert)."""
    return dict(marked) if marked else {}


# ── At save ───────────────────────────────────────────────────────────────


def _call(take_id: str, scene_id: str, index: int, quality: str, video_id: str | None) -> str:
    video = f'video_id: "{video_id}"' if video_id else "video_id"
    return (
        f'record_browser_demo(from_recording_id: "{take_id}", target_render: "{quality}", {video}, '
        f'scene: "{scene_id}", insert: {index})'
    )


async def save_notes(db: Any, spec: Any, project: Any = None) -> list[str]:
    """One note per screen insert of the parsed *spec* that plays a take painted under the floor, with the call that
    records it again for that insert at the quality the video is made for (``render_checks.target_quality``): said at
    save, before a preview's lint refuses it. Never raises: a take that cannot be read is not named."""
    from app.core.utils import safe_uuid
    from app.models.video_studio import VideoTake
    from app.services.video_studio import timeline

    notes: list[str] = []
    video_id = str(project.id) if project is not None and getattr(project, "id", None) else None
    quality: str | None = None
    try:
        parts = [(key, part) for key, part in timeline.insert_sources(spec).items() if part.kind == "take" and part.ref]
        rates: dict[str, float | None] = {}
        for key, part in parts:
            if part.ref not in rates:
                uid = safe_uuid(str(part.ref))
                row = await db.get(VideoTake, uid) if uid is not None else None
                frames = getattr(row, "captured_frames", None) if row is not None else None
                length = (getattr(row, "duration_ms", None) or 0) / 1000 if row is not None else 0
                measured = measured_of(getattr(row, "capture", None)) if row is not None else None
                rate = insert_rate(frames, length, measured)
                rates[part.ref] = rate if rate is not None and rate < MIN_INSERT_PAINTED_FPS else None
            rate = rates[part.ref]
            if rate is None:
                continue
            if quality is None:
                from app.services.video_studio.render_checks import target_quality
                from app.services.video_studio.spec import FINAL_QUALITY

                quality = await target_quality(db, project, default=FINAL_QUALITY)
            scene_id = timeline.insert_scene(key)
            notes.append(
                f"Scene {scene_id}: {timeline.insert_label(key)} plays take `{part.ref}`, painted at {_fps(rate)} fps, "
                f"under the {_fps(MIN_INSERT_PAINTED_FPS)} a screen insert source needs: a scene preview refuses it "
                f"(insert_capture_floor). Record it again: {_call(part.ref, scene_id, part.index, quality, video_id)}."
            )
    except Exception as exc:  # advice that cannot be read is not given
        logger.info("Insert capture floor at save not read: %s", format_exception_message(exc))
        return []
    return notes
