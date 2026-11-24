"""A tool check that lists alternatives under ``or``: the run goes ahead when one passes.

A sweep that has work when its own queue is non-empty OR an external source
holds an active alert stands down at zero tokens only when both are empty.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.services import assignment_engine
from app.services.assignment_activation_check import (
    MAX_EXTRA_CHECKS,
    SKIP_SUMMARY_PREFIX,
    any_of_skip_summary,
    chained_checks,
    describe_check,
    is_any_of,
    parse_activation_check,
    stored_activation_check,
)
from app.tools.base import CallableToolExecutor, CompositeToolExecutor, ToolDefinition, ToolResult

OWN_QUEUE = {"tool": "list_work_items", "arguments": {"open_only": True}, "condition": "non_empty"}
ALERTS = {"tool": "get_shared_state", "arguments": {"key": "alerts"}, "condition": {"path": "$.active", "in": [True]}}


def test_or_is_parsed_check_by_check_and_listed_in_order():
    value, error, provided = parse_activation_check({**OWN_QUEUE, "or": [ALERTS]})
    assert (error, provided) == (None, True)
    assert is_any_of(value) and "also" not in value
    assert [check["tool"] for check in chained_checks(value)] == ["list_work_items", "get_shared_state"]
    assert all("or" not in check for check in chained_checks(value))


def test_a_check_without_or_is_not_any_of():
    value, _error, _ = parse_activation_check({**OWN_QUEUE, "or": []})
    assert not is_any_of(value) and "or" not in value


@pytest.mark.parametrize(
    ("raw", "fragment"),
    [
        ({**OWN_QUEUE, "or": "x"}, "or takes a list of 1 to"),
        ({**OWN_QUEUE, "or": [ALERTS] * (MAX_EXTRA_CHECKS + 1)}, "or takes a list of 1 to"),
        ({**OWN_QUEUE, "or": [{**ALERTS, "also": [ALERTS]}]}, "cannot hold its own also list"),
        ({**OWN_QUEUE, "or": [{**ALERTS, "or": [ALERTS]}]}, "cannot hold its own or list"),
        ({**OWN_QUEUE, "or": [{"tool": "create_work_item"}]}, "or[1]: activation_check calls read-only tools only"),
        ({**OWN_QUEUE, "or": [ALERTS], "also": [ALERTS]}, "not both"),
    ],
)
def test_a_malformed_or_is_refused_naming_the_check(raw, fragment):
    value, error, provided = parse_activation_check(raw)
    assert value is None and provided
    assert fragment in error


def test_an_any_of_check_is_described_as_alternatives():
    line = describe_check(stored_activation_check({**OWN_QUEUE, "or": [ALERTS]}))
    assert line.startswith("any of: list_work_items(")
    assert "; or get_shared_state(" in line


def test_the_skip_summary_names_every_failed_check():
    summary = any_of_skip_summary(
        [
            ("list_work_items", f"{SKIP_SUMMARY_PREFIX} list_work_items returned empty", {"open_only": True}),
            ("get_shared_state", f"{SKIP_SUMMARY_PREFIX} get_shared_state: no $.active in [true]", {"key": "alerts"}),
        ]
    )
    assert summary.startswith(SKIP_SUMMARY_PREFIX)
    assert summary.startswith("Skipped: gate checks: none of 2 passed (")
    assert 'check 1: list_work_items {"open_only": true}: list_work_items returned empty' in summary
    assert 'check 2: get_shared_state {"key": "alerts"}: get_shared_state: no $.active in [true]' in summary


# ---------------------------------------------------------------------------
# The engine stage
# ---------------------------------------------------------------------------


class _Db:
    def __init__(self):
        self.commits = 0

    async def commit(self):
        self.commits += 1


def _executor(items: list, state: dict, calls: list) -> CompositeToolExecutor:
    async def _list(open_only: bool = False):
        calls.append("list_work_items")
        return ToolResult("", "items", structured=items)

    async def _get_state(key: str = ""):
        calls.append("get_shared_state")
        return ToolResult("", "state", structured=state)

    inner = CallableToolExecutor()
    inner.register("list_work_items", _list)
    inner.register("get_shared_state", _get_state)
    composite = CompositeToolExecutor()
    composite.add_tools(
        [
            ToolDefinition(name="list_work_items", description="list"),
            ToolDefinition(name="get_shared_state", description="read"),
        ],
        inner,
    )
    return composite


@pytest.fixture
def _engine(monkeypatch):
    published: list[tuple] = []

    async def _publish(*args, **kwargs):
        published.append(args)

    async def _release(run, agent, *, failed, error=None):
        return None

    async def _no_lease(_db, _run_id):
        return False

    async def _no_reasons(*_args, **_kwargs):
        return None

    monkeypatch.setattr(assignment_engine, "_publish_run_status", _publish)
    monkeypatch.setattr(assignment_engine, "_release_run_work_item", _release)
    monkeypatch.setattr(assignment_engine, "_run_holds_a_lease", _no_lease)
    monkeypatch.setattr("app.services.integration_event_filters.record_filter_reasons", _no_reasons)
    return published


def _with(monkeypatch, items, state, calls):
    async def _executor_for(*_args, **_kwargs):
        return _executor(items, state, calls)

    monkeypatch.setattr(assignment_engine, "_activation_check_executor", _executor_for)


async def _skip(check):
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
        trigger_context={},
        trigger_payload={},
    )
    agent = SimpleNamespace(id=uuid.uuid4(), name="Sweep")
    assignment = SimpleNamespace(id=uuid.uuid4(), name="Alert sweep", activation_check=check)
    db = _Db()
    skipped = await assignment_engine._skip_by_activation_check(
        db, run=run, agent=agent, assignment=assignment, effective_user_id=run.user_id
    )
    return skipped, run, db


async def test_every_check_failing_skips_the_run_at_zero_tokens(monkeypatch, _engine):
    calls: list = []
    _with(monkeypatch, [], {"active": False}, calls)

    skipped, run, db = await _skip(stored_activation_check({**OWN_QUEUE, "or": [ALERTS]}))

    assert skipped is True
    assert run.result_summary.startswith("Skipped: gate checks: none of 2 passed (check 1: list_work_items")
    assert "check 2: get_shared_state" in run.result_summary
    assert (run.total_input_tokens, run.total_output_tokens) == (0, 0)
    assert run.messages[0]["content"].startswith("Gate check, any of: ")
    assert calls == ["list_work_items", "get_shared_state"]
    assert db.commits == 1 and _engine[0][2] == "skipped"


async def test_the_first_check_passing_lets_the_run_go_ahead_without_the_others(monkeypatch, _engine):
    calls: list = []
    _with(monkeypatch, [{"id": "wi-1"}], {"active": False}, calls)

    skipped, run, db = await _skip(stored_activation_check({**OWN_QUEUE, "or": [ALERTS]}))

    assert skipped is False
    assert run.status == "pending" and db.commits == 0
    assert calls == ["list_work_items"]


async def test_a_later_check_passing_lets_the_run_go_ahead(monkeypatch, _engine):
    calls: list = []
    _with(monkeypatch, [], {"active": True}, calls)

    skipped, run, _db = await _skip(stored_activation_check({**OWN_QUEUE, "or": [ALERTS]}))

    assert skipped is False and run.status == "pending"
    assert calls == ["list_work_items", "get_shared_state"]


async def test_a_check_on_a_tool_the_agent_does_not_hold_fails_as_misconfigured(monkeypatch, _engine):
    # #1400: a configuration error is a failed alternative, named on the run, never a pass-through.
    calls: list = []
    _with(monkeypatch, [], {"active": False}, calls)
    unknown = {"tool": "list_things", "condition": "non_empty"}

    skipped, run, _db = await _skip(stored_activation_check({**OWN_QUEUE, "or": [unknown, ALERTS]}))

    assert skipped is True and run.status == "completed"
    assert run.trigger_context["gate_check_misconfigured"]["tool"] == "list_things"
    assert calls == ["list_work_items", "get_shared_state"]
