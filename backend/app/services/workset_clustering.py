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

"""Near-duplicate clustering and the corroboration score.

Two weeks of reporting means the same incident covered by many sources, and
that redundancy is the most useful signal in the corpus: what several
independent sources report is more trustworthy than one model's read of one
document. The data model carries the columns for it —
``cluster_id``, ``duplicate_of``, ``corroboration_count`` — and this module
computes them; without it, ranking would be one model's opinion wearing the
vocabulary of a corroborated score.

**Where this runs, and why there.** On SEAL, not per item. Clustering needs the
corpus, not a document: "how many other sources say this" is unanswerable while
sources are still arriving, and computed per ingest it would be recomputed N
times for N items and be wrong every time but the last. Seal is the moment the
corpus stops moving. The pass is also re-runnable — the automatic summariser
re-enqueues it once a sealed workset finishes processing, because a workset
sealed while items were still embedding would otherwise cluster over half of
itself.

**How the score is composed.** The two parts stay separate in
``priority_components``, and only the machine's half is ever adjusted:

* ``{"llm": 62, "corroboration": 14}`` — the automatic pass wrote 62, and this
  pass added 14 for two corroborating sources. The stored ``priority_score``
  becomes 76.
* ``{"agent": 62, "corroboration": 14}`` — an agent wrote 62. The corroboration
  is recorded and reported, and the SCORE is left alone. An agent's number is
  its own judgement of the document, and this module does not overrule it; the
  same precedence the automatic summariser follows.

Recomputation reads the base back out of ``priority_components`` rather than
off ``priority_score``, so running this twice cannot compound the bonus.

**Trust**: clustering reads vectors and ids, never content. Nothing here parses
what a document says.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from sqlalchemy import select

from app.core.utils import format_exception_message

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.workset import Workset, WorksetItem

logger = logging.getLogger(__name__)

# Cosine similarity above which two documents are "the same story". Deliberately
# high: the cost of a false positive is an inflated score on an item that is not
# actually corroborated, which is a wrong answer, while a false negative just
# leaves a score where the summariser put it. Two reports of one incident from
# different vendors sit around 0.90-0.95; two unrelated reports in the same
# domain sit far below.
SIMILARITY_THRESHOLD = 0.90
# What one corroborating source is worth, and the most they can add in total.
# Capped so corroboration ranks WITHIN a band rather than dominating it: five
# copies of a trivial notice must not outrank one report of a breach.
CORROBORATION_PER_SOURCE = 7
CORROBORATION_MAX = 20
# Two budgets, because there are two ways this grows and each bounds the other
# not at all.
#
# `_MAX_VECTORS` bounds the ROWS read: a document is many chunks, and a corpus
# of a few hundred documents is a few thousand vectors.
_MAX_VECTORS = 20_000
# `_MAX_CLUSTER_DOCUMENTS` bounds the similarity MATRIX, which is quadratic in
# documents and which the row budget does not constrain at all: 20 000
# single-chunk documents pass the vector cap and produce a 20 000 x 20 000
# float32 product — about 1.6 GB, in a worker process. "A workset holds a few
# hundred documents" is an invariant of the ingest path, not of this function,
# and nothing here enforced it. 2 000 documents is ~16 MB of similarities.
#
# Past either budget the pass clusters what it loaded and REPORTS what it left
# out, rather than quietly ranking a corpus it only partly looked at.
_MAX_CLUSTER_DOCUMENTS = 2_000


@dataclass
class ClusterOutcome:
    """What one clustering pass concluded."""

    documents: int = 0
    clusters: int = 0
    corroborated_items: int = 0
    rescored: int = 0
    # Documents whose vectors were not loaded because the budget ran out.
    skipped: int = 0
    error: str | None = None
    clustered_item_ids: list[uuid.UUID] = field(default_factory=list)


def _centroids(
    rows: list[tuple[str, object]],
) -> tuple[list[str], object]:
    """One unit vector per document, from its chunk vectors.

    The mean of a document's chunks, normalised — so cosine similarity is a dot
    product and the whole comparison is one matrix multiply. Every row handed
    here comes from ONE embedding space; the caller groups by
    ``(model, dimensions)`` first, because a cosine across two spaces is a
    number rather than a similarity.
    """
    import numpy as np

    grouped: dict[str, list] = {}
    for source_id, vector in rows:
        if vector is None:
            continue
        grouped.setdefault(source_id, []).append(np.asarray(vector, dtype="float32"))

    ids: list[str] = []
    vectors: list = []
    for source_id, chunk_vectors in grouped.items():
        stacked = np.vstack(chunk_vectors)
        centroid = stacked.mean(axis=0)
        norm = float(np.linalg.norm(centroid))
        if norm <= 0:
            # A zero centroid carries no direction; comparing it to anything
            # would produce a similarity of 0 at best and a NaN at worst.
            continue
        ids.append(source_id)
        vectors.append(centroid / norm)
    if not vectors:
        return [], np.zeros((0, 0), dtype="float32")
    return ids, np.vstack(vectors)


def _connected_groups(ids: list[str], matrix) -> list[list[str]]:
    """Group documents whose similarity crosses the threshold.

    Single-linkage union-find: A corroborates B and B corroborates C puts all
    three in one cluster even when A and C are further apart. That is the right
    reading for "the same story reported by several sources" — coverage of one
    incident drifts as it is re-reported, and requiring every pair to be near
    every other would split one story into fragments.
    """
    import numpy as np

    parent = list(range(len(ids)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        # Union by INDEX, not by rank or size — the lower index always wins.
        # With the path halving in `find` that is fast enough at this scale,
        # and it makes the representative of a set deterministic, which the
        # cluster id derived below depends on.
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[max(left_root, right_root)] = min(left_root, right_root)

    similarity = matrix @ matrix.T
    # The pairs above the threshold, without `np.triu`'s second full-size float
    # copy: the boolean mask is one byte per cell rather than four, and the
    # matrix is symmetric, so keeping `left < right` covers every pair once and
    # drops the diagonal with it.
    rows, cols = np.nonzero(similarity >= SIMILARITY_THRESHOLD)
    for left, right in zip(rows.tolist(), cols.tolist(), strict=True):
        if left < right:
            union(left, right)

    groups: dict[int, list[str]] = {}
    for index, source_id in enumerate(ids):
        groups.setdefault(find(index), []).append(source_id)
    return [members for members in groups.values() if len(members) > 1]


async def _document_vectors(
    db: AsyncSession, document_ids: list[uuid.UUID]
) -> tuple[dict[tuple[str, int], list[tuple[str, object]]], int]:
    """Chunk vectors for *document_ids*, grouped by what makes them comparable.

    **Scoped to the collection each document actually lives in.**
    ``Embedding.source_id`` is a plain string and is NOT unique on its own —
    the table is indexed on ``(knowledge_base_id, source_id)`` precisely because
    the same document id has rows under several collections. Borrowed documents
    are the ordinary case in this feature, so matching on ``source_id`` alone
    pulled in every other tenant's copy of the same document: their vectors
    moved this corpus's centroids (the same class of gap as on the reading
    path, here on the scoring path), the duplicated chunk sets skewed the mean
    toward whichever copy had the most chunks, and they burned the vector
    budget so truncation fired far earlier than its number suggested.

    The pair comes from ``knowledge_documents``, which is where a document's one
    true collection is recorded — NOT from the workset's own KB, which holds
    only the documents it owns and none of the ones it borrows.

    The group key is ``(embedding_model, embedding_dimensions)``, not dimension
    alone. Two models can share a dimensionality — 1536 is the shape of several
    — and vectors from different models occupy different spaces however wide
    they are: the cosine between them is a number, not a similarity, and
    clustering on it groups items that have nothing to do with each other.

    In practice a corpus is embedded by one model and there is a single group;
    the split only matters across a provider or model change, which is exactly
    when a silently meaningless similarity would be hardest to notice.
    """
    from sqlalchemy import tuple_

    from app.models.embedding import Embedding
    from app.models.knowledge_base import KnowledgeDocument

    if not document_ids:
        return {}, 0

    homes = (
        await db.execute(
            select(KnowledgeDocument.id, KnowledgeDocument.knowledge_base_id)
            .where(KnowledgeDocument.id.in_(list(document_ids)))
        )
    ).all()
    # A row-value IN over `(knowledge_base_id, source_id)`, which is also the
    # shape of `ix_embeddings_kb_source`.
    pairs = [
        (kb_id, str(document_id))
        for document_id, kb_id in homes
        if kb_id is not None
    ]
    if not pairs:
        return {}, 0

    rows = (
        await db.execute(
            select(
                Embedding.source_id,
                Embedding.embedding_model,
                Embedding.embedding_dimensions,
                Embedding.embedding,
            )
            .where(
                tuple_(Embedding.knowledge_base_id, Embedding.source_id).in_(
                    pairs
                )
            )
            .order_by(Embedding.source_id, Embedding.id)
            .limit(_MAX_VECTORS + 1)
        )
    ).all()

    truncated = len(rows) > _MAX_VECTORS
    if truncated:
        rows = rows[:_MAX_VECTORS]
        # The cut lands INSIDE one document's chunk list, and a centroid built
        # from an arbitrary prefix of a document is a wrong answer rather than a
        # missing one. Drop that document whole — the same preference the
        # zero-norm branch in `_centroids` already makes.
        boundary = rows[-1][0]
        rows = [row for row in rows if row[0] != boundary]

    by_space: dict[tuple[str, int], list[tuple[str, object]]] = {}
    read: set[str] = set()
    for source_id, model, dimensions, vector in rows:
        if not source_id:
            continue
        read.add(source_id)
        key = (str(model or ""), int(dimensions or 0))
        by_space.setdefault(key, []).append((source_id, vector))

    if truncated:
        # Everything the budget kept out: the document the cut landed in, and
        # everything ordered after it.
        return by_space, len({
            str(document_id)
            for document_id, _kb in homes
            if str(document_id) not in read
        })

    unembedded = len(pairs) - len(read)
    if unembedded > 0:
        # NOT the budget, and worth telling apart from it: these documents have
        # no vectors at all (still embedding, or too short to leave a usable
        # chunk), which
        # answers "why did nothing cluster?" without a database session.
        logger.debug(
            "Workset clustering: %d document(s) have no embeddings to cluster.",
            unembedded,
        )
    return by_space, 0


def _cluster_identity(workset_id: uuid.UUID, item_ids: list[uuid.UUID]) -> uuid.UUID:
    """A stable id for a cluster, derived from its members.

    Derived rather than random so re-running the pass over an UNCHANGED corpus
    produces the same ids: a stored citation, a UI selection or a log line that
    named a cluster still names the same one.

    The limit of that guarantee, since citations are named as a consumer: the
    anchor is the lowest member id, so a cluster that GAINS a member sorting
    below the current anchor is renamed. Membership churn renames; recomputation
    does not. Anything durable should cite the item, which is stable, rather
    than the cluster, which is a derived grouping.
    """
    anchor = min(str(item_id) for item_id in item_ids)
    return uuid.uuid5(uuid.NAMESPACE_URL, f"workset:{workset_id}:cluster:{anchor}")


def _base_score(item: WorksetItem) -> int | None:
    """The score before any corroboration bonus.

    Read out of the components when they are there, so a second pass adds the
    bonus to the model's number rather than to the number the first pass
    already boosted.
    """
    components = item.priority_components or {}
    for key in ("llm", "agent"):
        value = components.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return item.priority_score


def _apply(item: WorksetItem, *, corroboration: int) -> bool:
    """Write one item's cluster arithmetic. True when its score changed."""
    from app.services.workset import clamp_priority

    item.corroboration_count = corroboration
    base = _base_score(item)
    bonus = min(CORROBORATION_MAX, CORROBORATION_PER_SOURCE * corroboration)

    # MERGED, never replaced, and the key is REMOVED when the bonus is zero.
    # Keeping a stale `corroboration` on the reset path would leave an item
    # reporting `corroboration_count = 0` beside `{"corroboration": 14}` — the
    # count and the component disagreeing, with reads surfacing the component,
    # which is precisely the reset this pass exists to perform. Merging also
    # stops this branch from discarding keys it does not own.
    components = dict(item.priority_components or {})
    if bonus:
        components["corroboration"] = bonus
    else:
        components.pop("corroboration", None)

    if base is None:
        # Nothing has scored this item yet. The count is still recorded — it is
        # a fact about the corpus — but there is no number to add it to, and
        # inventing one would be this module scoring a document it never read.
        item.priority_components = components or None
        return False

    source = item.priority_source or "llm"
    key = "agent" if source == "agent" else "llm"
    components[key] = base
    item.priority_components = components

    if key == "agent":
        # An agent's own judgement of the document is not overruled here.
        return False
    updated = clamp_priority(base + bonus)
    changed = updated != item.priority_score
    item.priority_score = updated
    return changed


async def recompute_clusters(
    db: AsyncSession, workset: Workset
) -> ClusterOutcome:
    """Cluster *workset*'s items and fold corroboration into their scores.

    Idempotent: it recomputes every item's grouping from the current corpus,
    including resetting one that is no longer a duplicate of anything. The
    caller commits.
    """
    from app.models.workset import WORKSET_ITEM_EVICTED_STATUS, WorksetItem

    items = (
        (
            await db.execute(
                select(WorksetItem).where(
                    WorksetItem.workset_id == workset.id,
                    WorksetItem.status.not_in(
                        (WORKSET_ITEM_EVICTED_STATUS, "discarded")
                    ),
                )
            )
        )
        .scalars()
        .all()
    )
    if not items:
        return ClusterOutcome()

    by_document = {
        str(item.knowledge_document_id): item
        for item in items
        if item.knowledge_document_id is not None
    }

    outcome = ClusterOutcome(documents=len(by_document))
    try:
        by_space, outcome.skipped = await _document_vectors(
            db,
            [
                item.knowledge_document_id
                for item in items
                if item.knowledge_document_id is not None
            ],
        )
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning(
            "Workset clustering could not read vectors for %s: %s",
            workset.id,
            format_exception_message(exc),
        )
        return ClusterOutcome(error=format_exception_message(exc))

    groups: list[list[str]] = []
    for rows in by_space.values():
        ids, matrix = _centroids(rows)
        if len(ids) > _MAX_CLUSTER_DOCUMENTS:
            # Bounded HERE rather than by the row budget, because the product
            # below is quadratic in documents and the row budget says nothing
            # about how many documents those rows belong to. Deterministic:
            # the rows arrive ordered by document, so the same corpus keeps
            # the same head.
            outcome.skipped += len(ids) - _MAX_CLUSTER_DOCUMENTS
            ids = ids[:_MAX_CLUSTER_DOCUMENTS]
            matrix = matrix[:_MAX_CLUSTER_DOCUMENTS]
        if len(ids) > 1:
            groups.extend(_connected_groups(ids, matrix))

    # Every item starts from "corroborated by nothing"; membership below is
    # what puts a value back. Without this reset an item that stopped being a
    # duplicate — its twin discarded, or revised into different content —
    # would keep a corroboration it no longer has.
    clustered: set[uuid.UUID] = set()
    for group in groups:
        members = [by_document[source_id] for source_id in group if source_id in by_document]
        if len(members) < 2:
            continue
        cluster_id = _cluster_identity(workset.id, [m.id for m in members])
        # The representative is the best-scored member, oldest first on a tie:
        # a `duplicate_of` chain should point at the copy an analyst would
        # rather read, and at the one that reported it first when they are
        # equal.
        representative = sorted(
            members,
            key=lambda item: (
                item.priority_score is None,
                -(item.priority_score or 0),
                item.occurred_at.timestamp() if item.occurred_at else float("inf"),
                str(item.id),
            ),
        )[0]
        outcome.clusters += 1
        for member in members:
            member.cluster_id = cluster_id
            member.duplicate_of = (
                None if member.id == representative.id else representative.id
            )
            if _apply(member, corroboration=len(members) - 1):
                outcome.rescored += 1
            clustered.add(member.id)
            outcome.corroborated_items += 1

    for item in items:
        if item.id in clustered:
            continue
        item.cluster_id = None
        item.duplicate_of = None
        if item.corroboration_count or (item.priority_components or {}).get(
            "corroboration"
        ):
            if _apply(item, corroboration=0):
                outcome.rescored += 1
        else:
            item.corroboration_count = 0

    outcome.clustered_item_ids = sorted(clustered)
    return outcome


async def cluster_workset(workset_id: str | uuid.UUID) -> ClusterOutcome:
    """Job entry point: cluster one workset in its own session.

    Never raises. It runs after the work it depends on has already succeeded —
    a sealed corpus, embedded — and failing the job would re-run that work to
    no purpose. A corpus that is not clustered ranks by the summariser's score
    alone, which is where it started.
    """
    from app.core.database import async_session
    from app.models.workset import Workset

    try:
        parsed = (
            workset_id
            if isinstance(workset_id, uuid.UUID)
            else uuid.UUID(str(workset_id))
        )
    except (ValueError, AttributeError, TypeError):
        return ClusterOutcome(error="Not a workset id.")

    try:
        async with async_session() as db:
            workset = await db.get(Workset, parsed)
            if workset is None:
                return ClusterOutcome(error="No such workset.")
            outcome = await recompute_clusters(db, workset)
            await db.commit()
            if outcome.skipped:
                logger.warning(
                    "Workset %s clustered %d document(s) and skipped %d: the "
                    "vector budget of %d was reached.",
                    parsed,
                    outcome.documents - outcome.skipped,
                    outcome.skipped,
                    _MAX_VECTORS,
                )
            return outcome
    except Exception as exc:
        logger.warning(
            "Workset clustering failed for %s: %s",
            workset_id,
            format_exception_message(exc),
        )
        return ClusterOutcome(error=format_exception_message(exc))


async def enqueue_clustering(workset_id: uuid.UUID, *, delay_seconds: int = 5) -> bool:
    """Schedule a clustering pass. False when it could not be scheduled.

    Fire-and-forget, like the embedding queue: a corpus that misses its
    clustering ranks by the summariser's score, which is a degradation, not a
    loss. Call it AFTER committing — the job re-reads the workset, and a job
    that dequeues before the seal is visible would cluster an unsealed corpus.

    Bounded like the embedding enqueue: the queue's connection has no socket
    timeout, and this runs in requests too (a seal, an ingest that settles a
    sealed corpus).
    """
    import asyncio
    import time

    try:
        from saq import Job

        from app.core.config import settings as app_settings
        from app.services import kb_helpers
        from app.worker import queue

        await asyncio.wait_for(
            queue.enqueue(
                Job(
                    function="workset_cluster_job",
                    kwargs={"workset_id": str(workset_id)},
                    # Unique per enqueue: SAQ dedupes by key, and a bare key
                    # would drop the pass that follows a late-finishing item.
                    key=f"workset-cluster-{workset_id}-{uuid.uuid4().hex[:8]}",
                    scheduled=int(time.time()) + max(0, delay_seconds),
                    # SAQ's own default is 10 s. A pass reads up to
                    # `_MAX_VECTORS` vectors, compares every document with
                    # every other and writes each item's corroboration - a
                    # large corpus outlasts that, and a timed-out pass leaves
                    # the corpus unclustered for good.
                    timeout=app_settings.worker_job_timeout,
                )
            ),
            timeout=kb_helpers._ENQUEUE_TIMEOUT_SECONDS,
        )
        return True
    except Exception as exc:
        logger.warning(
            "Workset %s could not be queued for clustering: %s",
            workset_id,
            format_exception_message(exc),
        )
        return False
