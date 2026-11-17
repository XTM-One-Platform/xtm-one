"""add the Concierge experiments (#8259)

``concierge_experiments``: one measured variant of a finding's target, with
its dataset, sandbox copies, runs, sizing and result.

A new table, created only when missing (idempotent). Its only foreign key
points at ``concierge_findings``: its creation follows the lock-safe recipe
(``terminate_lock_holders`` + ``set_lock_timeout``) on that table. The indexes
are built in place on the new, empty table.

Revision ID: c6cnc8254062
Revises: c6cnc8254061
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "c6cnc8254062"
down_revision: str | Sequence[str] | None = "c6cnc8254061"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_FINDINGS = "concierge_findings"
_EXPERIMENTS = "concierge_experiments"


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN (" + ", ".join(f"'{value}'" for value in values) + ")"


# Frozen copies of the model's sets: a migration never imports the model.
_CHANGE_TYPES = (
    "persona",
    "skill",
    "assignment_prompt",
    "flow_repair",
    "flow_optimization",
    "new_flow",
    "agent_to_flow",
)
_EXPERIMENT_STATUSES = ("draft", "running", "completed", "incomplete", "skipped", "stale", "failed")


def _has_table(conn, name: str) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.tables WHERE table_schema = current_schema() AND table_name = :n"
            ),
            {"n": name},
        ).scalar()
    )


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    ]


def _create_experiments(conn) -> None:
    if _has_table(conn, _EXPERIMENTS):
        return
    # The foreign key takes SHARE ROW EXCLUSIVE on the findings table.
    terminate_lock_holders(conn, _FINDINGS)
    set_lock_timeout(conn)
    op.create_table(
        _EXPERIMENTS,
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("finding_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("change_type", sa.String(32), nullable=False),
        sa.Column("target_type", sa.String(16), nullable=False),
        sa.Column("target_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("subject_agent_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("base_text", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("base_version", sa.Integer(), nullable=True),
        sa.Column("variant_text", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("variant", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("variant_summary", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("status_reason", sa.String(32), nullable=True),
        sa.Column("dataset_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("evaluation_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("baseline_copy_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("variant_copy_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "baseline_run_ids",
            postgresql.ARRAY(postgresql.UUID(as_uuid=True)),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column(
            "variant_run_ids",
            postgresql.ARRAY(postgresql.UUID(as_uuid=True)),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column("placebo_comparison_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("sizing", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("result", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("proposal_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("author_agent_id", postgresql.UUID(as_uuid=True), nullable=True),
        *_timestamps(),
        sa.Column("launched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["finding_id"], [f"{_FINDINGS}.id"], ondelete="CASCADE"),
        sa.CheckConstraint(_in("status", _EXPERIMENT_STATUSES), name="ck_concierge_experiments_status_valid"),
        sa.CheckConstraint(_in("change_type", _CHANGE_TYPES), name="ck_concierge_experiments_change_type_valid"),
        if_not_exists=True,
    )
    op.create_index(
        "ix_concierge_experiments_finding", _EXPERIMENTS, ["finding_id", "created_at"], if_not_exists=True
    )
    op.create_index("ix_concierge_experiments_status", _EXPERIMENTS, ["status"], if_not_exists=True)


def upgrade() -> None:
    _create_experiments(op.get_bind())


def downgrade() -> None:
    conn = op.get_bind()
    if not _has_table(conn, _EXPERIMENTS):
        return
    terminate_lock_holders(conn, _EXPERIMENTS)
    set_lock_timeout(conn)
    op.drop_table(_EXPERIMENTS, if_exists=True)
