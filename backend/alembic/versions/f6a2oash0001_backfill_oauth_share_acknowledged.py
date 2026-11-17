"""backfill NULL integrations.oauth_share_acknowledged and enforce NOT NULL

``b7f1e6a93c02`` added ``integrations.oauth_share_acknowledged`` with
``ADD COLUMN IF NOT EXISTS ... NOT NULL DEFAULT FALSE``, but its
idempotency probe (``_column_exists``) skipped that DDL entirely on any
database where the column already existed from another path — the
``_ensure_model_columns`` safety net in ``app/core/migrations.py`` adds
model-defined columns "as nullable without defaults or constraints" ahead
of the real migration whenever a DB is stamped at/past a revision without
ever running its body. The model itself also lacked ``nullable=False`` /
``server_default`` (Python-side ``default=False`` only fires on ORM
flush), so a bare-nullable column created either way was never converged
onto the intended schema. Existing rows kept ``NULL``, and
``IntegrationResponse.oauth_share_acknowledged`` (a non-optional ``bool``)
raised on serialization — ``GET /api/v1/integrations`` 500ing on any
install with such a row.

Backfills NULLs to FALSE, then sets the column DEFAULT and NOT NULL to
match the model (``app/models/integration.py``). Idempotent: re-runs are a
no-op once converged. Follows the canonical lock-safe recipe: idempotency
probe -> terminate_lock_holders -> set_lock_timeout -> guarded DDL.

Revision ID: f6a2oash0001
Revises: b2c3agex0001
Create Date: 2026-07-30 13:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

# revision identifiers, used by Alembic.
revision: str = "f6a2oash0001"
down_revision: str | None = "b2c3agex0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "integrations"
_COLUMN = "oauth_share_acknowledged"


def _column_state(conn):
    """Return ``(is_nullable, column_default)`` for the column, or ``None`` if missing."""
    return conn.execute(
        sa.text(
            "SELECT is_nullable, column_default FROM information_schema.columns "
            "WHERE table_schema = current_schema() "
            "AND table_name = :table AND column_name = :column"
        ),
        {"table": _TABLE, "column": _COLUMN},
    ).one_or_none()


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    # Fresh installs run create_all (already NOT NULL DEFAULT FALSE per the
    # model) + stamp head; this body never executes for them.
    if not inspector.has_table(_TABLE):
        return

    state = _column_state(conn)
    if state is None:
        # Defensive: some install skipped column creation entirely (the
        # exact ``_column_exists`` guard gap this migration closes for
        # everyone else). Create it correctly from scratch.
        terminate_lock_holders(conn, _TABLE)
        set_lock_timeout(conn)
        op.execute(f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS {_COLUMN} BOOLEAN NOT NULL DEFAULT FALSE")
        return

    is_nullable, column_default = state
    if is_nullable == "NO" and column_default is not None:
        return  # already converged - no lock taken

    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    # Backfill before SET NOT NULL - Postgres refuses the constraint while
    # any row is still NULL.
    op.execute(f"UPDATE {_TABLE} SET {_COLUMN} = FALSE WHERE {_COLUMN} IS NULL")  # noqa: S608
    op.execute(f"ALTER TABLE {_TABLE} ALTER COLUMN {_COLUMN} SET DEFAULT FALSE")
    op.execute(f"ALTER TABLE {_TABLE} ALTER COLUMN {_COLUMN} SET NOT NULL")


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if not inspector.has_table(_TABLE):
        return
    if _column_state(conn) is None:
        return

    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    op.execute(f"ALTER TABLE {_TABLE} ALTER COLUMN {_COLUMN} DROP NOT NULL")
    op.execute(f"ALTER TABLE {_TABLE} ALTER COLUMN {_COLUMN} DROP DEFAULT")
