"""Regression tests for bounded evaluation-run concurrency.

Firing the entire agent × model matrix at the LLM provider at once overwhelmed
provider rate limits; the 429/529 retries then backed off inside each call,
inflating per-run durations into thousands of seconds. ``_gather_runs_bounded``
caps the number of runs in flight so the provider stays busy without thrashing.
"""

import asyncio

import pytest

from app.services.evaluation_engine import _gather_runs_bounded


class _ConcurrencyTracker:
    """Records the peak number of concurrently in-flight runners."""

    def __init__(self, hold: float = 0.02) -> None:
        self.active = 0
        self.peak = 0
        self.completed: list[str] = []
        self._hold = hold

    async def run(self, run_id: str) -> None:
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(self._hold)
            self.completed.append(run_id)
        finally:
            self.active -= 1


@pytest.mark.asyncio
async def test_bounded_caps_in_flight_runs():
    tracker = _ConcurrencyTracker()
    run_ids = [f"run-{i}" for i in range(12)]

    await _gather_runs_bounded(run_ids, max_concurrent=3, runner=tracker.run)

    assert tracker.peak <= 3
    assert sorted(tracker.completed) == sorted(run_ids)


@pytest.mark.asyncio
async def test_small_batch_runs_all_at_once():
    tracker = _ConcurrencyTracker()
    run_ids = [f"run-{i}" for i in range(4)]

    # Batch (4) is under the cap (5) → everything runs concurrently.
    await _gather_runs_bounded(run_ids, max_concurrent=5, runner=tracker.run)

    assert tracker.peak == 4
    assert sorted(tracker.completed) == sorted(run_ids)


@pytest.mark.asyncio
async def test_non_positive_cap_disables_bound():
    tracker = _ConcurrencyTracker()
    run_ids = [f"run-{i}" for i in range(8)]

    # 0 (or negative) restores the legacy unbounded behaviour.
    await _gather_runs_bounded(run_ids, max_concurrent=0, runner=tracker.run)

    assert tracker.peak == 8
    assert sorted(tracker.completed) == sorted(run_ids)


@pytest.mark.asyncio
async def test_one_failing_run_does_not_cancel_the_batch():
    tracker = _ConcurrencyTracker()
    run_ids = [f"run-{i}" for i in range(6)]

    async def runner(rid: str) -> None:
        if rid == "run-2":
            raise RuntimeError("boom")
        await tracker.run(rid)

    # return_exceptions=True: the failing run is swallowed, the rest finish.
    await _gather_runs_bounded(run_ids, max_concurrent=2, runner=runner)

    assert "run-2" not in tracker.completed
    assert sorted(tracker.completed) == sorted(r for r in run_ids if r != "run-2")


# ---------------------------------------------------------------------------
# Headline recompute serialisation
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_batch_completions_converge_on_one_basis():
    """Two batches finishing at once must agree on the run set they scored against.

    Overall Performance is comparative, so each concurrent recompute reads the
    evaluation's whole run set. Without the advisory lock the two interleave, and
    the last writer leaves some runs holding values normalised against a basis
    that is no longer in effect.
    """
    from app.models.agent import Agent
    from app.models.evaluation import Evaluation, EvaluationRun
    from app.services.evaluation_engine import _compute_run_headlines
    from tests.test_evaluation_aggregation import _make_db, _seed_perf_run

    sessionmaker, cleanup = await _make_db()
    try:
        async with sessionmaker() as session:
            agent = Agent(name="Agent")
            evaluation = Evaluation(name="Eval", scorer_selection=[])
            session.add_all([agent, evaluation])
            await session.flush()
            await _seed_perf_run(session, evaluation, agent, [1.0, 1.0])
            await _seed_perf_run(session, evaluation, agent, [5.0, 5.0])
            await _seed_perf_run(session, evaluation, agent, [10.0, 10.0])
            await session.commit()
            evaluation_id = evaluation.id

        async def recompute():
            async with sessionmaker() as session:
                await _compute_run_headlines(session, evaluation_id)

        await asyncio.gather(*(recompute() for _ in range(4)))

        async with sessionmaker() as session:
            concurrent = {
                r.model_id: r.performance_score
                for r in (
                    await session.execute(
                        EvaluationRun.__table__.select().where(
                            EvaluationRun.evaluation_id == evaluation_id
                        )
                    )
                ).all()
            }

        # Sequential recompute over the same data is the reference answer.
        async with sessionmaker() as session:
            await _compute_run_headlines(session, evaluation_id)
            await session.commit()
        async with sessionmaker() as session:
            sequential = {
                r.model_id: r.performance_score
                for r in (
                    await session.execute(
                        EvaluationRun.__table__.select().where(
                            EvaluationRun.evaluation_id == evaluation_id
                        )
                    )
                ).all()
            }

        assert concurrent == sequential
        assert sorted(v for v in sequential.values() if v is not None) == sorted(
            [100.0, *(v for v in sequential.values() if v != 100.0)]
        )
    finally:
        await cleanup()


# ---------------------------------------------------------------------------
# Stranded-headline recovery
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_recovery_recomputes_every_stranded_run_in_one_pass(monkeypatch):
    """One recovery pass must catch every stranded run, not just the first.

    Seeds two stranded ``completed`` runs (``headline_details=None``, each
    backed by real results) plus a healthy sibling whose headlines are already
    set. The reference ("expected") headlines are computed *before* recovery
    runs and the two runs are then re-stranded, so the later assertions can
    only pass if ``_recover_stranded_headlines`` itself produced the result —
    the expected-value computation must never write to the same rows the
    assertions read back.
    """
    from app.models.agent import Agent
    from app.models.evaluation import Evaluation, EvaluationRun
    from app.services.evaluation_engine import _compute_run_headlines
    from app.services.orchestration_adapters.evaluation import (
        _recover_stranded_headlines,
    )
    from tests.test_evaluation_aggregation import _make_db, _seed_perf_run

    sessionmaker, cleanup = await _make_db()
    try:
        async with sessionmaker() as session:
            agent = Agent(name="Agent")
            evaluation = Evaluation(name="Eval", scorer_selection=[])
            session.add_all([agent, evaluation])
            await session.flush()
            healthy = await _seed_perf_run(session, evaluation, agent, [1.0, 1.0])
            stranded_a = await _seed_perf_run(session, evaluation, agent, [5.0, 5.0])
            stranded_b = await _seed_perf_run(session, evaluation, agent, [10.0, 10.0])
            await session.commit()
            evaluation_id = evaluation.id
            healthy_id, stranded_a_id, stranded_b_id = (
                healthy.id,
                stranded_a.id,
                stranded_b.id,
            )

        # Give the sibling its headlines up front, as a normal completion
        # would — this is the run recovery must leave alone.
        async with sessionmaker() as session:
            await _compute_run_headlines(
                session, evaluation_id, batch_run_ids=[str(healthy_id)]
            )
            await session.commit()
            healthy_before = (await session.get(EvaluationRun, healthy_id)).headline_details

        # Reference answer, computed *before* recovery so it cannot be mistaken
        # for recovery's own effect. The runs are then re-stranded
        # (headline_details back to NULL) so `_recover_stranded_headlines` below
        # is the only thing that can make the later assertions pass.
        async with sessionmaker() as session:
            await _compute_run_headlines(
                session,
                evaluation_id,
                batch_run_ids=[str(stranded_a_id), str(stranded_b_id)],
            )
            await session.commit()
            expected_a = (await session.get(EvaluationRun, stranded_a_id)).headline_details
            expected_b = (await session.get(EvaluationRun, stranded_b_id)).headline_details

        async with sessionmaker() as session:
            for run_id in (stranded_a_id, stranded_b_id):
                run = await session.get(EvaluationRun, run_id)
                run.headline_details = None
            await session.commit()

        monkeypatch.setattr("app.core.database.async_session", sessionmaker)
        await _recover_stranded_headlines(str(evaluation_id))

        async with sessionmaker() as session:
            recovered_a = await session.get(EvaluationRun, stranded_a_id)
            recovered_b = await session.get(EvaluationRun, stranded_b_id)
            healthy_after = await session.get(EvaluationRun, healthy_id)

            assert recovered_a.headline_details is not None
            assert recovered_b.headline_details is not None
            assert recovered_a.headline_details == expected_a
            assert recovered_b.headline_details == expected_b
            assert healthy_after.headline_details == healthy_before
    finally:
        await cleanup()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_recovery_catches_a_partially_stranded_run(monkeypatch):
    """A run stranded before its own headline write must be caught even once a
    sibling's completion has already given it a non-NULL ``headline_details``.

    ``_recompute_performance_headlines`` (called unconditionally at the end of
    ``_compute_run_headlines``) rescores **every** completed run in the
    evaluation, not just the batch it was invoked for. So once *any* sibling
    run completes normally, a run interrupted before its own per-run write gets
    ``headline_details["performance_*"]`` from that sibling's call, though its
    judge/quality half (``quality_metric_keys``) was never computed. Selecting
    on ``headline_details IS NULL`` alone would miss it forever, so recovery
    also selects on a missing ``quality_metric_keys`` marker.
    """
    from app.models.agent import Agent
    from app.models.evaluation import Evaluation, EvaluationRun
    from app.services.evaluation_engine import _compute_run_headlines
    from app.services.orchestration_adapters.evaluation import (
        _recover_stranded_headlines,
    )
    from tests.test_evaluation_aggregation import _make_db, _seed_perf_run

    sessionmaker, cleanup = await _make_db()
    try:
        async with sessionmaker() as session:
            agent = Agent(name="Agent")
            evaluation = Evaluation(name="Eval", scorer_selection=[])
            session.add_all([agent, evaluation])
            await session.flush()
            # `stranded`: interrupted before its own per-run write —
            # `headline_details` starts out NULL.
            stranded = await _seed_perf_run(session, evaluation, agent, [5.0, 5.0])
            # `sibling`: completes normally afterwards.
            sibling = await _seed_perf_run(session, evaluation, agent, [1.0, 1.0])
            await session.commit()
            evaluation_id = evaluation.id
            stranded_id, sibling_id = stranded.id, sibling.id

        # Sibling's own per-run write. Its `_recompute_performance_headlines`
        # half rescores every completed run in the evaluation, including
        # `stranded` — which is exactly what leaves `stranded` with a
        # non-NULL `headline_details` missing `quality_metric_keys`.
        async with sessionmaker() as session:
            await _compute_run_headlines(
                session, evaluation_id, batch_run_ids=[str(sibling_id)]
            )
            await session.commit()
            partially_stranded = await session.get(EvaluationRun, stranded_id)
            assert partially_stranded.headline_details is not None
            assert "quality_metric_keys" not in partially_stranded.headline_details

        monkeypatch.setattr("app.core.database.async_session", sessionmaker)
        await _recover_stranded_headlines(str(evaluation_id))

        async with sessionmaker() as session:
            recovered = await session.get(EvaluationRun, stranded_id)
            assert recovered.headline_details is not None
            assert "quality_metric_keys" in recovered.headline_details
    finally:
        await cleanup()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_recovery_never_touches_another_evaluations_runs(monkeypatch):
    """Recovery for one evaluation must never read or modify another."""
    from app.models.agent import Agent
    from app.models.evaluation import Evaluation, EvaluationRun
    from app.services.orchestration_adapters.evaluation import (
        _recover_stranded_headlines,
    )
    from tests.test_evaluation_aggregation import _make_db, _seed_perf_run

    sessionmaker, cleanup = await _make_db()
    try:
        async with sessionmaker() as session:
            agent = Agent(name="Agent")
            eval_one = Evaluation(name="Eval One", scorer_selection=[])
            eval_two = Evaluation(name="Eval Two", scorer_selection=[])
            session.add_all([agent, eval_one, eval_two])
            await session.flush()
            run_one = await _seed_perf_run(session, eval_one, agent, [1.0, 1.0])
            run_two = await _seed_perf_run(session, eval_two, agent, [2.0, 2.0])
            await session.commit()
            eval_one_id, run_one_id, run_two_id = eval_one.id, run_one.id, run_two.id

        monkeypatch.setattr("app.core.database.async_session", sessionmaker)
        await _recover_stranded_headlines(str(eval_one_id))

        async with sessionmaker() as session:
            recovered_one = await session.get(EvaluationRun, run_one_id)
            untouched_two = await session.get(EvaluationRun, run_two_id)

            assert recovered_one.headline_details is not None
            assert untouched_two.headline_details is None
    finally:
        await cleanup()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_recovery_leaves_a_legitimately_blank_run_untouched(monkeypatch):
    """A run whose headlines are legitimately absent must not be reselected."""
    from app.models.agent import Agent
    from app.models.evaluation import Evaluation, EvaluationRun
    from app.services.evaluation_engine import _compute_run_headlines
    from app.services.orchestration_adapters.evaluation import (
        _recover_stranded_headlines,
    )
    from tests.test_evaluation_aggregation import _make_db, _seed_perf_run

    sessionmaker, cleanup = await _make_db()
    try:
        async with sessionmaker() as session:
            agent = Agent(name="Agent")
            evaluation = Evaluation(name="Eval", scorer_selection=[])
            session.add_all([agent, evaluation])
            await session.flush()
            run = await _seed_perf_run(session, evaluation, agent, [1.0])
            await session.commit()
            evaluation_id, run_id = evaluation.id, run.id

        # Normal completion already computed (possibly legitimately-empty)
        # headlines for this run.
        async with sessionmaker() as session:
            await _compute_run_headlines(session, evaluation_id, batch_run_ids=[str(run_id)])
            await session.commit()
            before = (await session.get(EvaluationRun, run_id)).headline_details

        monkeypatch.setattr("app.core.database.async_session", sessionmaker)
        await _recover_stranded_headlines(str(evaluation_id))

        async with sessionmaker() as session:
            after = await session.get(EvaluationRun, run_id)
            assert after.headline_details == before
    finally:
        await cleanup()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_recovery_is_idempotent_across_repeated_interruptions(monkeypatch):
    """A zero-results run recovered once must not be reselected on a later pass."""
    from app.models.evaluation import EvaluationRun as _Run
    from app.services.orchestration_adapters.evaluation import (
        _recover_stranded_headlines,
    )
    from tests.test_evaluation_aggregation import _make_db, _seed_performance_runs

    sessionmaker, cleanup = await _make_db()
    try:
        async with sessionmaker() as session:
            evaluation, runs = await _seed_performance_runs(session, [[]])
            await session.commit()
            evaluation_id, run_id = evaluation.id, runs[0].id

        monkeypatch.setattr("app.core.database.async_session", sessionmaker)
        await _recover_stranded_headlines(str(evaluation_id))

        async with sessionmaker() as session:
            first_pass = await session.get(_Run, run_id)
            assert first_pass.headline_details is not None
            first_details = first_pass.headline_details

        # A second interruption reacting to the same evaluation must not
        # re-select (and possibly re-clobber) an already-recovered run.
        await _recover_stranded_headlines(str(evaluation_id))

        async with sessionmaker() as session:
            second_pass = await session.get(_Run, run_id)
            assert second_pass.headline_details == first_details
    finally:
        await cleanup()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_recovery_degrades_gracefully_with_no_comparison_basis(monkeypatch):
    """A stranded run with no completed siblings still recovers quality.

    Overall Performance is comparative, so when every other run in the
    evaluation failed there is no basis to normalise against — that headline
    legitimately stays ``None`` rather than raising or blocking recovery of
    the run's other headlines.
    """
    from app.models.agent import Agent
    from app.models.evaluation import Evaluation
    from app.models.evaluation import EvaluationRun as _Run
    from app.services.orchestration_adapters.evaluation import (
        _recover_stranded_headlines,
    )
    from tests.test_evaluation_aggregation import _make_db, _seed_perf_run

    sessionmaker, cleanup = await _make_db()
    try:
        async with sessionmaker() as session:
            agent = Agent(name="Agent")
            evaluation = Evaluation(name="Eval", scorer_selection=[])
            session.add_all([agent, evaluation])
            await session.flush()
            stranded = await _seed_perf_run(session, evaluation, agent, [3.0, 3.0])
            failed_sibling = _Run(
                evaluation_id=evaluation.id,
                agent_id=agent.id,
                model_id="model-failed",
                trigger_reason="manual",
                status="failed",
                score=None,
            )
            session.add(failed_sibling)
            await session.commit()
            evaluation_id, stranded_id = evaluation.id, stranded.id

        monkeypatch.setattr("app.core.database.async_session", sessionmaker)
        await _recover_stranded_headlines(str(evaluation_id))

        async with sessionmaker() as session:
            recovered = await session.get(_Run, stranded_id)
            assert recovered.headline_details is not None
            assert recovered.performance_score is None
    finally:
        await cleanup()
