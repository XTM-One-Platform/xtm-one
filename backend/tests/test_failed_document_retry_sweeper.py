"""Tests for the failed-document auto-retry path in ``kb_helpers``.

This is the self-healing layer for KB document embeddings: documents that
landed in ``status = 'error'`` (typically because the embedding provider was
misconfigured or down) are automatically re-embedded once the provider is
fixed, with exponential backoff + jitter and no manual reprocess.

Covers:
  * ``schedule_embedding_retry`` — when a retry is scheduled vs. left NULL.
  * ``_embedding_retry_delay_seconds`` — exponential backoff + cap.
  * ``recover_failed_documents`` — the autonomous sweeper that re-enqueues
    due documents.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from app.models.knowledge_base import KnowledgeDocument
from app.services import kb_helpers


class _FakeSettings:
    """Deterministic auto-retry settings (jitter disabled for exact asserts)."""

    embedding_auto_retry_enabled = True
    embedding_auto_retry_base_delay_seconds = 300
    embedding_auto_retry_max_delay_seconds = 21_600
    embedding_auto_retry_jitter_seconds = 0
    embedding_auto_retry_max_attempts = 0
    embedding_auto_retry_batch_size = 100


@pytest.fixture
def fake_settings(monkeypatch):
    settings = _FakeSettings()
    monkeypatch.setattr(kb_helpers, "app_settings", settings)
    return settings


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


def _make_error_doc(
    *,
    retry_count: int = 0,
    next_retry_minutes_ago: float | None = 1.0,
    updated_minutes_ago: float = 30.0,
) -> KnowledgeDocument:
    next_retry_at = None
    if next_retry_minutes_ago is not None:
        next_retry_at = datetime.now(UTC) - timedelta(minutes=next_retry_minutes_ago)
    return KnowledgeDocument(
        id=uuid.uuid4(),
        knowledge_base_id=uuid.uuid4(),
        filename="failed.md",
        source_type="upload",
        content="x" * 64,
        status="error",
        error_message="embedding provider returned 500",
        chunk_count=0,
        embedding_retry_count=retry_count,
        embedding_next_retry_at=next_retry_at,
        updated_at=datetime.now(UTC) - timedelta(minutes=updated_minutes_ago),
    )


def test_default_backoff_cap_bounds_self_heal_latency():
    """Guard the platform default: the auto-retry backoff cap must stay low.

    With the old 6 h cap, a document that accumulated retries during a
    transient infra wave (DDL lock storm, provider blip) was parked in
    ``error`` — dashboard banner up — for hours after the cause had cleared.
    30 min bounds the worst-case self-heal
    latency.
    """
    from app.core.config import Settings

    default_cap = Settings.model_fields[
        "embedding_auto_retry_max_delay_seconds"
    ].default
    assert default_cap <= 1_800, (
        f"embedding_auto_retry_max_delay_seconds default ({default_cap}s) must "
        "not exceed 30 min — failed docs must self-heal promptly after a "
        "transient wave clears"
    )


# ---------------------------------------------------------------------------
# schedule_embedding_retry
# ---------------------------------------------------------------------------


def test_schedule_retry_sets_next_retry_for_retryable(fake_settings):
    doc = _make_error_doc(retry_count=0, next_retry_minutes_ago=None)
    before = datetime.now(UTC)

    kb_helpers.schedule_embedding_retry(doc, retryable=True)

    assert doc.embedding_next_retry_at is not None
    # First retry is scheduled ~base seconds out (jitter disabled).
    expected = before + timedelta(
        seconds=fake_settings.embedding_auto_retry_base_delay_seconds
    )
    delta = abs((doc.embedding_next_retry_at - expected).total_seconds())
    assert delta < 5, f"first retry should be ~base out; off by {delta}s"


def test_schedule_retry_clears_for_non_retryable(fake_settings):
    doc = _make_error_doc(retry_count=0, next_retry_minutes_ago=1.0)

    kb_helpers.schedule_embedding_retry(doc, retryable=False)

    assert doc.embedding_next_retry_at is None, (
        "structural/permanent failures must never be auto-retried"
    )


def test_schedule_retry_clears_when_disabled(fake_settings):
    fake_settings.embedding_auto_retry_enabled = False
    doc = _make_error_doc(retry_count=0, next_retry_minutes_ago=1.0)

    kb_helpers.schedule_embedding_retry(doc, retryable=True)

    assert doc.embedding_next_retry_at is None


def test_schedule_retry_clears_when_attempt_cap_reached(fake_settings):
    fake_settings.embedding_auto_retry_max_attempts = 3
    doc = _make_error_doc(retry_count=3, next_retry_minutes_ago=None)

    kb_helpers.schedule_embedding_retry(doc, retryable=True)

    assert doc.embedding_next_retry_at is None, (
        "a doc that exhausted its retry budget must stop being scheduled"
    )


def test_schedule_retry_allows_unlimited_when_cap_is_zero(fake_settings):
    fake_settings.embedding_auto_retry_max_attempts = 0  # unlimited
    doc = _make_error_doc(retry_count=99, next_retry_minutes_ago=None)

    kb_helpers.schedule_embedding_retry(doc, retryable=True)

    assert doc.embedding_next_retry_at is not None, (
        "max_attempts=0 means keep retrying forever (capped backoff)"
    )


# ---------------------------------------------------------------------------
# _embedding_retry_delay_seconds
# ---------------------------------------------------------------------------


def test_backoff_is_exponential_and_capped(fake_settings):
    base = fake_settings.embedding_auto_retry_base_delay_seconds
    assert kb_helpers._embedding_retry_delay_seconds(0) == base
    assert kb_helpers._embedding_retry_delay_seconds(1) == base * 2
    assert kb_helpers._embedding_retry_delay_seconds(2) == base * 4
    # Far-out attempts are clamped to max_delay, never astronomically large.
    capped = kb_helpers._embedding_retry_delay_seconds(1000)
    assert capped == fake_settings.embedding_auto_retry_max_delay_seconds


# ---------------------------------------------------------------------------
# recover_failed_documents
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recover_failed_documents_reenqueues_due_doc(fake_settings, monkeypatch):
    doc = _make_error_doc(retry_count=1, next_retry_minutes_ago=1.0)
    session = _FakeSession([doc])
    enqueue_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(kb_helpers, "enqueue_document_processing", enqueue_mock)
    monkeypatch.setattr(
        kb_helpers,
        "_resolve_embedding_enqueue_state",
        AsyncMock(return_value=(True, None)),
    )

    recovered = await kb_helpers.recover_failed_documents(session)

    assert recovered == 1
    # Re-enqueue flips the doc back to pending, spends one retry, and clears
    # the schedule (re-set by the processor on the next failure).
    assert doc.status == "pending"
    assert doc.embedding_retry_count == 2
    assert doc.embedding_next_retry_at is None
    enqueue_mock.assert_awaited_once_with(str(doc.id), embedding_ready=True)
    assert session.commits == 1


@pytest.mark.asyncio
async def test_recover_failed_documents_disabled_short_circuits(
    fake_settings, monkeypatch
):
    fake_settings.embedding_auto_retry_enabled = False
    doc = _make_error_doc()
    session = _FakeSession([doc])
    enqueue_mock = AsyncMock()
    resolver = AsyncMock()
    monkeypatch.setattr(kb_helpers, "enqueue_document_processing", enqueue_mock)
    monkeypatch.setattr(kb_helpers, "_resolve_embedding_enqueue_state", resolver)

    recovered = await kb_helpers.recover_failed_documents(session)

    assert recovered == 0
    enqueue_mock.assert_not_awaited()
    resolver.assert_not_awaited()
    assert session.commits == 0


@pytest.mark.asyncio
async def test_recover_failed_documents_skips_when_embedding_unconfigured(
    fake_settings, monkeypatch, caplog
):
    """While embedding is still broken, do NOT spend a retry — leave the docs
    due (so the next tick re-checks) and release the row locks via rollback.
    """
    doc = _make_error_doc()
    session = _FakeSession([doc])
    enqueue_mock = AsyncMock()
    monkeypatch.setattr(kb_helpers, "enqueue_document_processing", enqueue_mock)
    monkeypatch.setattr(
        kb_helpers,
        "_resolve_embedding_enqueue_state",
        AsyncMock(return_value=(False, "no embedding provider configured")),
    )

    with caplog.at_level("INFO"):
        recovered = await kb_helpers.recover_failed_documents(session)

    assert recovered == 0
    enqueue_mock.assert_not_awaited()
    # Untouched: still error, retry count not spent, still scheduled.
    assert doc.status == "error"
    assert doc.embedding_retry_count == 0
    assert doc.embedding_next_retry_at is not None
    assert session.commits == 0
    assert session.rollbacks == 1, (
        "must release FOR UPDATE SKIP LOCKED row locks before returning"
    )
    assert "Failed-document retry sweep skipped" in caplog.text


@pytest.mark.asyncio
async def test_recover_failed_documents_empty_returns_zero(fake_settings, monkeypatch):
    session = _FakeSession([])
    enqueue_mock = AsyncMock()
    resolver = AsyncMock()
    monkeypatch.setattr(kb_helpers, "enqueue_document_processing", enqueue_mock)
    monkeypatch.setattr(kb_helpers, "_resolve_embedding_enqueue_state", resolver)

    recovered = await kb_helpers.recover_failed_documents(session)

    assert recovered == 0
    enqueue_mock.assert_not_awaited()
    # Nothing due → must not probe the embedding provider config.
    resolver.assert_not_awaited()
    assert session.commits == 0


@pytest.mark.asyncio
async def test_recover_failed_documents_counts_only_successful_enqueues(
    fake_settings, monkeypatch
):
    docs = [
        _make_error_doc(retry_count=0),
        _make_error_doc(retry_count=0),
        _make_error_doc(retry_count=0),
    ]
    session = _FakeSession(docs)
    monkeypatch.setattr(
        kb_helpers,
        "enqueue_document_processing",
        AsyncMock(side_effect=[True, False, True]),
    )
    monkeypatch.setattr(
        kb_helpers,
        "_resolve_embedding_enqueue_state",
        AsyncMock(return_value=(True, None)),
    )

    recovered = await kb_helpers.recover_failed_documents(session)

    assert recovered == 2, "only successful SAQ enqueues count toward recovery"
    # A retry is spent ONLY for docs whose enqueue succeeded — the doc whose
    # enqueue failed (index 1) must keep its budget and stay ``error`` + due so
    # the next tick retries it, otherwise a Redis blip would silently consume a
    # slot of ``embedding_auto_retry_max_attempts`` for a job never queued.
    assert docs[0].embedding_retry_count == 1
    assert docs[0].status == "pending"
    assert docs[2].embedding_retry_count == 1
    assert docs[2].status == "pending"
    assert docs[1].embedding_retry_count == 0, (
        "a failed enqueue must not spend a retry"
    )
    assert docs[1].status == "error"
    assert docs[1].embedding_next_retry_at is not None, (
        "a failed enqueue must leave the doc scheduled for the next tick"
    )


@pytest.mark.asyncio
async def test_recover_failed_documents_clamps_nonpositive_batch_size(
    fake_settings, monkeypatch
):
    """A misconfigured ``batch_size`` of 0 / negative must not become a
    ``LIMIT 0`` (silently disables the sweep) or a negative ``LIMIT``
    (unbounded in Postgres) — it is clamped to a floor of 1."""
    doc = _make_error_doc()
    session = _FakeSession([doc])
    monkeypatch.setattr(
        kb_helpers, "enqueue_document_processing", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(
        kb_helpers,
        "_resolve_embedding_enqueue_state",
        AsyncMock(return_value=(True, None)),
    )

    await kb_helpers.recover_failed_documents(session, batch_size=0)

    from sqlalchemy.dialects import postgresql

    compiled = str(
        session.executed_statements[0].compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    ).upper()
    assert "LIMIT 1" in compiled, "non-positive batch_size must be clamped to 1"


@pytest.mark.asyncio
async def test_recover_failed_documents_caps_at_sweep_time(fake_settings, monkeypatch):
    """A doc that was scheduled under a higher / unlimited cap must not be
    re-enqueued past a lowered ``embedding_auto_retry_max_attempts`` — the
    sweeper re-checks the cap, clears the schedule, and skips the doc so it
    stops being perpetually due (the schedule-time clear in
    ``schedule_embedding_retry`` only runs on the *next* failure)."""
    fake_settings.embedding_auto_retry_max_attempts = 3
    # Still ``due`` (scheduled earlier under unlimited) but already at the cap.
    doc = _make_error_doc(retry_count=3, next_retry_minutes_ago=1.0)
    session = _FakeSession([doc])
    enqueue_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(kb_helpers, "enqueue_document_processing", enqueue_mock)
    monkeypatch.setattr(
        kb_helpers,
        "_resolve_embedding_enqueue_state",
        AsyncMock(return_value=(True, None)),
    )

    recovered = await kb_helpers.recover_failed_documents(session)

    assert recovered == 0
    enqueue_mock.assert_not_awaited()
    # Capped doc keeps its (already-spent) budget and stays ``error``, but its
    # schedule is cleared so the next tick won't pick it up again.
    assert doc.embedding_retry_count == 3
    assert doc.status == "error"
    assert doc.embedding_next_retry_at is None
    # Still commits to persist the cleared schedule + release row locks.
    assert session.commits == 1


@pytest.mark.asyncio
async def test_recover_failed_documents_uses_for_update_skip_locked(
    fake_settings, monkeypatch
):
    doc = _make_error_doc()
    session = _FakeSession([doc])
    monkeypatch.setattr(
        kb_helpers, "enqueue_document_processing", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(
        kb_helpers,
        "_resolve_embedding_enqueue_state",
        AsyncMock(return_value=(True, None)),
    )

    await kb_helpers.recover_failed_documents(session)

    assert len(session.executed_statements) == 1
    from sqlalchemy.dialects import postgresql

    compiled = str(
        session.executed_statements[0].compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    ).upper()
    assert "FOR UPDATE" in compiled
    assert "SKIP LOCKED" in compiled
