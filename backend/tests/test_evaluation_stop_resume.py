"""An evaluation whose worker stops (a deploy, a restart) resumes instead of failing.

The worker that stops pauses its runs at an entry boundary and hands the
execution back (``AdapterResult.defer``, no attempt spent); one cut in the
middle of an entry is taken back on its worker's last word, and the next
attempt puts its runs back to ``pending`` (``resume_stopped_runs``). Either
way a resumed run skips the entries it finished and redoes only the one the
stop cut. A worker gone without a word (a crash, an out-of-memory kill) still
fails what it left, as ``test_evaluation_abandoned_runs`` pins.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

import app.models  # noqa: F401 - register model metadata
from app.models.dataset import Dataset, DatasetEntry
from app.models.evaluation import EvaluationEpisode, EvaluationResult, EvaluationRun
from app.services.evaluation_engine import HANDED_BACK_PAYLOAD_KEY, NO_LEASE
from app.services.orchestration_adapters import AdapterContext
from app.services.orchestration_adapters.evaluation import EvaluationAdapter
from app.services.orchestration_adapters.sub_agent import HANDED_BACK_STATE
from tests.evaluation_database import (
    _active_runs_fixture,
    _make_db_full,
    _stub_audit_and_groups,  # noqa: F401 - autouse fixture
)
from tests.evaluation_helpers import run_on_postgres
from tests.test_orchestration_adapters import (
    _agentic_gate_available,  # noqa: F401 - autouse fixture
    _FakeDb,
    _isolate_registry,  # noqa: F401 - autouse fixture
)


async def _noop(*_args, **_kwargs):
    return None


#: When the stop in these tests happened.
_AT = "2026-11-19T20:54:02+00:00"


def _TAKEN_BACK(state: str, **record) -> dict:  # noqa: N802 - reads as a constant
    """A ``lease_expired`` record a take-back writes, from a worker whose last word was *state*."""
    return {"at": _AT, "attempt": 0, "owner_record": {"state": state, **record}}


def _execution(**fields):
    return SimpleNamespace(
        id=uuid.uuid4(),
        payload={"evaluation_id": str(uuid.uuid4()), "trigger_reason": "manual"},
        attempt_count=1,
        adapter_state=None,
        **fields,
    )


# ── the adapter ──


def test_the_adapter_resumes_when_its_worker_goes():
    """Taken back the moment its worker is gone, the attempt given back on a stop."""
    assert EvaluationAdapter.resume_when_worker_gone is True


@pytest.mark.asyncio
@pytest.mark.usefixtures("evaluation_enabled")
async def test_a_hand_back_defers_first_in_the_queue_without_an_attempt(monkeypatch):
    from app.services import evaluation_engine
    from app.services.orchestration_adapters import evaluation as adapter_module

    seen: dict = {}

    async def _execute(_db, _eid, _trigger, _execution_id, **kwargs):
        seen.update(kwargs)
        return True

    monkeypatch.setattr(evaluation_engine, "execute_evaluation", _execute)
    monkeypatch.setattr(adapter_module, "_flush_langfuse", _noop)
    draining = asyncio.Event()
    draining.set()

    result = await EvaluationAdapter().execute(
        AdapterContext(db=_FakeDb(), worker_id="w1", stopping=asyncio.Event(), draining=draining),
        _execution(),
    )

    assert result.kind == "defer"
    assert result.place == "head"
    assert result.adapter_state == HANDED_BACK_STATE
    # The stop marker the next attempt resumes on, kept in the payload past any retry or hold.
    assert "at" in result.payload_update[HANDED_BACK_PAYLOAD_KEY]
    # The stop signal, not the shutdown hook past the grace: the entry in flight gets the whole grace.
    assert seen["stopping"] is draining
    assert seen["resume_stopped_runs"] is False


@pytest.mark.asyncio
@pytest.mark.usefixtures("evaluation_enabled")
@pytest.mark.parametrize(
    ("adapter_state", "payload", "resumes"),
    [
        pytest.param("worker_gone", {"lease_expired": _TAKEN_BACK("stopped")}, True, id="stopped-on-its-word"),
        pytest.param("worker_gone", {"lease_expired": _TAKEN_BACK("draining")}, True, id="draining"),
        pytest.param(
            "worker_gone",
            {"lease_expired": _TAKEN_BACK("running", replaced_by_deploy=True)},
            True,
            id="killed-by-a-deploy",
        ),
        pytest.param("worker_gone", {"lease_expired": _TAKEN_BACK("running")}, False, id="gone-without-a-word"),
        pytest.param(None, {}, False, id="lease-expired"),
        # A retry (``engine.fail`` clears the state) or an AI hold between the
        # take-back and the resume: the take-back's record still says it.
        pytest.param(None, {"lease_expired": _TAKEN_BACK("stopped")}, True, id="state-cleared-by-a-retry"),
        pytest.param(
            "held:ai_disabled", {"lease_expired": _TAKEN_BACK("stopped")}, True, id="state-replaced-by-a-hold"
        ),
        # Handed back: its marker, whatever the adapter state became since.
        pytest.param(None, {"evaluation_handed_back": {"at": _AT}}, True, id="handed-back"),
        # Already resumed from that stop: an attempt cut otherwise after it is not resumed on it again.
        pytest.param(
            None,
            {"lease_expired": _TAKEN_BACK("stopped"), "stop_resumes": {"consumed": _AT}},
            False,
            id="stop-already-resumed-from",
        ),
    ],
)
async def test_the_runs_resume_only_after_a_stop_outside_the_work(monkeypatch, adapter_state, payload, resumes):
    """A crash or an out-of-memory kill may be the work's own: its runs still fail."""
    from app.services import evaluation_engine
    from app.services.orchestration_adapters import evaluation as adapter_module

    seen: dict = {}

    async def _execute(_db, _eid, _trigger, _execution_id, **kwargs):
        seen.update(kwargs)
        return False

    monkeypatch.setattr(evaluation_engine, "execute_evaluation", _execute)
    monkeypatch.setattr(adapter_module, "_flush_langfuse", _noop)
    execution = _execution()
    execution.adapter_state = adapter_state
    execution.payload.update(payload)

    result = await EvaluationAdapter().execute(AdapterContext(db=_FakeDb(), worker_id="w2"), execution)

    assert result.kind == "completed"
    assert seen["resume_stopped_runs"] is resumes


@pytest.mark.asyncio
async def test_a_stopping_worker_stops_its_lease_loss_work_before_it_says_it_stopped():
    """A worker that hears "stopped" takes the work back at once: nothing of the old one may still write."""
    from app.services import orchestration_reconciler as reconciler

    told = asyncio.Event()
    unwound: list[bool] = []

    async def _execution():
        await told.wait()  # the adapter's stop on a lost lease
        unwound.append(True)

    async def _assignment():
        await asyncio.sleep(3600)  # another adapter's long work, not told to stop

    task = asyncio.create_task(_execution())
    other = asyncio.create_task(_assignment())
    reconciler._execution_tasks.update({task, other})
    reconciler._lease_loss_events[uuid.uuid4()] = (told, task)
    try:
        await asyncio.wait_for(reconciler._stop_lease_loss_work(), timeout=2)
    finally:
        other.cancel()
        reconciler._execution_tasks.difference_update({task, other})
        reconciler._lease_loss_events.clear()

    assert told.is_set()
    assert unwound == [True], "waited for the execution to unwind"


def test_a_stopping_worker_says_draining_stops_its_work_then_says_stopped(monkeypatch):
    """The order the immediate take-back relies on: nothing writes after "stopped"."""
    from app.services import orchestration_reconciler as rec
    from app.services import worker_presence

    said: list[str] = []

    async def _publish(_worker_id, *, state, **_kw):
        said.append(state)

    async def _stop_work():
        said.append("stop-work")

    async def _cycle(_worker_id):
        return None

    async def _sleep(_pubsub):
        rec.stop_reconciler()

    async def _no_pubsub():
        return None, None

    monkeypatch.setattr(rec, "register_builtin_adapters", lambda: None)
    monkeypatch.setattr(rec, "_subscribe_wake", _no_pubsub)
    monkeypatch.setattr(rec, "_run_cycle", _cycle)
    monkeypatch.setattr(rec, "_sleep_or_wake", _sleep)
    monkeypatch.setattr(rec, "_write_heartbeat", lambda: None)
    monkeypatch.setattr(rec, "_stop_lease_loss_work", _stop_work)
    monkeypatch.setattr(worker_presence, "publish", _publish)

    asyncio.run(rec.reconciler_loop())

    assert said == [worker_presence.STATE_DRAINING, "stop-work", worker_presence.STATE_STOPPED]


@pytest.mark.parametrize(
    ("adapter", "record", "waits"),
    [
        pytest.param("evaluation", {"state": "stopped", "stops_lease_loss_work": True}, False, id="said-stopped"),
        # A worker of the release before: its evaluation may still be writing after its last word.
        pytest.param("evaluation", {"state": "stopped"}, True, id="older-release"),
        # Gone without saying it stopped (a stall, a kill): it may still be writing.
        pytest.param(
            "evaluation", {"state": "draining", "stops_lease_loss_work": True, "lapsed": True}, True, id="lapsed"
        ),
        pytest.param("assignment", {"state": "stopped"}, False, id="other-adapter"),
    ],
)
def test_an_evaluation_is_taken_back_at_once_only_from_a_worker_that_stopped_it(adapter, record, waits):
    from app.services import orchestration_reconciler as rec

    adapters = [
        SimpleNamespace(name="evaluation", stop_on_lease_loss=True),
        SimpleNamespace(name="assignment", stop_on_lease_loss=False),
    ]
    assert rec.waits_for_lease_of(adapters)(adapter, record) is waits


# ── the next attempt's reclaim (PostgreSQL) ──


async def _entries(sm, count):
    async with sm() as s:
        dataset = Dataset(name=f"ds-{uuid.uuid4().hex[:6]}", family="STIX_BUNDLE")
        s.add(dataset)
        await s.flush()
        entries = [DatasetEntry(dataset_id=dataset.id, position=i, expected_answer="a") for i in range(count)]
        s.add_all(entries)
        await s.commit()
        return [e.id for e in entries]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_take_back_after_a_stop_resumes_the_runs_it_left_running(monkeypatch):
    """The finished entry stays, the cut one is redone, the run is dispatched again."""
    from app.services.evaluation_engine import execute_evaluation

    execution = uuid.uuid4()
    dispatched: dict[str, str] = {}

    async def _fake_execute_run(db, run_id, **_kw):
        run = await db.get(EvaluationRun, uuid.UUID(run_id))
        dispatched[run_id] = run.status
        run.status = "completed"
        run.completed_at = datetime.now(UTC)
        await db.commit()

    monkeypatch.setattr("app.services.evaluation_engine.execute_evaluation_run", _fake_execute_run)

    started = datetime.now(UTC) - timedelta(minutes=5)
    sm, cleanup = await _make_db_full()
    try:
        eval_id, (cut, unowned) = await _active_runs_fixture(
            sm,
            [
                ("running", execution, {"started_at": started, "completed_entries": 1}),
                ("running", None, {}),
            ],
        )
        done, in_flight = await _entries(sm, 2)
        async with sm() as s:
            s.add_all(
                [
                    EvaluationEpisode(
                        run_id=cut, entry_id=done, correlation_id=f"eval:{cut}:{done}", status="completed"
                    ),
                    EvaluationResult(run_id=cut, entry_id=done, agent_output="a", score=80),
                    # The entry the stop cut: an episode a commit inside the entry left behind.
                    EvaluationEpisode(
                        run_id=cut,
                        entry_id=in_flight,
                        correlation_id=f"eval:{cut}:{in_flight}",
                        status="running",
                    ),
                ]
            )
            await s.commit()

        async with sm() as s:
            handed_back = await execute_evaluation(
                s, str(eval_id), "manual", execution, worker_id=NO_LEASE, resume_stopped_runs=True
            )

        assert handed_back is False
        assert dispatched == {str(cut): "pending"}, "the run is dispatched again, from pending"
        async with sm() as s:
            run = await s.get(EvaluationRun, cut)
            assert run.status == "completed"
            assert run.error is None
            episodes = (
                (await s.execute(select(EvaluationEpisode).where(EvaluationEpisode.run_id == cut))).scalars().all()
            )
            assert [(e.entry_id, e.status) for e in episodes] == [(done, "completed")]
            results = (await s.execute(select(EvaluationResult).where(EvaluationResult.run_id == cut))).scalars().all()
            assert [r.entry_id for r in results] == [done]
            # An unowned run may be another release's: failed as before, never resumed.
            assert (await s.get(EvaluationRun, unowned)).status == "failed"
    finally:
        await cleanup()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_stopping_worker_hands_back_with_nothing_written_at_batch_end(monkeypatch):
    """A run paused by the stop (left pending) hands the whole execution back."""
    from app.services import evaluation_engine as ee

    execution = uuid.uuid4()
    headlines: list = []

    async def _paused_run(db, run_id, *, stopping=None):
        assert stopping is not None and stopping.is_set()

    async def _headlines(*args, **kwargs):
        headlines.append(args)

    monkeypatch.setattr(ee, "execute_evaluation_run", _paused_run)
    monkeypatch.setattr(ee, "_compute_run_headlines", _headlines)
    stopping = asyncio.Event()
    stopping.set()

    sm, cleanup = await _make_db_full()
    try:
        eval_id, (run_id,) = await _active_runs_fixture(sm, [("pending", execution, {})])
        async with sm() as s:
            handed_back = await ee.execute_evaluation(
                s, str(eval_id), "manual", execution, worker_id=NO_LEASE, stopping=stopping
            )

        assert handed_back is True
        assert headlines == []
        async with sm() as s:
            assert (await s.get(EvaluationRun, run_id)).status == "pending"
    finally:
        await cleanup()


# ── a real run paused at an entry boundary, then resumed (PostgreSQL) ──


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_run_paused_by_a_stop_resumes_from_its_finished_entries(monkeypatch):
    """Each entry is judged once across the pause, and the resumed run completes."""
    from app.services import evaluation_engine as ee

    stopping = asyncio.Event()
    judged: list[str] = []

    async def _judge(*args, **kwargs):
        judged.append("entry")
        # The stop arrives while the first entry is being judged: the run
        # pauses at the next boundary, before the second entry.
        stopping.set()
        return {
            "score": 80.0,
            "sub_scores": {k: 80 for k in ee.SUB_SCORE_WEIGHTS},
            "evaluator_reasoning": "ok",
            "is_judge_failure": False,
        }

    paused: dict = {}

    async def _then(session, run_id):
        run = await session.get(EvaluationRun, run_id)
        paused["status"] = run.status
        paused["started_at"] = run.started_at
        paused["judged"] = len(judged)
        await session.commit()
        # The next worker, with no stop: the run resumes from its finished entry.
        await ee.execute_evaluation_run(session, str(run_id))

    stored, results = await run_on_postgres(
        monkeypatch,
        scorer_selection=None,
        answers=["a", "a", "a"],
        judge=_judge,
        stopping=stopping,
        then=_then,
    )

    assert paused["status"] == "pending"
    assert paused["judged"] == 1
    assert stored.status == "completed"
    assert stored.completed_entries == 3
    assert stored.started_at == paused["started_at"], "a resumed run keeps its first start"
    assert len(judged) == 3, "no entry judged twice"
    assert len(results) == 3
    assert len({r.entry_id for r in results}) == 3


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_resumed_run_counts_what_it_finished_before_the_stop(monkeypatch):
    """The anomaly an entry raised before the pause is in the run's totals after the resume."""
    from app.services import evaluation_engine as ee

    stopping = asyncio.Event()

    async def _judge(*args, **kwargs):
        # Entry 0 raised (no judge); the stop comes while entry 1 is judged.
        stopping.set()
        return {
            "score": 80.0,
            "sub_scores": {k: 80 for k in ee.SUB_SCORE_WEIGHTS},
            "evaluator_reasoning": "ok",
            "is_judge_failure": False,
        }

    async def _then(session, run_id):
        assert (await session.get(EvaluationRun, run_id)).status == "pending"
        await session.commit()
        await ee.execute_evaluation_run(session, str(run_id))

    stored, results = await run_on_postgres(
        monkeypatch,
        scorer_selection=None,
        answers=[RuntimeError("provider down"), "a", "a", "a"],
        judge=_judge,
        stopping=stopping,
        then=_then,
    )

    anomalous = sum(r.execution_anomaly_kind is not None for r in results)
    assert anomalous >= 1, "precondition: the raised entry is an anomaly"
    assert len(results) == 4
    assert stored.anomaly_count == anomalous


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_resumed_run_keeps_the_settings_it_started_with(monkeypatch):
    """An edit to the evaluation during the pause applies to its next runs, not to the resumed one."""
    from app.models.evaluation import Evaluation
    from app.services import evaluation_engine as ee

    stopping = asyncio.Event()
    judged: list[str] = []

    async def _judge(*args, **kwargs):
        judged.append("entry")
        stopping.set()
        return {
            "score": 80.0,
            "sub_scores": {k: 80 for k in ee.SUB_SCORE_WEIGHTS},
            "evaluator_reasoning": "ok",
            "is_judge_failure": False,
        }

    async def _then(session, run_id):
        run = await session.get(EvaluationRun, run_id)
        evaluation = await session.get(Evaluation, run.evaluation_id)
        evaluation.scorer_selection = []  # judge switched off while the run is paused
        await session.commit()
        await ee.execute_evaluation_run(session, str(run_id))

    stored, _results = await run_on_postgres(
        monkeypatch,
        scorer_selection=None,
        answers=["a", "a", "a"],
        judge=_judge,
        stopping=stopping,
        then=_then,
    )

    assert len(judged) == 3, "the resumed entries are judged as the first one was"
    assert stored.scorer_selection is None, "the run keeps the selection it was stamped with"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_resume_keeps_the_closed_callback_episodes_that_store_no_result(monkeypatch):
    """A callback entry that failed or timed out stays as it is; only the cut entry is redone."""
    from app.services.evaluation_engine import execute_evaluation

    execution = uuid.uuid4()

    async def _fake_execute_run(db, run_id, **_kw):
        return None

    monkeypatch.setattr("app.services.evaluation_engine.execute_evaluation_run", _fake_execute_run)

    sm, cleanup = await _make_db_full()
    try:
        eval_id, (cut,) = await _active_runs_fixture(sm, [("running", execution, {"started_at": datetime.now(UTC)})])
        timed_out, failed, in_flight = await _entries(sm, 3)
        async with sm() as s:
            s.add_all(
                [
                    EvaluationEpisode(
                        run_id=cut, entry_id=timed_out, correlation_id=f"eval:{cut}:{timed_out}", status="timed_out"
                    ),
                    EvaluationEpisode(
                        run_id=cut, entry_id=failed, correlation_id=f"eval:{cut}:{failed}", status="failed"
                    ),
                    EvaluationEpisode(
                        run_id=cut, entry_id=in_flight, correlation_id=f"eval:{cut}:{in_flight}", status="running"
                    ),
                ]
            )
            await s.commit()

        async with sm() as s:
            await execute_evaluation(s, str(eval_id), "manual", execution, worker_id=NO_LEASE, resume_stopped_runs=True)

        async with sm() as s:
            episodes = (
                (await s.execute(select(EvaluationEpisode).where(EvaluationEpisode.run_id == cut))).scalars().all()
            )
            assert {(e.entry_id, e.status) for e in episodes} == {
                (timed_out, "timed_out"),
                (failed, "failed"),
            }
            assert (await s.get(EvaluationRun, cut)).status == "pending"
    finally:
        await cleanup()


async def _resume_after_a_stop(sm, eval_id, execution, run_id, stop_resumes):
    """Put *run_id* back ``running`` (the stop cut it), record *stop_resumes*, run the next attempt's reclaim."""
    from app.models.orchestration import OrchestrationExecution
    from app.services.evaluation_engine import STOP_RESUMES_PAYLOAD_KEY, execute_evaluation

    async with sm() as s:
        await s.execute(EvaluationRun.__table__.update().where(EvaluationRun.id == run_id).values(status="running"))
        row = await s.get(OrchestrationExecution, execution)
        row.payload = {"lease_expired": _TAKEN_BACK("stopped"), STOP_RESUMES_PAYLOAD_KEY: stop_resumes}
        await s.commit()
    async with sm() as s:
        await execute_evaluation(s, str(eval_id), "manual", execution, worker_id=NO_LEASE, resume_stopped_runs=True)
    async with sm() as s:
        return await s.get(EvaluationRun, run_id), (await s.get(OrchestrationExecution, execution)).payload


@pytest.mark.integration
@pytest.mark.asyncio
async def test_resumes_of_a_run_with_no_entry_finished_in_between_are_bounded(monkeypatch):
    """An entry longer than the time between rollouts is not redone, and paid, forever; hops are not stalls."""
    from app.models.orchestration import OrchestrationExecution
    from app.services.evaluation_engine import (
        MAX_STOP_RESUMES_WITHOUT_PROGRESS,
        STALLED_RUN_ERROR,
        STOP_RESUME_MIN_WORK_SECONDS,
        STOP_RESUMES_PAYLOAD_KEY,
    )

    async def _fake_execute_run(db, run_id, **_kw):
        return None

    monkeypatch.setattr("app.services.evaluation_engine.execute_evaluation_run", _fake_execute_run)
    worked = (datetime.now(UTC) - timedelta(seconds=STOP_RESUME_MIN_WORK_SECONDS + 60)).isoformat()
    hopped = (datetime.now(UTC) - timedelta(seconds=10)).isoformat()
    last_allowed = MAX_STOP_RESUMES_WITHOUT_PROGRESS - 1

    sm, cleanup = await _make_db_full()
    try:
        async with sm() as s:
            row = OrchestrationExecution(adapter="evaluation", state="running", payload={})
            s.add(row)
            await s.commit()
            execution = row.id
        eval_id, (cut, sibling) = await _active_runs_fixture(
            sm,
            [
                ("running", execution, {"started_at": datetime.now(UTC)}),
                ("completed", execution, {"started_at": datetime.now(UTC), "completed_at": datetime.now(UTC)}),
            ],
        )

        # A hop within one rolling restart, seconds after the last resume: not a stall.
        run, payload = await _resume_after_a_stop(
            sm, eval_id, execution, cut, {"runs": {str(cut): {"results": 0, "stalled": last_allowed, "at": hopped}}}
        )
        assert run.status == "pending"
        assert payload[STOP_RESUMES_PAYLOAD_KEY]["runs"][str(cut)]["stalled"] == last_allowed
        # Consumed: an attempt cut otherwise after it is not resumed on this stop.
        assert payload[STOP_RESUMES_PAYLOAD_KEY]["consumed"] == _AT

        # Minutes of work and still no result: one stall too many, failed with the reason.
        run, payload = await _resume_after_a_stop(
            sm, eval_id, execution, cut, {"runs": {str(cut): {"results": 0, "stalled": last_allowed, "at": worked}}}
        )
        assert run.status == "failed"
        assert run.error == STALLED_RUN_ERROR
        # Its count is dropped: a later resume of it starts afresh.
        assert str(cut) not in payload[STOP_RESUMES_PAYLOAD_KEY]["runs"]
        async with sm() as s:
            assert (await s.get(EvaluationRun, sibling)).status == "completed"
    finally:
        await cleanup()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_resume_with_no_run_left_pending_still_writes_the_batch_end(monkeypatch):
    """The stop cut the batch-end pass itself: its runs all finished, their headlines are still to compute."""
    from app.services import evaluation_engine as ee

    execution = uuid.uuid4()
    batches: list[list[str]] = []

    async def _headlines(_session, _eid, *, batch_run_ids):
        batches.append(sorted(batch_run_ids))

    monkeypatch.setattr(ee, "_compute_run_headlines", _headlines)

    sm, cleanup = await _make_db_full()
    try:
        now = datetime.now(UTC)
        eval_id, (first, second) = await _active_runs_fixture(
            sm,
            [
                ("completed", execution, {"started_at": now, "completed_at": now}),
                ("completed", execution, {"started_at": now, "completed_at": now}),
            ],
        )
        async with sm() as s:
            await ee.execute_evaluation(
                s, str(eval_id), "manual", execution, worker_id=NO_LEASE, resume_stopped_runs=True
            )

        assert batches == [sorted([str(first), str(second)])]
    finally:
        await cleanup()


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("holder", "writes"),
    [
        pytest.param("this-worker", True, id="lease-ours"),
        # Taken back (its stop said, or its presence lapsed in a stall): another worker resumes it.
        pytest.param("another-worker", False, id="lease-moved"),
    ],
)
async def test_a_run_writes_its_entries_only_while_the_lease_is_its_workers(monkeypatch, holder, writes):
    stored, results = await run_on_postgres(
        monkeypatch, scorer_selection=None, answers=["a", "a"], lease_holder=holder, lease_worker="this-worker"
    )

    if writes:
        assert stored.status == "completed"
        assert len(results) == 2
    else:
        assert results == [], "nothing written beside the worker that resumes it"
        assert stored.status == "running", "its status is the new worker's to write"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_run_cut_during_its_setup_resumes_under_the_evaluations_settings(monkeypatch):
    """Started is not stamped: a run cut before its settings were written has none to keep."""
    from app.models.evaluation import Evaluation
    from app.services import evaluation_engine as ee

    judged: list[str] = []

    async def _judge(*args, **kwargs):
        judged.append("entry")
        return {
            "score": 80.0,
            "sub_scores": {k: 80 for k in ee.SUB_SCORE_WEIGHTS},
            "evaluator_reasoning": "ok",
            "is_judge_failure": False,
        }

    async def _cut_during_setup(session, run_id):
        # What a stop during the setup leaves: started, back to pending, no stamp written.
        run = await session.get(EvaluationRun, run_id)
        run.status, run.started_at = "pending", datetime.now(UTC)
        run.execution_mode = run.scorer_selection = None
        evaluation = await session.get(Evaluation, run.evaluation_id)
        evaluation.scorer_selection = []  # judge off
        await session.commit()

    stored, _results = await run_on_postgres(
        monkeypatch,
        scorer_selection=[],
        answers=["a", "a"],
        judge=_judge,
        before_run=_cut_during_setup,
    )

    assert judged == [], "the evaluation's judge-off holds: an unwritten stamp is not a judge-on one"
    assert stored.scorer_selection == []
