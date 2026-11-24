"""A run's trace survives the graceful stop of its process (xtm-one #1712).

The local trace is assembled in ``end_trace`` only; a turn still running when
its worker stops never reached it and left no trace. Open traces are now kept
until their end and closed as interrupted by the stop paths.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest

from app.services import langfuse_tracing as lft
from tests.ast_helpers import call_lines as _call_lines
from tests.ast_helpers import function_node as _function_node

APP_DIR = Path(lft.__file__).resolve().parents[1]


def _root_ctx() -> dict[str, Any]:
    return lft._register_open_trace(
        {
            "trace": None,
            "generation": None,
            "client": None,
            "parent_ctx": None,
            "metadata": {},
            "local_trace_id": uuid.uuid4(),
            "local_source": "assignment",
            "local_name": "playbook agent call",
            "local_metadata": {"source": "assignment"},
            "local_tags": [],
            "local_status": "ok",
            "local_spans": [],
            "local_span_counter": 0,
        }
    )


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    monkeypatch.setattr(lft, "_OPEN_TRACES", {})
    submitted: list[Any] = []
    monkeypatch.setattr(
        "app.services.tracing.dispatcher.submit_trace_for_flush",
        lambda trace, spans: submitted.append((trace, spans)),
    )
    return submitted


def test_open_trace_is_kept_until_its_end(_isolated: list[Any]) -> None:
    ctx = _root_ctx()
    assert lft.open_trace_count() == 1
    lft.end_trace(ctx, output="done")
    assert lft.open_trace_count() == 0
    assert len(_isolated) == 1
    assert _isolated[0][0].status == "ok"


@pytest.mark.asyncio
async def test_stop_closes_open_trace_with_its_last_step(_isolated: list[Any]) -> None:
    ctx = _root_ctx()
    done = lft._build_local_span_context(ctx, span_type="GENERATION", name="iteration 1", input_payload=None)
    lft._forget_open_span(ctx, done)
    ctx["local_spans"].append({"id": done["local_span_id"], "type": "GENERATION", "name": "iteration 1", "position": 1})
    lft._build_local_span_context(
        ctx, span_type="TOOL", name="deliver_json_answer", input_payload={"a": 1}, tool_call_id="call_1"
    )

    closed = await lft.close_open_traces_on_shutdown("worker_stopped")

    assert closed == 1
    assert lft.open_trace_count() == 0
    trace, spans = _isolated[0]
    assert trace.status == "error"
    assert trace.failure_cause == "engine_abandoned"
    assert "deliver_json_answer" in trace.error_message
    last_step = trace.metadata["last_step"]
    assert last_step["name"] == "deliver_json_answer"
    assert last_step["type"] == "TOOL"
    assert last_step["open"] is True
    assert last_step["tool_call_id"] == "call_1"
    assert trace.metadata["kind"] == lft.INTERRUPTED_TRACE_KIND
    interrupted = [s for s in spans if s.name == "deliver_json_answer"]
    assert interrupted and interrupted[0].level == "ERROR"
    assert interrupted[0].metadata.get("interrupted") is True


@pytest.mark.asyncio
async def test_last_step_falls_back_to_the_last_ended_span(_isolated: list[Any]) -> None:
    ctx = _root_ctx()
    ctx["local_spans"].append({"id": uuid.uuid4(), "type": "TOOL", "name": "search", "position": 3})
    ctx["local_spans"].append({"id": uuid.uuid4(), "type": "GENERATION", "name": "iteration 1", "position": 1})

    await lft.close_open_traces_on_shutdown("worker_stopped")

    trace, _ = _isolated[0]
    assert trace.metadata["last_step"]["name"] == "search"
    assert trace.metadata["last_step"]["open"] is False


@pytest.mark.asyncio
async def test_late_end_trace_after_the_stop_writes_nothing(_isolated: list[Any]) -> None:
    ctx = _root_ctx()
    await lft.close_open_traces_on_shutdown("worker_stopped")
    lft.end_trace(ctx, output="late answer")
    assert len(_isolated) == 1


@pytest.mark.asyncio
async def test_stop_with_nothing_open_writes_nothing(_isolated: list[Any]) -> None:
    assert await lft.close_open_traces_on_shutdown("worker_stopped") == 0
    assert _isolated == []


def test_registry_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(lft, "_OPEN_TRACES_MAX", 3)
    for _ in range(5):
        _root_ctx()
    assert lft.open_trace_count() == 3


def _calls_before(func_name: str, path: Path, first: str, second: str) -> None:
    node = _function_node(path, func_name)
    first_lines = _call_lines(node, first)
    second_lines = _call_lines(node, second)
    assert first_lines, f"{path.name}:{func_name} must call {first}()"
    assert second_lines
    assert min(first_lines) < min(second_lines), f"{first} must run before {second}"


def test_worker_shutdown_closes_open_traces_before_the_dispatcher_drain() -> None:
    _calls_before("shutdown", APP_DIR / "worker.py", "close_open_traces_on_shutdown", "shutdown_tracing_dispatcher")


def test_app_lifespan_closes_open_traces_before_the_dispatcher_drain() -> None:
    _calls_before("lifespan", APP_DIR / "main.py", "close_open_traces_on_shutdown", "shutdown_tracing_dispatcher")
