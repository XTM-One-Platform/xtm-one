"""add agents.accept_caller_output_format

Whether a caller may impose its own answer shape on this agent for one call
(``output_schema`` of consult_agent / spawn_background_task). Off for every
agent, existing and new: the agent answers in its configured format and such a
call is refused before the agent runs; only an agent's manager turns it on.

Lock profile: a ``NOT NULL`` column with a CONSTANT default is metadata-only on
PG 11+, but the ALTER still takes ACCESS EXCLUSIVE on ``agents``, a hot table
(every chat turn and run reads it), so an open transaction that merely read it
queues the ALTER behind itself in a rolling deploy. Canonical pre-flight:
idempotency probe -> terminate_lock_holders -> set_lock_timeout -> guarded DDL.

Revision ID: acof01callerfmt
Revises: a2ah2runctx01
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

# revision identifiers, used by Alembic.
revision: str = "acof01callerfmt"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "a2ah2runctx01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "agents"
_COLUMN = "accept_caller_output_format"


def _column_exists(conn, table: str, column: str) -> bool:
    # Scoped to the active schema: information_schema.columns spans every
    # schema, and a same-named table elsewhere must not skip the ADD COLUMN.
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
    if _column_exists(conn, _TABLE, _COLUMN):
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    op.execute(f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS {_COLUMN} BOOLEAN NOT NULL DEFAULT false")


def downgrade() -> None:
    conn = op.get_bind()
    if not _column_exists(conn, _TABLE, _COLUMN):
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS {_COLUMN}")
