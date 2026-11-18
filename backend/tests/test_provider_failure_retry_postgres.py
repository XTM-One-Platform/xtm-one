"""Provider failures after an embedding provider switch, on a real database:
which documents count, the batched retry that makes them due, and the sweeper
that re-queues them."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

import app.models  # noqa: F401 — registers every mapper
from app.models.knowledge_base import KnowledgeBase, KnowledgeDocument
from app.services import kb_helpers
from app.services.kb_document_errors import (
    EMBEDDING_PROVIDER_ERROR_MESSAGE,
    INTERNAL_ERROR_MESSAGE,
    NO_CHUNKS_MESSAGE,
    TIMEOUT_MESSAGE,
    dimension_mismatch_message,
    worker_timeout_message,
)
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 11, 18, 12, tzinfo=UTC)
_DUE = {"dims", "provider", "timeout", "legacy", "no_reason", "locked"}


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
                # Raw exception text from a row older than the fixed sentences.
                "legacy": doc("legacy", error="Error code: 401 - Incorrect API key provided"),
                "no_reason": doc("no_reason"),
                "content": doc("content", error=NO_CHUNKS_MESSAGE),
                "internal": doc("internal", error=INTERNAL_ERROR_MESSAGE),
                "worker": doc("worker", error=worker_timeout_message(600)),
                "spent": doc("spent", error=EMBEDDING_PROVIDER_ERROR_MESSAGE, retries=3),
                "locked": doc("locked", error=EMBEDDING_PROVIDER_ERROR_MESSAGE),
                "ready": doc("ready", status="ready"),
            }
            db.add_all(docs.values())
            await db.commit()

            assert await kb_helpers.count_provider_failures(db) == len(_DUE)

        # A row someone holds is waited on, not skipped; batches of one still
        # reach every row of the pass.
        async with scratch.sessions() as holder:
            await holder.execute(
                select(KnowledgeDocument).where(KnowledgeDocument.id == docs["locked"].id).with_for_update()
            )
            retry = asyncio.create_task(kb_helpers.retry_provider_failures_now(now=_NOW, batch_size=1))
            await asyncio.sleep(1)
            assert not retry.done()
            await holder.rollback()
            assert await retry == len(_DUE)

        async with scratch.sessions() as db:
            rows = {r.filename[:-3]: r for r in (await db.execute(select(KnowledgeDocument))).scalars()}
            assert {name for name, r in rows.items() if r.embedding_next_retry_at is not None} == _DUE
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

            assert requeued == len(_DUE)
            assert sorted(enqueued) == sorted(str(rows[n].id) for n in _DUE)


@pytest.mark.asyncio
async def test_retry_does_nothing_with_auto_retry_off(monkeypatch):
    monkeypatch.setattr(kb_helpers.app_settings, "embedding_auto_retry_enabled", False)

    assert await kb_helpers.retry_provider_failures_now(now=_NOW) == 0


@pytest.mark.asyncio
async def test_a_legacy_row_runs_on_the_config_the_save_compares_with(monkeypatch):
    from app.api.v1.settings import _legacy_embedding_config_id
    from app.core.security import encrypt_value
    from app.llm import router
    from app.models.llm_config import LLMProviderConfig
    from app.models.platform_settings import PlatformSettings

    async with (
        scratch_database("provlegacy", tables=ALL_TABLES, extensions=("vector",)) as scratch,
        scratch.sessions() as db,
    ):
        configs = [
            LLMProviderConfig(
                provider="openai",
                api_key_enc=encrypt_value("sk-test"),
                available_models=["text-embedding-3-small"],
                default_model=model,
                created_at=_NOW + timedelta(minutes=minutes),
            )
            for minutes, model in ((0, "text-embedding-3-small"), (1, "text-embedding-3-large"))
        ]
        db.add_all(configs)
        db.add(PlatformSettings(embedding_provider="openai", embedding_provider_id=None))
        await db.commit()
        monkeypatch.setattr(router, "ensure_not_excluded", lambda *_a, **_kw: _noop())

        _provider, model, used = await router.get_embedding_provider(db)

        assert used == configs[1].id
        assert model == "text-embedding-3-large"
        assert await _legacy_embedding_config_id(db, "openai") == used


@pytest.mark.asyncio
async def test_a_job_sees_the_embedding_config_it_used_was_switched(monkeypatch):
    from app.core.security import encrypt_value
    from app.llm import router
    from app.models.llm_config import LLMProviderConfig
    from app.models.platform_settings import PlatformSettings

    monkeypatch.setattr(router, "ensure_not_excluded", lambda *_a, **_kw: _noop())

    def config(provider, default_model):
        return LLMProviderConfig(
            provider=provider,
            api_key_enc=encrypt_value("sk-test"),
            available_models=[default_model] if default_model else [],
            default_model=default_model,
        )

    async with (
        scratch_database("provmoved", tables=ALL_TABLES, extensions=("vector",)) as scratch,
        scratch.sessions() as db,
    ):
        # The stored config has no model to embed with: the router falls back
        # to the OpenAI config, which is what the job used.
        stored = config("gemini", None)
        fallback = config("openai", "text-embedding-3-small")
        other = config("openai", "text-embedding-3-small")
        db.add_all([stored, fallback])
        await db.flush()
        other.created_at = fallback.created_at - timedelta(minutes=1)
        db.add(other)
        ps = PlatformSettings(embedding_provider="gemini", embedding_provider_id=stored.id)
        db.add(ps)
        await db.commit()

        assert await kb_helpers.embedding_config_moved(fallback.id) is False
        assert await kb_helpers.embedding_config_moved(str(fallback.id)) is False

        ps.embedding_provider = "openai"
        ps.embedding_provider_id = other.id
        await db.commit()

        assert await kb_helpers.embedding_config_moved(fallback.id) is True


@pytest.mark.asyncio
async def test_one_failure_is_made_due_only_while_it_is_still_that_failure(monkeypatch):
    monkeypatch.setattr(kb_helpers.app_settings, "embedding_auto_retry_enabled", True)
    monkeypatch.setattr(kb_helpers.app_settings, "embedding_auto_retry_max_attempts", 3)
    async with (
        scratch_database("provone", tables=ALL_TABLES, extensions=("vector",)) as scratch,
        scratch.sessions() as db,
    ):
        kb = KnowledgeBase(name=f"kb-{uuid.uuid4().hex[:8]}", user_id=None, is_company_managed=True)
        db.add(kb)
        await db.flush()
        mismatch = dimension_mismatch_message("text-embedding-3-large", "3072", 1536)
        failed, requeued, spent = (
            KnowledgeDocument(
                knowledge_base_id=kb.id,
                filename=f"{name}.md",
                source_type="text",
                status=status,
                error_message=mismatch,
                embedding_retry_count=retries,
            )
            for name, status, retries in (("failed", "error", 0), ("requeued", "pending", 0), ("spent", "error", 3))
        )
        db.add_all([failed, requeued, spent])
        await db.commit()

        assert await kb_helpers.retry_provider_failure_now(db, failed.id, now=_NOW) is True
        assert await kb_helpers.retry_provider_failure_now(db, requeued.id, now=_NOW) is False
        assert await kb_helpers.retry_provider_failure_now(db, spent.id, now=_NOW) is False

        due = (
            (
                await db.execute(
                    select(KnowledgeDocument.filename).where(KnowledgeDocument.embedding_next_retry_at.is_not(None))
                )
            )
            .scalars()
            .all()
        )
        assert due == ["failed.md"]


@pytest.mark.asyncio
async def test_one_failure_retry_never_raises():
    class _Broken:
        rolled_back = False

        async def execute(self, _statement):
            raise RuntimeError("connection dropped")

        async def rollback(self):
            self.rolled_back = True

    db = _Broken()

    assert await kb_helpers.retry_provider_failure_now(db, uuid.uuid4()) is False
    assert db.rolled_back is True


async def _noop():
    return None
