"""``reset_changed_document``: a content change drops the old vectors and queues the document.

The processor keeps a document's vectors until it replaces them, so every path
that changes a document's content goes through this helper.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock

from app.models.knowledge_base import KnowledgeBase, KnowledgeDocument
from app.services.kb_helpers import reset_changed_document


def _doc(kb: KnowledgeBase) -> KnowledgeDocument:
    return KnowledgeDocument(
        id=uuid.uuid4(),
        knowledge_base_id=kb.id,
        filename="runbook.md",
        source_type="upload",
        content="old",
        status="error",
        error_message="The provider failed.",
        chunk_count=4,
        embedding_retry_count=3,
        embedding_next_retry_at=datetime.now(UTC),
    )


async def test_the_old_vectors_go_and_the_document_is_queued_as_new():
    kb = KnowledgeBase(id=uuid.uuid4(), name="KB", chunk_count=10)
    doc = _doc(kb)
    db = AsyncMock()

    await reset_changed_document(db, doc, kb=kb)

    deleted = db.execute.await_args.args[0]
    assert deleted.is_delete and deleted.table.name == "embeddings"
    assert str(doc.id) in str(deleted.compile(compile_kwargs={"literal_binds": True}))
    assert (doc.status, doc.error_message, doc.chunk_count) == ("pending", None, 0)
    assert (doc.embedding_retry_count, doc.embedding_next_retry_at) == (0, None)
    assert kb.chunk_count == 6


async def test_a_caller_that_deleted_the_vectors_its_own_way_is_not_sent_a_second_delete():
    kb = KnowledgeBase(id=uuid.uuid4(), name="KB", chunk_count=10)
    doc = _doc(kb)
    db = AsyncMock()

    await reset_changed_document(db, doc, vectors_deleted=True)

    db.execute.assert_not_awaited()
    assert doc.status == "pending" and doc.chunk_count == 0
    assert kb.chunk_count == 10  # no KB given: its count is left to the caller
