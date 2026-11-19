"""Run ownership against a real (throwaway) PostgreSQL database.

Every run carries the execution that put it into pending or running
(``owner_execution_id``): each launch path stamps it, ``execute_evaluation`` runs only
the runs it owns, and the cleanup of an abandoned launch or re-score
(``_fail_active_runs``, ``_fail_rescore_runs``) fails only what that attempt still owns.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

import app.models  # noqa: F401 - register model metadata
from app.api.v1 import evaluations as evaluations_api
from app.models.agent import Agent
from app.models.evaluation import (
    Evaluation,
    EvaluationAgent,
    EvaluationResult,
    EvaluationRun,
)
from app.models.user import User
from app.services.evaluation_engine import NO_LEASE
from tests.evaluation_database import (
    _active_runs_fixture,
    _admin,
    _linked_dataset,
    _make_db,
    _make_db_full,
    _stub_audit_and_groups,  # noqa: F401 - autouse fixture
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


# --------------------------------------------- attempt-bound ownership
# Every run carries the execution that put it into pending/running, so an
# abandoned execution only ever fails (or runs) what it still owns.
# ``owner_execution_id`` has no FK: tests may use arbitrary UUIDs.


async def _stage_spy(monkeypatch, which: str) -> list:
    """Wrap the real staging helper and capture the executions it creates."""
    import app.services.evaluation_engine as engine

    staged: list = []
    real = getattr(engine, which)

    async def _spy(db, *args, **kwargs):
        execution = await real(db, *args, **kwargs)
        staged.append(execution)
        return execution

    monkeypatch.setattr(f"app.services.evaluation_engine.{which}", _spy)

    async def _noop_wake():
        return None

    monkeypatch.setattr(
        "app.services.orchestration_engine.notify_orchestration_wake", _noop_wake
    )
    return staged


async def test_replay_stamps_the_staged_execution_as_run_owner(monkeypatch):
    """A replayed run is committed owned by the staged execution."""
    staged = await _stage_spy(monkeypatch, "stage_evaluation_execution")

    sm, cleanup = await _make_db_full()
    try:
        async with sm() as s:
            admin = await _admin(s)
            evaluation = Evaluation(name="Eval", family="STIX_BUNDLE")
            s.add(evaluation)
            await s.flush()
            await _linked_dataset(s, evaluation, family="STIX_BUNDLE")
            agent = Agent(user_id=admin.id, name="Agent", skip_llm=True)
            s.add(agent)
            await s.flush()
            original = EvaluationRun(
                evaluation_id=evaluation.id,
                agent_id=agent.id,
                model_id="gpt-4.1",
                status="completed",
                trigger_reason="manual",
            )
            s.add(original)
            await s.commit()

            await evaluations_api.replay_run(
                run_id=str(original.id), user=admin, db=s
            )

        assert len(staged) == 1
        async with sm() as s2:
            replayed = (
                await s2.execute(
                    select(EvaluationRun).where(
                        EvaluationRun.evaluation_id == evaluation.id,
                        EvaluationRun.id != original.id,
                    )
                )
            ).scalar_one()
            assert replayed.owner_execution_id == staged[0].id
            # The original is untouched: ownership is stamped by the execution
            # that made a run active, not retro-applied to its siblings.
            reloaded_original = await s2.get(EvaluationRun, original.id)
            assert reloaded_original.owner_execution_id is None
    finally:
        await cleanup()


async def test_execute_evaluation_runs_only_the_runs_it_owns(monkeypatch):
    """An execution runs its own pending runs plus unowned ones, never another's.

    Two launches of one evaluation can overlap. If A adopted B's runs, B would
    complete with nothing to do, and A dying would leave B's runs ``running``
    with nothing to terminalise them (no periodic sweep). The NULL arm
    keeps rows that predate the column dispatchable.
    """
    from app.services.evaluation_engine import execute_evaluation

    execution_a, execution_b = uuid.uuid4(), uuid.uuid4()
    executed: list[str] = []

    async def _fake_execute_run(_db, run_id, **_kw):
        executed.append(run_id)

    monkeypatch.setattr(
        "app.services.evaluation_engine.execute_evaluation_run", _fake_execute_run
    )

    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            admin = await _admin(s)
            evaluation = Evaluation(name="Eval", family="STIX_BUNDLE")
            s.add(evaluation)
            await s.flush()
            agent = Agent(user_id=admin.id, name="Agent")
            s.add(agent)
            await s.flush()

            def _run(owner):
                return EvaluationRun(
                    evaluation_id=evaluation.id,
                    agent_id=agent.id,
                    model_id="gpt-4.1",
                    status="pending",
                    trigger_reason="manual",
                    owner_execution_id=owner,
                )

            mine, theirs, legacy = _run(execution_a), _run(execution_b), _run(None)
            s.add_all([mine, theirs, legacy])
            await s.commit()
            mine_id, theirs_id, legacy_id = (
                str(mine.id),
                str(theirs.id),
                str(legacy.id),
            )

            await execute_evaluation(
                s, str(evaluation.id), "manual", execution_a,
                worker_id=NO_LEASE,
            )

        assert sorted(executed) == sorted([mine_id, legacy_id])
        assert theirs_id not in executed, (
            "adopting another launch's pending run is what strands it when "
            "this execution later dies"
        )
    finally:
        await cleanup()


# ------------------------------------ re-score cleanup, _fail_rescore_runs
# An abandoned re-score never fails a run a LATER re-score has taken over.


async def _rescore_fixture(sm, *, status, owner, completed_at=None):
    """One evaluation run in *status* owned by *owner*. Returns its id."""
    async with sm() as s:
        # Reusable within one database: some tests build two runs.
        admin = (
            await s.execute(select(User).where(User.email == "a@x.io"))
        ).scalar_one_or_none() or await _admin(s)
        evaluation = Evaluation(name="Eval", family="STIX_BUNDLE")
        s.add(evaluation)
        await s.flush()
        agent = Agent(user_id=admin.id, name="Agent")
        s.add(agent)
        await s.flush()
        run = EvaluationRun(
            evaluation_id=evaluation.id,
            agent_id=agent.id,
            model_id="gpt-4.1",
            status=status,
            trigger_reason="manual",
            owner_execution_id=owner,
            completed_at=completed_at,
        )
        s.add(run)
        await s.commit()
        return run.id, evaluation.id


@pytest.mark.parametrize(
    ("status", "owner", "expected_status", "skip_logged"),
    [
        pytest.param(
            "completed", "later", "completed", False,
            id="completed_by_a_later_execution",
        ),
        pytest.param(
            "running", "later", "running", True,
            id="still_running_under_a_later_execution",
        ),
        pytest.param("running", "abandoned", "failed", False, id="owned_by_the_abandoned"),
        pytest.param("running", None, "failed", False, id="no_recorded_owner"),
    ],
)
async def test_fail_rescore_runs_fails_only_what_the_abandoned_attempt_owns(
    monkeypatch, caplog, status, owner, expected_status, skip_logged
):
    """An abandoned re-score's cleanup fails its own and unowned runs, never a run a
    later attempt took over.

    Failing a taken-over run shows the user a failure that never happened, over
    correct scores. Skipping an unowned run strands runs active before the column
    existed: nothing else terminalises them. A skipped run is logged with both
    executions, and every run keeps its owner, so a disputed score traces back to
    the execution that last acted on it.
    """
    from app.services.orchestration_adapters.evaluation import _fail_rescore_runs

    attempt_a, attempt_b = uuid.uuid4(), uuid.uuid4()
    owner_id = {"later": attempt_b, "abandoned": attempt_a, None: None}[owner]
    sm, cleanup = await _make_db()
    try:
        run_id, _ = await _rescore_fixture(sm, status=status, owner=owner_id)
        with caplog.at_level("WARNING"):
            # The run had failed before the rescore: a status only the cleanup can
            # hand back, so a run it touched is told apart from one it left alone.
            await _fail_rescore_runs(
                [str(run_id)], attempt_a, "A was abandoned", {str(run_id): "failed"}
            )

        async with sm() as s:
            after = await s.get(EvaluationRun, run_id)
            assert after.status == expected_status
            assert after.owner_execution_id == owner_id
            if expected_status == "failed":
                # Handed back its previous status, the failure kept apart from the
                # run's own error.
                assert after.rescore_error == "A was abandoned"
                assert after.error is None
                # A rescore never re-runs the agent, so these still describe the
                # ORIGINAL execution and must stay untouched.
                assert after.completed_at is None
                assert after.duration_seconds is None
            else:
                assert after.error is None
                assert after.rescore_error is None

        if skip_logged:
            logged = "\n".join(r.getMessage() for r in caplog.records)
            assert str(run_id) in logged
            assert str(attempt_a) in logged
            assert str(attempt_b) in logged
    finally:
        await cleanup()


async def test_fail_rescore_runs_never_overwrites_a_cancellation_and_is_idempotent(
    monkeypatch,
):
    """The cleanup never overwrites a cancellation, and a second pass changes nothing.

    The ownership predicate is evaluated at write time, so a cancellation landing
    between a read and the write wins.
    """
    from app.services.orchestration_adapters.evaluation import _fail_rescore_runs

    attempt_a = uuid.uuid4()
    sm, cleanup = await _make_db()
    try:
        run_id, _ = await _rescore_fixture(sm, status="cancelled", owner=attempt_a)
        await _fail_rescore_runs(
            [str(run_id)], attempt_a, "abandoned", {str(run_id): "completed"}
        )
        async with sm() as s:
            assert (await s.get(EvaluationRun, run_id)).status == "cancelled"

        # Idempotence on a run this execution really did fail.
        owned_id, _ = await _rescore_fixture(sm, status="running", owner=attempt_a)
        await _fail_rescore_runs([str(owned_id)], attempt_a, "first", {str(owned_id): "failed"})
        await _fail_rescore_runs([str(owned_id)], attempt_a, "second", {str(owned_id): "failed"})
        async with sm() as s:
            after = await s.get(EvaluationRun, owned_id)
            assert after.status == "failed"
            assert after.rescore_error == "first", "the second pass must be a no-op"
    finally:
        await cleanup()


async def test_a_superseded_rescore_never_writes_over_the_one_that_took_its_run(monkeypatch):
    """A cancel hands the run back at once, so a new rescore can take it while the
    cancelled one is still at work. The old one ends without touching the run
    the new one now owns: neither its status nor its rescore failure."""
    from app.services import evaluation_engine as ee

    first, second = uuid.uuid4(), uuid.uuid4()
    sm, cleanup = await _make_db()
    try:
        run_id, _ = await _rescore_fixture(sm, status="running", owner=first)

        async def _taken_over_then_fails(_session, rid, **_kwargs):
            # The user cancels, re-clicks Re-score: the second rescore owns the run.
            async with sm() as other:
                run = await other.get(EvaluationRun, uuid.UUID(rid))
                run.owner_execution_id = second
                await other.commit()
            raise RuntimeError("the first rescore's judge failed")

        monkeypatch.setattr("app.core.database.async_session", sm)
        monkeypatch.setattr(ee, "rescore_run", _taken_over_then_fails)

        await ee._rescore_run_with_own_session(
            str(run_id), previous_status="completed", execution_id=first
        )

        async with sm() as s:
            after = await s.get(EvaluationRun, run_id)
            assert (after.status, after.owner_execution_id, after.rescore_error) == (
                "running",
                second,
                None,
            )
    finally:
        await cleanup()


@pytest.mark.parametrize("previous_status", ["completed", "failed", "cancelled"])
async def test_an_abandoned_rescore_hands_its_runs_back_their_status(previous_status):
    """A rescore re-judges stored answers, so its abandonment is not the run's failure:
    the run gets back the status it had, the failure recorded apart."""
    from app.services.orchestration_adapters.evaluation import _fail_rescore_runs

    attempt = uuid.uuid4()
    sm, cleanup = await _make_db()
    try:
        run_id, _ = await _rescore_fixture(sm, status="running", owner=attempt)
        await _fail_rescore_runs(
            [str(run_id)], attempt, "abandoned", {str(run_id): previous_status}
        )
        async with sm() as s:
            after = await s.get(EvaluationRun, run_id)
            assert (after.status, after.error, after.rescore_error) == (
                previous_status,
                None,
                "abandoned",
            )
    finally:
        await cleanup()


# ----------------------------------- evaluation cleanup, _fail_active_runs
# The twin path: an abandoned launch never fails runs a later launch owns.


async def test_fail_active_runs_fails_only_what_the_abandoned_launch_owns(
    monkeypatch, caplog
):
    """An abandoned launch's cleanup fails its own and unowned runs, never a later
    launch's pending or running ones.

    Launch B's runs may not have started yet, and cleanup acts on ``pending`` as
    well as ``running``, so ownership has to cover both. A skipped run is logged
    with both executions, and a failed run keeps its owner, so a disputed score
    traces back to the execution that last acted on it.
    """
    from app.services.orchestration_adapters.evaluation import _fail_active_runs

    launch_a, launch_b = uuid.uuid4(), uuid.uuid4()
    sm, cleanup = await _make_db()
    try:
        eval_id, (b_pending, b_running, a_running, a_pending, legacy) = (
            await _active_runs_fixture(
                sm,
                [
                    ("pending", launch_b, {}),
                    ("running", launch_b, {}),
                    ("running", launch_a, {}),
                    ("pending", launch_a, {}),
                    ("running", None, {}),
                ],
            )
        )
        with caplog.at_level("WARNING"):
            await _fail_active_runs(str(eval_id), launch_a, "A was abandoned")

        async with sm() as s:
            for rid, kept_status in ((b_pending, "pending"), (b_running, "running")):
                after = await s.get(EvaluationRun, rid)
                assert after.status == kept_status
                assert after.error is None
                assert after.owner_execution_id == launch_b
            for rid, owner in ((a_running, launch_a), (a_pending, launch_a), (legacy, None)):
                after = await s.get(EvaluationRun, rid)
                assert after.status == "failed"
                assert after.error == "A was abandoned"
                assert after.owner_execution_id == owner

        logged = "\n".join(r.getMessage() for r in caplog.records)
        assert str(b_pending) in logged
        assert str(b_running) in logged
        assert str(launch_a) in logged
        assert str(launch_b) in logged
    finally:
        await cleanup()


async def test_fail_active_runs_stamps_completion_timing_exactly_as_before(
    monkeypatch,
):
    """The cleanup's SQL stamps completion timing: ``duration_seconds`` comes from
    ``started_at`` rounded to two decimals, and a run never started gets none.
    """
    from app.services.orchestration_adapters.evaluation import _fail_active_runs

    launch_a = uuid.uuid4()
    started = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    sm, cleanup = await _make_db()
    try:
        eval_id, (timed, untimed) = await _active_runs_fixture(
            sm,
            [
                ("running", launch_a, {"started_at": started}),
                ("pending", launch_a, {}),
            ],
        )
        before = datetime.now(UTC)
        await _fail_active_runs(str(eval_id), launch_a, "abandoned")

        async with sm() as s:
            with_timing = await s.get(EvaluationRun, timed)
            assert with_timing.completed_at is not None
            expected = (with_timing.completed_at - started).total_seconds()
            assert with_timing.duration_seconds == pytest.approx(
                round(expected, 2), abs=0.01
            )
            assert with_timing.duration_seconds > (before - started).total_seconds() - 1

            no_timing = await s.get(EvaluationRun, untimed)
            assert no_timing.completed_at is not None
            assert no_timing.duration_seconds is None
    finally:
        await cleanup()


async def test_fail_active_runs_leaves_a_run_being_rescored_alone(monkeypatch):
    """An evaluation-wide failure does not sweep an in-flight rescore.

    A rescore marks a completed run ``"running"`` again. ``_fail_active_runs``,
    reached on failure of an unrelated trigger on the same evaluation, would
    otherwise fail it and overwrite ``completed_at``/``duration_seconds``, which
    describe the original agent execution a rescore never re-runs.
    """
    from app.services.orchestration_adapters.evaluation import _fail_active_runs

    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            admin = await _admin(s)
            evaluation = Evaluation(name="Eval", family="STIX_BUNDLE")
            s.add(evaluation)
            await s.flush()
            agent = Agent(user_id=admin.id, name="Agent")
            s.add(agent)
            await s.flush()

            started = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
            rescoring = EvaluationRun(
                evaluation_id=evaluation.id,
                agent_id=agent.id,
                model_id="gpt-4.1",
                status="running",
                trigger_reason="manual",
                started_at=started,
                completed_at=started + timedelta(seconds=30),
                duration_seconds=30.0,
            )
            fresh = EvaluationRun(
                evaluation_id=evaluation.id,
                agent_id=agent.id,
                model_id="gpt-4.1",
                status="running",
                trigger_reason="manual",
                started_at=started,
            )
            s.add_all([rescoring, fresh])
            await s.commit()
            rescoring_id, fresh_id = rescoring.id, fresh.id

        # The runs carry no owner, so the ownership predicate cannot
        # protect the rescoring run: ``completed_at IS NULL`` has to.
        await _fail_active_runs(str(evaluation.id), uuid.uuid4(), "worker crashed")

        async with sm() as s:
            after_rescore = await s.get(EvaluationRun, rescoring_id)
            after_fresh = await s.get(EvaluationRun, fresh_id)

            assert after_rescore.status == "running", (
                "a completed run that is only 'running' because a rescore is "
                "in flight must not be failed by an unrelated evaluation-wide "
                "failure"
            )
            assert after_rescore.duration_seconds == 30.0
            assert after_rescore.completed_at is not None

            assert after_fresh.status == "failed", (
                "a run whose original execution never finished is still swept"
            )
            assert after_fresh.completed_at is not None
    finally:
        await cleanup()


# ------------------------------------- the other entry points and cleanup


async def test_bulk_rescore_stamps_the_staged_execution_on_every_run(monkeypatch):
    """A bulk re-score stamps the staged execution on every run in the batch.

    An unstamped run would be failable by any earlier execution's cleanup.
    """
    from app.api.v1.schemas.evaluation import RescoreFromRunsRequest

    staged = await _stage_spy(monkeypatch, "stage_rescore_execution")
    previous_owner = uuid.uuid4()

    sm, cleanup = await _make_db_full()
    try:
        async with sm() as s:
            admin = await _admin(s)
            evaluation = Evaluation(
                name="Eval",
                family="STIX_BUNDLE",
                model_ids=["gpt-4.1"],
                use_agent_default_model=False,
            )
            s.add(evaluation)
            await s.flush()
            await _linked_dataset(s, evaluation, family="STIX_BUNDLE")
            agent = Agent(user_id=admin.id, name="Agent", skip_llm=True)
            s.add(agent)
            await s.flush()
            runs = [
                EvaluationRun(
                    evaluation_id=evaluation.id,
                    agent_id=agent.id,
                    model_id="gpt-4.1",
                    status="completed",
                    trigger_reason="manual",
                    owner_execution_id=previous_owner,
                )
                for _ in range(2)
            ]
            s.add_all(runs)
            await s.flush()
            for run in runs:
                s.add(EvaluationResult(run_id=run.id, agent_output="hi", score=80.0))
            await s.commit()
            run_ids = [r.id for r in runs]

            await evaluations_api.rescore_evaluation_from_runs(
                evaluation_id=str(evaluation.id),
                body=RescoreFromRunsRequest(run_ids=[str(r) for r in run_ids]),
                user=admin,
                db=s,
            )

        assert len(staged) == 1
        async with sm() as s2:
            for rid in run_ids:
                after = await s2.get(EvaluationRun, rid)
                assert after.status == "running"
                assert after.owner_execution_id == staged[0].id
    finally:
        await cleanup()


async def test_rerun_full_stamps_the_staged_execution_on_every_new_run(monkeypatch):
    """A re-run from baselines stamps the staged execution on every new run."""
    from app.api.v1.schemas.evaluation import TriggerEvaluationFromRunsRequest

    staged = await _stage_spy(monkeypatch, "stage_evaluation_execution")

    sm, cleanup = await _make_db_full()
    try:
        async with sm() as s:
            admin = await _admin(s)
            evaluation = Evaluation(
                name="Eval",
                family="STIX_BUNDLE",
                model_ids=["gpt-4.1"],
                use_agent_default_model=False,
            )
            s.add(evaluation)
            await s.flush()
            await _linked_dataset(s, evaluation, family="STIX_BUNDLE")
            agent = Agent(user_id=admin.id, name="Agent", skip_llm=True)
            s.add(agent)
            await s.flush()
            baseline = EvaluationRun(
                evaluation_id=evaluation.id,
                agent_id=agent.id,
                model_id="gpt-4.1",
                status="completed",
                trigger_reason="manual",
            )
            s.add(baseline)
            await s.commit()

            await evaluations_api.trigger_evaluation_from_runs(
                evaluation_id=str(evaluation.id),
                body=TriggerEvaluationFromRunsRequest(run_ids=[str(baseline.id)]),
                user=admin,
                db=s,
            )

        assert len(staged) == 1
        async with sm() as s2:
            fresh = (
                await s2.execute(
                    select(EvaluationRun).where(
                        EvaluationRun.evaluation_id == evaluation.id,
                        EvaluationRun.id != baseline.id,
                    )
                )
            ).scalars().all()
            assert fresh
            assert all(r.owner_execution_id == staged[0].id for r in fresh)
    finally:
        await cleanup()


# ------------------------------------------------- ownership follow-ups


async def test_scheduled_evaluation_stamps_ownership_on_every_run(monkeypatch):
    """Scheduled runs are committed owned by the execution staged in the same pass.

    The endpoint tests do not reach the scheduler. An unstamped run would look
    like an older row, failable by any other execution's cleanup.
    """
    from app.models.orchestration import OrchestrationExecution
    from app.services import autonomous

    async def _noop_wake():
        return None

    monkeypatch.setattr(
        "app.services.orchestration_engine.notify_orchestration_wake", _noop_wake
    )

    sm, cleanup = await _make_db_full()
    try:
        async with sm() as s:
            admin = await _admin(s)
            agent = Agent(user_id=admin.id, name="Agent", skip_llm=True)
            s.add(agent)
            await s.flush()
            evaluation = Evaluation(
                name="Scheduled",
                family="STIX_BUNDLE",
                model_ids=["gpt-4.1"],
                use_agent_default_model=False,
                enabled=True,
                trigger_config={"schedule": "* * * * *"},
            )
            s.add(evaluation)
            await s.flush()
            await _linked_dataset(s, evaluation, family="STIX_BUNDLE")
            s.add(EvaluationAgent(evaluation_id=evaluation.id, agent_id=agent.id))
            await s.commit()

            count = await autonomous._process_scheduled_evaluations(s)
            assert count == 1

        async with sm() as s2:
            execution = (
                await s2.execute(
                    select(OrchestrationExecution).where(
                        OrchestrationExecution.adapter == "evaluation"
                    )
                )
            ).scalars().one()
            runs = (
                await s2.execute(
                    select(EvaluationRun).where(
                        EvaluationRun.evaluation_id == evaluation.id
                    )
                )
            ).scalars().all()
            assert runs
            assert all(r.owner_execution_id == execution.id for r in runs)
    finally:
        await cleanup()


async def test_unowned_pending_runs_are_claimed_exclusively(monkeypatch):
    """Two launches never both dispatch an unowned run: the claim is exclusive.

    Both overlapping launches match the NULL arm. Cleanup is idempotent; dispatch
    is not: running one run twice re-charges the judge and races two writers
    onto one row.
    """
    from app.services.evaluation_engine import execute_evaluation

    execution_a, execution_b = uuid.uuid4(), uuid.uuid4()
    dispatched: list[tuple[uuid.UUID, str]] = []

    sm, cleanup = await _make_db()
    try:
        eval_id, (legacy,) = await _active_runs_fixture(sm, [("pending", None, {})])

        current: dict[str, uuid.UUID] = {}

        async def _fake_execute_run(_db, run_id, **_kw):
            dispatched.append((current["id"], run_id))

        monkeypatch.setattr(
            "app.services.evaluation_engine.execute_evaluation_run", _fake_execute_run
        )

        # A runs first and claims the unowned row.
        current["id"] = execution_a
        async with sm() as s:
            await execute_evaluation(
                s, str(eval_id), "manual", execution_a,
                worker_id=NO_LEASE,
            )
        # B follows: the row is no longer NULL, so its claim matches nothing.
        current["id"] = execution_b
        async with sm() as s:
            await execute_evaluation(
                s, str(eval_id), "manual", execution_b,
                worker_id=NO_LEASE,
            )

        assert dispatched == [(execution_a, str(legacy))], (
            "a legacy run must be dispatched by exactly one execution"
        )
        async with sm() as s:
            assert (await s.get(EvaluationRun, legacy)).owner_execution_id == execution_a
    finally:
        await cleanup()


async def test_import_never_creates_an_active_ownerless_run():
    """An imported bundle is history: no run comes back active.

    Export keeps a run's status verbatim, so a bundle taken mid-run would
    restore ``pending``/``running`` runs with no execution behind them,
    permanently active.
    """
    from app.api.v1.evaluations import _importable_run_status

    for active in ("pending", "running"):
        assert _importable_run_status(active) == "failed"
    for terminal in ("completed", "failed", "cancelled"):
        assert _importable_run_status(terminal) == terminal
    assert _importable_run_status(None) == "completed"
    # An allowlist: the bundle is user-supplied and the column is
    # String(20), so an unknown value is neither trusted nor left to a DataError.
    assert _importable_run_status("queued") == "failed"
    assert _importable_run_status("x" * 40) == "failed"
    # A missing field keeps the default; a wrong JSON type fails closed rather
    # than make a run look successful.
    assert _importable_run_status("") == "completed"
    for malformed in (123, True, {"a": 1}, []):
        assert _importable_run_status(malformed) == "failed"


async def test_skip_logging_failure_never_blocks_cleanup_or_recovery(monkeypatch):
    """A failing skip log neither fails the cleanup nor skips the recovery.

    The diagnostic read runs after the cleanup UPDATE has committed, and both
    callers still have ``_recover_stranded_headlines`` to do.
    """
    from app.services.orchestration_adapters import evaluation as adapter_mod

    launch_a = uuid.uuid4()
    sm, cleanup = await _make_db()
    try:
        eval_id, (mine,) = await _active_runs_fixture(sm, [("running", launch_a, {})])

        real_log = adapter_mod._log_skipped_runs

        async def _exploding_log(db, still_skipped, execution_id):
            # The diagnostic SELECT fails, as if the DB went away after the UPDATE.
            class _Boom:
                async def execute(self, *_a, **_k):
                    raise RuntimeError("the database went away")

            return await real_log(_Boom(), still_skipped, execution_id)

        monkeypatch.setattr(adapter_mod, "_log_skipped_runs", _exploding_log)

        # Must not raise ...
        await adapter_mod._fail_active_runs(str(eval_id), launch_a, "abandoned")

        # ... and the cleanup is still applied.
        async with sm() as s:
            assert (await s.get(EvaluationRun, mine)).status == "failed"
    finally:
        await cleanup()
