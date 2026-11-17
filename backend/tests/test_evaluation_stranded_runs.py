"""The stranded-run sweep (``sweep_stranded_runs``) against a real (throwaway) PostgreSQL database.

A run whose execution reached a terminal state without finishing it is failed once the
grace period elapses, keeping its results and headlines; live work is left alone.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import delete, func, select, text, update

import app.models  # noqa: F401 - register model metadata
from app.models.evaluation import (
    EvaluationEpisode,
    EvaluationResult,
    EvaluationRun,
)
from app.models.orchestration import OrchestrationExecution
from tests.evaluation_database import (
    _make_db,
    _run_status,
    _stranded_fixture,
    _stub_audit_and_groups,  # noqa: F401 - autouse fixture
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


# ── the stranded-run sweep ───────────────────────────────────────────
#
# Every assertion below runs against a REAL Postgres: the sweep's predicate is SQL, and
# a fake session that never executes the query cannot see it match a healthy run.


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal_state", ["completed", "failed", "dead_letter", "cancelled"])
async def test_every_terminal_execution_state_strands_its_run(terminal_state):
    """The sweep terminalises a run its execution will never continue, whatever
    terminal state that execution reached.

    The entry the worker died on left NO episode row: ``execute_evaluation_run``
    commits the run as ``"running"`` while ``start_episode`` only flushes, so a
    crash rolls the episode back. Every other repair works on an episode row.
    ``dead_letter`` matters most: it is what a crashed worker's execution
    becomes once its retries run out. The error names stranding, tells an
    operator the run can be re-run, and differs from the reclaim's wording (a
    worker died mid-attempt, not no attempt left). Which clock the sweep reads
    is pinned by ``test_staleness_is_judged_by_the_execution_not_by_the_run``.
    """
    from app.services.evaluation_continuation import (
        STRANDED_RUN_ERROR,
        sweep_stranded_runs,
    )

    sm, cleanup = await _make_db()
    try:
        run_id, _, _ = await _stranded_fixture(sm, execution_state=terminal_state)
        async with sm() as s:
            swept = await sweep_stranded_runs(s)
        status, error = await _run_status(sm, run_id)
        assert (swept, status) == (1, "failed"), (
            f"a run whose execution is {terminal_state!r} was not closed out "
            f"(swept={swept}, status={status!r}) — that execution is terminal, "
            "so nothing is going to continue this run"
        )
        assert error == STRANDED_RUN_ERROR
        assert "re-run" in (error or ""), (
            f"the error does not tell an operator it can be re-run: {error!r}"
        )
        assert "Abandoned by a worker" not in (error or ""), (
            "indistinguishable from the reclaim's error, which names a different cause"
        )
    finally:
        await cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("entries", "completed", "recorded"),
    [
        pytest.param(3, 0, 1, id="results_before_stranding"),
        pytest.param(10, 7, 7, id="partly_done"),
        pytest.param(3, 3, 3, id="every_entry_finished"),
    ],
)
async def test_a_stranded_run_fails_and_keeps_every_result_it_recorded(
    entries, completed, recorded
):
    """A stranded run is failed, never completed, and keeps every result it recorded.

    A partly done run is never presented as complete on partial data. Nor is one
    whose entries all finished: the worker died between the last entry's commit
    and the run's terminal write, so the system cannot vouch for it. The results
    survive, so a re-run does not pay for them again.
    """
    from app.services.evaluation_continuation import sweep_stranded_runs

    sm, cleanup = await _make_db()
    try:
        run_id, _, entry_ids = await _stranded_fixture(sm, entries=entries)
        async with sm() as s:
            row = await s.get(EvaluationRun, run_id)
            row.completed_entries = completed
            for i, entry_id in enumerate(entry_ids[:recorded]):
                s.add(
                    EvaluationResult(
                        run_id=run_id,
                        entry_id=entry_id,
                        agent_output=f"entry {i}",
                        score=80,
                        sub_scores={},
                        evaluator_reasoning="r",
                        duration_seconds=1,
                    )
                )
            await s.commit()

        async with sm() as s:
            assert await sweep_stranded_runs(s) == 1
        status, _ = await _run_status(sm, run_id)
        assert status == "failed", (
            f"a run stranded at {completed} of {entries} was reported {status!r}, "
            "not failed"
        )
        async with sm() as s:
            kept = (
                await s.execute(
                    select(EvaluationResult).where(EvaluationResult.run_id == run_id)
                )
            ).scalars().all()
        assert sorted(r.agent_output for r in kept) == [
            f"entry {i}" for i in range(recorded)
        ], "the sweep destroyed a result the user already paid for"
    finally:
        await cleanup()


# ── live work is never disturbed ─────────────────────────────────────────────
#
# A net that closes out live work is worse than no net: it silently destroys
# results the user is paying for.


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs,why",
    [
        (
            {"execution_state": "running"},
            "its execution is still active — duration is not evidence of anything",
        ),
        (
            {"execution_state": "pending"},
            "its execution is BETWEEN retry attempts; pending is "
            "non-terminal, so "
            "this is safe by construction rather than by timing",
        ),
        (
            {"execution_age_seconds": 5},
            "its execution went terminal moments ago, inside the grace period",
        ),
        (
            {"episodes": ("waiting_callback",)},
            "it holds live callback work, whose expiry timeout_waiting_episodes owns",
        ),
        (
            {"episodes": ("resumed",)},
            "it holds an episode a callback has just revived",
        ),
        (
            {"run_completed_at": "now"},
            "it is a FINISHED run flipped back to running by a re-score",
        ),
    ],
)
async def test_the_sweep_leaves_live_work_alone(kwargs, why):
    """The sweep leaves every shape of live work alone."""
    from datetime import UTC, datetime

    from app.services.evaluation_continuation import sweep_stranded_runs

    if kwargs.get("run_completed_at") == "now":
        kwargs["run_completed_at"] = datetime.now(UTC)

    sm, cleanup = await _make_db()
    try:
        run_id, _, _ = await _stranded_fixture(sm, **kwargs)
        async with sm() as s:
            swept = await sweep_stranded_runs(s)
        status, _ = await _run_status(sm, run_id)
        assert swept == 0 and status == "running", (
            f"the sweep touched a run it must not have — {why}. "
            f"swept={swept} status={status!r}"
        )
    finally:
        await cleanup()


@pytest.mark.asyncio
async def test_a_run_whose_episode_is_held_is_deferred_not_terminalised():
    """A run whose episode is held is deferred, not terminalised.

    A held episode is evidence AGAINST abandonment. Failing the run around it
    would leave a ``running`` episode under a ``failed`` run, which the sweep,
    selecting only ``"running"`` runs, would never look at again.
    """
    import asyncio

    from app.services.evaluation_continuation import sweep_stranded_runs

    sm, cleanup = await _make_db()
    try:
        run_id, _, _ = await _stranded_fixture(sm, episodes=("running",))

        holder_ready = asyncio.Event()
        release = asyncio.Event()

        async def _hold_the_episode():
            async with sm() as s:
                await s.execute(
                    select(EvaluationEpisode)
                    .where(EvaluationEpisode.run_id == run_id)
                    .with_for_update()
                )
                holder_ready.set()
                await release.wait()
                await s.rollback()

        holder = asyncio.create_task(_hold_the_episode())
        try:
            await asyncio.wait_for(holder_ready.wait(), timeout=10)
            async with sm() as s:
                swept = await sweep_stranded_runs(s)
            status, _ = await _run_status(sm, run_id)
            assert (swept, status) == (0, "running"), (
                f"the run was terminalised around a held episode: "
                f"swept={swept} status={status!r}"
            )
        finally:
            release.set()
            await asyncio.wait_for(holder, timeout=10)

        # And once the holder is gone, a later pass takes it.
        async with sm() as s:
            swept_after = await sweep_stranded_runs(s)
        status_after, _ = await _run_status(sm, run_id)
        assert (swept_after, status_after) == (1, "failed"), (
            "deferring is only correct if a later pass then succeeds; it did not"
        )
    finally:
        await cleanup()


@pytest.mark.asyncio
async def test_staleness_is_judged_by_the_execution_not_by_the_run():
    """Staleness is judged by the execution's ``updated_at``, not the run's
    ``started_at``.

    The run started long ago; its execution went terminal moments ago. By the
    execution, the run is inside the grace period and left alone. By the run, it
    looks ancient and a healthy evaluation is destroyed mid-flight.
    """
    from app.services.evaluation_continuation import sweep_stranded_runs

    sm, cleanup = await _make_db()
    try:
        run_id, _, _ = await _stranded_fixture(
            sm,
            execution_age_seconds=5,       # terminal moments ago → NOT yet stale
            run_started_age_seconds=86_400,  # but the run began a day ago
        )
        async with sm() as s:
            swept = await sweep_stranded_runs(s)
        status, _ = await _run_status(sm, run_id)
        assert (swept, status) == (0, "running"), (
            "the sweep judged staleness from the run's start time, not from its "
            "execution. A long-running evaluation whose execution just finished "
            "would be destroyed"
        )
    finally:
        await cleanup()


@pytest.mark.asyncio
async def test_two_replicas_sweeping_concurrently_terminalise_each_run_once():
    """Two replicas sweeping at once terminalise each run once, by row lock."""
    import asyncio

    from app.services.evaluation_continuation import sweep_stranded_runs

    sm, cleanup = await _make_db()
    try:
        run_ids = [(await _stranded_fixture(sm))[0] for _ in range(4)]

        async def _replica():
            async with sm() as s:
                return await sweep_stranded_runs(s)

        a, b = await asyncio.gather(_replica(), _replica())
        assert a + b == 4, f"runs swept {a} + {b} times, expected 4 in total"
        async with sm() as s:
            statuses = [
                (await s.get(EvaluationRun, r)).status for r in run_ids
            ]
        assert statuses == ["failed"] * 4, statuses
    finally:
        await cleanup()


@pytest.mark.asyncio
async def test_one_pass_is_bounded_and_the_rest_is_taken_next_pass():
    """One pass is bounded and the rest waits for the next: periodic() is awaited
    inline, so an unbounded pass is a liveness bug."""
    from app.services.evaluation_continuation import sweep_stranded_runs

    sm, cleanup = await _make_db()
    try:
        for _ in range(5):
            await _stranded_fixture(sm)
        async with sm() as s:
            first = await sweep_stranded_runs(s, limit=2)
        assert first == 2, f"the pass took {first} runs, ignoring its limit"
        async with sm() as s:
            second = await sweep_stranded_runs(s, limit=10)
        assert second == 3, f"the residual was not taken next pass: {second}"
    finally:
        await cleanup()


@pytest.mark.asyncio
async def test_a_swept_runs_headlines_match_a_normally_finished_runs():
    """A swept run's headlines match a normally finished run's.

    Two runs with identical stored evidence, one finished the ordinary way, one
    swept: every headline field agrees. A sweep with its own formula (a
    divergence on the neighbouring path) fails this.
    """
    from app.services.evaluation_continuation import (
        _write_canonical_headlines,
        sweep_stranded_runs,
    )

    sm, cleanup = await _make_db()
    try:
        swept_id, _, swept_entries = await _stranded_fixture(sm, entries=2)
        async with sm() as s:
            swept_run = await s.get(EvaluationRun, swept_id)
            eval_id = swept_run.evaluation_id
            agent_id = swept_run.agent_id
            # A sibling in the SAME evaluation, so any evaluation-wide
            # comparative pass sees both.
            control = EvaluationRun(
                evaluation_id=eval_id,
                agent_id=agent_id,
                model_id="gpt-4.1",
                status="running",
                trigger_reason="manual",
                total_entries=2,
                completed_entries=2,
            )
            s.add(control)
            await s.flush()
            for run_id in (swept_id, control.id):
                for i, entry_id in enumerate(swept_entries[:2]):
                    s.add(
                        EvaluationResult(
                            run_id=run_id,
                            entry_id=entry_id,
                            agent_output="same evidence",
                            score=60 + i * 10,
                            f1_score=0.5 + i * 0.1,
                            sub_scores={},
                            evaluator_reasoning="r",
                            duration_seconds=1 + i,
                        )
                    )
            await s.commit()
            control_id = control.id

        # The control finishes the ordinary way.
        async with sm() as s:
            control_run = await s.get(EvaluationRun, control_id)
            control_run.status = "completed"
            await _write_canonical_headlines(s, control_run)
            await s.commit()

        async with sm() as s:
            assert await sweep_stranded_runs(s) == 1

        def _headline(row):
            # ``headline_details`` too, not just the four numerics: it is
            # the persisted provenance the API returns.
            return (
                row.score,
                row.quality_score,
                row.f1_score,
                row.performance_score,
                row.headline_details,
            )

        async with sm() as s:
            swept = _headline(await s.get(EvaluationRun, swept_id))
            control_h = _headline(await s.get(EvaluationRun, control_id))
        # Two runs both scoring (None, None, None, None) would agree trivially.
        # The four SCORES are checked, not the whole tuple: a set
        # ``headline_details`` with all-None scores would satisfy ``any`` on it.
        assert any(v is not None for v in control_h[:4]), (
            f"the control run has no headline scores at all ({control_h}), so "
            "this comparison would pass whatever the sweep wrote"
        )
        assert swept == control_h, (
            f"a swept run's headlines {swept} differ from a normally-finished "
            f"run's {control_h} over identical evidence — the path is visible "
            "in the numbers, which a repair must never do"
        )
    finally:
        await cleanup()


@pytest.mark.asyncio
async def test_the_sweep_tells_an_operator_which_run_it_closed_and_why(caplog):
    """The sweep logs which run it closed and why, so no run changes state
    without a trace.
    """
    import logging

    from app.services.evaluation_continuation import sweep_stranded_runs

    sm, cleanup = await _make_db()
    try:
        run_id, execution_id, _ = await _stranded_fixture(sm)
        with caplog.at_level(logging.WARNING):
            async with sm() as s:
                assert await sweep_stranded_runs(s) == 1
        messages = [r.getMessage() for r in caplog.records]
        assert any(str(run_id) in m for m in messages), (
            f"no log names the run that was closed out: {messages}"
        )
        assert any(str(execution_id) in m for m in messages), (
            "the log does not name the execution that owned it, so an operator "
            f"cannot trace why: {messages}"
        )
    finally:
        await cleanup()


@pytest.mark.asyncio
async def test_the_sweep_reaches_a_run_the_repair_itself_declined_to_touch():
    """The sweep reaches a run the episode repair declined to touch.

    Driven through ``fail_orphaned_episodes``, since the interaction is the point:

    1. a predecessor holds an episode row;
    2. ``fail_orphaned_episodes`` passes over it with ``skip_locked`` (a locked
       row is live work, and blocking would deadlock against the live path);
    3. the predecessor dies, releasing the row, and nothing else looks at it.

    The real skip window needs a real database, hence this file.
    """
    import asyncio

    from app.services.evaluation_continuation import (
        fail_orphaned_episodes,
        sweep_stranded_runs,
    )

    sm, cleanup = await _make_db()
    try:
        run_id, execution_id, _ = await _stranded_fixture(sm, episodes=("running",))

        holder_ready = asyncio.Event()
        release = asyncio.Event()
        repair_result = {}

        async def _predecessor_holding_the_row():
            async with sm() as s:
                await s.execute(
                    select(EvaluationEpisode)
                    .where(EvaluationEpisode.run_id == run_id)
                    .with_for_update()
                )
                holder_ready.set()
                await release.wait()
                await s.rollback()  # the predecessor dies

        holder = asyncio.create_task(_predecessor_holding_the_row())
        try:
            await asyncio.wait_for(holder_ready.wait(), timeout=10)
            # THE REPAIR RUNS AND SKIPS IT.
            async with sm() as s:
                repair_result["repaired"] = await fail_orphaned_episodes(
                    s,
                    owned_run_ids=[run_id],
                    reclaimed_run_ids=[],
                    execution_id=execution_id,
                    error="abandoned by a previous attempt",
                )
                await s.commit()
        finally:
            release.set()
            await asyncio.wait_for(holder, timeout=10)

        assert repair_result["repaired"] == 0, (
            "the repair did NOT skip the held episode, so this test is not "
            "exercising the window it claims to — the whole premise is that "
            f"skip_locked passed over it (repaired={repair_result['repaired']})"
        )
        async with sm() as s:
            still_running = (
                await s.execute(
                    select(EvaluationEpisode).where(
                        EvaluationEpisode.run_id == run_id,
                        EvaluationEpisode.status == "running",
                    )
                )
            ).scalars().all()
        assert len(still_running) == 1, (
            "after the holder died the episode should be 'running' again with "
            "nothing left that will look at it — that is the stranded state"
        )

        # No further trigger for this evaluation. The sweep alone must reach it.
        async with sm() as s:
            assert await sweep_stranded_runs(s) == 1, (
                "the run the repair declined to touch was never reached"
            )
        status, _ = await _run_status(sm, run_id)
        assert status == "failed", f"left {status!r}"
    finally:
        await cleanup()


@pytest.mark.asyncio
async def test_a_swept_run_carries_a_duration_like_every_other_failed_run():
    """A swept run carries ``duration_seconds`` like every other failed run.

    The reclaim, ``_fail_active_runs`` and ``_reconcile_run_status`` all set it;
    a completion time with a blank duration reads as a defect in the run.
    """
    from app.services.evaluation_continuation import sweep_stranded_runs

    sm, cleanup = await _make_db()
    try:
        run_id, _, _ = await _stranded_fixture(sm, run_started_age_seconds=300)
        async with sm() as s:
            assert await sweep_stranded_runs(s) == 1
        async with sm() as s:
            run = await s.get(EvaluationRun, run_id)
        assert run.duration_seconds is not None, (
            "a swept run has no duration, unlike every other failed run"
        )
        assert run.duration_seconds > 0, run.duration_seconds
    finally:
        await cleanup()


@pytest.mark.asyncio
async def test_a_replica_never_waits_on_a_run_another_replica_holds():
    """A replica never waits on a run row another replica holds.

    The concurrency test next door also passes when replicas block, since each
    run is still swept once. Here, with the row held, the sweep RETURNS; without
    ``skip_locked`` it hangs.
    """
    import asyncio

    from app.services.evaluation_continuation import sweep_stranded_runs

    sm, cleanup = await _make_db()
    try:
        run_id, _, _ = await _stranded_fixture(sm)
        holder_ready = asyncio.Event()
        release = asyncio.Event()

        async def _other_replica_holding_the_run():
            async with sm() as s:
                await s.execute(
                    select(EvaluationRun)
                    .where(EvaluationRun.id == run_id)
                    .with_for_update()
                )
                holder_ready.set()
                await release.wait()
                await s.rollback()

        holder = asyncio.create_task(_other_replica_holding_the_run())
        try:
            await asyncio.wait_for(holder_ready.wait(), timeout=10)
            async with sm() as s:
                # The bound is the assertion: without skip_locked this waits
                # for the holder, which only releases after this returns.
                swept = await asyncio.wait_for(sweep_stranded_runs(s), timeout=5)
            assert swept == 0, "a held run was swept by a second replica"
        finally:
            release.set()
            await asyncio.wait_for(holder, timeout=10)

        # And the next tick takes it, once the holder is gone.
        async with sm() as s:
            assert await sweep_stranded_runs(s) == 1
    finally:
        await cleanup()


@pytest.mark.asyncio
async def test_a_callback_accepted_mid_sweep_is_not_swept_away():
    """A callback accepted mid-sweep is not swept away: the parked veto is decided
    under the episode lock.

    The sweep's parked pre-filter is an UNLOCKED read, and its run lock does not
    exclude the callback endpoint, which locks the EPISODE alone and never
    restamps ``owner_execution_id``. A callback committing between that read and
    the episode lock turns the last ``"running"`` episode into ``"resumed"``;
    failing the run then loses accepted work. The sub-millisecond window is
    FORCED from inside the sweep's own session, since a race would pass either
    way. The episode lock must cover more than ``status == "running"``.
    """
    from app.services.evaluation_continuation import (
        ConversationContinuationService,
        sweep_stranded_runs,
    )

    sm, cleanup = await _make_db()
    try:
        run_id, _, _ = await _stranded_fixture(sm, episodes=("running",))
        async with sm() as s:
            correlation_id = (
                await s.execute(
                    select(EvaluationEpisode.correlation_id).where(
                        EvaluationEpisode.run_id == run_id
                    )
                )
            ).scalar_one()

        async with sm() as s:
            inner_execute = s.execute
            fired = []

            async def execute_then_land_the_callback(statement, *args, **kwargs):
                result = await inner_execute(statement, *args, **kwargs)
                rendered = str(statement).lower()
                # The unlocked parked pre-filter: the only statement selecting
                # the episode's run_id without FOR UPDATE. The callback lands here.
                if (
                    not fired
                    and "evaluation_episodes.run_id \nfrom" in rendered
                    and "for update" not in rendered
                ):
                    fired.append(True)
                    async with sm() as callback_session:
                        await ConversationContinuationService.resume_from_callback(
                            callback_session,
                            correlation_id=correlation_id,
                            idempotency_key="idem-1",
                            payload={"final_output": "the answer arrived"},
                        )
                        await callback_session.commit()
                return result

            s.execute = execute_then_land_the_callback
            swept = await sweep_stranded_runs(s)

        assert fired, (
            "the callback never landed — the pre-filter query this test hooks "
            "has changed shape, so the test is no longer exercising the window"
        )

        async with sm() as s:
            run = await s.get(EvaluationRun, run_id)
            episode = (
                await s.execute(
                    select(EvaluationEpisode).where(
                        EvaluationEpisode.run_id == run_id
                    )
                )
            ).scalar_one()

        assert episode.status == "resumed", (
            f"the episode reads {episode.status!r} — the callback did not take "
            "effect, so this test proves nothing about the sweep"
        )
        assert swept == 0, (
            f"the sweep reported taking {swept} run(s) although a callback had "
            "been accepted for this one"
        )
        assert run.status == "running", (
            f"the run reads {run.status!r}: it was closed after its callback was "
            "accepted, and _reconcile_run_status will freeze it there — the "
            "callback is lost"
        )
    finally:
        await cleanup()


@pytest.mark.asyncio
async def test_a_misconfigured_grace_period_cannot_abolish_the_grace_period(
    monkeypatch,
):
    """A negative grace period cannot move the cutoff into the future.

    ``evaluation_stranded_run_grace_seconds`` is subtracted from ``now``, so a
    negative value would let every just-terminal execution satisfy
    ``updated_at < cutoff``, removing the protection the setting exists for.
    The ``max(stranded_run_grace_floor_seconds(), ...)`` clamp sits at the use
    site, as ``stale_assignment_run_seconds`` does in ``autonomous.py``.
    """
    from app.core.config import settings as app_settings
    from app.services.evaluation_continuation import sweep_stranded_runs

    monkeypatch.setattr(
        app_settings, "evaluation_stranded_run_grace_seconds", -3600
    )

    sm, cleanup = await _make_db()
    try:
        run_id, _, _ = await _stranded_fixture(sm, execution_age_seconds=10)

        async with sm() as s:
            swept = await sweep_stranded_runs(s)

        status, _ = await _run_status(sm, run_id)
        assert swept == 0, (
            f"the sweep took {swept} run(s) whose execution went terminal ten "
            "seconds ago — the negative grace inverted the cutoff"
        )
        assert status == "running", (
            f"the run reads {status!r}: a misconfigured setting closed it with "
            "no grace window at all"
        )
    finally:
        await cleanup()


@pytest.mark.asyncio
async def test_a_full_batch_costs_a_fraction_of_the_reconciler_heartbeat_budget():
    """A full sweep batch costs a fraction of the reconciler heartbeat budget.

    ``periodic`` is awaited inline in the reconciler cycle, and this sweep writes
    canonical headlines per run, each in its own savepoint. A slow pass would
    push the cycle toward ``reconciler_heartbeat_max_age_seconds`` (120s) and
    trip the liveness probe. Measured on 20 runs of 10 entries with 7 results
    each: 0.145s total, 7.2ms per run, so it shares
    ``orchestration_claim_batch``. The ceiling sits two orders of magnitude
    above that, to catch a change of ALGORITHM (an N+1, a lock that waits), not
    CI timing noise.
    """
    import time

    from app.core.config import settings as app_settings
    from app.services.evaluation_continuation import sweep_stranded_runs

    sm, cleanup = await _make_db()
    try:
        batch = app_settings.orchestration_claim_batch
        for _ in range(batch):
            run_id, _, entry_ids = await _stranded_fixture(
                sm, episodes=("running",), entries=10
            )
            async with sm() as s:
                for i, entry_id in enumerate(entry_ids[:7]):
                    s.add(
                        EvaluationResult(
                            run_id=run_id,
                            entry_id=entry_id,
                            agent_output=f"out-{i}",
                            score=60 + i,
                            sub_scores={"a": 1, "b": 2},
                            evaluator_reasoning="r",
                            duration_seconds=1.5,
                        )
                    )
                await s.commit()

        async with sm() as s:
            started = time.perf_counter()
            swept = await sweep_stranded_runs(s)
            elapsed = time.perf_counter() - started

        assert swept == batch, (
            f"the pass took {swept} of {batch} runs, so the figure below is not "
            "the cost of a full batch"
        )
        assert elapsed < 15, (
            f"a full batch took {elapsed:.2f}s against a 0.145s measurement — "
            "that is an algorithmic change, not timing noise"
        )
    finally:
        await cleanup()


@pytest.mark.asyncio
async def test_the_sweep_leaves_no_episode_of_its_run_non_terminal():
    """The sweep finalises every episode it locks, ``"pending"`` included.

    A non-terminal episode left under a ``"failed"`` run is never looked at
    again, since the sweep selects only ``"running"`` runs. Only
    ``start_episode`` creates episodes today, always ``"running"``; one new
    creation site would make ``"pending"`` reachable.
    """
    from app.services.evaluation_continuation import sweep_stranded_runs

    sm, cleanup = await _make_db()
    try:
        run_id, _, _ = await _stranded_fixture(sm, episodes=("running", "pending"))

        async with sm() as s:
            assert await sweep_stranded_runs(s) == 1

        async with sm() as s:
            statuses = sorted(
                (
                    await s.execute(
                        select(EvaluationEpisode.status).where(
                            EvaluationEpisode.run_id == run_id
                        )
                    )
                )
                .scalars()
                .all()
            )
        assert statuses == ["failed", "failed"], (
            f"the run was closed but its episodes read {statuses} — a "
            "non-terminal episode is stranded under a terminal run"
        )
    finally:
        await cleanup()


@pytest.mark.asyncio
async def test_a_swept_run_reports_the_same_progress_a_reconciled_one_would():
    """A swept run reports the same progress a reconciled one would.

    ``_reconcile_run_status`` counts every TERMINAL episode, failed and timed-out
    included, in ``completed_entries``; the sweep does not go through it. The
    count is compared to the sibling's, not a literal, so the two cannot drift.
    """
    from app.services.evaluation_continuation import (
        TERMINAL_EPISODE_STATUSES,
        sweep_stranded_runs,
    )

    sm, cleanup = await _make_db()
    try:
        # Seven already finished, one stranded: the run the sweep has to close.
        run_id, _, _ = await _stranded_fixture(
            sm, entries=10, episodes=("running",) + ("completed",) * 7
        )
        async with sm() as s:
            row = await s.get(EvaluationRun, run_id)
            row.completed_entries = 7
            await s.commit()

        async with sm() as s:
            assert await sweep_stranded_runs(s) == 1

        async with sm() as s:
            run = await s.get(EvaluationRun, run_id)
            sibling_count = (
                await s.execute(
                    select(func.count(EvaluationEpisode.id)).where(
                        EvaluationEpisode.run_id == run_id,
                        EvaluationEpisode.status.in_(list(TERMINAL_EPISODE_STATUSES)),
                    )
                )
            ).scalar_one()

        assert run.completed_entries == sibling_count, (
            f"the swept run reports {run.completed_entries} of "
            f"{run.total_entries} while _reconcile_run_status would report "
            f"{sibling_count} for the same episodes — the progress number tells "
            "the user which path closed the run"
        )
    finally:
        await cleanup()


@pytest.mark.asyncio
async def test_the_sweeps_candidate_scan_is_index_backed():
    """The sweep's candidate query is index-backed, not a sequential scan.

    It runs every couple of seconds, and ``LIMIT`` bounds the rows LOCKED, not
    SCANNED, so without an index the full cost is paid when nothing is
    stranded. Asserted through the PLANNER on the SQL ``sweep_stranded_runs``
    really sends, captured from the connection, so a predicate added to the
    query that the index shape does not match fails here. The table is
    populated first, since Postgres prefers a sequential scan on a few rows.

    This pins the index shape against the query, not that an environment has it:
    the migration builds it best-effort, and ``_ensure_model_indexes`` recovers.
    """
    from sqlalchemy import event

    from app.services.evaluation_continuation import sweep_stranded_runs

    sm, cleanup = await _make_db()
    try:
        run_id, _, _ = await _stranded_fixture(sm)
        async with sm() as s:
            run = await s.get(EvaluationRun, run_id)
            # Enough terminal rows that a sequential scan is the expensive
            # option: many finished runs, few in flight, as in a real deployment.
            for i in range(3000):
                s.add(
                    EvaluationRun(
                        evaluation_id=run.evaluation_id,
                        agent_id=run.agent_id,
                        model_id="gpt-4.1",
                        status="completed",
                        trigger_reason="manual",
                        total_entries=1,
                        completed_entries=1,
                        completed_at=datetime.now(UTC),
                    )
                )
                if i % 500 == 0:
                    await s.flush()
            await s.commit()
            await s.execute(text("ANALYZE evaluation_runs"))
            await s.commit()

        captured: list[tuple[str, object]] = []

        def _capture(_conn, _cursor, statement, parameters, _context, _many):
            # The candidate scan: the sweep's one LIMITed id select that locks
            # nothing (the lock is taken by the next statement).
            if (
                not captured
                and statement.lstrip().startswith("SELECT evaluation_runs.id")
                and "LIMIT" in statement
                and "FOR UPDATE" not in statement
            ):
                captured.append((statement, parameters))

        sync_engine = sm.kw["bind"].sync_engine
        event.listen(sync_engine, "before_cursor_execute", _capture)
        try:
            async with sm() as s:
                await sweep_stranded_runs(s)
        finally:
            event.remove(sync_engine, "before_cursor_execute", _capture)

        assert len(captured) == 1, "the sweep's candidate query was not captured"
        statement, parameters = captured[0]
        async with sm() as s:
            conn = await s.connection()
            plan = "\n".join(
                (await conn.exec_driver_sql(f"EXPLAIN {statement}", parameters))
                .scalars()
                .all()
            )

        assert "ix_evaluation_runs_stranded_candidates" in plan, (
            "the sweep's candidate query does not use the partial index built "
            f"for it, so every tick scans the whole run table:\n{statement}\n{plan}"
        )
    finally:
        await cleanup()


@pytest.mark.asyncio
async def test_detection_happens_on_the_first_pass_after_the_grace_elapses(
    monkeypatch,
):
    """The sweep fires on the first pass after the grace elapses, not before.

    Detection within one grace period plus one tick rests on this; it is pinned
    one pass either side of the boundary. The grace is above the 240s floor, so
    this measures the grace period, not the clamp.
    """
    from app.core.config import settings as app_settings
    from app.services.evaluation_continuation import sweep_stranded_runs

    grace = 300
    monkeypatch.setattr(
        app_settings, "evaluation_stranded_run_grace_seconds", grace
    )

    sm, cleanup = await _make_db()
    try:
        inside, _, _ = await _stranded_fixture(sm, execution_age_seconds=grace - 30)
        outside, _, _ = await _stranded_fixture(sm, execution_age_seconds=grace + 30)

        async with sm() as s:
            swept = await sweep_stranded_runs(s)

        inside_status, _ = await _run_status(sm, inside)
        outside_status, _ = await _run_status(sm, outside)

        assert inside_status == "running", (
            f"a run {grace - 30}s into a {grace}s grace period reads "
            f"{inside_status!r} — the sweep fired before the window it promises"
        )
        assert outside_status == "failed", (
            f"a run {grace + 30}s past a {grace}s grace period reads "
            f"{outside_status!r} — detection did not happen on the first pass "
            "after the grace elapsed, so SC-003's bound is not met"
        )
        assert swept == 1, f"expected exactly the stale run to be taken, took {swept}"
    finally:
        await cleanup()


@pytest.mark.asyncio
async def test_an_episode_status_the_sweep_does_not_know_defers_the_run(caplog):
    """An episode status the sweep does not know defers the run and logs it.

    The sweep classifies through two hand-maintained tuples,
    ``_ACTIVE_EPISODE_STATUSES`` and ``PARKED_EPISODE_STATUSES``. A new status
    in neither could be a second kind of waiting; destroying live work is worse
    than leaving it stranded, so the ``unrecognised`` guard defers.
    """
    import logging

    from app.services.evaluation_continuation import sweep_stranded_runs

    sm, cleanup = await _make_db()
    try:
        run_id, _, _ = await _stranded_fixture(sm, episodes=("awaiting_human",))

        with caplog.at_level(logging.WARNING):
            async with sm() as s:
                swept = await sweep_stranded_runs(s)

        status, _ = await _run_status(sm, run_id)
        async with sm() as s:
            episode_status = (
                await s.execute(
                    select(EvaluationEpisode.status).where(
                        EvaluationEpisode.run_id == run_id
                    )
                )
            ).scalar_one()

        assert (swept, status) == (0, "running"), (
            f"the sweep took a run holding an episode in an unrecognised state "
            f"(swept={swept}, run={status!r})"
        )
        assert episode_status == "awaiting_human", (
            f"the episode was driven to {episode_status!r} by a sweep that does "
            "not know what that state means"
        )
        assert any("awaiting_human" in r.getMessage() for r in caplog.records), (
            "the sweep deferred silently — an operator has no way to learn that "
            "a status needs classifying"
        )
    finally:
        await cleanup()


@pytest.mark.asyncio
async def test_a_run_naming_an_execution_that_no_longer_exists_is_left_alone():
    """A run naming an execution that no longer exists is left alone.

    ``owner_execution_id`` has no foreign key. The sweep reads an owned run's
    clock through an OUTER join to the execution, so with no row the state is
    NULL and the owned arm does not match, while the unowned arm requires no
    owner. Nothing produces this state today, so the behaviour rests on the
    predicate alone: a NULL-tolerant state check would sweep the run on no
    evidence.
    """
    from app.services.evaluation_continuation import sweep_stranded_runs

    sm, cleanup = await _make_db()
    try:
        run_id, execution_id, _ = await _stranded_fixture(sm)
        # Delete the execution the run names, leaving the id dangling (no FK).
        async with sm() as s:
            await s.execute(
                delete(OrchestrationExecution).where(
                    OrchestrationExecution.id == execution_id
                )
            )
            await s.commit()

        async with sm() as s:
            still_dangling = (
                await s.execute(
                    select(EvaluationRun.owner_execution_id).where(
                        EvaluationRun.id == run_id
                    )
                )
            ).scalar_one()
        assert still_dangling == execution_id, (
            "the owner id was cleared when its execution went away, so a "
            "foreign key or trigger now exists and the dangling-owner case is no "
            "longer reachable — this test is testing nothing"
        )

        async with sm() as s:
            swept = await sweep_stranded_runs(s)

        status, error = await _run_status(sm, run_id)
        assert (swept, status) == (0, "running"), (
            f"the sweep closed out a run whose owning execution is gone "
            f"(swept={swept}, status={status!r}, error={error!r}) — it has no "
            "clock, so there is no evidence it was abandoned rather than live"
        )
    finally:
        await cleanup()


@pytest.mark.asyncio
async def test_runs_parked_on_callbacks_cannot_crowd_out_a_stranded_one():
    """Runs parked on callbacks never take a candidate slot from a stranded run.

    A parked run matches every clause of the candidate query: ``"running"``,
    unfinished, owned by an execution terminal since it parked (the callback
    endpoint never restamps ``owner_execution_id``). Vetoed after the ``LIMIT``,
    more parked runs than a batch would starve a stranded run sorting after them
    under ``ORDER BY id``, forever. The veto is in the candidate query; the
    locked re-check still closes the callback race. The stranded id is forced
    to sort last.
    """
    from app.core.config import settings as app_settings
    from app.services.evaluation_continuation import sweep_stranded_runs

    sm, cleanup = await _make_db()
    try:
        parked_count = app_settings.orchestration_claim_batch + 5
        for _ in range(parked_count):
            await _stranded_fixture(sm, episodes=("waiting_callback",))

        stranded_id, _, _ = await _stranded_fixture(sm)
        # Sorts after every parked run, so the test is deterministic.
        last_id = uuid.UUID("ffffffff-ffff-4fff-8fff-ffffffffffff")
        async with sm() as s:
            await s.execute(
                update(EvaluationRun)
                .where(EvaluationRun.id == stranded_id)
                .values(id=last_id)
            )
            await s.commit()

        async with sm() as s:
            swept = await sweep_stranded_runs(s)

        async with sm() as s:
            run = await s.get(EvaluationRun, last_id)
        assert run.status == "failed", (
            f"the stranded run is still {run.status!r} after a pass: "
            f"{parked_count} runs parked on callbacks filled every candidate "
            "slot, and since their ids sort first it will never be reached on "
            "any future pass either"
        )
        assert swept == 1, (
            f"the pass closed {swept} runs; only the stranded one should be "
            "taken, and no parked run may be touched"
        )
    finally:
        await cleanup()


async def test_the_grace_floor_tracks_the_retry_budget_it_is_derived_from(monkeypatch):
    """The grace floor moves with ``orchestration_default_max_attempts``.

    At seven attempts the retry bound passes 1200s, above a fixed floor and the
    600s default, and the sweep would close runs the engine still retries.
    Asserted as a RELATION against the real retry machinery: the floor covers
    the backoff ``_backoff_seconds`` waits at its jitter ceiling, plus one lease
    expiry per attempt.
    """
    from app.core.config import settings as app_settings
    from app.services import orchestration_engine
    from app.services.evaluation_continuation import stranded_run_grace_floor_seconds

    # The jitter at its ceiling: the longest wait the engine can draw.
    monkeypatch.setattr(orchestration_engine.random, "uniform", lambda _low, high: high)

    seen = {}
    for attempts in (1, 3, 7, 12):
        monkeypatch.setattr(app_settings, "orchestration_default_max_attempts", attempts)
        floor = stranded_run_grace_floor_seconds()
        worst_case = (
            sum(
                orchestration_engine._backoff_seconds(a)
                for a in range(1, attempts + 1)
            )
            + app_settings.orchestration_lease_seconds * attempts
        )
        assert floor >= worst_case, (
            f"at {attempts} attempts the floor is {floor}s but a retry can "
            f"legitimately still be coming for {worst_case:.0f}s — the sweep "
            "would race it"
        )
        seen[attempts] = floor
    assert seen[12] > seen[7] > seen[3] > seen[1], (
        f"the floor does not track the attempt budget: {seen}"
    )


# ── runs an upgrade leaves without an owner ──────────────────────────
#
# The release before ``owner_execution_id`` ran an evaluation as one execution
# naming the EVALUATION. A run it left ``"running"`` has no owner, so its clock
# is whether any live execution still names that evaluation.


async def _unowned_run(sm, *, names="its_evaluation", **kwargs):
    """A run with no owner, and the fixture's execution naming *names*.

    ``"its_evaluation"`` or ``"another_evaluation"`` set the payload the previous
    release wrote; ``"nothing"`` deletes the execution, as a purge would.
    """
    run_id, execution_id, entry_ids = await _stranded_fixture(sm, owner=False, **kwargs)
    async with sm() as s:
        run = await s.get(EvaluationRun, run_id)
        if names == "nothing":
            await s.execute(
                delete(OrchestrationExecution).where(
                    OrchestrationExecution.id == execution_id
                )
            )
        else:
            evaluation_id = (
                run.evaluation_id if names == "its_evaluation" else uuid.uuid4()
            )
            await s.execute(
                update(OrchestrationExecution)
                .where(OrchestrationExecution.id == execution_id)
                .values(
                    payload={
                        "evaluation_id": str(evaluation_id),
                        "trigger_reason": "manual",
                    }
                )
            )
        await s.commit()
    return run_id, entry_ids


@pytest.mark.asyncio
async def test_a_run_an_upgrade_left_unowned_fails_with_its_episodes():
    """An unowned run whose evaluation has only terminal executions is failed.

    As an owned stranded run is: its episodes too, with ``completed_at``,
    ``duration_seconds`` and ``completed_entries`` recomputed, under an error
    that names the upgrade.
    """
    from app.services.evaluation_continuation import (
        UPGRADE_INTERRUPTED_RUN_ERROR,
        sweep_stranded_runs,
    )

    sm, cleanup = await _make_db()
    try:
        run_id, _ = await _unowned_run(sm, episodes=("running", "completed"))

        async with sm() as s:
            assert await sweep_stranded_runs(s) == 1, (
                "a run the previous release left running, whose evaluation has "
                "no live execution, was not swept: it stays running for good"
            )

        async with sm() as s:
            run = await s.get(EvaluationRun, run_id)
            episodes = sorted(
                (
                    await s.execute(
                        select(EvaluationEpisode.status, EvaluationEpisode.error).where(
                            EvaluationEpisode.run_id == run_id
                        )
                    )
                ).all()
            )
        assert (run.status, run.error) == ("failed", UPGRADE_INTERRUPTED_RUN_ERROR)
        assert "upgrade" in run.error and "re-run" in run.error
        assert episodes == [
            ("completed", None),
            ("failed", UPGRADE_INTERRUPTED_RUN_ERROR),
        ], f"the run's episodes were not closed with it: {episodes}"
        assert run.completed_entries == 2, (
            f"completed_entries reads {run.completed_entries}, not the 2 "
            "terminal episodes"
        )
        assert run.completed_at is not None
        assert run.duration_seconds is not None and run.duration_seconds > 0
    finally:
        await cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize("execution_state", ["pending", "running"])
async def test_an_unowned_run_whose_evaluation_has_a_live_execution_is_left_alone(
    execution_state,
):
    """An unowned run is left alone while an execution of its evaluation is live.

    An old replica may still be running it during a rolling deploy, or a trigger
    path failed to stamp it and its worker holds it.
    """
    from app.services.evaluation_continuation import sweep_stranded_runs

    sm, cleanup = await _make_db()
    try:
        run_id, _ = await _unowned_run(
            sm, execution_state=execution_state, episodes=("running",)
        )

        async with sm() as s:
            swept = await sweep_stranded_runs(s)

        status, _ = await _run_status(sm, run_id)
        async with sm() as s:
            episode_status = (
                await s.execute(
                    select(EvaluationEpisode.status).where(
                        EvaluationEpisode.run_id == run_id
                    )
                )
            ).scalar_one()
        assert (swept, status, episode_status) == (0, "running", "running"), (
            f"the sweep closed an unowned run while a {execution_state!r} "
            "execution of its evaluation may still be running it"
        )
    finally:
        await cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "names",
    [
        pytest.param("another_evaluation", id="live_execution_of_another_evaluation"),
        pytest.param("nothing", id="no_execution_left"),
    ],
)
async def test_only_a_live_execution_of_its_own_evaluation_holds_an_unowned_run(names):
    """Another evaluation's live execution does not hold an unowned run, and none at all is abandonment."""
    from app.services.evaluation_continuation import sweep_stranded_runs

    sm, cleanup = await _make_db()
    try:
        run_id, _ = await _unowned_run(sm, names=names, execution_state="running")

        async with sm() as s:
            swept = await sweep_stranded_runs(s)

        status, _ = await _run_status(sm, run_id)
        assert (swept, status) == (1, "failed"), (
            f"an unowned run with {names!r} named was not swept "
            f"(swept={swept}, status={status!r})"
        )
    finally:
        await cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs",
    [
        pytest.param({"episodes": ("waiting_callback",)}, id="parked_on_a_callback"),
        pytest.param({"episodes": ("resumed",)}, id="callback_just_accepted"),
        pytest.param({"run_completed_at": "now"}, id="finished_run_being_rescored"),
    ],
)
async def test_the_sweep_leaves_live_work_of_an_unowned_run_alone(kwargs):
    """The parked and re-score vetoes hold for an unowned run too."""
    from app.services.evaluation_continuation import sweep_stranded_runs

    if kwargs.get("run_completed_at") == "now":
        kwargs["run_completed_at"] = datetime.now(UTC)

    sm, cleanup = await _make_db()
    try:
        run_id, _ = await _unowned_run(sm, **kwargs)

        async with sm() as s:
            swept = await sweep_stranded_runs(s)

        status, _ = await _run_status(sm, run_id)
        assert (swept, status) == (0, "running"), (
            f"the sweep closed an unowned run holding live work ({kwargs})"
        )
    finally:
        await cleanup()


@pytest.mark.asyncio
async def test_an_unowned_run_started_inside_the_grace_period_is_left_alone():
    """An unowned run is swept only once it started before the grace cutoff.

    The grace covers a run committed before the execution that runs it.
    """
    from app.services.evaluation_continuation import sweep_stranded_runs

    sm, cleanup = await _make_db()
    try:
        run_id, _ = await _unowned_run(sm, run_started_age_seconds=5)

        async with sm() as s:
            swept = await sweep_stranded_runs(s)

        status, _ = await _run_status(sm, run_id)
        assert (swept, status) == (0, "running"), (
            "the sweep closed an unowned run that started moments ago"
        )
    finally:
        await cleanup()


@pytest.mark.asyncio
async def test_an_owned_run_is_swept_whatever_else_runs_for_its_evaluation():
    """The live-execution veto is the unowned arm's alone.

    An owned run's clock is its own execution, so a later launch of the same
    evaluation does not hold it, and its error stays the stranded one.
    """
    from app.services.evaluation_continuation import (
        STRANDED_RUN_ERROR,
        sweep_stranded_runs,
    )

    sm, cleanup = await _make_db()
    try:
        run_id, _, _ = await _stranded_fixture(sm)
        async with sm() as s:
            run = await s.get(EvaluationRun, run_id)
            s.add(
                OrchestrationExecution(
                    adapter="evaluation",
                    state="running",
                    payload={"evaluation_id": str(run.evaluation_id)},
                    attempt_count=1,
                )
            )
            await s.commit()

        async with sm() as s:
            swept = await sweep_stranded_runs(s)

        status, error = await _run_status(sm, run_id)
        assert (swept, status, error) == (1, "failed", STRANDED_RUN_ERROR), (
            "an owned stranded run was held by another execution of its "
            f"evaluation (swept={swept}, status={status!r}, error={error!r})"
        )
    finally:
        await cleanup()
