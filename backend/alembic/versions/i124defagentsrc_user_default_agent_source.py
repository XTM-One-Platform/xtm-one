"""who set a person's default chat agent, and when

Adds ``users.default_agent_source`` (``user`` | ``platform``, NULL on rows
written before it) and ``users.default_agent_set_at`` (issue #529): an
orchestrator reading a person's own default agent
(``platform_insights.default_agents``) must tell the person's choice from the
CTEM Assistant the platform wires for everyone with none. No backfill (rule 4):
the reader tells a NULL source apart from the agent it points at.

**Lock safety.** Two nullable ``ADD COLUMN`` with no default are metadata-only
but take a brief ``ACCESS EXCLUSIVE`` lock on ``users``: canonical recipe
(idempotency probe, ``terminate_lock_holders``, ``set_lock_timeout``, guarded
DDL). Fresh installs run ``create_all`` + ``stamp head``: the body never
executes there.

Revision ID: i124defagentsrc
Revises: h123mcpusercred
Create Date: 2026-11-19 20:15:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "i124defagentsrc"
# NOT a constant: must always be main's CURRENT head, re-pointed on every rebase
# (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "h123mcpusercred"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "users"
_COLUMNS = {
    "default_agent_source": "VARCHAR(16)",
    "default_agent_set_at": "TIMESTAMP WITH TIME ZONE",
}


def _existing(conn: sa.engine.Connection) -> set[str]:
    rows = conn.execute(
        sa.text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = :table"
        ),
        {"table": _TABLE},
    ).all()
    return {row[0] for row in rows}


def upgrade() -> None:
    conn = op.get_bind()
    missing = [name for name in _COLUMNS if name not in _existing(conn)]
    if not missing:
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    for name in missing:
        op.execute(f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS {name} {_COLUMNS[name]}")


def downgrade() -> None:
    conn = op.get_bind()
    present = [name for name in _COLUMNS if name in _existing(conn)]
    if not present:
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    for name in present:
        op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS {name}")
