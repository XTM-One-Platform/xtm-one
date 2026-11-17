"""AI-disabled mode: a ``skipped`` flow-run status

A flow held back because it needs AI must be *recorded*, not silently dropped:
FR-016 requires a reason visible where runs are reviewed, and SC-005 requires
zero steps executed. ``cancelled`` would misreport it as someone's action, so
the check constraint gains one value.

Revision ID: ai2skipped02
Revises: ai1disabled01
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.core.migration_ddl import set_lock_timeout

revision: str = "ai2skipped02"
down_revision: str | None = "ai1disabled01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "flow_runs"
_CONSTRAINT = "ck_flow_runs_status"
_WITHOUT = "status IN ('pending','running','awaiting','completed','failed','cancelled')"
_WITH = (
    "status IN ('pending','running','awaiting','completed','failed',"
    "'cancelled','skipped')"
)


def _has_constraint(conn, table: str, name: str) -> bool:
    inspector = sa.inspect(conn)
    if not inspector.has_table(table):
        return False
    return any(c["name"] == name for c in inspector.get_check_constraints(table))


def _allows_skipped(conn) -> bool:
    """Whether the live constraint already admits ``skipped``.

    Read from the catalogue rather than tracked by revision, because the
    migrations CI job runs upgrade twice and a duplicate ``CREATE CONSTRAINT``
    is a hard failure.
    """
    row = conn.execute(
        sa.text(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conname = :name"
        ),
        {"name": _CONSTRAINT},
    ).scalar()
    return bool(row) and "skipped" in row


def upgrade() -> None:
    conn = op.get_bind()
    if _allows_skipped(conn):
        return
    set_lock_timeout(conn)
    # Drop-then-add: Postgres has no "modify check". Plain, deliberately — an
    # earlier version split this into ADD ... NOT VALID + VALIDATE CONSTRAINT to
    # avoid holding ACCESS EXCLUSIVE across the row scan. That bought nothing:
    # ``alembic/env.py`` wraps every migration in one transaction, so the lock
    # taken here is held until commit and the VALIDATE scan runs under it either
    # way. Splitting for real would need two migrations.
    #
    # The cost is therefore honest rather than mitigated: on a large
    # ``flow_runs`` history, writers wait for the scan. Acceptable because this
    # only *widens* the allowed set, so the scan cannot fail — every row that
    # satisfied the old constraint satisfies the new one.
    if _has_constraint(conn, _TABLE, _CONSTRAINT):
        op.drop_constraint(_CONSTRAINT, _TABLE, type_="check")
    op.create_check_constraint(_CONSTRAINT, _TABLE, _WITH)


def downgrade() -> None:
    conn = op.get_bind()
    if not _allows_skipped(conn):
        return
    set_lock_timeout(conn)
    # Any row already recorded as skipped would violate the narrower
    # constraint, so retire those first. They are held-back runs that never
    # executed a step, so cancelled is the closest surviving truth.
    conn.execute(
        sa.text("UPDATE flow_runs SET status = 'cancelled' WHERE status = 'skipped'")
    )
    if _has_constraint(conn, _TABLE, _CONSTRAINT):
        op.drop_constraint(_CONSTRAINT, _TABLE, type_="check")
    # Narrowing, so unlike the upgrade this scan can genuinely reject a row —
    # which is why the UPDATE above runs first. Validated immediately for the
    # same reason: an unvalidated narrowing constraint would be a lie.
    op.create_check_constraint(_CONSTRAINT, _TABLE, _WITHOUT)
