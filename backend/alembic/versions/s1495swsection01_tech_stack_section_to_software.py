"""the import and sync section ``tech_stack`` is named ``software`` (#1495)

Sam, 24 Nov 2026: nothing is released, so a retired name keeps no alias. The
saved syncs of the organization profile (``organization_context_syncs``) and
the stored organization-context category selections that still name
``tech_stack`` name ``software``, the section and category that replace it.

Data only, no DDL. The downgrade names them ``tech_stack`` again.

Revision ID: s1495swsection01
Revises: c1748ctemgrp01
Create Date: 2026-11-24 11:10:00.000000
"""

import json
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout

revision: str = "s1495swsection01"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "c1748ctemgrp01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Tables whose ``organization_context_categories`` column holds a category selection.
_SELECTION_TABLES = ("platform_settings", "agents", "assignments")


def _rename_section(conn: sa.engine.Connection, old: str, new: str) -> None:
    if not sa.inspect(conn).has_table("organization_context_syncs"):
        return
    conn.execute(
        sa.text("UPDATE organization_context_syncs SET section = :new WHERE section = :old"),
        {"old": old, "new": new},
    )


def _rename_selection(conn: sa.engine.Connection, old: str, new: str) -> None:
    """Every stored category selection naming *old* names *new*, in place, duplicates dropped."""
    inspector = sa.inspect(conn)
    for table in _SELECTION_TABLES:
        if not inspector.has_table(table):
            continue
        if "organization_context_categories" not in {c["name"] for c in inspector.get_columns(table)}:
            continue
        rows = conn.execute(
            sa.text(
                f"SELECT id, organization_context_categories FROM {table} "  # noqa: S608 - a constant table name
                "WHERE organization_context_categories::text LIKE :needle"
            ),
            {"needle": f'%"{old}"%'},
        ).all()
        for row_id, value in rows:
            names = json.loads(value) if isinstance(value, str) else value
            if not isinstance(names, list):
                continue
            renamed = list(dict.fromkeys(new if name == old else name for name in names))
            conn.execute(
                sa.text(
                    f"UPDATE {table} SET organization_context_categories = CAST(:value AS json) "  # noqa: S608
                    "WHERE id = :id"
                ),
                {"value": json.dumps(renamed), "id": row_id},
            )


def upgrade() -> None:
    conn = op.get_bind()
    set_lock_timeout(conn)
    _rename_section(conn, "tech_stack", "software")
    _rename_selection(conn, "tech_stack", "software")


def downgrade() -> None:
    conn = op.get_bind()
    set_lock_timeout(conn)
    _rename_section(conn, "software", "tech_stack")
