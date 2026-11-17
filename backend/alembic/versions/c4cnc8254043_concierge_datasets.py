"""datasets the Concierge builds from production (#8257)

``datasets.concierge_built`` marks a dataset the Concierge built, and
``concierge_dataset_entry_sources`` keeps where each of its entries came from
and the tool calls that source recorded, so an opt-out, a revoked opt-in or a
narrowed scope removes the entries derived from it (PRD 6.7, decisions B15, B16).

``datasets`` is existing: the column follows the lock-safe recipe. The new
table references ``dataset_entries`` and ``datasets`` (SHARE ROW EXCLUSIVE on
each, writers only), so its CREATE runs under ``set_lock_timeout``.

Revision ID: c4cnc8254043
Revises: c4cnc8254042
Create Date: 2026-11-03 23:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_ddl import create_index_in_place, set_lock_timeout, terminate_lock_holders

revision: str = "c4cnc8254043"
down_revision: str | Sequence[str] | None = "c4cnc8254042"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "concierge_dataset_entry_sources"


def _has_column(conn) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.columns WHERE table_schema = current_schema() "
                "AND table_name = 'datasets' AND column_name = 'concierge_built'"
            )
        ).scalar()
    )


def _has_table(conn) -> bool:
    return bool(conn.execute(sa.text("SELECT to_regclass(:t)"), {"t": _TABLE}).scalar())


def _has_table_column(conn, column: str) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.columns WHERE table_schema = current_schema() "
                "AND table_name = :t AND column_name = :c"
            ),
            {"t": _TABLE, "c": column},
        ).scalar()
    )


def _uuid_array(name: str) -> sa.Column:
    return sa.Column(name, postgresql.ARRAY(postgresql.UUID(as_uuid=True)), nullable=False, server_default="{}")


def upgrade() -> None:
    conn = op.get_bind()
    if not _has_column(conn):
        terminate_lock_holders(conn, "datasets")
        set_lock_timeout(conn)
        op.add_column(
            "datasets", sa.Column("concierge_built", sa.Boolean(), nullable=False, server_default=sa.false())
        )
    if _has_table(conn):
        # A table this revision created before it gained the node agents and
        # flows and the rater index: completed in place.
        missing = [c for c in ("source_node_agent_ids", "source_node_flow_ids") if not _has_table_column(conn, c)]
        if missing:
            terminate_lock_holders(conn, _TABLE)
            set_lock_timeout(conn)
            for column in missing:
                op.add_column(_TABLE, _uuid_array(column))
        with op.get_context().autocommit_block():
            create_index_in_place(conn, "ix_concierge_entry_sources_rater", _TABLE, "(source_rater_id)")
            create_index_in_place(
                conn, "ix_concierge_entry_sources_node_agents", _TABLE, "USING gin (source_node_agent_ids)"
            )
            create_index_in_place(
                conn, "ix_concierge_entry_sources_node_flows", _TABLE, "USING gin (source_node_flow_ids)"
            )
        return
    set_lock_timeout(conn)
    op.create_table(
        _TABLE,
        sa.Column(
            "entry_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("dataset_entries.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "dataset_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("datasets.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("case_kind", sa.String(20), nullable=False),
        sa.Column("source_kind", sa.String(20), nullable=False),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_trace_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("source_rating_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("source_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("source_rater_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("source_agent_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("source_flow_id", postgresql.UUID(as_uuid=True), nullable=True),
        _uuid_array("source_node_agent_ids"),
        _uuid_array("source_node_flow_ids"),
        sa.Column("recorded_calls", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("case_kind IN ('to_fix', 'non_regression')", name="ck_concierge_entry_sources_case_kind"),
        sa.CheckConstraint(
            "source_kind IN ('message', 'assignment_run', 'flow_run')", name="ck_concierge_entry_sources_source_kind"
        ),
        sa.UniqueConstraint("dataset_id", "source_kind", "source_id", name="uq_concierge_entry_sources_source"),
    )
    op.create_index("ix_concierge_entry_sources_user", _TABLE, ["source_user_id"])
    op.create_index("ix_concierge_entry_sources_agent", _TABLE, ["source_agent_id"])
    op.create_index("ix_concierge_entry_sources_flow", _TABLE, ["source_flow_id"])
    op.create_index("ix_concierge_entry_sources_rater", _TABLE, ["source_rater_id"])
    op.create_index(
        "ix_concierge_entry_sources_node_agents", _TABLE, ["source_node_agent_ids"], postgresql_using="gin"
    )
    op.create_index("ix_concierge_entry_sources_node_flows", _TABLE, ["source_node_flow_ids"], postgresql_using="gin")


def downgrade() -> None:
    conn = op.get_bind()
    if _has_table(conn):
        set_lock_timeout(conn)
        op.drop_table(_TABLE)
    if _has_column(conn):
        terminate_lock_holders(conn, "datasets")
        set_lock_timeout(conn)
        op.drop_column("datasets", "concierge_built")
