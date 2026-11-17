"""keep the prose a source wrote, so a citation has something to point into

Adds ``investigation_steps.answer``.

A provider web search does not return links — it returns an ANSWER, with its
sources attached to spans of that answer. Those spans are offsets, and offsets
into text nobody stored point nowhere. Without this column the feature can
report that a source was consulted and never show which sentence it supports,
which is the whole difference between provenance and a citation.

Nullable, with no default and no backfill. Only a source that writes has an
answer: a fetched page and a corpus passage cite a document instead, and their
quotes live in ``evidence``. Every existing step is one of those, so NULL is
not a gap to fill — it is the correct value, and inventing prose for a step
that never produced any would be the exact failure this feature exists to
avoid.

Idempotent: guarded by a column-existence probe. Follows the canonical
lock-safe recipe: probe -> terminate_lock_holders -> set_lock_timeout ->
guarded DDL. Nullable with no default, so the ``ALTER`` is a catalogue update
and rewrites no rows.

Revision ID: i5ans1939003
Revises: i4mcp1939002
Create Date: 2026-09-05 01:10:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

# revision identifiers, used by Alembic.
revision: str = "i5ans1939003"
down_revision: str | Sequence[str] | None = "i4mcp1939002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "investigation_steps"
_COLUMN = "answer"


def _column_exists(conn, table: str, column: str) -> bool:
    return (
        conn.execute(
            sa.text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = current_schema() "
                "AND table_name = :table AND column_name = :column"
            ),
            {"table": table, "column": column},
        ).first()
        is not None
    )


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if not inspector.has_table(_TABLE):
        return
    if _column_exists(conn, _TABLE, _COLUMN):
        return

    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)

    op.add_column(_TABLE, sa.Column(_COLUMN, sa.Text(), nullable=True))


def downgrade() -> None:
    """Drop the column, losing every retained answer.

    Stated rather than worked around: the citations in ``evidence`` keep their
    URLs and their quotes, so a reader still sees where a claim came from. What
    goes is the prose those spans indexed into, and with it the ability to
    render a citation inline rather than as a list.
    """
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if not inspector.has_table(_TABLE):
        return
    if not _column_exists(conn, _TABLE, _COLUMN):
        return

    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)

    op.drop_column(_TABLE, _COLUMN)
