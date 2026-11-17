"""The web sidebar's clear-all archives what the sidebar lists, and nothing else.

``DELETE /api/v1/web/chat/sessions`` archives the caller's conversations the
web lists (``web_listable_clause``: web ones and Slack / Teams direct-message
threads), and only their sandbox workspaces are collected. An embedded chat's
conversation (channel ``api``) and a Slack group thread stay as they are.
``DELETE /api/v1/chat/conversations`` still archives every one.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

import app.models  # noqa: F401 - registers every mapper on Base.metadata
from app.api.v1 import chat as chat_api
from app.api.v1 import chat_web_stream
from app.models.chat import Conversation
from app.models.user import User
from app.services.sandbox_workspace import gc as workspace_gc
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration

#: (channel, channel_thread_id) of each seeded conversation, by name.
_THREADS = {
    "web": ("web", None),
    "embedded": ("api", None),
    "slack_group": ("slack", "C0123:1700000000.000100"),
    "slack_dm": ("slack", "D0123:1700000000.000100"),
}


async def _seed(sessions) -> tuple[User, User, dict[str, uuid.UUID]]:
    async with sessions() as session:
        user = User(email=f"clear-{uuid.uuid4().hex[:8]}@test.local", full_name="Clear", hashed_password="x")
        other = User(email=f"other-{uuid.uuid4().hex[:8]}@test.local", full_name="Other", hashed_password="x")
        session.add_all([user, other])
        await session.flush()
        convs = {
            name: Conversation(user_id=user.id, channel=channel, channel_thread_id=thread, title=name)
            for name, (channel, thread) in _THREADS.items()
        }
        convs["someone_elses"] = Conversation(user_id=other.id, channel="web", title="theirs")
        session.add_all(convs.values())
        await session.commit()
        return user, other, {name: conv.id for name, conv in convs.items()}


async def _archived(sessions, ids: dict[str, uuid.UUID]) -> set[str]:
    async with sessions() as session:
        rows = (
            await session.execute(
                select(Conversation.id).where(Conversation.id.in_(ids.values()), Conversation.is_archived.is_(True))
            )
        ).scalars()
        archived = set(rows)
    return {name for name, conv_id in ids.items() if conv_id in archived}


@pytest.fixture
def collected(monkeypatch) -> list[uuid.UUID]:
    ids: list[uuid.UUID] = []

    def _collect(conversation_ids, *, reason):
        ids.extend(conversation_ids)

    monkeypatch.setattr(workspace_gc, "collect_conversations_soon", _collect)
    return ids


async def test_the_web_clear_all_archives_only_what_the_sidebar_lists(collected):
    async with scratch_database("webclear", tables=ALL_TABLES, extensions=("vector",)) as db:
        user, _other, ids = await _seed(db.sessions)
        async with db.sessions() as session:
            await chat_web_stream.archive_all_web_chat_sessions(user=user, db=session)

        assert await _archived(db.sessions, ids) == {"web", "slack_dm"}
        assert set(collected) == {ids["web"], ids["slack_dm"]}


async def test_the_api_clear_all_still_archives_every_conversation(collected):
    async with scratch_database("webclear", tables=ALL_TABLES, extensions=("vector",)) as db:
        user, _other, ids = await _seed(db.sessions)
        async with db.sessions() as session:
            await chat_api.archive_all_conversations(user=user, db=session)

        mine = set(_THREADS)
        assert await _archived(db.sessions, ids) == mine
        assert set(collected) == {ids[name] for name in mine}
