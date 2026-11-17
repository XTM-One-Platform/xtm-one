"""record the protocol version a task's partner spoke

A notification spells the task's state, and the two versions spell it
differently — v0.3's lowercase values against v1.0's enum names. The state was
always written in v0.3, so a v1.0 partner branching on it to decide whether to
read the task never branched.

The version is negotiated per request from the ``A2A-Version`` header, and a
notification is sent long after that request is over — from a sweep, or from a
background announcement — so it has to be recorded rather than re-derived.
Which is the rule the outbound half already follows for the same reason
(``A2AClientTarget.agreed_protocol_version``, FR-023).

No backfill. A task created before this spoke whatever it spoke, and the
announcement falls back to v0.3 for a NULL — the value every such task was
already being sent.

``ADD COLUMN`` with no default is metadata-only, and the canonical lock-safe
recipe still applies because the ALTER takes an ACCESS EXCLUSIVE lock:
idempotency probe -> terminate_lock_holders -> set_lock_timeout -> guarded DDL.

Revision ID: a2ac1pver001
Revises: a2ab1ask0001
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

# revision identifiers, used by Alembic.
revision: str = "a2ac1pver001"
down_revision: str | None = "a2ab1ask0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "a2a_tasks"
_COLUMN = "protocol_version"


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
    if _column_exists(conn, _TABLE, _COLUMN):
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    op.execute(
        f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS {_COLUMN} VARCHAR(16)"
    )


def downgrade() -> None:
    conn = op.get_bind()
    if not _column_exists(conn, _TABLE, _COLUMN):
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS {_COLUMN}")
