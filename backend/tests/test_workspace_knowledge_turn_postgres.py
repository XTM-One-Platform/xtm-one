"""A chat turn in a filed conversation receives its workspace's knowledge.

Spec 032 US1, US2: instructions, sources (inline or searched), bound knowledge
bases and linked skills reach ``run_agent_chat`` - for the person the turn acts
for, after any session replacement, and never outside the workspace. The turn
runs against PostgreSQL with only the model's side stubbed (the harness of
``test_memory_quarantine_chat_postgres.py``); it stops at the MCP loader, after
the system prompt and the knowledge and skill tools are built, and the
assertions read the prompt and the builders' arguments.
"""

from __future__ import annotations

import contextlib
import uuid
from unittest.mock import MagicMock

import pytest

import app.models  # noqa: F401 - registers every mapper on Base.metadata
from app.models.agent import Agent
from app.models.agent_skill import AgentSkill
from app.models.chat import Conversation
from app.models.knowledge_base import KnowledgeBase, KnowledgeDocument
from app.models.platform_settings import PlatformSettings
from app.models.user import User
from app.models.workspace import Workspace, WorkspaceKnowledgeBase, WorkspaceSkill
from app.services.content_safety import UNTRUSTED_CONTENT_POLICY, untrusted_block_spans
from app.services.workspaces import WORKSPACE_KB_SOURCES, ensure_workspace_kb
from tests.scratch_database import ALL_TABLES, scratch_database
from tests.test_memory_quarantine_chat_postgres import _LLM, _PERSONA, _StopTurnError, _stub_turn

pytestmark = pytest.mark.integration

_FACT = "The launch code name is Bluebird."
_INSTRUCTIONS = "Always end with the word ACK."


async def _seed(session, *, source_text: str = _FACT, percent: int = 15):
    owner = User(id=uuid.uuid4(), email="owner@test.local", full_name="Owner", hashed_password="x")
    reader = User(id=uuid.uuid4(), email="reader@test.local", full_name="Reader", hashed_password="x")
    session.add_all([owner, reader])
    await session.flush()
    session.add(PlatformSettings(workspace_sources_context_percent=percent))
    agent = Agent(
        id=uuid.uuid4(),
        name="Desk",
        slug=f"desk-{uuid.uuid4().hex[:6]}",
        persona=_PERSONA,
        enabled=True,
        user_id=owner.id,
        is_company_managed=True,
        builtin_tool_ids=[],
        handover_agent_ids=[],
        integration_bindings={},
        mcp_server_ids=[],
        custom_tool_ids=[],
        knowledge_base_ids=[],
        skill_ids=[],
        output_format="markdown",
    )
    workspace = Workspace(
        id=uuid.uuid4(),
        user_id=owner.id,
        name="Launch",
        slug="launch",
        instructions=_INSTRUCTIONS,
        shared_user_ids=[str(reader.id)],
        shared_user_permissions={str(reader.id): "read_only"},
    )
    session.add_all([agent, workspace])
    await session.flush()
    sources = await ensure_workspace_kb(session, workspace, WORKSPACE_KB_SOURCES)
    session.add(
        KnowledgeDocument(
            knowledge_base_id=sources.id,
            filename="brief.md",
            source_type="markdown",
            content=source_text,
            content_hash="h",
            status="ready",
        )
    )
    # Bound and linked by the owner, private to the owner.
    bound = KnowledgeBase(id=uuid.uuid4(), user_id=owner.id, name="Owner notes")
    skill = AgentSkill(id=uuid.uuid4(), user_id=owner.id, name="launch-checklist", content="Check the list.")
    session.add_all([bound, skill])
    await session.flush()
    session.add_all(
        [
            WorkspaceKnowledgeBase(workspace_id=workspace.id, knowledge_base_id=bound.id),
            WorkspaceSkill(workspace_id=workspace.id, skill_id=skill.id),
        ]
    )
    filed = Conversation(id=uuid.uuid4(), user_id=owner.id, workspace_id=workspace.id)
    filed_by_reader = Conversation(id=uuid.uuid4(), user_id=reader.id, workspace_id=workspace.id)
    unfiled = Conversation(id=uuid.uuid4(), user_id=owner.id)
    session.add_all([filed, filed_by_reader, unfiled])
    await session.commit()
    return {
        "owner": owner,
        "reader": reader,
        "agent": agent,
        "workspace": workspace,
        "sources": sources,
        "bound": bound,
        "skill": skill,
        "filed": filed,
        "filed_by_reader": filed_by_reader,
        "unfiled": unfiled,
    }


async def _turn(monkeypatch, session, world, *, user, conversation=None, **kwargs):
    """Run one turn to the MCP loader; return (system prompt, KB ids searched)."""
    from app.services.agent_chat import run_agent_chat

    measured = _stub_turn(monkeypatch)
    search = MagicMock(return_value=None)
    monkeypatch.setattr("app.services.builtin_tools.build_kb_search_tool", search)
    with pytest.raises(_StopTurnError):
        await run_agent_chat(
            session,
            str(user.id),
            world["agent"],
            [],
            "What is the code name?",
            conversation_id=str(conversation.id) if conversation is not None else None,
            meter_quota=False,
            prebuilt_llm=_LLM,
            **kwargs,
        )
    prompts = [text for text in measured if isinstance(text, str) and text.startswith(_PERSONA)]
    assert len(prompts) == 1
    searched = list(search.call_args.args[2]) if search.call_args else []
    return prompts[0], searched


@pytest.mark.asyncio
async def test_a_filed_turn_receives_instructions_sources_bound_kbs_and_skills(monkeypatch):
    async with scratch_database("wsturn", tables=ALL_TABLES, extensions=("vector",)) as db, db.sessions() as session:
        world = await _seed(session)
        prompt, searched = await _turn(monkeypatch, session, world, user=world["owner"], conversation=world["filed"])

    assert prompt.count(_INSTRUCTIONS) == 1
    # The source is inline, inside a quarantine block, and the policy that
    # explains those blocks is present.
    spans = untrusted_block_spans(prompt)
    assert any(_FACT in prompt[s:e] for s, e in spans)
    assert UNTRUSTED_CONTENT_POLICY in prompt
    # Inline sources are not searched as well; the bound KB is, read-only.
    assert searched == [str(world["bound"].id)]
    assert "launch-checklist" in prompt


@pytest.mark.asyncio
async def test_sources_over_the_budget_are_searched_never_inlined(monkeypatch):
    big = _FACT + " " + ("filler " * 40_000)
    async with scratch_database("wsturn", tables=ALL_TABLES, extensions=("vector",)) as db, db.sessions() as session:
        world = await _seed(session, source_text=big, percent=5)
        prompt, searched = await _turn(monkeypatch, session, world, user=world["owner"], conversation=world["filed"])

    assert _FACT not in prompt
    assert searched == [str(world["bound"].id), str(world["sources"].id)]


@pytest.mark.asyncio
async def test_an_unfiled_conversation_receives_nothing_from_the_workspace(monkeypatch):
    async with scratch_database("wsturn", tables=ALL_TABLES, extensions=("vector",)) as db, db.sessions() as session:
        world = await _seed(session)
        prompt, searched = await _turn(monkeypatch, session, world, user=world["owner"], conversation=world["unfiled"])

    assert _INSTRUCTIONS not in prompt
    assert _FACT not in prompt
    assert "launch-checklist" not in prompt
    assert searched == []


@pytest.mark.asyncio
async def test_a_reader_gets_the_workspace_but_not_what_only_the_owner_can_see(monkeypatch):
    async with scratch_database("wsturn", tables=ALL_TABLES, extensions=("vector",)) as db, db.sessions() as session:
        world = await _seed(session)
        prompt, searched = await _turn(
            monkeypatch, session, world, user=world["reader"], conversation=world["filed_by_reader"]
        )

    assert prompt.count(_INSTRUCTIONS) == 1
    assert _FACT in prompt
    # The owner's private bound KB and linked skill are not the reader's.
    assert searched == []
    assert "launch-checklist" not in prompt


@pytest.mark.asyncio
async def test_a_revoked_reader_gets_nothing_on_the_next_turn(monkeypatch):
    async with scratch_database("wsturn", tables=ALL_TABLES, extensions=("vector",)) as db, db.sessions() as session:
        world = await _seed(session)
        world["workspace"].shared_user_ids = []
        world["workspace"].shared_user_permissions = {}
        await session.commit()
        prompt, searched = await _turn(
            monkeypatch, session, world, user=world["reader"], conversation=world["filed_by_reader"]
        )

    assert _INSTRUCTIONS not in prompt
    assert _FACT not in prompt
    assert searched == []


@pytest.mark.asyncio
async def test_a_consulted_turn_receives_the_parents_workspace(monkeypatch):
    """A consult runs with no conversation: the workspace comes from the
    parent turn through ``session_workspace_id``."""
    async with scratch_database("wsturn", tables=ALL_TABLES, extensions=("vector",)) as db, db.sessions() as session:
        world = await _seed(session)
        prompt, _searched = await _turn(
            monkeypatch, session, world, user=world["owner"], session_workspace_id=world["workspace"].id
        )

    assert prompt.count(_INSTRUCTIONS) == 1


@pytest.mark.asyncio
async def test_a_session_replacement_keeps_the_workspace_entries(monkeypatch):
    """A flow's capability nodes replace the agent's own lists, never the
    workspace's."""
    async with scratch_database("wsturn", tables=ALL_TABLES, extensions=("vector",)) as db, db.sessions() as session:
        world = await _seed(session)
        prompt, searched = await _turn(
            monkeypatch,
            session,
            world,
            user=world["owner"],
            conversation=world["filed"],
            session_knowledge_base_ids=[],
            session_skill_ids=[],
        )

    assert searched == [str(world["bound"].id)]
    assert "launch-checklist" in prompt


@pytest.mark.asyncio
async def test_the_general_assistant_receives_the_same_knowledge(monkeypatch):
    """The General Assistant runs its own loop: the workspace reaches it
    through the same resolver, for the same person (FR-027)."""
    from unittest.mock import AsyncMock

    from app.services.general_chat import run_general_chat

    _stub_turn(monkeypatch)
    monkeypatch.setattr("app.services.builtin_tools.load_custom_and_mcp_tools", AsyncMock(return_value=None))
    search = MagicMock(return_value=None)
    monkeypatch.setattr("app.services.builtin_tools.build_kb_search_tool", search)
    loop = AsyncMock(side_effect=_StopTurnError)
    monkeypatch.setattr("app.services.general_chat.run_agent_loop", loop)
    big = _FACT + " " + ("filler " * 40_000)
    async with scratch_database("wsturn", tables=ALL_TABLES, extensions=("vector",)) as db, db.sessions() as session:
        world = await _seed(session, source_text=big, percent=5)
        # The General Assistant may answer its loop's failure itself: what is
        # asserted is what the loop was handed.
        with contextlib.suppress(_StopTurnError):
            await run_general_chat(
                session,
                str(world["owner"].id),
                [],
                "What is the code name?",
                conversation_id=str(world["filed"].id),
                prebuilt_llm=_LLM,
            )

    system = loop.await_args.kwargs["messages"][0].content
    assert system.count(_INSTRUCTIONS) == 1
    assert "launch-checklist" in system
    assert list(search.call_args.args[2]) == [str(world["bound"].id), str(world["sources"].id)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("channel", "thread", "receives"),
    [
        ("slack", "C0123/1700000000.000100", False),  # a channel thread: read by the whole channel
        ("teams", "19:abc@thread.tacv2", False),
        ("slack", "D0456", True),  # a direct message with the bot
        ("api", None, True),  # the embedded platform chat
    ],
)
async def test_only_a_persons_own_thread_receives_the_workspace(monkeypatch, channel, thread, receives):
    async with scratch_database("wsturn", tables=ALL_TABLES, extensions=("vector",)) as db, db.sessions() as session:
        world = await _seed(session)
        world["filed"].channel = channel
        world["filed"].channel_thread_id = thread
        await session.commit()
        prompt, _searched = await _turn(monkeypatch, session, world, user=world["owner"], conversation=world["filed"])

    assert (_INSTRUCTIONS in prompt) is receives


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "offered"),
    [("filed", False), ("unfiled", True), ("consult", False)],
)
async def test_the_filing_suite_is_offered_in_an_unfiled_conversation_only(monkeypatch, case, offered):
    from app.services.builtin_tools import workspace_filing

    real = workspace_filing.build_workspace_filing_tools
    calls: list[dict] = []

    def _spy(user_id, **kwargs):
        calls.append(kwargs)
        return real(user_id, **kwargs)

    monkeypatch.setattr(workspace_filing, "build_workspace_filing_tools", _spy)
    async with scratch_database("wsturn", tables=ALL_TABLES, extensions=("vector",)) as db, db.sessions() as session:
        world = await _seed(session)
        kwargs = (
            {"session_workspace_id": world["workspace"].id}
            if case == "consult"
            else {"conversation": world[case]}
        )
        await _turn(monkeypatch, session, world, user=world["owner"], **kwargs)

    if offered:
        assert calls == [{"conversation_id": str(world[case].id), "with_listing": True}]
    else:
        assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("surface", "asks"),
    [("agent_chat", True), ("channel", False), ("general_chat", True)],
)
async def test_a_turn_asks_the_person_before_filing_into_a_shared_workspace(monkeypatch, surface, asks):
    """FR-022 on the executor the loop is handed, approvals switched off: a
    filing call into a workspace other people see pauses for the person, one
    into their own private workspace does not. An agent turn that cannot
    collect an approval (a channel thread) screens nothing: the tool refuses."""
    from unittest.mock import AsyncMock

    from app.models.workspace import Workspace
    from app.services.agent_chat import run_agent_chat
    from app.services.general_chat import run_general_chat
    from app.tools.base import ToolCall

    _stub_turn(monkeypatch)
    monkeypatch.setattr("app.services.builtin_tools.load_custom_and_mcp_tools", AsyncMock(return_value=None))
    monkeypatch.setattr("app.services.hitl_whitelist.resolve_approve_tool_calls", AsyncMock(return_value=False))
    loop = AsyncMock(side_effect=_StopTurnError)
    monkeypatch.setattr(f"app.services.{'general_chat' if surface == 'general_chat' else 'agent_chat'}.run_agent_loop", loop)
    async with scratch_database("wsturn", tables=ALL_TABLES, extensions=("vector",)) as db, db.sessions() as session:
        world = await _seed(session)
        session.add(Workspace(id=uuid.uuid4(), user_id=world["owner"].id, name="Solo", slug="solo"))
        await session.commit()
        common = {"conversation_id": str(world["unfiled"].id), "prebuilt_llm": _LLM}
        with contextlib.suppress(_StopTurnError):
            if surface != "general_chat":
                await run_agent_chat(
                    session,
                    str(world["owner"].id),
                    world["agent"],
                    [],
                    "File this.",
                    meter_quota=False,
                    # The REST chat hands its proposals back for a resume.
                    approvals_out=[] if surface == "agent_chat" else None,
                    **common,
                )
            else:
                await run_general_chat(session, str(world["owner"].id), [], "File this.", **common)

        executor = loop.await_args.kwargs["executor"]
        shared = ToolCall(id="shared", name="file_this_conversation", arguments={"workspace_id": "Launch"})
        private = ToolCall(id="private", name="file_this_conversation", arguments={"workspace_id": "Solo"})
        # Read from the database the turn ran on: what the loop awaits first.
        await executor.prepare_consent([shared, private])
        assert [p["tool_call_id"] for p in executor.screen_for_approval([shared, private])] == (
            ["shared"] if asks else []
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["agent_chat", "general_chat"])
async def test_a_resumed_turn_files_the_conversation_the_person_approved(monkeypatch, surface):
    """The approval collected on the card reaches the move: a turn resumed with
    the person's decision files into the shared workspace, on the agent path
    and on the General Assistant's alike."""
    from unittest.mock import AsyncMock

    from app.services.agent_chat import run_agent_chat
    from app.services.general_chat import run_general_chat

    _stub_turn(monkeypatch)
    monkeypatch.setattr("app.services.builtin_tools.load_custom_and_mcp_tools", AsyncMock(return_value=None))
    monkeypatch.setattr("app.services.hitl_whitelist.resolve_approve_tool_calls", AsyncMock(return_value=False))
    monkeypatch.setattr(f"app.services.{surface}.run_agent_loop", AsyncMock(side_effect=_StopTurnError))
    async with scratch_database("wsturn", tables=ALL_TABLES, extensions=("vector",)) as db, db.sessions() as session:
        world = await _seed(session)
        workspace_id, conversation_id = world["workspace"].id, world["unfiled"].id
        proposal = {
            "tool_call_id": "call-file",
            "tool_name": "file_this_conversation",
            "tool_description": "",
            "arguments": {"workspace_id": str(workspace_id)},
            "input_schema": {},
            "source": "builtin",
            "requires_consent": True,
        }
        common = {
            "conversation_id": str(conversation_id),
            "prebuilt_llm": _LLM,
            "resume_proposals": [proposal],
            "resume_decisions": {"call-file": {"decision": "approve"}},
        }
        with contextlib.suppress(_StopTurnError):
            if surface == "agent_chat":
                await run_agent_chat(
                    session, str(world["owner"].id), world["agent"], [], "", meter_quota=False, approvals_out=[], **common
                )
            else:
                await run_general_chat(session, str(world["owner"].id), [], "", **common)
        session.expire_all()
        assert (await session.get(Conversation, conversation_id)).workspace_id == workspace_id


@pytest.mark.asyncio
async def test_a_filed_turn_gets_the_conversation_search_once_the_index_exists(monkeypatch):
    from app.services import workspace_knowledge
    from app.services.workspaces import WORKSPACE_KB_CONVERSATIONS, ensure_workspace_kb

    seen: list[tuple] = []
    real = workspace_knowledge.conversation_search_tool

    def _spy(knowledge, conversation_id):
        seen.append((knowledge.conversations_kb_id, conversation_id))
        return real(knowledge, conversation_id)

    monkeypatch.setattr(workspace_knowledge, "conversation_search_tool", _spy)
    async with scratch_database("wsturn", tables=ALL_TABLES, extensions=("vector",)) as db, db.sessions() as session:
        world = await _seed(session)
        await _turn(monkeypatch, session, world, user=world["owner"], conversation=world["filed"])
        assert seen == [(None, str(world["filed"].id))]
        kb = await ensure_workspace_kb(session, world["workspace"], WORKSPACE_KB_CONVERSATIONS)
        await session.commit()
        seen.clear()
        await _turn(monkeypatch, session, world, user=world["owner"], conversation=world["filed"])
        assert seen == [(str(kb.id), str(world["filed"].id))]
