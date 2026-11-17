"""add ix_flow_events_type_memory_id — the memory-event supersede lookup

Durable memory writes emit a ``Memory persisted`` flow event, and one
memory entry emits again every time it is rewritten: the consolidated upsert
is the same row over and over, and an operational block moves
``planned -> running -> completed`` in place. The emitter therefore deletes
that entry's older emissions as it stores the newer one, keyed on
``payload ->> 'memory_id'``.

Without this index that delete degrades to a scan of every emission of the
type on every durable memory write — the cost the supersede exists to remove.
``event_type_id`` leads the index so the same entry serves the exact lookup
and stays cheap for event types whose payload carries no ``memory_id``: they
index a single NULL under their own type.

Purely additive, and built ``CONCURRENTLY`` outside the ``lock_timeout`` belt:
``flow_events`` is written by every emit, so a plain ``CREATE INDEX``'s SHARE
lock would block emissions (and therefore memory writes) for the build.

Revision ID: m1mem516001
Revises: a2af1reopen1
Create Date: 2026-08-30 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "m1mem516001"
down_revision: str | Sequence[str] | None = "a2af1reopen1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "flow_events"
_INDEX = "ix_flow_events_type_memory_id"


def _table_exists(conn, table: str) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = current_schema() AND table_name = :t"
            ),
            {"t": table},
        ).scalar()
    )


def upgrade() -> None:
    conn = op.get_bind()
    if not _table_exists(conn, _TABLE):
        # Fresh install: create_all builds the table with the index declared
        # on the model and stamps head — this body never needs to run.
        return

    # IF NOT EXISTS carries the idempotency, so the migrations CI job re-running
    # this revision against an already-upgraded database is a no-op rather than
    # an error. A previous CONCURRENTLY build that crashed leaves the index
    # INVALID rather than absent; that case is named in UPGRADING because only
    # an operator can decide when to drop and rebuild it.
    with op.get_context().autocommit_block():
        op.execute("SET statement_timeout = 0")
        op.execute(
            f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {_INDEX} "
            f"ON {_TABLE} (event_type_id, (payload ->> 'memory_id'))"
        )


def downgrade() -> None:
    conn = op.get_bind()
    if not _table_exists(conn, _TABLE):
        return
    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX}")
