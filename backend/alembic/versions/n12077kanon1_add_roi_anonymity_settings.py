"""add platform_settings.roi_min_group_size and roi_by_person_visible

Slice L5 of issue #12077 (design ``dev-docs/roi-outcomes.md`` 12.5):
``roi_min_group_size`` is the k of the ROI k-anonymity rule (default 5, 2 to
50); ``roi_by_person_visible`` opens the administrators' "By person" breakdown
(decision D12-1, default off).

Two constant-default ``ADD COLUMN``, metadata-only on PG 11+. Idempotent via a
column-existence probe and lock-safe per the canonical recipe: idempotency
probe -> terminate_lock_holders -> set_lock_timeout -> guarded DDL
(``platform_settings`` is read by every request). Fresh installs run
``create_all`` + ``stamp head``: the body never executes there.

Revision ID: n12077kanon1
Revises: m12077costfct1
Create Date: 2026-11-15 20:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "n12077kanon1"
# NOT a constant: must always be main's CURRENT head, re-pointed on every rebase
# (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "m12077costfct1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "platform_settings"
_COLUMNS: tuple[tuple[str, str], ...] = (
    ("roi_min_group_size", "INTEGER NOT NULL DEFAULT 5"),
    ("roi_by_person_visible", "BOOLEAN NOT NULL DEFAULT false"),
)


def _has_column(conn: sa.engine.Connection, column: str) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = current_schema() "
                "AND table_name = :table AND column_name = :column"
            ),
            {"table": _TABLE, "column": column},
        ).first()
    )


def upgrade() -> None:
    conn = op.get_bind()
    if not sa.inspect(conn).has_table(_TABLE):
        return
    missing = [(name, definition) for name, definition in _COLUMNS if not _has_column(conn, name)]
    if not missing:
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    for name, definition in missing:
        op.execute(f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS {name} {definition}")


def downgrade() -> None:
    conn = op.get_bind()
    if not sa.inspect(conn).has_table(_TABLE):
        return
    present = [name for name, _definition in _COLUMNS if _has_column(conn, name)]
    if not present:
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    for name in present:
        op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS {name}")
