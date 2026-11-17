from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy.exc import DBAPIError

from app.api.v1 import chat as chat_api
from app.models.chat import Conversation
from app.models.user import User


class _FakeScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _StatementTimeoutError(Exception):
    sqlstate = "57014"


def _statement_timeout_error() -> DBAPIError:
    return DBAPIError(
        "UPDATE conversations SET title = $1",
        {},
        _StatementTimeoutError("canceling statement due to statement timeout"),
        False,
    )


def _conversation(user_id):
    now = datetime.now(UTC)
    return Conversation(
        id=uuid4(),
        user_id=user_id,
        channel="web",
        title="Old title",
        is_archived=False,
        created_at=now,
        updated_at=now,
    )


class _FakeDB:
    def __init__(self, conv, flush_effects):
        self.conv = conv
        self.flush_effects = list(flush_effects)
        self.execute_calls = 0
        self.flush_calls = 0
        self.refresh_calls = 0
        self.rollback_calls = 0
        self.commit_calls = 0

    async def execute(self, _query):
        self.execute_calls += 1
        return _FakeScalarResult(self.conv)

    async def commit(self):
        self.commit_calls += 1

    async def flush(self):
        effect = self.flush_effects[self.flush_calls]
        self.flush_calls += 1
        if isinstance(effect, Exception):
            raise effect

    async def refresh(self, _conv):
        self.refresh_calls += 1

    async def rollback(self):
        self.rollback_calls += 1


async def _noop_sleep(_seconds):
    return None


@pytest.mark.asyncio
async def test_update_conversation_retries_statement_timeout(monkeypatch):
    user = User(
        id=uuid4(),
        email="user@example.com",
        full_name="Test User",
        hashed_password="x",
        role="user",
    )
    conv = _conversation(user.id)
    db = _FakeDB(conv, [_statement_timeout_error(), None])
    monkeypatch.setattr(chat_api.asyncio, "sleep", _noop_sleep)

    response = await chat_api.update_conversation(
        str(conv.id),
        chat_api.ConversationUpdate(title="  New title  "),
        user,
        db,
    )

    assert response.title == "New title"
    assert db.execute_calls == 2
    assert db.flush_calls == 2
    assert db.refresh_calls == 1
    assert db.rollback_calls == 1
    assert db.commit_calls == 1


@pytest.mark.asyncio
async def test_update_conversation_returns_409_after_repeated_statement_timeouts(monkeypatch):
    user = User(
        id=uuid4(),
        email="user@example.com",
        full_name="Test User",
        hashed_password="x",
        role="user",
    )
    conv = _conversation(user.id)
    db = _FakeDB(conv, [_statement_timeout_error(), _statement_timeout_error()])
    monkeypatch.setattr(chat_api.asyncio, "sleep", _noop_sleep)

    with pytest.raises(HTTPException) as exc_info:
        await chat_api.update_conversation(
            str(conv.id),
            chat_api.ConversationUpdate(title="Locked title"),
            user,
            db,
        )

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == "Conversation is busy right now. Please try renaming it again."
    assert db.execute_calls == 2
    assert db.flush_calls == 2
    assert db.refresh_calls == 0
    assert db.rollback_calls == 2
