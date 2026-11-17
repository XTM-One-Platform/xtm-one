"""Regression tests for the Notion ``*_json`` argument coercion fix.

Production incident (May 13, 2026):
``create_notion_page`` raised
``TypeError: the JSON object must be str, bytes or bytearray, not dict``
roughly six times per agent loop iteration when the LLM passed
``properties_json`` as a parsed ``dict`` instead of a JSON-encoded string.
The retry storm exhausted the DB connection pool and triggered repeated
OOM kills (11 in 10 hours).

The fix introduces ``_parse_json_arg`` which accepts ``dict``/``list``,
JSON-encoded strings, and empty/None values; these tests lock that
contract for every Notion tool that previously called ``json.loads``
directly so the regression cannot return.
"""

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.integrations.providers.notion import _parse_json_arg, build_tools
from app.services.integration_tools import _SafeIntegrationExecutor
from app.tools.base import ToolCall

# ─── Unit tests for _parse_json_arg ──────────────────────────────────────────


def test_parse_json_arg_accepts_dict():
    value, err = _parse_json_arg({"Status": {"status": {"name": "Done"}}}, what="props")
    assert err is None
    assert value == {"Status": {"status": {"name": "Done"}}}


def test_parse_json_arg_accepts_list():
    value, err = _parse_json_arg([{"property": "Created"}], what="sorts")
    assert err is None
    assert value == [{"property": "Created"}]


def test_parse_json_arg_accepts_json_string():
    value, err = _parse_json_arg('{"Status": "Done"}', what="props")
    assert err is None
    assert value == {"Status": "Done"}


def test_parse_json_arg_treats_empty_string_as_absent():
    value, err = _parse_json_arg("", what="props")
    assert err is None
    assert value is None


def test_parse_json_arg_treats_none_as_absent():
    value, err = _parse_json_arg(None, what="props")
    assert err is None
    assert value is None


def test_parse_json_arg_rejects_invalid_string():
    value, err = _parse_json_arg("{not json}", what="props")
    assert value is None
    assert err is not None
    assert "Invalid props" in err


def test_parse_json_arg_rejects_unsupported_type_object_default():
    """The default ``expected="any"`` wording covers both object- and
    array-shaped tools so the LLM's retry guidance is correct for
    every caller, including ``children_json`` / ``sorts_json``."""
    value, err = _parse_json_arg(12345, what="props")
    assert value is None
    assert err is not None
    assert "must be a JSON object, JSON array, or JSON string" in err
    assert "int" in err


def test_parse_json_arg_rejects_unsupported_type_object_expected():
    """When the caller declares ``expected="object"`` the error message
    narrows to "JSON object or JSON string" so the LLM's retry payload
    matches the tool's real schema (and the existing
    ``_rejects_unsupported_properties_json_type`` regression test still
    matches the prefix)."""
    value, err = _parse_json_arg(12345, what="props", expected="object")
    assert value is None
    assert err is not None
    assert "must be a JSON object or JSON string" in err
    # The default-broader wording must NOT leak into the object-shaped
    # message — otherwise the LLM might send a JSON array for a tool
    # that only accepts an object.
    assert "JSON array" not in err
    assert "int" in err


def test_parse_json_arg_rejects_unsupported_type_array_expected():
    """Symmetric to the ``"object"`` case for array-shaped tools like
    ``children_json`` / ``sorts_json`` — the error must direct the LLM
    to a JSON array, not a JSON object."""
    value, err = _parse_json_arg(12345, what="sorts", expected="array")
    assert value is None
    assert err is not None
    assert "must be a JSON array or JSON string" in err
    assert "JSON object" not in err
    assert "int" in err


def test_parse_json_arg_rejects_bool():
    """``bool`` is a subclass of ``int``, so any future change that
    broadens the accepted-types tuple to include ``int`` would
    silently start accepting ``True`` / ``False`` as well.  Lock the
    rejection here so such a refactor is forced to reckon with it
    explicitly (typically by listing ``bool`` separately in
    ``isinstance`` or adding a ``not isinstance(value, bool)`` guard
    alongside the ``int`` accept).
    """
    value, err = _parse_json_arg(True, what="props", expected="object")
    assert value is None
    assert err is not None
    assert "must be a JSON object or JSON string" in err
    assert "bool" in err

    value, err = _parse_json_arg(False, what="props", expected="object")
    assert value is None
    assert err is not None
    assert "bool" in err


# ─── Integration tests: regression for the production crash ──────────────────
#
# Each test exercises one tool that previously called ``json.loads`` directly,
# passing a Python ``dict``/``list`` (the LLM-emitted shape that triggered the
# original ``TypeError``). The expectation is a successful tool call — not a
# hard error.


def _ok(json_body: dict) -> tuple[httpx.Response, None]:
    return httpx.Response(200, json=json_body), None


@pytest.mark.asyncio
async def test_create_notion_page_accepts_dict_properties_json():
    """Regression for the TypeError that crashed production.

    The production log at 2026-05-13T08:02:19Z was:
        File "/app/app/integrations/providers/notion.py", line 655
            extra_props = json.loads(properties_json)
        TypeError: the JSON object must be str, bytes or bytearray, not dict
    """
    executor = _SafeIntegrationExecutor()
    build_tools("test-token", executor)

    side_effect_responses: list[tuple[httpx.Response, None]] = [
        # 1st call: detect parent type (GET /databases/{parent_id})
        _ok({"data_sources": [{"id": "ds-1"}]}),
        # 2nd call: create page (POST /pages)
        _ok({"id": "page-1", "url": "https://notion.so/page-1"}),
    ]

    with patch(
        "app.integrations.providers.notion._common.safe_request",
        new_callable=AsyncMock,
    ) as mock_request:
        mock_request.side_effect = side_effect_responses

        result = await executor.execute_tool(
            ToolCall(
                id="call-1",
                name="create_notion_page",
                arguments={
                    "parent_id": "db-1",
                    "title": "Test page",
                    # The LLM-emitted shape that triggered the production crash:
                    # a parsed dict, NOT a JSON-encoded string.
                    "properties_json": {
                        "Status": {"status": {"name": "In Progress"}}
                    },
                },
            )
        )

    assert not result.is_error, result.content
    assert "Page created" in result.content

    # Verify the merged properties reached the API call body unchanged.
    create_call = mock_request.call_args_list[-1]
    body = create_call.kwargs["json"]
    assert body["properties"]["Status"] == {"status": {"name": "In Progress"}}
    # The auto-generated title property must still be present alongside.
    assert "title" in body["properties"]


@pytest.mark.asyncio
async def test_create_notion_page_still_accepts_json_string():
    """The fix must not regress the string code path."""
    executor = _SafeIntegrationExecutor()
    build_tools("test-token", executor)

    with patch(
        "app.integrations.providers.notion._common.safe_request",
        new_callable=AsyncMock,
    ) as mock_request:
        mock_request.side_effect = [
            _ok({"data_sources": [{"id": "ds-1"}]}),
            _ok({"id": "page-1", "url": "https://notion.so/page-1"}),
        ]

        result = await executor.execute_tool(
            ToolCall(
                id="call-2",
                name="create_notion_page",
                arguments={
                    "parent_id": "db-1",
                    "title": "Test page",
                    "properties_json": '{"Status": {"status": {"name": "Done"}}}',
                },
            )
        )

    assert not result.is_error, result.content
    body = mock_request.call_args_list[-1].kwargs["json"]
    assert body["properties"]["Status"] == {"status": {"name": "Done"}}


@pytest.mark.asyncio
async def test_create_notion_page_returns_actionable_error_for_garbage_string():
    """When the LLM sends a string that is neither valid JSON nor empty, the
    tool must return a structured, retryable error message — not an exception."""
    executor = _SafeIntegrationExecutor()
    build_tools("test-token", executor)

    with patch(
        "app.integrations.providers.notion._common.safe_request",
        new_callable=AsyncMock,
    ) as mock_request:
        mock_request.return_value = _ok({"data_sources": [{"id": "ds-1"}]})

        result = await executor.execute_tool(
            ToolCall(
                id="call-3",
                name="create_notion_page",
                arguments={
                    "parent_id": "db-1",
                    "title": "Test page",
                    "properties_json": "{not valid json",
                },
            )
        )

    # Tool returns the error string — agent loop will surface it back to the LLM.
    assert "Invalid properties_json" in result.content


@pytest.mark.asyncio
async def test_update_notion_page_accepts_dict_properties_json():
    executor = _SafeIntegrationExecutor()
    build_tools("test-token", executor)

    with patch(
        "app.integrations.providers.notion._common.safe_request",
        new_callable=AsyncMock,
    ) as mock_request:
        mock_request.return_value = _ok({"id": "page-1"})

        result = await executor.execute_tool(
            ToolCall(
                id="call-4",
                name="update_notion_page",
                arguments={
                    "page_id": "page-1",
                    "properties_json": {"Priority": {"select": {"name": "High"}}},
                },
            )
        )

    assert not result.is_error, result.content
    body = mock_request.call_args.kwargs["json"]
    assert body["properties"]["Priority"] == {"select": {"name": "High"}}


@pytest.mark.asyncio
async def test_query_notion_database_accepts_dict_filter_and_list_sorts():
    executor = _SafeIntegrationExecutor()
    build_tools("test-token", executor)

    with patch(
        "app.integrations.providers.notion._common.safe_request",
        new_callable=AsyncMock,
    ) as mock_request:
        # 1) GET /databases/{id} → resolve data_source_id
        # 2) POST /data_sources/{id}/query → execute query
        mock_request.side_effect = [
            _ok({"data_sources": [{"id": "ds-1"}]}),
            _ok({"results": [], "has_more": False}),
        ]

        result = await executor.execute_tool(
            ToolCall(
                id="call-5",
                name="query_notion_database",
                arguments={
                    "database_id": "db-1",
                    "filter_json": {"property": "Status", "select": {"equals": "Done"}},
                    "sorts_json": [{"property": "Created", "direction": "descending"}],
                    "count": 10,
                },
            )
        )

    assert not result.is_error, result.content
    query_body = mock_request.call_args_list[-1].kwargs["json"]
    assert query_body["filter"] == {"property": "Status", "select": {"equals": "Done"}}
    assert query_body["sorts"] == [{"property": "Created", "direction": "descending"}]


@pytest.mark.asyncio
async def test_create_notion_database_accepts_dict_properties_json():
    executor = _SafeIntegrationExecutor()
    build_tools("test-token", executor)

    with patch(
        "app.integrations.providers.notion._common.safe_request",
        new_callable=AsyncMock,
    ) as mock_request:
        mock_request.return_value = _ok(
            {"id": "db-new", "url": "https://notion.so/db-new"}
        )

        result = await executor.execute_tool(
            ToolCall(
                id="call-6",
                name="create_notion_database",
                arguments={
                    "parent_page_id": "page-parent",
                    "title": "My database",
                    "properties_json": {
                        "Name": {"title": {}},
                        "Done": {"checkbox": {}},
                    },
                },
            )
        )

    assert not result.is_error, result.content
    body = mock_request.call_args.kwargs["json"]
    # Either initial_data_source.properties (new format) or properties (legacy)
    sent_props = body.get("initial_data_source", {}).get("properties") or body.get(
        "properties"
    )
    assert sent_props == {"Name": {"title": {}}, "Done": {"checkbox": {}}}


@pytest.mark.asyncio
async def test_append_notion_blocks_json_accepts_list_directly():
    executor = _SafeIntegrationExecutor()
    build_tools("test-token", executor)

    with patch(
        "app.integrations.providers.notion._common.safe_request",
        new_callable=AsyncMock,
    ) as mock_request:
        mock_request.return_value = _ok({"results": []})

        result = await executor.execute_tool(
            ToolCall(
                id="call-7",
                name="append_notion_blocks_json",
                arguments={
                    "block_id": "page-1",
                    "children_json": [
                        {
                            "type": "heading_2",
                            "heading_2": {
                                "rich_text": [{"text": {"content": "My heading"}}]
                            },
                        }
                    ],
                },
            )
        )

    assert not result.is_error, result.content
    body = mock_request.call_args.kwargs["json"]
    assert isinstance(body["children"], list)
    assert body["children"][0]["type"] == "heading_2"


@pytest.mark.asyncio
async def test_update_notion_block_accepts_dict_block_json():
    executor = _SafeIntegrationExecutor()
    build_tools("test-token", executor)

    with patch(
        "app.integrations.providers.notion._common.safe_request",
        new_callable=AsyncMock,
    ) as mock_request:
        mock_request.return_value = _ok({"id": "block-1"})

        result = await executor.execute_tool(
            ToolCall(
                id="call-8",
                name="update_notion_block",
                arguments={
                    "block_id": "block-1",
                    "block_json": {
                        "paragraph": {
                            "rich_text": [{"text": {"content": "Updated text"}}]
                        }
                    },
                },
            )
        )

    assert not result.is_error, result.content
    body = mock_request.call_args.kwargs["json"]
    assert "paragraph" in body


# ─── update_notion_database coverage ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_update_notion_database_accepts_dict_properties_json():
    """One of the eight incident-affected tools — missing from the
    first round of regression tests. The LLM emits a dict here just like
    every other ``*_json`` arg; the tool must forward it unchanged to
    Notion's ``PATCH /data_sources/{id}`` endpoint."""
    executor = _SafeIntegrationExecutor()
    build_tools("test-token", executor)

    with patch(
        "app.integrations.providers.notion._common.safe_request",
        new_callable=AsyncMock,
    ) as mock_request:
        # 1) GET /databases/{id} → resolve data_source_id
        # 2) PATCH /data_sources/{id} → property update
        mock_request.side_effect = [
            _ok({"data_sources": [{"id": "ds-1"}]}),
            _ok({"id": "ds-1"}),
        ]

        result = await executor.execute_tool(
            ToolCall(
                id="call-9",
                name="update_notion_database",
                arguments={
                    "database_id": "db-1",
                    "properties_json": {
                        "New Column": {"rich_text": {}},
                        "Renamed": {"name": "New Name"},
                    },
                },
            )
        )

    assert not result.is_error, result.content
    patch_body = mock_request.call_args_list[-1].kwargs["json"]
    assert patch_body["properties"] == {
        "New Column": {"rich_text": {}},
        "Renamed": {"name": "New Name"},
    }


# ─── Negative validation tests ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_notion_page_rejects_list_properties_json():
    """``_parse_json_arg`` accepts ``list``, so the tool body needs its
    own ``isinstance(_, dict)`` guard — otherwise a list silently
    drops the user's intent and the page is created without the extra
    properties.

    The shape validation MUST also run BEFORE the parent-detect API
    calls so a malformed ``properties_json`` retry doesn't burn 1–2
    Notion API calls per attempt — exactly the wasted-API-calls
    pattern this incident fix is trying to remove.  ``mock_request``
    is asserted to never have been called.
    """
    executor = _SafeIntegrationExecutor()
    build_tools("test-token", executor)

    with patch(
        "app.integrations.providers.notion._common.safe_request",
        new_callable=AsyncMock,
    ) as mock_request:
        result = await executor.execute_tool(
            ToolCall(
                id="call-10",
                name="create_notion_page",
                arguments={
                    "parent_id": "db-1",
                    "title": "Test page",
                    "properties_json": [{"wrong": "shape"}],
                },
            )
        )

    # Fail-fast contract: shape validation runs BEFORE the parent-detect
    # round-trip, so a bad input never costs a Notion API call on retry.
    mock_request.assert_not_called()
    assert "must be a JSON object" in result.content
    assert "list" in result.content


@pytest.mark.asyncio
async def test_create_notion_page_rejects_garbage_string_before_api_call():
    """A malformed ``properties_json`` JSON string must also short-
    circuit BEFORE the parent-detect API call.  Pre-fix the parse
    happened inside the ``async with _client():`` block, so every retry
    paid 1–2 Notion round-trips before the deterministic parse error
    surfaced — multiplying the production retry storm.
    """
    executor = _SafeIntegrationExecutor()
    build_tools("test-token", executor)

    with patch(
        "app.integrations.providers.notion._common.safe_request",
        new_callable=AsyncMock,
    ) as mock_request:
        result = await executor.execute_tool(
            ToolCall(
                id="call-10b",
                name="create_notion_page",
                arguments={
                    "parent_id": "db-1",
                    "title": "Test page",
                    "properties_json": "{not valid json",
                },
            )
        )

    mock_request.assert_not_called()
    assert "Invalid properties_json" in result.content


@pytest.mark.asyncio
async def test_update_notion_page_rejects_list_properties_json():
    """Same guard as ``create_notion_page`` — a list short-circuits
    before hitting the Notion API instead of producing an opaque 400."""
    executor = _SafeIntegrationExecutor()
    build_tools("test-token", executor)

    with patch(
        "app.integrations.providers.notion._common.safe_request",
        new_callable=AsyncMock,
    ) as mock_request:
        result = await executor.execute_tool(
            ToolCall(
                id="call-11",
                name="update_notion_page",
                arguments={
                    "page_id": "page-1",
                    "properties_json": [{"Priority": {"select": {"name": "High"}}}],
                },
            )
        )

    # Validation MUST fire before any HTTP call — the mock must not
    # have been invoked.
    mock_request.assert_not_called()
    assert "must be a JSON object" in result.content
    assert "list" in result.content


@pytest.mark.asyncio
async def test_query_notion_database_rejects_falsy_list_filter_before_api_call():
    """``filter_json=[]`` is falsy, so the guard must distinguish absent
    (None/"") from malformed falsy values and still validate shape."""
    executor = _SafeIntegrationExecutor()
    build_tools("test-token", executor)

    with patch(
        "app.integrations.providers.notion._common.safe_request",
        new_callable=AsyncMock,
    ) as mock_request:
        result = await executor.execute_tool(
            ToolCall(
                id="call-11b",
                name="query_notion_database",
                arguments={
                    "database_id": "db-1",
                    "filter_json": [],
                    "count": 5,
                },
            )
        )

    mock_request.assert_not_called()
    assert "filter_json must be a JSON object" in result.content
    assert "list" in result.content


@pytest.mark.asyncio
async def test_query_notion_database_rejects_falsy_bool_sorts_before_api_call():
    """``sorts_json=False`` must not bypass parsing via a truthiness guard."""
    executor = _SafeIntegrationExecutor()
    build_tools("test-token", executor)

    with patch(
        "app.integrations.providers.notion._common.safe_request",
        new_callable=AsyncMock,
    ) as mock_request:
        result = await executor.execute_tool(
            ToolCall(
                id="call-11c",
                name="query_notion_database",
                arguments={
                    "database_id": "db-1",
                    "sorts_json": False,
                    "count": 5,
                },
            )
        )

    mock_request.assert_not_called()
    assert "sorts_json must be a JSON array or JSON string" in result.content
    assert "bool" in result.content


@pytest.mark.asyncio
async def test_update_notion_database_rejects_empty_json_string_properties():
    """``update_notion_database`` mirrors ``create_notion_database``'s
    non-empty-dict guard so both column-management surfaces reject the
    same wrong shapes consistently. The JSON-encoded empty-object
    string ``"{}"`` passes the outer truthiness check (non-empty
    string) but ``_parse_json_arg`` decodes it to ``{}``, which would
    no-op at Notion if forwarded. The new inner guard fast-fails with
    an LLM-actionable error instead."""
    executor = _SafeIntegrationExecutor()
    build_tools("test-token", executor)

    with patch(
        "app.integrations.providers.notion._common.safe_request",
        new_callable=AsyncMock,
    ) as mock_request:
        result = await executor.execute_tool(
            ToolCall(
                id="call-12",
                name="update_notion_database",
                arguments={
                    "database_id": "db-1",
                    "properties_json": "{}",  # decodes to empty dict
                },
            )
        )

    mock_request.assert_not_called()
    assert "non-empty JSON object" in result.content


@pytest.mark.asyncio
async def test_update_notion_database_rejects_list_properties_json():
    """A list-shaped ``properties_json`` would 400 from Notion with
    an opaque error the LLM would then retry — fail fast instead."""
    executor = _SafeIntegrationExecutor()
    build_tools("test-token", executor)

    with patch(
        "app.integrations.providers.notion._common.safe_request",
        new_callable=AsyncMock,
    ) as mock_request:
        result = await executor.execute_tool(
            ToolCall(
                id="call-12b",
                name="update_notion_database",
                arguments={
                    "database_id": "db-1",
                    "properties_json": [{"New Column": {"rich_text": {}}}],
                },
            )
        )

    mock_request.assert_not_called()
    assert "non-empty JSON object" in result.content


@pytest.mark.asyncio
async def test_query_notion_database_rejects_list_filter_json():
    """Notion's filter MUST be a JSON object (single condition or
    compound ``and``/``or`` group). A list would 400 from Notion — fail
    fast with an LLM-actionable message instead."""
    executor = _SafeIntegrationExecutor()
    build_tools("test-token", executor)

    with patch(
        "app.integrations.providers.notion._common.safe_request",
        new_callable=AsyncMock,
    ) as mock_request:
        result = await executor.execute_tool(
            ToolCall(
                id="call-13",
                name="query_notion_database",
                arguments={
                    "database_id": "db-1",
                    "filter_json": [{"property": "Status"}],  # wrong shape
                },
            )
        )

    mock_request.assert_not_called()
    assert "filter_json must be a JSON object" in result.content
    assert "list" in result.content


@pytest.mark.asyncio
async def test_query_notion_database_rejects_dict_sorts_json():
    """Notion's sorts MUST be a JSON array. A dict would 400 from
    Notion — fail fast with an LLM-actionable message."""
    executor = _SafeIntegrationExecutor()
    build_tools("test-token", executor)

    with patch(
        "app.integrations.providers.notion._common.safe_request",
        new_callable=AsyncMock,
    ) as mock_request:
        result = await executor.execute_tool(
            ToolCall(
                id="call-14",
                name="query_notion_database",
                arguments={
                    "database_id": "db-1",
                    "sorts_json": {"property": "Created"},  # wrong shape
                },
            )
        )

    mock_request.assert_not_called()
    assert "sorts_json must be a JSON array" in result.content
    assert "dict" in result.content
