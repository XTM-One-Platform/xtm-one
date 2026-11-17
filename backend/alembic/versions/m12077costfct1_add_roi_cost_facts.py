"""add roi_cost_facts (the AI cost of a closed day, per person, agent, fleet and class)

Slice L2 of issue #12077 (design ``dev-docs/roi-outcomes.md`` sections 12.3
and 12.11): ``roi_ledger.close_cost_days`` writes one row per day, person,
attribution, agent, fleet and class from ``llm_traces``, or from the
``llm_daily_stats`` rollup for a day past the trace retention.

A new table with no foreign key: its CREATE takes no lock another session
holds, and its indexes are built in place on the new, empty table. The
downgrade drops it with the canonical recipe. The rows are written by the
trace maintenance, never by the migration. Fresh installs run
``create_all`` + ``stamp head``: the body never executes there.

Revision ID: m12077costfct1
Revises: l12077ledger1
Create Date: 2026-11-15 16:25:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "m12077costfct1"
# NOT a constant: must always be main's CURRENT head, re-pointed on every rebase
# (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "l12077ledger1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "roi_cost_facts"


def upgrade() -> None:
    conn = op.get_bind()
    if sa.inspect(conn).has_table(_TABLE):
        return
    set_lock_timeout(conn)
    op.create_table(
        _TABLE,
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("attribution", sa.String(20), nullable=True),
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("fleet_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("cost_class", sa.String(16), nullable=False),
        sa.Column("cost", sa.Numeric(16, 8), nullable=False, server_default=sa.text("0")),
        sa.Column("trace_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("built_from", sa.String(8), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("cost_class IN ('automation', 'chat', 'platform')", name="ck_roi_cost_facts_class_valid"),
        sa.CheckConstraint("built_from IN ('traces', 'rollup')", name="ck_roi_cost_facts_built_from_valid"),
        if_not_exists=True,
    )
    op.create_index("ix_roi_cost_facts_day", _TABLE, ["day"], if_not_exists=True)
    op.create_index("ix_roi_cost_facts_user_day", _TABLE, ["user_id", "day"], if_not_exists=True)
    op.create_index("ix_roi_cost_facts_agent_day", _TABLE, ["agent_id", "day"], if_not_exists=True)
    op.create_index("ix_roi_cost_facts_fleet_day", _TABLE, ["fleet_id", "day"], if_not_exists=True)


def downgrade() -> None:
    conn = op.get_bind()
    if not sa.inspect(conn).has_table(_TABLE):
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    op.drop_table(_TABLE)
