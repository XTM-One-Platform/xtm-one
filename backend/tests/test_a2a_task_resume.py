"""An external A2A task cut by a graceful restart resumes instead of failing.

Gap a2a-task-no-resume-after-restart (#1657): partner tasks cut by a rollout
ended ``failed`` with "send the task again". A run its stopping worker cuts is
now handed back (``pending`` with the hand-back recorded, the lease refreshed),
the next live worker's reaper sweep resumes it from its checkpoint under the
same task id, and the resume bounds of assignment runs apply.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.a2a import tasks as task_svc
from app.services import background_checkpoint
from tests import a2a_server_stubs as stubs

pytestmark = pytest.mark.asyncio


class _Result:
    def __init__(self, value):
        self._value = value
        self.rowcount = 1

    def scalar_one_or_none(self):
        return self._value

    def all(self):
        return self._value


class _DB:
    def __init__(self, *answers):
        self._answers = list(answers)
        self.statements: list = []
        self.commits = 0

    async def execute(self, stmt):
        self.statements.append(stmt)
        return _Result(self._answers.pop(0) if self._answers else None)

    async def flush(self):
        pass

    async def commit(self):
        self.commits += 1


class _Ctx:
    def __init__(self, db):
        self._db = db

    async def __aenter__(self):
        return self._db

    async def __aexit__(self, *_exc):
        return False


class _Checkpoint:
    def __init__(self):
        self.closed: list[bool] = []

    def offer(self, _messages):
        return None

    async def aclose(self, *, flush: bool):
        self.closed.append(flush)


@pytest.fixture(autouse=True)
def _no_delegated_work(monkeypatch):
    stubs.silence_delegated_work(monkeypatch, task_svc)


@pytest.fixture
def stopping(monkeypatch):
    from app.core import worker_shutdown

    monkeypatch.setattr(worker_shutdown, "_request", None)
    worker_shutdown.note_shutdown(worker_shutdown.STOP, "SIGTERM")
    yield
    monkeypatch.setattr(worker_shutdown, "_request", None)


class TestTheAttemptAndTheHandBack:
    def test_a_fresh_run_is_attempt_one(self):
        assert task_svc._attempt_of({"client_id": "p"}) == 1
        assert task_svc._handed_back({"client_id": "p"}) is None

    def test_a_hand_back_gives_its_attempt_back(self):
        context = background_checkpoint.handed_back({}, attempt=2, signal="SIGTERM")
        assert task_svc._handed_back(context)["signal"] == "SIGTERM"
        assert task_svc._attempt_of(context) == 2

    def test_the_resume_is_recorded_graceful_and_clears_the_hand_back(self):
        context = background_checkpoint.handed_back({"client_id": "p"}, attempt=1, signal="SIGTERM")
        replay = type("Replay", (), {"replayed_calls": 4, "turn_id": "attempt-1", "completed_calls": 4})()
        resumed = task_svc._record_resume(context, attempt=1, replay=replay)
        assert task_svc._handed_back(resumed) is None
        assert resumed["client_id"] == "p"
        (entry,) = background_checkpoint.resumes_of(resumed)
        assert entry["graceful"] is True and entry["replayed_calls"] == 4
        assert task_svc._attempt_of(resumed) == 2


class TestAStopHandsTheRunBack:
    async def test_a_running_run_goes_back_to_pending_with_its_hand_back(self, monkeypatch, stopping):
        run = type("R", (), {"status": "running", "trigger_context": {"client_id": "p"}})()
        db = _DB(None, run)
        monkeypatch.setattr("app.core.database.async_session", lambda: _Ctx(db))
        checkpoint = _Checkpoint()

        handed = await task_svc._hand_back(uuid.uuid4(), uuid.uuid4(), checkpoint, attempt=1)

        assert handed is True
        assert checkpoint.closed == [True]
        assert db.commits == 1
        written = db.statements[-1].compile().params
        assert written["status"] == "pending"
        assert written["trigger_context"][background_checkpoint.HANDED_BACK_KEY]["signal"] == "SIGTERM"

    async def test_a_run_no_longer_running_is_not_handed_back(self, monkeypatch, stopping):
        run = type("R", (), {"status": "cancelled", "trigger_context": {}})()
        db = _DB(None, run)
        monkeypatch.setattr("app.core.database.async_session", lambda: _Ctx(db))

        assert await task_svc._hand_back(uuid.uuid4(), uuid.uuid4(), _Checkpoint(), attempt=1) is False
        assert db.commits == 0

    async def test_a_failed_write_falls_back_to_the_failure(self, monkeypatch, stopping):
        class _Broken(_Checkpoint):
            async def aclose(self, *, flush: bool):
                raise RuntimeError("redis down")

        assert await task_svc._hand_back(uuid.uuid4(), uuid.uuid4(), _Broken(), attempt=1) is False


class TestTheExecutorHandsBackInsteadOfFailing:
    async def test_a_cut_run_is_handed_back_and_not_finalised(self, monkeypatch, stopping):
        agent_id, run_id = uuid.uuid4(), uuid.uuid4()
        task = type(
            "T",
            (),
            {
                "id": uuid.uuid4(),
                "run_id": run_id,
                "exposure_id": uuid.uuid4(),
                "context_id": uuid.uuid4(),
                "caller_question": None,
                "clarification_count": 0,
                "lease_heartbeat_at": None,
            },
        )()
        run = type(
            "R",
            (),
            {
                "id": run_id,
                "status": "pending",
                "agent_id": agent_id,
                "started_at": None,
                "trigger_context": {"client_id": "partner-1"},
                "trigger_payload": {"message": "go"},
            },
        )()
        exposure = type("E", (), {"id": task.exposure_id, "revoked_at": None, "run_as_user_id": uuid.uuid4()})()
        agent = type(
            "A",
            (),
            {"id": agent_id, "name": "Exposed", "builtin_tool_ids": [], "handover_agent_ids": [], "handover_types": {}},
        )()
        finalised: list = []
        handed: list = []

        async def _fake_chat(**kwargs):
            assert kwargs["on_checkpoint"] is not None
            raise asyncio.CancelledError()

        async def _fake_finalise(*_a, **_k):
            finalised.append(_a)

        async def _fake_hand_back(task_id, rid, checkpoint, *, attempt):
            handed.append((rid, attempt))
            return True

        async def _noop(*_a, **_k):
            return None

        from app.services import assignment_run_resume

        monkeypatch.setattr("app.services.agent_chat.run_agent_chat", _fake_chat)
        monkeypatch.setattr(task_svc, "_supervise_run", _noop)
        monkeypatch.setattr(task_svc, "_finalise_run", _fake_finalise)
        monkeypatch.setattr(task_svc, "_hand_back", _fake_hand_back)
        monkeypatch.setattr(assignment_run_resume, "checkpoint_for", lambda *a, **k: _Checkpoint())
        monkeypatch.setattr("app.core.database.async_session", lambda: _Ctx(_DB(task, run, exposure, agent)))

        with pytest.raises(asyncio.CancelledError):
            await task_svc.execute_task_run(task.id)

        assert handed == [(run_id, 1)]
        assert finalised == []

    async def test_a_resume_past_its_bounds_fails_with_their_reason(self, monkeypatch):
        agent_id, run_id = uuid.uuid4(), uuid.uuid4()
        task = type(
            "T",
            (),
            {
                "id": uuid.uuid4(),
                "run_id": run_id,
                "exposure_id": uuid.uuid4(),
                "context_id": uuid.uuid4(),
                "caller_question": None,
                "clarification_count": 0,
                "lease_heartbeat_at": None,
            },
        )()
        context = background_checkpoint.handed_back({"client_id": "p"}, attempt=1, signal="SIGTERM")
        run = type(
            "R",
            (),
            {
                "id": run_id,
                "status": "pending",
                "agent_id": agent_id,
                "started_at": None,
                "trigger_context": context,
                "trigger_payload": {"message": "go"},
            },
        )()
        exposure = type("E", (), {"id": task.exposure_id, "revoked_at": None, "run_as_user_id": uuid.uuid4()})()
        agent = type("A", (), {"id": agent_id, "name": "Exposed"})()
        finalised: list = []

        async def _fake_finalise(_db, _task, _run_id, status, summary, error, **_k):
            finalised.append((status, error))

        async def _replay(_run_id, **_k):
            return type(
                "Replay",
                (),
                {"completed_calls": 0, "history": [], "replayed_calls": 0, "turn_id": "attempt-1", "resumed": None},
            )()

        async def _discard(*_a, **_k):
            return None

        from app.services import assignment_run_resume

        monkeypatch.setattr(task_svc, "_finalise_run", _fake_finalise)
        monkeypatch.setattr(assignment_run_resume, "load_replay", _replay)
        monkeypatch.setattr(assignment_run_resume, "discard", _discard)
        monkeypatch.setattr(assignment_run_resume, "resume_refused", lambda *a, **k: "resume cap reached")
        monkeypatch.setattr("app.core.database.async_session", lambda: _Ctx(_DB(task, run, exposure, agent)))

        await task_svc.execute_task_run(task.id)

        assert finalised == [("failed", "resume cap reached")]


class TestTheSweepResumesHandedBackTasks:
    async def test_a_settled_hand_back_is_due(self, monkeypatch):
        from app.core import worker_shutdown

        monkeypatch.setattr(worker_shutdown, "_request", None)
        old = datetime.now(UTC) - timedelta(seconds=60)
        fresh = datetime.now(UTC)
        due_id, fresh_id = uuid.uuid4(), uuid.uuid4()
        rows = [
            (due_id, background_checkpoint.handed_back({}, attempt=1, signal="SIGTERM", now=old)),
            (fresh_id, background_checkpoint.handed_back({}, attempt=1, signal="SIGTERM", now=fresh)),
        ]
        assert await task_svc.resume_handed_back_tasks(_DB(rows)) == [due_id]

    async def test_a_stopping_worker_resumes_nothing(self, stopping):
        db = _DB([(uuid.uuid4(), {})])
        assert await task_svc.resume_handed_back_tasks(db) == []
        assert db.statements == []
