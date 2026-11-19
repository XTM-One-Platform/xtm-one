"""rename the concierge_* index and constraint names of the health_* tables (#101, spec 2.3)

``h118health01`` renamed the ten ``concierge_*`` tables to ``health_*`` and kept
the names of their indexes and constraints (they follow a table rename by OID).
This migration renames those names too, so a migrated database carries the same
names as a fresh install (``create_all`` from the models, which now declare
``ix_health_*``, ``uq_health_*`` and ``ck_health_*``):

* constraints (primary keys, foreign keys, unique and check constraints): every
  name on a ``health_*`` table holding the token ``concierge_`` (at the start or
  after an underscore) gets its first such token replaced by ``health_``. A
  primary key or unique constraint renames the index it owns with it;
* indexes left after that (the plain and partial unique indexes): same rule.

The names are read from the catalog, never listed by hand, so an index created
by an older migration that the models do not declare
(``ix_concierge_cycles_month``, ``ix_concierge_budget_work_cycle_id``,
``ix_concierge_cycle_runs_run_id``) is carried too. Every statement is a
catalog-only ``RENAME`` (no rewrite, no index rebuilt). Idempotent: a name whose
target already exists is skipped, and a fresh install has no ``concierge_``
name, which makes this a no-op. Lock-safe recipe per table
(``terminate_lock_holders``, ``set_lock_timeout``). The downgrade writes the
former names back.

Revision ID: h120healthidx
Revises: h119healthgrp
Create Date: 2026-11-19 04:40:00.000000
"""

import re
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "h120healthidx"
# NOT a constant: must always be main's CURRENT head, re-pointed on every rebase
# (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "h119healthgrp"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: The tables ``h118health01`` renamed, by their new names.
TABLES = (
    "health_cycles",
    "health_budget_months",
    "health_budget_work",
    "health_cycle_runs",
    "health_signal_states",
    "health_schedule_marks",
    "health_dataset_entry_sources",
    "health_experiments",
    "health_findings",
    "health_pets",
)


def renamed(name: str, old: str, new: str) -> str | None:
    """*name* with its first ``old_`` token (at the start or after ``_``) as ``new_``; None when it has none."""
    pattern = re.compile(rf"(^|_){old}_")
    if not pattern.search(name):
        return None
    return pattern.sub(lambda m: f"{m.group(1)}{new}_", name, count=1)


def _relations(conn: Any) -> set[str]:
    return set(
        conn.execute(
            sa.text(
                "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = current_schema()"
            )
        ).scalars()
    )


def _constraints(conn: Any) -> list[tuple[str, str]]:
    rows = conn.execute(
        sa.text(
            "SELECT t.relname, c.conname FROM pg_constraint c "
            "JOIN pg_class t ON t.oid = c.conrelid "
            "JOIN pg_namespace n ON n.oid = t.relnamespace "
            "WHERE n.nspname = current_schema() AND t.relname = ANY(:tables) "
            "ORDER BY t.relname, c.conname"
        ),
        {"tables": list(TABLES)},
    )
    return [(row[0], row[1]) for row in rows]


def _indexes(conn: Any) -> list[tuple[str, str]]:
    rows = conn.execute(
        sa.text(
            "SELECT t.relname, i.relname FROM pg_index x "
            "JOIN pg_class i ON i.oid = x.indexrelid "
            "JOIN pg_class t ON t.oid = x.indrelid "
            "JOIN pg_namespace n ON n.oid = t.relnamespace "
            "WHERE n.nspname = current_schema() AND t.relname = ANY(:tables) "
            "ORDER BY t.relname, i.relname"
        ),
        {"tables": list(TABLES)},
    )
    return [(row[0], row[1]) for row in rows]


def _lock(conn: Any, table: str, locked: set[str]) -> None:
    if table in locked:
        return
    terminate_lock_holders(conn, table)
    set_lock_timeout(conn)
    locked.add(table)


def _rename_names(old: str, new: str) -> None:
    conn = op.get_bind()
    locked: set[str] = set()
    # Constraints first: a primary key or unique constraint carries its index.
    for table, name in _constraints(conn):
        target = renamed(name, old, new)
        if target is None:
            continue
        taken = {c for _, c in _constraints(conn)} | _relations(conn)
        if target in taken:
            continue
        _lock(conn, table, locked)
        op.execute(f'ALTER TABLE "{table}" RENAME CONSTRAINT "{name}" TO "{target}"')
    for table, name in _indexes(conn):
        target = renamed(name, old, new)
        if target is None or target in _relations(conn):
            continue
        _lock(conn, table, locked)
        op.execute(f'ALTER INDEX "{name}" RENAME TO "{target}"')


def upgrade() -> None:
    _rename_names("concierge", "health")


def downgrade() -> None:
    _rename_names("health", "concierge")
