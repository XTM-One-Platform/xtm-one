"""the crown jewels category is named primary assets (#1591, slice 2b)

The organization-context category that lists the organization's primary
assets (OpenCRQ's word) is ``primary_assets``; ``crown_jewels`` stays a read
alias for one release. The stored category selections (platform settings,
agents, assignments) naming ``crown_jewels`` name ``primary_assets``, in place,
duplicates dropped. The profile key ``crown_jewels`` itself is not touched.

Data only, no DDL. The downgrade names ``crown_jewels`` again.

Revision ID: p1591primcat01
Revises: v1591pallinks01
Create Date: 2026-11-24 05:00:00.000000
"""

import json
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout

revision: str = "p1591primcat01"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "v1591pallinks01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Tables whose ``organization_context_categories`` JSON list names categories.
_SELECTION_TABLES = ("platform_settings", "agents", "assignments")


def rename_list(names: list, old: str, new: str) -> list:
    """*names* with *old* named *new*, the first occurrence kept."""
    return list(dict.fromkeys(new if name == old else name for name in names))


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
            conn.execute(
                sa.text(
                    f"UPDATE {table} SET organization_context_categories = CAST(:value AS json) "  # noqa: S608
                    "WHERE id = :id"
                ),
                {"value": json.dumps(rename_list(names, old, new)), "id": row_id},
            )


def upgrade() -> None:
    conn = op.get_bind()
    set_lock_timeout(conn)
    _rename_selection(conn, "crown_jewels", "primary_assets")


def downgrade() -> None:
    conn = op.get_bind()
    set_lock_timeout(conn)
    _rename_selection(conn, "primary_assets", "crown_jewels")
