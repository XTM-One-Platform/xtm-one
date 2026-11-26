"""Who can use a resource a person lends to others, against PostgreSQL.

Every path is found by the service's real queries (the JSON / text pre-filters,
the KB join, the three-tier visibility of the viewer), and every preview
compares who can act with a resource before and after a proposed change. The
tables are created in a throwaway schema on one connection and dropped
afterwards.
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

import app.models  # noqa: F401 - every table registered
from app.api.v1 import resource_exposure as routes
from app.core.database import Base
from app.models.a2a import A2AExposure, A2AIntegrationSettings
from app.models.agent import Agent
from app.models.agent_skill import AgentSkill
from app.models.assignment import Assignment
from app.models.channel_bot import ChannelBot
from app.models.custom_tool import CustomTool
from app.models.flow import Flow
from app.models.general_assistant_config import GeneralAssistantConfig
from app.models.group import Group, GroupMember
from app.models.integration import Integration
from app.models.knowledge_base import KBSyncSource, KnowledgeBase
from app.models.mcp_server import MCPServer
from app.models.platform_settings import PlatformSettings
from app.models.prompt import Prompt
from app.models.user import User
from app.models.variable import Variable
from app.services import resource_exposure as ex
from tests.scratch_database import with_referenced_tables

pytestmark = pytest.mark.integration

ALICE, BOB, CAROL, DAVE, ADMIN = (uuid.uuid4() for _ in range(5))
TEAM = uuid.uuid4()  # Bob + Carol
OTHERS = uuid.uuid4()  # Dave

_TABLES = (
    "users",
    "groups",
    "group_members",
    "template_containers",
    "platform_settings",
    "general_assistant_config",
    "relay_groups",
    "integrations",
    # The per-user template candidates read the people's own accounts.
    "integration_user_credentials",
    "agents",
    # A knowledge base may belong to a workspace, whose visibility it then
    # follows.
    "workspaces",
    "knowledge_bases",
    "assignments",
    "flows",
    "kb_sync_sources",
    "agent_skills",
    "prompts",
    "mcp_servers",
    "custom_tools",
    "channel_bots",
    "variables",
    "a2a_exposures",
    "a2a_integration_settings",
)


def _database_url() -> str:
    return os.environ.get("DATABASE_URL", "postgresql+asyncpg://copilot:copilot@localhost:5432/copilot").strip()


@pytest.fixture
async def db():
    schema = f"exposure_{uuid.uuid4().hex[:10]}"
    engine = create_async_engine(_database_url())
    try:
        async with engine.connect() as probe:
            await probe.execute(text("SELECT 1"))
    except Exception as exc:
        await engine.dispose()
        pytest.skip(f"Cannot connect to PostgreSQL: {exc}")
    tables = with_referenced_tables([Base.metadata.tables[name] for name in _TABLES])
    try:
        async with engine.connect() as conn:
            await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
            # The schema alone: with `public` on the path, create_all would
            # find a database's own tables there and write into them.
            await conn.execute(text(f'SET search_path TO "{schema}"'))
            await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=tables, checkfirst=False))
            session = AsyncSession(bind=conn)
            for uid, name, role in (
                (ALICE, "Alice", "user"),
                (BOB, "Bob", "user"),
                (CAROL, "Carol", "user"),
                (DAVE, "Dave", "user"),
                (ADMIN, "Admin", "admin"),
            ):
                session.add(
                    User(id=uid, email=f"{name.lower()}@example.com", full_name=name, hashed_password="x", role=role)
                )
            await session.flush()
            session.add_all([Group(id=TEAM, name="Team"), Group(id=OTHERS, name="Others")])
            await session.flush()
            session.add_all(
                [
                    GroupMember(group_id=TEAM, user_id=BOB),
                    GroupMember(group_id=TEAM, user_id=CAROL),
                    GroupMember(group_id=OTHERS, user_id=DAVE),
                ]
            )
            await session.flush()
            try:
                yield session
            finally:
                await session.close()
                await conn.rollback()
                await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
                await conn.commit()
    finally:
        await engine.dispose()


def _viewer(uid: uuid.UUID, role: str = "user") -> SimpleNamespace:
    return SimpleNamespace(id=uid, role=role, is_admin=role == "admin")


async def _add(db, row):
    db.add(row)
    await db.flush()
    return row


def _integration(**fields) -> Integration:
    base = dict(
        id=uuid.uuid4(),
        user_id=ALICE,
        provider="github",
        name="Alice GitHub",
        auth_type="api_key",
        api_key_enc="enc",
        status="connected",
    )
    base.update(fields)
    return Integration(**base)


def _agent(**fields) -> Agent:
    base = dict(id=uuid.uuid4(), user_id=ALICE, name="Helper", slug=f"helper-{uuid.uuid4().hex[:6]}")
    base.update(fields)
    return Agent(**base)


def _team(**extra):
    return dict(shared_group_ids=[str(TEAM)], **extra)


def _people(audience: dict) -> set[str]:
    return {p["name"] for p in audience["people"]}


# ---------------------------------------------------------------------------
# A resource's exposure
# ---------------------------------------------------------------------------


async def test_every_path_is_listed_with_its_reason_and_audience(db):
    integ = await _add(db, _integration(shared_user_ids=[str(DAVE)]))
    iid = str(integ.id)
    agent = await _add(db, _agent(integration_bindings={"github": [iid]}, web_enabled=True, **_team()))
    await _add(
        db,
        Assignment(
            agent_id=agent.id,
            user_id=ALICE,
            name="Nightly",
            trigger_type="webhook",
            prompt="p",
            trigger_config={},
            integration_bindings={"github": [iid]},
        ),
    )
    await _add(
        db,
        Flow(
            user_id=ALICE,
            name="Flow",
            graph={"nodes": [{"id": "n", "kind": "integration", "config": {"integration_id": iid}}]},
        ),
    )
    kb = await _add(db, KnowledgeBase(user_id=ALICE, name="Docs", is_company_managed=True))
    await _add(
        db,
        KBSyncSource(knowledge_base_id=kb.id, user_id=ALICE, integration_id=integ.id, source_type="github", config={}),
    )
    await _add(db, AgentSkill(user_id=BOB, name="Private skill", source_integration_id=integ.id))
    await _add(db, GeneralAssistantConfig(integration_bindings={"github": [iid]}))

    result = await ex.resource_exposure(db, "integration", integ, viewer_id=ALICE, viewer_is_admin=False)

    kinds = sorted(p["kind"] for p in result["paths"])
    assert kinds == sorted(["people", "agent", "assignment", "flow", "knowledge_base", "skill", "general_assistant"])
    by_kind = {p["kind"]: p for p in result["paths"]}
    agent_path = by_kind["agent"]
    assert agent_path["reasons"] == ["binding"]
    assert agent_path["audience"]["public_link"] is True
    assert agent_path["audience"]["webhook_callers"] is True  # its webhook assignment
    assert {g["name"] for g in agent_path["audience"]["groups"]} == {"Team"}
    assert "chat_uses_own_integration" in agent_path["notes"]
    assert by_kind["assignment"]["parent"]["name"] == "Helper"
    assert by_kind["knowledge_base"]["audience"]["everyone"] is True
    # Bob's skill: Alice cannot open it, so its name is withheld but its owner is said.
    skill = by_kind["skill"]["entity"]
    assert skill["visible"] is False and skill["name"] is None and skill["owner_name"] == "Bob"
    assert result["reached"]["everyone"] is True
    assert result["secret"]["kind"] == "api_key" and result["secret"]["holder_name"] == "Alice"


async def test_the_integration_pages_own_answer_lists_the_same_paths(db):
    """``integration_exposure`` serves the integration pages; this
    engine serves every other resource and the previews. Both read the same
    carriers, so they must list the same paths with the same usability."""
    from app.services.integration_exposure import integration_exposure

    integ = await _add(db, _integration(auth_type="oauth", api_key_enc=None, access_token_enc="t", **_team()))
    iid = str(integ.id)
    agent = await _add(db, _agent(integration_bindings={"github": [iid]}, web_enabled=True))
    await _add(db, _agent(user_id=BOB, integration_bindings={"github": [iid]}))
    await _add(
        db,
        Assignment(
            agent_id=agent.id,
            user_id=ALICE,
            name="A",
            trigger_type="manual",
            prompt="p",
            trigger_config={"integration_id": iid},
        ),
    )
    kb = await _add(db, KnowledgeBase(user_id=ALICE, name="Docs"))
    await _add(
        db,
        KBSyncSource(knowledge_base_id=kb.id, user_id=ALICE, integration_id=integ.id, source_type="github", config={}),
    )
    await _add(db, GeneralAssistantConfig(integration_bindings={"github": [iid]}))

    theirs = await integration_exposure(db, integ, viewer_id=ALICE, viewer_is_admin=False)
    ours = await ex.resource_exposure(db, "integration", integ, viewer_id=ALICE, viewer_is_admin=False)

    def paths(result, usable_key):
        return sorted(
            (p["kind"], tuple(sorted(p["reasons"])), p[usable_key], repr(sorted(p["audience"].items())))
            for p in result["paths"]
        )

    assert paths(theirs, "credential_usable") == paths(ours, "usable")
    assert theirs["reached"] == ours["reached"] and theirs["credential"]["auth_type"] == "oauth"


async def test_an_admin_reaching_it_by_navigation_sees_what_belongs_to_it(db):
    integ = await _add(db, _integration())
    await _add(db, AgentSkill(user_id=BOB, name="Bob skill", source_integration_id=integ.id))
    result = await ex.resource_exposure(db, "integration", integ, viewer_id=ADMIN, viewer_is_admin=True)
    assert result["paths"][0]["entity"]["name"] == "Bob skill"


async def test_an_oauth_account_on_any_entity_is_usable_by_its_audience(db):
    integ = await _add(
        db, _integration(auth_type="oauth", api_key_enc=None, access_token_enc="t", shared_user_ids=[str(BOB)])
    )
    # Bob's agent carries Alice's account (shared with Bob, so the binding is
    # not orphaned): his own runs use it as bound; Carol, who may trigger its
    # assignment, runs it with her own.
    agent = await _add(db, _agent(user_id=BOB, integration_bindings={"github": [str(integ.id)]}, **_team()))
    await _add(db, _assignment(agent, trigger_type="manual"))
    result = await ex.resource_exposure(db, "integration", integ, viewer_id=ALICE, viewer_is_admin=False)
    [path] = [p for p in result["paths"] if p["kind"] == "agent"]
    assert path["usable"] is True and "account_not_shared" not in path["notes"]
    assert path["orphaned"] is False
    assert result["reached"]["people_count"] == 2  # Alice, and Bob, who sees it


async def test_mcp_servers_custom_tools_bots_and_variables_have_their_paths(db):
    server = await _add(db, MCPServer(user_id=ALICE, name="Tickets", transport="sse", headers_enc="h"))
    tool = await _add(db, CustomTool(user_id=ALICE, name="lookup"))
    var = await _add(db, Variable(user_id=ALICE, name="token", is_secret=True, value_enc="v"))
    bot = await _add(db, CustomTool(user_id=ALICE, name="unused"))
    agent = await _add(
        db,
        _agent(
            mcp_server_ids=[str(server.id)],
            custom_tool_ids=[str(tool.id)],
            persona=f"Use {{{{var_id.{var.id}}}}}",
            **_team(),
        ),
    )
    await _add(
        db,
        Assignment(
            agent_id=agent.id,
            user_id=ALICE,
            name="Scoped",
            trigger_type="manual",
            prompt="p",
            trigger_config={},
            tool_sources=[str(server.id)],
        ),
    )
    for kind, row, expected in (
        ("mcp_server", server, {"agent", "assignment"}),
        ("custom_tool", tool, {"agent"}),
        ("variable", var, {"agent"}),
        ("custom_tool", bot, set()),
    ):
        result = await ex.resource_exposure(db, kind, row, viewer_id=ALICE, viewer_is_admin=False)
        assert {p["kind"] for p in result["paths"]} == expected, kind
    var_result = await ex.resource_exposure(db, "variable", var, viewer_id=ALICE, viewer_is_admin=False)
    assert "variable_resolves_for_viewers_and_runs" in var_result["paths"][0]["notes"]


async def test_summaries_count_the_ways_a_resource_is_shared(db):
    integ = await _add(db, _integration(**_team(shared_user_ids=[str(DAVE)])))
    await _add(db, _agent(integration_bindings={"github": [str(integ.id)]}, web_enabled=True))
    await _add(db, _agent(integration_bindings={"github": [str(integ.id)]}))
    lonely = await _add(db, _integration(name="Unused"))
    summaries, truncated = await ex.exposure_summaries(db, "integration", [integ, lonely])
    assert not truncated
    assert summaries[str(integ.id)]["counts"] == {"group": 1, "people": 1, "agent": 2}
    assert summaries[str(integ.id)]["ways"] == 4 and summaries[str(integ.id)]["public_link"] is True
    assert summaries[str(lonely.id)]["ways"] == 0


# ---------------------------------------------------------------------------
# Previews
# ---------------------------------------------------------------------------


async def _preview(db, entity_type, entity, change, *, actor=ALICE, admin=False, parent=None):
    return await ex.preview_change(
        db, actor_id=actor, actor_is_admin=admin, entity_type=entity_type, entity=entity, change=change, parent=parent
    )


async def test_an_admin_s_preview_never_describes_a_colleague_s_private_variable(db):
    """A variable has no admin bypass on its own page, so an admin's preview of a
    change that references one they cannot open does not name it."""
    var = await _add(db, Variable(user_id=BOB, name="bob secret", is_secret=True, value_enc="v"))
    agent = await _add(db, _agent(user_id=ADMIN))
    result = await _preview(
        db,
        "agent",
        agent,
        {"persona": f"Use {{{{var_id.{var.id}}}}}", "shared_group_ids": [str(TEAM)]},
        actor=ADMIN,
        admin=True,
    )
    assert [i for i in result["items"] if i["resource"]["kind"] == "variable"] == []


async def test_an_admin_s_preview_still_describes_a_colleague_s_mcp_server(db):
    server = await _add(db, MCPServer(user_id=BOB, name="Bob tickets", transport="sse", headers_enc="h"))
    agent = await _add(db, _agent(user_id=ADMIN))
    result = await _preview(
        db,
        "agent",
        agent,
        {"mcp_server_ids": [str(server.id)], "shared_group_ids": [str(TEAM)]},
        actor=ADMIN,
        admin=True,
    )
    assert [i["resource"]["name"] for i in result["items"]] == ["Bob tickets"]


async def test_sharing_an_agent_names_whose_resources_the_new_people_get(db):
    server = await _add(db, MCPServer(user_id=ALICE, name="Tickets", transport="sse", headers_enc="h"))
    integ = await _add(db, _integration())
    agent = await _add(db, _agent(mcp_server_ids=[str(server.id)], integration_bindings={"github": [str(integ.id)]}))
    # The team may configure it: an assignment they add runs with their own
    # GitHub, never Alice's.
    change = {"shared_group_ids": [str(TEAM)], "shared_group_permissions": {str(TEAM): "config"}}
    result = await _preview(db, "agent", agent, change)
    assert result["requires_confirmation"]
    items = {i["resource"]["kind"]: i for i in result["items"]}
    assert set(items) == {"mcp_server"}
    assert _people(items["mcp_server"]["gained"]) == set() and items["mcp_server"]["gained"]["people_count"] == 2
    assert items["mcp_server"]["via"] == {"type": "agent", "id": str(agent.id), "name": "Helper"}
    # Its public link acts as Alice: it lends her GitHub to the link holders.
    linked = (await _preview(db, "agent", agent, {**change, "web_enabled": True}))["items"]
    [item] = [i for i in linked if i["resource"]["kind"] == "integration"]
    assert item["gained"]["public_link"] is True
    assert item["secret"]["is_yours"] is True
    assert "company_resource" in item["opt_outs"]


async def test_sharing_an_agent_to_chat_with_does_not_lend_its_bound_integration(db):
    """A chat turn of someone who cannot see a bound integration drops it for
    their own: a read-only share with nothing they can trigger lends
    the agent's MCP server, not the account."""
    server = await _add(db, MCPServer(user_id=ALICE, name="Tickets", transport="sse", headers_enc="h"))
    integ = await _add(db, _integration())
    agent = await _add(db, _agent(mcp_server_ids=[str(server.id)], integration_bindings={"github": [str(integ.id)]}))
    items = (await _preview(db, "agent", agent, {"shared_group_ids": [str(TEAM)]}))["items"]
    assert [i["resource"]["kind"] for i in items] == ["mcp_server"]


@pytest.mark.parametrize(
    ("kind", "field", "make"),
    [
        (
            "mcp_server",
            "mcp_server_ids",
            lambda: MCPServer(user_id=BOB, name="Bob secret server", transport="sse", headers_enc="h"),
        ),
        ("custom_tool", "custom_tool_ids", lambda: CustomTool(user_id=BOB, name="Bob secret tool")),
        # Through its public link, as the agent's owner.
        ("integration", "integration_bindings", lambda: _integration(user_id=BOB, name="Bob secret key")),
    ],
)
async def test_the_preview_never_describes_a_resource_its_caller_cannot_see(db, kind, field, make):
    """An id the change ADDS is described only when its writer could set it:
    a crafted id never reads back a colleague's private resource."""
    hidden = await _add(db, make())
    # An agent's binding reaches someone who cannot see it through the
    # callers no account names, acting as its owner, who must see it.
    owner = BOB if field == "integration_bindings" else ALICE
    agent = await _add(db, _agent(user_id=owner, web_enabled=True, **_team()))
    value = {"x": [str(hidden.id)]} if field == "integration_bindings" else [str(hidden.id)]
    result = await _preview(db, "agent", agent, {field: value})
    assert result["items"] == []
    assert "Bob secret" not in repr(result)
    # An admin may set any resource, so an admin is told.
    admin_result = await _preview(db, "agent", agent, {field: value}, actor=ADMIN, admin=True)
    assert [i["resource"]["id"] for i in admin_result["items"]] == [str(hidden.id)]


async def test_a_co_editor_is_still_told_whose_resource_the_entity_already_holds(db):
    tool = await _add(db, CustomTool(user_id=ALICE, name="Alice tool"))
    agent = await _add(
        db,
        _agent(
            custom_tool_ids=[str(tool.id)], shared_user_ids=[str(BOB)], shared_user_permissions={str(BOB): "config"}
        ),
    )
    [item] = (await _preview(db, "agent", agent, {"shared_group_ids": [str(OTHERS)]}, actor=BOB))["items"]
    assert item["resource"]["name"] == "Alice tool" and item["secret"]["holder_name"] == "Alice"


async def test_people_who_can_already_use_it_are_not_announced_again(db):
    server = await _add(db, MCPServer(user_id=ALICE, name="Tickets", transport="sse", headers_enc="h", **_team()))
    agent = await _add(db, _agent(mcp_server_ids=[str(server.id)]))
    result = await _preview(db, "agent", agent, {"shared_group_ids": [str(TEAM)]})
    assert result["items"] == [] and not result["requires_confirmation"]


async def test_binding_a_private_resource_to_a_shared_agent_is_announced(db):
    tool = await _add(db, CustomTool(user_id=ALICE, name="lookup"))
    agent = await _add(db, _agent(**_team()))
    result = await _preview(db, "agent", agent, {"custom_tool_ids": [str(tool.id)]})
    [item] = result["items"]
    assert item["resource"]["name"] == "lookup" and item["gained"]["people_count"] == 2
    assert item["lost"]["people_count"] == 0


async def test_referencing_a_secret_variable_from_a_shared_assignment_is_announced(db):
    var = await _add(db, Variable(user_id=ALICE, name="token", is_secret=True, value_enc="v"))
    agent = await _add(db, _agent(**_team()))
    result = await _preview(db, "assignment", None, {"prompt": f"Call with {{{{var_id.{var.id}}}}}"}, parent=agent)
    [item] = result["items"]
    assert item["resource"]["kind"] == "variable" and item["secret"]["kind"] == "secret_value"
    assert item["via"]["type"] == "assignment" and item["via"]["id"] is None


async def test_enabling_the_public_widget_announces_the_public_link(db):
    integ = await _add(db, _integration())
    agent = await _add(db, _agent(integration_bindings={"github": [str(integ.id)]}))
    [item] = (await _preview(db, "agent", agent, {"web_enabled": True}))["items"]
    assert item["gained"]["public_link"] is True and item["gained"]["people_count"] == 0


async def test_unsharing_an_agent_says_who_loses_what_it_lent(db):
    server = await _add(db, MCPServer(user_id=ALICE, name="Tickets", transport="sse", headers_enc="h"))
    agent = await _add(db, _agent(mcp_server_ids=[str(server.id)], **_team()))
    [item] = (await _preview(db, "agent", agent, {"shared_group_ids": None}))["items"]
    assert item["lost"]["people_count"] == 2 and item["gained"]["people_count"] == 0


async def test_deleting_a_resource_used_by_a_shared_agent_says_who_loses_it(db):
    server = await _add(db, MCPServer(user_id=ALICE, name="Tickets", transport="sse", headers_enc="h"))
    await _add(db, _agent(mcp_server_ids=[str(server.id)], **_team()))
    [item] = (await _preview(db, "mcp_server", server, {"delete": True}))["items"]
    assert item["lost"]["people_count"] == 2 and item["via"] is None


async def test_deleting_an_agent_takes_back_what_its_assignments_lent(db):
    server = await _add(db, MCPServer(user_id=ALICE, name="Tickets", transport="sse", headers_enc="h"))
    agent = await _add(db, _agent(**_team()))
    await _add(
        db,
        Assignment(
            agent_id=agent.id,
            user_id=ALICE,
            name="A",
            trigger_type="manual",
            prompt="p",
            trigger_config={},
            tool_sources=[str(server.id)],
        ),
    )
    [item] = (await _preview(db, "agent", agent, {"delete": True}))["items"]
    assert item["resource"]["name"] == "Tickets" and item["lost"]["people_count"] == 2


async def _preview_statements(db, entity_type, entity, change) -> int:
    """How many statements one preview sends to the database."""
    statements: list[str] = []
    engine = db.bind.sync_connection.engine

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        await _preview(db, entity_type, entity, change)
    finally:
        event.remove(engine, "before_cursor_execute", record)
    return len(statements)


async def test_a_preview_s_queries_do_not_grow_with_the_agents_sharing_a_resource(db):
    """Deleting an agent reads the audience of every agent binding what it
    binds - for a company integration, the whole fleet. Their groups, A2A
    exposures, webhook assignments and bots are read in one query each, never
    in a round trip per agent."""
    integ = await _add(db, _integration(user_id=None, is_company_managed=True))
    binding = {"github": [str(integ.id)]}
    agent = await _add(db, _agent(integration_bindings=binding, **_team()))

    async def grow_fleet_by(size: int) -> int:
        for _ in range(size):
            await _add(db, _agent(user_id=BOB, integration_bindings=binding, shared_group_ids=[str(uuid.uuid4())]))
        return await _preview_statements(db, "agent", agent, {"delete": True})

    with_three = await grow_fleet_by(3)
    with_thirty_three = await grow_fleet_by(30)
    assert with_thirty_three == with_three


async def test_primed_agents_keep_their_a2a_partners_and_webhook_callers(db):
    server = await _add(db, MCPServer(user_id=ALICE, name="Tickets", transport="sse", headers_enc="h"))
    ids = [str(server.id)]
    exposed = await _add(db, _agent(name="Exposed", mcp_server_ids=ids))
    await _add(db, A2AExposure(agent_id=exposed.id, card_token=uuid.uuid4().hex, run_as_user_id=ALICE))
    revoked = await _add(db, _agent(name="Revoked", mcp_server_ids=ids))
    await _add(
        db,
        A2AExposure(
            agent_id=revoked.id, card_token=uuid.uuid4().hex, run_as_user_id=ALICE, revoked_at=datetime.now(UTC)
        ),
    )
    hooked = await _add(db, _agent(name="Hooked", mcp_server_ids=ids))
    await _add(
        db,
        Assignment(
            agent_id=hooked.id, user_id=ALICE, name="Hook", trigger_type="webhook", prompt="p", trigger_config={}
        ),
    )
    result = await ex.resource_exposure(db, "mcp_server", server, viewer_id=ALICE, viewer_is_admin=False)
    audiences = {p["entity"]["name"]: p["audience"] for p in result["paths"] if p["kind"] == "agent"}
    assert {name: (a["a2a_partners"], a["webhook_callers"]) for name, a in audiences.items()} == {
        "Exposed": (True, False),
        "Revoked": (False, False),
        "Hooked": (False, True),
    }
    [item] = (await _preview(db, "mcp_server", server, {"delete": True}))["items"]
    assert item["lost"]["a2a_partners"] is True and item["lost"]["webhook_callers"] is True


async def test_a_token_no_entitled_person_placed_is_never_announced_as_lent(db):
    integ = await _add(db, _integration(user_id=BOB, auth_type="oauth", api_key_enc=None, access_token_enc="t"))
    agent = await _add(db, _agent(**_team()))
    # Alice binds Bob's unshared OAuth account: Bob cannot configure her agent.
    result = await _preview(db, "agent", agent, {"integration_bindings": {"github": [str(integ.id)]}})
    assert result["items"] == []


async def test_withholding_the_connected_account_says_who_loses_it(db):
    integ = await _add(
        db,
        _integration(
            auth_type="oauth", api_key_enc=None, access_token_enc="t", oauth_share_acknowledged=True, **_team()
        ),
    )
    [item] = (
        await _preview(
            db, "integration", integ, {"shared_group_ids": [str(TEAM)], "keep_oauth_credentials_on_share": False}
        )
    )["items"]
    # The team loses the account; the owner keeps it, theirs only.
    assert item["lost"]["people_count"] == 2
    assert item["secret"]["kind"] == "oauth_account" and item["secret"]["account_shared"] is False
    assert integ.access_token_enc == "t"  # nothing written


async def test_sharing_an_oauth_account_follows_the_rest_default_of_decision_15c(db):
    """Omitted, the share keeps the baseline meaning (the token is not shared);
    only an explicit ``true`` lends the account, as ``PUT /integrations`` does."""
    integ = await _add(db, _integration(auth_type="oauth", api_key_enc=None, access_token_enc="t"))
    omitted = await _preview(db, "integration", integ, {"shared_group_ids": [str(TEAM)]})
    assert omitted["items"] == []
    [item] = (
        await _preview(
            db, "integration", integ, {"shared_group_ids": [str(TEAM)], "keep_oauth_credentials_on_share": True}
        )
    )["items"]
    assert item["gained"]["people_count"] == 2 and item["secret"]["account_shared"] is True


async def test_making_a_resource_company_managed_reaches_everyone_but_those_it_reached(db):
    var = await _add(db, Variable(user_id=ALICE, name="token", is_secret=True, value_enc="v", **_team()))
    [item] = (await _preview(db, "variable", var, {"is_company_managed": True}, actor=ADMIN, admin=True))["items"]
    gained = item["gained"]
    assert gained["everyone"] is True
    assert gained["people_count"] == 1  # Dave: Alice and her team already had it, the admin acts


async def test_company_to_private_takes_it_from_other_owners_agents(db):
    tool = await _add(db, CustomTool(user_id=ALICE, name="lookup", is_company_managed=True))
    await _add(db, _agent(user_id=DAVE, custom_tool_ids=[str(tool.id)], shared_group_ids=[str(OTHERS)]))
    items = (await _preview(db, "custom_tool", tool, {"is_company_managed": False}, actor=ADMIN, admin=True))["items"]
    [item] = items
    assert item["lost"]["everyone"] is True  # everyone but Alice loses it; Dave's agent is cleaned too


async def test_company_to_private_takes_an_mcp_server_from_its_owners_company_managed_agent(db: AsyncSession) -> None:
    """#11866: a company-managed agent binds company-managed MCP servers only, so
    the owner's own company-managed agent loses the server it made private; the
    owner's personal agent keeps it."""
    from app.api.v1.helpers import toggle_company_managed_list_field

    srv = MCPServer(id=uuid.uuid4(), user_id=ALICE, name="tickets", transport="sse", is_company_managed=True)
    shared = _agent(user_id=ALICE, is_company_managed=True, mcp_server_ids=[str(srv.id)])
    own = _agent(user_id=ALICE, mcp_server_ids=[str(srv.id)])
    db.add_all([srv, shared, own])
    await db.flush()
    admin = await db.get(User, ADMIN)
    assert admin is not None

    await toggle_company_managed_list_field(db, admin, srv, False, agent_field="mcp_server_ids")

    assert not shared.mcp_server_ids
    assert own.mcp_server_ids == [str(srv.id)]


async def test_binding_to_the_general_assistant_reaches_everyone(db):
    tool = await _add(db, CustomTool(user_id=ADMIN, name="admin tool"))
    ga = await _add(db, GeneralAssistantConfig())
    [item] = (
        await _preview(db, "general_assistant", ga, {"custom_tool_ids": [str(tool.id)]}, actor=ADMIN, admin=True)
    )["items"]
    assert item["gained"]["everyone"] is True


async def test_creating_a_shared_resource_that_holds_a_secret_is_announced(db):
    [item] = (
        await _preview(db, "mcp_server", None, {"name": "New", "holds_secret": True, "shared_group_ids": [str(TEAM)]})
    )["items"]
    assert item["resource"]["name"] == "New" and item["gained"]["people_count"] == 2
    assert (await _preview(db, "mcp_server", None, {"name": "Private", "holds_secret": True}))["items"] == []


async def test_a_kb_sync_source_lends_its_integration_to_the_kb_readers(db):
    integ = await _add(db, _integration())
    kb = await _add(db, KnowledgeBase(user_id=ALICE, name="Docs", **_team()))
    [item] = (await _preview(db, "knowledge_base", kb, {"sync_source_integration_ids": [str(integ.id)]}))["items"]
    assert item["gained"]["people_count"] == 2 and item["via"]["type"] == "knowledge_base"


async def test_a_flow_node_lends_its_integration_to_the_flow_audience(db):
    integ = await _add(db, _integration())
    flow = await _add(db, Flow(user_id=ALICE, name="Flow", graph={"nodes": []}, **_team()))
    graph = {"nodes": [{"id": "n", "kind": "integration", "config": {"integration_id": str(integ.id)}}]}
    [item] = (await _preview(db, "flow", flow, {"graph": graph}))["items"]
    assert item["gained"]["people_count"] == 2


async def test_deleting_a_shared_flow_takes_back_what_its_nodes_lent(db):
    integ = await _add(db, _integration())
    graph = {"nodes": [{"id": "n", "kind": "integration", "config": {"integration_id": str(integ.id)}}]}
    flow = await _add(db, Flow(user_id=ALICE, name="Flow", graph=graph, **_team()))
    [item] = (await _preview(db, "flow", flow, {"delete": True}))["items"]
    assert item["lost"]["people_count"] == 2 and item["via"]["type"] == "flow"
    private = await _add(db, Flow(user_id=ALICE, name="Mine", graph=graph))
    assert (await _preview(db, "flow", private, {"delete": True}))["items"] == []


# ---------------------------------------------------------------------------
# Per-user bindings, A2A, flow activation, channel bots, groups, the MCP key
# ---------------------------------------------------------------------------


def _assignment(agent, **fields) -> Assignment:
    base = dict(
        agent_id=agent.id,
        user_id=agent.user_id,
        name="Nightly",
        trigger_type="schedule",
        prompt="p",
        trigger_config={},
        is_company_managed=agent.is_company_managed,
    )
    base.update(fields)
    return Assignment(**base)


async def test_an_assignment_resolving_a_per_user_binding_lends_its_owner_s_integration(db):
    integ = await _add(db, _integration())
    others = await _add(db, _integration(user_id=BOB, name="Bob GitHub"))
    agent = await _add(db, _agent(integration_template_bindings=["github"], **_team()))
    await _add(db, _assignment(agent))
    result = await ex.resource_exposure(db, "integration", integ, viewer_id=ALICE, viewer_is_admin=False)
    [path] = result["paths"]
    assert path["kind"] == "assignment" and path["reasons"] == ["per_user_template"]
    assert "per_user_resolves_to_acting_user" in path["notes"]
    assert path["audience"]["people_count"] == 3 and path["parent"]["name"] == "Helper"
    # The runs act as the agent's owner: a colleague's own integration is not theirs to pick.
    assert (await ex.resource_exposure(db, "integration", others, viewer_id=BOB, viewer_is_admin=False))["paths"] == []
    # A fixed binding for the provider is used instead of the per-user one.
    agent.integration_bindings = {"github": [str(others.id)]}
    await db.flush()
    assert (await ex.resource_exposure(db, "integration", integ, viewer_id=ALICE, viewer_is_admin=False))["paths"] == []


async def test_switching_to_per_user_takes_nothing_back_from_the_agent_s_audience(db):
    """Neither the agent's chats nor the runs of an assignment its audience
    adds used the owner's integration for anyone else: switching it to per
    user takes nothing back."""
    per_user = {"integration_bindings": {}, "integration_template_bindings": ["github"]}
    for name, extra, assignment in (
        ("Configured", {"shared_group_permissions": {str(TEAM): "config"}}, False),
        ("Configured with runs", {"shared_group_permissions": {str(TEAM): "config"}}, True),
        ("Chat only", {}, False),
    ):
        integ = await _add(db, _integration(name=name))
        agent = await _add(db, _agent(integration_bindings={"github": [str(integ.id)]}, **_team(**extra)))
        if assignment:
            await _add(db, _assignment(agent, user_id=BOB))
        assert (await _preview(db, "agent", agent, per_user))["items"] == [], name


async def test_switching_to_per_user_announces_what_the_owner_s_assignment_resolves(db):
    """Alice's own assignment now resolves her GitHub per user, as the person
    it runs as: the team that sees its work is told."""
    per_user = {"integration_bindings": {}, "integration_template_bindings": ["github"]}
    integ = await _add(db, _integration(name="With runs"))
    agent = await _add(db, _agent(integration_bindings={"github": [str(integ.id)]}, **_team()))
    await _add(db, _assignment(agent))
    [item] = (await _preview(db, "agent", agent, per_user))["items"]
    assert item["resource"]["name"] == "With runs" and item["gained"]["people_count"] == 2


async def test_choosing_whom_a_company_assignment_runs_as_lends_their_integration(db):
    await _add(db, _integration(user_id=BOB, name="Bob GitHub"))
    agent = await _add(db, _agent(user_id=ADMIN, is_company_managed=True, integration_template_bindings=["github"]))
    assignment = await _add(db, _assignment(agent))
    [item] = (
        await _preview(
            db, "assignment", assignment, {"run_as_user_id": str(BOB)}, actor=ADMIN, admin=True, parent=agent
        )
    )["items"]
    assert item["resource"]["name"] == "Bob GitHub" and item["secret"]["holder_name"] == "Bob"
    assert item["gained"]["everyone"] is True
    # A per-user fan-out acts as each user with their own.
    fan_out = {"run_as_user_id": str(BOB), "run_as_all_users": True}
    assert (await _preview(db, "assignment", assignment, fan_out, actor=ADMIN, admin=True, parent=agent))["items"] == []


def _company_github(name: str, grantee: uuid.UUID, created_at: datetime) -> Integration:
    return _integration(
        user_id=ADMIN,
        name=name,
        is_company_managed=True,
        access_restricted=True,
        shared_user_ids=[str(grantee)],
        created_at=created_at,
    )


async def test_a_per_user_binding_falls_back_to_the_company_integration_its_runs_may_use(db):
    """With no connected integration of their own, the runs use the first
    company-wide one the person they act as may use, as the assignment engine
    picks it."""
    now = datetime.now(UTC)
    hidden = await _add(db, _company_github("Bob's company GitHub", BOB, now - timedelta(hours=2)))
    company = await _add(db, _company_github("Company GitHub", ALICE, now - timedelta(hours=1)))
    agent = await _add(db, _agent(integration_template_bindings=["github"], web_enabled=True))
    await _add(db, _assignment(agent))

    async def assignment_paths(integration):
        result = await ex.resource_exposure(db, "integration", integration, viewer_id=ADMIN, viewer_is_admin=True)
        return [p for p in result["paths"] if p["kind"] == "assignment"]

    [path] = await assignment_paths(company)
    assert path["reasons"] == ["per_user_template"] and path["beyond_direct"]["public_link"] is True
    # Restricted to someone else: never the runs' to pick.
    assert await assignment_paths(hidden) == []
    # Once the person has one of their own connected, the runs use it instead.
    await _add(db, _integration(name="Alice GitHub"))
    assert await assignment_paths(company) == []


async def test_a_cut_lookup_of_own_integrations_never_claims_the_fallback(db, monkeypatch):
    """Past the bound, a person whose connected integration was not read is not
    taken for someone with none: the answer says it is incomplete instead."""
    now = datetime.now(UTC)
    company = await _add(db, _company_github("Company GitHub", ALICE, now - timedelta(hours=3)))
    company.shared_user_ids = [str(ALICE), str(BOB)]
    await _add(db, _integration(user_id=BOB, name="Bob GitHub", created_at=now - timedelta(hours=2)))
    await _add(db, _integration(name="Alice GitHub", created_at=now - timedelta(hours=1)))
    pairs = []
    for owner in (ALICE, BOB):
        agent = await _add(db, _agent(user_id=owner, integration_template_bindings=["github"]))
        pairs.append((await _add(db, _assignment(agent)), agent))
    monkeypatch.setattr(ex, "MAX_ENTITIES_PER_KIND", 1)
    picks = await ex.per_user_integrations(db, pairs)
    assert picks.truncated is True
    assert str(company.id) not in {i for ids in picks.values() for i in ids}


async def test_switching_to_per_user_announces_the_company_integration_the_runs_fall_back_to(db):
    await _add(db, _company_github("Company GitHub", ALICE, datetime.now(UTC)))
    agent = await _add(db, _agent(**_team()))
    await _add(db, _assignment(agent))
    [item] = (await _preview(db, "agent", agent, {"integration_template_bindings": ["github"]}))["items"]
    assert item["resource"]["name"] == "Company GitHub" and item["gained"]["people_count"] == 2


async def test_a_cut_preview_still_asks(db, monkeypatch):
    """What lies past a lookup's bound is not listed: the change asks anyway."""
    monkeypatch.setattr(ex, "MAX_ENTITIES_PER_KIND", 1)
    for name in ("First", "Second"):
        integ = await _add(db, _integration(name=name))
        await _add(db, _agent(integration_bindings={"github": [str(integ.id)]}, **_team()))
    team = await db.get(Group, TEAM)
    result = await _preview(db, "group", team, {"add_user_ids": [str(DAVE)]}, actor=ADMIN, admin=True)
    assert result["truncated"] is True and result["requires_confirmation"] is True


async def test_a_cut_preview_of_the_resource_itself_still_asks(db, monkeypatch):
    """Sharing a resource changes what every carrier holding it lends: one the
    lookup did not reach could hide someone, so the change asks."""
    integ = await _add(db, _integration())
    for owner in (BOB, CAROL):
        await _add(db, _agent(user_id=owner, integration_bindings={"github": [str(integ.id)]}))
    monkeypatch.setattr(ex, "MAX_ENTITIES_PER_KIND", 1)
    result = await _preview(db, "integration", integ, {"shared_group_ids": [str(TEAM)]})
    assert result["truncated"] is True and result["requires_confirmation"] is True


async def test_editing_an_unshared_agent_s_integrations_never_asks_for_a_cut_lookup(db, monkeypatch):
    """The other agents holding an integration reach the same people before and
    after an edit to one agent: a lookup cut before reaching them hides nobody,
    so an agent nobody else uses saves without an empty confirmation."""
    company = await _add(db, _integration(user_id=None, is_company_managed=True, name="Company GitHub"))
    binding = {"github": [str(company.id)]}
    for owner in (BOB, CAROL):
        await _add(db, _agent(user_id=owner, integration_bindings=binding))
    bound = await _add(db, _agent(integration_bindings=binding))
    unbound = await _add(db, _agent())
    monkeypatch.setattr(ex, "MAX_ENTITIES_PER_KIND", 1)
    for agent, change in ((unbound, {"integration_bindings": binding}), (bound, {"integration_bindings": {}})):
        result = await _preview(db, "agent", agent, change)
        assert result == {"items": [], "requires_confirmation": False, "truncated": False}


async def test_a_cut_lookup_of_the_edited_agent_s_assignments_still_asks(db, monkeypatch):
    """The edited agent's assignments are recomputed by the preview: one past the
    bound is neither counted nor compared, so the change asks."""
    agent = await _add(db, _agent(**_team()))
    await _add(db, _assignment(agent))
    monkeypatch.setattr(ex, "MAX_ENTITIES_PER_KIND", 1)
    within = await _preview(db, "agent", agent, {"web_enabled": True})
    assert within["truncated"] is False and within["requires_confirmation"] is False
    await _add(db, _assignment(agent, name="Weekly"))
    past = await _preview(db, "agent", agent, {"web_enabled": True})
    assert past["truncated"] is True and past["requires_confirmation"] is True


async def test_a_cut_lookup_still_counts_the_carriers_the_change_recomputes(db, monkeypatch):
    """The edited agent and its assignments are counted as stored even when the
    lookup reached none of them: what they stop lending is still said."""
    bound = await _add(db, MCPServer(user_id=ALICE, name="Tickets", transport="sse", headers_enc="h"))
    overridden = await _add(db, MCPServer(user_id=ALICE, name="Pager", transport="sse", headers_enc="h"))
    agent = await _add(db, _agent(mcp_server_ids=[str(bound.id)], **_team()))
    await _add(db, _assignment(agent, tool_sources=[str(overridden.id)]))

    async def past_the_bound(db, kind, resource_ids):
        return ex.References(truncated=True)

    monkeypatch.setattr(ex, "find_references", past_the_bound)
    dropped = await _preview(db, "agent", agent, {"mcp_server_ids": []})
    assert [(i["resource"]["name"], i["lost"]["people_count"]) for i in dropped["items"]] == [("Tickets", 2)]
    assert dropped["truncated"] is False
    unshared = await _preview(db, "agent", agent, {"shared_group_ids": None})
    assert sorted((i["resource"]["name"], i["lost"]["people_count"]) for i in unshared["items"]) == [
        ("Pager", 2),
        ("Tickets", 2),
    ]
    assert unshared["truncated"] is False


async def test_binding_an_inbound_a2a_integration_lends_the_agent_s_bindings_to_its_partners(db):
    integ = await _add(db, _integration())
    inbound = await _add(db, _integration(provider="oauth_a2a", name="Partner in", auth_type="none", api_key_enc=None))
    outbound = await _add(
        db, _integration(provider="oauth_a2a", name="Partner out", auth_type="none", api_key_enc=None)
    )
    await _add(db, A2AIntegrationSettings(integration_id=inbound.id, direction="inbound"))
    await _add(db, A2AIntegrationSettings(integration_id=outbound.id, direction="outbound"))
    agent = await _add(db, _agent(integration_bindings={"github": [str(integ.id)]}))

    def bindings(partner):
        return {"integration_bindings": {"github": [str(integ.id)], "oauth_a2a": [str(partner.id)]}}

    [item] = (await _preview(db, "agent", agent, bindings(inbound)))["items"]
    assert item["resource"]["name"] == "Alice GitHub" and item["gained"]["a2a_partners"] is True
    assert (await _preview(db, "agent", agent, bindings(outbound)))["items"] == []


async def test_activating_a_flow_with_a_webhook_trigger_lends_its_nodes_to_the_url_holders(db):
    integ = await _add(db, _integration())
    node = {"id": "n", "kind": "integration", "config": {"integration_id": str(integ.id)}}
    hooked = await _add(
        db,
        Flow(user_id=ALICE, name="Hook", graph={"nodes": [{"id": "t", "kind": "trigger.webhook"}, node]}),
    )
    [item] = (await _preview(db, "flow", hooked, {"status": "active"}))["items"]
    assert item["gained"]["webhook_callers"] is True and item["via"]["name"] == "Hook"
    manual = await _add(
        db, Flow(user_id=ALICE, name="Manual", graph={"nodes": [{"id": "t", "kind": "trigger.manual"}, node]})
    )
    assert (await _preview(db, "flow", manual, {"status": "active"}))["items"] == []


async def test_letting_a_bot_answer_people_without_an_account_lends_what_its_agents_carry(db):
    integ = await _add(db, _integration())
    bot = await _add(db, ChannelBot(provider="slack", name="Support bot", is_company_managed=True, api_key_enc="b"))
    channel = {"slack": {"channel_bot_id": str(bot.id), "receive_enabled": True}}
    await _add(db, _agent(integration_bindings={"github": [str(integ.id)]}, channel_config=channel))
    items = (await _preview(db, "channel_bot", bot, {"allow_anonymous": True}, actor=ADMIN, admin=True))["items"]
    lent = next(i for i in items if i["resource"]["kind"] == "integration")
    assert lent["gained"]["anonymous_channels"] == ["slack"]
    assert lent["via"] == {"type": "channel_bot", "id": str(bot.id), "name": "Support bot"}


async def test_a_group_s_new_and_departing_members_are_told_what_its_shares_lend(db):
    await _add(db, _integration(**_team()))
    server = await _add(db, MCPServer(user_id=ALICE, name="Tickets", transport="sse", headers_enc="h"))
    await _add(db, _agent(mcp_server_ids=[str(server.id)], **_team()))
    team = await db.get(Group, TEAM)
    items = (await _preview(db, "group", team, {"add_user_ids": [str(DAVE)]}, actor=ADMIN, admin=True))["items"]
    assert {i["resource"]["name"] for i in items} == {"Alice GitHub", "Tickets"}
    assert all(_people(i["gained"]) == {"Dave"} and i["via"]["name"] == "Team" for i in items)
    items = (await _preview(db, "group", team, {"remove_user_ids": [str(BOB)]}, actor=ADMIN, admin=True))["items"]
    assert all(_people(i["lost"]) == {"Bob"} for i in items) and len(items) == 2


async def test_unsharing_a_group_names_the_agents_its_members_would_orphan(db):
    """Bob's agent binds Alice's integration through the Team share: taking
    the share back (or Bob out of Team) orphans it, and the preview says so."""
    integ = await _add(db, _integration(**_team()))
    await _add(db, _agent(user_id=BOB, name="Bob's helper", integration_bindings={"github": [str(integ.id)]}))
    await _add(db, _agent(user_id=ALICE, name="Alice's helper", integration_bindings={"github": [str(integ.id)]}))
    items = (await _preview(db, "integration", integ, {"shared_group_ids": []}))["items"]
    (item,) = [i for i in items if i["resource"]["id"] == str(integ.id)]
    assert [(a["owner_name"], a["visible"]) for a in item["orphaned_agents"]] == [("Bob", False)]
    team = await db.get(Group, TEAM)
    items = (await _preview(db, "group", team, {"remove_user_ids": [str(BOB)]}, actor=ADMIN, admin=True))["items"]
    (item,) = [i for i in items if i["resource"]["id"] == str(integ.id)]
    assert [(a["name"], a["owner_name"]) for a in item["orphaned_agents"]] == [("Bob's helper", "Bob")]
    # Carol leaving orphans nothing: she binds nothing.
    items = (await _preview(db, "group", team, {"remove_user_ids": [str(CAROL)]}, actor=ADMIN, admin=True))["items"]
    assert all(not i["orphaned_agents"] for i in items)


@pytest.mark.parametrize(
    ("tier", "announced"),
    [
        # Dave's chats would use his own GitHub...
        pytest.param("read_only", {"Tickets"}, id="chat_only"),
        # ...and so would the runs of an assignment he adds.
        pytest.param("config", {"Tickets"}, id="configures"),
    ],
)
async def test_a_new_member_is_told_of_an_agent_s_tools_never_of_its_bound_integration(
    db, tier, announced
):
    integ = await _add(db, _integration())
    server = await _add(db, MCPServer(user_id=ALICE, name="Tickets", transport="sse", headers_enc="h"))
    await _add(
        db,
        _agent(
            mcp_server_ids=[str(server.id)],
            integration_bindings={"github": [str(integ.id)]},
            **_team(shared_group_permissions={str(TEAM): tier}),
        ),
    )
    team = await db.get(Group, TEAM)
    items = (await _preview(db, "group", team, {"add_user_ids": [str(DAVE)]}, actor=ADMIN, admin=True))["items"]
    assert {i["resource"]["name"] for i in items} == announced
    assert all(_people(i["gained"]) == {"Dave"} for i in items)


@pytest.mark.parametrize(
    "assignment",
    [
        pytest.param(None, id="chat_only"),
        pytest.param({}, id="triggerable"),
        # (an explicit None is stored as a JSON null: it inherits them too)
        pytest.param({"integration_bindings": None}, id="explicitly_inherits"),
        pytest.param({"trigger_type": "schedule"}, id="scheduled"),
        pytest.param({"enabled": False}, id="disabled"),
        pytest.param({"integration_bindings": {}}, id="own_bindings"),
    ],
)
async def test_the_headline_counts_who_acts_with_the_account_not_the_agent_s_chat_audience(db, assignment):
    """Alice's GitHub, shared with Bob, is bound to her agent, shared
    with the team (Bob, Carol) and Dave, who cannot see it. Their chats and
    the runs of its assignments they start use their own, whatever the
    assignment."""
    integ = await _add(db, _integration(shared_user_ids=[str(BOB)]))
    agent = await _add(
        db, _agent(integration_bindings={"github": [str(integ.id)]}, shared_user_ids=[str(DAVE)], **_team())
    )
    if assignment is not None:
        await _add(db, _assignment(agent, **{"trigger_type": "manual", **assignment}))

    result = await ex.resource_exposure(db, "integration", integ, viewer_id=ALICE, viewer_is_admin=False)

    # Alice and Bob, shared directly.
    assert result["reached"]["people_count"] == 2
    [agent_path] = [p for p in result["paths"] if p["kind"] == "agent"]
    # Its line lends the account to its owner's own runs, nobody it chats with.
    assert _people(agent_path["lent"]) == {"Alice"}
    assert agent_path["lent"]["people_count"] == 1 and agent_path["lent"]["groups"] == []
    assert agent_path["audience"]["people_count"] == 4
    assert agent_path["notes"] == ["chat_uses_own_integration"]


async def test_an_agent_that_also_pins_its_binding_in_a_pipeline_step_lends_it_to_no_one_it_chats_with(db):
    """A pipeline pin is checked like a binding: the team's chats use their own."""
    integ = await _add(db, _integration())
    iid = str(integ.id)
    pin = {"source": "integration", "integration_id": iid}
    await _add(db, _agent(integration_bindings={"github": [iid]}, prepare_tool_ref=pin, **_team()))

    result = await ex.resource_exposure(db, "integration", integ, viewer_id=ALICE, viewer_is_admin=False)

    [agent_path] = [p for p in result["paths"] if p["kind"] == "agent"]
    assert sorted(agent_path["reasons"]) == ["binding", "pipeline"]
    assert _people(agent_path["lent"]) == {"Alice"} and agent_path["lent"]["groups"] == []
    assert agent_path["notes"] == ["chat_uses_own_integration"]


async def test_a_company_agent_never_lends_its_binding_to_everyone(db):
    integ = await _add(db, _integration())
    agent = await _add(db, _agent(is_company_managed=True, integration_bindings={"github": [str(integ.id)]}, **_team()))

    async def reached() -> dict:
        return (await ex.resource_exposure(db, "integration", integ, viewer_id=ALICE, viewer_is_admin=False))["reached"]

    assert (await reached())["everyone"] is False and _people(await reached()) == {"Alice"}
    # Not through an assignment, even one that runs per user as its caller's own report.
    assignment = await _add(db, _assignment(agent, trigger_type="manual"))
    assert (await reached())["everyone"] is False
    assignment.hide_runs = True
    await db.flush()
    assert (await reached())["everyone"] is False and _people(await reached()) == {"Alice"}


@pytest.mark.parametrize(
    "change",
    [
        pytest.param({"name": "Digest"}, id="inherits_the_bindings"),
        pytest.param({"name": "Digest", "integration_bindings": {}}, id="own_bindings"),
        pytest.param({"name": "Digest", "enabled": False}, id="disabled"),
        pytest.param({"name": "Digest", "integration_bindings": "own"}, id="binds_it_itself"),
    ],
)
async def test_adding_or_deleting_an_assignment_lends_nothing_its_runs_drop(db, change):
    integ = await _add(db, _integration())
    agent = await _add(db, _agent(integration_bindings={"github": [str(integ.id)]}, **_team()))
    if change.get("integration_bindings") == "own":
        change = {**change, "integration_bindings": {"github": [str(integ.id)]}}
    assert (await _preview(db, "assignment", None, change, parent=agent))["items"] == []
    stored = await _add(db, _assignment(agent, trigger_type="manual"))
    assert (await _preview(db, "assignment", stored, {"delete": True}, parent=agent))["items"] == []


async def test_an_assignment_binding_it_itself_lends_it_to_its_webhook_callers(db):
    integ = await _add(db, _integration())
    agent = await _add(db, _agent(**_team()))
    change = {"name": "Hook", "trigger_type": "webhook", "integration_bindings": {"github": [str(integ.id)]}}
    [item] = (await _preview(db, "assignment", None, change, parent=agent))["items"]
    assert item["resource"]["name"] == "Alice GitHub"
    assert item["gained"]["webhook_callers"] is True and item["gained"]["people_count"] == 0


async def test_exposing_an_agent_on_the_platform_mcp_key_lends_its_bindings_to_the_key_holders(db):
    integ = await _add(db, _integration())
    agent = await _add(db, _agent(integration_bindings={"github": [str(integ.id)]}))
    settings = await _add(db, PlatformSettings(mcp_exposed_agent_ids=[]))
    listed = {"mcp_exposed_agent_ids": [str(agent.id)]}
    # Without a key nobody holds one yet; generating it then lends the listed agents.
    assert (await _preview(db, "platform_mcp", settings, listed, actor=ADMIN, admin=True))["items"] == []
    settings.mcp_exposed_agent_ids = [str(agent.id)]
    await db.flush()
    [item] = (await _preview(db, "platform_mcp", settings, {"has_mcp_key": True}, actor=ADMIN, admin=True))["items"]
    assert item["gained"]["mcp_key_holders"] is True and item["via"]["type"] == "platform_mcp"


async def test_what_an_agent_attaches_carries_its_resources_to_the_agent_s_audience(db):
    integ = await _add(db, _integration())
    kb = await _add(db, KnowledgeBase(user_id=ALICE, name="Docs"))
    await _add(
        db,
        KBSyncSource(knowledge_base_id=kb.id, user_id=ALICE, integration_id=integ.id, source_type="github", config={}),
    )
    skill = await _add(db, AgentSkill(user_id=ALICE, name="Runbook", source_integration_id=integ.id))
    await _add(db, _agent(knowledge_base_ids=[str(kb.id)], skill_ids=[str(skill.id)], web_enabled=True))
    result = await ex.resource_exposure(db, "integration", integ, viewer_id=ALICE, viewer_is_admin=False)
    [agent_path] = [p for p in result["paths"] if p["kind"] == "agent"]
    assert agent_path["reasons"] == ["attached_knowledge_base", "attached_skill"]
    assert agent_path["audience"]["public_link"] is True


async def test_attaching_a_prompt_with_a_secret_variable_to_a_shared_agent_is_announced(db):
    var = await _add(db, Variable(user_id=ALICE, name="token", is_secret=True, value_enc="v"))
    prompt = await _add(db, Prompt(user_id=ALICE, name="Style", content=f"Use {{{{var_id.{var.id}}}}}"))
    agent = await _add(db, _agent(**_team()))
    [item] = (await _preview(db, "agent", agent, {"prompt_ids": [str(prompt.id)]}))["items"]
    assert item["resource"]["name"] == "token" and item["gained"]["people_count"] == 2


async def test_editing_an_attached_prompt_lends_its_new_variable_through_the_agents_using_it(db):
    var = await _add(db, Variable(user_id=ALICE, name="token", is_secret=True, value_enc="v"))
    prompt = await _add(db, Prompt(user_id=ALICE, name="Style", content="Be brief"))
    await _add(db, _agent(prompt_ids=[str(prompt.id)], **_team()))
    change = {"content": f"Use {{{{var_id.{var.id}}}}}"}
    [item] = (await _preview(db, "prompt", prompt, change))["items"]
    assert item["resource"]["name"] == "token" and item["gained"]["people_count"] == 2


async def test_a_sync_source_added_to_a_knowledge_base_the_assistant_searches_reaches_everyone(db):
    integ = await _add(db, _integration(user_id=ADMIN, name="Admin GitHub"))
    kb = await _add(db, KnowledgeBase(user_id=ADMIN, name="Handbook"))
    await _add(db, GeneralAssistantConfig(knowledge_base_ids=[str(kb.id)]))
    change = {"sync_source_integration_ids": [str(integ.id)]}
    [item] = (await _preview(db, "knowledge_base", kb, change, actor=ADMIN, admin=True))["items"]
    assert item["gained"]["everyone"] is True


async def test_a_flow_s_knowledge_base_node_carries_its_sync_source_to_the_flow_audience(db):
    integ = await _add(db, _integration())
    kb = await _add(db, KnowledgeBase(user_id=ALICE, name="Docs"))
    await _add(
        db,
        KBSyncSource(knowledge_base_id=kb.id, user_id=ALICE, integration_id=integ.id, source_type="github", config={}),
    )
    node = {"id": "k", "kind": "capability.knowledge_base", "config": {"knowledge_base_ids": [str(kb.id)]}}
    flow = await _add(db, Flow(user_id=ALICE, name="Digest", graph={"nodes": []}, **_team()))
    [item] = (await _preview(db, "flow", flow, {"graph": {"nodes": [node]}}))["items"]
    assert item["resource"]["name"] == "Alice GitHub" and item["gained"]["people_count"] == 2
    flow.graph = {"nodes": [node]}
    await db.flush()
    result = await ex.resource_exposure(db, "integration", integ, viewer_id=ALICE, viewer_is_admin=False)
    [flow_path] = [p for p in result["paths"] if p["kind"] == "flow"]
    assert flow_path["reasons"] == ["attached_knowledge_base"]


async def test_the_integration_route_s_preview_is_the_engine_s(db):
    from app.services.integration_exposure import preview_exposure

    integ = await _add(db, _integration())
    agent = await _add(db, _agent())
    preview = await preview_exposure(
        db,
        actor_id=ALICE,
        actor_is_admin=False,
        entity_type="agent",
        entity=agent,
        change={
            "integration_ids": [str(integ.id)],
            "shared_group_ids": [str(TEAM)],
            "web_enabled": True,
        },
    )
    # The team's chats use their own; its public link acts as Alice.
    [item] = preview["credentials"]
    assert item["integration"]["id"] == str(integ.id) and item["reached"]["public_link"] is True
    assert item["via"]["type"] == "agent" and "company_integration" in item["opt_outs"]
    # A flow's integration_ids are the integrations its nodes will hold.
    flow = await _add(db, Flow(user_id=ALICE, name="Flow", graph={"nodes": []}, **_team()))
    preview = await preview_exposure(
        db,
        actor_id=ALICE,
        actor_is_admin=False,
        entity_type="flow",
        entity=flow,
        change={"integration_ids": [str(integ.id)]},
    )
    assert [c["reached"]["people_count"] for c in preview["credentials"]] == [2]


# ---------------------------------------------------------------------------
# The Security Audit's reading: the personal credentials other people act with
# ---------------------------------------------------------------------------


def _oauth(**overrides) -> Integration:
    return _integration(auth_type="oauth", api_key_enc=None, access_token_enc="t", **overrides)


async def test_the_audit_lists_the_personal_credentials_other_people_act_with(db):
    from app.services.integration_exposure import lent_personal_credentials

    bound = await _add(db, _integration(name="Alice GitHub"))
    shared_key = await _add(db, _integration(name="Alice Jira", provider="jira", shared_user_ids=[str(BOB)]))
    await _add(db, _oauth(name="Alice Drive", provider="google", oauth_share_acknowledged=True, **_team()))
    await _add(db, _oauth(name="Handed over", user_id=BOB, oauth_token_owner_id=ALICE))
    await _add(db, _agent(integration_bindings={"github": [str(bound.id)]}, web_enabled=True, **_team()))
    await _add(db, GeneralAssistantConfig(integration_bindings={"jira": [str(shared_key.id)]}))

    lent, truncated = await lent_personal_credentials(db)

    by_name = {credential.name: credential for credential in lent}
    assert set(by_name) == {"Alice GitHub", "Alice Jira", "Alice Drive", "Handed over"}
    github = by_name["Alice GitHub"]
    # Its public link acts with it; the team chatting with it uses their own.
    assert (github.kinds, github.reached.user_ids, github.reached.public_link) == (["agent"], set(), True)
    assert by_name["Alice Jira"].kinds == ["people", "general_assistant"]
    assert by_name["Alice Jira"].reached.everyone is True
    assert (by_name["Alice Drive"].kinds, by_name["Alice Drive"].reached.user_ids) == (["group"], {BOB, CAROL})
    # The owner acts with the account the previous owner connected.
    assert (by_name["Handed over"].kinds, by_name["Handed over"].reached.user_ids) == (["owner"], {BOB})
    assert by_name["Handed over"].holder_name == "Alice" and github.holder_name == "Alice"
    assert truncated is False


async def test_the_audit_leaves_out_what_only_its_holder_uses_and_company_credentials(db):
    from app.services.integration_exposure import lent_personal_credentials

    private = await _add(db, _integration(name="Alice private"))
    # Shared, but the account itself was not: grantees cannot act with it.
    await _add(db, _oauth(name="Alice Gmail", provider="google", **_team()))
    company = await _add(db, _integration(name="Company GitHub", user_id=None, is_company_managed=True))
    await _add(db, _integration(name="No credential", api_key_enc=None, shared_user_ids=[str(BOB)]))
    await _add(db, _agent(integration_bindings={"github": [str(private.id)]}))
    await _add(db, _agent(user_id=None, is_company_managed=True, integration_bindings={"github": [str(company.id)]}))
    assert await lent_personal_credentials(db) == ([], False)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def _endpoint(name: str):
    return next(r.endpoint for r in routes.router.routes if getattr(r, "name", None) == name)


async def test_the_exposure_is_for_the_resource_s_managers(db):
    server = await _add(db, MCPServer(user_id=ALICE, name="Tickets", transport="sse", **_team()))
    get_exposure = _endpoint("mcp_server_exposure")
    assert (await get_exposure(str(server.id), user=_viewer(ALICE), db=db))["resource"]["name"] == "Tickets"
    with pytest.raises(HTTPException) as exc:
        await get_exposure(str(server.id), user=_viewer(BOB), db=db)  # read-only grantee
    assert exc.value.status_code == 403
    with pytest.raises(HTTPException) as exc:
        await get_exposure(str(server.id), user=_viewer(DAVE), db=db)
    assert exc.value.status_code == 404
    with pytest.raises(HTTPException) as exc:
        await get_exposure("not-a-uuid", user=_viewer(ALICE), db=db)
    assert exc.value.status_code == 404
    assert (await get_exposure(str(server.id), user=_viewer(ADMIN, "admin"), db=db))["resource"]["id"]


async def test_the_summary_is_three_tier_for_an_admin_too(db):
    await _add(db, MCPServer(user_id=ALICE, name="Private", transport="sse"))
    mine = await _add(db, MCPServer(user_id=ADMIN, name="Mine", transport="sse"))
    summary = await _endpoint("mcp_server_exposure_summary")(user=_viewer(ADMIN, "admin"), db=db)
    assert set(summary["summaries"]) == {str(mine.id)}


async def test_the_preview_is_for_whoever_may_change_the_entity(db):
    agent = await _add(db, _agent(**_team()))
    body = routes.ExposurePreviewRequest(entity_type="agent", entity_id=str(agent.id), change={"web_enabled": True})
    assert (await routes.post_exposure_preview(body, user=_viewer(ALICE), db=db))["items"] == []
    with pytest.raises(HTTPException) as exc:
        await routes.post_exposure_preview(body, user=_viewer(BOB), db=db)
    assert exc.value.status_code == 403
    ga = routes.ExposurePreviewRequest(entity_type="general_assistant", change={})
    with pytest.raises(HTTPException) as exc:
        await routes.post_exposure_preview(ga, user=_viewer(ALICE), db=db)
    assert exc.value.status_code == 403
    new_assignment = routes.ExposurePreviewRequest(entity_type="assignment", parent_id=str(agent.id), change={})
    with pytest.raises(HTTPException) as exc:
        await routes.post_exposure_preview(new_assignment, user=_viewer(DAVE), db=db)
    assert exc.value.status_code == 404
