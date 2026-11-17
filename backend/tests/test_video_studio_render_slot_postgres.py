"""A render handed over by its stopping worker keeps its slot, against PostgreSQL.

The 22 Oct 2026 render was handed over by two deploys, at 72% and 81%, each time waiting 10-20 min "first in the
queue" while two renders the replacement worker had started meanwhile held the lane, its finished frames kept:

* the regression: a render at 80% whose worker gets SIGTERM, on a replacement worker that had filled both its slots
  with renders queued after it - 15 s after the hand-back, the render that took its slot last gives it back (queued
  again in its place, its frames kept) and the render resumes there within 60 s of its hand-back, its slot's age
  kept, before the render queued behind it; the one that gave its slot back takes none back;
* a running worker with a free slot keeps it for the render in flight on the stopping worker - nothing queued takes
  it - and the render takes it the moment it is handed back; two renders in flight on stopping workers are kept a
  slot each, one on each running worker;
* a render taken back from a worker gone without a word waits with the same priority.

``integration``-marked and skips when PostgreSQL is unreachable.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import update

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.core.config import settings
from app.models.orchestration import OrchestrationExecution, OrchestrationOutboxEvent
from app.services import orchestration_engine as engine
from app.services import orchestration_reconciler as reconciler
from app.services import worker_presence
from app.services.orchestration_adapters import video_studio as adapters
from app.services.video_studio import jobs
from app.services.video_studio.runners import JobOutcome
from tests.scratch_database import scratch_database

pytestmark = pytest.mark.integration

_RENDERS = jobs.RENDER_ADAPTER
#: The render lane as the reconciler reads it.
_LANE = SimpleNamespace(name=_RENDERS, keeps_slot_through=adapters.VideoRenderAdapter.keeps_slot_through)
OLD, NEW, OTHER = "worker-old:1:aaaaaaaa", "worker-new:1:bbbbbbbb", "worker-other:1:cccccccc"


class _Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 22, 14, 0, tzinfo=UTC)

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def world(monkeypatch):
    clock = _Clock()
    presence: dict[str, str] = {}
    # A worker's release, when a test names one: a record of a release that said none otherwise.
    versions: dict[str, str] = {}

    def _record(worker: str) -> dict:
        return {"worker_id": worker, "state": presence[worker], **({"version": versions[worker]} if worker in versions else {})}

    class _ClockedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock.now if tz is None else clock.now.astimezone(tz)

    async def present(*, at_most: int = 256):
        return [_record(worker) for worker in sorted(presence)]

    async def records(worker_ids):
        return {worker: (_record(worker) if worker in presence else None) for worker in worker_ids}

    async def nothing():
        return None

    monkeypatch.setattr(engine, "datetime", _ClockedDatetime)
    monkeypatch.setattr(reconciler, "datetime", _ClockedDatetime)
    monkeypatch.setattr(worker_presence, "present", present)
    monkeypatch.setattr(worker_presence, "records", records)
    monkeypatch.setattr(engine, "notify_orchestration_wake", nothing)
    monkeypatch.setattr(reconciler, "_slot_holders", {})
    return SimpleNamespace(clock=clock, presence=presence, versions=versions)


def _executions():
    return scratch_database("renderslot", tables=[OrchestrationExecution.__table__, OrchestrationOutboxEvent.__table__])


async def _queue(scratch, clock: _Clock) -> uuid.UUID:
    async with scratch.sessions() as db:
        execution = await engine.create_execution(
            db,
            adapter=_RENDERS,
            payload={"render_id": str(uuid.uuid4())},
            run_at=clock.now,
            deadline_at=clock.now + timedelta(hours=8),
            max_attempts=3,
        )
        await db.commit()
        return execution.id


async def _claim(scratch, worker: str, limit: int) -> list[uuid.UUID]:
    """What *worker*'s reconciler claims with *limit* slots free: those kept for the stopping workers' work left."""
    available = await reconciler._claimable_slots(_LANE, worker, limit)
    if available <= 0:
        return []
    async with scratch.sessions() as db:
        rows = await engine.claim_due(
            db, adapter=_RENDERS, worker_id=worker, limit=available, lease_seconds=jobs.RENDER_LEASE_SECONDS, keep_slot=True
        )
        claimed = {row.id: engine.slot_since(row) for row in rows}
        await db.commit()
    if worker == NEW:
        # As ``_do_execute`` registers each execution running in its own slot.
        for execution_id, since in claimed.items():
            reconciler._slot_holders[execution_id] = reconciler._SlotHolder(_RENDERS, since)
    return list(claimed)


async def _draws(scratch, execution_id: uuid.UUID, worker: str) -> None:
    """*worker*'s heartbeat on the render it draws: its lease renewed."""
    async with scratch.sessions() as db:
        await engine.extend_lease(db, execution_id=execution_id, worker_id=worker, lease_seconds=jobs.RENDER_LEASE_SECONDS)
        await db.commit()


async def _stamped(scratch, clock: _Clock) -> None:
    """Every row's last write at the test's clock: a write flushed twice gets the model's ``onupdate``, real time."""
    async with scratch.sessions() as db:
        await db.execute(
            update(OrchestrationExecution).where(OrchestrationExecution.updated_at > clock.now).values(updated_at=clock.now)
        )
        await db.commit()


async def _hand_back(scratch, clock: _Clock, execution_id: uuid.UUID, worker: str) -> None:
    """The stopping worker's hand-back, as the render adapter returns it (``AdapterResult.defer``, first in the queue)."""
    result = adapters.AdapterResult.defer(
        "Handed to the next worker at 80%.",
        delay_seconds=0.0,
        adapter_state=adapters.HANDED_OVER_STATE,
        payload_update=adapters.handed_over_by(worker),
        place="head",
    )
    async with scratch.sessions() as db:
        await reconciler._apply_result(db, execution_id, worker, result)
    await _stamped(scratch, clock)


async def _row(scratch, execution_id: uuid.UUID) -> OrchestrationExecution:
    async with scratch.sessions() as db:
        return await db.get(OrchestrationExecution, execution_id)


async def test_a_render_at_80_percent_survives_a_sigterm_its_slot_kept_and_resumes_within_60_seconds(world, monkeypatch):
    clock, presence = world.clock, world.presence
    async with _executions() as scratch:
        monkeypatch.setattr(reconciler, "async_session", scratch.sessions)
        presence[OLD] = worker_presence.STATE_RUNNING
        render = await _queue(scratch, clock)
        assert await _claim(scratch, OLD, 1) == [render]
        took_its_slot = engine.slot_since(await _row(scratch, render))
        # It draws for 10 min; meanwhile the replacement pod comes up and fills both its slots with renders queued
        # after it, and a third is queued.
        clock.advance(600)
        await _draws(scratch, render, OLD)
        first, second = await _queue(scratch, clock), await _queue(scratch, clock)
        presence[NEW] = worker_presence.STATE_RUNNING
        clock.advance(1)
        assert await _claim(scratch, NEW, 1) == [first]
        clock.advance(1)
        assert await _claim(scratch, NEW, 1) == [second]
        behind = await _queue(scratch, clock)
        # SIGTERM at 80%: the old worker drains, and its deployment's grace leaves no wait - it hands the render back.
        presence[OLD] = worker_presence.STATE_DRAINING
        clock.advance(20)
        await _hand_back(scratch, clock, render, OLD)
        handed_at = clock.now
        # A cycle or two for a worker with a free slot to take it first: none gives a slot back yet.
        assert await reconciler._give_back_slot(_LANE, NEW) is None
        clock.advance(reconciler.GIVE_BACK_AFTER_SECONDS + 1)
        # The render that took its slot last gives it back, the waiting render handed to this worker meanwhile.
        assert await reconciler._give_back_slot(_LANE, NEW) == second
        assert reconciler._slot_holders[second].give_back.is_set()
        assert not reconciler._slot_holders[first].give_back.is_set()
        waiting = await _row(scratch, render)
        assert waiting.payload[engine.CLAIM_FOR_PAYLOAD_KEY]["worker"] == NEW
        # Another worker asked meanwhile gives none back for it.
        assert await reconciler._give_back_slot(_LANE, OTHER) is None
        # Its render process stops, its frames kept (``render_process.give_slot_back``), and its execution goes back
        # to the queue in its place, due now.
        second_row = await _row(scratch, second)
        async with scratch.sessions() as db:
            await reconciler._apply_result(
                db, second, NEW, adapters._slot_given_back(JobOutcome("gave_back", "Gave its slot back at 3%."), NEW)
            )
        await _stamped(scratch, clock)
        del reconciler._slot_holders[second]
        clock.advance(2)
        # The freed slot takes the render back, before the one queued behind it and the one that gave its slot.
        assert await _claim(scratch, NEW, 1) == [render]
        resumed = await _row(scratch, render)
        assert resumed.lease_owner == NEW and resumed.state == "running"
        assert clock.now - handed_at < timedelta(seconds=60)
        # Its slot kept: the same age, before every render that took one after it.
        assert engine.slot_since(resumed) == took_its_slot
        assert engine.CLAIM_FOR_PAYLOAD_KEY not in resumed.payload
        gave = await _row(scratch, second)
        assert gave.state == "pending" and gave.adapter_state == jobs.SLOT_GIVEN_STATE
        assert gave.run_at == second_row.run_at and gave.attempt_count == second_row.attempt_count - 1
        assert (await _row(scratch, behind)).state == "pending"
        # The render that gave its slot back waits for the next one to free: it takes none back from anyone.
        clock.advance(reconciler.GIVE_BACK_AFTER_SECONDS + 1)
        assert await reconciler._give_back_slot(_LANE, NEW) is None
        # The next slot to free takes it before the render queued behind it.
        assert await _claim(scratch, OTHER, 1) == [second]


async def test_a_free_slot_is_kept_for_the_render_in_flight_on_a_stopping_worker(world, monkeypatch):
    clock, presence = world.clock, world.presence
    async with _executions() as scratch:
        monkeypatch.setattr(reconciler, "async_session", scratch.sessions)
        presence.update({OLD: worker_presence.STATE_RUNNING, NEW: worker_presence.STATE_RUNNING})
        render = await _queue(scratch, clock)
        assert await _claim(scratch, OLD, 1) == [render]
        clock.advance(300)
        await _draws(scratch, render, OLD)
        queued = await _queue(scratch, clock)
        presence[OLD] = worker_presence.STATE_DRAINING
        # The new worker's slot is the stopping worker's render's: the render queued after it does not take it.
        assert await reconciler._claimable_slots(_LANE, NEW, 1) == 0
        assert await _claim(scratch, NEW, 1) == []
        # A second free slot is anyone's.
        assert await reconciler._claimable_slots(_LANE, NEW, 2) == 1
        # Handed back: it takes the slot kept for it, first.
        clock.advance(30)
        await _hand_back(scratch, clock, render, OLD)
        assert await _claim(scratch, NEW, 1) == [render]
        assert (await _row(scratch, queued)).state == "pending"


async def test_two_renders_on_stopping_workers_are_kept_a_slot_each_one_on_each_running_worker(world, monkeypatch):
    clock, presence = world.clock, world.presence
    async with _executions() as scratch:
        monkeypatch.setattr(reconciler, "async_session", scratch.sessions)
        presence[OLD] = worker_presence.STATE_RUNNING
        one, two = await _queue(scratch, clock), await _queue(scratch, clock)
        assert await _claim(scratch, OLD, 2) == [one, two]
        presence.update({OLD: worker_presence.STATE_DRAINING, NEW: worker_presence.STATE_RUNNING, OTHER: worker_presence.STATE_RUNNING})
        assert await reconciler._slots_kept_elsewhere(_LANE, NEW) == 1
        assert await reconciler._slots_kept_elsewhere(_LANE, OTHER) == 1
        assert await reconciler._claimable_slots(_LANE, NEW, 2) == 1
        # Nothing to keep once no worker drains.
        presence[OLD] = worker_presence.STATE_RUNNING
        assert await reconciler._slots_kept_elsewhere(_LANE, NEW) == 0


async def test_the_slots_kept_for_a_draining_worker_are_kept_on_the_incoming_release(world, monkeypatch):
    """OTHER runs the draining worker's own release and is stopped by the same rollout next; NEW runs the
    incoming one. Both renders in flight on OLD are kept a slot on NEW, none on OTHER."""
    clock, presence, versions = world.clock, world.presence, world.versions
    async with _executions() as scratch:
        monkeypatch.setattr(reconciler, "async_session", scratch.sessions)
        presence[OLD] = worker_presence.STATE_RUNNING
        one, two = await _queue(scratch, clock), await _queue(scratch, clock)
        assert await _claim(scratch, OLD, 2) == [one, two]
        presence.update({OLD: worker_presence.STATE_DRAINING, NEW: worker_presence.STATE_RUNNING, OTHER: worker_presence.STATE_RUNNING})
        versions.update({OLD: "1.0.0-old", OTHER: "1.0.0-old", NEW: "1.0.1-new"})
        monkeypatch.setattr(settings, "app_version", "1.0.1-new")
        assert await reconciler._slots_kept_elsewhere(_LANE, NEW) == 2
        monkeypatch.setattr(settings, "app_version", "1.0.0-old")
        assert await reconciler._slots_kept_elsewhere(_LANE, OTHER) == 0


async def test_a_render_taken_back_from_a_worker_gone_waits_with_the_same_priority(world, monkeypatch):
    clock, presence = world.clock, world.presence
    async with _executions() as scratch:
        monkeypatch.setattr(reconciler, "async_session", scratch.sessions)
        presence[OLD] = worker_presence.STATE_RUNNING
        render = await _queue(scratch, clock)
        assert await _claim(scratch, OLD, 1) == [render]
        clock.advance(600)
        await _draws(scratch, render, OLD)
        queued = await _queue(scratch, clock)
        presence[NEW] = worker_presence.STATE_RUNNING
        clock.advance(1)
        assert await _claim(scratch, NEW, 1) == [queued]
        # Killed without a word: taken back, first in the queue, the attempt it spent counted.
        del presence[OLD]
        clock.advance(20)
        async with scratch.sessions() as db:
            await engine.reclaim_from_gone_owners(db, {OLD: {"state": "running"}}, adapters=[_RENDERS], limit=10)
            await db.commit()
        await _stamped(scratch, clock)
        assert await reconciler._give_back_slot(_LANE, NEW) is None
        clock.advance(reconciler.GIVE_BACK_AFTER_SECONDS + 1)
        assert await reconciler._give_back_slot(_LANE, NEW) == queued
        assert (await _row(scratch, render)).payload[engine.CLAIM_FOR_PAYLOAD_KEY]["worker"] == NEW
