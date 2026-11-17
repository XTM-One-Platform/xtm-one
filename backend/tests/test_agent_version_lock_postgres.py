"""The agent row lock that numbers versions, on a real PostgreSQL.

``update_agent`` failed on the statement timeout when the agent updated itself
from its own run: the run's turn had inserted a row referencing the agent (a
delegation record), so PostgreSQL held ``FOR KEY SHARE`` on the agent row until
the turn committed, and the versioning lock was ``FOR UPDATE``, which conflicts
with it. The lock is now ``FOR NO KEY UPDATE`` (no conflict with a foreign-key
insert) and bounded: a real writer holding the row past a few seconds gets a
typed retryable answer, the caller's session stays usable, and the next call
succeeds once the row is free.
"""

from __future__ import annotations

import time
import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.agent import Agent
from app.models.prompt_version import PromptVersion
from app.services import prompt_versioning
from tests.test_agent_builder_versions_postgres import _agent, _builder, _user
from tests.test_provisioning_postgres import _temp_database

pytestmark = pytest.mark.integration


def _fast_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(prompt_versioning, "_VERSION_LOCK_TIMEOUT", "200ms")
    monkeypatch.setattr(prompt_versioning, "_VERSION_LOCK_BACKOFF_SECONDS", (0.05,))


async def _personas(sessionmaker: async_sessionmaker[AsyncSession], agent_id: uuid.UUID) -> list[str]:
    async with sessionmaker() as session:
        rows = (
            (
                await session.execute(
                    select(PromptVersion.content)
                    .where(PromptVersion.agent_id == agent_id)
                    .order_by(PromptVersion.version_number)
                )
            )
            .scalars()
            .all()
        )
    return list(rows)


@pytest.mark.asyncio
async def test_an_agent_updates_itself_while_its_run_references_its_row(monkeypatch: pytest.MonkeyPatch) -> None:
    """A foreign-key insert's lock (FOR KEY SHARE), held by the agent's own open
    turn, no longer blocks the versioning lock."""
    _fast_lock(monkeypatch)
    async with _temp_database("avl_self") as sessionmaker:
        user = await _user(sessionmaker)
        agent_id = await _agent(sessionmaker, user, persona="Before.")
        async with sessionmaker() as run_turn:
            # What the insert of a delegation, run or friction row takes.
            await run_turn.execute(
                select(Agent.id).where(Agent.id == agent_id).with_for_update(read=True, key_share=True)
            )
            started = time.monotonic()
            content = await _builder(user.id, "update_agent", {"agent_id": str(agent_id), "persona": "After."})
            assert time.monotonic() - started < 5
            assert "updated:" in content
            await run_turn.commit()
        assert (await _personas(sessionmaker, agent_id))[-1] == "After."


@pytest.mark.asyncio
async def test_a_held_row_answers_retryable_then_succeeds_once_released(monkeypatch: pytest.MonkeyPatch) -> None:
    _fast_lock(monkeypatch)
    async with _temp_database("avl_busy") as sessionmaker:
        user = await _user(sessionmaker)
        agent_id = await _agent(sessionmaker, user, persona="Before.")
        async with sessionmaker() as holder:
            # Another writer of the agent row, mid-transaction.
            await holder.execute(select(Agent.id).where(Agent.id == agent_id).with_for_update())
            started = time.monotonic()
            content = await _builder(user.id, "update_agent", {"agent_id": str(agent_id), "persona": "After."})
            assert time.monotonic() - started < 5
            assert content.startswith("Error: ")
            assert prompt_versioning.AGENT_VERSION_LOCK_BUSY_DETAIL in content
            await holder.rollback()
        async with sessionmaker() as reader:
            assert (await reader.get(Agent, agent_id)).persona == "Before."
        assert await _personas(sessionmaker, agent_id) == []

        content = await _builder(user.id, "update_agent", {"agent_id": str(agent_id), "persona": "After."})
        assert "updated:" in content
        assert (await _personas(sessionmaker, agent_id))[-1] == "After."


@pytest.mark.asyncio
async def test_an_expired_wait_leaves_the_caller_session_usable(monkeypatch: pytest.MonkeyPatch) -> None:
    _fast_lock(monkeypatch)
    async with _temp_database("avl_session") as sessionmaker:
        user = await _user(sessionmaker)
        agent_id = await _agent(sessionmaker, user)
        async with sessionmaker() as holder, sessionmaker() as caller:
            await holder.execute(select(Agent.id).where(Agent.id == agent_id).with_for_update())
            with pytest.raises(prompt_versioning.AgentVersionLockBusyError):
                await prompt_versioning.lock_agent_for_versioning(caller, agent_id)
            # The savepoint rolled back alone: the transaction goes on, and the
            # session's lock_timeout is the one it had before.
            assert (await caller.execute(select(Agent.id).where(Agent.id == agent_id))).scalar_one() == agent_id
            await holder.rollback()
            await prompt_versioning.lock_agent_for_versioning(caller, agent_id)
            assert (await caller.execute(text("SELECT current_setting('lock_timeout')"))).scalar_one() == "0"
            await caller.commit()


@pytest.mark.asyncio
async def test_two_versioning_writers_still_serialise(monkeypatch: pytest.MonkeyPatch) -> None:
    """NO KEY UPDATE still excludes a second versioning writer: the numbers never collide."""
    _fast_lock(monkeypatch)
    async with _temp_database("avl_serial") as sessionmaker:
        user = await _user(sessionmaker)
        agent_id = await _agent(sessionmaker, user)
        async with sessionmaker() as first, sessionmaker() as second:
            await prompt_versioning.lock_agent_for_versioning(first, agent_id)
            with pytest.raises(prompt_versioning.AgentVersionLockBusyError):
                await prompt_versioning.lock_agent_for_versioning(second, agent_id)
            await first.rollback()
            await second.rollback()
