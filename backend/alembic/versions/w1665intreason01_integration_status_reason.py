"""integrations carry why and since when their status changed (#1665)

An integration in status ``error`` or ``reauthorize`` named no reason and no
time: the refresh sweep logged its verdict and dropped it. ``status_reason``
keeps the verdict, ``status_reason_at`` the time it was recorded; both are read
only while the status is not usable. Both are nullable with no default, so the ADD is a
catalog-only change and existing rows read "no reason recorded".

Revision ID: w1665intreason01
Revises: v1591pallinks01
Create Date: 2026-11-24 05:20:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "w1665intreason01"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "v1591pallinks01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "integrations"
_COLUMNS: dict[str, str] = {
    "status_reason": "VARCHAR(300)",
    "status_reason_at": "TIMESTAMP WITH TIME ZONE",
}


def _columns(conn: sa.engine.Connection) -> set[str] | None:
    inspector = sa.inspect(conn)
    if not inspector.has_table(_TABLE):
        return None
    return {c["name"] for c in inspector.get_columns(_TABLE)}


def upgrade() -> None:
    conn = op.get_bind()
    present = _columns(conn)
    if present is None or all(name in present for name in _COLUMNS):
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    for name, ddl in _COLUMNS.items():
        op.execute(f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS {name} {ddl}")


def downgrade() -> None:
    conn = op.get_bind()
    present = _columns(conn)
    if present is None:
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    for name in reversed(_COLUMNS):
        op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS {name}")
