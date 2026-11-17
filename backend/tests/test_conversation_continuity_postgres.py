"""A conversation's history follows it across agents, on PostgreSQL.

The issue's own scenario, in the data: user U holds a conversation about a
video with agent A ("Video Bot"), gives it two instructions, then deletes A
naming B ("Deck Bot") as successor. The conversation continues with B, which
reads A's turns as A's; U reads it back through ``read_conversation_history``
and finds it through ``list_conversations``; A's traces stay readable by U and
by nobody else. A second user V, with whom A was shared but who cannot use B,
keeps their conversation with nobody, its history intact.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import select

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.api.v1 import agents as agents_api
from app.api.v1 import chat as chat_api
from app.channels.router import get_conversation_history, store_message
from app.models.agent import Agent
from app.models.agent_tombstone import AgentTombstone
from app.models.audit_log import AuditLog
from app.models.chat import Conversation, Message
from app.models.llm_trace import LLMTrace
from app.models.user import User
from app.services import agent_memory
from app.services import conversation_continuity as continuity
from app.services.builtin_tools import traces as traces_module
from app.services.builtin_tools.conversation_history import build_conversation_history_tools
from app.services.tracing.read_store import PostgresTraceReadStore
from app.tools.base import ToolCall
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration


def _id(n: int) -> uuid.UUID:
    return uuid.UUID(int=0x6734_0000_0000_0000_0000_0000_0000 + n)


OWNER, OTHER, STRANGER = _id(1), _id(2), _id(3)
AGENT_A, AGENT_B = _id(10), _id(11)
CONV, CONV_OTHER, CONV_DECK = _id(20), _id(21), _id(22)
VIDEO, DECK = _id(30), _id(31)
TRACE_OWNER, TRACE_OTHER = _id(40), _id(41)


class _Redis:
    """The Redis the turn lock and the read audit ask: nothing running, nothing marked."""

    def __init__(self):
        self.keys: set[str] = set()

    async def exists(self, key):
        return int(key in self.keys)

    async def set(self, key, _value, ex=None):
        self.keys.add(key)


@pytest.fixture(autouse=True)
def _no_redis(monkeypatch):
    import app.core.redis as redis_module

    fake = _Redis()

    async def _client():
        return fake

    monkeypatch.setattr(redis_module, "redis_client", _client)
    monkeypatch.setattr(chat_api, "redis_client", _client)
    monkeypatch.setattr(agent_memory, "clear_deleted_agent_memory", AsyncMock())


async def _seed(sessions) -> None:
    async with sessions() as db:
        db.add_all(
            [
                User(id=OWNER, email="owner-6734@test.local", full_name="Owner", hashed_password="x"),
                User(id=OTHER, email="other-6734@test.local", full_name="Other", hashed_password="x"),
                User(id=STRANGER, email="stranger-6734@test.local", full_name="Stranger", hashed_password="x"),
            ]
        )
        await db.flush()
        db.add_all(
            [
                # Shared with the other user, who chats with it too.
                Agent(
                    id=AGENT_A,
                    user_id=OWNER,
                    name="Video Bot",
                    slug="video-bot-6734",
                    persona="p",
                    shared_user_ids=[str(OTHER)],
                    shared_user_permissions={str(OTHER): "read_only"},
                ),
                # The owner's own: the other user cannot use it.
                Agent(id=AGENT_B, user_id=OWNER, name="Deck Bot", slug="deck-bot-6734", persona="p"),
            ]
        )
        await db.flush()
        db.add_all(
            [
                Conversation(id=CONV, user_id=OWNER, agent_id=AGENT_A, title="Launch video", video_project_id=VIDEO),
                Conversation(id=CONV_OTHER, user_id=OTHER, agent_id=AGENT_A, title="Other's video"),
                Conversation(id=CONV_DECK, user_id=OWNER, agent_id=AGENT_B, title="Board deck"),
            ]
        )
        await db.flush()
        for role, content, meta in (
            ("user", "Use a calm soundtrack.", None),
            (
                "assistant",
                f"Done: calm music. [[VIDEO:{VIDEO}]]",
                {"tool_call_trace": [{"name": "edit_video", "input": "{}", "output": "ok", "success": True}]},
            ),
            ("user", "Make the intro five seconds long.", None),
            ("assistant", "Done: a five-second intro.", None),
        ):
            await store_message(db, CONV, role=role, content=content, metadata_json=json.dumps(meta) if meta else None)
        await store_message(db, CONV_OTHER, role="user", content="Shorter, please.")
        await store_message(db, CONV_OTHER, role="assistant", content="Shortened.")
        await store_message(db, CONV_DECK, role="user", content=f"Put the deck [[PRESENTATION:{DECK}]] in.")
        await store_message(db, CONV_DECK, role="assistant", content="Added it.")
        now = datetime.now(UTC)
        db.add_all(
            [
                LLMTrace(
                    id=TRACE_OWNER,
                    agent_id=AGENT_A,
                    user_id=OWNER,
                    conversation_id=CONV,
                    source="agent-chat",
                    name="agent-chat",
                    started_at=now - timedelta(hours=2),
                ),
                LLMTrace(
                    id=TRACE_OTHER,
                    agent_id=AGENT_A,
                    user_id=OTHER,
                    conversation_id=CONV_OTHER,
                    source="agent-chat",
                    name="agent-chat",
                    started_at=now - timedelta(hours=1),
                ),
            ]
        )
        await db.commit()


async def _delete_a(sessions, *, successor: uuid.UUID | None) -> None:
    async with sessions() as db:
        owner = await db.get(User, OWNER)
        await agents_api.delete_agent(
            agent_id=str(AGENT_A),
            request=None,
            user=owner,
            db=db,
            _ai_gate=None,
            successor_agent_id=str(successor) if successor else None,
        )


async def _messages(sessions, conversation_id) -> list[Message]:
    async with sessions() as db:
        return list(
            (
                await db.execute(
                    select(Message).where(Message.conversation_id == conversation_id).order_by(Message.sequence)
                )
            )
            .scalars()
            .all()
        )


@pytest.mark.asyncio
async def test_every_assistant_message_records_the_agent_that_wrote_it():
    async with scratch_database("cont", tables=ALL_TABLES, extensions=("vector",)) as scratch:
        await _seed(scratch.sessions)
        rows = await _messages(scratch.sessions, CONV)
        authors = [json.loads(m.metadata_json or "{}").get(continuity.AUTHOR_AGENT_KEY) for m in rows]
        assert authors == [None, str(AGENT_A), None, str(AGENT_A)]
        assert [m.role for m in rows] == ["user", "assistant", "user", "assistant"]


@pytest.mark.asyncio
async def test_a_conversation_that_never_changed_agent_reads_exactly_as_before():
    async with scratch_database("cont", tables=ALL_TABLES, extensions=("vector",)) as scratch:
        await _seed(scratch.sessions)
        async with scratch.sessions() as db:
            history = await get_conversation_history(db, CONV)
        assert [m.content for m in history] == [m.content for m in await _messages(scratch.sessions, CONV)]


@pytest.mark.asyncio
async def test_deleting_an_agent_with_a_successor_hands_its_conversations_over_with_their_history():
    async with scratch_database("cont", tables=ALL_TABLES, extensions=("vector",)) as scratch:
        await _seed(scratch.sessions)
        async with scratch.sessions() as db:
            before = (await db.get(Conversation, CONV)).updated_at
        await _delete_a(scratch.sessions, successor=AGENT_B)

        async with scratch.sessions() as db:
            assert await db.get(Agent, AGENT_A) is None
            tombstone = await db.get(AgentTombstone, AGENT_A)
            assert (tombstone.name, tombstone.user_id, tombstone.successor_agent_id) == ("Video Bot", OWNER, AGENT_B)
            mine = await db.get(Conversation, CONV)
            theirs = await db.get(Conversation, CONV_OTHER)
            # The owner can use B: their conversation continues with it, in
            # place, where it sat in their list.
            assert (mine.agent_id, mine.deleted_agent_id) == (AGENT_B, AGENT_A)
            assert mine.updated_at == before
            # The other user cannot use B: theirs stays with nobody, marked.
            assert (theirs.agent_id, theirs.deleted_agent_id) == (None, AGENT_A)

        rows = await _messages(scratch.sessions, CONV)
        note = rows[-1]
        assert note.role == "assistant" and note.content.startswith("Continued by Deck Bot on ")
        switch = json.loads(note.metadata_json)[continuity.AGENT_SWITCH_KEY]
        assert switch["from"] == {"agent_id": str(AGENT_A), "agent_name": "Video Bot", "deleted": True}
        assert switch["to"] == {"agent_id": str(AGENT_B), "agent_name": "Deck Bot"}
        assert switch["reason"] == continuity.SWITCH_BY_DELETION
        assert [m.content for m in await _messages(scratch.sessions, CONV_OTHER)] == ["Shorter, please.", "Shortened."]

        # B answers next, and reads A's turns as A's.
        async with scratch.sessions() as db:
            history = [m.content for m in await get_conversation_history(db, CONV)]
        assert history[0] == "Use a calm soundtrack."
        assert history[1].startswith("[Reply from Video Bot]\n\nDone: calm music.")
        assert history[2] == "Make the intro five seconds long."
        assert history[3] == "[Reply from Video Bot]\n\nDone: a five-second intro."
        assert history[4].startswith("[Note]\n\nContinued by Deck Bot on ")


@pytest.mark.asyncio
async def test_a_successor_the_deleting_user_cannot_use_is_refused_and_nothing_is_deleted():
    async with scratch_database("cont", tables=ALL_TABLES, extensions=("vector",)) as scratch:
        await _seed(scratch.sessions)
        async with scratch.sessions() as db:
            db.add(Agent(id=_id(12), user_id=STRANGER, name="Theirs", slug="theirs-6734", persona="p"))
            await db.commit()
        with pytest.raises(HTTPException) as refused:
            await _delete_a(scratch.sessions, successor=_id(12))
        assert refused.value.status_code == 400
        async with scratch.sessions() as db:
            assert await db.get(Agent, AGENT_A) is not None
            assert await db.get(AgentTombstone, AGENT_A) is None


@pytest.mark.asyncio
async def test_the_owner_continues_a_conversation_of_a_deleted_agent_from_its_menu():
    async with scratch_database("cont", tables=ALL_TABLES, extensions=("vector",)) as scratch:
        await _seed(scratch.sessions)
        await _delete_a(scratch.sessions, successor=None)
        async with scratch.sessions() as db:
            assert (await db.get(Conversation, CONV)).agent_id is None
            other = await db.get(User, OTHER)
            # Not theirs: the unknown conversation's 404.
            with pytest.raises(HTTPException) as refused:
                await chat_api.continue_conversation_with_agent(
                    conversation_id=str(CONV),
                    body=chat_api.ConversationAgentBinding(agent_id=str(AGENT_B)),
                    user=other,
                    db=db,
                    _chat_gate=None,
                )
            assert refused.value.status_code == 404
        async with scratch.sessions() as db:
            owner = await db.get(User, OWNER)
            response = await chat_api.continue_conversation_with_agent(
                conversation_id=str(CONV),
                body=chat_api.ConversationAgentBinding(agent_id=str(AGENT_B)),
                user=owner,
                db=db,
                _chat_gate=None,
            )
        assert (response.agent_id, response.agent_name) == (str(AGENT_B), "Deck Bot")
        assert (response.deleted_agent_id, response.deleted_agent_name) == (str(AGENT_A), "Video Bot")
        note = (await _messages(scratch.sessions, CONV))[-1]
        switch = json.loads(note.metadata_json)[continuity.AGENT_SWITCH_KEY]
        assert switch["from"]["deleted"] is True and switch["from"]["agent_name"] == "Video Bot"
        assert switch["reason"] == continuity.SWITCH_BY_USER
        async with scratch.sessions() as db:
            history = [m.content for m in await get_conversation_history(db, CONV)]
        assert history[3] == "[Reply from Video Bot]\n\nDone: a five-second intro."


@pytest.mark.asyncio
async def test_continuing_with_the_agent_it_already_has_writes_nothing():
    async with scratch_database("cont", tables=ALL_TABLES, extensions=("vector",)) as scratch:
        await _seed(scratch.sessions)
        async with scratch.sessions() as db:
            conv = await db.get(Conversation, CONV_DECK)
            agent = await db.get(Agent, AGENT_B)
            assert await continuity.continue_with_agent(db, conv, agent) is None
            await db.commit()
        assert len(await _messages(scratch.sessions, CONV_DECK)) == 2


async def _read(user_id, current, **arguments) -> str:
    """``read_conversation_history`` as *user_id* calls it in conversation *current*."""
    _, executor = build_conversation_history_tools(
        str(user_id), conversation_id=str(current) if current else None, agent_id=str(AGENT_B)
    )
    result = await executor.execute_tool(ToolCall(id="1", name="read_conversation_history", arguments=arguments))
    return result.content


@pytest.mark.asyncio
async def test_the_current_conversation_reads_back_oldest_first_by_window():
    async with scratch_database("cont", tables=ALL_TABLES, extensions=("vector",)) as scratch:
        await _seed(scratch.sessions)
        await _delete_a(scratch.sessions, successor=AGENT_B)

        first = await _read(OWNER, CONV, limit=2)
        assert f"Conversation {CONV}: messages 0 to 1 of 5, oldest first." in first
        assert f"before that with Video Bot (agent since deleted, id `{AGENT_A}`)" in first
        assert "#0 [user," in first and "Use a calm soundtrack." in first
        assert "#1 [assistant: Video Bot (deleted agent)," in first
        assert f"[[VIDEO:{VIDEO}]]" in first
        assert "tools called: edit_video (ok)" in first
        assert f'More: read_conversation_history(conversation_id="{CONV}", offset=2).' in first

        rest = await _read(OWNER, CONV, offset=2)
        assert "#2 [user," in rest and "Make the intro five seconds long." in rest
        assert "#4 [note," in rest and "Continued by Deck Bot" in rest
        assert "That is the end of the conversation." in rest

        found = await _read(OWNER, CONV, search="intro")
        assert "of 2 matching 'intro'" in found
        assert "calm soundtrack" not in found


@pytest.mark.asyncio
async def test_another_conversation_of_the_user_is_read_and_audited_another_users_is_refused():
    async with scratch_database("cont", tables=ALL_TABLES, extensions=("vector",)) as scratch:
        await _seed(scratch.sessions)
        read = await _read(OWNER, CONV_DECK, conversation_id=str(CONV))
        assert "Use a calm soundtrack." in read
        async with scratch.sessions() as db:
            audits = (await db.execute(select(AuditLog).where(AuditLog.action == "read_conversation"))).scalars().all()
        assert [(a.entity_id, a.user_id) for a in audits] == [(CONV, OWNER)]

        for refused in (
            await _read(OTHER, CONV_OTHER, conversation_id=str(CONV)),
            await _read(OWNER, CONV, conversation_id=str(CONV_OTHER)),
            await _read(OWNER, CONV, conversation_id=str(uuid.uuid4())),
        ):
            assert refused.startswith("Error: conversation `") and "not one of the user's own" in refused


async def _list(user_id, **arguments) -> str:
    defs, executor = build_conversation_history_tools(str(user_id), conversation_id=None)
    assert defs[0].name == "list_conversations"
    result = await executor.execute_tool(ToolCall(id="1", name="list_conversations", arguments=arguments))
    return result.content


@pytest.mark.asyncio
async def test_a_deleted_agents_conversations_are_found_by_its_last_name_and_by_their_object():
    async with scratch_database("cont", tables=ALL_TABLES, extensions=("vector",)) as scratch:
        await _seed(scratch.sessions)
        await _delete_a(scratch.sessions, successor=AGENT_B)

        by_name = await _list(OWNER, agent="video bot")
        assert f"(id: `{CONV}`" in by_name
        assert f"continued from Video Bot (deleted agent `{AGENT_A}`)" in by_name
        assert f"about video `{VIDEO}`" in by_name
        assert str(CONV_DECK) not in by_name
        assert "None of your conversations" in await _list(OWNER, agent="Nobody")

        assert f"`{CONV}`" in await _list(OWNER, object_id=str(VIDEO))
        by_deck = await _list(OWNER, object_id=str(DECK))
        assert f"`{CONV_DECK}`" in by_deck and f"`{CONV}`" not in by_deck
        by_kind = await _list(OWNER, object_type="presentation")
        assert f"`{CONV_DECK}`" in by_kind and f"`{CONV}`" not in by_kind
        # Never another user's, whatever the filter.
        assert str(CONV_OTHER) not in await _list(OWNER, agent=str(AGENT_A))


@pytest.mark.asyncio
async def test_a_conversation_of_an_agent_deleted_before_tombstones_is_found_read_and_continued():
    """What an older deletion left: no tombstone, nothing on the
    conversation, unstamped replies - only the traces, with the agent's name."""
    legacy_agent, legacy_conv, legacy_trace = _id(50), _id(51), _id(52)
    async with scratch_database("cont", tables=ALL_TABLES, extensions=("vector",)) as scratch:
        await _seed(scratch.sessions)
        async with scratch.sessions() as db:
            db.add(Agent(id=legacy_agent, user_id=OWNER, name="Legacy Bot", slug="legacy-6734", persona="p"))
            await db.flush()
            db.add(Conversation(id=legacy_conv, user_id=OWNER, title="Old thread"))
            await db.flush()
            db.add_all(
                [
                    Message(conversation_id=legacy_conv, role="user", content="Keep it under a minute.", sequence=1),
                    Message(
                        conversation_id=legacy_conv,
                        role="assistant",
                        content="Cut to 58 seconds.",
                        sequence=2,
                        llm_trace_id=legacy_trace,
                    ),
                    LLMTrace(
                        id=legacy_trace,
                        agent_id=legacy_agent,
                        user_id=OWNER,
                        conversation_id=legacy_conv,
                        source="agent-chat",
                        name="agent-chat",
                        agent_name_snapshot="Legacy Bot",
                        started_at=datetime.now(UTC) - timedelta(hours=3),
                    ),
                ]
            )
            await db.commit()
        async with scratch.sessions() as db:
            await db.delete(await db.get(Agent, legacy_agent))
            await db.commit()

        assert f"`{legacy_conv}`" in await _list(OWNER, agent="legacy")
        read = await _read(OWNER, CONV, conversation_id=str(legacy_conv))
        assert "#1 [assistant: Legacy Bot (deleted agent)," in read

        async with scratch.sessions() as db:
            owner = await db.get(User, OWNER)
            await chat_api.continue_conversation_with_agent(
                conversation_id=str(legacy_conv),
                body=chat_api.ConversationAgentBinding(agent_id=str(AGENT_B)),
                user=owner,
                db=db,
                _chat_gate=None,
            )
        async with scratch.sessions() as db:
            history = [m.content for m in await get_conversation_history(db, legacy_conv)]
        assert history[1] == "[Reply from Legacy Bot]\n\nCut to 58 seconds."


def _store_args(**overrides):
    args = {
        "search": None,
        "conversation_id": None,
        "source": None,
        "agent_id": None,
        "model": None,
        "status_filter": None,
        "level": None,
        "date_from": None,
        "date_to": None,
        "cursor": None,
        "cursor_id": None,
        "limit": 25,
    }
    args.update(overrides)
    return args


@pytest.mark.asyncio
async def test_a_deleted_agents_traces_stay_readable_by_its_user_and_its_owner_only():
    async with scratch_database("cont", tables=ALL_TABLES, extensions=("vector",)) as scratch:
        await _seed(scratch.sessions)
        await _delete_a(scratch.sessions, successor=None)
        store = PostgresTraceReadStore()

        async def _seen(user_id, **filters) -> set[uuid.UUID]:
            viewer = SimpleNamespace(id=user_id, is_admin=False)
            async with scratch.sessions() as db:
                rows, _ = await store.list_traces(db, user=viewer, user_group_ids=[], **_store_args(**filters))
            return {row[0].id for row in rows}

        # Its own run, for the user who ran it; every run, for its owner.
        assert await _seen(OTHER, agent_id=[str(AGENT_A)]) == {TRACE_OTHER}
        assert await _seen(OWNER, agent_id=[str(AGENT_A)]) == {TRACE_OWNER, TRACE_OTHER}
        assert await _seen(STRANGER) == set()
        # A free-text query matches the deleted agent's last name.
        assert await _seen(OWNER, search="Video Bot") == {TRACE_OWNER, TRACE_OTHER}
        async with scratch.sessions() as db:
            rows, _ = await store.list_traces(
                db, user=SimpleNamespace(id=OWNER, is_admin=False), user_group_ids=[], **_store_args()
            )
        assert {row.agent_name for row in rows} == {"Video Bot"}


@pytest.mark.asyncio
async def test_inspect_agent_traces_targets_a_deleted_agent_by_its_id():
    async with scratch_database("cont", tables=ALL_TABLES, extensions=("vector",)) as scratch:
        await _seed(scratch.sessions)
        await _delete_a(scratch.sessions, successor=AGENT_B)
        _, executor = traces_module.build_trace_tools(
            db=None, user_id=str(OTHER), agent_id=str(AGENT_B), allow_cross_agent=True
        )
        result = await executor.execute_tool(
            ToolCall(
                id="1",
                name="inspect_agent_traces",
                arguments={"target_agent_id": str(AGENT_A), "include_spans": False},
            )
        )
        listing = json.loads(result.content)
        assert [t["id"] for t in listing["traces"]] == [str(TRACE_OTHER)]
        assert listing["traces"][0]["agent_name"] == "Video Bot"
        assert listing["traces"][0]["agent_deleted"] is True
