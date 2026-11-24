"""A run uses the MCP servers and channel bots its agent and its assignment
store, whoever it acts for; its integrations follow chat's rule.

A binding is checked where it is written: a write that adds an MCP server, an
integration or a channel bot its writer cannot see is refused
(``binding_access``), and an import or a copy drops such an id with a warning.
A run re-checks no MCP server or channel bot: whoever triggered it and whoever
it runs as, it builds them from what the agent and the assignment store, so an
owner who shares an agent lends what the agent carries. An integration is used
only when the person the run acts for may use it, or the agent lends it
(``resolve_agent_integration_bindings``), as in a chat turn.

Each entry point that starts a run acting for someone other than the agent's
owner is driven here for real - the manual trigger route, a work-item
delegation, the per-user fan-out and an automatic trigger of a run-as
assignment - then the run executes against PostgreSQL with only the model, the
loop and the non-database peripherals stubbed. What it hands ``load_mcp_tools``,
``build_integration_tools``, ``build_channel_tools`` and the system prompt's
channel context is what is asserted.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import Request
from sqlalchemy import select

import app.models  # noqa: F401 - registers every mapper on Base.metadata
from app.models.agent import Agent
from app.models.assignment import Assignment, AssignmentRun
from app.models.channel_bot import ChannelBot
from app.models.group import Group, GroupMember
from app.models.integration import Integration
from app.models.mcp_server import MCPServer
from app.models.user import User
from tests.agentic_gate_stub import stub_agentic_gate
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _agentic_ai_available(monkeypatch):
    stub_agentic_gate(monkeypatch)


def _user(name: str, *, role: str = "user") -> User:
    return User(
        email=f"{name}-{uuid.uuid4().hex[:8]}@test.local",
        full_name=name.title(),
        hashed_password="x",
        role=role,
    )


def _integration(provider: str, **fields) -> Integration:
    return Integration(
        provider=provider,
        name=f"{provider}-{uuid.uuid4().hex[:6]}",
        auth_type="api_key",
        status="connected",
        **fields,
    )


def _channel(bot: str) -> dict:
    return {"channel_bot_id": bot, "publish_enabled": True, "publish_channels": []}


async def _seed(sessions) -> dict:
    """The owner, a member of the group the owner shares with, an admin, and
    rows the member cannot see: the owner's private ones, an admin's private
    ones, and company-managed ones restricted to another group."""
    async with sessions() as session:
        users = {
            "owner": _user("owner"),
            "member": _user("member"),
            "admin": _user("admin", role="admin"),
        }
        session.add_all(users.values())
        await session.flush()
        analysts = Group(name=f"analysts-{uuid.uuid4().hex[:8]}")
        others = Group(name=f"others-{uuid.uuid4().hex[:8]}")
        session.add_all([analysts, others])
        await session.flush()
        session.add(GroupMember(group_id=analysts.id, user_id=users["member"].id))
        owner, admin = users["owner"].id, users["admin"].id
        restricted = {"is_company_managed": True, "access_restricted": True,
                      "shared_group_ids": [str(others.id)]}
        servers = {
            "owner": MCPServer(name="owner-private", user_id=owner),
            "admin": MCPServer(name="admin-private", user_id=admin),
            "restricted": MCPServer(name="restricted", **restricted),
        }
        bots = {
            "owner": ChannelBot(
                provider="telegram_bot", name="owner-private", user_id=owner,
                is_company_managed=False,
            ),
            "admin": ChannelBot(
                provider="discord_bot", name="admin-private", user_id=admin,
                is_company_managed=False,
            ),
            "restricted": ChannelBot(provider="teams_bot", name="restricted", **restricted),
        }
        integrations = {
            "owner": _integration("notion", user_id=owner, is_company_managed=False),
            "admin": _integration("github", user_id=admin, is_company_managed=False),
            "restricted": _integration("confluence", user_id=admin, **restricted),
        }
        session.add_all([*servers.values(), *bots.values(), *integrations.values()])
        await session.commit()
        return {
            "users": {k: v.id for k, v in users.items()},
            "analysts": [str(analysts.id)],
            "servers": {k: str(v.id) for k, v in servers.items()},
            "bots": {k: str(v.id) for k, v in bots.items()},
            "integrations": {k: str(v.id) for k, v in integrations.items()},
        }


async def _agent(
    sessions,
    *,
    owner,
    server: str,
    bot: tuple[str, str],
    integration: tuple[str, str],
    company: bool = False,
    shared_with: list[str] | None = None,
    **assignment_fields,
) -> tuple[uuid.UUID, uuid.UUID]:
    """An agent bound to *server*, *bot* and *integration*, and one enabled
    manual assignment of it."""
    async with sessions() as session:
        agent = Agent(
            name=f"agent-{uuid.uuid4().hex[:6]}",
            slug=f"agent-{uuid.uuid4().hex[:8]}",
            persona="You are an agent.",
            enabled=True,
            user_id=owner,
            is_builtin=False,
            is_company_managed=company,
            shared_group_ids=shared_with,
            integration_bindings={integration[0]: [integration[1]]},
            channel_config={bot[0]: _channel(bot[1])},
            mcp_server_ids=[server],
            builtin_tool_ids=[],
            handover_agent_ids=[],
            output_format="markdown",
        )
        session.add(agent)
        await session.flush()
        assignment = Assignment(
            agent_id=agent.id,
            user_id=owner,
            is_company_managed=company,
            name=f"assignment-{uuid.uuid4().hex[:6]}",
            trigger_type="manual",
            trigger_config={},
            prompt="Do the thing.",
            enabled=True,
            dry_run=False,
            timeout_seconds=300,
            retry_on_failure=False,
            max_retries=0,
            notify_on_complete=False,
            execution_count=0,
            **assignment_fields,
        )
        session.add(assignment)
        await session.commit()
        return agent.id, assignment.id


def _stub_scheduling(monkeypatch) -> None:
    """The orchestration hand-off of a new run: committed, never enqueued."""

    async def _schedule(db, assignment, agent, run, **_kwargs):
        await db.commit()

    monkeypatch.setattr(
        "app.services.assignment_engine._schedule_run_execution", _schedule
    )


async def _run_of(sessions, assignment_id, user_id) -> uuid.UUID:
    async with sessions() as session:
        runs = (
            await session.execute(
                select(AssignmentRun.id).where(
                    AssignmentRun.assignment_id == assignment_id,
                    AssignmentRun.user_id == user_id,
                )
            )
        ).scalars().all()
    assert len(runs) == 1, runs
    return runs[0]


async def _execute(monkeypatch, sessions, run_id) -> SimpleNamespace:
    """Execute the run for real; spies on what it built its tools and its
    system prompt's channel context from."""
    from app.core.config import settings
    from app.llm.base import ChatMessage
    from app.services import assignment_engine, integration_tools
    from app.tools.wait_policy import WaitPolicy

    async def _fake_loop(**kwargs):
        kwargs["messages"].append(ChatMessage(role="assistant", content="done"))
        return SimpleNamespace(
            content="done",
            usage={"input_tokens": 1, "output_tokens": 1},
            model="model-x",
            stop_reason="end_turn",
        )

    build = AsyncMock(return_value=([], None))
    channels = AsyncMock(return_value=([], None))
    context = MagicMock(side_effect=integration_tools.format_channel_context)
    servers = AsyncMock(return_value=None)
    llm = SimpleNamespace(provider_name="anthropic", default_model="model-x", last_usage=None)
    for target, value in {
        "app.services.assignment_engine.async_session": sessions,
        "app.services.quota.check_agentic_quota": AsyncMock(),
        "app.services.quota.increment_agentic_usage": AsyncMock(),
        "app.services.quota.refund_agentic_usage_for_failure": AsyncMock(return_value=True),
        "app.services.quota.refund_agentic_usage_for_provider_outage": AsyncMock(return_value=True),
        "app.services.assignment_engine.record_agentic_execution": AsyncMock(),
        "app.services.assignment_engine._publish_run_status": AsyncMock(),
        "app.services.assignment_engine.event_bus": SimpleNamespace(emit=AsyncMock()),
        "app.services.variable_resolver.load_variables": AsyncMock(return_value={}),
        "app.llm.router.get_llm_for_user": AsyncMock(return_value=(llm, "model-x")),
        "app.services.langfuse_tracing.trace_llm_call": AsyncMock(return_value=None),
        "app.services.assignment_engine._build_trigger_information": lambda _run: "",
        "app.services.assignment_engine._load_memory_context": AsyncMock(return_value=("", "")),
        "app.services.builtin_tools.load_kb_names": AsyncMock(return_value={}),
        "app.services.builtin_tools.load_write_locked_kb_ids": AsyncMock(return_value=set()),
        "app.services.assignment_engine.load_authorized_trigger_files": AsyncMock(return_value=[]),
        "app.services.builtin_tools.load_mcp_tools": servers,
        "app.services.builtin_tools.load_custom_tools": AsyncMock(return_value=None),
        "app.services.integration_tools.build_integration_tools": build,
        "app.services.integration_tools.build_channel_tools": channels,
        "app.services.integration_tools.format_channel_context": context,
        "app.services.assignment_engine.resolve_run_max_iterations": AsyncMock(return_value=10),
        "app.services.assignment_engine.resolve_run_wait_policy": AsyncMock(return_value=WaitPolicy()),
        "app.tools.run_budget.build_run_budget_for_run": AsyncMock(return_value=None),
        "app.tools.agent_loop.run_agent_loop": _fake_loop,
        "app.services.tool_tracking.track_tool_usage": AsyncMock(),
        "app.services.assignment_engine._track_assignment_auth_failures": AsyncMock(),
        "app.services.assignment_engine._track_stale_reference_failure": AsyncMock(),
        "app.services.integration_tools.cancel_run_partner_tasks": AsyncMock(),
        "app.services.builtin_tools.cleanup_mcp_manager": AsyncMock(),
        "app.services.assignment_engine.create_execution": AsyncMock(),
    }.items():
        monkeypatch.setattr(target, value)
    monkeypatch.setattr(settings, "agent_self_fanout_enabled", False)

    await assignment_engine.execute_assignment_run(str(run_id))
    async with sessions() as session:
        run = await session.get(AssignmentRun, run_id)
        assert run.status == "completed", run.error
    return SimpleNamespace(
        servers=list(servers.await_args.args[1]),
        integrations=dict(build.await_args.args[1]) if build.await_count else {},
        channels=channels.await_args.args[1] if channels.await_count else None,
        prompted=context.call_args.args[0] if context.call_count else None,
    )


# --- The entry points ---------------------------------------------------------


async def _manual_trigger(sessions, assignment_id, user_id) -> None:
    """``POST /assignments/{id}/trigger``: the run acts for the caller."""
    from app.api.v1.assignments import trigger_assignment_manual

    request = Request({"type": "http", "method": "POST", "path": "/x", "headers": []})
    async with sessions() as db:
        await trigger_assignment_manual(
            str(assignment_id),
            request,
            body=None,
            user=await db.get(User, user_id),
            db=db,
            _ai_gate=None,
        )


async def _work_item_delegation(sessions, assignment_id, user_id) -> None:
    """``delegate_to_assignment``: the run acts for the delegating user."""
    from app.services.builtin_tools.work_items import _run_delegate_to_assignment

    reply = await _run_delegate_to_assignment(
        assignment_id=str(assignment_id),
        task="Handle it.",
        acting_user_id=str(user_id),
        delegating_agent_id=None,
    )
    assert reply.startswith("Delegated to assignment"), reply


async def _per_user_fanout(sessions, assignment_id, _user_id) -> None:
    """The scheduled fan-out of a ``run_as_all_users`` assignment: one run per
    active user, each acting for that user."""
    from app.services.autonomous import _fanout_assignment_to_users

    async with sessions() as db:
        assignment = await db.get(Assignment, assignment_id)
        agent = await db.get(Agent, assignment.agent_id)
        triggered = await _fanout_assignment_to_users(
            db, assignment, agent, datetime.now(UTC), "0 9 * * *"
        )
    assert triggered == 3


async def _run_as_trigger(sessions, assignment_id, _user_id) -> None:
    """An automatic trigger: the run acts for the assignment's run-as user."""
    from app.services.assignment_engine import trigger_assignment

    async with sessions() as db:
        assignment = await db.get(Assignment, assignment_id)
        agent = await db.get(Agent, assignment.agent_id)
        await trigger_assignment(
            assignment, agent, db=db, trigger_context={"type": "schedule"}
        )


# --- Tests --------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "trigger",
    [_manual_trigger, _work_item_delegation],
    ids=["manual-trigger", "work-item-delegation"],
)
async def test_a_group_members_run_uses_the_owners_private_bindings(monkeypatch, trigger):
    """A member of the group an agent is shared with runs one of its
    assignments: the run acts for the member and still uses the owner's
    private MCP server and channel bot, as the agent names them, but not the
    owner's private integration, which the agent does not lend."""
    _stub_scheduling(monkeypatch)
    async with scratch_database("runbind", tables=ALL_TABLES, extensions=("vector",)) as db:
        w = await _seed(db.sessions)
        users, servers, bots, integrations = (
            w["users"], w["servers"], w["bots"], w["integrations"]
        )
        _agent_id, assignment_id = await _agent(
            db.sessions,
            owner=users["owner"],
            shared_with=w["analysts"],
            server=servers["owner"],
            bot=("telegram", bots["owner"]),
            integration=("notion", integrations["owner"]),
        )

        await trigger(db.sessions, assignment_id, users["member"])
        seen = await _execute(
            monkeypatch, db.sessions, await _run_of(db.sessions, assignment_id, users["member"])
        )

        assert seen.servers == [servers["owner"]]
        assert seen.integrations == {}
        assert seen.channels == {"telegram": _channel(bots["owner"])}
        assert seen.prompted == {"telegram": _channel(bots["owner"])}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("trigger", "assignment_fields"),
    [
        (_per_user_fanout, {"run_as_all_users": True}),
        (_run_as_trigger, {"run_as_user_id": "member"}),
    ],
    ids=["per-user-fan-out", "run-as-user"],
)
async def test_a_company_managed_automation_runs_as_configured_for_every_user(
    monkeypatch, trigger, assignment_fields
):
    """A company-managed agent bound to company-managed rows restricted to a
    group the member is not in: a run acting for the member - their fan-out
    run, or the run of an assignment that runs as them - uses the MCP server
    and the channel bot, not the integration the member may not use."""
    _stub_scheduling(monkeypatch)
    async with scratch_database("runbind", tables=ALL_TABLES, extensions=("vector",)) as db:
        w = await _seed(db.sessions)
        users, servers, bots, integrations = (
            w["users"], w["servers"], w["bots"], w["integrations"]
        )
        fields = {
            key: users[value] if key == "run_as_user_id" else value
            for key, value in assignment_fields.items()
        }
        _agent_id, assignment_id = await _agent(
            db.sessions,
            owner=None,
            company=True,
            server=servers["restricted"],
            bot=("teams", bots["restricted"]),
            integration=("confluence", integrations["restricted"]),
            **fields,
        )

        await trigger(db.sessions, assignment_id, users["member"])
        seen = await _execute(
            monkeypatch, db.sessions, await _run_of(db.sessions, assignment_id, users["member"])
        )

        assert seen.servers == [servers["restricted"]]
        assert seen.integrations == {}
        assert seen.channels == {"teams": _channel(bots["restricted"])}
        assert seen.prompted == {"teams": _channel(bots["restricted"])}


@pytest.mark.asyncio
@pytest.mark.parametrize("runner", ["owner", "member"])
async def test_an_assignment_override_uses_every_row_it_names(monkeypatch, runner):
    """An override an admin wrote with their own private MCP server,
    integration and channel bot (an admin may bind any row): the owner's run
    and a group member's run both use its MCP server and channel bot as stored,
    never the agent's lists. The admin's integration is not the agent owner's
    binding: neither run may use it, so neither gets it."""
    _stub_scheduling(monkeypatch)
    async with scratch_database("runbind", tables=ALL_TABLES, extensions=("vector",)) as db:
        w = await _seed(db.sessions)
        users, servers, bots, integrations = (
            w["users"], w["servers"], w["bots"], w["integrations"]
        )
        override_channels = {"discord": _channel(bots["admin"])}
        _agent_id, assignment_id = await _agent(
            db.sessions,
            owner=users["owner"],
            shared_with=w["analysts"],
            server=servers["owner"],
            bot=("telegram", bots["owner"]),
            integration=("notion", integrations["owner"]),
            tool_sources=[servers["admin"]],
            integration_bindings={"github": [integrations["admin"]]},
            channel_config=override_channels,
        )

        await _manual_trigger(db.sessions, assignment_id, users[runner])
        seen = await _execute(
            monkeypatch, db.sessions, await _run_of(db.sessions, assignment_id, users[runner])
        )

        assert seen.servers == [servers["admin"]]
        assert seen.integrations == {}
        assert seen.channels == override_channels
        assert seen.prompted == override_channels
