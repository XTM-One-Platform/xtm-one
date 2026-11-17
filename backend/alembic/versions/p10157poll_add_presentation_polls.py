"""Add live presentation polls: presentation_polls and presentation_poll_votes.

Two new tables, both additive (xtm-one #10157):

* ``presentation_polls`` - the live state of one ``poll`` block of a deck
  (short audience code, the question the presenter opened, locked, revealed).
* ``presentation_poll_votes`` - one anonymous answer per device and question.

Safety contract (backend/AGENTS.md > "Database migrations (safety)"):

* Idempotent - every step probes first; fresh installs run ``create_all`` +
  ``stamp head`` so the body never runs there.
* Lock-safe - ``CREATE TABLE`` on a new name; the FK to ``presentations``
  takes a ShareRowExclusive lock on it, so it follows the recipe
  (``terminate_lock_holders`` then ``set_lock_timeout``) and is added in the
  CREATE TABLE of a table nobody reads yet.
* Reversible - ``downgrade`` drops both tables.

Revision ID: p10157poll
Revises: f10162fltconv1
Create Date: 2026-11-08
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "p10157poll"
down_revision: str | None = "f10162fltconv1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _table_exists(conn, table: str) -> bool:
    return bool(conn.execute(sa.text("SELECT to_regclass(:name) IS NOT NULL"), {"name": table}).scalar())


def upgrade() -> None:
    conn = op.get_bind()
    if not _table_exists(conn, "presentation_polls"):
        terminate_lock_holders(conn, "presentations")
        set_lock_timeout(conn)
        conn.execute(
            sa.text(
                """
                CREATE TABLE IF NOT EXISTS presentation_polls (
                    id UUID PRIMARY KEY,
                    presentation_id UUID NOT NULL REFERENCES presentations (id) ON DELETE CASCADE,
                    poll_key VARCHAR(32) NOT NULL,
                    code VARCHAR(12) NOT NULL,
                    active_question VARCHAR(32),
                    locked BOOLEAN NOT NULL DEFAULT false,
                    revealed BOOLEAN NOT NULL DEFAULT false,
                    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
                    CONSTRAINT uq_presentation_polls_key UNIQUE (presentation_id, poll_key),
                    CONSTRAINT presentation_polls_code_key UNIQUE (code)
                )
                """
            )
        )
    set_lock_timeout(conn)
    conn.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS ix_presentation_polls_presentation_id "
            "ON presentation_polls (presentation_id)"
        )
    )
    if not _table_exists(conn, "presentation_poll_votes"):
        set_lock_timeout(conn)
        conn.execute(
            sa.text(
                """
                CREATE TABLE IF NOT EXISTS presentation_poll_votes (
                    id UUID PRIMARY KEY,
                    poll_id UUID NOT NULL REFERENCES presentation_polls (id) ON DELETE CASCADE,
                    question_key VARCHAR(32) NOT NULL,
                    voter_hash VARCHAR(64) NOT NULL,
                    value VARCHAR(200) NOT NULL,
                    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
                    CONSTRAINT uq_presentation_poll_votes_voter UNIQUE (poll_id, question_key, voter_hash)
                )
                """
            )
        )
    set_lock_timeout(conn)
    conn.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS ix_presentation_poll_votes_poll_id ON presentation_poll_votes (poll_id)"
        )
    )


def downgrade() -> None:
    conn = op.get_bind()
    set_lock_timeout(conn)
    conn.execute(sa.text("DROP TABLE IF EXISTS presentation_poll_votes"))
    if _table_exists(conn, "presentation_polls"):
        terminate_lock_holders(conn, "presentations")
        set_lock_timeout(conn)
        conn.execute(sa.text("DROP TABLE IF EXISTS presentation_polls"))
