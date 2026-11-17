"""general_assistant_config.linked_agent_configured: keep the admin's link

``wire_default_agents`` linked the CTEM Assistant as the General Assistant on
every boot and AI re-enable, over whatever an administrator had chosen. NULL
means both "never configured" and "no agent", so the choice needs its own
column: the settings API sets it on any save of the link, and the boot / toggle
wiring links the CTEM Assistant only while it is false.

Backfill: only a row whose link is unambiguously an administrator's choice is
marked - one linked to an agent other than the CTEM Assistant, which nothing
but the settings API ever links. A NULL link cannot be told apart from "never
configured" (a row created with a provider before the next boot is NULL too),
so it stays false; the singleton table makes the backfill one row.

Safety contract (backend/AGENTS.md > "Database migrations (safety)"):

* Idempotent - the column is probed first and added with ``ADD COLUMN IF NOT
  EXISTS``.
* Lock-safe - a column with a constant default is metadata-only on
  PostgreSQL 11+; the canonical recipe runs before it (probe,
  ``terminate_lock_holders``, ``set_lock_timeout``).

Revision ID: g5099galink01
Revises: f1ctem8988s1
Create Date: 2026-10-24 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "g5099galink01"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "f1ctem8988s1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "general_assistant_config"
_COLUMN = "linked_agent_configured"


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
        sa.text(
            f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS {_COLUMN} "
            "BOOLEAN NOT NULL DEFAULT false"
        )
    )
    conn.execute(
        sa.text(
            "UPDATE general_assistant_config SET linked_agent_configured = true "
            "WHERE linked_agent_id IS NOT NULL "
            "AND linked_agent_id NOT IN "
            "(SELECT id FROM agents WHERE slug = 'ctem-assistant')"
        )
    )


def downgrade() -> None:
    conn = op.get_bind()
    if not _column_exists(conn):
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    conn.execute(sa.text(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS {_COLUMN}"))
