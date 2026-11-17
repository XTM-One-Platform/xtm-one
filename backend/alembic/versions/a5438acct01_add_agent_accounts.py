"""add agent accounts (agent_accounts, agent_account_replies, agent_account_subscriptions)

Purely additive: three new tables, no change to any existing column,
so older replicas during a rolling deploy ignore tables they do not know.
Nothing is backfilled.

Revision ID: a5438acct01
Revises: p5430pdf01
Create Date: 2026-10-06 21:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_ddl import set_lock_timeout

revision: str = "a5438acct01"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "p5430pdf01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ACCOUNTS = "agent_accounts"
_REPLIES = "agent_account_replies"
_SUBSCRIPTIONS = "agent_account_subscriptions"

#: The same definitions as the models' ``index=True`` / ``__table_args__``.
_INDEXES: tuple[tuple[str, str, list], ...] = (
    ("ix_agent_accounts_user_id", _ACCOUNTS, ["user_id"]),
    ("ix_agent_accounts_integration_id", _ACCOUNTS, ["integration_id"]),
    ("ix_agent_accounts_agent_id", _ACCOUNTS, ["agent_id"]),
    ("ix_agent_account_replies_account_created", _REPLIES, ["account_id", "created_at"]),
    ("ix_agent_account_replies_user_status", _REPLIES, ["user_id", "status"]),
    ("ix_agent_account_subscriptions_account_id", _SUBSCRIPTIONS, ["account_id"]),
)


def _has_table(conn, name: str) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = current_schema() AND table_name = :n"
            ),
            {"n": name},
        ).scalar()
    )


def _has_index(conn, name: str) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM pg_indexes "
                "WHERE schemaname = current_schema() AND indexname = :n"
            ),
            {"n": name},
        ).scalar()
    )


def _uuid(name: str, *args, **kwargs) -> sa.Column:
    return sa.Column(name, postgresql.UUID(as_uuid=True), *args, **kwargs)


def _created_at() -> sa.Column:
    return sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()"))


def _timestamps() -> list[sa.Column]:
    return [
        _created_at(),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    ]


def upgrade() -> None:
    conn = op.get_bind()

    # Idempotency probes: fresh installs run create_all + stamp head (this body
    # never executes) and the migrations CI job re-runs it, so every table and
    # index is probed on its own and a partial state converges.
    #
    # Nothing knows these tables yet, so nothing holds a lock on them. Their
    # foreign keys install triggers on `users`, `integrations` and `agents`,
    # which takes a SHARE ROW EXCLUSIVE lock there: the lock_timeout belt makes
    # a contended attempt fail fast (55P03, retried by the startup runner)
    # instead of queueing every writer of those tables behind it. Terminating
    # their holders would kill sessions that merely read them, which this lock
    # does not conflict with.
    if not _has_table(conn, _ACCOUNTS):
        set_lock_timeout(conn)
        op.create_table(
            _ACCOUNTS,
            _uuid("id", primary_key=True),
            _uuid("user_id", sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("name", sa.String(200), nullable=False),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("provider", sa.String(50), nullable=False),
            _uuid("integration_id", sa.ForeignKey("integrations.id", ondelete="SET NULL"), nullable=True),
            _uuid("agent_id", sa.ForeignKey("agents.id", ondelete="SET NULL"), nullable=True),
            sa.Column("mode", sa.String(20), nullable=False, server_default=sa.text("'on_behalf'")),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
            sa.Column("response_mode", sa.String(20), nullable=False, server_default=sa.text("'approval'")),
            sa.Column("ai_label_enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
            sa.Column("ai_label_text", sa.String(300), nullable=True),
            sa.Column("ai_label_position", sa.String(10), nullable=False, server_default=sa.text("'suffix'")),
            sa.Column("grace_period_seconds", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("owner_takeover_minutes", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column("max_replies_per_hour", sa.Integer(), nullable=False, server_default=sa.text("20")),
            sa.Column("context_messages", sa.Integer(), nullable=False, server_default=sa.text("20")),
            sa.Column("active_hours", postgresql.JSON(), nullable=True),
            sa.Column("instructions", sa.Text(), nullable=True),
            sa.Column("rules", postgresql.JSON(), nullable=True),
            sa.Column("account_external_id", sa.String(255), nullable=True),
            sa.Column("account_display_name", sa.String(255), nullable=True),
            sa.Column("account_email", sa.String(320), nullable=True),
            sa.Column("transport", sa.String(10), nullable=False, server_default=sa.text("'none'")),
            sa.Column("status", sa.String(20), nullable=False, server_default=sa.text("'setup'")),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.Column("last_event_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_reply_at", sa.DateTime(timezone=True), nullable=True),
            *_timestamps(),
            if_not_exists=True,
        )

    if not _has_table(conn, _REPLIES):
        set_lock_timeout(conn)
        op.create_table(
            _REPLIES,
            _uuid("id", primary_key=True),
            _uuid("account_id", sa.ForeignKey("agent_accounts.id", ondelete="CASCADE"), nullable=False),
            _uuid("user_id", sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("status", sa.String(20), nullable=False),
            sa.Column("conversation_type", sa.String(20), nullable=False),
            sa.Column("conversation_id", sa.String(500), nullable=False),
            sa.Column("conversation_name", sa.String(500), nullable=True),
            sa.Column("thread_id", sa.String(500), nullable=False),
            sa.Column("message_id", sa.String(255), nullable=False),
            sa.Column("sender_id", sa.String(255), nullable=True),
            sa.Column("sender_name", sa.String(255), nullable=True),
            sa.Column("sender_email", sa.String(320), nullable=True),
            sa.Column("inbound_text", sa.Text(), nullable=False, server_default=sa.text("''")),
            sa.Column("draft_text", sa.Text(), nullable=True),
            sa.Column("sent_text", sa.Text(), nullable=True),
            sa.Column("sent_message_id", sa.String(255), nullable=True),
            sa.Column("rule_id", sa.String(100), nullable=True),
            sa.Column("rule_name", sa.String(200), nullable=True),
            sa.Column("ai_label", sa.Boolean(), nullable=False, server_default=sa.text("false")),
            sa.Column("route", postgresql.JSON(), nullable=True),
            sa.Column("error", sa.Text(), nullable=True),
            _uuid("decided_by_user_id", sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
            sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
            *_timestamps(),
            if_not_exists=True,
        )

    if not _has_table(conn, _SUBSCRIPTIONS):
        set_lock_timeout(conn)
        op.create_table(
            _SUBSCRIPTIONS,
            _uuid("id", primary_key=True),
            _uuid("account_id", sa.ForeignKey("agent_accounts.id", ondelete="CASCADE"), nullable=False),
            sa.Column("external_id", sa.String(255), nullable=False, unique=True),
            sa.Column("resource", sa.String(1000), nullable=False),
            sa.Column("client_state_enc", sa.Text(), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            _created_at(),
            if_not_exists=True,
        )

    missing = [(name, table, columns) for name, table, columns in _INDEXES if not _has_index(conn, name)]
    if missing:
        # Instant on the tables created above; the belt matters for a table
        # left without an index, which may already be taking rows.
        set_lock_timeout(conn)
        for name, table, columns in missing:
            op.create_index(name, table, columns, if_not_exists=True)


def downgrade() -> None:
    conn = op.get_bind()

    # Children first. Each drop takes an ACCESS EXCLUSIVE lock on the tables
    # its foreign keys reference to remove their triggers: the belt makes a
    # busy one fail the downgrade fast, to be retried when it is quiet.
    for table in (_SUBSCRIPTIONS, _REPLIES, _ACCOUNTS):
        if _has_table(conn, table):
            set_lock_timeout(conn)
            op.drop_table(table, if_exists=True)
