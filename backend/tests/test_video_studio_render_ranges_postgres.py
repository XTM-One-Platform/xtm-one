"""Who draws a scene range of a render, against PostgreSQL: one conditional ``UPDATE`` decides it.

* a render's ranges are made once, with their executions - none for the
  first, which the render draws itself -, and read again by another attempt
  drawing from the same bundle; planned anew, the old ones are cancelled;
* a pending range goes to the first who claims it; its own execution run
  again takes it back, never a range the render took; the render takes over a
  range whose worker stopped, and that worker's writes no longer land; a drawn
  range is never taken again;
* the ranges of a render that ended are forgotten.

``integration``-marked and skips when PostgreSQL is unreachable; the
temporary database is the shared ``tests/scratch_database.py``.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.models.orchestration import OrchestrationExecution
from app.models.video_studio import VideoRenderRange
from app.services.video_studio import jobs, render_ranges
from tests.scratch_database import scratch_database
from tests.test_video_studio_render_ranges import _Store

pytestmark = [pytest.mark.integration]

RENDER = render_ranges.RENDER_OWNER


async def test_who_draws_a_scene_range_is_decided_by_one_conditional_update(monkeypatch) -> None:
    from app.services import file_storage as storage_module

    async def nothing(*_args, **_kwargs):
        return None

    monkeypatch.setattr(jobs, "wake", nothing)
    monkeypatch.setattr(storage_module, "file_storage", _Store())
    tables = (VideoRenderRange.__table__, OrchestrationExecution.__table__)
    async with scratch_database("video_render_ranges", tables=tables) as scratch:
        render_id = str(uuid.uuid4())
        planned = [render_ranges.Range(part, part * 100, part * 100 + 100, (f"s{part}",)) for part in range(3)]
        rows = await render_ranges.open_ranges(render_id, "d" * 64, planned, quality="high")
        assert [row.part for row in rows] == [0, 1, 2]
        assert rows[0].execution_id is None and all(row.execution_id for row in rows[1:])
        # Another attempt drawing from the same bundle finds the same ranges, their executions made once.
        again = await render_ranges.open_ranges(render_id, "d" * 64, planned, quality="high")
        assert [row.id for row in again] == [row.id for row in rows]
        async with scratch.sessions() as db:
            executions = (await db.execute(select(OrchestrationExecution))).scalars().all()
        assert sorted(execution.payload["range"] for execution in executions) == [1, 2]
        one, two = rows[1], rows[2]

        # A pending range goes to the first who claims it.
        assert await render_ranges.claim(one.id, by="worker-b", execution_id=one.execution_id)
        assert not await render_ranges.claim(one.id, by=RENDER)
        # Its own execution, run again on another worker, takes it back: the first worker's writes no longer land.
        assert await render_ranges.claim(one.id, by="worker-c", execution_id=one.execution_id)
        assert not await render_ranges.progress(one.id, by="worker-b", frames_done=10)
        assert await render_ranges.progress(one.id, by="worker-c", frames_done=10)
        # The render takes a pending range; that range's execution, starting late, never takes it back.
        assert await render_ranges.claim(two.id, by=RENDER)
        assert not await render_ranges.claim(two.id, by="worker-b", execution_id=two.execution_id)
        # The render takes over a range whose worker stopped.
        assert await render_ranges.claim(one.id, by=RENDER, take_over=True)
        assert not await render_ranges.progress(one.id, by="worker-c", frames_done=50)
        assert not await render_ranges.finish(one.id, by="worker-c")
        assert await render_ranges.finish(one.id, by=RENDER, measured={"frames": 100})
        drawn = await render_ranges.load(one.id)
        assert (drawn.status, drawn.frames_done, drawn.measured) == ("ready", 100, {"frames": 100})
        # Drawn: nobody takes it again.
        assert not await render_ranges.claim(one.id, by=RENDER, take_over=True)

        # Planned anew: the ranges still to draw are cancelled with their executions, the new ones read instead.
        anew = await render_ranges.open_ranges(render_id, "e" * 64, planned[:2], quality="high")
        assert [row.digest for row in await render_ranges.rows_of(render_id)] == ["e" * 64, "e" * 64]
        assert (await render_ranges.load(two.id)).status == "cancelled"
        async with scratch.sessions() as db:
            states = dict((await db.execute(select(OrchestrationExecution.id, OrchestrationExecution.state))).all())
        assert states[two.execution_id] == "cancelled" and states[anew[1].execution_id] == "pending"

        # The render ended: its ranges go, their executions cancelled.
        await render_ranges.forget(render_id)
        async with scratch.sessions() as db:
            left = (await db.execute(select(func.count()).select_from(VideoRenderRange))).scalar_one()
            state = (
                await db.execute(select(OrchestrationExecution.state).where(OrchestrationExecution.id == anew[1].execution_id))
            ).scalar_one()
        assert left == 0 and state == "cancelled"
