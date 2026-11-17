"""index sub_agent_tasks by parent run for the A2A delegated-work lookup

``a2a.tasks.live_delegated_work`` asks whether a run still has background work
running, and it asks on ``tasks/get`` — a polling method. The linkage between
the two tables is a JSON path (``metadata_json->>'parent_run_id'``, or
``assignment_run_id`` for a child's own run), which is the only correlation
that exists and the one ``mark_sub_agent_tasks_cancelled_for_runs`` already
matches on. Unindexed that is a JSONB scan per poll.

Partial, on the two paths and the two non-terminal statuses the lookup filters
by, so it stays small on a table whose finished rows dominate. It serves the
cancel cascade equally.

``CONCURRENTLY`` because ``sub_agent_tasks`` is written by chat, assignments and
evaluations: a plain build would hold a lock against all three. Best-effort per
``create_index_concurrently`` — a build that exceeds its timeout logs the SQL to
run by hand rather than wedging every booting replica.

Revision ID: a2a6delegidx
Revises: a2a5respond1
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import create_index_concurrently

# revision identifiers, used by Alembic.
revision: str = "a2a6delegidx"
down_revision: str | None = "a2a5respond1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "sub_agent_tasks"
_PARENT_INDEX = "ix_sub_agent_tasks_live_parent_run"
_OWN_INDEX = "ix_sub_agent_tasks_live_own_run"
_LIVE = "status IN ('pending','running')"


def _has_table(conn, table: str) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = current_schema() AND table_name = :t"
            ),
            {"t": table},
        ).scalar()
    )


def upgrade() -> None:
    conn = op.get_bind()
    if not _has_table(conn, _TABLE):
        return
    with op.get_context().autocommit_block():
        create_index_concurrently(
            conn,
            _PARENT_INDEX,
            _TABLE,
            f"((metadata_json->>'parent_run_id')) WHERE {_LIVE}",
        )
        create_index_concurrently(
            conn,
            _OWN_INDEX,
            _TABLE,
            f"((metadata_json->>'assignment_run_id')) WHERE {_LIVE}",
        )


def downgrade() -> None:
    conn = op.get_bind()
    if not _has_table(conn, _TABLE):
        return
    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_PARENT_INDEX}")
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_OWN_INDEX}")
