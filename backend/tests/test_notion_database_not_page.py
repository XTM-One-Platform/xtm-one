"""Defensive handling when a Notion database ID is passed as a page ID.

The mirror of ``test_notion_page_not_database.py``.

Production log triage (2026-07-18): a general-chat agent called
``get_notion_page`` with a *database* ID. Notion answered 400 "Provided ID
... is a database, not a page. Use the retrieve database API instead."; the
raw 400 was returned to the LLM with no tool redirect, producing a cascade of
follow-up misses (``/v1/data_sources/{id}`` 404) and three WARNING log lines
for one benign mistake.

The fix mirrors the page-not-database handling:
- ``get_notion_page`` / ``get_notion_page_properties`` detect the 400 via
  ``_is_database_not_page_400`` and immediately return an actionable
  "use get_notion_database / query_notion_database" error;
- the benign 400 stays out of the warning stream (``expected_statuses={400}``),
  while any OTHER 400 is still logged;
- ``get_notion_database``'s step-1 ``/data_sources/{id}`` probe marks its
  routine 404 miss as expected so it stops warning on every call, and so does
  ``resolve_data_source_id``'s ``/databases/{id}`` probe when handed a
  data_source_id.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import httpx
import pytest

from app.integrations.providers.notion import build_tools
from app.integrations.providers.notion._common import _is_database_not_page_400
from app.services.integration_tools import _SafeIntegrationExecutor

_DB_ID = "063ae1b1-b7e3-4471-98ea-f6c604ff66cf"
_DS_ID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


def _db_not_page_msg(db_id: str) -> str:
    """Notion's exact error message when a database ID is used as a page ID."""
    return (
        f"Provided ID {db_id} is a database, not a page. "
        "Use the retrieve database API instead."
    )


def _db_not_page_400(db_id: str = _DB_ID) -> tuple[httpx.Response, str]:
    """Simulate Notion's 400 'is a database, not a page' response."""
    msg = _db_not_page_msg(db_id)
    resp = httpx.Response(
        400,
        json={
            "object": "error",
            "status": 400,
            "code": "validation_error",
            "message": msg,
        },
    )
    formatted = f"Error: Notion rejected the request to read page — bad request. {msg}"
    return resp, formatted


def _build_handler(tool_name: str):
    executor = _SafeIntegrationExecutor()
    build_tools("fake-token", executor)
    return executor._handlers[tool_name]


# ── _is_database_not_page_400 ────────────────────────────────────────────────


def test_detector_matches_notion_message():
    resp, _ = _db_not_page_400()
    assert _is_database_not_page_400(resp)


def test_detector_rejects_other_400():
    resp = httpx.Response(
        400, json={"object": "error", "status": 400, "message": "Some other error."}
    )
    assert not _is_database_not_page_400(resp)


def test_detector_rejects_non_400_and_none():
    resp = httpx.Response(
        404,
        json={"object": "error", "status": 404, "message": "is a database, not a page"},
    )
    assert not _is_database_not_page_400(resp)
    assert not _is_database_not_page_400(None)


# ── get_notion_page: actionable error on database ID ─────────────────────────


@pytest.mark.asyncio
async def test_get_notion_page_returns_actionable_error_for_database_id():
    handler = _build_handler("get_notion_page")

    async def fake_safe_request(
        _client: Any,
        method: str,
        url: str,
        _provider: str,
        _action: str,
        **kwargs: Any,
    ) -> tuple[httpx.Response | None, str | None]:
        if method == "get" and f"/pages/{_DB_ID}" in url:
            return _db_not_page_400(_DB_ID)
        raise AssertionError(f"Unexpected: {method} {url}")

    with patch(
        "app.integrations.providers.notion._common.safe_request",
        new=fake_safe_request,
    ):
        result = await handler(page_id=_DB_ID)

    assert "is a Notion database, not a page" in result
    assert "get_notion_database" in result
    assert "query_notion_database" in result
    assert _DB_ID in result


@pytest.mark.asyncio
async def test_get_notion_page_no_followup_calls_for_database_id():
    """The redirect short-circuits — no block fetch, no other probes."""
    handler = _build_handler("get_notion_page")
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
        if method == "get" and f"/pages/{_DB_ID}" in url:
            return _db_not_page_400(_DB_ID)
        raise AssertionError(f"Unexpected: {method} {url}")

    with patch(
        "app.integrations.providers.notion._common.safe_request",
        new=fake_safe_request,
    ):
        result = await handler(page_id=_DB_ID)

    assert "get_notion_database" in result
    assert len(calls) == 1, calls


@pytest.mark.asyncio
async def test_get_notion_page_other_400_returns_original_error():
    handler = _build_handler("get_notion_page")

    async def fake_safe_request(
        _client: Any,
        method: str,
        url: str,
        _provider: str,
        _action: str,
        **kwargs: Any,
    ) -> tuple[httpx.Response | None, str | None]:
        if method == "get" and f"/pages/{_DB_ID}" in url:
            resp = httpx.Response(
                400,
                json={"object": "error", "status": 400, "message": "Invalid UUID."},
            )
            return resp, (
                "Error: Notion rejected the request to read page — "
                "bad request. Invalid UUID."
            )
        raise AssertionError(f"Unexpected: {method} {url}")

    with patch(
        "app.integrations.providers.notion._common.safe_request",
        new=fake_safe_request,
    ):
        result = await handler(page_id=_DB_ID)

    assert "is a Notion database, not a page" not in result
    assert "Invalid UUID" in result


# ── get_notion_page_properties: actionable error on database ID ──────────────


@pytest.mark.asyncio
async def test_get_notion_page_properties_returns_actionable_error_for_database_id():
    handler = _build_handler("get_notion_page_properties")

    async def fake_safe_request(
        _client: Any,
        method: str,
        url: str,
        _provider: str,
        _action: str,
        **kwargs: Any,
    ) -> tuple[httpx.Response | None, str | None]:
        if method == "get" and f"/pages/{_DB_ID}" in url:
            return _db_not_page_400(_DB_ID)
        raise AssertionError(f"Unexpected: {method} {url}")

    with patch(
        "app.integrations.providers.notion._common.safe_request",
        new=fake_safe_request,
    ):
        result = await handler(page_id=_DB_ID)

    assert "is a Notion database, not a page" in result
    assert "get_notion_page_properties" in result
    assert _DB_ID in result


# ── get_notion_database: routine data_sources probe miss is expected ─────────


@pytest.mark.asyncio
async def test_get_notion_database_marks_data_source_probe_404_expected():
    """The step-1 /data_sources/{id} probe passes expected_statuses={404} so
    the routine miss (the input is a real database_id, the common case) stays
    out of the warning stream."""
    handler = _build_handler("get_notion_database")
    probe_kwargs: dict[str, Any] = {}

    async def fake_safe_request(
        _client: Any,
        method: str,
        url: str,
        _provider: str,
        _action: str,
        **kwargs: Any,
    ) -> tuple[httpx.Response | None, str | None]:
        if method == "get" and f"/data_sources/{_DB_ID}" in url:
            probe_kwargs.update(kwargs)
            return None, "Error: Notion resource not found while trying to retrieve data source."
        if method == "get" and f"/databases/{_DB_ID}" in url:
            return httpx.Response(200, json={"data_sources": [{"id": _DS_ID}]}), None
        if method == "get" and f"/data_sources/{_DS_ID}" in url:
            return (
                httpx.Response(
                    200,
                    json={
                        "id": _DS_ID,
                        "object": "data_source",
                        "title": [{"plain_text": "My DB"}],
                        "properties": {"Name": {"id": "title", "type": "title"}},
                    },
                ),
                None,
            )
        raise AssertionError(f"Unexpected: {method} {url}")

    with patch(
        "app.integrations.providers.notion._common.safe_request",
        new=fake_safe_request,
    ):
        result = await handler(database_id=_DB_ID)

    assert probe_kwargs.get("expected_statuses") == {404}
    assert "Error:" not in result


# ── resolve_data_source_id: routine databases probe miss is expected ─────────


@pytest.mark.asyncio
async def test_resolve_data_source_id_marks_database_probe_404_expected():
    """The mirror of the probe above: handed a data_source_id, the
    step-1 /databases/{id} probe misses with a 404 that step 2 resolves. An
    hourly sync logged ``Could not find database`` on every run for it."""
    from app.integrations.providers.notion._common import NotionContext

    ctx = NotionContext("fake-token", None)
    probe_kwargs: dict[str, Any] = {}

    async def fake_safe_request(
        _client: Any,
        method: str,
        url: str,
        _provider: str,
        _action: str,
        **kwargs: Any,
    ) -> tuple[httpx.Response | None, str | None]:
        if method == "get" and f"/databases/{_DS_ID}" in url:
            probe_kwargs.update(kwargs)
            return (
                httpx.Response(
                    404,
                    json={
                        "object": "error",
                        "status": 404,
                        "code": "object_not_found",
                        "message": f"Could not find database with ID: {_DS_ID}.",
                    },
                ),
                "Error: Notion resource not found while trying to resolve data source.",
            )
        if method == "get" and f"/data_sources/{_DS_ID}" in url:
            return httpx.Response(200, json={"id": _DS_ID, "object": "data_source"}), None
        raise AssertionError(f"Unexpected: {method} {url}")

    with patch(
        "app.integrations.providers.notion._common.safe_request",
        new=fake_safe_request,
    ):
        async with ctx.client() as client:
            resolved = await ctx.resolve_data_source_id(client, _DS_ID)

    assert resolved == _DS_ID
    assert probe_kwargs.get("expected_statuses") == {400, 404}


class _NotionClient:
    """The HTTP client ``safe_request`` calls, answering each Notion path with
    a 200 when it is in *found* and Notion's 404 otherwise."""

    def __init__(self, found: frozenset[str] = frozenset()):
        self._found = found
        self.paths: list[str] = []

    async def request(self, method: str, url: str, **_kwargs: Any) -> httpx.Response:
        path = url.split("/v1", 1)[1]
        self.paths.append(path)
        request = httpx.Request(method, url)
        if path in self._found:
            return httpx.Response(200, json={"id": _DS_ID, "object": "data_source"}, request=request)
        return httpx.Response(
            404,
            json={
                "object": "error",
                "status": 404,
                "code": "object_not_found",
                "message": f"Could not find database with ID: {_DS_ID}.",
            },
            request=request,
        )


def _counting_context():
    """A context of an integration, its cross-run stale counter recorded."""
    from unittest.mock import AsyncMock

    from app.integrations.providers.notion._common import NotionContext

    ctx = NotionContext("fake-token", None, integration_id="integration-1")
    ctx._is_cross_run_stale = AsyncMock(return_value=False)
    ctx._incr_stale_counter = AsyncMock(return_value=1)
    ctx._clear_stale_ref = AsyncMock()
    return ctx


@pytest.mark.asyncio
async def test_a_data_source_id_resolves_without_a_warning_or_a_stale_count(caplog):
    """The symptom, through the real ``safe_request``: an hourly sync
    handed a data_source_id logged ``Could not find database`` on every run,
    and the probe's 404 counted the healthy id as a stale reference."""
    ctx = _counting_context()
    client = _NotionClient(found=frozenset({f"/data_sources/{_DS_ID}"}))

    with caplog.at_level("WARNING"):
        resolved = await ctx.resolve_data_source_id(client, _DS_ID)

    assert resolved == _DS_ID
    assert client.paths == [f"/databases/{_DS_ID}", f"/data_sources/{_DS_ID}"]
    assert [r for r in caplog.records if r.levelname == "WARNING"] == []
    ctx._incr_stale_counter.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_id_neither_probe_finds_still_warns_and_counts_once(caplog):
    """The other half of the contract: an id that is neither a database nor a
    data source is reported, and counted once for the run."""
    from app.integrations.providers.notion._common import _RESOLUTION_FAILED_SENTINEL

    ctx = _counting_context()

    with caplog.at_level("WARNING"):
        resolved = await ctx.resolve_data_source_id(_NotionClient(), _DS_ID)

    assert resolved == _RESOLUTION_FAILED_SENTINEL
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert any("could not resolve data_source_id" in w for w in warnings)
    assert any(f"/data_sources/{_DS_ID}" in w for w in warnings), "the step-2 404 still warns"
    assert not any(f"/databases/{_DS_ID}" in w for w in warnings)
    ctx._incr_stale_counter.assert_awaited_once()
