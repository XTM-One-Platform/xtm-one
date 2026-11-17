"""add the Concierge findings (#8259)

``concierge_findings``: what the Quality Analyst diagnosed, one row per
pattern on a target (``fingerprint`` unique), kept across cycles so a known
finding is never reported twice.

A new table with no foreign key, created only when missing (idempotent); its
indexes are built in place on the new, empty table. The downgrade drops it
under the lock-safe recipe (``terminate_lock_holders`` + ``set_lock_timeout``).

Revision ID: c6cnc8254061
Revises: c5cnc8254051
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "c6cnc8254061"
# The last migration of the nearest lower Concierge level, else main's head;
# re-pointed whenever one of them gains a migration (PR stack protocol).
down_revision: str | Sequence[str] | None = "c5cnc8254051"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_FINDINGS = "concierge_findings"


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN (" + ", ".join(f"'{value}'" for value in values) + ")"


# Frozen copies of the model's sets: a migration never imports the model.
_TARGET_TYPES = ("agent", "assignment", "flow")
_PATTERN_KINDS = (
    "failure_cause",
    "tool_errors",
    "negative_feedback",
    "low_criterion",
    "cost_drift",
    "latency",
    "evaluation_weakness",
    "behaviour",
)
_FINDING_STATUSES = ("open", "experimenting", "proposed", "resolved", "dismissed")
def _has_table(conn, name: str) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.tables WHERE table_schema = current_schema() AND table_name = :n"
            ),
            {"n": name},
        ).scalar()
    )


def _create_findings(conn) -> None:
    if _has_table(conn, _FINDINGS):
        return
    op.create_table(
        _FINDINGS,
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("target_type", sa.String(16), nullable=False),
        sa.Column("target_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("pattern_kind", sa.String(32), nullable=False),
        sa.Column("pattern_key", sa.String(128), nullable=False, server_default=sa.text("''")),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("hypothesis", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("suggested_experiment", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("evidence", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("content_evidence", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("occurrences", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("author_agent_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("proposal_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("reported_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(_in("status", _FINDING_STATUSES), name="ck_concierge_findings_status_valid"),
        sa.CheckConstraint(_in("target_type", _TARGET_TYPES), name="ck_concierge_findings_target_type_valid"),
        sa.CheckConstraint(_in("pattern_kind", _PATTERN_KINDS), name="ck_concierge_findings_pattern_kind_valid"),
        if_not_exists=True,
    )
    op.create_index("uq_concierge_findings_fingerprint", _FINDINGS, ["fingerprint"], unique=True, if_not_exists=True)
    op.create_index(
        "ix_concierge_findings_target", _FINDINGS, ["target_type", "target_id", "status"], if_not_exists=True
    )
    op.create_index("ix_concierge_findings_last_seen", _FINDINGS, ["last_seen_at"], if_not_exists=True)


def upgrade() -> None:
    _create_findings(op.get_bind())


def downgrade() -> None:
    conn = op.get_bind()
    if not _has_table(conn, _FINDINGS):
        return
    terminate_lock_holders(conn, _FINDINGS)
    set_lock_timeout(conn)
    op.drop_table(_FINDINGS, if_exists=True)
