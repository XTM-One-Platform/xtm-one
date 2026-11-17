"""Who may download a file the public widget's MCP tools produced, on PostgreSQL.

Same scratch-database recipe as ``test_conversation_history_postgres.py``. A
real database because what is under test IS the SQL: the widget download
route and ``_widget_turn_files`` admit a row only when it is bound to THIS
web_embed conversation of THIS agent and owned by nobody, and the platform's
own ``authorize_file_access`` must keep such a row admins-only.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import app.models  # noqa: F401 - registers every mapper on Base.metadata
from app.api.v1 import public_chat
from app.models.agent import Agent
from app.models.chat import Conversation
from app.models.file_attachment import FileAttachment
from app.models.user import User
from app.services.file_access import authorize_file_access
from app.tools.base import FileContext
from tests.test_conversation_history_postgres import _temp_database

pytestmark = pytest.mark.integration

_BYTES = {"widget/chart.png": b"\x89PNG widget", "chat/report.pdf": b"%PDF user"}


async def _seed(session) -> SimpleNamespace:
    admin = User(id=uuid.uuid4(), email=f"admin-{uuid.uuid4().hex[:8]}@test.local", full_name="Admin", hashed_password="x", role="admin")
    member = User(id=uuid.uuid4(), email=f"member-{uuid.uuid4().hex[:8]}@test.local", full_name="Member", hashed_password="x")
    session.add_all([admin, member])
    await session.flush()

    agent_a = Agent(id=uuid.uuid4(), name="Widget A", user_id=member.id, web_enabled=True, enabled=True, web_token=f"tok-a-{uuid.uuid4().hex}")
    agent_b = Agent(id=uuid.uuid4(), name="Widget B", user_id=member.id, web_enabled=True, enabled=True, web_token=f"tok-b-{uuid.uuid4().hex}")
    session.add_all([agent_a, agent_b])
    await session.flush()

    def _widget_conv(agent: Agent) -> Conversation:
        return Conversation(id=uuid.uuid4(), user_id=None, channel="web_embed", agent_id=agent.id, title="Web chat")

    visitor, other_visitor, other_agent_visitor = _widget_conv(agent_a), _widget_conv(agent_a), _widget_conv(agent_b)
    member_chat = Conversation(id=uuid.uuid4(), user_id=member.id, channel="web", agent_id=agent_a.id, title="Member chat")
    session.add_all([visitor, other_visitor, other_agent_visitor, member_chat])
    await session.flush()

    widget_file = FileAttachment(
        id=uuid.uuid4(), user_id=None, conversation_id=visitor.id, filename="chart.png",
        content_type="image/png", size=11, s3_object_name="widget/chart.png", source="mcp",
    )
    member_file = FileAttachment(
        id=uuid.uuid4(), user_id=member.id, conversation_id=member_chat.id, filename="report.pdf",
        content_type="application/pdf", size=9, s3_object_name="chat/report.pdf", source="agent_generated",
    )
    orphan_file = FileAttachment(
        id=uuid.uuid4(), user_id=None, conversation_id=None, filename="legacy.png",
        content_type="image/png", size=3, s3_object_name="generated/legacy.png", source="mcp",
    )
    session.add_all([widget_file, member_file, orphan_file])
    await session.commit()
    return SimpleNamespace(
        admin=admin, member=member, agent_a=agent_a, agent_b=agent_b, visitor=visitor,
        other_visitor=other_visitor, other_agent_visitor=other_agent_visitor,
        widget_file=widget_file, member_file=member_file, orphan_file=orphan_file,
    )


@pytest.fixture
async def world(monkeypatch):
    async with _temp_database() as (sessionmaker, _engine):
        async with sessionmaker() as session:
            seeded = await _seed(session)

        async def _no_rate_limit(*_args, **_kwargs):
            return None

        async def _download(name):
            return _BYTES[name]

        monkeypatch.setattr(public_chat, "enforce_rate_limit", _no_rate_limit)
        monkeypatch.setattr("app.services.file_storage.file_storage.download", _download)
        seeded.sessionmaker = sessionmaker
        yield seeded


async def _download_answer(token: str, session_id: object, file_id: object):
    try:
        response = await public_chat.download_session_file(token, str(session_id), str(file_id), request=SimpleNamespace())
    except HTTPException as exc:
        return exc.status_code, exc.detail
    return response.status_code, response


async def test_the_visitor_downloads_the_file_of_their_own_session(world):
    status, response = await _download_answer(world.agent_a.web_token, world.visitor.id, world.widget_file.id)

    assert status == 200
    assert response.body == b"\x89PNG widget"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cache-control"] == "private, no-store"
    assert "etag" not in response.headers
    assert response.headers["access-control-allow-origin"] == "*"


async def test_everyone_else_gets_the_unknown_file_answer(world):
    unknown = await _download_answer(world.agent_a.web_token, world.visitor.id, uuid.uuid4())
    assert unknown == (404, public_chat.WIDGET_FILE_NOT_FOUND)

    # Another visitor of the same agent, a visitor of another agent, another
    # agent's token on this session, a platform user's file and an unscoped
    # legacy row reached through this session: all the same answer.
    assert await _download_answer(world.agent_a.web_token, world.other_visitor.id, world.widget_file.id) == unknown
    assert await _download_answer(world.agent_b.web_token, world.other_agent_visitor.id, world.widget_file.id) == unknown
    assert await _download_answer(world.agent_b.web_token, world.visitor.id, world.widget_file.id) == unknown
    assert await _download_answer(world.agent_a.web_token, world.visitor.id, world.member_file.id) == unknown
    assert await _download_answer(world.agent_a.web_token, world.visitor.id, world.orphan_file.id) == unknown


async def test_a_turn_shows_only_the_files_of_its_own_session(world):
    ctx = FileContext()
    for row in (world.widget_file, world.member_file, world.orphan_file):
        ctx.add(SimpleNamespace(file_id=str(row.id)))
    ctx.add(SimpleNamespace(file_id="not-a-uuid"))

    files = await public_chat._widget_turn_files(world.visitor.id, ctx)
    assert files == [
        {"file_id": str(world.widget_file.id), "filename": "chart.png", "content_type": "image/png", "size": 11}
    ]
    assert await public_chat._widget_turn_files(world.other_visitor.id, ctx) == []


async def test_on_the_platform_the_widget_file_stays_admins_only(world):
    async with world.sessionmaker() as session:
        row = await session.get(FileAttachment, world.widget_file.id)
        orphan = await session.get(FileAttachment, world.orphan_file.id)
        member = await session.get(User, world.member.id)
        admin = await session.get(User, world.admin.id)

        # The agent's own owner included: a widget conversation has no owner,
        # so it can never be shared or org-linked, and nothing else admits it.
        with pytest.raises(HTTPException) as refused:
            await authorize_file_access(row, member, session)
        with pytest.raises(HTTPException) as orphan_refused:
            await authorize_file_access(orphan, member, session)
        assert (refused.value.status_code, refused.value.detail) == (
            orphan_refused.value.status_code,
            orphan_refused.value.detail,
        )
        await authorize_file_access(row, admin, session)
