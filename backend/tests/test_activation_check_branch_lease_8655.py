"""A gate check that skips an event run when its branch lease is held by a live run (#8655).

An event-driven assignment declares::

    {"tool": "get_shared_state",
     "arguments": {"key": "lease:{{payload.pull_request.head.ref}}"},
     "condition": {"path": "$.held_by_run.alive", "none_in": [true]}}

Held by a live run: the run is skipped with zero tokens. Held by a run that
ended, no lease at all, or a payload without the field: the run goes ahead.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.services import assignment_engine
from app.services.assignment_activation_check import (
    UnresolvedPlaceholderError,
    condition_met,
    parse_activation_check,
    resolved_arguments,
    run_activation_check,
    skip_summary,
)
from app.tools.base import CallableToolExecutor, CompositeToolExecutor, ToolDefinition, ToolResult

LEASE_CHECK = {
    "tool": "get_shared_state",
    "arguments": {"key": "lease:{{payload.pull_request.head.ref}}"},
    "condition": {"path": "$.held_by_run.alive", "none_in": [True]},
}
PAYLOAD = {"action": "created", "pull_request": {"number": 12, "head": {"ref": "issue/12-fix"}}}
HELD_ALIVE = {"key": "lease:issue/12-fix", "held_by_run": {"id": "r1", "status": "running", "alive": True}}
HELD_ENDED = {"key": "lease:issue/12-fix", "held_by_run": {"id": "r1", "status": "failed", "alive": False}}


def _executor(state: dict, seen: dict) -> CompositeToolExecutor:
    async def _get(key: str = ""):
        seen["key"] = key
        return ToolResult("", "state", structured=state)

    inner = CallableToolExecutor()
    inner.register("get_shared_state", _get)
    composite = CompositeToolExecutor()
    composite.add_tools([ToolDefinition(name="get_shared_state", description="read")], inner)
    return composite


def test_the_lease_check_is_accepted_at_save():
    value, error, _ = parse_activation_check(LEASE_CHECK)
    assert error is None
    assert value["condition"] == {"path": "$.held_by_run.alive", "none_in": [True]}


@pytest.mark.parametrize(
    ("data", "met"),
    [
        (HELD_ALIVE, False),
        (HELD_ENDED, True),
        ({"key": "lease:x"}, True),
        ({}, True),
    ],
)
def test_none_in_passes_unless_a_reached_value_is_listed(data, met):
    assert condition_met(data, LEASE_CHECK["condition"]) is met


def test_payload_placeholders_resolve_plain_values_only():
    assert resolved_arguments({"key": "lease:{{payload.pull_request.head.ref}}"}, {}, PAYLOAD) == {
        "key": "lease:issue/12-fix"
    }
    assert resolved_arguments({"n": "{{ payload.pull_request.number }}"}, {}, PAYLOAD) == {"n": "12"}
    for path in ("pull_request.base.ref", "pull_request.head", "missing"):
        with pytest.raises(UnresolvedPlaceholderError):
            resolved_arguments({"key": "{{payload." + path + "}}"}, {}, PAYLOAD)
    with pytest.raises(UnresolvedPlaceholderError):
        resolved_arguments({"key": "{{payload.a}}"}, {}, None)


async def test_a_lease_held_by_a_live_run_fails_the_check():
    seen: dict = {}
    outcome = await run_activation_check(LEASE_CHECK, _executor(HELD_ALIVE, seen), payload=PAYLOAD)
    assert seen == {"key": "lease:issue/12-fix"}
    assert not outcome.passed
    assert outcome.summary == skip_summary("get_shared_state", LEASE_CHECK["condition"])
    assert outcome.summary == "Skipped: gate check get_shared_state: $.held_by_run.alive is in [true]"


@pytest.mark.parametrize("state", [HELD_ENDED, {}])
async def test_an_ended_holder_or_no_lease_lets_the_run_go_ahead(state):
    outcome = await run_activation_check(LEASE_CHECK, _executor(state, {}), payload=PAYLOAD)
    assert outcome.passed and outcome.error is None


async def test_a_payload_without_the_branch_leaves_the_check_not_evaluated():
    seen: dict = {}
    outcome = await run_activation_check(LEASE_CHECK, _executor(HELD_ALIVE, seen), payload={"issue": {"number": 3}})
    assert outcome.passed
    assert "pull_request.head.ref" in (outcome.error or "")
    assert seen == {}


async def test_the_engine_hands_the_run_trigger_payload_to_the_check(monkeypatch):
    published: list = []

    async def _publish(*args, **kwargs):
        published.append(args)

    async def _release(run, agent, *, failed, error=None):
        return None

    async def _executor_for(*_args, **_kwargs):
        return _executor(HELD_ALIVE, {})

    class _Db:
        async def commit(self):
            return None

    monkeypatch.setattr(assignment_engine, "_publish_run_status", _publish)
    monkeypatch.setattr(assignment_engine, "_release_run_work_item", _release)
    monkeypatch.setattr(assignment_engine, "_activation_check_executor", _executor_for)
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
        trigger_payload=PAYLOAD,
    )
    agent = SimpleNamespace(id=uuid.uuid4(), name="Desk")
    assignment = SimpleNamespace(id=uuid.uuid4(), name="Review comment", activation_check=LEASE_CHECK)

    skipped = await assignment_engine._skip_by_activation_check(
        _Db(), run=run, agent=agent, assignment=assignment, effective_user_id=run.user_id
    )

    assert skipped is True
    assert run.result_summary.endswith("$.held_by_run.alive is in [true]")
    assert (run.total_input_tokens, run.total_output_tokens) == (0, 0)


async def _gate_tools(builtin_tool_ids: list[str], allowed: dict | None = None) -> list[str]:
    """The tools the gate's executor holds for an agent with *builtin_tool_ids* (#8682)."""
    agent = SimpleNamespace(
        id=uuid.uuid4(),
        name="Desk",
        builtin_tool_ids=builtin_tool_ids,
        builtin_allowed_tools=allowed,
        is_company_managed=False,
        user_id=uuid.uuid4(),
        integration_bindings={},
        integration_template_bindings=[],
    )
    run = SimpleNamespace(id=uuid.uuid4(), user_id=uuid.uuid4(), is_company_managed=False)
    assignment = SimpleNamespace(id=uuid.uuid4(), name="Review comment", integration_bindings={})
    executor = await assignment_engine._activation_check_executor(
        None,
        tool="get_shared_state",
        run=run,
        agent=agent,
        assignment=assignment,
        effective_user_id=run.user_id,
    )
    return [definition.name for definition in executor.all_tools]


@pytest.mark.parametrize("builtin_tool_ids", [["coordination"], ["shared_state"]])
async def test_the_gate_finds_get_shared_state_on_the_merged_group_and_the_former_id(builtin_tool_ids):
    assert "get_shared_state" in await _gate_tools(builtin_tool_ids)


async def test_the_gate_keeps_the_allow_list_of_the_merged_group():
    tools = await _gate_tools(["coordination"], {"coordination": ["list_work_items", "get_work_item"]})
    assert "get_shared_state" not in tools
    assert "list_work_items" in tools
