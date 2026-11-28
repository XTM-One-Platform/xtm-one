"""last verified signal-source delivery per integration

Adds ``ctem_integration_deliveries``: one row per ``(provider, integration_id)``
holding when a verified signal-source delivery last reached XTM One, so the
signal sources drawer tells "On" from "On, no delivery yet" (#2736). The column
set mirrors ``app/models/ctem_integration_delivery.py``: a fresh install runs
``create_all`` + ``stamp head`` while an existing install runs this body, and the
two must converge.

**Lock safety.** A new table with no foreign key: idempotency probe and early
return, ``set_lock_timeout``, guarded DDL. The downgrade drops the table with the
same recipe.

Revision ID: d2736intdeliv1
Revises: p2638ownerrd01
Create Date: 2026-11-28 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "d2736intdeliv1"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "p2638ownerrd01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "ctem_integration_deliveries"


def _table_exists(conn: sa.engine.Connection, table: str) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.tables WHERE table_schema = current_schema() AND table_name = :table"
            ),
            {"table": table},
        ).scalar()
    )


def upgrade() -> None:
    conn = op.get_bind()
    if _table_exists(conn, _TABLE):
        return
    set_lock_timeout(conn)
    op.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {_TABLE} (
            provider VARCHAR(50) NOT NULL,
            integration_id VARCHAR(64) NOT NULL,
            last_delivery_at TIMESTAMPTZ NOT NULL,
            PRIMARY KEY (provider, integration_id)
        )
        """
    )


def downgrade() -> None:
    conn = op.get_bind()
    if not _table_exists(conn, _TABLE):
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    op.execute(f"DROP TABLE IF EXISTS {_TABLE}")
