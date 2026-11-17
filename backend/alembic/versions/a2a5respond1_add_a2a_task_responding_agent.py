"""add a2a_tasks.responding_agent_id — attribute a transferred answer

A partner's task can now be answered by a different agent than the one whose
card it fetched: an inbound task may transfer, and the artifact it returns is
then not the addressed agent's work. This column records which agent produced
it, so ``build_artifacts`` can say so instead of the partner having no way to
tell. NULL means no transfer happened, which is the ordinary case.

Deliberately no foreign key: the column names who answered a historical task,
so deleting that agent later must neither erase the attribution nor be blocked
by it.

Nullable ``ADD COLUMN`` with no default is metadata-only on PG 11+; the
canonical lock-safe recipe still applies because the ALTER takes an
ACCESS EXCLUSIVE lock: idempotency probe -> terminate_lock_holders ->
set_lock_timeout -> guarded DDL.

Revision ID: a2a5respond1
Revises: a2a4unbound1
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

# revision identifiers, used by Alembic.
revision: str = "a2a5respond1"
down_revision: str | None = "a2a4unbound1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "a2a_tasks"
_COLUMN = "responding_agent_id"


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
    op.execute(f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS {_COLUMN} UUID")


def downgrade() -> None:
    conn = op.get_bind()
    if not _column_exists(conn, _TABLE, _COLUMN):
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS {_COLUMN}")
