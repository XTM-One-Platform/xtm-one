"""add the heartbeat and stage of every Video Studio job, and the provider state of an AI shot

What a caller is told of a job - queued or running, its stage, when
the worker last moved it - is read from the job's own row for every kind,
as it already was for a render:

* ``progress_at`` on ``video_takes``, ``video_clips`` and ``video_music``:
  the worker's last progress write, so a job it has written one for is never
  reported as queued;
* ``stage`` on ``video_clips`` and ``video_music``: what the job is doing;
* ``provider_state`` and ``provider_eta_at`` on ``video_clips``: what the
  provider last said of the upstream job, and when the shot should be made.

Nullable with no default: a job before this one has none, so each
``ADD COLUMN`` is metadata-only and needs no backfill.

Revision ID: v5955jobst01
Revises: v5951rprog01
Create Date: 2026-10-18 10:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "v5955jobst01"
down_revision: str | None = "v5951rprog01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLUMNS: dict[str, tuple[tuple[str, str], ...]] = {
    "video_takes": (("progress_at", "TIMESTAMP WITH TIME ZONE"),),
    "video_clips": (
        ("stage", "VARCHAR(200)"),
        ("provider_state", "VARCHAR(40)"),
        ("provider_eta_at", "TIMESTAMP WITH TIME ZONE"),
        ("progress_at", "TIMESTAMP WITH TIME ZONE"),
    ),
    "video_music": (
        ("stage", "VARCHAR(200)"),
        ("progress_at", "TIMESTAMP WITH TIME ZONE"),
    ),
}


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    for table, columns in _COLUMNS.items():
        if not inspector.has_table(table):
            continue
        present = {c["name"] for c in inspector.get_columns(table)}
        missing = [(name, kind) for name, kind in columns if name not in present]
        if not missing:
            continue
        terminate_lock_holders(conn, table)
        set_lock_timeout(conn)
        additions = ", ".join(f"ADD COLUMN IF NOT EXISTS {name} {kind}" for name, kind in missing)
        op.execute(f"ALTER TABLE {table} {additions}")


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    for table, columns in _COLUMNS.items():
        if not inspector.has_table(table):
            continue
        present = {c["name"] for c in inspector.get_columns(table)}
        there = [name for name, _kind in columns if name in present]
        if not there:
            continue
        terminate_lock_holders(conn, table)
        set_lock_timeout(conn)
        drops = ", ".join(f"DROP COLUMN IF EXISTS {name}" for name in there)
        op.execute(f"ALTER TABLE {table} {drops}")
