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

"""The raw capture records of a take over a window of its seconds, as ``get_video_recording(window=...)`` answers them.

A take keeps what its recorder did, second by second, in records the summary lines of ``get_video_recording`` only
count: the canvas copies captured alone (``capture.alone.runs``: ``{t, step, canvas, composited, why, painted, fps,
...}``), the frames dropped as painted at another geometry (``capture.off_scale_dropped``), and on each step's event
its scrolls and canvas zooms as paced (``scrolls``: ``{t, scroller, paced, why, zoom, riding, carried, held_from,
synthesis, ...}``) and its transitions (``motion``). Reading why a frame of a take shows what it shows (a static
toolbar drawn at two offsets across a cut, a zoom that steps) needs those records over a few seconds, not their
counts: this module keeps the ones whose span meets the window, as stored, the steps playing then with them.

The frames themselves are read from the take's frame list (:data:`FRAME_LIST_KEY`, a small JSON file stored at its
save, :func:`frame_listing`): every frame the take kept and every frame its flicker passes dropped, with the test that
dropped it, so a window answers which stage drew each frame there (``capture_rate.FRAME_STAGES``) and why one is gone.
"""

from __future__ import annotations

import re
from typing import Any

#: The most records of one kind answered: a window over a whole take is cut, the count of the rest said.
MOST_RECORDS = 40
#: The longest list a record keeps in the answer (a scroll's log of offsets); a longer one is said by its length.
MOST_ITEMS = 60
#: The most frames of the frame list answered over a window, the count of the rest said.
MOST_FRAMES = 120
#: Where a take's ``capture`` names its frame list file (:func:`frame_listing`, a ``SOURCE_TAKE`` file it owns).
FRAME_LIST_KEY = "frame_list"
#: The version of the frame list file.
FRAME_LIST_VERSION = 1
#: Why a frame dropped by a flash of a hold is gone when its run names no test of its own.
_HOLD_FLASH = "hold flash"

#: The step fields answered with each step that plays in the window (the rest of an event is its records).
_STEP_FIELDS = (
    "step",
    "action",
    "label",
    "ta",
    "t0",
    "t1",
    "ok",
    "capture_fps",
    "canvas_motion",
    "synthesised",
    # The span a node's highlight was probed over (``capture_rate._locate_probes``): its frames are never on screen.
    "probed",
    # The span the cursor looked for a node's card at places naming something else: its frames are never on screen.
    "hover_searched",
    # The span a located node was read over, then approached up to the button: a state shown and gone in it is dropped.
    "located_in",
    "approached",
    # What was approached: ``point`` for a step at a point (a wheel on a graph canvas), absent for a located node.
    "approach",
)

_WINDOW = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(?:-|\.\.|to)\s*(\d+(?:\.\d+)?)\s*$")


def parse_window(text: str) -> tuple[float, float] | str:
    """*text* (``"30.5-32"``, ``"30.5..32"``) as ``(start, end)`` seconds of the take, or why it is refused."""
    found = _WINDOW.match(text or "")
    if not found:
        return f"window {text!r} is not START-END in seconds of the recording (e.g. '30.5-32')"
    start, end = float(found.group(1)), float(found.group(2))
    if end <= start:
        return f"window {text!r} ends before it starts"
    return start, end


def _span(record: Any) -> tuple[float, float] | None:
    """The seconds of the take a record covers: its ``t`` (a number or ``[start, end]``), else its step's ``ta``/``t1``."""
    if not isinstance(record, dict):
        return None
    t = record.get("t")
    if isinstance(t, int | float) and not isinstance(t, bool):
        return float(t), float(t)
    if isinstance(t, list | tuple) and t and all(isinstance(v, int | float) and not isinstance(v, bool) for v in t[:2]):
        return float(t[0]), float(t[-1] if len(t) > 1 else t[0])
    start = record.get("ta", record.get("t0"))
    if isinstance(start, int | float):
        end = record.get("t1")
        return float(start), float(end) if isinstance(end, int | float) and end >= start else float(start)
    return None


def _meets(record: Any, start: float, end: float) -> bool:
    span = _span(record)
    return span is not None and span[0] <= end and start <= span[1]


def _short(record: Any) -> Any:
    """*record* with each list longer than :data:`MOST_ITEMS` (a scroll's log, its frames) said by its length."""
    if not isinstance(record, dict):
        return record
    return {
        key: f"{len(value)} items" if isinstance(value, list) and len(value) > MOST_ITEMS else value
        for key, value in record.items()
    }


def _kept(records: Any, start: float, end: float, cut: dict[str, int], kind: str) -> list:
    found = [_short(record) for record in records or [] if _meets(record, start, end)]
    if len(found) > MOST_RECORDS:
        cut[kind] = len(found) - MOST_RECORDS
    return found[:MOST_RECORDS]


def _skipped_in(alone: dict, start: float, end: float) -> int:
    """The copies skipped by the runs and the steps outside every run of *alone* whose span meets ``[start, end]``."""
    total = 0
    for kind in ("runs", "skipped_outside_runs"):
        for record in alone.get(kind) or []:
            if not _meets(record, start, end):
                continue
            skipped = record.get("skipped")
            if isinstance(skipped, int) and not isinstance(skipped, bool):
                total += skipped
    return total


def frame_list_file(capture: dict | None) -> str | None:
    """The id of the frame list file a take's *capture* names (:data:`FRAME_LIST_KEY`); ``None`` without one."""
    found = capture.get(FRAME_LIST_KEY) if isinstance(capture, dict) else None
    return str(found) if found else None


def frame_listing(before: list[tuple[float, str]], kept: list[tuple[float, str]], flashes: list[dict]) -> dict:
    """The frame list a take stores at its save: each frame (``[seconds, file name, why]``) of *before* (the frames
    the flicker passes read, ``capture_rate.held_flashes``), ``why`` ``None`` for a frame in *kept*, else the test of
    the run of *flashes* whose span holds it (``gate replay``, ``locate probe``...; a hold flash without one)."""
    kept_names = {name for _t, name in kept}
    spans = [
        (float(f["t"][0]), float(f["t"][-1]), str(f.get("test") or _HOLD_FLASH))
        for f in flashes or []
        if isinstance(f, dict) and isinstance(f.get("t"), list | tuple) and f["t"]
    ]
    rows: list[list] = []
    for t, name in before:
        why = None
        if name not in kept_names:
            why = next((test for a, b, test in spans if a <= t < b), _HOLD_FLASH)
        rows.append([round(float(t), 4), name, why])
    return {"version": FRAME_LIST_VERSION, "frames": rows}


def window_frames(listing: Any, start: float, end: float) -> tuple[list[dict], int]:
    """The frames of a take's frame list *listing* (:func:`frame_listing`) within ``[start, end]`` seconds, each
    ``{t, name, stage, kept}`` (and ``why`` when dropped), :data:`MOST_FRAMES` at most, and how many more were cut."""
    from app.services.video_studio.capture_rate import frame_stage

    rows = listing.get("frames") if isinstance(listing, dict) else None
    found: list[dict] = []
    for row in rows or []:
        if not isinstance(row, list | tuple) or len(row) < 2 or not isinstance(row[0], int | float):
            continue
        if not start <= float(row[0]) <= end:
            continue
        name, why = str(row[1]), row[2] if len(row) > 2 else None
        frame: dict[str, Any] = {"t": row[0], "name": name, "stage": frame_stage(name), "kept": why is None}
        if why is not None:
            frame["why"] = why
        found.append(frame)
    return found[:MOST_FRAMES], max(0, len(found) - MOST_FRAMES)


def window_records(capture: dict | None, events: list | None, start: float, end: float) -> dict:
    """The records of a take (its *capture* and *events*) whose span meets ``[start, end]`` seconds, as stored."""
    capture = capture if isinstance(capture, dict) else {}
    alone = capture.get("alone") if isinstance(capture.get("alone"), dict) else {}
    cut: dict[str, int] = {}
    steps: list[dict] = []
    scrolls: list[dict] = []
    motion: list[dict] = []
    for event in events or []:
        if not isinstance(event, dict):
            continue
        step = event.get("step")
        if _meets(event, start, end):
            steps.append({key: event[key] for key in _STEP_FIELDS if event.get(key) is not None})
        for scroll in _kept(event.get("scrolls"), start, end, cut, "scrolls"):
            scrolls.append({"step": step, **scroll})
        for window in _kept(event.get("motion"), start, end, cut, "motion"):
            motion.append({"step": step, **window})
    out: dict[str, Any] = {
        "window": [start, end],
        "steps": steps[:MOST_RECORDS],
        "alone_runs": _kept(alone.get("runs"), start, end, cut, "alone_runs"),
        "scrolls": scrolls[:MOST_RECORDS],
        "motion": motion[:MOST_RECORDS],
        "off_scale_dropped": _kept(capture.get("off_scale_dropped"), start, end, cut, "off_scale_dropped"),
        # The frames of another state dropped in a hold (``capture_rate.held_flashes``); absent on an older take.
        "held_flashes": _kept(capture.get("held_flashes"), start, end, cut, "held_flashes"),
        # The copies skipped, every encoder behind, outside every run, per step (``canvas_alone.skipped_outside``); those
        # of a run are its ``skipped`` and ``skipped_windows``. [] when none fell in the window, on every take (#10970).
        "skipped_outside_runs": _kept(alone.get("skipped_outside_runs"), start, end, cut, "skipped_outside_runs"),
        # The copies skipped in the window: the ``skipped`` of its runs plus those of its ``skipped_outside_runs``, so a
        # window's record adds up on its own, records cut by :data:`MOST_RECORDS` included (#10970, retest 5cfe9d18).
        "skipped_total": _skipped_in(alone, start, end),
        # The take-wide count of copies skipped (``alone.busy``), whatever the window: 0 when none. The runs' ``skipped``
        # plus ``skipped_outside_runs`` over the whole take make it, the rest not placed (``canvas_alone.skipped_words``).
        "skipped_total_take": int(alone.get("busy") or 0),
    }
    if alone.get("off"):
        out["alone_off"] = alone["off"]
    if cut:
        out["cut"] = cut
    return out
