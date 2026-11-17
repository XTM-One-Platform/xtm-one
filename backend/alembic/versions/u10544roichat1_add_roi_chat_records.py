"""roi_chat_records: finished chat deliverables, counted automatically on the low side (#10544)

Slice S2 of ``dev-docs/roi-outcomes.md`` section 11: one row per presentation,
report, video or generated document an agent built in a conversation, keyed on
the deliverable's own id (``record_key``), with its size, the use signals seen,
its last edit and deletion. Whether a row counts and how much is computed on
read by ``services/roi_chat.py``. A new table, nothing rewritten; the lock-safe
recipe (``terminate_lock_holders`` + ``set_lock_timeout``) covers the ``users``
FKs. Fresh installs run ``create_all`` + ``stamp head``: the body never
executes there.

Revision ID: u10544roichat1
Revises: t10469fltconf1
Create Date: 2026-11-10 01:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "u10544roichat1"
# NOT a constant: must always be main's CURRENT head, re-pointed on every rebase
# (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "t10469fltconf1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "roi_chat_records"


def _has_table(conn, name: str) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.tables WHERE table_schema = current_schema() AND table_name = :n"
            ),
            {"n": name},
        ).scalar()
    )


def upgrade() -> None:
    conn = op.get_bind()
    if _has_table(conn, _TABLE):
        return
    terminate_lock_holders(conn, "users")
    set_lock_timeout(conn)
    op.create_table(
        _TABLE,
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("record_key", sa.String(120), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("deliverable_id", sa.String(64), nullable=False),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("title", sa.String(300), nullable=False, server_default=sa.text("''")),
        sa.Column("size", sa.Float(), nullable=False, server_default=sa.text("0")),
        sa.Column("signals", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("first_signal_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_edit_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("adjusted_min", sa.Float(), nullable=True),
        sa.Column(
            "adjusted_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("adjusted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("backfilled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("record_key", name="uq_roi_chat_records_record_key"),
        sa.CheckConstraint(
            "kind IN ('presentation', 'report', 'video', 'file')", name="ck_roi_chat_records_kind_valid"
        ),
        sa.CheckConstraint("size >= 0", name="ck_roi_chat_records_size_positive"),
        sa.CheckConstraint("adjusted_min IS NULL OR adjusted_min >= 0", name="ck_roi_chat_records_adjusted_positive"),
        if_not_exists=True,
    )
    op.execute(f"CREATE INDEX IF NOT EXISTS ix_roi_chat_records_user_created ON {_TABLE} (user_id, created_at)")
    op.execute(f"CREATE INDEX IF NOT EXISTS ix_roi_chat_records_agent_created ON {_TABLE} (agent_id, created_at)")


def downgrade() -> None:
    conn = op.get_bind()
    if _has_table(conn, _TABLE):
        terminate_lock_holders(conn, _TABLE)
        set_lock_timeout(conn)
        op.drop_table(_TABLE, if_exists=True)
