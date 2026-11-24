"""run_agent_once(dry_run=true) bars the run's writes (#1911).

The flag rides on the task's metadata; the task's turn is then bound to a
``dry_run`` sandbox session: reads run, writes are logged and answered that
they were not executed.
"""

from __future__ import annotations

import inspect
import uuid

import pytest

from app.services import sub_agent
from app.services.health.sandbox.session import DRY_RUN_NOTICE, SandboxSession, sandbox_session
from app.tools.base import ToolCall, ToolDefinition, ToolResult
from tests.test_emt_agent_builder_gaps import _call, _run_once


@pytest.mark.asyncio
async def test_dry_run_marks_the_task(monkeypatch: pytest.MonkeyPatch) -> None:
    executor, agent, started, _task_id = _run_once(monkeypatch, ["completed"])

    await _call(executor, "run_agent_once", agent_id=str(agent.id), prompt="Send the brief.", dry_run=True)

    assert started[0]["extra_metadata"] == {"origin": "run_agent_once", sub_agent.RUN_DRY_RUN_KEY: True}


@pytest.mark.asyncio
async def test_without_dry_run_the_task_is_unmarked(monkeypatch: pytest.MonkeyPatch) -> None:
    executor, agent, started, _task_id = _run_once(monkeypatch, ["completed"])

    await _call(executor, "run_agent_once", agent_id=str(agent.id), prompt="Send the brief.")

    assert started[0]["extra_metadata"] == {"origin": "run_agent_once"}


def test_the_task_turn_binds_a_dry_run_session_on_the_flag() -> None:
    source = inspect.getsource(sub_agent._execute_sub_agent_task)
    assert "RUN_DRY_RUN_KEY" in source
    assert 'SandboxSession(mode="dry_run")' in source


@pytest.mark.asyncio
async def test_a_dry_run_session_answers_a_write_without_running_it() -> None:
    ran: list[str] = []

    async def _run() -> ToolResult:
        ran.append("ran")
        return ToolResult(tool_call_id="c1", content="sent")

    session = SandboxSession(mode="dry_run")
    with sandbox_session(session):
        result = await session.intercept(
            ToolCall(id="c1", name="send_email", arguments={"to": "x@example.com"}),
            ToolDefinition(
                name="send_email", description="Send an email", input_schema={}, source="integration:microsoft"
            ),
            _run,
        )

    assert ran == []
    assert result.content == DRY_RUN_NOTICE
    assert session.report.intercepted_writes == 1


def test_the_tool_declares_dry_run() -> None:
    from typing import Any

    from app.services.builtin_tools import build_agent_builder_tool

    no_db: Any = None
    definitions, _executor = build_agent_builder_tool(no_db, str(uuid.uuid4()))
    run_once = next(d for d in definitions if d.name == "run_agent_once")
    assert run_once.input_schema["properties"]["dry_run"]["type"] == "boolean"
