"""add runner_enrollment_keys.default_require_approval (XTM One Desktop preset)

XTM One Desktop enrolls the machine the user sits at from the Desktop
page with a ready-to-use preset: every capability enabled and shell commands
held for the user's approval in the chat. ``Runner.require_approval`` already
exists; what was missing is a way for an ENROLLMENT KEY to seed it, so a
fresh Desktop install lands usable and safe without a trip to the permission
editor. Fleet / MDM keys keep the historical default (no approval gate).

Lock safety: canonical recipe - idempotency probe + early return, then
``terminate_lock_holders`` + ``set_lock_timeout``, then a metadata-only
``ADD COLUMN IF NOT EXISTS`` with a constant default (no table rewrite on
PG 11+). No index: the column is only ever read alongside the whole row.

Revision ID: a7f3run0004
Revises: f1sha2dedup01
Create Date: 2026-09-25 08:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

# revision identifiers, used by Alembic.
revision: str = "a7f3run0004"
# NOT a constant: must always be main's CURRENT head, re-pointed on every rebase
# (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | None = "f1sha2dedup01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "runner_enrollment_keys"
_COLUMN = "default_require_approval"


def _table_exists(conn, table: str) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = current_schema() AND table_name = :table"
            ),
            {"table": table},
        ).scalar()
    )


def _column_exists(conn, table: str, column: str) -> bool:
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
    if not _table_exists(conn, _TABLE):
        # Fresh install: create_all builds the full table (column included)
        # and stamps head - this body never needs to run.
        return

    if not _column_exists(conn, _TABLE, _COLUMN):
        terminate_lock_holders(conn, _TABLE)
        set_lock_timeout(conn)
        op.execute(
            f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS {_COLUMN} "
            "BOOLEAN NOT NULL DEFAULT false"
        )


def downgrade() -> None:
    conn = op.get_bind()
    if not _table_exists(conn, _TABLE):
        return
    if _column_exists(conn, _TABLE, _COLUMN):
        terminate_lock_holders(conn, _TABLE)
        set_lock_timeout(conn)
        op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS {_COLUMN}")
