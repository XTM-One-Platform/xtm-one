"""Workspaces in the embedded chat (``@filigran/chatbot``), on PostgreSQL.

The chat panel OpenCTI and OpenAEV embed groups its conversations by
workspace through ``/api/v1/platform/chat/workspaces`` and files one with
``PATCH /api/v1/platform/chat/sessions/{id}``. These routes run the REST
handlers that decide every rule, so what is pinned here is the embedded
chat's own boundary - only the caller's platform-channel conversations - and
that the rules reach the panel unchanged: the default is not deleted, an
archived workspace takes no conversation, another person's workspace answers
like an unknown one. The history lists each conversation's last message and
channel as the web chat's list does.

``integration``-marked and skips when PostgreSQL is unreachable.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.api import deps
from app.api.v1 import platform as platform_api
from app.api.v1 import platform_chat_workspaces
from app.api.v1.platform import PLATFORM_CHAT_CHANNEL
from app.core.database import get_db
from app.models.agent import Agent
from app.models.chat import Conversation, Message
from app.models.user import User
from app.models.workspace import Workspace
from app.services.workspaces import create_workspace, ensure_default_workspace
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration

BASE = "/api/v1/platform/chat"


@asynccontextmanager
async def _database():
    async with scratch_database(
        "pcws", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True
    ) as db, db.sessions() as session:
        yield session


@asynccontextmanager
async def _client(session, user: User) -> AsyncIterator[httpx.AsyncClient]:
    """The embedded chat's routers over one real session, as *user*."""
    api = FastAPI()
    api.include_router(platform_api.router, prefix="/api/v1/platform")
    api.include_router(platform_chat_workspaces.router, prefix="/api/v1/platform")
    api.dependency_overrides[deps.get_current_user] = lambda: user
    api.dependency_overrides[deps.require_agentic_ai_licensed] = lambda: None
    api.dependency_overrides[deps.require_chat_enabled] = lambda: None

    async def _get_db():
        yield session
        await session.commit()

    api.dependency_overrides[get_db] = _get_db
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api), base_url="http://test"
    ) as client:
        yield client


async def _user(session, name: str) -> User:
    user = User(
        id=uuid.uuid4(),
        email=f"{name}-{uuid.uuid4().hex[:6]}@example.com",
        full_name=name,
        hashed_password="x",
        role="user",
    )
    session.add(user)
    await session.flush()
    return user


async def _conversation(session, owner: User, *, channel: str = PLATFORM_CHAT_CHANNEL) -> Conversation:
    conv = Conversation(id=uuid.uuid4(), user_id=owner.id, channel=channel, title="Thread")
    session.add(conv)
    await session.flush()
    # The history menu lists only conversations that hold a message.
    session.add(Message(conversation_id=conv.id, role="user", content="hello"))
    await session.flush()
    return conv


async def test_the_panel_lists_creates_renames_and_deletes_workspaces() -> None:
    async with _database() as session:
        alice = await _user(session, "alice")
        await session.commit()
        async with _client(session, alice) as client:
            listed = await client.get(f"{BASE}/workspaces")
            assert listed.status_code == 200, listed.text
            rows = listed.json()["workspaces"]
            # The default is created on first use, as on the Workspaces page.
            assert [w["name"] for w in rows] == ["General"]
            assert rows[0]["is_own"] is True and rows[0]["is_default"] is True
            default_id = rows[0]["id"]

            created = await client.post(f"{BASE}/workspaces", json={"name": "Acme audit"})
            assert created.status_code == 201, created.text
            ws_id = created.json()["id"]
            assert created.json()["can_manage"] is True

            renamed = await client.patch(f"{BASE}/workspaces/{ws_id}", json={"name": "Acme Q3"})
            assert renamed.status_code == 200, renamed.text
            assert renamed.json()["name"] == "Acme Q3"

            refused = await client.delete(f"{BASE}/workspaces/{default_id}")
            assert refused.status_code == 400, refused.text
            assert "default workspace cannot be deleted" in refused.json()["detail"]

            conv = await _conversation(session, alice)
            filed = await client.patch(f"{BASE}/sessions/{conv.id}", json={"workspace_id": ws_id})
            assert filed.status_code == 200, filed.text

            deleted = await client.delete(f"{BASE}/workspaces/{ws_id}")
            assert deleted.status_code == 204, deleted.text
            assert await session.get(Workspace, uuid.UUID(ws_id)) is None
            # Its conversation stays, in no workspace: never moved to the default.
            history = (await client.get(f"{BASE}/sessions")).json()["conversations"]
            assert [(c["conversation_id"], c["workspace_id"]) for c in history] == [
                (str(conv.id), None)
            ]


async def test_a_platform_conversation_is_filed_listed_and_unfiled() -> None:
    async with _database() as session:
        alice = await _user(session, "alice")
        workspace = await create_workspace(session, user_id=alice.id, name="Research")
        conv = await _conversation(session, alice)
        await session.commit()
        async with _client(session, alice) as client:
            filed = await client.patch(
                f"{BASE}/sessions/{conv.id}", json={"workspace_id": str(workspace.id)}
            )
            assert filed.status_code == 200, filed.text
            assert filed.json() == {
                "conversation_id": str(conv.id),
                "title": "Thread",
                "workspace_id": str(workspace.id),
            }

            history = (await client.get(f"{BASE}/sessions")).json()["conversations"]
            assert [(c["conversation_id"], c["workspace_id"]) for c in history] == [
                (str(conv.id), str(workspace.id))
            ]

            renamed = await client.patch(f"{BASE}/sessions/{conv.id}", json={"title": "Renamed"})
            assert renamed.status_code == 200, renamed.text
            # A rename alone leaves the filing as it is.
            assert renamed.json()["workspace_id"] == str(workspace.id)

            unfiled = await client.patch(f"{BASE}/sessions/{conv.id}", json={"workspace_id": None})
            assert unfiled.status_code == 200, unfiled.text
            assert unfiled.json()["workspace_id"] is None

            empty = await client.patch(f"{BASE}/sessions/{conv.id}", json={})
            assert empty.status_code == 422, empty.text


async def test_only_the_callers_platform_conversations_are_reachable() -> None:
    async with _database() as session:
        alice = await _user(session, "alice")
        bob = await _user(session, "bob")
        workspace = await create_workspace(session, user_id=alice.id, name="Research")
        web = await _conversation(session, alice, channel="web")
        bobs = await _conversation(session, bob)
        await session.commit()
        async with _client(session, alice) as client:
            for conv in (web, bobs):
                missing = await client.patch(
                    f"{BASE}/sessions/{conv.id}", json={"workspace_id": str(workspace.id)}
                )
                assert missing.status_code == 404, missing.text
                renamed = await client.patch(f"{BASE}/sessions/{conv.id}", json={"title": "Mine now"})
                assert renamed.status_code == 404, renamed.text
            assert (await client.patch(f"{BASE}/sessions/not-a-uuid", json={"title": "x"})).status_code == 404
        await session.refresh(web)
        await session.refresh(bobs)
        assert web.workspace_id is None and bobs.workspace_id is None
        assert (web.title, bobs.title) == ("Thread", "Thread")


async def test_the_history_lists_each_conversations_last_message_and_channel() -> None:
    """The panel shows a row's last message under its title and sets it on a
    send: the list read next must carry it, as the web chat's list does
    (markers stripped, a background task's wake left out)."""
    async with _database() as session:
        alice = await _user(session, "alice")
        conv = await _conversation(session, alice)
        deck = uuid.uuid4()
        session.add(
            Message(conversation_id=conv.id, role="assistant", content=f"The deck is ready [[PRESENTATION:{deck}]]")
        )
        await session.flush()
        session.add(
            Message(
                conversation_id=conv.id,
                role="user",
                content="(woken)",
                metadata_json='{"background_task_wake": true}',
            )
        )
        await session.commit()
        async with _client(session, alice) as client:
            listed = (await client.get(f"{BASE}/sessions")).json()["conversations"]
        assert [(c["conversation_id"], c["last_message_preview"].strip(), c["channel"]) for c in listed] == [
            (str(conv.id), "The deck is ready", PLATFORM_CHAT_CHANNEL)
        ]


async def test_the_workspace_rules_reach_the_panel() -> None:
    async with _database() as session:
        alice = await _user(session, "alice")
        bob = await _user(session, "bob")
        archived = await create_workspace(session, user_id=alice.id, name="Old")
        archived.is_archived = True
        hidden = await create_workspace(session, user_id=bob.id, name="Bob")
        conv = await _conversation(session, alice)
        await session.commit()
        async with _client(session, alice) as client:
            refused = await client.patch(
                f"{BASE}/sessions/{conv.id}", json={"workspace_id": str(archived.id)}
            )
            assert refused.status_code == 400, refused.text
            assert refused.json()["detail"] == "That workspace is archived."
            unknown = await client.patch(
                f"{BASE}/sessions/{conv.id}", json={"workspace_id": str(hidden.id)}
            )
            assert unknown.status_code == 404, unknown.text
            listed = (await client.get(f"{BASE}/workspaces")).json()["workspaces"]
            names = {w["name"] for w in listed}
            assert "Old" not in names and "Bob" not in names
            assert (
                await client.patch(f"{BASE}/workspaces/{hidden.id}", json={"name": "Mine"})
            ).status_code == 404


async def test_a_new_session_can_be_created_inside_a_workspace() -> None:
    async with _database() as session:
        alice = await _user(session, "alice")
        session.add(
            Agent(
                id=uuid.uuid4(),
                name="Ariane",
                slug=f"ariane-{uuid.uuid4().hex[:6]}",
                is_company_managed=True,
                enabled=True,
            )
        )
        await ensure_default_workspace(session, alice.id)
        workspace = await create_workspace(session, user_id=alice.id, name="Research")
        await session.commit()
        async with _client(session, alice) as client:
            created = await client.post(f"{BASE}/sessions", json={"workspace_id": str(workspace.id)})
            assert created.status_code == 200, created.text
            assert created.json()["workspace_id"] == str(workspace.id)
            conv = await session.get(Conversation, uuid.UUID(created.json()["conversation_id"]))
            assert conv.workspace_id == workspace.id

            restored = await client.post(
                f"{BASE}/sessions", json={"conversation_id": str(conv.id)}
            )
            assert restored.status_code == 200, restored.text
            assert restored.json()["workspace_id"] == str(workspace.id)

            refused = await client.post(f"{BASE}/sessions", json={"workspace_id": str(uuid.uuid4())})
            assert refused.status_code == 404, refused.text


async def test_a_session_title_is_held_to_the_web_chats_constraints() -> None:
    """The body is `ConversationUpdate`: a blank or over-long title is a 422,
    never a 500, and a request that changes nothing writes nothing."""
    async with _database() as session:
        alice = await _user(session, "alice")
        conv = await _conversation(session, alice)
        await session.commit()
        stamp = conv.updated_at
        async with _client(session, alice) as client:
            for title in ("", "   ", "x" * 501):
                refused = await client.patch(f"{BASE}/sessions/{conv.id}", json={"title": title})
                assert refused.status_code == 422, (title[:10], refused.text)
            same = await client.patch(f"{BASE}/sessions/{conv.id}", json={"title": "Thread"})
            assert same.status_code == 200, same.text
            assert same.json() == {
                "conversation_id": str(conv.id),
                "title": "Thread",
                "workspace_id": None,
            }
            unfile_nothing = await client.patch(
                f"{BASE}/sessions/{conv.id}", json={"workspace_id": None}
            )
            assert unfile_nothing.status_code == 200
        await session.refresh(conv)
        assert conv.updated_at == stamp, "an unchanged request writes nothing"


async def test_a_new_session_is_refused_in_a_workspace_it_may_not_use() -> None:
    async with _database() as session:
        alice = await _user(session, "alice")
        bob = await _user(session, "bob")
        session.add(
            Agent(
                id=uuid.uuid4(),
                name="Ariane",
                slug=f"ariane-{uuid.uuid4().hex[:6]}",
                is_company_managed=True,
                enabled=True,
            )
        )
        read_only = await create_workspace(session, user_id=bob.id, name="Bob's reading room")
        read_only.shared_user_ids = [str(alice.id)]
        read_only.shared_user_permissions = {str(alice.id): "read_only"}
        archived = await create_workspace(session, user_id=alice.id, name="Old")
        archived.is_archived = True
        foreign = await create_workspace(session, user_id=bob.id, name="Bob's")
        await session.commit()
        async with _client(session, alice) as client:
            for workspace, code in ((read_only, 403), (archived, 400), (foreign, 404)):
                refused = await client.post(f"{BASE}/sessions", json={"workspace_id": str(workspace.id)})
                assert refused.status_code == code, (workspace.name, refused.text)
            listed = (await client.get(f"{BASE}/workspaces")).json()["workspaces"]
            shared = next(w for w in listed if w["id"] == str(read_only.id))
            assert (shared["is_own"], shared["can_manage"]) == (False, False)
            # The panel's view of a workspace, nothing of its sharing.
            assert set(shared) == {"id", "user_id", "name", "is_default", "is_archived", "can_manage", "is_own"}
        created = (
            await session.execute(select(Conversation).where(Conversation.user_id == alice.id))
        ).scalars().all()
        assert created == [], "no refused session left a conversation behind"
