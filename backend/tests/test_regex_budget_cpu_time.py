"""A pattern budget is charged the CPU time of what runs the search, never runner load.

The output-schema checks, argument coercion and the Nova rule matcher bound what
a pattern may cost. They charged wall-clock time, so every pause of a loaded
host - the thread descheduled or waiting for the GIL, the matching worker
starved - spent budget no pattern used (up to 518 ms of a 1 s budget on a
30,001-item output) and left the last constraints of an output unjudged. These
tests pin the clock (``app.core.cpu_time.CpuStopwatch``), that a pause or
another thread's work spends nothing at any site, and that a pattern that
backtracks is still cut at the same CPU cost.
"""

from __future__ import annotations

import asyncio
import contextlib
import gc
import inspect
import logging
import os
import signal
import sys
import threading
import time

import jsonschema
import pytest

from app.core.cpu_time import CpuStopwatch, process_cpu_clock
from app.services import nova_matching, output_schema_validation, schema_guard
from app.services.nova_matching import KeywordRule
from app.services.output_schema_validation import output_schema_errors, output_schema_validator
from app.tools import agent_loop
from app.tools.agent_loop import coerce_tool_arguments

_BACKTRACKING = "^(a|aa)+$"
_BACKTRACKS_ON = "a" * 60 + "!"


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _busy(seconds: float) -> None:
    """Run on this thread for *seconds* of its own CPU time."""
    started = time.thread_time()
    while time.thread_time() - started < seconds:
        pass


# --- The clock ------------------------------------------------------------------


def test_a_stopwatch_charges_the_cpu_time_its_clock_counted():
    wall, cpu = _Clock(), _Clock()
    paused = CpuStopwatch(cpu, wall_clock=wall)
    wall.now, cpu.now = 0.5, 0.1  # descheduled for most of it
    assert (paused.charged(), paused.elapsed()) == (0.1, 0.5)

    wall.now, cpu.now = 10.0, 10.0
    ticked = CpuStopwatch(cpu, wall_clock=wall)
    # A Windows scheduler tick landed in 2 us of work: charged whole, as the
    # searches it misses are charged nothing, so they add up to the CPU used.
    wall.now, cpu.now = 10.000002, 10.015625
    assert ticked.charged() == pytest.approx(0.015625)


def test_a_stopwatch_with_no_cpu_clock_charges_the_time_that_passed():
    wall = _Clock()
    watch = CpuStopwatch(None, wall_clock=wall)
    wall.now = 0.3
    assert watch.charged() == 0.3

    readings = iter([1.0])

    def gone() -> float:
        try:
            return next(readings)
        except StopIteration:
            raise ProcessLookupError from None  # the process it read exited

    wall.now = 0.0
    lost = CpuStopwatch(gone, wall_clock=wall)
    wall.now = 0.4
    assert lost.charged() == 0.4


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="another process's CPU time is read on Linux only")
def test_another_processs_cpu_time_is_read_on_linux():
    clock = process_cpu_clock(os.getpid())
    assert clock is not None, "the Nova worker's budget would silently fall back to wall-clock time"
    before = clock()
    _busy(0.1)
    assert 0.05 <= clock() - before <= 1.0
    assert abs(clock() - time.process_time()) < 0.1
    assert process_cpu_clock(0) is None
    assert process_cpu_clock(2**22 + 12345) is None  # no such process


def test_the_regex_stopwatch_counts_this_threads_work_and_nothing_else():
    paused = schema_guard.regex_stopwatch()
    time.sleep(0.05)
    assert paused.charged() < 0.02
    elsewhere = schema_guard.regex_stopwatch()
    other = threading.Thread(target=_busy, args=(0.05,))
    other.start()
    other.join()
    # ``time.process_time()`` would charge the other thread's 50 ms here.
    assert elsewhere.charged() < 0.02
    working = schema_guard.regex_stopwatch()
    _busy(0.05)
    assert working.charged() >= 0.03


def test_the_regex_stopwatch_leaves_out_a_collection_on_its_thread(large_heap):
    collection_started = time.thread_time()
    collecting = schema_guard.regex_stopwatch()
    for _ in range(5):
        gc.collect()
    charged = collecting.charged()
    assert time.thread_time() - collection_started >= 0.03, "the collections walked too little to tell"
    assert charged < 0.02


def test_every_budget_site_reads_the_one_clock():
    # A site reading its own clock would charge load again, and escape the injected clocks.
    assert not hasattr(output_schema_validation, "time")
    for function in (
        output_schema_validation._search,
        agent_loop._bounded_search,
        agent_loop.coerce_tool_arguments,
        nova_matching._run_in_worker,
    ):
        assert "time." not in inspect.getsource(function), function.__name__
    for function in (output_schema_validation._search, agent_loop._bounded_search, agent_loop.coerce_tool_arguments):
        assert "schema_guard.regex_stopwatch()" in inspect.getsource(function), function.__name__
    assert "CpuStopwatch(worker.cpu_clock)" in inspect.getsource(nova_matching._run_in_worker)


# --- The output-schema checks -----------------------------------------------------


def _paused(monkeypatch, pause) -> None:
    """Every search runs *pause* first, inside the time it is charged for."""
    search = schema_guard.bounded_search

    def paused_search(pattern, text, *, remaining):
        pause()
        return search(pattern, text, remaining=remaining)

    monkeypatch.setattr(schema_guard, "bounded_search", paused_search)


def _another_thread_works() -> None:
    # What runs while the engine has released the GIL: another thread's CPU time.
    other = threading.Thread(target=_busy, args=(0.03,))
    other.start()
    other.join()


class _Node:
    def __init__(self) -> None:
        self.me = self


@pytest.fixture
def large_heap():
    """Objects every collection walks, as on a long-lived worker."""
    heap = [_Node() for _ in range(300_000)]
    yield heap
    heap.clear()
    gc.collect()


def _collection_lands() -> None:
    gc.collect()  # on the searching thread itself


@pytest.mark.parametrize(
    "pause",
    [
        pytest.param(lambda: time.sleep(0.02), id="descheduled"),
        pytest.param(_another_thread_works, id="other-thread"),
        pytest.param(_collection_lands, id="garbage-collection"),
    ],
)
def test_a_pause_inside_the_searches_spends_no_regex_budget(monkeypatch, caplog, pause, large_heap):
    # Charged in wall-clock time, 30 such pauses spent this budget after 5 searches.
    monkeypatch.setattr(output_schema_validation, "OUTPUT_SCHEMA_REGEX_BUDGET_SECONDS", 0.1)
    monkeypatch.setattr(output_schema_validation, "_WARNED", set())
    _paused(monkeypatch, pause)
    schema = {"type": "array", "items": {"type": "string", "pattern": "^[a-z]+$"}}
    output = ["abc"] * 29 + ["ABC"]
    with caplog.at_level(logging.WARNING, logger="app.services.output_schema_validation"):
        errors = output_schema_errors(output_schema_validator(schema, jsonschema.Draft7Validator), output)
    assert [(list(e.absolute_path), e.message) for e in errors] == [([29], "'ABC' does not match '^[a-z]+$'")]
    assert [r.getMessage() for r in caplog.records] == []


def _first_searches_descheduled(count: int = 10):
    searches = iter(range(count))

    def pause() -> None:
        if next(searches, None) is not None:
            time.sleep(0.02)

    return pause


@pytest.mark.parametrize(
    "pause", [pytest.param(None, id="at-rest"), pytest.param(_first_searches_descheduled(), id="descheduled")]
)
def test_a_backtracking_pattern_is_still_cut_at_the_same_cpu_cost(monkeypatch, caplog, pause):
    budget = 0.3
    monkeypatch.setattr(output_schema_validation, "OUTPUT_SCHEMA_REGEX_BUDGET_SECONDS", budget)
    monkeypatch.setattr(output_schema_validation, "_WARNED", set())
    if pause is not None:
        _paused(monkeypatch, pause)
    schema = {"type": "array", "items": {"type": "string", "pattern": _BACKTRACKING}}
    validator = output_schema_validator(schema, jsonschema.Draft7Validator)
    with caplog.at_level(logging.WARNING, logger="app.services.output_schema_validation"):
        started = time.thread_time()
        errors = output_schema_errors(validator, [_BACKTRACKS_ON] * 5000)
        spent = time.thread_time() - started
    assert errors == []
    reasons = {r.getMessage().split(": ", 1)[1].split(" (", 1)[0] for r in caplog.records}
    assert reasons == {schema_guard.TIMED_OUT, schema_guard.NO_TIME}
    # The whole budget went to the pattern, and no more than one search past it:
    # 5,000 searches cut in their last 0.1 ms, uncharged, would add half a second.
    cap = output_schema_validation.OUTPUT_SCHEMA_PATTERN_TIMEOUT_SECONDS
    assert budget - 0.05 <= spent <= budget + cap + 0.15


class _ScriptedSearch:
    """Stands in for the clock and the engine: each search charges and ends as scripted."""

    def __init__(self, script: list[tuple[float, bool]]) -> None:
        self._script = iter(script)
        self._charge = 0.0
        self.remaining: list[float] = []

    def stopwatch(self) -> _ScriptedSearch:
        return self

    def charged(self) -> float:
        return self._charge

    def search(self, pattern, text, *, remaining):
        self.remaining.append(remaining)
        if remaining <= 0:
            self._charge = 0.0
            raise schema_guard.PatternUnjudgedError(pattern, schema_guard.NO_TIME)
        self._charge, finishes = next(self._script)
        if not finishes:
            raise schema_guard.PatternUnjudgedError(pattern, schema_guard.TIMED_OUT)
        return True


def test_a_search_is_charged_its_time_past_the_allowance_and_a_cut_one_all_of_it(monkeypatch):
    monkeypatch.setattr(output_schema_validation, "OUTPUT_SCHEMA_REGEX_BUDGET_SECONDS", 0.5)
    engine = _ScriptedSearch(
        [(0.00005, True), (0.0301, True), (0.1, False), (0.1, False), (0.1, False), (0.1, False), (0.07, False)]
    )
    monkeypatch.setattr(schema_guard, "regex_stopwatch", engine.stopwatch)
    monkeypatch.setattr(schema_guard, "bounded_search", engine.search)
    schema = {"type": "array", "items": {"type": "string", "pattern": "^x"}}
    output_schema_errors(output_schema_validator(schema, jsonschema.Draft7Validator), ["x"] * 9)
    # Free within its allowance; 30 ms past it; then cut, each charged whole, to exactly nothing left.
    assert engine.remaining == pytest.approx([0.1, 0.1, 0.1, 0.1, 0.1, 0.1, 0.07, 0.0, 0.0], abs=1e-9)


# --- Argument coercion ------------------------------------------------------------


def test_a_pause_spends_none_of_a_coercion_calls_regex_budget(monkeypatch):
    # Judged, "^0" refuses "5" and "6": both become integers. Charged in
    # wall-clock time, the first pause ran out the call's 0.1 s and "6" was left as sent.
    _paused(monkeypatch, lambda: time.sleep(0.15))
    union = {"anyOf": [{"pattern": "^0"}, {"type": "integer"}]}
    schema = {"type": "object", "properties": {"a": union, "b": union}}
    assert coerce_tool_arguments({"a": "5", "b": "6"}, schema) == {"a": 5, "b": 6}


def test_a_backtracking_union_pattern_still_leaves_the_value_as_sent(monkeypatch, caplog):
    # The cut is asserted on what the engine was handed and how it ended, not on
    # this thread's CPU time: on a loaded xdist runner the thread time around the
    # call also counts the engine's own overshoot and the call's setup (0.384 s
    # read once for a 0.1 s budget), which no budget of the coercion controls.
    search = schema_guard.bounded_search
    searches: list[tuple[float, str]] = []

    def recorded_search(pattern, text, *, remaining):
        try:
            found = search(pattern, text, remaining=remaining)
        except schema_guard.PatternUnjudgedError as exc:
            searches.append((remaining, exc.why))
            raise
        searches.append((remaining, "judged"))
        return found

    monkeypatch.setattr(schema_guard, "bounded_search", recorded_search)
    monkeypatch.setattr(agent_loop, "_NOT_JUDGED_WARNED", set())
    union = {"anyOf": [{"pattern": _BACKTRACKING}, {"type": "integer"}]}
    args = {"q": _BACKTRACKS_ON}
    with caplog.at_level(logging.WARNING, logger=agent_loop.logger.name):
        assert coerce_tool_arguments(args, {"type": "object", "properties": {"q": union}}) is args
    budget = agent_loop._COERCION_REGEX_BUDGET_SECONDS
    # One search, handed no more than the call's budget, ended by the engine's timeout.
    assert len(searches) == 1
    remaining, ended = searches[0]
    assert 0 < remaining <= budget
    assert ended == schema_guard.TIMED_OUT
    assert [r.getMessage() for r in caplog.records] == [
        f"Argument coercion left a union value as sent: {_BACKTRACKING!r} did not finish within {budget} s"
    ]


# --- Nova rules in their worker ---------------------------------------------------

_NOVA_BACKTRACKING = 'rule Slow { meta: category = "Test"\n  keywords: $a = /(a|a)*b/\n  condition: keywords.$a }'
_NOVA_DANGER = 'rule Good { meta: category = "Test"\n  keywords: $k = "danger"\n  condition: keywords.$k }'
_NOVA_HOSTILE = "a" * 40 + " danger"


@pytest.fixture
def fresh_worker():
    nova_matching.shutdown_nova_matching_worker()
    yield
    nova_matching.shutdown_nova_matching_worker()


async def _started_job(worker, before: int, seconds: float = 60.0) -> None:
    deadline = time.monotonic() + seconds
    while worker.progress.value == before:
        assert time.monotonic() < deadline, "the worker never started the job"
        await asyncio.sleep(0.01)


async def test_a_rule_whose_worker_does_not_run_is_cut_at_the_wall_clock_backstop(fresh_worker, monkeypatch, caplog):
    monkeypatch.setattr(nova_matching, "RULE_BUDGET_SECONDS", 0.2)
    rules = [KeywordRule("Slow (1)", _NOVA_BACKTRACKING, "block"), KeywordRule("Good (2)", _NOVA_DANGER, "log")]
    assert await nova_matching.match_keyword_rules("danger", rules[1:]) != [None]
    # The host never runs the worker: its CPU clock does not move.
    nova_matching._worker.cpu_clock = lambda: 0.0
    with caplog.at_level(logging.WARNING, logger="app.services.nova_matching"):
        started = time.monotonic()
        outcomes = await nova_matching.match_keyword_rules(_NOVA_HOSTILE, rules)
        took = time.monotonic() - started
    assert outcomes[0] is None and outcomes[1] is not None
    backstop = 0.2 * nova_matching.RULE_WALL_CLOCK_BACKSTOP_FACTOR
    assert took >= backstop
    assert [r.getMessage() for r in caplog.records if "Slow" in r.getMessage()] == [
        f"Nova rule Slow (1) held the matching worker {backstop:.0f}s of wall-clock time "
        f"on a {len(_NOVA_HOSTILE)}-character input; skipped for this input"
    ]


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="another process's CPU time is read on Linux only")
async def test_a_stopped_worker_spends_none_of_its_rules_budget(fresh_worker, monkeypatch, caplog):
    budget = 1.0
    monkeypatch.setattr(nova_matching, "RULE_BUDGET_SECONDS", budget)
    rules = [KeywordRule("Slow (1)", _NOVA_BACKTRACKING, "block")]
    await nova_matching.match_keyword_rules("warm", [KeywordRule("Good", _NOVA_DANGER, "log")])
    worker = nova_matching._worker
    (pid,) = worker.executor._processes
    before = worker.progress.value
    with caplog.at_level(logging.WARNING, logger="app.services.nova_matching"):
        started = time.monotonic()
        task = asyncio.ensure_future(nova_matching.match_keyword_rules(_NOVA_HOSTILE, rules))
        await _started_job(worker, before)
        # A host too busy to run the worker, for longer than the rule's whole budget.
        os.kill(pid, signal.SIGSTOP)
        try:
            await asyncio.sleep(1.5)
        finally:
            with contextlib.suppress(ProcessLookupError):  # cut while stopped
                os.kill(pid, signal.SIGCONT)
        assert await asyncio.wait_for(task, 60) == [None]
        took = time.monotonic() - started
    # Charged in wall-clock time the rule was cut while stopped, about 1 s in.
    assert took >= 1.5 + budget * 0.8
    assert any(f"Nova rule Slow (1) overran its {budget:.1f}s budget" in r.getMessage() for r in caplog.records)
