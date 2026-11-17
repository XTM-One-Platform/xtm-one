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

"""What a video's and the platform's earlier renders measured, read for the next pre-flight.

Out of ``render_preflight``, which prices a render from it: the worker shape an estimate is made for
(:class:`Shape`), what an estimate said against what its render took (:class:`Missed`, :func:`estimate_error`,
:func:`estimate_errors`), and what the last finished render of a video measured (:class:`OwnRender`,
:func:`own_render`, :func:`parallel_efficiency`). Rates are read per weighted pixel with the pre-flight's own measures
(``render_preflight.measured_scene_rates``, ``stage_rates``), imported where they are read: the pre-flight imports
this module, never the other way round at import time.

:func:`estimate_errors` filters in the database - the quality, the estimate model, the worker shape the estimate was
made for and drawn on, the history it was priced from - and reads only what an error is made of, never the last
renders' whole ``measured`` and ``preflight`` on every pre-flight.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, NamedTuple

from app.core.utils import safe_uuid

logger = logging.getLogger(__name__)

_MIB = 1 << 20
#: The estimates whose errors are read: the last whole renders of the quality on the same worker shape, estimated by
#: this same model from the same kind of history.
ERRORS_READ = 5
#: The whole renders at a quality read, newest first, to find those :data:`ERRORS_READ`.
ERROR_RENDERS_READ = 60
#: The whole renders of a video read for what it measured, newest first.
VIDEO_RENDERS_READ = 5


@dataclass(frozen=True)
class Shape:
    """The worker shape a render is drawn on: its pages, its CPUs and its memory ceiling, in MiB to the nearest
    256 (``0``: unknown) - what the correction by the last estimates' errors is read per."""

    pages: int
    cpus: int
    memory_mib: int = 0

    @classmethod
    def of(cls, pages: int, cpus: int, ceiling: int | None) -> Shape:
        return cls(int(pages), int(cpus), round((ceiling or 0) / _MIB / 256) * 256)

    @classmethod
    def recorded(cls, preflight: Any) -> Shape | None:
        """The shape a kept estimate (``render_preflight.Preflight.as_dict``) was made for; ``None`` for one that kept
        none."""
        shape = preflight.get("shape") if isinstance(preflight, dict) else None
        if not isinstance(shape, dict):
            return None
        try:
            return cls(int(shape["pages"]), int(shape["cpus"]), int(shape.get("memory_mib") or 0))
        except (KeyError, TypeError, ValueError):
            return None

    def as_dict(self) -> dict:
        return {"pages": self.pages, "cpus": self.cpus, "memory_mib": self.memory_mib}

    def words(self) -> str:
        """``8 pages of 9 CPUs, a 30.0 GiB memory ceiling``."""
        said = f"{self.pages} page{'s' if self.pages != 1 else ''} of {self.cpus} CPU{'s' if self.cpus != 1 else ''}"
        return said + (f", a {self.memory_mib / 1024:.1f} GiB memory ceiling" if self.memory_mib else ", no memory ceiling known")


class Missed(NamedTuple):
    """What a whole render's estimate said, before its correction, against what the render took, in seconds, and the
    render."""

    said: float
    took: float
    render: str = ""


@dataclass(frozen=True)
class OwnRender:
    """What the last finished render of a video at a quality measured (:func:`own_render`): what prices every
    scene type it drew in the next estimate of the same video, before the platform's median across videos."""

    render_id: str
    pages: int
    cpus: int
    #: Weighted pixel-frames a second one page with ``render_preflight.PAGE_CPUS`` CPUs drew of each scene type
    #: (``render_preflight.scene_class``), on its own pages - contended or not (:attr:`contended`).
    scene_rates: dict
    contended: bool = False
    #: Its pages' parallel efficiency (:func:`parallel_efficiency`); ``None`` when it was not measured.
    efficiency: float | None = None
    #: What its stages took per their unit (``render_preflight.stage_rates``), and its figures stage's rate.
    stages: dict = field(default_factory=dict)
    figure_rate: float | None = None
    #: The scene types it drew on a shared worker and does not price (``render_pace.learned_scenes``).
    shared: tuple = ()

    @classmethod
    def of(
        cls, render_id: object, quality: str, width: int, height: int, duration_ms: int | None, measured: Any
    ) -> OwnRender | None:
        """What *measured* (a whole render's ``VideoRender.measured`` at *quality*) prices; ``None`` when it timed no
        scene type, or drew too few frames to set a rate. Drawn on a shared worker, only the scene types it
        found over their cost: the others' measures are the worker's share as much as their own cost."""
        from app.services.video_studio import render_pace, render_preflight

        if not isinstance(measured, dict):
            return None
        try:
            frames = int(measured.get("frames") or 0)
            pages, cpus = max(1, int(measured.get("pages") or 1)), max(1, int(measured.get("cpus") or 1))
        except (TypeError, ValueError):
            return None
        drawn = (
            render_preflight.measured_scene_rates(quality, width, height, measured)
            if frames >= render_preflight.MIN_MEASURED_FRAMES
            else {}
        )
        learned = render_pace.learned_scenes(measured, drawn)
        rates = {name: rate for name, rate in drawn.items() if name in learned}
        if not rates:
            return None
        return cls(
            str(render_id),
            pages,
            cpus,
            rates,
            render_preflight.contended(pages, cpus),
            parallel_efficiency(measured),
            # Its stages ran on frames read back drawn: what they took says less than drawing costs (``insert_frames``);
            # on a shared worker, they took the worker's share as well.
            {}
            if read_back(measured) or len(learned) < len(drawn)
            else render_preflight.stage_rates(measured, width, height, (duration_ms or 0) / 1000),
            render_preflight.measured_figure_rate(quality, width, height, measured),
            tuple(sorted(name for name in drawn if name not in learned)),
        )

    @property
    def short(self) -> str:
        return self.render_id[:8]


def parallel_efficiency(measured: Any) -> float | None:
    """How much of its pages' time over the whole frames stage a render's frames took: its frames a second over
    the run divided by its pages, against what a page took for a frame - the page-seconds its frames took over its
    pages times the stage's seconds, at most 1.

    The rest is what no frame's time holds: a page's start, each segment's encoder opened and flushed, the frames drawn
    uncaptured into a matted insert, the pages left idle while another draws the tail. ``None`` for a render that read
    frames back drawn (``insert_frames``: their page-seconds are a read's), or timed none.
    """
    if not isinstance(measured, dict) or read_back(measured):
        return None
    try:
        pages, seconds = max(1, int(measured["pages"])), float(measured["seconds"])
    except (KeyError, TypeError, ValueError):
        return None
    timed = measured.get("kinds") or measured.get("scenes")
    spent = 0.0
    for drawn in timed.values() if isinstance(timed, dict) else ():
        try:
            spent += max(0.0, float(drawn[1]))
        except (IndexError, TypeError, ValueError):
            continue
    if seconds <= 0 or spent <= 0:
        return None
    return min(1.0, spent / (pages * seconds))


def read_back(measured: Any) -> bool:
    """Whether a render read screen-insert frames back from its worker's disk (``insert_frames``): those frames cost it
    a read, not a drawing, so its whole rate, its stages and its time say less than a drawing costs."""
    inserts = measured.get("insert_frames") if isinstance(measured, dict) else None
    return isinstance(inserts, dict) and bool(inserts.get("cached"))


def estimate_error(preflight: Any, measured: dict) -> tuple[float, float] | None:
    """``(said, took)`` of a whole render this model estimated and that drew every frame in one attempt, its
    pace never explained by its scene types or its worker (``render_pace.explained``); *took* set back to an
    idle worker's at the share of its worker's CPU it got when it shared it (``cpu_share.idle_equivalent``):
    the estimate it corrects is an idle worker's."""
    from app.services.video_studio import cpu_share, render_pace
    from app.services.video_studio.render_preflight import ESTIMATE_MODEL

    if not isinstance(preflight, dict) or preflight.get("model") != ESTIMATE_MODEL:
        return None
    if render_pace.explained(measured):
        return None
    if (
        measured.get("frames_reused")
        or measured.get("frames_inherited")
        or int(measured.get("ranges") or 1) > 1
        or not measured.get("total_seconds")
        or read_back(measured)
    ):
        return None
    said = float(preflight.get("raw_seconds") or 0)
    took = cpu_share.idle_equivalent(float(measured["total_seconds"]), measured.get("contention"))
    return (said, took) if said > 0 and took > 0 else None


async def estimate_errors(db: Any, quality: str, *, shape: Shape, costs_from: str) -> tuple[Missed, ...]:
    """What the last :data:`ERRORS_READ` estimates at *quality* said against what their renders took, for the
    correction of the next one (``render_preflight.Rates.correction``): whole renders estimated by this model
    for this very worker *shape* - its pages, CPUs and memory ceiling - and drawn on its pages and CPUs, in one attempt
    (:func:`estimate_error`), priced from the same kind of history (*costs_from*): an estimate made from its video's
    own render and one made from the platform's err differently. Read among the last :data:`ERROR_RENDERS_READ` whole
    renders at *quality*, newest first, filtered in the database and read for what an error is made of only: the
    model, the shape and the history it was priced from in its ``preflight``, the pages and CPUs it drew on in its
    ``measured`` - each row checked again as it is read."""
    from sqlalchemy import func, select

    from app.models.video_studio import JOB_READY, VideoRender
    from app.services.video_studio.render_preflight import ESTIMATE_MODEL

    recorded, drew = VideoRender.preflight, VideoRender.measured
    rows = (
        await db.execute(
            select(
                VideoRender.id,
                drew["pages"].as_string(),
                drew["cpus"].as_string(),
                drew["frames_reused"].as_string(),
                drew["frames_inherited"].as_string(),
                drew["ranges"].as_string(),
                drew["total_seconds"].as_string(),
                drew["insert_frames"],
                drew["pace"],
                drew["contention"],
                recorded["model"].as_string(),
                recorded["shape"],
                recorded["costs_from"].as_string(),
                recorded["raw_seconds"].as_string(),
            )
            .where(
                VideoRender.scene_id.is_(None),
                VideoRender.status == JOB_READY,
                VideoRender.quality == quality,
                VideoRender.preflight.is_not(None),
                recorded["model"].as_string() == str(ESTIMATE_MODEL),
                recorded["shape"]["pages"].as_string() == str(shape.pages),
                recorded["shape"]["cpus"].as_string() == str(shape.cpus),
                func.coalesce(recorded["costs_from"].as_string(), "platform") == costs_from,
                drew["pages"].as_string() == str(shape.pages),
                drew["cpus"].as_string() == str(shape.cpus),
            )
            .order_by(VideoRender.finished_at.desc().nulls_last())
            .limit(ERROR_RENDERS_READ)
        )
    ).all()
    found: list[Missed] = []
    for row in rows:
        render_id, pages, cpus, reused, inherited, ranges, total, inserts, pace, contention = row[:10]
        model, kept_shape, priced, raw_seconds = row[10:]
        preflight = {"model": _number(model), "shape": kept_shape, "costs_from": priced, "raw_seconds": _number(raw_seconds)}
        measured = {
            "pages": _number(pages),
            "cpus": _number(cpus),
            "frames_reused": _number(reused),
            "frames_inherited": _number(inherited),
            "ranges": _number(ranges),
            "total_seconds": _number(total),
            "insert_frames": inserts,
            "pace": pace,
            "contention": contention,
        }
        if Shape.recorded(preflight) != shape:
            continue
        try:
            drew_on = (int(measured.get("pages") or 0), int(measured.get("cpus") or 0))
        except (TypeError, ValueError):
            continue
        if drew_on != (shape.pages, shape.cpus) or (preflight.get("costs_from") or "platform") != costs_from:
            continue
        error = estimate_error(preflight, measured)
        if error is not None:
            found.append(Missed(*error, str(render_id)))
        if len(found) >= ERRORS_READ:
            break
    return tuple(found)


def _number(value: Any) -> Any:
    """A JSON scalar read as text in SQL (``->>``) as the number it was written as; anything else as it is."""
    if not isinstance(value, str):
        return value
    try:
        number = float(value)
    except ValueError:
        return value
    return int(number) if number.is_integer() and "." not in value else number


async def own_render(db: Any, video_id: object, quality: str) -> OwnRender | None:
    """What the newest whole render of *video_id* at *quality* that timed its scene types measured
    (:class:`OwnRender`); ``None`` when none did."""
    from sqlalchemy import select

    from app.models.video_studio import JOB_READY, VideoRender

    project_id = safe_uuid(str(video_id)) if video_id is not None else None
    if project_id is None:
        return None
    rows = (
        await db.execute(
            select(VideoRender.id, VideoRender.width, VideoRender.height, VideoRender.duration_ms, VideoRender.measured)
            .where(
                VideoRender.project_id == project_id,
                VideoRender.scene_id.is_(None),
                VideoRender.status == JOB_READY,
                VideoRender.quality == quality,
                VideoRender.width.is_not(None),
                VideoRender.height.is_not(None),
            )
            .order_by(VideoRender.finished_at.desc().nulls_last())
            .limit(VIDEO_RENDERS_READ)
        )
    ).all()
    for render_id, width, height, duration_ms, measured in rows:
        found = OwnRender.of(render_id, quality, width, height, duration_ms, measured)
        if found is not None:
            return found
    return None
