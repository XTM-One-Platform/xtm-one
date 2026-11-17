"""index file_attachments.assignment_run_id so deleting runs stays fast

``file_attachments.assignment_run_id`` references ``assignment_runs.id`` with
``ON DELETE SET NULL`` and was the one reference to ``assignment_runs`` with no
index. PostgreSQL applies the action once per deleted run with a query on that
column, so deleting N runs scanned the whole ``file_attachments`` table N
times: an agent with a few thousand runs could no longer be deleted, its run
delete cancelled by the 30 s ``statement_timeout`` on every attempt. Deleting
an assignment and the run purges hit the same scan.

The index is declared on the model, so a fresh install gets it from
``create_all`` and this body does not run there.

**Lock safety.** Built in place (``create_index_in_place``) in an autocommit
block, not ``CONCURRENTLY``: a concurrent build waits for every older snapshot
on the whole database, which a rolling deploy on a busy install never lets it
outlast, while a plain build holds a SHARE lock on this one table for
the seconds it runs, after the terminate pre-flight and under a lock_timeout.
A valid index of this name - built by hand as the issue's workaround -
takes no lock at all. A failed build does not stop startup: the boot-time
model index reconciliation builds the model's index after the upgrade.

Revision ID: f2runix5258
Revises: g5209gadefault
Create Date: 2026-10-03 09:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import create_index_in_place

# revision identifiers, used by Alembic.
revision: str = "f2runix5258"
# NOT a constant: must always be main's CURRENT head, re-pointed on every rebase
# (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | None = "g5209gadefault"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "file_attachments"
_INDEX = "ix_file_attachments_assignment_run_id"


def _table_exists(conn) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = current_schema() AND table_name = :table"
            ),
            {"table": _TABLE},
        ).scalar()
    )


def upgrade() -> None:
    conn = op.get_bind()
    if not _table_exists(conn):
        return
    with op.get_context().autocommit_block():
        create_index_in_place(conn, _INDEX, _TABLE, "(assignment_run_id)")


def downgrade() -> None:
    conn = op.get_bind()
    if not _table_exists(conn):
        return
    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX}")
