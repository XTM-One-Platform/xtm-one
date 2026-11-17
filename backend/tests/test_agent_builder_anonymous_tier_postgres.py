"""The agent-builder read tools give a caller with no account the company-wide
tier alone, against real PostgreSQL.

An anonymous channel sender reaches the builder with ``user_id="anonymous"``
(``channels/router.py``). Such a caller holds no grant, so every read tool must
answer it with ``company_wide_visible_filter``: company-managed rows, minus
those narrowed to an audience by ``access_restricted``, never an
``is_internal`` knowledge base. A signed-in caller keeps its own three
tiers, unchanged. A row the caller cannot see must answer exactly like an
unknown id, or the reply confirms it exists.

``integration``-marked and skips when PostgreSQL is unreachable, like its
neighbours; the temporary-database harness is theirs, repeated rather than
shared (see ``test_ai_disabled_read_surface_postgres.py``).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.core.database as database
import app.models  # noqa: F401 - registers every table on Base.metadata
from app.core.config import settings
from app.models.agent import Agent
from app.models.agent_skill import AgentSkill
from app.models.custom_tool import CustomTool
from app.models.integration import Integration
from app.models.knowledge_base import KnowledgeBase, KnowledgeDocument
from app.models.mcp_server import MCPServer
from app.models.prompt import Prompt
from app.models.user import User
from app.models.variable import Variable
from app.services.builtin_tools import build_agent_builder_tool
from app.tools.base import ToolCall
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration

ANONYMOUS = "anonymous"


@dataclass(frozen=True)
class Entity:
    model: type
    list_tool: str
    get_tool: str
    id_arg: str
    fields: dict


# Every entity type the builder reads, with the tools that read it.
ENTITIES = {
    "agent": Entity(Agent, "list_agents", "get_agent", "agent_id", {"persona": "p"}),
    "knowledge_base": Entity(
        KnowledgeBase, "list_knowledge_bases", "get_knowledge_base", "knowledge_base_id", {}
    ),
    "integration": Entity(
        Integration,
        "list_integrations",
        "get_integration",
        "integration_id",
        {"provider": "github", "status": "connected"},
    ),
    "mcp_server": Entity(
        MCPServer,
        "list_mcp_servers",
        "get_mcp_server",
        "server_id",
        {"transport": "stdio", "command": "true", "enabled": True},
    ),
    "custom_tool": Entity(
        CustomTool,
        "list_custom_tools",
        "get_custom_tool",
        "tool_id",
        {"description": "d", "enabled": True},
    ),
    "prompt": Entity(
        Prompt, "list_prompts", "get_prompt", "prompt_id", {"description": "d", "content": "c"}
    ),
    "skill": Entity(AgentSkill, "list_skills", "get_skill", "skill_id", {"content": "c"}),
    "variable": Entity(Variable, "list_variables", "get_variable", "variable_id", {}),
}

# ``open`` is company-managed, ``restricted`` is company-managed narrowed to
# ``audience``, ``private`` is ``owner``'s own. The anonymous caller sees ``open``.
KINDS = ("open", "restricted", "private")


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


@dataclass
class World:
    users: dict[str, User]
    rows: dict[tuple[str, str], object]  # (entity, kind) -> row
    internal_kb: KnowledgeBase
    documents: dict[str, KnowledgeDocument]  # KB kind -> its document


def _row(entity: Entity, kind: str, users: dict[str, User]):
    fields = {"name": f"{entity.model.__tablename__}-{kind}-{uuid.uuid4().hex[:8]}"}
    fields.update(entity.fields)
    if entity.model is Agent:
        fields["slug"] = fields["name"]
    if kind == "private":
        fields["user_id"] = users["owner"].id
    else:
        fields["user_id"] = users["admin"].id
        fields["is_company_managed"] = True
    if kind == "restricted":
        audience = str(users["audience"].id)
        fields["access_restricted"] = True
        fields["shared_user_ids"] = [audience]
        fields["shared_user_permissions"] = {audience: "read_only"}
    return entity.model(**fields)


async def _seed(session_factory: async_sessionmaker) -> World:
    async with session_factory() as db:
        users = {
            name: User(
                email=f"{name}-{uuid.uuid4().hex[:8]}@example.com",
                hashed_password="x",
                full_name=name,
                role="admin" if name == "admin" else "user",
            )
            for name in ("owner", "audience", "admin")
        }
        db.add_all(users.values())
        await db.flush()

        rows = {
            (key, kind): _row(entity, kind, users)
            for key, entity in ENTITIES.items()
            for kind in KINDS
        }
        internal_kb = KnowledgeBase(
            name=f"knowledge_bases-internal-{uuid.uuid4().hex[:8]}",
            user_id=users["admin"].id,
            is_company_managed=True,
            is_internal=True,
        )
        db.add_all([*rows.values(), internal_kb])
        await db.flush()

        kbs = {kind: rows[("knowledge_base", kind)] for kind in KINDS}
        kbs["internal"] = internal_kb
        documents = {
            kind: KnowledgeDocument(
                knowledge_base_id=kb.id,
                filename=f"doc-{kind}-{uuid.uuid4().hex[:8]}.md",
                source_type="text",
            )
            for kind, kb in kbs.items()
        }
        db.add_all(documents.values())

        # The open agent names every open and restricted row it can bind, so
        # get_agent / list_agents(detail='full') resolve names through the tier.
        agent = rows[("agent", "open")]
        bound = {kind: {key: str(rows[(key, kind)].id) for key in ENTITIES} for kind in KINDS}
        agent.knowledge_base_ids = [bound[k]["knowledge_base"] for k in ("open", "restricted")]
        agent.prompt_ids = [bound[k]["prompt"] for k in ("open", "restricted")]
        agent.skill_ids = [bound[k]["skill"] for k in ("open", "restricted")]
        agent.mcp_server_ids = [bound[k]["mcp_server"] for k in ("open", "restricted")]
        agent.custom_tool_ids = [bound[k]["custom_tool"] for k in ("open", "restricted")]
        agent.handover_agent_ids = [bound["restricted"]["agent"]]
        agent.integration_bindings = {
            "github": [bound[k]["integration"] for k in ("open", "restricted")]
        }
        await db.commit()
    return World(users=users, rows=rows, internal_kb=internal_kb, documents=documents)


@pytest.fixture
def builder_db(monkeypatch):
    """Point the builder's dedicated sessions at a test database, with the
    custom-tool and variable tools switched on as on a Copilot install."""
    monkeypatch.setattr(settings, "disable_custom_tools", False)
    monkeypatch.setattr(settings, "disable_custom_variables", False)

    def _use(session_factory: async_sessionmaker) -> None:
        monkeypatch.setattr(database, "async_session", session_factory)

    return _use


async def _tool(caller: User | str, tool: str, /, **arguments: str) -> str:
    """Run a builder tool as *caller*, or as the raw user id an anonymous
    channel turn hands the builder."""
    user_id = caller if isinstance(caller, str) else str(caller.id)
    _, executor = build_agent_builder_tool(None, user_id)
    result = await executor.execute_tool(
        ToolCall(id=str(uuid.uuid4()), name=tool, arguments=arguments)
    )
    return result.content


def _masked(content: str, row_id) -> str:
    return content.replace(str(row_id), "<id>")


async def _answers_like_a_missing_id(caller, tool: str, arg: str, row_id, **extra) -> None:
    missing = uuid.uuid4()
    on_row = await _tool(caller, tool, **{arg: str(row_id)}, **extra)
    on_missing = await _tool(caller, tool, **{arg: str(missing)}, **extra)
    assert _masked(on_row, row_id) == _masked(on_missing, missing), (tool, on_row)
    assert "not found" in on_missing, (tool, on_missing)


@pytest.mark.parametrize("key", ENTITIES)
async def test_an_anonymous_caller_lists_and_reads_the_company_wide_tier_alone(
    builder_db, key
):
    entity = ENTITIES[key]
    async with _temp_database("anontier") as session_factory:
        builder_db(session_factory)
        world = await _seed(session_factory)
        names = {kind: world.rows[(key, kind)].name for kind in KINDS}

        listed = await _tool(ANONYMOUS, entity.list_tool)
        assert names["open"] in listed, listed
        assert names["restricted"] not in listed, listed
        assert names["private"] not in listed, listed
        if key == "knowledge_base":
            assert world.internal_kb.name not in listed, listed

        opened = await _tool(
            ANONYMOUS, entity.get_tool, **{entity.id_arg: str(world.rows[(key, "open")].id)}
        )
        assert names["open"] in opened, opened
        hidden = [world.rows[(key, "restricted")], world.rows[(key, "private")]]
        if key == "knowledge_base":
            hidden.append(world.internal_kb)
        for row in hidden:
            await _answers_like_a_missing_id(ANONYMOUS, entity.get_tool, entity.id_arg, row.id)


@pytest.mark.parametrize("key", ENTITIES)
async def test_a_signed_in_caller_keeps_its_own_tiers(builder_db, key):
    """No change for an account: the audience of a restricted row sees it, the
    owner sees their private row and not the restricted one."""
    entity = ENTITIES[key]
    async with _temp_database("anontier_auth") as session_factory:
        builder_db(session_factory)
        world = await _seed(session_factory)
        names = {kind: world.rows[(key, kind)].name for kind in KINDS}
        expected = {"audience": {"open", "restricted"}, "owner": {"open", "private"}}

        for persona, visible in expected.items():
            user = world.users[persona]
            listed = await _tool(user, entity.list_tool)
            for kind in KINDS:
                assert (names[kind] in listed) is (kind in visible), (persona, kind, listed)
            for kind in KINDS:
                row = world.rows[(key, kind)]
                if kind in visible:
                    got = await _tool(user, entity.get_tool, **{entity.id_arg: str(row.id)})
                    assert names[kind] in got, (persona, kind, got)
                else:
                    await _answers_like_a_missing_id(user, entity.get_tool, entity.id_arg, row.id)


@pytest.mark.parametrize(
    ("resource_type", "key"),
    [
        ("integrations", "integration"),
        ("knowledge_bases", "knowledge_base"),
        ("mcp_servers", "mcp_server"),
        ("custom_tools", "custom_tool"),
    ],
)
async def test_the_resource_catalog_gives_an_anonymous_caller_the_company_wide_tier(
    builder_db, resource_type, key
):
    async with _temp_database("anontier_res") as session_factory:
        builder_db(session_factory)
        world = await _seed(session_factory)
        listed = await _tool(ANONYMOUS, "list_available_resources", resource_type=resource_type)

        assert world.rows[(key, "open")].name in listed, listed
        assert world.rows[(key, "restricted")].name not in listed, listed
        assert world.rows[(key, "private")].name not in listed, listed
        if key == "knowledge_base":
            assert world.internal_kb.name not in listed, listed


async def test_an_anonymous_caller_reads_documents_of_the_company_wide_tier_alone(builder_db):
    async with _temp_database("anontier_docs") as session_factory:
        builder_db(session_factory)
        world = await _seed(session_factory)
        kbs = {kind: world.rows[("knowledge_base", kind)] for kind in KINDS}
        kbs["internal"] = world.internal_kb

        listed = await _tool(ANONYMOUS, "list_kb_documents", knowledge_base_id=str(kbs["open"].id))
        assert world.documents["open"].filename in listed, listed
        got = await _tool(
            ANONYMOUS,
            "get_kb_document",
            knowledge_base_id=str(kbs["open"].id),
            document_id=str(world.documents["open"].id),
        )
        assert world.documents["open"].filename in got, got

        for kind in ("restricted", "private", "internal"):
            await _answers_like_a_missing_id(
                ANONYMOUS, "list_kb_documents", "knowledge_base_id", kbs[kind].id
            )
            await _answers_like_a_missing_id(
                ANONYMOUS,
                "get_kb_document",
                "knowledge_base_id",
                kbs[kind].id,
                document_id=str(world.documents[kind].id),
            )


@pytest.mark.parametrize(("tool", "arguments"), [("get_agent", None), ("list_agents", {"detail": "full"})])
async def test_an_anonymous_caller_is_never_told_the_name_of_a_restricted_binding(
    builder_db, tool, arguments
):
    """A visible agent can reference rows the caller cannot see: their ids stay
    (they are in the agent row), their names are resolved through the tier."""
    async with _temp_database("anontier_names") as session_factory:
        builder_db(session_factory)
        world = await _seed(session_factory)
        agent = world.rows[("agent", "open")]
        if tool == "get_agent":
            content = await _tool(ANONYMOUS, tool, agent_id=str(agent.id))
            bound = ("knowledge_base", "prompt", "skill", "mcp_server", "custom_tool", "integration")
        else:
            content = await _tool(ANONYMOUS, tool, **arguments)
            bound = ("knowledge_base", "integration")

        for key in bound:
            assert world.rows[(key, "open")].name in content, (key, content)
            assert world.rows[(key, "restricted")].name not in content, (key, content)
            assert str(world.rows[(key, "restricted")].id) in content, (key, content)
        if tool == "get_agent":
            assert world.rows[("agent", "restricted")].name not in content, content


async def test_an_ownerless_company_managed_run_reads_agents_as_on_main(builder_db):
    """The builder of a scheduled company-managed run with no "run as" user
    gets no user id (``""``); that run keeps every company-managed agent, the
    restricted one included, and still never a private one."""
    from app.api.v1.helpers import company_managed_run_scoped, mark_company_managed_run
    from app.models.assignment import Assignment, AssignmentRun

    async with _temp_database("anontier_fleet") as session_factory:
        builder_db(session_factory)
        world = await _seed(session_factory)
        names = {kind: world.rows[("agent", kind)].name for kind in KINDS}
        async with session_factory() as db:
            assignment = Assignment(
                agent_id=world.rows[("agent", "open")].id,
                user_id=None,
                run_as_user_id=None,
                is_company_managed=True,
                name="fleet",
                trigger_type="schedule",
                trigger_config={},
                prompt="p",
                enabled=True,
            )
            db.add(assignment)
            await db.flush()
            run = AssignmentRun(
                assignment_id=assignment.id,
                agent_id=assignment.agent_id,
                user_id=None,
                is_company_managed=True,
                status="pending",
                trigger_payload={},
            )
            db.add(run)
            await db.commit()

        @company_managed_run_scoped
        async def _execute():
            assert mark_company_managed_run(assignment, run) is True
            listed = await _tool("", "list_agents")
            opened = await _tool("", "get_agent", agent_id=str(world.rows[("agent", "restricted")].id))
            return listed, opened

        listed, opened = await _execute()
        assert names["open"] in listed and names["restricted"] in listed, listed
        assert names["private"] not in listed, listed
        assert names["restricted"] in opened, opened

        # Outside the run, the same empty id is a caller with no account.
        outside = await _tool("", "list_agents")
        assert names["restricted"] not in outside, outside
