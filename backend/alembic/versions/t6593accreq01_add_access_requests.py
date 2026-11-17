"""add access requests

``access_requests`` holds what a viewer of a pasted XTM One link asked for
from its card (``dev-docs/teams-link-unfurl.md``, section 9): for now an
action request on an assignment run. A new, empty table: its foreign key to
``users`` takes a SHARE ROW EXCLUSIVE lock there, so the ``CREATE TABLE`` runs
under ``set_lock_timeout``; its two indexes are built on the empty table in
the same transaction.

Revision ID: t6593accreq01
Revises: m8654heads01
Create Date: 2026-10-27 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout

revision: str = "t6593accreq01"
# NOT a constant: must always be the base branch's CURRENT head, re-pointed
# whenever it gains a migration (backend/AGENTS.md > "Database migrations
# (safety)", rule 1).
down_revision: str | Sequence[str] | None = "m8654heads01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "access_requests"


def upgrade() -> None:
    conn = op.get_bind()
    if sa.inspect(conn).has_table(_TABLE):
        return
    set_lock_timeout(conn)
    op.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {_TABLE} (
            id UUID PRIMARY KEY,
            kind VARCHAR(20) NOT NULL,
            entity_type VARCHAR(50) NOT NULL,
            entity_id UUID NOT NULL,
            requester_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            status VARCHAR(20) NOT NULL,
            source VARCHAR(30) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL,
            closed_at TIMESTAMPTZ,
            closed_by_id UUID
        )
        """
    )
    op.execute(
        f"CREATE UNIQUE INDEX IF NOT EXISTS uq_access_requests_open ON {_TABLE} "
        "(requester_id, entity_type, entity_id) WHERE status = 'open'"
    )
    op.execute(f"CREATE INDEX IF NOT EXISTS ix_access_requests_entity ON {_TABLE} (entity_type, entity_id)")


def downgrade() -> None:
    conn = op.get_bind()
    if not sa.inspect(conn).has_table(_TABLE):
        return
    # Dropping the foreign key takes a lock on `users`.
    set_lock_timeout(conn)
    op.execute(f"DROP TABLE IF EXISTS {_TABLE}")
