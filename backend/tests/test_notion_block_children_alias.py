from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.integrations.providers.notion import build_tools
from app.services.integration_tools import _SafeIntegrationExecutor
from app.tools.base import ToolCall


def test_list_notion_block_children_schema_accepts_page_id_alias():
    executor = _SafeIntegrationExecutor()
    definitions = build_tools("dummy-token", executor)
    tool = next(t for t in definitions if t.name == "list_notion_block_children")

    properties = tool.input_schema["properties"]
    assert "block_id" in properties
    assert "page_id" in properties
    assert tool.input_schema["required"] == []


def test_get_notion_block_schema_accepts_page_id_alias():
    executor = _SafeIntegrationExecutor()
    definitions = build_tools("dummy-token", executor)
    tool = next(t for t in definitions if t.name == "get_notion_block")

    properties = tool.input_schema["properties"]
    assert "block_id" in properties
    assert "page_id" in properties
    assert tool.input_schema["required"] == []


@pytest.mark.asyncio
async def test_list_notion_block_children_accepts_page_id_alias():
    executor = _SafeIntegrationExecutor()
    build_tools("dummy-token", executor)

    with patch("app.integrations.providers.notion._common.safe_request", new_callable=AsyncMock) as mock_request:
        mock_request.return_value = (
            httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "id": "child-1",
                            "type": "paragraph",
                            "has_children": False,
                            "paragraph": {
                                "rich_text": [{"plain_text": "Hello from Notion"}]
                            },
                        }
                    ],
                    "has_more": False,
                },
            ),
            None,
        )

        result = await executor.execute_tool(
            ToolCall(
                id="call-1",
                name="list_notion_block_children",
                arguments={"page_id": "page-123", "count": 1},
            )
        )

    assert not result.is_error
    assert "Hello from Notion" in result.content
    assert "/blocks/page-123/children" in mock_request.call_args.args[2]


@pytest.mark.asyncio
async def test_list_notion_block_children_accepts_block_id():
    executor = _SafeIntegrationExecutor()
    build_tools("dummy-token", executor)

    with patch("app.integrations.providers.notion._common.safe_request", new_callable=AsyncMock) as mock_request:
        mock_request.return_value = (
            httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "id": "child-2",
                            "type": "paragraph",
                            "has_children": False,
                            "paragraph": {
                                "rich_text": [{"plain_text": "Canonical block_id path"}]
                            },
                        }
                    ],
                    "has_more": False,
                },
            ),
            None,
        )

        result = await executor.execute_tool(
            ToolCall(
                id="call-2",
                name="list_notion_block_children",
                arguments={"block_id": "block-456", "count": 1},
            )
        )

    assert not result.is_error
    assert "Canonical block_id path" in result.content
    assert "/blocks/block-456/children" in mock_request.call_args.args[2]


@pytest.mark.asyncio
async def test_list_notion_block_children_rejects_conflicting_ids():
    executor = _SafeIntegrationExecutor()
    build_tools("dummy-token", executor)

    result = await executor.execute_tool(
        ToolCall(
            id="call-3",
            name="list_notion_block_children",
            arguments={"block_id": "block-aaa", "page_id": "page-bbb"},
        )
    )

    assert result.is_error
    assert "Conflicting block identifiers" in result.content


@pytest.mark.asyncio
async def test_list_notion_block_children_rejects_missing_identifiers():
    executor = _SafeIntegrationExecutor()
    build_tools("dummy-token", executor)

    result = await executor.execute_tool(
        ToolCall(
            id="call-4",
            name="list_notion_block_children",
            arguments={},
        )
    )

    assert result.is_error
    assert "Missing block identifier" in result.content


@pytest.mark.asyncio
async def test_list_notion_block_children_rejects_non_string_page_id():
    executor = _SafeIntegrationExecutor()
    build_tools("dummy-token", executor)

    result = await executor.execute_tool(
        ToolCall(
            id="call-5",
            name="list_notion_block_children",
            arguments={"page_id": {"id": "page-123"}},
        )
    )

    assert result.is_error
    assert "Invalid 'page_id' argument" in result.content


@pytest.mark.asyncio
async def test_get_notion_block_accepts_page_id_alias():
    executor = _SafeIntegrationExecutor()
    build_tools("dummy-token", executor)

    with patch("app.integrations.providers.notion._common.safe_request", new_callable=AsyncMock) as mock_request:
        mock_request.return_value = (
            httpx.Response(
                200,
                json={
                    "id": "page-999",
                    "type": "paragraph",
                    "has_children": False,
                    "paragraph": {"rich_text": [{"plain_text": "Single block content"}]},
                },
            ),
            None,
        )

        result = await executor.execute_tool(
            ToolCall(
                id="call-6",
                name="get_notion_block",
                arguments={"page_id": "page-999"},
            )
        )

    assert not result.is_error
    assert "Block: page-999" in result.content
    assert "/blocks/page-999" in mock_request.call_args.args[2]


@pytest.mark.asyncio
async def test_get_notion_block_accepts_block_id():
    """Canonical block_id path on get_notion_block — locks contract
    symmetry with list_notion_block_children so a future change cannot
    silently break only the singular endpoint."""
    executor = _SafeIntegrationExecutor()
    build_tools("dummy-token", executor)

    with patch("app.integrations.providers.notion._common.safe_request", new_callable=AsyncMock) as mock_request:
        mock_request.return_value = (
            httpx.Response(
                200,
                json={
                    "id": "block-7",
                    "type": "paragraph",
                    "has_children": False,
                    "paragraph": {"rich_text": [{"plain_text": "Canonical block path"}]},
                },
            ),
            None,
        )

        result = await executor.execute_tool(
            ToolCall(
                id="call-7",
                name="get_notion_block",
                arguments={"block_id": "block-7"},
            )
        )

    assert not result.is_error
    assert "Block: block-7" in result.content
    assert "/blocks/block-7" in mock_request.call_args.args[2]


@pytest.mark.asyncio
async def test_get_notion_block_rejects_conflicting_ids():
    """get_notion_block must reject conflicting block_id/page_id values
    just like list_notion_block_children does — verifies the shared
    _resolve_block_identifier contract on both call sites."""
    executor = _SafeIntegrationExecutor()
    build_tools("dummy-token", executor)

    result = await executor.execute_tool(
        ToolCall(
            id="call-8",
            name="get_notion_block",
            arguments={"block_id": "block-aaa", "page_id": "page-bbb"},
        )
    )

    assert result.is_error
    assert "Conflicting block identifiers" in result.content


@pytest.mark.asyncio
async def test_get_notion_block_rejects_missing_identifiers():
    """Missing both identifiers must surface as a structured ToolResult
    error, not a TypeError from the handler signature."""
    executor = _SafeIntegrationExecutor()
    build_tools("dummy-token", executor)

    result = await executor.execute_tool(
        ToolCall(id="call-9", name="get_notion_block", arguments={})
    )

    assert result.is_error
    assert "Missing block identifier" in result.content


@pytest.mark.asyncio
async def test_block_tools_accept_same_value_in_both_block_and_page_id():
    """The conflict guard message explicitly invites callers to ``pass
    the same value for both`` — locking that contract prevents a future
    over-strict 'no duplicates' tightening from breaking models that
    redundantly forward both keys with the same value."""
    executor = _SafeIntegrationExecutor()
    build_tools("dummy-token", executor)

    with patch("app.integrations.providers.notion._common.safe_request", new_callable=AsyncMock) as mock_request:
        mock_request.return_value = (
            httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "id": "child-1",
                            "type": "paragraph",
                            "has_children": False,
                            "paragraph": {"rich_text": [{"plain_text": "Same value, no conflict"}]},
                        }
                    ],
                    "has_more": False,
                },
            ),
            None,
        )

        result = await executor.execute_tool(
            ToolCall(
                id="call-10",
                name="list_notion_block_children",
                arguments={"block_id": "same-id", "page_id": "same-id", "count": 1},
            )
        )

    assert not result.is_error
    assert "Same value, no conflict" in result.content
    assert "/blocks/same-id/children" in mock_request.call_args.args[2]


@pytest.mark.asyncio
async def test_block_tools_treat_whitespace_only_id_as_missing():
    """A whitespace-only identifier (``'   '``) must be treated the same
    way as a missing one — surfacing a structured error rather than
    being passed through to the Notion API as ``/blocks/   /children``
    where it would generate an opaque HTTP 400.
    """
    executor = _SafeIntegrationExecutor()
    build_tools("dummy-token", executor)

    result = await executor.execute_tool(
        ToolCall(
            id="call-11",
            name="list_notion_block_children",
            arguments={"block_id": "   "},
        )
    )

    assert result.is_error
    assert "Missing block identifier" in result.content
