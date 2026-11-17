"""Tests for Notion stale child-database state loading and persistence.

The counted ``stale_child_dbs`` tracking dict supersedes the
legacy one-shot ``skipped_child_db_ids`` list.  This module
covers the persistence layer of the unified mechanism:

- _load_stale_child_dbs: builds the mutable tracking dict from a persisted
  source config, ignores corrupt (non-dict) values, and migrates legacy
  ``skipped_child_db_ids`` entries to counted entries at the stale threshold
- _persist_stale_child_dbs: writes the dict back to KBSyncSource.config,
  removes the key when empty, always drops the superseded legacy key, is a
  no-op when nothing changed, handles a missing source, and is best-effort
  (exceptions are swallowed)
- sync_notion_pages wiring: 404s are recorded and persisted when
  sync_source_id is provided; a child DB that 404s is queried at most once
  per run (in-run re-encounter skip); non-404 errors are not recorded; no
  persistence happens when sync_source_id is absent

The log-level behaviour of the counted mechanism (threshold warning, weekly
re-check, recovery) is covered in test_kb_sync_notion_stale_child_dbs.py.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.services.kb_sync import (
    _NOTION_CHILD_DB_STALE_THRESHOLD,
    _load_stale_child_dbs,
    _persist_stale_child_dbs,
)

# ---------------------------------------------------------------------------
# Shared fixtures / helpers
# ---------------------------------------------------------------------------

_SOURCE_ID = str(uuid.uuid4())
_CHILD_DB_ID_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
_CHILD_DB_ID_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


def _entry(count: int = 1) -> dict:
    ts = datetime.now(UTC).isoformat()
    return {"count": count, "first_failed_at": ts, "last_failed_at": ts}


def _make_mock_source(config: dict | None = None) -> MagicMock:
    """Build a minimal KBSyncSource mock with the given config."""
    source = MagicMock()
    source.config = config or {}
    return source


def _make_mock_db(source: MagicMock | None = None) -> AsyncMock:
    """Build an AsyncSession mock that returns *source* from db.get()."""
    db = AsyncMock()
    db.get = AsyncMock(return_value=source)
    db.flush = AsyncMock()
    return db


# ---------------------------------------------------------------------------
# _load_stale_child_dbs
# ---------------------------------------------------------------------------


def test_load_returns_copy_of_persisted_dict():
    entry = _entry(count=2)
    config = {"stale_child_dbs": {_CHILD_DB_ID_A: entry}}
    stale = _load_stale_child_dbs(config)
    assert stale == {_CHILD_DB_ID_A: entry}
    # Mutating the returned dict must not alias the config value.
    stale[_CHILD_DB_ID_A]["count"] = 99
    assert config["stale_child_dbs"][_CHILD_DB_ID_A]["count"] == 2


def test_load_returns_empty_dict_when_key_missing():
    assert _load_stale_child_dbs({}) == {}


def test_load_ignores_non_dict_top_level_value():
    """A hand-edited / corrupt non-mapping value must not crash the sync."""
    assert _load_stale_child_dbs({"stale_child_dbs": "corrupt"}) == {}
    assert _load_stale_child_dbs({"stale_child_dbs": [1, 2]}) == {}


def test_load_drops_non_dict_entries():
    config = {
        "stale_child_dbs": {
            _CHILD_DB_ID_A: _entry(),
            _CHILD_DB_ID_B: "corrupt-entry",
        }
    }
    stale = _load_stale_child_dbs(config)
    assert _CHILD_DB_ID_A in stale
    assert _CHILD_DB_ID_B not in stale


def test_load_migrates_legacy_skipped_ids_at_threshold():
    """Legacy one-shot skipped IDs become counted entries at the threshold,
    so previously skipped child DBs stay skipped and enter the weekly
    re-check cycle."""
    config = {"skipped_child_db_ids": [_CHILD_DB_ID_A]}
    stale = _load_stale_child_dbs(config)
    assert stale[_CHILD_DB_ID_A]["count"] == _NOTION_CHILD_DB_STALE_THRESHOLD
    assert "last_failed_at" in stale[_CHILD_DB_ID_A]


def test_load_legacy_migration_does_not_overwrite_counted_entry():
    entry = _entry(count=1)
    config = {
        "stale_child_dbs": {_CHILD_DB_ID_A: entry},
        "skipped_child_db_ids": [_CHILD_DB_ID_A, _CHILD_DB_ID_B],
    }
    stale = _load_stale_child_dbs(config)
    # Existing counted entry wins; only the new legacy ID is migrated.
    assert stale[_CHILD_DB_ID_A]["count"] == 1
    assert stale[_CHILD_DB_ID_B]["count"] == _NOTION_CHILD_DB_STALE_THRESHOLD


# ---------------------------------------------------------------------------
# _persist_stale_child_dbs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_persist_writes_tracking_dict_to_config():
    source = _make_mock_source(config={})
    db = _make_mock_db(source)
    stale = {_CHILD_DB_ID_A: _entry()}

    await _persist_stale_child_dbs(db, _SOURCE_ID, stale)

    assert source.config["stale_child_dbs"] == stale
    db.flush.assert_awaited_once()


@pytest.mark.asyncio
async def test_persist_removes_key_when_dict_empty():
    source = _make_mock_source(config={"stale_child_dbs": {_CHILD_DB_ID_A: _entry()}})
    db = _make_mock_db(source)

    await _persist_stale_child_dbs(db, _SOURCE_ID, {})

    assert "stale_child_dbs" not in source.config
    db.flush.assert_awaited_once()


@pytest.mark.asyncio
async def test_persist_drops_superseded_legacy_key():
    source = _make_mock_source(
        config={"skipped_child_db_ids": [_CHILD_DB_ID_A], "page_id": "p1"}
    )
    db = _make_mock_db(source)
    stale = {_CHILD_DB_ID_A: _entry(count=_NOTION_CHILD_DB_STALE_THRESHOLD)}

    await _persist_stale_child_dbs(db, _SOURCE_ID, stale)

    assert "skipped_child_db_ids" not in source.config
    assert source.config["stale_child_dbs"] == stale
    # Unrelated keys are preserved.
    assert source.config["page_id"] == "p1"


@pytest.mark.asyncio
async def test_persist_is_noop_when_nothing_changed():
    stale = {_CHILD_DB_ID_A: _entry(count=2)}
    original_config = {"stale_child_dbs": stale, "page_id": "p1"}
    source = _make_mock_source(config=original_config)
    db = _make_mock_db(source)

    await _persist_stale_child_dbs(db, _SOURCE_ID, dict(stale))

    # No write when the resulting config equals the persisted one.
    db.flush.assert_not_awaited()
    assert source.config is original_config


@pytest.mark.asyncio
async def test_persist_returns_silently_when_source_not_found():
    db = _make_mock_db(source=None)

    await _persist_stale_child_dbs(db, _SOURCE_ID, {_CHILD_DB_ID_A: _entry()})

    db.flush.assert_not_awaited()


@pytest.mark.asyncio
async def test_persist_is_best_effort_on_db_error():
    db = AsyncMock()
    db.get = AsyncMock(side_effect=RuntimeError("DB unavailable"))

    # Must not raise - the caller's sync result must never be affected
    await _persist_stale_child_dbs(db, _SOURCE_ID, {_CHILD_DB_ID_A: _entry()})


# ---------------------------------------------------------------------------
# sync_notion_pages wiring (recording + persistence + in-run skip)
# ---------------------------------------------------------------------------


def _make_404_error(url: str) -> httpx.HTTPStatusError:
    req = httpx.Request("POST", url)
    resp = httpx.Response(404, request=req)
    return httpx.HTTPStatusError("404 Not Found", request=req, response=resp)


def _fake_notion_page_transport(blocks_results: list[dict]):
    """Build a fake _NotionRequestController.request serving one page."""
    blocks_resp = httpx.Response(
        200,
        json={"results": blocks_results, "has_more": False},
        request=httpx.Request("GET", "https://api.notion.com/v1/blocks/page1/children"),
    )
    page_resp = httpx.Response(
        200,
        json={
            "id": "page1",
            "url": "https://notion.so/page1",
            "properties": {},
            "parent": {"type": "page_id"},
        },
        request=httpx.Request("GET", "https://api.notion.com/v1/pages/page1"),
    )

    async def fake_request(self, client, method, url, **kwargs):
        if "/pages/page1" in url:
            return page_resp
        if "/blocks/page1/children" in url:
            return blocks_resp
        raise AssertionError(f"Unexpected request: {method} {url}")

    return fake_request


async def _run_sync(
    *,
    stale_child_dbs: dict | None,
    sync_source_id: str | None,
    query_side_effect,
    blocks_results: list[dict],
    persist_mock,
):
    from app.services.kb_sync import sync_notion_pages

    db = AsyncMock()
    db.begin_nested = MagicMock(
        return_value=AsyncMock(__aenter__=AsyncMock(), __aexit__=AsyncMock())
    )
    db.commit = AsyncMock()
    db.flush = AsyncMock()
    kb = MagicMock()
    kb.name = "Test KB"

    with (
        patch(
            "app.services.kb_sync._get_integration_token",
            new=AsyncMock(return_value="tok"),
        ),
        patch(
            "app.services.kb_sync._query_notion_database_pages",
            new=query_side_effect,
        ),
        patch("app.services.kb_sync._persist_stale_child_dbs", new=persist_mock),
        patch(
            "app.services.kb_sync._delete_stale_documents",
            new=AsyncMock(return_value=0),
        ),
        patch("app.services.kb_sync._recount_kb_stats", new=AsyncMock()),
        patch("app.services.kb_sync.enqueue_documents", new=AsyncMock()),
        patch(
            "app.services.kb_sync._NotionRequestController.request",
            new=_fake_notion_page_transport(blocks_results),
        ),
    ):
        await sync_notion_pages(
            db,
            kb,
            "user1",
            page_id="page1",
            recursive=True,
            sync_source_id=sync_source_id,
            stale_child_dbs=stale_child_dbs,
        )


@pytest.mark.asyncio
async def test_404_recorded_and_persisted_when_source_id_given():
    """A 404 increments the tracking dict and the state is persisted."""
    stale: dict = {}
    persisted: list = []

    async def fake_persist(db, source_id, stale_dict):
        persisted.append((source_id, dict(stale_dict)))

    child_db_url = f"https://api.notion.com/v1/data_sources/{_CHILD_DB_ID_A}/query"
    await _run_sync(
        stale_child_dbs=stale,
        sync_source_id=_SOURCE_ID,
        query_side_effect=AsyncMock(side_effect=_make_404_error(child_db_url)),
        blocks_results=[{"type": "child_database", "id": _CHILD_DB_ID_A}],
        persist_mock=fake_persist,
    )

    assert stale[_CHILD_DB_ID_A]["count"] == 1
    assert len(persisted) == 1
    assert persisted[0][0] == _SOURCE_ID
    assert _CHILD_DB_ID_A in persisted[0][1]


@pytest.mark.asyncio
async def test_404_child_db_queried_once_per_run_on_reencounter():
    """A child DB that 404s is skipped when re-encountered in the SAME run.

    The same stale child database can be referenced as a block from multiple
    pages; without the in-run skip, each re-encounter re-fires a
    guaranteed-404 API call AND would double-count the failure, letting the
    'consecutive sync runs' threshold be crossed within a single run.
    """
    stale: dict = {}
    query_calls: list[str] = []
    child_db_url = f"https://api.notion.com/v1/data_sources/{_CHILD_DB_ID_A}/query"
    err = _make_404_error(child_db_url)

    async def fake_query(client, headers, database_id, **kwargs):
        query_calls.append(database_id)
        raise err

    await _run_sync(
        stale_child_dbs=stale,
        sync_source_id=_SOURCE_ID,
        query_side_effect=fake_query,
        blocks_results=[
            {"type": "child_database", "id": _CHILD_DB_ID_A},
            {"type": "child_database", "id": _CHILD_DB_ID_A},
        ],
        persist_mock=AsyncMock(),
    )

    assert query_calls == [_CHILD_DB_ID_A], (
        f"a child DB that 404'd must be queried exactly once per run, got {query_calls}"
    )
    assert stale[_CHILD_DB_ID_A]["count"] == 1, (
        "re-encounters within one run must not double-count the 404"
    )


@pytest.mark.asyncio
async def test_stale_entry_prevents_api_call():
    """Child DBs already at the stale threshold are skipped without a query."""
    query_calls: list[str] = []

    async def fake_query(client, headers, database_id, **kwargs):
        query_calls.append(database_id)
        return []

    stale = {
        _CHILD_DB_ID_A: {
            "count": _NOTION_CHILD_DB_STALE_THRESHOLD,
            "first_failed_at": datetime.now(UTC).isoformat(),
            "last_failed_at": datetime.now(UTC).isoformat(),
        }
    }
    await _run_sync(
        stale_child_dbs=stale,
        sync_source_id=_SOURCE_ID,
        query_side_effect=fake_query,
        blocks_results=[
            {"type": "child_database", "id": _CHILD_DB_ID_A},  # stale -> skipped
            {"type": "child_database", "id": _CHILD_DB_ID_B},  # fresh -> queried
        ],
        persist_mock=AsyncMock(),
    )

    assert _CHILD_DB_ID_A not in query_calls, "Stale ID must not be queried"
    assert _CHILD_DB_ID_B in query_calls, "Non-stale ID must be queried"


@pytest.mark.asyncio
async def test_non_404_http_error_not_recorded():
    """A non-404 HTTP error (e.g. 403) must not be recorded as stale."""
    stale: dict = {}
    child_db_url = f"https://api.notion.com/v1/data_sources/{_CHILD_DB_ID_A}/query"
    req = httpx.Request("POST", child_db_url)
    err_403 = httpx.HTTPStatusError(
        "403 Forbidden", request=req, response=httpx.Response(403, request=req)
    )

    await _run_sync(
        stale_child_dbs=stale,
        sync_source_id=_SOURCE_ID,
        query_side_effect=AsyncMock(side_effect=err_403),
        blocks_results=[{"type": "child_database", "id": _CHILD_DB_ID_A}],
        persist_mock=AsyncMock(),
    )

    assert _CHILD_DB_ID_A not in stale


@pytest.mark.asyncio
async def test_no_persist_when_sync_source_id_absent():
    """Without a sync_source_id (direct API endpoint), nothing is persisted."""
    stale: dict = {}
    persist = AsyncMock()
    child_db_url = f"https://api.notion.com/v1/data_sources/{_CHILD_DB_ID_A}/query"

    await _run_sync(
        stale_child_dbs=stale,
        sync_source_id=None,
        query_side_effect=AsyncMock(side_effect=_make_404_error(child_db_url)),
        blocks_results=[{"type": "child_database", "id": _CHILD_DB_ID_A}],
        persist_mock=persist,
    )

    persist.assert_not_awaited()
    # The in-memory dict is still updated (the caller could persist it).
    assert stale[_CHILD_DB_ID_A]["count"] == 1
