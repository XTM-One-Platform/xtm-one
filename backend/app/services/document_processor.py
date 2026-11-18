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

"""Document processing service – chunks and embeds documents into pgvector.

Called by the SAQ worker when a document is uploaded to a knowledge base.
Handles: chunking (tiktoken), embedding (OpenAI), storage (pgvector).

Deadlock prevention
-------------------
Multiple SAQ workers can process documents from the **same** knowledge base
concurrently (worker concurrency defaults to 10).  Each embedding INSERT
triggers a FK-check that acquires a ShareLock on the referenced KB row.
If the same transaction also UPDATEs the KB row (e.g. ``kb.chunk_count``),
it needs to promote that lock to ExclusiveLock — but another worker already
holds its own ShareLock, creating a lock-upgrade deadlock.

To avoid this:
  1. The KB object is **expunged** from the session immediately after reading
     its config so the ORM never flushes an UPDATE to the KB row.
  2. KB stats are recounted in a **separate, short transaction** after the
     main embedding commit succeeds.
  3. The whole operation is wrapped in a **retry loop** (``_MAX_RETRIES``
     attempts with exponential back-off + jitter) to recover from any
     residual transient errors, including DDL lock waves on ``embeddings``.
"""

import asyncio
import logging
import secrets
import time
import uuid

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm.exc import StaleDataError

from app.core.database import async_session
from app.core.hot_imports import ensure_hot_path_imports
from app.core.utils import format_exception_message, is_zero_norm_vector
from app.llm.identity import bind_llm_caller, llm_caller_scope
from app.llm.provider_access import ProviderNotUsableError, canonical_id
from app.models.embedding import Embedding
from app.models.knowledge_base import KnowledgeBase, KnowledgeDocument
from app.services import kb_figure_pass, kb_figures
from app.services.chunking import chunk_text
from app.services.kb_document_errors import (
    EMBEDDING_FAILED_EVENT,
    EMBEDDING_NOT_AVAILABLE_MESSAGE,
    KNOWLEDGE_BASE_NOT_FOUND_MESSAGE,
    NO_CHUNKS_MESSAGE,
    NO_CONTENT_MESSAGE,
    NO_VALID_CHUNKS_MESSAGE,
    classify_document_exception,
    classify_embedding_failures,
    dimension_mismatch_message,
    embedding_failed_details,
)
from app.services.kb_embedding_text import resolve_embedding_text_async
from app.services.kb_helpers import embedding_config_moved, log_kb_activity, schedule_embedding_retry

logger = logging.getLogger(__name__)

# 5 attempts (was 3): with the exponential schedule below the retries span
# roughly 10 + 20 + 40 + 60 s of sleep plus up to 30 s of statement_timeout
# lock-queue wait per attempt — a ~5 min window.  That is enough to ride
# out a plain (non-CONCURRENT) index build on ``embeddings`` (e.g. the
# HNSW ANN builds from c9d3e7f1a5b2 / ``_ensure_model_indexes``), which
# takes a SHARE lock blocking every DELETE/INSERT for the build duration
# on populated tables.  3 attempts spanned ~2 min and permanently failed
# documents mid-deploy.  Worst case ≈ 300 s stays well
# within ``worker_document_job_timeout`` (1200 s).
_MAX_RETRIES = 5

# Delay before re-queueing a document whose row lock could not be
# acquired via ``FOR UPDATE SKIP LOCKED``.  Long enough that the
# conflicting transaction (typically a KB-sync upsert) has had time to
# commit, short enough that the UI's "Re-indexing in progress" banner
# clears quickly.  Tunable from tests via ``monkeypatch``.
_LOCKED_DOC_REENQUEUE_DELAY_SECONDS = 30

# Retry backoff for transient DB errors inside the ``process_document``
# retry loop.  Exponential: ``BASE * 2^(attempt-1)`` capped at ``MAX``,
# then jittered to a random point in
# ``[base * (1 - JITTER), base * (1 + JITTER)]`` so concurrent workers
# re-trying after a DDL wave do not all hit the DB at the same instant.
#
# During Alembic migration waves the DDL lock on ``embeddings`` can hold
# for many seconds (or minutes during a crash-loop).  The old fixed
# ``attempt * 2`` schedule (2 s / 4 s) exhausted all retries before
# most DDL windows closed.  10 s / 20 s gives the migration significantly
# more time to release the lock without blocking the worker for too long.
_RETRY_BASE_DELAY_SECONDS: float = 10.0
_RETRY_MAX_DELAY_SECONDS: float = 60.0
_RETRY_JITTER_FACTOR: float = 0.25
# SystemRandom avoids flake8-bandit S311 and matches the jitter source
# already used by ``quota._compute_flush_retry_delay``.
_RETRY_RANDOM = secrets.SystemRandom()

# PostgreSQL SQLSTATEs that indicate transient lock / timeout contention
# and are therefore safe to retry on the ``embeddings`` table:
#   57014 — query_canceled (statement_timeout fired)
#   55P03 — lock_not_available (NOWAIT / SKIP LOCKED contention)
#   40001 — serialization_failure
#   40P01 — deadlock_detected
_RETRYABLE_DB_SQLSTATES: frozenset[str] = frozenset({"57014", "55P03", "40001", "40P01"})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_retryable_db_error(exc: Exception) -> bool:
    """Return True for transient DB errors that are safe to retry.

    Walks the full exception chain so SQLAlchemy / asyncpg wrapper layers
    do not hide the underlying PostgreSQL SQLSTATE.  Falls back to a string
    probe for robustness across driver versions.
    """
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        for attr in ("sqlstate", "pgcode"):
            code = getattr(cur, attr, None)
            if isinstance(code, str) and code.upper() in _RETRYABLE_DB_SQLSTATES:
                return True
        cur = cur.__cause__ or cur.__context__
    msg = str(exc).lower()
    return any(
        kw in msg
        for kw in (
            "deadlock",
            "serialization",
            "could not serialize",
            "statement timeout",
            "querycancelederror",
            "canceling statement due to statement timeout",
            "canceling statement due to lock timeout",
            "lock not available",
            "could not obtain lock on row",
        )
    )


def _compute_retry_delay(attempt: int) -> float:
    """Return exponential backoff + jitter for the *attempt*-th retry.

    ``attempt`` is 1-based (the first retry is attempt 1, not attempt 0).
    The exponential base is clamped to ``_RETRY_MAX_DELAY_SECONDS`` first,
    then the jitter window is clamped to the cap *before* sampling rather
    than clamping the sampled value afterwards: a post-sampling
    ``min(..., cap)`` would collapse every over-cap sample onto an
    exactly-cap point mass, losing the spread the jitter exists to provide
    (same rationale as ``quota._compute_flush_retry_delay``).
    """
    base = min(
        _RETRY_BASE_DELAY_SECONDS * (2 ** (attempt - 1)),
        _RETRY_MAX_DELAY_SECONDS,
    )
    low = base * (1 - _RETRY_JITTER_FACTOR)
    high = min(base * (1 + _RETRY_JITTER_FACTOR), _RETRY_MAX_DELAY_SECONDS)
    high = max(low, high)
    return _RETRY_RANDOM.uniform(low, high)


async def _recount_kb_stats(
    kb_id: uuid.UUID,
    *,
    embedding_model: str | None = None,
) -> None:
    """Atomically recount KB chunk_count in a separate short transaction.

    Uses a fresh session so it never contends with the embedding transaction.

    Note: ``kb_sync._recount_kb_stats`` is a related helper that recounts both
    ``document_count`` *and* ``chunk_count`` using the caller's session.  This
    variant intentionally opens its own session and uses a direct UPDATE
    statement to avoid deadlocks when called alongside concurrent embedding
    INSERTs.
    """
    try:
        async with async_session() as db:
            result = await db.execute(
                select(func.coalesce(func.sum(KnowledgeDocument.chunk_count), 0)).where(
                    KnowledgeDocument.knowledge_base_id == kb_id
                )
            )
            total_chunks = result.scalar() or 0

            update_values: dict[str, object] = {"chunk_count": total_chunks}
            if embedding_model:
                update_values["embedding_model"] = embedding_model
            await db.execute(
                update(KnowledgeBase)
                .where(KnowledgeBase.id == kb_id)
                .values(**update_values)
            )
            await db.commit()
    except Exception as e:
        logger.warning("Failed to recount KB stats for %s: %s", kb_id, format_exception_message(e))


async def _reenqueue_document_after_lock(document_id: str, *, on_behalf_of: str | None = None) -> None:
    """Best-effort re-enqueue of a document whose row lock was held.

    Called from :func:`_process_document_impl` when ``FOR UPDATE SKIP
    LOCKED`` returns nothing for an existing document —'s
    SKIP-LOCKED change.  Without this re-enqueue the document would
    remain ``pending`` indefinitely because SAQ does not retry a job
    function that returned cleanly.

    Failures here are swallowed (logged at WARNING) because the
    autonomous scheduler's stuck-document sweeper provides a second,
    time-based recovery layer — we never want a transient Redis error
    here to bubble up as an unrelated SAQ ``error`` status on a
    document that's otherwise fine.
    """
    try:
        from saq import Job

        from app.core.config import settings as _app_settings
        from app.services.kb_helpers import document_job_kwargs, relock_job_key
        from app.worker import queue

        await queue.enqueue(
            Job(
                function="process_document_job",
                kwargs=document_job_kwargs(document_id, on_behalf_of),
                # Distinct stable key from the primary enqueue path: this
                # re-enqueue fires while OUR OWN job for the document is
                # still active (it is the job calling this helper), so the
                # primary ``process-document-<id>`` key would be deduped
                # by SAQ and the retry silently dropped.  The ``-relock``
                # suffix keeps the retry alive while still deduping
                # repeated relock retries for the same document.
                key=relock_job_key(document_id),
                timeout=_app_settings.worker_document_job_timeout,
                retries=_app_settings.worker_document_job_retries,
                retry_delay=_app_settings.worker_document_job_retry_delay_seconds,
                retry_backoff=_app_settings.worker_document_job_retry_backoff,
                scheduled=int(time.time()) + _LOCKED_DOC_REENQUEUE_DELAY_SECONDS,
            )
        )
    except Exception as exc:
        logger.warning(
            "Failed to re-enqueue locked document %s — stuck-document sweeper "
            "will pick it up on the next tick: %s",
            document_id,
            format_exception_message(exc),
        )


async def _mark_document_error(document_id: str, exc: BaseException) -> None:
    """Best-effort: mark a document as errored in a fresh session.

    Intentionally updates only the document row. This helper is called from
    outer retry/error paths where we may not have a usable in-flight session
    (or KB context) to also record activity logs.

    The row stores the classified sentence, never ``str(exc)``: the caller logs
    the raw exception with the document id.
    """
    _code, sentence = classify_document_exception(exc)
    try:
        async with async_session() as db:
            result = await db.execute(
                select(KnowledgeDocument).where(
                    KnowledgeDocument.id == uuid.UUID(document_id)
                )
            )
            doc = result.scalar_one_or_none()
            if doc:
                doc.status = "error"
                doc.error_message = sentence
                # Reached via the outer retry wrapper for a non-DB / exhausted
                # failure (typically an embedding-provider error) — retryable,
                # so schedule an automatic re-embed with backoff.
                schedule_embedding_retry(doc, retryable=True)
                await db.commit()
    except Exception as exc:
        logger.warning("Failed to mark document %s as errored: %s", document_id, format_exception_message(exc))


# ---------------------------------------------------------------------------
# Public entry point (with retry)
# ---------------------------------------------------------------------------


async def process_document(document_id: str, *, on_behalf_of: str | None = None) -> int:
    """Process a single document: chunk → embed → store in pgvector.

    Retries up to ``_MAX_RETRIES`` times on transient DB errors (deadlock,
    serialization, statement/lock timeout during DDL waves).
    Returns the number of chunks created.

    The 1-based *attempt* counter is forwarded to
    :func:`_process_document_impl` so the duplicate-job guard there can
    distinguish a legitimate internal retry (where the row may already
    be ``processing`` from a previous attempt's commit, or ``error``
    from the inner except's bookkeeping commit) from a stale duplicate
    SAQ job (where the row was advanced by a *different* worker and we
    must bail). Without this distinction the guard silently defeats
    the retry loop on every transient DB error.

    The person the embed signs as (``bind_llm_caller``) is unbound on
    return: the job's next step (``run_auto_pass``) signs as its own.
    """
    with llm_caller_scope():
        return await _process_document_retrying(document_id, on_behalf_of=on_behalf_of)


async def _process_document_retrying(document_id: str, *, on_behalf_of: str | None) -> int:
    last_exc: Exception | None = None
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            return await _process_document_impl(document_id, attempt=attempt, on_behalf_of=on_behalf_of)
        except StaleDataError as exc:
            logger.warning(
                "Skipped embedding for document %s due to stale row update (likely deleted/replaced during sync): %s",
                document_id,
                format_exception_message(exc),
            )
            return 0
        except Exception as exc:
            last_exc = exc
            if attempt < _MAX_RETRIES and _is_retryable_db_error(exc):
                wait = _compute_retry_delay(attempt)
                logger.warning(
                    "Retryable DB error embedding doc %s (attempt %d/%d), retrying in %.1fs: %s",
                    document_id,
                    attempt,
                    _MAX_RETRIES,
                    wait,
                    format_exception_message(exc),
                )
                await asyncio.sleep(wait)
                continue
            # Not retryable or exhausted retries
            await _mark_document_error(document_id, exc)
            logger.error(
                "Failed to embed document %s: %s", document_id, format_exception_message(exc), exc_info=True
            )
            return 0

    # Safety net (should be unreachable)
    if last_exc:
        await _mark_document_error(document_id, last_exc)
        logger.error(
            "Failed to embed document %s: %s",
            document_id,
            format_exception_message(last_exc),
            exc_info=last_exc,
        )
    return 0


# ---------------------------------------------------------------------------
# Core implementation
# ---------------------------------------------------------------------------


def _embedding_people(
    on_behalf_of: str | None, added_by: object, kb_owner: object
) -> list[object]:
    """Everyone whose LLM provider exclusions a document job honours.

    The person the job names, when it names one: the document was just added
    or changed for them. A job that names no one - a re-embed
    (``kb_helpers.enqueue_reembedding``), a sweeper re-enqueue, a scheduled
    retry - embeds for the person recorded on the document (*added_by*) AND
    for the knowledge base's owner (*kb_owner*), so a company-managed
    knowledge base, which has no owner, still keeps a refused person's
    document off the provider that excludes them. ``[None]`` when no
    one is known: platform-level work, for the person the context acts for.
    """
    if on_behalf_of:
        return [on_behalf_of]
    people: list[object] = []
    seen: set[str] = set()
    for person in (added_by, kb_owner):
        key = canonical_id(person)
        if key and key not in seen:
            seen.add(key)
            people.append(person)
    return people or [None]


async def _process_document_impl(
    document_id: str, *, attempt: int = 1, on_behalf_of: str | None = None
) -> int:
    """Inner implementation — called by :func:`process_document` with retry.

    *attempt* is the 1-based retry attempt index from the outer wrapper.
    When the outer wrapper is calling us back to retry a transient DB
    error (``attempt > 1``), the row's status may legitimately be:

    * ``processing`` — the previous attempt's
      ``doc.status = "processing"; await db.commit()`` (at the top of
      this function) succeeded, then the embedding step failed before
      the final ``ready`` commit.
    * ``error`` — the inner ``except Exception`` block successfully
      committed ``status = "error"`` + the ``embedding_failed``
      activity log before re-raising.

    The duplicate-job guard below MUST NOT short-circuit those legit
    retries — it is gated on ``attempt == 1`` exactly for that reason. On retry attempts the SELECT
    ``FOR UPDATE SKIP LOCKED`` still serializes against any concurrent
    worker: if a *different* worker (e.g. a sweeper-induced second SAQ
    job) holds the row, our SELECT returns no row and we exit via the
    ``_reenqueue_document_after_lock`` path above.
    """
    # Parse the UUID once so the lock attempt and the follow-up existence
    # check use the same predicate (avoids the duplicate ``uuid.UUID(...)``
    # parse and keeps both queries trivially equivalent in the eyes of
    # the SQL planner).
    doc_uuid = uuid.UUID(document_id)
    async with async_session() as db:
        # Load document
        result = await db.execute(
            select(KnowledgeDocument)
            .where(KnowledgeDocument.id == doc_uuid)
            .with_for_update(skip_locked=True)
        )
        doc = result.scalar_one_or_none()
        if not doc:
            exists_result = await db.execute(
                select(KnowledgeDocument.id).where(
                    KnowledgeDocument.id == doc_uuid
                )
            )
            if exists_result.scalar_one_or_none():
                # SKIP LOCKED hit: another transaction holds a row lock on
                # this document right now (typically a KB sync upsert mid-
                # flight, or a concurrent reprocess).  Previously we just
                # logged INFO and returned 0 — that abandoned the job and
                # the document silently stayed ``pending`` forever because
                # SAQ does not retry a function that returned cleanly.  We
                # now schedule a fresh SAQ job for the same document with
                # a short delay so it gets processed once the conflicting
                # transaction commits and the row lock is released.
                logger.info(
                    "Document %s is currently locked by another transaction; "
                    "scheduling retry in %ds",
                    document_id,
                    _LOCKED_DOC_REENQUEUE_DELAY_SECONDS,
                )
                await _reenqueue_document_after_lock(document_id, on_behalf_of=on_behalf_of)
            else:
                # A document deleted while its job waited: deletion cancels the
                # queued job, but not one already dequeued or in flight.
                logger.debug(
                    "Document %s not found; skipping embedding (likely stale queue entry)",
                    document_id,
                )
            return 0

        # Idempotency guard against sweeper-induced duplicate SAQ jobs.
        #
        # The autonomous stuck-document sweeper re-enqueues ``pending``
        # docs older than ``STUCK_PENDING_THRESHOLD_SECONDS`` (5 min).
        # Under worker saturation a legitimately-queued doc can wait
        # longer than that — the sweeper would re-enqueue, queuing a
        # second job for the same document.  When the original worker
        # eventually picks up its job and flips the row to
        # ``processing`` (then releases the row lock on commit), the
        # second job's worker can acquire the row lock with
        # ``FOR UPDATE SKIP LOCKED`` and would otherwise proceed to
        # re-do the work.  The delete-and-insert swap at finalize
        # means no duplicate chunks land in pgvector, but the
        # user-visible cost is real:
        #
        #   * Duplicate embedding-provider calls burn API tokens.
        #   * The row flips ``ready → pending → ready`` (the second
        #     worker's transit) so the UI's "Re-indexing in progress"
        #     banner can flicker for the user.
        #   * If the second worker fails mid-flight it overwrites the
        #     first worker's clean ``ready`` with a misleading
        #     ``error`` + ``error_message``, even though the original
        #     worker had completed cleanly.
        #
        # We treat any non-``pending`` status as "another worker has
        # this — silently bail".  ``error`` is included so a previously
        # errored doc isn't auto-retried just because the sweeper
        # somehow re-enqueued it; admins re-process errored docs via
        # the explicit reprocess API, not by being re-picked off a
        # stale queue entry.
        #
        # **Gated on ``attempt == 1``** so the outer ``process_document``
        # retry loop can re-enter this function after a transient DB
        # error without the guard short-circuiting it. On attempt > 1
        # we know we are our own retry (the row's current status is
        # whatever our previous attempt left it in: ``processing`` if
        # the embedding step failed before the final commit, ``error``
        # if the inner except already committed the bookkeeping flip
        # before re-raising). A *different* worker holding the row
        # concurrently is independently handled by ``FOR UPDATE SKIP
        # LOCKED`` returning no row above, which routes to
        # ``_reenqueue_document_after_lock`` rather than here.
        if attempt == 1 and doc.status != "pending":
            logger.info(
                "Skipping document %s: status is %r (not 'pending') — "
                "treating as duplicate job (likely sweeper-induced under "
                "worker saturation); the original worker keeps the row.",
                document_id,
                doc.status,
            )
            return 0

        # A PDF with no text layer at all extracts to nothing: its figure pass,
        # when one is due, is what it is indexed from.
        if not doc.content and not kb_figures.awaits_pass(doc.figure_reading, doc.content):
            doc.status = "error"
            doc.error_message = NO_CONTENT_MESSAGE
            # Structural/permanent: re-embedding empty content always fails,
            # so do NOT schedule an automatic retry.
            schedule_embedding_retry(doc, retryable=False)
            await db.commit()
            return 0

        # Load the knowledge base for config
        kb_result = await db.execute(
            select(KnowledgeBase).where(KnowledgeBase.id == doc.knowledge_base_id)
        )
        kb = kb_result.scalar_one_or_none()
        if not kb:
            doc.status = "error"
            doc.error_message = KNOWLEDGE_BASE_NOT_FOUND_MESSAGE
            # Structural/permanent (KB deleted): never auto-retry.
            schedule_embedding_retry(doc, retryable=False)
            await db.commit()
            return 0

        # Capture IDs before the try block so they survive session rollback.
        # Company-managed KB embeddings are stored shared (user_id NULL) so
        # hybrid search keeps returning them for every user — the KB owner is
        # tracked on the KnowledgeBase row, not on each embedding.
        kb_id = kb.id
        kb_user_id = None if kb.is_company_managed else kb.user_id
        kb_chunk_size = kb.chunk_size
        kb_chunk_overlap = kb.chunk_overlap
        kb_html_ingestion = getattr(kb, "html_ingestion", None)
        kb_describe_figures = bool(getattr(kb, "describe_figures", False))
        doc_filename = doc.filename
        doc_content = doc.content or ""
        doc_figure_reading = getattr(doc, "figure_reading", None)
        doc_id_str = str(doc.id)
        # Record whom the document is embedded for, so a later re-embed, which
        # names no one, still honours that person's exclusions.
        if acting_person := canonical_id(on_behalf_of):
            doc.added_by_user_id = uuid.UUID(acting_person)
        embed_for = _embedding_people(on_behalf_of, doc.added_by_user_id, kb_user_id)

        # Evict KB from session — we only needed its config values.
        # This prevents the ORM from flushing an UPDATE to the KB row
        # during commit, which would deadlock with the FK SharedLocks
        # held by the concurrent embedding INSERTs.
        db.expunge(kb)

        # Mark as processing
        doc.status = "processing"
        await db.commit()

        try:
            # The figures of a PDF, when its KB describes them and a pass is
            # due. No transaction is open across it (the ``processing`` commit
            # above closed it, and the pass reads on sessions of its own); its
            # reading is committed on its own, so an embedding failure below
            # never pays for the pictures again.
            if kb_describe_figures and kb_figures.awaits_pass(doc_figure_reading, doc_content):
                previous_reading = doc_figure_reading
                doc_figure_reading = await kb_figure_pass.run_figure_pass(
                    previous_reading,
                    doc_content,
                    user_id=on_behalf_of or (str(kb_user_id) if kb_user_id else None),
                    fallback_user_id=str(kb_user_id) if kb_user_id else None,
                )
                doc.figure_reading = doc_figure_reading
                await db.commit()
                await kb_figure_pass.release_source(previous_reading, doc_figure_reading)

            # Chunk what the KB embeds for this document: the stored source,
            # an HTML document's rendered text when the KB opted in, a PDF's
            # text with its figure descriptions when the KB describes them.
            to_embed = await resolve_embedding_text_async(
                doc_content,
                doc_filename,
                kb_html_ingestion,
                figures=doc_figure_reading if kb_describe_figures else None,
            )
            chunks = chunk_text(
                to_embed.text,
                chunk_size=kb_chunk_size,
                chunk_overlap=kb_chunk_overlap,
            )

            # Keep this session-scoped variant separate from _mark_document_error:
            # here we still have KB context and a valid live session, so we also
            # record an embedding_failed activity entry atomically.
            #
            # ``retryable`` controls whether the failure schedules an automatic
            # re-embed: structural failures (no extractable text) default to
            # False so we never retry a document that can't produce chunks,
            # while a provider-side failure (e.g. "no vectors returned") passes
            # True so it self-heals once the embedding server is fixed.
            # The config the embedding calls ran on, once resolved.
            provider_config_id = None

            async def _mark_document_unprocessable(
                reason: str, *, retryable: bool = False
            ) -> int:
                logger.warning(
                    "Skipping embedding for document %s (%s): %s",
                    doc_id_str,
                    doc_filename,
                    reason,
                )
                # ``chunk_count`` is left as is: a document keeps the vectors it
                # had; a content change zeroed both before re-queuing.
                doc.status = "error"
                doc.error_message = reason
                schedule_embedding_retry(doc, retryable=retryable)
                await log_kb_activity(
                    db,
                    kb_id,
                    EMBEDDING_FAILED_EVENT,
                    details=embedding_failed_details(doc_filename, reason),
                )
                await db.commit()
                # Failed on a provider an admin switched away from meanwhile:
                # retry now. Asked after the commit, holding no connection: a
                # switch committed later has its retry pass see this error.
                if provider_config_id is not None and await embedding_config_moved(provider_config_id):
                    schedule_embedding_retry(doc, retryable=True, immediately=True)
                    await db.commit()
                return 0

            if not chunks:
                return await _mark_document_unprocessable(NO_CHUNKS_MESSAGE)

            # Embed chunks in batches for performance.
            # Sequential embedding is the #1 bottleneck for document processing;
            # batching with bounded concurrency speeds this up dramatically.
            embed_batch_size = 8  # Concurrent embedding requests per batch

            valid_chunks = [c for c in chunks if len(c.strip()) >= 10]
            if not valid_chunks:
                return await _mark_document_unprocessable(NO_VALID_CHUNKS_MESSAGE)

            # Get embedding provider and model from platform settings.
            # Offload the first-use import to a thread so the heavy
            # ``app.llm.router`` → provider SDK chain does not block the
            # event loop.
            await ensure_hot_path_imports("app.llm.router")
            from app.llm.router import get_embedding_provider

            # The document is embedded for whoever added it, else for the
            # person recorded on it and the knowledge base's owner
            # (``_embedding_people``): a provider that excludes any of them
            # never receives it, and the refusal is final - an automatic retry
            # would meet the same exclusion.
            try:
                for person in embed_for:
                    embedding_provider, platform_embed_model, provider_config_id = await get_embedding_provider(
                        db, user_id=person
                    )
            except ProviderNotUsableError:
                return await _mark_document_unprocessable(EMBEDDING_NOT_AVAILABLE_MESSAGE)
            provider_name = getattr(embedding_provider, "provider_name", None)
            # A provider that signs each call as a person signs as whoever added
            # the document, else (also when they are gone or deactivated) the
            # KB's owner, else the service identity (company-managed KBs).
            if not await bind_llm_caller(db, on_behalf_of):
                await bind_llm_caller(db, kb_user_id)
            # The platform dimension ``get_embedding_provider`` set on the provider
            # (``None``: native size, except Custom OpenAI's ``EMBEDDING_DIMENSIONS``
            # fallback); the convergence guard below runs only when it is set.
            expected_dims = getattr(embedding_provider, "embedding_dimensions", None)
            # What each failed call raised: when no chunk could be embedded the
            # document's error names their cause (a timeout, a provider error),
            # not merely the absence of vectors.
            embed_failures: list[Exception] = []

            async def _embed_one(chunk: str) -> Embedding | None:
                try:
                    vec = await embedding_provider.embed(
                        chunk, model=platform_embed_model
                    )
                    if not vec:
                        # Provider returned an empty list/None — semantically
                        # different from a zero-norm vector below (which is a
                        # well-shaped vector that just happens to be useless).
                        # Distinct log lines make it easy to tell from grep
                        # whether the provider is silently dropping inputs
                        # vs. degrading to all-zeros.
                        logger.warning(
                            "Embedding provider returned empty vector (no values) for %s",
                            doc_filename,
                        )
                        return None
                    vec_values = [float(v) for v in vec]
                    if is_zero_norm_vector(vec_values):
                        logger.warning(
                            "Embedding provider returned zero-norm vector "
                            "(all components ≈ 0, %d dims) for %s",
                            len(vec_values),
                            doc_filename,
                        )
                        return None
                    return Embedding(
                        user_id=kb_user_id,
                        knowledge_base_id=kb_id,
                        source_type="document",
                        source_id=doc_id_str,
                        embedding_provider=provider_name,
                        embedding_provider_id=provider_config_id,
                        embedding_model=platform_embed_model,
                        embedding_dimensions=len(vec_values),
                        content=chunk,
                        embedding=vec_values,
                    )
                except Exception as e:
                    embed_failures.append(e)
                    logger.warning(
                        "Failed to embed chunk from %s (document %s): %s",
                        doc_filename,
                        doc_id_str,
                        format_exception_message(e),
                    )
                    return None

            records: list[Embedding] = []
            for i in range(0, len(valid_chunks), embed_batch_size):
                batch = valid_chunks[i : i + embed_batch_size]
                results = await asyncio.gather(*[_embed_one(c) for c in batch])
                records.extend(r for r in results if r is not None)

                # Dimension-convergence guard.  When the platform has an explicit
                # embedding dimension configured and the provider produced a
                # *different* size, re-embedding can never reconcile it — it is a
                # configuration mismatch (wrong Vector Dimensions for the model),
                # not a transient error.  Marking the document as a *non-retryable*
                # error with an actionable message stops the silent "permanently
                # stale" loop where every (re-)embed stores mismatched vectors that
                # the dashboard keeps flagging as stale and that "Re-embed all" can
                # never clear.  Correcting the platform embedding config re-queues
                # every document via ``_reset_kb_embeddings_for_new_model``, so a
                # doc parked here is never stranded once the dimension is fixed.
                #
                # Checked after every batch, so a model that cannot produce the
                # configured size costs one batch, not the whole document. The
                # mismatched records are discarded: they are added only at the swap.
                if expected_dims is not None:
                    produced_dims = sorted(
                        {r.embedding_dimensions for r in records if r.embedding_dimensions}
                    )
                    if produced_dims and produced_dims != [expected_dims]:
                        produced_desc = ", ".join(str(d) for d in produced_dims)
                        return await _mark_document_unprocessable(
                            dimension_mismatch_message(
                                platform_embed_model, produced_desc, expected_dims
                            ),
                            retryable=False,
                        )

            if not records:
                # Provider-side failure (misconfigured / down / timing out /
                # returned empty vectors) — retryable so the doc self-heals
                # once fixed. Every chunk either raised or came back empty.
                _code, sentence = classify_embedding_failures(
                    embed_failures,
                    returned_empty=len(embed_failures) < len(valid_chunks),
                )
                return await _mark_document_unprocessable(sentence, retryable=True)

            chunk_count = len(records)

            # Update document status (KB row is NOT in the session — no
            # conflicting UPDATE will be flushed alongside the embedding INSERTs).
            #
            # Re-acquire a row-level lock on the document before flipping it
            # to ``ready`` so two concurrent workers can't both mark the
            # same document done (the initial ``FOR UPDATE SKIP LOCKED`` at
            # load time was released by the ``processing`` commit above).
            #
            # ``skip_locked=True`` (not ``nowait=True``) is essential here:
            # when multiple workers race to finalize the same document,
            # ``nowait=True`` lets one worker acquire the lock and proceed,
            # but every *losing* worker raises ``LockNotAvailableError``.
            # That error propagates into the outer retry /
            # ``_mark_document_error`` path, which runs in a fresh unlocked
            # session and can overwrite the winner's already-committed
            # ``ready`` with ``error`` (and floods the logs with lock errors
            # under contention). With ``skip_locked=True`` the losing workers
            # instead get no row back, roll back their pending embeddings, and
            # return 0 — leaving the winner's commit untouched.
            #
            # ``no_autoflush``: nothing of this run is written before we own
            # the lock (the embeddings are added only once we hold it, below).
            with db.no_autoflush:
                relock_result = await db.execute(
                    select(KnowledgeDocument.id)
                    .where(KnowledgeDocument.id == doc.id)
                    .with_for_update(skip_locked=True)
                )
            if not relock_result.scalar_one_or_none():
                # The re-lock SELECT returned no row, which happens in two
                # distinct cases:
                #   * the row is locked by another transaction (SKIP LOCKED
                #     skipped it). Often that's the worker that wins the race
                #     and commits ``ready``; if instead an unrelated
                #     transaction holds it (KB sync / reprocess), the row
                #     stays ``processing`` and the stuck-document sweeper
                #     re-enqueues it on a later tick.
                #   * the row was concurrently deleted (the document or its
                #     KB was removed mid-flight), so there is nothing left to
                #     finalize.
                # We can't tell the cases apart here and don't need to: in
                # every case we discard our pending session changes and exit
                # cleanly, so we never double-commit embeddings or write
                # against a vanished row.
                await db.rollback()
                logger.info(
                    "Document %s: re-lock returned no row (locked by another "
                    "transaction or concurrently deleted); discarding local "
                    "embeddings without finalizing",
                    document_id,
                )
                return 0
            # The previous vectors are swapped in the same transaction: searches
            # keep them until it commits, and a refused or failed run leaves them.
            # A content change deletes its own before re-queuing.
            await db.execute(
                delete(Embedding).where(
                    Embedding.source_type == "document",
                    Embedding.source_id == doc_id_str,
                )
            )
            for record in records:
                db.add(record)
            doc.status = "ready"
            doc.chunk_count = chunk_count
            doc.error_message = None
            # Successful embed clears any prior auto-retry bookkeeping so a
            # future failure (e.g. after a content change / re-sync) starts
            # with a fresh backoff budget.
            doc.embedding_retry_count = 0
            doc.embedding_next_retry_at = None

            details = f"Embedded '{doc_filename}' — {chunk_count} chunk(s)"
            if to_embed.html_note:
                details += f", {to_embed.html_note}"
            if to_embed.figure_note:
                details += f", {to_embed.figure_note}"
            await log_kb_activity(
                db,
                kb_id,
                "embedding_completed",
                details=details,
                document_count=1,
            )

            await db.commit()

            # Recount KB stats in a separate short transaction to avoid
            # holding locks on the KB row during the embedding commit.
            await _recount_kb_stats(kb_id, embedding_model=platform_embed_model)

            logger.info(
                "Processed document '%s': %d chunks embedded", doc_filename, chunk_count
            )
            return chunk_count

        except Exception as e:
            # Rollback the failed transaction before using the session again
            await db.rollback()

            try:
                # The raw text is logged with the document id by the retry
                # wrapper this re-raises into; the row holds the classified
                # sentence only.
                _code, sentence = classify_document_exception(e)
                doc.status = "error"
                doc.error_message = sentence
                # Heavy embedding step raised (provider/network/transient) —
                # retryable, so schedule an automatic re-embed with backoff.
                schedule_embedding_retry(doc, retryable=True)
                await log_kb_activity(
                    db,
                    kb_id,
                    EMBEDDING_FAILED_EVENT,
                    details=embedding_failed_details(doc_filename, sentence),
                )
                await db.commit()
            except Exception:
                await db.rollback()

            raise
