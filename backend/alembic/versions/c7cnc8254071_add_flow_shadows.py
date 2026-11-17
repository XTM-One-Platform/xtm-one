"""add flow shadows and their run pairs (#8260)

Platform Concierge, level 7 (Flow Lab): a flow measured on an assignment's
live triggers, then switched over and watched (decision B12). Purely
additive: two new tables, nothing existing changes, no backfill.

**Lock safety.** The new tables cannot be locked by anyone, but their foreign
keys lock the REFERENCED tables (``assignments``, ``flows``, ``users``) in
SHARE ROW EXCLUSIVE mode, which waits behind a transaction that wrote to them.
The DDL runs under ``set_lock_timeout`` so a contended attempt fails fast
(55P03, retried by the startup runner). No ``terminate_lock_holders``: that
lock does not conflict with readers, and the pre-flight would kill every
session that merely read one of those rows (same reasoning as
``w1wsp3314001``). The pair rows reference runs by id without a key, so
``assignment_runs`` and ``flow_runs`` are not locked at all. Indexes are plain
builds on empty tables.

Idempotent: a fresh install runs ``create_all`` + ``stamp head``, and the CI
re-runs the revision on an upgraded database; both are no-ops.

Revision ID: c7cnc8254071
Revises: c6cnc8254062
Create Date: 2026-11-03 18:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_ddl import set_lock_timeout

revision: str = "c7cnc8254071"
# The last migration of the nearest lower Concierge level, else main's head:
# re-pointed whenever a lower level or main gains one.
down_revision: str | Sequence[str] | None = "c6cnc8254062"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SHADOW_STATUSES = (
    "'running', 'held', 'not_held', 'incomplete', 'watching', 'kept', "
    "'rolled_back', 'regression_kept', 'cancelled'"
)


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
    set_lock_timeout(conn)

    if not _has_table(conn, "flow_shadows"):
        op.create_table(
            "flow_shadows",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "assignment_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("assignments.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column(
                "flow_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("flows.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column(
                "production_flow_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("flows.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("proposal_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("purpose", sa.String(16), nullable=False),
            sa.Column("status", sa.String(20), nullable=False),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("cursor_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("switched_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("watch_until", sa.DateTime(timezone=True), nullable=True),
            sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("baseline", postgresql.JSON(), nullable=True),
            sa.Column("verdict", postgresql.JSON(), nullable=True),
            sa.Column("note", sa.String(1000), nullable=True),
            sa.Column("cycle_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column(
                "started_by_user_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("users.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.CheckConstraint("purpose IN ('measure', 'switch_over')", name="ck_flow_shadows_purpose"),
            sa.CheckConstraint(f"status IN ({_SHADOW_STATUSES})", name="ck_flow_shadows_status"),
        )
        op.create_index("ix_flow_shadows_flow_id", "flow_shadows", ["flow_id"])
        op.create_index("ix_flow_shadows_proposal_id", "flow_shadows", ["proposal_id"])
        op.create_index("ix_flow_shadows_status_ends", "flow_shadows", ["status", "ends_at"])
        op.create_index(
            "uq_flow_shadows_open_assignment",
            "flow_shadows",
            ["assignment_id"],
            unique=True,
            postgresql_where=sa.text("status IN ('running', 'watching')"),
        )

    if not _has_table(conn, "flow_shadow_pairs"):
        op.create_table(
            "flow_shadow_pairs",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "shadow_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("flow_shadows.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("assignment_run_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("flow_run_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("status", sa.String(16), nullable=False),
            sa.Column("comparison", postgresql.JSON(), nullable=True),
            sa.Column("intercepted_writes", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("simulated_writes", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("reservation", postgresql.JSON(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint("shadow_id", "assignment_run_id", name="uq_flow_shadow_pairs_run"),
            sa.CheckConstraint(
                "status IN ('pending', 'compared', 'incomplete', 'skipped')", name="ck_flow_shadow_pairs_status"
            ),
        )
        op.create_index(
            "ix_flow_shadow_pairs_shadow_status", "flow_shadow_pairs", ["shadow_id", "status"]
        )
    # Databases that ran an earlier draft of this revision (nullable, no default:
    # metadata only); probed first so a steady-state re-run takes no lock.
    for table, column, kind in (("flow_shadows", "cycle_id", "uuid"), ("flow_shadow_pairs", "reservation", "json")):
        if not _has_column(conn, table, column):
            op.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {kind}")


def downgrade() -> None:
    conn = op.get_bind()
    set_lock_timeout(conn)
    op.execute("DROP TABLE IF EXISTS flow_shadow_pairs")
    op.execute("DROP TABLE IF EXISTS flow_shadows")
