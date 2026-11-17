"""Tests for the ``list_knowledge_bases`` N+1 fix.

The per-KB document status-count + latest-error queries in
``_kb_to_response`` were fired once per knowledge base, so listing N KBs
issued 2N queries on ``knowledge_documents``. Under concurrent load that
storms the table and the 30 s ``statement_timeout`` cancels the count query
(SQLSTATE 57014), 500ing ``GET /api/v1/knowledge_bases``. The fix batches
both into one grouped query each and passes the results into
``_kb_to_response`` so it fires zero per-KB queries.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.api.v1 import knowledge_bases as kb_mod
from app.services.kb_document_errors import INTERNAL_ERROR_MESSAGE


def _fake_kb(**overrides) -> SimpleNamespace:
    base = {
        "id": uuid.uuid4(),
        "user_id": uuid.uuid4(),
        "name": "Docs",
        "description": None,
        "embedding_model": "text-embedding-3-large",
        "is_system_managed": False,
        "is_builtin": False,
        "sync_sources_customized": False,
        "is_read_only": False,
        "is_company_managed": False,
        "shared_group_ids": None,
        "shared_group_permissions": None,
        "shared_user_ids": None,
        "shared_user_permissions": None,
        "access_restricted": False,
        "tags": [],
        "document_count": 5,
        "chunk_count": 10,
        "query_count": 0,
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class _ExplodingDB:
    """Any query is a test failure — proves the batched path queries nothing."""

    async def execute(self, _statement):
        raise AssertionError(
            "_kb_to_response must NOT query per-KB when batched counts + "
            "latest_error are provided (that's the N+1 the fix removes)"
        )


class _RowsResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return list(self._rows)


class _RowsDB:
    def __init__(self, rows):
        self._rows = rows
        self.execute_calls = 0

    async def execute(self, _statement):
        self.execute_calls += 1
        return _RowsResult(self._rows)


@pytest.mark.asyncio
async def test_kb_to_response_uses_precomputed_values_without_querying():
    kb = _fake_kb()
    next_retry_iso = datetime.now(UTC).isoformat()
    resp = await kb_mod._kb_to_response(
        kb,
        _ExplodingDB(),
        can_manage=True,
        is_package_origin=False,
        owner_name="Alice",
        stale_info=None,
        status_counts={"ready": 3, "pending": 1, "error": 2},
        latest_error="boom: provider down",
        next_retry=next_retry_iso,
        retryable_error_count=1,
    )

    assert resp.ready_count == 3
    assert resp.pending_count == 1
    assert resp.error_count == 2
    # Raw text an older row may still hold is served as the generic
    # sentence, never verbatim.
    assert resp.latest_error_message == INTERNAL_ERROR_MESSAGE
    assert resp.latest_error_code == "internal_error"
    assert resp.next_auto_retry_at == next_retry_iso
    assert resp.retryable_error_count == 1
    assert resp.id == str(kb.id)


@pytest.mark.asyncio
async def test_kb_to_response_precomputed_none_error_is_respected():
    """A precomputed ``latest_error=None`` means "no error doc" — it must NOT
    fall back to querying (the sentinel distinguishes it from "not provided")."""
    kb = _fake_kb()
    resp = await kb_mod._kb_to_response(
        kb,
        _ExplodingDB(),
        can_manage=True,
        is_package_origin=False,
        status_counts={},  # KB with no docs → all zeros
        latest_error=None,
        next_retry=None,
        retryable_error_count=0,
    )
    assert resp.ready_count == 0
    assert resp.pending_count == 0
    assert resp.error_count == 0
    assert resp.latest_error_message is None
    assert resp.latest_error_code is None
    assert resp.next_auto_retry_at is None
    assert resp.retryable_error_count == 0


@pytest.mark.asyncio
async def test_load_kb_status_counts_groups_one_query():
    kb1, kb2 = _fake_kb(), _fake_kb()
    rows = [
        SimpleNamespace(knowledge_base_id=kb1.id, ready=3, pending=0, error=1),
        SimpleNamespace(knowledge_base_id=kb2.id, ready=0, pending=2, error=0),
    ]
    db = _RowsDB(rows)

    counts = await kb_mod._load_kb_status_counts(db, [kb1, kb2])

    assert db.execute_calls == 1  # single grouped query, not one per KB
    assert counts[str(kb1.id)] == {"ready": 3, "pending": 0, "error": 1}
    assert counts[str(kb2.id)] == {"ready": 0, "pending": 2, "error": 0}


@pytest.mark.asyncio
async def test_load_kb_latest_errors_maps_one_query():
    kb1, kb2 = _fake_kb(), _fake_kb()
    rows = [
        SimpleNamespace(knowledge_base_id=kb1.id, error_message="kb1 failed"),
        SimpleNamespace(knowledge_base_id=kb2.id, error_message="kb2 failed"),
    ]
    db = _RowsDB(rows)

    errors = await kb_mod._load_kb_latest_errors(db, [kb1, kb2])

    assert db.execute_calls == 1
    assert errors == {str(kb1.id): "kb1 failed", str(kb2.id): "kb2 failed"}


@pytest.mark.asyncio
async def test_load_kb_next_auto_retries_maps_one_query():
    kb1, kb2 = _fake_kb(), _fake_kb()
    next_retry = datetime.now(UTC)
    rows = [
        SimpleNamespace(
            knowledge_base_id=kb1.id, next_retry=next_retry, retryable_count=3
        ),
        SimpleNamespace(
            knowledge_base_id=kb2.id, next_retry=None, retryable_count=0
        ),
    ]
    db = _RowsDB(rows)

    retries = await kb_mod._load_kb_next_auto_retries(db, [kb1, kb2])

    assert db.execute_calls == 1  # single grouped query, not one per KB
    assert retries == {str(kb1.id): (next_retry.isoformat(), 3)}


@pytest.mark.asyncio
async def test_batch_helpers_short_circuit_on_empty():
    db = _RowsDB([])
    assert await kb_mod._load_kb_status_counts(db, []) == {}
    assert await kb_mod._load_kb_latest_errors(db, []) == {}
    assert await kb_mod._load_kb_next_auto_retries(db, []) == {}
    # No query issued at all for an empty page.
    assert db.execute_calls == 0
