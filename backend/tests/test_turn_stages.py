"""A chat turn's pre-generation timeline: the stages before the first provider request."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from app.services import langfuse_tracing
from app.services.langfuse_tracing import end_generation, mark_first_output, start_llm_generation
from app.services.tracing import turn_stages
from app.services.tracing.run_rollup import rollup_percentiles
from app.services.tracing.turn_stages import FIRST_REQUEST_STAGE, MAX_STAGES, TurnStages


class _Clock:
    """A clock the test moves by hand, in seconds."""

    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch):
    fake = _Clock()
    monkeypatch.setattr(turn_stages, "_clock", fake)
    return fake


def _root() -> dict:
    return {
        "trace": None,
        "generation": None,
        "parent_ctx": None,
        "metadata": {"source": "chat"},
        "local_trace_id": uuid.uuid4(),
        "local_source": "chat",
        "local_name": "chat",
        "local_metadata": {"source": "chat"},
        "local_started_at": datetime.now(UTC) - timedelta(seconds=2),
        "local_spans": [],
        "local_span_counter": 0,
    }


def test_each_mark_closes_the_stage_since_the_previous_one(clock):
    stages = TurnStages()
    clock.now += 0.120
    stages.mark("history")
    clock.now += 0.040
    stages.mark("memory")
    clock.now += 0.900
    stages.close()
    assert stages.stages == [("history", 120.0), ("memory", 40.0), (FIRST_REQUEST_STAGE, 900.0)]
    assert stages.pre_generation_ms == 1060.0


def test_nothing_is_recorded_after_the_first_request(clock):
    stages = TurnStages()
    clock.now += 0.010
    stages.close()
    clock.now += 5.0
    stages.mark("later")
    stages.close()
    assert [name for name, _ in stages.stages] == [FIRST_REQUEST_STAGE]


def test_the_timeline_is_bounded():
    stages = TurnStages()
    for index in range(MAX_STAGES + 10):
        stages.mark(f"s{index}")
    assert len(stages.stages) == MAX_STAGES


def test_first_output_is_measured_from_the_start_once(clock):
    stages = TurnStages()
    clock.now += 1.5
    stages.first_output()
    clock.now += 1.0
    stages.first_output()
    assert stages.first_output_ms == 1500.0


def test_a_pending_timeline_is_taken_once():
    started = turn_stages.begin_turn()
    assert turn_stages.take_pending() is started
    assert turn_stages.take_pending() is None


def test_a_task_that_copied_the_context_cannot_take_it_again():
    started = turn_stages.begin_turn()

    async def _nested() -> TurnStages | None:
        await asyncio.sleep(0)
        return turn_stages.take_pending()

    async def _run() -> tuple[TurnStages | None, TurnStages | None]:
        task = asyncio.create_task(_nested())  # copies the context before the take
        own = turn_stages.take_pending()
        return own, await task

    own, nested = asyncio.run(_run())
    assert own is started
    assert nested is None


def test_the_first_provider_request_closes_the_timeline_on_the_root(clock):
    root = _root()
    stages = TurnStages()
    turn_stages.attach(root, stages)
    clock.now += 0.300
    stages.mark("setup")
    clock.now += 0.200
    gen = start_llm_generation(root, model="m", messages=[])
    assert stages.closed
    assert stages.stages[-1] == (FIRST_REQUEST_STAGE, 200.0)
    clock.now += 0.700
    mark_first_output(gen)
    assert stages.first_output_ms == 1200.0


def test_end_trace_writes_the_timeline_on_the_rollup(clock):
    root = _root()
    stages = TurnStages()
    turn_stages.attach(root, stages)
    clock.now += 0.250
    stages.mark("memory")
    clock.now += 0.050
    gen = start_llm_generation(root, model="m", messages=[])
    clock.now += 0.400
    mark_first_output(gen)
    end_generation(gen, output="hi")
    with patch("app.services.tracing.dispatcher.submit_trace_for_flush") as submit:
        langfuse_tracing.end_trace(root, output="hi")
    rollup = submit.call_args.args[0].metadata["rollup"]
    assert rollup["pre_generation_ms"] == 300.0
    assert rollup["turn_first_token_ms"] == 700.0
    assert rollup["turn_stages"]["stages"] == [
        {"stage": "memory", "ms": 250.0},
        {"stage": FIRST_REQUEST_STAGE, "ms": 50.0},
    ]
    assert rollup["turn_stages"]["reached_first_request"] is True


def test_a_trace_without_a_timeline_keeps_its_rollup_unchanged():
    root = _root()
    with patch("app.services.tracing.dispatcher.submit_trace_for_flush") as submit:
        langfuse_tracing.end_trace(root, output="hi")
    rollup = submit.call_args.args[0].metadata["rollup"]
    assert "turn_stages" not in rollup
    assert "pre_generation_ms" not in rollup


def test_the_pre_generation_figures_join_the_percentiles():
    summary = rollup_percentiles(
        [{"pre_generation_ms": float(v), "turn_first_token_ms": float(v * 10)} for v in range(1, 11)]
    )
    assert summary["pre_generation_ms"] == {"p50": 5.5, "p90": 9.1, "n": 10}
    assert summary["turn_first_token_ms"]["n"] == 10


def test_a_continued_cut_wait_is_on_the_timeline_but_not_in_pre_generation(clock):
    root = _root()
    stages = TurnStages()
    turn_stages.attach(root, stages)
    clock.now += 0.200
    stages.mark("loop_setup")
    clock.now += 240.0
    stages.mark("continued_cut_wait")
    clock.now += 0.100
    start_llm_generation(root, model="m", messages=[])
    assert stages.pre_generation_ms == 300.0
    assert stages.wait_ms == 240000.0
    fields = turn_stages.rollup_fields(root)
    assert fields["pre_generation_ms"] == 300.0
    assert fields["pre_generation_wait_ms"] == 240000.0
    assert [s["stage"] for s in fields["turn_stages"]["stages"]] == [
        "loop_setup",
        "continued_cut_wait",
        FIRST_REQUEST_STAGE,
    ]


def test_a_turn_without_a_pause_reports_no_wait_field(clock):
    root = _root()
    stages = TurnStages()
    turn_stages.attach(root, stages)
    clock.now += 0.050
    stages.close()
    assert "pre_generation_wait_ms" not in turn_stages.rollup_fields(root)


def test_mark_current_reaches_the_root_timeline_from_a_child_span(clock):
    root = _root()
    stages = TurnStages()
    turn_stages.attach(root, stages)
    child = {"parent_ctx": {"parent_ctx": root}}
    clock.now += 0.400
    turn_stages.mark_current(child, "iteration_context")
    assert stages.stages == [("iteration_context", 400.0)]
    assert turn_stages.of_trace(child) is stages


def test_mark_current_is_a_no_op_without_a_timeline():
    turn_stages.mark_current(None, "loop_setup")
    turn_stages.mark_current(_root(), "loop_setup")
    loop: dict = {}
    loop["parent_ctx"] = loop
    turn_stages.mark_current(loop, "loop_setup")
    assert turn_stages.of_trace(loop) is None


def test_helpers_ignore_foreign_values():
    turn_stages.attach(None, TurnStages())
    turn_stages.attach({}, None)
    assert turn_stages.of_root({turn_stages.TRACE_CTX_KEY: "nope"}) is None
    assert turn_stages.of_root(None) is None
    assert turn_stages.rollup_fields({}) == {}
