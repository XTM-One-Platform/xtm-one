"""add agents.catalog_include_personal_tools

The publisher's choice of whether the Filigran AI Catalog download of an agent
carries the code of the custom tools bound to it that are not company-managed.
Off for a new publish. Agents already published when this column lands keep
the behaviour they had (the code was always included), so the rows with
``is_public_catalog`` are set to true in the same transaction as the ADD: the
change never happens silently for a publisher.

Lock profile: a ``NOT NULL`` column with a CONSTANT default is metadata-only on
PG 11+, but the ALTER still takes ACCESS EXCLUSIVE on ``agents``, a hot table
(every chat turn and run reads it), so an open transaction that merely read it
queues the ALTER behind itself in a rolling deploy. Canonical pre-flight:
idempotency probe -> terminate_lock_holders -> set_lock_timeout -> guarded DDL.
The backfill is bounded by the published agents (a handful of rows) and runs
only when the column is created: the probe returns early on a re-run, so a
publisher who turned the switch off is never switched back on.

Revision ID: c4249tools01
Revises: s2977stale01
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

# revision identifiers, used by Alembic.
revision: str = "c4249tools01"
down_revision: str | None = "s2977stale01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "agents"
_COLUMN = "catalog_include_personal_tools"


def _column_exists(conn, table: str, column: str) -> bool:
    # Scoped to the active schema - information_schema.columns spans ALL
    # schemas, so a same-named table elsewhere must not make the probe skip
    # the ADD COLUMN in the schema being migrated.
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = current_schema() "
                "AND table_name = :table AND column_name = :column"
            ),
            {"table": table, "column": column},
        ).scalar()
    )


def upgrade() -> None:
    conn = op.get_bind()
    # 1. Idempotency probe - steady-state re-runs take NO lock.
    if _column_exists(conn, _TABLE, _COLUMN):
        return
    # 2. Pre-flight: kill same-role lock holders on the table the DDL locks.
    terminate_lock_holders(conn, _TABLE)
    # 3. Safety belt: fail fast (55P03) instead of wedging the lock queue.
    set_lock_timeout(conn)
    # 4. Guarded DDL - constant default: metadata-only, no rewrite.
    op.execute(
        f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS {_COLUMN} "
        "BOOLEAN NOT NULL DEFAULT false"
    )
    # 5. Agents already published keep including the code, as they did.
    op.execute(
        "UPDATE agents SET catalog_include_personal_tools = true "
        "WHERE is_public_catalog IS TRUE"
    )


def downgrade() -> None:
    conn = op.get_bind()
    if not _column_exists(conn, _TABLE, _COLUMN):
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS {_COLUMN}")
