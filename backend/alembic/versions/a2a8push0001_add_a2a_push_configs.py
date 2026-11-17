"""add a2a_push_configs — a partner's callback for one task

The card declared push notifications unsupported and the four configuration
methods were refused by name. This is the table behind serving them: one row per
callback a partner registers, holding the address and the credential to present
back to it.

``token`` and ``auth_credentials`` are Fernet-encrypted by the application, so
the columns are plain text here — the same shape every other stored secret has.

Additive: one new table, no change to any existing one. The foreign key to
``a2a_tasks`` is what takes a lock on an existing table, so the canonical belt
applies to it even though CREATE TABLE itself locks nothing. The partial index
serves the delivery sweep, which claims by state.

Revision ID: a2a8push0001
Revises: a2a7context01
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

# revision identifiers, used by Alembic.
revision: str = "a2a8push0001"
down_revision: str | None = "a2a7context01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "a2a_push_configs"


def _has_table(conn, table: str) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = current_schema() AND table_name = :t"
            ),
            {"t": table},
        ).scalar()
    )


def upgrade() -> None:
    conn = op.get_bind()
    if _has_table(conn, _TABLE):
        return
    # The REFERENCES below takes a SHARE ROW EXCLUSIVE lock on a2a_tasks, which
    # a long-lived reader can block — the 2026-07-05 shape.
    terminate_lock_holders(conn, "a2a_tasks")
    set_lock_timeout(conn)
    op.create_table(
        _TABLE,
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "task_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("a2a_tasks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("client_id", sa.String(255), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("token", sa.Text(), nullable=True),
        sa.Column("auth_scheme", sa.String(40), nullable=True),
        sa.Column("auth_credentials", sa.Text(), nullable=True),
        sa.Column(
            "delivery_state", sa.String(20), nullable=False, server_default="pending"
        ),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index(f"ix_{_TABLE}_task_id", _TABLE, ["task_id"])
    op.create_index(f"ix_{_TABLE}_client_id", _TABLE, ["client_id"])


def downgrade() -> None:
    conn = op.get_bind()
    if _has_table(conn, _TABLE):
        terminate_lock_holders(conn, _TABLE)
        set_lock_timeout(conn)
        op.drop_table(_TABLE)
