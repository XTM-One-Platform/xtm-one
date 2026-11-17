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

"""Which scenes each encoded frame of a planned video paints: the one table every check of a render reads.

The page draws output frame ``n`` at ``T = n / fps`` (``compositor_page.renderFrame``) and paints every scene with
``T >= start`` and ``T < end``, the last scene to the video's end. A scene's encoded frames are therefore one run
``[first, end)``: ``first`` the first frame whose time reaches its start, ``end`` the first whose time reaches its end
- about ``ceil(start * fps)`` and ``ceil(end * fps)``, read as the page compares them (:func:`first_at`), never a second
past a painted end landing one frame over. A cut gives the frame it falls inside to the scene coming in; a
transition's frames are painted by both scenes and owned by the one coming in, but for its first: the page
draws it at its progress 0 or a hair past it (``applyTransition``), the scene before whole, whatever second the plan
starts the scene at - that frame is the scene before's.

Under motion blur a frame's sub-frames are drawn at their own times, but never across a cut: a sub-frame whose time
falls on the other side of a cut than its frame's centre is drawn at the centre (:func:`shutter_at`), so a frame next
to a cut is one scene's picture, never a mix of the two - the owner this table names.

:class:`FrameOwners` is built from the planned document, once per render, and read by the frame server
(``timeline.whole_frames``) and by the checks: the frames the presence gate reads an insert on and the joins it
names (``insert_checks.placed_inserts``), the frames an insert's checks judge it on - those its scene owns alone
(:meth:`FrameOwners.alone`), and those of its transitions that show it at the presence gate's floor
(``insert_checks.Placed.owns_frame``): its flicker, its flicker fact and its presence -, the transitions that blend it
(``insert_checks.blend_windows``) and the scene each frame of the frozen-picture watch shows (``frame_freeze``).
A check that works the frames out from seconds on its own is a test failure
(``tests/test_video_studio_frame_owners_6433.py``).
"""

from __future__ import annotations

import bisect
import math
from dataclasses import dataclass, field

#: The frames each side of a cut its join is read on: the last ones of the scene it leaves, the first of the next.
CUT_FRAMES = 2


def first_at(seconds: float, fps: float) -> int:
    """The first frame ``n >= 0`` whose time ``n / fps`` reaches *seconds*: the comparison the page makes (``T >=``)."""
    n = max(0, math.ceil(seconds * fps))
    while n > 0 and (n - 1) / fps >= seconds:
        n -= 1
    while n / fps < seconds:
        n += 1
    return n


def total_frames(document: dict) -> int:
    """The frames a planned *document* encodes: its ``frames``, else its duration's, else its last scene's end's."""
    fps = max(float(document.get("fps") or 30.0), 1.0)
    ends = [float(scene.get("end") or 0.0) for scene in document.get("scenes") or [] if isinstance(scene, dict)]
    return int(document.get("frames") or 0) or math.ceil(float(document.get("duration") or max(ends, default=0.0)) * fps)


def cut_times(document: dict) -> tuple[float, ...]:
    """The seconds at which a scene of a planned *document* comes in by a cut (no transition that lasts), in order."""
    scenes = [scene for scene in document.get("scenes") or [] if isinstance(scene, dict)]
    return tuple(
        float(scene.get("start") or 0.0)
        for scene in scenes[1:]
        if float((scene.get("transition_in") or {}).get("duration") or 0.0) <= 0
    )


def shutter_at(frame: int, at: float, fps: float, cuts: tuple[float, ...]) -> float:
    """Where the page draws a sub-frame asked at *at* (in frames) for output *frame*: *at* itself, but *frame* when a
    cut of *cuts* (seconds) falls between the two as the page compares them (``T >= start``) - the shutter of a frame
    never opens on the scene across a cut."""
    rate = max(float(fps), 1.0)
    centre, sub = frame / rate, at / rate
    low, high = min(centre, sub), max(centre, sub)
    for cut in cuts:
        if (centre >= cut) != (sub >= cut) and low <= cut <= high:
            return float(frame)
    return at


@dataclass(frozen=True)
class FrameOwners:
    """The encoded frames ``[first, end)`` each scene of a planned document is painted on, by scene index."""

    fps: float
    total: int
    ids: tuple[str, ...]
    spans: tuple[tuple[int, int], ...]
    #: Whether each scene comes in through a transition that lasts (its ``transition_in`` duration over 0).
    blended: tuple[bool, ...]
    _firsts: tuple[int, ...] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_firsts", tuple(first for first, _end in self.spans))

    @classmethod
    def of(cls, document: dict) -> FrameOwners:
        fps = max(float(document.get("fps") or 30.0), 1.0)
        total = total_frames(document)
        scenes = [scene for scene in document.get("scenes") or [] if isinstance(scene, dict)]
        spans = []
        for index, scene in enumerate(scenes):
            first = min(first_at(float(scene.get("start") or 0.0), fps), total)
            end = total if index == len(scenes) - 1 else min(first_at(float(scene.get("end") or 0.0), fps), total)
            spans.append((first, max(first, end)))
        return cls(
            fps,
            total,
            tuple(str(scene.get("id") or f"scene {index + 1}") for index, scene in enumerate(scenes)),
            tuple(spans),
            tuple(float((scene.get("transition_in") or {}).get("duration") or 0.0) > 0 for scene in scenes),
        )

    def frames_of(self, index: int) -> range:
        """The encoded frames the scene at *index* is painted on, its first to its last inclusive."""
        first, end = self.spans[index]
        return range(first, end)

    def owns(self, index: int, frame: int) -> bool:
        """Whether the scene at *index* is painted on encoded *frame*."""
        first, end = self.spans[index]
        return first <= frame < end

    def scenes_on(self, frame: int) -> tuple[int, ...]:
        """The indexes of the scenes painted on encoded *frame*, in the order the page stacks them."""
        started = bisect.bisect_right(self._firsts, frame)
        return tuple(index for index in range(started) if frame < self.spans[index][1])

    def owner(self, frame: int) -> int | None:
        """The scene encoded *frame* belongs to: the later one where two are painted (a transition's frame) - but on the
        first frame of a transition in that lasts, the scene before: the page draws that frame at its progress
        0 or a hair past it, the scene before whole, whatever second the plan starts the scene coming in at."""
        started = bisect.bisect_right(self._firsts, frame)
        painted = [index for index in range(started) if frame < self.spans[index][1]]
        if not painted:
            return None
        if len(painted) > 1 and self.blended[painted[-1]] and frame == self.spans[painted[-1]][0]:
            return painted[-2]
        return painted[-1]

    def owned(self, index: int) -> range:
        """The encoded frames the scene at *index* owns (:meth:`owner`): those it is painted on that no later scene is,
        and the first frame of the transition in of the scene after it - its own transition in's first frame is the
        scene before's, its transition out's other frames the scene coming in's."""
        first, end = self.spans[index]
        start = first + 1 if first < end and self.owner(first) != index else first
        later = [
            begun + 1 if self.owner(begun) == index else begun
            for begun, stop in self.spans[index + 1 :]
            if start <= begun < end and stop > begun
        ]
        return range(start, max(start, min([end, *later])))

    def alone(self, index: int) -> range:
        """The encoded frames the scene at *index* owns alone: those of :meth:`owned` no earlier scene is painted
        on - past its transition in, the page's progress 1 there -, the first frame of the next scene's transition in
        included (its progress 0, the scene at *index* whole)."""
        owned = self.owned(index)
        start = max([owned.start, *(stop for first, stop in self.spans[:index] if first <= owned.start < stop)])
        return range(min(start, owned.stop), owned.stop)

    def joins(self) -> list[tuple[int, int, int]]:
        """``(first, end, index of the scene coming in)`` of every join between two scenes: a transition's
        frames - those both scenes are painted on -, a cut's :data:`CUT_FRAMES` frames each side of it."""
        out = []
        for index in range(1, len(self.spans)):
            first = self.spans[index][0]
            if self.blended[index]:
                out.append((first, max(first, self.spans[index - 1][1]), index))
            else:
                out.append((max(0, first - CUT_FRAMES), min(self.total, first + CUT_FRAMES), index))
        return out

    def table(self) -> list[tuple[str, ...]]:
        """The scene ids painted on each encoded frame, by frame index."""
        out: list[list[str]] = [[] for _ in range(self.total)]
        for scene_id, (first, end) in zip(self.ids, self.spans, strict=True):
            for frame in range(first, end):
                out[frame].append(scene_id)
        return [tuple(ids) for ids in out]
