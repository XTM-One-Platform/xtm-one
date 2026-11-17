"""add the Video Studio media retention and the studio conversation binding

* ``video_takes`` / ``video_clips``: ``keep`` (exempt from the retention),
  ``last_used_at`` (when a video last used the media) and ``expired_at`` (when
  the retention deleted them) - ``services/video_studio/retention.py``;
  ``video_clips.name`` (what the person calls an AI shot).
* ``conversations.video_project_id``: the video a studio conversation was
  opened to edit, the twin of ``presentation_id``. No foreign key and no
  index: ``conversations`` is the hottest table and the code needs neither
  (the studio lists the caller's own conversations, the ``user_id`` index
  narrows first; a deleted video leaves an ordinary chat).

Every column is nullable or has a constant default (metadata-only, PG 11+),
one ``ALTER TABLE`` per table so each lock is taken once, each behind the
canonical recipe: idempotency probe -> terminate_lock_holders ->
set_lock_timeout -> guarded DDL.

Revision ID: v5826vstudio01
Revises: v5799speech01
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "v5826vstudio01"
down_revision: str | None = "v5799speech01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RETENTION = (
    ("keep", "BOOLEAN NOT NULL DEFAULT false"),
    ("last_used_at", "TIMESTAMP WITH TIME ZONE"),
    ("expired_at", "TIMESTAMP WITH TIME ZONE"),
)
_TABLES: dict[str, tuple[tuple[str, str], ...]] = {
    "video_takes": _RETENTION,
    "video_clips": (*_RETENTION, ("name", "VARCHAR(200)")),
    "conversations": (("video_project_id", "UUID"),),
}


def _existing(conn, table: str) -> set[str]:
    return set(
        conn.execute(
            sa.text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = :table"
            ),
            {"table": table},
        ).scalars()
    )


def upgrade() -> None:
    conn = op.get_bind()
    for table, columns in _TABLES.items():
        existing = _existing(conn, table)
        missing = [(name, kind) for name, kind in columns if name not in existing]
        if not missing:
            continue
        terminate_lock_holders(conn, table)
        set_lock_timeout(conn)
        additions = ", ".join(f"ADD COLUMN IF NOT EXISTS {name} {kind}" for name, kind in missing)
        op.execute(f"ALTER TABLE {table} {additions}")


def downgrade() -> None:
    conn = op.get_bind()
    for table, columns in _TABLES.items():
        existing = _existing(conn, table)
        present = [name for name, _ in columns if name in existing]
        if not present:
            continue
        terminate_lock_holders(conn, table)
        set_lock_timeout(conn)
        drops = ", ".join(f"DROP COLUMN IF EXISTS {name}" for name in present)
        op.execute(f"ALTER TABLE {table} {drops}")
