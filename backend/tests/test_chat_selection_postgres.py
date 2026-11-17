"""A chat request may only add to its selection what its caller could bind.

The selection a chat message carries (MCP servers, custom tools, knowledge
bases, integrations) is loaded with its stored credentials, code and content.
An id the request ADDS - one neither the user's profile defaults, the agent's
bindings nor the General Assistant's settings already name - must name a row
its caller could bind to an agent of their own: otherwise the request is
refused before anything is stored. The conversation's saved selection names
none: an id it kept that was unbound since is refused, and left out where the
selection is read.
Everything already known is used as stored, as on ``main``: nothing is hidden
or silently ignored at chat time. The one
chat-time rule is ``main``'s own for integrations: the turn does
not use one its user cannot see or whose personal OAuth token they may not
use, and it now says so on the reply as well as to the model.

The three-tier predicate reads the per-user sharing columns, which a mock
session cannot show: everything here runs against PostgreSQL.
"""

from __future__ import annotations

import inspect
import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

import app.models  # noqa: F401 - registers every mapper on Base.metadata
from app.models.agent import Agent
from app.models.chat import Conversation
from app.models.custom_tool import CustomTool
from app.models.general_assistant_config import GeneralAssistantConfig
from app.models.group import Group, GroupMember
from app.models.integration import Integration
from app.models.knowledge_base import KnowledgeBase
from app.models.mcp_server import MCPServer
from app.models.user import User
from app.services.chat_selection import (
    SELECTION_KINDS,
    refusal_detail,
    refused_chat_selection,
    usable_saved_selection,
)
from app.services.integration_credentials import (
    INTEGRATION_NOT_VISIBLE_REASON,
    OAUTH_NOT_SHARED_REASON,
)
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration

KINDS = list(SELECTION_KINDS)


def _user(name: str, role: str = "user") -> User:
    return User(
        email=f"{name}-{uuid.uuid4().hex[:8]}@test.local",
        full_name=name.title(),
        hashed_password="x",
        role=role,
    )


def _rows(owner: uuid.UUID, tag: str) -> dict:
    return {
        "mcp_server_ids": MCPServer(name=f"{tag}-server", user_id=owner),
        "custom_tool_ids": CustomTool(
            name=f"{tag}_tool", description="A tool",
            input_schema={"type": "object", "properties": {}},
            language="javascript", js_function="return 1;", user_id=owner,
        ),
        "knowledge_base_ids": KnowledgeBase(name=f"{tag}-kb", user_id=owner),
        "integration_ids": Integration(
            provider="github", name=f"{tag}-github", auth_type="api_key",
            status="connected", user_id=owner, is_company_managed=False,
        ),
    }


async def _seed(sessions) -> SimpleNamespace:
    """The member (in ``analysts``), a colleague whose private rows the member
    cannot see, a colleague's OAuth integration shared with ``analysts`` whose
    connected account was never shared, the member's own rows, an agent and
    one conversation per surface."""
    async with sessions() as session:
        member, colleague, admin = _user("member"), _user("colleague"), _user("admin", "admin")
        session.add_all([member, colleague, admin])
        await session.flush()
        analysts = Group(name=f"analysts-{uuid.uuid4().hex[:8]}")
        session.add(analysts)
        await session.flush()
        session.add(GroupMember(group_id=analysts.id, user_id=member.id))
        foreign = _rows(colleague.id, "colleague")
        own = _rows(member.id, "member")
        unshared_token = Integration(
            provider="slack", name="Team Slack", auth_type="oauth", status="connected",
            user_id=colleague.id, is_company_managed=False,
            shared_group_ids=[str(analysts.id)], oauth_share_acknowledged=False,
            access_token_enc="enc-token",
        )
        session.add_all([*foreign.values(), *own.values(), unshared_token])
        await session.flush()
        agent = Agent(
            name=f"agent-{uuid.uuid4().hex[:6]}", slug=f"agent-{uuid.uuid4().hex[:8]}",
            enabled=True, user_id=member.id, is_builtin=False, is_company_managed=False,
            integration_bindings={}, channel_config=None, mcp_server_ids=[],
            custom_tool_ids=[], knowledge_base_ids=[], builtin_tool_ids=[],
            handover_agent_ids=[], output_format="markdown",
            memory_short_term_enabled=False, memory_medium_term_enabled=False,
            memory_persistent_enabled=False, memory_user_enabled=False,
        )
        session.add(agent)
        await session.flush()
        agent_conv = Conversation(user_id=member.id, agent_id=agent.id, title="Agent chat")
        ga_conv = Conversation(user_id=member.id, agent_id=None, title="General chat")
        session.add_all([agent_conv, ga_conv])
        await session.commit()
        return SimpleNamespace(
            member=member.id,
            admin=admin.id,
            agent=agent.id,
            conversations={"agent": agent_conv.id, "general": ga_conv.id},
            foreign={k: str(v.id) for k, v in foreign.items()},
            own={k: str(v.id) for k, v in own.items()},
            unshared_token=str(unshared_token.id),
        )


def _requested(kind: str, *ids: str) -> dict:
    return {kind: list(ids)}


# --- A NEW request: refused ------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["agent", "general"])
@pytest.mark.parametrize("kind", KINDS)
async def test_a_request_adding_a_row_its_caller_cannot_see_is_refused(surface, kind):
    """A colleague's private row and an id naming no row are refused alike;
    the caller's own rows pass."""
    async with scratch_database("chatsel", tables=ALL_TABLES, extensions=("vector",)) as db:
        w = await _seed(db.sessions)
        async with db.sessions() as session:
            conv = await session.get(Conversation, w.conversations[surface])
            hidden = await refused_chat_selection(
                session, user_id=w.member, conversation=conv,
                requested=_requested(kind, w.own[kind], w.foreign[kind]),
            )
            missing = await refused_chat_selection(
                session, user_id=w.member, conversation=conv,
                requested=_requested(kind, w.own[kind], str(uuid.uuid4())),
            )
            own = await refused_chat_selection(
                session, user_id=w.member, conversation=conv,
                requested=_requested(kind, w.own[kind]),
            )

    assert hidden == missing == refusal_detail([kind])
    assert w.foreign[kind] not in hidden
    assert own is None


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["agent", "general"])
async def test_a_visible_integration_whose_token_is_not_shared_is_not_refused(surface):
    """The picker lists a shared integration whose owner never shared the
    connected account: the turn withholds it and says so, so sending
    it is not refused - a refusal would block every message of the chat."""
    async with scratch_database("chatsel", tables=ALL_TABLES, extensions=("vector",)) as db:
        w = await _seed(db.sessions)
        async with db.sessions() as session:
            conv = await session.get(Conversation, w.conversations[surface])
            refused = await refused_chat_selection(
                session, user_id=w.member, conversation=conv,
                requested=_requested("integration_ids", w.unshared_token),
            )

    assert refused is None


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["agent", "general"])
async def test_what_is_configured_is_not_refused(surface):
    """The user's profile defaults, the agent's bindings and the General
    Assistant's settings are already known: a request sending them back is not
    refused, and the turn uses them as stored."""
    async with scratch_database("chatsel", tables=ALL_TABLES, extensions=("vector",)) as db:
        w = await _seed(db.sessions)
        f = w.foreign
        async with db.sessions() as session:
            conv = await session.get(Conversation, w.conversations[surface])
            user = await session.get(User, w.member)
            user.default_knowledge_base_ids = [f["knowledge_base_ids"]]
            user.ga_custom_tool_ids = [f["custom_tool_ids"]]
            if surface == "agent":
                agent = await session.get(Agent, w.agent)
                agent.integration_bindings = {"github": [f["integration_ids"]]}
                agent.mcp_server_ids = [f["mcp_server_ids"]]
            else:
                session.add(
                    GeneralAssistantConfig(
                        integration_bindings={"github": [f["integration_ids"]]},
                        mcp_server_ids=[f["mcp_server_ids"]],
                    )
                )
            await session.commit()

            refused = await refused_chat_selection(
                session, user_id=w.member, conversation=conv,
                requested={kind: [f[kind]] for kind in KINDS},
            )

    assert refused is None


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["agent", "general"])
async def test_a_saved_id_unbound_since_is_refused_and_left_out(surface):
    """An MCP server the conversation saved while its agent (or the General
    Assistant) had it, unbound since, which the member could not bind: a
    request sending it back is refused, and the saved selection read for the
    chat page and the wake and channel turns leaves it out. A saved id still
    known stays."""
    async with scratch_database("chatsel", tables=ALL_TABLES, extensions=("vector",)) as db:
        w = await _seed(db.sessions)
        f = w.foreign
        async with db.sessions() as session:
            conv = await session.get(Conversation, w.conversations[surface])
            conv.tool_overrides_json = json.dumps(
                {
                    "mcp_server_ids": [f["mcp_server_ids"]],
                    "knowledge_base_ids": [f["knowledge_base_ids"]],
                    "agent_id": str(conv.agent_id) if conv.agent_id else None,
                }
            )
            user = await session.get(User, w.member)
            user.default_knowledge_base_ids = [f["knowledge_base_ids"]]
            await session.commit()

            refused = await refused_chat_selection(
                session, user_id=w.member, conversation=conv,
                requested=_requested("mcp_server_ids", f["mcp_server_ids"]),
            )
            usable = await usable_saved_selection(session, user_id=w.member, conversation=conv)
            # The conversation list counts the tools the reopened picker shows.
            from app.api.v1.chat import CONVERSATION_ACCESS_OWNER, _serialize_conversation_list

            [row] = await _serialize_conversation_list(session, [conv], user, access=CONVERSATION_ACCESS_OWNER)

    assert refused == refusal_detail(["mcp_server_ids"])
    assert usable["mcp_server_ids"] == []
    assert usable["knowledge_base_ids"] == [f["knowledge_base_ids"]]
    assert row.tool_count == 1


@pytest.mark.asyncio
async def test_a_channel_conversation_knows_the_bindings_of_the_agent_answering_it_for_the_general_assistant():
    """A Teams conversation the General Assistant owns is answered on the web by
    the agent linked to it, and the chat sends that agent's
    selection: its bindings are known, as a bound agent's are. A web
    conversation of the General Assistant does not know them."""
    async with scratch_database("chatsel", tables=ALL_TABLES, extensions=("vector",)) as db:
        w = await _seed(db.sessions)
        f = w.foreign
        async with db.sessions() as session:
            linked = Agent(
                name="CTEM Assistant", slug=f"ctem-{uuid.uuid4().hex[:8]}",
                enabled=True, user_id=None, is_builtin=False, is_company_managed=True,
                integration_bindings={"github": [f["integration_ids"]]}, channel_config=None,
                mcp_server_ids=[f["mcp_server_ids"]], custom_tool_ids=[f["custom_tool_ids"]],
                knowledge_base_ids=[f["knowledge_base_ids"]], builtin_tool_ids=[],
                handover_agent_ids=[], output_format="markdown",
                memory_short_term_enabled=False, memory_medium_term_enabled=False,
                memory_persistent_enabled=False, memory_user_enabled=False,
            )
            session.add(linked)
            await session.flush()
            session.add(GeneralAssistantConfig(linked_agent_id=linked.id))
            teams = Conversation(
                user_id=w.member, agent_id=None, channel="teams",
                channel_thread_id="a:1personal-chat", title="Teams chat",
            )
            session.add(teams)
            await session.commit()
            requested = {kind: [f[kind]] for kind in KINDS}
            in_teams = await refused_chat_selection(
                session, user_id=w.member, conversation=teams, requested=requested
            )
            on_the_web = await refused_chat_selection(
                session,
                user_id=w.member,
                conversation=await session.get(Conversation, w.conversations["general"]),
                requested=requested,
            )

    assert in_teams is None
    assert on_the_web == refusal_detail(KINDS)


@pytest.mark.asyncio
async def test_an_admin_may_add_any_server_tool_or_knowledge_base():
    """Admins may bind every MCP server, custom tool and knowledge base;
    integrations keep chat's own rule, which never bypassed for them."""
    async with scratch_database("chatsel", tables=ALL_TABLES, extensions=("vector",)) as db:
        w = await _seed(db.sessions)
        async with db.sessions() as session:
            conv = Conversation(user_id=w.admin, agent_id=None, title="Admin chat")
            session.add(conv)
            await session.commit()
            kept = await refused_chat_selection(
                session, user_id=w.admin, conversation=conv,
                requested={kind: [w.foreign[kind]] for kind in KINDS[:3]},
            )
            integration = await refused_chat_selection(
                session, user_id=w.admin, conversation=conv,
                requested=_requested("integration_ids", w.foreign["integration_ids"]),
            )

    assert kept is None
    assert integration == refusal_detail(["integration_ids"])


@pytest.mark.asyncio
@pytest.mark.parametrize("surface", ["agent", "general"])
async def test_the_message_route_refuses_before_storing_anything(monkeypatch, surface):
    from app.api.v1 import chat as chat_api
    from app.api.v1.chat import SendMessageRequest

    monkeypatch.setattr(chat_api, "enforce_rate_limit", AsyncMock())
    stored = AsyncMock(side_effect=AssertionError("the message was stored"))
    monkeypatch.setattr(chat_api, "store_message", stored)
    async with scratch_database("chatsel", tables=ALL_TABLES, extensions=("vector",)) as db:
        w = await _seed(db.sessions)
        async with db.sessions() as session:
            with pytest.raises(HTTPException) as exc:
                await chat_api.send_message(
                    str(w.conversations[surface]),
                    SendMessageRequest(
                        content="hello", mcp_server_ids=[w.foreign["mcp_server_ids"]]
                    ),
                    user=await session.get(User, w.member),
                    db=session,
                    _chat_gate=None,
                )

    assert exc.value.status_code == 400
    assert exc.value.detail == refusal_detail(["mcp_server_ids"])
    stored.assert_not_awaited()


def test_the_websocket_refuses_before_storing_the_user_message():
    """The web stream's send applies the same rule, before the user message (a 400, as REST)."""
    from app.api.v1 import chat as chat_api

    source = inspect.getsource(chat_api)
    check = source.index("_turn_selection_refusal = await refused_chat_selection(")
    assert check < source.index("turn_user_msg = await store_message(")
    assert "await _refuse_send(_turn_selection_refusal, 400)" in source[check:check + 1400]


# --- What is known: used as stored ------------------------------------------


class _StopTurnError(Exception):
    """Raised by the tool loader stub: everything asserted is built by then."""


class _FakeRedis:
    async def incr(self, key):
        return 1

    async def decr(self, key):
        return 0

    async def expire(self, key, ttl):
        return True

    async def delete(self, key):
        return 1


def _stub_turn(monkeypatch) -> SimpleNamespace:
    from app.core.config import settings

    monkeypatch.setattr(settings, "disable_custom_tools", False)
    spies = SimpleNamespace(
        loader=AsyncMock(side_effect=_StopTurnError),
        knowledge=MagicMock(return_value=None),
        integrations=AsyncMock(return_value=([], None)),
    )
    for target, value in {
        "app.core.redis.redis_client": AsyncMock(return_value=_FakeRedis()),
        "app.services.langfuse_tracing.trace_llm_call": AsyncMock(return_value=None),
        "app.services.variable_resolver.load_variables": AsyncMock(return_value={}),
        "app.services.agent_chat.enforce_agentic_quota": AsyncMock(return_value=None),
        "app.services.builtin_tools.load_custom_and_mcp_tools": spies.loader,
        "app.services.builtin_tools.build_kb_search_tool": spies.knowledge,
        "app.services.integration_tools.build_integration_tools": spies.integrations,
    }.items():
        monkeypatch.setattr(target, value)
    return spies


def _bound_ids(spy) -> list[str]:
    if not spy.await_count:
        return []
    bound = spy.await_args.args[1]
    return [str(i) for ids in bound.values() for i in (ids if isinstance(ids, list) else [ids])]


_LLM = (SimpleNamespace(provider_name="fake"), "fake-model")


@pytest.mark.asyncio
async def test_an_agent_turn_uses_a_stored_selection_as_stored_and_says_what_it_withholds(
    monkeypatch,
):
    """The saved selection the web chat sends back names rows the member could
    use when they picked them (a colleague's, unshared since): the MCP server,
    custom tool and knowledge base are loaded as stored, as on main. The
    integration is not bound, as on main, and the reply now says so:
    the provider, never the name of a row the member cannot see; the visible
    integration whose token is not shared is named."""
    from app.services.agent_chat import run_agent_chat

    spies = _stub_turn(monkeypatch)
    async with scratch_database("chatsel", tables=ALL_TABLES, extensions=("vector",)) as db:
        w = await _seed(db.sessions)
        f, own = w.foreign, w.own
        notice: list = []
        async with db.sessions() as session:
            agent = await session.get(Agent, w.agent)
            with pytest.raises(_StopTurnError):
                await run_agent_chat(
                    session, str(w.member), agent, [], "hello",
                    meter_quota=False, prebuilt_llm=_LLM,
                    session_mcp_server_ids=[own["mcp_server_ids"], f["mcp_server_ids"]],
                    session_custom_tool_ids=[f["custom_tool_ids"], own["custom_tool_ids"]],
                    session_knowledge_base_ids=[f["knowledge_base_ids"]],
                    session_integration_ids=[
                        f["integration_ids"], own["integration_ids"], w.unshared_token,
                    ],
                    unavailable_integrations_out=notice,
                )

    kwargs = spies.loader.await_args.kwargs
    assert list(kwargs["mcp_ids"]) == [own["mcp_server_ids"], f["mcp_server_ids"]]
    assert list(kwargs["custom_tool_ids"]) == [f["custom_tool_ids"], own["custom_tool_ids"]]
    assert _bound_ids(spies.integrations) == [own["integration_ids"]]
    assert sorted(notice, key=lambda item: item["provider"]) == [
        {"provider": "github", "reason": INTEGRATION_NOT_VISIBLE_REASON},
        {"provider": "slack", "reason": OAUTH_NOT_SHARED_REASON, "name": "Team Slack"},
    ]


@pytest.mark.asyncio
async def test_an_agent_turn_uses_profile_defaults_as_saved(monkeypatch):
    from app.services.agent_chat import run_agent_chat

    spies = _stub_turn(monkeypatch)
    async with scratch_database("chatsel", tables=ALL_TABLES, extensions=("vector",)) as db:
        w = await _seed(db.sessions)
        notice: list = []
        async with db.sessions() as session:
            user = await session.get(User, w.member)
            user.default_mcp_server_ids = [w.foreign["mcp_server_ids"], w.own["mcp_server_ids"]]
            await session.commit()
            agent = await session.get(Agent, w.agent)
            with pytest.raises(_StopTurnError):
                await run_agent_chat(
                    session, str(w.member), agent, [], "hello",
                    meter_quota=False, prebuilt_llm=_LLM, unavailable_integrations_out=notice,
                )

    assert list(spies.loader.await_args.kwargs["mcp_ids"]) == [
        w.foreign["mcp_server_ids"], w.own["mcp_server_ids"],
    ]
    assert notice == []


@pytest.mark.asyncio
async def test_the_general_assistant_uses_profile_defaults_as_saved(monkeypatch):
    """A user's saved General Assistant lists, integrations included, are used
    as saved (nothing is withheld at run time)."""
    from app.services.general_chat import run_general_chat

    spies = _stub_turn(monkeypatch)
    async with scratch_database("chatsel", tables=ALL_TABLES, extensions=("vector",)) as db:
        w = await _seed(db.sessions)
        f, own = w.foreign, w.own
        notice: list = []
        async with db.sessions() as session:
            user = await session.get(User, w.member)
            user.ga_mcp_server_ids = [f["mcp_server_ids"], own["mcp_server_ids"]]
            user.ga_custom_tool_ids = [f["custom_tool_ids"]]
            user.ga_knowledge_base_ids = [f["knowledge_base_ids"], own["knowledge_base_ids"]]
            user.ga_integration_bindings = {"github": [f["integration_ids"]]}
            await session.commit()
            with pytest.raises(_StopTurnError):
                await run_general_chat(
                    session, str(w.member), [], "hello", prebuilt_llm=_LLM,
                    unavailable_integrations_out=notice,
                )

    kwargs = spies.loader.await_args.kwargs
    assert list(kwargs["mcp_ids"]) == [f["mcp_server_ids"], own["mcp_server_ids"]]
    assert list(kwargs["custom_tool_ids"]) == [f["custom_tool_ids"]]
    assert list(spies.knowledge.call_args.args[2]) == [
        f["knowledge_base_ids"], own["knowledge_base_ids"],
    ]
    assert _bound_ids(spies.integrations) == [f["integration_ids"]]
    assert notice == []


@pytest.mark.asyncio
async def test_the_general_assistant_session_selection_says_what_it_withholds(monkeypatch):
    """A General Assistant session selection is resolved by main's resolver
    (visibility and personal OAuth token); what it drops is now shown
    on the reply too."""
    from app.services.general_chat import run_general_chat

    spies = _stub_turn(monkeypatch)
    async with scratch_database("chatsel", tables=ALL_TABLES, extensions=("vector",)) as db:
        w = await _seed(db.sessions)
        f, own = w.foreign, w.own
        notice: list = []
        async with db.sessions() as session:
            with pytest.raises(_StopTurnError):
                await run_general_chat(
                    session, str(w.member), [], "hello", prebuilt_llm=_LLM,
                    session_mcp_server_ids=[f["mcp_server_ids"]],
                    session_integration_ids=[
                        f["integration_ids"], own["integration_ids"], w.unshared_token,
                    ],
                    unavailable_integrations_out=notice,
                )

    assert list(spies.loader.await_args.kwargs["mcp_ids"]) == [f["mcp_server_ids"]]
    assert _bound_ids(spies.integrations) == [own["integration_ids"]]
    assert sorted(notice, key=lambda item: item["provider"]) == [
        {"provider": "github", "reason": INTEGRATION_NOT_VISIBLE_REASON},
        {"provider": "slack", "reason": OAUTH_NOT_SHARED_REASON, "name": "Team Slack"},
    ]


@pytest.mark.asyncio
async def test_the_general_assistant_platform_settings_run_as_configured(monkeypatch):
    """An admin configured the General Assistant with rows the member cannot
    see: they are the platform's settings, used as configured."""
    from app.services.general_chat import run_general_chat

    spies = _stub_turn(monkeypatch)
    async with scratch_database("chatsel", tables=ALL_TABLES, extensions=("vector",)) as db:
        w = await _seed(db.sessions)
        f = w.foreign
        notice: list = []
        async with db.sessions() as session:
            session.add(
                GeneralAssistantConfig(
                    mcp_server_ids=[f["mcp_server_ids"]],
                    custom_tool_ids=[f["custom_tool_ids"]],
                    knowledge_base_ids=[f["knowledge_base_ids"]],
                )
            )
            await session.commit()
            with pytest.raises(_StopTurnError):
                await run_general_chat(
                    session, str(w.member), [], "hello", prebuilt_llm=_LLM,
                    unavailable_integrations_out=notice,
                )

    kwargs = spies.loader.await_args.kwargs
    assert list(kwargs["mcp_ids"]) == [f["mcp_server_ids"]]
    assert list(kwargs["custom_tool_ids"]) == [f["custom_tool_ids"]]
    assert list(spies.knowledge.call_args.args[2]) == [f["knowledge_base_ids"]]
    assert notice == []


def test_a_stored_reply_serves_its_unavailable_integrations_and_drops_malformed_ones():
    from datetime import UTC, datetime

    from app.api.v1.chat import _build_message_response

    def _message(meta):
        return SimpleNamespace(
            id=uuid.uuid4(), conversation_id=uuid.uuid4(), role="assistant", content="ok",
            channel="web", channel_message_id=None, sequence=1,
            created_at=datetime.now(UTC), llm_trace_id=None, metadata_json=json.dumps(meta),
        )

    served = _build_message_response(
        _message(
            {
                "unavailable_integrations": [
                    {"provider": "github", "reason": INTEGRATION_NOT_VISIBLE_REASON},
                    {"provider": "github", "reason": INTEGRATION_NOT_VISIBLE_REASON},
                    {"provider": "slack", "reason": OAUTH_NOT_SHARED_REASON, "name": "Team"},
                    {"provider": "jira", "reason": "bogus"},
                    "not an item",
                ]
            }
        )
    )
    assert [item.model_dump(exclude_none=True) for item in served.unavailable_integrations] == [
        {"provider": "github", "reason": INTEGRATION_NOT_VISIBLE_REASON},
        {"provider": "slack", "reason": OAUTH_NOT_SHARED_REASON, "name": "Team"},
    ]
    assert _build_message_response(
        _message({"unavailable_integrations": {"x": 1}})
    ).unavailable_integrations is None
    assert _build_message_response(_message({})).unavailable_integrations is None
