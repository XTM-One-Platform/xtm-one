"""add platform_settings.setup_prompt_version / setup_prompt_progress

The first-connection setup proposal is versioned (#1087, CTEM-33): it is
proposed at install and once after an upgrade that carries a new setup
version, and each step of the guided flow saves on its own. Two nullable
columns (metadata-only on PG 11+), behind the canonical recipe: idempotency
probe -> terminate_lock_holders -> set_lock_timeout -> guarded DDL
(``platform_settings`` is read by every request).

Revision ID: s1087setupver1
Revises: c1110ctemsurface1
Create Date: 2026-11-22
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

# revision identifiers, used by Alembic.
revision: str = "s1087setupver1"
# NOT a constant: must always be main's CURRENT head, re-pointed on every rebase
# (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | None = "c1110ctemsurface1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "platform_settings"
_COLUMNS = {
    "setup_prompt_version": "VARCHAR(40)",
    "setup_prompt_progress": "JSON",
}


def _missing_columns(conn: sa.engine.Connection) -> list[str]:
    rows = conn.execute(
        sa.text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = :table"
        ),
        {"table": _TABLE},
    ).scalars()
    present = set(rows)
    return [name for name in _COLUMNS if name not in present]


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    # Fresh installs run create_all + stamp head; this body never executes.
    if not inspector.has_table(_TABLE):
        return
    missing = _missing_columns(conn)
    if not missing:
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    for name in missing:
        op.execute(f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS {name} {_COLUMNS[name]}")


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if not inspector.has_table(_TABLE):
        return
    present = [name for name in _COLUMNS if name not in _missing_columns(conn)]
    if not present:
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    for name in present:
        op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS {name}")
