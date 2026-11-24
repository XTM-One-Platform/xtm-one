"""``run_agent_chat`` in evaluation mode, with the loop, Redis and the database stubbed.

The skip_llm fast path and the execution count of an evaluation entry, the error exits
(context overflow, HTTP 413), the run ledger handed back through ``loop_outcome_out``
(tool steps, consulted and transferred agents, parks and waits, timeouts), the turn
budget, the prompt guard, a system prompt carrying no one's profile, memory and
scheduling, tool approval, and the time to first token on Full Loop.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest

import app.services.agent_chat as agent_chat_mod
from app.llm.base import ChatMessage
from app.tools.base import ToolDefinition, ToolResult

# Shared with main's own tests, which keep these helpers in test_agent_chat_smoke.py.
from tests.test_agent_chat_smoke import USER_ID, _agent, _FakeRedis, _pipeline_exec, _traced_agent


class _ExpireFailingRedis(_FakeRedis):
    async def expire(self, key, ttl):
        self.calls.append(("expire", key, ttl))
        raise ConnectionError("redis dropped after the incr")


@pytest.mark.asyncio
async def test_skip_llm_fast_path_still_decrements_when_expire_failed():
    """Outside evaluation this path decrements unconditionally.

    Keying the decrement on "tracking fully succeeded" would change chat only
    to keep evaluation mode off the counter. Either way the key
    outlives this turn when ``expire`` failed -- ``/runs/active-agents`` reads
    key names, not values -- so this pins only that the decrement runs.
    """
    fake_redis = _ExpireFailingRedis()

    with (
        patch("app.services.quota.check_agentic_quota", new=AsyncMock()),
        patch("app.services.quota.increment_agentic_usage", new=AsyncMock()),
        patch(
            "app.core.redis.redis_client",
            new=AsyncMock(return_value=fake_redis),
        ),
    ):
        await agent_chat_mod.run_agent_chat(
            db=object(),  # type: ignore[arg-type]
            user_id=USER_ID,
            agent=_agent(skip_llm=True),  # type: ignore[arg-type]
            history=[],
            user_message="echo this back",
        )

    ops = [c[0] for c in fake_redis.calls]
    assert ops.count("incr") == 1
    assert ops.count("decr") == 1


@pytest.mark.asyncio
async def test_skip_llm_fast_path_runs_in_evaluation_mode_without_llm():
    """In evaluation mode a model-independent (``skip_llm``) agent still runs
    its deterministic fast path and returns output WITHOUT invoking any chat
    model, and WITHOUT touching the activity counter (evaluation runs
    are not user chat activity — the counter is never incremented, so it must
    never be decremented either)."""
    fake_redis = _FakeRedis()
    check = AsyncMock()
    increment = AsyncMock()
    get_llm = AsyncMock()

    with (
        patch("app.services.quota.check_agentic_quota", new=check),
        patch("app.services.quota.increment_agentic_usage", new=increment),
        patch(
            "app.core.redis.redis_client",
            new=AsyncMock(return_value=fake_redis),
        ),
        patch("app.llm.router.get_llm_for_user", new=get_llm),
    ):
        text, new_agent_id = await agent_chat_mod.run_agent_chat(
            db=object(),  # type: ignore[arg-type]
            user_id=USER_ID,
            agent=_agent(skip_llm=True),  # type: ignore[arg-type]
            history=[],
            user_message="deterministic output",
            evaluation_mode=True,
        )

    assert text == "deterministic output"
    assert new_agent_id is None
    # Evaluation runs are not metered and the LLM is never consulted.
    check.assert_not_awaited()
    increment.assert_not_awaited()
    get_llm.assert_not_awaited()
    # Activity counter must be untouched: no incr on entry (evaluation_mode),
    # therefore no decr on exit (would otherwise drive the counter negative).
    ops = [c[0] for c in fake_redis.calls]
    assert "incr" not in ops
    assert "decr" not in ops


@pytest.mark.asyncio
async def test_skip_llm_eval_mode_populates_pipeline_traces():
    """In evaluation mode, the ``skip_llm`` fast path must record its PREPARE
    and FINALIZE pipeline tool calls into ``eval_result_out``.

    The fast path returns long before the LLM-loop eval-tracing hook, so
    without this the evaluation engine sees an empty timeline: no execution
    traces, ``agent_duration_ms == 0``, and ``actual_tools == []`` — which
    silently breaks ``tool_sequence`` scoring and leaves the run detail UI
    without the deterministic pipeline steps."""
    fake_redis = _FakeRedis()

    prep_defs = [
        ToolDefinition(
            name="prep_tool",
            description="",
            input_schema={"type": "object", "properties": {"input": {"type": "string"}}},
        )
    ]
    fin_defs = [
        ToolDefinition(
            name="fin_tool",
            description="",
            input_schema={"type": "object", "properties": {"response": {"type": "string"}}},
        )
    ]
    prep_exec = SimpleNamespace(
        execute_tool=AsyncMock(
            return_value=ToolResult(tool_call_id="p", content="prepared")
        )
    )
    fin_exec = SimpleNamespace(
        execute_tool=AsyncMock(
            return_value=ToolResult(tool_call_id="f", content="finalized")
        )
    )

    async def _fake_load(
        db, ref, user_id, agent, headers, element_scope=None, company_automation=None, owner_run=False
    ):
        if ref.get("tool_name") == "prep_tool":
            return prep_defs, prep_exec
        return fin_defs, fin_exec

    eval_out: dict = {}

    with (
        patch.object(
            agent_chat_mod, "_load_pipeline_tool_standalone", new=_fake_load
        ),
        patch(
            "app.core.redis.redis_client",
            new=AsyncMock(return_value=fake_redis),
        ),
        patch("app.llm.router.get_llm_for_user", new=AsyncMock()),
    ):
        text, new_agent_id = await agent_chat_mod.run_agent_chat(
            db=object(),  # type: ignore[arg-type]
            user_id=USER_ID,
            agent=_agent(
                skip_llm=True,
                prepare_tool_ref={"tool_name": "prep_tool"},
                finalize_tool_ref={"tool_name": "fin_tool"},
            ),  # type: ignore[arg-type]
            history=[],
            user_message="raw input",
            evaluation_mode=True,
            eval_result_out=eval_out,
        )

    assert text == "finalized"
    assert new_agent_id is None
    # eval_result_out carries a deterministic-run shape (no LLM trace, no answer start).
    assert eval_out["llm_trace_id"] is None
    assert eval_out["answer_started_at"] is None
    assert "ttft_ms" not in eval_out
    # Both pipeline stages are recorded as tool traces, in order, with phases.
    tool_traces = [t for t in eval_out["traces"] if t["type"] == "tool"]
    assert [t["tool_name"] for t in tool_traces] == ["prep_tool", "fin_tool"]
    assert [t["phase"] for t in tool_traces] == ["prepare", "finalize"]
    # tool_names powers structured_output tool_sequence scoring (actual_tools).
    assert eval_out["tool_names"] == ["prep_tool", "fin_tool"]
    # Inputs/outputs are captured so the run timeline is inspectable.
    assert tool_traces[0]["tool_input"] == "raw input"
    assert tool_traces[0]["tool_output"] == "prepared"
    assert tool_traces[1]["tool_input"] == "prepared"
    assert tool_traces[1]["tool_output"] == "finalized"
    # Deterministic runs contribute real durations to the timeline.
    assert all(t["duration_ms"] >= 0 for t in tool_traces)


@pytest.mark.asyncio
@pytest.mark.parametrize("evaluation_mode", [False, True], ids=["chat", "evaluation"])
async def test_a_prepare_refusal_reverses_the_execution_count_only_when_it_was_recorded(evaluation_mode):
    """The count is shared by the whole platform for the day.

    Outside evaluation the run was counted on the way in, so a refusal that
    did no work takes it off. An evaluation entry is never counted: taking one
    off there removed a real execution another user made.
    """
    record = AsyncMock()
    reverse = AsyncMock()
    err_json = json.dumps({"error": "No enterprise licence configured."})
    load_mock = AsyncMock(return_value=([], _pipeline_exec(ToolResult("", err_json, is_error=True))))

    with (
        patch("app.services.quota.check_agentic_quota", new=AsyncMock()),
        patch("app.services.quota.increment_agentic_usage", new=AsyncMock()),
        patch("app.services.quota.refund_agentic_usage", new=AsyncMock()),
        patch("app.core.redis.redis_client", new=AsyncMock(return_value=_FakeRedis())),
        patch.object(agent_chat_mod, "_load_pipeline_tool_standalone", new=load_mock),
        patch.object(agent_chat_mod, "record_agentic_execution", new=record),
        patch.object(agent_chat_mod, "refund_agentic_execution", new=reverse),
    ):
        text, _ = await agent_chat_mod.run_agent_chat(
            db=object(),  # type: ignore[arg-type]
            user_id=USER_ID,
            agent=_agent(skip_llm=True, prepare_tool_ref={"tool_name": "extract_stix"}),  # type: ignore[arg-type]
            history=[],
            user_message="extract",
            evaluation_mode=evaluation_mode,
        )

    assert text == err_json
    assert record.await_count == reverse.await_count == (0 if evaluation_mode else 1)


class _EmptyResult:
    """Every query in the setup window finds nothing."""

    def scalars(self):
        return self

    def all(self):
        return []

    def first(self):
        return None

    def scalar_one_or_none(self):
        return None

    def scalar(self):
        return None


class _EmptyDB:
    async def execute(self, *_args, **_kwargs):
        return _EmptyResult()

    async def get(self, *_args, **_kwargs):
        return None

    async def commit(self):
        return None

    async def rollback(self):
        return None

    async def flush(self):
        return None

    def add(self, *_args):
        return None

    def begin_nested(self):
        class _Savepoint:
            async def __aenter__(self):
                return None

            async def __aexit__(self, *_exc):
                return False

        return _Savepoint()


def _loop_agent() -> SimpleNamespace:
    """Agent stub reaching all the way to ``run_agent_loop``."""
    return _traced_agent(
        persona=None,
        output_schema=None,
        output_format=None,
        guardrails=None,
        prompt_ids=[],
        skill_ids=[],
        channel_config=None,
        integration_bindings=[],
        handover_agent_ids=[],
        handover_types=None,
        writable_knowledge_base_ids=[],
        builtin_tool_ids=None,
        integration_template_bindings=None,
        custom_tool_ids=None,
        mcp_allowed_tools=None,
        tool_deny_list=None,
        max_iterations=None,
        reasoning_effort=None,
        config=None,
        temperature=None,
        budget_config=None,
    )


async def _run_eval_turn(loop, agent: SimpleNamespace | None = None) -> tuple[str, dict]:
    eval_out: dict = {}
    local_trace_id = uuid.uuid4()
    with (
        patch("app.services.quota.check_agentic_quota", new=AsyncMock()),
        patch("app.services.quota.increment_agentic_usage", new=AsyncMock()),
        patch("app.core.redis.redis_client", new=AsyncMock(return_value=_FakeRedis())),
        patch(
            "app.services.langfuse_tracing.trace_llm_call",
            new=AsyncMock(return_value={"trace": None, "local_spans": []}),
        ),
        patch("app.services.langfuse_tracing.end_trace", new=MagicMock()),
        patch(
            "app.services.langfuse_tracing.get_local_trace_id",
            new=MagicMock(return_value=local_trace_id),
        ),
        patch.object(agent_chat_mod, "run_agent_loop", new=loop),
    ):
        text, _ = await agent_chat_mod.run_agent_chat(
            db=_EmptyDB(),  # type: ignore[arg-type]
            user_id="anon-participant",
            agent=agent or _loop_agent(),  # type: ignore[arg-type]
            history=[],
            user_message="hello",
            conversation_id="conv-1",
            prebuilt_llm=(SimpleNamespace(provider_name="fake"), "fake-model"),
            evaluation_mode=True,
            eval_result_out=eval_out,
        )
    assert eval_out.get("llm_trace_id") == str(local_trace_id)
    return text, eval_out


async def _one_tool_step(on_event) -> None:
    await on_event({"status": "thinking", "iteration": 1})
    await on_event(
        {
            "status": "tool_start",
            "tools": ["lookup"],
            "llm_model": "fake-model",
            "llm_usage": {"input_tokens": 120, "output_tokens": 8},
        }
    )
    await asyncio.sleep(0.01)
    await on_event({"status": "tool_done", "tool": "lookup", "success": True})


@pytest.mark.asyncio
async def test_each_tool_step_records_the_time_the_loop_measured_for_it():
    """One ``tool_start`` per batch timed only its first done: the others read 0 ms.

    The loop sends each tool's time and the group of the tools that ran
    together. An event without a time is still timed from ``tool_start``.
    """
    from app.llm.base import ChatResponse

    async def _loop(**kwargs):
        # The evaluation asks the loop for each tool's time.
        assert kwargs["report_tool_durations"] is True
        on_event = kwargs["on_event"]
        await on_event({"status": "thinking", "iteration": 1})
        await on_event({"status": "tool_start", "tools": ["a", "b", "c"]})
        await on_event({"status": "tool_done", "tool": "a", "success": True, "duration_ms": 30, "parallel_group": "g1"})
        await on_event({"status": "tool_done", "tool": "b", "success": True, "duration_ms": 80, "parallel_group": "g1"})
        await on_event({"status": "tool_done", "tool": "c", "success": True, "duration_ms": 20})
        await on_event({"status": "thinking", "iteration": 2})
        await on_event({"status": "tool_start", "tools": ["d"]})
        await asyncio.sleep(0.01)
        await on_event({"status": "tool_done", "tool": "d", "success": True})
        return ChatResponse(content="the answer", model="fake-model", stop_reason="end_turn")

    _text, eval_out = await _run_eval_turn(_loop)

    tools = [t for t in eval_out["traces"] if t["type"] == "tool"]
    assert [t["tool_name"] for t in tools] == ["a", "b", "c", "d"]
    assert [t["duration_ms"] for t in tools[:3]] == [30, 80, 20]
    # A 10 ms sleep, read in whole milliseconds: a margin for a sleep that wakes early.
    assert tools[3]["duration_ms"] >= 5
    assert [t.get("parallel_group") for t in tools] == ["g1", "g1", None, None]


@pytest.mark.asyncio
async def test_a_context_overflow_mid_run_still_hands_its_steps_to_the_evaluation():
    """The calls made before an overflow were real and billed.

    The overflow exit publishes the timeline with its friendly text; without it
    the engine sees no trace link, no tokens or cost, and a 0 step total.

    The stub raises where the loop's own guard does: after pruning, BEFORE the
    iteration's ``thinking`` event, so no model call is in flight.
    """
    from app.llm.base import ContextOverflowError

    async def _loop(**kwargs):
        await _one_tool_step(kwargs["on_event"])
        raise ContextOverflowError(estimated_tokens=9000, context_window=4096, model="fake-model")

    text, eval_out = await _run_eval_turn(_loop)

    assert "too large" in text
    kinds = [(t["type"], t["outcome"]) for t in eval_out["traces"]]
    # The failure is recorded, so the entry does not read as a clean run.
    assert kinds == [("llm", "success"), ("tool", "success"), ("error", "error")]
    assert eval_out["traces"][0]["usage"] == {"input_tokens": 120, "output_tokens": 8}
    assert eval_out["traces"][1]["duration_ms"] >= 10
    assert eval_out["traces"][2]["error_type"] == "context_overflow"
    assert eval_out["traces"][2]["duration_ms"] == 0


@pytest.mark.asyncio
async def test_a_provider_pre_flight_overflow_records_no_model_call():
    """The provider's check raises after ``thinking`` but before any request.

    No call reached the provider, so none is recorded: a failed ``llm`` step
    would count a call that never happened in model calls and reliability.
    """
    from app.llm.base import ContextOverflowError

    async def _loop(**kwargs):
        await kwargs["on_event"]({"status": "thinking", "iteration": 1})
        raise ContextOverflowError(estimated_tokens=9000, context_window=4096, model="fake-model")

    _text, eval_out = await _run_eval_turn(_loop)

    [only] = eval_out["traces"]
    assert only["type"] == "error"
    assert only["error_type"] == "context_overflow"


@pytest.mark.asyncio
async def test_a_413_is_recorded_as_the_failed_call_it_was():
    """The provider answered 413: a real call, refused, and not a success."""
    from app.llm.router import REQUEST_TOO_LARGE_STREAMING_MESSAGE

    async def _loop(**kwargs):
        await _one_tool_step(kwargs["on_event"])
        await kwargs["on_event"]({"status": "thinking", "iteration": 2})
        raise RuntimeError("Error code: 413 - request entity too large")

    text, eval_out = await _run_eval_turn(_loop)

    assert text == REQUEST_TOO_LARGE_STREAMING_MESSAGE
    kinds = [(t["type"], t["outcome"]) for t in eval_out["traces"]]
    assert kinds == [("llm", "success"), ("tool", "success"), ("llm", "error")]
    failed = eval_out["traces"][2]
    assert failed["error_type"] == "request_too_large"
    # The friendly text is not what the model answered.
    assert failed["raw_response"] == ""


@pytest.mark.asyncio
async def test_a_completed_evaluation_turn_still_publishes_its_answer_as_a_success():
    """The normal exit goes through the same publisher as the error exits."""
    from app.llm.base import ChatResponse

    async def _loop(**kwargs):
        await kwargs["on_event"]({"status": "thinking", "iteration": 1})
        return ChatResponse(content="the answer", model="fake-model", stop_reason="end_turn")

    text, eval_out = await _run_eval_turn(_loop)

    assert text == "the answer"
    [only] = eval_out["traces"]
    assert only["outcome"] == "success"
    assert "error_type" not in only
    assert only["raw_response"] == "the answer"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("output_format", "delivered"),
    [("json", {"indicators": ["evil.com"]}), ("markdown", None)],
    ids=["json-producer", "markdown"],
)
async def test_a_json_producer_delivers_bare_json(output_format, delivered):
    """The chat turn half of the one delivery rule LLM Only shares: an agent
    declared as a JSON producer delivers bare JSON, any other keeps its answer."""
    from app.llm.base import ChatResponse

    fenced = '```json\n{"indicators": ["evil.com"]}\n```'

    async def _loop(**kwargs):
        await kwargs["on_event"]({"status": "thinking", "iteration": 1})
        return ChatResponse(content=fenced, model="fake-model", stop_reason="end_turn")

    agent = _loop_agent()
    agent.output_format = output_format
    text, _eval_out = await _run_eval_turn(_loop, agent)

    if delivered is None:
        assert text == fenced
    else:
        assert json.loads(text) == delivered


# --- The run ledger handed back through loop_outcome_out -------------


def _generation_span(output_tokens: int) -> dict:
    return {
        "type": "GENERATION",
        "name": "llm_call",
        "model": "fake-model",
        "duration_ms": 12.0,
        "level": "DEFAULT",
        "usage_details": {"input": 100, "output": output_tokens},
        "metadata": {},
    }


async def _run_turn_with_outcome(loop, *, loop_outcome_out: dict | None, trace_ctx: dict | None, **run_kwargs):
    captured: dict = {}

    async def _capturing_loop(**kwargs):
        captured.update(kwargs)
        return await loop(**kwargs)

    with (
        patch("app.services.quota.check_agentic_quota", new=AsyncMock()),
        patch("app.services.quota.increment_agentic_usage", new=AsyncMock()),
        patch("app.core.redis.redis_client", new=AsyncMock(return_value=_FakeRedis())),
        patch("app.services.langfuse_tracing.trace_llm_call", new=AsyncMock(return_value=trace_ctx)),
        patch("app.services.langfuse_tracing.end_trace", new=MagicMock()),
        patch.object(agent_chat_mod, "run_agent_loop", new=_capturing_loop),
    ):
        text, _ = await agent_chat_mod.run_agent_chat(
            db=_EmptyDB(),  # type: ignore[arg-type]
            user_id="anon-participant",
            agent=_loop_agent(),  # type: ignore[arg-type]
            history=[],
            user_message="hello",
            conversation_id="conv-1",
            prebuilt_llm=(SimpleNamespace(provider_name="fake"), "fake-model"),
            loop_outcome_out=loop_outcome_out,
            **run_kwargs,
        )
    return text, captured


@pytest.mark.asyncio
async def test_a_turn_outside_evaluation_does_not_ask_for_tool_times():
    """Only the evaluation asks the loop for each tool's time: other runs' events stay as they were."""
    from app.llm.base import ChatResponse

    async def _loop(**_kwargs):
        return ChatResponse(content="the answer", model="fake-model", stop_reason="end_turn")

    _text, captured = await _run_turn_with_outcome(
        _loop, loop_outcome_out=None, trace_ctx={"trace": None, "local_spans": []}
    )

    assert "report_tool_durations" not in captured


@pytest.mark.asyncio
async def test_a_turn_hands_back_the_calls_its_trace_recorded():
    from app.llm.base import ChatResponse

    trace_ctx: dict = {"trace": None, "local_spans": []}

    async def _loop(**_kwargs):
        trace_ctx["local_spans"].append(_generation_span(40))
        return ChatResponse(content="the answer", model="fake-model", stop_reason="end_turn")

    outcome: dict = {}
    await _run_turn_with_outcome(_loop, loop_outcome_out=outcome, trace_ctx=trace_ctx)

    [call] = outcome["ledger"]["calls"]
    assert (call["kind"], call["model"], call["usage"]["output"]) == ("model", "fake-model", 40)


@pytest.mark.asyncio
async def test_a_turn_that_raises_still_hands_back_the_calls_it_made():
    """A helper that fails part-way made its calls; its caller counts them."""
    trace_ctx: dict = {"trace": None, "local_spans": []}

    async def _loop(**_kwargs):
        trace_ctx["local_spans"].append(_generation_span(7))
        raise RuntimeError("the helper failed")

    outcome: dict = {}
    with pytest.raises(RuntimeError, match="the helper failed"):
        await _run_turn_with_outcome(_loop, loop_outcome_out=outcome, trace_ctx=trace_ctx)

    assert [c["usage"]["output"] for c in outcome["ledger"]["calls"]] == [7]


@pytest.mark.asyncio
async def test_passing_loop_outcome_out_changes_nothing_the_turn_does():
    """The ledger is an output only; the loop is driven identically."""
    from app.llm.base import ChatResponse

    async def _loop(**_kwargs):
        return ChatResponse(content="the answer", model="fake-model", stop_reason="end_turn")

    with_text, with_kwargs = await _run_turn_with_outcome(
        _loop, loop_outcome_out={}, trace_ctx={"trace": None, "local_spans": []}
    )
    without_text, without_kwargs = await _run_turn_with_outcome(
        _loop, loop_outcome_out=None, trace_ctx={"trace": None, "local_spans": []}
    )

    assert with_text == without_text
    assert with_kwargs.keys() == without_kwargs.keys()
    # Built fresh in each run: ``wall_clock_deadline`` is "now + timeout", and
    # the system prompt's text carries the clock and cached platform state, both
    # read before the outcome dict plays any part. The rest of the history is
    # compared whole.
    with_messages, without_messages = with_kwargs.pop("messages"), without_kwargs.pop("messages")
    assert [m.role for m in with_messages] == [m.role for m in without_messages]
    assert with_messages[1:] == without_messages[1:]
    for key, value in with_kwargs.items():
        if not callable(value) and key != "wall_clock_deadline":
            assert value == without_kwargs[key], key


@pytest.mark.asyncio
async def test_a_pipeline_agent_hands_back_its_pipeline_tool_calls():
    """The ``skip_llm`` path opens no trace; its ledger is its two pipeline tools."""
    prep_exec = SimpleNamespace(
        execute_tool=AsyncMock(return_value=ToolResult(tool_call_id="p", content="prepared"))
    )
    fin_exec = SimpleNamespace(
        execute_tool=AsyncMock(return_value=ToolResult(tool_call_id="f", content="finalized"))
    )
    defs = {
        "prep_tool": [ToolDefinition(name="prep_tool", description="", input_schema={"type": "object", "properties": {"input": {"type": "string"}}})],
        "fin_tool": [ToolDefinition(name="fin_tool", description="", input_schema={"type": "object", "properties": {"response": {"type": "string"}}})],
    }

    async def _fake_load(
        db, ref, user_id, agent, headers, element_scope=None, company_automation=None, owner_run=False
    ):
        name = ref.get("tool_name")
        return defs[name], prep_exec if name == "prep_tool" else fin_exec

    outcome: dict = {}
    with (
        # Stubbed like the other skip_llm tests: an environment that enforces
        # quotas would otherwise refuse the turn before the pipeline runs.
        patch("app.services.quota.check_agentic_quota", new=AsyncMock()),
        patch("app.services.quota.increment_agentic_usage", new=AsyncMock()),
        patch.object(agent_chat_mod, "_load_pipeline_tool_standalone", new=_fake_load),
        patch("app.core.redis.redis_client", new=AsyncMock(return_value=_FakeRedis())),
        patch("app.llm.router.get_llm_for_user", new=AsyncMock()),
    ):
        await agent_chat_mod.run_agent_chat(
            db=object(),  # type: ignore[arg-type]
            user_id=USER_ID,
            agent=_agent(
                skip_llm=True,
                prepare_tool_ref={"tool_name": "prep_tool"},
                finalize_tool_ref={"tool_name": "fin_tool"},
            ),  # type: ignore[arg-type]
            history=[],
            user_message="raw input",
            loop_outcome_out=outcome,
        )

    calls = outcome["ledger"]["calls"]
    assert [(c["kind"], c["name"], c["outcome"]) for c in calls] == [
        ("tool", "prep_tool", "success"),
        ("tool", "fin_tool", "success"),
    ]


# --- A consulted agent's work is counted -----------------------------


def _caller_trace() -> dict:
    return {"trace": None, "local_spans": [], "local_trace_id": uuid.uuid4()}


def _helper_publishing(output_tokens: int, *, raises: bool = False, delay: float = 0.0):
    async def _helper(*_args, **kwargs):
        await asyncio.sleep(delay)
        kwargs["loop_outcome_out"]["ledger"] = {
            "calls": [{"kind": "model", "name": "llm_call", "model": "helper-model", "duration_ms": 3.0, "outcome": "success", "usage": {"input": 10, "output": output_tokens}}],
            "spawned_task_ids": [],
        }
        if raises:
            raise RuntimeError("the helper failed part-way")
        return "the helper's answer", None

    return _helper


@pytest.mark.asyncio
async def test_a_consulted_helpers_ledger_is_folded_into_the_callers():
    from app.services import langfuse_tracing as lft
    from app.services.run_ledger import ledger_from_trace

    caller = _caller_trace()
    caller["local_spans"].append(_generation_span(40))
    token = lft.set_current_trace_ctx(caller)
    try:
        with patch.object(agent_chat_mod, "run_agent_chat", new=_helper_publishing(9)):
            answer, _ = await agent_chat_mod._run_helper_turn(object(), "user", object(), [], "question?")
    finally:
        lft.reset_current_trace_ctx(token)

    assert answer == "the helper's answer"
    outputs = [c["usage"]["output"] for c in ledger_from_trace(caller)["calls"]]
    assert outputs == [40, 9]  # the caller's own call, then the helper's


@pytest.mark.asyncio
async def test_a_helper_that_raises_is_still_counted():
    from app.services import langfuse_tracing as lft
    from app.services.run_ledger import ledger_from_trace

    caller = _caller_trace()
    token = lft.set_current_trace_ctx(caller)
    try:
        with (
            patch.object(agent_chat_mod, "run_agent_chat", new=_helper_publishing(5, raises=True)),
            pytest.raises(RuntimeError, match="part-way"),
        ):
            await agent_chat_mod._run_helper_turn(object(), "user", object(), [], "question?")
    finally:
        lft.reset_current_trace_ctx(token)

    assert [c["usage"]["output"] for c in ledger_from_trace(caller)["calls"]] == [5]


@pytest.mark.asyncio
async def test_two_consults_in_one_turn_are_both_counted():
    from app.services import langfuse_tracing as lft
    from app.services.run_ledger import ledger_from_trace

    caller = _caller_trace()
    token = lft.set_current_trace_ctx(caller)
    try:
        with patch.object(agent_chat_mod, "run_agent_chat", new=_helper_publishing(1, delay=0.01)):
            await asyncio.gather(
                agent_chat_mod._run_helper_turn(object(), "user", object(), [], "first?"),
                agent_chat_mod._run_helper_turn(object(), "user", object(), [], "second?"),
            )
    finally:
        lft.reset_current_trace_ctx(token)

    assert len(ledger_from_trace(caller)["calls"]) == 2


@pytest.mark.asyncio
async def test_a_consults_own_steps_stay_out_of_the_agents_timeline():
    """The helper's events never renumber or overwrite the agent's steps."""
    from app.llm.base import ChatResponse

    def _wrapped(inner: dict) -> dict:
        return {"status": "consult_progress", "agent_name": "Helper", "agent_id": "h", "inner": inner}

    async def _loop(**kwargs):
        on_event = kwargs["on_event"]
        await on_event({"status": "thinking", "iteration": 1})
        await on_event({"status": "tool_start", "tools": ["consult_agent"], "llm_model": "fake-model", "llm_usage": {"input_tokens": 120, "output_tokens": 8}})
        await on_event(_wrapped({"status": "thinking", "iteration": 1}))
        await on_event(_wrapped({"status": "llm_step", "llm_usage": {"input_tokens": 70, "output_tokens": 5}}))
        await on_event(_wrapped({"status": "tool_start", "tools": ["lookup"]}))
        await asyncio.sleep(0.02)
        await on_event(_wrapped({"status": "tool_done", "tool": "lookup", "success": True}))
        await on_event({"status": "tool_done", "tool": "consult_agent", "success": True})
        await on_event({"status": "thinking", "iteration": 2})
        return ChatResponse(content="the answer", model="fake-model", stop_reason="end_turn")

    _text, eval_out = await _run_eval_turn(_loop)

    steps = eval_out["traces"]
    assert [(s["type"], s.get("tool_name")) for s in steps] == [("llm", None), ("tool", "consult_agent"), ("llm", None)]
    assert steps[1]["duration_ms"] >= 20, "the consult step kept the helper's time"
    assert "#2" in steps[2]["description"], "the helper's call renumbered the agent's"


# --- A transferred conversation's work is counted -------------------


class _AgentsSession:
    """A session whose only query is the handover-peer lookup: it finds *agents*."""

    def __init__(self, agents: list) -> None:
        self._agents = agents

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def execute(self, *_args, **_kwargs):
        agents = self._agents

        class _Result:
            def scalars(self):
                return self

            def all(self):
                return list(agents)

        return _Result()


class _FakePerfClock:
    """A ``perf_counter`` that moves only when the test advances it.

    The eval trace times its steps with ``time.perf_counter``; a wall-clock
    bound on those durations is at the mercy of a loaded runner. With this
    clock a step lasts exactly what the test says it lasts.
    """

    def __init__(self) -> None:
        # Binary-exact instants and steps (powers of two), so a duration
        # read in whole milliseconds is never 124 for 125 by float rounding.
        self.now = 1024.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _TimeWithClock:
    """The ``time`` module as ``agent_chat`` sees it, ``perf_counter`` swapped."""

    def __init__(self, clock: _FakePerfClock) -> None:
        self.perf_counter = clock

    def __getattr__(self, name: str):
        return getattr(time, name)


async def _run_transfer_turn(
    *,
    mandatory: bool,
    target_seconds: float = 0.0,
    clock: _FakePerfClock | None = None,
    source_seconds: float = 0.0,
    **run_kwargs,
) -> dict:
    """Run a source turn that hands over to a specialist.

    With ``clock`` the turn's ``perf_counter`` is that fake: the source's call
    takes ``source_seconds`` and the target's run ``target_seconds`` of it,
    and nothing else moves it. Without it the target really sleeps.
    """
    from app.llm.base import ChatResponse
    from app.tools.base import ToolCall

    target = _loop_agent()
    target.id = uuid.uuid4()
    target.name = "Specialist"
    target.slug = "specialist"
    target.enabled = True
    target.description = "Answers specialist questions."
    source = _loop_agent()
    source.handover_agent_ids = [str(target.id)]
    source.handover_types = {str(target.id): "mandatory_transfer" if mandatory else "transfer"}
    source.handover_conditions = {}

    trace_ctx: dict = {"trace": None, "local_spans": [], "local_trace_id": uuid.uuid4()}
    seen: dict = {}

    async def _source_loop(**kwargs):
        seen["source_kwargs"] = kwargs
        on_event = kwargs["on_event"]
        await on_event({"status": "thinking", "iteration": 1})
        if clock is not None:
            clock.advance(source_seconds)
        trace_ctx["local_spans"].append(_generation_span(30))
        if not mandatory:
            await on_event({"status": "tool_start", "tools": ["transfer_to_agent"], "llm_model": "fake-model", "llm_usage": {"input_tokens": 90, "output_tokens": 30}})
            await kwargs["executor"].execute_tool(
                ToolCall(id="t1", name="transfer_to_agent", arguments={"agent_id": str(target.id), "reason": "specialist"})
            )
            await on_event({"status": "tool_done", "tool": "transfer_to_agent", "success": True})
        return ChatResponse(content="handing over", model="fake-model", stop_reason="end_turn")

    real_run_agent_chat = agent_chat_mod.run_agent_chat

    async def _dispatch(*args, **kwargs):
        agent = args[2] if len(args) > 2 else kwargs.get("agent")
        if agent is target:
            seen["target_handler"] = kwargs.get("on_event")
            seen["target_kwargs"] = kwargs
            await kwargs["on_event"]({"status": "thinking", "iteration": 1})
            if clock is not None:
                clock.advance(target_seconds)
            else:
                await asyncio.sleep(target_seconds)
            kwargs["loop_outcome_out"]["ledger"] = {
                "calls": [{"kind": "model", "name": "llm_call", "model": "fake-model", "duration_ms": 9.0, "outcome": "success", "usage": {"input": 60, "output": 12}}],
                "spawned_task_ids": [],
            }
            return "the specialist's answer", None
        return await real_run_agent_chat(*args, **kwargs)

    eval_out: dict = {}
    outcome: dict = {}
    with (
        patch("app.services.quota.check_agentic_quota", new=AsyncMock()),
        patch("app.services.quota.increment_agentic_usage", new=AsyncMock()),
        patch("app.core.redis.redis_client", new=AsyncMock(return_value=_FakeRedis())),
        patch("app.services.langfuse_tracing.trace_llm_call", new=AsyncMock(return_value=trace_ctx)),
        patch("app.services.langfuse_tracing.end_trace", new=MagicMock()),
        patch("app.core.database.async_session", new=lambda: _AgentsSession([target])),
        patch.object(agent_chat_mod, "run_agent_loop", new=_source_loop),
        patch.object(agent_chat_mod, "run_agent_chat", new=_dispatch),
        patch.object(agent_chat_mod, "time", new=_TimeWithClock(clock) if clock is not None else time),
    ):
        text, _ = await real_run_agent_chat(
            db=_EmptyDB(),  # type: ignore[arg-type]
            user_id="anon-participant",
            agent=source,  # type: ignore[arg-type]
            history=[],
            user_message="hello",
            conversation_id="conv-1",
            prebuilt_llm=(SimpleNamespace(provider_name="fake"), "fake-model"),
            evaluation_mode=True,
            eval_result_out=eval_out,
            loop_outcome_out=outcome,
            **run_kwargs,
        )
    return {"text": text, "eval_out": eval_out, "outcome": outcome, "seen": seen}


@pytest.mark.asyncio
@pytest.mark.parametrize("mandatory", [False, True], ids=["transfer", "mandatory_transfer"])
async def test_a_transfer_counts_the_source_and_the_target(mandatory):
    turn = await _run_transfer_turn(mandatory=mandatory)

    assert turn["text"] == "the specialist's answer"
    outputs = [c["usage"]["output"] for c in turn["outcome"]["ledger"]["calls"] if c["kind"] == "model"]
    assert outputs == [30, 12]  # the source's call, then the target's
    # The transfer exit publishes the source's timeline once: each of
    # the source's steps appears exactly once.
    steps = [(t["type"], t.get("tool_name")) for t in turn["eval_out"]["traces"]]
    assert steps == ([("llm", None)] if mandatory else [("llm", None), ("tool", "transfer_to_agent")])
    # The target's events never reach the source's timeline, and it still runs
    # with a handler.
    assert all(s["type"] != "llm" or "#2" not in s["description"] for s in turn["eval_out"]["traces"])
    assert turn["seen"]["target_handler"] is not None


# --- A call before a park, and the wait itself -----------------------


def _answer_step(output_tokens: int) -> dict:
    return {"status": "llm_step", "llm_usage": {"input_tokens": 100, "output_tokens": output_tokens}, "llm_raw_response": "working on it"}


def _park_start() -> dict:
    return {"status": "wait_start", "tool": "wait", "seconds": 30.0, "reason": "open background tasks"}


@pytest.mark.asyncio
async def test_a_call_answered_before_a_park_is_kept_and_the_park_is_a_wait_step():
    """The call the loop parked after is not overwritten by the next ``thinking``."""
    from app.llm.base import ChatResponse

    async def _loop(**kwargs):
        on_event = kwargs["on_event"]
        await on_event({"status": "thinking", "iteration": 1})
        await on_event(_answer_step(11))
        await on_event(_park_start())
        await asyncio.sleep(0.03)
        await on_event({"status": "thinking", "iteration": 2})
        return ChatResponse(content="the answer", model="fake-model", stop_reason="end_turn")

    _text, eval_out = await _run_eval_turn(_loop)

    steps = eval_out["traces"]
    assert [s["type"] for s in steps] == ["llm", "wait", "llm"]
    assert steps[0]["usage"] == {"input_tokens": 100, "output_tokens": 11}
    assert "#1" in steps[0]["description"] and "#2" in steps[2]["description"]
    assert steps[1]["duration_ms"] >= 30
    assert steps[1]["outcome"] == "success"


@pytest.mark.asyncio
async def test_a_call_nothing_closed_is_kept_when_the_next_one_starts():
    """A park refused before it started (a message was queued) announces nothing."""
    from app.llm.base import ChatResponse

    async def _loop(**kwargs):
        on_event = kwargs["on_event"]
        await on_event({"status": "thinking", "iteration": 1})
        await on_event(_answer_step(7))
        await on_event({"status": "thinking", "iteration": 2})
        return ChatResponse(content="the answer", model="fake-model", stop_reason="end_turn")

    _text, eval_out = await _run_eval_turn(_loop)

    steps = eval_out["traces"]
    assert [s["type"] for s in steps] == ["llm", "llm"]
    assert steps[0]["usage"] == {"input_tokens": 100, "output_tokens": 7}


@pytest.mark.asyncio
async def test_a_park_that_hands_the_turn_off_ends_with_the_turn():
    from app.llm.base import ChatResponse

    async def _loop(**kwargs):
        on_event = kwargs["on_event"]
        await on_event({"status": "thinking", "iteration": 1})
        await on_event(_answer_step(4))
        await on_event(_park_start())
        await asyncio.sleep(0.02)
        return ChatResponse(content="Waiting on 1 task.", model="fake-model", stop_reason="waiting_on_tasks")

    _text, eval_out = await _run_eval_turn(_loop)

    steps = eval_out["traces"]
    assert [s["type"] for s in steps] == ["llm", "wait"]
    assert steps[0]["usage"] == {"input_tokens": 100, "output_tokens": 4}
    assert steps[1]["duration_ms"] >= 20


@pytest.mark.parametrize("batch", [["wait"], ["lookup", "wait"]], ids=["alone", "after-another-call"])
@pytest.mark.asyncio
async def test_an_explicit_wait_call_is_its_own_tool_step_not_a_second_wait(batch):
    from app.llm.base import ChatResponse

    async def _loop(**kwargs):
        on_event = kwargs["on_event"]
        await on_event({"status": "thinking", "iteration": 1})
        await on_event({"status": "tool_start", "tools": batch, "llm_model": "fake-model", "llm_usage": {"input_tokens": 90, "output_tokens": 6}})
        if "lookup" in batch:
            await on_event({"status": "tool_done", "tool": "lookup", "success": True})
        await on_event(_park_start())
        await asyncio.sleep(0.01)
        await on_event({"status": "tool_done", "tool": "wait", "success": True})
        await on_event({"status": "thinking", "iteration": 2})
        return ChatResponse(content="the answer", model="fake-model", stop_reason="end_turn")

    _text, eval_out = await _run_eval_turn(_loop)

    steps = eval_out["traces"]
    assert [(s["type"], s.get("tool_name")) for s in steps] == [
        ("llm", None),
        *[("tool", name) for name in batch],
        ("llm", None),
    ]


# --- A retried call, a transfer's timing -----------------------------


@pytest.mark.asyncio
async def test_a_call_that_timed_out_and_was_retried_is_recorded_as_a_timeout():
    """The loop retries a timed-out call with no event but the next ``thinking``."""
    from app.llm.base import ChatResponse

    async def _loop(**kwargs):
        on_event = kwargs["on_event"]
        await on_event({"status": "thinking", "iteration": 1})
        await asyncio.sleep(0.01)  # the call that never returned
        await on_event({"status": "thinking", "iteration": 1})
        await on_event(_answer_step(9))
        return ChatResponse(content="the answer", model="fake-model", stop_reason="end_turn")

    _text, eval_out = await _run_eval_turn(_loop)

    steps = eval_out["traces"]
    assert [(s["type"], s["outcome"]) for s in steps] == [("llm", "timeout"), ("llm", "success")]
    assert steps[0]["duration_ms"] >= 10
    assert steps[1]["usage"] == {"input_tokens": 100, "output_tokens": 9}


@pytest.mark.asyncio
@pytest.mark.parametrize("mandatory", [False, True], ids=["transfer", "mandatory_transfer"])
async def test_the_targets_run_is_not_timed_as_the_sources_last_call(mandatory):
    # A fake clock, not a wall-clock bound: the source's call lasts exactly
    # 125 ms and the target's run 500 ms of it, so a source call timed to the
    # end of the handover reads 625, never a runner-load-dependent value.
    clock = _FakePerfClock()
    turn = await _run_transfer_turn(mandatory=mandatory, target_seconds=0.5, clock=clock, source_seconds=0.125)

    source_calls = [s for s in turn["eval_out"]["traces"] if s["type"] == "llm"]
    assert source_calls
    assert [s["duration_ms"] for s in source_calls] == [125] * len(source_calls)


@pytest.mark.asyncio
async def test_a_ledger_that_cannot_be_read_is_handed_back_partial(monkeypatch):
    """Not as an empty ledger: that would read as a run that made no call."""
    from app.llm.base import ChatResponse

    async def _loop(**_kwargs):
        return ChatResponse(content="the answer", model="fake-model", stop_reason="end_turn")

    def _unreadable(_trace_ctx):
        raise RuntimeError("the spans could not be read")

    monkeypatch.setattr(agent_chat_mod, "ledger_from_trace", _unreadable)
    outcome: dict = {}

    await _run_turn_with_outcome(_loop, loop_outcome_out=outcome, trace_ctx={"trace": None, "local_spans": []})

    assert outcome["ledger"] == {"calls": [], "spawned_task_ids": [], "partial": True}


# --- Only a timed-out call is recorded as one -------------------------


def _real_loop(script, *, with_tool: bool = False, steered: bool = False):
    """The real agent loop on a scripted model, driving the evaluation's handler.

    *steered*: a user message is queued at the first ``tool_start``, as in the
    loop's own steering tests, which is what arms the resume-promise guard.
    """
    from app.tools import agent_loop as al
    from app.tools.base import ToolDefinition
    from tests.test_agent_loop_steering import _NoopExecutor, _ScriptedStubLLM

    llm = _ScriptedStubLLM(script)
    tools = [ToolDefinition(name="noop_tool", description="No-op test tool", input_schema={"type": "object", "properties": {}})]
    queue: asyncio.Queue[str] = asyncio.Queue()

    async def _loop(**kwargs):
        steer = {"done": not steered}

        async def _on_event(event: dict) -> None:
            await kwargs["on_event"](event)
            if event.get("status") == "tool_start" and not steer["done"]:
                steer["done"] = True
                queue.put_nowait("BlackRock is validated")

        return await al.run_agent_loop(
            llm=llm,
            messages=[ChatMessage(role="user", content="Summarise the incident.")],
            tools=tools if with_tool else None,
            executor=_NoopExecutor() if with_tool else None,
            max_iterations=8,
            max_tokens=256,
            progressive_tools=False,
            on_event=_on_event,
            steering_queue=queue if steered else None,
        )

    return _loop, llm


def _usage(inp: int, out: int) -> dict:
    return {"input_tokens": inp, "output_tokens": out}


def _continuation_script():
    from app.llm.base import ChatResponse

    return [
        ChatResponse(content="Part one, ", model="stub", stop_reason="max_tokens", usage=_usage(100, 256)),
        ChatResponse(content="and the end.", model="stub", stop_reason="end_turn", usage=_usage(120, 40)),
    ]


def _empty_rescue_script():
    from app.llm.base import ChatResponse

    return [
        ChatResponse(content="", model="stub", stop_reason="end_turn", usage=_usage(200, 1)),
        ChatResponse(content="Recovered answer.", model="stub", stop_reason="end_turn", usage=_usage(210, 30)),
    ]


def _stall_nudge_script():
    from app.llm.base import ChatResponse
    from tests.test_agent_loop_steering import _STALLY_TEXT, _tool_call_response

    return [
        _tool_call_response("stub"),
        ChatResponse(content=_STALLY_TEXT, model="stub"),
        _tool_call_response("stub"),
        ChatResponse(content="Submitted. All done.", model="stub"),
    ]


def _truncated_tool_call_script():
    """A tool call cut by ``max_tokens`` is discarded and retried (``_MAX_TRUNCATION_RETRIES``)."""
    from app.llm.base import ChatResponse
    from app.tools.base import ToolCall

    return [
        ChatResponse(content="", model="stub", stop_reason="max_tokens", tool_calls=[ToolCall(id="tc-cut", name="noop_tool", arguments={})]),
        ChatResponse(content="A concise checkpoint.", model="stub"),
    ]


def _resume_promise_script():
    from app.llm.base import ChatResponse
    from tests.test_agent_loop_steering import _PROMISE_TEXT, _tool_call_response

    return [
        _tool_call_response("stub"),
        ChatResponse(content=_PROMISE_TEXT, model="stub"),
        _tool_call_response("stub"),
        ChatResponse(content="ComCyberMI draft complete.", model="stub"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("script", "with_tool", "steered", "calls", "tool_steps"),
    [
        (_continuation_script, False, False, 2, 0),
        (_empty_rescue_script, False, False, 2, 0),
        (_stall_nudge_script, True, False, 4, 2),
        # The cut call is discarded, never run: no tool step, where running it would make one.
        (_truncated_tool_call_script, True, False, 2, 0),
        (_resume_promise_script, True, True, 4, 2),
    ],
    ids=["length-cut-continuation", "empty-response-rescue", "stall-nudge", "truncated-tool-call", "resume-promise-nudge"],
)
async def test_a_re_prompt_that_is_no_timeout_is_not_recorded_as_one(script, with_tool, steered, calls, tool_steps):
    """Only the non-streamed timeout retry sends ``thinking`` again over a call with no ``llm_step``."""
    loop, llm = _real_loop(script(), with_tool=with_tool, steered=steered)

    _text, eval_out = await _run_eval_turn(loop)

    steps = eval_out["traces"]
    # Together these say the re-prompt path was taken: without it the model is
    # called fewer times, or the cut tool call runs.
    assert len(llm.calls) == calls, "the re-prompt path was not taken"
    assert len([s for s in steps if s["type"] == "tool"]) == tool_steps, steps
    assert not [s for s in steps if s.get("outcome") == "timeout" or "timed out" in s.get("description", "")], steps


@pytest.mark.parametrize("evaluation_chain", [True, False], ids=["in-a-chain", "outside"])
@pytest.mark.asyncio
async def test_a_run_whose_trace_did_not_open_hands_back_a_partial_ledger(evaluation_chain):
    """No trace, no recorded call: unmeasured in an evaluation's chain, unchanged elsewhere."""
    from app.llm.base import ChatResponse

    async def _loop(**_kwargs):
        return ChatResponse(content="the answer", model="fake-model", stop_reason="end_turn")

    outcome: dict = {}
    await _run_turn_with_outcome(_loop, loop_outcome_out=outcome, trace_ctx=None, evaluation_chain=evaluation_chain)

    assert outcome["ledger"].get("partial") is (True if evaluation_chain else None)


# --- An evaluated turn runs on the model it names, and on no other -----------


async def _resolved_for(*, evaluation_mode: bool, evaluation_chain: bool = False) -> tuple[dict, list]:
    """What one turn asks the router and the run budget for: the resolution's
    keyword arguments, and the budget overrides of each budget built.

    *evaluation_chain* is a helper the evaluated turn consulted or transferred to:
    no evaluation mode, its own model."""
    from app.llm.base import ChatResponse

    resolution: dict = {}
    budgets: list = []
    llm = SimpleNamespace(provider_name="fake", default_model="evaluated-model")

    async def _get_llm_for_user(_db, _user_id, **kwargs):
        resolution.update(kwargs)
        return llm, kwargs["model_override"]

    async def _build_budget(_db, **kwargs):
        budgets.append(kwargs["overrides"])
        return None

    async def _loop(**_kwargs):
        return ChatResponse(content="the answer", model="evaluated-model", stop_reason="end_turn")

    agent = _loop_agent()
    agent.llm_model = "agent-model"
    agent.budget_config = {"max_cost_usd": 1.0, "fallback_enabled": True, "fallback_model": "cheaper-model"}
    with (
        patch("app.services.quota.check_agentic_quota", new=AsyncMock()),
        patch("app.services.quota.increment_agentic_usage", new=AsyncMock()),
        patch("app.core.redis.redis_client", new=AsyncMock(return_value=_FakeRedis())),
        patch(
            "app.services.langfuse_tracing.trace_llm_call",
            new=AsyncMock(return_value={"trace": None, "local_spans": []}),
        ),
        patch("app.services.langfuse_tracing.end_trace", new=MagicMock()),
        patch("app.llm.router.get_llm_for_user", new=_get_llm_for_user),
        patch("app.tools.run_budget.build_run_budget_for_run", new=_build_budget),
        patch.object(agent_chat_mod, "run_agent_loop", new=_loop),
    ):
        await agent_chat_mod.run_agent_chat(
            db=_EmptyDB(),  # type: ignore[arg-type]
            user_id="anon-participant",
            agent=agent,  # type: ignore[arg-type]
            history=[],
            user_message="hello",
            conversation_id="conv-1",
            model_id_override="evaluated-model" if evaluation_mode else None,
            evaluation_mode=evaluation_mode,
            evaluation_chain=evaluation_chain,
            eval_result_out={} if evaluation_mode else None,
        )
    return resolution, budgets


@pytest.mark.asyncio
async def test_an_evaluated_turn_resolves_its_model_with_no_failover_and_no_downgrade():
    """A run that names a model measures it: no failover chain, no budget downgrade.

    A gateway denied the evaluated model on every call on a staging instance;
    with a failover chain another provider's model answered each one and the
    run was scored as the named model's.
    """
    resolution, budgets = await _resolved_for(evaluation_mode=True)

    assert resolution["model_override"] == "evaluated-model"
    assert resolution["failover"] is False
    assert len(budgets) == 1
    assert budgets[0].fallback_enabled is False
    # The rest of the agent's budget still bounds the run.
    assert budgets[0].max_cost_usd == 1.0


@pytest.mark.asyncio
async def test_a_helper_the_evaluated_turn_reached_runs_on_its_own_model_alone():
    """The whole chain is measured: a consulted or transferred agent's
    answer is part of the entry, so it is not handed to another model either."""
    resolution, budgets = await _resolved_for(evaluation_mode=False, evaluation_chain=True)

    assert resolution["model_override"] == "agent-model"
    assert resolution["failover"] is False
    assert len(budgets) == 1
    assert budgets[0].fallback_enabled is False


@pytest.mark.asyncio
async def test_a_chat_turn_keeps_its_failover_and_its_downgrade():
    resolution, budgets = await _resolved_for(evaluation_mode=False)

    assert resolution["model_override"] == "agent-model"
    assert resolution["failover"] is True
    assert len(budgets) == 1
    assert budgets[0].fallback_enabled is True
    assert budgets[0].fallback_model == "cheaper-model"


# --- An evaluated turn's iteration cap, budget and reuse fingerprint ---------


async def _turn_budget(*, evaluation_mode: bool, configured: int | None, replayed: int | None = None) -> tuple[int, int]:
    """The iteration cap and wall-clock budget one turn hands the loop, chat or evaluation."""
    from app.llm.base import ChatResponse

    seen: dict = {}
    timeout_within = agent_chat_mod.chat_loop_timeout_within

    def _timeout(max_iterations, wall_clock_deadline, **kwargs):
        seen["timeout"] = timeout_within(max_iterations, wall_clock_deadline, **kwargs)
        return seen["timeout"]

    async def _loop(**kwargs):
        seen["max_iterations"] = kwargs["max_iterations"]
        return ChatResponse(content="the answer", model="fake-model", stop_reason="end_turn")

    agent = _loop_agent()
    agent.max_iterations = configured
    with (
        patch("app.services.quota.check_agentic_quota", new=AsyncMock()),
        patch("app.services.quota.increment_agentic_usage", new=AsyncMock()),
        patch("app.core.redis.redis_client", new=AsyncMock(return_value=_FakeRedis())),
        patch(
            "app.services.langfuse_tracing.trace_llm_call",
            new=AsyncMock(return_value={"trace": None, "local_spans": []}),
        ),
        patch("app.services.langfuse_tracing.end_trace", new=MagicMock()),
        patch.object(agent_chat_mod, "run_agent_loop", new=_loop),
        patch.object(agent_chat_mod, "chat_loop_timeout_within", new=_timeout),
    ):
        await agent_chat_mod.run_agent_chat(
            db=_EmptyDB(),  # type: ignore[arg-type]
            user_id="anon-participant",
            agent=agent,  # type: ignore[arg-type]
            history=[],
            user_message="hello",
            conversation_id="conv-1",
            prebuilt_llm=(SimpleNamespace(provider_name="fake"), "fake-model"),
            evaluation_mode=evaluation_mode,
            eval_result_out={} if evaluation_mode else None,
            evaluation_overrides={"max_iterations": replayed} if replayed is not None else None,
        )
    return seen["max_iterations"], seen["timeout"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("configured", "replayed"),
    [(1, None), (3, None), (15, None), (16, None), (100, None), (None, None), (7, 3), (7, 40)],
    ids=[
        "configured=1", "configured=3", "configured=15", "configured=16", "configured=100", "configured=None",
        "replayed=3", "replayed=40",
    ],
)
async def test_an_evaluated_agent_gets_the_iteration_cap_and_budget_chat_gives_it(configured, replayed):
    """Chat floors a low cap at 15 so the agent keeps its tools; evaluation scores it the same way.

    A replay runs the saved value, not the agent's current one, under the same floor.
    """
    from app.models.general_assistant_config import CHAT_MIN_LOOP_ITERATIONS

    chat = await _turn_budget(evaluation_mode=False, configured=configured if replayed is None else replayed)
    evaluation = await _turn_budget(evaluation_mode=True, configured=configured, replayed=replayed)

    assert evaluation == chat
    assert chat[0] >= CHAT_MIN_LOOP_ITERATIONS


class _NoPlatformSettingDB:
    """No General Assistant row: the platform default iteration count applies, as in ``_EmptyDB``."""

    async def scalar(self, *_args, **_kwargs):
        return None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("configured", "replayed", "floored"),
    [
        (3, None, 15), (None, None, None), (40, None, None), (7, 3, 15), (7, 40, None), (7, "not a number", None),
        # ``isinstance(True, int)`` holds, but ``true`` is no number: the platform default, not 1 floored to 15.
        (7, True, None),
    ],
    ids=["own=3", "own=default", "own=40", "replayed=3", "replayed=40", "replayed=no-number", "replayed=true"],
)
async def test_the_reuse_fingerprint_names_the_cap_the_turn_really_runs_with(configured, replayed, floored):
    """The fingerprint gets the cap only when the floor raised it, and it is the cap ``run_agent_chat`` runs."""
    from app.services.evaluation_engine import floored_max_iterations

    agent = _loop_agent()
    agent.max_iterations = configured
    overrides = {"max_iterations": replayed} if replayed is not None else None

    resolved = await floored_max_iterations(_NoPlatformSettingDB(), agent, overrides)  # type: ignore[arg-type]
    cap, _ = await _turn_budget(evaluation_mode=True, configured=configured, replayed=replayed)

    assert resolved == floored
    if resolved is not None:
        assert resolved == cap
    if replayed is True:
        from app.models.general_assistant_config import DEFAULT_MAX_ITERATIONS

        assert cap == DEFAULT_MAX_ITERATIONS


# --- The prompt guard in an evaluated turn ---------------------------


_GUARD_REFUSAL = "Blocked by a prompt guard rule."


def _blocking_guards(*, input_blocks: bool = True):
    """The two Nova checks, stubbed to block: the input check refuses, the answer check withholds."""
    from app.services.prompt_guards import MODEL_OUTPUT_WITHHELD_MESSAGE

    chat_message = AsyncMock(return_value=_GUARD_REFUSAL if input_blocks else None)
    model_output = AsyncMock(return_value=MODEL_OUTPUT_WITHHELD_MESSAGE)
    return chat_message, model_output


async def _guarded_turn(*, input_blocks: bool = True, **run_kwargs) -> dict:
    """One turn reaching ``run_agent_loop`` (the agent answers "the answer"), both checks blocking."""
    from app.llm.base import ChatResponse

    chat_message, model_output = _blocking_guards(input_blocks=input_blocks)
    loop_kwargs: list[dict] = []

    async def _loop(**kwargs):
        loop_kwargs.append(kwargs)
        if kwargs.get("on_event") is not None:
            await kwargs["on_event"]({"status": "thinking", "iteration": 1})
        return ChatResponse(content="the answer", model="fake-model", stop_reason="end_turn")

    with (
        patch("app.services.quota.check_agentic_quota", new=AsyncMock()),
        patch("app.services.quota.increment_agentic_usage", new=AsyncMock()),
        patch("app.core.redis.redis_client", new=AsyncMock(return_value=_FakeRedis())),
        patch(
            "app.services.langfuse_tracing.trace_llm_call",
            new=AsyncMock(return_value={"trace": None, "local_spans": []}),
        ),
        patch("app.services.langfuse_tracing.end_trace", new=MagicMock()),
        patch("app.services.prompt_guards.guard_chat_message", new=chat_message),
        patch("app.services.prompt_guards.guard_model_output", new=model_output),
        patch.object(agent_chat_mod, "run_agent_loop", new=_loop),
    ):
        text, _ = await agent_chat_mod.run_agent_chat(
            db=_EmptyDB(),  # type: ignore[arg-type]
            user_id="anon-participant",
            agent=_loop_agent(),  # type: ignore[arg-type]
            history=[],
            user_message="hello",
            conversation_id="conv-1",
            prebuilt_llm=(SimpleNamespace(provider_name="fake"), "fake-model"),
            **run_kwargs,
        )
    return {"text": text, "chat_message": chat_message, "model_output": model_output, "loop_kwargs": loop_kwargs}


@pytest.mark.asyncio
async def test_an_evaluated_turn_with_the_setting_on_refuses_a_blocked_prompt_as_chat_does():
    turn = await _guarded_turn(evaluation_mode=True, evaluation_prompt_guards=True)

    assert turn["text"] == _GUARD_REFUSAL
    turn["chat_message"].assert_awaited_once()
    assert turn["loop_kwargs"] == []  # refused before the model is called


@pytest.mark.asyncio
async def test_an_evaluated_turn_with_the_setting_on_scores_a_blocked_answer_as_withheld():
    from app.services.prompt_guards import MODEL_OUTPUT_WITHHELD_MESSAGE

    turn = await _guarded_turn(input_blocks=False, evaluation_mode=True, evaluation_prompt_guards=True)

    assert turn["text"] == MODEL_OUTPUT_WITHHELD_MESSAGE
    turn["chat_message"].assert_awaited_once()
    turn["model_output"].assert_awaited_once()
    assert turn["model_output"].await_args.args[0] == "the answer"


@pytest.mark.asyncio
async def test_the_setting_changes_nothing_the_loop_is_given():
    """Tool-result screening is built inside the loop, from what the loop is given."""
    off = await _guarded_turn(input_blocks=False, evaluation_mode=True, evaluation_prompt_guards=False)
    on = await _guarded_turn(input_blocks=False, evaluation_mode=True, evaluation_prompt_guards=True)

    [off_kwargs] = off["loop_kwargs"]
    [on_kwargs] = on["loop_kwargs"]
    assert off_kwargs.keys() == on_kwargs.keys()
    assert [t.name for t in off_kwargs.get("tools") or []] == [t.name for t in on_kwargs.get("tools") or []]


@pytest.mark.asyncio
async def test_the_built_in_injection_scan_runs_with_the_setting_off():
    """The built-in defences of ``prompt_guard.py`` are not the setting's to turn off."""
    from app.services.prompt_guard import scan_user_input

    scan = MagicMock(side_effect=scan_user_input)
    with patch("app.services.prompt_guard.scan_user_input", new=scan):
        turn = await _guarded_turn(evaluation_mode=True, evaluation_prompt_guards=False)

    assert turn["text"] == "the answer"
    scan.assert_called_once_with("hello")


@pytest.mark.asyncio
@pytest.mark.parametrize("flag, withheld", [(False, False), (True, True)], ids=["off", "on"])
async def test_a_transferred_answer_follows_the_setting(flag, withheld):
    from app.services.prompt_guards import MODEL_OUTPUT_WITHHELD_MESSAGE

    chat_message, model_output = _blocking_guards(input_blocks=False)
    with (
        patch("app.services.prompt_guards.guard_chat_message", new=chat_message),
        patch("app.services.prompt_guards.guard_model_output", new=model_output),
    ):
        turn = await _run_transfer_turn(mandatory=False, evaluation_prompt_guards=flag)

    assert turn["text"] == (MODEL_OUTPUT_WITHHELD_MESSAGE if withheld else "the specialist's answer")
    assert model_output.await_count == (1 if withheld else 0)
    assert chat_message.await_count == (1 if withheld else 0)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("turn", "setting", "checked"),
    [
        ({"evaluation_mode": True}, False, False),
        ({"evaluation_mode": True}, True, True),
        # A consult or transfer of an evaluated turn: in the evaluation chain.
        ({"evaluation_chain": True}, False, False),
        ({"evaluation_chain": True}, True, True),
        # A delegated task of an evaluated turn carries the chain, not the setting.
        ({"evaluation_chain": True}, None, True),
        ({}, None, True),
        ({}, False, True),
        ({}, True, True),
    ],
    ids=[
        "evaluated-off", "evaluated-on", "nested-off", "nested-on", "delegated", "chat", "chat-given-off",
        "chat-given-on",
    ],
)
async def test_a_turn_runs_the_prompt_guard_unless_its_evaluation_turned_it_off(turn, setting, checked):
    """The one rule behind the three checks: only an evaluation's explicit off skips them."""
    from app.services.prompt_guards import MODEL_OUTPUT_WITHHELD_MESSAGE

    kwargs = {} if setting is None else {"evaluation_prompt_guards": setting}
    result = await _guarded_turn(input_blocks=False, **turn, **kwargs)

    assert result["chat_message"].await_count == (1 if checked else 0)
    assert result["model_output"].await_count == (1 if checked else 0)
    assert result["text"] == (MODEL_OUTPUT_WITHHELD_MESSAGE if checked else "the answer")


async def _run_consult_turn(*, peer_holds: str | None = None, source_holds: str | None = None, **run_kwargs) -> dict:
    """An evaluated turn that consults one peer: the arguments the consult's helper turn was given.

    ``peer_holds`` names a tool whose call the peer reports held; ``source_holds``
    one whose call the source holds after the consult.
    """
    from app.llm.base import ChatResponse
    from app.tools.base import HumanInputRequestedError, ToolCall

    peer = _loop_agent()
    peer.id = uuid.uuid4()
    peer.name = "Specialist"
    peer.slug = "specialist"
    peer.enabled = True
    peer.description = "Answers specialist questions."
    source = _loop_agent()
    source.handover_agent_ids = [str(peer.id)]
    source.handover_types = {str(peer.id): "consult"}
    source.handover_conditions = {}
    # No tool approval: this test is about what the consult hands over.
    source.approve_tool_calls = False
    helper_kwargs: list[dict] = []
    # What the tool answered, shown when the consult never reached the peer.
    tool_results: list = []

    async def _helper(*_args, **kwargs):
        helper_kwargs.append(kwargs)
        if peer_holds and kwargs.get("approvals_out") is not None:
            kwargs["approvals_out"].append({"tool_call_id": "p1", "tool_name": peer_holds, "arguments": {}})
        return "the specialist's answer", None

    async def _source_loop(**kwargs):
        call = ToolCall(id="c1", name="consult_agent", arguments={"agent_id": str(peer.id), "question": "What is it?"})
        tool_results.append(await kwargs["executor"].execute_tool(call))
        if source_holds:
            raise HumanInputRequestedError(
                question=f"Approval required before running: {source_holds}",
                tool_call_id="c2",
                conversation=[],
                proposals=[{"tool_call_id": "c2", "tool_name": source_holds, "arguments": {}}],
            )
        return ChatResponse(content="answered", model="fake-model", stop_reason="end_turn")

    with (
        patch("app.services.quota.check_agentic_quota", new=AsyncMock()),
        patch("app.services.quota.increment_agentic_usage", new=AsyncMock()),
        patch("app.core.redis.redis_client", new=AsyncMock(return_value=_FakeRedis())),
        patch("app.services.langfuse_tracing.trace_llm_call", new=AsyncMock(return_value=_caller_trace())),
        patch("app.services.langfuse_tracing.end_trace", new=MagicMock()),
        patch("app.core.database.async_session", new=lambda: _AgentsSession([peer])),
        patch("app.services.prompt_guards.guard_chat_message", new=AsyncMock(return_value=None)),
        patch("app.services.prompt_guards.guard_model_output", new=AsyncMock(side_effect=lambda text, **_k: text)),
        patch.object(agent_chat_mod, "run_agent_loop", new=_source_loop),
        patch.object(agent_chat_mod, "_run_helper_turn", new=_helper),
    ):
        await agent_chat_mod.run_agent_chat(
            db=_EmptyDB(),  # type: ignore[arg-type]
            user_id="anon-participant",
            agent=source,  # type: ignore[arg-type]
            history=[],
            user_message="hello",
            conversation_id="conv-1",
            prebuilt_llm=(SimpleNamespace(provider_name="fake"), "fake-model"),
            **run_kwargs,
        )

    assert helper_kwargs, tool_results
    [consulted] = helper_kwargs
    return consulted


# --- An evaluated agent's prompt carries no one's profile -------------


class _OwnerDB(_EmptyDB):
    """``_EmptyDB`` whose user lookup finds *user*."""

    def __init__(self, user) -> None:
        self._user = user

    async def execute(self, statement, *_args, **_kwargs):
        if "FROM users" in str(statement):
            user = self._user

            class _UserResult(_EmptyResult):
                def scalar_one_or_none(self):
                    return user

            return _UserResult()
        return _EmptyResult()


def _owner(name: str, email: str, vertical: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        full_name=name,
        email=email,
        title="Analyst",
        timezone="Europe/Paris",
        ctem_vertical=vertical,
        default_knowledge_base_ids=[],
        default_mcp_server_ids=[],
    )


async def _system_prompt(owner, *, evaluation_mode: bool, company_managed: bool = False, **turn) -> str:
    """The system prompt one turn hands the loop, run as *owner* (``None``: no owner)."""
    from app.llm.base import ChatResponse

    seen: dict = {}

    async def _loop(**kwargs):
        seen["system"] = next(m.content for m in kwargs["messages"] if m.role == "system")
        return ChatResponse(content="the answer", model="fake-model", stop_reason="end_turn")

    agent = _loop_agent()
    agent.is_company_managed = company_managed
    with (
        patch("app.services.quota.check_agentic_quota", new=AsyncMock()),
        patch("app.services.quota.increment_agentic_usage", new=AsyncMock()),
        patch("app.core.redis.redis_client", new=AsyncMock(return_value=_FakeRedis())),
        patch(
            "app.services.langfuse_tracing.trace_llm_call",
            new=AsyncMock(return_value={"trace": None, "local_spans": []}),
        ),
        patch("app.services.langfuse_tracing.end_trace", new=MagicMock()),
        # The date line changes with the clock: held fixed, it is the same in every prompt.
        patch("app.services.builtin_tools.prompting.format_datetime_prompt", new=lambda _tz=None: "\n[date]"),
        # The CTEM vertical line is XTM One's: pinned, whatever mode the suite runs in.
        patch("app.core.config.Settings.is_xtm_one", new_callable=PropertyMock, return_value=True),
        patch.object(agent_chat_mod, "run_agent_loop", new=_loop),
    ):
        await agent_chat_mod.run_agent_chat(
            db=_OwnerDB(owner),  # type: ignore[arg-type]
            user_id=str(owner.id) if owner is not None else "anonymous",
            agent=agent,  # type: ignore[arg-type]
            history=[],
            user_message="hello",
            conversation_id="conv-1",
            prebuilt_llm=(SimpleNamespace(provider_name="fake"), "fake-model"),
            evaluation_mode=evaluation_mode,
            eval_result_out={} if evaluation_mode else None,
            **turn,
        )
    return seen["system"]


_ADA = _owner("Ada Lovelace", "ada@example.com", "fraud")
_ALAN = _owner("Alan Turing", "alan@example.com", "fimi")


@pytest.mark.asyncio
@pytest.mark.parametrize("company_managed", [False, True], ids=["own-agent", "company-managed"])
async def test_an_evaluated_agents_prompt_is_the_same_whoever_owns_the_evaluation(company_managed):
    """No one's profile: two owners get the same prompt, and an ownerless run the same identity."""
    ada, alan, ownerless = [
        await _system_prompt(owner, evaluation_mode=True, company_managed=company_managed)
        for owner in (_ADA, _ALAN, None)
    ]

    assert ada == alan
    for prompt in (ada, ownerless):
        for leaked in ("Ada Lovelace", "ada@example.com", "Alan Turing", "Europe/Paris", "Unidentified User"):
            assert leaked not in prompt
        # The default vertical a new user's chat gets, not the owner's.
        assert "Active CTEM vertical: cyber" in prompt
        # No identity is shown, so none is declared verified.
        assert "platform-verified" not in prompt
        if company_managed:
            # Memory is off in evaluation: the notice that says so.
            assert "you have no personal memory" in prompt
    # An ownerless run has no acting user, so it cannot delegate: the only difference, either way.
    assert set(ada.splitlines()) ^ set(ownerless.splitlines()) == {
        line for line in ada.splitlines() if line.startswith(("## Agent Delegation", "**Parallel self-execution"))
    }


@pytest.mark.asyncio
async def test_a_chat_turn_still_carries_the_users_profile():
    """Chat is unchanged: the identity line, the user's vertical, the per-user memory notice."""
    own = await _system_prompt(_ADA, evaluation_mode=False)
    shared = await _system_prompt(_ADA, evaluation_mode=False, company_managed=True)

    assert "Assisting Ada Lovelace, Analyst (ada@example.com, timezone: Europe/Paris)." in own
    assert "Active CTEM vertical: fraud" in own
    assert "per-user memory is private" in shared
    assert "platform-verified" in shared


@pytest.mark.asyncio
@pytest.mark.parametrize("company_managed", [False, True], ids=["own-agent", "company-managed"])
async def test_an_agent_the_evaluated_agent_hands_work_to_carries_no_ones_profile_either(company_managed):
    """A consult, transfer or delegated task of an evaluation runs in its chain, under the same rule."""
    prompt = await _system_prompt(_ADA, evaluation_mode=False, company_managed=company_managed, evaluation_chain=True)

    for leaked in ("Ada Lovelace", "ada@example.com", "Europe/Paris", "platform-verified"):
        assert leaked not in prompt
    assert "Active CTEM vertical: cyber" in prompt
    if company_managed:
        assert "you have no personal memory" in prompt


# --- No memory and no scheduling in any turn of an evaluation ----------------


async def _memory_turn(*, user_id: str = "anon-participant", **turn) -> tuple[dict, int, set[str]]:
    """One turn of an agent with short-term and persistent memory on.

    Returns the memory tiers it loaded, how often it stored one, and the tool names the loop was given.
    """
    from app.llm.base import ChatResponse

    span = MagicMock(return_value=None)
    store = AsyncMock(return_value=0)
    tool_names: set[str] = set()

    async def _loop(**kwargs):
        tool_names.update(t.name for t in kwargs.get("tools") or [])
        return ChatResponse(content="the answer", model="fake-model", stop_reason="end_turn")

    agent = _loop_agent()
    agent.memory_persistent_enabled = True

    with (
        patch("app.services.quota.check_agentic_quota", new=AsyncMock()),
        patch("app.services.quota.increment_agentic_usage", new=AsyncMock()),
        patch("app.core.redis.redis_client", new=AsyncMock(return_value=_FakeRedis())),
        patch(
            "app.services.langfuse_tracing.trace_llm_call",
            new=AsyncMock(return_value={"trace": None, "local_spans": []}),
        ),
        patch("app.services.langfuse_tracing.end_trace", new=MagicMock()),
        patch("app.services.langfuse_tracing.start_memory_span", new=span),
        patch("app.services.agent_memory.store_short_term_memory", new=store),
        patch("app.services.agent_memory.consolidate_memory", new=AsyncMock()),
        patch("app.services.persistent_memory.load_persistent_context", new=AsyncMock(return_value="")),
        patch.object(agent_chat_mod, "run_agent_loop", new=_loop),
    ):
        await agent_chat_mod.run_agent_chat(
            db=_EmptyDB(),  # type: ignore[arg-type]
            user_id=user_id,
            agent=agent,  # type: ignore[arg-type]
            history=[],
            user_message="hello",
            conversation_id="conv-1",
            prebuilt_llm=(SimpleNamespace(provider_name="fake"), "fake-model"),
            **turn,
        )
    return span.call_args.kwargs, store.await_count, tool_names


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("turn", "remembers"),
    [
        ({}, True),
        ({"evaluation_mode": True, "eval_result_out": {}}, False),
        # A consult, transfer or delegated task the evaluated agent reached.
        ({"evaluation_chain": True}, False),
    ],
    ids=["chat", "evaluated", "in-the-evaluation-chain"],
)
async def test_no_turn_of_an_evaluation_reads_or_writes_memory(turn, remembers):
    """No memory is read or written for any agent an evaluation reaches, not just the evaluated one."""
    loaded, stored, tools = await _memory_turn(**turn)

    assert loaded["short_term_enabled"] is remembers
    assert loaded["persistent_enabled"] is remembers
    assert stored == (1 if remembers else 0)
    # The memory tools come and go with the persistent tier: none at all in an evaluation.
    assert bool({"store_memory", "search_memory"} & tools) is remembers


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("turn", "schedules"),
    [
        ({}, True),
        ({"evaluation_mode": True, "eval_result_out": {}}, False),
        # A consult, transfer or delegated task the evaluated agent reached.
        ({"evaluation_chain": True}, False),
    ],
    ids=["chat", "evaluated", "in-the-evaluation-chain"],
)
async def test_no_turn_of_an_evaluation_can_schedule_an_action(turn, schedules):
    """No scheduling tool in any turn of an evaluation.

    A scheduled action runs later as the evaluation's owner, outside the evaluation,
    and the list tool reads the owner's own items. A signed-in user, since an
    anonymous turn never gets them.
    """
    from app.services.builtin_tools.scheduling import SCHEDULING_TOOL_NAMES

    _loaded, _stored, tools = await _memory_turn(user_id=str(uuid.uuid4()), **turn)

    assert bool(SCHEDULING_TOOL_NAMES & tools) is schedules
    if schedules:
        assert tools >= SCHEDULING_TOOL_NAMES


# --- Tool approval in an evaluation ------------------------------------


async def _approval_turn(*, suppressed: str | None = None, **turn) -> dict:
    """One turn of an agent that asks for approval, whose loop calls its one tool.

    The real gate decides: with no whitelist and no standing approval, a call
    to the tool is held when the gate is on. Returns what the turn handed the
    gate builder, its answer, the proposals it reported, and whether the tool ran.
    """
    from app.llm.base import ChatResponse
    from app.services import hitl_whitelist
    from app.tools.base import CallableToolExecutor, ToolCall

    seen: dict = {"ran": False}
    real_build = hitl_whitelist.build_approval_gate

    async def _spy_build(db, composite, **kwargs):
        seen["hitl_enabled"] = kwargs["hitl_enabled"]
        return await real_build(db, composite, **kwargs)

    async def _one_tool(_db, *, composite, **_kwargs):
        async def _get_clue() -> str:
            seen["ran"] = True
            return "the clue"

        executor = CallableToolExecutor()
        executor.register("get_clue", _get_clue)
        composite.add_tools(
            [ToolDefinition(name="get_clue", description="", input_schema={"type": "object", "properties": {}})],
            executor,
        )
        return None

    async def _loop(**kwargs):
        from app.tools.base import HumanInputRequestedError

        try:
            await kwargs["executor"].execute_tool(ToolCall(id="c1", name="get_clue", arguments={}))
        except HumanInputRequestedError as held:
            # As the real loop does: the held call leaves as a proposal.
            raise HumanInputRequestedError(
                question=held.question,
                tool_call_id=held.tool_call_id,
                conversation=[],
                proposals=[{"tool_call_id": "c1", "tool_name": "get_clue", "arguments": {}}],
            ) from held
        return ChatResponse(content="the tool's work", model="fake-model", stop_reason="end_turn")

    agent = _loop_agent()
    agent.approve_tool_calls = True
    agent.custom_tool_ids = ["00000000-0000-0000-0000-00000000c1ee"]
    approvals: list = []
    with (
        patch("app.services.quota.check_agentic_quota", new=AsyncMock()),
        patch("app.services.quota.increment_agentic_usage", new=AsyncMock()),
        patch("app.core.redis.redis_client", new=AsyncMock(return_value=_FakeRedis())),
        patch(
            "app.services.langfuse_tracing.trace_llm_call",
            new=AsyncMock(return_value={"trace": None, "local_spans": []}),
        ),
        patch("app.services.langfuse_tracing.end_trace", new=MagicMock()),
        patch("app.services.builtin_tools.load_custom_and_mcp_tools", new=_one_tool),
        patch("app.services.hitl_whitelist.build_approval_gate", new=_spy_build),
        patch("app.services.hitl_whitelist.resolve_whitelist", new=AsyncMock(return_value=frozenset())),
        patch("app.services.hitl_whitelist.resolve_vetoed", new=AsyncMock(return_value=frozenset())),
        patch("app.services.hitl_whitelist.resolve_gate_suppression", new=AsyncMock(return_value=suppressed)),
        patch.object(agent_chat_mod, "run_agent_loop", new=_loop),
    ):
        text, _ = await agent_chat_mod.run_agent_chat(
            db=_EmptyDB(),  # type: ignore[arg-type]
            user_id="anon-participant",
            agent=agent,  # type: ignore[arg-type]
            history=[],
            user_message="find the clue",
            conversation_id="conv-1",
            prebuilt_llm=(SimpleNamespace(provider_name="fake"), "fake-model"),
            approvals_out=approvals,
            **turn,
        )
    return {**seen, "text": text, "held": [p.get("tool_name") for p in approvals]}


_EVALUATED = {"evaluation_mode": True, "eval_result_out": {}}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("turn", "setting", "held"),
    [
        (_EVALUATED, False, False),
        (_EVALUATED, True, True),
        # A consult or transfer of an evaluated turn: in the evaluation chain.
        ({"evaluation_chain": True}, False, False),
        ({"evaluation_chain": True}, True, True),
        # A task spawned by an older release carries the chain but no setting.
        ({"evaluation_chain": True}, None, True),
        ({}, None, True),
        ({}, False, True),
    ],
    ids=["evaluated-off", "evaluated-on", "chain-off", "chain-on", "chain-unset", "chat", "chat-given-off"],
)
async def test_a_turn_holds_tool_calls_unless_its_evaluation_turned_approval_off(turn, setting, held):
    """Only an evaluation's explicit off skips the gate."""
    kwargs = {} if setting is None else {"evaluation_tool_approval": setting}
    result = await _approval_turn(**turn, **kwargs)

    assert result["hitl_enabled"] is held
    assert result["held"] == (["get_clue"] if held else [])
    assert result["ran"] is not held
    if not held:
        assert result["text"] == "the tool's work"


@pytest.mark.asyncio
async def test_with_the_setting_on_the_gate_still_decides_as_configured():
    """The setting decides whether the gate applies, not how: the kill switch still lets the call run."""
    from app.tools.approval import OUTCOME_APPROVALS_DISABLED

    result = await _approval_turn(suppressed=OUTCOME_APPROVALS_DISABLED, evaluation_tool_approval=True, **_EVALUATED)

    assert (result["hitl_enabled"], result["held"], result["ran"]) == (True, [], True)


# The two settings differ in each case, so handing one in place of the other fails.
_HANDED_SETTINGS = pytest.mark.parametrize(
    ("guards", "approval"), [(False, True), (True, False)], ids=["guards-off-approval-on", "guards-on-approval-off"]
)


@pytest.mark.asyncio
@_HANDED_SETTINGS
@pytest.mark.parametrize("mandatory", [False, True], ids=["transfer", "mandatory_transfer"])
async def test_a_transfer_hands_its_target_the_evaluations_settings_and_no_answer_callback(mandatory, guards, approval):
    """The target runs in the evaluation chain under the run's settings; it is not the evaluated turn."""
    held: list = []
    with (
        patch("app.services.prompt_guards.guard_chat_message", new=AsyncMock(return_value=None)),
        patch("app.services.prompt_guards.guard_model_output", new=AsyncMock(side_effect=lambda text, **_k: text)),
    ):
        turn = await _run_transfer_turn(
            mandatory=mandatory, evaluation_prompt_guards=guards, evaluation_tool_approval=approval, approvals_out=held
        )

    target = turn["seen"]["target_kwargs"]
    assert target["evaluation_chain"] is True
    assert target["evaluation_prompt_guards"] is guards
    assert target["evaluation_tool_approval"] is approval
    # Held calls of the target mark the evaluated entry.
    assert target["approvals_out"] is held
    assert target.get("on_token") is None
    assert target.get("eval_result_out") is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("evaluated", "guards", "approval"),
    [(True, False, True), (True, True, False), (False, None, None)],
    ids=["evaluated-guards-off-approval-on", "evaluated-guards-on-approval-off", "chat"],
)
async def test_a_consult_hands_the_peer_the_evaluations_settings_and_no_answer_callback(evaluated, guards, approval):
    """An evaluated consult runs the peer in the chain under the run's settings; in chat the peer's held calls stay its own."""
    held: list = []
    turn = (
        {"evaluation_mode": True, "eval_result_out": {}, "evaluation_prompt_guards": guards, "evaluation_tool_approval": approval}
        if evaluated
        else {}
    )
    consulted = await _run_consult_turn(approvals_out=held, **turn)

    if evaluated:
        assert consulted["evaluation_chain"] is True
        assert consulted["evaluation_prompt_guards"] is guards
        assert consulted["evaluation_tool_approval"] is approval
        assert consulted["approvals_out"] is held
    else:
        assert consulted.get("evaluation_tool_approval") is None
        assert consulted.get("approvals_out") is None
    # The consulted agent's turn cannot set the answer's start.
    assert consulted.get("on_token") is None
    assert consulted.get("eval_result_out") is None
    assert not consulted.get("evaluation_mode")


@pytest.mark.asyncio
async def test_the_entry_names_the_calls_held_by_the_consulted_peer_and_by_the_source():
    """Both hold a call: the held calls list holds both, whoever held them."""
    held: list = []
    await _run_consult_turn(
        peer_holds="peer_tool",
        source_holds="source_tool",
        evaluation_mode=True,
        eval_result_out={},
        evaluation_tool_approval=True,
        approvals_out=held,
    )

    assert sorted(p["tool_name"] for p in held) == ["peer_tool", "source_tool"]


# --- Time to first token on Full Loop -----------------------------------


class _StreamingModel:
    """A model client whose calls behave like a provider's, recording when each call's first text chunk arrived.

    ``first_chunk_windows`` holds, per streamed call, the ``perf_counter`` instants
    just before and just after its first text chunk was handed on: the evaluated
    turn's answer start must fall inside the window of the call that answered.
    """

    provider_name = "fake"

    def __init__(self) -> None:
        self.first_chunk_windows: list[tuple[float, float]] = []

    async def _text(self, on_token, text: str) -> None:
        before = time.perf_counter()
        await on_token(text[:3])
        self.first_chunk_windows.append((before, time.perf_counter()))
        await asyncio.sleep(0.02)  # the rest of the answer arrives later
        await on_token(text[3:])

    async def chat_stream_with_tools(self, *_args, on_token=None, on_thinking=None, text: str = "the answer", **_kwargs):
        from app.llm.base import ChatResponse

        if on_thinking is not None:
            await on_thinking("Let me think.")  # reasoning: never a token of the answer
            await asyncio.sleep(0.02)
        if text:
            await self._text(on_token, text)
        return ChatResponse(content=text, model="fake-model", stop_reason="end_turn")

    async def chat_stream(self, *_args, text: str = "the answer", **_kwargs):
        before = time.perf_counter()
        yield text[:3]
        self.first_chunk_windows.append((before, time.perf_counter()))
        await asyncio.sleep(0.02)
        yield text[3:]

    async def chat(self, *_args, **_kwargs):
        from app.llm.base import ChatResponse

        return ChatResponse(content="the answer", model="fake-model", stop_reason="end_turn")


async def _timed_eval_turn(loop, model: _StreamingModel | None = None, **run_kwargs) -> dict:
    """One evaluated turn (``eval_result_out`` given) whose loop is *loop*, on *model*."""
    model = model or _StreamingModel()
    loop_kwargs: dict = {}
    eval_out: dict = {}

    async def _capturing_loop(**kwargs):
        loop_kwargs.update(kwargs)
        return await loop(**kwargs)

    with (
        patch("app.services.quota.check_agentic_quota", new=AsyncMock()),
        patch("app.services.quota.increment_agentic_usage", new=AsyncMock()),
        patch("app.core.redis.redis_client", new=AsyncMock(return_value=_FakeRedis())),
        patch(
            "app.services.langfuse_tracing.trace_llm_call",
            new=AsyncMock(return_value={"trace": None, "local_spans": []}),
        ),
        patch("app.services.langfuse_tracing.end_trace", new=MagicMock()),
        patch.object(agent_chat_mod, "run_agent_loop", new=_capturing_loop),
    ):
        text, _ = await agent_chat_mod.run_agent_chat(
            db=_EmptyDB(),  # type: ignore[arg-type]
            user_id="anon-participant",
            agent=_loop_agent(),  # type: ignore[arg-type]
            history=[],
            user_message="hello",
            conversation_id="conv-1",
            prebuilt_llm=(model, "fake-model"),
            **({"evaluation_mode": True, "eval_result_out": eval_out} | run_kwargs),
        )
    return {"text": text, "eval_out": eval_out, "loop_kwargs": loop_kwargs, "model": model}


async def _raw(_chunk: str) -> None:
    """The loop's own stream handler: it holds text back before deciding it is the answer."""


@pytest.mark.asyncio
async def test_a_turn_outside_evaluation_hands_the_loop_its_callers_callback_and_client():
    """Nothing changes outside an evaluated turn."""
    from app.llm.base import ChatResponse

    async def _loop(**_kwargs):
        return ChatResponse(content="the answer", model="fake-model", stop_reason="end_turn")

    async def _caller_token(_chunk: str) -> None:
        return None

    model = _StreamingModel()
    chat = await _timed_eval_turn(_loop, model, evaluation_mode=False, eval_result_out=None, on_token=_caller_token)
    assert chat["loop_kwargs"]["llm"] is model
    assert chat["loop_kwargs"]["on_token"] is _caller_token

    unrecorded = await _timed_eval_turn(_loop, model, eval_result_out=None)
    assert unrecorded["loop_kwargs"]["llm"] is model
    assert unrecorded["loop_kwargs"]["on_token"] is None

    with_caller = await _timed_eval_turn(_loop, model, on_token=_caller_token)
    assert with_caller["loop_kwargs"]["on_token"] is _caller_token
    assert with_caller["loop_kwargs"]["llm"] is model


@pytest.mark.asyncio
async def test_a_short_answer_starts_at_its_calls_first_token_not_when_it_is_shown():
    """Held back by the loop, a short answer reaches the callback after its call: the start is still its first token."""
    async def _loop(**kwargs):
        await kwargs["on_event"]({"status": "thinking", "iteration": 1})
        response = await kwargs["llm"].chat_stream_with_tools(on_token=_raw)
        await asyncio.sleep(0.02)
        await kwargs["on_token"](response.content)  # replayed once the call ended
        return response

    turn = await _timed_eval_turn(_loop)

    assert turn["loop_kwargs"]["llm"] is not turn["model"]  # the evaluated turn's timing client
    assert turn["loop_kwargs"]["on_token"] is not None
    (before, after) = turn["model"].first_chunk_windows[0]
    assert before <= turn["eval_out"]["answer_started_at"] <= after
    assert "ttft_ms" not in turn["eval_out"]


@pytest.mark.asyncio
async def test_a_retracted_answer_gives_way_to_the_later_calls():
    async def _loop(**kwargs):
        on_event, answer = kwargs["on_event"], kwargs["on_token"]
        await on_event({"status": "thinking", "iteration": 1})
        first = await kwargs["llm"].chat_stream_with_tools(on_token=answer, text="a long preamble")
        await on_event({"status": "stream_retract"})  # tool calls followed
        await on_event({"status": "thinking", "iteration": 2})
        await kwargs["llm"].chat_stream_with_tools(on_token=answer)
        return first

    turn = await _timed_eval_turn(_loop)

    (before, after) = turn["model"].first_chunk_windows[1]
    assert before <= turn["eval_out"]["answer_started_at"] <= after


@pytest.mark.asyncio
async def test_a_continued_answer_keeps_its_first_calls_start():
    async def _loop(**kwargs):
        answer = kwargs["on_token"]
        await kwargs["on_event"]({"status": "thinking", "iteration": 1})
        await kwargs["llm"].chat_stream_with_tools(on_token=answer, text="the first half")
        return await kwargs["llm"].chat_stream_with_tools(on_token=answer, text=" and the rest")  # the continuation

    turn = await _timed_eval_turn(_loop)

    (before, after) = turn["model"].first_chunk_windows[0]
    assert before <= turn["eval_out"]["answer_started_at"] <= after


@pytest.mark.asyncio
async def test_reasoning_never_counts_as_the_answers_first_token():
    async def _loop(**kwargs):
        reasoning: list[float] = []

        async def _on_thinking(_chunk: str) -> None:
            reasoning.append(time.perf_counter())

        await kwargs["on_event"]({"status": "thinking", "iteration": 1})
        response = await kwargs["llm"].chat_stream_with_tools(on_token=kwargs["on_token"], on_thinking=_on_thinking)
        assert reasoning
        return response

    turn = await _timed_eval_turn(_loop)

    (before, after) = turn["model"].first_chunk_windows[0]
    assert before <= turn["eval_out"]["answer_started_at"] <= after


@pytest.mark.asyncio
async def test_a_streamed_call_without_tools_is_timed_too():
    async def _loop(**kwargs):
        from app.llm.base import ChatResponse

        await kwargs["on_event"]({"status": "thinking", "iteration": 1})
        chunks = [chunk async for chunk in kwargs["llm"].chat_stream()]
        await kwargs["on_token"]("".join(chunks))
        return ChatResponse(content="".join(chunks), model="fake-model", stop_reason="end_turn")

    turn = await _timed_eval_turn(_loop)

    (before, after) = turn["model"].first_chunk_windows[0]
    assert before <= turn["eval_out"]["answer_started_at"] <= after


@pytest.mark.asyncio
async def test_an_answer_from_a_non_streamed_call_has_no_start():
    async def _loop(**kwargs):
        await kwargs["on_event"]({"status": "thinking", "iteration": 1})
        response = await kwargs["llm"].chat()
        await kwargs["on_token"](response.content)  # sent in chunks, but nothing streamed it
        return response

    turn = await _timed_eval_turn(_loop)

    assert turn["eval_out"]["answer_started_at"] is None


@pytest.mark.asyncio
async def test_the_answer_starts_after_the_last_tool_call():
    tool_ended_at: list[float] = []

    async def _loop(**kwargs):
        on_event = kwargs["on_event"]
        await on_event({"status": "thinking", "iteration": 1})
        await kwargs["llm"].chat_stream_with_tools(on_token=_raw, text="")  # a bare tool call
        await on_event({"status": "tool_start", "tools": ["lookup"]})
        await asyncio.sleep(0.02)
        await on_event({"status": "tool_done", "tool": "lookup", "success": True})
        tool_ended_at.append(time.perf_counter())
        await on_event({"status": "thinking", "iteration": 2})
        response = await kwargs["llm"].chat_stream_with_tools(on_token=kwargs["on_token"])
        return response

    turn = await _timed_eval_turn(_loop)

    assert turn["eval_out"]["answer_started_at"] > tool_ended_at[0]


@pytest.mark.asyncio
async def test_streaming_leaves_the_final_calls_recorded_duration_whole():
    """The first token does not close the model step: it lasts as long as the call."""
    async def _loop(**kwargs):
        on_event = kwargs["on_event"]
        await on_event({"status": "thinking", "iteration": 1})
        await on_event({"status": "streaming"})
        response = await kwargs["llm"].chat_stream_with_tools(on_token=kwargs["on_token"])
        await asyncio.sleep(0.05)  # the call is still answering
        await on_event(_answer_step(9))
        return response

    turn = await _timed_eval_turn(_loop)

    [step] = [s for s in turn["eval_out"]["traces"] if s["type"] == "llm"]
    assert step["duration_ms"] >= 60  # 20 ms + 50 ms of sleeps, with room for a coarse clock


@pytest.mark.asyncio
async def test_the_answer_guard_still_withholds_a_blocked_answer_in_an_evaluated_turn():
    """The turn's own callback shows nothing, so the guard is told the answer was not streamed."""
    from app.services.prompt_guards import MODEL_OUTPUT_WITHHELD_MESSAGE

    async def _loop(**kwargs):
        await kwargs["on_event"]({"status": "thinking", "iteration": 1})
        return await kwargs["llm"].chat_stream_with_tools(on_token=kwargs["on_token"])

    model_output = AsyncMock(return_value=MODEL_OUTPUT_WITHHELD_MESSAGE)
    with (
        patch("app.services.prompt_guards.guard_chat_message", new=AsyncMock(return_value=None)),
        patch("app.services.prompt_guards.guard_model_output", new=model_output),
    ):
        turn = await _timed_eval_turn(_loop, evaluation_prompt_guards=True)

    assert turn["text"] == MODEL_OUTPUT_WITHHELD_MESSAGE
    assert model_output.await_args.kwargs["streamed"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("mandatory", [False, True], ids=["transfer", "mandatory_transfer"])
async def test_a_transferred_answer_has_no_start(mandatory):
    turn = await _run_transfer_turn(mandatory=mandatory)

    assert turn["eval_out"]["answer_started_at"] is None
    if mandatory:
        # Chat does not stream an answer that will be handed over; nor does the evaluation.
        assert turn["seen"]["source_kwargs"]["on_token"] is None


@pytest.mark.asyncio
async def test_an_answer_from_a_call_the_timer_did_not_see_has_no_start():
    """A budget downgrade swaps the loop's client for one the turn did not wrap: no stale start."""
    async def _loop(**kwargs):
        on_event = kwargs["on_event"]
        await on_event({"status": "thinking", "iteration": 1})
        await kwargs["llm"].chat_stream_with_tools(on_token=_raw, text="a preamble")  # timed, not the answer
        await on_event({"status": "thinking", "iteration": 2})
        fallback = _StreamingModel()  # the downgraded client, unwrapped
        return await fallback.chat_stream_with_tools(on_token=kwargs["on_token"])

    turn = await _timed_eval_turn(_loop)

    assert turn["eval_out"]["answer_started_at"] is None


def _failover_model(partial: str):
    """A primary whose connection drops after *partial*, failing over to a fallback timed like ``_StreamingModel``."""
    from app.llm.base import BaseLLMProvider, ChatResponse
    from app.llm.router import FailoverLLMProvider

    class _Provider(BaseLLMProvider):
        context_window = 100_000

        async def chat(self, *_args, **_kwargs):
            raise NotImplementedError

        async def embed(self, *_args, **_kwargs):
            return [0.0]

        async def list_models(self):
            return []

    class _DropsMidAnswer(_Provider):
        provider_name = "primary"

        async def chat_stream_with_tools(self, *_args, on_token=None, **_kwargs):
            await on_token(partial)
            raise RuntimeError("connection reset mid-stream")

        async def chat_stream(self, *_args, **_kwargs):
            yield partial
            raise RuntimeError("connection reset mid-stream")

    class _Fallback(_Provider, _StreamingModel):
        provider_name = "fallback"

        def __init__(self) -> None:
            _StreamingModel.__init__(self)

        async def chat_stream_with_tools(self, *_args, on_token=None, **_kwargs):
            await asyncio.sleep(0.02)  # the fallback answers later than the primary began
            await self._text(on_token, "the answer")
            return ChatResponse(content="the answer", model="fallback-model", stop_reason="end_turn")

        async def chat_stream(self, *_args, **_kwargs):
            await asyncio.sleep(0.02)
            async for chunk in _StreamingModel.chat_stream(self):
                yield chunk

    fallback = _Fallback()
    return FailoverLLMProvider(primary=_DropsMidAnswer(), fallbacks=[fallback]), fallback


@pytest.mark.asyncio
@pytest.mark.parametrize("past_holdback", [False, True], ids=["held_back", "shown_then_retracted"])
async def test_an_answer_restarted_by_a_failover_starts_at_the_fallbacks_first_token(past_holdback):
    """The primary drops mid-answer and the fallback answers again from the start, inside one call.

    Its partial is either still held back by the loop (no retract) or already shown
    (retracted): either way the answer is the fallback's, and so is its start.
    """
    from app.tools.agent_loop import _PREAMBLE_HOLDBACK_CHARS, run_agent_loop

    partial = "x" * (_PREAMBLE_HOLDBACK_CHARS + 50) if past_holdback else "The pri"
    model, fallback = _failover_model(partial)

    turn = await _timed_eval_turn(run_agent_loop, model)

    assert turn["text"] == "the answer"
    (before, after) = fallback.first_chunk_windows[-1]
    assert before <= turn["eval_out"]["answer_started_at"] <= after


@pytest.mark.asyncio
async def test_the_evaluation_engines_turn_never_asks_for_segments():
    """The engine runs the agent turn in evaluation mode and never asks for segments.

    A re-prompt takes back a narration that streamed unless segments are delivered;
    delivered as a segment, it would stay, and with it the start of an answer the
    later call wrote.
    """
    from app.services import evaluation_engine

    seen: dict = {}

    async def _turn(*_args, **kwargs):
        seen.update(kwargs)
        return "the answer", None

    with patch.object(agent_chat_mod, "run_agent_chat", new=_turn):
        await evaluation_engine._execute_agent_full_loop(object(), _loop_agent(), "fake-model", "hello")  # type: ignore[arg-type]

    assert seen["evaluation_mode"] is True
    assert not seen.get("deliver_segments")


@pytest.mark.asyncio
async def test_an_evaluated_turn_never_asks_the_loop_for_segments():
    """An evaluated turn called as the engine calls it, with no ``deliver_segments``, does
    not ask the loop for segments, so a re-prompt takes back the narration that streamed
    before it.

    ``run_agent_chat`` forwards the flag as given: this catches its default turning on, or
    evaluation mode starting to request segments, which the engine-side test cannot see.
    """

    async def _loop(**kwargs):
        await kwargs["on_event"]({"status": "thinking", "iteration": 1})
        return await kwargs["llm"].chat_stream_with_tools(on_token=kwargs["on_token"])

    turn = await _timed_eval_turn(_loop)

    assert not turn["loop_kwargs"].get("deliver_segments")


@pytest.mark.asyncio
async def test_the_real_loop_sets_the_start_at_the_answering_calls_first_token():
    """End to end: the actual agent loop, its events in their real order, and the turn's timing."""
    from app.tools.agent_loop import run_agent_loop

    turn = await _timed_eval_turn(run_agent_loop)

    assert turn["text"] == "the answer"
    (before, after) = turn["model"].first_chunk_windows[-1]
    assert before <= turn["eval_out"]["answer_started_at"] <= after


# --- The fan-out cap in an evaluated turn -------------------------------
# An evaluated turn has no parent run and no conversation, the two scopes the
# per-run cap counts by: its spawns are counted under a scope of their own.


async def _spawn_scopes(turns: int) -> list[list[dict]]:
    """The scope each of two spawns is counted under, per evaluated turn."""
    from app.llm.base import ChatResponse
    from app.tools.base import ToolCall

    counted: list[dict] = []

    async def _count(**scope):
        counted.append(scope)
        return 0

    async def _loop(**kwargs):
        for i in range(2):
            result = await kwargs["executor"].execute_tool(
                ToolCall(id=f"s{i}", name="spawn_background_task", arguments={"agent_id": "self", "task": "a part"})
            )
            assert "spawned successfully" in result.content, result.content
        return ChatResponse(content="the answer", model="fake-model", stop_reason="end_turn")

    scopes = []
    with (
        patch("app.services.quota.check_agentic_quota", new=AsyncMock()),
        patch("app.services.quota.increment_agentic_usage", new=AsyncMock()),
        patch("app.core.redis.redis_client", new=AsyncMock(return_value=_FakeRedis())),
        patch(
            "app.services.langfuse_tracing.trace_llm_call",
            new=AsyncMock(return_value={"trace": None, "local_spans": []}),
        ),
        patch("app.services.langfuse_tracing.end_trace", new=MagicMock()),
        patch.object(agent_chat_mod, "run_agent_loop", new=_loop),
        patch("app.services.sub_agent.count_active_sub_agent_executions", new=_count),
        patch(
            "app.services.sub_agent.create_sub_agent_task",
            new=AsyncMock(side_effect=lambda *_a, **_k: SimpleNamespace(id=uuid.uuid4(), status="pending")),
        ),
    ):
        for _ in range(turns):
            counted.clear()
            await agent_chat_mod.run_agent_chat(
                db=_OwnerDB(_ADA),  # type: ignore[arg-type]
                user_id=str(_ADA.id),
                agent=_loop_agent(),  # type: ignore[arg-type]
                history=[],
                user_message="split the work",
                prebuilt_llm=(SimpleNamespace(provider_name="fake"), "fake-model"),
                evaluation_mode=True,
                # Held for approval, the spawn would not run.
                evaluation_tool_approval=False,
                eval_result_out={},
            )
            scopes.append(list(counted))
    return scopes


@pytest.mark.asyncio
async def test_an_evaluated_turns_spawns_are_counted_under_a_scope_of_its_own():
    first, second = await _spawn_scopes(turns=2)

    # Both spawns of a turn count against one scope: the cap bounds the turn.
    assert len(first) == 2 and first[0] == first[1]
    assert first[0]["parent_run_id"] is None and first[0]["conversation_id"] is None
    assert first[0]["cap_scope"]
    # Another turn, another scope: tasks a turn left running never count against the next.
    assert second[0]["cap_scope"] != first[0]["cap_scope"]
