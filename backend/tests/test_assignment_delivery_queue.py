"""Tests for the per-assignment webhook delivery queue.

Before this queue, ``POST /api/hooks/assignments/{id}`` answered HTTP 200 and
created NO run whenever the assignment already had an active run. The sender —
typically a SIEM/SOAR firing on every alert — recorded a success for a run that
never happened, and the only trace was an INFO log. "One run per alert" was
unachievable by design.

The queue supplies the semantics the overlap guard deliberately declines to
provide, and these tests pin each of them:

* **Ordering** — FIFO per assignment, decided under the SAME per-assignment
  advisory lock the guard already takes, so enqueue and drain cannot race.
* **Bounds** — a depth cap with an explicit overflow policy. Dropping is
  allowed; dropping *silently* is not.
* **Backpressure** — 429 + ``Retry-After`` once the queue is full, so a sender
  with retry logic actually backs off (senders retry a 429, never a 200).
* **HITL** — ``awaiting_human`` holds the queue by default, and that hold is
  configurable.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.models.agent import Agent
from app.models.assignment import Assignment, AssignmentRun
from app.services import assignment_dispatch
from app.services import assignment_engine as ae


@pytest.fixture(autouse=True)
def _ai_enabled(monkeypatch):
    """AI on, without a database.

    ``trigger_assignment`` and ``trigger_or_queue_assignment`` now refuse when
    AI is switched off — gated at the service so the tick, the manual
    route and the external webhooks all inherit it. Resolving that opens a
    session, which these suites do not have; they are about delay, queueing and
    run-as semantics behind the gate. The queue drain asks the same background
    gate, so that is pinned open too.
    """
    from app.services import assignment_engine as _engine
    from tests.agentic_gate_stub import stub_agentic_gate

    async def _never_refuse(_assignment) -> bool:
        return False

    monkeypatch.setattr(_engine, "_refuse_if_ai_disabled", _never_refuse)
    stub_agentic_gate(monkeypatch)


# ── Test doubles ─────────────────────────────────────────────────────────────


class _FakeResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def scalar_one(self):
        return self._value

    def scalar(self):
        return self._value

    def scalars(self):
        return self

    def all(self):
        return self._value if isinstance(self._value, list) else []


class _FakeSession:
    """AsyncSession stub returning queued results in FIFO order.

    Statements with no queued result (the advisory-lock ``SELECT
    pg_advisory_xact_lock(...)``) resolve to ``None``. Every statement is
    recorded so tests can assert on lock ordering.
    """

    def __init__(self, results=()):
        self.results = list(results)
        self.statements = []
        self.commits = 0
        self.added = []

    async def execute(self, stmt, params=None):
        self.statements.append(stmt)
        # The advisory lock is a side-effecting SELECT with no result the code
        # reads — it must not consume a queued result, or every test would have
        # to pad its result list with a placeholder per lock acquisition.
        if "pg_advisory_xact_lock" in str(stmt):
            return _FakeResult(None)
        if not self.results:
            return _FakeResult(None)
        return _FakeResult(self.results.pop(0))

    def lock_statements(self):
        return [s for s in self.statements if "pg_advisory_xact_lock" in str(s)]

    def add(self, obj):
        self.added.append(obj)
        if getattr(obj, "id", None) is None:
            obj.id = uuid.uuid4()

    async def flush(self):
        return None

    async def commit(self):
        self.commits += 1

    async def __aenter__(self):
        return self

    async def __aexit__(self, _exc_type, _exc, _tb):
        return False


def _build_agent(enabled: bool = True) -> Agent:
    return Agent(id=uuid.uuid4(), name="Alert Triage Agent", enabled=enabled)


def _build_assignment(agent_id: uuid.UUID, enabled: bool = True) -> Assignment:
    return Assignment(
        id=uuid.uuid4(),
        agent_id=agent_id,
        name="SecOps Alert Intake",
        trigger_type="webhook",
        trigger_config={},
        prompt="Triage the alert",
        enabled=enabled,
    )


def _build_queued_run(assignment_id: uuid.UUID) -> AssignmentRun:
    return AssignmentRun(
        id=uuid.uuid4(),
        assignment_id=assignment_id,
        agent_id=uuid.uuid4(),
        status=ae.QUEUED_RUN_STATUS,
        trigger_payload={"alert": "id-1"},
    )


def _patch_engine(monkeypatch):
    """Capture create_execution + notify_orchestration_wake calls."""
    created: list[dict] = []

    async def _mock_create_execution(db, **kwargs):
        created.append(kwargs)
        return SimpleNamespace(id=uuid.uuid4())

    async def _mock_wake():
        return None

    monkeypatch.setattr(ae, "create_execution", _mock_create_execution)
    monkeypatch.setattr(ae, "notify_orchestration_wake", _mock_wake)
    monkeypatch.setattr(ae.settings, "scheduled_assignment_spread_seconds", 0)
    return created


def _usage(active: int = 0, keys: tuple[str, ...] = (), *, exclusive: bool = False) -> ae._SlotUsage:
    return ae._SlotUsage(active=active, busy_keys=frozenset(keys), exclusive=exclusive)


def _patch_slots(monkeypatch, *, active: int = 0, keys: tuple[str, ...] = (), exclusive: bool = False):
    """Stub the runs holding the assignment's slots."""

    async def _probe(_db, _assignment_id):
        return _usage(active, keys, exclusive=exclusive)

    monkeypatch.setattr(ae, "_slot_usage", _probe)


def _patch_queue_state(monkeypatch, *, busy: bool, depth: int):
    """Stub the two DB probes the enqueue decision is made on: the runs holding
    the assignment's slots (``busy`` takes every one of them) and its queue depth."""

    async def _count(_db, _assignment_id):
        return depth

    _patch_slots(monkeypatch, active=assignment_dispatch.MAX_CONCURRENCY_CAP if busy else 0)
    monkeypatch.setattr(ae, "count_queued_runs", _count)


# ── Status vocabulary ────────────────────────────────────────────────────────


def test_queued_is_neither_active_nor_terminal():
    """``queued`` must sit outside BOTH sets.

    In ``ACTIVE_RUN_STATUSES`` it would make ``has_active_run`` true for a
    queue that has nothing running, so the queue would block itself and never
    drain. In ``TERMINAL_RUN_STATUSES`` ``execute_assignment_run`` would refuse
    to run it once promoted.
    """
    assert ae.QUEUED_RUN_STATUS not in ae.ACTIVE_RUN_STATUSES
    assert ae.QUEUED_RUN_STATUS not in ae.TERMINAL_RUN_STATUSES


def test_queued_is_not_swept_as_a_stale_run():
    """Waiting in the queue is not being stuck.

    The stale-run sweep force-fails runs left in an active status with no live
    execution — exactly the shape of a queued run, which owns no execution row
    by design. It must not be a candidate.
    """
    from app.services.autonomous import STALE_ASSIGNMENT_ACTIVE_STATUSES

    assert ae.QUEUED_RUN_STATUS not in STALE_ASSIGNMENT_ACTIVE_STATUSES


# ── HITL hold ────────────────────────────────────────────────────────────────


def test_awaiting_human_holds_the_queue_by_default(monkeypatch):
    """A HITL-paused run still owns the assignment's tools and DB writes, so
    releasing the queue past it would reintroduce the parallel-run race the
    overlap guard exists to prevent. What changed is that the hold is now
    bounded and visible instead of a silent, unbounded loss of intake."""
    monkeypatch.setattr(
        ae.settings, "assignment_queue_hold_on_awaiting_human", True
    )
    assert "awaiting_human" in ae._queue_blocking_statuses()


def test_awaiting_human_hold_is_configurable(monkeypatch):
    monkeypatch.setattr(
        ae.settings, "assignment_queue_hold_on_awaiting_human", False
    )
    blocking = ae._queue_blocking_statuses()
    assert "awaiting_human" not in blocking
    # Every other active status still holds — only the HITL pause is released.
    assert set(blocking) == set(ae.ACTIVE_RUN_STATUSES) - {"awaiting_human"}


# ── trigger_or_queue_assignment: the happy path ──────────────────────────────


@pytest.mark.asyncio
async def test_runs_immediately_when_idle(monkeypatch):
    agent = _build_agent()
    assignment = _build_assignment(agent.id)
    created = _patch_engine(monkeypatch)
    _patch_queue_state(monkeypatch, busy=False, depth=0)
    session = _FakeSession()

    result = await ae.trigger_or_queue_assignment(
        assignment=assignment,
        agent=agent,
        payload={"alert": "a"},
        db=session,
        trigger_context={"type": "webhook"},
    )

    assert result.status == "ok"
    assert result.queue_depth == 0
    assert len(session.added) == 1
    run = session.added[0]
    assert str(run.id) == result.run_id
    # An idle assignment must behave EXACTLY like the legacy trigger: the run
    # goes straight to the orchestration engine, not through the queue.
    assert run.status == "pending"
    assert len(created) == 1
    assert created[0]["dedup_key"] == f"assignment_run:{result.run_id}"


@pytest.mark.asyncio
async def test_queues_when_a_run_is_active(monkeypatch):
    """The regression this issue is about: a delivery arriving mid-run used to
    vanish behind a 200. It must now become a real, durable run row."""
    agent = _build_agent()
    assignment = _build_assignment(agent.id)
    created = _patch_engine(monkeypatch)
    _patch_queue_state(monkeypatch, busy=True, depth=0)
    session = _FakeSession()

    result = await ae.trigger_or_queue_assignment(
        assignment=assignment,
        agent=agent,
        payload={"alert": "b"},
        db=session,
        trigger_context={"type": "webhook"},
    )

    assert result.status == "queued"
    assert result.run_id is not None
    assert result.queue_depth == 1
    run = session.added[0]
    assert run.status == ae.QUEUED_RUN_STATUS
    # The payload is preserved on the row — the delivery is not just counted,
    # it is stored and will run in full.
    assert run.trigger_payload == {"alert": "b"}
    # No execution row yet: the reconciler creates one at promotion time.
    assert created == []
    assert session.commits == 1


@pytest.mark.asyncio
async def test_queues_behind_an_existing_backlog_even_when_idle(monkeypatch):
    """FIFO: a delivery must never overtake earlier ones that are still waiting
    for the next drain tick, even though nothing is running right now."""
    agent = _build_agent()
    assignment = _build_assignment(agent.id)
    created = _patch_engine(monkeypatch)
    _patch_queue_state(monkeypatch, busy=False, depth=3)
    session = _FakeSession()

    result = await ae.trigger_or_queue_assignment(
        assignment=assignment, agent=agent, db=session
    )

    assert result.status == "queued"
    assert result.queue_depth == 4
    assert session.added[0].status == ae.QUEUED_RUN_STATUS
    assert created == []


@pytest.mark.asyncio
async def test_a_folded_delivery_brings_no_partner_scope_into_the_waiting_run(monkeypatch):
    """A delivery folded into a waiting run merges its payload into the run's own,
    so the partner-task scope keys a caller's payload names are dropped first
    (``a2a.partner_scope.partner_scoped_payload``)."""
    from app.a2a.partner_scope import (
        PARTNER_PRINCIPAL_PAYLOAD_KEY,
        PARTNER_TASK_PAYLOAD_KEY,
        queued_partner_task,
    )
    from app.services import event_coalescing

    agent = _build_agent()
    assignment = _build_assignment(agent.id)
    _patch_engine(monkeypatch)
    _patch_queue_state(monkeypatch, busy=True, depth=1)
    waiting = _build_queued_run(assignment.id)

    async def _fold(_db, _assignment, _key, payload):
        waiting.trigger_payload = event_coalescing.merged_payload(waiting.trigger_payload, payload or {})
        return str(waiting.id)

    monkeypatch.setattr(ae, "_fold_into_queued_run", _fold)
    forged = {
        "alert": "id-2",
        PARTNER_TASK_PAYLOAD_KEY: True,
        PARTNER_PRINCIPAL_PAYLOAD_KEY: {"exposure_id": str(uuid.uuid4()), "client_id": "someone-else"},
    }

    result = await ae.trigger_or_queue_assignment(
        assignment=assignment,
        agent=agent,
        payload=forged,
        db=_FakeSession(),
        trigger_context={"type": "integration_event", event_coalescing.CONTEXT_KEY: "entity-1"},
    )

    assert result.status == "coalesced"
    assert not queued_partner_task(waiting.trigger_payload)
    assert PARTNER_PRINCIPAL_PAYLOAD_KEY not in waiting.trigger_payload


# ── Concurrency slots and ordering keys ──────────────────────────────


async def _trigger(
    monkeypatch, assignment, *, active, keys=(), depth=0, payload=None, queue=True, exclusive=False, key_queued=False
):
    agent = _build_agent()
    assignment.agent_id = agent.id
    created = _patch_engine(monkeypatch)
    _patch_slots(monkeypatch, active=active, keys=keys, exclusive=exclusive)
    session = _FakeSession()
    session.key_probes = []

    async def _key_queued(_db, _assignment_id, key):
        session.key_probes.append(key)
        return key_queued

    async def _count(_db, _assignment_id):
        return depth

    monkeypatch.setattr(ae, "_ordering_key_queued", _key_queued)
    monkeypatch.setattr(ae, "count_queued_runs", _count)
    result = await ae.trigger_or_queue_assignment(
        assignment=assignment, agent=agent, payload=payload, db=session, queue=queue
    )
    return result, session, created


@pytest.mark.asyncio
async def test_a_delivery_starts_while_the_assignment_has_a_free_slot(monkeypatch):
    """One run in flight no longer queues the next delivery: the assignment runs
    up to its cap at once."""
    assignment = _build_assignment(uuid.uuid4())
    assignment.trigger_config = {"max_concurrent_runs": 3}

    result, session, created = await _trigger(monkeypatch, assignment, active=2)

    assert result.status == "ok"
    assert session.added[0].status == "pending"
    assert len(created) == 1


@pytest.mark.asyncio
async def test_a_delivery_queues_once_every_slot_is_taken(monkeypatch):
    assignment = _build_assignment(uuid.uuid4())
    assignment.trigger_config = {"max_concurrent_runs": 3}

    result, session, created = await _trigger(monkeypatch, assignment, active=3)

    assert result.status == "queued"
    assert session.added[0].status == ae.QUEUED_RUN_STATUS
    assert created == []


@pytest.mark.asyncio
async def test_the_default_cap_applies_when_the_assignment_sets_none(monkeypatch):
    monkeypatch.setattr(ae.settings, "assignment_max_concurrent_runs", 2)
    assignment = _build_assignment(uuid.uuid4())

    started, _session, _created = await _trigger(monkeypatch, assignment, active=1)
    queued, _session, _created = await _trigger(monkeypatch, assignment, active=2)

    assert (started.status, queued.status) == ("ok", "queued")


@pytest.mark.asyncio
async def test_a_delivery_whose_ordering_key_is_in_flight_queues_behind_it(monkeypatch):
    """Runs sharing an ordering key run one at a time, in arrival order, while
    the assignment's other slots stay open to other keys."""
    assignment = _build_assignment(uuid.uuid4())
    assignment.trigger_config = {"max_concurrent_runs": 5, "ordering_key": "ticket.id"}

    same, session, _created = await _trigger(
        monkeypatch, assignment, active=1, keys=("T-1",), payload={"ticket": {"id": "T-1"}}
    )
    assert same.status == "queued"
    assert session.added[0].trigger_context[assignment_dispatch.ORDERING_KEY_CONTEXT] == "T-1"

    other, session, _created = await _trigger(
        monkeypatch, assignment, active=1, keys=("T-1",), payload={"ticket": {"id": "T-2"}}
    )
    assert other.status == "ok"
    assert session.added[0].trigger_context[assignment_dispatch.ORDERING_KEY_CONTEXT] == "T-2"


@pytest.mark.asyncio
async def test_without_queueing_a_delivery_with_no_free_slot_is_dropped_not_queued(monkeypatch):
    """``queue=False`` (a pushed event with the event queue off): the skip of a
    busy assignment stands, and nothing is ever queued."""
    assignment = _build_assignment(uuid.uuid4())
    assignment.trigger_config = {"max_concurrent_runs": 2}

    dropped, session, created = await _trigger(monkeypatch, assignment, active=2, queue=False)
    assert dropped.status == "dropped"
    assert session.added == [] and created == []
    assert session.commits == 1

    started, session, created = await _trigger(monkeypatch, assignment, active=1, queue=False)
    assert started.status == "ok"
    assert len(created) == 1


@pytest.mark.asyncio
async def test_without_queueing_a_delivery_never_overtakes_a_queued_run_of_its_ordering_key(monkeypatch):
    """``queue=False`` reads the queue too: a delivery whose key a queued
    run carries is dropped rather than run ahead of it, while a free slot still
    takes one whose key waits for nothing."""
    assignment = _build_assignment(uuid.uuid4())
    assignment.trigger_config = {"max_concurrent_runs": 4, "ordering_key": "thread"}

    behind, session, created = await _trigger(
        monkeypatch, assignment, active=1, depth=2, payload={"thread": "t1"}, queue=False, key_queued=True
    )
    assert behind.status == "dropped"
    assert session.key_probes == ["t1"]
    assert session.added == [] and created == []

    free, session, created = await _trigger(
        monkeypatch, assignment, active=1, depth=2, payload={"thread": "t2"}, queue=False, key_queued=False
    )
    assert free.status == "ok" and len(created) == 1


@pytest.mark.asyncio
async def test_without_queueing_an_unordered_delivery_may_start_ahead_of_the_queue(monkeypatch):
    """Dropping it for a queue it could not join would lose it for nothing: the
    documented overtake of ``queue=False``."""
    assignment = _build_assignment(uuid.uuid4())
    assignment.trigger_config = {"max_concurrent_runs": 4}

    result, session, created = await _trigger(monkeypatch, assignment, active=1, depth=3, queue=False)

    assert result.status == "ok" and len(created) == 1
    assert session.key_probes == []


@pytest.mark.asyncio
async def test_a_run_that_is_not_a_delivery_keeps_every_slot_of_its_assignment(monkeypatch):
    """A manual, follow-up, scheduled or chained run in flight: deliveries wait
    for it, whatever the cap."""
    assignment = _build_assignment(uuid.uuid4())
    assignment.trigger_config = {"max_concurrent_runs": 10}

    queued, _session, created = await _trigger(monkeypatch, assignment, active=1, exclusive=True)
    assert queued.status == "queued" and created == []

    dropped, _session, created = await _trigger(monkeypatch, assignment, active=1, exclusive=True, queue=False)
    assert dropped.status == "dropped" and created == []


class _RowsSession:
    """A session whose one query answers fixed (status, ordering key, type) rows."""

    def __init__(self, rows):
        self._rows = rows

    async def execute(self, _statement):
        rows = self._rows
        return SimpleNamespace(all=lambda: rows)


@pytest.mark.asyncio
async def test_a_platform_event_run_in_flight_leaves_the_other_slots_free():
    """Three integration events and a platform event in flight: the next
    platform event starts while the cap has room, never queued behind them."""
    rows = [
        ("running", None, "integration_event", False),
        ("running", None, "integration_event", False),
        ("running", None, "integration_event", False),
        ("running", "run:abc", "platform_event", False),
    ]
    usage = await ae._slot_usage(_RowsSession(rows), uuid.uuid4())
    assert usage.exclusive is False and usage.active == 4
    assert usage.has_room(10) and usage.busy_keys == frozenset({"run:abc"})


@pytest.mark.asyncio
async def test_a_manual_run_in_flight_still_keeps_the_assignment_to_itself():
    usage = await ae._slot_usage(_RowsSession([("running", None, "manual", False)]), uuid.uuid4())
    assert usage.exclusive is True and not usage.has_room(10)


@pytest.mark.asyncio
async def test_a_handed_back_platform_event_run_holds_an_uncapped_assignment():
    """#2221: a platform event run handed back by a rollout (still running, resuming
    from its checkpoint) keeps an assignment with no max_concurrent_runs to itself:
    the release event of the same rollout queues behind it instead of starting beside it."""
    usage = await ae._slot_usage(_RowsSession([("running", None, "platform_event", True)]), uuid.uuid4())
    assert usage.resuming is True
    assert not usage.has_room(10, resume_holds=ae.resume_holds_assignment({"event": "platform.release.deployed"}))
    # An author-set cap is kept: the resuming run takes one slot like any other.
    assert usage.has_room(10, resume_holds=ae.resume_holds_assignment({"max_concurrent_runs": 3}))


@pytest.mark.asyncio
async def test_an_ordinary_platform_event_run_leaves_an_uncapped_assignment_free():
    usage = await ae._slot_usage(_RowsSession([("running", None, "platform_event", None)]), uuid.uuid4())
    assert usage.resuming is False
    assert usage.has_room(10, resume_holds=ae.resume_holds_assignment({}))


def test_the_resuming_clause_reads_the_hand_back_and_resume_keys():
    from app.services.background_checkpoint import HANDED_BACK_KEY, RESUMES_KEY

    compiled = str(ae._resuming_run_clause().compile(compile_kwargs={"literal_binds": True}))
    assert HANDED_BACK_KEY in compiled and RESUMES_KEY in compiled


@pytest.mark.asyncio
async def test_a_platform_event_delivery_queues_behind_a_resuming_run(monkeypatch):
    """The enqueue decision: a free slot but a resuming run in flight queues the delivery,
    and the same delivery starts once the run is no longer resuming."""
    assignment = _build_assignment(uuid.uuid4())
    assignment.trigger_config = {}

    async def _resuming(_db, _assignment_id):
        return ae._SlotUsage(active=1, busy_keys=frozenset(), resuming=True)

    result, session, created = await _trigger(monkeypatch, assignment, active=1)
    assert result.status == "ok" and len(created) == 1

    monkeypatch.setattr(ae, "_slot_usage", _resuming)
    agent = _build_agent()
    assignment.agent_id = agent.id
    session = _FakeSession()
    queued = await ae.trigger_or_queue_assignment(assignment=assignment, agent=agent, db=session)
    assert queued.status == "queued"
    assert session.added[0].status == ae.QUEUED_RUN_STATUS



def _patch_skip_probes(monkeypatch, *, queued: bool) -> list[str]:
    probes: list[str] = []

    async def _active(_db, _assignment_id, user_id=None):
        probes.append("active")
        return False

    async def _queued(_db, _assignment_id):
        probes.append("queued")
        return queued

    monkeypatch.setattr(ae, "has_active_run", _active)
    monkeypatch.setattr(ae, "_has_promotable_queued_run", _queued)
    return probes


@pytest.mark.asyncio
async def test_a_delivery_skipped_behind_a_queued_one_is_handed_to_the_queue(monkeypatch):
    """With nothing in flight but deliveries still queued, a delivery must not
    start ahead of them: the skip hands it to ``trigger_or_queue_assignment``,
    which queues it behind them."""
    agent = _build_agent()
    assignment = _build_assignment(agent.id)
    created = _patch_engine(monkeypatch)
    probes = _patch_skip_probes(monkeypatch, queued=True)
    session = _FakeSession()

    run_id = await ae.trigger_assignment(
        assignment=assignment,
        agent=agent,
        payload={"alert": "x"},
        db=session,
        skip_if_active=True,
        trigger_context={"type": "integration_event"},
    )

    assert run_id is None
    assert probes == ["active", "queued"]
    assert session.added == [] and created == []


@pytest.mark.asyncio
async def test_a_trigger_that_is_not_a_delivery_does_not_wait_for_the_queue(monkeypatch):
    """A schedule tick or a manual run keeps the guard it always had: an active
    run skips it, a queued delivery does not."""
    agent = _build_agent()
    assignment = _build_assignment(agent.id)
    created = _patch_engine(monkeypatch)
    probes = _patch_skip_probes(monkeypatch, queued=True)
    session = _FakeSession()

    run_id = await ae.trigger_assignment(
        assignment=assignment,
        agent=agent,
        payload={},
        db=session,
        skip_if_active=True,
        trigger_context={"type": "schedule"},
    )

    assert run_id is not None
    assert probes == ["active"]
    assert len(created) == 1


@pytest.mark.asyncio
async def test_a_mail_delivery_started_at_once_holds_its_thread(monkeypatch):
    """Two mails of one thread never run side by side: the key is read
    off the payload even when the delivery starts without queueing."""
    agent = _build_agent()
    assignment = _build_assignment(agent.id)
    assignment.trigger_type = "integration_event"
    assignment.trigger_config = {"provider": "microsoft", "event": "new_email"}
    _patch_engine(monkeypatch)
    _patch_skip_probes(monkeypatch, queued=False)
    session = _FakeSession()

    await ae.trigger_assignment(
        assignment=assignment,
        agent=agent,
        payload={"id": "m1", "conversation_id": "AAQkAD-thread"},
        db=session,
        skip_if_active=True,
        trigger_context={"type": "integration_event"},
    )

    (run,) = session.added
    assert run.trigger_context[assignment_dispatch.ORDERING_KEY_CONTEXT] == "AAQkAD-thread"


@pytest.mark.asyncio
async def test_a_provider_429_halves_the_cap_a_delivery_is_decided_on(monkeypatch):
    assignment = _build_assignment(uuid.uuid4())
    assignment.trigger_config = {"max_concurrent_runs": 4}

    async def _throttled(ids):
        return set(ids)

    monkeypatch.setattr(assignment_dispatch, "read_throttles", _throttled)
    monkeypatch.setattr(assignment_dispatch, "_throttle_cache", {})
    result, _session, _created = await _trigger(monkeypatch, assignment, active=2)

    assert result.status == "queued"


@pytest.mark.asyncio
async def test_decision_is_made_under_the_advisory_lock(monkeypatch):
    """Without the lock held BEFORE the probes, two concurrent deliveries could
    each observe a free slot and both take it."""
    agent = _build_agent()
    assignment = _build_assignment(agent.id)
    _patch_engine(monkeypatch)
    session = _FakeSession()
    locks_at_probe: list[int] = []

    async def _slots(_db, _assignment_id):
        locks_at_probe.append(len(session.lock_statements()))
        return _usage()

    async def _count(_db, _assignment_id):
        return 0

    monkeypatch.setattr(ae, "_slot_usage", _slots)
    monkeypatch.setattr(ae, "count_queued_runs", _count)

    await ae.trigger_or_queue_assignment(
        assignment=assignment, agent=agent, db=session
    )

    assert locks_at_probe == [1]


# ── Overflow policies ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_full_queue_rejects_with_retry_after(monkeypatch):
    """Default policy. 429 + Retry-After is the only outcome under which a
    burst is eventually delivered in full: senders retry a 429 and never a
    200."""
    agent = _build_agent()
    assignment = _build_assignment(agent.id)
    created = _patch_engine(monkeypatch)
    monkeypatch.setattr(ae.settings, "assignment_queue_max_depth", 5)
    monkeypatch.setattr(ae.settings, "assignment_queue_overflow_policy", "reject")
    monkeypatch.setattr(ae.settings, "assignment_queue_retry_after_seconds", 45)
    _patch_queue_state(monkeypatch, busy=True, depth=5)
    session = _FakeSession()

    result = await ae.trigger_or_queue_assignment(
        assignment=assignment, agent=agent, db=session
    )

    assert result.status == "queue_full"
    assert result.run_id is None
    assert result.retry_after_seconds == 45
    assert session.added == []
    assert created == []
    # The advisory lock must be released before the caller answers 429 —
    # otherwise the rejected delivery holds it while the retry waits on it.
    assert session.commits == 1


@pytest.mark.asyncio
async def test_full_queue_drop_newest_creates_nothing(monkeypatch):
    agent = _build_agent()
    assignment = _build_assignment(agent.id)
    _patch_engine(monkeypatch)
    monkeypatch.setattr(ae.settings, "assignment_queue_max_depth", 2)
    monkeypatch.setattr(
        ae.settings, "assignment_queue_overflow_policy", "drop_newest"
    )
    _patch_queue_state(monkeypatch, busy=True, depth=2)
    session = _FakeSession()

    result = await ae.trigger_or_queue_assignment(
        assignment=assignment, agent=agent, db=session
    )

    assert result.status == "dropped"
    assert result.run_id is None
    assert session.added == []


@pytest.mark.asyncio
async def test_full_queue_drop_oldest_evicts_the_head(monkeypatch, caplog):
    """The evicted delivery is finalized as ``skipped`` rather than deleted, so
    the loss stays visible as a run row carrying its payload."""
    agent = _build_agent()
    assignment = _build_assignment(agent.id)
    _patch_engine(monkeypatch)
    monkeypatch.setattr(ae.settings, "assignment_queue_max_depth", 2)
    monkeypatch.setattr(
        ae.settings, "assignment_queue_overflow_policy", "drop_oldest"
    )

    oldest = _build_queued_run(assignment.id)
    depths = iter([2, 1])

    async def _count(_db, _assignment_id):
        return next(depths)

    _patch_slots(monkeypatch, active=assignment_dispatch.MAX_CONCURRENCY_CAP)
    monkeypatch.setattr(ae, "count_queued_runs", _count)
    # The eviction's "oldest queued run" SELECT is the only statement that
    # consumes a queued result here.
    session = _FakeSession(results=[oldest])

    with caplog.at_level("WARNING"):
        result = await ae.trigger_or_queue_assignment(
            assignment=assignment, agent=agent, db=session
        )

    assert result.status == "queued"
    assert result.dropped_run_id == str(oldest.id)
    assert oldest.status == "skipped"
    assert oldest.error.startswith(ae.QUEUE_DROP_ERROR_PREFIX)
    assert oldest.completed_at is not None
    # Dropping is allowed; dropping silently is not.
    assert any("drop_oldest" in r.getMessage() for r in caplog.records)
    # The new delivery still got its slot.
    assert session.added[0].status == ae.QUEUED_RUN_STATUS
    # ONE commit for the whole path: the eviction and the insert that replaced
    # it must land together. Committing between them would briefly expose a
    # queue one short of its own cap, and would drop the advisory lock
    # mid-decision so a concurrent delivery could take the freed slot.
    assert session.commits == 1


@pytest.mark.asyncio
async def test_max_depth_cannot_be_disabled(monkeypatch):
    """A 0/negative cap must clamp to 1, not mean "unbounded" — an unbounded
    queue is a memory/DB growth hazard during exactly the alert storm the cap
    exists for."""
    monkeypatch.setattr(ae.settings, "assignment_queue_max_depth", 0)
    assert ae._queue_max_depth() == 1
    monkeypatch.setattr(ae.settings, "assignment_queue_retry_after_seconds", 0)
    assert ae._queue_retry_after_seconds() == 1


# ── Drain ────────────────────────────────────────────────────────────────────


def _candidates(*runs: AssignmentRun) -> list[tuple]:
    """The ``_queued_candidates`` rows for *runs*: id, created_at, ordering key."""
    return [(run.id, run.created_at, None) for run in runs]


def _patch_drain_counts(monkeypatch):
    async def _count(_db, _assignment_id):
        return 0

    monkeypatch.setattr(ae, "count_queued_runs", _count)


@pytest.mark.asyncio
async def test_drain_promotes_the_oldest_queued_run(monkeypatch):
    agent = _build_agent()
    assignment = _build_assignment(agent.id)
    created = _patch_engine(monkeypatch)
    queued = _build_queued_run(assignment.id)
    _patch_slots(monkeypatch)
    _patch_drain_counts(monkeypatch)
    # Statement order: advisory lock (no result) -> assignment -> agent ->
    # the queued candidates -> the chosen run.
    session = _FakeSession(results=[assignment, agent, _candidates(queued), queued])

    promoted = await ae._drain_one_assignment(session, assignment.id)

    assert promoted == 1
    # Promotion goes through the SAME scheduling path as an immediate trigger.
    assert queued.status == "pending"
    assert len(created) == 1
    assert created[0]["dedup_key"] == f"assignment_run:{queued.id}"
    # Lock held while choosing which run to promote, so two workers draining
    # the same assignment cannot each promote a different one.
    assert len(session.lock_statements()) == 1


@pytest.mark.asyncio
async def test_drain_fills_every_free_slot_taking_the_lock_before_each_promotion(monkeypatch):
    """Up to ``count`` runs start in one drain, each decided under the lock
    against the slots as they are after the previous promotion."""
    agent = _build_agent()
    assignment = _build_assignment(agent.id)
    assignment.trigger_config = {"max_concurrent_runs": 3}
    created = _patch_engine(monkeypatch)
    _patch_drain_counts(monkeypatch)
    runs = [_build_queued_run(assignment.id) for _ in range(4)]
    in_flight: list[int] = []

    async def _slots(_db, _assignment_id):
        return _usage(active=len(in_flight))

    monkeypatch.setattr(ae, "_slot_usage", _slots)

    async def _create_execution(_db, **kwargs):
        in_flight.append(1)
        created.append(kwargs)
        return SimpleNamespace(id=uuid.uuid4())

    monkeypatch.setattr(ae, "create_execution", _create_execution)
    results: list = []
    for index in range(3):
        waiting = runs[index:]
        # ... then the platform iteration cap ``_schedule_run_execution`` reads
        # and the agent's home fleet (no fleet: its run defaults).
        results += [assignment, agent, _candidates(*waiting), waiting[0], None, None]
    # The fourth pass finds every slot taken and promotes nothing.
    results += [assignment, agent]
    session = _FakeSession(results=results)

    promoted = await ae._drain_one_assignment(session, assignment.id, count=4)

    assert promoted == 3
    assert [run.status for run in runs] == ["pending", "pending", "pending", ae.QUEUED_RUN_STATUS]
    assert len(session.lock_statements()) == 4


@pytest.mark.asyncio
async def test_drain_is_a_noop_while_every_slot_is_taken(monkeypatch):
    agent = _build_agent()
    assignment = _build_assignment(agent.id)
    created = _patch_engine(monkeypatch)
    _patch_slots(monkeypatch, active=assignment_dispatch.MAX_CONCURRENCY_CAP)
    session = _FakeSession(results=[assignment, agent])

    assert await ae._drain_one_assignment(session, assignment.id) == 0
    assert created == []
    # Committed so the advisory lock is released immediately.
    assert session.commits == 1


@pytest.mark.asyncio
async def test_drain_leaves_a_run_whose_ordering_key_is_in_flight(monkeypatch):
    agent = _build_agent()
    assignment = _build_assignment(agent.id)
    created = _patch_engine(monkeypatch)
    _patch_slots(monkeypatch, active=1, keys=("T-1",))
    queued = _build_queued_run(assignment.id)
    session = _FakeSession(results=[assignment, agent, [(queued.id, queued.created_at, "T-1")]])

    assert await ae._drain_one_assignment(session, assignment.id) == 0
    assert queued.status == ae.QUEUED_RUN_STATUS
    assert created == []


@pytest.mark.asyncio
async def test_drain_abandons_a_delivery_whose_assignment_disappeared(monkeypatch):
    """A queued row nothing will ever promote is the same silent loss the queue
    was built to eliminate — it must be finalized with an explicit reason."""
    assignment = _build_assignment(uuid.uuid4())
    created = _patch_engine(monkeypatch)
    queued = _build_queued_run(assignment.id)
    # The assignment lookup returns nothing, then the queued runs to abandon.
    session = _FakeSession(results=[None, [queued]])

    assert await ae._drain_one_assignment(session, assignment.id) == 0
    assert queued.status == "skipped"
    assert queued.error.startswith(ae.QUEUE_DROP_ERROR_PREFIX)
    assert queued.completed_at is not None
    assert created == []


@pytest.mark.asyncio
async def test_drain_abandons_a_delivery_whose_agent_was_disabled(monkeypatch):
    assignment = _build_assignment(uuid.uuid4())
    agent = _build_agent(enabled=False)
    _patch_engine(monkeypatch)
    queued = _build_queued_run(assignment.id)
    session = _FakeSession(results=[assignment, agent, [queued]])

    assert await ae._drain_one_assignment(session, assignment.id) == 0
    assert queued.status == "skipped"


@pytest.mark.asyncio
async def test_draining_one_assignment_for_integration_events_selects_only_their_runs(monkeypatch):
    agent = _build_agent()
    assignment = _build_assignment(agent.id)
    _patch_engine(monkeypatch)
    _patch_slots(monkeypatch)
    session = _FakeSession(results=[assignment, agent, []])
    assert await ae._drain_one_assignment(session, assignment.id, only_integration_events=True) == 0
    selects = [s for s in session.statements if "pg_advisory_xact_lock" not in str(s)]
    assert ae.QUEUE_ORIGIN_KEY in str(selects[-1].compile(compile_kwargs={"literal_binds": True}))


def _state(*, agent_id=None, depth=1, active=0, cap=1, waited=60) -> assignment_dispatch.QueueState:
    return assignment_dispatch.QueueState(
        assignment_id=uuid.uuid4(),
        agent_id=agent_id or uuid.uuid4(),
        depth=depth,
        oldest_at=datetime.now(UTC) - timedelta(seconds=waited),
        active=active,
        cap=cap,
    )


def _patch_sweep(monkeypatch, states, *, waiting: int = 0):
    """The drain's reads: the queue states and the unclaimed executions."""
    loads: list[bool] = []

    async def _load(_db, *, only_integration_events):
        loads.append(only_integration_events)
        return list(states)

    async def _waiting(_db):
        return waiting

    monkeypatch.setattr(ae, "_load_queue_states", _load)
    monkeypatch.setattr(ae, "_unclaimed_assignment_executions", _waiting)
    monkeypatch.setattr(ae, "async_session", lambda: _FakeSession())
    return loads


@pytest.mark.asyncio
async def test_with_the_queue_off_the_sweep_promotes_only_what_integration_events_queued(monkeypatch):
    """ASSIGNMENT_QUEUE_ENABLED=false keeps the webhook backlog where it is, but a busy
    assignment never drops its events: their queued runs are still promoted."""
    monkeypatch.setattr(ae.settings, "assignment_queue_enabled", False)
    loads = _patch_sweep(monkeypatch, [_state()])
    calls: list[dict] = []

    async def _drain_one(_db, _assignment_id, **kwargs):
        calls.append(kwargs)
        return 1

    monkeypatch.setattr(ae, "_drain_one_assignment", _drain_one)
    assert await ae.drain_assignment_queues() == 1
    assert loads == [True]
    assert calls == [{"count": 1, "only_integration_events": True, "throttled": False}]


@pytest.mark.asyncio
async def test_with_the_queue_off_only_the_integration_event_queues_are_read():
    session = _FakeSession(results=[[]])
    assert await ae._load_queue_states(session, only_integration_events=True) == []
    assert ae.QUEUE_ORIGIN_KEY in str(session.statements[0].compile(compile_kwargs={"literal_binds": True}))

    session = _FakeSession(results=[[]])
    assert await ae._load_queue_states(session, only_integration_events=False) == []
    assert ae.QUEUE_ORIGIN_KEY not in str(session.statements[0].compile(compile_kwargs={"literal_binds": True}))


@pytest.mark.asyncio
async def test_with_the_queue_on_the_sweep_promotes_every_queued_run(monkeypatch):
    monkeypatch.setattr(ae.settings, "assignment_queue_enabled", True)
    loads = _patch_sweep(monkeypatch, [_state()])
    calls: list[dict] = []

    async def _drain_one(_db, _assignment_id, **kwargs):
        calls.append(kwargs)
        return 1

    monkeypatch.setattr(ae, "_drain_one_assignment", _drain_one)
    assert await ae.drain_assignment_queues() == 1
    assert loads == [False]
    assert calls == [{"count": 1, "only_integration_events": False, "throttled": False}]


@pytest.mark.asyncio
async def test_the_sweep_plans_every_free_slot_within_the_dispatch_capacity(monkeypatch):
    """The sweep hands each assignment its free slots, round-robin, and never
    more than the room left for executions the reconciler has not claimed."""
    monkeypatch.setattr(ae.settings, "assignment_queue_enabled", True)
    monkeypatch.setattr(ae.settings, "assignment_dispatch_max_pending_runs", 6)
    busy = _state(depth=10, cap=5, active=1, waited=300)
    other = _state(depth=10, cap=5, waited=30)
    held = _state(depth=10, cap=2, active=2, waited=900)
    _patch_sweep(monkeypatch, [busy, other, held], waiting=1)
    calls: dict = {}

    async def _drain_one(_db, assignment_id, **kwargs):
        calls[assignment_id] = kwargs["count"]
        return kwargs["count"]

    monkeypatch.setattr(ae, "_drain_one_assignment", _drain_one)

    assert await ae.drain_assignment_queues() == 5
    # 6 pending executions allowed, 1 already waiting: 5 slots, split fairly;
    # the assignment with no free slot takes no part.
    assert calls == {busy.assignment_id: 3, other.assignment_id: 2}


@pytest.mark.asyncio
async def test_a_throttled_assignment_is_drained_at_half_its_cap(monkeypatch):
    monkeypatch.setattr(ae.settings, "assignment_queue_enabled", True)
    monkeypatch.setattr(ae.settings, "assignment_dispatch_max_pending_runs", 50)
    state = _state(depth=10, cap=8, active=3)
    other = _state(depth=10, cap=8, active=3)
    _patch_sweep(monkeypatch, [state, other])

    async def _throttled(ids):
        assert set(ids) == {state.assignment_id, other.assignment_id}
        return {state.assignment_id}

    monkeypatch.setattr(assignment_dispatch, "read_throttles", _throttled)
    calls: dict = {}

    async def _drain_one(_db, assignment_id, **kwargs):
        calls[assignment_id] = kwargs
        return kwargs["count"]

    monkeypatch.setattr(ae, "_drain_one_assignment", _drain_one)

    assert await ae.drain_assignment_queues() == 6
    # Only the assignment whose run met the 429 backs off.
    assert calls[state.assignment_id] == {"count": 1, "only_integration_events": False, "throttled": True}
    assert calls[other.assignment_id] == {"count": 5, "only_integration_events": False, "throttled": False}


@pytest.mark.asyncio
async def test_a_drain_wakes_the_reconciler_once_whatever_it_promoted(monkeypatch):
    """Every promotion asks for a wake (``_schedule_run_execution``); the drain
    folds them into one, published after its last promotion."""
    monkeypatch.setattr(ae.settings, "assignment_queue_enabled", True)
    wakes: list[int] = []

    async def _wake():
        wakes.append(1)

    monkeypatch.setattr(ae, "notify_orchestration_wake", _wake)
    _patch_sweep(monkeypatch, [_state(), _state()])

    async def _drain_one(_db, _assignment_id, **_kwargs):
        for _ in range(3):
            await ae._wake_reconciler()
        assert wakes == [], "no wake before the drain is over"
        return 3

    monkeypatch.setattr(ae, "_drain_one_assignment", _drain_one)

    assert await ae.drain_assignment_queues() == 6
    assert wakes == [1]


@pytest.mark.asyncio
async def test_a_drain_that_promotes_nothing_wakes_nobody(monkeypatch):
    monkeypatch.setattr(ae.settings, "assignment_queue_enabled", True)
    wakes: list[int] = []

    async def _wake():
        wakes.append(1)

    monkeypatch.setattr(ae, "notify_orchestration_wake", _wake)
    _patch_sweep(monkeypatch, [_state()])

    async def _drain_one(_db, _assignment_id, **_kwargs):
        return 0

    monkeypatch.setattr(ae, "_drain_one_assignment", _drain_one)

    assert await ae.drain_assignment_queues() == 0
    assert wakes == []


@pytest.mark.asyncio
async def test_a_batched_wake_never_raises_nor_outlives_a_cancellation(monkeypatch):
    import asyncio

    wakes: list[int] = []

    async def _failing_wake():
        wakes.append(1)
        raise ConnectionError("redis is down")

    monkeypatch.setattr(ae, "notify_orchestration_wake", _failing_wake)

    async with ae.batched_wakes():
        await ae._wake_reconciler()
    assert wakes == [1], "a failed wake is swallowed: the reconciler tick is the fallback"

    wakes.clear()
    entered = asyncio.Event()

    async def _cancelled_mid_batch():
        async with ae.batched_wakes():
            await ae._wake_reconciler()
            entered.set()
            await asyncio.sleep(3600)

    task = asyncio.create_task(_cancelled_mid_batch())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert wakes == [], "a cancelled batch publishes nothing and stays cancelled"


@pytest.mark.asyncio
async def test_outside_a_batch_every_wake_is_published(monkeypatch):
    wakes: list[int] = []

    async def _wake():
        wakes.append(1)

    monkeypatch.setattr(ae, "notify_orchestration_wake", _wake)
    await ae._wake_reconciler()
    await ae._wake_reconciler()
    assert wakes == [1, 1]


def _state_rows(assignment_id, *, depth, unordered, free_keys, active, awaiting_human=0, not_deliveries=0):
    """The four reads of ``_load_queue_states``, in the order it makes them."""
    rows = [[(assignment_id, depth, datetime.now(UTC), unordered)]]
    if depth > unordered:
        rows.append([(assignment_id, free_keys)] if free_keys else [])
    rows.append([(assignment_id, active, awaiting_human, not_deliveries)] if active else [])
    rows.append([(assignment_id, uuid.uuid4(), {"max_concurrent_runs": 8}, "integration_event")])
    return rows


@pytest.mark.asyncio
async def test_a_queue_counts_as_startable_its_unordered_runs_and_its_free_keys():
    assignment_id = uuid.uuid4()
    session = _FakeSession(
        results=_state_rows(assignment_id, depth=12, unordered=2, free_keys=3, active=4)
    )

    (state,) = await ae._load_queue_states(session, only_integration_events=False)

    assert (state.depth, state.startable, state.active, state.exclusive) == (12, 5, 4, False)
    assert state.free == 4


@pytest.mark.asyncio
async def test_a_queue_blocked_on_its_keys_or_on_a_non_delivery_run_has_no_free_slot():
    blocked, exclusive = uuid.uuid4(), uuid.uuid4()
    (on_keys,) = await ae._load_queue_states(
        _FakeSession(results=_state_rows(blocked, depth=6, unordered=0, free_keys=0, active=2)),
        only_integration_events=False,
    )
    (on_run,) = await ae._load_queue_states(
        _FakeSession(results=_state_rows(exclusive, depth=6, unordered=6, free_keys=0, active=1, not_deliveries=1)),
        only_integration_events=False,
    )

    assert (on_keys.startable, on_keys.free) == (0, 0)
    assert (on_run.exclusive, on_run.free) == (True, 0)


@pytest.mark.asyncio
async def test_drain_sweep_survives_one_bad_assignment(monkeypatch):
    """One assignment's failure must never abort the sweep — the others still
    have deliveries waiting."""
    monkeypatch.setattr(ae.settings, "assignment_queue_enabled", True)
    bad, good = _state(waited=120), _state(waited=60)
    _patch_sweep(monkeypatch, [bad, good])

    async def _drain_one(_db, assignment_id, **_kwargs):
        if assignment_id == bad.assignment_id:
            raise RuntimeError("boom")
        return 1

    monkeypatch.setattr(ae, "_drain_one_assignment", _drain_one)

    assert await ae.drain_assignment_queues() == 1


@pytest.mark.asyncio
async def test_each_dispatch_cycle_logs_one_line_and_warns_once_it_lags(monkeypatch, caplog):
    monkeypatch.setattr(ae.settings, "assignment_queue_enabled", True)
    monkeypatch.setattr(ae.settings, "assignment_queue_backlog_seconds", 300)
    monkeypatch.setattr(ae, "_last_backlog_warning", 0.0)
    _patch_sweep(monkeypatch, [_state(depth=4, cap=1, active=1, waited=900)])

    with caplog.at_level("INFO", logger=ae.logger.name):
        assert await ae.drain_assignment_queues() == 0

    cycle = [r for r in caplog.records if r.getMessage().startswith("Assignment dispatch: promoted")]
    lag = [r for r in caplog.records if r.getMessage().startswith("Assignment dispatch lag")]
    assert len(cycle) == 1 and "4 queued across 1 assignments" in cycle[0].getMessage()
    assert len(lag) == 1 and lag[0].levelname == "WARNING"


# ── Endpoint contract ────────────────────────────────────────────────────────


class _WebhookRequest:
    def __init__(self, body: bytes = b'{"alert": "x"}'):
        self.headers: dict[str, str] = {}
        self.method = "POST"
        self.url = "http://xtm.local/api/hooks/assignments/x"
        self.client = SimpleNamespace(host="203.0.113.9")
        self._body = body

    async def body(self) -> bytes:
        return self._body


def _stub_direct_webhook_path(monkeypatch, assignment, agent):
    """Wire the direct-assignment handler up to in-memory rows.

    ``async_session`` is entered twice: once for the pre-auth assignment
    lookup, then once for the assignment + agent load inside the dedup-guarded
    body.
    """
    from app.api.v1 import webhooks

    sessions = iter(
        [
            _FakeSession(results=[assignment]),
            _FakeSession(results=[assignment, agent]),
        ]
    )
    monkeypatch.setattr(webhooks, "async_session", lambda: next(sessions))

    async def _clear(_assignment_id):
        return None

    async def _claim(_request, _body, scope):
        return False, None, f"dedupkey:{scope}"

    async def _finalize(_key, response):
        return response

    async def _release(_key):
        return None

    monkeypatch.setattr(webhooks, "_clear_direct_webhook_drift_state", _clear)
    monkeypatch.setattr(webhooks, "_claim_webhook_dedup_slot", _claim)
    monkeypatch.setattr(webhooks, "_finalize_dedup", _finalize)
    monkeypatch.setattr(webhooks, "_release_webhook_dedup_slot", _release)
    monkeypatch.setattr(webhooks.settings, "assignment_queue_enabled", True)
    return webhooks


@pytest.mark.asyncio
async def test_endpoint_reports_a_queued_delivery_with_its_run_id(monkeypatch):
    """The sender must be able to tell "queued" from "ran" AND still receive a
    run id — that id is what makes the delivery traceable end-to-end."""
    agent = _build_agent()
    assignment = _build_assignment(agent.id)
    webhooks = _stub_direct_webhook_path(monkeypatch, assignment, agent)
    queued_run_id = str(uuid.uuid4())

    async def _queue(**_kwargs):
        return ae.QueuedTriggerResult(
            status="queued", run_id=queued_run_id, queue_depth=3
        )

    monkeypatch.setattr(webhooks, "trigger_or_queue_assignment", _queue)

    response = await webhooks.receive_direct_assignment_webhook(
        str(assignment.id), _WebhookRequest()
    )

    assert response["status"] == "queued"
    assert response["run_id"] == queued_run_id
    assert response["queue_depth"] == 3
    # ``count`` counts runs STARTED by this delivery, so a queued delivery is
    # 0 — a sender tallying successful executions must not double-count it once
    # the queued run later fires.
    assert response["count"] == 0


@pytest.mark.asyncio
async def test_endpoint_answers_429_with_retry_after_when_the_queue_is_full(
    monkeypatch,
):
    """A 200 tells the sender "delivered"; only a 429 makes it retry. This is
    the whole reason ``reject`` is the default overflow policy."""
    from fastapi import HTTPException

    agent = _build_agent()
    assignment = _build_assignment(agent.id)
    webhooks = _stub_direct_webhook_path(monkeypatch, assignment, agent)
    released: list[str | None] = []

    async def _release(key):
        released.append(key)

    monkeypatch.setattr(webhooks, "_release_webhook_dedup_slot", _release)

    async def _queue(**_kwargs):
        return ae.QueuedTriggerResult(
            status="queue_full", queue_depth=20, retry_after_seconds=30
        )

    monkeypatch.setattr(webhooks, "trigger_or_queue_assignment", _queue)

    with pytest.raises(HTTPException) as exc:
        await webhooks.receive_direct_assignment_webhook(
            str(assignment.id), _WebhookRequest()
        )

    assert exc.value.status_code == 429
    assert exc.value.headers["Retry-After"] == "30"
    # The dedup slot must be RELEASED, not persisted: the sender's retry
    # carries the same idempotency key, and replaying the 429 body back as a
    # 200 would turn a rejected delivery into a phantom success.
    assert released == [f"dedupkey:assignments:{assignment.id}"]


@pytest.mark.asyncio
async def test_endpoint_marks_a_dropped_delivery_explicitly(monkeypatch):
    """``drop_newest`` answers 200 because the sender cannot fix it by
    retrying — but the body must say plainly that nothing will run."""
    agent = _build_agent()
    assignment = _build_assignment(agent.id)
    webhooks = _stub_direct_webhook_path(monkeypatch, assignment, agent)

    async def _queue(**_kwargs):
        return ae.QueuedTriggerResult(status="dropped", queue_depth=20)

    monkeypatch.setattr(webhooks, "trigger_or_queue_assignment", _queue)

    response = await webhooks.receive_direct_assignment_webhook(
        str(assignment.id), _WebhookRequest()
    )

    assert response["status"] == "dropped"
    assert "will not run" in response["reason"]
    assert response["queue_depth"] == 20


@pytest.mark.asyncio
async def test_endpoint_keeps_the_legacy_shape_when_a_run_starts(monkeypatch):
    """Existing integrations parse ``triggered[0].run_id`` — the immediate-run
    response must be byte-for-byte what it always was."""
    agent = _build_agent()
    assignment = _build_assignment(agent.id)
    webhooks = _stub_direct_webhook_path(monkeypatch, assignment, agent)
    run_id = str(uuid.uuid4())

    async def _queue(**_kwargs):
        return ae.QueuedTriggerResult(status="ok", run_id=run_id, queue_depth=0)

    monkeypatch.setattr(webhooks, "trigger_or_queue_assignment", _queue)

    response = await webhooks.receive_direct_assignment_webhook(
        str(assignment.id), _WebhookRequest()
    )

    assert response == {
        "status": "ok",
        "triggered": [
            {
                "agent": agent.name,
                "assignment": assignment.name,
                "run_id": run_id,
            }
        ],
        "count": 1,
    }


@pytest.mark.asyncio
async def test_endpoint_falls_back_to_the_legacy_drop_when_disabled(monkeypatch):
    """``ASSIGNMENT_QUEUE_ENABLED=false`` restores the old behaviour — but the
    drop is now a WARNING, not an INFO, because it IS a lost event."""
    agent = _build_agent()
    assignment = _build_assignment(agent.id)
    webhooks = _stub_direct_webhook_path(monkeypatch, assignment, agent)
    monkeypatch.setattr(webhooks.settings, "assignment_queue_enabled", False)

    async def _must_not_queue(**_kwargs):
        raise AssertionError("queue used while ASSIGNMENT_QUEUE_ENABLED=false")

    async def _skip(**_kwargs):
        return None

    monkeypatch.setattr(webhooks, "trigger_or_queue_assignment", _must_not_queue)
    monkeypatch.setattr(webhooks, "trigger_assignment", _skip)

    response = await webhooks.receive_direct_assignment_webhook(
        str(assignment.id), _WebhookRequest()
    )

    assert response["status"] == "skipped"
    assert response["reason"] == "assignment already has an active run"


@pytest.mark.asyncio
async def test_adapter_periodic_drains_the_queue(monkeypatch):
    """The drain hangs off ``periodic`` rather than a completion hook so it
    recovers the queue after EVERY way a run can stop owning its assignment —
    completion, failure, dead-letter, stale-sweep force-fail."""
    from app.services.orchestration_adapters.assignment import AssignmentAdapter

    calls: list[int] = []

    async def _drain(limit=None):
        calls.append(1)
        return 0

    monkeypatch.setattr(ae, "drain_assignment_queues", _drain)
    ctx = SimpleNamespace(db=_FakeSession(), worker_id="w1", cancel_event=None)

    await AssignmentAdapter().periodic(ctx)

    assert calls == [1]
