# SPDX-License-Identifier: Apache-2.0

"""A CTEM platform-event backlog past the queue limit is trimmed, against real PostgreSQL (#2570).

A platform restarted in a case-opened storm held 541 queued "Supervise CTEM loop" deliveries
far past the queue limit of 20. The first reconciler cycle keeps the oldest up to the limit and
finalises the rest ``skipped`` with one reason; a webhook delivery of the same assignment is left.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

import app.models  # noqa: F401 - registers every mapper on Base.metadata
from app.core.config import settings
from app.models.agent import Agent
from app.models.assignment import Assignment, AssignmentRun
from app.models.user import User
from app.services import assignment_engine as ae
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration

_T0 = datetime(2026, 11, 27, 6, 0, tzinfo=UTC)


async def test_a_storm_backlog_is_trimmed_to_the_queue_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "assignment_queue_max_depth", 20)
    async with scratch_database("qtrim", tables=ALL_TABLES, extensions=("vector",)) as db:
        async with db.sessions() as session:
            user = User(
                id=uuid.uuid4(), email=f"trim-{uuid.uuid4().hex[:8]}@test.local", full_name="T", hashed_password="x"
            )
            session.add(user)
            await session.flush()
            agent = Agent(id=uuid.uuid4(), name="CTEM Orchestrator", user_id=user.id)
            session.add(agent)
            await session.flush()
            assignment = Assignment(
                id=uuid.uuid4(),
                agent_id=agent.id,
                user_id=user.id,
                name="Supervise CTEM loop",
                trigger_type="platform_event",
                prompt="p",
                trigger_config={},
            )
            session.add(assignment)
            await session.flush()
            webhook = AssignmentRun(
                assignment_id=assignment.id,
                agent_id=agent.id,
                user_id=user.id,
                status=ae.QUEUED_RUN_STATUS,
                trigger_context={"type": "webhook"},
                created_at=_T0,
            )
            session.add(webhook)
            for index in range(550):
                session.add(
                    AssignmentRun(
                        assignment_id=assignment.id,
                        agent_id=agent.id,
                        user_id=user.id,
                        status=ae.QUEUED_RUN_STATUS,
                        trigger_context={"type": "platform_event", "event": "ctem_case.opened"},
                        created_at=_T0 + timedelta(seconds=index + 1),
                    )
                )
            await session.commit()
            assignment_id = assignment.id
            webhook_id = webhook.id

        async with db.sessions() as session:
            trimmed = await ae.trim_platform_event_queues(session)
            await session.commit()
        assert trimmed == {assignment_id: 531}

        async with db.sessions() as session:
            queued = (
                await session.execute(
                    select(func.count())
                    .select_from(AssignmentRun)
                    .where(AssignmentRun.assignment_id == assignment_id, AssignmentRun.status == ae.QUEUED_RUN_STATUS)
                )
            ).scalar()
            assert queued == 20
            kept = (await session.execute(select(AssignmentRun).where(AssignmentRun.id == webhook_id))).scalar_one()
            assert kept.status == ae.QUEUED_RUN_STATUS
            skipped = (
                (
                    await session.execute(
                        select(AssignmentRun.error).where(
                            AssignmentRun.assignment_id == assignment_id, AssignmentRun.status == "skipped"
                        )
                    )
                )
                .scalars()
                .first()
            )
            assert skipped is not None and ae.PLATFORM_EVENT_QUEUE_FULL_REASON in skipped

        # A second cycle has nothing over the limit.
        async with db.sessions() as session:
            assert await ae.trim_platform_event_queues(session) == {}
