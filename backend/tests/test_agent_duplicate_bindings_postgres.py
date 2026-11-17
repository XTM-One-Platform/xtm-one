"""A duplicate only carries the bindings its copier could bind.

The copy is a new personal agent owned by the copier, so each binding it
carries goes through the rule agent create / update apply to that copier
(three-tier visibility, the channel checks); the rest is left out and counted
in the response. An owner copying their own agent and an admin copy exactly
as before. The visibility predicate short-circuits for admins and reads the
per-user sharing columns otherwise, which a mock session cannot show: the real
handler runs here against PostgreSQL. Same scratch-database recipe as
``test_assignment_stale_reference_postgres.py``.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.models  # noqa: F401 - registers every mapper on Base.metadata
from app.api.v1 import agents as agents_api
from app.models.agent import Agent
from app.models.agent_skill import AgentSkill
from app.models.assignment import Assignment
from app.models.channel_bot import ChannelBot
from app.models.custom_tool import CustomTool
from app.models.group import Group, GroupMember
from app.models.integration import Integration
from app.models.knowledge_base import KnowledgeBase
from app.models.mcp_server import MCPServer
from app.models.prompt import Prompt
from app.models.user import User
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration


@asynccontextmanager
async def _temp_database() -> AsyncIterator[async_sessionmaker]:
    async with scratch_database(
        "dupbind",
        tables=ALL_TABLES,
        extensions=("vector",),
        bind_app_sessions=False,
    ) as db:
        yield db.sessions


@dataclass
class _World:
    owner: uuid.UUID
    member: uuid.UUID
    admin: uuid.UUID
    shared_agent: uuid.UUID
    ids: dict[str, str]


def _user(name: str, *, role: str = "user") -> User:
    return User(
        email=f"{name}-{uuid.uuid4().hex[:8]}@test.local",
        full_name=name.title(),
        hashed_password="x",
        role=role,
    )


async def _seed(sessions) -> _World:
    """An owner's agent shared with a group (``config``), bound to the owner's
    private resources, company-managed ones and the member's own."""
    async with sessions() as session:
        owner, member, admin = _user("owner"), _user("member"), _user("admin", role="admin")
        session.add_all([owner, member, admin])
        await session.flush()
        group = Group(name=f"analysts-{uuid.uuid4().hex[:8]}")
        session.add(group)
        await session.flush()
        session.add(GroupMember(group_id=group.id, user_id=member.id))

        def _mcp(name, **kw):
            return MCPServer(name=name, transport="streamable_http", url="https://example.invalid", **kw)

        rows = {
            "srv_private": _mcp("Owner server", user_id=owner.id),
            "srv_company": _mcp("Company server", user_id=None, is_company_managed=True),
            "srv_member": _mcp("Member server", user_id=member.id),
            "int_private": Integration(provider="github", name="Owner GitHub", user_id=owner.id),
            "int_company": Integration(
                provider="github", name="Company GitHub", user_id=None, is_company_managed=True
            ),
            "int_member": Integration(provider="slack", name="Member Slack", user_id=member.id),
            "kb_private": KnowledgeBase(name="Owner KB", user_id=owner.id),
            "kb_company": KnowledgeBase(name="Company KB", user_id=None, is_company_managed=True),
            "tool_private": CustomTool(name="Owner tool", user_id=owner.id),
            "tool_member": CustomTool(name="Member tool", user_id=member.id),
            "skill_private": AgentSkill(name="Owner skill", user_id=owner.id),
            "prompt_private": Prompt(name="Owner prompt", user_id=owner.id),
            "prompt_company": Prompt(name="Company prompt", user_id=None, is_company_managed=True),
            "handover_private": Agent(name="Owner helper", user_id=owner.id),
            # A company bot with no channel whitelist: a personal agent may not use it.
            "bot_company": ChannelBot(provider="slack", name="Company Slack bot", user_id=None),
            # The owner's private bot: whitelist rules pass, visibility does not.
            "bot_private": ChannelBot(
                provider="teams", name="Owner Teams bot", user_id=owner.id, is_company_managed=False
            ),
        }
        session.add_all(rows.values())
        await session.flush()
        ids = {key: str(row.id) for key, row in rows.items()}

        shared = Agent(
            name="Shared analyst",
            user_id=owner.id,
            shared_group_ids=[str(group.id)],
            shared_group_permissions={str(group.id): "config"},
            mcp_server_ids=[ids["srv_private"], ids["srv_company"], ids["srv_member"]],
            mcp_allowed_tools={
                ids["srv_private"]: ["read"],
                ids["srv_company"]: ["search"],
            },
            integration_bindings={
                "github": [ids["int_private"], ids["int_company"]],
                "slack": [ids["int_member"]],
            },
            knowledge_base_ids=[ids["kb_private"], ids["kb_company"]],
            custom_tool_ids=[ids["tool_private"], ids["tool_member"]],
            skill_ids=[ids["skill_private"]],
            skill_load_modes={ids["skill_private"]: "always"},
            prompt_ids=[ids["prompt_private"], ids["prompt_company"]],
            handover_agent_ids=[ids["handover_private"]],
            handover_types={ids["handover_private"]: "consult"},
            channel_config={
                "slack": {"channel_bot_id": ids["bot_company"], "receive_enabled": True},
                "teams": {"channel_bot_id": ids["bot_private"], "receive_enabled": True},
            },
            available_channel_types=["web", "slack", "teams"],
        )
        session.add(shared)
        await session.flush()
        session.add_all(
            [
                Assignment(
                    agent_id=shared.id,
                    user_id=owner.id,
                    name="Owner integration only",
                    trigger_type="manual",
                    prompt="Work",
                    integration_bindings={"github": [ids["int_private"]]},
                    channel_config={
                        "teams": {"channel_bot_id": ids["bot_private"], "publish_enabled": True}
                    },
                ),
                Assignment(
                    agent_id=shared.id,
                    user_id=owner.id,
                    name="Company integration",
                    trigger_type="manual",
                    prompt="Work",
                    integration_bindings={"github": [ids["int_company"]]},
                ),
            ]
        )
        await session.commit()
        return _World(owner.id, member.id, admin.id, shared.id, ids)


_BINDING_FIELDS = (
    "mcp_server_ids",
    "mcp_allowed_tools",
    "integration_bindings",
    "knowledge_base_ids",
    "custom_tool_ids",
    "skill_ids",
    "skill_load_modes",
    "prompt_ids",
    "handover_agent_ids",
    "handover_types",
    "channel_config",
    "available_channel_types",
)


async def _duplicate(sessions, user_id, agent_id):
    async with sessions() as session:
        user = await session.get(User, user_id)
        response = await agents_api.duplicate_agent(
            agent_id=str(agent_id),
            request=None,
            user=user,
            db=session,
            _agentic_gate=None,
        )
    async with sessions() as session:
        copy = await session.get(Agent, uuid.UUID(response.id))
        rows = await session.execute(select(Assignment).where(Assignment.agent_id == copy.id))
        scopes = {
            wf.name: (wf.integration_bindings, wf.channel_config) for wf in rows.scalars().all()
        }
        source = await session.get(Agent, agent_id)
    return response, copy, scopes, source


@pytest.mark.asyncio
async def test_a_group_member_copy_leaves_out_what_the_member_cannot_use():
    async with _temp_database() as sessions:
        world = await _seed(sessions)
        response, copy, scopes, _source = await _duplicate(
            sessions, world.member, world.shared_agent
        )
    ids = world.ids

    assert copy.mcp_server_ids == [ids["srv_company"], ids["srv_member"]]
    assert copy.mcp_allowed_tools == {ids["srv_company"]: ["search"]}
    assert copy.integration_bindings == {
        "github": [ids["int_company"]],
        "slack": [ids["int_member"]],
    }
    assert copy.knowledge_base_ids == [ids["kb_company"]]
    assert copy.custom_tool_ids == [ids["tool_member"]]
    assert copy.skill_ids is None and copy.skill_load_modes is None
    assert copy.prompt_ids == [ids["prompt_company"]]
    assert copy.handover_agent_ids is None and copy.handover_types is None
    assert copy.channel_config is None
    assert copy.available_channel_types == ["web"]
    # An assignment override nothing survives from is "none", never "inherit".
    assert scopes == {
        "Owner integration only": ({}, {}),
        "Company integration": ({"github": [ids["int_company"]]}, None),
    }

    assert len(response.warnings) == 10
    joined = " ".join(response.warnings)
    for private in ("Owner", *ids.values()):
        assert private not in joined


@pytest.mark.asyncio
@pytest.mark.parametrize("who", ["owner", "admin"])
async def test_an_owner_or_admin_copy_is_unchanged(who):
    async with _temp_database() as sessions:
        world = await _seed(sessions)
        response, copy, scopes, source = await _duplicate(
            sessions, getattr(world, who), world.shared_agent
        )

    for field in _BINDING_FIELDS:
        assert getattr(copy, field) == getattr(source, field), field
    assert scopes == {
        "Owner integration only": (
            {"github": [world.ids["int_private"]]},
            {"teams": {"channel_bot_id": world.ids["bot_private"], "publish_enabled": True}},
        ),
        "Company integration": ({"github": [world.ids["int_company"]]}, None),
    }
    assert response.warnings == []
