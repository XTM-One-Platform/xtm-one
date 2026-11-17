"""Tests for cursor-based conversation message pagination.

Covers the pure pagination helpers (page slicing, ordering, cursor filter
structure) and the ``GET /conversations/{id}/messages`` endpoint logic
(cursor resolution, ``has_more``, chronological ordering, error paths)
using the fake-DB pattern shared by the other chat API tests.
"""

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy.dialects import postgresql

from app.api.v1.chat import get_conversation_messages
from app.models.chat import Message
from app.services.chat_messages import (
    message_cursor_filter,
    message_order_desc,
    read_visible_message_page,
    slice_message_page,
)
from app.services.parent_wake import PARENT_WAKE_METADATA

# ---------------------------------------------------------------------------
# Fixtures / fakes
# ---------------------------------------------------------------------------

_BASE_TIME = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def _make_row(
    sequence: int,
    *,
    created_at: datetime | None = None,
    msg_id: uuid.UUID | None = None,
    conversation_id: uuid.UUID | None = None,
) -> SimpleNamespace:
    """A fake ``Message`` row with every attribute ``_build_message_response`` reads."""
    return SimpleNamespace(
        id=msg_id or uuid.uuid4(),
        conversation_id=conversation_id or uuid.uuid4(),
        role="user" if sequence % 2 else "assistant",
        content=f"message {sequence}",
        channel="web",
        channel_message_id=None,
        sequence=sequence,
        created_at=created_at or (_BASE_TIME + timedelta(seconds=sequence)),
        metadata_json=None,
        llm_trace_id=None,
    )


class FakeScalars:
    def __init__(self, items):
        self._items = items

    def all(self):
        return self._items


class FakeResult:
    def __init__(self, scalar=None, items=None):
        self._scalar = scalar
        self._items = items if items is not None else []

    def scalar_one_or_none(self):
        return self._scalar

    def scalars(self):
        return FakeScalars(self._items)


class FakeDB:
    """Replays canned results in order and records every executed statement."""

    def __init__(self, results):
        self._results = list(results)
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        return self._results.pop(0)


def _compile_pg(statement) -> str:
    return str(statement.compile(dialect=postgresql.dialect()))


# ---------------------------------------------------------------------------
# slice_message_page
# ---------------------------------------------------------------------------


def test_slice_message_page_detects_more_and_restores_chronological_order():
    # Newest-first fetch of limit + 1 = 4 rows (sequences 10, 9, 8, 7).
    rows = [_make_row(s) for s in (10, 9, 8, 7)]

    page, has_more = slice_message_page(rows, limit=3)

    assert has_more is True
    # Sentinel row (sequence 7) is dropped; page is chronological.
    assert [m.sequence for m in page] == [8, 9, 10]


def test_slice_message_page_no_more_when_fetch_not_full():
    rows = [_make_row(s) for s in (3, 2, 1)]

    page, has_more = slice_message_page(rows, limit=3)

    assert has_more is False
    assert [m.sequence for m in page] == [1, 2, 3]


def test_slice_message_page_empty():
    page, has_more = slice_message_page([], limit=5)

    assert page == []
    assert has_more is False


# ---------------------------------------------------------------------------
# Ordering / cursor filter (SQL structure)
# ---------------------------------------------------------------------------


def test_message_order_desc_is_sequence_then_created_at_then_id():
    clauses = [_compile_pg(c) for c in message_order_desc()]

    assert clauses == [
        "messages.sequence DESC",
        "messages.created_at DESC",
        "messages.id DESC",
    ]


def test_message_cursor_filter_uses_full_tiebreaker_chain():
    anchor = _make_row(5)

    sql = _compile_pg(message_cursor_filter(anchor))

    # Strictly-older semantics: sequence first, then created_at within an
    # equal sequence, then id within an equal (sequence, created_at).
    assert "messages.sequence <" in sql
    assert "messages.sequence =" in sql
    assert "messages.created_at <" in sql
    assert "messages.created_at =" in sql
    assert "messages.id <" in sql


# ---------------------------------------------------------------------------
# GET /conversations/{id}/messages endpoint logic
# ---------------------------------------------------------------------------


def _conv_and_user():
    conv_id = uuid.uuid4()
    user = SimpleNamespace(id=uuid.uuid4())
    # ``user_id`` / ``link_access`` are read by ``conversation_access``:
    # the handler now loads the conversation and evaluates access in Python
    # rather than filtering by owner in SQL, so the stub has to look like a
    # real row. Owned by the requester — access control itself is covered in
    # ``test_conversation_sharing.py``; these tests are about paging.
    conv = SimpleNamespace(id=conv_id, user_id=user.id, link_access="private")
    return conv, user


@pytest.mark.asyncio
async def test_get_messages_without_cursor_returns_latest_page():
    conv, user = _conv_and_user()
    # DB returns newest-first rows including the +1 sentinel.
    rows_desc = [
        _make_row(s, conversation_id=conv.id) for s in (5, 4, 3)
    ]
    db = FakeDB(
        [
            FakeResult(scalar=conv),  # conversation lookup
            FakeResult(items=rows_desc),  # message page fetch
            FakeResult(items=[]),  # feedback lookup
        ]
    )

    response = await get_conversation_messages(
        str(conv.id), before=None, limit=2, user=user, db=db
    )

    assert response.has_more is True
    assert [m.sequence for m in response.messages] == [4, 5]
    assert [m.content for m in response.messages] == ["message 4", "message 5"]
    # The page query orders newest-first and caps at limit + 1.
    page_sql = _compile_pg(db.statements[1])
    assert "ORDER BY messages.sequence DESC" in page_sql
    assert "LIMIT" in page_sql


@pytest.mark.asyncio
async def test_get_messages_with_cursor_applies_strictly_older_filter():
    conv, user = _conv_and_user()
    anchor = _make_row(10, conversation_id=conv.id)
    rows_desc = [_make_row(s, conversation_id=conv.id) for s in (9, 8)]
    db = FakeDB(
        [
            FakeResult(scalar=conv),  # conversation lookup
            FakeResult(scalar=anchor),  # anchor message lookup
            FakeResult(items=rows_desc),  # message page fetch
            FakeResult(items=[]),  # feedback lookup
        ]
    )

    response = await get_conversation_messages(
        str(conv.id), before=str(anchor.id), limit=50, user=user, db=db
    )

    assert response.has_more is False
    assert [m.sequence for m in response.messages] == [8, 9]
    # The page query carries the (sequence, created_at, id) cursor chain.
    page_sql = _compile_pg(db.statements[2])
    assert "messages.sequence <" in page_sql
    assert "messages.created_at <" in page_sql
    assert "messages.id <" in page_sql


@pytest.mark.asyncio
async def test_get_messages_orders_equal_sequence_rows_by_tiebreakers():
    conv, user = _conv_and_user()
    # Three rows sharing sequence=0 (legacy default) — the DB returns them
    # newest-first by (created_at, id); the page must come back chronological.
    t0 = _BASE_TIME
    id_low = uuid.UUID(int=1)
    id_high = uuid.UUID(int=2)
    oldest = _make_row(0, created_at=t0, conversation_id=conv.id)
    mid = _make_row(
        0, created_at=t0 + timedelta(seconds=1), msg_id=id_low, conversation_id=conv.id
    )
    newest = _make_row(
        0, created_at=t0 + timedelta(seconds=1), msg_id=id_high, conversation_id=conv.id
    )
    db = FakeDB(
        [
            FakeResult(scalar=conv),
            FakeResult(items=[newest, mid, oldest]),
            FakeResult(items=[]),
        ]
    )

    response = await get_conversation_messages(
        str(conv.id), before=None, limit=10, user=user, db=db
    )

    assert [m.id for m in response.messages] == [
        str(oldest.id),
        str(mid.id),
        str(newest.id),
    ]


@pytest.mark.asyncio
async def test_get_messages_conversation_not_found():
    _conv, user = _conv_and_user()
    db = FakeDB([FakeResult(scalar=None)])

    with pytest.raises(HTTPException) as exc_info:
        await get_conversation_messages(
            str(uuid.uuid4()), before=None, limit=50, user=user, db=db
        )

    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_get_messages_cursor_not_in_conversation_is_404():
    conv, user = _conv_and_user()
    db = FakeDB(
        [
            FakeResult(scalar=conv),
            FakeResult(scalar=None),  # anchor not found in this conversation
        ]
    )

    with pytest.raises(HTTPException) as exc_info:
        await get_conversation_messages(
            str(conv.id), before=str(uuid.uuid4()), limit=50, user=user, db=db
        )

    assert exc_info.value.status_code == 404
    assert "Cursor message" in exc_info.value.detail


@pytest.mark.asyncio
async def test_get_messages_invalid_cursor_is_422():
    conv, user = _conv_and_user()
    db = FakeDB([FakeResult(scalar=conv)])

    with pytest.raises(HTTPException) as exc_info:
        await get_conversation_messages(
            str(conv.id), before="not-a-uuid", limit=50, user=user, db=db
        )

    assert exc_info.value.status_code == 422


# ---------------------------------------------------------------------------
# Hidden parent wakes count for nothing in a page
# ---------------------------------------------------------------------------


class _HistoryDb:
    """Serves the message queries from *history* (newest first) the way the database would.

    A page query keeps the rows older than every message its statement names
    (the ``before`` cursor) up to its ``LIMIT``, and leaves out the hidden
    wakes when the statement carries :func:`message_shown_clause` (the wake's
    metadata is one of the statement's parameters); a lookup by id
    finds that message. The conversation, when given, answers the handler's
    first query; anything else (feedback, agent names) is empty.
    """

    def __init__(self, history, conv=None):
        self.history = list(history)
        self._conv = conv
        self.pages = 0

    async def execute(self, statement):
        if self._conv is not None:
            conv, self._conv = self._conv, None
            return FakeResult(scalar=conv)
        params = statement.compile().params.values()
        ids = {value for value in params if isinstance(value, uuid.UUID)}
        named = [row for row in self.history if row.id in ids]
        entity = (getattr(statement, "column_descriptions", None) or [{}])[0].get("entity")
        if entity is Message and statement._order_by_clauses:
            self.pages += 1
            start = max((self.history.index(row) + 1 for row in named), default=0)
            older = self.history[start:]
            if PARENT_WAKE_METADATA in params:
                older = [row for row in older if row.metadata_json != PARENT_WAKE_METADATA]
            return _Rows(older[: statement._limit])
        return _Rows([], scalar=named[0] if named else None)


class _Rows(FakeResult):
    def __init__(self, items, scalar=None):
        super().__init__(scalar=scalar, items=items)

    def all(self):
        return self._items


def _history(conversation_id, *runs):
    """Rows newest first from *runs* given oldest first: ``("visible", n)`` or ``("hidden", n)``."""
    rows, sequence = [], 0
    for kind, count in runs:
        for _ in range(count):
            sequence += 1
            row = _make_row(sequence, conversation_id=conversation_id)
            if kind == "hidden":
                row.role, row.metadata_json = "user", PARENT_WAKE_METADATA
            rows.append(row)
    return rows[::-1]


def _seqs(page) -> list[int]:
    return [m.sequence if hasattr(m, "sequence") else int(m["content"].split()[1]) for m in page]


@pytest.mark.asyncio
async def test_a_page_counts_only_visible_messages_across_a_run_of_hidden_wakes():
    conv_id = uuid.uuid4()
    db = _HistoryDb(_history(conv_id, ("visible", 3), ("hidden", 60), ("visible", 5)))
    page, has_more = await read_visible_message_page(db, [], 5)
    assert _seqs(page) == [64, 65, 66, 67, 68] and has_more is True
    assert db.pages == 1, "the hidden wakes are left out by the query, not by reading past them"

    page, has_more = await read_visible_message_page(db, [message_cursor_filter(page[0])], 5)
    assert _seqs(page) == [1, 2, 3] and has_more is False


@pytest.mark.asyncio
async def test_history_older_than_a_page_made_only_of_hidden_wakes_is_not_more():
    db = _HistoryDb(_history(uuid.uuid4(), ("hidden", 60), ("visible", 5)))
    page, has_more = await read_visible_message_page(db, [], 5)
    assert _seqs(page) == [61, 62, 63, 64, 65] and has_more is False


@pytest.mark.asyncio
async def test_the_chat_panel_pages_past_a_run_of_hidden_wakes():
    from app.api.v1.platform import read_message_page

    conv, user = _conv_and_user()
    history = _history(conv.id, ("visible", 3), ("hidden", 60), ("visible", 5))
    db = _HistoryDb(history)

    first = await read_message_page(db, user, conv, before=None, limit=5, supports_artifacts=False)
    assert _seqs(first["messages"]) == [64, 65, 66, 67, 68] and first["has_more"] is True
    older = await read_message_page(
        db, user, conv, before=first["messages"][0]["id"], limit=5, supports_artifacts=False
    )
    assert _seqs(older["messages"]) == [1, 2, 3] and older["has_more"] is False


@pytest.mark.asyncio
async def test_the_chat_panel_restores_past_a_run_of_hidden_wakes():
    from app.api.v1.platform import restore_chat_session_window

    conv, user = _conv_and_user()
    conv = SimpleNamespace(**vars(conv), agent_id=None, original_agent_id=None, workspace_id=None, channel="web")
    db = _HistoryDb(_history(conv.id, ("visible", 3), ("hidden", 60)))

    restored, has_more = await restore_chat_session_window(db, user, conv, supports_artifacts=False)
    assert _seqs(restored.messages) == [1, 2, 3] and has_more is False


@pytest.mark.asyncio
async def test_the_rest_history_pages_past_a_run_of_hidden_wakes():
    conv, user = _conv_and_user()
    history = _history(conv.id, ("visible", 3), ("hidden", 60), ("visible", 5))

    first = await get_conversation_messages(str(conv.id), before=None, limit=5, user=user, db=_HistoryDb(history, conv))
    assert _seqs(first.messages) == [64, 65, 66, 67, 68] and first.has_more is True
    older = await get_conversation_messages(
        str(conv.id), before=first.messages[0].id, limit=5, user=user, db=_HistoryDb(history, conv)
    )
    assert _seqs(older.messages) == [1, 2, 3] and older.has_more is False
