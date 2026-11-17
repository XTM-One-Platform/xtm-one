"""add worksets.max_auto_calls / auto_calls_used — the spend ceiling

The automatic summarisation pass makes one LLM call per ingested item and had
no budget of its own: the only bound was ``max_items``, which bounds STORAGE.
They are not the same bound — a correction re-summarises a row that already
counted, a transient retry buys another call, and a borrowed document its owner
re-embeds fires the pass once per referencing workset. So the spend of a
feature whose premise is bulk ingestion was unbounded over a corpus's life.

``max_auto_calls`` is that ceiling, ``auto_calls_used`` the counter it is
checked against. The default of 200 covers the motivating window (120 reports)
with room for revisions; a larger corpus asks for a larger budget rather
than discovering the cost afterwards.

Backfill note: existing rows read as ``200 / 0``, i.e. they get a full fresh
budget rather than being retroactively charged for passes already made. The
alternative — seeding ``auto_calls_used`` from ``item_count`` — would invent a
spend figure nobody measured, and could exhaust a live corpus's budget the
moment this deploys.

Additive and lock-light: ``ADD COLUMN ... DEFAULT <constant> NOT NULL`` is
metadata-only on PG 11+ (no table rewrite). Old replicas during a rolling
deploy never select columns they do not know about.

Revision ID: w3ks3097003
Revises: w2ks3074002
Create Date: 2026-08-25 19:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "w3ks3097003"
down_revision: str | Sequence[str] | None = "w2ks3074002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "worksets"
_COLUMNS = (
    ("max_auto_calls", "INTEGER NOT NULL DEFAULT 200"),
    ("auto_calls_used", "INTEGER NOT NULL DEFAULT 0"),
)


def _has_column(conn, table: str, column: str) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = current_schema() "
                "AND table_name = :t AND column_name = :c"
            ),
            {"t": table, "c": column},
        ).scalar()
    )


def _has_table(conn, name: str) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = current_schema() AND table_name = :t"
            ),
            {"t": name},
        ).scalar()
    )


def upgrade() -> None:
    conn = op.get_bind()
    if not _has_table(conn, _TABLE):
        return

    missing = [
        (name, ddl) for name, ddl in _COLUMNS if not _has_column(conn, _TABLE, name)
    ]
    # Steady-state re-run (a fresh install stamped head, a replica retried)
    # takes no lock at all.
    if not missing:
        return

    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    for name, ddl in missing:
        op.execute(
            sa.text(f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS {name} {ddl}")
        )


def downgrade() -> None:
    conn = op.get_bind()
    if not _has_table(conn, _TABLE):
        return

    present = [name for name, _ddl in _COLUMNS if _has_column(conn, _TABLE, name)]
    if not present:
        return

    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    for name in present:
        op.execute(sa.text(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS {name}"))
