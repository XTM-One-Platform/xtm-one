"""add the outbound A2A credential state to a2a_integration_settings (#12346)

``last_token_minted_at``, ``credential_error_code``, ``credential_error_detail``
and ``credential_error_at``: the state of an outbound partner credential, so
the integration's status can follow it and ``get_integration`` can show it.

Nullable columns with no default: metadata-only on PG 11+, no rewrite. It
follows the lock-safe recipe (idempotency probe, ``terminate_lock_holders``,
``set_lock_timeout``, guarded DDL) both ways.

Revision ID: a12346a2acred1
Revises: b12192bltsnap1
Create Date: 2026-11-17 08:10:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "a12346a2acred1"
# NOT a constant: must always be main's CURRENT head, re-pointed on every rebase
# (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "b12192bltsnap1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "a2a_integration_settings"
_COLUMNS = (
    ("last_token_minted_at", "TIMESTAMP WITH TIME ZONE"),
    ("credential_error_code", "VARCHAR(48)"),
    ("credential_error_detail", "TEXT"),
    ("credential_error_at", "TIMESTAMP WITH TIME ZONE"),
)


def _missing(conn: sa.engine.Connection) -> list[str]:
    present = set(
        conn.execute(
            sa.text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = :table"
            ),
            {"table": _TABLE},
        ).scalars()
    )
    return [name for name, _ in _COLUMNS if name not in present]


def upgrade() -> None:
    conn = op.get_bind()
    missing = set(_missing(conn))
    if not missing:
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    for name, kind in _COLUMNS:
        if name in missing:
            op.execute(f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS {name} {kind}")


def downgrade() -> None:
    conn = op.get_bind()
    present = {name for name, _ in _COLUMNS} - set(_missing(conn))
    if not present:
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    for name, _ in _COLUMNS:
        if name in present:
            op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS {name}")
