"""Platform Health: platform_health_snapshots

Slice S5 of issue #101 (design ``dev-docs/platform-health.md`` section 5): the
latest health snapshot a registered platform pushed with its registration, one
row per registration, replaced by every snapshot. Nothing is backfilled: a
platform sends its snapshot on its next heartbeat.

Lock profile: the new table carries a foreign key to ``platform_registrations``,
which takes a SHARE ROW EXCLUSIVE lock on it, so ``CREATE TABLE`` runs under
``set_lock_timeout``. The downgrade drops the table (ACCESS EXCLUSIVE on itself).
The column set mirrors ``PlatformHealthSnapshot`` in
``app/models/platform_health_snapshot.py``, so a fresh install's ``create_all``
and this body agree.

Revision ID: h125healthsnap01
Revises: i124defagentsrc
Create Date: 2026-11-20 00:20:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "h125healthsnap01"
# NOT a constant: must always be main's CURRENT head, re-pointed on every rebase
# (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "i124defagentsrc"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "platform_health_snapshots"
_REGISTRATIONS = "platform_registrations"


def _has_table(conn: sa.engine.Connection, table: str) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.tables WHERE table_schema = current_schema() AND table_name = :n"
            ),
            {"n": table},
        ).scalar()
    )


def upgrade() -> None:
    conn = op.get_bind()
    if not _has_table(conn, _TABLE):
        set_lock_timeout(conn)
        op.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {_TABLE} (
                registration_id UUID PRIMARY KEY
                    REFERENCES {_REGISTRATIONS} (id) ON DELETE CASCADE,
                schema_version INTEGER NOT NULL,
                snapshot JSONB NOT NULL,
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
