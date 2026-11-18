"""``retry_provider_failures_now`` on a real database, then the sweeper it feeds."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

import app.models  # noqa: F401 — registers every mapper
from app.models.knowledge_base import KnowledgeBase, KnowledgeDocument
from app.services import kb_helpers
from app.services.kb_document_errors import (
    EMBEDDING_PROVIDER_ERROR_MESSAGE,
    NO_CHUNKS_MESSAGE,
    TIMEOUT_MESSAGE,
    dimension_mismatch_message,
    worker_timeout_message,
)
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 11, 18, 12, tzinfo=UTC)


@pytest.mark.asyncio
async def test_provider_failures_become_due_and_the_sweeper_requeues_them(monkeypatch):
    monkeypatch.setattr(kb_helpers.app_settings, "embedding_auto_retry_enabled", True)
    monkeypatch.setattr(kb_helpers.app_settings, "embedding_auto_retry_max_attempts", 3)
    async with scratch_database("provretry", tables=ALL_TABLES, extensions=("vector",)) as scratch:
        async with scratch.sessions() as db:
            kb = KnowledgeBase(name=f"kb-{uuid.uuid4().hex[:8]}", user_id=None, is_company_managed=True)
            db.add(kb)
            await db.flush()

            def doc(name, *, status="error", error=None, retries=0):
                return KnowledgeDocument(
                    knowledge_base_id=kb.id,
                    filename=f"{name}.md",
                    source_type="text",
                    status=status,
                    error_message=error,
                    embedding_retry_count=retries,
                )

            docs = {
                "dims": doc("dims", error=dimension_mismatch_message("text-embedding-3-large", "3072", 1536)),
                "provider": doc("provider", error=EMBEDDING_PROVIDER_ERROR_MESSAGE, retries=1),
                "timeout": doc("timeout", error=TIMEOUT_MESSAGE),
                "content": doc("content", error=NO_CHUNKS_MESSAGE),
                "worker": doc("worker", error=worker_timeout_message(600)),
                "spent": doc("spent", error=EMBEDDING_PROVIDER_ERROR_MESSAGE, retries=3),
                "ready": doc("ready", status="ready"),
            }
            db.add_all(docs.values())
            await db.commit()

            assert await kb_helpers.retry_provider_failures_now(db, now=_NOW) == 3
            await db.commit()

        async with scratch.sessions() as db:
            rows = {r.filename[:-3]: r for r in (await db.execute(select(KnowledgeDocument))).scalars()}
            due = {name for name, r in rows.items() if r.embedding_next_retry_at is not None}
            assert due == {"dims", "provider", "timeout"}
            assert rows["dims"].embedding_next_retry_at == _NOW
            # Nothing else is written until the sweeper re-queues: the reason stays.
            assert rows["dims"].status == "error"
            assert rows["dims"].error_message.startswith("Embedding model '")
            assert rows["provider"].embedding_retry_count == 1

            enqueued: list[str] = []

            async def _enqueue(doc_id, **_kw):
                enqueued.append(doc_id)
                return True

            async def _embedding_ready(_probe):
                return True, None

            monkeypatch.setattr(kb_helpers, "enqueue_document_processing", _enqueue)
            monkeypatch.setattr(kb_helpers, "_sweep_embedding_state", _embedding_ready)
            requeued = await kb_helpers.recover_failed_documents(db, now=_NOW + timedelta(seconds=1))

            assert requeued == 3
            assert sorted(enqueued) == sorted(str(rows[n].id) for n in ("dims", "provider", "timeout"))
