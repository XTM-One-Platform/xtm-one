"""add the email signature settings of integrations and agent bindings

* ``integrations.signature_enabled`` (BOOLEAN NOT NULL DEFAULT TRUE) and
  ``integrations.signature_integration_id`` (UUID, nullable, no foreign key):
  the Email signature setting of an integration whose tools send email. The
  default is the behaviour before the setting existed - signed, with the
  signature integration picked automatically - so nothing is backfilled and no
  email loses its signature on upgrade.
* ``agents.integration_signatures`` (JSON, nullable): an agent's override per
  binding. NULL (every existing row) inherits every integration's setting.

Lock safety: canonical recipe per table - idempotency probe, then
``terminate_lock_holders`` + ``set_lock_timeout``, then metadata-only
``ADD COLUMN IF NOT EXISTS`` (a constant default or none: no table rewrite).
No index: the columns are only read with their row. Old replicas during a
rolling deploy never select a column they do not know about.

Revision ID: e6717sig0001
Revises: v5456vault01
Create Date: 2026-10-29 09:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

# revision identifiers, used by Alembic.
revision: str = "e6717sig0001"
# NOT a constant: must always be main's CURRENT head, re-pointed on every rebase
# (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "v5456vault01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Per table, each column and its type as added.
_COLUMNS: dict[str, tuple[tuple[str, str], ...]] = {
    "integrations": (
        ("signature_enabled", "BOOLEAN NOT NULL DEFAULT TRUE"),
        ("signature_integration_id", "UUID"),
    ),
    "agents": (("integration_signatures", "JSON"),),
}


def _existing_columns(conn, table: str) -> set[str] | None:
    """The columns of *table*, or ``None`` when it does not exist (a fresh install)."""
    inspector = sa.inspect(conn)
    if not inspector.has_table(table):
        return None
    return {column["name"] for column in inspector.get_columns(table)}


def upgrade() -> None:
    conn = op.get_bind()
    for table, columns in _COLUMNS.items():
        existing = _existing_columns(conn, table)
        if existing is None:
            continue
        missing = [(name, kind) for name, kind in columns if name not in existing]
        if not missing:
            continue
        terminate_lock_holders(conn, table)
        set_lock_timeout(conn)
        for name, kind in missing:
            op.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {name} {kind}")


def downgrade() -> None:
    conn = op.get_bind()
    for table, columns in _COLUMNS.items():
        existing = _existing_columns(conn, table)
        if existing is None:
            continue
        present = [name for name, _kind in columns if name in existing]
        if not present:
            continue
        terminate_lock_holders(conn, table)
        set_lock_timeout(conn)
        for name in present:
            op.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS {name}")
