"""add organizations and the per-organization profile versions (#9157)

The primary organization plus the organizations it tracks
(``models/organization.py``, design ``dev-docs/ctem-fleets.md`` section 9.2
and 9.3). ``organization_context_versions`` gains ``organization_id`` and its
version numbers become per organization.

Steps, each one re-runnable (idempotency probe first, the lock-safe recipe
``terminate_lock_holders`` + ``set_lock_timeout`` before every lock-taking
DDL):

1. create ``organizations`` (new, empty: its partial unique index is built
   in place, no CONCURRENTLY);
2. insert the primary organization, named after the latest profile
   (``name``), else "Primary";
3. add ``organization_context_versions.organization_id`` (nullable), point
   every existing version at the primary (the table holds one row per saved
   profile: a handful of rows, a bounded update), the FK and the not-null
   rule added ``NOT VALID`` then validated (no ``SET NOT NULL`` scan under an
   exclusive lock);
4. the unique ``(organization_id, version)`` index built in place, then the
   old ``unique(version)`` constraint dropped.

No behaviour change: every reader that names no organization reads the
primary's versions, which are every existing version.

Revision ID: m9157orgs01
Revises: c9160fleetoptin1
Create Date: 2026-11-06 11:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_ddl import create_index_in_place, set_lock_timeout, terminate_lock_holders

revision: str = "m9157orgs01"
# NOT a constant: must always be main's CURRENT head, re-pointed on every rebase
# (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "c9160fleetoptin1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORGS = "organizations"
_VERSIONS = "organization_context_versions"
_PRIMARY_INDEX = "uq_organizations_one_primary"
_FK = "fk_organization_context_versions_organization_id"
_NOT_NULL = "ck_organization_context_versions_organization_id_not_null"
_UNIQUE_INDEX = "uq_organization_context_versions_org_version"
_OLD_UNIQUE = "organization_context_versions_version_key"


def _has_table(conn, table: str) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.tables WHERE table_schema = current_schema() AND table_name = :n"
            ),
            {"n": table},
        ).scalar()
    )


def _has_column(conn, table: str, column: str) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = :t AND column_name = :c"
            ),
            {"t": table, "c": column},
        ).scalar()
    )


def _constraint_state(conn, name: str) -> str | None:
    """``"valid"`` / ``"not_valid"`` for *name*, ``None`` when absent."""
    row = conn.execute(
        sa.text(
            "SELECT convalidated FROM pg_constraint "
            "WHERE conname = :n AND connamespace = current_schema()::regnamespace"
        ),
        {"n": name},
    ).scalar()
    if row is None:
        return None
    return "valid" if row else "not_valid"


def _create_organizations(conn) -> None:
    if _has_table(conn, _ORGS):
        return
    set_lock_timeout(conn)
    op.create_table(
        _ORGS,
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("relationship", sa.String(16), nullable=True),
        sa.Column("criticality", sa.String(16), nullable=False, server_default=sa.text("'medium'")),
        sa.Column("opencti_organization_id", sa.String(100), nullable=True),
        sa.Column("monitoring", sa.String(8), nullable=False, server_default=sa.text("'off'")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("role IN ('primary', 'tracked')", name="ck_organizations_role_valid"),
        sa.CheckConstraint(
            "(role = 'primary' AND relationship IS NULL) OR "
            "(role = 'tracked' AND relationship IS NOT NULL"
            " AND relationship IN ('provider', 'customer', 'partner', 'subsidiary'))",
            name="ck_organizations_relationship_valid",
        ),
        sa.CheckConstraint(
            "criticality IN ('low', 'medium', 'high', 'critical')", name="ck_organizations_criticality_valid"
        ),
        sa.CheckConstraint("monitoring IN ('off', 'on')", name="ck_organizations_monitoring_valid"),
        if_not_exists=True,
    )
    # A new, empty table: the index is built in place within the transaction.
    op.execute(
        sa.text(f"CREATE UNIQUE INDEX IF NOT EXISTS {_PRIMARY_INDEX} ON {_ORGS} (role) WHERE role = 'primary'")
    )


def _insert_primary(conn) -> None:
    if conn.execute(sa.text("SELECT 1 FROM organizations WHERE role = 'primary'")).scalar():
        return
    name = None
    if _has_table(conn, _VERSIONS):
        name = conn.execute(
            sa.text(
                "SELECT NULLIF(btrim(profile->>'name'), '') FROM organization_context_versions "
                "ORDER BY version DESC LIMIT 1"
            )
        ).scalar()
    conn.execute(
        sa.text(
            "INSERT INTO organizations (id, name, role, criticality, monitoring) "
            "VALUES (gen_random_uuid(), :name, 'primary', 'critical', 'off')"
        ),
        {"name": (name or "Primary")[:200]},
    )


def _link_versions(conn) -> None:
    if not _has_column(conn, _VERSIONS, "organization_id"):
        terminate_lock_holders(conn, _VERSIONS)
        set_lock_timeout(conn)
        op.execute(sa.text(f"ALTER TABLE {_VERSIONS} ADD COLUMN IF NOT EXISTS organization_id uuid"))
    conn.execute(
        sa.text(
            "UPDATE organization_context_versions "
            "SET organization_id = (SELECT id FROM organizations WHERE role = 'primary') "
            "WHERE organization_id IS NULL"
        )
    )
    if _constraint_state(conn, _FK) is None:
        terminate_lock_holders(conn, _VERSIONS)
        terminate_lock_holders(conn, _ORGS)
        set_lock_timeout(conn)
        op.execute(
            sa.text(
                f"ALTER TABLE {_VERSIONS} ADD CONSTRAINT {_FK} FOREIGN KEY (organization_id) "
                f"REFERENCES {_ORGS}(id) ON DELETE CASCADE NOT VALID"
            )
        )
    if _constraint_state(conn, _FK) == "not_valid":
        op.execute(sa.text(f"ALTER TABLE {_VERSIONS} VALIDATE CONSTRAINT {_FK}"))
    if _constraint_state(conn, _NOT_NULL) is None:
        terminate_lock_holders(conn, _VERSIONS)
        set_lock_timeout(conn)
        op.execute(
            sa.text(
                f"ALTER TABLE {_VERSIONS} ADD CONSTRAINT {_NOT_NULL} CHECK (organization_id IS NOT NULL) NOT VALID"
            )
        )
    if _constraint_state(conn, _NOT_NULL) == "not_valid":
        op.execute(sa.text(f"ALTER TABLE {_VERSIONS} VALIDATE CONSTRAINT {_NOT_NULL}"))


def upgrade() -> None:
    conn = op.get_bind()
    if not _has_table(conn, _VERSIONS):
        return
    _create_organizations(conn)
    _insert_primary(conn)
    _link_versions(conn)
    with op.get_context().autocommit_block():
        create_index_in_place(conn, _UNIQUE_INDEX, _VERSIONS, "(organization_id, version)", unique=True, required=True)
    if _constraint_state(conn, _OLD_UNIQUE) is not None:
        terminate_lock_holders(conn, _VERSIONS)
        set_lock_timeout(conn)
        op.execute(sa.text(f"ALTER TABLE {_VERSIONS} DROP CONSTRAINT IF EXISTS {_OLD_UNIQUE}"))


def downgrade() -> None:
    conn = op.get_bind()
    if not _has_table(conn, _VERSIONS):
        return
    if _has_column(conn, _VERSIONS, "organization_id"):
        # Back to one sequence: only the primary's versions fit unique(version).
        conn.execute(
            sa.text(
                "DELETE FROM organization_context_versions WHERE organization_id IS DISTINCT FROM "
                "(SELECT id FROM organizations WHERE role = 'primary')"
            )
        )
    terminate_lock_holders(conn, _VERSIONS)
    set_lock_timeout(conn)
    if _constraint_state(conn, _OLD_UNIQUE) is None:
        op.execute(sa.text(f"ALTER TABLE {_VERSIONS} ADD CONSTRAINT {_OLD_UNIQUE} UNIQUE (version)"))
    op.execute(sa.text(f"DROP INDEX IF EXISTS {_UNIQUE_INDEX}"))
    op.execute(sa.text(f"ALTER TABLE {_VERSIONS} DROP CONSTRAINT IF EXISTS {_NOT_NULL}"))
    op.execute(sa.text(f"ALTER TABLE {_VERSIONS} DROP CONSTRAINT IF EXISTS {_FK}"))
    op.execute(sa.text(f"ALTER TABLE {_VERSIONS} DROP COLUMN IF EXISTS organization_id"))
    if _has_table(conn, _ORGS):
        terminate_lock_holders(conn, _ORGS)
        op.drop_table(_ORGS, if_exists=True)
