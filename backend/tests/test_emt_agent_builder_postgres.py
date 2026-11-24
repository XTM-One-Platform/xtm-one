"""The delegated-agent acceptance path, against real PostgreSQL.

Building the "[EMT] Executive Assistant" from the CEO's assistant must take:

* ONE ``create_agent`` call for the complete agent - model settings, built-in
  tools, memory flags, scoping, deny list, read-only bindings - with every one
  of its 17 integration bindings listed back (two LinkedIn, two Planhat), and
  nothing left behind when a field is refused;
* a background task to the agent it just created, with no handover binding;
* ``clone_agent`` for a variant, as a diff against the source;
* a read-only binding whose write tool is refused when called;
* integrations shared with a group, which an agent shared with that group
  resolves only when its member may use them.

``integration``-marked and skips when PostgreSQL is unreachable.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.models  # noqa: F401 - registers every table on Base.metadata
import app.services.builtin_tools as builtin_tools_module
from app.core.security import encrypt_value
from app.models.agent import Agent
from app.models.assignment import Assignment
from app.models.group import Group, GroupMember
from app.models.integration import Integration
from app.models.knowledge_base import KnowledgeBase
from app.models.user import User
from app.services import sub_agent
from app.services.builtin_tool_aliases import alias_scoped_functions
from app.services.builtin_tools import build_agent_builder_tool, build_sub_agent_tools
from app.services.builtin_tools.handover import owned_target_resolver, resolve_owned_delegation_target
from app.services.integration_read_only import READ_ONLY_CONTEXT_KEY, read_only_integration_ids
from app.services.integration_tools import (
    BindingBasis,
    ToolBindings,
    build_integration_tools,
    shadow_invisible_bindings,
)
from app.tools.base import ToolCall
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration

#: The EMT agent's 17 bindings: two LinkedIn accounts and two Planhat
#: workspaces among them, the case whose count read "Integrations: 15".
EMT_PROVIDERS = (
    ("linkedin", "LinkedIn Filigran"),
    ("linkedin", "LinkedIn Samuel"),
    ("planhat", "Planhat EU"),
    ("planhat", "Planhat US"),
    ("microsoft", "Executive mailbox"),
    ("google", "Google Workspace"),
    ("slack", "Slack"),
    ("notion", "Notion"),
    ("hubspot", "HubSpot"),
    ("jira", "Jira"),
    ("github", "GitHub"),
    ("perk", "Perk"),
    ("figma", "Figma"),
    ("drata", "Drata"),
    ("ashby", "Ashby"),
    ("hibob", "HiBob"),
    ("grafana", "Grafana"),
)


@asynccontextmanager
async def _database() -> AsyncIterator[async_sessionmaker]:
    async with scratch_database(
        "emtbuild", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True
    ) as db:
        yield db.sessions


async def _tool(caller: User, tool: str, /, **arguments) -> str:
    _, executor = build_agent_builder_tool(None, str(caller.id))
    result = await executor.execute_tool(ToolCall(id=str(uuid.uuid4()), name=tool, arguments=arguments))
    return result.content


async def _user(session, name: str, *, admin: bool = False) -> User:
    user = User(
        id=uuid.uuid4(),
        email=f"{name}-{uuid.uuid4().hex[:6]}@example.com",
        full_name=name,
        hashed_password="x",
        role="admin" if admin else "user",
    )
    session.add(user)
    await session.flush()
    return user


async def _integration(session, owner: User, provider: str, name: str, **fields) -> Integration:
    integration = Integration(
        id=uuid.uuid4(),
        user_id=owner.id,
        provider=provider,
        name=name,
        auth_type="api_key",
        api_key_enc=encrypt_value("secret-key"),
        status="connected",
        **fields,
    )
    session.add(integration)
    await session.flush()
    return integration


async def _group(session, name: str, *members: User) -> Group:
    group = Group(id=uuid.uuid4(), name=f"{name}-{uuid.uuid4().hex[:6]}")
    session.add(group)
    await session.flush()
    for member in members:
        session.add(GroupMember(group_id=group.id, user_id=member.id))
    await session.flush()
    return group


async def _agent(sessions, **filters) -> Agent | None:
    async with sessions() as session:
        return (await session.execute(select(Agent).filter_by(**filters))).scalar_one_or_none()


@pytest.fixture
def _any_model(monkeypatch):
    """The model catalog is not under test: every model id is selectable."""

    async def _selectable(*_args, **_kwargs):
        return None

    monkeypatch.setattr(builtin_tools_module, "_validate_selectable_chat_model_for_builder", _selectable)


# ---------------------------------------------------------------------------
# create_agent: one call, the complete agent
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("_any_model")
async def test_one_create_agent_call_builds_the_complete_emt_agent() -> None:
    async with _database() as sessions:
        async with sessions() as session:
            ceo = await _user(session, "ceo-assistant-owner")
            integrations = [
                await _integration(session, ceo, provider, name) for provider, name in EMT_PROVIDERS
            ]
            kb = KnowledgeBase(id=uuid.uuid4(), user_id=ceo.id, name="EMT notes")
            session.add(kb)
            await session.commit()
        linkedin_a, _linkedin_b, planhat_eu = integrations[0], integrations[1], integrations[2]
        bindings = ",".join(f"{i.provider}:{i.id}" for i in integrations)

        result = await _tool(
            ceo,
            "create_agent",
            name="[EMT] Executive Assistant",
            persona="You assist the executive management team.",
            integration_bindings=bindings,
            knowledge_base_ids=str(kb.id),
            llm_model="claude-opus-test",
            temperature="0.2",
            reasoning_effort="high",
            max_iterations=40,
            builtin_tool_ids="web_fetch,generate_file",
            memory_short_term_enabled="true",
            memory_medium_term_enabled="false",
            memory_persistent_enabled="true",
            memory_user_enabled="false",
            # A repeat is dropped, as on the REST routes (normalize_tool_deny_list).
            tool_deny_list="delete_linkedin_post, create_perk_webhook,delete_linkedin_post",
            integration_allowed_tools=json.dumps({"planhat": ["list_planhat_companies"]}),
            output_format="markdown",
            integration_read_only=f"linkedin:{linkedin_a.id},planhat:{planhat_eu.id}",
        )

        assert "Agent **[EMT] Executive Assistant** created successfully!" in result, result
        assert "  • Integrations: 17 bindings" in result
        for integration in integrations:
            assert f"    - {integration.provider}:{integration.id} ({integration.name})" in result
        assert f"linkedin:{linkedin_a.id} (LinkedIn Filigran) [read-only]" in result
        assert f"planhat:{planhat_eu.id} (Planhat EU) [read-only]" in result
        assert result.count("[read-only]") == 2
        assert "  • Configured: " in result

        agent = await _agent(sessions, name="[EMT] Executive Assistant")
        assert agent is not None
        assert sum(len(ids) for ids in agent.integration_bindings.values()) == 17
        assert len(agent.integration_bindings["linkedin"]) == 2
        assert len(agent.integration_bindings["planhat"]) == 2
        assert agent.llm_model == "claude-opus-test"
        assert agent.temperature == pytest.approx(0.2)
        assert agent.reasoning_effort == "high"
        assert agent.max_iterations == 40
        # Former ids are stored as their group: ``web_fetch`` alone is
        # the ``web`` group scoped to its functions, ``generate_file`` all of ``files``
        # but ``generate_image``.
        assert agent.builtin_tool_ids == ["web", "files"]
        assert agent.builtin_allowed_tools == {
            "web": list(alias_scoped_functions("web_fetch")),
            "files": list(alias_scoped_functions("generate_file")),
        }
        assert (
            agent.memory_short_term_enabled,
            agent.memory_medium_term_enabled,
            agent.memory_persistent_enabled,
            agent.memory_user_enabled,
        ) == (True, False, True, False)
        assert agent.tool_deny_list == ["delete_linkedin_post", "create_perk_webhook"]
        assert agent.integration_allowed_tools == {"planhat": ["list_planhat_companies"]}
        assert read_only_integration_ids(agent) == {str(linkedin_a.id), str(planhat_eu.id)}
        assert agent.available_channel_types == ["web"], "a new agent starts web-only, like REST"
        assert agent.knowledge_base_ids == [str(kb.id)]


async def test_a_refused_field_creates_nothing() -> None:
    async with _database() as sessions:
        async with sessions() as session:
            owner = await _user(session, "owner")
            planhat = await _integration(session, owner, "planhat", "Planhat EU")
            await session.commit()

        result = await _tool(owner, "create_agent", name="Half built", output_format="yaml")
        assert result.startswith("Agent not created: Invalid output_format")
        assert await _agent(sessions, name="Half built") is None

        unbound = str(uuid.uuid4())
        result = await _tool(
            owner,
            "create_agent",
            name="Half built",
            integration_bindings=f"planhat:{planhat.id}",
            integration_read_only=f"planhat:{unbound}",
        )
        assert result.startswith("Agent not created: integration_read_only names integrations")
        assert await _agent(sessions, name="Half built") is None


# ---------------------------------------------------------------------------
# spawn_background_task to the agent just created
# ---------------------------------------------------------------------------


async def test_a_background_task_to_the_agent_just_created_succeeds(monkeypatch) -> None:
    async with _database() as sessions:
        async with sessions() as session:
            owner = await _user(session, "owner")
            stranger = await _user(session, "stranger")
            session.add(Agent(id=uuid.uuid4(), user_id=stranger.id, name="Theirs", slug="theirs"))
            await session.commit()
        created_text = await _tool(owner, "create_agent", name="EMT Executive Assistant")
        assert "created successfully" in created_text
        created = await _agent(sessions, name="EMT Executive Assistant")
        assert created is not None

        spawned: list[dict] = []

        async def _count(**_kwargs):
            return 0

        async def _create(_db, **task):
            spawned.append(task)
            return SimpleNamespace(id=uuid.uuid4(), status="pending")

        monkeypatch.setattr(sub_agent, "count_active_sub_agent_executions", _count)
        monkeypatch.setattr(sub_agent, "create_sub_agent_task", _create)
        built = build_sub_agent_tools(
            None,
            str(owner.id),
            str(uuid.uuid4()),
            [],
            resolve_owned_target=owned_target_resolver(["agent_builder"], str(owner.id)),
        )
        assert built is not None
        _definitions, executor = built

        async def _spawn(agent_ref: str) -> str:
            result = await executor.execute_tool(
                ToolCall(id="1", name="spawn_background_task", arguments={"agent_id": agent_ref, "task": "Brief me."})
            )
            return result.content

        assert (await _spawn(str(created.id))).startswith("Background task spawned successfully.")
        assert (await _spawn(created.slug.upper())).startswith("Background task spawned successfully.")
        refused = await _spawn("theirs")
        assert "neither a delegate peer of this agent nor an agent you own" in refused
        assert [task["target_agent_id"] for task in spawned] == [str(created.id)] * 2

        found = await resolve_owned_delegation_target(created.slug, user_id=str(owner.id))
        assert found is not None and found.id == created.id
        assert await resolve_owned_delegation_target(created.slug, user_id=str(stranger.id)) is None


# ---------------------------------------------------------------------------
# clone_agent
# ---------------------------------------------------------------------------


async def test_clone_agent_copies_everything_but_assignments_memory_and_sharing() -> None:
    async with _database() as sessions:
        async with sessions() as session:
            owner = await _user(session, "owner")
            linkedin = await _integration(session, owner, "linkedin", "LinkedIn Filigran")
            planhat = await _integration(session, owner, "planhat", "Planhat EU")
            slack = await _integration(session, owner, "slack", "Slack")
            kb_notes = KnowledgeBase(id=uuid.uuid4(), user_id=owner.id, name="EMT notes")
            kb_board = KnowledgeBase(id=uuid.uuid4(), user_id=owner.id, name="Board packs")
            session.add_all([kb_notes, kb_board])
            group = await _group(session, "emt")
            source = Agent(
                id=uuid.uuid4(),
                user_id=owner.id,
                name="CEO Assistant",
                slug="ceo-assistant",
                persona="You assist the CEO.",
                integration_bindings={"linkedin": [str(linkedin.id)], "planhat": [str(planhat.id)]},
                integration_read_only={"linkedin": [str(linkedin.id)]},
                knowledge_base_ids=[str(kb_notes.id)],
                builtin_tool_ids=["web_fetch", "browser"],
                temperature=0.3,
                reasoning_effort="medium",
                max_iterations=30,
                memory_user_enabled=False,
                tool_deny_list=["delete_linkedin_post"],
                shared_group_ids=[str(group.id)],
                shared_group_permissions={str(group.id): "read_only"},
            )
            session.add(source)
            await session.flush()
            session.add(
                Assignment(
                    agent_id=source.id,
                    user_id=owner.id,
                    name="Daily brief",
                    trigger_type="manual",
                    trigger_config={},
                    prompt="Brief.",
                )
            )
            await session.commit()

        result = await _tool(
            owner,
            "clone_agent",
            source_agent_id=str(source.id),
            name="[EMT] Executive Assistant",
            persona_append="Serve the whole executive team.",
            add_integration_bindings=f"slack:{slack.id}",
            remove_integration_bindings=f"planhat:{planhat.id}",
            add_knowledge_base_ids=str(kb_board.id),
            add_builtin_tool_ids="agent_builder",
            remove_builtin_tool_ids="browser",
        )

        assert "Agent **[EMT] Executive Assistant** cloned from **CEO Assistant**." in result, result
        assert "  • name: 'CEO Assistant' -> '[EMT] Executive Assistant'" in result
        assert "  • persona: appended to" in result
        assert f"  • + integration slack:{slack.id} (Slack)" in result
        assert f"  • - integration planhat:{planhat.id} (Planhat EU)" in result
        assert f"  • + knowledge base {kb_board.id} (Board packs)" in result
        assert "  • + built-in tool agent_builder" in result
        assert "  • - built-in tool browser" in result
        assert f"linkedin:{linkedin.id} (LinkedIn Filigran) [read-only]" in result

        clone = await _agent(sessions, name="[EMT] Executive Assistant")
        assert clone is not None and clone.user_id == owner.id
        assert clone.persona == "You assist the CEO.\n\nServe the whole executive team."
        assert clone.integration_bindings == {"linkedin": [str(linkedin.id)], "slack": [str(slack.id)]}
        assert clone.integration_read_only == {"linkedin": [str(linkedin.id)]}
        assert clone.knowledge_base_ids == [str(kb_notes.id), str(kb_board.id)]
        assert clone.builtin_tool_ids == ["web_fetch", "agent_builder"]
        assert (clone.temperature, clone.reasoning_effort, clone.max_iterations) == (0.3, "medium", 30)
        assert clone.memory_user_enabled is False
        assert clone.tool_deny_list == ["delete_linkedin_post"]
        assert clone.shared_group_ids is None and clone.shared_group_permissions is None
        async with sessions() as session:
            copied = (await session.execute(select(Assignment).where(Assignment.agent_id == clone.id))).all()
        assert copied == []


async def test_a_clone_of_someone_elses_agent_leaves_out_what_its_cloner_cannot_use() -> None:
    async with _database() as sessions:
        async with sessions() as session:
            owner = await _user(session, "owner")
            member = await _user(session, "member")
            group = await _group(session, "emt", member)
            private = await _integration(session, owner, "planhat", "Owner's Planhat")
            source = Agent(
                id=uuid.uuid4(),
                user_id=owner.id,
                name="CEO Assistant",
                slug="ceo-assistant-shared",
                integration_bindings={"planhat": [str(private.id)]},
                shared_group_ids=[str(group.id)],
            )
            session.add(source)
            await session.commit()

        result = await _tool(member, "clone_agent", source_agent_id=str(source.id), name="My copy")

        assert "1 integration of the original agent was not copied: you cannot use it." in result
        clone = await _agent(sessions, name="My copy")
        assert clone is not None and not clone.integration_bindings


# ---------------------------------------------------------------------------
# Read-only bindings, end to end
# ---------------------------------------------------------------------------


async def test_a_read_only_binding_refuses_its_write_tool() -> None:
    async with _database() as sessions:
        async with sessions() as session:
            owner = await _user(session, "owner")
            perk = await _integration(session, owner, "perk", "Perk")
            await session.commit()
        bindings = {"perk": [str(perk.id)]}

        async with sessions() as session:
            writable, _ = await build_integration_tools(session, ToolBindings(bindings, basis=BindingBasis.CALLER_CHECKED, reason="test"), context={"source": "test"})
        assert "create_perk_webhook" in {d.name for d in writable}

        async with sessions() as session:
            definitions, executor = await build_integration_tools(
                session,
                ToolBindings(bindings, basis=BindingBasis.CALLER_CHECKED, reason="test"),
                context={"source": "test", READ_ONLY_CONTEXT_KEY: frozenset({str(perk.id)})},
            )
        names = {d.name for d in definitions}
        assert "list_perk_cost_centers" in names
        assert not names & {"create_perk_webhook", "delete_perk_webhook"}

        refused = await executor.execute_tool(
            ToolCall(
                id="1",
                name="create_perk_webhook",
                arguments={"name": "n", "url": "https://example.com", "secret": "s", "events": "trip.created"},
            )
        )
        assert refused.is_error
        assert refused.content.startswith(
            "Refused: create_perk_webhook writes to the perk integration 'Perk', which this agent may only read"
        )


async def test_a_read_only_binding_keeps_its_lookup_tools() -> None:
    """A ``lookup_*`` enrichment is a read: a read-only threat-intel
    binding keeps it, and still withholds and refuses the provider's write."""
    async with _database() as sessions:
        async with sessions() as session:
            owner = await _user(session, "analyst")
            greynoise = await _integration(session, owner, "greynoise", "GreyNoise")
            await session.commit()
        assert owner.role == "user"

        async with sessions() as session:
            definitions, executor = await build_integration_tools(
                session,
                ToolBindings({"greynoise": [str(greynoise.id)]}, basis=BindingBasis.CALLER_CHECKED, reason="test"),
                context={"source": "test", READ_ONLY_CONTEXT_KEY: frozenset({str(greynoise.id)})},
            )
        names = {d.name for d in definitions}
        assert {"lookup_greynoise_ip", "lookup_greynoise_ips", "lookup_greynoise_cve"} <= names
        assert "manage_greynoise_blocklist" not in names

        refused = await executor.execute_tool(
            ToolCall(id="1", name="manage_greynoise_blocklist", arguments={"action": "list"})
        )
        assert refused.is_error
        assert refused.content.startswith("Refused: manage_greynoise_blocklist writes to the greynoise integration")


async def test_a_catalog_install_keeps_the_read_only_marks_on_its_own_bindings() -> None:
    """The installer's choice for a provider takes that provider's mark."""
    from app.api.v1 import public_catalog

    async with _database() as sessions:
        async with sessions() as session:
            publisher = await _user(session, "publisher", admin=True)
            installer = await _user(session, "installer")
            shared_linkedin = await _integration(
                session, publisher, "linkedin", "LinkedIn Filigran", is_company_managed=True
            )
            shared_planhat = await _integration(
                session, publisher, "planhat", "Planhat EU", is_company_managed=True
            )
            own_planhat = await _integration(session, installer, "planhat", "My Planhat")
            published = Agent(
                id=uuid.uuid4(),
                user_id=publisher.id,
                name="EMT template",
                slug="emt-template",
                is_company_managed=True,
                is_public_catalog=True,
                integration_bindings={
                    "linkedin": [str(shared_linkedin.id)],
                    "planhat": [str(shared_planhat.id)],
                },
                integration_read_only={
                    "linkedin": [str(shared_linkedin.id)],
                    "planhat": [str(shared_planhat.id)],
                },
            )
            session.add(published)
            await session.commit()

        async with sessions() as session:
            caller = await session.get(User, installer.id)
            response = await public_catalog.add_catalog_item_to_account(
                entity_type="agents",
                entity_id=str(published.id),
                body=public_catalog.AgentSetupConfig(
                    integration_bindings={"planhat": [str(own_planhat.id)]}
                ),
                user=caller,
                db=session,
                _ai_gate=None,
            )

        clone = await _agent(sessions, id=uuid.UUID(response.id))
        assert clone.integration_bindings == {
            "linkedin": [str(shared_linkedin.id)],
            "planhat": [str(own_planhat.id)],
        }
        assert clone.integration_read_only == {
            "linkedin": [str(shared_linkedin.id)],
            "planhat": [str(own_planhat.id)],
        }


async def test_update_agent_marks_a_binding_read_only_and_get_agent_shows_it() -> None:
    async with _database() as sessions:
        async with sessions() as session:
            owner = await _user(session, "owner")
            linkedin = await _integration(session, owner, "linkedin", "LinkedIn Filigran")
            agent = Agent(
                id=uuid.uuid4(),
                user_id=owner.id,
                name="EMT",
                slug="emt-read-only",
                integration_bindings={"linkedin": [str(linkedin.id)]},
            )
            session.add(agent)
            await session.commit()

        result = await _tool(
            owner, "update_agent", agent_id=str(agent.id), integration_read_only=f"linkedin:{linkedin.id}"
        )
        assert "integration_read_only" in result
        shown = await _tool(owner, "get_agent", agent_id=str(agent.id))
        assert f"`linkedin:{linkedin.id}` (LinkedIn Filigran) [read-only]" in shown

        unbound = await _tool(
            owner, "update_agent", agent_id=str(agent.id), integration_read_only=f"planhat:{uuid.uuid4()}"
        )
        assert unbound.startswith("integration_read_only names integrations this agent does not bind")

        await _tool(owner, "update_agent", agent_id=str(agent.id), integration_bindings="clear")
        stored = await _agent(sessions, id=agent.id)
        assert stored.integration_read_only is None, "unbinding drops the read-only mark"


# ---------------------------------------------------------------------------
# Integrations shared with a group
# ---------------------------------------------------------------------------


async def test_an_integration_shared_with_a_group_is_used_by_the_groups_agents() -> None:
    async with _database() as sessions:
        async with sessions() as session:
            owner = await _user(session, "owner")
            member = await _user(session, "member")
            group = await _group(session, "emt", member)
            shared = await _integration(session, owner, "linkedin", "LinkedIn Filigran")
            private = await _integration(session, owner, "planhat", "Owner's Planhat")
            await session.commit()

        result = await _tool(
            owner,
            "update_integration",
            integration_id=str(shared.id),
            shared_group_ids=str(group.id),
            shared_group_permissions=json.dumps({str(group.id): "use"}),
        )
        assert result.startswith("Integration **LinkedIn Filigran** updated: sharing")
        shown = await _tool(owner, "get_integration", integration_id=str(shared.id))
        assert f"Shared with groups: {group.name} (`{group.id}`, use)" in shown

        managed = await _tool(
            owner,
            "update_integration",
            integration_id=str(shared.id),
            shared_group_permissions=f"{group.id}:manage",
        )
        assert "sharing" in managed
        assert f"(`{group.id}`, manage)" in await _tool(owner, "get_integration", integration_id=str(shared.id))
        unknown = await _tool(
            owner, "update_integration", integration_id=str(shared.id), shared_group_ids=str(uuid.uuid4())
        )
        assert unknown.startswith("Cannot share with unknown group(s):")

        # An agent shared with the group runs, for its member, only the
        # integrations that member may use.
        effective = {"linkedin": str(shared.id), "planhat": str(private.id)}
        origins = {key: "direct_agent_binding" for key in effective}
        async with sessions() as session:
            dropped = await shadow_invisible_bindings(
                session,
                effective,
                origins,
                acting_user_id=str(member.id),
                user_group_ids=[str(group.id)],
            )
        assert dropped == ["planhat"]
        assert effective == {"linkedin": str(shared.id)}

        cleared = await _tool(owner, "update_integration", integration_id=str(shared.id), shared_group_ids="clear")
        assert "sharing" in cleared
        async with sessions() as session:
            row = await session.get(Integration, shared.id)
        assert row.shared_group_ids is None and row.shared_group_permissions is None
