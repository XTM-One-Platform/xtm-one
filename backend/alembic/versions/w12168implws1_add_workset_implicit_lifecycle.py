"""add worksets.implicit and worksets.auto_calls_window_started_at

Issue #12168: the worksets the runtime derives on its own (an agent's chat
corpus, an assignment's corpus) are IMPLICIT. ``implicit`` marks them so they
get a sliding idle expiry, a rolling automatic-summarisation budget and
rotation once resolved or sealed; ``auto_calls_window_started_at`` opens the
budget window ``auto_calls_used`` counts in. Existing rows are marked by the
runtime on their next use, so no backfill runs here.

One constant-default and one nullable ``ADD COLUMN``, metadata-only on PG 11+.
Idempotent via a column-existence probe and lock-safe per the canonical
recipe: idempotency probe -> terminate_lock_holders -> set_lock_timeout ->
guarded DDL. Fresh installs run ``create_all`` + ``stamp head``: the body
never executes there.

Revision ID: w12168implws1
Revises: w12165wscnt1
Create Date: 2026-11-16 04:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "w12168implws1"
# NOT a constant: must always be main's CURRENT head, re-pointed on every rebase
# (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "w12165wscnt1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "worksets"
_COLUMNS: tuple[tuple[str, str], ...] = (
    ("implicit", "BOOLEAN NOT NULL DEFAULT false"),
    ("auto_calls_window_started_at", "TIMESTAMP WITH TIME ZONE"),
)


def _has_column(conn: sa.engine.Connection, column: str) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = current_schema() "
                "AND table_name = :table AND column_name = :column"
            ),
            {"table": _TABLE, "column": column},
        ).first()
    )


def upgrade() -> None:
    conn = op.get_bind()
    if not sa.inspect(conn).has_table(_TABLE):
        return
    missing = [(name, definition) for name, definition in _COLUMNS if not _has_column(conn, name)]
    if not missing:
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    for name, definition in missing:
        op.execute(f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS {name} {definition}")


def downgrade() -> None:
    conn = op.get_bind()
    if not sa.inspect(conn).has_table(_TABLE):
        return
    present = [name for name, _definition in _COLUMNS if _has_column(conn, name)]
    if not present:
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    for name in present:
        op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS {name}")
