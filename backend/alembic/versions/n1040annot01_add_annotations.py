"""add annotations (per-user ratings of agent work beyond chat messages)

Purely additive: one new table, no change to any existing column, so replicas
still on the previous version ignore a table they do not know about. Nothing is
backfilled: chat-message ratings stay in ``message_feedback`` (see
``app.models.annotation`` for why), and nothing else could be rated before.

Lock profile: the table is new, so nothing can hold a lock on it and its
indexes are built non-concurrently, in the same transaction. Its two foreign
keys install triggers on ``users`` and ``agents``, which takes a SHARE ROW
EXCLUSIVE lock on each: it waits only for a transaction that WROTE one of those
tables, never for a reader, so the upgrade runs under the lock_timeout belt
alone (a contended attempt fails fast, 55P03, and the startup runner retries)
- terminating the lock holders would kill the in-flight requests and agentic
loops of the previous-version replicas, which merely read ``users`` and
``agents``, for a lock they do not block (backend/AGENTS.md > "Database
migrations (safety)", rule 5). The downgrade is different: DROP TABLE takes an
ACCESS EXCLUSIVE lock on both to remove the triggers, which readers do block,
so it runs the full pre-flight. The polymorphic ``target_id`` deliberately
carries no key, which keeps the write-hot ``assignment_runs`` and
``sub_agent_tasks`` out of this migration entirely.

Revision ID: n1040annot01
Revises: i4234credsrc1
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "n1040annot01"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "i4234credsrc1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "annotations"
#: The tables the foreign keys lock to install (and remove) their triggers.
_REFERENCED_TABLES = ("users", "agents")

#: The same definitions as ``Annotation.__table_args__`` (the create_all path).
_INDEXES: tuple[tuple[str, list[str]], ...] = (
    ("ix_annotations_user_id", ["user_id"]),
    ("ix_annotations_agent_updated", ["agent_id", "updated_at"]),
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
    # never executes), and the migrations CI job re-runs the revision against
    # an already-upgraded database. Both must be no-ops rather than errors, and
    # the table and each index are probed on their own, so a partial state
    # converges too.
    if not _has_table(conn, _TABLE):
        # The belt alone: see the lock profile above.
        set_lock_timeout(conn)
        op.create_table(
            _TABLE,
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("target_type", sa.String(32), nullable=False),
            sa.Column("target_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column(
                "user_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("users.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "agent_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("agents.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("rating", sa.String(20), nullable=False),
            sa.Column("comment", sa.Text(), nullable=True),
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
            sa.UniqueConstraint(
                "target_type",
                "target_id",
                "user_id",
                name="uq_annotations_target_user",
            ),
            sa.CheckConstraint(
                "rating IN ('positive', 'negative')",
                name="ck_annotations_rating_valid",
            ),
            if_not_exists=True,
        )

    missing = [(name, columns) for name, columns in _INDEXES if not _has_index(conn, name)]
    if missing:
        # Instant on the table created above; the belt matters for a table left
        # without an index, which may already be taking ratings (the build's
        # SHARE lock waits for a writer, never for a reader).
        set_lock_timeout(conn)
        for name, columns in missing:
            op.create_index(name, _TABLE, columns, if_not_exists=True)


def downgrade() -> None:
    conn = op.get_bind()

    # The drop takes an ACCESS EXCLUSIVE lock on the table and, to remove the
    # foreign key triggers, on the tables it references: readers block that,
    # so the canonical pre-flight runs before the belt (see the lock profile).
    if _has_table(conn, _TABLE):
        for table in (_TABLE, *_REFERENCED_TABLES):
            terminate_lock_holders(conn, table)
        set_lock_timeout(conn)
        op.drop_table(_TABLE, if_exists=True)
