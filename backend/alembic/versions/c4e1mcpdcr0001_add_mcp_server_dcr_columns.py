"""add mcp_servers dynamic-client-registration columns

Adds the three columns that let a remote MCP server be connected over OAuth
without a pre-provisioned ``client_id`` / ``client_secret``:

* ``registration_url`` — the RFC 7591 dynamic client registration endpoint,
  discovered from the server's OAuth metadata (RFC 9728 -> RFC 8414) or
  configured by hand.
* ``oauth_resource`` — the RFC 8707 canonical resource identifier sent as the
  ``resource`` parameter on the authorize/token requests, which the MCP
  authorization spec requires so a token minted for one MCP server cannot be
  replayed against another.
* ``dcr_registered_at`` — set when ``client_id_enc`` was minted by dynamic
  registration rather than typed in, marking the client as disposable: an
  authorization server that later forgets it is answered by re-registering.
* ``oauth_metadata_url`` — the protected-resource metadata URL learned from a
  ``401 WWW-Authenticate: Bearer resource_metadata="…"`` challenge. RFC 9728
  allows that URL to sit on a host or path no well-known probe derived from
  the MCP URL would reach, so the challenge value is authoritative and must be
  remembered rather than re-derived.

All four are nullable with no default, so this is a metadata-only
``ADD COLUMN`` (no table rewrite on PG 11+), following the canonical lock-safe
recipe: idempotency probe -> terminate_lock_holders -> set_lock_timeout ->
guarded DDL.

Revision ID: c4e1mcpdcr0001
Revises: c5g9tou20001
Create Date: 2026-08-06 10:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

# revision identifiers, used by Alembic.
revision: str = "c4e1mcpdcr0001"
down_revision: str | None = "c5g9tou20001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "mcp_servers"
_COLUMNS: dict[str, str] = {
    "registration_url": "VARCHAR(500)",
    "oauth_resource": "VARCHAR(2000)",
    "dcr_registered_at": "TIMESTAMP WITH TIME ZONE",
    "oauth_metadata_url": "VARCHAR(2000)",
}


def _missing_columns(conn) -> list[str]:
    existing = {
        row[0]
        for row in conn.execute(
            sa.text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = :table"
            ),
            {"table": _TABLE},
        )
    }
    return [name for name in _COLUMNS if name not in existing]


def _present_columns(conn) -> list[str]:
    existing = {
        row[0]
        for row in conn.execute(
            sa.text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = :table"
            ),
            {"table": _TABLE},
        )
    }
    return [name for name in _COLUMNS if name in existing]


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    # Fresh installs run create_all + stamp head; this body never executes.
    if not inspector.has_table(_TABLE):
        return

    missing = _missing_columns(conn)
    if not missing:
        return

    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    for name in missing:
        op.execute(
            f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS {name} {_COLUMNS[name]}"
        )


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if not inspector.has_table(_TABLE):
        return

    present = _present_columns(conn)
    if not present:
        return

    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    for name in present:
        op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS {name}")
