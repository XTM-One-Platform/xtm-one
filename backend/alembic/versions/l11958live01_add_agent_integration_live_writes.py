"""add the OpenCTI Live writes override of agent bindings

* ``agents.integration_live_writes`` (JSON, nullable): an agent's override per
  OpenCTI binding of the integration's Live writes setting. NULL (every
  existing row) inherits every integration's setting, the behaviour before the
  column existed, so nothing is backfilled.

Lock safety: canonical recipe - idempotency probe, then
``terminate_lock_holders`` + ``set_lock_timeout``, then metadata-only
``ADD COLUMN IF NOT EXISTS`` (no default: no table rewrite). No index: the
column is only read with its row. Old replicas during a rolling deploy never
select a column they do not know about.

Revision ID: l11958live01
Revises: o11913sync01
Create Date: 2026-11-15 03:10:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

# revision identifiers, used by Alembic.
revision: str = "l11958live01"
# NOT a constant: must always be main's CURRENT head, re-pointed on every rebase
# (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "o11913sync01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "agents"
_COLUMN = "integration_live_writes"


def _existing_columns(conn) -> set[str] | None:
    """The columns of the table, or ``None`` when it does not exist (a fresh install)."""
    inspector = sa.inspect(conn)
    if not inspector.has_table(_TABLE):
        return None
    return {column["name"] for column in inspector.get_columns(_TABLE)}


def upgrade() -> None:
    conn = op.get_bind()
    existing = _existing_columns(conn)
    if existing is None or _COLUMN in existing:
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    op.execute(f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS {_COLUMN} JSON")


def downgrade() -> None:
    conn = op.get_bind()
    existing = _existing_columns(conn)
    if existing is None or _COLUMN not in existing:
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS {_COLUMN}")
