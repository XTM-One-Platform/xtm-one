"""record why a completed run stopped before its task was done

``stop_reason`` on ``assignment_runs``: the agent loop's terminal stop reason
when the run completed on one of its limits rather than on its own terms - the
iteration cap (``max_iterations``), the wall clock (``deadline``), the per-run
budget, a model call that never answered, background work still running when
the wait budget ran out. Without it such a run read as a clean success.

Nullable with no default: a run recorded before this one has none, so the
``ADD COLUMN`` is metadata-only and needs no backfill. No index: the column is
read with the run, never filtered on.

Revision ID: r3015stop01
Revises: s5811slackid01
Create Date: 2026-10-24 18:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "r3015stop01"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "s5811slackid01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "assignment_runs"
_COLUMN = "stop_reason"


def _has_column(conn) -> bool | None:
    """Whether the column is there; ``None`` when the table is not."""
    inspector = sa.inspect(conn)
    if not inspector.has_table(_TABLE):
        return None
    return _COLUMN in {column["name"] for column in inspector.get_columns(_TABLE)}


def upgrade() -> None:
    conn = op.get_bind()
    if _has_column(conn) is not False:
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    op.execute(f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS {_COLUMN} VARCHAR(32)")


def downgrade() -> None:
    conn = op.get_bind()
    if not _has_column(conn):
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS {_COLUMN}")
