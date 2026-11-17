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

"""A recording slowed to its narration by the save: the speed computed is applied, never handed back.

A recording scene whose narration outruns its recording, even with the
pauses it compressed given back (``extend_mode: zoom``), drifts in on its
last frame (``quality``'s ``held_frame``). The speed that makes the
recording last as long is known (``timeline.speed_to_fill``): with
``fit: auto`` - a recording scene's default - the save sets it
(:func:`fits`, :func:`apply`) and says so ("speed set to 0.69
automatically"). It never goes below :data:`SLOWEST_FIT_SPEED`: a recording
still short there keeps that speed and the advisory says how many seconds
are still missing, never a number to type.

The other way: in a dynamic video (``VideoSpec.pace``), a recording
that runs past its narration by more than :data:`LONGER_SLACK` is sped up to
end with it, never above ``pace.FASTEST_FIT_SPEED`` - its trailing hold is cut
already (``end_hold_ms``) - and the speed applied is said the same way.
"""

from __future__ import annotations

import copy
import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

#: The slowest a fit plays a recording: slower, its cursor and its typing read as slow motion.
SLOWEST_FIT_SPEED = 0.5
#: Seconds a fit at :data:`SLOWEST_FIT_SPEED` may still fall short of its narration and be said to fit.
_SHORT_TOLERANCE = 0.05
#: Seconds a recording of a dynamic video may run past its narration before a fit speeds it up.
LONGER_SLACK = 0.3


@dataclass(frozen=True)
class Fit:
    """The speed the save sets on a recording scene (:func:`fits`): what it then plays, and what its scene needs."""

    scene_id: str
    speed: float
    plays: float
    needs: float

    @property
    def short(self) -> float:
        """Seconds the recording still ends before its narration at :attr:`speed`."""
        return max(0.0, self.needs - self.plays)

    @property
    def clamped(self) -> bool:
        """Whether the fit stopped at :data:`SLOWEST_FIT_SPEED` with the recording still short."""
        return self.speed <= SLOWEST_FIT_SPEED and self.short > _SHORT_TOLERANCE

    @property
    def longer(self) -> float:
        """Seconds the recording still runs past its narration at :attr:`speed` (a dynamic video's speed-up)."""
        return max(0.0, self.plays - self.needs)

    def words(self) -> str:
        """``scene `demo`: speed set to 0.69 automatically (its recording now plays 16.0 s, as long as its narration)``."""
        from app.services.video_studio.pace import FASTEST_FIT_SPEED

        said = f"scene `{self.scene_id}`: speed set to {self.speed:g} automatically"
        if self.clamped:
            return (
                f"{said}, the slowest a fit plays (fit: auto): its recording still ends {self.short:.1f} s before its "
                "narration - record more of the flow, or shorten the narration"
            )
        if self.speed >= FASTEST_FIT_SPEED and self.longer > LONGER_SLACK:
            return (
                f"{said}, the fastest a fit plays in a dynamic video: its recording still runs {self.longer:.1f} s past "
                "its narration - trim it (trim_start, trim_end), or say more over it"
            )
        return f"{said} (its recording now plays {self.plays:.1f} s, as long as its narration)"


def _faster(played_at: Any, need: float, current: float, fastest: float) -> float | None:
    """The slowest speed (two decimals, above *current*, at most *fastest*) at which a recording playing
    ``played_at(speed)`` seconds ends within *need*; *fastest* when none does; ``None`` when it already does."""
    if played_at(current) <= need + LONGER_SLACK or current >= fastest:
        return None
    if played_at(fastest) > need:
        return fastest
    low, high = current, fastest
    for _ in range(40):
        middle = (low + high) / 2
        if played_at(middle) <= need:
            high = middle
        else:
            low = middle
    return min(fastest, math.ceil(high * 100) / 100)


def fits(spec: Any, document: dict, takes: Mapping[str, Any], narration: Mapping[str, Any] | None = None) -> list[Fit]:
    """The speed each recording scene of *spec* (``fit: auto``) needs to last as long as its narration, as planned in
    *document* with the recordings *takes* (``timeline.TakeMedia`` by take id); only the scenes it changes. In a
    dynamic video a recording longer than its narration - *narration*: ``scene id -> (start, end)`` in seconds
    of the video - is sped up to end with it."""
    from app.services.video_studio.pace import DYNAMIC, FASTEST_FIT_SPEED
    from app.services.video_studio.quality import HELD_FRAME_SECONDS, held_seconds
    from app.services.video_studio.timeline import EXTEND_MIN, NARRATION_TAIL, recording_seconds, speed_to_fill

    planned_by_id = {scene.get("id"): scene for scene in document.get("scenes") or []}
    dynamic = getattr(spec, "pace", None) == DYNAMIC
    found: list[Fit] = []
    for scene in spec.scenes:
        if scene.type != "recording" or scene.fit != "auto":
            continue
        planned, take = planned_by_id.get(scene.id), takes.get(scene.take_id)
        points = (planned or {}).get("time_map") or []
        if take is None or len(points) < 2:
            continue
        hold_end = float(scene.hold_end or 0.0)
        if held_seconds(planned) - hold_end < HELD_FRAME_SECONDS:
            spoken = (narration or {}).get(scene.id)
            if not dynamic or spoken is None:
                continue
            # The narration's end and its tail, in seconds of the scene: what the recording may last.
            said = float(spoken[1]) + NARRATION_TAIL - float(planned.get("start") or 0.0) - hold_end

            def played(speed: float, scene: Any = scene, take: Any = take) -> float:
                return recording_seconds(take, scene, speed=speed)

            current = float(scene.speed)
            if (faster := _faster(played, said, current, FASTEST_FIT_SPEED)) is not None:
                found.append(Fit(str(scene.id), faster, played(faster), said))
            continue
        need = float(points[-1][0]) - hold_end - EXTEND_MIN
        fill_to = need if scene.extend_mode == "zoom" else 0.0

        def played_at(speed: float, scene: Any = scene, take: Any = take, fill_to: float = fill_to) -> float:
            return recording_seconds(take, scene, speed=speed, fill_to=fill_to)

        current = float(scene.speed)
        speed = speed_to_fill(played_at, need, current=current)
        if speed is None or speed < SLOWEST_FIT_SPEED:
            speed = min(current, SLOWEST_FIT_SPEED)
        if speed >= current:
            continue
        found.append(Fit(str(scene.id), speed, played_at(speed), need))
    return found


def apply(authored: dict, found: list[Fit]) -> dict:
    """*authored* with the speed of each fit (:func:`fits`) set on its recording scene."""
    speeds = {fit.scene_id: fit.speed for fit in found}
    fitted = copy.deepcopy(authored)
    for scene in fitted.get("scenes") or []:
        if isinstance(scene, dict) and scene.get("id") in speeds:
            scene["speed"] = speeds[scene["id"]]
    return fitted
