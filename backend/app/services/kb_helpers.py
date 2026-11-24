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

"""Knowledge-base helpers — document enqueue + activity logging.

These live in the services layer so both API routes and other services
(kb_sync, document_processor, kb_sync_scheduler) can import them without
a cross-layer dependency on ``api.v1.helpers``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import time
import uuid
from collections.abc import Iterable, Iterator, Sequence
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import String, and_, cast, delete, event, func, or_, select, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session, SessionTransaction

from app.core.config import settings as app_settings
from app.core.database import async_session
from app.core.session_transactions import (
    enclosing_transaction,
    innermost_transaction,
    transaction_within,
)
from app.core.utils import format_exception_message
from app.models.embedding import Embedding
from app.models.knowledge_base import KBActivityLog, KnowledgeBase, KnowledgeDocument
from app.services.kb_document_errors import (
    PLATFORM_SENTENCES,
    PROVIDER_FAILURE_SENTENCES,
    WORKER_TIMEOUT_OPENING,
    worker_timeout_message,
)

logger = logging.getLogger(__name__)

# Filename extension -> KnowledgeDocument.source_type mapping shared by the
# upload endpoint and the skill-package reference materializer, so the
# download media-type mapping and UI labels stay consistent regardless of
# how the document entered the KB.
_SOURCE_TYPE_BY_EXTENSION = {
    "md": "markdown",
    "pdf": "pdf",
    "docx": "docx",
    "pptx": "pptx",
    "xlsx": "xlsx",
    "xls": "xls",
    "csv": "csv",
    "json": "json",
}


def source_type_for_filename(filename: str) -> str:
    """Derive a ``KnowledgeDocument.source_type`` from a filename extension."""
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return _SOURCE_TYPE_BY_EXTENSION.get(ext, "text")


async def reset_changed_document(
    db: AsyncSession,
    doc: KnowledgeDocument,
    *,
    kb: KnowledgeBase | None = None,
    vectors_deleted: bool = False,
) -> None:
    """Queue *doc*, whose content changed, for a fresh embed without its old vectors.

    The processor keeps a document's vectors until it replaces them, so
    old text would stay searchable if the new one failed to embed. *kb* is the
    document's KB when its ``chunk_count`` should drop the old chunks now;
    *vectors_deleted* when the caller deleted them its own way (KB sync).
    """
    if not vectors_deleted:
        await db.execute(
            delete(Embedding).where(
                Embedding.knowledge_base_id == doc.knowledge_base_id,
                Embedding.source_type == "document",
                Embedding.source_id == str(doc.id),
            )
        )
    if kb is not None:
        kb.chunk_count = max(0, (kb.chunk_count or 0) - (doc.chunk_count or 0))
    # ``pending`` is required: the processor's duplicate-job guard skips any
    # document that is not ``pending`` on its first attempt.
    doc.status = "pending"
    doc.error_message = None
    doc.chunk_count = 0
    # New content earns a fresh automatic-retry budget.
    doc.embedding_retry_count = 0
    doc.embedding_next_retry_at = None


async def apply_document_write_timeout(db: AsyncSession) -> None:
    """Raise *db*'s ``statement_timeout`` for one document write transaction.

    A document update deletes the document's old vectors and writes its new
    source in one transaction; on a large document that runs past the 30 s pool
    default (``core/database.py``) and is cancelled with SQLSTATE 57014, which
    fails the same way on every retry. The bound is the KB write timeout
    (``kb_sync_timeout_secs``), the one sync writes already run under;
    ``is_local`` scopes it to the transaction, so the pooled connection gets
    its default back at commit. A non-positive setting keeps the pool default.
    """
    seconds = int(getattr(app_settings, "kb_sync_timeout_secs", 0) or 0)
    if seconds <= 0:
        return
    from sqlalchemy import text

    # An int literal (never user input): SET LOCAL takes no bind parameter.
    timeout_ms = min(seconds * 1000, 2_147_483_647)
    await db.execute(text(f"SET LOCAL statement_timeout = {timeout_ms:d}"))


async def bump_kb_revision(db: AsyncSession, *kb_ids: uuid.UUID | str) -> None:
    """Move the content revision of each knowledge base in *kb_ids*.

    ``KnowledgeBase.revision`` is in the evaluation reuse fingerprint of a
    ``full_loop`` run, so a prediction made against a KB is reused only while
    its documents are unchanged. Every writer that adds, edits or deletes a
    document of a KB that outlives the write calls this in the transaction
    that writes the document:

    - adding a document, whoever adds it (``kb_documents.insert_document``:
      the REST URL crawl, text add and upload, a file the ``store_knowledge``
      tool stores, a workspace's Sources);
    - REST: document edit (content or name), document delete, force re-sync
      purge (``api/v1/knowledge_bases.py``);
    - sync ingestion (``kb_sync._recount_kb_stats``, when the sync added,
      changed or deleted a document - a re-sync of an unchanged source leaves
      it alone - and per changed page for a website crawl, which commits page
      by page);
    - the agent tools ``store_knowledge`` (text) and
      ``update_knowledge_document`` (``builtin_tools/kb.py``), the agent
      builder's ``add_kb_document`` and ``delete_kb_document``;
    - a workset's corpus writes (``workset._write_document``,
      ``_delete_document``);
    - a chat attachment copied into the agent's KB (``api/v1/chat.py``);
    - the built-in KB seed (``agents/seed.py``).

    Not called where the KB is created or deleted in the same transaction (a
    new KB id, or none, changes the fingerprint by itself), nor by re-embedding
    resets that leave the content as it was: the fingerprint reads
    ``embedding_model`` directly.

    The increment runs in SQL, so concurrent writers never lose one; the
    in-session ``KnowledgeBase`` objects keep their loaded value, so nothing
    may read the revision back from them or assign it in Python. The UPDATE
    holds the KB row lock until the transaction ends - the lock the writer's
    own ``document_count`` update already takes: call it next to that write,
    never ahead of a long step in the same transaction.
    """
    ids = sorted({uuid.UUID(str(kb_id)) for kb_id in kb_ids})
    if not ids:
        return
    await db.execute(
        update(KnowledgeBase)
        .where(KnowledgeBase.id.in_(ids))
        .values(revision=KnowledgeBase.revision + 1)
        .execution_options(synchronize_session=False)
    )


# Marker embedded in the ValueError ``sync_website`` raises when a crawl
# produces zero documents and every observed failure signal was permanent
# (404/403/401, malformed URL, redirect loop — no transient failures, no
# throttled recoveries).  The scheduler's ``_is_permanent_sync_error``
# matches on this marker for ``source_type="website"`` so the run feeds the
# strict ``sync_perm_fail_count`` quarantine budget.  A
# dedicated marker keeps the persisted ``last_sync_error`` honest — the
# crawler aggregates many per-URL failures and cannot claim a literal
# "404 Not Found" when the permanent errors may have been 403s or malformed
# URLs.  Lives here (not in kb_sync / kb_sync_scheduler) so both modules can
# import it without a cross-module dependency.
WEBSITE_ALL_PERMANENT_FAILURES_MARKER = (
    "every failed fetch hit a permanent HTTP error"
)


@contextlib.contextmanager
def kb_sync_log_context(
    *,
    kb_id: uuid.UUID | str | None = None,
    kb_name: str | None = None,
    sync_source_id: uuid.UUID | str | None = None,
    source_type: str | None = None,
) -> Iterator[None]:
    """Bind KB identifiers to the structlog context for a sync operation.

    Every log record emitted inside the ``with`` block — including the deep
    provider-level warnings in ``kb_sync.py`` (GitHub branch fallbacks, Notion
    rate limits, per-file failures, website crawl aborts) — automatically
    carries ``kb_id`` / ``kb_name`` (and, when known, ``sync_source_id`` /
    ``source_type``), so operators can act on a log line without grepping the
    database for which KB produced it (issue context: a bare "GitHub tree
    fetch 404 ... retrying with resolved default_branch" warning is not
    actionable without knowing which KB source to fix).

    Use at every KB-sync entry point: the scheduler (``execute_sync_source``),
    the manual sync API endpoints, and the background trigger/force-resync
    path. ``None`` values are skipped so partial context is fine. The previous
    values of the bound keys are restored on exit (via ``bound_contextvars``),
    so nesting inside an assignment-run context never clobbers it.
    """
    ctx = {
        key: str(value)
        for key, value in (
            ("kb_id", kb_id),
            ("kb_name", kb_name),
            ("sync_source_id", sync_source_id),
            ("source_type", source_type),
        )
        if value is not None
    }
    with structlog.contextvars.bound_contextvars(**ctx):
        yield


# How long a document may stay in ``pending`` (just queued) before the
# autonomous scheduler considers its SAQ job lost and re-enqueues it.
# Tuned to be well above the normal "enqueue → worker dequeue → start
# processing" latency (a few seconds even under load) but low enough
# that the user-visible "Re-indexing in progress" banner clears within
# a few minutes after a sync.
STUCK_PENDING_THRESHOLD_SECONDS = 5 * 60

# Safety margin between ``worker_document_job_timeout`` and the
# ``processing``-arm sweep cutoff.  Must be large enough that a job
# running right up to its timeout cannot be yanked by the sweeper a
# tick early.  10 min is comfortably above the slowest observed
# embedding-batch latencies in production.
STUCK_PROCESSING_THRESHOLD_MARGIN_SECONDS = 10 * 60


def _processing_threshold_seconds() -> int:
    """Derive the ``processing``-arm sweep cutoff from worker config.

    Hard-coding this (the original 30 min) was unsafe: an operator
    bumping ``worker_document_job_timeout`` above the literal would
    let the sweeper reset and re-enqueue a legitimately-running
    embedding job before SAQ ever times it out.  Reading the timeout
    at call-time keeps the margin honest regardless of how the
    setting is configured.
    """
    return (
        app_settings.worker_document_job_timeout
        + STUCK_PROCESSING_THRESHOLD_MARGIN_SECONDS
    )


# Cap on how many stuck documents we re-enqueue per sweep tick.  Without
# this a misconfigured deployment (e.g. wrong embedding provider) could
# have us re-enqueue thousands of documents per minute, hammering the
# worker and Redis.
_STUCK_DOCUMENT_BATCH_SIZE = 200

# Claim + commit stuck-document status updates in smaller chunks so the
# implicit ORM executemany UPDATE stays below statement-timeout-prone
# payload sizes during large backlogs.  Each chunk is
# claimed by its own ``SELECT ... FOR UPDATE SKIP LOCKED`` and committed
# before the next chunk is claimed, so the row locks never outlive the
# transaction that mutates the rows (see ``recover_stuck_documents``).
_STUCK_UPDATE_COMMIT_CHUNK_SIZE = 50

# Upper bound for the SAQ enqueue round-trip.  The enqueue runs on the
# web process (upload endpoints, KB sync, the autonomous sweepers); a
# silently-dead Redis socket without this bound would park the awaiting
# coroutine forever — freezing the entire autonomous tick.  A timeout
# converts that pathology into the normal "enqueue failed" path: the
# document stays ``pending`` and the stuck-document sweeper retries it.
_ENQUEUE_TIMEOUT_SECONDS = 30


def document_job_key(doc_id: str) -> str:
    """Stable SAQ job key for a document embedding job.

    Keying jobs by document (instead of SAQ's default random uuid per
    enqueue) makes every enqueue idempotent: SAQ refuses to enqueue a
    job whose key is already queued/active/scheduled-for-retry.  That
    property is load-bearing for the recovery sweeps — without it, a
    stalled or saturated worker turns the 1-minute autonomous tick into
    an unbounded duplicate-job generator (the same N stuck documents
    re-enqueued forever, each sweep adding N new Redis jobs that the
    worker must later chew through and discard one by one).
    """
    return f"process-document-{doc_id}"


def relock_job_key(doc_id: str) -> str:
    """SAQ job key of the retry scheduled when a document's row was locked."""
    return f"{document_job_key(doc_id)}-relock"


# Job keys looked up per MGET when cancelling: two per document.
_CANCEL_LOOKUP_CHUNK = 500


async def cancel_document_processing_jobs(doc_ids: Iterable[str]) -> int:
    """Best-effort cancellation of queued embedding jobs for deleted documents.

    Every embedding job carries one of the document's two stable keys, so the
    jobs are looked up by key (one MGET per chunk) rather than by scanning every
    job the queue holds. A job already dequeued finds the document gone and
    skips it. Each queue round trip is bounded like an enqueue: the queue's
    connection has no socket timeout, and a hung one ends in the warning below.
    A deletion never calls this itself: it goes through
    :func:`cancel_document_jobs_after_commit`, which runs it once the deletion
    committed.
    """
    doc_id_set = {str(doc_id) for doc_id in doc_ids if doc_id}
    if not doc_id_set:
        return 0

    cancelled = 0
    try:
        from saq.job import Status

        from app.worker import queue

        keys = [key for doc_id in sorted(doc_id_set) for key in (document_job_key(doc_id), relock_job_key(doc_id))]
        for start in range(0, len(keys), _CANCEL_LOOKUP_CHUNK):
            jobs = await asyncio.wait_for(
                queue.jobs(keys[start : start + _CANCEL_LOOKUP_CHUNK]), timeout=_ENQUEUE_TIMEOUT_SECONDS
            )
            for job in jobs:
                if job is None or job.status != Status.QUEUED:
                    continue
                await asyncio.wait_for(queue.abort(job, "document_deleted"), timeout=_ENQUEUE_TIMEOUT_SECONDS)
                cancelled += 1
    except Exception as exc:
        # ``exc_info=exc`` preserves the traceback in the warning record
        # (vs. the bare ``%s`` interpolation that was here before, which
        # only emitted ``str(exc)`` and lost the call stack). Important
        # for diagnosing whether the failure is on Redis connection
        # setup, the ``jobs`` key lookup, or the ``abort()``
        # pipeline — each one looks the same in the message body.
        logger.warning(
            "Failed to cancel queued document jobs for %d deleted document(s): %s",
            len(doc_id_set),
            format_exception_message(exc),
            exc_info=exc,
        )
        return 0

    if cancelled:
        logger.info(
            "Cancelled %d queued document embedding job(s) for %d deleted document(s)",
            cancelled,
            len(doc_id_set),
        )
    return cancelled


#: ``Session.info`` key: ``(transaction, document ids)`` waiting for the commit.
_JOBS_TO_CANCEL_ON_COMMIT = "_kb_document_jobs_to_cancel_on_commit"

#: Strong references to the cancellations started by a commit: asyncio holds
#: only weak ones, and a collected task would never cancel anything.
_cancellations_in_flight: set[asyncio.Task[int]] = set()


def cancel_document_jobs_after_commit(db: AsyncSession | Session, doc_ids: Iterable[str | uuid.UUID]) -> None:
    """Cancel the queued embedding jobs of *doc_ids* once *db* commits their deletion.

    The one way a deletion cancels document jobs: call it in the
    transaction that deletes the documents, where the deletion is decided. The
    ids wait on the session, tagged with the innermost transaction that can end
    on its own, and :func:`cancel_document_processing_jobs` starts only once
    the root transaction commits - as a task of its own, so neither the commit
    nor the locks it held (the platform-wide agent-delete lock) nor the caller
    ever wait on the queue. A transaction that ends without committing (a
    rollback, a failed COMMIT, a retried attempt, a SAVEPOINT rolled back, a
    session closed) drops its ids: a deletion that did not happen never cancels
    the jobs of documents that still exist. Never raises into the deletion.
    """
    ids = {str(doc_id) for doc_id in doc_ids if doc_id}
    if not ids:
        return
    try:
        if isinstance(db, AsyncSession):
            session = db.sync_session
        elif isinstance(db, Session):
            session = db
        else:
            raise TypeError(f"not a session: {type(db).__name__}")
        _listen_for_document_job_commits()
        # Begun here when nothing ran yet, so the ids belong to the
        # transaction the deletion runs in, never to one that ended before it.
        txn = innermost_transaction(session) or session.begin()
        session.info.setdefault(_JOBS_TO_CANCEL_ON_COMMIT, []).append((txn, ids))
    except Exception as exc:
        logger.warning(
            "Could not defer the cancellation of the queued jobs of %d deleted document(s) to the "
            "commit; a job that runs finds its document gone and skips it: %s",
            len(ids),
            format_exception_message(exc),
        )


def _listen_for_document_job_commits() -> None:
    if not event.contains(Session, "after_commit", _cancel_committed_document_jobs):
        event.listen(Session, "after_commit", _cancel_committed_document_jobs)
    if not event.contains(Session, "after_transaction_end", _forget_uncommitted_document_jobs):
        event.listen(Session, "after_transaction_end", _forget_uncommitted_document_jobs)


def _cancel_committed_document_jobs(session: Session) -> None:
    """Start the cancellation of what the root transaction just committed.

    ``after_commit`` fires for a SAVEPOINT release too, while the session still
    reports that savepoint as its nested transaction: its ids then ride on the
    enclosing transaction, which may still roll back. Never raises: the COMMIT
    is durable by now.
    """
    try:
        pending = session.info.pop(_JOBS_TO_CANCEL_ON_COMMIT, None)
        if not pending:
            return
        released = session.get_nested_transaction()
        if released is not None:
            parent = enclosing_transaction(released.parent)
            session.info[_JOBS_TO_CANCEL_ON_COMMIT] = [
                (parent if transaction_within(txn, released) else txn, ids) for txn, ids in pending
            ]
            return
        doc_ids = set().union(*(ids for _txn, ids in pending))
        task = asyncio.get_running_loop().create_task(cancel_document_processing_jobs(doc_ids))
        _cancellations_in_flight.add(task)
        task.add_done_callback(_cancellations_in_flight.discard)
    except Exception as exc:
        logger.warning(
            "Deleted documents committed but their queued jobs were not cancelled; a job that "
            "runs finds its document gone and skips it: %s",
            format_exception_message(exc),
        )


def _forget_uncommitted_document_jobs(session: Session, transaction: SessionTransaction) -> None:
    """Drop the ids of a transaction that ended without committing them.

    A root commit has taken its ids by now and a released SAVEPOINT has handed
    its ids on, so whatever is still tagged with the ended transaction, or one
    begun inside it, was discarded. Once the root ends nothing can commit the
    rest any more. Never raises, and fails towards cancelling nothing.
    """
    try:
        pending = session.info.get(_JOBS_TO_CANCEL_ON_COMMIT)
        if not pending:
            return
        if transaction.parent is None:
            del session.info[_JOBS_TO_CANCEL_ON_COMMIT]
            return
        kept = [(txn, ids) for txn, ids in pending if not transaction_within(txn, transaction)]
        if len(kept) != len(pending):
            session.info[_JOBS_TO_CANCEL_ON_COMMIT] = kept
    except Exception as exc:
        session.info.pop(_JOBS_TO_CANCEL_ON_COMMIT, None)
        logger.warning(
            "Dropped the queued document job cancellations of a session: %s",
            format_exception_message(exc),
        )


def document_job_kwargs(doc_id: str, on_behalf_of: str | None = None) -> dict[str, str]:
    """The kwargs of a ``process_document_job``: the document, and for whom.

    *on_behalf_of* (the person who added or changed the document) travels only
    when known, so the job embeds within that person's provider exclusions;
    without it the job embeds for the person recorded on the document
    and for the knowledge base's owner.
    """
    kwargs = {"document_id": doc_id}
    if on_behalf_of:
        kwargs["on_behalf_of"] = on_behalf_of
    return kwargs


async def refuse_documents_for_excluded_person(doc_ids: list[str], person: str | None) -> bool:
    """Mark *doc_ids* unembeddable when the embedding provider excludes *person*.

    Run at enqueue time, while the person is known: a refused document
    leaves ``pending`` at once, so no later re-enqueue - the stuck-document
    sweeper, a lost or failed job - can embed it without its person. The
    person is recorded on the document (``added_by_user_id``), so a later
    re-embed, which binds no one, still refuses it. Returns ``True``
    when the documents were refused (and must not be queued). Any other
    failure returns ``False``: the job refuses them in turn, since it carries
    the same person.
    """
    if not person or not doc_ids:
        return False
    from app.llm.provider_access import ProviderNotUsableError, canonical_id
    from app.llm.router import get_embedding_provider
    from app.services.kb_document_errors import EMBEDDING_NOT_AVAILABLE_MESSAGE

    try:
        async with async_session() as db:
            try:
                await get_embedding_provider(db, user_id=person)
                return False
            except ProviderNotUsableError:
                pass
            rows = await db.execute(
                select(KnowledgeDocument).where(
                    KnowledgeDocument.id.in_([uuid.UUID(str(d)) for d in doc_ids])
                )
            )
            for doc in rows.scalars().all():
                doc.status = "error"
                doc.error_message = EMBEDDING_NOT_AVAILABLE_MESSAGE
                if refused_for := canonical_id(person):
                    doc.added_by_user_id = uuid.UUID(refused_for)
                schedule_embedding_retry(doc, retryable=False)
            await db.commit()
    except Exception as exc:
        logger.warning(
            "Could not check the embedding provider's exclusions for %d document(s); "
            "the job checks them: %s",
            len(doc_ids),
            format_exception_message(exc),
        )
        return False
    logger.info(
        "Not embedding %d document(s): the embedding provider excludes the person they were added for",
        len(doc_ids),
    )
    return True


async def enqueue_document_processing(
    doc_id: str,
    *,
    embedding_ready: bool | None = None,
    on_behalf_of: str | None = None,
    exclusions_checked: bool = False,
) -> bool:
    """Enqueue a single document for background chunk + embed processing.

    Returns ``True`` when the SAQ enqueue actually succeeded, ``False``
    when it was skipped (no embedding provider configured) or failed
    (e.g. Redis blip — exception caught and logged at ERROR).  Callers
    that care about success — chiefly the stuck-document sweeper — can
    distinguish "scheduled" from "tried-and-failed" without parsing
    log lines.  Callers that don't care can still ``await`` the call
    and ignore the return value; ``bool`` is fully backward compatible
    with the previous ``None`` return.

    Safe to call even if the worker is not running — the document will
    stay in ``pending`` status and can be reprocessed later.

    The job embeds the document for *on_behalf_of*, by default the person the
    calling context acts for (``provider_access.bind_acting_user``: the
    request's user, the agent turn's user) - refused here already when
    the embedding provider excludes that person
    (:func:`refuse_documents_for_excluded_person`), unless the caller checked
    (*exclusions_checked*).
    """
    if on_behalf_of is None:
        from app.llm.provider_access import acting_user_id

        on_behalf_of = acting_user_id()
    if not exclusions_checked and await refuse_documents_for_excluded_person([doc_id], on_behalf_of):
        return False
    if embedding_ready is None:
        state, reason = await _resolve_embedding_enqueue_state()
        if state is False:
            logger.info(
                "Skipping document processing enqueue for %s: %s", doc_id, reason
            )
            return False
        if state is None:
            logger.warning(
                "Embedding configuration pre-check failed for %s; proceeding with enqueue: %s",
                doc_id,
                reason,
            )
    elif embedding_ready is False:
        # Caller already ran the pre-check and the embedding config is
        # NOT in a usable state.  Honour their verdict: don't enqueue.
        # Without this branch, ``enqueue_document_processing(doc_id,
        # embedding_ready=False)`` would skip the ``is None`` block
        # above and fall straight through to ``queue.enqueue`` — a
        # latent contract hole.  ``enqueue_documents`` short-circuits
        # before the per-doc loop today so the bug isn't exercised,
        # but a future caller that passes a cached ``False`` from a
        # different probe site would silently enqueue work the
        # platform can't process.
        logger.info(
            "Skipping document processing enqueue for %s "
            "(caller-provided pre-check result: not configured)",
            doc_id,
        )
        return False

    try:
        from saq import Job

        from app.worker import queue

        enqueued = await asyncio.wait_for(
            queue.enqueue(
                Job(
                    function="process_document_job",
                    kwargs=document_job_kwargs(doc_id, on_behalf_of),
                    # Stable per-document key: SAQ dedupes enqueues whose
                    # key is already queued/active, so sweeper re-enqueues
                    # for a doc that already has a live job are no-ops
                    # instead of unbounded duplicates (see
                    # ``document_job_key``).
                    key=document_job_key(doc_id),
                    timeout=app_settings.worker_document_job_timeout,
                    # Safe to retry: ``_process_document_impl`` deletes any
                    # stale ``Embedding`` rows for the document
                    # (``source_type='document', source_id=<doc_id>``) at
                    # the start of every attempt, so partial writes from a
                    # failed attempt cannot accumulate as duplicate chunks
                    # in pgvector across retries.
                    retries=app_settings.worker_document_job_retries,
                    retry_delay=app_settings.worker_document_job_retry_delay_seconds,
                    retry_backoff=app_settings.worker_document_job_retry_backoff,
                )
            ),
            timeout=_ENQUEUE_TIMEOUT_SECONDS,
        )
        if enqueued is None:
            # SAQ deduplicated: a job with this document's key is already
            # queued / active / scheduled for retry.  The document IS
            # going to be processed, so this counts as success — it just
            # didn't need a new job.
            logger.info(
                "Document %s already has a queued/active embedding job; "
                "skipped duplicate enqueue",
                doc_id,
            )
        return True
    except Exception as exc:
        # Log at ERROR (was WARNING) so a Redis blip during enqueue is
        # actually visible in production dashboards.  The document
        # remains ``pending`` in the database and the autonomous
        # scheduler's ``recover_stuck_documents`` sweep is the
        # second-line recovery — once ``updated_at`` exceeds
        # ``STUCK_PENDING_THRESHOLD_SECONDS`` the doc is automatically
        # re-enqueued.
        logger.error(
            "Failed to enqueue document processing job for %s: %s "
            "(will be retried by the stuck-document sweeper)",
            doc_id,
            format_exception_message(exc),
            exc_info=exc,
        )
        return False


async def enqueue_documents(doc_ids: list[str]) -> None:
    """Enqueue multiple documents. Call AFTER db.commit()."""
    if not doc_ids:
        return

    state, reason = await _resolve_embedding_enqueue_state()
    if state is False:
        logger.info(
            "Skipping enqueue for %d document(s): %s",
            len(doc_ids),
            reason,
        )
        return
    if state is None:
        logger.warning(
            "Embedding configuration batch pre-check failed; proceeding with enqueue for %d document(s): %s",
            len(doc_ids),
            reason,
        )

    # ``embedding_ready=True`` here regardless of whether the batch
    # pre-check returned ``True`` (configured) OR ``None`` (pre-check
    # itself failed but we proceed best-effort).  Passing ``state`` —
    # which is ``True`` or ``None`` at this point, never ``False`` —
    # would forward ``None`` to ``enqueue_document_processing`` and
    # trigger the ``if embedding_ready is None`` branch's full
    # per-document pre-check + WARNING + DB lookup, up to N times in
    # this loop.  For the stuck-document sweeper that can hit 200
    # documents per tick, that one provider-config lookup failure
    # would multiply into 200 repeated lookups and 200 identical
    # log lines on every autonomous tick.  Pass ``True`` so the
    # per-doc function short-circuits the pre-check and proceeds
    # to enqueue — exactly the behaviour the existing ``if state is
    # None`` fall-through already wants once the batch warning has
    # fired once.
    # One exclusion check for the batch: its documents share the person.
    from app.llm.provider_access import acting_user_id

    person = acting_user_id()
    if await refuse_documents_for_excluded_person(doc_ids, person):
        return
    for doc_id in doc_ids:
        await enqueue_document_processing(
            doc_id, embedding_ready=True, on_behalf_of=person, exclusions_checked=True
        )


async def enqueue_reembedding(doc_ids: list[str]) -> None:
    """Enqueue EXISTING documents to be embedded again (reprocess, a model change).

    Unlike :func:`enqueue_documents`, the work is not attributed to the person
    whose request triggers it: re-embedding a corpus is not that person adding
    content, and one reprocess or a platform-wide reset spans documents added
    by others. Each job then embeds for the person recorded on its document
    (``added_by_user_id``: whoever last added or changed it) and for its
    knowledge base's owner, and a provider that excludes either never receives
    it - an allowed admin cannot send an excluded person's documents to
    the forbidden provider, even in a company-managed knowledge base, which has
    no owner, and an excluded admin does not mark everyone's documents
    unavailable.
    """
    from app.llm.provider_access import acting_for

    with acting_for(None):
        await enqueue_documents(doc_ids)


async def warn_if_no_active_workers() -> bool:
    """ERROR-log when jobs are being enqueued but no SAQ worker is alive.

    SAQ workers refresh a ``worker_info`` heartbeat (60s TTL) on the
    queue; a worker that crashed — or, more insidiously, one whose Redis
    connections silently died and that is hanging forever on a dead
    ``BLMOVE`` read — stops refreshing within a minute and disappears
    from ``queue.info()["workers"]``.

    Without this probe the stuck-document sweeper churns silently:
    it re-enqueues the same documents tick after tick, the dashboard
    shows "Re-indexing in progress" forever, and nothing in any log
    says *why* nothing moves.  Called by the sweeper after it actually
    re-enqueued something, so the ERROR fires at most once per sweep
    tick and only while there is real work being dropped.

    Returns ``True`` when the alarm fired (no live workers), ``False``
    when at least one worker is alive or the probe itself failed
    (probe failures are logged at DEBUG — they usually just mean Redis
    is down, which already screams elsewhere).
    """
    try:
        from app.worker import queue

        info = await asyncio.wait_for(queue.info(), timeout=_ENQUEUE_TIMEOUT_SECONDS)
        workers = info.get("workers") or {}
        if not workers:
            logger.error(
                "Embedding jobs were enqueued but NO active SAQ worker is "
                "registered on the queue — documents will stay pending until "
                "a worker consumes them. Check that the worker process is "
                "running and that its Redis connection is healthy (a worker "
                "stuck on a silently-dead Redis socket looks exactly like "
                "this: alive, no errors, processing nothing)."
            )
            return True
        return False
    except Exception:
        logger.debug("SAQ worker liveness probe failed", exc_info=True)
        return False


async def _resolve_embedding_enqueue_state() -> tuple[bool | None, str | None]:
    """Return whether embedding jobs should be queued.

    Returns:
        (True, None): embedding configuration is valid.
        (False, reason): no embedding-capable provider/model configured.
        (None, reason): pre-check failed unexpectedly; caller may continue.
    """
    try:
        from app.llm.router import EmbeddingConfigurationError, get_embedding_provider

        async with async_session() as db:
            await get_embedding_provider(db)
        return True, None
    except EmbeddingConfigurationError as exc:
        return False, str(exc)
    except Exception as exc:  # pragma: no cover - defensive fallback
        return None, str(exc)


# ---------------------------------------------------------------------------
# Per-tick embedding-readiness gate for the autonomous recovery sweeps
# ---------------------------------------------------------------------------
#
# The three KB recovery sweeps (stuck / failed / stale) each probed the
# embedding configuration *after* claiming their batch with ``FOR UPDATE SKIP
# LOCKED``, then logged one INFO line and rolled the claim back.  On a
# deployment with no embedding provider configured — a normal state for an
# install that never enabled RAG — that meant, on every 60 s autonomous tick:
# three row claims + three rollbacks + three provider-config lookups, and (for
# any non-empty backlog) an identical ~500-character INFO line per tick, i.e.
# ~1440 lines/day saying the same static thing.
#
# ``resolve_embedding_recovery_state`` is the single per-tick probe.  The tick
# calls it once *before* touching any document row: on ``False`` all three
# sweeps are skipped outright (nothing claimed, no locks taken, no per-sweep
# log lines), and otherwise the verdict is forwarded to the sweeps so they
# don't re-probe.  The repeated "not configured" line is emitted at INFO the
# first time and when the reason text changes, then demoted to DEBUG with an
# hourly INFO heartbeat — the state stays discoverable without drowning the
# log.
_EMBEDDING_STATE_LOG_INTERVAL_SECONDS = 3600.0

# Minimum gap between two non-DEBUG lines of one key, applied even when the
# reason changed or embedding was healthy in between.  The "pre-check failed"
# reason is the text of whatever exception the lookup raised, so it is not
# static, and a flaky lookup alternates healthy / failed: without a floor
# either would re-emit on every 60 s tick.  Must stay above the tick interval
# or it bounds nothing.
_EMBEDDING_STATE_REASON_FLOOR_SECONDS = 300.0

# ``key -> (reason of the last non-DEBUG line, its monotonic timestamp,
# whether a healthy verdict has been seen since)``.  Module-level (not
# per-call) because the whole point is to remember across autonomous ticks;
# keyed so the "not configured" and "pre-check failed" lines throttle
# independently of one another.
_embedding_state_log_state: dict[str, tuple[str | None, float, bool]] = {}


def _should_log_embedding_state(key: str, reason: str | None) -> bool:
    """Return whether this repeated embedding-state line is due for INFO/WARNING.

    ``True`` on the first occurrence, once per
    ``_EMBEDDING_STATE_LOG_INTERVAL_SECONDS`` as a heartbeat, and when the
    reason changed (or embedding was healthy in between) provided the last
    line of this key is at least ``_EMBEDDING_STATE_REASON_FLOOR_SECONDS``
    old.  ``False`` otherwise — callers log at DEBUG instead so the line is
    still available with debug logging enabled.

    The stored state is always the last line actually EMITTED, so a reason
    that changed inside the floor and then stays is reported once the floor
    has passed rather than folded into the heartbeat.
    """
    now = time.monotonic()
    previous = _embedding_state_log_state.get(key)
    if previous is not None:
        last_reason, last_emitted, healthy_since = previous
        elapsed = now - last_emitted
        if not healthy_since and last_reason == reason:
            if elapsed < _EMBEDDING_STATE_LOG_INTERVAL_SECONDS:
                return False
        elif elapsed < _EMBEDDING_STATE_REASON_FLOOR_SECONDS:
            return False
    _embedding_state_log_state[key] = (reason, now, False)
    return True


def _mark_embedding_state_healthy() -> None:
    """Record a healthy verdict without re-arming the throttle.

    A *later* outage then reports its first line right away instead of being
    swallowed by the heartbeat window the previous outage left behind — once
    the reason floor has passed since the last line.  The timestamps are
    kept on purpose: forgetting them let a lookup that flaps healthy / failed
    re-emit its WARNING on every flip.
    """
    for key, (reason, emitted, _healthy) in list(_embedding_state_log_state.items()):
        _embedding_state_log_state[key] = (reason, emitted, True)


def reset_embedding_state_log_throttle() -> None:
    """Forget the throttle memory entirely, so the next state line logs at once."""
    _embedding_state_log_state.clear()


async def resolve_embedding_recovery_state() -> bool | None:
    """Probe embedding readiness once for a whole autonomous tick.

    Returns:
        ``True``: embedding is configured — run the recovery sweeps and pass
        this verdict down so they skip their own probe.
        ``False``: no embedding-capable provider — the caller MUST skip the
        sweeps entirely; re-enqueueing work nothing can process would only
        churn rows and logs.  The (throttled) operator-facing line is already
        logged here.
        ``None``: the probe itself failed unexpectedly — the caller should run
        the sweeps best-effort, and should still pass ``embedding_ready=True``
        down rather than forwarding this ``None``: the single WARNING is
        already logged here (throttled the same way), whereas ``None`` would
        make every sweep re-run the same failing probe and re-log it.
    """
    state, reason = await _resolve_embedding_enqueue_state()
    if state is True:
        _mark_embedding_state_healthy()
        return True

    if state is False:
        message = (
            "KB embedding recovery sweeps skipped — stuck / failed / stale "
            "documents are left untouched until an embedding provider is "
            "configured: %s"
        )
        if _should_log_embedding_state("unavailable", reason):
            logger.info(message, reason)
        else:
            logger.debug(message, reason)
        return False

    message = "KB embedding recovery pre-check failed; sweeps run best-effort: %s"
    if _should_log_embedding_state("probe-failed", reason):
        logger.warning(message, reason)
    else:
        logger.debug(message, reason)
    return None


# Reason text used when a caller hands a sweep a pre-computed ``False``
# verdict: the sweep has no reason string of its own to report in that case
# (the caller already logged the underlying provider error).
_CALLER_PRECHECK_UNAVAILABLE_REASON = (
    "embedding is not usable (caller-provided pre-check result)"
)


async def _sweep_embedding_state(
    embedding_ready: bool | None,
) -> tuple[bool | None, str | None]:
    """Resolve a sweep's embedding verdict, honouring a caller-provided one.

    ``embedding_ready=None`` means "no caller verdict, probe now" (the
    standalone / test path).  Anything else is the caller's already-probed
    verdict and is honoured as-is — including ``False``, so a caller that has
    seen a broken configuration can't accidentally have the sweep re-probe and
    proceed.  Mirrors :func:`enqueue_document_processing`'s ``embedding_ready``
    contract so the two conventions stay copy-pasteable.
    """
    if embedding_ready is None:
        return await _resolve_embedding_enqueue_state()
    if embedding_ready is False:
        return False, _CALLER_PRECHECK_UNAVAILABLE_REASON
    return True, None


def _is_invalid_cached_statement_error(exc: BaseException) -> bool:
    """Return whether *exc* is asyncpg's ``InvalidCachedStatementError``.

    A DDL migration during a deploy invalidates asyncpg's per-connection
    prepared-statement cache; the first query reusing a now-stale plan raises
    ``InvalidCachedStatementError`` (SQLAlchemy wraps it in ``DBAPIError``).
    Match by the orig/cause class name + message so we don't need a hard import
    of asyncpg's exception module and stay robust across wrapper layers.
    """
    for candidate in (exc, getattr(exc, "orig", None), getattr(exc, "__cause__", None)):
        if candidate is None:
            continue
        if type(candidate).__name__ == "InvalidCachedStatementError":
            return True
        if "cached statement plan is invalid" in str(candidate):
            return True
    return False


async def _execute_with_schema_cache_retry(
    db: AsyncSession,
    statement,
    *,
    attempts: int = 2,
):
    """Execute *statement*, retrying once if a DDL change invalidated the
    asyncpg prepared-statement cache.

    The failed statement aborts the transaction, so we roll back before
    retrying — the retry re-prepares against the new schema and succeeds.
    Only ``InvalidCachedStatementError`` is retried; every other failure
    propagates unchanged.
    """
    for attempt in range(attempts):
        try:
            return await db.execute(statement)
        except DBAPIError as exc:
            if not _is_invalid_cached_statement_error(exc):
                raise
            if attempt >= attempts - 1:
                # Retries exhausted — surface the cache-invalidation error.
                raise
            logger.warning(
                "Query hit InvalidCachedStatementError (schema changed under a "
                "cached plan) — rolling back and retrying (attempt %d/%d)",
                attempt + 1,
                attempts,
            )
            await db.rollback()
    # Unreachable: the loop either returns the result or re-raises.
    raise RuntimeError("retry loop exited without a result")


async def recover_stuck_documents(
    db: AsyncSession,
    *,
    pending_threshold_seconds: int = STUCK_PENDING_THRESHOLD_SECONDS,
    processing_threshold_seconds: int | None = None,
    batch_size: int = _STUCK_DOCUMENT_BATCH_SIZE,
    embedding_ready: bool | None = None,
) -> int:
    """Re-enqueue knowledge-base documents whose embedding job was lost.

    A document is considered stuck when:

    * status is ``pending`` and ``updated_at`` is older than
      ``pending_threshold_seconds`` — the SAQ enqueue either silently
      failed (Redis blip swallowed by the bare ``except`` in
      :func:`enqueue_document_processing`, or the worker process crashed
      between dequeue and the status flip to ``processing``).
    * status is ``processing`` and ``updated_at`` is older than
      ``processing_threshold_seconds`` — the worker that picked up the
      document died mid-embedding (OOM kill, pod restart, SIGKILL) and
      never updated the status to ``ready`` / ``error``.

    For each stuck document we *reset* the status to ``pending`` (so the
    ``processing`` arm becomes idempotent across the next worker pickup)
    and call :func:`enqueue_document_processing` to schedule a fresh SAQ
    job.  The actual embedding work is naturally idempotent — the
    processor swaps a document's chunks in one transaction — so a
    duplicate run is harmless if the original job happens to recover.

    This complements the per-job ``FOR UPDATE SKIP LOCKED`` retry in
    :mod:`document_processor` by catching the "enqueue itself was lost"
    case the in-process retry can't see.

    The sweep claims and commits documents in chunks of
    ``_STUCK_UPDATE_COMMIT_CHUNK_SIZE``: each chunk is selected with its
    own ``FOR UPDATE SKIP LOCKED``, mutated, committed, and enqueued
    before the next chunk is claimed.  This keeps the implicit ORM
    executemany UPDATE below statement-timeout-prone payload sizes on
    large backlogs while preserving the lock contract —
    a commit only ever releases locks on rows that were updated in the
    same transaction, so the sweeper can never hold-then-lose a lock on
    a row it hasn't processed yet.

    Returns the number of documents that were re-enqueued.  A return
    value greater than ``batch_size`` is impossible — the sweep stops
    once the cap is hit so the next tick can resume.

    ``processing_threshold_seconds`` defaults to
    ``worker_document_job_timeout + STUCK_PROCESSING_THRESHOLD_MARGIN_SECONDS``
    so it tracks the configured job timeout (rather than a hard-coded
    literal that an operator could accidentally exceed).

    ``embedding_ready`` lets a caller that already probed the embedding
    configuration for this tick (see
    :func:`resolve_embedding_recovery_state`) hand its verdict down so the
    sweep doesn't repeat the provider-config lookup.  ``None`` (the default)
    means "probe when the first chunk is claimed", the standalone behaviour.
    """
    if processing_threshold_seconds is None:
        processing_threshold_seconds = _processing_threshold_seconds()

    now = datetime.now(UTC)
    pending_cutoff = now - timedelta(seconds=pending_threshold_seconds)
    processing_cutoff = now - timedelta(seconds=processing_threshold_seconds)

    # ``FOR UPDATE SKIP LOCKED`` + the ``updated_at`` cutoff together
    # keep this sweep from racing the embedding worker.  Important
    # nuance about how ``document_processor._process_document_impl``
    # actually uses the row lock:
    #
    #   * On entry it SELECTs the row with ``FOR UPDATE SKIP LOCKED``
    #     and flips ``status = 'processing'``, then **commits** —
    #     releasing the row lock.
    #   * It does the heavy embedding work WITHOUT holding the row
    #     lock (that's the slow part, several seconds to minutes).
    #   * Just before the final ``status = 'ready'`` write it
    #     re-acquires the lock via ``with_for_update(skip_locked=True)``
    #     and, **if the row comes back**, swaps the chunks and commits the ready state. If the
    #     re-lock returns no row (another transaction holds it, or the row
    #     was deleted) it rolls back and returns 0, leaving the row
    #     ``processing`` — which is exactly what this sweep recovers.
    #
    # So the worker only holds the row lock during three SHORT
    # windows around its status transitions, not for the full
    # embedding run.  The primary protection against the sweeper
    # racing an actively-embedding doc is the WHERE clause's
    # ``updated_at < processing_cutoff`` filter combined with the
    # ``BEFORE UPDATE`` trigger that refreshes ``updated_at`` on
    # every status flip:
    #
    #   1. Worker commits ``status = 'processing'`` with the trigger
    #      writing ``updated_at = clock_timestamp()`` (NOT ``now()`` /
    #      ``current_timestamp`` / ``transaction_timestamp()`` — those
    #      return the surrounding transaction's start time, so a long-
    #      running KB sync that holds a single transaction across many
    #      page upserts would stamp every page with the same old
    #      timestamp and could be older than the sweeper's cutoff by
    #      the time it commits, immediately re-enqueueing freshly
    #      synced docs on the next tick; ``clock_timestamp()`` returns
    #      the actual wall-clock time of the trigger firing).
    #   2. Sweeper next tick (≥ 60 s later) checks
    #      ``updated_at < (wall-clock now) - (worker_timeout + 10 min)``.
    #   3. An actively-embedding doc has a fresh ``updated_at`` and
    #      doesn't match.  Only docs whose worker died (so
    #      ``updated_at`` is frozen at the "processing" timestamp and
    #      eventually crosses the cutoff) flow through.
    #
    # The ``FOR UPDATE SKIP LOCKED`` here is still useful for two
    # narrower reasons:
    #
    #   * Multiple autonomous tickers (e.g. uvicorn workers, the SAQ
    #     worker's autonomous loop) won't race each other on the same
    #     batch — each sweep claims its rows.
    #   * If a sweep happens to land exactly during one of the
    #     worker's three short lock windows, the row is silently
    #     excluded from the batch and picked up on a future tick if
    #     it stays genuinely stuck — even better defence-in-depth on
    #     top of the cutoff filter.
    #
    # ``LIMIT`` after ``FOR UPDATE SKIP LOCKED`` is the PostgreSQL-
    # canonical order: SKIP LOCKED filters the candidate set first,
    # then LIMIT caps the result; the planner does not over-lock.
    #
    # The sweep is chunked to keep SQLAlchemy's implicit executemany
    # UPDATE payload below statement-timeout-prone sizes on large
    # backlogs.  Crucially, each chunk is CLAIMED by its
    # own ``SELECT ... FOR UPDATE SKIP LOCKED`` and committed before
    # the next chunk is claimed.  Selecting the full batch once and
    # committing mid-iteration would be wrong: the first ``commit()``
    # releases *all* row locks acquired by the SELECT — including rows
    # in chunks not yet updated — so a worker could finish one of those
    # rows (``status = 'ready'``) and the sweeper's stale ORM instance
    # would then overwrite it back to ``pending``, re-introducing the
    # exact race the ``FOR UPDATE SKIP LOCKED`` regression test below
    # pins.  Per-chunk claiming means a commit only ever releases locks
    # on rows that were updated in the same transaction.
    #
    # Loop termination: every claimed row gets ``updated_at = now``
    # before its chunk commits, so committed rows no longer match the
    # ``updated_at < cutoff`` WHERE clause and each iteration claims a
    # disjoint set of rows.  A chunk that comes back smaller than its
    # LIMIT means the candidate set is drained (rows skipped via SKIP
    # LOCKED are picked up by a later tick, same as before).
    chunk_size = max(1, min(_STUCK_UPDATE_COMMIT_CHUNK_SIZE, batch_size))

    def _claim_chunk_query(limit: int):
        return (
            select(KnowledgeDocument)
            .where(
                (
                    (KnowledgeDocument.status == "pending")
                    & (KnowledgeDocument.updated_at < pending_cutoff)
                )
                | (
                    (KnowledgeDocument.status == "processing")
                    & (KnowledgeDocument.updated_at < processing_cutoff)
                )
            )
            .order_by(KnowledgeDocument.updated_at.asc())
            .limit(limit)
            .with_for_update(skip_locked=True)
        )

    # Count actual SAQ enqueues, not just attempts.  Before
    # ``enqueue_document_processing`` returned a bool, the loop here
    # had two bad shapes in succession:
    #
    #  * every attempt was counted as a success, even on Redis failure
    #    (the caught exception only logged) — the metric overstated
    #    recovery and hid Redis outages.
    #  * ``if enqueue_result is None`` was used to detect failure, but the
    #    helper returned ``None`` even on success — every recovery was
    #    reported as a failure (the metric understated to 0 and dashboards filled
    #    with spurious WARNING noise).
    #
    # ``enqueue_document_processing`` now returns ``bool`` (``True``
    # on a successful enqueue, ``False`` on skip / exception), so we
    # can count what actually happened.
    #
    # Pass ``embedding_ready=True`` regardless of whether the sweep's
    # embedding pre-check (probed once, on the first non-empty chunk
    # inside the claim loop below) returned ``True`` (configured) OR
    # ``None`` (the probe itself failed but we proceed best-effort).
    # By the time any enqueue runs, the probe result is ``True`` or
    # ``None``, never ``False`` — the ``False`` branch short-circuits
    # the whole sweep with a rollback + early return before the first
    # chunk is mutated.  Forwarding the raw probe result when it is
    # ``None`` would trigger ``enqueue_document_processing``'s
    # ``if embedding_ready is None`` branch and re-run the full
    # pre-check + WARNING + DB lookup for every document — for a
    # 200-doc sweep a single provider-config lookup failure would
    # multiply into 200 repeated lookups and 200 identical log lines
    # on every autonomous tick.  The single sweep-level WARNING
    # (logged once at first-chunk probe time) is the operator signal;
    # the per-doc function short-circuits the recheck and proceeds
    # best-effort.  Mirrors the convention established by
    # ``enqueue_documents`` for exactly the same reason — keep the two
    # enqueue-loop sites symmetric so a future caller copying the
    # pattern from one to the other can't accidentally regress the
    # cost-amplification on the sibling.
    successfully_reenqueued_ids: list[str] = []
    total_reenqueue_attempts = 0
    claimed_total = 0
    while claimed_total < batch_size:
        # Retry once on InvalidCachedStatementError: a DDL migration
        # during a rolling deploy invalidates asyncpg's prepared-
        # statement cache, and this sweep — running on the autonomous
        # tick right after the deploy — is a frequent first victim.
        result = await _execute_with_schema_cache_retry(
            db, _claim_chunk_query(min(chunk_size, batch_size - claimed_total))
        )
        chunk_docs = list(result.scalars().all())
        if not chunk_docs:
            break

        if claimed_total == 0:
            # Probe the embedding configuration once for the whole
            # sweep (on the first non-empty chunk) so we don't hammer
            # the LLM provider config table per document or per chunk.
            # If embedding isn't configured at all,
            # ``enqueue_document_processing`` would just no-op for
            # each call — short-circuit early instead.  A caller that already
            # probed for this tick passes ``embedding_ready`` and we reuse its
            # verdict instead of re-querying.
            state, reason = await _sweep_embedding_state(embedding_ready)
            if state is False:
                # ``len(chunk_docs)`` is only the first claimed chunk,
                # not the full backlog — the sweep returns before
                # claiming the rest — so the message scopes the count
                # explicitly to avoid misleading ops during debugging.
                logger.info(
                    "Stuck-document sweep skipped: %s (released the "
                    "first claimed chunk of %d document(s); the "
                    "remaining backlog was not claimed)",
                    reason,
                    len(chunk_docs),
                )
                # CRITICAL: release the row locks acquired by
                # ``FOR UPDATE SKIP LOCKED`` above before returning.
                # The autonomous tick reuses this ``db`` session
                # across the full tick (KB sync, memory maintenance,
                # …); without an explicit ``rollback()`` here, the
                # row locks would stay held until the session is
                # finally closed at the end of the tick — blocking
                # the embedding worker and any other maintenance task
                # that needs those rows even though the sweeper has
                # decided not to touch them.
                #
                # ``rollback()`` rather than ``commit()`` because we
                # have done no writes: the only mutation we performed
                # is the SELECT itself (which acquired the row locks),
                # and a rollback releases the locks without persisting
                # anything the SELECT side-effected.  Nothing has been
                # committed yet — the probe runs before the first
                # chunk's mutations — so the short-circuit is still
                # all-or-nothing.
                await db.rollback()
                return 0
            if state is None:
                # Pre-check itself failed unexpectedly.  Mirror the
                # sibling ``enqueue_documents`` call site: emit a
                # single batch-level WARNING with the underlying
                # reason so operators have a signal AND continue the
                # sweep best-effort with ``embedding_ready=True``
                # below (so the per-doc helper doesn't re-run the
                # failing probe N times — see the long comment above
                # the loop).  Without this WARNING the resolver
                # exception was completely silent while the sweeper
                # proceeded, hiding repeated provider-config failures
                # from observability dashboards.
                # As above, the count is scoped to the first claimed
                # chunk — later chunks also proceed best-effort but
                # have not been claimed yet at this point.
                logger.warning(
                    "Stuck-document sweep batch pre-check failed; proceeding "
                    "best-effort (first claimed chunk: %d document(s)): %s",
                    len(chunk_docs),
                    reason,
                )

        claimed_total += len(chunk_docs)

        # Reset processing→pending docs so the worker's status-write
        # path stays linear (``pending → processing → ready/error``)
        # and always explicitly bump ``updated_at`` so SQLAlchemy
        # issues a real UPDATE for every row (the ``BEFORE UPDATE``
        # trigger also refreshes it, but only fires when an UPDATE
        # actually reaches the database).
        #
        # Touching ``updated_at`` for already-pending rows is
        # critical: if we left those rows untouched here, no UPDATE
        # would be issued at all and the row's ``updated_at`` would
        # stay older than the cutoff — the next autonomous tick would
        # pick the exact same doc up again and re-enqueue forever,
        # regardless of whether SAQ actually picked it up in between.
        # Bumping the timestamp gives the freshly-re-enqueued job a
        # full ``pending_threshold_seconds`` window to make progress
        # before the sweep is allowed to fire on it again.  It is
        # also what guarantees the claim loop terminates: committed
        # rows no longer match the cutoff filter, so each iteration
        # claims a disjoint set.
        chunk_ids: list[str] = []
        for doc in chunk_docs:
            if doc.status == "processing":
                doc.error_message = worker_timeout_message(
                    int(processing_threshold_seconds)
                )
                doc.status = "pending"
            doc.updated_at = now
            chunk_ids.append(str(doc.id))

        # Commit the chunk before enqueueing it: the SQL transaction
        # never spans the SAQ network round-trip, and a later chunk's
        # failure (e.g. a statement timeout) still leaves every
        # earlier chunk fully committed and re-enqueued — partial
        # forward progress instead of the all-or-nothing abort that
        # motivated the chunking.
        await db.commit()

        for doc_id in chunk_ids:
            total_reenqueue_attempts += 1
            if await enqueue_document_processing(doc_id, embedding_ready=True):
                successfully_reenqueued_ids.append(doc_id)
            else:
                # The helper already logged at ERROR with a traceback when
                # the failure was an exception, and at INFO when it was a
                # provider-missing skip.  We add a sweep-context line so
                # operators can tell whether the loss came from the sweep
                # path vs the original sync enqueue, without re-emitting
                # the upstream traceback.
                logger.warning(
                    "Stuck-document sweeper failed to re-enqueue document %s "
                    "(see preceding log line for the underlying cause)",
                    doc_id,
                )

        if len(chunk_docs) < chunk_size:
            # Short read: the candidate set is drained (rows skipped
            # via SKIP LOCKED stay excluded until a later tick, same
            # as the pre-chunking single SELECT).  Skip the extra
            # empty round-trip.
            break

    if claimed_total == 0:
        # Quiet tick: nothing stuck.  Return before the summary log /
        # liveness probe so an idle deployment doesn't emit a
        # "re-enqueued 0 of 0" line every 60 s.
        return 0

    logger.info(
        "Stuck-document sweeper re-enqueued %d of %d document(s) "
        "(pending threshold=%ds, processing threshold=%ds)",
        len(successfully_reenqueued_ids),
        total_reenqueue_attempts,
        pending_threshold_seconds,
        processing_threshold_seconds,
    )

    # Stuck documents on every tick + jobs accepted by Redis + still no
    # progress is the signature of a dead/stalled worker.  Probe the
    # worker heartbeat so the failure mode is an explicit ERROR in the
    # logs instead of an infinitely-churning silent sweep.
    if successfully_reenqueued_ids:
        await warn_if_no_active_workers()

    return len(successfully_reenqueued_ids)


# ---------------------------------------------------------------------------
# Failed-document auto-retry (self-healing for ``error`` docs)
# ---------------------------------------------------------------------------


def _embedding_retry_delay_seconds(retry_count: int) -> float:
    """Backoff (seconds) before the next automatic embedding retry.

    Exponential — ``base * 2^retry_count`` capped at ``max_delay`` — plus
    random jitter so a large failed backlog re-embeds spread out rather than
    as a thundering herd. The exponent is capped at 32 so an unlimited-retry
    document (``embedding_auto_retry_max_attempts = 0``) can't overflow
    ``base * 2^retry_count`` into a giant integer; ``2^32`` already dwarfs any
    sane ``max_delay``.
    """
    base = max(1, app_settings.embedding_auto_retry_base_delay_seconds)
    max_delay = max(base, app_settings.embedding_auto_retry_max_delay_seconds)
    exponent = min(max(retry_count, 0), 32)
    delay = min(float(base) * float(2**exponent), float(max_delay))
    jitter = max(0, app_settings.embedding_auto_retry_jitter_seconds)
    if jitter:
        # Non-cryptographic jitter to de-correlate a large failed backlog.
        delay += random.uniform(0, jitter)  # noqa: S311
    return delay


def schedule_embedding_retry(
    doc: KnowledgeDocument,
    *,
    retryable: bool,
    now: datetime | None = None,
) -> None:
    """Schedule (or clear) the next automatic embedding retry for a document.

    Called by the document processor at every ``status = 'error'`` site.
    ``embedding_next_retry_at`` is left ``None`` (the doc is never auto-retried
    by :func:`recover_failed_documents`) when any of these hold:

      * auto-retry is disabled (``embedding_auto_retry_enabled = False``),
      * the failure is non-retryable — i.e. structural/permanent (no
        extractable text, no content, KB deleted): the caller passes
        ``retryable=False``, or
      * the per-document attempt cap has been reached
        (``embedding_auto_retry_max_attempts > 0`` and exhausted).

    Otherwise it is set to ``now + backoff(retry_count) + jitter``. The
    ``embedding_retry_count`` is *not* incremented here — the sweeper
    increments it when it actually re-enqueues the document, so the count
    reflects the number of retries initiated.
    """
    if not retryable or not app_settings.embedding_auto_retry_enabled:
        doc.embedding_next_retry_at = None
        return
    max_attempts = app_settings.embedding_auto_retry_max_attempts
    count = doc.embedding_retry_count or 0
    if max_attempts > 0 and count >= max_attempts:
        doc.embedding_next_retry_at = None
        return
    now = now or datetime.now(UTC)
    doc.embedding_next_retry_at = now + timedelta(
        seconds=_embedding_retry_delay_seconds(count)
    )


def _provider_failure_conditions() -> list:
    """Documents in error another embedding provider can fix, within the attempt cap.

    Any reason that is no fixed sentence counts: a dimension mismatch, raw text
    from a row older than the sentences or none, which may be a provider failure.
    """
    conditions = [
        KnowledgeDocument.status == "error",
        or_(
            KnowledgeDocument.error_message.in_(PROVIDER_FAILURE_SENTENCES),
            KnowledgeDocument.error_message.is_(None),
            and_(
                KnowledgeDocument.error_message.not_in(PLATFORM_SENTENCES),
                ~KnowledgeDocument.error_message.startswith(WORKER_TIMEOUT_OPENING, autoescape=True),
            ),
        ),
    ]
    max_attempts = app_settings.embedding_auto_retry_max_attempts
    if max_attempts > 0:
        conditions.append(KnowledgeDocument.embedding_retry_count < max_attempts)
    return conditions


async def count_provider_failures(db: AsyncSession) -> int:
    """How many documents :func:`retry_provider_failures_now` would make due."""
    result = await db.execute(
        select(func.count()).select_from(KnowledgeDocument).where(*_provider_failure_conditions())
    )
    return int(result.scalar_one())


async def _make_provider_failures_due(db: AsyncSession, ids: Sequence[uuid.UUID], now: datetime) -> int:
    """The guarded write: *ids* that still are provider failures under the cap
    become due *now*; a row re-queued meanwhile is left alone. Commits."""
    result = await db.execute(
        update(KnowledgeDocument)
        .where(KnowledgeDocument.id.in_(ids), *_provider_failure_conditions())
        .values(embedding_next_retry_at=now)
        .execution_options(synchronize_session=False)
    )
    await db.commit()
    return result.rowcount or 0


async def retry_provider_failures_now(*, now: datetime | None = None, batch_size: int | None = None) -> int:
    """Make the provider failures due now, for :func:`recover_failed_documents`.

    Run after a switch to another provider of the same model, which keeps the
    vectors; a dimension mismatch is otherwise never retried. One pass over the
    rows that qualify when it starts, in committed batches on its own session; a
    locked row is waited on. A job still on the old provider is covered by
    :func:`retry_document_after_switch`. Returns the row count.
    """
    if not app_settings.embedding_auto_retry_enabled:
        return 0
    from app.core.database import async_session

    now = now or datetime.now(UTC)
    limit = max(1, batch_size or app_settings.embedding_auto_retry_batch_size)
    total = 0
    try:
        async with async_session() as db:
            snapshot = select(KnowledgeDocument.id).where(*_provider_failure_conditions())
            ids = (await db.execute(snapshot)).scalars().all()
            await db.commit()
            for start in range(0, len(ids), limit):
                total += await _make_provider_failures_due(db, ids[start : start + limit], now)
    except Exception:
        logger.error("Retry of provider failures after an embedding provider switch failed", exc_info=True)
    logger.info("Embedding provider switch: %d failed document(s) due for retry", total)
    return total


async def retry_document_after_switch(db: AsyncSession, doc_id: uuid.UUID, *, now: datetime | None = None) -> bool:
    """Make one job's provider failure due now, the embedding config having
    moved while it ran (:func:`embedding_config_moved`). Never raises; returns
    whether the row was written."""
    if not app_settings.embedding_auto_retry_enabled:
        return False
    try:
        return bool(await _make_provider_failures_due(db, [doc_id], now or datetime.now(UTC)))
    except Exception:
        logger.warning("Could not retry document %s after an embedding provider switch", doc_id, exc_info=True)
        with contextlib.suppress(Exception):
            await db.rollback()
        return False


async def embedding_config_moved(used_config_id: uuid.UUID | str) -> bool:
    """Whether resolving the embedding provider now lands on another config than
    the one a job embedded with: an admin switched it while the job ran.

    The router's own resolution, fallbacks included, on a fresh session: the
    job's session still holds the settings it read when it started. Call it with
    no transaction open on the caller's session, or it holds two connections.
    """
    from app.core.database import async_session
    from app.llm.router import get_embedding_provider

    try:
        async with async_session() as db:
            _provider, _model, current = await get_embedding_provider(db)
    except Exception:
        logger.warning("Could not check whether the embedding config was switched; assuming not", exc_info=True)
        return False
    return current is not None and str(current) != str(used_config_id)


async def recover_failed_documents(
    db: AsyncSession,
    *,
    batch_size: int | None = None,
    now: datetime | None = None,
    embedding_ready: bool | None = None,
) -> int:
    """Re-enqueue ``error`` documents whose scheduled embedding retry is due.

    The self-healing counterpart to :func:`recover_stuck_documents`: where
    that recovers *lost* jobs (``pending`` / ``processing``), this recovers
    *failed* jobs (``error``). A document that failed to embed while the
    embedding provider was misconfigured or down is automatically re-embedded
    once the provider is fixed — with exponential backoff + jitter and no
    manual reprocess.

    Selection is driven primarily by ``embedding_next_retry_at`` (set by the
    document processor via :func:`schedule_embedding_retry` at error time): a
    ``NULL`` value means "never auto-retry" (auto-retry disabled, the failure
    was non-retryable, or the per-document attempt cap was hit). A successful
    re-enqueue increments ``embedding_retry_count`` and clears
    ``embedding_next_retry_at`` (re-set by the processor on the next failure),
    so a still-broken provider produces ever-longer backoff instead of
    hammering the worker. A retry is spent only when the SAQ enqueue actually
    succeeds — a failed enqueue (e.g. a Redis blip) leaves the row ``error``
    and still due so the next tick retries it, never consuming a slot of the
    ``embedding_auto_retry_max_attempts`` budget for a job that was never
    queued.

    The attempt cap is also re-checked here, not just at schedule time:
    ``embedding_auto_retry_max_attempts`` is read from settings, so if an
    operator lowers it (or switches off "unlimited") and restarts, documents
    scheduled under the old budget would otherwise stay ``due`` and be
    re-enqueued past the new cap. Such docs have their ``embedding_next_retry_at``
    cleared and are skipped, so they stop being perpetually due.

    Returns the number of documents successfully re-enqueued (capped at
    ``batch_size``).

    ``embedding_ready`` reuses a caller's per-tick embedding probe — see
    :func:`recover_stuck_documents`.
    """
    if not app_settings.embedding_auto_retry_enabled:
        return 0
    if batch_size is None:
        batch_size = app_settings.embedding_auto_retry_batch_size
    # Guard against a misconfigured ``EMBEDDING_AUTO_RETRY_BATCH_SIZE`` (or an
    # explicit caller value): ``LIMIT 0`` would silently disable the sweep and
    # Postgres treats a negative ``LIMIT`` as unbounded, scanning / locking far
    # more ``error`` rows than intended. Clamp to a floor of 1 — turning the
    # sweep off is what ``EMBEDDING_AUTO_RETRY_ENABLED=false`` is for.
    batch_size = max(1, batch_size)
    now = now or datetime.now(UTC)

    # ``FOR UPDATE SKIP LOCKED`` + the ``status = 'error'`` filter keep this
    # sweep from racing the embedding worker (which only ever holds an
    # ``error`` row's lock during the brief bookkeeping write that set the
    # error in the first place). Oldest-due-first ordering drains the backlog
    # fairly across ticks.
    result = await db.execute(
        select(KnowledgeDocument)
        .where(
            KnowledgeDocument.status == "error",
            KnowledgeDocument.embedding_next_retry_at.is_not(None),
            KnowledgeDocument.embedding_next_retry_at <= now,
        )
        .order_by(KnowledgeDocument.embedding_next_retry_at.asc())
        .limit(batch_size)
        .with_for_update(skip_locked=True)
    )
    due = list(result.scalars().all())
    if not due:
        return 0

    # Probe the embedding configuration once for the whole batch. If embedding
    # is still not configured (e.g. the operator hasn't finished fixing it),
    # do NOT spend a retry: leave the docs due so the next tick re-checks, and
    # release the ``FOR UPDATE SKIP LOCKED`` row locks (the autonomous tick
    # reuses this session across all downstream work — a leaked lock would
    # block the embedding worker and other maintenance tasks). See the sibling
    # short-circuit in ``recover_stuck_documents`` for the full rationale.
    state, reason = await _sweep_embedding_state(embedding_ready)
    if state is False:
        logger.info(
            "Failed-document retry sweep skipped for %d document(s): %s",
            len(due),
            reason,
        )
        await db.rollback()
        return 0
    if state is None:
        logger.warning(
            "Failed-document retry sweep batch pre-check failed; proceeding "
            "best-effort for %d document(s): %s",
            len(due),
            reason,
        )

    # Enqueue each due document while we still hold its ``FOR UPDATE SKIP
    # LOCKED`` row lock, and only *spend* a retry — increment
    # ``embedding_retry_count``, flip the row back to ``pending`` and clear the
    # schedule — for a document whose SAQ job actually made it onto the queue.
    # If the enqueue fails (e.g. a transient Redis blip) the row is left
    # untouched: still ``error`` and still due, so the next tick simply retries
    # it without a failed enqueue silently consuming a slot of the
    # ``embedding_auto_retry_max_attempts`` budget for a job that was never
    # queued.
    #
    # Unlike the sibling ``recover_stuck_documents`` (which commits the
    # ``pending`` flip *before* enqueueing), the enqueue happens here under the
    # row lock so the budget spend stays atomic with enqueue success; the
    # single post-loop ``commit`` then persists the status flip and the
    # increment together. The brief window in which a worker could grab the job
    # before the commit lands is harmless: it finds the row still locked
    # (``FOR UPDATE SKIP LOCKED`` → no row → the processor's delayed
    # re-enqueue) or, after commit, ``pending`` and ready to process — and the
    # stuck-document sweeper is the final backstop either way. ``error`` rows
    # are otherwise un-contended, so holding the lock across the enqueue batch
    # adds no meaningful contention.
    #
    # ``embedding_ready=True`` for the same reason as the sibling sweeper /
    # ``enqueue_documents``: the batch pre-check already ran, so forwarding the
    # raw ``state`` (possibly ``None``) would re-trigger the per-doc resolver
    # storm inside ``enqueue_document_processing``.
    # Re-check the attempt cap at sweep time (not only at schedule time): a doc
    # scheduled under a higher / unlimited cap that was since lowered would
    # otherwise stay ``due`` and be re-enqueued past the new budget. Clearing
    # ``embedding_next_retry_at`` for such docs stops them being perpetually
    # due. ``max_attempts <= 0`` means unlimited — never capped.
    max_attempts = app_settings.embedding_auto_retry_max_attempts
    reenqueued_ids: list[str] = []
    capped = 0
    for doc in due:
        if max_attempts > 0 and (doc.embedding_retry_count or 0) >= max_attempts:
            doc.embedding_next_retry_at = None
            capped += 1
            continue
        if not await enqueue_document_processing(str(doc.id), embedding_ready=True):
            logger.warning(
                "Failed-document retry sweeper failed to re-enqueue document %s "
                "(see preceding log line for the underlying cause); leaving it "
                "scheduled so the next tick retries without spending a retry",
                doc.id,
            )
            continue
        doc.embedding_retry_count = (doc.embedding_retry_count or 0) + 1
        # Cleared so a second failure re-schedules from the (now higher) retry
        # count; if the doc stays ``pending`` because the worker is down, the
        # stuck-document sweeper recovers it without spending another retry.
        doc.embedding_next_retry_at = None
        doc.status = "pending"
        doc.updated_at = now
        reenqueued_ids.append(str(doc.id))

    # Always commit — to persist the bookkeeping for re-enqueued docs, clear
    # the schedule on capped docs, and release the ``FOR UPDATE SKIP LOCKED``
    # row locks taken by the SELECT above, even when nothing was re-enqueued.
    await db.commit()

    if capped:
        logger.info(
            "Failed-document retry sweeper capped %d document(s) at the "
            "configured max attempts (%d); they will no longer be auto-retried "
            "(reprocess manually to reset the budget)",
            capped,
            max_attempts,
        )
    logger.info(
        "Failed-document retry sweeper re-enqueued %d of %d due document(s)",
        len(reenqueued_ids),
        len(due),
    )
    return len(reenqueued_ids)


# ---------------------------------------------------------------------------
# Stale-embedding auto-remediation (self-healing for ``ready`` docs)
# ---------------------------------------------------------------------------


async def recover_stale_documents(
    db: AsyncSession,
    *,
    batch_size: int | None = None,
    now: datetime | None = None,
    embedding_ready: bool | None = None,
) -> int:
    """Re-embed ``ready`` documents whose stored embeddings are stale.

    The third self-healing layer, complementing
    :func:`recover_stuck_documents` (lost ``pending`` / ``processing`` jobs)
    and :func:`recover_failed_documents` (``error`` jobs): this one recovers
    *successfully embedded* documents whose stored vectors no longer match the
    current platform embedding target after an admin changed the embedding
    provider / model / dimensions.

    Without this, a ``ready`` document with mismatched embeddings was never
    auto-fixed by any sweeper — the only remedy was the dashboard "Re-embed
    all" button — even though the UI advertised "auto-remediation active". This
    makes that promise real: it does exactly what the manual
    ``reprocess_all_stale`` endpoint does (reset the docs to ``pending`` and
    re-enqueue, the stale vectors searchable until replaced) but autonomously
    and batch-capped.

    A document is *stale* when its stored embeddings have ``embedding_model``
    different from the platform target OR an effective dimension (stored
    ``embedding_dimensions`` falling back to ``vector_dims``) different from the
    configured target. Selection mirrors ``_load_kb_stale_embedding_info`` /
    ``reprocess_all_stale`` so the dashboard "stale" badge and this sweeper
    agree on what counts as stale.

    Termination is guaranteed: re-embedding either produces matching vectors
    (model + dimensions now agree — see the OpenAI ``dimensions`` fix) so the
    doc leaves the stale set, or the document processor's dimension-convergence
    guard flips it to ``error`` (a config mismatch the admin must fix), which
    also removes it from the ``status = 'ready'`` candidate set. Either way the
    sweeper drains and stops.

    Returns the number of documents successfully re-enqueued (capped at
    ``batch_size``).

    ``embedding_ready`` reuses a caller's per-tick embedding probe — see
    :func:`recover_stuck_documents`.
    """
    if not app_settings.embedding_stale_auto_remediation_enabled:
        return 0
    if batch_size is None:
        batch_size = app_settings.embedding_stale_remediation_batch_size
    batch_size = max(1, batch_size)
    now = now or datetime.now(UTC)

    # Stale is evaluated against the current platform embedding target. When the
    # target isn't fully configured (no model or no dimension), stale cannot be
    # evaluated reliably — bail (matches ``_load_kb_stale_embedding_info``).
    from app.api.v1.platform_settings_helpers import get_platform_settings

    ps = await get_platform_settings(db)
    expected_model = ps.embedding_model
    expected_dims = ps.embedding_dimensions
    if not expected_model or expected_dims is None:
        return 0

    # A document is stale when it has at least one stored document-embedding
    # whose model or effective dimension differs from the platform target.
    # ``source_id`` is a string column, so the join casts the document UUID.
    stale_embedding_exists = (
        select(Embedding.id)
        .where(
            Embedding.source_type == "document",
            Embedding.source_id == cast(KnowledgeDocument.id, String),
            Embedding.knowledge_base_id == KnowledgeDocument.knowledge_base_id,
            or_(
                Embedding.embedding_model.is_distinct_from(expected_model),
                func.coalesce(
                    Embedding.embedding_dimensions,
                    func.vector_dims(Embedding.embedding),
                ).is_distinct_from(expected_dims),
            ),
        )
        .correlate(KnowledgeDocument)
        .exists()
    )

    result = await db.execute(
        select(KnowledgeDocument)
        .where(
            KnowledgeDocument.status == "ready",
            stale_embedding_exists,
        )
        .order_by(KnowledgeDocument.updated_at.asc())
        .limit(batch_size)
        .with_for_update(skip_locked=True)
    )
    due = list(result.scalars().all())
    if not due:
        return 0

    # Probe the embedding configuration once for the whole batch. If embedding
    # isn't usable right now, do NOT churn the docs — leave them ``ready`` and
    # release the ``FOR UPDATE SKIP LOCKED`` row locks (the autonomous tick
    # reuses this session across all downstream work; a leaked lock would block
    # the worker and other maintenance). Mirrors the sibling sweepers.
    state, reason = await _sweep_embedding_state(embedding_ready)
    if state is False:
        logger.info(
            "Stale-document remediation skipped for %d document(s): %s",
            len(due),
            reason,
        )
        await db.rollback()
        return 0
    if state is None:
        logger.warning(
            "Stale-document remediation batch pre-check failed; proceeding "
            "best-effort for %d document(s): %s",
            len(due),
            reason,
        )

    due_ids = [str(doc.id) for doc in due]

    # The stale vectors stay searchable until the processor swaps them; a
    # re-embed the dimension guard refuses leaves them in place.
    for doc in due:
        doc.status = "pending"
        doc.error_message = None
        # Fresh automatic-retry budget — this is a remediation, not a failure.
        doc.embedding_retry_count = 0
        doc.embedding_next_retry_at = None
        doc.updated_at = now

    await db.commit()

    # Enqueue after commit so the SQL transaction never spans the SAQ network
    # round-trip. A failed enqueue leaves the doc ``pending`` and the
    # stuck-document sweeper recovers it on a later tick — the doc is no longer
    # in the stale-``ready`` set so it won't be double-counted here.
    successfully_reenqueued: list[str] = []
    for doc_id in due_ids:
        if await enqueue_document_processing(doc_id, embedding_ready=True):
            successfully_reenqueued.append(doc_id)
        else:
            logger.warning(
                "Stale-document remediation failed to re-enqueue document %s "
                "(see preceding log line for the underlying cause); the "
                "stuck-document sweeper will pick it up on a later tick",
                doc_id,
            )

    logger.info(
        "Stale-document remediation re-enqueued %d of %d due document(s) for "
        "embedding (target model=%s, dimensions=%s)",
        len(successfully_reenqueued),
        len(due),
        expected_model,
        expected_dims,
    )
    return len(successfully_reenqueued)


async def log_kb_activity(
    db: AsyncSession,
    kb_id: str | uuid.UUID,
    event: str,
    details: str | None = None,
    source_type: str | None = None,
    document_count: int | None = None,
) -> None:
    """Insert a KB activity log entry (no flush — caller controls commit)."""
    log = KBActivityLog(
        knowledge_base_id=uuid.UUID(str(kb_id)),
        event=event,
        details=details,
        source_type=source_type,
        document_count=document_count,
    )
    db.add(log)
