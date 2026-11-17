"""The agent builder's assignment tools accept the ``work_item`` trigger.

``create_assignment`` / ``update_assignment`` take the same five trigger types
as the REST API and the UI. A ``work_item`` trigger makes the assignment its
agent's inbox; its options are the ones the trigger editor stores and the
dispatch reads (``correlation_prefix``, ``priority_min``, ``owner_agent_id``).
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

import app.core.database as database
import app.services.builtin_tools as builtin_tools_module
from app.services.builtin_tools.agent_builder import (
    ASSIGNMENT_TRIGGER_TYPES,
    work_item_trigger_error,
)
from app.tools.base import ToolCall
from tests.test_agent_builder_tool import (
    _DedicatedAssignmentSession,
    _SharedSession,
    _writable_assignment_from_session,
)

# ---------------------------------------------------------------------------
# The validator
# ---------------------------------------------------------------------------


def test_the_tools_accept_the_six_trigger_types():
    assert ASSIGNMENT_TRIGGER_TYPES == (
        "schedule",
        "webhook",
        "integration_event",
        "manual",
        "work_item",
        "platform_event",
    )


@pytest.mark.parametrize(
    "config",
    [
        {},
        {"correlation_prefix": "dev:"},
        {"priority_min": "high"},
        {"priority_min": ""},
        {"owner_agent_id": str(uuid.uuid4())},
        {"correlation_prefix": "dev:", "priority_min": "urgent"},
    ],
)
def test_a_work_item_trigger_takes_the_editor_options(config):
    assert work_item_trigger_error("work_item", config) is None


@pytest.mark.parametrize(
    ("config", "fragment"),
    [
        ([], "JSON object"),
        ({"correlation_prefix": 3}, "correlation_prefix"),
        ({"priority_min": "critical"}, "priority_min must be one of low, medium, high, urgent"),
        ({"owner_agent_id": "not-an-id"}, "owner_agent_id"),
    ],
)
def test_a_malformed_work_item_trigger_is_refused(config, fragment):
    error = work_item_trigger_error("work_item", config)
    assert error is not None and fragment in error


@pytest.mark.parametrize("trigger_type", ["schedule", "webhook", "integration_event", "manual"])
def test_the_other_trigger_types_are_not_judged_by_it(trigger_type):
    assert work_item_trigger_error(trigger_type, {"priority_min": "critical"}) is None


# ---------------------------------------------------------------------------
# create_assignment
# ---------------------------------------------------------------------------


class _CreateSession:
    def __init__(self, sink: list):
        self.sink = sink
        self.commits = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    def add(self, row):
        self.sink.append(row)

    async def commit(self):
        self.commits += 1


def _create_executor(monkeypatch, added: list, agent: SimpleNamespace):
    monkeypatch.setattr(database, "async_session", lambda: _CreateSession(added))

    async def _gate(_db, _uid, _agent_uuid, *, action="modify", assignment_write=False):
        return agent, None

    monkeypatch.setattr(builtin_tools_module, "_load_writable_agent_for_builder", _gate)
    _, executor = builtin_tools_module.build_agent_builder_tool(_SharedSession(), str(uuid.uuid4()))
    return executor


async def test_create_assignment_makes_a_work_item_inbox(monkeypatch):
    agent = SimpleNamespace(
        id=uuid.uuid4(),
        name="Desk",
        user_id=uuid.uuid4(),
        is_company_managed=False,
        builtin_tool_ids=["shared_state"],
    )
    added: list = []
    executor = _create_executor(monkeypatch, added, agent)

    result = await executor.execute_tool(
        ToolCall(
            id="1",
            name="create_assignment",
            arguments={
                "agent_id": str(agent.id),
                "name": "Inbox",
                "prompt": "Work the item",
                "trigger_type": "work_item",
                "trigger_config": '{"correlation_prefix": "dev:"}',
            },
        )
    )

    (row,) = added
    assert row.trigger_type == "work_item"
    assert row.trigger_config == {"correlation_prefix": "dev:"}
    assert "Trigger: work_item" in result.content
    # The inbox can manage its queue, like a REST create.
    assert agent.builtin_tool_ids == ["shared_state", "work_items"]


async def test_create_assignment_refuses_a_bad_work_item_option(monkeypatch):
    agent = SimpleNamespace(id=uuid.uuid4(), name="Desk", user_id=uuid.uuid4(), is_company_managed=False)
    added: list = []
    executor = _create_executor(monkeypatch, added, agent)

    result = await executor.execute_tool(
        ToolCall(
            id="1",
            name="create_assignment",
            arguments={
                "agent_id": str(agent.id),
                "name": "Inbox",
                "prompt": "Work the item",
                "trigger_type": "work_item",
                "trigger_config": '{"priority_min": "critical"}',
            },
        )
    )

    assert "priority_min must be one of" in result.content
    assert not added


async def test_create_assignment_still_refuses_an_unknown_trigger(monkeypatch):
    agent = SimpleNamespace(id=uuid.uuid4(), name="Desk", user_id=uuid.uuid4(), is_company_managed=False)
    added: list = []
    executor = _create_executor(monkeypatch, added, agent)

    result = await executor.execute_tool(
        ToolCall(
            id="1",
            name="create_assignment",
            arguments={"agent_id": str(agent.id), "name": "x", "prompt": "p", "trigger_type": "cron"},
        )
    )

    assert result.content == (
        "Invalid trigger_type: must be one of schedule, webhook, integration_event, manual, work_item, platform_event"
    )
    assert not added


# ---------------------------------------------------------------------------
# update_assignment
# ---------------------------------------------------------------------------


def _update_executor(monkeypatch, assignment, agent):
    sessions: list = []
    monkeypatch.setattr(
        database,
        "async_session",
        lambda: _DedicatedAssignmentSession(sessions, {assignment.id: assignment}, {agent.id: agent}),
    )
    monkeypatch.setattr(
        builtin_tools_module, "_load_writable_assignment_for_builder", _writable_assignment_from_session
    )
    _, executor = builtin_tools_module.build_agent_builder_tool(_SharedSession(), str(uuid.uuid4()))
    return executor, sessions


def _schedule_assignment(agent_id):
    return SimpleNamespace(
        id=uuid.uuid4(),
        name="Hourly pick-up",
        prompt="Drain the queue",
        activation_prompt=None,
        trigger_type="schedule",
        trigger_config={"cron": "0 * * * *"},
        delay=None,
        enabled=True,
        user_id=uuid.uuid4(),
        agent_id=agent_id,
    )


async def test_update_assignment_switches_a_schedule_to_the_work_item_trigger(monkeypatch):
    agent = SimpleNamespace(id=uuid.uuid4(), name="Desk", builtin_tool_ids=[])
    assignment = _schedule_assignment(agent.id)
    executor, sessions = _update_executor(monkeypatch, assignment, agent)

    result = await executor.execute_tool(
        ToolCall(
            id="1",
            name="update_assignment",
            arguments={
                "assignment_id": str(assignment.id),
                "trigger_type": "work_item",
                "trigger_config": "{}",
            },
        )
    )

    assert not result.is_error
    assert "trigger_type" in result.content and "trigger_config" in result.content
    assert assignment.trigger_type == "work_item"
    assert assignment.trigger_config == {}
    assert agent.builtin_tool_ids == ["work_items"]
    assert sum(s.commits for s in sessions) == 1


async def test_update_assignment_refuses_a_bad_work_item_option(monkeypatch):
    agent = SimpleNamespace(id=uuid.uuid4(), name="Desk", builtin_tool_ids=[])
    assignment = _schedule_assignment(agent.id)
    executor, sessions = _update_executor(monkeypatch, assignment, agent)

    result = await executor.execute_tool(
        ToolCall(
            id="1",
            name="update_assignment",
            arguments={
                "assignment_id": str(assignment.id),
                "trigger_type": "work_item",
                "trigger_config": '{"owner_agent_id": "nope"}',
            },
        )
    )

    assert "owner_agent_id" in result.content and "Nothing was saved." in result.content
    assert assignment.trigger_type == "schedule"
    assert agent.builtin_tool_ids == []
    assert sum(s.commits for s in sessions) == 0


async def test_update_assignment_still_validates_a_schedule(monkeypatch):
    agent = SimpleNamespace(id=uuid.uuid4(), name="Desk", builtin_tool_ids=[])
    assignment = _schedule_assignment(agent.id)
    executor, sessions = _update_executor(monkeypatch, assignment, agent)

    result = await executor.execute_tool(
        ToolCall(
            id="1",
            name="update_assignment",
            arguments={"assignment_id": str(assignment.id), "trigger_config": '{"cron": "nope"}'},
        )
    )

    assert "Nothing was saved." in result.content
    assert assignment.trigger_config == {"cron": "0 * * * *"}
    assert sum(s.commits for s in sessions) == 0
