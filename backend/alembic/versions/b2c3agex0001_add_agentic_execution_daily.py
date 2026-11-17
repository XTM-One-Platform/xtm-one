"""add agentic_execution_daily table (agentic execution telemetry)

Creates ``agentic_execution_daily``: one row per (UTC day, source) holding the
number of top-level agentic executions the install performed. Redis-buffered on
the hot path, flushed here by the autonomous scheduler, and read by the
anonymous usage telemetry gauges ``xtm_one_agentic_executions_count`` /
``xtm_one_agentic_executions_by_day``.

``CREATE TABLE IF NOT EXISTS`` takes no lock on any existing relation (there
are no readers of a table that does not exist yet), so the upgrade needs no
lock pre-flight. The downgrade's ``DROP TABLE`` does take ACCESS EXCLUSIVE, so
it follows the canonical recipe: idempotency probe -> terminate_lock_holders
-> set_lock_timeout -> guarded DDL.

The column set mirrors the ORM model exactly, including the ABSENCE of a
server default on ``updated_at`` (the model supplies it Python-side, like every
other ``updated_at`` in the schema): a fresh install runs ``create_all`` from
the model while an existing install runs this body, and the two must converge.

Revision ID: b2c3agex0001
Revises: e3d9plpem0001
Create Date: 2026-07-30 09:55:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

# revision identifiers, used by Alembic.
revision: str = "b2c3agex0001"
down_revision: str | None = "e3d9plpem0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "agentic_execution_daily"


def _table_exists(conn, table: str) -> bool:
    # Scope the probe to the active schema — information_schema spans ALL
    # schemas, so a same-named table elsewhere must not make the probe skip
    # the CREATE in the schema being migrated.
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = current_schema() AND table_name = :table"
            ),
            {"table": table},
        ).scalar()
    )


def upgrade() -> None:
    conn = op.get_bind()
    if _table_exists(conn, _TABLE):
        return
    op.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {_TABLE} (
            id UUID PRIMARY KEY,
            day DATE NOT NULL,
            source VARCHAR(30) NOT NULL,
            count INTEGER NOT NULL DEFAULT 0,
            updated_at TIMESTAMPTZ NOT NULL,
            CONSTRAINT uq_agentic_execution_daily_day_source UNIQUE (day, source)
        )
        """
    )
    # Index on the new (still empty) table — no CONCURRENTLY needed, nothing
    # can be waiting on a relation that was created in this transaction.
    op.execute(
        f"CREATE INDEX IF NOT EXISTS ix_{_TABLE}_day ON {_TABLE} (day)"
    )


def downgrade() -> None:
    conn = op.get_bind()
    if not _table_exists(conn, _TABLE):
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    op.execute(f"DROP TABLE IF EXISTS {_TABLE}")
