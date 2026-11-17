"""add ix_agent_memory_entries_agent_created - an agent's entries, newest first

Every chat turn reads the newest persistent-memory entries of the agent
(``persistent_memory.load_persistent_entries``: agent, not consolidated,
active for the reader, ``ORDER BY created_at DESC LIMIT n``). With the
single-column ``agent_id`` index that read fetches every entry of the agent,
evaluates the newer-state ``EXISTS`` on each, then sorts them all to keep n:
it grows with the agent's pool (217 to 451 ms per turn measured, #8836).
This index lets the planner walk the agent's entries newest first and stop
after n eligible ones.

Purely additive. Built in place (``create_index_in_place``) in an
autocommit block, not ``CONCURRENTLY``: a concurrent build waits for every
older snapshot on the whole database, which a rolling deploy never lets it
outlast, while a plain build holds a SHARE lock on this one table for the
seconds it runs, after the terminate pre-flight and under a lock_timeout. A
failed build does not stop startup: the index is declared on the model, so
the boot-time model index reconciliation builds it after the upgrade.

Revision ID: m8836memrec1
Revises: d10mrk6591010
Create Date: 2026-11-05 12:00:00.000000
"""

from collections.abc import Sequence

from alembic import op
from app.core.migration_ddl import create_index_in_place, set_lock_timeout, terminate_lock_holders

revision: str = "m8836memrec1"
# NOT a constant: must always be the base branch's CURRENT head, re-pointed
# whenever it gains a migration (backend/AGENTS.md > "Database migrations
# (safety)", rule 1).
down_revision: str | Sequence[str] | None = "d10mrk6591010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "agent_memory_entries"
_INDEX = "ix_agent_memory_entries_agent_created"
_DEFINITION = "(agent_id, created_at)"


def upgrade() -> None:
    conn = op.get_bind()
    with op.get_context().autocommit_block():
        create_index_in_place(conn, _INDEX, _TABLE, _DEFINITION)


def downgrade() -> None:
    conn = op.get_bind()
    with op.get_context().autocommit_block():
        terminate_lock_holders(conn, _TABLE)
        set_lock_timeout(conn)
        op.execute(f"DROP INDEX IF EXISTS {_INDEX}")
