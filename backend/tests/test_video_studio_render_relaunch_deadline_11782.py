"""A render relaunched by deploys gets back the time the stops cost it (#11782). Synthetic rows only.

* ``orchestration_engine.give_deadline_back`` moves a running execution's deadline out by seconds or to a time,
  never in, never past its cap after the deadline it was queued with, only for its lease owner;
* ``render_process.give_relaunch_time_back`` gives back the time from the render's last progress to its relaunch;
* ``record_relaunch`` keeps what was lost and given back, and a timed-out render's error says it;
* ``PaceNote`` moves a steady render's deadline out to the end its measured pace gives.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.models.video_studio import JOB_RUNNING
from app.services import orchestration_engine as engine
from app.services.video_studio import jobs, render_process, render_ranges, runners


def _execution(now: datetime, *, hours: float = 1.0, **fields) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        state="running",
        lease_owner="worker-a",
        deadline_at=now + timedelta(hours=hours),
        updated_at=now,
        payload=fields.pop("payload", {}),
        **fields,
    )


def _engine_db(monkeypatch, row: SimpleNamespace) -> SimpleNamespace:
    async def locked(_db, _execution_id, **_kwargs):
        return row

    async def flush():
        return None

    monkeypatch.setattr(engine, "_locked_get", locked)
    return SimpleNamespace(flush=flush)


def test_time_given_back_moves_the_deadline_out_within_its_cap(monkeypatch):
    now = datetime.now(UTC)
    row = _execution(now)
    first = row.deadline_at
    db = _engine_db(monkeypatch, row)
    give = engine.give_deadline_back
    deadline, given = asyncio.run(give(db, execution_id=row.id, worker_id="worker-a", seconds=600, most_seconds=3600))
    assert deadline == first + timedelta(seconds=600) and given == pytest.approx(600)
    # The first deadline is kept: three more relaunches of 20 min each stop at the cap, an hour past it.
    for _ in range(3):
        asyncio.run(give(db, execution_id=row.id, worker_id="worker-a", seconds=1200, most_seconds=3600))
    assert row.deadline_at == first + timedelta(seconds=3600)
    assert row.payload[engine.DEADLINE_FIRST_KEY] == first.isoformat()
    # Past the cap: nothing more given.
    assert asyncio.run(give(db, execution_id=row.id, worker_id="worker-a", seconds=60, most_seconds=3600))[1] == 0.0


def test_a_deadline_is_never_moved_in_nor_by_another_worker(monkeypatch):
    now = datetime.now(UTC)
    row = _execution(now)
    db = _engine_db(monkeypatch, row)
    before = row.deadline_at
    deadline, given = asyncio.run(
        engine.give_deadline_back(db, execution_id=row.id, worker_id="worker-a", until=now, most_seconds=3600)
    )
    assert deadline == before and given == 0.0 and engine.DEADLINE_FIRST_KEY not in row.payload
    with pytest.raises(engine.LeaseLostError):
        asyncio.run(
            engine.give_deadline_back(db, execution_id=row.id, worker_id="worker-b", seconds=60, most_seconds=3600)
        )
    row.state = "completed"
    with pytest.raises(engine.LeaseLostError):
        asyncio.run(
            engine.give_deadline_back(db, execution_id=row.id, worker_id="worker-a", seconds=60, most_seconds=3600)
        )


async def test_a_relaunch_gives_back_the_time_since_its_last_progress(monkeypatch):
    calls: list[dict] = []
    row = SimpleNamespace(id=uuid.uuid4(), status=JOB_RUNNING, quality="high", progress_at=None)

    async def load(_kind, _job_id):
        return row

    async def give(_db, **kwargs):
        calls.append(kwargs)
        return datetime.now(UTC) + timedelta(hours=2), float(kwargs["seconds"])

    class _Session:
        async def __aenter__(self):
            async def commit():
                return None

            return SimpleNamespace(commit=commit)

        async def __aexit__(self, *_exc):
            return False

    from app.core import database

    monkeypatch.setattr(jobs, "load", load)
    monkeypatch.setattr(engine, "give_deadline_back", give)
    monkeypatch.setattr(database, "async_session", _Session)
    lease = (uuid.uuid4(), "worker-b")
    since = datetime.now(UTC) - timedelta(seconds=240)
    lost, given = await render_process.give_relaunch_time_back(str(row.id), lease, since=since)
    assert 239 <= lost <= 242 and given == lost
    assert calls[0]["worker_id"] == "worker-b" and calls[0]["most_seconds"] > 0
    # Nothing known of its last progress, or no lease: nothing given back.
    assert await render_process.give_relaunch_time_back(str(row.id), lease, since=None) == (0, 0)
    assert (await render_process.give_relaunch_time_back(str(row.id), None, since=since))[1] == 0

    # A store that fails keeps the deadline it had, never fails the render.
    async def broken(*_args, **_kwargs):
        raise RuntimeError("database gone")

    monkeypatch.setattr(engine, "give_deadline_back", broken)
    assert (await render_process.give_relaunch_time_back(str(row.id), lease, since=since))[1] == 0


def test_a_timed_out_render_says_what_its_relaunches_cost_it(monkeypatch):
    relaunches = [
        {"progress": 20, "lost_seconds": 110, "given_back_seconds": 110},
        {"progress": 41, "lost_seconds": 95, "given_back_seconds": 95},
        {"progress": 60, "lost_seconds": 600, "given_back_seconds": 0},
    ]
    assert render_process.relaunch_cost(relaunches) == (805, 205)
    assert render_process.relaunch_cost_words(relaunches) == (
        "It was relaunched 3 times after its worker stopped, which cost it about 13 min, 3 min of it given back to "
        "its time limit."
    )
    assert render_process.relaunch_cost_words(None) == ""
    # An entry written before #11782 counts as a relaunch with no cost known.
    assert render_process.relaunch_cost_words([{"progress": 5}]) == "It was relaunched 1 time after its worker stopped."

    row = SimpleNamespace(id=uuid.uuid4(), status=JOB_RUNNING, stage="Drawing", progress=73, relaunches=relaunches)
    marked: list[dict] = []

    async def load(_kind, _job_id):
        return row

    async def mark(_kind, _job_id, **changes):
        marked.append(changes)

    monkeypatch.setattr(jobs, "load", load)
    monkeypatch.setattr(jobs, "mark", mark)
    asyncio.run(runners.dead_letter("render", row.id, "Execution timed out"))
    error = marked[0]["error"]
    assert error.startswith("Execution timed out It was at 73% (Drawing).")
    assert "relaunched 3 times" in error and "13 min" in error
    # Any other end of a render says nothing of it.
    marked.clear()
    asyncio.run(runners.dead_letter("render", row.id, "The render process crashed."))
    assert "relaunched" not in marked[0]["error"]


async def test_record_relaunch_keeps_what_the_stop_cost(monkeypatch):
    row = SimpleNamespace(id=uuid.uuid4(), status=JOB_RUNNING, progress=40, relaunches=None)

    async def load(_kind, _job_id):
        return row

    async def mark_if_open(_kind, _job_id, **changes):
        for name, value in changes.items():
            setattr(row, name, value)
        return True

    monkeypatch.setattr(jobs, "load", load)
    monkeypatch.setattr(jobs, "mark_if_open", mark_if_open)
    entry = await render_process.record_relaunch(
        str(row.id), resumed="after a deploy", attempt=(2, 3), frames_kept=0, lost_seconds=104, given_back_seconds=104
    )
    assert entry["lost_seconds"] == 104 and entry["given_back_seconds"] == 104
    entry = await render_process.record_relaunch(str(row.id), resumed="after a deploy", attempt=(3, 3), frames_kept=0)
    assert "lost_seconds" not in entry


async def test_a_steady_render_moves_its_deadline_to_its_pace(monkeypatch):
    from app.services.video_studio import render

    moved: list[tuple[float, float]] = []

    async def extend(seconds_left, *, most_seconds):
        moved.append((seconds_left, most_seconds))

    async def mark(*_args, **_kwargs):
        return None

    monkeypatch.setattr(render_ranges, "extend_to_eta", extend)
    monkeypatch.setattr(jobs, "mark", mark)
    from app.services.video_studio.render_pace import Pace

    pace = Pace(drawn=900, frames=9061, per_second=3.0, seconds_left=1020.0)
    tracker = SimpleNamespace(pace=lambda: pace)
    note = render.PaceNote("r", {"check_seconds": 120}, most_seconds=14400.0)
    await note.note(tracker)
    await note.note(tracker)  # within the period: once
    assert moved == [(1140.0, 14400.0)]
    # No measured pace yet, or a scene preview (no cap): never moved.
    moved.clear()
    await render.PaceNote("r", {}, most_seconds=14400.0).note(SimpleNamespace(pace=lambda: None))
    await render.PaceNote("r", {}).note(tracker)
    assert moved == []


async def test_extend_to_eta_needs_its_own_execution(monkeypatch):
    monkeypatch.setattr(render_ranges, "own_execution", lambda: (None, None))
    assert await render_ranges.extend_to_eta(600.0, most_seconds=3600.0) is None
