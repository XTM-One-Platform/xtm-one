"""add roi_run_records and roi_baseline_overrides (#8990)

ROI storage, slice 2 of issue #8990 (``models/roi.py``, design in
``dev-docs/ctem-fleets.md`` section 5.2): the units each run produced and the
organization's baseline overrides.

New tables, created only when missing (idempotent); their indexes are built
in place on the new, empty tables, so no CONCURRENTLY is needed. The FK-bearing
CREATE TABLE and the downgrade follow the lock-safe recipe
(``terminate_lock_holders`` + ``set_lock_timeout``).

Revision ID: r4oirec8990
Revises: r3015stop01
Create Date: 2026-11-05 21:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "r4oirec8990"
# NOT a constant: must always be main's CURRENT head, re-pointed on every rebase
# (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "r3015stop01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RECORDS = "roi_run_records"
_OVERRIDES = "roi_baseline_overrides"


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
    if not _has_table(conn, _RECORDS):
        terminate_lock_holders(conn, "assignment_runs")
        set_lock_timeout(conn)
        op.create_table(
            _RECORDS,
            sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column(
                "run_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("assignment_runs.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column(
                "agent_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("agents.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("fleet_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("unit", sa.String(64), nullable=False),
            sa.Column("units", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("review_min", sa.Float(), nullable=True),
            sa.Column("llm_cost", sa.Float(), nullable=False, server_default=sa.text("0")),
            sa.Column("duration_s", sa.Float(), nullable=False, server_default=sa.text("0")),
            sa.Column("outcome", sa.String(16), nullable=False, server_default=sa.text("'accepted'")),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("run_id", name="roi_run_records_run_id_key"),
            sa.CheckConstraint(
                "outcome IN ('accepted', 'modified', 'rejected')", name="ck_roi_run_records_outcome_valid"
            ),
            sa.CheckConstraint("units >= 0", name="ck_roi_run_records_units_positive"),
            if_not_exists=True,
        )
        op.create_index("ix_roi_run_records_agent_created", _RECORDS, ["agent_id", "created_at"], if_not_exists=True)
        op.create_index("ix_roi_run_records_fleet_created", _RECORDS, ["fleet_id", "created_at"], if_not_exists=True)
        op.create_index("ix_roi_run_records_created_at", _RECORDS, ["created_at"], if_not_exists=True)
    if not _has_table(conn, _OVERRIDES):
        terminate_lock_holders(conn, "users")
        set_lock_timeout(conn)
        op.create_table(
            _OVERRIDES,
            sa.Column("unit", sa.String(64), nullable=False),
            sa.Column("manual_min_low", sa.Float(), nullable=False),
            sa.Column("manual_min_likely", sa.Float(), nullable=False),
            sa.Column("manual_min_high", sa.Float(), nullable=False),
            sa.Column("review_min", sa.Float(), nullable=False),
            sa.Column("source", sa.String(300), nullable=False, server_default=sa.text("''")),
            sa.Column(
                "updated_by_user_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("users.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
            sa.PrimaryKeyConstraint("unit"),
            sa.CheckConstraint(
                "manual_min_low <= manual_min_likely AND manual_min_likely <= manual_min_high",
                name="ck_roi_baseline_overrides_ordered",
            ),
            sa.CheckConstraint("review_min >= 0", name="ck_roi_baseline_overrides_review_positive"),
            if_not_exists=True,
        )


def downgrade() -> None:
    conn = op.get_bind()
    for table in (_OVERRIDES, _RECORDS):
        if not _has_table(conn, table):
            continue
        terminate_lock_holders(conn, table)
        set_lock_timeout(conn)
        op.drop_table(table, if_exists=True)
