"""Regression tests for list_all_planhat_tasks compound tool.

The CS Ops Daily Digest sub-agent timed out at 600s while fetching all
Planhat tasks one company at a time.  ``list_all_planhat_tasks`` fixes this
by fetching all tasks in a single ``/tasks`` call and grouping results by
company name, reducing O(N companies) tool calls to O(1).

Tests cover:
- Happy path: tasks from multiple companies are returned grouped by company.
- ``include_done=False`` (default) filters out archived tasks client-side.
- ``include_done=True`` includes archived tasks.
- ``count`` is clamped to [1, 2000].
- Empty API response returns an appropriate message.
- API error is surfaced directly to the caller.
- A single API call is made regardless of the number of companies present.
- A truncation note is appended when the API limit was reached.
- Tasks without ``companyName`` fall back to ``companyId`` / "Unknown" in
  both the group header and the per-task line.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

import app.integrations.providers.planhat as planhat_mod
from app.tools.base import CallableToolExecutor, ToolCall

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _StubResponse:
    def __init__(self, payload: Any) -> None:
        self._payload = payload

    def json(self) -> Any:
        return self._payload


def _build_executor(monkeypatch, response: Any) -> tuple[CallableToolExecutor, AsyncMock]:
    """Patch ``safe_request`` in planhat module and build a tool executor."""
    recorder = AsyncMock(return_value=(_StubResponse(response), None))
    monkeypatch.setattr(planhat_mod, "safe_request", recorder)
    executor = CallableToolExecutor()
    planhat_mod.build_tools(token="dummy-token", executor=executor)
    return executor, recorder


def _build_error_executor(monkeypatch, error_msg: str) -> tuple[CallableToolExecutor, AsyncMock]:
    """Patch ``safe_request`` to simulate an API error."""
    recorder = AsyncMock(return_value=(None, error_msg))
    monkeypatch.setattr(planhat_mod, "safe_request", recorder)
    executor = CallableToolExecutor()
    planhat_mod.build_tools(token="dummy-token", executor=executor)
    return executor, recorder


_TASK_OPEN = {
    "_id": "t1",
    "action": "Send QBR slides",
    "status": "In-progress",
    "startTime": "2026-07-15T10:00:00Z",
    "isArchived": False,
    "companyName": "Acme Corp",
    "companyId": "cid-1",
}
_TASK_DONE = {
    "_id": "t2",
    "action": "Old follow-up",
    "status": "Done",
    "startTime": "2026-06-01T09:00:00Z",
    "isArchived": True,
    "companyName": "Acme Corp",
    "companyId": "cid-1",
}
_TASK_OTHER_COMPANY = {
    "_id": "t3",
    "action": "Onboarding call",
    "status": "In-progress",
    "startTime": "2026-07-16T14:00:00Z",
    "isArchived": False,
    "companyName": "Beta Inc",
    "companyId": "cid-2",
}


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_all_planhat_tasks_groups_by_company(monkeypatch):
    """Open tasks from multiple companies appear grouped by company name."""
    executor, _ = _build_executor(monkeypatch, [_TASK_OPEN, _TASK_OTHER_COMPANY])
    result = await executor.execute_tool(ToolCall(id="c1", name="list_all_planhat_tasks", arguments={}))
    assert not result.is_error
    text = result.content
    assert "Acme Corp" in text
    assert "Beta Inc" in text
    assert "Send QBR slides" in text
    assert "Onboarding call" in text
    assert "2 open tasks across 2 companies" in text


@pytest.mark.asyncio
async def test_list_all_planhat_tasks_makes_single_api_call(monkeypatch):
    """The compound tool issues exactly one HTTP request, not one per company."""
    executor, recorder = _build_executor(monkeypatch, [_TASK_OPEN, _TASK_OTHER_COMPANY])
    await executor.execute_tool(ToolCall(id="c2", name="list_all_planhat_tasks", arguments={}))
    assert recorder.call_count == 1


# ---------------------------------------------------------------------------
# include_done flag
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_all_planhat_tasks_excludes_done_by_default(monkeypatch):
    """Archived tasks are excluded when include_done is False (default)."""
    executor, _ = _build_executor(monkeypatch, [_TASK_OPEN, _TASK_DONE])
    result = await executor.execute_tool(
        ToolCall(id="c3", name="list_all_planhat_tasks", arguments={"include_done": False})
    )
    assert not result.is_error
    text = result.content
    assert "Send QBR slides" in text
    assert "Old follow-up" not in text
    assert "1 open task across 1 company" in text


@pytest.mark.asyncio
async def test_list_all_planhat_tasks_includes_done_when_requested(monkeypatch):
    """Archived tasks are included when include_done=True."""
    executor, _ = _build_executor(monkeypatch, [_TASK_OPEN, _TASK_DONE])
    result = await executor.execute_tool(
        ToolCall(id="c4", name="list_all_planhat_tasks", arguments={"include_done": True})
    )
    assert not result.is_error
    text = result.content
    assert "Send QBR slides" in text
    assert "Old follow-up" in text
    assert "2 total tasks across 1 company" in text


# ---------------------------------------------------------------------------
# Empty / error cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_all_planhat_tasks_empty_response(monkeypatch):
    """Empty task list returns a clear message."""
    executor, _ = _build_executor(monkeypatch, [])
    result = await executor.execute_tool(ToolCall(id="c5", name="list_all_planhat_tasks", arguments={}))
    assert not result.is_error
    assert "No open tasks found." in result.content


@pytest.mark.asyncio
async def test_list_all_planhat_tasks_all_done_filtered(monkeypatch):
    """If all tasks are archived and include_done is False, return empty message."""
    executor, _ = _build_executor(monkeypatch, [_TASK_DONE])
    result = await executor.execute_tool(
        ToolCall(id="c6", name="list_all_planhat_tasks", arguments={"include_done": False})
    )
    assert not result.is_error
    assert "No open tasks found." in result.content


@pytest.mark.asyncio
async def test_list_all_planhat_tasks_api_error_surfaced(monkeypatch):
    """API errors returned by safe_request are surfaced directly to the caller."""
    err_msg = "Error: Planhat rate limit exceeded while trying to list all tasks."
    executor, _ = _build_error_executor(monkeypatch, err_msg)
    result = await executor.execute_tool(ToolCall(id="c7", name="list_all_planhat_tasks", arguments={}))
    assert result.is_error
    assert err_msg in result.content


# ---------------------------------------------------------------------------
# count clamping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_all_planhat_tasks_clamps_count_to_max(monkeypatch):
    """count > 2000 is clamped to 2000 before hitting the API."""
    executor, recorder = _build_executor(monkeypatch, [])
    await executor.execute_tool(ToolCall(id="c8", name="list_all_planhat_tasks", arguments={"count": 9999}))
    params = recorder.call_args.kwargs.get("params", {})
    assert params.get("limit") == "2000"


@pytest.mark.asyncio
async def test_list_all_planhat_tasks_clamps_count_to_min(monkeypatch):
    """count <= 0 is clamped to 1."""
    executor, recorder = _build_executor(monkeypatch, [])
    await executor.execute_tool(ToolCall(id="c9", name="list_all_planhat_tasks", arguments={"count": -5}))
    params = recorder.call_args.kwargs.get("params", {})
    assert params.get("limit") == "1"


# ---------------------------------------------------------------------------
# Truncation note when the API limit was reached
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_all_planhat_tasks_truncation_note_when_limit_hit(monkeypatch):
    """When the API returns exactly `count` tasks, a truncation note is added."""
    executor, _ = _build_executor(monkeypatch, [_TASK_OPEN, _TASK_OTHER_COMPANY])
    result = await executor.execute_tool(ToolCall(id="c11", name="list_all_planhat_tasks", arguments={"count": 2}))
    assert not result.is_error
    assert "count limit reached" in result.content


@pytest.mark.asyncio
async def test_list_all_planhat_tasks_truncation_note_on_empty_open_result(monkeypatch):
    """The truncation note also applies when all fetched tasks were archived.

    With include_done=False, an all-archived page that hit the API limit must
    not claim "No open tasks found." unqualified - open tasks may exist
    beyond the limit because the limit applies before the archived filter.
    """
    executor, _ = _build_executor(monkeypatch, [_TASK_DONE])
    result = await executor.execute_tool(ToolCall(id="c13", name="list_all_planhat_tasks", arguments={"count": 1}))
    assert not result.is_error
    assert "No open tasks found." in result.content
    assert "count limit reached" in result.content


@pytest.mark.asyncio
async def test_list_all_planhat_tasks_no_truncation_note_below_limit(monkeypatch):
    """No truncation note appears when fewer than `count` tasks were fetched."""
    executor, _ = _build_executor(monkeypatch, [_TASK_OPEN, _TASK_OTHER_COMPANY])
    result = await executor.execute_tool(ToolCall(id="c12", name="list_all_planhat_tasks", arguments={"count": 50}))
    assert not result.is_error
    assert "count limit reached" not in result.content


# ---------------------------------------------------------------------------
# Company fallback when companyName is absent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_all_planhat_tasks_falls_back_to_company_id(monkeypatch):
    """Tasks without companyName fall back to companyId, then 'Unknown'.

    The resolved label must also be used in the per-task line rendered by
    ``_fmt_task`` — not just the group header — so the output never shows
    ``Company: ?`` for a task that was grouped under a real fallback label.
    """
    tasks = [
        {
            "_id": "t10",
            "action": "No-name task",
            "status": "In-progress",
            "startTime": "2026-07-10",
            "isArchived": False,
            "companyId": "cid-fallback",
            # no companyName key
        },
        {
            "_id": "t11",
            "action": "Totally unknown",
            "status": "In-progress",
            "startTime": "2026-07-11",
            "isArchived": False,
            # no companyName, no companyId
        },
    ]
    executor, _ = _build_executor(monkeypatch, tasks)
    result = await executor.execute_tool(ToolCall(id="c10", name="list_all_planhat_tasks", arguments={}))
    assert not result.is_error
    text = result.content
    assert "cid-fallback" in text
    assert "Unknown" in text
    assert "Company: cid-fallback" in text
    assert "Company: Unknown" in text
    assert "Company: ?" not in text
