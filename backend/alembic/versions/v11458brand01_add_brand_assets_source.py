"""add platform_settings.brand_assets_source_url / brand_assets_source_integration_id

The brand asset source (Settings > Platform): a GitHub folder of brand asset
packs, read at every asset seed with the token of a GitHub integration
(``services/brand_asset_source.py``). Unset, the seed reads the packs the
image ships and the folders ``BRAND_ASSETS_EXTRA_DIRS`` names only.

Two nullable columns with no default (metadata-only), in ONE ``ALTER TABLE``
so the lock is taken once, following the canonical lock-safe recipe:
idempotency probe -> terminate_lock_holders -> set_lock_timeout -> guarded DDL.
No foreign key on the integration id: it would lock ``integrations`` too, and
a deleted integration is reported by the sync like a skill source's.

Revision ID: v11458brand01
Revises: hideact11624
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "v11458brand01"
down_revision: str | None = "hideact11624"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "platform_settings"
_COLUMNS = (
    ("brand_assets_source_url", "VARCHAR(2048)"),
    ("brand_assets_source_integration_id", "UUID"),
)


def _missing_columns(conn) -> list[tuple[str, str]]:
    existing = set(
        conn.execute(
            sa.text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = :table"
            ),
            {"table": _TABLE},
        ).scalars()
    )
    return [(name, kind) for name, kind in _COLUMNS if name not in existing]


def upgrade() -> None:
    conn = op.get_bind()
    missing = _missing_columns(conn)
    if not missing:
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    additions = ", ".join(f"ADD COLUMN IF NOT EXISTS {name} {kind}" for name, kind in missing)
    op.execute(f"ALTER TABLE {_TABLE} {additions}")


def downgrade() -> None:
    conn = op.get_bind()
    missing = {name for name, _ in _missing_columns(conn)}
    present = [name for name, _ in _COLUMNS if name not in missing]
    if not present:
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    drops = ", ".join(f"DROP COLUMN IF EXISTS {name}" for name in present)
    op.execute(f"ALTER TABLE {_TABLE} {drops}")
