"""A run's ledger: the model calls, tool times, retries and tokens it records.

Every model call a run makes records a generation span in the run's own trace; a
delegated task in an evaluation's chain writes its ledger on its task row, read
back from PostgreSQL; each ``tool_done`` of an evaluation run carries its own
tool's time; and the engine reads retries and token usage from what the run
captured, so a category nobody reported stays absent rather than zero.
"""

from __future__ import annotations

import ast
import asyncio
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.models  # noqa: F401 — registers every mapper on Base.metadata
from app.llm.base import BaseLLMProvider, ChatMessage, ChatResponse, _record_retry_event, count_llm_retries
from app.models.sub_agent_task import SubAgentTask
from app.services import evaluation_engine as ee
from app.services import langfuse_tracing as lft
from app.services.evaluation.metric_package import (
    RETRY_COUNT_KEY,
    PerformanceConfig,
    run_performance,
)
from app.services.evaluation.metric_package.results import MetricStatus
from app.services.evaluation_engine import (
    _recover_reused_retry_count,
    _resolve_agent_token_usage,
    _sum_agent_token_usage,
    _sum_local_token_usage,
)
from app.services.run_ledger import (
    EVALUATION_CHAIN_KEY,
    EVALUATION_TOOL_APPROVAL_KEY,
    LEDGER_KEY,
    assemble_chain,
    ledger_from_trace,
    with_task_ledger,
)
from app.tools import agent_loop as al
from app.tools.base import CallableToolExecutor, CompositeToolExecutor, ToolCall, ToolDefinition
from tests.agentic_gate_stub import stub_agentic_gate
from tests.scratch_database import ALL_TABLES, scratch_database

# ── Generation spans ──────────────────────────────────────────────────────────────────
# An entry's figures are counted from the spans each run records for its own trace.
# That is only complete if every model call opens a generation, and only
# attributable if a nested run's spans stay in its own trace. Covered elsewhere: the
# compaction summary (``test_compaction_summariser_usage.py``) and the forced final
# answer and its retry (``test_forced_answer_usage_once.py``) assert their own
# generation.


def _root_trace_ctx() -> dict:
    """A local-only root trace, as ``trace_llm_call`` builds one without Langfuse."""
    return {
        "trace": None,
        "generation": None,
        "client": None,
        "parent_ctx": None,
        "metadata": {"source": "test"},
        "input_data": {"messages": []},
        "local_spans": [],
        "local_span_counter": 0,
        "local_trace_id": uuid.uuid4(),
        "local_source": "agent_chat",
        "local_name": "trace",
        "local_started_at": datetime.now(UTC),
        "local_metadata": {"source": "test"},
        "local_tags": [],
    }


def _generations(root: dict) -> list[dict]:
    return [s for s in root["local_spans"] if s["type"] == "GENERATION"]


class _ScriptedLLM(BaseLLMProvider):
    """Answers from a script, one ``ChatResponse`` per ``chat()`` call."""

    provider_name = "stub"
    context_window = 100_000

    def __init__(self, script: list[ChatResponse]) -> None:
        self._script = list(script)
        self.calls = 0

    async def chat(self, messages, **kwargs):
        self.calls += 1
        return self._script.pop(0)

    async def chat_stream(self, messages, **kwargs):  # pragma: no cover - never streamed here
        raise NotImplementedError

    async def embed(self, texts, model=None):  # pragma: no cover - never used
        raise NotImplementedError

    async def list_models(self):  # pragma: no cover - never used
        return []


async def _run_loop(llm: BaseLLMProvider, root: dict) -> ChatResponse:
    token = lft.set_current_trace_ctx(root)
    try:
        return await al.run_agent_loop(
            llm=llm,
            messages=[ChatMessage(role="user", content="Summarise the incident.")],
            tools=None,
            executor=None,
            max_iterations=5,
            max_tokens=256,
            progressive_tools=False,
        )
    finally:
        lft.reset_current_trace_ctx(token)


def _usage(inp: int, out: int) -> dict:
    return {"input_tokens": inp, "output_tokens": out}


@pytest.mark.asyncio
async def test_a_continuation_after_a_length_cut_records_its_own_generation():
    """A ``max_tokens`` cut is continued by further calls; each is a generation."""
    llm = _ScriptedLLM(
        [
            ChatResponse(content="Part one, ", model="stub", stop_reason="max_tokens", usage=_usage(100, 256)),
            ChatResponse(content="part two, ", model="stub", stop_reason="max_tokens", usage=_usage(120, 256)),
            ChatResponse(content="and the end.", model="stub", stop_reason="end_turn", usage=_usage(140, 40)),
        ]
    )
    root = _root_trace_ctx()

    await _run_loop(llm, root)

    generations = _generations(root)
    assert llm.calls == 3
    assert len(generations) == llm.calls, [g["name"] for g in generations]
    assert sorted(g["output_tokens"] for g in generations) == [40, 256, 256]


@pytest.mark.asyncio
async def test_an_empty_response_rescue_records_its_own_generation():
    """The rescue call after an empty answer is a model call like any other."""
    llm = _ScriptedLLM(
        [
            ChatResponse(content="", model="stub", stop_reason="end_turn", usage=_usage(200, 1)),
            ChatResponse(content="Recovered answer.", model="stub", stop_reason="end_turn", usage=_usage(210, 30)),
        ]
    )
    root = _root_trace_ctx()

    await _run_loop(llm, root)

    generations = _generations(root)
    assert llm.calls == 2
    assert len(generations) == llm.calls, [g["name"] for g in generations]
    assert sorted(g["output_tokens"] for g in generations) == [1, 30]


# --- ledger_from_trace --------------------------------------------------------


_TASK_ID = "3f2a8c1e-9b4d-4e7a-8c2f-1d5e6a7b8c9d"


def _spawn_output(task_id: str) -> str:
    return f"Task ID: {task_id}\nTarget agent: Research Analyst\nStatus: pending\nCheck back later."


def _loop_tool_output(content: str) -> dict:
    """A tool span's output as the agent loop closes it (``agent_loop``, ``_lf_end_tool_span``)."""
    return {"is_error": False, "content": content, "files_count": 0}


def _generation(root: dict, *, usage: dict, model: str = "stub", **end) -> None:
    ctx = lft.start_llm_generation(root, model=model, messages=[])
    lft.end_generation(ctx, output="ok", usage=usage, model=model, **end)


def _tool(root: dict, name: str, *, output: str = "done", **end) -> None:
    ctx = lft.start_tool_span(tool_name=name, arguments={}, parent_ctx=root)
    lft.end_tool_span(ctx, output=output, **end)


def test_the_ledger_holds_each_generation_and_tool_call_of_the_run():
    root = _root_trace_ctx()
    _generation(root, usage=_usage(100, 20), model="gpt-a")
    _tool(root, "lookup")
    _generation(root, usage=_usage(130, 25), model="gpt-b")

    ledger = ledger_from_trace(root)

    assert [(c["kind"], c["name"]) for c in ledger["calls"]] == [
        ("model", "llm_call"),
        ("tool", "lookup"),
        ("model", "llm_call"),
    ]
    models = [c for c in ledger["calls"] if c["kind"] == "model"]
    assert [c["model"] for c in models] == ["gpt-a", "gpt-b"]
    assert all(c["usage"] for c in models)
    assert all(c["duration_ms"] >= 0 for c in ledger["calls"])
    assert "usage" not in ledger["calls"][1]


def test_spans_that_are_not_calls_are_left_out():
    root = _root_trace_ctx()
    iteration = lft.start_iteration_span(iteration=1, parent_ctx=root)
    assert iteration is not None
    _generation(iteration, usage=_usage(10, 2))
    lft.end_span(iteration)

    assert [c["kind"] for c in ledger_from_trace(root)["calls"]] == ["model"]


@pytest.mark.parametrize(
    ("end", "expected"),
    [
        ({}, "success"),
        ({"level": "WARNING"}, "success"),
        ({"error": RuntimeError("provider exploded")}, "error"),
        ({"error": TimeoutError("took too long")}, "timeout"),
        ({"error": asyncio.CancelledError()}, "cancelled"),
    ],
    ids=["ok", "truncated", "error", "timeout", "cancelled"],
)
def test_a_calls_outcome_is_mapped_onto_the_suites_outcomes(end, expected):
    root = _root_trace_ctx()
    _generation(root, usage=_usage(10, 2), **end)

    assert [c["outcome"] for c in ledger_from_trace(root)["calls"]] == [expected]


def test_a_failed_tool_call_is_an_error():
    root = _root_trace_ctx()
    _tool(root, "lookup", output="Error: not found", is_error=True)

    assert [c["outcome"] for c in ledger_from_trace(root)["calls"]] == ["error"]


def test_a_task_the_spawn_tool_recorded_is_in_the_ledger():
    """Recorded from under a tool span, as the spawn tool runs: the root is the run's trace."""
    from app.services.run_ledger import record_spawned_task

    root = _root_trace_ctx()
    tool_span = lft.start_tool_span(tool_name="spawn_background_task", arguments={}, parent_ctx=root)
    record_spawned_task(tool_span, _TASK_ID)
    record_spawned_task(tool_span, _TASK_ID)  # the same task, recorded twice

    assert ledger_from_trace(root)["spawned_task_ids"] == [_TASK_ID]


@pytest.mark.parametrize("name", ["spawn_background_task", "mcp_spawn_background_task", "lookup"])
def test_a_tool_output_that_names_a_task_adds_no_task(name):
    """Only what the spawn tool created counts: a look-alike tool's result is not read."""
    root = _root_trace_ctx()
    _tool(root, name, output=_loop_tool_output(_spawn_output(_TASK_ID)))

    assert ledger_from_trace(root)["spawned_task_ids"] == []


def _spawn_tools(*, evaluation_chain: bool):
    from app.services.builtin_tools.handover import build_sub_agent_tools

    target = SimpleNamespace(id=uuid.uuid4(), name="Research Analyst", slug="research-analyst", description="")
    defs, executor = build_sub_agent_tools(
        object(), str(uuid.uuid4()), str(uuid.uuid4()), [target], evaluation_chain=evaluation_chain
    )
    return executor, target, defs


@pytest.mark.parametrize("evaluation_chain", [True, False], ids=["in-a-chain", "outside"])
@pytest.mark.asyncio
async def test_the_spawn_tool_records_the_task_it_created_only_within_a_chain(evaluation_chain):
    executor, target, _defs = _spawn_tools(evaluation_chain=evaluation_chain)
    created = SimpleNamespace(id=uuid.UUID(_TASK_ID), status="pending")
    root = _root_trace_ctx()
    token = lft.set_current_trace_ctx(root)
    try:
        with (
            patch("app.services.sub_agent.count_active_sub_agent_executions", new=AsyncMock(return_value=0)),
            patch("app.services.sub_agent.create_sub_agent_task", new=AsyncMock(return_value=created)),
        ):
            result = await executor.execute_tool(
                ToolCall(id="c1", name="spawn_background_task", arguments={"agent_id": str(target.id), "task": "Look into it."})
            )
    finally:
        lft.reset_current_trace_ctx(token)

    assert "spawned successfully" in result.content
    assert ledger_from_trace(root)["spawned_task_ids"] == ([_TASK_ID] if evaluation_chain else [])


@pytest.mark.asyncio
async def test_a_task_spawned_through_the_real_loop_is_in_the_runs_ledger():
    """The loop makes each iteration's and tool's span current: the spawn still records on the run's trace."""
    executor, target, defs = _spawn_tools(evaluation_chain=True)
    llm = _ScriptedLLM(
        [
            ChatResponse(
                content="",
                model="stub",
                stop_reason="tool_use",
                tool_calls=[ToolCall(id="c1", name="spawn_background_task", arguments={"agent_id": str(target.id), "task": "Look into it."})],
                usage=_usage(80, 6),
            ),
            ChatResponse(content="Delegated; the analyst is on it.", model="stub", stop_reason="end_turn", usage=_usage(120, 12)),
        ]
    )
    root = _root_trace_ctx()
    token = lft.set_current_trace_ctx(root)
    try:
        with (
            patch("app.services.sub_agent.count_active_sub_agent_executions", new=AsyncMock(return_value=0)),
            patch(
                "app.services.sub_agent.create_sub_agent_task",
                new=AsyncMock(return_value=SimpleNamespace(id=uuid.UUID(_TASK_ID), status="pending")),
            ),
        ):
            await al.run_agent_loop(
                llm=llm,
                messages=[ChatMessage(role="user", content="Look into the incident.")],
                tools=defs,
                executor=executor,
                max_iterations=5,
                max_tokens=256,
                progressive_tools=False,
            )
    finally:
        lft.reset_current_trace_ctx(token)

    assert llm.calls == 2
    assert ledger_from_trace(root)["spawned_task_ids"] == [_TASK_ID]


@pytest.mark.asyncio
async def test_a_model_call_that_failed_in_the_real_loop_lowers_the_completion_rate(monkeypatch):
    """A failed call reaches the chain as a failure, and the performance suite counts it.

    The loop ends a timed-out call's generation with the error before it retries. The ledger
    reads that span as a ``timeout``, and the completion rate the entry is scored with counts
    one of its two model calls as not completed.
    """
    from app.services.evaluation.metric_package import ChainInputs

    class _TimesOutOnce(_ScriptedLLM):
        async def chat(self, messages, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise TimeoutError("the provider did not answer in time")
            return self._script.pop(0)

    llm = _TimesOutOnce(
        [ChatResponse(content="The incident is contained.", model="stub", stop_reason="end_turn", usage=_usage(120, 12))]
    )
    root = _root_trace_ctx()
    monkeypatch.setattr(al, "_LLM_TIMEOUT_RETRY_BACKOFF_SECS", 0.0)

    answer = await _run_loop(llm, root)

    assert answer.content == "The incident is contained."
    chain = assemble_chain(ledger_from_trace(root), {})
    assert [(c["kind"], c["outcome"]) for c in chain["calls"]] == [("model", "timeout"), ("model", "success")]

    outcomes = {
        o.key: o
        for o in run_performance(
            execution_details={"agent_wall_clock_ms": 1000},
            token_usage=None,
            retry_count=0,
            price_rows=[],
            config=PerformanceConfig(),
            chain=ChainInputs(calls=chain["calls"], token_usage_by_model=[], price_rows_by_model={}, complete=True),
        )
    }
    assert outcomes["performance.reliability.completion_rate"].value == 0.5


def test_a_nested_runs_calls_are_not_in_its_parents_ledger():
    parent = _root_trace_ctx()
    tool_span = lft.start_tool_span(tool_name="consult_agent", arguments={}, parent_ctx=parent)
    child = _root_trace_ctx() | {"parent_ctx": tool_span}
    _generation(child, usage=_usage(50, 5))
    lft.end_tool_span(tool_span, output="the helper's answer")

    assert [c["kind"] for c in ledger_from_trace(child)["calls"]] == ["model"]
    assert [(c["kind"], c["name"]) for c in ledger_from_trace(parent)["calls"]] == [("tool", "consult_agent")]


def test_a_run_with_no_trace_has_an_empty_ledger():
    assert ledger_from_trace(None) == {"calls": [], "spawned_task_ids": []}


# --- assemble_chain -----------------------------------------------------------


def _model_call(model: str, inp: int, out: int) -> dict:
    return {"kind": "model", "name": "llm_call", "model": model, "duration_ms": 1.0, "outcome": "success", "usage": {"input": inp, "output": out, "total": inp + out}}


def _tool_call(name: str) -> dict:
    return {"kind": "tool", "name": name, "duration_ms": 1.0, "outcome": "success"}


def test_the_chain_holds_the_agents_calls_then_each_tasks():
    own = {"calls": [_model_call("gpt-a", 100, 10), _tool_call("spawn_background_task")], "spawned_task_ids": ["t1"]}
    task = {"calls": [_model_call("gpt-a", 50, 5), _tool_call("lookup")], "retries": 2}

    chain = assemble_chain(own, {"t1": task}, entry_retries=1)

    assert [c["name"] for c in chain["calls"]] == ["llm_call", "spawn_background_task", "llm_call", "lookup"]
    assert chain["retries"] == 3
    assert chain["complete"] is True


def test_usage_is_summed_per_serving_model():
    own = {"calls": [_model_call("gpt-a", 100, 10), _model_call("gpt-b", 7, 3)]}
    task = {"calls": [_model_call("gpt-a", 50, 5)]}

    chain = assemble_chain(own, {"t1": task})

    assert {p["model"]: p["usage"] for p in chain["usage_by_model"]} == {
        "gpt-a": {"input": 150, "output": 15, "total": 165},
        "gpt-b": {"input": 7, "output": 3, "total": 10},
    }


@pytest.mark.parametrize(
    ("own", "tasks", "stop_reason", "reason"),
    [
        pytest.param(
            {"calls": [_model_call("gpt-a", 1, 1)]},
            {"t1": {"calls": []}, "t2": None},
            None,
            "ledger_missing",
            id="a_task_ledger_is_missing",
        ),
        # A run whose ledger could not be read did not make no call: its totals are unavailable.
        pytest.param(
            {"calls": [], "spawned_task_ids": [], "partial": True}, {}, None, "ledger_missing", id="a_partial_own_ledger"
        ),
        pytest.param({"calls": []}, {"t1": None}, "wait_bound_reached", "wait_bound_reached", id="the_waits_stop_reason"),
    ],
)
def test_a_chain_missing_a_ledger_is_incomplete_and_says_why(own, tasks, stop_reason, reason):
    chain = assemble_chain(own, tasks, stop_reason=stop_reason)

    assert (chain["complete"], chain["incomplete_reason"]) == (False, reason)


def test_an_entry_with_no_task_is_complete():
    chain = assemble_chain({"calls": []}, {})

    assert chain == {"calls": [], "usage_by_model": [], "retries": 0, "complete": True}


def test_a_helper_whose_ledger_could_not_be_read_makes_its_callers_partial():
    from app.services.run_ledger import record_helper_ledger

    root = _root_trace_ctx()
    record_helper_ledger(root, {"calls": [_model_call("gpt-a", 5, 1)], "spawned_task_ids": []})
    assert "partial" not in ledger_from_trace(root)

    record_helper_ledger(root, {"calls": [], "spawned_task_ids": [], "partial": True})
    assert ledger_from_trace(root)["partial"] is True


# ── A delegated task within an evaluation's chain records its ledger ──────────────────
# The task runs as its own orchestration execution, possibly on another worker, so
# the evaluation reads its work back from the task row: ``metadata_json["ledger"]``,
# written with the retry count in the same transaction as the terminal status. A task
# outside a chain writes nothing new, and the mark changes nothing about how it runs.


_LEDGER = {
    "calls": [{"kind": "model", "name": "llm_call", "model": "helper", "duration_ms": 4.0, "outcome": "success", "usage": {"input": 20, "output": 3}}],
    "spawned_task_ids": [],
}


@pytest.fixture
def _agentic_ai_available(monkeypatch):
    stub_agentic_gate(monkeypatch)


class _FakeSessionCM:
    def __init__(self, db) -> None:
        self._db = db

    async def __aenter__(self):
        return self._db

    async def __aexit__(self, *_exc):
        return False


def _scalar(obj) -> MagicMock:
    res = MagicMock()
    res.scalar_one_or_none = MagicMock(return_value=obj)
    return res


def _groups() -> MagicMock:
    res = MagicMock()
    res.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
    return res


def _task_and_agent(flagged: bool) -> tuple[MagicMock, MagicMock]:
    task = MagicMock()
    task.conversation_id = None
    task.metadata_json = {EVALUATION_CHAIN_KEY: True} if flagged else {}
    agent = MagicMock()
    agent.handover_agent_ids = []
    agent.disable_chat = True
    agent.max_iterations = 25
    return task, agent


def _db(task, agent) -> MagicMock:
    """*agent* ``None``: the target agent is not found (or not accessible)."""
    db = MagicMock()
    # task, group ids, agent, then the task again for every later re-read.
    db.execute = AsyncMock(side_effect=[_scalar(task), _groups(), _scalar(agent)] + [_scalar(task)] * 4)
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    return db


async def _execute(task, agent, run):
    from app.services.sub_agent import _execute_sub_agent_task

    seen: dict = {}

    async def _capturing(*args, **kwargs):
        seen.update(kwargs)
        return await run(*args, **kwargs)

    with (
        patch("app.core.database.async_session", lambda: _FakeSessionCM(_db(task, agent))),
        patch("app.services.agent_chat.run_agent_chat", new=AsyncMock(side_effect=_capturing)),
        patch("app.services.sub_agent._update_task_delegation", new=AsyncMock()),
        patch("app.services.sub_agent_files.write_task_result_file_ids", new=AsyncMock(return_value=True)),
        patch("app.services.sub_agent.publish_task_event", new=AsyncMock(), create=True),
        patch("app.services.sub_agent._schedule_parent_wake_if_needed", new=MagicMock()),
    ):
        outcome = await _execute_sub_agent_task(
            task_id=str(uuid.uuid4()),
            user_id=str(uuid.uuid4()),
            target_agent_id=str(uuid.uuid4()),
            task_description="research the incident",
        )
    return outcome, seen


async def _answers_after_one_retry(*_args, **kwargs):
    _record_retry_event("retries")  # what retry_llm_call does on a back-off
    kwargs["loop_outcome_out"][LEDGER_KEY] = _LEDGER
    return "the findings", None


@pytest.mark.usefixtures("_agentic_ai_available")
@pytest.mark.asyncio
async def test_a_task_in_an_evaluations_chain_records_its_ledger_and_retries():
    task, agent = _task_and_agent(flagged=True)

    outcome, seen = await _execute(task, agent, _answers_after_one_retry)

    assert outcome.status == "completed"
    assert seen["evaluation_chain"] is True
    assert task.metadata_json[LEDGER_KEY] == {**_LEDGER, "retries": 1}


@pytest.mark.usefixtures("_agentic_ai_available")
@pytest.mark.asyncio
async def test_a_task_outside_a_chain_writes_nothing_new():
    task, agent = _task_and_agent(flagged=False)

    outcome, seen = await _execute(task, agent, _answers_after_one_retry)

    assert outcome.status == "completed"
    assert seen["evaluation_chain"] is False
    assert LEDGER_KEY not in task.metadata_json


@pytest.mark.usefixtures("_agentic_ai_available")
@pytest.mark.asyncio
async def test_a_task_whose_run_raised_still_records_what_it_did():
    """``failed`` is a terminal status too; the calls before the failure were made."""
    task, agent = _task_and_agent(flagged=True)

    async def _fails_part_way(*_args, **kwargs):
        kwargs["loop_outcome_out"][LEDGER_KEY] = _LEDGER
        raise RuntimeError("the delegated agent crashed")

    outcome, _seen = await _execute(task, agent, _fails_part_way)

    assert outcome.status == "failed"
    assert task.metadata_json[LEDGER_KEY]["calls"] == _LEDGER["calls"]


@pytest.mark.usefixtures("_agentic_ai_available")
@pytest.mark.asyncio
async def test_the_chain_mark_changes_nothing_the_task_runs():
    """The same call apart from the mark itself and the per-run objects."""
    _flagged_outcome, flagged = await _execute(*_task_and_agent(flagged=True), _answers_after_one_retry)
    _plain_outcome, plain = await _execute(*_task_and_agent(flagged=False), _answers_after_one_retry)

    # Built fresh for each run, or derived from its task id.
    per_run = {
        "evaluation_chain",
        "loop_outcome_out",
        "file_context",
        "wall_clock_deadline",
        "error_out",
        "messages_out",
        "quota_receipt_token",
    }
    assert flagged.keys() == plain.keys()
    for key in flagged.keys() - per_run:
        if not callable(flagged[key]):
            assert flagged[key] == plain[key], key


# --- Every terminal exit of a chain task leaves a ledger --------------

_NO_TURN = {"calls": [], "spawned_task_ids": [], "retries": 0}


@pytest.mark.usefixtures("_agentic_ai_available")
@pytest.mark.asyncio
async def test_a_task_killed_while_it_ran_keeps_its_ledger_and_its_cancelled_status():
    task, agent = _task_and_agent(flagged=True)

    async def _killed_mid_run(*args, **kwargs):
        task.status = "cancelled"  # the Kill action, from another session
        return await _answers_after_one_retry(*args, **kwargs)

    outcome, _seen = await _execute(task, agent, _killed_mid_run)

    assert outcome.status == "cancelled"
    assert task.status == "cancelled"
    assert task.metadata_json[LEDGER_KEY] == {**_LEDGER, "retries": 1}


@pytest.mark.usefixtures("_agentic_ai_available")
@pytest.mark.parametrize("flagged", [True, False], ids=["in-a-chain", "outside"])
@pytest.mark.asyncio
async def test_a_task_whose_target_is_gone_ends_with_an_empty_ledger(flagged):
    task, _agent = _task_and_agent(flagged=flagged)

    async def _never_runs(*_args, **_kwargs):
        raise AssertionError("no turn runs without its target agent")

    outcome, _seen = await _execute(task, None, _never_runs)

    assert outcome.status == "failed"
    if flagged:
        assert task.metadata_json[LEDGER_KEY] == _NO_TURN
    else:
        assert LEDGER_KEY not in task.metadata_json


@pytest.mark.usefixtures("_agentic_ai_available")
@pytest.mark.parametrize("flagged", [True, False], ids=["in-a-chain", "outside"])
@pytest.mark.asyncio
async def test_a_task_failed_before_its_turn_ends_with_an_empty_ledger(flagged):
    """Refused (AI unavailable past its hold) or out of time: the same exit."""
    from app.services import sub_agent

    task = SimpleNamespace(
        status="running",
        error=None,
        completed_at=None,
        conversation_id=None,
        metadata_json={EVALUATION_CHAIN_KEY: True} if flagged else {},
    )
    db = MagicMock(commit=AsyncMock())
    with (
        patch.object(sub_agent, "_update_task_delegation", new=AsyncMock()),
        patch.object(sub_agent, "publish_task_event", new=AsyncMock()),
        patch.object(sub_agent, "_schedule_parent_wake_if_needed", new=MagicMock()),
    ):
        outcome = await sub_agent._fail_task_before_turn(
            db,
            task,  # type: ignore[arg-type]
            task_id=str(uuid.uuid4()),
            user_id=str(uuid.uuid4()),
            assignment_run_id=None,
            error_msg="out of time",
            agent_name="Worker",
            failure_cause=None,
        )

    assert outcome.status == "failed"
    assert (task.metadata_json.get(LEDGER_KEY)) == (_NO_TURN if flagged else None)


@pytest.mark.usefixtures("_agentic_ai_available")
@pytest.mark.parametrize(
    ("status", "started_at", "expected"),
    [("pending", None, _NO_TURN), ("running", "now", None)],
    ids=["never-started", "running"],
)
@pytest.mark.asyncio
async def test_a_kill_records_an_empty_ledger_only_for_a_task_that_never_started(status, started_at, expected):
    """A running task's worker keeps its run's ledger (the test above), so the kill writes none."""
    from app.services import orchestration_engine, sub_agent

    owner = uuid.uuid4()
    task = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=owner,
        conversation_id=None,
        status=status,
        error=None,
        completed_at=None,
        started_at=started_at,
        metadata_json={EVALUATION_CHAIN_KEY: True},
    )
    db = MagicMock(execute=AsyncMock(return_value=_scalar(task)), commit=AsyncMock())
    with (
        patch("app.core.database.async_session", lambda: _FakeSessionCM(db)),
        patch.object(orchestration_engine, "get_execution_by_dedup_key", new=AsyncMock(return_value=None)),
        patch.object(sub_agent, "_update_task_delegation", new=AsyncMock()),
        patch.object(sub_agent, "publish_task_event", new=AsyncMock()),
    ):
        result = await sub_agent.cancel_sub_agent_task(str(task.id), str(owner))

    assert result is not None
    assert task.status == "cancelled"
    assert task.metadata_json.get(LEDGER_KEY) == expected


# --- An incomplete task, a re-run -------------------------------------


async def _parks_with_nothing_pending(*_args, **kwargs):
    """Ends ``waiting_on_tasks`` with no task left to wait on: no continuation to spawn."""
    kwargs["loop_outcome_out"][LEDGER_KEY] = _LEDGER
    kwargs["loop_outcome_out"]["stop_reason"] = "waiting_on_tasks"
    kwargs["loop_outcome_out"]["pending_task_ids"] = []
    return "still waiting", None


@pytest.mark.usefixtures("_agentic_ai_available")
@pytest.mark.parametrize("flagged", [True, False], ids=["in-a-chain", "outside"])
@pytest.mark.asyncio
async def test_an_incomplete_task_with_no_continuation_says_so(flagged):
    from app.services.run_ledger import NO_CONTINUATION_KEY

    task, agent = _task_and_agent(flagged=flagged)
    task.metadata_json = {**task.metadata_json, "wait_continuation_depth": 0}

    outcome, _seen = await _execute(task, agent, _parks_with_nothing_pending)

    assert outcome.status == "completed"  # the worker's outcome; the row reads ``incomplete``
    assert task.status == "incomplete"
    assert task.metadata_json.get(NO_CONTINUATION_KEY) is (True if flagged else None)


@pytest.mark.usefixtures("_agentic_ai_available")
@pytest.mark.asyncio
async def test_a_rerun_of_a_task_a_lost_attempt_started_records_a_partial_ledger():
    """The lost attempt's calls are in no ledger: the chain must not read this one as whole."""
    task, agent = _task_and_agent(flagged=True)
    task.status = "running"  # left by the attempt the engine lost

    outcome, _seen = await _execute(task, agent, _answers_after_one_retry)

    assert outcome.status == "completed"
    assert task.metadata_json[LEDGER_KEY]["partial"] is True


@pytest.mark.usefixtures("_agentic_ai_available")
@pytest.mark.asyncio
async def test_a_first_attempt_records_a_whole_ledger():
    task, agent = _task_and_agent(flagged=True)
    task.status = "pending"

    await _execute(task, agent, _answers_after_one_retry)

    assert "partial" not in task.metadata_json[LEDGER_KEY]


async def _parks_on_a_live_child(*_args, **kwargs):
    """Ends ``waiting_on_tasks`` with a task still running: a continuation waits for it."""
    kwargs["loop_outcome_out"][LEDGER_KEY] = _LEDGER
    kwargs["loop_outcome_out"]["stop_reason"] = "waiting_on_tasks"
    kwargs["loop_outcome_out"]["pending_task_ids"] = [str(uuid.uuid4())]
    return "still waiting", None


@pytest.mark.usefixtures("_agentic_ai_available")
@pytest.mark.asyncio
async def test_the_continuation_of_an_evaluated_turns_task_stays_under_its_cap_scope():
    """An evaluated turn's task has neither a parent run nor a conversation: its
    continuation is counted under the turn's ``cap_scope``, or the cap
    would lose it."""
    task, agent = _task_and_agent(flagged=True)
    task.metadata_json = {**task.metadata_json, "cap_scope": "evaluation_turn:t-1", "wait_continuation_depth": 0}

    create = AsyncMock(return_value=SimpleNamespace(id=uuid.uuid4()))
    with patch("app.services.sub_agent.create_sub_agent_task", new=create):
        await _execute(task, agent, _parks_on_a_live_child)

    assert task.status == "incomplete"
    assert create.await_args is not None
    assert create.await_args.kwargs["cap_scope"] == "evaluation_turn:t-1"


# --- The evaluation's tool approval setting rides on the task ---------


@pytest.mark.usefixtures("_agentic_ai_available")
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("metadata", "passed"),
    [
        ({EVALUATION_CHAIN_KEY: True, EVALUATION_TOOL_APPROVAL_KEY: False}, False),
        ({EVALUATION_CHAIN_KEY: True, EVALUATION_TOOL_APPROVAL_KEY: True}, True),
        # Spawned before the setting, or outside an evaluation: the gate as configured.
        ({EVALUATION_CHAIN_KEY: True}, None),
        ({}, None),
    ],
    ids=["off", "on", "chain-without-setting", "outside"],
)
async def test_a_task_runs_with_the_tool_approval_setting_it_was_spawned_with(metadata, passed):
    task, agent = _task_and_agent(flagged=False)
    task.metadata_json = dict(metadata)

    outcome, seen = await _execute(task, agent, _answers_after_one_retry)

    assert outcome.status == "completed"
    assert seen["evaluation_tool_approval"] is passed


@pytest.mark.usefixtures("_agentic_ai_available")
@pytest.mark.asyncio
@pytest.mark.parametrize("setting", [False, True])
async def test_a_wait_budget_continuation_keeps_the_tool_approval_setting(setting):
    from app.services.sub_agent import _spawn_wait_budget_continuation

    create = AsyncMock(return_value=SimpleNamespace(id=uuid.uuid4()))
    with patch("app.services.sub_agent.create_sub_agent_task", new=create):
        await _spawn_wait_budget_continuation(
            user_id=str(uuid.uuid4()),
            parent_agent_id=str(uuid.uuid4()),
            target_agent_id=str(uuid.uuid4()),
            original_task="work",
            pending_ids=[str(uuid.uuid4())],
            conversation_id=None,
            parent_run_id=None,
            visited_agents=[],
            timeout_seconds=None,
            max_iterations=None,
            continuation_of=str(uuid.uuid4()),
            continuation_depth=1,
            scope_headers=None,
            file_context=None,
            db=MagicMock(),
            evaluation_chain=True,
            evaluation_tool_approval=setting,
        )
    metadata = create.await_args.kwargs["extra_metadata"]
    assert (metadata[EVALUATION_CHAIN_KEY], metadata[EVALUATION_TOOL_APPROVAL_KEY]) == (True, setting)


@pytest.mark.usefixtures("_agentic_ai_available")
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("chain", "setting", "stored"),
    [
        (True, False, {EVALUATION_CHAIN_KEY: True, EVALUATION_TOOL_APPROVAL_KEY: False}),
        (True, True, {EVALUATION_CHAIN_KEY: True, EVALUATION_TOOL_APPROVAL_KEY: True}),
        (False, None, None),
    ],
    ids=["evaluation-off", "evaluation-on", "chat"],
)
async def test_the_spawn_tool_stores_the_tool_approval_setting_on_the_task(chain, setting, stored):
    from app.services.builtin_tools import handover

    peer = SimpleNamespace(
        id=uuid.uuid4(), name="Peer", slug="peer", description="", persona="", is_company_managed=False
    )
    create = AsyncMock(return_value=SimpleNamespace(id=uuid.uuid4(), status="pending"))
    built = handover.build_sub_agent_tools(
        MagicMock(),
        user_id=str(uuid.uuid4()),
        parent_agent_id=str(uuid.uuid4()),
        handover_agents=[peer],
        conversation_id=str(uuid.uuid4()),
        parent_run_id=str(uuid.uuid4()),
        evaluation_chain=chain,
        evaluation_tool_approval=setting,
    )
    with (
        patch("app.services.sub_agent.create_sub_agent_task", new=create),
        # The cap pre-check opens a session of its own; nothing is running.
        patch("app.services.sub_agent.count_active_sub_agent_executions", new=AsyncMock(return_value=0)),
    ):
        answer = await built[1]._handlers["spawn_background_task"](agent_id=str(peer.id), task="work")

    assert create.await_args is not None, answer
    assert create.await_args.kwargs["extra_metadata"] == stored


# ── Delegated tasks' ledgers read back from PostgreSQL ────────────────────────────────
# The entry's chain is assembled from task rows: the ledger each task wrote with its
# terminal status, the tasks it spawned, and the continuation an ``incomplete`` task
# spawned, found by a JSON lookup on ``metadata_json`` that only a real database can
# check.


@asynccontextmanager
async def _temp_database() -> AsyncIterator[async_sessionmaker]:
    async with scratch_database("ledger", tables=ALL_TABLES, extensions=("vector",)) as db:
        yield db.sessions


async def _seed_user(session):
    from app.models.agent import Agent
    from app.models.user import User

    user = User(
        id=uuid.uuid4(),
        email=f"ledger-{uuid.uuid4().hex[:8]}@test.local",
        full_name="Ledger Test",
        hashed_password="x",
    )
    session.add(user)
    await session.flush()
    agent = Agent(id=uuid.uuid4(), name="Worker", user_id=user.id)
    session.add(agent)
    await session.flush()
    user.agent_id = agent.id
    return user


def _outcome(output_tokens: int, spawned: list[str] | None = None) -> dict:
    return {
        "ledger": {
            "calls": [{"kind": "model", "name": "llm_call", "model": "gpt-a", "duration_ms": 5.0, "outcome": "success", "usage": {"input": 10, "output": output_tokens}}],
            "spawned_task_ids": list(spawned or []),
        }
    }


async def _task(session, user, *, status: str, metadata: dict) -> SubAgentTask:
    row = SubAgentTask(
        id=uuid.uuid4(),
        user_id=user.id,
        parent_agent_id=user.agent_id,
        target_agent_id=user.agent_id,
        task_description="delegated work",
        status=status,
        metadata_json=metadata,
    )
    session.add(row)
    await session.flush()
    return row


@pytest.mark.integration
async def test_the_wait_reads_each_tasks_ledger_its_continuation_and_its_spawns():
    async with _temp_database() as sessionmaker, sessionmaker() as session:
        user = await _seed_user(session)
        nested = await _task(
            session, user, status="completed",
            metadata=with_task_ledger({EVALUATION_CHAIN_KEY: True}, _outcome(3), 0),
        )
        first = await _task(
            session, user, status="incomplete",
            metadata=with_task_ledger({EVALUATION_CHAIN_KEY: True}, _outcome(1, [str(nested.id)]), 2),
        )
        continuation = await _task(
            session, user, status="completed",
            metadata=with_task_ledger(
                {EVALUATION_CHAIN_KEY: True, "wait_continuation_of": str(first.id), "wait_continuation_depth": 1},
                _outcome(2),
                0,
            ),
        )
        await session.commit()

        ledgers = await ee._collect_task_ledgers([str(first.id)], bound_seconds=5.0)

    assert set(ledgers) == {str(first.id), str(nested.id), str(continuation.id)}
    assert all(ledger is not None for ledger in ledgers.values())
    assert ledgers[str(first.id)]["retries"] == 2


@pytest.mark.integration
async def test_a_task_still_running_at_the_bound_is_reported_missing():
    async with _temp_database() as sessionmaker, sessionmaker() as session:
        user = await _seed_user(session)
        running = await _task(session, user, status="running", metadata={EVALUATION_CHAIN_KEY: True})
        await session.commit()

        ledgers = await ee._collect_task_ledgers([str(running.id)], bound_seconds=0.0)

    assert ledgers == {str(running.id): None}


@pytest.mark.integration
async def test_a_task_outside_an_evaluations_chain_is_not_read():
    """A task id is read off a tool's output: a task the chain did not spawn is missing, not waited on."""
    async with _temp_database() as sessionmaker, sessionmaker() as session:
        user = await _seed_user(session)
        foreign = await _task(session, user, status="running", metadata={})
        await session.commit()

        ledgers = await ee._collect_task_ledgers([str(foreign.id)], bound_seconds=60.0)

    assert ledgers == {str(foreign.id): None}


# ── Each ``tool_done`` carries its own tool's time ────────────────────────────────────
# The loop sends one ``tool_start`` per batch, so timing tools from events gives the
# first ``tool_done`` of a batch the batch's time and the others 0 ms. The loop
# measures each call itself and marks the calls that ran together, so a step total
# counts them once. Only a run that asks (``report_tool_durations``, the evaluation)
# gets the keys; every other run's events are unchanged.


_NAP_SCHEMA = {
    "type": "object",
    "properties": {"ms": {"type": "integer"}},
    "required": ["ms"],
}


class _ScriptedLLMDurations(BaseLLMProvider):
    """Asks for one batch of tool calls, then answers."""

    provider_name = "stub"
    context_window = 100_000

    def __init__(self, calls: list[ToolCall]) -> None:
        self._calls = calls
        self._turn = 0

    async def chat(self, messages, model=None, tools=None, **kwargs):
        self._turn += 1
        if self._turn == 1:
            return ChatResponse(content="", model="stub", stop_reason="tool_calls", tool_calls=self._calls)
        return ChatResponse(content="done", model="stub")

    async def chat_stream(self, messages, **kwargs):
        yield ""

    async def embed(self, text, model=None):
        return [0.0]

    async def list_models(self):
        return []


@pytest.fixture
def _quiet_langfuse(monkeypatch):
    monkeypatch.setattr(al, "_lf_start_llm_gen", lambda *a, **kw: None)
    monkeypatch.setattr(al, "_lf_end_generation", lambda *a, **kw: None)
    monkeypatch.setattr(al, "_lf_end_span", lambda *a, **kw: None)
    monkeypatch.setattr(al, "_lf_end_tool_span", lambda *a, **kw: None)
    monkeypatch.setattr(al, "_lf_start_iter_span", lambda **kw: None)
    monkeypatch.setattr(al, "_lf_start_tool_span", lambda **kw: None)


def _nap(call_id: str, **arguments) -> ToolCall:
    return ToolCall(id=call_id, name="get_record", arguments=arguments)


async def _tool_done_events(calls: list[ToolCall], *, report_tool_durations: bool = True) -> list[dict]:
    async def _nap_tool(ms: int) -> str:
        await asyncio.sleep(ms / 1000)
        return f"slept {ms} ms"

    callable_exec = CallableToolExecutor()
    callable_exec.register("get_record", _nap_tool)
    composite = CompositeToolExecutor()
    composite.add_tools([ToolDefinition(name="get_record", description="Read a record, slowly", input_schema=_NAP_SCHEMA)], callable_exec)
    events: list[dict] = []

    async def _on_event(event: dict) -> None:
        events.append(event)

    response = await al.run_agent_loop(
        llm=_ScriptedLLMDurations(calls),
        messages=[ChatMessage(role="user", content="go")],
        tools=composite.all_tools,
        executor=composite,
        max_iterations=4,
        progressive_tools=False,
        on_event=_on_event,
        report_tool_durations=report_tool_durations,
    )
    assert response.content == "done"
    return [e for e in events if e.get("status") == "tool_done"]


@pytest.mark.asyncio
async def test_a_tool_left_alone_after_a_rejection_records_its_own_time(_quiet_langfuse):
    """A rejection answered first does not take the batch's time from the tool that ran."""
    rejected, ran = await _tool_done_events([_nap("bad"), _nap("good", ms=60)])

    assert (rejected["success"], rejected["duration_ms"]) == (False, 0)
    assert ran["success"] is True
    assert ran["duration_ms"] >= 40
    assert ran.get("parallel_group") is None


@pytest.mark.asyncio
async def test_tools_run_together_record_their_own_times_and_share_a_group(_quiet_langfuse):
    """Run together: each records its own time, and the group says they overlapped."""
    short, long = await _tool_done_events([_nap("short", ms=30), _nap("long", ms=300)])

    assert 25 <= short["duration_ms"] < long["duration_ms"]
    assert long["duration_ms"] >= 280
    assert short["parallel_group"] and short["parallel_group"] == long["parallel_group"]


@pytest.mark.asyncio
async def test_a_batch_with_one_call_left_to_run_has_no_group(_quiet_langfuse):
    """An identical read mirrors its twin without running: the twin ran alone."""
    primary, mirror = await _tool_done_events([_nap("a", ms=50), _nap("b", ms=50)])

    assert primary["duration_ms"] >= 40 and primary.get("parallel_group") is None
    assert mirror["cached"] is True
    assert (mirror["duration_ms"], mirror.get("parallel_group")) == (0, None)


@pytest.mark.asyncio
async def test_a_run_that_does_not_ask_gets_its_events_as_before(_quiet_langfuse):
    """Chat, assignments and every other run: the events their clients read are unchanged."""
    events = await _tool_done_events(
        [_nap("bad"), _nap("short", ms=10), _nap("long", ms=20)], report_tool_durations=False
    )

    assert len(events) == 3
    assert all("duration_ms" not in e and "parallel_group" not in e for e in events)


def test_every_tool_done_the_loop_sends_carries_its_tool_time():
    """A ``tool_done`` site added without the timing would report 0 ms on evaluation runs."""
    tree = ast.parse(Path(al.__file__).read_text(encoding="utf-8"))
    sites = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Dict)
        and any(
            isinstance(key, ast.Constant) and key.value == "status" and isinstance(value, ast.Constant) and value.value == "tool_done"
            for key, value in zip(node.keys, node.values, strict=True)
        )
    ]

    def _timed(site: ast.Dict) -> bool:
        return any(
            key is None
            and isinstance(value, ast.Call)
            and getattr(value.func, "id", None) == "_tool_timing"
            and isinstance(value.args[0], ast.Name)
            and value.args[0].id == "report_tool_durations"
            for key, value in zip(site.keys, site.values, strict=True)
        )

    assert len(sites) >= 13, "the scan no longer finds the loop's tool_done events"
    assert [site.lineno for site in sites if not _timed(site)] == []


# ── Telemetry capture: reliability, retries, token categories ─────────────────────────


# What a run captures decides what the package can measure. A category nobody
# reported stays absent, so "not observed" never renders as a measured zero.


_COMPLETION_RATE = "performance.reliability.completion_rate"
_TOOL_ERROR_RATE = "performance.reliability.tool_error_rate"
_TOKEN_TOTAL = "performance.tokens.total"


def _clean_run_traces() -> list[dict]:
    """The timeline a completed entry with no failures captures."""
    return [
        {
            "type": "llm",
            "description": "LLM call #1 — agent requests tool: search",
            "duration_ms": 900,
            "outcome": "success",
        },
        {
            "type": "tool",
            "description": "Tool call: search",
            "duration_ms": 120,
            "tool_name": "search",
            "outcome": "success",
        },
        {
            "type": "llm",
            "description": "LLM call #2 — agent produces final answer",
            "duration_ms": 700,
            "outcome": "success",
        },
    ]


def _perf(traces, *, token_usage=None, retry_count=0):
    outcomes = run_performance(
        execution_details={
            "agent_wall_clock_ms": 1720,
            "ttft_ms": 310,
            "traces": traces,
        },
        token_usage=token_usage,
        retry_count=retry_count,
        price_rows=[],
        config=PerformanceConfig(),
    )
    return {outcome.key: outcome for outcome in outcomes}


def test_a_clean_run_reports_measured_reliability_rather_than_unavailable():
    by_key = _perf(_clean_run_traces())

    assert by_key[_COMPLETION_RATE].status is MetricStatus.SCORED
    assert by_key[_COMPLETION_RATE].value == 1.0
    assert by_key[_TOOL_ERROR_RATE].status is MetricStatus.SCORED
    assert by_key[_TOOL_ERROR_RATE].value == 0.0


def test_a_failed_tool_call_moves_the_rates_off_a_perfect_score():
    traces = _clean_run_traces()
    traces[1]["outcome"] = "error"

    by_key = _perf(traces)

    assert by_key[_COMPLETION_RATE].value == pytest.approx(2 / 3)
    assert by_key[_TOOL_ERROR_RATE].value == 1.0


def test_one_call_without_a_recorded_outcome_makes_every_rate_unavailable():
    """An unobserved call must not be quietly excluded from the denominator."""
    traces = _clean_run_traces()
    del traces[1]["outcome"]

    by_key = _perf(traces)

    assert by_key[_COMPLETION_RATE].status is MetricStatus.UNAVAILABLE
    assert by_key[_COMPLETION_RATE].value is None
    assert by_key[_TOOL_ERROR_RATE].value is None


def test_retries_are_reported_as_a_measured_count_including_zero():
    assert _perf(_clean_run_traces(), retry_count=0)[RETRY_COUNT_KEY].value == 0.0
    assert _perf(_clean_run_traces(), retry_count=3)[RETRY_COUNT_KEY].value == 3.0


def test_an_uncaptured_retry_count_is_unavailable_not_zero():
    outcome = _perf(_clean_run_traces(), retry_count=None)[RETRY_COUNT_KEY]

    assert outcome.status is MetricStatus.UNAVAILABLE
    assert outcome.value is None


def test_the_retry_meter_counts_only_retries_that_actually_happened():
    async def scenario():
        with count_llm_retries() as meter:
            assert meter == {"retries": 0}

            calls = {"n": 0}

            async def flaky():
                calls["n"] += 1
                if calls["n"] < 3:
                    raise RuntimeError("transient")
                return "ok"

            from app.llm.base import retry_llm_call

            result = await retry_llm_call(
                flaky,
                is_retryable=lambda _exc: True,
                base_delay=0.0,
                max_delay=0.0,
            )
            return result, dict(meter)

    result, meter = asyncio.run(scenario())
    assert result == "ok"
    assert meter == {"retries": 2}


def test_the_retry_meter_counts_the_retries_of_a_call_that_exhausted_them():
    async def scenario():
        with count_llm_retries() as meter:

            async def always_fails():
                raise RuntimeError("transient")

            from app.llm.base import retry_llm_call

            with pytest.raises(RuntimeError):
                await retry_llm_call(
                    always_fails,
                    is_retryable=lambda _exc: True,
                    max_retries=2,
                    base_delay=0.0,
                    max_delay=0.0,
                )
            return dict(meter)

    assert asyncio.run(scenario()) == {"retries": 2}


def test_a_reused_prediction_carries_the_source_runs_retry_count():
    details = {"metric_results": {RETRY_COUNT_KEY: {"status": "scored", "value": 4}}}
    assert _recover_reused_retry_count(details) == 4


@pytest.mark.parametrize(
    "details",
    [
        None,
        {},
        {"metric_results": {}},
        {"metric_results": {RETRY_COUNT_KEY: {"status": "unavailable", "value": None}}},
        {"metric_results": {RETRY_COUNT_KEY: {"value": -1}}},
        {"metric_results": {RETRY_COUNT_KEY: {"value": True}}},
    ],
)
def test_a_reused_prediction_with_no_recorded_retry_count_stays_uncaptured(details):
    """Better an unavailable retry metric than a zero the source never measured."""
    assert _recover_reused_retry_count(details) is None


# --- Token categories ------------------------------------------------------


class _FakeRow:
    def __init__(self, rows):
        self._rows = rows

    def first(self):
        return self._rows[0] if self._rows else None

    def all(self):
        return self._rows


class _FakeDB:
    """Answers the two queries ``_sum_agent_token_usage`` issues, in order."""

    def __init__(self, totals, span_details):
        self._answers = [_FakeRow([totals]), _FakeRow([(d,) for d in span_details])]

    async def execute(self, *_args, **_kwargs):
        return self._answers.pop(0)


def _usage_from_db(totals, span_details):
    return asyncio.run(
        _sum_agent_token_usage(_FakeDB(totals, span_details), [str(uuid.uuid4())])
    )


def test_an_unreported_token_category_is_absent_rather_than_zero():
    usage = _usage_from_db((1200, 300), [{"input": 1200, "output": 300}])

    assert usage["input_tokens"] == 1200
    assert usage["output_tokens"] == 300
    for absent in (
        "cache_read_input_tokens",
        "cache_write_input_tokens",
        "reasoning_output_tokens",
        "convention",
    ):
        assert absent not in usage


def test_anthropic_style_cache_buckets_are_summed_as_inclusive():
    usage = _usage_from_db(
        (1200, 300),
        [
            {"input": 800, "cache_read_input_tokens": 500},
            {"input": 400, "cache_creation_input_tokens": 100},
        ],
    )

    assert usage["cache_read_input_tokens"] == 500
    assert usage["cache_write_input_tokens"] == 100
    assert usage["convention"] == "inclusive"


def test_openai_style_cached_tokens_are_reported_under_the_separate_convention():
    """OpenAI's cached tokens are subtracted out of the input figure, not added in."""
    usage = _usage_from_db((900, 200), [{"input": 900, "input_cached_tokens": 350}])

    assert usage["cache_read_input_tokens"] == 350
    assert usage["convention"] == "separate"


def test_mixed_conventions_drop_the_cache_buckets_instead_of_guessing():
    usage = _usage_from_db(
        (900, 200),
        [
            {"input": 500, "cache_read_input_tokens": 100},
            {"input": 400, "input_cached_tokens": 50},
        ],
    )

    assert "cache_read_input_tokens" not in usage
    assert "cache_write_input_tokens" not in usage
    assert "convention" not in usage


def test_reasoning_tokens_survive_a_mixed_convention_because_they_are_convention_free():
    usage = _usage_from_db(
        (900, 200),
        [
            {"input": 500, "cache_read_input_tokens": 100},
            {"input": 400, "input_cached_tokens": 50, "output_reasoning_tokens": 64},
        ],
    )

    assert usage["reasoning_output_tokens"] == 64


def test_no_recorded_tokens_at_all_yields_no_usage_rather_than_zeroes():
    assert _usage_from_db((0, 0), []) is None


def test_the_token_total_reflects_the_captured_categories():
    by_key = _perf(
        _clean_run_traces(),
        token_usage={
            "input_tokens": 1200,
            "output_tokens": 300,
            "cache_read_input_tokens": 500,
            "convention": "inclusive",
        },
    )

    # Inclusive: the 500 cached tokens are already inside the 1200 input figure,
    # so the total is 1200 + 300 and not 2000.
    assert by_key[_TOKEN_TOTAL].value == 1500.0


def test_the_default_performance_suite_composes_from_measured_signals():
    """The full-suite default must be mostly measurable, not mostly unavailable.

    Not marked ``integration``: that marker means "requires external services",
    and this test needs none.

    With reliability, retries and tokens captured, the "inherit all" performance
    selection builds its headline from real measurements. Only cost may stay
    unavailable: no price row is configured.
    """
    by_key = _perf(
        _clean_run_traces(),
        retry_count=1,
        token_usage={
            "input_tokens": 1200,
            "output_tokens": 300,
            "cache_read_input_tokens": 500,
            "reasoning_output_tokens": 64,
            "convention": "inclusive",
        },
    )

    scored = {k for k, o in by_key.items() if o.status is MetricStatus.SCORED}
    unmeasured = {
        k
        for k, o in by_key.items()
        if o.status in (MetricStatus.UNAVAILABLE, MetricStatus.SKIPPED)
    }

    assert {
        _COMPLETION_RATE,
        _TOOL_ERROR_RATE,
        _TOKEN_TOTAL,
        RETRY_COUNT_KEY,
        "performance.latency.wall_clock",
        "performance.latency.ttft",
    } <= scored
    assert len(scored) > len(unmeasured), sorted(unmeasured)
    assert unmeasured <= {"performance.cost.llm"}, sorted(unmeasured)


# --- Token usage from the in-memory timeline --------------------------------
# The tracing dispatcher writes ``llm_traces``/``llm_spans`` from a background
# worker, so those rows land 2-4s after an entry is scored. The step timeline
# captured in-process carries the same provider usage, so it is the primary
# source and the DB the fallback, as for durations and tool calls.


def _llm_step(usage: dict | None, **extra) -> dict:
    step = {"type": "llm", "description": "LLM call", "outcome": "success"}
    if usage is not None:
        step["usage"] = usage
    step.update(extra)
    return step


def test_token_usage_is_read_from_the_in_memory_timeline():
    """Anthropic reports only UNCACHED input at ``input_tokens``; the cache
    buckets carry the bulk and must be folded in, or a 150k-token call reads 2."""
    usage = _sum_local_token_usage(
        [
            _llm_step(
                {
                    "input_tokens": 2,
                    "output_tokens": 94,
                    "cache_creation_input_tokens": 69371,
                    "cache_read_input_tokens": 0,
                }
            )
        ]
    )

    assert usage is not None
    assert usage["input_tokens"] == 2 + 69371
    assert usage["output_tokens"] == 94
    assert usage["cache_write_input_tokens"] == 69371
    assert usage["convention"] == "inclusive"


def test_token_usage_sums_every_llm_step_and_ignores_the_rest():
    usage = _sum_local_token_usage(
        [
            _llm_step({"input_tokens": 100, "output_tokens": 10}),
            {"type": "tool", "tool_name": "search", "usage": {"input_tokens": 999}},
            _llm_step({"input_tokens": 50, "output_tokens": 5}),
            _llm_step(None),
            _llm_step({}),
            "not a step",
        ]
    )

    assert usage == {"input_tokens": 150, "output_tokens": 15}


def test_an_in_memory_timeline_with_no_usage_yields_no_measurement():
    for timeline in ([], None, [_llm_step({})], [{"type": "tool"}]):
        assert _sum_local_token_usage(timeline) is None


def test_the_in_memory_timeline_wins_over_the_unflushed_trace_rows():
    """The DB rows are the fallback, not the source of truth."""
    resolved = asyncio.run(
        _resolve_agent_token_usage(
            _FakeDB((999_999, 999_999), []),
            [_llm_step({"input_tokens": 100, "output_tokens": 10})],
            [str(uuid.uuid4())],
        )
    )

    assert resolved == {"input_tokens": 100, "output_tokens": 10}


def test_the_trace_rows_still_answer_when_no_timeline_was_captured():
    resolved = asyncio.run(
        _resolve_agent_token_usage(
            _FakeDB((1200, 300), [{"input": 1200, "output": 300}]),
            [],
            [str(uuid.uuid4())],
        )
    )

    assert resolved["input_tokens"] == 1200
    assert resolved["output_tokens"] == 300


def test_no_timeline_and_no_trace_ids_stays_uncaptured():
    assert (
        asyncio.run(_resolve_agent_token_usage(_FakeDB((0, 0), []), [], [])) is None
    )
