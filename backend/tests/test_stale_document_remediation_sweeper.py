"""Tests for the stale-embedding auto-remediation sweeper in ``kb_helpers``.

This is the third self-healing layer for KB document embeddings: documents that
are ``status = 'ready'`` but whose stored vectors no longer match the platform
embedding target (after an admin changed the embedding provider / model /
dimensions) are automatically re-embedded with the current model — the same
work the dashboard "Re-embed all" button does, but autonomously and
batch-capped.

Covers ``recover_stale_documents``:
  * re-embeds a stale ``ready`` doc (reset → pending, vectors kept until replaced, enqueue);
  * short-circuits when the feature is disabled;
  * skips when the platform embedding target is not fully configured;
  * leaves docs untouched (and releases row locks) when embedding is unusable;
  * uses ``FOR UPDATE SKIP LOCKED``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.models.knowledge_base import KnowledgeDocument
from app.services import kb_helpers


class _FakeSettings:
    """Deterministic stale-remediation settings."""

    embedding_stale_auto_remediation_enabled = True
    embedding_stale_remediation_batch_size = 100


@pytest.fixture
def fake_settings(monkeypatch):
    settings = _FakeSettings()
    monkeypatch.setattr(kb_helpers, "app_settings", settings)
    return settings


@pytest.fixture
def configured_platform(monkeypatch):
    """Patch the platform-settings loader with a fully-configured target."""
    monkeypatch.setattr(
        "app.api.v1.platform_settings_helpers.get_platform_settings",
        AsyncMock(
            return_value=SimpleNamespace(
                embedding_model="text-embedding-3-large",
                embedding_dimensions=1536,
            )
        ),
    )


class _FakeSession:
    """Minimal async-session fake returning a fixed batch of docs."""

    def __init__(self, docs):
        self._docs = list(docs)
        self.commits = 0
        self.rollbacks = 0
        self.executed_statements: list = []

    class _Result:
        def __init__(self, docs):
            self._docs = docs

        def scalars(self):
            return self

        def all(self):
            return list(self._docs)

    async def execute(self, statement):
        self.executed_statements.append(statement)
        return self._Result(self._docs)

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


def _make_stale_ready_doc() -> KnowledgeDocument:
    return KnowledgeDocument(
        id=uuid.uuid4(),
        knowledge_base_id=uuid.uuid4(),
        filename="stale.md",
        source_type="upload",
        content="x" * 64,
        status="ready",
        chunk_count=5,
        embedding_retry_count=0,
        updated_at=datetime.now(UTC) - timedelta(minutes=30),
    )


@pytest.mark.asyncio
async def test_recover_stale_reembeds_ready_doc(
    fake_settings, configured_platform, monkeypatch
):
    doc = _make_stale_ready_doc()
    session = _FakeSession([doc])
    enqueue_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(kb_helpers, "enqueue_document_processing", enqueue_mock)
    monkeypatch.setattr(
        kb_helpers,
        "_resolve_embedding_enqueue_state",
        AsyncMock(return_value=(True, None)),
    )

    remediated = await kb_helpers.recover_stale_documents(session)

    assert remediated == 1
    # Reset to pending with a fresh budget so the worker re-embeds it.
    assert doc.status == "pending"
    assert doc.embedding_retry_count == 0
    assert doc.embedding_next_retry_at is None
    enqueue_mock.assert_awaited_once_with(str(doc.id), embedding_ready=True)
    # One SELECT and no purge: the stale vectors stay searchable until the
    # processor swaps them. Then a single commit.
    assert len(session.executed_statements) == 1
    assert doc.chunk_count == 5
    assert session.commits == 1
    assert session.rollbacks == 0


@pytest.mark.asyncio
async def test_recover_stale_disabled_short_circuits(fake_settings, monkeypatch):
    fake_settings.embedding_stale_auto_remediation_enabled = False
    session = _FakeSession([_make_stale_ready_doc()])
    enqueue_mock = AsyncMock()
    resolver = AsyncMock()
    monkeypatch.setattr(kb_helpers, "enqueue_document_processing", enqueue_mock)
    monkeypatch.setattr(kb_helpers, "_resolve_embedding_enqueue_state", resolver)

    remediated = await kb_helpers.recover_stale_documents(session)

    assert remediated == 0
    enqueue_mock.assert_not_awaited()
    resolver.assert_not_awaited()
    assert session.executed_statements == []
    assert session.commits == 0


@pytest.mark.asyncio
async def test_recover_stale_skips_when_platform_unconfigured(
    fake_settings, monkeypatch
):
    """When the platform embedding target is not fully configured (no model or
    no dimension), stale cannot be evaluated — bail before touching docs."""
    monkeypatch.setattr(
        "app.api.v1.platform_settings_helpers.get_platform_settings",
        AsyncMock(
            return_value=SimpleNamespace(
                embedding_model=None,
                embedding_dimensions=None,
            )
        ),
    )
    session = _FakeSession([_make_stale_ready_doc()])
    enqueue_mock = AsyncMock()
    resolver = AsyncMock()
    monkeypatch.setattr(kb_helpers, "enqueue_document_processing", enqueue_mock)
    monkeypatch.setattr(kb_helpers, "_resolve_embedding_enqueue_state", resolver)

    remediated = await kb_helpers.recover_stale_documents(session)

    assert remediated == 0
    enqueue_mock.assert_not_awaited()
    resolver.assert_not_awaited()
    # No query is issued (we bail right after the platform-settings probe).
    assert session.executed_statements == []
    assert session.commits == 0


@pytest.mark.asyncio
async def test_recover_stale_skips_when_embedding_unconfigured(
    fake_settings, configured_platform, monkeypatch, caplog
):
    """The target is configured but no usable embedding provider exists right
    now — leave the docs ``ready`` and release the row locks via rollback."""
    doc = _make_stale_ready_doc()
    session = _FakeSession([doc])
    enqueue_mock = AsyncMock()
    monkeypatch.setattr(kb_helpers, "enqueue_document_processing", enqueue_mock)
    monkeypatch.setattr(
        kb_helpers,
        "_resolve_embedding_enqueue_state",
        AsyncMock(return_value=(False, "no embedding provider configured")),
    )

    with caplog.at_level("INFO"):
        remediated = await kb_helpers.recover_stale_documents(session)

    assert remediated == 0
    enqueue_mock.assert_not_awaited()
    # Untouched — still ready, no purge committed.
    assert doc.status == "ready"
    assert session.commits == 0
    assert session.rollbacks == 1, (
        "must release FOR UPDATE SKIP LOCKED row locks before returning"
    )
    assert "Stale-document remediation skipped" in caplog.text


@pytest.mark.asyncio
async def test_recover_stale_empty_returns_zero(
    fake_settings, configured_platform, monkeypatch
):
    session = _FakeSession([])
    enqueue_mock = AsyncMock()
    resolver = AsyncMock()
    monkeypatch.setattr(kb_helpers, "enqueue_document_processing", enqueue_mock)
    monkeypatch.setattr(kb_helpers, "_resolve_embedding_enqueue_state", resolver)

    remediated = await kb_helpers.recover_stale_documents(session)

    assert remediated == 0
    enqueue_mock.assert_not_awaited()
    # Nothing due → must not probe the embedding provider config.
    resolver.assert_not_awaited()
    assert session.commits == 0


@pytest.mark.asyncio
async def test_recover_stale_uses_for_update_skip_locked(
    fake_settings, configured_platform, monkeypatch
):
    doc = _make_stale_ready_doc()
    session = _FakeSession([doc])
    monkeypatch.setattr(
        kb_helpers, "enqueue_document_processing", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(
        kb_helpers,
        "_resolve_embedding_enqueue_state",
        AsyncMock(return_value=(True, None)),
    )

    await kb_helpers.recover_stale_documents(session)

    from sqlalchemy.dialects import postgresql

    compiled = str(
        session.executed_statements[0].compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    ).upper()
    assert "FOR UPDATE" in compiled
    assert "SKIP LOCKED" in compiled
