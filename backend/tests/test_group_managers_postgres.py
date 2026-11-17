"""Group managers against PostgreSQL, read as the non-admins they are.

Mia manages Customer Success (Alice, Bob and Ada, an administrator) without
being a member; Carol is in Sales only and manages nothing. Every read goes
through the HTTP routes the pages call, and through the platform insights tools
an agent calls. Mia's LLM Analytics cover exactly her team - the non-admin
members of the groups she manages: never Ada, never Carol, never the rest of a
member's memberships, never an agent's other users. Her team's traces and
sessions are never hers to list or open.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.models  # noqa: F401 - registers every mapper on Base.metadata
from app.api import deps
from app.api.v1 import groups as groups_api
from app.api.v1 import traces as traces_api
from app.core.database import Base, get_db
from app.models.agent import Agent
from app.models.audit_log import AuditLog
from app.models.chat import Conversation
from app.models.group import Group, GroupManager, GroupMember
from app.models.user import User
from tests.scratch_database import scratch_database

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

NOW = datetime.now(UTC)

#: (person, hours ago, cost, agent): the traces of the window, all live.
TRACES = [
    ("alice", 2, 0.10, "a"),
    ("alice", 30, 0.20, "b"),
    ("bob", 5, 0.40, "a"),
    ("carol", 3, 0.80, "a"),
    ("mia", 4, 1.60, "b"),
    (None, 1, 3.20, "a"),
    ("ada", 6, 6.40, "b"),
]

_TABLES = [table for table in Base.metadata.sorted_tables if table.name != "embeddings"]


def _cost(*people: str | None) -> float:
    return sum(cost for person, _h, cost, _a in TRACES if person in people)


@asynccontextmanager
async def _platform() -> AsyncIterator[tuple[async_sessionmaker, dict[str, User], dict[str, Group], dict]]:
    async with scratch_database("managers", tables=_TABLES, extensions=("vector",)) as db:
        async with db.engine.begin() as conn:
            await conn.execute(
                text("INSERT INTO platform_settings (id, created_at, updated_at) VALUES (:id, now(), now())"),
                {"id": str(uuid.uuid4())},
            )
        async with db.sessions() as session:
            people = {
                name: User(
                    email=f"{name}@example.test",
                    hashed_password="x",
                    full_name=name.title(),
                    is_active=True,
                    role="admin" if name in ("admin", "ada") else "user",
                )
                for name in ("admin", "ada", "mia", "alice", "bob", "carol")
            }
            session.add_all(people.values())
            await session.flush()
            success, sales = Group(name="Customer Success"), Group(name="Sales")
            session.add_all([success, sales])
            await session.flush()
            session.add_all(
                [
                    GroupMember(group_id=success.id, user_id=people["alice"].id),
                    GroupMember(group_id=success.id, user_id=people["bob"].id),
                    GroupMember(group_id=success.id, user_id=people["ada"].id),
                    GroupMember(group_id=sales.id, user_id=people["alice"].id),
                    GroupMember(group_id=sales.id, user_id=people["carol"].id),
                    GroupManager(group_id=success.id, user_id=people["mia"].id, assigned_by=people["admin"].id),
                ]
            )
            # Both agents belong to the admin: Mia can open neither's chats.
            agents = {key: Agent(name=f"Agent {key.upper()}", user_id=people["admin"].id) for key in ("a", "b")}
            session.add_all(agents.values())
            await session.flush()
            conversation = Conversation(user_id=people["alice"].id, agent_id=agents["a"].id)
            session.add(conversation)
            await session.flush()
            traces: dict[str, list[uuid.UUID]] = {}
            for index, (person, hours_ago, cost, agent) in enumerate(TRACES):
                trace_id = uuid.uuid4()
                traces.setdefault(person or "nobody", []).append(trace_id)
                await session.execute(
                    text(
                        "INSERT INTO llm_traces (id, source, name, tags, started_at, status, level, model, user_id, "
                        "agent_id, conversation_id, input, output, input_tokens, output_tokens, total_tokens, "
                        "total_cost, duration_ms, error_message) VALUES (:id, 'agent-chat', 'turn', :tags, "
                        ":started_at, :status, 'DEFAULT', 'model-one', :user_id, :agent_id, :conversation_id, "
                        "CAST(:input AS json), CAST(:output AS json), 10, 20, 30, :cost, 150, :error)"
                    ),
                    {
                        "id": trace_id,
                        "tags": ["source:agent-chat"],
                        "started_at": NOW - timedelta(hours=hours_ago),
                        "status": "error" if person == "bob" else "ok",
                        "user_id": people[person].id if person else None,
                        "agent_id": agents[agent].id,
                        "conversation_id": conversation.id if index == 0 else None,
                        "input": json.dumps({"messages": [f"secret question of {person}"]}),
                        "output": json.dumps({"text": f"secret answer to {person}"}),
                        "cost": cost,
                        "error": "provider timeout" if person == "bob" else None,
                    },
                )
            await session.commit()
            yield db.sessions, people, {"success": success, "sales": sales}, {
                "traces": traces,
                "conversation_id": conversation.id,
                "agents": {key: agent.id for key, agent in agents.items()},
            }


@asynccontextmanager
async def _client(sessions: async_sessionmaker, user: User) -> AsyncIterator[httpx.AsyncClient]:
    app = FastAPI()
    app.include_router(traces_api.router, prefix="/api/v1/traces")
    app.include_router(groups_api.router, prefix="/api/v1/groups")

    async def _get_db():
        async with sessions() as db:
            yield db
            await db.commit()

    app.dependency_overrides[deps.get_current_user] = lambda: user
    app.dependency_overrides[get_db] = _get_db
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client


async def _call(user: User, tool: str, **args) -> dict | str:
    from app.services.builtin_tools import build_platform_insights_tools

    _defs, executor = build_platform_insights_tools(str(user.id), chart_sink=[])
    raw = await executor.handler(tool)(**args)
    try:
        return json.loads(raw)
    except ValueError:
        return raw


async def test_a_manager_reads_the_analytics_of_their_groups_non_admin_members_only():
    async with _platform() as (sessions, people, groups, _seed), _client(sessions, people["mia"]) as client:
        summary = (await client.get("/api/v1/traces/analytics/summary", params={"period": "7d"})).json()
        # Ada is in Customer Success, but an administrator is no one's team.
        assert summary["trace_count"] == 3
        assert summary["total_cost"] == pytest.approx(_cost("alice", "bob"))
        ada = await client.get(
            "/api/v1/traces/analytics/summary", params={"period": "7d", "user_ids": str(people["ada"].id)}
        )
        assert ada.json()["trace_count"] == 0

        named = await client.get(
            "/api/v1/traces/analytics/summary", params={"period": "7d", "group_ids": str(groups["success"].id)}
        )
        assert named.json()["total_cost"] == pytest.approx(_cost("alice", "bob"))

        by_agent = (await client.get("/api/v1/traces/analytics/cost-by-agent", params={"period": "7d"})).json()
        assert sum(item["total_cost"] for item in by_agent["items"]) == pytest.approx(_cost("alice", "bob"))
        assert {item["agent_name"] for item in by_agent["items"]} == {"Agent A", "Agent B"}
        # Whose each agent is: the admin's, whoever on the team ran it.
        assert {(item["owner_email"], item["owner_name"], item["is_company_managed"]) for item in by_agent["items"]} == {
            ("admin@example.test", "Admin", False)
        }
        # Neither agent is hers to open: the rows are figures only.
        assert {(item["can_open"], item["agent_url"], item["agent_status"]) for item in by_agent["items"]} == {
            (False, None, "active")
        }

        by_user = (await client.get("/api/v1/traces/analytics/cost-by-user", params={"period": "7d"})).json()
        assert {item["user_email"] for item in by_user["items"]} == {"alice@example.test", "bob@example.test"}
        # Alice is in Sales too: her manager is never told.
        assert {name for item in by_user["items"] for name in item["group_names"]} == {"Customer Success"}
        assert by_user["unattributed"] is None

        by_group = (await client.get("/api/v1/traces/analytics/cost-by-group", params={"period": "7d"})).json()
        assert [(item["group_name"], item["member_count"], item["active_user_count"]) for item in by_group["items"]] == [
            ("Customer Success", 2, 2)
        ]

        errors = (await client.get("/api/v1/traces/analytics/errors", params={"period": "7d", "limit": 100})).json()
        assert [item["error_message"] for item in errors["items"]] == ["provider timeout"]

        year = await client.get("/api/v1/traces/analytics/summary", params={"period": "1y"})
        assert year.status_code == 200 and year.json()["trace_count"] == 3

        sales = await client.get(
            "/api/v1/traces/analytics/summary", params={"period": "7d", "group_ids": str(groups["sales"].id)}
        )
        assert sales.status_code == 403


async def test_someone_who_manages_nothing_keeps_the_agent_scoped_rule():
    async with _platform() as (sessions, people, _groups, _seed), _client(sessions, people["carol"]) as client:
        assert (await client.get("/api/v1/traces/analytics/summary")).status_code == 403
        assert (await client.get("/api/v1/traces/analytics/cost-by-user")).status_code == 403
        assert (await client.get("/api/v1/traces")).status_code == 403
        assert (await client.get("/api/v1/groups/managed")).json() == []


async def test_a_manager_lists_and_opens_none_of_their_teams_traces_or_sessions():
    async with _platform() as (sessions, people, groups, seed), _client(sessions, people["mia"]) as client:
        success = str(groups["success"].id)
        # Traces and Sessions are an administrator's pages: managing a group adds nothing.
        assert (await client.get("/api/v1/traces")).status_code == 403
        assert (await client.get("/api/v1/traces/sessions")).status_code == 403
        assert (await client.get("/api/v1/traces", params={"group_id": success})).status_code == 403
        assert (await client.get("/api/v1/traces/sessions", params={"group_id": success})).status_code == 403

        alices = seed["traces"]["alice"][0]
        for path in (f"/api/v1/traces/{alices}", f"/api/v1/traces/{alices}/spans", f"/api/v1/traces/{alices}/payload"):
            assert (await client.get(path)).status_code == 404, path
        explorer = (await client.get(f"/api/v1/traces/sessions/{seed['conversation_id']}/explorer")).json()
        assert explorer["traces"] == [] and "secret" not in json.dumps(explorer)

        async with sessions() as session:
            reads = (
                await session.execute(
                    select(AuditLog).where(AuditLog.user_id == people["mia"].id, AuditLog.action == "break_glass")
                )
            ).scalars().all()
        assert reads == []


async def test_the_top_agents_rows_open_where_the_caller_may_open_the_agent():
    """An admin opens every agent, a manager the ones shared with her only."""
    async with _platform() as (sessions, people, _groups, seed):
        async with _client(sessions, people["admin"]) as client:
            rows = (await client.get("/api/v1/traces/analytics/cost-by-agent", params={"period": "7d"})).json()["items"]
        assert {(item["agent_id"], item["can_open"], item["agent_url"]) for item in rows} == {
            (str(agent), True, f"/agents/{agent}") for agent in seed["agents"].values()
        }

        async with sessions() as session:
            shared = await session.get(Agent, seed["agents"]["b"])
            shared.shared_user_ids = [str(people["mia"].id)]
            await session.commit()
        async with _client(sessions, people["mia"]) as client:
            rows = (await client.get("/api/v1/traces/analytics/cost-by-agent", params={"period": "7d"})).json()["items"]
        assert {item["agent_name"]: item["can_open"] for item in rows} == {"Agent A": False, "Agent B": True}


async def test_a_deleted_agent_row_reads_its_tombstone_and_never_opens():
    """A deleted agent keeps its last known name, an unknown id stays inert."""
    from app.models.agent_tombstone import AgentTombstone
    from app.services.tracing.read_store import agent_row_access

    async with _platform() as (sessions, people, _groups, seed):
        deleted, unknown = uuid.uuid4(), uuid.uuid4()
        async with sessions() as session:
            session.add(AgentTombstone(agent_id=deleted, name="Old Bot", user_id=people["admin"].id))
            await session.commit()
            access = await agent_row_access(
                session, [deleted, unknown, seed["agents"]["a"]], user=people["admin"], user_group_ids=[]
            )
        assert access[str(deleted)] == {"agent_status": "deleted", "can_open": False, "last_known_name": "Old Bot"}
        assert access[str(unknown)] == {"agent_status": "deleted", "can_open": False, "last_known_name": None}
        assert access[str(seed["agents"]["a"])]["can_open"] is True


async def test_an_admins_group_filter_counts_every_member():
    async with _platform() as (sessions, people, groups, _seed), _client(sessions, people["admin"]) as client:
        success = str(groups["success"].id)
        summary = await client.get("/api/v1/traces/analytics/summary", params={"period": "7d", "group_ids": success})
        assert summary.json()["total_cost"] == pytest.approx(_cost("alice", "bob", "ada"))
        by_group = await client.get("/api/v1/traces/analytics/cost-by-group", params={"period": "7d", "group_ids": success})
        assert [(item["group_name"], item["member_count"]) for item in by_group.json()["items"]] == [
            ("Customer Success", 3)
        ]

        listing = (await client.get("/api/v1/traces", params={"group_id": success, "limit": 50})).json()
        assert {item["user_email"] for item in listing["items"]} == {
            "alice@example.test",
            "bob@example.test",
            "ada@example.test",
        }
        sessions_listing = (await client.get("/api/v1/traces/sessions", params={"group_id": success})).json()
        assert [item["user_email"] for item in sessions_listing["items"]] == ["alice@example.test"]


async def test_a_manager_asks_an_agent_for_their_teams_usage():
    async with _platform() as (_sessions, people, _groups, seed):
        mia = people["mia"]
        by_agent = await _call(mia, "query_platform_analytics", metric="cost", group_by="agent", period="30d")
        assert by_agent["scope"] == "team" and "Customer Success" in by_agent["scope_note"]
        # Her own usage and her team's, never Ada's, Carol's nor the unattributed traces.
        assert by_agent["totals"]["total_cost"] == pytest.approx(_cost("alice", "bob", "mia"))
        assert {row["dimension_id"] for row in by_agent["rows"]} == {str(agent) for agent in seed["agents"].values()}
        assert {row["owner_email"] for row in by_agent["rows"]} == {"admin@example.test"}

        team_only = await _call(mia, "query_platform_analytics", group_ids=["Customer Success"], period="30d")
        assert team_only["totals"]["total_cost"] == pytest.approx(_cost("alice", "bob"))

        by_user = await _call(mia, "query_platform_analytics", group_by="user", period="30d")
        assert {row["dimension"] for row in by_user["rows"]} == {
            "alice@example.test",
            "bob@example.test",
            "mia@example.test",
        }
        assert {name for row in by_user["rows"] for name in row["groups"]} == {"Customer Success"}

        by_group = await _call(mia, "query_platform_analytics", group_by="group", period="30d")
        assert [(row["dimension"], row["member_count"]) for row in by_group["rows"]] == [("Customer Success", 2)]

        assert "groups you manage" in await _call(mia, "query_platform_analytics", group_ids=["Sales"])
        assert "only name yourself" in await _call(mia, "query_platform_analytics", user_ids=["carol@example.test"])
        assert "only name yourself" in await _call(mia, "query_platform_analytics", user_ids=["ada@example.test"])

        # The team reaches the analytics alone: the listings serve Mia her own slice.
        traces = await _call(mia, "list_platform_traces", period="7d", status="error")
        assert traces["scope"] == "self" and traces["rows"] == []
        everything = await _call(mia, "list_platform_traces", period="7d", limit=100)
        assert everything["total"] == 1
        assert {row["user_email"] for row in everything["rows"]} == {"mia@example.test"}
        assert "own groups" in await _call(mia, "list_platform_traces", group_ids=["Customer Success"])
        assert "only name yourself" in await _call(mia, "list_platform_traces", user_ids=["alice@example.test"])

        team_sessions = await _call(mia, "list_platform_sessions", period="7d")
        assert team_sessions["scope"] == "self" and team_sessions["rows"] == []

        carol = await _call(people["carol"], "list_platform_traces", period="7d")
        assert carol["scope"] == "self" and {row["user_email"] for row in carol["rows"]} == {"carol@example.test"}

        admin = await _call(people["admin"], "list_platform_traces", period="7d", group_ids=["Sales"])
        assert admin["scope"] == "platform"
        assert {row["user_email"] for row in admin["rows"]} == {"alice@example.test", "carol@example.test"}


async def test_an_admin_elects_and_removes_a_groups_managers():
    async with _platform() as (sessions, people, groups, _seed), _client(sessions, people["admin"]) as client:
        sales = groups["sales"].id
        added = await client.post(
            f"/api/v1/groups/{sales}/managers",
            json={"user_ids": [str(people["bob"].id), str(people["carol"].id), "not-an-id"]},
        )
        assert added.status_code == 200
        assert {(item["email"], item["is_member"]) for item in added.json()} == {
            ("bob@example.test", False),
            ("carol@example.test", True),
        }
        again = await client.post(f"/api/v1/groups/{sales}/managers", json={"user_ids": [str(people["bob"].id)]})
        assert again.json() == []

        listed = (await client.get(f"/api/v1/groups/{sales}/managers")).json()
        assert [item["email"] for item in listed] == ["bob@example.test", "carol@example.test"]
        members = (await client.get(f"/api/v1/groups/{sales}/members")).json()["items"]
        assert {item["email"]: item["is_manager"] for item in members} == {
            "alice@example.test": False,
            "carol@example.test": True,
        }

        removed = await client.delete(f"/api/v1/groups/{sales}/managers/{people['bob'].id}")
        assert removed.status_code == 204
        assert (await client.delete(f"/api/v1/groups/{sales}/managers/{people['bob'].id}")).status_code == 404

    async with _platform() as (sessions, people, groups, _seed), _client(sessions, people["mia"]) as client:
        assert (await client.get("/api/v1/groups/managed")).json() == [
            {"id": str(groups["success"].id), "name": "Customer Success", "member_count": 2}
        ]
        refused = await client.post(
            f"/api/v1/groups/{groups['sales'].id}/managers", json={"user_ids": [str(people["mia"].id)]}
        )
        assert refused.status_code == 403
