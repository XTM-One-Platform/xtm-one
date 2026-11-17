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

"""Workset reading — search, bounded verbatim reads, and the digest.

The read path, and the half that decides whether the whole primitive is worth
anything: a corpus you cannot query without loading it is just a slow context
window.

Three reads, deliberately different in kind:

* **Search** finds passages. Hybrid (vector + full text) over the workset's
  own corpus AND the collections its referenced documents live in — an ingest
  by reference does not copy, so its vectors are not in the workset's KB. The
  query is restricted to this workset's own documents, so `top_k` counts rows
  it can actually return rather than candidates it may have to throw away.
* **Read** returns verbatim text, always bounded, and lets the agent WIDEN a
  hit by whole chunks. A search hit is a fragment; the sentence that explains
  it is often the one after.
* **Digest** is the reduce input: counts, coverage, and the top summaries
  under an explicit character budget. It never returns raw content.

Every one of the three states **coverage** — how much of the corpus is
actually behind the answer. An agent that mistakes a truncated corpus for a
complete one writes a confident, wrong synthesis, and that failure is silent
unless the read path refuses to be silent about it.

**Trust**: everything returned here is text this platform did not author. It
is data — quoted, counted, attributed — never instruction.
"""

from __future__ import annotations

import logging
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import func, select

from app.core.utils import format_exception_message
from app.models.workset import (
    WORKSET_ITEM_EVICTED_STATUS,
    WorksetItem,
)
from app.services.workset import (
    WORKSET_CHUNK_OVERLAP,
    WORKSET_CHUNK_SIZE,
    budget_resets_at,
    budget_spent,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.user import User
    from app.models.workset import Workset

logger = logging.getLogger(__name__)

# A single read is a slice, not a document. Above this the caller is paging,
# which is what `offset` is for.
READ_WINDOW_DEFAULT_CHARS = 4000
READ_WINDOW_MAX_CHARS = 20000
# How far a hit may be widened in one call, in whole chunks either side.
MAX_EXPAND_CHUNKS = 5
# The digest is the input to a synthesis, so it must fit beside one.
DIGEST_DEFAULT_MAX_CHARS = 12000
DIGEST_MAX_CHARS = 40000
DIGEST_DEFAULT_TOP_N = 20


@dataclass
class Coverage:
    """What the corpus holds, and what the answer is actually based on.

    Returned with every read. ``complete`` is deliberately conservative: a
    workset that is not sealed can still grow, so nothing it reports is final.
    """

    total: int = 0
    ready: int = 0
    pending: int = 0
    failed: int = 0
    evicted: int = 0
    discarded: int = 0
    sealed: bool = False
    declared_total: int | None = None
    # The automatic pass hit its per-workset spend ceiling. Reported
    # because it changes what an unsummarised item MEANS: not "the model could
    # not read it" but "this corpus is not allowed to pay for any more", which
    # is a different thing to do something about.
    auto_budget_exhausted: bool = False
    # When an implicit workset's rolling budget comes back (None: a lifetime
    # ceiling, or not spent).
    auto_budget_resets_at: datetime | None = None

    @property
    def complete(self) -> bool:
        """Every item that was going to be processed has been, successfully.

        ``failed`` counts against this. A sealed workset holding five items
        that will never be summarised is NOT a complete corpus, and this
        boolean is what code branches on — ``describe()`` mentioning the
        failures is not enough, because nothing reads prose to decide. The
        module's whole thesis is that a truncated corpus must never look
        complete, and that has to hold for the machine-readable answer first.

        For "is there still work in flight?", which is a different question,
        use :attr:`settled`.
        """
        return (
            self.sealed
            and self.pending == 0
            and self.failed == 0
            and (self.declared_total is None or self.declared_total <= self.total)
        )

    @property
    def settled(self) -> bool:
        """Nothing can still progress — done, whether or not it all worked.

        This is what a waiter watches: an item whose retries are exhausted is
        terminal, and waiting on it forever is the failure mode that guard
        exists to prevent. Distinct from :attr:`complete`, which asks whether
        the corpus is whole.
        """
        return self.pending == 0

    def describe(self) -> str:
        """One line an agent can quote, and must not be able to misread."""
        parts = [f"{self.total} item(s)"]
        if self.ready:
            parts.append(f"{self.ready} summarised")
        if self.pending:
            parts.append(f"{self.pending} still being processed")
        if self.failed:
            parts.append(
                f"{self.failed} failed permanently (their content is stored "
                "but was never summarised — this corpus is NOT complete)"
            )
        if self.evicted:
            parts.append(
                f"{self.evicted} evicted (metadata and summary kept, content "
                "reclaimed — those are NOT searchable)"
            )
        if self.discarded:
            parts.append(f"{self.discarded} discarded")
        line = "Coverage: " + ", ".join(parts) + "."
        if self.auto_budget_exhausted and self.auto_budget_resets_at is None:
            line += (
                " This workset has spent its automatic summarisation budget, "
                "so nothing else will be summarised for it automatically — "
                "any item still without a summary needs one written with "
                "workset_set_summary, or a larger budget."
            )
        if self.auto_budget_exhausted and self.auto_budget_resets_at is not None:
            line += (
                " This workset has spent its automatic summarisation budget "
                "for now: it is rolling and replenishes at "
                f"{self.auto_budget_resets_at.isoformat(timespec='minutes')}. "
                "Until then a new item is refused unless it comes with its "
                "own summary and priority."
            )
        if not self.sealed:
            line += (
                " This workset is NOT sealed: more items may still arrive, so "
                "this is what has been collected so far, not the whole window."
            )
        if (
            self.declared_total is not None
            and self.declared_total != self.total
        ):
            line += (
                f" The caller announced {self.declared_total} item(s) and "
                f"{self.total} are present — that gap is unexplained."
            )
        return line


@dataclass
class SearchHit:
    """One passage, with everything needed to cite or widen it."""

    item_id: uuid.UUID
    title: str
    snippet: str
    score: float
    chunk_index: int | None = None
    occurred_at: str | None = None
    source_url: str | None = None
    priority_score: int | None = None
    summary: str | None = None


@dataclass
class SearchResult:
    hits: list[SearchHit] = field(default_factory=list)
    coverage: Coverage = field(default_factory=Coverage)
    error: str | None = None
    # Why an empty (or short) hit list is not the whole story. An empty result
    # that says nothing reads as "nothing in this corpus matches", and acting
    # on that when the passages were merely filtered out is the silent failure
    # this module exists to refuse.
    note: str | None = None


async def coverage_of(db: AsyncSession, workset: Workset) -> Coverage:
    """Count the corpus by status in one query."""
    rows = (
        await db.execute(
            select(WorksetItem.status, func.count())
            .where(WorksetItem.workset_id == workset.id)
            .group_by(WorksetItem.status)
        )
    ).all()
    by_status = {status: count for status, count in rows}
    exhausted = workset.processing == "auto" and budget_spent(workset)
    return Coverage(
        total=sum(by_status.values()),
        ready=by_status.get("ready", 0) + by_status.get("consumed", 0),
        pending=by_status.get("pending", 0),
        failed=by_status.get("failed", 0),
        evicted=by_status.get(WORKSET_ITEM_EVICTED_STATUS, 0),
        discarded=by_status.get("discarded", 0),
        sealed=workset.sealed_at is not None,
        declared_total=workset.declared_total,
        auto_budget_exhausted=exhausted,
        auto_budget_resets_at=budget_resets_at(workset) if exhausted else None,
    )


@dataclass
class SearchScope:
    """The rows a search may return, named before the query rather than after.

    Two lists, and the second is the one that matters. The collections
    (*kb_ids*) are where the content lives — the workset's own corpus plus,
    for an ingest by reference, the collection the borrowed document stayed
    in. Scoping on those ALONE is not enough: a workset that borrows two
    documents out of a team wiki would otherwise search the whole wiki.

    So *document_ids* names the workset's own documents, and the query is
    restricted to them. That restriction used to run in Python after the fact,
    and it made ``top_k`` a count of rows the workset might not be allowed to
    return: on a large borrowed collection the entire fused pool could belong
    to documents the workset does not own, every one of them dropped, and
    ``search`` returned an empty hit list with no error — indistinguishable
    from "nothing matches" while the matching passages sat in the corpus
    unreturned. Filtering in the query makes the count exact and
    removes the over-fetch guess entirely.
    """

    kb_ids: list[str] = field(default_factory=list)
    document_ids: list[str] = field(default_factory=list)
    # Borrowed documents dropped because the caller can no longer open their
    # collection. Counted, never silent: "we have 40 items and searched 38" is
    # a different answer from "we searched everything".
    withheld: int = 0


async def _search_scope(
    db: AsyncSession, workset: Workset, user: User | None
) -> SearchScope:
    """The collections a search looks in, and the documents it may return.

    One pass over the workset's non-evicted items: they carry both halves, and
    a workset is sized in the hundreds so the id list is bounded and known up
    front. The collection of a BORROWED document is joined in; an owned one
    lives in the workset's own corpus by construction.

    Borrowed documents are then re-authorized against *user* and dropped from
    the scope when they no longer resolve — see :func:`read_item` for why the
    check belongs on every read rather than only at ingest.
    """
    from app.models.knowledge_base import KnowledgeDocument
    from app.services.workset import readable_document_ids

    rows = (
        await db.execute(
            select(
                WorksetItem.knowledge_document_id,
                KnowledgeDocument.knowledge_base_id,
                WorksetItem.document_is_foreign,
            )
            .join(
                KnowledgeDocument,
                KnowledgeDocument.id == WorksetItem.knowledge_document_id,
            )
            .where(
                WorksetItem.workset_id == workset.id,
                WorksetItem.knowledge_document_id.is_not(None),
                WorksetItem.status != WORKSET_ITEM_EVICTED_STATUS,
            )
        )
    ).all()
    if not rows:
        return SearchScope()

    borrowed = [
        document_id for document_id, _kb, foreign in rows if foreign
    ]
    still_readable: set[uuid.UUID] = set(borrowed)
    if borrowed and user is not None:
        still_readable = await readable_document_ids(db, user, borrowed)

    kb_ids: set[str] = set()
    if workset.knowledge_base_id is not None:
        kb_ids.add(str(workset.knowledge_base_id))
    document_ids: set[str] = set()
    withheld = 0
    for document_id, kb_id, foreign in rows:
        if foreign and document_id not in still_readable:
            # Access to the source collection was revoked after ingest. The
            # item keeps its summary and its place in the coverage; what it
            # loses is its passages.
            withheld += 1
            continue
        document_ids.add(str(document_id))
        if kb_id:
            kb_ids.add(str(kb_id))
    return SearchScope(
        kb_ids=sorted(kb_ids),
        document_ids=sorted(document_ids),
        withheld=withheld,
    )


async def _items_for_documents(
    db: AsyncSession, workset: Workset, document_ids: list[str]
) -> dict[str, WorksetItem]:
    """This workset's items behind the documents a search actually returned.

    Bounded by the result count, not by the corpus size. It resolves a hit to
    the row that can be cited and widened; ownership is settled earlier, by the
    query itself (see :class:`SearchScope`), so a document missing here is a
    race — an eviction between scoping and searching — not a foreign passage.
    """
    if not document_ids:
        return {}
    valid: list[uuid.UUID] = []
    for raw in document_ids:
        try:
            valid.append(uuid.UUID(raw))
        except (ValueError, AttributeError, TypeError):
            continue
    if not valid:
        return {}
    items = (
        (
            await db.execute(
                select(WorksetItem).where(
                    WorksetItem.workset_id == workset.id,
                    WorksetItem.knowledge_document_id.in_(valid),
                    WorksetItem.status != WORKSET_ITEM_EVICTED_STATUS,
                )
            )
        )
        .scalars()
        .all()
    )
    return {str(i.knowledge_document_id): i for i in items}


async def search(
    db: AsyncSession,
    workset: Workset,
    *,
    user: User | None,
    query: str,
    top_k: int = 10,
    min_priority: int | None = None,
) -> SearchResult:
    """Find passages in the corpus. Hybrid, scoped, and coverage-aware.

    *user* is the acting user and is required rather than optional: it decides
    whether a BORROWED document's passages may still be returned. Passing
    ``None`` says there is no acting user (an internal caller reading the
    workset's own corpus) and skips the re-check — a decision, taken at the
    call site, rather than a default that quietly reads someone else's
    collection. See :func:`read_item`.
    """
    from app.services.hybrid_search import hybrid_search_embeddings

    coverage = await coverage_of(db, workset)
    query = (query or "").strip()
    if not query:
        return SearchResult(coverage=coverage, error="No query was given.")

    scope = await _search_scope(db, workset, user)
    # Both halves are required. An empty document list must NOT fall through:
    # `source_ids=[]` reads as "no restriction" downstream, which would search
    # the whole collection — the exact leak the restriction exists to close.
    if not scope.kb_ids or not scope.document_ids:
        if scope.withheld:
            return SearchResult(
                coverage=coverage,
                error=(
                    f"Nothing in this workset is searchable by you: its "
                    f"{scope.withheld} borrowed item(s) live in a collection "
                    "you can no longer open. Their summaries are still "
                    "readable. " + coverage.describe()
                ),
            )
        return SearchResult(
            coverage=coverage,
            error=(
                "Nothing in this workset is searchable yet — no item has "
                "embedded content. " + coverage.describe()
            ),
        )

    try:
        from app.llm.router import get_embedding_provider

        provider, model, _ = await get_embedding_provider(db)
        query_embedding = await provider.embed(query, model=model)
    except Exception as exc:
        logger.warning(
            "Workset search could not embed its query: %s",
            format_exception_message(exc),
        )
        return SearchResult(
            coverage=coverage,
            error=f"The embedding provider could not be reached: {format_exception_message(exc)}",
        )

    # A modest over-fetch, and no longer a defence against foreign rows: the
    # query already returns only this workset's documents. It buys headroom for
    # the `min_priority` filter below, which is the one predicate that still
    # cannot be pushed down — it lives on `workset_items`, not on `embeddings`.
    fetch = top_k if min_priority is None else max(top_k * 4, 20)
    try:
        results = await hybrid_search_embeddings(
            db,
            query_text=query,
            query_embedding=query_embedding,
            knowledge_base_ids=scope.kb_ids,
            # The ownership filter, IN the query. Post-filtering made `top_k` a
            # count of candidate rows rather than of returnable ones.
            source_ids=scope.document_ids,
            top_k=fetch,
            expected_embedding_dimensions=len(query_embedding),
            expected_embedding_model=model,
        )
    except Exception as exc:
        logger.warning(
            "Workset hybrid search failed: %s", format_exception_message(exc)
        )
        return SearchResult(
            coverage=coverage,
            error=f"The search failed: {format_exception_message(exc)}",
        )

    by_document = await _items_for_documents(
        db, workset, [str(r.source_id or "") for r in results]
    )
    # One content read per DOCUMENT that produced a hit, not one per hit: ten
    # passages from the same report would otherwise be ten reads of it.
    chunk_cache: dict[uuid.UUID, DocumentChunks] = {}

    hits: list[SearchHit] = []
    below_priority = 0
    for result in results:
        item = by_document.get(str(result.source_id or ""))
        if item is None:
            # The query is already scoped to this workset's documents, so this
            # is the narrow race: an item evicted, or its document deleted,
            # between the scope query and the search. Still dropped — a hit
            # must map to a row the caller can cite.
            continue
        if min_priority is not None and (item.priority_score or 0) < min_priority:
            # The one filter that cannot be pushed into the query: it lives on
            # `workset_items`. Counted so an empty result can say WHY.
            below_priority += 1
            continue
        hits.append(
            SearchHit(
                item_id=item.id,
                title=item.title,
                snippet=result.content,
                score=round(result.rrf_score, 6),
                chunk_index=await _locate_chunk(
                    db, item, result.content, cache=chunk_cache
                ),
                occurred_at=(
                    item.occurred_at.isoformat() if item.occurred_at else None
                ),
                source_url=item.source_url,
                priority_score=item.priority_score,
                summary=item.summary,
            )
        )
        if len(hits) >= top_k:
            break

    note = None
    if scope.withheld:
        note = (
            f"{scope.withheld} borrowed item(s) were NOT searched: you can no "
            "longer open the collection they live in, so their content is out "
            "of reach even though their summaries remain in this workset."
        )
    if not hits and below_priority:
        below = (
            f"{below_priority} passage(s) DID match this query, and every one "
            f"was dropped because its item scores below min_priority="
            f"{min_priority}. This is not an empty corpus: search again "
            "without the filter, or lower it."
        )
        note = f"{note} {below}" if note else below
    return SearchResult(hits=hits, coverage=coverage, note=note)


def _chunks_of(content: str, *, chunk_size: int, chunk_overlap: int) -> list[str]:
    """Re-chunk *content* with the parameters it was EMBEDDED under.

    ``embeddings`` stores a chunk's TEXT but no ordinal, so "chunk 7" and "the
    two chunks after this hit" cannot be answered from that table. They are
    recovered by re-chunking the stored document — ``chunk_text`` is pure, so
    given the right parameters the list is the one that was embedded.

    "The right parameters" is the whole subtlety, and getting it wrong is
    worse than not answering: a workset-OWNED document was chunked at the
    corpus's own 384/96, but a BORROWED one was chunked under its own
    collection's settings (512/128 by default), because an ingest by reference
    is deliberately not re-embedded. Chunking a borrowed document at 384/96
    produces a list that matches nothing — see ``chunks_for_document``.

    The alternative was a `chunk_index` column on `embeddings`, i.e. a
    migration on the largest table in the platform for an ordinal that is
    derivable. If chunking ever becomes non-deterministic this has to become
    that column instead, and the callers degrade to a character window rather
    than lying about an index.
    """
    from app.services.chunking import chunk_text

    return chunk_text(
        content, chunk_size=chunk_size, chunk_overlap=chunk_overlap
    )


@dataclass
class DocumentChunks:
    """A document's chunk list, and whether it is the embedded one."""

    chunks: list[str] = field(default_factory=list)
    # False when the parameters could not be established, or when they no
    # longer match what was embedded (see `_chunks_match_embeddings`).
    # The caller must then
    # NOT report an ordinal: `chunk_count` would describe a chunking that was
    # never applied, and the text would be a window matching no embedded chunk.
    exact: bool = True


# What the embed path keeps of a chunk list: `document_processor` and the
# `store_knowledge` tool both drop chunks shorter than this once stripped.
_MIN_EMBEDDED_CHUNK_CHARS = 10


def _chunks_match_embeddings(chunks: list[str], embedded: list[str]) -> bool | None:
    """Whether *chunks* is the chunking the stored embeddings came from.

    The parameters are read from the collection as it is NOW, and a
    collection's parameters can change under documents that are not
    re-embedded: the built-in knowledge base sync rewrites them on every boot
    and re-embeds only the documents whose content changed. The recomputed
    list then describes a chunking no embedding agrees with.

    So the recomputed embeddable chunks are compared with the chunk texts
    actually stored for the document, as multisets (`embeddings` keeps no
    ordinal). A count alone would not do: new parameters can yield the same
    number of chunks with different boundaries. A partly embedded document
    does not match either, and no ordinal is trusted for it. None when
    nothing is stored (never embedded, or a re-embed in flight): not judged.
    """
    if not embedded:
        return None
    embeddable = [
        chunk for chunk in chunks if len(chunk.strip()) >= _MIN_EMBEDDED_CHUNK_CHARS
    ]
    return Counter(embeddable) == Counter(embedded)


async def _embedded_chunk_texts(db: AsyncSession, document_id: uuid.UUID) -> list[str]:
    """The chunk texts stored for *document_id*, in no particular order."""
    from app.models.embedding import Embedding

    return list(
        (
            await db.execute(
                select(Embedding.content).where(
                    Embedding.source_type == "document",
                    Embedding.source_id == str(document_id),
                )
            )
        )
        .scalars()
        .all()
    )


async def chunks_for_document(
    db: AsyncSession,
    document_id: uuid.UUID,
    *,
    cache: dict[uuid.UUID, DocumentChunks] | None = None,
) -> DocumentChunks | None:
    """The chunks of *document_id* as they were embedded, or None if unreadable.

    The chunking parameters come from the collection the document ACTUALLY
    lives in, not from the workset's own corpus — which is the same thing for
    an owned document and a different thing for a borrowed one.
    """
    from app.models.knowledge_base import KnowledgeBase, KnowledgeDocument

    if cache is not None and document_id in cache:
        return cache[document_id]

    row = (
        await db.execute(
            select(
                KnowledgeDocument.content,
                KnowledgeDocument.status,
                KnowledgeBase.chunk_size,
                KnowledgeBase.chunk_overlap,
                KnowledgeDocument.filename,
                KnowledgeBase.html_ingestion,
                KnowledgeBase.describe_figures,
                KnowledgeDocument.figure_reading,
            )
            .join(
                KnowledgeBase,
                KnowledgeBase.id == KnowledgeDocument.knowledge_base_id,
            )
            .where(KnowledgeDocument.id == document_id)
        )
    ).first()
    if row is None:
        return None
    (
        source,
        status,
        chunk_size,
        chunk_overlap,
        filename,
        html_ingestion,
        describe_figures,
        figure_reading,
    ) = row
    if not source:
        return None
    # The text the document was embedded as, not always its stored source: a
    # KB that renders HTML chunked the rendered page.
    from app.services.kb_embedding_text import (
        HTML_INGESTION_RENDERED,
        HTML_INGESTION_SOURCE,
        embedding_text_for_async,
        is_html_document,
        normalize_html_ingestion,
    )
    from app.services.kb_figures import reading_applies

    figures = figure_reading if describe_figures else None
    content = await embedding_text_for_async(source, filename, html_ingestion, figures=figures)

    # Parameters the chunker can actually have been run with. An overlap of 0
    # is a legitimate "no overlap" setting, not a missing one - only a
    # non-positive size or an overlap outside [0, size) cannot be trusted.
    known = (
        chunk_size is not None
        and chunk_size > 0
        and chunk_overlap is not None
        and 0 <= chunk_overlap < chunk_size
    )
    if known:
        chunks = _chunks_of(
            content, chunk_size=chunk_size, chunk_overlap=chunk_overlap
        )
    else:
        # Chunk it anyway - a character window is still useful - but say it is
        # not exact.
        chunks = _chunks_of(
            content,
            chunk_size=WORKSET_CHUNK_SIZE,
            chunk_overlap=WORKSET_CHUNK_OVERLAP,
        )
    exact = known
    if exact and status == "ready":
        embedded = await _embedded_chunk_texts(db, document_id)
        matches = _chunks_match_embeddings(chunks, embedded)
        if (
            matches is False
            and is_html_document(source, filename)
            and set(embedded).isdisjoint(chunks)
        ):
            # The HTML setting changed since this document was embedded, and
            # changing it re-embeds nothing: its stored chunks may still be
            # the OTHER representation. Use that one when it matches exactly.
            # Stored chunks sharing one with the current reading were cut from
            # it (a document only partly embedded, say) and the other reading
            # cannot match them, so it is not derived - in a ``source`` KB
            # that would mean rendering the page for nothing.
            other_mode = (
                HTML_INGESTION_SOURCE
                if normalize_html_ingestion(html_ingestion) == HTML_INGESTION_RENDERED
                else HTML_INGESTION_RENDERED
            )
            other_chunks = _chunks_of(
                await embedding_text_for_async(source, filename, other_mode),
                chunk_size=chunk_size,
                chunk_overlap=chunk_overlap,
            )
            if _chunks_match_embeddings(other_chunks, embedded) is True:
                chunks, matches = other_chunks, True
        if matches is False and reading_applies(figure_reading, source):
            # The figure setting changed since this document was embedded:
            # its stored chunks may be the reading with (or without) the
            # descriptions. Use the other one when it matches exactly.
            other_chunks = _chunks_of(
                await embedding_text_for_async(
                    source, filename, html_ingestion, figures=None if figures is not None else figure_reading
                ),
                chunk_size=chunk_size,
                chunk_overlap=chunk_overlap,
            )
            if _chunks_match_embeddings(other_chunks, embedded) is True:
                chunks, matches = other_chunks, True
        exact = matches is not False
    result = DocumentChunks(chunks=chunks, exact=exact)
    if cache is not None:
        cache[document_id] = result
    return result


async def _locate_chunk(
    db: AsyncSession,
    item: WorksetItem,
    snippet: str,
    *,
    cache: dict[uuid.UUID, DocumentChunks] | None = None,
) -> int | None:
    """Which chunk a search hit is, so the agent can widen it. None if unknown.

    *cache* holds one chunk list per document across a result set — several
    passages from the same report are the common case, and re-reading and
    re-chunking it per hit would be the expensive way to get the same answer.
    """
    if item.knowledge_document_id is None or not snippet:
        return None
    resolved = await chunks_for_document(
        db, item.knowledge_document_id, cache=cache
    )
    if resolved is None or not resolved.exact:
        return None
    for index, chunk in enumerate(resolved.chunks):
        if chunk == snippet:
            return index
    return None


@dataclass
class ReadResult:
    text: str = ""
    item_id: uuid.UUID | None = None
    title: str = ""
    chunk_index: int | None = None
    chunk_count: int = 0
    offset: int = 0
    end: int = 0
    total_chars: int = 0
    truncated: bool = False
    note: str | None = None
    error: str | None = None


async def read_item(
    db: AsyncSession,
    workset: Workset,
    *,
    user: User | None,
    item_id: uuid.UUID,
    chunk_index: int | None = None,
    expand_before: int = 0,
    expand_after: int = 0,
    offset: int = 0,
    max_chars: int | None = None,
) -> ReadResult:
    """Read part of one item, verbatim and bounded.

    Two addressing modes, and the agent picks: by *chunk_index* (what a search
    hit gives it) widened by whole chunks either side, or by character
    *offset* for straight paging. Never the whole document by accident — the
    window is always explicit, which is the same contract
    ``get_knowledge_document`` settled on.

    **Authorization, and where it happens**. A workset's OWN content is
    authorized by the workset: the corpus is its storage, and its row is the
    permission. A BORROWED document is different — it lives in someone else's
    collection, and the check that let it be referenced was taken at ingest. If
    that collection is later unshared, or the reader leaves the group that owns
    it, an ingest-time snapshot would leave the workset serving content its
    reader can no longer open anywhere else. So a borrowed item is re-checked
    on every read, against *user*.

    The failure is a demotion, not an error: the item keeps its row, its
    summary and its place in the coverage, and loses only its source text —
    the same shape as an eviction, which this module already handles and
    already narrates. "The analysis outlives the content" is a property worth
    keeping; "the content outlives the permission" is not.

    *user* is required, and ``None`` explicitly means "no acting user" — an
    internal caller over the workset's own corpus. Making it a parameter with
    no default is the point: an omitted user would be an unchecked read, and
    that is precisely the bug this closes.
    """
    from app.models.knowledge_base import KnowledgeDocument

    item = (
        await db.execute(
            select(WorksetItem).where(
                WorksetItem.id == item_id,
                WorksetItem.workset_id == workset.id,
            )
        )
    ).scalar_one_or_none()
    if item is None:
        return ReadResult(error="No such item in this workset.")

    if item.status == WORKSET_ITEM_EVICTED_STATUS:
        # Deliberately not an error: the analysis survived the eviction and is
        # worth returning. What must not happen is the caller believing it
        # read the source.
        return ReadResult(
            item_id=item.id,
            title=item.title,
            text=item.summary or "",
            note=(
                "CONTENT EVICTED: this item's raw text was reclaimed to keep "
                "the workset inside its budget. Its summary is above (if one "
                "was written); the source is NOT available and this item is "
                "not searchable."
            ),
        )

    if item.knowledge_document_id is None:
        return ReadResult(
            item_id=item.id,
            title=item.title,
            text=item.summary or "",
            note="This item has no stored content — only its metadata.",
        )

    if item.document_is_foreign and user is not None:
        from app.services.workset import readable_document_ids

        if not await readable_document_ids(
            db, user, [item.knowledge_document_id]
        ):
            return ReadResult(
                item_id=item.id,
                title=item.title,
                text=item.summary or "",
                note=(
                    "CONTENT OUT OF REACH: this item was added by reference to "
                    "a document in another collection, and you can no longer "
                    "open that collection. Its summary is above (if one was "
                    "written); the source text is not available to you and "
                    "this item is not searchable for you."
                ),
            )

    content = await db.scalar(
        select(KnowledgeDocument.content).where(
            KnowledgeDocument.id == item.knowledge_document_id
        )
    )
    if content is None:
        return ReadResult(
            item_id=item.id,
            title=item.title,
            note="This item's document no longer holds any text.",
        )

    total = len(content)
    window = READ_WINDOW_DEFAULT_CHARS if max_chars is None else int(max_chars)
    window = max(1, min(window, READ_WINDOW_MAX_CHARS))

    if chunk_index is not None:
        resolved = await chunks_for_document(db, item.knowledge_document_id)
        chunks = resolved.chunks if resolved else []
        if not chunks:
            return ReadResult(
                item_id=item.id, title=item.title, error="This item has no chunks."
            )
        index = max(0, min(int(chunk_index), len(chunks) - 1))
        before = max(0, min(int(expand_before or 0), MAX_EXPAND_CHUNKS))
        after = max(0, min(int(expand_after or 0), MAX_EXPAND_CHUNKS))
        start = max(0, index - before)
        stop = min(len(chunks), index + after + 1)
        text = "\n\n".join(chunks[start:stop])
        truncated = len(text) > window
        notes: list[str] = []
        if truncated:
            text = text[:window]
            notes.append(
                f"Truncated at {window} characters. Ask for fewer chunks, or "
                "raise max_chars."
            )
        if resolved is not None and not resolved.exact:
            # The window is real text; the ORDINAL is not one an embedding
            # would agree with. Say so rather than report a confident index —
            # asserting an ordinal that does not exist is worse than admitting
            # the addressing is approximate.
            notes.append(
                "APPROXIMATE ADDRESSING: this document's chunking could not be "
                "matched to what was embedded (its parameters are unknown, or "
                "changed after it was embedded), so the text below is a window over "
                "the document rather than a chunk any search hit refers to. The "
                "index and count are not meaningful for this item."
            )
        return ReadResult(
            text=text,
            item_id=item.id,
            title=item.title,
            chunk_index=index if (resolved and resolved.exact) else None,
            chunk_count=len(chunks) if (resolved and resolved.exact) else 0,
            total_chars=total,
            truncated=truncated,
            note=" ".join(notes) or None,
        )

    start = max(0, int(offset or 0))
    if start >= total and total:
        return ReadResult(
            item_id=item.id,
            title=item.title,
            total_chars=total,
            error=(
                f"Offset {start} is past the end of this item "
                f"({total} characters)."
            ),
        )
    end = min(start + window, total)
    return ReadResult(
        text=content[start:end],
        item_id=item.id,
        title=item.title,
        chunk_count=item.chunk_count,
        offset=start,
        end=end,
        total_chars=total,
        truncated=end < total,
        note=(
            f"{total - end} characters remain — continue with offset={end}."
            if end < total
            else None
        ),
    )


@dataclass
class Digest:
    lines: list[str] = field(default_factory=list)
    coverage: Coverage = field(default_factory=Coverage)
    included: int = 0
    omitted: int = 0

    def render(self) -> str:
        body = "\n".join(self.lines)
        footer = self.coverage.describe()
        if self.omitted:
            footer += (
                f" This digest shows {self.included} of "
                f"{self.included + self.omitted} item(s) — the rest were left "
                "out for length, NOT because they are irrelevant. Search or "
                "list by priority to reach them."
            )
        return f"{body}\n\n{footer}" if body else footer


async def digest(
    db: AsyncSession,
    workset: Workset,
    *,
    top_n: int = DIGEST_DEFAULT_TOP_N,
    max_chars: int = DIGEST_DEFAULT_MAX_CHARS,
) -> Digest:
    """The bounded roll-up a synthesis is written from.

    Summaries and metadata only — never raw content. What does not fit is
    reported as omitted rather than dropped, because "here are the 20 that fit"
    read as "here are the 20 that matter" is exactly how a synthesis silently
    loses its tail.
    """
    coverage = await coverage_of(db, workset)
    budget = max(500, min(int(max_chars or DIGEST_DEFAULT_MAX_CHARS), DIGEST_MAX_CHARS))
    limit = max(1, int(top_n or DIGEST_DEFAULT_TOP_N))

    considered = select(WorksetItem).where(
        WorksetItem.workset_id == workset.id,
        WorksetItem.status.not_in(("discarded",)),
    )
    # Count separately and fetch only what could possibly be rendered: a digest
    # of the top 20 must not load a 500-item corpus to discover it omitted 480.
    total_considered = await db.scalar(
        select(func.count()).select_from(considered.subquery())
    ) or 0
    items = (
        (
            await db.execute(
                considered.order_by(
                    WorksetItem.priority_score.is_(None),
                    WorksetItem.priority_score.desc(),
                    WorksetItem.occurred_at.desc().nullslast(),
                    # A total order, so the same items are chosen run to run.
                    WorksetItem.id.asc(),
                ).limit(limit)
            )
        )
        .scalars()
        .all()
    )

    lines: list[str] = []
    used = 0
    included = 0
    for item in items:
        if included >= limit:
            break
        when = item.occurred_at.date().isoformat() if item.occurred_at else "undated"
        score = "—" if item.priority_score is None else str(item.priority_score)
        summary = item.summary or "(not summarised yet)"
        corroboration = (
            f" [corroborated by {item.corroboration_count}]"
            if item.corroboration_count
            else ""
        )
        evicted = " [content evicted]" if item.status == WORKSET_ITEM_EVICTED_STATUS else ""
        line = (
            f"- [{score}] {item.title} ({when}){corroboration}{evicted}\n"
            f"  {summary}\n"
            f"  cite: item={item.id}"
        )
        if used + len(line) > budget:
            break
        lines.append(line)
        used += len(line)
        included += 1

    return Digest(
        lines=lines,
        coverage=coverage,
        included=included,
        # Counted over everything eligible, not over the page that was
        # fetched: the whole point of the number is what did NOT make it in.
        omitted=max(0, total_considered - included),
    )


# ── Listing ──────────────────────────────────────────────────────────────────

LIST_DEFAULT_LIMIT = 25
LIST_MAX_LIMIT = 200


@dataclass
class ItemSummary:
    """One item as a list row — metadata and summary, never content."""

    item_id: uuid.UUID
    title: str
    status: str
    # The caller's own key for the item (``workset_add_item(external_ref=)``):
    # what an agent that re-ingests a source on a schedule matches against to
    # skip what it already handled.
    external_ref: str | None = None
    priority_score: int | None = None
    occurred_at: str | None = None
    summary: str | None = None
    corroboration_count: int = 0
    source_url: str | None = None
    tags: list[str] = field(default_factory=list)
    revision: int = 1
    chars: int = 0


@dataclass
class ItemPage:
    items: list[ItemSummary] = field(default_factory=list)
    total: int = 0
    offset: int = 0
    coverage: Coverage = field(default_factory=Coverage)


async def list_items(
    db: AsyncSession,
    workset: Workset,
    *,
    status: str | None = None,
    min_priority: int | None = None,
    tag: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    sort: str = "priority",
    limit: int = LIST_DEFAULT_LIMIT,
    offset: int = 0,
) -> ItemPage:
    """Enumerate the corpus — metadata and summaries only, never raw content.

    This is the deterministic complement to search: "what is in here, ranked"
    rather than "what matches this". ``since`` / ``until`` filter on the
    document's OWN date, which is what a reporting window means.
    """
    conditions = [WorksetItem.workset_id == workset.id]
    if status:
        conditions.append(WorksetItem.status == status)
    if min_priority is not None:
        conditions.append(WorksetItem.priority_score >= int(min_priority))
    if since is not None:
        conditions.append(WorksetItem.occurred_at >= since)
    if until is not None:
        conditions.append(WorksetItem.occurred_at <= until)

    base = select(WorksetItem).where(*conditions)
    total = await db.scalar(
        select(func.count()).select_from(base.subquery())
    ) or 0

    if sort == "date":
        ordering = (
            WorksetItem.occurred_at.desc().nullslast(),
            WorksetItem.id.asc(),
        )
    else:
        ordering = (
            WorksetItem.priority_score.is_(None),
            WorksetItem.priority_score.desc(),
            WorksetItem.occurred_at.desc().nullslast(),
            WorksetItem.id.asc(),
        )

    page = max(1, min(int(limit or LIST_DEFAULT_LIMIT), LIST_MAX_LIMIT))
    start = max(0, int(offset or 0))
    rows = (
        (
            await db.execute(
                base.order_by(*ordering).limit(page).offset(start)
            )
        )
        .scalars()
        .all()
    )

    items = [
        ItemSummary(
            item_id=row.id,
            title=row.title,
            status=row.status,
            external_ref=row.external_ref,
            priority_score=row.priority_score,
            occurred_at=row.occurred_at.isoformat() if row.occurred_at else None,
            summary=row.summary,
            corroboration_count=row.corroboration_count,
            source_url=row.source_url,
            tags=list(row.tags or []),
            revision=row.revision,
            chars=row.content_chars,
        )
        for row in rows
        # A tag filter in SQL would mean a JSON containment predicate per
        # dialect; the page is already bounded, so it is applied here.
        if not tag or tag in (row.tags or [])
    ]
    return ItemPage(
        items=items,
        total=total,
        offset=start,
        coverage=await coverage_of(db, workset),
    )


# ── Progress and ETA ─────────────────────────────────────────────────────────

# How many recent completions the throughput estimate is built from.
_ETA_SAMPLE_SIZE = 10
# Below this many samples there is no honest estimate to give. A fabricated
# ETA is worse than none, because the agent would wait on it.
_ETA_MIN_SAMPLES = 3
# Coefficient of variation above which the rate is not steady enough to call
# the estimate stable.
_ETA_STABLE_CV = 0.5
# No completion in this long, with work outstanding, reads as stalled.
_STALL_AFTER_SECONDS = 600


@dataclass
class Progress:
    """Where the processing is, and how long it is worth waiting."""

    declared_total: int | None = None
    added: int = 0
    sealed: bool = False
    pending: int = 0
    in_progress: int = 0
    ready: int = 0
    failed: int = 0
    evicted: int = 0
    discarded: int = 0
    throughput_per_min: float | None = None
    samples: int = 0
    eta_seconds: int | None = None
    eta_range: tuple[int, int] | None = None
    eta_confidence: str = "unknown"
    last_progress_at: str | None = None
    stalled: bool = False
    suggested_wait_seconds: int = 0
    settled: bool = False
    complete: bool = False

    @property
    def processed(self) -> int:
        return self.ready + self.failed + self.discarded

    @property
    def remaining(self) -> int:
        return self.pending

    def breakdown(self) -> str:
        """Where every added item is, the non-zero buckets only; they sum to ``added``.

        ``processed`` leaves evicted items out, so without this line a corpus
        with evictions reads as items gone missing.
        """
        buckets = (
            (self.ready, "summarised"),
            (self.failed, "failed"),
            (
                self.evicted,
                "evicted (summary kept, content reclaimed, not searchable)",
            ),
            (self.discarded, "discarded"),
            (self.pending, "outstanding"),
        )
        parts = [f"{count} {label}" for count, label in buckets if count]
        return f"Of the {self.added} added: " + ", ".join(parts) + "." if parts else ""

    def describe(self) -> str:
        """The one-liner an agent acts on. Every claim in it is checkable."""
        lines = [
            f"{self.processed} of {self.added} added item(s) processed"
            + (f" ({self.pending} outstanding)" if self.pending else "")
            + "."
        ]
        # One bucket alone is already said by the first line; two or more
        # (an evicted one above all) need the split to add up.
        if sum(1 for n in (self.ready, self.failed, self.evicted, self.discarded, self.pending) if n) > 1:
            lines.append(self.breakdown())
        if self.declared_total is not None and self.declared_total != self.added:
            lines.append(
                f"The caller announced {self.declared_total}; {self.added} are "
                "present. That gap is unexplained."
            )
        if not self.sealed:
            lines.append(
                "Ingestion is NOT sealed, so more may still arrive and none of "
                "this is final."
            )
        if self.failed:
            lines.append(
                f"{self.failed} item(s) failed permanently and will not "
                "progress."
            )
        if self.stalled:
            lines.append(
                "STALLED: nothing has completed recently while work is "
                "outstanding. Do not keep waiting — check the processor."
            )
        if self.eta_seconds is not None and self.eta_range is not None:
            lines.append(
                f"Estimated {self.eta_seconds}s remaining (between "
                f"{self.eta_range[0]}s and {self.eta_range[1]}s, "
                f"{self.eta_confidence}, from {self.samples} completion(s))."
            )
        elif self.pending:
            lines.append(
                f"No estimate yet — {self.samples} completion(s) observed, "
                f"{_ETA_MIN_SAMPLES} needed. Waiting is still fine; check back."
            )
        if self.settled:
            lines.append("Nothing can still progress.")
        return " ".join(lines)


async def progress_of(db: AsyncSession, workset: Workset) -> Progress:
    """Counts, throughput and an ETA — with the ETA refused when unearned.

    The estimate comes from observed per-item durations and the effective
    worker count, not from one item extrapolated. Below
    ``_ETA_MIN_SAMPLES`` completions there is no estimate at all: an agent
    told "about 9 minutes" will wait about 9 minutes, so a number invented
    from one sample is worse than admitting there isn't one yet.
    """
    from app.tools.wait_policy import DEFAULT_MAXIMUM_WAIT_TIME

    coverage = await coverage_of(db, workset)
    in_progress = await db.scalar(
        select(func.count())
        .select_from(WorksetItem)
        .where(
            WorksetItem.workset_id == workset.id,
            WorksetItem.status == "pending",
            WorksetItem.claimed_at.is_not(None),
        )
    ) or 0

    result = Progress(
        declared_total=workset.declared_total,
        added=coverage.total,
        sealed=coverage.sealed,
        pending=coverage.pending,
        in_progress=in_progress,
        ready=coverage.ready,
        failed=coverage.failed,
        evicted=coverage.evicted,
        discarded=coverage.discarded,
        last_progress_at=(
            workset.last_progress_at.isoformat()
            if workset.last_progress_at
            else None
        ),
        settled=coverage.settled,
        complete=coverage.complete,
    )

    durations = [
        (completed - started).total_seconds()
        for started, completed in (
            await db.execute(
                select(
                    WorksetItem.processing_started_at,
                    WorksetItem.processing_completed_at,
                )
                .where(
                    WorksetItem.workset_id == workset.id,
                    WorksetItem.processing_started_at.is_not(None),
                    WorksetItem.processing_completed_at.is_not(None),
                )
                .order_by(WorksetItem.processing_completed_at.desc())
                .limit(_ETA_SAMPLE_SIZE)
            )
        ).all()
        if started and completed and completed >= started
    ]
    result.samples = len(durations)

    if workset.last_progress_at is not None and result.pending:
        idle = (
            datetime.now(UTC) - workset.last_progress_at
        ).total_seconds()
        result.stalled = idle > _STALL_AFTER_SECONDS

    if durations:
        workers = max(1, workset.workers or 1)
        mean = sum(durations) / len(durations)
        if mean > 0:
            result.throughput_per_min = round(workers * 60.0 / mean, 2)
        if len(durations) >= _ETA_MIN_SAMPLES and result.pending:
            eta = result.pending * mean / workers
            low = result.pending * min(durations) / workers
            high = result.pending * max(durations) / workers
            spread = (
                (sum((d - mean) ** 2 for d in durations) / len(durations)) ** 0.5
            ) / mean
            result.eta_seconds = int(eta)
            result.eta_range = (int(low), int(high))
            result.eta_confidence = (
                "stable"
                if len(durations) >= _ETA_SAMPLE_SIZE and spread < _ETA_STABLE_CV
                else "rough"
            )

    # What to actually pass to `wait`: the estimate, clamped to what a single
    # wait may legally cover, so the agent is not told to ask for a pause that
    # will be clamped out from under it.
    if result.pending and not result.stalled:
        estimate = result.eta_seconds or DEFAULT_MAXIMUM_WAIT_TIME
        result.suggested_wait_seconds = max(
            5, min(int(estimate), DEFAULT_MAXIMUM_WAIT_TIME)
        )
    return result
