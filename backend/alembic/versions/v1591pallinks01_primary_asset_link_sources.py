"""primary-asset links carry source, roles, inherited needs (#1591)

``organization_crown_jewel_links`` (r1543assetrecords01) linked a crown
jewel, now a primary asset, to a record or a group with nothing else. The
OpenCRQ link sync (option ``link_supporting_assets``, on by default) imports
OpenCRQ ``support_to_primary`` links and ``depends_on`` chains, so a link
says where it comes from (``source``: manual / opencrq / rule), the OpenCRQ
link it mirrors (``external_id``), the roles of the supporting asset, the
security needs it inherits, the supporting asset a chain goes through
(``via``), and whether a person removed it (``removed``, a tombstone the
sync honours). Every existing row is a person's link: the defaults
(``manual``, empty) are its truth, so nothing is backfilled.

Revision ID: v1591pallinks01
Revises: c1503ctemcasestatus1
Create Date: 2026-11-23 22:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "v1591pallinks01"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "c1503ctemcasestatus1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LINKS = "organization_crown_jewel_links"
#: column -> its DDL; every one has a constant default, so the ADD is a catalog-only change.
_COLUMNS: dict[str, str] = {
    "source": "VARCHAR(80) NOT NULL DEFAULT 'manual'",
    "external_id": "VARCHAR(200) NOT NULL DEFAULT ''",
    "roles": "JSONB NOT NULL DEFAULT '[]'::jsonb",
    "inherited_needs": "JSONB NOT NULL DEFAULT '{}'::jsonb",
    "via": "VARCHAR(200) NOT NULL DEFAULT ''",
    "removed": "BOOLEAN NOT NULL DEFAULT false",
}


def _columns(conn: sa.engine.Connection) -> set[str] | None:
    inspector = sa.inspect(conn)
    if not inspector.has_table(_LINKS):
        return None
    return {c["name"] for c in inspector.get_columns(_LINKS)}


def upgrade() -> None:
    conn = op.get_bind()
    present = _columns(conn)
    if present is None or all(name in present for name in _COLUMNS):
        return
    terminate_lock_holders(conn, _LINKS)
    set_lock_timeout(conn)
    for name, ddl in _COLUMNS.items():
        op.execute(f"ALTER TABLE {_LINKS} ADD COLUMN IF NOT EXISTS {name} {ddl}")
    op.create_index(
        "ix_organization_crown_jewel_links_source", _LINKS, ["organization_id", "source"], if_not_exists=True
    )


def downgrade() -> None:
    conn = op.get_bind()
    present = _columns(conn)
    if present is None:
        return
    terminate_lock_holders(conn, _LINKS)
    set_lock_timeout(conn)
    op.drop_index("ix_organization_crown_jewel_links_source", table_name=_LINKS, if_exists=True)
    # The synced links and tombstones are dropped with their columns' meaning: a person's link stays.
    if "source" in present:
        op.execute("DELETE FROM organization_crown_jewel_links WHERE source <> 'manual' OR removed")
    for name in reversed(_COLUMNS):
        op.execute(f"ALTER TABLE {_LINKS} DROP COLUMN IF EXISTS {name}")
