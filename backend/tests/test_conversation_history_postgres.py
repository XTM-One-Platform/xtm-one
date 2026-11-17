"""Conversation history order and the platform history count, on PostgreSQL.

Same scratch-database recipe as ``test_store_message_sequence_postgres.py``.
A real database because what is under test IS the SQL:

* ``get_conversation_history`` is the history the send path hands the model
  on every channel. Ordered on ``sequence`` alone, tied rows (legacy
  ``sequence=0``, the duplicate) came back in heap order - a session
  double returns rows in the order it is handed, so only PostgreSQL can show
  it.
* ``GET /platform/chat/sessions`` counts the messages of each listed
  conversation; which rows the count reads, and that it is one bounded
  statement, is a property of the query.
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

import app.models  # noqa: F401 - registers every mapper on Base.metadata
from app.api.v1.platform import PLATFORM_CHAT_CHANNEL, list_platform_chat_sessions
from app.channels.router import get_conversation_history
from app.models.chat import Conversation, Message
from app.models.user import User
from app.services.chat_messages import message_cursor_filter, message_is_hidden, read_visible_message_page
from app.services.parent_wake import PARENT_WAKE_SOURCE
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration

_START = datetime(2026, 1, 1, tzinfo=UTC)
_WAKE = {"hidden": True, "source": PARENT_WAKE_SOURCE}


@asynccontextmanager
async def _temp_database() -> AsyncIterator[tuple[async_sessionmaker, AsyncEngine]]:
    async with scratch_database("history", tables=ALL_TABLES, extensions=("vector",)) as db:
        yield db.sessions, db.engine


@contextmanager
def _statements(engine: AsyncEngine) -> Iterator[list[str]]:
    """Every statement the engine sends while the block runs."""
    seen: list[str] = []

    def _record(_conn, _cursor, statement, _params, _context, _executemany):
        seen.append(" ".join(statement.split()))

    event.listen(engine.sync_engine, "before_cursor_execute", _record)
    try:
        yield seen
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", _record)


async def _user(sessionmaker) -> User:
    async with sessionmaker() as session:
        user = User(
            id=uuid.uuid4(),
            email=f"history-{uuid.uuid4().hex[:8]}@test.local",
            full_name="History Test",
            hashed_password="x",
        )
        session.add(user)
        await session.commit()
        return user


async def _conversation(
    sessionmaker,
    user: User,
    rows: list[dict],
    *,
    shuffle: bool = False,
    archived: bool = False,
    minutes_ago: int = 0,
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
            title="history",
            is_archived=archived,
            updated_at=_START - timedelta(minutes=minutes_ago),
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


def _thread(count: int) -> list[dict]:
    """``count`` messages, user and assistant alternating, named m0, m1, ..."""
    return [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"m{i}"}
        for i in range(count)
    ]


async def _history(sessionmaker, conversation_id: uuid.UUID, limit: int = 50) -> list[str]:
    async with sessionmaker() as session:
        messages = await get_conversation_history(session, conversation_id, limit=limit)
    return [m.content for m in messages]


async def test_history_keeps_the_newest_window_in_order_when_sequences_tie():
    """Tied sequences, inserted out of physical order: the newest N, in order.

    The model reads this list as the conversation. Ordered on ``sequence``
    alone, the window and the order inside it were whatever PostgreSQL
    scanned first; the web chat's ``(sequence, created_at, id)`` order makes
    them the newest rows, oldest first.
    """
    async with _temp_database() as (sessionmaker, _engine):
        user = await _user(sessionmaker)

        legacy = [dict(row, sequence=0) for row in _thread(60)]
        conv_id = await _conversation(sessionmaker, user, legacy, shuffle=True)
        assert await _history(sessionmaker, conv_id) == [f"m{i}" for i in range(10, 60)]

        short = [dict(row, sequence=0) for row in _thread(10)]
        conv_id = await _conversation(sessionmaker, user, short, shuffle=True)
        assert await _history(sessionmaker, conv_id) == [f"m{i}" for i in range(10)]


async def test_history_window_at_the_limit():
    """Distinct sequences: exactly ``limit`` rows is the whole thread, one more
    drops only the oldest."""
    async with _temp_database() as (sessionmaker, _engine):
        user = await _user(sessionmaker)
        for count in (0, 1, 5, 6):
            conv_id = await _conversation(sessionmaker, user, _thread(count))
            kept = min(count, 5)
            assert await _history(sessionmaker, conv_id, limit=5) == [
                f"m{i}" for i in range(count - kept, count)
            ], count


async def test_history_gap_guard_counts_rows_tied_with_the_window(caplog):
    """The stale-watermark warning counts every row between the watermark and
    the window, rows sharing the window's first ``sequence`` included.

    ``sequence < first.sequence`` alone missed them: here the loaded window
    starts on a ``sequence=2`` row with an older ``sequence=2`` row before it.
    """
    async with _temp_database() as (sessionmaker, _engine):
        user = await _user(sessionmaker)
        rows = [dict(row, sequence=seq) for row, seq in zip(_thread(4), (1, 2, 2, 2), strict=True)]
        conv_id = await _conversation(sessionmaker, user, rows)
        state = {"directives": "", "summary": "earlier", "through_sequence": 0, "compacted_count": 0}
        async with sessionmaker() as session:
            conv = await session.get(Conversation, conv_id)
            conv.compaction_state_json = json.dumps(state)
            await session.commit()

        with caplog.at_level(logging.WARNING, logger="app.channels.router"):
            async with sessionmaker() as session:
                history = await get_conversation_history(session, conv_id, limit=2)
        assert history[0].role == "system"
        assert [m.content for m in history[1:]] == ["m2", "m3"]
        assert "2 message(s) between sequence 0" in caplog.text


async def test_history_list_counts_the_messages_a_restore_shows():
    """``message_count`` is what the panel shows, read in one bounded statement,
    and each row's preview in one more for the whole page.

    A hidden parent wake and internal tool / system rows are not the
    conversation's messages: they inflated the count, and a conversation
    holding nothing else was listed. An ordinary conversation keeps its count.
    """
    async with _temp_database() as (sessionmaker, engine):
        user, stranger = await _user(sessionmaker), await _user(sessionmaker)
        await _conversation(sessionmaker, stranger, _thread(3))

        ordinary = await _conversation(sessionmaker, user, _thread(4), minutes_ago=1)
        with_internal = await _conversation(
            sessionmaker,
            user,
            [
                {"role": "user", "content": "question"},
                {"role": "assistant", "content": "delegated"},
                {"role": "user", "content": "wake", "meta": _WAKE},
                {"role": "tool", "content": "tool output"},
                {"role": "system", "content": "notice"},
                {"role": "assistant", "content": "summary"},
            ],
            minutes_ago=2,
        )
        await _conversation(sessionmaker, user, [], minutes_ago=3)
        await _conversation(
            sessionmaker, user, [{"role": "user", "content": "wake", "meta": _WAKE}], minutes_ago=4
        )
        await _conversation(sessionmaker, user, _thread(2), archived=True, minutes_ago=5)

        async with sessionmaker() as session:
            with _statements(engine) as seen:
                listed = (await list_platform_chat_sessions(db=session, user=user))["conversations"]

        assert [(c["conversation_id"], c["message_count"], c["last_message_preview"]) for c in listed] == [
            (str(ordinary), 4, "m3"),
            (str(with_internal), 3, "summary"),
        ]
        # The list, then every row's preview at once (``last_message_previews``).
        assert len(seen) == 2
        assert "row_number() OVER" in seen[1]


async def test_a_page_leaves_a_run_of_hidden_wakes_out_in_one_query():
    """``read_visible_message_page`` reads one ``limit + 1`` window with the
    wakes left out by the query: a run of them longer than a page neither
    empties the page nor counts as older history."""
    async with _temp_database() as (sessionmaker, engine):
        user = await _user(sessionmaker)
        wakes = [{"role": "user", "content": "wake", "meta": _WAKE}] * 12
        conv_id = await _conversation(sessionmaker, user, _thread(3) + wakes + _thread(5))
        only_wakes_before = await _conversation(sessionmaker, user, wakes + _thread(5))
        # Metadata that only names the wake, its "_" a LIKE wildcard: shown.
        mentions = await _conversation(
            sessionmaker,
            user,
            [
                {"role": "assistant", "content": "about", "meta": {"reasoning": f"a {PARENT_WAKE_SOURCE} came"}},
                {"role": "assistant", "content": "spaced", "meta": {"reasoning": "a background task wake came"}},
            ],
        )

        async with sessionmaker() as session:
            with _statements(engine) as seen:
                page, has_more = await read_visible_message_page(
                    session, [Message.conversation_id == conv_id], 5
                )
            assert [m.sequence for m in page] == [16, 17, 18, 19, 20] and has_more is True
            assert len(seen) == 1

            older, has_more = await read_visible_message_page(
                session, [Message.conversation_id == conv_id, message_cursor_filter(page[0])], 5
            )
            assert [m.content for m in older] == ["m0", "m1", "m2"] and has_more is False

            page, has_more = await read_visible_message_page(
                session, [Message.conversation_id == only_wakes_before], 5
            )
            assert len(page) == 5 and has_more is False

            page, _ = await read_visible_message_page(session, [Message.conversation_id == mentions], 5)
            assert [m.content for m in page] == ["about", "spaced"]
            assert not any(message_is_hidden(m) for m in page)
