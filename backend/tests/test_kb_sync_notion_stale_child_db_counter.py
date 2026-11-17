"""Tests for the Notion child-database non-404 failure-counter quarantine.

Complements the counted 404 tracking (``stale_child_dbs``, covered in
test_kb_sync_notion_stale_child_db.py / test_kb_sync_notion_stale_child_dbs.py):
child databases that keep failing with NON-404 errors (e.g. HTTP 400 from a
stale data_source reference after the Notion API migration) are counted per
sync cycle in ``config["stale_child_databases"]`` and quarantined after
``_NOTION_STALE_CHILD_DB_THRESHOLD`` consecutive failing cycles.

Covers:
- A non-404 failure increments the counter in stale_child_databases
- The counter grows at most once per run even on re-encounter
- An elevated warning is emitted exactly when the count first reaches the
  threshold (the quarantine trigger)
- A quarantined child database is skipped without any query call
- A child database below the threshold is still queried; success removes
  the entry (transient failures self-heal)
- A 404 failure takes the counted 404 path and does NOT increment the counter
- stale_child_databases=None (default) disables tracking (backward compat)
- _run_sync_in_isolated_session persists updated counts back to the source
  config, drops the key when all counts reset, skips the UPDATE when nothing
  changed, and tolerates a corrupted (non-dict) config value
- _run_sync_in_isolated_session also backstops the counted-404 state
  (stale_child_dbs): a change is re-persisted and committed even when the
  in-sync persist silently failed, and an unchanged state adds no commit
"""

from __future__ import annotations

import contextlib
import logging
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.services.kb_sync import (
    _NOTION_CHILD_DB_STALE_THRESHOLD,
    _NOTION_STALE_CHILD_DB_THRESHOLD,
    sync_notion_pages,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_KB_ID = uuid.uuid4()
_SOURCE_ID = str(uuid.uuid4())
_PAGE_ID = "11111111-1111-1111-1111-111111111111"
_CHILD_DB_ID = "22222222-2222-2222-2222-222222222222"


def _make_kb() -> SimpleNamespace:
    return SimpleNamespace(
        id=_KB_ID,
        name="Test KB",
        document_count=0,
        chunk_count=0,
    )


def _make_db() -> AsyncMock:
    db = AsyncMock()
    db.begin_nested = MagicMock(
        return_value=AsyncMock(__aenter__=AsyncMock(), __aexit__=AsyncMock())
    )
    db.commit = AsyncMock()
    db.flush = AsyncMock()
    return db


def _make_http_error(status_code: int) -> httpx.HTTPStatusError:
    url = f"https://api.notion.com/v1/data_sources/{_CHILD_DB_ID}/query"
    req = httpx.Request("POST", url)
    resp = httpx.Response(status_code, request=req)
    return httpx.HTTPStatusError(f"{status_code}", request=req, response=resp)


def _page_and_blocks_responses(child_db_count: int = 1):
    """Fake _NotionRequestController.request serving one page with child DBs."""
    page_resp = httpx.Response(
        200,
        json={
            "id": _PAGE_ID,
            "url": f"https://notion.so/{_PAGE_ID}",
            "properties": {},
            "parent": {"type": "page_id"},
        },
        request=httpx.Request(
            "GET", f"https://api.notion.com/v1/pages/{_PAGE_ID}"
        ),
    )
    blocks_resp = httpx.Response(
        200,
        json={
            "results": [
                {"type": "child_database", "id": _CHILD_DB_ID}
                for _ in range(child_db_count)
            ],
            "has_more": False,
        },
        request=httpx.Request(
            "GET", f"https://api.notion.com/v1/blocks/{_PAGE_ID}/children"
        ),
    )

    async def fake_request(self, client, method, url, **kwargs):
        if f"/pages/{_PAGE_ID}" in url:
            return page_resp
        if f"/blocks/{_PAGE_ID}/children" in url:
            return blocks_resp
        raise AssertionError(f"Unexpected request: {method} {url}")

    return fake_request


def _sync_patches(query_mock):
    """Common patch stack for driving sync_notion_pages without DB / HTTP."""
    return (
        patch(
            "app.services.kb_sync._get_integration_token",
            new=AsyncMock(return_value="tok"),
        ),
        patch("app.services.kb_sync._query_notion_database_pages", new=query_mock),
        patch(
            "app.services.kb_sync._persist_stale_child_dbs", new=AsyncMock()
        ),
        patch(
            "app.services.kb_sync._delete_stale_documents",
            new=AsyncMock(return_value=0),
        ),
        patch("app.services.kb_sync._recount_kb_stats", new=AsyncMock()),
        patch("app.services.kb_sync.enqueue_documents", new=AsyncMock()),
    )


async def _run_sync(
    *,
    query_mock,
    stale: dict[str, int] | None,
    child_db_count: int = 1,
    stale_child_dbs: dict | None = None,
) -> None:
    fake_request = _page_and_blocks_responses(child_db_count=child_db_count)
    with contextlib.ExitStack() as stack:
        for p in _sync_patches(query_mock):
            stack.enter_context(p)
        stack.enter_context(
            patch(
                "app.services.kb_sync._NotionRequestController.request",
                new=fake_request,
            )
        )
        await sync_notion_pages(
            _make_db(),
            _make_kb(),
            "user-id",
            page_id=_PAGE_ID,
            recursive=True,
            sync_source_id=_SOURCE_ID,
            stale_child_databases=stale,
            stale_child_dbs=stale_child_dbs,
        )


# ---------------------------------------------------------------------------
# Counter behaviour in sync_notion_pages
# ---------------------------------------------------------------------------


def test_stale_threshold_constant_is_positive() -> None:
    """Sanity check: threshold must be a positive integer."""
    assert isinstance(_NOTION_STALE_CHILD_DB_THRESHOLD, int)
    assert _NOTION_STALE_CHILD_DB_THRESHOLD > 0


@pytest.mark.asyncio
async def test_non_404_failure_increments_counter() -> None:
    """A non-404 child-database failure increments the count."""
    stale: dict[str, int] = {}
    await _run_sync(
        query_mock=AsyncMock(side_effect=_make_http_error(400)),
        stale=stale,
    )
    assert stale.get(_CHILD_DB_ID, 0) == 1


@pytest.mark.asyncio
async def test_counter_increments_once_per_run() -> None:
    """Re-encountering the same failing child DB in one run counts once."""
    stale: dict[str, int] = {}
    await _run_sync(
        query_mock=AsyncMock(side_effect=_make_http_error(400)),
        stale=stale,
        child_db_count=2,
    )
    assert stale.get(_CHILD_DB_ID, 0) == 1, (
        "The counter must grow at most once per sync cycle"
    )


@pytest.mark.asyncio
async def test_quarantine_warning_at_threshold(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An elevated warning fires exactly when the count reaches the threshold."""
    stale: dict[str, int] = {_CHILD_DB_ID: _NOTION_STALE_CHILD_DB_THRESHOLD - 1}
    with caplog.at_level(logging.WARNING, logger="app.services.kb_sync"):
        await _run_sync(
            query_mock=AsyncMock(side_effect=_make_http_error(400)),
            stale=stale,
        )
    assert stale[_CHILD_DB_ID] == _NOTION_STALE_CHILD_DB_THRESHOLD
    quarantine_logs = [
        r
        for r in caplog.records
        if "consecutive sync cycle" in r.message and _CHILD_DB_ID in r.message
    ]
    assert quarantine_logs, (
        "Expected a quarantine-trigger warning naming the child DB"
    )


@pytest.mark.asyncio
async def test_quarantined_child_db_is_skipped() -> None:
    """A child database at the threshold is skipped without any query call."""
    stale: dict[str, int] = {_CHILD_DB_ID: _NOTION_STALE_CHILD_DB_THRESHOLD}
    query_mock = AsyncMock(side_effect=AssertionError("must not be queried"))
    await _run_sync(query_mock=query_mock, stale=stale)
    query_mock.assert_not_awaited()
    # The count advances by one on skipped runs: it drives the
    # periodic recovery-probe cadence (every
    # _NOTION_STALE_CHILD_DB_RECHECK_EVERY quarantined runs the DB is
    # probed again instead of skipped).
    assert stale[_CHILD_DB_ID] == _NOTION_STALE_CHILD_DB_THRESHOLD + 1


@pytest.mark.asyncio
async def test_below_threshold_success_resets_counter() -> None:
    """Below the threshold the DB is still queried; success removes the entry."""
    stale: dict[str, int] = {
        _CHILD_DB_ID: _NOTION_STALE_CHILD_DB_THRESHOLD - 1
    }
    query_mock = AsyncMock(return_value=[])
    await _run_sync(query_mock=query_mock, stale=stale)
    query_mock.assert_awaited()
    assert _CHILD_DB_ID not in stale, (
        "A successful query must remove the child DB from stale_child_databases"
    )


@pytest.mark.asyncio
async def test_404_takes_counted_404_path_not_counter() -> None:
    """A 404 goes to the counted 404 tracking and does NOT increment the counter."""
    stale: dict[str, int] = {}
    await _run_sync(
        query_mock=AsyncMock(side_effect=_make_http_error(404)),
        stale=stale,
    )
    assert _CHILD_DB_ID not in stale, (
        "404s are handled by stale_child_dbs, not the non-404 failure counter"
    )


@pytest.mark.asyncio
async def test_prior_non_404_failures_seed_404_counter() -> None:
    """Prior non-404 failures carry over to the 404 stale counter.

    When a child DB has already failed N times with non-404 errors (e.g.
    HTTP 400 before a Notion API endpoint migration) and then returns its
    first 404, the 404 counter is initialized at N instead of starting at
    0 — so the stale threshold is reached one 404 day sooner for each
    prior non-404 failure rather than resetting the clock on the first 404.
    """
    # Derived from the 404 stale threshold (the code under test): the seed
    # must reach it on the first 404.  The non-404 quarantine threshold is
    # irrelevant here beyond staying below it (a quarantined DB is never
    # queried, so the transition path cannot be reached).
    prior_non404_count = _NOTION_CHILD_DB_STALE_THRESHOLD - 1
    assert prior_non404_count < _NOTION_STALE_CHILD_DB_THRESHOLD, (
        "Seed count must stay below the non-404 quarantine threshold or the "
        "child DB would be skipped before the 404 transition can happen"
    )
    stale_non404: dict[str, int] = {_CHILD_DB_ID: prior_non404_count}
    stale_404: dict = {}

    await _run_sync(
        query_mock=AsyncMock(side_effect=_make_http_error(404)),
        stale=stale_non404,
        stale_child_dbs=stale_404,
    )

    # The first 404 for this child DB should initialize the 404 counter at
    # prior_non404_count (via seeding) then increment it, reaching the
    # stale threshold immediately.
    assert _CHILD_DB_ID in stale_404, "404 counter entry must be created"
    assert stale_404[_CHILD_DB_ID]["count"] == _NOTION_CHILD_DB_STALE_THRESHOLD, (
        "Prior non-404 failures should seed the 404 counter so threshold is "
        "reached on the first 404 when prior count == threshold - 1"
    )


@pytest.mark.asyncio
async def test_partial_non_404_streak_still_requires_more_404_runs() -> None:
    """A seed below threshold - 1 does not mark the child DB stale yet."""
    stale_non404: dict[str, int] = {_CHILD_DB_ID: 1}
    stale_404: dict = {}

    await _run_sync(
        query_mock=AsyncMock(side_effect=_make_http_error(404)),
        stale=stale_non404,
        stale_child_dbs=stale_404,
    )

    # Seeded at 1, incremented to 2 — still below the threshold of 3.
    assert stale_404[_CHILD_DB_ID]["count"] == 2
    assert stale_404[_CHILD_DB_ID]["count"] < _NOTION_CHILD_DB_STALE_THRESHOLD


@pytest.mark.asyncio
async def test_prior_non_404_failures_do_not_seed_when_404_already_tracked() -> None:
    """Non-404 count is NOT carried over if the child DB already has a 404 entry.

    The seeding only applies on the FIRST 404 for a child DB (no existing
    valid entry in stale_child_dbs).  Subsequent 404 runs must not re-seed.
    """
    stale_non404: dict[str, int] = {_CHILD_DB_ID: _NOTION_CHILD_DB_STALE_THRESHOLD - 1}
    # Child DB already has one prior 404 run tracked
    now_str = datetime.now(UTC).isoformat()
    stale_404: dict = {
        _CHILD_DB_ID: {"count": 1, "first_failed_at": now_str, "last_failed_at": now_str}
    }

    await _run_sync(
        query_mock=AsyncMock(side_effect=_make_http_error(404)),
        stale=stale_non404,
        stale_child_dbs=stale_404,
    )

    # The existing entry has count 1; this run adds 1 → count 2 (no re-seeding).
    assert stale_404[_CHILD_DB_ID]["count"] == 2, (
        "Subsequent 404 runs must not re-seed from the non-404 counter — "
        "count should increment normally from the existing entry"
    )


@pytest.mark.asyncio
async def test_corrupt_404_entry_does_not_block_seeding() -> None:
    """A corrupted persisted 404 entry is treated as absent for seeding.

    Mirrors _record_child_db_404's defensive behaviour: a non-dict entry or
    a non-int count (hand-edited config) must not swallow the non-404
    carry-over — otherwise the counter-reset bug would reappear exactly
    when the tracking state needs self-healing.
    """
    stale_non404: dict[str, int] = {_CHILD_DB_ID: _NOTION_CHILD_DB_STALE_THRESHOLD - 1}
    stale_404: dict = {_CHILD_DB_ID: "corrupted-not-a-dict"}

    await _run_sync(
        query_mock=AsyncMock(side_effect=_make_http_error(404)),
        stale=stale_non404,
        stale_child_dbs=stale_404,
    )

    assert stale_404[_CHILD_DB_ID]["count"] == _NOTION_CHILD_DB_STALE_THRESHOLD, (
        "A corrupt existing entry must be treated as absent so the prior "
        "non-404 streak still seeds the 404 counter"
    )


@pytest.mark.asyncio
async def test_tracking_disabled_when_none(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """stale_child_databases=None disables tracking (backward compat)."""
    with caplog.at_level(logging.WARNING, logger="app.services.kb_sync"):
        await _run_sync(
            query_mock=AsyncMock(side_effect=_make_http_error(400)),
            stale=None,
        )
    quarantine_logs = [
        r for r in caplog.records if "consecutive sync cycle" in r.message
    ]
    assert not quarantine_logs, (
        "No quarantine log expected when stale_child_databases=None"
    )
    failure_logs = [
        r
        for r in caplog.records
        if "Failed to query child database" in r.message
    ]
    assert failure_logs, "Standard failure warning must still be emitted"


# ---------------------------------------------------------------------------
# Scheduler persistence (_run_sync_in_isolated_session, notion branch)
# ---------------------------------------------------------------------------


def _make_isolated_session_mocks(source_config: dict | None):
    """Build the sync_db session mock + fake source row for the scheduler tests."""
    kb_result = MagicMock()
    kb_result.scalar_one_or_none.return_value = _make_kb()

    src_row = SimpleNamespace(config=source_config)

    sync_db = MagicMock()
    sync_db.execute = AsyncMock(return_value=kb_result)
    sync_db.get = AsyncMock(return_value=src_row)
    sync_db.commit = AsyncMock()
    sync_db.flush = AsyncMock()

    class _SessionCtx:
        async def __aenter__(self):
            return sync_db

        async def __aexit__(self, *_):
            return False

    return sync_db, src_row, lambda: _SessionCtx()


async def _run_isolated_notion_sync(
    *,
    source_config: dict,
    sync_side_effect=None,
    sync_404_side_effect=None,
) -> tuple[MagicMock, SimpleNamespace]:
    """Run _run_sync_in_isolated_session's notion branch with a fake sync fn.

    ``sync_side_effect(stale_dict)`` mutates the non-404 stale dict the
    scheduler passes to sync_notion_pages, simulating in-sync failure/reset
    activity.
    ``sync_404_side_effect(stale_dict)`` mutates the counted-404 stale dict.
    """
    from app.services.kb_sync_scheduler import _run_sync_in_isolated_session

    sync_db, src_row, session_factory = _make_isolated_session_mocks(
        dict(source_config)
    )

    async def _fake_sync_notion_pages(
        *_a,
        stale_child_databases=None,
        stale_child_dbs=None,
        **_kw,
    ):
        if sync_side_effect is not None:
            sync_side_effect(stale_child_databases)
        if sync_404_side_effect is not None:
            sync_404_side_effect(stale_child_dbs)
        return []

    with (
        patch("app.core.database.async_session", session_factory),
        patch(
            "app.services.kb_sync._apply_sync_statement_timeout", new=AsyncMock()
        ),
        patch(
            "app.services.kb_sync.sync_notion_pages", new=_fake_sync_notion_pages
        ),
    ):
        await _run_sync_in_isolated_session(
            source_type="notion",
            config=dict(source_config),
            kb_id=_KB_ID,
            user_id="user-id",
            bound_integration_id=None,
            statement_timeout_secs=60,
            source_id=_SOURCE_ID,
        )

    return sync_db, src_row


@pytest.mark.asyncio
async def test_scheduler_persists_updated_stale_counts() -> None:
    """When counts change during a sync, the source config row is updated."""

    def _increment(stale: dict[str, int]) -> None:
        stale[_CHILD_DB_ID] = stale.get(_CHILD_DB_ID, 0) + 1

    sync_db, src_row = await _run_isolated_notion_sync(
        source_config={"recursive": "true"},
        sync_side_effect=_increment,
    )

    assert src_row.config["stale_child_databases"] == {_CHILD_DB_ID: 1}
    sync_db.commit.assert_awaited()


@pytest.mark.asyncio
async def test_scheduler_drops_key_when_all_counts_reset() -> None:
    """When every count resets, the stale key is removed from the config."""

    def _clear(stale: dict[str, int]) -> None:
        stale.clear()

    sync_db, src_row = await _run_isolated_notion_sync(
        source_config={
            "recursive": "true",
            "stale_child_databases": {_CHILD_DB_ID: 2},
        },
        sync_side_effect=_clear,
    )

    assert "stale_child_databases" not in src_row.config
    sync_db.commit.assert_awaited()


@pytest.mark.asyncio
async def test_scheduler_skips_persistence_when_unchanged() -> None:
    """When the stale dict is untouched, no follow-up source UPDATE happens."""
    sync_db, src_row = await _run_isolated_notion_sync(
        source_config={
            "recursive": "true",
            "stale_child_databases": {_CHILD_DB_ID: 1},
        },
        sync_side_effect=None,
    )

    sync_db.get.assert_not_awaited()
    sync_db.commit.assert_not_awaited()
    assert src_row.config["stale_child_databases"] == {_CHILD_DB_ID: 1}


@pytest.mark.asyncio
async def test_scheduler_commits_when_counted_404_state_changes() -> None:
    """A counted-404 state change must be persisted and committed even without
    non-404 changes — the backstop path that covers a silently-failed
    in-sync persist."""

    def _mark_404(stale: dict) -> None:
        stale[_CHILD_DB_ID] = {
            "count": 1,
            "first_failed_at": "2026-07-13T23:58:20+00:00",
            "last_failed_at": "2026-07-13T23:58:20+00:00",
        }

    sync_db, src_row = await _run_isolated_notion_sync(
        source_config={"recursive": "true"},
        sync_404_side_effect=_mark_404,
    )

    # The scheduler re-runs the idempotent _persist_stale_child_dbs, so the
    # 404 quarantine state must land in the source config row, not just
    # trigger an empty commit.
    assert src_row.config["stale_child_dbs"][_CHILD_DB_ID]["count"] == 1
    sync_db.commit.assert_awaited()


@pytest.mark.asyncio
async def test_scheduler_skips_commit_when_counted_404_state_unchanged() -> None:
    """No 404-state change and no non-404 change -> no follow-up commit."""
    stale_404_cfg = {
        _CHILD_DB_ID: {
            "count": 2,
            "first_failed_at": "2026-07-12T11:00:00+00:00",
            "last_failed_at": "2026-07-13T11:00:00+00:00",
        }
    }
    sync_db, _ = await _run_isolated_notion_sync(
        source_config={"recursive": "true", "stale_child_dbs": stale_404_cfg},
    )

    sync_db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_scheduler_tolerates_corrupted_stale_config() -> None:
    """A non-dict stale_child_databases config value is treated as empty."""

    def _increment(stale: dict[str, int]) -> None:
        stale[_CHILD_DB_ID] = stale.get(_CHILD_DB_ID, 0) + 1

    sync_db, src_row = await _run_isolated_notion_sync(
        source_config={
            "recursive": "true",
            "stale_child_databases": "corrupted-not-a-dict",
        },
        sync_side_effect=_increment,
    )

    # The corrupted value is replaced by the freshly tracked counts.
    assert src_row.config["stale_child_databases"] == {_CHILD_DB_ID: 1}
    sync_db.commit.assert_awaited()
