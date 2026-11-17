"""record the days whose llm_daily_stats rollup is stale

A repricing fills in the cost of traces a day's rollup was built without. The
rollup is insert-only, so the day has to be rebuilt, and a rebuild that has not
happened yet - a crash, a failed aggregation, a day past what one request
rebuilds - must not be lost. ``llm_daily_stats_stale_days`` records it with the
cost update itself, one row per day, and the rebuild clears it in the same
transaction that replaces the day's rows. The stale rows stay readable until
then: no day is ever left without its rollup.

Lock profile: ``CREATE TABLE`` of a new table with no foreign key takes no lock
any other session holds. Dropping it (downgrade) takes ACCESS EXCLUSIVE on the
table itself, so the downgrade follows the canonical recipe.

Revision ID: s2977stale01
Revises: c2797toudrop1
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

# revision identifiers, used by Alembic.
revision: str = "s2977stale01"
down_revision: str | None = "c2797toudrop1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "llm_daily_stats_stale_days"


def _table_exists(conn, table: str) -> bool:
    # Scoped to the active schema - information_schema spans ALL schemas.
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
            date DATE PRIMARY KEY,
            marked_at TIMESTAMPTZ NOT NULL DEFAULT now()
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
