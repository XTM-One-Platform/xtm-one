"""add xtm_hub_registrations (this platform's registration with the XTM Hub)

Creates ``xtm_hub_registrations``: at most one row (``id`` = 1, checked)
holding the platform token the XTM Hub issued when an administrator
registered XTM One, Fernet-encrypted, with the Hub and platform id it was
issued for, the heartbeat's last answer and a ``generation`` the heartbeat
compares-and-sets on.

``CREATE TABLE IF NOT EXISTS`` without a foreign key takes no lock on any
existing relation, so the upgrade needs no lock pre-flight. The downgrade's
``DROP TABLE`` takes ACCESS EXCLUSIVE and follows the canonical recipe:
idempotency probe -> terminate_lock_holders -> set_lock_timeout -> guarded
DDL.

The column set mirrors the ORM model (``app/models/xtm_hub_registration.py``):
a fresh install runs ``create_all`` from the model while an existing install
runs this body, and the two must converge.

Revision ID: k1hub0001
Revises: o2oapi00003
Create Date: 2026-10-02 14:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "k1hub0001"
down_revision: str | None = "o2oapi00003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "xtm_hub_registrations"


def _table_exists(conn, table: str) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = current_schema() AND table_name = :table"
            ),
            {"table": table},
        ).scalar()
    )


def upgrade() -> None:
    conn = op.get_bind()
    if _table_exists(conn, _TABLE):
        return
    op.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {_TABLE} (
            id INTEGER PRIMARY KEY,
            hub_url TEXT NOT NULL,
            platform_id UUID NOT NULL,
            token_enc TEXT NOT NULL,
            status VARCHAR(32) NOT NULL,
            generation INTEGER NOT NULL,
            registered_at TIMESTAMPTZ NOT NULL,
            last_check_at TIMESTAMPTZ,
            last_check_result VARCHAR(32),
            CONSTRAINT ck_xtm_hub_registrations_singleton CHECK (id = 1)
        )
        """
    )


def downgrade() -> None:
    conn = op.get_bind()
    if not _table_exists(conn, _TABLE):
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    op.execute(f"DROP TABLE IF EXISTS {_TABLE}")
