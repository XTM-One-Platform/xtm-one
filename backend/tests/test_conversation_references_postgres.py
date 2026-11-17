"""Conversations referenced with @, against real PostgreSQL.

* the @ menu offers what the user can open and is worth pointing at: their
  own (web chat and embedded chat) and those shared with them by name, most
  recent first - never another user's private conversation, an ``org_read``
  one nobody pointed them at, an archived or empty one, or the current one;
* a send keeps only what its sender can open, says how many it could not, and
  never more than the limit;
* the brief fences the referenced words and names the reader tool;
* ``read_conversation`` reads only conversations referenced in the current
  conversation, as the acting user, by window - and refuses one whose share
  was revoked since;
* ``run_agent_chat`` puts the brief on the message it sends, not on the
  stored one, and offers the reader on every turn of a conversation that
  references others.

``integration``-marked and skips when PostgreSQL is unreachable.
"""

from __future__ import annotations

import json
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import delete

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.api import deps
from app.api.v1 import chat as chat_api
from app.api.v1 import platform as platform_api
from app.channels.router import store_message
from app.core.database import get_db
from app.llm.base import ChatResponse
from app.models.chat import Conversation
from app.models.conversation_share import ConversationShare
from app.models.group import Group, GroupMember
from app.models.user import User
from app.services.builtin_tools.conversation_reader import (
    READ_CONVERSATION_TOOL,
    build_conversation_reader_tool,
)
from app.services.conversation_references import (
    MAX_REFERENCED_CONVERSATIONS,
    build_reference_brief,
    referenced_conversation_ids,
    resolve_referenced_conversations,
    search_reference_candidates,
    with_conversation_refs,
)
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration


@asynccontextmanager
async def _database():
    async with scratch_database(
        "convrefs", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True
    ) as db, db.sessions() as session:
        yield session


def _user(name: str) -> User:
    return User(
        id=uuid.uuid4(),
        email=f"{name}-{uuid.uuid4().hex[:6]}@example.com",
        full_name=name,
        hashed_password="x",
    )


async def _seed(session) -> SimpleNamespace:
    alice, bob = _user("Alice"), _user("Bob")
    session.add_all([alice, bob])
    await session.flush()
    group = Group(id=uuid.uuid4(), name=f"Analysts-{uuid.uuid4().hex[:4]}")
    session.add(group)
    await session.flush()
    session.add(GroupMember(group_id=group.id, user_id=alice.id))

    def conv(owner: User, title: str, **kw) -> Conversation:
        row = Conversation(id=uuid.uuid4(), user_id=owner.id, title=title, **kw)
        session.add(row)
        return row

    w = SimpleNamespace(alice=alice, bob=bob, group=group)
    w.current = conv(alice, "Current work")
    w.q3 = conv(
        alice,
        "Q3 threat report",
        compaction_state_json=json.dumps(
            {
                "directives": "- Use TLP:AMBER for every export",
                "summary": "We compared the Q3 campaigns against the Q2 baseline.",
                "through_sequence": 1,
                "compacted_count": 2,
            }
        ),
    )
    w.embedded = conv(alice, "OpenCTI triage", channel="api")
    w.empty = conv(alice, "Nothing yet")
    w.archived = conv(alice, "Old threat notes", is_archived=True)
    w.shared = conv(bob, "Incident review")
    w.group_shared = conv(bob, "Group runbook")
    w.org_read = conv(bob, "Org notes", link_access="org_read")
    w.private = conv(bob, "Bob private")
    await session.flush()
    session.add_all(
        [
            ConversationShare(conversation_id=w.shared.id, shared_with_user_id=alice.id, created_by=bob.id),
            ConversationShare(conversation_id=w.group_shared.id, shared_with_group_id=group.id, created_by=bob.id),
        ]
    )
    for row in (w.current, w.embedded, w.archived, w.shared, w.group_shared, w.org_read, w.private):
        await store_message(session, row.id, "user", f"question in {row.title}")
        await store_message(session, row.id, "assistant", f"answer in {row.title}")
    for i in range(6):
        await store_message(session, w.q3.id, "user" if i % 2 == 0 else "assistant", f"q3 message {i + 1}")
    # Most recent first: the order the menu must keep.
    now = datetime.now(UTC)
    for rank, row in enumerate(
        (w.current, w.q3, w.embedded, w.shared, w.group_shared, w.org_read, w.private, w.empty, w.archived)
    ):
        row.updated_at = now - timedelta(minutes=rank)
    await session.commit()
    return w


async def test_the_menu_offers_own_and_named_shares_most_recent_first():
    async with _database() as session:
        w = await _seed(session)
        rows = await search_reference_candidates(session, w.alice, query=None, limit=20, exclude=w.current.id)

        assert [r["id"] for r in rows] == [str(c.id) for c in (w.q3, w.embedded, w.shared, w.group_shared)]
        q3, _embedded, shared, _group = rows
        assert (q3["key"], q3["is_own"], q3["owner_name"]) == ("q3-threat-report", True, None)
        assert (shared["is_own"], shared["owner_name"]) == (False, "Bob")

        found = await search_reference_candidates(session, w.alice, query="THREAT", limit=20, exclude=None)
        assert [r["id"] for r in found] == [str(w.q3.id)], "the archived one is left out"
        assert await search_reference_candidates(session, w.alice, query="%", limit=20, exclude=None) == []
        # What follows @ is a key in the making: its words, in any order.
        for typed in ("q3-threat", "threat_q3", "Q3 REPORT"):
            hits = await search_reference_candidates(session, w.alice, query=typed, limit=20, exclude=None)
            assert [r["id"] for r in hits] == [str(w.q3.id)], typed
        assert await search_reference_candidates(session, w.alice, query="q3-runbook", limit=20, exclude=None) == []
        # Bob sees his own conversations, none of Alice's.
        bobs = await search_reference_candidates(session, w.bob, query=None, limit=20, exclude=None)
        assert {r["id"] for r in bobs} == {str(c.id) for c in (w.shared, w.group_shared, w.org_read, w.private)}


async def test_a_send_keeps_only_what_its_sender_can_open():
    async with _database() as session:
        w = await _seed(session)
        res = await resolve_referenced_conversations(
            session,
            w.alice,
            [str(w.q3.id), str(w.private.id), str(w.current.id), str(uuid.uuid4()), str(w.org_read.id), "junk", 7],
            current_conversation_id=w.current.id,
        )

        assert [r.id for r in res.references] == [w.q3.id, w.org_read.id]
        assert res.unavailable == 2, "Bob's private conversation and an unknown id"
        assert res.meta()[0] == {"conversation_id": str(w.q3.id), "title": "Q3 threat report", "key": "q3-threat-report"}
        # The WebSocket holds only the sender's id.
        by_id = await resolve_referenced_conversations(
            session, w.alice.id, [str(w.shared.id)], current_conversation_id=w.current.id
        )
        assert [r.id for r in by_id.references] == [w.shared.id]
        assert by_id.references[0].owner_name == "Bob"

        many = [w.q3, w.embedded, w.shared, w.group_shared, w.org_read, w.archived, w.empty]
        over = await resolve_referenced_conversations(
            session, w.alice, [str(c.id) for c in many], current_conversation_id=w.current.id
        )
        assert len(over.references) == MAX_REFERENCED_CONVERSATIONS
        assert over.over_limit == len(many) - MAX_REFERENCED_CONVERSATIONS


async def test_the_brief_fences_the_referenced_words_and_names_the_reader():
    async with _database() as session:
        w = await _seed(session)
        res = await resolve_referenced_conversations(
            session, w.alice, [str(w.q3.id), str(w.shared.id), str(w.private.id)], current_conversation_id=w.current.id
        )
        brief = await build_reference_brief(session, res)

        assert brief is not None
        assert f"@q3-threat-report is the conversation {w.q3.id} (the user's own, 6 messages" in brief
        assert f"@incident-review is the conversation {w.shared.id} (shared with the user" in brief
        assert "Use TLP:AMBER for every export" in brief
        assert "We compared the Q3 campaigns" in brief
        assert "q3 message 6" in brief and "q3 message 3" in brief and "q3 message 2" not in brief
        assert f'read_conversation(conversation_id="{w.q3.id}")' in brief
        assert "1 referenced conversation(s) could not be opened" in brief
        # The words of a conversation - its title, its owner, its messages -
        # only ever appear inside a fence.
        for fenced in ("Q3 threat report", "Shared with the user by: Bob", "answer in Incident review"):
            at = brief.index(fenced)
            assert brief.rfind("<<UNTRUSTED-", 0, at) > brief.rfind("<<END-UNTRUSTED-", 0, at), fenced
        assert await build_reference_brief(session, await resolve_referenced_conversations(
            session, w.alice, [], current_conversation_id=w.current.id
        )) is None


async def _reference(session, w, *convs: Conversation) -> None:
    res = await resolve_referenced_conversations(
        session, w.alice, [str(c.id) for c in convs], current_conversation_id=w.current.id
    )
    await store_message(
        session, w.current.id, "user", "@q3-threat-report what did we decide?",
        metadata_json=with_conversation_refs(None, res),
    )
    await session.commit()


async def test_read_conversation_reads_only_what_this_conversation_references_as_the_reader():
    async with _database() as session:
        w = await _seed(session)
        assert await build_conversation_reader_tool(session, w.alice.id, w.current.id) is None
        await _reference(session, w, w.q3, w.shared)
        assert await referenced_conversation_ids(session, w.current.id) == [w.q3.id, w.shared.id]

        [definition], executor = await build_conversation_reader_tool(session, w.alice.id, w.current.id)
        assert definition.name == READ_CONVERSATION_TOOL
        assert f'@q3-threat-report = "{w.q3.id}"' in definition.description
        assert "Incident review" not in definition.description, "titles stay out of the description"
        read = executor.handler(READ_CONVERSATION_TOOL)

        first = await read(conversation_id=str(w.q3.id), offset=0, limit=4)
        assert "Messages 1 to 4 of 6" in first and "q3 message 4" in first and "q3 message 5" not in first
        assert "offset=4)" in first and "<<UNTRUSTED-" in first
        rest = await read(conversation_id="@q3-threat-report", offset=4)
        assert "Messages 5 to 6 of 6" in rest and "That is the end of the conversation." in rest
        assert "past the end" in await read(conversation_id=str(w.q3.id), offset=6)

        refused = await read(conversation_id=str(w.private.id))
        assert refused.startswith("Error: that is not a conversation the user referenced")
        assert "answer in Bob private" not in refused

        assert "answer in Incident review" in await read(conversation_id=str(w.shared.id))
        await session.execute(delete(ConversationShare).where(ConversationShare.conversation_id == w.shared.id))
        await session.commit()
        revoked = await read(conversation_id=str(w.shared.id))
        assert revoked.startswith("Error: that conversation can no longer be opened")


async def test_a_reference_the_reader_can_no_longer_open_tells_the_model_nothing():
    """Bob revokes the share and renames the conversation: the
    next turn's tool names only what Alice can open, by the key her message
    was sent with, and no answer of the tool carries the new title."""
    async with _database() as session:
        w = await _seed(session)
        await _reference(session, w, w.q3, w.shared)
        await session.execute(delete(ConversationShare).where(ConversationShare.conversation_id == w.shared.id))
        w.shared.title = "Sensitive merger plan"
        # Alice's own conversation is renamed too: it keeps the key she sent.
        w.q3.title = "Renamed report"
        await session.commit()

        [definition], executor = await build_conversation_reader_tool(session, w.alice.id, w.current.id)
        read = executor.handler(READ_CONVERSATION_TOOL)
        assert definition.description.count(" = \"") == 1, "one reference listed"
        assert f'@q3-threat-report = "{w.q3.id}"' in definition.description
        answers = [
            definition.description,
            await read(conversation_id=str(w.shared.id)),
            await read(conversation_id="@incident-review"),
            await read(conversation_id="@sensitive-merger-plan"),
        ]
        for answer in answers:
            assert "sensitive" not in answer.lower(), answer
            assert str(w.shared.id) not in answer, answer
        for answer in answers[1:]:
            assert answer.startswith("Error: that is not a conversation the user referenced"), answer
        assert "q3 message 6" in await read(conversation_id="@q3-threat-report")

        # Nothing left that Alice may open: no tool at all.
        await session.delete(w.q3)
        await session.commit()
        assert await build_conversation_reader_tool(session, w.alice.id, w.current.id) is None


async def test_the_routes_serve_the_menu_and_the_embedded_chat_learns_no_owner():
    async with _database() as session:
        w = await _seed(session)
        api = FastAPI()
        api.include_router(chat_api.router, prefix="/api/v1/chat")
        api.include_router(platform_api.router, prefix="/api/v1/platform")
        api.dependency_overrides[deps.get_current_user] = lambda: w.alice

        async def _get_db():
            yield session

        api.dependency_overrides[get_db] = _get_db
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:
            web = await client.get(
                "/api/v1/chat/conversation-references",
                params={"q": "review", "exclude": str(w.current.id)},
            )
            embedded = await client.get("/api/v1/platform/chat/conversation-references", params={"q": "review"})
            too_many = await client.get("/api/v1/chat/conversation-references", params={"limit": 50})

        assert web.status_code == 200, web.text
        assert web.json()["conversations"] == [
            {
                "id": str(w.shared.id),
                "title": "Incident review",
                "key": "incident-review",
                "updated_at": w.shared.updated_at.isoformat(),
                "is_own": False,
                "owner_name": "Bob",
            }
        ]
        assert embedded.status_code == 200, embedded.text
        [row] = embedded.json()["conversations"]
        assert "owner_name" not in row and row["is_own"] is False
        assert too_many.status_code == 422


def _last_user(loop_kwargs: dict):
    return [m for m in loop_kwargs["messages"] if m.role == "user"][-1]


async def test_run_agent_chat_puts_the_brief_on_the_message_it_sends_and_offers_the_reader(monkeypatch):
    from app.services import nova_engine
    from app.services.agent_chat import run_agent_chat
    from app.services.content_safety import UNTRUSTED_CONTENT_POLICY
    from tests.test_prompt_guard_chat_surfaces_postgres import _FakeRedis
    from tests.test_prompt_guard_session_postgres import _agent, _stub_turn

    loop = AsyncMock(return_value=ChatResponse(content="done", model="fake-model"))
    llm = (SimpleNamespace(provider_name="fake"), "fake-model")
    monkeypatch.setattr(nova_engine, "NOVA_HUNTING_AVAILABLE", False)
    _stub_turn(monkeypatch)
    for target, value in {
        "app.core.redis.redis_client": AsyncMock(return_value=_FakeRedis()),
        "app.services.agent_chat.run_agent_loop": loop,
        "app.services.agent_chat.enforce_agentic_quota_ex": AsyncMock(return_value=(None, True)),
    }.items():
        monkeypatch.setattr(target, value)

    async with _database() as session:
        w = await _seed(session)
        agent = _agent(w.alice)
        session.add(agent)
        await session.commit()

        await run_agent_chat(
            session, str(w.alice.id), agent, [], "what did we decide?",
            conversation_id=str(w.current.id), prebuilt_llm=llm,
        )
        plain = loop.await_args.kwargs
        assert READ_CONVERSATION_TOOL not in {t.name for t in plain["tools"] or []}
        assert _last_user(plain).content == "what did we decide?"

        await _reference(session, w, w.q3)
        await run_agent_chat(
            session, str(w.alice.id), agent, [], "@q3-threat-report what did we decide?",
            conversation_id=str(w.current.id), prebuilt_llm=llm, reference_brief="THE BRIEF",
        )
        kwargs = loop.await_args.kwargs
        assert _last_user(kwargs).content == "@q3-threat-report what did we decide?\n\nTHE BRIEF"
        assert READ_CONVERSATION_TOOL in {t.name for t in kwargs["tools"]}
        assert UNTRUSTED_CONTENT_POLICY in kwargs["messages"][0].content

        # A later turn of the same conversation: no brief, the reader still there.
        await run_agent_chat(
            session, str(w.alice.id), agent, [], "and in the other one?",
            conversation_id=str(w.current.id), prebuilt_llm=llm,
        )
        later = loop.await_args.kwargs
        assert _last_user(later).content == "and in the other one?"
        assert READ_CONVERSATION_TOOL in {t.name for t in later["tools"]}

