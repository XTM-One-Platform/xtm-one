"""platform concierge: identity role, improvement opt-in, user opt-out, settings (#8254)

Level 1 of the Platform Concierge (#1783, ``dev-docs/platform-concierge.md``):

* ``agents.concierge_improvement_opt_in`` and ``flows.concierge_improvement_opt_in``
  (bool, false): the managers' consent to the Concierge reading content;
* ``agents.concierge_role`` (varchar, NULL): the fleet role that makes a run
  act as the Concierge identity;
* ``users.concierge_opt_out`` (bool, false): the user's opt-out;
* ``platform_settings.concierge_settings`` (JSON, NULL = defaults).

Safety contract (``dev-docs/reference/migrations-safety.md``):

* Idempotent - every column is probed first and guarded
  (``IF NOT EXISTS``).
* Lock-safe - a nullable column and a constant-default NOT NULL column are
  metadata-only; each existing table gets the canonical recipe (probe,
  ``terminate_lock_holders``, ``set_lock_timeout``), one table at a time.
* Reversible - the downgrade drops the columns; stored opt-ins, opt-outs and
  settings go with them.

Revision ID: c1cnc8254011
Revises: g9grpaioff01
Create Date: 2026-11-03 21:30:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "c1cnc8254011"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "g9grpaioff01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("agents", "concierge_improvement_opt_in", "BOOLEAN NOT NULL DEFAULT false"),
    ("agents", "concierge_role", "VARCHAR(32)"),
    ("flows", "concierge_improvement_opt_in", "BOOLEAN NOT NULL DEFAULT false"),
    ("users", "concierge_opt_out", "BOOLEAN NOT NULL DEFAULT false"),
    ("platform_settings", "concierge_settings", "JSON"),
)


def _columns(conn, table: str) -> set[str]:
    rows = conn.execute(
        sa.text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = :t"
        ),
        {"t": table},
    )
    return {row[0] for row in rows}


def _alter(conn, table: str, statements: list[str]) -> None:
    if not statements:
        return
    terminate_lock_holders(conn, table)
    set_lock_timeout(conn)
    for statement in statements:
        conn.execute(sa.text(f"ALTER TABLE {table} {statement}"))


def upgrade() -> None:
    conn = op.get_bind()
    for table in dict.fromkeys(t for t, _, _ in _COLUMNS):
        present = _columns(conn, table)
        _alter(
            conn,
            table,
            [f"ADD COLUMN IF NOT EXISTS {name} {ddl}" for t, name, ddl in _COLUMNS if t == table and name not in present],
        )


def downgrade() -> None:
    conn = op.get_bind()
    for table in dict.fromkeys(t for t, _, _ in _COLUMNS):
        present = _columns(conn, table)
        _alter(
            conn,
            table,
            [f"DROP COLUMN IF EXISTS {name}" for t, name, _ in _COLUMNS if t == table and name in present],
        )
