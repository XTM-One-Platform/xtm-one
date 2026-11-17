"""Tests for the ``position`` argument handling in the Notion page tools.

LLMs routinely pass natural-language placement words ("bottom", "top", ...)
instead of the documented sentinels. The old code forwarded any
non-'start'/'end' value verbatim as ``position.after_block.id`` and Notion
rejected the whole request with an opaque 400 ("should be a valid uuid,
instead was `\"bottom\"`"). ``_resolve_position`` now maps aliases
deterministically and fail-fasts on garbage BEFORE any API round-trip.
"""

import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.integrations.providers.notion import build_tools
from app.integrations.providers.notion.pages import _resolve_position
from app.services.integration_tools import _SafeIntegrationExecutor
from app.tools.base import ToolCall

_BLOCK_UUID = "550e8400-e29b-41d4-a716-446655440000"


# ── _resolve_position unit behaviour ─────────────────────────────────────


@pytest.mark.parametrize(
    "value",
    ["", "end", "End", "bottom", "BOTTOM", "last", "page_end", "default", "append", "none", "null", "  bottom  ", None],
)
def test_resolve_position_end_aliases_omit_position(value):
    payload, err = _resolve_position(value)
    assert payload is None
    assert err is None


@pytest.mark.parametrize("value", ["start", "Start", "top", "page_start", "beginning", "first"])
def test_resolve_position_start_aliases_map_to_page_start(value):
    payload, err = _resolve_position(value)
    assert payload == {"type": "page_start"}
    assert err is None


def test_resolve_position_accepts_block_uuid():
    payload, err = _resolve_position(_BLOCK_UUID)
    assert err is None
    assert payload == {"type": "after_block", "after_block": {"id": _BLOCK_UUID}}


def test_resolve_position_accepts_dashless_uuid():
    dashless = _BLOCK_UUID.replace("-", "")
    payload, err = _resolve_position(dashless)
    assert err is None
    assert payload == {"type": "after_block", "after_block": {"id": dashless}}


@pytest.mark.parametrize("value", ["after the summary", "below", "under-heading", "block-123"])
def test_resolve_position_rejects_garbage_with_actionable_error(value):
    payload, err = _resolve_position(value)
    assert payload is None
    assert err is not None and err.startswith("Error:")
    assert "'bottom'" in err
    assert "list_notion_block_children" in err


@pytest.mark.parametrize("value", [0, 1, 42.5, True])
def test_resolve_position_non_string_never_raises(value):
    """Tool args are not JSON-schema coerced - a non-string must reach the
    actionable fail-fast error, not raise AttributeError on .strip()."""
    payload, err = _resolve_position(value)
    assert payload is None
    assert err is not None and err.startswith("Error:")


# ── create_notion_page wiring ─────────────────────────────────────────────


def _build_executor() -> _SafeIntegrationExecutor:
    executor = _SafeIntegrationExecutor()
    build_tools("dummy-token", executor)
    return executor


def _page_parent_side_effect(captured_bodies: list[dict]):
    """safe_request side effect: both parent-detect probes miss (page parent),
    then POST /pages succeeds and its JSON body is captured."""

    async def _side_effect(client, method, url, provider, action, **kwargs):
        if method == "get":
            return None, "not found"
        captured_bodies.append(kwargs.get("json") or {})
        return (
            httpx.Response(
                200, json={"id": "new-page", "url": "https://notion.so/new-page"}
            ),
            None,
        )

    return _side_effect


@pytest.mark.asyncio
async def test_create_notion_page_bottom_omits_position():
    executor = _build_executor()
    bodies: list[dict] = []
    with patch(
        "app.integrations.providers.notion._common.safe_request", new_callable=AsyncMock
    ) as mock_request:
        mock_request.side_effect = _page_parent_side_effect(bodies)
        result = await executor.execute_tool(
            ToolCall(
                id="c1",
                name="create_notion_page",
                arguments={
                    "parent_id": "parent-1",
                    "title": "Report",
                    "position": "bottom",
                },
            )
        )
    assert not result.is_error, result.content
    assert "Page created" in result.content
    assert len(bodies) == 1
    assert "position" not in bodies[0], json.dumps(bodies[0])


@pytest.mark.asyncio
async def test_create_notion_page_top_maps_to_page_start():
    executor = _build_executor()
    bodies: list[dict] = []
    with patch(
        "app.integrations.providers.notion._common.safe_request", new_callable=AsyncMock
    ) as mock_request:
        mock_request.side_effect = _page_parent_side_effect(bodies)
        result = await executor.execute_tool(
            ToolCall(
                id="c2",
                name="create_notion_page",
                arguments={
                    "parent_id": "parent-1",
                    "title": "Report",
                    "position": "top",
                },
            )
        )
    assert not result.is_error, result.content
    assert bodies[0]["position"] == {"type": "page_start"}


@pytest.mark.asyncio
async def test_create_notion_page_block_uuid_becomes_after_block():
    executor = _build_executor()
    bodies: list[dict] = []
    with patch(
        "app.integrations.providers.notion._common.safe_request", new_callable=AsyncMock
    ) as mock_request:
        mock_request.side_effect = _page_parent_side_effect(bodies)
        result = await executor.execute_tool(
            ToolCall(
                id="c3",
                name="create_notion_page",
                arguments={
                    "parent_id": "parent-1",
                    "title": "Report",
                    "position": _BLOCK_UUID,
                },
            )
        )
    assert not result.is_error, result.content
    assert bodies[0]["position"] == {
        "type": "after_block",
        "after_block": {"id": _BLOCK_UUID},
    }


@pytest.mark.asyncio
async def test_create_notion_page_invalid_position_fails_fast_without_api_calls():
    executor = _build_executor()
    with patch(
        "app.integrations.providers.notion._common.safe_request", new_callable=AsyncMock
    ) as mock_request:
        result = await executor.execute_tool(
            ToolCall(
                id="c4",
                name="create_notion_page",
                arguments={
                    "parent_id": "parent-1",
                    "title": "Report",
                    "position": "after the last block",
                },
            )
        )
    # Fail-fast: no parent-detect round-trips are burned on a bad position.
    mock_request.assert_not_called()
    assert result.content.startswith("Error:")
    assert "position" in result.content


# ── move_notion_page wiring ──────────────────────────────────────────────

_PARENT_UUID = "660e8400-e29b-41d4-a716-446655440111"


def _move_side_effect(captured_bodies: list[dict]):
    """safe_request side effect: both parent probes 404 (page parent),
    then POST .../move succeeds."""

    async def _side_effect(client, method, url, provider, action, **kwargs):
        if method == "get":
            return httpx.Response(404, json={"code": "object_not_found"}), "not found"
        captured_bodies.append(kwargs.get("json") or {})
        return (
            httpx.Response(
                200, json={"id": "page-1", "url": "https://notion.so/page-1"}
            ),
            None,
        )

    return _side_effect


@pytest.mark.asyncio
async def test_move_notion_page_bottom_omits_position():
    executor = _build_executor()
    bodies: list[dict] = []
    with patch(
        "app.integrations.providers.notion._common.safe_request", new_callable=AsyncMock
    ) as mock_request:
        mock_request.side_effect = _move_side_effect(bodies)
        result = await executor.execute_tool(
            ToolCall(
                id="m1",
                name="move_notion_page",
                arguments={
                    "page_id": "page-1",
                    "new_parent_id": _PARENT_UUID,
                    "position": "bottom",
                },
            )
        )
    assert not result.is_error, result.content
    assert len(bodies) == 1
    assert "position" not in bodies[0], json.dumps(bodies[0])


@pytest.mark.asyncio
async def test_move_notion_page_invalid_position_fails_fast():
    executor = _build_executor()
    with patch(
        "app.integrations.providers.notion._common.safe_request", new_callable=AsyncMock
    ) as mock_request:
        result = await executor.execute_tool(
            ToolCall(
                id="m2",
                name="move_notion_page",
                arguments={
                    "page_id": "page-1",
                    "new_parent_id": _PARENT_UUID,
                    "position": "under the summary heading",
                },
            )
        )
    mock_request.assert_not_called()
    assert result.content.startswith("Error:")
    assert "position" in result.content
