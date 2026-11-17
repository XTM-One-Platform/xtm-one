"""Typed names past their column refused, derived names fitted, on PostgreSQL.

Three paths wrote a value longer than its ``String(n)`` column and answered
the database's refusal as a 500: a create or update route writing a name as
typed, an agent rename writing ``<name> - Knowledge Base`` onto its
system-managed knowledge bases, and a skill-package import deriving its tool
names (``<skill>-<script>``, then ``-imported-N`` on a collision). Driven
through the real routers (as an ordinary user, or an admin where the route is
admin-only): a typed value that does not fit is refused naming the field and
the limit, with nothing written; a derived one is shortened so the write
succeeds; a value at the limit is stored as sent. Same scratch-database recipe
as ``test_name_length_guards_postgres.py``.
"""

from __future__ import annotations

import io
import json
import uuid
import zipfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

import httpx
import pytest
from fastapi import Depends, FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.api import deps
from app.api.v1 import agent_skills as skills_api
from app.api.v1 import agent_templates as templates_api
from app.api.v1 import agents as agents_api
from app.api.v1 import api_keys as api_keys_api
from app.api.v1 import channel_bots as channel_bots_api
from app.api.v1 import custom_tools as custom_tools_api
from app.api.v1 import groups as groups_api
from app.api.v1 import integrations as integrations_api
from app.api.v1 import intents as intents_api
from app.api.v1 import knowledge_bases as knowledge_bases_api
from app.api.v1 import mcp_servers as mcp_servers_api
from app.api.v1 import prompts as prompts_api
from app.api.v1 import settings as settings_api
from app.api.v1 import variables as variables_api
from app.core.database import get_db
from app.models.agent import Agent
from app.models.agent_skill import AgentSkill
from app.models.agent_template import AgentTemplate
from app.models.api_key import UserApiKey
from app.models.channel_bot import ChannelBot
from app.models.custom_tool import CustomTool
from app.models.group import Group
from app.models.integration import Integration
from app.models.intent import Intent, Vertical
from app.models.knowledge_base import KnowledgeBase
from app.models.mcp_server import MCPServer
from app.models.prompt import Prompt
from app.models.user import User
from app.models.variable import Variable
from app.models.workset import Workset
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


def _app(sessions: async_sessionmaker, user: User) -> FastAPI:
    """The real routers, as *user*, one session per request."""
    app = FastAPI()
    app.include_router(templates_api.template_router, prefix="/api/v1/agents")
    app.include_router(agents_api.router, prefix="/api/v1/agents")
    app.include_router(knowledge_bases_api.router, prefix="/api/v1/knowledge-bases")
    app.include_router(prompts_api.router, prefix="/api/v1/prompts")
    app.include_router(skills_api.router, prefix="/api/v1/skills")
    app.include_router(custom_tools_api.router, prefix="/api/v1/custom-tools")
    app.include_router(mcp_servers_api.router, prefix="/api/v1/mcp-servers")
    app.include_router(integrations_api.router, prefix="/api/v1/integrations")
    app.include_router(channel_bots_api.router, prefix="/api/v1/channel-bots")
    app.include_router(groups_api.router, prefix="/api/v1/groups")
    app.include_router(variables_api.router, prefix="/api/v1/variables")
    app.include_router(intents_api.router, prefix="/api/v1")
    app.include_router(api_keys_api.router, prefix="/api/v1/api-keys")
    app.include_router(settings_api.router, prefix="/api/v1/settings")

    async def _get_db():
        async with sessions() as db:
            yield db
            await db.commit()

    # Loaded on the request's own session, as the real dependency does: a route
    # that writes to the caller's row writes through it.
    async def _current_user(db: AsyncSession = Depends(get_db)) -> User:
        return await db.get(User, user.id)

    app.dependency_overrides[deps.get_current_user] = _current_user
    app.dependency_overrides[get_db] = _get_db
    for gate in (deps.require_agentic_ai, deps.require_ai_enabled, deps.require_agentic_ai_licensed):
        app.dependency_overrides[gate] = lambda: None
    return app


@asynccontextmanager
async def _client(sessions: async_sessionmaker, user: User) -> AsyncIterator[httpx.AsyncClient]:
    # A server error is returned as the 500 it answers, not raised in the test.
    transport = httpx.ASGITransport(app=_app(sessions, user), raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


def _refusal(field_name: str, limit: int) -> dict:
    return {
        "loc": ["body", field_name],
        "msg": f"Value error, {field_name} must be at most {limit} characters (got {limit + 1}).",
    }


def _errors(response: httpx.Response) -> list[dict]:
    return [{"loc": e["loc"], "msg": e["msg"]} for e in response.json()["detail"]]


async def _names(sessions: async_sessionmaker, model: type) -> list[str]:
    async with sessions() as db:
        return sorted((await db.execute(select(model.name))).scalars())


# ---------------------------------------------------------------------------
# Create and update routes: a typed name its column cannot hold is refused
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EntityRoute:
    label: str
    model: type
    create: str
    body: dict = field(default_factory=dict)
    #: The update path, formatted with the created row's ``key``; ``None``: no update route.
    update: str | None = "{create}/{id}"
    key: str = "id"
    by_admin: bool = False


ENTITY_ROUTES = [
    EntityRoute("agent", Agent, "/api/v1/agents", {"persona": "p"}),
    EntityRoute("agent template", AgentTemplate, "/api/v1/agents/templates", {"slug": "digest"},
                update="{create}/{slug}", key="slug", by_admin=True),
    EntityRoute("knowledge base", KnowledgeBase, "/api/v1/knowledge-bases",
                {"embedding_model": "text-embedding-3-small"}),
    EntityRoute("prompt", Prompt, "/api/v1/prompts", {"content": "Summarise."}),
    EntityRoute("skill", AgentSkill, "/api/v1/skills", {"content": "# Skill"}),
    EntityRoute("custom tool", CustomTool, "/api/v1/custom-tools", {"js_function": "return 1;"}),
    EntityRoute("MCP server", MCPServer, "/api/v1/mcp-servers", {"transport": "stdio", "command": "docs"}),
    EntityRoute("integration", Integration, "/api/v1/integrations", {"provider": "drata"}),
    EntityRoute("channel bot", ChannelBot, "/api/v1/channel-bots", {"provider": "telegram_bot"},
                by_admin=True),
    EntityRoute("group", Group, "/api/v1/groups", by_admin=True),
    EntityRoute("variable", Variable, "/api/v1/variables", {"value": "eu"}),
    EntityRoute("intent", Intent, "/api/v1/intents", by_admin=True),
    EntityRoute("vertical", Vertical, "/api/v1/verticals", by_admin=True),
    EntityRoute("API key", UserApiKey, "/api/v1/api-keys", update=None),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ENTITY_ROUTES, ids=lambda c: c.label)
async def test_a_typed_name_its_column_cannot_hold_is_refused(case, monkeypatch):
    from app.core.config import settings

    # Off by default in XTM One mode, which CI runs.
    monkeypatch.setattr(settings, "disable_custom_variables", False)
    limit = case.model.__table__.c.name.type.length
    async with _temp_database("typed_len") as sessions:
        user, admin = await _users(sessions)
        async with _client(sessions, admin if case.by_admin else user) as client:
            refused = await client.post(case.create, json={**case.body, "name": "n" * (limit + 1)})
            assert refused.status_code == 422, refused.text
            assert _errors(refused) == [_refusal("name", limit)]
            assert await _names(sessions, case.model) == []

            created = await client.post(case.create, json={**case.body, "name": "n" * limit})
            assert created.status_code in (200, 201), created.text
            assert await _names(sessions, case.model) == ["n" * limit]
            if case.update is None:
                return

            path = case.update.format(create=case.create, **{case.key: created.json()[case.key]})
            refused = await client.put(path, json={"name": "m" * (limit + 1)})
            assert refused.status_code == 422, refused.text
            assert _errors(refused) == [_refusal("name", limit)]
            assert await _names(sessions, case.model) == ["n" * limit]

            renamed = await client.put(path, json={"name": "m" * limit})
            assert renamed.status_code == 200, renamed.text
            assert await _names(sessions, case.model) == ["m" * limit]


@pytest.mark.asyncio
async def test_padding_the_route_strips_still_fits():
    """Groups strip the name before writing it: padding is not counted."""
    limit = Group.__table__.c.name.type.length
    async with _temp_database("typed_len") as sessions:
        _user, admin = await _users(sessions)
        async with _client(sessions, admin) as client:
            response = await client.post("/api/v1/groups", json={"name": f"  {'g' * limit}  "})

        assert response.status_code == 201, response.text
        assert await _names(sessions, Group) == ["g" * limit]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("route", "body", "field_name", "column"),
    [
        ("/api/v1/agents", {"name": "Digest", "persona": "p"}, "icon", Agent.__table__.c.icon),
        ("/api/v1/agents", {"name": "Digest", "persona": "p"}, "output_format",
         Agent.__table__.c.output_format),
        ("/api/v1/custom-tools", {"name": "Lookup"}, "icon_url", CustomTool.__table__.c.icon_url),
        ("/api/v1/mcp-servers", {"name": "Docs", "transport": "stdio"}, "command",
         MCPServer.__table__.c.command),
        ("/api/v1/mcp-servers", {"name": "Docs", "transport": "sse"}, "url", MCPServer.__table__.c.url),
    ],
    ids=["agent icon", "agent output format", "tool icon", "MCP command", "MCP url"],
)
async def test_other_typed_text_its_column_cannot_hold_is_refused(route, body, field_name, column):
    limit = column.type.length
    # An MCP server URL must also be a URL (a literal public address: no lookup).
    prefix = "https://93.184.216.34/" if field_name == "url" else ""
    async with _temp_database("typed_len") as sessions:
        user, _admin = await _users(sessions)
        async with _client(sessions, user) as client:
            refused = await client.post(route, json={**body, field_name: prefix + "x" * (limit + 1 - len(prefix))})
            created = await client.post(route, json={**body, field_name: prefix + "x" * (limit - len(prefix))})

        assert refused.status_code == 422, refused.text
        assert _errors(refused) == [_refusal(field_name, limit)]
        assert created.status_code == 201, created.text
        async with sessions() as db:
            assert list((await db.execute(select(column))).scalars()) == [prefix + "x" * (limit - len(prefix))]


@pytest.mark.asyncio
async def test_a_profile_field_its_column_cannot_hold_is_refused():
    limit = User.__table__.c.full_name.type.length
    async with _temp_database("typed_len") as sessions:
        user, _admin = await _users(sessions)
        async with _client(sessions, user) as client:
            refused = await client.put(
                "/api/v1/settings/preferences", json={"full_name": "f" * (limit + 1)}
            )
        saved = await _put_and_read_on_send(
            _app(sessions, user),
            sessions,
            "/api/v1/settings/preferences",
            {"full_name": "f" * limit},
            lambda db: db.scalar(select(User.full_name).where(User.id == user.id)),
        )

        assert refused.status_code == 422, refused.text
        assert _errors(refused) == [_refusal("full_name", limit)]
        # Read from another connection as the 200 goes out: the client's next
        # request (the profile refetch) must see the saved value.
        assert saved == {"status": 200, "read": "f" * limit}


async def _put_and_read_on_send(app: FastAPI, sessions: async_sessionmaker, path: str, payload: dict, read):
    """PUT *payload*, running *read* on another session as the response body goes out."""
    inbound = [{"type": "http.request", "body": json.dumps(payload).encode(), "more_body": False}]
    outcome: dict = {}

    async def receive():
        return inbound.pop(0) if inbound else {"type": "http.disconnect"}

    async def send(message):
        if message["type"] == "http.response.start":
            outcome["status"] = message["status"]
        elif message["type"] == "http.response.body" and not message.get("more_body"):
            async with sessions() as probe:
                outcome["read"] = await read(probe)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "PUT",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": [(b"host", b"testserver"), (b"content-type", b"application/json")],
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
    }
    await app(scope, receive, send)
    return outcome


# ---------------------------------------------------------------------------
# Agent rename: the knowledge base named after the agent is fitted
# ---------------------------------------------------------------------------


KB_SUFFIX = " - Knowledge Base"


async def _rename(new_name: str) -> tuple[httpx.Response, KnowledgeBase]:
    async with _temp_database("rename_len") as sessions:
        user, _admin = await _users(sessions)
        async with sessions() as db:
            kb = KnowledgeBase(user_id=user.id, name=f"Digest{KB_SUFFIX}", is_system_managed=True)
            db.add(kb)
            await db.flush()
            agent = Agent(
                user_id=user.id, name="Digest", slug="digest", knowledge_base_ids=[str(kb.id)]
            )
            db.add(agent)
            await db.commit()
        async with _client(sessions, user) as client:
            response = await client.put(f"/api/v1/agents/{agent.id}", json={"name": new_name})
        async with sessions() as db:
            return response, await db.get(KnowledgeBase, kb.id)


@pytest.mark.asyncio
async def test_a_rename_to_an_at_limit_name_fits_its_knowledge_base_name():
    limit = Agent.__table__.c.name.type.length
    kb_limit = KnowledgeBase.__table__.c.name.type.length
    response, kb = await _rename("n" * limit)

    assert response.status_code == 200, response.text
    assert response.json()["name"] == "n" * limit
    assert kb.name == "n" * (kb_limit - len(KB_SUFFIX)) + KB_SUFFIX
    assert kb.description.startswith(f"Auto-created knowledge base for the {'n' * limit} agent.")


@pytest.mark.asyncio
async def test_a_rename_that_fits_names_its_knowledge_base_as_before():
    response, kb = await _rename("Weekly digest")

    assert response.status_code == 200, response.text
    assert kb.name == f"Weekly digest{KB_SUFFIX}"


# ---------------------------------------------------------------------------
# Skill-package import: derived names fitted, typed names refused
# ---------------------------------------------------------------------------


TOOL_LIMIT = CustomTool.__table__.c.name.type.length
SKILL_LIMIT = AgentSkill.__table__.c.name.type.length


def _skill_package(*stems: str, manifest: dict | None = None) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "demo/SKILL.md", "---\nname: demo\ndescription: A demo skill.\n---\n# Demo\n"
        )
        for stem in stems:
            archive.writestr(f"demo/scripts/{stem}.py", "print(1)\n")
        if manifest is not None:
            archive.writestr("demo/assets/xtm-resource-map.json", json.dumps(manifest))
    return buffer.getvalue()


async def _import_package(client: httpx.AsyncClient, package: bytes, **form: str) -> httpx.Response:
    return await client.post(
        "/api/v1/skills/import-package",
        files={"file": ("demo.zip", package, "application/zip")},
        data=form,
    )


@pytest.mark.asyncio
async def test_a_default_tool_name_longer_than_its_column_is_shortened():
    stem = "x" * (TOOL_LIMIT - len("demo-") + 1)
    async with _temp_database("skill_len") as sessions:
        user, _admin = await _users(sessions)
        async with _client(sessions, user) as client:
            response = await _import_package(client, _skill_package(stem))

        assert response.status_code == 201, response.text
        assert await _names(sessions, CustomTool) == [f"demo-{stem}"[:TOOL_LIMIT]]


@pytest.mark.asyncio
async def test_the_renamed_copy_of_an_at_limit_tool_name_is_shortened():
    """An ``as_new`` copy is the skill ``demo-imported``, and its default tool
    names are derived from that name: one more prefix on an at-limit name."""
    stem = "x" * (TOOL_LIMIT - len("demo-"))
    async with _temp_database("skill_len") as sessions:
        user, _admin = await _users(sessions)
        async with _client(sessions, user) as client:
            first = await _import_package(client, _skill_package(stem))
            second = await _import_package(client, _skill_package(stem), on_collision="as_new")

        assert (first.status_code, second.status_code) == (201, 201), second.text
        assert await _names(sessions, CustomTool) == sorted(
            [f"demo-{stem}", f"demo-imported-{stem}"[:TOOL_LIMIT]]
        )
        assert await _names(sessions, AgentSkill) == ["demo", "demo-imported"]


@pytest.mark.asyncio
async def test_a_second_import_keeps_its_counter_inside_the_column():
    """A tool name the manifest gives is the same in both copies: the second
    takes ``-imported-2``, which must fit too."""
    name = "n" * TOOL_LIMIT
    manifest = {"schema_version": 1, "scripts": [{"path": "scripts/run.py", "tool": {"name": name}}]}
    async with _temp_database("skill_len") as sessions:
        user, _admin = await _users(sessions)
        async with _client(sessions, user) as client:
            package = _skill_package("run", manifest=manifest)
            first = await _import_package(client, package)
            second = await _import_package(client, package, on_collision="as_new")

        assert (first.status_code, second.status_code) == (201, 201), second.text
        assert await _names(sessions, CustomTool) == sorted(
            [name, f"{name[: TOOL_LIMIT - 11]}-imported-2"]
        )


@pytest.mark.asyncio
async def test_a_short_package_imports_its_names_as_before():
    async with _temp_database("skill_len") as sessions:
        user, _admin = await _users(sessions)
        async with _client(sessions, user) as client:
            first = await _import_package(client, _skill_package("fetch_data"))
            second = await _import_package(client, _skill_package("fetch_data"), on_collision="as_new")

        assert (first.status_code, second.status_code) == (201, 201), second.text
        assert await _names(sessions, CustomTool) == ["demo-fetch-data", "demo-imported-fetch-data"]
        assert await _names(sessions, AgentSkill) == ["demo", "demo-imported"]


@pytest.mark.asyncio
async def test_a_manifest_tool_name_its_column_cannot_hold_is_refused():
    manifest = {
        "schema_version": 1,
        "scripts": [{"path": "scripts/run.py", "tool": {"name": "n" * (TOOL_LIMIT + 1)}}],
    }
    async with _temp_database("skill_len") as sessions:
        user, _admin = await _users(sessions)
        async with _client(sessions, user) as client:
            response = await _import_package(client, _skill_package("run", manifest=manifest))

        assert response.status_code == 400, response.text
        assert response.json()["detail"] == (
            f"Invalid assets/xtm-resource-map.json: `tool.name` must be at most {TOOL_LIMIT} "
            f"characters (got {TOOL_LIMIT + 1}) for scripts/run.py."
        )
        assert await _names(sessions, AgentSkill) == []
        assert await _names(sessions, CustomTool) == []


@pytest.mark.asyncio
async def test_a_new_name_its_column_cannot_hold_is_refused():
    async with _temp_database("skill_len") as sessions:
        user, _admin = await _users(sessions)
        async with _client(sessions, user) as client:
            first = await _import_package(client, _skill_package("run"))
            refused = await _import_package(
                client, _skill_package("run"), on_collision="as_new", new_name="n" * (SKILL_LIMIT + 1)
            )
            renamed = await _import_package(
                client, _skill_package("run"), on_collision="as_new", new_name="n" * SKILL_LIMIT
            )

        assert first.status_code == 201, first.text
        assert refused.status_code == 422, refused.text
        assert refused.json()["detail"] == (
            f"new_name must be at most {SKILL_LIMIT} characters (got {SKILL_LIMIT + 1})."
        )
        assert renamed.status_code == 201, renamed.text
        assert await _names(sessions, AgentSkill) == sorted(["demo", "n" * SKILL_LIMIT])
        # The renamed skill's default tool name is derived from the new name, fitted.
        assert await _names(sessions, CustomTool) == sorted(
            ["demo-run", f"{'n' * SKILL_LIMIT}-run"[:TOOL_LIMIT]]
        )


# ---------------------------------------------------------------------------
# Workset corpus: the knowledge base named after the workset key is fitted
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("key_length", [12, Workset.__table__.c.key.type.length])
async def test_a_workset_corpus_is_named_within_its_column(key_length):
    from app.services.workset import ensure_corpus

    kb_limit = KnowledgeBase.__table__.c.name.type.length
    key = "k" * key_length
    async with _temp_database("workset_len") as sessions:
        user, _admin = await _users(sessions)
        async with sessions() as db:
            workset = Workset(user_id=user.id, key=key)
            db.add(workset)
            await db.commit()
            kb_id = await ensure_corpus(db, workset, user)
            await db.commit()
        async with sessions() as db:
            kb = await db.get(KnowledgeBase, kb_id)
            count = (await db.execute(select(func.count()).select_from(KnowledgeBase))).scalar_one()

        assert kb.name == f"workset:{key}"[:kb_limit]
        assert count == 1
