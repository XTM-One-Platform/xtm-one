"""one enabled agent account per integration and per platform account

Two enabled agent accounts operating the same platform account both answered
every message and competed for the provider's subscriptions. The API
now refuses the second one; these two partial unique indexes are its backstop.
Enabled duplicates saved before the rule are paused first - the oldest keeps
the account, the others are disabled with the reason in ``last_error``.

Revision ID: a5450uniq01
Revises: a5438acct01
Create Date: 2026-10-06 23:40:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "a5450uniq01"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "a5438acct01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "agent_accounts"

#: The same definitions as ``AgentAccount.__table_args__``.
_INDEXES: tuple[tuple[str, str, str], ...] = (
    ("uq_agent_accounts_operated", "provider, account_external_id", "enabled AND account_external_id IS NOT NULL"),
    ("uq_agent_accounts_integration", "integration_id", "enabled AND integration_id IS NOT NULL"),
)

PAUSED_REASON = (
    "Paused: another agent account already operates this platform account. "
    "One agent account per platform account - add rules to that one instead."
)


def _has_index(conn, name: str) -> bool:
    return bool(
        conn.execute(
            sa.text("SELECT 1 FROM pg_indexes WHERE schemaname = current_schema() AND indexname = :n"),
            {"n": name},
        ).scalar()
    )


def pause_duplicates(conn) -> int:
    """Disable every enabled account but the oldest of each platform account and each integration."""
    paused = 0
    for partition, present in (
        ("provider, account_external_id", "account_external_id IS NOT NULL"),
        ("integration_id", "integration_id IS NOT NULL"),
    ):
        result = conn.execute(
            sa.text(
                f"""
                WITH ranked AS (
                    SELECT id, row_number() OVER (PARTITION BY {partition} ORDER BY created_at, id) AS rank
                    FROM {_TABLE}
                    WHERE enabled AND {present}
                )
                UPDATE {_TABLE} AS account
                SET enabled = false, status = 'error', transport = 'none', last_error = :reason
                FROM ranked
                WHERE account.id = ranked.id AND ranked.rank > 1
                """  # noqa: S608 - the table and columns are the literals above
            ),
            {"reason": PAUSED_REASON},
        )
        paused += result.rowcount or 0
    return paused


def upgrade() -> None:
    conn = op.get_bind()
    missing = [(name, columns, where) for name, columns, where in _INDEXES if not _has_index(conn, name)]
    if not missing:
        return
    # A few rows at most, and the indexes need the duplicates gone first.
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    pause_duplicates(conn)
    for name, columns, where in missing:
        op.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS {name} ON {_TABLE} ({columns}) WHERE {where}")


def downgrade() -> None:
    conn = op.get_bind()
    present = [name for name, _columns, _where in _INDEXES if _has_index(conn, name)]
    if not present:
        return
    # The paused accounts stay paused: re-enabling a duplicate is the owner's call.
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    for name in present:
        op.execute(f"DROP INDEX IF EXISTS {name}")
