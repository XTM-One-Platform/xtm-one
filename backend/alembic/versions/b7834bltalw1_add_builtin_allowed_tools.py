"""add agents.builtin_allowed_tools and assignments.builtin_allowed_tools

Built-in tools consolidation, stage 2a PR 3: per-function scoping of a
built-in tool group, with the same shape and semantics as
``mcp_allowed_tools`` (group id -> list of function names, missing or null =
every function). NULL on both tables = byte-identical behaviour to before.

Two nullable ``ADD COLUMN`` without default (metadata-only), following the
lock-safe recipe: idempotency probe, ``terminate_lock_holders``,
``set_lock_timeout``, then the guarded DDL. Fresh installs run ``create_all``
+ ``stamp head``: the body never executes there.

Revision ID: b7834bltalw1
Revises: f7602fltbdg1
Create Date: 2026-11-03 06:10:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "b7834bltalw1"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "f7602fltbdg1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLUMN = "builtin_allowed_tools"
_TABLES: tuple[str, ...] = ("agents", "assignments")


def _has_column(conn, table: str) -> bool:
    return (
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = current_schema() "
                "AND table_name = :table AND column_name = :column"
            ),
            {"table": table, "column": _COLUMN},
        ).first()
        is not None
    )


def upgrade() -> None:
    conn = op.get_bind()
    for table in _TABLES:
        if _has_column(conn, table):
            continue
        terminate_lock_holders(conn, table)
        set_lock_timeout(conn)
        op.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {_COLUMN} JSON")


def downgrade() -> None:
    conn = op.get_bind()
    for table in _TABLES:
        if not _has_column(conn, table):
            continue
        terminate_lock_holders(conn, table)
        set_lock_timeout(conn)
        op.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS {_COLUMN}")
