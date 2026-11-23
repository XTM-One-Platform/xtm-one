"""One user's failed fan-out trigger costs that user's run only, against PostgreSQL.

The fan-out rolls back after a per-user failure, and a rollback expires every
instance of the session: only a real AsyncSession shows the later users, and
the assignments after it in the same tick, then reading expired attributes.
"""

from __future__ import annotations

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


class _Redis:
    """Enough of Redis for the fan-out claim."""

    def __init__(self):
        self.data: dict[str, str] = {}

    async def set(self, key, value, ex=None, nx=False):
        if nx and key in self.data:
            return None
        self.data[key] = str(value)
        return True

    async def eval(self, script, _numkeys, key, token, *_args):
        if self.data.get(key) != token:
            return 0
        if "EXPIRE" not in script:
            del self.data[key]
        return 1


@asynccontextmanager
async def _temp_database() -> AsyncIterator[async_sessionmaker]:
    async with scratch_database(
        "rollbackexpiry",
        tables=ALL_TABLES,
        extensions=("vector",),
        bind_app_sessions=False,
    ) as db:
        yield db.sessions


async def _seed(sessionmaker: async_sessionmaker) -> tuple[list[uuid.UUID], list[uuid.UUID]]:
    """Two due fan-out assignments of one company agent; their ids and the user ids."""
    async with sessionmaker() as db:
        users = [
            User(
                email=f"user{i}-{uuid.uuid4().hex[:8]}@example.com",
                hashed_password="x",
                full_name=f"user{i}",
                role="admin" if i == 0 else "user",
            )
            for i in range(USERS)
        ]
        db.add_all(users)
        await db.flush()
        agent = Agent(
            name="Company agent",
            slug=f"company-{uuid.uuid4().hex[:8]}",
            persona="p",
            user_id=users[0].id,
            is_company_managed=True,
        )
        db.add(agent)
        await db.flush()
        assignments = [
            Assignment(
                agent_id=agent.id,
                user_id=users[0].id,
                is_company_managed=True,
                run_as_all_users=True,
                name=f"Weekly assessment {i}",
                trigger_type="schedule",
                trigger_config={"cron": "0 8 * * 1"},
                last_triggered_at=datetime.now(UTC) - timedelta(days=7, minutes=1),
                prompt="Do the thing.",
                enabled=True,
            )
            for i in range(2)
        ]
        db.add_all(assignments)
        await db.flush()
        seeded = [a.id for a in assignments], [u.id for u in users]
        await db.commit()
        return seeded


async def test_a_failed_user_costs_only_their_own_run(monkeypatch):
    from app.services import autonomous

    redis = _Redis()

    async def _client():
        return redis

    monkeypatch.setattr("app.core.redis.redis_client", _client)
    stub_agentic_gate(monkeypatch)

    async with _temp_database() as sessionmaker:
        assignment_ids, user_ids = await _seed(sessionmaker)
        failing_user = user_ids[1]

        async def _trigger(*, assignment, agent, db, triggered_by_user_id, trigger_context, **_kwargs):
            if triggered_by_user_id == failing_user:
                # As the real trigger does, it reads before it fails: the
                # rollback then ends an open transaction and expires the session.
                await db.execute(select(User.id).limit(1))
                raise RuntimeError("transient per-user failure")
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

        monkeypatch.setattr("app.services.assignment_engine.trigger_assignment", _trigger)

        async with sessionmaker() as db:
            await autonomous._process_scheduled_assignments(db)

        expected = Counter({uid: 1 for uid in user_ids if uid != failing_user})
        async with sessionmaker() as db:
            for assignment_id in assignment_ids:
                runs = Counter(
                    (
                        await db.execute(
                            select(AssignmentRun.user_id).where(AssignmentRun.assignment_id == assignment_id)
                        )
                    ).scalars()
                )
                assert runs == expected, assignment_id
                # Served: the window is stamped, the failed user waits for the next one.
                stamped = (await db.get(Assignment, assignment_id)).last_triggered_at
                assert stamped > datetime.now(UTC) - timedelta(minutes=5)
