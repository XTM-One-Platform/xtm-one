"""roi_run_records: nullable outcome and review columns (#9310)

Outcome review, slice O3 of ``dev-docs/roi-outcomes.md`` (section 3.3): a
record is written unreviewed (``outcome`` NULL) and a person marks it
accepted, modified or rejected. Existing rows keep ``accepted`` (they were
written before reviews existed, so the CTEM figures do not move).

Dropping NOT NULL and the server default is a catalog-only change (no table
rewrite); the three new columns are nullable without a default (no rewrite).
The CHECK constraint stays: a NULL outcome passes it. Every lock-taking step
follows the lock-safe recipe (``terminate_lock_holders`` + ``set_lock_timeout``).

Revision ID: r9310roireview1
Revises: r9285roidecl1
Create Date: 2026-11-06 16:40:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "r9310roireview1"
# NOT a constant: must always be main's CURRENT head, re-pointed on every rebase
# (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "r9285roidecl1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RECORDS = "roi_run_records"


def _has_column(conn, table: str, column: str) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = :t AND column_name = :c"
            ),
            {"t": table, "c": column},
        ).scalar()
    )


def upgrade() -> None:
    conn = op.get_bind()
    terminate_lock_holders(conn, _RECORDS)
    set_lock_timeout(conn)
    op.alter_column(_RECORDS, "outcome", existing_type=sa.String(16), nullable=True, server_default=None)
    if not _has_column(conn, _RECORDS, "reviewed_by_user_id"):
        terminate_lock_holders(conn, "users")
        set_lock_timeout(conn)
        op.add_column(
            _RECORDS,
            sa.Column(
                "reviewed_by_user_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("users.id", ondelete="SET NULL"),
                nullable=True,
            ),
        )
    if not _has_column(conn, _RECORDS, "reviewed_at"):
        op.add_column(_RECORDS, sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True))
    if not _has_column(conn, _RECORDS, "review_note"):
        op.add_column(_RECORDS, sa.Column("review_note", sa.String(500), nullable=True))


def downgrade() -> None:
    conn = op.get_bind()
    terminate_lock_holders(conn, _RECORDS)
    set_lock_timeout(conn)
    for column in ("review_note", "reviewed_at", "reviewed_by_user_id"):
        if _has_column(conn, _RECORDS, column):
            op.drop_column(_RECORDS, column)
    op.execute("UPDATE roi_run_records SET outcome = 'accepted' WHERE outcome IS NULL")
    op.alter_column(
        _RECORDS, "outcome", existing_type=sa.String(16), nullable=False, server_default=sa.text("'accepted'")
    )
