"""A render execution left ``running`` with no lease is taken back, never orphaned (#9404). Synthetic rows only.

* the engine's ``reclaim_leaseless`` reads only running, lease-less, quiet rows of the adapters named, and takes
  each back: pending first in its lane with attempts left, ``dead_letter`` with none, the harness reason said;
* ``reclaim_leaseless_renders`` runs the render adapter's dead-letter on the exhausted ones and wakes the workers;
* ``get_video_jobs`` states ``stalled: no lease since <time>`` for such a job.
"""

from __future__ import annotations

import contextlib
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import orchestration_engine as engine
from app.services.video_studio import jobs


def _execution(now, **fields):
    values = {
        "id": uuid.uuid4(),
        "adapter": jobs.RENDER_ADAPTER,
        "state": "running",
        "lease_owner": None,
        "lease_expires_at": None,
        "updated_at": now - timedelta(minutes=12),
        "attempt_count": 3,
        "max_attempts": 3,
        "payload": {"render_id": str(uuid.uuid4())},
        "adapter_state": None,
        "run_at": now,
        "last_error": None,
    }
    values.update(fields)
    return SimpleNamespace(**values)


class _Rows:
    def __init__(self, rows):
        self.rows = rows
        self.statements: list = []

    async def execute(self, statement, *_a, **_k):
        self.statements.append(statement)
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: list(self.rows)))

    async def flush(self):
        return None

    async def commit(self):
        return None


async def test_an_exhausted_leaseless_render_is_dead_lettered_with_the_harness_reason():
    now = datetime.now(UTC)
    row = _execution(now)
    taken = await engine.reclaim_leaseless(
        _Rows([row]), adapters=jobs.RENDER_ADAPTERS, quiet_before=now - timedelta(minutes=3), limit=10
    )
    assert taken == [row]
    assert row.state == "dead_letter"
    assert row.last_error.startswith(engine.NO_LEASE_WHY) and "no attempt left" in row.last_error
    expiry = row.payload[engine.LEASE_EXPIRY_PAYLOAD_KEY]
    assert expiry["silent_seconds"] >= 12 * 60 - 1
    assert engine.lease_expiry(row) is not None


async def test_a_leaseless_render_with_attempts_left_goes_back_first_in_its_lane(monkeypatch):
    now = datetime.now(UTC)
    row = _execution(now, attempt_count=1)
    head = now - timedelta(hours=1)
    monkeypatch.setattr(engine, "head_of_queue", AsyncMock(return_value=head))
    await engine.reclaim_leaseless(
        _Rows([row]), adapters=jobs.RENDER_ADAPTERS, quiet_before=now - timedelta(minutes=3), limit=10
    )
    assert row.state == "pending" and row.run_at == head
    assert row.lease_owner is None and row.lease_expires_at is None


async def test_the_query_reads_only_running_leaseless_quiet_rows_of_the_adapters_named():
    from sqlalchemy.dialects import postgresql

    db = _Rows([])
    now = datetime.now(UTC)
    assert await engine.reclaim_leaseless(db, adapters=["video_render"], quiet_before=now, limit=5) == []
    sql = str(db.statements[0].compile(dialect=postgresql.dialect()))
    where = sql.split("WHERE", 1)[1]
    assert "orchestration_executions.lease_expires_at IS NULL" in where
    assert "orchestration_executions.updated_at <" in where
    assert "orchestration_executions.state =" in where
    assert "FOR UPDATE SKIP LOCKED" in sql


async def test_no_adapter_named_reads_nothing():
    db = _Rows([])
    assert await engine.reclaim_leaseless(db, adapters=[], quiet_before=datetime.now(UTC), limit=5) == []
    assert db.statements == []


async def test_the_sweep_dead_letters_the_exhausted_and_wakes_the_workers(monkeypatch):
    now = datetime.now(UTC)
    dead = _execution(now, state="dead_letter")
    back = _execution(now, state="pending")

    @contextlib.asynccontextmanager
    async def _session():
        yield _Rows([])

    monkeypatch.setattr(jobs, "async_session", _session)
    monkeypatch.setattr(engine, "reclaim_leaseless", AsyncMock(return_value=[dead, back]))
    ran = AsyncMock(return_value=None)
    from app.services import orchestration_reconciler

    monkeypatch.setattr(orchestration_reconciler, "run_dead_letter", ran)
    woke = AsyncMock(return_value=None)
    monkeypatch.setattr(jobs, "wake", woke)
    assert await jobs.reclaim_leaseless_renders(now=now) == 2
    assert ran.await_count == 1 and ran.await_args.args[0] is dead
    assert woke.await_count == 1
    kwargs = engine.reclaim_leaseless.await_args.kwargs
    assert kwargs["quiet_before"] == now - jobs.LEASELESS_STALL
    assert tuple(kwargs["adapters"]) == jobs.RENDER_ADAPTERS


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({}, True),
        ({"lease_expires_at": datetime.now(UTC)}, False),
        ({"state": "pending"}, False),
        ({"adapter": "assignment"}, False),
    ],
)
def test_no_lease_since_is_said_only_for_a_running_render_without_a_lease(fields, expected):
    now = datetime.now(UTC)
    assert (jobs.no_lease_since(_execution(now, **fields)) is not None) is expected


def test_get_video_jobs_states_the_stall():
    now = datetime.now(UTC)
    since = now - timedelta(minutes=12)
    state = jobs.JobState(
        kind="render",
        id="r",
        status=jobs.STATE_RUNNING,
        percent=94,
        stage="Watching",
        last_progress_at=since,
        no_lease_since=since,
    )
    assert state.as_dict(now)["stalled"] == f"no lease since {since.isoformat()}"
    clause = jobs.lease_clause(state, now)
    assert clause.startswith(f"; stalled: no lease since {since.isoformat()}")


def test_the_stall_is_one_render_lease_period():
    assert timedelta(seconds=jobs.RENDER_LEASE_SECONDS) == jobs.LEASELESS_STALL
