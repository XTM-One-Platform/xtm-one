# SPDX-License-Identifier: Apache-2.0

"""Knowledge Base models – vectorized document collections for RAG.

A KnowledgeBase is a named collection of documents that agents can search
during assignment execution. Documents are chunked and embedded into pgvector
for semantic search.

Company-managed knowledge bases (is_company_managed=True) are shared with all
users but only admin-editable.

Use cases:
  - Vectorize Notion docs, OpenCTI docs, internal wikis
  - Learn email style from past emails
  - Product documentation for auto-answering questions
"""

import uuid
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class KnowledgeBase(Base):
    """A named collection of vectorized documents."""

    __tablename__ = "knowledge_bases"
    # ``user_id`` is indexed inline. The company-managed flag is filtered on by
    # every list endpoint's three-tier visibility query.
    __table_args__ = (
        Index("ix_knowledge_bases_is_company_managed", "is_company_managed"),
        # One Sources and one Conversations knowledge base per workspace.
        Index(
            "uq_knowledge_bases_workspace_role",
            "owner_workspace_id",
            "workspace_role",
            unique=True,
            postgresql_where=sa.text("owner_workspace_id IS NOT NULL"),
        ),
        sa.CheckConstraint(
            "(owner_workspace_id IS NULL) = (workspace_role IS NULL)",
            name="ck_knowledge_bases_workspace_role",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    # Company-managed KBs are shared with all users; only admins can edit.
    # user_id is NULL when True.
    is_company_managed: Mapped[bool] = mapped_column(Boolean, default=False)

    # Group sharing: list of Group UUIDs this entity is shared with.
    # Members of these groups can see (but not edit) the entity.
    shared_group_ids: Mapped[list | None] = mapped_column(
        JSON(none_as_null=True), nullable=True, default=None
    )
    shared_group_permissions: Mapped[dict | None] = mapped_column(
        JSON(none_as_null=True), nullable=True, default=None
    )

    # Per-user sharing + company-managed narrowing. Same shape as the
    # group columns above, keyed by User UUID; see ``app.models.agent`` for the
    # rationale and ``app.api.v1.helpers.user_visible_filter`` for enforcement.
    shared_user_ids: Mapped[list | None] = mapped_column(
        JSON(none_as_null=True), nullable=True, default=None
    )
    shared_user_permissions: Mapped[dict | None] = mapped_column(
        JSON(none_as_null=True), nullable=True, default=None
    )
    access_restricted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false", default=False
    )

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Moved by every document add, edit or delete, only through
    # ``kb_helpers.bump_kb_revision``; never assigned or read from a loaded row.
    # In the Full Loop reuse fingerprint, so a content change invalidates reused
    # predictions. Metadata edits do not move it; the fingerprint reads
    # ``embedding_model`` itself.
    revision: Mapped[int] = mapped_column(
        Integer, default=0, server_default=sa.text("0"), nullable=False
    )

    # Tags – free-form labels for filtering and organisation
    tags: Mapped[list | None] = mapped_column(JSON, nullable=True, default=list)

    # Embedding config
    embedding_model: Mapped[str] = mapped_column(
        String(100), default="text-embedding-3-large"
    )
    chunk_size: Mapped[int] = mapped_column(Integer, default=512)
    chunk_overlap: Mapped[int] = mapped_column(Integer, default=128)

    # System-managed flag (auto-created KBs for agents). NOTE: system-managed
    # KBs are still user-facing — an agent's auto-created collection is listed,
    # browsable and bindable like any other. It is `is_internal` below, not
    # this flag, that hides one.
    is_system_managed: Mapped[bool] = mapped_column(default=False)

    # Internal storage owned by another primitive (a Workset's corpus),
    # not a collection anyone browses, binds to an agent or picks in a form.
    # Enforced in `user_visible_filter`, the single choke point every list,
    # fetch and picker routes through — including for admins, because
    # `is_admin` widens WHICH entities you see, it does not turn another
    # primitive's storage into content. The owning primitive reads its own KB
    # by id; its own row is the authorization.
    is_internal: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false", default=False
    )

    # A knowledge base a workspace owns: its Sources or its
    # Conversations. Unlike ``is_internal`` it is listed in the library, but
    # its visibility is the WORKSPACE's (``user_visible_filter`` reads the
    # workspace, never this row's own sharing columns, which stay empty), it is
    # changed only from the workspace, and it is never bound to an agent. It
    # goes with the workspace: every child of a knowledge base cascades in the
    # database, so a workspace deleted by any path - a user's deletion
    # included - leaves nothing behind.
    owner_workspace_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=True,
    )
    #: ``sources`` | ``conversations``; set exactly when ``owner_workspace_id`` is.
    workspace_role: Mapped[str | None] = mapped_column(String(20), nullable=True)

    @classmethod
    def visibility_parent(cls):
        """``(column, parent model)``: a row whose column is set is visible
        exactly when that parent is (``helpers.user_visible_filter``), its own
        share columns unread. Here, the workspace that owns it."""
        from app.models.workspace import Workspace

        return cls.owner_workspace_id, Workspace

    # Built-in KBs are seeded at startup and cannot be modified by anyone
    # (not even admins). Their config is fully synced on every restart.
    is_builtin: Mapped[bool] = mapped_column(Boolean, default=False)

    # Content lock — "this collection is ground truth, nothing edits it".
    #
    # A read-only KB stays fully SEARCHABLE and READABLE; what is frozen is its
    # *content*. It exists because per-agent write grants
    # (``Agent.knowledge_base_permissions``) are granted on the AGENT side, so
    # any agent owner could hand themselves write access to an authoritative
    # collection. This flag lives on the knowledge base instead, so the
    # protection cannot be granted away from the other end: read-only KBs are
    # filtered out of every agent's writable scope, and the write tools reject
    # them even if a stale scope reaches them.
    #
    # It blocks AD-HOC content mutation — agent writes, manual document
    # add/upload/URL-crawl, document update, document delete. It deliberately
    # does NOT block configured import (sync) sources or re-embedding: a KB whose
    # authority *is* an external system of record (a policy wiki in Notion, a
    # spec repo on GitHub) is the most important shape of ground truth, and
    # freezing its refresh would silently rot it. To freeze a synced KB
    # completely, disable its sync sources as well.
    #
    # Distinct from ``is_builtin`` (platform-seeded, nothing may touch it at all,
    # not user-settable) and from the three-tier company-managed read-only gate
    # (which is about WHO may administer the entity, not about its content).
    is_read_only: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=sa.text("false"), nullable=False
    )

    # How an HTML document is embedded: ``source`` (the default, and
    # every KB created before the setting existed) chunks the stored markup as
    # is; ``rendered`` chunks the page's rendered text - its embedded data
    # summarised first, then the text in reading order - through
    # ``attachment_extract``. The stored source is the same either way (it is
    # what ``get_knowledge_document`` returns); only what is chunked and
    # embedded differs, resolved by ``kb_embedding_text.resolve_embedding_text``
    # at processing time, so changing the setting re-embeds nothing until a
    # document is processed again.
    html_ingestion: Mapped[str] = mapped_column(
        String(20), default="source", server_default=sa.text("'source'"), nullable=False
    )

    # Whether the figures of an uploaded PDF are described for the index: the
    # pages that carry visuals (a large image, vector drawings, almost no text
    # layer) are rendered and read by a vision model at ingestion, and each
    # description is inserted after its page's text before chunking
    # (``kb_figures``). Off by default and for every KB created before the
    # setting; it applies to PDFs uploaded while it is on, and the stored
    # source (the text layer) is the same either way.
    describe_figures: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=sa.text("false"), nullable=False
    )

    # Set True once an admin modifies the import (sync) sources of a built-in
    # KB. Startup seeding then stops overwriting/removing this KB's sync sources
    # so airgapped / internal-URL customizations survive restarts. All other
    # built-in fields (name, description, etc.) are still fully synced.
    sync_sources_customized: Mapped[bool] = mapped_column(Boolean, default=False)

    # Stats
    document_count: Mapped[int] = mapped_column(Integer, default=0)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    query_count: Mapped[int] = mapped_column(Integer, default=0)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


class KBSyncSource(Base):
    """A configured sync source for a knowledge base with optional schedule."""

    __tablename__ = "kb_sync_sources"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    knowledge_base_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("knowledge_bases.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=True
    )

    # Explicit integration to use for syncing (optional — falls back to auto-discovery)
    integration_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("integrations.id", ondelete="SET NULL"),
        nullable=True,
    )

    # Source type: notion | microsoft-drive | google-drive | github
    source_type: Mapped[str] = mapped_column(String(30), nullable=False)

    # Source-specific configuration (e.g. repo, branch, path for GitHub)
    config: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)

    # Schedule: "manual" | "6h" | "12h" | "daily" | "weekly"
    sync_interval: Mapped[str] = mapped_column(
        String(20), nullable=False, default="manual"
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    # Sync tracking
    last_synced_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_sync_status: Mapped[str | None] = mapped_column(
        String(20), nullable=True
    )  # syncing | success | error
    last_sync_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_sync_documents: Mapped[int] = mapped_column(Integer, default=0)

    # Auto-retry with exponential backoff for failed/timed-out syncs.
    # Mirrors the KnowledgeDocument embedding auto-retry pattern
    # (embedding_retry_count / embedding_next_retry_at). The scheduler
    # increments ``sync_failure_count`` on each consecutive failure,
    # resets it to 0 on success, and gates error-state retries on
    # ``sync_next_retry_at`` (now + base * 2^(count - 1) + jitter) instead
    # of the regular interval — so a transient failure recovers promptly
    # while a persistently-failing source backs off instead of being
    # hammered every tick.
    sync_failure_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default=sa.text("0"), nullable=False
    )
    sync_next_retry_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )

    # Liveness heartbeat for an in-flight sync. A running sync bumps this
    # every ``kb_sync_heartbeat_interval_secs`` so the stale sweeper can
    # tell a legitimately long-running sync (still heartbeating) apart from
    # an orphaned one whose worker was restarted/OOM-killed (heartbeat went
    # silent). This decouples stale detection from the per-attempt
    # execution timeout (``kb_sync_timeout_secs``), so a large-repo sync is
    # never falsely reset while an orphaned sync recovers within minutes
    # instead of waiting ``timeout + 5 min``. Cleared on success / failure.
    sync_heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )

    # Consecutive stale-reset counter. Incremented each time the stale
    # sweeper finds this source stuck in 'syncing' without a heartbeat
    # (see ``reset_stale_syncs``). Distinct from ``sync_failure_count`` which
    # tracks all failures — this counter specifically tracks the "hangs without
    # heartbeat" pattern.  When it reaches ``kb_sync_stale_quarantine_threshold``
    # the scheduler disables the source so it stops consuming sync capacity
    # indefinitely.  Reset to 0 on a successful sync or via force-resync.
    sync_stale_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default=sa.text("0"), nullable=False
    )

    # Consecutive permanent-error counter. Incremented each time the sync
    # fails with a permanently-unresolvable error (e.g. HTTP 404/410 —
    # the remote path no longer exists). Unlike ``sync_failure_count`` which
    # counts all failures, this counter specifically tracks the "path missing
    # or inaccessible" misconfiguration pattern; a non-permanent failure
    # (timeout, 5xx, rate limit) resets it to 0 so only genuinely
    # *consecutive* 404/410s accumulate. When it reaches
    # ``kb_sync_perm_fail_quarantine_threshold`` the scheduler disables the
    # source so it stops retrying a doomed configuration indefinitely and
    # flags it as "needs reconfiguration". Reset to 0 on a successful sync,
    # via force-resync, or by re-enabling the source.
    sync_perm_fail_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default=sa.text("0"), nullable=False
    )

    # Consecutive partial-page-failure counter. Incremented each time a
    # sync run completes successfully (some documents synced) but at least
    # one page-level fetch failed (e.g. a Notion page that 404s or 400s
    # within a larger database sync). Unlike ``sync_perm_fail_count`` this
    # counter fires even when the overall sync "succeeds" — tracking the
    # "some references are stale" pattern that never triggers a full-source
    # failure. When it reaches ``kb_sync_partial_fail_alert_threshold`` the
    # scheduler emits a one-shot alert so the operator can prune stale page
    # references without the source being quarantined. Reset to 0 on a
    # clean sync (zero page-level errors), a full sync failure (the streak
    # is interrupted), or a forced resync.
    sync_partial_fail_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default=sa.text("0"), nullable=False
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


class KBActivityLog(Base):
    """Activity log entry for a knowledge base (syncs, documents, embeddings)."""

    __tablename__ = "kb_activity_logs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    knowledge_base_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("knowledge_bases.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Activity type: sync_started | sync_completed | sync_failed | sync_alert |
    #                 sync_quarantined | sync_config_error | force_resync |
    #                 document_added | document_updated | document_deleted |
    #                 embedding_started | embedding_completed |
    #                 embedding_failed | reprocess_triggered
    event: Mapped[str] = mapped_column(String(50), nullable=False)
    details: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Optional references
    source_type: Mapped[str | None] = mapped_column(String(30), nullable=True)
    document_count: Mapped[int | None] = mapped_column(Integer, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), index=True
    )


class KnowledgeDocument(Base):
    """A single document within a knowledge base."""

    __tablename__ = "knowledge_documents"
    # ``knowledge_base_id`` is indexed inline. ``status`` supports the document
    # status breakdown. (The composite ``(status, updated_at)`` index used by
    # the stuck-document sweeper is a separate concurrent/idempotent artifact
    # bootstrapped in ``app/core/migrations.py::_ensure_schema_extras``.)
    __table_args__ = (Index("ix_knowledge_documents_status", "status"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    knowledge_base_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("knowledge_bases.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Source info
    # ``Text`` (unbounded) rather than a capped VARCHAR — same rationale as
    # ``source_url`` below: ``filename`` is populated from many sync
    # sources (Notion page title, Microsoft Graph document name, GitHub blob
    # path, website crawl URL) whose values have no predictable upper bound. A
    # long Notion page title overflowed the previous VARCHAR(500) and 500'd KB
    # sync with StringDataRightTruncationError. Any fixed cap just re-opens the
    # same bug for the next long-name source, and Postgres ``text`` has no
    # storage/perf penalty vs ``varchar``.
    filename: Mapped[str] = mapped_column(Text, nullable=False)
    source_type: Mapped[str] = mapped_column(
        String(30), nullable=False
    )  # upload | url | text | markdown
    # ``Text`` (unbounded) rather than a capped VARCHAR: ``source_url`` is
    # populated from many sync sources (Notion, Microsoft Graph web_url,
    # GitHub, Drata, website crawl) whose URLs have no predictable upper
    # bound — a long Notion page title (slug + page UUID) already overflowed
    # the previous VARCHAR(500) and 500'd KB sync with
    # StringDataRightTruncationError. Any fixed cap just re-opens the
    # same class of bug for the next long-URL source, and Postgres ``text``
    # has no storage/perf penalty vs ``varchar``. The value is also a stable
    # upsert key (see kb_sync._upsert_document), so we must NOT rewrite it to
    # a shorter form — widening the column is the correct fix.
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The ``KBSyncSource`` whose sync imported the document: a sync's
    # full-refresh sweep and a force re-sync delete only their own source's
    # documents, never those another source of the same type imported into
    # the knowledge base. NULL when no sync source owns it (upload, one-shot
    # import, a document synced before the column existed). No foreign key on
    # purpose (migration ``k5351kbdocsrc01``): a removed source leaves its id
    # behind, and ``kb_sync.owned_sync_documents`` reads it as unowned.
    sync_source_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )

    # Content (stored for re-processing)
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Processing state
    status: Mapped[str] = mapped_column(
        String(20), default="pending"
    )  # pending | processing | ready | error
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Automatic embedding-retry bookkeeping (self-healing for ``error`` docs).
    #
    # When a document fails to embed for a *retryable* reason (provider
    # misconfigured / down, transient network error, empty vectors), the
    # processor schedules the next automatic retry at ``embedding_next_retry_at``
    # with exponential backoff (``base * 2^embedding_retry_count``) + jitter,
    # and the autonomous ``recover_failed_documents`` sweeper re-enqueues it
    # once that timestamp is due. Structural/permanent failures (no extractable
    # text, no content, KB deleted) leave ``embedding_next_retry_at`` NULL so
    # the doc is never auto-retried. Both fields are reset on a successful embed
    # and on any manual reprocess.
    embedding_retry_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default=sa.text("0"), nullable=False
    )
    embedding_next_retry_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )

    # Metadata
    extra_metadata: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # What the figure pass of a PDF found, bound to the content it was made
    # for (``kb_figures``): its status, a platform-authored note, the page
    # descriptions with where each page's text ends, and, while a pass is due,
    # the object-storage key of the PDF kept for it. ``None`` for every other
    # document.
    figure_reading: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # The person the document was last added or changed for: recorded
    # when its embedding is refused or run for a known person, so re-embedding
    # it later - which binds no one - still checks that person's LLM provider
    # exclusions, even in a company-managed knowledge base, which has
    # no owner to fall back to. NULL when no person was known.
    added_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )
    # Refreshed by a ``BEFORE UPDATE`` Postgres trigger so we always know
    # when a row was last touched — used by the autonomous scheduler to
    # detect documents stuck in ``pending``/``processing`` whose SAQ
    # embedding job was lost (Redis blip, worker crash, SKIP-LOCKED gap).
    # The ``onupdate`` hook mirrors the trigger so ORM-driven flushes
    # also bump the value if the trigger ever fires later (e.g. inside
    # a test fake without the trigger installed).
    #
    # ``clock_timestamp()`` (NOT ``now()``) for ``server_default``: the
    # column DEFAULT is only consulted for raw SQL INSERTs that omit
    # ``updated_at`` (ORM-driven inserts use the Python ``default=``
    # lambda above which already gives wall-clock time), but a raw
    # INSERT inside a long-running transaction would otherwise get the
    # transaction's *start* time, which on a slow sync can already be
    # older than the autonomous sweeper's pending cutoff at commit.
    # ``clock_timestamp()`` returns the actual wall-clock at INSERT.
    # Keeps the column DEFAULT consistent with the matching BEFORE
    # UPDATE trigger in migration ``v2w3x4y5z6a7``.
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        server_default=sa.text("clock_timestamp()"),
    )


class KBExportPackage(Base):
    """A generated knowledge-base export package stored in object storage.

    One logical export (``export_id``) can produce multiple parts for large KBs.
    Each part maps to one ZIP object in S3/MinIO and expires after a TTL.
    """

    __tablename__ = "kb_export_packages"
    __table_args__ = (
        Index("ix_kb_export_packages_kb", "knowledge_base_id"),
        Index("ix_kb_export_packages_user", "user_id"),
        Index("ix_kb_export_packages_export", "export_id"),
        Index("ix_kb_export_packages_expires", "expires_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    knowledge_base_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("knowledge_bases.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    export_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, default=uuid.uuid4
    )
    part_number: Mapped[int] = mapped_column(Integer, nullable=False)
    total_parts: Mapped[int] = mapped_column(Integer, nullable=False)
    filename: Mapped[str] = mapped_column(Text, nullable=False)
    s3_object_name: Mapped[str] = mapped_column(Text, nullable=False)
    file_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
