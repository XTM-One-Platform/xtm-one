"""Stopping an evaluation run, against a real (throwaway) PostgreSQL database.

The cancel event ending a run batch, the emergency stop, and the lease fence: once the
orchestration lease moves, the abandoned worker commits no reclaim, aggregate or
headline write, and leaves its runs re-runnable with the entries they recorded.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

import app.models  # noqa: F401 - register model metadata
from app.models.agent import Agent
from app.models.dataset import Dataset, DatasetEntry, EvaluationDataset
from app.models.evaluation import (
    Evaluation,
    EvaluationEpisode,
    EvaluationResult,
    EvaluationRun,
)
from app.models.evaluation_metric import EvaluationMetric
from app.models.orchestration import OrchestrationExecution
from app.services.evaluation_engine import NO_LEASE
from tests.evaluation_database import (
    _active_runs_fixture,
    _admin,
    _make_db,
    _make_db_full,
    _stub_audit_and_groups,  # noqa: F401 - autouse fixture
)
from tests.evaluation_helpers import (
    HeadlinesFake,
    seed_provider,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.mark.asyncio
async def test_a_fired_cancel_event_stops_the_run_batch():
    """A fired cancel event stops the batch, in-flight and queued runs alike.

    ``_gather_runs_bounded`` passes ``return_exceptions=True``, so a cancelled
    child comes back as a CancelledError *result* and the gather completes
    normally. Only counting runner invocations tells a stop from a swallowed one.
    """
    import asyncio

    from app.services.evaluation_engine import _gather_runs_bounded

    started: list[str] = []
    finished: list[str] = []
    cancelled: list[str] = []
    cancel_event = asyncio.Event()

    async def _runner(rid: str) -> None:
        started.append(rid)
        if rid == "r0":
            # First run reaches a known point, then the lease moves.
            cancel_event.set()
        try:
            # Short work, LONG wait_for below: a batch the helper fails to cancel
            # COMPLETES and fills `finished`. With work == timeout, wait_for
            # would cancel everything itself.
            await asyncio.sleep(1)
        except asyncio.CancelledError:
            cancelled.append(rid)
            raise
        finished.append(rid)

    # The helper RAISES: a stop turned into a normal return skips the adapter's
    # handler, and the abandoned worker reports ``completed`` on a row it no
    # longer owns.
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(
            _gather_runs_bounded(
                [f"r{i}" for i in range(6)], 2, _runner, cancel_event=cancel_event
            ),
            timeout=20,
        )

    assert not finished, (
        f"runs completed despite cancellation: {finished} — the batch was not "
        "actually cancelled, or the cancellation was swallowed by "
        "return_exceptions=True"
    )
    assert len(started) < 6, (
        f"every run started ({started}); queued runs must never begin once the "
        "event has fired"
    )
    # The assertions above also hold for a batch returned from without being
    # cancelled: it keeps running in the background and nothing completes.
    assert cancelled, (
        "no run observed CancelledError — the batch was abandoned rather than "
        "cancelled, so its tasks are still running after the helper returned"
    )


@pytest.mark.asyncio
async def test_the_batch_is_unchanged_when_no_event_is_given():
    """Without a cancel event the batch runs as before: the cancellable path is
    inert for every evaluation that keeps its lease.
    """
    import asyncio

    from app.services.evaluation_engine import _gather_runs_bounded

    done: list[str] = []

    async def _runner(rid: str) -> None:
        await asyncio.sleep(0)
        done.append(rid)

    await _gather_runs_bounded([f"r{i}" for i in range(5)], 2, _runner)
    assert sorted(done) == [f"r{i}" for i in range(5)]

    done.clear()
    await _gather_runs_bounded(
        [f"r{i}" for i in range(5)], 2, _runner, cancel_event=asyncio.Event()
    )
    assert sorted(done) == [f"r{i}" for i in range(5)], (
        "an event that never fires must behave exactly as no event at all"
    )


@pytest.mark.asyncio
async def test_the_emergency_stop_interrupts_a_running_evaluation(monkeypatch):
    """The emergency stop interrupts a running evaluation.

    The whole chain with the real pieces: the stop's evaluation arm cancels the
    runs and the execution; the reconciler's watcher fires the event; the
    fan-out interrupts the agent call in flight and raises. The judge is never
    called, and the run keeps what the stop wrote, its open episode closed.

    The event is never handed to a run (it also fires on lease loss):
    the fan-out stops the run from outside.
    """
    from app.services import evaluation_engine as ee
    from app.services import orchestration_reconciler as reconciler
    from app.services import panic

    sm, cleanup = await _make_db_full()
    try:
        execution_id = uuid.uuid4()
        async with sm() as s:
            admin = await _admin(s)
            evaluation = Evaluation(
                name="Eval-stop", family="STIX_BUNDLE", execution_mode="full_loop"
            )
            s.add(evaluation)
            await s.flush()
            agent = Agent(user_id=admin.id, name="Agent-stop")
            ds = Dataset(name="ds-stop", family="STIX_BUNDLE")
            s.add_all([agent, ds])
            await s.flush()
            await seed_provider(s, ["gpt-4.1"])
            s.add_all(
                [
                    DatasetEntry(dataset_id=ds.id, position=i, expected_answer="a")
                    for i in range(2)
                ]
            )
            s.add(EvaluationDataset(evaluation_id=evaluation.id, dataset_id=ds.id))
            s.add(
                OrchestrationExecution(
                    id=execution_id,
                    adapter="evaluation",
                    state="running",
                    payload={},
                    lease_owner="worker-a",
                    lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
                    attempt_count=1,
                )
            )
            await s.flush()
            run = EvaluationRun(
                evaluation_id=evaluation.id,
                agent_id=agent.id,
                model_id="gpt-4.1",
                status="pending",
                trigger_reason="manual",
                owner_execution_id=execution_id,
                total_entries=2,
            )
            s.add(run)
            await s.flush()
            run_id, eval_id = run.id, evaluation.id
            await s.commit()

        in_call = asyncio.Event()
        interrupted: list[bool] = []
        judged: list[str] = []

        async def _agent_call(*_args, **_kwargs):
            in_call.set()
            try:
                await asyncio.sleep(30)  # the model call the stop must interrupt
            except asyncio.CancelledError:
                interrupted.append(True)
                raise
            return "answer", [], None, [], []

        async def _judge(*_args, **_kwargs):
            judged.append("judge")
            return {"score": 100, "sub_scores": {}, "evaluator_reasoning": "ok"}

        async def _no_tools(*_args, **_kwargs):
            return None

        monkeypatch.setattr(ee, "_execute_agent_full_loop", _agent_call)
        monkeypatch.setattr(ee, "score_result", _judge)
        monkeypatch.setattr(ee, "_build_evaluation_integration_tools", _no_tools)

        cancel_event = asyncio.Event()

        async def _evaluate() -> None:
            async with sm() as s:
                await ee.execute_evaluation(
                    s,
                    str(eval_id),
                    "manual",
                    execution_id,
                    cancel_event=cancel_event,
                    worker_id="worker-a",
                )

        task = asyncio.create_task(_evaluate())
        await asyncio.wait_for(in_call.wait(), timeout=15)

        # The emergency button, platform scope: the stop's evaluation arm.
        async with sm() as s:
            outcome = panic.EmergencyStopOutcome(scope="platform")
            await panic._cancel_platform_only(s, now=datetime.now(UTC), outcome=outcome)
            await s.commit()
        assert outcome.evaluation_runs_cancelled == 1

        # The watcher the evaluation adapter opts into reads the execution row.
        await asyncio.wait_for(
            reconciler._watch_execution_cancel(execution_id, cancel_event, 0.01),
            timeout=5,
        )
        assert cancel_event.is_set()

        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=15)

        assert interrupted == [True], "the agent call in flight was not interrupted"
        assert judged == [], "the judge was called after the emergency stop"

        async with sm() as s:
            after = await s.get(EvaluationRun, run_id)
            assert after.status == "cancelled"
            assert after.error == panic.STOP_MESSAGE
            episodes = (
                await s.execute(
                    select(EvaluationEpisode).where(EvaluationEpisode.run_id == run_id)
                )
            ).scalars().all()
            assert episodes, "the run never opened the entry it was stopped in"
            assert {e.status for e in episodes} == {"failed"}
            assert {e.error for e in episodes} == {panic.STOP_MESSAGE}
            # The interrupted entry leaves no half-written result: the
            # replacement cannot tell one from work genuinely done.
            results = (
                await s.execute(
                    select(EvaluationResult).where(EvaluationResult.run_id == run_id)
                )
            ).scalars().all()
            assert results == []
    finally:
        await cleanup()


async def _backend_pid(session) -> int:
    from sqlalchemy import text

    return (await session.execute(text("SELECT pg_backend_pid()"))).scalar_one()


async def _queued_on_a_lock(observer, pid: int) -> bool:
    """Whether backend *pid* comes to wait on a lock another transaction holds."""
    from sqlalchemy import text

    for _ in range(150):
        waiting = (
            await observer.execute(
                text("SELECT count(*) FROM pg_locks WHERE pid = :pid AND NOT granted"), {"pid": pid}
            )
        ).scalar_one()
        if waiting:
            return True
        await asyncio.sleep(0.1)
    return False


async def _has_written(observer, pid: int, table: str) -> bool:
    """Whether backend *pid*'s open transaction holds a write lock on *table*."""
    from sqlalchemy import text

    return bool(
        (
            await observer.execute(
                text(
                    "SELECT count(*) FROM pg_locks WHERE pid = :pid AND granted "
                    "AND locktype = 'relation' AND mode = 'RowExclusiveLock' "
                    "AND relation = CAST(:table AS regclass)"
                ),
                {"pid": pid, "table": table},
            )
        ).scalar_one()
    )


async def _seed_a_running_evaluation(sm) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """A running evaluation run under its evaluation execution, beside an autonomous
    execution the same stop arm cancels. Returns ``(admin_id, run_id, execution_id)``."""
    execution_id = uuid.uuid4()
    async with sm() as s:
        admin = await _admin(s)
        evaluation = Evaluation(name="Eval-lock-order", family="STIX_BUNDLE")
        agent = Agent(user_id=admin.id, name="Agent-lock-order")
        s.add_all([evaluation, agent])
        for id_, adapter in ((execution_id, "evaluation"), (uuid.uuid4(), "autonomous_pentest")):
            s.add(
                OrchestrationExecution(
                    id=id_,
                    adapter=adapter,
                    state="running",
                    payload={},
                    lease_owner="worker-a",
                    lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
                    attempt_count=1,
                )
            )
        await s.flush()
        run = EvaluationRun(
            evaluation_id=evaluation.id,
            agent_id=agent.id,
            model_id="gpt-4.1",
            status="running",
            trigger_reason="manual",
            owner_execution_id=execution_id,
            total_entries=1,
        )
        s.add(run)
        await s.commit()
        return admin.id, run.id, execution_id


async def _press_the_platform_stop(sm, admin_id: uuid.UUID, pid: asyncio.Future):
    """``emergency_stop`` as an operator presses it, every arm in its order. *pid*
    receives the stop's backend pid first, so the caller can watch its locks."""
    from unittest.mock import AsyncMock, patch

    from app.models.user import User
    from app.services import chat_turn_guard, panic

    async with sm() as stopper:
        pid.set_result(await _backend_pid(stopper))
        with (
            patch.object(chat_turn_guard, "list_processing_conversations", AsyncMock(return_value=[])),
            patch.object(chat_turn_guard, "request_turn_cancel", AsyncMock(return_value=True)),
        ):
            actor = await stopper.get(User, admin_id)
            outcome = await panic.emergency_stop(stopper, actor=actor, scope="platform")
        await stopper.commit()
        return outcome


@pytest.mark.asyncio
async def test_the_emergency_stop_waits_for_a_fenced_worker_instead_of_deadlocking_with_it():
    """The stop takes an evaluation's execution before its runs, the worker's order.

    A worker past ``_fence_on_lease`` holds its execution and is about to write
    its runs. The stop must queue behind that execution holding no run, so the
    worker's write commits, and the stop then cancels the executions and the run.
    The other order deadlocks: the stop holds the run the worker writes next.
    """
    from sqlalchemy import update

    from app.services import evaluation_engine as ee
    from app.services import panic

    sm, cleanup = await _make_db_full()
    try:
        admin_id, run_id, execution_id = await _seed_a_running_evaluation(sm)
        async with sm() as worker, sm() as observer:
            await ee._fence_on_lease(worker, execution_id, "worker-a", "the lock-order test")
            stop_pid = asyncio.get_running_loop().create_future()
            stopping = asyncio.create_task(_press_the_platform_stop(sm, admin_id, stop_pid))
            pid = await asyncio.wait_for(stop_pid, 30)
            queued = await _queued_on_a_lock(observer, pid)
            wrote_runs_first = await _has_written(observer, pid, "evaluation_runs")
            worker_error = None
            try:
                await worker.execute(
                    update(EvaluationRun).where(EvaluationRun.id == run_id).values(error="written by the worker")
                )
                await worker.commit()
            except Exception as exc:  # the deadlock, aborted on this side
                worker_error = exc
                await worker.rollback()
            (outcome,) = await asyncio.wait_for(asyncio.gather(stopping, return_exceptions=True), 30)

        assert queued, "the stop never waited for the execution the worker holds"
        assert not wrote_runs_first, "the stop wrote the runs before it had their execution"
        assert worker_error is None, f"the worker's run write failed: {worker_error!r}"
        assert not isinstance(outcome, BaseException), f"the stop failed: {outcome!r}"
        assert (
            outcome.executions_cancelled,
            outcome.autonomous_runs_cancelled,
            outcome.evaluation_runs_cancelled,
        ) == (2, 1, 1)
        async with sm() as s:
            after = await s.get(EvaluationRun, run_id)
            execution = await s.get(OrchestrationExecution, execution_id)
        assert (after.status, after.error) == ("cancelled", panic.STOP_MESSAGE)
        assert execution.state == "cancelled"
    finally:
        await cleanup()


@pytest.mark.asyncio
async def test_the_emergency_stop_leaves_a_run_the_worker_completes_meanwhile():
    """A run a worker completes while the stop runs keeps its result.

    The worker's end-of-entry commit writes the run without its execution's lock,
    so the stop can reach the run while that commit is open. Locked as it reads
    it, the stop waits, PostgreSQL re-checks the status, and the completed run
    drops out instead of being overwritten as cancelled.
    """
    from sqlalchemy import update

    sm, cleanup = await _make_db_full()
    try:
        admin_id, run_id, _execution_id = await _seed_a_running_evaluation(sm)
        async with sm() as worker, sm() as observer:
            await worker.execute(update(EvaluationRun).where(EvaluationRun.id == run_id).values(status="completed"))
            stop_pid = asyncio.get_running_loop().create_future()
            stopping = asyncio.create_task(_press_the_platform_stop(sm, admin_id, stop_pid))
            queued = await _queued_on_a_lock(observer, await asyncio.wait_for(stop_pid, 30))
            await worker.commit()
            outcome = await asyncio.wait_for(stopping, 30)

        assert queued, "the stop never waited for the run the worker is completing"
        assert outcome.evaluation_runs_cancelled == 0
        async with sm() as s:
            after = await s.get(EvaluationRun, run_id)
        assert (after.status, after.error) == ("completed", None)
    finally:
        await cleanup()


@pytest.mark.asyncio
async def test_a_callback_after_the_emergency_stop_is_discarded(monkeypatch):
    """A run waiting on its callback when the stop lands is not judged later.

    The stop closes the run's open episodes, as cancelling one run does:
    a callback arriving afterwards is refused (DISCARDED), and the
    judge is never called on a run the stop ended.
    """
    from app.services import evaluation_continuation as cont
    from app.services import evaluation_engine as ee
    from app.services import panic

    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            admin = await _admin(s)
            evaluation = Evaluation(name="Eval-late", family="STIX_BUNDLE")
            s.add(evaluation)
            await s.flush()
            agent = Agent(user_id=admin.id, name="Agent-late")
            ds = Dataset(name="ds-late", family="STIX_BUNDLE")
            s.add_all([agent, ds])
            await s.flush()
            entry = DatasetEntry(dataset_id=ds.id, position=0, expected_answer="a")
            s.add(entry)
            await s.flush()
            run = EvaluationRun(
                evaluation_id=evaluation.id,
                agent_id=agent.id,
                model_id="gpt-4.1",
                status="running",
                trigger_reason="manual",
                total_entries=1,
                started_at=datetime.now(UTC),
            )
            s.add(run)
            await s.flush()
            now = datetime.now(UTC)
            s.add(
                EvaluationEpisode(
                    run_id=run.id,
                    entry_id=entry.id,
                    correlation_id="corr-late",
                    status="waiting_callback",
                    waiting_since=now,
                    callback_deadline=now + timedelta(hours=1),
                )
            )
            run_id = run.id
            await s.commit()

        async with sm() as s:
            await panic._cancel_platform_only(
                s, now=datetime.now(UTC), outcome=panic.EmergencyStopOutcome(scope="platform")
            )
            await s.commit()

        judged: list[str] = []

        async def _judge(*_args, **_kwargs):
            judged.append("judge")
            return {"score": 100, "sub_scores": {}, "evaluator_reasoning": "ok"}

        monkeypatch.setattr(ee, "score_result", _judge)

        async with sm() as s:
            _episode, result = await cont.ConversationContinuationService.resume_from_callback(
                s, correlation_id="corr-late", idempotency_key="late-1", payload={"answer": "a"}
            )
            await s.commit()

        assert result == cont.CallbackOutcome.DISCARDED
        assert judged == []
        async with sm() as s:
            after = await s.get(EvaluationRun, run_id)
            assert after.status == "cancelled"
    finally:
        await cleanup()


async def _seed_running_runs_with_episodes(sm, admin_id: uuid.UUID, n_runs: int) -> dict[str, list[uuid.UUID]]:
    """*n_runs* running runs, each with a parked, a running and a finished episode,
    beside a completed run that still has an open episode. Returns the episode ids
    by what the stop should do with them: ``open`` closed, ``kept`` left alone."""
    tag = uuid.uuid4().hex[:8]
    now = datetime.now(UTC)
    async with sm() as s:
        evaluation = Evaluation(name=f"Eval-bulk-{tag}", family="STIX_BUNDLE")
        agent = Agent(user_id=admin_id, name=f"Agent-bulk-{tag}")
        ds = Dataset(name=f"ds-bulk-{tag}", family="STIX_BUNDLE")
        s.add_all([evaluation, agent, ds])
        await s.flush()
        entries = [DatasetEntry(dataset_id=ds.id, position=i, expected_answer="a") for i in range(3)]
        s.add_all(entries)
        await s.flush()
        ids: dict[str, list[uuid.UUID]] = {"open": [], "kept": []}
        for status in ["running"] * n_runs + ["completed"]:
            run = EvaluationRun(
                evaluation_id=evaluation.id,
                agent_id=agent.id,
                model_id="gpt-4.1",
                status=status,
                trigger_reason="manual",
                total_entries=3,
                started_at=now,
            )
            s.add(run)
            await s.flush()
            for entry, episode_status in zip(entries, ("waiting_callback", "running", "completed"), strict=True):
                parked = episode_status == "waiting_callback"
                episode = EvaluationEpisode(
                    run_id=run.id,
                    entry_id=entry.id,
                    correlation_id=f"corr-{uuid.uuid4().hex}",
                    status=episode_status,
                    waiting_since=now if parked else None,
                    callback_deadline=now + timedelta(hours=1) if parked else None,
                )
                s.add(episode)
                await s.flush()
                closes = status == "running" and episode_status != "completed"
                ids["open" if closes else "kept"].append(episode.id)
        await s.commit()
        return ids


async def _stop_counting_statements(sm) -> tuple[int, int]:
    """The platform-only arm of the stop, committed. Returns
    ``(runs cancelled, statements it issued)``."""
    from sqlalchemy import event

    from app.services import panic

    statements: list[str] = []

    def _record(_conn, _cursor, statement, *_args):
        statements.append(statement)

    async with sm() as s:
        sync_engine = s.get_bind()
        outcome = panic.EmergencyStopOutcome(scope="platform")
        event.listen(sync_engine, "before_cursor_execute", _record)
        try:
            await panic._cancel_platform_only(s, now=datetime.now(UTC), outcome=outcome)
        finally:
            event.remove(sync_engine, "before_cursor_execute", _record)
        await s.commit()
    return outcome.evaluation_runs_cancelled, len(statements)


@pytest.mark.asyncio
async def test_the_emergency_stop_closes_every_stopped_runs_episodes_in_a_constant_number_of_statements():
    """Stopping four runs costs the statements stopping one does, and closes the
    open episodes of every stopped run, and only those."""
    from app.services import panic

    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            admin_id = (await _admin(s)).id
        one = await _seed_running_runs_with_episodes(sm, admin_id, 1)
        stopped_one, statements_for_one = await _stop_counting_statements(sm)
        four = await _seed_running_runs_with_episodes(sm, admin_id, 4)
        stopped_four, statements_for_four = await _stop_counting_statements(sm)

        assert (stopped_one, stopped_four) == (1, 4)
        assert statements_for_four == statements_for_one
        async with sm() as s:
            episodes = {
                e.id: e
                for e in (
                    await s.execute(select(EvaluationEpisode).where(
                        EvaluationEpisode.id.in_(one["open"] + one["kept"] + four["open"] + four["kept"])
                    ))
                ).scalars()
            }
        closed = [episodes[i] for i in one["open"] + four["open"]]
        assert len(closed) == 10
        assert {(e.status, e.error, e.waiting_since, e.callback_deadline) for e in closed} == {
            ("failed", panic.STOP_MESSAGE, None, None)
        }
        assert all(e.completed_at is not None for e in closed)
        kept = [episodes[i] for i in one["kept"] + four["kept"]]
        # Per batch: each stopped run's finished episode, and the completed
        # run's three, open one included.
        assert sorted(e.status for e in kept) == sorted(
            ["completed"] * 7 + ["waiting_callback", "running"] * 2
        )
        assert all(e.error is None for e in kept)
    finally:
        await cleanup()


@pytest.mark.asyncio
async def test_closing_the_episodes_of_more_runs_than_the_driver_takes_parameters():
    """The close takes any number of runs: past the driver's 32,767 query
    parameters, a stop over that many runs would fail and roll back whole."""
    from app.services.evaluation_continuation import close_episodes_for_cancelled_runs

    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            closed = await close_episodes_for_cancelled_runs(
                s, run_ids=[uuid.uuid4() for _ in range(40_000)], error="stopped"
            )
            await s.rollback()
        assert closed == 0
    finally:
        await cleanup()


@pytest.mark.asyncio
async def test_the_reclaim_will_not_commit_once_the_lease_has_moved():
    """The reclaim's write never commits once the lease has moved.

    The reclaim marks runs ``failed``. An event check before ``db.commit()``
    lets the heartbeat fire in between, failing runs the replacement is
    running, so the lease is read ``FOR UPDATE`` in the same transaction. The
    lease moves here with NO event fired, the case an event check cannot see.
    """
    from app.services import evaluation_engine as ee

    sm, cleanup = await _make_db()
    try:
        execution_id = uuid.uuid4()
        async with sm() as s:
            admin = await _admin(s)
            evaluation = Evaluation(name="Eval-fence", family="STIX_BUNDLE")
            s.add(evaluation)
            await s.flush()
            agent = Agent(user_id=admin.id, name="Agent-fence")
            ds = Dataset(name="ds-fence", family="STIX_BUNDLE")
            s.add_all([agent, ds])
            await s.flush()
            s.add(DatasetEntry(dataset_id=ds.id, position=0, expected_answer="a"))
            s.add(EvaluationDataset(evaluation_id=evaluation.id, dataset_id=ds.id))
            s.add(
                OrchestrationExecution(
                    id=execution_id,
                    adapter="evaluation",
                    state="running",
                    payload={},
                    # Already the replacement's, as the reclaim is about to run.
                    lease_owner="worker-b",
                    attempt_count=2,
                )
            )
            await s.flush()
            abandoned = EvaluationRun(
                evaluation_id=evaluation.id,
                agent_id=agent.id,
                model_id="gpt-4.1",
                status="running",
                trigger_reason="manual",
                owner_execution_id=execution_id,
                total_entries=1,
                started_at=datetime.now(UTC),
            )
            s.add(abandoned)
            await s.flush()
            run_id, eval_id = abandoned.id, evaluation.id
            await s.commit()

        # No event is fired: the fence does not need one. LeaseLostError, not
        # CancelledError: the fence fires when the event is NOT set, so a
        # bare CancelledError would read as a genuine shutdown to every
        # classifier and reach _on_done, which returns silently on a cancelled
        # task.
        from app.services.orchestration_engine import LeaseLostError

        async with sm() as s:
            with pytest.raises(LeaseLostError):
                await ee.execute_evaluation(
                    s, str(eval_id), "manual", execution_id, worker_id="worker-a"
                )

        async with sm() as s:
            after = await s.get(EvaluationRun, run_id)
            assert after.status == "running", (
                f"the abandoned worker committed {after.status!r} on a run the "
                "replacement owns — the reclaim was checked, not fenced"
            )
            assert after.error is None, (
                f"an abandonment error was written by a worker that had already "
                f"lost the lease: {after.error!r}"
            )
    finally:
        await cleanup()


@pytest.mark.asyncio
async def test_an_abandoned_worker_cannot_complete_the_replacements_execution():
    """An abandoned worker cannot complete the replacement's execution.

    ``engine.complete``/``fail`` re-read the execution ``FOR UPDATE`` and check
    the caller's lease. The reconciler loads the execution BEFORE the adapter
    work and commits with ``expire_on_commit=False``, so without
    ``populate_existing`` the re-read hands back the CACHED object with the
    abandoned worker's ``lease_owner``, and the predicate passes. The fake
    session in ``test_orchestration_engine.py`` models no identity map, so this
    drives the real ORM against a real database.
    """
    from app.services import orchestration_engine as engine

    sm, cleanup = await _make_db()
    try:
        execution_id = uuid.uuid4()
        async with sm() as s:
            s.add(
                OrchestrationExecution(
                    id=execution_id,
                    adapter="evaluation",
                    state="running",
                    payload={},
                    lease_owner="worker-a",
                    attempt_count=1,
                )
            )
            await s.commit()

        # Worker A's session loads the row, then releases its transaction, as
        # _do_execute does before handing off to the adapter.
        session_a = sm()
        loaded = await engine.get_execution(session_a, execution_id)
        assert loaded.lease_owner == "worker-a"
        await session_a.commit()

        # The lease moves to worker B, committed from another session.
        async with sm() as s:
            row = await s.get(OrchestrationExecution, execution_id)
            row.lease_owner = "worker-b"
            await s.commit()

        # Worker A records a terminal state the way it would: ``_apply_result``
        # with the ``completed`` result an adapter returns on a stop, not
        # ``engine.complete`` directly, so the seam is the real one.
        from app.services import orchestration_reconciler as reconciler
        from app.services.orchestration_adapters.base import AdapterResult

        await reconciler._apply_result(
            session_a, execution_id, "worker-a", AdapterResult.completed()
        )
        await session_a.close()

        async with sm() as s:
            after = await s.get(OrchestrationExecution, execution_id)
            assert after.state == "running", (
                f"the abandoned worker drove the execution to {after.state!r} — "
                "the replacement's work is now orphaned"
            )
            assert after.lease_owner == "worker-b", (
                f"the abandoned worker cleared the replacement's lease "
                f"(owner is now {after.lease_owner!r})"
            )
    finally:
        await cleanup()


@pytest.mark.asyncio
async def test_an_abandoned_run_is_failed_re_runnable_and_keeps_its_entries():
    """An abandoned run is failed, re-runnable, and keeps its committed entries.

    Dispatch selects ``status == "pending"``, and the reclaim sets an abandoned
    run to ``"failed"`` with "re-run to retry". No replacement finishes it, on
    purpose: reuse skips the agent call but scorers, the judge included, always
    re-run, so re-dispatching would pay the judge twice. A human re-run is the
    decision to pay again. Through the real reclaim:

    * the run reaches a TERMINAL status, never wedged at "running";
    * the entries the abandoned worker committed SURVIVE, so a re-run can
      reuse them.
    """
    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            admin = await _admin(s)
            evaluation = Evaluation(name="Eval-handover", family="STIX_BUNDLE")
            s.add(evaluation)
            await s.flush()
            agent = Agent(user_id=admin.id, name="Agent-handover")
            ds = Dataset(name="ds-handover", family="STIX_BUNDLE")
            s.add_all([agent, ds])
            await s.flush()
            entries = [
                DatasetEntry(dataset_id=ds.id, position=i, expected_answer="a")
                for i in range(3)
            ]
            s.add_all(entries)
            await s.flush()
            execution_id = uuid.uuid4()
            run = EvaluationRun(
                evaluation_id=evaluation.id,
                agent_id=agent.id,
                model_id="gpt-4.1",
                status="running",
                trigger_reason="manual",
                total_entries=3,
                owner_execution_id=execution_id,
                started_at=datetime.now(UTC),
            )
            s.add(run)
            await s.flush()
            run_id, entry_ids = run.id, [e.id for e in entries]
            eval_id = evaluation.id
            # The execution the reclaim is fenced against, held by the
            # replacement that is about to run it.
            s.add(
                OrchestrationExecution(
                    id=execution_id,
                    adapter="evaluation",
                    state="running",
                    payload={},
                    lease_owner="worker-b",
                    attempt_count=2,
                )
            )
            await s.commit()

        # Worker A commits one entry, then its lease moves and it stops.
        async with sm() as s:
            s.add(
                EvaluationResult(
                    run_id=run_id,
                    entry_id=entry_ids[0],
                    agent_output="by worker A",
                    score=70,
                    sub_scores={},
                    evaluator_reasoning="r",
                    duration_seconds=1,
                )
            )
            await s.commit()

        # Worker B retries the SAME execution -- the production path, with no
        # stub standing in for the reclaim.
        from app.services import evaluation_engine as ee

        async with sm() as s:
            # Goes through the fence, not around it: the fence protects the
            # reclaim this test is about.
            await ee.execute_evaluation(
                s, str(eval_id), "manual", execution_id=execution_id,
                worker_id="worker-b",
            )

        async with sm() as s:
            run_after = (
                await s.execute(
                    select(EvaluationRun).where(EvaluationRun.id == run_id)
                )
            ).scalar_one()
            assert run_after.status != "running", (
                "the run was left at 'running' with nothing able to move it — "
                "that is the wedged state the sweep must never leave"
            )
            assert run_after.status == "failed", (
                f"expected the abandoned run to be failed and re-runnable, got "
                f"{run_after.status!r}"
            )
            assert "re-run" in (run_after.error or ""), (
                "the run was failed without telling the operator it can be "
                f"re-run: {run_after.error!r}"
            )

            rows = (
                await s.execute(
                    select(EvaluationResult).where(EvaluationResult.run_id == run_id)
                )
            ).scalars().all()
            assert len(rows) == 1, (
                f"expected worker A's single committed entry to survive, found "
                f"{len(rows)} — work already paid for stays valid"
            )
            assert rows[0].agent_output == "by worker A", (
                "the committed entry was overwritten; a re-run can no longer "
                "reuse it and will pay for it a second time"
            )
    finally:
        await cleanup()


@pytest.mark.asyncio
async def test_an_interrupted_rescore_leaves_the_existing_scores_alone(monkeypatch):
    """A rescore stopped before its score commit leaves every existing score as it
    was and the run ``"running"``.

    A re-score REPLACES existing scores, so a partial write corrupts a result the
    user already had. It commits once, after the last entry, so a stop between
    two judge calls rolls back the entries already re-judged. The run stays
    ``"running"``: nothing was written, and the retry has to redo it.
    """
    import asyncio

    from app.services import evaluation_engine as ee

    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            admin = await _admin(s)
            evaluation = Evaluation(name="Eval-rs-cancel", family="STIX_BUNDLE")
            s.add(evaluation)
            await s.flush()
            agent = Agent(user_id=admin.id, name="Agent-rs-cancel")
            ds = Dataset(name="ds-rs-cancel", family="STIX_BUNDLE")
            s.add_all([agent, ds])
            await s.flush()
            entries = [
                DatasetEntry(dataset_id=ds.id, position=i, expected_answer="a")
                for i in range(2)
            ]
            s.add_all(entries)
            await s.flush()
            finished = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
            run = EvaluationRun(
                evaluation_id=evaluation.id,
                agent_id=agent.id,
                model_id="gpt-4.1",
                # A finished run a rescore flipped back to "running".
                status="running",
                trigger_reason="manual",
                total_entries=2,
                completed_entries=2,
                completed_at=finished,
                score=42.0,
                scorer_selection=["BEFORE"],
            )
            s.add(run)
            await s.flush()
            s.add_all(
                [
                    EvaluationResult(
                        run_id=run.id,
                        entry_id=entry.id,
                        agent_output="original",
                        score=42,
                        sub_scores={},
                        evaluator_reasoning="original",
                        duration_seconds=1,
                    )
                    for entry in entries
                ]
            )
            await s.commit()
            run_id = run.id

        judged: list[int] = []
        in_second_entry = asyncio.Event()

        async def _judge(*_a, **_k):
            judged.append(len(judged))
            if len(judged) == 2:
                # Entry 1 is re-judged in the session; the stop lands here.
                in_second_entry.set()
                await asyncio.sleep(3600)
            return {"score": 99, "sub_scores": {}, "evaluator_reasoning": "rescored"}

        monkeypatch.setattr(ee, "score_result", _judge)

        task = asyncio.create_task(
            ee._rescore_run_with_own_session(
                str(run_id), previous_status="completed"
            )
        )
        await asyncio.wait_for(in_second_entry.wait(), timeout=30)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

        assert len(judged) == 2, "the stop did not land between two judge calls"
        async with sm() as s:
            results = (
                await s.execute(
                    select(EvaluationResult).where(EvaluationResult.run_id == run_id)
                )
            ).scalars().all()
            assert len(results) == 2
            for after in results:
                assert after.score == 42, (
                    f"the interrupted re-score partially overwrote an existing "
                    f"score (now {after.score}) — it must roll back whole"
                )
                assert after.evaluator_reasoning == "original"
            run_after = await s.get(EvaluationRun, run_id)
            assert run_after.status == "running", (
                "nothing was committed, so the retry must still find the run "
                "to rescore"
            )
            assert run_after.scorer_selection == ["BEFORE"]
            assert run_after.completed_at == finished
    finally:
        await cleanup()


@pytest.mark.asyncio
async def test_the_batch_end_aggregate_write_is_skipped_once_the_lease_moves(
    monkeypatch,
):
    """The batch-end aggregate write is skipped once the lease moves.

    ``_gather_runs_bounded`` observes the event only while awaiting runners. The
    headline recompute and shared performance basis after it mutate
    EVALUATION-scoped aggregates with no lease re-read, so an abandoned worker
    would overwrite headlines the replacement is recomputing. The fan-out here
    returns NORMALLY with the event set, as when the lease is lost after the
    last run's commit.
    """
    import asyncio

    from app.services import evaluation_engine as ee

    cancel_event = asyncio.Event()
    aggregate_writes: list[str] = []

    async def _gather_returns_normally(*_a, **_k):
        cancel_event.set()  # the lease moves as the last runner finishes

    async def _record(*_a, **_k):
        aggregate_writes.append("shared_performance")
        return True

    monkeypatch.setattr(ee, "_gather_runs_bounded", _gather_returns_normally)
    monkeypatch.setattr(ee, "_batch_has_a_completed_run", _record)
    monkeypatch.setattr(ee, "_recompute_shared_performance_basis", _record)

    with pytest.raises(asyncio.CancelledError):
        await ee.execute_rescore_runs(
            ["r0", "r1"],
            cancel_event=cancel_event,
            previous_statuses={"r0": "completed", "r1": "completed"},
        )

    assert aggregate_writes == [], (
        f"the abandoned worker wrote {aggregate_writes} after losing the "
        "lease — the aggregate write is the correctness one, not the latency one"
    )


@pytest.mark.asyncio
async def test_the_batch_end_headline_write_is_a_repeatable_recompute():
    """The batch-end headline write is a repeatable recompute.

    The lease can still move between the batch-end guard and the awaited write;
    racing the write bounds that to the stop signal's latency but is not atomic.
    The residual is harmless because the headlines are recomputed from stored
    scores: a late worker writes the SAME values. Write fencing would close the
    gap.

    Fails if anything non-repeatable enters the headline path: a timestamp, a
    counter, a run-time-dependent ordering, or a basis that narrows on each call.
    """
    from app.services import evaluation_engine as ee

    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            admin = await _admin(s)
            evaluation = Evaluation(name="Eval-idem", family="STIX_BUNDLE")
            s.add(evaluation)
            await s.flush()
            agent = Agent(user_id=admin.id, name="Agent-idem")
            ds = Dataset(name="ds-idem", family="STIX_BUNDLE")
            s.add_all([agent, ds])
            await s.flush()
            entries = [
                DatasetEntry(dataset_id=ds.id, position=i, expected_answer="a")
                for i in range(3)
            ]
            s.add_all(entries)
            await s.flush()
            # TWO completed runs: performance is COMPARATIVE, and with one run
            # the evaluation-wide writer has no basis and never runs.
            runs = []
            for r in range(2):
                run = EvaluationRun(
                    evaluation_id=evaluation.id,
                    agent_id=agent.id,
                    model_id=f"gpt-4.1-{r}",
                    status="completed",
                    trigger_reason="manual",
                    total_entries=3,
                    completed_entries=3,
                )
                s.add(run)
                await s.flush()
                runs.append(run)
                for i, entry in enumerate(entries):
                    res = EvaluationResult(
                        run_id=run.id,
                        entry_id=entry.id,
                        agent_output="out",
                        score=60 + i * 10 + r * 5,
                        f1_score=0.5 + i * 0.1,
                        sub_scores={},
                        evaluator_reasoning="r",
                        duration_seconds=1 + i + r * 3,
                    )
                    s.add(res)
                    await s.flush()
                    # Real performance metrics, different per run, so the
                    # comparative pass writes performance_score instead of
                    # collapsing to a tie.
                    for key, base in (
                        ("performance.latency.wall_clock", 1000.0),
                        ("performance.cost.llm", 0.02),
                    ):
                        s.add(
                            EvaluationMetric(
                                run_id=run.id,
                                result_id=res.id,
                                key=key,
                                value=base * (1 + i) * (1 + r),
                            )
                        )
            await s.commit()
            run_ids = [r.id for r in runs]
            eval_id = evaluation.id

        async def _snapshot():
            async with sm() as s:
                out = []
                for rid in run_ids:
                    row = await s.get(EvaluationRun, rid)
                    out.append(
                        (
                            row.score,
                            row.quality_score,
                            row.f1_score,
                            # The EVALUATION-wide comparative write, the one a
                            # late writer corrupts most visibly. Its provenance
                            # too: the value can coincide while the basis changed.
                            row.performance_score,
                            (row.headline_details or {}).get("performance"),
                        )
                    )
                return out

        # Worker A's pass: the one that holds the lease.
        async with sm() as s:
            await ee._compute_run_headlines(
                s, eval_id, batch_run_ids=[str(r) for r in run_ids]
            )
        first = await _snapshot()
        assert any(h[3] is not None for h in first), (
            "no performance_score was written, so this test would prove "
            "repeatability of the one aggregate it was written to cover"
        )

        # Worker B's pass over the same scores, then A's late write: neither
        # moves the numbers.
        for _ in range(2):
            async with sm() as s:
                await ee._compute_run_headlines(
                    s, eval_id, batch_run_ids=[str(r) for r in run_ids]
                )
            again = await _snapshot()
            assert again == first, (
                "the batch-end headline write is not repeatable: a second pass "
                f"over identical stored scores produced {again}, not {first}. "
                "A worker that lost the lease would therefore overwrite the "
                "replacement's aggregate with DIFFERENT values, and the guard "
                "cannot prevent that —"
            )
    finally:
        await cleanup()


@pytest.mark.asyncio
async def test_an_aggregate_write_never_begins_if_the_lease_is_already_gone(
    monkeypatch,
):
    """An aggregate write never begins if the lease is already gone.

    The pre-checks before ``_run_unless_stopped`` are awaits, so the event can be
    set on entry. Scheduling the write first would START it, and cancelling it
    later is not the same: it may already have committed. Only ZERO invocations
    tells "never started" from "started and cancelled".
    """
    import asyncio

    from app.services import evaluation_engine as ee

    cancel_event = asyncio.Event()
    began: list[str] = []

    async def _gather_returns_normally(*_a, **_k):
        return None

    async def _has_completed(*_a, **_k):
        # The lease moves HERE, after the tail's boundary guard and before the
        # write is scheduled. Earlier, the boundary guard raises and the
        # preflight is never reached.
        await asyncio.sleep(0)
        cancel_event.set()
        return True

    async def _aggregate(*_a, **_k):  # pragma: no cover - must not run
        began.append("shared_performance")

    monkeypatch.setattr(ee, "_gather_runs_bounded", _gather_returns_normally)
    monkeypatch.setattr(ee, "_batch_has_a_completed_run", _has_completed)
    monkeypatch.setattr(ee, "_recompute_shared_performance_basis", _aggregate)

    with pytest.raises(asyncio.CancelledError):
        await ee.execute_rescore_runs(
            ["r0"], cancel_event=cancel_event, previous_statuses={"r0": "completed"}
        )

    assert began == [], (
        "the aggregate write started even though the lease was already gone "
        "when the tail was entered — it must never begin, not begin and be "
        "cancelled"
    )


@pytest.mark.asyncio
async def test_an_aggregate_write_already_running_is_abandoned_mid_flight(
    monkeypatch,
):
    """An aggregate write already running is cancelled when the lease moves.

    ``_stop_if_no_longer_ours`` checks only on the way in, and a large headline
    recompute can outlast the heartbeat interval (``lease_seconds // 3``), so
    the event can be set mid-write. Drives the real ``execute_rescore_runs``
    tail with the shared performance pass in flight.
    """
    import asyncio

    from app.services import evaluation_engine as ee

    cancel_event = asyncio.Event()
    finished: list[str] = []
    entered = asyncio.Event()

    async def _gather_returns_normally(*_a, **_k):
        return None

    async def _has_completed(*_a, **_k):
        return True

    async def _slow_aggregate(*_a, **_k):
        entered.set()
        await asyncio.sleep(30)  # still running when the lease moves
        finished.append("shared_performance")

    async def _move_the_lease():
        await entered.wait()
        cancel_event.set()

    monkeypatch.setattr(ee, "_gather_runs_bounded", _gather_returns_normally)
    monkeypatch.setattr(ee, "_batch_has_a_completed_run", _has_completed)
    monkeypatch.setattr(ee, "_recompute_shared_performance_basis", _slow_aggregate)

    mover = asyncio.create_task(_move_the_lease())
    try:
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(
                ee.execute_rescore_runs(
                    ["r0"],
                    cancel_event=cancel_event,
                    previous_statuses={"r0": "completed"},
                ),
                timeout=20,
            )
    finally:
        mover.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await mover

    assert finished == [], (
        "the aggregate write ran to completion after the lease moved — a "
        "pre-check alone cannot stop a write that is already in flight"
    )


@pytest.mark.asyncio
async def test_a_batch_finishing_as_the_lease_moves_still_stops(monkeypatch):
    """A batch finishing as the lease moves still stops.

    ``stopped`` is ``not batch.done()`` sampled when ``asyncio.wait`` returned,
    so an event firing as the last runner finishes leaves it False. The
    re-check turns that into a stop.
    """
    import asyncio

    from app.services import evaluation_engine as ee

    cancel_event = asyncio.Event()

    async def _runner(_rid: str) -> None:
        # Finish AND fire the event in the same scheduling step.
        cancel_event.set()

    with pytest.raises(asyncio.CancelledError):
        await ee._gather_runs_bounded(
            ["r0"], 2, _runner, cancel_event=cancel_event
        )


@pytest.mark.asyncio
async def test_the_evaluation_batch_end_headline_write_is_skipped_once_the_lease_moves(
    monkeypatch,
):
    """``execute_evaluation`` never starts its batch-end headline write once the
    lease has moved.

    The fan-out here returns NORMALLY with the event set, as when the lease is
    lost after the last run's commit, so only the tail's own re-check can stop
    the write. An abandoned worker would otherwise overwrite headlines the
    replacement is recomputing. The headline fake records a call when it is
    made, so even a call whose coroutine is never awaited is counted.
    """
    from app.services import evaluation_engine as ee

    execution_id = uuid.uuid4()
    cancel_event = asyncio.Event()
    gathered: list[list[str]] = []
    headlines = HeadlinesFake()
    headline_calls = headlines.calls

    async def _gather_returns_normally(run_ids, *_a, **_k):
        gathered.append(list(run_ids))
        cancel_event.set()  # the lease moves as the last runner finishes

    monkeypatch.setattr(ee, "_gather_runs_bounded", _gather_returns_normally)
    monkeypatch.setattr(ee, "_compute_run_headlines", headlines)

    sm, cleanup = await _make_db()
    try:
        eval_id, (run_id,) = await _active_runs_fixture(
            sm, [("pending", execution_id, {})]
        )

        with pytest.raises(asyncio.CancelledError):
            async with sm() as s:
                await ee.execute_evaluation(
                    s,
                    str(eval_id),
                    "manual",
                    execution_id,
                    cancel_event=cancel_event,
                    worker_id=NO_LEASE,
                )

        assert gathered == [[str(run_id)]], "the batch never reached its fan-out"
        assert headline_calls == [], (
            "the abandoned worker began the batch-end headline write after "
            "losing the lease"
        )
    finally:
        await cleanup()


@pytest.mark.asyncio
async def test_a_retry_repairs_headlines_a_stopped_rescore_left_stale(monkeypatch):
    """A retry recomputes the headlines of a stopped rescore it skips, without the
    judge.

    The status flip shares ``rescore_run``'s commit, so a stop in the headline
    pass leaves the run terminal with NEW results and OLD
    ``score``/``quality_score``/``f1_score``. The retry rightly skips the
    judging, already paid for; nothing else would recompute the headlines.
    """
    from app.services import evaluation_engine as ee

    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            admin = await _admin(s)
            evaluation = Evaluation(name="Eval-rs-repair", family="STIX_BUNDLE")
            s.add(evaluation)
            await s.flush()
            agent = Agent(user_id=admin.id, name="Agent-rs-repair")
            ds = Dataset(name="ds-rs-repair", family="STIX_BUNDLE")
            s.add_all([agent, ds])
            await s.flush()
            entry = DatasetEntry(dataset_id=ds.id, position=0, expected_answer="a")
            s.add(entry)
            await s.flush()
            run = EvaluationRun(
                evaluation_id=evaluation.id,
                agent_id=agent.id,
                model_id="gpt-4.1",
                # Terminal already: the shape a stopped rescore leaves behind.
                status="completed",
                trigger_reason="manual",
                total_entries=1,
                completed_entries=1,
                score=42.0,
            )
            s.add(run)
            await s.flush()
            s.add(
                EvaluationResult(
                    run_id=run.id,
                    entry_id=entry.id,
                    agent_output="rescored",
                    score=91,
                    sub_scores={},
                    evaluator_reasoning="r",
                    duration_seconds=1,
                )
            )
            await s.commit()
            run_id = run.id

        headlines = HeadlinesFake()

        def _judge_must_not_run(*_a, **_k):
            raise AssertionError("the retry called the judge on a finished run")

        monkeypatch.setattr(ee, "_compute_run_headlines", headlines)
        monkeypatch.setattr(ee, "score_result", _judge_must_not_run)

        await ee._rescore_run_with_own_session(
            str(run_id), previous_status="completed"
        )
        repaired = [str(c["batch_run_ids"]) for c in headlines.calls]

        assert repaired == [f"['{run_id}']"], (
            "the retry skipped the run without repairing its headlines — the "
            f"stale aggregate survives forever (calls: {repaired})"
        )
    finally:
        await cleanup()


@pytest.mark.asyncio
async def test_a_stopped_rescore_is_never_left_scored_but_still_running(monkeypatch):
    """A stopped rescore is never left with committed scores and a ``"running"``
    status.

    The run's status is the retry's ONLY idempotence key (see
    ``EvaluationAdapter.execute``). Durable scores under a ``"running"`` status
    let a retry re-judge a finished run, re-charging the judge and overwriting
    a committed score. This drives the real ``_rescore_run_with_own_session``
    and stops it after the score commit, in the headline pass, as a timeout, a
    shutdown, a crash or a lost lease can. The invariant: scores durable
    IFF the run is terminal.
    """
    import asyncio
    import contextlib

    from app.services import evaluation_engine as ee

    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            admin = await _admin(s)
            evaluation = Evaluation(
                name="Eval-rs-window", family="STIX_BUNDLE", scorer_selection=["AFTER"]
            )
            s.add(evaluation)
            await s.flush()
            agent = Agent(user_id=admin.id, name="Agent-rs-window")
            ds = Dataset(name="ds-rs-window", family="STIX_BUNDLE")
            s.add_all([agent, ds])
            await s.flush()
            entry = DatasetEntry(dataset_id=ds.id, position=0, expected_answer="a")
            s.add(entry)
            await s.flush()
            run = EvaluationRun(
                evaluation_id=evaluation.id,
                agent_id=agent.id,
                model_id="gpt-4.1",
                status="running",
                trigger_reason="manual",
                total_entries=1,
                completed_entries=1,
                score=42.0,
                scorer_selection=["BEFORE"],
            )
            s.add(run)
            await s.flush()
            s.add(
                EvaluationResult(
                    run_id=run.id,
                    entry_id=entry.id,
                    agent_output="original",
                    score=42,
                    sub_scores={},
                    evaluator_reasoning="original",
                    duration_seconds=1,
                )
            )
            await s.commit()
            run_id = run.id

        in_window = asyncio.Event()

        async def _blocking_headlines(
            db,
            evaluation_id,
            batch_run_ids=None,
            *,
            require_completed=True,
            skip_performance_recompute=False,
            commit=True,
        ):
            # rescore_run calls this right AFTER committing its scores: blocking
            # here puts the cancellation in the window.
            in_window.set()
            await asyncio.sleep(3600)

        monkeypatch.setattr(ee, "_compute_run_headlines", _blocking_headlines)

        task = asyncio.create_task(
            ee._rescore_run_with_own_session(
                str(run_id), previous_status="completed"
            )
        )
        await asyncio.wait_for(in_window.wait(), timeout=30)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

        async with sm() as s:
            after = await s.get(EvaluationRun, run_id)
            # Without the stamp the window was never reached, and the status
            # assertion below would pass vacuously.
            assert after.scorer_selection == ["AFTER"], (
                "the score commit did not land, so this test never entered the "
                f"commit-to-finalize window (stamp={after.scorer_selection!r})"
            )
            assert after.status != "running", (
                "the rescore's scores are committed but the run still reads "
                "'running' — a retry will pass the idempotence gate and "
                "re-judge a run that is already finished"
            )

        # And the retry skips it: what the status is there to produce.
        reached: list[str] = []
        headlines = HeadlinesFake()

        async def _spy(_db, rid, **_kw):
            reached.append(rid)
            raise AssertionError("the retry must not re-enter rescore_run")

        # Replace the blocking stub before the retry: the skip path repairs
        # headlines, so the 1-hour sleep would hang here.
        monkeypatch.setattr(ee, "_compute_run_headlines", headlines)
        monkeypatch.setattr(ee, "rescore_run", _spy)
        await ee._rescore_run_with_own_session(
            str(run_id), previous_status="completed"
        )
        assert reached == [], "the retry re-judged a run whose scores had landed"
        assert len(headlines.calls) == 1, (
            "the retry skipped the run without repairing the headlines the "
            "stop left stale"
        )
    finally:
        await cleanup()


async def test_cancellation_escapes_the_per_entry_exception_handler():
    """Tripwire: nothing in ``evaluation_engine`` catches cancellation and carries on.

    ``execute_evaluation_run`` records an error result per failing entry and
    CONTINUES, so a caught cancellation would write an error for every remaining
    entry instead of stopping. Both spellings are caught: ``CancelledError`` and
    ``asyncio.CancelledError``, and ``BaseException`` or a bare ``except:``.
    """
    import ast

    from tests.app_sources import APP_DIR, tree, walk

    module = tree(APP_DIR / "services" / "evaluation_engine.py")
    offenders: list[str] = []
    for node in walk(module):
        if not isinstance(node, ast.ExceptHandler):
            continue
        if node.type is None:  # bare `except:` catches BaseException too
            offenders.append(f"bare except at line {node.lineno}")
            continue
        names = {
            n.id if isinstance(n, ast.Name) else n.attr
            for n in ast.walk(node.type)
            if isinstance(n, (ast.Name, ast.Attribute))
        }
        # A handler that re-raises does not swallow the stop: it records the
        # spend of a request already billed and lets CancelledError through.
        # What is pinned is that nothing CATCHES the stop and carries on.
        reraises = (
            bool(node.body)
            and isinstance(node.body[-1], ast.Raise)
            and node.body[-1].exc is None
        )
        for bad in ("BaseException", "CancelledError"):
            if bad in names and not reraises:
                offenders.append(f"{bad} at line {node.lineno}")

    assert not offenders, (
        "evaluation_engine catches cancellation, so a lost lease would be "
        f"recorded as a per-entry failure instead of stopping: {offenders}"
    )


@pytest.mark.asyncio
async def test_the_real_sequential_loop_raises_rather_than_breaking(monkeypatch):
    """The real sequential loop RAISES on a stop rather than breaking.

    The adapter-level sequential test stubs ``execute_evaluation`` with its own
    loop, so this drives the real one with ``run_mode="sequential"``. A
    ``break`` returns normally: the batch-end headline write runs and the
    adapter reports ``completed`` on a row the replacement owns.

    The event fires from INSIDE the first runner: set before the call, the
    pre-dispatch guard raises at the top of ``execute_evaluation`` and the
    sequential dispatch is never reached.
    """
    from app.services import evaluation_engine as ee

    execution_id = uuid.uuid4()
    sm, cleanup = await _make_db()
    try:
        async with sm() as s:
            admin = await _admin(s)
            evaluation = Evaluation(
                name="Eval-sequential",
                family="STIX_BUNDLE",
                run_mode="sequential",
            )
            s.add(evaluation)
            await s.flush()
            agent = Agent(user_id=admin.id, name="Agent-sequential")
            ds = Dataset(name="ds-sequential", family="STIX_BUNDLE")
            s.add_all([agent, ds])
            await s.flush()
            s.add(DatasetEntry(dataset_id=ds.id, position=0, expected_answer="a"))
            s.add(EvaluationDataset(evaluation_id=evaluation.id, dataset_id=ds.id))
            await s.flush()
            # Several runs, so "the queued ones never begin" is observable: with
            # one there is nothing left to refuse.
            expected_runs = 4
            for _ in range(expected_runs):
                s.add(
                    EvaluationRun(
                        evaluation_id=evaluation.id,
                        agent_id=agent.id,
                        model_id="gpt-4.1",
                        status="pending",
                        trigger_reason="manual",
                        owner_execution_id=execution_id,
                        total_entries=1,
                    )
                )
            await s.commit()
            eval_id = evaluation.id

        ran: list[str] = []

        cancel_event = asyncio.Event()

        async def _runner(rid: str) -> None:
            # The lease moves while the FIRST run is in flight, so the stop has
            # to come from the sequential dispatch itself.
            ran.append(rid)
            cancel_event.set()
            await asyncio.sleep(0.05)

        monkeypatch.setattr(ee, "_execute_run_with_own_session", _runner)

        async with sm() as s:
            with pytest.raises(asyncio.CancelledError):
                await ee.execute_evaluation(
                    s, str(eval_id), "manual", execution_id,
                    cancel_event=cancel_event,
                    # The stop under test is the event, not the lease.
                    worker_id=NO_LEASE,
                )

        assert 0 < len(ran) < expected_runs, (
            f"{len(ran)} of {expected_runs} runs started. Zero means the stop "
            "came from the pre-dispatch guard and the sequential fan-out was "
            "never reached; all four means the stop never happened."
        )
    finally:
        await cleanup()


@pytest.mark.asyncio
async def test_an_already_fired_event_starts_no_run_at_all():
    """An event fired before dispatch starts no run at all.

    Asserts ZERO invocations, which tells "never started" from "started and
    cancelled". It pins the guarantee, not the pre-dispatch guard: without the
    guard no runner enters either, because ``asyncio.wait`` returns on a
    completed waiter before the gather's children get a turn.
    """
    from app.services.evaluation_engine import _gather_runs_bounded

    entered: list[str] = []

    async def _runner(rid: str) -> None:
        entered.append(rid)
        await asyncio.sleep(0)

    cancel_event = asyncio.Event()
    cancel_event.set()  # already lost before dispatch begins

    with pytest.raises(asyncio.CancelledError):
        await _gather_runs_bounded(
            [f"r{i}" for i in range(5)], 2, _runner, cancel_event=cancel_event
        )

    assert entered == [], (
        f"{len(entered)} run(s) started despite the lease already having moved: "
        f"{entered} — the event must be checked before the batch is scheduled"
    )
