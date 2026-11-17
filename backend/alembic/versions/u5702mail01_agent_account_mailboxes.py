"""agent accounts: mailboxes - mailbox actions, the agent's note, one account per provider and integration

An agent account can operate an Outlook mailbox:

* ``agent_accounts.mailbox_actions`` - what the mailbox files on each email
  the agent triaged (``triage.normalize_mailbox_actions``), NULL = defaults.
* ``agent_account_decisions.note`` - the agent's one line on its triage.
* One enabled agent account per provider and integration, instead of per
  integration: a Teams account and an Outlook mailbox may share one
  Microsoft sign-in. The new index is laxer than the old one, so the rows
  that satisfied the old one satisfy it; it is created before the old one is
  dropped.

Safety contract (backend/AGENTS.md > "Database migrations (safety)"):

* Idempotent - every column and index is probed first, each DDL guarded.
* Lock-safe - nullable columns are metadata-only, ``agent_accounts`` holds a
  few rows; the canonical recipe (probe, ``terminate_lock_holders``,
  ``set_lock_timeout``) runs before each table's DDL.
* Reversible - the downgrade pauses the accounts the old index would refuse
  (the oldest per integration keeps it), restores the old index and drops the
  columns.

Revision ID: u5702mail01
Revises: p5699edit001
Create Date: 2026-10-12 14:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "u5702mail01"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "p5699edit001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ACCOUNTS = "agent_accounts"
_DECISIONS = "agent_account_decisions"
_COLUMNS: tuple[tuple[str, str, str], ...] = (
    (_ACCOUNTS, "mailbox_actions", "JSON"),
    (_DECISIONS, "note", "TEXT"),
)
_NEW_INDEX = "uq_agent_accounts_provider_integration"
_OLD_INDEX = "uq_agent_accounts_integration"
_WHERE = "enabled AND integration_id IS NOT NULL"

PAUSED_REASON = (
    "Paused: another agent account already operates this integration. "
    "One agent account per integration - add rules to that one instead."
)


def _has_column(conn, table: str, column: str) -> bool:
    return (
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = :t AND column_name = :c"
            ),
            {"t": table, "c": column},
        ).first()
        is not None
    )


def _has_index(conn, name: str) -> bool:
    return bool(
        conn.execute(
            sa.text("SELECT 1 FROM pg_indexes WHERE schemaname = current_schema() AND indexname = :n"),
            {"n": name},
        ).scalar()
    )


def upgrade() -> None:
    conn = op.get_bind()
    for table, column, kind in _COLUMNS:
        if _has_column(conn, table, column):
            continue
        terminate_lock_holders(conn, table)
        set_lock_timeout(conn)
        conn.execute(sa.text(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {kind}"))
    if _has_index(conn, _NEW_INDEX) and not _has_index(conn, _OLD_INDEX):
        return
    terminate_lock_holders(conn, _ACCOUNTS)
    set_lock_timeout(conn)
    op.execute(
        f"CREATE UNIQUE INDEX IF NOT EXISTS {_NEW_INDEX} ON {_ACCOUNTS} (provider, integration_id) WHERE {_WHERE}"
    )
    op.execute(f"DROP INDEX IF EXISTS {_OLD_INDEX}")


def downgrade() -> None:
    conn = op.get_bind()
    if not (_has_index(conn, _OLD_INDEX) and not _has_index(conn, _NEW_INDEX)):
        terminate_lock_holders(conn, _ACCOUNTS)
        set_lock_timeout(conn)
        conn.execute(
            sa.text(
                f"""
                WITH ranked AS (
                    SELECT id, row_number() OVER (PARTITION BY integration_id ORDER BY created_at, id) AS rank
                    FROM {_ACCOUNTS}
                    WHERE {_WHERE}
                )
                UPDATE {_ACCOUNTS} AS account
                SET enabled = false, status = 'error', transport = 'none', last_error = :reason
                FROM ranked
                WHERE account.id = ranked.id AND ranked.rank > 1
                """  # noqa: S608 - the table and condition are the literals above
            ),
            {"reason": PAUSED_REASON},
        )
        op.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS {_OLD_INDEX} ON {_ACCOUNTS} (integration_id) WHERE {_WHERE}")
        op.execute(f"DROP INDEX IF EXISTS {_NEW_INDEX}")
    for table, column, _kind in reversed(_COLUMNS):
        if not _has_column(conn, table, column):
            continue
        terminate_lock_holders(conn, table)
        set_lock_timeout(conn)
        conn.execute(sa.text(f"ALTER TABLE {table} DROP COLUMN IF EXISTS {column}"))
