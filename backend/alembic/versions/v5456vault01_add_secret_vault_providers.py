"""add secret_vault_providers

The vault providers an administrator connects in Settings > Security > Secret
vaults, next to the ``SECRET_VAULT_<NAME>_*`` declarations that keep
working unchanged. Purely additive: a new table with no foreign key, so it
takes no lock on an existing table, and older replicas during a rolling deploy
ignore it. Nothing is backfilled: an install had no database provider before.

Revision ID: v5456vault01
Revises: d6623dept0001
Create Date: 2026-10-21 15:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_ddl import set_lock_timeout

revision: str = "v5456vault01"
# NOT a constant: must always be the base branch's CURRENT head, re-pointed
# whenever it gains a migration (backend/AGENTS.md > "Database migrations
# (safety)", rule 1).
down_revision: str | None = "d6623dept0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "secret_vault_providers"


def upgrade() -> None:
    conn = op.get_bind()
    if sa.inspect(conn).has_table(TABLE):
        return
    op.create_table(
        TABLE,
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(32), nullable=False, unique=True),
        sa.Column("display_name", sa.String(200), nullable=False),
        sa.Column("type", sa.String(20), nullable=False),
        sa.Column("settings", postgresql.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("credentials_enc", sa.Text(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("grants", postgresql.JSON(), nullable=False, server_default=sa.text("'[]'::json")),
        sa.Column("browsable", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("cache_seconds", sa.Integer(), nullable=True),
        sa.Column("last_check_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_check_ok", sa.Boolean(), nullable=True),
        sa.Column("last_check_reason", sa.String(40), nullable=True),
        sa.Column("last_check_detail", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by_id", postgresql.UUID(as_uuid=True), nullable=True),
    )


def downgrade() -> None:
    conn = op.get_bind()
    if not sa.inspect(conn).has_table(TABLE):
        return
    # A drop takes an ACCESS EXCLUSIVE lock on the table: bounded, so a reader in flight fails it fast.
    set_lock_timeout(conn)
    op.execute(f"DROP TABLE IF EXISTS {TABLE}")
