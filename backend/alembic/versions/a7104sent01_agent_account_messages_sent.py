"""agent accounts: every message sent, proactive sends included

``agent_account_replies.kind`` and ``agent_account_decisions.kind`` say
``proactive`` for a message the agent sent through the account on its own (a
chat run, an assignment, a tool call); NULL, read as ``reply``, for an answer
to a message the account heard - every existing row. No backfill.
``agent_account_replies.origin`` keeps what produced a proactive send.

Safety contract (backend/AGENTS.md > "Database migrations (safety)"):

* Idempotent - each column is probed first and added with ``IF NOT EXISTS``.
* Lock-safe - a nullable column is metadata-only; the canonical recipe
  (probe, ``terminate_lock_holders``, ``set_lock_timeout``) runs before it.
* Reversible - the downgrade drops the columns, under the same recipe.

Revision ID: a7104sent01
Revises: b7059renew01
Create Date: 2026-11-01 16:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "a7104sent01"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "b7059renew01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLUMNS = (
    ("agent_account_replies", "kind", "VARCHAR(20)"),
    ("agent_account_replies", "origin", "JSON"),
    ("agent_account_decisions", "kind", "VARCHAR(20)"),
)


def _has_column(conn, table: str, column: str) -> bool:
    return (
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = :t AND column_name = :c"
            ),
            {"t": table, "c": column},
        ).first()
        is not None
    )


def upgrade() -> None:
    conn = op.get_bind()
    for table, column, sql_type in _COLUMNS:
        if _has_column(conn, table, column):
            continue
        terminate_lock_holders(conn, table)
        set_lock_timeout(conn)
        conn.execute(sa.text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {sql_type}"))


def downgrade() -> None:
    conn = op.get_bind()
    for table, column, _sql_type in reversed(_COLUMNS):
        if not _has_column(conn, table, column):
            continue
        terminate_lock_holders(conn, table)
        set_lock_timeout(conn)
        conn.execute(sa.text(f"ALTER TABLE {table} DROP COLUMN IF EXISTS {column}"))
