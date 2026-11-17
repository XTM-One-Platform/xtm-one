# SPDX-License-Identifier: Apache-2.0

"""Workset - a scoped, searchable working corpus for large-volume analysis.

A Workset is the intermediate object behind "analyse and synthesise the reports
of the last two weeks": a bounded collection of items an agent builds up,
summarises, prioritises and reasons over **without ever holding it in a
context**. It is the third member of the coordination family and deliberately
distinct from the other two:

* ``WorkItem`` is a unit of *work* to be picked up - it carries a payload, not
  content, and has no retrieval.
* ``SharedState`` is a key -> value *blackboard* - exact-match keys, free-form
  JSON, explicitly not sized for volume.
* ``Workset`` is the *corpus*: many items, each with stored content that is
  chunked and embedded, a summary, a priority and a status.

Storage split, and why:

* The **content and its vectors** live in a system-managed ``KnowledgeBase``
  (``is_system_managed = True``, hidden from every KB list and agent picker),
  one per workset. That reuses chunking, embedding, hybrid search, windowed
  verbatim reads and - importantly - the existing guard that a document too
  large for a context is never injected but handed to a work item.
* ``workset_items`` carries only the **state layer**: dedup key, summary,
  priority, status, clustering. It is small and queried constantly; the heavy
  text stays where the retrieval machinery already knows how to handle it.

An item created *by reference* to an existing ``KnowledgeDocument`` does not
copy or re-embed it: the item points at the foreign document, and purging a
workset deletes its own system KB but never a foreign one.

**Trust**: item content comes from documents this platform did not author -
threat reports, mails, integration payloads - and is summarised by a model
whose output an agent then reads. Every column fed from item content is
untrusted data, never instruction, and the taint travels with it.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.core.scopes import COORDINATION_SCOPE_DEFAULT, COORDINATION_SCOPES
from app.core.utils import CORRELATION_KEY_MAX_LENGTH

# ── Vocabulary ───────────────────────────────────────────────────────────────

# Where a workset lives. `capability.shared_state` asks the builder the same
# question, so the vocabulary is shared rather than mirrored by hand — see
# `app.core.scopes` for why that list is load-bearing and why it
# is deliberately not the event vocabulary. `run` isolates concurrent runs,
# `flow` capitalises across a flow's runs, `global` is the owner's own
# namespace.
#
# Kept under the historical names: `resolve_workset` validates against
# WORKSET_SCOPES and the builder node defaults from WORKSET_DEFAULT_SCOPE, and
# renaming those is churn with no reader benefit.
WORKSET_SCOPES: tuple[str, ...] = COORDINATION_SCOPES
WORKSET_DEFAULT_SCOPE = COORDINATION_SCOPE_DEFAULT

# A workset is `active` while it is being built or read, `resolved` once the
# work concluded (kept briefly, then purged by the retention sweep) - the same
# lifecycle SharedState uses, for the same reason: a workspace that nobody
# cleans up grows without bound.
WORKSET_STATUSES: tuple[str, ...] = ("active", "resolved")
WORKSET_ACTIVE_STATUS = "active"
WORKSET_RESOLVED_STATUS = "resolved"

# Who writes each item's summary and priority.
#   auto  - a bounded LLM pass in the same background job that embeds the item
#   agent - a processor agent drains the queue with the workset tools
#   none  - nothing automatic; the reading agent writes them itself
WORKSET_PROCESSING_MODES: tuple[str, ...] = ("auto", "agent", "none")
WORKSET_DEFAULT_PROCESSING = "auto"

# Item lifecycle. `pending` is awaiting summary/priority, `ready` has both,
# `failed` exhausted its attempts (terminal for waiting - a waiter must never
# hang on an item that can no longer progress), `evicted` lost its content to a
# budget overflow but kept its metadata, `discarded` was dropped on purpose,
# `consumed` was folded into a synthesis.
WORKSET_ITEM_STATUSES: tuple[str, ...] = (
    "pending",
    "ready",
    "consumed",
    "discarded",
    "evicted",
    "failed",
)
WORKSET_ITEM_PENDING_STATUS = "pending"
WORKSET_ITEM_READY_STATUS = "ready"
WORKSET_ITEM_EVICTED_STATUS = "evicted"
# Statuses that can still progress; used by the progress/wait terminal check.
WORKSET_ITEM_PENDING_STATUSES: tuple[str, ...] = ("pending",)

# How an item's content got here.
WORKSET_SOURCE_TYPES: tuple[str, ...] = (
    "inline",
    "knowledge_document",
    "file_attachment",
    "integration",
)

# Who wrote a summary / priority. Recorded because precedence depends on it:
# the automatic pass only ever fills what is empty and never overwrites what an
# agent wrote.
WORKSET_VALUE_SOURCES: tuple[str, ...] = ("agent", "auto")

# Default budgets. Generous enough for a two-week reporting window, bounded
# enough that a runaway ingestion is caught rather than discovered on the bill.
WORKSET_DEFAULT_MAX_ITEMS = 500
WORKSET_DEFAULT_MAX_CONTENT_CHARS = 20_000_000
# The SPEND ceiling: how many automatic summarisation calls one workset may
# ever pay for. Storage and spend are different budgets and the item count
# bounds only the first of them — a re-ingested correction, a transient retry
# and a borrowed document its owner re-embeds all buy another call for an item
# that already counted. Without this the exposure of a feature whose whole
# premise is bulk ingestion is unbounded over the corpus's life.
#
# 200 covers the motivating window (120 reports) with room for
# revisions, and a larger corpus has to ask for a larger budget rather than
# discovering the cost afterwards. Exhaustion is loud: the remaining items are
# failed with an actionable message and every read says the ceiling was hit.
WORKSET_DEFAULT_MAX_AUTO_CALLS = 200
# Priority is an integer 0-100: coarse enough that a model can produce it
# consistently, fine enough to rank a few hundred items.
WORKSET_PRIORITY_MIN = 0
WORKSET_PRIORITY_MAX = 100


class Workset(Base):
    """A scoped, disposable working corpus."""

    __tablename__ = "worksets"
    __table_args__ = (
        # Identity is per-USER for an unfiled corpus and per-WORKSPACE for a
        # filed one, which a single unique constraint cannot express.
        # Two PARTIAL uniques do.
        #
        # The trap this replaces: the old constraint noted that "NULLs are
        # distinct in Postgres, so anonymous rows never collide". Benign for
        # anonymous rows, a HAZARD here — setting `user_id = NULL` to make a
        # workspace corpus user-agnostic would have disabled uniqueness
        # entirely, so `resolve_workset`'s ON CONFLICT would stop conflicting
        # and every resolve would insert a fresh corpus. The predicates key on
        # `workspace_id`, which is never NULL on the rows it governs.
        # RENAMED, not reused: the old `uq_worksets_user_key` CONSTRAINT owns an
        # index of that name, and the partial replacements have to be built
        # CONCURRENTLY *before* it is dropped — otherwise there is a window with
        # no uniqueness at all, which on a rolling deploy is a window for an old
        # replica to insert a duplicate. Two objects cannot share a name, so the
        # unfiled index takes a new one and the constraint keeps its own until it
        # goes.
        Index(
            "uq_worksets_user_key_unfiled",
            "user_id",
            "key",
            unique=True,
            postgresql_where=text("workspace_id IS NULL"),
        ),
        Index(
            "uq_worksets_workspace_key",
            "workspace_id",
            "key",
            unique=True,
            postgresql_where=text("workspace_id IS NOT NULL"),
        ),
        Index("ix_worksets_user_status", "user_id", "status"),
        Index("ix_worksets_scope_ref", "scope", "scope_ref"),
        Index("ix_worksets_company_managed", "is_company_managed"),
        # Drives the retention sweep.
        Index("ix_worksets_expires_at", "expires_at"),
        Index("ix_worksets_workspace", "workspace_id"),
        # `workspace_id` and `scope` must agree, or a row is accepted at WRITE
        # time that fails at READ time: `_scope_key` would raise on a
        # `workspace`-scoped row with no workspace, or namespace against
        # nothing. This is the only one of the three tool tables that can
        # carry the check — `shared_states` and `work_items` have no `scope`
        # column, so for them the invariant lives in the write path.
        CheckConstraint(
            "(scope = 'workspace') = (workspace_id IS NOT NULL)",
            name="ck_worksets_workspace_scope",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    # Acting / owning user, kept for provenance even on company-managed rows
    # (mirrors WorkItem). NULL only when the creator is anonymous.
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=True,
    )
    is_company_managed: Mapped[bool] = mapped_column(Boolean, default=False)

    # Caller-chosen identifier, unique per user scope.
    key: Mapped[str] = mapped_column(
        String(CORRELATION_KEY_MAX_LENGTH), nullable=False
    )
    label: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Widened from String(10) with the `workspace` scope: "workspace"
    # is nine characters and fit by one, which is a ceiling to discover on the
    # NEXT scope name rather than this one.
    scope: Mapped[str] = mapped_column(
        String(30), default=WORKSET_DEFAULT_SCOPE, nullable=False
    )
    # The run or flow this workset is scoped to, as text: for THOSE scopes the
    # referent's kind depends on `scope`, so this is deliberately not a typed
    # FK. A run-scoped workset is purged with its run; the column is what makes
    # that findable.
    #
    # `workspace` is the exception and carries a typed FK instead — see
    # `workspace_id` below. The table is therefore deliberately HYBRID, not
    # half-migrated: the reasoning above still holds for run and flow, whose
    # referents are genuinely different kinds of thing, while a workspace is a
    # first-class, long-lived, user-visible entity and earns real referential
    # integrity.
    scope_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # The workspace this corpus belongs to when `scope == "workspace"`, and
    # NULL otherwise — kept honest by `ck_worksets_workspace_scope` below.
    #
    # RESTRICT, deliberately, even though the row IS meant to go with the
    # workspace. A workset owns documents, chunks and vectors, and the only
    # code that knows what it owns is `services.workset.purge_workset` — shared
    # by the REST purge and the retention sweep precisely so "what exactly gets
    # deleted" has one answer. A DB-level CASCADE would delete this row WITHOUT
    # calling it, orphaning the KnowledgeBase, its documents and every
    # embedding: the storage leak the TTL sweep exists to prevent, and
    # undetectable afterwards because the row that knew what they belonged to
    # is the one that got cascaded away.
    #
    # So deleting a workspace must enumerate its worksets, purge each through
    # that function, and only then delete the workspace. RESTRICT is what makes
    # that ordering enforceable rather than merely intended.
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="RESTRICT"),
        nullable=True,
    )

    # The agent responsible for keeping this tidy - resolving it when the work
    # concludes (same contract as SharedState.owner_agent_id).
    owner_agent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agents.id", ondelete="SET NULL"),
        nullable=True,
    )

    # The system-managed KB holding this workset's documents and vectors.
    # SET NULL rather than CASCADE: losing the KB must never silently delete
    # the state layer, which still holds every summary and priority.
    knowledge_base_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("knowledge_bases.id", ondelete="SET NULL"),
        nullable=True,
    )

    status: Mapped[str] = mapped_column(
        String(20), default=WORKSET_ACTIVE_STATUS, nullable=False
    )

    # ── Progress accounting ──────────────────────────────────────────────
    # What the caller ANNOUNCED it would add ("the query returned 137
    # reports"). A claim, never a fact: it is reported alongside `item_count`
    # and never silently reconciled with it.
    declared_total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    declared_total_source: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Ingestion closed. Until this is set, "everything added is processed" does
    # NOT mean "complete", and progress must say so in those words.
    sealed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Last time an item finished processing; `stalled` is derived from it at
    # read time (never stored, mirroring WorkItem's derived `stale`).
    last_progress_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # ── Recurring ingestion ──────────────────────────────────────────────
    # High-water mark of what has been ingested, for a sliding-window job (the
    # bi-weekly report re-run). The next run reads from here minus an overlap
    # so late-arriving corrections are not missed; it only advances on a
    # SEALED workset, so a failed run never skips a window.
    watermark_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # ── Processing ───────────────────────────────────────────────────────
    processing: Mapped[str] = mapped_column(
        String(10), default=WORKSET_DEFAULT_PROCESSING, nullable=False
    )
    processor_agent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agents.id", ondelete="SET NULL"),
        nullable=True,
    )
    # How many workers drain the queue in parallel; also the divisor when an
    # ETA is derived from observed per-item durations.
    workers: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    # The LLM settings for the automatic pass, in the shape a `capability.llm`
    # node stores (provider / model / temperature / ...), so one parser serves
    # the flow node and the agent-level binding alike.
    llm_config: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # ── Budgets and counters ─────────────────────────────────────────────
    max_items: Mapped[int] = mapped_column(
        Integer, default=WORKSET_DEFAULT_MAX_ITEMS, nullable=False
    )
    max_content_chars: Mapped[int] = mapped_column(
        Integer, default=WORKSET_DEFAULT_MAX_CONTENT_CHARS, nullable=False
    )
    # The spend ceiling and its counter. `auto_calls_used` only ever grows: it
    # is a record of what was paid for, not of what is currently stored, so a
    # correction that re-summarises an item is a second call and says so.
    max_auto_calls: Mapped[int] = mapped_column(
        Integer, default=WORKSET_DEFAULT_MAX_AUTO_CALLS, nullable=False
    )
    auto_calls_used: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )
    # An IMPLICIT workset is one the runtime derives on its own (an agent's
    # chat corpus, an assignment's corpus) rather than one a flow node or a
    # caller named. It lives as long as its agent, so a lifetime ceiling would
    # turn it into a dead corpus after its 200th call: for it `max_auto_calls`
    # is a ROLLING budget, `auto_calls_used` counting the calls of the window
    # that opened at `auto_calls_window_started_at` and starting again from 0
    # once that window is older than `workset_implicit_budget_window_hours`.
    # It also carries a sliding idle expiry, and a resolved or sealed one is
    # retired by `resolve_workset` rather than written into (rotation).
    implicit: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"), nullable=False)
    auto_calls_window_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Live items (evicted ones keep their row but stop counting against the
    # item budget - their content is what was reclaimed).
    item_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    evicted_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    content_chars: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # Retention: when this workset becomes purgeable even if nobody resolved it.
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


class WorksetItem(Base):
    """One document in a workset, with its derived state."""

    __tablename__ = "workset_items"
    __table_args__ = (
        # Idempotent ingestion: re-adding the same source updates in place
        # rather than duplicating. This is what makes a sliding-window re-run
        # cheap and what lets an overlap be read without fear.
        UniqueConstraint(
            "workset_id", "external_ref", name="uq_workset_items_ref"
        ),
        Index("ix_workset_items_workset_status", "workset_id", "status"),
        # The ranked read: highest priority first within a workset.
        Index("ix_workset_items_workset_priority", "workset_id", "priority_score"),
        # The window read ("the last two weeks") - on the document's OWN date.
        Index("ix_workset_items_workset_occurred", "workset_id", "occurred_at"),
        # Near-duplicate grouping, and the corroboration count derived from it.
        Index("ix_workset_items_cluster", "workset_id", "cluster_id"),
        Index("ix_workset_items_document", "knowledge_document_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    workset_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("worksets.id", ondelete="CASCADE"),
        nullable=False,
    )

    # Caller-supplied dedup key (the OpenCTI report id, the mail id, the issue
    # number). NULL is allowed - not every source has one - and NULLs are
    # distinct in Postgres, so unkeyed items never collide.
    external_ref: Mapped[str | None] = mapped_column(
        String(CORRELATION_KEY_MAX_LENGTH), nullable=True
    )

    title: Mapped[str] = mapped_column(Text, nullable=False)
    source_type: Mapped[str] = mapped_column(
        String(20), default="inline", nullable=False
    )
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The document's OWN date (published / reported), not when it was ingested.
    # A sliding window filters on this; using `created_at` would re-date the
    # whole corpus on every re-ingestion.
    occurred_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Where the content actually lives. SET NULL on both: losing the document
    # must leave the item's summary and priority readable rather than deleting
    # the analysis with the source.
    knowledge_document_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("knowledge_documents.id", ondelete="SET NULL"),
        nullable=True,
    )
    file_attachment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("file_attachments.id", ondelete="SET NULL"),
        nullable=True,
    )
    # True when the document belongs to a KB this workset does not own (an
    # ingest by reference). Purge must never delete a foreign document.
    document_is_foreign: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )

    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    content_chars: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Bumped when a re-ingestion brings CHANGED content for the same
    # `external_ref` - a correction, not a duplicate. The summary is reset to
    # pending with it, because the old one described different text (unless
    # the same re-ingestion gives one, which describes the new text).
    revision: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    # ── Derived state ────────────────────────────────────────────────────
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    summary_source: Mapped[str | None] = mapped_column(String(10), nullable=True)
    priority_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    priority_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    priority_source: Mapped[str | None] = mapped_column(String(10), nullable=True)
    # The score's parts, kept separate rather than folded into one opaque
    # number: `{"llm": 62, "corroboration": 18}`. What several independent
    # sources report is more trustworthy than one model's read of one
    # document, and an operator must be able to see which is which.
    priority_components: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    status: Mapped[str] = mapped_column(
        String(20), default=WORKSET_ITEM_PENDING_STATUS, nullable=False
    )
    # Never evicted by a budget overflow, whatever its score.
    pinned: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # ── Redundancy ───────────────────────────────────────────────────────
    # Near-duplicate grouping: two weeks of reports means the same incident
    # covered by many sources. The cluster is both a de-duplication device and
    # a salience signal.
    cluster_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    duplicate_of: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workset_items.id", ondelete="SET NULL"),
        nullable=True,
    )
    # How many other items corroborate this one (cluster size - 1), cached so
    # ranking does not recount on every read.
    corroboration_count: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )

    tags: Mapped[list | None] = mapped_column(JSON, nullable=True, default=list)
    extra_metadata: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # ── Claim + processing bookkeeping ───────────────────────────────────
    # Atomic claim (single conditional UPDATE, the `claim_work_item` pattern)
    # so several workers drain one queue without two doing the same item.
    claimed_by_agent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agents.id", ondelete="SET NULL"),
        nullable=True,
    )
    claimed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # The two timestamps an ETA is computed from - per-item observed duration,
    # divided by the effective worker count.
    processing_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    processing_completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )
