"""Which stored images a reader may reach, on PostgreSQL.

Same scratch-database recipe as ``test_public_chat_mcp_files_postgres.py``. A
real database because what is under test IS the SQL: the ids a channel reply
may sign (``channel_image_scope``) and who ``GET /api/v1/chat/images/{id}``
serves an owner-less image to.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import app.models  # noqa: F401 - registers every mapper on Base.metadata
from app.api.v1 import chat as chat_api
from app.channels.base import channel_image_scope
from app.models.agent import Agent
from app.models.chat import Conversation
from app.models.chat_image import ChatImage
from app.models.file_attachment import FileAttachment
from app.models.user import User
from tests.test_conversation_history_postgres import _temp_database

pytestmark = pytest.mark.integration


def _user(role: str = "user") -> User:
    return User(id=uuid.uuid4(), email=f"{role}-{uuid.uuid4().hex[:8]}@test.local", full_name=role, hashed_password="x", role=role)


def _file(conversation: Conversation | None, user: User | None = None) -> FileAttachment:
    return FileAttachment(
        id=uuid.uuid4(), user_id=user.id if user else None, conversation_id=conversation.id if conversation else None,
        filename="chart.png", content_type="image/png", size=3, s3_object_name=f"k/{uuid.uuid4().hex}", source="agent",
    )


def _image(conversation: Conversation | None, user: User | None = None) -> ChatImage:
    return ChatImage(
        id=uuid.uuid4(), user_id=user.id if user else None, conversation_id=conversation.id if conversation else None,
        prompt="a fox", model="m", image_data=b"\x89PNG", content_type="image/png",
    )


@pytest.fixture
async def world():
    async with _temp_database() as (sessionmaker, _engine):
        async with sessionmaker() as session:
            member, other, admin = _user(), _user(), _user("admin")
            session.add_all([member, other, admin])
            await session.flush()
            agent = Agent(id=uuid.uuid4(), name="Bot", user_id=member.id, enabled=True)
            session.add(agent)
            await session.flush()
            slack = Conversation(id=uuid.uuid4(), user_id=member.id, channel="slack", agent_id=agent.id, title="Slack")
            anonymous = Conversation(id=uuid.uuid4(), user_id=None, channel="slack", agent_id=agent.id, title="Anon")
            elsewhere = Conversation(id=uuid.uuid4(), user_id=other.id, channel="web", agent_id=agent.id, title="Other")
            session.add_all([slack, anonymous, elsewhere])
            await session.flush()
            rows = SimpleNamespace(
                file_here=_file(slack), image_here=_image(slack),
                file_of_member=_file(elsewhere, member), image_of_member=_image(None, member),
                file_foreign=_file(elsewhere, other), image_foreign=_image(elsewhere),
                image_anonymous=_image(anonymous), image_nowhere=_image(None),
            )
            session.add_all(vars(rows).values())
            await session.commit()
        yield SimpleNamespace(
            sessionmaker=sessionmaker, member=member, other=other, admin=admin,
            slack=slack, anonymous=anonymous, rows=rows,
        )


def _addresses(*rows) -> str:
    parts = []
    for row in rows:
        kind = "images" if isinstance(row, ChatImage) else "files"
        parts.append(f"![x](/api/v1/chat/{kind}/{row.id})")
    return " ".join(parts)


async def test_a_channel_reply_may_sign_only_what_its_conversation_or_its_user_holds(world):
    r = world.rows
    text = _addresses(r.file_here, r.image_here, r.file_of_member, r.image_of_member, r.file_foreign, r.image_foreign)
    async with world.sessionmaker() as db:
        scoped = await channel_image_scope(db, text, conversation_id=world.slack.id, user_id=world.member.id)
        anonymous = await channel_image_scope(db, text, conversation_id=world.anonymous.id, user_id=None)
        nothing = await channel_image_scope(db, text)

    assert scoped == {str(row.id) for row in (r.file_here, r.image_here, r.file_of_member, r.image_of_member)}
    # An anonymous sender's conversation holds none of these.
    assert anonymous == set()
    assert nothing == set()


async def _read(world, image, user):
    async with world.sessionmaker() as db:
        stored = await db.get(User, user.id)
        try:
            response = await chat_api.get_chat_image(str(image.id), user=stored, db=db)
        except HTTPException as exc:
            return exc.status_code
        return response.status_code


async def test_an_owner_less_image_is_read_under_its_conversation_rule(world):
    r = world.rows
    # Its conversation's owner and admins keep it; anyone else is refused.
    assert await _read(world, r.image_here, world.member) == 200
    assert await _read(world, r.image_here, world.admin) == 200
    assert await _read(world, r.image_here, world.other) == 404
    # An anonymous channel conversation's image: admins only, as its files.
    assert await _read(world, r.image_anonymous, world.other) == 404
    assert await _read(world, r.image_anonymous, world.admin) == 200
    # A row of no conversation and no one: admins only, as an orphan file.
    assert await _read(world, r.image_nowhere, world.member) == 404
    assert await _read(world, r.image_nowhere, world.admin) == 200
    # An owned image: its owner and admins, unchanged.
    assert await _read(world, r.image_of_member, world.member) == 200
    assert await _read(world, r.image_of_member, world.other) == 404
