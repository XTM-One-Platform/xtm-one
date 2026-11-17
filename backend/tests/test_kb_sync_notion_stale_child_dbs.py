"""Tests for Notion KB sync stale child-database skip logic.

Covers:
- _is_notion_child_db_stale: returns False when below threshold, True when
  threshold reached and within recheck window, False again after window
  expires; corrupt entries (missing/unparseable timestamp) are retried so
  they self-heal instead of being skipped forever
- _record_child_db_404: increments count, sets timestamps, returns new count
- _prune_stale_child_db_entries: drops entries older than the prune TTL and
  corrupt entries, keeps recent ones
- sync_notion_pages child-database BFS handling:
  - Skips known-stale child databases and emits only a DEBUG message
  - Logs a single WARNING when first reaching the stale threshold
  - Logs a WARNING on re-check attempts that still fail
  - Clears stale state and logs INFO when a child database recovers
  - Non-404 errors do NOT accumulate in the stale tracker
  - Normal (pre-threshold) 404s still emit a per-run WARNING
  - stale_child_dbs=None keeps the per-run WARNING behaviour for 404s
    (repeated 404s for the SAME child DB are still deduplicated within a
    single run via run_404_child_db_ids, independent of stale tracking)
  - Combined non-404 + 404 failure streak reaches quarantine threshold
    together when error type transitions mid-streak
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.services.kb_sync import (
    _NOTION_CHILD_DB_PRUNE_DAYS,
    _NOTION_CHILD_DB_RECHECK_DAYS,
    _NOTION_CHILD_DB_STALE_THRESHOLD,
    _is_notion_child_db_stale,
    _prune_stale_child_db_entries,
    _record_child_db_404,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_CHILD_DB_ID = "11111111-1111-1111-1111-111111111111"
_PAGE_ID = "22222222-2222-2222-2222-222222222222"
_PARENT_PAGE_ID = "33333333-3333-3333-3333-333333333333"


# ---------------------------------------------------------------------------
# _is_notion_child_db_stale
# ---------------------------------------------------------------------------


def test_is_stale_returns_false_when_entry_missing():
    assert _is_notion_child_db_stale({}, _CHILD_DB_ID) is False


def test_is_stale_returns_false_below_threshold():
    stale = {_CHILD_DB_ID: {"count": _NOTION_CHILD_DB_STALE_THRESHOLD - 1, "last_failed_at": datetime.now(UTC).isoformat()}}
    assert _is_notion_child_db_stale(stale, _CHILD_DB_ID) is False


def test_is_stale_returns_true_at_threshold_within_recheck_window():
    """count == threshold AND last_failed_at was recent → skip."""
    stale = {
        _CHILD_DB_ID: {
            "count": _NOTION_CHILD_DB_STALE_THRESHOLD,
            "last_failed_at": datetime.now(UTC).isoformat(),
        }
    }
    assert _is_notion_child_db_stale(stale, _CHILD_DB_ID) is True


def test_is_stale_returns_false_after_recheck_window_expires():
    """count >= threshold BUT last_failed_at is older than recheck window → allow retry."""
    old_ts = (datetime.now(UTC) - timedelta(days=_NOTION_CHILD_DB_RECHECK_DAYS + 1)).isoformat()
    stale = {
        _CHILD_DB_ID: {
            "count": _NOTION_CHILD_DB_STALE_THRESHOLD,
            "last_failed_at": old_ts,
        }
    }
    assert _is_notion_child_db_stale(stale, _CHILD_DB_ID) is False


def test_is_stale_returns_false_when_no_last_failed_at():
    """count >= threshold but no timestamp → retry so the entry self-heals.

    A failed retry re-records a valid ``last_failed_at`` via
    _record_child_db_404; a successful one clears the entry. Returning True
    would skip the database forever on a corrupt entry.
    """
    stale = {_CHILD_DB_ID: {"count": _NOTION_CHILD_DB_STALE_THRESHOLD}}
    assert _is_notion_child_db_stale(stale, _CHILD_DB_ID) is False


def test_is_stale_handles_naive_datetime_as_utc():
    """Naive (tz-unaware) timestamps should be treated as UTC, not crash."""
    now_naive = datetime.now(UTC).replace(tzinfo=None).isoformat()
    stale = {_CHILD_DB_ID: {"count": _NOTION_CHILD_DB_STALE_THRESHOLD, "last_failed_at": now_naive}}
    # Should return True (stale), not raise
    assert _is_notion_child_db_stale(stale, _CHILD_DB_ID) is True


def test_is_stale_returns_false_on_unparseable_timestamp():
    """Unparseable timestamp → retry once so the entry self-heals."""
    stale = {_CHILD_DB_ID: {"count": _NOTION_CHILD_DB_STALE_THRESHOLD, "last_failed_at": "not-a-date"}}
    assert _is_notion_child_db_stale(stale, _CHILD_DB_ID) is False


def test_is_stale_returns_false_on_non_dict_entry():
    """A corrupt (non-dict) entry must not raise and must allow a retry."""
    stale = {_CHILD_DB_ID: "corrupt"}
    assert _is_notion_child_db_stale(stale, _CHILD_DB_ID) is False


def test_is_stale_returns_false_on_non_int_count():
    """A corrupt (non-int) count must not raise a TypeError comparison."""
    recent_ts = datetime.now(UTC).isoformat()
    stale = {_CHILD_DB_ID: {"count": "three", "last_failed_at": recent_ts}}
    assert _is_notion_child_db_stale(stale, _CHILD_DB_ID) is False


# ---------------------------------------------------------------------------
# _record_child_db_404
# ---------------------------------------------------------------------------


def test_record_404_starts_count_at_one_for_new_entry():
    stale: dict = {}
    count = _record_child_db_404(stale, _CHILD_DB_ID)
    assert count == 1
    assert stale[_CHILD_DB_ID]["count"] == 1
    assert "first_failed_at" in stale[_CHILD_DB_ID]
    assert "last_failed_at" in stale[_CHILD_DB_ID]


def test_record_404_increments_existing_count():
    stale = {_CHILD_DB_ID: {"count": 2, "first_failed_at": "2026-01-01T00:00:00+00:00", "last_failed_at": "2026-01-02T00:00:00+00:00"}}
    count = _record_child_db_404(stale, _CHILD_DB_ID)
    assert count == 3
    assert stale[_CHILD_DB_ID]["count"] == 3
    # first_failed_at must be preserved
    assert stale[_CHILD_DB_ID]["first_failed_at"] == "2026-01-01T00:00:00+00:00"
    # last_failed_at must be updated (no longer the old value)
    assert stale[_CHILD_DB_ID]["last_failed_at"] != "2026-01-02T00:00:00+00:00"


def test_record_404_returns_new_count():
    stale = {_CHILD_DB_ID: {"count": 5, "first_failed_at": "x", "last_failed_at": "y"}}
    assert _record_child_db_404(stale, _CHILD_DB_ID) == 6


def test_record_404_restarts_streak_on_corrupt_count():
    """A non-int count (hand-edited config) restarts the streak at 1."""
    stale = {_CHILD_DB_ID: {"count": "three", "first_failed_at": "x", "last_failed_at": "y"}}
    assert _record_child_db_404(stale, _CHILD_DB_ID) == 1
    assert stale[_CHILD_DB_ID]["count"] == 1


def test_record_404_restarts_streak_on_non_dict_entry():
    """A non-dict entry (hand-edited config) is replaced by a fresh entry."""
    stale = {_CHILD_DB_ID: "corrupt"}
    assert _record_child_db_404(stale, _CHILD_DB_ID) == 1
    assert stale[_CHILD_DB_ID]["count"] == 1


# ---------------------------------------------------------------------------
# _prune_stale_child_db_entries
# ---------------------------------------------------------------------------


def test_prune_drops_entries_older_than_ttl():
    old_ts = (datetime.now(UTC) - timedelta(days=_NOTION_CHILD_DB_PRUNE_DAYS + 1)).isoformat()
    recent_ts = datetime.now(UTC).isoformat()
    stale = {
        "old-db": {"count": 5, "first_failed_at": old_ts, "last_failed_at": old_ts},
        "recent-db": {"count": 2, "first_failed_at": recent_ts, "last_failed_at": recent_ts},
    }
    pruned = _prune_stale_child_db_entries(stale)
    assert pruned == 1
    assert "old-db" not in stale
    assert "recent-db" in stale


def test_prune_drops_entries_with_missing_or_unparseable_timestamp():
    stale = {
        "no-ts": {"count": 3},
        "bad-ts": {"count": 3, "last_failed_at": "not-a-date"},
        "not-a-dict": "corrupt",
    }
    assert _prune_stale_child_db_entries(stale) == 3
    assert stale == {}


def test_prune_keeps_recent_entries_and_returns_zero():
    recent_ts = datetime.now(UTC).isoformat()
    stale = {_CHILD_DB_ID: {"count": 1, "first_failed_at": recent_ts, "last_failed_at": recent_ts}}
    assert _prune_stale_child_db_entries(stale) == 0
    assert _CHILD_DB_ID in stale


# ---------------------------------------------------------------------------
# sync_notion_pages — child-database BFS stale skip integration tests
# ---------------------------------------------------------------------------
# We test sync_notion_pages by patching its internal HTTP client calls so we
# can exercise the stale-tracking code paths without a real Notion API.


def _make_resp(status: int, body: dict | None = None) -> httpx.Response:
    return httpx.Response(
        status,
        json=body or {},
        request=httpx.Request("GET", "https://api.notion.com/v1/placeholder"),
    )


def _page_resp(page_id: str = _PARENT_PAGE_ID) -> dict:
    """Minimal Notion page API response (child_database blocks are served
    separately via the mocked blocks endpoint / _fetch_all_blocks patch)."""
    return {
        "object": "page",
        "id": page_id,
        "properties": {},
        "url": f"https://notion.so/{page_id.replace('-', '')}",
    }


def _child_db_block(db_id: str = _CHILD_DB_ID) -> dict:
    return {"id": db_id, "type": "child_database", "child_database": {"title": "My DB"}}


def _make_notion_client_mock(
    *,
    child_db_status: int = 200,
    child_db_page_ids: list[str] | None = None,
) -> MagicMock:
    """Build a mock httpx.AsyncClient for use with sync_notion_pages."""
    client = MagicMock()

    async def mock_request(method: str, url: str, **_: object) -> httpx.Response:
        # Page metadata fetch
        if "/pages/" in url:
            return _make_resp(200, _page_resp())
        # Child-database resolution: GET /databases/{id}
        if "/databases/" in url:
            return _make_resp(404)
        # Child-database resolution: GET /data_sources/{id}
        if "/data_sources/" in url and "query" not in url:
            return _make_resp(404)
        # Child-database query: POST /data_sources/{id}/query
        if "/data_sources/" in url and "query" in url:
            if child_db_status == 200:
                results = [{"object": "page", "id": pid} for pid in (child_db_page_ids or [])]
                return _make_resp(200, {"results": results, "has_more": False})
            resp = _make_resp(child_db_status)
            resp.request = httpx.Request(method.upper(), url)
            return resp
        # Fallback
        return _make_resp(404)

    client.request = AsyncMock(side_effect=mock_request)
    return client


async def _run_sync_notion_pages_with_stale(
    stale_child_dbs: dict,
    child_db_status: int = 200,
    child_db_page_ids: list[str] | None = None,
    stale_child_databases: dict | None = None,
) -> list[str]:
    """Drive sync_notion_pages with a single recursive page containing one child database.

    ``stale_child_databases`` optionally passes the NON-404 failure counter dict
    alongside the 404 tracking dict, to exercise the counter carry-over when
    errors transition from non-404 (e.g. HTTP 400) to HTTP 404 across sync
    cycles.
    """
    from app.services.kb_sync import sync_notion_pages

    db = AsyncMock()
    db.begin_nested = MagicMock(return_value=AsyncMock(__aenter__=AsyncMock(), __aexit__=AsyncMock()))
    kb = MagicMock()
    kb.name = "Test KB"
    kb.id = "kb-id"

    # We need to patch _get_integration_token, httpx.AsyncClient, _fetch_all_blocks,
    # and _upsert_document to isolate the child-database handling logic.
    client_mock = _make_notion_client_mock(
        child_db_status=child_db_status,
        child_db_page_ids=child_db_page_ids or [],
    )

    with (
        patch("app.services.kb_sync._get_integration_token", return_value="tok"),
        patch("app.services.kb_sync.httpx.AsyncClient") as mock_ctx,
        patch("app.services.kb_sync._fetch_all_blocks", return_value=[_child_db_block()]),
        patch("app.services.kb_sync._upsert_document", return_value=(MagicMock(id="doc-1"), False)),
        patch("app.services.kb_sync._delete_stale_documents", return_value=0),
        patch("app.services.kb_sync._recount_kb_stats"),
        patch("app.services.kb_sync.enqueue_documents"),
    ):
        mock_ctx.return_value.__aenter__ = AsyncMock(return_value=client_mock)
        mock_ctx.return_value.__aexit__ = AsyncMock(return_value=False)

        doc_ids = await sync_notion_pages(
            db,
            kb,
            "user-id",
            page_id=_PARENT_PAGE_ID,
            recursive=True,
            stale_child_dbs=stale_child_dbs,
            stale_child_databases=stale_child_databases,
        )

    return doc_ids


@pytest.mark.asyncio
async def test_child_db_404_below_threshold_emits_warning(caplog):
    """A 404 before the threshold emits the usual per-run WARNING."""
    stale: dict = {}  # no prior failures
    with caplog.at_level(logging.WARNING, logger="app.services.kb_sync"):
        await _run_sync_notion_pages_with_stale(stale, child_db_status=404)

    warning_msgs = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert any("Failed to query child database" in m for m in warning_msgs), (
        "Expected a WARNING about the failed child database query"
    )
    assert stale.get(_CHILD_DB_ID, {}).get("count", 0) == 1


@pytest.mark.asyncio
async def test_child_db_404_at_threshold_emits_single_stale_warning(caplog):
    """When count reaches the stale threshold, a single WARNING is emitted."""
    # Pre-fill with count = threshold - 1 so this run pushes it over
    prior_ts = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    stale = {
        _CHILD_DB_ID: {
            "count": _NOTION_CHILD_DB_STALE_THRESHOLD - 1,
            "first_failed_at": prior_ts,
            "last_failed_at": prior_ts,
        }
    }
    with caplog.at_level(logging.WARNING, logger="app.services.kb_sync"):
        await _run_sync_notion_pages_with_stale(stale, child_db_status=404)

    warning_msgs = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert any("marking as stale" in m for m in warning_msgs), (
        f"Expected 'marking as stale' WARNING. Got: {warning_msgs}"
    )
    assert stale[_CHILD_DB_ID]["count"] == _NOTION_CHILD_DB_STALE_THRESHOLD


@pytest.mark.asyncio
async def test_child_db_skipped_when_stale(caplog):
    """A stale child database is silently skipped (no WARNING, only DEBUG)."""
    recent_ts = datetime.now(UTC).isoformat()
    stale = {
        _CHILD_DB_ID: {
            "count": _NOTION_CHILD_DB_STALE_THRESHOLD,
            "first_failed_at": recent_ts,
            "last_failed_at": recent_ts,
        }
    }
    count_before = stale[_CHILD_DB_ID]["count"]

    with caplog.at_level(logging.DEBUG, logger="app.services.kb_sync"):
        await _run_sync_notion_pages_with_stale(stale, child_db_status=404)

    warning_msgs = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert not any("Failed to query child database" in m for m in warning_msgs), (
        "Stale child DB should NOT emit a per-run WARNING"
    )
    assert not any("marking as stale" in m for m in warning_msgs), (
        "Re-logging 'marking as stale' would re-introduce the noise"
    )
    debug_msgs = [r.message for r in caplog.records if r.levelno == logging.DEBUG]
    assert any("skipping known-stale" in m for m in debug_msgs), (
        "Expected a DEBUG 'skipping known-stale' message"
    )
    # Count must not increase (the DB was never attempted, so no new 404)
    assert stale[_CHILD_DB_ID]["count"] == count_before


@pytest.mark.asyncio
async def test_child_db_recheck_allowed_after_window_expires(caplog):
    """After the recheck window expires, the child database is retried."""
    old_ts = (datetime.now(UTC) - timedelta(days=_NOTION_CHILD_DB_RECHECK_DAYS + 1)).isoformat()
    stale = {
        _CHILD_DB_ID: {
            "count": _NOTION_CHILD_DB_STALE_THRESHOLD,
            "first_failed_at": old_ts,
            "last_failed_at": old_ts,
        }
    }
    # Simulate the recheck succeeding — the child DB is available again
    with caplog.at_level(logging.INFO, logger="app.services.kb_sync"):
        await _run_sync_notion_pages_with_stale(
            stale,
            child_db_status=200,
            child_db_page_ids=[_PAGE_ID],
        )

    # After recovery, the child DB should be removed from the stale dict
    assert _CHILD_DB_ID not in stale, "Recovered child DB should be cleared from stale_child_dbs"
    # And an INFO about recovery should have been logged
    info_msgs = [r.message for r in caplog.records if r.levelno == logging.INFO]
    assert any("recovered" in m for m in info_msgs), (
        f"Expected 'recovered' INFO message. Got info: {info_msgs}"
    )


@pytest.mark.asyncio
async def test_child_db_recheck_still_failing_emits_recheck_warning(caplog):
    """A re-check attempt that still returns 404 emits a WARNING (once)."""
    old_ts = (datetime.now(UTC) - timedelta(days=_NOTION_CHILD_DB_RECHECK_DAYS + 1)).isoformat()
    stale = {
        _CHILD_DB_ID: {
            "count": _NOTION_CHILD_DB_STALE_THRESHOLD,
            "first_failed_at": old_ts,
            "last_failed_at": old_ts,
        }
    }
    with caplog.at_level(logging.WARNING, logger="app.services.kb_sync"):
        await _run_sync_notion_pages_with_stale(stale, child_db_status=404)

    warning_msgs = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert any("still stale after re-check" in m for m in warning_msgs), (
        f"Expected 'still stale after re-check' WARNING. Got: {warning_msgs}"
    )
    # Count should increment (re-check attempted but failed)
    assert stale[_CHILD_DB_ID]["count"] == _NOTION_CHILD_DB_STALE_THRESHOLD + 1


@pytest.mark.asyncio
async def test_non_404_error_does_not_accumulate_stale_count(caplog):
    """Non-404 exceptions emit a WARNING but do NOT increment the stale counter."""
    stale: dict = {}
    # Simulate a 500 error by patching _query_notion_database_pages to raise
    from app.services.kb_sync import sync_notion_pages

    db = AsyncMock()
    db.begin_nested = MagicMock(return_value=AsyncMock(__aenter__=AsyncMock(), __aexit__=AsyncMock()))
    kb = MagicMock()
    kb.name = "Test KB"
    kb.id = "kb-id"

    async def _fail_with_500(*args, **kwargs):
        raise httpx.HTTPStatusError(
            "Server Error '500 Internal Server Error'",
            request=httpx.Request("POST", "https://api.notion.com/v1/data_sources/x/query"),
            response=httpx.Response(500, request=httpx.Request("POST", "https://api.notion.com/v1/data_sources/x/query")),
        )

    with (
        patch("app.services.kb_sync._get_integration_token", return_value="tok"),
        patch("app.services.kb_sync.httpx.AsyncClient") as mock_ctx,
        patch("app.services.kb_sync._fetch_all_blocks", return_value=[_child_db_block()]),
        patch("app.services.kb_sync._upsert_document", return_value=(MagicMock(id="doc-1"), False)),
        patch("app.services.kb_sync._delete_stale_documents", return_value=0),
        patch("app.services.kb_sync._recount_kb_stats"),
        patch("app.services.kb_sync.enqueue_documents"),
        patch("app.services.kb_sync._query_notion_database_pages", side_effect=_fail_with_500),
    ):
        client_mock = MagicMock()
        client_mock.request = AsyncMock(return_value=_make_resp(200, _page_resp()))
        mock_ctx.return_value.__aenter__ = AsyncMock(return_value=client_mock)
        mock_ctx.return_value.__aexit__ = AsyncMock(return_value=False)

        with caplog.at_level(logging.WARNING, logger="app.services.kb_sync"):
            await sync_notion_pages(
                db,
                kb,
                "user-id",
                page_id=_PARENT_PAGE_ID,
                recursive=True,
                stale_child_dbs=stale,
            )

    # stale dict should NOT have an entry for the child DB (500 is not 404)
    assert _CHILD_DB_ID not in stale, "Non-404 error must not add an entry to stale_child_dbs"
    warning_msgs = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert any("Failed to query child database" in m for m in warning_msgs), (
        "Expected a normal WARNING for the 500 error"
    )


@pytest.mark.asyncio
async def test_non_404_error_resets_prior_stale_count(caplog):
    """A non-404 error clears any accumulated 404 counter (breaks the consecutive streak)."""
    # Recent timestamp: the entry must survive the TTL prune at sync start so
    # the test exercises the non-404 reset path (not the prune path).
    recent_ts = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    stale = {
        _CHILD_DB_ID: {
            "count": _NOTION_CHILD_DB_STALE_THRESHOLD - 1,
            "first_failed_at": recent_ts,
            "last_failed_at": recent_ts,
        }
    }
    from app.services.kb_sync import sync_notion_pages

    db = AsyncMock()
    db.begin_nested = MagicMock(return_value=AsyncMock(__aenter__=AsyncMock(), __aexit__=AsyncMock()))
    kb = MagicMock()
    kb.name = "Test KB"
    kb.id = "kb-id"

    async def _fail_with_500(*args, **kwargs):
        raise httpx.HTTPStatusError(
            "Server Error '500 Internal Server Error'",
            request=httpx.Request("POST", "https://api.notion.com/v1/data_sources/x/query"),
            response=httpx.Response(500, request=httpx.Request("POST", "https://api.notion.com/v1/data_sources/x/query")),
        )

    with (
        patch("app.services.kb_sync._get_integration_token", return_value="tok"),
        patch("app.services.kb_sync.httpx.AsyncClient") as mock_ctx,
        patch("app.services.kb_sync._fetch_all_blocks", return_value=[_child_db_block()]),
        patch("app.services.kb_sync._upsert_document", return_value=(MagicMock(id="doc-1"), False)),
        patch("app.services.kb_sync._delete_stale_documents", return_value=0),
        patch("app.services.kb_sync._recount_kb_stats"),
        patch("app.services.kb_sync.enqueue_documents"),
        patch("app.services.kb_sync._query_notion_database_pages", side_effect=_fail_with_500),
    ):
        client_mock = MagicMock()
        client_mock.request = AsyncMock(return_value=_make_resp(200, _page_resp()))
        mock_ctx.return_value.__aenter__ = AsyncMock(return_value=client_mock)
        mock_ctx.return_value.__aexit__ = AsyncMock(return_value=False)

        await sync_notion_pages(
            db,
            kb,
            "user-id",
            page_id=_PARENT_PAGE_ID,
            recursive=True,
            stale_child_dbs=stale,
        )

    # The prior 404 count should have been cleared (non-404 breaks the streak)
    assert _CHILD_DB_ID not in stale, "Non-404 error should reset prior 404 stale count"


@pytest.mark.asyncio
async def test_stale_tracking_disabled_when_none(caplog):
    """When stale_child_dbs=None, 404s still emit the per-run WARNING.

    (Repeated 404s for the SAME child DB within one run are deduplicated by
    run_404_child_db_ids regardless of stale tracking, so 'per-run' means
    one warning per child DB per sync cycle.)"""
    with caplog.at_level(logging.WARNING, logger="app.services.kb_sync"):
        await _run_sync_notion_pages_with_stale(None, child_db_status=404)  # type: ignore[arg-type]

    warning_msgs = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert any("Failed to query child database" in m for m in warning_msgs), (
        "Without stale tracking, the original WARNING must still fire"
    )


@pytest.mark.asyncio
async def test_successful_child_db_clears_prior_stale_state():
    """A successful child-database query removes the entry from stale_child_dbs."""
    prior_ts = (datetime.now(UTC) - timedelta(days=_NOTION_CHILD_DB_RECHECK_DAYS + 1)).isoformat()
    stale = {
        _CHILD_DB_ID: {
            "count": _NOTION_CHILD_DB_STALE_THRESHOLD,
            "first_failed_at": prior_ts,
            "last_failed_at": prior_ts,
        }
    }
    await _run_sync_notion_pages_with_stale(stale, child_db_status=200, child_db_page_ids=[])
    assert _CHILD_DB_ID not in stale, "Successful query should clear the stale entry"


# ---------------------------------------------------------------------------
# Combined non-404 + 404 failure streak — error type transition
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_404_streak_carried_into_404_counter_on_transition(
    caplog,
) -> None:
    """A non-404 streak is pre-seeded into the 404 counter when the first 404 arrives.

    Scenario: a Notion child database has been failing
    with HTTP 400 (non-404) for two consecutive sync cycles, then the error
    type changes to HTTP 404 (Notion API migration).

    With threshold = 3 and non_404_prior = threshold - 1 (= 2), the combined
    streak (2 non-404 + 1 404) must reach the quarantine threshold immediately:
    the "marking as stale" WARNING must fire and the 404 counter's count must
    equal the threshold after the run.
    """
    non_404_prior = _NOTION_CHILD_DB_STALE_THRESHOLD - 1  # 2 prior non-404 failures
    stale_404: dict = {}  # no prior 404 tracking (first 404 run)
    stale_non404: dict = {_CHILD_DB_ID: non_404_prior}

    with caplog.at_level(logging.WARNING, logger="app.services.kb_sync"):
        await _run_sync_notion_pages_with_stale(
            stale_404,
            child_db_status=404,
            stale_child_databases=stale_non404,
        )

    warning_msgs = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert any("marking as stale" in m for m in warning_msgs), (
        "Expected 'marking as stale' WARNING when combined non-404 + 404 streak "
        f"reaches the threshold. Got: {warning_msgs}"
    )
    # The 404 counter must reflect the combined streak (pre-seeded + 1)
    assert stale_404.get(_CHILD_DB_ID, {}).get("count") == _NOTION_CHILD_DB_STALE_THRESHOLD, (
        "The 404 counter should be pre-seeded from the non-404 streak so the "
        "combined count equals the quarantine threshold."
    )


@pytest.mark.asyncio
async def test_no_pre_seeding_when_non_404_counter_is_zero() -> None:
    """When there is no prior non-404 failure streak, the 404 counter starts at 1.

    Ensures the carry-over logic is a strict no-op for fresh databases
    (non_404_prior == 0) and does not introduce false early quarantines.
    """
    stale_404: dict = {}
    stale_non404: dict = {}  # no prior non-404 failures

    await _run_sync_notion_pages_with_stale(
        stale_404,
        child_db_status=404,
        stale_child_databases=stale_non404,
    )

    assert stale_404.get(_CHILD_DB_ID, {}).get("count") == 1, (
        "Without prior non-404 failures the 404 counter should start at 1."
    )


@pytest.mark.asyncio
async def test_no_pre_seeding_when_404_entry_already_exists() -> None:
    """Pre-seeding is skipped when the child DB already has a 404 tracking entry.

    Guards the idempotency of the carry-over: if the database already has
    an ongoing 404 streak (e.g. from a previous run), the counter must only
    be incremented by 1, not re-seeded from the non-404 count.
    """
    prior_ts = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    # Existing 404 count = 1 (started on a previous run)
    stale_404: dict = {
        _CHILD_DB_ID: {
            "count": 1,
            "first_failed_at": prior_ts,
            "last_failed_at": prior_ts,
        }
    }
    # Also has a stale non-404 count (should NOT be added to existing 404 streak)
    stale_non404: dict = {_CHILD_DB_ID: 2}

    await _run_sync_notion_pages_with_stale(
        stale_404,
        child_db_status=404,
        stale_child_databases=stale_non404,
    )

    # Must be incremented by exactly 1 (not 1 + 2 = 3)
    assert stale_404.get(_CHILD_DB_ID, {}).get("count") == 2, (
        "Pre-seeding must be skipped when an existing 404 entry is present; "
        "the counter should only increment by 1."
    )


@pytest.mark.asyncio
async def test_partial_non_404_streak_requires_additional_404_runs(caplog) -> None:
    """A single prior non-404 failure combined with a single 404 stays below threshold.

    Only threshold - 1 non-404 failures followed by 1 404 immediately triggers
    quarantine. A lesser non-404 streak (threshold - 2 = 1) leaves count = 2,
    still below threshold, so no 'marking as stale' warning fires and the DB
    is retried on the next cycle.
    """
    non_404_prior = _NOTION_CHILD_DB_STALE_THRESHOLD - 2  # 1 prior non-404 failure
    if non_404_prior <= 0:
        pytest.skip(
            "Requires _NOTION_CHILD_DB_STALE_THRESHOLD >= 3 for a meaningful "
            "partial-streak scenario"
        )

    stale_404: dict = {}
    stale_non404: dict = {_CHILD_DB_ID: non_404_prior}

    with caplog.at_level(logging.WARNING, logger="app.services.kb_sync"):
        await _run_sync_notion_pages_with_stale(
            stale_404,
            child_db_status=404,
            stale_child_databases=stale_non404,
        )

    warning_msgs = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert not any("marking as stale" in m for m in warning_msgs), (
        "Only threshold - 1 non-404 failures + 1 404 immediately quarantines. "
        "Fewer non-404 failures must not trigger immediate quarantine."
    )
    # Count should be non_404_prior + 1 (= threshold - 1), still below threshold
    assert stale_404.get(_CHILD_DB_ID, {}).get("count") == non_404_prior + 1


@pytest.mark.asyncio
async def test_pre_seeding_applies_over_corrupt_404_entry(caplog) -> None:
    """A corrupt (non-dict) existing 404 entry does not defeat the carry-over.

    A hand-edited config can leave a non-dict value under the child DB id in
    the 404 tracking dict. Key presence alone must not skip the pre-seeding:
    the corrupt entry is treated as absent, the non-404 streak is carried
    over, and the combined count reaches the quarantine threshold.
    """
    non_404_prior = _NOTION_CHILD_DB_STALE_THRESHOLD - 1
    stale_404: dict = {_CHILD_DB_ID: "corrupt"}
    stale_non404: dict = {_CHILD_DB_ID: non_404_prior}

    with caplog.at_level(logging.WARNING, logger="app.services.kb_sync"):
        await _run_sync_notion_pages_with_stale(
            stale_404,
            child_db_status=404,
            stale_child_databases=stale_non404,
        )

    warning_msgs = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert any("marking as stale" in m for m in warning_msgs), (
        "A corrupt existing 404 entry must be treated as absent so the "
        f"non-404 carry-over still applies. Got: {warning_msgs}"
    )
    assert stale_404.get(_CHILD_DB_ID, {}).get("count") == _NOTION_CHILD_DB_STALE_THRESHOLD


@pytest.mark.asyncio
async def test_pre_seeding_applies_over_corrupt_404_count(caplog) -> None:
    """A dict 404 entry with a corrupt (non-int) count does not defeat the carry-over.

    ``_record_child_db_404`` self-heals a non-int count by restarting the
    streak from 1 — but without pre-seeding the prior non-404 failures would
    be lost. A dict entry whose ``count`` is not a positive int must be
    treated as absent for the carry-over decision.
    """
    non_404_prior = _NOTION_CHILD_DB_STALE_THRESHOLD - 1
    prior_ts = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    stale_404: dict = {
        _CHILD_DB_ID: {
            "count": "three",  # corrupt: non-int count
            "first_failed_at": prior_ts,
            "last_failed_at": prior_ts,
        }
    }
    stale_non404: dict = {_CHILD_DB_ID: non_404_prior}

    with caplog.at_level(logging.WARNING, logger="app.services.kb_sync"):
        await _run_sync_notion_pages_with_stale(
            stale_404,
            child_db_status=404,
            stale_child_databases=stale_non404,
        )

    warning_msgs = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert any("marking as stale" in m for m in warning_msgs), (
        "A dict entry with a corrupt count must be treated as absent so the "
        f"non-404 carry-over still applies. Got: {warning_msgs}"
    )
    assert stale_404.get(_CHILD_DB_ID, {}).get("count") == _NOTION_CHILD_DB_STALE_THRESHOLD


@pytest.mark.asyncio
async def test_pre_seeding_clamps_oversized_non_404_count(caplog) -> None:
    """An oversized non-404 count is clamped so the first 404 lands on the threshold.

    If the persisted non-404 count exceeds threshold - 1 (possible when the
    NON-404 quarantine threshold is raised above the 404 one, or via a
    hand-edited config), the pre-seed is clamped to threshold - 1 so the
    increment produces count == threshold exactly: the one-time 'marking as
    stale' warning fires instead of the misleading 'still stale after
    re-check' branch.  The NON-404 quarantine threshold is patched up so the
    pre-query skip does not hide the transition path.
    """
    oversized = _NOTION_CHILD_DB_STALE_THRESHOLD + 5
    stale_404: dict = {}
    stale_non404: dict = {_CHILD_DB_ID: oversized}

    with (
        caplog.at_level(logging.WARNING, logger="app.services.kb_sync"),
        patch("app.services.kb_sync._NOTION_STALE_CHILD_DB_THRESHOLD", oversized + 10),
    ):
        await _run_sync_notion_pages_with_stale(
            stale_404,
            child_db_status=404,
            stale_child_databases=stale_non404,
        )

    warning_msgs = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert any("marking as stale" in m for m in warning_msgs), (
        f"Expected the one-time 'marking as stale' WARNING. Got: {warning_msgs}"
    )
    assert not any("still stale after re-check" in m for m in warning_msgs), (
        "The clamped pre-seed must not overshoot into the re-check branch."
    )
    assert stale_404.get(_CHILD_DB_ID, {}).get("count") == _NOTION_CHILD_DB_STALE_THRESHOLD

