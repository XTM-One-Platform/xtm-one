"""agent accounts: where the signature of a reply comes from

Adds to ``agent_accounts``:
  - ``signature_source`` (VARCHAR(20), NULL = ``own``) - the account's own
    signature, one read from a signature integration, or none.
  - ``signature_integration_id`` (UUID) - the signature integration read when
    the source says so. A plain column: an integration deleted under it reads
    as gone, and the reply carries the account's own signature.

Two nullable ``ADD COLUMN`` (metadata-only), under the canonical recipe:
probe -> terminate_lock_holders -> set_lock_timeout -> guarded DDL. Nothing
else, on purpose. The first version of this migration also built an index
``CONCURRENTLY`` and a foreign key to ``integrations``, and blocked a rolling
deploy: this body runs under the migration advisory lock, and a concurrent
build waits for every open transaction - the sibling workers waiting on that
lock included - so it never finishes; a foreign key also locks the
referenced, busy ``integrations`` table. Upgrading a database that run left
behind (the columns and the constraint committed, an index possibly INVALID)
drops that index with a plain, catalog-only ``DROP INDEX`` and keeps the
constraint, which is harmless.

Revision ID: u5715sig01
Revises: u5702mail01
Create Date: 2026-10-12 18:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "u5715sig01"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "u5702mail01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "agent_accounts"
#: What the first version of this migration created (see the docstring).
_LEFTOVER_INDEX = "ix_agent_accounts_signature_integration_id"
_LEFTOVER_FK = "agent_accounts_signature_integration_id_fkey"
_COLUMNS: tuple[tuple[str, str], ...] = (
    ("signature_source", "VARCHAR(20)"),
    ("signature_integration_id", "UUID"),
)


def _column_exists(conn, column: str) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = :table AND column_name = :column"
            ),
            {"table": _TABLE, "column": column},
        ).scalar()
    )


def _index_exists(conn) -> bool:
    return bool(
        conn.execute(
            sa.text("SELECT 1 FROM pg_indexes WHERE schemaname = current_schema() AND indexname = :name"),
            {"name": _LEFTOVER_INDEX},
        ).scalar()
    )


def _constraint_exists(conn) -> bool:
    return bool(
        conn.execute(sa.text("SELECT 1 FROM pg_constraint WHERE conname = :name"), {"name": _LEFTOVER_FK}).scalar()
    )


def upgrade() -> None:
    conn = op.get_bind()
    for column, ddl_type in _COLUMNS:
        if _column_exists(conn, column):
            continue
        terminate_lock_holders(conn, _TABLE)
        set_lock_timeout(conn)
        op.execute(f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS {column} {ddl_type}")
    if _index_exists(conn):
        terminate_lock_holders(conn, _TABLE)
        set_lock_timeout(conn)
        op.execute(f"DROP INDEX IF EXISTS {_LEFTOVER_INDEX}")


def downgrade() -> None:
    conn = op.get_bind()
    if _index_exists(conn):
        terminate_lock_holders(conn, _TABLE)
        set_lock_timeout(conn)
        op.execute(f"DROP INDEX IF EXISTS {_LEFTOVER_INDEX}")
    if _constraint_exists(conn):
        terminate_lock_holders(conn, _TABLE)
        set_lock_timeout(conn)
        op.execute(f"ALTER TABLE {_TABLE} DROP CONSTRAINT IF EXISTS {_LEFTOVER_FK}")
    for column, _ddl_type in reversed(_COLUMNS):
        if not _column_exists(conn, column):
            continue
        terminate_lock_holders(conn, _TABLE)
        set_lock_timeout(conn)
        op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS {column}")
