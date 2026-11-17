"""agent accounts: the report of the last sync

``agent_accounts.sync_report`` holds what the last sync of an account found:
when it ran, how many subscriptions the rules need and how many the provider
holds, and the problems grouped by cause with the conversations they affect
(``services/agent_accounts/problems.report``). NULL until the account's next
sync.

Safety contract (backend/AGENTS.md > "Database migrations (safety)"):

* Idempotent - the column is probed first and added with ``IF NOT EXISTS``.
* Lock-safe - a nullable column is metadata-only; the canonical recipe
  (probe, ``terminate_lock_holders``, ``set_lock_timeout``) runs before it.
* Reversible - the downgrade drops the column, under the same recipe.

Revision ID: s5619sync01
Revises: r1rxn5511001
Create Date: 2026-10-10 22:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "s5619sync01"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "r1rxn5511001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "agent_accounts"
_COLUMN = "sync_report"


def _has_column(conn) -> bool:
    return (
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = :t AND column_name = :c"
            ),
            {"t": _TABLE, "c": _COLUMN},
        ).first()
        is not None
    )


def upgrade() -> None:
    conn = op.get_bind()
    if _has_column(conn):
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    conn.execute(sa.text(f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS {_COLUMN} JSON"))


def downgrade() -> None:
    conn = op.get_bind()
    if not _has_column(conn):
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    conn.execute(sa.text(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS {_COLUMN}"))
