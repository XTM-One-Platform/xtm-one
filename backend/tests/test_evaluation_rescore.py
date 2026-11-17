"""Rescoring stored predictions: the background run and the rescore-from-runs endpoint.
"""

import asyncio
import logging
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.api import deps
from app.api.v1 import evaluations as evaluations_api
from app.core.database import get_db
from app.services import evaluation_engine
from tests.evaluation_helpers import HeadlinesFake

# ── A mid-batch rescore failure commits no partial work ───────────────────────────────
# ``rescore_run`` mutates ``EvaluationResult`` rows (and stamps
# ``run.scorer_selection``) in a per-result loop and commits once at the end.
# ``_rescore_run_with_own_session`` runs it in the background and, on any
# exception, hands the run back its status from before the rescore with the failure
# in ``rescore_error``; it must roll back first, or the partial mutations
# are persisted with it.
#
# The fake session models real rollback semantics (an uncommitted mutation reverts
# on ``rollback()`` and becomes the committed value on ``commit()``), so the tests
# assert the rollback by its effect on the data, not a call count.


class _FakeResult:
    def __init__(self, score):
        self.score = score


class _FakeRun:
    def __init__(self):
        self.id = uuid.uuid4()
        self.evaluation_id = uuid.uuid4()
        self.status = "running"
        self.error = None
        self.rescore_error = None
        self.scorer_selection = None


def _apply_finalize(row, stmt):
    """Apply a finalize ``UPDATE ... WHERE id = ... AND status = 'running'`` to *row*."""
    params = stmt.compile().params
    if row.status != params["status_1"]:
        # WHERE excluded this row: zero rows matched.
        return SimpleNamespace(rowcount=0)
    row.status = params["status"]
    row.rescore_error = params["rescore_error"]
    if "error" in params:
        row.error = params["error"]
    return SimpleNamespace(rowcount=1)


class _FakeSession:
    """Models commit/rollback over the ``run``/``result`` mutations a real
    ``rescore_run`` pass makes, so a rollback is asserted by its effect on the
    data."""

    def __init__(self, run, result):
        self._run = run
        self._result = result
        self._committed_result_score = result.score
        self._committed_scorer_selection = run.scorer_selection
        self.rollback_calls = 0
        self.commit_calls = 0
        self.execute_calls = 0
        self.rollback_before_first_commit = None

    async def get(self, model, _id):
        return self._run

    async def execute(self, stmt):
        """Model the finalize step's atomic conditional UPDATE.

        ``_rescore_run_with_own_session`` finalizes via
        ``update(EvaluationRun).where(id == ..., status == "running")``; the row
        is changed only while the rescore still owns it, as the real UPDATE
        behaves against a concurrently committed cancellation.
        """
        self.execute_calls += 1
        return _apply_finalize(self._run, stmt)

    async def rollback(self):
        self.rollback_calls += 1
        # Discard whatever the mocked `rescore_run` mutated before raising;
        # without the production rollback these survive to `commit()`.
        self._result.score = self._committed_result_score
        self._run.scorer_selection = self._committed_scorer_selection

    async def commit(self):
        if self.rollback_before_first_commit is None:
            self.rollback_before_first_commit = self.rollback_calls > 0
        self.commit_calls += 1
        self._committed_result_score = self._result.score
        self._committed_scorer_selection = self._run.scorer_selection

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected_error"),
    [
        pytest.param(RuntimeError("judge provider exploded mid-batch"), "judge provider exploded", id="unexpected_error"),
        # A ValueError carries the code the run reports as it is.
        pytest.param(ValueError("no_predictions"), "no_predictions", id="value_error_code"),
    ],
)
async def test_a_rescore_that_raises_rolls_back_before_handing_the_run_back(
    monkeypatch, error, expected_error
):
    """The partial work is rolled back before the run gets back its previous status,
    the failure kept in ``rescore_error``."""
    run = _FakeRun()
    result = _FakeResult(score=10.0)
    session = _FakeSession(run, result)

    async def _rescore_run_partial_then_raise(_session, _run_id, **_kwargs):
        # As a real `rescore_run` pass: the scorer_selection stamp, then a
        # result mutation, then the failure, all uncommitted.
        run.scorer_selection = ["judge"]
        result.score = 99.0
        raise error

    # ``_rescore_run_with_own_session`` imports ``async_session`` lazily, so
    # the source module's attribute is the one to patch.
    monkeypatch.setattr("app.core.database.async_session", lambda: session)
    monkeypatch.setattr(
        evaluation_engine, "rescore_run", _rescore_run_partial_then_raise
    )

    await evaluation_engine._rescore_run_with_own_session(
        "11111111-1111-1111-1111-111111111111", previous_status="completed"
    )

    assert session.rollback_before_first_commit is True, (
        "a mid-batch rescore exception must roll back the session before "
        "committing the run's terminal status, or partial mutations made "
        "before the exception get persisted anyway"
    )
    # The partial mutations never survive.
    assert result.score == 10.0, "the partial score mutation must be reverted"
    assert run.scorer_selection is None, "the partial stamp must be reverted"
    assert run.status == "completed"
    # The reason the run's banner shows, kept apart from the run's own error.
    assert expected_error in run.rescore_error
    assert run.error is None
    if isinstance(error, ValueError):
        assert run.rescore_error == expected_error


@pytest.mark.asyncio
@pytest.mark.parametrize("previous_status", ["completed", "failed", "cancelled"])
async def test_a_failed_rescore_hands_the_run_back_its_status(monkeypatch, previous_status):
    """A rescore re-judges stored answers: its failure is not the run's. The run
    keeps the status it had, its own error untouched, and the failure is recorded apart."""
    run = _FakeRun()
    run.error = "the run's own error" if previous_status != "completed" else None
    session = _FakeSession(run, _FakeResult(score=10.0))

    async def _rescore_run_raises(_session, _run_id, **_kwargs):
        raise RuntimeError("judge provider exploded")

    monkeypatch.setattr("app.core.database.async_session", lambda: session)
    monkeypatch.setattr(evaluation_engine, "rescore_run", _rescore_run_raises)

    await evaluation_engine._rescore_run_with_own_session(
        "12121212-1212-1212-1212-121212121212", previous_status=previous_status
    )

    assert run.status == previous_status
    assert run.error == ("the run's own error" if previous_status != "completed" else None)
    assert "judge provider exploded" in run.rescore_error


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "rescored",
    [
        pytest.param(1, id="the_rescore_finished"),
        # The probe saw the cancel, rolled back and reports nothing persisted.
        pytest.param(0, id="the_rescore_stopped_on_the_cancel"),
    ],
)
async def test_cancelled_run_is_never_overwritten_by_a_late_scoring_outcome(
    monkeypatch, rescored
):
    """A concurrent cancel committed while the rescore is in flight wins: the
    background job's terminal status never overwrites it, and the run
    is not reported as landed ``completed``.

    The finalize is a conditional ``UPDATE ... WHERE status = 'running'``.
    The mocked ``rescore_run`` flips the row to "cancelled" to model the
    cancel committing just before that UPDATE.
    """
    run = _FakeRun()
    result = _FakeResult(score=10.0)
    session = _FakeSession(run, result)

    async def _rescore_run_cancelled_in_flight(_session, _run_id, **_kwargs):
        run.status = "cancelled"
        return rescored

    monkeypatch.setattr("app.core.database.async_session", lambda: session)
    monkeypatch.setattr(evaluation_engine, "rescore_run", _rescore_run_cancelled_in_flight)

    await evaluation_engine._rescore_run_with_own_session(
        "33333333-3333-3333-3333-333333333333", previous_status="completed"
    )

    assert session.execute_calls == 1
    assert run.status == "cancelled", "a cancellation must never be overwritten"
    assert run.error is None, "the cancelled row's error must be untouched"


@pytest.mark.asyncio
async def test_finalize_commit_failure_is_caught_and_recovered(monkeypatch):
    """A failure in the finalize commit does not escape
    ``_rescore_run_with_own_session``, and the run is still handed back its
    previous status on a fresh session.

    ``_gather_runs_bounded`` swallows exceptions, so an escaped one would leave
    the run "running" forever with nothing logged."""
    run = _FakeRun()
    result = _FakeResult(score=10.0)
    primary_session = _FakeSession(run, result)

    async def _commit_raises():
        raise ConnectionError("connection reset by peer")

    primary_session.commit = _commit_raises

    # The row as a fresh session reads it back: the primary session's
    # ``commit()`` raised, so the DB still holds "running", not the in-memory
    # value on ``run``.
    row_as_persisted = _FakeRun()

    class _RecoverySession:
        def __init__(self, row):
            self._row = row

        async def execute(self, stmt):
            """Model the recovery pass's atomic conditional UPDATE, as
            ``_FakeSession.execute``."""
            return _apply_finalize(self._row, stmt)

        async def commit(self):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc_info):
            return False

    recovery_session = _RecoverySession(row_as_persisted)
    sessions = iter([primary_session, recovery_session])
    monkeypatch.setattr(
        "app.core.database.async_session", lambda: next(sessions)
    )

    async def _rescore_run_success(_session, _run_id, **_kwargs):
        return 1

    monkeypatch.setattr(evaluation_engine, "rescore_run", _rescore_run_success)

    # Must not raise.
    await evaluation_engine._rescore_run_with_own_session(
        "44444444-4444-4444-4444-444444444444", previous_status="completed"
    )

    assert row_as_persisted.status == "completed", (
        "the recovery pass on a fresh session must still hand the run back "
        "even though the primary session's own commit is what broke"
    )
    assert row_as_persisted.rescore_error is not None


# ── Durable idempotence: only a still-"running" run is rescored ──────────────
#
# The orchestration engine clears ``adapter_state`` on every retry path this
# adapter can take (only ``await_event`` keeps it, and a rescore never parks),
# so a checkpoint there would not survive the crash it exists for. The run's
# own status is the idempotence key instead.


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_status", ["completed", "failed", "cancelled"])
async def test_rescore_skips_a_run_that_is_no_longer_running(
    monkeypatch, terminal_status
):
    """A retried batch skips the runs that already finished, without calling
    ``rescore_run``: rescoring them would re-charge the judge and could change
    the score."""
    run = _FakeRun()
    run.status = terminal_status
    result = _FakeResult(score=10.0)
    session = _FakeSession(run, result)

    calls = []
    headlines = HeadlinesFake()

    async def _rescore_run(_session, run_id, **_kwargs):
        calls.append(run_id)
        return 1

    monkeypatch.setattr("app.core.database.async_session", lambda: session)
    monkeypatch.setattr(evaluation_engine, "rescore_run", _rescore_run)
    monkeypatch.setattr(evaluation_engine, "_compute_run_headlines", headlines)

    await evaluation_engine._rescore_run_with_own_session(
        "55555555-5555-5555-5555-555555555555", previous_status="completed"
    )
    repairs = [
        (c["evaluation_id"], c["batch_run_ids"], c["require_completed"]) for c in headlines.calls
    ]

    assert calls == [], (
        "a run that is no longer 'running' must never be rescored again — "
        "this is what makes an orchestration-engine retry of the same batch "
        "idempotent, and what stops a queued run cancelled mid-batch from "
        "still calling the judge"
    )
    assert run.status == terminal_status, "the terminal status must be left alone"
    assert session.execute_calls == 0, (
        "a skipped run must not even issue the finalize UPDATE"
    )
    if terminal_status == "completed":
        # A completed run only gets its headlines repaired, committed once.
        assert repairs == [(run.evaluation_id, [str(run.id)], False)]
        assert session.commit_calls == 1
    else:
        assert repairs == []
        assert session.commit_calls == 0


def _completed_run_whose_headline_repair_raises(monkeypatch, error):
    run = _FakeRun()
    run.status = "completed"
    session = _FakeSession(run, _FakeResult(score=10.0))
    calls = []

    async def _rescore_run(_session, run_id, **_kwargs):
        calls.append(run_id)
        return 1

    monkeypatch.setattr("app.core.database.async_session", lambda: session)
    monkeypatch.setattr(evaluation_engine, "rescore_run", _rescore_run)
    monkeypatch.setattr(evaluation_engine, "_compute_run_headlines", HeadlinesFake(raises=error))
    return run, session, calls


@pytest.mark.asyncio
async def test_a_failed_headline_repair_leaves_the_completed_run_alone(monkeypatch, caplog):
    """The repair is best effort: its failure is rolled back and logged, and
    the run stays ``completed`` with its scores, skipped as any finished run.
    Escaping, it would reach the finalize fallback, which marks the run
    ``failed``."""
    run_id = "66666666-6666-6666-6666-666666666666"
    run, session, calls = _completed_run_whose_headline_repair_raises(
        monkeypatch, RuntimeError("headline store unavailable")
    )

    with caplog.at_level(logging.WARNING, logger=evaluation_engine.logger.name):
        await evaluation_engine._rescore_run_with_own_session(
            run_id, previous_status="completed"
        )

    assert calls == []
    assert run.status == "completed"
    assert run.error is None
    assert session.execute_calls == 0, "the run must not be rewritten"
    assert session.rollback_calls == 1
    assert session.commit_calls == 0
    assert any(
        record.levelno == logging.WARNING
        and "Headline repair failed" in record.getMessage()
        and run_id in record.getMessage()
        for record in caplog.records
    ), "the failed repair must be logged with the run it concerns"


@pytest.mark.asyncio
async def test_a_cancelled_headline_repair_still_propagates(monkeypatch):
    """Cancellation is not a repair failure: it escapes, so the task stops."""
    run, session, _calls = _completed_run_whose_headline_repair_raises(
        monkeypatch, asyncio.CancelledError()
    )

    with pytest.raises(asyncio.CancelledError):
        await evaluation_engine._rescore_run_with_own_session(
            "77777777-7777-7777-7777-777777777777", previous_status="completed"
        )

    assert run.status == "completed"
    assert session.execute_calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("a_run_completed", "run_count"),
    [
        pytest.param(True, 2, id="2_runs"),
        pytest.param(True, 8, id="8_runs"),
        # No completed run in the DB: no pass, rather than forcing a stale
        # terminal run into the comparative basis.
        pytest.param(False, 2, id="nothing_completed"),
    ],
)
async def test_execute_rescore_runs_recomputes_the_performance_basis_once_and_only_when_a_run_completed(
    monkeypatch, a_run_completed, run_count
):
    """One shared Overall Performance pass per batch, not one per run.

    Every run shares the same basis (the whole batch), and each
    ``_rescore_run_with_own_session`` call is handed its own run's previous
    status. The pass is gated on the DB (``_batch_has_a_completed_run``), so
    a retry that finds every run already completed by an earlier attempt still
    runs it; ``tests/test_evaluation_aggregation.py`` covers the gate on a real
    DB. It is stubbed here because the run IDs are fake.
    """
    run_ids = [f"run-{i}" for i in range(run_count)]
    previous_statuses = {
        rid: ("completed" if i % 2 else "failed") for i, rid in enumerate(run_ids)
    }
    per_run_calls = []

    async def _rescore_one(run_id, **kwargs):
        per_run_calls.append((run_id, kwargs.get("previous_status")))

    shared_pass = AsyncMock()

    monkeypatch.setattr(
        evaluation_engine, "_rescore_run_with_own_session", _rescore_one
    )
    monkeypatch.setattr(
        evaluation_engine, "_batch_has_a_completed_run", AsyncMock(return_value=a_run_completed)
    )
    monkeypatch.setattr(
        evaluation_engine, "_recompute_shared_performance_basis", shared_pass
    )

    await evaluation_engine.execute_rescore_runs(
        run_ids, previous_statuses=previous_statuses
    )

    assert sorted(rid for rid, _status in per_run_calls) == sorted(run_ids)
    assert dict(per_run_calls) == previous_statuses, (
        "every run must be handed back its own status from before the rescore"
    )
    if a_run_completed:
        shared_pass.assert_awaited_once_with(run_ids)
    else:
        shared_pass.assert_not_awaited()


# ── ``POST /evaluations/{evaluation_id}/rescore-from-runs`` ───────────────────────────
# A strict, execute()-queue-driven ``_FakeDb`` stands in for the DB session. The
# endpoint validates synchronously (run exists, has stored predictions), stages the
# orchestration execution through ``stage_rescore_execution`` on the same session,
# atomic with the flip of each valid run to ``"running"`` in one commit. The staging
# call is monkeypatched to record its invocations.


class _ScalarsResult:
    def __init__(self, values):
        self._values = values

    def scalars(self):
        return self

    def all(self):
        return self._values


class _RowsResult:
    """Stands in for a grouped ``select(col, func.count(...))`` result.

    ``.all()`` returns raw ``(run_id, count)`` tuples, with no ``.scalars()`` unwrap.
    """

    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeDb:
    def __init__(self, execute_results, runs=None):
        self._execute_results = list(execute_results)
        self._runs_by_id = runs or {}
        self.added = []
        self.flush_calls = 0
        self.commits = 0
        self.rollbacks = 0

    async def execute(self, _statement):
        from sqlalchemy.sql.dml import Update

        if isinstance(_statement, Update):
            # The conditional-UPDATE recovery path: applied to the
            # registered runs as a real conditional UPDATE would, instead of
            # consuming from the SELECT-result queue below.
            params = _statement.compile().params
            target_ids = params["id_1"]
            if not isinstance(target_ids, list):
                target_ids = [target_ids]
            excluded_status = params.get("status_1")
            excluded_statuses = (
                excluded_status
                if isinstance(excluded_status, (list, tuple, set))
                else {excluded_status}
            )
            affected = 0
            for run_id in target_ids:
                run = self._runs_by_id.get(run_id)
                if run is None or run.status in excluded_statuses:
                    continue
                run.status = params["status"]
                run.error = params["error"]
                affected += 1
            return SimpleNamespace(rowcount=affected)

        if not self._execute_results:
            raise AssertionError("Unexpected extra execute() call")
        return self._execute_results.pop(0)

    def add(self, obj):
        if getattr(obj, "id", None) is None:
            obj.id = uuid.uuid4()
        self.added.append(obj)

    async def flush(self):
        self.flush_calls += 1

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


@pytest.fixture
def fake_user():
    return SimpleNamespace(id=uuid.uuid4(), is_admin=True, full_name="Admin")


@pytest.fixture
def evaluations_client(fake_user):
    app = FastAPI()
    app.include_router(evaluations_api.router, prefix="/api/v1/evaluations")

    app.dependency_overrides[deps.get_current_user] = lambda: fake_user
    app.dependency_overrides[deps.require_ai_enabled] = lambda: None

    def _mk_client_with_db(fake_db):
        async def _get_db():
            yield fake_db

        app.dependency_overrides[get_db] = _get_db
        return TestClient(app)

    return _mk_client_with_db


def _make_evaluation(evaluation_id, *, scorer_selection=None):
    return SimpleNamespace(
        id=evaluation_id,
        name="Eval",
        is_company_managed=False,
        # Rescore requires a runnable output family, like every trigger.
        family="STIX_BUNDLE",
        scorer_selection=scorer_selection,
        timeout_seconds=3600,
    )


def _make_run(run_id, evaluation_id):
    return SimpleNamespace(
        id=run_id,
        evaluation_id=evaluation_id,
        status="completed",
        error=None,
        # Outside any placebo comparison, which the rescore routes refuse.
        placebo_comparison_id=None,
    )


def _patch_common(monkeypatch, evaluation):
    async def _groups(_db, _uid):
        return []

    async def _get_eval(_db, _uid, _eid, _groups, *, is_admin=False):
        return evaluation

    async def _audit(*_args, **_kwargs):
        return None

    monkeypatch.setattr(evaluations_api, "get_user_group_ids", _groups)
    monkeypatch.setattr(evaluations_api, "_get_user_evaluation", _get_eval)
    monkeypatch.setattr(evaluations_api, "log_audit", _audit)


def _patch_stage_rescore_execution(monkeypatch):
    """Stub ``stage_rescore_execution``, which the endpoints call on their own
    session, recording every invocation."""
    calls = []

    async def _stage_rescore_execution(
        db, evaluation_id, run_ids, *, previous_statuses
    ):
        calls.append(
            {
                "evaluation_id": evaluation_id,
                "run_ids": run_ids,
                "previous_statuses": previous_statuses,
            }
        )
        # Must carry an ``id``: the endpoint stamps it onto every run in the
        # batch as ``owner_execution_id`` in the same transaction.
        return SimpleNamespace(id=uuid.uuid4())

    monkeypatch.setattr(
        "app.services.evaluation_engine.stage_rescore_execution",
        _stage_rescore_execution,
    )
    return calls


@pytest.mark.asyncio
async def test_rescore_from_runs_success_for_multiple_runs(
    monkeypatch, evaluations_client
):
    """Runs with stored predictions are each flipped to ``status: "running"`` and
    scheduled as one background batch."""
    evaluation_id = uuid.uuid4()
    run_id_a = uuid.uuid4()
    run_id_b = uuid.uuid4()
    evaluation = _make_evaluation(evaluation_id)
    run_a = _make_run(run_id_a, evaluation_id)
    run_b = _make_run(run_id_b, evaluation_id)

    fake_db = _FakeDb(
        execute_results=[
            _ScalarsResult([]),  # dataset family gate: none missing
            _ScalarsResult([run_a, run_b]),  # runs-in-evaluation lookup
            _RowsResult([(run_id_a, 4), (run_id_b, 7)]),  # predictions count
        ]
    )
    _patch_common(monkeypatch, evaluation)
    enqueue_calls = _patch_stage_rescore_execution(monkeypatch)

    with evaluations_client(fake_db) as client:
        resp = client.post(
            f"/api/v1/evaluations/{evaluation_id}/rescore-from-runs",
            json={"run_ids": [str(run_id_a), str(run_id_b)]},
        )

    assert resp.status_code == 200
    results = resp.json()["results"]
    assert len(results) == 2
    by_run = {r["run_id"]: r for r in results}
    assert by_run[str(run_id_a)]["status"] == "running"
    assert by_run[str(run_id_a)].get("rescored") is None
    assert by_run[str(run_id_b)]["status"] == "running"
    assert run_a.status == "running"
    assert run_b.status == "running"
    assert fake_db.commits == 1
    assert len(enqueue_calls) == 1
    assert set(enqueue_calls[0]["run_ids"]) == {str(run_id_a), str(run_id_b)}


@pytest.mark.asyncio
async def test_rescore_from_runs_partial_failure_no_predictions(
    monkeypatch, evaluations_client
):
    """A run with no stored predictions fails synchronously as ``no_predictions``
    while the rest of the batch is still scheduled."""
    evaluation_id = uuid.uuid4()
    run_id_ok = uuid.uuid4()
    run_id_empty = uuid.uuid4()
    evaluation = _make_evaluation(evaluation_id)
    run_ok = _make_run(run_id_ok, evaluation_id)
    run_empty = _make_run(run_id_empty, evaluation_id)

    fake_db = _FakeDb(
        execute_results=[
            _ScalarsResult([]),
            _ScalarsResult([run_ok, run_empty]),
            # run_id_empty has no rows at all in the grouped count.
            _RowsResult([(run_id_ok, 3)]),
        ]
    )
    _patch_common(monkeypatch, evaluation)
    enqueue_calls = _patch_stage_rescore_execution(monkeypatch)

    with evaluations_client(fake_db) as client:
        resp = client.post(
            f"/api/v1/evaluations/{evaluation_id}/rescore-from-runs",
            json={"run_ids": [str(run_id_ok), str(run_id_empty)]},
        )

    assert resp.status_code == 200, (
        f"a per-run failure must not abort the batch with a non-200; got "
        f"{resp.status_code}: {resp.json()!r}"
    )
    results = resp.json()["results"]
    by_run = {r["run_id"]: r for r in results}
    assert by_run[str(run_id_ok)]["status"] == "running"
    assert by_run[str(run_id_empty)]["status"] == "failed"
    assert by_run[str(run_id_empty)]["error"] == "no_predictions"
    assert run_empty.status == "completed"  # untouched, never flipped
    assert len(enqueue_calls) == 1
    assert enqueue_calls[0]["run_ids"] == [str(run_id_ok)]


@pytest.mark.asyncio
@pytest.mark.parametrize("in_progress_status", ["pending", "running"])
async def test_rescore_from_runs_skips_run_already_in_progress(
    monkeypatch, evaluations_client, in_progress_status
):
    """A run already ``pending``/``running`` fails as ``run_in_progress`` and is
    never re-queued, while the rest of the batch proceeds."""
    evaluation_id = uuid.uuid4()
    run_id_ok = uuid.uuid4()
    run_id_busy = uuid.uuid4()
    evaluation = _make_evaluation(evaluation_id)
    run_ok = _make_run(run_id_ok, evaluation_id)
    run_busy = _make_run(run_id_busy, evaluation_id)
    run_busy.status = in_progress_status

    fake_db = _FakeDb(
        execute_results=[
            _ScalarsResult([]),
            _ScalarsResult([run_ok, run_busy]),
            _RowsResult([(run_id_ok, 3), (run_id_busy, 5)]),
        ]
    )
    _patch_common(monkeypatch, evaluation)
    enqueue_calls = _patch_stage_rescore_execution(monkeypatch)

    with evaluations_client(fake_db) as client:
        resp = client.post(
            f"/api/v1/evaluations/{evaluation_id}/rescore-from-runs",
            json={"run_ids": [str(run_id_ok), str(run_id_busy)]},
        )

    assert resp.status_code == 200
    results = resp.json()["results"]
    by_run = {r["run_id"]: r for r in results}
    assert by_run[str(run_id_ok)]["status"] == "running"
    assert by_run[str(run_id_busy)]["status"] == "failed"
    assert by_run[str(run_id_busy)]["error"] == "run_in_progress"
    assert run_busy.status == in_progress_status  # untouched, never re-flipped
    assert len(enqueue_calls) == 1
    assert enqueue_calls[0]["run_ids"] == [str(run_id_ok)]


@pytest.mark.asyncio
async def test_rescore_from_runs_reports_unknown_run_id_per_entry(
    monkeypatch, evaluations_client
):
    """An unknown or foreign run ID fails per entry as ``run_not_found``, not as a
    batch-wide 404 (unlike ``trigger_evaluation_from_runs``, on purpose)."""
    evaluation_id = uuid.uuid4()
    run_id_known = uuid.uuid4()
    run_id_unknown = uuid.uuid4()
    evaluation = _make_evaluation(evaluation_id)
    run_known = _make_run(run_id_known, evaluation_id)

    fake_db = _FakeDb(
        execute_results=[
            _ScalarsResult([]),
            # Only the known run resolves for this evaluation.
            _ScalarsResult([run_known]),
            _RowsResult([(run_id_known, 2)]),
        ]
    )
    _patch_common(monkeypatch, evaluation)
    enqueue_calls = _patch_stage_rescore_execution(monkeypatch)

    with evaluations_client(fake_db) as client:
        resp = client.post(
            f"/api/v1/evaluations/{evaluation_id}/rescore-from-runs",
            json={"run_ids": [str(run_id_known), str(run_id_unknown)]},
        )

    assert resp.status_code == 200
    results = resp.json()["results"]
    by_run = {r["run_id"]: r for r in results}
    assert by_run[str(run_id_known)]["status"] == "running"
    assert by_run[str(run_id_unknown)]["status"] == "failed"
    assert by_run[str(run_id_unknown)]["error"] == "run_not_found"
    assert enqueue_calls[0]["run_ids"] == [str(run_id_known)]


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["single", "bulk"])
async def test_a_rescore_refuses_the_retired_scorer_selection(monkeypatch, evaluations_client, endpoint):
    """A caller still sending ``scorer_selection`` is told so (422), not rescored with the
    evaluation's selection instead of theirs."""
    evaluation_id = uuid.uuid4()
    run_id = uuid.uuid4()
    _patch_common(monkeypatch, _make_evaluation(evaluation_id))
    enqueue_calls = _patch_stage_rescore_execution(monkeypatch)
    url, body = {
        "single": (f"/api/v1/evaluations/runs/{run_id}/rescore", {}),
        "bulk": (f"/api/v1/evaluations/{evaluation_id}/rescore-from-runs", {"run_ids": [str(run_id)]}),
    }[endpoint]

    with evaluations_client(_FakeDb(execute_results=[])) as client:
        resp = client.post(url, json={**body, "scorer_selection": ["judge"]})

    assert resp.status_code == 422
    assert "scorer_selection" in resp.text
    assert enqueue_calls == []


@pytest.mark.asyncio
async def test_rescore_from_runs_requires_admin_and_ai_gate(evaluations_client):
    """The endpoint is gated by ``require_admin`` and ``require_ai_enabled``, as the
    single-run rescore endpoint is."""
    evaluation_id = uuid.uuid4()
    run_id = uuid.uuid4()

    app = FastAPI()
    app.include_router(evaluations_api.router, prefix="/api/v1/evaluations")

    non_admin_user = SimpleNamespace(id=uuid.uuid4(), is_admin=False, full_name="User")
    app.dependency_overrides[deps.get_current_user] = lambda: non_admin_user
    app.dependency_overrides[deps.require_ai_enabled] = lambda: None

    fake_db = _FakeDb(execute_results=[])

    async def _get_db():
        yield fake_db

    app.dependency_overrides[get_db] = _get_db

    with TestClient(app) as client:
        resp = client.post(
            f"/api/v1/evaluations/{evaluation_id}/rescore-from-runs",
            json={"run_ids": [str(run_id)]},
        )

    assert resp.status_code == 403

    # An admin is still blocked by the AI gate alone, so dropping
    # ``require_ai_enabled`` from the endpoint fails this test.
    admin_user = SimpleNamespace(id=uuid.uuid4(), is_admin=True, full_name="Admin")
    app.dependency_overrides[deps.get_current_user] = lambda: admin_user

    def _raise_ai_disabled():
        raise HTTPException(status_code=403, detail="AI is disabled on this deployment.")

    app.dependency_overrides[deps.require_ai_enabled] = _raise_ai_disabled

    with TestClient(app) as client:
        resp = client.post(
            f"/api/v1/evaluations/{evaluation_id}/rescore-from-runs",
            json={"run_ids": [str(run_id)]},
        )

    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_rescore_from_runs_dedupes_duplicate_run_ids(
    monkeypatch, evaluations_client
):
    """A run ID listed twice, even differing only in case, yields one result entry
    and one scheduled run ID."""
    evaluation_id = uuid.uuid4()
    run_id = uuid.uuid4()
    evaluation = _make_evaluation(evaluation_id)
    run = _make_run(run_id, evaluation_id)

    fake_db = _FakeDb(
        execute_results=[
            _ScalarsResult([]),
            _ScalarsResult([run]),
            _RowsResult([(run_id, 1)]),
        ]
    )
    _patch_common(monkeypatch, evaluation)
    enqueue_calls = _patch_stage_rescore_execution(monkeypatch)

    with evaluations_client(fake_db) as client:
        resp = client.post(
            f"/api/v1/evaluations/{evaluation_id}/rescore-from-runs",
            json={"run_ids": [str(run_id), str(run_id).upper()]},
        )

    assert resp.status_code == 200
    results = resp.json()["results"]
    assert len(results) == 1, (
        f"a case-only duplicate run ID must dedup to a single result; got "
        f"{len(results)} entries: {results!r}"
    )
    assert enqueue_calls[0]["run_ids"] == [str(run_id)]


class _ScalarResult:
    """Single-value result for ``scalar_one()`` / ``scalar_one_or_none()``."""

    def __init__(self, value):
        self._value = value

    def scalar_one(self):
        return self._value

    def scalar_one_or_none(self):
        return self._value


def _patch_stage_rescore_execution_failure(monkeypatch):
    """Make the orchestration execution staging call blow up, like a DB blip
    creating the execution row would."""

    async def _stage_rescore_execution(*_args, **_kwargs):
        raise RuntimeError("orchestration execution could not be created")

    monkeypatch.setattr(
        "app.services.evaluation_engine.stage_rescore_execution",
        _stage_rescore_execution,
    )


@pytest.mark.asyncio
async def test_rescore_from_runs_staging_failure_leaves_every_run_untouched(
    monkeypatch, evaluations_client
):
    """A staging failure leaves every run as it was and returns a 500: staging
    happens before the ``"running"`` flip, in one transaction."""
    evaluation_id = uuid.uuid4()
    run_id_a = uuid.uuid4()
    run_id_b = uuid.uuid4()
    evaluation = _make_evaluation(evaluation_id)
    run_a = _make_run(run_id_a, evaluation_id)
    run_b = _make_run(run_id_b, evaluation_id)

    fake_db = _FakeDb(
        execute_results=[
            _ScalarsResult([]),  # dataset family gate
            _ScalarsResult([run_a, run_b]),  # runs-in-evaluation lookup
            _RowsResult([(run_id_a, 4), (run_id_b, 7)]),  # predictions count
        ],
        runs={run_id_a: run_a, run_id_b: run_b},
    )
    _patch_common(monkeypatch, evaluation)
    _patch_stage_rescore_execution_failure(monkeypatch)

    with evaluations_client(fake_db) as client:
        resp = client.post(
            f"/api/v1/evaluations/{evaluation_id}/rescore-from-runs",
            json={"run_ids": [str(run_id_a), str(run_id_b)]},
        )

    assert resp.status_code == 500
    # A literal, not a value captured before the call, which would also pass if
    # the staging call were removed: the point is that staging precedes the flip.
    assert run_a.status == "completed"
    assert run_b.status == "completed"
    assert run_a.error is None
    assert run_b.error is None
    assert fake_db.commits == 0, (
        "staging failure must be caught before any commit — there is no "
        "'running' flip to revert any more"
    )
    assert fake_db.rollbacks == 1, (
        "the SELECT ... FOR UPDATE lock must be released via an explicit "
        "rollback rather than left held until get_db() teardown"
    )


@pytest.mark.asyncio
async def test_single_run_rescore_staging_failure_leaves_the_run_untouched(
    monkeypatch, evaluations_client
):
    """Same recovery on the single-run endpoint, which shares the contract."""
    evaluation_id = uuid.uuid4()
    run_id = uuid.uuid4()
    evaluation = _make_evaluation(evaluation_id)
    run = _make_run(run_id, evaluation_id)

    fake_db = _FakeDb(
        execute_results=[
            _ScalarResult(run),  # locked run lookup
            _ScalarsResult([]),  # dataset family gate
            _ScalarResult(3),  # predictions count
        ],
        runs={run_id: run},
    )
    _patch_common(monkeypatch, evaluation)
    _patch_stage_rescore_execution_failure(monkeypatch)

    with evaluations_client(fake_db) as client:
        resp = client.post(f"/api/v1/evaluations/runs/{run_id}/rescore", json={})

    assert resp.status_code == 500
    assert run.status == "completed"
    assert run.error is None
    assert fake_db.commits == 0
    assert fake_db.rollbacks == 1


# ── A rescore keeps a legacy run's token F1 ──────────────────────────────────


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_rescore_keeps_the_token_f1_a_legacy_run_still_shows(monkeypatch):
    """A run scored on main holds a token F1 at result and run level; a rescore leaves
    both as they were, never recomputing the run's from its entries, and gives the run
    its Overall Quality."""
    from sqlalchemy import select

    from app.models.evaluation import EvaluationResult, EvaluationRun
    from tests.evaluation_helpers import rescore_as_started, run_on_postgres

    async def _score_on_main_then_rescore(session, run_id):
        run = await session.get(EvaluationRun, run_id)
        # Not what today's penalised mean of the entries gives: it must stay as stored.
        run.f1_score, run.quality_score = 55.0, None
        rows = (await session.execute(select(EvaluationResult).where(EvaluationResult.run_id == run_id))).scalars()
        for result, f1 in zip(sorted(rows, key=lambda r: r.agent_output), (80.0, 60.0, 40.0), strict=True):
            result.f1_score = f1
        await session.commit()
        await rescore_as_started(session, run_id)

    run, results = await run_on_postgres(
        monkeypatch,
        scorer_selection=None,
        answers=["Paris a", "Paris b", "Paris c"],
        expected_answers=["Paris"] * 3,
        family="PROSE",
        then=_score_on_main_then_rescore,
    )

    assert [r.f1_score for r in results] == [80.0, 60.0, 40.0]
    assert run.f1_score == 55.0
    assert run.quality_score is not None


# ── A rescore builds its quality config from every entry of the run ──────────


async def _rescore_a_cancelled_classification_run(monkeypatch, *, pinned, live_edit=None):
    """A CLASSIFICATION run whose dataset declares no vocabulary, on entries answered
    `phishing`, `malware`, `spam`, cancelled with a result for its first entry only, which
    predicts `spam`; then rescored. ``live_edit`` sets the live expected answer of the third
    entry, which has no result, before the rescore. Returns the first entry's metrics on the
    fresh pass and after the rescore."""
    from sqlalchemy import delete, select

    from app.models.dataset import DatasetEntry
    from app.models.evaluation import EvaluationResult, EvaluationRun
    from tests.evaluation_helpers import rescore_as_started, run_on_postgres

    fresh: dict = {}

    async def _cancel_after_the_first_entry_then_rescore(session, run_id):
        rows = (
            await session.execute(
                select(EvaluationResult, DatasetEntry)
                .join(DatasetEntry, DatasetEntry.id == EvaluationResult.entry_id)
                .where(EvaluationResult.run_id == run_id)
            )
        ).all()
        first = next(result for result, entry in rows if entry.position == 0)
        fresh.update(first.execution_details["metric_results"])
        await session.execute(
            delete(EvaluationResult).where(EvaluationResult.run_id == run_id, EvaluationResult.id != first.id)
        )
        if live_edit is not None:
            next(entry for _result, entry in rows if entry.position == 2).expected_answer = live_edit
        run = await session.get(EvaluationRun, run_id)
        run.status = "cancelled"
        await session.commit()
        await rescore_as_started(session, run_id)

    run, results = await run_on_postgres(
        monkeypatch,
        scorer_selection=[],
        answers=["spam", "malware", "spam"],
        family="CLASSIFICATION",
        expected_answers=["phishing", "malware", "spam"],
        pinned=pinned,
        then=_cancel_after_the_first_entry_then_rescore,
    )

    assert run.status == "cancelled"
    assert len(results) == 1
    assert fresh["classification.vocabulary_validity"]["value"] == 100.0
    return fresh, results[0].execution_details["metric_results"]


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("pinned", [True, False], ids=["pinned", "unpinned"])
async def test_a_rescore_of_a_cancelled_run_keeps_the_vocabulary_of_every_entry(monkeypatch, pinned):
    """The labels come from the references of every entry the run scores, its snapshot or,
    for a run without one, the live entries: a rescore keeps `spam`, which only an entry
    without a result expects, and scores the answer as the fresh pass did."""
    fresh, rescored = await _rescore_a_cancelled_classification_run(monkeypatch, pinned=pinned)

    assert rescored["classification.vocabulary_validity"] == fresh["classification.vocabulary_validity"]
    assert rescored["classification.micro_prf"] == fresh["classification.micro_prf"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_rescore_of_a_run_without_a_snapshot_reads_the_live_entries(monkeypatch):
    """A run without a snapshot (a legacy or imported run) is rescored against the live
    entries, as a resumed callback is: once the only entry expecting `spam` expects
    `ransomware`, the rescore no longer knows the label."""
    _fresh, rescored = await _rescore_a_cancelled_classification_run(
        monkeypatch, pinned=False, live_edit="ransomware"
    )

    validity = rescored["classification.vocabulary_validity"]
    assert (validity["value"], validity["detail"]["offending_labels"]) == (0.0, ["spam"])
