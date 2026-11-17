"""Who an assignment run's end is owed to, against PostgreSQL.

A chat turn that triggers a run owes it to its conversation; the conversation
is told once how it ended:

* ``trigger_assignment(owed_conversation_id=...)`` writes the notice in the
  run's own transaction, and nothing for a trigger with no conversation;
* the wake claims an ended run's notice once, however many wakes race for it,
  never while the run is active, and resumes the conversation with the run's
  id, assignment, status, duration and result;
* a wait or a ``get_run`` of the run's own conversation tells it first, one
  of another conversation does not; a cancelled run is claimed and not told;
* the sweep resumes a conversation whose wake was lost, and ages notices out.

``integration``-marked and skips when PostgreSQL is unreachable; the
temporary database is the shared ``tests/scratch_database.py``.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, update

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.models.agent import Agent
from app.models.assignment import Assignment, AssignmentRun, AssignmentRunConversationNotice
from app.models.user import User
from app.services import assignment_engine, parent_wake
from app.services import assignment_run_wake as wake
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = [pytest.mark.integration]

_STARTED = datetime(2026, 10, 20, 19, 53, tzinfo=UTC)
_ENDED = _STARTED + timedelta(seconds=246)


def _database(prefix: str):
    return scratch_database(prefix, tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True)


async def _seed(db) -> tuple[User, User, Agent, Assignment]:
    owner = User(email=f"owner-{uuid.uuid4().hex[:8]}@filigran.io", hashed_password="x", full_name="Owner", role="user")
    stranger = User(
        email=f"stranger-{uuid.uuid4().hex[:8]}@filigran.io", hashed_password="x", full_name="Stranger", role="user"
    )
    db.add_all([owner, stranger])
    await db.flush()
    agent = Agent(user_id=owner.id, name="Recap agent", slug=f"recap-{uuid.uuid4().hex[:8]}")
    db.add(agent)
    await db.flush()
    assignment = Assignment(
        agent_id=agent.id, user_id=owner.id, name="Eng recap", trigger_type="schedule", prompt="Sleep 60 s"
    )
    db.add(assignment)
    await db.flush()
    return owner, stranger, agent, assignment


def _run(assignment: Assignment, user: User, status: str, **extra) -> AssignmentRun:
    return AssignmentRun(
        assignment_id=assignment.id,
        agent_id=assignment.agent_id,
        user_id=user.id,
        is_company_managed=extra.pop("is_company_managed", False),
        status=status,
        started_at=_STARTED,
        **extra,
    )


@pytest.fixture
def told(monkeypatch) -> list[dict]:
    calls: list[dict] = []

    async def _record(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(parent_wake, "wake_conversation", _record)
    return calls


async def test_a_chat_triggered_run_is_owed_to_its_conversation(monkeypatch) -> None:
    monkeypatch.setattr(assignment_engine, "_refuse_if_ai_disabled", AsyncMock())
    monkeypatch.setattr(assignment_engine, "_wake_reconciler", AsyncMock())
    async with _database("run_wake_owed") as scratch:
        conversation = uuid.uuid4()
        async with scratch.sessions() as db:
            owner, _stranger, agent, assignment = await _seed(db)
            await db.commit()
            ids = (owner.id, agent.id, assignment.id)

        async with scratch.sessions() as db:
            owner_id, agent_id, assignment_id = ids
            assignment = await db.get(Assignment, assignment_id)
            agent = await db.get(Agent, agent_id)
            owed = await assignment_engine.trigger_assignment(
                assignment, agent, db=db, triggered_by_user_id=owner_id, owed_conversation_id=conversation
            )
            await assignment_engine.trigger_assignment(assignment, agent, db=db, triggered_by_user_id=owner_id)

        async with scratch.sessions() as db:
            notices = (await db.execute(select(AssignmentRunConversationNotice))).scalars().all()
        assert [(str(n.run_id), n.conversation_id, n.user_id, n.told_at) for n in notices] == [
            (owed, conversation, owner_id, None)
        ]


async def test_the_end_of_a_run_is_told_once_to_its_own_conversation(told) -> None:
    async with _database("run_wake_once") as scratch:
        conversation, elsewhere = uuid.uuid4(), uuid.uuid4()
        async with scratch.sessions() as db:
            owner, _stranger, _agent, assignment = await _seed(db)
            run = _run(assignment, owner, "running")
            db.add(run)
            await db.flush()
            db.add(AssignmentRunConversationNotice(run_id=run.id, conversation_id=conversation, user_id=owner.id))
            await db.commit()
            run_id, owner_id = run.id, owner.id

        assert await wake.wake_owed_conversation(run_id) is False, "an active run is nobody's news yet"

        async with scratch.sessions() as db:
            await db.execute(
                update(AssignmentRun)
                .where(AssignmentRun.id == run_id)
                .values(status="completed", completed_at=_ENDED, result_summary="Email sent to Sam.")
            )
            await db.commit()
        await wake.mark_told([str(run_id)], elsewhere)

        first, second = await asyncio.gather(
            wake.wake_owed_conversation(run_id), wake.wake_owed_conversation(run_id)
        )
        assert sorted([first, second]) == [False, True]
        assert len(told) == 1
        call = told[0]
        assert (call["conversation_id"], call["user_id"], call["label"]) == (
            str(conversation),
            str(owner_id),
            wake.WAKE_LABEL,
        )
        assert call["delegated_at"] is not None
        text = call["wake_text"]
        assert text.startswith(
            f"[Platform] Assignment run {run_id} (Eng recap), which you triggered in this conversation, "
            "completed after 4m 6s."
        )
        assert "Result summary:\n\nEmail sent to Sam." in text


async def test_a_conversation_already_told_is_not_told_again(told) -> None:
    async with _database("run_wake_told") as scratch:
        conversation = uuid.uuid4()
        async with scratch.sessions() as db:
            owner, _stranger, _agent, assignment = await _seed(db)
            read = _run(assignment, owner, "failed", completed_at=_ENDED, error="Gmail refused the send.")
            cancelled = _run(assignment, owner, "cancelled", completed_at=_ENDED)
            db.add_all([read, cancelled])
            await db.flush()
            for run in (read, cancelled):
                db.add(AssignmentRunConversationNotice(run_id=run.id, conversation_id=conversation, user_id=owner.id))
            await db.commit()
            read_id, cancelled_id = read.id, cancelled.id

        # A wait or a get_run in that conversation read how it ended.
        await wake.mark_told([str(read_id)], str(conversation))
        assert await wake.wake_owed_conversation(read_id) is False
        # Nobody's news: claimed, never told.
        assert await wake.wake_owed_conversation(cancelled_id) is False
        assert told == []

        async with scratch.sessions() as db:
            untold = (
                await db.execute(
                    select(AssignmentRunConversationNotice).where(AssignmentRunConversationNotice.told_at.is_(None))
                )
            ).scalars().all()
        assert untold == []


async def test_the_sweep_resumes_a_lost_wake_and_ages_notices_out(told) -> None:
    async with _database("run_wake_sweep") as scratch:
        now = datetime.now(UTC)
        conversation = uuid.uuid4()
        async with scratch.sessions() as db:
            owner, _stranger, _agent, assignment = await _seed(db)
            lost = _run(assignment, owner, "completed", completed_at=now - timedelta(minutes=5), result_summary="Done")
            fresh = _run(assignment, owner, "completed", completed_at=now - timedelta(seconds=30))
            told_long_ago = _run(assignment, owner, "completed", completed_at=now - timedelta(days=2))
            forgotten = _run(assignment, owner, "running")
            db.add_all([lost, fresh, told_long_ago, forgotten])
            await db.flush()
            db.add_all(
                [
                    AssignmentRunConversationNotice(run_id=lost.id, conversation_id=conversation, user_id=owner.id),
                    AssignmentRunConversationNotice(run_id=fresh.id, conversation_id=conversation, user_id=owner.id),
                    AssignmentRunConversationNotice(
                        run_id=told_long_ago.id,
                        conversation_id=conversation,
                        user_id=owner.id,
                        told_at=now - timedelta(days=2),
                    ),
                    AssignmentRunConversationNotice(
                        run_id=forgotten.id,
                        conversation_id=conversation,
                        user_id=owner.id,
                        created_at=now - timedelta(days=31),
                    ),
                ]
            )
            await db.commit()
            lost_id, fresh_id = lost.id, fresh.id

        assert await wake.sweep_lost_wakes(now) == 1
        await asyncio.gather(*list(wake._WAKES))
        assert [call["wake_text"].split(" (")[0] for call in told] == [f"[Platform] Assignment run {lost_id}"]

        async with scratch.sessions() as db:
            left = {
                n.run_id: n.told_at is not None
                for n in (await db.execute(select(AssignmentRunConversationNotice))).scalars().all()
            }
        assert left == {lost_id: True, fresh_id: False}
