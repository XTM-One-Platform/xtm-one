"""add ctem_surface_passes (CTEM-26, what each attack surface pass read)

One row per committed attack surface pass: the providers it read, what each
read covered per owned domain or range (pages, records, truncated), the hosts
observed and the signals per path (#1498). The next pass reads the latest row
so a host a previous partial read missed is not raised as a new host (path 7).
Purely additive: one new table, nothing backfilled, so old replicas during a
rolling deploy ignore it. Design: dev-docs/ctem-attack-surface.md.

Revision ID: c1498surfacepass1
Revises: s1495software01
Create Date: 2026-11-23 11:50:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "c1498surfacepass1"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "s1495software01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "ctem_surface_passes"


def _has_table(conn, name: str) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.tables WHERE table_schema = current_schema() AND table_name = :n"
            ),
            {"n": name},
        ).scalar()
    )


def upgrade() -> None:
    conn = op.get_bind()
    # Idempotency probe: fresh installs run create_all + stamp head, and a crashed replica may retry.
    if _has_table(conn, _TABLE):
        return
    # The foreign key to `organizations` installs its trigger there: clear the holders first.
    terminate_lock_holders(conn, "organizations")
    set_lock_timeout(conn)
    op.create_table(
        _TABLE,
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("provider", sa.String(40), nullable=False, server_default=""),
        sa.Column("observed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("coverage", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("statuses", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("counts", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("previous_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        if_not_exists=True,
    )
    op.create_index("ix_ctem_surface_passes_org_at", _TABLE, ["organization_id", "at"], if_not_exists=True)


def downgrade() -> None:
    conn = op.get_bind()
    set_lock_timeout(conn)
    if _has_table(conn, _TABLE):
        op.drop_table(_TABLE, if_exists=True)
