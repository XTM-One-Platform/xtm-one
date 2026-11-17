"""add platform_settings.roi_org_totals_visible

Whether every user also reads the organization-wide ROI totals (#9836, slice
R1, ``dev-docs/roi-outcomes.md`` 10.2). Default TRUE: Sam's "not only for
admin" (7 Nov 2026).

Constant-default ``ADD COLUMN``, metadata-only on PG 11+. Idempotent via a
column-existence probe and lock-safe per the canonical recipe: idempotency
probe -> terminate_lock_holders -> set_lock_timeout -> guarded DDL
(``platform_settings`` is read by every request).

Revision ID: h9836roiorg1
Revises: h9868wsitem1
Create Date: 2026-11-07 22:20:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

# revision identifiers, used by Alembic.
revision: str = "h9836roiorg1"
# NOT a constant: must always be main's CURRENT head, re-pointed on every rebase
# (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | None = "h9868wsitem1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "platform_settings"
_COLUMN = "roi_org_totals_visible"
_DEFINITION = "BOOLEAN NOT NULL DEFAULT true"


def _has_column(conn) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = current_schema() "
                "AND table_name = :table AND column_name = :column"
            ),
            {"table": _TABLE, "column": _COLUMN},
        ).first()
    )


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    # Fresh installs run create_all + stamp head; this body never executes.
    if not inspector.has_table(_TABLE) or _has_column(conn):
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    op.execute(f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS {_COLUMN} {_DEFINITION}")


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if not inspector.has_table(_TABLE) or not _has_column(conn):
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS {_COLUMN}")
