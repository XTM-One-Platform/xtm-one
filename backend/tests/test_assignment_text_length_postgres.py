"""A value longer than its assignment column, against a real PostgreSQL.

``Assignment.trigger_type`` is ``VARCHAR(30)``, ``name`` ``VARCHAR(200)`` and
``delay`` ``VARCHAR(20)``. Every path that writes them handed the value to the
INSERT or UPDATE as sent: PostgreSQL refuses a longer one
(``StringDataRightTruncationError``) and the request answered a 500. Driven
here through the real routers and tools as an ordinary (non-admin) user: the
REST create and update routes answer a 422 naming the field and the limit, the
agent-builder tools refuse with the same sentence, agent import answers a 400 in
every format, and a template's default assignment falls back to what it gets
when the key is absent. A refused request writes nothing, and every value the
column holds keeps its answer from before, an empty or unknown trigger type
included. Same scratch-database recipe as
``test_assignment_visibility_postgres.py``.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.api import deps
from app.api.v1 import agents as agents_api
from app.api.v1 import assignments as assignments_api
from app.core.database import get_db
from app.models.agent import Agent
from app.models.agent_template import AgentTemplate
from app.models.assignment import Assignment
from app.models.user import User
from app.services.agentschema import build_agentschema_export
from app.services.builtin_tools import build_agent_builder_tool
from app.tools.base import ToolCall
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration

#: The bounded text columns an assignment request writes as sent.
FIELDS = ("name", "trigger_type", "delay")


def _limit(field: str) -> int:
    return Assignment.__table__.c[field].type.length


def _too_long(field: str) -> str:
    return "x" * (_limit(field) + 1)


def _refusal(field: str) -> str:
    return f"{field} must be at most {_limit(field)} characters (got {_limit(field) + 1})."


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


async def _seed(sessions: async_sessionmaker) -> tuple[User, Agent]:
    """An ordinary (non-admin) user and a private agent of theirs."""
    async with sessions() as db:
        user = User(
            email=f"asg-len-{uuid.uuid4().hex[:8]}@example.com",
            hashed_password="x",
            full_name="Length Test",
        )
        db.add(user)
        await db.flush()
        agent = Agent(
            name="Length agent",
            slug=f"length-{uuid.uuid4().hex[:8]}",
            persona="p",
            user_id=user.id,
        )
        db.add(agent)
        await db.commit()
    assert not user.is_admin
    return user, agent


async def _add_assignment(sessions: async_sessionmaker, agent: Agent) -> uuid.UUID:
    async with sessions() as db:
        assignment = Assignment(
            agent_id=agent.id,
            user_id=agent.user_id,
            name="Digest",
            trigger_type="manual",
            prompt="Go.",
        )
        db.add(assignment)
        await db.commit()
        return assignment.id


async def _assignments(sessions: async_sessionmaker) -> list[Assignment]:
    async with sessions() as db:
        return list(
            (await db.execute(select(Assignment).order_by(Assignment.created_at))).scalars()
        )


async def _agent_names(sessions: async_sessionmaker) -> list[str]:
    async with sessions() as db:
        return list((await db.execute(select(Agent.name).order_by(Agent.created_at))).scalars())


@asynccontextmanager
async def _client(sessions: async_sessionmaker, user: User) -> AsyncIterator[httpx.AsyncClient]:
    """The real assignments and agents routers, as *user*, one session per request."""
    app = FastAPI()
    app.include_router(assignments_api.router, prefix="/api/v1/assignments")
    app.include_router(agents_api.router, prefix="/api/v1/agents")

    async def _get_db():
        async with sessions() as db:
            yield db
            await db.commit()

    app.dependency_overrides[deps.get_current_user] = lambda: user
    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[deps.require_agentic_ai] = lambda: None
    app.dependency_overrides[deps.require_ai_enabled] = lambda: None
    # A server error is returned as the 500 it answers, not raised in the test.
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def _tool(user: User, tool: str, arguments: dict[str, str]) -> str:
    _, executor = build_agent_builder_tool(None, str(user.id))
    result = await executor.execute_tool(
        ToolCall(id=str(uuid.uuid4()), name=tool, arguments=arguments)
    )
    return result.content


def _create_body(agent: Agent, **overrides: str) -> dict[str, str]:
    return {
        "agent_id": str(agent.id),
        "name": "Digest",
        "trigger_type": "manual",
        "prompt": "Go.",
        **overrides,
    }


# ---------------------------------------------------------------------------
# REST
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("field", FIELDS)
async def test_rest_create_refuses_a_value_longer_than_its_column(field):
    async with _temp_database("asg_len") as sessions:
        user, agent = await _seed(sessions)
        async with _client(sessions, user) as client:
            response = await client.post(
                "/api/v1/assignments", json=_create_body(agent, **{field: _too_long(field)})
            )

        assert response.status_code == 422, response.text
        [error] = response.json()["detail"]
        assert (error["type"], error["loc"]) == ("value_error", ["body", field])
        assert error["msg"] == f"Value error, {_refusal(field)}"
        assert await _assignments(sessions) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("field", FIELDS)
async def test_rest_update_refuses_a_value_longer_than_its_column(field):
    async with _temp_database("asg_len") as sessions:
        user, agent = await _seed(sessions)
        assignment_id = await _add_assignment(sessions, agent)
        async with _client(sessions, user) as client:
            response = await client.put(
                f"/api/v1/assignments/{assignment_id}",
                json={field: _too_long(field), "prompt": "Changed."},
            )

        assert response.status_code == 422, response.text
        [error] = response.json()["detail"]
        assert (error["type"], error["loc"]) == ("value_error", ["body", field])
        assert error["msg"] == f"Value error, {_refusal(field)}"
        [stored] = await _assignments(sessions)
        assert (stored.name, stored.trigger_type, stored.delay, stored.prompt) == (
            "Digest",
            "manual",
            None,
            "Go.",
        )


#: Values the columns hold, each accepted by every write path on ``main``. A
#: trigger type is not an allow-list: an empty or unknown one is saved as sent.
_FITTING = [
    ("trigger_type", "t" * _limit("trigger_type")),
    ("trigger_type", ""),
    ("trigger_type", "custom_trigger"),
    ("name", "n" * _limit("name")),
    ("name", ""),
    ("delay", "1" * (_limit("delay") - 1) + "m"),
    ("delay", ""),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("field", "value"), _FITTING)
async def test_rest_create_and_update_keep_every_value_the_column_holds(field, value):
    async with _temp_database("asg_len") as sessions:
        user, agent = await _seed(sessions)
        existing = await _add_assignment(sessions, agent)
        async with _client(sessions, user) as client:
            created = await client.post(
                "/api/v1/assignments", json=_create_body(agent, **{field: value})
            )
            updated = await client.put(f"/api/v1/assignments/{existing}", json={field: value})

        assert (created.status_code, updated.status_code) == (201, 200), (
            created.text,
            updated.text,
        )
        assert (created.json()[field], updated.json()[field]) == (value, value)
        assert [getattr(a, field) for a in await _assignments(sessions)] == [value, value]


# ---------------------------------------------------------------------------
# Agent import, every format
# ---------------------------------------------------------------------------


def _bundle(*entries: dict[str, str]) -> dict:
    return {
        "version": 3,
        "export_type": "agent",
        "agent": {"name": "Imported agent", "persona": "p"},
        "assignments": list(entries),
    }


def _entry(**fields: str) -> dict[str, str]:
    return {"name": "Digest", "trigger_type": "manual", "prompt": "Go.", **fields}


#: ``POST /agents/import`` (a JSON body), ``POST /agents/import-file`` with a
#: native bundle and with an AgentSchema document, ``POST /agents/import/confirm``.
IMPORT_ROUTES = ("json", "native_file", "agentschema_file", "confirm")


async def _import(client: httpx.AsyncClient, route: str, bundle: dict) -> httpx.Response:
    if route == "json":
        return await client.post("/api/v1/agents/import", json=bundle)
    if route == "native_file":
        upload = ("agent.json", json.dumps(bundle), "application/json")
        return await client.post("/api/v1/agents/import-file", files={"file": upload})
    if route == "agentschema_file":
        text, _document = build_agentschema_export(bundle)
        upload = ("agent.yaml", text, "application/yaml")
        return await client.post("/api/v1/agents/import-file", files={"file": upload})
    assert route == "confirm"
    return await client.post("/api/v1/agents/import/confirm", json={"bundle": bundle})


@pytest.mark.asyncio
@pytest.mark.parametrize("route", IMPORT_ROUTES)
@pytest.mark.parametrize("field", FIELDS)
async def test_import_refuses_a_value_longer_than_its_column_in_every_format(route, field):
    async with _temp_database("asg_len") as sessions:
        user, _agent = await _seed(sessions)
        async with _client(sessions, user) as client:
            response = await _import(client, route, _bundle(_entry(**{field: _too_long(field)})))

        assert response.status_code == 400, response.text
        # Named by its position when the name is what does not fit.
        where = "#1" if field == "name" else "'Digest'"
        assert response.json()["detail"] == f"Imported assignment {where}: {_refusal(field)}"
        assert await _agent_names(sessions) == ["Length agent"]
        assert await _assignments(sessions) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("route", IMPORT_ROUTES)
async def test_import_keeps_every_value_the_column_holds_in_every_format(route):
    entries = [
        _entry(
            name="n" * _limit("name"),
            trigger_type="t" * _limit("trigger_type"),
            delay="1" * (_limit("delay") - 1) + "m",
        ),
        _entry(name="Empty type", trigger_type=""),
        _entry(name="Unknown type", trigger_type="custom_trigger"),
    ]
    async with _temp_database("asg_len") as sessions:
        user, _agent = await _seed(sessions)
        async with _client(sessions, user) as client:
            response = await _import(client, route, _bundle(*entries))

        assert response.status_code == 201, response.text
        stored = {(a.name, a.trigger_type, a.delay) for a in await _assignments(sessions)}
        assert stored == {(e["name"], e["trigger_type"], e.get("delay")) for e in entries}


# ---------------------------------------------------------------------------
# Agent-builder tools
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["name", "delay"])
async def test_builder_create_assignment_refuses_a_value_longer_than_its_column(field):
    async with _temp_database("asg_len") as sessions:
        user, agent = await _seed(sessions)
        arguments = {"agent_id": str(agent.id), "name": "Digest", "prompt": "Go."}
        content = await _tool(user, "create_assignment", {**arguments, field: _too_long(field)})

        assert content == _refusal(field)
        assert await _assignments(sessions) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["name", "delay"])
async def test_builder_update_assignment_refuses_a_value_longer_than_its_column(field):
    async with _temp_database("asg_len") as sessions:
        user, agent = await _seed(sessions)
        assignment_id = await _add_assignment(sessions, agent)
        arguments = {"assignment_id": str(assignment_id), "prompt": "Changed."}
        content = await _tool(user, "update_assignment", {**arguments, field: _too_long(field)})

        assert content == _refusal(field)
        [stored] = await _assignments(sessions)
        assert (stored.name, stored.delay, stored.prompt) == ("Digest", None, "Go.")


@pytest.mark.asyncio
async def test_builder_tools_refuse_an_over_long_trigger_type_with_their_allow_list():
    """The tools admit four trigger types, all shorter than the column, so an
    over-long one is refused before any write - as on ``main``."""
    async with _temp_database("asg_len") as sessions:
        user, agent = await _seed(sessions)
        assignment_id = await _add_assignment(sessions, agent)
        created = await _tool(
            user,
            "create_assignment",
            {
                "agent_id": str(agent.id),
                "name": "Digest",
                "prompt": "Go.",
                "trigger_type": _too_long("trigger_type"),
            },
        )
        updated = await _tool(
            user,
            "update_assignment",
            {"assignment_id": str(assignment_id), "trigger_type": _too_long("trigger_type")},
        )

        for content in (created, updated):
            assert content.startswith("Invalid trigger_type: must be one of ")
        [stored] = await _assignments(sessions)
        assert stored.trigger_type == "manual"


@pytest.mark.asyncio
async def test_builder_tools_measure_the_value_they_store():
    """Both tools store ``name`` and ``delay`` stripped: a padded value that fits
    once stripped is saved, as on ``main``."""
    name = "n" * _limit("name")
    delay = "1" * (_limit("delay") - 1) + "m"
    async with _temp_database("asg_len") as sessions:
        user, agent = await _seed(sessions)
        existing = await _add_assignment(sessions, agent)
        created = await _tool(
            user,
            "create_assignment",
            {
                "agent_id": str(agent.id),
                "name": f"  {name}  ",
                "prompt": "Go.",
                "delay": f"  {delay}  ",
            },
        )
        updated = await _tool(
            user,
            "update_assignment",
            {"assignment_id": str(existing), "name": f"  {name}  ", "delay": f"  {delay}  "},
        )

        assert "added to agent" in created, created
        assert "updated" in updated, updated
        assert [(a.name, a.delay) for a in await _assignments(sessions)] == [(name, delay)] * 2


# ---------------------------------------------------------------------------
# Agent templates
# ---------------------------------------------------------------------------


async def _add_template(sessions: async_sessionmaker, *entries: dict[str, str]) -> str:
    slug = f"length-{uuid.uuid4().hex[:8]}"
    async with sessions() as db:
        db.add(AgentTemplate(slug=slug, name="Length template", default_assignments=list(entries)))
        await db.commit()
    return slug


async def _create_from_template(
    sessions: async_sessionmaker, user: User, slug: str, via: str
) -> None:
    if via == "rest":
        async with _client(sessions, user) as client:
            response = await client.post(
                "/api/v1/agents", json={"name": "From template", "template_slug": slug}
            )
        assert response.status_code == 201, response.text
    else:
        content = await _tool(
            user, "create_agent", {"name": "From template", "template_slug": slug}
        )
        assert "created successfully" in content, content


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("via", "missing_name"), [("rest", "Untitled Assignment"), ("builder", "Assignment")]
)
async def test_a_template_value_longer_than_its_column_is_read_as_absent(via, missing_name):
    """A template entry is admin-saved JSON, and one broken entry must not fail
    agent creation for every user: like a malformed ``trigger_config``, a value
    its column cannot hold is replaced by the one an absent key gets."""
    async with _temp_database("asg_len") as sessions:
        user, _agent = await _seed(sessions)
        slug = await _add_template(
            sessions,
            {
                "name": _too_long("name"),
                "trigger_type": _too_long("trigger_type"),
                "delay": _too_long("delay"),
                "prompt": "Go.",
            },
        )

        await _create_from_template(sessions, user, slug, via)

        [stored] = await _assignments(sessions)
        assert (stored.name, stored.trigger_type, stored.delay) == (missing_name, "manual", None)


@pytest.mark.asyncio
@pytest.mark.parametrize("via", ["rest", "builder"])
async def test_a_template_keeps_every_value_the_column_holds(via):
    entries = [
        _entry(
            name="n" * _limit("name"),
            trigger_type="t" * _limit("trigger_type"),
            delay="1" * (_limit("delay") - 1) + "m",
        ),
        _entry(name="Empty type", trigger_type=""),
        _entry(name="Unknown type", trigger_type="custom_trigger"),
    ]
    async with _temp_database("asg_len") as sessions:
        user, _agent = await _seed(sessions)
        slug = await _add_template(sessions, *entries)

        await _create_from_template(sessions, user, slug, via)

        stored = {(a.name, a.trigger_type, a.delay) for a in await _assignments(sessions)}
        assert stored == {(e["name"], e["trigger_type"], e.get("delay")) for e in entries}
