"""Gate check chains: list_timers is evaluable, a skip names its check, a passed-over check is visible (#10724).

1. ``list_timers`` answers its list as ``structured``: an empty answer fails
   ``non_empty`` and the run is skipped at zero tokens, as ``list_work_items``.
2. A skip on a chain names the position of the failing check and its resolved
   arguments, so two checks on one tool are told apart.
3. A check passed over as not evaluable is named on the run and counted under
   the assignment's drop reasons; the run still proceeds.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.services import assignment_engine, integration_event_filters
from app.services.assignment_activation_check import (
    chain_position,
    run_activation_check,
    stored_activation_check,
    unevaluated_reason,
)
from app.services.builtin_tools.timers import build_timer_tools
from app.tools.base import CallableToolExecutor, CompositeToolExecutor, ToolCall, ToolDefinition, ToolResult

OPEN_ITEMS = {"tool": "list_work_items", "arguments": {"open_only": True}, "condition": "non_empty"}
TIMERS = {"tool": "list_timers", "arguments": {"key_prefix": "{{payload.prefix}}"}, "condition": "non_empty"}
TEXT_ONLY = {"tool": "count_things", "arguments": {}, "condition": "non_empty"}


def _timer_executor(monkeypatch, timers: list[dict]) -> CompositeToolExecutor:
    from app.services import platform_timers

    async def _fake_list(**kwargs):
        prefix = kwargs.get("key_prefix") or ""
        return [t for t in timers if t["correlation_key"].startswith(prefix)]

    monkeypatch.setattr(platform_timers, "list_timers", _fake_list)
    definitions, executor = build_timer_tools(str(uuid.uuid4()), str(uuid.uuid4()))
    composite = CompositeToolExecutor()
    composite.add_tools(definitions, executor)
    return composite


TIMER = {
    "id": "t-1",
    "correlation_key": "brief:abc",
    "due_at": "2026-11-10T09:00:00+00:00",
    "state": "armed",
}


async def test_list_timers_serves_its_list_structured(monkeypatch):
    composite = _timer_executor(monkeypatch, [TIMER])
    none = await composite.execute_tool(ToolCall(id="a", name="list_timers", arguments={"key_prefix": "nope:"}))
    assert none.structured == [] and str(none.content).startswith("No timers")
    one = await composite.execute_tool(ToolCall(id="b", name="list_timers", arguments={"key_prefix": "brief:"}))
    assert [t["correlation_key"] for t in one.structured] == ["brief:abc"]
    assert "brief:abc" in str(one.content)


async def test_an_empty_timer_list_fails_non_empty(monkeypatch):
    composite = _timer_executor(monkeypatch, [TIMER])
    outcome = await run_activation_check(TIMERS, composite, payload={"prefix": "nope:"})
    assert not outcome.passed and outcome.error is None
    assert outcome.summary == "Skipped: gate check list_timers returned empty"
    assert outcome.arguments == {"key_prefix": "nope:"}
    armed = await run_activation_check(TIMERS, composite, payload={"prefix": "brief:"})
    assert armed.passed and armed.error is None


def test_a_single_check_keeps_its_summary_and_a_chain_names_the_failing_one():
    summary = "Skipped: gate check get_shared_state: $.held_by_run.alive is in [true]"
    assert chain_position(summary, position=1, total=1, tool="get_shared_state", arguments={"key": "k"}) == summary
    lease = chain_position(summary, position=1, total=2, tool="get_shared_state", arguments={"key": "lock:b"})
    claim = chain_position(summary, position=2, total=2, tool="get_shared_state", arguments={"key": "claim:s"})
    assert lease.endswith('(check 1 of 2: get_shared_state {"key": "lock:b"})')
    assert claim.endswith('(check 2 of 2: get_shared_state {"key": "claim:s"})')
    assert lease != claim


# ---------------------------------------------------------------------------
# The engine stage
# ---------------------------------------------------------------------------


class _Db:
    def __init__(self):
        self.commits = 0

    async def commit(self):
        self.commits += 1


def _parts(check, payload):
    run = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        status="pending",
        result_summary=None,
        messages=None,
        total_input_tokens=None,
        total_output_tokens=None,
        started_at=None,
        completed_at=None,
        is_company_managed=False,
        trigger_context={"type": "platform_event"},
        trigger_payload=payload,
    )
    agent = SimpleNamespace(id=uuid.uuid4(), name="Developer")
    assignment = SimpleNamespace(id=uuid.uuid4(), name="Probe", activation_check=check)
    return run, agent, assignment


@pytest.fixture
def _engine(monkeypatch):
    published: list[tuple] = []
    reasons: list[tuple] = []

    async def _publish(*args, **kwargs):
        published.append(args)

    async def _release(run, agent, *, failed, error=None):
        return None

    async def _no_lease(_db, _run_id):
        return False

    async def _record(assignment_id, counted):
        reasons.append((assignment_id, dict(counted)))

    monkeypatch.setattr(assignment_engine, "_publish_run_status", _publish)
    monkeypatch.setattr(assignment_engine, "_release_run_work_item", _release)
    monkeypatch.setattr(assignment_engine, "_run_holds_a_lease", _no_lease)
    monkeypatch.setattr(integration_event_filters, "record_filter_reasons", _record)
    return published, reasons


def _with_tools(monkeypatch, timers: list[dict]):
    async def _items(open_only: bool = False):
        return ToolResult("", "1 work item(s)", structured=[{"id": "wi-1"}])

    async def _count():
        return "3 things"

    async def _executor_for(*_args, **_kwargs):
        composite = _timer_executor(monkeypatch, timers)
        inner = CallableToolExecutor()
        inner.register("list_work_items", _items)
        inner.register("count_things", _count)
        composite.add_tools(
            [
                ToolDefinition(name="list_work_items", description="list"),
                ToolDefinition(name="count_things", description="count"),
            ],
            inner,
        )
        return composite

    monkeypatch.setattr(assignment_engine, "_activation_check_executor", _executor_for)


async def _skip(check, payload):
    run, agent, assignment = _parts(check, payload)
    db = _Db()
    skipped = await assignment_engine._skip_by_activation_check(
        db, run=run, agent=agent, assignment=assignment, effective_user_id=run.user_id
    )
    return skipped, run, assignment


async def test_a_chained_empty_timer_list_skips_the_run_at_zero_tokens(monkeypatch, _engine):
    published, reasons = _engine
    _with_tools(monkeypatch, [TIMER])

    skipped, run, _assignment = await _skip(stored_activation_check({**OPEN_ITEMS, "also": [TIMERS]}), {"prefix": "nope:"})

    assert skipped is True
    assert run.result_summary == (
        'Skipped: gate check list_timers returned empty (check 2 of 2: list_timers {"key_prefix": "nope:"})'
    )
    assert (run.total_input_tokens, run.total_output_tokens) == (0, 0)
    assert published[0][2] == "skipped"
    assert reasons == [] and "gate_checks_not_evaluated" not in run.trigger_context


async def test_a_check_passed_over_is_named_on_the_run_and_counted(monkeypatch, _engine):
    _published, reasons = _engine
    _with_tools(monkeypatch, [TIMER])

    skipped, run, assignment = await _skip(
        stored_activation_check({**OPEN_ITEMS, "also": [TEXT_ONLY, TIMERS]}), {"prefix": "brief:"}
    )

    # The rule is unchanged: a check that cannot be evaluated lets the run proceed.
    assert skipped is False and run.status == "pending"
    [entry] = run.trigger_context["gate_checks_not_evaluated"]
    assert entry["position"] == 2 and entry["tool"] == "count_things"
    assert "no structured result" in entry["reason"]
    assert run.trigger_context["type"] == "platform_event"
    assert reasons == [(assignment.id, {unevaluated_reason(2, "count_things"): 1})]
