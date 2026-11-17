"""A workspace's conversation search, against PostgreSQL.

Spec 032 US3: filed conversations are indexed once idle (FR-015, FR-016), one
document each, replaced and never duplicated (FR-017); tool results never
reach the index or the summary (FR-017a); secrets stay redacted (FR-021); a
conversation unfiled, moved, archived or deleted leaves the search at once
(FR-018), and so does one whose owner lost the workspace; group threads are
never indexed. Also the search tool's filter and
its links (FR-019, FR-020), and filing by asking the agent (FR-022, FR-027b).
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

import app.models  # noqa: F401 - registers every mapper on Base.metadata
from app.models.chat import Conversation, Message
from app.models.file_attachment import FileAttachment
from app.models.knowledge_base import KnowledgeBase, KnowledgeDocument
from app.models.platform_settings import PlatformSettings
from app.models.user import User
from app.models.workspace import Workspace, WorkspaceConversationIndex
from app.services import workspace_conversation_index as wci
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration

_TOOL_SECRET = "TOOL-ONLY-FACT-7731"


@pytest.fixture
def summary(monkeypatch):
    spy = AsyncMock(return_value="- They chose the blue plan.")
    monkeypatch.setattr(wci, "summarise", spy)
    monkeypatch.setattr("app.services.kb_helpers.enqueue_document_processing", AsyncMock(return_value=True))
    return spy


async def _seed(session, *, channel="web", thread=None, minutes_ago=60):
    owner = User(id=uuid.uuid4(), email=f"o-{uuid.uuid4().hex[:6]}@test.local", full_name="Owner", hashed_password="x")
    session.add(owner)
    await session.flush()
    if (await session.execute(select(PlatformSettings.id).limit(1))).first() is None:
        session.add(PlatformSettings(workspace_conversation_idle_minutes=5))
    workspace = Workspace(id=uuid.uuid4(), user_id=owner.id, name="Launch", slug=f"l-{uuid.uuid4().hex[:6]}")
    other = Workspace(id=uuid.uuid4(), user_id=owner.id, name="Other", slug=f"o-{uuid.uuid4().hex[:6]}")
    session.add_all([workspace, other])
    await session.flush()
    conversation = Conversation(
        id=uuid.uuid4(),
        user_id=owner.id,
        workspace_id=workspace.id,
        title="Plan choice",
        channel=channel,
        channel_thread_id=thread,
    )
    session.add(conversation)
    await session.flush()
    when = datetime.now(UTC) - timedelta(minutes=minutes_ago)
    attachment = FileAttachment(
        id=uuid.uuid4(),
        user_id=owner.id,
        conversation_id=conversation.id,
        filename="brief.txt",
        content_type="text/plain",
        size=10,
        s3_object_name=f"test/{uuid.uuid4()}",
    )
    session.add(attachment)
    rows = [
        Message(
            conversation_id=conversation.id,
            role="user",
            content="Which plan? The key is [[SECRET_VARIABLE:00000000-0000-0000-0000-000000000000:0123456789abcdef]].",
            metadata_json=json.dumps({"attachments": [{"file_id": str(attachment.id), "filename": "brief.txt"}]}),
            sequence=1,
            created_at=when,
        ),
        Message(
            conversation_id=conversation.id,
            role="assistant",
            content="We chose the blue plan.",
            tool_calls_json=json.dumps([{"name": "lookup", "arguments": _TOOL_SECRET}]),
            metadata_json=json.dumps({"tool_call_trace": [{"tool": "lookup", "output": _TOOL_SECRET}]}),
            sequence=2,
            created_at=when,
        ),
        Message(conversation_id=conversation.id, role="tool", content=_TOOL_SECRET, sequence=3, created_at=when),
        Message(
            conversation_id=conversation.id,
            role="user",
            content="WAKE: background task finished",
            metadata_json=json.dumps({"hidden": True, "source": "background_task_wake"}),
            sequence=4,
            created_at=when,
        ),
    ]
    session.add_all(rows)
    await session.commit()
    # Ids captured now: the tests expire the session, and a lazy load of an
    # expired attribute cannot run outside the greenlet.
    return SimpleNamespace(
        owner=owner,
        workspace=workspace,
        other=other,
        conversation=conversation,
        attachment=attachment,
        workspace_id=workspace.id,
        other_id=other.id,
        conversation_id=conversation.id,
    )


async def _documents(session, workspace_id) -> list[KnowledgeDocument]:
    return list(
        (
            await session.execute(
                select(KnowledgeDocument)
                .join(KnowledgeBase, KnowledgeBase.id == KnowledgeDocument.knowledge_base_id)
                .where(KnowledgeBase.owner_workspace_id == workspace_id)
            )
        )
        .scalars()
        .all()
    )


@pytest.mark.asyncio
async def test_an_idle_filed_conversation_is_indexed_without_its_tool_results(monkeypatch, summary):
    monkeypatch.setattr("app.services.file_storage.file_storage.download", AsyncMock(return_value=b"Budget: 4 million."))
    async with scratch_database("wsconv", tables=ALL_TABLES, extensions=("vector",)) as db, db.sessions() as session:
        world = await _seed(session)
        updated_before = world.conversation.updated_at

        assert await wci.pick_idle_conversations(session) == [world.conversation_id]
        assert await wci.index_conversation(str(world.conversation_id)) == wci.STATUS_INDEXED

        session.expire_all()
        (doc,) = await _documents(session, world.workspace_id)
        assert doc.source_type == "conversation"
        assert doc.extra_metadata == {"conversation_id": str(world.conversation_id)}
        assert "We chose the blue plan." in doc.content
        assert "Budget: 4 million." in doc.content
        assert "- They chose the blue plan." in doc.content
        assert "[secret]" in doc.content and "SECRET_VARIABLE" not in doc.content
        assert _TOOL_SECRET not in doc.content
        assert "WAKE: background task finished" not in doc.content
        transcript = summary.await_args.args[1]
        assert _TOOL_SECRET not in transcript and "We chose the blue plan." in transcript
        # Nothing written to the conversation row: the sidebar order holds.
        conv = await session.get(Conversation, world.conversation_id)
        assert conv.updated_at == updated_before
        # Indexed and current: not picked again.
        assert await wci.pick_idle_conversations(session) == []


@pytest.mark.asyncio
async def test_new_messages_replace_the_one_document(summary):
    async with scratch_database("wsconv", tables=ALL_TABLES, extensions=("vector",)) as db, db.sessions() as session:
        world = await _seed(session)
        await wci.index_conversation(str(world.conversation_id))
        session.expire_all()
        (first,) = await _documents(session, world.workspace_id)
        session.add(
            Message(
                conversation_id=world.conversation_id,
                role="user",
                content="And the red plan?",
                sequence=5,
                created_at=datetime.now(UTC) - timedelta(minutes=30),
            )
        )
        # The first indexing an hour ago, the new message half an hour ago -
        # which bumps ``updated_at``, as ``store_message`` does on every one.
        (await session.get(WorkspaceConversationIndex, world.conversation_id)).indexed_at = datetime.now(
            UTC
        ) - timedelta(minutes=60)
        (await session.get(Conversation, world.conversation_id)).updated_at = datetime.now(UTC) - timedelta(minutes=30)
        await session.commit()

        assert await wci.pick_idle_conversations(session) == [world.conversation_id]
        await wci.index_conversation(str(world.conversation_id))
        session.expire_all()
        (second,) = await _documents(session, world.workspace_id)
        assert second.id == first.id
        assert "And the red plan?" in second.content


@pytest.mark.asyncio
async def test_a_renamed_conversation_is_indexed_again_under_its_new_title(summary):
    async with scratch_database("wsconv", tables=ALL_TABLES, extensions=("vector",)) as db, db.sessions() as session:
        world = await _seed(session)
        assert await wci.index_conversation(str(world.conversation_id)) == wci.STATUS_INDEXED
        assert world.conversation_id not in await wci.pick_idle_conversations(session)

        conv = await session.get(Conversation, world.conversation_id)
        conv.title = "Blue plan decision"
        await session.commit()

        assert await wci.pick_idle_conversations(session) == [world.conversation_id]
        assert await wci.index_conversation(str(world.conversation_id)) == wci.STATUS_INDEXED
        session.expire_all()
        (doc,) = await _documents(session, world.workspace_id)
        assert doc.filename == "Blue plan decision"
        assert "Blue plan decision" in doc.content


@pytest.mark.asyncio
async def test_a_conversation_that_is_still_active_or_a_group_thread_is_not_picked(summary):
    async with scratch_database("wsconv", tables=ALL_TABLES, extensions=("vector",)) as db, db.sessions() as session:
        recent = await _seed(session, minutes_ago=1)
        assert await wci.pick_idle_conversations(session) == []
        group = await _seed(session, channel="slack", thread="C0123/1700000000.000100")
        assert group.conversation.id not in await wci.pick_idle_conversations(session)
        assert await wci.index_conversation(str(group.conversation.id)) is None
        assert recent.conversation.id not in await wci.pick_idle_conversations(session)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["archive", "unfile", "move", "delete"])
async def test_a_conversation_leaves_the_search_at_once_and_the_sweep_reclaims_it(summary, change):
    async with scratch_database("wsconv", tables=ALL_TABLES, extensions=("vector",)) as db, db.sessions() as session:
        world = await _seed(session)
        await wci.index_conversation(str(world.conversation_id))
        session.expire_all()
        (doc,) = await _documents(session, world.workspace_id)
        doc_id, kb_id = str(doc.id), str(doc.knowledge_base_id)
        assert await wci.unsearchable_document_ids(session, kb_id, exclude_conversation_id=None) == set()

        conv = await session.get(Conversation, world.conversation_id)
        if change == "archive":
            conv.is_archived = True
        elif change == "unfile":
            conv.workspace_id = None
        elif change == "move":
            conv.workspace_id = world.other_id
        else:
            await session.delete(conv)
        await session.commit()

        # Gone from the next search, before any sweep.
        assert await wci.unsearchable_document_ids(session, kb_id, exclude_conversation_id=None) == {doc_id}
        assert await wci.sweep_stale_documents(session) == 1
        await session.commit()
        assert await _documents(session, world.workspace_id) == []
        if change == "move":
            # Re-indexed into the workspace it moved to.
            assert await wci.index_conversation(str(world.conversation_id)) == wci.STATUS_INDEXED
            assert len(await _documents(session, world.other_id)) == 1


async def _members_conversation(session, world, grant: str) -> User:
    """Hand the seeded conversation to a member who sees the workspace through
    *grant*: a per-person share, a group's, or the company-managed tier."""
    from app.models.group import Group, GroupMember

    member = User(id=uuid.uuid4(), email=f"m-{uuid.uuid4().hex[:6]}@test.local", full_name="Member", hashed_password="x")
    session.add(member)
    await session.flush()
    workspace = await session.get(Workspace, world.workspace_id)
    if grant == "user":
        workspace.shared_user_ids = [str(member.id)]
        workspace.shared_user_permissions = {str(member.id): "config"}
    elif grant == "group":
        group = Group(id=uuid.uuid4(), name=f"g-{uuid.uuid4().hex[:6]}")
        session.add(group)
        await session.flush()
        session.add(GroupMember(group_id=group.id, user_id=member.id))
        workspace.shared_group_ids = [str(group.id)]
        workspace.shared_group_permissions = {str(group.id): "config"}
    else:
        workspace.is_company_managed = True
    conversation = await session.get(Conversation, world.conversation_id)
    conversation.user_id = member.id
    await session.commit()
    return member


async def _revoke(session, world, grant: str) -> None:
    workspace = await session.get(Workspace, world.workspace_id)
    if grant == "user":
        workspace.shared_user_ids, workspace.shared_user_permissions = [], {}
    elif grant == "group":
        workspace.shared_group_ids, workspace.shared_group_permissions = [], {}
    else:
        workspace.access_restricted = True
    await session.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("grant", ["user", "group", "company"])
async def test_a_member_who_loses_the_workspace_stops_being_indexed_and_leaves_the_search(summary, grant):
    """A revoked share ends the indexing and drops what was indexed, like an
    unfiling: the member's later words never reach the workspace."""
    async with scratch_database("wsconv", tables=ALL_TABLES, extensions=("vector",)) as db, db.sessions() as session:
        world = await _seed(session)
        await _members_conversation(session, world, grant)
        assert world.conversation_id in await wci.pick_idle_conversations(session)
        assert await wci.index_conversation(str(world.conversation_id)) == wci.STATUS_INDEXED
        session.expire_all()
        (doc,) = await _documents(session, world.workspace_id)
        doc_id, kb_id = str(doc.id), str(doc.knowledge_base_id)
        assert await wci.unsearchable_document_ids(session, kb_id, exclude_conversation_id=None) == set()

        await _revoke(session, world, grant)

        assert await wci.unsearchable_document_ids(session, kb_id, exclude_conversation_id=None) == {doc_id}
        assert world.conversation_id not in await wci.pick_idle_conversations(session)
        assert await wci.index_conversation(str(world.conversation_id)) is None
        assert await wci.sweep_stale_documents(session) == 1
        await session.commit()
        assert await _documents(session, world.workspace_id) == []


@pytest.mark.asyncio
async def test_an_administrators_filed_conversation_is_indexed_without_a_grant(summary):
    async with scratch_database("wsconv", tables=ALL_TABLES, extensions=("vector",)) as db, db.sessions() as session:
        world = await _seed(session)
        member = await _members_conversation(session, world, "user")
        await _revoke(session, world, "user")
        assert world.conversation_id not in await wci.pick_idle_conversations(session)
        (await session.get(User, member.id)).role = "admin"
        await session.commit()
        assert world.conversation_id in await wci.pick_idle_conversations(session)
        assert await wci.index_conversation(str(world.conversation_id)) == wci.STATUS_INDEXED


@pytest.mark.asyncio
async def test_the_current_conversation_is_never_returned_to_itself(summary):
    async with scratch_database("wsconv", tables=ALL_TABLES, extensions=("vector",)) as db, db.sessions() as session:
        world = await _seed(session)
        await wci.index_conversation(str(world.conversation_id))
        session.expire_all()
        (doc,) = await _documents(session, world.workspace_id)
        assert await wci.unsearchable_document_ids(
            session, str(doc.knowledge_base_id), exclude_conversation_id=world.conversation_id
        ) == {str(doc.id)}


@pytest.mark.asyncio
async def test_the_search_tool_filters_on_current_documents_and_escapes_titles(monkeypatch, summary):
    from app.services.builtin_tools.workspace_knowledge import build_workspace_conversation_search_tool

    async with scratch_database("wsconv", tables=ALL_TABLES, extensions=("vector",)) as db, db.sessions() as session:
        world = await _seed(session)
        world.conversation.title = "x](https://evil.example) [y"
        await session.commit()
        await wci.index_conversation(str(world.conversation_id))
        session.expire_all()
        (doc,) = await _documents(session, world.workspace_id)

        provider = SimpleNamespace(embed=AsyncMock(return_value=[0.1, 0.2]))
        monkeypatch.setattr(
            "app.llm.router.get_embedding_provider", AsyncMock(return_value=(provider, "emb", None))
        )
        hybrid = AsyncMock(return_value=[SimpleNamespace(source_id=str(doc.id), content="We chose the blue plan.")])
        monkeypatch.setattr("app.services.hybrid_search.hybrid_search_embeddings", hybrid)
        def _tool(current):
            _defs, executor = build_workspace_conversation_search_tool(
                workspace_name="Launch",
                kb_id=str(doc.knowledge_base_id),
                current_conversation_id=current,
            )
            return executor._handlers["search_workspace_conversations"]

        out = await _tool(str(uuid.uuid4()))(query="plan")
        assert hybrid.await_args.kwargs["knowledge_base_ids"] == [str(doc.knowledge_base_id)]
        assert "source_ids" not in hybrid.await_args.kwargs
        assert r"[x\]\(https://evil.example\) \[y](" in out
        assert f"/chat?c={world.conversation_id})" in out
        assert "<<UNTRUSTED-" in out
        # From the conversation itself, its own passage is dropped.
        assert await _tool(str(world.conversation_id))(query="plan") == "No conversation of this workspace matches."

        executor_blank = _tool(None)
        blank = await executor_blank(query="   ")
        assert blank.startswith("The `query` argument is required")


@pytest.mark.asyncio
async def test_counts_split_indexed_from_waiting(summary):
    async with scratch_database("wsconv", tables=ALL_TABLES, extensions=("vector",)) as db, db.sessions() as session:
        world = await _seed(session)
        assert await wci.index_counts(session, world.workspace_id) == {"indexed": 0, "pending": 1}
        await wci.index_conversation(str(world.conversation_id))
        assert await wci.index_counts(session, world.workspace_id) == {"indexed": 1, "pending": 0}
        assert (await session.execute(select(func.count()).select_from(WorkspaceConversationIndex))).scalar() == 1
