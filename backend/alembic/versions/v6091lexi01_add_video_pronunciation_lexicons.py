"""add video_pronunciation_lexicons

The platform's pronunciation lexicon of the Video Studio voice-over:
one row per version, written once and never updated - the newest is the
lexicon every video reads. A new table with no foreign key (the author is a
bare id and a label), so it takes no lock on an existing table and old
replicas during a rolling deploy ignore it. Nothing is backfilled: the first
version is seeded at startup from the platform's data file.

The column set and the unnamed UNIQUE clause mirror the ORM model
(``VideoPronunciationLexicon`` in ``app/models/video_studio.py``), so a fresh
install's ``create_all`` and this body produce the same constraint names.

Revision ID: v6091lexi01
Revises: v6065avqa01
Create Date: 2026-10-20 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout

revision: str = "v6091lexi01"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "v6065avqa01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "video_pronunciation_lexicons"


def upgrade() -> None:
    conn = op.get_bind()
    if sa.inspect(conn).has_table(_TABLE):
        return
    set_lock_timeout(conn)
    op.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {_TABLE} (
            id UUID PRIMARY KEY,
            version INTEGER NOT NULL UNIQUE,
            entries JSON NOT NULL,
            note TEXT,
            created_by_user_id UUID,
            created_by_label VARCHAR(320),
            created_at TIMESTAMPTZ NOT NULL
        )
        """
    )


def downgrade() -> None:
    conn = op.get_bind()
    if not sa.inspect(conn).has_table(_TABLE):
        return
    set_lock_timeout(conn)
    op.execute(f"DROP TABLE IF EXISTS {_TABLE}")
