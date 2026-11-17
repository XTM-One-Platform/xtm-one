"""`POST /flows/output-schemas`: what each node of a draft graph declares.

Through the real route, against a real PostgreSQL: agents and event types are
read with the caller's visibility, a wired Structured output replaces the
agent's own schema, and only a JSON answer whose schema is an object
declares `fields`.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.api.deps import get_current_user
from app.api.v1 import flows as api
from app.core.database import get_db
from app.models.agent import Agent
from app.models.flow_event import FlowEventType
from app.models.user import User
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration

SEVERITY = {"type": "object", "properties": {"severity": {"type": "string"}}}
WIRED = {"type": "object", "properties": {"verdict": {"type": "boolean"}}}


class Caller:
    def __init__(self) -> None:
        self.user: User | None = None


@asynccontextmanager
async def _client(session_factory: async_sessionmaker, caller: Caller) -> AsyncIterator[httpx.AsyncClient]:
    app = FastAPI()
    app.include_router(api.router, prefix="/api/v1/flows")

    async def _get_db():
        async with session_factory() as db:
            yield db

    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[get_current_user] = lambda: caller.user
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client


async def _user(db, name: str, role: str = "user") -> User:
    user = User(email=f"{name}-{uuid.uuid4().hex[:6]}@example.com", hashed_password="x", full_name=name, role=role)
    db.add(user)
    await db.flush()
    return user


async def _agent(db, owner: User | None, *, output_format="json", schema=SEVERITY, **visibility) -> Agent:
    agent = Agent(
        name=f"agent-{uuid.uuid4().hex[:6]}",
        slug=f"a-{uuid.uuid4().hex[:8]}",
        user_id=owner.id if owner else None,
        output_format=output_format,
        output_schema=schema,
        **visibility,
    )
    db.add(agent)
    await db.flush()
    return agent


def _node(node_id: str, kind: str, **config) -> dict:
    return {"id": node_id, "kind": kind, "position": {"x": 0, "y": 0}, "config": config}


def _edge(source: str, target: str, target_handle: str = "in") -> dict:
    return {
        "id": f"{source}-{target}",
        "source": source,
        "source_handle": "provides" if target_handle != "in" else "out",
        "target": target,
        "target_handle": target_handle,
        "kind": "capability" if target_handle != "in" else "data",
    }


async def _declared(client: httpx.AsyncClient, nodes: list, edges: list | None = None) -> dict:
    response = await client.post(
        "/api/v1/flows/output-schemas",
        json={"graph": {"nodes": nodes, "edges": edges or [], "viewport": {"x": 0, "y": 0, "zoom": 1}}},
    )
    assert response.status_code == 200, response.text
    return response.json()["nodes"]


@pytest.mark.asyncio
async def test_an_agent_declares_fields_only_where_the_caller_may_read_it():
    async with scratch_database("outschemas", tables=ALL_TABLES, extensions=("vector",)) as scratch:
        async with scratch.sessions() as db:
            caller_user = await _user(db, "caller")
            other = await _user(db, "other")
            admin = await _user(db, "admin", role="admin")
            own = await _agent(db, caller_user)
            private = await _agent(db, other)
            shared = await _agent(db, other, shared_user_ids=[str(caller_user.id)])
            company = await _agent(db, None, is_company_managed=True)
            markdown = await _agent(db, caller_user, output_format="markdown")
            list_root = await _agent(db, caller_user, schema={"type": "array"})
            await db.commit()

        nodes = [
            _node("own", "agent", agent_id=str(own.id)),
            _node("private", "agent", agent_id=str(private.id)),
            _node("shared", "agent", agent_id=str(shared.id)),
            _node("company", "agent", agent_id=str(company.id)),
            _node("markdown", "agent", agent_id=str(markdown.id)),
            _node("list_root", "agent", agent_id=str(list_root.id)),
        ]
        caller = Caller()
        async with _client(scratch.sessions, caller) as client:
            caller.user = caller_user
            assert await _declared(client, nodes) == {
                "own": {"fields": SEVERITY},
                "shared": {"fields": SEVERITY},
                "company": {"fields": SEVERITY},
            }, "a private agent of someone else leaks nothing; markdown and list answers have no fields"

            caller.user = admin
            assert set(await _declared(client, nodes)) == {"own", "private", "shared", "company"}


@pytest.mark.asyncio
async def test_a_wired_structured_output_replaces_the_agents_schema():
    async with scratch_database("outschemas", tables=ALL_TABLES, extensions=("vector",)) as scratch:
        async with scratch.sessions() as db:
            caller_user = await _user(db, "caller")
            other = await _user(db, "other")
            json_agent = await _agent(db, caller_user)
            markdown = await _agent(db, caller_user, output_format="markdown")
            private = await _agent(db, other)
            await db.commit()

        nodes = [
            _node("so", "capability.structured_output", schema=WIRED),
            _node("json_agent", "agent", agent_id=str(json_agent.id)),
            _node("markdown", "agent", agent_id=str(markdown.id)),
            # The wired schema is the graph's own: it applies whoever owns the agent.
            _node("private", "agent", agent_id=str(private.id)),
            _node("no_agent", "agent"),
        ]
        # A malformed edge ahead of the real ones is skipped, not fatal.
        edges = [{"source": ["so"], "target": "json_agent", "target_handle": "output"}] + [
            _edge("so", n, "output") for n in ("json_agent", "markdown", "private", "no_agent")
        ]
        caller = Caller()
        caller.user = caller_user
        async with _client(scratch.sessions, caller) as client:
            assert await _declared(client, nodes, edges) == {
                "json_agent": {"fields": WIRED},
                "markdown": {"fields": WIRED},
                "private": {"fields": WIRED},
                "no_agent": {"fields": WIRED},
            }


@pytest.mark.asyncio
async def test_trigger_parameters_and_visible_event_types_are_declared():
    async with scratch_database("outschemas", tables=ALL_TABLES, extensions=("vector",)) as scratch:
        async with scratch.sessions() as db:
            caller_user = await _user(db, "caller")
            other = await _user(db, "other")
            fields = [
                {"name": "verdict", "type": "boolean", "required": True, "description": "Go or no go"},
                {"name": "raw", "type": "json", "required": False},
            ]
            own_type = FlowEventType(user_id=caller_user.id, name="own.verdict", fields=fields)
            other_type = FlowEventType(user_id=other.id, name="other.verdict", fields=fields)
            db.add_all([own_type, other_type])
            await db.commit()

        nodes = [
            _node(
                "t1",
                "trigger.manual",
                parameters=[
                    {"name": "ticket", "type": "string", "description": "The ticket id"},
                    {"name": "limit", "type": "number"},
                    {"name": "extra", "type": "json"},
                ],
            ),
            _node("w1", "util.wait_event", event_type_id=str(own_type.id)),
            _node("e1", "trigger.event", event_type_id=str(other_type.id)),
        ]
        caller = Caller()
        caller.user = caller_user
        async with _client(scratch.sessions, caller) as client:
            assert await _declared(client, nodes) == {
                "t1": {
                    "payload": {
                        "type": "object",
                        "properties": {
                            "ticket": {"type": "string", "description": "The ticket id"},
                            "limit": {"type": "number"},
                            "extra": {},
                        },
                    }
                },
                "w1": {
                    "fields": {
                        "type": "object",
                        "properties": {"verdict": {"type": "boolean", "description": "Go or no go"}, "raw": {}},
                    }
                },
            }, "another user's private event type declares nothing"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "graph",
    [
        {"nodes": "not a list", "edges": 3},
        {"nodes": [1, "x", {"kind": "agent"}, {"id": "a1", "kind": "agent", "config": "text"}], "edges": ["e"]},
        {"nodes": [{"id": "a1", "kind": "agent", "config": {"agent_id": "not-a-uuid"}}]},
        {},
        # Edge ends and handles that are not strings, into an agent node.
        {"nodes": [{"id": "a1", "kind": "agent"}], "edges": [{"source": ["x"], "target": "a1", "target_handle": {"y": 1}}]},
        {"nodes": [{"id": "a1", "kind": "agent"}], "edges": [{"source": {"x": 1}, "target": "a1", "target_handle": "output"}]},
        {
            "nodes": [{"id": "so", "kind": "capability.structured_output", "config": {"schema": WIRED}}, {"id": "a1", "kind": "agent"}],
            "edges": [{"source": "so", "target": "a1", "target_handle": ["output"]}],
        },
        {"nodes": [{"id": "a1", "kind": "agent"}], "edges": [{"source": "a1", "target": ["a1"]}]},
        # Node kinds that are not strings, alone and as a capability source.
        {"nodes": [{"id": "e1", "kind": ["trigger.event"], "config": {"event_type_id": str(uuid.uuid4())}}]},
        {
            "nodes": [{"id": "c1", "kind": {"k": 1}}, {"id": "a1", "kind": "agent"}],
            "edges": [{"source": "c1", "target": "a1", "target_handle": "tools"}],
        },
        # The largest draft a save accepts is answered.
        {"nodes": [{"id": f"n{i}"} for i in range(200)], "edges": [{} for _ in range(1000)]},
    ],
)
async def test_a_malformed_draft_declares_nothing_rather_than_failing(graph):
    async with scratch_database("outschemas", tables=ALL_TABLES, extensions=("vector",)) as scratch:
        async with scratch.sessions() as db:
            caller_user = await _user(db, "caller")
            await db.commit()
        caller = Caller()
        caller.user = caller_user
        async with _client(scratch.sessions, caller) as client:
            response = await client.post("/api/v1/flows/output-schemas", json={"graph": graph})
            assert response.status_code == 200, response.text
            assert response.json() == {"nodes": {}}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {},
        {"graph": "x"},
        {"graph": {"nodes": [{"id": f"n{i}"} for i in range(201)]}},
        {"graph": {"nodes": [], "edges": [{} for _ in range(1001)]}},
    ],
)
async def test_an_invalid_body_is_a_422(body):
    async with scratch_database("outschemas", tables=ALL_TABLES, extensions=("vector",)) as scratch:
        async with scratch.sessions() as db:
            caller_user = await _user(db, "caller")
            await db.commit()
        caller = Caller()
        caller.user = caller_user
        async with _client(scratch.sessions, caller) as client:
            response = await client.post("/api/v1/flows/output-schemas", json=body)
            assert response.status_code == 422
