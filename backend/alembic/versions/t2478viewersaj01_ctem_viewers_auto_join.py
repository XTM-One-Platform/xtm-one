"""turn auto join on for the seeded "CTEM Viewers" group (#2478)

#2441 asked for the seeded "CTEM Viewers" group (``groups.seed_key =
'ctem_viewers'``) with auto join on; the platforms seeded by #2445 got it off.
This sets ``auto_join = true`` on that one row, once: Alembic runs a revision a
single time, and the boot seed (``services/ctem_groups.py``) never changes a
seeded group that exists, so an administrator who turns the toggle off later
keeps it off. No other column and no other group is touched. A data-only
update of at most one row: no DDL, no lock beyond that row.

Revision ID: t2478viewersaj01
Revises: s2441ctemseed01
Create Date: 2026-11-26 08:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "t2478viewersaj01"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "s2441ctemseed01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _has_seed_key(conn: sa.engine.Connection) -> bool:
    inspector = sa.inspect(conn)
    if not inspector.has_table("groups"):
        return False
    return "seed_key" in {c["name"] for c in inspector.get_columns("groups")}


def upgrade() -> None:
    conn = op.get_bind()
    if not _has_seed_key(conn):
        return
    op.execute("UPDATE groups SET auto_join = true WHERE seed_key = 'ctem_viewers' AND auto_join IS NOT true")


def downgrade() -> None:
    conn = op.get_bind()
    if not _has_seed_key(conn):
        return
    op.execute("UPDATE groups SET auto_join = false WHERE seed_key = 'ctem_viewers' AND auto_join IS true")
