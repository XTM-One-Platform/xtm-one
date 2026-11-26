"""add ctem_webhook_sources: generic signed inbound webhook per trigger path, off by default (#2506)

A fresh table: an existing tenant has no row, so nothing it records changes.

Revision ID: w2506webhooksrc01
Revises: e686spdtol01
Create Date: 2026-11-26 11:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_ddl import set_lock_timeout

revision: str = "w2506webhooksrc01"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "e686spdtol01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    conn = op.get_bind()
    if sa.inspect(conn).has_table("ctem_webhook_sources"):
        return
    # A new table: its foreign key takes a brief lock on users, so the belt alone
    # (no lock holder to end), as dev-docs/reference/migrations-safety.md describes.
    set_lock_timeout(conn)
    op.create_table(
        "ctem_webhook_sources",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("trigger_path", sa.String(40), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("subject_kind", sa.String(20), nullable=False),
        sa.Column("secret_enc", sa.Text(), nullable=False),
        sa.Column("mapping", postgresql.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("secret_rotated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_delivery_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(300), nullable=True),
        sa.Column(
            "created_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_ctem_webhook_sources_trigger_path", "ctem_webhook_sources", ["trigger_path"])


def downgrade() -> None:
    conn = op.get_bind()
    if not sa.inspect(conn).has_table("ctem_webhook_sources"):
        return
    set_lock_timeout(conn)
    op.drop_table("ctem_webhook_sources")
