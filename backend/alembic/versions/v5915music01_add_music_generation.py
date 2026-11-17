"""add the Music generation setting and the Video Studio's AI music tracks

* ``platform_settings.music_provider`` / ``music_provider_id`` /
  ``music_model``: the Music generation setting (Settings > AI models),
  beside the speech and video ones (``router.get_music_provider``). Three
  nullable columns with no default (metadata-only) in ONE ``ALTER TABLE``,
  under the canonical lock-safe recipe: idempotency probe ->
  terminate_lock_holders -> set_lock_timeout -> guarded DDL.
* ``video_music``: one AI-composed track. A new table with no foreign key
  (the owner is a plain id, like the file references), so creating it takes
  no lock on an existing table; its index is built in place on the new,
  empty table.

Revision ID: v5915music01
Revises: vcapburn5905
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "v5915music01"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "vcapburn5905"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SETTINGS = "platform_settings"
_COLUMNS = (
    ("music_provider", "VARCHAR(50)"),
    ("music_provider_id", "UUID"),
    ("music_model", "VARCHAR(100)"),
)
_TABLE = "video_music"


def _existing_columns(conn) -> set[str]:
    return set(
        conn.execute(
            sa.text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = :table"
            ),
            {"table": _SETTINGS},
        ).scalars()
    )


def _has_table(conn, name: str) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.tables WHERE table_schema = current_schema() AND table_name = :n"
            ),
            {"n": name},
        ).scalar()
    )


def _add_settings_columns(conn) -> None:
    existing = _existing_columns(conn)
    missing = [(name, kind) for name, kind in _COLUMNS if name not in existing]
    if not missing:
        return
    terminate_lock_holders(conn, _SETTINGS)
    set_lock_timeout(conn)
    additions = ", ".join(f"ADD COLUMN IF NOT EXISTS {name} {kind}" for name, kind in missing)
    op.execute(f"ALTER TABLE {_SETTINGS} {additions}")


def _create_video_music(conn) -> None:
    if _has_table(conn, _TABLE):
        return
    op.create_table(
        _TABLE,
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("name", sa.String(200), nullable=True),
        sa.Column("provider", sa.String(40), nullable=False),
        sa.Column("model", sa.String(120), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("params", postgresql.JSON(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default=sa.text("'pending'")),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("file_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("progress", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("execution_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        if_not_exists=True,
    )
    op.create_index("ix_video_music_user_id", _TABLE, ["user_id"], if_not_exists=True)
    op.create_index("ix_video_music_user_created", _TABLE, ["user_id", "created_at"], if_not_exists=True)


def upgrade() -> None:
    conn = op.get_bind()
    _add_settings_columns(conn)
    _create_video_music(conn)


def downgrade() -> None:
    conn = op.get_bind()
    if _has_table(conn, _TABLE):
        op.drop_table(_TABLE, if_exists=True)
    present = [name for name, _ in _COLUMNS if name in _existing_columns(conn)]
    if not present:
        return
    terminate_lock_holders(conn, _SETTINGS)
    set_lock_timeout(conn)
    drops = ", ".join(f"DROP COLUMN IF EXISTS {name}" for name in present)
    op.execute(f"ALTER TABLE {_SETTINGS} {drops}")
