"""Workspace management from chat through the agent builder, against real
PostgreSQL.

The tools are the platform's own workspace surface offered to the model, so
what these tests pin is that they decide exactly what the REST routes decide:
a conversation is only its owner's to file, filing takes the manage right on
the workspace and refuses an archived one, the default is neither archived
nor deleted, a knowledge base the caller cannot see is never bound, and a
workspace the caller cannot see answers like an unknown id.

``integration``-marked and skips when PostgreSQL is unreachable, like its
neighbours.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.models.audit_log import AuditLog
from app.models.chat import Conversation
from app.models.knowledge_base import KnowledgeBase
from app.models.user import User
from app.models.work_item import WorkItem
from app.models.workspace import Workspace, WorkspaceKnowledgeBase
from app.services.builtin_tools import build_agent_builder_tool
from app.services.workspaces import create_workspace, ensure_default_workspace
from app.tools.base import ToolCall
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration


@asynccontextmanager
async def _database() -> AsyncIterator[async_sessionmaker]:
    async with scratch_database(
        "wstools", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True
    ) as db:
        yield db.sessions


async def _tool(
    caller: User | str, name: str, /, conversation_id: str | None = None, **arguments
) -> str:
    user_id = caller if isinstance(caller, str) else str(caller.id)
    _, executor = build_agent_builder_tool(None, user_id, conversation_id=conversation_id)
    result = await executor.execute_tool(
        ToolCall(id=str(uuid.uuid4()), name=name, arguments=arguments)
    )
    return result.content


async def _user(session, name: str, *, admin: bool = False) -> User:
    user = User(
        id=uuid.uuid4(),
        email=f"{name}-{uuid.uuid4().hex[:6]}@example.com",
        full_name=name,
        hashed_password="x",
        role="admin" if admin else "user",
    )
    session.add(user)
    await session.flush()
    return user


async def _conversation(session, owner: User, title: str, workspace=None) -> Conversation:
    conversation = Conversation(
        id=uuid.uuid4(),
        user_id=owner.id,
        channel="web",
        title=title,
        workspace_id=workspace.id if workspace is not None else None,
    )
    session.add(conversation)
    await session.flush()
    return conversation


async def _reload(sessions, model, row_id):
    async with sessions() as session:
        return await session.get(model, row_id)


def test_every_workspace_tool_is_declared_and_registered() -> None:
    """A definition without a handler is a tool the model calls into a
    refusal; a handler without a definition is one it never learns of."""
    definitions, executor = build_agent_builder_tool(None, "", conversation_id=None)
    names = {d.name for d in definitions}
    workspace_tools = {
        "list_workspaces",
        "get_workspace",
        "create_workspace",
        "update_workspace",
        "delete_workspace",
        "set_default_workspace",
        "update_workspace_knowledge_bases",
        "list_conversations",
        "move_conversations_to_workspace",
    }
    assert workspace_tools <= names
    assert workspace_tools <= set(executor.tool_names)
    assert len(names) == len(definitions), "a tool name is declared twice"


async def test_an_anonymous_caller_manages_no_workspace() -> None:
    for name, args in (
        ("list_workspaces", {}),
        ("create_workspace", {"name": "W"}),
        ("move_conversations_to_workspace", {"workspace_id": "W"}),
        ("list_conversations", {}),
    ):
        assert "not available for anonymous users" in await _tool("anonymous", name, **args)


async def test_create_then_move_the_current_conversation_by_name() -> None:
    async with _database() as sessions:
        async with sessions() as s:
            alice = await _user(s, "alice")
            here = await _conversation(s, alice, "Acme audit notes")
            await s.commit()

        created = await _tool(alice, "create_workspace", name="Acme audit", description="Q3")
        assert "created" in created, created
        async with sessions() as s:
            workspace = (
                await s.execute(select(Workspace).where(Workspace.user_id == alice.id, Workspace.name == "Acme audit"))
            ).scalar_one()
            audit = (
                await s.execute(select(AuditLog).where(AuditLog.entity_id == str(workspace.id)))
            ).scalar_one()
            assert audit.details["via"] == "agent"

        moved = await _tool(
            alice, "move_conversations_to_workspace", conversation_id=str(here.id),
            workspace_id="acme audit",
        )
        assert "Moved 1 conversation(s) into **Acme audit**" in moved, moved
        assert "takes effect from the next message" in moved, moved
        assert (await _reload(sessions, Conversation, here.id)).workspace_id == workspace.id

        listed = await _tool(alice, "list_workspaces", conversation_id=str(here.id))
        assert "**General**" in listed and "[default]" in listed, listed
        assert "this conversation is filed here" in listed, listed

        again = await _tool(
            alice, "move_conversations_to_workspace", conversation_id=str(here.id),
            workspace_id=str(workspace.id), conversation_ids="current",
        )
        assert "Already in **Acme audit**" in again, again

        unfiled = await _tool(
            alice, "move_conversations_to_workspace", conversation_id=str(here.id),
            workspace_id="none",
        )
        assert "out of their workspace" in unfiled, unfiled
        assert (await _reload(sessions, Conversation, here.id)).workspace_id is None


async def test_outside_a_conversation_the_ids_are_required() -> None:
    async with _database() as sessions:
        async with sessions() as s:
            alice = await _user(s, "alice")
            await create_workspace(s, user_id=alice.id, name="Research")
            await s.commit()
        answer = await _tool(alice, "move_conversations_to_workspace", workspace_id="Research")
        assert "Name the conversations to move" in answer, answer


async def test_only_the_owners_conversations_move_and_all_or_nothing() -> None:
    async with _database() as sessions:
        async with sessions() as s:
            alice = await _user(s, "alice")
            bob = await _user(s, "bob")
            workspace = await create_workspace(s, user_id=alice.id, name="Research")
            mine = await _conversation(s, alice, "Mine")
            theirs = await _conversation(s, bob, "Theirs")
            await s.commit()

        answer = await _tool(
            alice, "move_conversations_to_workspace", workspace_id=str(workspace.id),
            conversation_ids=f"{mine.id}, {theirs.id}",
        )
        assert "not found, or not yours" in answer and str(theirs.id) in answer, answer
        assert "Nothing was moved" in answer, answer
        assert (await _reload(sessions, Conversation, mine.id)).workspace_id is None
        assert (await _reload(sessions, Conversation, theirs.id)).workspace_id is None


async def test_a_workspace_the_caller_cannot_see_answers_like_an_unknown_one() -> None:
    async with _database() as sessions:
        async with sessions() as s:
            alice = await _user(s, "alice")
            bob = await _user(s, "bob")
            hidden = await create_workspace(s, user_id=bob.id, name="Bob private")
            mine = await _conversation(s, alice, "Mine")
            await s.commit()

        on_hidden = await _tool(
            alice, "move_conversations_to_workspace", workspace_id=str(hidden.id),
            conversation_ids=str(mine.id),
        )
        missing = uuid.uuid4()
        on_missing = await _tool(
            alice, "move_conversations_to_workspace", workspace_id=str(missing),
            conversation_ids=str(mine.id),
        )
        assert on_hidden.replace(str(hidden.id), "<id>") == on_missing.replace(str(missing), "<id>")
        assert "not found" in on_missing, on_missing
        # By name, a workspace the caller cannot see is simply not one of theirs.
        by_name = await _tool(
            alice, "move_conversations_to_workspace", workspace_id="Bob private",
            conversation_ids=str(mine.id),
        )
        assert "No workspace named" in by_name, by_name
        assert "not found" in await _tool(alice, "get_workspace", workspace_id=str(hidden.id))
        assert (await _reload(sessions, Conversation, mine.id)).workspace_id is None


async def test_a_read_only_grantee_reads_but_cannot_file() -> None:
    async with _database() as sessions:
        async with sessions() as s:
            alice = await _user(s, "alice")
            bob = await _user(s, "bob")
            shared = await create_workspace(s, user_id=bob.id, name="Team")
            shared.shared_user_ids = [str(alice.id)]
            shared.shared_user_permissions = {str(alice.id): "read_only"}
            mine = await _conversation(s, alice, "Mine")
            await s.commit()

        opened = await _tool(alice, "get_workspace", workspace_id=str(shared.id))
        assert "**Team**" in opened and "read it only" in opened, opened
        refused = await _tool(
            alice, "move_conversations_to_workspace", workspace_id=str(shared.id),
            conversation_ids=str(mine.id),
        )
        assert "don't have permission to use this workspace" in refused, refused
        assert (await _reload(sessions, Conversation, mine.id)).workspace_id is None


async def test_an_archived_workspace_takes_no_conversation_until_restored() -> None:
    async with _database() as sessions:
        async with sessions() as s:
            alice = await _user(s, "alice")
            workspace = await create_workspace(s, user_id=alice.id, name="Old")
            mine = await _conversation(s, alice, "Mine")
            await s.commit()

        assert "archived" in await _tool(
            alice, "update_workspace", workspace_id=str(workspace.id), archived=True
        )
        refused = await _tool(
            alice, "move_conversations_to_workspace", workspace_id=str(workspace.id),
            conversation_ids=str(mine.id),
        )
        assert "That workspace is archived." in refused, refused
        # Kwargs reach the handler verbatim: the string "false" restores it,
        # where `bool("false")` would have kept it archived.
        assert "restored" in await _tool(
            alice, "update_workspace", workspace_id=str(workspace.id), archived="false"
        )
        assert (await _reload(sessions, Workspace, workspace.id)).is_archived is False
        assert "must be 'true' or 'false'" in await _tool(
            alice, "update_workspace", workspace_id=str(workspace.id), archived="maybe"
        )
        assert "Moved 1" in await _tool(
            alice, "move_conversations_to_workspace", workspace_id=str(workspace.id),
            conversation_ids=str(mine.id),
        )


async def test_the_default_is_neither_archived_nor_deleted_and_can_change() -> None:
    async with _database() as sessions:
        async with sessions() as s:
            alice = await _user(s, "alice")
            default = await ensure_default_workspace(s, alice.id)
            other = await create_workspace(s, user_id=alice.id, name="Research")
            await s.commit()

        assert "cannot be archived" in await _tool(
            alice, "update_workspace", workspace_id=str(default.id), archived=True
        )
        assert "cannot be deleted" in await _tool(
            alice, "delete_workspace", workspace_id=str(default.id)
        )
        assert "now your default" in await _tool(
            alice, "set_default_workspace", workspace_id="Research"
        )
        assert (await _reload(sessions, Workspace, other.id)).is_default is True
        assert (await _reload(sessions, Workspace, default.id)).is_default is False
        assert "already your default" in await _tool(
            alice, "set_default_workspace", workspace_id=str(other.id)
        )


async def test_deleting_unfiles_conversations_and_is_refused_while_work_items_remain() -> None:
    async with _database() as sessions:
        async with sessions() as s:
            alice = await _user(s, "alice")
            default = await ensure_default_workspace(s, alice.id)
            doomed = await create_workspace(s, user_id=alice.id, name="Doomed")
            busy = await create_workspace(s, user_id=alice.id, name="Busy")
            thread = await _conversation(s, alice, "Thread", doomed)
            s.add(WorkItem(user_id=alice.id, title="tracked", workspace_id=busy.id))
            await s.commit()

        refused = await _tool(alice, "delete_workspace", workspace_id=str(busy.id))
        assert "still holds 1 work item" in refused, refused
        assert await _reload(sessions, Workspace, busy.id) is not None

        deleted = await _tool(alice, "delete_workspace", workspace_id="Doomed")
        assert "has been deleted" in deleted and "1 conversation(s)" in deleted, deleted
        assert "in no workspace" in deleted and "default" not in deleted, deleted
        assert await _reload(sessions, Workspace, doomed.id) is None
        assert (await _reload(sessions, Conversation, thread.id)).workspace_id is None
        assert (await _reload(sessions, Workspace, default.id)).is_default is True


async def test_knowledge_bases_are_bound_only_when_the_caller_sees_them() -> None:
    async with _database() as sessions:
        async with sessions() as s:
            alice = await _user(s, "alice")
            bob = await _user(s, "bob")
            workspace = await create_workspace(s, user_id=alice.id, name="Research")
            visible = KnowledgeBase(id=uuid.uuid4(), user_id=alice.id, name="Policies")
            hidden = KnowledgeBase(id=uuid.uuid4(), user_id=bob.id, name="Bob corpus")
            s.add_all([visible, hidden])
            await s.commit()

        refused = await _tool(
            alice, "update_workspace_knowledge_bases", workspace_id=str(workspace.id),
            add_knowledge_base_ids=f"{visible.id},{hidden.id}",
        )
        assert "not visible to you" in refused and "Nothing was changed" in refused, refused
        assert "Bob corpus" not in refused

        bound = await _tool(
            alice, "update_workspace_knowledge_bases", workspace_id=str(workspace.id),
            add_knowledge_base_ids=str(visible.id),
        )
        assert "1 knowledge base(s) bound" in bound and "Policies" in bound, bound
        assert "Policies" in await _tool(alice, "get_workspace", workspace_id="Research")

        unbound = await _tool(
            alice, "update_workspace_knowledge_bases", workspace_id=str(workspace.id),
            remove_knowledge_base_ids=str(visible.id),
        )
        assert "1 unbound" in unbound and "Now bound: none" in unbound, unbound
        async with sessions() as s:
            assert (
                await s.execute(
                    select(WorkspaceKnowledgeBase).where(
                        WorkspaceKnowledgeBase.workspace_id == workspace.id
                    )
                )
            ).first() is None
            assert await s.get(KnowledgeBase, visible.id) is not None


async def test_list_conversations_filters_the_callers_own_threads() -> None:
    async with _database() as sessions:
        async with sessions() as s:
            alice = await _user(s, "alice")
            bob = await _user(s, "bob")
            workspace = await create_workspace(s, user_id=alice.id, name="Research")
            filed = await _conversation(s, alice, "Acme findings", workspace)
            loose = await _conversation(s, alice, "Lunch plans")
            await _conversation(s, bob, "Acme from bob")
            await s.commit()

        everything = await _tool(alice, "list_conversations", conversation_id=str(loose.id))
        assert "Acme findings" in everything and "Lunch plans" in everything, everything
        assert "Acme from bob" not in everything, everything
        assert "in **Research**" in everything and "<- this conversation" in everything

        only_filed = await _tool(alice, "list_conversations", workspace_id="Research")
        assert str(filed.id) in only_filed and str(loose.id) not in only_filed, only_filed

        only_unfiled = await _tool(alice, "list_conversations", unfiled_only=True)
        assert str(loose.id) in only_unfiled and str(filed.id) not in only_unfiled

        searched = await _tool(alice, "list_conversations", search="acme")
        assert str(filed.id) in searched and str(loose.id) not in searched, searched
        # A LIKE wildcard in the search is text, never "everything".
        assert "No conversation" in await _tool(alice, "list_conversations", search="%")


async def test_update_clears_the_optional_fields_like_rest_and_omitted_keeps_them() -> None:
    """An empty description or colour clears it and an empty icon puts the
    default back, as on `PUT /workspaces/{id}`; an omitted field is left."""
    from app.models.workspace import WORKSPACE_DEFAULT_ICON

    async with _database() as sessions:
        async with sessions() as s:
            alice = await _user(s, "alice")
            workspace = await create_workspace(s, user_id=alice.id, name="Research")
            await s.commit()

        styled = await _tool(
            alice, "update_workspace", workspace_id=str(workspace.id),
            description="CTI threads", icon="Shield", color="#00d4ff",
        )
        assert "updated" in styled, styled

        renamed = await _tool(alice, "update_workspace", workspace_id="Research", name="Research Q4")
        assert "name" in renamed, renamed
        kept = await _reload(sessions, Workspace, workspace.id)
        assert (kept.description, kept.icon, kept.color) == ("CTI threads", "Shield", "#00d4ff")

        cleared = await _tool(
            alice, "update_workspace", workspace_id=str(workspace.id),
            description="", icon="", color="",
        )
        assert "description cleared" in cleared and "default icon" in cleared, cleared
        after = await _reload(sessions, Workspace, workspace.id)
        assert (after.description, after.icon, after.color) == ("", WORKSPACE_DEFAULT_ICON, "")

        again = await _tool(alice, "update_workspace", workspace_id=str(workspace.id), color="")
        assert "nothing changed" in again, again


async def test_a_workspace_id_no_longer_visible_matches_nothing_like_its_name() -> None:
    """A share revoked: the caller's own thread is still filed there, but the
    workspace answers by id as by name - nothing matches - and the unfiltered
    list says where it is."""
    async with _database() as sessions:
        async with sessions() as s:
            alice = await _user(s, "alice")
            bob = await _user(s, "bob")
            shared = await create_workspace(s, user_id=bob.id, name="Bob's")
            shared.shared_user_ids = [str(alice.id)]
            shared.shared_user_permissions = {str(alice.id): "config"}
            thread = await _conversation(s, alice, "Filed while shared", shared)
            await s.commit()

        listed = await _tool(alice, "list_conversations", workspace_id=str(shared.id))
        assert "Filed while shared" in listed, listed

        async with sessions() as s:
            revoked = await s.get(Workspace, shared.id)
            revoked.shared_user_ids = []
            revoked.shared_user_permissions = {}
            await s.commit()

        by_id = await _tool(alice, "list_conversations", workspace_id=str(shared.id))
        assert by_id == "No conversation of yours matches.", by_id
        by_name = await _tool(alice, "list_conversations", workspace_id="Bob's")
        assert "No workspace named" in by_name, by_name
        unfiltered = await _tool(alice, "list_conversations")
        assert "Filed while shared" in unfiltered and "a workspace you no longer see" in unfiltered
        assert (await _reload(sessions, Conversation, thread.id)).workspace_id == shared.id


async def test_a_workspace_named_like_an_unfile_word_is_never_unfiled_silently() -> None:
    async with _database() as sessions:
        async with sessions() as s:
            alice = await _user(s, "alice")
            named_none = await create_workspace(s, user_id=alice.id, name="None")
            filed = await create_workspace(s, user_id=alice.id, name="Research")
            thread = await _conversation(s, alice, "Thread", filed)
            await s.commit()

        refused = await _tool(
            alice, "move_conversations_to_workspace",
            workspace_id="None", conversation_ids=str(thread.id),
        )
        assert str(named_none.id) in refused and "Nothing was moved" in refused, refused
        assert "workspace_id null" in refused, "the refusal names the unambiguous unfile value"
        assert (await _reload(sessions, Conversation, thread.id)).workspace_id == filed.id

        unfiled = await _tool(
            alice,
            "move_conversations_to_workspace",
            workspace_id=None,
            conversation_ids=str(thread.id),
        )
        assert "out of their workspace" in unfiled, unfiled
        assert (await _reload(sessions, Conversation, thread.id)).workspace_id is None


async def test_removing_a_knowledge_base_the_caller_cannot_see_says_so() -> None:
    async with _database() as sessions:
        async with sessions() as s:
            alice = await _user(s, "alice")
            bob = await _user(s, "bob")
            workspace = await create_workspace(s, user_id=alice.id, name="Research")
            hidden = KnowledgeBase(id=uuid.uuid4(), user_id=bob.id, name="Bob corpus")
            s.add(hidden)
            await s.flush()
            s.add(WorkspaceKnowledgeBase(workspace_id=workspace.id, knowledge_base_id=hidden.id))
            await s.commit()

        answer = await _tool(
            alice, "update_workspace_knowledge_bases", workspace_id=str(workspace.id),
            remove_knowledge_base_ids=str(hidden.id),
        )
        assert "or not visible to you" in answer, answer
        assert "Bob corpus" not in answer
