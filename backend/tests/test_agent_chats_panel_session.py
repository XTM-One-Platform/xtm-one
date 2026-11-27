"""The agent detail's read-only chat panel routes - ``uv run pytest``.

``POST /api/v1/agents/{id}/chats/sessions`` restores one of the agent's
conversations and ``GET /api/v1/agents/{id}/chats/conversations/{cid}/messages``
pages it, in the panel's shape, for exactly the readers
``GET /api/v1/agents/{id}/chats/{cid}`` admits (``can_view_agent_chats``):
403 for a viewer it refuses, 404 for a conversation of another agent, and
never a new conversation.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api.v1 import agent_chats
from app.api.v1 import platform as platform_api


class _Db:
    """Answers the conversation lookup with *conv*."""

    def __init__(self, conv):
        self.conv = conv

    async def execute(self, _stmt):
        conv = self.conv
        return SimpleNamespace(scalar_one_or_none=lambda: conv)


@pytest.fixture
def access(monkeypatch):
    state = SimpleNamespace(allowed=True, agent=SimpleNamespace(id=uuid.uuid4()), restored=[], paged=[])

    async def _groups(_db, _uid):
        return []

    async def _agent(_db, _uid, _agent_id, _groups, *, is_admin):
        return state.agent

    async def _restore(db, user, conv, *, supports_artifacts, keep_dashboards=False):
        # XTM One's own panel draws a dashboard's marker as the dashboard: always kept here.
        assert keep_dashboards is True
        state.restored.append((conv, supports_artifacts))
        session = platform_api.PlatformChatSessionResponse(
            conversation_id=str(conv.id),
            messages=[{"id": "m1", "role": "assistant", "content": "hello"}],
            agent_id=str(state.agent.id),
            agent_name="Analyst",
        )
        return session, True

    async def _page(db, user, conv, *, before, limit, supports_artifacts, keep_dashboards=False):
        assert keep_dashboards is True
        state.paged.append((conv, before, limit, supports_artifacts))
        return {"messages": [], "has_more": False}

    monkeypatch.setattr(agent_chats, "get_user_group_ids", _groups)
    monkeypatch.setattr(agent_chats, "get_user_agent", _agent)
    monkeypatch.setattr(agent_chats, "can_view_agent_chats", lambda _u, _a, _g: state.allowed)
    monkeypatch.setattr(platform_api, "restore_chat_session_window", _restore)
    monkeypatch.setattr(platform_api, "read_message_page", _page)
    return state


def _reader():
    return SimpleNamespace(id=uuid.uuid4(), is_admin=False)


def _conv():
    return SimpleNamespace(id=uuid.uuid4())


async def _restore(db, conversation_id: str | None):
    return await agent_chats.restore_agent_chat_panel_session(
        "agent",
        agent_chats.AgentChatPanelSessionRequest(conversation_id=conversation_id, supports_artifacts=True),
        user=_reader(),
        db=db,
    )


async def test_an_admitted_reader_restores_the_conversation_in_panel_shape(access):
    conv = _conv()

    session = await _restore(_Db(conv), str(conv.id))

    assert session.conversation_id == str(conv.id)
    assert session.agent_name == "Analyst"
    assert session.has_more is True
    assert [m["content"] for m in session.messages] == ["hello"]
    assert access.restored == [(conv, True)]


async def test_an_admitted_reader_pages_older_messages(access):
    conv = _conv()

    page = await agent_chats.list_agent_chat_panel_messages(
        "agent", str(conv.id), before="m1", limit=50, supports_artifacts=True, user=_reader(), db=_Db(conv)
    )

    assert page == {"messages": [], "has_more": False}
    assert access.paged == [(conv, "m1", 50, True)]


async def test_a_reader_the_agent_detail_refuses_is_403_on_both_routes(access):
    access.allowed = False
    conv = _conv()

    with pytest.raises(HTTPException) as restore:
        await _restore(_Db(conv), str(conv.id))
    with pytest.raises(HTTPException) as page:
        await agent_chats.list_agent_chat_panel_messages(
            "agent", str(conv.id), before=None, limit=50, supports_artifacts=False, user=_reader(), db=_Db(conv)
        )

    assert restore.value.status_code == page.value.status_code == 403
    assert access.restored == access.paged == []


async def test_a_conversation_the_agent_does_not_hold_is_404(access):
    with pytest.raises(HTTPException) as exc:
        await _restore(_Db(None), str(uuid.uuid4()))

    assert exc.value.status_code == 404
    assert access.restored == []


async def test_a_restore_without_a_conversation_never_starts_one(access):
    with pytest.raises(HTTPException) as exc:
        await _restore(_Db(_conv()), None)

    assert exc.value.status_code == 404
    assert access.restored == []


async def test_the_panel_agent_list_is_the_agent_itself(access):
    access.agent = SimpleNamespace(id=uuid.uuid4(), name="Writer", slug="writer", icon="Crown", description="x" * 500)

    agents = await agent_chats.list_agent_chat_panel_agents("agent", user=_reader(), db=_Db(None))

    # One entry, the mark and name the empty conversation greets with; never
    # the description (the empty state is short, UX retest 09a3805e F2).
    assert [a.model_dump() for a in agents] == [
        {"id": str(access.agent.id), "name": "Writer", "slug": "writer", "icon": "Crown"}
    ]


async def test_the_panel_agent_list_follows_the_chat_access_rule(access):
    access.allowed = False

    with pytest.raises(HTTPException) as exc:
        await agent_chats.list_agent_chat_panel_agents("agent", user=_reader(), db=_Db(None))

    assert exc.value.status_code == 403


def test_the_routes_are_served_under_the_agents_prefix():
    from app.main import application

    paths = {(route.path, method) for route in application.routes for method in getattr(route, "methods", ())}

    assert ("/api/v1/agents/{agent_id}/chats/sessions", "POST") in paths
    assert ("/api/v1/agents/{agent_id}/chats/conversations/{conversation_id}/messages", "GET") in paths
    assert ("/api/v1/agents/{agent_id}/chats/panel-agents", "GET") in paths


def test_panel_agents_is_not_taken_by_the_conversation_detail_route():
    from app.main import application

    order = [route.path for route in application.routes]

    assert order.index("/api/v1/agents/{agent_id}/chats/panel-agents") < order.index(
        "/api/v1/agents/{agent_id}/chats/{conversation_id}"
    )
