"""Regression tests for Notion create-page properties_json compatibility."""

from typing import Any
from unittest.mock import patch

import httpx
import pytest

from app.integrations.providers.notion import build_tools
from app.services.integration_tools import _SafeIntegrationExecutor


def _response(status_code: int, payload: dict[str, Any]) -> httpx.Response:
    return httpx.Response(status_code, json=payload)


def _create_page_handler(executor: _SafeIntegrationExecutor):
    return executor._handlers["create_notion_page"]


def _update_page_handler(executor: _SafeIntegrationExecutor):
    return executor._handlers["update_notion_page"]


@pytest.mark.asyncio
async def test_create_notion_page_accepts_dict_properties_json():
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
        if method == "get" and url.endswith("/databases/db-1"):
            return _response(200, {"data_sources": [{"id": "ds-1"}]}), None
        if method == "post" and url.endswith("/pages"):
            return _response(200, {"id": "page-1", "url": "https://notion.so/page-1"}), None
        raise AssertionError(f"Unexpected request: {method} {url}")

    executor = _SafeIntegrationExecutor()
    build_tools("fake-token", executor)
    handler = _create_page_handler(executor)

    with patch("app.integrations.providers.notion._common.safe_request", new=fake_safe_request):
        result = await handler(
            parent_id="db-1",
            title="Demo page",
            properties_json={
                "Status": {"status": {"name": "In Progress"}},
            },
        )

    assert "Page created:" in result

    post_call = next(call for call in calls if call["method"] == "post")
    assert post_call["json"]["parent"] == {"type": "data_source_id", "data_source_id": "ds-1"}
    assert post_call["json"]["properties"]["title"]["title"][0]["text"]["content"] == "Demo page"
    assert post_call["json"]["properties"]["Status"] == {"status": {"name": "In Progress"}}


@pytest.mark.asyncio
async def test_create_notion_page_accepts_json_string_properties_json():
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
        if method == "get" and url.endswith("/databases/db-1"):
            return _response(200, {"data_sources": [{"id": "ds-1"}]}), None
        if method == "post" and url.endswith("/pages"):
            return _response(200, {"id": "page-1", "url": "https://notion.so/page-1"}), None
        raise AssertionError(f"Unexpected request: {method} {url}")

    executor = _SafeIntegrationExecutor()
    build_tools("fake-token", executor)
    handler = _create_page_handler(executor)

    with patch("app.integrations.providers.notion._common.safe_request", new=fake_safe_request):
        result = await handler(
            parent_id="db-1",
            title="Demo page",
            properties_json='{"Priority": {"select": {"name": "High"}}}',
        )

    assert "Page created:" in result

    post_call = next(call for call in calls if call["method"] == "post")
    assert post_call["json"]["properties"]["Priority"] == {"select": {"name": "High"}}


@pytest.mark.asyncio
async def test_create_notion_page_rejects_unsupported_properties_json_type():
    async def fake_safe_request(
        _client: Any,
        method: str,
        url: str,
        _provider: str,
        _action: str,
        **kwargs: Any,
    ) -> tuple[httpx.Response | None, str | None]:
        if method == "get" and url.endswith("/databases/db-1"):
            return _response(200, {"data_sources": [{"id": "ds-1"}]}), None
        raise AssertionError(f"Unexpected request: {method} {url}")

    executor = _SafeIntegrationExecutor()
    build_tools("fake-token", executor)
    handler = _create_page_handler(executor)

    with patch("app.integrations.providers.notion._common.safe_request", new=fake_safe_request):
        result = await handler(
            parent_id="db-1",
            title="Demo page",
            properties_json=42,
        )

    assert "must be a JSON object or JSON string" in result


@pytest.mark.asyncio
async def test_create_notion_page_rejects_non_object_properties_json_string():
    async def fake_safe_request(
        _client: Any,
        method: str,
        url: str,
        _provider: str,
        _action: str,
        **kwargs: Any,
    ) -> tuple[httpx.Response | None, str | None]:
        if method == "get" and url.endswith("/databases/db-1"):
            return _response(200, {"data_sources": [{"id": "ds-1"}]}), None
        raise AssertionError(f"Unexpected request: {method} {url}")

    executor = _SafeIntegrationExecutor()
    build_tools("fake-token", executor)
    handler = _create_page_handler(executor)

    with patch("app.integrations.providers.notion._common.safe_request", new=fake_safe_request):
        result = await handler(
            parent_id="db-1",
            title="Demo page",
            properties_json='["not", "an", "object"]',
        )

    # Harmonised wording: both ``create_notion_page`` and
    # ``update_notion_page`` now return the same
    # "properties_json must be a JSON object" prefix so a downstream
    # LLM correcting its payload on the next turn sees consistent
    # phrasing across both tools.
    assert "properties_json must be a JSON object" in result


@pytest.mark.asyncio
async def test_update_notion_page_accepts_dict_properties_json():
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
        if method == "patch" and url.endswith("/pages/page-1"):
            return _response(200, {"id": "page-1"}), None
        raise AssertionError(f"Unexpected request: {method} {url}")

    executor = _SafeIntegrationExecutor()
    build_tools("fake-token", executor)
    handler = _update_page_handler(executor)

    with patch("app.integrations.providers.notion._common.safe_request", new=fake_safe_request):
        result = await handler(
            page_id="page-1",
            properties_json={"Status": {"status": {"name": "Done"}}},
        )

    assert "updated" in result
    assert calls[0]["json"]["properties"]["Status"] == {"status": {"name": "Done"}}


@pytest.mark.asyncio
async def test_update_notion_page_rejects_unsupported_properties_json_type():
    executor = _SafeIntegrationExecutor()
    build_tools("fake-token", executor)
    handler = _update_page_handler(executor)

    result = await handler(
        page_id="page-1",
        properties_json=42,
    )

    assert "must be a JSON object or JSON string" in result
