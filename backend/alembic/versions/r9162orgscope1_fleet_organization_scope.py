"""fleet organization scope: the organizations a member reasons about (R3 of #9162)

``fleets`` gains ``organization_scope`` (JSON, ``{"mode": "primary"}`` by
default) and ``fleet_members`` a nullable ``organization_scope`` (same shape,
NULL inherits the fleet's). ``mode`` is ``primary``, ``monitored`` (every
tracked organization whose monitoring is on, plus the primary) or ``list``
with ``organization_ids`` (dev-docs/ctem-fleets.md section 10.3).

Nothing is backfilled: every existing fleet reads the primary organization,
which is what every run read before this revision, so every effective scope
is the same before and after. A new column with a constant default is a
metadata-only change on PostgreSQL; the lock-safe recipe still applies.

Revision ID: r9162orgscope1
Revises: r9321roirates1
Create Date: 2026-11-06 18:20:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "r9162orgscope1"
# NOT a constant: must always be main's CURRENT head, re-pointed on every rebase
# (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "r9321roirates1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    conn = op.get_bind()
    terminate_lock_holders(conn, "fleets")
    terminate_lock_holders(conn, "fleet_members")
    set_lock_timeout(conn)
    conn.execute(
        sa.text(
            "ALTER TABLE fleets ADD COLUMN IF NOT EXISTS organization_scope JSON NOT NULL "
            'DEFAULT \'{"mode": "primary"}\''
        )
    )
    conn.execute(sa.text("ALTER TABLE fleet_members ADD COLUMN IF NOT EXISTS organization_scope JSON"))


def downgrade() -> None:
    conn = op.get_bind()
    terminate_lock_holders(conn, "fleet_members")
    terminate_lock_holders(conn, "fleets")
    set_lock_timeout(conn)
    conn.execute(sa.text("ALTER TABLE fleet_members DROP COLUMN IF EXISTS organization_scope"))
    conn.execute(sa.text("ALTER TABLE fleets DROP COLUMN IF EXISTS organization_scope"))
