"""add integrations.oauth_token_owner_id (whose OAuth token it is)

The personal OAuth token rule reads ``integrations.user_id`` as
the owner of the stored token. An owner transfer changes ``user_id`` and keeps
the token, so the new owner holds someone else's connected account with
nothing saying so. ``oauth_token_owner_id`` records who connected the stored
token when that is not the integration's owner; NULL means the owner's own
(every existing row, and every token stored by the owner's authorization).

Lock safety: canonical recipe - idempotency probe + early return, then
``terminate_lock_holders`` + ``set_lock_timeout``, then a metadata-only
``ADD COLUMN IF NOT EXISTS`` of a nullable column with no default (no table
rewrite, no backfill). No foreign key (it would lock ``users``) and no index:
the column is only ever read alongside the whole row.

Revision ID: o3950tokown01
Revises: b8ack2642001
Create Date: 2026-09-27 10:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

# revision identifiers, used by Alembic.
revision: str = "o3950tokown01"
# Chains after this branch's backfill; ``b8ack2642001`` is the one re-pointed
# onto main's current head (backend/AGENTS.md > "Database migrations (safety)",
# rule 1).
down_revision: str | None = "b8ack2642001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "integrations"
_COLUMN = "oauth_token_owner_id"


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
        op.execute(f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS {_COLUMN} UUID")


def downgrade() -> None:
    conn = op.get_bind()
    if not _table_exists(conn, _TABLE):
        return
    if _column_exists(conn, _TABLE, _COLUMN):
        terminate_lock_holders(conn, _TABLE)
        set_lock_timeout(conn)
        op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS {_COLUMN}")
