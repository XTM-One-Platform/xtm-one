"""add security_audit_finding_states

One row per Security Audit finding id the administrators were notified of or
dismissed: the durable notification memory (it was Redis-only) and the
dismissals. Purely additive: a new table, no change to any existing column, so
old replicas during a rolling deploy ignore it. Nothing is backfilled here -
the notifier imports the legacy Redis memory on its first run.

Revision ID: s5013fstate01
Revises: a4996mcsrc01
Create Date: 2026-09-30 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "s5013fstate01"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "a4996mcsrc01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "security_audit_finding_states"


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

    # Idempotency probe: fresh installs run create_all + stamp head (this body
    # never executes), and a crashed replica may retry.
    if _has_table(conn, _TABLE):
        return

    # The foreign keys to `users` install their triggers there under a SHARE
    # ROW EXCLUSIVE lock, which an in-flight write to `users` blocks: clear the
    # holders first, then the lock_timeout belt makes a racing one fail fast
    # (55P03, retried by the startup runner) instead of queueing every writer
    # of `users` behind the DDL.
    terminate_lock_holders(conn, "users")
    set_lock_timeout(conn)
    op.create_table(
        _TABLE,
        sa.Column("finding_id", sa.String(200), primary_key=True),
        sa.Column("notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notified_severity", sa.String(20), nullable=True),
        sa.Column("dismissed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "dismissed_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("dismissed_severity", sa.String(20), nullable=True),
        sa.Column("dismissal_reason", sa.String(1000), nullable=True),
        sa.Column("reopened_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reopened_severity", sa.String(20), nullable=True),
        sa.Column("restored_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "restored_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        if_not_exists=True,
    )


def downgrade() -> None:
    conn = op.get_bind()
    if not _has_table(conn, _TABLE):
        return
    # Dropping the table removes its foreign key triggers from `users` under an
    # ACCESS EXCLUSIVE lock there: the belt makes a busy `users` fail fast.
    set_lock_timeout(conn)
    op.drop_table(_TABLE, if_exists=True)
