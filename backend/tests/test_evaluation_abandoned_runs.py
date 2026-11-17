"""Runs abandoned by a dead worker, against a real (throwaway) PostgreSQL database.

The retry's reclaim in ``execute_evaluation``, the evaluation adapter's dead-letter
handling, and ``fail_orphaned_episodes`` repairing the episodes those runs left
non-terminal, never touching a run or an episode that live work still holds.
"""

from __future__ import annotations

import contextlib
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.dialects import postgresql

import app.models  # noqa: F401 - register model metadata
from app.models.dataset import Dataset, DatasetEntry
from app.models.evaluation import (
    EvaluationEpisode,
    EvaluationRun,
)
from app.models.orchestration import OrchestrationExecution
from app.services.evaluation_engine import NO_LEASE
from tests.evaluation_database import (
    _active_runs_fixture,
    _make_db,
    _make_db_full,
    _stub_audit_and_groups,  # noqa: F401 - autouse fixture
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


# --------------------------------------- runs abandoned by a dead worker
# A killed worker reaches no cancellation point, and the retry selects on
# "pending", so nothing else terminalises what it left "running".


async def test_retry_fails_the_runs_its_own_crashed_attempt_abandoned(monkeypatch):
    """A crash mid-batch then a retry leaves no run in progress.

    Attempt 1 flips two runs to "running" and is killed. The retry of the SAME
    execution runs the pending ones and completes, so no `_fail_active_runs`
    call site fires: only the reclaim fails the two abandoned runs.
    """
    from app.services.evaluation_engine import execute_evaluation

    execution = uuid.uuid4()
    dispatched: list[str] = []

    async def _fake_execute_run(db, run_id):
        dispatched.append(run_id)
        run = await db.get(EvaluationRun, uuid.UUID(run_id))
        run.status = "completed"
        run.completed_at = datetime.now(UTC)
        await db.commit()

    monkeypatch.setattr(
        "app.services.evaluation_engine.execute_evaluation_run", _fake_execute_run
    )

    started = datetime.now(UTC) - timedelta(seconds=42)
    # Full metadata: completing a run triggers the headline recompute, which
    # reads tables the curated _TABLES subset omits.
    sm, cleanup = await _make_db_full()
    try:
        # Attempt 1 got as far as flipping a1/a2 to "running", then was killed.
        eval_id, (a1, a2, p1) = await _active_runs_fixture(
            sm,
            [
                ("running", execution, {"started_at": started}),
                ("running", execution, {}),
                ("pending", execution, {}),
            ],
        )

        async with sm() as s:
            await execute_evaluation(
                s, str(eval_id), "manual", execution, worker_id=NO_LEASE
            )

        assert dispatched == [str(p1)], "only the still-pending run is dispatched"

        async with sm() as s:
            for rid in (a1, a2):
                after = await s.get(EvaluationRun, rid)
                assert after.status == "failed"
                assert "did not survive its attempt" in after.error
                assert after.completed_at is not None
            # The abandoned run that had started gets a real duration; the one
            # that never did keeps NULL rather than a bogus zero. The duration is
            # checked against the row's own timestamps (both written by the
            # reclaim), not the wall clock: database setup on a loaded runner
            # runs between ``started`` and the reclaim.
            started_run = await s.get(EvaluationRun, a1)
            assert started_run.duration_seconds == pytest.approx(
                (started_run.completed_at - started_run.started_at).total_seconds(),
                abs=0.01,
            )
            assert started_run.duration_seconds >= 42
            assert (await s.get(EvaluationRun, a2)).duration_seconds is None
            assert (await s.get(EvaluationRun, p1)).status == "completed"

            stranded = (
                await s.execute(
                    select(EvaluationRun).where(
                        EvaluationRun.evaluation_id == eval_id,
                        EvaluationRun.status.in_(["pending", "running"]),
                    )
                )
            ).scalars().all()
            assert stranded == [], "no run may outlive the attempt that abandoned it"
    finally:
        await cleanup()


async def test_reclaim_fails_its_own_and_unowned_running_runs_never_anothers(
    monkeypatch,
):
    """The reclaim is ownership-scoped like the cleanup helpers.

    A concurrent launch's in-flight run is not this execution's to fail. An unowned
    ``running`` run is: ``owner_execution_id`` has no backfill, so a run active
    before the column existed carries NULL forever, and strict ownership would
    strand it. The NULL arm is safe here as in the cleanup helpers, not in
    dispatch: a second ``status = "failed"`` is harmless, a second dispatch
    re-charges the judge.
    """
    from app.services.evaluation_engine import execute_evaluation

    mine, theirs = uuid.uuid4(), uuid.uuid4()

    async def _fake_execute_run(_db, _run_id):
        return None

    monkeypatch.setattr(
        "app.services.evaluation_engine.execute_evaluation_run", _fake_execute_run
    )

    sm, cleanup = await _make_db()
    try:
        eval_id, (ours, theirs_run, legacy) = await _active_runs_fixture(
            sm, [("running", mine, {}), ("running", theirs, {}), ("running", None, {})]
        )

        async with sm() as s:
            await execute_evaluation(
                s, str(eval_id), "manual", mine,
                worker_id=NO_LEASE,
            )

        async with sm() as s:
            assert (await s.get(EvaluationRun, ours)).status == "failed"
            assert (await s.get(EvaluationRun, theirs_run)).status == "running"
            legacy_after = await s.get(EvaluationRun, legacy)
            assert legacy_after.status == "failed"
            assert "did not survive its attempt" in legacy_after.error
    finally:
        await cleanup()


@pytest.mark.parametrize(
    ("holder", "expected"),
    [
        pytest.param("previous_release", "running", id="held_by_another_live_execution"),
        pytest.param("itself", "failed", id="named_only_by_the_reclaiming_execution"),
    ],
)
async def test_reclaim_leaves_an_unowned_run_another_live_execution_may_hold(
    monkeypatch, holder, expected
):
    """An unowned run is the reclaim's only while no OTHER live execution names its evaluation.

    During a rolling deploy a replica on the previous release may still be
    running it, through an execution that names the evaluation, never the run.
    The reclaiming execution is live too, and its own row never holds the run.
    """
    from app.services.evaluation_engine import execute_evaluation

    mine = uuid.uuid4()

    async def _fake_execute_run(_db, _run_id):
        return None

    monkeypatch.setattr(
        "app.services.evaluation_engine.execute_evaluation_run", _fake_execute_run
    )

    sm, cleanup = await _make_db()
    try:
        eval_id, (legacy,) = await _active_runs_fixture(sm, [("running", None, {})])
        async with sm() as s:
            s.add(
                OrchestrationExecution(
                    id=mine if holder == "itself" else uuid.uuid4(),
                    adapter="evaluation",
                    state="running",
                    payload={"evaluation_id": str(eval_id), "trigger_reason": "manual"},
                    attempt_count=1,
                )
            )
            await s.commit()

        async with sm() as s:
            await execute_evaluation(
                s, str(eval_id), "manual", mine, worker_id=NO_LEASE
            )

        async with sm() as s:
            status = (await s.get(EvaluationRun, legacy)).status
        assert status == expected, (
            f"an unowned run named by {holder!r} reads {status!r} after the "
            f"reclaim, not {expected!r}"
        )
    finally:
        await cleanup()


async def test_dead_letter_leaves_an_unowned_run_another_live_execution_may_hold():
    """The dead-letter cleanup applies the same veto to unowned runs."""
    from app.services.orchestration_adapters.base import AdapterContext
    from app.services.orchestration_adapters.evaluation import EvaluationAdapter

    execution_id = uuid.uuid4()

    sm, cleanup = await _make_db()
    try:
        eval_id, (owned, legacy) = await _active_runs_fixture(
            sm, [("running", execution_id, {}), ("running", None, {})]
        )
        async with sm() as s:
            s.add(
                OrchestrationExecution(
                    adapter="evaluation",
                    state="running",
                    payload={"evaluation_id": str(eval_id), "trigger_reason": "manual"},
                    attempt_count=1,
                )
            )
            await s.commit()

        await EvaluationAdapter().on_dead_letter(
            AdapterContext(db=None, worker_id="w1"),
            SimpleNamespace(
                id=execution_id,
                payload={"evaluation_id": str(eval_id), "trigger_reason": "manual"},
                last_error="attempts exhausted",
            ),
        )

        async with sm() as s:
            statuses = (
                (await s.get(EvaluationRun, owned)).status,
                (await s.get(EvaluationRun, legacy)).status,
            )
        assert statuses == ("failed", "running"), (
            f"owned/unowned read {statuses}: the dead letter failed an unowned "
            "run another live execution of its evaluation may still be running"
        )
    finally:
        await cleanup()


async def test_reclaim_leaves_terminal_and_cancelled_runs_alone(monkeypatch):
    """The reclaim touches only ``running`` rows, checked at write time.

    A run that finished, or was cancelled, between the crash and the retry keeps
    its state.
    """
    from app.services.evaluation_engine import execute_evaluation

    execution = uuid.uuid4()

    async def _fake_execute_run(_db, _run_id):
        return None

    monkeypatch.setattr(
        "app.services.evaluation_engine.execute_evaluation_run", _fake_execute_run
    )

    sm, cleanup = await _make_db()
    try:
        eval_id, (done, cancelled) = await _active_runs_fixture(
            sm,
            [
                ("completed", execution, {"completed_at": datetime.now(UTC)}),
                ("cancelled", execution, {}),
            ],
        )

        async with sm() as s:
            await execute_evaluation(
                s, str(eval_id), "manual", execution, worker_id=NO_LEASE
            )

        async with sm() as s:
            assert (await s.get(EvaluationRun, done)).status == "completed"
            assert (await s.get(EvaluationRun, cancelled)).status == "cancelled"
            assert (await s.get(EvaluationRun, cancelled)).error is None
    finally:
        await cleanup()


@pytest.mark.parametrize("parked_status", ["waiting_callback", "resumed"])
async def test_reclaim_leaves_callback_parked_runs_alone(monkeypatch, parked_status):
    """The reclaim leaves a run parked on a callback.

    ``execute_evaluation_run`` leaves a callback-backed run ``"running"`` while
    its episodes wait. The callback, ``timeout_waiting_episodes`` or a
    ``resume_episode`` execution owns it; failing it would kill healthy work.
    """
    from app.services.evaluation_engine import execute_evaluation

    execution = uuid.uuid4()

    async def _fake_execute_run(_db, _run_id):
        return None

    monkeypatch.setattr(
        "app.services.evaluation_engine.execute_evaluation_run", _fake_execute_run
    )

    sm, cleanup = await _make_db_full()
    try:
        eval_id, (parked, abandoned) = await _active_runs_fixture(
            sm, [("running", execution, {}), ("running", execution, {})]
        )
        async with sm() as s:
            # Own fixture rows, not whatever a previous test left behind: an
            # unordered ``limit(1)`` plus a fallback is two code paths.
            ds = Dataset(name="ds-parked", family="STIX_BUNDLE")
            s.add(ds)
            await s.flush()
            entry = DatasetEntry(dataset_id=ds.id, position=0, expected_answer="a")
            s.add(entry)
            await s.flush()
            # The parked run waits on a callback; the abandoned one died mid-entry,
            # leaving a "running" episode nothing will move.
            s.add_all(
                [
                    EvaluationEpisode(
                        run_id=parked,
                        entry_id=entry.id,
                        correlation_id=f"eval:{parked}:{entry.id}",
                        status=parked_status,
                    ),
                    EvaluationEpisode(
                        run_id=abandoned,
                        entry_id=entry.id,
                        correlation_id=f"eval:{abandoned}:{entry.id}",
                        status="running",
                    ),
                ]
            )
            await s.commit()

        async with sm() as s:
            await execute_evaluation(
                s, str(eval_id), "manual", execution, worker_id=NO_LEASE
            )

        async with sm() as s:
            still_parked = await s.get(EvaluationRun, parked)
            assert still_parked.status == "running", (
                f"a run with a {parked_status} episode is waiting, not abandoned"
            )
            assert still_parked.error is None
            # A "running" episode marks a run that died mid-entry: it does not
            # protect the run the way a parked one does.
            assert (await s.get(EvaluationRun, abandoned)).status == "failed"
    finally:
        await cleanup()


@pytest.mark.parametrize("owned", [True, False], ids=["owned", "legacy_unowned"])
async def test_reclaim_repairs_the_orphaned_episode_of_a_parked_run(monkeypatch, owned):
    """A mixed-episode run keeps its parked episode and has its orphan failed.

    The entry loop ``continue``s past a paused entry, so one run can hold a
    ``waiting_callback`` episode and a ``running`` one the worker died on.
    Failing the run would destroy outstanding callback work; leaving the orphan
    keeps ``_reconcile_run_status`` short of ``total_entries`` forever. The
    repair is per episode, and the run stays ``"running"`` until its callback
    resolves. An unowned parked run gets the same repair: ``~parked``
    excludes it from the run reclaim, and strict ownership would exclude it from
    the episode repair too.
    """
    from app.services.evaluation_engine import execute_evaluation

    execution = uuid.uuid4()

    async def _noop(_db, _run_id):
        return None

    monkeypatch.setattr(
        "app.services.evaluation_engine.execute_evaluation_run", _noop
    )

    sm, cleanup = await _make_db()
    try:
        # total_entries must be real: _reconcile_run_status treats 0 as "nothing
        # outstanding" and terminalises, which would mask the behaviour under test.
        eval_id, (mixed,) = await _active_runs_fixture(
            sm, [("running", execution if owned else None, {"total_entries": 2})]
        )
        async with sm() as s:
            ds = Dataset(name="ds-mixed", family="STIX_BUNDLE")
            s.add(ds)
            await s.flush()
            e1 = DatasetEntry(dataset_id=ds.id, position=0, expected_answer="a")
            e2 = DatasetEntry(dataset_id=ds.id, position=1, expected_answer="b")
            s.add_all([e1, e2])
            await s.flush()
            s.add_all(
                [
                    EvaluationEpisode(
                        run_id=mixed,
                        entry_id=e1.id,
                        correlation_id=f"eval:{mixed}:{e1.id}",
                        status="waiting_callback",
                    ),
                    EvaluationEpisode(
                        run_id=mixed,
                        entry_id=e2.id,
                        correlation_id=f"eval:{mixed}:{e2.id}",
                        status="running",
                    ),
                ]
            )
            await s.commit()

        async with sm() as s:
            await execute_evaluation(
                s, str(eval_id), "manual", execution, worker_id=NO_LEASE
            )

        async with sm() as s:
            assert (await s.get(EvaluationRun, mixed)).status == "running", (
                "the run still has outstanding callback work — only its "
                "orphaned episode is repaired"
            )
            episodes = {
                e.entry_id: e
                for e in (
                    await s.execute(
                        select(EvaluationEpisode).where(
                            EvaluationEpisode.run_id == mixed
                        )
                    )
                ).scalars().all()
            }
            assert episodes[e1.id].status == "waiting_callback", (
                "the parked episode belongs to the callback machinery"
            )
            orphan = episodes[e2.id]
            assert orphan.status == "failed", (
                "the entry the worker died on must go terminal, or the run can "
                "never reconcile"
            )
            assert "did not survive its attempt" in orphan.error
            assert orphan.completed_at is not None
    finally:
        await cleanup()


async def test_dead_lettered_resume_terminalises_its_episode(monkeypatch):
    """A dead-lettered resume execution finalizes its episode.

    ``on_dead_letter`` keys on ``rescore_runs`` then ``evaluation_id``, and a
    ``resume_episode`` payload carries neither. An episode left ``"resumed"`` is
    what the reclaim reads as owned elsewhere, so the run would never be repaired.
    """
    from app.services.orchestration_adapters.base import AdapterContext
    from app.services.orchestration_adapters.evaluation import EvaluationAdapter

    sm, cleanup = await _make_db()
    try:
        _eval_id, (run_id,) = await _active_runs_fixture(
            sm, [("running", uuid.uuid4(), {"total_entries": 1})]
        )
        async with sm() as s:
            ds = Dataset(name="ds-resume", family="STIX_BUNDLE")
            s.add(ds)
            await s.flush()
            entry = DatasetEntry(dataset_id=ds.id, position=0, expected_answer="a")
            s.add(entry)
            await s.flush()
            episode = EvaluationEpisode(
                run_id=run_id,
                entry_id=entry.id,
                correlation_id=f"eval:{run_id}:{entry.id}",
                status="resumed",
                resumed_at=datetime.now(UTC),
                idempotency_key="callback-1",
            )
            s.add(episode)
            await s.commit()
            episode_id = episode.id

        await EvaluationAdapter().on_dead_letter(
            AdapterContext(db=None, worker_id="w1"),
            SimpleNamespace(
                id=uuid.uuid4(),
                payload={
                    "kind": "resume_episode",
                    "episode_id": str(episode_id),
                    "idempotency_key": "callback-1",
                },
                last_error="deadline exceeded",
            ),
        )

        async with sm() as s:
            after = await s.get(EvaluationEpisode, episode_id)
            assert after.status == "failed", (
                "an abandoned resume leaves the episode terminal, not 'resumed'"
            )
            assert "deadline exceeded" in after.error
            # Its only entry is terminal, so the run reconciles too.
            assert (await s.get(EvaluationRun, run_id)).status != "running"
    finally:
        await cleanup()


async def test_dead_lettered_resume_leaves_an_already_finished_episode_alone(
    monkeypatch,
):
    """A resume that landed before the repair is not re-finalized with an
    abandonment error: the repair re-checks under the row lock.
    """
    from app.services.orchestration_adapters.base import AdapterContext
    from app.services.orchestration_adapters.evaluation import EvaluationAdapter

    sm, cleanup = await _make_db()
    try:
        _eval_id, (run_id,) = await _active_runs_fixture(
            sm, [("running", uuid.uuid4(), {"total_entries": 1})]
        )
        async with sm() as s:
            ds = Dataset(name="ds-resume-done", family="STIX_BUNDLE")
            s.add(ds)
            await s.flush()
            entry = DatasetEntry(dataset_id=ds.id, position=0, expected_answer="a")
            s.add(entry)
            await s.flush()
            episode = EvaluationEpisode(
                run_id=run_id,
                entry_id=entry.id,
                correlation_id=f"eval:{run_id}:{entry.id}",
                status="completed",
                final_output="the answer",
            )
            s.add(episode)
            await s.commit()
            episode_id = episode.id

        await EvaluationAdapter().on_dead_letter(
            AdapterContext(db=None, worker_id="w1"),
            SimpleNamespace(
                id=uuid.uuid4(),
                payload={"kind": "resume_episode", "episode_id": str(episode_id)},
                last_error="deadline exceeded",
            ),
        )

        async with sm() as s:
            after = await s.get(EvaluationEpisode, episode_id)
            assert after.status == "completed"
            assert after.error is None
            assert after.final_output == "the answer"
    finally:
        await cleanup()


async def test_dead_lettered_resume_leaves_a_newer_callback_generation_alone(
    monkeypatch,
):
    """An older resume's dead letter leaves an episode a newer callback owns.

    A second callback is legal while an episode is ``"resumed"``, and it enqueues
    a second resume execution with no dedup key. Finalizing would destroy a
    callback the caller was told was accepted, and the newer execution would
    no-op on a terminal row.
    """
    from app.services.orchestration_adapters.base import AdapterContext
    from app.services.orchestration_adapters.evaluation import EvaluationAdapter

    sm, cleanup = await _make_db()
    try:
        _eval_id, (run_id,) = await _active_runs_fixture(
            sm, [("running", uuid.uuid4(), {"total_entries": 1})]
        )
        async with sm() as s:
            ds = Dataset(name="ds-gen", family="STIX_BUNDLE")
            s.add(ds)
            await s.flush()
            entry = DatasetEntry(dataset_id=ds.id, position=0, expected_answer="a")
            s.add(entry)
            await s.flush()
            episode = EvaluationEpisode(
                run_id=run_id,
                entry_id=entry.id,
                correlation_id=f"eval:{run_id}:{entry.id}",
                status="resumed",
                resumed_at=datetime.now(UTC),
                idempotency_key="callback-2",
            )
            s.add(episode)
            await s.commit()
            episode_id = episode.id

        # The abandoned execution belongs to the FIRST callback.
        await EvaluationAdapter().on_dead_letter(
            AdapterContext(db=None, worker_id="w1"),
            SimpleNamespace(
                id=uuid.uuid4(),
                payload={
                    "kind": "resume_episode",
                    "episode_id": str(episode_id),
                    "idempotency_key": "callback-1",
                },
                last_error="deadline exceeded",
            ),
        )

        async with sm() as s:
            after = await s.get(EvaluationEpisode, episode_id)
            assert after.status == "resumed", (
                "the newer callback still owns this episode; failing it would "
                "lose work the caller was told was accepted"
            )
            assert after.error is None
    finally:
        await cleanup()


async def test_resume_repair_failure_propagates_rather_than_reporting_success(
    monkeypatch,
):
    """A failing resume repair raises instead of reporting success.

    Swallowed, the reconciler would record a repair that never happened and the
    episode would stay non-terminal. The reconciler retries dead-letter
    failures and the repair is idempotent under its row lock, so raising is safe.
    """
    from app.services.orchestration_adapters.base import AdapterContext
    from app.services.orchestration_adapters.evaluation import EvaluationAdapter

    def _boom():
        raise RuntimeError("the database went away")

    monkeypatch.setattr("app.core.database.async_session", _boom)

    with pytest.raises(RuntimeError, match="the database went away"):
        await EvaluationAdapter().on_dead_letter(
            AdapterContext(db=None, worker_id="w1"),
            SimpleNamespace(
                id=uuid.uuid4(),
                payload={
                    "kind": "resume_episode",
                    "episode_id": str(uuid.uuid4()),
                    "idempotency_key": "k",
                },
                last_error="deadline exceeded",
            ),
        )


async def test_reclaim_repairs_episodes_of_the_runs_it_just_failed(monkeypatch):
    """The episode repair covers the runs the reclaim just failed.

    The reclaim moves abandoned runs to ``"failed"`` first, so a repair filtered
    on ``pending``/``running`` would skip them and leave their episodes ``running``.
    """
    from app.services.evaluation_engine import execute_evaluation

    execution = uuid.uuid4()

    async def _noop(_db, _run_id):
        return None

    monkeypatch.setattr(
        "app.services.evaluation_engine.execute_evaluation_run", _noop
    )

    sm, cleanup = await _make_db()
    try:
        eval_id, (crashed,) = await _active_runs_fixture(
            sm, [("running", execution, {"total_entries": 2})]
        )
        async with sm() as s:
            ds = Dataset(name="ds-crashed", family="STIX_BUNDLE")
            s.add(ds)
            await s.flush()
            entry = DatasetEntry(dataset_id=ds.id, position=0, expected_answer="a")
            s.add(entry)
            await s.flush()
            # No parked sibling: the run is reclaimed and its episode with it.
            s.add(
                EvaluationEpisode(
                    run_id=crashed,
                    entry_id=entry.id,
                    correlation_id=f"eval:{crashed}:{entry.id}",
                    status="running",
                )
            )
            await s.commit()

        async with sm() as s:
            await execute_evaluation(
                s, str(eval_id), "manual", execution, worker_id=NO_LEASE
            )

        async with sm() as s:
            assert (await s.get(EvaluationRun, crashed)).status == "failed"
            episode = (
                await s.execute(
                    select(EvaluationEpisode).where(
                        EvaluationEpisode.run_id == crashed
                    )
                )
            ).scalar_one()
            assert episode.status == "failed", (
                "an episode on a failed run is still an episode nobody will move"
            )
    finally:
        await cleanup()


async def test_orphan_repair_revalidates_run_ownership_under_the_lock(monkeypatch):
    """The orphan repair re-checks run ownership under the lock.

    Another launch can claim an unowned run between the caller's read and the
    repair, creating a live episode. A second ``"failed"`` on a run is harmless;
    finalizing a live episode is not.
    """
    from app.services.evaluation_continuation import fail_orphaned_episodes

    mine, theirs = uuid.uuid4(), uuid.uuid4()

    sm, cleanup = await _make_db()
    try:
        _eval_id, (my_run, their_run) = await _active_runs_fixture(
            sm,
            [
                ("running", mine, {"total_entries": 2}),
                ("running", theirs, {"total_entries": 2}),
            ],
        )
        async with sm() as s:
            ds = Dataset(name="ds-owner", family="STIX_BUNDLE")
            s.add(ds)
            await s.flush()
            entry = DatasetEntry(dataset_id=ds.id, position=0, expected_answer="a")
            s.add(entry)
            await s.flush()
            s.add_all(
                [
                    EvaluationEpisode(
                        run_id=my_run,
                        entry_id=entry.id,
                        correlation_id=f"eval:{my_run}:{entry.id}",
                        status="running",
                    ),
                    EvaluationEpisode(
                        run_id=their_run,
                        entry_id=entry.id,
                        correlation_id=f"eval:{their_run}:{entry.id}",
                        status="running",
                    ),
                ]
            )
            await s.commit()

        # Both runs are handed in, as a stale caller read would; only the one
        # this execution still owns is touched.
        async with sm() as s:
            await fail_orphaned_episodes(
                s,
                owned_run_ids=[my_run, their_run],
                reclaimed_run_ids=[],
                execution_id=mine,
                error="abandoned",
            )
            # The helper flushes; the caller commits, so the repair shares a
            # transaction with the run reclaim.
            await s.commit()

        async with sm() as s:
            episodes = {
                e.run_id: e
                for e in (
                    await s.execute(select(EvaluationEpisode))
                ).scalars().all()
            }
            assert episodes[my_run].status == "failed"
            assert episodes[their_run].status == "running", (
                "another execution's live episode must survive a stale run list"
            )
    finally:
        await cleanup()


@pytest.mark.parametrize(
    ("holder", "expected"),
    [
        pytest.param("previous_release", "running", id="held_by_another_live_execution"),
        pytest.param("itself", "failed", id="named_only_by_the_repairing_execution"),
        pytest.param("nobody", "failed", id="no_live_execution"),
    ],
)
async def test_orphan_repair_leaves_an_unowned_run_another_live_execution_may_hold(
    holder, expected
):
    """An unowned run's episode is repaired only while no OTHER live execution names its evaluation.

    A replica on the previous release may still be running the episode during a
    rolling deploy, through an execution that names the evaluation, never the
    run. The repairing execution is live too, and its own row never vetoes its
    repair; with no live execution at all the episode is failed as before.
    """
    from app.services.evaluation_continuation import fail_orphaned_episodes

    mine = uuid.uuid4()

    sm, cleanup = await _make_db()
    try:
        eval_id, (legacy,) = await _active_runs_fixture(sm, [("running", None, {})])
        async with sm() as s:
            ds = Dataset(name="ds-legacy", family="STIX_BUNDLE")
            s.add(ds)
            await s.flush()
            entry = DatasetEntry(dataset_id=ds.id, position=0, expected_answer="a")
            s.add(entry)
            await s.flush()
            s.add(
                EvaluationEpisode(
                    run_id=legacy,
                    entry_id=entry.id,
                    correlation_id=f"eval:{legacy}:{entry.id}",
                    status="running",
                )
            )
            if holder != "nobody":
                s.add(
                    OrchestrationExecution(
                        id=mine if holder == "itself" else uuid.uuid4(),
                        adapter="evaluation",
                        state="running",
                        payload={
                            "evaluation_id": str(eval_id),
                            "trigger_reason": "manual",
                        },
                        attempt_count=1,
                    )
                )
            await s.commit()

        async with sm() as s:
            await fail_orphaned_episodes(
                s,
                owned_run_ids=[legacy],
                reclaimed_run_ids=[],
                execution_id=mine,
                error="abandoned",
            )
            await s.commit()

        async with sm() as s:
            status = (
                await s.execute(
                    select(EvaluationEpisode.status).where(
                        EvaluationEpisode.run_id == legacy
                    )
                )
            ).scalar_one()
        assert status == expected, (
            f"the episode of an unowned run named by {holder!r} reads {status!r} "
            f"after the orphan repair, not {expected!r}"
        )
    finally:
        await cleanup()


@pytest.mark.parametrize(
    ("stored_key", "payload_key"),
    [("callback-1", None), ("", "")],
    ids=["no_token", "blank_token"],
)
async def test_dead_lettered_resume_without_a_usable_token_refuses_to_repair(
    monkeypatch, stored_key, payload_key
):
    """A dead-lettered resume with no or a blank ``idempotency_key`` repairs nothing.

    It cannot be told apart from a newer generation, so it fails closed: leaving
    an episode unrepaired beats destroying an accepted callback. A blank
    key identifies nothing even against a stored blank key, for executions
    persisted before the schema rejected one.
    """
    from app.services.orchestration_adapters.base import AdapterContext
    from app.services.orchestration_adapters.evaluation import EvaluationAdapter

    sm, cleanup = await _make_db()
    try:
        _eval_id, (run_id,) = await _active_runs_fixture(
            sm, [("running", uuid.uuid4(), {"total_entries": 1})]
        )
        async with sm() as s:
            ds = Dataset(name="ds-token", family="STIX_BUNDLE")
            s.add(ds)
            await s.flush()
            entry = DatasetEntry(dataset_id=ds.id, position=0, expected_answer="a")
            s.add(entry)
            await s.flush()
            episode = EvaluationEpisode(
                run_id=run_id,
                entry_id=entry.id,
                correlation_id=f"eval:{run_id}:{entry.id}",
                status="resumed",
                resumed_at=datetime.now(UTC),
                idempotency_key=stored_key,
            )
            s.add(episode)
            await s.commit()
            episode_id = episode.id

        payload = {"kind": "resume_episode", "episode_id": str(episode_id)}
        if payload_key is not None:
            payload["idempotency_key"] = payload_key
        await EvaluationAdapter().on_dead_letter(
            AdapterContext(db=None, worker_id="w1"),
            SimpleNamespace(
                id=uuid.uuid4(), payload=payload, last_error="deadline exceeded"
            ),
        )

        async with sm() as s:
            assert (await s.get(EvaluationEpisode, episode_id)).status == "resumed", (
                "without a usable token a newer callback cannot be ruled out"
            )
    finally:
        await cleanup()


async def test_blank_idempotency_key_is_rejected_at_the_callback_schema():
    """The callback schema rejects a blank ``idempotency_key``.

    ``resume_from_callback`` treats an empty key as absent, so every repeat would
    open a new generation keyed ``""``, and an older execution's repair would
    match the newer one (``"" == ""``) and finalize its episode.
    """
    import pydantic

    from app.api.v1.schemas.evaluation import ResumeEvaluationEpisodeRequest

    with pytest.raises(pydantic.ValidationError):
        ResumeEvaluationEpisodeRequest(correlation_id="c", idempotency_key="")
    with pytest.raises(pydantic.ValidationError):
        ResumeEvaluationEpisodeRequest(correlation_id="", idempotency_key="k")
    assert (
        ResumeEvaluationEpisodeRequest(
            correlation_id="c", idempotency_key="k"
        ).idempotency_key
        == "k"
    )


@pytest.mark.parametrize("fault", ["episode_repair", "run_reclaim"])
@pytest.mark.usefixtures("evaluation_enabled")
async def test_a_failed_pre_dispatch_repair_is_retryable_and_applies_nothing(
    monkeypatch, fault
):
    """A fault in the episode repair or in the run reclaim ``UPDATE`` is retryable and
    leaves nothing applied.

    Outside the retryable boundary it would take the generic non-retryable path,
    whose ``_fail_active_runs`` fails runs but NOT episodes, leaving a failed run
    with a ``"running"`` episode that never reaches ``on_dead_letter``. Reclaim
    and repair share one transaction, so the retry finds the work as it was.
    """
    from app.services.orchestration_adapters.base import AdapterContext
    from app.services.orchestration_adapters.evaluation import EvaluationAdapter
    from tests.agentic_gate_stub import stub_agentic_gate

    # The adapter reads the agentic gate on this session before it reclaims:
    # stubbed, so the only fault is the injected one.
    stub_agentic_gate(monkeypatch)

    execution_id = uuid.uuid4()
    fired: list[str] = []

    async def _boom(*_a, **_k):
        fired.append("repair")
        raise RuntimeError("the database went away")

    if fault == "episode_repair":
        monkeypatch.setattr(
            "app.services.evaluation_engine.fail_orphaned_episodes", _boom
        )

    sm, cleanup = await _make_db()
    try:
        eval_id, (crashed,) = await _active_runs_fixture(
            sm, [("running", execution_id, {"total_entries": 2})]
        )
        # The pre-dispatch reclaim is fenced on the lease: the execution
        # row has to exist and be ours, or the test stops at the fence.
        async with sm() as s:
            s.add(
                OrchestrationExecution(
                    id=execution_id,
                    adapter="evaluation",
                    state="running",
                    payload={},
                    lease_owner="w1",
                    attempt_count=1,
                )
            )
            await s.commit()

        async with sm() as s:
            if fault == "run_reclaim":
                real_execute = s.execute

                async def _fail_the_reclaim(stmt, *a, **k):
                    # The reclaim is the first UPDATE of ``evaluation_runs``.
                    table = getattr(stmt, "table", None)
                    if (
                        not fired
                        and getattr(stmt, "is_update", False)
                        and getattr(table, "name", None) == "evaluation_runs"
                    ):
                        fired.append("reclaim")
                        raise RuntimeError("the database went away")
                    return await real_execute(stmt, *a, **k)

                monkeypatch.setattr(s, "execute", _fail_the_reclaim)
            result = await EvaluationAdapter().execute(
                AdapterContext(db=s, worker_id="w1"),
                SimpleNamespace(
                    id=execution_id,
                    payload={
                        "evaluation_id": str(eval_id),
                        "trigger_reason": "manual",
                    },
                ),
            )

        assert fired == [fault.split("_")[-1]], "the injected fault never ran"
        assert result.kind == "failed"
        assert result.retryable is True, (
            "a repair failure must be retried, not dead-ended non-retryably"
        )

        async with sm() as s:
            assert (await s.get(EvaluationRun, crashed)).status == "running", (
                "the reclaim shares the repair's transaction, so a failure "
                "leaves the run exactly as the retry needs to find it"
            )
    finally:
        await cleanup()


async def test_repair_leaves_a_run_that_is_only_running_for_a_rescore(monkeypatch):
    """The episode repair leaves a run ``"running"`` only for a re-score.

    A re-score keeps ``completed_at`` set. Finalizing its episodes would reconcile
    the run and overwrite the original execution's ``completed_at`` and
    ``duration_seconds``, which a re-score never re-runs.
    """
    from app.services.evaluation_engine import execute_evaluation

    execution = uuid.uuid4()
    started = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

    async def _noop(_db, _run_id):
        return None

    monkeypatch.setattr(
        "app.services.evaluation_engine.execute_evaluation_run", _noop
    )

    sm, cleanup = await _make_db()
    try:
        eval_id, (rescoring,) = await _active_runs_fixture(
            sm,
            [
                (
                    "running",
                    execution,
                    {
                        "total_entries": 1,
                        "started_at": started,
                        "completed_at": started + timedelta(seconds=30),
                        "duration_seconds": 30.0,
                    },
                )
            ],
        )
        async with sm() as s:
            ds = Dataset(name="ds-rescore-ep", family="STIX_BUNDLE")
            s.add(ds)
            await s.flush()
            entry = DatasetEntry(dataset_id=ds.id, position=0, expected_answer="a")
            s.add(entry)
            await s.flush()
            s.add(
                EvaluationEpisode(
                    run_id=rescoring,
                    entry_id=entry.id,
                    correlation_id=f"eval:{rescoring}:{entry.id}",
                    status="running",
                )
            )
            await s.commit()

        async with sm() as s:
            await execute_evaluation(
                s, str(eval_id), "manual", execution, worker_id=NO_LEASE
            )

        async with sm() as s:
            after = await s.get(EvaluationRun, rescoring)
            assert after.status == "running", "a re-scoring run is not abandoned"
            assert after.duration_seconds == 30.0
            assert after.completed_at == started + timedelta(seconds=30)
            episode = (
                await s.execute(
                    select(EvaluationEpisode).where(
                        EvaluationEpisode.run_id == rescoring
                    )
                )
            ).scalar_one()
            assert episode.status == "running"
    finally:
        await cleanup()


async def test_dead_letter_repairs_episodes_not_just_runs(monkeypatch):
    """``on_dead_letter`` repairs episodes as well as failing runs.

    A persistent fault in the pre-dispatch repair exhausts its retries and lands
    here, so failing runs alone would leave each with a ``"running"`` episode
    nobody can move.
    """
    from app.services.orchestration_adapters.base import AdapterContext
    from app.services.orchestration_adapters.evaluation import EvaluationAdapter

    execution_id = uuid.uuid4()

    sm, cleanup = await _make_db()
    try:
        eval_id, (owned, legacy) = await _active_runs_fixture(
            sm,
            [
                ("running", execution_id, {"total_entries": 1}),
                # Older row: no owner, ever.
                ("running", None, {"total_entries": 1}),
            ],
        )
        async with sm() as s:
            ds = Dataset(name="ds-dl", family="STIX_BUNDLE")
            s.add(ds)
            await s.flush()
            entry = DatasetEntry(dataset_id=ds.id, position=0, expected_answer="a")
            s.add(entry)
            await s.flush()
            s.add_all(
                [
                    EvaluationEpisode(
                        run_id=rid,
                        entry_id=entry.id,
                        correlation_id=f"eval:{rid}:{entry.id}",
                        status="running",
                    )
                    for rid in (owned, legacy)
                ]
            )
            await s.commit()

        await EvaluationAdapter().on_dead_letter(
            AdapterContext(db=None, worker_id="w1"),
            SimpleNamespace(
                id=execution_id,
                payload={"evaluation_id": str(eval_id), "trigger_reason": "manual"},
                last_error="attempts exhausted",
            ),
        )

        async with sm() as s:
            episodes = {
                e.run_id: e
                for e in (
                    await s.execute(select(EvaluationEpisode))
                ).scalars().all()
            }
            for rid, label in ((owned, "owned"), (legacy, "legacy unowned")):
                assert (await s.get(EvaluationRun, rid)).status == "failed"
                assert episodes[rid].status == "failed", (
                    f"the {label} run's episode must be terminalised too, or "
                    "the dead letter leaves exactly what it is repairing"
                )
    finally:
        await cleanup()


async def test_repair_reconciles_when_the_callback_resolved_first(monkeypatch):
    """The repair reconciles the run when the parked sibling resolved first.

    That sibling's reconcile saw the orphan still ``"running"`` and returned
    early; nothing else looks again, so the run would stay ``"running"``.
    """
    from app.services.evaluation_engine import execute_evaluation

    execution = uuid.uuid4()

    async def _noop(_db, _run_id):
        return None

    monkeypatch.setattr(
        "app.services.evaluation_engine.execute_evaluation_run", _noop
    )

    sm, cleanup = await _make_db()
    try:
        eval_id, (run_id,) = await _active_runs_fixture(
            sm, [("running", execution, {"total_entries": 2})]
        )
        async with sm() as s:
            ds = Dataset(name="ds-cb-first", family="STIX_BUNDLE")
            s.add(ds)
            await s.flush()
            e1 = DatasetEntry(dataset_id=ds.id, position=0, expected_answer="a")
            e2 = DatasetEntry(dataset_id=ds.id, position=1, expected_answer="b")
            s.add_all([e1, e2])
            await s.flush()
            s.add_all(
                [
                    # The callback already landed and completed this one.
                    EvaluationEpisode(
                        run_id=run_id,
                        entry_id=e1.id,
                        correlation_id=f"eval:{run_id}:{e1.id}",
                        status="completed",
                        completed_at=datetime.now(UTC),
                    ),
                    # The entry the worker died on.
                    EvaluationEpisode(
                        run_id=run_id,
                        entry_id=e2.id,
                        correlation_id=f"eval:{run_id}:{e2.id}",
                        status="running",
                    ),
                ]
            )
            await s.commit()

        async with sm() as s:
            await execute_evaluation(
                s, str(eval_id), "manual", execution, worker_id=NO_LEASE
            )

        async with sm() as s:
            assert (await s.get(EvaluationRun, run_id)).status != "running", (
                "every episode is terminal — nothing else will ever reconcile "
                "this run, so the repair has to"
            )
    finally:
        await cleanup()


async def test_dead_letter_runs_and_episodes_land_in_one_transaction(monkeypatch):
    """The dead-letter hook fails runs and repairs episodes in one transaction.

    The execution is already committed as ``dead_letter`` and the recovery
    sweeps pick only ``pending``/``running`` rows, so the hook never fires again.
    A worker dying between two transactions would leave a failed run with a
    ``"running"`` episode for good.
    """
    from app.services.orchestration_adapters import evaluation as adapter_mod
    from app.services.orchestration_adapters.base import AdapterContext

    execution_id = uuid.uuid4()
    sm, cleanup = await _make_db()
    try:
        eval_id, (run_id,) = await _active_runs_fixture(
            sm, [("running", execution_id, {"total_entries": 1})]
        )
        async with sm() as s:
            ds = Dataset(name="ds-atomic", family="STIX_BUNDLE")
            s.add(ds)
            await s.flush()
            entry = DatasetEntry(dataset_id=ds.id, position=0, expected_answer="a")
            s.add(entry)
            await s.flush()
            s.add(
                EvaluationEpisode(
                    run_id=run_id,
                    entry_id=entry.id,
                    correlation_id=f"eval:{run_id}:{entry.id}",
                    status="running",
                )
            )
            await s.commit()

        # Break the episode half: the run failures roll back with it.
        async def _boom(*_a, **_k):
            raise RuntimeError("the database went away")

        monkeypatch.setattr(adapter_mod, "_recover_stranded_headlines", _boom)
        monkeypatch.setattr(
            "app.services.evaluation_continuation.fail_orphaned_episodes", _boom
        )

        with contextlib.suppress(Exception):
            await adapter_mod.EvaluationAdapter().on_dead_letter(
                AdapterContext(db=None, worker_id="w1"),
                SimpleNamespace(
                    id=execution_id,
                    payload={
                        "evaluation_id": str(eval_id),
                        "trigger_reason": "manual",
                    },
                    last_error="attempts exhausted",
                ),
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
            assert (run.status, episode.status) in {
                ("failed", "failed"),
                ("running", "running"),
            }, (
                "runs and episodes must land together — a failed run with a "
                f"running episode is the stranding this repairs, got "
                f"{run.status!r}/{episode.status!r}"
            )
    finally:
        await cleanup()


async def test_dead_letter_leaves_a_rescoring_run_and_its_timings_alone(monkeypatch):
    """The dead-letter episode repair carries the rescore guard too.

    Without ``active`` it matches every run the execution owned, including a
    finished one ``"running"`` again for a rescore. Finalizing its episodes would
    let ``_reconcile_run_status`` overwrite the original execution's
    ``completed_at`` and ``duration_seconds``.
    """
    from app.services.orchestration_adapters import evaluation as adapter_mod
    from app.services.orchestration_adapters.base import AdapterContext

    execution_id = uuid.uuid4()
    started = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    finished = started + timedelta(seconds=30)

    sm, cleanup = await _make_db()
    try:
        eval_id, (rescoring,) = await _active_runs_fixture(
            sm,
            [
                (
                    "running",
                    execution_id,
                    {
                        "total_entries": 1,
                        "started_at": started,
                        "completed_at": finished,
                        "duration_seconds": 30.0,
                        "score": 88.0,
                    },
                )
            ],
        )
        async with sm() as s:
            ds = Dataset(name="ds-dl-rescore", family="STIX_BUNDLE")
            s.add(ds)
            await s.flush()
            entry = DatasetEntry(dataset_id=ds.id, position=0, expected_answer="a")
            s.add(entry)
            await s.flush()
            s.add(
                EvaluationEpisode(
                    run_id=rescoring,
                    entry_id=entry.id,
                    correlation_id=f"eval:{rescoring}:{entry.id}",
                    status="running",
                )
            )
            await s.commit()

        await adapter_mod.EvaluationAdapter().on_dead_letter(
            AdapterContext(db=None, worker_id="w1"),
            SimpleNamespace(
                id=execution_id,
                payload={"evaluation_id": str(eval_id), "trigger_reason": "manual"},
                last_error="attempts exhausted",
            ),
        )

        async with sm() as s:
            after = await s.get(EvaluationRun, rescoring)
            assert after.completed_at == finished, (
                "completed_at describes the ORIGINAL agent execution, which a "
                "rescore never re-runs"
            )
            assert after.duration_seconds == 30.0
            assert after.score == 88.0
    finally:
        await cleanup()


async def test_run_and_episode_locks_are_taken_in_one_direction(monkeypatch):
    """Lock order is run, then episode, everywhere both are touched.

    The abandoned-work repairs lock runs first (their reclaim is an ``UPDATE``
    on runs), so ``process_resume_episode`` locking the episode first would be
    an ABBA inversion and could deadlock. The order is observed, not raced,
    which would be flaky.
    """
    from app.services.evaluation_continuation import process_resume_episode

    order: list[str] = []
    sm, cleanup = await _make_db()
    try:
        _eval_id, (run_id,) = await _active_runs_fixture(
            sm, [("running", uuid.uuid4(), {"total_entries": 1})]
        )
        async with sm() as s:
            ds = Dataset(name="ds-lockorder", family="STIX_BUNDLE")
            s.add(ds)
            await s.flush()
            entry = DatasetEntry(dataset_id=ds.id, position=0, expected_answer="a")
            s.add(entry)
            await s.flush()
            episode = EvaluationEpisode(
                run_id=run_id,
                entry_id=entry.id,
                correlation_id=f"eval:{run_id}:{entry.id}",
                status="resumed",
                resumed_at=datetime.now(UTC),
                final_output="done",
            )
            s.add(episode)
            await s.commit()
            episode_id = episode.id

        async with sm() as s:
            real = s.execute

            async def _watch(stmt, *a, **k):
                sql = str(stmt).lower()
                if "for update" in sql:
                    if "evaluation_runs" in sql:
                        order.append("run")
                    elif "evaluation_episodes" in sql:
                        order.append("episode")
                return await real(stmt, *a, **k)

            monkeypatch.setattr(s, "execute", _watch)
            await process_resume_episode(s, episode_id=str(episode_id))

        assert order, "expected the resume path to take row locks"
        assert order[0] == "run", (
            f"the run lock must come first, got {order} — an episode-first "
            "resume deadlocks against a run-first repair"
        )
    finally:
        await cleanup()


@pytest.mark.asyncio
async def test_the_episode_repair_skips_rows_a_live_worker_holds():
    """``fail_orphaned_episodes`` skips a locked episode row and never waits on it.

    ``execute_evaluation_run`` holds an episode row while reaching for the run
    the repair's caller already locked. Conforming it would hold a run lock
    across every entry's scoring tail, so the janitor yields. A locked row is
    live work, not abandoned, so skipping it is also correct.
    """
    import app.services.evaluation_continuation as cont

    captured: list[str] = []

    class _Result:
        def scalars(self):
            return self

        def all(self):
            return []

    class _Session:
        async def execute(self, stmt, *a, **k):
            captured.append(str(stmt.compile(dialect=postgresql.dialect())))
            return _Result()

    await cont.fail_orphaned_episodes(
        _Session(),
        owned_run_ids=[uuid.uuid4()],
        reclaimed_run_ids=[],
        execution_id=uuid.uuid4(),
        error="worker lost",
    )

    episode_locks = [
        sql
        for sql in captured
        if "FOR UPDATE" in sql and "evaluation_episodes" in sql.split("FROM")[-1]
    ]
    assert episode_locks, f"expected an episode lock, got {captured}"
    for sql in episode_locks:
        assert "SKIP LOCKED" in sql, (
            "the episode lock must not wait — without SKIP LOCKED this repair "
            f"deadlocks against a live execute_evaluation_run:\n{sql}"
        )
