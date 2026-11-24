# SPDX-License-Identifier: Apache-2.0

"""Human gates S2 (#842) against PostgreSQL: the dual write per engine and the downgrade.

The four engines keep their own columns; the ``before_flush`` hook of
``decision_hooks`` writes the ``decisions`` row and its ``decision_events`` in
the same transaction. Each test writes the engine's columns the way the engine
does and reads the decision back. The downgrade runs on a seeded database.
"""

from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.models  # noqa: F401 - registers every table
from app.models import decision_hooks
from app.models.agent import Agent
from app.models.assignment import AssignmentRun
from app.models.decision import Decision, DecisionEvent
from app.models.flow import Flow
from app.models.flow_run import FlowRun
from app.models.user import User
from app.models.work_item import WorkItem
from app.services import decision_router
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration

_REVISION = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "c942decisions01_add_decisions.py"


def _load_revision():
    spec = importlib.util.spec_from_file_location("c942decisions01", _REVISION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _decisions(session: AsyncSession, source_id: uuid.UUID) -> list[Decision]:
    return list(
        (
            await session.execute(select(Decision).where(Decision.source_id == source_id).order_by(Decision.created_at))
        ).scalars()
    )


async def _events(session: AsyncSession, decision_id: uuid.UUID) -> list[str]:
    rows = await session.execute(
        select(DecisionEvent.event).where(DecisionEvent.decision_id == decision_id).order_by(DecisionEvent.created_at)
    )
    return list(rows.scalars())


@pytest.fixture
async def sessions():
    decision_hooks._HAS_TABLE.clear()
    async with scratch_database(
        "decisions_s2", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True
    ) as db:
        yield async_sessionmaker(db.engine, expire_on_commit=False), db.engine
    decision_hooks._HAS_TABLE.clear()


async def _user_and_agent(session: AsyncSession) -> tuple[User, Agent]:
    user = User(id=uuid.uuid4(), email=f"{uuid.uuid4().hex[:8]}@example.com", full_name="Reviewer")
    session.add(user)
    await session.flush()
    agent = Agent(id=uuid.uuid4(), user_id=user.id, name="Gatekeeper")
    session.add(agent)
    await session.commit()
    return user, agent


async def test_run_hitl_opens_then_answers(sessions):
    make, _ = sessions
    async with make() as session:
        user, agent = await _user_and_agent(session)
        run = AssignmentRun(id=uuid.uuid4(), agent_id=agent.id, user_id=user.id, status="running")
        session.add(run)
        await session.commit()
        run.status = "awaiting_human"
        run.human_input_question = "Merge now?"
        await session.commit()
        [row] = await _decisions(session, run.id)
        assert (row.kind, row.status, row.recipient) == ("hitl", "open", f"user:{user.id}")
        assert row.ask["question"] == "Merge now?"
        run.human_input_response = "yes"
        await session.commit()
        await session.refresh(row)
        assert row.status == "answered" and row.answer == {"text": "yes"}
        assert await _events(session, row.id) == ["asked", "answered"]


async def test_tool_approval_opens_with_the_three_options_and_cancels_on_dismiss(sessions):
    make, _ = sessions
    async with make() as session:
        user, agent = await _user_and_agent(session)
        run = AssignmentRun(id=uuid.uuid4(), agent_id=agent.id, user_id=user.id, status="running")
        session.add(run)
        await session.commit()
        run.status = "awaiting_human"
        run.human_input_question = "Approval required to run x"
        run.pending_approvals = [{"tool_call_id": "c1", "tool_name": "x"}]
        await session.commit()
        run.status = "completed"
        await session.commit()
        [row] = await _decisions(session, run.id)
        assert row.kind == "tool_approval" and row.status == "cancelled"
        assert [o["key"] for o in row.ask["options"]] == ["approve", "approve_always", "reject"]
        assert await _events(session, row.id) == ["asked", "cancelled"]


async def test_flow_ask_user_park_opens_and_release_answers(sessions):
    make, _ = sessions
    async with make() as session:
        user, _agent = await _user_and_agent(session)
        flow = Flow(id=uuid.uuid4(), user_id=user.id, name="F", graph={"nodes": [], "edges": []})
        session.add(flow)
        await session.flush()
        run = FlowRun(
            id=uuid.uuid4(), flow_id=flow.id, user_id=user.id, status="running", state={}, trigger_type="manual"
        )
        session.add(run)
        await session.commit()
        run.state = {"waiting": {"kind": "human", "node": "n1", "question": "Ship?", "released": False}}
        await session.commit()
        token = decision_hooks.answer_actor.set(user.id)
        try:
            run.state = {
                "waiting": {"kind": "human", "node": "n1", "question": "Ship?", "released": True, "response": "ok"}
            }
            await session.commit()
        finally:
            decision_hooks.answer_actor.reset(token)
        [row] = await _decisions(session, run.id)
        assert (row.kind, row.status, row.answered_by) == ("flow_ask", "answered", user.id)
        assert row.answer == {"text": "ok"}


async def test_work_item_ask_opens_and_answer_closes(sessions):
    make, _ = sessions
    async with make() as session:
        user, _agent = await _user_and_agent(session)
        item = WorkItem(id=uuid.uuid4(), user_id=user.id, title="Pick one", status="open")
        session.add(item)
        await session.commit()
        item.needs_human = True
        item.ask = {"question": "Which?", "options": [{"key": "A", "label": "This"}, {"key": "B", "label": "That"}]}
        item.status = "blocked"
        await session.commit()
        item.needs_human = None
        item.ask = None
        item.payload = {"answer": {"option": "A", "label": "This", "answered_at": "now"}}
        item.status = "open"
        await session.commit()
        [row] = await _decisions(session, item.id)
        assert (row.kind, row.status) == ("work_item", "answered")
        assert row.answer == {"option": "A", "label": "This"}


async def test_a_second_pause_cancels_the_first_open_decision(sessions):
    make, _ = sessions
    async with make() as session:
        user, _agent = await _user_and_agent(session)
        item = WorkItem(id=uuid.uuid4(), user_id=user.id, title="Two asks", status="open", needs_human=True)
        session.add(item)
        await session.commit()
        item.needs_human = None
        await session.commit()
        item.needs_human = True
        await session.commit()
        rows = await _decisions(session, item.id)
        assert [r.status for r in rows] == ["cancelled", "open"]
        open_count = await session.scalar(
            select(func.count()).select_from(Decision).where(Decision.source_id == item.id, Decision.status == "open")
        )
        assert open_count == 1


async def test_refusal_is_audited_after_the_rollback_on_a_real_session(sessions, monkeypatch):
    from fastapi import HTTPException

    make, _ = sessions
    async with make() as session:
        user, _agent = await _user_and_agent(session)
        item = WorkItem(id=uuid.uuid4(), user_id=user.id, title="Refused", status="open", needs_human=True)
        session.add(item)
        await session.commit()
        user_id, item_id = user.id, item.id

    async def _refuse(*args, **kwargs):
        raise HTTPException(status_code=403, detail="not yours")

    monkeypatch.setattr(decision_router, "_dispatch", _refuse)
    async with make() as session:
        # The user and the decision row are loaded on the request session: the
        # rollback of the refusal audit expires both.
        person = await session.get(User, user_id)
        with pytest.raises(HTTPException) as exc:
            await decision_router.answer_decision(session, f"work_item:{item_id}", person, option="A", surface="bell")
        assert exc.value.status_code == 403
    async with make() as session:
        [row] = await _decisions(session, item_id)
        assert row.status == "open"
        assert await _events(session, row.id) == ["asked", "refused"]
        refused = (
            await session.execute(
                select(DecisionEvent).where(DecisionEvent.decision_id == row.id, DecisionEvent.event == "refused")
            )
        ).scalar_one()
        assert (refused.actor_id, refused.surface, refused.detail["status"]) == (user_id, "bell", 403)


async def test_downgrade_on_a_seeded_database_then_upgrade_again(sessions):
    make, engine = sessions
    async with make() as session:
        user, _agent = await _user_and_agent(session)
        session.add(WorkItem(id=uuid.uuid4(), user_id=user.id, title="Seeded", status="open", needs_human=True))
        await session.commit()
        assert await session.scalar(select(func.count()).select_from(Decision)) == 1
    revision = _load_revision()

    def _run(step):
        def _call(sync_conn) -> None:
            context = MigrationContext.configure(sync_conn)
            with Operations.context(context), context.begin_transaction():
                step()

        return _call

    async with engine.connect() as conn:
        await conn.run_sync(_run(revision.downgrade))
        await conn.commit()
    async with engine.connect() as conn:
        gone = await conn.scalar(
            text("SELECT to_regclass('decisions') IS NULL AND to_regclass('decision_events') IS NULL")
        )
        assert gone
        # The work item still holds its own pause: nothing a person must answer is lost.
        assert await conn.scalar(text("SELECT count(*) FROM work_items WHERE needs_human")) == 1
    async with engine.connect() as conn:
        await conn.run_sync(_run(revision.upgrade))
        await conn.commit()
        await conn.run_sync(_run(revision.upgrade))  # idempotent
        await conn.commit()
        assert await conn.scalar(text("SELECT count(*) FROM decisions")) == 0
