"""roi_rates: the organization's hourly rate per role and currency (#9321)

Rates by role, slice O4 of ``dev-docs/roi-outcomes.md`` (section 3.4): an
administrator sets the loaded hourly rate of a built-in role ("analyst") or a
custom one ("legal") in a currency, so a custom unit's ``rate_role`` carries
its own rate. A new table, nothing rewritten; the lock-safe recipe
(``terminate_lock_holders`` + ``set_lock_timeout``) covers the ``users`` FK.

Revision ID: r9321roirates1
Revises: r9310roireview1
Create Date: 2026-11-06 17:20:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "r9321roirates1"
# NOT a constant: must always be main's CURRENT head, re-pointed on every rebase
# (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "r9310roireview1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RATES = "roi_rates"


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
    if _has_table(conn, _RATES):
        return
    terminate_lock_holders(conn, "users")
    set_lock_timeout(conn)
    op.create_table(
        _RATES,
        sa.Column("role", sa.String(64), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("amount", sa.Float(), nullable=False),
        sa.Column("source", sa.String(300), nullable=False, server_default=sa.text("''")),
        sa.Column(
            "updated_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("role", "currency"),
        sa.CheckConstraint("amount > 0", name="ck_roi_rates_amount_positive"),
        if_not_exists=True,
    )


def downgrade() -> None:
    conn = op.get_bind()
    if _has_table(conn, _RATES):
        terminate_lock_holders(conn, _RATES)
        set_lock_timeout(conn)
        op.drop_table(_RATES, if_exists=True)
