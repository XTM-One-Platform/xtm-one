"""add work_item_id to sandbox_workspaces

The work item the inbox dispatched a run for (#9868): while the item is open,
the run's sandbox workspace outlives the run and the next run of the same item
(same agent, same person) adopts it, instead of starting with an empty
``workspace/``. Deleted once the item is done or cancelled, or after the run
idle TTL.

Nullable UUID with no default and no foreign key (like ``conversation_id`` and
``assignment_run_id``): metadata-only ``ADD COLUMN``, canonical lock-safe
recipe (idempotency probe -> terminate_lock_holders -> set_lock_timeout ->
guarded DDL), then the index built in place. NULL reads as "no item", which is
what every existing row is: no backfill.

Revision ID: h9868wsitem1
Revises: g9776grantsc1
Create Date: 2026-11-07 22:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import create_index_in_place, set_lock_timeout, terminate_lock_holders

# revision identifiers, used by Alembic.
revision: str = "h9868wsitem1"
# NOT a constant: must always be main's CURRENT head, re-pointed on every rebase
# (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | None = "g9776grantsc1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "sandbox_workspaces"
_COLUMN = "work_item_id"
_INDEX = "ix_sandbox_workspaces_work_item"


def _has_column(conn) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = current_schema() "
                "AND table_name = :table AND column_name = :column"
            ),
            {"table": _TABLE, "column": _COLUMN},
        ).first()
    )


def upgrade() -> None:
    conn = op.get_bind()
    # Fresh installs run create_all + stamp head; this body never executes.
    if not sa.inspect(conn).has_table(_TABLE):
        return
    if not _has_column(conn):
        terminate_lock_holders(conn, _TABLE)
        set_lock_timeout(conn)
        op.execute(f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS {_COLUMN} UUID")
    with op.get_context().autocommit_block():
        create_index_in_place(conn, _INDEX, _TABLE, f"({_COLUMN})")


def downgrade() -> None:
    conn = op.get_bind()
    if not sa.inspect(conn).has_table(_TABLE):
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    op.execute(f"DROP INDEX IF EXISTS {_INDEX}")
    if _has_column(conn):
        terminate_lock_holders(conn, _TABLE)
        set_lock_timeout(conn)
        op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS {_COLUMN}")
