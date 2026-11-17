"""allow 'flow' as an llm_traces source

Flow runs now open a root trace per burst (`flow-run:<name>`) with one span
per executed node — the walker's observability. The ``llm_traces.source``
check constraint enumerates the known producers, so the new one must be
added: drop + re-add ``NOT VALID`` (existing rows all satisfy the superset,
and skipping validation avoids scanning a hot, potentially huge table).

Revision ID: e6g8flwtrc1
Revises: e5f7flwevt1
Create Date: 2026-08-14 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

# revision identifiers, used by Alembic.
revision: str = "e6g8flwtrc1"
down_revision: str | None = "e5f7flwevt1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "llm_traces"
_CONSTRAINT = "ck_llm_traces_source_valid"

_NEW_CHECK = (
    "source IN ('agent-chat', 'general-chat', 'assignment', 'flow', "
    "'memory', 'evaluation', 'proxy')"
)
_OLD_CHECK = (
    "source IN ('agent-chat', 'general-chat', 'assignment', "
    "'memory', 'evaluation', 'proxy')"
)


def _table_exists(conn, table: str) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = current_schema() AND table_name = :table"
            ),
            {"table": table},
        ).scalar()
    )


def _swap_check(conn, check_sql: str) -> None:
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    # Literal DDL from module constants — never user input (ruff S608).
    op.execute(f"ALTER TABLE {_TABLE} DROP CONSTRAINT IF EXISTS {_CONSTRAINT}")
    op.execute(
        f"ALTER TABLE {_TABLE} ADD CONSTRAINT {_CONSTRAINT} "
        f"CHECK ({check_sql}) NOT VALID"
    )


def upgrade() -> None:
    conn = op.get_bind()
    if not _table_exists(conn, _TABLE):
        return
    _swap_check(conn, _NEW_CHECK)


def downgrade() -> None:
    conn = op.get_bind()
    if not _table_exists(conn, _TABLE):
        return
    _swap_check(conn, _OLD_CHECK)
