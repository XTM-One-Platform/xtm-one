"""A workspace's own knowledge bases, against PostgreSQL.

Spec 032: a workspace's Sources are listed to exactly the people who can see
the workspace (FR-002), changed only from the workspace (FR-003), never bound
anywhere (FR-004), and go with the workspace (FR-031). Asserted on the real
routes and services, as non-admins, because ``user_visible_filter``
short-circuits for admins and a visibility rule tested only as an admin proves
nothing (``test_workspaces_postgres.py`` explains the history).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from sqlalchemy import func, select

import app.models  # noqa: F401 — registers every mapper
from app.api import deps
from app.api.v1 import knowledge_bases as kb_api
from app.api.v1 import workspace_knowledge as workspace_knowledge_api
from app.api.v1 import workspaces as workspaces_api
from app.api.v1.binding_access import refused_additions
from app.core.database import get_db
from app.models.group import Group, GroupMember
from app.models.knowledge_base import KnowledgeBase, KnowledgeDocument
from app.models.workspace import Workspace
from app.services.agent_bindings import (
    validate_company_managed_bindings,
    validate_personal_agent_bindings,
)
from app.services.workspaces import (
    WORKSPACE_KB_CONVERSATIONS,
    WORKSPACE_KB_SOURCES,
    WorkspaceActionRefusedError,
    ensure_workspace_kb,
    rebind_workspace_knowledge_bases,
)
from tests.test_workspaces_postgres import _make_user, _temp_database

pytestmark = pytest.mark.integration


@asynccontextmanager
async def _client(session, user) -> AsyncIterator[httpx.AsyncClient]:
    api = FastAPI()
    api.include_router(kb_api.router, prefix="/api/v1/knowledge-bases")
    api.include_router(workspaces_api.router, prefix="/api/v1/workspaces")
    api.include_router(workspace_knowledge_api.router, prefix="/api/v1/workspaces")
    api.dependency_overrides[deps.get_current_user] = lambda: user
    for gate in (
        deps.require_agentic_ai_licensed,
        deps.require_agentic_ai,
        deps.require_ai_enabled,
    ):
        api.dependency_overrides[gate] = lambda: None

    async def _get_db():
        yield session
        await session.commit()

    api.dependency_overrides[get_db] = _get_db
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api), base_url="http://test"
    ) as client:
        yield client


async def _world(session):
    """An owner's workspace shared with a reader (read_only) and a group."""
    owner = await _make_user(session, email="owner@example.com")
    reader = await _make_user(session, email="reader@example.com")
    member = await _make_user(session, email="member@example.com")
    stranger = await _make_user(session, email="stranger@example.com")
    group = Group(id=uuid.uuid4(), name=f"team-{uuid.uuid4().hex[:6]}")
    session.add(group)
    await session.flush()
    session.add(GroupMember(group_id=group.id, user_id=member.id))
    workspace = Workspace(
        id=uuid.uuid4(),
        user_id=owner.id,
        name="Research",
        slug="research",
        shared_user_ids=[str(reader.id)],
        shared_user_permissions={str(reader.id): "read_only"},
        shared_group_ids=[str(group.id)],
        shared_group_permissions={str(group.id): "read_only"},
    )
    session.add(workspace)
    await session.flush()
    kb = await ensure_workspace_kb(session, workspace, WORKSPACE_KB_SOURCES)
    session.add(
        KnowledgeDocument(
            knowledge_base_id=kb.id,
            filename="fact.md",
            source_type="markdown",
            content="The launch code name is Bluebird.",
            content_hash="h",
            status="ready",
        )
    )
    await session.commit()
    return owner, reader, member, stranger, workspace, kb


async def _listed(session, user, kb_id) -> dict | None:
    async with _client(session, user) as client:
        resp = await client.get("/api/v1/knowledge-bases")
    assert resp.status_code == 200, resp.text
    return next((row for row in resp.json() if row["id"] == str(kb_id)), None)


# ── Visibility follows the workspace (FR-002) ────────────────────────────────


@pytest.mark.asyncio
async def test_the_sources_kb_is_listed_to_exactly_the_workspace_audience() -> None:
    async with _temp_database() as sessions, sessions() as session:
        owner, reader, member, stranger, workspace, kb = await _world(session)

        for user in (owner, reader, member):
            row = await _listed(session, user, kb.id)
            assert row is not None, user.email
            assert row["managed_by_workspace"] == {"id": str(workspace.id), "name": "Research"}
            assert row["content_lock"] == "workspace"
            # Managed from the workspace only, its owner included.
            assert row["can_manage"] is False

        assert await _listed(session, stranger, kb.id) is None
        async with _client(session, stranger) as client:
            assert (await client.get(f"/api/v1/knowledge-bases/{kb.id}")).status_code == 404


@pytest.mark.asyncio
async def test_a_revoked_share_hides_the_kb_on_the_next_request() -> None:
    async with _temp_database() as sessions, sessions() as session:
        owner, reader, _member, _stranger, workspace, kb = await _world(session)
        assert await _listed(session, reader, kb.id) is not None

        async with _client(session, owner) as client:
            resp = await client.put(
                f"/api/v1/workspaces/{workspace.id}",
                json={"shared_user_ids": [], "shared_user_permissions": {}},
            )
        assert resp.status_code == 200, resp.text

        assert await _listed(session, reader, kb.id) is None


@pytest.mark.asyncio
async def test_a_company_managed_workspace_reaches_everyone_unless_restricted() -> None:
    async with _temp_database() as sessions, sessions() as session:
        owner, _reader, _member, stranger, workspace, kb = await _world(session)
        workspace.is_company_managed = True
        await session.commit()
        assert await _listed(session, stranger, kb.id) is not None

        workspace.access_restricted = True
        await session.commit()
        assert await _listed(session, stranger, kb.id) is None
        assert await _listed(session, owner, kb.id) is not None


# ── The collection belongs to the workspace, its content to its managers (FR-003)


@pytest.mark.asyncio
@pytest.mark.parametrize(("method", "kwargs"), [("PUT", {"json": {"name": "Renamed"}}), ("DELETE", {})])
async def test_the_collection_itself_is_changed_only_with_its_workspace(method, kwargs) -> None:
    """Settings, sharing and deletion are refused even to the workspace's
    owner and to an admin: the knowledge base is shared through the workspace
    and goes with it."""
    async with _temp_database() as sessions, sessions() as session:
        owner, *_rest, kb = await _world(session)
        admin = await _make_user(session, email="admin@example.com", is_admin=True)
        await session.commit()
        for user in (owner, admin):
            async with _client(session, user) as client:
                resp = await client.request(method, f"/api/v1/knowledge-bases/{kb.id}", **kwargs)
            assert resp.status_code == 403, resp.text
            assert resp.json()["detail"] == "Managed by workspace 'Research'. Change it from the workspace."
        assert await session.get(KnowledgeBase, kb.id) is not None


@pytest.mark.asyncio
async def test_no_route_reaches_the_conversations_kb() -> None:
    """The Conversations knowledge base holds what people wrote in their own
    threads: it is internal, so no knowledge-base route lists, reads, searches
    or writes it - not for the workspace's owner, a member or an admin. Only
    the workspace's search and the indexer reach it."""
    from app.api.v1.helpers import KB_WRITE_WORKSPACE, knowledge_base_write_refusal

    async with _temp_database() as sessions, sessions() as session:
        owner, _reader, member, _stranger, workspace, _sources = await _world(session)
        admin = await _make_user(session, email="admin@example.com", is_admin=True)
        kb = await ensure_workspace_kb(session, workspace, WORKSPACE_KB_CONVERSATIONS)
        doc = KnowledgeDocument(
            knowledge_base_id=kb.id,
            filename="conversation.md",
            source_type="markdown",
            content="What a member wrote in their own thread.",
            content_hash="c",
            status="ready",
        )
        session.add(doc)
        await session.commit()
        assert kb.is_internal is True

        body = {"filename": "forged.md", "content": "Forged.", "source_type": "markdown"}
        for user in (owner, member, admin):
            assert await _listed(session, user, kb.id) is None, user.email
            async with _client(session, user) as client:
                reads = [
                    await client.get(f"/api/v1/knowledge-bases/{kb.id}"),
                    await client.get(f"/api/v1/knowledge-bases/{kb.id}/documents"),
                    await client.get(f"/api/v1/knowledge-bases/{kb.id}/documents/{doc.id}"),
                    await client.get(f"/api/v1/knowledge-bases/{kb.id}/documents/{doc.id}/download"),
                    await client.post(f"/api/v1/knowledge-bases/{kb.id}/test-search", json={"query": "thread"}),
                    await client.post(f"/api/v1/knowledge-bases/{kb.id}/documents/text", json=body),
                    await client.put(
                        f"/api/v1/knowledge-bases/{kb.id}/documents/{doc.id}", json={"content": "Forged."}
                    ),
                    await client.delete(f"/api/v1/knowledge-bases/{kb.id}/documents/{doc.id}"),
                ]
                listed = await client.get("/api/v1/knowledge-bases/global/documents")
            assert [r.status_code for r in reads] == [404] * len(reads), (user.email, [r.text for r in reads])
            assert listed.status_code == 200, listed.text
            assert str(doc.id) not in listed.text, user.email

            refusal = await knowledge_base_write_refusal(session, kb, user)
            assert refusal is not None and refusal.reason == KB_WRITE_WORKSPACE, user.email
        assert (
            await session.execute(
                select(func.count()).select_from(KnowledgeDocument).where(KnowledgeDocument.knowledge_base_id == kb.id)
            )
        ).scalar() == 1
        assert (await session.get(KnowledgeDocument, doc.id)).content == "What a member wrote in their own thread."


@pytest.mark.asyncio
async def test_documents_are_written_by_the_workspace_managers_only() -> None:
    """The Sources content goes through the knowledge-base routes, under the
    WORKSPACE's tier: its owner and a ``config`` grantee add and remove
    documents, a ``read_only`` member is refused, whatever the knowledge base's
    own (empty) share columns say."""
    async with _temp_database() as sessions, sessions() as session:
        owner, reader, _member, _stranger, workspace, kb = await _world(session)
        editor = await _make_user(session, email="editor@example.com")
        workspace.shared_user_ids = [str(reader.id), str(editor.id)]
        workspace.shared_user_permissions = {str(reader.id): "read_only", str(editor.id): "config"}
        await session.commit()
        body = {"filename": "more.md", "content": "Another fact.", "source_type": "markdown"}

        async with _client(session, reader) as client:
            refused = await client.post(f"/api/v1/knowledge-bases/{kb.id}/documents/text", json=body)
        assert refused.status_code == 403, refused.text

        added = []
        for user in (owner, editor):
            async with _client(session, user) as client:
                resp = await client.post(f"/api/v1/knowledge-bases/{kb.id}/documents/text", json=body)
            assert resp.status_code == 201, resp.text
            added.append(resp.json()["id"])

        async with _client(session, reader) as client:
            assert (
                await client.delete(f"/api/v1/knowledge-bases/{kb.id}/documents/{added[0]}")
            ).status_code == 403
        async with _client(session, editor) as client:
            assert (
                await client.delete(f"/api/v1/knowledge-bases/{kb.id}/documents/{added[0]}")
            ).status_code == 204
        assert (
            await session.execute(
                select(func.count()).select_from(KnowledgeDocument).where(KnowledgeDocument.knowledge_base_id == kb.id)
            )
        ).scalar() == 2


@pytest.mark.asyncio
async def test_sync_sources_are_managed_through_the_workspace_tier(monkeypatch) -> None:
    async def _licensed(_db):
        return None

    monkeypatch.setattr(kb_api, "_assert_sync_sources_licensed", _licensed)
    body = {"source_type": "github", "config": {}, "sync_interval": "manual"}
    async with _temp_database() as sessions, sessions() as session:
        owner, reader, *_rest, kb = await _world(session)
        async with _client(session, reader) as client:
            refused = await client.post(f"/api/v1/knowledge-bases/{kb.id}/sync-sources", json=body)
        assert refused.status_code == 403, refused.text
        assert refused.json()["detail"] == "You don't have permission to add sync sources to this knowledge base."

        async with _client(session, owner) as client:
            created = await client.post(f"/api/v1/knowledge-bases/{kb.id}/sync-sources", json=body)
        assert created.status_code == 201, created.text


# ── Never bound (FR-004) ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_binding_surface_accepts_a_workspace_kb() -> None:
    async with _temp_database() as sessions, sessions() as session:
        owner, *_rest, _workspace, kb = await _world(session)
        other = Workspace(id=uuid.uuid4(), user_id=owner.id, name="Other", slug="other")
        session.add(other)
        await session.flush()

        with pytest.raises(HTTPException) as personal:
            await validate_personal_agent_bindings(
                session,
                owner.id,
                [],
                mcp_server_ids=None,
                knowledge_base_ids=[str(kb.id)],
                integration_bindings=None,
                handover_agent_ids=None,
                custom_tool_ids=None,
                skill_ids=None,
                prompt_ids=None,
            )
        assert personal.value.status_code == 400

        assert await refused_additions(
            session, user=owner, model=KnowledgeBase, value=[str(kb.id)]
        ) == [str(kb.id)]

        with pytest.raises(WorkspaceActionRefusedError) as rebind:
            await rebind_workspace_knowledge_bases(
                session, other, owner.id, [], is_admin=False, add={kb.id}
            )
        assert rebind.value.status_code == 404


@pytest.mark.asyncio
async def test_a_company_managed_agent_cannot_bind_one_either() -> None:
    """Including the adjacent case: an internal company-managed corpus (a
    workset's) used to pass this validator, which skipped ``is_internal``."""
    async with _temp_database() as sessions, sessions() as session:
        _owner, *_rest, workspace, kb = await _world(session)
        workspace.is_company_managed = True
        internal = KnowledgeBase(
            id=uuid.uuid4(), name="workset:x", is_internal=True, is_company_managed=True
        )
        session.add(internal)
        await session.flush()
        for kb_id in (kb.id, internal.id):
            with pytest.raises(HTTPException) as exc:
                await validate_company_managed_bindings(
                    session,
                    mcp_server_ids=None,
                    knowledge_base_ids=[str(kb_id)],
                    integration_bindings=None,
                    handover_agent_ids=None,
                )
            assert exc.value.status_code == 400


# ── The workspace's own routes (US1, US2) ────────────────────────────────────


@pytest.mark.asyncio
async def test_the_sources_kb_is_created_once_by_a_manager_only() -> None:
    async with _temp_database() as sessions, sessions() as session:
        owner, reader, _member, stranger, workspace, kb = await _world(session)
        editor = await _make_user(session, email="editor@example.com")
        workspace.shared_user_ids = [str(reader.id), str(editor.id)]
        workspace.shared_user_permissions = {str(reader.id): "read_only", str(editor.id): "config_with_chats"}
        await session.commit()
        path = f"/api/v1/workspaces/{workspace.id}/sources"

        async with _client(session, reader) as client:
            assert (await client.post(path)).status_code == 403
        async with _client(session, stranger) as client:
            assert (await client.post(path)).status_code == 404
        for user in (owner, editor):
            async with _client(session, user) as client:
                resp = await client.post(path)
            assert resp.status_code == 200, resp.text
            assert resp.json() == {"knowledge_base_id": str(kb.id)}
        assert (
            await session.execute(
                select(func.count()).select_from(KnowledgeBase).where(KnowledgeBase.owner_workspace_id == workspace.id)
            )
        ).scalar() == 1


@pytest.mark.asyncio
async def test_the_summary_is_readable_by_a_reader_without_manage_rights() -> None:
    async with _temp_database() as sessions, sessions() as session:
        owner, reader, _member, stranger, workspace, kb = await _world(session)
        workspace.instructions = "Answer in French."
        await session.commit()
        async with _client(session, reader) as client:
            resp = await client.get(f"/api/v1/workspaces/{workspace.id}/knowledge")
        assert resp.status_code == 200, resp.text
        assert resp.json() == {
            "instructions": "Answer in French.",
            "can_manage": False,
            "sources_knowledge_base_id": str(kb.id),
            "sources": {"count": 1, "ready": 1, "pending": 0, "failed": 0},
            "skills": 0,
            "knowledge_bases": 0,
            "conversations": {"indexed": 0, "pending": 0},
        }
        async with _client(session, owner) as client:
            assert (await client.get(f"/api/v1/workspaces/{workspace.id}/knowledge")).json()["can_manage"] is True
        async with _client(session, stranger) as client:
            assert (await client.get(f"/api/v1/workspaces/{workspace.id}/knowledge")).status_code == 404


@pytest.mark.asyncio
async def test_instructions_are_bounded_and_kept_on_a_partial_update() -> None:
    async with _temp_database() as sessions, sessions() as session:
        owner, *_rest, workspace, _kb = await _world(session)
        async with _client(session, owner) as client:
            ok = await client.put(f"/api/v1/workspaces/{workspace.id}", json={"instructions": "x" * 8000})
            too_long = await client.put(f"/api/v1/workspaces/{workspace.id}", json={"instructions": "x" * 8001})
            renamed = await client.put(f"/api/v1/workspaces/{workspace.id}", json={"name": "Renamed"})
        assert ok.status_code == 200, ok.text
        assert too_long.status_code == 422
        assert renamed.json()["instructions"] == "x" * 8000


async def _attachment(session, *, owner, conversation_workspace_id):
    from app.models.chat import Conversation
    from app.models.file_attachment import FileAttachment

    conversation = Conversation(id=uuid.uuid4(), user_id=owner.id, workspace_id=conversation_workspace_id)
    session.add(conversation)
    await session.flush()
    attachment = FileAttachment(
        id=uuid.uuid4(),
        user_id=owner.id,
        conversation_id=conversation.id,
        filename="brief.txt",
        content_type="text/plain",
        size=26,
        s3_object_name=f"test/{uuid.uuid4()}",
    )
    session.add(attachment)
    await session.commit()
    return attachment


@pytest.mark.asyncio
async def test_a_conversation_file_becomes_a_source_only_from_this_workspace(monkeypatch) -> None:
    from unittest.mock import AsyncMock

    from app.services.file_storage import file_storage

    monkeypatch.setattr(file_storage, "download", AsyncMock(return_value=b"The budget is 4 million."))
    enqueue = AsyncMock(return_value=True)
    monkeypatch.setattr(workspace_knowledge_api, "enqueue_document_processing", enqueue)
    async with _temp_database() as sessions, sessions() as session:
        owner, reader, _member, _stranger, workspace, kb = await _world(session)
        here = await _attachment(session, owner=owner, conversation_workspace_id=workspace.id)
        elsewhere = await _attachment(session, owner=owner, conversation_workspace_id=None)
        path = f"/api/v1/workspaces/{workspace.id}/sources/from-attachment"

        async with _client(session, owner) as client:
            refused = await client.post(path, json={"file_attachment_id": str(elsewhere.id)})
            added = await client.post(path, json={"file_attachment_id": str(here.id)})
        async with _client(session, reader) as client:
            forbidden = await client.post(path, json={"file_attachment_id": str(here.id)})

        assert refused.status_code == 404
        assert forbidden.status_code == 403
        assert added.status_code == 201, added.text
        document_id = added.json()["document_id"]
        assert added.json()["knowledge_base_id"] == str(kb.id)
        doc = await session.get(KnowledgeDocument, uuid.UUID(document_id))
        assert (doc.knowledge_base_id, doc.filename, doc.content, doc.status) == (
            kb.id,
            "brief.txt",
            "The budget is 4 million.",
            "pending",
        )
        enqueue.assert_awaited_once_with(document_id)


@pytest.mark.asyncio
async def test_linked_skills_follow_the_bound_knowledge_base_rules() -> None:
    from app.models.agent_skill import AgentSkill

    async with _temp_database() as sessions, sessions() as session:
        owner, reader, _member, _stranger, workspace, _kb = await _world(session)
        mine = AgentSkill(id=uuid.uuid4(), user_id=owner.id, name="Triage", content="Do triage.")
        theirs = AgentSkill(id=uuid.uuid4(), user_id=reader.id, name="Private", content="Secret.")
        session.add_all([mine, theirs])
        await session.commit()
        path = f"/api/v1/workspaces/{workspace.id}/skills"

        async with _client(session, owner) as client:
            hidden = await client.put(path, json={"skill_ids": [str(theirs.id)]})
            linked = await client.put(path, json={"skill_ids": [str(mine.id)]})
        assert hidden.status_code == 404
        assert linked.status_code == 200, linked.text
        assert [s["skill_id"] for s in linked.json()] == [str(mine.id)]

        async with _client(session, reader) as client:
            # The reader cannot see the owner's private skill: neither listed
            # to them nor changeable by them.
            assert (await client.get(path)).json() == []
            assert (await client.put(path, json={"skill_ids": []})).status_code == 403


# ── Deletion (US5) ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_deleting_a_workspace_takes_its_knowledge_and_nothing_else(monkeypatch) -> None:
    from unittest.mock import AsyncMock

    from app.models.agent_skill import AgentSkill
    from app.models.embedding import Embedding
    from app.models.knowledge_base import KBExportPackage, KBSyncSource
    from app.models.workspace import WorkspaceKnowledgeBase, WorkspaceSkill
    from app.services import kb_helpers
    from app.services.file_storage import file_storage

    cancelled: list = []
    monkeypatch.setattr(
        kb_helpers, "cancel_document_jobs_after_commit", lambda _db, ids: cancelled.extend(ids)
    )
    storage_delete = AsyncMock()
    monkeypatch.setattr(file_storage, "delete", storage_delete)
    async with _temp_database() as sessions, sessions() as session:
        owner, *_rest, workspace, kb = await _world(session)
        doc_id = (
            await session.execute(select(KnowledgeDocument.id).where(KnowledgeDocument.knowledge_base_id == kb.id))
        ).scalar_one()
        library = KnowledgeBase(id=uuid.uuid4(), user_id=owner.id, name="Library KB")
        skill = AgentSkill(id=uuid.uuid4(), user_id=owner.id, name="Skill", content="Body.")
        session.add_all([library, skill])
        await session.flush()
        session.add_all(
            [
                WorkspaceKnowledgeBase(workspace_id=workspace.id, knowledge_base_id=library.id),
                WorkspaceSkill(workspace_id=workspace.id, skill_id=skill.id),
                Embedding(
                    knowledge_base_id=kb.id,
                    source_type="document",
                    source_id=str(doc_id),
                    content="chunk",
                    embedding=[0.1, 0.2, 0.3],
                ),
                KBSyncSource(knowledge_base_id=kb.id, user_id=owner.id, source_type="github", config={}),
                KBExportPackage(
                    knowledge_base_id=kb.id,
                    user_id=owner.id,
                    part_number=1,
                    total_parts=1,
                    filename="w.zip",
                    s3_object_name="exports/w.zip",
                    file_size=1,
                    expires_at=datetime.now(UTC) + timedelta(days=1),
                ),
            ]
        )
        await session.commit()

        kb_id, library_id, skill_id = kb.id, library.id, skill.id
        async with _client(session, owner) as client:
            resp = await client.delete(f"/api/v1/workspaces/{workspace.id}")
        assert resp.status_code == 204, resp.text
        session.expire_all()

        for model in (KnowledgeDocument, Embedding, KBSyncSource, KBExportPackage):
            column = model.knowledge_base_id
            assert (
                await session.execute(select(func.count()).select_from(model).where(column == kb_id))
            ).scalar() == 0, model.__name__
        assert (
            await session.execute(select(func.count()).select_from(KnowledgeBase).where(KnowledgeBase.id == kb_id))
        ).scalar() == 0
        assert (
            await session.execute(select(func.count()).select_from(WorkspaceSkill))
        ).scalar() == 0
        # What was only linked stays.
        assert await session.get(KnowledgeBase, library_id) is not None
        assert await session.get(AgentSkill, skill_id) is not None
        assert cancelled == [doc_id]
        storage_delete.assert_awaited_once_with("exports/w.zip")


@pytest.mark.asyncio
async def test_a_refused_deletion_leaves_the_knowledge_in_place() -> None:
    async with _temp_database() as sessions, sessions() as session:
        owner, *_rest, workspace, kb = await _world(session)
        workspace.is_default = True
        await session.commit()
        async with _client(session, owner) as client:
            resp = await client.delete(f"/api/v1/workspaces/{workspace.id}")
        assert resp.status_code == 400
        assert await session.get(KnowledgeBase, kb.id) is not None


@pytest.mark.asyncio
async def test_a_company_managed_workspace_keeps_its_sources_when_its_owner_is_deleted() -> None:
    """User deletion hands company-managed rows to an admin, then cascades the
    rest: a Sources knowledge base owned by the person would have cascaded
    away while its workspace survived. It has no owner of its own."""
    from app.api.v1.helpers import reassign_company_managed_entities
    from app.models.user import User

    async with _temp_database() as sessions, sessions() as session:
        owner, *_rest, workspace, kb = await _world(session)
        admin = await _make_user(session, email="admin@example.com", is_admin=True)
        workspace.is_company_managed = True
        await session.commit()
        assert kb.user_id is None
        kb_id, workspace_id, owner_id, admin_id = kb.id, workspace.id, owner.id, admin.id

        await reassign_company_managed_entities(session, owner_id, admin_id)
        await session.delete(await session.get(User, owner_id))
        await session.commit()
        session.expire_all()

        assert (
            await session.execute(select(Workspace.user_id).where(Workspace.id == workspace_id))
        ).scalar_one() == admin_id
        assert (
            await session.execute(select(func.count()).select_from(KnowledgeBase).where(KnowledgeBase.id == kb_id))
        ).scalar() == 1


@pytest.mark.asyncio
async def test_the_read_routes_serve_a_workspace_manager_as_a_manager(monkeypatch) -> None:
    """The sync-source list serves the full error to whoever may change the
    sources - the workspace's managers, not the knowledge base's (empty) share
    columns - and the fixed reader sentence to anyone else."""
    from app.models.knowledge_base import KBSyncSource

    async with _temp_database() as sessions, sessions() as session:
        owner, reader, *_rest, kb = await _world(session)
        session.add(
            KBSyncSource(
                knowledge_base_id=kb.id,
                user_id=owner.id,
                source_type="github",
                config={},
                last_sync_status="error",
                last_sync_error="repository acme/private not found",
            )
        )
        await session.commit()
        listed = {}
        for user in (owner, reader):
            async with _client(session, user) as client:
                resp = await client.get(f"/api/v1/knowledge-bases/{kb.id}/sync-sources")
            assert resp.status_code == 200, resp.text
            (listed[user.email],) = resp.json()
        assert listed["owner@example.com"]["last_sync_error"] == "repository acme/private not found"
        assert listed["reader@example.com"]["last_sync_error"] != "repository acme/private not found"


@pytest.mark.asyncio
async def test_the_workspace_knowledge_settings_are_admin_only_and_bounded() -> None:
    from app.api.v1 import settings as settings_api

    async def _call(session, user, method, json=None):
        api = FastAPI()
        api.include_router(settings_api.router, prefix="/api/v1/settings")
        api.dependency_overrides[deps.get_current_user] = lambda: user

        async def _get_db():
            yield session
            await session.commit()

        api.dependency_overrides[get_db] = _get_db
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:
            return await client.request(method, "/api/v1/settings/workspace-knowledge", json=json)

    async with _temp_database() as sessions, sessions() as session:
        owner, *_rest = await _world(session)
        admin = await _make_user(session, email="admin@example.com", is_admin=True)
        await session.commit()

        assert (await _call(session, owner, "GET")).status_code == 403
        default = await _call(session, admin, "GET")
        assert default.json() == {"sources_context_percent": 15, "conversation_idle_minutes": 30}
        body = {"sources_context_percent": 20, "conversation_idle_minutes": 60}
        assert (await _call(session, admin, "PUT", body)).json() == body
        assert (await _call(session, admin, "GET")).json() == body
        for bad in ({"sources_context_percent": 41, "conversation_idle_minutes": 60},
                    {"sources_context_percent": 20, "conversation_idle_minutes": 4}):
            assert (await _call(session, admin, "PUT", bad)).status_code == 422
