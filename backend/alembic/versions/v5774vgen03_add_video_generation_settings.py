"""add platform_settings.video_provider / video_provider_id / video_model

The Video generation setting (Settings > AI models), beside the image one:
the provider and model AI clips are made with (``router.get_video_provider``).
Unset, clips use the first provider with a built-in video model.

Three nullable columns with no default (metadata-only), in ONE ``ALTER
TABLE`` so the lock is taken once, following the canonical lock-safe recipe:
idempotency probe -> terminate_lock_holders -> set_lock_timeout -> guarded DDL.

Revision ID: v5774vgen03
Revises: v5774gadflt02
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "v5774vgen03"
down_revision: str | None = "v5774gadflt02"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "platform_settings"
_COLUMNS = (
    ("video_provider", "VARCHAR(50)"),
    ("video_provider_id", "UUID"),
    ("video_model", "VARCHAR(100)"),
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
    present = [name for name, _ in _COLUMNS if name not in {n for n, _ in _missing_columns(conn)}]
    if not present:
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    drops = ", ".join(f"DROP COLUMN IF EXISTS {name}" for name in present)
    op.execute(f"ALTER TABLE {_TABLE} {drops}")
