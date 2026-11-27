"""The debounce of a coalesced burst (#2544), against real PostgreSQL.

An OpenCRQ batch recompute whose jobs finish over about five minutes reached
the dispatcher as one coalesced event per poll tick. Folding into a run that
has not started merged nothing once the run of the first tick had ended, so
each tick started a run of its own. A poller now declares a quiet period:
the run of the key is held (``delayed``, its orchestration execution's
``run_at`` moved on each fold) until no event of the key arrived for that
long, never past the cap from the burst's first event. Which run a fold
finds, the held execution and what a restart leaves are the point, so they
run here, not against a fake session. Same scratch-database recipe as
``test_event_run_dedup_postgres.py``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import app.models  # noqa: F401 - registers every mapper on Base.metadata
from app.models.agent import Agent
from app.models.assignment import Assignment, AssignmentRun
from app.models.orchestration import OrchestrationExecution
from app.models.user import User
from app.services import assignment_engine as ae
from app.services import event_coalescing, opencrq_job_events
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration

_T0 = datetime.now(UTC).replace(microsecond=0)
_EVENT = "risk_scenario_recomputed"
_CONTEXT = {"type": "integration_event", "provider": "opencrq", "event": _EVENT}


class _Clock:
    def __init__(self) -> None:
        self.at = _T0

    def __call__(self) -> datetime:
        return self.at


@pytest.fixture
def _clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    """The hold clock, moved by the test; AI on, no wake publish."""
    from app.services import agentic_gate

    clock = _Clock()

    async def _no_refusal(_db: object) -> None:
        return None

    async def _ai_on(_assignment: object) -> None:
        return None

    async def _no_wake() -> None:
        return None

    monkeypatch.setattr(event_coalescing, "now", clock)
    monkeypatch.setattr(agentic_gate, "agentic_refusal", _no_refusal)
    monkeypatch.setattr(ae, "_refuse_if_ai_disabled", _ai_on)
    monkeypatch.setattr(ae, "_wake_reconciler", _no_wake)
    return clock


def _price_run(run_id: str, minute: int) -> dict[str, Any]:
    finished = (_T0 + timedelta(minutes=minute)).isoformat().replace("+00:00", "Z")
    return {
        "id": run_id,
        "kind": "crq-price-recompute",
        "status": "succeeded",
        "finishedAt": finished,
        "processedTotal": 0,
        "expectedTotal": None,
        "currentStep": '{"moved": ["s1"], "movedCount": 1}',
    }


def _page(tick: int, size: int, *, provider_declares: bool = True) -> dict[str, Any]:
    runs = [_price_run(f"t{tick}-r{i}", tick) for i in range(size)]
    (event,) = opencrq_job_events.coalesce_events(runs, _EVENT, base_url="https://crq", tenant_id="t1")
    if not provider_declares:
        event.pop(event_coalescing.QUIET_KEY)
        event.pop(event_coalescing.MAX_KEY)
    return event


async def _seed(session: AsyncSession) -> tuple[Agent, Assignment]:
    user = User(
        id=uuid.uuid4(), email=f"burst-{uuid.uuid4().hex[:8]}@test.local", full_name="Burst", hashed_password="x"
    )
    session.add(user)
    await session.flush()
    agent = Agent(id=uuid.uuid4(), name="Risk Desk", user_id=user.id)
    session.add(agent)
    await session.flush()
    assignment = Assignment(
        id=uuid.uuid4(),
        agent_id=agent.id,
        user_id=user.id,
        name="Scenario re-priced",
        trigger_type="integration_event",
        prompt="p",
        trigger_config={"provider": "opencrq", "event": _EVENT, "max_concurrent_runs": 1},
    )
    session.add(assignment)
    await session.commit()
    return agent, assignment


async def _deliver(db: Any, ids: tuple[uuid.UUID, uuid.UUID], event: dict[str, Any]) -> ae.QueuedTriggerResult:
    async with db.sessions() as session:
        assignment = await session.get(Assignment, ids[1])
        agent = await session.get(Agent, ids[0])
        context = {**_CONTEXT, "message_id": event["message_id"], event_coalescing.CONTEXT_KEY: event["coalesce_key"]}
        return await ae.trigger_or_queue_assignment(
            assignment, agent, event, db=session, trigger_context=context, overflow="reject"
        )


async def _runs(db: Any) -> list[AssignmentRun]:
    async with db.sessions() as session:
        return list((await session.execute(select(AssignmentRun).order_by(AssignmentRun.created_at))).scalars())


async def _execution(db: Any, run_id: uuid.UUID) -> OrchestrationExecution:
    async with db.sessions() as session:
        execution: OrchestrationExecution = (
            await session.execute(
                select(OrchestrationExecution).where(OrchestrationExecution.dedup_key == f"assignment_run:{run_id}")
            )
        ).scalar_one()
    return execution


def _payload(run: AssignmentRun) -> dict[str, Any]:
    assert run.trigger_payload is not None
    return run.trigger_payload


def _context(run: AssignmentRun) -> dict[str, Any]:
    assert run.trigger_context is not None
    return run.trigger_context


async def _finish(db: Any, run_id: uuid.UUID) -> None:
    """The reconciler claims a due execution and the run ends."""
    async with db.sessions() as session:
        run = await session.get(AssignmentRun, run_id)
        run.status = "completed"
        execution = (
            await session.execute(
                select(OrchestrationExecution).where(OrchestrationExecution.dedup_key == f"assignment_run:{run_id}")
            )
        ).scalar_one()
        execution.state = "completed"
        await session.commit()


async def test_three_ticks_of_one_burst_start_one_run_after_the_quiet_period(_clock: _Clock) -> None:
    """Acceptance 1: three poll ticks 60 s apart are ONE run, held 300 s past
    the last, with run_count summed and covers listing the runs (newest 50)."""
    async with scratch_database("debounce", tables=ALL_TABLES, extensions=("vector",)) as db:
        async with db.sessions() as session:
            agent, assignment = await _seed(session)
        ids = (agent.id, assignment.id)

        statuses = []
        for tick, size in enumerate((16, 18, 30)):
            _clock.at = _T0 + timedelta(seconds=60 * tick)
            statuses.append((await _deliver(db, ids, _page(tick, size))).status)

        assert statuses == ["ok", "coalesced", "coalesced"]
        (run,) = await _runs(db)
        assert run.status == "delayed"
        assert _payload(run)["run_count"] == 64
        covers = _payload(run)["covers"]
        assert len(covers) == event_coalescing.MAX_COVERED
        assert covers[-1]["id"] == "t2-r29"
        hold = _context(run)[event_coalescing.HOLD_CONTEXT]
        assert hold["first_at"] == _T0.isoformat()
        assert hold["last_at"] == (_T0 + timedelta(seconds=120)).isoformat()
        execution = await _execution(db, run.id)
        assert execution.state == "pending"
        assert execution.run_at == _T0 + timedelta(seconds=120 + 300), "300 s after the last tick"


async def test_a_burst_longer_than_the_cap_is_one_run_per_window(_clock: _Clock) -> None:
    """Acceptance 2: a tick every minute for 20 minutes: the cap (900 s) ends
    the first window, the rest is the next run, never more."""
    async with scratch_database("debounce", tables=ALL_TABLES, extensions=("vector",)) as db:
        async with db.sessions() as session:
            agent, assignment = await _seed(session)
        ids = (agent.id, assignment.id)

        for tick in range(20):
            _clock.at = _T0 + timedelta(seconds=60 * tick)
            await _deliver(db, ids, _page(tick, 2))
            runs = await _runs(db)
            # The reconciler claims what is due at this instant.
            for run in runs:
                if run.status == "delayed" and (await _execution(db, run.id)).run_at <= _clock.at:
                    await _finish(db, run.id)

        runs = await _runs(db)
        assert len(runs) == 2, [r.status for r in runs]
        first, second = runs
        assert first.status == "completed"
        assert (await _execution(db, first.id)).run_at == _T0 + timedelta(seconds=900), "the cap ends the window"
        assert _payload(first)["run_count"] == 2 * 15, "ticks 0 to 14"
        # Opened while the first still held the slot: it waits in the queue,
        # keeps folding the burst and takes its hold when the drain promotes it.
        assert second.status == ae.QUEUED_RUN_STATUS
        assert _payload(second)["run_count"] == 2 * 5, "ticks 15 to 19"
        hold = _context(second)[event_coalescing.HOLD_CONTEXT]
        assert hold["first_at"] == (_T0 + timedelta(seconds=900)).isoformat()
        assert event_coalescing.hold_seconds(second.trigger_context, _clock.at) == 300


async def test_an_event_without_the_declaration_is_dispatched_as_before(_clock: _Clock) -> None:
    """Acceptance 3: no quiet period declared, no hold: one page starts its run
    at once, and the next page starts the next run once the first ended."""
    async with scratch_database("debounce", tables=ALL_TABLES, extensions=("vector",)) as db:
        async with db.sessions() as session:
            agent, assignment = await _seed(session)
        ids = (agent.id, assignment.id)

        assert (await _deliver(db, ids, _page(0, 3, provider_declares=False))).status == "ok"
        (run,) = await _runs(db)
        assert run.status == "pending"
        assert event_coalescing.HOLD_CONTEXT not in _context(run)
        assert (await _execution(db, run.id)).run_at <= datetime.now(UTC)
        await _finish(db, run.id)

        _clock.at = _T0 + timedelta(seconds=60)
        assert (await _deliver(db, ids, _page(1, 3, provider_declares=False))).status == "ok"
        assert [r.status for r in await _runs(db)] == ["completed", "pending"], "one run per page, as before"


async def test_a_held_burst_survives_a_restart_once(_clock: _Clock) -> None:
    """Acceptance 5: the hold is the run row and its execution, so a worker
    restart (a new session, nothing in memory) neither loses the held burst
    nor starts it twice: a page delivered after it folds into the same run,
    and a page read again (same runs) adds nothing."""
    async with scratch_database("debounce", tables=ALL_TABLES, extensions=("vector",)) as db:
        async with db.sessions() as session:
            agent, assignment = await _seed(session)
        ids = (agent.id, assignment.id)

        first_page = _page(0, 4)
        await _deliver(db, ids, first_page)
        # The worker dies: nothing but PostgreSQL remains. The next worker's
        # poll reads the page again past a backed-off cursor, then the next one.
        _clock.at = _T0 + timedelta(seconds=60)
        assert (await _deliver(db, ids, first_page)).status == "coalesced"
        assert (await _deliver(db, ids, _page(1, 3))).status == "coalesced"

        (run,) = await _runs(db)
        assert run.status == "delayed"
        assert _payload(run)["run_count"] == 7, "the page read twice counts once"
        execution = await _execution(db, run.id)
        assert execution.state == "pending" and execution.lease_owner is None
        assert execution.run_at == _T0 + timedelta(seconds=60 + 300)
