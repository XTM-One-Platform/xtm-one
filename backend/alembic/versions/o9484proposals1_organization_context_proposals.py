"""organization_context_proposals: changes proposed from chat, waiting for review (#9484, S-a2)

``propose_organization_context_change`` (``dev-docs/ctem-fleets.md``
section 6.1) stores a proposal here and never writes the profile: field
changes of the primary or of a tracked organization, or a new tracked
organization. An administrator accepts or rejects it in Settings >
Organization.

Lock safety: a NEW, empty table, created with ``if_not_exists`` after an
idempotency probe; its index is built in place within the transaction (no
CONCURRENTLY needed on an empty table). No existing table is altered.
Reversible: ``downgrade`` drops the table.

Revision ID: o9484proposals1
Revises: o9484aidraft1
Create Date: 2026-11-07 07:45:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_ddl import set_lock_timeout

revision: str = "o9484proposals1"
# NOT a constant: must always be main's CURRENT head, re-pointed on every rebase
# (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | None = "o9484aidraft1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "organization_context_proposals"
_INDEX = "ix_organization_context_proposals_status_created"


def _table_exists(conn) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.tables WHERE table_schema = current_schema() AND table_name = :table"
            ),
            {"table": _TABLE},
        ).scalar()
    )


def upgrade() -> None:
    conn = op.get_bind()
    if _table_exists(conn):
        return
    set_lock_timeout(conn)
    op.create_table(
        _TABLE,
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("proposals", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("based_on_version", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("author_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            ondelete="CASCADE",
            name="fk_organization_context_proposals_organization_id",
        ),
        sa.ForeignKeyConstraint(["author_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["decided_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.CheckConstraint("kind IN ('fields', 'new_tracked')", name="ck_organization_context_proposals_kind_valid"),
        sa.CheckConstraint(
            "status IN ('pending', 'accepted', 'rejected')", name="ck_organization_context_proposals_status_valid"
        ),
        if_not_exists=True,
    )
    # A new, empty table: the index is built in place within the transaction.
    op.execute(sa.text(f"CREATE INDEX IF NOT EXISTS {_INDEX} ON {_TABLE} (status, created_at)"))


def downgrade() -> None:
    op.execute(sa.text(f"DROP TABLE IF EXISTS {_TABLE}"))
