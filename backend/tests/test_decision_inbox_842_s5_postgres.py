# SPDX-License-Identifier: Apache-2.0

"""Human gates S5 (#842) against PostgreSQL: one "Waiting for you" list across every source.

A run question, a tool approval, a flow Ask-user park and a work item each
open their decision through the engines' own columns (the dual write of S2);
the inbox lists the four for their recipient, soonest deadline first, with
where each comes from, and nothing for another person. The list is served
only while the read switch is on.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.models  # noqa: F401 - registers every table
from app.core.config import settings
from app.models import decision_hooks
from app.models.agent import Agent
from app.models.assignment import AssignmentRun
from app.models.decision import Decision
from app.models.flow import Flow
from app.models.flow_run import FlowRun
from app.models.user import User
from app.models.work_item import WorkItem
from app.services import decision_inbox
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration


@pytest.fixture
async def make(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    decision_hooks._HAS_TABLE.clear()
    decision_inbox._switch_cache.clear()
    monkeypatch.setattr(settings, "decisions_read_enabled", True)
    async with scratch_database(
        "decisions_s5", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True
    ) as db:
        yield async_sessionmaker(db.engine, expire_on_commit=False)
    decision_hooks._HAS_TABLE.clear()
    decision_inbox._switch_cache.clear()


async def _seed_four_sources(session: AsyncSession) -> tuple[User, User]:
    user = User(id=uuid.uuid4(), email=f"{uuid.uuid4().hex[:8]}@example.com", full_name="Reviewer")
    other = User(id=uuid.uuid4(), email=f"{uuid.uuid4().hex[:8]}@example.com", full_name="Other")
    session.add_all([user, other])
    await session.flush()
    agent = Agent(id=uuid.uuid4(), user_id=user.id, name="Gatekeeper")
    flow = Flow(id=uuid.uuid4(), user_id=user.id, name="Weekly digest flow", graph={"nodes": [], "edges": []})
    session.add_all([agent, flow])
    await session.flush()
    question = AssignmentRun(id=uuid.uuid4(), agent_id=agent.id, user_id=user.id, status="running")
    approval = AssignmentRun(id=uuid.uuid4(), agent_id=agent.id, user_id=user.id, status="running")
    flow_run = FlowRun(
        id=uuid.uuid4(), flow_id=flow.id, user_id=user.id, status="running", state={}, trigger_type="manual"
    )
    item = WorkItem(id=uuid.uuid4(), user_id=user.id, owner_agent_id=agent.id, title="Renew the licence", status="open")
    elsewhere = WorkItem(id=uuid.uuid4(), user_id=other.id, title="Not yours", status="open")
    session.add_all([question, approval, flow_run, item, elsewhere])
    await session.commit()

    question.status = "awaiting_human"
    question.human_input_question = "Which feed?"
    approval.status = "awaiting_human"
    approval.human_input_question = "Approval required to run x"
    approval.pending_approvals = [{"tool_call_id": "c1", "tool_name": "x", "arguments": {}}]
    flow_run.status = "awaiting"
    flow_run.state = {"waiting": {"kind": "human", "question": "Send it?", "step_id": "s1", "node": "ask"}}
    deadline = (datetime.now(UTC) + timedelta(hours=2)).isoformat()
    item.needs_human = True
    item.status = "blocked"
    item.ask = {"question": "Renew for a year?", "deadline": deadline}
    elsewhere.needs_human = True
    elsewhere.status = "blocked"
    elsewhere.ask = {"question": "Someone else's call"}
    await session.commit()
    return user, other


async def test_four_sources_in_one_list_for_their_recipient(make: async_sessionmaker[AsyncSession]) -> None:
    async with make() as session:
        user, other = await _seed_four_sources(session)
        answer = await decision_inbox.waiting_for(session, SimpleNamespace(id=user.id, is_admin=False))
        assert answer["enabled"] is True
        items = answer["items"]
        assert {i["source_type"] for i in items} == {"run", "flow_run", "work_item"}
        assert {i["kind"] for i in items} == {"hitl", "tool_approval", "flow_ask", "work_item"}
        assert len(items) == 4
        # The one with a deadline comes first; the rest by when they were asked.
        assert items[0]["question"] == "Renew for a year?" and items[0]["deadline"]
        by_kind = {i["kind"]: i for i in items}
        assert by_kind["work_item"]["origin"] == "Renew the licence"
        assert by_kind["work_item"]["agent"] == "Gatekeeper"
        assert by_kind["work_item"]["link"].startswith("/tasks/work-items/")
        assert by_kind["hitl"]["agent"] == "Gatekeeper"
        assert by_kind["hitl"]["link"].startswith("/operations/executions/")
        assert by_kind["flow_ask"]["origin"] == "Weekly digest flow"
        assert by_kind["flow_ask"]["link"].startswith("/flows/library/")
        assert by_kind["tool_approval"]["id"].startswith("run:")
        assert "Someone else's call" not in {i["question"] for i in items}

        theirs = await decision_inbox.waiting_for(session, SimpleNamespace(id=other.id, is_admin=False))
        assert [i["question"] for i in theirs["items"]] == ["Someone else's call"]


async def test_an_answered_decision_leaves_the_list(make: async_sessionmaker[AsyncSession]) -> None:
    async with make() as session:
        user, _ = await _seed_four_sources(session)
        row = (
            await session.execute(Decision.__table__.select().where(Decision.kind == "hitl", Decision.status == "open"))
        ).first()
        assert row is not None
        run = await session.get(AssignmentRun, row.source_id)
        assert run is not None
        run.human_input_response = "the weekly one"
        await session.commit()
        answer = await decision_inbox.waiting_for(session, SimpleNamespace(id=user.id, is_admin=False))
        assert "hitl" not in {i["kind"] for i in answer["items"]}
        assert len(answer["items"]) == 3


async def test_the_list_is_not_served_while_the_switch_is_off(
    make: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "decisions_read_enabled", False)
    async with make() as session:
        user, _ = await _seed_four_sources(session)
        answer = await decision_inbox.waiting_for(session, SimpleNamespace(id=user.id, is_admin=False))
        assert answer == {"enabled": False, "reason": "DECISIONS_READ_ENABLED is off", "items": []}
