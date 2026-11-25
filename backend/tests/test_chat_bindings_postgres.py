"""A chat turn uses what its agent names, as its owner configured it.

This PR checks an agent's binding where it is written (``binding_access``);
nothing re-checks it when a turn loads it. A turn by someone the owner shared
the agent with runs the owner's MCP servers, custom tools, knowledge bases and
channel bots, as on ``main``. The one check chat already made on ``main`` is
unchanged: a fixed integration binding the chatting user cannot
see, or whose personal OAuth token they may not use, is not used for them and
falls back to their own or a company-wide integration of the same provider
(``shadow_invisible_bindings``). What that leaves missing is now said on the
reply (``unavailable_integrations_out``) and to the model, never a silent
absence.

``run_agent_chat`` and ``run_general_chat`` run against PostgreSQL with only
the model and the non-database peripherals stubbed; what they hand the tool
loaders and the system prompt's channel context is what is asserted. A turn
stops at the MCP loader, the last of them before the agent loop.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.models  # noqa: F401 - registers every mapper on Base.metadata
from app.models.agent import Agent
from app.models.channel_bot import ChannelBot
from app.models.custom_tool import CustomTool
from app.models.group import Group, GroupMember
from app.models.integration import Integration
from app.models.knowledge_base import KnowledgeBase
from app.models.mcp_server import MCPServer
from app.models.user import User
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration


def _user(name: str) -> User:
    return User(
        email=f"{name}-{uuid.uuid4().hex[:8]}@test.local",
        full_name=name.title(),
        hashed_password="x",
    )


def _integration(provider: str, owner) -> Integration:
    return Integration(
        provider=provider,
        name=f"{provider}-{uuid.uuid4().hex[:6]}",
        auth_type="api_key",
        status="connected",
        user_id=owner,
        is_company_managed=False,
    )


def _channel(bot: str) -> dict:
    return {"channel_bot_id": bot, "publish_enabled": True, "publish_channels": []}


async def _seed(sessions) -> dict:
    """The owner, the member of a group the owner shares with (who also has
    their own notion integration), and rows the member cannot see: the
    owner's private ones and a colleague's, shared with another group."""
    async with sessions() as session:
        users = {"owner": _user("owner"), "member": _user("member"), "colleague": _user("colleague")}
        session.add_all(users.values())
        await session.flush()
        analysts = Group(name=f"analysts-{uuid.uuid4().hex[:8]}")
        others = Group(name=f"others-{uuid.uuid4().hex[:8]}")
        session.add_all([analysts, others])
        await session.flush()
        session.add(GroupMember(group_id=analysts.id, user_id=users["member"].id))
        session.add(GroupMember(group_id=others.id, user_id=users["colleague"].id))
        owner, colleague = users["owner"].id, users["colleague"].id
        elsewhere = {"user_id": colleague, "shared_group_ids": [str(others.id)]}
        with_analysts = {"user_id": owner, "shared_group_ids": [str(analysts.id)]}
        rows = {
            "owner_server": MCPServer(name="owner-private", user_id=owner),
            "owner_tool": CustomTool(
                name="owner_tool", description="The owner's private tool",
                input_schema={"type": "object", "properties": {}},
                language="javascript", js_function="return 1;", user_id=owner,
            ),
            "owner_kb": KnowledgeBase(name="owner-kb", user_id=owner),
            "owner_bot": ChannelBot(
                provider="telegram_bot", name="owner-private", user_id=owner,
                is_company_managed=False,
            ),
            "owner_notion": _integration("notion", owner),
            "owner_jira": _integration("jira", owner),
            "member_notion": _integration("notion", users["member"].id),
            # Shared with the member's group; the owner never shared the
            # connected account (no consent) - or did (consent recorded).
            "owner_slack_unshared": Integration(
                provider="slack", name="Owner Slack", auth_type="oauth", status="connected",
                is_company_managed=False, oauth_share_acknowledged=False,
                access_token_enc="enc-token", **with_analysts,
            ),
            "owner_hubspot_consented": Integration(
                provider="hubspot", name="Owner HubSpot", auth_type="oauth", status="connected",
                is_company_managed=False, oauth_share_acknowledged=True,
                access_token_enc="enc-token", **with_analysts,
            ),
            "company_github": Integration(
                provider="github", name="Company GitHub", auth_type="api_key",
                status="connected", user_id=owner, is_company_managed=True,
            ),
            "colleague_server": MCPServer(name="colleague-shared", **elsewhere),
            "colleague_tool": CustomTool(
                name="colleague_tool", description="A colleague's tool",
                input_schema={"type": "object", "properties": {}},
                language="javascript", js_function="return 1;", **elsewhere,
            ),
            "colleague_kb": KnowledgeBase(name="colleague-kb", **elsewhere),
        }
        session.add_all(rows.values())
        await session.commit()
        return {
            "users": {k: str(v.id) for k, v in users.items()},
            "analysts": [str(analysts.id)],
            **{k: str(v.id) for k, v in rows.items()},
        }


class _StopTurnError(Exception):
    """Raised by the MCP loader stub: everything asserted is built by then."""


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
    """Stub the model's side of a turn; spy on the loaders it reaches."""
    from app.core.config import settings
    from app.services import integration_tools

    # Custom tools are off by default outside Copilot mode.
    monkeypatch.setattr(settings, "disable_custom_tools", False)
    spies = SimpleNamespace(
        loader=AsyncMock(side_effect=_StopTurnError),
        integrations=AsyncMock(return_value=([], None)),
        channels=AsyncMock(return_value=([], None)),
        context=MagicMock(side_effect=integration_tools.format_channel_context),
        knowledge=MagicMock(return_value=None),
    )
    for target, value in {
        "app.core.redis.redis_client": AsyncMock(return_value=_FakeRedis()),
        "app.services.langfuse_tracing.trace_llm_call": AsyncMock(return_value=None),
        "app.services.variable_resolver.load_variables": AsyncMock(return_value={}),
        "app.services.agent_chat.enforce_agentic_quota": AsyncMock(return_value=None),
        "app.services.builtin_tools.load_custom_and_mcp_tools": spies.loader,
        "app.services.builtin_tools.build_kb_search_tool": spies.knowledge,
        "app.services.integration_tools.build_integration_tools": spies.integrations,
        "app.services.integration_tools.build_channel_tools": spies.channels,
        "app.services.integration_tools.format_channel_context": spies.context,
    }.items():
        monkeypatch.setattr(target, value)
    return spies


_LLM = (SimpleNamespace(provider_name="fake"), "fake-model")


@pytest.mark.asyncio
@pytest.mark.parametrize("grantee", ["group", "user"], ids=["group-member", "per-user-grantee"])
@pytest.mark.parametrize(
    "from_session", [False, True], ids=["agent-list", "web-chat-session-selection"]
)
async def test_a_grantee_turn_uses_the_owners_private_server_and_bot(
    monkeypatch, grantee, from_session
):
    """Someone the owner shared the agent with - through a group or by name -
    chats with it: the turn runs the owner's private MCP server and channel
    bot and lists that bot's channels. The web chat sends the agent's servers
    back as the session selection; they are loaded the same way. The owner's
    private integration still falls back to the member's own, as on main."""
    from app.services.agent_chat import run_agent_chat

    spies = _stub_turn(monkeypatch)
    async with scratch_database("chatbind", tables=ALL_TABLES, extensions=("vector",)) as db:
        w = await _seed(db.sessions)
        users = w["users"]
        stored_channels = {"telegram": _channel(w["owner_bot"])}
        notice: list = []
        async with db.sessions() as session:
            agent = _agent(
                w,
                grantee,
                integration_bindings={"notion": [w["owner_notion"]]},
                channel_config=stored_channels,
                mcp_server_ids=[w["owner_server"]],
                custom_tool_ids=[w["owner_tool"]],
                knowledge_base_ids=[w["owner_kb"]],
            )
            session.add(agent)
            await session.commit()

            with pytest.raises(_StopTurnError):
                await run_agent_chat(
                    session,
                    users["member"],
                    agent,
                    [],
                    "hello",
                    meter_quota=False,
                    prebuilt_llm=_LLM,
                    session_mcp_server_ids=[w["owner_server"]] if from_session else None,
                    session_custom_tool_ids=[w["owner_tool"]] if from_session else None,
                    session_knowledge_base_ids=[w["owner_kb"]] if from_session else None,
                    unavailable_integrations_out=notice,
                )

        kwargs = spies.loader.await_args.kwargs
        assert list(kwargs["mcp_ids"]) == [w["owner_server"]]
        assert list(kwargs["custom_tool_ids"]) == [w["owner_tool"]]
        assert list(spies.knowledge.call_args.args[2]) == [w["owner_kb"]]
        assert spies.channels.await_args.args[1] == stored_channels
        assert spies.context.call_args.args[0] == stored_channels
        assert dict(spies.integrations.await_args.args[1]) == {"notion": w["member_notion"]}
        # The owner's integration was replaced by the member's own of the same
        # provider: nothing the turn needs is missing, nothing is reported.
        assert notice == []


def _agent(w: dict, grantee: str, **bindings) -> Agent:
    users = w["users"]
    fields = {
        "integration_bindings": {},
        "channel_config": None,
        "mcp_server_ids": [],
        "custom_tool_ids": [],
        "knowledge_base_ids": [],
        **bindings,
    }
    return Agent(
        name=f"agent-{uuid.uuid4().hex[:6]}",
        slug=f"agent-{uuid.uuid4().hex[:8]}",
        enabled=True,
        user_id=uuid.UUID(users["owner"]),
        is_builtin=False,
        is_company_managed=False,
        shared_group_ids=w["analysts"] if grantee == "group" else None,
        shared_user_ids=[users["member"]] if grantee == "user" else None,
        builtin_tool_ids=[],
        handover_agent_ids=[],
        output_format="markdown",
        memory_short_term_enabled=False,
        memory_medium_term_enabled=False,
        memory_persistent_enabled=False,
        memory_user_enabled=False,
        **fields,
    )


# (binding key in the seed, used for the grantee, what the reply says)
_INTEGRATION_CASES = {
    "private-no-replacement": ("owner_jira", False, {"provider": "jira", "reason": "integration_not_visible"}),
    "shared-token-not-shared": (
        "owner_slack_unshared",
        False,
        {"provider": "slack", "reason": "oauth_credentials_not_shared", "name": "Owner Slack"},
    ),
    "shared-token-consented": ("owner_hubspot_consented", True, None),
    "company-managed": ("company_github", True, None),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("case", list(_INTEGRATION_CASES))
@pytest.mark.parametrize(
    "from_session", [False, True], ids=["agent-bindings", "web-chat-session-selection"]
)
async def test_a_grantee_turn_uses_or_says_each_integration_of_the_agent(
    monkeypatch, case, from_session
):
    """Main's chat rule for integrations, from the grantee's
    side: a consented token and a company-managed integration are used; the
    owner's private one with nothing to replace it, and a shared one whose
    connected account its owner never shared, are not - and the reply says
    which, and why: the provider only for a row the grantee cannot
    see, the name of one they can. The web chat sends the agent's bindings
    back as the session selection: the same outcome."""
    from app.services.agent_chat import run_agent_chat

    key, used, said = _INTEGRATION_CASES[case]
    spies = _stub_turn(monkeypatch)
    async with scratch_database("chatbind", tables=ALL_TABLES, extensions=("vector",)) as db:
        w = await _seed(db.sessions)
        provider = {
            "owner_jira": "jira", "owner_slack_unshared": "slack",
            "owner_hubspot_consented": "hubspot", "company_github": "github",
        }[key]
        notice: list = []
        async with db.sessions() as session:
            agent = _agent(w, "group", integration_bindings={provider: [w[key]]})
            session.add(agent)
            await session.commit()
            with pytest.raises(_StopTurnError):
                await run_agent_chat(
                    session, w["users"]["member"], agent, [], "hello",
                    meter_quota=False, prebuilt_llm=_LLM,
                    session_integration_ids=[w[key]] if from_session else None,
                    unavailable_integrations_out=notice,
                )

    bound = (
        [str(i) for ids in spies.integrations.await_args.args[1].values()
         for i in (ids if isinstance(ids, list) else [ids])]
        if spies.integrations.await_count
        else []
    )
    assert bound == ([w[key]] if used else [])
    assert notice == ([said] if said else [])


@pytest.mark.asyncio
async def test_the_owner_s_own_turn_uses_every_binding_and_says_nothing(monkeypatch):
    from app.services.agent_chat import run_agent_chat

    spies = _stub_turn(monkeypatch)
    async with scratch_database("chatbind", tables=ALL_TABLES, extensions=("vector",)) as db:
        w = await _seed(db.sessions)
        notice: list = []
        async with db.sessions() as session:
            agent = _agent(
                w, "group",
                integration_bindings={"jira": [w["owner_jira"]], "slack": [w["owner_slack_unshared"]]},
            )
            session.add(agent)
            await session.commit()
            with pytest.raises(_StopTurnError):
                await run_agent_chat(
                    session, w["users"]["owner"], agent, [], "hello",
                    meter_quota=False, prebuilt_llm=_LLM,
                    unavailable_integrations_out=notice,
                )

    assert set(spies.integrations.await_args.args[1].values()) == {
        w["owner_jira"], w["owner_slack_unshared"],
    }
    assert notice == []


@pytest.mark.asyncio
async def test_a_grantee_s_prompt_lists_only_the_integrations_their_turn_uses(monkeypatch):
    """The "Connected integrations" block of the system prompt the agent loop
    receives: the company-managed integration the member's turn uses is
    listed, the owner's private one withheld from them is not."""
    from app.services.agent_chat import run_agent_chat

    class _AtTheLoopError(Exception):
        pass

    seen: dict = {}

    async def _loop(**kwargs):
        seen["system"] = "\n".join(m.content for m in kwargs["messages"] if m.role == "system")
        raise _AtTheLoopError

    _stub_turn(monkeypatch)
    monkeypatch.setattr("app.services.builtin_tools.load_custom_and_mcp_tools", AsyncMock(return_value=None))
    monkeypatch.setattr("app.services.agent_chat.run_agent_loop", _loop)
    async with scratch_database("chatbind", tables=ALL_TABLES, extensions=("vector",)) as db:
        w = await _seed(db.sessions)
        async with db.sessions() as session:
            jira_name = (await session.get(Integration, uuid.UUID(w["owner_jira"]))).name
            agent = _agent(
                w, "group",
                integration_bindings={"jira": [w["owner_jira"]], "github": [w["company_github"]]},
            )
            session.add(agent)
            await session.commit()
            with pytest.raises(_AtTheLoopError):
                await run_agent_chat(
                    session, w["users"]["member"], agent, [], "hello",
                    meter_quota=False, prebuilt_llm=_LLM,
                )

    block = seen["system"].split("Connected integrations:", 1)[1]
    assert "- Company GitHub (github)" in block
    assert jira_name not in seen["system"]
    assert "(jira)" not in block
