"""add ctem_signal_sources: platform signal sources, off by default (#2434)

The OpenCTI and OpenAEV sources of the CTEM trigger paths become platform
objects with their own enabled flag, run-as and provisioning state, never held
by a fleet assignment. A fresh table: every source starts off; the data move
of the sources enabled assignments provision today is a later slice of #2434.

Revision ID: s2434sigsrc01
Revises: p2202fleetrd01
Create Date: 2026-11-26 01:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_ddl import set_lock_timeout

revision: str = "s2434sigsrc01"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "p2202fleetrd01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    conn = op.get_bind()
    if sa.inspect(conn).has_table("ctem_signal_sources"):
        return
    # A new table: its foreign keys take a brief lock on integrations and users, so the belt
    # alone (no lock holder to end), as dev-docs/reference/migrations-safety.md describes.
    set_lock_timeout(conn)
    op.create_table(
        "ctem_signal_sources",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("provider", sa.String(40), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column(
            "integration_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("integrations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "run_as_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("state", postgresql.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint(
            "provider", "kind", "integration_id", name="uq_ctem_signal_sources_provider_kind_integration"
        ),
    )


def downgrade() -> None:
    conn = op.get_bind()
    if not sa.inspect(conn).has_table("ctem_signal_sources"):
        return
    set_lock_timeout(conn)
    op.drop_table("ctem_signal_sources")
