"""platform_settings.audit_log_retention_days: audit log retention window (#9633)

One integer column on the settings singleton, 90 days by default, read by the
hourly audit log purge (``services/audit_retention.py``). ``audit_logs`` itself
is not altered: its ``created_at`` index (``ix_audit_logs_created_at``, baseline)
already serves the purge's range scan.

Lock safety: ``ADD COLUMN ... NOT NULL DEFAULT <constant>`` is catalogue-only on
PostgreSQL 11+ (no table rewrite), on a single-row table; guarded by an
idempotency probe and ``set_lock_timeout``. Reversible: ``downgrade`` drops it.

Revision ID: o9633auditret1
Revises: n9437bellopt1
Create Date: 2026-11-07 10:30:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout

revision: str = "o9633auditret1"
# NOT a constant: must always be main's CURRENT head, re-pointed on every rebase
# (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | None = "n9437bellopt1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "platform_settings"
_COLUMN = "audit_log_retention_days"


def _column_exists(conn, table: str, column: str) -> bool:
    inspector = sa.inspect(conn)
    if not inspector.has_table(table):
        return False
    return any(col["name"] == column for col in inspector.get_columns(table))


def upgrade() -> None:
    conn = op.get_bind()
    # Guarded: the migrations CI job runs upgrade twice.
    if _column_exists(conn, _TABLE, _COLUMN):
        return
    set_lock_timeout(conn)
    op.add_column(
        _TABLE,
        sa.Column(_COLUMN, sa.Integer(), nullable=False, server_default="90"),
    )


def downgrade() -> None:
    conn = op.get_bind()
    if not _column_exists(conn, _TABLE, _COLUMN):
        return
    set_lock_timeout(conn)
    op.drop_column(_TABLE, _COLUMN)
