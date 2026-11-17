# Copyright (c) 2025-2026 Filigran SAS
#
# This file is part of the XTM One Enterprise Edition ("EE") and is
# licensed under the XTM One Enterprise Edition License (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://github.com/XTM-One-Platform/xtm-one/blob/main/LICENSE
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.

"""Hybrid search — combines vector similarity (pgvector) with BM25-like
full-text search (PostgreSQL ``tsvector`` / ``ts_rank_cd``) using
Reciprocal Rank Fusion (RRF).

How it works:
  1. **Vector search**: cosine similarity via pgvector ``<=>`` operator.
  2. **Full-text search**: PostgreSQL ``plainto_tsquery`` + ``ts_rank_cd``
     for BM25-like keyword relevance.
  3. **Fusion**: Reciprocal Rank Fusion (RRF) combines both ranked lists
     into a single ranking:  ``score = 1/(k + rank_vector) + 1/(k + rank_text)``
     where ``k=60`` (standard constant).

This module is the single source of truth for hybrid retrieval across
knowledge bases, agent memory, and any future vector-indexed content.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Float, func, literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.embedding import Embedding
from app.services.vector import (
    _scoped_statement_timeout,
    ann_cosine_distance,
    apply_hnsw_iterative_scan,
    get_pgvector_capabilities,
)

logger = logging.getLogger(__name__)

# RRF constant — standard value from the original paper
_RRF_K = 60

# Default weights for vector vs text search (0–1, must sum to 1)
_DEFAULT_VECTOR_WEIGHT = 0.7
_DEFAULT_TEXT_WEIGHT = 0.3


@dataclass
class HybridResult:
    """A single result from hybrid search with scoring breakdown."""

    id: str
    content: str
    vector_rank: int | None = None
    text_rank: int | None = None
    vector_score: float = 0.0
    text_score: float = 0.0
    rrf_score: float = 0.0
    source_type: str | None = None
    source_id: str | None = None
    knowledge_base_id: str | None = None


async def hybrid_search_embeddings(
    db: AsyncSession,
    query_text: str,
    query_embedding: list[float],
    knowledge_base_ids: list[str] | None = None,
    source_ids: list[str] | None = None,
    agent_id: str | None = None,
    user_id: str | None = None,
    top_k: int = 10,
    vector_weight: float = _DEFAULT_VECTOR_WEIGHT,
    text_weight: float = _DEFAULT_TEXT_WEIGHT,
    expected_embedding_dimensions: int | None = None,
    expected_embedding_model: str | None = None,
) -> list[HybridResult]:
    """Perform hybrid search on the embeddings table.

    Combines vector similarity search (pgvector cosine distance) with
    PostgreSQL full-text search using Reciprocal Rank Fusion.

    Args:
        db: Async database session.
        query_text: Natural language query for full-text search.
        query_embedding: Query embedding vector for similarity search.
        knowledge_base_ids: Optional KB ID filter.
        source_ids: Optional source-document filter, narrower than a KB. Use it
            when only SOME documents of a collection are in scope: a caller
            that filters the results afterwards instead has to over-fetch and
            guess, and returns nothing at all when the fused pool happens to be
            all documents it must drop. ``(knowledge_base_id,
            source_id)`` is indexed, and the list is expected to be bounded by
            the caller.
        agent_id: Optional agent ID filter.
        user_id: Optional user ID filter (includes company-managed).
        top_k: Number of results to return.
        vector_weight: Weight for vector search in RRF (0–1).
        text_weight: Weight for text search in RRF (0–1).

    Returns:
        Sorted list of HybridResult, highest RRF score first.
    """
    # Expand the candidate pool to ensure good fusion
    candidate_k = min(top_k * 3, 100)
    vector_weight_value = float(vector_weight)
    text_weight_value = float(text_weight)

    # ANN support: cast the vector distance so it matches the
    # partial HNSW expression index for ``expected_embedding_dimensions``
    # (see ``services/vector.py::ann_cosine_distance``).  Only the strict
    # query (``embedding_dimensions = N``) can prove the index predicate; the
    # legacy-NULL fallback keeps the exact path, same as before.
    halfvec_ok, iterative_ok = await get_pgvector_capabilities(db)

    # Build static WHERE clause fragments
    static_where_clauses: list[Any] = []
    probe_clauses = []
    if knowledge_base_ids:
        kb_uuids = [uuid.UUID(kid) for kid in knowledge_base_ids]
        static_where_clauses.append(Embedding.knowledge_base_id.in_(kb_uuids))
        probe_clauses.append(Embedding.knowledge_base_id.in_(kb_uuids))

    if source_ids:
        # Deduped and kept as text: `source_id` is a plain string column, not
        # an FK, so it is compared as the caller stored it.
        scoped_sources = sorted({str(sid) for sid in source_ids if sid})
        static_where_clauses.append(Embedding.source_id.in_(scoped_sources))
        probe_clauses.append(Embedding.source_id.in_(scoped_sources))

    if agent_id:
        parsed_agent_id = uuid.UUID(agent_id)
        static_where_clauses.append(Embedding.agent_id == parsed_agent_id)
        probe_clauses.append(Embedding.agent_id == parsed_agent_id)

    if user_id:
        parsed_user_id = uuid.UUID(user_id)
        static_where_clauses.append(
            (Embedding.user_id == parsed_user_id) | (Embedding.user_id.is_(None))
        )
        probe_clauses.append(
            (Embedding.user_id == parsed_user_id) | (Embedding.user_id.is_(None))
        )

    async def _run_query(
        *,
        include_legacy_nulls: bool,
        static_clauses: list[Any],
        expected_dimensions: int | None,
        expected_model: str | None,
    ) -> list[HybridResult]:
        """Execute hybrid SQL with explicit static filters and metadata options."""
        where_clauses = [*static_clauses]

        if expected_dimensions is not None:
            if include_legacy_nulls:
                where_clauses.append(
                    (Embedding.embedding_dimensions == expected_dimensions)
                    | (Embedding.embedding_dimensions.is_(None))
                )
            else:
                where_clauses.append(
                    Embedding.embedding_dimensions == expected_dimensions
                )

        if expected_model:
            if include_legacy_nulls:
                where_clauses.append(
                    (Embedding.embedding_model == expected_model)
                    | (Embedding.embedding_model.is_(None))
                )
            else:
                where_clauses.append(Embedding.embedding_model == expected_model)

        vector_distance = ann_cosine_distance(
            query_embedding, expected_dimensions, halfvec_ok=halfvec_ok
        )
        vector_similarity = (literal(1.0) - vector_distance).label(
            "vector_similarity"
        )
        text_query = func.plainto_tsquery("english", query_text)
        text_vector = func.to_tsvector("english", Embedding.content)
        text_relevance = func.ts_rank_cd(text_vector, text_query, 32).label(
            "text_relevance"
        )

        vector_results = (
            select(
                Embedding.id.label("id"),
                Embedding.content.label("content"),
                Embedding.source_type.label("source_type"),
                Embedding.source_id.label("source_id"),
                Embedding.knowledge_base_id.label("knowledge_base_id"),
                vector_similarity,
                func.row_number()
                .over(order_by=vector_distance)
                .label("vector_rank"),
            )
            .where(*where_clauses)
            .order_by(vector_distance)
            .limit(candidate_k)
            .cte("vector_results")
        )

        text_results = (
            select(
                Embedding.id.label("id"),
                Embedding.content.label("content"),
                Embedding.source_type.label("source_type"),
                Embedding.source_id.label("source_id"),
                Embedding.knowledge_base_id.label("knowledge_base_id"),
                text_relevance,
                func.row_number()
                .over(order_by=text_relevance.desc())
                .label("text_rank"),
            )
            .where(*where_clauses, text_vector.op("@@")(text_query))
            .limit(candidate_k)
            .cte("text_results")
        )

        combined = (
            select(
                func.coalesce(vector_results.c.id, text_results.c.id).label("id"),
                func.coalesce(vector_results.c.content, text_results.c.content).label(
                    "content"
                ),
                func.coalesce(
                    vector_results.c.source_type,
                    text_results.c.source_type,
                ).label("source_type"),
                func.coalesce(
                    vector_results.c.source_id,
                    text_results.c.source_id,
                ).label("source_id"),
                func.coalesce(
                    vector_results.c.knowledge_base_id,
                    text_results.c.knowledge_base_id,
                ).label("knowledge_base_id"),
                vector_results.c.vector_rank,
                text_results.c.text_rank,
                func.coalesce(vector_results.c.vector_similarity, 0.0).label(
                    "vector_similarity"
                ),
                func.coalesce(text_results.c.text_relevance, 0.0).label(
                    "text_relevance"
                ),
                (
                    func.coalesce(
                        literal(vector_weight_value, type_=Float())
                        / (_RRF_K + vector_results.c.vector_rank),
                        0.0,
                    )
                    + func.coalesce(
                        literal(text_weight_value, type_=Float())
                        / (_RRF_K + text_results.c.text_rank),
                        0.0,
                    )
                ).label("rrf_score"),
            )
            .select_from(
                vector_results.join(
                    text_results,
                    vector_results.c.id == text_results.c.id,
                    full=True,
                )
            )
            .cte("combined")
        )

        result = await db.execute(
            select(*combined.c)
            .order_by(combined.c.rrf_score.desc())
            .limit(top_k)
        )
        rows = result.mappings().all()

        return [
            HybridResult(
                id=str(row["id"]),
                content=row["content"],
                vector_rank=row["vector_rank"],
                text_rank=row["text_rank"],
                vector_score=float(row["vector_similarity"]),
                text_score=float(row["text_relevance"]),
                rrf_score=float(row["rrf_score"]),
                source_type=row["source_type"],
                source_id=row["source_id"],
                knowledge_base_id=(
                    str(row["knowledge_base_id"]) if row["knowledge_base_id"] else None
                ),
            )
            for row in rows
        ]

    # Fail-fast guard: cap each hybrid query at the configured
    # per-statement timeout so the heavy two-CTE FULL OUTER JOIN cannot
    # hold the connection beyond the global pool timeout.  The cap is
    # SCOPED to this ``async with`` block — see
    # ``_scoped_statement_timeout`` — so downstream queries in the same
    # FastAPI request inherit the pool's default timeout rather than
    # our search-specific one.
    async with _scoped_statement_timeout(db):
        if iterative_ok:
            await apply_hnsw_iterative_scan(db)
        results = await _run_query(
            include_legacy_nulls=False,
            static_clauses=static_where_clauses,
            expected_dimensions=expected_embedding_dimensions,
            expected_model=expected_embedding_model,
        )
        if results:
            return results

        # Unlike ``search_knowledge_bases``, the strict query in
        # ``_run_query`` only applies a dimension filter when the
        # caller passes ``expected_embedding_dimensions`` explicitly —
        # there is no derived ``len(query_embedding)`` filter.  So
        # legacy NULL-dimension rows are already returned by the
        # strict path when no kwarg is set, and the probe-and-fallback
        # is only needed when an explicit metadata kwarg is in effect.
        should_retry_legacy = (
            expected_embedding_dimensions is not None
            or bool(expected_embedding_model)
        )
        if not should_retry_legacy:
            return results

        legacy_probe_clauses = [*probe_clauses]
        legacy_null_conditions = []
        if expected_embedding_dimensions is not None:
            legacy_probe_clauses.append(
                (Embedding.embedding_dimensions == expected_embedding_dimensions)
                | (Embedding.embedding_dimensions.is_(None))
            )
            legacy_null_conditions.append(Embedding.embedding_dimensions.is_(None))
        if expected_embedding_model:
            legacy_probe_clauses.append(
                (Embedding.embedding_model == expected_embedding_model)
                | (Embedding.embedding_model.is_(None))
            )
            legacy_null_conditions.append(Embedding.embedding_model.is_(None))

        legacy_probe_stmt = (
            select(Embedding.id)
            .where(*legacy_probe_clauses)
            .where(or_(*legacy_null_conditions))
            .limit(1)
        )
        legacy_probe = await db.execute(legacy_probe_stmt)
        if legacy_probe.first() is None:
            return results

        logger.debug(
            "Hybrid search strict filter returned no rows; "
            "retrying with legacy null metadata fallback"
        )
        try:
            return await _run_query(
                include_legacy_nulls=True,
                static_clauses=static_where_clauses,
                expected_dimensions=expected_embedding_dimensions,
                expected_model=expected_embedding_model,
            )
        except Exception:
            logger.warning(
                "Hybrid search legacy metadata fallback failed", exc_info=True
            )
            return results
