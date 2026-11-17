"""add knowledge_bases.is_internal — storage owned by another primitive

A Workset keeps its corpus in a KnowledgeBase so chunking, embedding,
hybrid search and the oversized-document guard are reused rather than
reimplemented. That KB must not appear in any list, picker or agent binding:
it is storage owned by another primitive, not a collection anyone browses.

``is_system_managed`` does NOT already mean that, despite the name — an agent's
auto-created KB carries it and is listed, browsable and bindable like any
other. Repurposing it would have hidden every agent's own collection, so this
adds a distinct flag rather than overloading one whose meaning is established.

Additive and lock-light: `ADD COLUMN ... DEFAULT false NOT NULL` is
metadata-only on PG 11+ (no table rewrite), and every existing row reads as
`false`, which is the pre-existing behaviour exactly. Old replicas during a
rolling deploy never select a column they do not know about.

Revision ID: w2ks3074002
Revises: w1ks3074001
Create Date: 2026-08-25 02:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

revision: str = "w2ks3074002"
down_revision: str | Sequence[str] | None = "w1ks3074001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "knowledge_bases"
_COLUMN = "is_internal"


def _has_column(conn, table: str, column: str) -> bool:
    return bool(
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = current_schema() "
                "AND table_name = :t AND column_name = :c"
            ),
            {"t": table, "c": column},
        ).scalar()
    )


def upgrade() -> None:
    conn = op.get_bind()

    # 1. Idempotency probe + early return, so a steady-state re-run (fresh
    #    installs stamp head; a retrying replica re-enters here) takes NO lock
    #    at all.
    if _has_column(conn, _TABLE, _COLUMN):
        return

    # 2. Pre-flight: kill same-role lock holders so the ALTER acquires its
    #    brief ACCESS EXCLUSIVE instantly. There is no cold table — an agentic
    #    loop holds a session open for minutes, and every subsequent reader
    #    queues behind a blocked ALTER.
    terminate_lock_holders(conn, _TABLE)
    # 3. Short lock_timeout belt: a racing reconnect fails fast (55P03) rather
    #    than wedging the lock queue; the startup runner retries.
    set_lock_timeout(conn)

    # 4. The guarded DDL. A constant default is metadata-only — no rewrite.
    op.execute(
        sa.text(
            f"ALTER TABLE {_TABLE} ADD COLUMN IF NOT EXISTS {_COLUMN} "
            "BOOLEAN NOT NULL DEFAULT false"
        )
    )


def downgrade() -> None:
    conn = op.get_bind()

    if not _has_column(conn, _TABLE, _COLUMN):
        return
    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)
    op.execute(sa.text(f"ALTER TABLE {_TABLE} DROP COLUMN IF EXISTS {_COLUMN}"))
