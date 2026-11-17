"""Regression tests for Microsoft To-Do tools.

Tests cover:
- ``list_microsoft_tasks`` default filter (incomplete only, ``status ne 'completed'``).
- ``list_microsoft_tasks`` with ``include_completed=True`` omits the filter.
- ``list_all_microsoft_tasks`` fetches lists then tasks concurrently in one call,
  returning a consolidated view — the core fix for iteration-budget exhaustion in
  To-Do manager assignments.
- ``list_all_microsoft_tasks`` clamps ``count_per_list`` to [1, 100].
- ``list_all_microsoft_tasks`` empty-list and all-completed-tasks edge cases.
- ``list_all_microsoft_tasks`` surfaces per-list fetch failures instead of
  silently reporting an errored list as empty.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.integrations.providers.microsoft import build_tools
from app.tools.base import CallableToolExecutor, ToolCall

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _StubResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def json(self) -> dict[str, Any]:
        return self._payload


# Dispatch table keyed on URL fragment.
class _MultiRouteRecorder:
    """Stand-in for ``safe_request`` that routes responses by URL suffix."""

    def __init__(self, routes: dict[str, dict[str, Any] | str]) -> None:
        """``routes``: maps a URL substring to a JSON payload dict.

        A ``str`` value simulates a ``safe_request`` failure for that route:
        the recorder returns ``(None, <error string>)`` instead of a response.

        Longer keys take precedence over shorter ones so that
        ``/me/todo/lists/list-1/tasks`` is matched before the shorter
        ``/me/todo/lists`` prefix.
        """
        # Pre-sort by key length descending for most-specific-first matching.
        self.routes = dict(sorted(routes.items(), key=lambda kv: len(kv[0]), reverse=True))
        self.calls: list[dict[str, Any]] = []

    async def __call__(
        self, client, method, url, provider, op, **kwargs
    ) -> tuple[_StubResponse | None, str | None]:
        self.calls.append({"method": method, "url": url, "op": op, **kwargs})
        for key, payload in self.routes.items():
            if key in url:
                if isinstance(payload, str):
                    return None, payload
                return _StubResponse(payload), None
        # Default empty response
        return _StubResponse({"value": []}), None


def _build_executor(
    monkeypatch, routes: dict[str, dict[str, Any] | str]
) -> tuple[CallableToolExecutor, _MultiRouteRecorder]:
    from app.integrations.providers import microsoft as ms

    recorder = _MultiRouteRecorder(routes)
    monkeypatch.setattr(ms._common, "safe_request", recorder)
    executor = CallableToolExecutor()
    build_tools(token="dummy", executor=executor)
    return executor, recorder


# ---------------------------------------------------------------------------
# list_microsoft_tasks — default incomplete-only filter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_microsoft_tasks_filters_incomplete_by_default(monkeypatch):
    """``list_microsoft_tasks`` must send ``$filter=status ne 'completed'`` by default."""
    list_id = "list-abc"
    executor, recorder = _build_executor(
        monkeypatch,
        {
            f"/me/todo/lists/{list_id}/tasks": {
                "value": [
                    {"id": "t1", "title": "Buy milk", "status": "notStarted"},
                ]
            }
        },
    )

    result = await executor.execute_tool(
        ToolCall(id="c1", name="list_microsoft_tasks", arguments={"list_id": list_id})
    )
    assert not result.is_error

    task_calls = [c for c in recorder.calls if f"/me/todo/lists/{list_id}/tasks" in c["url"]]
    assert task_calls, "Expected a request for tasks"
    params = task_calls[0].get("params", {})
    assert params.get("$filter") == "status ne 'completed'"


@pytest.mark.asyncio
async def test_list_microsoft_tasks_include_completed_omits_filter(monkeypatch):
    """``list_microsoft_tasks(include_completed=True)`` must NOT send the status filter."""
    list_id = "list-abc"
    executor, recorder = _build_executor(
        monkeypatch,
        {
            f"/me/todo/lists/{list_id}/tasks": {
                "value": [
                    {"id": "t1", "title": "Done task", "status": "completed"},
                ]
            }
        },
    )

    result = await executor.execute_tool(
        ToolCall(
            id="c2",
            name="list_microsoft_tasks",
            arguments={"list_id": list_id, "include_completed": True},
        )
    )
    assert not result.is_error

    task_calls = [c for c in recorder.calls if f"/me/todo/lists/{list_id}/tasks" in c["url"]]
    assert task_calls, "Expected a request for tasks"
    params = task_calls[0].get("params", {})
    assert "$filter" not in params, "Completed filter should be absent when include_completed=True"


# ---------------------------------------------------------------------------
# list_all_microsoft_tasks — compound O(1) tool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_all_microsoft_tasks_returns_consolidated_view(monkeypatch):
    """Single tool call returns tasks grouped by all lists."""
    executor, _recorder = _build_executor(
        monkeypatch,
        {
            "/me/todo/lists": {
                "value": [
                    {"id": "list-1", "displayName": "Work", "isDefaultList": False},
                    {"id": "list-2", "displayName": "Personal", "isDefaultList": True},
                ]
            },
            "/me/todo/lists/list-1/tasks": {
                "value": [
                    {"id": "t1", "title": "Write report", "status": "notStarted"},
                ]
            },
            "/me/todo/lists/list-2/tasks": {
                "value": [
                    {"id": "t2", "title": "Buy groceries", "status": "notStarted"},
                ]
            },
        },
    )

    result = await executor.execute_tool(
        ToolCall(id="c3", name="list_all_microsoft_tasks", arguments={})
    )
    assert not result.is_error
    text = result.content
    assert "Write report" in text
    assert "Buy groceries" in text
    assert "Work" in text
    assert "Personal" in text
    # Summary line
    assert "2 incomplete task(s)" in text


@pytest.mark.asyncio
async def test_list_all_microsoft_tasks_uses_incomplete_filter(monkeypatch):
    """Every per-list tasks request must carry the ``status ne 'completed'`` filter."""
    executor, recorder = _build_executor(
        monkeypatch,
        {
            "/me/todo/lists": {
                "value": [
                    {"id": "list-1", "displayName": "Work", "isDefaultList": True},
                ]
            },
            "/me/todo/lists/list-1/tasks": {
                "value": [
                    {"id": "t1", "title": "Active task", "status": "notStarted"},
                ]
            },
        },
    )

    await executor.execute_tool(
        ToolCall(id="c4", name="list_all_microsoft_tasks", arguments={})
    )

    task_calls = [c for c in recorder.calls if "/tasks" in c["url"] and "todo/lists/" in c["url"]]
    assert task_calls, "Expected at least one tasks request"
    for call in task_calls:
        params = call.get("params", {})
        assert params.get("$filter") == "status ne 'completed'", (
            f"Expected status filter on {call['url']}"
        )


@pytest.mark.asyncio
async def test_list_all_microsoft_tasks_empty_lists(monkeypatch):
    """No task lists returns a friendly message, not an error."""
    executor, _recorder = _build_executor(
        monkeypatch,
        {"/me/todo/lists": {"value": []}},
    )

    result = await executor.execute_tool(
        ToolCall(id="c5", name="list_all_microsoft_tasks", arguments={})
    )
    assert not result.is_error
    assert "No task lists found" in result.content


@pytest.mark.asyncio
async def test_list_all_microsoft_tasks_all_lists_empty_tasks(monkeypatch):
    """All lists exist but have no incomplete tasks returns a friendly message."""
    executor, _recorder = _build_executor(
        monkeypatch,
        {
            "/me/todo/lists": {
                "value": [
                    {"id": "list-1", "displayName": "Work", "isDefaultList": True},
                ]
            },
            "/me/todo/lists/list-1/tasks": {"value": []},
        },
    )

    result = await executor.execute_tool(
        ToolCall(id="c6", name="list_all_microsoft_tasks", arguments={})
    )
    assert not result.is_error
    assert "No incomplete tasks found" in result.content


@pytest.mark.asyncio
async def test_list_all_microsoft_tasks_clamps_count_per_list(monkeypatch):
    """``count_per_list`` is clamped to 1–100; an out-of-range value is corrected silently."""
    executor, recorder = _build_executor(
        monkeypatch,
        {
            "/me/todo/lists": {
                "value": [
                    {"id": "list-1", "displayName": "Work", "isDefaultList": True},
                ]
            },
            "/me/todo/lists/list-1/tasks": {
                "value": [
                    {"id": "t1", "title": "Capped task", "status": "notStarted"},
                ]
            },
        },
    )

    # Pass a ridiculously large count — should be clamped to 100.
    result = await executor.execute_tool(
        ToolCall(
            id="c7",
            name="list_all_microsoft_tasks",
            arguments={"count_per_list": 9999},
        )
    )
    assert not result.is_error

    task_calls = [c for c in recorder.calls if "/tasks" in c["url"] and "todo/lists/" in c["url"]]
    assert task_calls
    params = task_calls[0].get("params", {})
    assert int(params["$top"]) == 100, "count_per_list must be clamped to 100"


@pytest.mark.asyncio
async def test_list_all_microsoft_tasks_makes_single_lists_request(monkeypatch):
    """Only ONE ``/me/todo/lists`` request is made regardless of how many lists exist."""
    executor, recorder = _build_executor(
        monkeypatch,
        {
            "/me/todo/lists": {
                "value": [
                    {"id": "list-1", "displayName": "A", "isDefaultList": False},
                    {"id": "list-2", "displayName": "B", "isDefaultList": True},
                    {"id": "list-3", "displayName": "C", "isDefaultList": False},
                ]
            },
            "/me/todo/lists/list-1/tasks": {"value": []},
            "/me/todo/lists/list-2/tasks": {
                "value": [{"id": "t1", "title": "Task B", "status": "notStarted"}]
            },
            "/me/todo/lists/list-3/tasks": {"value": []},
        },
    )

    result = await executor.execute_tool(
        ToolCall(id="c8", name="list_all_microsoft_tasks", arguments={})
    )
    assert not result.is_error

    lists_calls = [c for c in recorder.calls if c["url"].endswith("/me/todo/lists")]
    assert len(lists_calls) == 1, "Should only fetch the lists index once"

    tasks_calls = [c for c in recorder.calls if "/tasks" in c["url"] and "todo/lists/" in c["url"]]
    assert len(tasks_calls) == 3, "Should fetch tasks from all 3 lists"


@pytest.mark.asyncio
async def test_list_all_microsoft_tasks_surfaces_partial_failures(monkeypatch):
    """A failed per-list fetch must be reported, never silently treated as empty."""
    executor, _recorder = _build_executor(
        monkeypatch,
        {
            "/me/todo/lists": {
                "value": [
                    {"id": "list-1", "displayName": "Work", "isDefaultList": True},
                    {"id": "list-2", "displayName": "Broken", "isDefaultList": False},
                ]
            },
            "/me/todo/lists/list-1/tasks": {
                "value": [{"id": "t1", "title": "Active task", "status": "notStarted"}]
            },
            "/me/todo/lists/list-2/tasks": "Error listing all tasks: 503 Service Unavailable",
        },
    )

    result = await executor.execute_tool(
        ToolCall(id="c9", name="list_all_microsoft_tasks", arguments={})
    )
    assert not result.is_error
    text = result.content
    assert "Active task" in text
    assert "could not fetch tasks from 1 list(s)" in text
    assert "Broken" in text


@pytest.mark.asyncio
async def test_list_all_microsoft_tasks_all_fetches_failed(monkeypatch):
    """When every per-list fetch fails, the output must NOT claim 'no tasks found'."""
    executor, _recorder = _build_executor(
        monkeypatch,
        {
            "/me/todo/lists": {
                "value": [
                    {"id": "list-1", "displayName": "Work", "isDefaultList": True},
                ]
            },
            "/me/todo/lists/list-1/tasks": "Error listing all tasks: 401 Unauthorized",
        },
    )

    result = await executor.execute_tool(
        ToolCall(id="c10", name="list_all_microsoft_tasks", arguments={})
    )
    assert not result.is_error
    text = result.content
    assert "No incomplete tasks found across all lists" not in text
    assert "could not fetch tasks from 1 list(s)" in text
    assert "Work" in text
