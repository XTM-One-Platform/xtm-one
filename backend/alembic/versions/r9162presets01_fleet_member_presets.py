"""fleet_member_presets: company-wide presets of member settings (R4 of #9162)

A preset is a named set of values for any of the three member fields
(conversation shared, collaboration instruction, organization scope), managed
by administrators (decision R-D3 A, ``dev-docs/ctem-fleets.md`` section 10.6).
The three built-in presets live in code, not in this table. A new table,
nothing rewritten; the lock-safe recipe covers the ``users`` foreign key.

Revision ID: r9162presets01
Revises: m9157runcap01
Create Date: 2026-11-06 22:40:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "r9162presets01"
# NOT a constant: must always be main's CURRENT head, re-pointed on every rebase
# (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "m9157runcap01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "fleet_member_presets"


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
    if _has_table(conn, _TABLE):
        return
    terminate_lock_holders(conn, "users")
    set_lock_timeout(conn)
    op.create_table(
        _TABLE,
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("values", sa.JSON(), nullable=False),
        sa.Column(
            "created_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        if_not_exists=True,
    )
    op.create_index("uq_fleet_member_presets_name", _TABLE, ["name"], unique=True, if_not_exists=True)


def downgrade() -> None:
    conn = op.get_bind()
    if _has_table(conn, _TABLE):
        terminate_lock_holders(conn, _TABLE)
        set_lock_timeout(conn)
        op.drop_index("uq_fleet_member_presets_name", table_name=_TABLE, if_exists=True)
        op.drop_table(_TABLE, if_exists=True)
