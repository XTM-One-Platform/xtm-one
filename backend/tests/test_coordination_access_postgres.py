"""Company-managed fleets coordinate end to end, and an agent can narrow its rows.

Against a real PostgreSQL (``tests/scratch_database.py``): the agents' own
work-item and shared-state tools, built exactly as ``run_agent_chat`` builds
them (the agent's own tier and id), the inbox dispatcher and the services, on
real rows. What a fake session cannot show is proven here: the SQL mirror of
the narrowing (``restricted_governors``) hides exactly what the Python rule
refuses, the listings and the dispatch stay one query, two fleets using the
same shared-state key each reach their own entry, and the default - no
narrowing - is the always-on base rule for every agent and row. Marked
``integration``: CI's integration job is the one with a PostgreSQL service.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import func, select

import app.models  # noqa: F401 - every table, for ALL_TABLES
from app.models.agent import Agent
from app.models.assignment import Assignment
from app.models.shared_state import SharedState
from app.models.user import User
from app.models.work_item import WorkItem
from app.services import autonomous
from app.services import shared_state as shared_state_service
from app.services import work_item as wi_service
from app.services.builtin_tools import build_shared_state_tools, build_work_item_tools
from app.services.company_managed_writes import (
    CompanyManagedWriteRefusedError,
    agent_writer,
    coordination_access_of,
    coordination_admits,
    person_writer,
)
from app.tools.base import ToolCall
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration


@asynccontextmanager
async def _database() -> AsyncIterator:
    async with scratch_database("coordaccess", tables=ALL_TABLES, extensions=("vector",)) as db:
        yield db.sessions


def _policy(read="all", write="all", read_ids=(), write_ids=()):
    return {
        "read": read,
        "write": write,
        "read_agent_ids": [str(i) for i in read_ids],
        "write_agent_ids": [str(i) for i in write_ids],
    }


class _Fleet:
    """Two company-managed fleets (A/B and C/D), a dispatcher E and a personal agent P."""

    def __init__(self, sessions):
        self.sessions = sessions
        self.admin = User(
            id=uuid.uuid4(),
            email=f"admin-{uuid.uuid4().hex[:8]}@test.local",
            full_name="Admin",
            hashed_password="x",
            role="admin",
        )
        self.member = User(
            id=uuid.uuid4(),
            email=f"member-{uuid.uuid4().hex[:8]}@test.local",
            full_name="Member",
            hashed_password="x",
        )
        self.agents: dict[str, Agent] = {}

    async def create(self, **policies) -> _Fleet:
        async with self.sessions() as db:
            db.add_all([self.admin, self.member])
            await db.flush()
            for name in "ABCDE":
                self.agents[name] = Agent(
                    id=uuid.uuid4(),
                    user_id=self.admin.id,
                    name=f"Agent {name}",
                    slug=f"agent-{name.lower()}-{uuid.uuid4().hex[:6]}",
                    is_company_managed=True,
                    builtin_tool_ids=["work_items", "shared_state"],
                    coordination_access=policies.get(name),
                )
            self.agents["P"] = Agent(
                id=uuid.uuid4(),
                user_id=self.member.id,
                name="Personal",
                slug=f"personal-{uuid.uuid4().hex[:6]}",
                is_company_managed=False,
                builtin_tool_ids=["work_items", "shared_state"],
            )
            db.add_all(list(self.agents.values()))
            await db.commit()
        return self

    def id(self, name: str) -> uuid.UUID:
        return self.agents[name].id

    def tools(self, name: str, *, user_id: str | None = None, acting_admin: bool = False):
        """The agent's tools as ``run_agent_chat`` builds them."""
        agent = self.agents[name]
        user = user_id or (str(self.member.id) if name == "P" else "anonymous")
        wi = build_work_item_tools(
            None,
            user,
            str(agent.id),
            is_company_managed=agent.is_company_managed,
            company_managed_agent=agent.is_company_managed,
            acting_admin=acting_admin,
        )[1]
        ss = build_shared_state_tools(
            None,
            user,
            str(agent.id),
            is_company_managed=agent.is_company_managed,
            company_managed_agent=agent.is_company_managed,
            acting_admin=acting_admin,
        )[1]
        return wi, ss

    async def set_policy(self, name: str, value) -> None:
        async with self.sessions() as db:
            agent = await db.get(Agent, self.id(name))
            agent.coordination_access = value
            await db.commit()


async def _call(executor, name, /, **arguments):
    return await executor.execute_tool(
        ToolCall(id=str(uuid.uuid4()), name=name, arguments=arguments)
    )


async def _item_created_by(sessions, agent_id: uuid.UUID, title: str) -> WorkItem:
    async with sessions() as db:
        return (
            await db.execute(
                select(WorkItem).where(
                    WorkItem.created_by_agent_id == agent_id, WorkItem.title == title
                )
            )
        ).scalar_one()


async def _dispatch_inbox(monkeypatch, sessions, agent: Agent, *, owner: uuid.UUID | None = None):
    """Run one sweep of the inbox of *agent*; the claimed item ids."""
    async with sessions() as db:
        assignment = Assignment(
            id=uuid.uuid4(),
            agent_id=agent.id,
            user_id=None,
            is_company_managed=agent.is_company_managed,
            name=f"Inbox {agent.name}",
            trigger_type="work_item",
            trigger_config={"owner_agent_id": str(owner)} if owner else {},
            prompt="Work the item you were given.",
            enabled=True,
        )
        db.add(assignment)
        await db.commit()
    claimed: list[str] = []

    async def _trigger(**kwargs):
        claimed.append(kwargs["payload"]["work_item_id"])
        return "run-1"

    async def _no_active_run(_db, _assignment_id):
        return False

    monkeypatch.setattr(autonomous, "trigger_assignment", _trigger)
    monkeypatch.setattr(autonomous, "has_active_run", _no_active_run)
    async with sessions() as db:
        await autonomous._process_work_item_assignments(db)
    async with sessions() as db:
        (await db.get(Assignment, assignment.id)).enabled = False
        await db.commit()
    return claimed


# ── The default: company-managed fleets coordinate as before ───────────────────


@pytest.mark.asyncio
async def test_a_fleet_hands_work_over_and_reads_the_result(monkeypatch):
    """A creates an item owned by B; B's inbox claims it and its run completes
    it; A reads the result - with nobody narrowing anything."""
    async with _database() as sessions:
        fleet = await _Fleet(sessions).create()
        a_items, _ = fleet.tools("A")
        created = await _call(
            a_items, "create_work_item", title="Patch the gateway", owner_agent_id=str(fleet.id("B"))
        )
        assert not created.is_error, created.content
        item = await _item_created_by(sessions, fleet.id("A"), "Patch the gateway")
        assert item.is_company_managed and item.owner_agent_id == fleet.id("B")

        assert await _dispatch_inbox(monkeypatch, sessions, fleet.agents["B"]) == [str(item.id)]
        # The run finalizer settles it as B.
        done = await wi_service.complete_work_item(
            str(item.id),
            user_id=None,
            result="Gateway patched",
            writer=agent_writer(is_company_managed=True, agent_id=fleet.id("B")),
        )
        assert done["status"] == "done"

        listed = await _call(a_items, "list_work_items", status="done")
        assert "Patch the gateway" in listed.content and "read-only" not in listed.content


@pytest.mark.asyncio
async def test_a_fleet_shares_and_merges_its_state():
    async with _database() as sessions:
        fleet = await _Fleet(sessions).create()
        _, a_state = fleet.tools("A")
        _, b_state = fleet.tools("B")
        assert not (await _call(a_state, "set_shared_state", key="deploy", value_json='{"stage": "build"}')).is_error
        merged = await _call(b_state, "set_shared_state", key="deploy", value_json='{"approved": true}')
        assert not merged.is_error, merged.content
        read = await _call(a_state, "get_shared_state", key="deploy")
        assert '"stage": "build"' in read.content and '"approved": true' in read.content
        assert "read-only" not in read.content
        async with sessions() as db:
            assert await db.scalar(select(func.count()).select_from(SharedState)) == 1


@pytest.mark.asyncio
async def test_a_personal_agent_reads_the_fleets_rows_and_changes_none():
    async with _database() as sessions:
        fleet = await _Fleet(sessions).create()
        a_items, a_state = fleet.tools("A")
        await _call(a_items, "create_work_item", title="Fleet task", owner_agent_id=str(fleet.id("B")))
        await _call(a_state, "set_shared_state", key="deploy", value_json='{"stage": "build"}')
        item = await _item_created_by(sessions, fleet.id("A"), "Fleet task")

        p_items, p_state = fleet.tools("P")
        listed = await _call(p_items, "list_work_items")
        assert "Fleet task" in listed.content and "read-only for this agent" in listed.content
        refused = await _call(p_items, "complete_work_item", work_item_id=str(item.id))
        assert refused.is_error and "nothing was written" in refused.content
        write = await _call(p_state, "set_shared_state", key="deploy", value_json='{"stage": "x"}')
        assert write.is_error
        async with sessions() as db:
            assert await db.scalar(select(func.count()).select_from(SharedState)) == 1
            assert (await db.get(WorkItem, item.id)).status == "open"


@pytest.mark.asyncio
async def test_an_admin_keeps_full_rest_access_to_a_narrowed_row():
    async with _database() as sessions:
        fleet = await _Fleet(sessions).create(A=_policy(read="self", write="self"))
        a_items, _ = fleet.tools("A")
        await _call(a_items, "create_work_item", title="Locked", owner_agent_id=str(fleet.id("A")))
        item = await _item_created_by(sessions, fleet.id("A"), "Locked")
        # A person's REST read is unchanged, and an admin still writes.
        listed = await wi_service.list_work_items(user_id=str(fleet.member.id))
        assert [i["id"] for i in listed] == [str(item.id)]
        out = await wi_service.complete_work_item(
            str(item.id), user_id=str(fleet.admin.id), writer=person_writer(is_admin=True)
        )
        assert out["status"] == "done"


@pytest.mark.asyncio
async def test_an_agent_acting_for_an_admin_reads_past_a_narrowing_and_writes_nothing_more() -> None:
    """#2161 (Sam, 25 Nov 2026): a gate filed by a loop agent narrowed to
    ``self`` is listed and read by another fleet agent whose turn acts for an
    administrator, stays hidden when it acts for anyone else, and the
    administrator's agent still cannot write past the write narrowing."""
    async with _database() as sessions:
        fleet = await _Fleet(sessions).create(A=_policy(read="self", write="self"))
        a_items, a_state = fleet.tools("A")
        await _call(a_items, "create_work_item", title="Remediation gate", correlation_key="ctem:remediation:1")
        await _call(a_state, "set_shared_state", key="loop", value_json='{"stage": "gate"}')
        item = await _item_created_by(sessions, fleet.id("A"), "Remediation gate")

        b_items, _ = fleet.tools("B", user_id=str(fleet.member.id))
        hidden = (await _call(b_items, "list_work_items", correlation_key_prefix="ctem:remediation:")).content
        assert "Remediation gate" not in hidden

        admin_items, admin_state = fleet.tools("B", user_id=str(fleet.admin.id), acting_admin=True)
        listed = (await _call(admin_items, "list_work_items", correlation_key_prefix="ctem:remediation:")).content
        assert "Remediation gate" in listed
        assert "Remediation gate" in (await _call(admin_items, "get_work_item", work_item_id=str(item.id))).content
        assert '"stage": "gate"' in (await _call(admin_state, "get_shared_state", key="loop")).content

        admin_b = agent_writer(is_company_managed=True, agent_id=fleet.id("B"), acting_admin=True)
        assert coordination_admits(
            coordination_access_of(_policy(read="self", write="self")),
            admin_b,
            mode="read",
            governor_id=fleet.id("A"),
        )
        with pytest.raises(CompanyManagedWriteRefusedError):
            await wi_service.update_work_item(str(item.id), user_id=None, priority="high", writer=admin_b)


# ── Two fleets, each narrowed to its own ──────────────────────────────────────


@pytest.mark.asyncio
async def test_two_fleets_keep_to_their_own_rows(monkeypatch):
    async with _database() as sessions:
        fleet = await _Fleet(sessions).create()
        id_a, id_b, id_c, id_d = (fleet.id(n) for n in "ABCD")
        await fleet.set_policy("A", _policy(read="agents", write="agents", read_ids=[id_b], write_ids=[id_b]))
        await fleet.set_policy("C", _policy(read="agents", write="agents", read_ids=[id_d], write_ids=[id_d]))
        a_items, a_state = fleet.tools("A")
        b_items, b_state = fleet.tools("B")
        c_items, c_state = fleet.tools("C")
        d_items, _ = fleet.tools("D")

        await _call(a_items, "create_work_item", title="Fleet one task", owner_agent_id=str(id_b))
        await _call(c_items, "create_work_item", title="Fleet two task", owner_agent_id=str(id_d))
        # The same key in both fleets, each on its own entry.
        await _call(a_state, "set_shared_state", key="deploy", value_json='{"fleet": 1}')
        await _call(c_state, "set_shared_state", key="deploy", value_json='{"fleet": 2}')
        async with sessions() as db:
            assert await db.scalar(select(func.count()).select_from(SharedState)) == 2

        seen_by_b = (await _call(b_items, "list_work_items")).content
        assert "Fleet one task" in seen_by_b and "Fleet two task" not in seen_by_b
        seen_by_d = (await _call(d_items, "list_work_items")).content
        assert "Fleet two task" in seen_by_d and "Fleet one task" not in seen_by_d

        merged = await _call(b_state, "set_shared_state", key="deploy", value_json='{"approved": true}')
        assert not merged.is_error, merged.content
        assert '"fleet": 1' in (await _call(b_state, "get_shared_state", key="deploy")).content
        assert '"fleet": 2' in (await _call(c_state, "get_shared_state", key="deploy")).content
        # C never reaches fleet one's entry under the same key.
        assert '"approved"' not in (await _call(c_state, "get_shared_state", key="deploy")).content

        # Each inbox claims its own fleet's work.
        fleet_one = await _item_created_by(sessions, id_a, "Fleet one task")
        fleet_two = await _item_created_by(sessions, id_c, "Fleet two task")
        assert await _dispatch_inbox(monkeypatch, sessions, fleet.agents["B"]) == [str(fleet_one.id)]
        assert await _dispatch_inbox(monkeypatch, sessions, fleet.agents["D"]) == [str(fleet_two.id)]

        # A dispatcher outside fleet one never drains B's narrowed queue.
        async with sessions() as db:
            await db.execute(
                WorkItem.__table__.update()
                .where(WorkItem.id == fleet_one.id)
                .values(status="open", claimed_by_agent_id=None, claimed_at=None)
            )
            await db.commit()
        assert await _dispatch_inbox(monkeypatch, sessions, fleet.agents["E"], owner=id_b) == []


@pytest.mark.asyncio
async def test_a_narrowing_applies_live_and_falls_back_when_its_agent_is_gone():
    async with _database() as sessions:
        fleet = await _Fleet(sessions).create()
        id_a, id_c = fleet.id("A"), fleet.id("C")
        a_items, _ = fleet.tools("A")
        await _call(a_items, "create_work_item", title="Before", owner_agent_id=str(id_a))
        item = await _item_created_by(sessions, id_a, "Before")
        c = agent_writer(is_company_managed=True, agent_id=id_c)

        # The default: C may work A's item.
        assert (await wi_service.update_work_item(str(item.id), user_id=None, priority="low", writer=c))
        # Tightened: the existing row follows at once.
        await fleet.set_policy("A", _policy(read="all", write="self"))
        with pytest.raises(CompanyManagedWriteRefusedError):
            await wi_service.update_work_item(str(item.id), user_id=None, priority="high", writer=c)
        # The governor deleted (the foreign key sets it NULL): the base rule again.
        async with sessions() as db:
            await db.delete(await db.get(Agent, id_a))
            await db.commit()
        out = await wi_service.update_work_item(str(item.id), user_id=None, priority="high", writer=c)
        assert out["priority"] == "high" and out["created_by_agent_id"] is None


@pytest.mark.asyncio
async def test_a_hidden_entry_holding_the_key_in_the_same_user_scope_is_a_clash():
    """A narrowed entry created in a chat for user X holds (X, key): another
    fleet's agent in a chat for X is refused on that key, never merged."""
    async with _database() as sessions:
        fleet = await _Fleet(sessions).create()
        await fleet.set_policy("A", _policy(read="self", write="self"))
        user = str(fleet.member.id)
        _, a_state = fleet.tools("A", user_id=user)
        _, c_state = fleet.tools("C", user_id=user)
        assert not (await _call(a_state, "set_shared_state", key="vault", value_json='{"secret": 1}')).is_error
        clash = await _call(c_state, "set_shared_state", key="vault", value_json='{"secret": 2}')
        assert clash.is_error and "cannot access" in clash.content and "Use another key" in clash.content
        assert "No shared state found" in (await _call(c_state, "get_shared_state", key="vault")).content
        async with sessions() as db:
            entry = (await db.execute(select(SharedState))).scalar_one()
            assert entry.value == {"secret": 1}


# ── The SQL mirror ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_listing_hides_exactly_what_the_rule_refuses_to_read():
    """Every policy x actor x row shape: ``list_work_items(reader=...)`` holds a
    row exactly when ``coordination_admits(read)`` does."""
    policies = [
        None,
        _policy(read="self", write="self"),
        _policy(read="agents", write="self", read_ids=["B"]),
        _policy(read="agents", write="agents", read_ids=["B"], write_ids=["B"]),
        _policy(read="all", write="self"),
    ]
    async with _database() as sessions:
        fleet = await _Fleet(sessions).create()
        ids = {n: fleet.id(n) for n in "ABCDEP"}
        for policy in policies:
            if policy is not None:
                policy = {
                    **policy,
                    "read_agent_ids": [str(ids[n]) for n in policy["read_agent_ids"]],
                    "write_agent_ids": [str(ids[n]) for n in policy["write_agent_ids"]],
                }
            await fleet.set_policy("A", policy)
            async with sessions() as db:
                await db.execute(WorkItem.__table__.delete())
                rows = [
                    WorkItem(is_company_managed=True, created_by_agent_id=ids["A"], title="plain"),
                    WorkItem(
                        is_company_managed=True,
                        created_by_agent_id=ids["A"],
                        owner_agent_id=ids["C"],
                        title="assigned to C",
                    ),
                    WorkItem(
                        is_company_managed=True,
                        created_by_agent_id=ids["A"],
                        claimed_by_agent_id=ids["D"],
                        status="claimed",
                        title="claimed by D",
                    ),
                    WorkItem(is_company_managed=True, created_by_agent_id=None, title="no governor"),
                    WorkItem(
                        is_company_managed=False,
                        user_id=fleet.member.id,
                        created_by_agent_id=ids["A"],
                        title="private",
                    ),
                ]
                db.add_all(rows)
                await db.commit()
            access = coordination_access_of(policy)
            for name in "ABCDEP":
                actor = agent_writer(is_company_managed=name != "P", agent_id=ids[name])
                user = str(fleet.member.id) if name == "P" else None
                listed = {
                    i["title"]
                    for i in await wi_service.list_work_items(user_id=user, reader=actor)
                }
                expected = {
                    row.title
                    for row in rows
                    if (row.is_company_managed or (user and row.user_id == fleet.member.id))
                    and (
                        not row.is_company_managed
                        or coordination_admits(
                            access,
                            actor,
                            mode="read",
                            governor_id=row.created_by_agent_id,
                            participants=(row.owner_agent_id, row.claimed_by_agent_id),
                        )
                    )
                }
                assert listed == expected, (policy, name)

            # Shared state, keyed on its owner agent.
            async with sessions() as db:
                await db.execute(SharedState.__table__.delete())
                db.add(SharedState(is_company_managed=True, owner_agent_id=ids["A"], key="k", value={}))
                await db.commit()
            for name in "ABCDEP":
                actor = agent_writer(is_company_managed=name != "P", agent_id=ids[name])
                listed = await shared_state_service.list_states(user_id=None, reader=actor)
                expected = coordination_admits(access, actor, mode="read", governor_id=ids["A"])
                assert bool(listed) is expected, (policy, name)


@pytest.mark.asyncio
async def test_the_api_keeps_only_company_managed_agents_in_a_list():
    from app.api.v1.agents import _coordination_access_value

    async with _database() as sessions:
        fleet = await _Fleet(sessions).create()
        async with sessions() as db:
            stored = await _coordination_access_value(
                db,
                _policy(
                    read="agents",
                    write="agents",
                    write_ids=[fleet.id("B"), fleet.id("P"), uuid.uuid4(), fleet.id("A")],
                ),
                agent_id=fleet.id("A"),
            )
        assert stored == _policy(
            read="agents", write="agents", read_ids=[fleet.id("B")], write_ids=[fleet.id("B")]
        )


@pytest.mark.asyncio
async def test_deleting_an_agent_takes_it_out_of_every_list():
    from app.api.v1.helpers import purge_agent_references

    async with _database() as sessions:
        fleet = await _Fleet(sessions).create()
        id_b = fleet.id("B")
        await fleet.set_policy(
            "A",
            _policy(read="agents", write="agents", read_ids=[id_b, fleet.id("C")], write_ids=[id_b]),
        )
        async with sessions() as db:
            await purge_agent_references(db, id_b)
            await db.commit()
        async with sessions() as db:
            agent = await db.get(Agent, fleet.id("A"))
            assert agent.coordination_access == _policy(
                read="agents", write="agents", read_ids=[fleet.id("C")]
            )
