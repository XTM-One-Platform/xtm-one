"""get_agent_account shows where listening stands: push expiry and the provider's read cursor."""

from __future__ import annotations

import uuid

import pytest

from app.api.v1.schemas.agent_account import AgentAccountResponse
from app.services.agent_accounts import state
from app.services.agent_accounts.providers import AccountProvider
from app.services.agent_accounts.providers import google_gmail as gmail
from app.services.builtin_tools.agent_accounts import _describe


class FakeRedis:
    def __init__(self, values: dict[str, bytes]):
        self.values = values
        self.asked: list[list[str]] = []

    async def mget(self, keys):
        self.asked.append(list(keys))
        return [self.values.get(k) for k in keys]


def _account(**extra) -> AgentAccountResponse:
    base = {
        "id": str(uuid.uuid4()),
        "user_id": str(uuid.uuid4()),
        "name": "Personal Gmail",
        "provider": "google_gmail",
        "provider_label": "Gmail",
        "mode": "delegate",
        "enabled": True,
        "response_mode": "shadow",
        "ai_label_enabled": False,
        "ai_label_preview": "",
        "ai_label_position": "suffix",
        "grace_period_seconds": 0,
        "owner_takeover_minutes": 0,
        "max_replies_per_hour": 20,
        "context_messages": 20,
        "rules": [],
        "reaction_feedback": {},
        "mailbox_actions": {},
        "transport": "push",
        "status": "active",
        "subscription_count": 1,
        "created_at": "2026-11-07T12:00:00+00:00",
        "updated_at": "2026-11-07T12:00:00+00:00",
    }
    base.update(extra)
    return AgentAccountResponse(**base)


@pytest.mark.asyncio
async def test_read_cursors_reads_every_account_in_one_call(monkeypatch) -> None:
    first, second = str(uuid.uuid4()), str(uuid.uuid4())
    redis = FakeRedis({state._key("cursor", first): b"98765"})

    async def _redis():
        return redis

    monkeypatch.setattr(state, "_redis", _redis)
    assert await state.read_cursors([first, second]) == {first: "98765"}
    assert len(redis.asked) == 1


@pytest.mark.asyncio
async def test_read_cursors_fails_open(monkeypatch) -> None:
    async def _no_redis():
        return None

    monkeypatch.setattr(state, "_redis", _no_redis)
    assert await state.read_cursors([str(uuid.uuid4())]) == {}

    class Broken:
        async def mget(self, keys):
            raise ConnectionError("down")

    async def _broken():
        return Broken()

    monkeypatch.setattr(state, "_redis", _broken)
    assert await state.read_cursors([str(uuid.uuid4())]) == {}
    assert await state.read_cursors([]) == {}


def test_gmail_names_its_cursor_the_last_history_id() -> None:
    assert gmail.GmailProvider.cursor_label == "last history id"
    assert AccountProvider.cursor_label == "read cursor"


def test_describe_shows_the_watch_expiry_and_the_last_history_id() -> None:
    account = _account(
        subscription_expires_at="2026-11-14T12:00:00+00:00",
        read_cursor="98765",
        read_cursor_label="last history id",
    )
    text = _describe(account, uuid.UUID(account.user_id))
    assert "Push subscription expires 2026-11-14T12:00:00+00:00 (renewed a day before)" in text
    assert "Last history id: 98765" in text


def test_describe_says_nothing_of_a_cursor_never_read() -> None:
    account = _account(subscription_count=0, transport="none")
    text = _describe(account, uuid.UUID(account.user_id))
    assert "Push subscription expires" not in text
    assert "history id" not in text
