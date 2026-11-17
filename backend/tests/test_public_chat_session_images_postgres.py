"""Who may see an image a public widget answer shows, on PostgreSQL.

Same scratch-database recipe as ``test_public_chat_mcp_files_postgres.py``. A
real database because what is under test IS the SQL: the widget's image route
serves a generated image only when it is bound to THIS web_embed conversation of
THIS agent and owned by nobody, and neither session route reaches into the
other's table.
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
from app.models.chat_image import ChatImage
from app.models.file_attachment import FileAttachment
from app.models.user import User
from tests.test_conversation_history_postgres import _temp_database

pytestmark = pytest.mark.integration

_PNG = b"\x89PNG widget fox"


def _image(conversation: Conversation | None, *, user: User | None = None, content_type: str = "image/png") -> ChatImage:
    return ChatImage(
        id=uuid.uuid4(),
        user_id=user.id if user else None,
        conversation_id=conversation.id if conversation else None,
        prompt="a fox",
        model="image-model",
        image_data=_PNG,
        content_type=content_type,
    )


async def _seed(session) -> SimpleNamespace:
    member = User(id=uuid.uuid4(), email=f"member-{uuid.uuid4().hex[:8]}@test.local", full_name="Member", hashed_password="x")
    session.add(member)
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

    widget_image = _image(visitor)
    other_image = _image(other_visitor)
    member_image = _image(member_chat, user=member)
    orphan_image = _image(None)
    svg_image = _image(visitor, content_type="image/svg+xml")
    widget_file = FileAttachment(
        id=uuid.uuid4(), user_id=None, conversation_id=visitor.id, filename="chart.png",
        content_type="image/png", size=11, s3_object_name="widget/chart.png", source="mcp",
    )
    session.add_all([widget_image, other_image, member_image, orphan_image, svg_image, widget_file])
    await session.commit()
    return SimpleNamespace(
        agent_a=agent_a, agent_b=agent_b, visitor=visitor, other_visitor=other_visitor,
        other_agent_visitor=other_agent_visitor, widget_image=widget_image, other_image=other_image,
        member_image=member_image, orphan_image=orphan_image, svg_image=svg_image, widget_file=widget_file,
    )


@pytest.fixture
async def world(monkeypatch):
    async with _temp_database() as (sessionmaker, _engine):
        async with sessionmaker() as session:
            seeded = await _seed(session)

        async def _no_rate_limit(*_args, **_kwargs):
            return None

        async def _download(name):
            return {"widget/chart.png": b"\x89PNG widget chart"}[name]

        monkeypatch.setattr(public_chat, "enforce_rate_limit", _no_rate_limit)
        monkeypatch.setattr("app.services.file_storage.file_storage.download", _download)
        yield seeded


async def _answer(route, token: str, session_id: object, item_id: object):
    try:
        response = await route(token, str(session_id), str(item_id), request=SimpleNamespace())
    except HTTPException as exc:
        return exc.status_code, exc.detail
    return response.status_code, response


async def test_the_visitor_sees_the_image_of_their_own_session(world):
    status, response = await _answer(
        public_chat.download_session_image, world.agent_a.web_token, world.visitor.id, world.widget_image.id
    )

    assert status == 200
    assert response.body == _PNG
    assert response.headers["content-type"] == "image/png"
    assert response.headers["content-disposition"] == "inline"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cache-control"] == "private, no-store"


async def test_everyone_else_gets_the_unknown_file_answer(world):
    image = public_chat.download_session_image
    unknown = await _answer(image, world.agent_a.web_token, world.visitor.id, uuid.uuid4())
    assert unknown == (404, public_chat.WIDGET_FILE_NOT_FOUND)

    # Another visitor of the same agent, a visitor of another agent, another
    # agent's token on this session, a platform user's image, an image of
    # nobody's conversation and an image no browser would paint as one.
    assert await _answer(image, world.agent_a.web_token, world.other_visitor.id, world.widget_image.id) == unknown
    assert await _answer(image, world.agent_a.web_token, world.visitor.id, world.other_image.id) == unknown
    assert await _answer(image, world.agent_b.web_token, world.other_agent_visitor.id, world.widget_image.id) == unknown
    assert await _answer(image, world.agent_b.web_token, world.visitor.id, world.widget_image.id) == unknown
    assert await _answer(image, world.agent_a.web_token, world.visitor.id, world.member_image.id) == unknown
    assert await _answer(image, world.agent_a.web_token, world.visitor.id, world.orphan_image.id) == unknown
    assert await _answer(image, world.agent_a.web_token, world.visitor.id, world.svg_image.id) == unknown


async def test_neither_session_route_reaches_into_the_other_table(world):
    unknown = (404, public_chat.WIDGET_FILE_NOT_FOUND)
    token, session_id = world.agent_a.web_token, world.visitor.id

    assert await _answer(public_chat.download_session_image, token, session_id, world.widget_file.id) == unknown
    assert await _answer(public_chat.download_session_file, token, session_id, world.widget_image.id) == unknown
    # Each still serves its own row to the same session.
    status, _ = await _answer(public_chat.download_session_file, token, session_id, world.widget_file.id)
    assert status == 200
