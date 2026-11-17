"""add frictions_undisposed to assignment_runs

``frictions_undisposed`` on ``assignment_runs``: how many frictions of the run
(``agent_frictions``, ``services/frictions.py``) had no disposition when it
ended. Written by the run finalizer, read by the Operations row and the
``inspect_my_traces`` rollup.

Nullable with no default: a run that left no friction, and every run before
this migration, has none, so the ``ADD COLUMN`` is metadata-only and needs no
backfill (rule 4, no unbounded backfill).

Revision ID: f8528frict02
Revises: w8558wicomp1
Create Date: 2026-11-05 00:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "f8528frict02"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "w8558wicomp1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "assignment_runs"
_COLUMN = "frictions_undisposed"


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
    op.execute(f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS {_COLUMN} INTEGER")


def downgrade() -> None:
    conn = op.get_bind()
    if not _has_column(conn):
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS {_COLUMN}")
