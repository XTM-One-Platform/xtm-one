"""Conversations and knowledge bases bound to a workspace.

The container side of a workspace: a conversation may be filed under one, a
knowledge base may be bound to one, and deleting the workspace must do the
right thing to both — which is not the same thing for each.

The asymmetry is the point, and it is what these tests are really about:

* a **conversation** loses nothing by losing its workspace — the thread and
  every message survive, unfiled, which is a normal state — so the FK is
  ``SET NULL`` and the delete unfiles them, never moving them to a default;
* a **knowledge base** is not owned by the workspace at all, so a delete must
  remove the binding row and touch nothing else. A workspace taking somebody's
  corpus with it on the way out would be the real failure.

Nobody's default receives them: not the deleter's (on a shared workspace that
would move somebody's threads where they cannot see them), and not their own
owner's either (a default is not a choice they made for that thread).
"""

from __future__ import annotations

import os
import urllib.parse
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, OperationalError, ProgrammingError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.models  # noqa: F401 — registers every mapper
from app.api import deps
from app.api.v1 import chat as chat_api
from app.api.v1 import workspaces as workspaces_api
from app.core.database import Base, get_db
from app.models.chat import Conversation
from app.models.knowledge_base import KnowledgeBase
from app.models.user import User
from app.models.workspace import Workspace, WorkspaceKnowledgeBase
from app.services.workspaces import (
    create_workspace,
    ensure_default_workspace,
    unfile_workspace_conversations,
)

# The integration job is the one CI runs with PostgreSQL: unmarked, every test
# below skips in the unit shards and none of them ever runs.
pytestmark = pytest.mark.integration


def _base_url() -> str:
    return os.environ.get(
        "DATABASE_URL",
        "postgresql+asyncpg://copilot:copilot@localhost:5432/copilot",
    )


@asynccontextmanager
async def _temp_database() -> AsyncIterator[async_sessionmaker]:
    parsed = urllib.parse.urlparse(_base_url())
    admin_url = parsed._replace(path="/postgres").geturl()
    db_name = f"wsconv_{uuid.uuid4().hex[:12]}"
    test_url = parsed._replace(path=f"/{db_name}").geturl()

    admin_engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        async with admin_engine.connect() as conn:
            await conn.execute(text(f'CREATE DATABASE "{db_name}"'))
    except (DBAPIError, OperationalError, ProgrammingError) as exc:
        orig = getattr(exc, "orig", None)
        pgcode = getattr(orig, "pgcode", None) or getattr(orig, "sqlstate", None)
        if pgcode == "42501":
            pytest.skip("DATABASE_URL user requires CREATEDB privilege")
        raise
    except Exception as exc:  # pragma: no cover — no PG in this environment
        pytest.skip(f"Cannot connect to PostgreSQL: {exc}")
    finally:
        await admin_engine.dispose()

    engine = create_async_engine(test_url)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            await conn.run_sync(Base.metadata.create_all)
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()
        cleanup = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
        try:
            async with cleanup.connect() as conn:
                await conn.execute(
                    text(
                        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                        "WHERE datname = :db AND pid <> pg_backend_pid()"
                    ),
                    {"db": db_name},
                )
                await conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}"'))
        finally:
            await cleanup.dispose()


async def _make_user(session, *, email: str, is_admin: bool = False) -> User:
    user = User(
        id=uuid.uuid4(),
        email=email,
        full_name=email.split("@")[0],
        hashed_password="x",
        role="admin" if is_admin else "user",
    )
    session.add(user)
    await session.flush()
    return user


async def _make_kb(session, *, owner: User, name: str) -> KnowledgeBase:
    kb = KnowledgeBase(id=uuid.uuid4(), user_id=owner.id, name=name)
    session.add(kb)
    await session.flush()
    return kb


@asynccontextmanager
async def _client(session, user: User) -> AsyncIterator[httpx.AsyncClient]:
    """Both routers over one real session, as the given user.

    An in-loop ASGI client, not ``TestClient``: the latter drives the app on
    its own event loop in a worker thread while the asyncpg connection behind
    ``session`` belongs to this one.
    """
    api = FastAPI()
    api.include_router(workspaces_api.router, prefix="/api/v1/workspaces")
    api.include_router(chat_api.router, prefix="/api/v1/chat")
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


# ── Filing a conversation ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_conversation_can_be_filed_and_listed_by_workspace() -> None:
    async with _temp_database() as sessions, sessions() as session:
        user = await _make_user(session, email="filer@example.com")
        await session.commit()
        async with _client(session, user) as client:
            ws_id = (
                await client.post("/api/v1/workspaces", json={"name": "Research"})
            ).json()["id"]

            filed = await client.post(
                "/api/v1/chat/conversations",
                json={"title": "Filed", "workspace_id": ws_id},
            )
            assert filed.status_code == 201, filed.text
            assert filed.json()["workspace_id"] == ws_id

            unfiled = await client.post(
                "/api/v1/chat/conversations", json={"title": "Unfiled"}
            )
            assert unfiled.status_code == 201
            assert unfiled.json()["workspace_id"] is None

            in_ws = await client.get(
                "/api/v1/chat/conversations", params={"workspace_id": ws_id}
            )
            assert in_ws.status_code == 200, in_ws.text
            titles = {c["title"] for c in in_ws.json()}
            assert titles == {"Filed"}

            everything = {
                c["title"] for c in (await client.get("/api/v1/chat/conversations")).json()
            }
            assert {"Filed", "Unfiled"} <= everything


@pytest.mark.asyncio
async def test_a_new_conversation_is_not_auto_filed() -> None:
    """"Unfiled" must mean one thing, not two.

    Defaulting new conversations into the caller's default workspace would
    make an absent ``workspace_id`` mean "predates workspaces" on old rows and
    nothing at all on new ones.
    """
    async with _temp_database() as sessions, sessions() as session:
        user = await _make_user(session, email="plain@example.com")
        await session.commit()
        async with _client(session, user) as client:
            # Materialise the default so it exists and could have been chosen.
            await client.get("/api/v1/workspaces")
            created = await client.post(
                "/api/v1/chat/conversations", json={"title": "Plain"}
            )
            assert created.json()["workspace_id"] is None


@pytest.mark.asyncio
async def test_filing_into_someone_elses_workspace_is_refused() -> None:
    """A known UUID must not be enough to put a thread in someone's container."""
    async with _temp_database() as sessions, sessions() as session:
        alice = await _make_user(session, email="alice@example.com")
        bob = await _make_user(session, email="bob@example.com")
        bobs = await create_workspace(session, user_id=bob.id, name="Bob's")
        await session.commit()

        async with _client(session, alice) as client:
            refused = await client.post(
                "/api/v1/chat/conversations",
                json={"title": "Sneaky", "workspace_id": str(bobs.id)},
            )
            assert refused.status_code in (403, 404), refused.text


def _share(workspace: Workspace, user: User, tier: str) -> None:
    workspace.shared_user_ids = [*(workspace.shared_user_ids or []), str(user.id)]
    workspace.shared_user_permissions = {
        **(workspace.shared_user_permissions or {}),
        str(user.id): tier,
    }


@pytest.mark.asyncio
async def test_the_workspace_filter_is_own_scope_and_parsed() -> None:
    """Refused, not ignored, with the shared list; a bad id is a 422."""
    async with _temp_database() as sessions, sessions() as session:
        user = await _make_user(session, email="filter@example.com")
        await session.commit()
        async with _client(session, user) as client:
            ws_id = (
                await client.post("/api/v1/workspaces", json={"name": "W"})
            ).json()["id"]
            shared = await client.get(
                "/api/v1/chat/conversations",
                params={"workspace_id": ws_id, "scope": "shared"},
            )
            assert shared.status_code == 422, shared.text
            malformed = await client.get(
                "/api/v1/chat/conversations", params={"workspace_id": "nope"}
            )
            assert malformed.status_code == 422, malformed.text


@pytest.mark.asyncio
async def test_the_workspace_filter_lists_only_the_callers_threads() -> None:
    """Another member's thread in the same shared workspace is theirs, not ours."""
    async with _temp_database() as sessions, sessions() as session:
        alice = await _make_user(session, email="alice@example.com")
        bob = await _make_user(session, email="bob@example.com")
        shared = await create_workspace(session, user_id=alice.id, name="Team")
        _share(shared, bob, "config")
        await session.commit()

        async with _client(session, bob) as client:
            filed = await client.post(
                "/api/v1/chat/conversations",
                json={"title": "Bob's", "workspace_id": str(shared.id)},
            )
            assert filed.status_code == 201, filed.text
        async with _client(session, alice) as client:
            await client.post(
                "/api/v1/chat/conversations",
                json={"title": "Alice's", "workspace_id": str(shared.id)},
            )
            listed = await client.get(
                "/api/v1/chat/conversations", params={"workspace_id": str(shared.id)}
            )
            assert {c["title"] for c in listed.json()} == {"Alice's"}


@pytest.mark.asyncio
async def test_a_read_only_grantee_reads_the_bindings_but_cannot_file() -> None:
    """Reading takes visibility; filing takes the manage right."""
    async with _temp_database() as sessions, sessions() as session:
        alice = await _make_user(session, email="alice@example.com")
        bob = await _make_user(session, email="bob@example.com")
        kb = await _make_kb(session, owner=alice, name="Alice's Public")
        kb.is_company_managed = True
        shared = await create_workspace(session, user_id=alice.id, name="Team")
        _share(shared, bob, "read_only")
        session.add(
            WorkspaceKnowledgeBase(
                id=uuid.uuid4(), workspace_id=shared.id, knowledge_base_id=kb.id
            )
        )
        await session.commit()

        async with _client(session, bob) as client:
            bindings = await client.get(f"/api/v1/workspaces/{shared.id}/knowledge-bases")
            assert bindings.status_code == 200, bindings.text
            assert [r["name"] for r in bindings.json()] == ["Alice's Public"]

            refused = await client.post(
                "/api/v1/chat/conversations",
                json={"title": "Nope", "workspace_id": str(shared.id)},
            )
            assert refused.status_code == 403, refused.text


@pytest.mark.asyncio
async def test_a_binding_the_reader_cannot_see_is_neither_shown_nor_dropped() -> None:
    """Binding a private corpus into a shared workspace does not publish it.

    Bob, a config grantee, binds his private knowledge base into Alice's
    workspace. Alice is not shown its name, and her replace with the list she
    was shown keeps it bound instead of unbinding what she could never name.
    """
    async with _temp_database() as sessions, sessions() as session:
        alice = await _make_user(session, email="alice@example.com")
        bob = await _make_user(session, email="bob@example.com")
        alices_kb = await _make_kb(session, owner=alice, name="Alice's Corpus")
        bobs_kb = await _make_kb(session, owner=bob, name="Bob's Private Corpus")
        shared = await create_workspace(session, user_id=alice.id, name="Team")
        _share(shared, bob, "config")
        await session.commit()
        url = f"/api/v1/workspaces/{shared.id}/knowledge-bases"

        async with _client(session, bob) as client:
            bound = await client.put(url, json={"knowledge_base_ids": [str(bobs_kb.id)]})
            assert bound.status_code == 200, bound.text

        async with _client(session, alice) as client:
            shown = await client.get(url)
            assert shown.json() == []
            replaced = await client.put(
                url, json={"knowledge_base_ids": [str(alices_kb.id)]}
            )
            assert replaced.status_code == 200, replaced.text
            assert [r["name"] for r in replaced.json()] == ["Alice's Corpus"]

        async with _client(session, bob) as client:
            # Bob can only see his own corpus of the two.
            assert [r["name"] for r in (await client.get(url)).json()] == [
                "Bob's Private Corpus"
            ]


@pytest.mark.asyncio
async def test_filing_into_an_archived_workspace_is_refused() -> None:
    async with _temp_database() as sessions, sessions() as session:
        user = await _make_user(session, email="archiver@example.com")
        await session.commit()
        async with _client(session, user) as client:
            ws_id = (
                await client.post("/api/v1/workspaces", json={"name": "Retired"})
            ).json()["id"]
            await client.put(
                f"/api/v1/workspaces/{ws_id}", json={"is_archived": True}
            )
            refused = await client.post(
                "/api/v1/chat/conversations",
                json={"title": "Nope", "workspace_id": ws_id},
            )
            assert refused.status_code == 400, refused.text


# ── Deleting a workspace ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_deleting_a_workspace_leaves_its_conversations_unfiled() -> None:
    async with _temp_database() as sessions, sessions() as session:
        user = await _make_user(session, email="mover@example.com")
        await session.commit()
        async with _client(session, user) as client:
            assert (await client.get("/api/v1/workspaces")).json()[0]["is_default"] is True
            ws_id = (
                await client.post("/api/v1/workspaces", json={"name": "Doomed"})
            ).json()["id"]
            conv_id = (
                await client.post(
                    "/api/v1/chat/conversations",
                    json={"title": "Survivor", "workspace_id": ws_id},
                )
            ).json()["id"]

            assert (await client.delete(f"/api/v1/workspaces/{ws_id}")).status_code == 204

            listed = {c["id"]: c for c in (await client.get("/api/v1/chat/conversations")).json()}
            assert conv_id in listed, "the conversation must survive its workspace"
            assert listed[conv_id]["workspace_id"] is None, (
                "a conversation whose workspace is deleted is unfiled, never "
                "moved to the default workspace"
            )


@pytest.mark.asyncio
async def test_unfiling_reaches_every_owner_and_fills_no_default() -> None:
    """A shared workspace's threads, whoever owns them, end up in no workspace.

    Neither the deleter's default nor each owner's own receives them.
    Exercised at the service level because it is the service that decides.
    """
    async with _temp_database() as sessions, sessions() as session:
        alice = await _make_user(session, email="alice@example.com")
        bob = await _make_user(session, email="bob@example.com")
        shared = await create_workspace(session, user_id=alice.id, name="Shared")
        other = await create_workspace(session, user_id=alice.id, name="Other")
        await ensure_default_workspace(session, alice.id)
        await ensure_default_workspace(session, bob.id)

        bobs_thread = Conversation(
            id=uuid.uuid4(),
            user_id=bob.id,
            channel="web",
            title="Bob's",
            workspace_id=shared.id,
        )
        alices_thread = Conversation(
            id=uuid.uuid4(),
            user_id=alice.id,
            channel="web",
            title="Alice's",
            workspace_id=shared.id,
        )
        elsewhere = Conversation(
            id=uuid.uuid4(),
            user_id=alice.id,
            channel="web",
            title="Elsewhere",
            workspace_id=other.id,
        )
        session.add_all([bobs_thread, alices_thread, elsewhere])
        await session.commit()

        unfiled = await unfile_workspace_conversations(session, shared)
        await session.commit()
        assert unfiled == 2

        await session.refresh(bobs_thread)
        await session.refresh(alices_thread)
        await session.refresh(elsewhere)
        assert bobs_thread.workspace_id is None
        assert alices_thread.workspace_id is None
        assert elsewhere.workspace_id == other.id, "another workspace's threads are not touched"


@pytest.mark.asyncio
async def test_deleting_a_workspace_never_deletes_a_bound_knowledge_base() -> None:
    """The binding is not ownership.

    The join row goes; the corpus stays. A workspace taking somebody's
    knowledge base with it would be the failure this asserts against.
    """
    async with _temp_database() as sessions, sessions() as session:
        user = await _make_user(session, email="binder@example.com")
        kb = await _make_kb(session, owner=user, name="Reports")
        await session.commit()
        async with _client(session, user) as client:
            ws_id = (
                await client.post("/api/v1/workspaces", json={"name": "Doomed"})
            ).json()["id"]
            bound = await client.put(
                f"/api/v1/workspaces/{ws_id}/knowledge-bases",
                json={"knowledge_base_ids": [str(kb.id)]},
            )
            assert bound.status_code == 200, bound.text

            assert (await client.delete(f"/api/v1/workspaces/{ws_id}")).status_code == 204

        assert await session.get(KnowledgeBase, kb.id) is not None
        remaining = (
            (
                await session.execute(
                    select(WorkspaceKnowledgeBase).where(
                        WorkspaceKnowledgeBase.knowledge_base_id == kb.id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert remaining == []


# ── Knowledge-base bindings ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_binding_replaces_rather_than_accumulates() -> None:
    async with _temp_database() as sessions, sessions() as session:
        user = await _make_user(session, email="replacer@example.com")
        one = await _make_kb(session, owner=user, name="One")
        two = await _make_kb(session, owner=user, name="Two")
        await session.commit()
        async with _client(session, user) as client:
            ws_id = (
                await client.post("/api/v1/workspaces", json={"name": "W"})
            ).json()["id"]
            url = f"/api/v1/workspaces/{ws_id}/knowledge-bases"

            first = await client.put(url, json={"knowledge_base_ids": [str(one.id)]})
            assert [r["name"] for r in first.json()] == ["One"]

            # Replacing with a different set unbinds the first.
            second = await client.put(url, json={"knowledge_base_ids": [str(two.id)]})
            assert [r["name"] for r in second.json()] == ["Two"]

            # Re-sending the same set is idempotent, not a duplicate row.
            again = await client.put(url, json={"knowledge_base_ids": [str(two.id)]})
            assert [r["name"] for r in again.json()] == ["Two"]

            cleared = await client.put(url, json={"knowledge_base_ids": []})
            assert cleared.json() == []
            assert (await client.get(url)).json() == []


@pytest.mark.asyncio
async def test_binding_a_knowledge_base_you_cannot_see_is_refused() -> None:
    """Otherwise binding becomes a way to name a corpus you were never shown."""
    async with _temp_database() as sessions, sessions() as session:
        alice = await _make_user(session, email="alice@example.com")
        bob = await _make_user(session, email="bob@example.com")
        bobs_kb = await _make_kb(session, owner=bob, name="Bob's Corpus")
        await session.commit()
        async with _client(session, alice) as client:
            ws_id = (
                await client.post("/api/v1/workspaces", json={"name": "Alice's"})
            ).json()["id"]
            refused = await client.put(
                f"/api/v1/workspaces/{ws_id}/knowledge-bases",
                json={"knowledge_base_ids": [str(bobs_kb.id)]},
            )
            assert refused.status_code == 404, refused.text
            # And nothing was partially applied.
            assert (
                await client.get(f"/api/v1/workspaces/{ws_id}/knowledge-bases")
            ).json() == []


@pytest.mark.asyncio
async def test_deleting_a_knowledge_base_removes_only_the_binding() -> None:
    """The other direction of the same rule."""
    async with _temp_database() as sessions, sessions() as session:
        user = await _make_user(session, email="kbdel@example.com")
        kb = await _make_kb(session, owner=user, name="Temp")
        workspace = await create_workspace(session, user_id=user.id, name="W")
        session.add(
            WorkspaceKnowledgeBase(
                id=uuid.uuid4(),
                workspace_id=workspace.id,
                knowledge_base_id=kb.id,
            )
        )
        await session.commit()

        await session.delete(kb)
        await session.commit()

        assert await session.get(Workspace, workspace.id) is not None
        assert (
            (
                await session.execute(
                    select(WorkspaceKnowledgeBase).where(
                        WorkspaceKnowledgeBase.workspace_id == workspace.id
                    )
                )
            )
            .scalars()
            .all()
            == []
        )


# ── Filing an existing conversation (PATCH) ─────────────────────────────────


@pytest.mark.asyncio
async def test_an_existing_conversation_is_filed_and_unfiled_by_its_owner() -> None:
    """The owner files, re-files and unfiles; a rename leaves the filing alone."""
    async with _temp_database() as sessions, sessions() as session:
        alice = await _make_user(session, email="alice@example.com")
        bob = await _make_user(session, email="bob@example.com")
        mine = await create_workspace(session, user_id=alice.id, name="Mine")
        bobs = await create_workspace(session, user_id=bob.id, name="Bob's")
        shared = await create_workspace(session, user_id=bob.id, name="Shared")
        _share(shared, alice, "read_only")
        retired = await create_workspace(
            session, user_id=alice.id, name="Retired", is_archived=True
        )
        await session.commit()

        async with _client(session, alice) as client:
            conv_id = (
                await client.post("/api/v1/chat/conversations", json={"title": "Thread"})
            ).json()["id"]
            url = f"/api/v1/chat/conversations/{conv_id}"

            filed = await client.patch(url, json={"workspace_id": str(mine.id)})
            assert filed.status_code == 200, filed.text
            assert filed.json()["workspace_id"] == str(mine.id)

            renamed = await client.patch(url, json={"title": "Renamed"})
            assert renamed.status_code == 200, renamed.text
            assert renamed.json()["title"] == "Renamed"
            assert renamed.json()["workspace_id"] == str(mine.id)

            invisible = await client.patch(url, json={"workspace_id": str(bobs.id)})
            assert invisible.status_code == 404, invisible.text
            read_only = await client.patch(url, json={"workspace_id": str(shared.id)})
            assert read_only.status_code == 403, read_only.text
            archived = await client.patch(url, json={"workspace_id": str(retired.id)})
            assert archived.status_code == 400, archived.text

            unfiled = await client.patch(url, json={"workspace_id": None})
            assert unfiled.status_code == 200, unfiled.text
            assert unfiled.json()["workspace_id"] is None

        async with _client(session, bob) as client:
            stranger = await client.patch(
                f"/api/v1/chat/conversations/{conv_id}",
                json={"workspace_id": str(bobs.id)},
            )
            assert stranger.status_code == 404, stranger.text
