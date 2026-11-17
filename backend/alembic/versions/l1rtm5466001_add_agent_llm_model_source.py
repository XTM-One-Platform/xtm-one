"""agents.llm_model_source: run an agent on the platform routing model

One nullable column. NULL keeps every existing agent on its own model and the
usual fallbacks; ``"routing"`` makes it run on ``PlatformSettings.routing_model``
(the platform's lightweight model), resolved by
``app.llm.agent_model.agent_model_selection``.

Safety contract (backend/AGENTS.md > "Database migrations (safety)"):

* Idempotent - the column is probed first and added with ``ADD COLUMN IF NOT
  EXISTS``.
* Lock-safe - a nullable column with no default is metadata-only; the canonical
  recipe runs before it (probe, ``terminate_lock_holders``, ``set_lock_timeout``).

Revision ID: l1rtm5466001
Revises: s5459swi001
Create Date: 2026-10-07 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "l1rtm5466001"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "s5459swi001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "agents"
_COLUMN = "llm_model_source"


def _column_exists(conn) -> bool:
    return (
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = current_schema() "
                "AND table_name = :t AND column_name = :c"
            ),
            {"t": _TABLE, "c": _COLUMN},
        ).first()
        is not None
    )


def upgrade() -> None:
    conn = op.get_bind()
    if _column_exists(conn):
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    conn.execute(
        sa.text(f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS {_COLUMN} VARCHAR(16)")
    )


def downgrade() -> None:
    conn = op.get_bind()
    if not _column_exists(conn):
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    conn.execute(sa.text(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS {_COLUMN}"))
