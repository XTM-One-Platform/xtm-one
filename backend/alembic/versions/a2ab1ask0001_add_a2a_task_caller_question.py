"""add the columns a task needs to ask its caller a question

A2A's ``input-required`` means a task is blocked on its CLIENT — the calling
agent, not a person. An exposed agent that needs a detail it was not given had
no way to say so: it could only write the question into an answer and let the
task complete, which reads to the caller as a result.

``caller_question`` holds what was asked while the run that asked it is over.
The state is projected, not stored (one source of truth per ``006``'s data
model), so a task whose run finished with a question outstanding reports
``input-required`` and everything else reports what the run says.

``clarification_count`` bounds the exchange. Two agents can ask each other for
precision indefinitely, and neither is a person who gets bored.

No backfill. Existing tasks asked nothing, and NULL is exactly that.

``ADD COLUMN`` with a constant default is metadata-only on PG 11+; the
canonical lock-safe recipe still applies because the ALTER takes an
ACCESS EXCLUSIVE lock: idempotency probe -> terminate_lock_holders ->
set_lock_timeout -> guarded DDL.

Revision ID: a2ab1ask0001
Revises: a2aa2caps0001
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

# revision identifiers, used by Alembic.
revision: str = "a2ab1ask0001"
down_revision: str | None = "a2aa2caps0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "a2a_tasks"
_COLUMNS = {
    "caller_question": "TEXT",
    "clarification_count": "INTEGER NOT NULL DEFAULT 0",
}


def _column_exists(conn, table: str, column: str) -> bool:
    # Scoped to the active schema: information_schema.columns spans all of
    # them, and a same-named table elsewhere must not decide this probe.
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
    missing = {c: t for c, t in _COLUMNS.items() if not _column_exists(conn, _TABLE, c)}
    if not missing:
        return
    # One belt for both: the table is taken ACCESS EXCLUSIVE either way, and
    # twice is twice the chance of waiting behind someone else's lock.
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    for column, ddl_type in missing.items():
        op.execute(f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS {column} {ddl_type}")


def downgrade() -> None:
    conn = op.get_bind()
    present = [c for c in _COLUMNS if _column_exists(conn, _TABLE, c)]
    if not present:
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    for column in present:
        op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS {column}")
