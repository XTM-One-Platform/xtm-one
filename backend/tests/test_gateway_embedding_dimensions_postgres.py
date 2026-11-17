"""A platform embeds at its configured dimension and the stale sweep converges.

With ``text-embedding-3-large`` (native 3072) behind the Filigran AI Gateway and
a platform dimension of 1024, a processed document stores 1024-wide vectors and
the stale sweep finds nothing to re-embed. Before, the gateway was never asked
for 1024, so every sweep purged and re-embedded the same documents. A provider
that cannot shorten its vectors leaves the document in error instead.

A re-embed keeps a document's previous vectors until it replaces them: a refused
one leaves them searchable, a successful one swaps them without duplicates. A
content change still drops the old content's vectors before it re-queues.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass

import httpx
import pytest
from sqlalchemy import func, select

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.api.v1 import knowledge_bases as kb_api
from app.api.v1.schemas.knowledge_base import UpdateKnowledgeDocumentRequest
from app.llm import router
from app.llm.gateway_provider import FiligranGatewayProvider, StaticTokenAuth
from app.models.embedding import Embedding
from app.models.knowledge_base import KnowledgeBase, KnowledgeDocument
from app.models.llm_config import LLMProviderConfig
from app.models.platform_settings import PlatformSettings
from app.models.user import User
from app.services import document_processor, kb_helpers
from app.services.kb_document_errors import dimension_mismatch_message
from tests.scratch_database import ALL_TABLES, ScratchDatabase, scratch_database

pytestmark = pytest.mark.integration

MODEL = "text-embedding-3-large"
NATIVE = 3072
CONFIGURED = 1024
CONTENT = "Rotate the signing keys every 90 days and log each rotation."


def _gateway(bodies: list[dict]) -> FiligranGatewayProvider:
    """A gateway that, like OpenAI, answers the ``dimensions`` asked for, else the native size."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models":
            return httpx.Response(200, json={"object": "list", "data": []})
        body = json.loads(request.content)
        bodies.append(body)
        size = body.get("dimensions", NATIVE)
        return httpx.Response(200, json={
            "object": "list",
            "data": [{"object": "embedding", "index": 0, "embedding": [0.5] * size}],
            "model": MODEL,
            "usage": {"prompt_tokens": 1, "total_tokens": 1},
        })

    return FiligranGatewayProvider(
        api_key="http://gateway.local:8080",
        http_client=httpx.AsyncClient(auth=StaticTokenAuth("t"), transport=httpx.MockTransport(handler)),
    )


class _NativeOnly:
    """A provider that cannot shorten its vectors (Azure OpenAI, Bedrock, ...): it ignores ``embedding_dimensions``."""

    provider_name = "azure_openai"

    async def embed(self, text, model=None):
        return [0.5] * NATIVE


@dataclass
class World:
    scratch: ScratchDatabase
    user_id: uuid.UUID
    kb_id: uuid.UUID
    doc_id: uuid.UUID


async def _world(scratch: ScratchDatabase, provider_key: str, *, previous_vectors: int = 0) -> World:
    """A KB with one document at ``CONFIGURED``: ``pending``, or ``ready`` with *previous_vectors* native chunks."""
    async with scratch.sessions() as db:
        user = User(email=f"gw-{uuid.uuid4().hex[:8]}@test.local", hashed_password="x", full_name="Owner")
        db.add(user)
        config = LLMProviderConfig(provider=provider_key, api_key_enc="x", available_models=[MODEL])
        db.add(config)
        await db.flush()
        db.add(PlatformSettings(embedding_provider_id=config.id, embedding_model=MODEL, embedding_dimensions=CONFIGURED))
        kb = KnowledgeBase(name=f"kb-{uuid.uuid4().hex[:8]}", user_id=user.id, chunk_count=previous_vectors)
        db.add(kb)
        await db.flush()
        doc = KnowledgeDocument(
            knowledge_base_id=kb.id,
            filename="runbook.md",
            source_type="upload",
            content=CONTENT,
            status="ready" if previous_vectors else "pending",
            chunk_count=previous_vectors,
        )
        db.add(doc)
        await db.flush()
        for i in range(previous_vectors):
            db.add(Embedding(
                user_id=user.id,
                knowledge_base_id=kb.id,
                source_type="document",
                source_id=str(doc.id),
                embedding_model=MODEL,
                embedding_dimensions=NATIVE,
                content=f"{CONTENT} ({i})",
                embedding=[0.5] * NATIVE,
            ))
        await db.commit()
        return World(scratch, user.id, kb.id, doc.id)


async def _vector_dims(world: World) -> list[int]:
    async with world.scratch.sessions() as db:
        return list((await db.execute(
            select(func.vector_dims(Embedding.embedding)).where(Embedding.source_id == str(world.doc_id))
        )).scalars().all())


async def _document(world: World) -> KnowledgeDocument:
    async with world.scratch.sessions() as db:
        return await db.get(KnowledgeDocument, world.doc_id)


async def _stale(world: World) -> dict:
    """The dashboard's stale counts for the world's KB (empty: up to date)."""
    async with world.scratch.sessions() as db:
        return await kb_api._load_kb_stale_embedding_info(db, [await db.get(KnowledgeBase, world.kb_id)])


async def _sweep(world: World) -> int:
    async with world.scratch.sessions() as db:
        return await kb_helpers.recover_stale_documents(db, embedding_ready=True)


@pytest.fixture
def serve(monkeypatch):
    """Route every embedding through a provider, with the stale sweep on and its enqueue recorded."""
    enqueued: list[str] = []

    async def _enqueue(document_id, **_kwargs):
        enqueued.append(document_id)
        return True

    monkeypatch.setattr(kb_helpers.app_settings, "embedding_stale_auto_remediation_enabled", True)
    monkeypatch.setattr(kb_helpers, "enqueue_document_processing", _enqueue)

    def _serve(provider) -> list[str]:
        async def _cached(_config):
            return provider

        monkeypatch.setattr(router, "_cached_provider", _cached)
        return enqueued

    return _serve


async def test_a_gateway_platform_stores_the_configured_dimension_and_leaves_nothing_stale(serve):
    bodies: list[dict] = []
    serve(_gateway(bodies))
    async with scratch_database("gwdims", tables=ALL_TABLES, extensions=("vector",)) as scratch:
        world = await _world(scratch, "filigran_gateway")
        await document_processor.process_document(str(world.doc_id))

        assert (await _document(world)).status == "ready"
        dims = await _vector_dims(world)
        assert dims and set(dims) == {CONFIGURED}
        assert await _sweep(world) == 0
    assert bodies and all(b.get("dimensions") == CONFIGURED for b in bodies)


async def test_a_provider_that_cannot_shorten_its_vectors_is_refused_not_re_embedded(serve):
    serve(_NativeOnly())
    async with scratch_database("gwdims", tables=ALL_TABLES, extensions=("vector",)) as scratch:
        world = await _world(scratch, "azure_openai")
        await document_processor.process_document(str(world.doc_id))

        doc = await _document(world)
        assert doc.status == "error"
        assert doc.error_message == dimension_mismatch_message(MODEL, str(NATIVE), CONFIGURED)
        assert await _vector_dims(world) == []
        assert await _sweep(world) == 0


async def test_a_refused_re_embed_keeps_the_previous_vectors_searchable(serve):
    enqueued = serve(_NativeOnly())
    async with scratch_database("gwdims", tables=ALL_TABLES, extensions=("vector",)) as scratch:
        world = await _world(scratch, "azure_openai", previous_vectors=2)
        assert await _stale(world)

        assert await _sweep(world) == 1
        assert await _vector_dims(world) == [NATIVE, NATIVE]  # searchable while it is re-embedded
        await document_processor.process_document(enqueued[0])

        doc = await _document(world)
        assert doc.status == "error"
        assert await _vector_dims(world) == [NATIVE, NATIVE]
        assert doc.chunk_count == 2  # what it still stores
        assert await _sweep(world) == 0
        # The error says what to fix; the dashboard no longer counts the KB stale.
        assert await _stale(world) == {}


async def test_reprocessing_a_document_keeps_its_vectors_when_the_re_embed_is_refused(serve):
    serve(_NativeOnly())
    async with scratch_database(
        "gwdims", tables=ALL_TABLES, extensions=("vector",), stub_worker_queue=True
    ) as scratch:
        world = await _world(scratch, "azure_openai", previous_vectors=2)
        async with scratch.sessions() as db:
            await kb_api.reprocess_document(
                kb_id=str(world.kb_id),
                doc_id=str(world.doc_id),
                user=await db.get(User, world.user_id),
                db=db,
                _ai_gate=None,
            )
        assert await _vector_dims(world) == [NATIVE, NATIVE]
        await document_processor.process_document(str(world.doc_id))

        assert (await _document(world)).status == "error"
        assert await _vector_dims(world) == [NATIVE, NATIVE]


async def test_a_successful_re_embed_swaps_the_previous_vectors_without_duplicates(serve):
    enqueued = serve(_gateway([]))
    async with scratch_database("gwdims", tables=ALL_TABLES, extensions=("vector",)) as scratch:
        world = await _world(scratch, "filigran_gateway", previous_vectors=2)

        assert await _sweep(world) == 1
        await document_processor.process_document(enqueued[0])

        doc = await _document(world)
        dims = await _vector_dims(world)
        assert doc.status == "ready"
        assert set(dims) == {CONFIGURED} and len(dims) == doc.chunk_count
        assert await _sweep(world) == 0


async def test_an_edit_whose_re_embed_is_refused_leaves_none_of_the_old_content(serve):
    serve(_NativeOnly())
    async with scratch_database(
        "gwdims", tables=ALL_TABLES, extensions=("vector",), stub_worker_queue=True
    ) as scratch:
        world = await _world(scratch, "azure_openai", previous_vectors=2)
        async with scratch.sessions() as db:
            await kb_api.update_document(
                kb_id=str(world.kb_id),
                doc_id=str(world.doc_id),
                body=UpdateKnowledgeDocumentRequest(content="Rotate the signing keys every 30 days."),
                user=await db.get(User, world.user_id),
                db=db,
                _ai_gate=None,
            )
        await document_processor.process_document(str(world.doc_id))

        assert (await _document(world)).status == "error"
        assert await _vector_dims(world) == []
