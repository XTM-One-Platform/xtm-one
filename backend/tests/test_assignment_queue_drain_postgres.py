"""The delivery-queue drain against real PostgreSQL.

``drain_assignment_queues`` picks at most ``ASSIGNMENT_QUEUE_DRAIN_BATCH``
assignments per cycle. It used to pick the ones with the OLDEST queued runs and
only then skip those a run still held, so as many held assignments as the batch
took every slot on every cycle and no other queue was drained again: in
production every pull request delivery of one assignment stayed ``queued``
while its sibling assignments ran. Which assignments the query selects is the
point, so it is run here, not against a mock. Same scratch-database recipe as
``test_assignment_stale_reference_postgres.py``.
"""

from __future__ import annotations

import asyncio
import uuid
from collections import Counter
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

import app.models  # noqa: F401 - registers every mapper on Base.metadata
from app.models.agent import Agent
from app.models.assignment import Assignment, AssignmentRun
from app.models.user import User
from app.services import assignment_engine as ae
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration

_T0 = datetime(2026, 9, 28, 19, 0, tzinfo=UTC)


async def _seed(session, *, held: int) -> tuple[list[uuid.UUID], uuid.UUID]:
    """``held`` assignments paused on an approval with an older backlog, and
    one idle assignment whose backlog is the newest."""
    user = User(id=uuid.uuid4(), email=f"drain-{uuid.uuid4().hex[:8]}@test.local", full_name="Drain", hashed_password="x")
    session.add(user)
    await session.flush()
    agent = Agent(id=uuid.uuid4(), name="Convention Guardian", user_id=user.id)
    session.add(agent)
    await session.flush()

    def assignment(name: str) -> Assignment:
        row = Assignment(
            id=uuid.uuid4(),
            agent_id=agent.id,
            user_id=user.id,
            name=name,
            trigger_type="webhook",
            prompt="p",
            trigger_config={},
        )
        session.add(row)
        return row

    held_ids = []
    for index in range(held):
        row = assignment(f"Held {index}")
        await session.flush()
        held_ids.append(row.id)
        session.add_all(
            [
                AssignmentRun(assignment_id=row.id, agent_id=agent.id, user_id=user.id, status="awaiting_human", created_at=_T0),
                AssignmentRun(
                    assignment_id=row.id,
                    agent_id=agent.id,
                    user_id=user.id,
                    status=ae.QUEUED_RUN_STATUS,
                    created_at=_T0 + timedelta(minutes=index),
                ),
            ]
        )
    idle = assignment("Validate new pull requests")
    await session.flush()
    session.add(
        AssignmentRun(
            assignment_id=idle.id,
            agent_id=agent.id,
            user_id=user.id,
            status=ae.QUEUED_RUN_STATUS,
            created_at=_T0 + timedelta(hours=1),
        )
    )
    await session.commit()
    return held_ids, idle.id


@pytest.fixture
def _promotions(monkeypatch):
    """Record promotions instead of creating orchestration executions."""
    promoted: list[uuid.UUID] = []

    async def _no_refusal(_db):
        return None

    async def _schedule(db, assignment, _agent, run, *, trigger_context=None, extra_delay_seconds=0):
        run.status = "pending"
        await db.commit()
        promoted.append(assignment.id)

    from app.services import agentic_gate

    monkeypatch.setattr(agentic_gate, "agentic_refusal", _no_refusal)
    monkeypatch.setattr(ae, "_schedule_run_execution", _schedule)
    monkeypatch.setattr(ae.settings, "assignment_queue_enabled", True)
    monkeypatch.setattr(ae.settings, "assignment_queue_hold_on_awaiting_human", True)
    return promoted


async def test_held_assignments_do_not_starve_the_drain(_promotions):
    async with scratch_database("qdrain", tables=ALL_TABLES, extensions=("vector",)) as db:
        async with db.sessions() as session:
            held_ids, idle_id = await _seed(session, held=3)

        assert await ae.drain_assignment_queues(limit=2) == 1
        assert _promotions == [idle_id]

        async with db.sessions() as session:
            statuses = dict(
                (
                    await session.execute(
                        select(AssignmentRun.assignment_id, AssignmentRun.status).where(
                            AssignmentRun.assignment_id.in_(held_ids),
                            AssignmentRun.status == ae.QUEUED_RUN_STATUS,
                        )
                    )
                ).all()
            )
        assert set(statuses) == set(held_ids), "a held assignment keeps its backlog"


async def test_an_assignment_released_by_its_run_is_drained_again(_promotions):
    async with scratch_database("qdrain", tables=ALL_TABLES, extensions=("vector",)) as db:
        async with db.sessions() as session:
            held_ids, idle_id = await _seed(session, held=1)
            paused = (
                await session.execute(
                    select(AssignmentRun).where(
                        AssignmentRun.assignment_id == held_ids[0],
                        AssignmentRun.status == "awaiting_human",
                    )
                )
            ).scalar_one()
            paused.status = "completed"
            await session.commit()

        assert await ae.drain_assignment_queues(limit=5) == 2
        assert sorted(_promotions) == sorted([held_ids[0], idle_id])


# ---------------------------------------------------------------------------
# Bounded concurrency, fair turns, idempotent promotion, ordering keys.
# ---------------------------------------------------------------------------


class _WorkerKilled(BaseException):
    """A worker dying mid-promotion: not an ``Exception``, so the drain's
    per-assignment guard does not swallow it and the transaction is lost."""


class _PromotedRuns(list[tuple[uuid.UUID, uuid.UUID]]):
    """``(assignment id, run id)`` of every promotion, in order. ``kill_on``
    (1-based call numbers) kills the worker inside those promotions, after the
    row changed and before the commit."""

    def __init__(self) -> None:
        super().__init__()
        self.kill_on: set[int] = set()
        self.calls = 0


@pytest.fixture
def _promoted_runs(monkeypatch) -> _PromotedRuns:
    from app.services import agentic_gate

    promoted = _PromotedRuns()

    async def _no_refusal(_db):
        return None

    async def _schedule(db, assignment, _agent, run, *, trigger_context=None, extra_delay_seconds=0):
        promoted.calls += 1
        run.status = "pending"
        if promoted.calls in promoted.kill_on:
            await db.flush()
            raise _WorkerKilled
        await db.commit()
        promoted.append((assignment.id, run.id))

    monkeypatch.setattr(agentic_gate, "agentic_refusal", _no_refusal)
    monkeypatch.setattr(ae, "_schedule_run_execution", _schedule)
    monkeypatch.setattr(ae.settings, "assignment_queue_enabled", True)
    monkeypatch.setattr(ae.settings, "assignment_queue_hold_on_awaiting_human", True)
    monkeypatch.setattr(ae.settings, "assignment_max_concurrent_runs", 8)
    monkeypatch.setattr(ae.settings, "assignment_dispatch_max_pending_runs", 10)
    return promoted


async def _seed_backlog(
    session, counts: dict[str, int], *, trigger_config: dict | None = None, keys: list[str | None] | None = None
) -> dict[str, uuid.UUID]:
    """Queued deliveries for each named assignment, interleaved by arrival
    (one per assignment in turn, a second apart). ``keys`` cycles over each
    assignment's runs as their ordering key."""
    user = User(id=uuid.uuid4(), email=f"load-{uuid.uuid4().hex[:8]}@test.local", full_name="Load", hashed_password="x")
    session.add(user)
    await session.flush()
    agent = Agent(id=uuid.uuid4(), name="Mail Desk", user_id=user.id)
    session.add(agent)
    await session.flush()
    ids: dict[str, uuid.UUID] = {}
    for name in counts:
        row = Assignment(
            id=uuid.uuid4(),
            agent_id=agent.id,
            user_id=user.id,
            name=name,
            trigger_type="integration_event",
            prompt="p",
            trigger_config=dict(trigger_config or {}),
        )
        session.add(row)
        ids[name] = row.id
    await session.flush()
    arrival = 0
    for index in range(max(counts.values())):
        for name, count in counts.items():
            if index >= count:
                continue
            key = keys[index % len(keys)] if keys else None
            session.add(
                AssignmentRun(
                    assignment_id=ids[name],
                    agent_id=agent.id,
                    user_id=user.id,
                    status=ae.QUEUED_RUN_STATUS,
                    trigger_context={"type": "integration_event", **({"ordering_key": key} if key else {})},
                    created_at=_T0 + timedelta(seconds=arrival),
                )
            )
            arrival += 1
    await session.commit()
    return ids


async def _finish_runs(db, run_ids) -> None:
    async with db.sessions() as session:
        runs = (await session.execute(select(AssignmentRun).where(AssignmentRun.id.in_(list(run_ids))))).scalars()
        for run in runs:
            run.status = "completed"
        await session.commit()


async def _statuses(db) -> dict[uuid.UUID, tuple[uuid.UUID, str]]:
    async with db.sessions() as session:
        rows = (await session.execute(select(AssignmentRun.id, AssignmentRun.assignment_id, AssignmentRun.status))).all()
    return {run_id: (assignment_id, status) for run_id, assignment_id, status in rows}


def _count(statuses, status: str, assignment_id: uuid.UUID | None = None) -> int:
    return sum(1 for a, s in statuses.values() if s == status and assignment_id in (None, a))


async def test_two_hundred_queued_deliveries_are_promoted_once_each_in_fair_turns(_promoted_runs):
    """Acceptance: 200 deliveries over 3 assignments are all promoted, more
    than one at a time per assignment, never past a cap, never twice, every
    free slot filled on every drain, and the three take turns."""
    counts = {"alerts": 120, "mail": 50, "tickets": 30}
    async with scratch_database("qdrain", tables=ALL_TABLES, extensions=("vector",)) as db:
        async with db.sessions() as session:
            ids = await _seed_backlog(session, counts)
        names = {value: key for key, value in ids.items()}

        while True:
            statuses = await _statuses(db)
            queued = {name: _count(statuses, ae.QUEUED_RUN_STATUS, ids[name]) for name in counts}
            before = len(_promoted_runs)
            promoted = await ae.drain_assignment_queues()
            if not any(queued.values()):
                assert promoted == 0
                break
            assert promoted == min(10, sum(min(8, q) for q in queued.values())), "every free slot is filled"
            taken = Counter(names[assignment_id] for assignment_id, _run in _promoted_runs[before:])
            assert max(taken.values()) <= 8, "the per-assignment cap holds"
            if all(queued[name] > taken[name] for name in counts):
                assert max(taken[n] for n in counts) - min(taken[n] for n in counts) <= 1, taken
            await _finish_runs(db, [run_id for _a, run_id in _promoted_runs[before:]])

        run_ids = [run_id for _a, run_id in _promoted_runs]
        assert len(run_ids) == len(set(run_ids)) == 200, "every delivery promoted exactly once"
        assert _count(await _statuses(db), "completed") == 200, "nothing left behind"


async def test_concurrent_drains_never_promote_a_run_twice_nor_past_the_cap(_promoted_runs):
    """Three reconcilers draining the same queue at once: every promotion
    re-checks the slots under the assignment's advisory lock."""
    async with scratch_database("qdrain", tables=ALL_TABLES, extensions=("vector",)) as db:
        async with db.sessions() as session:
            ids = await _seed_backlog(session, {"mail": 30})

        results = await asyncio.gather(*(ae.drain_assignment_queues() for _ in range(3)))

        run_ids = [run_id for _a, run_id in _promoted_runs]
        assert sum(results) == len(run_ids) == 8, "the cap holds across workers"
        assert len(set(run_ids)) == 8, "no run promoted twice"
        statuses = await _statuses(db)
        assert _count(statuses, "pending", ids["mail"]) == 8
        assert _count(statuses, ae.QUEUED_RUN_STATUS) == 22


async def test_a_drain_killed_mid_promotion_is_resumed_without_loss_or_duplicate(_promoted_runs):
    """A worker dying inside a promotion loses that transaction only: the run
    it was promoting is still queued, and the next drain resumes."""
    _promoted_runs.kill_on.add(3)
    async with scratch_database("qdrain", tables=ALL_TABLES, extensions=("vector",)) as db:
        async with db.sessions() as session:
            await _seed_backlog(session, {"mail": 12})

        with pytest.raises(_WorkerKilled):
            await ae.drain_assignment_queues()
        statuses = await _statuses(db)
        assert _count(statuses, "pending") == 2
        assert _count(statuses, ae.QUEUED_RUN_STATUS) == 10, "the killed promotion is undone"

        await _finish_runs(db, [run_id for _a, run_id in _promoted_runs])
        while promoted := await ae.drain_assignment_queues():
            await _finish_runs(db, [run_id for _a, run_id in _promoted_runs[-promoted:]])

        run_ids = [run_id for _a, run_id in _promoted_runs]
        assert len(run_ids) == len(set(run_ids)) == 12
        assert _count(await _statuses(db), "completed") == 12


async def test_runs_sharing_an_ordering_key_start_one_at_a_time_in_arrival_order(_promoted_runs):
    async with scratch_database("qdrain", tables=ALL_TABLES, extensions=("vector",)) as db:
        async with db.sessions() as session:
            ids = await _seed_backlog(session, {"threads": 8}, keys=["a", "b", None, "a"])
            runs = (
                await session.execute(
                    select(AssignmentRun.id, AssignmentRun.trigger_context)
                    .where(AssignmentRun.assignment_id == ids["threads"])
                    .order_by(AssignmentRun.created_at)
                )
            ).all()
        by_key: dict[str | None, list[uuid.UUID]] = {}
        for run_id, context in runs:
            by_key.setdefault((context or {}).get("ordering_key"), []).append(run_id)

        await ae.drain_assignment_queues()
        first = {run_id for _a, run_id in _promoted_runs}
        assert first == {by_key["a"][0], by_key["b"][0], *by_key[None]}, "one run per key, every keyless run"

        await _finish_runs(db, [by_key["a"][0]])
        before = len(_promoted_runs)
        await ae.drain_assignment_queues()
        assert [run_id for _a, run_id in _promoted_runs[before:]] == [by_key["a"][1]], "the key's next run, and only it"


# ---------------------------------------------------------------------------
# Startable runs, runs that are not deliveries, queue=False, /metrics.
# ---------------------------------------------------------------------------


async def _add_run(session, assignment_id: uuid.UUID, *, status: str, trigger_context: dict | None) -> uuid.UUID:
    assignment = await session.get(Assignment, assignment_id)
    run = AssignmentRun(
        assignment_id=assignment_id,
        agent_id=assignment.agent_id,
        user_id=assignment.user_id,
        status=status,
        trigger_context=trigger_context,
        created_at=_T0 - timedelta(minutes=1),
    )
    session.add(run)
    await session.commit()
    return run.id


async def test_a_queue_blocked_on_its_keys_takes_no_capacity_from_a_healthy_one(_promoted_runs):
    """A queue whose runs mostly wait for a key in flight used to take a share
    of the drain it could not use: the healthy queue beside it was starved of
    the difference. Only the runs that can start now count."""
    async with scratch_database("qdrain", tables=ALL_TABLES, extensions=("vector",)) as db:
        async with db.sessions() as session:
            blocked = (
                await _seed_backlog(session, {"blocked": 10}, keys=["a", "a", "a", "b", "a", None, "a", "b", None, "a"])
            )["blocked"]
            healthy = (await _seed_backlog(session, {"healthy": 10}))["healthy"]
            await _add_run(
                session, blocked, status="running", trigger_context={"type": "integration_event", "ordering_key": "a"}
            )
            states = {state.assignment_id: state for state in await ae._load_queue_states(session, only_integration_events=False)}

        # Unordered: 2; key "b": its head; key "a": in flight.
        assert (states[blocked].depth, states[blocked].startable, states[blocked].free) == (10, 3, 3)
        assert (states[healthy].startable, states[healthy].free) == (10, 8)

        assert await ae.drain_assignment_queues() == 10, "every slot of the pending capacity is used"
        taken = Counter(assignment_id for assignment_id, _run in _promoted_runs)
        assert taken == {blocked: 3, healthy: 7}


@pytest.mark.parametrize("trigger_context", [{"type": "manual"}, {"type": "followup"}, None])
async def test_a_run_that_is_not_a_delivery_holds_its_assignment_s_deliveries(_promoted_runs, trigger_context):
    """A manual, follow-up, scheduled or chained run - or one that says nothing
    of its type - has its assignment to itself."""
    async with scratch_database("qdrain", tables=ALL_TABLES, extensions=("vector",)) as db:
        async with db.sessions() as session:
            ids = await _seed_backlog(session, {"mail": 5})
            own = await _add_run(session, ids["mail"], status="running", trigger_context=trigger_context)
            usage = await ae._slot_usage(session, ids["mail"])
            (state,) = await ae._load_queue_states(session, only_integration_events=False)
            await session.commit()

        assert usage.exclusive and not usage.has_room(8)
        assert state.exclusive and state.free == 0
        assert await ae.drain_assignment_queues() == 0

        await _finish_runs(db, [own])
        assert await ae.drain_assignment_queues() == 5


async def test_without_queueing_a_delivery_never_overtakes_a_queued_run_of_its_thread(_promoted_runs, monkeypatch):
    async def _ai_on(_assignment):
        return None

    monkeypatch.setattr(ae, "_refuse_if_ai_disabled", _ai_on)
    async with scratch_database("qdrain", tables=ALL_TABLES, extensions=("vector",)) as db:
        async with db.sessions() as session:
            ids = await _seed_backlog(
                session, {"mail": 2}, trigger_config={"ordering_key": "thread"}, keys=["t1"]
            )

        async def deliver(thread: str) -> str:
            async with db.sessions() as session:
                assignment = await session.get(Assignment, ids["mail"])
                agent = await session.get(Agent, assignment.agent_id)
                result = await ae.trigger_or_queue_assignment(
                    assignment,
                    agent,
                    {"thread": thread},
                    db=session,
                    trigger_context={"type": "integration_event"},
                    queue=False,
                )
                return result.status

        assert await deliver("t1") == "dropped", "t1 is queued: it would run ahead of it"
        assert await deliver("t2") == "ok", "t2 waits for nothing: a free slot takes it"
        statuses = await _statuses(db)
        assert _count(statuses, ae.QUEUED_RUN_STATUS) == 2, "the queue is left as it was"
        assert _count(statuses, "pending") == 1


async def test_the_dispatch_metrics_carry_no_assignment_name():
    from prometheus_client import generate_latest

    from app.api.v1 import metrics
    from app.services import assignment_dispatch

    async with (
        scratch_database("qdrain", tables=ALL_TABLES, extensions=("vector",)) as db,
        db.sessions() as session,
    ):
        ids = await _seed_backlog(session, {"Q3 layoffs - private triage": 2})
        stats = await assignment_dispatch.read_dispatch_stats(session)
        await metrics._read_assignment_dispatch(session, "platform-1")

    (assignment_id,) = ids.values()
    assert "name" not in stats["assignments"][assignment_id]
    exposition = generate_latest(metrics.REGISTRY).decode()
    assert "layoffs" not in exposition
    assert f'xtmone_assignment_queue_depth{{assignment_id="{assignment_id}",platform_id="platform-1"}} 2.0' in exposition
