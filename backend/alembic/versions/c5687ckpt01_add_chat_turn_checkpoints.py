"""add chat_turn_checkpoints (the tool work of a chat turn still in flight)

Purely additive: one new table, no change to any existing column, so old
replicas during a rolling deploy ignore a table they do not know about.
Nothing is backfilled - no turn kept a checkpoint before.

``chat_turn_checkpoints`` holds at most one row per conversation (the
conversation id is the primary key): the completed tool rounds of the turn in
flight, deleted in the transaction that stores its answer.

The column set and the unnamed PRIMARY KEY / REFERENCES clauses mirror the ORM
model (``ChatTurnCheckpoint`` in ``app/models/chat.py``), so a fresh install's
``create_all`` and this body produce the same constraint names.

Revision ID: c5687ckpt01
Revises: m5688sup01
Create Date: 2026-10-11 22:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout

revision: str = "c5687ckpt01"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "m5688sup01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "chat_turn_checkpoints"


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


def upgrade() -> None:
    conn = op.get_bind()
    # Idempotency probe: a fresh install runs create_all + stamp head (this
    # body never executes) and the migrations CI job re-runs the revision.
    if _has_table(conn, _TABLE):
        return
    # Nothing knows this table yet, so nothing holds a lock on it. Its foreign
    # key installs triggers on `conversations` (SHARE ROW EXCLUSIVE there): the
    # belt makes a contended attempt fail fast (55P03, retried by the startup
    # runner) instead of queueing every writer of that hot table behind it.
    # Terminating its holders would kill sessions that merely read it, which
    # this lock does not conflict with.
    set_lock_timeout(conn)
    op.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {_TABLE} (
            conversation_id UUID PRIMARY KEY
                REFERENCES conversations (id) ON DELETE CASCADE,
            turn_id VARCHAR(64),
            entries JSON NOT NULL,
            created_at TIMESTAMPTZ NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL
        )
        """
    )


def downgrade() -> None:
    conn = op.get_bind()
    if not _has_table(conn, _TABLE):
        return
    # DROP TABLE removes the foreign key triggers on `conversations` (ACCESS
    # EXCLUSIVE there): the belt makes a busy table fail the downgrade fast,
    # to be retried when it is quiet.
    set_lock_timeout(conn)
    op.execute(f"DROP TABLE IF EXISTS {_TABLE}")
