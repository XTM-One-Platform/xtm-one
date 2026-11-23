"""Two scheduler ticks never fan out the same assignment at once (#1470), against
PostgreSQL.

The window lock of the tick expires after 55 s and ``last_triggered_at`` is
stamped only once a fan-out returns, so a second replica's tick can reach an
assignment the first one is still fanning out. Only a real database shows what
its per-user dedup sees mid fan-out: the runs committed so far, not the rest.
Redis is an in-memory double shared by both ticks, as two pods share one.
"""

from __future__ import annotations

import asyncio
import uuid
from collections import Counter
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.models  # noqa: F401 - registers every mapper on Base.metadata
from app.models.agent import Agent
from app.models.assignment import Assignment, AssignmentRun
from app.models.user import User
from tests.agentic_gate_stub import stub_agentic_gate
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration

USERS = 4


class _SharedRedis:
    """``SET NX EX``, ``GET`` and the claim's compare-and-delete / compare-and-expire ``EVAL``.

    ``ttl`` records each key's last expiry and ``renewals`` each successful renew.
    """

    def __init__(self):
        self.data: dict[str, str] = {}
        self.ttl: dict[str, int | None] = {}
        self.renewals = 0

    async def set(self, key, value, ex=None, nx=False):
        if nx and key in self.data:
            return None
        self.data[key] = str(value)
        self.ttl[key] = ex
        return True

    async def get(self, key):
        return self.data.get(key)

    async def delete(self, *keys):
        return sum(self.data.pop(key, None) is not None for key in keys)

    async def eval(self, script, numkeys, *keys_and_args):
        assert numkeys == 1
        key, token, *ttl = keys_and_args
        if self.data.get(key) != token:
            return 0
        if "EXPIRE" in script:
            self.ttl[key] = int(ttl[0])
            self.renewals += 1
        else:
            del self.data[key]
        return 1


@asynccontextmanager
async def _temp_database() -> AsyncIterator[async_sessionmaker]:
    async with scratch_database(
        "fanoutclaim",
        tables=ALL_TABLES,
        extensions=("vector",),
        bind_app_sessions=False,
    ) as db:
        yield db.sessions


async def _seed(sessionmaker: async_sessionmaker) -> tuple[uuid.UUID, datetime]:
    """The due fan-out assignment: its id and the ``last_triggered_at`` it starts with."""
    async with sessionmaker() as db:
        admin = User(
            email=f"admin-{uuid.uuid4().hex[:8]}@example.com",
            hashed_password="x",
            full_name="admin",
            role="admin",
        )
        db.add(admin)
        db.add_all(
            User(
                email=f"user{i}-{uuid.uuid4().hex[:8]}@example.com",
                hashed_password="x",
                full_name=f"user{i}",
                role="user",
            )
            for i in range(USERS - 1)
        )
        await db.flush()
        agent = Agent(
            name="Company agent",
            slug=f"company-{uuid.uuid4().hex[:8]}",
            persona="p",
            user_id=admin.id,
            is_company_managed=True,
        )
        db.add(agent)
        await db.flush()
        assignment = Assignment(
            agent_id=agent.id,
            user_id=admin.id,
            is_company_managed=True,
            run_as_all_users=True,
            name="Weekly scoping assessment",
            trigger_type="schedule",
            trigger_config={"cron": "0 8 * * 1"},
            # A week ago: this Monday's 08:00 window is due.
            last_triggered_at=datetime.now(UTC) - timedelta(days=7, minutes=1),
            prompt="Do the thing.",
            enabled=True,
        )
        db.add(assignment)
        await db.flush()
        seeded = assignment.id, assignment.last_triggered_at
        await db.commit()
        return seeded


def _committing_trigger():
    """``trigger_assignment`` as far as the dedup sees it: one committed run per user."""

    async def _trigger(*, assignment, agent, db, triggered_by_user_id, trigger_context, **_kwargs):
        run = AssignmentRun(
            assignment_id=assignment.id,
            agent_id=agent.id,
            user_id=triggered_by_user_id,
            status="pending",
            trigger_context=trigger_context,
        )
        db.add(run)
        await db.commit()
        return str(run.id)

    return _trigger


async def _runs_per_user(sessionmaker, assignment_id) -> Counter:
    async with sessionmaker() as db:
        rows = (
            await db.execute(select(AssignmentRun.user_id).where(AssignmentRun.assignment_id == assignment_id))
        ).scalars()
        return Counter(rows)


@pytest.fixture
def shared_redis(monkeypatch) -> _SharedRedis:
    redis = _SharedRedis()

    async def _client():
        return redis

    monkeypatch.setattr("app.core.redis.redis_client", _client)
    stub_agentic_gate(monkeypatch)
    return redis


async def test_a_tick_overlapping_a_fan_out_in_progress_makes_no_second_run(monkeypatch, shared_redis):
    from app.services import autonomous

    async with _temp_database() as sessionmaker:
        assignment_id, seeded_at = await _seed(sessionmaker)
        commit_run = _committing_trigger()
        first_run_committed = asyncio.Event()
        second_tick_done = asyncio.Event()

        async with sessionmaker() as db_a, sessionmaker() as db_b:

            async def _trigger(**kwargs):
                run_id = await commit_run(**kwargs)
                # Tick A pauses after its first user, as pod A's did between
                # 08:00:55 and 08:02:05, while tick B runs from start to end.
                if kwargs["db"] is db_a and not first_run_committed.is_set():
                    first_run_committed.set()
                    await asyncio.wait_for(second_tick_done.wait(), 30)
                return run_id

            monkeypatch.setattr("app.services.assignment_engine.trigger_assignment", _trigger)

            tick_a = asyncio.create_task(autonomous._process_scheduled_assignments(db_a))
            await asyncio.wait_for(first_run_committed.wait(), 30)
            try:
                await autonomous._process_scheduled_assignments(db_b)
            finally:
                second_tick_done.set()
            await tick_a

        runs = await _runs_per_user(sessionmaker, assignment_id)
        assert len(runs) == USERS
        assert set(runs.values()) == {1}, runs
        async with sessionmaker() as db:
            stamped = (await db.get(Assignment, assignment_id)).last_triggered_at
        assert stamped > seeded_at
        # Released once done; a crashed holder's claim would expire on its own.
        assert shared_redis.data == {}
        assert all(ttl and ttl > 0 for ttl in shared_redis.ttl.values())


async def test_a_fan_out_that_raises_leaves_the_window_to_the_next_tick(monkeypatch, shared_redis):
    from app.services import autonomous

    async with _temp_database() as sessionmaker:
        assignment_id, seeded_at = await _seed(sessionmaker)
        monkeypatch.setattr("app.services.assignment_engine.trigger_assignment", _committing_trigger())
        real_fanout = autonomous._fanout_assignment_to_users
        calls = 0

        async def _fails_once(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("transient database failure")
            return await real_fanout(*args, **kwargs)

        monkeypatch.setattr(autonomous, "_fanout_assignment_to_users", _fails_once)

        async with sessionmaker() as db:
            await autonomous._process_scheduled_assignments(db)
        async with sessionmaker() as db:
            assert (await db.get(Assignment, assignment_id)).last_triggered_at == seeded_at
        assert shared_redis.data == {}

        async with sessionmaker() as db:
            await autonomous._process_scheduled_assignments(db)
        runs = await _runs_per_user(sessionmaker, assignment_id)
        assert len(runs) == USERS
        assert set(runs.values()) == {1}, runs


async def test_a_claim_held_elsewhere_leaves_the_window_open_until_it_lapses(monkeypatch, shared_redis):
    from app.services import autonomous

    async with _temp_database() as sessionmaker:
        assignment_id, seeded_at = await _seed(sessionmaker)
        monkeypatch.setattr("app.services.assignment_engine.trigger_assignment", _committing_trigger())
        # A claim left by a pod that crashed mid fan-out.
        key = f"copilot:scheduler_fanout_claim:{assignment_id}"
        shared_redis.data[key] = "crashed-pod"

        async with sessionmaker() as db:
            await autonomous._process_scheduled_assignments(db)
        assert await _runs_per_user(sessionmaker, assignment_id) == Counter()
        async with sessionmaker() as db:
            assert (await db.get(Assignment, assignment_id)).last_triggered_at == seeded_at
        assert shared_redis.data[key] == "crashed-pod"

        del shared_redis.data[key]  # its TTL lapsed
        async with sessionmaker() as db:
            await autonomous._process_scheduled_assignments(db)
        runs = await _runs_per_user(sessionmaker, assignment_id)
        assert len(runs) == USERS
        assert set(runs.values()) == {1}, runs


async def test_without_redis_the_fan_out_waits_for_a_later_tick(monkeypatch, shared_redis):
    from app.services import autonomous

    async def _refused(*_args, **_kwargs):
        raise ConnectionError("Redis unavailable")

    monkeypatch.setattr(shared_redis, "set", _refused)

    async with _temp_database() as sessionmaker:
        assignment_id, seeded_at = await _seed(sessionmaker)
        monkeypatch.setattr("app.services.assignment_engine.trigger_assignment", _committing_trigger())
        async with sessionmaker() as db:
            await autonomous._process_scheduled_assignments(db)
        assert await _runs_per_user(sessionmaker, assignment_id) == Counter()
        async with sessionmaker() as db:
            assert (await db.get(Assignment, assignment_id)).last_triggered_at == seeded_at


async def test_the_claim_is_renewed_while_the_fan_out_runs(monkeypatch, shared_redis):
    from app.services import autonomous

    monkeypatch.setattr(autonomous, "_FANOUT_CLAIM_RENEW_SECONDS", 0.01)
    assignment_id = uuid.uuid4()
    key = f"copilot:scheduler_fanout_claim:{assignment_id}"

    async with autonomous._fanout_claim(assignment_id) as claimed:
        assert claimed
        await asyncio.sleep(0.1)
        assert shared_redis.renewals >= 2
        assert shared_redis.ttl[key] == autonomous._FANOUT_CLAIM_TTL_SECONDS
    assert key not in shared_redis.data

    renewals = shared_redis.renewals
    await asyncio.sleep(0.05)
    assert shared_redis.renewals == renewals  # renewal stops with the claim
