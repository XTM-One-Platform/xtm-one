# SPDX-License-Identifier: Apache-2.0

"""The fleet's Approval tab lists every open decision the fleet raised (#1493), against PostgreSQL.

A fleet whose home member has a run with a tool approval, a run with a
question and three needs_human work items lists five decisions, one per
source, and nothing of an agent outside the fleet. A decision addressed to
another person is listed for a manager of the fleet, read only
(``can_answer`` false), and never for a plain reader. An answered decision
leaves the list.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.models  # noqa: F401 - registers every table
from app.core.config import settings
from app.models import decision_hooks
from app.models.agent import Agent
from app.models.assignment import AssignmentRun
from app.models.fleet import Fleet, FleetMember
from app.models.user import User
from app.models.work_item import WorkItem
from app.services import decision_inbox
from app.services.workspaces import create_workspace
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration


@pytest.fixture
async def make(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    decision_hooks._HAS_TABLE.clear()
    decision_inbox._switch_cache.clear()
    monkeypatch.setattr(settings, "decisions_read_enabled", True)
    async with scratch_database(
        "decisions_fleet", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True
    ) as db:
        yield async_sessionmaker(db.engine, expire_on_commit=False)
    decision_hooks._HAS_TABLE.clear()
    decision_inbox._switch_cache.clear()


async def _seed(session: AsyncSession) -> tuple[User, User, Fleet]:
    owner = User(id=uuid.uuid4(), email=f"{uuid.uuid4().hex[:8]}@example.com", full_name="Owner")
    other = User(id=uuid.uuid4(), email=f"{uuid.uuid4().hex[:8]}@example.com", full_name="Other")
    session.add_all([owner, other])
    await session.flush()
    member = Agent(id=uuid.uuid4(), user_id=owner.id, name="Loop Orchestrator")
    outsider = Agent(id=uuid.uuid4(), user_id=owner.id, name="Not in the fleet")
    session.add_all([member, outsider])
    await session.flush()
    workspace = await create_workspace(session, user_id=owner.id, name="loop home")
    fleet = Fleet(
        user_id=owner.id, slug="loop", name="Loop", workspace_id=workspace.id, orchestrator_agent_id=member.id
    )
    session.add(fleet)
    await session.flush()
    session.add(FleetMember(fleet_id=fleet.id, agent_id=member.id, role="member"))

    question = AssignmentRun(id=uuid.uuid4(), agent_id=member.id, user_id=owner.id, status="running")
    approval = AssignmentRun(id=uuid.uuid4(), agent_id=member.id, user_id=owner.id, status="running")
    outside_run = AssignmentRun(id=uuid.uuid4(), agent_id=outsider.id, user_id=owner.id, status="running")
    items = [
        WorkItem(id=uuid.uuid4(), user_id=owner.id, owner_agent_id=member.id, title=f"Decide {n}", status="open")
        for n in range(3)
    ]
    created_for_other = WorkItem(
        id=uuid.uuid4(), user_id=other.id, created_by_agent_id=member.id, title="Someone else's call", status="open"
    )
    outside_item = WorkItem(
        id=uuid.uuid4(), user_id=owner.id, owner_agent_id=outsider.id, title="Outside", status="open"
    )
    session.add_all([question, approval, outside_run, *items, created_for_other, outside_item])
    await session.commit()

    question.status = "awaiting_human"
    question.human_input_question = "Which feed?"
    approval.status = "awaiting_human"
    approval.human_input_question = "Approval required to run x"
    approval.pending_approvals = [{"tool_call_id": "c1", "tool_name": "x", "arguments": {}}]
    outside_run.status = "awaiting_human"
    outside_run.human_input_question = "Not the fleet's"
    for n, item in enumerate([*items, created_for_other, outside_item]):
        item.needs_human = True
        item.status = "blocked"
        item.ask = {"question": f"Question {n}?"}
    await session.commit()
    return owner, other, fleet


async def test_the_fleet_lists_every_decision_its_members_raised(make: async_sessionmaker[AsyncSession]) -> None:
    async with make() as session:
        owner, _, fleet = await _seed(session)
        answer = await decision_inbox.waiting_for(
            session, SimpleNamespace(id=owner.id, is_admin=False), fleet_id=fleet.id
        )
        assert answer["enabled"] is True
        items = answer["items"]
        assert len(items) == 5
        assert sorted(i["kind"] for i in items) == ["hitl", "tool_approval", "work_item", "work_item", "work_item"]
        assert all(i["can_answer"] for i in items)
        assert all(i["agent"] == "Loop Orchestrator" for i in items)
        assert "Not the fleet's" not in {i["question"] for i in items}

        # The unfiltered inbox still holds the outsider's decisions: the fleet filter only narrows.
        everything = await decision_inbox.waiting_for(session, SimpleNamespace(id=owner.id, is_admin=False))
        assert len(everything["items"]) == 7


async def test_a_manager_sees_another_recipient_read_only(make: async_sessionmaker[AsyncSession]) -> None:
    async with make() as session:
        owner, other, fleet = await _seed(session)
        managed = await decision_inbox.waiting_for(
            session, SimpleNamespace(id=owner.id, is_admin=False), fleet_id=fleet.id, every_recipient=True
        )
        assert len(managed["items"]) == 6
        read_only = [i for i in managed["items"] if not i["can_answer"]]
        assert [i["origin"] for i in read_only] == ["Someone else's call"]

        theirs = await decision_inbox.waiting_for(
            session, SimpleNamespace(id=other.id, is_admin=False), fleet_id=fleet.id
        )
        assert [i["origin"] for i in theirs["items"]] == ["Someone else's call"]
        assert theirs["items"][0]["can_answer"] is True


async def test_an_answered_decision_leaves_the_fleet_list(make: async_sessionmaker[AsyncSession]) -> None:
    async with make() as session:
        owner, _, fleet = await _seed(session)
        first = await decision_inbox.waiting_for(
            session, SimpleNamespace(id=owner.id, is_admin=False), fleet_id=fleet.id
        )
        hitl = next(i for i in first["items"] if i["kind"] == "hitl")
        run = await session.get(AssignmentRun, uuid.UUID(hitl["source_id"]))
        assert run is not None
        run.human_input_response = "the weekly one"
        await session.commit()
        after = await decision_inbox.waiting_for(
            session, SimpleNamespace(id=owner.id, is_admin=False), fleet_id=fleet.id
        )
        assert len(after["items"]) == 4
        assert "hitl" not in {i["kind"] for i in after["items"]}
