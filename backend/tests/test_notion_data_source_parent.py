"""Notion pages whose parent is a data source, and block-update conflicts.

Since Notion API 2025-09-03 a database row created through the data
sources API reports ``parent.type == "data_source_id"`` instead of
``database_id``. ``get_notion_page_properties`` used to refuse such a page
("is not inside a database"), and ``get_notion_page`` skipped its
properties, although ``update_notion_page`` wrote them and
``query_notion_database`` returned them.

Two ``update_notion_block`` calls on blocks of one page in the same round
can make Notion answer 409 ``conflict_error`` on one of them; the PATCH is
retried with backoff (``retry_statuses``) before the error is surfaced.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.integrations.http import safe_request
from app.integrations.providers.notion import build_tools
from app.integrations.providers.notion._common import (
    _BLOCK_WRITE_RETRY_STATUSES,
    _DATABASE_ROW_PARENT_TYPES,
)
from app.services.integration_tools import _SafeIntegrationExecutor

_PAGE_ID = "11111111-1111-4111-8111-111111111111"
_DS_ID = "22222222-2222-4222-8222-222222222222"
_BLOCK_ID = "33333333-3333-4333-8333-333333333333"


def _build_handler(tool_name: str):
    executor = _SafeIntegrationExecutor()
    build_tools("fake-token", executor)
    return executor._handlers[tool_name]


def _row_page(parent: dict[str, Any]) -> dict[str, Any]:
    return {
        "object": "page",
        "id": _PAGE_ID,
        "parent": parent,
        "properties": {
            "Name": {
                "id": "title",
                "type": "title",
                "title": [{"plain_text": "Weekly recap", "text": {"content": "Weekly recap"}}],
            },
            "Status": {"id": "s1", "type": "select", "select": {"name": "Done"}},
            "Due": {"id": "d1", "type": "date", "date": {"start": "2026-11-05"}},
            "Owner": {"id": "p1", "type": "people", "people": []},
            "Link": {"id": "u1", "type": "url", "url": "https://example.com/recap"},
            "Notes": {
                "id": "r1",
                "type": "rich_text",
                "rich_text": [{"plain_text": "All good", "text": {"content": "All good"}}],
            },
        },
    }


def _page_reader(page: dict[str, Any]):
    async def fake_safe_request(
        _client: Any,
        method: str,
        url: str,
        _provider: str,
        _action: str,
        **kwargs: Any,
    ) -> tuple[httpx.Response | None, str | None]:
        if method == "get" and url.endswith(f"/pages/{_PAGE_ID}"):
            return httpx.Response(200, json=page), None
        if method == "get" and f"/blocks/{_PAGE_ID}/children" in url:
            return httpx.Response(200, json={"results": [], "has_more": False}), None
        raise AssertionError(f"Unexpected: {method} {url}")

    return fake_safe_request


def test_both_database_parent_types_are_rows():
    assert {"database_id", "data_source_id"} == _DATABASE_ROW_PARENT_TYPES


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "parent",
    [
        {"type": "data_source_id", "data_source_id": _DS_ID},
        {"type": "database_id", "database_id": _DS_ID},
    ],
)
async def test_page_properties_read_for_every_database_parent(parent):
    handler = _build_handler("get_notion_page_properties")
    with patch(
        "app.integrations.providers.notion._common.safe_request",
        new=_page_reader(_row_page(parent)),
    ):
        result = await handler(page_id=_PAGE_ID)

    assert "is not inside a database" not in result
    assert "# Properties for: Weekly recap" in result
    for line in (
        "**Status** (select)",
        "**Due** (date)",
        "**Link** (url)",
        "**Notes** (rich_text)",
        "**Name** (title)",
    ):
        assert line in result
    assert "Done" in result
    assert "2026-11-05" in result
    assert "https://example.com/recap" in result
    assert "All good" in result
    # An empty people property is listed, not dropped.
    assert "**Owner** (people)" in result
    assert "Owner" in result.split("Unset properties that need attention:", 1)[1]


@pytest.mark.asyncio
async def test_page_properties_still_refuse_a_plain_page():
    handler = _build_handler("get_notion_page_properties")
    page = _row_page({"type": "page_id", "page_id": _DS_ID})
    with patch(
        "app.integrations.providers.notion._common.safe_request",
        new=_page_reader(page),
    ):
        result = await handler(page_id=_PAGE_ID)

    assert "is not inside a database (parent type: page_id)" in result


@pytest.mark.asyncio
async def test_get_page_shows_properties_of_a_data_source_row():
    handler = _build_handler("get_notion_page")
    page = _row_page({"type": "data_source_id", "data_source_id": _DS_ID})
    with patch(
        "app.integrations.providers.notion._common.safe_request",
        new=_page_reader(page),
    ):
        result = await handler(page_id=_PAGE_ID)

    text = result.content if hasattr(result, "content") else result
    assert "**Properties:**" in text
    assert "Done" in text
    assert "**Unset properties:**" in text


@pytest.mark.asyncio
async def test_block_update_patch_retries_on_conflict():
    handler = _build_handler("update_notion_block")
    seen: list[dict[str, Any]] = []

    async def fake_safe_request(
        _client: Any,
        method: str,
        url: str,
        _provider: str,
        _action: str,
        **kwargs: Any,
    ) -> tuple[httpx.Response | None, str | None]:
        if method == "patch" and url.endswith(f"/blocks/{_BLOCK_ID}"):
            seen.append(kwargs)
            return httpx.Response(200, json={"id": _BLOCK_ID}), None
        raise AssertionError(f"Unexpected: {method} {url}")

    with patch(
        "app.integrations.providers.notion._common.safe_request",
        new=fake_safe_request,
    ):
        result = await handler(block_id=_BLOCK_ID, block_json='{"archived": false}')

    assert "updated" in result
    assert len(seen) == 1
    assert seen[0]["retry_statuses"] == _BLOCK_WRITE_RETRY_STATUSES
    assert 409 in seen[0]["retry_statuses"]


def _response(status_code: int) -> httpx.Response:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.is_success = 200 <= status_code < 300
    resp.headers = httpx.Headers({})
    resp.text = ""
    resp.json = lambda: {}
    return resp


@pytest.mark.asyncio
async def test_safe_request_lands_a_block_patch_after_a_conflict():
    """The retry set in use: a 409 then a 200 lands the write."""
    responses = [_response(409), _response(200)]

    class _Client:
        async def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
            return responses.pop(0)

    with patch("app.integrations.http.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        resp, err = await safe_request(
            _Client(),
            "patch",
            f"https://api.notion.com/v1/blocks/{_BLOCK_ID}",
            "Notion",
            "update block",
            retry_statuses=_BLOCK_WRITE_RETRY_STATUSES,
        )

    assert err is None
    assert resp is not None
    assert resp.status_code == 200
    assert mock_sleep.call_count == 1
