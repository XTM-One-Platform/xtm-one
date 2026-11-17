"""add assignment_run_conversation_notices

The conversation an assignment run a chat turn triggered reports its end to:
the worker resumes that conversation with the run's outcome unless a wait or a
``get_run`` already told the agent (``told_at``). One row per run, keyed on the
run id with no foreign key.

Lock profile: a new table with no foreign key, so ``CREATE TABLE`` and its
partial index take no lock any other session holds; ``assignment_runs`` and
``conversations`` are left untouched. The belt is set anyway, as for every
index build. Dropping it (downgrade) takes ACCESS EXCLUSIVE on the table
itself and follows the canonical recipe.

The column set mirrors ``AssignmentRunConversationNotice`` in
``app/models/assignment.py`` and the index its declaration, name and
predicate included, so a fresh install's ``create_all`` and this body agree.

Revision ID: r6169wake01
Revises: m6176heads01
Create Date: 2026-10-20 23:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "r6169wake01"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "m6176heads01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "assignment_run_conversation_notices"
_INDEX = "ix_assignment_run_notices_untold"


def _has_table(conn) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = current_schema() AND table_name = :n"
            ),
            {"n": _TABLE},
        ).scalar()
    )


def upgrade() -> None:
    conn = op.get_bind()
    if _has_table(conn):
        return
    set_lock_timeout(conn)
    op.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {_TABLE} (
            run_id UUID PRIMARY KEY,
            conversation_id UUID NOT NULL,
            user_id UUID NOT NULL,
            created_at TIMESTAMPTZ NOT NULL,
            told_at TIMESTAMPTZ
        )
        """
    )
    op.execute(f"CREATE INDEX IF NOT EXISTS {_INDEX} ON {_TABLE} (created_at) WHERE told_at IS NULL")


def downgrade() -> None:
    conn = op.get_bind()
    if not _has_table(conn):
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    op.execute(f"DROP TABLE IF EXISTS {_TABLE}")
