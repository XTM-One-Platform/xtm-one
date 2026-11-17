"""add persona_improvements (the human-approved persona self-improvement loop)

Purely additive: one new table, no change to any existing column, so replicas
still on the previous version ignore it. Nothing is backfilled.

Lock profile: the table is new, so nothing can hold a lock on it and its
indexes are built non-concurrently, in the same transaction. Its one foreign
key installs triggers on ``agents``, which takes a SHARE ROW EXCLUSIVE lock
there: it waits only for a transaction that WROTE ``agents``, never for a
reader, so the upgrade runs under the lock_timeout belt alone (a contended
attempt fails fast, 55P03, and the startup runner retries) - terminating the
lock holders would kill the in-flight requests and agentic loops of the
previous-version replicas, which merely read ``agents``, for a lock they do not
block (backend/AGENTS.md > "Database migrations (safety)", rule 5). The
downgrade is different: DROP TABLE takes an ACCESS EXCLUSIVE lock on
``agents`` to remove the triggers, which readers do block, so it runs the full
pre-flight. The evaluation, run and user ids carry no key, which keeps
``evaluation_runs`` and ``users`` out of this migration.

Revision ID: n1044impr001
Revises: n1041crit001
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "n1044impr001"
down_revision: str | Sequence[str] | None = "n1041crit001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "persona_improvements"
_REFERENCED_TABLES = ("agents",)
_STATUSES = (
    "pending_approval",
    "evaluating",
    "rejected",
    "kept",
    "rolled_back",
    "regression_kept",
    "failed",
)
_OPEN_PREDICATE = "status IN ('pending_approval', 'evaluating')"


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
            sa.text(
                "SELECT 1 FROM pg_indexes "
                "WHERE schemaname = current_schema() AND indexname = :n"
            ),
            {"n": name},
        ).scalar()
    )


def upgrade() -> None:
    conn = op.get_bind()

    # Idempotency probes: fresh installs run create_all + stamp head (this body
    # never executes), and the migrations CI job re-runs the revision.
    if not _has_table(conn, _TABLE):
        # The belt alone: see the lock profile above.
        set_lock_timeout(conn)
        op.create_table(
            _TABLE,
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "agent_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("agents.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("status", sa.String(32), nullable=False),
            sa.Column("evaluation_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("baseline_run_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("candidate_run_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("base_persona", sa.Text(), nullable=False, server_default=sa.text("''")),
            sa.Column("base_version_number", sa.Integer(), nullable=True),
            sa.Column("proposed_persona", sa.Text(), nullable=False),
            sa.Column("summary", sa.Text(), nullable=True),
            sa.Column("applied_version_number", sa.Integer(), nullable=True),
            sa.Column("rollback_version_number", sa.Integer(), nullable=True),
            sa.Column("quality_metric", sa.String(32), nullable=False, server_default=sa.text("'score'")),
            sa.Column("baseline_score", sa.Float(), nullable=True),
            sa.Column("candidate_score", sa.Float(), nullable=True),
            sa.Column("delta", sa.Float(), nullable=True),
            sa.Column("verdict", sa.String(16), nullable=True),
            sa.Column("outcome_note", sa.Text(), nullable=True),
            sa.Column("requested_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("decided_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
            sa.CheckConstraint(
                "status IN (" + ", ".join(f"'{status}'" for status in _STATUSES) + ")",
                name="ck_persona_improvements_status_valid",
            ),
            if_not_exists=True,
        )

    indexes = [
        ("ix_persona_improvements_agent_created", False),
        ("uq_persona_improvements_open", True),
    ]
    missing = [(name, unique) for name, unique in indexes if not _has_index(conn, name)]
    if missing:
        # Instant on the table created above; the belt matters for a table left
        # without an index, which may already hold cycles (the build's SHARE
        # lock waits for a writer, never for a reader).
        set_lock_timeout(conn)
        for name, unique in missing:
            if unique:
                op.create_index(
                    name,
                    _TABLE,
                    ["agent_id"],
                    unique=True,
                    postgresql_where=sa.text(_OPEN_PREDICATE),
                    if_not_exists=True,
                )
            else:
                op.create_index(name, _TABLE, ["agent_id", "created_at"], if_not_exists=True)


def downgrade() -> None:
    conn = op.get_bind()

    # The drop takes an ACCESS EXCLUSIVE lock on the table and, to remove the
    # foreign key triggers, on the table it references: readers block that, so
    # the canonical pre-flight runs before the belt (see the lock profile).
    if _has_table(conn, _TABLE):
        for table in (_TABLE, *_REFERENCED_TABLES):
            terminate_lock_holders(conn, table)
        set_lock_timeout(conn)
        op.drop_table(_TABLE, if_exists=True)
