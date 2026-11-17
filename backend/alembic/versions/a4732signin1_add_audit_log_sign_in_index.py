"""add ix_audit_logs_sign_ins - each account's completed sign-ins

The Security Audit reports the automations of accounts nobody signed in to for
30 days by reading each owner's completed sign-ins (``login`` on the
user) out of ``audit_logs``, which is never pruned. With the single-column
indexes that read intersects every sign-in ever recorded and grows with the
table; this partial index holds the sign-ins only, keyed by user then time,
so each owner costs one index probe.

Purely additive. Built by ``create_index_concurrently`` in an autocommit
block, outside the ``lock_timeout`` belt: every audited action writes
``audit_logs``, so a plain build's SHARE lock would block them, sign-ins
included; the helper bounds the build on this pre-existing table and drops
and rebuilds an INVALID leftover of an interrupted one.

Revision ID: a4732signin1
Revises: c3785htmlkb01
Create Date: 2026-09-29 00:00:00.000000
"""

from collections.abc import Sequence

from alembic import op
from app.core.migration_ddl import create_index_concurrently

revision: str = "a4732signin1"
down_revision: str | Sequence[str] | None = "c3785htmlkb01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "audit_logs"
_INDEX = "ix_audit_logs_sign_ins"
_DEFINITION = "(user_id, created_at) WHERE action = 'login' AND entity_type = 'user'"


def upgrade() -> None:
    conn = op.get_bind()
    with op.get_context().autocommit_block():
        create_index_concurrently(conn, _INDEX, _TABLE, _DEFINITION)


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX}")
