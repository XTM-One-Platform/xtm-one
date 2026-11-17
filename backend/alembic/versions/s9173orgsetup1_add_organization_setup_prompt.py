"""add platform_settings.organization_setup_prompted_at / _by_user_id / _outcome

The first-login organization setup proposal (#9173, S3): the first time an
administrator signs in to an XTM One platform whose organization profile was
never saved, the platform proposes the setup once. The proposal is answered
once per instance (set up now, later or dismissed) and never shown again:
these columns are that server-side record. Three nullable columns
(metadata-only on PG 11+), behind the canonical recipe: idempotency probe ->
terminate_lock_holders -> set_lock_timeout -> guarded DDL
(``platform_settings`` is read by every request).

Revision ID: s9173orgsetup1
Revises: p10157poll
Create Date: 2026-11-08
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

# revision identifiers, used by Alembic.
revision: str = "s9173orgsetup1"
# NOT a constant: must always be main's CURRENT head, re-pointed on every rebase
# (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | None = "p10157poll"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "platform_settings"
_COLUMNS = {
    "organization_setup_prompted_at": "TIMESTAMP WITH TIME ZONE",
    "organization_setup_prompted_by_user_id": "UUID",
    "organization_setup_prompt_outcome": "VARCHAR(20)",
}


def _missing_columns(conn) -> list[str]:
    rows = conn.execute(
        sa.text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = :table"
        ),
        {"table": _TABLE},
    ).scalars()
    present = set(rows)
    return [name for name in _COLUMNS if name not in present]


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    # Fresh installs run create_all + stamp head; this body never executes.
    if not inspector.has_table(_TABLE):
        return
    missing = _missing_columns(conn)
    if not missing:
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    for name in missing:
        op.execute(f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS {name} {_COLUMNS[name]}")


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if not inspector.has_table(_TABLE):
        return
    present = [name for name in _COLUMNS if name not in _missing_columns(conn)]
    if not present:
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    for name in present:
        op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS {name}")
