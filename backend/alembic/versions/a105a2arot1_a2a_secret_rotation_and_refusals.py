"""keep the A2A client id across a secret rotation, record refused token requests (#105)

``mcp_oauth_clients.secret_rotated_at``: when an ``oauth_a2a`` partner credential's
secret was last rotated in place. A regenerate now keeps the client id and
replaces the secret hash on the same row, so a bearer minted before the
rotation is refused by comparing its ``iat`` with this stamp (it used to be
refused because the row was deleted).

``a2a_integration_settings.last_refused_token_at`` / ``last_refused_client_id``
/ ``last_refused_reason``: the last token request an INBOUND ``oauth_a2a``
integration refused, shown to its managers. Never a secret: a time, the client
id the caller presented (an identifier it sends in clear) and a reason code.
``retired_client_ids``: the client ids an explicit "Rotate client id and
secret" retired, so a partner still presenting one is told apart from a
client id nobody issued.

Nullable columns with no default: metadata-only on PG 11+, no rewrite. It
follows the lock-safe recipe (idempotency probe, ``terminate_lock_holders``,
``set_lock_timeout``, guarded DDL) both ways.

Revision ID: a105a2arot1
Revises: a12346a2acred1
Create Date: 2026-11-18 08:10:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "a105a2arot1"
# NOT a constant: must always be main's CURRENT head, re-pointed on every rebase
# (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "a12346a2acred1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLUMNS: dict[str, tuple[tuple[str, str], ...]] = {
    "mcp_oauth_clients": (("secret_rotated_at", "TIMESTAMP WITH TIME ZONE"),),
    "a2a_integration_settings": (
        ("last_refused_token_at", "TIMESTAMP WITH TIME ZONE"),
        ("last_refused_client_id", "VARCHAR(255)"),
        ("last_refused_reason", "VARCHAR(32)"),
        ("retired_client_ids", "JSON"),
    ),
}


def _present(conn: sa.engine.Connection, table: str) -> set[str]:
    return set(
        conn.execute(
            sa.text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = :table"
            ),
            {"table": table},
        ).scalars()
    )


def upgrade() -> None:
    conn = op.get_bind()
    for table, columns in _COLUMNS.items():
        missing = [name for name, _ in columns if name not in _present(conn, table)]
        if not missing:
            continue
        terminate_lock_holders(conn, table)
        set_lock_timeout(conn)
        for name, kind in columns:
            if name in missing:
                op.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {name} {kind}")


def downgrade() -> None:
    conn = op.get_bind()
    for table, columns in _COLUMNS.items():
        present = [name for name, _ in columns if name in _present(conn, table)]
        if not present:
            continue
        terminate_lock_holders(conn, table)
        set_lock_timeout(conn)
        for name in present:
            op.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS {name}")
