"""Regression tests for the admin guard on the channel bot tool test endpoint.

Before the fix, ``POST /channel-bots/{id}/test-tool`` had no admin check — any
authenticated user could call it and exercise integration tools (Slack, Teams,
WhatsApp …) using another user's channel bot credentials.

The fix adds ``if not user.is_admin: raise 403`` before the bot is even loaded.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.v1 import channel_bots as channel_bots_api


class _FakeDB:
    async def execute(self, _stmt):
        return None


def _user(*, is_admin: bool):
    return SimpleNamespace(id=uuid.uuid4(), is_admin=is_admin, role="admin" if is_admin else "user")


@pytest.mark.asyncio
async def test_non_admin_receives_403():
    """Non-admin users must be rejected before any bot lookup occurs."""
    with pytest.raises(HTTPException) as exc_info:
        await channel_bots_api.test_channel_bot_tool(
            bot_id=str(uuid.uuid4()),
            body={},
            user=_user(is_admin=False),
            db=_FakeDB(),
        )
    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_admin_passes_guard(monkeypatch):
    """Admin users must pass the guard — 403 must not be raised.

    The bot is set to 'disconnected' so the endpoint returns a benign
    "not connected" error, keeping the test self-contained without needing
    live integration tools.
    """
    bot = SimpleNamespace(id=uuid.uuid4(), status="disconnected", name="bot", provider="slack")

    async def _get_bot(_db, _uid, _bid, **_kw):
        return bot

    monkeypatch.setattr(channel_bots_api, "get_user_channel_bot", _get_bot)

    result = await channel_bots_api.test_channel_bot_tool(
        bot_id=str(bot.id),
        body={"tool_name": "list_channels"},
        user=_user(is_admin=True),
        db=_FakeDB(),
    )
    assert result.get("status") == "error"
    assert "not connected" in result.get("message", "").lower()
