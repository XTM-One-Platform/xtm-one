# SPDX-License-Identifier: Apache-2.0

"""Human gates S2b (#842) against PostgreSQL: the backfill, its count check, the read switch, the rollback.

A gate opened before ``c942decisions01`` has no ``decisions`` row: each test
seeds the engine's columns, removes what the dual write wrote (the state of a
database upgraded from before S2), runs the backfill and reads the rows back.
"""

from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import select, text
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
from app.services import decision_backfill, decision_router
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration

_REVISION = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "c943decisionsbf1_backfill_open_decisions.py"


def _load_revision():
    spec = importlib.util.spec_from_file_location("c943decisionsbf1", _REVISION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
async def sessions():
    decision_hooks._HAS_TABLE.clear()
    async with scratch_database(
        "decisions_s2b", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True
    ) as db:
        yield async_sessionmaker(db.engine, expire_on_commit=False), db.engine
    decision_hooks._HAS_TABLE.clear()


async def _forget_decisions(session: AsyncSession) -> None:
    """The database as an upgrade from before S2 left it: open gates, no decisions."""
    await session.execute(text("DELETE FROM decision_events"))
    await session.execute(text("DELETE FROM decisions"))
    await session.commit()


async def _seed_legacy_gates(session: AsyncSession) -> dict[str, uuid.UUID]:
    user = User(id=uuid.uuid4(), email=f"{uuid.uuid4().hex[:8]}@example.com", full_name="Reviewer")
    session.add(user)
    await session.flush()
    agent = Agent(id=uuid.uuid4(), user_id=user.id, name="Gatekeeper")
    flow = Flow(id=uuid.uuid4(), user_id=user.id, name="F", graph={"nodes": [], "edges": []})
    session.add_all([agent, flow])
    await session.flush()
    ids = {name: uuid.uuid4() for name in ("hitl", "approval", "answered_run", "flow", "item", "done_item", "user")}
    ids["user"] = user.id
    session.add_all(
        [
            AssignmentRun(
                id=ids["hitl"],
                agent_id=agent.id,
                user_id=user.id,
                status="awaiting_human",
                human_input_question="Merge now?",
            ),
            AssignmentRun(
                id=ids["approval"],
                agent_id=agent.id,
                user_id=user.id,
                status="awaiting_human",
                human_input_question="Approval required to run x",
                pending_approvals=[{"tool_call_id": "c1", "tool_name": "x"}],
            ),
            # Answered while the engine had not resumed yet: nothing to ask again.
            AssignmentRun(
                id=ids["answered_run"],
                agent_id=agent.id,
                user_id=user.id,
                status="awaiting_human",
                human_input_question="Already answered",
                human_input_response="yes",
            ),
            FlowRun(
                id=ids["flow"],
                flow_id=flow.id,
                user_id=user.id,
                status="awaiting",
                trigger_type="manual",
                state={"waiting": {"kind": "human", "node": "n1", "question": "Ship?", "released": False}},
            ),
            WorkItem(
                id=ids["item"],
                user_id=user.id,
                title="Pick one",
                status="blocked",
                needs_human=True,
                ask={
                    "question": "Which?",
                    "options": [{"key": "A", "label": "This"}, {"key": "B", "label": "That"}],
                    "deadline": "2026-12-01T09:00:00+00:00",
                },
            ),
            WorkItem(id=ids["done_item"], user_id=user.id, title="Finished", status="done", needs_human=True),
        ]
    )
    await session.commit()
    await _forget_decisions(session)
    return ids


def _sync(engine_conn, fn):
    return engine_conn.run_sync(lambda sync_conn: fn(sync_conn))


async def _open_rows(session: AsyncSession) -> dict[uuid.UUID, Decision]:
    rows = (await session.execute(select(Decision).where(Decision.status == "open"))).scalars()
    return {row.source_id: row for row in rows}


async def test_each_source_is_backfilled_and_answered_or_finished_ones_are_skipped(sessions) -> None:
    make, engine = sessions
    async with make() as session:
        ids = await _seed_legacy_gates(session)
    async with engine.begin() as conn:
        written = await _sync(conn, decision_backfill.backfill)
    assert written == {"run": 2, "flow_run": 1, "work_item": 1}
    async with make() as session:
        rows = await _open_rows(session)
        assert set(rows) == {ids["hitl"], ids["approval"], ids["flow"], ids["item"]}
        assert (rows[ids["hitl"]].kind, rows[ids["hitl"]].recipient) == ("hitl", f"user:{ids['user']}")
        assert rows[ids["approval"]].kind == "tool_approval"
        assert [o["key"] for o in rows[ids["approval"]].ask["options"]] == ["approve", "approve_always", "reject"]
        assert (rows[ids["flow"]].kind, rows[ids["flow"]].ask["question"]) == ("flow_ask", "Ship?")
        item = rows[ids["item"]]
        assert item.kind == "work_item" and item.deadline is not None and item.ask["options"][0]["key"] == "A"
        events = (await session.execute(select(DecisionEvent))).scalars().all()
        assert len(events) == 4
        assert {(e.event, e.surface, e.detail["source"]) for e in events} == {("asked", "backfill", "backfill")}


async def test_a_second_run_is_idempotent_and_the_count_check_is_complete(sessions) -> None:
    make, engine = sessions
    async with make() as session:
        await _seed_legacy_gates(session)
    async with engine.begin() as conn:
        report = await _sync(conn, decision_backfill.backfill_report)
        assert {name: count.missing_count for name, count in report.items()} == {
            "run": 2,
            "flow_run": 1,
            "work_item": 1,
        }
        assert not decision_backfill.report_complete(report)
        await _sync(conn, lambda c: decision_backfill.backfill(c, batch_size=1))
        again = await _sync(conn, decision_backfill.backfill)
        assert again == {"run": 0, "flow_run": 0, "work_item": 0}
        report = await _sync(conn, decision_backfill.backfill_report)
        assert decision_backfill.report_complete(report)
        assert report["run"].open_sources == 2 and report["run"].with_decision == 2
    async with make() as session:
        assert len((await session.execute(select(Decision))).scalars().all()) == 4


async def test_read_switch_is_off_by_default_and_on_only_with_a_complete_count(sessions) -> None:
    make, engine = sessions
    async with make() as session:
        await _seed_legacy_gates(session)
    async with engine.begin() as conn:
        off = await _sync(conn, decision_backfill.read_switch)
        assert off["on"] is False and off["setting"] is False
        incomplete = await _sync(conn, lambda c: decision_backfill.read_switch(c, enabled=True))
        assert incomplete["on"] is False and "backfill" in incomplete["reason"]
        assert incomplete["sources"]["work_item"]["missing"] == 1
        await _sync(conn, decision_backfill.backfill)
        on = await _sync(conn, lambda c: decision_backfill.read_switch(c, enabled=True))
        assert on["on"] is True and on["reason"] is None


async def test_a_legacy_pause_answered_after_the_backfill_closes_its_decision(sessions) -> None:
    make, engine = sessions
    async with make() as session:
        ids = await _seed_legacy_gates(session)
    async with engine.begin() as conn:
        await _sync(conn, decision_backfill.backfill)
    async with make() as session:
        # The router finds the backfilled row by the public id the old pause carries.
        _ref, row = await decision_router.resolve(session, f"work_item:{ids['item']}")
        assert row is not None and row.status == "open"
        # The engine answers on its own columns, as answer_work_item does.
        item = await session.get(WorkItem, ids["item"])
        item.needs_human = None
        item.ask = None
        item.payload = {"answer": {"option": "B", "label": "That", "answered_at": "now"}}
        item.status = "open"
        run = await session.get(AssignmentRun, ids["hitl"])
        run.human_input_response = "go"
        await session.commit()
        # The hook closes the row with a Core update: the row resolve loaded is stale.
        session.expire_all()
        answered = {
            r.source_id: r
            for r in (await session.execute(select(Decision).where(Decision.status == "answered"))).scalars()
        }
        assert answered[ids["item"]].answer == {"option": "B", "label": "That"}
        assert answered[ids["hitl"]].answer == {"text": "go"}


async def test_rollback_switch_off_then_downgrade_loses_nothing(sessions) -> None:
    make, engine = sessions
    async with make() as session:
        ids = await _seed_legacy_gates(session)
    revision = _load_revision()

    def _run(step):
        def _call(sync_conn) -> None:
            context = MigrationContext.configure(sync_conn)
            with Operations.context(context), context.begin_transaction():
                step()

        return _call

    async with engine.connect() as conn:
        await conn.run_sync(_run(revision.upgrade))
        await conn.commit()
    async with make() as session:
        # One gate answered after the backfill: its row is audit and stays.
        item = await session.get(WorkItem, ids["item"])
        item.needs_human = None
        item.payload = {"answer": {"option": "A", "label": "This", "answered_at": "now"}}
        item.status = "open"
        await session.commit()
    async with engine.connect() as conn:
        await conn.run_sync(_run(revision.downgrade))
        await conn.commit()
        assert await conn.scalar(text("SELECT count(*) FROM decisions WHERE status = 'open'")) == 0
        assert await conn.scalar(text("SELECT count(*) FROM decisions WHERE status = 'answered'")) == 1
        # Every engine still holds its own pause.
        assert await conn.scalar(text("SELECT count(*) FROM assignment_runs WHERE status = 'awaiting_human'")) == 3
        assert await conn.scalar(text("SELECT count(*) FROM flow_runs WHERE status = 'awaiting'")) == 1
        switch = await conn.run_sync(lambda c: decision_backfill.read_switch(c, enabled=True))
        assert switch["on"] is False
        # Upgrading again restores the same open gates.
        await conn.run_sync(_run(revision.upgrade))
        await conn.commit()
        assert await conn.scalar(text("SELECT count(*) FROM decisions WHERE status = 'open'")) == 3
