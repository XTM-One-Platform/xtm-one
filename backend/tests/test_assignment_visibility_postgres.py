"""Assignment visibility per tier, against real PostgreSQL.

An assignment is visible exactly when its parent agent is, so the predicate is
``user_visible_filter(Agent)``: a JSONB ``?|`` over ``shared_user_ids`` and
``shared_group_ids`` plus the ``access_restricted`` narrowing of the
company-wide tier. The unit tests compile that SQL; only a real database says
which rows it admits. Every REST route and agent-builder tool that reads or
writes an assignment is driven here as each NON-ADMIN tier - the agent's owner,
a group granted read-only, a group granted config, a user granted read-only, a
user granted config, a member of a restricted company-managed agent's audience,
a stranger - and as an admin, whose bypass short-circuits the predicate and so
proves nothing about the others.

``integration``-marked and skips when PostgreSQL is unreachable, like its
neighbours; the temporary database is the shared ``tests/scratch_database.py``.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.api import deps
from app.api.v1 import assignments as assignments_api
from app.core.database import get_db
from app.models.agent import Agent
from app.models.assignment import Assignment, AssignmentRun
from app.models.group import Group, GroupMember
from app.models.user import User
from app.services import assignment_engine
from app.services.builtin_tools import build_agent_builder_tool
from app.tools.base import ToolCall
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration

# The non-admin tiers, by the grant that reaches them.
PERSONAS = (
    "owner",
    "group_ro",
    "group_config",
    "user_ro",
    "user_config",
    "audience",
    "stranger",
)

# The seeded assignments each persona can see: those of the agents it can see.
# ``private`` is the owner's agent shared with both groups and both users,
# ``company`` is company-managed, ``restricted`` is company-managed narrowed to
# ``audience``, ``strangers`` is the stranger's private agent.
VISIBLE = {
    "owner": {"private", "company"},
    "group_ro": {"private", "company"},
    "group_config": {"private", "company"},
    "user_ro": {"private", "company"},
    "user_config": {"private", "company"},
    "audience": {"company", "restricted"},
    "stranger": {"company", "strangers"},
}

# Who may manage the private agent, and so edit, delete and purge the runs of
# its assignments.
MANAGERS = {"owner", "group_config", "user_config"}


@asynccontextmanager
async def _temp_database(
    prefix: str,
) -> AsyncIterator[tuple[async_sessionmaker, AsyncEngine]]:
    """Isolated database with the full schema, dropped on exit."""
    async with scratch_database(
        prefix, tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True
    ) as db:
        yield db.sessions, db.engine


@dataclass
class World:
    users: dict[str, User]
    agents: dict[str, Agent]
    assignments: dict[str, Assignment]

    def key_of(self, assignment_id: str) -> str:
        return next(
            key for key, a in self.assignments.items() if str(a.id) == assignment_id
        )


async def _seed(session_factory: async_sessionmaker) -> World:
    async with session_factory() as db:
        users = {
            name: User(
                email=f"{name}-{uuid.uuid4().hex[:8]}@example.com",
                hashed_password="x",
                full_name=name,
                role="admin" if name == "admin" else "user",
            )
            for name in (*PERSONAS, "admin")
        }
        db.add_all(users.values())
        await db.flush()

        read_only_group = Group(name=f"ro-{uuid.uuid4().hex[:8]}")
        config_group = Group(name=f"config-{uuid.uuid4().hex[:8]}")
        db.add_all([read_only_group, config_group])
        await db.flush()
        db.add_all(
            [
                GroupMember(group_id=read_only_group.id, user_id=users["group_ro"].id),
                GroupMember(group_id=config_group.id, user_id=users["group_config"].id),
            ]
        )

        def uid(name: str) -> str:
            return str(users[name].id)

        agents = {
            "private": Agent(
                name="Private agent",
                slug=f"private-{uuid.uuid4().hex[:8]}",
                persona="p",
                user_id=users["owner"].id,
                shared_group_ids=[str(read_only_group.id), str(config_group.id)],
                shared_group_permissions={
                    str(read_only_group.id): "read_only",
                    str(config_group.id): "config",
                },
                shared_user_ids=[uid("user_ro"), uid("user_config")],
                shared_user_permissions={
                    uid("user_ro"): "read_only",
                    uid("user_config"): "config",
                },
            ),
            "company": Agent(
                name="Company agent",
                slug=f"company-{uuid.uuid4().hex[:8]}",
                persona="p",
                user_id=users["admin"].id,
                is_company_managed=True,
            ),
            "restricted": Agent(
                name="Restricted agent",
                slug=f"restricted-{uuid.uuid4().hex[:8]}",
                persona="p",
                user_id=users["admin"].id,
                is_company_managed=True,
                access_restricted=True,
                shared_user_ids=[uid("audience")],
                shared_user_permissions={uid("audience"): "read_only"},
            ),
            "strangers": Agent(
                name="Stranger's agent",
                slug=f"strangers-{uuid.uuid4().hex[:8]}",
                persona="p",
                user_id=users["stranger"].id,
            ),
        }
        db.add_all(agents.values())
        await db.flush()

        assignments = {
            key: _assignment(agent, key)
            for key, agent in agents.items()
        }
        # Per-user runs: the trigger gate lets a non-admin run it, which is how
        # a user outside the audience could trigger it.
        assignments["restricted"].hide_runs = True
        db.add_all(assignments.values())
        await db.commit()
    return World(users=users, agents=agents, assignments=assignments)


def _assignment(agent: Agent, name: str, **overrides) -> Assignment:
    fields = {
        "agent_id": agent.id,
        # Owner and visibility mirror the parent agent, as every creation path
        # writes them.
        "user_id": agent.user_id,
        "is_company_managed": agent.is_company_managed,
        "name": f"A-{name}",
        "trigger_type": "manual",
        "trigger_config": {},
        "prompt": "Do the thing.",
        "enabled": True,
    }
    fields.update(overrides)
    return Assignment(**fields)


async def _add_assignment(
    session_factory: async_sessionmaker, world: World, key: str, *, runs_by=()
) -> Assignment:
    """A fresh copy of the seeded assignment *key*, with a run per name in *runs_by*."""
    agent = world.agents[key]
    template = world.assignments[key]
    copy = _assignment(agent, f"{key}-{uuid.uuid4().hex[:6]}", hide_runs=template.hide_runs)
    async with session_factory() as db:
        db.add(copy)
        await db.flush()
        for name in runs_by:
            db.add(
                AssignmentRun(
                    assignment_id=copy.id,
                    agent_id=agent.id,
                    user_id=world.users[name].id,
                    is_company_managed=False,
                    status="completed",
                )
            )
        await db.commit()
    return copy


async def _count_runs(session_factory: async_sessionmaker, assignment_id) -> int:
    async with session_factory() as db:
        return (
            await db.execute(
                select(func.count())
                .select_from(AssignmentRun)
                .where(AssignmentRun.assignment_id == assignment_id)
            )
        ).scalar_one()


async def _exists(session_factory: async_sessionmaker, assignment_id) -> bool:
    async with session_factory() as db:
        return (
            await db.execute(select(Assignment.id).where(Assignment.id == assignment_id))
        ).scalar_one_or_none() is not None


@asynccontextmanager
async def _client(
    session_factory: async_sessionmaker, user: User
) -> AsyncIterator[httpx.AsyncClient]:
    """The real assignments router, as *user*, on its own session per request."""
    app = FastAPI()
    app.include_router(assignments_api.router, prefix="/api/v1/assignments")

    async def _get_db():
        async with session_factory() as db:
            yield db
            await db.commit()

    app.dependency_overrides[deps.get_current_user] = lambda: user
    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[deps.require_agentic_ai] = lambda: None
    app.dependency_overrides[deps.require_ai_enabled] = lambda: None
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest.fixture
def engine_triggers(monkeypatch) -> list[tuple[str, uuid.UUID]]:
    """Replace the assignment engine's trigger with a recorded, completed run."""
    calls: list[tuple[str, uuid.UUID]] = []

    async def _trigger(*, assignment, agent, payload, db, trigger_context, triggered_by_user_id=None, **_):
        run = AssignmentRun(
            assignment_id=assignment.id,
            agent_id=agent.id,
            user_id=triggered_by_user_id,
            is_company_managed=False,
            status="completed",
            trigger_payload=payload,
            trigger_context=trigger_context,
        )
        db.add(run)
        await db.flush()
        calls.append((assignment.name, triggered_by_user_id))
        return run.id

    monkeypatch.setattr(assignment_engine, "trigger_assignment", _trigger)
    return calls


async def _tool(user: User | str, tool: str, /, **arguments: str) -> str:
    """Run a builder tool as *user*, or as the raw user id an anonymous
    channel turn hands the builder (``"anonymous"``)."""
    caller = user if isinstance(user, str) else str(user.id)
    _, executor = build_agent_builder_tool(None, caller)
    result = await executor.execute_tool(
        ToolCall(id=str(uuid.uuid4()), name=tool, arguments=arguments)
    )
    return result.content


def _write_status(persona: str, key: str, allowed: int) -> int:
    """What an edit / delete / purge answers: 404 when the assignment is not
    visible, 403 when it is visible but not manageable."""
    if key not in VISIBLE[persona]:
        return 404
    if key == "private":
        return allowed if persona in MANAGERS else 403
    if key == "strangers":
        return allowed
    return 403  # company-managed: admins only


def _trigger_status(persona: str, key: str) -> int:
    if key not in VISIBLE[persona]:
        return 404
    # Company-managed and not per-user: admins only. ``restricted`` runs per
    # user, so its audience may run it; personal assignments run as the caller.
    return 403 if key == "company" else 200


# ---------------------------------------------------------------------------
# REST
# ---------------------------------------------------------------------------


async def test_the_list_across_agents_holds_what_the_caller_can_see():
    async with _temp_database("asgvis_list") as (session_factory, _engine):
        world = await _seed(session_factory)
        for persona in PERSONAS:
            async with _client(session_factory, world.users[persona]) as client:
                resp = await client.get("/api/v1/assignments")
            assert resp.status_code == 200, (persona, resp.text)
            listed = {world.key_of(row["id"]) for row in resp.json()}
            assert listed == VISIBLE[persona], persona

        # An admin reads the whole platform across agents, colleagues' private
        # assignments included.
        async with _client(session_factory, world.users["admin"]) as client:
            resp = await client.get("/api/v1/assignments")
        assert {world.key_of(row["id"]) for row in resp.json()} == set(world.assignments)


async def test_one_agent_s_list_is_three_tier_and_keeps_the_admin_bypass():
    async with _temp_database("asgvis_agent") as (session_factory, _engine):
        world = await _seed(session_factory)
        path = f"/api/v1/assignments?agent_id={world.agents['private'].id}"
        for persona in PERSONAS:
            async with _client(session_factory, world.users[persona]) as client:
                resp = await client.get(path)
            assert resp.status_code == 200, (persona, resp.text)
            listed = {world.key_of(row["id"]) for row in resp.json()}
            assert listed == ({"private"} & VISIBLE[persona]), persona

        # Settings > Objects opens a colleague's agent for an admin.
        async with _client(session_factory, world.users["admin"]) as client:
            resp = await client.get(path)
        assert {world.key_of(row["id"]) for row in resp.json()} == {"private"}


async def test_the_detail_and_its_runs_answer_404_for_an_invisible_assignment():
    async with _temp_database("asgvis_detail") as (session_factory, _engine):
        world = await _seed(session_factory)
        for persona in PERSONAS:
            async with _client(session_factory, world.users[persona]) as client:
                for key, assignment in world.assignments.items():
                    expected = 200 if key in VISIBLE[persona] else 404
                    resp = await client.get(f"/api/v1/assignments/{assignment.id}")
                    assert resp.status_code == expected, (persona, key, resp.text)
                    resp = await client.get(f"/api/v1/assignments/{assignment.id}/runs")
                    assert resp.status_code == expected, (persona, key, resp.text)

        async with _client(session_factory, world.users["admin"]) as client:
            for assignment in world.assignments.values():
                resp = await client.get(f"/api/v1/assignments/{assignment.id}")
                assert resp.status_code == 200, resp.text


async def test_the_schedule_holds_what_the_caller_can_see():
    async with _temp_database("asgvis_schedule") as (session_factory, _engine):
        world = await _seed(session_factory)
        for persona in PERSONAS:
            async with _client(session_factory, world.users[persona]) as client:
                resp = await client.get("/api/v1/assignments/schedule")
            assert resp.status_code == 200, (persona, resp.text)
            listed = {world.key_of(row["assignment_id"]) for row in resp.json()}
            assert listed == VISIBLE[persona], persona

        # An admin's Schedule tab reflects the whole platform.
        async with _client(session_factory, world.users["admin"]) as client:
            resp = await client.get("/api/v1/assignments/schedule")
        assert {world.key_of(row["assignment_id"]) for row in resp.json()} == set(
            world.assignments
        )


async def test_a_webhook_secret_reaches_the_managers_of_a_shared_agent_only():
    """The per-user share made the assignment visible to ``user_ro``; its
    delivery credential follows the manage tier, like a group grant's."""
    async with _temp_database("asgvis_secret") as (session_factory, _engine):
        world = await _seed(session_factory)
        hook = _assignment(
            world.agents["private"],
            "hook",
            trigger_type="webhook",
            trigger_config={"webhook_secret": "s3cr3t-delivery-key"},
        )
        async with session_factory() as db:
            db.add(hook)
            await db.commit()

        for persona in ("owner", "group_ro", "group_config", "user_ro", "user_config"):
            async with _client(session_factory, world.users[persona]) as client:
                detail = (await client.get(f"/api/v1/assignments/{hook.id}")).json()
                listed = (await client.get("/api/v1/assignments")).json()
            row = next(r for r in listed if r["id"] == str(hook.id))
            for payload in (detail, row):
                has_secret = "webhook_secret" in (payload["trigger_config"] or {})
                assert has_secret is (persona in MANAGERS), persona


async def test_edit_and_delete_need_the_manage_tier_of_the_parent_agent():
    async with _temp_database("asgvis_write") as (session_factory, _engine):
        world = await _seed(session_factory)
        for persona in PERSONAS:
            async with _client(session_factory, world.users[persona]) as client:
                for key, assignment in world.assignments.items():
                    resp = await client.put(
                        f"/api/v1/assignments/{assignment.id}",
                        json={"name": f"renamed by {persona}"},
                    )
                    assert resp.status_code == _write_status(persona, key, 200), (
                        persona,
                        key,
                        resp.text,
                    )

                    copy = await _add_assignment(session_factory, world, key)
                    resp = await client.delete(f"/api/v1/assignments/{copy.id}")
                    expected = _write_status(persona, key, 204)
                    assert resp.status_code == expected, (persona, key, resp.text)
                    assert await _exists(session_factory, copy.id) is (expected != 204)

        async with _client(session_factory, world.users["admin"]) as client:
            resp = await client.put(
                f"/api/v1/assignments/{world.assignments['private'].id}",
                json={"name": "renamed by admin"},
            )
        assert resp.status_code == 200, resp.text


async def test_the_manual_trigger_needs_the_assignment_to_be_visible(engine_triggers):
    async with _temp_database("asgvis_trigger") as (session_factory, _engine):
        world = await _seed(session_factory)
        for persona in PERSONAS:
            async with _client(session_factory, world.users[persona]) as client:
                for key, assignment in world.assignments.items():
                    resp = await client.post(f"/api/v1/assignments/{assignment.id}/trigger")
                    assert resp.status_code == _trigger_status(persona, key), (
                        persona,
                        key,
                        resp.text,
                    )

        # Every run went through as the caller, and none reached an assignment
        # the caller cannot see - the restricted one outside its audience above
        # all.
        restricted_runs = {
            user_id for name, user_id in engine_triggers if name == "A-restricted"
        }
        assert restricted_runs == {world.users["audience"].id}
        assert all(name != "A-company" for name, _ in engine_triggers)


async def test_purging_an_assignment_s_runs_needs_the_manage_tier():
    """The purge deletes EVERY run of the assignment, the private runs of
    whoever else triggered it included - the tier that may delete the
    assignment (and its runs with it), never a read-only grant."""
    async with _temp_database("asgvis_purge") as (session_factory, _engine):
        world = await _seed(session_factory)
        for persona in PERSONAS:
            async with _client(session_factory, world.users[persona]) as client:
                for key in world.assignments:
                    copy = await _add_assignment(
                        session_factory, world, key, runs_by=("owner", "admin")
                    )
                    resp = await client.delete(f"/api/v1/assignments/{copy.id}/runs")
                    expected = _write_status(persona, key, 204)
                    assert resp.status_code == expected, (persona, key, resp.text)
                    remaining = await _count_runs(session_factory, copy.id)
                    assert remaining == (0 if expected == 204 else 2), (persona, key)

        async with _client(session_factory, world.users["admin"]) as client:
            copy = await _add_assignment(
                session_factory, world, "private", runs_by=("owner",)
            )
            resp = await client.delete(f"/api/v1/assignments/{copy.id}/runs")
        assert resp.status_code == 204, resp.text
        assert await _count_runs(session_factory, copy.id) == 0


async def test_the_list_is_one_statement_whatever_the_number_of_rows():
    async with _temp_database("asgvis_sql") as (session_factory, engine):
        world = await _seed(session_factory)
        for _ in range(5):
            await _add_assignment(session_factory, world, "private")

        statements: list[str] = []

        def _record(_conn, _cursor, statement, *_args):
            statements.append(" ".join(statement.split()).lower())

        event.listen(engine.sync_engine, "before_cursor_execute", _record)
        try:
            async with _client(session_factory, world.users["user_ro"]) as client:
                resp = await client.get("/api/v1/assignments")
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", _record)

        assert resp.status_code == 200, resp.text
        assert len(resp.json()) == 7  # private + 5 copies + company
        reads = [s for s in statements if "from assignments" in s]
        assert len(reads) == 1, reads
        assert "from agents" in reads[0], reads[0]
        # The group memberships, then the list: nothing per row.
        app_reads = [
            s
            for s in statements
            if s.startswith("select") and " from " in s and "pg_catalog" not in s
        ]
        assert len(app_reads) == 2, app_reads


# ---------------------------------------------------------------------------
# Agent-builder tools
# ---------------------------------------------------------------------------


def _not_found(content: str, assignment_id) -> bool:
    return content == f"Assignment `{assignment_id}` not found or you don't have access."


async def test_the_builder_lists_and_reads_what_the_caller_can_see():
    async with _temp_database("asgvis_tool_read") as (session_factory, _engine):
        world = await _seed(session_factory)
        for persona in PERSONAS:
            user = world.users[persona]
            content = await _tool(user, "list_assignments")
            listed = {
                key
                for key, a in world.assignments.items()
                if f"(id: `{a.id}`" in content
            }
            assert listed == VISIBLE[persona], (persona, content)

            for key, a in world.assignments.items():
                content = await _tool(user, "get_assignment", assignment_id=str(a.id))
                if key in VISIBLE[persona]:
                    assert f"**{a.name}**" in content, (persona, key, content)
                else:
                    assert _not_found(content, a.id), (persona, key, content)


async def test_an_anonymous_builder_caller_gets_the_company_wide_tier_alone():
    """An anonymous channel sender (``effective_user_id = "anonymous"``) holds no
    grant: company-managed agents' assignments, minus a restricted agent's."""
    async with _temp_database("asgvis_tool_anon") as (session_factory, _engine):
        world = await _seed(session_factory)
        content = await _tool("anonymous", "list_assignments")
        listed = {
            key for key, a in world.assignments.items() if f"(id: `{a.id}`" in content
        }
        assert listed == {"company"}, content

        for key, a in world.assignments.items():
            content = await _tool("anonymous", "get_assignment", assignment_id=str(a.id))
            if key == "company":
                assert f"**{a.name}**" in content, content
            else:
                assert _not_found(content, a.id), (key, content)


async def test_the_builder_does_not_tell_a_hidden_assignment_from_a_missing_one(engine_triggers):
    """Existence hiding: a caller that cannot see an assignment must get
    the answer an unknown id gets, or the reply confirms it exists."""
    async with _temp_database("asgvis_tool_oracle") as (session_factory, _engine):
        world = await _seed(session_factory)
        owner = world.users["owner"]
        hidden = world.assignments["strangers"].id
        missing = uuid.uuid4()
        for tool in ("get_assignment", "trigger_assignment"):
            on_hidden = await _tool(owner, tool, assignment_id=str(hidden))
            on_missing = await _tool(owner, tool, assignment_id=str(missing))
            assert on_hidden.replace(str(hidden), "<id>") == on_missing.replace(
                str(missing), "<id>"
            ), (tool, on_hidden, on_missing)
            assert _not_found(on_missing, missing), (tool, on_missing)
        assert engine_triggers == []


async def test_the_builder_edits_and_deletes_with_the_rest_api_s_gates():
    async with _temp_database("asgvis_tool_write") as (session_factory, _engine):
        world = await _seed(session_factory)
        outcomes = {
            200: "updated: name",
            403: "company-managed assignments",
            404: "not found",
        }
        for persona in PERSONAS:
            user = world.users[persona]
            for key, assignment in world.assignments.items():
                status = _write_status(persona, key, 200)
                content = await _tool(
                    user,
                    "update_assignment",
                    assignment_id=str(assignment.id),
                    name=f"renamed by {persona}",
                )
                if status == 403 and key == "private":
                    expected = "You don't have permission to edit assignments on this agent."
                else:
                    expected = outcomes[status]
                assert expected in content, (persona, key, content)

                copy = await _add_assignment(session_factory, world, key)
                content = await _tool(user, "delete_assignment", assignment_id=str(copy.id))
                deleted = _write_status(persona, key, 204) == 204
                assert ("has been deleted" in content) is deleted, (persona, key, content)
                assert await _exists(session_factory, copy.id) is not deleted


async def test_the_builder_triggers_what_the_rest_api_triggers(engine_triggers):
    """Same gate as ``POST /assignments/{id}/trigger``: a company-managed
    assignment that runs per user is open to a non-admin who can see it."""
    async with _temp_database("asgvis_tool_trigger") as (session_factory, _engine):
        world = await _seed(session_factory)
        for persona in PERSONAS:
            user = world.users[persona]
            for key, assignment in world.assignments.items():
                content = await _tool(
                    user, "trigger_assignment", assignment_id=str(assignment.id)
                )
                status = _trigger_status(persona, key)
                if status == 200:
                    assert "triggered successfully" in content, (persona, key, content)
                elif status == 403:
                    assert "Only admins can trigger company-managed assignments" in content, (
                        persona,
                        key,
                        content,
                    )
                else:
                    assert _not_found(content, assignment.id), (persona, key, content)


async def test_the_overlap_guard_of_a_per_user_assignment_is_the_caller_s_own_run(engine_triggers):
    """A per-user assignment runs once per user: somebody else's report in
    flight does not block mine, my own does - through REST and the builder."""
    async with _temp_database("asgvis_guard") as (session_factory, _engine):
        world = await _seed(session_factory)
        restricted = world.assignments["restricted"]
        audience = world.users["audience"]
        path = f"/api/v1/assignments/{restricted.id}/trigger"

        async def _running(user: User) -> None:
            async with session_factory() as db:
                db.add(
                    AssignmentRun(
                        assignment_id=restricted.id,
                        agent_id=restricted.agent_id,
                        user_id=user.id,
                        is_company_managed=False,
                        status="running",
                    )
                )
                await db.commit()

        await _running(world.users["admin"])
        async with _client(session_factory, audience) as client:
            resp = await client.post(path)
        assert resp.status_code == 200, resp.text
        content = await _tool(audience, "trigger_assignment", assignment_id=str(restricted.id))
        assert "triggered successfully" in content, content

        await _running(audience)
        async with _client(session_factory, audience) as client:
            resp = await client.post(path)
        assert resp.status_code == 409, resp.text
        content = await _tool(audience, "trigger_assignment", assignment_id=str(restricted.id))
        assert "already have a run in progress" in content, content
        assert [name for name, _ in engine_triggers] == ["A-restricted", "A-restricted"]


# ---------------------------------------------------------------------------
# REST and the agent tools answer the same way
# ---------------------------------------------------------------------------

# Everyone, the admin included: REST and the tools give an admin the same bypass.
CALLERS = (*PERSONAS, "admin")


@pytest.fixture
def delegate_triggers(monkeypatch) -> list[tuple[str, uuid.UUID, bool]]:
    """The assignment engine's trigger as every caller uses it (positional or
    keyword), honouring ``skip_if_active`` like the real engine."""
    calls: list[tuple[str, uuid.UUID, bool]] = []

    async def _trigger(
        assignment,
        agent,
        payload=None,
        db=None,
        trigger_context=None,
        triggered_by_user_id=None,
        skip_if_active=False,
        active_run_user_id=None,
        **_,
    ):
        if skip_if_active and await assignment_engine.has_active_run(
            db, assignment.id, user_id=active_run_user_id
        ):
            return None
        run = AssignmentRun(
            assignment_id=assignment.id,
            agent_id=agent.id,
            user_id=triggered_by_user_id,
            is_company_managed=False,
            status="completed",
            trigger_payload=payload,
            trigger_context=trigger_context,
        )
        db.add(run)
        await db.flush()
        calls.append((assignment.name, triggered_by_user_id, skip_if_active))
        return run.id

    monkeypatch.setattr(assignment_engine, "trigger_assignment", _trigger)
    return calls


async def _delegate(user: User, assignment_id) -> str:
    from app.services.builtin_tools import work_items

    return await work_items._run_delegate_to_assignment(
        assignment_id=str(assignment_id),
        task="Do it.",
        acting_user_id=str(user.id),
        delegating_agent_id=None,
    )


def _tool_status(content: str, assignment: Assignment) -> int:
    """A tool answer as the status REST gives the same call."""
    if (
        "triggered successfully" in content
        or content.startswith("Delegated to assignment")
        or f"**{assignment.name}**" in content
    ):
        return 200
    if content.startswith("Only admins can"):
        return 403
    if "not found or" in content:
        return 404
    raise AssertionError(f"unmapped tool answer: {content}")


async def test_every_assignment_tool_answers_like_its_rest_route(delegate_triggers):
    """Same caller, same assignment: for every tier, the admin included, the
    agent-builder ``get_assignment``, ``list_assignments(agent_id=)`` and
    ``trigger_assignment`` tools and the work-items ``delegate_to_assignment``
    tool are allowed exactly when ``GET /assignments/{id}``,
    ``GET /assignments?agent_id=`` and ``POST /assignments/{id}/trigger`` are:
    an admin's agent reaches a colleague's private assignment as the
    admin does from Settings > Objects."""
    async with _temp_database("asgvis_parity") as (session_factory, _engine):
        world = await _seed(session_factory)
        for caller in CALLERS:
            user = world.users[caller]
            for key, assignment in world.assignments.items():
                async with _client(session_factory, user) as client:
                    rest_read = (
                        await client.get(f"/api/v1/assignments/{assignment.id}")
                    ).status_code
                    rest_list = {
                        world.key_of(row["id"])
                        for row in (
                            await client.get(
                                f"/api/v1/assignments?agent_id={assignment.agent_id}"
                            )
                        ).json()
                    }
                    rest_trigger = (
                        await client.post(f"/api/v1/assignments/{assignment.id}/trigger")
                    ).status_code

                read = await _tool(user, "get_assignment", assignment_id=str(assignment.id))
                listed = await _tool(
                    user, "list_assignments", agent_id=str(assignment.agent_id)
                )
                trigger = await _tool(
                    user, "trigger_assignment", assignment_id=str(assignment.id)
                )
                delegate = await _delegate(user, assignment.id)

                where = (caller, key)
                tool_list = {
                    k for k, a in world.assignments.items() if f"(id: `{a.id}`" in listed
                }
                if caller == "admin":
                    # The bypass opens everything, on REST and in the tools.
                    assert (rest_read, rest_trigger) == (200, 200), where
                    assert rest_list == {key}, where
                assert _tool_status(read, assignment) == rest_read, (where, read)
                assert tool_list == rest_list, (where, listed)
                assert _tool_status(trigger, assignment) == rest_trigger, (where, trigger)
                assert _tool_status(delegate, assignment) == rest_trigger, (where, delegate)

                # And REST itself is the table of the description.
                if caller != "admin":
                    assert rest_trigger == _trigger_status(caller, key), where


async def test_delegating_a_per_user_assignment_guards_on_the_caller_s_own_run(
    delegate_triggers,
):
    """``delegate_to_assignment`` shares REST's overlap guard: somebody else's
    report in flight does not block a per-user assignment, the caller's own does;
    any other assignment keeps the engine's atomic whole-assignment guard."""
    async with _temp_database("asgvis_delegate_guard") as (session_factory, _engine):
        world = await _seed(session_factory)
        restricted = world.assignments["restricted"]
        audience = world.users["audience"]

        async def _running(user: User, assignment: Assignment) -> None:
            async with session_factory() as db:
                db.add(
                    AssignmentRun(
                        assignment_id=assignment.id,
                        agent_id=assignment.agent_id,
                        user_id=user.id,
                        is_company_managed=False,
                        status="running",
                    )
                )
                await db.commit()

        await _running(world.users["admin"], restricted)
        content = await _delegate(audience, restricted.id)
        assert content.startswith("Delegated to assignment"), content

        await _running(audience, restricted)
        content = await _delegate(audience, restricted.id)
        assert "You already have a run of assignment" in content, content

        private = world.assignments["private"]
        await _running(world.users["owner"], private)
        content = await _delegate(world.users["group_ro"], private.id)
        assert "already has an active run" in content, content

        assert delegate_triggers == [("A-restricted", audience.id, True)]


async def test_a_trigger_racing_past_the_overlap_check_is_still_refused(
    delegate_triggers, monkeypatch
):
    """The overlap check is check-then-act; the engine's locked guard is what
    makes it atomic. With the check blinded (a concurrent trigger committing
    between check and insert), REST, the builder and the delegation all still
    refuse through ``trigger_assignment(skip_if_active=True,
    active_run_user_id=...)``, scoped to the caller for a per-user assignment."""
    from app.api.v1 import helpers

    async def _blind(*_args, **_kwargs):
        return None

    monkeypatch.setattr(assignments_api, "manual_trigger_overlap", _blind)
    monkeypatch.setattr(helpers, "manual_trigger_overlap", _blind)
    async with _temp_database("asgvis_race") as (session_factory, _engine):
        world = await _seed(session_factory)
        audience = world.users["audience"]
        restricted = world.assignments["restricted"]
        async with session_factory() as db:
            db.add(
                AssignmentRun(
                    assignment_id=restricted.id,
                    agent_id=restricted.agent_id,
                    user_id=audience.id,
                    is_company_managed=False,
                    status="running",
                )
            )
            await db.commit()

        async with _client(session_factory, audience) as client:
            resp = await client.post(f"/api/v1/assignments/{restricted.id}/trigger")
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"] == (
            "You already have a run in progress for this assignment"
        )
        content = await _tool(
            audience, "trigger_assignment", assignment_id=str(restricted.id)
        )
        assert "You already have a run in progress" in content, content
        content = await _delegate(audience, restricted.id)
        assert "You already have a run of assignment" in content, content
        assert delegate_triggers == []


async def test_an_admin_s_agent_writes_what_rest_lets_the_admin_write():
    """The write tools take the admin bypass the REST routes take: an
    admin's agent edits, deletes and adds to a colleague's private assignment
    and agent, while an unknown id still answers "not found"."""
    async with _temp_database("asgvis_admin_tool_write") as (session_factory, _engine):
        world = await _seed(session_factory)
        admin = world.users["admin"]
        missing = uuid.uuid4()

        for key in ("private", "strangers"):
            assignment = world.assignments[key]
            content = await _tool(
                admin,
                "update_assignment",
                assignment_id=str(assignment.id),
                name=f"renamed by the admin's agent {key}",
            )
            assert "updated: name" in content, (key, content)

            agent_id = world.agents[key].id
            content = await _tool(
                admin, "add_assignment_to_agent", agent_id=str(agent_id), name=f"x {key}", prompt="p"
            )
            assert "added to agent" in content, (key, content)
            content = await _tool(admin, "get_agent", agent_id=str(agent_id))
            assert world.agents[key].name in content, (key, content)

            content = await _tool(admin, "delete_assignment", assignment_id=str(assignment.id))
            assert "has been deleted" in content, (key, content)
            assert not await _exists(session_factory, assignment.id), key

        for tool, extra in (
            ("update_assignment", {"name": "renamed"}),
            ("delete_assignment", {}),
        ):
            content = await _tool(admin, tool, assignment_id=str(missing), **extra)
            assert str(missing) in content and "not found" in content, (tool, content)


async def test_in_strict_mode_an_admin_s_agent_keeps_to_its_three_tiers(monkeypatch):
    """With AGENT_TOOLS_STRICT_ADMIN_ACCESS an admin's agent reaches only what
    its three tiers show: a colleague's private assignment and
    agent answer exactly like an unknown id, read or written, and stay
    untouched; the platform-wide listings are refused."""
    from app.core.config import settings

    monkeypatch.setattr(settings, "agent_tools_strict_admin_access", True)
    async with _temp_database("asgvis_admin_tool_strict") as (session_factory, _engine):
        world = await _seed(session_factory)
        admin = world.users["admin"]
        for key in ("private", "strangers"):
            assignment = world.assignments[key]
            for tool, extra in (
                ("get_assignment", {}),
                ("update_assignment", {"name": "renamed"}),
                ("delete_assignment", {}),
            ):
                content = await _tool(admin, tool, assignment_id=str(assignment.id), **extra)
                assert "not found" in content, (key, tool, content)
            content = await _tool(
                admin, "add_assignment_to_agent", agent_id=str(world.agents[key].id), name="x", prompt="p"
            )
            assert "not found" in content, (key, content)
            for tool, extra in (
                ("get_agent", {}),
                ("update_agent", {"name": "renamed"}),
                ("delete_agent", {}),
            ):
                content = await _tool(admin, tool, agent_id=str(world.agents[key].id), **extra)
                assert "not found" in content, (key, tool, content)
            listed = await _tool(admin, "list_assignments")
            assert str(assignment.id) not in listed, key
            assert await _exists(session_factory, assignment.id), key
        for tool in ("list_agents", "list_runs"):
            content = await _tool(admin, tool, scope="all")
            assert "AGENT_TOOLS_STRICT_ADMIN_ACCESS is set" in content, (tool, content)


async def test_an_admin_manages_a_colleague_s_private_assignment_through_rest(
    engine_triggers,
):
    """Administration > Objects: an admin opens a colleague's private
    assignment and does everything its page offers - read, edit, trigger,
    list and purge runs, delete - through the REST routes."""
    async with _temp_database("asgvis_admin_rest") as (session_factory, _engine):
        world = await _seed(session_factory)
        admin = world.users["admin"]
        for key in ("private", "strangers"):
            assignment = world.assignments[key]
            base = f"/api/v1/assignments/{assignment.id}"
            async with _client(session_factory, admin) as client:
                assert (await client.get(base)).status_code == 200, key
                listed = await client.get(
                    f"/api/v1/assignments?agent_id={assignment.agent_id}"
                )
                assert [r["id"] for r in listed.json()] == [str(assignment.id)], key
                resp = await client.put(base, json={"name": f"edited by admin {key}"})
                assert resp.status_code == 200, (key, resp.text)
                assert resp.json()["name"] == f"edited by admin {key}"
                resp = await client.post(f"{base}/trigger")
                assert resp.status_code == 200, (key, resp.text)
                assert (await client.get(f"{base}/runs")).status_code == 200, key
                assert (await client.delete(f"{base}/runs")).status_code == 204, key
                assert (await client.delete(base)).status_code == 204, key
            assert not await _exists(session_factory, assignment.id), key
