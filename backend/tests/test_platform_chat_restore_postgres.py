"""The embedded platform chat restore, against a real PostgreSQL.

Same scratch-database recipe as ``test_store_message_sequence_postgres.py``.
A real database because what is under test IS the SQL: which rows the
``ORDER BY ... LIMIT`` window keeps, in which order they come back, and how
many statements a restore costs. ``test_platform_chat_session_agent.py``
drives the same endpoint through a session double, which returns whatever
rows it is handed: it can pin the statement's shape, never tell a stable
ordering from an arbitrary one.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, event
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

import app.models  # noqa: F401 — registers every mapper on Base.metadata
from app.api.v1.platform import (
    _PLATFORM_CHAT_RESTORE_LIMIT,
    PLATFORM_CHAT_CHANNEL,
    PlatformChatSessionRequest,
    PlatformChatSessionResponse,
    create_platform_chat_session,
)
from app.models.agent import Agent
from app.models.chat import Conversation, Message, MessageFeedback
from app.models.user import User
from app.services.parent_wake import PARENT_WAKE_SOURCE
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration

LIMIT = _PLATFORM_CHAT_RESTORE_LIMIT
_START = datetime(2026, 1, 1, tzinfo=UTC)


@asynccontextmanager
async def _temp_database() -> AsyncIterator[tuple[async_sessionmaker, AsyncEngine]]:
    async with scratch_database(
        "restore",
        tables=ALL_TABLES,
        extensions=("vector",),
        bind_app_sessions=False,
    ) as db:
        yield db.sessions, db.engine


@contextmanager
def _statements(engine: AsyncEngine) -> Iterator[list[str]]:
    """Every statement the engine sends while the block runs, one line each."""
    seen: list[str] = []

    def _record(_conn, _cursor, statement, _params, _context, _executemany):
        seen.append(" ".join(statement.split()))

    event.listen(engine.sync_engine, "before_cursor_execute", _record)
    try:
        yield seen
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", _record)


async def _agents(sessionmaker, *names: str) -> dict[str, Agent]:
    """Enabled agents whose slug is their lowercased name."""
    async with sessionmaker() as session:
        agents = {name: Agent(name=name, slug=name.lower()) for name in names}
        session.add_all(agents.values())
        await session.commit()
        return agents


async def _user(sessionmaker) -> User:
    async with sessionmaker() as session:
        user = User(
            id=uuid.uuid4(),
            email=f"restore-{uuid.uuid4().hex[:8]}@test.local",
            full_name="Restore Test",
            hashed_password="x",
        )
        session.add(user)
        await session.commit()
        return user


async def _conversation(
    sessionmaker,
    user: User,
    *,
    agent: Agent,
    rows: list[dict],
    original: Agent | None = None,
    shuffle: bool = False,
) -> uuid.UUID:
    """One platform conversation holding ``rows``, oldest first.

    Each row is a ``role`` and a ``content``, with an optional ``sequence``
    (default: its position + 1, what ``store_message`` allocates) and ``meta``.
    Creation times follow the list, one second apart. ``shuffle`` inserts the
    rows in a scrambled physical order, so an ordering that falls back on how
    PostgreSQL happens to scan the heap cannot pass for a real one.
    """
    async with sessionmaker() as session:
        conv = Conversation(
            user_id=user.id,
            channel=PLATFORM_CHAT_CHANNEL,
            agent_id=agent.id,
            original_agent_id=(original or agent).id,
            title="restore",
        )
        session.add(conv)
        await session.flush()
        messages = [
            Message(
                conversation_id=conv.id,
                role=row["role"],
                content=row["content"],
                channel=PLATFORM_CHAT_CHANNEL,
                sequence=row.get("sequence", position + 1),
                created_at=_START + timedelta(seconds=position),
                metadata_json=json.dumps(row["meta"]) if row.get("meta") else None,
            )
            for position, row in enumerate(rows)
        ]
        if shuffle:
            # Odd positions newest first, then even positions oldest first:
            # neither the physical order nor its reverse is the thread's.
            messages = messages[1::2][::-1] + messages[::2]
            for message in messages:
                session.add(message)
                await session.flush()
        else:
            session.add_all(messages)
        await session.commit()
        return conv.id


async def _restore(
    sessionmaker, user: User, conversation_id, *, slug: str
) -> PlatformChatSessionResponse:
    async with sessionmaker() as session:
        return await create_platform_chat_session(
            PlatformChatSessionRequest(conversation_id=str(conversation_id), agent_slug=slug),
            db=session,
            user=user,
        )


def _thread(count: int) -> list[dict]:
    """``count`` messages, user and assistant alternating, named m0, m1, ..."""
    return [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"m{i}"}
        for i in range(count)
    ]


def _assistant_names(resp: PlatformChatSessionResponse) -> list[str | None]:
    return [m.get("agent_name") for m in resp.messages if m["role"] == "assistant"]


def _transferred_thread(target: Agent) -> list[dict]:
    """Alpha answers, then hands over to *target* mid-turn, which keeps it."""
    chain = [{"agent_id": str(target.id), "agent_name": target.name}]
    return [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "by alpha"},
        {"role": "user", "content": "transfer me"},
        {"role": "assistant", "content": "by beta", "meta": {"transfer_chain": chain}},
        {"role": "user", "content": "thanks"},
        {"role": "assistant", "content": "still beta"},
    ]


async def test_the_window_at_every_boundary():
    """0, 1, 49, 50, 51 and 100 messages: the newest N, oldest first.

    Up to the limit the whole thread is restored and attributed from the
    original agent (exactly N used to count as truncated); past it the
    newest N come back and the walk is skipped, its start being unknown.
    """
    async with _temp_database() as (sessionmaker, _engine):
        agents = await _agents(sessionmaker, "Alpha")
        user = await _user(sessionmaker)
        for count in (0, 1, LIMIT - 1, LIMIT, LIMIT + 1, 2 * LIMIT):
            conv_id = await _conversation(
                sessionmaker, user, agent=agents["Alpha"], rows=_thread(count)
            )
            resp = await _restore(sessionmaker, user, conv_id, slug="alpha")

            kept = min(count, LIMIT)
            assert resp.conversation_id == str(conv_id)
            assert [m["content"] for m in resp.messages] == [
                f"m{i}" for i in range(count - kept, count)
            ], count
            names = _assistant_names(resp)
            assert names == ["Alpha" if count <= LIMIT else None] * len(names), count
            assert resp.agent_name == "Alpha"


async def test_tied_sequences_keep_the_newest_window_in_order():
    """Legacy rows share ``sequence=0``, and a stored sequence may be duplicated.

    ``sequence`` alone then orders nothing: the window, the sentinel dropped
    from it and the display order were whatever PostgreSQL scanned first. The
    web chat's ``(sequence, created_at, id)`` order makes them the newest rows,
    oldest first.
    """
    async with _temp_database() as (sessionmaker, _engine):
        agents = await _agents(sessionmaker, "Alpha")
        user = await _user(sessionmaker)

        legacy = [dict(row, sequence=0) for row in _thread(LIMIT + 10)]
        conv_id = await _conversation(
            sessionmaker, user, agent=agents["Alpha"], rows=legacy, shuffle=True
        )
        resp = await _restore(sessionmaker, user, conv_id, slug="alpha")
        assert [m["content"] for m in resp.messages] == [f"m{i}" for i in range(10, LIMIT + 10)]

        short = [dict(row, sequence=0) for row in _thread(10)]
        conv_id = await _conversation(
            sessionmaker, user, agent=agents["Alpha"], rows=short, shuffle=True
        )
        resp = await _restore(sessionmaker, user, conv_id, slug="alpha")
        assert [m["content"] for m in resp.messages] == [f"m{i}" for i in range(10)]
        assert _assistant_names(resp) == ["Alpha"] * 5


async def test_rows_the_panel_never_shows_take_no_place_in_the_window():
    """Tool and system rows and hidden wakes are not the thread's messages.

    A thread of exactly N visible messages plus internal rows was called
    truncated: fewer than N came back and the attribution was dropped. A
    parent wake, the platform's own prompt, came back as a user bubble.
    The web chat filters the roles before its LIMIT and never renders a wake.
    """
    async with _temp_database() as (sessionmaker, _engine):
        agents = await _agents(sessionmaker, "Alpha")
        user = await _user(sessionmaker)

        visible = _thread(LIMIT)
        internal = [
            {"role": "tool", "content": "tool output"},
            {"role": "system", "content": "notice"},
        ]
        conv_id = await _conversation(
            sessionmaker, user, agent=agents["Alpha"], rows=visible[:10] + internal + visible[10:]
        )
        resp = await _restore(sessionmaker, user, conv_id, slug="alpha")
        assert [m["content"] for m in resp.messages] == [row["content"] for row in visible]
        assert _assistant_names(resp) == ["Alpha"] * (LIMIT // 2)

        wake = {
            "role": "user",
            "content": "background task finished: raw specialist output",
            "meta": {"hidden": True, "source": PARENT_WAKE_SOURCE},
        }
        rows = [
            {"role": "user", "content": "question"},
            {"role": "assistant", "content": "delegated"},
            wake,
            {"role": "assistant", "content": "summary"},
        ]
        conv_id = await _conversation(sessionmaker, user, agent=agents["Alpha"], rows=rows)
        resp = await _restore(sessionmaker, user, conv_id, slug="alpha")
        assert [m["content"] for m in resp.messages] == ["question", "delegated", "summary"]
        assert _assistant_names(resp) == ["Alpha", "Alpha"]


async def test_a_transferred_thread_is_named_with_one_name_query():
    """Per-message agents from the persisted chain, both names in one query.

    However long the thread, a restore costs the same statements: no
    per-message lookup, and a single ``IN`` read for the original and the
    current agent.
    """
    async with _temp_database() as (sessionmaker, engine):
        agents = await _agents(sessionmaker, "Alpha", "Beta")
        user = await _user(sessionmaker)
        conv_id = await _conversation(
            sessionmaker,
            user,
            agent=agents["Beta"],
            original=agents["Alpha"],
            rows=_transferred_thread(agents["Beta"]),
        )
        with _statements(engine) as short:
            resp = await _restore(sessionmaker, user, conv_id, slug="alpha")
        assert [m.get("agent_name") for m in resp.messages] == [
            None, "Alpha", None, "Beta", None, "Beta",
        ]
        assert resp.agent_id == str(agents["Beta"].id)
        assert resp.agent_name == "Beta"
        name_reads = [s for s in short if s.startswith("SELECT agents.id, agents.name FROM agents")]
        assert len(name_reads) == 1
        assert len([s for s in short if " FROM messages" in s]) == 1

        conv_id = await _conversation(
            sessionmaker, user, agent=agents["Beta"], original=agents["Alpha"], rows=_thread(LIMIT)
        )
        with _statements(engine) as long:
            await _restore(sessionmaker, user, conv_id, slug="alpha")
        assert len(long) == len(short)


async def test_deleted_agents_leave_honest_gaps():
    """``ON DELETE SET NULL`` clears the ids: no crash, and no guessed name.

    With the original agent gone the walk has no start, so answers before the
    first recorded handover carry no name (the panel falls back) while those
    after it keep the name the chain recorded. With the current agent gone as
    well, the conversation reports neither an id nor a name.
    """
    async with _temp_database() as (sessionmaker, _engine):
        agents = await _agents(sessionmaker, "Alpha", "Beta", "Gamma")
        user = await _user(sessionmaker)
        conv_id = await _conversation(
            sessionmaker,
            user,
            agent=agents["Beta"],
            original=agents["Alpha"],
            rows=_transferred_thread(agents["Beta"]),
        )

        for gone, agent_name in (("Alpha", "Beta"), ("Beta", None)):
            async with sessionmaker() as session:
                await session.execute(delete(Agent).where(Agent.id == agents[gone].id))
                await session.commit()
            resp = await _restore(sessionmaker, user, conv_id, slug="gamma")
            assert _assistant_names(resp) == [None, "Beta", "Beta"], gone
            assert resp.agent_name == agent_name, gone
        assert resp.agent_id is None


async def test_only_the_callers_own_conversation_is_restored():
    """Someone else's conversation id opens a fresh session of the caller's own.

    The restore reads ``Conversation.user_id == caller``: a foreign id is
    handled like a stale one and never returns a message.
    """
    async with _temp_database() as (sessionmaker, _engine):
        agents = await _agents(sessionmaker, "Alpha")
        owner, stranger = await _user(sessionmaker), await _user(sessionmaker)
        conv_id = await _conversation(sessionmaker, owner, agent=agents["Alpha"], rows=_thread(4))

        resp = await _restore(sessionmaker, stranger, conv_id, slug="alpha")
        assert resp.conversation_id != str(conv_id)
        assert resp.messages == []
        async with sessionmaker() as session:
            fresh = await session.get(Conversation, uuid.UUID(resp.conversation_id))
        assert fresh is not None
        assert fresh.user_id == stranger.id

        resp = await _restore(sessionmaker, owner, conv_id, slug="alpha")
        assert [m["content"] for m in resp.messages] == ["m0", "m1", "m2", "m3"]


async def _messages_of(sessionmaker, conversation_id) -> list[Message]:
    async with sessionmaker() as session:
        result = await session.execute(
            Message.__table__.select().where(Message.conversation_id == conversation_id).order_by(Message.sequence)
        )
        return list(result.all())


async def test_restored_entries_carry_their_id_time_and_the_callers_rating():
    """Each entry names its row and its time; an answer the caller rated says so.

    The panel rates a restored answer by its id, orders the thread by its
    time, and shows a thumb it already gave as given. The ratings are the
    caller's alone: another user's rating of the same answer never leaks.
    """
    async with _temp_database() as (sessionmaker, engine):
        agents = await _agents(sessionmaker, "Alpha")
        owner, other = await _user(sessionmaker), await _user(sessionmaker)
        conv_id = await _conversation(sessionmaker, owner, agent=agents["Alpha"], rows=_thread(6))
        stored = await _messages_of(sessionmaker, conv_id)
        by_content = {row.content: row for row in stored}
        async with sessionmaker() as session:
            session.add_all(
                [
                    MessageFeedback(
                        message_id=by_content["m1"].id, user_id=owner.id, rating="positive", comment="spot on"
                    ),
                    MessageFeedback(message_id=by_content["m3"].id, user_id=owner.id, rating="negative"),
                    # Another user's ratings: of an answer the owner rated too,
                    # and of one the owner never rated.
                    MessageFeedback(
                        message_id=by_content["m1"].id, user_id=other.id, rating="negative", comment="theirs"
                    ),
                    MessageFeedback(
                        message_id=by_content["m5"].id, user_id=other.id, rating="positive", comment="theirs"
                    ),
                ]
            )
            await session.commit()

        with _statements(engine) as seen:
            resp = await _restore(sessionmaker, owner, conv_id, slug="alpha")

        assert [m["content"] for m in resp.messages] == [f"m{i}" for i in range(6)]
        for entry in resp.messages:
            row = by_content[entry["content"]]
            assert entry["id"] == str(row.id)
            restored_at = datetime.fromisoformat(entry["created_at"])
            assert restored_at.tzinfo is not None, entry["created_at"]
            assert restored_at == row.created_at
        feedback = {m["content"]: m.get("feedback") for m in resp.messages if "feedback" in m}
        assert feedback == {
            "m1": {"rating": "positive", "comment": "spot on"},
            "m3": {"rating": "negative", "comment": None},
        }
        assert all("feedback" not in m for m in resp.messages if m["role"] == "user")
        # The unrated answer, and the one only the other user rated, carry no key.
        assert "feedback" not in resp.messages[5]

        # One batched read of the caller's ratings, never one per message.
        feedback_reads = [s for s in seen if " FROM message_feedback" in s]
        assert len(feedback_reads) == 1

        # And the other user, restoring nothing of the owner's, gets nothing of theirs.
        resp = await _restore(sessionmaker, other, conv_id, slug="alpha")
        assert resp.messages == []


async def test_a_restored_user_message_carries_the_conversations_it_referenced():
    """The panel shows a reference as a chip, as the web chat does:
    the restored user message carries its ``conversation_refs``, validated,
    and nothing else of the referenced conversation."""
    ref = {"conversation_id": str(uuid.uuid4()), "title": "Q3 threat report", "key": "q3-threat-report"}
    async with _temp_database() as (sessionmaker, _engine):
        agents = await _agents(sessionmaker, "Alpha")
        owner = await _user(sessionmaker)
        rows = [
            {"role": "user", "content": "@q3-threat-report what did we decide?", "meta": {"conversation_refs": [ref]}},
            {"role": "assistant", "content": "We decided to escalate."},
            {"role": "user", "content": "thanks"},
        ]
        conv_id = await _conversation(sessionmaker, owner, agent=agents["Alpha"], rows=rows)
        resp = await _restore(sessionmaker, owner, conv_id, slug="alpha")

    first, answer, last = resp.messages
    assert first["conversation_refs"] == [ref]
    assert "conversation_refs" not in answer and "conversation_refs" not in last
