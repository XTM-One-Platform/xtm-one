"""organization_context_syncs: scheduled imports of profile sections (#11913)

The import of one profile section from one platform (#11780, #11886), saved
with its source, filters, batch type and removal policy so the scheduler tick
runs it again (``services/organization_context_sync.py``). Section ``all`` is
the global schedule that runs every other enabled sync in order.

Lock safety: a NEW, empty table, created with ``if_not_exists`` after an
idempotency probe; its index is built in place within the transaction (no
CONCURRENTLY needed on an empty table). No existing table is altered.
Reversible: ``downgrade`` drops the table.

Revision ID: o11913sync01
Revises: w11893nhrcpt01
Create Date: 2026-11-14 19:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_ddl import set_lock_timeout

revision: str = "o11913sync01"
# NOT a constant: must always be main's CURRENT head, re-pointed on every rebase
# (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | None = "w11893nhrcpt01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "organization_context_syncs"
_INDEX = "ix_organization_context_syncs_due"


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
        sa.Column("section", sa.String(32), nullable=False),
        sa.Column("source", sa.String(16), nullable=False, server_default=sa.text("''")),
        sa.Column("filters", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("batch_type", sa.String(64), nullable=False, server_default=sa.text("''")),
        sa.Column("remove_gone", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("frequency", sa.String(16), nullable=False),
        sa.Column("hour", sa.Integer(), nullable=False, server_default=sa.text("6")),
        sa.Column("minute", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("weekday", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("timezone", sa.String(64), nullable=False, server_default=sa.text("'UTC'")),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("paused_reason", sa.String(500), nullable=False, server_default=sa.text("''")),
        sa.Column("owner_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_result", postgresql.JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.CheckConstraint(
            "frequency IN ('hourly', 'daily', 'weekly')", name="ck_organization_context_syncs_frequency_valid"
        ),
        if_not_exists=True,
    )
    # A new, empty table: the index is built in place within the transaction.
    op.execute(sa.text(f"CREATE INDEX IF NOT EXISTS {_INDEX} ON {_TABLE} (enabled, next_run_at)"))


def downgrade() -> None:
    op.execute(sa.text(f"DROP TABLE IF EXISTS {_TABLE}"))
