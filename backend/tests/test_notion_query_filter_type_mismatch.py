"""Tests for the query_notion_database filter type-mismatch enrichment.

Production incidents (2026-07-08 / 2026-07-11):
The tool consistently returned a generic Notion 400 error whenever the LLM
guessed the wrong filter property type (e.g. ``select`` vs ``status``).
The agent had no way to self-correct and kept retrying with the same wrong
type.

The fix fetches the database schema on a type-mismatch 400 and returns an
actionable error that includes the correct type key and a corrected filter
example, so the agent can fix its filter on the very next attempt.

These tests lock the contract for:
- ``_is_filter_type_mismatch_400`` — detects the specific Notion 400 pattern
- ``_extract_filter_property_name`` / ``_extract_filter_type_key`` — filter parsing
- ``_build_filter_mismatch_hint`` — builds the actionable error string
- ``_is_filter_undefined_type_400`` — detects the missing-type-key 400 (scoped
  to ``body.filter`` so non-filter "should be defined" errors pass through)
- ``_validate_filter_missing_type`` — pre-flight validation that rejects
  conditions missing their type key (and non-object compound entries) before
  any HTTP call
- ``query_notion_database`` end-to-end: the query POST opts into
  ``expected_statuses=set()`` so the failed response is inspectable, schema is
  fetched on 400, enriched error is returned, and the original error is
  returned for non-mismatch failures.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import httpx
import pytest

from app.integrations.providers.notion import (
    _build_filter_mismatch_hint,
    _extract_filter_property_name,
    _extract_filter_type_key,
    _is_filter_type_mismatch_400,
    _is_filter_undefined_type_400,
    _validate_filter_missing_type,
    build_tools,
)
from app.services.integration_tools import _SafeIntegrationExecutor

# ── Helpers ──────────────────────────────────────────────────────────────────


def _ok(payload: dict) -> tuple[httpx.Response, None]:
    return httpx.Response(200, json=payload), None


def _err_400(message: str) -> tuple[httpx.Response, str]:
    """Simulate a 400 Bad Request from Notion (both raw resp and formatted err)."""
    resp = httpx.Response(
        400,
        json={
            "object": "error",
            "status": 400,
            "code": "validation_error",
            "message": message,
        },
    )
    formatted = f"Error: Notion rejected the request to query database — bad request. {message}"
    return resp, formatted


def _err_500() -> tuple[httpx.Response, str]:
    resp = httpx.Response(500, json={"object": "error", "status": 500, "message": "Internal"})
    return resp, "Error: Notion server error (500) while trying to query database."


_TYPE_MISMATCH_MSG = (
    "The property type in the database does not match the property type of the "
    "filter provided: database property status does not match filter select"
)

_SCHEMA_RESPONSE = {
    "id": "ds-abc",
    "object": "data_source",
    "properties": {
        "Name": {"type": "title", "id": "title"},
        "Status": {"type": "status", "id": "status-id", "status": {"options": [{"name": "In Progress"}, {"name": "Done"}]}},
        "Priority": {"type": "select", "id": "pri-id", "select": {"options": [{"name": "High"}, {"name": "Low"}]}},
        "Tags": {"type": "multi_select", "id": "tags-id", "multi_select": {"options": []}},
    },
}

def _build_handler(tool_name: str):
    executor = _SafeIntegrationExecutor()
    build_tools("fake-token", executor)
    return executor._handlers[tool_name]


def _extract_corrected_filter(hint: str) -> dict:
    """Parse the corrected-filter JSON embedded in a mismatch hint.

    ``json.JSONDecoder.raw_decode`` stops exactly at the end of the JSON
    object, so the extraction is robust against any characters (including
    ``.``) appearing inside the payload values.
    """
    start = hint.index("Corrected filter: ") + len("Corrected filter: ")
    corrected, _ = json.JSONDecoder().raw_decode(hint, start)
    assert isinstance(corrected, dict)
    return corrected


# ── Unit tests: _is_filter_type_mismatch_400 ─────────────────────────────────


def test_is_filter_type_mismatch_400_detects_mismatch():
    resp = httpx.Response(
        400,
        json={
            "object": "error",
            "status": 400,
            "message": "The property type in the database does not match the property type "
            "of the filter provided: database property status does not match filter select",
        },
    )
    assert _is_filter_type_mismatch_400(resp) is True


def test_is_filter_type_mismatch_400_ignores_non_mismatch_400():
    resp = httpx.Response(
        400,
        json={"object": "error", "status": 400, "message": "Could not find property with name or id."},
    )
    assert _is_filter_type_mismatch_400(resp) is False


def test_is_filter_type_mismatch_400_ignores_non_400():
    resp = httpx.Response(
        404,
        json={"object": "error", "status": 404, "message": "database property status does not match filter select"},
    )
    assert _is_filter_type_mismatch_400(resp) is False


def test_is_filter_type_mismatch_400_returns_false_for_none():
    assert _is_filter_type_mismatch_400(None) is False


def test_is_filter_type_mismatch_400_multi_select_text():
    """Reproduces the multi_select-vs-text mismatch from the incident log."""
    resp = httpx.Response(
        400,
        json={
            "message": "The property type in the database does not match the property type "
            "of the filter provided: database property multi_select does not match filter text"
        },
    )
    assert _is_filter_type_mismatch_400(resp) is True


# ── Unit tests: _extract_filter_property_name / _extract_filter_type_key ─────


def test_extract_filter_property_name_simple():
    assert _extract_filter_property_name({"property": "Status", "select": {}}) == "Status"


def test_extract_filter_property_name_returns_none_for_compound():
    assert _extract_filter_property_name({"and": [{"property": "Status", "select": {}}]}) is None


def test_extract_filter_property_name_returns_none_for_none():
    assert _extract_filter_property_name(None) is None  # type: ignore[arg-type]


def test_extract_filter_type_key_select():
    assert _extract_filter_type_key({"property": "Priority", "select": {"equals": "High"}}) == "select"


def test_extract_filter_type_key_status():
    assert _extract_filter_type_key({"property": "Status", "status": {"equals": "Done"}}) == "status"


def test_extract_filter_type_key_title():
    assert _extract_filter_type_key({"property": "Name", "title": {"contains": "Meeting"}}) == "title"


def test_extract_filter_type_key_multi_select():
    assert _extract_filter_type_key({"property": "Tags", "multi_select": {"contains": "Frontend"}}) == "multi_select"


def test_extract_filter_type_key_skips_timestamp():
    # timestamp filters don't have a "property" key but have a "timestamp" key
    assert _extract_filter_type_key({"timestamp": "created_time", "date": {"equals": "2024-01-01"}}) == "date"


def test_extract_filter_type_key_returns_none_for_compound():
    assert _extract_filter_type_key({"and": []}) is None


# ── Unit tests: _build_filter_mismatch_hint ───────────────────────────────────


def test_build_hint_select_vs_status():
    """LLM used 'select' for a 'status' property."""
    hint = _build_filter_mismatch_hint(
        {"property": "Status", "select": {"equals": "Done"}},
        _SCHEMA_RESPONSE["properties"],
        _TYPE_MISMATCH_MSG,
    )
    assert "Status" in hint
    assert "'status'" in hint
    assert "'select'" in hint
    # Should contain the corrected filter JSON
    assert '"status"' in hint
    assert "get_notion_database" in hint


def test_build_hint_includes_corrected_filter_json():
    """The corrected filter must be parseable and use the real type key."""
    hint = _build_filter_mismatch_hint(
        {"property": "Status", "select": {"equals": "Done"}},
        _SCHEMA_RESPONSE["properties"],
        _TYPE_MISMATCH_MSG,
    )
    corrected = _extract_corrected_filter(hint)
    assert corrected["property"] == "Status"
    assert "status" in corrected
    assert corrected["status"] == {"equals": "Done"}
    assert "select" not in corrected


def test_build_hint_case_insensitive_property_lookup():
    """Property name lookup must be case-insensitive."""
    hint = _build_filter_mismatch_hint(
        {"property": "status", "select": {"equals": "Done"}},  # lowercase name
        _SCHEMA_RESPONSE["properties"],
        _TYPE_MISMATCH_MSG,
    )
    # Should still find 'Status' in the schema and use 'status' type
    assert "'status'" in hint
    assert "Corrected filter:" in hint


def test_build_hint_property_not_in_schema():
    hint = _build_filter_mismatch_hint(
        {"property": "Nonexistent", "select": {"equals": "Done"}},
        _SCHEMA_RESPONSE["properties"],
        _TYPE_MISMATCH_MSG,
    )
    assert "not found in the database schema" in hint
    assert "get_notion_database" in hint


def test_build_hint_compound_filter_no_property_key():
    hint = _build_filter_mismatch_hint(
        {"and": [{"property": "Status", "select": {}}]},
        _SCHEMA_RESPONSE["properties"],
        _TYPE_MISMATCH_MSG,
    )
    assert "get_notion_database" in hint
    # Should not try to suggest a specific corrected filter for compound case
    assert "Corrected filter:" not in hint


def test_build_hint_adjusts_equals_to_contains_for_multi_select():
    """A carried-over 'equals' operator is invalid for multi_select — must become 'contains'."""
    hint = _build_filter_mismatch_hint(
        {"property": "Tags", "select": {"equals": "Frontend"}},
        _SCHEMA_RESPONSE["properties"],
        _TYPE_MISMATCH_MSG,
    )
    corrected = _extract_corrected_filter(hint)
    assert corrected == {"property": "Tags", "multi_select": {"contains": "Frontend"}}


def test_build_hint_adjusts_contains_to_equals_for_select():
    """A carried-over 'contains' operator is invalid for select — must become 'equals'."""
    hint = _build_filter_mismatch_hint(
        {"property": "Priority", "multi_select": {"contains": "High"}},
        _SCHEMA_RESPONSE["properties"],
        _TYPE_MISMATCH_MSG,
    )
    corrected = _extract_corrected_filter(hint)
    assert corrected == {"property": "Priority", "select": {"equals": "High"}}


def test_build_hint_corrected_filter_with_dot_in_value():
    """Values containing '.' (e.g. version strings) must not truncate the extraction."""
    hint = _build_filter_mismatch_hint(
        {"property": "Status", "select": {"equals": "v1.2 (rc.3)"}},
        _SCHEMA_RESPONSE["properties"],
        _TYPE_MISMATCH_MSG,
    )
    corrected = _extract_corrected_filter(hint)
    assert corrected == {"property": "Status", "status": {"equals": "v1.2 (rc.3)"}}


def test_build_hint_keeps_valid_operator_unchanged():
    """Operators valid for the actual type (e.g. status 'equals') pass through untouched."""
    hint = _build_filter_mismatch_hint(
        {"property": "Status", "select": {"equals": "Done"}},
        _SCHEMA_RESPONSE["properties"],
        _TYPE_MISMATCH_MSG,
    )
    corrected = _extract_corrected_filter(hint)
    assert corrected["status"] == {"equals": "Done"}


# ── Integration tests: query_notion_database end-to-end ───────────────────────


@pytest.mark.asyncio
async def test_query_enriches_select_vs_status_mismatch():
    """select-filter against a status property → enriched error with corrected filter."""
    handler = _build_handler("query_notion_database")
    calls: list[dict[str, Any]] = []

    async def fake_safe_request(
        _client: Any,
        method: str,
        url: str,
        _provider: str,
        _action: str,
        **kwargs: Any,
    ) -> tuple[httpx.Response | None, str | None]:
        calls.append({"method": method, "url": url, **kwargs})
        if method == "get" and "/databases/" in url:
            # _resolve_data_source_id: database → data_source
            return _ok({"data_sources": [{"id": "ds-abc"}]})
        if method == "post" and "/data_sources/ds-abc/query" in url:
            return _err_400(_TYPE_MISMATCH_MSG)
        if method == "get" and "/data_sources/ds-abc" in url:
            return _ok(_SCHEMA_RESPONSE)
        raise AssertionError(f"Unexpected request: {method} {url}")

    with patch(
        "app.integrations.providers.notion._common.safe_request",
        new=fake_safe_request,
    ):
        result = await handler(
            database_id="db-abc",
            filter_json='{"property": "Status", "select": {"equals": "Done"}}',
        )

    # Must include the corrected type key and a parseable filter
    assert "status" in result.lower()
    assert "select" in result.lower()
    assert "Corrected filter:" in result
    assert "get_notion_database" in result
    # Schema fetch should have been called
    schema_calls = [c for c in calls if c["method"] == "get" and c["url"].replace("https://api.notion.com/v1", "") == "/data_sources/ds-abc"]
    assert schema_calls, "Schema fetch was not called"


@pytest.mark.asyncio
async def test_query_enriches_status_vs_select_mismatch():
    """status-filter against a select property (the reverse direction) → enriched error."""
    handler = _build_handler("query_notion_database")

    mismatch_msg = (
        "The property type in the database does not match the property type of the "
        "filter provided: database property select does not match filter status"
    )

    async def fake_safe_request(
        _client: Any,
        method: str,
        url: str,
        _provider: str,
        _action: str,
        **kwargs: Any,
    ) -> tuple[httpx.Response | None, str | None]:
        if method == "get" and "/databases/" in url:
            return _ok({"data_sources": [{"id": "ds-abc"}]})
        if method == "post" and "/data_sources/ds-abc/query" in url:
            return _err_400(mismatch_msg)
        if method == "get" and "/data_sources/ds-abc" in url:
            return _ok(_SCHEMA_RESPONSE)
        raise AssertionError(f"Unexpected: {method} {url}")

    with patch(
        "app.integrations.providers.notion._common.safe_request",
        new=fake_safe_request,
    ):
        result = await handler(
            database_id="db-abc",
            filter_json='{"property": "Priority", "status": {"equals": "High"}}',
        )

    # Priority is a 'select' property → corrected filter should use 'select'
    assert "select" in result.lower()
    assert "Corrected filter:" in result


@pytest.mark.asyncio
async def test_query_returns_fallback_hint_when_schema_fetch_fails():
    """When schema fetch fails, return a shorter hint (not the generic raw error)."""
    handler = _build_handler("query_notion_database")

    async def fake_safe_request(
        _client: Any,
        method: str,
        url: str,
        _provider: str,
        _action: str,
        **kwargs: Any,
    ) -> tuple[httpx.Response | None, str | None]:
        if method == "get" and "/databases/" in url:
            return _ok({"data_sources": [{"id": "ds-abc"}]})
        if method == "post" and "/data_sources/ds-abc/query" in url:
            return _err_400(_TYPE_MISMATCH_MSG)
        if method == "get" and "/data_sources/ds-abc" in url:
            # Schema fetch itself fails
            err_resp = httpx.Response(500, json={"object": "error", "status": 500})
            return err_resp, "Error: Notion server error (500)."
        raise AssertionError(f"Unexpected: {method} {url}")

    with patch(
        "app.integrations.providers.notion._common.safe_request",
        new=fake_safe_request,
    ):
        result = await handler(
            database_id="db-abc",
            filter_json='{"property": "Status", "select": {"equals": "Done"}}',
        )

    # Should still mention the property name and mismatch, not just raw error
    assert "Status" in result
    assert "select" in result.lower()
    assert "get_notion_database" in result


@pytest.mark.asyncio
async def test_query_returns_fallback_hint_when_schema_response_malformed():
    """A schema fetch that succeeds but returns an unusable body (e.g. a JSON
    array) must still yield the fallback mismatch hint, never the raw error."""
    handler = _build_handler("query_notion_database")

    async def fake_safe_request(
        _client: Any,
        method: str,
        url: str,
        _provider: str,
        _action: str,
        **kwargs: Any,
    ) -> tuple[httpx.Response | None, str | None]:
        if method == "get" and "/databases/" in url:
            return _ok({"data_sources": [{"id": "ds-abc"}]})
        if method == "post" and "/data_sources/ds-abc/query" in url:
            return _err_400(_TYPE_MISMATCH_MSG)
        if method == "get" and "/data_sources/ds-abc" in url:
            # 200 OK but the body is a list — .get() raises inside enrichment.
            return httpx.Response(200, json=[{"unexpected": "shape"}]), None
        raise AssertionError(f"Unexpected: {method} {url}")

    with patch(
        "app.integrations.providers.notion._common.safe_request",
        new=fake_safe_request,
    ):
        result = await handler(
            database_id="db-abc",
            filter_json='{"property": "Status", "select": {"equals": "Done"}}',
        )

    # Fallback hint, not the raw passthrough error.
    assert "Status" in result
    assert "select" in result.lower()
    assert "get_notion_database" in result
    assert "bad request" not in result.lower()


def test_build_hint_tolerates_non_dict_schema_property():
    """A schema entry whose value is not a dict must not crash the hint builder."""
    hint = _build_filter_mismatch_hint(
        {"property": "Status", "select": {"equals": "Done"}},
        {"Status": "unexpected-string"},
        _TYPE_MISMATCH_MSG,
    )
    # No type could be determined — degrade to the not-found style hint.
    assert "get_notion_database" in hint


@pytest.mark.asyncio
async def test_query_compound_filter_mismatch_skips_schema_fetch():
    """Compound (and/or) filters get the generic hint without an extra schema GET."""
    handler = _build_handler("query_notion_database")
    calls: list[dict[str, Any]] = []

    async def fake_safe_request(
        _client: Any,
        method: str,
        url: str,
        _provider: str,
        _action: str,
        **kwargs: Any,
    ) -> tuple[httpx.Response | None, str | None]:
        calls.append({"method": method, "url": url})
        if method == "get" and "/databases/" in url:
            return _ok({"data_sources": [{"id": "ds-abc"}]})
        if method == "post" and "/data_sources/ds-abc/query" in url:
            return _err_400(_TYPE_MISMATCH_MSG)
        raise AssertionError(f"Unexpected: {method} {url}")

    with patch(
        "app.integrations.providers.notion._common.safe_request",
        new=fake_safe_request,
    ):
        result = await handler(
            database_id="db-abc",
            filter_json='{"and": [{"property": "Status", "select": {"equals": "Done"}}]}',
        )

    # The schema cannot pinpoint the offending sub-condition — no extra GET.
    schema_calls = [
        c for c in calls if c["method"] == "get" and "/data_sources/" in c["url"]
    ]
    assert not schema_calls, "Schema was unexpectedly fetched for a compound filter"
    # Still an actionable hint, not the raw passthrough error.
    assert "get_notion_database" in result
    assert "Corrected filter:" not in result


@pytest.mark.asyncio
async def test_query_passthrough_non_mismatch_400():
    """A generic 400 (not a type mismatch) is returned as-is without schema fetch."""
    handler = _build_handler("query_notion_database")
    calls: list[dict[str, Any]] = []

    other_400_msg = "Could not find property with name or id: NonExistent"

    async def fake_safe_request(
        _client: Any,
        method: str,
        url: str,
        _provider: str,
        _action: str,
        **kwargs: Any,
    ) -> tuple[httpx.Response | None, str | None]:
        calls.append({"method": method, "url": url})
        if method == "get" and "/databases/" in url:
            return _ok({"data_sources": [{"id": "ds-abc"}]})
        if method == "post" and "/data_sources/ds-abc/query" in url:
            return _err_400(other_400_msg)
        raise AssertionError(f"Unexpected: {method} {url}")

    with patch(
        "app.integrations.providers.notion._common.safe_request",
        new=fake_safe_request,
    ):
        result = await handler(
            database_id="db-abc",
            filter_json='{"property": "NonExistent", "select": {"equals": "X"}}',
        )

    # Schema should NOT have been fetched for a non-mismatch 400
    schema_calls = [c for c in calls if c["method"] == "get" and "/data_sources/" in c["url"]]
    assert not schema_calls, "Schema was unexpectedly fetched for a non-mismatch 400"
    # Original error should be returned
    assert "bad request" in result.lower() or "could not find" in result.lower()


@pytest.mark.asyncio
async def test_query_passthrough_non_filter_error():
    """Non-400 errors (e.g. 500) bypass the mismatch logic entirely."""
    handler = _build_handler("query_notion_database")

    async def fake_safe_request(
        _client: Any,
        method: str,
        url: str,
        _provider: str,
        _action: str,
        **kwargs: Any,
    ) -> tuple[httpx.Response | None, str | None]:
        if method == "get" and "/databases/" in url:
            return _ok({"data_sources": [{"id": "ds-abc"}]})
        if method == "post" and "/data_sources/ds-abc/query" in url:
            return _err_500()
        raise AssertionError(f"Unexpected: {method} {url}")

    with patch(
        "app.integrations.providers.notion._common.safe_request",
        new=fake_safe_request,
    ):
        result = await handler(
            database_id="db-abc",
            filter_json='{"property": "Status", "select": {"equals": "Done"}}',
        )

    assert "server error" in result.lower() or "500" in result


@pytest.mark.asyncio
async def test_query_no_filter_no_enrichment():
    """A query without a filter that fails should not trigger the enrichment path."""
    handler = _build_handler("query_notion_database")
    calls: list[dict[str, Any]] = []

    async def fake_safe_request(
        _client: Any,
        method: str,
        url: str,
        _provider: str,
        _action: str,
        **kwargs: Any,
    ) -> tuple[httpx.Response | None, str | None]:
        calls.append({"method": method, "url": url})
        if method == "get" and "/databases/" in url:
            return _ok({"data_sources": [{"id": "ds-abc"}]})
        if method == "post" and "/data_sources/ds-abc/query" in url:
            return _err_400(_TYPE_MISMATCH_MSG)
        raise AssertionError(f"Unexpected: {method} {url}")

    with patch(
        "app.integrations.providers.notion._common.safe_request",
        new=fake_safe_request,
    ):
        # No filter_json — should not trigger enrichment
        result = await handler(database_id="db-abc")

    # Schema should NOT be fetched (no filter in payload)
    schema_calls = [c for c in calls if c["method"] == "get" and "/data_sources/" in c["url"]]
    assert not schema_calls
    assert "bad request" in result.lower()


@pytest.mark.asyncio
async def test_query_successful_returns_entries():
    """Happy path: successful query is unaffected by the mismatch logic."""
    handler = _build_handler("query_notion_database")

    async def fake_safe_request(
        _client: Any,
        method: str,
        url: str,
        _provider: str,
        _action: str,
        **kwargs: Any,
    ) -> tuple[httpx.Response | None, str | None]:
        if method == "get" and "/databases/" in url:
            return _ok({"data_sources": [{"id": "ds-abc"}]})
        if method == "post" and "/data_sources/ds-abc/query" in url:
            return _ok(
                {
                    "results": [
                        {
                            "id": "page-1",
                            "object": "page",
                            "url": "https://notion.so/page-1",
                            "properties": {
                                "Name": {
                                    "type": "title",
                                    "title": [{"plain_text": "My Task"}],
                                }
                            },
                        }
                    ],
                    "has_more": False,
                }
            )
        if method == "get" and "/pages/page-1" in url:
            # Auto-expand: page metadata fetch (≤5 results triggers full content)
            return _ok(
                {
                    "id": "page-1",
                    "object": "page",
                    "parent": {"type": "database_id", "database_id": "db-abc"},
                    "properties": {
                        "Name": {"type": "title", "title": [{"plain_text": "My Task"}]}
                    },
                }
            )
        if method == "get" and "/blocks/page-1/children" in url:
            # Auto-expand: block children fetch
            return _ok({"results": [], "has_more": False})
        # No schema fetch should happen on success
        raise AssertionError(f"Unexpected: {method} {url}")

    with patch(
        "app.integrations.providers.notion._common.safe_request",
        new=fake_safe_request,
    ):
        result = await handler(
            database_id="db-abc",
            filter_json='{"property": "Status", "status": {"equals": "Done"}}',
        )

    assert "My Task" in result
    assert "Error" not in result


# ── Unit tests: _is_filter_undefined_type_400 ─────────────────────────────────


def test_is_filter_undefined_type_400_detects_should_be_defined():
    """The 'should be defined, instead was undefined' Notion 400 pattern is detected."""
    resp = httpx.Response(
        400,
        json={
            "object": "error",
            "status": 400,
            "message": "body.filter.and[0].title/rich_text/number/checkbox should be defined, instead was 'undefined'",
        },
    )
    assert _is_filter_undefined_type_400(resp) is True


def test_is_filter_undefined_type_400_ignores_mismatch_400():
    """A type-mismatch 400 is not confused with the undefined-type 400."""
    resp = httpx.Response(
        400,
        json={
            "object": "error",
            "status": 400,
            "message": "The property type in the database does not match the property type of the filter provided",
        },
    )
    assert _is_filter_undefined_type_400(resp) is False


def test_is_filter_undefined_type_400_ignores_non_400():
    resp = httpx.Response(
        404,
        json={"object": "error", "status": 404, "message": "title should be defined, instead was 'undefined'"},
    )
    assert _is_filter_undefined_type_400(resp) is False


def test_is_filter_undefined_type_400_returns_false_for_none():
    assert _is_filter_undefined_type_400(None) is False


def test_is_filter_undefined_type_400_ignores_non_filter_undefined_400():
    """Notion uses the same 'should be defined / undefined' wording for missing
    non-filter fields (e.g. sorts) — those must NOT be diagnosed as a missing
    filter type key."""
    resp = httpx.Response(
        400,
        json={
            "object": "error",
            "status": 400,
            "message": "body.sorts[0].direction should be defined, instead was 'undefined'",
        },
    )
    assert _is_filter_undefined_type_400(resp) is False


# ── Unit tests: _validate_filter_missing_type ─────────────────────────────────


def test_validate_filter_missing_type_simple_valid_passes():
    """A well-formed simple filter returns None."""
    assert _validate_filter_missing_type({"property": "Status", "select": {"equals": "Done"}}) is None


def test_validate_filter_missing_type_title_filter_passes():
    assert _validate_filter_missing_type({"property": "Name", "title": {"contains": "Meeting"}}) is None


def test_validate_filter_missing_type_simple_missing_type_key():
    """A simple condition with only ``property`` and no type key returns an error."""
    err = _validate_filter_missing_type({"property": "Name"})
    assert err is not None
    assert "missing" in err.lower()
    assert "Name" in err
    assert "get_notion_database" in err
    # Must include example format hint
    assert "title" in err or "rich_text" in err or "<type>" in err


def test_validate_filter_missing_type_compound_and_with_missing_inner():
    """An ``and`` compound filter whose sub-condition has no type key returns an error."""
    err = _validate_filter_missing_type(
        {"and": [{"property": "Name"}, {"property": "Status", "select": {"equals": "Done"}}]}
    )
    assert err is not None
    assert "Name" in err
    assert "filter.and[0]" in err


def test_validate_filter_missing_type_compound_or_with_missing_inner():
    """An ``or`` compound filter whose second sub-condition has no type key returns an error."""
    err = _validate_filter_missing_type(
        {"or": [{"property": "Status", "select": {"equals": "Done"}}, {"property": "Priority"}]}
    )
    assert err is not None
    assert "Priority" in err
    assert "filter.or[1]" in err


def test_validate_filter_missing_type_compound_and_all_valid_passes():
    """An ``and`` compound filter where every sub-condition has a type key returns None."""
    err = _validate_filter_missing_type(
        {
            "and": [
                {"property": "Status", "select": {"equals": "Done"}},
                {"property": "Name", "title": {"contains": "Meeting"}},
            ]
        }
    )
    assert err is None


def test_validate_filter_missing_type_non_dict_input_returns_none():
    """Non-dict inputs are tolerated without raising (None / list / etc.)."""
    assert _validate_filter_missing_type(None) is None  # type: ignore[arg-type]
    assert _validate_filter_missing_type("string") is None  # type: ignore[arg-type]


def test_validate_filter_missing_type_non_dict_compound_entry_fails_fast():
    """A non-object entry inside an 'and'/'or' list must fail pre-flight with
    the exact path instead of being skipped."""
    err = _validate_filter_missing_type(
        {"and": [{"property": "Status", "select": {"equals": "Done"}}, "not-a-condition"]}
    )
    assert err is not None
    assert "filter.and[1]" in err
    assert "must be a JSON object" in err
    assert "str" in err


def test_validate_filter_missing_type_compound_list_not_a_list():
    """A compound key whose value is not a list is rejected with a clear error."""
    err = _validate_filter_missing_type({"or": {"property": "Status"}})
    assert err is not None
    assert "filter.or" in err
    assert "must be a list" in err


def test_validate_filter_missing_type_checks_both_and_and_or_branches():
    """A dict (incorrectly) carrying both 'and' and 'or' at the same level must
    have BOTH branches validated — a malformed entry in the 'or' branch cannot
    slip past pre-flight just because the 'and' branch validated first."""
    err = _validate_filter_missing_type(
        {
            "and": [{"property": "Status", "select": {"equals": "Done"}}],
            "or": [{"property": "Priority"}],
        }
    )
    assert err is not None
    assert "filter.or[0]" in err
    assert "Priority" in err


def test_validate_filter_missing_type_both_branches_valid_passes():
    """Both-compound-keys dict where every entry is well-formed returns None."""
    err = _validate_filter_missing_type(
        {
            "and": [{"property": "Status", "select": {"equals": "Done"}}],
            "or": [{"property": "Name", "title": {"contains": "x"}}],
        }
    )
    assert err is None


def test_validate_filter_missing_type_timestamp_filter_passes():
    """Timestamp filters (no ``property`` key) are valid and must not be rejected."""
    assert _validate_filter_missing_type({"timestamp": "created_time", "date": {"equals": "2024-01-01"}}) is None


def test_validate_filter_missing_type_nested_path_in_error():
    """The error message must include the correct nested path for deep compounds."""
    err = _validate_filter_missing_type(
        {"and": [{"property": "Status", "select": {"equals": "Done"}}, {"property": "BadOne"}]}
    )
    assert err is not None
    assert "filter.and[1]" in err


# ── Integration tests: pre-flight validation in query_notion_database ─────────


@pytest.mark.asyncio
async def test_query_rejects_simple_filter_missing_type_before_api_call():
    """A simple filter with only ``property`` and no type key fails pre-flight
    without making any API call to Notion."""
    handler = _build_handler("query_notion_database")

    async def fake_safe_request(
        _client: Any,
        method: str,
        url: str,
        _provider: str,
        _action: str,
        **kwargs: Any,
    ) -> tuple[httpx.Response | None, str | None]:
        raise AssertionError(f"Unexpected API call: {method} {url}")

    with patch(
        "app.integrations.providers.notion._common.safe_request",
        new=fake_safe_request,
    ):
        result = await handler(
            database_id="db-abc",
            filter_json='{"property": "Name"}',
        )

    assert "missing" in result.lower()
    assert "Name" in result
    assert "get_notion_database" in result


@pytest.mark.asyncio
async def test_query_rejects_compound_and_filter_missing_type_before_api_call():
    """An ``and`` compound filter with a type-less condition fails pre-flight."""
    handler = _build_handler("query_notion_database")
    calls: list[dict] = []

    async def fake_safe_request(
        _client: Any,
        method: str,
        url: str,
        _provider: str,
        _action: str,
        **kwargs: Any,
    ) -> tuple[httpx.Response | None, str | None]:
        calls.append({"method": method, "url": url})
        # Allow the data-source resolution lookup
        if method == "get" and "/databases/" in url:
            return _ok({"data_sources": [{"id": "ds-abc"}]})
        raise AssertionError(f"Unexpected API call: {method} {url}")

    with patch(
        "app.integrations.providers.notion._common.safe_request",
        new=fake_safe_request,
    ):
        result = await handler(
            database_id="db-abc",
            filter_json='{"and": [{"property": "Name"}, {"property": "Status", "select": {"equals": "Done"}}]}',
        )

    # No query POST should have been made
    post_calls = [c for c in calls if c["method"] == "post"]
    assert not post_calls, "Query POST was made despite pre-flight failure"
    assert "missing" in result.lower()
    assert "Name" in result


@pytest.mark.asyncio
async def test_query_valid_filter_passes_preflight_and_succeeds():
    """A well-formed filter passes pre-flight and the query proceeds normally."""
    handler = _build_handler("query_notion_database")

    async def fake_safe_request(
        _client: Any,
        method: str,
        url: str,
        _provider: str,
        _action: str,
        **kwargs: Any,
    ) -> tuple[httpx.Response | None, str | None]:
        if method == "get" and "/databases/" in url:
            return _ok({"data_sources": [{"id": "ds-abc"}]})
        if method == "post" and "/data_sources/ds-abc/query" in url:
            return _ok({"results": [], "has_more": False})
        raise AssertionError(f"Unexpected: {method} {url}")

    with patch(
        "app.integrations.providers.notion._common.safe_request",
        new=fake_safe_request,
    ):
        result = await handler(
            database_id="db-abc",
            filter_json='{"property": "Status", "select": {"equals": "Done"}}',
        )

    assert "No entries found" in result


@pytest.mark.asyncio
async def test_query_post_call_undefined_type_400_returns_actionable_error():
    """Even if the pre-flight passes, a Notion 'should be defined' 400 is caught
    post-call and returned as an actionable error (not a raw passthrough)."""
    handler = _build_handler("query_notion_database")

    undefined_msg = "body.filter.title/rich_text/number/checkbox should be defined, instead was 'undefined'"

    async def fake_safe_request(
        _client: Any,
        method: str,
        url: str,
        _provider: str,
        _action: str,
        **kwargs: Any,
    ) -> tuple[httpx.Response | None, str | None]:
        if method == "get" and "/databases/" in url:
            return _ok({"data_sources": [{"id": "ds-abc"}]})
        if method == "post" and "/data_sources/ds-abc/query" in url:
            resp = httpx.Response(
                400,
                json={"object": "error", "status": 400, "message": undefined_msg},
            )
            formatted = f"Error: Notion rejected the request — bad request. {undefined_msg}"
            return resp, formatted
        raise AssertionError(f"Unexpected: {method} {url}")

    with patch(
        "app.integrations.providers.notion._common.safe_request",
        new=fake_safe_request,
    ):
        # Use a valid-looking filter so pre-flight passes (has type key)
        result = await handler(
            database_id="db-abc",
            filter_json='{"property": "Status", "select": {"equals": "Done"}}',
        )

    assert "missing" in result.lower() or "should be defined" in result.lower() or "type key" in result.lower()
    assert "get_notion_database" in result


@pytest.mark.asyncio
async def test_query_post_opts_into_expected_statuses_contract():
    """The query POST must pass ``expected_statuses=set()`` to safe_request.

    Without it the real safe_request returns ``(None, err)`` for any non-2xx
    (the historical contract), so ``resp`` is ``None`` and NEITHER the
    missing-type-key handler NOR the type-mismatch enrichment can ever
    inspect ``resp.status_code`` in production — the enrichment paths would be
    dead code that only fires under test mocks. This test simulates the real
    contract: the failed response is only handed back when the caller opted in.
    """
    handler = _build_handler("query_notion_database")

    undefined_msg = "body.filter.title/rich_text/number/checkbox should be defined, instead was 'undefined'"

    async def fake_safe_request(
        _client: Any,
        method: str,
        url: str,
        _provider: str,
        _action: str,
        **kwargs: Any,
    ) -> tuple[httpx.Response | None, str | None]:
        if method == "get" and "/databases/" in url:
            return _ok({"data_sources": [{"id": "ds-abc"}]})
        if method == "post" and "/data_sources/ds-abc/query" in url:
            resp = httpx.Response(
                400,
                json={"object": "error", "status": 400, "message": undefined_msg},
            )
            formatted = f"Error: Notion rejected the request — bad request. {undefined_msg}"
            # Faithful to app.integrations.http.safe_request: the failed
            # response is returned ONLY when the caller opted in via
            # expected_statuses; otherwise (None, err).
            if kwargs.get("expected_statuses") is not None:
                return resp, formatted
            return None, formatted
        raise AssertionError(f"Unexpected: {method} {url}")

    with patch(
        "app.integrations.providers.notion._common.safe_request",
        new=fake_safe_request,
    ):
        result = await handler(
            database_id="db-abc",
            filter_json='{"property": "Status", "select": {"equals": "Done"}}',
        )

    # If the opt-in kwarg were missing, resp would be None and the raw
    # passthrough error would be returned instead of the actionable hint.
    assert "type key" in result.lower()
    assert "get_notion_database" in result
