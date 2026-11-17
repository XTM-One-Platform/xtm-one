"""add users.organization_context_notice_dismissed

The command center's "Your organization context is incomplete" notice
(#11586): shown to administrators only and dismissible per person. The column
keeps the organization context fields that were missing when the person
closed it (``[path]``), so the notice comes back only when a field outside
that list becomes missing. A pure UI preference: no behaviour depends on it
server-side; briefs keep their own notice, never dismissible.

Nullable JSON with no default, so this is a metadata-only ``ADD COLUMN`` (no
table rewrite), following the canonical lock-safe recipe: idempotency probe ->
terminate_lock_holders -> set_lock_timeout -> guarded DDL. NULL is read as
"never dismissed", which is why no backfill is needed.

Revision ID: orgnotice11586
Revises: c11545ctemcases1
Create Date: 2026-11-13 11:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

# revision identifiers, used by Alembic.
revision: str = "orgnotice11586"
# NOT a constant: must always be main's CURRENT head, re-pointed on every rebase
# (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | None = "c11545ctemcases1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "users"
_COLUMN = "organization_context_notice_dismissed"
_TYPE = "JSON"


def _has_column(conn) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = current_schema() "
                "AND table_name = :table AND column_name = :column"
            ),
            {"table": _TABLE, "column": _COLUMN},
        ).first()
    )


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    # Fresh installs run create_all + stamp head; this body never executes.
    if not inspector.has_table(_TABLE):
        return
    if _has_column(conn):
        return

    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    op.execute(f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS {_COLUMN} {_TYPE}")


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    if not inspector.has_table(_TABLE):
        return
    if not _has_column(conn):
        return

    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS {_COLUMN}")
