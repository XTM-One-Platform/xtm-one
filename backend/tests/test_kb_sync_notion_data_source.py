"""Tests for Notion KB sync data_source_id resolution and database query.

Covers:
- _resolve_notion_data_source_id: two-step resolution (databases → data_sources),
  empty data_sources fallback, non-200 fallback, exception handling
- _query_notion_database_pages: successful resolution + query via data_sources
  endpoint, stale data_source_id retry (404 with resolved id, first page only),
  pagination, and a regression guard asserting the legacy
  POST /databases/{id}/query endpoint is never called
- sync_notion_pages explicit page modes: permanent page errors (400/404/410)
  and transient failures both re-raise when nothing syncs (never fall through
  to the destructive stale-delete + commit path), and mixed failures prefer
  the permanent error so the scheduler perm-fail quarantine can trigger
"""

from unittest.mock import AsyncMock

import httpx
import pytest

from app.services.kb_sync import (
    _NotionRequestController,
    _query_notion_database_pages,
    _resolve_notion_data_source_id,
    sync_notion_pages,
)

# ---------------------------------------------------------------------------
# Shared fixtures / helpers
# ---------------------------------------------------------------------------

_DB_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
_DS_ID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
_PAGE_ID_1 = "cccccccc-cccc-cccc-cccc-cccccccccccc"
_PAGE_ID_2 = "dddddddd-dddd-dddd-dddd-dddddddddddd"

_NOTION_BASE = "https://api.notion.com/v1"


class _FakeNotionClient:
    """Minimal httpx-compatible mock that pops responses from a queue."""

    def __init__(self, responses: list[httpx.Response]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, str]] = []  # (method, url)

    async def request(self, method: str, url: str, **_: object) -> httpx.Response:
        self.calls.append((method.upper(), url))
        if not self._responses:
            raise AssertionError(f"Unexpected request: {method} {url}")
        resp = self._responses.pop(0)
        # Re-bind the queued response to the actual request so a raised
        # HTTPStatusError reports the real method/URL used by the code under
        # test (queued responses are built with a placeholder GET request).
        resp.request = httpx.Request(method.upper(), url)
        return resp

    async def get(self, url: str, **_: object) -> httpx.Response:
        return await self.request("GET", url)

    async def post(self, url: str, **_: object) -> httpx.Response:
        return await self.request("POST", url)


def _make_resp(status_code: int, *, body: dict | None = None) -> httpx.Response:
    # The request is a placeholder: _FakeNotionClient.request() re-binds each
    # queued response to the actual request before returning it.
    return httpx.Response(
        status_code=status_code,
        json=body or {},
        request=httpx.Request("GET", "https://api.notion.com/v1/placeholder"),
    )


def _db_resp(data_sources: list[dict] | None = None) -> httpx.Response:
    """Simulate a successful GET /databases/{id} response."""
    payload: dict = {"object": "database", "id": _DB_ID}
    if data_sources is not None:
        payload["data_sources"] = data_sources
    return _make_resp(200, body=payload)


def _ds_query_resp(page_ids: list[str], *, has_more: bool = False, cursor: str | None = None) -> httpx.Response:
    """Simulate a successful POST /data_sources/{id}/query response."""
    results = [{"object": "page", "id": pid} for pid in page_ids]
    body: dict = {"results": results, "has_more": has_more}
    if cursor:
        body["next_cursor"] = cursor
    return _make_resp(200, body=body)


# ---------------------------------------------------------------------------
# _resolve_notion_data_source_id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_returns_data_source_id_from_databases_response():
    """Step 1 succeeds: GET /databases/{id} returns data_sources → returns first id."""
    client = _FakeNotionClient(
        [_db_resp(data_sources=[{"id": _DS_ID}])]
    )
    result = await _resolve_notion_data_source_id(client, {}, _DB_ID)
    assert result == _DS_ID
    assert len(client.calls) == 1
    assert client.calls[0] == ("GET", f"{_NOTION_BASE}/databases/{_DB_ID}")


@pytest.mark.asyncio
async def test_resolve_falls_back_to_database_id_when_data_sources_empty():
    """GET /databases/{id} returns 200 but empty data_sources; step 2 also fails → database_id."""
    client = _FakeNotionClient(
        [
            _db_resp(data_sources=[]),           # step 1: 200, no data_sources
            _make_resp(404),                     # step 2: GET /data_sources/{id} → 404
        ]
    )
    result = await _resolve_notion_data_source_id(client, {}, _DB_ID)
    assert result == _DB_ID
    assert len(client.calls) == 2
    assert client.calls[1] == ("GET", f"{_NOTION_BASE}/data_sources/{_DB_ID}")


@pytest.mark.asyncio
async def test_resolve_falls_back_to_database_id_when_data_sources_absent():
    """GET /databases/{id} returns 200 but no 'data_sources' key; step 2 fails → database_id."""
    client = _FakeNotionClient(
        [
            _db_resp(data_sources=None),         # key absent from payload
            _make_resp(403),                     # step 2: not authorised
        ]
    )
    result = await _resolve_notion_data_source_id(client, {}, _DB_ID)
    assert result == _DB_ID


@pytest.mark.asyncio
async def test_resolve_step2_recognises_already_valid_data_source_id():
    """Step 1 fails, step 2 succeeds: input is already a data_source_id → return as-is."""
    client = _FakeNotionClient(
        [
            _make_resp(400),    # step 1: GET /databases/{id} → 400
            _make_resp(200),    # step 2: GET /data_sources/{id} → 200
        ]
    )
    result = await _resolve_notion_data_source_id(client, {}, _DB_ID)
    assert result == _DB_ID
    assert len(client.calls) == 2
    assert client.calls[0] == ("GET", f"{_NOTION_BASE}/databases/{_DB_ID}")
    assert client.calls[1] == ("GET", f"{_NOTION_BASE}/data_sources/{_DB_ID}")


@pytest.mark.asyncio
async def test_resolve_returns_database_id_when_both_steps_fail():
    """Both steps fail → returns original database_id as last-resort fallback."""
    client = _FakeNotionClient(
        [
            _make_resp(400),    # step 1: GET /databases/{id} → 400
            _make_resp(404),    # step 2: GET /data_sources/{id} → 404
        ]
    )
    result = await _resolve_notion_data_source_id(client, {}, _DB_ID)
    assert result == _DB_ID


@pytest.mark.asyncio
async def test_resolve_handles_step1_exception_gracefully():
    """If step 1 raises a network exception, step 2 is still attempted."""

    class _ErrorClient(_FakeNotionClient):
        _fail_next = True

        async def get(self, url: str, **kw: object) -> httpx.Response:  # type: ignore[override]
            if self._fail_next and "databases" in url:
                self._fail_next = False
                raise httpx.ConnectError("network down")
            return await super().get(url, **kw)

    client = _ErrorClient([_make_resp(200)])  # step 2 succeeds
    result = await _resolve_notion_data_source_id(client, {}, _DB_ID)
    assert result == _DB_ID  # database_id is already valid data_source_id


@pytest.mark.asyncio
async def test_resolve_uses_request_controller_when_provided():
    """When a _NotionRequestController is passed, requests go through it."""
    controller = _NotionRequestController(requests_per_second=0, retry_attempts=1)
    client = _FakeNotionClient([_db_resp(data_sources=[{"id": _DS_ID}])])
    result = await _resolve_notion_data_source_id(
        client, {}, _DB_ID, request_controller=controller
    )
    assert result == _DS_ID


# ---------------------------------------------------------------------------
# _query_notion_database_pages
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_query_uses_data_sources_endpoint_after_resolution():
    """Resolution succeeds → query goes to /data_sources/{ds_id}/query (not /databases/)."""
    client = _FakeNotionClient(
        [
            _db_resp(data_sources=[{"id": _DS_ID}]),          # resolve step 1
            _ds_query_resp([_PAGE_ID_1, _PAGE_ID_2]),         # query
        ]
    )
    pages = await _query_notion_database_pages(client, {}, _DB_ID)
    assert pages == [_PAGE_ID_1, _PAGE_ID_2]
    urls = [url for _, url in client.calls]
    assert any(f"data_sources/{_DS_ID}/query" in url for url in urls)
    assert not any("databases" in url and "query" in url for url in urls), (
        "Legacy /databases/{id}/query must NOT be called"
    )


@pytest.mark.asyncio
async def test_query_never_calls_legacy_databases_query_on_resolution_failure():
    """When resolution fails (both steps), the legacy /databases/{id}/query is NOT called.

    Regression guard: the old fallback code issued POST /databases/{id}/query, which
    always returns 400 with Notion API 2025-09-03 and produces confusing log noise.
    """
    client = _FakeNotionClient(
        [
            _make_resp(400),   # resolve step 1
            _make_resp(404),   # resolve step 2
            _make_resp(404),   # query
        ]
    )
    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        await _query_notion_database_pages(client, {}, _DB_ID)

    # The raised error should reference data_sources, not databases/…/query
    error_url = str(exc_info.value.request.url)
    assert "data_sources" in error_url, f"Expected data_sources URL, got: {error_url}"
    assert "databases" not in error_url or "query" not in error_url, (
        f"Legacy /databases/…/query must NOT be the raised URL: {error_url}"
    )

    called_urls = [url for _, url in client.calls]
    assert not any("databases" in u and "query" in u for u in called_urls), (
        "POST /databases/{id}/query (legacy) must never be called"
    )


@pytest.mark.asyncio
async def test_query_retries_once_on_stale_data_source_id_404():
    """When a resolved data_source_id returns 404, re-resolve and retry exactly once."""
    new_ds_id = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"

    client = _FakeNotionClient(
        [
            _db_resp(data_sources=[{"id": _DS_ID}]),           # initial resolution
            _make_resp(404),                                   # stale 404
            _db_resp(data_sources=[{"id": new_ds_id}]),       # re-resolution
            _ds_query_resp([_PAGE_ID_1]),                      # retry succeeds
        ]
    )
    pages = await _query_notion_database_pages(client, {}, _DB_ID)
    assert pages == [_PAGE_ID_1]
    # The second query must use the fresh data_source_id
    post_calls = [(m, u) for m, u in client.calls if m == "POST"]
    assert len(post_calls) == 2
    assert f"data_sources/{_DS_ID}/query" in post_calls[0][1]
    assert f"data_sources/{new_ds_id}/query" in post_calls[1][1]


@pytest.mark.asyncio
async def test_query_does_not_retry_stale_more_than_once():
    """Stale retry is attempted at most once; a second 404 propagates as an error."""
    client = _FakeNotionClient(
        [
            _db_resp(data_sources=[{"id": _DS_ID}]),   # initial resolution
            _make_resp(404),                           # stale 404 → triggers retry
            _db_resp(data_sources=[{"id": _DS_ID}]),   # re-resolution (same id)
            _make_resp(404),                           # still 404 → should raise
        ]
    )
    with pytest.raises(httpx.HTTPStatusError):
        await _query_notion_database_pages(client, {}, _DB_ID)

    post_calls = [u for m, u in client.calls if m == "POST"]
    assert len(post_calls) == 2, "Exactly two POST calls (initial + one retry)"


@pytest.mark.asyncio
async def test_query_stale_retry_not_triggered_mid_pagination():
    """A 404 on a later page (start_cursor set) propagates without re-resolution.

    Pagination cursors are opaque and bound to the data source that issued
    them, so replaying one against a freshly-resolved data_source_id would be
    invalid — the stale-id retry must only apply to the first page.
    """
    client = _FakeNotionClient(
        [
            _db_resp(data_sources=[{"id": _DS_ID}]),                       # resolution
            _ds_query_resp([_PAGE_ID_1], has_more=True, cursor="tok"),     # page 1 OK
            _make_resp(404),                                               # page 2: 404
        ]
    )
    with pytest.raises(httpx.HTTPStatusError):
        await _query_notion_database_pages(client, {}, _DB_ID)

    # No re-resolution and no retry: 1 GET (resolve) + 2 POSTs (page 1 + page 2)
    assert len(client.calls) == 3
    post_calls = [u for m, u in client.calls if m == "POST"]
    assert len(post_calls) == 2


@pytest.mark.asyncio
async def test_query_stale_retry_not_triggered_when_ds_id_equals_database_id():
    """The stale-retry path is NOT triggered when resolution failed (ds_id == database_id).

    In that case resolution could not find a distinct data_source_id, so a 404
    from /data_sources/{database_id}/query is not a 'stale mapped id' situation.
    """
    client = _FakeNotionClient(
        [
            _make_resp(400),   # resolve step 1 fails
            _make_resp(404),   # resolve step 2 fails  → ds_id falls back to database_id
            _make_resp(404),   # query
        ]
    )
    with pytest.raises(httpx.HTTPStatusError):
        await _query_notion_database_pages(client, {}, _DB_ID)

    # Only 3 calls total; no extra re-resolution call
    assert len(client.calls) == 3


@pytest.mark.asyncio
async def test_query_handles_pagination_correctly():
    """Paginated results across multiple pages are collected and returned."""
    _cursor = "cursor-token"
    client = _FakeNotionClient(
        [
            _db_resp(data_sources=[{"id": _DS_ID}]),                           # resolve
            _ds_query_resp([_PAGE_ID_1], has_more=True, cursor=_cursor),       # page 1
            _ds_query_resp([_PAGE_ID_2], has_more=False),                      # page 2
        ]
    )
    pages = await _query_notion_database_pages(client, {}, _DB_ID)
    assert pages == [_PAGE_ID_1, _PAGE_ID_2]


@pytest.mark.asyncio
async def test_query_skips_non_page_results():
    """Only objects with 'object' == 'page' are included in the returned ids."""
    client = _FakeNotionClient(
        [
            _db_resp(data_sources=[{"id": _DS_ID}]),
            _make_resp(
                200,
                body={
                    "results": [
                        {"object": "page", "id": _PAGE_ID_1},
                        {"object": "database", "id": _DB_ID},  # should be skipped
                    ],
                    "has_more": False,
                },
            ),
        ]
    )
    pages = await _query_notion_database_pages(client, {}, _DB_ID)
    assert pages == [_PAGE_ID_1]


@pytest.mark.asyncio
async def test_query_uses_request_controller_for_all_calls():
    """When a _NotionRequestController is supplied, every HTTP call goes through it."""
    controller = _NotionRequestController(requests_per_second=0, retry_attempts=1)
    client = _FakeNotionClient(
        [
            _db_resp(data_sources=[{"id": _DS_ID}]),
            _ds_query_resp([_PAGE_ID_1]),
        ]
    )
    pages = await _query_notion_database_pages(
        client, {}, _DB_ID, request_controller=controller
    )
    assert pages == [_PAGE_ID_1]
    # All requests went through the fake client (which the controller delegates to)
    assert len(client.calls) == 2


class _FakeNestedTxn:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeDB:
    def __init__(self) -> None:
        self.committed = False

    def begin_nested(self):
        return _FakeNestedTxn()

    async def commit(self) -> None:
        self.committed = True


class _FailingNotionPageClient:
    """Per-page failure map: page id -> HTTP status int or exception to raise.

    Pages absent from the map fail the test (unexpected request).
    """

    failures: dict[str, int | Exception] = {}

    def __init__(self, *_args, **_kwargs) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def request(self, method: str, url: str, **_: object) -> httpx.Response:
        req = httpx.Request(method.upper(), url)
        if "/v1/pages/" in url:
            page_id = url.rsplit("/", 1)[-1]
            failure = self.failures.get(page_id)
            if isinstance(failure, Exception):
                raise failure
            if isinstance(failure, int):
                return httpx.Response(failure, request=req)
        raise AssertionError(f"Unexpected request during test: {method} {url}")


_STALE_PAGE_ID = "3178fce1-7f2a-80ab-84c4-edf29453b982"
_STALE_PAGE_ID_2 = "2d68fce1-7f2a-8198-ac5a-dd44f2f6c804"


def _patch_sync_helpers(monkeypatch) -> None:
    from app.services import kb_sync

    monkeypatch.setattr(kb_sync.httpx, "AsyncClient", _FailingNotionPageClient)
    monkeypatch.setattr(kb_sync, "_get_integration_token", AsyncMock(return_value="token"))
    monkeypatch.setattr(kb_sync, "_delete_stale_documents", AsyncMock(return_value=0))
    monkeypatch.setattr(kb_sync, "_recount_kb_stats", AsyncMock())
    monkeypatch.setattr(kb_sync, "enqueue_documents", AsyncMock())


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [400, 404, 410])
async def test_sync_notion_pages_raises_on_explicit_stale_page(monkeypatch, status_code):
    """Explicit page sources must propagate permanent page errors to scheduler retry logic."""
    from app.services import kb_sync

    _patch_sync_helpers(monkeypatch)
    monkeypatch.setattr(
        _FailingNotionPageClient, "failures", {_STALE_PAGE_ID: status_code}
    )

    db = _FakeDB()
    kb = type("KB", (), {"name": "Talent Development Knowledge Base"})()

    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        await sync_notion_pages(
            db,
            kb,
            user_id="user-1",
            page_id=_STALE_PAGE_ID,
        )

    assert exc_info.value.response.status_code == status_code
    assert db.committed is False
    kb_sync._delete_stale_documents.assert_not_awaited()


@pytest.mark.asyncio
async def test_sync_notion_pages_raises_on_explicit_transient_failure(monkeypatch):
    """A transient failure on ALL explicit pages must also fail the sync.

    Falling through to the stale-delete + commit path with an empty doc set
    would wipe every previously synced Notion document during a provider
    outage and record the run as a success. The transient error is re-raised
    (unchanged) so the scheduler retries with backoff instead of quarantining.
    """
    from app.services import kb_sync

    _patch_sync_helpers(monkeypatch)
    monkeypatch.setattr(
        _FailingNotionPageClient,
        "failures",
        {_STALE_PAGE_ID: httpx.ConnectError("connection reset")},
    )

    db = _FakeDB()
    kb = type("KB", (), {"name": "Talent Development Knowledge Base"})()

    with pytest.raises(httpx.ConnectError):
        await sync_notion_pages(
            db,
            kb,
            user_id="user-1",
            page_id=_STALE_PAGE_ID,
        )

    assert db.committed is False
    kb_sync._delete_stale_documents.assert_not_awaited()


@pytest.mark.asyncio
async def test_sync_notion_pages_prefers_permanent_error_on_mixed_failures(monkeypatch):
    """When explicit pages fail with a mix of transient and permanent errors
    and nothing syncs, the permanent error wins so the scheduler's perm-fail
    quarantine budget accumulates instead of endlessly retrying."""
    from app.services import kb_sync

    _patch_sync_helpers(monkeypatch)
    monkeypatch.setattr(
        _FailingNotionPageClient,
        "failures",
        {
            _STALE_PAGE_ID: httpx.ConnectError("connection reset"),
            _STALE_PAGE_ID_2: 400,
        },
    )

    db = _FakeDB()
    kb = type("KB", (), {"name": "Talent Development Knowledge Base"})()

    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        await sync_notion_pages(
            db,
            kb,
            user_id="user-1",
            page_ids=[_STALE_PAGE_ID, _STALE_PAGE_ID_2],
        )

    assert exc_info.value.response.status_code == 400
    assert db.committed is False
    kb_sync._delete_stale_documents.assert_not_awaited()
