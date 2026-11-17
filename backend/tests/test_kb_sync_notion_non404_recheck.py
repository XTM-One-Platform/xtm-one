"""Tests for the NON-404 child-database quarantine recovery probe.

The non-404 quarantine (``stale_child_databases``) used to be
permanent: once a child database reached the threshold it was never queried
again, so a repaired database could only recover via a manual force re-sync.

Now every ``_NOTION_STALE_CHILD_DB_RECHECK_EVERY``-th quarantined run probes
the database again:

- not due -> the database is skipped and the counter advances by exactly one
  (once per run, even when the same child DB appears on multiple pages)
- due -> the database is queried; success clears the tracking entry,
  a non-404 failure increments the counter (waiting another N runs),
  a 404 hands over to the counted-404 path (which has its own weekly
  recheck)
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.services.kb_sync import (
    _NOTION_STALE_CHILD_DB_RECHECK_EVERY,
    _NOTION_STALE_CHILD_DB_THRESHOLD,
)

_CHILD_DB_ID = "11111111-1111-1111-1111-111111111111"
_PARENT_PAGE_ID = "33333333-3333-3333-3333-333333333333"

# A count that makes the recheck due on this run:
# skips_since_quarantine = count - threshold == RECHECK_EVERY - 1.
_DUE_COUNT = _NOTION_STALE_CHILD_DB_THRESHOLD + _NOTION_STALE_CHILD_DB_RECHECK_EVERY - 1


def _make_resp(status: int, body: dict | None = None) -> httpx.Response:
    return httpx.Response(
        status,
        json=body or {},
        request=httpx.Request("GET", "https://api.notion.com/v1/placeholder"),
    )


def _page_resp(page_id: str = _PARENT_PAGE_ID) -> dict:
    return {
        "object": "page",
        "id": page_id,
        "properties": {},
        "url": f"https://notion.so/{page_id.replace('-', '')}",
    }


def _child_db_block(db_id: str = _CHILD_DB_ID) -> dict:
    return {"id": db_id, "type": "child_database", "child_database": {"title": "My DB"}}


def _make_notion_client_mock(*, child_db_status: int = 200) -> MagicMock:
    client = MagicMock()

    async def mock_request(method: str, url: str, **_: object) -> httpx.Response:
        if "/pages/" in url:
            return _make_resp(200, _page_resp())
        if "/databases/" in url:
            return _make_resp(404)
        if "/data_sources/" in url and "query" not in url:
            return _make_resp(404)
        if "/data_sources/" in url and "query" in url:
            if child_db_status == 200:
                return _make_resp(200, {"results": [], "has_more": False})
            resp = _make_resp(child_db_status)
            resp.request = httpx.Request(method.upper(), url)
            return resp
        return _make_resp(404)

    client.request = AsyncMock(side_effect=mock_request)
    return client


async def _run_sync(
    *,
    stale_child_databases: dict,
    stale_child_dbs: dict | None = None,
    child_db_status: int = 200,
) -> MagicMock:
    """Drive sync_notion_pages with one recursive page containing one child DB.

    Returns the mocked httpx client so tests can inspect whether the child
    database was actually queried (probe) or skipped.
    """
    from app.services.kb_sync import sync_notion_pages

    db = AsyncMock()
    db.begin_nested = MagicMock(
        return_value=AsyncMock(__aenter__=AsyncMock(), __aexit__=AsyncMock())
    )
    kb = MagicMock()
    kb.name = "Test KB"
    kb.id = "kb-id"

    client_mock = _make_notion_client_mock(child_db_status=child_db_status)

    with (
        patch("app.services.kb_sync._get_integration_token", return_value="tok"),
        patch("app.services.kb_sync.httpx.AsyncClient") as mock_ctx,
        patch("app.services.kb_sync._fetch_all_blocks", return_value=[_child_db_block()]),
        patch(
            "app.services.kb_sync._upsert_document",
            return_value=(MagicMock(id="doc-1"), False),
        ),
        patch("app.services.kb_sync._delete_stale_documents", return_value=0),
        patch("app.services.kb_sync._recount_kb_stats"),
        patch("app.services.kb_sync.enqueue_documents"),
    ):
        mock_ctx.return_value.__aenter__ = AsyncMock(return_value=client_mock)
        mock_ctx.return_value.__aexit__ = AsyncMock(return_value=False)

        await sync_notion_pages(
            db,
            kb,
            "user-id",
            page_id=_PARENT_PAGE_ID,
            recursive=True,
            stale_child_dbs=stale_child_dbs if stale_child_dbs is not None else {},
            stale_child_databases=stale_child_databases,
        )

    return client_mock


def _child_db_was_queried(client_mock: MagicMock) -> bool:
    """True when a query/resolution call targeted the quarantined child DB."""
    for call in client_mock.request.await_args_list:
        url = call.args[1] if len(call.args) > 1 else call.kwargs.get("url", "")
        if _CHILD_DB_ID in str(url):
            return True
    return False


@pytest.mark.asyncio
async def test_quarantined_child_db_skipped_and_counter_advances():
    """A quarantined child DB that is not due for recheck is skipped, and the
    counter advances by one so the modulo cadence progresses."""
    stale_non404 = {_CHILD_DB_ID: _NOTION_STALE_CHILD_DB_THRESHOLD}

    client_mock = await _run_sync(stale_child_databases=stale_non404)

    assert not _child_db_was_queried(client_mock), (
        "A quarantined child database must not be queried outside recheck runs"
    )
    assert stale_non404[_CHILD_DB_ID] == _NOTION_STALE_CHILD_DB_THRESHOLD + 1, (
        "The skip must advance the counter by exactly one per run"
    )


@pytest.mark.asyncio
async def test_recheck_probe_success_clears_quarantine():
    """On the due run the child DB is probed; success clears the entry."""
    stale_non404 = {_CHILD_DB_ID: _DUE_COUNT}

    client_mock = await _run_sync(
        stale_child_databases=stale_non404, child_db_status=200
    )

    assert _child_db_was_queried(client_mock), (
        "The due run must probe the quarantined child database"
    )
    assert _CHILD_DB_ID not in stale_non404, (
        "A successful probe must clear the non-404 quarantine entry"
    )


@pytest.mark.asyncio
async def test_recheck_probe_non404_failure_keeps_quarantine_counting():
    """A probe that still fails (non-404) increments the counter, pushing the
    next probe another N runs out."""
    stale_non404 = {_CHILD_DB_ID: _DUE_COUNT}

    client_mock = await _run_sync(
        stale_child_databases=stale_non404, child_db_status=400
    )

    assert _child_db_was_queried(client_mock)
    assert stale_non404[_CHILD_DB_ID] == _DUE_COUNT + 1


@pytest.mark.asyncio
async def test_recheck_probe_404_hands_over_to_counted_404_path():
    """A probe that fails with 404 seeds/records the counted-404 tracker so
    the (recheck-capable) 404 quarantine takes over."""
    stale_non404 = {_CHILD_DB_ID: _DUE_COUNT}
    stale_404: dict = {}

    client_mock = await _run_sync(
        stale_child_databases=stale_non404,
        stale_child_dbs=stale_404,
        child_db_status=404,
    )

    assert _child_db_was_queried(client_mock)
    entry = stale_404.get(_CHILD_DB_ID)
    assert isinstance(entry, dict) and entry.get("count", 0) >= 1, (
        "The 404 must be recorded in the counted-404 tracker"
    )


@pytest.mark.asyncio
async def test_below_threshold_child_db_still_probed_every_run():
    """Below the quarantine threshold nothing is skipped (regression)."""
    stale_non404 = {_CHILD_DB_ID: _NOTION_STALE_CHILD_DB_THRESHOLD - 1}

    client_mock = await _run_sync(stale_child_databases=stale_non404)

    assert _child_db_was_queried(client_mock)
    assert _CHILD_DB_ID not in stale_non404, (
        "A successful query must clear the below-threshold failure count"
    )


async def _run_duplicate_blocks_sync(stale_non404: dict) -> MagicMock:
    """Drive a recursive sync where TWO pages embed the SAME child database.

    Returns the mocked httpx client so tests can inspect whether the
    quarantined child database was queried on a re-encounter.
    """
    from app.services.kb_sync import sync_notion_pages

    db = AsyncMock()
    db.begin_nested = MagicMock(
        return_value=AsyncMock(__aenter__=AsyncMock(), __aexit__=AsyncMock())
    )
    kb = MagicMock()
    kb.name = "Test KB"
    kb.id = "kb-id"

    second_page = "44444444-4444-4444-4444-444444444444"
    client_mock = MagicMock()

    async def mock_request(method: str, url: str, **_: object) -> httpx.Response:
        if "/pages/" in url:
            return _make_resp(200, _page_resp())
        return _make_resp(404)

    client_mock.request = AsyncMock(side_effect=mock_request)

    def _blocks_for(block_id: str) -> list[dict]:
        # Both pages embed the SAME child database plus a link to page 2.
        blocks = [_child_db_block()]
        if block_id == _PARENT_PAGE_ID:
            blocks.append({"id": second_page, "type": "child_page", "child_page": {}})
        return blocks

    with (
        patch("app.services.kb_sync._get_integration_token", return_value="tok"),
        patch("app.services.kb_sync.httpx.AsyncClient") as mock_ctx,
        patch(
            "app.services.kb_sync._fetch_all_blocks",
            side_effect=lambda client, headers, block_id, **kw: _blocks_for(block_id),
        ),
        patch(
            "app.services.kb_sync._upsert_document",
            return_value=(MagicMock(id="doc-1"), False),
        ),
        patch("app.services.kb_sync._delete_stale_documents", return_value=0),
        patch("app.services.kb_sync._recount_kb_stats"),
        patch("app.services.kb_sync.enqueue_documents"),
    ):
        mock_ctx.return_value.__aenter__ = AsyncMock(return_value=client_mock)
        mock_ctx.return_value.__aexit__ = AsyncMock(return_value=False)

        await sync_notion_pages(
            db,
            kb,
            "user-id",
            page_id=_PARENT_PAGE_ID,
            recursive=True,
            stale_child_dbs={},
            stale_child_databases=stale_non404,
        )

    return client_mock


@pytest.mark.asyncio
async def test_skip_counter_advances_once_per_run_with_duplicate_blocks():
    """The same quarantined child DB referenced from multiple pages advances
    the counter only once per run (consecutive-run semantics)."""
    stale_non404 = {_CHILD_DB_ID: _NOTION_STALE_CHILD_DB_THRESHOLD}

    await _run_duplicate_blocks_sync(stale_non404)

    assert stale_non404[_CHILD_DB_ID] == _NOTION_STALE_CHILD_DB_THRESHOLD + 1


@pytest.mark.asyncio
async def test_skip_log_emitted_once_per_run_with_duplicate_blocks(caplog):
    """The info-level skip line fires at most once per run per child DB —
    re-encounters from other pages are logged at debug level only."""
    import logging

    stale_non404 = {_CHILD_DB_ID: _NOTION_STALE_CHILD_DB_THRESHOLD}

    with caplog.at_level(logging.INFO, logger="app.services.kb_sync"):
        await _run_duplicate_blocks_sync(stale_non404)

    skip_lines = [
        r
        for r in caplog.records
        if r.levelno == logging.INFO
        and "skipping quarantined child database" in r.getMessage()
        and _CHILD_DB_ID in r.getMessage()
    ]
    assert len(skip_lines) == 1, (
        f"Expected exactly one skip log line, got {len(skip_lines)}"
    )


@pytest.mark.asyncio
async def test_recovery_probe_fires_at_most_once_per_run():
    """On a due run where the probe SUCCEEDS (clearing the tracking entry),
    a re-encounter of the same child DB from another page must not probe
    again — the probe decision is frozen at the first encounter."""
    stale_non404 = {_CHILD_DB_ID: _DUE_COUNT}

    client_mock = await _run_duplicate_blocks_sync_with_success(stale_non404)

    probe_calls = [
        call
        for call in client_mock.request.await_args_list
        if _CHILD_DB_ID
        in str(call.args[1] if len(call.args) > 1 else call.kwargs.get("url", ""))
    ]
    # One resolution call + one query call at most (single probe): the
    # child DB must not be re-queried after the successful probe cleared
    # the tracking entry.
    urls = [
        str(call.args[1] if len(call.args) > 1 else call.kwargs.get("url", ""))
        for call in probe_calls
    ]
    query_calls = [u for u in urls if "query" in u]
    assert len(query_calls) <= 1, (
        f"The recovery probe must fire at most once per run, got {query_calls}"
    )
    assert _CHILD_DB_ID not in stale_non404


async def _run_duplicate_blocks_sync_with_success(stale_non404: dict) -> MagicMock:
    """Like _run_duplicate_blocks_sync but the child DB query SUCCEEDS."""
    from app.services.kb_sync import sync_notion_pages

    db = AsyncMock()
    db.begin_nested = MagicMock(
        return_value=AsyncMock(__aenter__=AsyncMock(), __aexit__=AsyncMock())
    )
    kb = MagicMock()
    kb.name = "Test KB"
    kb.id = "kb-id"

    second_page = "44444444-4444-4444-4444-444444444444"
    client_mock = _make_notion_client_mock(child_db_status=200)

    def _blocks_for(block_id: str) -> list[dict]:
        blocks = [_child_db_block()]
        if block_id == _PARENT_PAGE_ID:
            blocks.append({"id": second_page, "type": "child_page", "child_page": {}})
        return blocks

    with (
        patch("app.services.kb_sync._get_integration_token", return_value="tok"),
        patch("app.services.kb_sync.httpx.AsyncClient") as mock_ctx,
        patch(
            "app.services.kb_sync._fetch_all_blocks",
            side_effect=lambda client, headers, block_id, **kw: _blocks_for(block_id),
        ),
        patch(
            "app.services.kb_sync._upsert_document",
            return_value=(MagicMock(id="doc-1"), False),
        ),
        patch("app.services.kb_sync._delete_stale_documents", return_value=0),
        patch("app.services.kb_sync._recount_kb_stats"),
        patch("app.services.kb_sync.enqueue_documents"),
    ):
        mock_ctx.return_value.__aenter__ = AsyncMock(return_value=client_mock)
        mock_ctx.return_value.__aexit__ = AsyncMock(return_value=False)

        await sync_notion_pages(
            db,
            kb,
            "user-id",
            page_id=_PARENT_PAGE_ID,
            recursive=True,
            stale_child_dbs={},
            stale_child_databases=stale_non404,
        )

    return client_mock


@pytest.mark.asyncio
async def test_recheck_decision_does_not_flip_mid_run():
    """A re-encounter must not recompute the recheck cadence against the
    just-incremented counter: entering the run one tick before the probe
    boundary, the first encounter advances the counter to the boundary
    value, and the second encounter must still SKIP (not probe) — the
    probe belongs to the NEXT run."""
    stale_non404 = {_CHILD_DB_ID: _DUE_COUNT - 1}

    client_mock = await _run_duplicate_blocks_sync(stale_non404)

    assert not _child_db_was_queried(client_mock), (
        "The recheck probe must not fire mid-run on a re-encounter after "
        "the counter was advanced by the first encounter"
    )
    assert stale_non404[_CHILD_DB_ID] == _DUE_COUNT
