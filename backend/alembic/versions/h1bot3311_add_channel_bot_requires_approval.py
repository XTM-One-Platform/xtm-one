"""add channel_bots.requires_approval

A channel bot contributes tools that send, edit and delete messages in
somebody's Slack, Teams or Discord, and it had no per-connection approval
switch — only the platform-wide whitelist, which decides for every bot at once.
This is the counterpart of ``integrations.requires_approval``.

Defaults TRUE: a bot that was gating before the upgrade keeps gating, and one
configured afterwards gates until somebody decides otherwise.

Revision ID: h1bot3311
Revises: d4a2all3282
Create Date: 2026-09-03 13:10:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "h1bot3311"
down_revision: str | None = "d4a2all3282"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if not inspector.has_table("channel_bots"):
        return
    columns = {c["name"] for c in inspector.get_columns("channel_bots")}
    if "requires_approval" in columns:
        return
    terminate_lock_holders(conn, "channel_bots")
    set_lock_timeout(conn)
    op.execute(
        "ALTER TABLE channel_bots ADD COLUMN IF NOT EXISTS "
        "requires_approval BOOLEAN NOT NULL DEFAULT TRUE"
    )


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if not inspector.has_table("channel_bots"):
        return
    # Probe before locking, like the upgrade. Without it a steady-state rerun
    # terminated lock holders and asked for ACCESS EXCLUSIVE to drop a column
    # that was already gone.
    columns = {c["name"] for c in inspector.get_columns("channel_bots")}
    if "requires_approval" not in columns:
        return
    terminate_lock_holders(conn, "channel_bots")
    set_lock_timeout(conn)
    op.execute("ALTER TABLE channel_bots DROP COLUMN IF EXISTS requires_approval")
