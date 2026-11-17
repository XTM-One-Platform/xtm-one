"""agents.integration_lending: personal integrations lent through an agent

One nullable JSON column on ``agents``: the owner's personal integrations
the agent lends to the people it is shared with, usable in their chats with
that agent only (``Agent.integration_lending``). NULL lends nothing, so
existing agents keep today's behaviour; no default, no backfill.

Revision ID: l8824lend01
Revises: c8cnc8254084
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "l8824lend01"
# NOT a constant: must always be the base branch's CURRENT head, re-pointed
# whenever it gains a migration (backend/AGENTS.md > "Database migrations
# (safety)", rule 1).
down_revision: str | Sequence[str] | None = "c8cnc8254084"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "agents"
_COLUMN = "integration_lending"


def _column_exists(conn, table: str, column: str) -> bool:
    inspector = sa.inspect(conn)
    if not inspector.has_table(table):
        return False
    return any(col["name"] == column for col in inspector.get_columns(table))


def upgrade() -> None:
    conn = op.get_bind()
    # Guarded: the migrations CI job runs upgrade twice.
    if _column_exists(conn, _TABLE, _COLUMN):
        return
    # Nullable with no default is catalogue-only; the belt is for a
    # concurrent lock holder on a live table.
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    op.add_column(_TABLE, sa.Column(_COLUMN, sa.JSON(), nullable=True))


def downgrade() -> None:
    conn = op.get_bind()
    if not _column_exists(conn, _TABLE, _COLUMN):
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    op.drop_column(_TABLE, _COLUMN)
