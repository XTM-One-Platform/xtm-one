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

"""When a queued whole render starts: the renders it waits behind, how long each has left, its ETA to start.

A render queued behind another video's render read "1 render being made now, 0 queued ahead of it" for 25 minutes:
the pre-flight's estimate it was answered with covers its own drawing, never the wait before it. The render lane's
slots are held by the renders being made, so a queued one starts once as many of them have ended as there are
renders queued before it, plus one. Each render being made has its end (``render_pace.eta_of``: its measured pace
once a tenth of its frames are drawn, else its start plus its pre-flight's estimate), each one queued ahead its
pre-flight's estimate: :func:`starts_in` deals them onto the slots as they free, in queue order.

No other video is named: a render being made is "the render being made", whoever's video it is.
"""

from __future__ import annotations

import heapq
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any


@dataclass(frozen=True)
class Ahead:
    """A whole render a queued one waits behind: being made (its seconds left) or queued before it (its estimate)."""

    running: bool
    #: Seconds left (being made) or its pre-flight's estimate (queued ahead); ``None`` when neither is known.
    seconds: float | None
    #: Where *seconds* comes from: ``measured`` (its pace), ``preflight`` (its estimate).
    source: str | None = None
    #: A render being made past the end its estimate gave: its seconds left are 0, its end not known.
    overdue: bool = False

    def as_dict(self) -> dict:
        return {
            "running": self.running,
            "seconds": round(self.seconds) if self.seconds is not None else None,
            "source": self.source,
            "overdue": self.overdue,
        }


def starts_in(running: Sequence[float | None], ahead: Sequence[float | None]) -> float | None:
    """Seconds until a queued render starts: the slots the *running* renders hold free as each ends (its seconds
    left), each taken in turn by the next render queued *ahead* for its estimate. ``None`` when no render is being made
    (it waits for a worker, not a slot) or when one of them is not known."""
    if not running or any(seconds is None for seconds in (*running, *ahead)):
        return None
    free = sorted(max(0.0, float(seconds)) for seconds in running if seconds is not None)
    for seconds in ahead:
        heapq.heappush(free, heapq.heappop(free) + max(0.0, float(seconds or 0.0)))
    return free[0]


def _minutes(seconds: float) -> str:
    return f"{round(seconds)} s" if seconds < 120 else f"{round(seconds / 60)} min"


def _instant(value: Any) -> datetime | None:
    """An ETA's ``at`` (ISO text) in UTC; ``None`` when there is none."""
    if not isinstance(value, str):
        return None
    try:
        found = datetime.fromisoformat(value)
    except ValueError:
        return None
    return found.replace(tzinfo=UTC) if found.tzinfo is None else found


@dataclass(frozen=True)
class RenderWait:
    """What a queued whole render waits behind on the render lane, and when it starts (:func:`starts_in`)."""

    renders: tuple[Ahead, ...]
    starts_in_s: float | None

    def words(self) -> str:
        """``the render being made ends in about 18 min (by its measured pace): this one starts in about 18 min``."""
        running = [render for render in self.renders if render.running]
        queued = [render for render in self.renders if not render.running]
        if not running:
            return ""
        parts = []
        for render in running:
            if render.overdue:
                parts.append("is past the end its estimate gave")
            elif render.seconds is None:
                parts.append("has no estimate of its end")
            else:
                pace = "its measured pace" if render.source == "measured" else "its pre-flight's estimate"
                parts.append(f"ends in about {_minutes(render.seconds)} (by {pace})")
        if len(running) == 1:
            said = f"the render being made {parts[0]}"
        else:
            said = f"of the {len(running)} renders being made, " + ", ".join(f"one {part}" for part in parts)
        if queued:
            known = [render.seconds for render in queued if render.seconds is not None]
            took = f", about {_minutes(sum(known))} between them" if len(known) == len(queued) else ""
            said += f"; {len(queued)} queued before it{took}"
        if self.starts_in_s is None:
            return f"{said} - when this one starts is not known"
        if self.starts_in_s <= 0:
            return f"{said} - this one starts as soon as a slot frees"
        return f"{said} - this one starts in about {_minutes(self.starts_in_s)}"

    def as_dict(self) -> dict:
        return {
            "renders_ahead": [render.as_dict() for render in self.renders],
            "starts_in_seconds": round(self.starts_in_s) if self.starts_in_s is not None else None,
        }


def _left(render: Any, now: datetime) -> Ahead:
    """A render being made: its seconds left by its ETA (``render_pace.eta_of``), or its whole estimate while no
    worker has started it (one a stopped worker left, resumed first)."""
    from app.services.video_studio import render_pace

    eta = render_pace.eta_of(render)
    if eta is None:
        return Ahead(running=True, seconds=None)
    at = _instant(eta.get("at"))
    if at is None:
        said = eta.get("expected_seconds")
        return Ahead(running=True, seconds=float(said) if said is not None else None, source=eta["source"])
    left = (at - now).total_seconds()
    return Ahead(running=True, seconds=max(0.0, left), source=eta["source"], overdue=left < 0)


def _expected(render: Any) -> Ahead:
    """A render queued before the queued one: its pre-flight's estimate."""
    expected = getattr(render, "preflight", None)
    said = expected.get("expected_seconds") if isinstance(expected, dict) else None
    return Ahead(
        running=False,
        seconds=float(said) if said is not None else None,
        source="preflight" if said is not None else None,
    )


def wait_of(running: Sequence[Any], ahead: Sequence[Any], *, now: datetime | None = None) -> RenderWait:
    """The wait of a queued whole render behind the *running* renders of its lane and those queued *ahead* of it, in
    queue order (``VideoRender`` rows)."""
    now = now or datetime.now(UTC)
    renders = (*(_left(render, now) for render in running), *(_expected(render) for render in ahead))
    return RenderWait(
        renders,
        starts_in(
            [render.seconds for render in renders if render.running],
            [render.seconds for render in renders if not render.running],
        ),
    )


async def read(session: Any, being_made: Any, queued_ahead: Sequence[Any], *, now: datetime | None = None) -> RenderWait:
    """:func:`wait_of` read on *session* at *now*: the whole renders whose execution on the render lane is
    *being_made*, and those whose execution matches *queued_ahead* (``jobs.queue_place``'s conditions), in queue
    order."""
    from sqlalchemy import select

    from app.models.orchestration import OrchestrationExecution
    from app.models.video_studio import VideoRender

    async def renders_of(*conditions: Any) -> list:
        """The renders of the executions matching *conditions*, in the order the lane takes them."""
        executions = (await session.execute(select(OrchestrationExecution).where(*conditions))).scalars().all()
        ids = [execution.id for execution in sorted(executions, key=lambda e: (e.run_at, e.created_at))]
        if not ids:
            return []
        rows = (await session.execute(select(VideoRender).where(VideoRender.execution_id.in_(ids)))).scalars().all()
        by_execution = {row.execution_id: row for row in rows}
        return [by_execution[execution_id] for execution_id in ids if execution_id in by_execution]

    return wait_of(await renders_of(being_made), await renders_of(*queued_ahead), now=now)
