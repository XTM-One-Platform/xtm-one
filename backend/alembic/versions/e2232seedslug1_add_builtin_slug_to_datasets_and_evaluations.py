"""add datasets.slug and evaluations.slug for built-in benchmarks

The stable identity the startup seed matches a built-in dataset or evaluation
by (``app/evaluations/seed.py``), the way ``agents.slug`` identifies a
built-in agent. Nullable, no default: every existing row reads as a
user-created dataset or evaluation, which is what it is, and the seed never
touches a row without a slug.

Lock profile: a nullable ``ADD COLUMN`` with no default is metadata-only on
PG 11+, but the ALTER still takes ACCESS EXCLUSIVE on its table, and the
evaluation engine reads both tables during a run, so any open transaction
that has read one queues the ALTER behind itself in a rolling deploy.
Canonical pre-flight per table: idempotency probe -> terminate_lock_holders
-> set_lock_timeout -> guarded DDL.

Plus one plain index per table (``ix_datasets_slug``, ``ix_evaluations_slug``,
the names ``index=True`` gives the model columns on a fresh install), built by
``create_index_concurrently`` in an autocommit block, outside the
``lock_timeout`` belt. Not UNIQUE: only the seed writes a slug, under the
bootstrap lock, and a unique index could not be built concurrently by the
shared helper.

Revision ID: e2232seedslug1
Revises: a4732signin1
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import (
    create_index_concurrently,
    set_lock_timeout,
    terminate_lock_holders,
)

# revision identifiers, used by Alembic.
revision: str = "e2232seedslug1"
down_revision: str | None = "a4732signin1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLUMN = "slug"
_TABLES = ("datasets", "evaluations")


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
    for table in _TABLES:
        # 1. Idempotency probe - steady-state re-runs take NO lock.
        if _column_exists(conn, table, _COLUMN):
            continue
        # 2. Pre-flight: kill same-role lock holders on the table the DDL locks.
        terminate_lock_holders(conn, table)
        # 3. Safety belt: fail fast (55P03) instead of wedging the lock queue.
        set_lock_timeout(conn)
        # 4. Guarded DDL - nullable, no default: metadata-only, no rewrite.
        op.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {_COLUMN} VARCHAR(200)")
    # Bounded, and an INVALID leftover of an interrupted build is dropped and
    # rebuilt rather than matched by IF NOT EXISTS (create_index_concurrently).
    with op.get_context().autocommit_block():
        for table in _TABLES:
            create_index_concurrently(conn, f"ix_{table}_{_COLUMN}", table, f"({_COLUMN})")


def downgrade() -> None:
    conn = op.get_bind()
    with op.get_context().autocommit_block():
        for table in _TABLES:
            op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS ix_{table}_{_COLUMN}")
    for table in _TABLES:
        if not _column_exists(conn, table, _COLUMN):
            continue
        terminate_lock_holders(conn, table)
        set_lock_timeout(conn)
        op.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS {_COLUMN}")
