"""#2204: an integration auth failure the run already records is never an undisposed friction.

The run's ``IntegrationAuthGuard`` writes a 401 / 403 on
``integration_auth_failures`` and the coverage notice. The guarded executor
marks that result (and the calls it refuses afterwards), and the agent loop
files its friction already disposed of instead of as a ``tool_error``. An
error outside a guarded integration stays a ``tool_error``.
"""

from __future__ import annotations

import pytest

from app.services import frictions
from app.services.integration_auth_guard import (
    AUTH_FAILURE_RECORDED_REF,
    AuthGuardedExecutor,
    IntegrationAuthGuard,
)
from app.tools import agent_loop
from app.tools.base import ToolCall, ToolDefinition, ToolResult


class _Inner:
    def __init__(self, answers: dict[str, str]) -> None:
        self.answers = answers

    async def execute_tool(self, call: ToolCall) -> ToolResult:
        return ToolResult(tool_call_id=call.id, content=self.answers[call.name], is_error=True)


def _executor(answers: dict[str, str]) -> AuthGuardedExecutor:
    defs = [ToolDefinition(name=name, description=name, source="integration:entra") for name in answers]
    return AuthGuardedExecutor(_Inner(answers), defs, IntegrationAuthGuard())


@pytest.mark.asyncio
async def test_a_guarded_403_and_the_calls_refused_after_a_401_are_marked() -> None:
    executor = _executor(
        {
            "list_entra_users": "Error: Entra API returned 403 Forbidden: permission denied for GET /users",
            "list_entra_groups": "Error: 401 Unauthorized - token rejected",
            "list_entra_roles": "never sent",
        }
    )

    forbidden = await executor.execute_tool(ToolCall(id="1", name="list_entra_users", arguments={}))
    rejected = await executor.execute_tool(ToolCall(id="2", name="list_entra_groups", arguments={}))
    refused = await executor.execute_tool(ToolCall(id="3", name="list_entra_roles", arguments={}))

    assert forbidden.auth_failure_recorded == "entra 403"
    assert rejected.auth_failure_recorded == "entra 401"
    assert refused.refused and refused.auth_failure_recorded == "entra 401"


@pytest.mark.asyncio
async def test_an_error_that_is_not_an_auth_failure_is_not_marked() -> None:
    executor = _executor({"list_entra_users": "Error: 500 upstream timeout"})

    result = await executor.execute_tool(ToolCall(id="1", name="list_entra_users", arguments={}))

    assert result.auth_failure_recorded == ""


def test_the_loop_files_a_recorded_auth_failure_disposed_and_the_rest_as_a_tool_error(monkeypatch) -> None:
    recorded: list[tuple[str, str]] = []
    monkeypatch.setattr(frictions, "note_tool_error", lambda tool, content: recorded.append(("tool_error", tool)))
    monkeypatch.setattr(
        frictions,
        "note_expected_error",
        lambda tool, content, reason: recorded.append(("expected_error", reason)),
    )
    call = ToolCall(id="1", name="list_entra_users", arguments={})

    agent_loop._log_tool_result(
        call,
        ToolResult(
            tool_call_id="1",
            content="Error: 403 Forbidden",
            is_error=True,
            auth_failure_recorded="entra 403",
        ),
    )
    agent_loop._log_tool_result(call, ToolResult(tool_call_id="1", content="Error: 403 Forbidden", is_error=True))

    assert recorded == [
        ("expected_error", f"{AUTH_FAILURE_RECORDED_REF}: entra 403"),
        ("tool_error", "list_entra_users"),
    ]
