"""add improvement_proposals (#8256)

The Platform Concierge's improvement proposals (#1783, decision B6): one
generic table, of which the persona self-improvement loop (#1044) becomes the
``persona`` type. Purely additive: one new table, its indexes built in the
same transaction (the table is new: nothing can hold a lock on it), no change
to any existing column, so replicas still on the previous version ignore it.
The ``persona_improvements`` rows move in with the next revision.

Lock profile: the one foreign key, to ``agents``, takes a SHARE ROW EXCLUSIVE
lock there, which waits only for a transaction that WROTE ``agents``: the
CREATE runs under the lock_timeout belt alone, as ``n1044impr001`` did
(backend/AGENTS.md > "Database migrations (safety)", rule 5). The downgrade's
DROP takes an ACCESS EXCLUSIVE lock on ``agents``, so it runs the full
pre-flight.

Revision ID: c3cnc8254031
Revises: c2cnc8254021
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "c3cnc8254031"
down_revision: str | Sequence[str] | None = "c2cnc8254021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "improvement_proposals"
_REFERENCED_TABLES = ("agents",)

_TYPES = (
    "persona",
    "skill",
    "assignment_prompt",
    "flow_repair",
    "flow_optimization",
    "new_flow",
    "agent_to_flow",
)
_TARGET_TYPES = ("agent", "skill", "assignment", "flow")
_STATUSES = (
    "pending_approval",
    "rejected",
    "applying",
    "shadow",
    "watching",
    "kept",
    "rolled_back",
    "regression_kept",
    "failed",
)
_OPEN = "status IN ('pending_approval', 'applying', 'shadow', 'watching')"


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN (" + ", ".join(f"'{value}'" for value in values) + ")"


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


def _jsonb(default: str) -> sa.TextClause:
    return sa.text(f"'{default}'::jsonb")


def _create_table() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("proposal_type", sa.String(32), nullable=False),
        sa.Column("target_type", sa.String(16), nullable=False),
        sa.Column("target_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "agent_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agents.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("dedup_key", sa.String(64), nullable=False),
        sa.Column("base_content", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("proposed_content", sa.Text(), nullable=False, server_default=sa.text("''")),
        sa.Column("change", postgresql.JSONB(), nullable=False, server_default=_jsonb("{}")),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("base_version_number", sa.Integer(), nullable=True),
        sa.Column("applied_version_number", sa.Integer(), nullable=True),
        sa.Column("rollback_version_number", sa.Integer(), nullable=True),
        sa.Column("apply_state", postgresql.JSONB(), nullable=False, server_default=_jsonb("{}")),
        sa.Column("finding", postgresql.JSONB(), nullable=False, server_default=_jsonb("{}")),
        sa.Column("evidence_refs", postgresql.JSONB(), nullable=False, server_default=_jsonb("[]")),
        sa.Column("experiment_refs", postgresql.JSONB(), nullable=False, server_default=_jsonb("[]")),
        sa.Column("experiment", postgresql.JSONB(), nullable=False, server_default=_jsonb("{}")),
        sa.Column("experiment_verdict", sa.String(16), nullable=True),
        sa.Column("evaluation_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("baseline_run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("candidate_run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("quality_metric", sa.String(32), nullable=False, server_default=sa.text("'score'")),
        sa.Column("baseline_score", sa.Float(), nullable=True),
        sa.Column("candidate_score", sa.Float(), nullable=True),
        sa.Column("delta", sa.Float(), nullable=True),
        sa.Column("verdict", sa.String(16), nullable=True),
        sa.Column("outcome_note", sa.Text(), nullable=True),
        sa.Column("author_agent_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("requested_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("decided_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("shadow_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("watch_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(_in("status", _STATUSES), name="ck_improvement_proposals_status_valid"),
        sa.CheckConstraint(_in("proposal_type", _TYPES), name="ck_improvement_proposals_type_valid"),
        sa.CheckConstraint(_in("target_type", _TARGET_TYPES), name="ck_improvement_proposals_target_type_valid"),
        if_not_exists=True,
    )


_INDEXES: tuple[tuple[str, list[str], bool, str | None], ...] = (
    ("ix_improvement_proposals_target_created", ["target_type", "target_id", "created_at"], False, None),
    ("ix_improvement_proposals_agent_created", ["agent_id", "created_at"], False, None),
    ("ix_improvement_proposals_status", ["status"], False, None),
    (
        "uq_improvement_proposals_open_target",
        ["target_type", "target_id"],
        True,
        f"{_OPEN} AND proposal_type <> 'new_flow'",
    ),
    ("uq_improvement_proposals_open_dedup", ["dedup_key"], True, _OPEN),
)

def upgrade() -> None:
    conn = op.get_bind()

    if not _has_table(conn, _TABLE):
        # The belt alone: see the lock profile above.
        set_lock_timeout(conn)
        _create_table()

    missing = [index for index in _INDEXES if not _has_index(conn, index[0])]
    if missing:
        set_lock_timeout(conn)
        for name, columns, unique, where in missing:
            op.create_index(
                name,
                _TABLE,
                columns,
                unique=unique,
                postgresql_where=sa.text(where) if where else None,
                if_not_exists=True,
            )


def downgrade() -> None:
    conn = op.get_bind()
    if not _has_table(conn, _TABLE):
        return
    # The drop takes an ACCESS EXCLUSIVE lock on the table and, to remove the
    # foreign key triggers, on ``agents``: readers block that, so the canonical
    # pre-flight runs before the belt.
    for table in (_TABLE, *_REFERENCED_TABLES):
        terminate_lock_holders(conn, table)
    set_lock_timeout(conn)
    op.drop_table(_TABLE, if_exists=True)
