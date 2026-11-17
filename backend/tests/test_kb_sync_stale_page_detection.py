"""Tests for KB sync stale-page detection and partial-failure tracking.

Covers:
- ``sync_notion_pages`` raises when ALL queued pages fail (so the scheduler's
  existing quarantine logic kicks in instead of recording a spurious success).
- ``sync_notion_pages`` skips ``_delete_stale_documents`` on a total failure
  to avoid wiping existing KB content when every remote page is inaccessible.
- ``sync_notion_pages`` appends to ``_error_collector`` when provided.
- ``_run_sync_in_isolated_session`` returns a ``(doc_ids, page_error_count)``
  tuple so the caller can track partial failures across runs.
- ``_execute_sync_source_inner`` increments ``sync_partial_fail_count`` when
  some pages fail within an otherwise-successful sync, and emits a one-shot
  alert when the threshold is first crossed.
- ``sync_partial_fail_count`` resets to 0 on a clean sync (no page errors).
"""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from app.services import kb_sync_scheduler

# ---------------------------------------------------------------------------
# Helpers shared by multiple tests
# ---------------------------------------------------------------------------


def _make_source(**overrides):
    """Return a minimal KBSyncSource-like SimpleNamespace."""
    defaults = dict(
        id=uuid.uuid4(),
        knowledge_base_id=uuid.uuid4(),
        source_type="notion",
        config={"page_id": "a46d129b-ef1e-48e6-bc98-62176e82622e"},
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
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


async def _noop_heartbeat(_source_id):
    """Heartbeat stub that completes immediately."""


# ---------------------------------------------------------------------------
# sync_notion_pages — total failure raises, skips deletes, populates collector
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sync_notion_pages_raises_when_all_pages_fail(monkeypatch):
    """When every page in the queue fails, ``sync_notion_pages`` must raise so
    the scheduler's failure-count and quarantine logic fires instead of
    recording a spurious success with 0 documents.  In explicit page mode the
    original ``httpx.HTTPStatusError`` is re-raised (preferred by the
    permanent-failure classifier); non-explicit modes raise a
    ``RuntimeError`` (see the search-mode test below)."""

    kb = SimpleNamespace(id=uuid.uuid4(), name="Test KB")

    # Build a fake DB that supports begin_nested() as an async ctx manager.
    class _FakeNested:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    db = SimpleNamespace(commit=AsyncMock(), begin_nested=_FakeNested)

    # Every page fetch raises 404.
    def _raise_404(client, method, url, **kwargs):
        raise httpx.HTTPStatusError(
            "404 Not Found",
            request=httpx.Request(method, url),
            response=httpx.Response(404, request=httpx.Request(method, url)),
        )

    class _FailingController:
        async def request(self, client, method, url, **kwargs):
            _raise_404(client, method, url)

    # Minimal fake httpx.AsyncClient context manager.
    class _FakeClientCtx:
        async def __aenter__(self):
            return MagicMock()  # client object; not used directly

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(
        "app.services.kb_sync._get_integration_token",
        AsyncMock(return_value="fake-token"),
    )
    monkeypatch.setattr(
        "app.services.kb_sync._NotionRequestController",
        lambda: _FailingController(),
    )
    monkeypatch.setattr(
        "app.services.kb_sync._recount_kb_stats",
        AsyncMock(),
    )
    monkeypatch.setattr(
        "app.services.kb_sync._delete_stale_documents",
        AsyncMock(return_value=0),
    )
    monkeypatch.setattr(
        "app.services.kb_sync.enqueue_documents",
        AsyncMock(),
    )
    monkeypatch.setattr(
        "httpx.AsyncClient",
        lambda **kw: _FakeClientCtx(),
    )

    from app.services.kb_sync import sync_notion_pages

    with pytest.raises(httpx.HTTPStatusError, match="404"):
        await sync_notion_pages(
            db,
            kb,
            "user-1",
            page_id="a46d129b-ef1e-48e6-bc98-62176e82622e",
        )


@pytest.mark.asyncio
async def test_sync_notion_pages_does_not_delete_stale_docs_on_total_failure(monkeypatch):
    """When ALL pages fail, ``_delete_stale_documents`` must NOT be called.
    Calling it with an empty set would wipe every existing Notion doc in the KB.
    """

    class _FakeNested:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    db = SimpleNamespace(commit=AsyncMock(), begin_nested=_FakeNested)
    kb = SimpleNamespace(id=uuid.uuid4(), name="Test KB")

    class _FailingController:
        async def request(self, client, method, url, **kwargs):
            raise httpx.HTTPStatusError(
                "404 Not Found",
                request=httpx.Request(method, url),
                response=httpx.Response(404, request=httpx.Request(method, url)),
            )

    class _FakeClientCtx:
        async def __aenter__(self):
            return MagicMock()

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(
        "app.services.kb_sync._get_integration_token",
        AsyncMock(return_value="fake-token"),
    )
    monkeypatch.setattr(
        "app.services.kb_sync._NotionRequestController",
        lambda: _FailingController(),
    )
    monkeypatch.setattr("app.services.kb_sync._recount_kb_stats", AsyncMock())
    delete_stale = AsyncMock(return_value=0)
    monkeypatch.setattr("app.services.kb_sync._delete_stale_documents", delete_stale)
    monkeypatch.setattr("app.services.kb_sync.enqueue_documents", AsyncMock())
    monkeypatch.setattr("httpx.AsyncClient", lambda **kw: _FakeClientCtx())

    from app.services.kb_sync import sync_notion_pages

    with pytest.raises(httpx.HTTPStatusError):
        await sync_notion_pages(
            db,
            kb,
            "user-1",
            page_id="a46d129b-ef1e-48e6-bc98-62176e82622e",
        )

    delete_stale.assert_not_called()


@pytest.mark.asyncio
async def test_sync_notion_pages_error_collector_populated_on_failure(monkeypatch):
    """``_error_collector``, when provided, must receive every failing page's
    ``(page_id, error_message)`` tuple."""

    class _FakeNested:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    db = SimpleNamespace(commit=AsyncMock(), begin_nested=_FakeNested)
    kb = SimpleNamespace(id=uuid.uuid4(), name="Test KB")

    class _FailingController:
        async def request(self, client, method, url, **kwargs):
            raise httpx.HTTPStatusError(
                "404 Not Found",
                request=httpx.Request(method, url),
                response=httpx.Response(404, request=httpx.Request(method, url)),
            )

    class _FakeClientCtx:
        async def __aenter__(self):
            return MagicMock()

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(
        "app.services.kb_sync._get_integration_token",
        AsyncMock(return_value="fake-token"),
    )
    monkeypatch.setattr(
        "app.services.kb_sync._NotionRequestController",
        lambda: _FailingController(),
    )
    monkeypatch.setattr("app.services.kb_sync._recount_kb_stats", AsyncMock())
    monkeypatch.setattr(
        "app.services.kb_sync._delete_stale_documents", AsyncMock(return_value=0)
    )
    monkeypatch.setattr("app.services.kb_sync.enqueue_documents", AsyncMock())
    monkeypatch.setattr("httpx.AsyncClient", lambda **kw: _FakeClientCtx())

    errors: list[tuple[str, str]] = []

    from app.services.kb_sync import sync_notion_pages

    with pytest.raises(httpx.HTTPStatusError):
        await sync_notion_pages(
            db,
            kb,
            "user-1",
            page_id="a46d129b-ef1e-48e6-bc98-62176e82622e",
            _error_collector=errors,
        )

    assert len(errors) == 1
    failed_id, err_msg = errors[0]
    # The page ID should be present (with or without dashes, after _extract_notion_id).
    assert "a46d129b" in failed_id
    # Error message must reference the HTTP error.
    assert "404" in err_msg


@pytest.mark.asyncio
async def test_sync_notion_pages_search_mode_raises_total_failure_error(
    monkeypatch,
):
    """Non-explicit modes (search) with a total failure must raise a
    ``NotionSyncTotalFailureError`` whose message mentions the number of
    failed pages and includes a snippet of the original error for operator
    debugging.  (Explicit page modes re-raise the original httpx error
    instead — see above.)"""

    class _FakeNested:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    db = SimpleNamespace(commit=AsyncMock(), begin_nested=_FakeNested)
    kb = SimpleNamespace(id=uuid.uuid4(), name="Test KB")

    class _SearchOkPagesFailController:
        """Succeed on /search (returning one page), fail every page fetch."""

        async def request(self, client, method, url, **kwargs):
            if "/search" in url:
                resp = MagicMock()
                resp.raise_for_status = MagicMock()
                resp.json = MagicMock(
                    return_value={
                        "results": [{"id": "f4f73ed4-665d-4724-9764-1f2bdd8fdc29"}],
                        "has_more": False,
                    }
                )
                return resp
            raise httpx.HTTPStatusError(
                "400 Bad Request",
                request=httpx.Request(method, url),
                response=httpx.Response(400, request=httpx.Request(method, url)),
            )

    class _FakeClientCtx:
        async def __aenter__(self):
            return MagicMock()

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(
        "app.services.kb_sync._get_integration_token",
        AsyncMock(return_value="fake-token"),
    )
    monkeypatch.setattr(
        "app.services.kb_sync._NotionRequestController",
        lambda: _SearchOkPagesFailController(),
    )
    monkeypatch.setattr("app.services.kb_sync._recount_kb_stats", AsyncMock())
    delete_stale = AsyncMock(return_value=0)
    monkeypatch.setattr("app.services.kb_sync._delete_stale_documents", delete_stale)
    monkeypatch.setattr("app.services.kb_sync.enqueue_documents", AsyncMock())
    monkeypatch.setattr("httpx.AsyncClient", lambda **kw: _FakeClientCtx())

    from app.services.kb_sync import NotionSyncTotalFailureError, sync_notion_pages

    with pytest.raises(NotionSyncTotalFailureError) as exc_info:
        await sync_notion_pages(
            db,
            kb,
            "user-1",
            search_query="anything",
        )

    msg = str(exc_info.value)
    # Must say how many pages failed and carry the underlying error snippet.
    assert "All 1 Notion page" in msg
    assert "400" in msg
    # The delete-stale guard also applies on the search-mode total failure.
    delete_stale.assert_not_called()


# ---------------------------------------------------------------------------
# Partial-failure tracking in execute_sync_source
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_sync_source_increments_partial_fail_count(monkeypatch):
    """When the isolated-session sync returns page errors (partial failure),
    ``sync_partial_fail_count`` must increment and the source remains enabled."""
    db = SimpleNamespace(flush=AsyncMock(), commit=AsyncMock(), rollback=AsyncMock())
    source = _make_source(sync_partial_fail_count=0)
    kb = SimpleNamespace(id=uuid.uuid4(), name="Test KB")

    monkeypatch.setattr(kb_sync_scheduler, "_run_sync_heartbeat", _noop_heartbeat)
    # Return 1 doc but 2 page errors (partial failure).
    monkeypatch.setattr(
        kb_sync_scheduler,
        "_run_sync_in_isolated_session",
        AsyncMock(return_value=(["doc-1"], 2)),
    )
    monkeypatch.setattr("app.services.kb_helpers.log_kb_activity", AsyncMock())
    monkeypatch.setattr(
        kb_sync_scheduler.settings, "kb_sync_partial_fail_alert_threshold", 3
    )

    await kb_sync_scheduler.execute_sync_source(db, source, kb, "system")

    assert source.last_sync_status == "success"
    assert source.sync_partial_fail_count == 1  # incremented from 0 → 1
    assert source.enabled is True  # source stays enabled on partial failure


@pytest.mark.asyncio
async def test_execute_sync_source_resets_partial_fail_count_on_clean_sync(monkeypatch):
    """A clean sync (zero page errors) must reset ``sync_partial_fail_count``
    to 0 so earlier accumulated partial failures don't persist."""
    db = SimpleNamespace(flush=AsyncMock(), commit=AsyncMock(), rollback=AsyncMock())
    source = _make_source(sync_partial_fail_count=2)  # had 2 partial runs before
    kb = SimpleNamespace(id=uuid.uuid4(), name="Test KB")

    monkeypatch.setattr(kb_sync_scheduler, "_run_sync_heartbeat", _noop_heartbeat)
    # Clean run — 0 page errors.
    monkeypatch.setattr(
        kb_sync_scheduler,
        "_run_sync_in_isolated_session",
        AsyncMock(return_value=(["doc-1"], 0)),
    )
    monkeypatch.setattr("app.services.kb_helpers.log_kb_activity", AsyncMock())

    await kb_sync_scheduler.execute_sync_source(db, source, kb, "system")

    assert source.last_sync_status == "success"
    assert source.sync_partial_fail_count == 0  # reset on clean sync


@pytest.mark.asyncio
async def test_execute_sync_source_resets_partial_fail_count_on_full_failure(
    monkeypatch,
):
    """A FULL sync failure interrupts the consecutive-partial-failure streak:
    ``sync_partial_fail_count`` must reset to 0 in the failure path so only
    uninterrupted partial-success runs accumulate toward the alert."""
    db = SimpleNamespace(flush=AsyncMock(), commit=AsyncMock(), rollback=AsyncMock())
    source = _make_source(sync_partial_fail_count=2)  # streak in progress
    kb = SimpleNamespace(id=uuid.uuid4(), name="Test KB")

    monkeypatch.setattr(kb_sync_scheduler, "_run_sync_heartbeat", _noop_heartbeat)
    monkeypatch.setattr(
        kb_sync_scheduler,
        "_run_sync_in_isolated_session",
        AsyncMock(side_effect=RuntimeError("total sync failure")),
    )
    monkeypatch.setattr("app.services.kb_helpers.log_kb_activity", AsyncMock())

    with pytest.raises(RuntimeError, match="total sync failure"):
        await kb_sync_scheduler.execute_sync_source(db, source, kb, "system")

    assert source.last_sync_status == "error"
    assert source.sync_partial_fail_count == 0  # streak interrupted


@pytest.mark.asyncio
async def test_execute_sync_source_no_partial_alert_below_threshold(monkeypatch):
    """Below the threshold, no ``sync_alert`` must be emitted even when page
    errors are present — the alert fires only on the crossing event."""
    db = SimpleNamespace(flush=AsyncMock(), commit=AsyncMock(), rollback=AsyncMock())
    source = _make_source(sync_partial_fail_count=0)
    kb = SimpleNamespace(id=uuid.uuid4(), name="Test KB")

    monkeypatch.setattr(kb_sync_scheduler, "_run_sync_heartbeat", _noop_heartbeat)
    monkeypatch.setattr(
        kb_sync_scheduler,
        "_run_sync_in_isolated_session",
        AsyncMock(return_value=(["doc-1"], 1)),  # 1 page error
    )
    log_activity = AsyncMock()
    monkeypatch.setattr("app.services.kb_helpers.log_kb_activity", log_activity)
    monkeypatch.setattr(
        kb_sync_scheduler.settings, "kb_sync_partial_fail_alert_threshold", 3
    )

    await kb_sync_scheduler.execute_sync_source(db, source, kb, "system")

    # Counter went 0 → 1, still below threshold of 3 — no sync_alert.
    sync_alert_calls = [
        c for c in log_activity.await_args_list
        if len(c.args) >= 3 and c.args[2] == "sync_alert"
    ]
    assert len(sync_alert_calls) == 0, "must not emit sync_alert below threshold"


@pytest.mark.asyncio
async def test_execute_sync_source_alert_fires_only_on_crossing_not_above(monkeypatch):
    """Once the threshold is already exceeded, a further run above the threshold
    must NOT emit a second alert — the alert fires only on the below→at/above
    transition."""
    db = SimpleNamespace(flush=AsyncMock(), commit=AsyncMock(), rollback=AsyncMock())
    source = _make_source(sync_partial_fail_count=5)  # already above threshold of 3
    kb = SimpleNamespace(id=uuid.uuid4(), name="Test KB")

    emit_alert = AsyncMock()
    monkeypatch.setattr(kb_sync_scheduler, "_run_sync_heartbeat", _noop_heartbeat)
    monkeypatch.setattr(
        kb_sync_scheduler,
        "_run_sync_in_isolated_session",
        AsyncMock(return_value=(["doc-1"], 1)),
    )
    monkeypatch.setattr("app.services.kb_helpers.log_kb_activity", AsyncMock())
    monkeypatch.setattr(
        kb_sync_scheduler.settings, "kb_sync_partial_fail_alert_threshold", 3
    )
    # Patch the alert emitter to confirm it is NOT called.
    monkeypatch.setattr(kb_sync_scheduler, "_emit_partial_fail_alert", emit_alert)

    await kb_sync_scheduler.execute_sync_source(db, source, kb, "system")

    emit_alert.assert_not_called()


@pytest.mark.asyncio
async def test_execute_sync_source_zero_threshold_disables_partial_alert(monkeypatch):
    """Setting ``kb_sync_partial_fail_alert_threshold`` to 0 must disable the
    partial-failure alert entirely, regardless of page error count."""
    db = SimpleNamespace(flush=AsyncMock(), commit=AsyncMock(), rollback=AsyncMock())
    source = _make_source(sync_partial_fail_count=100)
    kb = SimpleNamespace(id=uuid.uuid4(), name="Test KB")

    emit_alert = AsyncMock()
    monkeypatch.setattr(kb_sync_scheduler, "_run_sync_heartbeat", _noop_heartbeat)
    monkeypatch.setattr(
        kb_sync_scheduler,
        "_run_sync_in_isolated_session",
        AsyncMock(return_value=(["doc-1"], 5)),
    )
    monkeypatch.setattr("app.services.kb_helpers.log_kb_activity", AsyncMock())
    monkeypatch.setattr(
        kb_sync_scheduler.settings, "kb_sync_partial_fail_alert_threshold", 0
    )
    monkeypatch.setattr(kb_sync_scheduler, "_emit_partial_fail_alert", emit_alert)

    await kb_sync_scheduler.execute_sync_source(db, source, kb, "system")

    emit_alert.assert_not_called()


@pytest.mark.asyncio
async def test_execute_sync_source_emits_partial_fail_alert_at_threshold(monkeypatch):
    """When ``sync_partial_fail_count`` first reaches the threshold, the
    ``_emit_partial_fail_alert`` helper must be called exactly once."""
    db = SimpleNamespace(flush=AsyncMock(), commit=AsyncMock(), rollback=AsyncMock())
    source = _make_source(sync_partial_fail_count=2)  # 3rd run will cross threshold 3
    kb = SimpleNamespace(id=uuid.uuid4(), name="Test KB")

    emit_alert = AsyncMock()
    monkeypatch.setattr(kb_sync_scheduler, "_run_sync_heartbeat", _noop_heartbeat)
    monkeypatch.setattr(
        kb_sync_scheduler,
        "_run_sync_in_isolated_session",
        AsyncMock(return_value=(["doc-1"], 1)),  # 1 page error
    )
    monkeypatch.setattr("app.services.kb_helpers.log_kb_activity", AsyncMock())
    monkeypatch.setattr(
        kb_sync_scheduler.settings, "kb_sync_partial_fail_alert_threshold", 3
    )
    monkeypatch.setattr(kb_sync_scheduler, "_emit_partial_fail_alert", emit_alert)

    await kb_sync_scheduler.execute_sync_source(db, source, kb, "system")

    assert source.sync_partial_fail_count == 3
    emit_alert.assert_awaited_once()
    # Confirm the call received the correct partial_fail_count.
    call_kwargs = emit_alert.await_args.kwargs
    assert call_kwargs.get("partial_fail_count") == 3


# ---------------------------------------------------------------------------
# Config settings
# ---------------------------------------------------------------------------


def test_partial_fail_alert_threshold_default():
    """The default threshold for partial-failure alerts must be 3."""
    from app.core.config import Settings

    assert Settings().kb_sync_partial_fail_alert_threshold == 3


def test_partial_fail_alert_threshold_zero_disables():
    """Setting the threshold to 0 must be accepted (disables the alert)."""
    from app.core.config import Settings

    s = Settings(kb_sync_partial_fail_alert_threshold=0)
    assert s.kb_sync_partial_fail_alert_threshold == 0
