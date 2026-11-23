"""organization_asset_groups: the members a source names that the inventory lacks, and their last read (#1556)

``unresolved_members`` counts the members a group's source named that no
record of the inventory holds yet ("not in the inventory", never dropped);
``members_synced_at`` is when the source last answered the whole member list.
The memberships themselves are rows of ``organization_asset_group_members``
(#1543), written by source and sync.

Lock-light: two ``ADD COLUMN``s, one with a constant default and one
nullable, both catalog changes on PostgreSQL 11+, no rewrite. Re-running after
an interruption is a no-op (each column is probed first).

Revision ID: g1556grpmembers1
Revises: u1595nopastel01
Create Date: 2026-11-23 23:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "g1556grpmembers1"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "u1595nopastel01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_GROUPS = "organization_asset_groups"
_UNRESOLVED = "unresolved_members"
_SYNCED = "members_synced_at"


def _columns(conn: sa.engine.Connection) -> set[str]:
    rows = conn.execute(
        sa.text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = :t"
        ),
        {"t": _GROUPS},
    ).scalars()
    return set(rows)


def upgrade() -> None:
    conn = op.get_bind()
    present = _columns(conn)
    if not present or {_UNRESOLVED, _SYNCED} <= present:  # no table (fresh installs run create_all) or done
        return
    terminate_lock_holders(conn, _GROUPS)
    set_lock_timeout(conn)
    if _UNRESOLVED not in present:
        op.add_column(_GROUPS, sa.Column(_UNRESOLVED, sa.Integer(), nullable=False, server_default=sa.text("0")))
    if _SYNCED not in present:
        op.add_column(_GROUPS, sa.Column(_SYNCED, sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    conn = op.get_bind()
    present = _columns(conn)
    if not present & {_UNRESOLVED, _SYNCED}:
        return
    terminate_lock_holders(conn, _GROUPS)
    set_lock_timeout(conn)
    for column in (_SYNCED, _UNRESOLVED):
        if column in present:
            op.drop_column(_GROUPS, column)
