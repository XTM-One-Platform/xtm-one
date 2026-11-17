"""A background task the platform ends as failed leaves an error trace, with what its turn did."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import attempt_charges, sub_agent
from app.services import langfuse_tracing as lft
from app.services.orchestration_adapters.sub_agent import SubAgentAdapter
from app.services.orchestration_engine import LEASE_EXPIRY_PAYLOAD_KEY
from app.services.tracing import background_run, dispatcher


def _task(**overrides):
    values = {
        "id": uuid.uuid4(),
        "user_id": uuid.uuid4(),
        "parent_agent_id": uuid.uuid4(),
        "target_agent_id": uuid.uuid4(),
        "conversation_id": None,
        "task_description": "do the thing",
        "status": "running",
        "error": None,
        "completed_at": None,
        "started_at": datetime(2026, 10, 30, 18, 33, tzinfo=UTC),
        "created_at": datetime(2026, 10, 30, 18, 32, tzinfo=UTC),
        "metadata_json": {},
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class _FakeRedis:
    """The few Redis calls a checkpoint makes, in memory."""

    def __init__(self):
        self.values: dict[str, str] = {}
        self.lists: dict[str, list[str]] = {}

    async def set(self, key, value, ex=None):
        self.values[key] = value

    async def get(self, key):
        return self.values.get(key)

    async def delete(self, key):
        self.values.pop(key, None)
        self.lists.pop(key, None)

    async def rpush(self, key, value):
        self.lists.setdefault(key, []).append(value)

    async def ltrim(self, key, start, end):
        items = self.lists.get(key, [])
        self.lists[key] = items[start:] if end == -1 else items[start : end + 1]

    async def expire(self, key, seconds):
        return True

    async def lrange(self, key, start, end):
        items = self.lists.get(key, [])
        return items[start:] if end == -1 else items[start : end + 1]


@pytest.fixture(autouse=True)
def redis(monkeypatch):
    fake = _FakeRedis()

    async def _redis():
        return fake

    monkeypatch.setattr(background_run, "_redis", _redis)
    return fake


@pytest.fixture
def flushed(monkeypatch):
    calls = []
    monkeypatch.setattr(dispatcher, "submit_trace_for_flush", lambda trace, spans: calls.append((trace, spans)))
    return calls


async def _settle():
    """Let the queued checkpoint writes and lost-run closes run."""
    while background_run._PENDING:
        await asyncio.gather(*list(background_run._PENDING), return_exceptions=True)


async def test_lost_run_trace_names_the_task_and_the_cause(flushed):
    task = _task(metadata_json={"assignment_run_id": str(uuid.uuid4())})
    heard = datetime(2026, 10, 30, 18, 40, tzinfo=UTC)
    now = datetime(2026, 10, 30, 18, 47, tzinfo=UTC)

    trace_id = await background_run.close_lost_background_run(
        task, reason="lease expired", last_heartbeat_at=heard.isoformat(), attempt=3, now=now
    )

    assert len(flushed) == 1
    trace, spans = flushed[0]
    assert spans == []
    assert trace.id == trace_id == background_run.background_run_trace_id(task.id)
    assert str(task.id) in trace.name
    assert trace.status == "error"
    assert trace.level == "ERROR"
    assert trace.error_message == "lease expired"
    assert trace.failure_cause == "engine_abandoned"
    assert trace.agent_id == task.target_agent_id
    assert str(trace.assignment_run_id) == task.metadata_json["assignment_run_id"]
    assert trace.started_at == task.started_at
    assert trace.ended_at == now
    assert trace.metadata["task_id"] == str(task.id)
    assert trace.metadata["last_heartbeat_at"] == heard.isoformat()
    assert trace.metadata["attempt"] == 3


async def test_a_lost_resumed_attempt_keeps_its_resumed_from_link(flushed):
    """The trace closing a resumed attempt lost again is found by a search on resumed_from."""
    from app.services.background_checkpoint import record_resume

    run_id = str(uuid.uuid4())
    metadata = record_resume(
        {"assignment_run_id": run_id}, attempt=2, cause="lost", replayed_calls=2, resumed_from="attempt-1"
    )
    task = _task(metadata_json=metadata)

    await background_run.close_lost_background_run(task, reason="lease expired", attempt=2)

    trace, _ = flushed[0]
    link = trace.metadata["resumed_from"]
    assert link["task_id"] == str(task.id) and link["run_id"] == run_id
    assert link["attempt"] == "attempt-1" and link["resume"] == 1
    assert trace.metadata["kind"] == background_run.LOST_RUN_TRACE_KIND


async def test_a_lost_first_attempt_has_no_resumed_from(flushed):
    await background_run.close_lost_background_run(_task(), reason="lease expired")
    trace, _ = flushed[0]
    assert "resumed_from" not in trace.metadata


def test_lost_run_trace_id_is_stable_across_passes():
    task_id = uuid.uuid4()
    assert background_run.background_run_trace_id(task_id) == background_run.background_run_trace_id(str(task_id))
    assert background_run.background_run_trace_id(task_id) != background_run.background_run_trace_id(uuid.uuid4())


async def test_tracing_failure_never_raises(monkeypatch):
    def boom(trace, spans):
        raise RuntimeError("sink down")

    monkeypatch.setattr(dispatcher, "submit_trace_for_flush", boom)
    assert await background_run.close_lost_background_run(_task(), reason="worker lost") is None
    assert background_run.record_lost_background_run(_task(), reason="worker lost") is True
    await _settle()


async def test_unreadable_checkpoint_still_records_the_lost_run_trace(flushed, monkeypatch):
    async def _down():
        raise ConnectionError("redis down")

    monkeypatch.setattr(background_run, "_redis", _down)
    task = _task()

    trace_id = await background_run.close_lost_background_run(task, reason="worker lost")

    assert trace_id == background_run.background_run_trace_id(task.id)
    assert flushed[0][1] == []


@pytest.mark.asyncio
async def test_orphan_sweep_failure_records_the_lost_run_trace(flushed, monkeypatch):
    now = datetime.now(UTC)
    record = {
        "fail_after": (now - timedelta(minutes=1)).isoformat(),
        "attempt": 2,
        "last_heartbeat_at": (now - timedelta(minutes=20)).isoformat(),
        "silent_seconds": 1200,
    }
    task = _task(metadata_json={sub_agent.ORPHANED_KEY: record})

    async def _no_delegation(*args, **kwargs):
        return None

    monkeypatch.setattr(sub_agent, "_update_task_delegation", _no_delegation)

    await sub_agent._fail_orphaned_task(SimpleNamespace(), task, error="abandoned", now=now)
    await _settle()

    assert task.status == "failed"
    assert len(flushed) == 1
    trace, _ = flushed[0]
    assert trace.id == background_run.background_run_trace_id(task.id)
    assert trace.error_message == "abandoned"
    assert trace.metadata["last_heartbeat_at"] == record["last_heartbeat_at"]
    assert trace.metadata["attempt"] == 2


def _session(task):
    return SimpleNamespace(
        execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: task)),
        commit=AsyncMock(),
    )


@pytest.fixture
def quiet_dead_letter(monkeypatch):
    monkeypatch.setattr(attempt_charges, "credit_abandoned_task", AsyncMock(return_value=0))
    monkeypatch.setattr(sub_agent, "_update_task_delegation", AsyncMock())
    monkeypatch.setattr(sub_agent, "_schedule_parent_wake_if_needed", AsyncMock())


async def test_dead_letter_of_a_lost_task_records_the_lost_run_trace(flushed, quiet_dead_letter):
    task = _task(status="pending")
    execution = SimpleNamespace(
        payload={"task_id": str(task.id)}, last_error="Deadline exceeded.", attempt_count=1, deadline_at=None
    )

    await SubAgentAdapter().on_dead_letter(SimpleNamespace(db=_session(task)), execution)
    await _settle()

    assert task.status == "failed"
    assert len(flushed) == 1
    trace, _ = flushed[0]
    assert trace.id == background_run.background_run_trace_id(task.id)
    assert trace.status == "error"
    assert trace.error_message == "Deadline exceeded."
    assert trace.metadata["attempt"] == 1


async def test_dead_letter_of_an_orphaned_running_task_records_nothing_yet(flushed, quiet_dead_letter):
    """Its worker may still end the turn and write its own trace."""
    task = _task(status="running")
    now = datetime.now(UTC)
    execution = SimpleNamespace(
        payload={
            "task_id": str(task.id),
            LEASE_EXPIRY_PAYLOAD_KEY: {
                "at": now.isoformat(),
                "last_heartbeat_at": (now - timedelta(seconds=61)).isoformat(),
                "silent_seconds": 61,
                "owner": "worker-a",
                "attempt": 3,
            },
        },
        last_error="Lease expired.",
        attempt_count=3,
        deadline_at=now + timedelta(minutes=10),
    )

    await SubAgentAdapter().on_dead_letter(SimpleNamespace(db=_session(task)), execution)
    await _settle()

    assert task.status == "running"
    assert flushed == []


@pytest.mark.parametrize("status", ["completed", "failed", "incomplete"])
async def test_dead_letter_of_an_ended_task_keeps_its_own_trace(flushed, quiet_dead_letter, status):
    """A task that ended through its turn already traced itself: nothing is added."""
    task = _task(status=status)
    execution = SimpleNamespace(
        payload={"task_id": str(task.id)}, last_error="Deadline exceeded.", attempt_count=1, deadline_at=None
    )

    await SubAgentAdapter().on_dead_letter(SimpleNamespace(db=_session(task)), execution)
    await _settle()

    assert task.status == status
    assert flushed == []


# -- The turn's own trace, checkpointed from its open ---------------


@pytest.fixture
def local_only_tracing(monkeypatch):
    async def _no_client(_db):
        return None

    monkeypatch.setattr(lft, "get_langfuse_client", _no_client)
    monkeypatch.setattr(lft, "_resolve_user_group_ids", AsyncMock(return_value=[]))


async def _turn_with_two_tool_calls(task_id):
    """Open a turn's trace under the task's recorder and end two tool calls in it."""
    with background_run.recording_spans(task_id):
        ctx = await lft.trace_llm_call(SimpleNamespace(), name="agent-chat:worker", user_id="u", source="worker")
        for index, tool in enumerate(("search_knowledge", "get_github_file"), start=1):
            span = lft.start_tool_span(tool_name=tool, tool_call_id=f"call-{index}", arguments={"n": index}, parent_ctx=ctx)
            lft.end_tool_span(span, output=f"result {index}")
        # A trace a consult opens inside the turn is its own, not the turn's.
        helper = await lft.trace_llm_call(
            SimpleNamespace(), name="agent-chat:helper", user_id="u", source="helper", parent_trace_ctx=ctx
        )
        lft.end_tool_span(lft.start_tool_span(tool_name="helper_tool", parent_ctx=helper), output="x")
    await _settle()
    return ctx


async def test_a_run_killed_after_two_tool_calls_has_one_trace_with_its_two_spans(flushed, local_only_tracing):
    task = _task()
    ctx = await _turn_with_two_tool_calls(task.id)
    assert flushed == []  # the trace store is written at the end of a turn, not before

    # The worker is killed here; the orphan sweep (or the dead letter) fails the task.
    assert background_run.record_lost_background_run(task, reason="lease lost: no heartbeat for 20 min") is True
    await _settle()
    # A second pass writes the same trace.
    await background_run.close_lost_background_run(task, reason="lease lost: no heartbeat for 20 min")

    assert len(flushed) == 2
    trace, spans = flushed[0]
    assert trace.id == flushed[1][0].id == ctx["local_trace_id"]
    assert str(task.id) in trace.name
    assert trace.status == "error"
    assert trace.error_message == "lease lost: no heartbeat for 20 min"
    assert trace.failure_cause == "engine_abandoned"
    assert trace.metadata["turn_trace_name"] == "agent-chat:worker"
    assert [span.name for span in spans] == ["tool:search_knowledge", "tool:get_github_file"]
    assert all(span.trace_id == trace.id and span.type == "TOOL" for span in spans)
    assert [span.tool_call_id for span in spans] == ["call-1", "call-2"]
    assert spans[0].output == "result 1"


async def test_a_completed_run_keeps_its_trace_unchanged(flushed, local_only_tracing):
    task = _task()
    with background_run.recording_spans(task.id):
        ctx = await lft.trace_llm_call(SimpleNamespace(), name="agent-chat:worker", user_id="u", source="worker")
        for tool in ("search_knowledge", "get_github_file"):
            lft.end_tool_span(lft.start_tool_span(tool_name=tool, parent_ctx=ctx), output="ok")
        lft.end_trace(ctx, output="done")
    await _settle()
    await asyncio.sleep(0)

    assert len(flushed) == 1
    trace, spans = flushed[0]
    assert trace.id == ctx["local_trace_id"]
    assert trace.name == "agent-chat:worker"
    assert trace.status == "ok"
    assert trace.error_message is None
    assert [span.name for span in spans] == ["tool:search_knowledge", "tool:get_github_file"]


async def test_a_turn_outside_a_background_task_checkpoints_nothing(flushed, local_only_tracing, redis):
    ctx = await lft.trace_llm_call(SimpleNamespace(), name="agent-chat:chat", user_id="u", source="chat")
    lft.end_tool_span(lft.start_tool_span(tool_name="search_knowledge", parent_ctx=ctx), output="ok")
    await _settle()

    assert redis.values == {}
    assert redis.lists == {}


async def test_a_rerun_of_the_task_starts_a_fresh_checkpoint(flushed, local_only_tracing):
    task = _task()
    await _turn_with_two_tool_calls(task.id)
    with background_run.recording_spans(task.id):
        second = await lft.trace_llm_call(SimpleNamespace(), name="agent-chat:worker", user_id="u", source="worker")
    await _settle()

    await background_run.close_lost_background_run(task, reason="worker lost")

    trace, spans = flushed[0]
    assert trace.id == second["local_trace_id"]
    assert spans == []
