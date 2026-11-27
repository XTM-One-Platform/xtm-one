"""An assignment run cut by a rollout resumes from its checkpoint, or fails with its reason.

The run that is not a background task's child, driven through the code that
has each behaviour:

- its rounds are checkpointed under a key of its own and replayed by the next
  attempt, the resume recorded on its ``trigger_context`` with the link from
  the attempt lost (``resumed_from``) to the one that resumed it
  (``resumed_as``);
- a worker asked to stop hands the run back, still ``running`` (its work item
  claims held), checkpoint flushed, and the adapter re-queues it at once;
- at most ``MAX_RESUMES`` wordless resumes in a row with no progress between
  them and ``MAX_LOST_RESUMES`` in all: the next loss fails it with
  the cap's reason, at the attempt or at the dead letter, never with the
  hand-back's;
- a retry run starts without the resumes of the run it retries;
- its first trace after a resume carries ``metadata.resumed_from``.
"""

from __future__ import annotations

import inspect
import json
import logging
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.llm.base import ChatMessage
from app.services import assignment_engine, assignment_run_resume, background_checkpoint, orchestration_reconciler
from app.services.background_checkpoint import (
    HANDED_BACK_KEY,
    MAX_GRACEFUL_RESUMES,
    MAX_GRACEFUL_STREAK,
    MAX_LOST_RESUMES,
    MAX_RESUMES,
    RESUME_CAP_REACHED,
    TaskReplay,
    record_resume,
)
from app.services.orchestration_adapters.assignment import AssignmentAdapter
from app.services.turn_checkpoint import ResumedWork
from app.tools.base import ToolCall

RUN = "7b1e2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"


def _round(call_id: str, result: str) -> list[ChatMessage]:
    return [
        ChatMessage(
            role="assistant",
            content="",
            tool_calls=[ToolCall(id=call_id, name="web_fetch", arguments={"url": f"https://example.org/{call_id}"})],
        ),
        ChatMessage(role="tool", content=result, tool_call_id=call_id, name="web_fetch"),
    ]


class _FakeRedis:
    """A Redis store; ``eval`` runs the checkpoint's compare-and-set as one step, as Redis does."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.calls: list[str] = []

    async def set(self, key, value, ex=None):
        self.calls.append("set")
        self.store[key] = value

    async def get(self, key):
        self.calls.append("get")
        return self.store.get(key)

    async def eval(self, script, numkeys, key, payload, epoch, attempt, ttl):
        assert script is assignment_run_resume._WRITE_IF_NOT_LATER_LUA and numkeys == 1
        self.calls.append("eval")
        raw = self.store.get(key)
        doc = json.loads(raw) if raw else {}
        stored = doc.get("place") if isinstance(doc, dict) else None
        if isinstance(stored, list):
            stored_place, own = (int(stored[0]), int(stored[1])), (int(epoch), int(attempt))
            if stored_place > own or (doc.get("ended") is True and stored_place == own):
                return 0
        self.store[key] = payload
        return 1

    async def delete(self, key):
        self.store.pop(key, None)


@pytest.fixture
def redis(monkeypatch):
    fake = _FakeRedis()

    async def _client():
        return fake

    monkeypatch.setattr(background_checkpoint, "_redis", _client)
    return fake


def _resumed(times: int, **extra) -> dict:
    context: dict = dict(extra)
    for attempt in range(2, 2 + times):
        context = record_resume(
            context, attempt=attempt, cause="lost", replayed_calls=1, resumed_from=f"attempt-{attempt - 1}"
        )
    return context


# ---------------------------------------------------------------------------
# 1. Checkpoint and replay
# ---------------------------------------------------------------------------


async def test_a_run_checkpoints_under_its_own_key_never_a_task_s(redis):
    checkpoint = assignment_run_resume.checkpoint_for(RUN, replay=None, attempt=1)
    checkpoint.offer(_round("a", "result a"))
    await checkpoint._task

    assert background_checkpoint.checkpoint_key(f"assignment-run:{RUN}") in redis.store
    assert background_checkpoint.checkpoint_key(RUN) not in redis.store


async def test_the_next_attempt_replays_the_rounds_and_records_its_resume(redis, caplog):
    checkpoint = assignment_run_resume.checkpoint_for(RUN, replay=None, attempt=1)
    checkpoint.offer([*_round("a", "result a"), *_round("b", "result b")])
    await checkpoint._task
    context = background_checkpoint.handed_back({"source": "schedule"}, attempt=1, signal="SIGTERM")
    run = SimpleNamespace(id=RUN, trigger_context=context)

    with caplog.at_level(logging.INFO):
        replay = await assignment_run_resume.prepare_resume(run, attempt=2)

    assert replay.replayed_calls == 2
    assert [m.tool_call_id for m in replay.history if m.role == "tool"] == ["a", "b"]
    assert replay.history[-1].content.startswith("[Resumed:")
    resume = background_checkpoint.resumes_of(run.trigger_context)[-1]
    assert resume["resumed_from"] == "attempt-1" and resume["resumed_as"] == "attempt-2"
    assert "SIGTERM" in resume["cause"] and resume["replayed_calls"] == 2
    assert HANDED_BACK_KEY not in run.trigger_context and run.trigger_context["source"] == "schedule"
    assert any(RUN in r.getMessage() and "claims carried" in r.getMessage() for r in caplog.records)


async def test_a_run_checkpoint_stores_its_secret_values_as_markers_and_the_resume_restores_them(redis):
    from app.core.redaction import known_secrets_scope, remember_secret_value

    with known_secrets_scope(RUN):
        remember_secret_value("0f1e2d3c-4b5a-4968-8776-655443322110", "s3cr3t-token-value")
        checkpoint = assignment_run_resume.checkpoint_for(RUN, replay=None, attempt=1)
        checkpoint.offer(
            [
                ChatMessage(
                    role="assistant",
                    content="",
                    tool_calls=[ToolCall(id="a", name="http", arguments={"token": "s3cr3t-token-value"})],
                ),
                ChatMessage(role="tool", content="echo s3cr3t-token-value", tool_call_id="a", name="http"),
            ]
        )
        await checkpoint._task
        stored = redis.store[background_checkpoint.checkpoint_key(f"assignment-run:{RUN}")]
        assert "s3cr3t-token-value" not in stored, "Redis holds the marker, never the value"
        assert "[[SECRET_VARIABLE:" in stored

        replay = await assignment_run_resume.prepare_resume(SimpleNamespace(id=RUN, trigger_context={}), attempt=2)
        history = assignment_run_resume.restore_history(replay.history)

    call = next(m for m in history if m.tool_calls).tool_calls[0]
    assert call.arguments == {"token": "s3cr3t-token-value"}
    assert next(m for m in history if m.role == "tool").content == "echo s3cr3t-token-value"


async def test_an_attempt_replaced_never_overwrites_the_checkpoint_of_a_later_one(redis, caplog):
    later = assignment_run_resume.checkpoint_for(RUN, replay=None, attempt=3, place=(0, 3))
    later.offer([*_round("a", "result a"), *_round("b", "result b")])
    await later._task
    stale = assignment_run_resume.checkpoint_for(RUN, replay=None, attempt=2, place=(0, 2))
    with caplog.at_level(logging.INFO):
        stale.offer(_round("a", "result a"))
        await stale._task

    replay = await background_checkpoint.load_task_replay(f"assignment-run:{RUN}")
    assert replay.turn_id == "attempt-3" and replay.replayed_calls == 2
    assert any("a later attempt owns it" in r.getMessage() for r in caplog.records)

    newer = assignment_run_resume.checkpoint_for(RUN, replay=None, attempt=1, place=(1, 1))
    newer.offer(_round("c", "result c"))
    await newer._task
    assert (await background_checkpoint.load_task_replay(f"assignment-run:{RUN}")).turn_id == "attempt-1", (
        "a later epoch (a HITL resume) writes over an earlier one"
    )


async def test_a_fenced_checkpoint_write_is_one_compare_and_set_never_a_read_then_a_write(redis):
    """No later attempt's write can land between the place check and the write."""
    checkpoint = assignment_run_resume.checkpoint_for(RUN, replay=None, attempt=2, place=(0, 2))
    checkpoint.offer(_round("a", "result a"))
    await checkpoint._task

    assert redis.calls == ["eval"], "the place is compared and the snapshot written in one Redis script"
    script = assignment_run_resume._WRITE_IF_NOT_LATER_LUA
    assert script.index("redis.call('GET'") < script.index("return 0") < script.index("redis.call('SET'")


async def test_a_stale_attempt_s_hand_back_flush_after_a_newer_write_keeps_the_newer_checkpoint(redis):
    """The race of the review: the old attempt flushes on its hand-back after the new one wrote."""
    stale = assignment_run_resume.checkpoint_for(RUN, replay=None, attempt=2, place=(0, 2))
    stale.offer(_round("a", "result a"))
    await stale._task
    newer = assignment_run_resume.checkpoint_for(RUN, replay=None, attempt=3, place=(0, 3))
    newer.offer([*_round("a", "result a"), *_round("b", "result b")])
    await newer._task

    stale.offer([*_round("a", "result a"), *_round("x", "stale result")])
    await stale.aclose(flush=True)

    replay = await background_checkpoint.load_task_replay(f"assignment-run:{RUN}")
    assert replay.turn_id == "attempt-3" and replay.replayed_calls == 2
    assert [m.tool_call_id for m in replay.history if m.role == "tool"] == ["a", "b"]


def test_a_resumed_run_registers_the_files_it_produced_and_keeps_its_partner_tasks():
    source = inspect.getsource(assignment_engine.execute_assignment_run)
    resume = source.index("if _run_replay is not None:")
    files = source.index("load_run_generated_files(db, run, exclude_ids=set(trigger_file_ids))", resume)
    assert files - resume < 600, "the files the lost attempt produced are registered with its replay"
    assert "handed_back=_handed_back," in source, "a handed-back run's partner tasks stay outstanding"


@pytest.mark.parametrize(
    ("handed_back", "replaced", "cancelled"),
    [
        (False, True, False),  # a live old attempt replaced while a delegation is outstanding
        (True, False, False),  # handed back to the queue
        (False, False, True),  # a terminal outcome: the run is over
    ],
)
async def test_only_an_attempt_that_still_owns_its_run_cancels_its_partner_work(
    monkeypatch, handed_back, replaced, cancelled
):
    """A replaced attempt leaves the outstanding delegation to the attempt that replaced it."""
    delegations = AsyncMock()
    partner_tasks = AsyncMock()
    monkeypatch.setattr("app.services.integration_tools.cancel_outstanding_delegations", delegations)
    monkeypatch.setattr("app.services.integration_tools.cancel_run_partner_tasks", partner_tasks)
    monkeypatch.setattr(assignment_engine, "_run_completed", AsyncMock(return_value=False))
    run = SimpleNamespace(id=uuid.UUID(RUN))
    integ_exec = SimpleNamespace(outstanding=["delegation-1"])

    await assignment_engine._teardown_partner_work(
        object(), run, integ_exec, handed_back=handed_back, replaced=replaced
    )

    assert delegations.await_count == int(cancelled)
    assert partner_tasks.await_count == int(cancelled)


@pytest.mark.parametrize(("status", "cancels"), [("completed", False), ("failed", True), ("cancelled", True)])
async def test_a_completed_run_keeps_its_still_running_partner_tasks(
    monkeypatch: pytest.MonkeyPatch, status: str, cancels: bool
) -> None:
    """#12079: a run that completed keeps the tasks it handed back as still
    running (their answers arrive as a2a.delegation.completed); a failed or
    cancelled run still cancels them (FR-005). Input-required tasks, which wait
    on this run's own answer, are cancelled in every case (FR-025)."""
    delegations = AsyncMock()
    partner_tasks = AsyncMock()
    monkeypatch.setattr("app.services.integration_tools.cancel_outstanding_delegations", delegations)
    monkeypatch.setattr("app.services.integration_tools.cancel_run_partner_tasks", partner_tasks)

    class _Db:
        async def execute(self, _stmt: Any) -> SimpleNamespace:
            return SimpleNamespace(scalar_one_or_none=lambda: status)

    db: Any = _Db()
    run: Any = SimpleNamespace(id=uuid.UUID(RUN))
    await assignment_engine._teardown_partner_work(db, run, SimpleNamespace(), handed_back=False, replaced=False)

    assert delegations.await_count == 1
    assert partner_tasks.await_count == int(cancels)


async def test_an_unreadable_status_still_cancels() -> None:
    class _Db:
        async def execute(self, _stmt: Any) -> None:
            raise RuntimeError("db gone")

    db: Any = _Db()
    run: Any = SimpleNamespace(id=uuid.UUID(RUN))
    assert await assignment_engine._run_completed(db, run) is False


def test_every_replaced_return_of_the_engine_marks_the_attempt_replaced():
    source = inspect.getsource(assignment_engine.execute_assignment_run)
    ending_checks = source.count("await _ended_elsewhere(db, run, place=attempt_place)")
    assert ending_checks == 3
    assert source.count("_replaced = _elsewhere == REPLACED_ATTEMPT") == ending_checks
    assert "replaced=_replaced," in source
    assert "cancel_run_partner_tasks(db, run.id)" not in source.split("finally:")[-1].split(
        "cleanup_mcp_manager"
    )[0], "the teardown cancels only through _teardown_partner_work"


async def test_a_run_lost_with_nothing_checkpointed_starts_from_its_prompt(redis):
    run = SimpleNamespace(id=RUN, trigger_context={})
    replay = await assignment_run_resume.prepare_resume(run, attempt=2)
    assert replay.history == []
    assert len(background_checkpoint.resumes_of(run.trigger_context)) == 1


def test_an_attempt_resumes_a_run_left_running_or_handed_back_only():
    assert assignment_run_resume.resuming({}, reclaimed=True)
    assert assignment_run_resume.resuming({HANDED_BACK_KEY: {"attempt": 1}}, reclaimed=False)
    assert not assignment_run_resume.resuming({}, reclaimed=False)
    assert not assignment_run_resume.resuming(None, reclaimed=False)


async def test_a_run_that_ends_discards_its_checkpoint(redis):
    checkpoint = assignment_run_resume.checkpoint_for(RUN, replay=None, attempt=1)
    checkpoint.offer(_round("a", "result a"))
    await checkpoint._task
    await assignment_run_resume.discard(RUN)
    assert redis.store == {}


# ---------------------------------------------------------------------------
# 2. Hand-back by a stopping worker
# ---------------------------------------------------------------------------


async def test_a_wound_down_run_is_handed_back_with_its_checkpoint_flushed(monkeypatch, caplog):
    from tests.test_background_edition_gate import _Session

    run = SimpleNamespace(id=uuid.UUID(RUN), status="running", trigger_context={"source": "schedule"})
    monkeypatch.setattr("app.core.database.async_session", lambda: _Session([run]))
    monkeypatch.setattr(orchestration_reconciler, "_stop_signal", "SIGTERM")
    checkpoint = SimpleNamespace(aclose=AsyncMock())

    with caplog.at_level(logging.WARNING):
        await assignment_run_resume.hand_back(RUN, checkpoint=checkpoint, attempt=2, place=None)

    checkpoint.aclose.assert_awaited_once_with(flush=True)
    assert run.status == "running", "still held by a live run until the next attempt resumes it"
    back = run.trigger_context[HANDED_BACK_KEY]
    assert back["attempt"] == 2 and back["signal"] == "SIGTERM"
    assert any("handed back" in r.getMessage() and RUN in r.getMessage() for r in caplog.records)


async def test_a_run_that_ended_meanwhile_keeps_its_ending(monkeypatch):
    from tests.test_background_edition_gate import _Session

    run = SimpleNamespace(id=uuid.UUID(RUN), status="completed", trigger_context={})
    monkeypatch.setattr("app.core.database.async_session", lambda: _Session([run]))
    await assignment_run_resume.hand_back(RUN, checkpoint=SimpleNamespace(aclose=AsyncMock()), attempt=2, place=None)
    assert HANDED_BACK_KEY not in run.trigger_context


async def test_the_adapter_requeues_a_handed_back_run_at_once(monkeypatch):
    from app.services.orchestration_adapters import sub_agent

    monkeypatch.setattr(
        "app.services.assignment_engine.execute_assignment_run",
        AsyncMock(return_value=assignment_run_resume.RUN_HANDED_BACK),
    )
    monkeypatch.setattr(sub_agent, "_end_hand_back", AsyncMock(side_effect=lambda ctx, execution: execution))
    monkeypatch.setattr("app.services.ai_hold.close_hold_if_ai_is_back", AsyncMock(return_value=None))
    monkeypatch.setattr(
        "app.services.orchestration_adapters.assignment._bind_memory_chain", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        "app.services.orchestration_adapters.assignment.release_db_connection_before_loop", AsyncMock()
    )
    execution = SimpleNamespace(payload={"run_id": RUN}, attempt_count=1, deadline_at=None)

    result = await AssignmentAdapter().execute(SimpleNamespace(db=SimpleNamespace()), execution)

    # A deferral first in the queue, not a failed attempt: a retryable
    # failure spent an attempt per rollout and dead-lettered the run.
    assert result.kind == "defer"
    assert result.place == "head" and result.backoff_seconds == 0.0
    assert result.adapter_state == sub_agent.HANDED_BACK_STATE
    assert result.error == assignment_run_resume.HANDED_BACK_ERROR


def test_the_engine_winds_a_run_down_into_a_hand_back():
    source = inspect.getsource(assignment_engine.execute_assignment_run)
    assert 'loop_kwargs["on_checkpoint"] = _run_checkpoint.offer' in source
    assert "except TurnWoundDown:" in source
    assert "return assignment_run_resume.RUN_HANDED_BACK" in source
    assert "messages.extend(assignment_run_resume.restore_history(_run_replay.history))" in source


# ---------------------------------------------------------------------------
# 3. The resume cap
# ---------------------------------------------------------------------------


def test_the_resume_past_the_last_is_refused_with_the_run_s_reason():
    assert assignment_run_resume.resume_refused(_resumed(MAX_RESUMES - 1)) is None
    reason = assignment_run_resume.resume_refused(_resumed(MAX_RESUMES))
    assert reason is not None and reason.startswith(RESUME_CAP_REACHED)
    assert "assignment run" in reason and "work item claims are released" in reason
    assert "runs again at once" not in reason
    # Every count is named.
    assert "3 resume(s) in a row after a loss without a word with no progress between them" in reason
    assert "(the most a run makes is 3)" in reason
    assert "3 after a loss without a word in all (the most a run makes is 20)" in reason
    assert "0 in a row after a graceful stop with no progress between them (the most a run makes is 3)" in reason
    assert "0 after a graceful stop in all (the most a run makes is 200)" in reason


def test_a_run_execution_has_an_attempt_for_its_first_run_and_every_resume(monkeypatch):
    from app.core.config import settings

    # A background task's bound: the engine never dead-letters a run
    # that progressed between its losses at its fourth.
    monkeypatch.setattr(settings, "orchestration_default_max_attempts", 3)
    assert assignment_run_resume.execution_max_attempts() == MAX_LOST_RESUMES + 1
    monkeypatch.setattr(settings, "orchestration_default_max_attempts", 8)
    assert assignment_run_resume.execution_max_attempts() == 8
    source = inspect.getsource(assignment_engine)
    assert source.count("max_attempts=assignment_run_resume.execution_max_attempts()") == 3


def test_the_engine_fails_a_run_past_its_resumes_before_it_is_charged():
    source = inspect.getsource(assignment_engine.execute_assignment_run)
    refused = source.index("assignment_run_resume.resume_refused(\n                run.trigger_context,")
    assert refused < source.index("await record_agentic_execution(SOURCE_ASSIGNMENT)")
    assert "_release_run_work_item(run, agent, failed=True, error=_cap_reached)" in source
    cancel = source.index("await assignment_run_resume.cancel_partner_tasks(db, run.id)")
    assert source.index("await db.commit()", cancel) < source.index("return", cancel), (
        "the concluded delegation rows are committed before the attempt returns"
    )


def _dead_lettered_run(context: dict) -> tuple[SimpleNamespace, SimpleNamespace, SimpleNamespace]:
    run = SimpleNamespace(
        id=uuid.UUID(RUN),
        status="running",
        error=None,
        completed_at=None,
        agent_id=uuid.uuid4(),
        trigger_context=context,
        failure_cause=None,
        failure_determination=None,
    )
    session = SimpleNamespace(
        execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: run)), commit=AsyncMock()
    )
    execution = SimpleNamespace(
        payload={"run_id": RUN},
        last_error=assignment_run_resume.HANDED_BACK_ERROR,
        attempt_count=4,
        max_attempts=4,
        state="dead_letter",
        deadline_at=datetime.now(UTC) + timedelta(hours=1),
    )
    return run, session, execution


async def test_a_run_dead_lettered_at_its_cap_fails_with_the_cap_not_the_hand_back(monkeypatch, redis):
    monkeypatch.setattr("app.services.assignment_run_wake.schedule", lambda run_id: None)
    cancelled = AsyncMock()
    monkeypatch.setattr("app.services.integration_tools.cancel_run_partner_tasks", cancelled)
    context = background_checkpoint.handed_back(_resumed(MAX_RESUMES), attempt=4, signal="SIGTERM")
    run, session, execution = _dead_lettered_run(context)
    redis.store[background_checkpoint.checkpoint_key(f"assignment-run:{RUN}")] = "{}"

    await AssignmentAdapter().on_dead_letter(SimpleNamespace(db=session), execution)

    assert run.status == "failed"
    assert run.error.startswith(RESUME_CAP_REACHED)
    assert "runs again at once" not in run.error and "SIGTERM" in run.error
    stored = json.loads(redis.store[background_checkpoint.checkpoint_key(f"assignment-run:{RUN}")])
    assert stored["ended"] is True and "entries" not in stored, "nothing is left to resume"
    # The partner tasks the hand-backs left are cancelled.
    cancelled.assert_awaited_once_with(session, run.id)


async def test_a_run_dead_lettered_after_a_worker_gone_names_no_worker_detail(monkeypatch, redis):
    # A take-back from a worker gone writes its host,
    # version and memory into the execution's error; the run's viewers read
    # the generic words instead.
    from app.services import orchestration_engine
    from app.services.orchestration_adapters import assignment as assignment_adapter

    monkeypatch.setattr("app.services.assignment_run_wake.schedule", lambda run_id: None)
    monkeypatch.setattr("app.services.integration_tools.cancel_run_partner_tasks", AsyncMock())
    run, session, execution = _dead_lettered_run({})
    execution.last_error = "worker-7f9c (version 1.4.1, 91% of its memory) went silent, with no attempt left."
    execution.payload = {
        **execution.payload,
        orchestration_engine.LEASE_EXPIRY_PAYLOAD_KEY: {"owner_record": {"host": "worker-7f9c"}},
    }

    await AssignmentAdapter().on_dead_letter(SimpleNamespace(db=session), execution)

    assert run.status == "failed"
    assert run.error == assignment_adapter.WORKER_GONE_DEAD_LETTER_ERROR
    assert "worker-7f9c" not in run.error


async def test_a_run_never_resumed_keeps_its_own_dead_letter_reason(monkeypatch, redis):
    monkeypatch.setattr("app.services.assignment_run_wake.schedule", lambda run_id: None)
    cancelled = AsyncMock()
    monkeypatch.setattr("app.services.integration_tools.cancel_run_partner_tasks", cancelled)
    run, session, execution = _dead_lettered_run({})
    execution.last_error = "Deadline exceeded"

    await AssignmentAdapter().on_dead_letter(SimpleNamespace(db=session), execution)

    assert run.status == "failed" and run.error == "Deadline exceeded"


async def test_a_first_attempt_dead_lettered_with_nothing_recorded_cancels_its_partner_work(monkeypatch, redis):
    """A first attempt whose worker died with a delegation outstanding: no attempt will collect it."""
    monkeypatch.setattr("app.services.assignment_run_wake.schedule", lambda run_id: None)
    cancelled = AsyncMock()
    monkeypatch.setattr("app.services.integration_tools.cancel_run_partner_tasks", cancelled)
    run, session, execution = _dead_lettered_run({})
    execution.last_error = "Worker lost"
    assert not assignment_run_resume.may_hold_checkpoint(run.trigger_context)

    await AssignmentAdapter().on_dead_letter(SimpleNamespace(db=session), execution)

    assert run.status == "failed"
    cancelled.assert_awaited_once_with(session, run.id)


async def test_a_first_attempt_dead_lettered_while_winding_down_cannot_bring_its_checkpoint_back(monkeypatch, redis):
    """A first attempt records no fence: the dead letter's tombstone still covers its writer at (0, 1)."""
    monkeypatch.setattr("app.services.assignment_run_wake.schedule", lambda run_id: None)
    monkeypatch.setattr("app.services.integration_tools.cancel_run_partner_tasks", AsyncMock())
    key = background_checkpoint.checkpoint_key(f"assignment-run:{RUN}")
    first = assignment_run_resume.checkpoint_for(RUN, replay=None, attempt=1, place=(0, 1))
    first.offer(_round("a", "result a"))
    await first._task
    _run, session, execution = _dead_lettered_run({})
    execution.attempt_count = 1
    execution.last_error = "Worker lost"

    await AssignmentAdapter().on_dead_letter(SimpleNamespace(db=session), execution)

    stored = json.loads(redis.store[key])
    assert stored["ended"] is True and stored["place"] == [0, 1]
    first.offer([*_round("a", "result a"), *_round("x", "late")])
    await first.aclose(flush=True)
    assert json.loads(redis.store[key])["ended"] is True, "the winding-down first attempt is fenced"
    assert (await background_checkpoint.load_task_replay(f"assignment-run:{RUN}")).history == []


def test_the_dead_letter_tombstone_is_at_or_after_every_writer_of_the_execution():
    assert assignment_run_resume.dead_letter_place((0, 0), attempts=1) == (0, 1)
    assert assignment_run_resume.dead_letter_place((0, 3), attempts=2) == (0, 3)
    assert assignment_run_resume.dead_letter_place((1, 0), attempts=2) == (1, 2)
    assert assignment_run_resume.dead_letter_place((0, 0), attempts=None) == (0, 0)


async def test_a_stale_writer_cannot_bring_back_the_checkpoint_of_a_run_that_ended(redis):
    """The ending leaves a tombstone at its place: a replaced attempt's writer in flight is fenced."""
    key = background_checkpoint.checkpoint_key(f"assignment-run:{RUN}")
    replaced = assignment_run_resume.checkpoint_for(RUN, replay=None, attempt=2, place=(0, 2))
    replaced.offer(_round("a", "result a"))
    await replaced._task
    newer = assignment_run_resume.checkpoint_for(RUN, replay=None, attempt=3, place=(0, 3))
    newer.offer(_round("b", "result b"))
    await newer._task

    await assignment_run_resume.discard(RUN, place=(0, 3))
    assert json.loads(redis.store[key]) == {**json.loads(redis.store[key]), "place": [0, 3], "ended": True}
    assert "entries" not in json.loads(redis.store[key]), "the tombstone holds no transcript"

    replaced.offer([*_round("a", "result a"), *_round("x", "stale")])
    await replaced.aclose(flush=True)
    same_place = assignment_run_resume.checkpoint_for(RUN, replay=None, attempt=3, place=(0, 3))
    same_place.offer([*_round("b", "result b"), *_round("y", "late")])
    await same_place._task

    assert json.loads(redis.store[key])["ended"] is True, "no writer at or before the ending's place writes again"
    assert (await background_checkpoint.load_task_replay(f"assignment-run:{RUN}")).history == []

    later_epoch = assignment_run_resume.checkpoint_for(RUN, replay=None, attempt=1, place=(1, 1))
    later_epoch.offer(_round("c", "result c"))
    await later_epoch._task
    assert json.loads(redis.store[key])["place"] == [1, 1], "a later epoch (a HITL resume) still writes"


def test_every_attempt_stops_its_checkpoint_writer_in_its_teardown():
    source = inspect.getsource(assignment_engine.execute_assignment_run)
    teardown = source.split("finally:")[-1]
    assert "if _run_checkpoint is not None and not _handed_back:" in teardown
    assert "assignment_run_resume.discard(run.id, place=attempt_place)" in source
    assert "assignment_run_resume.discard(run.id)\n" not in source


def test_a_retry_run_starts_without_the_resumes_of_the_run_it_retries():
    context = background_checkpoint.handed_back(_resumed(2, source="schedule"), attempt=3, signal="SIGTERM")
    clean = assignment_run_resume.without_resume_state(context)
    assert clean == {"source": "schedule"}
    plain = {"source": "schedule"}
    assert assignment_run_resume.without_resume_state(plain) is plain
    source = inspect.getsource(assignment_engine)
    assert "assignment_run_resume.without_resume_state(\n" in source


# ---------------------------------------------------------------------------
# 4. The resume trace
# ---------------------------------------------------------------------------


def test_a_resumed_run_names_its_first_trace():
    assert assignment_run_resume.resumed_trace_meta(RUN, {}) is None
    link = assignment_run_resume.resumed_trace_meta(RUN, _resumed(1))["resumed_from"]
    assert link["entry"] == "assignment_run" and link["run_id"] == RUN
    assert link["attempt"] == "attempt-1" and link["resumed_as"] == "attempt-2"
    assert link["resume"] == 1 and link["of"] == MAX_LOST_RESUMES
    assert link["streak"] == 1 and link["streak_of"] == MAX_RESUMES


def test_the_run_trace_carries_the_link():
    source = inspect.getsource(assignment_engine.execute_assignment_run)
    assert "assignment_run_resume.resumed_trace_meta(run.id, run.trigger_context)" in source


# ---------------------------------------------------------------------------
# 5. A graceful stop does not spend the resume cap (as for tasks)
# ---------------------------------------------------------------------------


async def _hand_back_and_resume(context: dict, attempt: int, calls: int | None = None) -> dict:
    """*context* after one rollout: the run handed back at *attempt*, resumed by the next claim.

    The hand-back is a deferral that gives the attempt back: the claim
    that resumes it runs at the same attempt number. *calls*: the run's
    completed calls when it is resumed; by default it progressed since the
    rollout before (``attempt * 10``), as a run at work does.
    """
    completed = attempt * 10 if calls is None else calls
    context = background_checkpoint.handed_back(context, attempt=attempt, signal="SIGTERM")
    assert assignment_run_resume.resume_refused(context, attempt=attempt, completed_calls=completed) is None
    run = SimpleNamespace(id=RUN, trigger_context=context)
    replay = TaskReplay(resumed=ResumedWork(), history=[], stored_completed_calls=completed)
    await assignment_run_resume.prepare_resume(run, attempt=attempt, replay=replay)
    return run.trigger_context


async def test_three_hand_backs_then_a_fourth_resume_is_allowed(redis):
    # Three rollouts in a row used to fail the run at the
    # fourth claim with "Resume cap reached ... after 3 resume(s)".
    context: dict = {}
    for attempt in (1, 2, 3):
        context = await _hand_back_and_resume(context, attempt)
    assert assignment_run_resume.resume_counts(context) == (0, 3)
    assert all(r["graceful"] for r in background_checkpoint.resumes_of(context)), "every hand-back is recorded"
    handed = background_checkpoint.handed_back(context, attempt=4, signal="SIGTERM")
    assert assignment_run_resume.resume_refused(handed, attempt=4) is None, "a fourth rollout: graceful again"
    assert assignment_run_resume.resume_refused(context, attempt=5) is None, "none of the three spent the cap"


async def test_three_losses_without_a_word_are_still_refused_with_both_counts(redis):
    context = _resumed(MAX_RESUMES)
    # The rollout came with no progress since the losses: their streak stands.
    context = await _hand_back_and_resume(context, 5, calls=1)
    assert assignment_run_resume.resume_counts(context) == (3, 1)
    reason = assignment_run_resume.resume_refused(context, attempt=6)
    assert reason is not None and reason.startswith(RESUME_CAP_REACHED)
    assert "the assignment run lost its worker again after 3 resume(s)" in reason
    assert "3 resume(s) in a row after a loss without a word with no progress between them" in reason
    assert "1 after a graceful stop in all (the most a run makes is 200)" in reason


async def test_graceful_resumes_past_their_own_bound_are_refused_with_their_own_reason(redis):
    context: dict = {}
    for attempt in range(1, 1 + MAX_GRACEFUL_RESUMES):
        context = await _hand_back_and_resume(context, attempt)
    assert MAX_GRACEFUL_RESUMES == 200
    handed = background_checkpoint.handed_back(context, attempt=201, signal="SIGTERM")
    reason = assignment_run_resume.resume_refused(handed, attempt=201, completed_calls=10_000)
    assert reason is not None and reason.startswith(RESUME_CAP_REACHED)
    assert "the assignment run was stopped gracefully again after 200 graceful resume(s), at the bound of 200" in reason
    assert "0 resume(s) in a row after a loss without a word" in reason and "SIGTERM" in reason
    assert assignment_run_resume.resume_refused(context, attempt=202) is None, "a crash is still resumed"


async def test_a_run_that_progresses_between_25_rollouts_is_resumed_at_each(redis):
    # Production, 2 Nov 2026: assignment runs alive for two hours under a
    # rollout every 4 to 8 minutes failed at the 21st graceful stop while
    # their completed calls grew between every take-back.
    context: dict = {}
    for attempt in range(1, 26):
        context = await _hand_back_and_resume(context, attempt)
    assert assignment_run_resume.resume_counts(context) == (0, 25)
    assert assignment_run_resume.graceful_streak(context) == 1
    handed = background_checkpoint.handed_back(context, attempt=26, signal="SIGTERM")
    assert assignment_run_resume.resume_refused(handed, attempt=26, completed_calls=260) is None


def _wait_round(call_id: str, message: str) -> list[ChatMessage]:
    return [
        ChatMessage(
            role="assistant",
            content="",
            tool_calls=[ToolCall(id=call_id, name="wait", arguments={"duration": "5m"})],
        ),
        ChatMessage(role="tool", content=message, tool_call_id=call_id, name="wait"),
    ]


async def test_a_run_waiting_on_ci_across_four_graceful_take_backs_completes_its_wait(redis):
    # Production, 2 Nov 2026: a run waiting on CI was taken back three
    # times in a row during the same wait; each cut cancelled the call, its
    # completed calls stayed flat and the run failed at the bound of 3
    # graceful resumes with no progress. A wait cut by a stopping worker now
    # completes, is checkpointed, and counts as progress at the resume.
    from app.tools import wait_policy as wp

    budget = wp.WaitBudget(wp.WaitPolicy(per_call_seconds=300, total_seconds=1800))
    context: dict = {}
    replay: TaskReplay | None = None
    for attempt in range(1, 6):
        checkpoint = assignment_run_resume.checkpoint_for(RUN, replay=replay, attempt=attempt)
        last = attempt == 5
        result = await wp.execute_wait(
            {"duration": "1s" if last else "5m"},
            budget=budget,
            poll_interval=0.01,
            stopping=lambda last=last: not last,
        )
        if last:
            assert result.reason == wp.WAIT_REASON_ELAPSED, "the last worker completes the wait"
            break
        assert result.reason == wp.WAIT_REASON_WORKER_STOPPING
        checkpoint.offer(_wait_round(f"wait-{attempt}", result.message))
        await checkpoint._task
        context = background_checkpoint.handed_back(context, attempt=attempt, signal="SIGTERM")
        replay = await assignment_run_resume.load_replay(RUN)
        assert replay.completed_calls == attempt, "every cut wait is a completed call"
        assert assignment_run_resume.resume_refused(context, attempt=attempt, completed_calls=replay.completed_calls) is None
        run = SimpleNamespace(id=RUN, trigger_context=context)
        await assignment_run_resume.prepare_resume(run, attempt=attempt, replay=replay)
        context = run.trigger_context
        assert assignment_run_resume.graceful_streak(context) == 1
    assert assignment_run_resume.resume_counts(context) == (0, 4)


async def test_three_graceful_stops_with_flat_progress_end_the_run_at_the_fourth(redis):
    context: dict = {}
    for attempt in (1, 2, 3):
        context = await _hand_back_and_resume(context, attempt, calls=7)
    assert assignment_run_resume.graceful_streak(context) == MAX_GRACEFUL_STREAK == 3
    handed = background_checkpoint.handed_back(context, attempt=4, signal="SIGTERM")
    reason = assignment_run_resume.resume_refused(handed, attempt=4, completed_calls=7)
    assert reason is not None and reason.startswith(RESUME_CAP_REACHED)
    assert "stopped gracefully again after 3 graceful resume(s), at the bound of 3 graceful resume(s) in a row" in reason
    assert "3 in a row after a graceful stop with no progress between them (the most a run makes is 3)" in reason
    assert assignment_run_resume.resume_refused(handed, attempt=4, completed_calls=8) is None, "progress restarts it"
    # A take-back on its worker's word is graceful too, under the same streak.
    assert assignment_run_resume.resume_refused(context, attempt=4, taken_back_on_its_word=True, completed_calls=7)
    assert assignment_run_resume.resume_refused(context, attempt=5, completed_calls=7) is None, "a crash is its own"


def test_graceful_resumes_of_the_older_writer_never_add_to_the_graceful_streak():
    # Written before ``completed_calls`` existed: no reliable progress, no streak.
    handed = "its worker was asked to stop (SIGTERM) and handed it back"
    legacy = {"resumes": [{"attempt": a, "cause": handed, "graceful": False} for a in range(2, 27)]}
    assert assignment_run_resume.resume_counts(legacy) == (0, 25)
    assert assignment_run_resume.graceful_streak(legacy) == 0
    assert assignment_run_resume.resume_refused(legacy, attempt=27, taken_back_on_its_word=True) is None


async def test_a_claim_lost_without_a_word_after_a_hand_back_is_counted(redis, caplog):
    # The claim resuming the hand-back crashed before ``prepare_resume``
    # cleared it: it spent its attempt, so the next one runs at a later one.
    handed = background_checkpoint.handed_back(_resumed(MAX_RESUMES), attempt=5, signal="SIGTERM")
    assert assignment_run_resume.resume_refused(handed, attempt=5) is None
    reason = assignment_run_resume.resume_refused(handed, attempt=6)
    assert reason is not None and "was lost without a word before it resumed" in reason
    run = SimpleNamespace(id=RUN, trigger_context=background_checkpoint.handed_back({}, attempt=1, signal="SIGTERM"))
    with caplog.at_level(logging.INFO):
        await assignment_run_resume.prepare_resume(run, attempt=2)
    (resume,) = background_checkpoint.resumes_of(run.trigger_context)
    assert resume["graceful"] is False and "lost without a word" in resume["cause"]
    assert any("a counted resume; 1 of 3 in a row" in r.getMessage() for r in caplog.records)


async def test_a_run_left_running_without_a_hand_back_is_a_counted_resume(redis):
    run = SimpleNamespace(id=RUN, trigger_context={"orphaned": {"last_heartbeat_at": "t"}})
    assert assignment_run_resume.next_resume_is_graceful(run.trigger_context, attempt=2) is False
    await assignment_run_resume.prepare_resume(run, attempt=2)
    assert assignment_run_resume.resume_counts(run.trigger_context) == (1, 0)


async def test_a_hand_back_resume_logs_graceful_and_both_counts(redis, caplog):
    run = SimpleNamespace(id=RUN, trigger_context=background_checkpoint.handed_back({}, attempt=2, signal="SIGTERM"))
    with caplog.at_level(logging.INFO):
        await assignment_run_resume.prepare_resume(run, attempt=2)
    line = next(r.getMessage() for r in caplog.records if "resumed as attempt" in r.getMessage())
    assert "a graceful resume; 0 of 3 in a row after a loss without a word with no progress, 0 of 20 in all" in line
    assert "1 of 3 in a row after a graceful stop with no progress, 1 of 200 after a graceful stop in all" in line


async def test_the_trace_link_counts_only_the_resumes_against_the_cap(redis):
    context: dict = {}
    for attempt in (1, 2, 3, 4):
        context = await _hand_back_and_resume(context, attempt)
    link = assignment_run_resume.resumed_trace_meta(RUN, context)["resumed_from"]
    assert link["entry"] == "assignment_run"
    assert link["resume"] == 0 and link["of"] == MAX_LOST_RESUMES
    assert link["streak"] == 0 and link["streak_of"] == MAX_RESUMES
    assert link["graceful"] is True and link["graceful_resumes"] == 4
    assert link["graceful_of"] == MAX_GRACEFUL_RESUMES
    assert link["graceful_streak"] == 1 and link["graceful_streak_of"] == MAX_GRACEFUL_STREAK


def test_a_resume_recorded_before_the_flag_is_read_by_its_cause():
    # Older resumed runs carry no ``graceful`` key: a hand-back's cause
    # ("... and handed it back") makes it graceful, any other a counted one.
    legacy = {
        "resumes": [
            {"attempt": 2, "cause": "its worker was asked to stop (SIGTERM) and handed it back"},
            {"attempt": 3, "cause": "its worker was asked to stop (SIGTERM) and handed it back"},
            {"attempt": 4, "cause": "its worker stopped or lost its lease before the turn ended"},
        ]
    }
    assert assignment_run_resume.resume_counts(legacy) == (1, 2)
    assert assignment_run_resume.resume_refused(legacy, attempt=5) is None


def test_the_engine_reads_the_attempt_to_tell_a_graceful_resume():
    source = inspect.getsource(assignment_engine.execute_assignment_run)
    refused = source[source.index("assignment_run_resume.resume_refused(") :]
    refused = refused[: refused.index(")")]
    assert "attempt=attempt" in refused
    assert "completed_calls=_run_replay.completed_calls" in refused
    assert "taken_back_on_its_word=taken_back_on_its_word" in refused


async def test_the_claim_resuming_a_handed_back_run_ends_its_deferral_record(monkeypatch):
    # The hand-back deferral is no AI hold: its record ends as the next claim
    # starts, before the AI gate is read, as for a background task.
    from app.services.orchestration_adapters import sub_agent

    order: list[str] = []

    async def _end(ctx, execution):
        order.append("end_hand_back")
        return execution

    async def _gate(ctx, execution, **_kwargs):
        order.append("ai_gate")
        return None

    monkeypatch.setattr(sub_agent, "_end_hand_back", _end)
    monkeypatch.setattr("app.services.ai_hold.close_hold_if_ai_is_back", _gate)
    monkeypatch.setattr(
        "app.services.assignment_engine.execute_assignment_run",
        AsyncMock(return_value=assignment_run_resume.RUN_HANDED_BACK),
    )
    monkeypatch.setattr(
        "app.services.orchestration_adapters.assignment._bind_memory_chain", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        "app.services.orchestration_adapters.assignment.release_db_connection_before_loop", AsyncMock()
    )
    execution = SimpleNamespace(payload={"run_id": RUN}, attempt_count=1, deadline_at=None)

    await AssignmentAdapter().execute(SimpleNamespace(db=SimpleNamespace()), execution)

    assert order == ["end_hand_back", "ai_gate"]


async def test_a_hand_back_keeps_its_charge_recorded_for_a_durable_credit(monkeypatch):
    # The hand-back credits nothing and removes no
    # record, so no worker death or refund failure between the two loses the
    # charge. The claim resuming the run (or the dead letter) credits it.
    from app.services.attempt_charges import recorded_charge_id, with_recorded_charge
    from tests.test_background_edition_gate import _Session

    context = with_recorded_charge({}, charge_id="charge-2", user_id=uuid.uuid4(), attempt=2, epoch=0)
    run = SimpleNamespace(id=uuid.UUID(RUN), status="running", agent_id=uuid.uuid4(), trigger_context=context)
    monkeypatch.setattr("app.core.database.async_session", lambda: _Session([run]))
    refund = AsyncMock(return_value=True)
    monkeypatch.setattr("app.services.quota.refund_agentic_usage_for_provider_outage", refund)

    await assignment_run_resume.hand_back(
        RUN, checkpoint=SimpleNamespace(aclose=AsyncMock()), attempt=2, place=(0, 2)
    )

    assert recorded_charge_id(run.trigger_context) == "charge-2" and HANDED_BACK_KEY in run.trigger_context
    refund.assert_not_awaited()


def test_the_claim_resuming_a_hand_back_takes_the_handed_back_attempt_s_charge():
    # The deferral gives the attempt back: the resuming claim runs at the same
    # attempt number, and the charge of that attempt is the lost one.
    from app.services.attempt_charges import lost_attempt_charge, with_recorded_charge

    context = with_recorded_charge({}, charge_id="charge-2", user_id=uuid.uuid4(), attempt=2, epoch=0)
    context = background_checkpoint.handed_back(context, attempt=2, signal="SIGTERM")
    run = SimpleNamespace(status="running", trigger_context=context)
    back = assignment_run_resume.handed_back_attempt(context)
    assert back == 2
    charge = lost_attempt_charge(run, attempt=2, epoch=0, handed_back_at=back)
    assert charge is not None and charge.charge_id == "charge-2"
    assert lost_attempt_charge(run, attempt=2, epoch=0) is None, "not without the hand-back"
    assert lost_attempt_charge(run, attempt=2, epoch=1, handed_back_at=back) is None, "never another epoch"


def test_the_engine_passes_the_hand_back_to_the_lost_charge():
    source = inspect.getsource(assignment_engine.execute_assignment_run)
    assert "handed_back_at=assignment_run_resume.handed_back_attempt(run.trigger_context)" in source


def test_a_resume_written_by_the_older_writer_with_graceful_false_is_read_by_its_cause():
    # An older assignment writer called ``record_resume`` with its default
    # ``graceful: false``.
    handed = "its worker was asked to stop (SIGTERM) and handed it back"
    legacy = {"resumes": [{"attempt": a, "cause": handed, "graceful": False} for a in (2, 3, 4)]}
    assert assignment_run_resume.resume_counts(legacy) == (0, 3)
    assert assignment_run_resume.resume_refused(legacy, attempt=5) is None, "a fourth resume is allowed"
    link = assignment_run_resume.resumed_trace_meta(RUN, legacy)["resumed_from"]
    assert link["graceful"] is True and link["resume"] == 0 and link["graceful_resumes"] == 3
    silent = {"resumes": [{"attempt": 2, "cause": "its worker went silent", "graceful": False}]}
    assert assignment_run_resume.resume_counts(silent) == (1, 0)


def test_the_graceful_writer_never_records_a_hand_back_cause_on_a_counted_resume():
    # What makes the legacy reading safe: a counted resume after a hand-back
    # carries the "lost without a word" cause, never the hand-back's.
    handed = background_checkpoint.handed_back({}, attempt=1, signal="SIGTERM")
    assert assignment_run_resume.next_resume_is_graceful(handed, attempt=2) is False
    assert not assignment_run_resume.lost_cause(handed, attempt=2).endswith("and handed it back")
    assert assignment_run_resume.next_resume_is_graceful(handed, attempt=1) is True
    assert assignment_run_resume.lost_cause(handed, attempt=1).endswith("and handed it back")


def test_the_dead_letter_tombstone_covers_a_handed_back_first_attempt():
    # The hand-back deferral gives attempt 1 back, so
    # a dead letter at the deadline before any claim reads attempts 0.
    handed = background_checkpoint.handed_back({}, attempt=1, signal="SIGTERM")
    back = assignment_run_resume.handed_back_attempt(handed)
    assert assignment_run_resume.dead_letter_place((0, 0), attempts=0, handed_back=back) == (0, 1)
    assert assignment_run_resume.handed_back_attempt({}) is None


async def test_a_first_attempt_handed_back_then_dead_lettered_fences_its_checkpoint(monkeypatch, redis):
    key = background_checkpoint.checkpoint_key(f"assignment-run:{RUN}")
    first = assignment_run_resume.checkpoint_for(RUN, replay=None, attempt=1, place=(0, 1))
    first.offer(_round("a", "result a"))
    await first._task
    _run, session, execution = _dead_lettered_run(
        background_checkpoint.handed_back({}, attempt=1, signal="SIGTERM")
    )
    execution.attempt_count = 0
    execution.last_error = "deadline exceeded"

    await AssignmentAdapter().on_dead_letter(SimpleNamespace(db=session), execution)

    stored = json.loads(redis.store[key])
    assert stored["ended"] is True and stored["place"] == [0, 1]
    assert (await background_checkpoint.load_task_replay(f"assignment-run:{RUN}")).history == []


# ---------------------------------------------------------------------------
# 6. Only wordless losses with no progress between them spend the cap
# ---------------------------------------------------------------------------


def _lost_with(context: dict, *, attempt: int, completed: int) -> dict:
    """*context* after one more resume after a loss without a word, the run at *completed* calls in all."""
    return record_resume(
        context, attempt=attempt, cause="lost", replayed_calls=4, resumed_from=None, completed_calls=completed
    )


def test_a_run_that_progressed_between_its_wordless_losses_is_resumed_past_three():
    # Production, 1 Nov 2026: a pick-up run made 56 to 94 calls between three
    # rollouts and failed at the third loss with "3 resume(s) after a loss
    # without a word". Progress between losses starts the streak again.
    context: dict = {}
    for attempt, completed in zip(range(2, 2 + 10), range(10, 110, 10), strict=True):
        assert assignment_run_resume.resume_refused(context, attempt=attempt, completed_calls=completed) is None
        context = _lost_with(context, attempt=attempt, completed=completed)
    assert assignment_run_resume.resume_counts(context) == (10, 0)
    assert assignment_run_resume.lost_streak(context) == 1


def test_a_run_with_flat_progress_still_fails_at_the_fourth_wordless_loss():
    context: dict = {}
    for attempt in (2, 3, 4):
        assert assignment_run_resume.resume_refused(context, attempt=attempt, completed_calls=12) is None
        context = _lost_with(context, attempt=attempt, completed=12)
    reason = assignment_run_resume.resume_refused(context, attempt=5, completed_calls=12)
    assert reason is not None and reason.startswith(RESUME_CAP_REACHED)
    assert "3 resume(s) in a row after a loss without a word with no progress between them" in reason
    # One call more since the last resume is progress: the streak starts again.
    assert assignment_run_resume.resume_refused(context, attempt=5, completed_calls=13) is None


def test_progress_minutes_later_restarts_the_streak_and_the_total_still_bounds_it():
    context: dict = {}
    for attempt in (2, 3):
        context = _lost_with(context, attempt=attempt, completed=5)
    assert assignment_run_resume.lost_streak(context) == 2
    context = _lost_with(context, attempt=4, completed=9)
    assert assignment_run_resume.lost_streak(context) == 1, "the run progressed: its streak starts again"
    for attempt in range(5, 2 + MAX_LOST_RESUMES):
        context = _lost_with(context, attempt=attempt, completed=attempt * 10)
    assert assignment_run_resume.resume_counts(context)[0] == MAX_LOST_RESUMES
    reason = assignment_run_resume.resume_refused(context, attempt=40, completed_calls=10_000)
    assert reason is not None and "20 after a loss without a word in all (the most a run makes is 20)" in reason


def test_a_take_back_from_a_stopping_or_replaced_worker_is_a_graceful_resume():
    context = _resumed(MAX_RESUMES)
    assert assignment_run_resume.resume_refused(context, attempt=5) is not None
    assert assignment_run_resume.resume_refused(context, attempt=5, taken_back_on_its_word=True) is None
    assert "said it was stopping" in assignment_run_resume.lost_cause(context, taken_back_on_its_word=True)


async def test_the_resume_records_the_run_s_completed_calls_and_a_generic_cause(redis, caplog):
    checkpoint = assignment_run_resume.checkpoint_for(RUN, replay=None, attempt=1, place=(0, 1))
    checkpoint.offer(_round("a", "result a") + _round("b", "result b"))
    await checkpoint._task
    stored = json.loads(redis.store[background_checkpoint.checkpoint_key(f"assignment-run:{RUN}")])
    assert stored["completed_calls"] == 2, "the run checkpoint carries its progress"

    replay = await assignment_run_resume.load_replay(RUN)
    assert replay.completed_calls == 2
    run = SimpleNamespace(id=RUN, trigger_context={})
    with caplog.at_level(logging.INFO):
        assert await assignment_run_resume.prepare_resume(run, attempt=2, replay=replay) is replay
    (resume,) = background_checkpoint.resumes_of(run.trigger_context)
    # No worker host, version or memory on a run anyone who sees it reads.
    assert resume["completed_calls"] == 2
    assert resume["cause"] == "its worker stopped or lost its lease before the turn ended"
    line = next(r.getMessage() for r in caplog.records if "resumed as attempt" in r.getMessage())
    assert "(2 completed in all)" in line and "1 of 3 in a row" in line and "1 of 20 in all" in line

    # The next attempt goes on from there: its writer counts on from the replay.
    later = assignment_run_resume.checkpoint_for(RUN, replay=replay, attempt=2, place=(0, 2))
    later.offer(_round("c", "result c"))
    await later._task
    assert (await assignment_run_resume.load_replay(RUN)).completed_calls == 3


def test_the_engine_reads_the_checkpoint_before_it_decides_the_resume():
    source = inspect.getsource(assignment_engine.execute_assignment_run)
    loaded = source.index("_run_replay = await assignment_run_resume.load_replay(")
    assert loaded < source.index("assignment_run_resume.resume_refused(")
    assert "replay=_run_replay," in source, "the replay is read once, and recorded by prepare_resume"


def test_the_assignment_adapter_takes_back_runs_from_a_worker_gone():
    assert AssignmentAdapter.resume_when_worker_gone is True
    assert "assignment" in orchestration_reconciler.resumable_adapters([AssignmentAdapter()])


async def test_the_adapter_hands_the_take_back_to_the_engine_and_raises_a_legacy_bound(monkeypatch):
    from app.services import orchestration_engine
    from app.services.orchestration_adapters import sub_agent

    execute = AsyncMock(return_value=None)
    monkeypatch.setattr("app.services.assignment_engine.execute_assignment_run", execute)
    monkeypatch.setattr(sub_agent, "_end_hand_back", AsyncMock(side_effect=lambda ctx, execution: execution))
    monkeypatch.setattr("app.services.ai_hold.close_hold_if_ai_is_back", AsyncMock(return_value=None))
    monkeypatch.setattr(
        "app.services.orchestration_adapters.assignment._bind_memory_chain", AsyncMock(return_value=None)
    )
    monkeypatch.setattr("app.services.orchestration_adapters.assignment.release_db_connection_before_loop", AsyncMock())
    raised = AsyncMock()
    monkeypatch.setattr(sub_agent, "_raise_attempt_bound", raised)
    run = SimpleNamespace(status="completed", error=None)
    db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: run)))
    monkeypatch.setattr("app.services.assignment_run_wake.schedule", lambda run_id: None)
    execution = SimpleNamespace(
        id=uuid.uuid4(),
        payload={
            "run_id": RUN,
            orchestration_engine.LEASE_EXPIRY_PAYLOAD_KEY: {
                "attempt": 2,
                "owner_record": {"state": "running", "replaced_by_deploy": True},
            },
        },
        adapter_state=orchestration_engine.OWNER_GONE_STATE,
        attempt_count=2,
        max_attempts=4,
        deadline_at=None,
    )

    await AssignmentAdapter().execute(SimpleNamespace(db=db, cancel_event=None), execution)

    raised.assert_awaited_once()
    kwargs = execute.await_args.kwargs
    assert kwargs["taken_back_on_its_word"] is True, "a worker a deploy replaced: a graceful resume"
    assert "taken_back_reason" not in kwargs, "the worker's own details never reach the run"
