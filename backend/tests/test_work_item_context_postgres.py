"""What a work item says to the person reading it, on real rows.

Against a real PostgreSQL (``tests/scratch_database.py``), as a NON-admin
(rule 19): the agents are named only when the reader may open them, the
person and the visibility tier are the row's, the origin recorded at creation
links back only to what the reader may open, and the pickup state is the
dispatch's own rule - an inbox whose filters leave an item out does not claim
to take it. Requeueing clears the claim, so the item can be claimed again.
Marked ``integration``: CI's integration job is the one with a PostgreSQL
service.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest

import app.models  # noqa: F401 - every table, for ALL_TABLES
from app.models.agent import Agent
from app.models.assignment import Assignment, AssignmentRun
from app.models.chat import Conversation
from app.models.flow import Flow
from app.models.flow_run import FlowRun
from app.models.user import User
from app.services import work_item as wi_service
from app.services import work_item_context as ctx
from app.services.company_managed_writes import PLATFORM_WRITER, agent_writer, person_writer
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration


@asynccontextmanager
async def _database() -> AsyncIterator:
    async with scratch_database("wictx", tables=ALL_TABLES, extensions=("vector",)) as db:
        yield db.sessions


def _user(name: str) -> User:
    return User(
        id=uuid.uuid4(),
        email=f"{name}-{uuid.uuid4().hex[:8]}@test.local",
        full_name=name.title(),
        hashed_password="x",
    )


def _agent(owner: User | None, name: str, *, company: bool = False) -> Agent:
    return Agent(
        id=uuid.uuid4(),
        user_id=None if company else owner.id,
        name=name,
        slug=f"{name.lower().replace(' ', '-')}-{uuid.uuid4().hex[:6]}",
        is_company_managed=company,
        builtin_tool_ids=["work_items"],
    )


class _World:
    """A member reading, another member, their private agents and a fleet agent."""

    async def create(self, sessions) -> _World:
        self.sessions = sessions
        self.member = _user("member")
        self.other = _user("other")
        self.mine = _agent(self.member, "My Agent")
        self.theirs = _agent(self.other, "Their Agent")
        self.fleet = _agent(None, "Fleet Agent", company=True)
        async with sessions() as db:
            db.add_all([self.member, self.other])
            await db.flush()
            db.add_all([self.mine, self.theirs, self.fleet])
            await db.commit()
        return self

    async def item(self, **kwargs) -> dict:
        kwargs.setdefault("user_id", str(self.member.id))
        kwargs.setdefault("title", "Load the page records")
        kwargs.setdefault("writer", PLATFORM_WRITER)
        return await wi_service.create_work_item(**kwargs)

    async def inbox(self, agent: Agent, owner: User | None, **config) -> Assignment:
        async with self.sessions() as db:
            assignment = Assignment(
                id=uuid.uuid4(),
                agent_id=agent.id,
                user_id=owner.id if owner else None,
                is_company_managed=agent.is_company_managed,
                name=f"Inbox of {agent.name}",
                trigger_type="work_item",
                trigger_config=config,
                prompt="Work the item you were given.",
                enabled=True,
            )
            db.add(assignment)
            await db.commit()
        return assignment

    async def describe(self, item: dict) -> dict:
        reread = await wi_service.get_work_item(item["id"], user_id=str(self.member.id))
        assert reread is not None, "the member must see the item"
        return (await ctx.describe_work_items([reread], viewer=self.member))[0]["context"]


async def test_agents_are_named_only_when_the_reader_may_open_them():
    async with _database() as sessions:
        world = await _World().create(sessions)
        item = await world.item(
            owner_agent_id=str(world.theirs.id), created_by_agent_id=str(world.mine.id)
        )
        context = await world.describe(item)

    # A colleague's private OWNER agent keeps its name, read-only: no id, no
    # link (#858). Who holds the work is part of the item.
    assert context["owner_agent"]["id"] is None
    assert context["owner_agent"]["name"] == "Their Agent"
    assert context["owner_agent"]["accessible"] is False
    assert context["created_by_agent"]["name"] == "My Agent"
    assert context["created_by_agent"]["accessible"] is True
    assert context["claimed_by_agent"] is None
    assert context["person"] == {"id": str(world.member.id), "name": "Member", "is_you": True}
    assert context["visibility"] == {"reason": "own", "workspace": None}


async def test_a_company_managed_item_says_the_whole_fleet_sees_it_and_who_it_acts_for():
    async with _database() as sessions:
        world = await _World().create(sessions)
        item = await world.item(
            user_id=str(world.other.id),
            owner_agent_id=str(world.fleet.id),
            created_by_agent_id=str(world.fleet.id),
            is_company_managed=True,
            writer=agent_writer(is_company_managed=True, agent_id=world.fleet.id),
        )
        context = await world.describe(item)

    assert context["visibility"]["reason"] == "company_managed"
    assert context["person"] == {"id": str(world.other.id), "name": "Other", "is_you": False}
    assert context["owner_agent"]["name"] == "Fleet Agent"


async def test_the_origin_links_back_only_to_what_the_reader_may_open():
    async with _database() as sessions:
        world = await _World().create(sessions)
        async with sessions() as db:
            my_chat = Conversation(id=uuid.uuid4(), user_id=world.member.id, channel="web", title="Notion import")
            their_chat = Conversation(id=uuid.uuid4(), user_id=world.other.id, channel="web", title="Private")
            assignment = Assignment(
                id=uuid.uuid4(),
                agent_id=world.mine.id,
                user_id=world.member.id,
                name="Nightly sync",
                trigger_type="schedule",
                trigger_config={},
                prompt="Sync.",
                enabled=True,
            )
            db.add_all([my_chat, their_chat, assignment])
            await db.flush()
            my_run = AssignmentRun(
                id=uuid.uuid4(),
                assignment_id=assignment.id,
                agent_id=world.mine.id,
                user_id=world.member.id,
                status="completed",
            )
            their_run = AssignmentRun(
                id=uuid.uuid4(), agent_id=world.theirs.id, user_id=world.other.id, status="completed"
            )
            flow = Flow(id=uuid.uuid4(), user_id=world.member.id, name="Triage flow", graph={})
            db.add_all([my_run, their_run, flow])
            await db.flush()
            flow_run = FlowRun(
                id=uuid.uuid4(), flow_id=flow.id, user_id=world.member.id, trigger_type="manual"
            )
            db.add(flow_run)
            await db.commit()

        from_chat = await world.item(origin=wi_service.WorkItemOrigin(conversation_id=my_chat.id))
        from_run = await world.item(origin=wi_service.WorkItemOrigin(run_id=str(my_run.id)))
        from_flow = await world.item(
            source="flow", origin=wi_service.WorkItemOrigin(flow_run_id=flow_run.id)
        )
        hidden = await world.item(
            origin=wi_service.WorkItemOrigin(conversation_id=their_chat.id, run_id=their_run.id)
        )
        dropped = await world.item(origin=wi_service.WorkItemOrigin(conversation_id="not-a-uuid"))
        chat_ctx = await world.describe(from_chat)
        run_ctx = await world.describe(from_run)
        flow_ctx = await world.describe(from_flow)
        hidden_ctx = await world.describe(hidden)
        dropped_ctx = await world.describe(dropped)

    assert chat_ctx["origin"]["kind"] == "chat"
    assert chat_ctx["origin"]["conversation"] == {"id": str(my_chat.id), "title": "Notion import"}

    assert run_ctx["origin"]["kind"] == "run"
    assert run_ctx["origin"]["run"] == {
        "id": str(my_run.id),
        "name": "Nightly sync",
        "status": "completed",
        "accessible": True,
    }

    assert flow_ctx["origin"]["kind"] == "flow"
    assert flow_ctx["origin"]["flow_run"]["id"] == str(flow_run.id)
    assert flow_ctx["origin"]["flow_run"]["flow_id"] == str(flow.id)
    assert flow_ctx["origin"]["flow_run"]["name"] == "Triage flow"

    # Recorded, but another person's chat and run: said to exist, never linked.
    assert hidden_ctx["origin"]["conversation"] is None
    assert hidden_ctx["origin"]["conversation_hidden"] is True
    assert hidden_ctx["origin"]["run"] == {"id": None, "name": None, "status": None, "accessible": False}

    assert dropped_ctx["origin"]["kind"] == "unknown"
    assert dropped["conversation_id"] is None


async def test_the_pickup_state_is_the_dispatch_rule():
    async with _database() as sessions:
        world = await _World().create(sessions)
        await world.inbox(world.mine, world.member, priority_min="high")

        taken = await world.item(owner_agent_id=str(world.mine.id), priority="urgent")
        left_out = await world.item(owner_agent_id=str(world.mine.id), priority="medium")
        later = await world.item(
            owner_agent_id=str(world.mine.id),
            priority="high",
            not_before=(datetime.now(UTC) + timedelta(days=2)).isoformat(),
        )
        # A deadline ahead defers nothing (#8753): taken at once.
        dated = await world.item(
            owner_agent_id=str(world.mine.id),
            priority="high",
            due_at=(datetime.now(UTC) + timedelta(days=2)).isoformat(),
        )
        nobody = await world.item(owner_agent_id=str(world.theirs.id))
        unowned = await world.item()
        taken_ctx, left_ctx, later_ctx = [await world.describe(i) for i in (taken, left_out, later)]
        nobody_ctx, unowned_ctx = await world.describe(nobody), await world.describe(unowned)

        # What the scheduler will claim is exactly what the views call automatic.
        dispatchable = await wi_service.list_dispatchable_work_items(
            owner_agent_id=str(world.mine.id),
            user_id=str(world.member.id),
            priority_min="high",
            writer=agent_writer(is_company_managed=False, agent_id=world.mine.id),
        )

    assert {d["id"] for d in dispatchable} == {taken["id"], dated["id"]}
    assert taken_ctx["pickup"]["state"] == "automatic"
    [inbox] = taken_ctx["pickup"]["inboxes"]
    assert inbox["name"] == "Inbox of My Agent"
    assert inbox["accessible"] is True and inbox["accepts"] is True
    assert inbox["priority_min"] == "high"

    assert left_ctx["pickup"]["state"] == "filtered_out"
    assert left_ctx["pickup"]["inboxes"][0]["accepts"] is False
    assert later_ctx["pickup"]["state"] == "waiting_until_due"
    assert later_ctx["pickup"]["available_at"] == later["not_before"]
    assert nobody_ctx["pickup"]["state"] == "manual"
    assert nobody_ctx["pickup"]["auto_cancel_at"] is not None
    assert unowned_ctx["pickup"]["state"] == "unassigned"


async def test_an_inbox_the_reader_cannot_open_is_counted_unnamed():
    async with _database() as sessions:
        world = await _World().create(sessions)
        # Their personal agent drains the fleet agent's queue, from their own assignment.
        await world.inbox(world.theirs, world.other, owner_agent_id=str(world.fleet.id))
        item = await world.item(
            user_id=str(world.other.id),
            owner_agent_id=str(world.fleet.id),
            is_company_managed=True,
            writer=agent_writer(is_company_managed=True, agent_id=world.fleet.id),
        )
        context = await world.describe(item)

    [inbox] = context["pickup"]["inboxes"]
    assert inbox["accessible"] is False
    assert inbox["name"] is None and inbox["id"] is None
    # A personal agent never takes over a company-managed item.
    assert inbox["accepts"] is False
    assert context["pickup"]["state"] == "filtered_out"


async def test_requeue_clears_the_claim_so_the_item_can_be_claimed_again():
    async with _database() as sessions:
        world = await _World().create(sessions)
        item = await world.item(owner_agent_id=str(world.mine.id), payload={"_dispatch_attempts": 4})
        writer = agent_writer(is_company_managed=False, agent_id=world.mine.id)
        claim = await wi_service.claim_work_item(
            item["id"], agent_id=str(world.mine.id), user_id=str(world.member.id), writer=writer
        )
        assert claim["claimed"] is True
        await wi_service.complete_work_item(
            item["id"], user_id=str(world.member.id), result="done once", writer=writer
        )

        # A plain status edit leaves the claim behind: the item could never be
        # claimed again, which is what requeue exists for.
        member = person_writer(is_admin=False)
        requeued = await wi_service.requeue_work_item(item["id"], user_id=str(world.member.id), writer=member)
        again = await wi_service.claim_work_item(
            item["id"], agent_id=str(world.mine.id), user_id=str(world.member.id), writer=writer
        )

    assert requeued["status"] == "open"
    assert requeued["claimed_by_agent_id"] is None
    assert requeued["result"] is None
    assert "_dispatch_attempts" not in requeued["payload"]
    assert again["claimed"] is True


async def test_the_owner_filter_counts_what_the_reader_may_name():
    async with _database() as sessions:
        world = await _World().create(sessions)
        for _ in range(2):
            await world.item(owner_agent_id=str(world.mine.id))
        await world.item(owner_agent_id=str(world.theirs.id))
        await world.item()
        counts = await wi_service.count_work_items_by_owner(user_id=str(world.member.id))
        owners = await ctx.describe_owners(counts, viewer=world.member)

    by_name = {(o["agent"] or {}).get("name"): o["count"] for o in owners}
    assert by_name["My Agent"] == 2
    assert owners[-1] == {
        "agent": {"id": None, "name": None, "icon": None, "accessible": False},
        "count": 1,
    }
    assert {"agent": None, "count": 1} in owners
