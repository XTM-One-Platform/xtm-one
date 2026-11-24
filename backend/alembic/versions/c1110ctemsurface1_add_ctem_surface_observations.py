"""add ctem_surface_observations (CTEM-26, the outside lens baseline)

One row per (organization, host) the attack surface pass saw through a
connected provider (#1110). Purely additive: one new table, nothing
backfilled, so old replicas during a rolling deploy ignore it. Design:
dev-docs/ctem-attack-surface.md.

Revision ID: c1110ctemsurface1
Revises: c943decisionsbf1
Create Date: 2026-11-22 07:10:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "c1110ctemsurface1"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "c943decisionsbf1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "ctem_surface_observations"


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
        sa.Column("host", sa.String(300), nullable=False),
        sa.Column("ip", sa.String(64), nullable=False, server_default=""),
        sa.Column("ports", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("cert_names", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("dns_target", sa.String(300), nullable=False, server_default=""),
        sa.Column("source_provider", sa.String(40), nullable=False, server_default=""),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("signalled", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("organization_id", "host", name="uq_ctem_surface_observations_org_host"),
        if_not_exists=True,
    )
    op.create_index("ix_ctem_surface_observations_organization_id", _TABLE, ["organization_id"], if_not_exists=True)


def downgrade() -> None:
    conn = op.get_bind()
    set_lock_timeout(conn)
    if _has_table(conn, _TABLE):
        op.drop_table(_TABLE, if_exists=True)
