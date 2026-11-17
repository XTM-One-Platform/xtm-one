"""add conversation_channel_bindings (continue a conversation in Slack / Teams)

Purely additive: one new table, no change to any existing column, so old
replicas during a rolling deploy simply ignore it. Nothing is backfilled: a
conversation that started on Slack or Teams is still found from its own
``conversations.channel_thread_id``; a row here only exists once a user
continued a conversation in a direct-message thread.

Revision ID: c5x1chn5134
Revises: f2runix5258
Create Date: 2026-10-02 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_ddl import create_index_concurrently, set_lock_timeout

revision: str = "c5x1chn5134"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "f2runix5258"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "conversation_channel_bindings"

#: The same definitions as ``ConversationChannelBinding`` (the create_all path).
_INDEXES: tuple[tuple[str, str], ...] = (
    ("ix_conversation_channel_bindings_conversation_id", "(conversation_id)"),
    ("ix_conversation_channel_bindings_user_id", "(user_id)"),
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


def upgrade() -> None:
    conn = op.get_bind()

    # Idempotency probes: fresh installs run create_all + stamp head (this body
    # never executes), and the migrations CI job re-runs the revision against
    # an already-upgraded database; both must be no-ops.
    #
    # The table's foreign keys install triggers on `conversations` and `users`
    # (SHARE ROW EXCLUSIVE there): the lock_timeout belt makes a contended
    # attempt fail fast (55P03, retried by the startup runner) instead of
    # queueing every writer of those hot tables behind it. Terminating their
    # holders would kill sessions that merely read them, which this lock does
    # not conflict with.
    if not _has_table(conn, _TABLE):
        set_lock_timeout(conn)
        op.create_table(
            _TABLE,
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "conversation_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("conversations.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "user_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("users.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("channel", sa.String(30), nullable=False),
            sa.Column("channel_thread_id", sa.String(500), nullable=False),
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
            sa.UniqueConstraint(
                "channel",
                "channel_thread_id",
                name="uq_conversation_channel_bindings_thread",
            ),
            if_not_exists=True,
        )

    # CONCURRENTLY, and last because the autocommit block commits the table
    # above: instant on a table created here, and a table found without an
    # index may already hold bindings, which a plain build would block writes
    # to. The helper also drops the INVALID residue of an interrupted build.
    missing = [(name, cols) for name, cols in _INDEXES if not _has_index(conn, name)]
    if missing:
        with op.get_context().autocommit_block():
            for name, cols in missing:
                create_index_concurrently(conn, name, _TABLE, cols)


def downgrade() -> None:
    conn = op.get_bind()
    # Dropping the table removes its foreign key triggers from `conversations`
    # and `users` (ACCESS EXCLUSIVE there): the belt makes a busy table fail
    # the downgrade fast, to be retried when it is quiet.
    if _has_table(conn, _TABLE):
        set_lock_timeout(conn)
        op.drop_table(_TABLE, if_exists=True)
