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

"""Vector service – embedding storage and semantic search via pgvector."""

from __future__ import annotations

import contextlib
import logging
import re
import uuid
from collections.abc import AsyncIterator, Callable, Coroutine
from typing import Any

from pgvector.sqlalchemy import HALFVEC, Vector
from sqlalchemy import cast, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings as app_settings
from app.core.utils import format_exception_message
from app.models.embedding import Embedding
from app.models.knowledge_base import KnowledgeBase

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# ANN (HNSW) index support — assessment finding ARCH-2.
#
# ``embeddings.embedding`` is an untyped (dimension-flexible) vector column,
# so the HNSW indexes are partial EXPRESSION indexes per supported dimension
# (see ``app/models/embedding.py`` and migration ``c9d3e7f1a5b2``):
#
#   (embedding::vector(N))      vector_cosine_ops   WHERE embedding_dimensions = N
#   (embedding::halfvec(3072))  halfvec_cosine_ops  WHERE embedding_dimensions = 3072
#
# PostgreSQL only uses an expression index when the query's ORDER BY
# expression matches the indexed expression — so the search queries must cast
# the column identically.  ``ann_cosine_distance`` below builds the correctly
# cast distance expression; dimensions without an ANN index fall back to the
# plain (exact, seq-scan) distance, which is the pre-index behaviour.
# ---------------------------------------------------------------------------

# Dimensions indexed through the ``vector`` opclass (pgvector caps indexable
# ``vector`` columns at 2000 dims).
ANN_VECTOR_DIMENSIONS: tuple[int, ...] = (768, 1024, 1536)

# Dimensions indexed through half-precision ``halfvec`` (pgvector >= 0.7,
# indexable up to 4000 dims) — used for the 3072-dim platform default.
ANN_HALFVEC_DIMENSIONS: tuple[int, ...] = (3072,)

# Cached (halfvec_available, iterative_scan_available) probe result.
# halfvec needs pgvector >= 0.7; hnsw.iterative_scan needs pgvector >= 0.8.
# Both hold on the supported PostgreSQL 17 + pgvector stack; the probe keeps
# searches working (on the exact-scan path) on out-of-support servers.
_pgvector_capabilities: tuple[bool, bool] | None = None


async def get_pgvector_capabilities(db: AsyncSession) -> tuple[bool, bool]:
    """Return ``(halfvec_available, iterative_scan_available)``, cached.

    Probes the installed pgvector extension version once per process.  On any
    probe failure (non-PG test backend, missing extension) returns
    ``(False, False)`` WITHOUT caching so a transient error doesn't disable
    ANN search for the process lifetime.
    """
    global _pgvector_capabilities
    if _pgvector_capabilities is not None:
        return _pgvector_capabilities
    try:
        result = await db.execute(
            text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
        )
        raw = result.scalar_one_or_none()
        if not isinstance(raw, str):
            return (False, False)
        parts = raw.split(".")
        version = (int(parts[0]), int(parts[1]) if len(parts) > 1 else 0)
    except Exception:
        logger.debug(
            "Could not probe pgvector extension version; "
            "ANN search casts disabled for this call",
            exc_info=True,
        )
        return (False, False)
    _pgvector_capabilities = (version >= (0, 7), version >= (0, 8))
    return _pgvector_capabilities


def ann_cosine_distance(
    query_embedding: list[float],
    dimensions: int | None,
    *,
    halfvec_ok: bool = False,
):
    """Cosine-distance expression that matches the HNSW expression indexes.

    Casts ``embeddings.embedding`` to the fixed-dimension type backing the
    partial index for *dimensions* so the planner can use it.  The cast is
    safe even on the legacy-NULL metadata fallback path: a row whose actual
    vector dimension differs from *dimensions* would already error on the
    plain ``<=>`` operator (pgvector rejects mixed-dimension comparisons), so
    the cast introduces no new failure mode.

    Falls back to the plain (exact-scan) distance when *dimensions* has no
    ANN index or when ``halfvec`` is unavailable for the 3072-dim path.
    """
    if dimensions in ANN_VECTOR_DIMENSIONS:
        return cast(Embedding.embedding, Vector(dimensions)).cosine_distance(
            query_embedding
        )
    if halfvec_ok and dimensions in ANN_HALFVEC_DIMENSIONS:
        return cast(Embedding.embedding, HALFVEC(dimensions)).cosine_distance(
            query_embedding
        )
    return Embedding.embedding.cosine_distance(query_embedding)


async def apply_hnsw_iterative_scan(db: AsyncSession) -> None:
    """Best-effort ``SET LOCAL hnsw.iterative_scan = relaxed_order``.

    The KB searches filter on ``knowledge_base_id`` (and model metadata)
    AFTER the HNSW scan.  Without iterative scanning, a highly selective
    filter can leave fewer than the requested top-k rows once the scan's
    ``ef_search`` candidates are filtered — silent recall loss.  With
    ``relaxed_order`` (pgvector >= 0.8) the scan keeps fetching candidates
    until the LIMIT is satisfied.  ``SET LOCAL`` is transaction-scoped, only
    affects HNSW scans, and auto-resets on commit/rollback — so unlike the
    statement timeout there is nothing to restore.

    Callers must gate on :func:`get_pgvector_capabilities` so the GUC is
    never set on servers that don't define it (an unknown parameter would
    abort the transaction).
    """
    try:
        await db.execute(text("SET LOCAL hnsw.iterative_scan = 'relaxed_order'"))
    except Exception:
        logger.debug(
            "Could not enable hnsw.iterative_scan for this search",
            exc_info=True,
        )


# Whitelist for ``SHOW statement_timeout`` return values that are safe
# to inline back into ``SET LOCAL statement_timeout = '<value>'``.
# PostgreSQL's GUC value grammar accepts ``[0-9]+(\.[0-9]+)?(us|ms|s|
# min|h|d)?`` plus the bare ``0`` (= disabled).  Anything that doesn't
# match falls back to ``DEFAULT`` so we never SQL-inject a malformed
# value back into the session.
_PG_DURATION_RE = re.compile(r"^[0-9]+(?:\.[0-9]+)?(?:us|ms|s|min|h|d)?$")


@contextlib.asynccontextmanager
async def _scoped_statement_timeout(db: AsyncSession) -> AsyncIterator[None]:
    """Scoped ``SET LOCAL statement_timeout`` for KB-search queries.

    Clusters of ``57014`` timeouts were reported that each
    consumed the full 30 s pool default and starved the pool —
    capping individual KB search statements at the per-query
    ``kb_search_statement_timeout_ms`` setting recovers throughput
    under load.

    Why this is a context manager (not a fire-and-forget helper)
    -----------------------------------------------------------
    ``SET LOCAL`` is transaction-scoped, not statement-scoped.  In
    FastAPI's typical wiring the same DB session/transaction is reused
    across multiple operations in a single request — so an earlier
    fire-and-forget ``SET LOCAL statement_timeout = '5s'`` at the top
    of a KB search would silently cap UNRELATED downstream queries in
    the same request (e.g. an audit-log insert after the search), which
    is not what we want.

    By wrapping the search in an ``async with`` and restoring the
    prior value in the ``finally`` block (or falling back to
    ``DEFAULT`` when the prior value is not in our safe-grammar
    whitelist), the timeout cap applies ONLY to statements inside the
    ``with`` body.  Callers that want to cap a wider scope can still
    wrap that scope explicitly — the contract is now opt-in rather
    than opt-out.

    Configurable via ``kb_search_statement_timeout_ms`` in
    ``Settings``.  A non-positive value is treated as "inherit the
    pool's global timeout" and the context manager is a strict no-op
    (it does not even probe ``SHOW``).

    All DB calls are wrapped in best-effort ``try/except`` so an
    unsupported backend (e.g. SQLite in tests, or a temporarily
    missing transaction context) never breaks the search — the
    worst case is that the query falls back to the pool-level timeout.
    """
    timeout_ms = max(0, int(app_settings.kb_search_statement_timeout_ms or 0))
    if timeout_ms <= 0:
        yield
        return

    prior_value: str | None = None
    try:
        show_result = await db.execute(text("SHOW statement_timeout"))
        prior_raw = show_result.scalar_one_or_none()
        # ``SHOW`` returns a str on PostgreSQL.  Whitelist the value
        # against ``_PG_DURATION_RE`` BEFORE inlining it back: we
        # control ``SET LOCAL`` callsites today but a future GUC
        # introduction that adds e.g. unit suffixes we haven't seen
        # before would otherwise become a potential SQL injection
        # vector if we trusted the value blindly.  Anything that
        # doesn't match falls back to ``DEFAULT`` on restore.
        if isinstance(prior_raw, str) and _PG_DURATION_RE.match(prior_raw):
            prior_value = prior_raw
    except Exception:
        # SQLite / non-PG / no transaction: leave prior_value=None and
        # restore via DEFAULT in the finally below.  We still attempt
        # the SET LOCAL so a partial setup is harmless on retry.
        logger.debug(
            "Could not capture prior statement_timeout for KB search; "
            "will restore via DEFAULT after the scoped block",
            exc_info=True,
        )

    try:
        await db.execute(text(f"SET LOCAL statement_timeout = '{timeout_ms}'"))
    except Exception:
        logger.debug(
            "Could not apply SET LOCAL statement_timeout=%dms for KB search; "
            "scoped block will run with the pool's default timeout",
            timeout_ms,
            exc_info=True,
        )
        yield
        return

    try:
        yield
    finally:
        try:
            if prior_value is not None:
                # Inline the captured value (already whitelisted above).
                await db.execute(
                    text(f"SET LOCAL statement_timeout = '{prior_value}'")
                )
            else:
                # No safe prior captured — fall back to the session
                # default rather than leaving our cap in place for the
                # rest of the transaction.
                await db.execute(text("SET LOCAL statement_timeout = DEFAULT"))
        except Exception:
            logger.debug(
                "Could not restore statement_timeout after KB search; "
                "the cap may persist for the rest of this transaction",
                exc_info=True,
            )


async def store_embedding(
    db: AsyncSession,
    user_id: str | None,
    source_type: str,
    content: str,
    embedding: list[float],
    knowledge_base_id: str | None = None,
    agent_id: str | None = None,
    source_id: str | None = None,
    embedding_provider: str | None = None,
    embedding_provider_id: str | None = None,
    embedding_model: str | None = None,
    embedding_dimensions: int | None = None,
) -> Embedding:
    """Store a document chunk with its embedding vector.

    user_id may be None for company-managed knowledge bases.
    """
    record = Embedding(
        user_id=uuid.UUID(user_id) if user_id else None,
        knowledge_base_id=uuid.UUID(knowledge_base_id) if knowledge_base_id else None,
        agent_id=uuid.UUID(agent_id) if agent_id else None,
        source_type=source_type,
        source_id=source_id,
        embedding_provider=embedding_provider,
        embedding_provider_id=uuid.UUID(embedding_provider_id) if embedding_provider_id else None,
        embedding_model=embedding_model,
        embedding_dimensions=embedding_dimensions or len(embedding),
        content=content,
        embedding=embedding,
    )
    db.add(record)
    await db.flush()
    return record


async def search_knowledge_bases(
    db: AsyncSession,
    user_id: str | None,
    knowledge_base_ids: list[str],
    query_embedding: list[float],
    top_k: int = 10,
    expected_dimensions: int | None = None,
    expected_model: str | None = None,
) -> list[Embedding]:
    """Search across multiple knowledge bases.

    Filters by knowledge_base_id only. User_id filtering is not applied here
    because access control is handled upstream (only accessible KB IDs are passed in),
    and company-managed KBs store embeddings with user_id=NULL.
    """
    kb_uuids = [uuid.UUID(kb_id) for kb_id in knowledge_base_ids]
    target_dimensions = expected_dimensions or len(query_embedding)

    # ANN support: cast the distance expression so it matches the partial
    # HNSW expression index for ``target_dimensions`` (the strict query's
    # ``embedding_dimensions = N`` filter implies the index predicate).  The
    # legacy-NULL fallback query keeps the same expression but its OR-NULL
    # predicate cannot prove the partial predicate, so it stays on the exact
    # path — which is fine, it is the rare compatibility branch.
    halfvec_ok, iterative_ok = await get_pgvector_capabilities(db)
    distance = ann_cosine_distance(
        query_embedding, target_dimensions, halfvec_ok=halfvec_ok
    )

    def _build_query(*, include_legacy_nulls: bool):
        query = select(Embedding).where(Embedding.knowledge_base_id.in_(kb_uuids))
        if target_dimensions > 0:
            if include_legacy_nulls:
                query = query.where(
                    (Embedding.embedding_dimensions == target_dimensions)
                    | (Embedding.embedding_dimensions.is_(None))
                )
            else:
                query = query.where(Embedding.embedding_dimensions == target_dimensions)
        if expected_model:
            if include_legacy_nulls:
                query = query.where(
                    (Embedding.embedding_model == expected_model)
                    | (Embedding.embedding_model.is_(None))
                )
            else:
                query = query.where(Embedding.embedding_model == expected_model)
        return query.order_by(distance).limit(top_k)

    # Fail-fast guard: cap each search at the configured per-statement
    # timeout so a single slow plan cannot block the connection pool.
    # The cap is SCOPED to this ``async with`` block — see
    # ``_scoped_statement_timeout`` — so downstream queries in the same
    # FastAPI request (e.g. an audit-log insert) inherit the pool's
    # default timeout rather than our search-specific one.
    async with _scoped_statement_timeout(db):
        if iterative_ok:
            await apply_hnsw_iterative_scan(db)
        result = await db.execute(_build_query(include_legacy_nulls=False))
        rows = list(result.scalars().all())
        if rows:
            return rows

        # Legacy-NULL fallback contract
        # ------------------------------
        # The strict query above filters out rows where
        # ``embedding_dimensions`` or ``embedding_model`` are NULL
        # (legacy rows ingested before metadata columns existed).
        # That filter is implicit even when the CALLER didn't pass
        # ``expected_dimensions`` or ``expected_model``: we derive
        # ``target_dimensions = len(query_embedding)`` and filter on
        # it, which silently excludes legacy NULL-dimension rows.
        #
        # The probe-and-fallback path below recovers those legacy
        # rows.  It must therefore engage whenever ANY metadata
        # filter (explicit or derived) is in effect — i.e. whenever
        # ``target_dimensions > 0`` OR ``expected_model`` is set —
        # not only when the caller passes the kwargs explicitly.
        # Without this broader gate, a caller that omits the kwargs
        # would get strict-only behavior and silently drop legacy
        # rows (issue raised in the PR review).
        should_retry_legacy = target_dimensions > 0 or bool(expected_model)
        if not should_retry_legacy:
            return rows

        legacy_null_conditions = []
        # Probe only for rows that are valid for the fallback query AND
        # have at least one NULL metadata field.  Because strict
        # retrieval already returned no rows, this probe cannot match
        # strict-only candidates — its only purpose is to short-circuit
        # when there are no legacy NULL rows at all (avoiding the cost
        # of the full OR-expanded retrieval query).
        legacy_probe = select(Embedding.id).where(
            Embedding.knowledge_base_id.in_(kb_uuids)
        )
        if target_dimensions > 0:
            legacy_null_conditions.append(Embedding.embedding_dimensions.is_(None))
            legacy_probe = legacy_probe.where(
                (Embedding.embedding_dimensions == target_dimensions)
                | (Embedding.embedding_dimensions.is_(None))
            )
        if expected_model:
            legacy_null_conditions.append(Embedding.embedding_model.is_(None))
            legacy_probe = legacy_probe.where(
                (Embedding.embedding_model == expected_model)
                | (Embedding.embedding_model.is_(None))
            )
        if not legacy_null_conditions:
            # Defensive: ``should_retry_legacy`` was true but no NULL
            # column applies (shouldn't happen with the gate above).
            return rows
        legacy_probe = legacy_probe.where(or_(*legacy_null_conditions)).limit(1)

        legacy_probe_result = await db.execute(legacy_probe)
        if legacy_probe_result.first() is None:
            return rows

        # Compatibility fallback for legacy rows ingested before
        # model/dim metadata existed.  Run this only when strict
        # retrieval returned nothing to avoid expensive OR clauses on
        # the hot path.
        logger.debug(
            "Vector search strict filter returned no rows; "
            "retrying with legacy null metadata fallback"
        )
        try:
            result = await db.execute(_build_query(include_legacy_nulls=True))
        except Exception:
            logger.warning(
                "Vector search legacy metadata fallback failed", exc_info=True
            )
            return rows

        return list(result.scalars().all())


# ---------------------------------------------------------------------------
# KB query count tracking
# ---------------------------------------------------------------------------


async def _increment_kb_query_count(
    kb_ids: set[str],
) -> None:
    """Increment query_count only on KBs that returned results (best-effort).

    Called after a successful search so only the knowledge bases that
    actually contributed content are counted, giving an accurate
    per-KB utilization metric.

    Uses an independent session to avoid interfering with the caller's
    session (which may be mid-query on the same asyncpg connection).
    """
    if not kb_ids:
        return
    try:
        from app.core.database import async_session

        kb_uuids = [uuid.UUID(k) for k in kb_ids]
        async with async_session() as session:
            await session.execute(
                update(KnowledgeBase)
                .where(KnowledgeBase.id.in_(kb_uuids))
                .values(query_count=KnowledgeBase.query_count + 1)
            )
            await session.commit()
    except Exception:
        logger.warning("Failed to increment KB query_count", exc_info=True)


# ---------------------------------------------------------------------------
# Search-result rendering
# ---------------------------------------------------------------------------

# Per-chunk cap on rendered content. Retrieval returns fragments, so a long
# chunk is trimmed rather than allowed to crowd out the other hits.
_CHUNK_RENDER_LIMIT = 2500


async def _load_document_titles(
    db: AsyncSession, source_ids: set[str]
) -> dict[str, str]:
    """Return ``{document_id: filename}`` for KB-document embedding source ids.

    Unparseable or unknown ids are simply absent from the result, so a caller
    can treat a missing entry as "no document provenance available".
    """
    if not source_ids:
        return {}
    from app.models.knowledge_base import KnowledgeDocument

    parsed: list[uuid.UUID] = []
    for raw in source_ids:
        try:
            parsed.append(uuid.UUID(str(raw)))
        except (ValueError, AttributeError, TypeError):
            continue
    if not parsed:
        return {}
    try:
        result = await db.execute(
            select(KnowledgeDocument.id, KnowledgeDocument.filename).where(
                KnowledgeDocument.id.in_(parsed)
            )
        )
        return {str(row[0]): row[1] for row in result.all()}
    except Exception:
        # Provenance is an enhancement, never a reason to fail a search.
        logger.warning(
            "Could not resolve source document titles for search results",
            exc_info=True,
        )
        return {}


async def _load_knowledge_base_names(
    db: AsyncSession, kb_ids: set[str]
) -> dict[str, str]:
    """Return ``{knowledge_base_id: name}``; an unknown id is simply absent."""
    parsed = [u for kb_id in kb_ids if (u := _safe_uuid(kb_id)) is not None]
    if not parsed:
        return {}
    try:
        result = await db.execute(
            select(KnowledgeBase.id, KnowledgeBase.name).where(KnowledgeBase.id.in_(parsed))
        )
        return {str(row[0]): row[1] for row in result.all()}
    except Exception:
        logger.warning("Could not resolve knowledge base names for search results", exc_info=True)
        return {}


def _safe_uuid(value: object) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return None


async def _render_search_results(db: AsyncSession, results: list) -> str:
    """Render retrieved chunks, each tagged with its source document and knowledge base.

    Chunk text alone is anonymous: an agent could not tell which document a
    fragment came from, let alone ask for that document in full. Tagging every
    hit with its document title and id makes the chunk -> document link explicit,
    which is what turns ``search_knowledge`` into a usable entry point for
    ``get_knowledge_document``. The knowledge base is named too,
    so an agent bound to several can tell where each hit comes from.

    *results* items only need ``content`` plus the optional ``source_type`` /
    ``source_id`` / ``knowledge_base_id`` attributes, so this serves both the
    hybrid and pure-vector result shapes.
    """
    doc_ids = {
        str(r.source_id)
        for r in results
        if getattr(r, "source_type", None) == "document"
        and getattr(r, "source_id", None)
    }
    titles = await _load_document_titles(db, doc_ids)
    kb_names = await _load_knowledge_base_names(
        db,
        {str(kb) for r in results if (kb := getattr(r, "knowledge_base_id", None))},
    )

    chunks: list[str] = []
    for index, r in enumerate(results, start=1):
        header = f"[{index}]"
        source_id = getattr(r, "source_id", None)
        if getattr(r, "source_type", None) == "document" and source_id:
            title = titles.get(str(source_id))
            if title:
                header = f"[{index}] from '{title}' (document id: {source_id})"
        kb_name = kb_names.get(str(getattr(r, "knowledge_base_id", None) or ""))
        if kb_name:
            header += f" in knowledge base '{kb_name}'"
        chunks.append(f"{header}\n{r.content[:_CHUNK_RENDER_LIMIT]}")
    return "\n\n".join(chunks)


# ---------------------------------------------------------------------------
# Cross-knowledge-base diversification
# ---------------------------------------------------------------------------

#: How far below the weakest hit of the global ranking a collection's best hit
#: may sit (cosine similarity) and still be given a slot: close enough that a
#: ranking normalised per knowledge base would have kept it.
_DIVERSITY_VECTOR_TOLERANCE = 0.05

#: The most collections absent from the global top-k that are probed, one small
#: hybrid query each.
_MAX_DIVERSITY_PROBES = 12


def diversify_across_knowledge_bases(
    ranked: list,
    candidates: list,
    limit: int,
) -> list:
    """Give a slot to relevant knowledge bases the global ranking left out.

    One fused ranking over every bound knowledge base lets a large collection of
    near-matches crowd a smaller, exactly-relevant one out of the top-k
    (five hits from one schema collection, none from the product documentation).
    *candidates* are the best hit of each collection absent from *ranked*, each
    ranked within its own collection - a per-knowledge-base normalisation. A
    candidate earns a slot when it is genuinely relevant: it matches every term
    of the query (full-text rank) or it is as close to the query as the weakest
    global hit, within :data:`_DIVERSITY_VECTOR_TOLERANCE`. At most half the
    slots go to them, each replacing the lowest-ranked hit of a collection that
    holds several, so no collection loses its only hit.
    """
    if not ranked or not candidates or limit < 2:
        return ranked
    # Only hits the vector ranking found carry a similarity; a full-text-only
    # hit reports 0.0, which would let every candidate through.
    similarities = [
        hit.vector_score for hit in ranked if getattr(hit, "vector_rank", None) is not None
    ]
    floor = min(similarities) if similarities else float("inf")
    eligible = sorted(
        (
            c
            for c in candidates
            if getattr(c, "text_rank", None) is not None
            or (getattr(c, "vector_score", 0.0) or 0.0) >= floor - _DIVERSITY_VECTOR_TOLERANCE
        ),
        key=lambda c: (getattr(c, "text_rank", None) is None, -(getattr(c, "vector_score", 0.0) or 0.0)),
    )[: max(1, limit // 2)]
    kept = list(ranked)
    added: list = []
    for candidate in eligible:
        counts: dict[object, int] = {}
        for hit in kept:
            counts[hit.knowledge_base_id] = counts.get(hit.knowledge_base_id, 0) + 1
        replaceable = [i for i, hit in enumerate(kept) if counts[hit.knowledge_base_id] > 1]
        if len(kept) + len(added) >= limit:
            if not replaceable:
                break
            kept.pop(replaceable[-1])
        added.append(candidate)
    return kept + added


async def _diversified_hybrid_search(
    db: AsyncSession,
    *,
    query: str,
    query_embedding: list[float],
    kb_ids: list[str],
    limit: int,
    expected_dimensions: int,
    embedding_model: str | None,
) -> list:
    """Hybrid search over *kb_ids*, diversified across knowledge bases."""
    from app.services.hybrid_search import hybrid_search_embeddings

    options = {
        "query_text": query,
        "query_embedding": query_embedding,
        "expected_embedding_dimensions": expected_dimensions,
        "expected_embedding_model": embedding_model,
    }
    ranked = await hybrid_search_embeddings(
        db, knowledge_base_ids=kb_ids, top_k=limit, **options
    )
    if len(kb_ids) < 2 or not ranked or limit < 2:
        return ranked
    represented = {hit.knowledge_base_id for hit in ranked}
    candidates = []
    try:
        for kb_id in [k for k in kb_ids if k not in represented][:_MAX_DIVERSITY_PROBES]:
            best = await hybrid_search_embeddings(db, knowledge_base_ids=[kb_id], top_k=1, **options)
            candidates.extend(hit for hit in best[:1] if hit.knowledge_base_id == kb_id)
    except Exception:
        # A failed probe costs the diversification, never the ranking found.
        logger.warning("Knowledge base diversity probe failed", exc_info=True)
        with contextlib.suppress(Exception):
            await db.rollback()
        return ranked
    return diversify_across_knowledge_bases(ranked, candidates, limit)


# ---------------------------------------------------------------------------
# Reusable search_knowledge tool factory
# ---------------------------------------------------------------------------


def make_search_knowledge_fn(
    db: AsyncSession,
    user_id: str,
    kb_ids: list[str],
    *,
    _embedding_provider_model: tuple | None = None,
) -> Callable[..., Coroutine[Any, Any, str]]:
    """Create a ``search_knowledge`` callable for use as an LLM tool.

    The returned coroutine captures *user_id* and *kb_ids* from the
    enclosing scope so it can be registered on a ``CallableToolExecutor``
    without repeating the same closure in every service module.

    Uses an independent DB session to avoid interfering with the caller's
    session, which may be mid-operation on the same asyncpg connection
    during the agentic loop.

    When *_embedding_provider_model* is supplied as ``(provider, model)``
    the embedding provider is reused across calls instead of being
    re-resolved from the DB on every search.

    Usage::

        fn = make_search_knowledge_fn(db, user_id, kb_ids)
        executor.register("search_knowledge", fn)
    """

    del db

    _cached_pm = _embedding_provider_model

    async def _search_knowledge(
        query: str, limit: int = 5, *, scope_kb_ids: list[str] | None = None
    ) -> str:
        """Search knowledge bases for relevant content.

        Uses hybrid search (vector similarity + BM25 full-text), diversified
        across knowledge bases, when available, falling back to pure vector
        search. *scope_kb_ids* narrows the search to some of the bound
        knowledge bases - never beyond them; it is keyword-only so no tool
        schema can hand it to the model.
        """
        from app.core.database import async_session as _session_factory

        search_kb_ids = (
            [k for k in kb_ids if k in set(scope_kb_ids)] if scope_kb_ids is not None else kb_ids
        )
        if not search_kb_ids:
            return "No relevant results found in knowledge bases."
        try:
            # Resolve the embedding provider and compute the query embedding
            # WITHOUT holding a pooled DB connection across the embed HTTP call.
            # A SQLAlchemy async session only checks out a connection on its
            # first query, so provider resolution (which issues one) runs in a
            # short throwaway session that closes before the embed; the search
            # session below is opened only for the DB query. Otherwise one
            # pooled connection is pinned per in-flight RAG search for the whole
            # embedding round-trip and concurrent searches exhaust the pool.
            if _cached_pm:
                embedding_provider, embedding_model = _cached_pm[:2]
            else:
                from app.llm.router import get_embedding_provider

                async with _session_factory() as _prov_db:
                    embedding_provider, embedding_model, _provider_id = (
                        await get_embedding_provider(_prov_db)
                    )
            query_embedding = await embedding_provider.embed(
                query, model=embedding_model
            )
            expected_dimensions = len(query_embedding)

            async with _session_factory() as search_db:
                hit_kb_ids: set[str] = set()

                # Try hybrid search first (vector + BM25 fusion)
                try:
                    # Do NOT pass user_id here: access control is already enforced
                    # upstream by restricting kb_ids to only the KBs the agent is
                    # allowed to read.  Adding a user_id filter on top of the KB ID
                    # filter would silently exclude embeddings that were created by a
                    # different user (e.g. group-shared KBs or KBs originally created
                    # by an admin), making newly attached KBs invisible even though
                    # the agent owner explicitly bound them.
                    hybrid_results = await _diversified_hybrid_search(
                        search_db,
                        query=query,
                        query_embedding=query_embedding,
                        kb_ids=search_kb_ids,
                        limit=limit,
                        expected_dimensions=expected_dimensions,
                        embedding_model=embedding_model,
                    )
                    if hybrid_results:
                        for r in hybrid_results:
                            if r.knowledge_base_id:
                                hit_kb_ids.add(r.knowledge_base_id)
                        rendered = await _render_search_results(
                            search_db, hybrid_results
                        )
                        await _increment_kb_query_count(hit_kb_ids)
                        return rendered
                except Exception:
                    # A persistent hybrid-path failure (broken tsvector index,
                    # migration drift, etc.) would otherwise silently and
                    # permanently downgrade RAG to vector-only with no signal.
                    logger.warning(
                        "Hybrid search failed, falling back to vector-only",
                        exc_info=True,
                    )
                    with contextlib.suppress(Exception):
                        await search_db.rollback()

                # Fallback: pure vector search
                results = await search_knowledge_bases(
                    search_db,
                    user_id,
                    search_kb_ids,
                    query_embedding,
                    top_k=limit,
                    expected_dimensions=expected_dimensions,
                    expected_model=embedding_model,
                )
                if not results:
                    return "No relevant results found in knowledge bases."
                for r in results:
                    if r.knowledge_base_id:
                        hit_kb_ids.add(str(r.knowledge_base_id))
                rendered = await _render_search_results(search_db, results)
                await _increment_kb_query_count(hit_kb_ids)
                return rendered
        except Exception as e:
            logger.error("Knowledge base search failed: %s", format_exception_message(e), exc_info=True)
            return "Knowledge base search failed. Please try again later."

    return _search_knowledge
