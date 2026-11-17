"""add platform_procedure_manifests and the reported manifest fingerprint

A registered OpenCRQ hands XTM One the procedures it serves, with its registration:
``platform_procedure_manifests`` holds them, one row per registration, and
``platform_registrations.procedure_manifest_fingerprint`` the fingerprint the platform
reported last. Nothing is backfilled: a platform sends its manifest on its next heartbeat.

Lock profile: the new table carries a foreign key to ``platform_registrations``, which takes a
SHARE ROW EXCLUSIVE lock on it, so ``CREATE TABLE`` runs under ``set_lock_timeout``. The new
column is metadata-only but needs a brief ACCESS EXCLUSIVE lock on ``platform_registrations``,
so it follows the canonical recipe (probe, ``terminate_lock_holders``, ``set_lock_timeout``).
The downgrade drops the table (ACCESS EXCLUSIVE on itself) and the column the same way.

The column set mirrors ``PlatformProcedureManifest`` in
``app/models/platform_procedure_manifest.py`` and the column ``PlatformRegistration``
declares, so a fresh install's ``create_all`` and this body agree.

Revision ID: m6239manifest01
Revises: u6299emlcase01
Create Date: 2026-10-22 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "m6239manifest01"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "u6299emlcase01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "platform_procedure_manifests"
_REGISTRATIONS = "platform_registrations"
_COLUMN = "procedure_manifest_fingerprint"


def _has_table(conn, table: str) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.tables WHERE table_schema = current_schema() AND table_name = :n"
            ),
            {"n": table},
        ).scalar()
    )


def _has_column(conn) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = :t AND column_name = :c"
            ),
            {"t": _REGISTRATIONS, "c": _COLUMN},
        ).scalar()
    )


def upgrade() -> None:
    conn = op.get_bind()
    if not _has_column(conn):
        terminate_lock_holders(conn, _REGISTRATIONS)
        set_lock_timeout(conn)
        op.execute(f"ALTER TABLE {_REGISTRATIONS} ADD COLUMN IF NOT EXISTS {_COLUMN} VARCHAR(64)")
    if not _has_table(conn, _TABLE):
        set_lock_timeout(conn)
        op.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {_TABLE} (
                registration_id UUID PRIMARY KEY
                    REFERENCES {_REGISTRATIONS} (id) ON DELETE CASCADE,
                fingerprint VARCHAR(64) NOT NULL,
                procedures JSON NOT NULL,
                received_at TIMESTAMPTZ NOT NULL
            )
            """
        )


def downgrade() -> None:
    conn = op.get_bind()
    if _has_table(conn, _TABLE):
        terminate_lock_holders(conn, _TABLE)
        set_lock_timeout(conn)
        op.execute(f"DROP TABLE IF EXISTS {_TABLE}")
    if _has_column(conn):
        terminate_lock_holders(conn, _REGISTRATIONS)
        set_lock_timeout(conn)
        op.execute(f"ALTER TABLE {_REGISTRATIONS} DROP COLUMN IF EXISTS {_COLUMN}")
