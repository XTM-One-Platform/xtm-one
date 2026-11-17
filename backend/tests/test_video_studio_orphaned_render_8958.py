"""A render row nothing runs any more is failed with its reason (#8958, slice 3). Synthetic rows only.

* a render still ``running`` whose execution ended, is gone, or kept ``running`` with a lease no worker renewed,
  quiet for :data:`jobs.ORPHAN_STALL`, is failed with where it stood and why, so ``render_video`` on its revision
  starts a fresh render instead of answering it as in flight (the 5 Nov 2026 case: 94 %, no progress for 70 min);
* the reason names the progress, the stage and what became of the execution;
* a running execution's stale lease is cancelled with it; an ended execution is left as it is.
"""

from __future__ import annotations

import contextlib
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.services import orchestration_engine as engine
from app.services.video_studio import jobs


@contextlib.asynccontextmanager
async def _session(db):
    yield db


class _Session:
    def __init__(self, rows, rowcount=1):
        self.rows = rows
        self.rowcount = rowcount
        self.writes: list = []
        self.committed = False

    async def execute(self, statement, *_args, **_kwargs):
        from sqlalchemy.sql.dml import Update

        if isinstance(statement, Update):
            self.writes.append(statement)
            return SimpleNamespace(rowcount=self.rowcount)
        return SimpleNamespace(all=lambda: list(self.rows))

    async def commit(self):
        self.committed = True


def _render(now, **fields):
    values = {
        "id": uuid.uuid4(),
        "progress": 94,
        "stage": "Watching the render for frozen pictures: 226 of 226 s, scene outro",
        "progress_at": now - timedelta(minutes=70),
        "updated_at": now - timedelta(minutes=70),
    }
    values.update(fields)
    return SimpleNamespace(**values)


def test_the_reason_says_where_it_stood_and_what_became_of_its_job():
    now = datetime.now(UTC)
    row = _render(now)
    stale = SimpleNamespace(state="running")
    said = jobs.orphan_reason(row, stale, timedelta(minutes=70))
    assert said.startswith("Stalled: no progress for 70 min at 94% (Watching the render")
    assert "no worker holds its lease any more" in said and "Render this revision again" in said
    assert "its job is gone" in jobs.orphan_reason(row, None, timedelta(minutes=16))
    assert "its job ended (dead_letter)" in jobs.orphan_reason(row, SimpleNamespace(state="dead_letter"), timedelta(minutes=16))
    assert "()" not in jobs.orphan_reason(_render(now, stage=None), None, timedelta(minutes=16))


async def test_a_stalled_render_with_a_stale_lease_is_failed_and_its_execution_cancelled(monkeypatch):
    now = datetime.now(UTC)
    execution = SimpleNamespace(id=uuid.uuid4(), state="running", lease_expires_at=now - timedelta(minutes=60))
    db = _Session([(_render(now), execution)])
    cancelled = AsyncMock(return_value=None)
    monkeypatch.setattr(engine, "cancel_execution", cancelled)
    monkeypatch.setattr(jobs, "async_session", lambda: _session(db))
    assert await jobs.fail_orphaned_renders(now=now) == 1
    [write] = db.writes
    assert "video_renders" in str(write) and db.committed
    assert cancelled.await_count == 1


async def test_a_render_whose_job_ended_is_failed_and_the_ended_job_left_alone(monkeypatch):
    now = datetime.now(UTC)
    db = _Session([(_render(now), SimpleNamespace(id=uuid.uuid4(), state="failed")), (_render(now), None)])
    cancelled = AsyncMock(return_value=None)
    monkeypatch.setattr(engine, "cancel_execution", cancelled)
    monkeypatch.setattr(jobs, "async_session", lambda: _session(db))
    assert await jobs.fail_orphaned_renders(now=now) == 2
    assert cancelled.await_count == 0 and len(db.writes) == 2


async def test_a_render_its_worker_ended_meanwhile_is_not_counted(monkeypatch):
    now = datetime.now(UTC)
    db = _Session([(_render(now), None)], rowcount=0)
    monkeypatch.setattr(engine, "cancel_execution", AsyncMock(return_value=None))
    monkeypatch.setattr(jobs, "async_session", lambda: _session(db))
    assert await jobs.fail_orphaned_renders(now=now) == 0


def test_the_query_reads_only_open_quiet_rows_whose_job_runs_no_more():
    # The where clause names the stall, the open statuses and the three orphan cases, never a handed-back render.
    from sqlalchemy.dialects import postgresql

    captured: dict = {}

    class _Capture(_Session):
        async def execute(self, statement, *_a, **_k):
            captured["sql"] = str(statement.compile(dialect=postgresql.dialect()))
            return SimpleNamespace(all=lambda: [])

    import asyncio

    db = _Capture([])
    original = jobs.async_session
    jobs.async_session = lambda: _session(db)
    try:
        assert asyncio.run(jobs.fail_orphaned_renders(now=datetime.now(UTC))) == 0
    finally:
        jobs.async_session = original
    sql = captured["sql"]
    where = sql.split("WHERE", 1)[1]
    assert "LEFT OUTER JOIN orchestration_executions" in sql
    assert "coalesce(video_renders.progress_at, video_renders.updated_at) <" in where
    assert "orchestration_executions.lease_expires_at" in where
    # A handed-back render's execution is pending: never one of the states read here.
    assert "adapter_state" not in where and "pending" not in where


def test_the_stall_is_fifteen_minutes():
    stall = timedelta(minutes=15)
    assert stall == jobs.ORPHAN_STALL
