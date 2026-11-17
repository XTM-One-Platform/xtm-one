"""The embedded chat's prompt picker and message feedback, against PostgreSQL.

A real database because what is under test IS the SQL: which prompts the
three-tier predicate lets through, in which order, and which message a
rating may land on. Both routes are driven through the real platform router,
one session per request, as the embedded chat (``@filigran/chatbot``) calls
them.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.models  # noqa: F401 - registers every mapper on Base.metadata
from app.api import deps
from app.api.v1 import platform as platform_api
from app.core.database import get_db
from app.models.chat import Conversation, Message, MessageFeedback
from app.models.group import Group, GroupMember
from app.models.prompt import Prompt
from app.models.user import User
from app.services import platform_chat_extras as extras
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration

_START = datetime(2026, 1, 1, tzinfo=UTC)
_NOT_FOUND = {"detail": "Message not found"}


@asynccontextmanager
async def _temp_database() -> AsyncIterator[async_sessionmaker]:
    async with scratch_database(
        "chatextras",
        tables=ALL_TABLES,
        extensions=("vector",),
        bind_app_sessions=False,
    ) as db:
        yield db.sessions


@asynccontextmanager
async def _client(sessionmaker: async_sessionmaker, user: User) -> AsyncIterator[httpx.AsyncClient]:
    """The real platform router, as *user*, on its own session per request."""
    app = FastAPI()
    app.include_router(platform_api.router, prefix="/api/v1/platform")

    async def _get_db():
        async with sessionmaker() as db:
            yield db

    app.dependency_overrides[deps.get_current_user] = lambda: user
    app.dependency_overrides[get_db] = _get_db
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def _users(sessionmaker, *names: str, admin: tuple[str, ...] = ()) -> dict[str, User]:
    async with sessionmaker() as session:
        users = {
            name: User(
                email=f"{name}-{uuid.uuid4().hex[:8]}@test.local",
                full_name=name,
                hashed_password="x",
                role="admin" if name in admin else "user",
            )
            for name in names
        }
        session.add_all(users.values())
        await session.commit()
        return users


# ---------------------------------------------------------------------------
# GET /platform/chat/prompts
# ---------------------------------------------------------------------------


async def _prompt_world(sessionmaker) -> dict[str, User]:
    """Prompts of every tier, created one second apart in this order.

    ``caller`` owns two (one hidden from chat) and belongs to ``team``;
    ``other`` shares one with the team, one with the caller directly, one
    hidden from chat with the caller, and keeps one private; the company
    publishes one, one hidden from chat and one narrowed to ``other``.
    """
    users = await _users(sessionmaker, "caller", "other", "admin", admin=("admin",))
    caller, other, admin = users["caller"], users["other"], users["admin"]
    async with sessionmaker() as session:
        team = Group(name=f"team-{uuid.uuid4().hex[:8]}")
        session.add(team)
        await session.flush()
        session.add(GroupMember(group_id=team.id, user_id=caller.id))
        specs = [
            ("mine", {"user_id": caller.id, "description": "my own"}),
            ("mine-hidden", {"user_id": caller.id, "hide_from_chat": True}),
            (
                "team-shared",
                {
                    "user_id": other.id,
                    "shared_group_ids": [str(team.id)],
                    "shared_group_permissions": {str(team.id): "read_only"},
                },
            ),
            (
                "user-shared",
                {
                    "user_id": other.id,
                    "shared_user_ids": [str(caller.id)],
                    "shared_user_permissions": {str(caller.id): "read_only"},
                },
            ),
            (
                "shared-hidden",
                {
                    "user_id": other.id,
                    "hide_from_chat": True,
                    "shared_user_ids": [str(caller.id)],
                    "shared_user_permissions": {str(caller.id): "read_only"},
                },
            ),
            ("theirs-private", {"user_id": other.id}),
            ("admin-own", {"user_id": admin.id}),
            ("company", {"user_id": None, "is_company_managed": True, "description": "for everyone"}),
            ("company-hidden", {"user_id": None, "is_company_managed": True, "hide_from_chat": True}),
            (
                "company-narrowed",
                {
                    "user_id": None,
                    "is_company_managed": True,
                    "access_restricted": True,
                    "shared_user_ids": [str(other.id)],
                    "shared_user_permissions": {str(other.id): "read_only"},
                },
            ),
        ]
        session.add_all(
            Prompt(
                name=name,
                content=f"content of {name}",
                created_at=_START + timedelta(seconds=position),
                **fields,
            )
            for position, (name, fields) in enumerate(specs)
        )
        await session.commit()
    return users


async def _listed(sessionmaker, user: User) -> list[dict]:
    async with _client(sessionmaker, user) as client:
        resp = await client.get("/api/v1/platform/chat/prompts")
    assert resp.status_code == 200, resp.text
    return resp.json()["prompts"]


async def test_the_picker_lists_own_shared_and_company_prompts_newest_first():
    async with _temp_database() as sessionmaker:
        users = await _prompt_world(sessionmaker)
        prompts = await _listed(sessionmaker, users["caller"])

        assert [p["title"] for p in prompts] == ["company", "user-shared", "team-shared", "mine"]
        by_title = {p["title"]: p for p in prompts}
        assert set(by_title["mine"]) == {"id", "title", "content", "description"}
        assert by_title["mine"]["content"] == "content of mine"
        assert by_title["mine"]["description"] == "my own"
        assert by_title["company"]["description"] == "for everyone"
        # An empty description is served as none, not as an empty string.
        assert by_title["team-shared"]["description"] is None
        async with sessionmaker() as session:
            mine = (await session.execute(select(Prompt).where(Prompt.name == "mine"))).scalar_one()
        assert by_title["mine"]["id"] == str(mine.id)


async def test_hidden_and_foreign_prompts_are_not_listed():
    """``hide_from_chat`` at every tier, another user's private prompt, a narrowed one."""
    async with _temp_database() as sessionmaker:
        users = await _prompt_world(sessionmaker)
        titles = {p["title"] for p in await _listed(sessionmaker, users["caller"])}
        assert titles.isdisjoint(
            {"mine-hidden", "shared-hidden", "company-hidden", "theirs-private", "admin-own", "company-narrowed"}
        )

        # The narrowed company prompt is for its audience, who sees it.
        titles = {p["title"] for p in await _listed(sessionmaker, users["other"])}
        assert {"company-narrowed", "theirs-private", "company"} <= titles


async def test_an_admin_gets_the_library_set_not_every_prompt():
    """No admin bypass on this external-facing surface."""
    async with _temp_database() as sessionmaker:
        users = await _prompt_world(sessionmaker)
        prompts = await _listed(sessionmaker, users["admin"])
        assert [p["title"] for p in prompts] == ["company", "admin-own"]


async def test_the_list_is_bounded(monkeypatch):
    monkeypatch.setattr(extras, "PLATFORM_CHAT_PROMPT_LIMIT", 2)
    async with _temp_database() as sessionmaker:
        users = await _prompt_world(sessionmaker)
        prompts = await _listed(sessionmaker, users["caller"])
        assert [p["title"] for p in prompts] == ["company", "user-shared"]


# ---------------------------------------------------------------------------
# POST / DELETE /platform/chat/conversations/{cid}/messages/{mid}/feedback
# ---------------------------------------------------------------------------


@dataclass
class _Thread:
    conversation_id: uuid.UUID
    question_id: uuid.UUID
    answer_id: uuid.UUID


async def _thread(sessionmaker, user: User, *, channel: str = platform_api.PLATFORM_CHAT_CHANNEL) -> _Thread:
    async with sessionmaker() as session:
        conv = Conversation(user_id=user.id, channel=channel, title="feedback")
        session.add(conv)
        await session.flush()
        question = Message(conversation_id=conv.id, role="user", content="question", channel=channel, sequence=1)
        answer = Message(conversation_id=conv.id, role="assistant", content="answer", channel=channel, sequence=2)
        session.add_all([question, answer])
        await session.commit()
        return _Thread(conv.id, question.id, answer.id)


def _url(conversation_id, message_id) -> str:
    return f"/api/v1/platform/chat/conversations/{conversation_id}/messages/{message_id}/feedback"


async def _ratings(sessionmaker, message_id: uuid.UUID) -> list[tuple[uuid.UUID, str, str | None]]:
    async with sessionmaker() as session:
        rows = await session.execute(
            select(MessageFeedback.user_id, MessageFeedback.rating, MessageFeedback.comment).where(
                MessageFeedback.message_id == message_id
            )
        )
        return [tuple(row) for row in rows.all()]


async def test_a_rating_is_set_changed_and_retracted():
    async with _temp_database() as sessionmaker:
        users = await _users(sessionmaker, "owner")
        owner = users["owner"]
        thread = await _thread(sessionmaker, owner)
        url = _url(thread.conversation_id, thread.answer_id)
        async with _client(sessionmaker, owner) as client:
            resp = await client.post(url, json={"rating": "positive", "comment": "  spot on  "})
            assert resp.status_code == 200, resp.text
            assert resp.json() == {"rating": "positive", "comment": "spot on"}
            assert await _ratings(sessionmaker, thread.answer_id) == [(owner.id, "positive", "spot on")]

            resp = await client.post(url, json={"rating": "negative", "comment": None})
            assert resp.status_code == 200, resp.text
            assert resp.json() == {"rating": "negative", "comment": None}
            assert await _ratings(sessionmaker, thread.answer_id) == [(owner.id, "negative", None)]

            resp = await client.post(url, json={"rating": "negative", "comment": "   "})
            assert resp.json() == {"rating": "negative", "comment": None}

            resp = await client.delete(url)
            assert resp.status_code == 204
            assert resp.content == b""
            assert await _ratings(sessionmaker, thread.answer_id) == []

            # A second retract (a double click, a retry) changes nothing and succeeds.
            resp = await client.delete(url)
            assert resp.status_code == 204
            assert await _ratings(sessionmaker, thread.answer_id) == []


async def test_a_rating_is_the_callers_own():
    """Retracting mine leaves another user's rating of the same answer alone."""
    async with _temp_database() as sessionmaker:
        users = await _users(sessionmaker, "owner", "other")
        owner, other = users["owner"], users["other"]
        thread = await _thread(sessionmaker, owner)
        async with sessionmaker() as session:
            session.add(MessageFeedback(message_id=thread.answer_id, user_id=other.id, rating="negative"))
            await session.commit()
        url = _url(thread.conversation_id, thread.answer_id)
        async with _client(sessionmaker, owner) as client:
            assert (await client.post(url, json={"rating": "positive"})).status_code == 200
            assert (await client.delete(url)).status_code == 204
        assert await _ratings(sessionmaker, thread.answer_id) == [(other.id, "negative", None)]


async def test_the_comment_is_bounded():
    async with _temp_database() as sessionmaker:
        users = await _users(sessionmaker, "owner")
        owner = users["owner"]
        thread = await _thread(sessionmaker, owner)
        url = _url(thread.conversation_id, thread.answer_id)
        async with _client(sessionmaker, owner) as client:
            resp = await client.post(url, json={"rating": "positive", "comment": "x" * 2001})
            assert resp.status_code == 422
            assert await _ratings(sessionmaker, thread.answer_id) == []

            resp = await client.post(url, json={"rating": "positive", "comment": "x" * 2000})
            assert resp.status_code == 200
            assert resp.json()["comment"] == "x" * 2000

            resp = await client.post(url, json={"rating": "neutral"})
            assert resp.status_code == 422


async def test_anything_but_the_callers_platform_answer_is_not_found():
    """Foreign, web-channel, user-role, unknown and malformed: one 404, nothing written.

    The embedded chat can only name what it restored or streamed; anything
    else answers exactly like an id that does not exist, so a caller cannot
    tell someone else's conversation from none.
    """
    async with _temp_database() as sessionmaker:
        users = await _users(sessionmaker, "owner", "stranger")
        owner, stranger = users["owner"], users["stranger"]
        mine = await _thread(sessionmaker, owner)
        web = await _thread(sessionmaker, owner, channel="web")
        theirs = await _thread(sessionmaker, stranger)
        unknown = uuid.uuid4()

        targets = {
            "stranger's conversation": (theirs.conversation_id, theirs.answer_id),
            "stranger's answer under my conversation": (mine.conversation_id, theirs.answer_id),
            "my answer under the stranger's conversation": (theirs.conversation_id, mine.answer_id),
            "web-channel conversation": (web.conversation_id, web.answer_id),
            "user-role message": (mine.conversation_id, mine.question_id),
            "unknown conversation": (unknown, mine.answer_id),
            "unknown message": (mine.conversation_id, unknown),
            "malformed conversation id": ("conv-1", mine.answer_id),
            "malformed message id": (mine.conversation_id, "msg-1"),
        }
        async with _client(sessionmaker, owner) as client:
            for label, (conversation_id, message_id) in targets.items():
                url = _url(conversation_id, message_id)
                resp = await client.post(url, json={"rating": "positive", "comment": "x"})
                assert (resp.status_code, resp.json()) == (404, _NOT_FOUND), label
                resp = await client.delete(url)
                assert (resp.status_code, resp.json()) == (404, _NOT_FOUND), label

        async with sessionmaker() as session:
            assert (await session.execute(select(func.count()).select_from(MessageFeedback))).scalar_one() == 0
