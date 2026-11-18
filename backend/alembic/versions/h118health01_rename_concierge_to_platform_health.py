"""rename the Concierge schema to Platform Health (#101, spec 2.3)

``dev-docs/platform-health.md`` section 2.3, one migration, every value carried:

* columns: ``agents.concierge_improvement_opt_in`` and
  ``flows.concierge_improvement_opt_in`` to ``health_enrolled``, plus a new
  ``health_content_opt_in`` on both carrying the same value (the owner's
  former opt-in was a consent to content, so it stays one);
  ``users.concierge_opt_out`` to ``health_content_opt_out``;
  ``agents.concierge_role`` to ``health_role``;
  ``platform_settings.concierge_settings`` to ``health_settings`` (the JSON is
  carried as stored; S2's ``HealthSettings`` reads the thresholds and ignores
  ``scope_mode``, which never becomes an enrolment);
* tables: the ten ``concierge_*`` tables to ``health_*``. Indexes, constraints
  and foreign keys follow a table rename in PostgreSQL by OID, so their names
  are kept and no index is rebuilt.

Every statement is a catalog-only ``RENAME`` (no rewrite) except the one
``UPDATE`` of the enrolled rows. Lock-safe recipe both ways (idempotency
probe, ``terminate_lock_holders``, ``set_lock_timeout``, guarded DDL); a fresh
install (``create_all`` + ``stamp head``) already has the new names and the
probe makes this a no-op. The previous-version pods read the old names during
a rolling deploy: the same window as any rename, accepted by the spec.

Revision ID: h118health01
Revises: a105a2arot1
Create Date: 2026-11-18 12:20:00.000000
"""

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "h118health01"
# NOT a constant: must always be main's CURRENT head, re-pointed on every rebase
# (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "a105a2arot1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: (table, old column, new column).
_COLUMNS = (
    ("agents", "concierge_improvement_opt_in", "health_enrolled"),
    ("agents", "concierge_role", "health_role"),
    ("flows", "concierge_improvement_opt_in", "health_enrolled"),
    ("users", "concierge_opt_out", "health_content_opt_out"),
    ("platform_settings", "concierge_settings", "health_settings"),
)
#: The second opt-in, seeded from ``health_enrolled``.
_CONTENT_OPT_IN = ("agents", "flows")
_TABLES = (
    "cycles",
    "budget_months",
    "budget_work",
    "cycle_runs",
    "signal_states",
    "schedule_marks",
    "dataset_entry_sources",
    "experiments",
    "findings",
    "pets",
)


def _tables(conn: Any) -> set[str]:
    return set(
        conn.execute(
            sa.text("SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema()")
        ).scalars()
    )


def _columns(conn: Any, table: str) -> set[str]:
    return set(
        conn.execute(
            sa.text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = :t"
            ),
            {"t": table},
        ).scalars()
    )


def _rename_columns(conn: Any, pairs: Sequence[tuple[str, str, str]]) -> None:
    for table, old, new in pairs:
        cols = _columns(conn, table)
        if old in cols and new not in cols:
            terminate_lock_holders(conn, table)
            set_lock_timeout(conn)
            op.execute(f"ALTER TABLE {table} RENAME COLUMN {old} TO {new}")


def _rename_tables(conn: Any, pairs: Sequence[tuple[str, str]]) -> None:
    for old, new in pairs:
        present = _tables(conn)
        if old in present and new not in present:
            terminate_lock_holders(conn, old)
            set_lock_timeout(conn)
            op.execute(f"ALTER TABLE {old} RENAME TO {new}")


def upgrade() -> None:
    conn = op.get_bind()
    _rename_columns(conn, _COLUMNS)
    for table in _CONTENT_OPT_IN:
        cols = _columns(conn, table)
        if "health_content_opt_in" in cols or "health_enrolled" not in cols:
            continue
        terminate_lock_holders(conn, table)
        set_lock_timeout(conn)
        op.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS health_content_opt_in BOOLEAN NOT NULL DEFAULT false")
        op.execute(f"UPDATE {table} SET health_content_opt_in = true WHERE health_enrolled IS TRUE")  # noqa: S608 - table is a module constant
    _rename_tables(conn, [(f"concierge_{t}", f"health_{t}") for t in _TABLES])


def downgrade() -> None:
    conn = op.get_bind()
    _rename_tables(conn, [(f"health_{t}", f"concierge_{t}") for t in reversed(_TABLES)])
    for table in _CONTENT_OPT_IN:
        if "health_content_opt_in" in _columns(conn, table):
            terminate_lock_holders(conn, table)
            set_lock_timeout(conn)
            op.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS health_content_opt_in")
    _rename_columns(conn, [(table, new, old) for table, old, new in reversed(_COLUMNS)])
