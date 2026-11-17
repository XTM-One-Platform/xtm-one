"""add platform_settings.speech_provider / speech_provider_id / speech_model / speech_voice

The Speech generation setting (Settings > AI models), beside the image and
video ones: the provider, model and voice the chat's read-aloud and the Video
Studio voice-over speak with (``router.get_speech_provider``). Unset, speech
uses the first provider with a built-in speech model.

Four nullable columns with no default (metadata-only), in ONE ``ALTER
TABLE`` so the lock is taken once, following the canonical lock-safe recipe:
idempotency probe -> terminate_lock_holders -> set_lock_timeout -> guarded DDL.

Revision ID: v5799speech01
Revises: v5774vgen03
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "v5799speech01"
down_revision: str | None = "v5774vgen03"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "platform_settings"
_COLUMNS = (
    ("speech_provider", "VARCHAR(50)"),
    ("speech_provider_id", "UUID"),
    ("speech_model", "VARCHAR(100)"),
    ("speech_voice", "VARCHAR(200)"),
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
