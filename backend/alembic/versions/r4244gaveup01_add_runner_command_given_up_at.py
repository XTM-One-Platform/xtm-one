"""add runner_commands.given_up_at

When whoever dispatched a runner command stopped waiting for it - the agent
tool's wait ended, its chat turn was cancelled, its flow run was cancelled or
dead-lettered. Recorded on the row so it survives a restart, another replica
and a Redis flush: the transport never requeues or redelivers a command nobody
waits for (``runner_transport.give_up_on_command``). Nullable, no default:
every existing row reads as "still awaited", which is what it was.

Lock profile: a nullable ``ADD COLUMN`` with no default is metadata-only on
PG 11+, but the ALTER still takes ACCESS EXCLUSIVE on ``runner_commands``,
which the runner transport reads and writes on every poll, so any open
transaction that has read it queues the ALTER behind itself in a rolling
deploy. Canonical pre-flight: idempotency probe -> terminate_lock_holders ->
set_lock_timeout -> guarded DDL.

Plus two partial indexes, so neither new lookup scans the command audit
history: ``ix_runner_commands_given_up_dispatched`` for the scheduler's sweep
of given-up commands past their lease (read every tick), and
``ix_runner_commands_flow_run_open`` for the open commands a cancelled or
dead-lettered flow run gives up. Built by ``create_index_concurrently`` in an
autocommit block, outside the ``lock_timeout`` belt: bounded by its own build
timeout, an INVALID leftover of an interrupted build dropped and rebuilt, and
idempotent on their own so a re-run after a crash between the steps still
builds them.

Revision ID: r4244gaveup01
Revises: b3818logo001
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import (
    create_index_concurrently,
    set_lock_timeout,
    terminate_lock_holders,
)

# revision identifiers, used by Alembic.
revision: str = "r4244gaveup01"
down_revision: str | None = "b3818logo001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "runner_commands"
_COLUMN = "given_up_at"
_INDEXES = {
    "ix_runner_commands_given_up_dispatched": (
        "(dispatched_at) WHERE status = 'dispatched' AND given_up_at IS NOT NULL"
    ),
    "ix_runner_commands_flow_run_open": (
        "(flow_run_id) WHERE flow_run_id IS NOT NULL "
        "AND status IN ('pending', 'pending_approval', 'dispatched')"
    ),
}


def _column_exists(conn, table: str, column: str) -> bool:
    # Scoped to the active schema - information_schema.columns spans ALL
    # schemas, so a same-named table elsewhere must not make the probe skip
    # the ADD COLUMN in the schema being migrated.
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = current_schema() "
                "AND table_name = :table AND column_name = :column"
            ),
            {"table": table, "column": column},
        ).scalar()
    )


def upgrade() -> None:
    conn = op.get_bind()
    # 1. Idempotency probe - steady-state re-runs take NO lock.
    if not _column_exists(conn, _TABLE, _COLUMN):
        # 2. Pre-flight: kill same-role lock holders on the table the DDL locks.
        terminate_lock_holders(conn, _TABLE)
        # 3. Safety belt: fail fast (55P03) instead of wedging the lock queue.
        set_lock_timeout(conn)
        # 4. Guarded DDL - nullable, no default: metadata-only, no rewrite.
        op.execute(
            f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS {_COLUMN} "
            "TIMESTAMP WITH TIME ZONE"
        )
    # Bounded, and an INVALID leftover of an interrupted build is dropped and
    # rebuilt rather than matched by IF NOT EXISTS (create_index_concurrently).
    with op.get_context().autocommit_block():
        for name, definition in _INDEXES.items():
            create_index_concurrently(conn, name, _TABLE, definition)


def downgrade() -> None:
    conn = op.get_bind()
    with op.get_context().autocommit_block():
        for name in _INDEXES:
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {name}")
    if not _column_exists(conn, _TABLE, _COLUMN):
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS {_COLUMN}")
