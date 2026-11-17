"""A conversation Stop that follows a partner's recorded answer.

A stop stamps every OUTSTANDING delegation of a stopped conversation, so a
cancelled caller is never resumed. The other ordering had no fence: the answer
is recorded first (the row is concluded, nothing is left to stamp), the user
presses Stop, and the detached partner wake delivered the answer anyway. The
Stop's cancel cascade now records ``Conversation.stopped_at`` and the wake
compares it with the delegation's submission before delivering.

What a WHERE clause and a commit reach is decided by running them, so this runs
against PostgreSQL - same scratch-database recipe as
``test_a2a_client_owed_cancel_postgres.py``. The wake turn itself is stubbed:
``_run_turn_for`` records the conversations it would have delivered to.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.models  # noqa: F401 - registers every mapper on Base.metadata
from app.models.a2a import A2ADelegation
from app.models.chat import Conversation
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration


@asynccontextmanager
async def _temp_database() -> AsyncIterator[async_sessionmaker]:
    async with scratch_database(
        "a2astop",
        tables=ALL_TABLES,
        extensions=("vector",),
        bind_app_sessions=False,
    ) as db:
        yield db.sessions


async def _seed(session):
    """A user, an agent, a partner registration and a chat conversation."""
    from app.models.agent import Agent
    from app.models.integration import Integration
    from app.models.user import User

    user = User(
        id=uuid.uuid4(),
        email=f"a2a-{uuid.uuid4().hex[:8]}@test.local",
        full_name="A2A Stop Test",
        hashed_password="x",
    )
    session.add(user)
    await session.flush()
    agent = Agent(id=uuid.uuid4(), name="Caller", user_id=user.id)
    partner = Integration(
        id=uuid.uuid4(),
        provider="oauth_a2a",
        name=f"acme-{uuid.uuid4().hex[:6]}",
        user_id=user.id,
    )
    conversation = Conversation(id=uuid.uuid4(), user_id=user.id, title="Stopped")
    session.add_all([agent, partner, conversation])
    await session.flush()
    return user, agent, partner, conversation


async def _answered(session, *, user, agent, partner, conversation, submitted_at):
    """A chat delegation whose partner answer has just been recorded."""
    row = A2ADelegation(
        id=uuid.uuid4(),
        integration_id=partner.id,
        agent_id=agent.id,
        user_id=user.id,
        conversation_ref=conversation.id,
        partner_task_id=f"task-{uuid.uuid4().hex[:8]}",
        protocol_version="0.3",
        signature_state="unsigned",
        submitted_at=submitted_at,
        outcome="completed",
        terminal_at=datetime.now(UTC),
    )
    session.add(row)
    await session.flush()
    return row


async def _outstanding(session, *, user, agent, partner, conversation, submitted_at):
    """A chat delegation the partner is still working on (no target: a cancel
    concludes it locally, so no partner call is needed)."""
    row = A2ADelegation(
        id=uuid.uuid4(),
        integration_id=partner.id,
        agent_id=agent.id,
        user_id=user.id,
        conversation_ref=conversation.id,
        partner_task_id=f"task-{uuid.uuid4().hex[:8]}",
        protocol_version="0.3",
        signature_state="unsigned",
        submitted_at=submitted_at,
    )
    session.add(row)
    await session.flush()
    return row


async def _read_conversation(session, conversation_id):
    return (
        await session.execute(
            select(Conversation)
            .where(Conversation.id == conversation_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()


@pytest.fixture
def delivered(monkeypatch):
    """Stub the wake turn and the partner: record what would be delivered."""
    from app.a2a.client import delegation as delegation_mod
    from app.services import parent_wake

    calls: list = []

    async def _run_turn_for(_db, conv, **_kw):
        calls.append(conv.id)

    async def _cancel(*_a, **_k):
        raise AssertionError("a concluded delegation has no partner task to cancel")

    monkeypatch.setattr(parent_wake, "_run_turn_for", _run_turn_for)
    monkeypatch.setattr(delegation_mod, "cancel", _cancel)
    return calls


async def _wakes_settled():
    from app.services.parent_wake import _PARENT_WAKE_TASKS

    if _PARENT_WAKE_TASKS:
        await asyncio.gather(*list(_PARENT_WAKE_TASKS))


def _answer():
    return SimpleNamespace(
        succeeded=True, result_text="done", detail=None, outcome="completed"
    )


async def test_a_stop_after_the_answer_was_recorded_is_not_followed_by_it(
    delivered, monkeypatch
):
    """The race: the answer is recorded, the user presses Stop, and only then
    does the resume reach the wake. The stopped conversation is not woken - and
    the next piece of work the user starts is delivered as usual."""
    from app.a2a.client import callbacks
    from app.services import sub_agent

    async with _temp_database() as sessionmaker, sessionmaker() as session:
        monkeypatch.setattr("app.core.database.async_session", sessionmaker)
        user, agent, partner, conversation = await _seed(session)
        row = await _answered(
            session,
            user=user,
            agent=agent,
            partner=partner,
            conversation=conversation,
            submitted_at=datetime.now(UTC) - timedelta(minutes=5),
        )
        await session.commit()
        before = (await _read_conversation(session, conversation.id)).updated_at

        await sub_agent.cancel_tasks_for_conversation(None, str(conversation.id))
        stopped = await _read_conversation(session, conversation.id)
        assert stopped.stopped_at is not None, "the Stop left no durable record"
        assert stopped.updated_at == before, "a Stop reordered the conversation list"

        await callbacks._resume_caller(session, row, _answer(), partner_name="Acme")
        await _wakes_settled()
        assert delivered == [], "the stopped conversation was woken with the late answer"

        # A new message after the Stop delegates again: that work is not the
        # work the user stopped, and its answer is delivered normally.
        later = await _answered(
            session,
            user=user,
            agent=agent,
            partner=partner,
            conversation=conversation,
            submitted_at=datetime.now(UTC),
        )
        await session.commit()
        await callbacks._resume_caller(session, later, _answer(), partner_name="Acme")
        await _wakes_settled()
        assert delivered == [conversation.id]


async def test_a_conversation_never_stopped_is_woken_as_before(delivered, monkeypatch):
    from app.a2a.client import callbacks

    async with _temp_database() as sessionmaker, sessionmaker() as session:
        monkeypatch.setattr("app.core.database.async_session", sessionmaker)
        user, agent, partner, conversation = await _seed(session)
        row = await _answered(
            session,
            user=user,
            agent=agent,
            partner=partner,
            conversation=conversation,
            submitted_at=datetime.now(UTC) - timedelta(minutes=5),
        )
        await session.commit()

        await callbacks._resume_caller(session, row, _answer(), partner_name="Acme")
        await _wakes_settled()
        assert delivered == [conversation.id]


async def test_work_the_user_starts_after_the_stop_is_left_alone(monkeypatch):
    """The Stop is acknowledged before its cascade reaches the database, and the
    user can send a new message in between. What that turn delegates is not the
    work the user stopped: the cascade must neither stamp nor cancel it, and the
    Stop it records is the moment of the Stop, so that work's answer is still
    delivered."""
    from app.api.v1 import chat
    from app.services import sub_agent

    gate = asyncio.Event()
    real = sub_agent.cancel_tasks_for_conversation

    async def _gated(db, conversation_id, **kwargs):
        await gate.wait()
        return await real(db, conversation_id, **kwargs)

    async with _temp_database() as sessionmaker, sessionmaker() as session:
        monkeypatch.setattr("app.core.database.async_session", sessionmaker)
        monkeypatch.setattr(sub_agent, "cancel_tasks_for_conversation", _gated)
        user, agent, partner, conversation = await _seed(session)
        stopped_work = await _outstanding(
            session,
            user=user,
            agent=agent,
            partner=partner,
            conversation=conversation,
            submitted_at=datetime.now(UTC) - timedelta(minutes=5),
        )
        await session.commit()

        chat._spawn_cancel_cascade(str(conversation.id))
        await asyncio.sleep(0.01)
        next_work = await _outstanding(
            session,
            user=user,
            agent=agent,
            partner=partner,
            conversation=conversation,
            submitted_at=datetime.now(UTC),
        )
        await session.commit()
        gate.set()
        await asyncio.gather(*list(chat._cancel_cascade_tasks))

        rows = {
            r.id: r
            for r in (
                await session.execute(
                    select(A2ADelegation).execution_options(populate_existing=True)
                )
            ).scalars()
        }
        assert rows[stopped_work.id].outcome == "cancelled"
        assert rows[stopped_work.id].cancel_pending_at is not None
        assert rows[next_work.id].outcome is None, (
            "the cascade cancelled work the user started after the Stop"
        )
        assert rows[next_work.id].cancel_pending_at is None, (
            "the cascade stamped work the user started after the Stop"
        )
        stopped = await _read_conversation(session, conversation.id)
        assert stopped.stopped_at < next_work.submitted_at, (
            "the Stop was recorded after the work the user started next, whose "
            "answer would then be dropped"
        )


async def test_an_older_stop_never_moves_the_record_back(monkeypatch):
    """Two cascades can finish out of order: the record keeps the latest Stop."""
    from app.services import sub_agent

    async with _temp_database() as sessionmaker, sessionmaker() as session:
        monkeypatch.setattr("app.core.database.async_session", sessionmaker)
        _user, _agent, _partner, conversation = await _seed(session)
        await session.commit()
        later = datetime.now(UTC)
        earlier = later - timedelta(seconds=30)

        await sub_agent.cancel_tasks_for_conversation(
            None, str(conversation.id), stopped_at=later
        )
        await sub_agent.cancel_tasks_for_conversation(
            None, str(conversation.id), stopped_at=earlier
        )
        assert (await _read_conversation(session, conversation.id)).stopped_at == later


async def test_the_follow_up_guard_reads_the_stop_the_cascade_wrote(monkeypatch):
    """The stopped wake turn's follow-up check reads the same record: a Stop
    recorded after the chain began stops it, an older one does not."""
    from app.services import sub_agent
    from app.services.parent_wake import _stopped_since

    async with _temp_database() as sessionmaker, sessionmaker() as session:
        monkeypatch.setattr("app.core.database.async_session", sessionmaker)
        _user, _agent, _partner, conversation = await _seed(session)
        await session.commit()
        conv_id = conversation.id
        chain_began = datetime.now(UTC)
        assert await _stopped_since(session, conv_id, chain_began) is False

        await sub_agent.cancel_tasks_for_conversation(None, str(conv_id))
        await session.rollback()
        assert await _stopped_since(session, conv_id, chain_began) is True
        assert (
            await _stopped_since(session, conv_id, datetime.now(UTC)) is False
        ), "a Stop older than the chain blocked it"
