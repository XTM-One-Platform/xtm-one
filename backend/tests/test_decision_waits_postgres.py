"""Long waits of a human gate against real PostgreSQL (#842, S7; human-gates.md section 7, F3).

- a scheduled run paused past N hours no longer holds its assignment (the
  overlap guard and the delivery queue) and still resumes on answer;
- a chat-started run paused as long still holds;
- a decision past its deadline expires with the default option, else "no
  answer", and its run is resumed with that as the response.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select

import app.models  # noqa: F401 - registers every mapper on Base.metadata
from app.models.agent import Agent
from app.models.assignment import Assignment, AssignmentRun
from app.models.decision import Decision, DecisionEvent
from app.models.user import User
from app.services import assignment_engine as ae
from app.services import decision_waits as dw
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration


async def _seed(
    session: Any,
    *,
    trigger_type: str,
    paused_hours: float,
    deadline: datetime | None = None,
    ask: Any = None,
    kind: str | None = None,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    user = User(id=uuid.uuid4(), email=f"f3-{uuid.uuid4().hex[:8]}@test.local", full_name="F3", hashed_password="x")
    session.add(user)
    await session.flush()
    agent = Agent(id=uuid.uuid4(), name="Weekly digest", user_id=user.id)
    session.add(agent)
    await session.flush()
    assignment = Assignment(
        id=uuid.uuid4(),
        agent_id=agent.id,
        user_id=user.id,
        name="Digest",
        trigger_type="schedule",
        prompt="p",
        trigger_config={},
    )
    session.add(assignment)
    await session.flush()
    run = AssignmentRun(
        assignment_id=assignment.id,
        agent_id=agent.id,
        user_id=user.id,
        status="awaiting_human",
        trigger_context={"type": trigger_type},
        human_input_question="Publish the report?",
    )
    session.add(run)
    await session.flush()
    # The decision the pause opened (by the dual-write hook when it ran, else
    # written here), aged as if the pause began ``paused_hours`` ago.
    decision = (
        await session.execute(
            select(Decision).where(
                Decision.source_type == "run", Decision.source_id == run.id, Decision.status == "open"
            )
        )
    ).scalar_one_or_none()
    if decision is None:
        decision = Decision(
            source_type="run",
            source_id=run.id,
            kind="hitl",
            ask={"question": "Publish the report?"},
            recipient=f"user:{user.id}",
            status="open",
        )
        session.add(decision)
    decision.created_at = datetime.now(UTC) - timedelta(hours=paused_hours)
    decision.deadline = deadline
    if kind is not None:
        decision.kind = kind
    if ask is not None:
        decision.ask = ask
    await session.commit()
    return assignment.id, run.id, decision.id


@pytest.fixture
def _four_hours(monkeypatch):
    monkeypatch.setattr(dw.settings, "human_gate_queue_release_hours", 4)


async def test_scheduled_run_paused_past_n_hours_frees_its_assignment(_four_hours) -> None:
    async with scratch_database("f3wait", tables=ALL_TABLES, extensions=("vector",)) as db:
        async with db.sessions() as session:
            assignment_id, run_id, _ = await _seed(session, trigger_type="schedule", paused_hours=5)
        async with db.sessions() as session:
            assert await ae.has_active_run(session, assignment_id) is False
            assert await ae.count_active_runs(session, assignment_id) == 0
            assert await ae.find_active_run(session, assignment_id) is None
            # Still paused and answerable: nothing about the run changed.
            run = await session.get(AssignmentRun, run_id)
            assert run.status == "awaiting_human"


async def test_scheduled_run_paused_under_n_hours_still_holds(_four_hours) -> None:
    async with scratch_database("f3wait", tables=ALL_TABLES, extensions=("vector",)) as db:
        async with db.sessions() as session:
            assignment_id, _, _ = await _seed(session, trigger_type="schedule", paused_hours=1)
        async with db.sessions() as session:
            assert await ae.has_active_run(session, assignment_id) is True


async def test_chat_started_run_is_unaffected(_four_hours) -> None:
    async with scratch_database("f3wait", tables=ALL_TABLES, extensions=("vector",)) as db:
        async with db.sessions() as session:
            assignment_id, _, _ = await _seed(session, trigger_type="chat_handover", paused_hours=48)
        async with db.sessions() as session:
            assert await ae.has_active_run(session, assignment_id) is True


async def test_event_run_paused_on_a_tool_approval_frees_its_assignment_at_once(_four_hours: None) -> None:
    # #1647: the approval gate asks before the tool runs; later events of the assignment run.
    async with scratch_database("f3wait", tables=ALL_TABLES, extensions=("vector",)) as db:
        async with db.sessions() as session:
            assignment_id, run_id, _ = await _seed(
                session, trigger_type="platform_event", paused_hours=0, kind="tool_approval"
            )
        async with db.sessions() as session:
            assert await ae.has_active_run(session, assignment_id) is False
            run = await session.get(AssignmentRun, run_id)
            assert run.status == "awaiting_human"


async def test_chat_started_tool_approval_still_holds(_four_hours: None) -> None:
    async with scratch_database("f3wait", tables=ALL_TABLES, extensions=("vector",)) as db:
        async with db.sessions() as session:
            assignment_id, _, _ = await _seed(
                session, trigger_type="chat_handover", paused_hours=0, kind="tool_approval"
            )
        async with db.sessions() as session:
            assert await ae.has_active_run(session, assignment_id) is True


async def test_re_ask_after_a_rollout_keeps_the_release_clock(_four_hours: None) -> None:
    # #1647: a run that leaves its pause unanswered and asks the same question again
    # keeps the first asking time, so the 4 h release does not restart.
    async with scratch_database("f3wait", tables=ALL_TABLES, extensions=("vector",)) as db:
        async with db.sessions() as session:
            assignment_id, run_id, first_id = await _seed(session, trigger_type="schedule", paused_hours=5)
        async with db.sessions() as session:
            first = await session.get(Decision, first_id)
            asked_at = first.created_at
            run = await session.get(AssignmentRun, run_id)
            run.status = "running"
            await session.flush()
            run.status = "awaiting_human"
            await session.commit()
        async with db.sessions() as session:
            reopened = (
                await session.execute(
                    select(Decision).where(
                        Decision.source_type == "run", Decision.source_id == run_id, Decision.status == "open"
                    )
                )
            ).scalar_one()
            assert reopened.id != first_id
            assert reopened.created_at == asked_at
            assert await ae.has_active_run(session, assignment_id) is False


async def _expire_and_read(db, monkeypatch, **seed):
    resumed: list[uuid.UUID] = []

    async def _resume(session, run):
        resumed.append(run.id)
        await session.commit()

    monkeypatch.setattr(ae, "resume_assignment_run", _resume)

    async def _no_void(_run_id):
        return None

    from app.services import push_action_tokens

    monkeypatch.setattr(push_action_tokens, "void_run_tokens", _no_void)
    async with db.sessions() as session:
        _, run_id, decision_id = await _seed(session, trigger_type="schedule", **seed)
    assert await dw.expire_overdue_decisions() == 1
    async with db.sessions() as session:
        decision = await session.get(Decision, decision_id)
        run = await session.get(AssignmentRun, run_id)
        events = (
            (await session.execute(select(DecisionEvent.event).where(DecisionEvent.decision_id == decision_id)))
            .scalars()
            .all()
        )
    return decision, run, events, resumed


async def test_decision_past_its_deadline_expires_with_the_default_option(_four_hours, monkeypatch) -> None:
    ask = {
        "question": "Publish the report?",
        "options": [{"key": "A", "label": "Publish"}, {"key": "B", "label": "Hold"}],
        "recommendation": "B",
    }
    async with scratch_database("f3exp", tables=ALL_TABLES, extensions=("vector",)) as db:
        decision, run, events, resumed = await _expire_and_read(
            db, monkeypatch, paused_hours=1, deadline=datetime.now(UTC) - timedelta(minutes=1), ask=ask
        )
    assert decision.status == "expired"
    assert decision.answer == {"option": "B", "label": "Hold"}
    assert "expired" in events and "answered" not in events
    assert run.human_input_response == "B"
    assert run.human_input_history[-1]["resolution"] == "expired"
    assert resumed == [run.id]


async def test_decision_past_its_deadline_without_default_resumes_with_no_answer(_four_hours, monkeypatch) -> None:
    async with scratch_database("f3exp", tables=ALL_TABLES, extensions=("vector",)) as db:
        decision, run, _, resumed = await _expire_and_read(
            db, monkeypatch, paused_hours=1, deadline=datetime.now(UTC) - timedelta(minutes=1)
        )
    assert decision.status == "expired"
    assert run.human_input_response == dw.NO_ANSWER
    assert resumed == [run.id]


async def test_decision_before_its_deadline_is_left_open(_four_hours) -> None:
    async with scratch_database("f3exp", tables=ALL_TABLES, extensions=("vector",)) as db:
        async with db.sessions() as session:
            await _seed(
                session, trigger_type="schedule", paused_hours=1, deadline=datetime.now(UTC) + timedelta(hours=1)
            )
        assert await dw.expire_overdue_decisions() == 0
