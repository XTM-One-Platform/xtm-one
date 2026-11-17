"""add Concierge sandbox copies and their runs (#8255)

Level 2 of the Platform Concierge stack:

* ``agents.is_sandbox`` and ``flows.is_sandbox`` (bool, NOT NULL, default
  false) mark a hidden copy the Concierge experiments on;
* ``sandbox_copies`` links a copy to its source, with what it was made for, the
  recordings it replays and when it may be purged;
* ``sandbox_runs`` keeps each sandbox run's report and its exact count of
  simulated writes.

Safety contract (backend/AGENTS.md > "Database migrations (safety)"):

* Idempotent - every object is probed first and created ``IF NOT EXISTS``.
* Lock-safe - a constant-default NOT NULL column is metadata-only; each altered
  table gets the canonical recipe (probe, ``terminate_lock_holders``,
  ``set_lock_timeout``). The new tables' foreign keys take SHARE ROW EXCLUSIVE
  on ``agents`` and ``flows``: the belt makes a contended attempt fail fast.
* Additive - old replicas during a rolling deploy ignore the columns (their
  default is false) and the tables.
* Reversible - the downgrade drops the tables, then the copies themselves
  (their rows have no meaning without the columns), then the columns.

Revision ID: c2cnc8254021
Revises: c1cnc8254012
Create Date: 2026-11-03 21:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "c2cnc8254021"
down_revision: str | Sequence[str] | None = "c1cnc8254012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_FLAGGED = ("agents", "flows")
_COPIES = "sandbox_copies"
_RUNS = "sandbox_runs"

#: The same definitions as the models (the create_all path).
_INDEXES: tuple[tuple[str, str, list[str]], ...] = (
    ("ix_sandbox_copies_expires_at", _COPIES, ["expires_at"]),
    ("ix_sandbox_copies_source", _COPIES, ["source_type", "source_id"]),
    ("ix_sandbox_runs_copy_created", _RUNS, ["copy_id", "created_at"]),
    ("ix_sandbox_runs_flow_run_id", _RUNS, ["flow_run_id"]),
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


def _has_index(conn, name: str) -> bool:
    return bool(
        conn.execute(
            sa.text("SELECT 1 FROM pg_indexes WHERE schemaname = current_schema() AND indexname = :n"),
            {"n": name},
        ).scalar()
    )


def upgrade() -> None:
    conn = op.get_bind()

    for table in _FLAGGED:
        if not _has_column(conn, table, "is_sandbox"):
            terminate_lock_holders(conn, table)
            set_lock_timeout(conn)
            conn.execute(
                sa.text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS is_sandbox BOOLEAN NOT NULL DEFAULT false")
            )

    if not _has_table(conn, _COPIES):
        set_lock_timeout(conn)
        op.create_table(
            _COPIES,
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("source_type", sa.String(16), nullable=False),
            sa.Column("source_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column(
                "copy_agent_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("agents.id", ondelete="CASCADE"),
                nullable=True,
                unique=True,
            ),
            sa.Column(
                "copy_flow_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("flows.id", ondelete="CASCADE"),
                nullable=True,
                unique=True,
            ),
            sa.Column("created_for", sa.String(255), nullable=False, server_default=""),
            sa.Column("created_by_agent_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("source_trace_ids", postgresql.JSONB(), nullable=True),
            sa.Column("source_run_ids", postgresql.JSONB(), nullable=True),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
            sa.CheckConstraint("source_type IN ('agent', 'flow')", name="ck_sandbox_copies_source_type"),
            sa.CheckConstraint("num_nonnulls(copy_agent_id, copy_flow_id) = 1", name="ck_sandbox_copies_one_copy"),
            if_not_exists=True,
        )

    if not _has_table(conn, _RUNS):
        set_lock_timeout(conn)
        op.create_table(
            _RUNS,
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "copy_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey(f"{_COPIES}.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("case_ref", sa.String(255), nullable=True),
            sa.Column("purpose", sa.String(16), nullable=False, server_default="experiment"),
            sa.Column("status", sa.String(16), nullable=False, server_default="running"),
            sa.Column("flow_run_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("trace_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("report", postgresql.JSONB(), nullable=True),
            sa.Column("simulated_writes", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
            sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
            sa.CheckConstraint("purpose IN ('experiment', 'shadow')", name="ck_sandbox_runs_purpose"),
            sa.CheckConstraint("status IN ('running', 'completed', 'failed')", name="ck_sandbox_runs_status"),
            if_not_exists=True,
        )

    missing = [(name, table, columns) for name, table, columns in _INDEXES if not _has_index(conn, name)]
    if missing:
        # Instant on the tables created above; the belt matters for a table
        # left without an index by an interrupted run.
        set_lock_timeout(conn)
        for name, table, columns in missing:
            op.create_index(name, table, columns, if_not_exists=True)


def downgrade() -> None:
    conn = op.get_bind()

    for table in (_RUNS, _COPIES):
        if _has_table(conn, table):
            set_lock_timeout(conn)
            op.drop_table(table, if_exists=True)

    for table in _FLAGGED:
        if _has_column(conn, table, "is_sandbox"):
            terminate_lock_holders(conn, table)
            set_lock_timeout(conn)
            # A copy is meaningless once the flag is gone: it would surface in
            # every library as an ordinary, ownerless agent or flow.
            conn.execute(sa.text(f"DELETE FROM {table} WHERE is_sandbox"))  # noqa: S608 - constant table names
            conn.execute(sa.text(f"ALTER TABLE {table} DROP COLUMN IF EXISTS is_sandbox"))
