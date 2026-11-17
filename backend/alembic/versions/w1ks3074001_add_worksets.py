"""add workset tables (worksets, workset_items)

Purely additive: two new tables, no change to any existing column's type or
nullability, so old replicas during a rolling deploy simply ignore two tables
they do not know about. Nothing is backfilled - there is no prior state that
could be interpreted as a workset.

The full v1 column set lands in ONE migration on purpose, including the parts
whose behaviour is not implemented yet (`cluster_id` / `corroboration_count`,
`watermark_at`, `revision`, the progress and ETA bookkeeping). Adding a column
later is a cheap migration, but changing what a stored `priority_score` MEANS
once scores exist is not: a score computed without a corroboration component
cannot be compared with one computed with it, and there is no backfill that
recovers the difference. So the shape is fixed now and filled in as the
features land.

Content and vectors are NOT here: they live in a system-managed KnowledgeBase
per workset (`worksets.knowledge_base_id`), which is what lets chunking,
embedding, hybrid search and the oversized-document guard be reused unchanged
rather than reimplemented against a second corpus store.

Revision ID: w1ks3074001
Revises: a2a1srv0001
Create Date: 2026-08-25 01:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_ddl import set_lock_timeout

revision: str = "w1ks3074001"
down_revision: str | Sequence[str] | None = "a2a1srv0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _has_table(conn, name: str) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = current_schema() AND table_name = :n"
            ),
            {"n": name},
        ).scalar()
    )


def upgrade() -> None:
    conn = op.get_bind()

    # Idempotency probe: fresh installs run create_all + stamp head (this body
    # never executes), and the migrations CI job re-runs the revision against
    # an already-upgraded database. Both must be no-ops rather than errors.
    #
    # No `terminate_lock_holders` here, and the indexes below are built
    # non-concurrently on purpose: nothing else knows a table created in this
    # same transaction, so nothing can hold a lock on it or queue behind these
    # builds. `CREATE INDEX CONCURRENTLY` would in fact be wrong here — it
    # cannot run inside the transaction that just created the table, and it
    # exists to avoid blocking writers that, for a brand-new table, do not
    # exist. The foreign keys to existing tables (`users`, `agents`,
    # `knowledge_bases`, `knowledge_documents`, `file_attachments`) are the
    # exception: each takes a SHARE ROW EXCLUSIVE lock on the table it
    # references, so the CREATE TABLE runs under the lock_timeout belt - a
    # contended attempt fails fast (55P03, retried by the startup runner)
    # instead of queueing every writer of those tables behind it.
    if not _has_table(conn, "worksets"):
        set_lock_timeout(conn)
        op.create_table(
            "worksets",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "user_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("users.id", ondelete="CASCADE"),
                nullable=True,
            ),
            sa.Column(
                "is_company_managed",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
            sa.Column("key", sa.String(200), nullable=False),
            sa.Column("label", sa.Text(), nullable=True),
            sa.Column(
                "scope", sa.String(10), nullable=False, server_default="run"
            ),
            sa.Column("scope_ref", sa.String(64), nullable=True),
            sa.Column(
                "owner_agent_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("agents.id", ondelete="SET NULL"),
                nullable=True,
            ),
            # SET NULL, not CASCADE: losing the system KB must not delete the
            # state layer, which still holds every summary and priority.
            sa.Column(
                "knowledge_base_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("knowledge_bases.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column(
                "status", sa.String(20), nullable=False, server_default="active"
            ),
            # Progress accounting. `declared_total` is a CLAIM (what the caller
            # announced), never reconciled with `item_count` behind anyone's
            # back; `sealed_at` is what makes "complete" meaningful.
            sa.Column("declared_total", sa.Integer(), nullable=True),
            sa.Column("declared_total_source", sa.Text(), nullable=True),
            sa.Column("sealed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "last_progress_at", sa.DateTime(timezone=True), nullable=True
            ),
            # Sliding-window ingestion high-water mark; advances only on a
            # sealed workset so a failed run never skips a window.
            sa.Column("watermark_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "processing", sa.String(10), nullable=False, server_default="auto"
            ),
            sa.Column(
                "processor_agent_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("agents.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column(
                "workers", sa.Integer(), nullable=False, server_default="1"
            ),
            sa.Column("llm_config", postgresql.JSON(), nullable=True),
            sa.Column(
                "max_items", sa.Integer(), nullable=False, server_default="500"
            ),
            sa.Column(
                "max_content_chars",
                sa.Integer(),
                nullable=False,
                server_default="20000000",
            ),
            sa.Column(
                "item_count", sa.Integer(), nullable=False, server_default="0"
            ),
            sa.Column(
                "evicted_count", sa.Integer(), nullable=False, server_default="0"
            ),
            sa.Column(
                "content_chars", sa.Integer(), nullable=False, server_default="0"
            ),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("user_id", "key", name="uq_worksets_user_key"),
        )
        op.create_index(
            "ix_worksets_user_status", "worksets", ["user_id", "status"]
        )
        op.create_index(
            "ix_worksets_scope_ref", "worksets", ["scope", "scope_ref"]
        )
        op.create_index(
            "ix_worksets_company_managed", "worksets", ["is_company_managed"]
        )
        op.create_index("ix_worksets_expires_at", "worksets", ["expires_at"])

    if not _has_table(conn, "workset_items"):
        set_lock_timeout(conn)
        op.create_table(
            "workset_items",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "workset_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("worksets.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("external_ref", sa.String(200), nullable=True),
            sa.Column("title", sa.Text(), nullable=False),
            sa.Column(
                "source_type",
                sa.String(20),
                nullable=False,
                server_default="inline",
            ),
            sa.Column("source_url", sa.Text(), nullable=True),
            # The document's OWN date, which is what a sliding window filters
            # on; `created_at` would re-date the corpus on every re-ingestion.
            sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=True),
            # SET NULL on both: losing the source must leave the analysis
            # (summary, priority) readable rather than deleting it too.
            sa.Column(
                "knowledge_document_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("knowledge_documents.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column(
                "file_attachment_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("file_attachments.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column(
                "document_is_foreign",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
            sa.Column("content_hash", sa.String(64), nullable=True),
            sa.Column(
                "content_chars", sa.Integer(), nullable=False, server_default="0"
            ),
            sa.Column(
                "chunk_count", sa.Integer(), nullable=False, server_default="0"
            ),
            sa.Column(
                "revision", sa.Integer(), nullable=False, server_default="1"
            ),
            sa.Column("summary", sa.Text(), nullable=True),
            sa.Column("summary_source", sa.String(10), nullable=True),
            sa.Column("priority_score", sa.Integer(), nullable=True),
            sa.Column("priority_reason", sa.Text(), nullable=True),
            sa.Column("priority_source", sa.String(10), nullable=True),
            sa.Column("priority_components", postgresql.JSON(), nullable=True),
            sa.Column(
                "status", sa.String(20), nullable=False, server_default="pending"
            ),
            sa.Column(
                "pinned", sa.Boolean(), nullable=False, server_default=sa.false()
            ),
            sa.Column("cluster_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column(
                "duplicate_of",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("workset_items.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column(
                "corroboration_count",
                sa.Integer(),
                nullable=False,
                server_default="0",
            ),
            sa.Column("tags", postgresql.JSON(), nullable=True),
            sa.Column("extra_metadata", postgresql.JSON(), nullable=True),
            sa.Column(
                "claimed_by_agent_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("agents.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "processing_started_at",
                sa.DateTime(timezone=True),
                nullable=True,
            ),
            sa.Column(
                "processing_completed_at",
                sa.DateTime(timezone=True),
                nullable=True,
            ),
            sa.Column(
                "attempt_count", sa.Integer(), nullable=False, server_default="0"
            ),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            # Idempotent ingestion. NULLs are distinct in Postgres, so items
            # from sources with no stable id never collide with each other.
            sa.UniqueConstraint(
                "workset_id", "external_ref", name="uq_workset_items_ref"
            ),
        )
        op.create_index(
            "ix_workset_items_workset_status",
            "workset_items",
            ["workset_id", "status"],
        )
        op.create_index(
            "ix_workset_items_workset_priority",
            "workset_items",
            ["workset_id", "priority_score"],
        )
        op.create_index(
            "ix_workset_items_workset_occurred",
            "workset_items",
            ["workset_id", "occurred_at"],
        )
        op.create_index(
            "ix_workset_items_cluster",
            "workset_items",
            ["workset_id", "cluster_id"],
        )
        op.create_index(
            "ix_workset_items_document",
            "workset_items",
            ["knowledge_document_id"],
        )


def downgrade() -> None:
    conn = op.get_bind()

    # Children first: `workset_items` holds an FK to `worksets` (and a
    # self-referential one), so dropping in the other order would fail.
    # Dropping a table removes its foreign key triggers from the tables they
    # reference: the belt makes a busy one fail the downgrade fast.
    if _has_table(conn, "workset_items"):
        set_lock_timeout(conn)
        op.drop_table("workset_items")
    if _has_table(conn, "worksets"):
        set_lock_timeout(conn)
        op.drop_table("worksets")
