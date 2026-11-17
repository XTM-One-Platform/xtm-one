"""Tests for Notion per-page stale-reference quarantine.

Covers:
- sync_notion_pages: skip pages in skip_page_ids, collect 404/410 pages in
  _not_found_pages_out, non-permanent errors are NOT collected.
- _handle_notion_stale_pages: increments per-page 404 counter, quarantines
  after threshold, resets counts for pages that succeeded, no double-count
  for already-quarantined pages.
- _reset_notion_stale_page_config: clears both tracking keys from config,
  is a no-op when neither key is present.
"""

import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.services.kb_sync import sync_notion_pages
from app.services.kb_sync_scheduler import (
    _NOTION_CFG_NOT_FOUND_COUNTS,
    _NOTION_CFG_SKIPPED_PAGE_IDS,
    _NOTION_PAGE_STALE_THRESHOLD,
    _handle_notion_stale_pages,
    _preview_page_ids,
    _reset_notion_stale_page_config,
)

# ---------------------------------------------------------------------------
# Shared test constants
# ---------------------------------------------------------------------------

_PAGE_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
_PAGE_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
_PAGE_C = "cccccccc-cccc-cccc-cccc-cccccccccccc"
_SOURCE_ID = "11111111-1111-1111-1111-111111111111"
_KB_ID = uuid.uuid4()


# ---------------------------------------------------------------------------
# _handle_notion_stale_pages — unit tests (no I/O)
# ---------------------------------------------------------------------------


def _make_source(config: dict | None = None):
    """Minimal KBSyncSource-like SimpleNamespace for unit tests."""
    ns = SimpleNamespace()
    ns.config = config or {}
    return ns


def test_handle_stale_pages_increments_count_below_threshold():
    """Counts increment but no page is quarantined when below threshold."""
    source = _make_source()
    newly = _handle_notion_stale_pages(
        source,
        [_PAGE_A],
        base_config={},
        kb_id=_KB_ID,
        source_id=_SOURCE_ID,
    )
    counts = source.config[_NOTION_CFG_NOT_FOUND_COUNTS]
    assert counts[_PAGE_A] == 1
    assert _NOTION_CFG_SKIPPED_PAGE_IDS not in source.config
    assert newly == []


def test_handle_stale_pages_quarantines_at_threshold():
    """Page is added to skipped list once it reaches the threshold."""
    source = _make_source(
        {_NOTION_CFG_NOT_FOUND_COUNTS: {_PAGE_A: _NOTION_PAGE_STALE_THRESHOLD - 1}}
    )
    newly = _handle_notion_stale_pages(
        source,
        [_PAGE_A],
        base_config=source.config,
        kb_id=_KB_ID,
        source_id=_SOURCE_ID,
    )
    skipped = source.config[_NOTION_CFG_SKIPPED_PAGE_IDS]
    assert _PAGE_A in skipped
    # Newly quarantined pages are reported back to the caller for alerting.
    assert newly == [_PAGE_A]
    # Count should still be stored (or at least page should be in skipped)
    assert source.config.get(_NOTION_CFG_NOT_FOUND_COUNTS, {}).get(_PAGE_A) == _NOTION_PAGE_STALE_THRESHOLD


def test_handle_stale_pages_resets_count_for_succeeding_page():
    """Counter is reset to 0 for a page that is NOT in not_found_page_ids."""
    source = _make_source(
        {_NOTION_CFG_NOT_FOUND_COUNTS: {_PAGE_A: 2, _PAGE_B: 1}}
    )
    # Only PAGE_B fails this run; PAGE_A succeeded.
    _handle_notion_stale_pages(
        source,
        [_PAGE_B],
        base_config=source.config,
        kb_id=_KB_ID,
        source_id=_SOURCE_ID,
    )
    counts = source.config.get(_NOTION_CFG_NOT_FOUND_COUNTS) or {}
    # PAGE_A succeeded → counter removed (reset)
    assert _PAGE_A not in counts
    # PAGE_B still failing → count incremented
    assert counts[_PAGE_B] == 2


def test_handle_stale_pages_does_not_double_count_quarantined():
    """Already-quarantined pages are not incremented or re-reported."""
    source = _make_source(
        {
            _NOTION_CFG_NOT_FOUND_COUNTS: {_PAGE_A: _NOTION_PAGE_STALE_THRESHOLD},
            _NOTION_CFG_SKIPPED_PAGE_IDS: [_PAGE_A],
        }
    )
    newly = _handle_notion_stale_pages(
        source,
        [_PAGE_A],
        base_config=source.config,
        kb_id=_KB_ID,
        source_id=_SOURCE_ID,
    )
    # Counter should NOT be incremented further
    counts = source.config.get(_NOTION_CFG_NOT_FOUND_COUNTS) or {}
    assert counts.get(_PAGE_A, 0) == _NOTION_PAGE_STALE_THRESHOLD
    # Still in skipped list, but not reported as NEWLY quarantined again
    assert _PAGE_A in (source.config.get(_NOTION_CFG_SKIPPED_PAGE_IDS) or [])
    assert newly == []


def test_handle_stale_pages_multiple_pages_mixed():
    """Multiple pages: one quarantines, one increments, one resets."""
    source = _make_source(
        {
            _NOTION_CFG_NOT_FOUND_COUNTS: {
                _PAGE_A: _NOTION_PAGE_STALE_THRESHOLD - 1,
                _PAGE_C: 1,
            }
        }
    )
    # PAGE_A: one more 404 → reaches threshold → quarantined
    # PAGE_B: first 404 → count = 1
    # PAGE_C: succeeds → counter reset (removed)
    _handle_notion_stale_pages(
        source,
        [_PAGE_A, _PAGE_B],
        base_config=source.config,
        kb_id=_KB_ID,
        source_id=_SOURCE_ID,
    )
    skipped = source.config.get(_NOTION_CFG_SKIPPED_PAGE_IDS) or []
    counts = source.config.get(_NOTION_CFG_NOT_FOUND_COUNTS) or {}

    assert _PAGE_A in skipped
    assert _PAGE_B not in skipped
    assert counts.get(_PAGE_B) == 1
    assert _PAGE_C not in counts  # reset because it didn't fail


def test_handle_stale_pages_tolerates_corrupted_config_types():
    """Corrupted (wrong-type) tracking values fall back to empty state
    instead of raising and failing an otherwise successful sync."""
    source = _make_source(
        {
            _NOTION_CFG_NOT_FOUND_COUNTS: ["not", "a", "dict"],
            _NOTION_CFG_SKIPPED_PAGE_IDS: "not-a-list",
        }
    )
    newly = _handle_notion_stale_pages(
        source,
        [_PAGE_A],
        base_config=source.config,
        kb_id=_KB_ID,
        source_id=_SOURCE_ID,
    )
    # Tracking restarts from a clean slate: PAGE_A counts as its first 404.
    assert source.config[_NOTION_CFG_NOT_FOUND_COUNTS] == {_PAGE_A: 1}
    assert newly == []


def test_handle_stale_pages_tolerates_corrupted_config_items():
    """Item-level corruption (non-int counter values, non-string skip
    entries) is dropped instead of raising on the success path — a bad
    counter restarts from scratch, valid entries are preserved."""
    source = _make_source(
        {
            _NOTION_CFG_NOT_FOUND_COUNTS: {_PAGE_A: "2", _PAGE_B: None, _PAGE_C: 1},
            _NOTION_CFG_SKIPPED_PAGE_IDS: [_PAGE_B, {"bad": "entry"}, 42],
        }
    )
    _handle_notion_stale_pages(
        source,
        [_PAGE_A, _PAGE_C],
        base_config=source.config,
        kb_id=_KB_ID,
        source_id=_SOURCE_ID,
    )
    counts = source.config[_NOTION_CFG_NOT_FOUND_COUNTS]
    # Corrupted "2" was dropped -> PAGE_A restarts at 1; valid PAGE_C counts on.
    assert counts[_PAGE_A] == 1
    assert counts[_PAGE_C] == 2
    # Non-string skip entries were dropped; the valid one survives sorted().
    assert source.config[_NOTION_CFG_SKIPPED_PAGE_IDS] == [_PAGE_B]


def test_handle_stale_pages_clears_counts_when_all_succeed():
    """When no pages fail, all counts are reset and the keys are removed."""
    source = _make_source(
        {_NOTION_CFG_NOT_FOUND_COUNTS: {_PAGE_A: 1, _PAGE_B: 2}}
    )
    _handle_notion_stale_pages(
        source,
        [],  # no failures this run
        base_config=source.config,
        kb_id=_KB_ID,
        source_id=_SOURCE_ID,
    )
    assert _NOTION_CFG_NOT_FOUND_COUNTS not in source.config
    assert _NOTION_CFG_SKIPPED_PAGE_IDS not in source.config


def test_preview_page_ids_caps_long_lists():
    """Log/alert previews stay bounded with a (+N more) suffix; short lists
    are joined in full without a suffix."""
    short = ["a", "b"]
    assert _preview_page_ids(short) == "a, b"
    long = [f"id-{i}" for i in range(12)]
    preview = _preview_page_ids(long, limit=5)
    assert preview == "id-0, id-1, id-2, id-3, id-4 (+7 more)"


# ---------------------------------------------------------------------------
# _reset_notion_stale_page_config — unit tests
# ---------------------------------------------------------------------------


def test_reset_notion_stale_page_config_clears_both_keys():
    """Removes both tracking keys from config and reassigns dict."""
    source = _make_source(
        {
            _NOTION_CFG_NOT_FOUND_COUNTS: {_PAGE_A: 2},
            _NOTION_CFG_SKIPPED_PAGE_IDS: [_PAGE_A],
            "other_key": "preserved",
        }
    )
    # Simulate what SQLAlchemy does: store config in __dict__ (already done by _make_source)
    _reset_notion_stale_page_config(source)
    assert _NOTION_CFG_NOT_FOUND_COUNTS not in source.config
    assert _NOTION_CFG_SKIPPED_PAGE_IDS not in source.config
    assert source.config.get("other_key") == "preserved"


def test_reset_notion_stale_page_config_no_op_when_keys_absent():
    """Does not touch config (or ORM object) when there is nothing to clear."""
    original_config = {"repo": "my-repo", "branch": "main"}
    source = _make_source(original_config.copy())
    _reset_notion_stale_page_config(source)
    # config should be unchanged (function is a no-op)
    assert source.config == original_config


def test_reset_notion_stale_page_config_works_with_empty_config():
    """Does not raise when config is empty / None."""
    source = SimpleNamespace()
    source.config = {}
    source.__dict__["config"] = {}
    _reset_notion_stale_page_config(source)  # should not raise


# ---------------------------------------------------------------------------
# sync_notion_pages — integration-lite tests (mocked HTTP)
# ---------------------------------------------------------------------------


class _AsyncNullContext:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _make_db_and_kb():
    kb = SimpleNamespace(
        id=uuid.uuid4(),
        name="Test KB",
        document_count=0,
        chunk_count=0,
    )

    # Plain (non-async) callable: production code does
    # ``async with db.begin_nested():`` — SQLAlchemy's begin_nested() is a
    # sync call returning an async context manager, so the mock must too.
    def _begin_nested():
        return _AsyncNullContext()

    db = SimpleNamespace(
        begin_nested=_begin_nested,
        execute=AsyncMock(return_value=SimpleNamespace(
            scalar_one_or_none=lambda: None,
            scalars=lambda: SimpleNamespace(all=lambda: []),
            rowcount=0,
        )),
        commit=AsyncMock(),
        flush=AsyncMock(),
    )
    return db, kb


@pytest.mark.asyncio
async def test_sync_notion_pages_skips_quarantined_page(monkeypatch):
    """Pages in skip_page_ids are silently skipped — no HTTP call is made."""
    # Patch the token fetch so we don't need real DB
    monkeypatch.setattr(
        "app.services.kb_sync._get_integration_token",
        AsyncMock(return_value="test-token"),
    )
    # Patch _upsert_document so there's no real DB insertion
    monkeypatch.setattr(
        "app.services.kb_sync._upsert_document",
        AsyncMock(return_value=(None, False)),
    )
    # Patch _delete_stale_documents and _recount_kb_stats
    monkeypatch.setattr("app.services.kb_sync._delete_stale_documents", AsyncMock(return_value=0))
    monkeypatch.setattr("app.services.kb_sync._recount_kb_stats", AsyncMock())
    monkeypatch.setattr("app.services.kb_sync.enqueue_documents", AsyncMock())

    db, kb = _make_db_and_kb()

    http_calls: list[str] = []

    async def _fake_request(client_self, client, method, url, **kwargs):
        http_calls.append(url)
        # This should never be reached for the skipped page
        raise AssertionError(f"Unexpected HTTP call: {method} {url}")

    with patch("app.services.kb_sync._NotionRequestController.request", new=_fake_request):
        await sync_notion_pages(
            db,
            kb,
            "user-1",
            page_ids=[_PAGE_A],
            skip_page_ids=frozenset([_PAGE_A]),
        )

    assert http_calls == [], "No HTTP requests should be made for a skipped page"


def _patch_sync_internals(monkeypatch):
    """Patch the DB-facing internals of sync_notion_pages for mocked-HTTP tests.

    ``_upsert_document`` returns a real doc-like object so successfully
    synced pages land in ``all_doc_ids`` — required since main's
    explicit-page guard re-raises when an explicit sync produced
    NOTHING; the per-page quarantine path only runs on mixed runs where at
    least one page succeeded.
    """
    monkeypatch.setattr(
        "app.services.kb_sync._get_integration_token",
        AsyncMock(return_value="test-token"),
    )
    monkeypatch.setattr(
        "app.services.kb_sync._upsert_document",
        AsyncMock(return_value=(SimpleNamespace(id=uuid.uuid4()), False)),
    )
    monkeypatch.setattr("app.services.kb_sync._delete_stale_documents", AsyncMock(return_value=0))
    monkeypatch.setattr("app.services.kb_sync._recount_kb_stats", AsyncMock())
    monkeypatch.setattr("app.services.kb_sync.enqueue_documents", AsyncMock())


def _make_mixed_request(ok_page_id: str, fail_page_id: str, fail_status: int):
    """Fake _NotionRequestController.request: one healthy page, one failing.

    The healthy page returns a valid page object and an empty block list;
    the failing page raises an HTTPStatusError with *fail_status*.
    """

    async def _fake_request(client_self, client, method, url, **kwargs):
        req = httpx.Request(method, url)
        if f"/pages/{fail_page_id}" in url:
            resp = httpx.Response(fail_status, request=req)
            raise httpx.HTTPStatusError(str(fail_status), request=req, response=resp)
        if f"/pages/{ok_page_id}" in url:
            return httpx.Response(
                200,
                json={
                    "object": "page",
                    "id": ok_page_id,
                    "properties": {},
                    "url": f"https://notion.so/{ok_page_id.replace('-', '')}",
                },
                request=req,
            )
        if "/blocks/" in url:
            return httpx.Response(
                200, json={"results": [], "has_more": False}, request=req
            )
        raise AssertionError(f"Unexpected HTTP call: {method} {url}")

    return _fake_request


@pytest.mark.asyncio
async def test_sync_notion_pages_collects_404_in_out_list(monkeypatch):
    """A page returning HTTP 404 is appended to _not_found_pages_out."""
    _patch_sync_internals(monkeypatch)
    db, kb = _make_db_and_kb()
    not_found: list[str] = []

    fake = _make_mixed_request(_PAGE_B, _PAGE_A, 404)
    with patch("app.services.kb_sync._NotionRequestController.request", new=fake):
        await sync_notion_pages(
            db,
            kb,
            "user-1",
            page_ids=[_PAGE_B, _PAGE_A],
            _not_found_pages_out=not_found,
        )

    assert not_found == [_PAGE_A]


@pytest.mark.asyncio
async def test_sync_notion_pages_collects_410_in_out_list(monkeypatch):
    """A page returning HTTP 410 Gone is also appended to _not_found_pages_out."""
    _patch_sync_internals(monkeypatch)
    db, kb = _make_db_and_kb()
    not_found: list[str] = []

    fake = _make_mixed_request(_PAGE_B, _PAGE_A, 410)
    with patch("app.services.kb_sync._NotionRequestController.request", new=fake):
        await sync_notion_pages(
            db,
            kb,
            "user-1",
            page_ids=[_PAGE_B, _PAGE_A],
            _not_found_pages_out=not_found,
        )

    assert not_found == [_PAGE_A]


@pytest.mark.asyncio
async def test_sync_notion_pages_does_not_collect_non_permanent_errors(monkeypatch):
    """Transient errors (5xx, connection errors) are NOT added to _not_found_pages_out."""
    _patch_sync_internals(monkeypatch)
    db, kb = _make_db_and_kb()
    not_found: list[str] = []

    fake = _make_mixed_request(_PAGE_B, _PAGE_A, 503)
    with patch("app.services.kb_sync._NotionRequestController.request", new=fake):
        await sync_notion_pages(
            db,
            kb,
            "user-1",
            page_ids=[_PAGE_B, _PAGE_A],
            _not_found_pages_out=not_found,
        )

    # 503 is transient — must NOT be in not_found list
    assert not_found == []


@pytest.mark.asyncio
async def test_sync_notion_pages_does_not_collect_when_out_not_provided(monkeypatch):
    """When _not_found_pages_out is None, 404s are just logged (no collection)."""
    _patch_sync_internals(monkeypatch)
    db, kb = _make_db_and_kb()

    fake = _make_mixed_request(_PAGE_B, _PAGE_A, 404)
    # Should complete without error even though one page 404s and the caller
    # did not provide an out-list.
    with patch("app.services.kb_sync._NotionRequestController.request", new=fake):
        result = await sync_notion_pages(
            db,
            kb,
            "user-1",
            page_ids=[_PAGE_B, _PAGE_A],
            # _not_found_pages_out not provided (default None)
        )

    assert len(result) == 1  # the healthy page synced; no exception raised


@pytest.mark.asyncio
async def test_sync_notion_pages_nested_block_404_not_collected(monkeypatch):
    """A 404 on a NESTED block-children request (deleted child block) fails
    the page for this run but must NOT count toward the page's quarantine -
    the page itself still exists."""
    _patch_sync_internals(monkeypatch)
    db, kb = _make_db_and_kb()
    not_found: list[str] = []
    nested_block_id = "99999999-9999-9999-9999-999999999999"

    async def _fake_request(client_self, client, method, url, **kwargs):
        req = httpx.Request(method, url)
        if f"/pages/{_PAGE_A}" in url or f"/pages/{_PAGE_B}" in url:
            page = _PAGE_A if _PAGE_A in url else _PAGE_B
            return httpx.Response(
                200,
                json={"object": "page", "id": page, "properties": {},
                      "url": f"https://notion.so/{page.replace('-', '')}"},
                request=req,
            )
        if f"/blocks/{_PAGE_A}/children" in url:
            # Page A's top-level blocks include a nested container block.
            return httpx.Response(
                200,
                json={
                    "results": [
                        {"id": nested_block_id, "type": "toggle",
                         "has_children": True, "toggle": {"rich_text": []}}
                    ],
                    "has_more": False,
                },
                request=req,
            )
        if f"/blocks/{nested_block_id}/children" in url:
            # The nested block vanished mid-crawl -> 404 NOT about the page.
            resp = httpx.Response(404, request=req)
            raise httpx.HTTPStatusError("404 Not Found", request=req, response=resp)
        if f"/blocks/{_PAGE_B}/children" in url:
            return httpx.Response(
                200, json={"results": [], "has_more": False}, request=req
            )
        raise AssertionError(f"Unexpected HTTP call: {method} {url}")

    with patch("app.services.kb_sync._NotionRequestController.request", new=_fake_request):
        await sync_notion_pages(
            db,
            kb,
            "user-1",
            page_ids=[_PAGE_B, _PAGE_A],
            _not_found_pages_out=not_found,
        )

    assert not_found == [], (
        f"A nested block 404 must not quarantine the page, got: {not_found}"
    )


@pytest.mark.asyncio
async def test_sync_notion_pages_all_explicit_404_raises_and_still_collects(monkeypatch):
    """When EVERY explicit page 404s, the explicit-page guard
    re-raises so the run is recorded as a failure — but the out-list is
    still populated before the raise (the scheduler calls
    _handle_notion_stale_pages in the failure path so the consecutive-404
    count accumulates even on total failures)."""
    _patch_sync_internals(monkeypatch)
    db, kb = _make_db_and_kb()
    not_found: list[str] = []

    async def _fake_request(client_self, client, method, url, **kwargs):
        req = httpx.Request(method, url)
        resp = httpx.Response(404, request=req)
        raise httpx.HTTPStatusError("404 Not Found", request=req, response=resp)

    with (
        patch("app.services.kb_sync._NotionRequestController.request", new=_fake_request),
        pytest.raises(httpx.HTTPStatusError),
    ):
        await sync_notion_pages(
            db,
            kb,
            "user-1",
            page_ids=[_PAGE_A],
            _not_found_pages_out=not_found,
        )

    assert _PAGE_A in not_found


# ---------------------------------------------------------------------------
# execute_sync_source — scheduler wiring (counter reset + owner alert)
# ---------------------------------------------------------------------------


async def _noop_heartbeat(source_id):
    await asyncio.sleep(3600)  # cancelled by execute_sync_source's finally


def _make_scheduler_source(config: dict):
    """SimpleNamespace source with every attribute the success path touches."""
    return SimpleNamespace(
        id=uuid.uuid4(),
        source_type="notion",
        config=config,
        integration_id=None,
        user_id=None,
        last_sync_status=None,
        last_synced_at=None,
        last_sync_error=None,
        last_sync_documents=0,
        sync_failure_count=0,
        sync_stale_count=0,
        sync_perm_fail_count=0,
        sync_next_retry_at=None,
        sync_heartbeat_at=None,
        enabled=True,
    )


@pytest.mark.asyncio
async def test_execute_sync_source_clean_run_resets_page_counters(monkeypatch):
    """A successful sync with NO 404s must clear leftover per-page counters,
    preserving the 'consecutive' contract — a page that recovered (or left
    scope) must not keep an old count that a much-later 404 would resume."""
    from app.services import kb_sync_scheduler

    db = SimpleNamespace(flush=AsyncMock(), commit=AsyncMock(), rollback=AsyncMock())
    source = _make_scheduler_source(
        {
            "page_ids": [_PAGE_A],
            _NOTION_CFG_NOT_FOUND_COUNTS: {_PAGE_A: _NOTION_PAGE_STALE_THRESHOLD - 1},
        }
    )
    kb = SimpleNamespace(id=uuid.uuid4(), name="KB")

    monkeypatch.setattr(
        kb_sync_scheduler,
        "_run_sync_in_isolated_session",
        # (doc_ids, page_error_count) tuple; clean run: out-list stays empty
        AsyncMock(return_value=(["doc-1"], 0)),
    )
    monkeypatch.setattr(kb_sync_scheduler, "_run_sync_heartbeat", _noop_heartbeat)
    monkeypatch.setattr("app.services.kb_helpers.log_kb_activity", AsyncMock())

    await kb_sync_scheduler.execute_sync_source(db, source, kb, "system")

    assert source.last_sync_status == "success"
    assert _NOTION_CFG_NOT_FOUND_COUNTS not in source.config
    assert source.config.get("page_ids") == [_PAGE_A]  # rest of config intact


@pytest.mark.asyncio
async def test_execute_sync_source_emits_alert_on_page_quarantine(monkeypatch):
    """Crossing the threshold quarantines the page AND surfaces an
    owner-visible sync_alert (a visible status,
    not just a log line)."""
    from app.services import kb_sync_scheduler

    db = SimpleNamespace(flush=AsyncMock(), commit=AsyncMock(), rollback=AsyncMock())
    source = _make_scheduler_source(
        {
            "page_ids": [_PAGE_A],
            _NOTION_CFG_NOT_FOUND_COUNTS: {_PAGE_A: _NOTION_PAGE_STALE_THRESHOLD - 1},
        }
    )
    kb = SimpleNamespace(id=uuid.uuid4(), name="KB")

    async def _fake_run(**kwargs):
        kwargs["_notion_not_found_out"].append(_PAGE_A)
        return [], 1  # (doc_ids, page_error_count)

    alert = AsyncMock()
    monkeypatch.setattr(kb_sync_scheduler, "_run_sync_in_isolated_session", _fake_run)
    monkeypatch.setattr(kb_sync_scheduler, "_run_sync_heartbeat", _noop_heartbeat)
    monkeypatch.setattr(kb_sync_scheduler, "_emit_stale_page_quarantine_alert", alert)
    monkeypatch.setattr("app.services.kb_helpers.log_kb_activity", AsyncMock())

    await kb_sync_scheduler.execute_sync_source(db, source, kb, "system")

    assert _PAGE_A in (source.config.get(_NOTION_CFG_SKIPPED_PAGE_IDS) or [])
    alert.assert_awaited_once()
    assert alert.await_args.kwargs["page_ids"] == [_PAGE_A]


# ---------------------------------------------------------------------------
# execute_sync_source failure path — stale tracking on total failures
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_sync_source_tracks_404_on_total_failure(monkeypatch):
    """When the sync raises (total failure) and _notion_not_found is non-empty,
    the failure path must still call _handle_notion_stale_pages so the
    consecutive-404 counter accumulates.

    The per-page count should increment on a failing run even though the
    overall sync never produced any documents.
    """
    from app.services import kb_sync_scheduler

    db = SimpleNamespace(
        flush=AsyncMock(), commit=AsyncMock(), rollback=AsyncMock(),
        refresh=AsyncMock(),
    )
    source = _make_scheduler_source({"page_ids": [_PAGE_A]})
    kb = SimpleNamespace(id=uuid.uuid4(), name="KB")

    async def _fake_run(**kwargs):
        # Populate the out-list just like sync_notion_pages does, then raise.
        kwargs["_notion_not_found_out"].append(_PAGE_A)
        raise RuntimeError("total sync failure")

    monkeypatch.setattr(kb_sync_scheduler, "_run_sync_in_isolated_session", _fake_run)
    monkeypatch.setattr(kb_sync_scheduler, "_run_sync_heartbeat", _noop_heartbeat)
    monkeypatch.setattr("app.services.kb_helpers.log_kb_activity", AsyncMock())

    with pytest.raises(RuntimeError, match="total sync failure"):
        await kb_sync_scheduler.execute_sync_source(db, source, kb, "system")

    # Counter must have been incremented even though the run failed.
    counts = source.config.get(_NOTION_CFG_NOT_FOUND_COUNTS) or {}
    assert counts.get(_PAGE_A) == 1, (
        f"Expected page_not_found_counts[{_PAGE_A!r}] == 1, got config={source.config!r}"
    )


@pytest.mark.asyncio
async def test_execute_sync_source_quarantines_page_on_total_failure_at_threshold(
    monkeypatch,
):
    """When total-failure runs accumulate to the threshold, the stale page
    must be quarantined (added to skipped_stale_page_ids) and an owner alert
    emitted — same as the partial-failure path."""
    from app.services import kb_sync_scheduler

    db = SimpleNamespace(
        flush=AsyncMock(), commit=AsyncMock(), rollback=AsyncMock(),
        refresh=AsyncMock(),
    )
    source = _make_scheduler_source(
        {
            "page_ids": [_PAGE_A],
            # Already at threshold-1 from prior failing runs.
            _NOTION_CFG_NOT_FOUND_COUNTS: {_PAGE_A: _NOTION_PAGE_STALE_THRESHOLD - 1},
        }
    )
    kb = SimpleNamespace(id=uuid.uuid4(), name="KB")

    async def _fake_run(**kwargs):
        kwargs["_notion_not_found_out"].append(_PAGE_A)
        raise RuntimeError("total sync failure")

    alert = AsyncMock()
    monkeypatch.setattr(kb_sync_scheduler, "_run_sync_in_isolated_session", _fake_run)
    monkeypatch.setattr(kb_sync_scheduler, "_run_sync_heartbeat", _noop_heartbeat)
    monkeypatch.setattr(kb_sync_scheduler, "_emit_stale_page_quarantine_alert", alert)
    monkeypatch.setattr("app.services.kb_helpers.log_kb_activity", AsyncMock())

    with pytest.raises(RuntimeError):
        await kb_sync_scheduler.execute_sync_source(db, source, kb, "system")

    # Page must now be quarantined.
    assert _PAGE_A in (source.config.get(_NOTION_CFG_SKIPPED_PAGE_IDS) or []), (
        f"Expected {_PAGE_A!r} in skipped list, got config={source.config!r}"
    )
    # Owner-visible alert must have fired once.
    alert.assert_awaited_once()
    assert alert.await_args.kwargs["page_ids"] == [_PAGE_A]


@pytest.mark.asyncio
async def test_execute_sync_source_stale_tracking_error_does_not_mask_failure(
    monkeypatch,
):
    """The failure-path stale tracking is best-effort: if
    _handle_notion_stale_pages raises (e.g. unexpected state), the original
    sync exception must still propagate and _handle_sync_failure must still
    persist the failure status — the source must not stay stuck in
    'syncing'."""
    from app.services import kb_sync_scheduler

    db = SimpleNamespace(
        flush=AsyncMock(), commit=AsyncMock(), rollback=AsyncMock(),
        refresh=AsyncMock(),
    )
    source = _make_scheduler_source({"page_ids": [_PAGE_A]})
    kb = SimpleNamespace(id=uuid.uuid4(), name="KB")

    async def _fake_run(**kwargs):
        kwargs["_notion_not_found_out"].append(_PAGE_A)
        raise RuntimeError("total sync failure")

    def _boom(*args, **kwargs):
        raise ValueError("tracking blew up")

    monkeypatch.setattr(kb_sync_scheduler, "_run_sync_in_isolated_session", _fake_run)
    monkeypatch.setattr(kb_sync_scheduler, "_run_sync_heartbeat", _noop_heartbeat)
    monkeypatch.setattr(kb_sync_scheduler, "_handle_notion_stale_pages", _boom)
    monkeypatch.setattr("app.services.kb_helpers.log_kb_activity", AsyncMock())

    # The ORIGINAL sync error propagates — not the tracking ValueError.
    with pytest.raises(RuntimeError, match="total sync failure"):
        await kb_sync_scheduler.execute_sync_source(db, source, kb, "system")

    # Failure status was still persisted by _handle_sync_failure.
    assert source.last_sync_status == "error"
    assert source.sync_failure_count == 1


@pytest.mark.asyncio
async def test_execute_sync_source_no_stale_tracking_on_non_notion_failure(
    monkeypatch,
):
    """Stale page tracking is Notion-specific; a non-notion source that fails
    must not attempt to call _handle_notion_stale_pages."""
    from app.services import kb_sync_scheduler

    db = SimpleNamespace(
        flush=AsyncMock(), commit=AsyncMock(), rollback=AsyncMock(),
        refresh=AsyncMock(),
    )
    source = SimpleNamespace(
        id=uuid.uuid4(),
        source_type="github",
        config={},
        integration_id=None,
        user_id=None,
        last_sync_status=None,
        last_synced_at=None,
        last_sync_error=None,
        last_sync_documents=0,
        sync_failure_count=0,
        sync_stale_count=0,
        sync_perm_fail_count=0,
        sync_partial_fail_count=0,
        sync_next_retry_at=None,
        sync_heartbeat_at=None,
        enabled=True,
    )
    kb = SimpleNamespace(id=uuid.uuid4(), name="KB")

    handle_stale = AsyncMock()
    monkeypatch.setattr(
        kb_sync_scheduler,
        "_run_sync_in_isolated_session",
        AsyncMock(side_effect=RuntimeError("github failure")),
    )
    monkeypatch.setattr(kb_sync_scheduler, "_run_sync_heartbeat", _noop_heartbeat)
    monkeypatch.setattr(kb_sync_scheduler, "_handle_notion_stale_pages", handle_stale)
    monkeypatch.setattr("app.services.kb_helpers.log_kb_activity", AsyncMock())

    with pytest.raises(RuntimeError):
        await kb_sync_scheduler.execute_sync_source(db, source, kb, "system")

    handle_stale.assert_not_called()


@pytest.mark.asyncio
async def test_execute_sync_source_failure_respects_refreshed_empty_config(
    monkeypatch,
):
    """The failure-path base_config selection must use a None-check, not
    truthiness: when db.refresh loads a legitimately EMPTY config dict from
    the DB, the tracking must build on that empty dict — not resurrect the
    stale pre-sync snapshot and its dead keys."""
    from app.services import kb_sync_scheduler

    source = _make_scheduler_source({"page_ids": [_PAGE_A], "dead_key": "stale"})
    kb = SimpleNamespace(id=uuid.uuid4(), name="KB")

    async def _fake_refresh(obj, attribute_names=None):
        # Simulate the DB row's config having been emptied while the sync ran.
        obj.config = {}

    db = SimpleNamespace(
        flush=AsyncMock(), commit=AsyncMock(), rollback=AsyncMock(),
        refresh=AsyncMock(side_effect=_fake_refresh),
    )

    async def _fake_run(**kwargs):
        kwargs["_notion_not_found_out"].append(_PAGE_A)
        raise RuntimeError("total sync failure")

    monkeypatch.setattr(kb_sync_scheduler, "_run_sync_in_isolated_session", _fake_run)
    monkeypatch.setattr(kb_sync_scheduler, "_run_sync_heartbeat", _noop_heartbeat)
    monkeypatch.setattr("app.services.kb_helpers.log_kb_activity", AsyncMock())

    with pytest.raises(RuntimeError, match="total sync failure"):
        await kb_sync_scheduler.execute_sync_source(db, source, kb, "system")

    # Tracking built on the refreshed empty config: counter present, but the
    # pre-sync snapshot's keys were NOT resurrected.
    counts = source.config.get(_NOTION_CFG_NOT_FOUND_COUNTS) or {}
    assert counts.get(_PAGE_A) == 1
    assert "dead_key" not in source.config
    assert "page_ids" not in source.config


@pytest.mark.asyncio
async def test_execute_sync_source_no_quarantine_alert_when_persistence_fails(
    monkeypatch,
):
    """When _handle_sync_failure's internal flush fails (rolled back), the
    stale-page quarantine alert must NOT be emitted — the config mutation was
    reverted, so alerting would notify about a quarantine that never landed.
    The next tick re-runs the tracking and
    re-emits on a clean pass."""
    from app.services import kb_sync_scheduler

    # First flush (sync start) succeeds; the flush inside
    # _handle_sync_failure fails, triggering its internal rollback path.
    db = SimpleNamespace(
        flush=AsyncMock(side_effect=[None, RuntimeError("db down")]),
        commit=AsyncMock(), rollback=AsyncMock(), refresh=AsyncMock(),
    )
    source = _make_scheduler_source(
        {
            "page_ids": [_PAGE_A],
            _NOTION_CFG_NOT_FOUND_COUNTS: {_PAGE_A: _NOTION_PAGE_STALE_THRESHOLD - 1},
        }
    )
    kb = SimpleNamespace(id=uuid.uuid4(), name="KB")

    async def _fake_run(**kwargs):
        kwargs["_notion_not_found_out"].append(_PAGE_A)
        raise RuntimeError("total sync failure")

    alert = AsyncMock()
    monkeypatch.setattr(kb_sync_scheduler, "_run_sync_in_isolated_session", _fake_run)
    monkeypatch.setattr(kb_sync_scheduler, "_run_sync_heartbeat", _noop_heartbeat)
    monkeypatch.setattr(kb_sync_scheduler, "_emit_stale_page_quarantine_alert", alert)
    monkeypatch.setattr("app.services.kb_helpers.log_kb_activity", AsyncMock())

    with pytest.raises(RuntimeError, match="total sync failure"):
        await kb_sync_scheduler.execute_sync_source(db, source, kb, "system")

    # The quarantine crossed the threshold in memory, but persistence failed
    # and was rolled back — no owner-visible alert may fire.
    alert.assert_not_called()


# ---------------------------------------------------------------------------
# _is_notion_permanent_page_sync_error — blocks-level 404
# ---------------------------------------------------------------------------


def test_is_permanent_error_matches_blocks_children_404_with_page_id():
    """A 404 from GET /v1/blocks/{page_id}/children must be treated as a
    permanent page-level error when page_id is provided."""
    from app.services.kb_sync import _is_notion_permanent_page_sync_error

    page_id = "3378fce1-7f2a-8136-b43b-d5e83d884045"
    url = f"https://api.notion.com/v1/blocks/{page_id}/children?page_size=100"
    req = httpx.Request("GET", url)
    resp = httpx.Response(404, request=req)
    exc = httpx.HTTPStatusError("404 Not Found", request=req, response=resp)

    assert _is_notion_permanent_page_sync_error(exc, page_id=page_id) is True


def test_is_permanent_error_ignores_nested_block_404_different_id():
    """A 404 from a NESTED block (different ID from the page) must NOT be
    treated as a permanent page error."""
    from app.services.kb_sync import _is_notion_permanent_page_sync_error

    page_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    nested_block_id = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    url = f"https://api.notion.com/v1/blocks/{nested_block_id}/children?page_size=100"
    req = httpx.Request("GET", url)
    resp = httpx.Response(404, request=req)
    exc = httpx.HTTPStatusError("404 Not Found", request=req, response=resp)

    assert _is_notion_permanent_page_sync_error(exc, page_id=page_id) is False


def test_is_permanent_error_ignores_blocks_children_404_without_page_id():
    """Without page_id, a 404 from /v1/blocks/ must NOT match (no page context
    to validate against — legacy call sites that don't pass page_id)."""
    from app.services.kb_sync import _is_notion_permanent_page_sync_error

    page_id = "3378fce1-7f2a-8136-b43b-d5e83d884045"
    url = f"https://api.notion.com/v1/blocks/{page_id}/children?page_size=100"
    req = httpx.Request("GET", url)
    resp = httpx.Response(404, request=req)
    exc = httpx.HTTPStatusError("404 Not Found", request=req, response=resp)

    # Called without page_id (e.g. from an old call site) — must return False.
    assert _is_notion_permanent_page_sync_error(exc) is False


def test_is_permanent_error_ignores_blocks_children_400():
    """A 400 from the blocks children endpoint must NOT be treated as a
    permanent page error — unlike /v1/pages/, a blocks 400 signals a
    request-level problem (bad cursor, malformed body), not a stale page."""
    from app.services.kb_sync import _is_notion_permanent_page_sync_error

    page_id = "3378fce1-7f2a-8136-b43b-d5e83d884045"
    url = f"https://api.notion.com/v1/blocks/{page_id}/children?page_size=100"
    req = httpx.Request("GET", url)
    resp = httpx.Response(400, request=req)
    exc = httpx.HTTPStatusError("400 Bad Request", request=req, response=resp)

    assert _is_notion_permanent_page_sync_error(exc, page_id=page_id) is False


def test_is_permanent_error_ignores_non_children_blocks_path():
    """A 404 from a blocks endpoint other than /children (e.g. the block
    metadata endpoint /v1/blocks/{id}) must NOT match — only the top-level
    children fetch identifies the page's own content as inaccessible."""
    from app.services.kb_sync import _is_notion_permanent_page_sync_error

    page_id = "3378fce1-7f2a-8136-b43b-d5e83d884045"
    url = f"https://api.notion.com/v1/blocks/{page_id}"
    req = httpx.Request("GET", url)
    resp = httpx.Response(404, request=req)
    exc = httpx.HTTPStatusError("404 Not Found", request=req, response=resp)

    assert _is_notion_permanent_page_sync_error(exc, page_id=page_id) is False


def test_is_permanent_error_still_matches_pages_path():
    """/v1/pages/{id} 404 must still return True (regression check)."""
    from app.services.kb_sync import _is_notion_permanent_page_sync_error

    page_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    url = f"https://api.notion.com/v1/pages/{page_id}"
    req = httpx.Request("GET", url)
    resp = httpx.Response(404, request=req)
    exc = httpx.HTTPStatusError("404 Not Found", request=req, response=resp)

    assert _is_notion_permanent_page_sync_error(exc, page_id=page_id) is True
    assert _is_notion_permanent_page_sync_error(exc) is True  # without page_id too
