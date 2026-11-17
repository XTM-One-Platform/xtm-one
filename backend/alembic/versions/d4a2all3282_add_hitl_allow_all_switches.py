"""tool approval gate: platform kill switch + per-user allow-all

Adds the two wholesale suppression switches above the per-tool decision table:

* ``platform_settings.tool_approvals_enabled`` (default TRUE) — an
  administrator's platform-wide kill switch for the approval gate. Off means
  no tool call pauses for anyone; calls are recorded with an explicit
  ``auto_run_approvals_disabled`` outcome.
* ``users.approve_all_tools`` (default FALSE) — one user's own allow-all mode,
  the wholesale form of answering "Yes, always" per tool. Recorded as
  ``auto_run_user_allows_all``.

Both are constant-default ``ADD COLUMN``s, metadata-only on PG 11+. Idempotent
via a column-existence probe, and lock-safe per the canonical recipe:
idempotency probe -> terminate_lock_holders -> set_lock_timeout -> guarded DDL.
``users`` and ``platform_settings`` are both hot tables (every request reads
them), so the pre-flight matters here exactly like the 2026-07-05 outage
taught.

Revision ID: d4a2all3282
Revises: e5rel3298
Create Date: 2026-09-02 09:10:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

# revision identifiers, used by Alembic.
revision: str = "d4a2all3282"
down_revision: str | None = "e5rel3298"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: ``(table, column, type + default)``. The DDL is emitted as guarded SQL
#: rather than through ``op.add_column``/``op.drop_column``: the probe below
#: keeps a steady-state re-run lock-free, but AGENTS.md's recipe asks the
#: statement itself to carry ``IF NOT EXISTS`` / ``IF EXISTS`` — a probe and an
#: unguarded ALTER leave a window between the check and the lock in which a
#: retrying replica can lose the race.
_COLUMNS: list[tuple[str, str, str]] = [
    ("platform_settings", "tool_approvals_enabled", "BOOLEAN NOT NULL DEFAULT true"),
    ("users", "approve_all_tools", "BOOLEAN NOT NULL DEFAULT false"),
]


def _column_exists(conn, table: str, column: str) -> bool:
    return (
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = current_schema() "
                "AND table_name = :table AND column_name = :column"
            ),
            {"table": table, "column": column},
        ).first()
        is not None
    )


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    for table, column, definition in _COLUMNS:
        if not inspector.has_table(table):
            continue
        if _column_exists(conn, table, column):
            continue
        terminate_lock_holders(conn, table)
        set_lock_timeout(conn)
        op.execute(
            f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {definition}"
        )


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    for table, column, _definition in _COLUMNS:
        if not inspector.has_table(table):
            continue
        if not _column_exists(conn, table, column):
            continue
        terminate_lock_holders(conn, table)
        set_lock_timeout(conn)
        op.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS {column}")
