"""add memory_consolidations, memory_lane_checkpoints and tombstones (#8258)

Platform Concierge, level 5 (Memory Steward). Purely additive: two new
tables, nothing backfilled, no existing column touched, so replicas running
the previous code ignore them.

- ``memory_consolidations``: one row per change a consolidation pass applied
  to one memory lane (agent pool, or one user's pool with an agent), with the
  ids of the entries it flagged superseded, for the user's history and undo.
- ``memory_lane_checkpoints``: one row per lane, the fingerprint of the lane
  at its last pass (an unchanged lane is skipped) and digests of the
  medium-term observations seen so far (promotion).
- ``memory_consolidation_tombstones``: the fingerprints of changes a pass must
  never make again (undone, or a promotion whose entry was deleted).

Their foreign keys take SHARE ROW EXCLUSIVE on ``agents`` and ``users``: the
``set_lock_timeout`` belt makes a contended attempt fail fast (and the
startup runner retry). Each table is probed first, so a re-run does nothing.

Revision ID: c5cnc8254051
Revises: c4cnc8254043
Create Date: 2026-11-03 21:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_ddl import set_lock_timeout

revision: str = "c5cnc8254051"
# The last migration of the nearest lower Concierge level, else main's head;
# re-pointed when a lower level adds one.
down_revision: str | Sequence[str] | None = "c4cnc8254043"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _has_table(conn, name: str) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = current_schema() AND table_name = :n"
            ),
            {"n": name},
        ).scalar()
    )


def _uuid_fk(name: str, table: str, *, nullable: bool) -> sa.Column:
    return sa.Column(
        name,
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey(f"{table}.id", ondelete="CASCADE"),
        nullable=nullable,
    )


def upgrade() -> None:
    conn = op.get_bind()

    if not _has_table(conn, "memory_consolidations"):
        set_lock_timeout(conn)
        op.create_table(
            "memory_consolidations",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            _uuid_fk("agent_id", "agents", nullable=False),
            _uuid_fk("user_id", "users", nullable=True),
            sa.Column("operation", sa.String(20), nullable=False),
            sa.Column("reason", sa.String(30), nullable=False),
            sa.Column(
                "before_entry_ids",
                postgresql.JSONB(),
                nullable=False,
                server_default=sa.text("'[]'::jsonb"),
            ),
            sa.Column("after_entry_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("fingerprint", sa.String(64), nullable=False),
            sa.Column(
                "applied_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.Column("undone_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index(
            "ix_memory_consolidations_lane",
            "memory_consolidations",
            ["agent_id", "user_id", "applied_at"],
        )
        op.create_index(
            "ix_memory_consolidations_user_applied",
            "memory_consolidations",
            ["user_id", "applied_at"],
            postgresql_where=sa.text("user_id IS NOT NULL"),
        )
        op.create_index(
            "ix_memory_consolidations_before",
            "memory_consolidations",
            ["before_entry_ids"],
            postgresql_using="gin",
        )

    if not _has_table(conn, "memory_lane_checkpoints"):
        set_lock_timeout(conn)
        op.create_table(
            "memory_lane_checkpoints",
            sa.Column("lane_key", sa.String(80), primary_key=True),
            _uuid_fk("agent_id", "agents", nullable=False),
            _uuid_fk("user_id", "users", nullable=True),
            sa.Column("fingerprint", sa.String(64), nullable=False),
            sa.Column(
                "observations",
                postgresql.JSONB(),
                nullable=False,
                server_default=sa.text("'{}'::jsonb"),
            ),
            sa.Column("cursor", sa.Integer(), nullable=False, server_default="0"),
            sa.Column(
                "checked_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
        )
        op.create_index(
            "ix_memory_lane_checkpoints_agent_id", "memory_lane_checkpoints", ["agent_id"]
        )
        op.create_index(
            "ix_memory_lane_checkpoints_user_id", "memory_lane_checkpoints", ["user_id"]
        )


    if not _has_table(conn, "memory_consolidation_tombstones"):
        set_lock_timeout(conn)
        op.create_table(
            "memory_consolidation_tombstones",
            sa.Column("lane_key", sa.String(80), primary_key=True),
            sa.Column("fingerprint", sa.String(64), primary_key=True),
            _uuid_fk("agent_id", "agents", nullable=False),
            _uuid_fk("user_id", "users", nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
        )
        op.create_index(
            "ix_memory_consolidation_tombstones_agent_id",
            "memory_consolidation_tombstones",
            ["agent_id"],
        )
        op.create_index(
            "ix_memory_consolidation_tombstones_user_id",
            "memory_consolidation_tombstones",
            ["user_id"],
        )


def downgrade() -> None:
    conn = op.get_bind()
    set_lock_timeout(conn)
    op.execute("DROP TABLE IF EXISTS memory_consolidation_tombstones")
    op.execute("DROP TABLE IF EXISTS memory_lane_checkpoints")
    op.execute("DROP TABLE IF EXISTS memory_consolidations")
