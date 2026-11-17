"""Tests for defensive handling when a Notion page ID is passed as a database ID.

Production incident (2026-07-07 through 2026-07-13):
The "JP Digital Clone" agent's "Morning Focus Brief" assignment stored a Notion
*page* ID as a database_id.  Every daily run triggered a cascade of three
failing tool calls:
  1. GET /v1/data_sources/{id} → 404 "not found"
  2. GET /v1/databases/{id}    → 400 "is a page, not a database"
  3. POST /v1/data_sources/{id}/query → fails with the original page ID

The fix short-circuits step 2: ``resolve_data_source_id`` now returns
``_PAGE_NOT_DATABASE_SENTINEL`` on detection of the Notion 400 "is a page, not
a database" response.  The calling tool functions detect the sentinel and
immediately return an actionable "use get_notion_page" error — eliminating the
wasted tool calls and the 30-second timeout spike.

Covered:
- ``resolve_data_source_id``: returns sentinel on 400 "is a page", skips data_source step
- ``normalize_relation_properties``: actionable error when a relation target is a page
  (the sentinel is never serialized into the request payload)
- ``query_notion_database``: returns actionable error when sentinel detected
- ``get_notion_database``: returns actionable error when databases 400 "is a page"
- ``list_notion_templates``: returns actionable error when sentinel detected
- ``update_notion_database``: returns actionable error when sentinel detected
- Non-page 400 errors are NOT intercepted (existing paths preserved)
"""

from __future__ import annotations

import json
import uuid
from typing import Any
from unittest.mock import patch

import httpx
import pytest

from app.integrations.providers.notion import (
    _PAGE_NOT_DATABASE_SENTINEL,
    build_tools,
)
from app.services.integration_tools import _SafeIntegrationExecutor

# ── Shared constants ─────────────────────────────────────────────────────────

_PAGE_ID = "1bb8fce1-7f2a-8034-a80a-e6765e22ab22"
_DB_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
_DS_ID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


# ── Helpers ──────────────────────────────────────────────────────────────────


def _ok(payload: dict) -> tuple[httpx.Response, None]:
    return httpx.Response(200, json=payload), None


def _page_not_db_msg(page_id: str) -> str:
    """Notion's exact error message when a page ID is used as a database ID."""
    return (
        f"Provided database_id {page_id.replace('-', '')} "
        "is a page, not a database."
    )


def _page_not_db_400(page_id: str = _PAGE_ID) -> tuple[httpx.Response, str]:
    """Simulate Notion's 400 'is a page, not a database' response."""
    msg = _page_not_db_msg(page_id)
    resp = httpx.Response(
        400,
        json={
            "object": "error",
            "status": 400,
            "code": "validation_error",
            "message": msg,
        },
    )
    formatted = (
        "Error: Notion rejected the request to resolve data source — "
        f"bad request. {msg}"
    )
    return resp, formatted


def _not_found_404(resource_id: str = _PAGE_ID) -> tuple[None, str]:
    return None, "Error: Notion resource not found while trying to check data source."


def _build_handler(tool_name: str):
    executor = _SafeIntegrationExecutor()
    build_tools("fake-token", executor)
    return executor._handlers[tool_name]


# ── Unit test: _PAGE_NOT_DATABASE_SENTINEL value ──────────────────────────────


def test_page_not_database_sentinel_is_a_non_empty_string():
    """The sentinel must be a non-empty, non-UUID string so it can never be a valid ID."""
    assert isinstance(_PAGE_NOT_DATABASE_SENTINEL, str)
    assert _PAGE_NOT_DATABASE_SENTINEL
    # Notion IDs are UUIDs (dashed or dashless) — the sentinel must never
    # parse as one, so it can never collide with a real database ID.
    with pytest.raises(ValueError):
        uuid.UUID(_PAGE_NOT_DATABASE_SENTINEL)


# ── resolve_data_source_id: sentinel returned on page 400 ────────────────────


@pytest.mark.asyncio
async def test_resolve_data_source_id_returns_sentinel_for_page_400():
    """resolve_data_source_id returns _PAGE_NOT_DATABASE_SENTINEL on 400 'is a page'."""
    from app.integrations.providers.notion._common import NotionContext

    ctx = NotionContext("fake-token", None)

    async def fake_safe_request(
        _client: Any,
        method: str,
        url: str,
        _provider: str,
        _action: str,
        **kwargs: Any,
    ) -> tuple[httpx.Response | None, str | None]:
        if method == "get" and f"/databases/{_PAGE_ID}" in url:
            return _page_not_db_400(_PAGE_ID)
        raise AssertionError(f"Unexpected request: {method} {url}")

    with patch(
        "app.integrations.providers.notion._common.safe_request",
        new=fake_safe_request,
    ):
        async with ctx.client() as client:
            result = await ctx.resolve_data_source_id(client, _PAGE_ID)

    assert result == _PAGE_NOT_DATABASE_SENTINEL


@pytest.mark.asyncio
async def test_resolve_data_source_id_skips_data_source_step_for_page_400():
    """When databases 400 'is a page', the data_sources lookup is skipped entirely."""
    from app.integrations.providers.notion._common import NotionContext

    ctx = NotionContext("fake-token", None)
    calls: list[str] = []

    async def fake_safe_request(
        _client: Any,
        method: str,
        url: str,
        _provider: str,
        _action: str,
        **kwargs: Any,
    ) -> tuple[httpx.Response | None, str | None]:
        calls.append(url)
        if method == "get" and "/databases/" in url:
            return _page_not_db_400(_PAGE_ID)
        raise AssertionError(f"Unexpected: {method} {url}")

    with patch(
        "app.integrations.providers.notion._common.safe_request",
        new=fake_safe_request,
    ):
        async with ctx.client() as client:
            result = await ctx.resolve_data_source_id(client, _PAGE_ID)

    assert result == _PAGE_NOT_DATABASE_SENTINEL
    # Only one HTTP call should have been made — no data_sources probe
    data_source_calls = [u for u in calls if "/data_sources/" in u]
    assert not data_source_calls, "data_sources probe should be skipped for page IDs"


@pytest.mark.asyncio
async def test_resolve_data_source_id_non_page_400_falls_through():
    """A generic 400 that is NOT 'is a page' should fall through to step 2 normally."""
    from app.integrations.providers.notion._common import NotionContext

    ctx = NotionContext("fake-token", None)

    async def fake_safe_request(
        _client: Any,
        method: str,
        url: str,
        _provider: str,
        _action: str,
        **kwargs: Any,
    ) -> tuple[httpx.Response | None, str | None]:
        if method == "get" and "/databases/" in url:
            resp = httpx.Response(
                400,
                json={
                    "object": "error",
                    "status": 400,
                    "message": "Could not find database.",
                },
            )
            return resp, "Error: Notion rejected the request — bad request. Could not find database."
        if method == "get" and f"/data_sources/{_PAGE_ID}" in url:
            # Simulate the input being a valid data_source_id (step 2 succeeds)
            return _ok({"id": _PAGE_ID, "object": "data_source"})
        raise AssertionError(f"Unexpected: {method} {url}")

    with patch(
        "app.integrations.providers.notion._common.safe_request",
        new=fake_safe_request,
    ):
        async with ctx.client() as client:
            result = await ctx.resolve_data_source_id(client, _PAGE_ID)

    # Non-page 400 should fall through → step 2 returns the original ID
    assert result == _PAGE_ID
    assert result != _PAGE_NOT_DATABASE_SENTINEL


# ── normalize_relation_properties: page target never serialized ──────────────


@pytest.mark.asyncio
async def test_normalize_relation_properties_returns_error_for_page_target():
    """A relation column targeting a page returns an actionable error — the
    sentinel must never be written into the request payload."""
    from app.integrations.providers.notion._common import NotionContext

    ctx = NotionContext("fake-token", None)
    props = {"Related": {"relation": {"database_id": _PAGE_ID}}}

    async def fake_safe_request(
        _client: Any,
        method: str,
        url: str,
        _provider: str,
        _action: str,
        **kwargs: Any,
    ) -> tuple[httpx.Response | None, str | None]:
        if method == "get" and f"/databases/{_PAGE_ID}" in url:
            return _page_not_db_400(_PAGE_ID)
        raise AssertionError(f"Unexpected: {method} {url}")

    with patch(
        "app.integrations.providers.notion._common.safe_request",
        new=fake_safe_request,
    ):
        async with ctx.client() as client:
            err = await ctx.normalize_relation_properties(client, props)

    assert err is not None
    assert "is a Notion page, not a database" in err
    assert _PAGE_ID in err
    # The sentinel must not have leaked into the (would-be) request payload.
    assert _PAGE_NOT_DATABASE_SENTINEL not in json.dumps(props)


@pytest.mark.asyncio
async def test_update_notion_database_relation_targeting_page_returns_error():
    """update_notion_database rejects a relation column whose target is a page
    before any mutation request is sent."""
    handler = _build_handler("update_notion_database")
    calls: list[tuple[str, str]] = []

    async def fake_safe_request(
        _client: Any,
        method: str,
        url: str,
        _provider: str,
        _action: str,
        **kwargs: Any,
    ) -> tuple[httpx.Response | None, str | None]:
        calls.append((method, url))
        if method == "get" and f"/databases/{_PAGE_ID}" in url:
            return _page_not_db_400(_PAGE_ID)
        raise AssertionError(f"Unexpected: {method} {url}")

    with patch(
        "app.integrations.providers.notion._common.safe_request",
        new=fake_safe_request,
    ):
        result = await handler(
            database_id=_DB_ID,
            properties_json=json.dumps(
                {"Related": {"relation": {"database_id": _PAGE_ID}}}
            ),
        )

    assert "is a Notion page, not a database" in result
    assert _PAGE_ID in result
    # No PATCH mutation may have been attempted.
    assert not any(m == "patch" for m, _ in calls), calls


# ── query_notion_database: actionable error on page ID ───────────────────────


@pytest.mark.asyncio
async def test_query_notion_database_returns_actionable_error_for_page_id():
    """query_notion_database returns 'use get_notion_page' when given a page ID."""
    handler = _build_handler("query_notion_database")

    async def fake_safe_request(
        _client: Any,
        method: str,
        url: str,
        _provider: str,
        _action: str,
        **kwargs: Any,
    ) -> tuple[httpx.Response | None, str | None]:
        if method == "get" and f"/databases/{_PAGE_ID}" in url:
            return _page_not_db_400(_PAGE_ID)
        raise AssertionError(f"Unexpected: {method} {url}")

    with patch(
        "app.integrations.providers.notion._common.safe_request",
        new=fake_safe_request,
    ):
        result = await handler(database_id=_PAGE_ID)

    assert "is a Notion page, not a database" in result
    assert "get_notion_page" in result
    assert _PAGE_ID in result


@pytest.mark.asyncio
async def test_query_notion_database_no_data_source_probe_for_page_id():
    """query_notion_database does not probe /data_sources/ when the ID is a page."""
    handler = _build_handler("query_notion_database")
    calls: list[str] = []

    async def fake_safe_request(
        _client: Any,
        method: str,
        url: str,
        _provider: str,
        _action: str,
        **kwargs: Any,
    ) -> tuple[httpx.Response | None, str | None]:
        calls.append(url)
        if method == "get" and f"/databases/{_PAGE_ID}" in url:
            return _page_not_db_400(_PAGE_ID)
        raise AssertionError(f"Unexpected: {method} {url}")

    with patch(
        "app.integrations.providers.notion._common.safe_request",
        new=fake_safe_request,
    ):
        result = await handler(database_id=_PAGE_ID)

    assert "get_notion_page" in result
    # Must not try /data_sources/ or /query at all
    assert not any("/data_sources/" in u for u in calls), (
        "Unexpected data_sources probe: " + str(calls)
    )


@pytest.mark.asyncio
async def test_query_notion_database_still_works_for_real_database():
    """query_notion_database works normally for a real database ID (regression guard)."""
    handler = _build_handler("query_notion_database")

    async def fake_safe_request(
        _client: Any,
        method: str,
        url: str,
        _provider: str,
        _action: str,
        **kwargs: Any,
    ) -> tuple[httpx.Response | None, str | None]:
        if method == "get" and f"/databases/{_DB_ID}" in url:
            return _ok({"data_sources": [{"id": _DS_ID}]})
        if method == "post" and f"/data_sources/{_DS_ID}/query" in url:
            return _ok({
                "results": [
                    {"id": "page-1", "object": "page", "url": "https://notion.so/p1",
                     "properties": {"Name": {"type": "title", "title": [{"plain_text": "Task 1"}]}}},
                ],
                "has_more": False,
            })
        if method == "get" and "/pages/page-1" in url:
            # Page metadata fetch for auto-expansion
            return _ok({"id": "page-1", "object": "page", "url": "https://notion.so/p1",
                        "properties": {"Name": {"type": "title", "title": [{"plain_text": "Task 1"}]}}})
        if method == "get" and "/blocks/page-1" in url:
            return _ok({"results": [], "has_more": False})
        raise AssertionError(f"Unexpected: {method} {url}")
    with patch(
        "app.integrations.providers.notion._common.safe_request",
        new=fake_safe_request,
    ):
        result = await handler(database_id=_DB_ID)

    # Should return database results, not a page error
    assert "get_notion_page" not in result
    assert "is a Notion page" not in result


# ── get_notion_database: actionable error on page ID ─────────────────────────


@pytest.mark.asyncio
async def test_get_notion_database_returns_actionable_error_for_page_id():
    """get_notion_database returns 'use get_notion_page' when given a page ID."""
    handler = _build_handler("get_notion_database")

    async def fake_safe_request(
        _client: Any,
        method: str,
        url: str,
        _provider: str,
        _action: str,
        **kwargs: Any,
    ) -> tuple[httpx.Response | None, str | None]:
        if method == "get" and f"/data_sources/{_PAGE_ID}" in url:
            # Step 1: try as data_source → 404
            return _not_found_404(_PAGE_ID)
        if method == "get" and f"/databases/{_PAGE_ID}" in url:
            # Step 2: try as database → 400 "is a page"
            return _page_not_db_400(_PAGE_ID)
        raise AssertionError(f"Unexpected: {method} {url}")

    with patch(
        "app.integrations.providers.notion._common.safe_request",
        new=fake_safe_request,
    ):
        result = await handler(database_id=_PAGE_ID)

    assert "is a Notion page, not a database" in result
    assert "get_notion_page" in result
    assert _PAGE_ID in result


@pytest.mark.asyncio
async def test_get_notion_database_non_page_400_returns_original_error():
    """A generic 400 (not 'is a page') from get_notion_database propagates normally."""
    handler = _build_handler("get_notion_database")

    async def fake_safe_request(
        _client: Any,
        method: str,
        url: str,
        _provider: str,
        _action: str,
        **kwargs: Any,
    ) -> tuple[httpx.Response | None, str | None]:
        if method == "get" and f"/data_sources/{_DB_ID}" in url:
            return _not_found_404(_DB_ID)
        if method == "get" and f"/databases/{_DB_ID}" in url:
            resp = httpx.Response(
                400,
                json={"object": "error", "status": 400, "message": "Some other error."},
            )
            return resp, "Error: Notion rejected the request to retrieve database — bad request. Some other error."
        raise AssertionError(f"Unexpected: {method} {url}")

    with patch(
        "app.integrations.providers.notion._common.safe_request",
        new=fake_safe_request,
    ):
        result = await handler(database_id=_DB_ID)

    # Non-page 400 → original error, not the page-specific message
    assert "is a Notion page, not a database" not in result
    assert "get_notion_page" not in result
    assert "bad request" in result.lower()


# ── list_notion_templates: actionable error on page ID ───────────────────────


@pytest.mark.asyncio
async def test_list_notion_templates_returns_actionable_error_for_page_id():
    """list_notion_templates returns an error when given a page ID."""
    handler = _build_handler("list_notion_templates")

    async def fake_safe_request(
        _client: Any,
        method: str,
        url: str,
        _provider: str,
        _action: str,
        **kwargs: Any,
    ) -> tuple[httpx.Response | None, str | None]:
        if method == "get" and f"/databases/{_PAGE_ID}" in url:
            return _page_not_db_400(_PAGE_ID)
        raise AssertionError(f"Unexpected: {method} {url}")

    with patch(
        "app.integrations.providers.notion._common.safe_request",
        new=fake_safe_request,
    ):
        result = await handler(database_id=_PAGE_ID)

    assert "is a Notion page, not a database" in result
    assert _PAGE_ID in result


# ── update_notion_database: actionable error on page ID ──────────────────────


@pytest.mark.asyncio
async def test_update_notion_database_returns_actionable_error_for_page_id():
    """update_notion_database returns 'use update_notion_page' when given a page ID."""
    handler = _build_handler("update_notion_database")

    async def fake_safe_request(
        _client: Any,
        method: str,
        url: str,
        _provider: str,
        _action: str,
        **kwargs: Any,
    ) -> tuple[httpx.Response | None, str | None]:
        if method == "get" and f"/databases/{_PAGE_ID}" in url:
            return _page_not_db_400(_PAGE_ID)
        raise AssertionError(f"Unexpected: {method} {url}")

    with patch(
        "app.integrations.providers.notion._common.safe_request",
        new=fake_safe_request,
    ):
        result = await handler(
            database_id=_PAGE_ID,
            properties_json='{"New Column": {"rich_text": {}}}',
        )

    assert "is a Notion page, not a database" in result
    assert _PAGE_ID in result
    assert "update_notion_page" in result
