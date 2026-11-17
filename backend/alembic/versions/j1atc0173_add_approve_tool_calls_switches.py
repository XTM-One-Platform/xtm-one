"""add the per-tool approval switches, off on an upgraded install

``agents.approve_tool_calls`` turns the per-tool gate on or off for one agent;
NULL follows ``platform_settings.approve_tool_calls_default``. The gate used to
read ``agents.human_in_the_loop``, which keeps its prompt-driven meaning.

An upgrade switches approval on for no agent, existing or created later: the
install default is added FALSE on the existing row, then its column default
becomes TRUE so a fresh install (``create_all``) starts with it on. No agent
row is written.

Also puts ``agents.human_in_the_loop``'s default back to main's FALSE, for a
database that ran an earlier cut of ``e5rel3298``.

Revision ID: j1atc0173
Revises: i1seed3311
Create Date: 2026-09-29 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "j1atc0173"
down_revision: str | None = "i1seed3311"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _columns(inspector, table: str) -> set[str]:
    return {c["name"] for c in inspector.get_columns(table)}


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    if inspector.has_table("agents") and "approve_tool_calls" not in _columns(inspector, "agents"):
        terminate_lock_holders(conn, "agents")
        set_lock_timeout(conn)
        op.execute("ALTER TABLE agents ADD COLUMN IF NOT EXISTS approve_tool_calls BOOLEAN")
        op.execute("ALTER TABLE agents ALTER COLUMN human_in_the_loop SET DEFAULT FALSE")

    if inspector.has_table("platform_settings") and "approve_tool_calls_default" not in _columns(
        inspector, "platform_settings"
    ):
        terminate_lock_holders(conn, "platform_settings")
        set_lock_timeout(conn)
        # Two statements on purpose: the existing (upgraded) row reads FALSE,
        # a row created afterwards by a fresh install reads TRUE.
        op.execute(
            "ALTER TABLE platform_settings ADD COLUMN IF NOT EXISTS "
            "approve_tool_calls_default BOOLEAN NOT NULL DEFAULT FALSE"
        )
        op.execute("ALTER TABLE platform_settings ALTER COLUMN approve_tool_calls_default SET DEFAULT TRUE")


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    for table, column in (
        ("platform_settings", "approve_tool_calls_default"),
        ("agents", "approve_tool_calls"),
    ):
        if not inspector.has_table(table) or column not in _columns(inspector, table):
            continue
        terminate_lock_holders(conn, table)
        set_lock_timeout(conn)
        op.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS {column}")
