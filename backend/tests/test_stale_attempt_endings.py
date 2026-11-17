"""A stale attempt never writes its ending over another one.

An attempt whose lease was lost keeps running while the orchestration engine
re-runs the work, and the dead-letter or a cancel can end it meanwhile. The
first ending stands: the stale attempt discards its own, and what follows it
(notification, chain, retry, delivery).
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import tests.test_assignment_stale_reference as harness
from app.tools.base import HumanInputRequestedError
from tests.agentic_gate_stub import stub_agentic_gate


@pytest.fixture(autouse=True)
def _agentic_ai_available(monkeypatch):
    """Both bodies ask the background gate first; these tests are about
    how they end."""
    stub_agentic_gate(monkeypatch)


# ── Assignment runs ──────────────────────────────────────────────────────────


def _is_run_status_read(stmt) -> bool:
    descriptions = getattr(stmt, "column_descriptions", None) or []
    return (
        [d.get("name") for d in descriptions] == ["status", "trigger_context"]
        and getattr(descriptions[0].get("entity"), "__tablename__", None) == "assignment_runs"
    )


def _status_session(
    row_status: str,
    row_context: dict | None = None,
    *,
    at_start: tuple[str, dict | None] | None = None,
):
    """The engine harness's session, answering the ending checks with
    *row_status* and *row_context* (what another writer committed meanwhile)
    and, for a re-run (the only attempt that checks as it starts), its start
    check with *at_start*."""

    class _Session(harness._EngineSession):
        status_reads: list = []

        async def execute(self, stmt, *args, **kwargs):
            if _is_run_status_read(stmt):
                first = at_start is not None and not type(self).status_reads
                answer = at_start if first else (row_status, row_context)
                type(self).status_reads.append(stmt)
                return harness._EngineResult(rows=[answer])
            return await super().execute(stmt, *args, **kwargs)

    return _Session


def _events(mocks) -> list:
    return [call.args[0] for call in mocks["event_bus"].emit.await_args_list]


def _for_update(stmt) -> bool:
    return getattr(stmt, "_for_update_arg", None) is not None


@pytest.mark.asyncio
async def test_a_run_another_attempt_completed_is_not_completed_again(monkeypatch):
    from app.services.event_bus import ASSIGNMENT_RUN_COMPLETED

    session_cls = _status_session("completed")
    monkeypatch.setattr(harness, "_EngineSession", session_cls)

    run, _session, mocks = await harness._drive_run(monkeypatch, report_integration_id=None)

    assert run.status != "completed"
    assert run.completed_at is None
    assert ASSIGNMENT_RUN_COMPLETED not in _events(mocks)
    mocks["create_execution"].assert_not_awaited()
    # Read under the row lock, so no other ending lands before this commit.
    assert session_cls.status_reads and all(_for_update(s) for s in session_cls.status_reads)


@pytest.mark.asyncio
async def test_a_failure_does_not_overwrite_the_ending_of_the_attempt_that_replaced_it(monkeypatch):
    from app.services.event_bus import ASSIGNMENT_RUN_FAILED

    monkeypatch.setattr(harness, "_EngineSession", _status_session("completed"))

    def _boom():
        raise RuntimeError("provider exploded")

    run, _session, mocks = await harness._drive_run(
        monkeypatch, report_integration_id=None, on_loop=_boom
    )

    assert run.status != "failed"
    assert run.error is None
    assert run.failure_cause is None
    assert ASSIGNMENT_RUN_FAILED not in _events(mocks)
    # No retry of a run someone else already ended.
    mocks["create_execution"].assert_not_awaited()


@pytest.mark.asyncio
async def test_a_run_the_dead_letter_failed_is_not_paused_for_a_human(monkeypatch):
    monkeypatch.setattr(harness, "_EngineSession", _status_session("failed"))
    notify = AsyncMock()
    monkeypatch.setattr("app.services.notify.deliver_notification", notify)

    def _ask():
        raise HumanInputRequestedError("Approve?", "call-1", [])

    _run, session, _mocks = await harness._drive_run(
        monkeypatch, report_integration_id=None, on_loop=_ask
    )

    notify.assert_not_awaited()
    assert session.commits == 1  # the "running" commit, never the pause


@pytest.mark.asyncio
async def test_a_run_another_attempt_paused_is_neither_paused_again_nor_completed(monkeypatch):
    """Every attempt commits ``running`` before its loop, so a pause on the row
    at this attempt's ending is another attempt's: its question and its
    notification stand."""
    from app.services.event_bus import ASSIGNMENT_RUN_COMPLETED

    monkeypatch.setattr(harness, "_EngineSession", _status_session("awaiting_human"))
    notify = AsyncMock()
    monkeypatch.setattr("app.services.notify.deliver_notification", notify)

    def _ask():
        raise HumanInputRequestedError("Approve again?", "call-2", [])

    run, session, _mocks = await harness._drive_run(
        monkeypatch, report_integration_id=None, on_loop=_ask
    )
    notify.assert_not_awaited()
    assert session.commits == 1  # the "running" commit, never a second pause

    monkeypatch.setattr(harness, "_EngineSession", _status_session("awaiting_human"))
    run, _session, mocks = await harness._drive_run(monkeypatch, report_integration_id=None)
    assert run.status != "completed"
    assert ASSIGNMENT_RUN_COMPLETED not in _events(mocks)


@pytest.mark.asyncio
async def test_a_failure_after_this_attempt_paused_the_run_is_recorded_as_before(monkeypatch):
    """This attempt's own pause, read back after its commit, is not another
    attempt's ending."""
    from app.services import assignment_engine

    monkeypatch.setattr(harness, "_EngineSession", _status_session("awaiting_human"))
    calls: list = []
    real = assignment_engine._ended_elsewhere

    async def _first_says_live(db, run, **kwargs):
        if kwargs.get("starting"):
            return await real(db, run, **kwargs)
        calls.append(run.status)
        if len(calls) == 1:
            return None
        return await real(db, run, **kwargs)

    monkeypatch.setattr(assignment_engine, "_ended_elsewhere", _first_says_live)
    cleanup = "app.services.builtin_tools.cleanup_mcp_manager"
    # Fails once, right after the pause committed; the teardown's call passes.
    monkeypatch.setattr(cleanup, AsyncMock(side_effect=[RuntimeError("mcp gone"), None]))

    class _KeepCleanup:
        """The harness's monkeypatch, leaving the failing cleanup in place."""

        def setattr(self, target, *args, **kwargs):
            if target != cleanup:
                monkeypatch.setattr(target, *args, **kwargs)

    def _ask():
        raise HumanInputRequestedError("Approve?", "call-1", [])

    run, _session, _mocks = await harness._drive_run(
        _KeepCleanup(), report_integration_id=None, on_loop=_ask
    )

    assert len(calls) == 1, "not asked again after its own pause"
    assert run.status == "failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("fails", [False, True])
async def test_an_attempt_a_rerun_replaced_writes_no_ending(monkeypatch, fails):
    """Attempt 1 finishes, or fails, while attempt 2 - the one the engine runs
    the run with now - is still running: the run ends as attempt 2 ends."""
    from app.services.attempt_charges import QUOTA_ATTEMPT_FENCE_KEY
    from app.services.event_bus import ASSIGNMENT_RUN_COMPLETED, ASSIGNMENT_RUN_FAILED

    monkeypatch.setattr(
        harness,
        "_EngineSession",
        _status_session("running", {"type": "schedule", QUOTA_ATTEMPT_FENCE_KEY: [0, 2]}),
    )

    def _boom():
        raise RuntimeError("provider exploded")

    run, _session, mocks = await harness._drive_run(
        monkeypatch, report_integration_id=None, on_loop=_boom if fails else None, attempt=1
    )

    assert run.status not in ("completed", "failed")
    assert ASSIGNMENT_RUN_COMPLETED not in _events(mocks)
    assert ASSIGNMENT_RUN_FAILED not in _events(mocks)
    mocks["create_execution"].assert_not_awaited()


@pytest.mark.asyncio
async def test_the_attempt_the_fence_names_ends_the_run(monkeypatch):
    from app.services.attempt_charges import QUOTA_ATTEMPT_FENCE_KEY

    monkeypatch.setattr(
        harness,
        "_EngineSession",
        _status_session("running", {"type": "schedule", QUOTA_ATTEMPT_FENCE_KEY: [0, 2]}),
    )

    run, _session, _mocks = await harness._drive_run(
        monkeypatch, report_integration_id=None, attempt=2
    )

    assert run.status == "completed", run.error


@pytest.mark.asyncio
async def test_a_live_row_ends_as_before(monkeypatch):
    from app.services.event_bus import ASSIGNMENT_RUN_COMPLETED

    monkeypatch.setattr(harness, "_EngineSession", _status_session("running"))

    run, _session, mocks = await harness._drive_run(monkeypatch, report_integration_id=None)

    assert run.status == "completed", run.error
    assert run.completed_at is not None
    assert ASSIGNMENT_RUN_COMPLETED in _events(mocks)


@pytest.mark.asyncio
async def test_a_failure_after_this_attempt_set_its_own_ending_is_still_recorded(monkeypatch):
    """Once this attempt checked and set its ending, the row is locked and its
    own flushed status reads back as terminal: that is not another ending."""
    from app.services import assignment_engine

    monkeypatch.setattr(harness, "_EngineSession", _status_session("running"))
    monkeypatch.setattr(
        assignment_engine,
        "_link_run_output_files",
        AsyncMock(side_effect=RuntimeError("storage down")),
    )
    reads: list[str] = []
    real = assignment_engine._ended_elsewhere

    async def _spy(db, run, **kwargs):
        if not kwargs.get("starting"):
            reads.append(run.status)
        return await real(db, run, **kwargs)

    monkeypatch.setattr(assignment_engine, "_ended_elsewhere", _spy)

    run, _session, _mocks = await harness._drive_run(monkeypatch, report_integration_id=None)

    assert run.status == "failed"
    assert "storage down" in (run.error or "")
    assert len(reads) == 1  # asked once, before this attempt's ending


@pytest.mark.asyncio
async def test_an_unreadable_status_raises_instead_of_writing_an_ending(monkeypatch):
    """A failed statement leaves the transaction unusable: the attempt could
    not commit an ending anyway, so the read raises to the engine, which
    retries the execution, instead of pretending nothing happened."""
    sessions: list = []

    class _Session(harness._EngineSession):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            sessions.append(self)

        async def execute(self, stmt, *args, **kwargs):
            if _is_run_status_read(stmt):
                raise RuntimeError("connection reset")
            return await super().execute(stmt, *args, **kwargs)

    monkeypatch.setattr(harness, "_EngineSession", _Session)

    objects = harness._engine_objects()
    with pytest.raises(RuntimeError, match="connection reset"):
        await harness._drive_run(
            monkeypatch, report_integration_id=None, engine_objects=objects
        )

    run = objects[0]
    assert run.status == "running", "neither completed nor failed"
    assert run.completed_at is None
    assert sessions


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["completed", "failed", "skipped"])
async def test_a_run_ended_before_its_rerun_started_is_left_alone(monkeypatch, status):
    """The lost attempt ended the run between the re-run's load and its start
    check; every ending the re-run could write before its loop is written
    under that check's lock."""
    session_cls = _status_session("running", at_start=(status, None))
    monkeypatch.setattr(harness, "_EngineSession", session_cls)

    _run, session, mocks = await harness._drive_run(
        monkeypatch, report_integration_id=None, attempt=2
    )

    assert session.commits == 1, "its place on the run, never the running commit"
    assert _events(mocks) == []
    assert len(session_cls.status_reads) == 1
    assert _for_update(session_cls.status_reads[0])


@pytest.mark.asyncio
async def test_an_attempt_replaced_before_it_started_does_nothing(monkeypatch):
    from app.services.attempt_charges import QUOTA_ATTEMPT_FENCE_KEY

    fenced = {"type": "schedule", QUOTA_ATTEMPT_FENCE_KEY: [0, 3]}
    monkeypatch.setattr(
        harness, "_EngineSession", _status_session("running", fenced, at_start=("running", fenced))
    )

    _run, session, mocks = await harness._drive_run(
        monkeypatch, report_integration_id=None, attempt=2
    )

    assert session.commits == 1, "its (older) place only, never the running commit"
    assert _events(mocks) == []


@pytest.mark.asyncio
async def test_a_resume_starts_from_its_pause(monkeypatch):
    """A paused run is where its resume starts, not an ending."""
    from app.services.event_bus import ASSIGNMENT_RUN_COMPLETED

    monkeypatch.setattr(
        harness,
        "_EngineSession",
        _status_session("running", at_start=("awaiting_human", None)),
    )

    run, _session, mocks = await harness._drive_run(
        monkeypatch, report_integration_id=None, attempt=2
    )

    assert run.status == "completed", run.error
    assert ASSIGNMENT_RUN_COMPLETED in _events(mocks)


# ── Delegated tasks ──────────────────────────────────────────────────────────


class _SessionCM:
    def __init__(self, db):
        self._db = db

    async def __aenter__(self):
        return self._db

    async def __aexit__(self, *_exc):
        return False


def _result(obj):
    res = MagicMock()
    res.scalar_one_or_none = MagicMock(return_value=obj)
    return res


def _groups():
    res = MagicMock()
    res.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
    return res


def _task(status: str, **fields) -> MagicMock:
    task = MagicMock()
    task.conversation_id = None
    task.metadata_json = {}
    task.status = status
    task.result = fields.get("result")
    task.error = fields.get("error")
    return task


def _agent() -> MagicMock:
    agent = MagicMock()
    agent.handover_agent_ids = []
    agent.disable_chat = True
    agent.max_iterations = 25
    return agent


async def _run_task(*, reread, run_side_effect, failure_row=None, attempt=None):
    """Drive ``_execute_sub_agent_task``: the row is running when the attempt
    starts, and *reread* is what it holds when the attempt ends."""
    from app.services.sub_agent import _execute_sub_agent_task

    db = MagicMock()
    db.execute = AsyncMock(
        side_effect=[_result(_task("running")), _groups(), _result(_agent()), _result(reread)]
    )
    db.commit = AsyncMock()
    sessions = [db]
    if failure_row is not None:
        failure_db = MagicMock()
        failure_db.execute = AsyncMock(return_value=_result(failure_row))
        failure_db.commit = AsyncMock()
        sessions.append(failure_db)
    it = iter(sessions)
    delegation = AsyncMock()
    with (
        patch("app.core.database.async_session", lambda: _SessionCM(next(it))),
        patch("app.services.agent_chat.run_agent_chat", new=AsyncMock(side_effect=run_side_effect)),
        patch("app.services.sub_agent._update_task_delegation", new=delegation),
        patch("app.services.sub_agent.publish_task_event", new=AsyncMock()),
        patch(
            "app.services.sub_agent_files.write_task_result_file_ids",
            new=AsyncMock(return_value=True),
        ),
    ):
        outcome = await _execute_sub_agent_task(
            task_id=str(uuid.uuid4()),
            user_id=str(uuid.uuid4()),
            target_agent_id=str(uuid.uuid4()),
            task_description="export Q1",
            file_context=None,
            attempt=attempt,
        )
    return outcome, sessions, delegation


@pytest.mark.asyncio
async def test_a_task_the_rerun_completed_keeps_the_rerun_result():
    done = _task("completed", result="the re-run's answer")

    outcome, _sessions, delegation = await _run_task(
        reread=done, run_side_effect=AsyncMock(return_value=("stale answer", None))
    )

    assert outcome.status == "completed"
    assert done.status == "completed"
    assert done.result == "the re-run's answer"
    assert [call.args[2] for call in delegation.await_args_list] == ["running"]


@pytest.mark.asyncio
async def test_a_task_the_dead_letter_failed_is_not_completed_by_the_stale_attempt():
    failed = _task("failed", error="worker lost")

    outcome, _sessions, _delegation = await _run_task(
        reread=failed, run_side_effect=AsyncMock(return_value=("stale answer", None))
    )

    assert outcome.status == "failed"
    assert outcome.error == "worker lost"
    assert failed.status == "failed"
    assert failed.result is None


@pytest.mark.asyncio
async def test_a_timeout_does_not_fail_a_task_another_attempt_completed():
    done = _task("completed", result="the re-run's answer")

    outcome, sessions, _delegation = await _run_task(
        reread=None,
        run_side_effect=AsyncMock(side_effect=TimeoutError),
        failure_row=done,
    )

    assert outcome.status == "completed"
    assert done.status == "completed"
    assert done.error is None
    sessions[1].commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_exception_does_not_rewrite_the_dead_letter_error():
    failed = _task("failed", error="worker lost")

    outcome, sessions, _delegation = await _run_task(
        reread=None,
        run_side_effect=AsyncMock(side_effect=RuntimeError("provider down")),
        failure_row=failed,
    )

    assert outcome.status == "failed"
    assert failed.error == "worker lost"
    sessions[1].commit.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["completed", "failed", "cancelled", "incomplete"])
async def test_a_stale_attempts_files_never_replace_those_of_the_ending_that_stands(status):
    from app.services.sub_agent_files import RESULT_FILE_IDS_KEY, write_task_result_file_ids

    winner = str(uuid.uuid4())
    row = SimpleNamespace(status=status, metadata_json={RESULT_FILE_IDS_KEY: [winner]})
    db = MagicMock()
    db.execute = AsyncMock(return_value=_result(row))
    db.commit = AsyncMock()

    with patch("app.core.database.async_session", lambda: _SessionCM(db)):
        assert await write_task_result_file_ids(str(uuid.uuid4()), [str(uuid.uuid4())]) is False

    assert row.metadata_json == {RESULT_FILE_IDS_KEY: [winner]}
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_task_attempt_a_rerun_replaced_writes_no_result():
    from app.services.sub_agent import REPLACED_ATTEMPT_ERROR
    from app.services.sub_agent_files import TASK_ATTEMPT_KEY

    live = _task("running")
    live.metadata_json = {TASK_ATTEMPT_KEY: 2}

    outcome, _sessions, delegation = await _run_task(
        reread=live, run_side_effect=AsyncMock(return_value=("stale answer", None)), attempt=1
    )

    assert (outcome.status, outcome.error) == ("failed", REPLACED_ATTEMPT_ERROR)
    assert live.status == "running" and live.result is None
    assert [call.args[2] for call in delegation.await_args_list] == ["running"]


@pytest.mark.asyncio
async def test_a_replaced_attempts_files_never_replace_those_of_the_rerun():
    from app.services.sub_agent_files import (
        RESULT_FILE_IDS_KEY,
        TASK_ATTEMPT_KEY,
        write_task_result_file_ids,
    )

    rerun, stale, more = (str(uuid.uuid4()) for _ in range(3))
    row = SimpleNamespace(
        status="running", metadata_json={TASK_ATTEMPT_KEY: 2, RESULT_FILE_IDS_KEY: [rerun]}
    )
    db = MagicMock()
    db.execute = AsyncMock(return_value=_result(row))
    db.commit = AsyncMock()

    with patch("app.core.database.async_session", lambda: _SessionCM(db)):
        assert await write_task_result_file_ids(str(uuid.uuid4()), [stale], place=(0, 1)) is False
        assert await write_task_result_file_ids(str(uuid.uuid4()), [rerun, more], place=(0, 2))

    assert row.metadata_json[RESULT_FILE_IDS_KEY] == [rerun, more]
    assert row.metadata_json[TASK_ATTEMPT_KEY] == 2


def test_a_rerun_records_its_attempt_on_the_task_as_it_starts():
    import inspect

    from app.services import sub_agent

    src = inspect.getsource(sub_agent._execute_sub_agent_task)
    credit = src.index("await credit_lost_attempts(task_id, attempt=attempt, db=db)")
    assert src.index("with_task_attempt_fence(", credit) - credit < 400
    assert "place=attempt_place,\n            )" in src  # the recording context


@pytest.mark.asyncio
async def test_a_running_task_still_records_its_files_as_they_land():
    from app.services.sub_agent_files import RESULT_FILE_IDS_KEY, write_task_result_file_ids

    fid = str(uuid.uuid4())
    row = SimpleNamespace(status="running", metadata_json={})
    db = MagicMock()
    db.execute = AsyncMock(return_value=_result(row))
    db.commit = AsyncMock()

    with patch("app.core.database.async_session", lambda: _SessionCM(db)):
        assert await write_task_result_file_ids(str(uuid.uuid4()), [fid]) is True

    assert row.metadata_json == {RESULT_FILE_IDS_KEY: [fid]}


def test_the_ended_row_is_read_back_as_its_outcome():
    from app.services.sub_agent import _ended_elsewhere

    assert _ended_elsewhere(None) is None
    assert _ended_elsewhere(SimpleNamespace(status="running", error=None)) is None
    assert _ended_elsewhere(SimpleNamespace(status="pending", error=None)) is None
    assert _ended_elsewhere(SimpleNamespace(status="completed", error=None)).status == "completed"
    failed = _ended_elsewhere(SimpleNamespace(status="failed", error="boom"))
    assert (failed.status, failed.error) == ("failed", "boom")
