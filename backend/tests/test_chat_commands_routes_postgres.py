"""The chat commands' routes against PostgreSQL (#6590).

``POST /api/v1/chat/commands/run`` answers whoever may send in the
conversation - its owner, the conversation's agent still visible to them - and
nobody else: not a stranger, not someone the conversation is shared with, not
an administrator reading another person's conversation, not after the agent's
share is revoked. Before a conversation exists, the agent must be visible
(own, user grant, group grant, company-managed). Every actor is an ordinary
user unless the test says otherwise (rule 19).

``integration``-marked and skips when PostgreSQL is unreachable.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.api import deps
from app.api.v1 import chat_commands
from app.core.database import get_db
from app.models.agent import Agent
from app.models.chat import Conversation, Message
from app.models.conversation_share import ConversationShare
from app.models.group import Group, GroupMember
from app.models.user import User
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration

RUN = "/api/v1/chat/commands/run"

#: A reading as the agent loop publishes it and the run stores it.
_DETAIL = {
    "system": {"persona": 600, "platform": 400},
    "tools": [
        {"name": "search", "kind": "builtin", "group": None, "tokens": 500},
        {"name": "create_issue", "kind": "mcp", "group": "Github (MCP)", "tokens": 1_500},
    ],
    "tool_results": {"create_issue": 3_000},
    "compaction": {"gate_tokens": 5_000, "approaching_tokens": 6_800, "trigger_tokens": 8_000, "target_tokens": 7_000},
}
_READING = {
    "context_tokens": 7_000,
    "context_window": 100_000,
    "context_breakdown": {"system": 1_000, "tools": 500, "dynamic_tools": 1_500, "tool_results": 3_000, "conversation": 1_000},
    "context_level": "normal",
    "context_detail": _DETAIL,
    "context_model": "model-x",
    "context_provider_tokens": 6_000,
    "context_cached_tokens": 4_000,
}


def _scratch(name: str):
    return scratch_database(name, tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True)


def _user(email: str, role: str = "user") -> User:
    return User(id=uuid.uuid4(), email=email, hashed_password="x", full_name=email.split("@")[0], role=role)


@pytest.fixture(autouse=True)
def _no_rate_limit(monkeypatch):
    async def _allow(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(chat_commands, "enforce_rate_limit", _allow)


@asynccontextmanager
async def _client(sessions: async_sessionmaker, user: User) -> AsyncIterator[httpx.AsyncClient]:
    api = FastAPI()
    api.include_router(chat_commands.router, prefix="/api/v1/chat")

    async def _get_db():
        async with sessions() as db:
            yield db

    api.dependency_overrides[deps.get_current_user] = lambda: user
    api.dependency_overrides[deps.require_chat_enabled] = lambda: None
    api.dependency_overrides[get_db] = _get_db
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:
        yield client


async def _conversation(db, user: User, agent: Agent | None, *messages: tuple[str, str, dict | None]) -> Conversation:
    conversation = Conversation(user_id=user.id, agent_id=agent.id if agent else None)
    db.add(conversation)
    await db.flush()
    for sequence, (role, content, meta) in enumerate(messages, start=1):
        db.add(
            Message(
                conversation_id=conversation.id,
                role=role,
                content=content,
                sequence=sequence,
                metadata_json=json.dumps(meta) if meta is not None else None,
                created_at=datetime.now(UTC) - timedelta(minutes=10) + timedelta(seconds=sequence),
            )
        )
    await db.flush()
    return conversation


async def test_the_owner_gets_the_report_of_the_latest_call_and_nobody_else_does() -> None:
    async with _scratch("chat_commands_owner") as scratch:
        async with scratch.sessions() as db:
            owner, stranger, viewer = _user("ada@filigran.io"), _user("alan@filigran.io"), _user("grace@filigran.io")
            admin = _user("root@filigran.io", role="admin")
            db.add_all([owner, stranger, viewer, admin])
            await db.flush()
            agent = Agent(user_id=owner.id, name="Analyst", updated_at=datetime.now(UTC) - timedelta(days=1))
            mentioned = Agent(user_id=owner.id, name="Mentioned")
            db.add_all([agent, mentioned])
            await db.flush()
            # The newest reply is a mentioned agent's: its reading measures
            # that agent, so the report reads the conversation agent's.
            foreign = {**_READING, "context_provider_tokens": 99, "answered_by": {"agent_id": str(mentioned.id), "agent_name": "Mentioned"}}
            conversation = await _conversation(
                db,
                owner,
                agent,
                ("user", "hello", None),
                ("assistant", "answer", _READING),
                ("assistant", "mentioned answer", foreign),
                ("user", "q" * 300, None),
            )
            db.add(ConversationShare(conversation_id=conversation.id, shared_with_user_id=viewer.id))
            await db.commit()

        body = {"text": "/context detail", "conversation_id": str(conversation.id)}
        async with _client(scratch.sessions, owner) as client:
            response = await client.post(RUN, json=body)
        assert response.status_code == 200
        payload = response.json()
        report = payload["result"]
        assert (payload["command"], payload["argument"]) == ("context", "detail")
        assert report["state"] == "measured"
        assert (report["model"], report["window"]) == ("model-x", 100_000)
        assert (report["used_tokens"], report["cached_tokens"]) == (6_000, 4_000)
        # The estimate (7,000) is over the provider's count: every line shrinks
        # to it and still sums exactly, its items too.
        assert sum(c["tokens"] for c in report["categories"]) == 6_000
        for category in report["categories"]:
            if category["items"]:
                assert sum(i["tokens"] for i in category["items"]) == category["tokens"]
        mcp = next(c for c in report["categories"] if c["key"] == "mcp_tools")
        assert [(i["name"], i["group"]) for i in mcp["items"]] == [("create_issue", "Github (MCP)")]
        # The answer the measured call wrote, the mentioned reply and the
        # question since: the next request carries all three.
        new_tokens = (len("answer") + len("mentioned answer") + 300) // 3
        assert report["new_message_tokens"] == new_tokens
        assert report["compaction"] == {
            "level": "normal",
            "gate_tokens": 5_000 + new_tokens,
            "approaching_tokens": 6_800,
            "trigger_tokens": 8_000,
            "target_tokens": 7_000,
            "free_tokens": 8_000 - 5_000 - new_tokens,
        }
        assert report["agent_changed"] is False
        assert payload["markdown"].startswith("## Context usage")

        for other in (stranger, viewer, admin):
            async with _client(scratch.sessions, other) as client:
                refused = await client.post(RUN, json=body)
            assert refused.status_code == 404, other.email


async def test_a_general_assistant_conversation_skips_a_mentioned_agents_reading() -> None:
    """A mentioned agent's reply stores its own run's gauge reading, never the
    command keys: in a conversation with no agent of its own, it still is not
    the conversation's reading."""
    async with _scratch("chat_commands_ga_mention") as scratch:
        async with scratch.sessions() as db:
            owner = _user("ada@filigran.io")
            db.add(owner)
            await db.flush()
            mentioned = Agent(user_id=owner.id, name="Mentioned")
            db.add(mentioned)
            await db.flush()
            gauge_only = {key: _READING[key] for key in ("context_tokens", "context_window", "context_breakdown")}
            mention = {**gauge_only, "context_tokens": 7_000, "answered_by": {"agent_id": str(mentioned.id), "agent_name": "Mentioned"}}
            conversation = await _conversation(
                db,
                owner,
                None,
                ("assistant", "assistant answer", {**_READING, "context_provider_tokens": 5_000}),
                ("assistant", "mentioned answer", mention),
            )
            await db.commit()

        async with _client(scratch.sessions, owner) as client:
            report = (await client.post(RUN, json={"text": "/context", "conversation_id": str(conversation.id)})).json()["result"]
        assert (report["state"], report["used_tokens"]) == ("measured", 5_000)


async def test_the_summary_leaves_the_items_out_and_flags_an_edited_agent() -> None:
    async with _scratch("chat_commands_summary") as scratch:
        async with scratch.sessions() as db:
            owner = _user("ada@filigran.io")
            db.add(owner)
            await db.flush()
            agent = Agent(user_id=owner.id, name="Analyst", updated_at=datetime.now(UTC))
            db.add(agent)
            await db.flush()
            conversation = await _conversation(db, owner, agent, ("assistant", "answer", _READING))
            await db.commit()

        async with _client(scratch.sessions, owner) as client:
            report = (await client.post(RUN, json={"text": "/context", "conversation_id": str(conversation.id)})).json()["result"]
        assert report["detail"] is False
        assert all(c["items"] == [] for c in report["categories"])
        assert report["agent_changed"] is True


async def test_a_revoked_share_stops_the_report_at_once() -> None:
    async with _scratch("chat_commands_revoked") as scratch:
        async with scratch.sessions() as db:
            owner, grantee = _user("ada@filigran.io"), _user("alan@filigran.io")
            db.add_all([owner, grantee])
            await db.flush()
            agent = Agent(
                user_id=owner.id,
                name="Shared",
                shared_user_ids=[str(grantee.id)],
                shared_user_permissions={str(grantee.id): "read_only"},
            )
            db.add(agent)
            await db.flush()
            conversation = await _conversation(db, grantee, agent, ("assistant", "answer", _READING))
            await db.commit()

        body = {"text": "/context", "conversation_id": str(conversation.id)}
        async with _client(scratch.sessions, grantee) as client:
            assert (await client.post(RUN, json=body)).status_code == 200

        async with scratch.sessions() as db:
            row = await db.get(Agent, agent.id)
            row.shared_user_ids = []
            row.shared_user_permissions = {}
            await db.commit()
        async with _client(scratch.sessions, grantee) as client:
            assert (await client.post(RUN, json=body)).status_code == 404


async def test_before_a_conversation_the_agent_must_be_visible() -> None:
    async with _scratch("chat_commands_agent") as scratch:
        async with scratch.sessions() as db:
            owner, member, stranger, config_grantee = (
                _user("ada@filigran.io"),
                _user("grace@filigran.io"),
                _user("alan@filigran.io"),
                _user("edsger@filigran.io"),
            )
            db.add_all([owner, member, stranger, config_grantee])
            await db.flush()
            group = Group(name="Analysts")
            db.add(group)
            await db.flush()
            db.add(GroupMember(group_id=group.id, user_id=member.id))
            private = Agent(user_id=owner.id, name="Private")
            grouped = Agent(
                user_id=owner.id,
                name="Grouped",
                shared_group_ids=[str(group.id)],
                shared_group_permissions={str(group.id): "read_only"},
                shared_user_ids=[str(config_grantee.id)],
                shared_user_permissions={str(config_grantee.id): "config"},
            )
            managed = Agent(user_id=owner.id, name="Managed", is_company_managed=True)
            db.add_all([private, grouped, managed])
            await db.flush()
            # The member's own earlier conversation with the grouped agent.
            await _conversation(db, member, grouped, ("user", "hi", None), ("assistant", "answer", _READING))
            await db.commit()

        def run(agent: Agent) -> dict:
            return {"text": "/context detail", "agent_id": str(agent.id)}

        async with _client(scratch.sessions, stranger) as client:
            assert (await client.post(RUN, json=run(private))).status_code == 404
            assert (await client.post(RUN, json=run(grouped))).status_code == 404
            managed_report = await client.post(RUN, json=run(managed))
        assert managed_report.status_code == 200
        assert managed_report.json()["result"]["state"] == "none"

        async with _client(scratch.sessions, config_grantee) as client:
            assert (await client.post(RUN, json=run(grouped))).json()["result"]["state"] == "none"

        async with _client(scratch.sessions, member) as client:
            report = (await client.post(RUN, json=run(grouped))).json()["result"]
        # Borrowed from the member's own conversation with the agent: its
        # overhead only, measured by the estimate.
        assert report["state"] == "previous_conversation"
        keys = {c["key"] for c in report["categories"]}
        assert keys == {"system_prompt", "builtin_tools", "mcp_tools"}
        assert report["used_tokens"] == 1_000 + 500 + 1_500
        assert report["compaction"]["gate_tokens"] == 1_000


@pytest.mark.parametrize(
    "body",
    [
        {"text": "hello"},
        {"text": "/contextual"},
        {"text": "/context foo"},
        {"text": ""},
        {"text": "/context", "conversation_id": str(uuid.uuid4()), "agent_id": str(uuid.uuid4())},
        {"text": "/context " + "x" * 300},
        {"text": "/context", "conversation_id": "not-an-id"},
        {"text": "/context", "agent_id": "not-an-id"},
    ],
    ids=["message", "near-miss", "unknown-argument", "empty", "both-ids", "too-long", "bad-conversation-id", "bad-agent-id"],
)
async def test_what_is_not_a_command_is_refused_as_invalid(body) -> None:
    async with _scratch("chat_commands_invalid") as scratch:
        async with scratch.sessions() as db:
            owner = _user("ada@filigran.io")
            db.add(owner)
            await db.commit()
        async with _client(scratch.sessions, owner) as client:
            assert (await client.post(RUN, json=body)).status_code == 422


async def test_help_and_the_command_list() -> None:
    async with _scratch("chat_commands_help") as scratch:
        async with scratch.sessions() as db:
            owner = _user("ada@filigran.io")
            db.add(owner)
            await db.commit()
        async with _client(scratch.sessions, owner) as client:
            listed = (await client.get("/api/v1/chat/commands")).json()
            helped = (await client.post(RUN, json={"text": "/help"})).json()
        assert [(c["name"], c["usage"]) for c in listed] == [("context", "/context [detail]"), ("help", "/help")]
        assert listed[0]["arguments"] == [
            {"name": "detail", "aliases": ["-d"], "description": listed[0]["arguments"][0]["description"]}
        ]
        assert helped["result"] == {"kind": "help", "commands": listed}
        assert helped["markdown"].startswith("## Commands")
