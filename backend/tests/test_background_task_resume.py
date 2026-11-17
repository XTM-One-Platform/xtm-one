"""A background task its worker lost resumes from its checkpoint, or fails with its claim released.

Four behaviours, each driven through the code that has it:

- the checkpoint a task's turn writes is replayed by the next attempt, with
  the resume note and the sandbox workspace's manifest;
- at most ``MAX_RESUMES`` resumes, each recorded on the task with the link
  from the attempt lost (``resumed_from``) to the one that resumed it
  (``resumed_as``); the next loss fails the task with its reason;
- an orphaned task is failed within the orphan threshold (at once when its
  worker is known gone) and its work item claims are released in the same
  scheduler pass;
- a stopping worker (SIGTERM) winds its checkpointed turns down and hands each
  task back, re-queued at once.
"""

from __future__ import annotations

import inspect
import json
import logging
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core import worker_shutdown
from app.llm.base import ChatMessage
from app.services import attempt_charges, background_checkpoint, orchestration_reconciler, sub_agent
from app.services.background_checkpoint import (
    MAX_RESUMES,
    TaskCheckpoint,
    TaskReplay,
    load_task_replay,
    note_resume,
    record_resume,
    resume_refused,
    resumes_of,
)
from app.services.orchestration_adapters.sub_agent import SubAgentAdapter
from app.services.orchestration_engine import LEASE_EXPIRY_PAYLOAD_KEY
from app.services.turn_checkpoint import ResumedWork
from app.tools.base import ToolCall

TASK = "5d7c1e0a-2b3f-4c4d-8e5f-6a7b8c9d0e1f"


def _round(call_id: str, result: str) -> list[ChatMessage]:
    return [
        ChatMessage(
            role="assistant",
            content="",
            tool_calls=[ToolCall(id=call_id, name="code_interpreter", arguments={"code": f"print('{call_id}')"})],
        ),
        ChatMessage(role="tool", content=result, tool_call_id=call_id, name="code_interpreter"),
    ]


class _FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.sets = 0

    async def set(self, key, value, ex=None):
        self.sets += 1
        self.store[key] = value

    async def get(self, key):
        return self.store.get(key)

    async def delete(self, key):
        self.store.pop(key, None)


@pytest.fixture
def redis(monkeypatch):
    fake = _FakeRedis()

    async def _client():
        return fake

    monkeypatch.setattr(background_checkpoint, "_redis", _client)
    return fake


@pytest.fixture(autouse=True)
def _no_stop_request(monkeypatch):
    monkeypatch.setattr(worker_shutdown, "_request", None)


# ---------------------------------------------------------------------------
# 1. The checkpoint is replayed on resume
# ---------------------------------------------------------------------------


async def test_an_assignment_run_resume_reads_its_own_workspace_and_names_the_running_command(redis, monkeypatch):
    """No conversation: the run's own workspace scope; the note names the check still running with its run_id."""
    seen = {}

    async def _manifest(conversation_id, user_id, *, assignment_run_id=None):
        seen.update(conversation_id=conversation_id, assignment_run_id=assignment_run_id)
        return "co/ (1 file)\n1 sandbox command(s) started before the interruption are still running: backend-unit on image 'python-dev', run_id r42, running for 30 s."

    monkeypatch.setattr("app.services.sandbox_workspace.session.manifest_for_resume", _manifest)
    checkpoint = TaskCheckpoint(uuid.UUID(TASK), attempt=2)
    checkpoint.offer([*_round("a", "result a")])
    await checkpoint._task
    run_id = uuid.uuid4()

    replay = await load_task_replay(TASK, assignment_run_id=run_id, user_id=uuid.uuid4())

    assert seen == {"conversation_id": None, "assignment_run_id": run_id}
    assert "run_id r42" in replay.history[-1].content


async def test_the_next_attempt_replays_the_rounds_the_lost_one_checkpointed(redis, monkeypatch):
    async def _manifest(conversation_id, user_id):
        return "report.md (2 KB, uncommitted edit)"

    monkeypatch.setattr("app.services.sandbox_workspace.session.manifest_for_resume", _manifest)
    checkpoint = TaskCheckpoint(uuid.UUID(TASK), attempt=2)
    checkpoint.offer([*_round("a", "result a"), *_round("b", "result b")])
    await checkpoint._task
    stored = json.loads(redis.store[background_checkpoint.checkpoint_key(TASK)])
    assert stored["turn_id"] == "attempt-2"

    replay = await load_task_replay(TASK, conversation_id=uuid.uuid4(), user_id=uuid.uuid4())

    assert replay.turn_id == "attempt-2"
    assert replay.replayed_calls == 2
    assert [m.tool_call_id for m in replay.history if m.role == "tool"] == ["a", "b"]
    note = replay.history[-1]
    assert note.role == "assistant" and note.content.startswith("[Resumed:")
    assert "2 completed tool call(s)" in note.content
    assert "report.md (2 KB, uncommitted edit)" in note.content, "the resumed turn finds its workspace"


async def test_nothing_checkpointed_or_unreadable_starts_from_the_description(redis):
    assert (await load_task_replay(TASK)).history == []
    redis.store[background_checkpoint.checkpoint_key(TASK)] = "{not json"
    unreadable = await load_task_replay(TASK)
    assert unreadable.history == [] and unreadable.resumed.found


async def test_a_turn_that_ends_leaves_nothing_to_resume(redis):
    checkpoint = TaskCheckpoint(uuid.UUID(TASK), attempt=1)
    checkpoint.offer(_round("a", "result a"))
    await checkpoint._task
    await checkpoint.aclose(flush=False)
    await background_checkpoint.discard_task_checkpoint(TASK)
    assert background_checkpoint.checkpoint_key(TASK) not in redis.store


async def test_a_reclaimed_task_runs_its_turn_from_the_replayed_history(redis, monkeypatch):
    from tests.test_background_edition_gate import (
        ENTERPRISE,
        _patch_task_collaborators,
        _pin,
        _task_row,
        _TaskSession,
    )

    _pin(monkeypatch, **ENTERPRISE)
    monkeypatch.setattr("app.services.quota.quota_store_out", AsyncMock(return_value=False))
    monkeypatch.setattr(sub_agent, "credit_lost_attempts", AsyncMock())
    task = _task_row()
    target = SimpleNamespace(id=uuid.uuid4(), name="Exposure analyst", handover_agent_ids=[])
    monkeypatch.setattr("app.core.database.async_session", lambda: _TaskSession([task, target, task]))
    monkeypatch.setattr("app.api.v1.helpers.get_user_group_ids", AsyncMock(return_value=[]))
    collaborators = _patch_task_collaborators(monkeypatch)
    rounds = _round("a", "result a")
    replay = TaskReplay(
        resumed=ResumedWork(messages=rounds, found=True),
        history=[*rounds, ChatMessage(role="assistant", content="[Resumed: ...]")],
        turn_id="attempt-2",
    )
    monkeypatch.setattr(background_checkpoint, "load_task_replay", AsyncMock(return_value=replay))
    discard = AsyncMock()
    monkeypatch.setattr(background_checkpoint, "discard_task_checkpoint", discard)

    outcome = await sub_agent._execute_sub_agent_task(
        task_id=str(task.id),
        user_id=str(task.user_id),
        target_agent_id=str(target.id),
        task_description="Summarise the new exposures",
        timeout_seconds=600,
        attempt=3,
    )

    assert outcome.succeeded
    turn = collaborators.run_agent_chat.await_args
    assert turn.args[3] == replay.history, "the turn starts from what the lost attempt did"
    assert callable(turn.kwargs["on_checkpoint"]), "and checkpoints its own rounds"
    (resume,) = resumes_of(task.metadata_json)
    assert resume["resumed_from"] == "attempt-2" and resume["resumed_as"] == "attempt-3"
    assert resume["replayed_calls"] == 1
    discard.assert_awaited_once_with(str(task.id))


# ---------------------------------------------------------------------------
# 2. At most MAX_RESUMES resumes, each recorded
# ---------------------------------------------------------------------------


def test_each_resume_is_recorded_with_the_link_from_the_attempt_lost():
    at = datetime(2026, 10, 31, 21, 0, tzinfo=UTC)
    metadata = record_resume(
        {"assignment_run_id": "r"}, attempt=2, cause="lost", replayed_calls=4, resumed_from="attempt-1", now=at
    )
    assert metadata["assignment_run_id"] == "r", "the other metadata is kept"
    assert resumes_of(metadata) == [
        {
            "at": at.isoformat(),
            "attempt": 2,
            "cause": "lost",
            "replayed_calls": 4,
            "resumed_from": "attempt-1",
            "resumed_as": "attempt-2",
            "graceful": False,
        }
    ]


def test_the_resume_past_the_last_is_refused_with_its_reason():
    metadata: dict = {}
    for attempt in range(2, 2 + MAX_RESUMES):
        assert resume_refused(metadata) is None
        metadata = record_resume(metadata, attempt=attempt, cause="lost", replayed_calls=0, resumed_from=None)
    assert MAX_RESUMES == 3
    reason = resume_refused(metadata)
    assert reason is not None and "after 3 resume(s)" in reason


def test_the_resume_note_names_the_cause_and_clears_the_markers(caplog):
    task = SimpleNamespace(
        id=uuid.UUID(TASK),
        metadata_json={
            "assignment_run_id": "run-1",
            background_checkpoint.HANDED_BACK_KEY: {"at": "t", "attempt": 1, "signal": "SIGTERM"},
            sub_agent.ORPHANED_KEY: {"last_heartbeat_at": "t"},
        },
    )
    replay = TaskReplay(resumed=ResumedWork(messages=_round("a", "r"), found=True), history=[], turn_id="attempt-1")
    with caplog.at_level(logging.INFO, logger=background_checkpoint.__name__):
        # The hand-back's deferral gave attempt 1 back: the next claim runs at attempt 1 again.
        metadata = note_resume(task, replay=replay, attempt=1)
    (resume,) = resumes_of(metadata)
    assert "asked to stop (SIGTERM)" in resume["cause"]
    assert background_checkpoint.HANDED_BACK_KEY not in metadata and sub_agent.ORPHANED_KEY not in metadata
    assert resume["graceful"] is True, "a hand-back's resume is graceful"
    line = caplog.records[-1].getMessage()
    assert TASK in line and "run-1" in line and "SIGTERM" in line
    assert "0 of 3 in a row after a loss without a word with no progress, 0 of 20 in all, " in line
    assert "1 of 3 in a row after a graceful stop with no progress, 1 of 200 after a graceful stop in all" in line


async def test_a_task_past_its_resumes_is_failed_before_its_turn(redis, monkeypatch):
    from tests.test_background_edition_gate import (
        ENTERPRISE,
        _patch_task_collaborators,
        _pin,
        _task_row,
        _TaskSession,
    )

    _pin(monkeypatch, **ENTERPRISE)
    monkeypatch.setattr("app.services.quota.quota_store_out", AsyncMock(return_value=False))
    monkeypatch.setattr(sub_agent, "credit_lost_attempts", AsyncMock())
    task = _task_row()
    for attempt in (2, 3, 4):
        task.metadata_json = record_resume(
            task.metadata_json, attempt=attempt, cause="lost", replayed_calls=1, resumed_from=f"attempt-{attempt - 1}"
        )
    target = SimpleNamespace(id=uuid.uuid4(), name="Exposure analyst", handover_agent_ids=[])
    monkeypatch.setattr("app.core.database.async_session", lambda: _TaskSession([task, target, task]))
    monkeypatch.setattr("app.api.v1.helpers.get_user_group_ids", AsyncMock(return_value=[]))
    collaborators = _patch_task_collaborators(monkeypatch)
    discard = AsyncMock()
    monkeypatch.setattr(background_checkpoint, "discard_task_checkpoint", discard)

    outcome = await sub_agent._execute_sub_agent_task(
        task_id=str(task.id),
        user_id=str(task.user_id),
        target_agent_id=str(target.id),
        task_description="Summarise the new exposures",
        timeout_seconds=600,
        attempt=5,
    )

    assert not outcome.succeeded and "after 3 resume(s)" in outcome.error
    assert outcome.error.startswith(background_checkpoint.RESUME_CAP_REACHED)
    assert task.status == "failed" and task.error == outcome.error
    collaborators.run_agent_chat.assert_not_awaited()
    collaborators.delegation.assert_awaited()
    # Its checkpoint is kept for a relaunch (#12386).
    discard.assert_not_awaited()


# ---------------------------------------------------------------------------
# 3. An orphaned task is failed in bounded time, its claims released
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("value", "expected"), [(900, 900), (30, 60), (0, 900), (None, 900)])
def test_the_orphan_wait_is_the_stale_run_threshold(monkeypatch, value, expected):
    monkeypatch.setattr(sub_agent.settings, "stale_in_process_run_seconds", value)
    assert sub_agent.orphan_wait_seconds() == expected


def test_the_stale_run_fallback_matches_the_orphan_threshold():
    from app.core.config import Settings

    assert Settings.model_fields["stale_assignment_run_seconds"].default == 900
    assert Settings.model_fields["stale_in_process_run_seconds"].default == 900


def test_an_orphaned_task_with_a_far_deadline_is_failed_within_the_threshold(monkeypatch):
    monkeypatch.setattr(sub_agent.settings, "stale_in_process_run_seconds", 900)
    now = datetime(2026, 10, 31, 21, 0, tzinfo=UTC)
    task = SimpleNamespace(started_at=now - timedelta(minutes=5), metadata_json={})
    execution = SimpleNamespace(deadline_at=now + timedelta(hours=4), last_error="Lease expired")
    expiry = SimpleNamespace(attempt=3, owner="worker-a", last_heartbeat_at=now, silent_seconds=61)

    record = sub_agent.mark_task_orphaned(task, execution, expiry, now=now)

    assert datetime.fromisoformat(record["fail_after"]) == now + timedelta(seconds=900)


async def test_a_task_taken_back_from_a_worker_known_gone_is_failed_at_once(monkeypatch, caplog):
    credit = AsyncMock(return_value=0)
    monkeypatch.setattr(attempt_charges, "credit_abandoned_task", credit)
    monkeypatch.setattr(sub_agent, "_update_task_delegation", AsyncMock())
    task = SimpleNamespace(
        id=uuid.UUID(TASK),
        status="running",
        error=None,
        completed_at=None,
        started_at=datetime.now(UTC),
        conversation_id=None,
        target_agent_id=uuid.uuid4(),
        metadata_json={},
    )
    session = SimpleNamespace(
        execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: task)), commit=AsyncMock()
    )
    heard = datetime.now(UTC) - timedelta(seconds=30)
    execution = SimpleNamespace(
        payload={
            "task_id": TASK,
            "assignment_run_id": "run-7",
            LEASE_EXPIRY_PAYLOAD_KEY: {
                "at": datetime.now(UTC).isoformat(),
                "last_heartbeat_at": heard.isoformat(),
                "silent_seconds": 30,
                "owner": "worker-a",
                "attempt": 3,
                "why": "its pod was deleted",
                "owner_record": {"version": "1.0", "state": "stopping"},
            },
        },
        last_error="Taken back from worker-a: its pod was deleted.",
        attempt_count=3,
        deadline_at=datetime.now(UTC) + timedelta(hours=1),
    )

    with caplog.at_level(logging.ERROR):
        await SubAgentAdapter().on_dead_letter(SimpleNamespace(db=session), execution)

    assert task.status == "failed", "no worker is left to write its ending"
    assert sub_agent.orphan_record(task) is None
    credit.assert_awaited_once()
    line = next(r.getMessage() for r in caplog.records if "work item claims" in r.getMessage())
    assert TASK in line and "run-7" in line


def test_the_scheduler_releases_the_claims_in_the_pass_that_failed_their_holder():
    from app.services import autonomous

    source = inspect.getsource(autonomous._run_autonomous_tick_inner)
    orphans = source.index("recover_orphaned_sub_agent_tasks(_orphan_db)")
    stale = source.index("_cleanup_stale_assignment_runs(db)")
    releases = [i for i in range(len(source)) if source.startswith("release_ended_run_claims()", i)]
    assert any(orphans < i < stale for i in releases), "released right after the orphan sweep"
    assert any(i > stale for i in releases), "and right after the stale-run cleanup"


# ---------------------------------------------------------------------------
# 4. A stopping worker winds its turns down and hands its tasks back at once
# ---------------------------------------------------------------------------


def test_the_stop_signal_asks_the_checkpointed_turns_to_wind_down(monkeypatch):
    from app import worker

    monkeypatch.delenv(worker_shutdown.GRACEFUL_TIMEOUT_ENV, raising=False)
    assert worker_shutdown.wind_down_due() is False
    worker.stop_asked("SIGTERM")
    request = worker_shutdown.shutdown_request()
    assert request is not None and request.reason == worker_shutdown.STOP and request.detail == "SIGTERM"
    assert worker_shutdown.wind_down_due() is True


async def test_a_flushing_close_writes_the_latest_snapshot_once_more(redis):
    checkpoint = TaskCheckpoint(uuid.UUID(TASK), attempt=1)
    checkpoint.offer(_round("a", "result a"))
    await checkpoint._task
    assert redis.sets == 1
    await checkpoint.aclose(flush=True)
    assert redis.sets == 2
    stored = json.loads(redis.store[background_checkpoint.checkpoint_key(TASK)])
    assert [e.get("tool_call_id") for e in stored["entries"]] == [None, "a"]


async def test_a_wound_down_task_is_handed_back_with_its_checkpoint_flushed(monkeypatch, caplog):
    from tests.test_background_edition_gate import _Session

    task = SimpleNamespace(id=uuid.UUID(TASK), status="running", metadata_json={"assignment_run_id": "run-9"})
    monkeypatch.setattr("app.core.database.async_session", lambda: _Session([task]))
    monkeypatch.setattr(orchestration_reconciler, "_stop_signal", "SIGTERM")
    checkpoint = SimpleNamespace(aclose=AsyncMock())

    with caplog.at_level(logging.WARNING):
        outcome = await sub_agent._hand_back_wound_down_task(TASK, checkpoint=checkpoint, attempt=2, place=None)

    checkpoint.aclose.assert_awaited_once_with(flush=True)
    assert outcome.handed_back and outcome.error == sub_agent.HANDED_BACK_ERROR
    assert task.status == "running", "still held by a live task until the next attempt resumes it"
    back = task.metadata_json[background_checkpoint.HANDED_BACK_KEY]
    assert back["attempt"] == 2 and back["signal"] == "SIGTERM"
    assert "asked to stop (SIGTERM)" in background_checkpoint.lost_cause(task.metadata_json)
    assert any("handed back" in r.getMessage() and "run-9" in r.getMessage() for r in caplog.records)


async def test_a_task_that_ended_meanwhile_keeps_its_ending(monkeypatch):
    from tests.test_background_edition_gate import _Session

    task = SimpleNamespace(id=uuid.UUID(TASK), status="completed", metadata_json={}, error=None)
    monkeypatch.setattr("app.core.database.async_session", lambda: _Session([task]))
    outcome = await sub_agent._hand_back_wound_down_task(
        TASK, checkpoint=SimpleNamespace(aclose=AsyncMock()), attempt=2, place=None
    )
    assert outcome.succeeded and not outcome.handed_back
    assert background_checkpoint.HANDED_BACK_KEY not in task.metadata_json


async def test_the_adapter_requeues_a_handed_back_task_at_once(monkeypatch):
    body = AsyncMock(return_value=sub_agent.SubAgentTaskOutcome(status="handed_back", error=sub_agent.HANDED_BACK_ERROR))
    monkeypatch.setattr("app.services.sub_agent._execute_sub_agent_task", body)
    session = SimpleNamespace(
        execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: None)),
        commit=AsyncMock(),
    )
    execution = SimpleNamespace(
        payload={
            "task_id": TASK,
            "target_agent_id": str(uuid.uuid4()),
            "user_id": str(uuid.uuid4()),
            "task_description": "do it",
        },
        attempt_count=1,
    )

    result = await SubAgentAdapter().execute(SimpleNamespace(db=session), execution)

    # A deferral first in the queue, not a failed attempt: on its last attempt a
    # retryable failure dead-lettered it and it never ran again.
    assert result.kind == "defer"
    assert result.place == "head" and result.backoff_seconds == 0.0
    assert result.adapter_state == "handed_back"
    assert result.error == sub_agent.HANDED_BACK_ERROR
    assert SubAgentAdapter.resume_when_worker_gone is True


# ---------------------------------------------------------------------------
# 5. The resume cap's outcome is explicit, and a resumed attempt's trace says so
# ---------------------------------------------------------------------------


def test_a_task_execution_has_an_attempt_for_its_first_run_and_every_resume():
    assert background_checkpoint.MAX_ATTEMPTS == MAX_RESUMES + 1, "an assignment run's bound is unchanged"
    assert background_checkpoint.MAX_TASK_ATTEMPTS == background_checkpoint.MAX_LOST_RESUMES + 1
    source = inspect.getsource(sub_agent)
    assert "max_attempts=background_checkpoint.task_max_attempts(settings.orchestration_default_max_attempts)" in source
    assert background_checkpoint.task_max_attempts(3) == background_checkpoint.MAX_TASK_ATTEMPTS, "the default"
    assert background_checkpoint.task_max_attempts(4) == background_checkpoint.MAX_TASK_ATTEMPTS, "the old floor"
    assert background_checkpoint.task_max_attempts(10) == 10, "an operator's bound above the old floor is kept"
    assert background_checkpoint.task_max_attempts(40) == 40


def _dead_lettered_task(metadata: dict) -> tuple[SimpleNamespace, SimpleNamespace, SimpleNamespace]:
    task = SimpleNamespace(
        id=uuid.UUID(TASK),
        status="running",
        error=None,
        completed_at=None,
        started_at=datetime.now(UTC),
        conversation_id=None,
        target_agent_id=uuid.uuid4(),
        metadata_json=metadata,
    )
    session = SimpleNamespace(
        execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: task)), commit=AsyncMock()
    )
    execution = SimpleNamespace(
        payload={"task_id": TASK, "assignment_run_id": "run-4"},
        last_error=sub_agent.HANDED_BACK_ERROR,
        attempt_count=4,
        max_attempts=4,
        deadline_at=datetime.now(UTC) + timedelta(hours=1),
    )
    return task, session, execution


async def test_a_task_dead_lettered_at_its_cap_fails_with_the_cap_not_the_hand_back(monkeypatch, caplog):
    monkeypatch.setattr(attempt_charges, "credit_abandoned_task", AsyncMock(return_value=0))
    monkeypatch.setattr(sub_agent, "_update_task_delegation", AsyncMock())
    metadata: dict = {"assignment_run_id": "run-4"}
    for attempt in (2, 3, 4):
        metadata = record_resume(metadata, attempt=attempt, cause="lost", replayed_calls=1, resumed_from=f"attempt-{attempt - 1}")
    metadata = background_checkpoint.handed_back(metadata, attempt=4, signal="SIGTERM")
    task, session, execution = _dead_lettered_task(metadata)

    with caplog.at_level(logging.ERROR):
        await SubAgentAdapter().on_dead_letter(SimpleNamespace(db=session), execution)

    assert task.status == "failed"
    assert task.error.startswith(background_checkpoint.RESUME_CAP_REACHED)
    assert "runs again at once" not in task.error and "SIGTERM" in task.error
    line = next(r.getMessage() for r in caplog.records if "work item claims" in r.getMessage())
    assert background_checkpoint.RESUME_CAP_REACHED in line and "run-4" in line


async def test_a_task_never_resumed_keeps_its_own_dead_letter_reason(monkeypatch):
    monkeypatch.setattr(attempt_charges, "credit_abandoned_task", AsyncMock(return_value=0))
    monkeypatch.setattr(sub_agent, "_update_task_delegation", AsyncMock())
    task, session, execution = _dead_lettered_task({})
    execution.last_error = "Deadline exceeded"

    await SubAgentAdapter().on_dead_letter(SimpleNamespace(db=session), execution)

    assert task.status == "failed" and task.error == "Deadline exceeded"


def test_a_resumed_attempt_names_its_first_trace():
    assert background_checkpoint.resumed_trace_meta(TASK, {}) is None
    metadata = record_resume(
        {"assignment_run_id": "run-2"}, attempt=2, cause="its worker was asked to stop (SIGTERM) and handed it back",
        replayed_calls=4, resumed_from="attempt-1",
    )
    link = background_checkpoint.resumed_trace_meta(TASK, metadata)["resumed_from"]
    assert link["entry"] == "background_task" and link["task_id"] == TASK and link["run_id"] == "run-2"
    assert link["attempt"] == "attempt-1" and link["resumed_as"] == "attempt-2"
    assert link["resume"] == 1 and link["of"] == background_checkpoint.MAX_LOST_RESUMES and "SIGTERM" in link["cause"]
    assert link["streak"] == 1 and link["streak_of"] == MAX_RESUMES


def test_the_resumed_turn_opens_its_trace_with_the_link():
    source = inspect.getsource(sub_agent._execute_sub_agent_task)
    assert "resumed_trace_meta(task_id, task.metadata_json)" in source
    assert "next_root_trace(_resumed_trace)" in source


# ---------------------------------------------------------------------------
# 6. A graceful stop does not spend the resume cap
# ---------------------------------------------------------------------------


def _hand_back_and_resume(metadata: dict, attempt: int, calls: int | None = None) -> dict:
    """*metadata* after one rollout: the turn handed back at *attempt*, resumed by the next claim.

    The hand-back is a deferral that gives the attempt back: the claim
    that resumes it runs at the same attempt number. *calls*: the task's
    completed calls when it is resumed; by default it progressed since the
    rollout before (``attempt * 10``), as a task at work does.
    """
    completed = attempt * 10 if calls is None else calls
    metadata = background_checkpoint.handed_back(metadata, attempt=attempt, signal="SIGTERM")
    task = SimpleNamespace(id=uuid.UUID(TASK), metadata_json=metadata)
    assert resume_refused(task.metadata_json, attempt=attempt, completed_calls=completed) is None
    replay = TaskReplay(
        resumed=ResumedWork(), history=[], turn_id=f"attempt-{attempt}", stored_completed_calls=completed
    )
    return note_resume(task, replay=replay, attempt=attempt)


def test_three_hand_backs_then_a_fourth_resume_is_allowed():
    metadata: dict = {}
    for attempt in (1, 2, 3):
        metadata = _hand_back_and_resume(metadata, attempt)
    assert background_checkpoint.resume_counts(metadata) == (0, 3)
    # A fourth rollout: graceful again, allowed.
    assert resume_refused(background_checkpoint.handed_back(metadata, attempt=4, signal="SIGTERM")) is None
    # A crash after them: none of the three spent the cap, so it is allowed too.
    assert resume_refused(metadata) is None
    assert all(r["graceful"] for r in resumes_of(metadata)), "every hand-back is still recorded"


def test_three_losses_without_a_word_are_still_refused_with_the_cap_text():
    metadata: dict = {}
    for attempt in (2, 3, 4):
        assert resume_refused(metadata) is None
        metadata = record_resume(metadata, attempt=attempt, cause="lost", replayed_calls=0, resumed_from=None)
    # The rollout came with no progress since the losses: their streak stands.
    metadata = _hand_back_and_resume(metadata, 5, calls=0)
    assert background_checkpoint.resume_counts(metadata) == (3, 1)
    reason = resume_refused(metadata)
    assert reason is not None and reason.startswith(background_checkpoint.RESUME_CAP_REACHED)
    assert "after 3 resume(s)" in reason
    assert "3 resume(s) in a row after a loss without a word with no progress between them (the most a task makes is 3)" in reason
    assert "3 after a loss without a word in all (the most a task makes is 20)" in reason
    assert "1 in a row after a graceful stop with no progress between them (the most a task makes is 3)" in reason
    assert "1 after a graceful stop in all (the most a task makes is 200)" in reason


def test_graceful_resumes_past_their_own_bound_are_refused_with_their_own_reason():
    metadata: dict = {}
    for attempt in range(1, 1 + background_checkpoint.MAX_GRACEFUL_RESUMES):
        metadata = _hand_back_and_resume(metadata, attempt)
    assert background_checkpoint.MAX_GRACEFUL_RESUMES == 200
    handed = background_checkpoint.handed_back(metadata, attempt=201, signal="SIGTERM")
    reason = resume_refused(handed, attempt=201, completed_calls=10_000)
    assert reason is not None and reason.startswith(background_checkpoint.RESUME_CAP_REACHED)
    assert "stopped gracefully again after 200 graceful resume(s), at the bound of 200 graceful" in reason
    assert "0 resume(s) in a row after a loss without a word" in reason and "SIGTERM" in reason
    # The other bound is untouched: a crash is still resumed.
    assert resume_refused(metadata) is None


# ---------------------------------------------------------------------------
# 6bis. Graceful stops follow the streak rule: progress between them restarts it
# ---------------------------------------------------------------------------


def test_a_task_that_progresses_between_25_rollouts_is_resumed_at_each():
    # Production, 2 Nov 2026: tasks alive for two hours under a rollout every
    # 4 to 8 minutes failed at the 21st graceful stop while their completed
    # calls grew between every take-back.
    metadata: dict = {}
    for attempt in range(1, 26):
        metadata = _hand_back_and_resume(metadata, attempt)
    assert background_checkpoint.resume_counts(metadata) == (0, 25)
    assert background_checkpoint.graceful_streak(metadata) == 1
    handed = background_checkpoint.handed_back(metadata, attempt=26, signal="SIGTERM")
    assert resume_refused(handed, attempt=26, completed_calls=260) is None


def test_three_graceful_stops_with_flat_progress_end_the_task_at_the_fourth():
    metadata: dict = {}
    for attempt in (1, 2, 3):
        metadata = _hand_back_and_resume(metadata, attempt, calls=7)
    assert background_checkpoint.graceful_streak(metadata) == 3
    handed = background_checkpoint.handed_back(metadata, attempt=4, signal="SIGTERM")
    reason = resume_refused(handed, attempt=4, completed_calls=7)
    assert reason is not None and reason.startswith(background_checkpoint.RESUME_CAP_REACHED)
    assert "stopped gracefully again after 3 graceful resume(s), at the bound of 3 graceful resume(s) in a row" in reason
    assert "3 in a row after a graceful stop with no progress between them (the most a task makes is 3)" in reason
    # One call more since the last resume is progress: the streak starts again.
    assert resume_refused(handed, attempt=4, completed_calls=8) is None
    # The wordless streak is its own: a crash after them is still resumed.
    assert resume_refused(metadata, attempt=5, completed_calls=7) is None


def test_a_take_back_on_its_word_follows_the_graceful_streak_too():
    metadata: dict = {}
    for attempt in (1, 2, 3):
        metadata = _hand_back_and_resume(metadata, attempt, calls=4)
    assert resume_refused(metadata, attempt=4, taken_back_on_its_word=True, completed_calls=4) is not None
    assert resume_refused(metadata, attempt=4, taken_back_on_its_word=True, completed_calls=5) is None


def test_graceful_resumes_recorded_before_the_count_never_end_a_task_alive_across_the_deploy():
    # Entries written before ``completed_calls`` existed tell no reliable
    # progress: they never add to the streak, and the bound in all is far above them.
    legacy = {
        "resumes": [
            {"attempt": a, "cause": "its worker was asked to stop (SIGTERM) and handed it back", "graceful": True}
            for a in range(1, 26)
        ]
    }
    assert background_checkpoint.graceful_streak(legacy) == 0
    handed = background_checkpoint.handed_back(legacy, attempt=26, signal="SIGTERM")
    assert resume_refused(handed, attempt=26, completed_calls=0) is None


async def test_graceful_stops_never_lower_a_legacy_execution_bound(monkeypatch):
    # A graceful stop spends no orchestration attempt (a hand-back defers, a
    # take-back on its word gives it back): the legacy row is still raised to
    # the task bound, never re-bounded below it by the graceful streak.
    execution = await _claim(monkeypatch, background_checkpoint.MAX_ATTEMPTS)
    assert execution.max_attempts == background_checkpoint.MAX_TASK_ATTEMPTS
    assert background_checkpoint.MAX_TASK_ATTEMPTS > background_checkpoint.MAX_GRACEFUL_STREAK + 1


def test_a_take_back_from_a_worker_that_said_it_was_stopping_is_graceful():
    from app.services.orchestration_adapters.sub_agent import _taken_back_on_its_word
    from app.services.orchestration_engine import OWNER_GONE_STATE

    def _execution(state: str | None, owner_state: str | None):
        record = {"attempt": 1, "owner_record": {"state": owner_state}} if owner_state else {"attempt": 1}
        return SimpleNamespace(adapter_state=state, payload={LEASE_EXPIRY_PAYLOAD_KEY: record})

    assert _taken_back_on_its_word(_execution(OWNER_GONE_STATE, "draining")) is True
    assert _taken_back_on_its_word(_execution(OWNER_GONE_STATE, "stopped")) is True
    assert _taken_back_on_its_word(_execution(OWNER_GONE_STATE, "running")) is False, "a crash spends the cap"
    assert _taken_back_on_its_word(_execution(None, None)) is False, "a lapsed lease spends the cap"

    metadata: dict = {}
    for attempt in (2, 3, 4):
        metadata = record_resume(metadata, attempt=attempt, cause="lost", replayed_calls=0, resumed_from=None)
    assert resume_refused(metadata) is not None
    assert resume_refused(metadata, taken_back_on_its_word=True) is None
    task = SimpleNamespace(id=uuid.UUID(TASK), metadata_json=metadata)
    replay = TaskReplay(resumed=ResumedWork(), history=[], turn_id="attempt-4")
    noted = note_resume(task, replay=replay, attempt=5, taken_back_on_its_word=True)
    last = resumes_of(noted)[-1]
    assert last["graceful"] is True and "said it was stopping" in last["cause"]
    assert background_checkpoint.resume_counts(noted) == (3, 1)


def test_a_claim_lost_without_a_word_after_a_hand_back_is_counted():
    """The claim resuming a hand-back crashes before ``note_resume`` cleared it.

    That claim ran at the hand-back's attempt and spent it; the next one runs
    at a later attempt, so its resume is a loss without a word, not graceful.
    """
    metadata: dict = {}
    for attempt in (2, 3, 4):
        metadata = record_resume(metadata, attempt=attempt, cause="lost", replayed_calls=0, resumed_from=None)
    handed = background_checkpoint.handed_back(metadata, attempt=5, signal="SIGTERM")
    assert resume_refused(handed, attempt=5) is None, "the claim that resumes the hand-back is graceful"
    reason = resume_refused(handed, attempt=6)
    assert reason is not None and "was lost without a word before it resumed" in reason
    task = SimpleNamespace(id=uuid.UUID(TASK), metadata_json=background_checkpoint.handed_back({}, attempt=1, signal="SIGTERM"))
    replay = TaskReplay(resumed=ResumedWork(), history=[], turn_id="attempt-1")
    (resume,) = resumes_of(note_resume(task, replay=replay, attempt=2))
    assert resume["graceful"] is False and "lost without a word" in resume["cause"]


def test_the_trace_link_counts_only_the_resumes_against_the_cap():
    """``resume`` of ``of`` never counts graceful resumes past the loss cap."""
    metadata: dict = {"assignment_run_id": "run-5"}
    for attempt in (1, 2, 3, 4):
        metadata = _hand_back_and_resume(metadata, attempt)
    link = background_checkpoint.resumed_trace_meta(TASK, metadata)["resumed_from"]
    assert link["resume"] == 0 and link["of"] == background_checkpoint.MAX_LOST_RESUMES
    assert link["streak"] == 0 and link["streak_of"] == MAX_RESUMES
    assert link["graceful"] is True and link["graceful_resumes"] == 4
    assert link["graceful_of"] == background_checkpoint.MAX_GRACEFUL_RESUMES
    assert link["graceful_streak"] == 1 and link["graceful_streak_of"] == background_checkpoint.MAX_GRACEFUL_STREAK


def test_a_resume_recorded_before_the_flag_is_read_by_its_cause():
    legacy = {
        "resumes": [
            {"attempt": 2, "cause": "its worker was asked to stop (SIGTERM) and handed it back"},
            {"attempt": 3, "cause": "its worker stopped or lost its lease before the turn ended"},
        ]
    }
    assert background_checkpoint.resume_counts(legacy) == (1, 1)


async def test_the_adapter_passes_a_take_back_on_its_word_to_the_task(monkeypatch):
    from app.services.orchestration_engine import OWNER_GONE_STATE

    body = AsyncMock(return_value=sub_agent.SubAgentTaskOutcome(status="completed"))
    monkeypatch.setattr("app.services.sub_agent._execute_sub_agent_task", body)
    session = SimpleNamespace(
        execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: None)),
        commit=AsyncMock(),
    )
    execution = SimpleNamespace(
        payload={
            "task_id": TASK,
            "target_agent_id": str(uuid.uuid4()),
            "user_id": str(uuid.uuid4()),
            "task_description": "do it",
            LEASE_EXPIRY_PAYLOAD_KEY: {"attempt": 1, "owner_record": {"state": "draining"}},
        },
        adapter_state=OWNER_GONE_STATE,
        attempt_count=2,
    )

    await SubAgentAdapter().execute(SimpleNamespace(db=session), execution)

    assert body.await_args.kwargs["taken_back_on_its_word"] is True


async def test_a_dead_letter_at_the_cap_names_both_counts(monkeypatch):
    monkeypatch.setattr(attempt_charges, "credit_abandoned_task", AsyncMock(return_value=0))
    monkeypatch.setattr(sub_agent, "_update_task_delegation", AsyncMock())
    metadata: dict = {"assignment_run_id": "run-4"}
    for attempt in (1, 2):
        metadata = _hand_back_and_resume(metadata, attempt)
    for attempt in (4, 5, 6):
        metadata = record_resume(metadata, attempt=attempt, cause="lost", replayed_calls=0, resumed_from=None)
    task, session, execution = _dead_lettered_task(metadata)

    await SubAgentAdapter().on_dead_letter(SimpleNamespace(db=session), execution)

    assert task.status == "failed" and task.error.startswith(background_checkpoint.RESUME_CAP_REACHED)
    assert "3 resume(s) in a row after a loss without a word" in task.error
    assert "3 after a loss without a word in all" in task.error
    assert "2 after a graceful stop in all" in task.error


# ---------------------------------------------------------------------------
# 7. Only losses without a word in a row, with no progress between them, spend the cap
# ---------------------------------------------------------------------------


def _lost(metadata: dict, attempt: int, calls: int) -> dict:
    """*metadata* after one loss without a word, resumed with *calls* tool calls to replay."""
    assert resume_refused(metadata, attempt=attempt, replayed_calls=calls) is None
    return record_resume(
        metadata, attempt=attempt, cause="lost", replayed_calls=calls, resumed_from=f"attempt-{attempt - 1}"
    )


def test_a_task_that_progresses_between_wordless_losses_is_resumed_past_three():
    """Rollouts every few minutes: each lost attempt completed tool calls before its worker went."""
    metadata: dict = {}
    for attempt, calls in zip(range(2, 9), (2, 5, 9, 12, 15, 21, 30), strict=True):
        metadata = _lost(metadata, attempt, calls)
    assert background_checkpoint.resume_counts(metadata) == (7, 0)
    assert background_checkpoint.lost_streak(metadata) == 1, "each loss followed progress"
    assert resume_refused(metadata, attempt=9, replayed_calls=31) is None


def test_three_wordless_losses_with_no_progress_between_them_are_refused():
    metadata: dict = {}
    metadata = _lost(metadata, 2, 4)
    metadata = _lost(metadata, 3, 4)
    metadata = _lost(metadata, 4, 4)
    assert background_checkpoint.lost_streak(metadata) == 3
    reason = resume_refused(metadata, attempt=5, replayed_calls=4)
    assert reason is not None and reason.startswith(background_checkpoint.RESUME_CAP_REACHED)
    assert "3 resume(s) in a row after a loss without a word with no progress between them" in reason


def test_progress_resets_the_streak_and_a_stall_after_it_counts_again():
    metadata: dict = {}
    for attempt in (2, 3, 4):
        metadata = _lost(metadata, attempt, 3)
    # The fourth loss is allowed only because the lost attempt completed a call.
    assert resume_refused(metadata, attempt=5, replayed_calls=3) is not None
    metadata = _lost(metadata, 5, 4)
    assert background_checkpoint.lost_streak(metadata) == 1
    metadata = _lost(metadata, 6, 4)
    metadata = _lost(metadata, 7, 4)
    assert resume_refused(metadata, attempt=8, replayed_calls=4) is not None, "a stall after progress is bounded"


def test_a_graceful_resume_with_progress_resets_the_streak():
    metadata: dict = {}
    for attempt in (2, 3, 4):
        metadata = _lost(metadata, attempt, 1)
    task = SimpleNamespace(
        id=uuid.UUID(TASK), metadata_json=background_checkpoint.handed_back(metadata, attempt=5, signal="SIGTERM")
    )
    replay = TaskReplay(resumed=ResumedWork(messages=[*_round("a", "r"), *_round("b", "r")], found=True), history=[])
    metadata = note_resume(task, replay=replay, attempt=5)
    assert background_checkpoint.lost_streak(metadata) == 0
    assert resume_refused(metadata, attempt=6, replayed_calls=2) is None


def test_the_total_of_wordless_losses_stays_bounded_with_progress():
    metadata: dict = {}
    for attempt in range(2, 2 + background_checkpoint.MAX_LOST_RESUMES):
        metadata = _lost(metadata, attempt, attempt)
    assert background_checkpoint.MAX_LOST_RESUMES == 20
    reason = resume_refused(metadata, attempt=30, replayed_calls=100)
    assert reason is not None and "20 after a loss without a word in all (the most a task makes is 20)" in reason


def test_a_resume_without_its_replay_known_assumes_no_progress():
    metadata: dict = {}
    for attempt in (2, 3, 4):
        metadata = _lost(metadata, attempt, attempt)
    assert background_checkpoint.lost_streak(metadata) == 1
    assert background_checkpoint.next_lost_streak(metadata) == 2
    assert background_checkpoint.next_lost_streak(metadata, replayed_calls=10) == 1


def test_the_engine_reason_names_the_cause_of_a_wordless_take_back():
    from app.services.orchestration_adapters.sub_agent import _taken_back_reason

    gone = "Its worker on pod-a (version 1.2) went without a word, last heard 25 s ago"
    execution = SimpleNamespace(
        attempt_count=3,
        payload={LEASE_EXPIRY_PAYLOAD_KEY: {"attempt": 2, "silent_seconds": 25, "why": gone, "owner_record": {"state": "running"}}},
    )
    reason = _taken_back_reason(execution)
    assert reason is not None and reason.startswith(gone) and "25 s" in reason
    lapsed = SimpleNamespace(attempt_count=3, payload={LEASE_EXPIRY_PAYLOAD_KEY: {"attempt": 2, "silent_seconds": 910}})
    assert _taken_back_reason(lapsed).startswith("its lease expired")
    stale = SimpleNamespace(attempt_count=5, payload={LEASE_EXPIRY_PAYLOAD_KEY: {"attempt": 2, "why": gone}})
    assert _taken_back_reason(stale) is None, "a record of an earlier attempt is not this loss's"
    on_its_word = SimpleNamespace(
        attempt_count=2, payload={LEASE_EXPIRY_PAYLOAD_KEY: {"attempt": 2, "owner_record": {"state": "draining"}}}
    )
    assert _taken_back_reason(on_its_word) is None

    task = SimpleNamespace(id=uuid.UUID(TASK), metadata_json={})
    replay = TaskReplay(resumed=ResumedWork(), history=[], turn_id="attempt-2")
    (resume,) = resumes_of(note_resume(task, replay=replay, attempt=3, reason=reason))
    assert resume["cause"].startswith("its worker on pod-a") and resume["graceful"] is False


async def test_the_adapter_raises_an_old_execution_to_the_task_bound_and_passes_the_reason(monkeypatch):
    body = AsyncMock(return_value=sub_agent.SubAgentTaskOutcome(status="completed"))
    monkeypatch.setattr("app.services.sub_agent._execute_sub_agent_task", body)
    session = SimpleNamespace(
        execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: None)),
        commit=AsyncMock(),
    )
    execution = SimpleNamespace(
        id=uuid.uuid4(),
        payload={
            "task_id": TASK,
            "target_agent_id": str(uuid.uuid4()),
            "user_id": str(uuid.uuid4()),
            "task_description": "do it",
            LEASE_EXPIRY_PAYLOAD_KEY: {"attempt": 2, "silent_seconds": 30, "why": "Its worker went without a word"},
        },
        adapter_state=None,
        attempt_count=3,
        max_attempts=background_checkpoint.MAX_ATTEMPTS,
    )

    await SubAgentAdapter().execute(SimpleNamespace(db=session), execution)

    assert execution.max_attempts == background_checkpoint.MAX_TASK_ATTEMPTS
    assert body.await_args.kwargs["taken_back_on_its_word"] is False
    assert body.await_args.kwargs["taken_back_reason"].startswith("Its worker went without a word")


def test_the_task_reads_its_replay_before_it_decides_on_the_resume():
    source = inspect.getsource(sub_agent._execute_sub_agent_task)
    loaded = source.index("_task_replay = await background_checkpoint.load_task_replay(")
    refused = source.index("refused_resume = background_checkpoint.resume_refused(")
    assert loaded < refused
    assert "completed_calls=_task_replay.completed_calls" in source


# ---------------------------------------------------------------------------
# 8. Progress is the cumulative count of completed calls, not the retained ones
# ---------------------------------------------------------------------------


async def test_eviction_of_old_rounds_is_not_mistaken_for_a_stall(redis, monkeypatch):
    """A checkpoint at its bound drops its oldest rounds while the task keeps completing calls."""
    from app.services import turn_checkpoint

    bounded = turn_checkpoint.bounded
    monkeypatch.setattr(turn_checkpoint, "bounded", lambda entries: bounded(entries, limit=10_000))
    big = "x" * 4_000
    first = TaskCheckpoint(uuid.UUID(TASK), attempt=1)
    first.offer([*_round("a", big), *_round("b", big), *_round("c", big)])
    await first._task
    replay = await load_task_replay(TASK)
    assert replay.replayed_calls < 3, "the oldest round was dropped"
    assert replay.completed_calls == 3

    metadata = record_resume(
        {},
        attempt=2,
        cause="lost",
        replayed_calls=replay.replayed_calls,
        resumed_from=replay.turn_id,
        completed_calls=replay.completed_calls,
    )
    second = TaskCheckpoint(uuid.UUID(TASK), resumed=replay.resumed, attempt=2, completed_calls=replay.completed_calls)
    second.offer([*_round("d", big), *_round("e", big)])
    await second._task
    again = await load_task_replay(TASK)
    assert again.replayed_calls <= replay.replayed_calls, "the retained count stays flat or drops"
    assert again.completed_calls == 5, "the cumulative count grows"
    assert background_checkpoint.next_lost_streak(metadata, completed_calls=again.completed_calls) == 1


def test_full_checkpoints_that_progress_resume_past_three_and_a_flat_count_is_refused():
    metadata: dict = {}
    for attempt, completed in zip(range(2, 8), (10, 14, 19, 25, 30, 36), strict=True):
        assert resume_refused(metadata, attempt=attempt, completed_calls=completed) is None
        # The retained count stays at 8: the checkpoint is full.
        metadata = record_resume(
            metadata, attempt=attempt, cause="lost", replayed_calls=8, resumed_from=None, completed_calls=completed
        )
    assert background_checkpoint.lost_streak(metadata) == 1
    for attempt in (8, 9):
        assert resume_refused(metadata, attempt=attempt, completed_calls=36) is None
        metadata = record_resume(
            metadata, attempt=attempt, cause="lost", replayed_calls=8, resumed_from=None, completed_calls=36
        )
    assert resume_refused(metadata, attempt=10, completed_calls=36) is not None, "a flat count is a stall"


def test_an_entry_without_the_count_falls_back_to_its_replayed_calls():
    old = {"resumes": [{"attempt": 2, "cause": "lost", "replayed_calls": 6, "graceful": False}]}
    assert background_checkpoint.next_lost_streak(old, completed_calls=6) == 2, "no more than replayed then"
    assert background_checkpoint.next_lost_streak(old, completed_calls=7) == 1
    stored = TaskReplay(resumed=ResumedWork(messages=_round("a", "r"), found=True), history=[])
    assert stored.completed_calls == stored.replayed_calls == 1, "a checkpoint written before the count"


def test_a_resume_that_progressed_minutes_before_its_last_loss_starts_the_streak_again():
    """Two wordless losses, a resume that completes calls (a commit pushed), then a third loss minutes later."""
    metadata: dict = {}
    metadata = _lost(metadata, 2, 5)
    metadata = _lost(metadata, 3, 5)
    metadata = _lost(metadata, 4, 5)
    assert background_checkpoint.lost_streak(metadata) == 3
    task = SimpleNamespace(id=uuid.UUID(TASK), metadata_json=metadata)
    replay = TaskReplay(resumed=ResumedWork(), history=[], stored_completed_calls=9)
    assert resume_refused(metadata, attempt=5, completed_calls=replay.completed_calls) is None
    metadata = note_resume(task, replay=replay, attempt=5)
    assert background_checkpoint.lost_streak(metadata) == 1


# ---------------------------------------------------------------------------
# 9. Only an execution at the old fixed floor is raised; an operator's bound is kept
# ---------------------------------------------------------------------------


async def _claim(monkeypatch, max_attempts: int, configured: int = 3) -> SimpleNamespace:
    from app.core.config import settings

    monkeypatch.setattr(settings, "orchestration_default_max_attempts", configured)
    monkeypatch.setattr(
        "app.services.sub_agent._execute_sub_agent_task",
        AsyncMock(return_value=sub_agent.SubAgentTaskOutcome(status="completed")),
    )
    session = SimpleNamespace(
        execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: None)), commit=AsyncMock()
    )
    execution = SimpleNamespace(
        id=uuid.uuid4(),
        payload={
            "task_id": TASK,
            "target_agent_id": str(uuid.uuid4()),
            "user_id": str(uuid.uuid4()),
            "task_description": "x",
        },
        adapter_state=None,
        attempt_count=2,
        max_attempts=max_attempts,
    )
    await SubAgentAdapter().execute(SimpleNamespace(db=session), execution)
    return execution


async def test_an_old_row_at_the_old_floor_is_raised(monkeypatch):
    execution = await _claim(monkeypatch, background_checkpoint.MAX_ATTEMPTS)
    assert execution.max_attempts == background_checkpoint.MAX_TASK_ATTEMPTS


@pytest.mark.parametrize("value", [5, 10, 20, 21, 30])
async def test_a_row_with_another_bound_is_left_alone(monkeypatch, value):
    execution = await _claim(monkeypatch, value)
    assert execution.max_attempts == value


@pytest.mark.parametrize("configured", [3, 4, 5, 10, 30])
async def test_a_legacy_row_is_raised_whatever_the_setting_is_now(monkeypatch, configured):
    execution = await _claim(monkeypatch, background_checkpoint.MAX_ATTEMPTS, configured=configured)
    assert execution.max_attempts == background_checkpoint.task_max_attempts(configured)
    assert execution.max_attempts > background_checkpoint.MAX_ATTEMPTS


async def test_compaction_of_the_transcript_never_lowers_the_progress_meter(redis):
    checkpoint = TaskCheckpoint(uuid.UUID(TASK), attempt=2, completed_calls=4)
    checkpoint.offer([*_round("a", "r"), *_round("b", "r"), *_round("c", "r")])
    await checkpoint._task
    assert checkpoint.completed_calls() == 7
    # The loop compacted its transcript: only the latest round is still in it.
    checkpoint.offer([*_round("c", "r"), *_round("d", "r")])
    await checkpoint._task
    assert checkpoint.completed_calls() == 8
    stored = json.loads(redis.store[background_checkpoint.checkpoint_key(TASK)])
    assert stored[background_checkpoint.COMPLETED_CALLS_KEY] == 8


async def test_an_operator_bound_is_kept_at_creation_and_at_claim(monkeypatch):
    assert background_checkpoint.task_max_attempts(10) == 10
    execution = await _claim(monkeypatch, 10, configured=10)
    assert execution.max_attempts == 10, "never raised past the operator's value"
