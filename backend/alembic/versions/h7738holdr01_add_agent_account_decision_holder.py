"""add agent_account_decisions.holder

A message another pipeline of the same agent took first (outcome
``already_handled``) records WHO took it as data, the claim's holder
value (``inbound_claims``: ``assignment`` or ``agent_account:<id>``), so the
activity page words it in the viewer's language and links the account instead
of showing a backend sentence with a raw id. NULL for every other outcome and
for the rows written before.

One nullable ``ADD COLUMN`` without default (metadata-only), following the
lock-safe recipe: idempotency probe, ``terminate_lock_holders``,
``set_lock_timeout``, then the guarded DDL. Fresh installs run ``create_all``
+ ``stamp head``: the body never executes there.

Revision ID: h7738holdr01
Revises: b7834bltalw1
Create Date: 2026-11-03 05:50:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "h7738holdr01"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "b7834bltalw1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "agent_account_decisions"
_COLUMN = "holder"


def _present(conn) -> bool:
    return (
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = :table AND column_name = :column"
            ),
            {"table": _TABLE, "column": _COLUMN},
        ).first()
        is not None
    )


def upgrade() -> None:
    conn = op.get_bind()
    if _present(conn):
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    op.execute(f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS {_COLUMN} VARCHAR(80)")


def downgrade() -> None:
    conn = op.get_bind()
    if not _present(conn):
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS {_COLUMN}")
