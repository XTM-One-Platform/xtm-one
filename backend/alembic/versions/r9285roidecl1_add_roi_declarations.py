"""add roi_declarations and roi_run_records.work_item_id (#9285)

ROI declarations, slice O2 of ``dev-docs/roi-outcomes.md`` (section 3.2): the
unit an agent, an assignment or a fleet declares and how it is counted
(``per_run``, ``per_output_item``, ``per_work_item``), and the work item a
``per_work_item`` record comes from.

New table created only when missing (idempotent); the nullable column is
added without a default (no rewrite); the unique index on it is built with
``create_index_in_place`` in an autocommit block (``roi_run_records`` is small
and the column is all NULL), never CONCURRENTLY. Every lock-taking step follows
the lock-safe recipe (``terminate_lock_holders`` + ``set_lock_timeout``).

Revision ID: r9285roidecl1
Revises: r9252roiunits1
Create Date: 2026-11-06 16:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_ddl import create_index_in_place, set_lock_timeout, terminate_lock_holders

revision: str = "r9285roidecl1"
# NOT a constant: must always be main's CURRENT head, re-pointed on every rebase
# (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "r9252roiunits1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DECLARATIONS = "roi_declarations"
_RECORDS = "roi_run_records"
_COLUMN = "work_item_id"
_INDEX = "uq_roi_run_records_work_item_id"


def _has_table(conn, name: str) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.tables WHERE table_schema = current_schema() AND table_name = :n"
            ),
            {"n": name},
        ).scalar()
    )


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
    if not _has_table(conn, _DECLARATIONS):
        terminate_lock_holders(conn, "users")
        set_lock_timeout(conn)
        op.create_table(
            _DECLARATIONS,
            sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("subject_type", sa.String(16), nullable=False),
            sa.Column("subject_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("unit", sa.String(64), nullable=False),
            sa.Column("count_mode", sa.String(32), nullable=False),
            sa.Column("count_path", sa.String(200), nullable=True),
            sa.Column("units_per_count", sa.Integer(), nullable=False, server_default=sa.text("1")),
            sa.Column(
                "updated_by_user_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("users.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("subject_type", "subject_id", name="uq_roi_declarations_subject"),
            sa.CheckConstraint(
                "subject_type IN ('agent', 'assignment', 'fleet')", name="ck_roi_declarations_subject_type"
            ),
            sa.CheckConstraint(
                "count_mode IN ('per_run', 'per_output_item', 'per_work_item')",
                name="ck_roi_declarations_count_mode",
            ),
            sa.CheckConstraint("units_per_count >= 1", name="ck_roi_declarations_units_per_count"),
            if_not_exists=True,
        )
    if not _has_column(conn, _RECORDS, _COLUMN):
        terminate_lock_holders(conn, _RECORDS)
        set_lock_timeout(conn)
        op.add_column(_RECORDS, sa.Column(_COLUMN, postgresql.UUID(as_uuid=True), nullable=True))
    with op.get_context().autocommit_block():
        create_index_in_place(conn, _INDEX, _RECORDS, f"({_COLUMN})", unique=True, required=True)


def downgrade() -> None:
    conn = op.get_bind()
    terminate_lock_holders(conn, _RECORDS)
    set_lock_timeout(conn)
    op.execute(f"DROP INDEX IF EXISTS {_INDEX}")
    if _has_column(conn, _RECORDS, _COLUMN):
        op.drop_column(_RECORDS, _COLUMN)
    if _has_table(conn, _DECLARATIONS):
        terminate_lock_holders(conn, _DECLARATIONS)
        set_lock_timeout(conn)
        op.drop_table(_DECLARATIONS, if_exists=True)
