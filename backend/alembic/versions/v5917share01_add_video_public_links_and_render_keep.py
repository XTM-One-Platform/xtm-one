"""add video_public_links and video_renders.keep

Sharing a video beyond its grants, and keeping its renders bounded:

* ``video_public_links`` holds at most one row per video (``project_id``
  unique): the SHA-256 of the URL token a public request is looked up by, the
  same token Fernet-encrypted so a manager can copy the link again, whether
  its visitors may download the MP4, who issued it and when. A new table, so
  old replicas during a rolling deploy ignore it; nothing is backfilled.
* ``video_renders.keep`` exempts a render from the render retention. A
  constant default, so the ``ADD COLUMN`` is metadata-only and existing
  renders read as not kept.

The column set and the unnamed UNIQUE / REFERENCES clauses mirror the ORM
model (``VideoPublicLink`` in ``app/models/video_studio.py``), so a fresh
install's ``create_all`` and this body produce the same constraint names.

Revision ID: v5917share01
Revises: v5915music01
Create Date: 2026-10-17 17:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "v5917share01"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "v5915music01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LINKS = "video_public_links"
_RENDERS = "video_renders"


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if inspector.has_table(_RENDERS) and "keep" not in {c["name"] for c in inspector.get_columns(_RENDERS)}:
        terminate_lock_holders(conn, _RENDERS)
        set_lock_timeout(conn)
        op.execute(f"ALTER TABLE {_RENDERS} ADD COLUMN IF NOT EXISTS keep BOOLEAN NOT NULL DEFAULT false")
    if inspector.has_table("video_projects") and not inspector.has_table(_LINKS):
        # The foreign keys install triggers on `video_projects` and `users`
        # (SHARE ROW EXCLUSIVE there): the belt makes a contended attempt fail
        # fast, retried by the startup runner, instead of queueing their
        # writers behind it.
        set_lock_timeout(conn)
        op.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {_LINKS} (
                id UUID PRIMARY KEY,
                project_id UUID NOT NULL UNIQUE
                    REFERENCES video_projects (id) ON DELETE CASCADE,
                token_hash VARCHAR(64) NOT NULL UNIQUE,
                token_enc TEXT NOT NULL,
                allow_download BOOLEAN NOT NULL DEFAULT false,
                created_by_user_id UUID REFERENCES users (id) ON DELETE SET NULL,
                created_at TIMESTAMPTZ NOT NULL
            )
            """
        )


def downgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if inspector.has_table(_LINKS):
        set_lock_timeout(conn)
        op.execute(f"DROP TABLE IF EXISTS {_LINKS}")
    if inspector.has_table(_RENDERS) and "keep" in {c["name"] for c in inspector.get_columns(_RENDERS)}:
        terminate_lock_holders(conn, _RENDERS)
        set_lock_timeout(conn)
        op.execute(f"ALTER TABLE {_RENDERS} DROP COLUMN IF EXISTS keep")
