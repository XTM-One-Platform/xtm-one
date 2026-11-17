"""Pre-flight type guard for ``update_notion_block``.

Notion does not support changing a block's TYPE. When the agent sent a
payload with fields for a different type than the live block (e.g.
``heading_3`` fields on a ``paragraph`` block), the API rejected it with a
raw HTTP 400 that the agent blindly retried. The tool now fetches the block
first and refuses the type change up front with actionable guidance, while
leaving same-type edits (and non-type updates like ``archived``) untouched.
"""

import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.integrations.providers.notion import build_tools
from app.services.integration_tools import _SafeIntegrationExecutor
from app.tools.base import ToolCall


def _block(block_type: str) -> httpx.Response:
    return httpx.Response(200, json={"id": "blk-1", "type": block_type})


def _ok() -> httpx.Response:
    return httpx.Response(200, json={})


async def _call_update(executor, block_json: str):
    return await executor.execute_tool(
        ToolCall(
            id="c1",
            name="update_notion_block",
            arguments={"block_id": "blk-1", "block_json": block_json},
        )
    )


@pytest.mark.asyncio
async def test_update_block_type_mismatch_refused_without_patch():
    """heading_3 fields on a paragraph block → actionable error, and the
    PATCH is never sent (only the pre-flight GET runs)."""
    executor = _SafeIntegrationExecutor()
    build_tools("dummy-token", executor)

    with patch(
        "app.integrations.providers.notion._common.safe_request", new_callable=AsyncMock
    ) as mock_request:
        mock_request.return_value = (_block("paragraph"), None)
        result = await _call_update(
            executor,
            '{"heading_3": {"rich_text": [{"text": {"content": "Title"}}]}}',
        )

    assert "cannot change the type" in result.content
    assert "`paragraph`" in result.content
    assert "heading_3" in result.content
    # Actionable: names the exact tools to convert a block.
    assert "delete_notion_block" in result.content
    assert "append_notion_blocks_json" in result.content
    # Only the pre-flight GET ran — no PATCH was attempted.
    assert mock_request.call_count == 1
    assert mock_request.call_args_list[0].args[1] == "get"


@pytest.mark.asyncio
async def test_update_block_mixed_type_keys_refused_without_patch():
    """A payload that mixes the matching type key with another block type
    (e.g. ``paragraph`` + ``heading_3``) must still be refused — the matching
    ``paragraph`` key must not let the mismatched ``heading_3`` fields slip
    through to a PATCH that Notion rejects with a block-type-mismatch 400.
    Only the pre-flight GET runs."""
    executor = _SafeIntegrationExecutor()
    build_tools("dummy-token", executor)

    with patch(
        "app.integrations.providers.notion._common.safe_request", new_callable=AsyncMock
    ) as mock_request:
        mock_request.return_value = (_block("paragraph"), None)
        result = await _call_update(
            executor,
            '{"paragraph": {"rich_text": [{"text": {"content": "Body"}}]}, '
            '"heading_3": {"rich_text": [{"text": {"content": "Title"}}]}}',
        )

    assert "cannot change the type" in result.content
    assert "`paragraph`" in result.content
    # The error names the mismatched type, not the (matching) paragraph key.
    assert "heading_3" in result.content
    # No PATCH was attempted — only the pre-flight GET ran.
    assert mock_request.call_count == 1
    assert mock_request.call_args_list[0].args[1] == "get"


@pytest.mark.asyncio
async def test_update_block_matching_type_proceeds_to_patch():
    """paragraph fields on a paragraph block → pre-flight passes, PATCH runs."""
    executor = _SafeIntegrationExecutor()
    build_tools("dummy-token", executor)

    with patch(
        "app.integrations.providers.notion._common.safe_request", new_callable=AsyncMock
    ) as mock_request:
        mock_request.side_effect = [(_block("paragraph"), None), (_ok(), None)]
        result = await _call_update(
            executor,
            '{"paragraph": {"rich_text": [{"text": {"content": "Edited"}}]}}',
        )

    assert not result.is_error
    assert "updated" in result.content
    assert mock_request.call_count == 2
    assert mock_request.call_args_list[0].args[1] == "get"
    assert mock_request.call_args_list[1].args[1] == "patch"


@pytest.mark.asyncio
async def test_update_block_without_type_key_skips_preflight():
    """A non-type update (e.g. {"archived": true}) must NOT incur a pre-flight
    GET — it patches directly (zero extra latency)."""
    executor = _SafeIntegrationExecutor()
    build_tools("dummy-token", executor)

    with patch(
        "app.integrations.providers.notion._common.safe_request", new_callable=AsyncMock
    ) as mock_request:
        mock_request.return_value = (_ok(), None)
        result = await _call_update(executor, '{"archived": true}')

    assert not result.is_error
    assert "updated" in result.content
    assert mock_request.call_count == 1
    assert mock_request.call_args_list[0].args[1] == "patch"


@pytest.mark.asyncio
async def test_update_block_preflight_fetch_failure_falls_through_to_patch():
    """If the pre-flight GET itself fails, fall through to the PATCH so a
    transient read error never blocks a legitimate update (the API stays the
    source of truth)."""
    executor = _SafeIntegrationExecutor()
    build_tools("dummy-token", executor)

    with patch(
        "app.integrations.providers.notion._common.safe_request", new_callable=AsyncMock
    ) as mock_request:
        mock_request.side_effect = [
            (None, "Notion returned HTTP 503 while trying to retrieve block."),
            (_ok(), None),
        ]
        result = await _call_update(
            executor,
            '{"heading_3": {"rich_text": [{"text": {"content": "Title"}}]}}',
        )

    assert not result.is_error
    assert "updated" in result.content
    assert mock_request.call_count == 2
    assert mock_request.call_args_list[0].args[1] == "get"
    assert mock_request.call_args_list[1].args[1] == "patch"


@pytest.mark.asyncio
async def test_update_block_sanitizes_rich_text_in_update_payload():
    """The update-payload shape ({"paragraph": {...}}) carries no top-level
    ``type`` key, so the rich-text hardening must still resolve the body via
    the block-type key. Misplaced annotations are moved to a sibling of
    ``text`` and over-long content is truncated to 2000 chars BEFORE the PATCH
    is sent."""
    executor = _SafeIntegrationExecutor()
    build_tools("dummy-token", executor)

    long_content = "x" * 2500
    block_json = (
        '{"paragraph": {"rich_text": [{"text": {"content": "'
        + long_content
        + '", "annotations": {"bold": true}}}]}}'
    )

    with patch(
        "app.integrations.providers.notion._common.safe_request", new_callable=AsyncMock
    ) as mock_request:
        mock_request.side_effect = [(_block("paragraph"), None), (_ok(), None)]
        result = await _call_update(executor, block_json)

    assert not result.is_error
    # The PATCH body is the (in-place sanitized) payload.
    patch_call = mock_request.call_args_list[1]
    assert patch_call.args[1] == "patch"
    sent_item = patch_call.kwargs["json"]["paragraph"]["rich_text"][0]
    # Annotations moved out of ``text`` to a sibling of it.
    assert "annotations" not in sent_item["text"]
    assert sent_item["annotations"] == {"bold": True}
    # Content truncated to the 2000-char Notion limit (1999 + ellipsis).
    assert len(sent_item["text"]["content"]) == 2000
    assert sent_item["text"]["content"].endswith("…")


@pytest.mark.asyncio
async def test_update_block_multiple_mismatched_types_message():
    """When several mismatched type keys are present, the error must describe
    them as "fields for" the listed types (not "a `t1, t2` block", which names
    no real block) and keep the conversion guidance generic."""
    executor = _SafeIntegrationExecutor()
    build_tools("dummy-token", executor)

    with patch(
        "app.integrations.providers.notion._common.safe_request", new_callable=AsyncMock
    ) as mock_request:
        mock_request.return_value = (_block("paragraph"), None)
        result = await _call_update(
            executor,
            '{"heading_2": {"rich_text": [{"text": {"content": "A"}}]}, '
            '"heading_3": {"rich_text": [{"text": {"content": "B"}}]}}',
        )

    assert "cannot change the type" in result.content
    assert "`paragraph`" in result.content
    # Lists the mismatched types as "fields for ...", not a fake block name.
    assert "fields for `heading_2`, `heading_3`" in result.content
    assert "a `heading_2, heading_3` block" not in result.content
    # Generic conversion guidance (no single fabricated target type).
    assert "a new block of the desired type" in result.content
    # No PATCH was attempted — only the pre-flight GET ran.
    assert mock_request.call_count == 1
    assert mock_request.call_args_list[0].args[1] == "get"


# ---------------------------------------------------------------------------
# table_row.cells normalisation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_table_row_with_bare_string_cells_normalised():
    """update_notion_block must coerce bare string cells into rich_text object
    arrays before PATCHing.  Notion rejects {"cells": [["Alice"]]} (where
    "Alice" is a string) with HTTP 400; the fix must auto-wrap to
    [[{"type": "text", "text": {"content": "Alice"}}]]."""
    executor = _SafeIntegrationExecutor()
    build_tools("dummy-token", executor)

    with patch(
        "app.integrations.providers.notion._common.safe_request", new_callable=AsyncMock
    ) as mock_request:
        # Pre-flight GET returns the existing block type; PATCH succeeds.
        mock_request.side_effect = [(_block("table_row"), None), (_ok(), None)]
        result = await _call_update(
            executor,
            '{"table_row": {"cells": [["Alice"], ["Manager"], ["Berlin"]]}}',
        )

    assert not result.is_error
    assert "updated" in result.content

    # The PATCH body must carry rich_text objects, not bare strings.
    patch_call = mock_request.call_args_list[1]
    assert patch_call.args[1] == "patch"
    cells = patch_call.kwargs["json"]["table_row"]["cells"]
    assert cells == [
        [{"type": "text", "text": {"content": "Alice"}}],
        [{"type": "text", "text": {"content": "Manager"}}],
        [{"type": "text", "text": {"content": "Berlin"}}],
    ]


@pytest.mark.asyncio
async def test_update_table_row_with_bare_top_level_string_cells_normalised():
    """Cells sent as bare strings at the top level (cells[0] = "Alice" instead
    of cells[0] = ["Alice"]) are the exact pattern observed in production
    (body.table_row.cells[0][0] should be an object, instead was "Alice")."""
    executor = _SafeIntegrationExecutor()
    build_tools("dummy-token", executor)

    with patch(
        "app.integrations.providers.notion._common.safe_request", new_callable=AsyncMock
    ) as mock_request:
        mock_request.side_effect = [(_block("table_row"), None), (_ok(), None)]
        result = await _call_update(
            executor,
            '{"table_row": {"cells": ["Alice", "Manager"]}}',
        )

    assert not result.is_error
    assert "updated" in result.content

    patch_call = mock_request.call_args_list[1]
    cells = patch_call.kwargs["json"]["table_row"]["cells"]
    assert cells == [
        [{"type": "text", "text": {"content": "Alice"}}],
        [{"type": "text", "text": {"content": "Manager"}}],
    ]


@pytest.mark.asyncio
async def test_update_table_row_already_correct_cells_pass_through():
    """Cells already in the correct [[{rich_text_obj}]] shape must pass through
    unchanged (no double-wrapping or data loss)."""
    executor = _SafeIntegrationExecutor()
    build_tools("dummy-token", executor)

    expected_cells = [
        [{"type": "text", "text": {"content": "Alice"}}],
        [{"type": "text", "text": {"content": "Manager"}}],
    ]

    with patch(
        "app.integrations.providers.notion._common.safe_request", new_callable=AsyncMock
    ) as mock_request:
        mock_request.side_effect = [(_block("table_row"), None), (_ok(), None)]
        result = await _call_update(
            executor,
            json.dumps({"table_row": {"cells": expected_cells}}),
        )

    assert not result.is_error
    patch_call = mock_request.call_args_list[1]
    cells = patch_call.kwargs["json"]["table_row"]["cells"]
    assert cells == expected_cells
