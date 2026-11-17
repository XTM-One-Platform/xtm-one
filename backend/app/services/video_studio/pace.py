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

"""How a video moves: a dynamic pace by construction, and what the save says of its pace.

A product demo built from defaults reads slow: every recording waits for its
whole narration with its pauses given back, the camera moves calmly, a card
reveals its lines one by one. ``VideoSpec.pace: dynamic`` (the Filigran
Product Video template's default) presets the quick version wherever the spec
leaves a choice unset (:func:`with_defaults`, applied by
``templates.apply_template`` before the template's own defaults, so a
template's calm preset never wins over the pace it asks for):

* each recording scene cuts its pauses (``idle.mode cut``), moves its camera at
  the ``dynamic`` pace with half-second zooms, and keeps 300 ms of its take
  after its last step (``end_hold_ms``); ``fit: auto`` also speeds a recording
  longer than its narration up to :data:`FASTEST_FIT_SPEED`
  (``recording_fit``), the speed said;
* the transitions ``auto`` cut at the ``dynamic`` pace;
* a statement reveals its words one by one.

Whatever the pace, the save says how it reads (:func:`report`): the voice-over's
words a minute, the average and the longest scene, and per recording scene the
seconds of still screen - the cursor still, the page not changing - while the
voice speaks. A scene over :data:`LONGEST_SCENE_SECONDS` or with a still
stretch past :data:`STILL_SECONDS` under its narration is a ``pace`` advisory
(``quality``).
"""

from __future__ import annotations

import copy
import itertools
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

DYNAMIC = "dynamic"
#: What a dynamic video presets on each recording scene, each statement and each ``auto`` transition it leaves unset.
RECORDING_DEFAULTS: dict[str, Any] = {"idle": {"mode": "cut"}, "camera": {"pace": "dynamic", "move_seconds": 0.5}, "end_hold_ms": 300}
STATEMENT_DEFAULTS: dict[str, Any] = {"reveal": "words"}
TRANSITION_DEFAULTS: dict[str, Any] = {"type": "auto", "pace": "dynamic"}
#: The fastest ``fit: auto`` plays a recording longer than its narration in a dynamic video (``recording_fit``).
FASTEST_FIT_SPEED = 1.25
#: A scene longer than this, or still for longer than :data:`STILL_SECONDS` at once while the voice speaks, reads slow.
LONGEST_SCENE_SECONDS = 20.0
STILL_SECONDS = 3.0
#: How far the cursor moves between two samples, as a share of the frame, to be moving.
_CURSOR_MOVES = 0.002
#: Seconds around a step its page is taken to change in: from just before it acts to its answer shown.
_ACTS_BEFORE = 0.3
_ANSWERED_AFTER = 0.9
#: The steps that leave the page as it is.
_STILL_ACTIONS = frozenset({"wait", "pause", "marker"})


def _setdefaults(target: dict, defaults: Mapping[str, Any]) -> None:
    """*defaults* set into *target* where it has nothing, objects merged key by key."""
    for key, value in defaults.items():
        if isinstance(value, Mapping):
            child = target.get(key)
            if child is None:
                target[key] = copy.deepcopy(dict(value))
            elif isinstance(child, dict):
                _setdefaults(child, value)
        elif key not in target or target[key] is None:
            target[key] = copy.deepcopy(value)


def _auto_transition(transition: Any) -> Any:
    """A transition the author left ``auto`` (or unset) cut at the dynamic pace unless it names its own."""
    if transition is None:
        return copy.deepcopy(TRANSITION_DEFAULTS)
    if isinstance(transition, dict) and transition.get("type", "auto") == "auto":
        out = dict(transition)
        _setdefaults(out, TRANSITION_DEFAULTS)
        return out
    return transition


def with_defaults(spec: dict, pace: str | None) -> dict:
    """*spec* (a stored, authored spec: what its author set) with what *pace* presets where it leaves a choice unset;
    the spec itself when the pace presets nothing."""
    if pace != DYNAMIC:
        return spec
    out = copy.deepcopy(spec)
    out["default_transition"] = _auto_transition(out.get("default_transition"))
    for scene in out.get("scenes") or []:
        if not isinstance(scene, dict):
            continue
        if "transition" in scene:
            scene["transition"] = _auto_transition(scene["transition"])
        if scene.get("type") == "recording":
            _setdefaults(scene, RECORDING_DEFAULTS)
        elif scene.get("type") == "statement":
            _setdefaults(scene, STATEMENT_DEFAULTS)
    return out


# ── What the save says of the pace ────────────────────────────────────────


@dataclass(frozen=True)
class ScenePace:
    """One scene as the pace advisory reads it: its length, and for a recording its still screen under its narration."""

    scene_id: str
    seconds: float
    #: Seconds of still screen while the voice speaks (a recording only), all of it and the longest stretch at once.
    still: float = 0.0
    longest_still: float = 0.0
    #: Where that stretch starts, in seconds of the scene.
    still_from: float = 0.0


@dataclass(frozen=True)
class PaceReport:
    """How a video reads (:func:`report`)."""

    scenes: list[ScenePace]
    words: int = 0
    spoken_seconds: float = 0.0
    recordings: set[str] = field(default_factory=set)

    @property
    def words_per_minute(self) -> float | None:
        return self.words * 60 / self.spoken_seconds if self.spoken_seconds > 0 else None

    def summary(self) -> str:
        """``Pace: 162 words a minute of voice-over; scenes 9.4 s on average, the longest `s07` 18.2 s; still screen
        while the voice speaks: `s02` 1.2 s, `s05` 10.0 s.``"""
        if not self.scenes:
            return ""
        lengths = [scene.seconds for scene in self.scenes]
        longest = max(self.scenes, key=lambda scene: scene.seconds)
        parts = []
        if (wpm := self.words_per_minute) is not None:
            parts.append(f"{wpm:.0f} words a minute of voice-over")
        parts.append(
            f"scenes {sum(lengths) / len(lengths):.1f} s on average, the longest `{longest.scene_id}` {longest.seconds:.1f} s"
        )
        still = [f"`{scene.scene_id}` {scene.still:.1f} s" for scene in self.scenes if scene.scene_id in self.recordings]
        if still:
            parts.append("still screen while the voice speaks: " + ", ".join(still))
        return "Pace: " + "; ".join(parts) + "."

    def flagged(self) -> list[tuple[str, str]]:
        """``(scene id, words)`` of each scene that reads slow: over :data:`LONGEST_SCENE_SECONDS`, or still past
        :data:`STILL_SECONDS` at once while its narration speaks."""
        found = []
        for scene in self.scenes:
            said = []
            if scene.seconds > LONGEST_SCENE_SECONDS:
                said.append(
                    f"it lasts {scene.seconds:.1f} s, over {LONGEST_SCENE_SECONDS:g} s: split it, or say less over it"
                )
            if scene.longest_still > STILL_SECONDS:
                said.append(
                    f"its screen stays still {scene.longest_still:.1f} s from {scene.still_from:.1f} s while its "
                    f"narration speaks (the cursor still, the page unchanged; {scene.still:.1f} s in all): cut the "
                    "pause (idle.mode cut, trim_end, end_hold_ms), act on the page meanwhile, or say less there"
                )
            if said:
                found.append((scene.scene_id, "; ".join(said)))
        return found


def _merge(windows: list[tuple[float, float]]) -> list[tuple[float, float]]:
    merged: list[tuple[float, float]] = []
    for start, end in sorted(w for w in windows if w[1] > w[0]):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def page_changes(events: list[dict]) -> list[tuple[float, float]]:
    """Where a take's page changes, in its seconds: around each step that acts on it (never a wait, a pause or a
    marker), from just before it acts to its answer shown; a ``wait_for`` from what it waited for showing."""
    windows = []
    for event in events:
        if not event.get("ok", True) or event.get("action") in _STILL_ACTIONS:
            continue
        t0 = float(event.get("t0") or 0.0)
        t1 = float(event.get("t1") or t0)
        if event.get("action") == "wait_for":
            windows.append((t1 - 0.2, t1 + _ANSWERED_AFTER))
        else:
            windows.append((t0 - _ACTS_BEFORE, t1 + _ANSWERED_AFTER))
    return _merge(windows)


def cursor_moves(samples: list[list[float]]) -> list[tuple[float, float]]:
    """Where a planned recording's cursor moves, in seconds of its scene (its ``cursor.samples``)."""
    windows = []
    for (t0, x0, y0), (t1, x1, y1) in itertools.pairwise(tuple(s[:3]) for s in samples if len(s) >= 3):
        if abs(x1 - x0) > _CURSOR_MOVES or abs(y1 - y0) > _CURSOR_MOVES:
            windows.append((float(t0), float(t1)))
    return _merge(windows)


def still_under(planned: dict, take: Any, spoken: tuple[float, float]) -> tuple[float, float, float]:
    """``(all, longest, from)``: the seconds of still screen of a planned recording scene while its narration speaks -
    *spoken*, in seconds of the scene -, the longest stretch at once and where it starts."""
    from app.services.video_studio.timeline import map_inverse

    points = [tuple(point) for point in planned.get("time_map") or []]
    if len(points) < 2:
        return 0.0, 0.0, 0.0
    length = float(points[-1][0])
    moving = [
        (map_inverse(points, start), map_inverse(points, end))
        for start, end in page_changes(list(getattr(take, "events", None) or []))
        if end >= points[0][1] and start <= points[-1][1]
    ]
    moving += cursor_moves((planned.get("cursor") or {}).get("samples") or [])
    lo, hi = max(0.0, spoken[0]), min(length, spoken[1])
    still, longest, since, cursor = 0.0, 0.0, lo, lo
    for start, end in _merge(moving):
        if end <= cursor:
            continue
        gap_end = min(max(start, cursor), hi)
        if gap_end > cursor:
            gap = gap_end - cursor
            still += gap
            if gap > longest:
                longest, since = gap, cursor
        cursor = max(cursor, min(end, hi))
        if cursor >= hi:
            break
    if hi > cursor:
        gap = hi - cursor
        still += gap
        if gap > longest:
            longest, since = gap, cursor
    return round(still, 2), round(longest, 2), round(since, 2)


def report(document: dict, takes: Mapping[str, Any], narration: Mapping[str, tuple[float, float]], spec: Any) -> PaceReport:
    """How the planned *document* reads: each scene's length, and each recording scene's still screen while
    its narration (*narration*: ``scene id -> (start, end)`` in seconds of the video) speaks; *takes* the recordings
    (``timeline.TakeMedia`` by take id), *spec* the effective spec."""
    by_id = {scene.id: scene for scene in spec.scenes}
    scenes, recordings = [], set()
    words = 0
    spoken_seconds = 0.0
    for planned in document.get("scenes") or []:
        sid = str(planned.get("id") or "")
        start, end = float(planned.get("start") or 0.0), float(planned.get("end") or 0.0)
        authored = by_id.get(sid)
        span = narration.get(sid)
        if span is not None and authored is not None and authored.narration:
            words += len(re.findall(r"\w+", authored.narration))
            spoken_seconds += max(0.0, float(span[1]) - float(span[0]))
        take = takes.get(getattr(authored, "take_id", "") or "") if planned.get("type") == "recording" else None
        if take is None:
            scenes.append(ScenePace(sid, round(end - start, 2)))
            continue
        recordings.add(sid)
        local = (float(span[0]) - start, float(span[1]) - start) if span is not None else (0.0, 0.0)
        still, longest, since = still_under(planned, take, local) if span is not None else (0.0, 0.0, 0.0)
        scenes.append(ScenePace(sid, round(end - start, 2), still, longest, since))
    return PaceReport(scenes, words, round(spoken_seconds, 2), recordings)
