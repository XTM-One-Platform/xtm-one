"""Filing a conversation into a workspace by asking the agent.

Spec 032 FR-027b and FR-022: ``file_this_conversation`` files the turn's own
conversation under the workspace tools' rules, and into a workspace other
people can see only a call the person approved on the approval card - the
consent screen ``install_filing_consent`` puts on the turn's executor, whatever
the agent's approval settings. Where nothing screens, such a target is refused.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

import app.models  # noqa: F401 - registers every mapper on Base.metadata
from app.models.chat import Conversation
from app.models.user import User
from app.models.workspace import Workspace
from app.services.builtin_tools.workspace_filing import (
    build_workspace_filing_tools,
    install_filing_consent,
    private_filing_targets,
)
from app.tools.base import CompositeToolExecutor, HumanInputRequestedError, ToolCall
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration


async def _seed(session):
    owner = User(id=uuid.uuid4(), email="owner@test.local", full_name="Owner", hashed_password="x")
    colleague = User(id=uuid.uuid4(), email="colleague@test.local", full_name="Colleague", hashed_password="x")
    session.add_all([owner, colleague])
    await session.flush()
    private = Workspace(id=uuid.uuid4(), user_id=owner.id, name="Mine", slug="mine")
    shared = Workspace(
        id=uuid.uuid4(),
        user_id=owner.id,
        name="Team",
        slug="team",
        shared_user_ids=[str(colleague.id)],
        shared_user_permissions={str(colleague.id): "read_only"},
    )
    archived = Workspace(id=uuid.uuid4(), user_id=owner.id, name="Old", slug="old", is_archived=True)
    theirs = Workspace(
        id=uuid.uuid4(),
        user_id=colleague.id,
        name="Theirs",
        slug="theirs",
        shared_user_ids=[str(owner.id)],
        shared_user_permissions={str(owner.id): "read_only"},
    )
    conversation = Conversation(id=uuid.uuid4(), user_id=owner.id, title="Plan")
    session.add_all([private, shared, archived, theirs, conversation])
    await session.commit()
    return SimpleNamespace(
        owner_id=str(owner.id),
        colleague_id=str(colleague.id),
        private_id=str(private.id),
        shared_id=str(shared.id),
        archived_id=str(archived.id),
        theirs_id=str(theirs.id),
        conversation_id=conversation.id,
    )


def _file(user_id: str, conversation_id):
    _defs, executor = build_workspace_filing_tools(
        user_id, conversation_id=str(conversation_id) if conversation_id else None
    )
    return executor._handlers["file_this_conversation"]


async def _filed_in(session, conversation_id):
    session.expire_all()
    return (await session.get(Conversation, conversation_id)).workspace_id


@pytest.mark.asyncio
async def test_the_suite_offers_three_tools_or_one_beside_the_agent_builder():
    defs, _ = build_workspace_filing_tools(str(uuid.uuid4()), conversation_id=None)
    assert [d.name for d in defs] == ["list_workspaces", "create_workspace", "file_this_conversation"]
    defs, _ = build_workspace_filing_tools(str(uuid.uuid4()), conversation_id=None, with_listing=False)
    assert [d.name for d in defs] == ["file_this_conversation"]


@pytest.mark.asyncio
async def test_a_private_workspace_files_at_once():
    async with scratch_database("wsfile", tables=ALL_TABLES, extensions=("vector",)) as db, db.sessions() as session:
        world = await _seed(session)
        out = await _file(world.owner_id, world.conversation_id)(workspace_id=world.private_id)
        assert "Moved 1 conversation(s) into **Mine**" in out
        assert str(await _filed_in(session, world.conversation_id)) == world.private_id


async def _screened(session, user_id: str, conversation_id, *, approved: set[str] | None = None):
    """The filing suite on a composite carrying the turn's consent screen."""
    defs, executor = build_workspace_filing_tools(user_id, conversation_id=str(conversation_id))
    composite = CompositeToolExecutor()
    composite.add_tools(defs, executor)
    install_filing_consent(composite, user_id, can_ask=True, approved_call_ids=approved)
    # What the agent loop awaits before screening a batch.
    await composite.prepare_consent([_call("any")])
    return composite


def _call(workspace_id: str, call_id: str = "call-1") -> ToolCall:
    return ToolCall(id=call_id, name="file_this_conversation", arguments={"workspace_id": workspace_id})


@pytest.mark.asyncio
async def test_where_nothing_screens_a_shared_workspace_is_refused():
    """An MCP client, a run or a sub-agent asks nobody: no argument files it."""
    async with scratch_database("wsfile", tables=ALL_TABLES, extensions=("vector",)) as db, db.sessions() as session:
        world = await _seed(session)
        out = await _file(world.owner_id, world.conversation_id)(workspace_id="Team")
        assert out.startswith("Nothing was moved. The workspace 'Team' is shared with other people")
        assert await _filed_in(session, world.conversation_id) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["shared_id", "Team", "theirs_id", "No such workspace"])
async def test_a_call_naming_anything_but_a_private_workspace_pauses_for_approval(target):
    async with scratch_database("wsfile", tables=ALL_TABLES, extensions=("vector",)) as db, db.sessions() as session:
        world = await _seed(session)
        composite = await _screened(session, world.owner_id, world.conversation_id)
        ref = getattr(world, target, target)

        proposals = composite.screen_for_approval([_call(ref)])
        assert [(p["tool_call_id"], p["tool_name"], p["arguments"]) for p in proposals] == [
            ("call-1", "file_this_conversation", {"workspace_id": ref})
        ]
        with pytest.raises(HumanInputRequestedError):
            await composite.execute_tool(_call(ref))
        assert await _filed_in(session, world.conversation_id) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["private_id", "Mine", "mine"])
async def test_a_private_workspace_files_without_asking(target):
    async with scratch_database("wsfile", tables=ALL_TABLES, extensions=("vector",)) as db, db.sessions() as session:
        world = await _seed(session)
        composite = await _screened(session, world.owner_id, world.conversation_id)
        ref = getattr(world, target, target)

        assert composite.screen_for_approval([_call(ref)]) == []
        result = await composite.execute_tool(_call(ref))
        assert "into **Mine**" in result.content
        assert str(await _filed_in(session, world.conversation_id)) == world.private_id


@pytest.mark.asyncio
async def test_an_approved_call_files_into_the_shared_workspace():
    """Mid-turn: the approval puts the call on the bypass, as the loop does."""
    async with scratch_database("wsfile", tables=ALL_TABLES, extensions=("vector",)) as db, db.sessions() as session:
        world = await _seed(session)
        composite = await _screened(session, world.owner_id, world.conversation_id)
        composite.bypass_approval_for({"call-1"})

        result = await composite.execute_tool(_call(world.shared_id))
        assert "into **Team**" in result.content
        assert str(await _filed_in(session, world.conversation_id)) == world.shared_id


@pytest.mark.asyncio
async def test_a_resumed_turn_runs_the_call_approved_before_it():
    """Resumed from the persisted proposal: the approved id, and only it."""
    async with scratch_database("wsfile", tables=ALL_TABLES, extensions=("vector",)) as db, db.sessions() as session:
        world = await _seed(session)
        composite = await _screened(session, world.owner_id, world.conversation_id, approved={"call-1"})

        with pytest.raises(HumanInputRequestedError):
            await composite.execute_tool(_call(world.shared_id, call_id="call-2"))
        result = await composite.execute_tool(_call(world.shared_id))
        assert "into **Team**" in result.content
        assert str(await _filed_in(session, world.conversation_id)) == world.shared_id


@pytest.mark.asyncio
async def test_an_approval_is_spent_by_the_call_it_was_given_for():
    """A provider reusing call ids (``call_<name>_<n>``) does not ride a later
    filing on an earlier approval."""
    async with scratch_database("wsfile", tables=ALL_TABLES, extensions=("vector",)) as db, db.sessions() as session:
        world = await _seed(session)
        composite = await _screened(session, world.owner_id, world.conversation_id)
        composite.bypass_approval_for({"call-1"})
        assert "into **Team**" in (await composite.execute_tool(_call(world.shared_id))).content

        conv = await session.get(Conversation, world.conversation_id)
        conv.workspace_id = None
        await session.commit()
        (proposal,) = composite.screen_for_approval([_call(world.shared_id)])
        assert proposal["requires_consent"] is True
        with pytest.raises(HumanInputRequestedError):
            await composite.execute_tool(_call(world.shared_id))
        assert await _filed_in(session, world.conversation_id) is None


@pytest.mark.asyncio
async def test_a_name_the_screen_cleared_is_still_refused_when_it_resolves_to_a_shared_workspace():
    """The screen folds a name in Python, the tool resolves it in SQL: when they
    disagree, the tool decides on the workspace it resolved, and asks for its id."""
    async with scratch_database("wsfile", tables=ALL_TABLES, extensions=("vector",)) as db, db.sessions() as session:
        world = await _seed(session)
        owner, colleague = uuid.UUID(world.owner_id), uuid.UUID(world.colleague_id)
        session.add(Workspace(id=uuid.uuid4(), user_id=owner, name="Straße", slug="own-street"))
        lookalike = Workspace(
            id=uuid.uuid4(),
            user_id=colleague,
            name="strasse",
            slug="their-street",
            shared_user_ids=[world.owner_id],
            shared_user_permissions={world.owner_id: "config"},
        )
        session.add(lookalike)
        await session.commit()
        composite = await _screened(session, world.owner_id, world.conversation_id)

        assert composite.screen_for_approval([_call("strasse")]) == []
        result = await composite.execute_tool(_call("strasse"))
        assert result.content == (
            "Nothing was moved. The workspace 'strasse' is shared with other people, who could "
            f"then search these conversations: call again with its id `{lookalike.id}` so the "
            "person is asked to approve the move."
        )
        assert await _filed_in(session, world.conversation_id) is None


@pytest.mark.asyncio
async def test_a_screened_turn_does_not_vouch_for_a_nested_executor():
    """A sub-agent's executor dispatched from inside a screened call screens
    nothing of its own: its move into a shared workspace is refused."""
    async with scratch_database("wsfile", tables=ALL_TABLES, extensions=("vector",)) as db, db.sessions() as session:
        world = await _seed(session)
        inner_defs, inner_exec = build_workspace_filing_tools(world.owner_id, conversation_id=str(world.conversation_id))
        nested = CompositeToolExecutor()
        nested.add_tools(inner_defs, inner_exec)

        from app.tools.base import CallableToolExecutor, ToolDefinition

        async def _delegate() -> str:
            result = await nested.execute_tool(_call(world.shared_id, call_id="inner"))
            return result.content

        outer_exec = CallableToolExecutor()
        outer_exec.register("file_this_conversation", _delegate)
        outer = CompositeToolExecutor()
        outer.add_tools(
            [ToolDefinition(name="file_this_conversation", description="", input_schema={"type": "object"})],
            outer_exec,
        )
        install_filing_consent(outer, world.owner_id, can_ask=True)
        outer.bypass_approval_for({"outer"})

        result = await outer.execute_tool(ToolCall(id="outer", name="file_this_conversation", arguments={}))
        assert result.content.startswith("Nothing was moved. The workspace 'Team' is shared")
        assert await _filed_in(session, world.conversation_id) is None


@pytest.mark.asyncio
async def test_the_private_workspaces_are_read_only_once_a_filing_call_is_screened(monkeypatch):
    """A turn that offers the tools and files nothing reads nothing; until the
    read, a call naming a workspace asks."""
    from unittest.mock import AsyncMock

    from app.services.builtin_tools import workspace_filing

    read = AsyncMock(return_value=frozenset({"mine"}))
    monkeypatch.setattr(workspace_filing, "private_filing_targets", read)
    defs, executor = build_workspace_filing_tools(str(uuid.uuid4()), conversation_id=str(uuid.uuid4()))
    composite = CompositeToolExecutor()
    composite.add_tools(defs, executor)
    install_filing_consent(composite, str(uuid.uuid4()), can_ask=True)
    read.assert_not_awaited()

    assert [p["tool_call_id"] for p in composite.screen_for_approval([_call("mine")])] == ["call-1"]
    await composite.prepare_consent([_call("mine"), _call("Team", call_id="call-2")])
    await composite.prepare_consent([_call("mine")])
    assert read.await_count == 1
    assert composite.screen_for_approval([_call("mine")]) == []


@pytest.mark.asyncio
async def test_private_targets_are_own_unshared_workspaces_named_once():
    async with scratch_database("wsfile", tables=ALL_TABLES, extensions=("vector",)) as db, db.sessions() as session:
        world = await _seed(session)
        owner = uuid.UUID(world.owner_id)
        # A second own workspace named like the first: the name is ambiguous.
        twin = Workspace(id=uuid.uuid4(), user_id=owner, name="Twin", slug="twin-a")
        twin_too = Workspace(id=uuid.uuid4(), user_id=owner, name="twin", slug="twin-b")
        session.add_all([twin, twin_too])
        await session.commit()

        targets = await private_filing_targets(session, owner)
        assert targets == frozenset(
            {
                "none",
                world.private_id,
                "mine",
                world.archived_id,
                "old",
                str(twin.id),
                str(twin_too.id),
                "twin-a",
                "twin-b",
            }
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ("archived_id", "That workspace is archived."),
        ("theirs_id", "You don't have permission to use this workspace."),
    ],
)
async def test_a_workspace_the_person_cannot_file_into_is_refused(target, expected):
    async with scratch_database("wsfile", tables=ALL_TABLES, extensions=("vector",)) as db, db.sessions() as session:
        world = await _seed(session)
        out = await _file(world.owner_id, world.conversation_id)(workspace_id=getattr(world, target))
        assert expected in out
        assert await _filed_in(session, world.conversation_id) is None


@pytest.mark.asyncio
async def test_someone_elses_conversation_is_not_filed():
    async with scratch_database("wsfile", tables=ALL_TABLES, extensions=("vector",)) as db, db.sessions() as session:
        world = await _seed(session)
        out = await _file(world.colleague_id, world.conversation_id)(workspace_id=world.theirs_id)
        assert "not found, or not yours" in out
        assert await _filed_in(session, world.conversation_id) is None


@pytest.mark.asyncio
async def test_a_channel_thread_not_recorded_yet_is_told_to_ask_again():
    async with scratch_database("wsfile", tables=ALL_TABLES, extensions=("vector",)) as db, db.sessions() as session:
        world = await _seed(session)
        out = await _file(world.owner_id, None)(workspace_id=world.private_id)
        assert out.startswith("This conversation is not recorded yet")


@pytest.mark.asyncio
async def test_an_anonymous_participant_files_nothing():
    out = await _file("anonymous", uuid.uuid4())(workspace_id="Mine")
    assert out == "Workspace management is not available for anonymous users."


@pytest.mark.asyncio
async def test_a_group_channel_thread_is_not_filed():
    async with scratch_database("wsfile", tables=ALL_TABLES, extensions=("vector",)) as db, db.sessions() as session:
        world = await _seed(session)
        conv = await session.get(Conversation, world.conversation_id)
        conv.channel, conv.channel_thread_id = "slack", "C0123/1700000000.000100"
        await session.commit()
        out = await _file(world.owner_id, world.conversation_id)(workspace_id=world.private_id)
        assert out.startswith("This is a channel thread other people read")
        assert await _filed_in(session, world.conversation_id) is None


@pytest.mark.asyncio
async def test_the_builder_move_tool_is_screened_the_same_way():
    """The agent-builder tool files any of the person's conversations: it
    pauses on the same screen, and refuses where nothing screens (FR-022)."""
    from app.services.builtin_tools.workspaces import build_workspace_tools
    from app.tools.base import CallableToolExecutor

    async with scratch_database("wsfile", tables=ALL_TABLES, extensions=("vector",)) as db, db.sessions() as session:
        world = await _seed(session)
        defs, handlers = build_workspace_tools(world.owner_id)
        args = {"workspace_id": world.shared_id, "conversation_ids": str(world.conversation_id)}

        refused = await handlers["move_conversations_to_workspace"](**args)
        assert refused.startswith("Nothing was moved. The workspace 'Team' is shared")
        assert await _filed_in(session, world.conversation_id) is None

        executor = CallableToolExecutor()
        executor.register("move_conversations_to_workspace", handlers["move_conversations_to_workspace"])
        composite = CompositeToolExecutor()
        composite.add_tools([d for d in defs if d.name == "move_conversations_to_workspace"], executor)
        install_filing_consent(composite, world.owner_id, can_ask=True)
        call = ToolCall(id="move-1", name="move_conversations_to_workspace", arguments=args)

        assert [p["tool_call_id"] for p in composite.screen_for_approval([call])] == ["move-1"]
        composite.bypass_approval_for({"move-1"})
        moved = await composite.execute_tool(call)
        assert "into **Team**" in moved.content
        assert str(await _filed_in(session, world.conversation_id)) == world.shared_id
