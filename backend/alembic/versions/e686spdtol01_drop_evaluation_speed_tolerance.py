"""drop evaluations.speed_tolerance

Revision ID: e686spdtol01
Revises: x1784hubkey01
"""

from collections.abc import Sequence

from alembic import op
from app.core.migration_ddl import column_exists, set_lock_timeout, terminate_lock_holders

revision: str = "e686spdtol01"
down_revision: str | Sequence[str] | None = "x1784hubkey01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _has_speed_tolerance() -> bool:
    return column_exists(op.get_bind(), "evaluations", "speed_tolerance")


def _lock_evaluations() -> None:
    conn = op.get_bind()
    terminate_lock_holders(conn, "evaluations")
    set_lock_timeout(conn)


def upgrade() -> None:
    if not _has_speed_tolerance():
        return
    _lock_evaluations()
    op.execute("ALTER TABLE evaluations DROP COLUMN IF EXISTS speed_tolerance")


def downgrade() -> None:
    if _has_speed_tolerance():
        return
    _lock_evaluations()
    op.execute("ALTER TABLE evaluations ADD COLUMN IF NOT EXISTS speed_tolerance DOUBLE PRECISION NOT NULL DEFAULT 5.0")
    op.execute("UPDATE evaluations SET speed_tolerance = performance_tolerance")
