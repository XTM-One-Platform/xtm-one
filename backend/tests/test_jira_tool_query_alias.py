"""Regression tests for Jira issue search tool argument compatibility.

After the domain-split refactor the search tool lives in
``app.integrations.providers.jira.issues`` and issues its HTTP call through the
shared ``jira_request`` wrapper, so the network mock is patched there.
"""

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.integrations.providers.jira import build_tools
from app.services.integration_tools import _SafeIntegrationExecutor
from app.tools.base import ToolCall

_PATCH_TARGET = "app.integrations.providers.jira.issues.jira_request"


def test_search_jira_issues_schema_accepts_query_alias():
    executor = _SafeIntegrationExecutor()
    definitions = build_tools("cloud-id||token", executor)
    search_tool = next(t for t in definitions if t.name == "search_jira_issues")

    properties = search_tool.input_schema["properties"]
    assert "jql" in properties
    assert "query" in properties
    assert search_tool.input_schema["required"] == []


@pytest.mark.asyncio
async def test_search_jira_issues_accepts_query_argument():
    executor = _SafeIntegrationExecutor()
    build_tools("cloud-id||token", executor)

    with patch(_PATCH_TARGET, new_callable=AsyncMock) as mock_request:
        mock_request.return_value = (
            httpx.Response(
                200,
                json={
                    "issues": [{"key": "DEMO-1", "fields": {"summary": "Demo issue"}}],
                },
            ),
            None,
        )

        result = await executor.execute_tool(
            ToolCall(
                id="call-1",
                name="search_jira_issues",
                arguments={"query": "project = DEMO", "count": 5},
            )
        )

    assert not result.is_error
    assert "Found 1 issue(s)" in result.content
    assert "DEMO-1: Demo issue" in result.content
    assert mock_request.call_args.kwargs["json"]["jql"] == "project = DEMO"


@pytest.mark.asyncio
async def test_search_jira_issues_rejects_conflicting_jql_and_query():
    executor = _SafeIntegrationExecutor()
    build_tools("cloud-id||token", executor)

    result = await executor.execute_tool(
        ToolCall(
            id="call-2",
            name="search_jira_issues",
            arguments={"jql": "project = A", "query": "project = B"},
        )
    )

    assert result.is_error
    assert "Conflicting JQL inputs" in result.content


@pytest.mark.asyncio
async def test_search_jira_issues_rejects_empty_inputs():
    executor = _SafeIntegrationExecutor()
    build_tools("cloud-id||token", executor)

    result = await executor.execute_tool(
        ToolCall(id="call-3", name="search_jira_issues", arguments={})
    )

    assert result.is_error
    assert "Missing JQL query" in result.content


@pytest.mark.asyncio
async def test_search_jira_issues_rejects_non_string_jql_argument():
    """Model-generated tool calls can occasionally send a non-string value
    (e.g. a number, list, or object) for ``jql``/``query``. The handler must
    return a structured ``ToolResult`` error rather than letting ``.strip()``
    raise ``AttributeError`` deep inside the agent loop."""
    executor = _SafeIntegrationExecutor()
    build_tools("cloud-id||token", executor)

    result = await executor.execute_tool(
        ToolCall(
            id="call-4",
            name="search_jira_issues",
            arguments={"jql": {"nested": "object"}},
        )
    )

    assert result.is_error
    assert "Invalid 'jql' argument" in result.content
    assert "dict" in result.content


@pytest.mark.asyncio
async def test_search_jira_issues_rejects_non_string_query_argument():
    """Same contract as the ``jql`` type check but for the alias."""
    executor = _SafeIntegrationExecutor()
    build_tools("cloud-id||token", executor)

    result = await executor.execute_tool(
        ToolCall(
            id="call-5",
            name="search_jira_issues",
            arguments={"query": 12345},
        )
    )

    assert result.is_error
    assert "Invalid 'query' argument" in result.content
    assert "int" in result.content


@pytest.mark.asyncio
async def test_search_jira_issues_rejects_list_argument():
    """Lists must also be rejected - a common shape mistake when the model tries
    to send multiple JQL fragments."""
    executor = _SafeIntegrationExecutor()
    build_tools("cloud-id||token", executor)

    result = await executor.execute_tool(
        ToolCall(
            id="call-6",
            name="search_jira_issues",
            arguments={"jql": ["project = A", "project = B"]},
        )
    )

    assert result.is_error
    assert "Invalid 'jql' argument" in result.content
    assert "list" in result.content


def test_search_jira_issues_return_annotation_includes_tool_result():
    """``search_jira_issues`` mixes ``str`` (success) and ``ToolResult`` (error)
    returns; the annotation must reflect both so type checkers and callers don't
    silently miss the error branch."""
    import typing

    from app.tools.base import ToolResult as _ToolResult

    executor = _SafeIntegrationExecutor()
    build_tools("cloud-id||token", executor)
    handler = executor._handlers["search_jira_issues"]
    return_type = typing.get_type_hints(handler).get("return")

    args = set(typing.get_args(return_type))
    assert str in args
    assert _ToolResult in args
