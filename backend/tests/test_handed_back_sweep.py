"""A run handed back by a rollout that no worker resumes is nudged, then failed (#11576).

Before: a run whose stopping worker handed it back stayed ``running`` with its
work items held; when its execution was never claimed again nothing ended it,
the stale-run sweep skipping any run whose execution is pending or running.
"""

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.services import handed_back_sweep as sweep
from app.services.orchestration_adapters.assignment import execution_dedup_key

NOW = datetime(2026, 11, 13, 10, 0, tzinfo=UTC)


def _decide(age_seconds, state, lease_owner=None):
    return sweep.decide(
        since=NOW - timedelta(seconds=age_seconds),
        now=NOW,
        execution_state=state,
        lease_owner=lease_owner,
        resume_seconds=180,
        orphan_seconds=300,
    ).decision


def test_a_fresh_hand_back_waits_for_its_resume():
    assert _decide(60, "pending") == "wait"
    assert _decide(60, None) == "wait"


def test_a_pending_execution_past_the_resume_bound_is_nudged():
    assert _decide(200, "pending") == "nudge"


def test_a_leased_execution_is_being_resumed():
    assert _decide(1000, "running", lease_owner="worker-1") == "wait"


def test_past_the_orphan_bound_the_run_is_failed():
    assert _decide(301, "pending") == "fail"
    assert _decide(301, "running", lease_owner=None) == "fail"


def test_a_missing_or_terminal_execution_fails_past_the_resume_bound():
    assert _decide(200, None) == "fail"
    assert _decide(200, "dead_letter") == "fail"
    assert _decide(200, "cancelled") == "fail"


def test_handed_back_at_reads_the_mark():
    at = NOW.isoformat()
    assert sweep.handed_back_at({"handed_back": {"at": at, "attempt": 1}}) == NOW
    assert sweep.handed_back_at({"handed_back": {"at": "nonsense"}}) is None
    assert sweep.handed_back_at({}) is None
    assert sweep.handed_back_at(None) is None


class _Scalars:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _Session:
    def __init__(self, runs, executions, present_keys=()):
        self._results = [_Scalars(runs), _Scalars(executions), _Scalars([(k,) for k in present_keys])]
        self.committed = False

    async def execute(self, _statement):
        return self._results.pop(0)

    async def refresh(self, _obj, **_kw):
        return None

    async def commit(self):
        self.committed = True


def _run(age_seconds):
    return SimpleNamespace(
        id=uuid.uuid4(),
        status="running",
        error=None,
        completed_at=None,
        trigger_context={"handed_back": {"at": (NOW - timedelta(seconds=age_seconds)).isoformat(), "attempt": 1}},
    )


def _execution(run, state, lease_owner=None, payload=None):
    return SimpleNamespace(
        id=uuid.uuid4(),
        dedup_key=execution_dedup_key(run.id),
        state=state,
        lease_owner=lease_owner,
        payload=payload or {},
        run_at=NOW - timedelta(hours=1),
        updated_at=None,
    )


@pytest.fixture
def engine_calls(monkeypatch):
    calls = {"cancelled": [], "woken": 0}

    async def _cancel(_db, *, execution_id):
        calls["cancelled"].append(execution_id)

    async def _wake():
        calls["woken"] += 1

    monkeypatch.setattr("app.services.orchestration_engine.cancel_execution", _cancel)
    monkeypatch.setattr("app.services.orchestration_engine.notify_orchestration_wake", _wake)
    monkeypatch.setattr(sweep, "_bounds", lambda: (180.0, 300.0))
    return calls


@pytest.mark.asyncio
async def test_a_stranded_run_is_failed_and_its_execution_cancelled(engine_calls):
    run = _run(400)
    execution = _execution(run, "pending", payload={"not_release": {"version": "old", "until": "x"}})
    db = _Session([run], [execution])

    nudged, failed = await sweep.fail_stranded_hand_backs(db, now=NOW)

    assert (nudged, failed) == (0, 1)
    assert run.status == "failed"
    assert run.error.startswith(sweep.STRANDED_HAND_BACK_ERROR)
    assert run.completed_at == NOW
    assert engine_calls["cancelled"] == [execution.id]
    assert db.committed


@pytest.mark.asyncio
async def test_a_late_pending_execution_is_made_due_and_workers_woken(engine_calls):
    run = _run(200)
    execution = _execution(run, "pending", payload={"not_release": {"version": "old"}, "claim_for": {}, "keep": 1})
    db = _Session([run], [execution])

    nudged, failed = await sweep.fail_stranded_hand_backs(db, now=NOW)

    assert (nudged, failed) == (1, 0)
    assert run.status == "running"
    assert execution.run_at == NOW
    assert execution.payload == {"keep": 1}
    assert engine_calls["woken"] == 1


@pytest.mark.asyncio
async def test_a_row_a_worker_holds_now_is_left_to_it(engine_calls):
    run = _run(400)
    db = _Session([run], [], present_keys=[execution_dedup_key(run.id)])

    assert await sweep.fail_stranded_hand_backs(db, now=NOW) == (0, 0)
    assert run.status == "running"


@pytest.mark.asyncio
async def test_a_run_not_handed_back_is_never_read_further(engine_calls):
    run = SimpleNamespace(id=uuid.uuid4(), status="running", trigger_context={"resumes": []})
    db = _Session([run], [])

    assert await sweep.fail_stranded_hand_backs(db, now=NOW) == (0, 0)
    assert run.status == "running"
