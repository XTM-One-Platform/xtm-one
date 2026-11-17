"""add group_managers: the users an admin elects manager of a group

Purely additive: one new table, no change to any existing column, so old
replicas during a rolling deploy ignore a table they do not know about.
Nothing is backfilled: no group had a manager before.

Revision ID: x6367grpmgr01
Revises: v6339draw01
Create Date: 2026-10-23 10:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_ddl import set_lock_timeout

revision: str = "x6367grpmgr01"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "v6339draw01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "group_managers"

#: The same definitions as ``GroupManager`` (the create_all path).
_INDEXES: tuple[tuple[str, list[str]], ...] = (
    ("ix_group_managers_group_id", ["group_id"]),
    ("ix_group_managers_user_id", ["user_id"]),
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

    # Fresh installs run create_all + stamp head (this body never executes) and
    # the migrations CI job re-runs it on an upgraded database: every object is
    # probed on its own so both, and a partial state, converge.
    #
    # Nothing knows the table yet, so nothing holds a lock on it. Its foreign
    # keys install triggers on `groups` and `users` (SHARE ROW EXCLUSIVE there):
    # the belt makes a contended attempt fail fast (55P03, retried by the
    # startup runner) instead of queueing their writers behind it. Their
    # readers do not conflict with that lock, so no holder is terminated.
    if not _has_table(conn, _TABLE):
        set_lock_timeout(conn)
        op.create_table(
            _TABLE,
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "group_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("groups.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "user_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("users.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "assigned_by",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("users.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=True,
                server_default=sa.text("now()"),
            ),
            sa.UniqueConstraint("group_id", "user_id", name="uq_group_managers_group_user"),
            if_not_exists=True,
        )

    missing = [(name, columns) for name, columns in _INDEXES if not _has_index(conn, name)]
    if missing:
        # Instant on the table created above; the belt matters for a table left
        # without an index by an interrupted run.
        set_lock_timeout(conn)
        for name, columns in missing:
            op.create_index(name, _TABLE, columns, if_not_exists=True)


def downgrade() -> None:
    conn = op.get_bind()

    # Removing the foreign key triggers takes an ACCESS EXCLUSIVE lock on
    # `groups` and `users` (see upgrade): the belt makes a busy table fail the
    # downgrade fast, to be retried when it is quiet.
    if _has_table(conn, _TABLE):
        set_lock_timeout(conn)
        op.drop_table(_TABLE, if_exists=True)
