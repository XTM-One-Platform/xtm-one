"""per-platform tool approval: remember that a client could answer a prompt

Adds ``platform_registrations.approval_support_seen_at``.

``supports_approval_prompts`` is written by registration alone: the platform
speaking about its own assistant. But a client declares the same capability
per chat message (``supports_tool_approval``), and that evidence was being
discarded — which mattered in exactly one arrangement, and it is not a rare
one:

    approval_policy = skip, supports_approval_prompts = NULL, capable client

An operator (or ``HITL_PLATFORM_DEFAULT_POLICY=skip`` at install) turned
approval off because the platform could not ask. The platform never declares
the capability, so the existing nudge — which fires only on a declared TRUE —
stays silent forever, while a client that would happily have shown a prompt
goes unasked and the tools run unattended.

This column records that a client said it can answer. It never decides whether
tools gate: one capable client does not speak for an unattended caller of the
same platform. It only lets the settings page offer the safer posture, with the
operator's choice standing until they change it themselves.

A timestamp rather than a boolean: "a client said so once, long ago" and "a
client said so this morning" deserve different confidence, and a flag cannot
go stale.

Nullable with no default — an absent observation is the correct state for every
existing row, and backfilling would invent evidence.

Idempotent: guarded by a column-existence probe, so re-running converges without
redoing work. Follows the canonical lock-safe recipe: idempotency probe ->
terminate_lock_holders -> set_lock_timeout -> guarded DDL.

Revision ID: c3q2obs0173
Revises: b2p1lat0173
Create Date: 2026-08-17 11:20:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

# revision identifiers, used by Alembic.
revision: str = "c3q2obs0173"
down_revision: str | None = "b2p1lat0173"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "platform_registrations"
_COLUMN = "approval_support_seen_at"


def _column_exists(conn, table: str, column: str) -> bool:
    return (
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = current_schema() "
                "AND table_name = :table AND column_name = :column"
            ),
            {"table": table, "column": column},
        ).first()
        is not None
    )


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    # A deployment that has never had a Suite platform connected may not have
    # the table at all yet; nothing to widen in that case.
    if not inspector.has_table(_TABLE):
        return

    if _column_exists(conn, _TABLE, _COLUMN):
        return

    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)

    op.add_column(
        _TABLE,
        sa.Column(_COLUMN, sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if not inspector.has_table(_TABLE):
        return

    if not _column_exists(conn, _TABLE, _COLUMN):
        return

    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)

    op.drop_column(_TABLE, _COLUMN)
