"""add localizable detail codes to investigations and investigation_steps

``status_detail`` and ``end_reason`` were free English text written by the
orchestrator and rendered verbatim in the chat block, so a French UI showed a
French badge above an English sentence. This adds the machine-readable form —
a code plus a JSON params dict — that the frontend can translate.

The free-text columns STAY, deliberately: one family of detail cannot be
localized at all, because it is lifted from a third-party exception
(``str(httpx.ConnectError)``). A code therefore wins wherever one exists and
the raw text degrades to the previous behaviour where none can, rather than
losing the diagnostic entirely.

``investigations.leads_dropped`` lands here too. The count of leads discarded
at the plan cap was previously concatenated into the ``end_reason`` sentence
(the cheapest fix at the time, which avoided a migration); a number the block
wants to render should not have to be parsed back out of prose.

Unlike ``i1inv3302001`` — which created its tables and needed no lock recipe
because nothing could hold a lock on a table born in the same transaction —
this migration ALTERs existing tables, so it follows the canonical recipe:
idempotency probe -> terminate_lock_holders -> set_lock_timeout -> guarded
DDL. Every added column is nullable or constant-default, so each ADD COLUMN is
metadata-only on PG 11+ and no table is rewritten.

Revision ID: i2det3302002
Revises: i1inv3302001
Create Date: 2026-09-03 16:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "i2det3302002"
down_revision: str | Sequence[str] | None = "i1inv3302001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: ``{table: [(column, DDL type clause)]}``.
_ADDITIONS: dict[str, list[tuple[str, str]]] = {
    "investigations": [
        ("end_reason_code", "VARCHAR(40)"),
        ("end_reason_params", "JSON"),
        ("leads_dropped", "INTEGER NOT NULL DEFAULT 0"),
    ],
    "investigation_steps": [
        ("detail_code", "VARCHAR(40)"),
        ("detail_params", "JSON"),
    ],
}


def _column_exists(conn, table: str, column: str) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = current_schema() "
                "AND table_name = :table AND column_name = :column"
            ),
            {"table": table, "column": column},
        ).scalar()
    )


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    for table, columns in _ADDITIONS.items():
        # Fresh installs run create_all + stamp head; this body never executes.
        if not inspector.has_table(table):
            continue
        missing = [c for c in columns if not _column_exists(conn, table, c[0])]
        if not missing:
            continue
        # Once per table, not once per column: each ALTER takes its own brief
        # ACCESS EXCLUSIVE lock, and the pre-flight is what makes it acquire
        # instantly instead of queueing behind a long-lived reader.
        terminate_lock_holders(conn, table)
        set_lock_timeout(conn)
        for column, ddl_type in missing:
            op.execute(
                f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {ddl_type}"
            )


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    for table, columns in _ADDITIONS.items():
        if not inspector.has_table(table):
            continue
        present = [c for c in columns if _column_exists(conn, table, c[0])]
        if not present:
            continue
        terminate_lock_holders(conn, table)
        set_lock_timeout(conn)
        for column, _ddl_type in present:
            op.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS {column}")
