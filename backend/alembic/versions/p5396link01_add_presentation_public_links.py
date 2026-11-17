"""add presentation_public_links (a deck anyone with the link may view)

Purely additive: one new table, no change to any existing column, so old
replicas during a rolling deploy ignore a table they do not know about.
Nothing is backfilled - no deck had a public link before.

``presentation_public_links`` holds at most one row per deck
(``presentation_id`` unique): the SHA-256 of the URL token a public request is
looked up by, the same token Fernet-encrypted so a manager can copy the link
again, who issued it and when.

The column set and the unnamed UNIQUE / REFERENCES clauses mirror the ORM
model (``PresentationPublicLink`` in ``app/models/presentation.py``), so a
fresh install's ``create_all`` and this body produce the same constraint
names.

Revision ID: p5396link01
Revises: k1hub0001
Create Date: 2026-10-06 13:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout

revision: str = "p5396link01"
# NOT a constant: must always be main's CURRENT head, re-pointed whenever main
# gains a migration (backend/AGENTS.md > "Database migrations (safety)", rule 1).
down_revision: str | Sequence[str] | None = "k1hub0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "presentation_public_links"


def _has_table(conn, name: str) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = current_schema() AND table_name = :n"
            ),
            {"n": name},
        ).scalar()
    )


def upgrade() -> None:
    conn = op.get_bind()
    # Idempotency probe: a fresh install runs create_all + stamp head (this
    # body never executes) and the migrations CI job re-runs the revision.
    if _has_table(conn, _TABLE):
        return
    # Nothing knows this table yet, so nothing holds a lock on it. Its foreign
    # keys install triggers on `presentations` and `users` (SHARE ROW
    # EXCLUSIVE there): the belt makes a contended attempt fail fast (55P03,
    # retried by the startup runner) instead of queueing every writer of those
    # tables behind it. Terminating their holders would kill sessions that
    # merely read them, which this lock does not conflict with.
    set_lock_timeout(conn)
    op.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {_TABLE} (
            id UUID PRIMARY KEY,
            presentation_id UUID NOT NULL UNIQUE
                REFERENCES presentations (id) ON DELETE CASCADE,
            token_hash VARCHAR(64) NOT NULL UNIQUE,
            token_enc TEXT NOT NULL,
            created_by_user_id UUID REFERENCES users (id) ON DELETE SET NULL,
            created_at TIMESTAMPTZ NOT NULL
        )
        """
    )


def downgrade() -> None:
    conn = op.get_bind()
    if not _has_table(conn, _TABLE):
        return
    # DROP TABLE removes the foreign key triggers on `presentations` and
    # `users` (ACCESS EXCLUSIVE there): the belt makes a busy table fail the
    # downgrade fast, to be retried when it is quiet.
    set_lock_timeout(conn)
    op.execute(f"DROP TABLE IF EXISTS {_TABLE}")
