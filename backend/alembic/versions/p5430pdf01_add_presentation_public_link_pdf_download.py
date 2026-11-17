"""add presentation_public_links.allow_pdf_download (a public link may offer a PDF)

One boolean column with a constant default: metadata-only on PostgreSQL 11+,
no rewrite, no backfill - every existing link keeps offering no download,
which is what it offered before. Old replicas during a rolling deploy
ignore the column; their inserts get the server default.

``ALTER TABLE`` takes a brief ACCESS EXCLUSIVE lock, so both directions follow
the canonical recipe: idempotency probe -> terminate_lock_holders ->
set_lock_timeout -> guarded DDL. The column mirrors the ORM model
(``PresentationPublicLink.allow_pdf_download``).

Revision ID: p5430pdf01
Revises: m5289heads01
Create Date: 2026-10-06 18:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "p5430pdf01"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "m5289heads01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "presentation_public_links"
_COLUMN = "allow_pdf_download"


def _has_column(conn) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = :t AND column_name = :c"
            ),
            {"t": _TABLE, "c": _COLUMN},
        ).scalar()
    )


def upgrade() -> None:
    conn = op.get_bind()
    if _has_column(conn):
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    op.execute(f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS {_COLUMN} BOOLEAN NOT NULL DEFAULT false")


def downgrade() -> None:
    conn = op.get_bind()
    if not _has_column(conn):
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    op.execute(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS {_COLUMN}")
