"""Regression tests for the agent visibility check on conversation creation.

Before the fix, any agent UUID could be supplied to ``POST /conversations``
regardless of ownership, exposing the existence of private agents and allowing
conversations to be started against agents the requesting user has no access to.

The fix wires ``get_user_agent`` (three-tier visibility) into
``create_conversation`` so invisible agents return 404 — consistent with every
other endpoint that looks up an agent by ID.
"""

from __future__ import annotations

import contextlib
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import app.api.v1.helpers as helpers_module
from app.api.v1 import chat as chat_api


class _FakeDB:
    def __init__(self):
        self.added: list = []

    async def execute(self, _stmt):
        return None

    async def flush(self):
        pass

    def add(self, obj):
        self.added.append(obj)


def _user():
    return SimpleNamespace(id=uuid.uuid4(), is_admin=False, role="user")


@pytest.mark.asyncio
async def test_invisible_agent_returns_404(monkeypatch):
    """A non-visible agent UUID must yield 404 — not start a conversation."""
    async def _get_user_group_ids(_db, _uid):
        return []

    async def _get_user_agent(_db, _uid, _aid, _groups, **_kw):
        raise HTTPException(status_code=404, detail="Agent not found")

    monkeypatch.setattr(helpers_module, "get_user_group_ids", _get_user_group_ids)
    monkeypatch.setattr(helpers_module, "get_user_agent", _get_user_agent)

    body = chat_api.ConversationCreate(agent_id=str(uuid.uuid4()))
    with pytest.raises(HTTPException) as exc_info:
        await chat_api.create_conversation(body=body, user=_user(), db=_FakeDB(), _chat_gate=None)

    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_visible_agent_stages_conversation(monkeypatch):
    """A visible (own / company-managed) agent must stage the conversation for insert.

    The fake DB does not populate server-default datetime fields, so the final
    ConversationResponse construction raises AttributeError — that expected
    side-effect is suppressed; what matters is the conversation was added
    to the session before it.
    """
    agent_id = str(uuid.uuid4())
    agent = SimpleNamespace(id=uuid.UUID(agent_id), enabled=True)

    async def _get_user_group_ids(_db, _uid):
        return []

    async def _get_user_agent(_db, _uid, _aid, _groups, **_kw):
        return agent

    monkeypatch.setattr(helpers_module, "get_user_group_ids", _get_user_group_ids)
    monkeypatch.setattr(helpers_module, "get_user_agent", _get_user_agent)

    body = chat_api.ConversationCreate(agent_id=agent_id)
    fake_db = _FakeDB()
    with contextlib.suppress(AttributeError):
        await chat_api.create_conversation(body=body, user=_user(), db=fake_db, _chat_gate=None)

    assert len(fake_db.added) == 1, "Conversation should be staged for insert"


@pytest.mark.asyncio
async def test_disabled_agent_returns_400(monkeypatch):
    """A disabled but visible agent must yield 400, not start a conversation."""
    agent_id = str(uuid.uuid4())
    agent = SimpleNamespace(id=uuid.UUID(agent_id), enabled=False)

    async def _get_user_group_ids(_db, _uid):
        return []

    async def _get_user_agent(_db, _uid, _aid, _groups, **_kw):
        return agent

    monkeypatch.setattr(helpers_module, "get_user_group_ids", _get_user_group_ids)
    monkeypatch.setattr(helpers_module, "get_user_agent", _get_user_agent)

    body = chat_api.ConversationCreate(agent_id=agent_id)
    with pytest.raises(HTTPException) as exc_info:
        await chat_api.create_conversation(body=body, user=_user(), db=_FakeDB(), _chat_gate=None)

    assert exc_info.value.status_code == 400
    assert "disabled" in exc_info.value.detail.lower()
