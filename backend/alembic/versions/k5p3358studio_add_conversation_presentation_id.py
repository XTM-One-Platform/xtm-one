"""Add conversations.presentation_id for the Presentation Studio.

A studio conversation is a chat opened next to a deck's live preview and bound
to that deck for its whole life: the binding is what lets the chat layer tell
the agent which presentation is being worked on, guarantee it the deck tools,
and let the viewer resume the right conversation. One nullable column, its FK
and its index; every existing conversation stays NULL.

Safety contract (backend/AGENTS.md > "Database migrations (safety)"):

* Idempotent — ``ADD COLUMN IF NOT EXISTS``, guarded FK add, ``CREATE INDEX
  CONCURRENTLY IF NOT EXISTS``. Fresh installs run ``create_all`` + ``stamp
  head`` so the body never executes there; existing installs run it exactly
  once, and a crashed replica may retry.
* Lock-safe — ``conversations`` is one of the hottest tables on the platform
  (every chat turn bumps ``updated_at``). The ``ADD COLUMN`` is nullable with
  no default (metadata-only, PG 11+) and follows the canonical recipe:
  idempotency probe, ``terminate_lock_holders`` pre-flight, ``set_lock_timeout``
  belt. The FK is added ``NOT VALID`` (no scan of either table under the
  ShareRowExclusive lock) and validated separately under ShareUpdateExclusive,
  which lets chat traffic through. The index is built ``CONCURRENTLY`` inside
  an autocommit block.

``presentations.conversation_id`` already points the other way, so the two
tables now reference each other; the model marks this side ``use_alter`` and
this migration adds it after both tables exist, so neither ``create_all`` nor
the upgrade path has an ordering problem.

Revision ID: k5p3358studio
Revises: j4p3358collab
Create Date: 2026-09-09
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "k5p3358studio"
down_revision: str | None = "j4p3358collab"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_FK_NAME = "fk_conversations_presentation_id_presentations"
_INDEX_NAME = "ix_conversations_presentation_id"


def _column_exists(conn, table: str, column: str) -> bool:
    return bool(
        conn.execute(
            sa.text("SELECT 1 FROM information_schema.columns WHERE table_name = :t AND column_name = :c"),
            {"t": table, "c": column},
        ).scalar()
    )


def _constraint_exists(conn, name: str) -> bool:
    return bool(conn.execute(sa.text("SELECT 1 FROM pg_constraint WHERE conname = :n"), {"n": name}).scalar())


def upgrade() -> None:
    conn = op.get_bind()

    if not _column_exists(conn, "conversations", "presentation_id"):
        terminate_lock_holders(conn, "conversations")
        set_lock_timeout(conn)
        conn.execute(sa.text("ALTER TABLE conversations ADD COLUMN IF NOT EXISTS presentation_id UUID"))

    # NOT VALID first: the stronger lock is held only for the catalog change,
    # never for a scan of ``conversations``. VALIDATE then re-checks the rows
    # (all NULL today) under a lock that lets reads and writes through.
    if not _constraint_exists(conn, _FK_NAME):
        terminate_lock_holders(conn, "conversations")
        terminate_lock_holders(conn, "presentations")
        set_lock_timeout(conn)
        conn.execute(
            sa.text(
                f"ALTER TABLE conversations ADD CONSTRAINT {_FK_NAME} "
                "FOREIGN KEY (presentation_id) REFERENCES presentations (id) "
                "ON DELETE SET NULL NOT VALID"
            )
        )
    set_lock_timeout(conn)
    conn.execute(sa.text(f"ALTER TABLE conversations VALIDATE CONSTRAINT {_FK_NAME}"))

    with op.get_context().autocommit_block():
        op.execute(sa.text(f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {_INDEX_NAME} ON conversations (presentation_id)"))


def downgrade() -> None:
    conn = op.get_bind()
    with op.get_context().autocommit_block():
        op.execute(sa.text(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX_NAME}"))
    if _column_exists(conn, "conversations", "presentation_id"):
        terminate_lock_holders(conn, "conversations")
        set_lock_timeout(conn)
        conn.execute(sa.text(f"ALTER TABLE conversations DROP CONSTRAINT IF EXISTS {_FK_NAME}"))
        conn.execute(sa.text("ALTER TABLE conversations DROP COLUMN IF EXISTS presentation_id"))
