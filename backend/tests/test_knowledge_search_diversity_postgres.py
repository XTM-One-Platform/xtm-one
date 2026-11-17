"""search_knowledge across several knowledge bases, against real PostgreSQL.

The reported case: "OpenCTI connectors" on an agent bound to eight knowledge
bases returned five hits, all from "Enterprise Data Platform Schemas". One
fused ranking over every bound collection let a large collection of near
matches fill the top-k, so the documentation that answers the question never
showed. Reproduced here with real pgvector + full-text hybrid search: the
global ranking alone leaves the documentation out, ``search_knowledge`` gives
it a slot, names the knowledge base of every hit, and its ``knowledge_base``
filter searches one collection only.

``integration``-marked and skips when PostgreSQL (or pgvector) is unavailable.
"""

from __future__ import annotations

import uuid

import pytest

import app.models  # noqa: F401 - registers every table on Base.metadata
from app.models.embedding import Embedding
from app.models.knowledge_base import KnowledgeBase
from app.models.user import User
from app.services.builtin_tools import build_kb_search_tool
from app.services.hybrid_search import hybrid_search_embeddings
from app.tools.base import ToolCall
from tests.scratch_database import ALL_TABLES, scratch_database

pytestmark = pytest.mark.integration

MODEL = "fake-embedding-model"
QUERY = "OpenCTI connectors"
QUERY_VECTOR = [1.0, 0.0, 0.0]


class _Embedder:
    async def embed(self, _text: str, model: str | None = None) -> list[float]:
        return QUERY_VECTOR


def _chunk(kb: KnowledgeBase, content: str, vector: list[float]) -> Embedding:
    return Embedding(
        knowledge_base_id=kb.id,
        source_type="manual",
        content=content,
        embedding=vector,
        embedding_model=MODEL,
        embedding_dimensions=len(vector),
    )


async def test_a_crowded_out_knowledge_base_is_found_and_named() -> None:
    async with scratch_database(
        "kbdiversity", tables=ALL_TABLES, extensions=("vector",), skip_without_extensions=True
    ) as db:
        async with db.sessions() as session:
            user = User(email=f"u-{uuid.uuid4().hex[:6]}@example.com", hashed_password="x", full_name="u")
            session.add(user)
            await session.flush()
            schemas = KnowledgeBase(id=uuid.uuid4(), user_id=user.id, name="Enterprise Data Platform Schemas")
            docs = KnowledgeBase(id=uuid.uuid4(), user_id=user.id, name="OpenCTI Documentation")
            sales = KnowledgeBase(id=uuid.uuid4(), user_id=user.id, name="Sales playbook")
            session.add_all([schemas, docs, sales])
            await session.flush()
            # Forty schema chunks closer to the query than anything else, none
            # of them about OpenCTI connectors.
            session.add_all(
                _chunk(schemas, f"Table warehouse_{i}: columns id, source, ingested_at.", [1.0, 0.02 * i, 0.0])
                for i in range(40)
            )
            session.add(
                _chunk(
                    docs,
                    "OpenCTI connectors import data from external sources into the platform.",
                    [0.6, 0.8, 0.0],
                )
            )
            session.add(_chunk(sales, "Quarterly pricing guidelines for resellers.", [0.0, 0.0, 1.0]))
            await session.commit()
        kb_ids = [str(schemas.id), str(docs.id), str(sales.id)]

        async with db.sessions() as session:
            global_only = await hybrid_search_embeddings(
                session,
                query_text=QUERY,
                query_embedding=QUERY_VECTOR,
                knowledge_base_ids=kb_ids,
                top_k=5,
                expected_embedding_dimensions=3,
                expected_embedding_model=MODEL,
            )
        assert {hit.knowledge_base_id for hit in global_only} == {str(schemas.id)}, (
            "the reported failure: the global ranking alone is all schemas"
        )

        _definitions, executor = build_kb_search_tool(
            None, str(user.id), kb_ids, _embedding_provider_model=(_Embedder(), MODEL)
        )

        async def _search(**arguments) -> str:
            result = await executor.execute_tool(ToolCall(id="1", name="search_knowledge", arguments=arguments))
            return result.content

        found = await _search(query=QUERY, limit=5)
        assert "in knowledge base 'OpenCTI Documentation'\nOpenCTI connectors import data" in found
        assert found.count("in knowledge base 'Enterprise Data Platform Schemas'") == 4
        assert "Sales playbook" not in found, "an unrelated collection is not promoted"

        filtered = await _search(query=QUERY, limit=5, knowledge_base="OpenCTI Documentation")
        assert filtered.startswith("[1] in knowledge base 'OpenCTI Documentation'")
        assert "Enterprise Data Platform Schemas" not in filtered
