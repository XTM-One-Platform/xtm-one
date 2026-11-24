"""add the snapshots of dashboard data sources refreshed on the server

One new table, ``custom_dashboard_source_snapshots``
(``models/custom_dashboard.py``): what a data source whose ``refreshMode`` is
``server``, ``schedule``, ``event`` or ``manual`` read last, read with its publisher's access, and when
it is read again (``services/dashboard_refresh.py``). Nothing is backfilled: a
snapshot is written when such a source is saved, and read on its due time.

Lock profile: the foreign keys to ``custom_dashboards`` and ``users`` install
triggers there, so their holders are cleared and the lock timeout set before
the guarded ``CREATE TABLE``.

Revision ID: t1930dashsnap01
Revises: s1925gadash01
Create Date: 2026-11-24 21:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "t1930dashsnap01"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "s1925gadash01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "custom_dashboard_source_snapshots"


def _has_table(conn: sa.engine.Connection, name: str) -> bool:
    return bool(
        conn.execute(
            sa.text("SELECT 1 FROM information_schema.tables WHERE table_schema = current_schema() AND table_name = :n"),
            {"n": name},
        ).scalar()
    )


def upgrade() -> None:
    conn = op.get_bind()
    # Idempotency probe: fresh installs run create_all + stamp head, and a crashed replica may retry.
    if _has_table(conn, _TABLE):
        return
    terminate_lock_holders(conn, "custom_dashboards")
    terminate_lock_holders(conn, "users")
    set_lock_timeout(conn)
    op.create_table(
        _TABLE,
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "dashboard_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("custom_dashboards.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("source_id", sa.String(60), nullable=False),
        sa.Column(
            "publisher_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
        ),
        sa.Column("source_key", sa.String(64), nullable=False),
        sa.Column("result", postgresql.JSON(), nullable=True),
        sa.Column("refreshed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("trigger", sa.String(320), nullable=True),
        sa.Column("alert_state", postgresql.JSON(), nullable=True),
        sa.UniqueConstraint("dashboard_id", "source_id", name="uq_custom_dashboard_source_snapshot"),
        if_not_exists=True,
    )
    op.create_index(
        "ix_custom_dashboard_source_snapshots_dashboard_id", _TABLE, ["dashboard_id"], if_not_exists=True
    )
    op.create_index("ix_custom_dashboard_source_snapshots_due_at", _TABLE, ["due_at"], if_not_exists=True)
    op.create_index("ix_custom_dashboard_source_snapshots_trigger", _TABLE, ["trigger"], if_not_exists=True)


def downgrade() -> None:
    conn = op.get_bind()
    if not _has_table(conn, _TABLE):
        return
    set_lock_timeout(conn)
    op.drop_index("ix_custom_dashboard_source_snapshots_trigger", table_name=_TABLE, if_exists=True)
    op.drop_index("ix_custom_dashboard_source_snapshots_due_at", table_name=_TABLE, if_exists=True)
    op.drop_index("ix_custom_dashboard_source_snapshots_dashboard_id", table_name=_TABLE, if_exists=True)
    op.drop_table(_TABLE, if_exists=True)
