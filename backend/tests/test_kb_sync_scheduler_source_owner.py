"""The scheduler hands every sync the source it runs for.

A sync records its source as the owner of the documents it imports and sweeps
only those (``kb_sync.owned_sync_documents``); a sync called without it would
own only the unowned documents and leave its own stale documents behind. The
database side is ``test_kb_sync_source_ownership_postgres.py``.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.kb_sync_scheduler import _run_sync_in_isolated_session

_SOURCE_ID = str(uuid.uuid4())

# source type -> (sync function, the keyword it takes the source id under, config)
_SYNCS = {
    "github": ("sync_github_repo", "source_id", {"repo": "owner/repo"}),
    "github_public": ("sync_github_repo", "source_id", {"repo": "owner/repo"}),
    "notion": ("sync_notion_pages", "sync_source_id", {"page_id": "a46d129bef1e48e6bc9862176e82622e"}),
    "microsoft-drive": ("sync_microsoft_drive", "sync_source_id", {"folder_path": "/"}),
    "google-drive": ("sync_google_drive", "sync_source_id", {"folder_id": "root"}),
    "drata": ("sync_drata_policies", "sync_source_id", {"statuses": "ACTIVE"}),
    "website": ("sync_website", "sync_source_id", {"url": "https://docs.example.com"}),
}


def _session_factory():
    kb_result = MagicMock()
    kb_result.scalar_one_or_none.return_value = SimpleNamespace(id=uuid.uuid4(), name="KB")
    sync_db = MagicMock()
    sync_db.execute = AsyncMock(return_value=kb_result)
    sync_db.commit = AsyncMock()

    class _Session:
        async def __aenter__(self):
            return sync_db

        async def __aexit__(self, *_exc):
            return False

    return _Session


@pytest.mark.asyncio
@pytest.mark.parametrize("source_type", sorted(_SYNCS))
async def test_the_scheduler_passes_the_source_to_the_sync(source_type):
    function, keyword, config = _SYNCS[source_type]
    sync = AsyncMock(return_value=[])
    with (
        patch("app.core.database.async_session", _session_factory()),
        patch("app.services.kb_sync._apply_sync_statement_timeout", new=AsyncMock()),
        patch("app.services.kb_sync.sync_egress_route", new=AsyncMock(return_value=None)),
        patch(f"app.services.kb_sync.{function}", new=sync),
    ):
        doc_ids, _errors = await _run_sync_in_isolated_session(
            source_type=source_type,
            config=config,
            kb_id=uuid.uuid4(),
            user_id=str(uuid.uuid4()),
            bound_integration_id=None,
            statement_timeout_secs=60,
            source_id=_SOURCE_ID,
        )

    assert doc_ids == []
    assert sync.await_count == 1
    assert sync.await_args.kwargs[keyword] == _SOURCE_ID
