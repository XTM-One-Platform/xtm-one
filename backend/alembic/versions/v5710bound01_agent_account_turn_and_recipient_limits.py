"""agent accounts: what one account spends per hour and reaches per reply

Adds to ``agent_accounts``:
  - ``max_turns_per_hour`` (INTEGER NOT NULL DEFAULT 0, 0 = no limit) - the
    agent turns the account runs per hour, every conversation together. The
    existing mailbox accounts (``microsoft_outlook``, the one provider whose
    conversations are emails) get the mailbox preset, 60: one sender writing
    new emails starts a new conversation with each, so the per-conversation
    limit never bounded them. A chat account keeps no account-wide limit.
  - ``max_reply_recipients`` (INTEGER NOT NULL DEFAULT 10, 0 = no limit) - the
    people a reply may reach besides the sender before the message is left to
    the owner (a mailbox's reply-all).

Safety contract (backend/AGENTS.md > "Database migrations (safety)"):

* Idempotent - each column is probed first and added with ``IF NOT EXISTS``;
  the mailbox backfill runs only in the upgrade that added the column, so a
  limit an owner set since is never overwritten.
* Lock-safe - a NOT NULL column with a constant default is metadata-only,
  under the canonical recipe (probe, ``terminate_lock_holders``,
  ``set_lock_timeout``). The backfill touches the mailbox accounts alone, a
  handful of rows of a per-user table, under the same ``lock_timeout``.
* Reversible - the downgrade drops both columns, under the same recipe.

Revision ID: v5710bound01
Revises: k5361kbdoc01
Create Date: 2026-10-24 10:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "v5710bound01"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "k5361kbdoc01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "agent_accounts"
_TURNS = "max_turns_per_hour"
_COLUMNS: tuple[tuple[str, str], ...] = (
    (_TURNS, "INTEGER NOT NULL DEFAULT 0"),
    ("max_reply_recipients", "INTEGER NOT NULL DEFAULT 10"),
)
#: ``rules.MAILBOX_TURNS_PER_HOUR`` when this migration was written (a migration never imports app logic).
_MAILBOX_TURNS_PER_HOUR = 60
_MAILBOX_PROVIDER = "microsoft_outlook"


def _column_exists(conn, column: str) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = :table AND column_name = :column"
            ),
            {"table": _TABLE, "column": column},
        ).scalar()
    )


def upgrade() -> None:
    conn = op.get_bind()
    for column, ddl in _COLUMNS:
        if _column_exists(conn, column):
            continue
        terminate_lock_holders(conn, _TABLE)
        set_lock_timeout(conn)
        op.execute(f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS {column} {ddl}")
        if column == _TURNS:
            conn.execute(
                sa.text("UPDATE agent_accounts SET max_turns_per_hour = :turns WHERE provider = :provider"),
                {"turns": _MAILBOX_TURNS_PER_HOUR, "provider": _MAILBOX_PROVIDER},
            )


def downgrade() -> None:
    conn = op.get_bind()
    for column, _ddl in reversed(_COLUMNS):
        if not _column_exists(conn, column):
            continue
        terminate_lock_holders(conn, _TABLE)
        set_lock_timeout(conn)
        op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS {column}")
