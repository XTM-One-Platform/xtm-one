"""Runs waiting for a worker say why, and the administrators are told once per starvation episode (#2040)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services import run_queue_starvation as starvation

NOW = datetime(2026, 11, 25, 6, 0, tzinfo=UTC)


class _Redis:
    """The few Redis calls the episode key needs, in memory."""

    def __init__(self):
        self.store: dict[str, str] = {}

    async def set(self, key, value, nx=False, ex=None):
        if nx and key in self.store:
            return None
        self.store[key] = value
        return True

    async def expire(self, key, ttl):
        return key in self.store

    async def delete(self, key):
        self.store.pop(key, None)


def _starving(monkeypatch, *, waiting=3, wait=1200.0, expired=0, admins=2):
    monkeypatch.setattr(starvation, "_starvation", AsyncMock(return_value=(waiting, wait, expired)))
    notify = AsyncMock(return_value=admins)
    monkeypatch.setattr(starvation, "_notify_admins", notify)
    db = MagicMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    return db, notify


@pytest.mark.asyncio
async def test_a_wait_over_the_threshold_alerts_exactly_once_per_episode(monkeypatch):
    redis = _Redis()
    db, notify = _starving(monkeypatch)
    assert await starvation.check_queue_starvation(db, now=NOW, redis=redis) == 2
    for minute in range(1, 6):  # the episode goes on: the next ticks send nothing
        assert await starvation.check_queue_starvation(db, now=NOW + timedelta(minutes=minute), redis=redis) == 0
    assert notify.await_count == 1
    summary = notify.await_args.kwargs["summary"]
    assert "3 due assignment run(s) wait for a free worker, the oldest for 1200s" in summary
    # The queue drains: the episode ends, and the next one alerts again.
    monkeypatch.setattr(starvation, "_starvation", AsyncMock(return_value=(0, 0.0, 0)))
    assert await starvation.check_queue_starvation(db, now=NOW + timedelta(hours=1), redis=redis) == 0
    assert starvation.EPISODE_KEY not in redis.store
    monkeypatch.setattr(starvation, "_starvation", AsyncMock(return_value=(1, 1000.0, 0)))
    assert await starvation.check_queue_starvation(db, now=NOW + timedelta(hours=2), redis=redis) == 2
    assert notify.await_count == 2


@pytest.mark.asyncio
async def test_a_run_expired_before_start_opens_an_episode_too(monkeypatch):
    redis = _Redis()
    db, notify = _starving(monkeypatch, waiting=1, wait=30.0, expired=1)
    assert await starvation.check_queue_starvation(db, now=NOW, redis=redis) == 2
    assert "1 run(s) expired before any worker started them" in notify.await_args.kwargs["summary"]


@pytest.mark.asyncio
async def test_a_short_wait_sends_nothing(monkeypatch):
    redis = _Redis()
    db, notify = _starving(monkeypatch, wait=starvation.STARVATION_ALERT_SECONDS - 1)
    assert await starvation.check_queue_starvation(db, now=NOW, redis=redis) == 0
    notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_check_never_breaks_the_tick(monkeypatch):
    monkeypatch.setattr(starvation, "_starvation", AsyncMock(side_effect=RuntimeError("db blip")))
    db = MagicMock()
    db.rollback = AsyncMock()
    assert await starvation.check_queue_starvation(db, now=NOW, redis=_Redis()) == 0


def test_a_waiting_run_says_why_and_where_it_is_in_the_queue():
    line = starvation.describe_wait(run_at=NOW - timedelta(minutes=50), now=NOW, ahead=13)
    assert line == "Waiting: no free worker for 3000s; position 14 in the assignment queue (13 due run(s) ahead)"
    later = starvation.describe_wait(run_at=NOW + timedelta(minutes=5), now=NOW, ahead=0)
    assert later == f"Waiting: not due yet, it starts at {(NOW + timedelta(minutes=5)).isoformat()}"


def _scalar(value):
    res = MagicMock()
    res.scalar_one_or_none.return_value = value
    res.scalar_one.return_value = value
    return res


@pytest.mark.asyncio
async def test_list_runs_reads_the_wait_reason_of_a_pending_run():
    execution = MagicMock(run_at=NOW - timedelta(minutes=10))
    db = MagicMock()
    db.execute = AsyncMock(side_effect=[_scalar(execution), _scalar(4)])
    line = await starvation.queue_wait_line(db, uuid.uuid4(), now=NOW)
    assert line == "Waiting: no free worker for 600s; position 5 in the assignment queue (4 due run(s) ahead)"
    started = MagicMock()
    started.execute = AsyncMock(return_value=_scalar(None))
    assert await starvation.queue_wait_line(started, uuid.uuid4(), now=NOW) is None


def test_list_runs_and_get_run_carry_the_wait_line():
    import inspect

    from app.services.builtin_tools import agent_builder

    source = inspect.getsource(agent_builder)
    assert source.count("await queue_wait_line(_db,") == 2
