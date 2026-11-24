"""The tool-capable activation gate of an assignment (``activation_check``).

One read-only tool call and a condition on its structured result, evaluated by
the platform with no model call before the run is charged, before a model is
resolved and before the LLM gate. A failed check ends the run skipped with
zero tokens; a check that cannot be evaluated lets the run go ahead.
"""

from __future__ import annotations

import inspect
import uuid
from types import SimpleNamespace

import pytest

import app.core.database as database
import app.services.builtin_tools as builtin_tools_module
from app.api.v1.schemas.assignment import CreateAssignmentRequest, UpdateAssignmentRequest
from app.services import assignment_engine
from app.services.assignment_activation_check import (
    condition_met,
    describe_check,
    parse_activation_check,
    resolved_arguments,
    run_activation_check,
    skip_summary,
    stored_activation_check,
    structured_result,
)
from app.tools.base import CallableToolExecutor, CompositeToolExecutor, ToolCall, ToolDefinition, ToolResult

ITEM = {"id": "wi-1", "title": "Prepare board pack", "status": "open", "priority": "high", "is_stale": False}


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def test_a_check_is_normalized_with_its_defaults():
    value, error, provided = parse_activation_check('{"tool": "list_work_items"}')
    assert (error, provided) == (None, True)
    assert value == {"tool": "list_work_items", "arguments": {}, "condition": "non_empty"}


@pytest.mark.parametrize("raw", [None, "", "   "])
def test_a_blank_value_leaves_the_setting_unchanged(raw):
    assert parse_activation_check(raw) == (None, None, False)


@pytest.mark.parametrize("raw", ["none", "clear", "null", {}, "{}"])
def test_none_clear_and_an_empty_object_remove_the_check(raw):
    assert parse_activation_check(raw) == (None, None, True)


def test_a_path_condition_is_kept_with_its_path_trimmed():
    value, error, _ = parse_activation_check(
        {"tool": "get_shared_state", "arguments": {"key": "x"}, "condition": {"path": " $.status ", "in": ["active"]}}
    )
    assert error is None
    assert value["condition"] == {"path": "$.status", "in": ["active"]}


@pytest.mark.parametrize(
    ("raw", "fragment"),
    [
        ({"tool": "create_work_item"}, "read-only"),
        ({"tool": "send_mail"}, "read-only"),
        ({"tool": "list_work_items", "extra": 1}, "unknown key"),
        ({"arguments": {}}, "name of the tool"),
        ({"tool": "list_work_items", "arguments": []}, "arguments must be an object"),
        ({"tool": "list_work_items", "condition": "sometimes"}, "condition"),
        ({"tool": "list_work_items", "condition": {"path": "status", "in": ["open"]}}, "path"),
        ({"tool": "list_work_items", "condition": {"path": "$.status", "in": []}}, "list of 1"),
        ({"tool": "list_work_items", "condition": {"path": "$.status", "in": ["a"], "contains": "b"}}, "exactly one"),
        ({"tool": "list_work_items", "condition": {"path": "$.subject", "contains": ""}}, "non-empty string"),
        ("{not json", "not valid JSON"),
        ("[1]", "must be an object"),
    ],
)
def test_a_malformed_check_is_refused_with_its_reason(raw, fragment):
    value, error, provided = parse_activation_check(raw)
    assert value is None and provided
    assert fragment in error


def test_a_stored_check_that_no_longer_parses_reads_as_none():
    assert stored_activation_check({"tool": "delete_everything"}) is None
    assert stored_activation_check(None) is None


# ---------------------------------------------------------------------------
# Conditions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("data", "non_empty"),
    [([], False), ({}, False), (None, False), ("", False), (0, False), (False, False), ([ITEM], True), ({"a": 1}, True), (3, True)],
)
def test_non_empty_and_empty_read_the_shape_of_the_result(data, non_empty):
    assert condition_met(data, "non_empty") is non_empty
    assert condition_met(data, "empty") is (not non_empty)


def test_a_path_on_a_list_reads_each_element():
    items = [ITEM, {**ITEM, "id": "wi-2", "status": "blocked"}]
    assert condition_met(items, {"path": "$.status", "in": ["BLOCKED"]})
    assert condition_met(items, {"path": "$[*].status", "in": ["open"]})
    assert not condition_met(items, {"path": "$.status", "in": ["done"]})
    assert condition_met(items, {"path": "$[1].id", "in": ["wi-2"]})


def test_not_in_needs_a_value_and_none_listed():
    assert condition_met({"status": "active"}, {"path": "$.status", "not_in": ["resolved"]})
    assert not condition_met({"status": "resolved"}, {"path": "$.status", "not_in": ["resolved"]})
    assert not condition_met({}, {"path": "$.status", "not_in": ["resolved"]})


def test_contains_matches_text_in_any_case():
    events = [{"subject": "Weekly sync"}, {"subject": "BOARD meeting - Q4"}]
    assert condition_met(events, {"path": "$.subject", "contains": "board meeting"})
    assert not condition_met(events, {"path": "$.subject", "contains": "offsite"})


def test_the_skip_summaries_say_what_failed():
    assert skip_summary("list_work_items", "non_empty") == "Skipped: gate check list_work_items returned empty"
    assert skip_summary("t", "empty") == "Skipped: gate check t returned a non-empty result"
    assert "no $.status in" in skip_summary("t", {"path": "$.status", "in": ["active"]})


def test_placeholders_name_the_running_agent_and_assignment():
    resolved = resolved_arguments(
        {"owner_agent_id": "{{agent.id}}", "nested": ["{{assignment.id}}"], "open_only": True},
        {"agent.id": "A1", "assignment.id": "S1"},
    )
    assert resolved == {"owner_agent_id": "A1", "nested": ["S1"], "open_only": True}


def test_a_result_is_read_structured_first_then_as_json_text():
    assert structured_result(ToolResult("1", "No items.", structured=[])) == ([], True)
    assert structured_result(ToolResult("1", '[{"a": 1}]')) == ([{"a": 1}], True)
    assert structured_result(ToolResult("1", "plain text")) == (None, False)


# ---------------------------------------------------------------------------
# Evaluation through an executor
# ---------------------------------------------------------------------------


def _executor(handler) -> CompositeToolExecutor:
    inner = CallableToolExecutor()
    inner.register("list_work_items", handler)
    composite = CompositeToolExecutor()
    composite.add_tools([ToolDefinition(name="list_work_items", description="list")], inner)
    return composite


CHECK = {"tool": "list_work_items", "arguments": {"owner_agent_id": "{{agent.id}}", "open_only": True}, "condition": "non_empty"}


async def test_an_empty_queue_fails_the_check():
    seen = {}

    async def _list(owner_agent_id: str = "", open_only: bool = False):
        seen.update(owner_agent_id=owner_agent_id, open_only=open_only)
        return ToolResult("", "No work items match those filters.", structured=[])

    outcome = await run_activation_check(CHECK, _executor(_list), placeholders={"agent.id": "agent-7"})
    assert not outcome.passed
    assert outcome.summary == "Skipped: gate check list_work_items returned empty"
    assert seen == {"owner_agent_id": "agent-7", "open_only": True}


async def test_one_open_item_passes_the_check():
    async def _list(owner_agent_id: str = "", open_only: bool = False):
        return ToolResult("", "1 work item(s)", structured=[ITEM])

    outcome = await run_activation_check(CHECK, _executor(_list))
    assert outcome.passed and outcome.error is None


async def test_a_tool_error_lets_the_run_proceed_and_says_why():
    async def _list(**_kwargs):
        raise RuntimeError("database down")

    outcome = await run_activation_check(CHECK, _executor(_list))
    assert outcome.passed
    assert "list_work_items failed" in outcome.error


async def test_a_text_only_result_lets_the_run_proceed():
    async def _list(**_kwargs):
        return "3 items"

    outcome = await run_activation_check(CHECK, _executor(_list))
    assert outcome.passed and "no structured result" in outcome.error


async def test_a_tool_the_agent_does_not_hold_skips_the_run_as_misconfigured() -> None:
    # #1400: a permanent configuration error never lets the run through at full cost.
    outcome = await run_activation_check({**CHECK, "tool": "get_shared_state"}, _executor(lambda: None))
    assert not outcome.passed and outcome.misconfigured
    assert "holds no tool" in (outcome.error or "") and "misconfigured" in outcome.summary


async def test_a_target_the_tool_reports_missing_skips_the_run_as_misconfigured() -> None:
    async def _list(**_kwargs: object) -> ToolResult:
        return ToolResult(tool_call_id="", content="gone", structured={"healthy": None, "target_missing": True})

    outcome = await run_activation_check(CHECK, _executor(_list))
    assert not outcome.passed and outcome.misconfigured
    assert "no longer exists" in (outcome.error or "")


async def test_a_transient_failure_still_lets_the_run_proceed_not_misconfigured() -> None:
    async def _list(**_kwargs: object) -> None:
        raise RuntimeError("database down")

    outcome = await run_activation_check(CHECK, _executor(_list))
    assert outcome.passed and not outcome.misconfigured


async def test_the_work_item_tool_serves_a_structured_list(monkeypatch):
    from app.services import work_item

    async def _items(**_kwargs):
        return [ITEM]

    monkeypatch.setattr(work_item, "list_work_items", _items)
    _defs, executor = builtin_tools_module.build_work_item_tools(None, str(uuid.uuid4()), str(uuid.uuid4()))
    result = await executor.execute_tool(ToolCall(id="c", name="list_work_items", arguments={"open_only": True}))
    assert result.structured == [ITEM]
    assert "Prepare board pack" in result.content


async def test_the_shared_state_tool_serves_the_entry_or_an_empty_object(monkeypatch):
    from app.services import shared_state

    states = {"board-cycle": {"key": "board-cycle", "status": "active", "value": {"q": 4}}}

    async def _get_state(*, key, **_kwargs):
        return states.get(key)

    monkeypatch.setattr(shared_state, "get_state", _get_state)
    _defs, executor = builtin_tools_module.build_shared_state_tools(None, str(uuid.uuid4()), str(uuid.uuid4()))
    found = await executor.execute_tool(ToolCall(id="c", name="get_shared_state", arguments={"key": "board-cycle"}))
    missing = await executor.execute_tool(ToolCall(id="d", name="get_shared_state", arguments={"key": "nope"}))
    assert found.structured["status"] == "active"
    assert missing.structured == {}
    assert condition_met(found.structured, {"path": "$.status", "in": ["active"]})


# ---------------------------------------------------------------------------
# The engine stage
# ---------------------------------------------------------------------------


class _Db:
    def __init__(self):
        self.commits = 0

    async def commit(self):
        self.commits += 1


def _run_parts(check):
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
    )
    agent = SimpleNamespace(id=uuid.uuid4(), name="CEO desk")
    assignment = SimpleNamespace(id=uuid.uuid4(), name="Board prep", activation_check=check)
    return run, agent, assignment


@pytest.fixture
def _engine(monkeypatch):
    published: list[tuple] = []
    released: list = []

    async def _publish(*args, **kwargs):
        published.append(args)

    async def _release(run, agent, *, failed, error=None):
        released.append(failed)

    monkeypatch.setattr(assignment_engine, "_publish_run_status", _publish)
    monkeypatch.setattr(assignment_engine, "_release_run_work_item", _release)
    return published, released


async def test_a_failed_check_ends_the_run_skipped_with_zero_tokens(monkeypatch, _engine):
    published, released = _engine

    async def _empty(**_kwargs):
        return ToolResult("", "No work items match those filters.", structured=[])

    async def _executor_for(*_args, **_kwargs):
        return _executor(_empty)

    monkeypatch.setattr(assignment_engine, "_activation_check_executor", _executor_for)
    run, agent, assignment = _run_parts(CHECK)
    db = _Db()

    skipped = await assignment_engine._skip_by_activation_check(
        db, run=run, agent=agent, assignment=assignment, effective_user_id=run.user_id
    )

    assert skipped is True
    assert run.status == "completed"
    assert run.result_summary == "Skipped: gate check list_work_items returned empty"
    assert (run.total_input_tokens, run.total_output_tokens) == (0, 0)
    assert run.completed_at is not None and db.commits == 1
    assert run.messages[0]["content"].startswith("Gate check: list_work_items(")
    assert published[0][2] == "skipped"
    assert released == [False]


async def test_a_taken_over_attempt_runs_no_gate_tool_and_ends_nothing(monkeypatch, _engine):
    """The gate runs outside the agent loop's fence."""
    from app.core.turn_writer import TurnSuperseded, single_writer

    published, released = _engine
    ran: list[str] = []

    async def _empty(**_kwargs):
        ran.append("gate")
        return ToolResult("", "No work items match those filters.", structured=[])

    async def _executor_for(*_args, **_kwargs):
        return _executor(_empty)

    async def _taken() -> str | None:
        return "its execution runs on worker w2"

    monkeypatch.setattr(assignment_engine, "_activation_check_executor", _executor_for)
    run, agent, assignment = _run_parts(CHECK)
    db = _Db()
    with single_writer(_taken), pytest.raises(TurnSuperseded):
        await assignment_engine._skip_by_activation_check(
            db, run=run, agent=agent, assignment=assignment, effective_user_id=run.user_id
        )
    assert ran == []
    assert run.status == "pending" and db.commits == 0
    assert published == [] and released == []


async def test_an_attempt_taken_over_during_the_gate_setup_runs_no_gate_tool(monkeypatch, _engine):
    from app.core.turn_writer import TurnSuperseded, single_writer

    published, released = _engine
    ran: list[str] = []
    asked = {"n": 0}

    async def _empty(**_kwargs):
        ran.append("gate")
        return ToolResult("", "1 work item(s)", structured=[ITEM])

    async def _executor_for(*_args, **_kwargs):
        return _executor(_empty)

    async def _flips() -> str | None:
        asked["n"] += 1
        return None if asked["n"] == 1 else "its execution runs on worker w2"

    monkeypatch.setattr(assignment_engine, "_activation_check_executor", _executor_for)
    run, agent, assignment = _run_parts(CHECK)
    db = _Db()
    with single_writer(_flips), pytest.raises(TurnSuperseded):
        await assignment_engine._skip_by_activation_check(
            db, run=run, agent=agent, assignment=assignment, effective_user_id=run.user_id
        )
    assert ran == [] and asked["n"] == 2
    assert run.status == "pending" and db.commits == 0
    assert published == [] and released == []


async def test_an_attempt_taken_over_while_its_gate_ran_writes_no_skip(monkeypatch, _engine):
    from app.core.turn_writer import TurnSuperseded, single_writer

    published, released = _engine
    asked = {"n": 0}

    async def _empty(**_kwargs):
        return ToolResult("", "No work items match those filters.", structured=[])

    async def _executor_for(*_args, **_kwargs):
        return _executor(_empty)

    async def _flips() -> str | None:
        asked["n"] += 1
        return None if asked["n"] <= 2 else "its execution was taken back"

    monkeypatch.setattr(assignment_engine, "_activation_check_executor", _executor_for)
    run, agent, assignment = _run_parts(CHECK)
    db = _Db()
    with single_writer(_flips), pytest.raises(TurnSuperseded):
        await assignment_engine._skip_by_activation_check(
            db, run=run, agent=agent, assignment=assignment, effective_user_id=run.user_id
        )
    assert asked["n"] == 3
    assert run.status == "pending" and db.commits == 0
    assert published == [] and released == []


async def test_a_passing_check_leaves_the_run_to_the_agent_loop(monkeypatch, _engine):
    async def _one(**_kwargs):
        return ToolResult("", "1 work item(s)", structured=[ITEM])

    async def _executor_for(*_args, **_kwargs):
        return _executor(_one)

    monkeypatch.setattr(assignment_engine, "_activation_check_executor", _executor_for)
    run, agent, assignment = _run_parts(CHECK)
    db = _Db()

    assert not await assignment_engine._skip_by_activation_check(
        db, run=run, agent=agent, assignment=assignment, effective_user_id=run.user_id
    )
    assert run.status == "pending" and db.commits == 0


async def test_a_check_that_cannot_run_is_logged_and_the_run_proceeds(monkeypatch, caplog, _engine):
    async def _boom(*_args, **_kwargs):
        raise RuntimeError("integration unreachable")

    monkeypatch.setattr(assignment_engine, "_activation_check_executor", _boom)
    run, agent, assignment = _run_parts(CHECK)

    assert not await assignment_engine._skip_by_activation_check(
        _Db(), run=run, agent=agent, assignment=assignment, effective_user_id=None
    )
    assert any("could not run" in record.getMessage() for record in caplog.records)


async def test_an_assignment_without_a_check_builds_no_tools(monkeypatch):
    async def _never(*_args, **_kwargs):
        raise AssertionError("no tools are built without a check")

    monkeypatch.setattr(assignment_engine, "_activation_check_executor", _never)
    run, agent, assignment = _run_parts(None)
    assert not await assignment_engine._skip_by_activation_check(
        _Db(), run=run, agent=agent, assignment=assignment, effective_user_id=None
    )


def test_the_check_runs_before_the_charge_the_model_and_the_llm_gate():
    source = inspect.getsource(assignment_engine.execute_assignment_run)
    check = source.index("_skip_by_activation_check(")
    assert check < source.index("check_agentic_quota(")
    assert check < source.index("get_llm_for_user(")
    assert check < source.index("_run_activation_gate(")
    # A HITL resume and a checkpoint resume are past the gate (#8831).
    assert "if _gate_check_due(run, resuming=_resuming) and await _skip_by_activation_check(" in source


# ---------------------------------------------------------------------------
# The REST schemas and the agent builder
# ---------------------------------------------------------------------------


def test_the_rest_create_and_update_validate_the_check():
    body = CreateAssignmentRequest(
        agent_id=str(uuid.uuid4()),
        name="Board prep",
        trigger_type="schedule",
        trigger_config={"cron": "* * * * *"},
        prompt="Prepare",
        activation_check={"tool": "list_work_items", "arguments": {"open_only": True}},
    )
    assert body.activation_check["condition"] == "non_empty"
    with pytest.raises(ValueError, match="read-only"):
        UpdateAssignmentRequest(activation_check={"tool": "update_work_item"})
    cleared = UpdateAssignmentRequest(activation_check=None)
    assert "activation_check" in cleared.model_fields_set and cleared.activation_check is None


class _CreateSession:
    def __init__(self, sink: list):
        self.sink = sink

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    def add(self, row):
        self.sink.append(row)

    async def commit(self):
        return None


async def test_the_builder_creates_and_clears_a_check(monkeypatch):
    from tests.test_agent_builder_tool import (
        _DedicatedAssignmentSession,
        _SharedSession,
        _writable_assignment_from_session,
    )

    user_id = str(uuid.uuid4())
    uid = uuid.UUID(user_id)
    agent_id = uuid.uuid4()
    added: list = []
    monkeypatch.setattr(database, "async_session", lambda: _CreateSession(added))

    async def _gate(_db, _uid, _agent_uuid, *, action="modify", assignment_write=False):
        return SimpleNamespace(id=_agent_uuid, name="Desk", user_id=uid, is_company_managed=False), None

    monkeypatch.setattr(builtin_tools_module, "_load_writable_agent_for_builder", _gate)
    _, executor = builtin_tools_module.build_agent_builder_tool(_SharedSession(), user_id)

    refused = await executor.execute_tool(
        ToolCall(
            id="1",
            name="create_assignment",
            arguments={"agent_id": str(agent_id), "name": "x", "prompt": "p", "activation_check": '{"tool": "delete_agent"}'},
        )
    )
    assert "read-only" in refused.content and not added

    created = await executor.execute_tool(
        ToolCall(
            id="2",
            name="create_assignment",
            arguments={
                "agent_id": str(agent_id),
                "name": "Board prep",
                "prompt": "Prepare the board pack",
                "trigger_type": "schedule",
                "trigger_config": '{"cron": "* * * * *"}',
                "activation_check": (
                    '{"tool": "list_work_items", "arguments": {"owner_agent_id": "{{agent.id}}", '
                    '"open_only": true}, "condition": "non_empty"}'
                ),
            },
        )
    )
    (row,) = added
    assert row.activation_check == {
        "tool": "list_work_items",
        "arguments": {"owner_agent_id": "{{agent.id}}", "open_only": True},
        "condition": "non_empty",
    }
    assert "Gate check: list_work_items(" in created.content

    assignment_id = uuid.uuid4()
    stored = {
        assignment_id: SimpleNamespace(
            id=assignment_id,
            name="Board prep",
            prompt="p",
            activation_prompt=None,
            activation_check=row.activation_check,
            trigger_type="schedule",
            trigger_config={"cron": "* * * * *"},
            delay=None,
            enabled=True,
            user_id=uid,
        )
    }
    sessions: list = []
    monkeypatch.setattr(database, "async_session", lambda: _DedicatedAssignmentSession(sessions, stored))
    monkeypatch.setattr(builtin_tools_module, "_load_writable_assignment_for_builder", _writable_assignment_from_session)
    cleared = await executor.execute_tool(
        ToolCall(id="3", name="update_assignment", arguments={"assignment_id": str(assignment_id), "activation_check": "none"})
    )
    assert "activation_check (tool gate)" in cleared.content
    assert stored[assignment_id].activation_check is None


async def test_get_assignment_lists_the_gate_stages_and_the_delivery(monkeypatch):
    from app.core import redis as core_redis
    from tests.test_agent_builder_tool import _DedicatedAssignmentSession, _SharedSession

    user_id = str(uuid.uuid4())
    assignment_id, agent_id = uuid.uuid4(), uuid.uuid4()
    assignment = SimpleNamespace(
        id=assignment_id,
        name="Inbox triage",
        description=None,
        prompt="Triage",
        activation_prompt="Is it a customer? YES or NO.",
        activation_check={"tool": "get_shared_state", "arguments": {"key": "triage"}, "condition": "non_empty"},
        trigger_type="integration_event",
        trigger_config={"provider": "slack", "event": "new_slack_message"},
        delay=None,
        enabled=True,
        human_in_the_loop=False,
        ai_label=True,
        ai_label_prompt=None,
        human_in_the_loop_prompt=None,
        agent_id=agent_id,
        events_filtered_count=4,
    )

    class _Redis:
        async def get(self, key):
            return {f"event_poll:{assignment_id}": "1793311200.0"}.get(key)

    async def _redis():
        return _Redis()

    async def _no_groups(*_args):
        return []

    monkeypatch.setattr(core_redis, "redis_client", _redis)
    monkeypatch.setattr(
        database,
        "async_session",
        lambda: _DedicatedAssignmentSession([], {assignment_id: assignment}, {agent_id: SimpleNamespace(id=agent_id, name="Desk")}),
    )
    import app.api.v1.helpers as api_helpers

    monkeypatch.setattr(api_helpers, "get_user_group_ids", _no_groups)
    _, executor = builtin_tools_module.build_agent_builder_tool(_SharedSession(), user_id)
    result = await executor.execute_tool(ToolCall(id="1", name="get_assignment", arguments={"assignment_id": str(assignment_id)}))

    text = result.content
    assert "Gate stages" in text
    assert "1. Tool check (no model call): " + describe_check(assignment.activation_check) in text
    assert "2. Prompt gate (one model call): set" in text
    assert "Delivery: poll every 5 min" in text
    assert "Last poll: 2026-" in text
    assert "Last notification: never" in text
    assert "Events filtered: 4" in text
