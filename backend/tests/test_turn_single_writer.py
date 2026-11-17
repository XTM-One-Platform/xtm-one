"""One engine-run turn, one writer: a turn another attempt took over stops before its next tool round."""

from __future__ import annotations

import asyncio
import inspect
import logging
import uuid
from types import SimpleNamespace

import pytest

from app.core import turn_writer
from app.core.turn_writer import TurnSuperseded, ensure_single_writer, single_writer
from app.llm.base import BaseLLMProvider, ChatMessage, ChatResponse
from app.services import assignment_engine, orchestration_reconciler, sub_agent
from app.services import orchestration_engine as engine
from app.services.orchestration_adapters.assignment import AssignmentAdapter
from app.services.orchestration_adapters.base import AdapterContext, AdapterResult, OrchestrationAdapter
from app.services.orchestration_adapters.sub_agent import SubAgentAdapter
from app.tools.agent_loop import run_agent_loop
from app.tools.base import ToolCall, ToolDefinition, ToolResult

_TOOL = ToolDefinition(
    name="post_comment",
    description="Post one comment",
    input_schema={"type": "object", "properties": {"n": {"type": "integer"}}},
)


# ── superseded_by: the rule ────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("state", "owner", "count", "expected"),
    [
        pytest.param("running", "w1", 2, None, id="still-ours"),
        pytest.param("running", None, 2, None, id="running-without-owner-is-not-a-take-over"),
        pytest.param("running", "w2", 3, "runs on worker w2", id="claimed-by-another-worker"),
        pytest.param("running", "w1", 3, "attempt 3 of its execution replaced attempt 2", id="re-claimed-by-us-later"),
        pytest.param("pending", None, 2, "taken back", id="taken-back-and-queued"),
        pytest.param(None, None, None, "no longer exists", id="row-gone"),
        pytest.param("completed", None, 2, None, id="terminal-is-not-a-take-over"),
        pytest.param("cancelled", None, 2, None, id="cancel-is-the-watcher's"),
        pytest.param("dead_letter", None, 2, None, id="dead-letter-is-not-a-take-over"),
    ],
)
def test_superseded_by(state, owner, count, expected):
    reason = engine.superseded_by(state=state, lease_owner=owner, attempt_count=count, worker_id="w1", attempt=2)
    if expected is None:
        assert reason is None
    else:
        assert reason is not None and expected in reason


def test_an_unknown_attempt_is_judged_on_the_owner_alone():
    assert engine.superseded_by(state="running", lease_owner="w1", attempt_count=9, worker_id="w1", attempt=None) is None
    assert engine.superseded_by(state="running", lease_owner="w2", attempt_count=9, worker_id="w1", attempt=None)


# ── ensure_single_writer: the check ────────────────────────────────────────


async def test_no_check_bound_is_a_no_op():
    assert not turn_writer.bound()
    await ensure_single_writer()


async def test_a_still_ours_check_lets_the_turn_go_on():
    async def _ours() -> str | None:
        return None

    with single_writer(_ours):
        assert turn_writer.bound()
        await ensure_single_writer()
    assert not turn_writer.bound()


async def test_a_superseded_turn_raises_a_cancellation():
    async def _taken() -> str | None:
        return "its execution runs on worker w2"

    with single_writer(_taken), pytest.raises(TurnSuperseded) as raised:
        await ensure_single_writer()
    assert isinstance(raised.value, asyncio.CancelledError)
    assert "w2" in str(raised.value)


async def test_an_unreadable_check_fails_open(caplog):
    async def _broken() -> str | None:
        raise ConnectionError("database down")

    with single_writer(_broken), caplog.at_level(logging.WARNING):
        await ensure_single_writer()
    assert any("could not be read" in r.getMessage() for r in caplog.records)


# ── The agent loop: checked before every tool round ────────────────────────


class _CommentingLLM(BaseLLMProvider):
    """Asks for one comment per round, ``rounds`` times, then answers."""

    provider_name = "stub"
    context_window = 200_000

    def __init__(self, rounds: int = 3, *, parallel: bool = False) -> None:
        self.rounds = rounds
        self.parallel = parallel
        self.tool_rounds = 0

    async def chat(self, messages, model=None, temperature=0.7, max_tokens=4096, tools=None, **kwargs):
        if not tools or self.tool_rounds >= self.rounds:
            return ChatResponse(content="done", model="stub")
        self.tool_rounds += 1
        n = self.tool_rounds
        calls = [ToolCall(id=f"tc-{n}", name="post_comment", arguments={"n": n})]
        if self.parallel:
            calls.append(ToolCall(id=f"tc-{n}-b", name="post_comment", arguments={"n": n * 100}))
        return ChatResponse(content="", model="stub", tool_calls=calls)

    async def chat_stream(self, messages, model=None, temperature=0.7, max_tokens=4096, tools=None, **kwargs):
        yield "done"

    async def embed(self, text, model=None):
        return [0.0]

    async def list_models(self):
        return []


class _Executor:
    def __init__(self) -> None:
        self.ran: list[int] = []

    async def execute_tool(self, tool_call):
        self.ran.append(tool_call.arguments["n"])
        return ToolResult(tool_call_id=tool_call.id, content=f"posted {tool_call.arguments['n']}")


def _taken_over_after(rounds: int):
    """A check that holds for *rounds* tool rounds, then finds the execution another worker's."""
    asked = {"n": 0}

    async def _check() -> str | None:
        asked["n"] += 1
        return None if asked["n"] <= rounds else "its execution runs on worker w2"

    return _check, asked


@pytest.mark.parametrize("parallel", [False, True], ids=["single-call-rounds", "parallel-rounds"])
async def test_the_loop_runs_no_call_once_its_execution_is_taken_over(parallel):
    llm = _CommentingLLM(rounds=5, parallel=parallel)
    executor = _Executor()
    # The round's check, then one check before each write call of the round (#11802).
    check, asked = _taken_over_after(3 if parallel else 2)
    with single_writer(check), pytest.raises(TurnSuperseded):
        await run_agent_loop(
            llm=llm,
            messages=[ChatMessage(role="user", content="comment")],
            tools=[_TOOL],
            executor=executor,
        )
    # The first round ran (its calls are the turn's), the second was refused before any of its calls.
    assert executor.ran == ([1, 100] if parallel else [1])
    assert asked["n"] == (4 if parallel else 3)


async def test_a_write_of_a_round_taken_over_after_its_check_is_answered_unrun():
    """The round's check passes, the execution is taken over before its second write: that write never runs (#11802)."""
    llm = _CommentingLLM(rounds=5, parallel=True)
    executor = _Executor()
    check, _asked = _taken_over_after(2)  # the round's check and the first write's
    messages = [ChatMessage(role="user", content="comment")]
    with single_writer(check), pytest.raises(TurnSuperseded):
        await run_agent_loop(llm=llm, messages=messages, tools=[_TOOL], executor=executor)
    assert executor.ran == [1]


async def test_a_read_of_a_round_taken_over_runs():
    read_tool = ToolDefinition(
        name="get_comment", description="Read one comment", input_schema={"type": "object"}, read_only=True
    )
    ran: list[str] = []

    class _Reads(_Executor):
        async def execute_tool(self, tool_call):
            ran.append(tool_call.name)
            return ToolResult(tool_call_id=tool_call.id, content="ok")

    class _ReadingLLM(_CommentingLLM):
        async def chat(self, messages, model=None, temperature=0.7, max_tokens=4096, tools=None, **kwargs):
            if not tools or self.tool_rounds >= 1:
                return ChatResponse(content="done", model="stub")
            self.tool_rounds += 1
            return ChatResponse(
                content="", model="stub", tool_calls=[ToolCall(id="r-1", name="get_comment", arguments={})]
            )

    check, asked = _taken_over_after(1)  # the round's check only
    with single_writer(check):
        await run_agent_loop(
            llm=_ReadingLLM(), messages=[ChatMessage(role="user", content="read")], tools=[read_tool], executor=_Reads()
        )
    # A read is never checked call by call: it runs.
    assert ran == ["get_comment"] and asked["n"] == 1


async def test_the_loop_is_unchanged_when_no_check_is_bound():
    llm = _CommentingLLM(rounds=3)
    executor = _Executor()
    response = await run_agent_loop(
        llm=llm,
        messages=[ChatMessage(role="user", content="comment")],
        tools=[_TOOL],
        executor=executor,
    )
    assert response.content == "done"
    assert executor.ran == [1, 2, 3]


async def test_a_turn_with_no_tool_call_is_not_checked():
    check, asked = _taken_over_after(0)
    with single_writer(check):
        response = await run_agent_loop(
            llm=_CommentingLLM(rounds=0),
            messages=[ChatMessage(role="user", content="hello")],
            tools=[_TOOL],
            executor=_Executor(),
        )
    assert response.content == "done"
    assert asked["n"] == 0


# ── The reconciler: binds the check, absorbs the stop, writes nothing ─────


class _FakeDb:
    def __init__(self) -> None:
        self.commits = 0

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        pass


class _FakeSessionCtx:
    def __init__(self, db) -> None:
        self._db = db

    async def __aenter__(self):
        return self._db

    async def __aexit__(self, *_exc):
        return False


def _harness(monkeypatch, adapter: OrchestrationAdapter, *, attempt: int = 2):
    db = _FakeDb()
    monkeypatch.setattr(orchestration_reconciler, "async_session", lambda: _FakeSessionCtx(db))
    execution = SimpleNamespace(state="running", lease_owner="w1", deadline_at=None, payload={}, attempt_count=attempt)

    async def _get_execution(_db, _id):
        return execution

    monkeypatch.setattr(engine, "get_execution", _get_execution)
    writes: list[str] = []

    async def _safe_terminal(_db, _eid, _wid, *, error=None, fail=False):
        writes.append(error or "terminal")

    async def _apply_result(_db, _eid, _wid, result):
        writes.append(result.kind)

    monkeypatch.setattr(orchestration_reconciler, "_safe_terminal", _safe_terminal)
    monkeypatch.setattr(orchestration_reconciler, "_apply_result", _apply_result)
    monkeypatch.setattr(orchestration_reconciler, "get_adapter", lambda _name: adapter)
    return writes


async def test_a_superseded_turn_is_absorbed_with_nothing_written(monkeypatch, caplog):
    class _Superseded(OrchestrationAdapter):
        name = "stub"
        single_writer_turns = True

        async def execute(self, _ctx: AdapterContext, _execution) -> AdapterResult:
            raise TurnSuperseded("its execution runs on worker w2")

    writes = _harness(monkeypatch, _Superseded())
    with caplog.at_level(logging.WARNING):
        await orchestration_reconciler._do_execute(uuid.uuid4(), "stub", "w1", asyncio.Event(), stop_on_lease_loss=False)
    assert writes == []
    assert any("another attempt took the execution over" in r.getMessage() for r in caplog.records)


async def test_the_check_is_bound_only_for_an_adapter_that_asks(monkeypatch):
    seen: dict[str, bool] = {}

    class _Probe(OrchestrationAdapter):
        name = "stub"

        async def execute(self, _ctx: AdapterContext, _execution) -> AdapterResult:
            seen[type(self).__name__] = turn_writer.bound()
            return AdapterResult.completed()

    class _Asks(_Probe):
        single_writer_turns = True

    for adapter in (_Probe(), _Asks()):
        writes = _harness(monkeypatch, adapter)
        await orchestration_reconciler._do_execute(uuid.uuid4(), "stub", "w1", asyncio.Event(), stop_on_lease_loss=False)
        assert writes == ["completed"]
    assert seen == {"_Probe": False, "_Asks": True}
    assert not turn_writer.bound()


async def test_the_bound_check_reads_the_row_against_this_worker_and_attempt(monkeypatch):
    rows = iter(
        [
            SimpleNamespace(state="running", lease_owner="w1", attempt_count=2),
            SimpleNamespace(state="pending", lease_owner=None, attempt_count=2),
        ]
    )

    class _Result:
        def __init__(self, row) -> None:
            self._row = row

        def one_or_none(self):
            return self._row

    class _ReadDb(_FakeDb):
        async def execute(self, _stmt):
            return _Result(next(rows))

    db = _ReadDb()
    monkeypatch.setattr(orchestration_reconciler, "async_session", lambda: _FakeSessionCtx(db))
    adapter = SimpleNamespace(single_writer_turns=True)
    execution = SimpleNamespace(attempt_count=2)
    with orchestration_reconciler._writer_scope(adapter, uuid.uuid4(), "w1", execution):
        await ensure_single_writer()
        with pytest.raises(TurnSuperseded, match="taken back"):
            await ensure_single_writer()


def test_turn_adapters_ask_for_one_writer():
    assert SubAgentAdapter.single_writer_turns is True
    assert AssignmentAdapter.single_writer_turns is True
    assert OrchestrationAdapter.single_writer_turns is False


# ── The turn owners: a superseded attempt writes no ending ─────────────────


def test_an_assignment_attempt_superseded_counts_as_replaced():
    source = inspect.getsource(assignment_engine.execute_assignment_run)
    handler = source.split("except TurnSuperseded:", 1)
    assert len(handler) == 2, "execute_assignment_run must classify a superseded turn"
    body = handler[1].split("except Exception as e:", 1)[0]
    assert "_replaced = True" in body
    assert "raise" in body
    # Before the generic failure handler, which would write a failed ending.
    assert source.index("except TurnSuperseded:") < source.index("except Exception as e:\n            # ── Classify")


async def test_an_approved_call_of_a_taken_over_attempt_is_not_run():
    """The approval-resume helpers run calls outside the loop's dispatch."""
    executor = _Executor()
    check, asked = _taken_over_after(1)
    call = ToolCall(id="a", name="post_comment", arguments={"n": 7})
    with single_writer(check):
        assert await assignment_engine._run_unless_stopped(executor, call, None) == "posted 7"
        with pytest.raises(TurnSuperseded):
            await assignment_engine._run_unless_stopped(executor, call, None)
    assert executor.ran == [7]
    assert asked["n"] == 2


class _PlacedRedis:
    """Redis double running ``assignment_run_resume._WRITE_IF_NOT_LATER_LUA``'s rule."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def set(self, key, value, ex=None):
        self.store[key] = value

    async def eval(self, script, numkeys, key, payload, epoch, attempt, ttl):
        import json

        raw = self.store.get(key)
        doc = json.loads(raw) if raw else {}
        place = doc.get("place")
        if isinstance(place, list) and (place[0], place[1]) > (epoch, attempt):
            return 0
        self.store[key] = payload
        return 1


async def test_a_superseded_task_attempt_never_overwrites_its_successors_checkpoint(monkeypatch):
    """A write in flight when the attempt was superseded lands after its successor's."""
    import json

    from app.services import background_checkpoint
    from app.services.background_checkpoint import TaskCheckpoint

    redis = _PlacedRedis()

    async def _redis():
        return redis

    monkeypatch.setattr(background_checkpoint, "_redis", _redis)
    task_id = uuid.uuid4()
    key = background_checkpoint.checkpoint_key(task_id)
    entry = [{"role": "assistant", "content": "", "tool_calls": []}]

    await TaskCheckpoint(task_id, attempt=3, place=(0, 3))._write(entry)
    await TaskCheckpoint(task_id, attempt=2, place=(0, 2))._write(entry)
    assert json.loads(redis.store[key])["place"] == [0, 3]

    # An attempt not run by the engine keeps the plain write.
    await TaskCheckpoint(task_id)._write(entry)
    assert json.loads(redis.store[key])["place"] is None


def test_a_background_task_superseded_stops_its_checkpoint_without_a_last_snapshot():
    source = inspect.getsource(sub_agent._execute_sub_agent_task)
    handler = source.split("except TurnSuperseded:", 1)
    assert len(handler) == 2, "_execute_sub_agent_task must classify a superseded turn"
    body = handler[1].split("turn_cut =", 1)[0]
    assert "aclose(flush=False)" in body
    assert "raise" in body
