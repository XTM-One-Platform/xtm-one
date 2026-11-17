"""add quota_global_usage

The platform-wide agentic limits (the license ceilings in XTM One mode,
``platform_limit`` in Copilot mode) are enforced against the ``_global`` Redis
counters, which the quota flush could not persist: ``quota_usage.user_id`` is
NOT NULL with a foreign key to ``users``. After a Redis wipe every
platform-wide ceiling restarted from zero for the rest of its period.
``quota_global_usage`` holds their durable high-water mark, one row per quota
type and period, attributed to no user.

Lock profile: ``CREATE TABLE`` of a new table with no foreign key takes no lock
any other session holds, and ``quota_usage`` is left untouched. Dropping it
(downgrade) takes ACCESS EXCLUSIVE on the table itself, so the downgrade
follows the canonical recipe.

Revision ID: q4711glob01
Revises: a4732signin1
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

# revision identifiers, used by Alembic.
revision: str = "q4711glob01"
down_revision: str | None = "a4732signin1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "quota_global_usage"


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
            usage_type VARCHAR(30) NOT NULL,
            period VARCHAR(10) NOT NULL,
            count INTEGER NOT NULL DEFAULT 0,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (usage_type, period)
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
