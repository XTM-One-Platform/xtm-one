"""Tests for the orchestration reconciler's pure helpers + result mapping."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.services import orchestration_reconciler as reconciler


class _FakeDb:
    def __init__(self):
        self.commits = 0
        self.rollbacks = 0

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


class _FakeSessionCtx:
    """Async-context-manager wrapper around a pre-built fake session."""

    def __init__(self, db):
        self._db = db

    async def __aenter__(self):
        return self._db

    async def __aexit__(self, *_exc):
        return False


def test_worker_id_format():
    wid = reconciler._make_worker_id()
    # host:pid:shorthex
    assert wid.count(":") == 2
    _host, pid, short = wid.split(":")
    assert pid.isdigit()
    assert len(short) == 8


def test_execution_timeout_uses_deadline():
    ex = SimpleNamespace(deadline_at=datetime.now(UTC) + timedelta(seconds=42))
    timeout = reconciler._execution_timeout(ex)
    assert 0 < timeout <= 42


def test_execution_timeout_past_deadline_is_minimal():
    ex = SimpleNamespace(deadline_at=datetime.now(UTC) - timedelta(seconds=5))
    assert reconciler._execution_timeout(ex) == 1.0


def test_execution_timeout_defaults_to_job_timeout():
    from app.core.config import settings

    ex = SimpleNamespace(deadline_at=None)
    assert reconciler._execution_timeout(ex) == float(settings.worker_job_timeout)


# ── Result mapping → engine transitions ──────────────────────────────────────


class _CaptureHandler(logging.Handler):
    """Capture records directly off a named logger.

    ``caplog`` relies on propagation to the root logger, but ``app.worker`` runs
    ``setup_logging()`` at import (reconfiguring handlers / propagation), so a
    handler attached straight to the target logger is the config-independent way
    to assert on emitted records.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.NOTSET)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    @property
    def text(self) -> str:
        return "\n".join(r.getMessage() for r in self.records)


def _attach_capture(logger_name: str) -> _CaptureHandler:
    handler = _CaptureHandler()
    logging.getLogger(logger_name).addHandler(handler)
    return handler


@pytest.mark.asyncio
async def test_run_cycle_bounds_claim_by_free_capacity(monkeypatch):
    # The reconciler must only claim what it can reasonably start soon: the
    # per-cycle claim limit is bounded by free execution slots
    # (capacity - in-flight), so a worker doesn't over-lease work queued behind
    # the semaphore while other replicas sit idle.
    from app.services import orchestration_engine as engine

    db = _FakeDb()
    monkeypatch.setattr(reconciler, "async_session", lambda: _FakeSessionCtx(db))
    monkeypatch.setattr(reconciler, "_semaphore_capacity", lambda: 3)

    async def _empty(*_a, **_k):
        return []

    monkeypatch.setattr(engine, "sweep_deadlines", _empty)
    monkeypatch.setattr(engine, "reclaim_expired_leases", _empty)

    recorded = {}

    async def _claim_due(_db, *, adapter, worker_id, limit, lease_seconds=None):
        recorded["limit"] = limit
        return []

    monkeypatch.setattr(engine, "claim_due", _claim_due)

    async def _noop_outbox(_worker_id):
        return None

    monkeypatch.setattr(reconciler, "_drive_outbox", _noop_outbox)

    class _StubAdapter:
        name = "assignment"

        async def periodic(self, _ctx):
            return None

    monkeypatch.setattr(reconciler, "registered_adapters", lambda: [_StubAdapter()])

    # Simulate 2 in-flight execution tasks → free capacity = 3 - 2 = 1.
    sentinels = {object(), object()}
    reconciler._execution_tasks.update(sentinels)
    try:
        await reconciler._run_cycle("w1")
    finally:
        reconciler._execution_tasks.difference_update(sentinels)

    assert recorded["limit"] == 1


@pytest.mark.asyncio
async def test_run_cycle_skips_claim_when_at_capacity(monkeypatch):
    # When in-flight tasks already fill capacity, the reconciler must not claim
    # (or lease) any more this cycle — leaving due work for idle replicas.
    from app.services import orchestration_engine as engine

    db = _FakeDb()
    monkeypatch.setattr(reconciler, "async_session", lambda: _FakeSessionCtx(db))
    monkeypatch.setattr(reconciler, "_semaphore_capacity", lambda: 2)

    async def _empty(*_a, **_k):
        return []

    monkeypatch.setattr(engine, "sweep_deadlines", _empty)
    monkeypatch.setattr(engine, "reclaim_expired_leases", _empty)

    called = {"claim": False}

    async def _claim_due(_db, *, adapter, worker_id, limit, lease_seconds=None):  # pragma: no cover
        called["claim"] = True
        return []

    monkeypatch.setattr(engine, "claim_due", _claim_due)

    async def _noop_outbox(_worker_id):
        return None

    monkeypatch.setattr(reconciler, "_drive_outbox", _noop_outbox)

    class _StubAdapter:
        name = "assignment"

        async def periodic(self, _ctx):
            return None

    monkeypatch.setattr(reconciler, "registered_adapters", lambda: [_StubAdapter()])

    sentinels = {object(), object()}  # 2 in-flight, capacity 2 → 0 free slots
    reconciler._execution_tasks.update(sentinels)
    try:
        await reconciler._run_cycle("w1")
    finally:
        reconciler._execution_tasks.difference_update(sentinels)

    assert called["claim"] is False


# ── Per-adapter dedicated concurrency budget ─────────────────────────────────


def test_adapter_concurrency_limit_only_positive_ints():
    # A dedicated budget is a POSITIVE int; None / 0 / negative all mean "share
    # the reconciler pool" (the shared-semaphore fallback).
    assert reconciler._adapter_concurrency_limit(SimpleNamespace(name="a")) is None
    assert (
        reconciler._adapter_concurrency_limit(
            SimpleNamespace(name="a", concurrency_limit=None)
        )
        is None
    )
    assert (
        reconciler._adapter_concurrency_limit(
            SimpleNamespace(name="a", concurrency_limit=0)
        )
        is None
    )
    assert (
        reconciler._adapter_concurrency_limit(
            SimpleNamespace(name="a", concurrency_limit=-3)
        )
        is None
    )
    assert (
        reconciler._adapter_concurrency_limit(
            SimpleNamespace(name="a", concurrency_limit=12)
        )
        == 12
    )


def test_available_slots_dedicated_adapter_uses_own_budget(monkeypatch):
    # A dedicated adapter is bounded by its OWN budget (limit - its in-flight),
    # independent of the shared pool AND of unrelated in-flight tasks.
    auton = SimpleNamespace(name="autonomous_pentest", concurrency_limit=12)
    assign = SimpleNamespace(name="assignment")  # shared (no limit)
    monkeypatch.setattr(reconciler, "_semaphore_capacity", lambda: 15)

    sentinels = {object() for _ in range(50)}  # noise: lots of in-flight tasks
    reconciler._execution_tasks.update(sentinels)
    reconciler._inflight_by_adapter.clear()
    reconciler._inflight_by_adapter["autonomous_pentest"] = 4
    try:
        assert reconciler._available_slots_for(auton, [auton, assign]) == 12 - 4
    finally:
        reconciler._execution_tasks.difference_update(sentinels)
        reconciler._inflight_by_adapter.clear()


def test_available_slots_shared_pool_excludes_dedicated_inflight(monkeypatch):
    # Shared-pool adapters must NOT be shrunk by a dedicated adapter's in-flight
    # tasks: shared in-flight = total in-flight - dedicated in-flight.
    auton = SimpleNamespace(name="autonomous_pentest", concurrency_limit=12)
    assign = SimpleNamespace(name="assignment")
    monkeypatch.setattr(reconciler, "_semaphore_capacity", lambda: 15)

    # 6 in-flight tasks total, 4 of them autonomous (dedicated) → shared = 2.
    sentinels = {object() for _ in range(6)}
    reconciler._execution_tasks.update(sentinels)
    reconciler._inflight_by_adapter.clear()
    reconciler._inflight_by_adapter["autonomous_pentest"] = 4
    try:
        # shared availability = capacity(15) - shared_inflight(6 - 4 = 2) = 13.
        assert reconciler._available_slots_for(assign, [auton, assign]) == 13
    finally:
        reconciler._execution_tasks.difference_update(sentinels)
        reconciler._inflight_by_adapter.clear()


def test_get_adapter_semaphore_sized_to_limit_and_cached():
    reconciler._adapter_semaphores.pop("autonomous_pentest", None)
    try:
        sem = reconciler._get_adapter_semaphore("autonomous_pentest", 9)
        assert sem._value == 9
        # Cached: the same name returns the SAME object (never resized).
        assert reconciler._get_adapter_semaphore("autonomous_pentest", 3) is sem
    finally:
        reconciler._adapter_semaphores.pop("autonomous_pentest", None)


@pytest.mark.asyncio
async def test_spawn_execution_task_logs_unexpected_crash(monkeypatch):
    # If _run_execution raises outside its own handlers, the done-callback must
    # observe the exception and log it with execution/adapter context — instead
    # of leaving asyncio to emit an unstructured "Task exception was never
    # retrieved" warning.
    async def _boom(*_args, **_kwargs):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(reconciler, "_run_execution", _boom)
    handler = _attach_capture("app.services.orchestration_reconciler")
    try:
        reconciler._spawn_execution_task(uuid.uuid4(), "assignment", "w1")
        # The task is still pending in the registry — await it (swallowing the
        # raise), then yield once so the done-callback runs.
        await asyncio.gather(
            *list(reconciler._execution_tasks), return_exceptions=True
        )
        await asyncio.sleep(0)
    finally:
        logging.getLogger("app.services.orchestration_reconciler").removeHandler(
            handler
        )

    assert "crashed unexpectedly" in handler.text
    assert "assignment" in handler.text


@pytest.mark.asyncio
async def test_log_reconciler_exit_logs_unexpected_exception():
    from app import worker

    async def _boom():
        raise RuntimeError("loop died")

    task = asyncio.create_task(_boom())
    await asyncio.gather(task, return_exceptions=True)

    handler = _attach_capture("app.worker")
    try:
        worker._log_reconciler_exit(task)
    finally:
        logging.getLogger("app.worker").removeHandler(handler)

    assert "exited unexpectedly" in handler.text


@pytest.mark.asyncio
async def test_log_reconciler_exit_silent_on_cancellation():
    from app import worker

    async def _runs_forever():
        await asyncio.sleep(3600)

    task = asyncio.create_task(_runs_forever())
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    handler = _attach_capture("app.worker")
    try:
        worker._log_reconciler_exit(task)
    finally:
        logging.getLogger("app.worker").removeHandler(handler)

    # A clean shutdown cancellation is expected, not an error.
    assert "exited unexpectedly" not in handler.text


@pytest.mark.asyncio
async def test_apply_result_completed(monkeypatch):
    from app.services import orchestration_engine as engine

    calls = {}

    async def _complete(db, *, execution_id, worker_id=None, require_lease=True, payload_update=None):
        calls["complete"] = (execution_id, worker_id, payload_update)

    monkeypatch.setattr(engine, "complete", _complete)
    ex_id = uuid.uuid4()
    from app.services.orchestration_adapters.base import AdapterResult

    db = _FakeDb()
    await reconciler._apply_result(
        db=db, execution_id=ex_id, worker_id="w1", result=AdapterResult.completed()
    )
    assert calls["complete"][0] == ex_id
    assert db.commits == 1


@pytest.mark.asyncio
async def test_apply_result_await(monkeypatch):
    from app.services import orchestration_engine as engine

    calls = {}

    async def _await_event(db, *, execution_id, worker_id=None, require_lease=True, adapter_state, deadline_at=None):
        calls["await"] = (execution_id, adapter_state)

    monkeypatch.setattr(engine, "await_event", _await_event)
    ex_id = uuid.uuid4()
    from app.services.orchestration_adapters.base import AdapterResult

    await reconciler._apply_result(
        db=_FakeDb(),
        execution_id=ex_id,
        worker_id="w1",
        result=AdapterResult.await_event(adapter_state="awaiting_human"),
    )
    assert calls["await"] == (ex_id, "awaiting_human")


def _stop_harness(monkeypatch, raiser):
    """Wire _do_execute to a stub adapter that raises, and record terminal writes.

    Deliberately NOT the evaluation adapter: that one classifies its own
    cancellation, which is exactly what these tests must work without. Every
    other adapter relies on the central handling in ``_do_execute``.
    """
    from app.services import orchestration_engine as engine
    from app.services.orchestration_adapters.base import (
        AdapterContext,
        AdapterResult,
        OrchestrationAdapter,
    )

    db = _FakeDb()
    monkeypatch.setattr(reconciler, "async_session", lambda: _FakeSessionCtx(db))
    execution = SimpleNamespace(
        state="running", lease_owner="w1", deadline_at=None, payload={}
    )

    async def _get_execution(_db, _id):
        return execution

    monkeypatch.setattr(engine, "get_execution", _get_execution)

    terminal_writes: list[str] = []

    async def _safe_terminal(_db, _eid, _wid, *, error=None, fail=False):
        terminal_writes.append(error or ("fail" if fail else "complete"))

    monkeypatch.setattr(reconciler, "_safe_terminal", _safe_terminal)

    class _StubAdapter(OrchestrationAdapter):
        name = "stub"

        async def execute(
            self, _ctx: AdapterContext, _execution
        ) -> AdapterResult:
            raise raiser()

    monkeypatch.setattr(reconciler, "get_adapter", lambda _name: _StubAdapter())
    return terminal_writes


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("event_set", "stop_on_lease_loss", "expected_writes"),
    [
        pytest.param(True, True, [], id="stop-racing-the-deadline-writes-nothing"),
        pytest.param(False, True, ["Execution timed out"], id="deadline-without-a-stop"),
        pytest.param(
            True, False, ["Execution timed out"], id="stop-racing-the-deadline-not-opted-in"
        ),
    ],
)
async def test_a_deadline_is_written_as_a_timeout_unless_a_lease_loss_stop_raced_it(
    monkeypatch, event_set, stop_on_lease_loss, expected_writes
):
    """``asyncio.timeout`` turns a stop racing the deadline into ``TimeoutError``.

    For an adapter that opted in, a set event means the row is no longer ours,
    so nothing is written rather than "Execution timed out" naming the wrong
    cause. A genuine deadline is still recorded, or an overrunning execution
    would be stranded; for any other adapter the event can only be an operator
    cancel, so a deadline stays a timeout.
    """
    cancel_event = asyncio.Event()
    if event_set:
        cancel_event.set()
    terminal_writes = _stop_harness(monkeypatch, TimeoutError)

    await reconciler._do_execute(
        uuid.uuid4(), "stub", "w1", cancel_event, stop_on_lease_loss=stop_on_lease_loss
    )

    assert terminal_writes == expected_writes


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("event_set", "stop_on_lease_loss", "absorbed"),
    [
        pytest.param(True, True, True, id="lease-loss-stop-is-absorbed-and-logged"),
        pytest.param(False, True, False, id="shutdown-propagates"),
        pytest.param(True, False, False, id="operator-cancel-not-opted-in-propagates"),
    ],
)
async def test_a_cancellation_is_absorbed_only_as_an_opted_in_lease_loss(
    monkeypatch, caplog, event_set, stop_on_lease_loss, absorbed
):
    """A raw ``CancelledError`` from an adapter that does not classify its own.

    Absorbed only when the adapter opted in and the event is set, and then named
    in the logs: a cancelled task logs nothing in ``_on_done``, so a lease loss
    and a worker shutdown would otherwise look identical. A shutdown (event
    unset), or any cancel of an adapter that did not opt in, still
    unwinds the task. No arm writes a terminal state.
    """
    cancel_event = asyncio.Event()
    if event_set:
        cancel_event.set()
    terminal_writes = _stop_harness(monkeypatch, asyncio.CancelledError)

    run = reconciler._do_execute(
        uuid.uuid4(), "stub", "w1", cancel_event, stop_on_lease_loss=stop_on_lease_loss
    )
    with caplog.at_level(logging.INFO):
        if absorbed:
            await run
        else:
            with pytest.raises(asyncio.CancelledError):
                await run

    assert terminal_writes == []
    if absorbed:
        assert any("no longer ours" in r.getMessage() for r in caplog.records), (
            "the stop was swallowed without a word to the operator"
        )


@pytest.mark.asyncio
async def test_do_execute_releases_connection_before_adapter_work(monkeypatch):
    # The read-only lease re-read opens a transaction that would otherwise pin a
    # pooled connection for the full (potentially minutes-long) adapter.execute().
    # _do_execute must commit that transaction BEFORE running the adapter, so the
    # connection is returned to the pool during the long work.
    from app.services import orchestration_engine as engine
    from app.services.orchestration_adapters.base import (
        AdapterContext,
        AdapterResult,
        OrchestrationAdapter,
    )

    db = _FakeDb()
    monkeypatch.setattr(reconciler, "async_session", lambda: _FakeSessionCtx(db))

    execution = SimpleNamespace(
        state="running", lease_owner="w1", deadline_at=None, payload={}
    )

    async def _get_execution(_db, _id):
        return execution

    monkeypatch.setattr(engine, "get_execution", _get_execution)

    seen = {}

    class _StubAdapter(OrchestrationAdapter):
        name = "stub"

        async def execute(self, ctx: AdapterContext, _execution) -> AdapterResult:
            # Capture how many commits happened before the long work started.
            seen["commits_before_execute"] = ctx.db.commits
            return AdapterResult.completed()

    monkeypatch.setattr(reconciler, "get_adapter", lambda _name: _StubAdapter())

    async def _complete(_db, **_k):
        return None

    monkeypatch.setattr(engine, "complete", _complete)

    await reconciler._do_execute(uuid.uuid4(), "stub", "w1", asyncio.Event(), stop_on_lease_loss=False)

    # The lease-read transaction was committed before the adapter ran (1 commit),
    # and _apply_result committed the terminal transition afterwards (2nd commit).
    assert seen["commits_before_execute"] == 1
    assert db.commits == 2


@pytest.mark.asyncio
async def test_do_execute_bails_when_lease_not_ours(monkeypatch):
    # If the lease was reclaimed before we started, _do_execute must not commit
    # or run the adapter at all.
    from app.services import orchestration_engine as engine

    db = _FakeDb()
    monkeypatch.setattr(reconciler, "async_session", lambda: _FakeSessionCtx(db))

    execution = SimpleNamespace(
        state="running", lease_owner="someone-else", deadline_at=None, payload={}
    )

    async def _get_execution(_db, _id):
        return execution

    monkeypatch.setattr(engine, "get_execution", _get_execution)

    def _boom(_name):  # pragma: no cover - must never be reached
        raise AssertionError("adapter must not be resolved when lease is lost")

    monkeypatch.setattr(reconciler, "get_adapter", _boom)

    await reconciler._do_execute(uuid.uuid4(), "stub", "w1", asyncio.Event(), stop_on_lease_loss=False)
    assert db.commits == 0


@pytest.mark.asyncio
async def test_apply_result_failed(monkeypatch):
    from app.services import orchestration_engine as engine

    calls = {}

    async def _fail(db, *, execution_id, worker_id=None, require_lease=True, error, retryable=True):
        calls["fail"] = (execution_id, error, retryable)
        # A clean adapter-reported non-retryable failure resolves to ``failed``.
        return SimpleNamespace(id=execution_id, state="failed")

    monkeypatch.setattr(engine, "fail", _fail)
    ex_id = uuid.uuid4()
    from app.services.orchestration_adapters.base import AdapterResult

    await reconciler._apply_result(
        db=_FakeDb(),
        execution_id=ex_id,
        worker_id="w1",
        result=AdapterResult.failed("boom", retryable=False),
    )
    assert calls["fail"] == (ex_id, "boom", False)


@pytest.mark.asyncio
async def test_apply_result_dead_letter_runs_on_dead_letter(monkeypatch):
    # engine.fail() can land an execution in ``dead_letter`` (attempts exhausted
    # / past deadline) WITHOUT the deadline/lease sweeps that normally drive
    # on_dead_letter. _apply_result must run the adapter cleanup itself so the
    # backing row (e.g. a stuck SubAgentTask) is failed instead of stranded.
    from app.services import orchestration_engine as engine

    ex = SimpleNamespace(id=uuid.uuid4(), state="dead_letter")
    dl_calls = []

    async def _fail(_db, **_kw):
        return ex

    async def _run_dl(execution, worker_id):
        dl_calls.append((execution, worker_id))

    monkeypatch.setattr(engine, "fail", _fail)
    monkeypatch.setattr(reconciler, "run_dead_letter", _run_dl)
    from app.services.orchestration_adapters.base import AdapterResult

    db = _FakeDb()
    await reconciler._apply_result(
        db=db,
        execution_id=ex.id,
        worker_id="w1",
        result=AdapterResult.failed("boom", retryable=True),
    )
    assert db.commits == 1
    assert dl_calls == [(ex, "w1")]


@pytest.mark.asyncio
async def test_apply_result_failed_terminal_skips_on_dead_letter(monkeypatch):
    # A clean ``failed`` (adapter reported it; the body already cleaned up) must
    # NOT trigger on_dead_letter — only ``dead_letter`` does.
    from app.services import orchestration_engine as engine

    ex = SimpleNamespace(id=uuid.uuid4(), state="failed")
    dl_calls = []

    async def _fail(_db, **_kw):
        return ex

    async def _run_dl(execution, worker_id):
        dl_calls.append((execution, worker_id))

    monkeypatch.setattr(engine, "fail", _fail)
    monkeypatch.setattr(reconciler, "run_dead_letter", _run_dl)
    from app.services.orchestration_adapters.base import AdapterResult

    db = _FakeDb()
    await reconciler._apply_result(
        db=db,
        execution_id=ex.id,
        worker_id="w1",
        result=AdapterResult.failed("boom", retryable=False),
    )
    assert db.commits == 1
    assert dl_calls == []


@pytest.mark.asyncio
async def test_safe_terminal_dead_letter_runs_on_dead_letter(monkeypatch):
    # A reconciler-driven timeout / adapter crash that exhausts attempts lands
    # the execution in ``dead_letter`` via engine.fail(); _safe_terminal must
    # drive the adapter cleanup so the backing row is failed, not stranded.
    from app.services import orchestration_engine as engine

    ex = SimpleNamespace(id=uuid.uuid4(), state="dead_letter")
    dl_calls = []

    async def _fail(_db, **_kw):
        return ex

    async def _run_dl(execution, worker_id):
        dl_calls.append((execution, worker_id))

    monkeypatch.setattr(engine, "fail", _fail)
    monkeypatch.setattr(reconciler, "run_dead_letter", _run_dl)

    db = _FakeDb()
    await reconciler._safe_terminal(
        db, ex.id, "w1", error="Execution timed out", fail=True
    )
    assert db.commits == 1
    assert dl_calls == [(ex, "w1")]


# ── run_dead_letter resilience (shared by reconciler + cleanup-stale API) ─────


@pytest.mark.asyncio
async def test_run_dead_letter_swallows_adapter_error(monkeypatch):
    # A raising on_dead_letter (e.g. corrupt payload) must be logged and
    # swallowed — never propagated — so a single bad execution can't 500 the
    # /runs/cleanup-stale request or abort cleanup of the rest of the batch.
    # (A deterministic ValueError is non-transient, so no retry/backoff runs.)
    db = _FakeDb()
    monkeypatch.setattr(reconciler, "async_session", lambda: _FakeSessionCtx(db))

    class _BoomAdapter:
        async def on_dead_letter(self, _ctx, _execution):
            raise ValueError("corrupt payload")

    monkeypatch.setattr(reconciler, "get_adapter", lambda _name: _BoomAdapter())

    execution = SimpleNamespace(
        id=uuid.uuid4(), adapter="assignment", payload={}, last_error=None
    )
    handler = _attach_capture("app.services.orchestration_reconciler")
    try:
        # Must not raise.
        await reconciler.run_dead_letter(execution, "w1")
    finally:
        logging.getLogger("app.services.orchestration_reconciler").removeHandler(
            handler
        )

    assert "on_dead_letter failed" in handler.text
    # The failing path never reaches the commit.
    assert db.commits == 0


@pytest.mark.asyncio
async def test_run_dead_letter_unknown_adapter_is_noop(monkeypatch):
    # An execution referencing an unregistered adapter must be logged and
    # skipped, not raised.
    from app.services.orchestration_adapters import AdapterNotFoundError

    def _missing(_name):
        raise AdapterNotFoundError(_name)

    monkeypatch.setattr(reconciler, "get_adapter", _missing)

    execution = SimpleNamespace(
        id=uuid.uuid4(), adapter="ghost", payload={}, last_error=None
    )
    handler = _attach_capture("app.services.orchestration_reconciler")
    try:
        await reconciler.run_dead_letter(execution, "w1")
    finally:
        logging.getLogger("app.services.orchestration_reconciler").removeHandler(
            handler
        )

    assert "unknown adapter" in handler.text


@pytest.mark.asyncio
async def test_run_dead_letter_retries_on_transient_error(monkeypatch):
    """on_dead_letter is retried up to _DEAD_LETTER_MAX_RETRIES times before
    giving up.  A transient error on the first attempt must NOT prevent
    the cleanup from succeeding on the next attempt."""
    db = _FakeDb()
    # Every async_session() call returns a fresh context wrapping the same db.
    monkeypatch.setattr(reconciler, "async_session", lambda: _FakeSessionCtx(db))

    async def _no_sleep(_s):
        pass

    monkeypatch.setattr(reconciler.asyncio, "sleep", _no_sleep)

    call_count = 0

    class _FlakyAdapter:
        async def on_dead_letter(self, _ctx, _execution):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ConnectionResetError("transient DB hiccup")
            # Second attempt succeeds.

    monkeypatch.setattr(reconciler, "get_adapter", lambda _name: _FlakyAdapter())

    execution = SimpleNamespace(
        id=uuid.uuid4(), adapter="assignment", payload={}, last_error=None
    )
    handler = _attach_capture("app.services.orchestration_reconciler")
    try:
        await reconciler.run_dead_letter(execution, "w1")
    finally:
        logging.getLogger("app.services.orchestration_reconciler").removeHandler(handler)

    # Two calls: fail then succeed.
    assert call_count == 2
    # DB commit reached on the successful attempt.
    assert db.commits == 1
    # A retry-warning must have been emitted for the first failure.
    assert "retrying" in handler.text
    # The final "failed for execution" error must NOT be emitted.
    assert "on_dead_letter failed" not in handler.text


@pytest.mark.asyncio
async def test_run_dead_letter_exhausts_retries_and_swallows(monkeypatch):
    """When all retries of a transient failure fail, the error is logged but
    NOT propagated."""
    db = _FakeDb()
    monkeypatch.setattr(reconciler, "async_session", lambda: _FakeSessionCtx(db))

    async def _no_sleep(_s):
        pass

    monkeypatch.setattr(reconciler.asyncio, "sleep", _no_sleep)

    call_count = 0

    class _AlwaysBoom:
        async def on_dead_letter(self, _ctx, _execution):
            nonlocal call_count
            call_count += 1
            # Transient class (connection loss) so every attempt is retried.
            raise ConnectionResetError("persistent connection failure")

    monkeypatch.setattr(reconciler, "get_adapter", lambda _name: _AlwaysBoom())

    execution = SimpleNamespace(
        id=uuid.uuid4(), adapter="assignment", payload={}, last_error=None
    )
    handler = _attach_capture("app.services.orchestration_reconciler")
    try:
        # Must NOT raise even when all retries are exhausted.
        await reconciler.run_dead_letter(execution, "w1")
    finally:
        logging.getLogger("app.services.orchestration_reconciler").removeHandler(handler)

    expected_calls = reconciler._DEAD_LETTER_MAX_RETRIES + 1
    assert call_count == expected_calls
    # No commit on total failure.
    assert db.commits == 0
    assert "on_dead_letter failed" in handler.text


@pytest.mark.asyncio
async def test_run_dead_letter_does_not_retry_non_transient_error(monkeypatch):
    """A deterministic adapter failure (corrupt payload, adapter bug) is not
    worth retrying: it fails on the FIRST attempt without burning the backoff
    budget, keeping the reconciler cycle short."""
    db = _FakeDb()
    monkeypatch.setattr(reconciler, "async_session", lambda: _FakeSessionCtx(db))

    sleeps: list[float] = []

    async def _record_sleep(s):
        sleeps.append(s)

    monkeypatch.setattr(reconciler.asyncio, "sleep", _record_sleep)

    call_count = 0

    class _DeterministicBoom:
        async def on_dead_letter(self, _ctx, _execution):
            nonlocal call_count
            call_count += 1
            raise RuntimeError("corrupt payload")

    monkeypatch.setattr(
        reconciler, "get_adapter", lambda _name: _DeterministicBoom()
    )

    execution = SimpleNamespace(
        id=uuid.uuid4(), adapter="assignment", payload={}, last_error=None
    )
    handler = _attach_capture("app.services.orchestration_reconciler")
    try:
        await reconciler.run_dead_letter(execution, "w1")
    finally:
        logging.getLogger("app.services.orchestration_reconciler").removeHandler(handler)

    # Single attempt, no backoff sleeps, no retry warning.
    assert call_count == 1
    assert sleeps == []
    assert "retrying" not in handler.text
    assert "on_dead_letter failed" in handler.text
    assert db.commits == 0


def test_transient_dead_letter_error_classification():
    """The predicate matches connection/pool/timeout classes and rejects
    deterministic failures."""
    from sqlalchemy.exc import DBAPIError, OperationalError

    assert reconciler._is_transient_dead_letter_error(
        ConnectionResetError("reset")
    )
    assert reconciler._is_transient_dead_letter_error(TimeoutError("timeout"))
    assert reconciler._is_transient_dead_letter_error(
        OperationalError("stmt", None, Exception("db down"))
    )
    invalidated = DBAPIError("stmt", None, Exception("gone"))
    invalidated.connection_invalidated = True
    assert reconciler._is_transient_dead_letter_error(invalidated)
    assert not reconciler._is_transient_dead_letter_error(
        DBAPIError("stmt", None, Exception("constraint"))
    )
    assert not reconciler._is_transient_dead_letter_error(
        RuntimeError("adapter bug")
    )
    assert not reconciler._is_transient_dead_letter_error(
        ValueError("corrupt payload")
    )
    # Deterministic OSError subclasses (filesystem/permissions) are NOT
    # transient — only the network/timeout classes qualify.
    assert not reconciler._is_transient_dead_letter_error(
        FileNotFoundError("missing file")
    )
    assert not reconciler._is_transient_dead_letter_error(
        PermissionError("denied")
    )


@pytest.mark.parametrize(
    ("code", "transient"),
    [
        ({"sqlstate": "40P01"}, True),
        ({"sqlstate": "40001"}, True),
        ({"pgcode": "40P01"}, True),
        ({"sqlstate": "23505"}, False),
        ({"sqlstate": None}, False),
    ],
    ids=["deadlock", "serialization-failure", "deadlock-pgcode", "unique-violation", "no-code"],
)
def test_a_deadlock_or_serialization_failure_is_a_transient_dead_letter_error(code, transient):
    """A deadlock or serialization failure arrives as a plain DBAPIError, so only the
    driver's code identifies it: asyncpg's ``sqlstate``, psycopg's ``pgcode``."""
    from sqlalchemy.exc import DBAPIError

    orig = Exception("driver error")
    for name, value in code.items():
        setattr(orig, name, value)

    assert reconciler._is_transient_dead_letter_error(DBAPIError("stmt", None, orig)) is transient


# ── Fast operator-cancel watcher ─────────────────────────────────────────────


class _StateResult:
    """Fake ``db.execute`` result whose ``scalar_one_or_none`` yields a state."""

    def __init__(self, state):
        self._state = state

    def scalar_one_or_none(self):
        return self._state


class _StateDb:
    """Fake session returning a (possibly changing) execution state per query."""

    def __init__(self, states):
        self._states = list(states)
        self._i = 0

    async def execute(self, _stmt):
        state = self._states[min(self._i, len(self._states) - 1)]
        self._i += 1
        return _StateResult(state)


def test_start_cancel_watcher_optout_returns_none():
    # An adapter that does not set cancel_poll_seconds gets NO fast watcher: the
    # lease heartbeat is then the only producer of the event. The event
    # belongs to the caller and must come back untouched -- it exists whether or
    # not the adapter wants polling, because the heartbeat fires it either way.
    for poll in (None, 0, -1):
        adapter = SimpleNamespace(name="a", cancel_poll_seconds=poll)
        event = asyncio.Event()
        watcher = reconciler._start_cancel_watcher(adapter, uuid.uuid4(), event)
        assert watcher is None
        assert not event.is_set()


@pytest.mark.asyncio
async def test_watch_execution_cancel_fires_on_terminal_state(monkeypatch):
    # The watcher must fire the event the first poll that sees a terminal state
    # (operator cancel committed 'cancelled' from the web process).
    db = _StateDb(["cancelled"])
    monkeypatch.setattr(reconciler, "async_session", lambda: _FakeSessionCtx(db))

    event = asyncio.Event()
    # Tiny poll so the test is fast; the first sleep elapses then the row reads
    # terminal and the event fires.
    await asyncio.wait_for(
        reconciler._watch_execution_cancel(uuid.uuid4(), event, 0.01), timeout=1.0
    )
    assert event.is_set()


@pytest.mark.asyncio
async def test_watch_execution_cancel_stays_unset_while_running(monkeypatch):
    # While the row is still 'running', the event must NOT fire; it fires only
    # once the state flips terminal.
    db = _StateDb(["running", "running", "cancelled"])
    monkeypatch.setattr(reconciler, "async_session", lambda: _FakeSessionCtx(db))

    event = asyncio.Event()
    watcher = asyncio.create_task(
        reconciler._watch_execution_cancel(uuid.uuid4(), event, 0.01)
    )
    try:
        await asyncio.wait_for(event.wait(), timeout=1.0)
    finally:
        await reconciler._stop_cancel_watcher(watcher)
    # It only fired after the state actually went terminal (>= 3 polls).
    assert event.is_set()
    assert db._i >= 3


@pytest.mark.asyncio
async def test_watch_execution_cancel_fires_on_missing_row(monkeypatch):
    # A deleted execution (row gone) is also a stop signal.
    db = _StateDb([None])
    monkeypatch.setattr(reconciler, "async_session", lambda: _FakeSessionCtx(db))

    event = asyncio.Event()
    await asyncio.wait_for(
        reconciler._watch_execution_cancel(uuid.uuid4(), event, 0.01), timeout=1.0
    )
    assert event.is_set()


# ── Wakes ────────────────────────────────────────────────────────────


class _BufferedPubSub:
    """A wake channel holding ``buffered`` nudges; ``fail_drain`` breaks the reads after the first."""

    def __init__(self, buffered: int, *, fail_drain: bool = False):
        self.buffered = buffered
        self.fail_drain = fail_drain
        self.reads = 0

    async def get_message(self, *, ignore_subscribe_messages, timeout):
        self.reads += 1
        if self.reads > 1 and self.fail_drain:
            raise ConnectionError("connection reset")
        if self.buffered == 0:
            if timeout:
                await asyncio.sleep(timeout)
            return None
        self.buffered -= 1
        return {"type": "message", "data": "wake"}


@pytest.mark.asyncio
async def test_a_burst_of_wakes_costs_one_cycle(monkeypatch):
    monkeypatch.setattr(reconciler.settings, "reconciler_interval_seconds", 30)
    pubsub = _BufferedPubSub(250)

    await asyncio.wait_for(reconciler._sleep_or_wake(pubsub), timeout=1.0)

    assert pubsub.buffered == 0, "every buffered wake is consumed with the first"


@pytest.mark.asyncio
async def test_coalescing_stops_at_its_bound(monkeypatch):
    monkeypatch.setattr(reconciler.settings, "reconciler_interval_seconds", 30)
    monkeypatch.setattr(reconciler, "_MAX_COALESCED_WAKES", 5)
    pubsub = _BufferedPubSub(20)

    await asyncio.wait_for(reconciler._sleep_or_wake(pubsub), timeout=1.0)

    assert pubsub.buffered == 20 - 1 - 5


@pytest.mark.asyncio
async def test_a_channel_error_while_coalescing_still_wakes(monkeypatch):
    monkeypatch.setattr(reconciler.settings, "reconciler_interval_seconds", 30)
    pubsub = _BufferedPubSub(5, fail_drain=True)

    await asyncio.wait_for(reconciler._sleep_or_wake(pubsub), timeout=1.0)

    assert pubsub.reads == 2


def _stub_execution(monkeypatch, run):
    async def _no_heartbeat(*_args, **_kwargs):
        await asyncio.sleep(3600)

    monkeypatch.setattr(reconciler, "_heartbeat", _no_heartbeat)
    monkeypatch.setattr(reconciler, "_do_execute", run)


@pytest.mark.asyncio
async def test_the_wake_after_an_assignment_execution_never_raises(monkeypatch):
    wakes: list[int] = []

    async def _done(*_args, **_kwargs):
        return None

    async def _failing_wake():
        wakes.append(1)
        raise ConnectionError("redis is down")

    _stub_execution(monkeypatch, _done)
    monkeypatch.setattr(reconciler.engine, "notify_orchestration_wake", _failing_wake)

    await reconciler._run_execution(uuid.uuid4(), "assignment", "w1")

    assert wakes == [1]


@pytest.mark.asyncio
async def test_a_cancelled_assignment_execution_stays_cancelled_and_wakes_nobody(monkeypatch):
    wakes: list[int] = []
    started = asyncio.Event()

    async def _forever(*_args, **_kwargs):
        started.set()
        await asyncio.sleep(3600)

    async def _wake():
        wakes.append(1)

    _stub_execution(monkeypatch, _forever)
    monkeypatch.setattr(reconciler.engine, "notify_orchestration_wake", _wake)

    task = asyncio.create_task(reconciler._run_execution(uuid.uuid4(), "assignment", "w1"))
    await asyncio.wait_for(started.wait(), timeout=1.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert wakes == []


# --- The heartbeat is what stops abandoned work ----------------------


@pytest.mark.asyncio
async def test_heartbeat_does_not_cancel_on_a_transient_error(monkeypatch):
    """Could-not-tell is NOT the same as not-ours.

    Deliberately fail-OPEN, against the constitution's fail-closed default: a
    database blip must not abandon work nobody has taken, which would turn a
    transient fault into a stranded run. Only ``LeaseLostError`` cancels.

    Measured on a virtual clock: the heartbeat's sleeps return at once and
    the test waits for the beats themselves, never for wall-clock time, so a
    loaded CI worker waking late cannot fail it.
    """
    monkeypatch.setattr(reconciler.settings, "orchestration_lease_seconds", 3)
    monkeypatch.setattr(
        reconciler, "async_session", lambda: _FakeSessionCtx(_FakeDb())
    )

    real_sleep = asyncio.sleep
    sleeps: list[float] = []

    async def _virtual_sleep(seconds, *_args, **_kwargs):
        sleeps.append(seconds)
        await real_sleep(0)

    monkeypatch.setattr(reconciler.asyncio, "sleep", _virtual_sleep)

    calls = {"n": 0}
    beaten_through = asyncio.Event()

    async def _blip(_db, **_k):
        calls["n"] += 1
        if calls["n"] >= 3:
            beaten_through.set()
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(reconciler.engine, "extend_lease", _blip)

    event = asyncio.Event()
    task = asyncio.create_task(reconciler._heartbeat(uuid.uuid4(), "w1", event))
    try:
        # Three failed beats in a row: the loop survived two transient errors.
        await asyncio.wait_for(beaten_through.wait(), timeout=10)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    assert calls["n"] >= 3, (
        "the heartbeat must KEEP BEATING through a transient error, not return"
    )
    assert all(s == max(1, 3 // 3) for s in sleeps[: calls["n"]]), (
        f"each beat waits one interval (lease // 3), saw {sleeps}"
    )
    assert not event.is_set(), (
        "a transient error must not cancel — that would abandon work nobody "
        "has taken (FR-007)"
    )


@pytest.mark.asyncio
async def test_a_lost_lease_stops_the_work_within_one_beat_with_no_extra_query(
    monkeypatch, caplog
):
    """A lost lease STOPS the work, not just the heartbeat.

    The beat that finds the lease gone fires the event within one interval
    (``lease_seconds // 3``), measured rather than restated. Each beat is one
    ``extend_lease`` call and nothing more: the signal adds no query of its own.
    The stop is logged at INFO with the execution and the worker, because
    ``debug`` is off in production and an operator must find why a run stopped.

    Measured on a virtual clock: the heartbeat's sleeps advance it, so
    the bound is exact and a loaded CI worker waking late cannot fail it.
    """
    monkeypatch.setattr(reconciler.settings, "orchestration_lease_seconds", 3)
    monkeypatch.setattr(
        reconciler, "async_session", lambda: _FakeSessionCtx(_FakeDb())
    )

    clock = {"now": 0.0}
    sleeps: list[float] = []
    real_sleep = asyncio.sleep

    async def _virtual_sleep(seconds, *_args, **_kwargs):
        sleeps.append(seconds)
        clock["now"] += seconds
        await real_sleep(0)

    monkeypatch.setattr(reconciler.asyncio, "sleep", _virtual_sleep)

    beats: list[float] = []

    async def _extend(_db, **_k):
        beats.append(clock["now"])
        if len(beats) >= 2:
            raise reconciler.engine.LeaseLostError("gone")

    monkeypatch.setattr(reconciler.engine, "extend_lease", _extend)

    execution_id = uuid.uuid4()
    event = asyncio.Event()
    started = clock["now"]
    with caplog.at_level(logging.INFO, logger=reconciler.logger.name):
        await asyncio.wait_for(
            reconciler._heartbeat(execution_id, "worker-xyz", event), timeout=6
        )
    stopped = clock["now"]

    assert event.is_set(), (
        "a lost lease must fire the cancel event: without it the abandoned "
        "worker carries on and both workers judge the same runs"
    )
    assert len(beats) == 2, (
        f"expected exactly one extend_lease per beat, saw {len(beats)} for two "
        "beats: the cancel signal must add no query of its own"
    )

    interval = max(1, 3 // 3)
    # Exactly one interval, never two: an implementation that burned an extra
    # beat must fail.
    assert sleeps == [interval, interval], (
        f"expected one sleep of {interval}s per beat, saw {sleeps}"
    )
    assert beats[0] - started == interval
    assert stopped - beats[0] == interval, (
        f"the stop took {stopped - beats[0]:.2f}s after the last good beat, "
        f"beyond one interval of {interval}s"
    )

    assert any(
        r.levelno >= logging.INFO
        and str(execution_id) in r.getMessage()
        and "worker-xyz" in r.getMessage()
        for r in caplog.records
    ), "the stop is not logged at INFO with the execution and the worker"


@pytest.mark.asyncio
async def test_the_fast_watcher_fires_the_callers_event_not_its_own(monkeypatch):
    """The watcher must watch the event the caller owns.

    ``_run_execution`` creates the event and the heartbeat fires it too, so a
    watcher that built its own would set something nobody reads: the fast
    operator-cancel path would silently stop working, and no other test
    notices a fresh ``asyncio.Event()`` swapped in for the argument.
    """
    db = _StateDb(["cancelled"])
    monkeypatch.setattr(reconciler, "async_session", lambda: _FakeSessionCtx(db))

    caller_event = asyncio.Event()
    adapter = SimpleNamespace(name="a", cancel_poll_seconds=0.01)
    watcher = reconciler._start_cancel_watcher(adapter, uuid.uuid4(), caller_event)
    try:
        await asyncio.wait_for(caller_event.wait(), timeout=5)
    finally:
        await reconciler._stop_cancel_watcher(watcher)

    assert caller_event.is_set(), (
        "the watcher fired an event other than the caller's — the operator "
        "cancel path would stop working with nothing to show for it"
    )


@pytest.mark.asyncio
async def test_do_execute_hands_the_adapter_the_very_event_it_was_given(monkeypatch):
    """Identity, not just presence.

    ``_run_execution`` creates ONE event and gives it to both the heartbeat and
    ``_do_execute``. If ``_do_execute`` passed the adapter a different event —
    or built a fresh one for the watcher — the heartbeat would fire something
    the adapter never reads, and abandoned work would carry on with every test
    still green. Asserting ``is not None`` does not catch that; asserting
    identity does.
    """
    from app.services import orchestration_engine as engine
    from app.services.orchestration_adapters.base import (
        AdapterContext,
        AdapterResult,
        OrchestrationAdapter,
    )

    monkeypatch.setattr(
        reconciler, "async_session", lambda: _FakeSessionCtx(_FakeDb())
    )
    execution = SimpleNamespace(
        state="running", lease_owner="w1", deadline_at=None, payload={}
    )

    async def _get_execution(_db, _id):
        return execution

    monkeypatch.setattr(engine, "get_execution", _get_execution)

    seen: dict[str, object] = {}

    class _StubAdapter(OrchestrationAdapter):
        name = "stub"
        cancel_poll_seconds = None  # opts OUT of fast polling, like evaluation

        async def execute(self, ctx: AdapterContext, _execution) -> AdapterResult:
            seen["event"] = ctx.cancel_event
            return AdapterResult.completed()

    monkeypatch.setattr(reconciler, "get_adapter", lambda _name: _StubAdapter())

    async def _complete(_db, **_k):
        return None

    monkeypatch.setattr(engine, "complete", _complete)

    mine = asyncio.Event()
    await reconciler._do_execute(uuid.uuid4(), "stub", "w1", mine, stop_on_lease_loss=True)

    assert seen["event"] is mine, (
        "the adapter was handed a different event from the one the heartbeat "
        "fires — a lost lease would set an event nobody reads"
    )


# --- Only an adapter that opted in is stopped by a lost lease ---------


def test_only_the_evaluation_adapter_opts_in_to_stopping_on_lease_loss():
    """The lease-loss stop is for evaluation, not autonomous agents.

    The autonomous adapter reads ``cancel_event`` for operator cancels, so a
    heartbeat firing it for every execution stops autonomous runs mid-cycle on
    a lost lease -- a change to how agents run that was never decided. Each
    adapter's answer is pinned here, so opting
    another one in is a deliberate, visible change. Read from the classes, not
    the process-wide registry, which other tests replace.
    """
    from app.services.orchestration_adapters.assignment import AssignmentAdapter
    from app.services.orchestration_adapters.autonomous import (
        AutonomousPentestAdapter,
    )
    from app.services.orchestration_adapters.evaluation import EvaluationAdapter
    from app.services.orchestration_adapters.flow import FlowAdapter
    from app.services.orchestration_adapters.sub_agent import SubAgentAdapter

    assert {
        cls.__name__: cls.stop_on_lease_loss
        for cls in (
            EvaluationAdapter,
            AutonomousPentestAdapter,
            SubAgentAdapter,
            AssignmentAdapter,
            FlowAdapter,
        )
    } == {
        "EvaluationAdapter": True,
        "AutonomousPentestAdapter": False,
        "SubAgentAdapter": False,
        "AssignmentAdapter": False,
        "FlowAdapter": False,
    }


async def _run_with_a_lost_lease(
    monkeypatch, *, stop_on_lease_loss: bool, cancel_poll_seconds: float | None = None
) -> dict:
    """Drive ``_run_execution`` while every heartbeat finds the lease gone.

    Returns what the adapter saw, and how many heartbeats ran -- so a "not
    stopped" result cannot come from a heartbeat that never beat. A positive
    ``cancel_poll_seconds`` starts the fast watcher, as the autonomous adapter
    does; it sleeps before its first poll, so a long interval keeps it quiet
    for the test and leaves the heartbeat as the only possible producer.
    """
    from app.services import orchestration_engine as engine
    from app.services.orchestration_adapters.base import (
        AdapterResult,
        OrchestrationAdapter,
    )

    monkeypatch.setattr(reconciler.settings, "orchestration_lease_seconds", 3)
    seen: dict = {"beats": 0}

    class _Adapter(OrchestrationAdapter):
        name = "stub"

        async def execute(self, ctx, _execution):
            seen["cancel_event"] = ctx.cancel_event
            # Longer than one heartbeat interval (lease // 3 == 1 s).
            if ctx.cancel_event is None:
                await asyncio.sleep(1.6)
            else:
                try:
                    await asyncio.wait_for(ctx.cancel_event.wait(), timeout=1.6)
                    seen["stopped"] = True
                except TimeoutError:
                    seen["stopped"] = False
            return AdapterResult.completed()

    _Adapter.stop_on_lease_loss = stop_on_lease_loss
    _Adapter.cancel_poll_seconds = cancel_poll_seconds
    execution = SimpleNamespace(
        state="running", lease_owner="w1", deadline_at=None, payload={}
    )

    async def _get_execution(_db, _id):
        return execution

    async def _lost(_db, **_k):
        seen["beats"] += 1
        raise engine.LeaseLostError("gone")

    async def _complete(_db, **_k):
        raise engine.LeaseLostError("the lease check refuses the write")

    monkeypatch.setattr(engine, "get_execution", _get_execution)
    monkeypatch.setattr(engine, "extend_lease", _lost)
    monkeypatch.setattr(engine, "complete", _complete)
    monkeypatch.setattr(
        reconciler, "async_session", lambda: _FakeSessionCtx(_FakeDb())
    )
    monkeypatch.setattr(reconciler, "get_adapter", lambda _n: _Adapter())

    await asyncio.wait_for(reconciler._run_execution(uuid.uuid4(), "stub", "w1"), timeout=5)
    return seen


@pytest.mark.asyncio
async def test_a_lost_lease_does_not_stop_an_adapter_that_did_not_opt_in(monkeypatch):
    """Every adapter but evaluation keeps running on a lost lease.

    The work runs to the end of its unit; the lease check refuses its write.
    With no fast watcher either, the adapter is handed no event at all: the
    agent loop treats ``None`` as an exact passthrough, and
    races every await against anything else.
    """
    seen = await _run_with_a_lost_lease(monkeypatch, stop_on_lease_loss=False)

    assert seen["beats"] >= 1, "the heartbeat never ran, so this proved nothing"
    assert seen["cancel_event"] is None


@pytest.mark.asyncio
async def test_a_lost_lease_does_not_stop_an_adapter_that_reads_the_event_for_cancels(
    monkeypatch,
):
    """The autonomous shape.

    The adapter reads ``cancel_event`` because it wants operator cancels (the
    fast watcher), not because it opted in to lease loss. It must be handed the
    event -- and a lost lease must NOT fire it. Firing the heartbeat's event for
    every adapter fails here.
    """
    seen = await _run_with_a_lost_lease(
        monkeypatch, stop_on_lease_loss=False, cancel_poll_seconds=30
    )

    assert seen["beats"] >= 1, "the heartbeat never ran, so this proved nothing"
    assert seen["cancel_event"] is not None
    assert seen["stopped"] is False, "a lost lease stopped an adapter that never opted in"


@pytest.mark.asyncio
async def test_a_lost_lease_still_stops_an_adapter_that_opted_in(monkeypatch):
    seen = await _run_with_a_lost_lease(monkeypatch, stop_on_lease_loss=True)

    assert seen["beats"] >= 1
    assert seen["stopped"] is True


