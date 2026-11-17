"""Unit tests for the durable orchestration engine.

The DB-dependent functions are exercised against a small in-memory fake session
that returns preset rows (mirroring the existing ``FakeSession`` style in the
suite). Postgres-specific behaviour (SKIP LOCKED, ON CONFLICT) is validated by
the real-DB integration path in CI; here we pin the FSM decision logic:
transition table, lease ownership, retry/deadline routing, sweeps, and outbox
backoff.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.models.orchestration import (
    OrchestrationExecution,
    OrchestrationOutboxEvent,
)
from app.services import orchestration_engine as engine

# ── Fake session ──────────────────────────────────────────────────────────────


class _FakeResult:
    def __init__(self, *, scalar=None, items=None):
        self._scalar = scalar
        self._items = items if items is not None else []

    def scalar_one_or_none(self):
        return self._scalar

    def scalar_one(self):
        if self._scalar is None:
            raise RuntimeError("no row")
        return self._scalar

    def scalar(self):
        return self._scalar

    def scalars(self):
        return SimpleNamespace(all=lambda: list(self._items))


class _FakeDb:
    def __init__(self, *, scalar=None, items=None):
        self._scalar = scalar
        self._items = items if items is not None else []
        self.flushes = 0
        self.added: list = []

    async def execute(self, *_args, **_kwargs):
        return _FakeResult(scalar=self._scalar, items=self._items)

    async def flush(self):
        self.flushes += 1

    def add(self, obj):
        self.added.append(obj)


def _exec(**kw) -> OrchestrationExecution:
    base = dict(
        id=uuid.uuid4(),
        adapter="assignment",
        state="running",
        adapter_state=None,
        payload={},
        run_at=datetime.now(UTC),
        deadline_at=None,
        lease_owner="w1",
        lease_expires_at=datetime.now(UTC) + timedelta(seconds=60),
        attempt_count=1,
        max_attempts=3,
        last_error=None,
        dedup_key=None,
    )
    base.update(kw)
    return OrchestrationExecution(**base)


def _outbox(**kw) -> OrchestrationOutboxEvent:
    base = dict(
        id=uuid.uuid4(),
        execution_id=uuid.uuid4(),
        event_type="resume",
        dedup_key="d1",
        payload={},
        status="pending",
        available_at=datetime.now(UTC),
        attempts=0,
        max_attempts=5,
        last_error=None,
    )
    base.update(kw)
    return OrchestrationOutboxEvent(**base)


# ── Schema regression ───────────────────────────────────────────────────


def test_adapter_state_column_is_unbounded_text():
    """``adapter_state`` must be unbounded TEXT, not a length-capped VARCHAR.

    Adapters store an opaque blob here; the autonomous adapter serializes a small
    JSON object (cycle + mode + no-progress streak). A bounded VARCHAR(50)
    silently overflowed once that blob grew, crashing the reconciler's persist
    with StringDataRightTruncationError. ``Text`` has ``length is None``.
    """
    assert OrchestrationExecution.__table__.c.adapter_state.type.length is None


def test_autonomous_adapter_state_blob_exceeds_legacy_50_char_cap():
    """The real serialized autonomous state is what overflowed the old cap.

    Pins WHY the column had to be widened: the exact blob the autonomous adapter
    writes (see ``orchestration_adapters/autonomous.py``) is longer than 50 chars.
    """
    import json

    blob = json.dumps({"cycle": 1, "mode": "continue", "no_progress_streak": 0})
    assert len(blob) > 50


# ── Pure logic ────────────────────────────────────────────────────────────────


def test_backoff_is_capped_and_jittered():
    # Large attempt → capped at MAX_BACKOFF_SECONDS (jitter never exceeds cap).
    assert engine._backoff_seconds(50) <= engine.MAX_BACKOFF_SECONDS
    # First attempt is roughly the base (within the ±25% jitter band).
    first = engine._backoff_seconds(1)
    assert engine.BASE_BACKOFF_SECONDS * 0.7 <= first <= engine.BASE_BACKOFF_SECONDS * 1.3


def test_transition_table_allows_and_rejects():
    engine._check_transition("pending", "running")  # ok
    engine._check_transition("running", "completed")  # ok
    engine._check_transition("running", "pending")  # retry, ok
    engine._check_transition("failed", "pending")  # operator requeue, ok
    engine._check_transition("dead_letter", "pending")  # operator requeue, ok
    with pytest.raises(engine.InvalidTransitionError):
        engine._check_transition("completed", "running")
    with pytest.raises(engine.InvalidTransitionError):
        engine._check_transition("pending", "completed")


def test_require_owner():
    ex = _exec(lease_owner="w1")
    engine._require_owner(ex, "w1", True)  # ok
    engine._require_owner(ex, None, False)  # lease-free path: ok
    with pytest.raises(engine.LeaseLostError):
        engine._require_owner(ex, "other", True)


# ── Terminal transitions ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_complete_clears_lease():
    ex = _exec(state="running", lease_owner="w1")
    db = _FakeDb(scalar=ex)
    out = await engine.complete(db, execution_id=ex.id, worker_id="w1")
    assert out.state == "completed"
    assert out.lease_owner is None
    assert db.flushes == 1


@pytest.mark.asyncio
async def test_complete_raises_when_lease_lost():
    ex = _exec(state="running", lease_owner="someone_else")
    db = _FakeDb(scalar=ex)
    with pytest.raises(engine.LeaseLostError):
        await engine.complete(db, execution_id=ex.id, worker_id="w1")


@pytest.mark.asyncio
async def test_fail_retryable_with_attempts_goes_pending_with_backoff():
    ex = _exec(state="running", attempt_count=1, max_attempts=3, deadline_at=None)
    db = _FakeDb(scalar=ex)
    before = datetime.now(UTC)
    out = await engine.fail(db, execution_id=ex.id, worker_id="w1", error="boom")
    assert out.state == "pending"
    assert out.run_at > before  # scheduled with backoff
    assert out.lease_owner is None


@pytest.mark.asyncio
async def test_fail_non_retryable_goes_failed():
    ex = _exec(state="running")
    db = _FakeDb(scalar=ex)
    out = await engine.fail(
        db, execution_id=ex.id, worker_id="w1", error="nope", retryable=False
    )
    assert out.state == "failed"


@pytest.mark.asyncio
async def test_fail_exhausted_attempts_dead_letters():
    ex = _exec(state="running", attempt_count=3, max_attempts=3)
    db = _FakeDb(scalar=ex)
    out = await engine.fail(db, execution_id=ex.id, worker_id="w1", error="boom")
    assert out.state == "dead_letter"


@pytest.mark.asyncio
async def test_fail_past_deadline_dead_letters_even_with_attempts():
    ex = _exec(
        state="running",
        attempt_count=1,
        max_attempts=3,
        deadline_at=datetime.now(UTC) - timedelta(minutes=1),
    )
    db = _FakeDb(scalar=ex)
    out = await engine.fail(db, execution_id=ex.id, worker_id="w1", error="late")
    assert out.state == "dead_letter"


@pytest.mark.asyncio
async def test_await_event_releases_lease_and_keeps_running():
    ex = _exec(state="running", lease_owner="w1")
    db = _FakeDb(scalar=ex)
    out = await engine.await_event(
        db, execution_id=ex.id, worker_id="w1", adapter_state="awaiting_human"
    )
    assert out.state == "running"
    assert out.adapter_state == "awaiting_human"
    assert out.lease_owner is None


@pytest.mark.asyncio
async def test_extend_lease_owner_mismatch_raises():
    ex = _exec(state="running", lease_owner="other")
    db = _FakeDb(scalar=ex)
    with pytest.raises(engine.LeaseLostError):
        await engine.extend_lease(db, execution_id=ex.id, worker_id="w1")


@pytest.mark.asyncio
async def test_extend_lease_refreshes_expiry():
    ex = _exec(state="running", lease_owner="w1")
    db = _FakeDb(scalar=ex)
    await engine.extend_lease(db, execution_id=ex.id, worker_id="w1", lease_seconds=120)
    assert ex.lease_expires_at > datetime.now(UTC) + timedelta(seconds=60)


@pytest.mark.asyncio
async def test_requeue_resets_failed_to_pending():
    ex = _exec(state="failed", attempt_count=3, last_error="boom", lease_owner=None)
    db = _FakeDb(scalar=ex)
    out = await engine.requeue_execution(db, execution_id=ex.id)
    assert out.state == "pending"
    assert out.attempt_count == 0
    assert out.last_error is None


@pytest.mark.asyncio
async def test_requeue_rejects_completed():
    ex = _exec(state="completed")
    db = _FakeDb(scalar=ex)
    with pytest.raises(engine.InvalidTransitionError):
        await engine.requeue_execution(db, execution_id=ex.id)


@pytest.mark.asyncio
async def test_rearm_for_resume_running_to_pending():
    # A genuinely paused (AWAIT) execution has its lease released.
    ex = _exec(
        state="running",
        adapter_state="awaiting_human",
        attempt_count=2,
        lease_owner=None,
        lease_expires_at=None,
    )
    db = _FakeDb(scalar=ex)
    out = await engine.rearm_for_resume(db, execution_id=ex.id)
    assert out.state == "pending"
    assert out.attempt_count == 0
    # adapter_state must be preserved so the adapter resumes from the state it
    # saved at await_event time (e.g. the autonomous cycle counter). Clearing it
    # wedged autonomous runs after their first cycle via a colliding resume
    # dedup key.
    assert out.adapter_state == "awaiting_human"


@pytest.mark.asyncio
async def test_rearm_for_resume_noop_when_not_running():
    ex = _exec(state="completed")
    db = _FakeDb(scalar=ex)
    out = await engine.rearm_for_resume(db, execution_id=ex.id)
    assert out.state == "completed"  # unchanged


@pytest.mark.asyncio
async def test_rearm_for_resume_raises_when_still_lease_held():
    # Resume raced ahead of the AWAIT pause: the execution is still running
    # under an active worker lease. Re-arming would steal the lease and cause a
    # duplicate run, so the engine signals the outbox to retry instead.
    ex = _exec(state="running", adapter_state="awaiting_human", lease_owner="w1")
    db = _FakeDb(scalar=ex)
    with pytest.raises(engine.ResumeNotReadyError):
        await engine.rearm_for_resume(db, execution_id=ex.id)
    assert ex.state == "running"  # unchanged — lease not stolen


@pytest.mark.asyncio
async def test_await_event_clears_existing_deadline():
    # An AWAIT with no deadline (e.g. HITL waiting on a human) must CLEAR a
    # deadline set at creation, otherwise sweep_deadlines would dead-letter the
    # paused execution after the original timeout.
    ex = _exec(
        state="running",
        lease_owner="w1",
        deadline_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    db = _FakeDb(scalar=ex)
    out = await engine.await_event(
        db, execution_id=ex.id, worker_id="w1", adapter_state="awaiting_human"
    )
    assert out.deadline_at is None


# ── Sweeps + claim ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reclaim_requeues_and_dead_letters():
    requeueable = _exec(state="running", attempt_count=1, max_attempts=3)
    exhausted = _exec(state="running", attempt_count=3, max_attempts=3)
    db = _FakeDb(items=[requeueable, exhausted])
    dead = await engine.reclaim_expired_leases(db, limit=10)
    assert requeueable.state == "pending"
    assert exhausted.state == "dead_letter"
    assert dead == [exhausted]


@pytest.mark.asyncio
async def test_sweep_deadlines_dead_letters_all():
    a = _exec(state="running", deadline_at=datetime.now(UTC) - timedelta(minutes=1))
    b = _exec(state="pending", deadline_at=datetime.now(UTC) - timedelta(minutes=1))
    db = _FakeDb(items=[a, b])
    swept = await engine.sweep_deadlines(db, limit=10)
    assert a.state == "dead_letter"
    assert b.state == "dead_letter"
    assert set(swept) == {a, b}


@pytest.mark.asyncio
async def test_claim_due_leases_and_increments_attempt():
    pending = _exec(state="pending", lease_owner=None, attempt_count=0)
    db = _FakeDb(items=[pending])
    claimed = await engine.claim_due(
        db, adapter="assignment", worker_id="w1", limit=5
    )
    assert claimed == [pending]
    assert pending.state == "running"
    assert pending.lease_owner == "w1"
    assert pending.attempt_count == 1
    assert pending.lease_expires_at is not None


# ── Create (non-dedup) ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_execution_without_dedup_adds_pending_row():
    db = _FakeDb()
    out = await engine.create_execution(
        db, adapter="assignment", payload={"run_id": "r1"}
    )
    assert out.state == "pending"
    assert out.adapter == "assignment"
    assert out.payload == {"run_id": "r1"}
    assert db.added == [out]
    assert db.flushes == 1


# ── Reset (re-engage) ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reset_execution_reengages_from_terminal_state():
    # A stopped autonomous run keeps its execution row (terminal) under the stable
    # dedup key; RESTART / RESUME must hard-reset that SAME row to a fresh pending
    # engagement rather than being blocked by ON CONFLICT DO NOTHING.
    ex = _exec(
        state="cancelled",
        adapter="autonomous_pentest",
        adapter_state='{"cycle": 42}',
        attempt_count=3,
        last_error="stopped",
        lease_owner="w9",
        lease_expires_at=datetime.now(UTC) + timedelta(seconds=30),
        deadline_at=None,
    )
    db = _FakeDb(scalar=ex)
    new_deadline = datetime.now(UTC) + timedelta(hours=6)
    out = await engine.reset_execution(
        db,
        execution_id=ex.id,
        payload={"openaev_run_id": "run-1", "objective": "new"},
        deadline_at=new_deadline,
    )
    assert out is ex
    assert out.state == "pending"
    assert out.payload == {"openaev_run_id": "run-1", "objective": "new"}
    assert out.adapter_state is None  # cycle counter reset to 0
    assert out.attempt_count == 0
    assert out.last_error is None
    assert out.lease_owner is None
    assert out.lease_expires_at is None
    assert out.deadline_at == new_deadline
    assert db.flushes == 1


@pytest.mark.asyncio
async def test_reset_execution_missing_returns_none():
    db = _FakeDb(scalar=None)
    out = await engine.reset_execution(
        db, execution_id=uuid.uuid4(), payload={}, deadline_at=None
    )
    assert out is None


# ── Outbox ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mark_outbox_processed():
    ev = _outbox(status="pending")
    db = _FakeDb(scalar=ev)
    await engine.mark_outbox_processed(db, event_id=ev.id)
    assert ev.status == "processed"
    assert ev.processed_at is not None


@pytest.mark.asyncio
async def test_mark_outbox_failed_backs_off_then_dead_letters():
    # Attempts remaining → backoff (stays pending, available_at pushed out).
    ev = _outbox(status="pending", attempts=1, max_attempts=5)
    before = datetime.now(UTC)
    db = _FakeDb(scalar=ev)
    await engine.mark_outbox_failed(db, event_id=ev.id, error="boom")
    assert ev.status == "pending"
    assert ev.available_at > before

    # Exhausted → dead_letter.
    ev2 = _outbox(status="pending", attempts=5, max_attempts=5)
    db2 = _FakeDb(scalar=ev2)
    await engine.mark_outbox_failed(db2, event_id=ev2.id, error="boom")
    assert ev2.status == "dead_letter"


@pytest.mark.asyncio
async def test_claim_outbox_dead_letters_when_over_max():
    fresh = _outbox(status="pending", attempts=0, max_attempts=5)
    over = _outbox(status="pending", attempts=5, max_attempts=5)
    db = _FakeDb(items=[fresh, over])
    claimed = await engine.claim_outbox_events(db, worker_id="w1", limit=10)
    # ``fresh`` claimed (attempts→1); ``over`` exceeded max → dead_letter, not returned.
    assert claimed == [fresh]
    assert fresh.attempts == 1
    assert over.status == "dead_letter"


# ── Cancel executions for deleted runs ────────────────────────────────────────


class _SeqDb:
    """Fake session that returns queued results in call order.

    ``cancel_executions_for_runs`` issues one SELECT (the matched execution ids)
    then a locked re-read per id via ``cancel_execution`` — a single fixed
    result (``_FakeDb``) can't model that two-phase access, so this fake pops a
    pre-seeded queue of results instead.
    """

    def __init__(self, results):
        self._results = list(results)
        self.flushes = 0

    async def execute(self, *_args, **_kwargs):
        return self._results.pop(0) if self._results else _FakeResult()

    async def flush(self):
        self.flushes += 1


@pytest.mark.asyncio
async def test_cancel_executions_for_runs_empty_is_noop():
    db = _SeqDb([])
    assert await engine.cancel_executions_for_runs(db, []) == 0
    # ``None`` ids are filtered out, so this is still a no-op (no DB hit).
    assert await engine.cancel_executions_for_runs(db, [None]) == 0


@pytest.mark.asyncio
async def test_cancel_executions_for_runs_cancels_pending_and_running():
    run_id = uuid.uuid4()
    pending = _exec(
        state="pending",
        lease_owner=None,
        lease_expires_at=None,
        payload={"run_id": str(run_id)},
    )
    running = _exec(
        state="running", lease_owner="w1", payload={"parent_run_id": str(run_id)}
    )
    db = _SeqDb(
        [
            _FakeResult(items=[pending.id, running.id]),  # SELECT matched ids
            _FakeResult(scalar=pending),  # locked re-read #1
            _FakeResult(scalar=running),  # locked re-read #2
        ]
    )
    cancelled = await engine.cancel_executions_for_runs(db, [run_id])
    assert cancelled == 2
    assert pending.state == "cancelled"
    assert running.state == "cancelled"
    # A running execution's lease is released so the worker bails on its next
    # terminal transition instead of clobbering the cancelled row.
    assert running.lease_owner is None


@pytest.mark.asyncio
async def test_cancel_executions_for_runs_skips_already_terminal():
    # A worker can finish a run between the SELECT and the locked re-read; the
    # terminal short-circuit in cancel_execution must leave it untouched and not
    # count it (race-safety).
    run_id = uuid.uuid4()
    done = _exec(state="completed")
    db = _SeqDb([_FakeResult(items=[done.id]), _FakeResult(scalar=done)])
    cancelled = await engine.cancel_executions_for_runs(db, [run_id])
    assert cancelled == 0
    assert done.state == "completed"  # unchanged


@pytest.mark.asyncio
async def test_cancel_executions_for_runs_batches_large_id_lists(monkeypatch):
    # Shrink the batch so 3 run ids span two matching queries; the union must
    # still cancel every matched execution exactly once. Guards the
    # bind-parameter-limit batching added for large purges / agent deletes.
    monkeypatch.setattr(engine, "_RUN_ID_QUERY_CHUNK", 2)
    run_ids = [uuid.uuid4() for _ in range(3)]
    e1 = _exec(state="pending", lease_owner=None, payload={"run_id": str(run_ids[0])})
    e2 = _exec(
        state="running", lease_owner="w1", payload={"parent_run_id": str(run_ids[2])}
    )
    db = _SeqDb(
        [
            _FakeResult(items=[e1.id]),  # batch 1 (run_ids[0:2]) SELECT
            _FakeResult(items=[e2.id]),  # batch 2 (run_ids[2:3]) SELECT
            _FakeResult(scalar=e1),  # locked re-read
            _FakeResult(scalar=e2),  # locked re-read
        ]
    )
    cancelled = await engine.cancel_executions_for_runs(db, run_ids)
    assert cancelled == 2
    assert e1.state == "cancelled"
    assert e2.state == "cancelled"


@pytest.mark.asyncio
async def test_cancel_executions_for_runs_dedups_across_batches(monkeypatch):
    # One execution can match on two different deleted run ids (its own
    # assignment_run_id and its orchestrator parent_run_id). With a tiny batch
    # those land in separate queries; the Python-side union must cancel it only
    # once (a second locked re-read is queued and must stay unconsumed).
    monkeypatch.setattr(engine, "_RUN_ID_QUERY_CHUNK", 1)
    run_ids = [uuid.uuid4(), uuid.uuid4()]
    shared = _exec(
        state="running",
        lease_owner="w1",
        payload={
            "assignment_run_id": str(run_ids[0]),
            "parent_run_id": str(run_ids[1]),
        },
    )
    db = _SeqDb(
        [
            _FakeResult(items=[shared.id]),  # batch 1 (run_ids[0]) -> shared
            _FakeResult(items=[shared.id]),  # batch 2 (run_ids[1]) -> shared again
            _FakeResult(scalar=shared),  # the single (deduped) locked re-read
            _FakeResult(scalar=shared),  # only consumed if dedup regresses
        ]
    )
    cancelled = await engine.cancel_executions_for_runs(db, run_ids)
    assert cancelled == 1
    assert shared.state == "cancelled"


# ── Mid-work ownership re-check reads POST-LOCK values ───────────────────────
# checkpoint_adapter_state runs REPEATEDLY inside a long autonomous burst, in the
# same session that loaded the execution before the work began. Sessions use
# expire_on_commit=False and the ORM does not overwrite attributes it has already
# loaded, so a plain SELECT ... FOR UPDATE locks the row but hands back the
# PRE-LOCK lease_owner / state. The ownership check would then pass even after
# another worker reclaimed the lease (or the operator cancelled the run) and the
# burst would keep authoring cycles against a run it no longer owns - defeating
# the guarantee the checkpoint offers its caller. The staleness itself only shows
# up with two concurrent sessions on a live database, so these pin the mechanism
# that prevents it.


class _StatementCaptureDb(_FakeDb):
    """``_FakeDb`` that also records the statements it executed."""

    def __init__(self, *, scalar=None, items=None):
        super().__init__(scalar=scalar, items=items)
        self.statements: list = []

    async def execute(self, stmt, *args, **kwargs):
        self.statements.append(stmt)
        return await super().execute(stmt, *args, **kwargs)


@pytest.mark.asyncio
async def test_locked_get_refresh_populates_existing():
    db = _StatementCaptureDb()
    await engine._locked_get(db, uuid.uuid4(), refresh=True)
    assert db.statements[0].get_execution_options().get("populate_existing") is True


@pytest.mark.asyncio
async def test_locked_get_defaults_to_no_populate_existing():
    # A caller that locks a row its session has not loaded (the heartbeat opens
    # a fresh session per beat) must not pay an attribute refresh it does not need.
    db = _StatementCaptureDb()
    await engine._locked_get(db, uuid.uuid4())
    assert db.statements[0].get_execution_options().get("populate_existing") is not True


# The transitions whose caller may already hold the row: the reconciler
# applies a result on the session that loaded the row before the adapter ran, an
# outbox handler re-arms on the session that read it to route the event, and
# cancel's callers may look the execution up first. The PostgreSQL replay is in
# test_orchestration_stale_lease_postgres.py; these pin the mechanism in the
# unit suite.
_TRANSITIONS_ON_A_HELD_ROW = {
    "complete": lambda db, ex: engine.complete(db, execution_id=ex.id, worker_id="w1"),
    "fail": lambda db, ex: engine.fail(db, execution_id=ex.id, worker_id="w1", error="boom"),
    "await_event": lambda db, ex: engine.await_event(
        db, execution_id=ex.id, worker_id="w1", adapter_state="awaiting"
    ),
    "rearm_for_resume": lambda db, ex: engine.rearm_for_resume(db, execution_id=ex.id),
    "cancel_execution": lambda db, ex: engine.cancel_execution(db, execution_id=ex.id),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("transition", sorted(_TRANSITIONS_ON_A_HELD_ROW))
async def test_transitions_on_a_held_row_lock_with_refresh(transition):
    paused = transition == "rearm_for_resume"
    execution = _exec(state="running", lease_owner=None if paused else "w1")
    db = _StatementCaptureDb(scalar=execution)
    await _TRANSITIONS_ON_A_HELD_ROW[transition](db, execution)
    assert db.statements[0].get_execution_options().get("populate_existing") is True


@pytest.mark.asyncio
async def test_checkpoint_adapter_state_locks_with_refresh():
    execution = _exec(state="running", lease_owner="w1")
    db = _StatementCaptureDb(scalar=execution)
    await engine.checkpoint_adapter_state(
        db, execution_id=execution.id, worker_id="w1", adapter_state='{"cycle": 7}'
    )
    assert execution.adapter_state == '{"cycle": 7}'
    assert db.flushes == 1
    # Re-asserting ownership is the point, and it is only meaningful against the
    # row's post-lock values.
    assert db.statements[0].get_execution_options().get("populate_existing") is True


@pytest.mark.asyncio
async def test_checkpoint_adapter_state_raises_when_lease_stolen():
    execution = _exec(state="running", lease_owner="w2")
    db = _StatementCaptureDb(scalar=execution)
    with pytest.raises(engine.LeaseLostError):
        await engine.checkpoint_adapter_state(
            db, execution_id=execution.id, worker_id="w1", adapter_state="{}"
        )


@pytest.mark.asyncio
async def test_checkpoint_adapter_state_raises_when_row_went_terminal():
    execution = _exec(state="cancelled", lease_owner="w1")
    db = _StatementCaptureDb(scalar=execution)
    with pytest.raises(engine.LeaseLostError):
        await engine.checkpoint_adapter_state(
            db, execution_id=execution.id, worker_id="w1", adapter_state="{}"
        )
