"""agent accounts: the activity log and the sent-reply notification

* ``agent_account_decisions``: what an account did with each message it heard,
  one row per message (``uq_agent_account_decisions_message``), for the
  account page's log and analytics. Purely additive, nothing backfilled: the
  previous log lived in Redis for a week.
* ``agent_accounts.notify_on_send``: the owner is notified of each reply the
  account posts on its own; on by default, every existing account included.

Safety contract (backend/AGENTS.md > "Database migrations (safety)"):

* Idempotent - the table, its indexes and the column are each probed first
  and created with ``IF NOT EXISTS``.
* Lock-safe - nothing knows the new table, so nothing holds a lock on it; its
  foreign key takes a SHARE ROW EXCLUSIVE lock on ``agent_accounts``, under the
  ``lock_timeout`` belt so a contended attempt fails fast (retried by the
  startup runner) - terminating holders would kill sessions that merely read
  it. A NOT NULL column with a constant default is metadata-only; the
  canonical recipe (probe, ``terminate_lock_holders``, ``set_lock_timeout``)
  runs before it.
* Reversible - the downgrade drops the column and the table, under the belt.

Revision ID: t5631act01
Revises: s5619sync01
Create Date: 2026-10-11 10:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "t5631act01"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "s5619sync01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ACCOUNTS = "agent_accounts"
_DECISIONS = "agent_account_decisions"
_INDEX = "ix_agent_account_decisions_account_created"
_COLUMN = "notify_on_send"


def _has_table(conn, name: str) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.tables WHERE table_schema = current_schema() AND table_name = :n"
            ),
            {"n": name},
        ).scalar()
    )


def _has_index(conn, name: str) -> bool:
    return bool(
        conn.execute(
            sa.text("SELECT 1 FROM pg_indexes WHERE schemaname = current_schema() AND indexname = :n"),
            {"n": name},
        ).scalar()
    )


def _has_column(conn) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = :t AND column_name = :c"
            ),
            {"t": _ACCOUNTS, "c": _COLUMN},
        ).scalar()
    )


def upgrade() -> None:
    conn = op.get_bind()
    if not _has_table(conn, _DECISIONS):
        set_lock_timeout(conn)
        op.create_table(
            _DECISIONS,
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "account_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("agent_accounts.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("outcome", sa.String(40), nullable=False),
            sa.Column("reason", sa.String(40), nullable=True),
            sa.Column("respond", sa.Boolean(), nullable=False, server_default=sa.text("false")),
            sa.Column("waited", sa.Boolean(), nullable=False, server_default=sa.text("false")),
            sa.Column("conversation_type", sa.String(20), nullable=False),
            sa.Column("conversation_id", sa.String(500), nullable=False),
            sa.Column("conversation_name", sa.String(500), nullable=True),
            sa.Column("message_id", sa.String(255), nullable=False),
            sa.Column("sender_id", sa.String(255), nullable=True),
            sa.Column("sender_name", sa.String(255), nullable=True),
            sa.Column("preview", sa.Text(), nullable=False, server_default=sa.text("''")),
            sa.Column("mentions_account", sa.Boolean(), nullable=False, server_default=sa.text("false")),
            sa.Column("addressed", sa.Boolean(), nullable=False, server_default=sa.text("false")),
            sa.Column("thread_continuation", sa.Boolean(), nullable=False, server_default=sa.text("false")),
            sa.Column("rule_id", sa.String(100), nullable=True),
            sa.Column("rule_name", sa.String(200), nullable=True),
            sa.Column("sender_match", postgresql.JSON(), nullable=True),
            sa.Column("reply_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("message_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
            sa.UniqueConstraint(
                "account_id", "conversation_id", "message_id", name="uq_agent_account_decisions_message"
            ),
            if_not_exists=True,
        )
    if not _has_index(conn, _INDEX):
        # Instant on the table created above; the belt matters for a table
        # the startup create_all made, which may already be taking rows.
        set_lock_timeout(conn)
        op.create_index(_INDEX, _DECISIONS, ["account_id", "created_at"], if_not_exists=True)
    if not _has_column(conn):
        terminate_lock_holders(conn, _ACCOUNTS)
        set_lock_timeout(conn)
        conn.execute(
            sa.text(f"ALTER TABLE {_ACCOUNTS} ADD COLUMN IF NOT EXISTS {_COLUMN} BOOLEAN NOT NULL DEFAULT true")
        )


def downgrade() -> None:
    conn = op.get_bind()
    if _has_column(conn):
        terminate_lock_holders(conn, _ACCOUNTS)
        set_lock_timeout(conn)
        conn.execute(sa.text(f"ALTER TABLE {_ACCOUNTS} DROP COLUMN IF EXISTS {_COLUMN}"))
    if _has_table(conn, _DECISIONS):
        # Removing the foreign key's triggers takes an ACCESS EXCLUSIVE lock on
        # ``agent_accounts``: the belt makes a busy one fail fast.
        set_lock_timeout(conn)
        op.drop_table(_DECISIONS, if_exists=True)
