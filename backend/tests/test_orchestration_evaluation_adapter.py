"""The evaluation adapter (``EvaluationAdapter``) beyond a plain run.

Stranded-headline recovery on failure and dead letter, the ``rescore_runs`` payload and
its retries, lease loss and shutdown cancellation, the two independent janitors of
``periodic``, and the basis repair a dead-lettered rescore triggers.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.orchestration_adapters import AdapterContext
from app.services.orchestration_adapters.evaluation import EvaluationAdapter
from tests.test_orchestration_adapters import (
    _agentic_gate_available,  # noqa: F401 - autouse fixture
    _FakeDb,
    _isolate_registry,  # noqa: F401 - autouse fixture
)

pytestmark = pytest.mark.usefixtures("evaluation_enabled")  # evaluation is off by default

# ── EvaluationAdapter: recovering stranded headlines ──


@pytest.mark.asyncio
async def test_evaluation_adapter_failure_recovers_stranded_headlines(monkeypatch):
    """A failed execute() must attempt stranded-headline recovery."""
    from app.services import evaluation_engine
    from app.services.orchestration_adapters import evaluation as orchestration_adapters_evaluation

    fail_calls = []
    recover_calls = []

    async def _fake_execute(_db, evaluation_id, trigger_reason, _execution_id, **_kw):
        raise RuntimeError("boom")

    async def _fake_fail_active_runs(evaluation_id, _execution_id, error, **_kw):
        fail_calls.append(evaluation_id)

    async def _fake_recover(evaluation_id):
        recover_calls.append(evaluation_id)

    monkeypatch.setattr(evaluation_engine, "execute_evaluation", _fake_execute)
    monkeypatch.setattr(
        orchestration_adapters_evaluation, "_fail_active_runs", _fake_fail_active_runs
    )
    monkeypatch.setattr(
        orchestration_adapters_evaluation, "_recover_stranded_headlines", _fake_recover
    )
    monkeypatch.setattr(
        orchestration_adapters_evaluation, "_flush_langfuse", lambda *_a, **_k: _noop_awaitable()
    )
    ctx = AdapterContext(db=_FakeDb(), worker_id="w1")
    execution = SimpleNamespace(
        id=uuid.uuid4(),
        payload={"evaluation_id": "ev1", "trigger_reason": "schedule"},
    )

    result = await EvaluationAdapter().execute(ctx, execution)
    assert result.kind == "failed"
    assert "boom" in result.error
    assert fail_calls == ["ev1"]
    assert recover_calls == ["ev1"]


@pytest.mark.asyncio
async def test_evaluation_adapter_dead_letter_recovers_stranded_headlines(monkeypatch):
    """``on_dead_letter`` must also attempt stranded-headline recovery."""
    from app.services.orchestration_adapters import evaluation as orchestration_adapters_evaluation

    fail_calls = []
    recover_calls = []

    async def _fake_fail_active_runs(evaluation_id, _execution_id, error, **_kw):
        fail_calls.append(evaluation_id)

    async def _fake_recover(evaluation_id):
        recover_calls.append(evaluation_id)

    monkeypatch.setattr(
        orchestration_adapters_evaluation, "_fail_active_runs", _fake_fail_active_runs
    )
    monkeypatch.setattr(
        orchestration_adapters_evaluation, "_recover_stranded_headlines", _fake_recover
    )
    ctx = AdapterContext(db=_FakeDb(), worker_id="w1")
    execution = SimpleNamespace(
        id=uuid.uuid4(),
        payload={"evaluation_id": "ev1"},
        last_error="deadline exceeded",
    )

    await EvaluationAdapter().on_dead_letter(ctx, execution)
    assert fail_calls == ["ev1"]
    assert recover_calls == ["ev1"]


@pytest.mark.asyncio
async def test_recover_stranded_headlines_swallows_errors_and_logs_a_warning(monkeypatch, caplog):
    """Recovery failures must never propagate — only be logged as a warning."""
    from app.services.orchestration_adapters import evaluation as evaluation_adapter_mod

    def _boom():
        raise RuntimeError("db unreachable")

    monkeypatch.setattr("app.core.database.async_session", _boom)

    # A valid UUID, so the failure genuinely comes from `async_session()`
    # raising rather than from `uuid.UUID(...)` rejecting the id first.
    broken_id = str(uuid.uuid4())
    with caplog.at_level("WARNING"):
        # Must not raise.
        await evaluation_adapter_mod._recover_stranded_headlines(broken_id)

    assert any(
        r.levelname == "WARNING"
        and "Stranded-headline recovery failed" in r.message
        and broken_id in r.message
        for r in caplog.records
    )


async def _noop_awaitable():
    return None


# ── EvaluationAdapter: rescore_runs retry idempotence ─────────────────
#
# The durable engine can reclaim an expired lease and re-dispatch the SAME
# ``rescore_runs`` payload after a worker crash. A run's score mutations
# commit in one transaction and its terminal status flips in a later one, so
# a crash between the two leaves the run "running" — exactly the state a
# retry re-processes, double-charging the judge model.
#
# The guard is NOT an ``adapter_state`` checkpoint: every engine retry path
# that applies here (``fail`` -> pending, ``reclaim_expired_leases``,
# ``requeue_execution``, ``reset_execution``) clears ``adapter_state``, and
# ``await_event`` — the one transition that preserves it — is never used by a
# rescore. The run's own status is the durable key instead, re-checked inside
# ``_rescore_run_with_own_session`` (see
# ``tests/test_evaluation_rescore.py``). These tests pin the
# adapter's half: it always hands the full payload down, and it flushes
# Langfuse the same way the full-evaluation branch does.


@pytest.mark.asyncio
async def test_rescore_runs_dispatches_the_whole_payload_and_flushes_langfuse(monkeypatch):
    """Every named run is dispatched, whatever ``adapter_state`` holds: a retry
    re-dispatches the whole payload and the per-run status guard decides what
    is rescored. The judge calls' Langfuse spans are flushed on success and on
    failure, like the full-evaluation branch."""
    from app.services import evaluation_engine
    from app.services.orchestration_adapters import evaluation as adapter_mod

    flushes = []

    async def _fake_flush(context):
        flushes.append(context)

    monkeypatch.setattr(adapter_mod, "_flush_langfuse", _fake_flush)

    dispatched = []

    async def _fake_rescore_with_own_session(run_id, **_kwargs):
        dispatched.append(run_id)

    monkeypatch.setattr(
        evaluation_engine, "_rescore_run_with_own_session", _fake_rescore_with_own_session
    )
    # Non-UUID fake run ids: the DB-truth gate is stubbed rather than parsed.
    monkeypatch.setattr(
        evaluation_engine, "_batch_has_a_completed_run", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(
        evaluation_engine, "_recompute_shared_performance_basis", AsyncMock()
    )

    run_ids = ["run-1", "run-2", "run-3"]
    execution = SimpleNamespace(
        id=uuid.uuid4(),
        payload={
            "kind": "rescore_runs",
            "run_ids": run_ids,
            "scorer_selection": None,
            "previous_statuses": {rid: "completed" for rid in run_ids},
        },
        # A reclaimed lease arrives with adapter_state cleared; an inherited
        # value must not filter the dispatch either.
        adapter_state='["run-1"]',
    )
    ok = await EvaluationAdapter().execute(
        AdapterContext(db=_FakeDb(), worker_id="w2"), execution
    )
    assert ok.kind == "completed"
    assert dispatched == run_ids
    assert len(flushes) == 1

    failed_runs = []

    async def _fake_fail(run_ids, _execution_id, _error, _previous_statuses):
        failed_runs.extend(run_ids)

    async def _boom(_run_ids, **_kw):
        raise RuntimeError("the database went away")

    monkeypatch.setattr(adapter_mod, "_fail_rescore_runs", _fake_fail)
    monkeypatch.setattr(evaluation_engine, "execute_rescore_runs", _boom)

    ko = await EvaluationAdapter().execute(
        AdapterContext(db=_FakeDb(), worker_id="w3"), execution
    )
    assert ko.kind == "failed"
    assert ko.retryable is False
    assert failed_runs == run_ids
    assert len(flushes) == 2, "the flush must also happen when the batch fails"


@pytest.mark.asyncio
async def test_rescore_runs_shared_performance_recompute_failure_is_retryable(
    monkeypatch,
):
    """A failure in the coalesced performance pass is retryable:
    by the time ``SharedPerformanceRecomputeError``
    can be raised, every run in the batch is already terminal, so retrying
    the whole payload can never re-judge a run still ``"running"`` -- unlike
    an unclassified batch failure, which stays non-retryable."""
    from app.services import evaluation_engine
    from app.services.orchestration_adapters import evaluation as adapter_mod

    monkeypatch.setattr(adapter_mod, "_flush_langfuse", AsyncMock())

    failed_runs = []

    async def _fake_fail(run_ids, _execution_id, _error, _previous_statuses):
        failed_runs.extend(run_ids)

    monkeypatch.setattr(adapter_mod, "_fail_rescore_runs", _fake_fail)

    async def _boom(_run_ids, **_kw):
        raise evaluation_engine.SharedPerformanceRecomputeError(
            "transient DB error during the shared pass"
        )

    monkeypatch.setattr(evaluation_engine, "execute_rescore_runs", _boom)

    execution = SimpleNamespace(
        id=uuid.uuid4(),
        payload={
            "kind": "rescore_runs",
            "run_ids": ["run-1"],
            "scorer_selection": None,
            "previous_statuses": {"run-1": "completed"},
        },
        adapter_state=None,
    )
    result = await EvaluationAdapter().execute(
        AdapterContext(db=_FakeDb(), worker_id="w4"), execution
    )
    assert result.kind == "failed"
    assert result.retryable is True
    assert failed_runs == [], (
        "no run can still be \"running\" at this point, so "
        "_fail_rescore_runs must not be invoked"
    )


@pytest.mark.asyncio
async def test_a_dead_lettered_rescore_fails_its_own_runs_then_repairs_their_basis(
    monkeypatch,
):
    """A dead-lettered rescore fails exactly its own run ids, with the abandoned
    execution's id so cleanup only touches runs it still owns, and never
    falls through to the evaluation-wide sweep, which would fail a sibling run
    genuinely running from another trigger.

    Its performance basis is then recomputed: the runs are already terminal when
    the coalesced pass dead-letters, so ``_fail_rescore_runs`` matches nothing
    and nothing else revisits the basis. Runs first, basis second, so a
    run this hook just failed is not in the basis the recompute reads.
    """
    from app.services.orchestration_adapters import evaluation as adapter_mod

    calls = []
    evaluation_wide_calls = []

    async def _fake_fail_rescore_runs(run_ids, execution_id, error, _previous_statuses):
        calls.append(("fail", list(run_ids), execution_id, error))

    async def _fake_recompute(run_ids):
        calls.append(("recompute", list(run_ids)))

    async def _fake_fail_active_runs(evaluation_id, _execution_id, _error, **_kw):
        evaluation_wide_calls.append(evaluation_id)

    async def _fake_recover(evaluation_id):
        evaluation_wide_calls.append(evaluation_id)

    monkeypatch.setattr(adapter_mod, "_fail_rescore_runs", _fake_fail_rescore_runs)
    monkeypatch.setattr(
        "app.services.evaluation_engine._recompute_shared_performance_basis",
        _fake_recompute,
    )
    monkeypatch.setattr(adapter_mod, "_fail_active_runs", _fake_fail_active_runs)
    monkeypatch.setattr(adapter_mod, "_recover_stranded_headlines", _fake_recover)

    ctx = AdapterContext(db=_FakeDb(), worker_id="w1")
    dead_execution_id = uuid.uuid4()
    execution = SimpleNamespace(
        id=dead_execution_id,
        payload={
            "kind": "rescore_runs",
            "run_ids": ["run-1", "run-2"],
            "previous_statuses": {"run-1": "completed", "run-2": "completed"},
            # An evaluation_id in the payload must not re-open the
            # evaluation-wide path for a rescore.
            "evaluation_id": "ev1",
        },
        last_error="deadline exceeded",
    )

    await EvaluationAdapter().on_dead_letter(ctx, execution)

    assert calls == [
        ("fail", ["run-1", "run-2"], dead_execution_id, "deadline exceeded"),
        ("recompute", ["run-1", "run-2"]),
    ]
    assert evaluation_wide_calls == []


# ── EvaluationAdapter: lease loss and shutdown cancellation ──


class _CancelSessionCtx:
    """Minimal async session context for the end-to-end cancellation tests."""

    def __init__(self, db):
        self._db = db

    async def __aenter__(self):
        return self._db

    async def __aexit__(self, *_exc):
        return False


def _rescore_cancel_harness(monkeypatch, execute_rescore_runs_impl):
    """Same seam as ``_cancel_harness``, for the ``rescore_runs`` payload.

    The branch's ``CancelledError`` -> ``LeaseLostError`` classification needs
    end-to-end cover: tests that call ``_gather_runs_bounded`` directly stay
    green with it deleted.
    """
    import uuid as _uuid

    from app.services import evaluation_engine as ee
    from app.services import orchestration_engine as engine
    from app.services import orchestration_reconciler as reconciler
    from app.services.orchestration_adapters import evaluation as adapter_mod

    terminal_writes: list[str] = []

    async def _complete(_db, **_k):
        terminal_writes.append("complete")

    async def _fail(_db, **_k):
        terminal_writes.append("fail")
        return SimpleNamespace(state="failed")

    monkeypatch.setattr(engine, "complete", _complete)
    monkeypatch.setattr(engine, "fail", _fail)

    execution = SimpleNamespace(
        state="running",
        lease_owner="w1",
        deadline_at=None,
        payload={
            "kind": "rescore_runs",
            "run_ids": ["r0", "r1", "r2"],
            "previous_statuses": {"r0": "completed", "r1": "completed", "r2": "completed"},
        },
        id=_uuid.uuid4(),
    )

    async def _get_execution(_db, _id):
        return execution

    async def _no_fail_rescore(*_a, **_k):
        terminal_writes.append("_fail_rescore_runs")

    monkeypatch.setattr(engine, "get_execution", _get_execution)
    monkeypatch.setattr(
        reconciler, "async_session", lambda: _CancelSessionCtx(_FakeDb())
    )
    monkeypatch.setattr(reconciler, "get_adapter", lambda _n: EvaluationAdapter())
    monkeypatch.setattr(ee, "execute_rescore_runs", execute_rescore_runs_impl)
    monkeypatch.setattr(adapter_mod, "_fail_rescore_runs", _no_fail_rescore)
    return terminal_writes, execution


def _cancel_harness(monkeypatch, execute_evaluation_impl):
    """Wire _do_execute -> EvaluationAdapter -> a stubbed execute_evaluation.

    Returns the list terminal writes land in, and the execution.
    """
    import uuid as _uuid

    from app.services import evaluation_engine as ee
    from app.services import orchestration_engine as engine
    from app.services import orchestration_reconciler as reconciler

    terminal_writes: list[str] = []

    async def _complete(_db, **_k):
        terminal_writes.append("complete")

    async def _fail(_db, **_k):
        terminal_writes.append("fail")
        return SimpleNamespace(state="failed")

    monkeypatch.setattr(engine, "complete", _complete)
    monkeypatch.setattr(engine, "fail", _fail)

    execution = SimpleNamespace(
        state="running",
        lease_owner="w1",
        deadline_at=None,
        payload={"evaluation_id": str(_uuid.uuid4()), "trigger_reason": "manual"},
        id=_uuid.uuid4(),
    )

    async def _get_execution(_db, _id):
        return execution

    monkeypatch.setattr(engine, "get_execution", _get_execution)
    monkeypatch.setattr(
        reconciler, "async_session", lambda: _CancelSessionCtx(_FakeDb())
    )
    monkeypatch.setattr(reconciler, "get_adapter", lambda _n: EvaluationAdapter())
    monkeypatch.setattr(ee, "execute_evaluation", execute_evaluation_impl)
    return terminal_writes, execution


_CANCEL_HARNESSES = {
    "evaluation": _cancel_harness,
    "rescore_runs": _rescore_cancel_harness,
}


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", list(_CANCEL_HARNESSES))
async def test_a_lost_lease_raises_lease_lost_and_writes_no_terminal_state(
    monkeypatch, kind
):
    """The adapter turns a cancellation with its event set into ``LeaseLostError``.

    Raised from ``execute`` itself, on both branches: ``_do_execute``'s own
    ``CancelledError`` arm also writes nothing, so only the exception type shows
    the adapter classified the stop. Then end to end through ``_do_execute``: an
    abandoned worker writes no terminal state on a row it no longer owns, and
    ``_fail_rescore_runs`` counts as a write, since reaching it would fail runs
    the replacement is about to complete.
    """
    from app.services import orchestration_reconciler as reconciler
    from app.services.orchestration_engine import LeaseLostError

    cancel_event = asyncio.Event()

    async def _lease_moves(*_a, **_kw):
        cancel_event.set()
        raise asyncio.CancelledError()

    terminal_writes, execution = _CANCEL_HARNESSES[kind](monkeypatch, _lease_moves)

    ctx = AdapterContext(db=_FakeDb(), worker_id="w1", cancel_event=cancel_event)
    with pytest.raises(LeaseLostError):
        await EvaluationAdapter().execute(ctx, execution)

    cancel_event.clear()
    await reconciler._do_execute(
        execution.id,
        "evaluation",
        "w1",
        cancel_event,
        stop_on_lease_loss=reconciler._stops_on_lease_loss("evaluation"),
    )

    assert terminal_writes == [], (
        f"the {kind} path wrote {terminal_writes} on a row it no longer owns"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", list(_CANCEL_HARNESSES))
async def test_a_shutdown_cancellation_is_not_reported_as_a_lost_lease(
    monkeypatch, kind
):
    """With the event UNSET a cancellation is a worker shutdown and propagates.

    Classifying every ``CancelledError`` as a lease loss would silence a real
    shutdown and report it as a routine hand-off.
    """
    from app.services import orchestration_reconciler as reconciler

    cancel_event = asyncio.Event()  # deliberately never set

    async def _shutdown(*_a, **_kw):
        raise asyncio.CancelledError()

    terminal_writes, execution = _CANCEL_HARNESSES[kind](monkeypatch, _shutdown)
    with pytest.raises(asyncio.CancelledError):
        await reconciler._do_execute(
            execution.id,
            "evaluation",
            "w1",
            cancel_event,
            stop_on_lease_loss=reconciler._stops_on_lease_loss("evaluation"),
        )

    assert terminal_writes == [], (
        f"a genuine shutdown wrote {terminal_writes} instead of propagating"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("max_concurrent", "expected_entered"),
    [
        pytest.param(1, ["r0"], id="sequential"),
        pytest.param(2, None, id="concurrent"),
    ],
)
async def test_a_lease_loss_writes_no_terminal_state_end_to_end(
    monkeypatch, max_concurrent, expected_entered
):
    """The seam no helper-level test can see.

    Drives the REAL wiring: fired event -> ``_gather_runs_bounded`` cancels ->
    ``execute_evaluation`` propagates -> ``EvaluationAdapter.execute`` converts
    to ``LeaseLostError`` -> ``_do_execute`` returns WITHOUT writing. A stop that
    returns normally (``contextlib.suppress``, or a ``break`` between runs in
    the sequential mode local providers use) would report ``completed`` on a
    row the replacement owns.
    """
    from app.services import evaluation_engine as ee
    from app.services import orchestration_reconciler as reconciler

    cancel_event = asyncio.Event()
    entered: list[str] = []

    async def _runner(rid: str) -> None:
        entered.append(rid)
        cancel_event.set()
        await asyncio.sleep(1)

    async def _impl(
        _db, _evaluation_id, _trigger, _execution_id, cancel_event=None, **_kw
    ):
        await ee._gather_runs_bounded(
            ["r0", "r1", "r2"], max_concurrent, _runner, cancel_event=cancel_event
        )

    terminal_writes, execution = _cancel_harness(monkeypatch, _impl)
    await reconciler._do_execute(
        execution.id,
        "evaluation",
        "w1",
        cancel_event,
        stop_on_lease_loss=reconciler._stops_on_lease_loss("evaluation"),
    )

    if expected_entered is not None:
        assert entered == expected_entered, (
            f"the loop did not stop after the lease moved: {entered}"
        )
    assert terminal_writes == [], (
        f"the abandoned worker wrote {terminal_writes} on a row it no longer "
        "owns — a lease loss must reach _do_execute's LeaseLostError handler, "
        "which returns without writing"
    )


# ── EvaluationAdapter.periodic: the two janitors are independent ─────


def _periodic_harness(monkeypatch, *, failing: str):
    """Drive ``periodic`` with one janitor raising and record what still ran.

    ``periodic`` imports both lazily, so patching the module attributes is what
    the call actually resolves.
    """
    from app.services import evaluation_continuation as cont

    ran: list[str] = []

    def _make(name):
        async def _janitor(_db, **_kw):
            if name == failing:
                raise RuntimeError(f"{name} is having a bad day")
            ran.append(name)
            return 0

        _janitor.__name__ = name
        return _janitor

    monkeypatch.setattr(
        cont, "timeout_waiting_episodes", _make("timeout_waiting_episodes")
    )
    monkeypatch.setattr(cont, "sweep_stranded_runs", _make("sweep_stranded_runs"))
    return ran


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failing", "still_ran"),
    [
        pytest.param(
            "timeout_waiting_episodes", "sweep_stranded_runs", id="callback-janitor-fails"
        ),
        pytest.param("sweep_stranded_runs", "timeout_waiting_episodes", id="sweep-fails"),
    ],
)
async def test_one_failing_janitor_neither_stops_the_other_nor_escapes(
    monkeypatch, failing, still_ran
):
    """The two janitors are independent.

    ``timeout_waiting_episodes`` runs first: if its failure skipped the sweep, a
    callback-provider outage would leave stranded runs stranded, in exactly the
    conditions that produce them. The sweep runs second: its failure must not
    escape ``periodic`` to the reconciler, which commits after this returns.
    Either way the failed janitor's transaction is rolled back, so the other
    does not inherit a poisoned session.
    """
    from app.services.orchestration_adapters.base import AdapterContext
    from app.services.orchestration_adapters.evaluation import EvaluationAdapter

    ran = _periodic_harness(monkeypatch, failing=failing)
    db = _FakeDb()

    await EvaluationAdapter().periodic(AdapterContext(db=db, worker_id="w1"))

    assert ran == [still_ran], f"the other janitor did not run after {failing} raised: {ran}"
    assert db.rollbacks == 1, "the failed janitor's transaction was not rolled back"


@pytest.mark.asyncio
async def test_periodic_does_not_leave_a_janitors_run_locks_held(monkeypatch):
    """Both janitors can return holding run locks. The caller must release them.

    Their shared COMMIT CONTRACT says they commit when they act and return
    WITHOUT committing when they find nothing — and both reach a "found nothing"
    return with run rows already locked: ``timeout_waiting_episodes`` takes
    ``SELECT ... FOR UPDATE`` on the runs before the episode re-check that can
    leave it with none, and ``sweep_stranded_runs`` returns from ``not locked``
    and ``not targets`` after its own run lock. The contract puts the duty on the
    caller, on every path, not only the exception one.

    Left held, those locks outlive the hook and head-of-line block the live
    engine's end-of-entry run write — the liveness regression the ``skip_locked``
    comments exist to prevent, reintroduced at the call site. They also make the
    two janitors non-independent: they share a session, so whichever commits
    commits for both.

    MUTATION CHECK: removing the ``else`` branch fails this with 0 rollbacks.
    """
    from app.services.orchestration_adapters.base import AdapterContext
    from app.services.orchestration_adapters.evaluation import EvaluationAdapter

    ran = _periodic_harness(monkeypatch, failing="")
    db = _FakeDb(open_transaction=True)
    await EvaluationAdapter().periodic(AdapterContext(db=db, worker_id="w1"))

    assert ran == ["timeout_waiting_episodes", "sweep_stranded_runs"], (
        f"both janitors must still run: {ran}"
    )
    assert db.rollbacks >= 1, (
        "periodic returned with a transaction still open — a janitor's run "
        "locks outlive the hook and block the live engine"
    )
    assert not db.in_transaction(), (
        "periodic left the session in a transaction it neither committed nor "
        "rolled back"
    )


# ── EvaluationAdapter: repairing the basis a dead-lettered rescore skipped ───
#
# ``execute_rescore_runs`` finalises every run BEFORE its coalesced performance
# pass, so when that pass exhausts the retry budget the runs are already
# terminal and ``_fail_rescore_runs`` -- which only touches rows still
# ``"running"`` -- is a guaranteed no-op. The dead letter repairs the basis,
# since nothing else revisits it.


@pytest.mark.asyncio
async def test_a_failed_basis_repair_is_logged_and_never_escapes_the_dead_letter_hook(
    monkeypatch, caplog
):
    """The repair is a best-effort second chance after the run cleanup.

    A dead letter means the original fault may still be live, so the repair is
    likely to fail too. It must not raise: the reconciler retries
    ``on_dead_letter`` as a whole, so an escaping failure would re-run the
    cleanup indefinitely, and the cleanup already done must stand. The WARNING
    is the only diagnosis, so it names the exception even when its ``str()`` is
    empty (a dead letter is usually a ``TimeoutError``) and carries the
    traceback.
    """
    from app.services.orchestration_adapters import evaluation as adapter_mod

    failed: list[list[str]] = []

    async def _fake_fail(run_ids, _execution_id, _error, _previous_statuses):
        failed.append(list(run_ids))

    async def _timeout(_run_ids):
        raise TimeoutError()  # str() == ""

    monkeypatch.setattr(adapter_mod, "_fail_rescore_runs", _fake_fail)
    monkeypatch.setattr(
        "app.services.evaluation_engine._recompute_shared_performance_basis", _timeout
    )
    ctx = AdapterContext(db=_FakeDb(), worker_id="w1")
    execution = SimpleNamespace(
        id=uuid.uuid4(),
        payload={
            "kind": "rescore_runs",
            "run_ids": ["r1"],
            "previous_statuses": {"r1": "completed"},
        },
        last_error="deadline exceeded",
    )

    with caplog.at_level(logging.WARNING):
        await EvaluationAdapter().on_dead_letter(ctx, execution)

    assert failed == [["r1"]], (
        "the run cleanup that had already succeeded was lost when the repair "
        "after it failed"
    )
    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    repair_lines = [m for m in warnings if "repair the shared performance basis" in m]
    assert repair_lines, "the failed repair logged nothing at all"
    assert "TimeoutError" in repair_lines[0], (
        f"the failure reason is unusable: {repair_lines[0]!r}. An exception with "
        "an empty str() must still name itself, or a best-effort repair that "
        "keeps failing cannot be diagnosed."
    )

    # The message names the exception; only the traceback says where it came
    # from, and this repair swallows everything so there is no other record.
    record = next(
        r
        for r in caplog.records
        if "repair the shared performance basis" in r.getMessage()
    )
    assert record.exc_info is not None, (
        "the failed repair logged no traceback, so a run of these has a name "
        "and no origin"
    )
