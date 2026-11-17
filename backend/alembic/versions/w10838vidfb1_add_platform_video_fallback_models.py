"""platform_settings.video_fallback_models: the configured video fallback list (#10838)

One nullable JSON column: the video models an AI shot falls back to, in order,
after the default video model fails. NULL means none (no backfill): every
shot is made on the default alone, as the text models' fallback is its own
setting.

Revision ID: w10838vidfb1
Revises: v10798vidrev1
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "w10838vidfb1"
# NOT a constant: must always be main's CURRENT head, re-pointed on every rebase
# (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | None = "v10798vidrev1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "platform_settings"
_COLUMN = "video_fallback_models"


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
    # Catalogue-only, but ALTER TABLE still needs ACCESS EXCLUSIVE: platform_settings
    # is read inside long transactions by the previous release's pods during a
    # rollout, so without the pre-flight the 5s lock_timeout fired on every attempt
    # and the platform never left v10798vidrev1 (production, 10 Nov 2026).
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    op.execute(f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS {_COLUMN} JSON")


def downgrade() -> None:
    conn = op.get_bind()
    if not _column_exists(conn, _TABLE, _COLUMN):
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS {_COLUMN}")
