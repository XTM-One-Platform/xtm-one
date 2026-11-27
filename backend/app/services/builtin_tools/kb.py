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

"""Knowledge base search/store tools, KB name loading, and KB prompt formatting.

Originally moved verbatim from the ``app/services/builtin_tools.py`` module as
part of the package split, then extended with the full-source-document tools
(``list_knowledge_documents`` / ``get_knowledge_document`` /
``update_knowledge_document``) so an agent can reach the whole
document a set of retrieved chunks came from — and keep it current — instead of
only ever seeing ranked fragments.
"""

from __future__ import annotations

import re
import uuid
from collections import Counter
from typing import TYPE_CHECKING

from sqlalchemy import select as sa_select

from app.core.utils import format_exception_message, is_zero_norm_vector, safe_uuid
from app.services.kb_document_errors import (
    NO_VALID_CHUNKS_MESSAGE,
    classify_embedding_failures,
    public_error_message,
)
from app.services.kb_sections import Heading, SectionError, find_section, insert_after, parse_outline, replace_section
from app.tools.base import CallableToolExecutor, ToolDefinition, register_fetched_file

from ._shared import list_available_files, logger, persist_tool_artifact, resolve_file_ref

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.knowledge_base import KnowledgeBase, KnowledgeDocument
    from app.tools.base import FileContext


# ── Shared KB argument resolution ────────────────────────────────────────────


class _KBTargetResolver:
    """Resolve a ``knowledge_base`` tool argument against the KBs in scope.

    *kbs* is a list of ``(kb_id, kb_name)`` tuples. KB names are NOT unique (no
    DB constraint), so a name only resolves when it maps to exactly one KB in
    scope; an ambiguous name resolves to *None* so the caller can tell the agent
    to disambiguate by id rather than silently picking whichever KB happened to
    come last in the list.

    Two resolution modes, because read and write want different defaults for an
    omitted argument:

    * :meth:`resolve_one` — a single target, defaulting to the first KB. Used by
      the write tools, where "write somewhere" must land in exactly one place.
    * :meth:`resolve_scope` — a list of targets, defaulting to *all* KBs in
      scope. Used by the read tools, where omitting the argument naturally means
      "look across everything I can read".
    """

    def __init__(self, kbs: list[tuple[str, str]]) -> None:
        self.kbs = list(kbs)
        self._valid_ids = {kb_id for kb_id, _ in self.kbs}
        counts = Counter(name.strip().lower() for _, name in self.kbs)
        self._id_by_name = {
            name.strip().lower(): kb_id
            for kb_id, name in self.kbs
            if counts[name.strip().lower()] == 1
        }
        self.default_id = self.kbs[0][0] if self.kbs else None
        self.multi = len(self.kbs) > 1

    def resolve_one(self, knowledge_base: str | None) -> str | None:
        """Return the single target KB id, or *None* when unresolvable."""
        if not knowledge_base:
            return self.default_id
        key = str(knowledge_base).strip()
        if key in self._valid_ids:
            return key
        # Unambiguous-name lookup only; an unknown or ambiguous name yields None
        # so the caller is told to pick by id.
        return self._id_by_name.get(key.lower())

    def resolve_scope(self, knowledge_base: str | None) -> list[str] | None:
        """Return the KB ids to look in, or *None* when unresolvable.

        An omitted argument widens to every KB in scope.
        """
        if not knowledge_base:
            return [kb_id for kb_id, _ in self.kbs]
        resolved = self.resolve_one(knowledge_base)
        return None if resolved is None else [resolved]

    @property
    def options(self) -> str:
        """``"Name (id), Name (id)"`` — for disambiguation error messages."""
        return ", ".join(f"{name} ({kb_id})" for kb_id, name in self.kbs)

# ── Knowledge Base Search ────────────────────────────────────────────────────


def build_kb_search_tool(
    db: AsyncSession,
    user_id: str,
    kb_ids: list[str],
    *,
    _embedding_provider_model: tuple | None = None,
) -> tuple[list[ToolDefinition], CallableToolExecutor] | None:
    """Build the knowledge-base semantic-search tool.

    When *_embedding_provider_model* is supplied as ``(provider, model)``
    the embedding provider is captured in the closure and reused across
    all search calls within the same agent loop, avoiding repeated DB
    lookups.

    Returns *None* when *kb_ids* is empty.
    """
    if not kb_ids:
        return None

    from app.services.vector import make_search_knowledge_fn

    search = make_search_knowledge_fn(
        db,
        user_id,
        kb_ids,
        _embedding_provider_model=_embedding_provider_model,
    )
    names: dict[str, str] = {}

    async def _search_knowledge(query: str, limit: int = 5, knowledge_base: str = "") -> str:
        """Search the bound knowledge bases, or the ones *knowledge_base* names."""
        requested = str(knowledge_base or "").strip()
        if not requested:
            return await search(query, limit)
        if not names:
            from app.core.database import async_session

            async with async_session() as _db:
                names.update(await load_kb_names(_db, kb_ids))
        resolver = _KBTargetResolver(
            [(kb_id, names[kb_id]) for kb_id in kb_ids if kb_id in names]
        )
        scope = resolver.resolve_scope(requested)
        if scope is None and "," in requested:
            parts = [resolver.resolve_scope(part.strip()) for part in requested.split(",") if part.strip()]
            scope = None if any(p is None for p in parts) else [k for p in parts for k in p]
        if scope is None:
            return (
                f"Unknown or ambiguous knowledge base '{requested}'. Pass the name "
                f"or id of one this agent can search: {resolver.options}."
            )
        return await search(query, limit, scope_kb_ids=scope)

    executor = CallableToolExecutor()
    executor.register("search_knowledge", _search_knowledge)

    definition = ToolDefinition(
        name="search_knowledge",
        description=(
            "Search (READ) the agent's knowledge bases (vectorized documents) for "
            "information relevant to the query. Use this to find answers from "
            "uploaded documentation, wikis, style guides, and other reference "
            "material. Every hit names its knowledge base and document. This "
            "tool only reads — it never modifies a knowledge base."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The search query — what information are you looking for?",
                },
                "limit": {
                    "type": "integer",
                    "description": "Number of results to return (default 5)",
                    "default": 5,
                },
                "knowledge_base": {
                    "type": "string",
                    "description": (
                        "Optional: search only this knowledge base (its name or id; "
                        "several comma-separated). Omit to search all of them."
                    ),
                    "default": "",
                },
            },
            "required": ["query"],
        },
        source="builtin:knowledge",
    )

    return [definition], executor


# ── Knowledge Base Write (store_knowledge) ────────────────────────────────────


async def load_kb_names(db: AsyncSession, kb_ids: list[str]) -> dict[str, str]:
    """Return a ``{kb_id: name}`` map for the given knowledge base ids.

    Silently skips ids that are not valid UUIDs or not found. Used to build
    human-readable KB capability prompts and ``store_knowledge`` tool
    descriptions.
    """
    if not kb_ids:
        return {}
    from app.models.knowledge_base import KnowledgeBase

    uuids: list[uuid.UUID] = []
    for kid in kb_ids:
        parsed = safe_uuid(str(kid))
        if parsed is not None:
            uuids.append(parsed)
    if not uuids:
        return {}
    result = await db.execute(
        sa_select(KnowledgeBase.id, KnowledgeBase.name).where(
            KnowledgeBase.id.in_(uuids)
        )
    )
    return {str(row[0]): row[1] for row in result.all()}


async def load_write_locked_kb_ids(db: AsyncSession, kb_ids: list[str]) -> set[str]:
    """Return the subset of *kb_ids* whose knowledge base's content is locked.

    Read-only (ground truth), installed by a skill package or
    built-in: ``helpers.knowledge_base_content_lock``, the rule the write tools
    refuse at execution time. Callers strip these from an agent's WRITABLE
    scope before building the write tools, so the tools are never offered for
    a locked collection - a per-agent write grant must not be able to override
    a lock that lives on the knowledge base. Two queries for any number of ids.
    """
    if not kb_ids:
        return set()
    from app.api.v1.helpers import content_locked_knowledge_base_ids
    from app.models.knowledge_base import KnowledgeBase

    uuids: list[uuid.UUID] = []
    for kid in kb_ids:
        parsed = safe_uuid(str(kid))
        if parsed is not None:
            uuids.append(parsed)
    if not uuids:
        return set()
    result = await db.execute(
        sa_select(KnowledgeBase).where(KnowledgeBase.id.in_(uuids))
    )
    kbs = list(result.scalars().all())
    return set(await content_locked_knowledge_base_ids(db, kbs))


def build_kb_store_tool(
    db: AsyncSession,
    user_id: str,
    writable_kbs: list[tuple[str, str]],
    *,
    source_type: str = "agent",
) -> tuple[list[ToolDefinition], CallableToolExecutor] | None:
    """Build the knowledge-base write tool (``store_knowledge``).

    *writable_kbs* is a list of ``(kb_id, kb_name)`` tuples the agent is allowed
    to write to. Returns *None* when there is nothing writable.

    When more than one KB is writable, the tool exposes a ``knowledge_base``
    argument (KB name or id) so the agent can choose the target; it defaults to
    the first writable KB when omitted. With a single writable KB the argument is
    unnecessary and the lone KB is always used.

    *source_type* is stored on the created ``KnowledgeDocument`` provenance
    (e.g. ``"agent"`` for chat, ``"assignment"`` for assignment runs).
    """
    del user_id  # store uses an independent session; kept for signature parity
    if not writable_kbs:
        return None

    import hashlib

    resolver = _KBTargetResolver(writable_kbs)
    multi = resolver.multi

    async def _store_file_document(
        target_kb_id: str,
        title: str | None,
        file_id: str,
        replace: bool,
        file_context: FileContext | None,
    ) -> str:
        """Store a conversation file as a document: its text, queued for embedding.

        The text is the knowledge-base upload's own extraction
        (``chunking.extract_text_from_file``), and the embedding runs on the
        document-processing queue like an upload's, so a large file is not
        embedded inside one tool call.
        """

        ref = resolve_file_ref(file_context, file_id)
        if ref is None:
            return (
                f"Error: no file '{file_id}' is attached to this conversation. "
                f"{list_available_files(file_context)}"
            )
        try:
            raw = await file_context.read_bytes(ref.file_id)
        except Exception as exc:
            logger.warning(
                "store_knowledge could not load %s (%s): %s",
                ref.file_id,
                ref.filename,
                format_exception_message(exc),
            )
            raw = None
        if raw is None:
            return (
                f"Error: the content of '{ref.filename}' could not be loaded "
                "from storage. The file may have been removed."
            )
        from app.services.chunking import extract_text_from_file_async

        try:
            text = await extract_text_from_file_async(raw, ref.filename)
        except Exception as exc:
            logger.warning(
                "store_knowledge could not extract %s (%s): %s",
                ref.file_id,
                ref.filename,
                format_exception_message(exc),
            )
            text = ""
        if not text or len(text.strip()) < 10:
            return (
                f"Error: '{ref.filename}' holds no text a knowledge base can index "
                "(a binary or empty file). Nothing was stored."
            )
        doc_title = (title or "").strip() or ref.filename
        try:
            from app.core.database import async_session
            from app.models.knowledge_base import KnowledgeBase
            from app.services.kb_documents import insert_document
            from app.services.kb_helpers import enqueue_document_processing

            async with async_session() as file_db:
                kb = (
                    await file_db.execute(
                        sa_select(KnowledgeBase).where(KnowledgeBase.id == target_kb_id)
                    )
                ).scalar_one_or_none()
                if not kb:
                    return "Error: Knowledge base not found."
                if lock_err := await _agent_write_lock_error(file_db, kb):
                    return lock_err
                if replace:
                    await _delete_titled_documents(file_db, kb, doc_title)
                doc = await insert_document(
                    file_db, kb, filename=doc_title, source_type=source_type, content=text
                )
                stored_doc_id = str(doc.id)
                kb_name = kb.name
                await file_db.commit()
            enqueued = await enqueue_document_processing(stored_doc_id)
        except Exception as e:
            logger.error("store_knowledge (file) failed: %s", format_exception_message(e), exc_info=True)
            return "Failed to store in knowledge base. Please try again later."
        summary = (
            f"Stored '{doc_title}' in knowledge base '{kb_name}' (document id: "
            f"{stored_doc_id}) from the file {ref.filename} (file_id {ref.file_id}, "
            f"{len(text):,} characters of text)."
        )
        if enqueued:
            return (
                f"{summary} Chunking and embedding are queued: it is readable in full "
                "via get_knowledge_document now, and searchable via search_knowledge "
                "once its status is 'ready'."
            )
        return (
            f"{summary} Embedding could NOT be queued (no embedding provider "
            "configured, or the queue is unavailable): it is readable in full via "
            "get_knowledge_document, NOT searchable yet, and the platform's recovery "
            "sweep embeds it once embedding works again."
        )

    async def _store_knowledge(
        title: str,
        content: str | None = None,
        replace: bool = True,
        knowledge_base: str | None = None,
        file_id: str | None = None,
        file_context: FileContext | None = None,
    ) -> str:
        target_kb_id = resolver.resolve_one(knowledge_base)
        if target_kb_id is None:
            # Include ids so a duplicate/ambiguous name can be disambiguated.
            return (
                f"Error: '{knowledge_base}' is not a writable knowledge base "
                "(or its name is ambiguous - use the id). "
                f"Choose one of: {resolver.options}."
            )
        if isinstance(file_id, str) and file_id.strip():
            if content is not None:
                return "Error: pass either 'content' or 'file_id', not both."
            return await _store_file_document(target_kb_id, title, file_id.strip(), replace, file_context)
        if not isinstance(content, str):
            return (
                "Error: provide the document text in 'content', or a file of this "
                "conversation in 'file_id'."
            )
        try:
            from sqlalchemy import select as sql_select

            from app.core.database import async_session
            from app.llm.router import get_embedding_provider
            from app.models.embedding import Embedding
            from app.models.knowledge_base import KnowledgeBase, KnowledgeDocument
            from app.services.chunking import chunk_text
            from app.services.kb_embedding_text import embedding_text_for_async
            from app.services.kb_helpers import bump_kb_revision

            # ── Phase A (short session): read KB config + resolve provider ──
            # The chunk-embedding loop below is a series of HTTP round-trips
            # that can take 10-30s for a large document.  Holding an open
            # session/transaction across it pins one pooled DB connection per
            # in-flight ``store_knowledge`` call, so the config read
            # closes its session before any embedding happens and all writes
            # land in one short transaction afterwards (Phase C).
            async with async_session() as cfg_db:
                kb_result = await cfg_db.execute(
                    sql_select(KnowledgeBase).where(KnowledgeBase.id == target_kb_id)
                )
                kb_cfg = kb_result.scalar_one_or_none()
                if not kb_cfg:
                    return "Error: Knowledge base not found."
                # Defence in depth: the shared content-write gate (package-
                # installed, built-in, read-only), refused before spending any
                # embedding calls. A read-only KB is already stripped from the
                # writable scope, so reaching one means the KB was locked mid-run.
                if lock_err := await _agent_write_lock_error(cfg_db, kb_cfg):
                    return lock_err
                kb_chunk_size = kb_cfg.chunk_size
                kb_chunk_overlap = kb_cfg.chunk_overlap
                kb_html_ingestion = getattr(kb_cfg, "html_ingestion", None)

                (
                    embedding_provider,
                    embed_model,
                    provider_config_id,
                ) = await get_embedding_provider(cfg_db)
                provider_name = getattr(embedding_provider, "provider_name", None)

            # ── Phase B (no session): chunk + embed over HTTP ──────────────
            # Same choice as the document processor: the stored source, or an
            # HTML document's rendered text when the KB opted in.
            chunks = chunk_text(
                await embedding_text_for_async(content, title, kb_html_ingestion),
                chunk_size=kb_chunk_size,
                chunk_overlap=kb_chunk_overlap,
            )
            embedded_chunks: list[tuple[str, list[float]]] = []
            failed_count = 0
            # What the failed calls raised: a document with no embedded chunk
            # records their cause, as the document processor does.
            embed_failures: list[Exception] = []
            for chunk_index, chunk in enumerate(chunks, start=1):
                if len(chunk.strip()) < 10:
                    continue
                try:
                    embedding_vector = await embedding_provider.embed(
                        chunk, model=embed_model
                    )
                    # Mirror document_processor._embed_one: a provider
                    # that returns None/[] or an all-zeros vector did NOT
                    # produce a usable embedding — persisting it would
                    # mark the doc "ready" while the chunk is invisible
                    # (or broken) for similarity search. Count it as a
                    # failed chunk instead.
                    if not embedding_vector:
                        failed_count += 1
                        logger.warning(
                            "Embedding provider returned empty vector for "
                            "chunk %d of '%s' in KB %s",
                            chunk_index,
                            title,
                            target_kb_id,
                        )
                        continue
                    if is_zero_norm_vector(embedding_vector):
                        failed_count += 1
                        logger.warning(
                            "Embedding provider returned zero-norm vector "
                            "(%d dims) for chunk %d of '%s' in KB %s",
                            len(embedding_vector),
                            chunk_index,
                            title,
                            target_kb_id,
                        )
                        continue
                    embedded_chunks.append((chunk, embedding_vector))
                except Exception as e:
                    failed_count += 1
                    embed_failures.append(e)
                    # No outcome prediction here: the final document
                    # state (error vs partial index) is only known after
                    # the loop and is reported by the dedicated
                    # zero-chunk / partial-indexing warnings below.
                    logger.error(
                        "Failed to embed chunk %d of '%s' in KB %s "
                        "(chunk_len=%d): %s",
                        chunk_index,
                        title,
                        target_kb_id,
                        len(chunk or ""),
                        format_exception_message(e),
                        exc_info=True,
                    )

            chunk_count = len(embedded_chunks)
            attempted = chunk_count + failed_count

            # ── Phase C (short session): apply all writes atomically ──────
            async with async_session() as kb_db:
                kb_result = await kb_db.execute(
                    sql_select(KnowledgeBase).where(KnowledgeBase.id == target_kb_id)
                )
                kb = kb_result.scalar_one_or_none()
                if not kb:
                    return "Error: Knowledge base not found."

                if replace:
                    await _delete_titled_documents(kb_db, kb, title)

                content_hash = hashlib.sha256(content.encode()).hexdigest()
                doc = KnowledgeDocument(
                    knowledge_base_id=kb.id,
                    filename=title,
                    source_type=source_type,
                    content=content,
                    content_hash=content_hash,
                    status="processing",
                )
                kb_db.add(doc)
                await kb_db.flush()
                await bump_kb_revision(kb_db, kb.id)

                if chunk_count == 0:
                    # Nothing embedded — mirror document_processor semantics:
                    # a "ready" doc with 0 chunks would be invisible to RAG
                    # while looking healthy. Mark it as an error instead so
                    # operators (and reprocessing) can pick it up.  An
                    # embedding-provider failure is transient/retryable (the
                    # auto-retry sweeper re-embeds it once the provider is
                    # back); no extractable text is structural and never
                    # auto-retried — same split as document_processor.
                    from app.services.kb_helpers import schedule_embedding_retry

                    doc.status = "error"
                    doc.error_message = (
                        classify_embedding_failures(
                            embed_failures,
                            returned_empty=failed_count > len(embed_failures),
                        )[1]
                        if failed_count
                        else NO_VALID_CHUNKS_MESSAGE
                    )
                    doc.chunk_count = 0
                    schedule_embedding_retry(doc, retryable=bool(failed_count))
                    kb.document_count = (kb.document_count or 0) + 1
                    await kb_db.commit()
                    # Include the cause: in the no-extractable-text path
                    # ``attempted`` is 0, so "zero of 0 chunk(s)" alone is
                    # ambiguous during ops triage — the error message names
                    # the actual failure (provider vs no text).
                    logger.warning(
                        "store_knowledge: zero of %d chunk(s) embedded for "
                        "'%s' in KB %s — document marked as error: %s",
                        attempted,
                        title,
                        kb.id,
                        doc.error_message,
                    )
                    if failed_count:
                        # ``schedule_embedding_retry`` may decline to schedule
                        # (auto-retry disabled via settings) — only promise an
                        # automatic retry when one is actually on the books.
                        retry_hint = (
                            "it will be retried automatically once the "
                            "provider recovers."
                            if doc.embedding_next_retry_at is not None
                            else "ask an administrator to fix the provider "
                            "and reprocess the document."
                        )
                        return (
                            f"Error: failed to store '{title}' in knowledge "
                            f"base '{kb.name}' — none of the {attempted} "
                            "chunk(s) could be embedded. The document was "
                            "saved with status 'error' and is NOT searchable. "
                            "The embedding provider may be misconfigured or "
                            f"unavailable; {retry_hint}"
                        )
                    return (
                        f"Error: failed to store '{title}' in knowledge base "
                        f"'{kb.name}' — the content contains no extractable "
                        "text after filtering out chunks shorter than 10 "
                        "characters. The document was saved with status "
                        "'error' and is NOT searchable. Provide more "
                        "substantial content and store it again."
                    )

                for chunk, embedding_vector in embedded_chunks:
                    kb_db.add(
                        Embedding(
                            user_id=None if kb.is_company_managed else kb.user_id,
                            knowledge_base_id=kb.id,
                            source_type="document",
                            source_id=str(doc.id),
                            embedding_provider=provider_name,
                            embedding_provider_id=provider_config_id,
                            embedding_model=embed_model,
                            embedding_dimensions=len(embedding_vector),
                            content=chunk,
                            embedding=embedding_vector,
                        )
                    )

                doc.status = "ready"
                doc.chunk_count = chunk_count
                kb.embedding_model = embed_model
                kb.document_count = (kb.document_count or 0) + 1
                kb.chunk_count = (kb.chunk_count or 0) + chunk_count
                # Captured before the session closes so the returned message can
                # hand the agent the document id — the stable handle for
                # ``get_knowledge_document`` / ``update_knowledge_document``
                # (titles are not unique across knowledge bases).
                stored_doc_id = str(doc.id)
                await kb_db.commit()

            if failed_count:
                logger.warning(
                    "store_knowledge: partial indexing for '%s' in KB %s — "
                    "%d of %d chunk(s) embedded, %d failed",
                    title,
                    kb.id,
                    chunk_count,
                    attempted,
                    failed_count,
                )
                return (
                    f"Stored '{title}' in knowledge base '{kb.name}' "
                    f"(document id: {stored_doc_id}) with a "
                    f"PARTIAL index: {chunk_count} of {attempted} chunks "
                    f"embedded, {failed_count} chunk(s) failed to embed and "
                    "are NOT searchable. The searchable portion is available "
                    "via search_knowledge; the full text is always readable "
                    "verbatim via get_knowledge_document."
                )
            return (
                f"Stored '{title}' in knowledge base '{kb.name}' "
                f"(document id: {stored_doc_id}, {chunk_count} chunks "
                "embedded). It is now searchable via search_knowledge, and "
                "readable in full via get_knowledge_document."
            )
        except Exception as e:
            logger.error("store_knowledge failed: %s", format_exception_message(e), exc_info=True)
            return "Failed to store in knowledge base. Please try again later."

    executor = CallableToolExecutor()
    executor.register("store_knowledge", _store_knowledge)

    if multi:
        # Include the id alongside each name: KB names are not unique, so the
        # id lets the agent target the right KB directly (and avoids tripping
        # the ambiguous-name rejection in the tool body).
        target_list = "; ".join(f"'{name}' (id: {kid})" for kid, name in writable_kbs)
        description = (
            "Store (WRITE) a document in one of the agent's writable knowledge "
            "bases. The content is chunked and embedded for later RAG search via "
            "search_knowledge. Use this to persist analysis results, style "
            f"guides, or reference data. Writable knowledge bases: {target_list}. "
            "Specify which one via the 'knowledge_base' argument (name or id; "
            "prefer the id when names collide; defaults to the first). If a "
            "document with the same title exists, it is replaced."
        )
    else:
        only_name = writable_kbs[0][1]
        description = (
            "Store (WRITE) a document in the agent's knowledge base "
            f"'{only_name}'. The content is chunked and embedded for later RAG "
            "search via search_knowledge. Use this to persist analysis results, "
            "style guides, or reference data. If a document with the same title "
            "exists, it is replaced."
        )

    properties: dict[str, object] = {
        "title": {
            "type": "string",
            "description": "Document title (e.g. 'writing-style-guide', 'analysis-feb-2026')",
        },
        "content": {
            "type": "string",
            "description": "The text content to store (markdown supported)",
        },
        "file_id": {
            "type": "string",
            "description": (
                "Instead of content: a file of this conversation (its file_id, "
                "marker or exact filename) - its text is extracted and stored "
                "like a knowledge base upload."
            ),
        },
        "replace": {
            "type": "boolean",
            "description": "Replace existing document with the same title (default true)",
            "default": True,
        },
    }
    if multi:
        properties["knowledge_base"] = {
            "type": "string",
            "description": (
                "Which writable knowledge base to store into (name or id). "
                "Defaults to the first writable knowledge base when omitted."
            ),
        }

    definition = ToolDefinition(
        name="store_knowledge",
        description=description,
        input_schema={
            "type": "object",
            "properties": properties,
            "required": ["title"],
        },
        source="builtin:knowledge",
    )

    del db  # store uses an independent session
    return [definition], executor


# ── Full source documents (read + update) ─────────────────────────────
#
# ``search_knowledge`` is lossy by design: chunking + similarity retrieval finds
# relevant fragments but can never reconstruct a document verbatim. The tools
# below close that gap — they are deterministic exact-match operations on the
# ``knowledge_documents`` rows (where the original text is stored at ingest time)
# rather than semantic queries over the vector layer:
#
#   * ``list_knowledge_documents``  — enumerate documents (ids, titles, status).
#   * ``get_knowledge_document``    — read one document's full, verbatim source.
#   * ``update_knowledge_document`` — replace/append its source and re-embed.
#
# Because the text lives on the document row, a read works even while embedding
# is still pending — the write-then-read case ``search_knowledge`` cannot serve.

# Default / maximum size of one ``get_knowledge_document`` content window.
#
# The agent loop caps every tool result centrally
# (``agent_loop._MAX_TOOL_RESULT_CHARS_CAP`` and the window-scaled per-result
# budget below it), but that cap TRUNCATES — it would silently drop the tail of a
# large document, which is precisely what a "give me the complete verbatim
# source" tool must not do. So this tool windows explicitly instead: it returns a
# bounded slice, states the total length, and tells the agent the ``offset`` to
# continue from. Paging is visible and lossless where central truncation is
# invisible and lossy. The default stays comfortably under the smallest
# per-result budget (a 128K-token model allows ~76K chars) so a slice is never
# centrally truncated on top of being windowed.
_DOC_WINDOW_DEFAULT_CHARS = 50_000
_DOC_WINDOW_MAX_CHARS = 150_000

# Above this source length a document is NOT injected into the context at all —
# it is registered as a durable work item instead (see
# ``_offload_oversized_document``).
#
# Why a hard ceiling on top of windowing: windowing keeps any SINGLE result
# bounded, but an agent walking a 2 MB document would need ~40 slices, and each
# one displaces earlier context. Long before the end it has evicted the
# beginning, so it never holds the document it set out to read — it just burns
# the whole context window to arrive nowhere. Past this point paging is not a
# smaller version of the right answer, it is the wrong shape of work: it belongs
# in a durable task that can be processed out of band.
#
# 200K characters ~= 50K tokens: four default windows, and already a third to a
# half of a typical working context. Below it, paging still leaves room to
# actually reason about what was read; above it, it does not. An explicit
# ``offset`` / ``max_chars`` request is always honoured regardless — a caller
# asking for one known slice of a large document is doing something bounded and
# deliberate, and must not be redirected.
_DOC_CONTEXT_LIMIT_CHARS = 200_000

#: Extensions a document's file copy keeps: its stored source is text of that
#: kind. Any other name (a PDF, a Notion page title) gets ``.txt``.
_TEXT_COPY_EXTENSIONS = frozenset(
    {"txt", "md", "markdown", "csv", "tsv", "json", "jsonl", "ndjson", "html", "htm", "xml", "yaml", "yml"}
)

_DOC_LIST_DEFAULT_LIMIT = 50
_DOC_LIST_MAX_LIMIT = 200

# How many candidate titles to offer when a title lookup misses.
_DOC_TITLE_HINT_LIMIT = 10

VALID_DOCUMENT_UPDATE_MODES = ("replace", "append", "replace_section", "insert_after", "edit")

#: Modes that address one heading section and need ``section``.
_SECTION_MODES = ("replace_section", "insert_after")

#: A ``replace`` keeping less than this share of the stored source is refused
#: unless ``confirm_shrink`` is set: a truncated model output or a stray file
#: marker must not silently wipe a long document.
_SHRINK_GUARD_RATIO = 0.5
#: ...for a stored source of at least this many characters: rewriting a short
#: note into a shorter one is ordinary editing.
_SHRINK_GUARD_MIN_CHARS = 2_000

#: A ``content`` that is nothing but a conversation file marker.
_FILE_MARKER_ONLY_RE = re.compile(r"^\s*\[\[FILE:[^\]]+\]\]\s*$")


def _format_outline(headings: list[Heading]) -> list[str]:
    """One line per heading: index, level, offset, length and the indented text."""
    if not headings:
        return ["Outline: this document has no markdown heading."]
    lines = [
        f"Outline ({len(headings)} headings; '#N' is the index a 'section' "
        "argument accepts, offsets are character offsets in the source):"
    ]
    for heading in headings:
        indent = "  " * (heading.level - 1)
        lines.append(
            f"#{heading.index} {indent}{'#' * heading.level} {heading.text} "
            f"[offset {heading.start}, {heading.length} chars]"
        )
    return lines


def _kb_uuids(kb_ids: list[str]) -> list[uuid.UUID]:
    """Parse *kb_ids* to UUIDs, silently dropping unparseable entries."""
    parsed = [safe_uuid(str(k)) for k in kb_ids]
    return [u for u in parsed if u is not None]


def _read_only_kb_error(kb_name: str) -> str:
    """The message every write tool returns for a content-locked KB.

    Says plainly that this is a property of the collection, not a missing
    permission the agent could route around — otherwise the model retries with a
    different KB argument, or reports the failure as a transient error.
    """
    return (
        f"Error: knowledge base '{kb_name}' is READ-ONLY — it is maintained as "
        "ground truth and its content cannot be changed by an agent. This is not "
        "a transient failure and not something you can retry or work around: do "
        "not attempt another write to it. If the stored content is genuinely "
        "wrong or out of date, report what should change and who should change "
        "it, and continue with the rest of the task."
    )


# SQLSTATE classes whose failures may succeed when the same call is simply made
# again: connection exceptions (08), transaction rollbacks such as a deadlock or
# a serialization failure (40), insufficient resources (53) and operator
# intervention such as a cancelled statement or a shutdown (57).
_TRANSIENT_SQLSTATE_CLASSES = ("08", "40", "53", "57")


def _sqlstate(exc: BaseException) -> str | None:
    """The PostgreSQL SQLSTATE an exception (or the driver error it wraps) carries."""
    for candidate in (exc, getattr(exc, "orig", None)):
        for attr in ("sqlstate", "pgcode"):
            value = getattr(candidate, attr, None)
            if isinstance(value, str) and len(value) == 5:
                return value
    return None


def _document_write_failure(exc: BaseException, stage: str, new_length: int | None) -> str:
    """The ``update_knowledge_document`` answer for an unexpected exception.

    Raised before the commit, so nothing was written: the answer names the
    cause by its class (never its message, which may carry SQL or data), the
    *stage* it happened at, and whether the same call can succeed again - an
    agent must be able to tell a retry from a change of input from a stop.
    """
    from sqlalchemy.exc import DataError, DBAPIError, IntegrityError
    from sqlalchemy.orm.exc import ObjectDeletedError, StaleDataError

    from app.core.db_availability import is_transient_connect_error

    kind = type(exc).__name__
    sqlstate = _sqlstate(exc) or ""
    unchanged = "Nothing was changed."
    if (
        isinstance(exc, TimeoutError | ConnectionError)
        or sqlstate[:2] in _TRANSIENT_SQLSTATE_CLASSES
        or (isinstance(exc, DBAPIError) and exc.connection_invalidated)
        or is_transient_connect_error(exc)
    ):
        return (
            f"Error: the database was briefly unavailable while {stage} "
            f"({kind}{', SQLSTATE ' + sqlstate if sqlstate else ''}). {unchanged} "
            "This is transient: call again with the same arguments."
        )
    if isinstance(exc, StaleDataError | ObjectDeletedError):
        return (
            f"Error: the document was deleted or changed by someone else while "
            f"{stage}. {unchanged} Read it again (get_knowledge_document or "
            "list_knowledge_documents), then write your change on the current "
            "version."
        )
    if sqlstate.startswith("54"):
        size = f" (the new source is {new_length} characters)" if new_length is not None else ""
        return (
            f"Error: the database refused the new source as too large{size}. "
            f"{unchanged} The same content fails again: split it into several "
            "documents (store_knowledge) or shorten it."
        )
    if isinstance(exc, DataError) or sqlstate.startswith("22"):
        return (
            f"Error: the database refused the new source as invalid data "
            f"({kind}{', SQLSTATE ' + sqlstate if sqlstate else ''}). {unchanged} "
            "The same content fails again: remove control or malformed "
            "characters, or shorten it."
        )
    if isinstance(exc, IntegrityError):
        return (
            f"Error: the write conflicts with the stored state of the knowledge "
            f"base ({kind}{', SQLSTATE ' + sqlstate if sqlstate else ''}). "
            f"{unchanged} Read the document again before another write."
        )
    return (
        f"Error: update_knowledge_document failed while {stage} ({kind}). "
        f"{unchanged} This is not a known transient failure: the same call is "
        "likely to fail the same way. Report it with the document id; the "
        "cause is in the platform's server log."
    )


async def _delete_titled_documents(db: AsyncSession, kb: KnowledgeBase, title: str) -> None:
    """Delete *kb*'s documents titled *title* and their vectors (``store_knowledge``'s replace)."""
    from sqlalchemy import delete as sql_delete

    from app.models.embedding import Embedding
    from app.models.knowledge_base import KnowledgeDocument

    existing = await db.execute(
        sa_select(KnowledgeDocument).where(
            KnowledgeDocument.knowledge_base_id == kb.id,
            KnowledgeDocument.filename == title,
        )
    )
    for old_doc in list(existing.scalars().all()):
        await db.execute(sql_delete(Embedding).where(Embedding.source_id == str(old_doc.id)))
        old_chunks = old_doc.chunk_count or 0
        await db.delete(old_doc)
        kb.document_count = max((kb.document_count or 0) - 1, 0)
        kb.chunk_count = max((kb.chunk_count or 0) - old_chunks, 0)


async def _agent_write_lock_error(db: AsyncSession, kb: KnowledgeBase) -> str | None:
    """The write tools' refusal for a content-locked KB, else ``None``.

    The decision is the shared gate's (``helpers.knowledge_base_write_refusal``
    without a user: the agent writes under its own grant); the read-only and
    built-in sentences are the tools' own, worded for the model.
    """
    from app.api.v1.helpers import (
        KB_WRITE_BUILTIN,
        KB_WRITE_READ_ONLY,
        knowledge_base_write_refusal,
    )

    refusal = await knowledge_base_write_refusal(db, kb)
    if refusal is None:
        return None
    if refusal.reason == KB_WRITE_READ_ONLY:
        return _read_only_kb_error(kb.name)
    if refusal.reason == KB_WRITE_BUILTIN:
        return (
            f"Error: '{kb.name}' is a built-in knowledge base and its documents "
            "cannot be modified."
        )
    return f"Error: {refusal.detail} ('{kb.name}')."


async def _fetch_kb_document(
    db: AsyncSession,
    kb_ids: list[str],
    *,
    document_id: str | None,
    title: str | None,
) -> tuple[KnowledgeDocument | None, KnowledgeBase | None, str | None]:
    """Look up one document by id or exact title within *kb_ids*.

    Returns ``(document, knowledge_base, error)`` — exactly one of *document* and
    *error* is set. The ``knowledge_base_id IN kb_ids`` predicate is the access
    boundary: *kb_ids* is the agent's readable (or writable) set, so a document
    in a knowledge base the agent is not bound to is indistinguishable from one
    that does not exist.

    Title lookup is exact first, then case-insensitive, and finally reports the
    available titles so an agent that guessed the casing or the wording can
    correct itself without a separate listing call. A title matching several
    documents (titles are unique per store-with-replace, but not across
    knowledge bases, and ``store_knowledge(replace=False)`` may duplicate one)
    is reported as ambiguous with ids rather than resolved arbitrarily.
    """
    from sqlalchemy import func

    from app.models.knowledge_base import KnowledgeBase, KnowledgeDocument

    kb_uuids = _kb_uuids(kb_ids)
    if not kb_uuids:
        return None, None, "Error: no knowledge base is in scope for this tool."

    docs: list[KnowledgeDocument] = []
    if document_id:
        from .id_prefix import id_prefix, resolve_id_prefix

        # An id prefix (8+ hexadecimal characters) of one document in scope
        # stands for its full id; one starting several is refused with them.
        document_id, ambiguous = await resolve_id_prefix(
            db,
            KnowledgeDocument,
            document_id,
            [KnowledgeDocument.knowledge_base_id.in_(kb_uuids)],
            noun="documents",
            label=KnowledgeDocument.filename,
        )
        if ambiguous:
            return None, None, ambiguous
        doc_uuid = safe_uuid(str(document_id).strip())
        if doc_uuid is None and id_prefix(document_id) is not None:
            # A well-formed prefix that starts no document in scope: the
            # document is gone or out of scope, not a malformed argument.
            return None, None, (
                f"Error: document `{str(document_id).strip()}` not found in the knowledge bases "
                "in scope: no document id starts with it. Use list_knowledge_documents to get valid ids."
            )
        if doc_uuid is None:
            return None, None, (
                f"Error: '{document_id}' is not a valid document id — expected a "
                "UUID. Use list_knowledge_documents to get valid ids."
            )
        result = await db.execute(
            sa_select(KnowledgeDocument).where(
                KnowledgeDocument.id == doc_uuid,
                KnowledgeDocument.knowledge_base_id.in_(kb_uuids),
            )
        )
        docs = list(result.scalars().all())
        if not docs:
            return None, None, (
                f"Error: no document with id {document_id} in the knowledge "
                "base(s) available to this tool. Use list_knowledge_documents "
                "to see what is there."
            )
    else:
        key = str(title or "").strip()
        if not key:
            return None, None, (
                "Error: provide either 'document_id' or 'title' to identify the "
                "document."
            )
        for predicate in (
            KnowledgeDocument.filename == key,
            func.lower(KnowledgeDocument.filename) == key.lower(),
        ):
            result = await db.execute(
                sa_select(KnowledgeDocument)
                .where(
                    KnowledgeDocument.knowledge_base_id.in_(kb_uuids),
                    predicate,
                )
                .order_by(KnowledgeDocument.created_at.desc())
            )
            docs = list(result.scalars().all())
            if docs:
                break
        if not docs:
            hint_result = await db.execute(
                sa_select(KnowledgeDocument.filename, KnowledgeDocument.id)
                .where(KnowledgeDocument.knowledge_base_id.in_(kb_uuids))
                .order_by(KnowledgeDocument.created_at.desc())
                .limit(_DOC_TITLE_HINT_LIMIT)
            )
            hints = [
                f"'{name}' (id: {doc_id})" for name, doc_id in hint_result.all()
            ]
            available = (
                f" Available documents include: {'; '.join(hints)}."
                if hints
                else " There are no documents in scope."
            )
            return None, None, (
                f"Error: no document titled '{key}' in the knowledge base(s) "
                f"available to this tool.{available}"
            )
        if len(docs) > 1:
            listed = "; ".join(f"id: {d.id} (status: {d.status})" for d in docs)
            return None, None, (
                f"Error: '{key}' matches {len(docs)} documents — pass "
                f"'document_id' to pick one: {listed}."
            )

    doc = docs[0]
    kb_result = await db.execute(
        sa_select(KnowledgeBase).where(KnowledgeBase.id == doc.knowledge_base_id)
    )
    kb = kb_result.scalar_one_or_none()
    if kb is None:
        # The KB row vanished between the document lookup and here (concurrent
        # delete). Treat it as not found rather than rendering a half-resolved
        # document with no home.
        return None, None, (
            f"Error: the knowledge base holding document {doc.id} no longer "
            "exists."
        )
    return doc, kb, None


def _extraction_warning(meta: object) -> str | None:
    """The extraction warning a sync recorded on a document's metadata."""
    if isinstance(meta, dict):
        warning = meta.get("extraction_warning")
        if isinstance(warning, str) and warning:
            return warning
    return None


def _format_document_metadata(
    doc: KnowledgeDocument, kb: KnowledgeBase
) -> list[str]:
    """Render the metadata block shared by the read and update tool outputs."""
    content_length = len(doc.content) if doc.content is not None else 0
    lines = [
        f"Document **{doc.filename}** (id: {doc.id})",
        f"  - Knowledge base: {kb.name} (id: {kb.id})",
        f"  - Source type: {doc.source_type}",
        f"  - Embedding status: {doc.status}",
        f"  - Indexed chunks: {doc.chunk_count or 0}",
        f"  - Source length: {content_length} characters",
    ]
    if doc.source_url:
        lines.append(f"  - Source URL: {doc.source_url}")
    if doc.created_at:
        lines.append(f"  - Created: {doc.created_at.isoformat()}")
    if doc.updated_at:
        lines.append(f"  - Last updated: {doc.updated_at.isoformat()}")
    if doc.status == "error" and doc.error_message:
        lines.append(f"  - Embedding error: {public_error_message(doc.error_message)}")
    warning = _extraction_warning(getattr(doc, "extra_metadata", None))
    if warning:
        lines.append(
            f"  - Extraction warning: {warning}. The source text may be "
            "incomplete: open the original file before relying on it."
        )
    if doc.status != "ready":
        lines.append(
            "  - NOTE: this document is not fully indexed yet, so it is NOT (or "
            "not completely) searchable via search_knowledge. The source text "
            "below is authoritative regardless — it is stored on the document "
            "itself, not derived from the vector index."
        )
    return lines


def build_kb_document_read_tools(
    db: AsyncSession,
    user_id: str,
    readable_kbs: list[tuple[str, str]],
    *,
    agent_id: str | None = None,
    is_company_managed: bool = False,
    company_managed_agent: bool = False,
    conversation_id: str | None = None,
    assignment_run_id: str | None = None,
    file_owner_id: str | None = None,
) -> tuple[list[ToolDefinition], CallableToolExecutor] | None:
    """Build the full-source-document READ tools.

    *readable_kbs* is the ``(kb_id, kb_name)`` list the agent may read — the same
    set ``search_knowledge`` covers. Returns *None* when it is empty.

    Exposes ``list_knowledge_documents`` (enumerate) and
    ``get_knowledge_document`` (verbatim source of one document). Both are exact,
    deterministic lookups on the stored document rows — the complement to
    ``search_knowledge``'s ranked fragments.

    *agent_id* / *is_company_managed* are the identity used to register a work
    item when a document is too large to place in the context at all (see
    ``_DOC_CONTEXT_LIMIT_CHARS``). Without a usable identity the oversized read
    degrades to the ordinary windowed read with an explicit warning rather than
    failing. *company_managed_agent* is the acting agent's own tier, the writer
    of that item (``services/company_managed_writes.py``), like the work-item
    and shared-state tools'.

    *conversation_id* / *assignment_run_id* scope the file copy of a document
    read: the source text is also stored as a working file of the
    conversation or run, registered in the turn's FileContext, so the agent can
    hand the document to a file tool and the user gets a chip. The copy is
    owned by *file_owner_id* when given (``""`` = no owner, a company-managed
    run's files), else by *user_id*.
    """
    if not readable_kbs:
        return None

    resolver = _KBTargetResolver(readable_kbs)
    multi = resolver.multi
    acting_user_id = user_id if user_id and user_id != "anonymous" else None
    copy_owner_id = acting_user_id if file_owner_id is None else (file_owner_id or None)
    #: Per build (one turn): the file copy of each document version read.
    document_copies: dict[tuple[str, str], object] = {}

    async def _document_file_copy(
        doc_id: str,
        filename: str,
        content: str,
        version: str,
        file_context: FileContext | None,
    ) -> str | None:
        """The metadata line naming the document's file copy, or ``None`` when none could be made."""
        if not any(safe_uuid(scope) for scope in (conversation_id, assignment_run_id, copy_owner_id)):
            return None
        key = (doc_id, version)
        ref = document_copies.get(key)
        if ref is None:
            from app.services.file_storage import guess_content_type

            name = " ".join(str(filename or "document").replace("\\", "/").rsplit("/", 1)[-1].split())[:200]
            extension = name.rsplit(".", 1)[-1].lower() if "." in name else ""
            if extension not in _TEXT_COPY_EXTENSIONS:
                name = f"{name or 'document'}.txt"
            try:
                ref = await persist_tool_artifact(
                    user_id=copy_owner_id or "",
                    filename=name,
                    raw_bytes=content.encode("utf-8"),
                    content_type=guess_content_type(name),
                    conversation_id=conversation_id,
                    assignment_run_id=assignment_run_id,
                    file_context=file_context,
                    source="agent_working",
                    fetched=True,
                )
            except Exception as exc:
                logger.warning(
                    "get_knowledge_document could not store a file copy of %s: %s",
                    doc_id,
                    format_exception_message(exc),
                )
                return None
            document_copies[key] = ref
        else:
            register_fetched_file(file_context, ref)
        return (
            f"  - As a file of this conversation: {ref.filename} (file_id {ref.file_id}, "
            f"download marker [[FILE:{ref.file_id}]])"
        )

    async def _offload_oversized_document(
        doc: KnowledgeDocument, kb: KnowledgeBase, total: int
    ) -> str | None:
        """Register an oversized document as a work item; return the handle text.

        Returns *None* when no work item could be created (no usable identity, or
        the write failed), so the caller can fall back to the windowed read.

        Idempotent per document *version*: the correlation key carries the
        document id and its content hash, so repeated reads converge on ONE open
        item instead of spawning one per attempt, while a genuinely new revision
        of the document gets its own item (the old one described different text).
        """
        from app.services.company_managed_writes import agent_writer
        from app.services.work_item import create_work_item, list_work_items

        if acting_user_id is None and not is_company_managed:
            # ``create_work_item`` rejects this combination outright: the row
            # would be invisible to everyone. Don't try.
            return None

        version = doc.content_hash or f"len{total}"
        correlation_key = f"kb-doc-{doc.id}-{version[:16]}"
        writer = agent_writer(is_company_managed=company_managed_agent, agent_id=agent_id)
        try:
            existing = await list_work_items(
                user_id=acting_user_id,
                correlation_key=correlation_key,
                open_only=True,
                limit=1,
                reader=writer,
            )
            if existing:
                item = existing[0]
                reused = True
            else:
                item = await create_work_item(
                    user_id=acting_user_id,
                    title=f"Process oversized knowledge document: {doc.filename}",
                    description=(
                        f"'{doc.filename}' in knowledge base '{kb.name}' is "
                        f"{total} characters — too large to read into an agent "
                        "context in one piece, so its content was NOT injected. "
                        "Read it deliberately in bounded slices with "
                        "get_knowledge_document (document_id="
                        f"{doc.id}, offset=..., max_chars=...), or process it out "
                        "of band. Prefer search_knowledge when you only need the "
                        "relevant passages rather than the whole source."
                    ),
                    owner_agent_id=agent_id,
                    created_by_agent_id=agent_id,
                    priority="medium",
                    correlation_key=correlation_key,
                    payload={
                        "reason": "oversized_knowledge_document",
                        "knowledge_base_id": str(kb.id),
                        "knowledge_base_name": kb.name,
                        "document_id": str(doc.id),
                        "filename": doc.filename,
                        "source_type": doc.source_type,
                        "embedding_status": doc.status,
                        "content_length": total,
                        "context_limit_chars": _DOC_CONTEXT_LIMIT_CHARS,
                        "suggested_window_chars": _DOC_WINDOW_DEFAULT_CHARS,
                    },
                    is_company_managed=is_company_managed,
                    writer=writer,
                )
                reused = False
        except Exception as exc:
            logger.warning(
                "Could not register a work item for oversized document %s "
                "(%d chars); falling back to a windowed read: %s",
                doc.id,
                total,
                format_exception_message(exc),
            )
            return None

        verb = "already tracked by" if reused else "registered as"
        return (
            f"NOT LOADED: this document is {total} characters, above the "
            f"{_DOC_CONTEXT_LIMIT_CHARS}-character limit for placing a document "
            "in the context, so its content was deliberately not injected — "
            "paging through it would consume the whole context window and still "
            "not leave you holding the document.\n"
            f"It is {verb} work item **{item['title']}** "
            f"(id: {item['id']}, status: {item['status']}, "
            f"correlation_key: {item.get('correlation_key')}).\n"
            "Do this instead:\n"
            "  - Use search_knowledge to retrieve just the passages relevant to "
            "your task — that is what retrieval is for, and it is almost always "
            "the right move here.\n"
            "  - If you genuinely need specific text, call "
            "get_knowledge_document again with an explicit bounded window "
            f"(offset=0, max_chars={_DOC_WINDOW_DEFAULT_CHARS}) and page only "
            "the part you need; explicit windows are always served.\n"
            "  - Leave the rest to the work item rather than reading the whole "
            "document now."
        )

    async def _list_knowledge_documents(
        knowledge_base: str | None = None,
        status: str | None = None,
        limit: int = _DOC_LIST_DEFAULT_LIMIT,
    ) -> str:
        scope = resolver.resolve_scope(knowledge_base)
        if scope is None:
            return (
                f"Error: '{knowledge_base}' is not one of the knowledge bases this tool reads - those this agent "
                "is given -, or its name is ambiguous (use the id). "
                f"Choose one of: {resolver.options}."
            )
        # A non-positive or unparseable limit falls back to the default rather
        # than clamping to 1: an agent that sends 0 (or garbage) meant "give me
        # the usual page", and returning a single row would look like the
        # knowledge base is nearly empty.
        try:
            requested_limit = int(limit)
        except (TypeError, ValueError):
            requested_limit = _DOC_LIST_DEFAULT_LIMIT
        if requested_limit <= 0:
            requested_limit = _DOC_LIST_DEFAULT_LIMIT
        capped_limit = min(requested_limit, _DOC_LIST_MAX_LIMIT)

        try:
            from sqlalchemy import func

            from app.core.database import async_session
            from app.models.knowledge_base import KnowledgeBase, KnowledgeDocument

            kb_uuids = _kb_uuids(scope)
            if not kb_uuids:
                return "No readable knowledge base is in scope."

            async with async_session() as read_db:
                # Column select, NOT ``select(KnowledgeDocument)``: a listing only
                # needs each document's size, and loading whole ORM rows would
                # pull every document's full ``content`` over the wire — megabytes
                # for exactly the large-document knowledge bases this feature
                # exists to serve. ``length()`` computes the size server-side.
                query = sa_select(
                    KnowledgeDocument.id,
                    KnowledgeDocument.knowledge_base_id,
                    KnowledgeDocument.filename,
                    KnowledgeDocument.source_type,
                    KnowledgeDocument.status,
                    KnowledgeDocument.chunk_count,
                    func.coalesce(func.length(KnowledgeDocument.content), 0),
                    KnowledgeDocument.extra_metadata,
                ).where(KnowledgeDocument.knowledge_base_id.in_(kb_uuids))
                count_query = (
                    sa_select(func.count())
                    .select_from(KnowledgeDocument)
                    .where(KnowledgeDocument.knowledge_base_id.in_(kb_uuids))
                )
                if status:
                    wanted = str(status).strip().lower()
                    query = query.where(KnowledgeDocument.status == wanted)
                    count_query = count_query.where(
                        KnowledgeDocument.status == wanted
                    )
                total = (await read_db.execute(count_query)).scalar() or 0
                result = await read_db.execute(
                    query.order_by(KnowledgeDocument.created_at.desc()).limit(
                        capped_limit
                    )
                )
                rows = result.all()
                if not rows:
                    scoped = (
                        "" if knowledge_base is None else f" in '{knowledge_base}'"
                    )
                    filtered = "" if not status else f" with status '{status}'"
                    return f"No documents{scoped}{filtered}."

                kb_names = {kb_id: name for kb_id, name in readable_kbs}
                # Fall back to a lookup for any KB name not already in scope's
                # tuple list (defensive — the scope is derived from it).
                missing = {
                    str(row[1]) for row in rows if str(row[1]) not in kb_names
                }
                if missing:
                    name_result = await read_db.execute(
                        sa_select(KnowledgeBase.id, KnowledgeBase.name).where(
                            KnowledgeBase.id.in_(_kb_uuids(list(missing)))
                        )
                    )
                    kb_names.update(
                        {str(row[0]): row[1] for row in name_result.all()}
                    )

                lines = [f"{len(rows)} of {total} document(s):"]
                for (
                    doc_id,
                    doc_kb_id,
                    filename,
                    source_type,
                    doc_status,
                    chunk_count,
                    content_length,
                    doc_meta,
                ) in rows:
                    kb_label = kb_names.get(str(doc_kb_id), "?")
                    warning = _extraction_warning(doc_meta)
                    flag = f", extraction warning: {warning}" if warning else ""
                    lines.append(
                        f"- **{filename}** (id: {doc_id}, knowledge base: "
                        f"{kb_label}, type: {source_type}, status: {doc_status}, "
                        f"chunks: {chunk_count or 0}, "
                        f"{content_length or 0} characters{flag})"
                    )
                if len(rows) < total:
                    lines.append(
                        f"({total - len(rows)} more — raise 'limit' (max "
                        f"{_DOC_LIST_MAX_LIMIT}) or filter by 'status'.)"
                    )
                lines.append(
                    "Read a document's full source with get_knowledge_document."
                )
                return "\n".join(lines)
        except Exception as e:
            logger.error(
                "list_knowledge_documents failed: %s",
                format_exception_message(e),
                exc_info=True,
            )
            return "Failed to list knowledge base documents. Please try again later."

    async def _document_body(
        doc: KnowledgeDocument,
        kb: KnowledgeBase,
        start: int,
        window: int,
        explicit_window: bool,
    ) -> list[str]:
        """The lines under a document's metadata: its source, a slice of it, or its work item."""
        lines: list[str] = []
        # ``is not None`` (not truthiness): ``content`` is nullable, so a
        # document stored with empty text is a real state distinct from
        # "no stored source", and must round-trip as empty rather than
        # being reported as missing.
        if doc.content is None:
            lines.append(
                "Source: (this document has no stored text — it cannot "
                "be read back verbatim)"
            )
            return lines

        total = len(doc.content)
        if start >= total and total:
            lines.append(
                f"Source: offset {start} is past the end of this "
                f"document ({total} characters). Use an offset below "
                f"{total}."
            )
            return lines

        # Oversized and no explicit window: hand the document to a
        # durable work item rather than injecting (or paging) it.
        if total > _DOC_CONTEXT_LIMIT_CHARS and not explicit_window:
            handle = await _offload_oversized_document(doc, kb, total)
            if handle is not None:
                lines.append(handle)
                return lines
            # No work item could be created — degrade to the windowed
            # read rather than failing, but say plainly that this is the
            # first slice of something far larger, so the agent does not
            # mistake it for the whole document.
            lines.append(
                f"WARNING: this document is {total} characters, above "
                f"the {_DOC_CONTEXT_LIMIT_CHARS}-character limit for "
                "placing a document in the context, and it could not be "
                "registered as a work item. Only the first slice is "
                "shown below. Prefer search_knowledge for the passages "
                "you actually need instead of paging the whole source."
            )

        end = min(start + window, total)
        if start == 0 and end == total:
            lines.append(f"Source (complete, {total} characters):")
        else:
            lines.append(
                f"Source (characters {start}-{end} of {total}, verbatim "
                "slice):"
            )
        lines.append(doc.content[start:end])
        if end < total:
            lines.append("")
            lines.append(
                f"[TRUNCATED — {total - end} characters remain. Call "
                f"get_knowledge_document again with offset={end} to "
                "continue.]"
            )
        return lines

    async def _get_knowledge_document(
        document_id: str | None = None,
        title: str | None = None,
        knowledge_base: str | None = None,
        offset: int = 0,
        max_chars: int | None = None,
        section: str | int | None = None,
        outline: bool = False,
        file_context: FileContext | None = None,
    ) -> str:
        if not document_id and not title:
            return (
                "Error: provide either 'document_id' (preferred — from "
                "search_knowledge or list_knowledge_documents) or 'title'."
            )
        scope = resolver.resolve_scope(knowledge_base)
        if scope is not None and (outline or section not in (None, "")):
            return await _document_sections(scope, document_id, title, section, bool(outline))
        if scope is None:
            return (
                f"Error: '{knowledge_base}' is not one of the knowledge bases this tool reads - those this agent "
                "is given -, or its name is ambiguous (use the id). "
                f"Choose one of: {resolver.options}."
            )
        try:
            start = max(0, int(offset or 0))
        except (TypeError, ValueError):
            start = 0
        try:
            window = int(max_chars) if max_chars else _DOC_WINDOW_DEFAULT_CHARS
        except (TypeError, ValueError):
            window = _DOC_WINDOW_DEFAULT_CHARS
        window = max(1, min(window, _DOC_WINDOW_MAX_CHARS))
        # Did the caller ask for a specific bounded slice, or for "the document"?
        # Only the latter is redirected to a work item when oversized: an explicit
        # window is a deliberate, bounded request and is always served.
        explicit_window = start > 0 or max_chars is not None

        try:
            from app.core.database import async_session

            async with async_session() as read_db:
                doc, kb, error = await _fetch_kb_document(
                    read_db, scope, document_id=document_id, title=title
                )
                if error or doc is None or kb is None:
                    return error or "Error: document not found."

                lines = _format_document_metadata(doc, kb)
                body = await _document_body(doc, kb, start, window, explicit_window)
                copy_source = (
                    (
                        str(doc.id),
                        doc.filename,
                        doc.content,
                        doc.content_hash or str(len(doc.content)),
                    )
                    if doc.content
                    else None
                )
            # Stored once the read session is closed: the copy is an object
            # store upload, never held on a pooled connection.
            if copy_source is not None:
                copy_line = await _document_file_copy(*copy_source, file_context)
                if copy_line:
                    lines.append(copy_line)
            return "\n".join([*lines, "", *body])
        except Exception as e:
            logger.error(
                "get_knowledge_document failed: %s",
                format_exception_message(e),
                exc_info=True,
            )
            return "Failed to read the knowledge base document. Please try again later."

    async def _document_sections(
        scope: list[str],
        document_id: str | None,
        title: str | None,
        section: str | int | None,
        outline: bool,
    ) -> str:
        """The outline of a document and/or one of its sections, verbatim.

        No file copy and no oversize redirect: an outline is small, and a
        section is the bounded read the caller asked for.
        """
        try:
            from app.core.database import async_session

            async with async_session() as read_db:
                doc, kb, error = await _fetch_kb_document(
                    read_db, scope, document_id=document_id, title=title
                )
                if error or doc is None or kb is None:
                    return error or "Error: document not found."
                lines = _format_document_metadata(doc, kb)
                source = doc.content
        except Exception as e:
            logger.error(
                "get_knowledge_document (section) failed: %s",
                format_exception_message(e),
                exc_info=True,
            )
            return "Failed to read the knowledge base document. Please try again later."

        if source is None:
            return "\n".join([*lines, "", "Source: (this document has no stored text — it has no section to read)"])
        if outline:
            lines += ["", *_format_outline(parse_outline(source))]
        if section not in (None, ""):
            try:
                heading = find_section(source, section)
            except SectionError as exc:
                return "\n".join([*lines, "", f"Error: {exc}"])
            text = source[heading.start : heading.end]
            if len(text) > _DOC_WINDOW_MAX_CHARS:
                return "\n".join([
                    *lines,
                    "",
                    f"Section #{heading.index} '{heading.text}' is {len(text)} "
                    f"characters, above the {_DOC_WINDOW_MAX_CHARS}-character "
                    "read limit. Read one of its subsections (outline: true "
                    f"lists them), or call again with offset={heading.start} "
                    f"and max_chars to page through it.",
                ])
            lines += [
                "",
                f"Section #{heading.index} '{heading.text}' (level {heading.level}, "
                f"characters {heading.start}-{heading.end} of {len(source)}, verbatim, "
                "from its heading line to the next heading of the same or a "
                "higher level):",
                text,
            ]
        return "\n".join(lines)

    executor = CallableToolExecutor()
    executor.register("list_knowledge_documents", _list_knowledge_documents)
    executor.register("get_knowledge_document", _get_knowledge_document)

    kb_scope_property = {
        "type": "string",
        "description": (
            "Restrict to one readable knowledge base (name or id; prefer the id "
            "when names collide). Defaults to all readable knowledge bases."
        ),
    }

    list_properties: dict[str, object] = {
        "status": {
            "type": "string",
            "description": (
                "Only documents with this embedding status: 'ready', 'pending', "
                "'processing' or 'error'."
            ),
        },
        "limit": {
            "type": "integer",
            "description": (
                f"How many documents to return (default "
                f"{_DOC_LIST_DEFAULT_LIMIT}, max {_DOC_LIST_MAX_LIMIT})."
            ),
            "default": _DOC_LIST_DEFAULT_LIMIT,
        },
    }
    get_properties: dict[str, object] = {
        "document_id": {
            "type": "string",
            "description": (
                "The document's UUID — as reported by search_knowledge, "
                "list_knowledge_documents or store_knowledge. Preferred over "
                "'title'."
            ),
        },
        "title": {
            "type": "string",
            "description": (
                "The document's exact title, as an alternative to "
                "'document_id'. Rejected as ambiguous if several documents "
                "share it."
            ),
        },
        "offset": {
            "type": "integer",
            "description": (
                "Character offset to start reading from — use the offset the "
                "previous call reported to page through a large document "
                "(default 0)."
            ),
            "default": 0,
        },
        "max_chars": {
            "type": "integer",
            "description": (
                f"Characters to return in this call (default "
                f"{_DOC_WINDOW_DEFAULT_CHARS}, max {_DOC_WINDOW_MAX_CHARS})."
            ),
        },
        "outline": {
            "type": "boolean",
            "description": (
                "true to return the markdown heading outline (index, level, "
                "text, character offset and length of each section) instead "
                "of the body. Read it first to address a long document by "
                "section."
            ),
        },
        "section": {
            "type": "string",
            "description": (
                "Return ONE section verbatim, from its heading line to the next "
                "heading of the same or a higher level, with its character "
                "range: the exact heading text ('Rules registry'), a heading "
                "path ('Parent > Child') or the outline index ('#3'). A "
                "heading present several times is refused with each offset."
            ),
        },
    }
    if multi:
        list_properties["knowledge_base"] = kb_scope_property
        get_properties["knowledge_base"] = kb_scope_property

    definitions = [
        ToolDefinition(
            name="list_knowledge_documents",
            description=(
                "List (READ) the source documents in the agent's knowledge "
                "bases with their ids, titles, embedding status and size. This "
                "is an exact enumeration, not a search — use it to discover "
                "what a knowledge base actually holds, then read one in full "
                "with get_knowledge_document."
            ),
            input_schema={"type": "object", "properties": list_properties},
            source="builtin:knowledge",
        ),
        ToolDefinition(
            name="get_knowledge_document",
            description=(
                "Read (READ) the COMPLETE, VERBATIM source document behind a "
                "knowledge base entry, plus its metadata and embedding status. "
                "Use this — not search_knowledge — whenever you need the whole "
                "document rather than relevant fragments: rewriting a document, "
                "diffing against an authoritative baseline, or handing an exact "
                "artifact to another agent. search_knowledge returns ranked "
                "chunks and can never reconstruct a document faithfully. The "
                "source text is stored on the document itself, so this also "
                "works while embedding is still pending (write-then-read). "
                "Large documents are returned in verbatim slices: continue with "
                "the 'offset' the previous call reports. A document too large to "
                "belong in a context at all is NOT returned — it is registered "
                "as a work item instead, and you should use search_knowledge for "
                "the passages you need (an explicit 'offset'/'max_chars' window "
                "is still served if you need exact text). For a long markdown "
                "document, 'outline': true lists its headings and 'section' "
                "reads one section; update_knowledge_document then changes that "
                "section alone (mode 'replace_section')."
            ),
            input_schema={"type": "object", "properties": get_properties},
            source="builtin:knowledge",
        ),
    ]

    del db  # reads use an independent session
    return definitions, executor


def build_kb_document_update_tool(
    db: AsyncSession,
    user_id: str,
    writable_kbs: list[tuple[str, str]],
) -> tuple[list[ToolDefinition], CallableToolExecutor] | None:
    """Build the full-source-document UPDATE tool (``update_knowledge_document``).

    *writable_kbs* is the ``(kb_id, kb_name)`` list the agent may write to — the
    same set that grants ``store_knowledge``. Returns *None* when it is empty.

    Updating replaces (or appends to) the stored source text and re-fires
    chunking + embedding through the regular document-processing queue, so the
    vector layer converges on the new source. It deliberately reuses that queue
    rather than embedding inline: the queue is the platform's durable path (it
    handles large documents, retries transient provider failures, and self-heals
    via the recovery sweeper), and the resulting ``pending`` window is observable
    through ``get_knowledge_document``.
    """
    del user_id  # access is scoped by *writable_kbs*; writes use their own session
    if not writable_kbs:
        return None

    resolver = _KBTargetResolver(writable_kbs)
    multi = resolver.multi

    async def _file_text(file_id: str, file_context: FileContext | None) -> tuple[str | None, str | None]:
        """``(text, error)`` of a conversation file given as the new content."""
        ref = resolve_file_ref(file_context, file_id)
        if ref is None or file_context is None:
            return None, (
                f"Error: no file '{file_id}' is attached to this conversation. "
                f"{list_available_files(file_context)}"
            )
        try:
            raw = await file_context.read_bytes(ref.file_id)
        except Exception as exc:
            logger.warning(
                "update_knowledge_document could not load %s (%s): %s",
                ref.file_id,
                ref.filename,
                format_exception_message(exc),
            )
            raw = None
        if raw is None:
            return None, (
                f"Error: the content of '{ref.filename}' could not be loaded "
                "from storage. The file may have been removed. Nothing was changed."
            )
        from app.services.chunking import extract_text_from_file_async

        try:
            text = await extract_text_from_file_async(raw, ref.filename)
        except Exception as exc:
            logger.warning(
                "update_knowledge_document could not extract %s (%s): %s",
                ref.file_id,
                ref.filename,
                format_exception_message(exc),
            )
            text = ""
        if not text or not text.strip():
            return None, (
                f"Error: '{ref.filename}' holds no text (a binary or empty "
                "file). Nothing was changed."
            )
        return text, None

    def _new_source(
        mode: str,
        previous: str,
        content: str,
        section: str | int | None,
        edits: object,
        confirm_shrink: bool,
    ) -> tuple[str | None, str, str | None]:
        """``(new_source, detail, error)`` for one update *mode* over *previous*."""
        if mode == "append":
            separator = "\n\n" if previous and content else ""
            return f"{previous}{separator}{content}", "", None
        if mode in _SECTION_MODES:
            try:
                if mode == "replace_section":
                    new_source, heading, (start, end) = replace_section(previous, section, content)
                    detail = (
                        f" Section #{heading.index} '{heading.text}' (was characters "
                        f"{heading.start}-{heading.end}) is now characters {start}-{end}; "
                        "every character outside it is unchanged."
                    )
                else:
                    new_source, heading, (start, end) = insert_after(previous, section, content)
                    detail = (
                        f" New section inserted after section #{heading.index} "
                        f"'{heading.text}', at characters {start}-{end}."
                    )
            except SectionError as exc:
                return None, "", f"Error: {exc} Nothing was changed."
            return new_source, detail, None
        if mode == "edit":
            from app.services.builtin_tools.media import FileEditError, apply_text_edits

            try:
                new_source, account = apply_text_edits(previous, edits)
            except FileEditError as exc:
                return None, "", f"Error: {exc}"
            return new_source, " Edits applied: " + "; ".join(account) + ".", None
        # replace
        if (
            not confirm_shrink
            and len(previous) >= _SHRINK_GUARD_MIN_CHARS
            and len(content) < len(previous) * _SHRINK_GUARD_RATIO
        ):
            return None, "", (
                f"Error: this replace would shrink the document from {len(previous)} "
                f"to {len(content)} characters (more than "
                f"{int((1 - _SHRINK_GUARD_RATIO) * 100)}% removed). Nothing was "
                "changed. To change one part, use mode 'replace_section' or "
                "'edit'; if the whole document really is meant to become this "
                "short, call again with confirm_shrink=true."
            )
        return content, "", None

    async def _update_knowledge_document(
        content: str | None = None,
        document_id: str | None = None,
        title: str | None = None,
        mode: str = "replace",
        knowledge_base: str | None = None,
        section: str | int | None = None,
        edits: list[dict[str, object]] | None = None,
        file_id: str | None = None,
        confirm_shrink: bool = False,
        file_context: FileContext | None = None,
    ) -> str:
        if not document_id and not title:
            return (
                "Error: provide either 'document_id' (preferred — from "
                "search_knowledge or list_knowledge_documents) or 'title' to "
                "identify the document to update."
            )
        normalized_mode = str(mode or "replace").strip().lower()
        if normalized_mode not in VALID_DOCUMENT_UPDATE_MODES:
            return (
                f"Error: '{mode}' is not a valid mode. Use "
                f"{', '.join(repr(m) for m in VALID_DOCUMENT_UPDATE_MODES)}."
            )
        if normalized_mode in _SECTION_MODES and section in (None, ""):
            return (
                f"Error: mode '{normalized_mode}' needs 'section' (a heading "
                "text, a heading path 'Parent > Child' or an outline index "
                "'#3'; get_knowledge_document with outline=true lists them)."
            )
        if normalized_mode == "edit":
            if not edits:
                return (
                    "Error: mode 'edit' needs 'edits', a list of {find, replace, "
                    "replace_all?} operations."
                )
            content = ""
        elif file_id:
            if content:
                return "Error: pass either 'content' or 'file_id', not both."
            if normalized_mode == "append":
                return "Error: 'file_id' is accepted with modes 'replace', 'replace_section' and 'insert_after'."
            content, file_error = await _file_text(str(file_id), file_context)
            if file_error is not None:
                return file_error
        if content is None:
            return "Error: 'content' is required (or 'file_id' for a text file of the conversation)."
        if _FILE_MARKER_ONLY_RE.match(content):
            return (
                "Error: 'content' is only a file marker, not the document text. "
                "Pass the file's id as 'file_id' instead (its text becomes the "
                "new source). Nothing was changed."
            )
        # PostgreSQL text columns cannot store NUL (0x00). Rejecting it here
        # turns an unhandled asyncpg error mid-write into an actionable message,
        # matching the text-document API endpoint's guard.
        if "\x00" in content:
            return (
                "Error: the content contains NUL bytes and cannot be stored as "
                "text. Provide decoded text, not raw binary."
            )
        scope = resolver.resolve_scope(knowledge_base)
        if scope is None:
            return (
                f"Error: '{knowledge_base}' is not a writable knowledge base "
                "(or its name is ambiguous - use the id). "
                f"Choose one of: {resolver.options}."
            )

        # What the write was doing when an unexpected exception hit, so the
        # answer names the stage and whether the new source is saved.
        stage = "opening a database session"
        new_length: int | None = None
        try:
            import hashlib

            from app.core.database import async_session
            from app.services.kb_helpers import (
                apply_document_write_timeout,
                bump_kb_revision,
                enqueue_document_processing,
                log_kb_activity,
                reset_changed_document,
            )

            async with async_session() as write_db:
                stage = "looking the document up"
                doc, kb, error = await _fetch_kb_document(
                    write_db, scope, document_id=document_id, title=title
                )
                if error or doc is None or kb is None:
                    return error or "Error: document not found."

                # The shared content-write gate, the one the REST document
                # routes run: package-installed and built-in KBs are re-created
                # from their source (an edit would be silently reverted), and a
                # read-only KB is ground truth - stripped from the writable
                # scope already, so reaching one means it was locked mid-run.
                stage = "checking that the knowledge base is writable"
                if lock_err := await _agent_write_lock_error(write_db, kb):
                    return lock_err

                stage = f"computing the new source (mode '{normalized_mode}')"
                previous = doc.content or ""
                new_source, detail, mode_error = _new_source(
                    normalized_mode, previous, content, section, edits, bool(confirm_shrink)
                )
                if mode_error is not None or new_source is None:
                    return mode_error or "Error: nothing to write."
                if "\x00" in new_source:
                    return (
                        "Error: the result contains NUL bytes and cannot be "
                        "stored as text. Nothing was changed."
                    )
                new_content = new_source
                new_length = len(new_content)

                if new_content == previous and doc.status == "ready":
                    # Identical content already fully indexed: re-embedding would
                    # burn provider calls and open a needless unsearchable
                    # window for no change. A non-ready document still gets the
                    # re-embed below, since that is how the agent recovers one.
                    return (
                        f"No change: '{doc.filename}' (id: {doc.id}) in "
                        f"'{kb.name}' already holds exactly this content and is "
                        "fully indexed. Nothing was re-embedded."
                    )

                # Old vectors go now: until the re-embed lands, search returns
                # nothing for this document rather than its previous content;
                # get_knowledge_document still reads the new text.
                stage = "writing the new source"
                # The vector delete and the new source of a large document run
                # past the 30 s pool default: the write gets the KB write bound.
                await apply_document_write_timeout(write_db)
                await reset_changed_document(write_db, doc, kb=kb)
                doc.content = new_content
                doc.content_hash = hashlib.sha256(
                    new_content.encode()
                ).hexdigest()
                await bump_kb_revision(write_db, kb.id)

                await log_kb_activity(
                    write_db,
                    kb.id,
                    "document_updated",
                    details=(
                        f"Agent updated document '{doc.filename}' "
                        f"({normalized_mode}, {len(new_content)} characters)"
                    ),
                    source_type=doc.source_type,
                    document_count=1,
                )

                doc_id_str = str(doc.id)
                doc_filename = doc.filename
                kb_name = kb.name
                previous_length = len(previous)
                stage = "committing the new source"
                await write_db.commit()
        except Exception as e:
            logger.error(
                "update_knowledge_document failed while %s: %s",
                stage,
                format_exception_message(e),
                exc_info=True,
            )
            return _document_write_failure(e, stage, new_length)

        try:
            enqueued = await enqueue_document_processing(doc_id_str)
        except Exception as e:
            # The new source is committed: answering "failed" here would make
            # the agent redo a write that already landed.
            logger.error(
                "update_knowledge_document could not queue the re-embed of %s: %s",
                doc_id_str,
                format_exception_message(e),
                exc_info=True,
            )
            enqueued = False
        verb = {
            "append": "Appended to",
            "replace_section": "Replaced one section of",
            "insert_after": "Inserted a section into",
            "edit": "Edited",
        }.get(normalized_mode, "Replaced")
        summary = (
            f"{verb} '{doc_filename}' (id: {doc_id_str}, kept) in knowledge base "
            f"'{kb_name}' — the source is now {new_length} characters "
            f"({(new_length or 0) - previous_length:+d}).{detail}"
        )
        if enqueued:
            return (
                f"{summary} Re-chunking and re-embedding is queued: the "
                "document is 'pending' and NOT searchable via "
                "search_knowledge until it returns to 'ready'. Check "
                "get_knowledge_document for its status; the full source is "
                "readable there immediately."
            )
        return (
            f"{summary} However, re-embedding could NOT be queued (no "
            "embedding provider configured, or the queue is unavailable), "
            "so the document is 'pending' and NOT searchable via "
            "search_knowledge — its previous vectors were dropped so stale "
            "content is never returned as current. The full source is still "
            "readable via get_knowledge_document, and the platform's "
            "recovery sweep re-embeds it once embedding works again. Tell "
            "the user if they are relying on search."
        )

    executor = CallableToolExecutor()
    executor.register("update_knowledge_document", _update_knowledge_document)

    properties: dict[str, object] = {
        "content": {
            "type": "string",
            "description": (
                "The new text (markdown supported): the whole source for "
                "'replace', the text added for 'append', the whole section "
                "(its heading line included) for 'replace_section' and "
                "'insert_after'. Never a [[FILE:...]] marker: pass 'file_id'."
            ),
        },
        "document_id": {
            "type": "string",
            "description": (
                "The document's UUID — as reported by search_knowledge, "
                "list_knowledge_documents or store_knowledge. Preferred over "
                "'title'."
            ),
        },
        "title": {
            "type": "string",
            "description": (
                "The document's exact title, as an alternative to "
                "'document_id'. Rejected as ambiguous if several documents "
                "share it."
            ),
        },
        "mode": {
            "type": "string",
            "enum": list(VALID_DOCUMENT_UPDATE_MODES),
            "description": (
                "'replace' (default) overwrites the whole source (refused when "
                "it would remove more than half of a long document, unless "
                "confirm_shrink); 'append' adds 'content' at the end; "
                "'replace_section' replaces the section 'section' names and "
                "keeps every other character; 'insert_after' adds 'content' as "
                "a new section after that section (and its subsections); "
                "'edit' applies 'edits' (exact find/replace) to the source."
            ),
            "default": "replace",
        },
        "section": {
            "type": "string",
            "description": (
                "For 'replace_section' and 'insert_after': the section, by "
                "exact heading text, heading path ('Parent > Child') or outline "
                "index ('#3', from get_knowledge_document outline=true). A "
                "heading present several times is refused with each offset."
            ),
        },
        "edits": {
            "type": "array",
            "description": (
                "For 'edit': ordered exact find/replace operations on the "
                "stored source. Each find must exist and be unique (or set "
                "replace_all); nothing is written unless every edit applies."
            ),
            "items": {
                "type": "object",
                "properties": {
                    "find": {"type": "string"},
                    "replace": {"type": "string"},
                    "replace_all": {"type": "boolean"},
                },
                "required": ["find", "replace"],
            },
        },
        "file_id": {
            "type": "string",
            "description": (
                "A text file of this conversation whose text is the new content "
                "(instead of 'content'), for 'replace', 'replace_section' or "
                "'insert_after'. The document keeps its id."
            ),
        },
        "confirm_shrink": {
            "type": "boolean",
            "description": (
                "true to allow a 'replace' that removes more than half of a "
                "long document. Prefer 'replace_section' or 'edit'."
            ),
        },
    }
    if multi:
        target_list = "; ".join(f"'{name}' (id: {kid})" for kid, name in writable_kbs)
        properties["knowledge_base"] = {
            "type": "string",
            "description": (
                "Restrict the lookup to one writable knowledge base (name or "
                f"id). Writable: {target_list}. Defaults to all of them."
            ),
        }

    definition = ToolDefinition(
        name="update_knowledge_document",
        description=(
            "Update (WRITE) the source text of an EXISTING document in one of "
            "the agent's writable knowledge bases, then automatically re-chunk "
            "and re-embed it so search stays consistent with the source. Read "
            "it first with get_knowledge_document. A failure names its cause "
            "and whether a retry helps. For "
            "a long document change only what is wrong: mode 'replace_section' "
            "(one heading section), 'insert_after' (a new section) or 'edit' "
            "(exact find/replace); the document keeps its id. Use "
            "store_knowledge instead to create a new document. Re-embedding is "
            "asynchronous: the document reports 'pending' and is not searchable "
            "until it is 'ready' again, though its new source is readable "
            "immediately via get_knowledge_document."
        ),
        input_schema={
            "type": "object",
            "properties": properties,
        },
        source="builtin:knowledge",
    )

    del db  # writes use an independent session
    return [definition], executor


def format_knowledge_base_prompt(
    read_kbs: list[tuple[str, str]],
    write_kbs: list[tuple[str, str]],
) -> str:
    """Return a system-prompt block describing KB read/write capabilities.

    *read_kbs* / *write_kbs* are lists of ``(kb_id, kb_name)`` tuples. The block
    tells the agent which knowledge bases it can search and which it can write
    to, so it uses ``search_knowledge`` / ``store_knowledge`` appropriately.

    Returns an empty string when there are no readable or writable KBs.
    """
    if not read_kbs and not write_kbs:
        return ""
    write_ids = {kb_id for kb_id, _ in write_kbs}
    lines = ["\n── Knowledge Bases ──"]
    if read_kbs:
        read_names = ", ".join(name for _, name in read_kbs)
        lines.append(
            f"Readable (`search_knowledge` for fragments, "
            f"`get_knowledge_document` for a whole document): {read_names}."
        )
    if write_kbs:
        write_names = ", ".join(name for _, name in write_kbs)
        lines.append(
            f"Writable (`store_knowledge` to add, `update_knowledge_document` "
            f"to revise + re-embed): {write_names}. "
            "Only persist durable, reusable knowledge here."
        )
        read_only = [name for kb_id, name in read_kbs if kb_id not in write_ids]
        if read_only:
            lines.append("All other knowledge bases are read-only.")
    elif read_kbs:
        lines.append("Read-only access — you cannot modify any knowledge base.")
    return "\n".join(lines)
