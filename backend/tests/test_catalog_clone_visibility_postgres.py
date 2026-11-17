"""Installing a catalog agent as a NON-admin, against real PostgreSQL.

"Add to my account" decides which of the publisher's resources the clone may
use BY REFERENCE, and that decision is the three-tier visibility predicate
(``user_visible_filter``) - which only a real database evaluates, and which an
admin short-circuits. So every security assertion here is made as an ordinary
user, with an admin install as the contrast that proves the check is per
caller:

* an integration the installer names in the setup body must be one they may
  use - naming another user's is refused (it would run with that user's
  credentials: assignment runs do not re-check a fixed binding);
* a company-managed resource narrowed to an audience (``access_restricted``)
  the installer is not in is never bound by reference, and its content is
  never copied, listed or downloaded for them;
* what the catalog discloses about referenced variables is bounded by what the
  agent's owner could resolve.

``integration``-marked and skips when PostgreSQL is unreachable, like its
neighbours.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.api.v1 import public_catalog
from app.core.config import settings
from app.models.agent import Agent
from app.models.agent_skill import AgentSkill
from app.models.assignment import Assignment
from app.models.custom_tool import CustomTool
from app.models.group import Group, GroupMember
from app.models.integration import Integration
from app.models.knowledge_base import KnowledgeBase
from app.models.mcp_server import MCPServer
from app.models.prompt import Prompt
from app.models.prompt_version import PromptVersion
from app.models.user import User
from app.models.variable import Variable
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration


@asynccontextmanager
async def _temp_database(prefix: str) -> AsyncIterator[async_sessionmaker]:
    """Isolated database with the full schema, dropped on exit."""
    async with scratch_database(
        prefix,
        tables=ALL_TABLES,
        extensions=("vector",),
        skip_without_extensions=True,
        bind_app_sessions=False,
    ) as db:
        yield db.sessions


def _user(role: str, name: str) -> User:
    return User(
        email=f"{name}-{uuid.uuid4().hex[:8]}@example.com",
        hashed_password="x",
        full_name=name.title(),
        role=role,
    )


async def _seed(session: async_sessionmaker) -> SimpleNamespace:
    """A published agent bound to resources narrowed to an audience the
    installer is not in, plus another user's private integration and variable."""
    async with session() as db:
        publisher = _user("admin", "publisher")
        installer = _user("user", "installer")
        victim = _user("user", "victim")
        admin = _user("admin", "admin")
        db.add_all([publisher, installer, victim, admin])
        await db.flush()

        audience = Group(name=f"audience-{uuid.uuid4().hex[:8]}", created_by=publisher.id)
        db.add(audience)
        await db.flush()
        db.add(GroupMember(group_id=audience.id, user_id=publisher.id))
        narrowed = {
            "is_company_managed": True,
            "access_restricted": True,
            "shared_group_ids": [str(audience.id)],
        }

        victim_integration = Integration(
            user_id=victim.id, provider="github", name="Victim GitHub",
            auth_type="api_key", status="connected",
        )
        installer_integration = Integration(
            user_id=installer.id, provider="github", name="Installer GitHub",
            auth_type="api_key", status="connected",
        )
        open_integration = Integration(
            user_id=publisher.id, provider="opencti", name="Shared OpenCTI",
            auth_type="api_key", status="connected", is_company_managed=True,
        )
        restricted_integration = Integration(
            user_id=publisher.id, provider="slack", name="Audience Slack",
            auth_type="api_key", status="connected", **narrowed,
        )
        restricted_kb = KnowledgeBase(user_id=publisher.id, name="Audience KB", **narrowed)
        restricted_server = MCPServer(
            user_id=publisher.id, name="Audience MCP", transport="http",
            url="https://mcp.example.invalid", **narrowed,
        )
        restricted_tool = CustomTool(
            user_id=publisher.id, name="Audience tool", description="d",
            js_function="return 1", **narrowed,
        )
        restricted_skill = AgentSkill(
            user_id=publisher.id, name="Audience skill", content="c", **narrowed
        )
        restricted_prompt = Prompt(
            user_id=publisher.id, name="Audience prompt", description="d", content="c",
            **narrowed,
        )
        victim_variable = Variable(
            user_id=victim.id, name="VICTIM_VAR", value="victim-value", is_secret=False
        )
        db.add_all(
            [
                victim_integration, installer_integration, open_integration,
                restricted_integration, restricted_kb, restricted_server,
                restricted_tool, restricted_skill, restricted_prompt, victim_variable,
            ]
        )
        await db.flush()

        agent = Agent(
            user_id=publisher.id,
            name="Published",
            slug=f"published-{uuid.uuid4().hex[:8]}",
            is_company_managed=True,
            is_public_catalog=True,
            persona=f"Region: {{{{var_id.{victim_variable.id}}}}}",
            integration_bindings={
                "slack": [str(restricted_integration.id)],
                "opencti": [str(open_integration.id)],
            },
            knowledge_base_ids=[str(restricted_kb.id)],
            mcp_server_ids=[str(restricted_server.id)],
            custom_tool_ids=[str(restricted_tool.id)],
            skill_ids=[str(restricted_skill.id)],
            prompt_ids=[str(restricted_prompt.id)],
            budget_config={"max_tool_calls": 5000},
        )
        db.add(agent)
        await db.flush()
        db.add(
            Assignment(
                agent_id=agent.id,
                user_id=publisher.id,
                name="Poll",
                trigger_type="integration_event",
                trigger_config={
                    "provider": "slack",
                    "event": "new_message",
                    "integration_id": str(restricted_integration.id),
                    "owner_agent_id": str(agent.id),
                },
                prompt="Summarise.",
                enabled=True,
            )
        )
        await db.commit()
        return SimpleNamespace(
            agent_id=agent.id,
            installer_id=installer.id,
            admin_id=admin.id,
            victim_integration=str(victim_integration.id),
            installer_integration=str(installer_integration.id),
            open_integration=str(open_integration.id),
            restricted_integration=str(restricted_integration.id),
            restricted_kb=str(restricted_kb.id),
            restricted_server=str(restricted_server.id),
            restricted_tool=str(restricted_tool.id),
            restricted_skill=str(restricted_skill.id),
            restricted_prompt=str(restricted_prompt.id),
            victim_variable=str(victim_variable.id),
        )


async def _install(session, seed, user_id, bindings=None):
    async with session() as db:
        user = await db.get(User, user_id)
        body = public_catalog.AgentSetupConfig(integration_bindings=bindings)
        response = await public_catalog.add_catalog_item_to_account(
            entity_type="agents",
            entity_id=str(seed.agent_id),
            body=body,
            user=user,
            db=db,
            _ai_gate=None,
        )
    async with session() as db:
        clone = await db.get(Agent, uuid.UUID(response.id))
        [assignment] = (
            await db.execute(select(Assignment).where(Assignment.agent_id == clone.id))
        ).scalars().all()
        copied_values = (
            await db.execute(select(Variable.value).where(Variable.user_id == user_id))
        ).scalars().all()
        return clone, assignment, copied_values


@pytest.mark.asyncio
@pytest.mark.parametrize("as_list", [True, False], ids=["wizard-list", "bare-id"])
async def test_a_crafted_install_cannot_bind_another_users_integration(as_list):
    async with _temp_database("catalog_clone_crafted") as session:
        seed = await _seed(session)
        chosen = [seed.victim_integration] if as_list else seed.victim_integration

        with pytest.raises(HTTPException) as exc:
            await _install(session, seed, seed.installer_id, {"github": chosen})
        assert exc.value.status_code == 400

        async with session() as db:
            agents = (await db.execute(select(func.count()).select_from(Agent))).scalar_one()
        assert agents == 1, "a refused install must not leave a clone behind"


@pytest.mark.asyncio
async def test_a_non_admin_installer_never_gets_a_narrowed_resource_by_reference(monkeypatch):
    monkeypatch.setattr(settings, "agent_loop_max_tool_calls", 50)
    async with _temp_database("catalog_clone_nonadmin") as session:
        seed = await _seed(session)

        clone, assignment, copied_values = await _install(
            session, seed, seed.installer_id, {"github": [seed.installer_integration]}
        )

        # The audience's Slack is not the installer's to use; the shared
        # OpenCTI and the installer's own GitHub are.
        assert clone.integration_bindings == {
            "opencti": [seed.open_integration],
            "github": [seed.installer_integration],
        }
        # Narrowed resources are never bound by reference. The knowledge base
        # is recreated empty; the server, tool, skill and prompt are left out
        # - a private copy would hand over content that is not the
        # installer's to read.
        assert seed.restricted_kb not in (clone.knowledge_base_ids or [])
        assert len(clone.knowledge_base_ids or []) == 1
        assert not clone.mcp_server_ids
        assert not clone.custom_tool_ids
        assert not clone.skill_ids
        assert not clone.prompt_ids
        async with session() as db:
            for model in (MCPServer, CustomTool, AgentSkill, Prompt):
                copies = (
                    await db.execute(
                        select(func.count()).select_from(model).where(
                            model.user_id == seed.installer_id
                        )
                    )
                ).scalar_one()
                assert copies == 0, model.__name__
        # The publisher's budget is carried at the installer's ceiling.
        assert clone.budget_config["max_tool_calls"] == 50
        # The trigger polls nothing and drains nothing of the publisher's.
        assert "integration_id" not in assignment.trigger_config
        assert "owner_agent_id" not in assignment.trigger_config
        # Another user's variable, named in the persona, is not copied.
        assert "victim-value" not in copied_values
        # Like every other creation path: a unique slug (the MCP agent endpoint
        # names each tool after it) and an "Initial version" baseline.
        assert clone.slug == "published-copy"
        async with session() as db:
            versions = (
                await db.execute(
                    select(PromptVersion.commit_message).where(
                        PromptVersion.agent_id == clone.id
                    )
                )
            ).scalars().all()
        assert versions == ["Initial version"]


@pytest.mark.asyncio
async def test_an_admin_installer_binds_what_the_role_lets_them_see():
    """The contrast: the same install by an admin binds the narrowed resources,
    so the refusals above are the per-caller predicate, not a blanket rule."""
    async with _temp_database("catalog_clone_admin") as session:
        seed = await _seed(session)

        clone, assignment, _ = await _install(session, seed, seed.admin_id)

        assert clone.integration_bindings["slack"] == [seed.restricted_integration]
        assert clone.knowledge_base_ids == [seed.restricted_kb]
        assert clone.mcp_server_ids == [seed.restricted_server]
        assert clone.custom_tool_ids == [seed.restricted_tool]
        assert clone.skill_ids == [seed.restricted_skill]
        assert clone.prompt_ids == [seed.restricted_prompt]
        assert assignment.trigger_config["integration_id"] == seed.restricted_integration
        assert clone.budget_config["max_tool_calls"] == 5000


async def _install_response(session, seed, user_id, bindings=None):
    async with session() as db:
        return await public_catalog.add_catalog_item_to_account(
            entity_type="agents",
            entity_id=str(seed.agent_id),
            body=public_catalog.AgentSetupConfig(integration_bindings=bindings),
            user=await db.get(User, user_id),
            db=db,
            _ai_gate=None,
        )


@pytest.mark.asyncio
async def test_an_install_says_what_it_did_not_copy_and_why():
    """Nothing is left out silently: the response names, per kind,
    what the installer's copy does not carry, counts only - never a name of a
    resource the installer cannot see. The admin, who may use all of it,
    is told nothing."""
    async with _temp_database("catalog_clone_said") as session:
        seed = await _seed(session)

        member = await _install_response(
            session, seed, seed.installer_id, {"github": [seed.installer_integration]}
        )
        admin = await _install_response(session, seed, seed.admin_id)

    assert member.warnings == [
        "1 integration of the agent was not connected: it is not shared with you. "
        "Connect your own in the copy.",
        "1 MCP server of the agent was not copied: it is not shared with you.",
        "1 knowledge base of the agent was copied as your own, empty: add its documents.",
        "1 custom tool of the agent was not copied: it is not shared with you.",
        "1 skill of the agent was not copied: it is not shared with you.",
        "1 prompt of the agent was not copied: it is not shared with you.",
    ]
    for line in member.warnings:
        assert "Audience" not in line
    assert admin.warnings == []


@pytest.mark.asyncio
async def test_an_install_says_what_a_catalog_copy_never_carries():
    """Handovers and channel bots never travel with a catalog copy, and an
    assignment limited to a server outside the agent's set runs without it:
    each is said."""
    async with _temp_database("catalog_clone_never") as session:
        seed = await _seed(session)
        async with session() as db:
            agent = await db.get(Agent, seed.agent_id)
            agent.handover_agent_ids = [str(uuid.uuid4()), str(uuid.uuid4())]
            agent.channel_config = {"telegram": {"channel_bot_id": str(uuid.uuid4())}}
            [assignment] = (
                await db.execute(select(Assignment).where(Assignment.agent_id == agent.id))
            ).scalars().all()
            assignment.tool_sources = [str(uuid.uuid4())]
            await db.commit()

        response = await _install_response(session, seed, seed.admin_id)

    assert response.warnings == [
        "2 handover agents of the agent were not copied: a catalog copy never carries handovers.",
        "1 channel setting of the agent was not copied: a catalog copy never carries channel bots.",
        "1 assignment MCP server of the agent was not copied: it is not one of the agent's "
        "servers, so the assignment runs without it.",
    ]


@pytest.mark.asyncio
async def test_the_catalog_discloses_only_what_the_owner_could_resolve():
    """Anyone may publish their own agent: a persona naming someone else's
    variable must not turn the catalog page or the download into a way to read
    it. A narrowed integration or knowledge base is not presented as
    auto-bound to a reader who will not get it, and a narrowed tool, server,
    skill or prompt is neither listed nor downloaded for that reader."""
    async with _temp_database("catalog_disclosure") as session:
        seed = await _seed(session)

        async with session() as db:
            installer = await db.get(User, seed.installer_id)
            detail = json.loads(
                (
                    await public_catalog.get_public_agent(
                        agent_id=str(seed.agent_id), user=installer, db=db
                    )
                ).body
            )
            bundle = json.loads(
                (
                    await public_catalog.download_catalog_item(
                        entity_type="agents",
                        entity_id=str(seed.agent_id),
                        user=installer,
                        db=db,
                    )
                ).body
            )

        assert seed.victim_variable not in {v["id"] for v in detail["variables"]}
        assert seed.victim_variable not in {v["id"] for v in bundle["variables"]}
        assert "victim-value" not in json.dumps(detail)
        assert "victim-value" not in json.dumps(bundle)

        slack = next(p for p in detail["integration_providers"] if p["provider"] == "slack")
        assert slack["is_company_managed"] is False
        assert "integration_id" not in slack
        opencti = next(p for p in detail["integration_providers"] if p["provider"] == "opencti")
        assert opencti["is_company_managed"] is True
        assert [kb["is_company_managed"] for kb in detail["knowledge_bases"]] == [False]
        for section in ("custom_tools", "mcp_servers", "skills", "prompts"):
            assert detail[section] == [], section
            assert bundle[section] == [], section
        for name in ("Audience tool", "Audience MCP", "Audience skill", "Audience prompt"):
            assert name not in json.dumps(bundle), name


@pytest.mark.asyncio
async def test_an_export_by_a_non_manager_leaves_the_narrowed_resources_out(monkeypatch):
    """``GET /agents/{id}/export`` by a user who can see the company-managed
    agent but not manage it is the catalog download's projection: the
    narrowed tool, server, skill and prompt are not theirs to read. An admin,
    who manages the agent, still exports every one of them."""
    from app.api.v1 import agents as agents_api

    async def _no_audit(*_args, **_kwargs):
        return None

    monkeypatch.setattr(agents_api, "log_audit", _no_audit)
    async with _temp_database("agent_export_projection") as session:
        seed = await _seed(session)

        async def _export(user_id):
            async with session() as db:
                user = await db.get(User, user_id)
                response = await agents_api.export_agent(
                    agent_id=str(seed.agent_id), request=None, format="native", user=user, db=db
                )
            return json.loads(response.body)

        projected = await _export(seed.installer_id)
        full = await _export(seed.admin_id)

    for section in ("custom_tools", "mcp_servers", "skills", "prompts"):
        assert projected[section] == [], section
        assert len(full[section]) == 1, section
    for name in ("Audience tool", "Audience MCP", "Audience skill", "Audience prompt"):
        assert name not in json.dumps(projected), name
        assert name in json.dumps(full), name
