"""add video_projects.asset_refs

Which videos use a recording, an AI shot or an AI track
was read by loading up to 500 visible videos and scanning each spec in Python:
past that cap a video's media were refused to the people it is shared with, and
a failed shot only an older video used was hidden from its editor. The project
now stores the media its spec names - ``"<kind>:<id>"``, written on every flush
of the row (``models.video_studio._index_asset_refs``) - behind a GIN index on
``asset_refs::jsonb``: one indexed lookup answers the question for any number
of videos.

Nullable with no default, so the ``ADD COLUMN`` is metadata-only. The existing
videos are indexed from their specs in bounded batches, each committed on its
own (``autocommit_block``), the same paths ``spec.referenced_ids`` reads; a row
an older replica writes during the rollout is NULL and indexed by the media
retention before it reads (``retention.index_missing_refs``). The index is
built in place (``create_index_in_place``). A re-run is a no-op: an indexed row
is no longer NULL. Fresh installs run ``create_all`` + ``stamp head``.

Revision ID: v5921arefs01
Revises: v6091lexi01
Create Date: 2026-10-20 09:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import (
    DEFAULT_LOCK_TIMEOUT,
    create_index_in_place,
    set_lock_timeout,
    terminate_lock_holders,
)

revision: str = "v5921arefs01"
down_revision: str | None = "v6091lexi01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "video_projects"
_INDEX = "ix_video_projects_asset_refs_jsonb_gin"
_BATCH_SIZE = 200

#: ``(kind, jsonpath)`` of every media id a spec names, as ``spec.referenced_ids`` reads them.
_PATHS = (
    ("take", '$.scenes[*] ? (@.type == "recording").take_id'),
    ("clip", '$.scenes[*] ? (@.type == "clip").clip_id'),
    ("clip", '$.scenes[*] ? (@.type == "image").animate.clip_id'),
    ("clip", '$.scenes[*] ? (@.type == "statement").backdrop.clip_id'),
    ("take", '$.scenes[*].overlays[*] ? (@.type == "screen_insert").source.take_id'),
    ("clip", '$.scenes[*].overlays[*] ? (@.type == "screen_insert").source.clip_id'),
    ("music", "$.music.music_id"),
)
_UUID = "[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_REFS = " UNION ALL ".join(
    f"SELECT '{kind}:' || lower(jsonb_path_query(p.spec::jsonb, '{path}') #>> '{{}}') AS ref"
    for kind, path in _PATHS
)
_BACKFILL = (
    f"UPDATE {_TABLE} p SET asset_refs = COALESCE("  # noqa: S608 - constants only
    f"(SELECT json_agg(DISTINCT r.ref ORDER BY r.ref) FROM ({_REFS}) r WHERE r.ref ~ '^[a-z]+:{_UUID}$'), "
    "'[]'::json) "
    f"WHERE p.id IN (SELECT id FROM {_TABLE} WHERE asset_refs IS NULL ORDER BY id LIMIT :n)"
)


def _has_column(conn) -> bool | None:
    """Whether ``video_projects.asset_refs`` exists; ``None`` when the table does not."""
    inspector = sa.inspect(conn)
    if not inspector.has_table(_TABLE):
        return None
    return "asset_refs" in {c["name"] for c in inspector.get_columns(_TABLE)}


def backfill_in_batches(conn, batch_size: int = _BATCH_SIZE) -> int:
    """Index the videos not indexed yet, *batch_size* at a time; returns how many."""
    previous = conn.execute(sa.text("SELECT current_setting('lock_timeout')")).scalar()
    conn.execute(sa.text("SELECT set_config('lock_timeout', :t, false)"), {"t": DEFAULT_LOCK_TIMEOUT})
    total = 0
    try:
        while True:
            result = conn.execute(sa.text(_BACKFILL), {"n": batch_size})
            if not result.rowcount:
                return total
            total += result.rowcount
    finally:
        conn.execute(sa.text("SELECT set_config('lock_timeout', :t, false)"), {"t": previous})


def upgrade() -> None:
    conn = op.get_bind()
    state = _has_column(conn)
    if state is None:
        return
    if state is False:
        terminate_lock_holders(conn, _TABLE)
        set_lock_timeout(conn)
        op.execute(f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS asset_refs JSON")
    with op.get_context().autocommit_block():
        backfill_in_batches(op.get_bind())
        create_index_in_place(op.get_bind(), _INDEX, _TABLE, "USING gin ((asset_refs::jsonb))")


def downgrade() -> None:
    conn = op.get_bind()
    if not _has_column(conn):
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    op.execute(f"DROP INDEX IF EXISTS {_INDEX}")
    op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS asset_refs")
