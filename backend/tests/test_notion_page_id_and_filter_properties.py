"""Tests for the Notion invalid-``filter_properties`` path.

When ``filter_properties`` contains a property name that does not exist in the
database schema, Notion returns 400 with "filter_properties contains an invalid
attribute: <name>". ``query_notion_database`` then reads the data source's
schema once, maps the names it knows to property ids, drops the entries it
does not (naming them, and the real properties, in the answer) and asks again
(the hourly "Auto-Draft on Brief Approval" run failed the same way every
time on a guessed "Name"). Only when the schema cannot be read, or Notion
rejects the resolved ids too, does the agent get the actionable error that
tells it to call ``get_notion_database`` first.

This failure mode appeared in a production incident affecting the
"[Product] Roadmap & Vision" exec-summary assignments. The companion
page-ID-as-database-ID failure from the same incident is covered by
``test_notion_page_not_database.py``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import httpx
import pytest

from app.integrations.providers.notion import (
    _is_filter_properties_invalid_400,
    build_tools,
)
from app.services.integration_tools import _SafeIntegrationExecutor

# ── Helpers ───────────────────────────────────────────────────────────────────

_FILTER_PROPS_INVALID_MSG = (
    "The schema for the data source with id ds-xyz is malformed and contains the following "
    "errors: filter_properties contains an invalid attribute: Now"
)


def _ok(payload: dict) -> tuple[httpx.Response, None]:
    return httpx.Response(200, json=payload), None


def _err_400(message: str) -> tuple[httpx.Response, str]:
    resp = httpx.Response(
        400,
        json={"object": "error", "status": 400, "code": "validation_error", "message": message},
    )
    formatted = f"Notion integration HTTP error: bad request. {message}"
    return resp, formatted


def _build_handler(tool_name: str):
    executor = _SafeIntegrationExecutor()
    build_tools("fake-token", executor)
    return executor._handlers[tool_name]


# ── Unit tests: _is_filter_properties_invalid_400 ────────────────────────────


def test_is_filter_properties_invalid_400_detects_invalid_attribute():
    resp = httpx.Response(
        400,
        json={
            "object": "error",
            "status": 400,
            "message": _FILTER_PROPS_INVALID_MSG,
        },
    )
    assert _is_filter_properties_invalid_400(resp) is True


def test_is_filter_properties_invalid_400_case_insensitive():
    resp = httpx.Response(
        400,
        json={
            "message": "FILTER_PROPERTIES contains an INVALID ATTRIBUTE: Now",
        },
    )
    assert _is_filter_properties_invalid_400(resp) is True


def test_is_filter_properties_invalid_400_ignores_non_400():
    resp = httpx.Response(
        200,
        json={"message": "filter_properties contains an invalid attribute: Now"},
    )
    assert _is_filter_properties_invalid_400(resp) is False


def test_is_filter_properties_invalid_400_ignores_other_400():
    resp = httpx.Response(
        400,
        json={"message": "Could not find property with name or id: Now"},
    )
    assert _is_filter_properties_invalid_400(resp) is False


def test_is_filter_properties_invalid_400_returns_false_for_none():
    assert _is_filter_properties_invalid_400(None) is False


def test_is_filter_properties_invalid_400_ignores_type_mismatch_400():
    """A filter type-mismatch 400 must not be mistaken for a filter_properties error."""
    resp = httpx.Response(
        400,
        json={
            "message": (
                "The property type in the database does not match the property type "
                "of the filter provided: database property status does not match filter select"
            )
        },
    )
    assert _is_filter_properties_invalid_400(resp) is False


# ── Integration tests: filter_properties invalid attribute ───────────────────


_SCHEMA = {
    "properties": {
        "Brief": {"id": "title", "type": "title"},
        "Status": {"id": "%5EOE%40", "type": "status"},
        "Owner": {"id": "BJXS", "type": "people"},
    }
}
_PAGE = {"object": "page", "id": "p1", "url": "https://notion.so/p1", "properties": {}}


@pytest.mark.asyncio
async def test_query_resolves_names_to_ids_and_names_the_unknown_ones():
    """A rejected filter_properties is resolved against the schema once: known
    names become property ids (an id already encoded is sent as is), the
    unknown entry is dropped and named with the database's real properties."""
    handler = _build_handler("query_notion_database")
    queries: list[str] = []
    schema_fetches: list[str] = []

    async def fake_safe_request(
        _client: Any,
        method: str,
        url: str,
        _provider: str,
        _action: str,
        **kwargs: Any,
    ) -> tuple[httpx.Response | None, str | None]:
        if method == "get" and "/databases/" in url:
            return _ok({"data_sources": [{"id": "ds-xyz"}]})
        if method == "post" and "/data_sources/ds-xyz/query" in url:
            queries.append(url)
            if len(queries) == 1:
                return _err_400(_FILTER_PROPS_INVALID_MSG)
            return _ok({"results": [_PAGE], "has_more": False})
        if method == "get" and url.endswith("/data_sources/ds-xyz"):
            schema_fetches.append(url)
            return _ok(_SCHEMA)
        if method == "get" and "/pages/" in url:
            return _ok(_PAGE)
        if method == "get" and "/blocks/" in url:
            return _ok({"results": [], "has_more": False})
        raise AssertionError(f"Unexpected: {method} {url}")

    with patch("app.integrations.providers.notion._common.safe_request", new=fake_safe_request):
        result = await handler(database_id="db-xyz", filter_properties="Name, status, BJXS")

    assert len(schema_fetches) == 1
    assert len(queries) == 2
    assert queries[1].endswith(
        "?filter_properties[]=%5EOE%40&filter_properties[]=BJXS"
    )
    assert result.startswith("Note: filter_properties left out Name")
    assert "its properties: Brief, Owner, Status" in result
    assert "ID: p1" in result


@pytest.mark.asyncio
async def test_query_returns_actionable_error_when_the_schema_cannot_be_read():
    """With no schema to resolve against, the agent gets the actionable error
    that tells it to call get_notion_database first."""
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
            return _ok({"data_sources": [{"id": "ds-xyz"}]})
        if method == "post" and "/data_sources/ds-xyz/query" in url:
            return _err_400(_FILTER_PROPS_INVALID_MSG)
        if method == "get" and url.endswith("/data_sources/ds-xyz"):
            return None, "Error: Notion rejected the request to retrieve the schema."
        raise AssertionError(f"Unexpected: {method} {url}")

    with patch("app.integrations.providers.notion._common.safe_request", new=fake_safe_request):
        result = await handler(
            database_id="db-xyz",
            filter_properties="Now",
        )

    # Must mention filter_properties and direct the agent to get_notion_database
    assert "filter_properties" in result
    assert "invalid" in result.lower()
    assert "get_notion_database" in result
    # Resolve + query + one schema read, never a second query.
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_query_resolves_filter_properties_only_once():
    """Notion rejecting the resolved ids too ends with the actionable error,
    after a single schema read - never a loop."""
    handler = _build_handler("query_notion_database")
    schema_fetches: list[str] = []
    queries: list[str] = []

    async def fake_safe_request(
        _client: Any,
        method: str,
        url: str,
        _provider: str,
        _action: str,
        **kwargs: Any,
    ) -> tuple[httpx.Response | None, str | None]:
        if method == "get" and "/databases/" in url:
            return _ok({"data_sources": [{"id": "ds-xyz"}]})
        if method == "post" and "/data_sources/" in url and "/query" in url:
            queries.append(url)
            return _err_400(_FILTER_PROPS_INVALID_MSG)
        if method == "get" and "/data_sources/" in url:
            schema_fetches.append(url)
            return _ok(_SCHEMA)
        raise AssertionError(f"Unexpected: {method} {url}")

    with patch("app.integrations.providers.notion._common.safe_request", new=fake_safe_request):
        result = await handler(database_id="db-xyz", filter_properties="Status")

    assert len(schema_fetches) == 1
    assert len(queries) == 2
    assert "get_notion_database" in result


@pytest.mark.asyncio
async def test_query_filter_properties_error_embeds_notion_message():
    """The actionable error must embed the raw Notion message so the agent can
    see exactly which attribute was invalid."""
    handler = _build_handler("query_notion_database")

    invalid_prop = "InvalidPropertyName"
    msg = (
        f"The schema for the data source with id ds-1 is malformed and contains the following "
        f"errors: filter_properties contains an invalid attribute: {invalid_prop}"
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
            return _ok({"data_sources": [{"id": "ds-1"}]})
        if method == "post" and "/data_sources/" in url:
            return _err_400(msg)
        if method == "get" and url.endswith("/data_sources/ds-1"):
            return None, "Error: schema unavailable."
        raise AssertionError(f"Unexpected: {method} {url}")

    with patch("app.integrations.providers.notion._common.safe_request", new=fake_safe_request):
        result = await handler(database_id="db-1", filter_properties=invalid_prop)

    assert invalid_prop in result
