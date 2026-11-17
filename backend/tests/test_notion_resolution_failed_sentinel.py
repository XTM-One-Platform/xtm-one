"""Tests for the _RESOLUTION_FAILED_SENTINEL handling in Notion tools.

Production incident (2026-07-08 through 2026-07-14):
The "[OAEV] PR Watch" agent's assignments failed daily because several Notion
``data_source`` IDs were stale (deleted / moved / no longer shared with the
integration).  Previously, ``resolve_data_source_id`` silently returned the
stale ID unchanged when both probes (databases + data_sources) failed with 404,
so the tool proceeded to POST ``/data_sources/{stale_id}/query`` — which also
404'd — and returned a raw, unhelpful error to the LLM.

The fix introduces ``_RESOLUTION_FAILED_SENTINEL``: returned by
``resolve_data_source_id`` when **both** the databases and data_sources probes
fail with 404/object_not_found.  Callers detect the sentinel and return a
clear, actionable error telling the agent to use ``search_notion`` or
``list_notion_databases`` to locate the database by name, instead of
propagating a raw 404 or silently using a stale ID in further API calls.

A second improvement adds a stale-retry path in ``query_notion_database``:
when the POST query to a *resolved* ds_id returns 404 (the data source was
rotated after initial resolution), the tool re-resolves once from the parent
database ID and retries — exactly as ``kb_sync._query_notion_database_pages``
already does.

Covered:
- ``resolve_data_source_id``: returns sentinel when both probes fail (404)
- ``resolve_data_source_id``: still returns the original ID when step-2
  (data_sources probe) succeeds (regression guard)
- ``resolve_data_source_id``: non-404 failures (auth / network / 5xx) do NOT
  produce the sentinel — the original ID is returned so the real error
  surfaces on the actual call
- ``resolve_data_source_id``: a 404 on only one probe is not enough — both
  probes must confirm not-found
- ``normalize_relation_properties``: actionable error on sentinel
  (sentinel never serialized into the request payload)
- honest-suppression wording: every sentinel-based caller error
  states the 404 may be a cached/suppressed verdict (API possibly not
  contacted) and names the connectivity test as the live escape hatch
- ``query_notion_database``: actionable error with search guidance on sentinel
- ``query_notion_database``: no query POST attempted when sentinel detected
- ``query_notion_database``: stale-retry when POST 404s and ds_id was resolved
  (asserts the re-resolution + fresh-ds retry actually happen)
- ``query_notion_database``: stale-retry reports the actionable guidance when
  the re-resolution itself fails with 404s
- ``query_notion_database``: stale-retry not attempted when ds_id == database_id
- ``list_notion_templates``: actionable error on sentinel
- ``update_notion_database``: actionable error on sentinel
"""

from __future__ import annotations

import json
import uuid
from typing import Any
from unittest.mock import patch

import httpx
import pytest

from app.integrations.providers.notion import (
    _RESOLUTION_FAILED_SENTINEL,
    build_tools,
)
from app.services.integration_tools import _SafeIntegrationExecutor

# ── Shared constants ──────────────────────────────────────────────────────────

# A stale data_source ID — 404 from both the databases and data_sources probes.
_STALE_DS_ID = "f9252699-481d-49bc-96ae-753a2cbef92a"

# A database ID (different from the ds_id) used for the stale-retry tests.
_DB_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
# A fresh data_source_id that the re-resolution step returns.
_FRESH_DS_ID = "cccccccc-cccc-cccc-cccc-cccccccccccc"


# ── Helpers ───────────────────────────────────────────────────────────────────


def _ok(payload: dict) -> tuple[httpx.Response, None]:
    return httpx.Response(200, json=payload), None


def _not_found_404(action: str = "resolve") -> tuple[None, str]:
    return (
        None,
        f"Error: Notion resource not found while trying to {action}.",
    )


def _resp_404(action: str = "resolve") -> tuple[httpx.Response, str]:
    """Return a real httpx.Response(404) so status_code checks work in tests."""
    resp = httpx.Response(
        404,
        json={
            "object": "error",
            "status": 404,
            "code": "object_not_found",
            "message": f"Could not find resource while trying to {action}.",
        },
    )
    return resp, f"Error: Notion resource not found while trying to {action}."


def _db_resp_with_ds(ds_id: str = _FRESH_DS_ID) -> tuple[httpx.Response, None]:
    """Simulate GET /databases/{id} returning a data_sources list."""
    return _ok({"id": _DB_ID, "object": "database", "data_sources": [{"id": ds_id, "name": "My DB"}]})


def _build_handler(tool_name: str):
    executor = _SafeIntegrationExecutor()
    build_tools("fake-token", executor)
    return executor._handlers[tool_name]


# ── Unit tests: _RESOLUTION_FAILED_SENTINEL value ────────────────────────────


def test_resolution_failed_sentinel_is_non_uuid_string():
    """The sentinel must be a non-empty, non-UUID string to never collide with a real ID."""
    assert isinstance(_RESOLUTION_FAILED_SENTINEL, str)
    assert _RESOLUTION_FAILED_SENTINEL
    with pytest.raises(ValueError):
        uuid.UUID(_RESOLUTION_FAILED_SENTINEL)


# ── resolve_data_source_id: sentinel returned when both probes fail ───────────


@pytest.mark.asyncio
async def test_resolve_data_source_id_returns_sentinel_when_both_probes_fail():
    """Returns _RESOLUTION_FAILED_SENTINEL when both databases and data_sources 404."""
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
        if method == "get" and f"/databases/{_STALE_DS_ID}" in url:
            return _not_found_404("resolve databases")
        if method == "get" and f"/data_sources/{_STALE_DS_ID}" in url:
            return _not_found_404("check data source")
        raise AssertionError(f"Unexpected request: {method} {url}")

    with patch(
        "app.integrations.providers.notion._common.safe_request",
        new=fake_safe_request,
    ):
        async with ctx.client() as client:
            result = await ctx.resolve_data_source_id(client, _STALE_DS_ID)

    assert result == _RESOLUTION_FAILED_SENTINEL


@pytest.mark.asyncio
async def test_resolve_data_source_id_returns_original_when_data_sources_probe_succeeds():
    """When step-2 (data_sources probe) succeeds, the ID is returned unchanged."""
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
            # Step 1 fails — no data_sources list
            return _not_found_404("resolve databases")
        if method == "get" and "/data_sources/" in url:
            # Step 2 succeeds — the input is already a valid data_source_id
            return _ok({"id": _STALE_DS_ID, "object": "data_source", "name": "DB"})
        raise AssertionError(f"Unexpected: {method} {url}")

    with patch(
        "app.integrations.providers.notion._common.safe_request",
        new=fake_safe_request,
    ):
        async with ctx.client() as client:
            result = await ctx.resolve_data_source_id(client, _STALE_DS_ID)

    # The ID is valid (step 2 confirmed it) — return as-is.
    assert result == _STALE_DS_ID
    assert result != _RESOLUTION_FAILED_SENTINEL


@pytest.mark.asyncio
async def test_resolve_data_source_id_no_sentinel_on_non_404_failures():
    """Auth / network / 5xx failures must NOT produce the sentinel.

    A transient Notion outage or a broken token would otherwise be
    misreported to the agent as "the database was deleted / unshared" —
    the original ID is returned so the caller's actual API call surfaces
    the real error.
    """
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
                503,
                json={"object": "error", "status": 503, "message": "upstream down"},
            )
            return resp, "Error: Notion service unavailable (HTTP 503)."
        if method == "get" and "/data_sources/" in url:
            # Network error — no response object at all.
            return None, "Error: network error while trying to check data source."
        raise AssertionError(f"Unexpected: {method} {url}")

    with patch(
        "app.integrations.providers.notion._common.safe_request",
        new=fake_safe_request,
    ):
        async with ctx.client() as client:
            result = await ctx.resolve_data_source_id(client, _STALE_DS_ID)

    assert result == _STALE_DS_ID
    assert result != _RESOLUTION_FAILED_SENTINEL


@pytest.mark.asyncio
async def test_resolve_data_source_id_no_sentinel_when_only_one_probe_is_404():
    """A 404 on one probe plus a non-404 failure on the other must not
    produce the sentinel — both probes must confirm not-found."""
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
            return _not_found_404("resolve databases")
        if method == "get" and "/data_sources/" in url:
            resp = httpx.Response(
                401,
                json={"object": "error", "status": 401, "code": "unauthorized"},
            )
            return resp, "Error: Notion authentication failed (HTTP 401)."
        raise AssertionError(f"Unexpected: {method} {url}")

    with patch(
        "app.integrations.providers.notion._common.safe_request",
        new=fake_safe_request,
    ):
        async with ctx.client() as client:
            result = await ctx.resolve_data_source_id(client, _STALE_DS_ID)

    assert result == _STALE_DS_ID
    assert result != _RESOLUTION_FAILED_SENTINEL


# ── normalize_relation_properties: sentinel never serialized ─────────────────


@pytest.mark.asyncio
async def test_normalize_relation_properties_returns_error_for_resolution_failed():
    """A relation column with a stale target returns an actionable error."""
    from app.integrations.providers.notion._common import NotionContext

    ctx = NotionContext("fake-token", None)
    props = {"Related": {"relation": {"database_id": _STALE_DS_ID}}}

    async def fake_safe_request(
        _client: Any,
        method: str,
        url: str,
        _provider: str,
        _action: str,
        **kwargs: Any,
    ) -> tuple[httpx.Response | None, str | None]:
        # Both probes fail → sentinel
        return _not_found_404("resolve")

    with patch(
        "app.integrations.providers.notion._common.safe_request",
        new=fake_safe_request,
    ):
        async with ctx.client() as client:
            err = await ctx.normalize_relation_properties(client, props)

    assert err is not None
    assert "unavailable" in err.lower()
    assert _STALE_DS_ID in err
    # Honest suppression: the 404s behind the sentinel may be cached/suppressed
    # verdicts, so the message must not claim a live check.
    assert "cached" in err.lower()
    # The sentinel must not have leaked into the (would-be) request payload.
    assert _RESOLUTION_FAILED_SENTINEL not in json.dumps(props)


# ── query_notion_database: actionable error on sentinel ──────────────────────


@pytest.mark.asyncio
async def test_query_notion_database_returns_actionable_error_for_stale_ds_id():
    """query_notion_database returns a search-guidance error for a stale data_source_id."""
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
            return _not_found_404("resolve databases")
        if method == "get" and "/data_sources/" in url:
            return _not_found_404("check data source")
        raise AssertionError(f"Unexpected request: {method} {url}")

    with patch(
        "app.integrations.providers.notion._common.safe_request",
        new=fake_safe_request,
    ):
        result = await handler(database_id=_STALE_DS_ID)

    assert "unavailable" in result.lower()
    assert "search_notion" in result or "list_notion_databases" in result
    assert _STALE_DS_ID in result
    # Honest suppression: the 404s behind the sentinel may have
    # been synthesized by the in-run missing cache or the cross-run stale
    # suppression WITHOUT contacting the Notion API — the message must say so
    # and name the live escape hatch (connectivity test).
    assert "cached" in result.lower()
    assert "connectivity test" in result.lower()


@pytest.mark.asyncio
async def test_query_notion_database_no_query_post_for_stale_ds_id():
    """No POST /query is attempted when resolution returns the sentinel."""
    handler = _build_handler("query_notion_database")
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
        if method == "get" and "/databases/" in url:
            return _not_found_404("resolve databases")
        if method == "get" and "/data_sources/" in url:
            return _not_found_404("check data source")
        raise AssertionError(f"Unexpected: {method} {url}")

    with patch(
        "app.integrations.providers.notion._common.safe_request",
        new=fake_safe_request,
    ):
        result = await handler(database_id=_STALE_DS_ID)

    assert "unavailable" in result.lower()
    # No POST /query must have been issued.
    post_calls = [(m, u) for m, u in calls if m == "post"]
    assert not post_calls, f"Unexpected POST calls: {post_calls}"


# ── query_notion_database: stale-retry on POST 404 ───────────────────────────


@pytest.mark.asyncio
async def test_query_notion_database_retries_once_on_stale_post_404():
    """When the POST query 404s and ds_id was resolved, re-resolves and retries once."""
    handler = _build_handler("query_notion_database")
    post_calls: list[str] = []
    db_resolutions: list[str] = []

    async def fake_safe_request(
        _client: Any,
        method: str,
        url: str,
        _provider: str,
        _action: str,
        **kwargs: Any,
    ) -> tuple[httpx.Response | None, str | None]:
        if method == "get" and f"/databases/{_DB_ID}" in url:
            db_resolutions.append(url)
            if len(db_resolutions) == 1:
                # First resolution: returns a ds_id that then becomes stale.
                return _db_resp_with_ds(_STALE_DS_ID)
            # Re-resolution after the POST 404: returns the fresh ds_id.
            return _db_resp_with_ds(_FRESH_DS_ID)
        if method == "post" and f"/data_sources/{_STALE_DS_ID}/query" in url:
            post_calls.append(url)
            # First query: stale → 404
            return _resp_404("query database")
        if method == "post" and f"/data_sources/{_FRESH_DS_ID}/query" in url:
            post_calls.append(url)
            # Retry succeeds
            return _ok({"object": "list", "results": [
                {"id": "page-id-1", "object": "page", "url": "https://notion.so/page1",
                 "properties": {"Name": {"type": "title", "title": [{"plain_text": "Test"}]}}}
            ], "has_more": False})
        if method == "get" and "/pages/page-id-1" in url:
            # Auto-expansion metadata fetch — non-database parent skips
            # the relation-resolution path.
            return _ok({"id": "page-id-1", "object": "page", "parent": {"type": "workspace"}, "properties": {}})
        if method == "get" and "/blocks/page-id-1/children" in url:
            # Auto-expansion content fetch — no blocks.
            return _ok({"object": "list", "results": [], "has_more": False})
        raise AssertionError(f"Unexpected: {method} {url}")

    with patch(
        "app.integrations.providers.notion._common.safe_request",
        new=fake_safe_request,
    ):
        result = await handler(database_id=_DB_ID)

    # The stale POST 404 must have triggered exactly one re-resolution and
    # one retry POST against the fresh ds_id, and the retry result must be
    # returned (not an error).
    assert len(db_resolutions) == 2, db_resolutions
    assert len(post_calls) == 2, post_calls
    assert _FRESH_DS_ID in post_calls[1]
    assert "unavailable" not in result.lower(), f"Unexpected error: {result!r}"
    assert "page-id-1" in result


@pytest.mark.asyncio
async def test_query_notion_database_stale_retry_reports_unavailable_when_reresolution_fails():
    """When the re-resolution after a POST 404 also fails with 404s, the
    actionable 'unavailable' guidance is returned instead of the raw 404."""
    handler = _build_handler("query_notion_database")
    db_resolutions: list[str] = []

    async def fake_safe_request(
        _client: Any,
        method: str,
        url: str,
        _provider: str,
        _action: str,
        **kwargs: Any,
    ) -> tuple[httpx.Response | None, str | None]:
        if method == "get" and f"/databases/{_DB_ID}" in url:
            db_resolutions.append(url)
            if len(db_resolutions) == 1:
                return _db_resp_with_ds(_STALE_DS_ID)
            # Re-resolution: the database itself is now gone.
            return _resp_404("resolve databases")
        if method == "get" and f"/data_sources/{_DB_ID}" in url:
            return _resp_404("check data source")
        if method == "post" and f"/data_sources/{_STALE_DS_ID}/query" in url:
            return _resp_404("query database")
        raise AssertionError(f"Unexpected: {method} {url}")

    with patch(
        "app.integrations.providers.notion._common.safe_request",
        new=fake_safe_request,
    ):
        result = await handler(database_id=_DB_ID)

    assert "unavailable" in result.lower(), result
    assert "search_notion" in result or "list_notion_databases" in result
    assert "cached" in result.lower()


@pytest.mark.asyncio
async def test_query_notion_database_stale_retry_not_attempted_when_ds_id_equals_database_id():
    """When ds_id == database_id (input was already a data_source_id), no stale retry."""
    handler = _build_handler("query_notion_database")
    post_calls: list[str] = []

    async def fake_safe_request(
        _client: Any,
        method: str,
        url: str,
        _provider: str,
        _action: str,
        **kwargs: Any,
    ) -> tuple[httpx.Response | None, str | None]:
        if method == "get" and "/databases/" in url:
            return _not_found_404("resolve databases")
        if method == "get" and "/data_sources/" in url:
            return _not_found_404("check data source")
        if method == "post" and "/query" in url:
            post_calls.append(url)
            return _resp_404("query database")
        raise AssertionError(f"Unexpected: {method} {url}")

    with patch(
        "app.integrations.providers.notion._common.safe_request",
        new=fake_safe_request,
    ):
        result = await handler(database_id=_STALE_DS_ID)

    # Should return the sentinel-based actionable error (not attempt a query POST)
    assert "unavailable" in result.lower()
    assert not post_calls, f"Unexpected POST calls: {post_calls}"


# ── list_notion_templates: actionable error on sentinel ──────────────────────


@pytest.mark.asyncio
async def test_list_notion_templates_returns_actionable_error_for_stale_ds_id():
    """list_notion_templates returns a search-guidance error for a stale data_source_id."""
    handler = _build_handler("list_notion_templates")

    async def fake_safe_request(
        _client: Any,
        method: str,
        url: str,
        _provider: str,
        _action: str,
        **kwargs: Any,
    ) -> tuple[httpx.Response | None, str | None]:
        if method == "get" and "/databases/" in url:
            return _not_found_404("resolve databases")
        if method == "get" and "/data_sources/" in url:
            return _not_found_404("check data source")
        raise AssertionError(f"Unexpected request: {method} {url}")

    with patch(
        "app.integrations.providers.notion._common.safe_request",
        new=fake_safe_request,
    ):
        result = await handler(database_id=_STALE_DS_ID)

    assert "unavailable" in result.lower()
    assert "search_notion" in result or "list_notion_databases" in result
    assert _STALE_DS_ID in result
    assert "cached" in result.lower()
    assert "connectivity test" in result.lower()


# ── update_notion_database: actionable error on sentinel ─────────────────────


@pytest.mark.asyncio
async def test_update_notion_database_returns_actionable_error_for_stale_ds_id():
    """update_notion_database returns a clear error for a stale data_source_id."""
    handler = _build_handler("update_notion_database")
    patch_calls: list[tuple[str, str]] = []

    async def fake_safe_request(
        _client: Any,
        method: str,
        url: str,
        _provider: str,
        _action: str,
        **kwargs: Any,
    ) -> tuple[httpx.Response | None, str | None]:
        patch_calls.append((method, url))
        if method == "get" and "/databases/" in url:
            return _not_found_404("resolve databases")
        if method == "get" and "/data_sources/" in url:
            return _not_found_404("check data source")
        raise AssertionError(f"Unexpected request: {method} {url}")

    with patch(
        "app.integrations.providers.notion._common.safe_request",
        new=fake_safe_request,
    ):
        result = await handler(
            database_id=_STALE_DS_ID,
            properties_json=json.dumps({"New Column": {"rich_text": {}}}),
        )

    assert "unavailable" in result.lower()
    assert _STALE_DS_ID in result
    assert "cached" in result.lower()
    assert "connectivity test" in result.lower()
    # No PATCH mutation may have been attempted.
    assert not any(m == "patch" for m, _ in patch_calls), patch_calls
