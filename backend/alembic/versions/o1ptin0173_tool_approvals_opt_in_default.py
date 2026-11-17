"""make the install-wide tool approval default opt-in on fresh installs

``j1atc0173`` added ``platform_settings.approve_tool_calls_default`` FALSE on
an upgraded install and then set the column default TRUE, so a fresh install
started with every agent asking before running tools. Tool approvals are
opt-in everywhere now: the column default goes back to FALSE.

Non-destructive: no row is written. An install whose row was created TRUE, or
whose administrator turned it on, keeps its value.

Lock profile: ``ALTER COLUMN ... SET DEFAULT`` is metadata-only but takes
ACCESS EXCLUSIVE on ``platform_settings``, which every request reads, so an
open transaction that merely read it would queue the ALTER behind itself in a
rolling deploy. Canonical pre-flight: idempotency probe -> terminate_lock_holders
-> set_lock_timeout -> guarded DDL.

Revision ID: o1ptin0173
Revises: k1llm5160
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

# revision identifiers, used by Alembic.
revision: str = "o1ptin0173"
down_revision: str | None = "k1llm5160"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "platform_settings"
_COLUMN = "approve_tool_calls_default"


def _column_default(conn) -> str | None:
    """The column's default expression, or ``None`` when the column is absent."""
    row = conn.execute(
        sa.text(
            "SELECT column_default FROM information_schema.columns "
            "WHERE table_schema = current_schema() "
            "AND table_name = :table AND column_name = :column"
        ),
        {"table": _TABLE, "column": _COLUMN},
    ).first()
    if row is None:
        return None
    return (row[0] or "").strip().lower()


def _set_default(conn, value: str) -> None:
    current = _column_default(conn)
    if current is None or current == value:
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    op.execute(f"ALTER TABLE {_TABLE} ALTER COLUMN {_COLUMN} SET DEFAULT {value}")


def upgrade() -> None:
    _set_default(op.get_bind(), "false")


def downgrade() -> None:
    _set_default(op.get_bind(), "true")
