"""store the synthesised report, and how much of it is sourced

Adds ``investigations.report``, ``report_sources``, ``report_claims`` and
``report_uncited_claims``.

A run could say what each source answered and never what they added up to.
The synthesis that does is written against the passages the run retained, and
every marker in it is checked against them — so the report and the numbered
passages it cites have to be stored together. A report kept without its
sources would be prose with markers pointing at nothing.

All four are nullable with no default and no backfill. NULL is not a gap to
fill: it is what a run WITHOUT a report looks like, and that is a real
outcome. The run may have retained nothing a report could stand on, no model
may have been available, or the draft may have kept citing excerpts that were
never supplied — in which case nothing is published, deliberately. Inventing
a report for the runs that already exist would be the exact failure this
feature is built to prevent.

``report_sources`` holds only the passages the report actually cites, which is
why it is stored rather than recomputed from the steps: the steps know what
was read, and only the draft knows what was used.

Idempotent: guarded by column-existence probes. Follows the canonical
lock-safe recipe: probe -> terminate_lock_holders -> set_lock_timeout ->
guarded DDL. Every column is nullable with no default, so the ``ALTER`` is a
catalogue update and rewrites no rows.

Revision ID: i6rep1939004
Revises: i5ans1939003
Create Date: 2026-09-05 02:05:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout, terminate_lock_holders

# revision identifiers, used by Alembic.
revision: str = "i6rep1939004"
down_revision: str | Sequence[str] | None = "i5ans1939003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "investigations"
_COLUMNS: tuple[tuple[str, sa.types.TypeEngine], ...] = (
    ("report", sa.Text()),
    ("report_sources", sa.JSON()),
    ("report_claims", sa.Integer()),
    ("report_uncited_claims", sa.Integer()),
)


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

    missing = [(n, t) for n, t in _COLUMNS if not _column_exists(conn, _TABLE, n)]
    if not missing:
        return

    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)

    for name, type_ in missing:
        op.add_column(_TABLE, sa.Column(name, type_, nullable=True))


def downgrade() -> None:
    """Drop all four, losing every report.

    Stated rather than worked around: the steps keep their evidence and their
    retained answers, so what each source found survives. What goes is the
    synthesis across them, which cannot be recovered without asking a model
    again — and re-upgrading does not bring it back.
    """
    conn = op.get_bind()
    inspector = sa.inspect(conn)
    if not inspector.has_table(_TABLE):
        return

    present = [n for n, _ in _COLUMNS if _column_exists(conn, _TABLE, n)]
    if not present:
        return

    terminate_lock_holders(conn, _TABLE)
    set_lock_timeout(conn)

    for name in present:
        op.drop_column(_TABLE, name)
