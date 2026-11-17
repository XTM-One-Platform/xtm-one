"""add platform wait bounds to general_assistant_config

Adds the two values that bound the agentic ``wait`` tool — the agent's timer —
now that a pause no longer consumes an agentic-loop iteration:

  - ``maximum_wait_time`` (INTEGER NOT NULL DEFAULT 300) — longest single wait.
  - ``maximum_total_wait_time`` (INTEGER NOT NULL DEFAULT 1800) — total time one
    run may spend waiting, the cumulative bound that keeps an iteration-free
    wait from becoming an unbounded loop.

Both are constant-default ``ADD COLUMN`` (metadata-only on PG 11+) and follow
the canonical lock-safe recipe: idempotency probe -> terminate_lock_holders ->
set_lock_timeout -> guarded DDL.  ``general_assistant_config`` is a singleton
table read by every chat turn, so it is exactly the "hot table with long-lived
readers" case the recipe exists for.

The defaults are kept in lockstep with ``app.tools.wait_policy``
(``DEFAULT_MAXIMUM_WAIT_TIME`` / ``DEFAULT_MAXIMUM_TOTAL_WAIT_TIME``) so a row
created by the DB default matches the Python default end-to-end.

Chains onto ``g1novasrc00``, main's head after the
Nova/favorites fork was collapsed.  This revision has been re-pointed forward
every time main moved; the last move (from ``f9u2fav0001``) happened because
the collapse landed thirteen minutes before this one and left it on a parent
that was no longer the tip.  Re-pointing is safe here for the same reason as before: main
has had an ambiguous head ever since, so ``alembic upgrade head`` has refused
to run and no database can be stamped at this revision.  Leaving it on a stale
parent gives the history two Alembic heads,
which breaks more than one thing: the startup runner calls
``alembic upgrade "head"`` (singular), which refuses to run with multiple heads,
and CI's stepwise ``upgrade +1`` walk dies with "Ambiguous walk".

Revision ID: e7a3wait2698
Revises: g1novasrc00
Create Date: 2026-07-31 10:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

# revision identifiers, used by Alembic.
revision: str = "e7a3wait2698"
down_revision: str | None = "g1novasrc00"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "general_assistant_config"

# (column, DDL type + constraint clause) — literal defaults rather than an
# import from app.tools.wait_policy: a migration body must keep producing the
# same SQL after the application constant is retuned.
_COLUMNS: tuple[tuple[str, str], ...] = (
    ("maximum_wait_time", "INTEGER NOT NULL DEFAULT 300"),
    ("maximum_total_wait_time", "INTEGER NOT NULL DEFAULT 1800"),
)


def _column_exists(conn, table: str, column: str) -> bool:
    # Scope the probe to the active schema — information_schema.columns spans
    # ALL schemas, so a same-named table elsewhere must not make the probe
    # skip the ADD COLUMN in the schema being migrated.
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

    for column, ddl_type in _COLUMNS:
        # 1. Idempotency probe — steady-state re-runs take NO lock.
        if _column_exists(conn, _TABLE, column):
            continue
        # 2. Pre-flight: kill same-role lock holders so the ALTER is instant.
        terminate_lock_holders(conn, _TABLE)
        # 3. Safety belt: fail fast (55P03) instead of wedging the lock queue.
        set_lock_timeout(conn)
        # 4. Guarded, constant-default (metadata-only) DDL.
        op.execute(
            f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS {column} {ddl_type}"
        )


def downgrade() -> None:
    conn = op.get_bind()

    for column, _ddl_type in _COLUMNS:
        if not _column_exists(conn, _TABLE, column):
            continue
        terminate_lock_holders(conn, _TABLE)
        set_lock_timeout(conn)
        op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS {column}")
