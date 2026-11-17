"""Force Re-sync recovery of auto-disabled KB sync sources.

The scheduler auto-disables a sync source in three situations, each writing a
distinct ``last_sync_error`` prefix:

- quarantine by a watchdog — repeated stale resets or a
  persistent error count — both writing
  ``QUARANTINE_ERROR_PREFIX``,
- quarantine after consecutive permanent 404/410 errors
  (``PERM_FAIL_ERROR_PREFIX``), and
- a permanent configuration error (``CONFIG_ERROR_PREFIX``).

Force Re-sync is the documented recovery path for all of them: it must
re-enable a source whose error message carries one of those prefixes, while a
source an operator disabled manually (no prefix) must stay disabled. These
tests call the endpoint function directly with the repo's fake-session
convention (the backend suite has no real-DB harness).
"""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.api.v1 import knowledge_bases as kb_api
from app.services.kb_sync_scheduler import (
    CONFIG_ERROR_PREFIX,
    PERM_FAIL_ERROR_PREFIX,
    QUARANTINE_ERROR_PREFIX,
)


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _RowsResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)


class _SeqDB:
    """Async session stub returning queued results in ``execute`` order."""

    def __init__(self, results):
        self._results = list(results)

    async def execute(self, stmt, *args, **kwargs):
        return self._results.pop(0)

    async def flush(self):
        return None

    async def commit(self):
        return None


def _make_source(
    last_sync_error: str | None, config: dict | None = None
) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        source_type="microsoft-drive",
        enabled=False,
        last_sync_status="error",
        last_sync_error=last_sync_error,
        last_sync_documents=0,
        sync_stale_count=3,
        sync_failure_count=2,
        sync_next_retry_at=None,
        config=config or {},
    )


async def _run_force_resync(monkeypatch, source):
    kb = SimpleNamespace(
        id=uuid.uuid4(), name="KB", is_builtin=False, document_count=0, chunk_count=0
    )
    user = SimpleNamespace(id=uuid.uuid4(), is_admin=True, role="admin")

    monkeypatch.setattr(
        kb_api, "get_user_knowledge_base", AsyncMock(return_value=kb)
    )
    monkeypatch.setattr(kb_api, "assert_knowledge_base_not_package_origin", AsyncMock())
    monkeypatch.setattr(kb_api, "_assert_sync_sources_licensed", AsyncMock())
    monkeypatch.setattr(kb_api, "_assert_kb_manageable", AsyncMock())
    monkeypatch.setattr(kb_api, "log_kb_activity", AsyncMock())
    monkeypatch.setattr(kb_api, "_spawn_sync_task", lambda coro: coro.close())

    # execute #1: source lookup; execute #2: doc rows (none synced yet)
    db = _SeqDB([_ScalarResult(source), _RowsResult([])])
    return await kb_api.force_resync_source(
        str(kb.id), str(source.id), user=user, db=db
    )


@pytest.mark.asyncio
async def test_force_resync_reenables_config_error_disabled_source(monkeypatch):
    """A source auto-disabled by a permanent config error must
    be re-enabled by Force Re-sync so the user can recover after fixing the
    source settings (the next sync re-disables it if the config is still
    broken)."""
    source = _make_source(f"{CONFIG_ERROR_PREFIX} sharing link used as site_id")

    response = await _run_force_resync(monkeypatch, source)

    assert source.enabled is True, "config-error disable must be cleared"
    assert source.last_sync_error is None
    assert source.sync_failure_count == 0
    assert "source re-enabled" in response["message"]


@pytest.mark.asyncio
async def test_force_resync_reenables_quarantined_source(monkeypatch):
    """A source quarantined by the stale-reset watchdog must be
    re-enabled by Force Re-sync."""
    source = _make_source(f"{QUARANTINE_ERROR_PREFIX} 3 consecutive stale resets")

    response = await _run_force_resync(monkeypatch, source)

    assert source.enabled is True, "quarantine must be cleared"
    assert "source re-enabled" in response["message"]


@pytest.mark.asyncio
async def test_force_resync_reenables_perm_fail_quarantined_source(monkeypatch):
    """A source quarantined after consecutive permanent 404/410 errors
    must be re-enabled by Force Re-sync."""
    source = _make_source(
        f"{PERM_FAIL_ERROR_PREFIX} 5 consecutive 'not found' errors"
    )

    response = await _run_force_resync(monkeypatch, source)

    assert source.enabled is True, "perm-fail quarantine must be cleared"
    assert "source re-enabled" in response["message"]


@pytest.mark.asyncio
async def test_force_resync_keeps_manually_disabled_source_disabled(monkeypatch):
    """A source an operator disabled manually (no auto-disable prefix) must
    stay disabled — Force Re-sync still runs one sync for it but must not
    silently flip the operator's decision."""
    source = _make_source("some earlier error")

    response = await _run_force_resync(monkeypatch, source)

    assert source.enabled is False, "manual disable must be preserved"
    assert "source re-enabled" not in response["message"]


@pytest.mark.asyncio
async def test_force_resync_clears_skipped_child_db_ids(monkeypatch):
    """Force Re-sync must clear the stale Notion child-database skip list so
    a re-shared / restored child database is re-attempted by the fresh sync
    (the documented recovery path for the self-healing 404 skip)."""
    source = _make_source(
        None,
        config={
            "page_id": "abc123",
            "recursive": "true",
            "skipped_child_db_ids": [str(uuid.uuid4()), str(uuid.uuid4())],
        },
    )

    await _run_force_resync(monkeypatch, source)

    assert "skipped_child_db_ids" not in source.config, (
        "force re-sync must clear the stale child-database skip list"
    )
    # The rest of the source configuration must be preserved untouched.
    assert source.config["page_id"] == "abc123"
    assert source.config["recursive"] == "true"


@pytest.mark.asyncio
async def test_force_resync_clears_stale_child_dbs_tracking(monkeypatch):
    """Force Re-sync must also clear the counted stale_child_dbs tracking
    dict so previously quarantined child databases are
    re-attempted immediately by the fresh sync."""
    source = _make_source(
        None,
        config={
            "page_id": "abc123",
            "recursive": "true",
            "stale_child_dbs": {
                str(uuid.uuid4()): {
                    "count": 3,
                    "first_failed_at": "2026-07-01T00:00:00+00:00",
                    "last_failed_at": "2026-07-12T00:00:00+00:00",
                }
            },
        },
    )

    await _run_force_resync(monkeypatch, source)

    assert "stale_child_dbs" not in source.config, (
        "force re-sync must clear the counted stale child-database tracking"
    )
    assert source.config["page_id"] == "abc123"


@pytest.mark.asyncio
async def test_force_resync_leaves_config_untouched_when_no_skip_list(monkeypatch):
    """When no skip list is present the config dict must not be reassigned."""
    original_config = {"page_id": "abc123"}
    source = _make_source(None, config=original_config)

    await _run_force_resync(monkeypatch, source)

    assert source.config is original_config, (
        "config must not be rewritten when there is nothing to clear"
    )
