"""Regression tests for Notion rich_text 2000-character limit enforcement.

Covers:
- _sanitize_rich_text: content truncation to 2000 chars with ellipsis marker
- _truncate_properties_rich_text: truncation applied to properties dicts
- update_notion_page: full integration — oversized rich_text is truncated before the API call
- add_notion_comment: comment text truncated before the API call
"""

import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.integrations.providers.notion import (
    _NOTION_RICH_TEXT_LIMIT,
    _sanitize_rich_text,
    _truncate_properties_rich_text,
    build_tools,
)
from app.integrations.providers.notion.formatting import _sanitize_blocks
from app.services.integration_tools import _SafeIntegrationExecutor
from app.tools.base import ToolCall

# ---------------------------------------------------------------------------
# _sanitize_rich_text unit tests
# ---------------------------------------------------------------------------


def test_sanitize_rich_text_truncates_overlong_content(caplog):
    """text.content exceeding the limit must be truncated to exactly 2000 chars,
    with a trailing ellipsis to signal truncation, AND a single WARNING
    must be emitted so operators see truncations in logs.

    The warning is part of the behavioural contract (PR description
    promises log emission on truncation) — a regression that silently
    drops the log would otherwise pass undetected.
    """
    import logging

    long_text = "x" * (_NOTION_RICH_TEXT_LIMIT + 709)  # reproduces the 2709-char case
    rt = [{"type": "text", "text": {"content": long_text}}]
    with caplog.at_level(
        logging.WARNING, logger="app.integrations.providers.notion"
    ):
        result = _sanitize_rich_text(rt)
    result_content = result[0]["text"]["content"]
    assert len(result_content) == _NOTION_RICH_TEXT_LIMIT
    assert result_content.endswith("…")
    truncation_warnings = [
        r
        for r in caplog.records
        if r.levelname == "WARNING" and "truncated" in r.getMessage()
    ]
    assert truncation_warnings, (
        "_sanitize_rich_text must emit a WARNING when it truncates "
        "content so operators see the loss-of-information event "
        "in logs.  Observed records: "
        f"{[r.getMessage() for r in caplog.records]}"
    )
    assert any(
        str(_NOTION_RICH_TEXT_LIMIT) in r.getMessage() for r in truncation_warnings
    ), (
        "the truncation WARNING should include the limit so operators "
        "can correlate it with the Notion 2 000-char constraint"
    )


def test_sanitize_rich_text_exact_limit_unchanged():
    """Content that is exactly 2000 chars should not be modified."""
    exact_text = "a" * _NOTION_RICH_TEXT_LIMIT
    rt = [{"type": "text", "text": {"content": exact_text}}]
    result = _sanitize_rich_text(rt)
    assert result[0]["text"]["content"] == exact_text


def test_sanitize_rich_text_short_content_unchanged():
    """Short content must not be altered."""
    rt = [{"type": "text", "text": {"content": "hello"}}]
    result = _sanitize_rich_text(rt)
    assert result[0]["text"]["content"] == "hello"


def test_sanitize_rich_text_still_fixes_misplaced_annotations():
    """Annotation-fixup behaviour must still work after adding content truncation."""
    rt = [
        {
            "type": "text",
            "text": {"content": "bold text", "annotations": {"bold": True}},
        }
    ]
    result = _sanitize_rich_text(rt)
    assert "annotations" not in result[0]["text"]
    assert result[0]["annotations"] == {"bold": True}


def test_sanitize_rich_text_truncation_and_annotation_fix_combined():
    """Both truncation and annotation-fixup should apply to the same item."""
    long_text = "z" * (_NOTION_RICH_TEXT_LIMIT + 100)
    rt = [
        {
            "type": "text",
            "text": {"content": long_text, "annotations": {"bold": True}},
        }
    ]
    result = _sanitize_rich_text(rt)
    result_content = result[0]["text"]["content"]
    assert len(result_content) == _NOTION_RICH_TEXT_LIMIT
    assert result_content.endswith("…")
    assert result[0]["annotations"] == {"bold": True}


def test_sanitize_rich_text_multiple_items_each_truncated():
    """Each item in the list must be independently truncated with a trailing ellipsis."""
    long_text = "y" * (_NOTION_RICH_TEXT_LIMIT + 1)
    rt = [
        {"type": "text", "text": {"content": long_text}},
        {"type": "text", "text": {"content": long_text}},
    ]
    result = _sanitize_rich_text(rt)
    for item in result:
        result_content = item["text"]["content"]
        assert len(result_content) == _NOTION_RICH_TEXT_LIMIT
        assert result_content.endswith("…")


def test_sanitize_rich_text_non_string_content_ignored():
    """Non-string content values (malformed items) should not raise."""
    rt = [{"type": "text", "text": {"content": 12345}}]
    result = _sanitize_rich_text(rt)
    assert result[0]["text"]["content"] == 12345  # unchanged, no crash


def test_sanitize_rich_text_non_dict_items_skipped():
    """A rich_text array is LLM-generated, so an item may be a bare string /
    null / scalar instead of a rich_text object. The sanitizer must be
    best-effort: skip the non-dict entries (leave them untouched for the
    Notion API to reject with its own actionable error) and still process
    the valid dict items, rather than crash the whole tool call on
    ``item.get(...)`` with an AttributeError.
    """
    long_text = "q" * (_NOTION_RICH_TEXT_LIMIT + 50)
    rt = [
        "plain string",  # malformed
        None,  # malformed
        12345,  # malformed
        ["nested", "list"],  # malformed
        {"type": "text", "text": {"content": long_text}},  # valid -> truncated
    ]
    result = _sanitize_rich_text(rt)
    # Non-dict entries pass through unchanged, in order, no crash.
    assert result[0] == "plain string"
    assert result[1] is None
    assert result[2] == 12345
    assert result[3] == ["nested", "list"]
    # The one valid dict item is still sanitized (truncated with ellipsis).
    assert len(result[4]["text"]["content"]) == _NOTION_RICH_TEXT_LIMIT
    assert result[4]["text"]["content"].endswith("…")


# ---------------------------------------------------------------------------
# _sanitize_blocks unit tests
# ---------------------------------------------------------------------------


def test_sanitize_blocks_non_dict_entries_skipped():
    """``append_notion_blocks_json`` only guarantees the parsed payload is a
    non-empty list - the individual entries are LLM-generated and may be
    non-dicts. ``_sanitize_blocks`` must skip them defensively instead of
    crashing on ``block.get(...)`` / ``for k in block``, while still
    sanitizing the valid dict blocks.
    """
    long_text = "b" * (_NOTION_RICH_TEXT_LIMIT + 25)
    blocks = [
        "not a block",  # malformed
        None,  # malformed
        42,  # malformed
        {
            "type": "paragraph",
            "paragraph": {
                "rich_text": [{"type": "text", "text": {"content": long_text}}]
            },
        },  # valid -> rich_text truncated
    ]
    result = _sanitize_blocks(blocks)
    # Non-dict entries pass through untouched (API rejects malformed blocks).
    assert result[0] == "not a block"
    assert result[1] is None
    assert result[2] == 42
    # The valid block's rich_text is still sanitized.
    sanitized = result[3]["paragraph"]["rich_text"][0]["text"]["content"]
    assert len(sanitized) == _NOTION_RICH_TEXT_LIMIT
    assert sanitized.endswith("…")


def test_sanitize_blocks_non_dict_rich_text_items_skipped():
    """A valid block may itself carry a malformed rich_text array (non-dict
    items). The nested sanitization must not crash and must still truncate
    the valid rich_text item.
    """
    long_text = "c" * (_NOTION_RICH_TEXT_LIMIT + 25)
    blocks = [
        {
            "paragraph": {  # update shape (no top-level "type")
                "rich_text": [
                    "stray string",
                    None,
                    {"type": "text", "text": {"content": long_text}},
                ]
            }
        }
    ]
    result = _sanitize_blocks(blocks)
    rich = result[0]["paragraph"]["rich_text"]
    assert rich[0] == "stray string"
    assert rich[1] is None
    assert len(rich[2]["text"]["content"]) == _NOTION_RICH_TEXT_LIMIT
    assert rich[2]["text"]["content"].endswith("…")


# ---------------------------------------------------------------------------
# _truncate_properties_rich_text unit tests
# ---------------------------------------------------------------------------


def test_truncate_properties_rich_text_truncates_overlong(caplog):
    """rich_text property values exceeding the limit must be truncated
    with ellipsis AND must emit a single WARNING per truncated item so
    operators see the truncation in logs.
    """
    import logging

    long_text = "a" * 2709
    props = {
        "Handoff Summary": {
            "rich_text": [{"type": "text", "text": {"content": long_text}}]
        }
    }
    with caplog.at_level(
        logging.WARNING, logger="app.integrations.providers.notion"
    ):
        _truncate_properties_rich_text(props)
    result_content = props["Handoff Summary"]["rich_text"][0]["text"]["content"]
    assert len(result_content) == _NOTION_RICH_TEXT_LIMIT
    assert result_content.endswith("…")
    truncation_warnings = [
        r
        for r in caplog.records
        if r.levelname == "WARNING" and "truncated" in r.getMessage()
    ]
    assert len(truncation_warnings) == 1, (
        "Exactly one WARNING is expected for a single oversized "
        "rich_text item; observed: "
        f"{[r.getMessage() for r in caplog.records]}"
    )


def test_truncate_properties_rich_text_short_content_unchanged():
    """Short property values must not be altered."""
    props = {
        "Status Note": {
            "rich_text": [{"type": "text", "text": {"content": "short text"}}]
        }
    }
    _truncate_properties_rich_text(props)
    assert props["Status Note"]["rich_text"][0]["text"]["content"] == "short text"


def test_truncate_properties_rich_text_non_rich_text_properties_ignored():
    """Non-rich_text properties (select, status, number) must not be touched."""
    props = {
        "Priority": {"select": {"name": "High"}},
        "Status": {"status": {"name": "Done"}},
    }
    original = json.dumps(props)
    _truncate_properties_rich_text(props)
    assert json.dumps(props) == original


def test_truncate_properties_rich_text_multiple_properties():
    """All rich_text properties in the dict should be truncated independently."""
    long_text = "m" * (_NOTION_RICH_TEXT_LIMIT + 500)
    props = {
        "Summary": {
            "rich_text": [{"type": "text", "text": {"content": long_text}}]
        },
        "Notes": {
            "rich_text": [{"type": "text", "text": {"content": long_text}}]
        },
    }
    _truncate_properties_rich_text(props)
    for key in ("Summary", "Notes"):
        result_content = props[key]["rich_text"][0]["text"]["content"]
        assert len(result_content) == _NOTION_RICH_TEXT_LIMIT
        assert result_content.endswith("…")


def test_truncate_properties_rich_text_truncates_title_property():
    """``title`` properties (used by create_notion_page and database
    primary-key columns) are subject to the same 2 000-char per-item limit
    as ``rich_text``.  Without this branch a >2 000-char title would
    silently produce an HTTP 400 from Notion.
    """
    long_title = "t" * (_NOTION_RICH_TEXT_LIMIT + 100)
    props = {
        "Name": {
            "title": [{"type": "text", "text": {"content": long_title}}]
        }
    }
    _truncate_properties_rich_text(props)
    result_content = props["Name"]["title"][0]["text"]["content"]
    assert len(result_content) == _NOTION_RICH_TEXT_LIMIT
    assert result_content.endswith("…")


def test_truncate_properties_rich_text_handles_title_and_rich_text_together():
    """A realistic create_notion_page-style mix of a title column plus a
    rich_text column must have BOTH truncated in a single pass.
    """
    long_text = "x" * (_NOTION_RICH_TEXT_LIMIT + 300)
    props = {
        "Name": {"title": [{"type": "text", "text": {"content": long_text}}]},
        "Handoff Summary": {
            "rich_text": [{"type": "text", "text": {"content": long_text}}]
        },
    }
    _truncate_properties_rich_text(props)
    title_content = props["Name"]["title"][0]["text"]["content"]
    rt_content = props["Handoff Summary"]["rich_text"][0]["text"]["content"]
    assert len(title_content) == _NOTION_RICH_TEXT_LIMIT
    assert title_content.endswith("…")
    assert len(rt_content) == _NOTION_RICH_TEXT_LIMIT
    assert rt_content.endswith("…")


def test_truncate_properties_rich_text_short_title_unchanged():
    """A short title must round-trip unchanged through the truncator."""
    props = {
        "Name": {"title": [{"type": "text", "text": {"content": "Sprint Review"}}]}
    }
    _truncate_properties_rich_text(props)
    assert props["Name"]["title"][0]["text"]["content"] == "Sprint Review"


# ---------------------------------------------------------------------------
# update_notion_page integration test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_notion_page_truncates_rich_text_before_api_call(caplog):
    """update_notion_page must truncate rich_text content to 2000 chars before
    calling the Notion API, preventing HTTP 400 errors.  Truncation must
    also emit a WARNING (operator-visible log) so silent data loss is
    impossible.
    """
    import logging

    executor = _SafeIntegrationExecutor()
    build_tools("fake-token", executor)

    long_text = "x" * 2709  # value from the real-world error report
    properties_json = json.dumps(
        {
            "Handoff Summary": {
                "rich_text": [{"type": "text", "text": {"content": long_text}}]
            }
        }
    )

    with patch(
        "app.integrations.providers.notion._common.safe_request", new_callable=AsyncMock
    ) as mock_req:
        mock_req.return_value = (httpx.Response(200, json={"id": "page-123"}), None)

        with caplog.at_level(
            logging.WARNING, logger="app.integrations.providers.notion"
        ):
            result = await executor.execute_tool(
                ToolCall(
                    id="call-1",
                    name="update_notion_page",
                    arguments={"page_id": "page-123", "properties_json": properties_json},
                )
            )

    assert not result.is_error
    # Verify the payload sent to the API had truncated content with ellipsis
    call_kwargs = mock_req.call_args
    sent_body = call_kwargs.kwargs["json"]
    sent_content = sent_body["properties"]["Handoff Summary"]["rich_text"][0]["text"][
        "content"
    ]
    assert len(sent_content) == _NOTION_RICH_TEXT_LIMIT
    assert sent_content.endswith("…")
    # The WARNING is part of the behavioural contract: regressions
    # that silently drop the log would otherwise pass undetected.
    assert any(
        r.levelname == "WARNING" and "truncated" in r.getMessage()
        for r in caplog.records
    ), (
        "update_notion_page must surface a WARNING when truncating "
        "oversized rich_text content; observed records: "
        f"{[r.getMessage() for r in caplog.records]}"
    )


@pytest.mark.asyncio
async def test_update_notion_page_rejects_non_dict_properties_json():
    """Regression: ``update_notion_page`` must reject any
    ``properties_json`` value that parses to a non-dict (list, scalar,
    null) with a clear error string — NOT crash inside
    ``_truncate_properties_rich_text`` on ``.values()``.

    Bot review pointed out that ``json.loads('[]')`` returns a list
    and the downstream ``_truncate_properties_rich_text`` would
    ``AttributeError`` on ``.values()``, turning a user input error
    into an opaque tool crash.  Validate the parsed type FIRST and
    return a structured error message that lets the LLM correct
    its payload on the next turn.
    """
    executor = _SafeIntegrationExecutor()
    build_tools("fake-token", executor)

    invalid_payloads = ["[]", '"x"', "123", "true", "null"]

    for raw in invalid_payloads:
        with patch(
            "app.integrations.providers.notion._common.safe_request", new_callable=AsyncMock
        ) as mock_req:
            result = await executor.execute_tool(
                ToolCall(
                    id=f"call-non-dict-{raw}",
                    name="update_notion_page",
                    arguments={"page_id": "page-x", "properties_json": raw},
                )
            )
            # Returned (not raised) as an error string - and flagged as the
            # failure it is.
            assert result.is_error, (
                "the returned error string must be recorded as a failure; "
                f"raw payload: {raw!r}"
            )
            body = result.content if isinstance(result.content, str) else str(result.content)
            assert "properties_json must be a JSON object" in body, (
                f"expected the structured error string for non-dict "
                f"payload {raw!r}; got {body!r}"
            )
            # CRITICAL: the API must NOT have been called when validation rejects.
            mock_req.assert_not_called()


@pytest.mark.asyncio
async def test_update_notion_page_short_rich_text_passes_through():
    """Short rich_text content must reach the API unchanged."""
    executor = _SafeIntegrationExecutor()
    build_tools("fake-token", executor)

    short_text = "This is a concise summary."
    properties_json = json.dumps(
        {
            "Summary": {
                "rich_text": [{"type": "text", "text": {"content": short_text}}]
            }
        }
    )

    with patch(
        "app.integrations.providers.notion._common.safe_request", new_callable=AsyncMock
    ) as mock_req:
        mock_req.return_value = (httpx.Response(200, json={"id": "page-456"}), None)

        result = await executor.execute_tool(
            ToolCall(
                id="call-2",
                name="update_notion_page",
                arguments={"page_id": "page-456", "properties_json": properties_json},
            )
        )

    assert not result.is_error
    call_kwargs = mock_req.call_args
    sent_body = call_kwargs.kwargs["json"]
    sent_content = sent_body["properties"]["Summary"]["rich_text"][0]["text"]["content"]
    assert sent_content == short_text


# ---------------------------------------------------------------------------
# add_notion_comment integration tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_notion_comment_truncates_overlong_text(caplog):
    """add_notion_comment must truncate text exceeding 2000 chars before
    the API call AND must emit a WARNING — the log is operator-visible
    signal that data was lost on the way to Notion.
    """
    import logging

    executor = _SafeIntegrationExecutor()
    build_tools("fake-token", executor)

    long_comment = "c" * 2500

    with patch(
        "app.integrations.providers.notion._common.AutoRefreshClient.post",
        new_callable=AsyncMock,
    ) as mock_req:
        mock_req.return_value = httpx.Response(200, json={})

        with caplog.at_level(
            logging.WARNING, logger="app.integrations.providers.notion"
        ):
            result = await executor.execute_tool(
                ToolCall(
                    id="call-3",
                    name="add_notion_comment",
                    arguments={"page_id": "page-789", "text": long_comment},
                )
            )

    assert not result.is_error
    call_kwargs = mock_req.call_args
    sent_body = call_kwargs.kwargs["json"]
    sent_content = sent_body["rich_text"][0]["text"]["content"]
    assert len(sent_content) == _NOTION_RICH_TEXT_LIMIT
    assert sent_content.endswith("…")
    assert any(
        r.levelname == "WARNING" and "truncated" in r.getMessage()
        for r in caplog.records
    ), (
        "add_notion_comment must surface a WARNING when truncating "
        f"oversized comment text; observed records: "
        f"{[r.getMessage() for r in caplog.records]}"
    )


@pytest.mark.asyncio
async def test_create_notion_page_truncates_overlong_title_when_no_properties_json(caplog):
    """A long ``title`` argument with empty ``properties_json`` must still be
    truncated before reaching the API — this was the gap surfaced as
    suggestion #2 in the original review and is now closed by calling
    ``_truncate_properties_rich_text`` unconditionally inside
    ``create_notion_page``.

    Truncation must also emit a WARNING so operators see the
    loss-of-information event in logs.
    """
    import logging

    executor = _SafeIntegrationExecutor()
    build_tools("fake-token", executor)

    long_title = "t" * (_NOTION_RICH_TEXT_LIMIT + 250)

    with patch(
        "app.integrations.providers.notion._common.safe_request", new_callable=AsyncMock
    ) as mock_req:
        mock_req.side_effect = [
            (httpx.Response(404), None),  # parent is not a database
            (httpx.Response(404), None),  # parent is not a data_source either → treat as page
            (httpx.Response(200, json={"id": "page-new"}), None),  # POST /pages
        ]

        with caplog.at_level(
            logging.WARNING, logger="app.integrations.providers.notion"
        ):
            result = await executor.execute_tool(
                ToolCall(
                    id="call-title",
                    name="create_notion_page",
                    arguments={"parent_id": "parent-abc", "title": long_title},
                )
            )

    assert not result.is_error
    final_call = mock_req.call_args_list[-1]
    sent_body = final_call.kwargs["json"]
    sent_title = sent_body["properties"]["title"]["title"][0]["text"]["content"]
    assert len(sent_title) == _NOTION_RICH_TEXT_LIMIT
    assert sent_title.endswith("…")
    assert any(
        r.levelname == "WARNING" and "truncated" in r.getMessage()
        for r in caplog.records
    ), (
        "create_notion_page must surface a WARNING when truncating "
        f"an oversized title; observed records: "
        f"{[r.getMessage() for r in caplog.records]}"
    )


@pytest.mark.asyncio
async def test_add_notion_comment_short_text_passes_through():
    """Short comment text must reach the API unchanged."""
    executor = _SafeIntegrationExecutor()
    build_tools("fake-token", executor)

    short_comment = "This is a short comment."

    with patch(
        "app.integrations.providers.notion._common.AutoRefreshClient.post",
        new_callable=AsyncMock,
    ) as mock_req:
        mock_req.return_value = httpx.Response(200, json={})

        result = await executor.execute_tool(
            ToolCall(
                id="call-4",
                name="add_notion_comment",
                arguments={"page_id": "page-abc", "text": short_comment},
            )
        )

    assert not result.is_error
    call_kwargs = mock_req.call_args
    sent_body = call_kwargs.kwargs["json"]
    sent_content = sent_body["rich_text"][0]["text"]["content"]
    assert sent_content == short_comment
