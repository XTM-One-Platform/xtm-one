"""Concurrent ``store_message`` calls must never allocate the same sequence.

Same scratch-database recipe as ``test_a2a_hooks_postgres.py``.  A real
database rather than a fake session because what is under test IS the row
lock: ``store_message`` bumps ``conversation.updated_at`` BEFORE reading
``max(sequence)``, so the UPDATE's row lock serializes sequence allocation
per conversation.  A session double cannot model two transactions racing
through READ COMMITTED snapshots.

The race became reachable with mid-turn segment delivery: the WS
``segment_complete`` branch persists an assistant message on its own
short-lived session WHILE the steering listener can persist a user steering
message on another session.  Before the reorder both could read the same
``max(sequence)`` (the schema's sequence index is non-unique) and commit
duplicate sequence numbers, making history reloads order the segment and the
steering correction nondeterministically.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.models  # noqa: F401 — registers every mapper on Base.metadata
from app.channels.router import store_message
from app.models.chat import Conversation, Message
from app.models.user import User
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration


@asynccontextmanager
async def _temp_database() -> AsyncIterator[async_sessionmaker]:
    async with scratch_database("msgseq", tables=ALL_TABLES, extensions=("vector",)) as db:
        yield db.sessions


async def _seed_conversation(sessionmaker) -> uuid.UUID:
    async with sessionmaker() as session:
        user = User(
            id=uuid.uuid4(),
            email=f"seq-{uuid.uuid4().hex[:8]}@test.local",
            full_name="Sequence Test",
            hashed_password="x",
        )
        session.add(user)
        await session.flush()
        conv = Conversation(user_id=user.id, title="seq race")
        session.add(conv)
        await session.commit()
        return conv.id


async def test_concurrent_store_message_allocates_distinct_sequences():
    """N writers on N separate sessions racing on one conversation (the
    mid-turn segment persist vs the steering listener) must
    produce N distinct, gap-free sequence numbers."""
    writers = 8
    async with _temp_database() as sessionmaker:
        conv_id = await _seed_conversation(sessionmaker)

        # One barrier so every writer opens its transaction before any
        # commits — maximizing the window in which an unserialized
        # ``max(sequence)`` read produces duplicates.
        barrier = asyncio.Barrier(writers)

        async def _write(n: int) -> None:
            async with sessionmaker() as session:
                await barrier.wait()
                await store_message(
                    session,
                    conv_id,
                    role="assistant" if n % 2 else "user",
                    content=f"message {n}",
                    channel="web",
                )
                await session.commit()

        await asyncio.gather(*(_write(n) for n in range(writers)))

        async with sessionmaker() as session:
            rows = await session.execute(
                select(Message.sequence).where(
                    Message.conversation_id == conv_id
                )
            )
            sequences = sorted(r[0] for r in rows)
        assert sequences == list(range(1, writers + 1)), (
            "concurrent store_message calls must serialize sequence "
            f"allocation per conversation — got {sequences}"
        )
