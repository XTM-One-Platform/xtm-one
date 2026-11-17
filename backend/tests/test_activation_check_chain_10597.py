"""A tool check that chains further checks under ``also`` (#10597).

An event-driven run with two independent reasons to stand down (a live lease,
a per-head claim another run took) is skipped at zero tokens by either: the
checks run in order, with no model call, and the first that fails ends the run.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.services import assignment_engine
from app.services.assignment_activation_check import (
    MAX_EXTRA_CHECKS,
    chained_checks,
    describe_check,
    parse_activation_check,
    stored_activation_check,
)
from app.tools.base import CallableToolExecutor, CompositeToolExecutor, ToolDefinition, ToolResult

LEASE = {
    "tool": "get_shared_state",
    "arguments": {"key": "lock:{{payload.branch}}"},
    "condition": {"path": "$.held_by_run.alive", "none_in": [True]},
}
CLAIM = {
    "tool": "get_shared_state",
    "arguments": {"key": "claim:{{payload.sha}}"},
    "condition": {"path": "$.held_by_run.alive", "none_in": [True]},
}
OPEN_ROUND = {"tool": "list_work_items", "arguments": {"correlation_key": "ux:{{payload.pr}}"}, "condition": "empty"}


def test_also_is_parsed_check_by_check_with_its_defaults():
    value, error, provided = parse_activation_check({**LEASE, "also": [CLAIM, {"tool": "list_work_items"}]})
    assert (error, provided) == (None, True)
    assert value["also"] == [
        {**CLAIM},
        {"tool": "list_work_items", "arguments": {}, "condition": "non_empty"},
    ]
    assert [check["arguments"].get("key") for check in chained_checks(value)] == [
        "lock:{{payload.branch}}",
        "claim:{{payload.sha}}",
        None,
    ]
    assert all("also" not in check for check in chained_checks(value))


def test_a_check_without_also_is_stored_as_before():
    value, error, _ = parse_activation_check(LEASE)
    assert error is None and "also" not in value
    assert parse_activation_check({**LEASE, "also": []})[0] == value
    assert chained_checks(value) == [value]


@pytest.mark.parametrize(
    ("also", "fragment"),
    [
        ("x", "list of 1 to"),
        ([CLAIM] * (MAX_EXTRA_CHECKS + 1), "list of 1 to"),
        ([{}], "also[1] must be a check object"),
        ([{**CLAIM, "also": [CLAIM]}], "cannot hold its own also"),
        ([{"tool": "create_work_item"}], "also[1]: activation_check calls read-only tools only"),
        ([CLAIM, {"tool": "list_work_items", "arguments": {"k": "{{nope}}"}}], "also[2]"),
    ],
)
def test_a_malformed_also_is_refused_naming_the_check(also, fragment):
    value, error, provided = parse_activation_check({**LEASE, "also": also})
    assert value is None and provided
    assert fragment in error


def test_a_stored_chain_reads_back_and_is_described_in_order():
    stored = stored_activation_check({**LEASE, "also": [OPEN_ROUND]})
    assert stored is not None
    line = describe_check(stored)
    assert line.index("get_shared_state(") < line.index("; then list_work_items(")
    assert "runs when empty" in line


# ---------------------------------------------------------------------------
# The engine stage
# ---------------------------------------------------------------------------


class _Db:
    def __init__(self):
        self.commits = 0

    async def commit(self):
        self.commits += 1


def _executor(states: dict, items: list, calls: list) -> CompositeToolExecutor:
    async def _get_state(key: str = ""):
        calls.append(("get_shared_state", key))
        return ToolResult("", "state", structured=states.get(key, {}))

    async def _list(correlation_key: str = ""):
        calls.append(("list_work_items", correlation_key))
        return ToolResult("", "items", structured=items)

    inner = CallableToolExecutor()
    inner.register("get_shared_state", _get_state)
    inner.register("list_work_items", _list)
    composite = CompositeToolExecutor()
    composite.add_tools(
        [
            ToolDefinition(name="get_shared_state", description="read"),
            ToolDefinition(name="list_work_items", description="list"),
        ],
        inner,
    )
    return composite


def _parts(check):
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
        trigger_payload={"branch": "feat/x", "sha": "abc123", "pr": "42"},
    )
    agent = SimpleNamespace(id=uuid.uuid4(), name="Developer")
    assignment = SimpleNamespace(id=uuid.uuid4(), name="Green check", activation_check=check)
    return run, agent, assignment


@pytest.fixture
def _engine(monkeypatch):
    published: list[tuple] = []

    async def _publish(*args, **kwargs):
        published.append(args)

    async def _release(run, agent, *, failed, error=None):
        return None

    async def _no_lease(_db, _run_id):
        return False

    monkeypatch.setattr(assignment_engine, "_publish_run_status", _publish)
    monkeypatch.setattr(assignment_engine, "_release_run_work_item", _release)
    monkeypatch.setattr(assignment_engine, "_run_holds_a_lease", _no_lease)
    return published


def _with(monkeypatch, states, items, calls):
    async def _executor_for(*_args, **_kwargs):
        return _executor(states, items, calls)

    monkeypatch.setattr(assignment_engine, "_activation_check_executor", _executor_for)


async def _skip(check):
    run, agent, assignment = _parts(check)
    db = _Db()
    skipped = await assignment_engine._skip_by_activation_check(
        db, run=run, agent=agent, assignment=assignment, effective_user_id=run.user_id
    )
    return skipped, run, db


async def test_a_failing_second_check_skips_the_run_at_zero_tokens(monkeypatch, _engine):
    calls: list = []
    claimed = {"claim:abc123": {"key": "claim:abc123", "held_by_run": {"id": "other", "alive": True}}}
    _with(monkeypatch, claimed, [], calls)
    check = stored_activation_check({**LEASE, "also": [CLAIM, OPEN_ROUND]})

    skipped, run, db = await _skip(check)

    assert skipped is True
    assert run.result_summary == (
        "Skipped: gate check get_shared_state: $.held_by_run.alive is in [true]"
        ' (check 2 of 3: get_shared_state {"key": "claim:abc123"})'
    )
    assert (run.total_input_tokens, run.total_output_tokens) == (0, 0)
    assert '"claim:{{payload.sha}}"' in run.messages[0]["content"]
    # The lease passed, the claim failed: the third check never ran.
    assert calls == [("get_shared_state", "lock:feat/x"), ("get_shared_state", "claim:abc123")]
    assert db.commits == 1 and _engine[0][2] == "skipped"


async def test_an_open_round_skips_the_run_on_the_third_check(monkeypatch, _engine):
    calls: list = []
    _with(monkeypatch, {}, [{"id": "wi-1", "status": "open"}], calls)

    skipped, run, _db = await _skip(stored_activation_check({**LEASE, "also": [CLAIM, OPEN_ROUND]}))

    assert skipped is True
    assert run.result_summary.startswith("Skipped: gate check list_work_items returned a non-empty result")
    assert run.result_summary.endswith(' (check 3 of 3: list_work_items {"correlation_key": "ux:42"})')
    assert [name for name, _ in calls] == ["get_shared_state", "get_shared_state", "list_work_items"]


async def test_every_check_passing_leaves_the_run_to_the_agent_loop(monkeypatch, _engine):
    calls: list = []
    _with(monkeypatch, {}, [], calls)

    skipped, run, db = await _skip(stored_activation_check({**LEASE, "also": [CLAIM, OPEN_ROUND]}))

    assert skipped is False
    assert run.status == "pending" and db.commits == 0
    assert len(calls) == 3


async def test_an_extra_check_that_cannot_be_evaluated_never_blocks(monkeypatch, _engine):
    calls: list = []
    _with(monkeypatch, {}, [], calls)
    # The payload carries no ``missing`` field: that check is passed over, the next one still runs.
    unevaluable = {**CLAIM, "arguments": {"key": "claim:{{payload.missing}}"}}

    skipped, _run, _db = await _skip(stored_activation_check({**LEASE, "also": [unevaluable, OPEN_ROUND]}))

    assert skipped is False
    assert [name for name, _ in calls] == ["get_shared_state", "list_work_items"]
