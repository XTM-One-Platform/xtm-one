"""Copy names and imported text fields, against a real PostgreSQL.

Every copy path derived the copy's name from the original without fitting it
to the column: ``Copy of {name}`` (agent duplicate, evaluation copy) and
``{name} (copy)`` (catalog install) answered a 500 for a long original, and
``{name} (copy)`` cut to 200 characters (flow and template duplicate) dropped
the suffix. The slug a copy, an import or a catalog install derives from an
at-limit name took a counter past ``Agent.slug`` on its second use, and an
installer's own name was written as typed. Agent import wrote the other text
fields of a bundle as sent, so a string longer than its column or a non-string
answered a 500 after the first rows were flushed. Driven through the real
routers as an ordinary user (the evaluation copy is admin-only): the original
is shortened with its affix kept, the slug fits, an installer's name its column
cannot hold is refused, the import answers a 400 naming the entity, the field
and the limit with nothing written, and everything that fits is unchanged.
Same scratch-database recipe as ``test_assignment_visibility_postgres.py``.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.api import deps
from app.api.v1 import agents as agents_api
from app.api.v1 import evaluations as evaluations_api
from app.api.v1 import flows as flows_api
from app.api.v1 import presentations as presentations_api
from app.api.v1 import public_catalog as catalog_api
from app.api.v1 import template_containers as templates_api
from app.core.database import get_db
from app.models.agent import Agent
from app.models.agent_skill import AgentSkill
from app.models.assignment import Assignment
from app.models.custom_tool import CustomTool
from app.models.evaluation import Evaluation
from app.models.flow import Flow
from app.models.mcp_server import MCPServer
from app.models.presentation import Presentation
from app.models.prompt import Prompt
from app.models.template_container import TemplateContainer
from app.models.user import User
from app.models.variable import Variable
from app.services.agentschema import build_agentschema_export
from tests.scratch_database import ALL_TABLES, scratch_database
from tests.test_name_length_guards import _REFUSED

pytestmark = pytest.mark.integration


@asynccontextmanager
async def _temp_database(prefix: str) -> AsyncIterator[async_sessionmaker]:
    """Isolated database with the full schema, dropped on exit."""
    async with scratch_database(
        prefix,
        tables=ALL_TABLES,
        extensions=("vector",),
        skip_without_extensions=True,
        refuse_other_databases=True,
    ) as db:
        yield db.sessions


async def _users(sessions: async_sessionmaker) -> tuple[User, User]:
    """An ordinary user and an admin."""
    async with sessions() as db:
        users = tuple(
            User(
                email=f"{role}-{uuid.uuid4().hex[:8]}@example.com",
                hashed_password="x",
                full_name=role,
                role=role,
            )
            for role in ("user", "admin")
        )
        db.add_all(users)
        await db.commit()
    assert not users[0].is_admin and users[1].is_admin
    return users


@asynccontextmanager
async def _client(sessions: async_sessionmaker, user: User) -> AsyncIterator[httpx.AsyncClient]:
    """The real routers, as *user*, one session per request."""
    app = FastAPI()
    app.include_router(agents_api.router, prefix="/api/v1/agents")
    app.include_router(evaluations_api.router, prefix="/api/v1/evaluations")
    app.include_router(flows_api.router, prefix="/api/v1/flows")
    app.include_router(presentations_api.router, prefix="/api/v1/presentations")
    app.include_router(templates_api.router, prefix="/api/v1/template-containers")
    app.include_router(catalog_api.router, prefix="/api/v1/public")

    async def _get_db():
        async with sessions() as db:
            yield db
            await db.commit()

    app.dependency_overrides[deps.get_current_user] = lambda: user
    app.dependency_overrides[get_db] = _get_db
    for gate in (deps.require_agentic_ai, deps.require_ai_enabled, deps.require_agentic_ai_licensed):
        app.dependency_overrides[gate] = lambda: None
    # A server error is returned as the 500 it answers, not raised in the test.
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


# ---------------------------------------------------------------------------
# Copy names
# ---------------------------------------------------------------------------


def _deck(title: str) -> dict:
    return {"title": title, "slides": [{"layout": "title", "title": "Hello"}]}


@dataclass(frozen=True)
class CopyPath:
    label: str
    model: type
    route: str
    prefix: str = ""
    suffix: str = ""
    column: str = "name"
    by_admin: bool = False
    source: dict = field(default_factory=dict)


COPY_PATHS = [
    CopyPath("agent duplicate", Agent, "/api/v1/agents/{id}/duplicate", prefix="Copy of ",
             source={"persona": "p"}),
    CopyPath("evaluation copy", Evaluation, "/api/v1/evaluations/{id}/copy", prefix="Copy of ",
             by_admin=True),
    CopyPath("flow duplicate", Flow, "/api/v1/flows/{id}/duplicate", suffix=" (copy)"),
    CopyPath("presentation duplicate", Presentation, "/api/v1/presentations/{id}/duplicate",
             prefix="Copy of ", column="title"),
    CopyPath("template duplicate", TemplateContainer, "/api/v1/template-containers/{id}/duplicate",
             suffix=" (copy)"),
    CopyPath("catalog agent", Agent, "/api/v1/public/catalog/agents/{id}/add", suffix=" (copy)",
             source={"persona": "p", "is_public_catalog": True, "is_company_managed": True}),
    CopyPath("catalog prompt", Prompt, "/api/v1/public/catalog/prompts/{id}/add", suffix=" (copy)",
             source={"content": "c", "is_public_catalog": True, "is_company_managed": True}),
    CopyPath("catalog skill", AgentSkill, "/api/v1/public/catalog/skills/{id}/add", suffix=" (copy)",
             source={"content": "c", "is_public_catalog": True, "is_company_managed": True}),
    CopyPath("catalog MCP server", MCPServer, "/api/v1/public/catalog/mcp-servers/{id}/add",
             suffix=" (copy)",
             source={"transport": "stdio", "command": "docs", "is_public_catalog": True,
                     "is_company_managed": True}),
    CopyPath("catalog custom tool", CustomTool, "/api/v1/public/catalog/custom-tools/{id}/add",
             suffix=" (copy)",
             source={"js_function": "return 1;", "is_public_catalog": True, "is_company_managed": True}),
]


def _limit(case: CopyPath) -> int:
    return case.model.__table__.c[case.column].type.length


async def _copy(case: CopyPath, original: str) -> tuple[int, str | None, str]:
    """Copy a source named *original*; return the status, the copy's name and the body."""
    async with _temp_database("copy_len") as sessions:
        user, admin = await _users(sessions)
        actor = admin if case.by_admin else user
        fields = {case.column: original, **case.source}
        if case.model is Agent:
            fields["slug"] = f"src-{uuid.uuid4().hex[:8]}"
        if case.model is Presentation:
            fields["spec"] = _deck(original)
        async with sessions() as db:
            row = case.model(user_id=actor.id, **fields)
            db.add(row)
            await db.commit()
            source_id = row.id
        async with _client(sessions, actor) as client:
            response = await client.post(case.route.format(id=source_id))
        body = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
        return response.status_code, body.get(case.column), response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("case", COPY_PATHS, ids=lambda c: c.label)
async def test_a_long_original_is_shortened_and_the_affix_kept(case):
    limit = _limit(case)
    status, name, text = await _copy(case, "n" * limit)

    assert status in (200, 201), text
    room = limit - len(case.prefix) - len(case.suffix)
    assert name == f"{case.prefix}{'n' * room}{case.suffix}"


@pytest.mark.asyncio
@pytest.mark.parametrize("case", COPY_PATHS, ids=lambda c: c.label)
async def test_a_name_that_fits_is_copied_as_before(case):
    status, name, text = await _copy(case, "Weekly digest")

    assert status in (200, 201), text
    assert name == f"{case.prefix}Weekly digest{case.suffix}"


CATALOG_PATHS = [case for case in COPY_PATHS if case.label.startswith("catalog")]


async def _install(case: CopyPath, body: dict) -> tuple[int, str, list[str]]:
    """Install a catalog source with *body*; return the status, the body and the names stored."""
    async with _temp_database("copy_len") as sessions:
        user, admin = await _users(sessions)
        fields = {"name": "Weekly digest", **case.source}
        if case.model is Agent:
            fields["slug"] = f"src-{uuid.uuid4().hex[:8]}"
        async with sessions() as db:
            row = case.model(user_id=admin.id, **fields)
            db.add(row)
            await db.commit()
            source_id = row.id
        async with _client(sessions, user) as client:
            response = await client.post(case.route.format(id=source_id), json=body)
        async with sessions() as db:
            names = list((await db.execute(select(case.model.name))).scalars())
        return response.status_code, response.text, names


@pytest.mark.asyncio
@pytest.mark.parametrize("case", CATALOG_PATHS, ids=lambda c: c.label)
async def test_an_installer_name_its_column_cannot_hold_is_refused(case):
    """The installer's own name is kept as typed, so it is refused, never cut."""
    limit = _limit(case)
    status, text, names = await _install(case, {"name": "n" * (limit + 1)})

    assert status == 400, text
    assert json.loads(text)["detail"] == f"name must be at most {limit} characters (got {limit + 1})."
    assert names == ["Weekly digest"]


@pytest.mark.asyncio
@pytest.mark.parametrize("case", CATALOG_PATHS, ids=lambda c: c.label)
async def test_an_installer_name_that_fits_is_used_as_typed(case):
    limit = _limit(case)
    status, text, names = await _install(case, {"name": "n" * limit})

    assert status == 200, text
    assert sorted(names) == sorted(["Weekly digest", "n" * limit])


# ---------------------------------------------------------------------------
# Agent import, every format
# ---------------------------------------------------------------------------


IMPORT_ROUTES = ("json", "native_file", "agentschema_file", "confirm")


async def _import(client: httpx.AsyncClient, route: str, bundle: dict) -> httpx.Response:
    if route == "json":
        return await client.post("/api/v1/agents/import", json={"export_type": "agent", **bundle})
    if route == "native_file":
        upload = ("agent.json", json.dumps({"export_type": "agent", **bundle}), "application/json")
        return await client.post("/api/v1/agents/import-file", files={"file": upload})
    if route == "agentschema_file":
        text, _document = build_agentschema_export(bundle)
        upload = ("agent.yaml", text, "application/yaml")
        return await client.post("/api/v1/agents/import-file", files={"file": upload})
    assert route == "confirm"
    return await client.post(
        "/api/v1/agents/import/confirm", json={"bundle": {"export_type": "agent", **bundle}}
    )


async def _written(sessions: async_sessionmaker) -> dict[str, int]:
    async with sessions() as db:
        return {
            model.__tablename__: (await db.execute(select(func.count()).select_from(model))).scalar_one()
            for model in (Agent, Variable, CustomTool, MCPServer, AgentSkill, Prompt)
        }


#: Refused through every entry point; the rest of ``_REFUSED`` through the JSON body.
_EVERY_ROUTE = [0, 2, 8]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("route", "index"),
    [(route, index) for route in IMPORT_ROUTES for index in _EVERY_ROUTE]
    + [("json", index) for index in range(len(_REFUSED)) if index not in _EVERY_ROUTE],
)
async def test_import_refuses_a_value_its_column_cannot_take(route, index):
    bundle, detail = _REFUSED[index]
    async with _temp_database("import_len") as sessions:
        user, _admin = await _users(sessions)
        async with _client(sessions, user) as client:
            response = await _import(client, route, bundle)

        assert response.status_code == 400, response.text
        assert response.json()["detail"] == detail
        assert set((await _written(sessions)).values()) == {0}


def _fitting_bundle() -> dict:
    def limit(model, name: str) -> int:
        return model.__table__.c[name].type.length

    return {
        "version": 3,
        "agent": {
            "name": "n" * limit(Agent, "name"),
            "persona": "p",
            "icon": "i" * limit(Agent, "icon"),
            "llm_model": "m" * limit(Agent, "llm_model"),
        },
        "variables": [{"id": str(uuid.uuid4()), "name": "TOKEN", "value": 12345, "is_secret": True}],
        "custom_tools": [{"name": "Lookup", "icon_url": "u" * limit(CustomTool, "icon_url")}],
        "mcp_servers": [
            {"name": "Docs", "transport": "t" * limit(MCPServer, "transport"),
             "command": "c" * limit(MCPServer, "command")}
        ],
        "prompts": [{"name": "Weekly", "content": "Summarise."}],
        "assignments": [
            {"name": "Digest", "prompt": "Go.", "description": "", "activation_prompt": None,
             "delay": "1" * (limit(Assignment, "delay") - 1) + "m"}
        ],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("route", IMPORT_ROUTES)
async def test_import_keeps_every_value_its_column_takes(route):
    bundle = _fitting_bundle()
    async with _temp_database("import_len") as sessions:
        user, _admin = await _users(sessions)
        async with _client(sessions, user) as client:
            response = await _import(client, route, bundle)

        assert response.status_code == 201, response.text
        async with sessions() as db:
            agent = (await db.execute(select(Agent))).scalar_one()
            tool = (await db.execute(select(CustomTool))).scalar_one()
            server = (await db.execute(select(MCPServer))).scalar_one()
        # No provider lists any model here, so the catalog check is skipped
        # and a model that fits is kept.
        assert (agent.name, agent.icon, agent.llm_model) == (
            bundle["agent"]["name"], bundle["agent"]["icon"], bundle["agent"]["llm_model"]
        )
        assert tool.icon_url == bundle["custom_tools"][0]["icon_url"]
        assert (server.transport, server.command) == (
            bundle["mcp_servers"][0]["transport"], bundle["mcp_servers"][0]["command"]
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("route", IMPORT_ROUTES)
@pytest.mark.parametrize("key", ["llm_model", "llm_provider"])
async def test_import_drops_a_model_its_column_cannot_hold(route, key):
    """Like an unlisted model, not a refusal: on an install whose providers list
    models, such a model is dropped by the catalog check and the bundle
    imports, so refusing it would break that import."""
    bundle = {
        "version": 3,
        "agent": {"name": "Imported agent", "persona": "p", "llm_provider": "anthropic",
                  "llm_model": "claude", key: "m" * (Agent.__table__.c[key].type.length + 1)},
    }
    async with _temp_database("import_len") as sessions:
        user, _admin = await _users(sessions)
        async with _client(sessions, user) as client:
            response = await _import(client, route, bundle)

        assert response.status_code == 201, response.text
        async with sessions() as db:
            agent = (await db.execute(select(Agent))).scalar_one()
        assert (agent.llm_provider, agent.llm_model) == (None, None)
        if route != "json":  # the JSON body route answers the agent alone
            assert any(
                w.startswith("The model override was not imported: ")
                for w in response.json()["warnings"]
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("route", [route for route in IMPORT_ROUTES if route != "json"])
async def test_a_model_the_catalog_drops_keeps_its_warning(route):
    """On an install whose providers list models, an over-long model is not in
    the catalog: it is dropped with the catalog's own warning, as before."""
    from app.models.llm_config import LLMProviderConfig

    model = "m" * (Agent.__table__.c.llm_model.type.length + 1)
    bundle = {
        "version": 3,
        "agent": {"name": "Imported agent", "persona": "p", "llm_provider": "anthropic", "llm_model": model},
    }
    async with _temp_database("import_len") as sessions:
        user, _admin = await _users(sessions)
        async with sessions() as db:
            db.add(LLMProviderConfig(provider="anthropic", api_key_enc="x", available_models=["claude"]))
            await db.commit()
        async with _client(sessions, user) as client:
            response = await _import(client, route, bundle)

        assert response.status_code == 201, response.text
        assert (
            f"The model '{model}' is not available to you on this platform; the imported "
            "agent uses the default model."
        ) in response.json()["warnings"]


@pytest.mark.asyncio
@pytest.mark.parametrize("route", IMPORT_ROUTES)
async def test_import_ignores_what_it_never_writes(route):
    """A variable without an ``id`` is skipped, and the legacy ``key`` is not
    read when ``name`` is set: whatever they hold, the bundle imports."""
    too_long = "x" * (Variable.__table__.c.name.type.length + 1)
    bundle = {
        "version": 3,
        "agent": {"name": "Imported agent", "persona": "p"},
        "variables": [
            {"name": too_long, "value": 5, "description": ["unused"]},
            {"id": str(uuid.uuid4()), "name": "REGION", "key": too_long, "value": "eu"},
        ],
    }
    async with _temp_database("import_len") as sessions:
        user, _admin = await _users(sessions)
        async with _client(sessions, user) as client:
            response = await _import(client, route, bundle)

        assert response.status_code == 201, response.text
        async with sessions() as db:
            assert list((await db.execute(select(Variable.name))).scalars()) == ["REGION"]


# ---------------------------------------------------------------------------
# Agent slugs
# ---------------------------------------------------------------------------

SLUG_LIMIT = Agent.__table__.c.slug.type.length
AGENT_NAME_LIMIT = Agent.__table__.c.name.type.length


async def _slugs(sessions: async_sessionmaker) -> list[str]:
    async with sessions() as db:
        return list((await db.execute(select(Agent.slug))).scalars())


async def _seed_agents(
    sessions: async_sessionmaker, owner: User, *slugs: str, name: str | None = None, **columns
) -> list[uuid.UUID]:
    """Agents of *owner* holding *slugs*, each named after its slug unless *name* is given."""
    async with sessions() as db:
        agents = [Agent(user_id=owner.id, name=name or slug, slug=slug, **columns) for slug in slugs]
        db.add_all(agents)
        await db.commit()
        return [agent.id for agent in agents]


@pytest.mark.asyncio
async def test_a_second_copy_of_an_at_limit_name_gets_a_slug_that_fits():
    """The copy's name fits, and so must the counter its slug takes when an
    earlier copy already holds the plain slug."""
    async with _temp_database("slug_len") as sessions:
        user, _admin = await _users(sessions)
        taken = "copy-of-" + "n" * (SLUG_LIMIT - len("copy-of-"))
        [source_id] = await _seed_agents(sessions, user, "src", name="n" * AGENT_NAME_LIMIT)
        await _seed_agents(sessions, user, taken)
        async with _client(sessions, user) as client:
            response = await client.post(f"/api/v1/agents/{source_id}/duplicate")

        assert response.status_code == 201, response.text
        assert taken[: SLUG_LIMIT - 2] + "-2" in await _slugs(sessions)


@pytest.mark.asyncio
@pytest.mark.parametrize("route", IMPORT_ROUTES)
async def test_a_second_import_of_an_at_limit_name_gets_a_slug_that_fits(route):
    async with _temp_database("slug_len") as sessions:
        user, _admin = await _users(sessions)
        name = "n" * AGENT_NAME_LIMIT
        await _seed_agents(sessions, user, name[:SLUG_LIMIT])
        async with _client(sessions, user) as client:
            response = await _import(client, route, {"version": 3, "agent": {"name": name, "persona": "p"}})

        assert response.status_code == 201, response.text
        assert sorted(await _slugs(sessions)) == sorted([name[:SLUG_LIMIT], name[: SLUG_LIMIT - 2] + "-2"])


@pytest.mark.asyncio
async def test_an_imported_name_whose_slug_is_longer_than_the_name():
    """``slugify`` decomposes U+33AF into ``rads2``: 41 characters, 205 in the slug."""
    async with _temp_database("slug_len") as sessions:
        user, _admin = await _users(sessions)
        async with _client(sessions, user) as client:
            response = await _import(client, "json", {"version": 3, "agent": {"name": "\u33af" * 41}})

        assert response.status_code == 201, response.text
        assert [len(slug) for slug in await _slugs(sessions)] == [SLUG_LIMIT]


@pytest.mark.asyncio
async def test_a_tenth_catalog_install_of_an_at_limit_name_gets_a_slug_that_fits():
    case = next(case for case in COPY_PATHS if case.label == "catalog agent")
    async with _temp_database("slug_len") as sessions:
        user, admin = await _users(sessions)
        base = "n" * (AGENT_NAME_LIMIT - len(" (copy)")) + "-copy"
        await _seed_agents(sessions, user, base, *(f"{base}-{n}" for n in range(2, 10)))
        [source_id] = await _seed_agents(sessions, admin, "src", name="n" * AGENT_NAME_LIMIT, **case.source)
        async with _client(sessions, user) as client:
            response = await client.post(case.route.format(id=source_id))

        assert response.status_code == 200, response.text
        assert base[: SLUG_LIMIT - 3].rstrip("-") + "-10" in await _slugs(sessions)


@pytest.mark.asyncio
async def test_the_agent_builder_fits_the_slug_of_an_at_limit_name():
    from app.services.builtin_tools import build_agent_builder_tool
    from app.tools.base import ToolCall

    async with _temp_database("slug_len") as sessions:
        user, _admin = await _users(sessions)
        name = "n" * AGENT_NAME_LIMIT
        await _seed_agents(sessions, user, name[:SLUG_LIMIT])
        _, executor = build_agent_builder_tool(None, str(user.id))
        result = await executor.execute_tool(
            ToolCall(id=str(uuid.uuid4()), name="create_agent", arguments={"name": name})
        )

        assert name[: SLUG_LIMIT - 2] + "-1" in await _slugs(sessions), result.content
